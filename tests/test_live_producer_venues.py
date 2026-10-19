"""Exchange-API fetchers for the venues live-producers streams.

Payloads are excerpts of real responses captured on 2026-10-14. Each venue's
fetcher must (a) reproduce the producer's Atlas-scheme ID for the same
instrument, so live records join their Atlas row, and (b) classify the
underlying from the venue's own metadata, because RWA perps (stocks, indices,
metals, FX) sit next to crypto under the same quote currency.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from atlas.parsers import parse_contract


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _route(routes: dict[str, object]):
    """Fake `requests.get` answering by URL suffix (and `category` param)."""

    def get(url, params=None, timeout=None):
        key = url
        if params and "category" in params:
            key = f"{url}?category={params['category']}"
        for suffix, payload in routes.items():
            if key.endswith(suffix):
                return _response(payload)
        raise AssertionError(f"unexpected request {key}")

    return get


def _fetch(fetcher, routes: dict[str, object]) -> list[dict]:
    with patch("atlas.exchange_definitions.http.requests.get", side_effect=_route(routes)):
        return fetcher(timeout_seconds=5)


def _ids(exchange: str, rows: list[dict]) -> dict[str, str]:
    return {sd["id"]: parse_contract(exchange, sd).internal_id for sd in rows}


def _classes(rows: list[dict]) -> dict[str, str]:
    return {sd["id"]: sd["underlying"] for sd in rows}


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

GATE_USDT = [
    {"name": "BTC_USDT", "quanto_multiplier": "0.0001", "contract_type": "", "is_pre_market": False, "type": "direct"},
    {"name": "AAPL_USDT", "quanto_multiplier": "0.01", "contract_type": "stocks", "is_pre_market": False, "type": "direct"},
    {"name": "SPX500_USDT", "quanto_multiplier": "0.001", "contract_type": "indices", "is_pre_market": False, "type": "direct"},
    {"name": "XAU_USDT", "quanto_multiplier": "0.0001", "contract_type": "metals", "is_pre_market": False, "type": "direct"},
    {"name": "CL_USDT", "quanto_multiplier": "0.01", "contract_type": "commodities", "is_pre_market": False, "type": "direct"},
    {"name": "EURUSD_USDT", "quanto_multiplier": "1", "contract_type": "forex", "is_pre_market": False, "type": "direct"},
    {"name": "OPENAI_USDT", "quanto_multiplier": "0.01", "contract_type": "stocks", "is_pre_market": True, "type": "direct"},
    {"name": "H100_USDT", "quanto_multiplier": "1", "contract_type": "indices", "is_pre_market": True, "type": "direct"},
    {"name": "BP_USDT", "quanto_multiplier": "10", "contract_type": "", "is_pre_market": True, "type": "direct"},
    {"name": "NEW_USDT", "quanto_multiplier": "1", "contract_type": "bonds", "is_pre_market": False, "type": "direct"},
]
GATE_BTC = [{"name": "BTC_USD", "quanto_multiplier": "0", "contract_type": "", "is_pre_market": False, "type": "inverse"}]
GATE_DELIVERY = [
    {"name": "BTC_USDT_20261023", "quanto_multiplier": "0.0001", "expire_time": 1792742400, "underlying": "BTC_USDT", "type": "direct"}
]


class TestGate:
    def test_perpetual_ids_match_the_producer(self):
        from atlas.exchange_definitions.gate_io import fetch_gateio_perpetual_btc, fetch_gateio_perpetual_usdt

        usdt = _fetch(fetch_gateio_perpetual_usdt, {"/futures/usdt/contracts": GATE_USDT})
        btc = _fetch(fetch_gateio_perpetual_btc, {"/futures/btc/contracts": GATE_BTC})
        assert _ids("gateio-perpetual-usdt", usdt)["BTC_USDT"] == "perpetual-BTC-USDT:USDT"
        # FX pairs keep Gate's concatenated base, as the producer does.
        assert _ids("gateio-perpetual-usdt", usdt)["EURUSD_USDT"] == "perpetual-EURUSD-USDT:USDT"
        # BTC-settled contracts are inverse: margin is the base asset.
        assert _ids("gateio-perpetual-btc", btc) == {"BTC_USD": "perpetual-BTC-USD:BTC"}

    def test_contract_sizes(self):
        from atlas.exchange_definitions.gate_io import fetch_gateio_perpetual_btc, fetch_gateio_perpetual_usdt

        usdt = {sd["id"]: sd for sd in _fetch(fetch_gateio_perpetual_usdt, {"/futures/usdt/contracts": GATE_USDT})}
        (btc,) = _fetch(fetch_gateio_perpetual_btc, {"/futures/btc/contracts": GATE_BTC})
        assert usdt["BTC_USDT"]["contract_size"] == 0.0001
        assert usdt["BTC_USDT"]["quantity_unit"] == "base"
        # The inverse contract reports multiplier 0: it is one USD per contract.
        assert (btc["contract_size"], btc["quantity_unit"]) == (1.0, "quote")

    def test_underlying_follows_contract_type(self):
        from atlas.exchange_definitions.gate_io import fetch_gateio_perpetual_usdt

        rows = _fetch(fetch_gateio_perpetual_usdt, {"/futures/usdt/contracts": GATE_USDT})
        assert _classes(rows) == {
            "BTC_USDT": "crypto",
            "AAPL_USDT": "equity",
            "SPX500_USDT": "index",
            "XAU_USDT": "commodity",
            "CL_USDT": "commodity",
            "EURUSD_USDT": "forex",
            # Pre-market stocks are private companies (pre-IPO) ...
            "OPENAI_USDT": "pre_market",
            # ... but a "pre-market" index is just a new index.
            "H100_USDT": "index",
            "BP_USDT": "pre_market",
            "NEW_USDT": "unknown",
        }

    def test_delivery_futures_inherit_their_perpetuals_class(self):
        from atlas.exchange_definitions.gate_io import fetch_gateio_futures_usdt

        rows = _fetch(
            fetch_gateio_futures_usdt,
            {"/delivery/usdt/contracts": GATE_DELIVERY, "/futures/usdt/contracts": GATE_USDT},
        )
        assert _ids("gateio-futures-usdt", rows) == {
            "BTC_USDT_20261023": "future-BTC-USDT:USDT-20261023"
        }
        assert rows[0]["underlying"] == "crypto"
        assert rows[0]["contract_size"] == 0.0001

    def test_tardis_rows_parse_from_the_id(self):
        assert parse_contract("gateio-perpetual-btc", {"id": "BTC_USD", "type": "perpetual"}).internal_id == "perpetual-BTC-USD:BTC"
        assert parse_contract("gateio-perpetual-usdt", {"id": "ETH_USDT", "type": "perpetual"}).internal_id == "perpetual-ETH-USDT:USDT"

    def test_shared_tardis_feed_is_split_by_settlement(self):
        from atlas.exchanges import get_symbol_filter

        usdt, btc = get_symbol_filter("gateio-perpetual-usdt"), get_symbol_filter("gateio-perpetual-btc")
        assert usdt({"id": "BTC_USDT"})
        assert not usdt({"id": "BTC_USD"})
        assert btc({"id": "BTC_USD"})
        assert not btc({"id": "BTC_USDT"})


# ---------------------------------------------------------------------------
# HTX
# ---------------------------------------------------------------------------

HTX = {
    "data": [
        {"contract_code": "BTC-USDT", "symbol": "BTC", "contract_size": 0.001, "contract_status": 1, "business_type": "swap", "trade_partition": "USDT", "tradfi_labels": []},
        {"contract_code": "TQQQ-USDT", "symbol": "TQQQ", "contract_size": 0.01, "contract_status": 1, "business_type": "swap", "trade_partition": "USDT", "tradfi_labels": ["Stocks", "Indices"]},
        {"contract_code": "XAU-USDT", "symbol": "XAU", "contract_size": 0.001, "contract_status": 1, "business_type": "swap", "trade_partition": "USDT", "tradfi_labels": ["Metals"]},
        {"contract_code": "TBT-USDT", "symbol": "TBT", "contract_size": 0.1, "contract_status": 1, "business_type": "swap", "trade_partition": "USDT", "tradfi_labels": ["Indices"]},
        {"contract_code": "ETH-USDT", "symbol": "ETH", "contract_size": 0.01, "contract_status": 5, "business_type": "swap", "trade_partition": "USDT", "tradfi_labels": []},
        # Suspended: no bid, no ask, last trade weeks old.
        {"contract_code": "CYBER-USDT", "symbol": "CYBER", "contract_size": 0.1, "contract_status": 3, "business_type": "swap", "trade_partition": "USDT", "tradfi_labels": []},
    ]
}


class TestHtx:
    def test_listed_contracts_and_their_ids(self):
        from atlas.exchange_definitions.huobi import fetch_htx_perpetual_usdt

        rows = _fetch(fetch_htx_perpetual_usdt, {"/swap_contract_info": HTX})
        ids = _ids("htx-perpetual-usdt", rows)
        # Suspended (status 3) is not delisted: the daily update would close a
        # contract the fetcher left out.
        assert "end_date" not in {sd["id"]: sd for sd in rows}["CYBER-USDT"]
        # Status 5 (settling at funding time) is still a trading contract.
        assert ids["ETH-USDT"] == "perpetual-ETH-USDT:USDT"
        assert ids["BTC-USDT"] == "perpetual-BTC-USDT:USDT"
        assert {sd["id"]: sd["contract_size"] for sd in rows}["BTC-USDT"] == 0.001

    def test_underlying_follows_tradfi_labels(self):
        from atlas.exchange_definitions.huobi import fetch_htx_perpetual_usdt

        rows = _fetch(fetch_htx_perpetual_usdt, {"/swap_contract_info": HTX})
        assert _classes(rows) == {
            "BTC-USDT": "crypto",
            "ETH-USDT": "crypto",
            "TQQQ-USDT": "equity",  # an index ETF is still an equity
            "XAU-USDT": "commodity",
            "TBT-USDT": "index",
            "CYBER-USDT": "crypto",
        }


# ---------------------------------------------------------------------------
# MEXC
# ---------------------------------------------------------------------------


def _mexc(symbol, base, quote, settle, size, zones, opening=0, created=1591242684000, state=0):
    return {
        "symbol": symbol, "baseCoin": base, "quoteCoin": quote, "settleCoin": settle,
        "contractSize": size, "state": state, "openingTime": opening, "createTime": created,
        "conceptPlate": [f"mc-trade-zone-{z}" for z in zones],
    }


MEXC = {
    "data": [
        _mexc("BTC_USDT", "BTC", "USDT", "USDT", 0.0001, ["mainly", "pow"]),
        _mexc("BTC_USD", "BTC", "USD", "BTC", 100, ["pow", "mainly"], created=1608718223000),
        _mexc("ETH_USD1", "ETH", "USD1", "USD1", 0.01, ["mainly"]),
        _mexc("GLD_USDT", "GLD", "USDT", "USDT", 0.001, ["metals", "tradfi", "ETF", "Commodities"], opening=1789653600000),
        _mexc("NAS100_USDT", "NAS100", "USDT", "USDT", 1e-05, ["Stock", "tradfi", "stockindex"]),
        _mexc("EUR_USDT", "EUR", "USDT", "USDT", 1, ["web3", "tradfi", "Forex"]),
        _mexc("USOIL_USDT", "USOIL", "USDT", "USDT", 0.01, ["0fees", "OIL", "tradfi"]),
        _mexc("XAU_USDT", "XAU", "USDT", "USDT", 0.001, ["Commodities", "metals", "metalsfutures", "tradfi"]),
        _mexc("OPENAI_USDT", "OPENAI", "USDT", "USDT", 0.001, ["Stock", "preipo", "tradfi"]),
        _mexc("SI_USDT", "SI", "USDT", "USDT", 10, ["stockmeme"]),
        _mexc("TESLA_USDT", "TESLA", "USDT", "USDT", 0.01, ["Stock", "tradfi", "TechGiants"]),
        _mexc("ODD_USDT", "ODD", "USDT", "USDT", 1, ["tradfi"]),
        _mexc("GONE_USDT", "GONE", "USDT", "USDT", 1, ["mainly"], state=3),
    ]
}


class TestMexc:
    def test_ids_use_the_settle_asset(self):
        from atlas.exchange_definitions.mexc import fetch_mexc_perpetual

        ids = _ids("mexc-perpetual", _fetch(fetch_mexc_perpetual, {"/contract/detail": MEXC}))
        assert ids["BTC_USDT"] == "perpetual-BTC-USDT:USDT"
        # Coin-margined: settles in BTC.
        assert ids["BTC_USD"] == "perpetual-BTC-USD:BTC"
        # A stablecoin the linear whitelist does not know is still linear.
        assert ids["ETH_USD1"] == "perpetual-ETH-USD1:USD1"
        # Offline contracts (state 3) are reported delisted.
        rows = {sd["id"]: sd for sd in _fetch(fetch_mexc_perpetual, {"/contract/detail": MEXC})}
        assert "end_date" in rows["GONE_USDT"]
        assert "end_date" not in rows["BTC_USDT"]

    def test_contract_size_units_and_listing_date(self):
        from atlas.exchange_definitions.mexc import fetch_mexc_perpetual

        rows = {sd["id"]: sd for sd in _fetch(fetch_mexc_perpetual, {"/contract/detail": MEXC})}
        assert (rows["BTC_USDT"]["contract_size"], rows["BTC_USDT"]["quantity_unit"]) == (0.0001, "base")
        assert (rows["BTC_USD"]["contract_size"], rows["BTC_USD"]["quantity_unit"]) == (100.0, "quote")
        # Opening time when MEXC scheduled one, else the creation time.
        assert rows["GLD_USDT"]["first_capture"] == "2026-09-17T14:00:00Z"
        assert rows["BTC_USDT"]["first_capture"] == "2020-06-04T03:51:24Z"

    def test_underlying_follows_trade_zones(self):
        from atlas.exchange_definitions.mexc import fetch_mexc_perpetual

        classes = _classes(_fetch(fetch_mexc_perpetual, {"/contract/detail": MEXC}))
        assert classes == {
            "BTC_USDT": "crypto",
            "BTC_USD": "crypto",
            "ETH_USD1": "crypto",
            "GLD_USDT": "equity",  # an ETF, although filed under Commodities
            "NAS100_USDT": "index",
            "EUR_USDT": "forex",
            "USOIL_USDT": "commodity",
            "XAU_USDT": "commodity",
            "OPENAI_USDT": "pre_market",
            "SI_USDT": "crypto",  # a memecoin named after a stock
            "TESLA_USDT": "equity",
            "ODD_USDT": "unknown",  # TradFi, class not given
            "GONE_USDT": "crypto",
        }


# ---------------------------------------------------------------------------
# Bitget (v3 instruments)
# ---------------------------------------------------------------------------


def _bitget(symbol, base, quote, kind="perpetual", symbol_type="crypto", rwa="NO", status="online", delivery="", launch="0"):
    return {
        "symbol": symbol, "baseCoin": base, "quoteCoin": quote, "type": kind, "status": status,
        "symbolType": symbol_type, "isRwa": rwa, "deliveryTime": delivery, "launchTime": launch,
        "quantityMultiplier": "0.0001",
    }


BITGET = {
    "USDT-FUTURES": {"data": [
        _bitget("BTCUSDT", "BTC", "USDT"),
        _bitget("TSLAUSDT", "TSLA", "USDT", symbol_type="stock", rwa="YES"),
        _bitget("XAUUSDT", "XAU", "USDT", symbol_type="metal", rwa="YES"),
        _bitget("CLUSDT", "CL", "USDT", symbol_type="commodity", rwa="YES"),
        # Bitget leaves some RWA contracts at symbolType crypto.
        _bitget("EURUSDUSDT", "EURUSD", "USDT", rwa="YES"),
        _bitget("OLDUSDT", "OLD", "USDT", status="offline"),
    ]},
    "USDC-FUTURES": {"data": [_bitget("BTCPERP", "BTC", "USDC")]},
    "COIN-FUTURES": {"data": [
        _bitget("BTCUSD_CM", "BTC", "USD", launch="1768997398543"),
        _bitget("BTCCMZ26", "BTC", "USD", kind="delivery", delivery="1798185600000", launch="1788334076393"),
    ]},
}
BITGET_ROUTES = {f"/instruments?category={k}": v for k, v in BITGET.items()}


class TestBitget:
    def test_ids_match_the_producer(self):
        from atlas.exchange_definitions import bitget

        usdt = _fetch(bitget.fetch_bitget_perpetual_usdt, BITGET_ROUTES)
        usdc = _fetch(bitget.fetch_bitget_perpetual_usdc, BITGET_ROUTES)
        coin = _fetch(bitget.fetch_bitget_perpetual_coin, BITGET_ROUTES)
        dated = _fetch(bitget.fetch_bitget_futures_coin, BITGET_ROUTES)
        assert _ids("bitget-perpetual-usdt", usdt)["BTCUSDT"] == "perpetual-BTC-USDT:USDT"
        # Offline contracts are reported delisted.
        assert "end_date" in {sd["id"]: sd for sd in usdt}["OLDUSDT"]
        assert _ids("bitget-perpetual-usdc", usdc) == {"BTCPERP": "perpetual-BTC-USDC:USDC"}
        assert _ids("bitget-perpetual-coin", coin) == {"BTCUSD_CM": "perpetual-BTC-USD:BTC"}
        assert _ids("bitget-futures-coin", dated) == {"BTCCMZ26": "future-BTC-USD:BTC-20261225"}

    def test_quantity_units(self):
        from atlas.exchange_definitions import bitget

        (usdc,) = _fetch(bitget.fetch_bitget_perpetual_usdc, BITGET_ROUTES)
        (coin,) = _fetch(bitget.fetch_bitget_perpetual_coin, BITGET_ROUTES)
        # quantityMultiplier is an order step, not a contract multiplier.
        assert (usdc["contract_size"], usdc["quantity_unit"]) == (1.0, "base")
        assert (coin["contract_size"], coin["quantity_unit"]) == (1.0, "quote")
        # The launch time dates a listing where Bitget recorded one.
        assert coin["first_capture"] == "2026-01-21T12:09:58.543Z"
        assert "first_capture" not in usdc

    def test_underlying(self):
        from atlas.exchange_definitions import bitget

        assert _classes(_fetch(bitget.fetch_bitget_perpetual_usdt, BITGET_ROUTES)) == {
            "BTCUSDT": "crypto",
            "TSLAUSDT": "equity",
            "XAUUSDT": "commodity",
            "CLUSDT": "commodity",
            "EURUSDUSDT": "unknown",
            "OLDUSDT": "crypto",
        }

    def test_tardis_rows_parse_from_the_id(self):
        assert parse_contract("bitget-perpetual-usdc", {"id": "ETHPERP", "type": "perpetual"}).internal_id == "perpetual-ETH-USDC:USDC"
        assert parse_contract("bitget-perpetual-usdt", {"id": "ETHUSDT", "type": "perpetual"}).internal_id == "perpetual-ETH-USDT:USDT"


# ---------------------------------------------------------------------------
# Deribit
# ---------------------------------------------------------------------------


def _deribit(name, base, quote, settle, kind, size, underlying="crypto", period="perpetual", expiry=32503708800000, active=True):
    return {
        "instrument_name": name, "base_currency": base, "quote_currency": quote,
        "settlement_currency": settle, "instrument_type": kind, "contract_size": size,
        "underlying_type": underlying, "settlement_period": period,
        "expiration_timestamp": expiry, "is_active": active,
    }


DERIBIT = {"result": [
    _deribit("BTC-PERPETUAL", "BTC", "USD", "BTC", "reversed", 10.0),
    _deribit("BTC_USDC-PERPETUAL", "BTC", "USDC", "USDC", "linear", 0.0001),
    _deribit("COIN50_USDC-PERPETUAL", "COIN50", "USDC", "USDC", "linear", 0.01, underlying="crypto_index"),
    _deribit("AAPL_USDC-PERPETUAL", "AAPL", "USDC", "USDC", "linear", 0.01, underlying="equity"),
    _deribit("SPY_USDC-PERPETUAL", "SPY", "USDC", "USDC", "linear", 0.01, underlying="equity_etf"),
    _deribit("GOLD_USDC-PERPETUAL", "GOLD", "USDC", "USDC", "linear", 0.001, underlying="commodity"),
    _deribit("OPENAI_USDC-PERPETUAL", "OPENAI", "USDC", "USDC", "linear", 0.01, underlying="preipo"),
    _deribit("PONS_USDC-PERPETUAL", "PONS", "USDC", "USDC", "linear", 1, active=False),
    _deribit("BTC-16OCT26", "BTC", "USD", "BTC", "reversed", 10.0, period="day", expiry=1792137600000),
    _deribit("ETH_USDC-16OCT26", "ETH", "USDC", "USDC", "linear", 0.001, period="day", expiry=1792137600000),
]}


class TestDeribit:
    def test_linear_contracts_settle_in_their_quote(self):
        from atlas.exchange_definitions.deribit import fetch_deribit_futures, fetch_deribit_perpetual

        perps = _ids("deribit-perpetual", _fetch(fetch_deribit_perpetual, {"/get_instruments": DERIBIT}))
        dated = _ids("deribit-futures", _fetch(fetch_deribit_futures, {"/get_instruments": DERIBIT}))
        assert perps["BTC-PERPETUAL"] == "perpetual-BTC-USD:BTC"
        assert perps["BTC_USDC-PERPETUAL"] == "perpetual-BTC-USDC:USDC"
        assert "PONS_USDC-PERPETUAL" not in perps  # inactive
        assert dated == {
            "BTC-16OCT26": "future-BTC-USD:BTC-20261016",
            "ETH_USDC-16OCT26": "future-ETH-USDC:USDC-20261016",
        }

    def test_tardis_rows_infer_linear_from_the_name(self):
        assert parse_contract("deribit", {"id": "SOL_USDC-PERPETUAL", "type": "perpetual"}).internal_id == "perpetual-SOL-USDC:USDC"
        assert parse_contract("deribit", {"id": "ETH_USDC-28MAR25", "type": "future"}).internal_id == "future-ETH-USDC:USDC-20250328"
        assert parse_contract("deribit-perpetual", {"id": "ETH-PERPETUAL", "type": "perpetual"}).internal_id == "perpetual-ETH-USD:ETH"

    def test_contract_size_and_underlying(self):
        from atlas.exchange_definitions.deribit import fetch_deribit_perpetual

        rows = _fetch(fetch_deribit_perpetual, {"/get_instruments": DERIBIT})
        by_id = {sd["id"]: sd for sd in rows}
        assert (by_id["BTC-PERPETUAL"]["contract_size"], by_id["BTC-PERPETUAL"]["quantity_unit"]) == (10.0, "quote")
        assert (by_id["BTC_USDC-PERPETUAL"]["contract_size"], by_id["BTC_USDC-PERPETUAL"]["quantity_unit"]) == (0.0001, "base")
        assert _classes(rows) == {
            "BTC-PERPETUAL": "crypto",
            "BTC_USDC-PERPETUAL": "crypto",
            "COIN50_USDC-PERPETUAL": "index",
            "AAPL_USDC-PERPETUAL": "equity",
            "SPY_USDC-PERPETUAL": "equity",
            "GOLD_USDC-PERPETUAL": "commodity",
            "OPENAI_USDC-PERPETUAL": "pre_market",
        }


# ---------------------------------------------------------------------------
# WhiteBIT
# ---------------------------------------------------------------------------

WHITEBIT_FUTURES = {"result": [
    {"ticker_id": "BTC_PERP", "stock_currency": "BTC", "money_currency": "USDT", "product_type": "Perpetual", "index_currency": "BTC"},
    {"ticker_id": "AAPL_PERP", "stock_currency": "AAPL", "money_currency": "USDT", "product_type": "Perpetual", "index_currency": "AAPL"},
    {"ticker_id": "XAU_PERP", "stock_currency": "XAU", "money_currency": "USDT", "product_type": "Perpetual", "index_currency": "XAU"},
    {"ticker_id": "SPY_PERP", "stock_currency": "SPY", "money_currency": "USDT", "product_type": "Perpetual", "index_currency": "SPY"},
]}
WHITEBIT_MARKETS = [
    {"name": "BTC_PERP", "type": "futures", "isTradFiFutures": False},
    {"name": "BTC_USDT", "type": "spot", "isTradFiFutures": False},
    {"name": "SPY_PERP", "type": "futures", "isTradFiFutures": True},
]


class TestWhitebit:
    def test_ids_and_tradfi_classes(self):
        from atlas.exchange_definitions.whitebit import fetch_whitebit_perpetual

        rows = _fetch(fetch_whitebit_perpetual, {"/public/futures": WHITEBIT_FUTURES, "/public/markets": WHITEBIT_MARKETS})
        assert _ids("whitebit-perpetual", rows)["BTC_PERP"] == "perpetual-BTC-USDT:USDT"
        # TradFi perps are missing from /markets (or flagged there).
        assert _classes(rows) == {
            "BTC_PERP": "crypto",
            "AAPL_PERP": "equity",
            "XAU_PERP": "commodity",
            "SPY_PERP": "equity",
        }


# ---------------------------------------------------------------------------
# Crypto.com
# ---------------------------------------------------------------------------


def _cdc(symbol, base, inst, product, index, expiry=0, tradable=True):
    return {
        "symbol": symbol, "base_ccy": base, "quote_ccy": "USD", "inst_type": inst,
        "product_type": product, "underlying_symbol": index, "expiry_timestamp_ms": expiry,
        "contract_size": "1", "tradable": tradable,
    }


CDC = {"result": {"data": [
    {"symbol": "BTC_USD", "base_ccy": "BTC", "quote_ccy": "USD", "inst_type": "CCY_PAIR", "product_type": "DIGITAL_CURRENCIES", "tradable": True},
    _cdc("BTCUSD-PERP", "BTC", "PERPETUAL_SWAP", "DIGITAL_CURRENCIES", "BTCUSD-INDEX"),
    _cdc("AAPLUSD-PERP", "AAPL", "PERPETUAL_SWAP", "EQUITY", "AAPLUSD-INDEX"),
    _cdc("SPYUSD-PERP", "SPY", "PERPETUAL_SWAP", "EQUITY_IND", "SPYUSD-INDEX"),
    _cdc("XAUUSD-PERP", "XAU", "PERPETUAL_SWAP", "COMMODITIES", "XAUUSD-INDEX"),
    _cdc("OPENAIIPOUSD-PERP", "OPENAIIPO", "PERPETUAL_SWAP", "PRE_IPO", "OPENAIIPOUSD-INDEX"),
    _cdc("OLDUSD-PERP", "OLD", "PERPETUAL_SWAP", "DIGITAL_CURRENCIES", "OLDUSD-INDEX", tradable=False),
    # Some dated futures carry no product_type.
    _cdc("BTCUSD-261127", "BTC", "FUTURE", None, "BTCUSD-INDEX", expiry=1795766400000),
]}}


class TestCryptocom:
    def test_ids(self):
        from atlas.exchange_definitions.crypto_com import fetch_cryptocom_futures, fetch_cryptocom_perpetual

        perps = _ids("cryptocom-perpetual", _fetch(fetch_cryptocom_perpetual, {"/get-instruments": CDC}))
        dated = _ids("cryptocom-futures", _fetch(fetch_cryptocom_futures, {"/get-instruments": CDC}))
        assert perps["BTCUSD-PERP"] == "perpetual-BTC-USD:USD"
        assert "OLDUSD-PERP" not in perps
        assert "BTC_USD" not in perps  # spot
        assert dated == {"BTCUSD-261127": "future-BTC-USD:USD-20261127"}

    def test_underlying(self):
        from atlas.exchange_definitions.crypto_com import fetch_cryptocom_futures, fetch_cryptocom_perpetual

        assert _classes(_fetch(fetch_cryptocom_perpetual, {"/get-instruments": CDC})) == {
            "BTCUSD-PERP": "crypto",
            "AAPLUSD-PERP": "equity",
            "SPYUSD-PERP": "equity",
            "XAUUSD-PERP": "commodity",
            "OPENAIIPOUSD-PERP": "pre_market",
        }
        # The untagged future takes the class of the index it tracks.
        assert _classes(_fetch(fetch_cryptocom_futures, {"/get-instruments": CDC})) == {"BTCUSD-261127": "crypto"}

    def test_tardis_rows_parse_from_the_id(self):
        assert parse_contract("cryptocom-perpetual", {"id": "ETHUSD-PERP", "type": "perpetual"}).internal_id == "perpetual-ETH-USD:USD"
        assert parse_contract("cryptocom-futures", {"id": "ETHUSD-250328", "type": "future"}).internal_id == "future-ETH-USD:USD-20250328"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exchange", "tardis_id"),
    [
        ("gateio-perpetual-usdt", "gate-io-futures"),
        ("gateio-perpetual-btc", "gate-io-futures"),
        ("gateio-futures-usdt", None),
        ("htx-perpetual-usdt", "huobi-dm-linear-swap"),
        ("mexc-perpetual", None),
        ("bitget-perpetual-usdt", "bitget-futures"),
        ("bitget-perpetual-usdc", "bitget-futures"),
        ("bitget-perpetual-coin", None),
        ("bitget-futures-coin", None),
        ("deribit-perpetual", "deribit"),
        ("deribit-futures", "deribit"),
        ("whitebit-perpetual", None),
        ("cryptocom-perpetual", "crypto-com"),
        ("cryptocom-futures", "crypto-com"),
    ],
)
def test_tardis_coverage(exchange, tardis_id):
    from atlas.exchanges import is_on_tardis, to_tardis_exchange_id
    from atlas.update import EXCHANGES

    if tardis_id is None:
        assert not is_on_tardis(exchange)
    else:
        assert is_on_tardis(exchange)
        assert to_tardis_exchange_id(exchange) == tardis_id
    assert exchange in EXCHANGES
