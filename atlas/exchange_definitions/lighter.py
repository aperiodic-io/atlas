"""Lighter (zkLighter) perpetuals.

Instruments come from ``GET /api/v1/orderBookDetails?filter=perp``. A market is
addressed by its numeric ``market_id`` (the ``original_id``), settles in USDC,
and is named by its base asset (``ETH``, ``NVDA``, ``XAU``). USD-quoted FX pairs
are listed as one ticker (``EURUSD``) and are normalized to their base, like
any other USD-quoted market; other pairs keep the full ticker (``USDJPY``).
The live producer (live-producers ``streaming/feeds/lighter``) applies the same
rule, so both sides mint the same ``internal_id``.

Lighter publishes no asset-class field. Its markets are grouped by
``strategy_index`` (the risk strategy a market belongs to), which follows the
asset class: 2 crypto, 3 commodities, 4 FX, 5 US equities and ETFs, 6 Asian
equities, 7 pre-launch and other special markets; 0 holds only inactive
markets. ``_UNDERLYING_BY_SYMBOL`` corrects the groups that mix classes (index
products among US equities, launched tokens among the special markets) and
classifies the inactive ones.
"""

from __future__ import annotations

from datetime import UTC, datetime

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

LIGHTER_URL = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails?filter=perp"

# Fiat currencies whose USD-quoted pairs (EURUSD, ...) are normalized to the base.
LIGHTER_FIAT = frozenset(
    {
        "AUD",
        "BRL",
        "CAD",
        "CHF",
        "CNH",
        "CNY",
        "DKK",
        "EUR",
        "GBP",
        "HKD",
        "INR",
        "JPY",
        "KRW",
        "MXN",
        "NOK",
        "NZD",
        "PLN",
        "SEK",
        "SGD",
        "TRY",
        "ZAR",
    }
)

_UNDERLYING_BY_STRATEGY = {
    2: UnderlyingType.crypto,
    3: UnderlyingType.commodity,
    4: UnderlyingType.forex,
    5: UnderlyingType.equity,
    6: UnderlyingType.equity,
    7: UnderlyingType.pre_market,
}

_UNDERLYING_BY_SYMBOL = {
    # Index products listed with the US equities.
    "US500": UnderlyingType.index,
    "US100": UnderlyingType.index,
    # Special markets (strategy 7) that are not pre-launch assets.
    "US10Y": UnderlyingType.index,
    "AI": UnderlyingType.crypto,
    "ANSEM": UnderlyingType.crypto,
    "PRL": UnderlyingType.crypto,
    "DRV": UnderlyingType.crypto,
    # Inactive markets (strategy 0).
    "AI16Z": UnderlyingType.crypto,
    "MKR": UnderlyingType.crypto,
    "LAUNCHCOIN": UnderlyingType.crypto,
    "YZY": UnderlyingType.crypto,
    "1000TOSHI": UnderlyingType.crypto,
    "DUSK": UnderlyingType.crypto,
    "BIRB": UnderlyingType.crypto,
    "PIPPIN": UnderlyingType.crypto,
    "SAMSUNG": UnderlyingType.equity,
    "HYUNDAI": UnderlyingType.equity,
    "SKHYNIX": UnderlyingType.equity,
    "HANMI": UnderlyingType.equity,
    "DIA": UnderlyingType.equity,
    "MAGS": UnderlyingType.equity,
    "SOXX": UnderlyingType.equity,
    "KRCOMP": UnderlyingType.index,
    "SPACEX": UnderlyingType.pre_market,
}


def lighter_symbol(name: str) -> str:
    """The Atlas symbol of a Lighter market name (see the module docstring)."""
    if len(name) == 6 and name.endswith("USD") and name[:3] in LIGHTER_FIAT:
        return name[:3]
    return name


def lighter_underlying(name: str, strategy_index: int | None) -> UnderlyingType:
    if name in _UNDERLYING_BY_SYMBOL:
        return _UNDERLYING_BY_SYMBOL[name]
    return _UNDERLYING_BY_STRATEGY.get(strategy_index, UnderlyingType.unknown)


def parse_lighter(exchange: str, sd: SymbolData) -> Contract:
    ctype = instrument_type(sd)
    base = sd.get("base_asset")
    if not base:
        raise SkipSymbol(f"{exchange}: market {sd['id']!r} has no base asset")
    collateral = sd.get("margin_asset") or "USDC"
    return make_contract(
        exchange, sd, lighter_symbol(str(base)), collateral, collateral, ctype
    )


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_lighter_payload(timeout_seconds: int) -> dict:
    response = requests.get(LIGHTER_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or "order_book_details" not in payload:
        raise ValueError("lighter orderBookDetails: unexpected payload")
    return payload


def _iso_from_ms(value: object) -> str | None:
    try:
        ms = int(str(value))
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return (
        datetime.fromtimestamp(ms / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def lighter_symbols(payload: dict) -> list[dict]:
    """Every perp market, inactive ones included (they stay in the master)."""
    symbols = []
    for item in payload.get("order_book_details") or []:
        if item.get("market_type") != "perp" or item.get("market_id") is None:
            continue
        name = item.get("symbol") or ""
        try:
            size = float(item.get("multiplier") or 0)
        except ValueError:
            size = 0.0
        sd: dict = {
            "id": str(item["market_id"]),
            "type": "perpetual",
            "base_asset": name,
            "margin_asset": "USDC",
            "contract_size": size if size > 0 else 1.0,
            "underlying": lighter_underlying(name, item.get("strategy_index")).value,
        }
        if listed := _iso_from_ms(item.get("created_at")):
            sd["listing_date"] = listed
        symbols.append(sd)
    return symbols


def fetch_lighter_perps(timeout_seconds: int) -> list[dict]:
    return lighter_symbols(_fetch_lighter_payload(timeout_seconds))
