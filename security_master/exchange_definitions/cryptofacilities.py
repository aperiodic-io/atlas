from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import (
    SkipSymbol,
    contract_type,
    make_contract,
    parse_yymmdd,
    resolve_margin,
    split_concat,
)

CF_ASSET = {"XBT": "BTC"}


def parse_cryptofacilities(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    parts = sid.split("_")
    if len(parts) < 2:
        raise SkipSymbol(f"{exchange}: expected at least 2 underscore parts in {sid!r}")

    pair = split_concat(parts[1], ["USD", "EUR", "USDT"])
    if not pair:
        raise SkipSymbol(f"{exchange}: cannot split {parts[1]!r} in {sid!r}")

    symbol, denominator = pair
    symbol = CF_ASSET.get(symbol, symbol)
    ctype = contract_type(sd)
    margin = resolve_margin(symbol, denominator, ctype)
    delivery = parse_yymmdd(parts[2]) if len(parts) >= 3 else None
    return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)
