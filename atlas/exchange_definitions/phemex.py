from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract, resolve_margin, split_concat


def parse_phemex(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = contract_type(sd)

    if sid.startswith("s"):
        pair = split_concat(sid[1:])
        if pair:
            symbol, denominator = pair
            return make_contract(exchange, sd, symbol, denominator, None, ctype)
        raise SkipSymbol(f"{exchange}: cannot split {sid!r} (s-prefixed spot)")

    pair = split_concat(sid, ["USDT", "USDC", "USD", "BTC", "ETH"])
    if pair:
        symbol, denominator = pair
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")
