"""The README's examples, against the bundled data.

Only identities are asserted, not counts that move with every daily refresh,
so a symbology change that breaks a documented example fails CI.
"""

from __future__ import annotations

import pytest

from atlas import AssetClass, SecurityMaster


@pytest.fixture(scope="module")
def sm() -> SecurityMaster:
    return SecurityMaster.load()


def test_one_tesla_many_spellings(sm):
    for exchange, original_id in [
        ("hyperliquid-perps", "xyz:TSLA"),
        ("lighter-perps", "112"),
        ("avantis-perps", "86"),
        ("aster-perps", "tslausdt"),
        ("grvt-perps", "TSLA_USDT_Perp"),
        ("binance-futures", "tslausdt"),
    ]:
        assert sm.base_asset(exchange, original_id).asset_id == "TSLA", (exchange, original_id)
    assert sm.asset("TSLA").asset_class is AssetClass.equity
    assert sm.by_exchange_and_original_id("lighter-perps", "112") == "perpetual-TSLA-USDC:USDC"
    assert {("avantis-perps", "86"), ("lighter-perps", "112"), ("hyperliquid-perps", "xyz:TSLA")} <= set(
        sm.by_unified_id("perpetual-TSLA-USDC:USDC")
    )


def test_a_ticker_is_not_an_asset(sm):
    for exchange, original_id in [
        ("aster-perps", "spxusdt"),
        ("lighter-perps", "42"),
        ("apex-perps", "SPXUSDT"),
        ("extended-perps", "SPX-USD"),
        ("variational-perps", "SPX"),
    ]:
        assert sm.base_asset(exchange, original_id).name == "SPX6900"
    assert sm.base_asset("hyperliquid-perps", "xyz:SP500").asset_class is AssetClass.index
    assert sm.base_asset("extended-perps", "MON-USD").name == "Monad"
    assert sm.base_asset("bitget-perps-usdt", "AAPLUSDT").asset_id == "AAPL"


def test_fx_conventions(sm):
    assert sm.base_asset("pacifica-perps", "EURUSD").asset_id == "EURUSD"
    assert sm.base_asset("hyperliquid-perps", "xyz:JPY").asset_id == "USDJPY"
    lighter_fx = {
        sm.base_asset(*key).asset_id for key in sm.instruments(exchange="lighter-perps", underlying="forex")
    }
    assert {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF"} <= lighter_fx


def test_delisted_rwa_contracts_keep_their_window(sm):
    spacex = sm.instrument_metadata("lighter-perps", "173")
    assert spacex["symbol"] == "SPACEX"
    assert spacex["underlying"] == "pre_market"
    assert spacex["first_capture"].startswith("2026-05-08")
    assert spacex["end_date"].startswith("2026-06-19")


def test_multiplier_example(sm):
    row = sm.instrument_metadata("pacifica-perps", "kBONK")
    assert (row["base_asset_id"], row["base_multiplier"]) == ("BONK", 1000)
