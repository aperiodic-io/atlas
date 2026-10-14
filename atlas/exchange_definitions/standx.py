"""StandX perpetuals (standx.com), margined and settled in the DUSD stablecoin.

Contracts come from ``GET https://perps.standx.com/api/query_symbol_info``. A
market is addressed as ``BTC-USD`` (the ``original_id``); its base asset is
the symbol and its ``quote_asset`` (DUSD, StandX's margin and settlement
token) the denominator and margin asset, so the internal ID carries the
settlement asset like the live producer's (live-producers ``standx.go``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

STANDX_SYMBOL_INFO_URL = "https://perps.standx.com/api/query_symbol_info"
STANDX_SETTLE_ASSET = "DUSD"

# The venue's own market categories. The API carries none; StandX's web app
# (standx.com/perps) tags every market Crypto, Commodities or Stocks. This is
# that table for the non-crypto markets as shipped on 2026-10-14; a market it
# does not list is crypto (StandX lists TradFi perps under that table only).
_VENUE_CATEGORIES = {
    "XAU-USD": "Commodities",
    "XAG-USD": "Commodities",
    "CL-USD": "Commodities",
    "TSLA-USD": "Stocks",
    "MU-USD": "Stocks",
    "SPCX-USD": "Stocks",
}

_CATEGORY_UNDERLYING = {
    "Crypto": UnderlyingType.crypto,
    "Commodities": UnderlyingType.commodity,
    "Stocks": UnderlyingType.equity,
}


def standx_underlying(symbol: str) -> UnderlyingType:
    return _CATEGORY_UNDERLYING[_VENUE_CATEGORIES.get(symbol, "Crypto")]


def _iso_millis(value: str) -> str:
    """``2025-07-10T05:15:32.089568Z`` as ``2025-07-10T05:15:32.089Z``."""
    t = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse_standx(exchange: str, sd: SymbolData) -> Contract:
    ctype = instrument_type(sd)
    if ctype != ContractType.perpetual:
        raise SkipSymbol(
            f"{exchange}: only perpetuals are listed, got {sd.get('type')!r}"
        )
    sid = sd["id"]
    base = sd.get("base_asset") or sid.split("-", 1)[0]
    settle = sd.get("margin_asset") or STANDX_SETTLE_ASSET
    if not base:
        raise SkipSymbol(f"{exchange}: no base asset in {sid!r}")
    return make_contract(exchange, sd, str(base), settle, settle, ctype)


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_standx_symbols(timeout_seconds: int) -> Any:
    response = requests.get(STANDX_SYMBOL_INFO_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"standx query_symbol_info returned {str(payload)[:200]}")
    return payload


def fetch_standx_perps(timeout_seconds: int) -> list[dict]:
    """Every StandX perpetual, with its listing time as ``first_capture``."""
    symbols: list[dict] = []
    for item in _fetch_standx_symbols(timeout_seconds):
        if not item or not item.get("symbol") or not item.get("base_asset"):
            continue
        sd: dict[str, Any] = {
            "id": item["symbol"],
            "type": "perpetual",
            "base_asset": item["base_asset"],
            "margin_asset": item.get("quote_asset") or STANDX_SETTLE_ASSET,
            "contract_size": 1.0,
            "underlying": standx_underlying(item["symbol"]).value,
        }
        if status := item.get("status"):
            sd["status"] = status
        if created := item.get("created_at"):
            sd["first_capture"] = _iso_millis(created)
        symbols.append(sd)
    return symbols
