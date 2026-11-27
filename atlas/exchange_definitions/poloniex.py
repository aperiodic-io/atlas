from __future__ import annotations

import time
from datetime import UTC, datetime

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import (
    upcoming,
    SkipSymbol,
    delisted_end_date,
    instrument_type,
    make_contract,
    ms_to_utc_timestamp,
    parse_utc_timestamp,
)
from .fields import parse_recorded_legs
from .http import get_json

# --- Exchange API (poloniex-spot) ----------------------------------------------
#
# Poloniex's v1 API named markets `QUOTE_BASE` (`USDT_BTC`, `BTC_ETH`); its
# current API, live since 2022-08-02, names them `BASE_QUOTE` (`BTC_USDT`,
# `ETH_BTC`). The history on file holds both: every `QUOTE_BASE` id stopped on 2022-08-02
# and every `BASE_QUOTE` id started then. The two can't be told apart from the
# id alone when both legs are quote assets (`BTC_ETH` is ETH/BTC, `BTC_BNB`
# is BTC/BNB), so a row's end date decides.
_MARKETS_URL = "https://api.poloniex.com/markets"
_BASE_QUOTE_SINCE = datetime(2022, 8, 2, tzinfo=UTC)
_BASE_QUOTE_SINCE_MS = int(_BASE_QUOTE_SINCE.timestamp() * 1000)

# Quote assets of v1 `QUOTE_BASE` markets, for rows that carry no dates.
_V1_QUOTES = {"USDT", "BTC", "ETH", "TRX", "BNB", "USDC", "USDD", "TUSD", "USDJ", "BUSD", "DAI", "PAX"}

# `state` is `NORMAL`, `PAUSE` (halted) or `POST_ONLY`, all listed: Poloniex
# drops delisted markets from `/markets`. `OFFLINE`, the state its
# `/currencies` uses for delisted assets, ends a market at first sighting
# should it ever appear. A market whose `tradableStartTime` is still ahead has
# not started trading (some carry placeholder times in 2099): it is `upcoming`.
_POLONIEX_DELISTED = "OFFLINE"


def _ended(sd: SymbolData) -> datetime | None:
    raw = sd.get("end_date")
    if not isinstance(raw, str):
        return None
    try:
        return parse_utc_timestamp(raw)
    except ValueError:
        return None


def _is_quote_base(sd: SymbolData, first: str, second: str) -> bool:
    ended = _ended(sd)
    if ended is not None:
        return ended <= _BASE_QUOTE_SINCE
    return first in _V1_QUOTES and second not in _V1_QUOTES


def parse_poloniex_api(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; history rows are `BASE_QUOTE`, or v1 `QUOTE_BASE`."""
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    parts = sd["id"].split("_")
    if len(parts) != 2 or not all(parts):
        raise SkipSymbol(f"{exchange}: expected 2 underscore parts in {sd['id']!r}")
    first, second = (part.upper() for part in parts)
    base, quote = (second, first) if _is_quote_base(sd, first, second) else (first, second)
    return make_contract(exchange, sd, base, quote, None, instrument_type(sd))


def _poloniex_symbol(item: dict) -> dict:
    tradable_since = item.get("tradableStartTime")
    sd = {
        "id": item["symbol"],
        "type": "spot",
        "base_coin": item["baseCurrencyName"],
        "quote_coin": item["quoteCurrencyName"],
        # Poloniex classifies nothing and lists only crypto assets.
        "underlying": UnderlyingType.crypto.value,
        # Markets carried over from the v1 API all report their migration
        # (2022-07-28), not their listing, so only later times are used.
        "first_capture": ms_to_utc_timestamp(tradable_since)
        if isinstance(tradable_since, int) and tradable_since >= _BASE_QUOTE_SINCE_MS
        else None,
    }
    if item.get("state") == _POLONIEX_DELISTED:
        sd["end_date"] = delisted_end_date()
    return {k: v for k, v in sd.items() if v is not None}


def _is_trading(item: dict, now_ms: int) -> bool:
    tradable_since = item.get("tradableStartTime")
    return not (isinstance(tradable_since, int) and tradable_since > now_ms)


def fetch_poloniex_spot(timeout_seconds: int) -> list[dict]:
    payload = get_json(_MARKETS_URL, timeout_seconds)
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) and item.get("symbol") for item in payload
    ):
        raise ValueError(f"Poloniex markets: expected a list of markets, got {payload!r:.200}")
    now_ms = int(time.time() * 1000)
    return [
        _poloniex_symbol(item) if _is_trading(item, now_ms) else upcoming(_poloniex_symbol(item))
        for item in payload
    ]
