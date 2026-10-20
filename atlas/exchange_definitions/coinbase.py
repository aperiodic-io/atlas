from __future__ import annotations

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import parse_dash
from .fields import parse_recorded_legs

_PRODUCTS_URL = "https://api.exchange.coinbase.com/products"
# Coinbase Exchange rejects requests that carry no User-Agent.
_HEADERS = {"User-Agent": "atlas-security-master", "Accept": "application/json"}


def parse_coinbase(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; Tardis-only rows are `BASE-QUOTE`."""
    return parse_recorded_legs(exchange, sd) or parse_dash(exchange, sd)


@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    reraise=True,
)
def _fetch_coinbase_products(timeout_seconds: int) -> list[dict]:
    response = requests.get(_PRODUCTS_URL, timeout=timeout_seconds, headers=_HEADERS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"Coinbase products: expected a JSON list, got {type(payload).__name__}")
    return payload


def _is_listed(product: dict) -> bool:
    # `online` products are listed, including halted ones (`trading_disabled`,
    # `cancel_only`), which the update must not close. `delisted` ones leave.
    return product.get("status") == "online"


def fetch_coinbase_spot(timeout_seconds: int) -> list[dict]:
    return [
        {
            "id": product["id"],
            "type": "spot",
            "base_coin": product["base_currency"],
            "quote_coin": product["quote_currency"],
        }
        for product in _fetch_coinbase_products(timeout_seconds)
        if _is_listed(product)
    ]
