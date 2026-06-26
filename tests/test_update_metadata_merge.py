from atlas.update import _append_contract_size_change, _drop_none_fields, _merge_existing_fields


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


def test_drop_none_fields_removes_only_requested_none_fields() -> None:
    symbols = [
        {
            "id": "BTCUSDT",
            "margin": None,
            "delivery_date": None,
            "first_capture": None,
            "contract_type": "spot",
        },
        {
            "id": "ETHUSDT",
            "margin": "USDT",
            "delivery_date": "2026-01-01T00:00:00",
            "contract_type": "perpetual",
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


def test_append_contract_size_change_creates_history_when_none_exists() -> None:
    sd = {"id": "BTC-USDT-SWAP"}
    result = _append_contract_size_change(sd, new_size=0.001, effective_from="2026-01-01T00:00:00Z")
    assert result["contract_size_history"] == [{"effective_from": "2026-01-01T00:00:00Z", "value": 0.001}]


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


def test_append_contract_size_change_does_not_append_when_value_same() -> None:
    sd = {
        "id": "BTC-USDT-SWAP",
        "contract_size_history": [{"effective_from": "2019-12-04T00:00:00Z", "value": 0.001}],
    }
    result = _append_contract_size_change(sd, new_size=0.001, effective_from="2026-01-01T00:00:00Z")
    assert len(result["contract_size_history"]) == 1


def test_append_contract_size_change_does_not_mutate_input() -> None:
    original_history = [{"effective_from": "2019-12-04T00:00:00Z", "value": 0.0001}]
    sd = {"id": "BTC-USDT-SWAP", "contract_size_history": original_history}
    _append_contract_size_change(sd, new_size=0.001, effective_from="2026-01-01T00:00:00Z")
    assert len(original_history) == 1
