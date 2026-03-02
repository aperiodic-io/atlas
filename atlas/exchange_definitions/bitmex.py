from __future__ import annotations

import re

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import (
    SkipSymbol,
    contract_type,
    make_contract,
    parse_cme_month_year,
    resolve_margin,
    split_concat,
)

BITMEX_ASSET = {"XBT": "BTC", "XXBT": "BTC"}


def _norm_bitmex(asset: str) -> str:
    return BITMEX_ASSET.get(asset, asset)


def parse_bitmex(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = contract_type(sd)

    if "_" in sid:
        parts = sid.split("_", 1)
        if len(parts) == 2:
            symbol = _norm_bitmex(parts[0])
            denominator = _norm_bitmex(parts[1])
            margin = resolve_margin(symbol, denominator, ctype)
            return make_contract(exchange, sd, symbol, denominator, margin, ctype)
        raise SkipSymbol(f"{exchange}: cannot parse underscore symbol {sid!r}")

    match = re.match(r"^([A-Z]{2,})([FGHJKMNQUVXZ])(\d{2})$", sid)
    if match:
        raw_base = match.group(1)
        pair = split_concat(raw_base, ["USDT", "USDC", "USD", "EUR", "ETH", "BTC"])
        if pair:
            symbol, denominator = pair
            symbol = _norm_bitmex(symbol)
            margin = resolve_margin(symbol, denominator, ctype)
            delivery = parse_cme_month_year(match.group(2), match.group(3))
            return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)

        symbol = _norm_bitmex(raw_base)
        delivery = parse_cme_month_year(match.group(2), match.group(3))
        return make_contract(exchange, sd, symbol, "USD", symbol, ctype, delivery)

    pair = split_concat(sid, ["USDT", "USDC", "USD", "EUR", "ETH", "BTC"])
    if pair:
        symbol, denominator = pair
        symbol = _norm_bitmex(symbol)
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    raise SkipSymbol(f"{exchange}: cannot parse symbol {sid!r}")
