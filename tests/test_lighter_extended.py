"""Parsers and exchange-API fetchers of Lighter and Extended, from
recorded venue payloads (tests/data/defi_b, fields trimmed to those read).

The internal IDs pinned here are the ones the live producer
(live-producers streaming/feeds/{lighter,extended}) publishes under
the Atlas scheme; the two sides must agree for every live contract.
"""

from __future__ import annotations

import importlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from atlas.contracts import ContractType
from atlas.database import SecurityMaster
from atlas.exchange_definitions import is_beta_exchange
from atlas.exchange_definitions.extended import (
    extended_symbols,
    extended_underlying,
    fetch_extended_perps,
)
from atlas.exchange_definitions.lighter import (
    fetch_lighter_perps,
    lighter_symbol,
    lighter_symbols,
)
from atlas.exchanges import (
    get_allowed_types,
    get_exchange_fetcher,
    to_tardis_exchange_id,
)
from atlas.parsers import SkipSymbol, parse_contract
from atlas.types import UnderlyingType

DATA = Path(__file__).parent / "data" / "defi_b"


def _payload(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _rows(exchange: str, symbols: list[dict]) -> dict[str, dict]:
    return {sd["id"]: sd for sd in symbols}


def _iid(exchange: str, sd: dict) -> str:
    return parse_contract(exchange, sd).internal_id


class TestRegistration:
    @pytest.mark.parametrize("exchange", ["lighter-perps", "extended-perps"])
    def test_stable_perpetual_only_with_an_api_fetcher(self, exchange):
        # Beta exchanges are skipped by update(); these have no other source.
        assert is_beta_exchange(exchange) is False
        assert get_allowed_types(exchange) == {"perpetual"}
        assert get_exchange_fetcher(exchange) is not None

    def test_lighter_reads_tardis_under_its_tardis_id(self):
        # Tardis archives Lighter as "lighter", keyed by the same market ids.
        assert to_tardis_exchange_id("lighter-perps") == "lighter"


class TestLighter:
    symbols = lighter_symbols(_payload("lighter_order_book_details.json"))

    def test_perp_markets_only_inactive_included(self):
        rows = _rows("lighter-perps", self.symbols)
        assert len(rows) == 239
        assert rows["22"]["base_asset"] == "AI16Z"  # inactive, kept for history
        assert not any(sd["id"] == "2048" for sd in self.symbols)  # spot ETH/USDC
        assert all(
            sd["type"] == "perpetual" and sd["contract_size"] == 1.0
            for sd in self.symbols
        )

    @pytest.mark.parametrize(
        ("market_id", "expected"),
        [
            ("0", "perpetual-ETH-USDC:USDC"),
            ("4", "perpetual-1000PEPE-USDC:USDC"),
            ("92", "perpetual-XAU-USDC:USDC"),
            ("96", "perpetual-EUR-USDC:USDC"),  # EURUSD: a USD-quoted pair is its base
            ("97", "perpetual-GBP-USDC:USDC"),
            ("98", "perpetual-USDJPY-USDC:USDC"),  # USD base: the full pair stays
            ("110", "perpetual-NVDA-USDC:USDC"),
            ("160", "perpetual-HYUNDAIUSD-USDC:USDC"),  # an equity, not an FX pair
            ("180", "perpetual-US500-USDC:USDC"),
            ("192", "perpetual-OPENAI-USDC:USDC"),
        ],
    )
    def test_internal_ids(self, market_id, expected):
        sd = _rows("lighter-perps", self.symbols)[market_id]
        assert _iid("lighter-perps", sd) == expected

    def test_internal_ids_are_unique(self):
        ids = [_iid("lighter-perps", sd) for sd in self.symbols]
        assert len(ids) == len(set(ids))

    def test_symbol_rule(self):
        assert lighter_symbol("EURUSD") == "EUR"
        assert lighter_symbol("USDKRW") == "USDKRW"
        assert lighter_symbol("ABCUSD") == "ABCUSD"

    @pytest.mark.parametrize(
        ("name", "underlying"),
        [
            ("BTC", "crypto"),
            ("XAU", "commodity"),
            ("WTI", "commodity"),
            ("EURUSD", "forex"),
            ("USDJPY", "forex"),
            ("NVDA", "equity"),
            ("SPY", "equity"),
            ("TENCENT", "equity"),
            ("US500", "index"),
            ("US10Y", "index"),
            ("OPENAI", "pre_market"),
            ("ANTHROPIC", "pre_market"),
            ("AI", "crypto"),
            ("SAMSUNG", "equity"),  # inactive
        ],
    )
    def test_underlying(self, name, underlying):
        by_name = {sd["base_asset"]: sd for sd in self.symbols}
        assert by_name[name]["underlying"] == underlying

    def test_every_live_market_has_a_known_underlying(self):
        # RWA classification is the point: no active market may fall to unknown.
        payload = _payload("lighter_order_book_details.json")
        active = {
            str(m["market_id"])
            for m in payload["order_book_details"]
            if m["status"] == "active"
        }
        unknown = [
            sd["base_asset"]
            for sd in self.symbols
            if sd["id"] in active and sd["underlying"] == "unknown"
        ]
        assert unknown == []
        assert (
            Counter(sd["underlying"] for sd in self.symbols if sd["id"] in active)[
                "forex"
            ]
            == 9
        )

    def test_listing_time_is_the_first_capture(self):
        # The daily update reads Lighter's API only; its listing time is the
        # best start there is (rows on file keep their own first_capture).
        rows = _rows("lighter-perps", self.symbols)
        assert rows["0"]["first_capture"] == "2025-01-17T07:21:01.100Z"

    def test_missing_base_asset_is_skipped(self):
        with pytest.raises(SkipSymbol):
            parse_contract("lighter-perps", {"id": "5", "type": "perpetual"})

    def test_fetch_retries_then_succeeds(self):
        good = MagicMock()
        good.json.return_value = _payload("lighter_order_book_details.json")
        good.raise_for_status.return_value = None
        with (
            patch(
                "atlas.exchange_definitions.lighter.requests.get",
                side_effect=[requests.ConnectionError("x"), good],
            ) as get,
            patch("tenacity.nap.time.sleep"),
        ):
            symbols = fetch_lighter_perps(5)
        assert len(symbols) == 239
        assert get.call_args.args[0].endswith("/api/v1/orderBookDetails?filter=perp")


class TestExtended:
    symbols = extended_symbols(_payload("extended_markets.json"))

    def test_perpetuals_of_every_status(self):
        payload = _payload("extended_markets.json")["data"]
        perps = [m for m in payload if m["type"] == "PERPETUAL"]
        assert len(self.symbols) == len(perps) == 403  # spot markets skipped
        assert {m["status"] for m in perps} >= {
            "ACTIVE",
            "DELISTED",
            "PRELISTED",
            "REDUCE_ONLY",
        }

    @pytest.mark.parametrize(
        ("market", "expected", "underlying"),
        [
            ("BTC-USD", "perpetual-BTC-USD:USD", "crypto"),
            ("XAUT-USD", "perpetual-XAUT-USD:USD", "crypto"),
            ("EUR-USD", "perpetual-EUR-USD:USD", "forex"),
            ("USDJPY-USD", "perpetual-USDJPY-USD:USD", "forex"),
            ("XAU-USD", "perpetual-XAU-USD:USD", "commodity"),
            ("TSLA_24_5-USD", "perpetual-TSLA_24_5-USD:USD", "equity"),
            ("SPX500m-USD", "perpetual-SPX500M-USD:USD", "index"),
            ("KR200-USD", "perpetual-KR200-USD:USD", "index"),
            ("DRAM-USD", "perpetual-DRAM-USD:USD", "equity"),
            ("KORU-USD", "perpetual-KORU-USD:USD", "equity"),
            ("OPENAI-USD", "perpetual-OPENAI-USD:USD", "pre_market"),
        ],
    )
    def test_internal_id_and_underlying(self, market, expected, underlying):
        sd = _rows("extended-perps", self.symbols)[market]
        assert _iid("extended-perps", sd) == expected
        assert sd["underlying"] == underlying

    def test_every_trading_market_has_a_known_underlying(self):
        payload = {m["name"]: m for m in _payload("extended_markets.json")["data"]}
        unknown = [
            sd["id"]
            for sd in self.symbols
            if payload[sd["id"]]["status"] in ("ACTIVE", "REDUCE_ONLY")
            and sd["underlying"] == "unknown"
        ]
        assert unknown == []

    def test_underlying_of_unclassified_markets(self):
        assert (
            extended_underlying("RWA", "TradFi", "PLACE_JPY") == UnderlyingType.unknown
        )
        assert extended_underlying("Other", None, None) == UnderlyingType.unknown
        assert extended_underlying("L1", "L1", "FTM") == UnderlyingType.crypto

    def test_non_ok_payload_is_retried_then_raised(self):
        resp = MagicMock()
        resp.json.return_value = {"status": "ERROR"}
        with (
            patch(
                "atlas.exchange_definitions.extended.requests.get", return_value=resp
            ) as get,
            patch("tenacity.nap.time.sleep"),
            pytest.raises(ValueError),
        ):
            fetch_extended_perps(5)
        assert get.call_count == 3


@pytest.mark.parametrize(
    ("exchange", "fetcher_module", "fetcher", "payload", "original_id", "internal_id"),
    [
        (
            "lighter-perps",
            "lighter",
            "fetch_lighter_perps",
            "lighter_order_book_details.json",
            "96",
            "perpetual-EUR-USDC:USDC",
        ),
        (
            "extended-perps",
            "extended",
            "fetch_extended_perps",
            "extended_markets.json",
            "SPX500m-USD",
            "perpetual-SPX500M-USD:USD",
        ),
    ],
)
def test_exchange_source_update_writes_a_loadable_snapshot(
    tmp_path,
    monkeypatch,
    exchange,
    fetcher_module,
    fetcher,
    payload,
    original_id,
    internal_id,
):
    """The --source exchange path: rows carry an internal_id and a first_capture
    (their first observation), without which SecurityMaster would not index
    them; a second run keeps that first observation."""
    updater = importlib.import_module("atlas.update")
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    module = importlib.import_module(f"atlas.exchange_definitions.{fetcher_module}")
    symbols = getattr(
        module, fetcher.replace("fetch_", "").replace("_perps", "_symbols")
    )(_payload(payload))
    source = SimpleNamespace(
        fetch_exchange=lambda _exchange: {"availableSymbols": symbols}
    )
    assert updater.update([exchange], source) == []
    rows = json.loads((tmp_path / f"{exchange}.json").read_text())
    assert rows
    assert all(r.get("internal_id") for r in rows)
    assert all(r.get("first_capture") for r in rows)
    assert all(r.get("underlying") for r in rows)
    first = {r["id"]: r["first_capture"] for r in rows}
    assert updater.update([exchange], source) == []
    again = json.loads((tmp_path / f"{exchange}.json").read_text())
    assert {r["id"]: r["first_capture"] for r in again} == first
    sm = SecurityMaster.load(data_dir=tmp_path)
    assert sm.by_exchange_and_original_id(exchange, original_id) == internal_id
    assert all(r["contract_size"] == 1.0 for r in rows)
    assert parse_contract(exchange, rows[0]).instrument_type == ContractType.perpetual
