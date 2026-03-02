from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract


def parse_upbit(exchange: str, sd: SymbolData) -> Contract:
    parts = sd["id"].split("-")
    if len(parts) == 2:
        quote, base = parts
        return make_contract(exchange, sd, base, quote, None, contract_type(sd))
    raise SkipSymbol(f"{exchange}: expected 2 dash parts in {sd['id']!r}")
