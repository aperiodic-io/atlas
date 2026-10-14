"""Bitget, Gate and Coinbase are stable venues with API fetchers and snapshots."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas.database import SecurityMaster
from atlas.exchange_definitions import is_beta_exchange
from atlas.parsers import parse_contract
from atlas.exchanges import get_allowed_types, get_exchange_fetcher, to_tardis_exchange_id

NEW_STABLE_EXCHANGES = ["bitget", "bitget-futures", "gate-io", "gate-io-futures", "coinbase"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    @pytest.mark.parametrize("exchange", NEW_STABLE_EXCHANGES)
    def test_venue_is_stable(self, exchange: str):
        assert is_beta_exchange(exchange) is False

    @pytest.mark.parametrize("exchange", NEW_STABLE_EXCHANGES)
    def test_venue_has_an_api_fetcher(self, exchange: str):
        assert get_exchange_fetcher(exchange) is not None

    @pytest.mark.parametrize("exchange", NEW_STABLE_EXCHANGES)
    def test_exchange_ids_are_tardis_ids(self, exchange: str):
        assert to_tardis_exchange_id(exchange) == exchange

    @pytest.mark.parametrize(
        ("exchange", "allowed"),
        [
            ("bitget", {"spot"}),
            ("gate-io", {"spot"}),
            ("coinbase", {"spot"}),
            ("bitget-futures", {"perpetual", "future"}),
            ("gate-io-futures", {"perpetual", "future"}),
        ],
    )
    def test_allowed_types(self, exchange: str, allowed: set[str]):
        assert get_allowed_types(exchange) == allowed

    def test_other_tardis_only_venues_stay_beta(self):
        assert is_beta_exchange("kraken") is True


# ---------------------------------------------------------------------------
# Snapshot updates: API-only rows (no Tardis history) need a capture date,
# otherwise SecurityMaster.load() leaves them out of every lookup.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exchange", "row"),
    [
        ("bitget", {"id": "RNKEUSDT", "type": "spot", "base_coin": "rNKE", "quote_coin": "USDT"}),
        (
            "gate-io-futures",
            {
                "id": "BTC_USDT_20261225",
                "type": "future",
                "base_coin": "BTC",
                "quote_coin": "USDT",
                "settle_coin": "USDT",
                "delivery_date": "2026-12-25T08:00:00+00:00",
            },
        ),
    ],
)
def test_api_only_rows_receive_stable_capture_dates(tmp_path, monkeypatch, exchange, row):
    updater = importlib.import_module("atlas.update")
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    tardis_row = {"id": "BTC_USDT" if "gate" in exchange else "BTCUSDT", "type": row["type"],
                  "availableSince": "2020-01-01T00:00:00.000Z"}
    source = SimpleNamespace(
        fetch_exchange=lambda _exchange: {"availableSymbols": [tardis_row, row.copy()]}
    )

    assert updater.update([exchange], source) == []
    path = tmp_path / f"{exchange}.json"
    rows = {r["id"]: r for r in json.loads(path.read_text())}
    captured = rows[row["id"]]["first_capture"]
    assert captured
    assert rows[tardis_row["id"]]["first_capture"] == "2020-01-01T00:00:00.000Z"

    assert updater.update([exchange], source) == []
    rows = {r["id"]: r for r in json.loads(path.read_text())}
    assert rows[row["id"]]["first_capture"] == captured
    assert rows[row["id"]]["internal_id"]


# ---------------------------------------------------------------------------
# Bundled snapshots
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"


def _snapshot(exchange: str) -> list[dict]:
    return json.loads((_DATA_DIR / f"{exchange}.json").read_text())


@pytest.mark.parametrize("exchange", NEW_STABLE_EXCHANGES)
def test_snapshot_is_bundled_and_fully_mapped(exchange: str):
    rows = _snapshot(exchange)
    allowed = get_allowed_types(exchange)

    assert rows
    assert {row["type"] for row in rows} <= allowed
    assert [row["id"] for row in rows if not row.get("internal_id")] == []
    assert [row["id"] for row in rows if not row.get("first_capture")] == []
    assert len({row["id"] for row in rows}) == len(rows)


@pytest.mark.parametrize("exchange", NEW_STABLE_EXCHANGES)
def test_snapshot_internal_ids_match_the_parser(exchange: str):
    mismatches = [
        (row["id"], row["internal_id"], parse_contract(exchange, row).internal_id)
        for row in _snapshot(exchange)
        if parse_contract(exchange, row).internal_id != row["internal_id"]
    ]
    assert mismatches == []


@pytest.mark.parametrize(
    ("exchange", "original_id", "internal_id"),
    [
        ("bitget", "BTCUSDT", "spot-BTC-USDT"),
        ("bitget-futures", "BTCPERP", "perpetual-BTC-USDC:USDC"),
        ("bitget-futures", "BTCUSD_CM", "perpetual-BTC-USD:BTC"),
        ("bitget-futures", "BTCUSDH25", "future-BTC-USD:BTC-20250328"),
        ("gate-io", "BTC_USDT", "spot-BTC-USDT"),
        ("gate-io-futures", "BTC_USD", "perpetual-BTC-USD:BTC"),
        ("coinbase", "BTC-USD", "spot-BTC-USD"),
    ],
)
def test_security_master_resolves_new_venues(exchange: str, original_id: str, internal_id: str):
    sm = SecurityMaster.load(exchanges=[exchange])
    assert sm.by_exchange_and_original_id(exchange, original_id) == internal_id


def test_security_master_resolves_api_only_rows():
    """Rows Tardis does not list are reachable because they carry a first_capture."""
    rows = {row["id"]: row for row in _snapshot("gate-io-futures")}
    delivery = next(row for row in rows.values() if row["type"] == "future" and not row.get("end_date"))
    sm = SecurityMaster.load(exchanges=["gate-io-futures"])
    assert sm.by_exchange_and_original_id("gate-io-futures", delivery["id"]) == delivery["internal_id"]
