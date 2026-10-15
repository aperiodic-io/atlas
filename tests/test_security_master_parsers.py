import json
from datetime import datetime
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch


from atlas.contracts import ContractType
from atlas.exchange_definitions import is_beta_exchange
from atlas.parsers import SkipSymbol, parse_contract
from atlas.types import UnderlyingType


def _sd(id: str, type: str, **extra: object) -> dict:
    return {"id": id, "type": type, **extra}


def _parse(exchange: str, id: str, type: str):
    return parse_contract(exchange, _sd(id, type))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_unknown_exchange_raises(self):
        with pytest.raises(ValueError, match="Unknown exchange"):
            parse_contract("not-an-exchange", _sd("BTCUSDT", "spot"))

    def test_unknown_type_gives_unknown_contract_type(self):
        c = _parse("binance-spot", "BTCUSDT", "mystery")
        assert c is not None
        assert c.instrument_type == ContractType.unknown

    def test_missing_id_key_raises(self):
        with pytest.raises(KeyError):
            parse_contract("binance-spot", {})

    def test_beta_exchange_classification(self):
        assert is_beta_exchange("binance-spot") is False
        assert is_beta_exchange("okx-spot") is False
        assert is_beta_exchange("coinbase") is True

    @pytest.mark.parametrize(
        "exchange",
        [
            "gateio-perpetual-usdt",
            "gateio-futures-usdt",
            "gateio-perpetual-btc",
            "htx-perpetual-usdt",
            "mexc-perpetual",
            "bitget-perpetual-usdt",
            "bitget-perpetual-usdc",
            "bitget-perpetual-coin",
            "bitget-futures-coin",
            "deribit-perpetual",
            "deribit-futures",
            "whitebit-perpetual",
            "cryptocom-perpetual",
            "cryptocom-futures",
        ],
    )
    def test_exchanges_with_api_fetchers_are_stable(self, exchange):
        # The daily update skips beta exchanges, so a venue with its own
        # fetcher must not be beta or it would never get a snapshot.
        assert is_beta_exchange(exchange) is False


# ---------------------------------------------------------------------------
# Spot exchanges
# ---------------------------------------------------------------------------


class TestBinanceSpot:
    def test_btcusdt(self):
        c = _parse("binance-spot", "BTCUSDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot
        assert c.delivery_date is None

    def test_ethbtc(self):
        c = _parse("binance-spot", "ETHBTC", "spot")
        assert c.symbol == "ETH"
        assert c.denominator == "BTC"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot

    def test_bnbbusd(self):
        c = _parse("binance-spot", "BNBBUSD", "spot")
        assert c.symbol == "BNB"
        assert c.denominator == "BUSD"

    def test_trxxrp_alias_binance_spot(self):
        c = _parse("binance-spot", "trxxrp", "spot")
        assert c.symbol == "TRX"
        assert c.denominator == "XRP"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot

    def test_wintrx_alias_binance_spot(self):
        c = _parse("binance-spot", "wintrx", "spot")
        assert c.symbol == "WIN"
        assert c.denominator == "TRX"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot

    def test_btcpln_alias_binance_spot(self):
        c = _parse("binance-spot", "btcpln", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "PLN"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot

    def test_usdtars_alias_binance_spot(self):
        c = _parse("binance-spot", "usdtars", "spot")
        assert c.symbol == "USDT"
        assert c.denominator == "ARS"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot

    def test_bnbjpy_alias_binance_spot(self):
        c = _parse("binance-spot", "bnbjpy", "spot")
        assert c.symbol == "BNB"
        assert c.denominator == "JPY"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot

    def test_unrecognised_quote_raises(self):
        with pytest.raises(SkipSymbol):
            _parse("binance-spot", "ABCXYZ", "spot")

    def test_alias_binance_spot(self):
        c = _parse("binance-spot", "BTCUSDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.instrument_type == ContractType.spot


class TestCoinbase:
    def test_btc_usd(self):
        c = _parse("coinbase", "BTC-USD", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot

    def test_eth_usdc(self):
        c = _parse("coinbase", "ETH-USDC", "spot")
        assert c.symbol == "ETH"
        assert c.denominator == "USDC"
        assert c.margin is None

    def test_eth_btc(self):
        c = _parse("coinbase", "ETH-BTC", "spot")
        assert c.symbol == "ETH"
        assert c.denominator == "BTC"
        assert c.margin is None


class TestKraken:
    def test_legacy_8char(self):
        c = _parse("kraken", "XXBTZUSD", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin is None

    def test_slash_format(self):
        c = _parse("kraken", "XBT/USD", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"

    def test_modern_concat(self):
        c = _parse("kraken", "BTCUSDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"

    def test_xeth_xxbt(self):
        c = _parse("kraken", "XETHXXBT", "spot")
        assert c.symbol == "ETH"
        assert c.denominator == "BTC"


class TestUpbit:
    def test_krw_btc(self):
        # Upbit format is QUOTE-BASE
        c = _parse("upbit", "KRW-BTC", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "KRW"
        assert c.margin is None

    def test_btc_eth(self):
        c = _parse("upbit", "BTC-ETH", "spot")
        assert c.symbol == "ETH"
        assert c.denominator == "BTC"


class TestBitstamp:
    def test_lowercase(self):
        c = _parse("bitstamp", "btcusd", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin is None

    def test_lowercase_with_underscore(self):
        c = _parse("bitstamp", "btc_usd", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"


class TestPoloniex:
    def test_new_format_base_quote(self):
        c = _parse("poloniex", "BTC_USDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"

    def test_old_format_quote_base(self):
        # Old Poloniex: QUOTE_BASE where base is not a known quote asset
        c = _parse("poloniex", "USDT_LINK", "spot")
        assert c.symbol == "LINK"
        assert c.denominator == "USDT"


class TestBitfinex:
    def test_t_prefix_usd(self):
        c = _parse("bitfinex", "tBTCUSD", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"

    def test_ust_normalised_to_usdt(self):
        c = _parse("bitfinex", "tBTCUST", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"

    def test_colon_separated(self):
        c = _parse("bitfinex", "tBTC:USD", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"

    def test_funding_skipped(self):
        with pytest.raises(SkipSymbol):
            _parse("bitfinex", "fBTC", "spot")

    def test_no_t_prefix_skipped(self):
        with pytest.raises(SkipSymbol):
            _parse("bitfinex", "BTCUSD", "spot")


class TestGemini:
    def test_lowercase(self):
        c = _parse("gemini", "btcusd", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"

    def test_gusd(self):
        c = _parse("gemini", "btcgusd", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "GUSD"


class TestKucoin:
    def test_btc_usdt(self):
        c = _parse("kucoin", "BTC-USDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.instrument_type == ContractType.spot


class TestOkxSpot:
    def test_btc_usdt(self):
        c = _parse("okx-spot", "BTC-USDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot


class TestGateIo:
    def test_spot(self):
        c = _parse("gate-io", "BTC_USDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin is None


# ---------------------------------------------------------------------------
# Perpetuals
# ---------------------------------------------------------------------------


class TestBinanceFuturesPerp:
    def test_linear_perp(self):
        c = _parse("binance-futures", "BTCUSDT", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"
        assert c.instrument_type == ContractType.perpetual

    def test_inverse_perp(self):
        c = _parse("binance-futures-cm", "BTCUSD_PERP", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.perpetual

    def test_eth_inverse_perp(self):
        c = _parse("binance-futures", "ETHUSD_PERP", "perpetual")
        assert c.symbol == "ETH"
        assert c.margin == "ETH"


class TestBinanceFuturesMarginAsset:
    """Binance reports the margin asset per symbol; prefer it over the
    denominator whitelist in ``resolve_margin``, which cannot know which
    quote assets are stablecoins and misreads new ones as inverse."""

    def test_usd1_margined_perp_is_linear(self):
        c = parse_contract(
            "binance-futures",
            _sd("BTCUSD1", "perpetual", margin_asset="USD1"),
        )
        assert c.symbol == "BTC"
        assert c.denominator == "USD1"
        assert c.margin == "USD1"
        assert c.internal_id == "perpetual-BTC-USD1:USD1"

    def test_u_margined_perp_is_linear(self):
        c = parse_contract(
            "binance-futures",
            _sd("BTCU", "perpetual", margin_asset="U"),
        )
        assert c.internal_id == "perpetual-BTC-U:U"

    def test_btc_quoted_perp_is_linear_in_btc(self):
        c = parse_contract(
            "binance-futures",
            _sd("ETHBTC", "perpetual", margin_asset="BTC"),
        )
        assert c.symbol == "ETH"
        assert c.denominator == "BTC"
        assert c.margin == "BTC"
        assert c.internal_id == "perpetual-ETH-BTC:BTC"

    def test_inverse_perp_keeps_base_margin(self):
        c = parse_contract(
            "binance-futures-cm",
            _sd("BTCUSD_PERP", "perpetual", margin_asset="BTC"),
        )
        assert c.internal_id == "perpetual-BTC-USD:BTC"

    def test_dated_future_uses_reported_margin(self):
        c = parse_contract(
            "binance-futures",
            _sd("BTCUSDT_260327", "future", margin_asset="USDT"),
        )
        assert c.internal_id == "future-BTC-USDT:USDT-20260327"

    def test_falls_back_to_denominator_heuristic_without_margin_asset(self):
        c = parse_contract("binance-futures", _sd("BTCUSDT", "perpetual"))
        assert c.internal_id == "perpetual-BTC-USDT:USDT"

    def test_spot_ignores_margin_asset(self):
        c = parse_contract(
            "binance-spot",
            _sd("BTCUSDT", "spot", margin_asset="USDT"),
        )
        assert c.margin is None
        assert c.internal_id == "spot-BTC-USDT"


class TestBitmexPerp:
    def test_inverse_xbtusd(self):
        c = _parse("bitmex", "XBTUSD", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.perpetual

    def test_linear_xbtusdt(self):
        c = _parse("bitmex", "XBTUSDT", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"

    def test_ethusd(self):
        c = _parse("bitmex", "ETHUSD", "perpetual")
        assert c.symbol == "ETH"
        assert c.denominator == "USD"


class TestOkxPerps:
    def test_inverse(self):
        c = _parse("okx-perps", "BTC-USD-SWAP", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.perpetual

    def test_linear(self):
        c = _parse("okx-perps", "BTC-USDT-SWAP", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"

    def test_non_swap_suffix_skipped(self):
        with pytest.raises(SkipSymbol):
            _parse("okx-perps", "BTC-USD-250328", "future")


class TestBybitSpot:
    def test_btc_usdt(self):
        c = _parse("bybit-spot", "BTCUSDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin is None

    def test_mnt_quote(self):
        c = _parse("bybit-spot", "ADAMNT", "spot")
        assert c.symbol == "ADA"
        assert c.denominator == "MNT"


class TestBybitPerps:
    def test_inverse_perp(self):
        c = _parse("bybit-perps", "BTCUSD", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.perpetual

    def test_usdc_linear_perp_with_suffix(self):
        c = _parse("bybit-perps", "BTCPERP", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDC"
        assert c.margin == "USDC"

    def test_api_metadata_overrides_symbol_heuristic(self):
        c = parse_contract(
            "bybit-perps",
            _sd(
                "BTCPERP",
                "perpetual",
                base_coin="BTC",
                quote_coin="USDC",
                settle_coin="USDC",
                quantity_unit="base",
            ),
        )
        assert c.internal_id == "perpetual-BTC-USDC:USDC"
        assert c.quantity_unit == "base"

    def test_inverse_quantity_is_quote_denomination(self):
        c = parse_contract(
            "bybit-perps",
            _sd(
                "BTCUSD",
                "perpetual",
                base_coin="BTC",
                quote_coin="USD",
                settle_coin="BTC",
                quantity_unit="quote",
            ),
        )
        assert c.internal_id == "perpetual-BTC-USD:BTC"
        assert c.quantity_unit == "quote"

    def test_linear_perp(self):
        c = _parse("bybit-perps", "BTCUSDT", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"


class TestHyperliquid:
    def test_btc_perpetual(self):
        c = _parse("hyperliquid-perps", "BTC", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDC"
        assert c.margin == "USDC"
        assert c.instrument_type == ContractType.perpetual

    def test_spot(self):
        c = _parse("hyperliquid-spot", "PURR/USDC", "spot")
        assert c.symbol == "PURR"
        assert c.denominator == "USDC"
        assert c.margin is None

    def test_at_prefix_skipped(self):
        with pytest.raises(SkipSymbol):
            _parse("hyperliquid-perps", "@0", "spot")

    def test_hip3_perp_keeps_dex_prefix(self):
        c = parse_contract(
            "hyperliquid-perps",
            _sd("xyz:KSTR", "perpetual", margin_asset="USDC"),
        )
        assert c.internal_id == "perpetual-XYZ:KSTR-USDC:USDC"

    @pytest.mark.parametrize(
        ("sid", "collateral", "expected"),
        [
            ("flx:TSLA", "USDH", "perpetual-FLX:TSLA-USDH:USDH"),
            ("km:US500", "USDH", "perpetual-KM:US500-USDH:USDH"),
            ("hyna:BTC", "USDE", "perpetual-HYNA:BTC-USDE:USDE"),
            ("cash:NVDA", "USDT0", "perpetual-CASH:NVDA-USDT0:USDT0"),
        ],
    )
    def test_hip3_perp_settles_in_the_dex_collateral(self, sid, collateral, expected):
        # A HIP-3 dex quotes, margins and settles every perp in its own
        # collateral token, which is not necessarily USDC.
        c = parse_contract("hyperliquid-perps", _sd(sid, "perpetual", margin_asset=collateral))
        assert c.denominator == collateral
        assert c.margin == collateral
        assert c.internal_id == expected

    def test_perp_without_venue_collateral_falls_back_to_usdc(self):
        c = _parse("hyperliquid-perps", "xyz:KSTR", "perpetual")
        assert c.internal_id == "perpetual-XYZ:KSTR-USDC:USDC"


class TestBitfinexDerivatives:
    def test_btc_perpetual(self):
        c = _parse("bitfinex-derivatives", "BTCF0:USTF0", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"
        assert c.instrument_type == ContractType.perpetual

    def test_eth_perpetual(self):
        c = _parse("bitfinex-derivatives", "ETHF0:USTF0", "perpetual")
        assert c.symbol == "ETH"
        assert c.denominator == "USDT"


class TestHuobiDmSwap:
    def test_inverse(self):
        c = _parse("huobi-dm-swap", "BTC-USD", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.perpetual

    def test_linear(self):
        c = _parse("huobi-dm-linear-swap", "BTC-USDT", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"


class TestGateIoFuturesPerp:
    def test_linear(self):
        c = _parse("gate-io-futures", "BTC_USDT", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"
        assert c.instrument_type == ContractType.perpetual

    def test_inverse(self):
        c = _parse("gate-io-futures", "BTC_USD", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"


# ---------------------------------------------------------------------------
# Futures
# ---------------------------------------------------------------------------


class TestBinanceFuturesDated:
    def test_linear_future(self):
        c = _parse("binance-futures", "BTCUSDT250328", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"
        assert c.instrument_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_eth_linear_future(self):
        c = _parse("binance-futures", "ETHUSDT250328", "future")
        assert c.symbol == "ETH"
        assert c.delivery_date == datetime(2025, 3, 28)


class TestBinanceDelivery:
    def test_inverse_future(self):
        c = _parse("binance-futures-cm", "BTCUSD_250328", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_inverse_perp(self):
        c = _parse("binance-futures-cm", "BTCUSD_PERP", "perpetual")
        assert c.symbol == "BTC"
        assert c.instrument_type == ContractType.perpetual
        assert c.delivery_date is None


class TestBitmexFuture:
    def test_cme_month_code_june(self):
        c = _parse("bitmex", "XBTM25", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.future
        assert c.delivery_date == datetime(2025, 6, 1)

    def test_cme_month_code_september(self):
        c = _parse("bitmex", "XBTU25", "future")
        assert c.delivery_date == datetime(2025, 9, 1)

    def test_cme_month_code_march(self):
        c = _parse("bitmex", "XBTH26", "future")
        assert c.delivery_date == datetime(2026, 3, 1)

    def test_eth_future(self):
        c = _parse("bitmex", "ETHU25", "future")
        assert c.symbol == "ETH"
        assert c.delivery_date == datetime(2025, 9, 1)


class TestOkexFutures:
    def test_inverse(self):
        c = _parse("okx-futures", "BTC-USD-250328", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_linear(self):
        c = _parse("okx-futures", "BTC-USDT-250328", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_wrong_part_count_skipped(self):
        with pytest.raises(SkipSymbol):
            _parse("okx-futures", "BTC-USD", "future")


class TestOkxUnifiedMargin:
    """OKX decorates the quote leg of its unified-margin families — `BTC-USD_UM-260828`,
    `BTC-USD_UM_XPERP-310404` — while `uly` stays `BTC-USD`. The decoration must not
    leak into the denominator, and these contracts are linear (`ctType: linear`,
    `settleCcy: USD`), not inverse."""

    def test_unified_margin_future(self):
        c = _parse("okx-futures", "BTC-USD_UM-260828", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "USD"
        assert c.internal_id == "future-BTC-USD:USD-20260828"

    def test_xperp_future(self):
        c = _parse("okx-futures", "AAPL-USD_UM_XPERP-310613", "future")
        assert c.symbol == "AAPL"
        assert c.denominator == "USD"
        assert c.margin == "USD"
        assert c.internal_id == "future-AAPL-USD:USD-20310613"

    def test_unified_margin_swap(self):
        c = _parse("okx-perps", "ADA-USD_UM-SWAP", "perpetual")
        assert c.symbol == "ADA"
        assert c.denominator == "USD"
        assert c.margin == "USD"
        assert c.internal_id == "perpetual-ADA-USD:USD"

    def test_does_not_collide_with_the_inverse_contract(self):
        linear = _parse("okx-futures", "BTC-USD_UM-260828", "future")
        inverse = _parse("okx-futures", "BTC-USD-260828", "future")
        assert linear.internal_id != inverse.internal_id
        assert inverse.internal_id == "future-BTC-USD:BTC-20260828"

    def test_unknown_suffix_is_left_alone(self):
        c = _parse("okx-futures", "BTC-USD_MYSTERY-260828", "future")
        assert c.denominator == "USD_MYSTERY"


class TestBybitFutures:
    def test_inverse(self):
        c = _parse("bybit-futures", "BTCUSD-28MAR25", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_linear(self):
        c = _parse("bybit-futures", "BTCUSDT-28MAR25", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_cme_style_inverse(self):
        c = _parse("bybit-futures", "BTCUSDH26", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.delivery_date == datetime(2026, 3, 1)

    def test_api_metadata_keeps_exact_delivery_date(self):
        c = parse_contract(
            "bybit-futures",
            _sd(
                "BTCUSDT-24JUL26",
                "future",
                base_coin="BTC",
                quote_coin="USDT",
                settle_coin="USDT",
                quantity_unit="base",
                delivery_date="2026-07-24T08:00:00+00:00",
            ),
        )
        assert c.internal_id == "future-BTC-USDT:USDT-20260724"
        assert c.delivery_date is not None
        assert c.delivery_date.date() == datetime(2026, 7, 24).date()


class TestDeribit:
    def test_perpetual(self):
        c = _parse("deribit", "BTC-PERPETUAL", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.perpetual
        assert c.delivery_date is None

    def test_future(self):
        c = _parse("deribit", "BTC-28MAR25", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_call_option(self):
        c = _parse("deribit", "BTC-28MAR25-50000-C", "option")
        assert c.symbol == "BTC"
        assert c.instrument_type == ContractType.option
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_put_option(self):
        c = _parse("deribit", "ETH-28MAR25-2000-P", "option")
        assert c.symbol == "ETH"
        assert c.instrument_type == ContractType.option
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_eth_perpetual(self):
        c = _parse("deribit", "ETH-PERPETUAL", "perpetual")
        assert c.symbol == "ETH"
        assert c.instrument_type == ContractType.perpetual


class TestHuobiDm:
    def test_rolling_current_week(self):
        c = _parse("huobi-dm", "BTC_CW", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.future
        assert c.delivery_date is None

    def test_rolling_next_quarter(self):
        c = _parse("huobi-dm", "BTC_NQ", "future")
        assert c.symbol == "BTC"
        assert c.delivery_date is None

    def test_dated(self):
        c = _parse("huobi-dm", "BTC201225", "future")
        assert c.symbol == "BTC"
        assert c.delivery_date == datetime(2020, 12, 25)


class TestGateIoFuturesDated:
    def test_linear(self):
        c = _parse("gate-io-futures", "BTC_USDT_20250328", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"
        assert c.instrument_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 28)


class TestCryptofacilities:
    def test_perpetual_index(self):
        c = _parse("cryptofacilities", "PI_XBTUSD", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.perpetual

    def test_future(self):
        c = _parse("cryptofacilities", "FI_XBTUSD_210129", "future")
        assert c.symbol == "BTC"
        assert c.instrument_type == ContractType.future
        assert c.delivery_date == datetime(2021, 1, 29)


class TestFtx:
    def test_perpetual(self):
        c = _parse("ftx", "BTC-PERP", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.perpetual

    def test_spot(self):
        c = _parse("ftx", "BTC/USD", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot

    def test_dated_future(self):
        c = _parse("ftx", "BTC-0325", "future")
        assert c.symbol == "BTC"
        assert c.instrument_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 1)


class TestPhemex:
    def test_spot_s_prefix(self):
        c = _parse("phemex", "sBTCUSDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin is None
        assert c.instrument_type == ContractType.spot

    def test_inverse_perp(self):
        c = _parse("phemex", "BTCUSD", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.instrument_type == ContractType.perpetual

    def test_linear_perp(self):
        c = _parse("phemex", "BTCUSDT", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"


# ---------------------------------------------------------------------------
# original_id and contract_size round-trip
# ---------------------------------------------------------------------------


class TestContractFields:
    def test_original_id_preserved(self):
        sd = _sd("BTCUSDT", "spot")
        c = parse_contract("binance-spot", sd)
        assert c.original_id == "BTCUSDT"

    def test_exchange_preserved(self):
        c = _parse("coinbase", "BTC-USD", "spot")
        assert c.exchange == "coinbase"

    def test_default_contract_size(self):
        c = _parse("binance-spot", "BTCUSDT", "spot")
        assert c.contract_size is None

    def test_contract_size_preserved(self):
        sd = _sd("BTCUSDT", "spot", contract_size=1.0)
        c = parse_contract("binance-spot", sd)
        assert c.contract_size == 1.0

    def test_symbols_uppercased(self):
        c = _parse("bitstamp", "btcusd", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"


class TestFetchOkxSwap:
    def _mock_get(self, data: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"data": data}
        return resp

    def test_contract_size_populated_from_ctval(self):
        from atlas.exchange_definitions.okx import fetch_okx_swap
        item = {"instId": "BTC-USDT-SWAP", "state": "live", "ctVal": "0.01"}
        with patch("atlas.exchange_definitions.okx.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_okx_swap(timeout_seconds=5)
        assert symbols[0]["contract_size"] == 0.01

    def test_contract_size_absent_when_ctval_missing(self):
        from atlas.exchange_definitions.okx import fetch_okx_swap
        item = {"instId": "BTC-USDT-SWAP", "state": "live"}
        with patch("atlas.exchange_definitions.okx.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_okx_swap(timeout_seconds=5)
        assert "contract_size" not in symbols[0]


class TestFetchOkxUnderlying:
    """OKX tags every instrument with `instCategory`; the ticker alone is ambiguous.

    Tokenized stocks on spot carry an `X` prefix (`XAAPL-USDT`), which reads like a
    crypto ticker, and stock perps use the bare ticker (`AAPL-USDT-SWAP`).
    """

    def _mock_get(self, data: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"data": data}
        return resp

    @pytest.mark.parametrize(
        ("fetcher_name", "inst_id"),
        [
            ("fetch_okx_spot", "XAAPL-USDT"),
            ("fetch_okx_swap", "AAPL-USDT-SWAP"),
            ("fetch_okx_futures", "AAPL-USD_UM_XPERP-310613"),
        ],
    )
    @pytest.mark.parametrize(
        ("inst_category", "expected_underlying"),
        [
            ("1", UnderlyingType.crypto.value),
            ("3", UnderlyingType.equity.value),
            ("4", UnderlyingType.commodity.value),
            ("", UnderlyingType.unknown.value),
            ("99", UnderlyingType.unknown.value),
        ],
    )
    def test_underlying_is_normalized_from_inst_category(
        self, fetcher_name: str, inst_id: str, inst_category: str, expected_underlying: str
    ):
        from atlas.exchange_definitions import okx

        item = {"instId": inst_id, "state": "live", "instCategory": inst_category}
        with patch("atlas.exchange_definitions.okx.requests.get", return_value=self._mock_get([item])):
            symbols = getattr(okx, fetcher_name)(timeout_seconds=5)
        assert symbols[0]["underlying"] == expected_underlying

    def test_missing_inst_category_is_explicitly_unknown(self):
        from atlas.exchange_definitions.okx import fetch_okx_swap

        item = {"instId": "BTC-USDT-SWAP", "state": "live"}
        with patch("atlas.exchange_definitions.okx.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_okx_swap(timeout_seconds=5)
        assert symbols[0]["underlying"] == UnderlyingType.unknown.value

    def test_venue_category_field_is_not_mistaken_for_asset_class(self):
        """`category` is `1` on every instrument, whatever the asset class."""
        from atlas.exchange_definitions.okx import fetch_okx_swap

        item = {"instId": "XAU-USDT-SWAP", "state": "live", "category": "1", "instCategory": "4"}
        with patch("atlas.exchange_definitions.okx.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_okx_swap(timeout_seconds=5)
        assert symbols[0]["underlying"] == UnderlyingType.commodity.value


class TestFetchBybit:
    def test_retries_malformed_response(self):
        from atlas.exchange_definitions.bybit import fetch_bybit_spot

        malformed = MagicMock()
        malformed.raise_for_status.return_value = None
        malformed.json.side_effect = ValueError("invalid JSON")
        valid = MagicMock()
        valid.raise_for_status.return_value = None
        valid.json.return_value = {
            "result": {"list": [{"symbol": "BTCUSDT", "status": "Trading"}]}
        }

        with patch(
            "atlas.exchange_definitions.bybit.requests.get",
            side_effect=[malformed, valid],
        ) as get:
            assert fetch_bybit_spot(timeout_seconds=5) == [{"id": "BTCUSDT", "type": "spot"}]

        assert get.call_count == 2

    def test_derivative_fetch_preserves_mapping_metadata_and_paginates(self):
        from atlas.exchange_definitions.bybit import fetch_bybit_perps

        first = MagicMock()
        first.raise_for_status.return_value = None
        first.json.return_value = {
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "Trading",
                        "contractType": "LinearPerpetual",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                    }
                ],
                "nextPageCursor": "next-page",
            }
        }
        second = MagicMock()
        second.raise_for_status.return_value = None
        second.json.return_value = {
            "result": {
                "list": [
                    {
                        "symbol": "BTCPERP",
                        "status": "Trading",
                        "contractType": "LinearPerpetual",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDC",
                        "settleCoin": "USDC",
                    }
                ],
                "nextPageCursor": "",
            }
        }
        inverse = MagicMock()
        inverse.raise_for_status.return_value = None
        inverse.json.return_value = {"result": {"list": [], "nextPageCursor": ""}}

        with patch(
            "atlas.exchange_definitions.bybit.requests.get",
            side_effect=[first, second, inverse],
        ) as get:
            assert fetch_bybit_perps(timeout_seconds=5) == [
                {
                    "id": "BTCUSDT",
                    "type": "perpetual",
                    "base_coin": "BTC",
                    "quote_coin": "USDT",
                    "settle_coin": "USDT",
                    "quantity_unit": "base",
                    "contract_size": 1.0,
                    "underlying": "crypto",
                },
                {
                    "id": "BTCPERP",
                    "type": "perpetual",
                    "base_coin": "BTC",
                    "quote_coin": "USDC",
                    "settle_coin": "USDC",
                    "quantity_unit": "base",
                    "contract_size": 1.0,
                    "underlying": "crypto",
                },
            ]

        assert get.call_args_list[1].kwargs["params"]["cursor"] == "next-page"

    @pytest.mark.parametrize(
        ("symbol_type", "expected_underlying"),
        [
            ("", UnderlyingType.crypto.value),
            ("innovation", UnderlyingType.crypto.value),
            ("stock", UnderlyingType.equity.value),
            ("ETF", UnderlyingType.equity.value),  # SPYUSDT, QQQUSDT
            ("commodity", UnderlyingType.commodity.value),
            ("forex", UnderlyingType.forex.value),  # EURUSDUSDT, USDJPYUSDT
            (None, UnderlyingType.crypto.value),  # the inverse category
            ("bond", UnderlyingType.unknown.value),  # never silently crypto
        ],
    )
    def test_derivative_underlying_is_normalized(
        self, symbol_type: str, expected_underlying: str
    ):
        from atlas.exchange_definitions.bybit import _to_symbol

        symbol = _to_symbol(
            {"symbol": "AAPLUSDT", "symbolType": symbol_type},
            "perpetual",
            "base",
        )

        assert symbol["underlying"] == expected_underlying

    def test_derivative_fetch_converts_delivery_time_to_delivery_date(self):
        from atlas.exchange_definitions.bybit import _to_symbol

        symbol = _to_symbol(
            {"symbol": "BTCUSDT-24JUL26", "deliveryTime": "1784880000000"},
            "future",
            "base",
        )

        assert symbol["delivery_date"] == "2026-07-24T08:00:00+00:00"


class TestFetchBinanceFuturesUsdM:
    def _mock_get(self, symbols: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"symbols": symbols}
        return resp

    def _item(self, symbol: str) -> dict:
        return {
            "symbol": symbol,
            "contractType": "PERPETUAL",
            "underlyingType": "COIN",
            "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.01"},
                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
            ],
        }

    def test_contract_size_is_always_one(self):
        from atlas.exchange_definitions.binance import fetch_binance_futures_usdm
        with patch("atlas.exchange_definitions.binance.requests.get", return_value=self._mock_get([self._item("BTCUSDT")])):
            symbols = fetch_binance_futures_usdm(timeout_seconds=5)
        assert symbols[0]["contract_size"] == 1.0

    def test_contract_size_one_regardless_of_step_size(self):
        from atlas.exchange_definitions.binance import fetch_binance_futures_usdm
        item = {"symbol": "SOLUSDT", "contractType": "PERPETUAL", "status": "TRADING",
                "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.1"}]}
        with patch("atlas.exchange_definitions.binance.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_binance_futures_usdm(timeout_seconds=5)
        assert symbols[0]["contract_size"] == 1.0

    def test_underlying_type_is_normalized(self):
        from atlas.exchange_definitions.binance import fetch_binance_futures_usdm

        item = self._item("AAPLUSDT") | {
            "contractType": "TRADIFI_PERPETUAL",
            "underlyingType": "HK_EQUITY",
        }
        with patch("atlas.exchange_definitions.binance.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_binance_futures_usdm(timeout_seconds=5)
        assert symbols[0]["underlying"] == UnderlyingType.equity.value

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("FX", UnderlyingType.forex.value),  # USDBRLUSDT
            ("CN_EQUITY", UnderlyingType.equity.value),  # CXMTUSDT, UNITREEUSDT
            ("INDEX", UnderlyingType.index.value),  # BTCDOMUSDT
            ("PREMARKET", UnderlyingType.pre_market.value),  # OPENAIUSDT (pre-IPO)
        ],
    )
    def test_every_live_underlying_type_is_mapped(self, raw, expected):
        from atlas.exchange_definitions.binance import fetch_binance_futures_usdm

        item = self._item("USDBRLUSDT") | {"contractType": "TRADIFI_PERPETUAL", "underlyingType": raw}
        with patch("atlas.exchange_definitions.binance.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_binance_futures_usdm(timeout_seconds=5)
        assert symbols[0]["underlying"] == expected

    def test_tradfi_perpetuals_are_perpetuals(self):
        # The fetcher must not rely on update.py renaming them back.
        from atlas.exchange_definitions.binance import fetch_binance_futures_usdm

        item = self._item("XAUUSDT") | {"contractType": "TRADIFI_PERPETUAL", "underlyingType": "COMMODITY"}
        with patch("atlas.exchange_definitions.binance.requests.get", return_value=self._mock_get([item])):
            (symbol,) = fetch_binance_futures_usdm(timeout_seconds=5)
        assert symbol["type"] == "perpetual"
        assert parse_contract("binance-futures", symbol).internal_id == "perpetual-XAU-USDT:USDT"

    def test_unknown_underlying_type_is_explicit(self):
        from atlas.exchange_definitions.binance import fetch_binance_futures_usdm

        item = self._item("BTCUSDT") | {"underlyingType": "UNRECOGNISED"}
        with patch("atlas.exchange_definitions.binance.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_binance_futures_usdm(timeout_seconds=5)
        assert symbols[0]["underlying"] == UnderlyingType.unknown.value

    def test_delisted_symbol_includes_underlying_type(self):
        from atlas.exchange_definitions.binance import fetch_binance_futures_usdm

        item = self._item("OMGUSDT") | {"status": "SETTLING"}
        with patch("atlas.exchange_definitions.binance.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_binance_futures_usdm(timeout_seconds=5)
        assert symbols == [
            {
                "id": "omgusdt",
                "type": "perpetual",
                "contract_size": 1.0,
                "underlying": UnderlyingType.crypto.value,
            }
        ]


class TestFetchBinanceFuturesCoinM:
    def _mock_get(self, symbols: list[dict]) -> MagicMock:
        resp = MagicMock()
        resp.json.return_value = {"symbols": symbols}
        return resp

    def test_contract_size_populated_from_api_field(self):
        from atlas.exchange_definitions.binance import fetch_binance_futures_coinm
        item = {"symbol": "BTCUSD_PERP", "contractType": "PERPETUAL", "contractStatus": "TRADING", "contractSize": 100}
        with patch("atlas.exchange_definitions.binance.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_binance_futures_coinm(timeout_seconds=5)
        assert symbols[0]["contract_size"] == 100.0

    def test_contract_size_absent_when_field_missing(self):
        from atlas.exchange_definitions.binance import fetch_binance_futures_coinm
        item = {"symbol": "BTCUSD_PERP", "contractType": "PERPETUAL", "contractStatus": "TRADING"}
        with patch("atlas.exchange_definitions.binance.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_binance_futures_coinm(timeout_seconds=5)
        assert "contract_size" not in symbols[0]

    def test_underlying_is_normalized(self):
        from atlas.exchange_definitions.binance import fetch_binance_futures_coinm

        item = {
            "symbol": "BTCUSD_PERP",
            "contractType": "PERPETUAL",
            "contractStatus": "TRADING",
            "underlyingType": "COIN",
        }
        with patch("atlas.exchange_definitions.binance.requests.get", return_value=self._mock_get([item])):
            symbols = fetch_binance_futures_coinm(timeout_seconds=5)
        assert symbols[0]["underlying"] == UnderlyingType.crypto.value


class TestFetchHyperliquidPerps:
    @staticmethod
    def _response(payload: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = payload
        return resp

    def _mock_info(
        self,
        metas: dict[str | None, dict],
        tokens: list[dict] | None = None,
        categories: list[list[str]] | None = None,
    ):
        """Route `/info` requests by body: `meta` per dex, `perpDexs`, `spotMeta`,
        `perpCategories`."""
        dexes = [None] + [{"name": dex} for dex in metas if dex is not None]

        def post(url, json, timeout):
            if json["type"] == "perpDexs":
                return self._response(dexes)
            if json["type"] == "spotMeta":
                return self._response({"tokens": tokens or [], "universe": []})
            if json["type"] == "perpCategories":
                return self._response(categories or [])
            assert json["type"] == "meta"
            return self._response(metas[json.get("dex")])

        return post

    def test_contract_size_is_one_unit_of_underlying(self):
        from atlas.exchange_definitions.hyperliquid import fetch_hyperliquid_perps
        item = {"name": "BTC", "szDecimals": 5, "maxLeverage": 40}
        post = self._mock_info({None: {"universe": [item]}})
        with patch("atlas.exchange_definitions.hyperliquid.requests.post", side_effect=post):
            symbols = fetch_hyperliquid_perps(timeout_seconds=5)
        assert symbols[0]["contract_size"] == 1.0

    def test_includes_hip3_dexes_with_their_collateral(self):
        from atlas.exchange_definitions.hyperliquid import fetch_hyperliquid_perps

        tokens = [
            {"index": 0, "name": "USDC"},
            {"index": 235, "name": "USDE"},
            {"index": 360, "name": "USDH"},
        ]
        metas = {
            None: {"universe": [{"name": "BTC"}], "collateralToken": 0},
            "xyz": {"universe": [{"name": "xyz:KSTR"}], "collateralToken": 0},
            "flx": {
                "universe": [{"name": "flx:TSLA"}, {"name": "flx:XMR", "isDelisted": True}],
                "collateralToken": 360,
            },
            "hyna": {"universe": [{"name": "hyna:BTC"}], "collateralToken": 235},
        }
        post = self._mock_info(metas, tokens)
        with patch("atlas.exchange_definitions.hyperliquid.requests.post", side_effect=post):
            symbols = fetch_hyperliquid_perps(timeout_seconds=5)

        assert {sd["id"]: sd.get("margin_asset") for sd in symbols} == {
            "BTC": "USDC",
            "xyz:KSTR": "USDC",
            "flx:TSLA": "USDH",
            "flx:XMR": "USDH",
            "hyna:BTC": "USDE",
        }
        assert all(sd["type"] == "perpetual" for sd in symbols)
        assert all(sd["contract_size"] == 1.0 for sd in symbols)

    def test_underlying_comes_from_the_venues_perp_categories(self):
        from atlas.exchange_definitions.hyperliquid import fetch_hyperliquid_perps

        # Captured from `{"type": "perpCategories"}` on 2026-10-14.
        categories = [
            ["xyz:TSLA", "stocks"],
            ["para:AAOI", "stock"],
            ["xyz:SP500", "indices"],
            ["xyz:GOLD", "commodities"],
            ["xyz:JPY", "fx"],
            ["km:EUR", "FX"],
            ["io:OAI", "preipo"],
            ["para:10Y", "rates"],
            ["para:BTCD", "crypto"],
            ["xyz:NEW", "bonds"],
        ]
        names = [name for name, _ in categories]
        metas = {
            None: {"universe": [{"name": "BTC"}, {"name": "kPEPE"}]},
            "xyz": {"universe": [{"name": n} for n in names] + [{"name": "xyz:NIFTY", "isDelisted": True}, {"name": "xyz:MYSTERY", "isDelisted": True}]},
        }
        post = self._mock_info(metas, categories=categories)
        with patch("atlas.exchange_definitions.hyperliquid.requests.post", side_effect=post):
            symbols = fetch_hyperliquid_perps(timeout_seconds=5)

        assert {sd["id"]: sd["underlying"] for sd in symbols} == {
            "BTC": "crypto",  # the main dex lists crypto only
            "kPEPE": "crypto",
            "xyz:TSLA": "equity",
            "para:AAOI": "equity",
            "xyz:SP500": "index",
            "xyz:GOLD": "commodity",
            "xyz:JPY": "forex",
            "km:EUR": "forex",
            "io:OAI": "pre_market",
            "para:10Y": "index",  # a treasury yield is a benchmark level
            "para:BTCD": "crypto",
            "xyz:NEW": "unknown",  # a label Atlas has not mapped yet
            # Uncategorised (delisted before the venue categorised assets):
            "xyz:NIFTY": "index",  # from the curated table
            "xyz:MYSTERY": "unknown",
        }

    def test_fetched_hip3_perps_parse_to_collateral_denominated_ids(self):
        from atlas.exchange_definitions.hyperliquid import fetch_hyperliquid_perps

        metas = {
            None: {"universe": [{"name": "BTC"}], "collateralToken": 0},
            "cash": {"universe": [{"name": "cash:NVDA"}], "collateralToken": 268},
        }
        tokens = [{"index": 0, "name": "USDC"}, {"index": 268, "name": "USDT0"}]
        post = self._mock_info(metas, tokens)
        with patch("atlas.exchange_definitions.hyperliquid.requests.post", side_effect=post):
            symbols = fetch_hyperliquid_perps(timeout_seconds=5)

        ids = [parse_contract("hyperliquid-perps", sd).internal_id for sd in symbols]
        assert ids == ["perpetual-BTC-USDC:USDC", "perpetual-CASH:NVDA-USDT0:USDT0"]

    def test_retries_malformed_response(self):
        from atlas.exchange_definitions.hyperliquid import fetch_hyperliquid_perps

        malformed = MagicMock()
        malformed.raise_for_status.return_value = None
        malformed.json.side_effect = ValueError("invalid JSON")
        valid = self._response({"universe": [{"name": "BTC"}]})

        with patch(
            "atlas.exchange_definitions.hyperliquid.requests.post",
            side_effect=[
                malformed,
                valid,
                self._response([None]),
                self._response({"tokens": [], "universe": []}),
                self._response([]),
            ],
        ) as post:
            assert fetch_hyperliquid_perps(timeout_seconds=5) == [
                {"id": "BTC", "type": "perpetual", "contract_size": 1.0, "underlying": "crypto"}
            ]

        assert post.call_count == 5


# ---------------------------------------------------------------------------
# Oracle-priced RWA perp venues: Ostium, Avantis, gTrade
#
# The fixtures are excerpts of the venues' live responses (2026-10-14). Their
# internal_ids must equal the live-producers Atlas-scheme IDs
# (streaming/feeds/{ostium,avantis,gtrade}), which is what joins live records
# to stored partitions; the venues are RWA venues, so getting each pair's
# underlying class right (forex vs equity vs index vs commodity) matters as
# much as the ID.
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str):
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _mock_json(payload) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _by_id(symbols: list[dict]) -> dict[str, dict]:
    return {sd["id"]: sd for sd in symbols}


class TestFetchOstiumPerps:
    def _fetch(self):
        from atlas.exchange_definitions.ostium import fetch_ostium_perps

        with (
            patch(
                "atlas.exchange_definitions.ostium.requests.post",
                return_value=_mock_json(_fixture("ostium_pairs.json")),
            ),
            patch(
                "atlas.exchange_definitions.ostium.requests.get",
                return_value=_mock_json(_fixture("ostium_prices.json")),
            ),
        ):
            return fetch_ostium_perps(timeout_seconds=5)

    def test_live_pairs_only(self):
        # USD/KRW (53) is listed with a zero open-interest cap; NVO-USD is a
        # price feed with no pair yet. Neither can be traded.
        ids = [sd["id"] for sd in self._fetch()]
        assert ids == ["0", "2", "4", "5", "6", "10", "13", "18", "52"]

    @pytest.mark.parametrize(
        ("pair_id", "internal_id", "underlying"),
        [
            ("0", "perpetual-BTC-USDC:USDC", "crypto"),
            ("2", "perpetual-EUR-USDC:USDC", "forex"),
            # Non-USD quotes keep both legs, so USD/JPY cannot collide with a
            # USD-quoted USD contract.
            ("4", "perpetual-USDJPY-USDC:USDC", "forex"),
            ("5", "perpetual-XAU-USDC:USDC", "commodity"),
            # The symbol is the venue's display name from the price feed, not
            # the subgraph's original ticker (HG, SPX, NIK).
            ("6", "perpetual-XCU-USDC:USDC", "commodity"),
            ("10", "perpetual-US500-USDC:USDC", "index"),
            ("13", "perpetual-JP225JPY-USDC:USDC", "index"),
            ("18", "perpetual-NVDA-USDC:USDC", "equity"),
            # ETFs (URA) are equity shares whatever they track.
            ("52", "perpetual-URA-USDC:USDC", "equity"),
        ],
    )
    def test_internal_id_and_underlying(self, pair_id, internal_id, underlying):
        sd = _by_id(self._fetch())[pair_id]
        c = parse_contract("ostium-perpetual", sd)
        assert c.internal_id == internal_id
        assert c.original_id == pair_id
        assert c.contract_size == 1.0
        assert c.margin == "USDC"
        assert sd["underlying"] == underlying

    def test_feed_ids_join_case_insensitively(self):
        from atlas.exchange_definitions.ostium import ostium_symbols

        pairs = [{"id": "0", "from": "BTC", "to": "USD", "feed": "0xABCD", "maxOI": "1", "group": {"name": "crypto"}}]
        prices = [{"feed_id": "0xabcd", "pair": "BTC-USD"}]
        assert [sd["base_coin"] for sd in ostium_symbols(pairs, prices)] == ["BTC"]

    def test_retries_indexer_without_queried_fields(self):
        # The endpoint load-balances two indexers; a GraphQL error from one is
        # retried rather than read as an empty venue.
        from atlas.exchange_definitions.ostium import fetch_ostium_perps

        error = _mock_json({"errors": [{"message": "Type `Pair` has no field `isActive`"}]})
        with (
            patch(
                "atlas.exchange_definitions.ostium.requests.post",
                side_effect=[error, _mock_json(_fixture("ostium_pairs.json"))],
            ) as post,
            patch(
                "atlas.exchange_definitions.ostium.requests.get",
                return_value=_mock_json(_fixture("ostium_prices.json")),
            ),
        ):
            assert len(fetch_ostium_perps(timeout_seconds=5)) == 9
        assert post.call_count == 2

    def test_rows_without_metadata_are_skipped(self):
        with pytest.raises(SkipSymbol):
            parse_contract("ostium-perpetual", _sd("0", "perpetual"))


class TestFetchAvantisPerps:
    def _fetch(self):
        from atlas.exchange_definitions.avantis import fetch_avantis_perps

        with patch(
            "atlas.exchange_definitions.avantis.requests.get",
            return_value=_mock_json(_fixture("avantis_trading.json")),
        ):
            return fetch_avantis_perps(timeout_seconds=5)

    def test_listed_pairs_only_in_index_order(self):
        # 22 (SHIB) and 35 ("DELISTED") are delisted; 32 has no name at all.
        ids = [sd["id"] for sd in self._fetch()]
        assert ids == ["0", "11", "12", "21", "78", "96", "114", "115", "188"]

    @pytest.mark.parametrize(
        ("index", "internal_id", "underlying"),
        [
            ("0", "perpetual-ETH-USDC:USDC", "crypto"),
            ("11", "perpetual-EUR-USDC:USDC", "forex"),
            ("12", "perpetual-USDJPY-USDC:USDC", "forex"),
            # Metals and energy are commodities.
            ("21", "perpetual-XAU-USDC:USDC", "commodity"),
            ("96", "perpetual-WTI-USDC:USDC", "commodity"),
            # US500 is priced off SPY, which the venue classifies as equity.
            ("78", "perpetual-US500-USDC:USDC", "equity"),
            # Whitespace inside a name would break the ID.
            ("114", "perpetual-SKHYNIX-USDC:USDC", "equity"),
            # Upside perps are separate markets sharing ETH's price feed.
            ("115", "perpetual-ETH_UPSIDE-USDC:USDC", "crypto"),
            # KRX equities are quoted in KRW.
            ("188", "perpetual-005930KRW-USDC:USDC", "equity"),
        ],
    )
    def test_internal_id_and_underlying(self, index, internal_id, underlying):
        sd = _by_id(self._fetch())[index]
        c = parse_contract("avantis-perpetual", sd)
        assert c.internal_id == internal_id
        assert c.original_id == index
        assert c.contract_size == 1.0
        assert sd["underlying"] == underlying

    @pytest.mark.parametrize(
        ("asset_type", "group", "expected"),
        [
            ("", "CRYPTO3", UnderlyingType.crypto),
            (None, "FOREX", UnderlyingType.forex),
            ("", "COMMODITIES", UnderlyingType.commodity),
            ("", "EQUITIES", UnderlyingType.equity),
            ("", "SOMETHING", UnderlyingType.unknown),
            ("metal", "CRYPTO1", UnderlyingType.commodity),
        ],
    )
    def test_underlying_falls_back_to_the_group(self, asset_type, group, expected):
        from atlas.exchange_definitions.avantis import avantis_underlying

        assert avantis_underlying(asset_type, group) == expected


class TestFetchGtradePerps:
    def _fetch(self):
        from atlas.exchange_definitions.gtrade import fetch_gtrade_perps

        with patch(
            "atlas.exchange_definitions.gtrade.requests.get",
            return_value=_mock_json(_fixture("gtrade_markets.json")),
        ):
            return fetch_gtrade_perps(timeout_seconds=5)

    @pytest.mark.parametrize(
        ("market", "internal_id", "underlying"),
        [
            # One market per pair whatever the collateral (DAI, WETH, USDC,
            # GNS): settled in USD.
            ("BTC/USD", "perpetual-BTC-USD:USD", "crypto"),
            ("EUR/USD", "perpetual-EUR-USD:USD", "forex"),
            ("USD/JPY", "perpetual-USDJPY-USD:USD", "forex"),
            ("EUR/JPY", "perpetual-EURJPY-USD:USD", "forex"),
            ("XAU/USD", "perpetual-XAU-USD:USD", "commodity"),
            ("WTI/USD", "perpetual-WTI-USD:USD", "commodity"),
            # The venue files index ETFs under indices.
            ("SPY/USD", "perpetual-SPY-USD:USD", "index"),
            ("AAPL/USD", "perpetual-AAPL-USD:USD", "equity"),
            # gTrade's SPX is the SPX6900 token, not the S&P 500.
            ("SPX/USD", "perpetual-SPX-USD:USD", "crypto"),
        ],
    )
    def test_internal_id_and_underlying(self, market, internal_id, underlying):
        sd = _by_id(self._fetch())[market]
        c = parse_contract("gtrade-perpetual", sd)
        assert c.internal_id == internal_id
        assert c.original_id == market
        assert c.contract_size == 1.0
        assert sd["underlying"] == underlying

    def test_markets_without_base_or_quote_are_skipped(self):
        from atlas.exchange_definitions.gtrade import gtrade_symbols

        assert gtrade_symbols([{"symbol": "X/USD", "base": "", "quote": "USD", "group": "crypto"}]) == []

    def test_retries_malformed_response(self):
        from atlas.exchange_definitions.gtrade import fetch_gtrade_perps

        with patch(
            "atlas.exchange_definitions.gtrade.requests.get",
            side_effect=[_mock_json({"code": "NOT_READY"}), _mock_json(_fixture("gtrade_markets.json"))],
        ) as get:
            assert len(fetch_gtrade_perps(timeout_seconds=5)) == 9
        assert get.call_count == 2


class TestRwaVenueRegistry:
    @pytest.mark.parametrize("exchange", ["ostium-perpetual", "avantis-perpetual", "gtrade-perpetual"])
    def test_refreshed_daily_from_the_exchange_api(self, exchange):
        # Tardis does not cover these venues: the update must not skip them as
        # beta, and they need an exchange-API fetcher.
        from atlas.exchanges import get_allowed_types, get_exchange_fetcher
        from atlas.update import EXCHANGES

        assert is_beta_exchange(exchange) is False
        assert get_exchange_fetcher(exchange) is not None
        assert get_allowed_types(exchange) == {"perpetual"}
        assert exchange in EXCHANGES
