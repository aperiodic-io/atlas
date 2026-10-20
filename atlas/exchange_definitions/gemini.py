from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import (
    SkipSymbol,
    delisted_end_date,
    instrument_type,
    make_contract,
    parse_concat,
)
from .fields import parse_recorded_legs
from .http import get_json


def parse_gemini(exchange: str, sd: SymbolData) -> Contract:
    quotes = ["USDT", "GUSD", "USDC", "USD", "BTC", "ETH", "DAI"]
    return parse_concat(exchange, sd, quotes)


# --- Exchange API (gemini-spot, gemini-perps) ---------------------------------
#
# `/v1/symbols` lists every market as a lowercase ticker (`btcusd`,
# `btcgusdperp`); base, quote, product type and status come only from
# `/v1/symbols/details/<symbol>`, one request per market (there is no bulk
# details endpoint: `/v1/symbols/details/ALL` is a 400 `InvalidSymbol`). Tardis
# ids are the details' uppercase `symbol` (`BTCUSD`, `BTCGUSDPERP`).
_SYMBOLS_URL = "https://api.gemini.com/v1/symbols"
_DETAILS_URL = "https://api.gemini.com/v1/symbols/details/{}"

# Gemini allows 120 public requests a minute. Every request (the symbol list
# included) starts at most every 0.6 seconds, 100 a minute, through one
# process-wide limiter: `gemini-spot` and `gemini-perps` run back to back and
# share the budget, and the headroom absorbs `get_json` retries, which are not
# spaced. A few run at once to hide latency: the ~340 spot markets take about
# three and a half minutes, the perpetuals a few seconds.
_REQUEST_INTERVAL_SECONDS = 0.6
_MAX_WORKERS = 4

# `status`: `open` trades; `cancel_only`, `post_only` and `limit_only` are
# temporary restrictions and stay listed; `closed` is a market Gemini has shut
# (delisted pairs such as GRAMSGD, retired wrap markets such as GUSDUSD), which
# ends at first sighting as Gemini publishes no delisting time.
_GEMINI_CLOSED = "closed"

# Gemini publishes no underlying classification. Its markets are crypto
# except for fiat currencies quoted against the dollar (EURUSD, AUDUSD), which
# are FX. Gold tokens (PAXG, XAUT) are crypto tokens on Gemini, which does not
# say otherwise.
_GEMINI_FIAT = {"USD", "EUR", "GBP", "SGD", "AUD", "CAD", "CHF", "HKD", "JPY"}

# Quote assets for Tardis-only spot ids, longest and most specific first so
# `BTCRLUSD` is BTC/RLUSD and `BTCGUSD` BTC/GUSD, not BTCRL/USD or BTCG/USD.
_SPOT_QUOTES = [
    "RLUSD",
    "GUSD",
    "USDC",
    "USDT",
    "USD",
    "DAI",
    "BTC",
    "ETH",
    "BCH",
    "LTC",
    "SOL",
    "FIL",
    "EUR",
    "GBP",
    "SGD",
]
# Perpetuals are `<BASE><SETTLE>PERP`, settled in GUSD or USDC.
_PERP_QUOTES = ["GUSD", "USDC"]
# Gemini assets ending in `G`, whose dollar markets (`PAXGUSD`) read as a
# GUSD market (PAX/GUSD) otherwise. Every such asset in Tardis's Gemini
# history; API rows carry their own legs.
_G_BASES = {"PAXG", "USDG", "MOODENG", "WCFG"}


def _split_gemini(sid: str, quotes: list[str]) -> tuple[str, str] | None:
    """Split a Gemini ticker against `quotes`, most specific first."""
    if sid.endswith("USD") and sid[:-3] in _G_BASES:
        return sid[:-3], "USD"
    for quote in quotes:
        base = sid[: -len(quote)]
        # Gemini has no `USD` base: `USDGUSD` is USDG/USD, never USD/GUSD.
        if sid.endswith(quote) and base and base != "USD":
            return base, quote
    return None


def parse_gemini_api(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; Tardis-only rows are `BTCUSD` spot or `BTCGUSDPERP` perps."""
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    sid = str(sd["id"]).upper()
    ctype = instrument_type(sd)
    if ctype == ContractType.perpetual:
        pair = _split_gemini(sid[:-4], _PERP_QUOTES) if sid.endswith("PERP") else None
        if pair is None:
            raise SkipSymbol(f"{exchange}: cannot split perpetual {sd['id']!r}")
        base, quote = pair
        return make_contract(exchange, sd, base, quote, quote, ctype)
    if ctype != ContractType.spot:
        raise SkipSymbol(f"{exchange}: unsupported type {sd.get('type')!r} for {sd['id']!r}")
    pair = _split_gemini(sid, _SPOT_QUOTES)
    if pair is None:
        raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r} against known quotes")
    return make_contract(exchange, sd, pair[0], pair[1], None, ctype)


class _RateLimiter:
    """Space request starts at least `interval` seconds apart across threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, interval: float) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + interval
        if start > now:
            time.sleep(start - now)


_LIMITER = _RateLimiter()


def _get(url: str, timeout_seconds: int) -> object:
    _LIMITER.wait(_REQUEST_INTERVAL_SECONDS)
    return get_json(url, timeout_seconds)


def _fetch_symbols(timeout_seconds: int) -> list[str]:
    payload = _get(_SYMBOLS_URL, timeout_seconds)
    if not isinstance(payload, list) or not all(isinstance(s, str) for s in payload):
        raise ValueError(f"Gemini symbols: expected a list of tickers, got {payload!r:.200}")
    return payload


def _fetch_details(symbols: list[str], timeout_seconds: int) -> list[dict]:
    def details(symbol: str) -> dict:
        payload = _get(_DETAILS_URL.format(symbol), timeout_seconds)
        if not isinstance(payload, dict) or payload.get("result") == "error" or not all(
            payload.get(key) for key in ("symbol", "base_currency", "quote_currency")
        ):
            raise ValueError(f"Gemini details for {symbol!r}: unexpected payload {payload!r:.200}")
        return payload

    # `map` cancels the queued requests once one fails.
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        return list(pool.map(details, symbols))


def _underlying(base: str) -> UnderlyingType:
    return UnderlyingType.forex if base.upper() in _GEMINI_FIAT else UnderlyingType.crypto


def _gemini_symbol(item: dict, kind: str) -> dict:
    base, quote = item["base_currency"], item["quote_currency"]
    sd: dict = {
        "id": str(item["symbol"]).upper(),
        "type": kind,
        "base_coin": base,
        "quote_coin": quote,
        "underlying": _underlying(base).value,
    }
    if kind == "perpetual":
        # Linear perpetuals settled in their quote stablecoin, sized in base
        # units (`min_order_size` 0.0001 BTC on BTCGUSDPERP). Gemini's
        # `contract_price_currency` is GUSD on some USDC perps, so the settle
        # asset is read from the quote (`quote_currency`), not from it.
        sd.update(margin_asset=quote, contract_size=1.0, quantity_unit="base")
    if item.get("status") == _GEMINI_CLOSED:
        sd["end_date"] = delisted_end_date()
    return {k: v for k, v in sd.items() if v is not None}


def _fetch_gemini(kind: str, timeout_seconds: int) -> list[dict]:
    perps = kind == "perpetual"
    symbols = [s for s in _fetch_symbols(timeout_seconds) if s.lower().endswith("perp") == perps]
    product_type = "swap" if perps else "spot"
    return [
        _gemini_symbol(item, kind)
        for item in _fetch_details(symbols, timeout_seconds)
        # Any other product a ticker turns out to be is not this slug's.
        if item.get("product_type") == product_type
    ]


def fetch_gemini_spot(timeout_seconds: int) -> list[dict]:
    return _fetch_gemini("spot", timeout_seconds)


def fetch_gemini_perpetual(timeout_seconds: int) -> list[dict]:
    return _fetch_gemini("perpetual", timeout_seconds)
