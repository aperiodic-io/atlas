"""WhiteBIT USDT-margined perpetual futures (`BTC_PERP`)."""

from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol
from .fields import parse_recorded_legs
from .http import get_json

_WHITEBIT_API = "https://whitebit.com/api/v4/public"

# WhiteBIT's TradFi perpetuals (stocks, ETFs, metals, energy) are listed by
# `/futures` but not by `/markets`, which only knows the crypto futures and
# flags TradFi ones `isTradFiFutures`; `/futures` itself names the class only
# in free text (`index_name`: "Gold (Derivatives) future contract"). So a
# TradFi contract is a commodity when its index asset is one of these, and an
# equity (single stock or ETF) otherwise.
_WHITEBIT_COMMODITIES = frozenset({"XAU", "XAG", "XCU", "XPD", "XPT", "BZ", "CL", "NATGAS"})


def _whitebit_underlying(item: dict, market: dict | None) -> UnderlyingType:
    tradfi = market is None or bool(market.get("isTradFiFutures"))
    if not tradfi:
        return UnderlyingType.crypto
    asset = item.get("index_currency") or item.get("stock_currency")
    if asset in _WHITEBIT_COMMODITIES:
        return UnderlyingType.commodity
    return UnderlyingType.equity


def fetch_whitebit_perpetual(timeout_seconds: int) -> list[dict]:
    futures = get_json(f"{_WHITEBIT_API}/futures", timeout_seconds).get("result", [])
    markets = {
        market["name"]: market
        for market in get_json(f"{_WHITEBIT_API}/markets", timeout_seconds)
        if market.get("type") == "futures"
    }
    symbols = []
    for item in futures:
        if item.get("product_type") != "Perpetual":
            continue
        settle = item["money_currency"]
        symbols.append(
            {
                "id": item["ticker_id"],
                "type": "perpetual",
                "base_coin": item["stock_currency"],
                "quote_coin": settle,
                "margin_asset": settle,
                "contract_size": 1.0,
                "quantity_unit": "base",
                "underlying": _whitebit_underlying(item, markets.get(item["ticker_id"])).value,
            }
        )
    return symbols


def parse_whitebit(exchange: str, sd: SymbolData) -> Contract:
    contract = parse_recorded_legs(exchange, sd)
    if contract is None:
        raise SkipSymbol(f"{exchange}: {sd['id']!r} has no recorded base/quote")
    return contract
