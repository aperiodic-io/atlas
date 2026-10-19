from __future__ import annotations

import re
from datetime import UTC, datetime

import requests

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import (
    instrument_type,
    make_contract,
    ms_to_datetime,
    ms_to_utc_timestamp,
    parse_concat,
    parse_yymmdd,
    split_concat,
    utc_timestamp,
    venue_margin,
)
from .common import SkipSymbol


def parse_binance(exchange: str, sd: SymbolData) -> Contract:
    return parse_concat(exchange, sd)


def parse_binance_futures(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"].upper()
    ctype = instrument_type(sd)

    if "_" in sid:
        base_str, suffix = sid.rsplit("_", 1)
        pair = split_concat(base_str, ["USD", "BUSD", "USDT", "USDC"])
        symbol, denominator = pair if pair else (base_str, "USD")
        margin = venue_margin(sd, symbol, denominator, ctype)
        delivery = None if suffix == "PERP" else parse_yymmdd(suffix)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)

    match = re.match(r"^([A-Z]+)(\d{6})$", sid)
    if match:
        pair = split_concat(match.group(1))
        if pair:
            symbol, denominator = pair
            margin = venue_margin(sd, symbol, denominator, ctype)
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
    ctype = instrument_type(sd)
    margin = venue_margin(sd, symbol, denominator, ctype)
    delivery = None if suffix == "PERP" else parse_yymmdd(suffix)
    return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)


def _to_symbol(id_value: str, type_value: str) -> dict[str, str]:
    return {"id": id_value, "type": type_value}


# Spot symbols in these states are either delisted (BREAK) or not yet listed.
_SPOT_UNLISTED_STATUSES = {"BREAK", "PRE_TRADING"}

# Futures in these states have stopped trading for good; `deliveryDate` is when.
# Every other status (pending, delivering, ...) is still listed. `onboardDate`
# is when a contract was listed.
_FUTURES_DELISTED_STATUSES = {"SETTLING", "CLOSE", "DELIVERED"}


def _mark_futures_lifecycle(sd: dict, item: dict, status: str | None) -> dict:
    if listed_at := ms_to_utc_timestamp(item.get("onboardDate")):
        sd["first_capture"] = listed_at
    if status in _FUTURES_DELISTED_STATUSES:
        # Perpetuals keep a 2100-12-25 sentinel until a delist time is set, so a
        # delivery date that has not passed yet falls back to now.
        now = datetime.now(UTC)
        delivered = ms_to_datetime(item.get("deliveryDate"))
        sd["end_date"] = utc_timestamp(delivered if delivered and delivered <= now else now)
    return sd


def fetch_binance_spot(timeout_seconds: int) -> list[dict[str, str]]:
    payload = requests.get(
        "https://api.binance.com/api/v3/exchangeInfo", timeout=timeout_seconds
    ).json()
    return [
        _to_symbol(item["symbol"].lower(), "spot")
        for item in payload.get("symbols", [])
        if item.get("status") not in _SPOT_UNLISTED_STATUSES
    ]


def _normalize_binance_instrument_type(raw_type: str | None) -> str:
    # TradFi perpetuals (stocks, metals, FX) are `TRADIFI_PERPETUAL`.
    return "perpetual" if (raw_type or "").endswith("PERPETUAL") else "future"


_BINANCE_UNDERLYING_TYPE_MAP = {
    "COIN": UnderlyingType.crypto,
    "CN_EQUITY": UnderlyingType.equity,
    "COMMODITY": UnderlyingType.commodity,
    "EQUITY": UnderlyingType.equity,
    "FX": UnderlyingType.forex,
    "HK_EQUITY": UnderlyingType.equity,
    "INDEX": UnderlyingType.index,
    "KR_EQUITY": UnderlyingType.equity,
    "PREMARKET": UnderlyingType.pre_market,
}


def _normalize_binance_underlying_type(raw_type: str | None) -> UnderlyingType:
    return _BINANCE_UNDERLYING_TYPE_MAP.get(raw_type or "", UnderlyingType.unknown)


def fetch_binance_futures_usdm(timeout_seconds: int) -> list[dict]:
    payload = requests.get(
        "https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=timeout_seconds
    ).json()
    symbols = []
    for item in payload.get("symbols", []):
        sd = _to_symbol(
            item["symbol"].lower(),
            _normalize_binance_instrument_type(item.get("contractType")),
        )
        sd["contract_size"] = 1.0
        if margin_asset := item.get("marginAsset"):
            sd["margin_asset"] = margin_asset
        sd["underlying"] = _normalize_binance_underlying_type(
            item.get("underlyingType")
        ).value
        symbols.append(_mark_futures_lifecycle(sd, item, item.get("status")))
    return symbols


def fetch_binance_futures_coinm(timeout_seconds: int) -> list[dict]:
    payload = requests.get(
        "https://dapi.binance.com/dapi/v1/exchangeInfo", timeout=timeout_seconds
    ).json()
    symbols = []
    for item in payload.get("symbols", []):
        sd = _to_symbol(
            item["symbol"].lower(),
            _normalize_binance_instrument_type(item.get("contractType")),
        )
        if (cs := item.get("contractSize")) is not None:
            sd["contract_size"] = float(cs)
        if margin_asset := item.get("marginAsset"):
            sd["margin_asset"] = margin_asset
        sd["underlying"] = _normalize_binance_underlying_type(
            item.get("underlyingType")
        ).value
        symbols.append(_mark_futures_lifecycle(sd, item, item.get("contractStatus")))
    return symbols
