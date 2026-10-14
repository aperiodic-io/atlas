"""Nado perpetuals (Ink L2, successor of Vertex), settled in USDT0.

A market is addressed by its product id, which is the ``original_id``; the row
keeps the venue listing (``BTC-PERP``) as ``listing``. Symbols drop the ``-PERP``
suffix; a USD-quoted currency pair is its base (``EURUSD`` -> ``EUR``), any other
pair keeps both legs (``USDJPY``), as the producer does.

Nado states no asset class. Its price feeds tell crypto from traditional assets:
crypto perps use a 24/7 oracle with no weekend fallback, traditional ones (stocks,
ETFs, FX, commodities) a weekend fallback. ``nado_underlying`` uses documented
tables for the traditional ones and calls a crypto-feed perp crypto; a
weekend-feed perp missing from the tables is ``unknown`` rather than guessed.
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

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

NADO_SETTLEMENT_ASSET = "USDT0"
NADO_LIVE_STATUSES = frozenset({"live", "post_only", "reduce_only"})
_GATEWAY = "https://gateway.prod.nado.xyz"
_ARCHIVE = "https://archive.prod.nado.xyz"

NADO_COMMODITIES = frozenset(
    {"WTI", "BRENT", "XAU", "XAG", "XPT", "XPD", "NATGAS", "COPPER"}
)
NADO_EQUITIES = frozenset(
    {
        "AAPL",
        "AMD",
        "AMZN",
        "AVGO",
        "BBX",
        "CRCL",
        "DELL",
        "GOOGL",
        "HIMS",
        "INTC",
        "LLY",
        "META",
        "MRVL",
        "MSFT",
        "MSTR",
        "MU",
        "NBIS",
        "NVDA",
        "ORCL",
        "PENG",
        "QQQ",
        "SKHY",
        "SNDK",
        "SPY",
        "TSLA",
        "ZHIPU",
    }
)
# SpaceX ("SPCX", "SpaceX Perp") is a private company.
NADO_PRE_IPO = frozenset({"SPCX"})
_CURRENCIES = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "CHF",
        "AUD",
        "CAD",
        "NZD",
        "TRY",
        "MXN",
        "BRL",
        "KRW",
        "CNH",
        "INR",
        "ZAR",
    }
)


def nado_symbol(listing: str) -> str:
    """The contract symbol of a ``-PERP`` listing."""
    s = listing.removesuffix("-PERP")
    if len(s) == 6 and s.endswith("USD") and s.upper() == s:
        return s[:3]
    return s


def _is_currency_pair(listing: str) -> bool:
    s = listing.removesuffix("-PERP")
    return len(s) == 6 and s[:3] in _CURRENCIES and s[3:] in _CURRENCIES


def nado_underlying(listing: str, price_feed: dict | None) -> UnderlyingType:
    if _is_currency_pair(listing):
        return UnderlyingType.forex
    base = listing.removesuffix("-PERP").upper()
    if base in NADO_COMMODITIES:
        return UnderlyingType.commodity
    if base in NADO_PRE_IPO:
        return UnderlyingType.pre_market
    if base in NADO_EQUITIES:
        return UnderlyingType.equity
    oracle = (price_feed or {}).get("oracle") or {}
    if oracle and not oracle.get("weekend"):
        return UnderlyingType.crypto
    return UnderlyingType.unknown


def parse_nado(exchange: str, sd: SymbolData) -> Contract:
    listing = sd.get("listing")
    if not listing or not listing.endswith("-PERP"):
        raise SkipSymbol(f"nado: product {sd['id']!r} has no -PERP listing")
    settle = sd.get("margin_asset") or NADO_SETTLEMENT_ASSET
    return make_contract(
        exchange, sd, nado_symbol(listing), settle, settle, instrument_type(sd)
    )


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _get(url: str, timeout_seconds: int) -> Any:
    # The gateway refuses (403) a request that does not accept gzip.
    response = requests.get(
        url, timeout=timeout_seconds, headers={"Accept-Encoding": "gzip"}
    )
    response.raise_for_status()
    return response.json()


def fetch_nado_perps(timeout_seconds: int) -> list[dict]:
    """The tradable perpetuals (not_tradable products are delisted or not yet listed)."""
    symbols_payload = _get(f"{_GATEWAY}/v1/symbols", timeout_seconds)
    pairs_payload = _get(f"{_GATEWAY}/v2/pairs", timeout_seconds)
    feeds_payload = _get(f"{_ARCHIVE}/v2/symbols", timeout_seconds)
    if not isinstance(symbols_payload, list) or not isinstance(pairs_payload, list):
        raise ValueError("nado: symbols and pairs must be lists")
    quotes = {
        p.get("product_id"): p.get("quote")
        for p in pairs_payload
        if isinstance(p, dict)
    }
    feeds = {
        v.get("symbol"): v.get("price_feed")
        for v in (feeds_payload.values() if isinstance(feeds_payload, dict) else [])
        if isinstance(v, dict)
    }
    out = []
    for item in symbols_payload:
        if not isinstance(item, dict) or item.get("type") != "perp":
            continue
        listing = item.get("symbol") or ""
        quote = quotes.get(item.get("product_id"))
        if (
            item.get("trading_status") not in NADO_LIVE_STATUSES
            or not quote
            or not listing.endswith("-PERP")
        ):
            continue
        out.append(
            {
                "id": str(item["product_id"]),
                "type": "perpetual",
                "listing": listing,
                "contract_size": 1.0,
                "margin_asset": quote,
                "underlying": nado_underlying(listing, feeds.get(listing)).value,
            }
        )
    return sorted(out, key=lambda sd: int(sd["id"]))
