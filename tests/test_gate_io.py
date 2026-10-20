"""Gate parsers and the spot API fetcher, run against canned payloads.

Gate's contract fetchers (`gateio-*`) are covered in `test_live_producer_venues.py`.
"""

from __future__ import annotations

import pytest

from atlas.parsers import parse_contract


def _sd(id: str, type: str, **extra: object) -> dict:
    return {"id": id, "type": type, **extra}


def _iid(exchange: str, id: str, type: str, **extra: object) -> str:
    return parse_contract(exchange, _sd(id, type, **extra)).internal_id


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TestGateParsers:
    @pytest.mark.parametrize(
        ("sid", "expected"),
        [
            ("BTC_USDT", "spot-BTC-USDT"),
            ("BTC_TRY", "spot-BTC-TRY"),
            ("BTC_USD", "spot-BTC-USD"),
            ("BTC_EUR", "spot-BTC-EUR"),
            ("ETH_BTC", "spot-ETH-BTC"),
            ("BTC_CNYX", "spot-BTC-CNYX"),
            ("XRP_RLUSD", "spot-XRP-RLUSD"),
        ],
    )
    def test_spot_tardis_ids(self, sid: str, expected: str):
        assert _iid("gateio-spot", sid, "spot") == expected

    def test_spot_api_fields_are_authoritative(self):
        assert (
            _iid("gateio-spot", "BTC_USDT", "spot", base_coin="BTC", quote_coin="USDT")
            == "spot-BTC-USDT"
        )

    @pytest.mark.parametrize(
        ("sid", "type", "extra", "expected"),
        [
            ("BTC_USDT", "perpetual", {}, "perpetual-BTC-USDT:USDT"),
            ("BTC_USD", "perpetual", {}, "perpetual-BTC-USD:BTC"),
            ("EURUSD_USDT", "perpetual", {}, "perpetual-EURUSD-USDT:USDT"),
            ("BTC_USDT_20261225", "future", {}, "future-BTC-USDT:USDT-20261225"),
            (
                "BTC_USD",
                "perpetual",
                {"base_coin": "BTC", "quote_coin": "USD", "settle_coin": "BTC"},
                "perpetual-BTC-USD:BTC",
            ),
            (
                "ETH_USDT_20261023",
                "future",
                {
                    "base_coin": "ETH",
                    "quote_coin": "USDT",
                    "settle_coin": "USDT",
                    "delivery_date": "2026-10-23T08:00:00+00:00",
                },
                "future-ETH-USDT:USDT-20261023",
            ),
        ],
    )
    def test_futures(self, sid: str, type: str, extra: dict, expected: str):
        assert _iid("gate-io-futures", sid, type, **extra) == expected


class TestGateSpotFetcher:
    def _fetch(self, monkeypatch, items: list[dict]) -> list[dict]:
        from atlas.exchange_definitions import gate_io

        urls: list[str] = []

        def fake_get_json(url, timeout_seconds, params=None):
            urls.append(url)
            return items

        monkeypatch.setattr(gate_io, "get_json", fake_get_json)
        rows = gate_io.fetch_gate_io_spot(5)
        assert urls == ["https://api.gateio.ws/api/v4/spot/currency_pairs"]
        return rows

    def test_rows_carry_legs_and_listing_time(self, monkeypatch):
        rows = self._fetch(
            monkeypatch,
            [
                {"id": "BTC_USDT", "base": "BTC", "quote": "USDT", "trade_status": "tradable", "buy_start": 1600000000},
                {"id": "USDT_USD", "base": "USDT", "quote": "USD", "trade_status": "tradable", "buy_start": 0},
            ],
        )
        assert rows == [
            {
                "id": "BTC_USDT", "type": "spot", "base_coin": "BTC", "quote_coin": "USDT",
                "first_capture": "2020-09-13T12:26:40.000Z",
            },
            {"id": "USDT_USD", "type": "spot", "base_coin": "USDT", "quote_coin": "USD"},
        ]

    def test_halted_pairs_stay_listed_and_past_delistings_end(self, monkeypatch):
        rows = {
            row["id"]: row
            for row in self._fetch(
                monkeypatch,
                [
                    {"id": "MTR_USDT", "base": "MTR", "quote": "USDT", "trade_status": "untradable"},
                    {"id": "OLD_USDT", "base": "OLD", "quote": "USDT", "trade_status": "tradable",
                     "delisting_time": 1700000000},
                    # Scheduled for the future: still listed today.
                    {"id": "SOON_USDT", "base": "SOON", "quote": "USDT", "trade_status": "tradable",
                     "delisting_time": 4102444800},
                ],
            )
        }
        assert set(rows) == {"MTR_USDT", "OLD_USDT", "SOON_USDT"}
        assert "end_date" not in rows["MTR_USDT"]
        assert rows["OLD_USDT"]["end_date"] == "2023-11-14T22:13:20.000Z"
        assert "end_date" not in rows["SOON_USDT"]
