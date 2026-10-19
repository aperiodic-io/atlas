"""Bitget spot parser and API fetcher. The fetcher runs against canned payloads.

Bitget's contracts (`bitget-perps-*`, `bitget-futures-coin`) are covered in
`test_live_producer_venues.py`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from atlas.parsers import parse_contract


def _sd(id: str, type: str, **extra: object) -> dict:
    return {"id": id, "type": type, **extra}


def _iid(exchange: str, id: str, type: str, **extra: object) -> str:
    return parse_contract(exchange, _sd(id, type, **extra)).internal_id


def _response(payload: object) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


# ---------------------------------------------------------------------------
# Bitget
# ---------------------------------------------------------------------------


class TestBitgetSpotParser:
    def test_api_fields_are_authoritative_and_upper_cased(self):
        c = parse_contract(
            "bitget-spot",
            _sd("RNNNNUSDT", "spot", base_coin="rNNNN", quote_coin="USDT"),
        )
        assert c.internal_id == "spot-RNNNN-USDT"
        assert c.margin is None

    @pytest.mark.parametrize(
        ("sid", "expected"),
        [
            ("BTCUSDT", "spot-BTC-USDT"),
            ("BTCUSDC", "spot-BTC-USDC"),
            ("BTCEUR", "spot-BTC-EUR"),
            ("BTCBRL", "spot-BTC-BRL"),
            ("BTCUSD1", "spot-BTC-USD1"),
            ("BUSD1", "spot-B-USD1"),
            ("ETHUSDE", "spot-ETH-USDE"),
            ("BTCRLUSD", "spot-BTC-RLUSD"),
            ("USDTVND", "spot-USDT-VND"),
            ("BGSOLSOL", "spot-BGSOL-SOL"),
            ("BTCU", "spot-BTC-U"),
            ("USDCUSD", "spot-USDC-USD"),
            ("USDTUSD", "spot-USDT-USD"),
        ],
    )
    def test_tardis_only_rows_split_on_bitget_quotes(self, sid: str, expected: str):
        assert _iid("bitget-spot", sid, "spot") == expected


def _bitget_instrument(symbol: str, base: str, quote: str, **extra: str) -> dict:
    return {"symbol": symbol, "baseCoin": base, "quoteCoin": quote, "status": "online", **extra}


class TestBitgetSpotFetcher:
    def _fetch(self, monkeypatch, items: list[dict]) -> tuple[list[dict], list[dict]]:
        from atlas.exchange_definitions import bitget

        calls: list[dict] = []

        def fake_get_json(url, timeout_seconds, params=None):
            calls.append({"url": url, "params": params, "timeout": timeout_seconds})
            return {"code": "00000", "data": items}

        monkeypatch.setattr(bitget, "get_json", fake_get_json)
        return bitget.fetch_bitget_spot(7), calls

    def test_rows_carry_legs_class_and_listing_time(self, monkeypatch):
        rows, calls = self._fetch(
            monkeypatch,
            [
                _bitget_instrument("BTCUSDT", "BTC", "USDT", symbolType="crypto", launchTime="1600000000000"),
                _bitget_instrument("RNKEUSDT", "rNKE", "USDT", symbolType="stock"),
                _bitget_instrument("XAUTUSDT", "XAUT", "USDT", symbolType="metal", launchTime="0"),
            ],
        )
        assert rows == [
            {
                "id": "BTCUSDT", "type": "spot", "base_coin": "BTC", "quote_coin": "USDT",
                "underlying": "crypto", "first_capture": "2020-09-13T12:26:40.000Z",
            },
            {"id": "RNKEUSDT", "type": "spot", "base_coin": "rNKE", "quote_coin": "USDT", "underlying": "equity"},
            {"id": "XAUTUSDT", "type": "spot", "base_coin": "XAUT", "quote_coin": "USDT", "underlying": "commodity"},
        ]
        assert calls == [
            {"url": "https://api.bitget.com/api/v3/market/instruments", "params": {"category": "SPOT"}, "timeout": 7}
        ]

    def test_statuses_follow_the_contract_endpoint(self, monkeypatch):
        # Halted pairs (`limit_open`, ...) stay listed, `listed` has not started
        # trading, and `offline` is delisted at `offTime`.
        rows, _ = self._fetch(
            monkeypatch,
            [
                _bitget_instrument("SWEATUSDT", "SWEAT", "USDT", status="limit_open"),
                _bitget_instrument("NEWUSDT", "NEW", "USDT", status="listed"),
                _bitget_instrument("OLDUSDT", "OLD", "USDT", status="offline", offTime="1700000000000"),
            ],
        )
        by_id = {row["id"]: row for row in rows}
        assert set(by_id) == {"SWEATUSDT", "OLDUSDT"}
        assert "end_date" not in by_id["SWEATUSDT"]
        assert by_id["OLDUSDT"]["end_date"] == "2023-11-14T22:13:20.000Z"

    def test_fetched_rows_parse_from_their_legs(self, monkeypatch):
        rows, _ = self._fetch(monkeypatch, [_bitget_instrument("RNKEUSDT", "rNKE", "USDT", symbolType="stock")])
        assert parse_contract("bitget-spot", rows[0]).internal_id == "spot-RNKE-USDT"
