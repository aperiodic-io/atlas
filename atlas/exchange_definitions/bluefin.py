"""Bluefin Pro (bluefin.io), a Sui perpetuals DEX.

Contracts come from ``GET https://api.sui-prod.bluefin.io/v1/exchange/info``
(the official Pro SDK's ``exchange-pro-api.yaml``). A market is addressed as
``BTC-PERP`` (the ``original_id``); ``baseAssetSymbol`` (``GOLD``) is the
symbol, and USDC, the only margin asset, the denominator and margin asset,
matching the live producer's ID (live-producers ``bluefin.go``).
"""

from __future__ import annotations

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
from .pacifica import iso_millis

BLUEFIN_INFO_URL = "https://api.sui-prod.bluefin.io/v1/exchange/info"
BLUEFIN_SETTLE_ASSET = "USDC"

# Bluefin publishes no market category. Its markets are crypto except these
# RWA tickers (the venue lists gold as GOLD, "Gold"); a documented table, so a
# new TradFi listing needs adding here.
_NON_CRYPTO = {
    "GOLD": UnderlyingType.commodity,
    "SILVER": UnderlyingType.commodity,
    "XAU": UnderlyingType.commodity,
    "XAG": UnderlyingType.commodity,
}


def bluefin_underlying(base: str) -> UnderlyingType:
    return _NON_CRYPTO.get(base.upper(), UnderlyingType.crypto)


def parse_bluefin(exchange: str, sd: SymbolData) -> Contract:
    ctype = instrument_type(sd)
    if ctype != ContractType.perpetual:
        raise SkipSymbol(
            f"{exchange}: only perpetuals are listed, got {sd.get('type')!r}"
        )
    sid = sd["id"]
    base = sd.get("base_asset") or sid.removesuffix("-PERP")
    settle = sd.get("margin_asset") or BLUEFIN_SETTLE_ASSET
    if not base:
        raise SkipSymbol(f"{exchange}: no base asset in {sid!r}")
    return make_contract(exchange, sd, str(base), settle, settle, ctype)


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_bluefin_info(timeout_seconds: int) -> Any:
    response = requests.get(BLUEFIN_INFO_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise ValueError(f"bluefin exchange/info returned {str(payload)[:200]}")
    return payload


def fetch_bluefin_perps(timeout_seconds: int) -> list[dict]:
    """Every Bluefin perpetual (delisted ones included, with their status), with
    its trading start as ``first_capture``."""
    payload = _fetch_bluefin_info(timeout_seconds)
    symbols: list[dict] = []
    for item in payload["markets"]:
        if not item or not item.get("symbol"):
            continue
        base = item.get("baseAssetSymbol") or item["symbol"].removesuffix("-PERP")
        sd: dict[str, Any] = {
            "id": item["symbol"],
            "type": "perpetual",
            "base_asset": base,
            "margin_asset": BLUEFIN_SETTLE_ASSET,
            "contract_size": 1.0,
            "underlying": bluefin_underlying(base).value,
        }
        if status := item.get("status"):
            sd["status"] = status
        if start := item.get("tradingStartTimeAtMillis"):
            sd["first_capture"] = iso_millis(int(start))
        symbols.append(sd)
    return symbols
