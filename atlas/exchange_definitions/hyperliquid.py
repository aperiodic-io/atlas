from __future__ import annotations

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

    # Perpetuals on Hyperliquid are usually just the symbol name
    symbol = sid
    denominator = "USDC"
    margin = "USDC"
    return make_contract(exchange, sd, symbol, denominator, margin, ctype)


def _to_symbol(id_value: str, type_value: str) -> dict[str, str]:
    return {"id": id_value, "type": type_value}


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_hyperliquid_payload(type_value: str, timeout_seconds: int) -> dict:
    """Fetch Hyperliquid metadata, retrying transient invalid responses."""
    response = requests.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": type_value},
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


def fetch_hyperliquid_perps(timeout_seconds: int) -> list[dict]:
    response = _fetch_hyperliquid_payload("meta", timeout_seconds)

    return [
        {**_to_symbol(item["name"], "perpetual"), "contract_size": 1.0}
        for item in response.get("universe", [])
    ]
