from __future__ import annotations

import re
from datetime import UTC, datetime

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .fields import parse_recorded_legs
from .http import get_json
from .common import (
    SkipSymbol,
    delisted_end_date,
    instrument_type,
    make_contract,
    ms_to_datetime,
    parse_dash,
    parse_yymmdd,
    resolve_margin,
    split_concat,
    utc_timestamp,
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
# contract_status: 0 delisted, 1 listed, 2 pending listing, 3 suspended,
# 4 listing suspended, 5 settling, 6 delivering, 7 settled, 8 delivered. Swaps
# pass through 5-7 at every funding settlement, and a suspension is temporary,
# so all of those are still listed (live-producers streams only 1, 5 and 6).
# A swap scheduled for delisting carries the time in `delivery_time` (empty
# otherwise) and is suspended until then.
_HTX_NOT_YET_LISTED = 2
_HTX_DELISTED_STATUSES = {0, 8}


def _htx_end_date(item: dict) -> str | None:
    delisting = ms_to_datetime(item.get("delivery_time"))
    if item.get("contract_status") in _HTX_DELISTED_STATUSES:
        return delisted_end_date(item.get("delivery_time"))
    if delisting and delisting <= datetime.now(UTC):
        return utc_timestamp(delisting)
    return None


def _htx_listed_at(create_date: object) -> str | None:
    """`create_date` is a `YYYYMMDD` day."""
    try:
        return utc_timestamp(datetime.strptime(str(create_date), "%Y%m%d"))
    except ValueError:
        return None

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
        if item.get("contract_status") == _HTX_NOT_YET_LISTED:
            continue
        code = item["contract_code"]
        base, quote = code.split("-", 1)
        sd = {
            "id": code,
            "type": "perpetual",
            "base_coin": base,
            "quote_coin": quote,
            "margin_asset": item.get("trade_partition") or quote,
            "contract_size": float(item.get("contract_size") or 1.0),
            "quantity_unit": "base",
            "underlying": _htx_underlying(item.get("tradfi_labels") or []).value,
            "first_capture": _htx_listed_at(item.get("create_date")),
            "end_date": _htx_end_date(item),
        }
        symbols.append({k: v for k, v in sd.items() if v is not None})
    return symbols


def parse_htx_linear_swap(exchange: str, sd: SymbolData) -> Contract:
    return parse_recorded_legs(exchange, sd) or parse_huobi_dm_linear_swap(exchange, sd)
