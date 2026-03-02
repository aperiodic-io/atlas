from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import (
    SkipSymbol,
    contract_type,
    make_contract,
    parse_underscore_spot,
    parse_yyyymmdd,
    resolve_margin,
)


def parse_gate_io(exchange: str, sd: SymbolData) -> Contract:
    return parse_underscore_spot(exchange, sd)


def parse_gate_io_futures(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    parts = sid.split("_")
    ctype = contract_type(sd)

    if len(parts) == 2:
        symbol, denominator = parts
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    if len(parts) == 3:
        symbol, denominator, date_str = parts
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            margin,
            ctype,
            parse_yyyymmdd(date_str),
        )

    raise SkipSymbol(f"{exchange}: expected 2 or 3 underscore parts in {sid!r}")
