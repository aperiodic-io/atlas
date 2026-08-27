from __future__ import annotations

import requests

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from .common import (
    SkipSymbol,
    instrument_type,
    make_contract,
    parse_dash,
    parse_yymmdd,
    parse_yyyymmdd,
    resolve_margin,
)


# OKX decorates the quote leg of its unified-margin instrument families:
# `BTC-USD_UM-260828`, `AAPL-USD_UM_XPERP-310613`. The venue's own `uly` for
# both is plain `BTC-USD` / `AAPL-USD`, and the decoration is not part of the
# quote asset. These families are linear (`ctType: linear`, `settleCcy: USD`),
# so they must not fall through to the inverse branch of `resolve_margin`.
# Longest first, so `_UM_XPERP` is not truncated to `_XPERP` by the `_UM` rule.
_UNIFIED_MARGIN_SUFFIXES = ("_UM_XPERP", "_UM")


def _split_unified_margin(denominator: str) -> tuple[str, bool]:
    """Strip an OKX unified-margin family suffix off the quote leg.

    Returns the bare denominator and whether the suffix was present, which is
    what marks the contract as linear. An unrecognised suffix is left intact
    rather than guessed at.
    """
    for suffix in _UNIFIED_MARGIN_SUFFIXES:
        if denominator.endswith(suffix):
            return denominator[: -len(suffix)], True
    return denominator, False


def _okex_margin(symbol: str, denominator: str, ctype: ContractType) -> tuple[str, str | None]:
    denominator, unified_margin = _split_unified_margin(denominator)
    if unified_margin:
        return denominator, denominator
    return denominator, resolve_margin(symbol, denominator, ctype)


def parse_okex(exchange: str, sd: SymbolData) -> Contract:
    return parse_dash(exchange, sd)


def parse_okex_swap(exchange: str, sd: SymbolData) -> Contract:
    parts = sd["id"].split("-")
    if len(parts) == 3 and parts[2] == "SWAP":
        symbol, denominator = parts[0], parts[1]
        ctype = instrument_type(sd)
        denominator, margin = _okex_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)
    raise SkipSymbol(f"{exchange}: expected 3-part SWAP format in {sd['id']!r}")


def parse_okex_futures(exchange: str, sd: SymbolData) -> Contract:
    parts = sd["id"].split("-")
    if len(parts) != 3:
        raise SkipSymbol(f"{exchange}: expected 3 dash parts in {sd['id']!r}")

    symbol, denominator, date_str = parts
    delivery = parse_yymmdd(date_str) or parse_yyyymmdd(date_str)
    if delivery is None:
        raise SkipSymbol(f"{exchange}: cannot parse date {date_str!r} in {sd['id']!r}")

    ctype = instrument_type(sd)
    denominator, margin = _okex_margin(symbol, denominator, ctype)
    return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)


def _to_symbol(item: dict, type_value: str) -> dict:
    result: dict = {"id": item["instId"], "type": type_value}
    if ct_val := item.get("ctVal"):
        result["contract_size"] = float(ct_val)
    return result


def fetch_okx_spot(timeout_seconds: int) -> list[dict[str, str]]:
    payload = requests.get(
        "https://www.okx.com/api/v5/public/instruments?instType=SPOT",
        timeout=timeout_seconds,
    ).json()
    return [
        _to_symbol(item, "spot")
        for item in payload.get("data", [])
        if item.get("state") == "live"
    ]


def fetch_okx_swap(timeout_seconds: int) -> list[dict[str, str]]:
    payload = requests.get(
        "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
        timeout=timeout_seconds,
    ).json()
    return [
        _to_symbol(item, "perpetual")
        for item in payload.get("data", [])
        if item.get("state") == "live"
    ]


def fetch_okx_futures(timeout_seconds: int) -> list[dict[str, str]]:
    payload = requests.get(
        "https://www.okx.com/api/v5/public/instruments?instType=FUTURES",
        timeout=timeout_seconds,
    ).json()
    return [
        _to_symbol(item, "future")
        for item in payload.get("data", [])
        if item.get("state") == "live"
    ]
