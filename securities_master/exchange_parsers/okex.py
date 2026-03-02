from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import (
    SkipSymbol,
    contract_type,
    make_contract,
    parse_dash,
    parse_yymmdd,
    parse_yyyymmdd,
    resolve_margin,
)


def parse_okex(exchange: str, sd: SymbolData) -> Contract:
    return parse_dash(exchange, sd)


def parse_okex_swap(exchange: str, sd: SymbolData) -> Contract:
    parts = sd["id"].split("-")
    if len(parts) == 3 and parts[2] == "SWAP":
        symbol, denominator = parts[0], parts[1]
        ctype = contract_type(sd)
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)
    raise SkipSymbol(f"{exchange}: expected 3-part SWAP format in {sd['id']!r}")


def parse_okex_futures(exchange: str, sd: SymbolData) -> Contract:
    parts = sd["id"].split("-")
    if len(parts) != 3:
        raise SkipSymbol(f"{exchange}: expected 3 dash parts in {sd['id']!r}")

    symbol, denominator, date_str = parts
    delivery = parse_yymmdd(date_str) or parse_yyyymmdd(date_str)
    if delivery is None:
        raise SkipSymbol(f"{exchange}: cannot parse date {date_str!r} in {sd['id']!r}")

    ctype = contract_type(sd)
    margin = resolve_margin(symbol, denominator, ctype)
    return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)
