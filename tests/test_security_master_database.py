import json
from pathlib import Path

from atlas.database import SecuritiesMaster


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

    sm = SecuritiesMaster.load(data_dir=tmp_path)

    assert sm.exchanges_for_contract("btc", "usdt", "usdt") == ["binance-futures"]
    assert sm.exchanges_for_contract("BTC", "USDT", None) == [
        "binance-spot",
        "okx-spot",
    ]
