from __future__ import annotations

import re

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .fields import parse_recorded_legs
from .http import get_json
from .common import (
    SkipSymbol,
    instrument_type,
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
        ctype = instrument_type(sd)
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)
    raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r}")


def parse_huobi_dm(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = instrument_type(sd)

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


# --- Exchange API (live-producers' htx-perpetual-usdt) ----------------------
#
# Only contract_status 1 (listed) trades; 5 and 6 are the transient
# settlement/delivery states every contract passes through at funding time.
# Everything else (0 delisting, 2 pending, 3 suspended, 4, 7, 8) is excluded,
# as live-producers does.
_HTX_ACTIVE_STATUSES = {1, 5, 6}

_HTX_UNDERLYING = (
    # An ETF is listed under both labels; it is an equity security.
    ("Stocks", UnderlyingType.equity),
    ("Indices", UnderlyingType.index),
    ("Metals", UnderlyingType.commodity),
    ("Commodities", UnderlyingType.commodity),
)


def _htx_underlying(labels: list[str]) -> UnderlyingType:
    if not labels:
        return UnderlyingType.crypto
    for label, underlying in _HTX_UNDERLYING:
        if label in labels:
            return underlying
    return UnderlyingType.unknown


def fetch_htx_perpetual_usdt(timeout_seconds: int) -> list[dict]:
    payload = get_json(
        "https://api.hbdm.vn/linear-swap-api/v1/swap_contract_info", timeout_seconds
    )
    symbols = []
    for item in payload.get("data", []):
        if item.get("business_type") != "swap":
            continue
        if item.get("contract_status") not in _HTX_ACTIVE_STATUSES:
            continue
        code = item["contract_code"]
        base, quote = code.split("-", 1)
        symbols.append(
            {
                "id": code,
                "type": "perpetual",
                "base_coin": base,
                "quote_coin": quote,
                "margin_asset": item.get("trade_partition") or quote,
                "contract_size": float(item.get("contract_size") or 1.0),
                "quantity_unit": "base",
                "underlying": _htx_underlying(item.get("tradfi_labels") or []).value,
            }
        )
    return symbols


def parse_htx_linear_swap(exchange: str, sd: SymbolData) -> Contract:
    return parse_recorded_legs(exchange, sd) or parse_huobi_dm_linear_swap(exchange, sd)
