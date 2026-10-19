"""Aster (asterdex.com) USDⓈ-margined perpetuals.

Aster's futures API is Binance USD-M's wire format: ``GET /fapi/v1/exchangeInfo``
lists every contract with ``baseAsset``/``quoteAsset``/``marginAsset``, a
``status`` and the venue's own classification of the underlying
(``symbolType`` 1 for real-world assets, ``underlyingSubType`` tags such as
``STOCK``, ``ETF``, ``Commodities`` and ``pre-launch``). References: ccxt 4.5.88
``aster.py``; github.com/asterdex/api-docs ``aster-finance-futures-api-v3.md``.

The live producer (live-producers ``streaming/feeds/aster``) streams the
``TRADING`` contracts under ``symbol = baseAsset`` and
``denominator = quoteAsset``, settled in ``marginAsset`` (equal to the quote on
every Aster contract: USDT, USD1 or Aster's ``U``), so the Atlas ID is
``perpetual-<base>-<quote>:<margin>``.

Tardis covers the venue as ``aster-futures`` with lower-cased ids
(``btcusdt``), the form its websocket streams use; the API fetcher emits the
same ids so the hybrid source merges the two, as for Binance. Rows only Tardis
knows (no asset fields) are split against the known quote assets.
"""

from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .binance import mark_futures_lifecycle
from .common import SkipSymbol, instrument_type, make_contract, split_concat

ASTER_EXCHANGE_INFO_URL = "https://fapi.asterdex.com/fapi/v1/exchangeInfo"


def parse_aster(exchange: str, sd: SymbolData) -> Contract:
    base, quote = sd.get("base_asset"), sd.get("quote_asset")
    if not base or not quote:
        pair = split_concat(sd["id"].upper())
        if pair is None:
            raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r} against known quotes")
        base, quote = pair
    ctype = instrument_type(sd)
    margin = sd.get("margin_asset") or quote
    return make_contract(exchange, sd, base, quote, margin, ctype)


def classify_aster_underlying(item: dict[str, Any]) -> UnderlyingType:
    """The underlying class from Aster's own tags.

    ``underlyingSubType`` carries ``pre-launch`` for pre-IPO perps (OpenAI,
    Anthropic, ...), ``STOCK``/``ETF`` for equities and funds, ``Commodities``
    for metals, oil and gas. A crypto contract has ``underlyingType`` ``COIN``,
    ``symbolType`` 0 and only crypto tags (``Meme``, ``AI``, ...). Anything else
    is ``unknown`` rather than guessed.
    """
    tags = set(item.get("underlyingSubType") or [])
    if "pre-launch" in tags:
        return UnderlyingType.pre_market
    if tags & {"STOCK", "ETF"}:
        return UnderlyingType.equity
    if "Commodities" in tags:
        return UnderlyingType.commodity
    if item.get("symbolType", 0) == 0 and item.get("underlyingType") == "COIN":
        return UnderlyingType.crypto
    return UnderlyingType.unknown


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_aster_exchange_info(timeout_seconds: int) -> Any:
    response = requests.get(ASTER_EXCHANGE_INFO_URL, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise ValueError("Aster exchangeInfo has no symbols list")
    return payload


def aster_symbols(payload: dict[str, Any]) -> list[dict]:
    """Every perpetual of an exchangeInfo payload, delisting ones included.

    A contract still ``PENDING_TRADING`` has no contract type yet and is left
    out until it lists. As on Binance, ``onboardDate`` is the listing time and a
    ``SETTLING``/``CLOSE``/``DELIVERED`` contract stopped trading at
    ``deliveryDate``.
    """
    symbols = []
    for item in payload.get("symbols", []):
        if item.get("contractType") != "PERPETUAL":
            continue
        sd: dict[str, Any] = {
            "id": item["symbol"].lower(),
            "type": "perpetual",
            "contract_size": 1.0,
            "base_asset": item.get("baseAsset"),
            "quote_asset": item.get("quoteAsset"),
            "margin_asset": item.get("marginAsset"),
            "underlying": classify_aster_underlying(item).value,
        }
        symbols.append(mark_futures_lifecycle(sd, item, item.get("status")))
    return symbols


def fetch_aster_perps(timeout_seconds: int) -> list[dict]:
    return aster_symbols(_fetch_aster_exchange_info(timeout_seconds))
