"""Bitget, Gate and Coinbase are stable venues with API fetchers and snapshots."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from atlas.exchange_definitions import is_beta_exchange
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
