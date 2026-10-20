import json
from datetime import datetime
from pathlib import Path

import pytest

from atlas import Asset, AssetClass, AssetRegistry, SecurityMaster


def _write(tmp_path: Path, name: str, rows: list[dict]) -> None:
    (tmp_path / f"{name}.json").write_text(json.dumps(rows))


@pytest.fixture
def sm(tmp_path: Path) -> SecurityMaster:
    registry = AssetRegistry(tmp_path / "symbology" / "assets.json")
    registry.add(Asset("BTC", "BTC", "Bitcoin", AssetClass.crypto, cmc_id=1, cmc_slug="bitcoin", categories=("Layer 1",)))
    registry.add(Asset("USDT", "USDT", "Tether USDt", AssetClass.crypto, cmc_id=825))
    registry.add(Asset("PEPE", "PEPE", "Pepe", AssetClass.crypto, cmc_id=24478, categories=("Memes",)))
    registry.add(Asset("LUNA", "LUNA", "Terra", AssetClass.crypto, cmc_id=20314))
    registry.add(Asset("LUNC", "LUNC", "Terra Classic", AssetClass.crypto, cmc_id=4172))
    registry.save()
    _write(
        tmp_path,
        "binance-spot",
        [
            {
                "id": "btcusdt",
                "first_capture": "2017-01-01T00:00:00.000Z",
                "symbol": "BTC",
                "denominator": "USDT",
                "internal_id": "spot-BTC-USDT",
                "base_asset_id": "BTC",
                "quote_asset_id": "USDT",
                "unified_id": "spot-BTC-USDT",
            },
            {
                "id": "lunausdt",
                "first_capture": "2021-01-28T00:00:00.000Z",
                "symbol": "LUNA",
                "denominator": "USDT",
                "internal_id": "spot-LUNA-USDT",
                "base_asset_id": "LUNA",
                "quote_asset_id": "USDT",
                "base_asset_history": [{"asset_id": "LUNC", "until": "2022-05-28T00:00:00Z"}],
                "unified_id": "spot-LUNA-USDT",
            },
        ],
    )
    _write(
        tmp_path,
        "binance-futures",
        [
            {
                "id": "btcusdt",
                "first_capture": "2019-09-01T00:00:00.000Z",
                "type": "perpetual",
                "symbol": "BTC",
                "denominator": "USDT",
                "margin": "USDT",
                "internal_id": "perpetual-BTC-USDT:USDT",
                "base_asset_id": "BTC",
                "quote_asset_id": "USDT",
                "margin_asset_id": "USDT",
                "unified_id": "perpetual-BTC-USDT:USDT",
            },
            {
                "id": "1000pepeusdt",
                "first_capture": "2023-05-05T00:00:00.000Z",
                "type": "perpetual",
                "symbol": "1000PEPE",
                "denominator": "USDT",
                "margin": "USDT",
                "internal_id": "perpetual-1000PEPE-USDT:USDT",
                "base_asset_id": "PEPE",
                "base_multiplier": 1000,
                "quote_asset_id": "USDT",
                "margin_asset_id": "USDT",
                "unified_id": "perpetual-PEPE-USDT:USDT",
            },
        ],
    )
    _write(
        tmp_path,
        "bybit-perps-usd",
        [
            {
                "id": "BTCUSDT",
                "first_capture": "2020-03-25T00:00:00.000Z",
                "type": "perpetual",
                "symbol": "BTC",
                "denominator": "USDT",
                "margin": "USDT",
                "internal_id": "perpetual-BTC-USDT:USDT",
                "base_asset_id": "BTC",
                "quote_asset_id": "USDT",
                "margin_asset_id": "USDT",
                "unified_id": "perpetual-BTC-USDT:USDT",
            },
            {
                "id": "UNMAPPEDUSDT",
                "first_capture": "2020-03-25T00:00:00.000Z",
                "type": "perpetual",
                "symbol": "UNMAPPED",
                "denominator": "USDT",
                "internal_id": "perpetual-UNMAPPED-USDT:USDT",
            },
        ],
    )
    return SecurityMaster.load(data_dir=tmp_path)


def test_registry_is_loaded_from_the_data_dir(sm):
    assert len(sm.assets) == 5
    assert sm.asset("BTC").name == "Bitcoin"
    assert sm.asset("NOPE") is None
    assert sm.asset_by_cmc_id(24478).asset_id == "PEPE"


def test_instrument_legs(sm):
    assert sm.base_asset("binance-futures", "btcusdt").asset_id == "BTC"
    assert sm.quote_asset("binance-futures", "btcusdt").asset_id == "USDT"
    assert sm.margin_asset("binance-futures", "btcusdt").asset_id == "USDT"
    assert sm.margin_asset("binance-spot", "btcusdt") is None
    assert sm.base_asset("bybit-perps-usd", "UNMAPPEDUSDT") is None
    assert sm.base_asset("nope", "nope") is None


def test_base_asset_honours_history(sm):
    assert sm.base_asset("binance-spot", "lunausdt").asset_id == "LUNA"
    assert sm.base_asset("binance-spot", "lunausdt", at=datetime(2022, 1, 1)).asset_id == "LUNC"
    assert sm.base_asset("binance-spot", "lunausdt", at=datetime(2023, 1, 1)).asset_id == "LUNA"


def test_unified_id_lookups(sm):
    assert sm.unified_id("binance-futures", "1000pepeusdt") == "perpetual-PEPE-USDT:USDT"
    assert sm.by_unified_id("perpetual-BTC-USDT:USDT") == [
        ("binance-futures", "btcusdt"),
        ("bybit-perps-usd", "BTCUSDT"),
    ]
    assert sm.by_unified_id("nope") == []


def test_instruments_and_exchanges_for_an_asset(sm):
    assert sm.instruments_for_asset("BTC") == [
        ("binance-futures", "btcusdt"),
        ("binance-spot", "btcusdt"),
        ("bybit-perps-usd", "BTCUSDT"),
    ]
    assert sm.instruments_for_asset("BTC", exchange="bybit-perps-usd") == [("bybit-perps-usd", "BTCUSDT")]
    assert sm.exchanges_for_asset("BTC") == ["binance-futures", "binance-spot", "bybit-perps-usd"]
    # Historical base assets count too.
    assert sm.instruments_for_asset("LUNC") == [("binance-spot", "lunausdt")]


def test_assets_by_category_and_ticker(sm):
    assert [asset.asset_id for asset in sm.assets_in_category("Memes")] == ["PEPE"]
    assert [asset.asset_id for asset in sm.find_assets("btc")] == ["BTC"]
    with pytest.raises(ValueError):
        sm.assets_in_category("Not A Category")


def test_load_without_a_registry_still_works(tmp_path):
    _write(tmp_path, "binance-spot", [{"id": "x", "first_capture": "2020-01-01T00:00:00Z", "internal_id": "spot-X-Y"}])
    sm = SecurityMaster.load(data_dir=tmp_path)
    assert len(sm.assets) == 0
    assert sm.base_asset("binance-spot", "x") is None


def test_bundled_registry_is_consistent_with_bundled_snapshots():
    sm = SecurityMaster.load()
    referenced: set[str] = set()
    for (exchange, original_id) in sm._metadata_map:
        row = sm.instrument_metadata(exchange, original_id)
        for key in ("base_asset_id", "quote_asset_id", "margin_asset_id"):
            if (asset_id := row.get(key)) is not None:
                referenced.add(asset_id)
        for segment in row.get("base_asset_history", []):
            referenced.add(segment["asset_id"])
    missing = sorted(asset_id for asset_id in referenced if sm.asset(asset_id) is None)
    assert missing == []
