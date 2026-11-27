"""The centralised venues outside the live producers, read from their own APIs.

Kraken, Bitfinex, Bitstamp, Gemini, HTX, KuCoin, Phemex, Poloniex, Upbit and
Crypto.com spot have stable slugs in the live-producers scheme (`kraken-spot`,
`gemini-perps`, `htx-spot`, ...), with their history bundled. BitMEX (closed
on 2026-09-23) and FTX have no API left and are not registered.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas import SecurityMaster
from atlas.exchanges import (
    EXCHANGE_DEFINITIONS,
    get_allowed_types,
    get_exchange_fetcher,
)
from atlas.parsers import parse_contract
from atlas.update import EXCHANGES, update
from atlas.update_sources import ExchangeApiSymbolSource

_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"

# slug -> allowed instrument types
NEW_VENUES: dict[str, set[str]] = {
    "bitfinex-spot": {"spot"},
    "bitfinex-perps": {"perpetual"},
    "bitstamp-spot": {"spot"},
    "bitstamp-perps": {"perpetual"},
    "cryptocom-spot": {"spot"},
    "gemini-spot": {"spot"},
    "gemini-perps": {"perpetual"},
    "htx-spot": {"spot"},
    "htx-perps-coin": {"perpetual"},
    "htx-futures-coin": {"future"},
    "htx-futures-usdt": {"future"},
    "kraken-spot": {"spot"},
    "kraken-perps": {"perpetual"},
    "kraken-futures": {"future"},
    "kucoin-spot": {"spot"},
    "phemex-spot": {"spot"},
    "phemex-perps": {"perpetual"},
    "poloniex-spot": {"spot"},
    "upbit-spot": {"spot"},
}


class TestRegistry:
    @pytest.mark.parametrize("exchange", sorted(NEW_VENUES))
    def test_new_venue_is_stable_with_a_fetcher(self, exchange):
        assert exchange in EXCHANGE_DEFINITIONS
        assert get_exchange_fetcher(exchange) is not None

    @pytest.mark.parametrize("exchange", sorted(NEW_VENUES))
    def test_new_venue_is_refreshed_by_default(self, exchange):
        assert exchange in EXCHANGES

    @pytest.mark.parametrize("exchange", sorted(NEW_VENUES))
    def test_allowed_types(self, exchange):
        assert get_allowed_types(exchange) == NEW_VENUES[exchange]

    def test_every_registered_exchange_is_in_the_default_run_once(self):
        assert len(EXCHANGES) == len(set(EXCHANGES))
        assert set(EXCHANGES) == set(EXCHANGE_DEFINITIONS)

class TestRouting:
    @pytest.mark.parametrize("exchange", sorted(NEW_VENUES))
    def test_exchange_source_calls_the_venue_fetcher(self, exchange, monkeypatch):
        calls = []
        definition = EXCHANGE_DEFINITIONS[exchange]
        monkeypatch.setitem(
            EXCHANGE_DEFINITIONS,
            exchange,
            replace(definition, exchange_fetcher=lambda t: calls.append(t) or []),
        )
        assert ExchangeApiSymbolSource(timeout_seconds=7).fetch_exchange(exchange) == {
            "availableSymbols": []
        }
        assert calls == [7]

class TestUpdate:
    @pytest.mark.parametrize("exchange", sorted(NEW_VENUES))
    def test_a_fresh_venue_snapshot_is_written_and_parsed(self, exchange, tmp_path, monkeypatch):
        """End to end through `update`: API rows written, each with an internal id and a start."""
        import atlas.update as updater

        rows = json.loads((_DATA_DIR / f"{exchange}.json").read_text(encoding="utf-8"))
        live = [
            {k: v for k, v in row.items() if k not in {"first_capture", "end_date", "missing_since", "internal_id"}}
            for row in rows
            if not row.get("end_date") and not row.get("missing_since") and row.get("base_coin")
        ][:20]
        assert live, f"{exchange}: no API-sourced open rows bundled"
        monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
        source = SimpleNamespace(
            lifecycle=ExchangeApiSymbolSource.lifecycle,
            fetch_exchange=lambda _e: {"availableSymbols": [r.copy() for r in live]},
        )
        assert update([exchange], source) == []
        written = json.loads((tmp_path / f"{exchange}.json").read_text(encoding="utf-8"))
        assert {r["id"] for r in written} == {r["id"] for r in live}
        assert all(r["internal_id"] and r["first_capture"] for r in written)


def _snapshot(exchange: str) -> list[dict]:
    return json.loads((_DATA_DIR / f"{exchange}.json").read_text(encoding="utf-8"))


class TestBundledSnapshots:
    @pytest.mark.parametrize("exchange", sorted(NEW_VENUES))
    def test_snapshot_is_bundled_and_fully_mapped(self, exchange):
        rows = _snapshot(exchange)
        assert rows
        assert {row["type"] for row in rows} <= NEW_VENUES[exchange]
        assert [row["id"] for row in rows if not row.get("internal_id")] == []
        assert [row["id"] for row in rows if not row.get("first_capture")] == []
        assert len({row["id"] for row in rows}) == len(rows)
        assert [row["id"] for row in rows if "missing_since" in row and row.get("end_date")] == []

    @pytest.mark.parametrize("exchange", sorted(NEW_VENUES))
    def test_snapshot_internal_ids_match_the_parser(self, exchange):
        mismatches = [
            (row["id"], row["internal_id"], parse_contract(exchange, row).internal_id)
            for row in _snapshot(exchange)
            if parse_contract(exchange, row).internal_id != row["internal_id"]
        ]
        assert mismatches == []

    @pytest.mark.parametrize("exchange", sorted(NEW_VENUES))
    def test_open_rows_come_from_the_venue_api(self, exchange):
        """Every open row was seen by the fetcher (it carries the venue's legs).

        History the API no longer lists is either closed or pending
        confirmation (`missing_since`) by the next daily run.
        """
        open_rows = [
            row for row in _snapshot(exchange) if not row.get("end_date") and not row.get("missing_since")
        ]
        assert open_rows
        assert [row["id"] for row in open_rows if not row.get("base_coin")] == []

    @pytest.mark.parametrize("exchange", sorted(NEW_VENUES))
    def test_derivatives_carry_margin_and_size(self, exchange):
        if NEW_VENUES[exchange] == {"spot"}:
            pytest.skip("spot")
        for row in _snapshot(exchange):
            assert row.get("margin"), row["id"]
            if row.get("base_coin"):
                assert row.get("contract_size"), row["id"]
                assert row.get("quantity_unit") in {"base", "quote"}, row["id"]

    @pytest.mark.parametrize("exchange", sorted(NEW_VENUES))
    def test_api_rows_classify_their_underlying(self, exchange):
        from atlas.types import UnderlyingType

        values = {row.get("underlying") for row in _snapshot(exchange) if row.get("base_coin")}
        assert values <= {u.value for u in UnderlyingType} | {None}

    def test_dated_futures_carry_delivery_dates(self):
        for exchange in ("htx-futures-coin", "htx-futures-usdt", "kraken-futures"):
            for row in _snapshot(exchange):
                assert row.get("delivery_date"), (exchange, row["id"])


@pytest.mark.parametrize(
    ("exchange", "original_id", "internal_id"),
    [
        ("kraken-spot", "BTC/USD", "spot-BTC-USD"),
        # History before 2026-07-10 holds Kraken's WebSocket v1 names.
        ("kraken-spot", "XBT/USD", "spot-BTC-USD"),
        ("kraken-perps", "PF_XBTUSD", "perpetual-BTC-USD:USD"),
        ("kraken-perps", "PI_XBTUSD", "perpetual-BTC-USD:BTC"),
        ("bitfinex-spot", "BTCUSD", "spot-BTC-USD"),
        ("bitfinex-perps", "BTCF0:USTF0", "perpetual-BTC-USDT:USDT"),
        ("bitstamp-spot", "btcusd", "spot-BTC-USD"),
        ("gemini-spot", "BTCUSD", "spot-BTC-USD"),
        ("htx-spot", "btcusdt", "spot-BTC-USDT"),
        ("htx-perps-coin", "BTC-USD", "perpetual-BTC-USD:BTC"),
        ("kucoin-spot", "BTC-USDT", "spot-BTC-USDT"),
        ("phemex-spot", "sBTCUSDT", "spot-BTC-USDT"),
        ("phemex-perps", "BTCUSDT", "perpetual-BTC-USDT:USDT"),
        ("poloniex-spot", "BTC_USDT", "spot-BTC-USDT"),
        ("upbit-spot", "KRW-BTC", "spot-BTC-KRW"),
        ("cryptocom-spot", "BTC_USD", "spot-BTC-USD"),
    ],
)
def test_security_master_resolves_flagship_instruments(exchange, original_id, internal_id):
    sm = SecurityMaster.load(exchanges=[exchange])
    assert sm.by_exchange_and_original_id(exchange, original_id) == internal_id
