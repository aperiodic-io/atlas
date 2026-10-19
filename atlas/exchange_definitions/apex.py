"""ApeX Omni (omni.apex.exchange) perpetuals.

``GET https://omni.apex.exchange/api/v3/symbols`` lists the crypto perpetuals
under ``data.contractConfig.perpetualContract`` and the stock, commodity and
index perpetuals under ``.stockContract`` (each with ``crossSymbolName`` such as
``BTCUSDT``, ``baseTokenId``, ``settleAssetId``, ``category``, ``enableTrade``,
``enableDisplay``); ``.predictionContract`` holds 0-1 event contracts, which are
not perpetuals. References: ccxt 4.5.88 ``apex.py`` (which reads only the
perpetual list); the ApeX Omni API docs.

The live producer (live-producers ``streaming/feeds/apex``) streams every
listed (tradeable and shown) contract of both lists under
``symbol = baseTokenId`` and ``denominator = settleAssetId`` (USDT), settled in
that token, so the Atlas ID is ``perpetual-<base>-USDT:USDT``.

Tardis does not cover ApeX Omni: this API is the only source.
"""

from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

APEX_SYMBOLS_URL = "https://omni.apex.exchange/api/v3/symbols"

_STOCK_CATEGORY_MAP = {
    "STOCK": UnderlyingType.equity,
    "COMMODITY": UnderlyingType.commodity,
    "INDEX": UnderlyingType.index,
}


def parse_apex(exchange: str, sd: SymbolData) -> Contract:
    base, settle = sd.get("base_asset"), sd.get("settle_asset")
    if not base or not settle:
        raise SkipSymbol(f"{exchange}: {sd['id']!r} has no base/settle asset")
    return make_contract(exchange, sd, base, settle, settle, instrument_type(sd))


def classify_apex_underlying(contract: dict[str, Any], *, stock_list: bool) -> UnderlyingType:
    """The underlying class from ApeX's own lists and categories.

    A contract of the perpetual list is crypto (its categories are L1, DEFI,
    MEME, ...). The stock list mixes equities, commodities and indices, told
    apart by ``category`` (STOCK, COMMODITY, INDEX); a stock-list contract
    without one is ``unknown`` rather than guessed.
    """
    if not stock_list:
        return UnderlyingType.crypto
    return _STOCK_CATEGORY_MAP.get(contract.get("category") or "", UnderlyingType.unknown)


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_apex_symbols(timeout_seconds: int) -> Any:
    response = requests.get(APEX_SYMBOLS_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    config = ((payload or {}).get("data") or {}).get("contractConfig")
    if not isinstance(config, dict):
        raise ValueError("ApeX symbols has no contractConfig")
    return payload


def apex_symbols(payload: dict[str, Any]) -> list[dict]:
    """The perpetual and stock contracts that are tradeable or shown.

    ApeX keeps retired contracts in these lists with both flags off and has no
    status that tells a retirement from a pause, nor any listing or delisting
    time. Leaving retired contracts out lets the updater's two-run
    confirmation and mass-disappearance guard decide when one has delisted.
    """
    config = payload["data"]["contractConfig"]
    symbols = []
    for list_name, stock_list in (("perpetualContract", False), ("stockContract", True)):
        for contract in config.get(list_name) or []:
            if not (contract.get("enableTrade") or contract.get("enableDisplay")):
                continue
            if not contract.get("crossSymbolName"):
                continue
            symbols.append(
                {
                    "id": contract["crossSymbolName"],
                    "type": "perpetual",
                    "contract_size": 1.0,
                    "base_asset": contract.get("baseTokenId"),
                    "settle_asset": contract.get("settleAssetId"),
                    "margin_asset": contract.get("settleAssetId"),
                    "underlying": classify_apex_underlying(contract, stock_list=stock_list).value,
                }
            )
    return symbols


def fetch_apex_perps(timeout_seconds: int) -> list[dict]:
    return apex_symbols(_fetch_apex_symbols(timeout_seconds))
