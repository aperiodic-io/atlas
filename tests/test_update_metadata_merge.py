from atlas.update import (
    _append_contract_size_change,
    _drop_none_fields,
    _merge_existing_fields,
    _merge_symbol,
    _merge_missing_rows,
    _matches_exchange_constraints,
)
import json
import importlib
import io
from contextlib import redirect_stdout
from types import SimpleNamespace


def test_merge_existing_fields_keeps_metadata_when_source_missing() -> None:
    symbols = [{"id": "BTCUSDT", "type": "spot"}]
    existing_by_id = {
        "BTCUSDT": {
            "id": "BTCUSDT",
            "type": "spot",
            "first_capture": "2020-01-01T00:00:00.000Z",
            "end_date": None,
            "custom_metadata": "from-history",
        }
    }

    merged = _merge_existing_fields(symbols, existing_by_id)

    assert "first_capture" not in symbols[0]
    assert "custom_metadata" not in symbols[0]
    assert merged[0]["first_capture"] == "2020-01-01T00:00:00.000Z"
    assert merged[0]["custom_metadata"] == "from-history"


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


def test_new_direct_rows_receive_capture_dates_on_every_exchange(tmp_path, monkeypatch) -> None:
    # SecurityMaster.load drops rows without first_capture, so a row new to the
    # file would otherwise be invisible to lookups.
    updater = importlib.import_module("atlas.update")
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    source = SimpleNamespace(fetch_exchange=lambda _exchange: {"availableSymbols": [
        {"id": "BTC_USDT", "type": "perpetual", "margin_asset": "USDT"}
    ]})
    assert updater.update(["mexc-perps"], source) == []
    path = tmp_path / "mexc-perps.json"
    original = json.loads(path.read_text())[0]
    assert original["first_capture"]
    assert updater.update(["mexc-perps"], source) == []
    assert json.loads(path.read_text())[0]["first_capture"] == original["first_capture"]


def test_merge_existing_fields_preserves_symbology_fields() -> None:
    from atlas.update import LOCALLY_OWNED_FIELDS
    from integrations.symbology import SYMBOLOGY_FIELDS

    assert set(SYMBOLOGY_FIELDS) <= LOCALLY_OWNED_FIELDS
    existing = {
        "BTCUSDT": {
            "id": "BTCUSDT",
            "type": "spot",
            "base_asset_id": "BTC",
            "quote_asset_id": "USDT",
            "unified_id": "spot-BTC-USDT",
            "base_asset_match": "cmc_market_pair",
            "category": ["Layer 1"],
        }
    }
    incoming = [{"id": "BTCUSDT", "type": "spot", "base_asset_id": "WRONG", "category": None}]
    merged = _merge_existing_fields(incoming, existing)
    assert merged[0]["base_asset_id"] == "BTC"
    assert merged[0]["unified_id"] == "spot-BTC-USDT"
    assert merged[0]["category"] == ["Layer 1"]


