"""Extended (Starknet) perpetuals.

Instruments come from ``GET /api/v1/info/markets``. A market is addressed by
its name (``BTC-USD``, ``TSLA_24_5-USD``, the ``original_id``), and Extended
reports the asset (``assetName``: ``BTC``, ``EUR``, ``USDJPY``, ``SPX500m``) and
the collateral it settles in (``collateralAssetName``: ``USD``, which ccxt also
uses as the settle currency). The asset is the Atlas symbol and the collateral
its denominator and margin, as for the live producer (live-producers
``streaming/feeds/extended``).

Extended classifies every market (``category`` / ``subCategory``), which gives
the underlying: crypto categories are crypto (tokenized gold such as PAXG and
XAUT included), RWA equities are equities, ``Commodity`` commodities, ``FX``
forex and ``Pre-market`` pre_market. ``ETF/Index`` mixes both: a market whose
description names an ETF (or a leveraged "Bull"/"Bear" fund) is an equity, the
others (S&P 500, Nasdaq-100, Nikkei 225, KOSPI 200) indices.
"""

from __future__ import annotations

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
from .common import (
    SkipSymbol,
    delisted_end_date,
    instrument_type,
    make_contract,
    ms_to_utc_timestamp,
)

EXTENDED_URL = "https://api.starknet.extended.exchange/api/v1/info/markets"

_RWA_SUBCATEGORY = {
    "Equity": UnderlyingType.equity,
    "Commodity": UnderlyingType.commodity,
    "FX": UnderlyingType.forex,
    "Pre-market": UnderlyingType.pre_market,
}

_CRYPTO_CATEGORIES = {"Crypto", "L1", "L2", "Infra"}


def extended_underlying(
    category: str | None, sub_category: str | None, description: str | None
) -> UnderlyingType:
    if category in _CRYPTO_CATEGORIES:
        return UnderlyingType.crypto
    if category != "RWA":
        return UnderlyingType.unknown
    if sub_category == "ETF/Index":
        words = (description or "").split()
        if "ETF" in words or "Bull" in words or "Bear" in words:
            return UnderlyingType.equity
        return UnderlyingType.index
    return _RWA_SUBCATEGORY.get(sub_category or "", UnderlyingType.unknown)


def parse_extended(exchange: str, sd: SymbolData) -> Contract:
    ctype = instrument_type(sd)
    base = sd.get("base_asset")
    collateral = sd.get("margin_asset")
    if not base or not collateral:
        raise SkipSymbol(f"{exchange}: market {sd['id']!r} has no asset or collateral")
    return make_contract(
        exchange, sd, str(base), str(collateral), str(collateral), ctype
    )


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_extended_payload(timeout_seconds: int) -> dict:
    # Extended rejects requests without a User-Agent (403); requests sends one.
    response = requests.get(EXTENDED_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        raise ValueError("extended markets: unexpected payload")
    return payload


def extended_symbols(payload: dict) -> list[dict]:
    """Every perpetual, delisted and prelisted ones included.

    ``createdAt`` is the listing time. A ``DELISTED`` market ends at the first
    run that sees it (Extended publishes no delisting time); ``PRELISTED`` and
    ``REDUCE_ONLY`` markets are still listed.
    """
    symbols = []
    for item in payload.get("data") or []:
        if item.get("type") != "PERPETUAL" or not item.get("name"):
            continue
        sd: dict = {
            "id": item["name"],
            "type": "perpetual",
            "base_asset": item.get("assetName") or "",
            "margin_asset": item.get("collateralAssetName") or "",
            "contract_size": 1.0,
            "underlying": extended_underlying(
                item.get("category"), item.get("subCategory"), item.get("description")
            ).value,
        }
        if listed := ms_to_utc_timestamp(item.get("createdAt")):
            sd["first_capture"] = listed
        if item.get("status") == "DELISTED":
            sd["end_date"] = delisted_end_date()
        symbols.append(sd)
    return symbols


def fetch_extended_perps(timeout_seconds: int) -> list[dict]:
    return extended_symbols(_fetch_extended_payload(timeout_seconds))
