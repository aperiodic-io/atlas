from __future__ import annotations

import requests

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract, resolve_margin


def parse_hyperliquid(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    if sid.startswith("@"):
        raise SkipSymbol(f"{exchange}: internal index symbol {sid!r} skipped")

    ctype = contract_type(sd)

    if "/" in sid:
        symbol, denominator = sid.split("/", 1)
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    # Perpetuals on Hyperliquid are usually just the symbol name
    symbol = sid
    denominator = "USDC"
    margin = "USDC"
    return make_contract(exchange, sd, symbol, denominator, margin, ctype)


def _to_symbol(id_value: str, type_value: str) -> dict[str, str]:
    return {"id": id_value, "type": type_value}


def fetch_hyperliquid_spot(timeout_seconds: int) -> list[dict[str, str]]:
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "spotMeta"}
    response = requests.post(url, json=payload, timeout=timeout_seconds).json()

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


def fetch_hyperliquid_perps(timeout_seconds: int) -> list[dict[str, str]]:
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "meta"}
    response = requests.post(url, json=payload, timeout=timeout_seconds).json()

    universe = response.get("universe", [])
    return [_to_symbol(item["name"], "perpetual") for item in universe]
