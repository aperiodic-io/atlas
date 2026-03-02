from __future__ import annotations

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract, parse_ddmmmyy


def parse_deribit(exchange: str, sd: SymbolData) -> Contract:
    ctype = contract_type(sd)
    if ctype == ContractType.unknown:
        raise SkipSymbol(
            f"{exchange}: unsupported contract type {sd.get('type')!r} for {sd['id']!r}"
        )

    sid = sd["id"]
    if "-" not in sid:
        parts = sid.split("_", 1)
        if len(parts) == 2:
            symbol, denominator = parts
            return make_contract(exchange, sd, symbol, denominator, None, ctype)
        raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")

    dash_parts = sid.split("-")
    first = dash_parts[0]
    if "_" in first:
        symbol, denominator = first.split("_", 1)
    else:
        symbol, denominator = first, "USD"

    if dash_parts[1] == "PERPETUAL":
        return make_contract(exchange, sd, symbol, denominator, symbol, ctype)

    delivery = parse_ddmmmyy(dash_parts[1])
    return make_contract(exchange, sd, symbol, denominator, symbol, ctype, delivery)
