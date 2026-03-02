import pytest
from datetime import datetime


from atlas.contracts import ContractType
from atlas.exchange_definitions import is_beta_exchange
from atlas.parsers import SkipSymbol, parse_contract


def _sd(id: str, type: str) -> dict:
    return {"id": id, "type": type}


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
        c = _parse("binance", "BTCUSDT", "mystery")
        assert c is not None
        assert c.contract_type == ContractType.unknown

    def test_missing_id_key_raises(self):
        with pytest.raises(KeyError):
            parse_contract("binance", {})

    def test_beta_exchange_classification(self):
        assert is_beta_exchange("binance-spot") is False
        assert is_beta_exchange("okx-spot") is False
        assert is_beta_exchange("coinbase") is True


# ---------------------------------------------------------------------------
# Spot exchanges
# ---------------------------------------------------------------------------


class TestBinanceSpot:
    def test_btcusdt(self):
        c = _parse("binance", "BTCUSDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin is None
        assert c.contract_type == ContractType.spot
        assert c.delivery_date is None

    def test_ethbtc(self):
        c = _parse("binance", "ETHBTC", "spot")
        assert c.symbol == "ETH"
        assert c.denominator == "BTC"
        assert c.margin is None
        assert c.contract_type == ContractType.spot

    def test_bnbbusd(self):
        c = _parse("binance", "BNBBUSD", "spot")
        assert c.symbol == "BNB"
        assert c.denominator == "BUSD"

    def test_trxxrp_alias_binance_spot(self):
        c = _parse("binance-spot", "trxxrp", "spot")
        assert c.symbol == "TRX"
        assert c.denominator == "XRP"
        assert c.margin is None
        assert c.contract_type == ContractType.spot

    def test_wintrx_alias_binance_spot(self):
        c = _parse("binance-spot", "wintrx", "spot")
        assert c.symbol == "WIN"
        assert c.denominator == "TRX"
        assert c.margin is None
        assert c.contract_type == ContractType.spot

    def test_btcpln_alias_binance_spot(self):
        c = _parse("binance-spot", "btcpln", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "PLN"
        assert c.margin is None
        assert c.contract_type == ContractType.spot

    def test_usdtars_alias_binance_spot(self):
        c = _parse("binance-spot", "usdtars", "spot")
        assert c.symbol == "USDT"
        assert c.denominator == "ARS"
        assert c.margin is None
        assert c.contract_type == ContractType.spot

    def test_bnbjpy_alias_binance_spot(self):
        c = _parse("binance-spot", "bnbjpy", "spot")
        assert c.symbol == "BNB"
        assert c.denominator == "JPY"
        assert c.margin is None
        assert c.contract_type == ContractType.spot

    def test_unrecognised_quote_raises(self):
        with pytest.raises(SkipSymbol):
            _parse("binance", "ABCXYZ", "spot")

    def test_alias_binance_spot(self):
        c = _parse("binance-spot", "BTCUSDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.contract_type == ContractType.spot


class TestCoinbase:
    def test_btc_usd(self):
        c = _parse("coinbase", "BTC-USD", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin is None
        assert c.contract_type == ContractType.spot

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
        assert c.contract_type == ContractType.spot


class TestOkxSpot:
    def test_btc_usdt(self):
        c = _parse("okx-spot", "BTC-USDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin is None
        assert c.contract_type == ContractType.spot


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
        assert c.contract_type == ContractType.perpetual

    def test_inverse_perp(self):
        c = _parse("binance-futures-cm", "BTCUSD_PERP", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.contract_type == ContractType.perpetual

    def test_eth_inverse_perp(self):
        c = _parse("binance-futures", "ETHUSD_PERP", "perpetual")
        assert c.symbol == "ETH"
        assert c.margin == "ETH"


class TestBitmexPerp:
    def test_inverse_xbtusd(self):
        c = _parse("bitmex", "XBTUSD", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.contract_type == ContractType.perpetual

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
        assert c.contract_type == ContractType.perpetual

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
        assert c.contract_type == ContractType.perpetual

    def test_inverse_perp_with_suffix(self):
        c = _parse("bybit-perps", "BTCPERP", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"

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
        assert c.contract_type == ContractType.perpetual

    def test_spot(self):
        c = _parse("hyperliquid-spot", "PURR/USDC", "spot")
        assert c.symbol == "PURR"
        assert c.denominator == "USDC"
        assert c.margin is None

    def test_at_prefix_skipped(self):
        with pytest.raises(SkipSymbol):
            _parse("hyperliquid-perps", "@0", "spot")


class TestBitfinexDerivatives:
    def test_btc_perpetual(self):
        c = _parse("bitfinex-derivatives", "BTCF0:USTF0", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin == "USDT"
        assert c.contract_type == ContractType.perpetual

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
        assert c.contract_type == ContractType.perpetual

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
        assert c.contract_type == ContractType.perpetual

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
        assert c.contract_type == ContractType.future
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
        assert c.contract_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_inverse_perp(self):
        c = _parse("binance-futures-cm", "BTCUSD_PERP", "perpetual")
        assert c.symbol == "BTC"
        assert c.contract_type == ContractType.perpetual
        assert c.delivery_date is None


class TestBitmexFuture:
    def test_cme_month_code_june(self):
        c = _parse("bitmex", "XBTM25", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.contract_type == ContractType.future
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
        assert c.contract_type == ContractType.future
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


class TestBybitFutures:
    def test_inverse(self):
        c = _parse("bybit-futures", "BTCUSD-28MAR25", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.contract_type == ContractType.future
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


class TestDeribit:
    def test_perpetual(self):
        c = _parse("deribit", "BTC-PERPETUAL", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.contract_type == ContractType.perpetual
        assert c.delivery_date is None

    def test_future(self):
        c = _parse("deribit", "BTC-28MAR25", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.contract_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_call_option(self):
        c = _parse("deribit", "BTC-28MAR25-50000-C", "option")
        assert c.symbol == "BTC"
        assert c.contract_type == ContractType.option
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_put_option(self):
        c = _parse("deribit", "ETH-28MAR25-2000-P", "option")
        assert c.symbol == "ETH"
        assert c.contract_type == ContractType.option
        assert c.delivery_date == datetime(2025, 3, 28)

    def test_eth_perpetual(self):
        c = _parse("deribit", "ETH-PERPETUAL", "perpetual")
        assert c.symbol == "ETH"
        assert c.contract_type == ContractType.perpetual


class TestHuobiDm:
    def test_rolling_current_week(self):
        c = _parse("huobi-dm", "BTC_CW", "future")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.contract_type == ContractType.future
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
        assert c.contract_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 28)


class TestCryptofacilities:
    def test_perpetual_index(self):
        c = _parse("cryptofacilities", "PI_XBTUSD", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.contract_type == ContractType.perpetual

    def test_future(self):
        c = _parse("cryptofacilities", "FI_XBTUSD_210129", "future")
        assert c.symbol == "BTC"
        assert c.contract_type == ContractType.future
        assert c.delivery_date == datetime(2021, 1, 29)


class TestFtx:
    def test_perpetual(self):
        c = _parse("ftx", "BTC-PERP", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.contract_type == ContractType.perpetual

    def test_spot(self):
        c = _parse("ftx", "BTC/USD", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin is None
        assert c.contract_type == ContractType.spot

    def test_dated_future(self):
        c = _parse("ftx", "BTC-0325", "future")
        assert c.symbol == "BTC"
        assert c.contract_type == ContractType.future
        assert c.delivery_date == datetime(2025, 3, 1)


class TestPhemex:
    def test_spot_s_prefix(self):
        c = _parse("phemex", "sBTCUSDT", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USDT"
        assert c.margin is None
        assert c.contract_type == ContractType.spot

    def test_inverse_perp(self):
        c = _parse("phemex", "BTCUSD", "perpetual")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
        assert c.margin == "BTC"
        assert c.contract_type == ContractType.perpetual

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
        c = parse_contract("binance", sd)
        assert c.original_id == "BTCUSDT"

    def test_exchange_preserved(self):
        c = _parse("coinbase", "BTC-USD", "spot")
        assert c.exchange == "coinbase"

    def test_default_contract_size(self):
        c = _parse("binance", "BTCUSDT", "spot")
        assert c.contract_size == 1.0

    def test_symbols_uppercased(self):
        c = _parse("bitstamp", "btcusd", "spot")
        assert c.symbol == "BTC"
        assert c.denominator == "USD"
