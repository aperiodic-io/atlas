"""Avantis perpetuals (Base; rebranded Veranta), from the venue's pair catalog.

Avantis is an oracle-priced perp venue listing crypto, FX, metals, commodities
and US and Korean equities, all settled in USDC. Discovery mirrors the
live-producers feed (``streaming/feeds/avantis``):

* every pair of ``GET https://data.avantisfi.com/v2/trading`` with
  ``isPairListed`` and a non-blank base and quote (the catalog keeps delisted
  pairs, some with empty or placeholder names);
* ``original_id`` is the pair index;
* the symbol is the base as listed with whitespace removed (``SK HYNIX`` ->
  ``SKHYNIX``), with the quote appended when it is not USD (``USD/JPY`` ->
  ``USDJPY``, ``005930/KRW`` -> ``005930KRW``). Upside pairs keep their suffix
  (``ETH_UPSIDE``): they are separate markets on the same price feed.

Underlying comes from the pair's price-feed ``assetType`` (``crypto``, ``fx``,
``equity``, ``metal``, ``commodity``), falling back to the pair group's name
(``CRYPTO1`` .. ``CRYPTO4``, ``FOREX``, ``COMMODITIES``, ``EQUITIES``).
"""

from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

AVANTIS_TRADING_URL = "https://data.avantisfi.com/v2/trading"
AVANTIS_SETTLE_ASSET = "USDC"

_AVANTIS_ASSET_TYPES = {
    "crypto": UnderlyingType.crypto,
    "fx": UnderlyingType.forex,
    "equity": UnderlyingType.equity,
    "metal": UnderlyingType.commodity,
    "commodity": UnderlyingType.commodity,
}

_AVANTIS_GROUPS = {
    "FOREX": UnderlyingType.forex,
    "COMMODITIES": UnderlyingType.commodity,
    "EQUITIES": UnderlyingType.equity,
}


def avantis_underlying(asset_type: str | None, group_name: str | None) -> UnderlyingType:
    if asset_type in _AVANTIS_ASSET_TYPES:
        return _AVANTIS_ASSET_TYPES[asset_type]
    group = (group_name or "").upper()
    if group.startswith("CRYPTO"):
        return UnderlyingType.crypto
    return _AVANTIS_GROUPS.get(group, UnderlyingType.unknown)


def parse_avantis(exchange: str, sd: SymbolData) -> Contract:
    base, quote, settle = sd.get("base_coin"), sd.get("quote_coin"), sd.get("settle_coin")
    if not base or not quote or not settle:
        raise SkipSymbol(f"{exchange}: pair {sd['id']!r} has no base/quote/settle metadata")
    symbol = base if quote == "USD" else f"{base}{quote}"
    return make_contract(exchange, sd, symbol, settle, settle, instrument_type(sd))


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_avantis_catalog(timeout_seconds: int) -> dict[str, Any]:
    response = requests.get(AVANTIS_TRADING_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if "pairInfos" not in payload:
        raise ValueError("avantis pair catalog without pairInfos")
    return payload


def avantis_symbols(catalog: dict[str, Any]) -> list[dict]:
    """Listed pairs of a /v2/trading catalog as symbol rows, in index order."""
    groups = catalog.get("groupInfo") or {}
    symbols = []
    for index, pair in (catalog.get("pairInfos") or {}).items():
        base_raw, quote = pair.get("from") or "", pair.get("to") or ""
        if not pair.get("isPairListed") or not base_raw.strip() or not quote.strip():
            continue
        attributes = (pair.get("feed") or {}).get("attributes") or {}
        group = groups.get(str(pair.get("groupIndex"))) or {}
        symbols.append(
            {
                "id": str(index),
                "type": "perpetual",
                "contract_size": 1.0,
                "base_coin": "".join(base_raw.split()),
                "quote_coin": quote,
                "settle_coin": AVANTIS_SETTLE_ASSET,
                "margin_asset": AVANTIS_SETTLE_ASSET,
                "underlying": avantis_underlying(
                    attributes.get("assetType"), group.get("name")
                ).value,
            }
        )
    symbols.sort(key=lambda sd: int(sd["id"]))
    return symbols


def fetch_avantis_perps(timeout_seconds: int) -> list[dict]:
    return avantis_symbols(_fetch_avantis_catalog(timeout_seconds))
