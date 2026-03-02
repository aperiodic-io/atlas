from __future__ import annotations

import requests

import re

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from .common import (
    SkipSymbol,
    contract_type,
    make_contract,
    parse_cme_month_year,
    parse_concat,
    parse_ddmmmyy,
    resolve_margin,
    split_concat,
)


def parse_bybit(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = contract_type(sd)

    if "-" in sid:
        parts = sid.split("-")
        if len(parts) == 2:
            base_str, date_str = parts
            pair = split_concat(base_str, ["USDT", "USDC", "USD", "BTC", "ETH"])
            delivery = parse_ddmmmyy(date_str)
            if pair:
                symbol, denominator = pair
                margin = resolve_margin(symbol, denominator, ctype)
                return make_contract(
                    exchange, sd, symbol, denominator, margin, ctype, delivery
                )
        raise SkipSymbol(f"{exchange}: cannot parse dated symbol {sid!r}")

    # Handle CME style futures like BTCUSDH26
    match = re.match(r"^([A-Z]{2,})([FGHJKMNQUVXZ])(\d{2})$", sid)
    if match:
        raw_base = match.group(1)
        pair = split_concat(raw_base, ["USDT", "USDC", "USD", "EUR", "ETH", "BTC"])
        delivery = parse_cme_month_year(match.group(2), match.group(3))
        if pair:
            symbol, denominator = pair
            margin = resolve_margin(symbol, denominator, ctype)
            return make_contract(
                exchange, sd, symbol, denominator, margin, ctype, delivery
            )

        # Inverse futures: BTCUSDH26 -> symbol: BTC, denominator: USD
        if raw_base.endswith("USD"):
            symbol = raw_base[:-3]
            denominator = "USD"
            margin = symbol
            return make_contract(
                exchange, sd, symbol, denominator, margin, ctype, delivery
            )

    # Handle PERP suffix
    clean_sid = sid
    if sid.endswith("PERP"):
        clean_sid = sid[:-4]

    pair = split_concat(clean_sid, ["USDT", "USDC", "USD", "BTC", "ETH"])
    if pair:
        symbol, denominator = pair
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    # If it's a perpetual and split_concat failed, it's likely an inverse perpetual (e.g. BTCPERP)
    if ctype == ContractType.perpetual:
        symbol = clean_sid
        denominator = "USD"
        margin = symbol
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


def parse_bybit_spot(exchange: str, sd: SymbolData) -> Contract:
    return parse_concat(exchange, sd)


def _to_symbol(id_value: str, type_value: str) -> dict[str, str]:
    return {"id": id_value, "type": type_value}


def fetch_bybit_spot(timeout_seconds: int) -> list[dict[str, str]]:
    payload = requests.get(
        "https://api.bybit.com/v5/market/instruments-info?category=spot",
        timeout=timeout_seconds,
    ).json()
    return [
        _to_symbol(item["symbol"], "spot")
        for item in payload.get("result", {}).get("list", [])
        if item.get("status") == "Trading"
    ]


def _fetch_bybit_derivatives(
    category: str, timeout_seconds: int
) -> list[dict[str, str]]:
    payload = requests.get(
        f"https://api.bybit.com/v5/market/instruments-info?category={category}",
        timeout=timeout_seconds,
    ).json()
    symbols = []
    for item in payload.get("result", {}).get("list", []):
        if item.get("status") != "Trading":
            continue

        ctype = item.get("contractType", "")
        if "Perpetual" in ctype:
            symbols.append(_to_symbol(item["symbol"], "perpetual"))
        elif "Futures" in ctype:
            symbols.append(_to_symbol(item["symbol"], "future"))
    return symbols


def fetch_bybit_perps(timeout_seconds: int) -> list[dict[str, str]]:
    linear = _fetch_bybit_derivatives("linear", timeout_seconds)
    inverse = _fetch_bybit_derivatives("inverse", timeout_seconds)
    return [s for s in linear + inverse if s["type"] == "perpetual"]


def fetch_bybit_futures(timeout_seconds: int) -> list[dict[str, str]]:
    linear = _fetch_bybit_derivatives("linear", timeout_seconds)
    inverse = _fetch_bybit_derivatives("inverse", timeout_seconds)
    return [s for s in linear + inverse if s["type"] == "future"]
