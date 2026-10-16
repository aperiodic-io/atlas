from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .fields import parse_recorded_legs
from .http import get_json, iso_from_seconds
from .common import (
    SkipSymbol,
    instrument_type,
    make_contract,
    parse_underscore_spot,
    parse_yyyymmdd,
    resolve_margin,
)


def parse_gate_io(exchange: str, sd: SymbolData) -> Contract:
    return parse_underscore_spot(exchange, sd)


def parse_gate_io_futures(exchange: str, sd: SymbolData) -> Contract:
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


# --- Exchange API (live-producers' gateio-* venues) -------------------------
#
# Gate classifies every futures contract by `contract_type`; crypto contracts
# leave it empty. `is_pre_market` marks contracts on companies or tokens that
# are not publicly traded yet (OPENAI_USDT, ANTHROPIC_USDT, ...); for indices
# (H100_USDT, a GPU-rental price index) it only means "new", so it overrides
# the class of stocks and crypto alone.
_GATE_CONTRACT_TYPES = {
    "": UnderlyingType.crypto,
    "stocks": UnderlyingType.equity,
    "indices": UnderlyingType.index,
    "metals": UnderlyingType.commodity,
    "commodities": UnderlyingType.commodity,
    "forex": UnderlyingType.forex,
}

_GATE_API = "https://api.gateio.ws/api/v4"


def _gate_underlying(item: dict) -> UnderlyingType:
    contract_type = item.get("contract_type") or ""
    if item.get("is_pre_market") and contract_type in {"", "stocks"}:
        return UnderlyingType.pre_market
    return _GATE_CONTRACT_TYPES.get(contract_type, UnderlyingType.unknown)


def _gate_perpetual(item: dict, settle: str) -> dict:
    name = item["name"]
    base, quote = name.split("_", 1)
    # A BTC-settled (inverse) contract is worth one USD and reports
    # quanto_multiplier 0; linear ones are `quanto_multiplier` base units.
    multiplier = float(item.get("quanto_multiplier") or 0)
    inverse = item.get("type") == "inverse"
    return {
        "id": name,
        "type": "perpetual",
        "base_coin": base,
        "quote_coin": quote,
        "margin_asset": settle,
        "contract_size": multiplier if multiplier > 0 else 1.0,
        "quantity_unit": "quote" if inverse else "base",
        "underlying": _gate_underlying(item).value,
    }


def _fetch_gate_perpetuals(settle: str, timeout_seconds: int) -> list[dict]:
    items = get_json(f"{_GATE_API}/futures/{settle.lower()}/contracts", timeout_seconds)
    return [_gate_perpetual(item, settle) for item in items]


def fetch_gateio_perpetual_usdt(timeout_seconds: int) -> list[dict]:
    return _fetch_gate_perpetuals("USDT", timeout_seconds)


def fetch_gateio_perpetual_btc(timeout_seconds: int) -> list[dict]:
    return _fetch_gate_perpetuals("BTC", timeout_seconds)


def fetch_gateio_futures_usdt(timeout_seconds: int) -> list[dict]:
    """USDT-settled dated (delivery) futures, e.g. `BTC_USDT_20261023`.

    Delivery contracts carry no `contract_type`; each names its perpetual
    (`underlying: "BTC_USDT"`), whose class it inherits.
    """
    items = get_json(f"{_GATE_API}/delivery/usdt/contracts", timeout_seconds)
    classes = {
        sd["id"]: sd["underlying"] for sd in _fetch_gate_perpetuals("USDT", timeout_seconds)
    }
    symbols = []
    for item in items:
        base, quote, _date = item["name"].split("_", 2)
        multiplier = float(item.get("quanto_multiplier") or 0)
        sd = {
            "id": item["name"],
            "type": "future",
            "base_coin": base,
            "quote_coin": quote,
            "margin_asset": "USDT",
            "contract_size": multiplier if multiplier > 0 else 1.0,
            "quantity_unit": "base",
            "delivery_date": iso_from_seconds(item.get("expire_time")),
            "underlying": classes.get(item.get("underlying"), UnderlyingType.unknown.value),
        }
        symbols.append({k: v for k, v in sd.items() if v is not None})
    return symbols


def parse_gateio(exchange: str, sd: SymbolData) -> Contract:
    """Live-producers' Gate venues: API legs first, then the id (Tardis rows)."""
    return parse_recorded_legs(exchange, sd) or parse_gate_io_futures(exchange, sd)
