from __future__ import annotations

import re

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract, resolve_margin, split_concat


def parse_bitfinex(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    if sid.startswith("f"):
        raise SkipSymbol(f"{exchange}: funding symbol {sid!r} skipped")
    if not sid.startswith("t"):
        raise SkipSymbol(f"{exchange}: expected 't' prefix in {sid!r}")

    pair_str = sid[1:]
    ctype = contract_type(sd)

    if ":" in pair_str:
        symbol, denominator = pair_str.split(":", 1)
        denominator = "USDT" if denominator == "UST" else denominator
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    quotes = [
        "USDT",
        "UST",
        "USD",
        "BTC",
        "ETH",
        "EOS",
        "EUR",
        "GBP",
        "JPY",
        "CNHT",
        "XAUT",
    ]
    pair = split_concat(pair_str, quotes)
    if pair:
        symbol, denominator = pair
        denominator = "USDT" if denominator == "UST" else denominator
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    raise SkipSymbol(f"{exchange}: cannot split {sid!r}")


def parse_bitfinex_derivatives(exchange: str, sd: SymbolData) -> Contract:
    match = re.match(r"^([A-Z]+)F0:([A-Z]+)F0$", sd["id"])
    if match:
        symbol = match.group(1)
        denominator = "USDT" if match.group(2) == "UST" else match.group(2)
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            denominator,
            contract_type(sd),
        )
    raise SkipSymbol(f"{exchange}: cannot parse derivative {sd['id']!r}")
