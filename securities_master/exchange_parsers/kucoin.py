from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import parse_dash


def parse_kucoin(exchange: str, sd: SymbolData) -> Contract:
    return parse_dash(exchange, sd)
