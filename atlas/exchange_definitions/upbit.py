from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract
from .fields import parse_recorded_legs
from .http import get_json


def parse_upbit(exchange: str, sd: SymbolData) -> Contract:
    parts = sd["id"].split("-")
    if len(parts) == 2:
        quote, base = parts
        return make_contract(exchange, sd, base, quote, None, instrument_type(sd))
    raise SkipSymbol(f"{exchange}: expected 2 dash parts in {sd['id']!r}")


# --- Exchange API (upbit-spot) -------------------------------------------------
#
# `/v1/market/all` lists every market Upbit trades as `QUOTE-BASE` (`KRW-BTC`,
# `BTC-0G`, `USDT-BREV`), the Atlas id. Upbit publishes no market status,
# listing time or delisting time: a delisted market simply leaves the list.
# Markets under an investment warning or caution (`market_event.warning`,
# `market_event.caution`, formerly `market_warning`) still trade and stay
# listed.
_MARKETS_URL = "https://api.upbit.com/v1/market/all"


def parse_upbit_api(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; history rows are `QUOTE-BASE`."""
    return parse_recorded_legs(exchange, sd) or parse_upbit(exchange, sd)


def _upbit_symbol(item: dict) -> dict:
    market = item["market"]
    parts = market.split("-")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Upbit market: expected QUOTE-BASE, got {market!r}")
    quote, base = parts
    return {
        "id": market,
        "type": "spot",
        "base_coin": base,
        "quote_coin": quote,
        # Upbit classifies nothing and lists only crypto assets.
        "underlying": UnderlyingType.crypto.value,
    }


def fetch_upbit_spot(timeout_seconds: int) -> list[dict]:
    payload = get_json(_MARKETS_URL, timeout_seconds, params={"is_details": "true"})
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) and isinstance(item.get("market"), str) for item in payload
    ):
        raise ValueError(f"Upbit markets: expected a list of markets, got {payload!r:.200}")
    return [_upbit_symbol(item) for item in payload]
