from __future__ import annotations

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

import re

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from .common import (
    SkipSymbol,
    instrument_type,
    make_contract,
    parse_cme_month_year,
    parse_concat,
    parse_ddmmmyy,
    resolve_margin,
    split_concat,
)


def parse_bybit(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = instrument_type(sd)

    # Bybit's API exposes the exact base, quote, and settlement currencies.
    # Prefer those fields over raw-symbol heuristics: `BTCPERP`, for example,
    # is a USDC-linear perpetual rather than an inverse BTC/USD contract.
    base_coin = sd.get("base_coin")
    quote_coin = sd.get("quote_coin")
    settle_coin = sd.get("settle_coin")
    if base_coin and quote_coin:
        return make_contract(
            exchange,
            sd,
            str(base_coin),
            str(quote_coin),
            str(settle_coin) if settle_coin else resolve_margin(str(base_coin), str(quote_coin), ctype),
            ctype,
        )

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

    # Bybit's historical `*PERP` symbols are USDC-linear perpetuals. Current
    # API metadata above remains authoritative when it is available.
    if sid.endswith("PERP") and ctype == ContractType.perpetual:
        return make_contract(
            exchange, sd, sid[:-4], "USDC", "USDC", ctype, quantity_unit="base"
        )

    # Handle remaining symbol suffixes.
    clean_sid = sid

    pair = split_concat(clean_sid, ["USDT", "USDC", "USD", "BTC", "ETH"])
    if pair:
        symbol, denominator = pair
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            margin,
            ctype,
            quantity_unit="quote" if denominator == "USD" else "base",
        )

    # If it's a perpetual and split_concat failed, it is an inverse perpetual.
    if ctype == ContractType.perpetual:
        symbol = clean_sid
        denominator = "USD"
        margin = symbol
        return make_contract(
            exchange, sd, symbol, denominator, margin, ctype, quantity_unit="quote"
        )

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


def parse_bybit_spot(exchange: str, sd: SymbolData) -> Contract:
    return parse_concat(exchange, sd)


def _to_symbol(
    item: dict, type_value: str, quantity_unit: str, *, derivative: bool = True
) -> dict[str, str | float]:
    if not derivative:
        return {"id": item["symbol"], "type": type_value}
    symbol = {
        "id": item["symbol"],
        "type": type_value,
        "base_coin": item.get("baseCoin"),
        "quote_coin": item.get("quoteCoin"),
        "settle_coin": item.get("settleCoin"),
        "quantity_unit": quantity_unit,
        "contract_size": 1.0,
    }
    return {key: value for key, value in symbol.items() if value is not None}


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_bybit_payload(
    category: str, timeout_seconds: int, cursor: str | None = None
) -> dict:
    """Fetch a Bybit instruments payload, retrying transient invalid responses."""
    params = {"category": category, "limit": 1000}
    if cursor:
        params["cursor"] = cursor
    response = requests.get(
        "https://api.bybit.com/v5/market/instruments-info",
        params=params,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


def fetch_bybit_spot(timeout_seconds: int) -> list[dict[str, str]]:
    payload = _fetch_bybit_payload("spot", timeout_seconds)
    return [
        _to_symbol(item, "spot", "base", derivative=False)
        for item in payload.get("result", {}).get("list", [])
        if item.get("status") == "Trading"
    ]


def _fetch_bybit_derivatives(
    category: str, timeout_seconds: int
) -> list[dict[str, str]]:
    symbols: dict[str, dict[str, str | float]] = {}
    cursor: str | None = None
    while True:
        payload = _fetch_bybit_payload(category, timeout_seconds, cursor)
        for item in payload.get("result", {}).get("list", []):
            if item.get("status") != "Trading":
                continue

            ctype = item.get("contractType", "")
            if "Perpetual" in ctype:
                type_value = "perpetual"
            elif "Futures" in ctype:
                type_value = "future"
            else:
                continue
            symbol = _to_symbol(
                item,
                type_value,
                "base" if category == "linear" else "quote",
            )
            symbols[str(symbol["id"])] = symbol
        cursor = payload.get("result", {}).get("nextPageCursor") or None
        if cursor is None:
            break
    return list(symbols.values())


def fetch_bybit_perps(timeout_seconds: int) -> list[dict[str, str]]:
    linear = _fetch_bybit_derivatives("linear", timeout_seconds)
    inverse = _fetch_bybit_derivatives("inverse", timeout_seconds)
    return [s for s in linear + inverse if s["type"] == "perpetual"]


def fetch_bybit_futures(timeout_seconds: int) -> list[dict[str, str]]:
    linear = _fetch_bybit_derivatives("linear", timeout_seconds)
    inverse = _fetch_bybit_derivatives("inverse", timeout_seconds)
    return [s for s in linear + inverse if s["type"] == "future"]
