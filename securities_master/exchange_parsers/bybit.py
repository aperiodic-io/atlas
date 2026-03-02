from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import (
    SkipSymbol,
    contract_type,
    make_contract,
    parse_concat,
    parse_ddmmmyy,
    resolve_margin,
    split_concat,
)


def parse_bybit(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = contract_type(sd)

    if "-" in sid:
        dash_idx = sid.index("-")
        pair = split_concat(sid[:dash_idx], ["USDT", "USDC", "USD", "BTC", "ETH"])
        delivery = parse_ddmmmyy(sid[dash_idx + 1 :])
        if pair:
            symbol, denominator = pair
            margin = resolve_margin(symbol, denominator, ctype)
            return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)
        raise SkipSymbol(f"{exchange}: cannot split base of {sid!r}")

    pair = split_concat(sid, ["USDT", "USDC", "USD", "BTC", "ETH"])
    if pair:
        symbol, denominator = pair
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


def parse_bybit_spot(exchange: str, sd: SymbolData) -> Contract:
    return parse_concat(exchange, sd)
