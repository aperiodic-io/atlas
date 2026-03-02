from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract, parse_concat, resolve_margin, split_concat


def parse_bitget(exchange: str, sd: SymbolData) -> Contract:
    return parse_concat(exchange, sd)


def parse_bitget_futures(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]

    if "_" in sid:
        base_str, suffix = sid.rsplit("_", 1)
        suffix_quotes = {
            "UMCBL": ["USDT"],
            "SUMCBL": ["USDT"],
            "CMCBL": ["USDC"],
            "DMCBL": ["USD"],
        }
        quotes = suffix_quotes.get(suffix)
        if quotes is None:
            raise SkipSymbol(f"{exchange}: unknown suffix {suffix!r} in {sid!r}")

        pair = split_concat(base_str, quotes)
        if pair:
            symbol, denominator = pair
            ctype = contract_type(sd)
            margin = resolve_margin(symbol, denominator, ctype)
            return make_contract(exchange, sd, symbol, denominator, margin, ctype)
        raise SkipSymbol(f"{exchange}: cannot split {base_str!r} with suffix {suffix!r}")

    return parse_concat(exchange, sd)
