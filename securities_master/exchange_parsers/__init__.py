from __future__ import annotations

from ..parser_interface import Parser
from .binance import parse_binance, parse_binance_delivery, parse_binance_futures
from .bitfinex import parse_bitfinex, parse_bitfinex_derivatives
from .bitget import parse_bitget, parse_bitget_futures
from .bitmex import parse_bitmex
from .bitstamp import parse_bitstamp
from .bybit import parse_bybit, parse_bybit_spot
from .coinbase import parse_coinbase
from .common import SkipSymbol
from .crypto_com import parse_crypto_com
from .cryptofacilities import parse_cryptofacilities
from .deribit import parse_deribit
from .ftx import parse_ftx
from .gate_io import parse_gate_io, parse_gate_io_futures
from .gemini import parse_gemini
from .huobi import (
    parse_huobi,
    parse_huobi_dm,
    parse_huobi_dm_linear_swap,
    parse_huobi_dm_swap,
)
from .hyperliquid import parse_hyperliquid
from .kraken import parse_kraken
from .kucoin import parse_kucoin
from .okex import parse_okex, parse_okex_futures, parse_okex_swap
from .phemex import parse_phemex
from .poloniex import parse_poloniex
from .upbit import parse_upbit

PARSERS: dict[str, Parser] = {
    "binance": parse_binance,
    "binance-futures": parse_binance_futures,
    "binance-delivery": parse_binance_delivery,
    "bitmex": parse_bitmex,
    "bitfinex": parse_bitfinex,
    "bitfinex-derivatives": parse_bitfinex_derivatives,
    "bitget": parse_bitget,
    "bitget-futures": parse_bitget_futures,
    "bitstamp": parse_bitstamp,
    "bybit": parse_bybit,
    "bybit-spot": parse_bybit_spot,
    "coinbase": parse_coinbase,
    "crypto-com": parse_crypto_com,
    "cryptofacilities": parse_cryptofacilities,
    "deribit": parse_deribit,
    "ftx": parse_ftx,
    "gate-io": parse_gate_io,
    "gate-io-futures": parse_gate_io_futures,
    "gemini": parse_gemini,
    "huobi": parse_huobi,
    "huobi-dm": parse_huobi_dm,
    "huobi-dm-swap": parse_huobi_dm_swap,
    "huobi-dm-linear-swap": parse_huobi_dm_linear_swap,
    "hyperliquid": parse_hyperliquid,
    "kraken": parse_kraken,
    "kucoin": parse_kucoin,
    "okex": parse_okex,
    "okex-swap": parse_okex_swap,
    "okx-perps": parse_okex_futures,
    "phemex": parse_phemex,
    "poloniex": parse_poloniex,
    "upbit": parse_upbit,
}

__all__ = ["PARSERS", "SkipSymbol"]
