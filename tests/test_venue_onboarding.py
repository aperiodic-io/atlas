"""Bitget, Gate and Coinbase spot are stable venues with API fetchers and snapshots.

They follow the live-producers naming (`bitget-spot`, `gateio-spot`,
`coinbase-spot`), like Bitget's and Gate's contracts (`bitget-perps-*`,
`gateio-*`).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas.database import SecurityMaster
from atlas.parsers import parse_contract
from atlas.exchanges import get_allowed_types, get_exchange_fetcher

NEW_STABLE_EXCHANGES = ["bitget-spot", "gateio-spot", "coinbase-spot"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    @pytest.mark.parametrize("exchange", NEW_STABLE_EXCHANGES)
    def test_venue_has_an_api_fetcher(self, exchange: str):
        assert get_exchange_fetcher(exchange) is not None

    @pytest.mark.parametrize(
        ("exchange", "allowed"),
        [
            ("bitget-spot", {"spot"}),
            ("gateio-spot", {"spot"}),
            ("coinbase-spot", {"spot"}),
        ],
    )
    def test_allowed_types(self, exchange: str, allowed: set[str]):
        assert get_allowed_types(exchange) == allowed

# ---------------------------------------------------------------------------
# Snapshot updates: rows new to the file need a capture date,
# otherwise SecurityMaster.load() leaves them out of every lookup.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exchange", "row"),
    [
        ("bitget-spot", {"id": "RNKEUSDT", "type": "spot", "base_coin": "rNKE", "quote_coin": "USDT"}),
        ("gateio-spot", {"id": "NEW_USDT", "type": "spot", "base_coin": "NEW", "quote_coin": "USDT"}),
    ],
)
def test_api_only_rows_receive_stable_capture_dates(tmp_path, monkeypatch, exchange, row):
    updater = importlib.import_module("atlas.update")
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    history_id = "BTC_USDT" if "gate" in exchange else "BTCUSDT"
    history = {"id": history_id, "type": row["type"], "first_capture": "2020-01-01T00:00:00.000Z"}
    (tmp_path / f"{exchange}.json").write_text(json.dumps([history]), encoding="utf-8")
    listed = {"id": history_id, "type": row["type"]}
    source = SimpleNamespace(
        fetch_exchange=lambda _exchange: {"availableSymbols": [listed.copy(), row.copy()]}
    )

    assert updater.update([exchange], source) == []
    path = tmp_path / f"{exchange}.json"
    rows = {r["id"]: r for r in json.loads(path.read_text())}
    captured = rows[row["id"]]["first_capture"]
    assert captured
    assert rows[history_id]["first_capture"] == "2020-01-01T00:00:00.000Z"

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
        ("bitget-spot", "BTCUSDT", "spot-BTC-USDT"),
        ("gateio-spot", "BTC_USDT", "spot-BTC-USDT"),
        ("coinbase-spot", "BTC-USD", "spot-BTC-USD"),
    ],
)
def test_security_master_resolves_new_venues(exchange: str, original_id: str, internal_id: str):
    sm = SecurityMaster.load(exchanges=[exchange])
    assert sm.by_exchange_and_original_id(exchange, original_id) == internal_id


def test_security_master_resolves_api_only_rows():
    """Rows new to the file (Bitget's tokenized stocks) carry a first_capture."""
    stock = next(
        row for row in _snapshot("bitget-spot") if row.get("underlying") == "equity" and not row.get("end_date")
    )
    sm = SecurityMaster.load(exchanges=["bitget-spot"])
    assert sm.by_exchange_and_original_id("bitget-spot", stock["id"]) == stock["internal_id"]
