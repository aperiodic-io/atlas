"""Paradex perpetuals.

Instruments come from ``GET /v1/markets`` (perpetuals only: the endpoint also
lists thousands of options and a few spot markets). A market is addressed by
its symbol (``BTC-USD-PERP``, the ``original_id``), quoted in USD and settled in
its ``settlement_currency`` (USDC), which is the Atlas denominator and margin,
as for the live producer (live-producers ``streaming/feeds/paradex``).

Paradex tags real-world-asset markets ``RWA`` without saying which kind;
``_RWA_UNDERLYING`` names the commodities and indices among them, and every
other RWA market is an equity (single stocks and equity ETFs). Every market
without the RWA tag is crypto (tokenized gold such as PAXG included, as
Binance classifies it).
"""

from __future__ import annotations

from datetime import UTC, datetime

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

PARADEX_URL = "https://api.prod.paradex.trade/v1/markets"

_RWA_UNDERLYING = {
    # Commodities: precious and base metals, crude oil (WTI, Brent), natural gas.
    "XAU": UnderlyingType.commodity,
    "XAG": UnderlyingType.commodity,
    "XPT": UnderlyingType.commodity,
    "XPD": UnderlyingType.commodity,
    "XCU": UnderlyingType.commodity,
    "CL": UnderlyingType.commodity,
    "BZ": UnderlyingType.commodity,
    "NG": UnderlyingType.commodity,
    # Equity indices.
    "US500": UnderlyingType.index,
    "US100": UnderlyingType.index,
    "US30": UnderlyingType.index,
}


def paradex_underlying(base: str, tags: list[str] | None) -> UnderlyingType:
    if "RWA" not in (tags or []):
        return UnderlyingType.crypto
    return _RWA_UNDERLYING.get(base, UnderlyingType.equity)


def parse_paradex(exchange: str, sd: SymbolData) -> Contract:
    ctype = instrument_type(sd)
    base = sd.get("base_asset")
    if not base:
        raise SkipSymbol(f"{exchange}: market {sd['id']!r} has no base asset")
    settlement = sd.get("margin_asset") or "USDC"
    return make_contract(exchange, sd, str(base), settlement, settlement, ctype)


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_paradex_payload(timeout_seconds: int) -> dict:
    response = requests.get(PARADEX_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "results" not in payload:
        raise ValueError("paradex markets: unexpected payload")
    return payload


def paradex_symbols(payload: dict) -> list[dict]:
    symbols = []
    for item in payload.get("results") or []:
        if item.get("asset_kind") != "PERP" or not item.get("symbol"):
            continue
        base = item.get("base_currency") or ""
        sd: dict = {
            "id": item["symbol"],
            "type": "perpetual",
            "base_asset": base,
            "margin_asset": item.get("settlement_currency") or "USDC",
            "contract_size": 1.0,
            "underlying": paradex_underlying(base, item.get("tags")).value,
        }
        open_at = item.get("open_at")
        if isinstance(open_at, int) and open_at > 0:
            sd["listing_date"] = (
                datetime.fromtimestamp(open_at / 1000, tz=UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        symbols.append(sd)
    return symbols


def fetch_paradex_perps(timeout_seconds: int) -> list[dict]:
    return paradex_symbols(_fetch_paradex_payload(timeout_seconds))
