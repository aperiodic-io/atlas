import json
from datetime import UTC, datetime

from integrations.cmc_id_probe import (
    CmcAsset,
    CmcCatalogue,
    CatalogueDiagnostics,
    PriceObservation,
)
from integrations.cmc_probe_metadata import populate_cmc_probe_metadata


def test_populate_cmc_probe_metadata_writes_only_probe_evidence(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "binance-futures.json"
    target.write_text(
        json.dumps(
            [
                {"id": "btcusdt", "symbol": "BTC"},
                {"id": "ethusdt", "symbol": "ETH"},
            ]
        )
    )
    observed_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    catalogue = CmcCatalogue(
        assets=(
            CmcAsset(1, "BTC", "bitcoin", 100, observed_at, True),
            CmcAsset(2, "ETH", "ethereum", 50, observed_at, True),
            CmcAsset(3, "ETH", "wrapped-ethereum", 50.1, observed_at, True),
        ),
        diagnostics=CatalogueDiagnostics(3, 3, 3, (), 0, False),
    )
    observations = {
        "BTC": PriceObservation(100, "USDT", observed_at, "binance-spot", "BTCUSDT"),
        "ETH": PriceObservation(50, "USDT", observed_at, "binance-spot", "ETHUSDT"),
    }

    stats = populate_cmc_probe_metadata(data_dir, catalogue, observations)

    rows = json.loads(target.read_text())
    assert rows[0]["cmc_probe"]["status"] == "price_compatible"
    assert rows[0]["cmc_probe"]["cmc_id"] == 1
    assert "cmc_id" not in rows[0]
    assert rows[1]["cmc_probe"]["status"] == "multiple_price_matches"
    assert "cmc_id" not in rows[1]["cmc_probe"]
    assert stats["binance-futures"]["price_compatible"] == 1
    assert stats["binance-futures"]["multiple_price_matches"] == 1
