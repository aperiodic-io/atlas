from __future__ import annotations

import re

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import (
    contract_type,
    make_contract,
    parse_concat,
    parse_yymmdd,
    resolve_margin,
    split_concat,
)
from .common import SkipSymbol


def parse_binance(exchange: str, sd: SymbolData) -> Contract:
    return parse_concat(exchange, sd)


def parse_binance_futures(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"].upper()
    ctype = contract_type(sd)

    if "_" in sid:
        base_str, suffix = sid.rsplit("_", 1)
        pair = split_concat(base_str, ["USD", "BUSD", "USDT", "USDC"])
        symbol, denominator = pair if pair else (base_str, "USD")
        margin = resolve_margin(symbol, denominator, ctype)
        delivery = None if suffix == "PERP" else parse_yymmdd(suffix)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)

    match = re.match(r"^([A-Z]+)(\d{6})$", sid)
    if match:
        pair = split_concat(match.group(1))
        if pair:
            symbol, denominator = pair
            margin = resolve_margin(symbol, denominator, ctype)
            return make_contract(
                exchange,
                sd,
                symbol,
                denominator,
                margin,
                ctype,
                parse_yymmdd(match.group(2)),
            )

    return parse_concat(exchange, sd)


def parse_binance_delivery(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"].upper()
    if "_" not in sid:
        raise SkipSymbol(f"{exchange}: expected underscore in {sid!r}")

    base_str, suffix = sid.rsplit("_", 1)
    pair = split_concat(base_str, ["USD", "BUSD", "USDT"])
    symbol, denominator = pair if pair else (base_str, "USD")
    ctype = contract_type(sd)
    margin = resolve_margin(symbol, denominator, ctype)
    delivery = None if suffix == "PERP" else parse_yymmdd(suffix)
    return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)
