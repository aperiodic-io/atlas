from __future__ import annotations

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract, parse_ddmmmyy
from .fields import parse_recorded_legs
from .http import get_json, iso_from_millis


def _deribit_margin(sd: SymbolData, symbol: str, denominator: str, linear: bool) -> str:
    """Settle asset: the venue's when recorded, else from the naming scheme.

    `BTC_USDC-PERPETUAL` names its quote, and these linear contracts settle in
    it; the bare `BTC-PERPETUAL` is inverse and settles in BTC.
    """
    reported = sd.get("margin_asset")
    if reported:
        return str(reported)
    return denominator if linear else symbol


def parse_deribit(exchange: str, sd: SymbolData) -> Contract:
    ctype = instrument_type(sd)
    if ctype == ContractType.unknown:
        raise SkipSymbol(
            f"{exchange}: unsupported contract type {sd.get('type')!r} for {sd['id']!r}"
        )

    sid = sd["id"]
    if "-" not in sid:
        parts = sid.split("_", 1)
        if len(parts) == 2:
            symbol, denominator = parts
            return make_contract(exchange, sd, symbol, denominator, None, ctype)
        raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")

    dash_parts = sid.split("-")
    first = dash_parts[0]
    linear = "_" in first
    if linear:
        symbol, denominator = first.split("_", 1)
    else:
        symbol, denominator = first, "USD"
    margin = _deribit_margin(sd, symbol, denominator, linear)

    if dash_parts[1] == "PERPETUAL":
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    delivery = parse_ddmmmyy(dash_parts[1])
    return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)


# --- Exchange API (live-producers' deribit-perpetual / deribit-futures) -----

_DERIBIT_UNDERLYING_TYPES = {
    "crypto": UnderlyingType.crypto,
    "crypto_index": UnderlyingType.index,
    "equity": UnderlyingType.equity,
    "equity_etf": UnderlyingType.equity,
    "commodity": UnderlyingType.commodity,
    "preipo": UnderlyingType.pre_market,
}


def _deribit_symbol(item: dict) -> dict:
    perpetual = item.get("settlement_period") == "perpetual"
    inverse = item.get("instrument_type") == "reversed"
    sd = {
        "id": item["instrument_name"],
        "type": "perpetual" if perpetual else "future",
        "base_coin": item["base_currency"],
        "quote_coin": item["quote_currency"],
        "margin_asset": item["settlement_currency"],
        # Inverse: USD per contract (BTC-PERPETUAL is 10). Linear: base units.
        "contract_size": float(item.get("contract_size") or 1.0),
        "quantity_unit": "quote" if inverse else "base",
        "underlying": _DERIBIT_UNDERLYING_TYPES.get(
            item.get("underlying_type") or "", UnderlyingType.unknown
        ).value,
    }
    if not perpetual:
        sd["delivery_date"] = iso_from_millis(item.get("expiration_timestamp"))
    return {k: v for k, v in sd.items() if v is not None}


def _fetch_deribit_futures_kind(timeout_seconds: int) -> list[dict]:
    """Every unexpired, active future and perpetual (Deribit's `future` kind)."""
    payload = get_json(
        "https://www.deribit.com/api/v2/public/get_instruments",
        timeout_seconds,
        params={"currency": "any", "kind": "future", "expired": "false"},
    )
    return [_deribit_symbol(item) for item in payload.get("result", []) if item.get("is_active")]


def fetch_deribit_perpetual(timeout_seconds: int) -> list[dict]:
    return [sd for sd in _fetch_deribit_futures_kind(timeout_seconds) if sd["type"] == "perpetual"]


def fetch_deribit_futures(timeout_seconds: int) -> list[dict]:
    return [sd for sd in _fetch_deribit_futures_kind(timeout_seconds) if sd["type"] == "future"]


def parse_deribit_api(exchange: str, sd: SymbolData) -> Contract:
    return parse_recorded_legs(exchange, sd) or parse_deribit(exchange, sd)
