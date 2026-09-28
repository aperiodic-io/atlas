from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, instrument_type, make_contract, resolve_margin


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
    # USDT0 on some HIP-3 dexes. Rows without it (Tardis-only) assume USDC.
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


def fetch_hyperliquid_perps(timeout_seconds: int) -> list[dict]:
    """Perpetuals on the main dex and on every HIP-3 dex, delisted ones included.

    Each symbol carries its dex's collateral token as `margin_asset`, resolved
    from the dex's `collateralToken` spot-token index.
    """
    metas = [_fetch_hyperliquid_payload("meta", timeout_seconds)]
    metas += [
        _fetch_hyperliquid_payload("meta", timeout_seconds, dex=dex)
        for dex in _hip3_dex_names(timeout_seconds)
    ]
    spot_meta = _fetch_hyperliquid_payload("spotMeta", timeout_seconds)
    token_names = {token["index"]: token["name"] for token in spot_meta.get("tokens", [])}

    symbols = []
    for meta in metas:
        collateral = token_names.get(meta.get("collateralToken"))
        for item in meta.get("universe", []):
            sd = {**_to_symbol(item["name"], "perpetual"), "contract_size": 1.0}
            if collateral:
                sd["margin_asset"] = collateral
            symbols.append(sd)
    return symbols
