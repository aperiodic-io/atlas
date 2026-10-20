from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .exchange_definitions.apex import fetch_apex_perps, parse_apex
from .exchange_definitions.aster import fetch_aster_perps, parse_aster
from .exchange_definitions.avantis import fetch_avantis_perps, parse_avantis
from .exchange_definitions.binance import (
    fetch_binance_futures_coinm,
    fetch_binance_futures_usdm,
    fetch_binance_spot,
    parse_binance,
    parse_binance_delivery,
    parse_binance_futures,
)
from .exchange_definitions.bitfinex import (
    fetch_bitfinex_perpetual,
    fetch_bitfinex_spot,
    parse_bitfinex_api,
    parse_bitfinex_perpetual_api,
)
from .exchange_definitions.bitget import (
    fetch_bitget_futures_coin,
    fetch_bitget_perpetual_coin,
    fetch_bitget_perpetual_usdc,
    fetch_bitget_perpetual_usdt,
    fetch_bitget_spot,
    parse_bitget,
    parse_bitget_v3,
)
from .exchange_definitions.bitstamp import (
    fetch_bitstamp_perpetual,
    fetch_bitstamp_spot,
    parse_bitstamp_api,
)
from .exchange_definitions.bybit import (
    fetch_bybit_futures,
    fetch_bybit_perps,
    fetch_bybit_spot,
    parse_bybit,
    parse_bybit_spot,
)
from .exchange_definitions.coinbase import fetch_coinbase_spot, parse_coinbase
from .exchange_definitions.crypto_com import (
    fetch_cryptocom_futures,
    fetch_cryptocom_perpetual,
    fetch_cryptocom_spot,
    parse_cryptocom_derivative,
    parse_cryptocom_spot,
)
from .exchange_definitions.cryptofacilities import (
    fetch_kraken_futures,
    fetch_kraken_perpetual,
    parse_kraken_derivative,
)
from .exchange_definitions.deribit import (
    fetch_deribit_futures,
    fetch_deribit_perpetual,
    parse_deribit_api,
)
from .exchange_definitions.extended import fetch_extended_perps, parse_extended
from .exchange_definitions.gate_io import (
    fetch_gateio_futures_usdt,
    fetch_gateio_perpetual_btc,
    fetch_gateio_perpetual_usdt,
    fetch_gate_io_spot,
    parse_gate_io,
    parse_gateio,
)
from .exchange_definitions.gemini import (
    fetch_gemini_perpetual,
    fetch_gemini_spot,
    parse_gemini_api,
)
from .exchange_definitions.grvt import fetch_grvt_perps, parse_grvt
from .exchange_definitions.huobi import (
    fetch_htx_futures_coin,
    fetch_htx_futures_usdt,
    fetch_htx_perpetual_coin,
    fetch_htx_perpetual_usdt,
    fetch_htx_spot,
    parse_htx_coin_futures,
    parse_htx_coin_swap,
    parse_htx_linear_swap,
    parse_htx_spot,
    parse_htx_usdt_futures,
)
from .exchange_definitions.hyperliquid import (
    fetch_hyperliquid_perps,
    fetch_hyperliquid_spot,
    parse_hyperliquid,
)
from .exchange_definitions.kraken import fetch_kraken_spot, parse_kraken_spot
from .exchange_definitions.kucoin import fetch_kucoin_spot, parse_kucoin_spot
from .exchange_definitions.lighter import fetch_lighter_perps, parse_lighter
from .exchange_definitions.mexc import fetch_mexc_perpetual, parse_mexc
from .exchange_definitions.okx import (
    fetch_okx_futures,
    fetch_okx_spot,
    fetch_okx_swap,
    parse_okex,
    parse_okex_futures,
    parse_okex_swap,
)
from .exchange_definitions.pacifica import fetch_pacifica_perps, parse_pacifica
from .exchange_definitions.phemex import (
    fetch_phemex_perpetual,
    fetch_phemex_spot,
    parse_phemex_api,
)
from .exchange_definitions.poloniex import fetch_poloniex_spot, parse_poloniex_api
from .exchange_definitions.upbit import fetch_upbit_spot, parse_upbit_api
from .exchange_definitions.whitebit import fetch_whitebit_perpetual, parse_whitebit
from .exchange_definitions.variational import fetch_variational_perps, parse_variational
from .parser_interface import Parser

ExchangeFetcher = Callable[[int], list[dict[str, str]]]


@dataclass(frozen=True)
class ExchangeDefinition:
    """A venue read from its own API.

    `parser` turns a row (fetched, or historical on file) into a `Contract`;
    `exchange_fetcher` lists the venue's current universe. `allowed_types` and
    `symbol_filter` keep a slug's file to its product: they also apply to the
    historical rows already on file.
    """

    parser: Parser
    exchange_fetcher: ExchangeFetcher
    symbol_filter: Callable[[dict], bool] | None = None
    allowed_types: set[str] | None = None


def _define(
    parser: Parser,
    *,
    exchange_fetcher: ExchangeFetcher,
    symbol_filter: Callable[[dict], bool] | None = None,
    allowed_types: set[str] | None = None,
) -> ExchangeDefinition:
    """Register an exchange, refreshed by the daily update from its API."""
    return ExchangeDefinition(
        parser=parser,
        exchange_fetcher=exchange_fetcher,
        symbol_filter=symbol_filter,
        allowed_types=allowed_types,
    )


def hyperliquid_filter(sd: dict) -> bool:
    sid = sd.get("id", "")
    return not re.match(r"^@\d+$", sid)


# live-producers venues. Their Atlas slug is the producer's exchange id, so a
# consumer joins a published record to its Atlas row without a mapping table.
# `bitget-spot`, `gateio-spot` and `coinbase-spot` are not streamed yet; they
# take the slug the producer's scheme gives them (`<venue>-spot`, as `okx-spot`).
_LIVE_PRODUCER_VENUES: dict[str, ExchangeDefinition] = {
    "gateio-spot": _define(
        parse_gate_io,
        exchange_fetcher=fetch_gate_io_spot,
        allowed_types={"spot"},
    ),
    "gateio-perps-usdt": _define(
        parse_gateio,
        exchange_fetcher=fetch_gateio_perpetual_usdt,
        allowed_types={"perpetual"},
    ),
    "gateio-futures-usdt": _define(
        parse_gateio,
        exchange_fetcher=fetch_gateio_futures_usdt,
        allowed_types={"future"},
    ),
    "gateio-perps-btc": _define(
        parse_gateio,
        exchange_fetcher=fetch_gateio_perpetual_btc,
        allowed_types={"perpetual"},
    ),
    "htx-perps-usdt": _define(
        parse_htx_linear_swap,
        exchange_fetcher=fetch_htx_perpetual_usdt,
        allowed_types={"perpetual"},
    ),
    "mexc-perps": _define(
        parse_mexc,
        exchange_fetcher=fetch_mexc_perpetual,
        allowed_types={"perpetual"},
    ),
    "coinbase-spot": _define(
        parse_coinbase,
        exchange_fetcher=fetch_coinbase_spot,
        allowed_types={"spot"},
    ),
    "bitget-spot": _define(
        parse_bitget,
        exchange_fetcher=fetch_bitget_spot,
        allowed_types={"spot"},
    ),
    "bitget-perps-usdt": _define(
        parse_bitget_v3,
        exchange_fetcher=fetch_bitget_perpetual_usdt,
        allowed_types={"perpetual"},
    ),
    "bitget-perps-usdc": _define(
        parse_bitget_v3,
        exchange_fetcher=fetch_bitget_perpetual_usdc,
        allowed_types={"perpetual"},
    ),
    # Bitget's v3 coin-margined symbols (`BTCUSD_CM`, `BTCCMZ26`) replaced the
    # pre-migration ones (`BTCUSD`), which are not on file.
    "bitget-perps-coin": _define(
        parse_bitget_v3,
        exchange_fetcher=fetch_bitget_perpetual_coin,
        allowed_types={"perpetual"},
    ),
    "bitget-futures-coin": _define(
        parse_bitget_v3,
        exchange_fetcher=fetch_bitget_futures_coin,
        allowed_types={"future"},
    ),
    "deribit-perps": _define(
        parse_deribit_api,
        exchange_fetcher=fetch_deribit_perpetual,
        allowed_types={"perpetual"},
    ),
    "deribit-futures": _define(
        parse_deribit_api,
        exchange_fetcher=fetch_deribit_futures,
        allowed_types={"future"},
    ),
    "whitebit-perps": _define(
        parse_whitebit,
        exchange_fetcher=fetch_whitebit_perpetual,
        allowed_types={"perpetual"},
    ),
    "cryptocom-perps": _define(
        parse_cryptocom_derivative,
        exchange_fetcher=fetch_cryptocom_perpetual,
        allowed_types={"perpetual"},
    ),
    "cryptocom-futures": _define(
        parse_cryptocom_derivative,
        exchange_fetcher=fetch_cryptocom_futures,
        allowed_types={"future"},
    ),
}


# Centralised venues outside the live producers, in the same slug scheme.
# BitMEX (closed 2026-09-23) and FTX have no API left and are not listed.
_API_VENUES: dict[str, ExchangeDefinition] = {
    "bitfinex-spot": _define(
        parse_bitfinex_api,
        exchange_fetcher=fetch_bitfinex_spot,
        allowed_types={"spot"},
    ),
    "bitfinex-perps": _define(
        parse_bitfinex_perpetual_api,
        exchange_fetcher=fetch_bitfinex_perpetual,
        allowed_types={"perpetual"},
    ),
    "bitstamp-spot": _define(
        parse_bitstamp_api,
        exchange_fetcher=fetch_bitstamp_spot,
        allowed_types={"spot"},
    ),
    "bitstamp-perps": _define(
        parse_bitstamp_api,
        exchange_fetcher=fetch_bitstamp_perpetual,
        allowed_types={"perpetual"},
    ),
    "cryptocom-spot": _define(
        parse_cryptocom_spot,
        exchange_fetcher=fetch_cryptocom_spot,
        allowed_types={"spot"},
    ),
    "gemini-spot": _define(
        parse_gemini_api,
        exchange_fetcher=fetch_gemini_spot,
        allowed_types={"spot"},
    ),
    "gemini-perps": _define(
        parse_gemini_api,
        exchange_fetcher=fetch_gemini_perpetual,
        allowed_types={"perpetual"},
    ),
    "htx-spot": _define(
        parse_htx_spot,
        exchange_fetcher=fetch_htx_spot,
        allowed_types={"spot"},
    ),
    "htx-perps-coin": _define(
        parse_htx_coin_swap,
        exchange_fetcher=fetch_htx_perpetual_coin,
        allowed_types={"perpetual"},
    ),
    # Dated contract codes (`BTC261225`), not HTX's rolling aliases (`BTC_CQ`),
    # which point at a new contract every week or quarter.
    "htx-futures-coin": _define(
        parse_htx_coin_futures,
        exchange_fetcher=fetch_htx_futures_coin,
        allowed_types={"future"},
    ),
    "htx-futures-usdt": _define(
        parse_htx_usdt_futures,
        exchange_fetcher=fetch_htx_futures_usdt,
        allowed_types={"future"},
    ),
    "kraken-spot": _define(
        parse_kraken_spot,
        exchange_fetcher=fetch_kraken_spot,
        allowed_types={"spot"},
    ),
    "kraken-perps": _define(
        parse_kraken_derivative,
        exchange_fetcher=fetch_kraken_perpetual,
        allowed_types={"perpetual"},
    ),
    "kraken-futures": _define(
        parse_kraken_derivative,
        exchange_fetcher=fetch_kraken_futures,
        allowed_types={"future"},
    ),
    "kucoin-spot": _define(
        parse_kucoin_spot,
        exchange_fetcher=fetch_kucoin_spot,
        allowed_types={"spot"},
    ),
    "phemex-spot": _define(
        parse_phemex_api,
        exchange_fetcher=fetch_phemex_spot,
        allowed_types={"spot"},
    ),
    "phemex-perps": _define(
        parse_phemex_api,
        exchange_fetcher=fetch_phemex_perpetual,
        allowed_types={"perpetual"},
    ),
    "poloniex-spot": _define(
        parse_poloniex_api,
        exchange_fetcher=fetch_poloniex_spot,
        allowed_types={"spot"},
    ),
    "upbit-spot": _define(
        parse_upbit_api,
        exchange_fetcher=fetch_upbit_spot,
        allowed_types={"spot"},
    ),
}


EXCHANGE_DEFINITIONS: dict[str, ExchangeDefinition] = {
    "apex-perps": _define(
        parse_apex,
        exchange_fetcher=fetch_apex_perps,
        allowed_types={"perpetual"},
    ),
    "aster-perps": _define(
        parse_aster,
        exchange_fetcher=fetch_aster_perps,
        allowed_types={"perpetual"},
    ),
    "binance-spot": _define(
        parse_binance,
        exchange_fetcher=fetch_binance_spot,
    ),
    "binance-futures": _define(
        parse_binance_futures,
        exchange_fetcher=fetch_binance_futures_usdm,
    ),
    "binance-futures-cm": _define(
        parse_binance_delivery,
        exchange_fetcher=fetch_binance_futures_coinm,
    ),
    "bybit-spot": _define(
        parse_bybit_spot,
        exchange_fetcher=fetch_bybit_spot,
        allowed_types={"spot"},
    ),
    "bybit-perps": _define(
        parse_bybit,
        exchange_fetcher=fetch_bybit_perps,
        allowed_types={"perpetual"},
    ),
    "bybit-futures": _define(
        parse_bybit,
        exchange_fetcher=fetch_bybit_futures,
        allowed_types={"future"},
    ),
    "grvt-perps": _define(
        parse_grvt,
        exchange_fetcher=fetch_grvt_perps,
        allowed_types={"perpetual"},
    ),
    "hyperliquid-spot": _define(
        parse_hyperliquid,
        exchange_fetcher=fetch_hyperliquid_spot,
        symbol_filter=hyperliquid_filter,
        allowed_types={"spot"},
    ),
    "hyperliquid-perps": _define(
        parse_hyperliquid,
        exchange_fetcher=fetch_hyperliquid_perps,
        symbol_filter=hyperliquid_filter,
        allowed_types={"perpetual"},
    ),
    "lighter-perps": _define(
        parse_lighter,
        exchange_fetcher=fetch_lighter_perps,
        allowed_types={"perpetual"},
    ),
    "okx-spot": _define(
        parse_okex, exchange_fetcher=fetch_okx_spot
    ),
    "okx-perps": _define(
        parse_okex_swap,
        exchange_fetcher=fetch_okx_swap,
    ),
    "okx-futures": _define(
        parse_okex_futures,
        exchange_fetcher=fetch_okx_futures,
    ),
    "extended-perps": _define(
        parse_extended,
        exchange_fetcher=fetch_extended_perps,
        allowed_types={"perpetual"},
    ),
    # Oracle-priced RWA perp venue.
    "avantis-perps": _define(
        parse_avantis,
        exchange_fetcher=fetch_avantis_perps,
        allowed_types={"perpetual"},
    ),
    # DeFi perpetual venues.
    "pacifica-perps": _define(
        parse_pacifica,
        exchange_fetcher=fetch_pacifica_perps,
        allowed_types={"perpetual"},
    ),
    **_LIVE_PRODUCER_VENUES,
    **_API_VENUES,
    "variational-perps": _define(
        parse_variational,
        exchange_fetcher=fetch_variational_perps,
        allowed_types={"perpetual"},
    ),
}

PARSERS: dict[str, Parser] = {
    exchange: definition.parser for exchange, definition in EXCHANGE_DEFINITIONS.items()
}


def get_exchange_definition(exchange: str) -> ExchangeDefinition | None:
    return EXCHANGE_DEFINITIONS.get(exchange)


def get_exchange_fetcher(exchange: str) -> ExchangeFetcher | None:
    definition = get_exchange_definition(exchange)
    if definition is None:
        return None
    return definition.exchange_fetcher


def get_symbol_filter(exchange: str) -> Callable[[dict], bool] | None:
    definition = get_exchange_definition(exchange)
    if definition is None:
        return None
    return definition.symbol_filter


def get_allowed_types(exchange: str) -> set[str] | None:
    definition = get_exchange_definition(exchange)
    if definition is None:
        return None
    return definition.allowed_types
