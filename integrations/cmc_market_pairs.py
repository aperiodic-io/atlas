"""CoinMarketCap exchange market pairs: CMC's own venue-to-asset mapping.

For every market it tracks on an exchange, CMC publishes the venue's native
ticker (``derivativeTickerId`` or the trade URL) next to the CMC IDs of the
base and quote assets. That is identity evidence of a different order from a
ticker or a price: CMC has already decided which asset ``BTCUSDT`` on Binance
trades, and Atlas only has to find its own row for that native ticker.

The endpoint is CMC's keyless website API. It lists *currently* tracked markets
only, so it identifies active instruments; delisted rows are resolved from
these anchors by ``integrations/symbology.py``.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

import requests

from integrations.http_retry import retry_delay_seconds


CMC_MARKET_PAIRS_URL = (
    "https://api.coinmarketcap.com/data-api/v3/exchange/market-pairs/latest"
)
CMC_CATEGORIES = ("spot", "perpetual", "futures")
DEFAULT_PAGE_SIZE = 1000
REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MIN_INTERVAL_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 120

# Atlas instrument types each CMC market category may match.
_CATEGORY_TYPES = {"spot": "spot", "perpetual": "perpetual", "futures": "future"}

# Suffixes CMC appends to a venue's native derivative ticker.
_DERIVATIVE_SUFFIXES = ("_UMCBL", "_SUMCBL", "_DMCBL", "_CMCBL", "_SDMCBL", "_SCMCBL")

# Tickers different venues use for one asset. Only used to sanity-check that a
# matched row and CMC agree on what the base asset is called.
_TICKER_ALIASES = {"XBT": "BTC"}


@dataclass(frozen=True)
class CmcVenue:
    """A CMC exchange and the Atlas snapshots its market categories cover."""

    slug: str
    exchanges: Mapping[str, tuple[str, ...]]

    def __hash__(self) -> int:
        return hash(self.slug)


CMC_VENUES: dict[str, CmcVenue] = {
    venue.slug: venue
    for venue in (
        CmcVenue(
            "binance",
            {
                "spot": ("binance-spot",),
                "perpetual": ("binance-futures", "binance-futures-cm"),
                "futures": ("binance-futures", "binance-futures-cm"),
            },
        ),
        CmcVenue(
            "bybit",
            {
                "spot": ("bybit-spot",),
                "perpetual": ("bybit-perps-usd", "bybit-perps-coin"),
                "futures": ("bybit-futures",),
            },
        ),
        CmcVenue(
            "okx",
            {
                "spot": ("okx-spot",),
                "perpetual": ("okx-perps",),
                "futures": ("okx-futures",),
            },
        ),
        CmcVenue(
            "hyperliquid",
            {"spot": ("hyperliquid-spot",), "perpetual": ("hyperliquid-perps",)},
        ),
        CmcVenue("coinbase-exchange", {"spot": ("coinbase-spot",)}),
        CmcVenue(
            "bitget",
            {
                "spot": ("bitget-spot",),
                "perpetual": ("bitget-perps-usdt", "bitget-perps-usdc", "bitget-perps-coin"),
                "futures": ("bitget-futures-coin",),
            },
        ),
        CmcVenue(
            "gate",
            {
                "spot": ("gateio-spot",),
                "perpetual": ("gateio-perps-usdt", "gateio-perps-btc"),
                "futures": ("gateio-futures-usdt",),
            },
        ),
    )
}


class JsonResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class JsonSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> JsonResponse: ...


class CmcMarketPairsError(RuntimeError):
    """CMC did not return a usable market-pair listing."""


@dataclass(frozen=True)
class MarketPair:
    venue: str
    category: str
    market_pair: str
    base_cmc_id: int
    base_symbol: str
    base_name: str
    base_slug: str
    quote_cmc_id: int | None
    quote_symbol: str
    derivative_ticker_id: str | None
    market_url: str | None
    price_usd: float | None
    last_updated: datetime | None


@dataclass(frozen=True)
class MarketPairListing:
    venue: str
    category: str
    pairs: tuple[MarketPair, ...]
    reported_total: int | None
    duplicates: int
    malformed: int

    @property
    def complete(self) -> bool:
        """Whether every reported pair arrived intact."""
        return self.malformed == 0 and (
            self.reported_total is None or len(self.pairs) >= self.reported_total
        )


@dataclass(frozen=True)
class MatchResult:
    matches: dict[tuple[str, str], MarketPair]
    unmatched: tuple[MarketPair, ...]
    # A matched row whose ticker does not resemble CMC's base ticker: usually
    # an Atlas parser split (``ARBIDR`` read as AR/BIDR rather than ARB/IDR).
    symbol_mismatches: tuple[tuple[tuple[str, str], MarketPair], ...]
    # Rows two CMC pairs claim for different base assets.
    conflicts: tuple[tuple[str, str], ...]


def parse_market_pair(item: object, venue: str, category: str) -> MarketPair:
    """Parse one CMC market pair, raising ``ValueError`` if it names no base asset."""
    if not isinstance(item, dict):
        raise ValueError("market pair is not an object")
    base_cmc_id = _positive_int(item.get("baseCurrencyId"))
    base_symbol = item.get("baseSymbol")
    if base_cmc_id is None or not isinstance(base_symbol, str) or not base_symbol:
        raise ValueError(f"market pair without a base asset: {item.get('marketPair')!r}")
    price = item.get("price")
    return MarketPair(
        venue=venue,
        category=category,
        market_pair=str(item.get("marketPair") or ""),
        base_cmc_id=base_cmc_id,
        base_symbol=base_symbol,
        base_name=str(item.get("baseCurrencyName") or ""),
        base_slug=str(item.get("baseCurrencySlug") or ""),
        quote_cmc_id=_positive_int(item.get("quoteCurrencyId")),
        quote_symbol=str(item.get("quoteSymbol") or ""),
        derivative_ticker_id=_optional_str(item.get("derivativeTickerId")),
        market_url=_optional_str(item.get("marketUrl")),
        price_usd=float(price) if isinstance(price, int | float) and price > 0 else None,
        last_updated=_parse_timestamp(item.get("lastUpdated")),
    )


def native_id_candidates(pair: MarketPair) -> tuple[str, ...]:
    """Upper-case spellings of the venue ticker a CMC pair may be listed under.

    Ordered most specific first: CMC's derivative ticker, then the trade URL,
    then the pair name. Matching takes the first candidate Atlas has a row for.
    """
    candidates: list[str] = []
    ticker = pair.derivative_ticker_id
    if ticker:
        candidates.append(ticker)
        for suffix in _DERIVATIVE_SUFFIXES:
            if ticker.upper().endswith(suffix):
                candidates.append(ticker[: -len(suffix)])
        quote_suffix = f"_{pair.quote_symbol}"
        if pair.quote_symbol and ticker.upper().endswith(quote_suffix.upper()):
            candidates.append(ticker[: -len(quote_suffix)])
    segments = [
        unquote(segment)
        for segment in urlparse(pair.market_url or "").path.split("/")
        if segment
    ]
    if segments:
        last = segments[-1]
        candidates.append(last)
        candidates.append(last.replace("_", ""))
        if len(segments) >= 2:
            candidates.append(f"{segments[-2]}/{last}")
            candidates.append(f"{segments[-2]}{last}")
            candidates.append(f"{segments[-2]}-{last}")
    base, _, quote = pair.market_pair.partition("/")
    if base and quote:
        candidates.extend(
            (f"{base}{quote}", f"{base}-{quote}", f"{base}_{quote}", f"{base}/{quote}")
        )
    return tuple(dict.fromkeys(candidate.upper() for candidate in candidates if candidate))


def symbols_consistent(row_symbol: str, pair_symbol: str) -> bool:
    """Whether an Atlas base ticker and CMC's venue base ticker name one asset.

    Tolerates case, a Hyperliquid ``dex:`` prefix, a contract-multiplier
    prefix (``1000PEPE``, ``kPEPE``, ``1MBABYDOGE``) and known venue aliases.
    """
    return bool(_ticker_variants(row_symbol) & _ticker_variants(pair_symbol))


def _ticker_variants(symbol: str) -> set[str]:
    variants = {symbol.upper()}
    for value in list(variants):
        if ":" in value:
            variants.add(value.split(":", 1)[1])
    for value in list(variants):
        if (match := re.fullmatch(r"(?:10{3,}|1M)([A-Z0-9].*)", value)) or (match := re.fullmatch(r"[KM]([A-Z][A-Z0-9]{3,})", value)):
            variants.add(match.group(1))
    return variants | {_TICKER_ALIASES[value] for value in variants if value in _TICKER_ALIASES}


def fetch_market_pairs(
    session: JsonSession,
    venue: str,
    category: str,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
) -> MarketPairListing:
    """Fetch every CMC market pair of one category on one exchange."""
    if page_size <= 0 or max_attempts <= 0 or min_interval_seconds < 0:
        raise ValueError("invalid CMC market-pair request settings")
    pairs: dict[tuple[Any, ...], MarketPair] = {}
    duplicates = 0
    malformed = 0
    reported_total: int | None = None
    start = 1
    while True:
        data = _get_page(
            session,
            {"slug": venue, "category": category, "start": start, "limit": page_size},
            max_attempts,
            timeout_seconds,
        )
        if reported_total is None:
            reported_total = _positive_int(data.get("numMarketPairs"))
        page = data.get("marketPairs") or []
        if not isinstance(page, list):
            raise CmcMarketPairsError(f"{venue} {category}: marketPairs is not a list")
        for item in page:
            try:
                pair = parse_market_pair(item, venue, category)
            except ValueError:
                malformed += 1
                continue
            key = (
                item.get("marketId"),
                pair.derivative_ticker_id,
                pair.market_url,
                pair.market_pair,
            )
            if key in pairs:
                duplicates += 1
                continue
            pairs[key] = pair
        if len(page) < page_size:
            break
        start += len(page)
        if min_interval_seconds:
            time.sleep(min_interval_seconds)
    return MarketPairListing(
        venue=venue,
        category=category,
        pairs=tuple(pairs.values()),
        reported_total=reported_total,
        duplicates=duplicates,
        malformed=malformed,
    )


def fetch_venue_market_pairs(
    session: JsonSession,
    venue: str,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    **kwargs: Any,
) -> dict[str, MarketPairListing]:
    """Fetch every category of a venue that Atlas has snapshots for."""
    listings: dict[str, MarketPairListing] = {}
    for category in CMC_VENUES[venue].exchanges:
        listings[category] = fetch_market_pairs(
            session, venue, category, min_interval_seconds=min_interval_seconds, **kwargs
        )
        if min_interval_seconds:
            time.sleep(min_interval_seconds)
    return listings


def match_market_pairs(
    pairs: Iterable[MarketPair], rows_by_exchange: Mapping[str, list[dict]]
) -> MatchResult:
    """Attach each CMC pair to the active Atlas row trading its native ticker."""
    index: dict[tuple[str, str], list[tuple[str, dict]]] = defaultdict(list)
    for exchange, rows in rows_by_exchange.items():
        for row in rows:
            if row.get("end_date") or not isinstance(row.get("id"), str):
                continue
            index[(exchange, row["id"].upper())].append((exchange, row))

    claims: dict[tuple[str, str], list[MarketPair]] = defaultdict(list)
    unmatched: list[MarketPair] = []
    mismatches: list[tuple[tuple[str, str], MarketPair]] = []
    for pair in pairs:
        venue = CMC_VENUES.get(pair.venue)
        wanted_type = _CATEGORY_TYPES.get(pair.category)
        exchanges = venue.exchanges.get(pair.category, ()) if venue else ()
        found: tuple[str, dict] | None = None
        for candidate in native_id_candidates(pair):
            for exchange in exchanges:
                hits = [
                    hit
                    for hit in index.get((exchange, candidate), [])
                    if hit[1].get("type") == wanted_type
                ]
                if hits:
                    found = hits[0]
                    break
            if found:
                break
        if found is None:
            unmatched.append(pair)
            continue
        exchange, row = found
        key = (exchange, row["id"])
        if not symbols_consistent(str(row.get("symbol") or ""), pair.base_symbol):
            mismatches.append((key, pair))
            continue
        claims[key].append(pair)

    matches: dict[tuple[str, str], MarketPair] = {}
    conflicts: list[tuple[str, str]] = []
    for key, claimed in claims.items():
        if len({pair.base_cmc_id for pair in claimed}) > 1:
            conflicts.append(key)
            continue
        matches[key] = claimed[0]
    return MatchResult(
        matches=matches,
        unmatched=tuple(unmatched),
        symbol_mismatches=tuple(mismatches),
        conflicts=tuple(sorted(conflicts)),
    )


def _get_page(
    session: JsonSession,
    params: dict[str, Any],
    max_attempts: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = session.get(
                CMC_MARKET_PAIRS_URL,
                params=params,
                timeout=timeout_seconds,
                headers={"User-Agent": "Mozilla/5.0 (atlas-cmc-market-pairs/1.0)"},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise CmcMarketPairsError(
                    f"CMC market pairs response for {params['slug']} has no data"
                )
            return data
        except (requests.RequestException, ValueError, CmcMarketPairsError) as error:
            last_error = error
            if attempt + 1 < max_attempts:
                time.sleep(retry_delay_seconds(error, attempt, MAX_RETRY_DELAY_SECONDS))
    raise CmcMarketPairsError(
        f"failed to fetch CMC market pairs {params}: {last_error}"
    ) from last_error


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
