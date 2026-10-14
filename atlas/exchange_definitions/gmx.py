"""GMX v2 on Arbitrum (``gmx-perpetual``).

GMX is an oracle-priced perpetual DEX: each market is a pool (a long and a
short collateral token) trading one index token, and one index token can trade
in several pools (ETH in ETH-USDC, ETH-ETH, wstETH-USDe and USDG-USDG), each
with its own open interest, funding and liquidity. The symbol therefore carries
the pool, ``ETH[ETH-USDC]``, exactly as the live producer
(live-producers ``streaming/feeds/gmx``) names it. Positions are sized in USD
and a pool pays out in its long or its short token, so there is no single
settlement token: the denominator and the margin are ``USD``.

Source: the GMX Oracle API (https://arbitrum-api.gmxinfra.io, docs.gmx.io
``docs/api/rest-api/markets.md``): ``/markets/info`` for the markets (listed
flag, tokens, listing date) and ``/tokens`` for token symbols. Only Arbitrum
is covered; the Avalanche and MegaETH deployments are separate venues.
"""

from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

GMX_ORACLE_URL = "https://arbitrum-api.gmxinfra.io"

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# GMX publishes no asset class beyond a token's `synthetic` flag (synthetic
# crypto tokens such as BTC carry it too), so non-crypto index tokens are
# listed here. Source: docs.gmx.io docs/trading/tradfi-markets.md ("precious
# metals (gold, silver), energy (WTI crude oil, Brent crude oil, natural gas)
# and US equities (SPCX, SPY, QQQ)"). SPY and QQQ are ETFs, classed as
# equity like other venues' ETFs; SPCX is SpaceX stock. Tokenized gold
# (XAUT.v2, Tether Gold) stays crypto: GMX prices the ERC-20 token from crypto
# venues, it is not a gold reference price. Every other index token is crypto.
GMX_UNDERLYING: dict[str, UnderlyingType] = {
    "GOLD": UnderlyingType.commodity,
    "SILVER": UnderlyingType.commodity,
    "WTIOIL": UnderlyingType.commodity,
    "BRENTOIL": UnderlyingType.commodity,
    "NATGAS": UnderlyingType.commodity,
    "SPY": UnderlyingType.equity,
    "QQQ": UnderlyingType.equity,
    "SPCX": UnderlyingType.equity,
}


def gmx_underlying(index_symbol: str) -> UnderlyingType:
    return GMX_UNDERLYING.get(index_symbol.upper(), UnderlyingType.crypto)


def gmx_symbol(index_symbol: str, long_symbol: str, short_symbol: str) -> str:
    """``ETH[ETH-USDC]``: the index token and its pool."""
    return f"{index_symbol}[{long_symbol}-{short_symbol}]"


def parse_gmx(exchange: str, sd: SymbolData) -> Contract:
    ctype = instrument_type(sd)
    if ctype != ContractType.perpetual:
        raise SkipSymbol(f"{exchange}: {sd['id']!r} is not a perpetual")
    try:
        symbol = gmx_symbol(sd["index_symbol"], sd["long_symbol"], sd["short_symbol"])
    except KeyError as missing:
        raise SkipSymbol(f"{exchange}: {sd['id']!r} lacks {missing}") from None
    return make_contract(exchange, sd, symbol, "USD", "USD", ctype)


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_gmx_json(path: str, timeout_seconds: int) -> Any:
    response = requests.get(f"{GMX_ORACLE_URL}{path}", timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


def gmx_symbols(markets_info: dict, tokens: dict) -> list[dict]:
    """The listed perpetual markets of a /markets/info + /tokens snapshot.

    Swap-only pools (zero index token) and unlisted (deprecated) markets are
    left out, as the producer leaves them out; rows already in the snapshot are
    kept by the update when a market is delisted. Two listed markets mapping to
    one symbol would merge downstream, so both are dropped, as the producer
    drops them.
    """
    by_address = {t["address"].lower(): t for t in tokens.get("tokens", [])}
    rows: list[dict] = []
    for market in markets_info.get("markets", []):
        index_address = str(market.get("indexToken", "")).lower()
        if not market.get("isListed") or index_address == _ZERO_ADDRESS:
            continue
        index = by_address.get(index_address)
        long = by_address.get(str(market.get("longToken", "")).lower())
        short = by_address.get(str(market.get("shortToken", "")).lower())
        if index is None or long is None or short is None:
            continue
        sd = {
            "id": market["marketToken"],
            "type": "perpetual",
            "index_symbol": index["symbol"],
            "long_symbol": long["symbol"],
            "short_symbol": short["symbol"],
            "contract_size": 1.0,
            "quantity_unit": "base",
            "margin_asset": "USD",
            "underlying": gmx_underlying(index["symbol"]).value,
        }
        if market.get("listingDate"):
            sd["listing_date"] = market["listingDate"]
        rows.append(sd)

    counts: dict[str, int] = {}
    for sd in rows:
        key = gmx_symbol(sd["index_symbol"], sd["long_symbol"], sd["short_symbol"])
        counts[key] = counts.get(key, 0) + 1
    return [
        sd
        for sd in rows
        if counts[gmx_symbol(sd["index_symbol"], sd["long_symbol"], sd["short_symbol"])] == 1
    ]


def fetch_gmx_perps(timeout_seconds: int) -> list[dict]:
    return gmx_symbols(
        _fetch_gmx_json("/markets/info", timeout_seconds),
        _fetch_gmx_json("/tokens", timeout_seconds),
    )
