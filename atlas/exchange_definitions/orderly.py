"""Orderly Network perpetuals (``orderly-perpetual``).

Orderly is one shared order book behind WOOFi Pro and the other Orderly brokers.
``GET https://api.orderly.org/v1/public/info`` lists its markets as
``PERP_<BASE>_<QUOTE>``; a market a broker launched for its own users ("Perp
Anything", e.g. the mythos stock perps) carries the broker id as a fourth part,
``PERP_AAPL_USDC_mythos``. The broker is kept as a symbol prefix, as Hyperliquid's
HIP-3 dexes are (``MYTHOS:AAPL``), so a broker market never shares an ID with a
main-book market of the same base. The quote (USDC) is the settlement asset, so
``perpetual-BTC-USDC:USDC`` -- the same ID the live-producers ``orderly-perpetual``
feed publishes under its Atlas scheme.

Orderly publishes no asset-class metadata, so ``underlying`` comes from the
explicit tables below (checked against the venue's market list and index prices
of 2026-10-14). A market in none of them defaults to crypto on the main book and
to unknown on a broker book, where the listings are mostly real-world assets.
References: ccxt ``woofipro.py``; orderly.network/docs.
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

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

ORDERLY_INFO_URL = "https://api.orderly.org/v1/public/info"

_FOREX = {"EURUSD", "USDJPY"}
_COMMODITY = {"XAU", "XAG", "CL", "BZ", "COPPER", "NATGAS"}
_INDEX = {
    "NAS100",  # Nasdaq 100
    "SPX500",  # S&P 500
    "BVIV",  # Volmex Bitcoin implied-volatility index
}
_EQUITY = {
    # single stocks
    "AAOI",
    "AAPL",
    "AMD",
    "AMZN",
    "AVGO",
    "BABA",
    "CBRS",
    "COIN",
    "CRCL",
    "DELL",
    "GLW",
    "GOOGL",
    "HOOD",
    "INTC",
    "LITE",
    "META",
    "MRVL",
    "MSFT",
    "MSTR",
    "MU",
    "NBIS",
    "NVDA",
    "ORCL",
    "PLTR",
    "SAMSUNG",
    "SKHY",
    "SKHYNIX",
    "SNDK",
    "TSLA",
    "TSM",
    "WDC",
    # ETFs (traded as equities)
    "DRAM",
    "EWY",
    "QQQ",
    "SNXX",
    "SOXL",
    "SOXS",
    "SPY",
    "SQQQ",
    "TQQQ",
}
_PRE_MARKET = {"SPCX"}  # SpaceX before its listing
# Crypto tokens listed on broker books (the main book defaults to crypto).
# Tokenized gold (PAXG, XAUT) is a crypto asset, as Binance classifies PAXG.
_BROKER_CRYPTO = {
    "ALLO",
    "BCH",
    "CAP",
    "DEXE",
    "DODOX",
    "EIGEN",
    "FIL",
    "GRAM",
    "JTO",
    "KAITO",
    "MARSCOIN",
    "MET",
    "OPN",
    "PAXG",
    "PONS",
    "PYTH",
    "RE",
    "SKY",
    "SYN",
    "XAUT",
    "XLM",
}


def _split(sid: str) -> tuple[str, str, str | None] | None:
    parts = sid.split("_")
    if len(parts) not in (3, 4) or parts[0] != "PERP" or not parts[1] or not parts[2]:
        return None
    broker = parts[3] if len(parts) == 4 else None
    if broker == "":
        return None
    return parts[1], parts[2], broker


def orderly_underlying(sid: str, is_pretge: bool = False) -> UnderlyingType:
    """Asset class of one Orderly market (see the module docstring)."""
    split = _split(sid)
    if split is None:
        return UnderlyingType.unknown
    base, _, broker = split
    if is_pretge:
        return UnderlyingType.pre_market
    if base in _FOREX:
        return UnderlyingType.forex
    if base in _COMMODITY:
        return UnderlyingType.commodity
    if base in _INDEX:
        return UnderlyingType.index
    if base in _EQUITY:
        return UnderlyingType.equity
    if base in _PRE_MARKET:
        return UnderlyingType.pre_market
    if broker is None or base in _BROKER_CRYPTO:
        return UnderlyingType.crypto
    return UnderlyingType.unknown


def parse_orderly(exchange: str, sd: SymbolData) -> Contract:
    split = _split(sd["id"])
    if split is None:
        raise SkipSymbol(f"{exchange}: not an Orderly perpetual: {sd['id']!r}")
    base, quote, broker = split
    symbol = f"{broker}:{base}" if broker else base
    settle = sd.get("margin_asset") or quote
    return make_contract(exchange, sd, symbol, quote, settle, instrument_type(sd))


def _iso_ms(ms: Any) -> str | None:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_info(timeout_seconds: int) -> list[dict[str, Any]]:
    response = requests.get(ORDERLY_INFO_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("success"):
        raise ValueError(f"orderly /v1/public/info failed: {str(payload)[:200]}")
    return payload["data"]["rows"]


def fetch_orderly_perps(timeout_seconds: int) -> list[dict]:
    """Every market /v1/public/info lists, with settle asset, asset class and listing date."""
    symbols = []
    for row in _fetch_info(timeout_seconds):
        sid = row.get("symbol")
        split = _split(sid) if isinstance(sid, str) else None
        if split is None:
            continue
        sd: dict[str, Any] = {
            "id": sid,
            "type": "perpetual",
            "contract_size": 1.0,
            "margin_asset": split[1],
            "underlying": orderly_underlying(sid, bool(row.get("is_pretge"))).value,
        }
        if listed := _iso_ms(row.get("created_time")):
            sd["first_capture"] = listed
        symbols.append(sd)
    return symbols
