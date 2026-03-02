from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import parse_underscore_spot


def parse_crypto_com(exchange: str, sd: SymbolData) -> Contract:
    return parse_underscore_spot(exchange, sd)
