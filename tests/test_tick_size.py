"""Tick-size history: the daily refresh must keep every tick an instrument has had.

A venue re-ticks a contract in place (same id), so the snapshot's current
`tick_size` alone cannot price an order book from before the change. These
tests pin that the refresh records each change with the time it was first seen,
and that lookups by time return the tick that applied then.
"""

import importlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from atlas.database import SecurityMaster
from atlas.update import LOCALLY_OWNED_FIELDS, _record_tick_size

SEEN = "2026-10-21T02:00:00.000Z"
LATER = "2026-10-22T02:00:00.000Z"


def _fetch_usdm(items: list[dict]) -> list[dict]:
    from atlas.exchange_definitions.binance import fetch_binance_futures_usdm

    resp = MagicMock()
    resp.json.return_value = {"symbols": items}
    with patch("atlas.exchange_definitions.binance.requests.get", return_value=resp):
        return fetch_binance_futures_usdm(timeout_seconds=5)


def _usdm_item(symbol: str, tick: str | None) -> dict:
    price_filter = {"filterType": "PRICE_FILTER", "minPrice": "0.0000010"}
    if tick is not None:
        price_filter["tickSize"] = tick
    return {
        "symbol": symbol,
        "contractType": "PERPETUAL",
        "underlyingType": "COIN",
        "status": "TRADING",
        "filters": [price_filter, {"filterType": "LOT_SIZE", "stepSize": "1"}],
    }


class TestBinanceUsdmFetch:
    def test_tick_size_comes_from_the_price_filter(self):
        (symbol,) = _fetch_usdm([_usdm_item("BRUSDT", "0.0000100")])
        assert symbol["tick_size"] == 0.00001

    def test_no_tick_size_when_the_venue_omits_it(self):
        # Recording a guess would make a later real value look like a re-tick.
        (symbol,) = _fetch_usdm([_usdm_item("BRUSDT", None)])
        assert "tick_size" not in symbol


class TestRecordTickSize:
    def test_first_sighting_starts_the_history(self):
        # Without this entry a later change would lose the tick it replaced.
        row = _record_tick_size({"id": "brusdt", "tick_size": 0.00001}, SEEN)
        assert row["tick_size_history"] == [{"effective_from": SEEN, "value": 0.00001}]

    def test_unchanged_tick_adds_nothing(self):
        history = [{"effective_from": SEEN, "value": 0.00001}]
        row = _record_tick_size(
            {"id": "brusdt", "tick_size": 0.00001, "tick_size_history": history}, LATER
        )
        assert row["tick_size_history"] == history

    def test_changed_tick_is_appended_when_first_seen(self):
        history = [{"effective_from": SEEN, "value": 0.00001}]
        row = _record_tick_size(
            {"id": "brusdt", "tick_size": 0.0001, "tick_size_history": history}, LATER
        )
        assert row["tick_size_history"] == [
            {"effective_from": SEEN, "value": 0.00001},
            {"effective_from": LATER, "value": 0.0001},
        ]

    def test_row_without_tick_is_left_alone(self):
        row = {"id": "brusdt", "type": "perpetual"}
        assert _record_tick_size(row, SEEN) == row

    def test_input_is_not_mutated(self):
        history = [{"effective_from": SEEN, "value": 0.00001}]
        _record_tick_size({"id": "brusdt", "tick_size": 0.0001, "tick_size_history": history}, LATER)
        assert history == [{"effective_from": SEEN, "value": 0.00001}]


def test_history_is_locally_owned() -> None:
    # The venue payload never carries a history, so a refresh must not drop it.
    assert "tick_size_history" in LOCALLY_OWNED_FIELDS


def test_daily_refresh_records_a_re_tick(tmp_path, monkeypatch) -> None:
    updater = importlib.import_module("atlas.update")
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    ticks = {"BRUSDT": "0.0000100"}

    def fetch(_exchange: str) -> dict:
        return {"availableSymbols": _fetch_usdm([_usdm_item("BRUSDT", ticks["BRUSDT"])])}

    source = SimpleNamespace(fetch_exchange=fetch)
    path = tmp_path / "binance-futures.json"

    assert updater.update(["binance-futures"], source) == []
    (first,) = json.loads(path.read_text())
    assert first["tick_size"] == 0.00001
    assert [e["value"] for e in first["tick_size_history"]] == [0.00001]

    assert updater.update(["binance-futures"], source) == []
    assert json.loads(path.read_text())[0]["tick_size_history"] == first["tick_size_history"]

    ticks["BRUSDT"] = "0.0001000"
    assert updater.update(["binance-futures"], source) == []
    (row,) = json.loads(path.read_text())
    assert row["tick_size"] == 0.0001
    assert [e["value"] for e in row["tick_size_history"]] == [0.00001, 0.0001]
    assert row["tick_size_history"][0] == first["tick_size_history"][0]


def _master(tmp_path, row: dict) -> SecurityMaster:
    base = {
        "id": "brusdt",
        "first_capture": "2025-03-21T00:00:00.000Z",
        "symbol": "BR",
        "denominator": "USDT",
        "margin": "USDT",
        "internal_id": "perpetual-BR-USDT:USDT",
    }
    (tmp_path / "binance-futures.json").write_text(json.dumps([{**base, **row}]))
    return SecurityMaster.load(data_dir=tmp_path)


# BRUSDT's 2026-10-22 re-tick, dated by Binance's announcement.
BRUSDT_HISTORY = [
    {"effective_from": "2026-10-21T04:00:35Z", "value": 0.00001},
    {"effective_from": "2026-10-22T06:30:00Z", "value": 0.0001},
]


class TestTickSizeLookup:
    def test_current_tick_without_a_time(self, tmp_path):
        sm = _master(tmp_path, {"tick_size": 0.0001, "tick_size_history": BRUSDT_HISTORY})
        assert sm.tick_size("binance-futures", "brusdt") == 0.0001

    def test_tick_in_effect_at_a_time(self, tmp_path):
        sm = _master(tmp_path, {"tick_size": 0.0001, "tick_size_history": BRUSDT_HISTORY})
        before = datetime(2026, 10, 22, 6, 29, 59, tzinfo=UTC)
        assert sm.tick_size("binance-futures", "brusdt", at=before) == 0.00001
        assert sm.tick_size("binance-futures", "brusdt", at=datetime(2026, 10, 22, 6, 30)) == 0.0001

    def test_unknown_before_the_first_recorded_tick(self, tmp_path):
        # The venue may have re-ticked before Atlas looked; don't guess.
        sm = _master(tmp_path, {"tick_size": 0.0001, "tick_size_history": BRUSDT_HISTORY})
        assert sm.tick_size("binance-futures", "brusdt", at=datetime(2026, 1, 1)) is None

    def test_unknown_instrument(self, tmp_path):
        sm = _master(tmp_path, {})
        assert sm.tick_size("binance-futures", "nopeusdt") is None
        assert sm.tick_size("binance-futures", "brusdt") is None
