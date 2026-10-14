"""Bitget parsers and API fetchers. Fetchers run against canned payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from atlas.contracts import ContractType
from atlas.parsers import SkipSymbol, parse_contract
from atlas.types import UnderlyingType


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
            "bitget",
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
        assert _iid("bitget", sid, "spot") == expected


class TestBitgetFuturesParser:
    @pytest.mark.parametrize(
        ("sid", "type", "expected"),
        [
            # USDT-margined linear perpetuals.
            ("BTCUSDT", "perpetual", "perpetual-BTC-USDT:USDT"),
            ("1000000MOGUSDT", "perpetual", "perpetual-1000000MOG-USDT:USDT"),
            # USDC-margined linear perpetuals are named `<BASE>PERP`.
            ("BTCPERP", "perpetual", "perpetual-BTC-USDC:USDC"),
            ("1000BONKPERP", "perpetual", "perpetual-1000BONK-USDC:USDC"),
            ("TONCOINPERP", "perpetual", "perpetual-TONCOIN-USDC:USDC"),
            # Coin-margined inverse perpetuals, before and after the UTA rename.
            ("BTCUSD", "perpetual", "perpetual-BTC-USD:BTC"),
            ("BTCUSD_CM", "perpetual", "perpetual-BTC-USD:BTC"),
            ("HYPEUSD_CM", "perpetual", "perpetual-HYPE-USD:HYPE"),
            # Coin-margined quarterlies: delivery on the last Friday of the month.
            ("BTCUSDZ24", "future", "future-BTC-USD:BTC-20241227"),
            ("BTCUSDH25", "future", "future-BTC-USD:BTC-20250328"),
            ("ETHUSDH26", "future", "future-ETH-USD:ETH-20260327"),
            ("BTCCMZ26", "future", "future-BTC-USD:BTC-20261225"),
            ("BTCCMH27", "future", "future-BTC-USD:BTC-20270326"),
            # Legacy v1 suffixes.
            ("BTCUSDT_UMCBL", "perpetual", "perpetual-BTC-USDT:USDT"),
            ("BTCPERP_CMCBL", "perpetual", "perpetual-BTC-USDC:USDC"),
        ],
    )
    def test_tardis_id_families(self, sid: str, type: str, expected: str):
        assert _iid("bitget-futures", sid, type) == expected

    def test_quarterly_delivery_prefers_api_delivery_time(self):
        c = parse_contract(
            "bitget-futures",
            _sd(
                "BTCCMZ26",
                "future",
                base_coin="BTC",
                quote_coin="USD",
                settle_coin="BTC",
                delivery_date="2026-12-24T08:00:00+00:00",
            ),
        )
        assert c.internal_id == "future-BTC-USD:BTC-20261224"
        assert c.delivery_date == datetime(2026, 12, 24, 8, tzinfo=UTC)

    def test_api_row_without_delivery_time_falls_back_to_last_friday(self):
        c = parse_contract(
            "bitget-futures",
            _sd("ETHCMH27", "future", base_coin="ETH", quote_coin="USD", settle_coin="ETH"),
        )
        assert c.internal_id == "future-ETH-USD:ETH-20270326"

    def test_api_fields_are_authoritative(self):
        c = parse_contract(
            "bitget-futures",
            _sd("XAUTPERP", "perpetual", base_coin="XAUT", quote_coin="USDC", settle_coin="USDC"),
        )
        assert c.internal_id == "perpetual-XAUT-USDC:USDC"

    def test_unknown_legacy_suffix_is_skipped(self):
        with pytest.raises(SkipSymbol):
            parse_contract("bitget-futures", _sd("BTCUSDT_XYZ", "perpetual"))

    def test_quarterly_without_month_code_is_not_a_future(self):
        c = parse_contract("bitget-futures", _sd("BTCUSD", "perpetual"))
        assert c.instrument_type == ContractType.perpetual
        assert c.delivery_date is None


def _bitget_instrument(symbol: str, base: str, quote: str, **extra: str) -> dict:
    return {"symbol": symbol, "baseCoin": base, "quoteCoin": quote, "status": "online", **extra}


class TestBitgetFetchers:
    def _patch(self, monkeypatch, payloads: dict[str, list[dict]]) -> list[dict]:
        from atlas.exchange_definitions import bitget

        calls: list[dict] = []

        def fake_get(url, params=None, timeout=None, **_kwargs):
            calls.append({"url": url, "params": params, "timeout": timeout})
            return _response({"code": "00000", "data": payloads[params["category"]]})

        monkeypatch.setattr(bitget.requests, "get", fake_get)
        return calls

    def test_spot(self, monkeypatch):
        from atlas.exchange_definitions.bitget import fetch_bitget_spot

        calls = self._patch(
            monkeypatch,
            {
                "SPOT": [
                    _bitget_instrument("BTCUSDT", "BTC", "USDT", symbolType="crypto"),
                    _bitget_instrument("RNKEUSDT", "rNKE", "USDT", symbolType="stock"),
                    _bitget_instrument("XAUTUSDT", "XAUT", "USDT", symbolType="metal"),
                    _bitget_instrument("OLDUSDT", "OLD", "USDT", status="halt"),
                ]
            },
        )
        assert fetch_bitget_spot(7) == [
            {"id": "BTCUSDT", "type": "spot", "base_coin": "BTC", "quote_coin": "USDT", "underlying": "crypto"},
            {"id": "RNKEUSDT", "type": "spot", "base_coin": "rNKE", "quote_coin": "USDT", "underlying": "equity"},
            {"id": "XAUTUSDT", "type": "spot", "base_coin": "XAUT", "quote_coin": "USDT", "underlying": "commodity"},
        ]
        assert calls[0]["url"] == "https://api.bitget.com/api/v3/market/instruments"
        assert calls[0]["timeout"] == 7

    def test_futures_cover_usdt_usdc_and_coin_margined_products(self, monkeypatch):
        from atlas.exchange_definitions.bitget import fetch_bitget_futures

        calls = self._patch(
            monkeypatch,
            {
                "USDT-FUTURES": [
                    _bitget_instrument("BTCUSDT", "BTC", "USDT", type="perpetual", symbolType="crypto"),
                    _bitget_instrument("AAPLUSDT", "AAPL", "USDT", type="perpetual", symbolType="stock"),
                    _bitget_instrument("GONEUSDT", "GONE", "USDT", type="perpetual", status="offline"),
                ],
                "USDC-FUTURES": [
                    _bitget_instrument("BTCPERP", "BTC", "USDC", type="perpetual", symbolType="crypto"),
                ],
                "COIN-FUTURES": [
                    _bitget_instrument("BTCUSD_CM", "BTC", "USD", type="perpetual", symbolType="crypto"),
                    _bitget_instrument(
                        "BTCCMZ26",
                        "BTC",
                        "USD",
                        type="delivery",
                        symbolType="crypto",
                        deliveryTime="1798185600000",
                    ),
                ],
            },
        )
        symbols = {s["id"]: s for s in fetch_bitget_futures(5)}

        assert [c["params"]["category"] for c in calls] == [
            "USDT-FUTURES",
            "USDC-FUTURES",
            "COIN-FUTURES",
        ]
        assert set(symbols) == {"BTCUSDT", "AAPLUSDT", "BTCPERP", "BTCUSD_CM", "BTCCMZ26"}
        assert symbols["BTCUSDT"] == {
            "id": "BTCUSDT",
            "type": "perpetual",
            "base_coin": "BTC",
            "quote_coin": "USDT",
            "settle_coin": "USDT",
            "contract_size": 1.0,
            "quantity_unit": "base",
            "underlying": "crypto",
        }
        assert symbols["AAPLUSDT"]["underlying"] == UnderlyingType.equity.value
        assert symbols["BTCPERP"]["settle_coin"] == "USDC"
        assert symbols["BTCUSD_CM"] == {
            "id": "BTCUSD_CM",
            "type": "perpetual",
            "base_coin": "BTC",
            "quote_coin": "USD",
            "settle_coin": "BTC",
            "underlying": "crypto",
        }
        assert symbols["BTCCMZ26"]["type"] == "future"
        assert symbols["BTCCMZ26"]["delivery_date"] == "2026-12-25T08:00:00+00:00"

    def test_fetched_rows_parse(self, monkeypatch):
        from atlas.exchange_definitions.bitget import fetch_bitget_futures

        self._patch(
            monkeypatch,
            {
                "USDT-FUTURES": [_bitget_instrument("BTCUSDT", "BTC", "USDT", type="perpetual")],
                "USDC-FUTURES": [_bitget_instrument("BTCPERP", "BTC", "USDC", type="perpetual")],
                "COIN-FUTURES": [
                    _bitget_instrument("BTCUSD_CM", "BTC", "USD", type="perpetual"),
                    _bitget_instrument(
                        "BTCCMH27", "BTC", "USD", type="delivery", deliveryTime="1806048000000"
                    ),
                ],
            },
        )
        assert sorted(
            parse_contract("bitget-futures", s).internal_id for s in fetch_bitget_futures(5)
        ) == [
            "future-BTC-USD:BTC-20270326",
            "perpetual-BTC-USD:BTC",
            "perpetual-BTC-USDC:USDC",
            "perpetual-BTC-USDT:USDT",
        ]

    def test_error_payload_raises(self, monkeypatch):
        from atlas.exchange_definitions import bitget

        monkeypatch.setattr(bitget._fetch_bitget_instruments.retry, "sleep", lambda _s: None)
        monkeypatch.setattr(
            bitget.requests,
            "get",
            lambda *_a, **_k: _response({"code": "40034", "msg": "bad", "data": None}),
        )
        with pytest.raises(ValueError, match="40034"):
            bitget.fetch_bitget_spot(5)

