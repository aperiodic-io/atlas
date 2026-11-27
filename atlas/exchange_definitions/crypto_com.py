from __future__ import annotations

import re

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract, parse_yymmdd
from .fields import parse_recorded_legs
from .http import get_json, iso_from_millis


# --- Exchange API (live-producers' cryptocom-perps / cryptocom-futures) --
#
# Crypto.com derivatives are quoted and settled in USD (`BTCUSD-PERP`,
# `BTCUSD-261127`), which is also ccxt's settle currency for them.

_CRYPTOCOM_PRODUCT_TYPES = {
    "DIGITAL_CURRENCIES": UnderlyingType.crypto,
    "EQUITY": UnderlyingType.equity,
    # Index-tracking ETFs (SPY, QQQ, EWJ, leveraged ETFs): equity securities.
    "EQUITY_IND": UnderlyingType.equity,
    "COMMODITIES": UnderlyingType.commodity,
    "PRE_IPO": UnderlyingType.pre_market,
    "CURRENCIES": UnderlyingType.forex,
}

_DERIVATIVE_TYPES = {"PERPETUAL_SWAP": "perpetual", "FUTURE": "future"}


_CRYPTOCOM_INSTRUMENTS = "https://api.crypto.com/exchange/v1/public/get-instruments"


def _cryptocom_instruments(timeout_seconds: int) -> list[dict]:
    """Every instrument; an error response fails rather than empties the venue."""
    payload = get_json(_CRYPTOCOM_INSTRUMENTS, timeout_seconds)
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError(f"Crypto.com instruments: unexpected response {str(payload)[:200]!r}")
    data = (payload.get("result") or {}).get("data")
    if not isinstance(data, list):
        raise ValueError("Crypto.com instruments: no `result.data` list in the response")
    return data


def _classes_by(items: list[dict], key: str) -> dict[str, UnderlyingType]:
    """Class of each `key` value (an index, a base asset) as tagged rows give it.

    Some rows carry no `product_type` (a dated future, `SHIB_BTC@OEX_HK`); they
    take the class the venue gives the same index or asset elsewhere.
    """
    classes: dict[str, UnderlyingType] = {}
    for item in items:
        product_type = item.get("product_type")
        value = item.get(key)
        if value and product_type in _CRYPTOCOM_PRODUCT_TYPES:
            classes.setdefault(value, _CRYPTOCOM_PRODUCT_TYPES[product_type])
    return classes


def _cryptocom_underlying(item: dict, classes: dict[str, UnderlyingType], key: str) -> UnderlyingType:
    product_type = item.get("product_type")
    if product_type:
        return _CRYPTOCOM_PRODUCT_TYPES.get(product_type, UnderlyingType.unknown)
    return classes.get(item.get(key) or "", UnderlyingType.unknown)


def _fetch_cryptocom(type_value: str, timeout_seconds: int) -> list[dict]:
    items = [
        item
        for item in _cryptocom_instruments(timeout_seconds)
        if item.get("inst_type") in _DERIVATIVE_TYPES and item.get("tradable")
    ]
    by_index = _classes_by(items, "underlying_symbol")
    symbols = []
    for item in items:
        if _DERIVATIVE_TYPES[item["inst_type"]] != type_value:
            continue
        sd = {
            "id": item["symbol"],
            "type": type_value,
            "base_coin": item["base_ccy"],
            "quote_coin": item["quote_ccy"],
            "margin_asset": item["quote_ccy"],
            "contract_size": float(item.get("contract_size") or 1.0),
            "quantity_unit": "base",
            "underlying": _cryptocom_underlying(item, by_index, "underlying_symbol").value,
        }
        if type_value == "future":
            sd["delivery_date"] = iso_from_millis(item.get("expiry_timestamp_ms"))
        symbols.append({k: v for k, v in sd.items() if v is not None})
    return symbols


def fetch_cryptocom_perpetual(timeout_seconds: int) -> list[dict]:
    return _fetch_cryptocom("perpetual", timeout_seconds)


def fetch_cryptocom_futures(timeout_seconds: int) -> list[dict]:
    return _fetch_cryptocom("future", timeout_seconds)


_CRYPTOCOM_DERIVATIVE_ID = re.compile(r"^([A-Z0-9]+)USD-(PERP|\d{6})$")


def parse_cryptocom_derivative(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; history rows are `BTCUSD-PERP` / `BTCUSD-261127`."""
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    match = _CRYPTOCOM_DERIVATIVE_ID.match(sd["id"])
    if match is None:
        raise SkipSymbol(f"{exchange}: cannot parse {sd['id']!r}")
    ctype = instrument_type(sd)
    delivery = None
    if ctype == ContractType.future:
        delivery = parse_yymmdd(match.group(2))
        if delivery is None:
            raise SkipSymbol(f"{exchange}: no delivery date in {sd['id']!r}")
    return make_contract(exchange, sd, match.group(1), "USD", "USD", ctype, delivery)


# --- Exchange API (cryptocom-spot) ------------------------------------------
#
# Spot pairs come from the same instruments endpoint as the derivatives, as
# `CCY_PAIR` rows whose symbols are the spot history's ids (`BTC_USD`,
# `1INCH_USDT`, and one venue-suffixed pair, `SHIB_BTC@OEX_HK`). Crypto.com
# lists only `tradable` instruments and publishes no delisted status or listing
# time: a pair that stops trading simply disappears, as with the derivatives.
def fetch_cryptocom_spot(timeout_seconds: int) -> list[dict]:
    pairs = [
        item
        for item in _cryptocom_instruments(timeout_seconds)
        if item.get("inst_type") == "CCY_PAIR" and item.get("tradable")
    ]
    by_base = _classes_by(pairs, "base_ccy")
    return [
        {
            "id": item["symbol"],
            "type": "spot",
            "base_coin": item["base_ccy"],
            "quote_coin": item["quote_ccy"],
            "underlying": _cryptocom_underlying(item, by_base, "base_ccy").value,
        }
        for item in pairs
    ]


def parse_cryptocom_spot(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; history rows are `BASE_QUOTE`, maybe `@VENUE`-suffixed."""
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    parts = sd["id"].split("@", 1)[0].split("_")
    if len(parts) != 2 or not all(parts):
        raise SkipSymbol(f"{exchange}: expected BASE_QUOTE, got {sd['id']!r}")
    return make_contract(exchange, sd, parts[0], parts[1], None, ContractType.spot)
