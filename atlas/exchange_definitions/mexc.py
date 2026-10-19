"""MEXC USDT/USDC/USD1-margined and coin-margined perpetual swaps."""

from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract, venue_margin
from .fields import parse_recorded_legs
from .http import get_json, iso_from_millis

_MEXC_ZONE_PREFIX = "mc-trade-zone-"

# MEXC has no asset-class field. It files every contract into trade zones
# (`conceptPlate`), and the TradFi ones are tagged `mc-trade-zone-tradfi` plus
# a class zone. The rule below reads those zones, most specific first:
# - `preipo` (OPENAI_USDT, ANTHROPIC_USDT) are pre-IPO companies;
# - `ETF` wins over the zones an ETF is also filed in (GLD is tagged
#   Commodities, TLT stockindex): an ETF is an equity security;
# - `Forex` (EUR_USDT, JPY_USDT), then commodities (`Commodities`, `OIL`,
#   `metals`, `metalsfutures`: XAU, SILVER, USOIL; MEXC files the gold tokens
#   PAXG and XAUT here too);
# - `stockindex` without `ETF` is a cash index (NAS100, SPX500, JP225);
# - `Stock` is a single stock. `stockmeme` is not: it holds memecoins named
#   after stocks and carries no `tradfi` zone, so it stays crypto.
# A `tradfi` contract none of these explain is `unknown`, never crypto.
_MEXC_ZONE_CLASSES = (
    ({"preipo"}, UnderlyingType.pre_market),
    ({"ETF"}, UnderlyingType.equity),
    ({"Forex"}, UnderlyingType.forex),
    ({"Commodities", "OIL", "metals", "metalsfutures"}, UnderlyingType.commodity),
    ({"stockindex"}, UnderlyingType.index),
    ({"Stock"}, UnderlyingType.equity),
)


def mexc_underlying(item: dict) -> UnderlyingType:
    zones = {
        zone.removeprefix(_MEXC_ZONE_PREFIX) for zone in item.get("conceptPlate") or []
    }
    for markers, underlying in _MEXC_ZONE_CLASSES:
        if zones & markers:
            return underlying
    if "tradfi" in zones:
        return UnderlyingType.unknown
    return UnderlyingType.crypto


def _mexc_symbol(item: dict) -> dict:
    base, quote, settle = item["baseCoin"], item["quoteCoin"], item["settleCoin"]
    inverse = settle != quote
    sd = {
        "id": item["symbol"],
        "type": "perpetual",
        "base_coin": base,
        "quote_coin": quote,
        "margin_asset": settle,
        # Linear: base units per contract. Inverse (BTC_USD, settled in BTC):
        # USD per contract.
        "contract_size": float(item.get("contractSize") or 1.0),
        "quantity_unit": "quote" if inverse else "base",
        "underlying": mexc_underlying(item).value,
        # Tardis does not cover MEXC futures, so the venue's own listing time
        # is the only listing date there is.
        "first_capture": iso_from_millis(item.get("openingTime"))
        or iso_from_millis(item.get("createTime")),
    }
    return {k: v for k, v in sd.items() if v is not None}


def fetch_mexc_perpetual(timeout_seconds: int) -> list[dict]:
    """Enabled (`state` 0) contracts; 1-4 are delivering, delivered, offline, paused."""
    payload = get_json("https://contract.mexc.com/api/v1/contract/detail", timeout_seconds)
    return [_mexc_symbol(item) for item in payload.get("data", []) if item.get("state") == 0]


def parse_mexc(exchange: str, sd: SymbolData) -> Contract:
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    parts = sd["id"].split("_")
    if len(parts) != 2:
        raise SkipSymbol(f"{exchange}: expected BASE_QUOTE in {sd['id']!r}")
    base, quote = parts
    ctype = instrument_type(sd)
    # Without recorded legs: `_USD` contracts are the coin-margined ones.
    return make_contract(exchange, sd, base, quote, venue_margin(sd, base, quote, ctype), ctype)
