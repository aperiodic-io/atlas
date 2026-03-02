from __future__ import annotations

import re
from datetime import datetime

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract, resolve_margin


def parse_ftx(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = contract_type(sd)

    if "/" in sid:
        symbol, denominator = sid.split("/", 1)
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            resolve_margin(symbol, denominator, ctype),
            ctype,
        )

    if sid.endswith("-PERP"):
        symbol = sid[:-5]
        return make_contract(exchange, sd, symbol, "USD", symbol, ctype)

    if "-" in sid:
        symbol, date_str = sid.split("-", 1)
        match = re.match(r"^(\d{2})(\d{2})$", date_str)
        if match:
            try:
                delivery = datetime(2000 + int(match.group(2)), int(match.group(1)), 1)
                return make_contract(exchange, sd, symbol, "USD", symbol, ctype, delivery)
            except ValueError:
                pass

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")
