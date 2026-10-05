from atlas.update import (
    _append_contract_size_change,
    _drop_none_fields,
    _merge_existing_fields,
    _merge_symbol,
    _merge_missing_rows,
    _matches_exchange_constraints,
)
from atlas.update import _apply_snapshot_metadata
import json
import importlib
import io
from contextlib import redirect_stdout
from types import SimpleNamespace


def test_apply_snapshot_metadata_preserves_existing_lifecycle_fields() -> None:
    symbols = [
        {
            "id": "OLDUSDT",
            "availableSince": "2024-01-01T00:00:00.000Z",
            "availableTo": "2025-01-01T00:00:00.000Z",
        }
    ]

    mapped = _apply_snapshot_metadata(symbols)

    assert mapped == [
        {
            "id": "OLDUSDT",
            "first_capture": "2024-01-01T00:00:00.000Z",
            "end_date": "2025-01-01T00:00:00.000Z",
        }
    ]


def test_merge_existing_fields_keeps_metadata_when_source_missing() -> None:
    symbols = [{"id": "BTCUSDT", "type": "spot"}]
    existing_by_id = {
        "BTCUSDT": {
            "id": "BTCUSDT",
            "type": "spot",
            "first_capture": "2020-01-01T00:00:00.000Z",
            "end_date": None,
            "custom_metadata": "from-tardis",
        }
    }

    merged = _merge_existing_fields(symbols, existing_by_id)

    assert "first_capture" not in symbols[0]
    assert "custom_metadata" not in symbols[0]
    assert merged[0]["first_capture"] == "2020-01-01T00:00:00.000Z"
    assert merged[0]["custom_metadata"] == "from-tardis"


def test_merge_existing_fields_does_not_override_source_values() -> None:
    symbols = [{"id": "BTCUSDT", "type": "spot", "first_capture": "2024-01-01T00:00:00.000Z"}]
    existing_by_id = {
        "BTCUSDT": {
            "id": "BTCUSDT",
            "type": "spot",
            "first_capture": "2020-01-01T00:00:00.000Z",
        }
    }

    merged = _merge_existing_fields(symbols, existing_by_id)

    assert symbols[0]["first_capture"] == "2024-01-01T00:00:00.000Z"
    assert merged[0]["first_capture"] == "2024-01-01T00:00:00.000Z"


def test_tardis_refresh_replaces_recovered_capture_date() -> None:
    existing = {"HYPE/USDC": {"id": "HYPE/USDC", "first_capture": "2026-01-01T00:00:00Z"}}
    rows = _merge_existing_fields([{"id": "HYPE/USDC", "first_capture": "2025-01-01T00:00:00Z"}], existing, ignore_metadata=True)
    assert rows == [{"id": "HYPE/USDC", "first_capture": "2025-01-01T00:00:00Z"}]


def test_direct_refresh_retains_capture_date_until_replaced() -> None:
    existing = {"HYPE/USDC": {"id": "HYPE/USDC", "first_capture": "2026-01-01T00:00:00Z"}}
    rows = _merge_existing_fields([{"id": "HYPE/USDC"}], existing)
    assert rows == [existing["HYPE/USDC"]]
    refreshed = _merge_existing_fields([{"id": "HYPE/USDC", "first_capture": "2025-01-01T00:00:00Z"}], existing)
    assert refreshed == [{"id": "HYPE/USDC", "first_capture": "2025-01-01T00:00:00Z"}]


def test_new_direct_hyperliquid_rows_receive_stable_capture_dates(tmp_path, monkeypatch) -> None:
    updater = importlib.import_module("atlas.update")
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    source = SimpleNamespace(fetch_exchange=lambda _exchange: {"availableSymbols": [{"id": "NEW/USDC", "type": "spot"}]})
    assert updater.update(["hyperliquid-spot"], source) == []
    path = tmp_path / "hyperliquid-spot.json"
    original = json.loads(path.read_text())[0]
    assert original["first_capture"]
    assert "first_capture_source" not in original
    assert updater.update(["hyperliquid-spot"], source) == []
    assert json.loads(path.read_text())[0]["first_capture"] == original["first_capture"]


def test_partial_tardis_refresh_preserves_direct_capture_instances(tmp_path, monkeypatch) -> None:
    updater = importlib.import_module("atlas.update")
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    path = tmp_path / "hyperliquid-spot.json"
    path.write_text(json.dumps([
        {"id": "HYPE/USDC", "type": "spot", "first_capture": "2026-03-01T00:00:00Z", "cmc_id": 32196},
        {"id": "HFUN/USDC", "type": "spot", "first_capture": "2026-03-01T00:00:00Z", "cmc_id": 34624},
    ]))
    source = SimpleNamespace(fetch_exchange=lambda _exchange: {"availableSymbols": [
        {"id": "HYPE/USDC", "type": "spot", "availableSince": "2025-01-01T00:00:00Z"},
        {"id": "HFUN/USDC", "type": "spot"},
        {"id": "NEW/USDC", "type": "spot"},
    ]})
    assert updater.update(["hyperliquid-spot"], source) == []
    rows = {row["id"]: row for row in json.loads(path.read_text())}
    assert rows["HYPE/USDC"]["first_capture"] == "2025-01-01T00:00:00Z"
    assert rows["HFUN/USDC"]["first_capture"] == "2026-03-01T00:00:00Z"
    assert rows["HYPE/USDC"]["cmc_id"] == 32196
    assert rows["HFUN/USDC"]["cmc_id"] == 34624
    assert rows["NEW/USDC"]["first_capture"]
    assert all("first_capture_source" not in row for row in rows.values())


def test_update_progress_supports_ascii_terminals(tmp_path, monkeypatch) -> None:
    updater = importlib.import_module("atlas.update")
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    source = SimpleNamespace(fetch_exchange=lambda _exchange: {"availableSymbols": [{"id": "NEW/USDC", "type": "spot"}]})
    buffer = io.BytesIO()
    output = io.TextIOWrapper(buffer, encoding="ascii")
    with redirect_stdout(output):
        assert updater.update(["hyperliquid-spot"], source) == []
    output.flush()
    assert buffer.getvalue().decode("ascii")


def test_merge_existing_fields_preserves_existing_cmc_id() -> None:
    symbols = [
        {"id": "BTCUSDT", "type": "spot", "cmc_id": None},
        {"id": "ETHUSDT", "type": "spot", "cmc_id": 999999},
    ]
    existing_by_id = {
        "BTCUSDT": {"id": "BTCUSDT", "type": "spot", "cmc_id": 1},
        "ETHUSDT": {"id": "ETHUSDT", "type": "spot", "cmc_id": 1027},
    }

    merged = _merge_existing_fields(symbols, existing_by_id)

    assert merged[0]["cmc_id"] == 1
    assert merged[1]["cmc_id"] == 1027


def test_merge_symbol_preserves_locally_owned_fields() -> None:
    existing = {
        "id": "BTCUSDT",
        "cmc_id": 1,
        "contract_size_history": [{"effective_from": "2025-01-01", "value": 1.0}],
    }
    incoming = {
        "id": "BTCUSDT",
        "cmc_id": 999999,
        "contract_size_history": [],
        "type": "perpetual",
    }

    merged = _merge_symbol(existing, incoming)

    assert merged["cmc_id"] == 1
    assert merged["contract_size_history"] == existing["contract_size_history"]
    assert merged["type"] == "perpetual"


def test_merge_symbol_supports_other_protected_fields() -> None:
    merged = _merge_symbol(
        {"id": "BTCUSDT", "review_status": "approved"},
        {"id": "BTCUSDT", "review_status": "pending"},
        protected_fields=frozenset({"review_status"}),
    )

    assert merged["review_status"] == "approved"


def test_merge_existing_fields_can_skip_existing_metadata() -> None:
    symbols = [{"id": "BTCUSDT", "type": "spot"}]
    existing_by_id = {
        "BTCUSDT": {
            "id": "BTCUSDT",
            "type": "spot",
            "first_capture": "2020-01-01T00:00:00.000Z",
            "end_date": "2024-01-01T00:00:00.000Z",
            "custom_metadata": "from-tardis",
        }
    }

    merged = _merge_existing_fields(symbols, existing_by_id, ignore_metadata=True)

    assert "first_capture" not in symbols[0]
    assert "end_date" not in symbols[0]
    assert "custom_metadata" not in symbols[0]
    assert "first_capture" not in merged[0]
    assert "end_date" not in merged[0]
    assert merged[0]["custom_metadata"] == "from-tardis"


def test_product_constraints_remove_stale_cross_product_rows() -> None:
    merged = _merge_missing_rows(
        [{"id": "BTCUSDT", "type": "perpetual"}],
        [
            {"id": "BTCUSDT", "type": "perpetual"},
            {"id": "BTCUSDT-27MAR26", "type": "future"},
        ],
    )

    filtered = [
        row
        for row in merged
        if _matches_exchange_constraints(row, {"perpetual"}, None)
    ]

    assert filtered == [{"id": "BTCUSDT", "type": "perpetual"}]


def test_drop_none_fields_removes_only_requested_none_fields() -> None:
    symbols = [
        {
            "id": "BTCUSDT",
            "margin": None,
            "delivery_date": None,
            "first_capture": None,
        },
        {
            "id": "ETHUSDT",
            "margin": "USDT",
            "delivery_date": "2026-01-01T00:00:00",
        },
    ]

    cleaned = _drop_none_fields(symbols, {"margin", "delivery_date"})

    assert symbols[0]["margin"] is None
    assert symbols[0]["delivery_date"] is None
    assert "margin" not in cleaned[0]
    assert "delivery_date" not in cleaned[0]
    assert cleaned[0]["first_capture"] is None
    assert cleaned[1]["margin"] == "USDT"
    assert cleaned[1]["delivery_date"] == "2026-01-01T00:00:00"


def test_append_contract_size_change_no_history_field_when_first_value_seen() -> None:
    # A brand-new instrument with one size is constant by definition — no history needed
    sd = {"id": "BTC-USDT-SWAP"}
    result = _append_contract_size_change(sd, new_size=0.001, effective_from="2026-01-01T00:00:00Z")
    assert "contract_size_history" not in result


def test_append_contract_size_change_appends_when_value_differs() -> None:
    sd = {
        "id": "BTC-USDT-SWAP",
        "contract_size_history": [{"effective_from": "2019-12-04T00:00:00Z", "value": 0.0001}],
    }
    result = _append_contract_size_change(sd, new_size=0.001, effective_from="2026-01-01T00:00:00Z")
    assert result["contract_size_history"] == [
        {"effective_from": "2019-12-04T00:00:00Z", "value": 0.0001},
        {"effective_from": "2026-01-01T00:00:00Z", "value": 0.001},
    ]


def test_append_contract_size_change_drops_history_when_value_unchanged() -> None:
    # Previously had a single recorded value; daily update sees same value → stays constant
    sd = {
        "id": "BTC-USDT-SWAP",
        "contract_size_history": [{"effective_from": "2019-12-04T00:00:00Z", "value": 0.001}],
    }
    result = _append_contract_size_change(sd, new_size=0.001, effective_from="2026-01-01T00:00:00Z")
    assert "contract_size_history" not in result


def test_append_contract_size_change_does_not_mutate_input() -> None:
    original_history = [{"effective_from": "2019-12-04T00:00:00Z", "value": 0.0001}]
    sd = {"id": "BTC-USDT-SWAP", "contract_size_history": original_history}
    _append_contract_size_change(sd, new_size=0.001, effective_from="2026-01-01T00:00:00Z")
    assert len(original_history) == 1


def test_append_contract_size_change_omits_history_field_when_value_is_constant() -> None:
    # Single-entry history (never changed) is redundant — contract_size scalar already carries it
    sd = {"id": "BTC-USDT-SWAP"}
    result = _append_contract_size_change(sd, new_size=0.001, effective_from="2026-01-01T00:00:00Z")
    assert "contract_size_history" not in result


def test_append_contract_size_change_omits_history_field_when_unchanged_from_existing() -> None:
    sd = {
        "id": "BTC-USDT-SWAP",
        "contract_size_history": [{"effective_from": "2019-12-04T00:00:00Z", "value": 0.001}],
    }
    result = _append_contract_size_change(sd, new_size=0.001, effective_from="2026-01-01T00:00:00Z")
    assert "contract_size_history" not in result
