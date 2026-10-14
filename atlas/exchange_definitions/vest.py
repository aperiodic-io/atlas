"""Vest Markets perpetuals (https://vestmarkets.com).

Vest lists USDC-settled linear perpetuals on US stocks and ETFs, index and
commodity futures, FX pairs and a few crypto assets. It publishes no API
documentation; ``GET /v2/exchangeInfo`` (read off the live venue, also used by
third-party clients such as glazec/rwa-cli) lists every market with its
``base``, settlement ``quote`` (USDC), asset class ``asset`` (``stock``,
``crypto`` or ``forex``) and ``tradingStatus`` (``TRADING`` or ``HALT``).

The symbol is normalised exactly as live-producers does
(``streaming/feeds/vest/vest.go``), so the Atlas ``internal_id`` equals the
producer's Atlas-scheme ID:

* stocks/ETFs are listed as ``<TICKER>-USD`` and lose the ``-USD`` quote suffix
  (``AAPL-USD-PERP`` -> ``AAPL``); futures-based and crypto markets carry a bare
  base already (``CL``, ``BTC``, ``kPEPE``);
* FX markets keep the whole pair without the hyphen (``EUR-USD`` ->
  ``EURUSD``): the bare currency would collide with a stock ticker
  (``NOK-USD``, the krone, against ``NOK-PERP``, Nokia), and Vest quotes JPY,
  CHF and CAD against USD, the inverse of the usual USDJPY;
* of two trading markets mapping to one symbol, only the smallest venue symbol
  is kept (``CL-PERP``, crude oil, wins over ``CL-USD-PERP``, Colgate).

Only TRADING markets are fetched, as the producer streams only those (Vest
rejects subscriptions to HALT markets); rows of markets that stop trading are
kept by the update's merge with the existing snapshot.
"""

from __future__ import annotations

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

VEST_EXCHANGE_INFO_URL = "https://server-prod.hz.vestmarkets.com/v2/exchangeInfo"

_ASSET_UNDERLYING = {
    "stock": UnderlyingType.equity,
    "crypto": UnderlyingType.crypto,
    "forex": UnderlyingType.forex,
}

# Vest labels its CME-futures-based markets "stock". Index futures are named by
# their contract root; every other market whose name ends in "Futures" tracks a
# commodity future (Crude Oil, Brent, Henry Hub Natural Gas, Gold, Silver,
# Copper, Platinum on 2026-10-14). ETFs (SPY, GLD, SLV, ...) stay equity.
_INDEX_FUTURES = frozenset({"SPX", "NDX", "RTY", "ES", "NQ", "YM"})


def vest_symbol(base: str, asset: str | None) -> str:
    """The published symbol of a market, as live-producers derives it."""
    if asset == "forex":
        return base.replace("-", "")
    return base.removesuffix("-USD")


def vest_underlying(symbol: str, asset: str | None, display_name: str | None) -> UnderlyingType:
    """The underlying class: the venue's ``asset``, corrected for futures it calls stock."""
    if asset == "stock":
        if symbol in _INDEX_FUTURES:
            return UnderlyingType.index
        if (display_name or "").strip().endswith("Futures"):
            return UnderlyingType.commodity
    return _ASSET_UNDERLYING.get(asset or "", UnderlyingType.unknown)


def parse_vest(exchange: str, sd: SymbolData) -> Contract:
    ctype = instrument_type(sd)
    if ctype != ContractType.perpetual:
        raise SkipSymbol(f"{exchange}: {sd['id']!r} is not a perpetual")
    base = sd.get("base")
    if not base:
        raise SkipSymbol(f"{exchange}: {sd['id']!r} carries no venue base asset")
    quote = sd.get("quote") or "USDC"
    margin = sd.get("margin_asset") or quote
    return make_contract(
        exchange, sd, vest_symbol(str(base), sd.get("asset")), quote, margin, ctype
    )


def vest_symbols(markets: list[dict]) -> list[dict]:
    """Symbol rows for the trading markets of an exchangeInfo ``symbols`` list."""
    rows: list[dict] = []
    owner: dict[str, str] = {}
    for item in sorted(markets, key=lambda m: m.get("symbol") or ""):
        if item.get("tradingStatus") != "TRADING" or not item.get("symbol") or not item.get("base"):
            continue
        symbol = vest_symbol(item["base"], item.get("asset"))
        key = f"{symbol}-{item.get('quote')}".upper()
        if key in owner:
            # Same rule as the producer: the smallest venue symbol keeps the ID.
            continue
        owner[key] = item["symbol"]
        rows.append(
            {
                "id": item["symbol"],
                "type": "perpetual",
                "base": item["base"],
                "quote": item.get("quote"),
                "asset": item.get("asset"),
                "display_name": item.get("displayName"),
                "margin_asset": item.get("quote"),
                "contract_size": 1.0,
                "quantity_unit": "base",
                "underlying": vest_underlying(
                    symbol, item.get("asset"), item.get("displayName")
                ).value,
            }
        )
    return rows


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_vest_exchange_info(timeout_seconds: int) -> dict:
    response = requests.get(VEST_EXCHANGE_INFO_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise ValueError("vest exchangeInfo: no symbols list")
    return payload


def fetch_vest_perps(timeout_seconds: int) -> list[dict]:
    """The trading perpetuals listed by Vest's exchangeInfo."""
    return vest_symbols(_fetch_vest_exchange_info(timeout_seconds)["symbols"])
