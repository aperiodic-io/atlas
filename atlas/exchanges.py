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
    parse_bitfinex,
    parse_bitfinex_api,
    parse_bitfinex_derivatives,
    parse_bitfinex_perpetual_api,
)
from .exchange_definitions.bitget import (
    fetch_bitget_futures_coin,
    fetch_bitget_perpetual_coin,
    fetch_bitget_perpetual_usdc,
    fetch_bitget_perpetual_usdt,
    fetch_bitget_spot,
    parse_bitget,
    parse_bitget_futures,
    parse_bitget_v3,
)
from .exchange_definitions.bitmex import parse_bitmex
from .exchange_definitions.bitstamp import (
    fetch_bitstamp_perpetual,
    fetch_bitstamp_spot,
    parse_bitstamp,
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
    parse_crypto_com,
    parse_cryptocom_derivative,
    parse_cryptocom_spot,
)
from .exchange_definitions.cryptofacilities import (
    fetch_kraken_futures,
    fetch_kraken_perpetual,
    parse_cryptofacilities,
    parse_kraken_derivative,
)
from .exchange_definitions.deribit import (
    fetch_deribit_futures,
    fetch_deribit_perpetual,
    parse_deribit,
    parse_deribit_api,
)
from .exchange_definitions.extended import fetch_extended_perps, parse_extended
from .exchange_definitions.ftx import parse_ftx
from .exchange_definitions.gate_io import (
    fetch_gateio_futures_usdt,
    fetch_gateio_perpetual_btc,
    fetch_gateio_perpetual_usdt,
    fetch_gate_io_spot,
    parse_gate_io,
    parse_gate_io_futures,
    parse_gateio,
)
from .exchange_definitions.gemini import (
    fetch_gemini_perpetual,
    fetch_gemini_spot,
    parse_gemini,
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
    parse_huobi,
    parse_huobi_dm,
    parse_huobi_dm_linear_swap,
    parse_huobi_dm_swap,
)
from .exchange_definitions.hyperliquid import (
    fetch_hyperliquid_perps,
    fetch_hyperliquid_spot,
    parse_hyperliquid,
)
from .exchange_definitions.kraken import fetch_kraken_spot, parse_kraken, parse_kraken_spot
from .exchange_definitions.kucoin import fetch_kucoin_spot, parse_kucoin, parse_kucoin_spot
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
    parse_phemex,
    parse_phemex_api,
)
from .exchange_definitions.poloniex import fetch_poloniex_spot, parse_poloniex, parse_poloniex_api
from .exchange_definitions.upbit import fetch_upbit_spot, parse_upbit, parse_upbit_api
from .exchange_definitions.whitebit import fetch_whitebit_perpetual, parse_whitebit
from .exchange_definitions.variational import fetch_variational_perps, parse_variational
from .parser_interface import Parser

ExchangeFetcher = Callable[[int], list[dict[str, str]]]


@dataclass(frozen=True)
class ExchangeDefinition:
    parser: Parser
    beta: bool
    # None when Tardis does not cover the venue: a Tardis-only refresh must
    # then fail rather than read another product's feed or write nothing.
    tardis_id: str | None
    exchange_fetcher: ExchangeFetcher | None = None
    symbol_filter: Callable[[dict], bool] | None = None
    allowed_types: set[str] | None = None
    # Whether Tardis lists the exchange. One it does not (a DeFi venue read only
    # from its own API) is skipped by the Tardis source rather than refreshed
    # from an empty listing, and its rows get Atlas's first observation as
    # their availability start.
    on_tardis: bool = True


def _define(
    exchange: str,
    parser: Parser,
    *,
    tardis_id: str | None = None,
    exchange_fetcher: ExchangeFetcher | None = None,
    symbol_filter: Callable[[dict], bool] | None = None,
    allowed_types: set[str] | None = None,
    on_tardis: bool = True,
) -> ExchangeDefinition:
    """Register an exchange.

    An exchange is stable (refreshed by the daily update) exactly when it has
    its own exchange-API fetcher; parser-only exchanges depend on Tardis
    alone and stay beta. ``on_tardis=False`` marks a venue Tardis does not list
    under any id.
    """
    return ExchangeDefinition(
        parser=parser,
        beta=exchange_fetcher is None,
        tardis_id=tardis_id or exchange,
        exchange_fetcher=exchange_fetcher,
        symbol_filter=symbol_filter,
        allowed_types=allowed_types,
        on_tardis=on_tardis,
    )


def hyperliquid_filter(sd: dict) -> bool:
    sid = sd.get("id", "")
    return not re.match(r"^@\d+$", sid)


def _id_suffix_filter(suffix: str) -> Callable[[dict], bool]:
    """Keep the rows of one settlement family of a shared Tardis feed.

    Several live-producers venues read one Tardis exchange: Gate's USDT- and
    BTC-settled perpetuals are both `gate-io-futures` (`BTC_USDT`, `BTC_USD`),
    Bitget's USDT and USDC perpetuals are both `bitget-futures` (`BTCUSDT`,
    `BTCPERP`).
    """

    def matches(sd: dict) -> bool:
        return str(sd.get("id", "")).endswith(suffix)

    return matches


# live-producers venues. Their Atlas slug is the producer's exchange id, so a
# consumer joins a published record to its Atlas row without a mapping table.
# `bitget-spot`, `gateio-spot` and `coinbase-spot` are not streamed yet; they
# take the slug the producer's scheme gives them (`<venue>-spot`, as `okx-spot`).
_LIVE_PRODUCER_VENUES: dict[str, ExchangeDefinition] = {
    "gateio-spot": _define(
        "gateio-spot",
        parse_gate_io,
        tardis_id="gate-io",
        exchange_fetcher=fetch_gate_io_spot,
        allowed_types={"spot"},
    ),
    "gateio-perps-usdt": _define(
        "gateio-perps-usdt",
        parse_gateio,
        tardis_id="gate-io-futures",
        exchange_fetcher=fetch_gateio_perpetual_usdt,
        symbol_filter=_id_suffix_filter("_USDT"),
        allowed_types={"perpetual"},
    ),
    "gateio-futures-usdt": _define(
        "gateio-futures-usdt",
        parse_gateio,
        on_tardis=False,
        exchange_fetcher=fetch_gateio_futures_usdt,
        allowed_types={"future"},
    ),
    "gateio-perps-btc": _define(
        "gateio-perps-btc",
        parse_gateio,
        tardis_id="gate-io-futures",
        exchange_fetcher=fetch_gateio_perpetual_btc,
        symbol_filter=_id_suffix_filter("_USD"),
        allowed_types={"perpetual"},
    ),
    "htx-perps-usdt": _define(
        "htx-perps-usdt",
        parse_htx_linear_swap,
        tardis_id="huobi-dm-linear-swap",
        exchange_fetcher=fetch_htx_perpetual_usdt,
        symbol_filter=_id_suffix_filter("-USDT"),
        allowed_types={"perpetual"},
    ),
    # Tardis's `mexc` is MEXC spot; it has no MEXC futures.
    "mexc-perps": _define(
        "mexc-perps",
        parse_mexc,
        on_tardis=False,
        exchange_fetcher=fetch_mexc_perpetual,
        allowed_types={"perpetual"},
    ),
    "coinbase-spot": _define(
        "coinbase-spot",
        parse_coinbase,
        tardis_id="coinbase",
        exchange_fetcher=fetch_coinbase_spot,
        allowed_types={"spot"},
    ),
    "bitget-spot": _define(
        "bitget-spot",
        parse_bitget,
        tardis_id="bitget",
        exchange_fetcher=fetch_bitget_spot,
        allowed_types={"spot"},
    ),
    "bitget-perps-usdt": _define(
        "bitget-perps-usdt",
        parse_bitget_v3,
        tardis_id="bitget-futures",
        exchange_fetcher=fetch_bitget_perpetual_usdt,
        symbol_filter=_id_suffix_filter("USDT"),
        allowed_types={"perpetual"},
    ),
    "bitget-perps-usdc": _define(
        "bitget-perps-usdc",
        parse_bitget_v3,
        tardis_id="bitget-futures",
        exchange_fetcher=fetch_bitget_perpetual_usdc,
        symbol_filter=_id_suffix_filter("PERP"),
        allowed_types={"perpetual"},
    ),
    # Tardis lists Bitget's pre-migration coin-margined symbols (`BTCUSD`),
    # not the v3 ones the venue trades now (`BTCUSD_CM`, `BTCCMZ26`).
    "bitget-perps-coin": _define(
        "bitget-perps-coin",
        parse_bitget_v3,
        on_tardis=False,
        exchange_fetcher=fetch_bitget_perpetual_coin,
        allowed_types={"perpetual"},
    ),
    "bitget-futures-coin": _define(
        "bitget-futures-coin",
        parse_bitget_v3,
        on_tardis=False,
        exchange_fetcher=fetch_bitget_futures_coin,
        allowed_types={"future"},
    ),
    "deribit-perps": _define(
        "deribit-perps",
        parse_deribit_api,
        tardis_id="deribit",
        exchange_fetcher=fetch_deribit_perpetual,
        allowed_types={"perpetual"},
    ),
    "deribit-futures": _define(
        "deribit-futures",
        parse_deribit_api,
        tardis_id="deribit",
        exchange_fetcher=fetch_deribit_futures,
        allowed_types={"future"},
    ),
    "whitebit-perps": _define(
        "whitebit-perps",
        parse_whitebit,
        on_tardis=False,
        exchange_fetcher=fetch_whitebit_perpetual,
        allowed_types={"perpetual"},
    ),
    "cryptocom-perps": _define(
        "cryptocom-perps",
        parse_cryptocom_derivative,
        tardis_id="crypto-com",
        exchange_fetcher=fetch_cryptocom_perpetual,
        allowed_types={"perpetual"},
    ),
    "cryptocom-futures": _define(
        "cryptocom-futures",
        parse_cryptocom_derivative,
        tardis_id="crypto-com",
        exchange_fetcher=fetch_cryptocom_futures,
        allowed_types={"future"},
    ),
}


# Venues that were Tardis-only parsers, now read from their own APIs. Their
# Tardis-named exchanges (`kraken`, `gemini`, `huobi`, ...) stay beta parsers;
# BitMEX (closed 2026-09-23) and FTX have no API left and stay beta only.
_API_VENUES: dict[str, ExchangeDefinition] = {
    "bitfinex-spot": _define(
        "bitfinex-spot",
        parse_bitfinex_api,
        tardis_id="bitfinex",
        exchange_fetcher=fetch_bitfinex_spot,
        allowed_types={"spot"},
    ),
    "bitfinex-perps": _define(
        "bitfinex-perps",
        parse_bitfinex_perpetual_api,
        tardis_id="bitfinex-derivatives",
        exchange_fetcher=fetch_bitfinex_perpetual,
        allowed_types={"perpetual"},
    ),
    "bitstamp-spot": _define(
        "bitstamp-spot",
        parse_bitstamp_api,
        tardis_id="bitstamp",
        exchange_fetcher=fetch_bitstamp_spot,
        allowed_types={"spot"},
    ),
    "bitstamp-perps": _define(
        "bitstamp-perps",
        parse_bitstamp_api,
        tardis_id="bitstamp",
        exchange_fetcher=fetch_bitstamp_perpetual,
        allowed_types={"perpetual"},
    ),
    "cryptocom-spot": _define(
        "cryptocom-spot",
        parse_cryptocom_spot,
        tardis_id="crypto-com",
        exchange_fetcher=fetch_cryptocom_spot,
        allowed_types={"spot"},
    ),
    "gemini-spot": _define(
        "gemini-spot",
        parse_gemini_api,
        tardis_id="gemini",
        exchange_fetcher=fetch_gemini_spot,
        allowed_types={"spot"},
    ),
    "gemini-perps": _define(
        "gemini-perps",
        parse_gemini_api,
        tardis_id="gemini",
        exchange_fetcher=fetch_gemini_perpetual,
        allowed_types={"perpetual"},
    ),
    "htx-spot": _define(
        "htx-spot",
        parse_htx_spot,
        tardis_id="huobi",
        exchange_fetcher=fetch_htx_spot,
        allowed_types={"spot"},
    ),
    "htx-perps-coin": _define(
        "htx-perps-coin",
        parse_htx_coin_swap,
        tardis_id="huobi-dm-swap",
        exchange_fetcher=fetch_htx_perpetual_coin,
        allowed_types={"perpetual"},
    ),
    # Tardis's `huobi-dm` lists only rolling aliases (`BTC_CQ`), which point at
    # a new contract every week or quarter; the venue's ids are dated.
    "htx-futures-coin": _define(
        "htx-futures-coin",
        parse_htx_coin_futures,
        on_tardis=False,
        exchange_fetcher=fetch_htx_futures_coin,
        allowed_types={"future"},
    ),
    "htx-futures-usdt": _define(
        "htx-futures-usdt",
        parse_htx_usdt_futures,
        tardis_id="huobi-dm-linear-swap",
        exchange_fetcher=fetch_htx_futures_usdt,
        allowed_types={"future"},
    ),
    "kraken-spot": _define(
        "kraken-spot",
        parse_kraken_spot,
        tardis_id="kraken",
        exchange_fetcher=fetch_kraken_spot,
        allowed_types={"spot"},
    ),
    "kraken-perps": _define(
        "kraken-perps",
        parse_kraken_derivative,
        tardis_id="cryptofacilities",
        exchange_fetcher=fetch_kraken_perpetual,
        allowed_types={"perpetual"},
    ),
    "kraken-futures": _define(
        "kraken-futures",
        parse_kraken_derivative,
        tardis_id="cryptofacilities",
        exchange_fetcher=fetch_kraken_futures,
        allowed_types={"future"},
    ),
    "kucoin-spot": _define(
        "kucoin-spot",
        parse_kucoin_spot,
        tardis_id="kucoin",
        exchange_fetcher=fetch_kucoin_spot,
        allowed_types={"spot"},
    ),
    "phemex-spot": _define(
        "phemex-spot",
        parse_phemex_api,
        tardis_id="phemex",
        exchange_fetcher=fetch_phemex_spot,
        allowed_types={"spot"},
    ),
    "phemex-perps": _define(
        "phemex-perps",
        parse_phemex_api,
        tardis_id="phemex",
        exchange_fetcher=fetch_phemex_perpetual,
        allowed_types={"perpetual"},
    ),
    "poloniex-spot": _define(
        "poloniex-spot",
        parse_poloniex_api,
        tardis_id="poloniex",
        exchange_fetcher=fetch_poloniex_spot,
        allowed_types={"spot"},
    ),
    "upbit-spot": _define(
        "upbit-spot",
        parse_upbit_api,
        tardis_id="upbit",
        exchange_fetcher=fetch_upbit_spot,
        allowed_types={"spot"},
    ),
}


EXCHANGE_DEFINITIONS: dict[str, ExchangeDefinition] = {
    "apex-perps": _define(
        "apex-perps",
        parse_apex,
        exchange_fetcher=fetch_apex_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    "aster-perps": _define(
        "aster-perps",
        parse_aster,
        tardis_id="aster-futures",
        exchange_fetcher=fetch_aster_perps,
        allowed_types={"perpetual"},
    ),
    "binance-spot": _define(
        "binance-spot",
        parse_binance,
        tardis_id="binance",
        exchange_fetcher=fetch_binance_spot,
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
    "bitmex": _define("bitmex", parse_bitmex),
    "bitfinex": _define("bitfinex", parse_bitfinex),
    "bitfinex-derivatives": _define("bitfinex-derivatives", parse_bitfinex_derivatives),
    "bitget": _define("bitget", parse_bitget),
    "bitget-futures": _define("bitget-futures", parse_bitget_futures),
    "bitstamp": _define("bitstamp", parse_bitstamp),
    "bybit-spot": _define(
        "bybit-spot",
        parse_bybit_spot,
        tardis_id="bybit-spot",
        exchange_fetcher=fetch_bybit_spot,
        allowed_types={"spot"},
    ),
    "bybit-perps": _define(
        "bybit-perps",
        parse_bybit,
        tardis_id="bybit",
        exchange_fetcher=fetch_bybit_perps,
        allowed_types={"perpetual"},
    ),
    "bybit-futures": _define(
        "bybit-futures",
        parse_bybit,
        tardis_id="bybit",
        exchange_fetcher=fetch_bybit_futures,
        allowed_types={"future"},
    ),
    "coinbase": _define("coinbase", parse_coinbase),
    "crypto-com": _define("crypto-com", parse_crypto_com),
    "cryptofacilities": _define("cryptofacilities", parse_cryptofacilities),
    "deribit": _define("deribit", parse_deribit),
    "ftx": _define("ftx", parse_ftx),
    "gate-io": _define("gate-io", parse_gate_io),
    "gate-io-futures": _define("gate-io-futures", parse_gate_io_futures),
    "gemini": _define("gemini", parse_gemini),
    "grvt-perps": _define(
        "grvt-perps",
        parse_grvt,
        exchange_fetcher=fetch_grvt_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    "huobi": _define("huobi", parse_huobi),
    "huobi-dm": _define("huobi-dm", parse_huobi_dm),
    "huobi-dm-swap": _define("huobi-dm-swap", parse_huobi_dm_swap),
    "huobi-dm-linear-swap": _define("huobi-dm-linear-swap", parse_huobi_dm_linear_swap),
    "hyperliquid-spot": _define(
        "hyperliquid-spot",
        parse_hyperliquid,
        tardis_id="hyperliquid",
        exchange_fetcher=fetch_hyperliquid_spot,
        symbol_filter=hyperliquid_filter,
        allowed_types={"spot"},
    ),
    "hyperliquid-perps": _define(
        "hyperliquid-perps",
        parse_hyperliquid,
        tardis_id="hyperliquid",
        exchange_fetcher=fetch_hyperliquid_perps,
        symbol_filter=hyperliquid_filter,
        allowed_types={"perpetual"},
    ),
    "kraken": _define("kraken", parse_kraken),
    "lighter-perps": _define(
        "lighter-perps",
        parse_lighter,
        tardis_id="lighter",
        exchange_fetcher=fetch_lighter_perps,
        allowed_types={"perpetual"},
    ),
    "kucoin": _define("kucoin", parse_kucoin),
    "okx-spot": _define(
        "okx-spot", parse_okex, tardis_id="okex", exchange_fetcher=fetch_okx_spot
    ),
    "okx-perps": _define(
        "okx-perps",
        parse_okex_swap,
        tardis_id="okex-swap",
        exchange_fetcher=fetch_okx_swap,
    ),
    "okx-futures": _define(
        "okx-futures",
        parse_okex_futures,
        tardis_id="okex-futures",
        exchange_fetcher=fetch_okx_futures,
    ),
    "extended-perps": _define(
        "extended-perps",
        parse_extended,
        exchange_fetcher=fetch_extended_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    # Oracle-priced RWA perp venue: no Tardis coverage, so the exchange API is
    # the only source and the daily update must refresh it (on_tardis=False).
    "avantis-perps": _define(
        "avantis-perps",
        parse_avantis,
        exchange_fetcher=fetch_avantis_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    "phemex": _define("phemex", parse_phemex),
    "poloniex": _define("poloniex", parse_poloniex),
    "upbit": _define("upbit", parse_upbit),
    # DeFi perpetual venues: listed from their own APIs only (Tardis has none).
    "pacifica-perps": _define(
        "pacifica-perps",
        parse_pacifica,
        exchange_fetcher=fetch_pacifica_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    **_LIVE_PRODUCER_VENUES,
    **_API_VENUES,
    "variational-perps": _define(
        "variational-perps",
        parse_variational,
        exchange_fetcher=fetch_variational_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
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


def is_on_tardis(exchange: str) -> bool:
    definition = get_exchange_definition(exchange)
    if definition is None:
        return True
    return definition.on_tardis


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
