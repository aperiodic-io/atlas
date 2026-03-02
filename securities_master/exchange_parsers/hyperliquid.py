from __future__ import annotations

import re

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract


def parse_hyperliquid(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    if sid.startswith("@"):
        raise SkipSymbol(f"{exchange}: index symbol {sid!r} skipped")
    if re.match(r"^[A-Z0-9]+$", sid):
        return make_contract(exchange, sd, sid, "USDC", "USDC", contract_type(sd))
    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")
