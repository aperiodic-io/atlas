"""Pacifica (pacifica.fi), a Solana perpetuals DEX that also lists RWA perps.

Contracts come from ``GET https://api.pacifica.fi/api/v1/info``. Every perp
settles in USDC (ccxt's ``pacifica.parse_market`` agrees), so USDC is both the
denominator and the margin asset. The venue addresses a market by its symbol
(``BTC``, ``kBONK``, ``EURUSD``), which is the ``original_id``.

FX perps are six-letter pairs. A pair quoted in USD publishes its base
(``EURUSD`` -> ``EUR``), any other pair keeps both legs (``USDJPY``), so the
internal ID matches the live producer's (live-producers ``pacifica.go``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract

PACIFICA_INFO_URL = "https://api.pacifica.fi/api/v1/info"
PACIFICA_SETTLE_ASSET = "USDC"

# ISO 4217 codes an FX perp can be quoted in.
_FIAT = {
    "USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD", "CNH", "CNY", "HKD",
    "SGD", "SEK", "NOK", "MXN", "ZAR", "TRY", "KRW", "INR", "BRL",
}  # fmt: skip

# The venue's own market categories. Pacifica's API carries none; its web app
# (app.pacifica.fi) tags every market with one or more of Majors, L1/L2, Meme,
# DeFi, AI, Pre-Market, Equities, Commodities and FX. This is that table for
# the non-crypto tags (as shipped on 2026-10-14); every other tag is crypto.
_VENUE_CATEGORIES: dict[str, str] = {
    "ANTHROPIC": "Pre-Market",
    **dict.fromkeys(
        [
            "NVDA", "TSLA", "GOOGL", "MSTR", "PLTR", "MSFT", "AMZN", "COIN",
            "HOOD", "META", "AAPL", "NFLX", "GME", "CRCL", "SP500", "SPCX",
            "SKHYNIX", "SAMSUNG", "MU", "DRAM", "SNDK",
        ],
        "Equities",
    ),
    **dict.fromkeys(
        ["PAXG", "XAU", "XAG", "COPPER", "CL", "NATGAS", "URNM", "PLATINUM"],
        "Commodities",
    ),
    **dict.fromkeys(
        ["EURUSD", "USDKRW", "NZDUSD", "AUDUSD", "USDJPY", "USDCAD", "USDCHF", "GBPUSD"],
        "FX",
    ),
}  # fmt: skip

_CATEGORY_UNDERLYING = {
    "Pre-Market": UnderlyingType.pre_market,
    "Equities": UnderlyingType.equity,
    "Commodities": UnderlyingType.commodity,
    "FX": UnderlyingType.forex,
}

# Where the venue's tag is too coarse for the underlying class: Pacifica files
# its S&P 500 perp under Equities, but its underlying is the index.
_UNDERLYING_OVERRIDES = {"SP500": UnderlyingType.index}


def iso_millis(epoch_ms: int) -> str:
    """Epoch milliseconds as ``2025-06-02T16:22:13.944Z`` (the Tardis style)."""
    t = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_ms % 1000:03d}Z"


def pacifica_contract_symbol(base: str) -> str:
    """The symbol a market's base asset publishes under (see module docstring)."""
    if len(base) == 6 and base[3:] == "USD" and base[:3] in _FIAT and base[:3] != "USD":
        return base[:3]
    return base


def pacifica_underlying(
    symbol: str, execution_modes: list[str] | None = None
) -> UnderlyingType:
    """The underlying class of a Pacifica market.

    From the venue's own category; a market the table does not know yet is
    crypto unless the venue trades it through its RWA request-for-quote mode
    (``execution_modes`` containing ``rfq``, which only RWA perps use), in
    which case its class is unknown until the table learns it.
    """
    if symbol in _UNDERLYING_OVERRIDES:
        return _UNDERLYING_OVERRIDES[symbol]
    category = _VENUE_CATEGORIES.get(symbol)
    if category is not None:
        return _CATEGORY_UNDERLYING[category]
    if execution_modes and "rfq" in execution_modes:
        return UnderlyingType.unknown
    return UnderlyingType.crypto


def parse_pacifica(exchange: str, sd: SymbolData) -> Contract:
    ctype = instrument_type(sd)
    if ctype != ContractType.perpetual:
        raise SkipSymbol(
            f"{exchange}: only perpetuals are listed, got {sd.get('type')!r}"
        )
    base = sd.get("base_asset") or sd["id"]
    settle = sd.get("margin_asset") or PACIFICA_SETTLE_ASSET
    return make_contract(
        exchange, sd, pacifica_contract_symbol(str(base)), settle, settle, ctype
    )


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_pacifica_info(timeout_seconds: int) -> Any:
    response = requests.get(PACIFICA_INFO_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("success"):
        raise ValueError(f"pacifica /info returned no data: {str(payload)[:200]}")
    return payload


def fetch_pacifica_perps(timeout_seconds: int) -> list[dict]:
    """Every Pacifica perpetual, with its listing time as ``first_capture``."""
    payload = _fetch_pacifica_info(timeout_seconds)
    symbols: list[dict] = []
    for item in payload.get("data") or []:
        if (
            not item
            or item.get("instrument_type") != "perpetual"
            or not item.get("symbol")
        ):
            continue
        base = item.get("base_asset") or item["symbol"]
        sd: dict[str, Any] = {
            "id": item["symbol"],
            "type": "perpetual",
            "base_asset": base,
            "margin_asset": PACIFICA_SETTLE_ASSET,
            "contract_size": 1.0,
            "underlying": pacifica_underlying(base, item.get("execution_modes")).value,
        }
        if created := item.get("created_at"):
            sd["first_capture"] = iso_millis(int(created))
        symbols.append(sd)
    return symbols
