from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import (
    SkipSymbol,
    epoch_to_iso,
    instrument_type,
    make_contract,
    parse_underscore_spot,
    parse_venue_fields,
    parse_yyyymmdd,
    resolve_margin,
)

_API_URL = "https://api.gateio.ws/api/v4"

# Gate serves one contract list per settlement currency. USDT contracts are
# linear (`type: direct`); BTC-settled ones are inverse (`type: inverse`, a
# contract is worth 1 USD).
_PERPETUAL_SETTLE_COINS = ("usdt", "btc")
_DELIVERY_SETTLE_COINS = ("usdt", "btc")

# `contract_type` on Gate perpetuals; an empty string is a crypto contract.
# Gate's `forex` contracts (EURUSD_USDT, USDBRL_USDT, ...) have no matching
# UnderlyingType and are recorded as `unknown`.
_UNDERLYING_TYPES = {
    "": UnderlyingType.crypto,
    "stocks": UnderlyingType.equity,
    "indices": UnderlyingType.index,
    "metals": UnderlyingType.commodity,
    "commodities": UnderlyingType.commodity,
}


def parse_gate_io(exchange: str, sd: SymbolData) -> Contract:
    contract = parse_venue_fields(exchange, sd)
    if contract is not None:
        return contract
    return parse_underscore_spot(exchange, sd)


def parse_gate_io_futures(exchange: str, sd: SymbolData) -> Contract:
    contract = parse_venue_fields(exchange, sd)
    if contract is not None:
        if contract.delivery_date is None and sd["id"].count("_") == 2:
            contract.delivery_date = parse_yyyymmdd(sd["id"].rsplit("_", 1)[1])
        return contract

    sid = sd["id"]
    parts = sid.split("_")
    ctype = instrument_type(sd)

    if len(parts) == 2:
        symbol, denominator = parts
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    if len(parts) == 3:
        symbol, denominator, date_str = parts
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            margin,
            ctype,
            parse_yyyymmdd(date_str),
        )

    raise SkipSymbol(f"{exchange}: expected 2 or 3 underscore parts in {sid!r}")


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_gate_payload(path: str, timeout_seconds: int) -> list[dict[str, Any]]:
    """Fetch one Gate public list endpoint, retrying transient invalid responses."""
    response = requests.get(f"{_API_URL}{path}", timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"Gate {path}: expected a JSON list, got {type(payload).__name__}")
    return payload


def fetch_gate_io_spot(timeout_seconds: int) -> list[dict]:
    return [
        {
            "id": item["id"],
            "type": "spot",
            "base_coin": item["base"],
            "quote_coin": item["quote"],
        }
        for item in _fetch_gate_payload("/spot/currency_pairs", timeout_seconds)
        if item.get("trade_status") == "tradable"
    ]


def _underlying(item: dict) -> str:
    if item.get("is_pre_market"):
        return UnderlyingType.pre_market.value
    return _UNDERLYING_TYPES.get(
        str(item.get("contract_type") or ""), UnderlyingType.unknown
    ).value


def _contract_symbol(item: dict, type_value: str, settle: str, pair: str) -> dict:
    base, quote = pair.split("_", 1)
    symbol: dict = {
        "id": item["name"],
        "type": type_value,
        "base_coin": base,
        "quote_coin": quote,
        "settle_coin": settle.upper(),
    }
    if item.get("type") == "inverse":
        symbol["contract_size"] = 1.0
        symbol["quantity_unit"] = "quote"
    elif (multiplier := item.get("quanto_multiplier")) not in (None, ""):
        # Linear sizes are counted in contracts of `quanto_multiplier` base units.
        symbol["contract_size"] = float(multiplier)
    symbol["underlying"] = _underlying(item)
    return symbol


def fetch_gate_io_futures(timeout_seconds: int) -> list[dict]:
    """Live perpetuals and delivery futures across Gate's settlement currencies.

    Contracts in the process of delisting (`in_delisting`) only accept
    reduce-only orders and are left out.
    """
    symbols: list[dict] = []
    for settle in _PERPETUAL_SETTLE_COINS:
        for item in _fetch_gate_payload(f"/futures/{settle}/contracts", timeout_seconds):
            if item.get("status", "trading") != "trading" or item.get("in_delisting"):
                continue
            symbols.append(_contract_symbol(item, "perpetual", settle, item["name"]))
    for settle in _DELIVERY_SETTLE_COINS:
        for item in _fetch_gate_payload(f"/delivery/{settle}/contracts", timeout_seconds):
            if item.get("in_delisting"):
                continue
            pair = item.get("underlying") or item["name"].rsplit("_", 1)[0]
            symbol = _contract_symbol(item, "future", settle, pair)
            if delivery := epoch_to_iso(item.get("expire_time"), milliseconds=False):
                symbol["delivery_date"] = delivery
            symbols.append(symbol)
    return symbols
