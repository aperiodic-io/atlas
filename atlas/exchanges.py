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
from .exchange_definitions.bitfinex import parse_bitfinex, parse_bitfinex_derivatives
from .exchange_definitions.bitget import (
    fetch_bitget_futures_coin,
    fetch_bitget_perpetual_coin,
    fetch_bitget_perpetual_usdc,
    fetch_bitget_perpetual_usdt,
    parse_bitget,
    parse_bitget_futures,
    parse_bitget_v3,
)
from .exchange_definitions.bitmex import parse_bitmex
from .exchange_definitions.bitstamp import parse_bitstamp
from .exchange_definitions.bybit import (
    fetch_bybit_futures,
    fetch_bybit_perps,
    fetch_bybit_spot,
    parse_bybit,
    parse_bybit_spot,
)
from .exchange_definitions.coinbase import parse_coinbase
from .exchange_definitions.crypto_com import (
    fetch_cryptocom_futures,
    fetch_cryptocom_perpetual,
    parse_crypto_com,
    parse_cryptocom_derivative,
)
from .exchange_definitions.cryptofacilities import parse_cryptofacilities
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
    parse_gate_io,
    parse_gate_io_futures,
    parse_gateio,
)
from .exchange_definitions.gemini import parse_gemini
from .exchange_definitions.grvt import fetch_grvt_perps, parse_grvt
from .exchange_definitions.gtrade import fetch_gtrade_perps, parse_gtrade
from .exchange_definitions.huobi import (
    fetch_htx_perpetual_usdt,
    parse_htx_linear_swap,
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
from .exchange_definitions.kraken import parse_kraken
from .exchange_definitions.kucoin import parse_kucoin
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
from .exchange_definitions.ostium import fetch_ostium_perps, parse_ostium
from .exchange_definitions.pacifica import fetch_pacifica_perps, parse_pacifica
from .exchange_definitions.phemex import parse_phemex
from .exchange_definitions.poloniex import parse_poloniex
from .exchange_definitions.upbit import parse_upbit
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
_LIVE_PRODUCER_VENUES: dict[str, ExchangeDefinition] = {
    "gateio-perpetual-usdt": _define(
        "gateio-perpetual-usdt",
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
    "gateio-perpetual-btc": _define(
        "gateio-perpetual-btc",
        parse_gateio,
        tardis_id="gate-io-futures",
        exchange_fetcher=fetch_gateio_perpetual_btc,
        symbol_filter=_id_suffix_filter("_USD"),
        allowed_types={"perpetual"},
    ),
    "htx-perpetual-usdt": _define(
        "htx-perpetual-usdt",
        parse_htx_linear_swap,
        tardis_id="huobi-dm-linear-swap",
        exchange_fetcher=fetch_htx_perpetual_usdt,
        symbol_filter=_id_suffix_filter("-USDT"),
        allowed_types={"perpetual"},
    ),
    # Tardis's `mexc` is MEXC spot; it has no MEXC futures.
    "mexc-perpetual": _define(
        "mexc-perpetual",
        parse_mexc,
        on_tardis=False,
        exchange_fetcher=fetch_mexc_perpetual,
        allowed_types={"perpetual"},
    ),
    "bitget-perpetual-usdt": _define(
        "bitget-perpetual-usdt",
        parse_bitget_v3,
        tardis_id="bitget-futures",
        exchange_fetcher=fetch_bitget_perpetual_usdt,
        symbol_filter=_id_suffix_filter("USDT"),
        allowed_types={"perpetual"},
    ),
    "bitget-perpetual-usdc": _define(
        "bitget-perpetual-usdc",
        parse_bitget_v3,
        tardis_id="bitget-futures",
        exchange_fetcher=fetch_bitget_perpetual_usdc,
        symbol_filter=_id_suffix_filter("PERP"),
        allowed_types={"perpetual"},
    ),
    # Tardis lists Bitget's pre-migration coin-margined symbols (`BTCUSD`),
    # not the v3 ones the venue trades now (`BTCUSD_CM`, `BTCCMZ26`).
    "bitget-perpetual-coin": _define(
        "bitget-perpetual-coin",
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
    "deribit-perpetual": _define(
        "deribit-perpetual",
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
    "whitebit-perpetual": _define(
        "whitebit-perpetual",
        parse_whitebit,
        on_tardis=False,
        exchange_fetcher=fetch_whitebit_perpetual,
        allowed_types={"perpetual"},
    ),
    "cryptocom-perpetual": _define(
        "cryptocom-perpetual",
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


EXCHANGE_DEFINITIONS: dict[str, ExchangeDefinition] = {
    "apex-perpetual": _define(
        "apex-perpetual",
        parse_apex,
        exchange_fetcher=fetch_apex_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    "aster-perpetual": _define(
        "aster-perpetual",
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
    "grvt-perpetual": _define(
        "grvt-perpetual",
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
    "lighter-perpetual": _define(
        "lighter-perpetual",
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
    "extended-perpetual": _define(
        "extended-perpetual",
        parse_extended,
        exchange_fetcher=fetch_extended_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    # Oracle-priced RWA perp venues: no Tardis coverage, so the exchange API is
    # the only source and the daily update must refresh them (on_tardis=False).
    "ostium-perpetual": _define(
        "ostium-perpetual",
        parse_ostium,
        exchange_fetcher=fetch_ostium_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    "avantis-perpetual": _define(
        "avantis-perpetual",
        parse_avantis,
        exchange_fetcher=fetch_avantis_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    "gtrade-perpetual": _define(
        "gtrade-perpetual",
        parse_gtrade,
        exchange_fetcher=fetch_gtrade_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    "phemex": _define("phemex", parse_phemex),
    "poloniex": _define("poloniex", parse_poloniex),
    "upbit": _define("upbit", parse_upbit),
    # DeFi perpetual venues: listed from their own APIs only (Tardis has none).
    "pacifica-perpetual": _define(
        "pacifica-perpetual",
        parse_pacifica,
        exchange_fetcher=fetch_pacifica_perps,
        allowed_types={"perpetual"},
        on_tardis=False,
    ),
    **_LIVE_PRODUCER_VENUES,
    "variational-perpetual": _define(
        "variational-perpetual",
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
