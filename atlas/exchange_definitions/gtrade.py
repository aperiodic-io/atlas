"""gTrade (Gains Network) perpetuals on Arbitrum, from the Trading API.

gTrade fills every trade against its gToken vaults at an oracle-derived price.
A market is one pair (``BTC/USD``) whatever the collateral it is traded with
(DAI, WETH, USDC, GNS), and the API quotes its open interest and funding in
USD, so a contract is a pair settled in ``USD``. Discovery mirrors the
live-producers feed (``streaming/feeds/gtrade``):

* every market of ``GET https://api.gains.trade/v1/arbitrum/markets`` (the API
  lists only tradable markets: the legacy ``GOOGL_1``-style duplicates still in
  the contracts are absent);
* ``original_id`` is the API symbol (``BTC/USD``), which the REST and WebSocket
  APIs address a market by;
* the symbol is the base, with the quote appended when it is not USD
  (``USD/JPY`` -> ``USDJPY``).

Underlying comes from the market's ``group``, the venue's own classification:
it files index ETFs (SPY, QQQ) under ``indices`` and commodity ETFs (GDX, URA)
under ``commodities``. ``SPX/USD`` on gTrade is the SPX6900 token (``crypto``).
Base, Polygon and MegaETH deployments are separate chains and are not listed.
"""

from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

GTRADE_MARKETS_URL = "https://api.gains.trade/v1/arbitrum/markets"
GTRADE_SETTLE_ASSET = "USD"

_GTRADE_GROUPS = {
    "crypto": UnderlyingType.crypto,
    "forex": UnderlyingType.forex,
    "stocks": UnderlyingType.equity,
    "indices": UnderlyingType.index,
    "commodities": UnderlyingType.commodity,
}


def gtrade_underlying(group: str | None) -> UnderlyingType:
    return _GTRADE_GROUPS.get(group or "", UnderlyingType.unknown)


def parse_gtrade(exchange: str, sd: SymbolData) -> Contract:
    base, quote, settle = sd.get("base_coin"), sd.get("quote_coin"), sd.get("settle_coin")
    if not base or not quote or not settle:
        raise SkipSymbol(f"{exchange}: market {sd['id']!r} has no base/quote/settle metadata")
    symbol = base if quote == "USD" else f"{base}{quote}"
    return make_contract(exchange, sd, symbol, settle, settle, instrument_type(sd))


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_gtrade_markets(timeout_seconds: int) -> list[dict[str, Any]]:
    response = requests.get(GTRADE_MARKETS_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload.get("data"), list):
        raise ValueError("gtrade markets response without a data list")
    return payload["data"]


def gtrade_symbols(markets: list[dict[str, Any]]) -> list[dict]:
    """Markets of the Trading API as symbol rows, in API order."""
    symbols = []
    for market in markets:
        sid, base, quote = market.get("symbol"), market.get("base"), market.get("quote")
        if not sid or not base or not quote:
            continue
        symbols.append(
            {
                "id": sid,
                "type": "perpetual",
                "contract_size": 1.0,
                "base_coin": base,
                "quote_coin": quote,
                "settle_coin": GTRADE_SETTLE_ASSET,
                "margin_asset": GTRADE_SETTLE_ASSET,
                "underlying": gtrade_underlying(market.get("group")).value,
            }
        )
    return symbols


def fetch_gtrade_perps(timeout_seconds: int) -> list[dict]:
    return gtrade_symbols(_fetch_gtrade_markets(timeout_seconds))
