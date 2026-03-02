from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract, resolve_margin, split_concat


def parse_bitstamp(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"].upper().replace("_", "")
    quotes = ["USD", "EUR", "BTC", "ETH", "USDT", "GBP", "PAX", "USDC", "USDP"]
    pair = split_concat(sid, quotes)
    if pair:
        symbol, denominator = pair
        ctype = contract_type(sd)
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            resolve_margin(symbol, denominator, ctype),
            ctype,
        )
    raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r}")
