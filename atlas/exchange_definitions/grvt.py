"""GRVT perpetuals (USDT-settled crypto, equity, ETF and commodity perps).

The instrument list states each perp's ``asset_class`` (CRYPTO, EQUITY,
COMMODITY), which gives ``underlying``; perps on companies that are not listed
(the venue labels them EQUITY) are ``pre_market``. ``create_time`` is the listing
time and becomes ``first_capture``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

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
from .common import SkipSymbol, delisted_end_date, instrument_type, make_contract

GRVT_ASSET_CLASSES = {
    "CRYPTO": UnderlyingType.crypto,
    "EQUITY": UnderlyingType.equity,
    "COMMODITY": UnderlyingType.commodity,
    "INDEX": UnderlyingType.index,
    "FX": UnderlyingType.forex,
    "FOREX": UnderlyingType.forex,
}
# Private companies GRVT lists as EQUITY (pre-IPO perps).
GRVT_PRE_IPO = frozenset({"ANTHROPIC", "OPENAI", "SPACEX"})


def grvt_underlying(base: str, asset_class: str | None) -> UnderlyingType:
    if base.upper() in GRVT_PRE_IPO:
        return UnderlyingType.pre_market
    return GRVT_ASSET_CLASSES.get((asset_class or "").upper(), UnderlyingType.unknown)


def parse_grvt(exchange: str, sd: SymbolData) -> Contract:
    """``BTC_USDT_Perp`` -> ``perpetual-BTC-USDT:USDT``: GRVT perps settle in their quote."""
    base, quote = sd.get("base"), sd.get("quote")
    if not base or not quote:
        parts = sd["id"].split("_")
        if len(parts) != 3 or parts[2] != "Perp":
            raise SkipSymbol(f"grvt: instrument {sd['id']!r} is not BASE_QUOTE_Perp")
        base, quote = parts[0], parts[1]
    settle = sd.get("margin_asset") or quote
    return make_contract(exchange, sd, base, quote, settle, instrument_type(sd))


def _ns_to_iso(value: Any) -> str | None:
    try:
        ns = int(value)
    except (TypeError, ValueError):
        return None
    if ns <= 0:
        return None
    listed = datetime.fromtimestamp(ns // 1_000_000_000, UTC) + timedelta(
        microseconds=ns % 1_000_000_000 // 1000
    )
    return listed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_grvt_instruments(timeout_seconds: int) -> list[dict[str, Any]]:
    response = requests.post(
        "https://market-data.grvt.io/full/v1/all_instruments",
        # `is_active: true` would filter the delisted instruments out.
        json={"is_active": False},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
        raise ValueError("grvt all_instruments: no result list")
    return payload["result"]


def fetch_grvt_perps(timeout_seconds: int) -> list[dict]:
    """Every perpetual; a ``DELISTED`` one ends at the first run that sees it.

    GRVT publishes no delisting time. ``create_time`` is the listing time.
    """
    symbols = []
    for item in _fetch_grvt_instruments(timeout_seconds):
        if item.get("kind") != "PERPETUAL":
            continue
        base, quote = item.get("base"), item.get("quote")
        if not item.get("instrument") or not base or not quote:
            continue
        sd = {
            "id": item["instrument"],
            "type": "perpetual",
            "base": base,
            "quote": quote,
            "contract_size": 1.0,
            "margin_asset": quote,
            "underlying": grvt_underlying(base, item.get("asset_class")).value,
        }
        if listed := _ns_to_iso(item.get("create_time")):
            sd["first_capture"] = listed
        if item.get("status") == "DELISTED":
            sd["end_date"] = delisted_end_date()
        symbols.append(sd)
    return symbols
