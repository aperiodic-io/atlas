"""Ostium perpetuals (Arbitrum), from the venue's Builder API.

Ostium is an oracle-priced perp venue listing FX, commodities, indices,
stocks, ETFs and crypto, all settled in USDC. Contract discovery mirrors the
live-producers feed (``streaming/feeds/ostium``) so that the producer's
Atlas-scheme ID equals ``internal_id`` for every live pair:

* the pairs come from the subgraph the Builder SDK reads; the query names only
  fields every indexer behind the endpoint serves (it load-balances between two
  and only one has ``isActive``/``removedAt``);
* a pair is live when its open-interest cap is non-zero (``USD/KRW`` is listed
  with a zero cap) and the live price snapshot carries its feed;
* the symbol is the base of the price feed's display name (``US500-USD``; the
  subgraph keeps the original ticker ``SPX``), with the quote appended when it
  is not USD (``USD-JPY`` -> ``USDJPY``, ``JP225-JPY`` -> ``JP225JPY``);
* ``original_id`` is the on-chain pair index.

Underlying comes from the pair's group. ``etf`` maps to ``equity``: an ETF is an
exchange-traded equity share whatever it tracks (Ostium lists URA, TLT, HYG ...
under it).
"""

from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

OSTIUM_BUILDER_URL = "https://builder.prod.bedrock.ostium.io"
OSTIUM_PAIRS_QUERY = "{ pairs(first: 1000) { id from to feed maxOI group { name } } }"
OSTIUM_SETTLE_ASSET = "USDC"

_OSTIUM_UNDERLYING = {
    "crypto": UnderlyingType.crypto,
    "forex": UnderlyingType.forex,
    "commodities": UnderlyingType.commodity,
    "indices": UnderlyingType.index,
    "stocks": UnderlyingType.equity,
    "etf": UnderlyingType.equity,
}


def ostium_underlying(group: str | None) -> UnderlyingType:
    return _OSTIUM_UNDERLYING.get(group or "", UnderlyingType.unknown)


def parse_ostium(exchange: str, sd: SymbolData) -> Contract:
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
def _fetch_ostium_pairs(timeout_seconds: int) -> list[dict[str, Any]]:
    response = requests.post(
        f"{OSTIUM_BUILDER_URL}/v1/subgraph/gn",
        json={"query": OSTIUM_PAIRS_QUERY},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise ValueError(f"ostium pairs query: {payload['errors']}")
    return payload["data"]["pairs"]


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_ostium_prices(timeout_seconds: int) -> list[dict[str, Any]]:
    response = requests.get(f"{OSTIUM_BUILDER_URL}/v1/prices", timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()["prices"]


def ostium_symbols(pairs: list[dict[str, Any]], prices: list[dict[str, Any]]) -> list[dict]:
    """Live pairs as symbol rows, joining subgraph pairs and the price snapshot."""
    names = {str(p.get("feed_id", "")).lower(): p.get("pair", "") for p in prices}
    symbols = []
    for pair in pairs:
        pair_id = pair.get("id")
        if not pair_id or pair.get("maxOI") in (None, "", "0"):
            continue
        name = names.get(str(pair.get("feed", "")).lower())
        if not name:
            continue
        base, sep, quote = name.partition("-")
        if not sep or not base or not quote:
            continue
        group = (pair.get("group") or {}).get("name")
        symbols.append(
            {
                "id": pair_id,
                "type": "perpetual",
                "contract_size": 1.0,
                "base_coin": base,
                "quote_coin": quote,
                "settle_coin": OSTIUM_SETTLE_ASSET,
                "margin_asset": OSTIUM_SETTLE_ASSET,
                "underlying": ostium_underlying(group).value,
            }
        )
    symbols.sort(key=lambda sd: int(sd["id"]))
    return symbols


def fetch_ostium_perps(timeout_seconds: int) -> list[dict]:
    return ostium_symbols(
        _fetch_ostium_pairs(timeout_seconds), _fetch_ostium_prices(timeout_seconds)
    )
