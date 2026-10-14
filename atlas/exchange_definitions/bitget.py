from __future__ import annotations

import re
from datetime import datetime

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import (
    CME_MONTHS,
    KNOWN_QUOTES,
    SkipSymbol,
    epoch_to_iso,
    instrument_type,
    last_weekday_of_month,
    make_contract,
    parse_concat,
    parse_venue_fields,
    resolve_margin,
    split_concat,
)

# Bitget's unified (v3) instrument endpoint serves every product line with one
# schema. The v2 mix endpoint no longer lists coin-margined contracts at all
# (`productType=COIN-FUTURES` returns an empty list since the April 2026 move
# of coin-margined products to the unified account), so v3 is the only public
# source for `BTCUSD_CM` / `BTCCMZ26`.
_INSTRUMENTS_URL = "https://api.bitget.com/api/v3/market/instruments"
_SPOT_CATEGORY = "SPOT"
_COIN_MARGINED = "COIN-FUTURES"
# Linear product lines settle in a fixed stablecoin; coin-margined (inverse)
# contracts settle in their base coin.
_LINEAR_SETTLE_COINS = {"USDT-FUTURES": "USDT", "USDC-FUTURES": "USDC"}
_FUTURES_CATEGORIES = (*_LINEAR_SETTLE_COINS, _COIN_MARGINED)

_UNDERLYING_TYPES = {
    "crypto": UnderlyingType.crypto,
    "stock": UnderlyingType.equity,
    "metal": UnderlyingType.commodity,
    "commodity": UnderlyingType.commodity,
    "index": UnderlyingType.index,
}

# Spot quote assets for Tardis-only rows without API fields. Bitget adds
# `RLUSD` (tried before `USD`) and `VND`, and has never quoted in `TUSD`, which
# would otherwise split `USDTUSD` (USDT/USD) as USD/TUSD.
_SPOT_QUOTES = ["RLUSD", *(q for q in KNOWN_QUOTES if q != "TUSD"), "VND"]

# Pre-2024 (v1 API) contract ids carried a product-line suffix.
_LEGACY_SUFFIX_QUOTES = {
    "UMCBL": "USDT",
    "SUMCBL": "USDT",
    "CMCBL": "USDC",
    "DMCBL": "USD",
}

# Coin-margined quarterlies: `BTCUSDH26` until the April 2026 unified-account
# migration, `BTCCMH27` after it. CME month code + two-digit year.
_QUARTERLY = re.compile(
    r"^(?P<base>[A-Z0-9]+?)(?:USD|CM)(?P<month>[FGHJKMNQUVXZ])(?P<year>\d{2})$"
)
_COIN_MARGINED_PERP_SUFFIX = "USD_CM"
_USDC_PERP_SUFFIX = "PERP"


def _underlying(item: dict) -> str:
    return _UNDERLYING_TYPES.get(
        str(item.get("symbolType") or "").lower(), UnderlyingType.unknown
    ).value


def _quarterly_delivery(month_code: str, year_suffix: str) -> datetime:
    """Delivery day of a Bitget quarterly when the API no longer lists it.

    Bitget delivers coin-margined quarterlies on the last Friday of the
    contract month (08:00 UTC), e.g. BTCUSDH25 on 2025-03-28 and BTCCMZ26 on
    2026-12-25. Live contracts carry the exact API `deliveryTime` instead.
    """
    return last_weekday_of_month(2000 + int(year_suffix), CME_MONTHS[month_code], 4)


def parse_bitget(exchange: str, sd: SymbolData) -> Contract:
    contract = parse_venue_fields(exchange, sd)
    if contract is not None:
        return contract
    return parse_concat(exchange, sd, _SPOT_QUOTES)


def _parse_legacy_suffix(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    base_str, suffix = sid.rsplit("_", 1)
    quote = _LEGACY_SUFFIX_QUOTES.get(suffix)
    if quote is None:
        raise SkipSymbol(f"{exchange}: unknown suffix {suffix!r} in {sid!r}")
    ctype = instrument_type(sd)
    if quote == "USDC" and base_str.endswith(_USDC_PERP_SUFFIX):
        symbol = base_str[: -len(_USDC_PERP_SUFFIX)]
        return make_contract(exchange, sd, symbol, quote, quote, ctype)
    pair = split_concat(base_str, [quote])
    if pair is None:
        raise SkipSymbol(f"{exchange}: cannot split {base_str!r} with suffix {suffix!r}")
    symbol, denominator = pair
    return make_contract(
        exchange, sd, symbol, denominator, resolve_margin(symbol, denominator, ctype), ctype
    )


def parse_bitget_futures(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = instrument_type(sd)

    contract = parse_venue_fields(exchange, sd)
    if contract is not None:
        if (
            ctype == ContractType.future
            and contract.delivery_date is None
            and (match := _QUARTERLY.match(sid))
        ):
            contract.delivery_date = _quarterly_delivery(match.group("month"), match.group("year"))
        return contract

    if sid.endswith(_COIN_MARGINED_PERP_SUFFIX):
        symbol = sid[: -len(_COIN_MARGINED_PERP_SUFFIX)]
        return make_contract(exchange, sd, symbol, "USD", symbol, ctype)

    if "_" in sid:
        return _parse_legacy_suffix(exchange, sd)

    if ctype == ContractType.future and (match := _QUARTERLY.match(sid)):
        symbol = match.group("base")
        delivery = _quarterly_delivery(match.group("month"), match.group("year"))
        return make_contract(exchange, sd, symbol, "USD", symbol, ctype, delivery)

    if sid.endswith(_USDC_PERP_SUFFIX):
        symbol = sid[: -len(_USDC_PERP_SUFFIX)]
        return make_contract(exchange, sd, symbol, "USDC", "USDC", ctype)

    pair = split_concat(sid, ["USDT", "USDC", "USD"])
    if pair is None:
        raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")
    symbol, denominator = pair
    return make_contract(
        exchange, sd, symbol, denominator, resolve_margin(symbol, denominator, ctype), ctype
    )


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_bitget_instruments(category: str, timeout_seconds: int) -> list[dict]:
    """Fetch one Bitget product line, retrying transient or invalid responses."""
    response = requests.get(
        _INSTRUMENTS_URL, params={"category": category}, timeout=timeout_seconds
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "00000" or not isinstance(payload.get("data"), list):
        raise ValueError(
            f"Bitget {category} instruments request failed: "
            f"{payload.get('code')} {payload.get('msg')}"
        )
    return payload["data"]


def _is_live(item: dict) -> bool:
    return item.get("status") == "online"


def fetch_bitget_spot(timeout_seconds: int) -> list[dict]:
    return [
        {
            "id": item["symbol"],
            "type": "spot",
            "base_coin": item["baseCoin"],
            "quote_coin": item["quoteCoin"],
            "underlying": _underlying(item),
        }
        for item in _fetch_bitget_instruments(_SPOT_CATEGORY, timeout_seconds)
        if _is_live(item)
    ]


def _futures_symbol(item: dict, category: str) -> dict | None:
    raw_type = item.get("type")
    if raw_type == "perpetual":
        type_value = "perpetual"
    elif raw_type == "delivery":
        type_value = "future"
    else:
        return None
    symbol: dict = {
        "id": item["symbol"],
        "type": type_value,
        "base_coin": item["baseCoin"],
        "quote_coin": item["quoteCoin"],
        "settle_coin": _LINEAR_SETTLE_COINS.get(category, item["baseCoin"]),
        "underlying": _underlying(item),
    }
    if category in _LINEAR_SETTLE_COINS:
        # Linear sizes and open interest are quoted in the base coin. Coin-margined
        # contracts mix units (order book in USD contracts, open interest in the
        # base coin), so no single quantity unit is recorded for them.
        symbol["contract_size"] = 1.0
        symbol["quantity_unit"] = "base"
    if type_value == "future" and (
        delivery := epoch_to_iso(item.get("deliveryTime"), milliseconds=True)
    ):
        symbol["delivery_date"] = delivery
    return symbol


def fetch_bitget_futures(timeout_seconds: int) -> list[dict]:
    symbols: list[dict] = []
    for category in _FUTURES_CATEGORIES:
        for item in _fetch_bitget_instruments(category, timeout_seconds):
            if _is_live(item) and (symbol := _futures_symbol(item, category)):
                symbols.append(symbol)
    return symbols
