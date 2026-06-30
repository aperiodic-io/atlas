import json
from pathlib import Path

from atlas.database import SecurityMaster


def _write_exchange(tmp_path: Path, name: str, rows: list[dict]) -> None:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(rows, indent=2))


def test_exchanges_for_contract_matches_symbol_denominator_margin(tmp_path: Path) -> None:
    _write_exchange(
        tmp_path,
        "binance-spot",
        [
            {
                "id": "BTCUSDT",
                "first_capture": "2024-01-01T00:00:00.000Z",
                "symbol": "BTC",
                "denominator": "USDT",
                "margin": None,
                "internal_id": "spot-BTC-USDT",
            }
        ],
    )
    _write_exchange(
        tmp_path,
        "binance-futures",
        [
            {
                "id": "BTCUSDT",
                "first_capture": "2024-01-01T00:00:00.000Z",
                "symbol": "BTC",
                "denominator": "USDT",
                "margin": "USDT",
                "internal_id": "perpetual-BTC-USDT:USDT",
            }
        ],
    )
    _write_exchange(
        tmp_path,
        "okx-spot",
        [
            {
                "id": "BTC-USDT",
                "first_capture": "2024-01-01T00:00:00.000Z",
                "symbol": "BTC",
                "denominator": "USDT",
                "margin": None,
                "internal_id": "spot-BTC-USDT",
            }
        ],
    )

    sm = SecurityMaster.load(data_dir=tmp_path)

    assert sm.exchanges_for_contract("btc", "usdt", "usdt") == ["binance-futures"]
    assert sm.exchanges_for_contract("BTC", "USDT", None) == [
        "binance-spot",
        "okx-spot",
    ]


def test_okx_perps_snapshot_keeps_legacy_usdc_contract_sizes() -> None:
    rows = json.loads(
        (Path(__file__).resolve().parents[1] / "atlas" / "data" / "okx-perps.json").read_text()
    )
    by_id = {row["id"]: row for row in rows}

    assert by_id["BTC-USDC-SWAP"]["contract_size"] == 0.01
    assert by_id["BTC-USDC-SWAP"]["contract_size_history"] == [
        {"effective_from": "2019-12-04T00:00:00Z", "value": 0.0001},
        {"effective_from": "2020-03-20T08:00:00Z", "value": 0.01},
    ]
    assert by_id["ETH-USDC-SWAP"]["contract_size"] == 0.1
    assert by_id["ETH-USDC-SWAP"]["contract_size_history"] == [
        {"effective_from": "2019-12-04T00:00:00Z", "value": 0.001},
        {"effective_from": "2020-03-19T08:00:00Z", "value": 0.1},
    ]
