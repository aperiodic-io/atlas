"""dYdX v4 perpetuals, read from the public indexer.

Every market is quoted in USD and settled in USDC (ccxt: ``BTC/USDC:USDC``), so a
contract's denominator and margin are USDC. The indexer carries no asset-class
metadata; ``dydx_underlying`` is a small documented table (commodities, currencies,
prediction markets), everything else being a crypto asset. Tokenised assets (PAXG,
XAUT, TSLAX) are crypto tokens, as Binance classifies PAXG and XAUT.
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

DYDX_SETTLEMENT_ASSET = "USDC"

# Bases priced off a commodity, an ISO currency (FX pairs), or a prediction market.
DYDX_COMMODITIES = frozenset(
    {"WTI", "BRENT", "XAU", "XAG", "XPT", "XPD", "NATGAS", "COPPER"}
)
DYDX_CURRENCIES = frozenset(
    {
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
DYDX_PREDICTION_MARKETS = frozenset({"TRUMPWIN", "DODGERSWIN"})


def dydx_underlying(base: str) -> UnderlyingType:
    base = base.upper()
    if base in DYDX_COMMODITIES:
        return UnderlyingType.commodity
    if base in DYDX_CURRENCIES:
        return UnderlyingType.forex
    if base in DYDX_PREDICTION_MARKETS:
        return UnderlyingType.unknown
    return UnderlyingType.crypto


def _split_ticker(ticker: str) -> tuple[str, str]:
    base, sep, quote = ticker.rpartition("-")
    if not sep or not base or not quote:
        raise SkipSymbol(f"dydx: ticker {ticker!r} is not BASE-QUOTE")
    return base, quote


def parse_dydx(exchange: str, sd: SymbolData) -> Contract:
    """``BTC-USD`` -> ``perpetual-BTC-USDC:USDC``; a non-USD quote is folded into the
    symbol (``EUR-GBP`` -> ``EURGBP``), as the producer does."""
    base, quote = _split_ticker(sd["id"])
    symbol = base if quote == "USD" else base + quote
    settle = sd.get("margin_asset") or DYDX_SETTLEMENT_ASSET
    return make_contract(exchange, sd, symbol, settle, settle, instrument_type(sd))


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_dydx_markets(timeout_seconds: int) -> dict[str, Any]:
    response = requests.get(
        "https://indexer.dydx.trade/v4/perpetualMarkets", timeout=timeout_seconds
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), dict):
        raise ValueError("dydx perpetualMarkets: no markets object")
    return payload["markets"]


def fetch_dydx_perps(timeout_seconds: int) -> list[dict]:
    """Every market the indexer lists, delisted (FINAL_SETTLEMENT) ones included: the
    daily hybrid update dates them from Tardis (``dydx-v4``)."""
    symbols = []
    for key, market in sorted(_fetch_dydx_markets(timeout_seconds).items()):
        if not isinstance(market, dict):
            continue
        ticker = market.get("ticker") or key
        try:
            base, _ = _split_ticker(ticker)
        except SkipSymbol:
            continue
        symbols.append(
            {
                "id": ticker,
                "type": "perpetual",
                "contract_size": 1.0,
                "margin_asset": DYDX_SETTLEMENT_ASSET,
                "underlying": dydx_underlying(base).value,
            }
        )
    return symbols
