"""Coinbase spot parser and API fetcher. The fetcher runs against a canned payload."""

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
# Coinbase
# ---------------------------------------------------------------------------


class TestCoinbaseParser:
    @pytest.mark.parametrize(
        "quote", ["USD", "EUR", "GBP", "USDC", "USDT", "BTC", "ETH", "DAI", "INR", "SGD"]
    )
    def test_tardis_ids_split_on_dash(self, quote: str):
        assert _iid("coinbase", f"SOL-{quote}", "spot") == f"spot-SOL-{quote}"

    def test_api_fields_are_authoritative(self):
        assert (
            _iid("coinbase", "1INCH-EUR", "spot", base_coin="1INCH", quote_coin="EUR")
            == "spot-1INCH-EUR"
        )


class TestCoinbaseFetcher:
    def test_only_tradeable_products(self, monkeypatch):
        from atlas.exchange_definitions import coinbase

        def product(pid: str, **extra: object) -> dict:
            base, quote = pid.split("-")
            return {
                "id": pid,
                "base_currency": base,
                "quote_currency": quote,
                "status": "online",
                "trading_disabled": False,
                "cancel_only": False,
                **extra,
            }

        captured: dict = {}

        def fake_get(url, timeout=None, headers=None, **_kwargs):
            captured.update(url=url, headers=headers)
            return _response(
                [
                    product("BTC-USD"),
                    product("ETH-EUR", limit_only=True),
                    product("ADA-USDC", status="delisted", trading_disabled=True),
                    product("XYZ-USD", trading_disabled=True),
                    product("ABC-USD", cancel_only=True),
                ]
            )

        monkeypatch.setattr(coinbase.requests, "get", fake_get)
        assert coinbase.fetch_coinbase_spot(5) == [
            {"id": "BTC-USD", "type": "spot", "base_coin": "BTC", "quote_coin": "USD"},
            {"id": "ETH-EUR", "type": "spot", "base_coin": "ETH", "quote_coin": "EUR"},
        ]
        assert captured["url"] == "https://api.exchange.coinbase.com/products"
        assert captured["headers"]["User-Agent"]
