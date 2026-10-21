from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract, resolve_margin, utc_timestamp


import re

def parse_hyperliquid(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    if re.match(r"^@\d+$", sid):
        raise SkipSymbol(f"{exchange}: internal index symbol {sid!r} skipped")

    ctype = instrument_type(sd)

    if "/" in sid:
        symbol, denominator = sid.split("/", 1)
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    # Perpetuals are named by their asset alone; HIP-3 builder-deployed perps
    # carry their dex as a prefix (`xyz:TSLA`), which is kept in the symbol.
    # A perp dex quotes, margins and settles in its own collateral token, which
    # the fetcher records per symbol: USDC on the main dex, but e.g. USDH or
    # USDT0 on some HIP-3 dexes. Rows without it (history) assume USDC.
    collateral = sd.get("margin_asset") or "USDC"
    return make_contract(exchange, sd, sid, collateral, collateral, ctype)


def _to_symbol(id_value: str, type_value: str) -> dict[str, str]:
    return {"id": id_value, "type": type_value}


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_hyperliquid_payload(
    type_value: str, timeout_seconds: int, dex: str | None = None
) -> Any:
    """Fetch Hyperliquid metadata, retrying transient invalid responses."""
    body = {"type": type_value}
    if dex is not None:
        body["dex"] = dex
    response = requests.post(
        "https://api.hyperliquid.xyz/info",
        json=body,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


def fetch_hyperliquid_spot(timeout_seconds: int) -> list[dict[str, str]]:
    response = _fetch_hyperliquid_payload("spotMeta", timeout_seconds)

    tokens = {token["index"]: token["name"] for token in response.get("tokens", [])}
    universe = response.get("universe", [])

    symbols = []
    for item in universe:
        name = item.get("name")
        tokens_indices = item.get("tokens")
        if name and tokens_indices and len(tokens_indices) == 2:
            base_name = tokens[tokens_indices[0]]
            quote_name = tokens[tokens_indices[1]]
            # The ID in Hyperliquid spot is often BASE/QUOTE
            symbols.append(_to_symbol(f"{base_name}/{quote_name}", "spot"))
    return symbols


def _hip3_dex_names(timeout_seconds: int) -> list[str]:
    """Names of the HIP-3 builder-deployed perp dexes (the main dex is `null`)."""
    dexes = _fetch_hyperliquid_payload("perpDexs", timeout_seconds)
    return [dex["name"] for dex in dexes if dex and dex.get("name")]


# HIP-3 dexes list stocks, indices, commodities and FX next to crypto. The
# venue classifies them itself through the `perpCategories` info request
# (`[["xyz:TSLA", "stocks"], ...]`); its labels map as follows. `rates` are
# treasury yields (para:10Y), benchmark levels like an index. `preipo`
# (io:OAI, vntl:SPACEX) are private companies, Binance's PREMARKET class.
_HYPERLIQUID_CATEGORIES = {
    "crypto": UnderlyingType.crypto,
    "stocks": UnderlyingType.equity,
    "stock": UnderlyingType.equity,
    "indices": UnderlyingType.index,
    "rates": UnderlyingType.index,
    "commodities": UnderlyingType.commodity,
    "fx": UnderlyingType.forex,
    "preipo": UnderlyingType.pre_market,
}

# HIP-3 assets `perpCategories` does not list: almost all delisted before the
# venue started categorising. Classified by hand from the asset each name
# tracks; the same coin on another dex is categorised alike where one exists
# (mkts:SEMI as km:SEMI, km:USBOND as mkts:USBOND). Names whose asset could not
# be identified (cash:CAR, io:SBE) stay unknown.
_HYPERLIQUID_UNCATEGORISED = {
    **dict.fromkeys(
        [
            "cash:BTC", "cash:ETH", "hyna:BTC", "hyna:ETH", "hyna:HYPE", "hyna:SOL",
            "hyna:LIT", "hyna:ZEC", "hyna:XRP", "hyna:LIGHTER", "hyna:BNB", "hyna:DOGE",
            "hyna:SUI", "hyna:PUMP", "hyna:FARTCOIN", "hyna:ENA", "hyna:XMR", "hyna:LTC",
            "hyna:LINK", "hyna:XPL", "hyna:IP", "hyna:BCH", "hyna:ADA", "hyna:1000PEPE",
            "hyna:BASED",
        ],
        UnderlyingType.crypto,
    ),
    **dict.fromkeys(
        [
            "cash:TSLA", "cash:NVDA", "cash:HOOD", "cash:GOOGL", "cash:INTC", "cash:AMZN",
            "cash:MSFT", "cash:META", "cash:EWY", "cash:KWEB", "io:EWY", "km:USBOND",
            "mkts:BABA", "mkts:TSLA", "mkts:AAPL", "mkts:GOOGL", "mkts:NVDA",
            "mkts:TENCENT", "mkts:MU", "mkts:PLTR", "mkts:XIAOMI", "mkts:BMNR", "mkts:RTX",
            "para:NAVER", "para:TENCENT", "para:CAMBRICON", "para:INNOLIGHT", "xyz:ACN",
            "xyz:INNOLIGHT", "xyz:AMEC", "xyz:CATL", "xyz:RELIANCE",
        ],
        UnderlyingType.equity,
    ),
    **dict.fromkeys(
        [
            "abcd:USA500", "cash:USA500", "flx:USA100", "io:H100", "para:H100", "xyz:H100",
            "km:JPN225", "mkts:JPN225", "mkts:USENERGY", "mkts:SEMI", "mkts:GLDMINE",
            "mkts:BVIV", "xyz:NIFTY", "xyz:IBOV",
        ],
        UnderlyingType.index,
    ),
    **dict.fromkeys(
        [
            "cash:GOLD", "cash:SILVER", "cash:WTI", "hyna:SILVER", "hyna:GOLD",
            "mkts:USOIL", "mkts:SILVER", "mkts:GOLD", "xyz:URANIUM", "xyz:TTF",
        ],
        UnderlyingType.commodity,
    ),
    "mkts:EUR": UnderlyingType.forex,
    "xyz:YMTC": UnderlyingType.pre_market,
    "xyz:JIO": UnderlyingType.pre_market,
}


def _hyperliquid_underlying(name: str, categories: dict[str, str]) -> UnderlyingType:
    if ":" not in name:
        # The main dex lists crypto only.
        return UnderlyingType.crypto
    category = categories.get(name)
    if category is not None:
        return _HYPERLIQUID_CATEGORIES.get(category.lower(), UnderlyingType.unknown)
    return _HYPERLIQUID_UNCATEGORISED.get(name, UnderlyingType.unknown)


def fetch_hyperliquid_perps(timeout_seconds: int) -> list[dict]:
    """Perpetuals on the main dex and on every HIP-3 dex, delisted ones included.

    Delisted perps carry an `end_date` so the updater can close their rows.

    Each symbol carries its dex's collateral token as `margin_asset`, resolved
    from the dex's `collateralToken` spot-token index, and its asset class as
    `underlying`.
    """
    metas = [_fetch_hyperliquid_payload("meta", timeout_seconds)]
    metas += [
        _fetch_hyperliquid_payload("meta", timeout_seconds, dex=dex)
        for dex in _hip3_dex_names(timeout_seconds)
    ]
    spot_meta = _fetch_hyperliquid_payload("spotMeta", timeout_seconds)
    captured_at = utc_timestamp(datetime.now(UTC))
    token_names = {token["index"]: token["name"] for token in spot_meta.get("tokens", [])}
    categories = dict(_fetch_hyperliquid_payload("perpCategories", timeout_seconds))

    symbols = []
    for meta in metas:
        collateral = token_names.get(meta.get("collateralToken"))
        for item in meta.get("universe", []):
            sd = {**_to_symbol(item["name"], "perpetual"), "contract_size": 1.0}
            if collateral:
                sd["margin_asset"] = collateral
            sd["underlying"] = _hyperliquid_underlying(item["name"], categories).value
            if item.get("isDelisted"):
                # Hyperliquid flags delistings without saying when; use first sighting.
                sd["end_date"] = captured_at
            symbols.append(sd)
    return symbols
