from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract, parse_dash, resolve_margin, split_concat


def parse_kucoin(exchange: str, sd: SymbolData) -> Contract:
    return parse_dash(exchange, sd)


def parse_kucoin_perps(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"].upper()
    if not sid.endswith("M"):
        raise SkipSymbol(f"{exchange}: expected KuCoin perp suffix 'M' in {sd['id']!r}")

    pair = split_concat(sid[:-1])
    if pair is None:
        raise SkipSymbol(f"{exchange}: cannot split KuCoin perp symbol {sd['id']!r}")
    symbol, denominator = pair
    if symbol == "XBT":
        symbol = "BTC"

    ctype = contract_type(sd)
    margin = resolve_margin(symbol, denominator, ctype)
    return make_contract(exchange, sd, symbol, denominator, margin, ctype)
