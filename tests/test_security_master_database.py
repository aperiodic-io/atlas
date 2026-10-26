import json
from datetime import datetime
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


def test_okx_perps_snapshot_keeps_delisted_usdc_contract_sizes() -> None:
    """OKX's USDC swaps (2022-10-18 to 2025-12-12) were 0.0001 BTC and 0.001 ETH a contract
    for their whole life, per OKX's listing notice: not their USDT twins' sizes. Delisted,
    no snapshot refreshes them."""
    rows = json.loads(
        (Path(__file__).resolve().parents[1] / "atlas" / "data" / "okx-perps.json").read_text()
    )
    by_id = {row["id"]: row for row in rows}

    assert by_id["BTC-USDC-SWAP"]["contract_size"] == 0.0001
    assert "contract_size_history" not in by_id["BTC-USDC-SWAP"]
    assert by_id["ETH-USDC-SWAP"]["contract_size"] == 0.001
    assert "contract_size_history" not in by_id["ETH-USDC-SWAP"]
    assert by_id["BTC-USDC-SWAP"]["end_date"] == "2025-12-12T00:00:00.000Z"
    assert by_id["ETH-USDC-SWAP"]["end_date"] == "2025-12-12T00:00:00.000Z"


def test_okx_perps_snapshot_keeps_delisted_unified_margin_contract_sizes() -> None:
    """OKX's USD-margined `*-USD_UM-SWAP` perps are delisted, so the instruments endpoint no
    longer returns them and no snapshot would ever fill their size. Without it open interest
    prices at one coin a contract. Sizes are those of the same underlying's dated
    unified-margin futures (`BTC-USD_UM-261030` and kin), not the USDT swaps': ETH is 0.001
    here against 0.1 on `ETH-USDT-SWAP`, SOL 0.1 against 1."""
    rows = json.loads(
        (Path(__file__).resolve().parents[1] / "atlas" / "data" / "okx-perps.json").read_text()
    )
    by_id = {row["id"]: row for row in rows}

    expected = {
        "BTC-USD_UM-SWAP": 0.01,
        "ETH-USD_UM-SWAP": 0.001,
        "SOL-USD_UM-SWAP": 0.1,
        "XAU-USD_UM-SWAP": 0.001,
    }
    for symbol_id, size in expected.items():
        assert by_id[symbol_id]["contract_size"] == size, symbol_id
        assert "contract_size_history" not in by_id[symbol_id], symbol_id
        assert by_id[symbol_id]["end_date"], symbol_id


def test_symbol_ids_includes_mid_window_listing(tmp_path: Path) -> None:
    _write_exchange(
        tmp_path,
        "okx-perps",
        [
            {
                "id": "0G-USDT-SWAP",
                "first_capture": "2025-09-22T00:00:00.000Z",
            }
        ],
    )

    sm = SecurityMaster.load(data_dir=tmp_path)

    symbols = sm.symbol_ids(
        exchange="okx-perps",
        first_capture=datetime(2025, 9, 1),
        end_date=datetime(2025, 10, 1),
    )

    assert symbols == ["0G-USDT-SWAP"]


def test_symbol_ids_excludes_listing_that_starts_after_window(tmp_path: Path) -> None:
    _write_exchange(
        tmp_path,
        "okx-perps",
        [
            {
                "id": "FUTURE-USDT-SWAP",
                "first_capture": "2025-10-05T00:00:00.000Z",
            }
        ],
    )

    sm = SecurityMaster.load(data_dir=tmp_path)

    symbols = sm.symbol_ids(
        exchange="okx-perps",
        first_capture=datetime(2025, 9, 1),
        end_date=datetime(2025, 10, 1),
    )

    assert symbols == []


def test_symbol_ids_excludes_listing_that_ended_before_window(tmp_path: Path) -> None:
    _write_exchange(
        tmp_path,
        "okx-perps",
        [
            {
                "id": "DELISTED-USDT-SWAP",
                "first_capture": "2024-01-01T00:00:00.000Z",
                "end_date": "2025-08-15T00:00:00.000Z",
            }
        ],
    )

    sm = SecurityMaster.load(data_dir=tmp_path)

    symbols = sm.symbol_ids(
        exchange="okx-perps",
        first_capture=datetime(2025, 9, 1),
        end_date=datetime(2025, 10, 1),
    )

    assert symbols == []


def test_security_master_returns_instrument_metadata(tmp_path) -> None:
    data = [
        {
            "id": "BTCUSD",
            "type": "perpetual",
            "first_capture": "2020-01-01T00:00:00.000Z",
            "internal_id": "perpetual-BTC-USD:BTC",
            "contract_size": 1.0,
            "quantity_unit": "quote",
        }
    ]
    (tmp_path / "bybit-perps-coin.json").write_text(json.dumps(data))

    metadata = SecurityMaster.load(data_dir=tmp_path).instrument_metadata(
        "bybit-perps-coin", "BTCUSD"
    )

    assert metadata is not None
    assert metadata["quantity_unit"] == "quote"
    metadata["quantity_unit"] = "base"
    assert (
        SecurityMaster.load(data_dir=tmp_path)
        .instrument_metadata("bybit-perps-coin", "BTCUSD")["quantity_unit"]
        == "quote"
    )


def test_security_master_ignores_non_exchange_json_files(tmp_path: Path) -> None:
    _write_exchange(
        tmp_path,
        "binance-spot",
        [
            {
                "id": "BTCUSDT",
                "first_capture": "2024-01-01T00:00:00.000Z",
            }
        ],
    )
    (tmp_path / "cmc_mappings.json").write_text(
        json.dumps({"schema_version": 1, "mappings": []})
    )

    symbols = SecurityMaster.load(data_dir=tmp_path).symbol_ids(
        exchange="binance-spot",
        first_capture=datetime(2024, 1, 1),
        end_date=datetime(2024, 2, 1),
    )

    assert symbols == ["BTCUSDT"]
