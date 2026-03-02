from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract

POLONIEX_OLD_QUOTES = {"USDT", "BTC", "ETH", "TRX", "BNB", "USDC"}


def parse_poloniex(exchange: str, sd: SymbolData) -> Contract:
    parts = sd["id"].split("_")
    if len(parts) != 2:
        raise SkipSymbol(f"{exchange}: expected 2 underscore parts in {sd['id']!r}")

    a, b = parts
    ctype = contract_type(sd)
    if a in POLONIEX_OLD_QUOTES and b not in POLONIEX_OLD_QUOTES:
        return make_contract(exchange, sd, b, a, None, ctype)
    return make_contract(exchange, sd, a, b, None, ctype)
