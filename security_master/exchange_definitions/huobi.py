from __future__ import annotations

import re

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import (
    SkipSymbol,
    contract_type,
    make_contract,
    parse_dash,
    parse_yymmdd,
    resolve_margin,
    split_concat,
)

HUOBI_DM_ROLLING = {"CW", "NW", "CQ", "NQ"}


def parse_huobi(exchange: str, sd: SymbolData) -> Contract:
    pair = split_concat(sd["id"].upper())
    if pair:
        symbol, denominator = pair
        ctype = contract_type(sd)
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)
    raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r}")


def parse_huobi_dm(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = contract_type(sd)

    if "_" in sid:
        symbol, suffix = sid.split("_", 1)
        if suffix in HUOBI_DM_ROLLING:
            return make_contract(exchange, sd, symbol, "USD", symbol, ctype)
        raise SkipSymbol(f"{exchange}: unknown rolling suffix {suffix!r} in {sid!r}")

    match = re.match(r"^([A-Z]+)(\d{6})$", sid)
    if match:
        return make_contract(
            exchange,
            sd,
            match.group(1),
            "USD",
            match.group(1),
            ctype,
            parse_yymmdd(match.group(2)),
        )

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


def parse_huobi_dm_swap(exchange: str, sd: SymbolData) -> Contract:
    return parse_dash(exchange, sd)


def parse_huobi_dm_linear_swap(exchange: str, sd: SymbolData) -> Contract:
    return parse_dash(exchange, sd)
