import json
from datetime import UTC, datetime

from integrations.cmc_id_probe import (
    CmcAsset,
    CmcCatalogue,
    CatalogueDiagnostics,
    PriceObservation,
)
from integrations.cmc_probe_metadata import populate_cmc_probe_metadata
from integrations.cmc_probe_metadata import remove_cmc_probe_metadata


def test_populate_cmc_probe_metadata_writes_only_probe_evidence(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "binance-futures.json"
    target.write_text(
        json.dumps(
            [
                {"id": "btcusdt", "symbol": "BTC"},
                {"id": "1000cheemsusdt", "symbol": "1000CHEEMS"},
            ]
        )
    )
    observed_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    catalogue = CmcCatalogue(
        assets=(
            CmcAsset(1, "BTC", "bitcoin", 100, observed_at, True),
            CmcAsset(2, "CHEEMS", "cheems", 0.05, observed_at, True),
        ),
        diagnostics=CatalogueDiagnostics(3, 3, 3, (), 0, False),
    )
    observations_by_exchange = {
        "binance-futures": {
            "BTC": PriceObservation(100, "USDT", observed_at, "binance-futures", "BTCUSDT"),
            "1000CHEEMS": PriceObservation(
                50, "USDT", observed_at, "binance-futures", "1000CHEEMSUSDT"
            ),
        }
    }

    stats = populate_cmc_probe_metadata(
        data_dir,
        catalogue,
        observations_by_exchange,
        {
            "BTC": {
                "assetCode": "BTC",
                "assetName": "Bitcoin",
                "delisted": False,
                "enLink": "https://binance.example/BTC",
                "commissionRate": 0,
            }
        },
    )

    rows = json.loads(target.read_text())
    assert rows[0]["cmc_id"] == 1
    assert rows[1]["cmc_id"] == 2
    assert "cmc_probe" not in rows[0]
    assert "cmc_probe" not in rows[1]
    probe_rows = json.loads((data_dir / "cmc_probe" / "binance-futures.json").read_text())
    assert probe_rows[0]["cmc_probe"]["status"] == "price_compatible"
    assert probe_rows[0]["binance_public_asset"] == {
        "source": "binance-public-asset-endpoint",
        "assetCode": "BTC",
        "assetName": "Bitcoin",
        "enLink": "https://binance.example/BTC",
        "delisted": False,
    }
    assert probe_rows[1]["cmc_lookup_symbol"] == "CHEEMS"
    assert stats["binance-futures"]["price_compatible"] == 2


def test_populate_cmc_probe_metadata_preserves_existing_cmc_ids(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "binance-futures.json"
    target.write_text(
        json.dumps(
            [
                {"id": "btcusdt", "symbol": "BTC", "cmc_id": 999},
                {"id": "missingusdt", "symbol": "MISSING", "cmc_id": 998},
            ]
        )
    )
    observed_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    catalogue = CmcCatalogue(
        assets=(CmcAsset(1, "BTC", "bitcoin", 100, observed_at, True),),
        diagnostics=CatalogueDiagnostics(1, 1, 1, (), 0, False),
    )
    observations_by_exchange = {
        "binance-futures": {
            "BTC": PriceObservation(
                100, "USDT", observed_at, "binance-futures", "BTCUSDT"
            )
        }
    }

    populate_cmc_probe_metadata(data_dir, catalogue, observations_by_exchange)

    rows = {row["id"]: row for row in json.loads(target.read_text())}
    assert rows["btcusdt"]["cmc_id"] == 999
    assert rows["missingusdt"]["cmc_id"] == 998


def test_remove_cmc_probe_metadata_is_scoped_to_requested_exchanges(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-spot.json").write_text(
        json.dumps([{"id": "BTC", "cmc_probe": {}}])
    )
    (data_dir / "bybit-spot.json").write_text(
        json.dumps([{"id": "BTC", "cmc_probe": {}}])
    )

    removed = remove_cmc_probe_metadata(data_dir, {"bybit-spot"})

    assert removed == {"bybit-spot": 1}
    assert "cmc_probe" in json.loads((data_dir / "binance-spot.json").read_text())[0]
    assert "cmc_probe" not in json.loads((data_dir / "bybit-spot.json").read_text())[0]
