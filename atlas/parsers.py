from __future__ import annotations

from .contracts import Contract
from .exchange_definitions import SkipSymbol
from .exchanges import PARSERS
from .parser_interface import SymbolData


def parse_contract(exchange: str, symbol_data: SymbolData) -> Contract:
    parser = PARSERS.get(exchange)
    if parser is None:
        raise ValueError(f"Unknown exchange: {exchange}")
    return parser(exchange, symbol_data)


__all__ = ["SkipSymbol", "parse_contract"]
