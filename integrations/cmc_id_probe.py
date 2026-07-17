"""Keyless, non-persisting CoinMarketCap price-compatibility probe.

The CMC website endpoint used here is undocumented. This module is deliberately
an exploratory tool: it reports catalogue integrity and price compatibility but
never establishes identity or writes a CMC mapping.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import requests


CMC_LISTING_URL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing"
BINANCE_SPOT_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"
DEFAULT_PAGE_SIZE = 5000
REQUEST_TIMEOUT_SECONDS = 20
CONTRACT_MULTIPLIERS = ("1000000", "100000", "10000", "1000")
COMMON_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "FDUSD", "USD", "BTC", "ETH")


class JsonResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class JsonSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> JsonResponse: ...


class CmcProbeError(RuntimeError):
    """The keyless probe cannot produce a trustworthy diagnostic."""


class ProbeStatus(StrEnum):
    NO_TICKER_CANDIDATE = "no_ticker_candidate"
    STALE_PRICE = "stale_price"
    PRICE_MISMATCH = "price_mismatch"
    MULTIPLE_PRICE_MATCHES = "multiple_price_matches"
    PRICE_COMPATIBLE = "price_compatible"


@dataclass(frozen=True)
class CmcAsset:
    cmc_id: int
    symbol: str
    slug: str
    price_usd: float
    last_updated: datetime
    is_active: bool


@dataclass(frozen=True)
class PriceObservation:
    price: float
    quote_currency: str
    observed_at: datetime
    venue: str
    instrument_id: str
    base_units_per_contract: float = 1.0

    @property
    def normalized_price(self) -> float:
        if self.base_units_per_contract <= 0:
            raise ValueError("base_units_per_contract must be positive")
        return self.price / self.base_units_per_contract


@dataclass(frozen=True)
class PriceMatch:
    asset: CmcAsset
    relative_difference: float
    timestamp_skew: timedelta


@dataclass(frozen=True)
class ProbeResult:
    status: ProbeStatus
    match: PriceMatch | None = None


@dataclass(frozen=True)
class CatalogueDiagnostics:
    reported_total: int | None
    returned_rows: int
    unique_ids: int
    duplicate_ids: tuple[int, ...]
    malformed_rows: int
    total_count_changed: bool

    @property
    def is_complete(self) -> bool:
        return (
            self.reported_total == self.unique_ids
            and not self.duplicate_ids
            and self.malformed_rows == 0
            and not self.total_count_changed
        )


@dataclass(frozen=True)
class CmcCatalogue:
    assets: tuple[CmcAsset, ...]
    diagnostics: CatalogueDiagnostics


def fetch_cmc_catalogue(  # noqa: PLR0912 - pagination diagnostics are intentionally explicit
    session: JsonSession,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_attempts: int = 3,
) -> CmcCatalogue:
    """Fetch CMC website listing pages and expose, rather than hide, anomalies."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")

    assets_by_id: dict[int, CmcAsset] = {}
    duplicate_ids: set[int] = set()
    malformed_rows = 0
    returned_rows = 0
    reported_total: int | None = None
    total_count_changed = False
    start = 1

    while True:
        payload = _get_json(
            session,
            CMC_LISTING_URL,
            {"start": start, "limit": page_size, "convert": "USD"},
            max_attempts,
        )
        if not isinstance(payload, dict):
            raise CmcProbeError("CMC response is not a JSON object")
        try:
            data = payload["data"]
            page = data["cryptoCurrencyList"]
        except (KeyError, TypeError) as error:
            raise CmcProbeError("CMC response has no cryptocurrency listing") from error
        if not isinstance(page, list):
            raise CmcProbeError("CMC cryptocurrency listing is not a list")

        total_count = data.get("totalCount")
        try:
            parsed_total = int(total_count) if total_count is not None else None
        except (TypeError, ValueError) as error:
            raise CmcProbeError(f"invalid CMC totalCount {total_count!r}") from error
        if reported_total is None:
            reported_total = parsed_total
        elif parsed_total != reported_total:
            total_count_changed = True

        returned_rows += len(page)
        for item in page:
            try:
                asset = _parse_cmc_asset(item)
            except (KeyError, TypeError, ValueError):
                malformed_rows += 1
                continue
            if asset.cmc_id in assets_by_id:
                duplicate_ids.add(asset.cmc_id)
                continue
            assets_by_id[asset.cmc_id] = asset

        if len(page) < page_size:
            break
        # CMC's website response may return more rows than requested. Advance by
        # the observed page size so the probe neither assumes nor conceals it.
        start += len(page)

    diagnostics = CatalogueDiagnostics(
        reported_total=reported_total,
        returned_rows=returned_rows,
        unique_ids=len(assets_by_id),
        duplicate_ids=tuple(sorted(duplicate_ids)),
        malformed_rows=malformed_rows,
        total_count_changed=total_count_changed,
    )
    return CmcCatalogue(tuple(assets_by_id.values()), diagnostics)


def fetch_binance_spot_price(
    session: JsonSession, base_symbol: str
) -> PriceObservation:
    """Fetch a timestamped Binance spot observation priced in USDT."""
    payload = _get_json(
        session,
        BINANCE_SPOT_TICKER_URL,
        {"symbol": f"{base_symbol.upper()}USDT"},
        max_attempts=3,
    )
    if not isinstance(payload, dict):
        raise CmcProbeError(f"invalid Binance spot ticker for {base_symbol}")
    try:
        price = float(payload["lastPrice"])
        close_time_ms = int(payload["closeTime"])
    except (KeyError, TypeError, ValueError) as error:
        raise CmcProbeError(f"invalid Binance spot ticker for {base_symbol}") from error
    if not math.isfinite(price) or price <= 0:
        raise CmcProbeError(f"invalid Binance spot price for {base_symbol}: {price}")
    return PriceObservation(
        price=price,
        quote_currency="USDT",
        observed_at=datetime.fromtimestamp(close_time_ms / 1000, tz=UTC),
        venue="binance-spot",
        instrument_id=f"{base_symbol.upper()}USDT",
    )


def fetch_binance_spot_prices(
    session: JsonSession, base_symbols: Iterable[str]
) -> dict[str, PriceObservation]:
    """Fetch one timestamped USDT spot observation for each requested base symbol."""
    payload = _get_json(session, BINANCE_SPOT_TICKER_URL, {}, max_attempts=3)
    if not isinstance(payload, list):
        raise CmcProbeError("Binance spot ticker response is not a list")
    return price_observations_from_binance_tickers(
        payload, base_symbols, venue="binance-spot"
    )


def price_observations_from_binance_tickers(
    tickers_payload: Iterable[object], base_symbols: Iterable[str], venue: str
) -> dict[str, PriceObservation]:
    """Select timestamped USDT prices from Binance spot or futures tickers."""
    requested_symbols = {symbol.upper() for symbol in base_symbols if symbol}
    tickers = {
        ticker.get("symbol"): ticker
        for ticker in tickers_payload
        if isinstance(ticker, dict) and isinstance(ticker.get("symbol"), str)
    }
    observations: dict[str, PriceObservation] = {}
    for base_symbol in requested_symbols:
        ticker = tickers.get(f"{base_symbol}USDT")
        if ticker is None:
            continue
        try:
            price = float(ticker["lastPrice"])
            close_time_ms = int(ticker["closeTime"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        observations[base_symbol] = PriceObservation(
            price=price,
            quote_currency="USDT",
            observed_at=datetime.fromtimestamp(close_time_ms / 1000, tz=UTC),
            venue=venue,
            instrument_id=f"{base_symbol}USDT",
        )
    return observations


def candidates_for_symbol(assets: Iterable[CmcAsset], symbol: str) -> list[CmcAsset]:
    return [asset for asset in assets if asset.symbol.upper() == symbol.upper()]


def normalize_cmc_lookup_symbol(symbol: str, cmc_symbols: set[str]) -> str:
    """Prefer an exact CMC ticker, then remove known contract multipliers."""
    upper_symbol = symbol.upper()
    candidates = [upper_symbol]
    for quote in COMMON_QUOTE_SUFFIXES:
        if upper_symbol.endswith(quote) and len(upper_symbol) > len(quote):
            candidates.append(upper_symbol[: -len(quote)])
            break
    for candidate in candidates:
        if candidate in cmc_symbols:
            return candidate
    for candidate in candidates:
        for multiplier in CONTRACT_MULTIPLIERS:
            if candidate.startswith(multiplier):
                unmultiplied = candidate[len(multiplier) :]
                if unmultiplied in cmc_symbols:
                    return unmultiplied
    return upper_symbol


def contract_multiplier_for_cmc_lookup(
    symbol: str, lookup_symbol: str, cmc_symbols: set[str]
) -> float:
    """Return base units represented by a multiplied Binance ticker."""
    upper_symbol = symbol.upper()
    base_symbol = upper_symbol
    for quote in COMMON_QUOTE_SUFFIXES:
        if upper_symbol.endswith(quote) and len(upper_symbol) > len(quote):
            base_symbol = upper_symbol[: -len(quote)]
            break
    if base_symbol == lookup_symbol or base_symbol in cmc_symbols:
        return 1.0
    for multiplier in CONTRACT_MULTIPLIERS:
        if base_symbol == f"{multiplier}{lookup_symbol}":
            return float(multiplier)
    return 1.0


def classify_price_candidates(
    candidates: Iterable[CmcAsset],
    exchange_price: PriceObservation,
    max_relative_difference: float,
    max_timestamp_skew: timedelta,
) -> ProbeResult:
    """Classify price evidence without asserting that it establishes identity."""
    if max_relative_difference <= 0:
        raise ValueError("max_relative_difference must be positive")
    if max_timestamp_skew < timedelta(0):
        raise ValueError("max_timestamp_skew must not be negative")
    if exchange_price.quote_currency not in {"USD", "USDT"}:
        raise ValueError("the probe supports only USD and USDT exchange quotes")
    if (
        not math.isfinite(exchange_price.normalized_price)
        or exchange_price.normalized_price <= 0
    ):
        raise ValueError("exchange price must be finite and positive")

    all_candidates = list(candidates)
    if not all_candidates:
        return ProbeResult(ProbeStatus.NO_TICKER_CANDIDATE)

    fresh_candidates = [
        candidate
        for candidate in all_candidates
        if abs(candidate.last_updated - exchange_price.observed_at)
        <= max_timestamp_skew
    ]
    if not fresh_candidates:
        return ProbeResult(ProbeStatus.STALE_PRICE)

    matches = [
        PriceMatch(
            asset=candidate,
            relative_difference=abs(
                candidate.price_usd - exchange_price.normalized_price
            )
            / exchange_price.normalized_price,
            timestamp_skew=abs(candidate.last_updated - exchange_price.observed_at),
        )
        for candidate in fresh_candidates
        if abs(candidate.price_usd - exchange_price.normalized_price)
        / exchange_price.normalized_price
        <= max_relative_difference
    ]
    if not matches:
        return ProbeResult(ProbeStatus.PRICE_MISMATCH)
    if len(matches) > 1:
        return ProbeResult(ProbeStatus.MULTIPLE_PRICE_MATCHES)
    return ProbeResult(ProbeStatus.PRICE_COMPATIBLE, matches[0])


def _get_json(
    session: JsonSession,
    url: str,
    params: dict[str, Any],
    max_attempts: int,
) -> Any:
    error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "atlas-cmc-probe/1.0",
                },
            )
            response.raise_for_status()
            return response.json()
        except (CmcProbeError, requests.RequestException, ValueError) as exc:
            error = exc
            if attempt + 1 < max_attempts:
                time.sleep(2**attempt)
    raise CmcProbeError(
        f"GET {url} failed after {max_attempts} attempts: {error}"
    ) from error


def _parse_cmc_asset(item: object) -> CmcAsset:
    if not isinstance(item, dict):
        raise ValueError("CMC asset is not an object")
    quotes = item.get("quotes")
    if not isinstance(quotes, list):
        raise ValueError("CMC asset has no quotes")
    usd_quote = next(
        (
            quote
            for quote in quotes
            if isinstance(quote, dict) and quote.get("name") == "USD"
        ),
        None,
    )
    if usd_quote is None:
        raise ValueError("CMC asset has no USD quote")
    price = float(usd_quote["price"])
    if not math.isfinite(price) or price <= 0:
        raise ValueError("CMC asset has invalid USD price")
    return CmcAsset(
        cmc_id=int(item["id"]),
        symbol=str(item["symbol"]),
        slug=str(item["slug"]),
        price_usd=price,
        last_updated=_parse_timestamp(item["lastUpdated"]),
        is_active=bool(item.get("isActive")),
    )


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return timestamp.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTC,ETH,SOL")
    parser.add_argument("--max-relative-difference", type=float, default=0.05)
    parser.add_argument("--max-timestamp-skew-seconds", type=int, default=180)
    args = parser.parse_args()

    try:
        with requests.Session() as session:
            catalogue = fetch_cmc_catalogue(session)
            diagnostics = catalogue.diagnostics
            print(
                "CMC catalogue: "
                f"reported_total={diagnostics.reported_total} "
                f"returned_rows={diagnostics.returned_rows} "
                f"unique_ids={diagnostics.unique_ids} "
                f"duplicate_ids={len(diagnostics.duplicate_ids)} "
                f"malformed_rows={diagnostics.malformed_rows} "
                f"total_count_changed={diagnostics.total_count_changed} "
                f"complete={diagnostics.is_complete}",
                file=sys.stderr,
            )
            print(
                "symbol\tcmc_id\tslug\texchange_price_usdt\tcmc_price_usd\tdifference\tstatus"
            )
            for raw_symbol in args.symbols.split(","):
                symbol = raw_symbol.strip().upper()
                if not symbol:
                    continue
                try:
                    observation = fetch_binance_spot_price(session, symbol)
                    result = classify_price_candidates(
                        candidates_for_symbol(catalogue.assets, symbol),
                        observation,
                        args.max_relative_difference,
                        timedelta(seconds=args.max_timestamp_skew_seconds),
                    )
                except (CmcProbeError, ValueError) as error:
                    print(f"{symbol}\t\t\t\t\t\tfetch_failed: {error}")
                    continue

                if result.match is None:
                    print(
                        f"{symbol}\t\t\t{observation.normalized_price:.8f}\t\t\t{result.status}"
                    )
                    continue
                match = result.match
                print(
                    f"{symbol}\t{match.asset.cmc_id}\t{match.asset.slug}\t"
                    f"{observation.normalized_price:.8f}\t{match.asset.price_usd:.8f}\t"
                    f"{match.relative_difference:.4%}\t{result.status}"
                )
    except CmcProbeError as error:
        print(f"CMC probe failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
