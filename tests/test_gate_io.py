"""Gate spot/futures parsers and API fetchers. Fetchers run against canned payloads."""

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
        assert _iid("gate-io", sid, "spot") == expected

    def test_spot_api_fields_are_authoritative(self):
        assert (
            _iid("gate-io", "BTC_USDT", "spot", base_coin="BTC", quote_coin="USDT")
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


class TestGateFetchers:
    def _patch(self, monkeypatch, payloads: dict[str, list[dict]]) -> list[str]:
        from atlas.exchange_definitions import gate_io

        urls: list[str] = []

        def fake_get(url, timeout=None, **_kwargs):
            urls.append(url)
            return _response(payloads[url.removeprefix("https://api.gateio.ws/api/v4")])

        monkeypatch.setattr(gate_io.requests, "get", fake_get)
        return urls

    def test_spot(self, monkeypatch):
        from atlas.exchange_definitions.gate_io import fetch_gate_io_spot

        self._patch(
            monkeypatch,
            {
                "/spot/currency_pairs": [
                    {"id": "BTC_USDT", "base": "BTC", "quote": "USDT", "trade_status": "tradable"},
                    {"id": "BTC_TRY", "base": "BTC", "quote": "TRY", "trade_status": "tradable"},
                    {"id": "OLD_USDT", "base": "OLD", "quote": "USDT", "trade_status": "untradable"},
                ]
            },
        )
        assert fetch_gate_io_spot(5) == [
            {"id": "BTC_USDT", "type": "spot", "base_coin": "BTC", "quote_coin": "USDT"},
            {"id": "BTC_TRY", "type": "spot", "base_coin": "BTC", "quote_coin": "TRY"},
        ]

    def test_futures_include_perpetuals_and_delivery_contracts(self, monkeypatch):
        from atlas.exchange_definitions.gate_io import fetch_gate_io_futures

        def perp(name: str, **extra: object) -> dict:
            return {
                "name": name,
                "type": "direct",
                "quanto_multiplier": "0.0001",
                "status": "trading",
                "in_delisting": False,
                "contract_type": "",
                "is_pre_market": False,
                **extra,
            }

        urls = self._patch(
            monkeypatch,
            {
                "/futures/usdt/contracts": [
                    perp("BTC_USDT"),
                    perp("AAPL_USDT", contract_type="stocks", quanto_multiplier="0.01"),
                    perp("SPX500_USDT", contract_type="indices"),
                    perp("XAU_USDT", contract_type="metals"),
                    perp("CL_USDT", contract_type="commodities"),
                    perp("EURUSD_USDT", contract_type="forex"),
                    perp("OPENAI_USDT", is_pre_market=True),
                    perp("GONE_USDT", in_delisting=True),
                    perp("DEAD_USDT", status="delisted"),
                ],
                "/futures/btc/contracts": [
                    perp("BTC_USD", type="inverse", quanto_multiplier="0"),
                ],
                "/delivery/usdt/contracts": [
                    {
                        "name": "BTC_USDT_20261225",
                        "underlying": "BTC_USDT",
                        "type": "direct",
                        "quanto_multiplier": "0.0001",
                        "expire_time": 1798185600,
                        "in_delisting": False,
                    },
                    {
                        "name": "BTC_USDT_20261016",
                        "underlying": "BTC_USDT",
                        "type": "direct",
                        "quanto_multiplier": "0.0001",
                        "expire_time": 1792137600,
                        "in_delisting": True,
                    },
                ],
                "/delivery/btc/contracts": [],
            },
        )
        symbols = {s["id"]: s for s in fetch_gate_io_futures(5)}

        assert len(urls) == 4
        assert set(symbols) == {
            "BTC_USDT",
            "AAPL_USDT",
            "SPX500_USDT",
            "XAU_USDT",
            "CL_USDT",
            "EURUSD_USDT",
            "OPENAI_USDT",
            "BTC_USD",
            "BTC_USDT_20261225",
        }
        assert symbols["BTC_USDT"] == {
            "id": "BTC_USDT",
            "type": "perpetual",
            "base_coin": "BTC",
            "quote_coin": "USDT",
            "settle_coin": "USDT",
            "contract_size": 0.0001,
            "underlying": "crypto",
        }
        assert symbols["BTC_USD"] == {
            "id": "BTC_USD",
            "type": "perpetual",
            "base_coin": "BTC",
            "quote_coin": "USD",
            "settle_coin": "BTC",
            "contract_size": 1.0,
            "quantity_unit": "quote",
            "underlying": "crypto",
        }
        assert {sid: symbols[sid]["underlying"] for sid in symbols} == {
            "BTC_USDT": "crypto",
            "AAPL_USDT": "equity",
            "SPX500_USDT": "index",
            "XAU_USDT": "commodity",
            "CL_USDT": "commodity",
            "EURUSD_USDT": "unknown",
            "OPENAI_USDT": "pre_market",
            "BTC_USD": "crypto",
            "BTC_USDT_20261225": "crypto",
        }
        assert symbols["BTC_USDT_20261225"] == {
            "id": "BTC_USDT_20261225",
            "type": "future",
            "base_coin": "BTC",
            "quote_coin": "USDT",
            "settle_coin": "USDT",
            "contract_size": 0.0001,
            "underlying": "crypto",
            "delivery_date": "2026-12-25T08:00:00+00:00",
        }
        assert (
            parse_contract("gate-io-futures", symbols["BTC_USDT_20261225"]).internal_id
            == "future-BTC-USDT:USDT-20261225"
        )

