from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import parse_concat


def parse_gemini(exchange: str, sd: SymbolData) -> Contract:
    quotes = ["USDT", "GUSD", "USDC", "USD", "BTC", "ETH", "DAI"]
    return parse_concat(exchange, sd, quotes)
