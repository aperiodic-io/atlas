from atlas.update import _drop_none_fields, _merge_existing_fields


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

    _merge_existing_fields(symbols, existing_by_id)

    assert symbols[0]["first_capture"] == "2020-01-01T00:00:00.000Z"
    assert symbols[0]["custom_metadata"] == "from-tardis"


def test_merge_existing_fields_does_not_override_source_values() -> None:
    symbols = [{"id": "BTCUSDT", "type": "spot", "first_capture": "2024-01-01T00:00:00.000Z"}]
    existing_by_id = {
        "BTCUSDT": {
            "id": "BTCUSDT",
            "type": "spot",
            "first_capture": "2020-01-01T00:00:00.000Z",
        }
    }

    _merge_existing_fields(symbols, existing_by_id)

    assert symbols[0]["first_capture"] == "2024-01-01T00:00:00.000Z"


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

    _drop_none_fields(symbols, {"margin", "delivery_date"})

    assert "margin" not in symbols[0]
    assert "delivery_date" not in symbols[0]
    assert symbols[0]["first_capture"] is None
    assert symbols[1]["margin"] == "USDT"
    assert symbols[1]["delivery_date"] == "2026-01-01T00:00:00"
