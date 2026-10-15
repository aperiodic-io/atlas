"""Hibachi (hibachi.xyz) perpetuals.

``GET https://data-api.hibachi.xyz/market/inventory`` lists every market with its
contract (``symbol`` such as ``BTC/USDT-P``, ``underlyingSymbol``,
``settlementSymbol``, ``category`` CRYPTO or FX, ``status``) and live info
(``tags``). References: ccxt 4.5.88 ``hibachi.py`` (REST); docs.hibachi.xyz.

The live producer (live-producers ``streaming/feeds/hibachi``) streams every
market that is not ``CLOSED`` under ``symbol = underlyingSymbol`` and
``denominator = settlementSymbol`` (USDT), settled in that token, so the Atlas
ID is ``perpetual-<underlying>-USDT:USDT``. FX pairs are quoted in USD per unit
(``JPY/USDT-P`` trades around 0.0063), so the underlying alone is the symbol.

Tardis does not cover Hibachi: this API is the only source.
"""

from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

HIBACHI_INVENTORY_URL = "https://data-api.hibachi.xyz/market/inventory"


def parse_hibachi(exchange: str, sd: SymbolData) -> Contract:
    base, settle = sd.get("base_asset"), sd.get("settle_asset")
    if not base or not settle:
        raise SkipSymbol(f"{exchange}: {sd['id']!r} has no underlying/settlement asset")
    return make_contract(exchange, sd, base, settle, settle, instrument_type(sd))


def classify_hibachi_underlying(contract: dict[str, Any], tags: list[str]) -> UnderlyingType:
    """The underlying class from Hibachi's own metadata.

    ``category`` is CRYPTO or FX. Hibachi files silver (XAG) under FX but tags
    it ``commodity`` in the market info, so that tag wins; anything else is
    ``unknown`` rather than guessed.
    """
    category = contract.get("category")
    if "commodity" in tags:
        return UnderlyingType.commodity
    if category == "CRYPTO":
        return UnderlyingType.crypto
    if category == "FX":
        return UnderlyingType.forex
    return UnderlyingType.unknown


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_hibachi_inventory(timeout_seconds: int) -> Any:
    response = requests.get(HIBACHI_INVENTORY_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise ValueError("Hibachi inventory has no markets list")
    return payload


def hibachi_symbols(payload: dict[str, Any]) -> list[dict]:
    """The listed (not ``CLOSED``) perpetuals of an inventory payload."""
    symbols = []
    for market in payload.get("markets", []):
        contract = market.get("contract") or {}
        if not contract.get("symbol") or contract.get("status") in (None, "", "CLOSED"):
            continue
        tags = (market.get("info") or {}).get("tags") or []
        symbols.append(
            {
                "id": contract["symbol"],
                "type": "perpetual",
                "contract_size": 1.0,
                "base_asset": contract.get("underlyingSymbol"),
                "settle_asset": contract.get("settlementSymbol"),
                "margin_asset": contract.get("settlementSymbol"),
                "underlying": classify_hibachi_underlying(contract, tags).value,
            }
        )
    return symbols


def fetch_hibachi_perps(timeout_seconds: int) -> list[dict]:
    return hibachi_symbols(_fetch_hibachi_inventory(timeout_seconds))
