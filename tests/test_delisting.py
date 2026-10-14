import importlib
import json
from types import SimpleNamespace

import pytest

updater = importlib.import_module("atlas.update")


def _complete_source(rows: list[dict]) -> SimpleNamespace:
    """A source that reports an exchange's whole current universe, like the APIs do."""
    return SimpleNamespace(
        complete_universe=True,
        fetch_exchange=lambda _exchange: {"availableSymbols": [dict(r) for r in rows]},
    )


def _write(tmp_path, exchange: str, rows: list[dict]):
    path = tmp_path / f"{exchange}.json"
    path.write_text(json.dumps(rows))
    return path


def _read(path) -> dict[str, dict]:
    return {row["id"]: row for row in json.loads(path.read_text())}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    return tmp_path


def _spot(sid: str, **extra) -> dict:
    return {"id": sid, "type": "spot", **extra}


LIVE = [_spot(f"live{i}usdt") for i in range(20)]


def test_exchange_api_is_the_default_source(data_dir, monkeypatch) -> None:
    monkeypatch.setattr(
        updater,
        "ExchangeApiSymbolSource",
        lambda: SimpleNamespace(
            complete_universe=True,
            fetch_exchange=lambda _e: {"availableSymbols": [_spot("btcusdt")]},
        ),
    )
    assert updater.update(["binance-spot"]) == []
    assert updater.parse_args([]).source == "exchange"


def test_daily_workflow_uses_exchange_source() -> None:
    from pathlib import Path

    workflow = Path(__file__).parents[1] / ".github/workflows/daily_update.yaml"
    assert "python -m atlas.update --source exchange" in workflow.read_text()


def test_vanished_row_is_kept_and_marked_missing_on_first_miss(data_dir) -> None:
    path = _write(data_dir, "binance-spot", [*LIVE, _spot("gonebtc", first_capture="2020-01-01T00:00:00.000Z")])

    assert updater.update(["binance-spot"], _complete_source(LIVE)) == []

    gone = _read(path)["gonebtc"]
    assert gone["first_capture"] == "2020-01-01T00:00:00.000Z"
    assert gone["missing_since"]
    assert "end_date" not in gone


def test_vanished_row_is_delisted_at_first_miss_when_still_missing_a_day_later(data_dir) -> None:
    path = _write(data_dir, "binance-spot", [*LIVE, _spot("gonebtc", missing_since="2026-10-12T02:00:00.000Z")])

    assert updater.update(["binance-spot"], _complete_source(LIVE)) == []

    gone = _read(path)["gonebtc"]
    assert gone["end_date"] == "2026-10-12T02:00:00.000Z"
    assert "missing_since" not in gone


def test_immediate_rerun_does_not_confirm_a_miss(data_dir) -> None:
    path = _write(data_dir, "binance-spot", [*LIVE, _spot("gonebtc")])
    source = _complete_source(LIVE)

    assert updater.update(["binance-spot"], source) == []
    first_miss = _read(path)["gonebtc"]["missing_since"]
    assert updater.update(["binance-spot"], source) == []

    gone = _read(path)["gonebtc"]
    assert gone["missing_since"] == first_miss
    assert "end_date" not in gone


def test_row_that_returns_after_one_miss_is_not_delisted(data_dir) -> None:
    path = _write(data_dir, "binance-spot", [*LIVE, _spot("flakybtc")])

    assert updater.update(["binance-spot"], _complete_source(LIVE)) == []
    assert updater.update(["binance-spot"], _complete_source([*LIVE, _spot("flakybtc")])) == []

    flaky = _read(path)["flakybtc"]
    assert "missing_since" not in flaky
    assert "end_date" not in flaky


def test_existing_end_date_is_not_touched_when_row_stays_missing(data_dir) -> None:
    path = _write(data_dir, "binance-spot", [*LIVE, _spot("oldbtc", end_date="2021-05-01T00:00:00.000Z")])

    assert updater.update(["binance-spot"], _complete_source(LIVE)) == []

    old = _read(path)["oldbtc"]
    assert old["end_date"] == "2021-05-01T00:00:00.000Z"
    assert "missing_since" not in old


LIVE_FUTURES = [{"id": f"BTC-USD-30{m:02d}25", "type": "future"} for m in range(1, 13)]


def test_expired_future_with_date_only_delivery_ends_after_delivery_day(data_dir) -> None:
    expired = {"id": "BTC-USD-200925", "type": "future", "delivery_date": "2020-09-25T00:00:00"}
    path = _write(data_dir, "okx-futures", [*LIVE_FUTURES, expired])

    assert updater.update(["okx-futures"], _complete_source(LIVE_FUTURES)) == []

    row = _read(path)["BTC-USD-200925"]
    assert row["end_date"] == "2020-09-26T00:00:00.000Z"
    assert "missing_since" not in row


def test_expired_future_with_exact_delivery_time_ends_then(data_dir) -> None:
    live = [{"id": f"BTCUSDT-{d:02d}DEC30", "type": "future"} for d in range(1, 13)]
    expired = {"id": "BTCUSDT-09OCT26", "type": "future", "delivery_date": "2026-10-09T08:00:00+00:00"}
    path = _write(data_dir, "bybit-futures", [*live, expired])

    assert updater.update(["bybit-futures"], _complete_source(live)) == []

    assert _read(path)["BTCUSDT-09OCT26"]["end_date"] == "2026-10-09T08:00:00.000Z"


def test_unexpired_future_that_vanishes_waits_for_confirmation(data_dir) -> None:
    pulled = {"id": "BTC-USD-301231", "type": "future", "delivery_date": "2030-12-31T00:00:00"}
    path = _write(data_dir, "okx-futures", [*LIVE_FUTURES, pulled])

    assert updater.update(["okx-futures"], _complete_source(LIVE_FUTURES)) == []

    row = _read(path)["BTC-USD-301231"]
    assert row["missing_since"]
    assert "end_date" not in row


def test_exchange_reported_delisting_is_applied_immediately(data_dir) -> None:
    path = _write(data_dir, "binance-futures", [{"id": "omgusdt", "type": "perpetual"}])
    source = _complete_source([
        {"id": "btcusdt", "type": "perpetual"},
        {"id": "omgusdt", "type": "perpetual", "end_date": "2025-01-31T09:00:00.000Z"},
    ])

    assert updater.update(["binance-futures"], source) == []

    assert _read(path)["omgusdt"]["end_date"] == "2025-01-31T09:00:00.000Z"


def test_exchange_reported_delisting_only_changes_lifecycle_fields(data_dir) -> None:
    on_file = {"id": "luna2perp", "type": "perpetual", "base_coin": "LUNA2", "first_capture": "2022-06-01T00:00:00.000Z"}
    path = _write(data_dir, "binance-futures", [{"id": "btcusdt", "type": "perpetual"}, on_file])
    source = _complete_source([
        {"id": "btcusdt", "type": "perpetual"},
        {"id": "luna2perp", "type": "perpetual", "base_coin": "LUNA", "end_date": "2025-01-31T09:00:00.000Z"},
    ])

    assert updater.update(["binance-futures"], source) == []

    row = _read(path)["luna2perp"]
    assert row["base_coin"] == "LUNA2"
    assert row["first_capture"] == "2022-06-01T00:00:00.000Z"
    assert row["end_date"] == "2025-01-31T09:00:00.000Z"


def test_exchange_reported_delisting_never_overwrites_recorded_end_date(data_dir) -> None:
    path = _write(data_dir, "binance-futures", [
        {"id": "btcusdt", "type": "perpetual"},
        {"id": "omgusdt", "type": "perpetual", "end_date": "2025-02-01T00:00:00.000Z"},
    ])
    source = _complete_source([
        {"id": "btcusdt", "type": "perpetual"},
        {"id": "omgusdt", "type": "perpetual", "end_date": "2025-01-31T09:00:00.000Z"},
    ])

    assert updater.update(["binance-futures"], source) == []

    assert _read(path)["omgusdt"]["end_date"] == "2025-02-01T00:00:00.000Z"


def test_delisted_instruments_we_never_tracked_are_not_added(data_dir) -> None:
    path = _write(data_dir, "binance-futures", [{"id": "btcusdt", "type": "perpetual"}])
    source = _complete_source([
        {"id": "btcusdt", "type": "perpetual"},
        {"id": "ancientusdt", "type": "perpetual", "end_date": "2019-01-01T00:00:00.000Z"},
    ])

    assert updater.update(["binance-futures"], source) == []

    assert set(_read(path)) == {"btcusdt"}


def test_relisted_instrument_has_its_end_date_cleared(data_dir) -> None:
    path = _write(data_dir, "binance-spot", [*LIVE, _spot("backbtc", end_date="2022-01-01T00:00:00.000Z")])

    assert updater.update(["binance-spot"], _complete_source([*LIVE, _spot("backbtc")])) == []

    assert "end_date" not in _read(path)["backbtc"]


def test_new_listing_gets_first_capture_on_every_exchange(data_dir) -> None:
    path = _write(data_dir, "binance-spot", LIVE)

    assert updater.update(["binance-spot"], _complete_source([*LIVE, _spot("newusdt")])) == []

    assert _read(path)["newusdt"]["first_capture"]


def test_exchange_listing_time_fills_first_capture_but_never_overwrites_it(data_dir) -> None:
    path = _write(data_dir, "binance-futures", [
        {"id": "btcusdt", "type": "perpetual", "first_capture": "2019-09-08T00:00:00.000Z"},
        {"id": "gaibusdt", "type": "perpetual"},
    ])
    source = _complete_source([
        {"id": "btcusdt", "type": "perpetual", "first_capture": "2019-09-08T17:55:00.000Z"},
        {"id": "gaibusdt", "type": "perpetual", "first_capture": "2025-11-20T09:00:00.000Z"},
        {"id": "newusdt", "type": "perpetual", "first_capture": "2026-10-13T08:00:00.000Z"},
    ])

    assert updater.update(["binance-futures"], source) == []

    rows = _read(path)
    assert rows["btcusdt"]["first_capture"] == "2019-09-08T00:00:00.000Z"
    assert rows["gaibusdt"]["first_capture"] == "2025-11-20T09:00:00.000Z"
    assert rows["newusdt"]["first_capture"] == "2026-10-13T08:00:00.000Z"


def test_listed_row_without_any_first_capture_gets_capture_time(data_dir) -> None:
    path = _write(data_dir, "binance-spot", [*LIVE, _spot("nocapturebtc")])

    assert updater.update(["binance-spot"], _complete_source([*LIVE, _spot("nocapturebtc")])) == []

    assert _read(path)["nocapturebtc"]["first_capture"]


def test_delisted_row_without_first_capture_is_not_stamped(data_dir) -> None:
    path = _write(data_dir, "binance-futures", [{"id": "btcusdt", "type": "perpetual"}, {"id": "omgusdt", "type": "perpetual"}])
    source = _complete_source([
        {"id": "btcusdt", "type": "perpetual"},
        {"id": "omgusdt", "type": "perpetual", "end_date": "2025-01-31T09:00:00.000Z"},
    ])

    assert updater.update(["binance-futures"], source) == []

    assert "first_capture" not in _read(path)["omgusdt"]


def test_mass_disappearance_fails_the_exchange_without_writing(data_dir) -> None:
    rows = [_spot(f"coin{i}usdt") for i in range(100)]
    path = _write(data_dir, "binance-spot", rows)
    before = path.read_text()

    failed = updater.update(["binance-spot"], _complete_source(rows[:80]))

    assert failed == ["binance-spot"]
    assert path.read_text() == before


def test_small_disappearance_on_small_exchange_is_allowed(data_dir) -> None:
    rows = [_spot(f"coin{i}usdt") for i in range(10)]
    path = _write(data_dir, "binance-spot", rows)

    assert updater.update(["binance-spot"], _complete_source(rows[:5])) == []

    assert sum("missing_since" in row for row in _read(path).values()) == 5


def test_empty_response_fails_the_exchange_without_writing(data_dir) -> None:
    path = _write(data_dir, "binance-spot", [_spot("btcusdt")])
    before = path.read_text()

    assert updater.update(["binance-spot"], _complete_source([])) == ["binance-spot"]
    assert path.read_text() == before


def test_partial_sources_never_infer_delistings(data_dir) -> None:
    path = _write(data_dir, "binance-spot", [*LIVE, _spot("gonebtc")])
    source = SimpleNamespace(fetch_exchange=lambda _e: {"availableSymbols": list(LIVE)})

    assert updater.update(["binance-spot"], source) == []

    assert _read(path)["gonebtc"] == _spot("gonebtc")
