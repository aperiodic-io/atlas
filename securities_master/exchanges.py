from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .exchange_definitions.binance import (
    fetch_binance_futures_coinm,
    fetch_binance_futures_usdm,
    fetch_binance_spot,
    parse_binance,
    parse_binance_delivery,
    parse_binance_futures,
)
from .exchange_definitions.bitfinex import parse_bitfinex, parse_bitfinex_derivatives
from .exchange_definitions.bitget import parse_bitget, parse_bitget_futures
from .exchange_definitions.bitmex import parse_bitmex
from .exchange_definitions.bitstamp import parse_bitstamp
from .exchange_definitions.bybit import parse_bybit, parse_bybit_spot
from .exchange_definitions.coinbase import parse_coinbase
from .exchange_definitions.cryptofacilities import parse_cryptofacilities
from .exchange_definitions.crypto_com import parse_crypto_com
from .exchange_definitions.deribit import parse_deribit
from .exchange_definitions.ftx import parse_ftx
from .exchange_definitions.gate_io import parse_gate_io, parse_gate_io_futures
from .exchange_definitions.gemini import parse_gemini
from .exchange_definitions.huobi import (
    parse_huobi,
    parse_huobi_dm,
    parse_huobi_dm_linear_swap,
    parse_huobi_dm_swap,
)
from .exchange_definitions.hyperliquid import parse_hyperliquid
from .exchange_definitions.kraken import parse_kraken
from .exchange_definitions.kucoin import parse_kucoin
from .exchange_definitions.okex import (
    fetch_okx_futures,
    fetch_okx_spot,
    fetch_okx_swap,
    parse_okex,
    parse_okex_futures,
    parse_okex_swap,
)
from .exchange_definitions.phemex import parse_phemex
from .exchange_definitions.poloniex import parse_poloniex
from .exchange_definitions.upbit import parse_upbit
from .parser_interface import Parser

ExchangeFetcher = Callable[[int], list[dict[str, str]]]


@dataclass(frozen=True)
class ExchangeDefinition:
    parser: Parser
    beta: bool
    tardis_id: str
    exchange_fetcher: ExchangeFetcher | None = None


def _is_stable_exchange(exchange: str) -> bool:
    return exchange.startswith("okx") or exchange.startswith("binance")


def _define(
    exchange: str,
    parser: Parser,
    *,
    tardis_id: str | None = None,
    exchange_fetcher: ExchangeFetcher | None = None,
) -> ExchangeDefinition:
    return ExchangeDefinition(
        parser=parser,
        beta=not _is_stable_exchange(exchange),
        tardis_id=tardis_id or exchange,
        exchange_fetcher=exchange_fetcher,
    )


EXCHANGE_DEFINITIONS: dict[str, ExchangeDefinition] = {
    "binance": _define("binance", parse_binance, exchange_fetcher=fetch_binance_spot),
    "binance-spot": _define(
        "binance-spot", parse_binance, tardis_id="binance", exchange_fetcher=fetch_binance_spot
    ),
    "binance-futures": _define(
        "binance-futures",
        parse_binance_futures,
        tardis_id="binance-futures",
        exchange_fetcher=fetch_binance_futures_usdm,
    ),
    "binance-futures-cm": _define(
        "binance-futures-cm",
        parse_binance_delivery,
        tardis_id="binance-delivery",
        exchange_fetcher=fetch_binance_futures_coinm,
    ),
    "binance-perps": _define(
        "binance-perps",
        parse_binance_futures,
        tardis_id="binance-futures",
        exchange_fetcher=fetch_binance_futures_usdm,
    ),
    "binance-delivery": _define(
        "binance-delivery",
        parse_binance_delivery,
        tardis_id="binance-delivery",
        exchange_fetcher=fetch_binance_futures_coinm,
    ),
    "bitmex": _define("bitmex", parse_bitmex),
    "bitfinex": _define("bitfinex", parse_bitfinex),
    "bitfinex-derivatives": _define("bitfinex-derivatives", parse_bitfinex_derivatives),
    "bitget": _define("bitget", parse_bitget),
    "bitget-futures": _define("bitget-futures", parse_bitget_futures),
    "bitstamp": _define("bitstamp", parse_bitstamp),
    "bybit": _define("bybit", parse_bybit),
    "bybit-spot": _define("bybit-spot", parse_bybit_spot),
    "coinbase": _define("coinbase", parse_coinbase),
    "crypto-com": _define("crypto-com", parse_crypto_com),
    "cryptofacilities": _define("cryptofacilities", parse_cryptofacilities),
    "deribit": _define("deribit", parse_deribit),
    "ftx": _define("ftx", parse_ftx),
    "gate-io": _define("gate-io", parse_gate_io),
    "gate-io-futures": _define("gate-io-futures", parse_gate_io_futures),
    "gemini": _define("gemini", parse_gemini),
    "huobi": _define("huobi", parse_huobi),
    "huobi-dm": _define("huobi-dm", parse_huobi_dm),
    "huobi-dm-swap": _define("huobi-dm-swap", parse_huobi_dm_swap),
    "huobi-dm-linear-swap": _define("huobi-dm-linear-swap", parse_huobi_dm_linear_swap),
    "hyperliquid": _define("hyperliquid", parse_hyperliquid),
    "kraken": _define("kraken", parse_kraken),
    "kucoin": _define("kucoin", parse_kucoin),
    "okx-spot": _define(
        "okx-spot", parse_okex, tardis_id="okex", exchange_fetcher=fetch_okx_spot
    ),
    "okx-perps": _define(
        "okx-perps", parse_okex_swap, tardis_id="okex-swap", exchange_fetcher=fetch_okx_swap
    ),
    "okx-futures": _define(
        "okx-futures", parse_okex_futures, tardis_id="okex-futures", exchange_fetcher=fetch_okx_futures
    ),
    "phemex": _define("phemex", parse_phemex),
    "poloniex": _define("poloniex", parse_poloniex),
    "upbit": _define("upbit", parse_upbit),
}

PARSERS: dict[str, Parser] = {
    exchange: definition.parser for exchange, definition in EXCHANGE_DEFINITIONS.items()
}


def get_exchange_definition(exchange: str) -> ExchangeDefinition | None:
    return EXCHANGE_DEFINITIONS.get(exchange)


def is_beta_exchange(exchange: str) -> bool:
    definition = get_exchange_definition(exchange)
    if definition is None:
        return False
    return definition.beta


def to_tardis_exchange_id(exchange: str) -> str:
    definition = get_exchange_definition(exchange)
    if definition is None:
        return exchange
    return definition.tardis_id


def get_exchange_fetcher(exchange: str) -> ExchangeFetcher | None:
    definition = get_exchange_definition(exchange)
    if definition is None:
        return None
    return definition.exchange_fetcher
