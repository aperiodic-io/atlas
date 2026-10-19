"""Variational Omni (`variational-perps`).

Variational is an RFQ perpetuals venue; its only public market data is the read-only
`GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats`
(docs.variational.io, technical-documentation/api.md), the same source the
live-producers feed discovers its contracts from.

Every listing with a funding interval and a quote is a USDC-settled linear perpetual,
named by its ticker. The "Swap on ..." listings (no funding interval, no quotes) are
total-return swaps, a different instrument (omni/trading/swaps.md), and are skipped.

The venue publishes no asset class: the web app's asset classes come from an endpoint
behind a Cloudflare browser challenge. `underlying` is therefore derived by
`classify_underlying`, a small documented rule set (see its docstring).
"""

from __future__ import annotations

import re
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import instrument_type, make_contract

STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"

# Every Variational perpetual is margined and settled in USDC.
SETTLE_ASSET = "USDC"

# Commodity perpetuals (omni/trading/tradfi-perpetuals.md "Index Prices": metals spot,
# WTI/Brent crude, Henry Hub natural gas and COMEX copper futures).
COMMODITY_TICKERS = frozenset({"XAU", "XAG", "XPT", "XPD", "CL", "BZ", "NATGAS", "COPPER"})

# Pre-IPO perpetuals (omni/trading/pre-ipo-perpetuals.md). Their funding is fixed at
# 0.005 % per 8 h, i.e. an annualized funding_rate of 0.05475, which is how the list was
# found; once a company lists, its contract becomes a regular equity perpetual.
PRE_IPO_TICKERS = frozenset({"OPENAI", "ANTHROPIC"})
PRE_IPO_ANNUAL_FUNDING = 0.05475

# Legal-entity and fund suffixes in the listing names of stocks and ETFs ("Apple Inc.",
# "Nokia Oyj", "Novo Nordisk A/S", "iShares Russell 2000 ETF", "Invesco QQQ Trust,
# Series 1"). Crypto listings are named after the token or protocol ("Bitcoin",
# "Trust Wallet", "Giggle Fund", "SynFutures"), so the words are matched with their
# corporate spelling only.
_EQUITY_NAME = re.compile(
    r"\b(Inc|Corp|Co|Ltd|plc|N\.V|S\.A|SE|AG|Oyj|ASA|AB)\b\.?"
    r"|\bA/S\b"
    r"|\b(Incorporated|Corporation|Company|Limited|Holdings?|Group|ETF|Common Stock"
    r"|Depositary|Series \d+)\b"
)


def classify_underlying(ticker: str, name: str, annual_funding: str | None = None) -> str:
    """Asset class of a Variational perpetual.

    In order: the documented commodity tickers; pre-IPO listings (documented tickers, or the
    fixed pre-IPO funding rate); stocks and ETFs by a corporate or fund suffix in the name
    (ETFs, including index-tracking ones such as US500 "State Street SPDR S&P 500 ETF
    Trust", are listed securities, so `equity`); everything else is a crypto token, which
    is what 436 of the 556 perpetuals listed on 2026-10-14 are.
    """
    if ticker in COMMODITY_TICKERS:
        return UnderlyingType.commodity.value
    if ticker in PRE_IPO_TICKERS or _is_pre_ipo_funding(annual_funding):
        return UnderlyingType.pre_market.value
    if _EQUITY_NAME.search(name or ""):
        return UnderlyingType.equity.value
    return UnderlyingType.crypto.value


def _is_pre_ipo_funding(annual_funding: str | None) -> bool:
    try:
        return annual_funding is not None and float(annual_funding) == PRE_IPO_ANNUAL_FUNDING
    except ValueError:
        return False


def parse_variational(exchange: str, sd: SymbolData) -> Contract:
    settle = sd.get("margin_asset") or SETTLE_ASSET
    return make_contract(exchange, sd, sd["id"], settle, settle, instrument_type(sd))


def is_perpetual(listing: dict[str, Any]) -> bool:
    """A perpetual future, not a total-return swap (which has no funding interval and no
    quotes)."""
    interval = listing.get("funding_interval_s")
    return (
        bool(listing.get("ticker"))
        and isinstance(interval, (int, float))
        and interval > 0
        and isinstance(listing.get("quotes"), dict)
    )


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_stats(timeout_seconds: int) -> dict[str, Any]:
    response = requests.get(STATS_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("listings"), list):
        raise ValueError("variational /metadata/stats: no listings array")
    return payload


def fetch_variational_perps(timeout_seconds: int) -> list[dict]:
    """The live perpetuals; a ticker listed twice is kept once (as the producer does)."""
    symbols: list[dict] = []
    seen: set[str] = set()
    for listing in _fetch_stats(timeout_seconds)["listings"]:
        if not isinstance(listing, dict) or not is_perpetual(listing):
            continue
        ticker = listing["ticker"]
        if ticker in seen:
            continue
        seen.add(ticker)
        symbols.append(
            {
                "id": ticker,
                "type": "perpetual",
                "contract_size": 1.0,
                "margin_asset": SETTLE_ASSET,
                "underlying": classify_underlying(
                    ticker, listing.get("name", ""), listing.get("funding_rate")
                ),
            }
        )
    return symbols
