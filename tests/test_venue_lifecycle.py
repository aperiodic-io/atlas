"""Listing and delisting signals of the venues added for live-producers.

The daily update reads every exchange's whole current universe from its API
(`SymbolLifecycle.COMPLETE`): a row the fetcher reports with an `end_date` is
closed right away, a row it no longer returns is closed once a second run
confirms the miss, and every other returned row counts as listed. So each
fetcher must (a) still return halted, paused and suspended instruments, which
would otherwise be closed as delisted, (b) report the venue's own delisted
status as an `end_date`, at the venue's delisting time where it publishes
one, and (c) give a listing time as `first_capture` where the venue publishes
one. Payload fields and values are from the live APIs on 2026-10-19.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from atlas.exchange_definitions.apex import apex_symbols
from atlas.exchange_definitions.aster import aster_symbols
from atlas.exchange_definitions.avantis import avantis_symbols
from atlas.exchange_definitions.bitget import fetch_bitget_perpetual_usdt
from atlas.exchange_definitions.common import parse_utc_timestamp
from atlas.exchange_definitions.deribit import fetch_deribit_perpetual
from atlas.exchange_definitions.extended import extended_symbols
from atlas.exchange_definitions.gate_io import fetch_gateio_perpetual_usdt
from atlas.exchange_definitions.grvt import fetch_grvt_perps
from atlas.exchange_definitions.huobi import fetch_htx_perpetual_usdt
from atlas.exchange_definitions.lighter import lighter_symbols
from atlas.exchange_definitions.mexc import fetch_mexc_perpetual
from atlas.update_sources import SymbolLifecycle

updater = importlib.import_module("atlas.update")


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _fetch(fetcher, payload: object) -> list[dict]:
    """Run a CEX fetcher against one canned `get_json` response."""
    with patch(
        "atlas.exchange_definitions.http.requests.get", return_value=_response(payload)
    ):
        return fetcher(timeout_seconds=5)


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {sd["id"]: sd for sd in rows}


class _Clock:
    """Brackets a call so a first-sighting `end_date` can be checked against now."""

    def __enter__(self):
        self.before = datetime.now(UTC).replace(microsecond=0)
        return self

    def __exit__(self, *exc):
        self.after = datetime.now(UTC)

    def assert_now(self, value: str) -> None:
        assert self.before <= parse_utc_timestamp(value) <= self.after, value


def _assert_listed(sd: dict) -> None:
    assert "end_date" not in sd, sd


# ---------------------------------------------------------------------------
# Venues that publish a delisting time
# ---------------------------------------------------------------------------


def _aster(symbol: str, status: str, onboard: int, delivery: int, ctype="PERPETUAL") -> dict:
    return {
        "symbol": symbol, "status": status, "contractType": ctype, "onboardDate": onboard,
        "deliveryDate": delivery, "baseAsset": symbol[:-4], "quoteAsset": "USDT",
        "marginAsset": "USDT", "underlyingType": "COIN", "tags": [],
    }  # fmt: skip


class TestAster:
    # Aster's futures API is Binance's: SETTLING/CLOSE/DELIVERED contracts have
    # stopped trading at `deliveryDate`, and perps keep a 2100 sentinel there
    # until a delist time is set.
    PAYLOAD: ClassVar[dict] = {
        "symbols": [
            _aster("BTCUSDT", "TRADING", 1709622000000, 4133404800000),
            _aster("TONUSDT", "SETTLING", 1709622000000, 1781859600000),
            _aster("NEWUSDT", "SETTLING", 1709622000000, 4133404800000),
            _aster("MBLUSDT", "PENDING_TRADING", 1699974000000, 4133404800000, ctype=""),
        ]
    }

    def test_settling_perp_ends_at_its_delivery_date(self):
        rows = _by_id(aster_symbols(self.PAYLOAD))
        assert rows["tonusdt"]["end_date"] == "2026-06-19T09:00:00.000Z"
        _assert_listed(rows["btcusdt"])

    def test_settling_perp_without_a_passed_delist_time_ends_now(self):
        with _Clock() as clock:
            rows = _by_id(aster_symbols(self.PAYLOAD))
        clock.assert_now(rows["newusdt"]["end_date"])

    def test_onboard_date_is_the_listing_time(self):
        rows = _by_id(aster_symbols(self.PAYLOAD))
        assert rows["btcusdt"]["first_capture"] == "2024-03-05T07:00:00.000Z"

    def test_contracts_not_yet_trading_are_left_out(self):
        assert "mblusdt" not in _by_id(aster_symbols(self.PAYLOAD))


def _bitget(symbol: str, status: str, off_time="-1", launch_time="0") -> dict:
    return {
        "symbol": symbol, "status": status, "type": "perpetual", "baseCoin": symbol[:-4],
        "quoteCoin": "USDT", "offTime": off_time, "launchTime": launch_time,
    }  # fmt: skip


class TestBitget:
    # v3 statuses: listed (not trading yet), online, limit_open / limit_close /
    # restrictedAPI (temporary trading restrictions), offline (delisted at `offTime`).
    PAYLOAD: ClassVar[dict] = {
        "data": [
            _bitget("BTCUSDT", "online"),
            _bitget("AUSDT", "limit_open", launch_time="1758124392000"),
            _bitget("BUSDT", "limit_close"),
            _bitget("CUSDT", "restrictedAPI"),
            _bitget("DUSDT", "offline", off_time="1781859600000"),
            _bitget("EUSDT", "offline"),
            _bitget("FUSDT", "listed"),
        ]
    }

    def test_restricted_contracts_stay_listed(self):
        rows = _by_id(_fetch(fetch_bitget_perpetual_usdt, self.PAYLOAD))
        for sid in ("BTCUSDT", "AUSDT", "BUSDT", "CUSDT"):
            _assert_listed(rows[sid])

    def test_offline_contract_ends_at_its_off_time(self):
        rows = _by_id(_fetch(fetch_bitget_perpetual_usdt, self.PAYLOAD))
        assert rows["DUSDT"]["end_date"] == "2026-06-19T09:00:00.000Z"

    def test_offline_contract_without_an_off_time_ends_now(self):
        with _Clock() as clock:
            rows = _by_id(_fetch(fetch_bitget_perpetual_usdt, self.PAYLOAD))
        clock.assert_now(rows["EUSDT"]["end_date"])

    def test_contracts_not_yet_trading_are_left_out(self):
        assert "FUSDT" not in _by_id(_fetch(fetch_bitget_perpetual_usdt, self.PAYLOAD))

    def test_launch_time_is_the_listing_time_on_every_category(self):
        rows = _by_id(_fetch(fetch_bitget_perpetual_usdt, self.PAYLOAD))
        assert rows["AUSDT"]["first_capture"] == "2025-09-17T15:53:12.000Z"
        # `0` means Bitget did not record one.
        assert "first_capture" not in rows["BTCUSDT"]


def _htx(code: str, status: int, delivery_time="", create_date="20201021") -> dict:
    return {
        "contract_code": code, "business_type": "swap", "contract_status": status,
        "delivery_time": delivery_time, "create_date": create_date, "contract_size": 0.001,
        "trade_partition": "USDT", "tradfi_labels": [],
    }  # fmt: skip


class TestHtx:
    # contract_status: 0 delisted, 1 listed, 2 pending listing, 3 suspended,
    # 4 listing suspended, 5 settling, 6 delivering, 7 settled, 8 delivered. A swap
    # scheduled for delisting carries the time in `delivery_time` (empty otherwise).
    PAYLOAD: ClassVar[dict] = {
        "data": [
            _htx("BTC-USDT", 1),
            _htx("PAUSE-USDT", 3),
            _htx("HALT-USDT", 4),
            _htx("SETTLE-USDT", 5),
            _htx("DOS-USDT", 3, delivery_time="1792137600000", create_date="20260812"),
            _htx("SOON-USDT", 1, delivery_time="4133404800000"),
            _htx("GONE-USDT", 0),
            _htx("NEXT-USDT", 2),
        ]
    }

    def test_suspended_and_settling_swaps_stay_listed(self):
        rows = _by_id(_fetch(fetch_htx_perpetual_usdt, self.PAYLOAD))
        for sid in ("BTC-USDT", "PAUSE-USDT", "HALT-USDT", "SETTLE-USDT", "SOON-USDT"):
            _assert_listed(rows[sid])

    def test_swap_past_its_delisting_time_ends_then(self):
        rows = _by_id(_fetch(fetch_htx_perpetual_usdt, self.PAYLOAD))
        assert rows["DOS-USDT"]["end_date"] == "2026-10-16T08:00:00.000Z"

    def test_delisted_swap_ends_now(self):
        with _Clock() as clock:
            rows = _by_id(_fetch(fetch_htx_perpetual_usdt, self.PAYLOAD))
        clock.assert_now(rows["GONE-USDT"]["end_date"])

    def test_pending_listing_is_left_out(self):
        assert "NEXT-USDT" not in _by_id(_fetch(fetch_htx_perpetual_usdt, self.PAYLOAD))

    def test_create_date_is_the_listing_time(self):
        rows = _by_id(_fetch(fetch_htx_perpetual_usdt, self.PAYLOAD))
        assert rows["DOS-USDT"]["first_capture"] == "2026-08-12T00:00:00.000Z"


# ---------------------------------------------------------------------------
# Venues that flag a delisting without a time (first sighting, like Hyperliquid)
# ---------------------------------------------------------------------------


def _extended(name: str, status: str, created=1737098461318) -> dict:
    return {
        "name": name, "type": "PERPETUAL", "status": status, "assetName": name.split("-")[0],
        "collateralAssetName": "USD", "category": "L1", "createdAt": created,
    }  # fmt: skip


class TestExtended:
    PAYLOAD: ClassVar[dict] = {
        "data": [
            _extended("BTC-USD", "ACTIVE"),
            _extended("ENA-USD", "REDUCE_ONLY"),
            _extended("NEW-USD", "PRELISTED"),
            _extended("OLD-USD", "DELISTED"),
        ]
    }

    def test_delisted_market_ends_now(self):
        with _Clock() as clock:
            rows = _by_id(extended_symbols(self.PAYLOAD))
        clock.assert_now(rows["OLD-USD"]["end_date"])

    def test_reduce_only_and_prelisted_markets_stay_listed(self):
        rows = _by_id(extended_symbols(self.PAYLOAD))
        for sid in ("BTC-USD", "ENA-USD", "NEW-USD"):
            _assert_listed(rows[sid])

    def test_created_at_is_the_listing_time(self):
        rows = _by_id(extended_symbols(self.PAYLOAD))
        assert rows["BTC-USD"]["first_capture"] == "2025-01-17T07:21:01.318Z"
        assert "listing_date" not in rows["BTC-USD"]


def _lighter(market_id: int, symbol: str, status: str) -> dict:
    return {
        "market_id": market_id, "symbol": symbol, "market_type": "perp", "status": status,
        "strategy_index": 2, "multiplier": "1", "created_at": 1737098461318,
    }  # fmt: skip


class TestLighter:
    PAYLOAD: ClassVar[dict] = {
        "order_book_details": [_lighter(1, "BTC", "active"), _lighter(28, "MKR", "inactive")]
    }

    def test_inactive_market_ends_now(self):
        with _Clock() as clock:
            rows = _by_id(lighter_symbols(self.PAYLOAD))
        clock.assert_now(rows["28"]["end_date"])
        _assert_listed(rows["1"])

    def test_created_at_is_the_listing_time(self):
        rows = _by_id(lighter_symbols(self.PAYLOAD))
        assert rows["1"]["first_capture"] == "2025-01-17T07:21:01.318Z"
        assert "listing_date" not in rows["1"]


def _grvt(instrument: str, status: str) -> dict:
    return {
        "instrument": instrument, "kind": "PERPETUAL", "status": status,
        "base": instrument.split("_")[0], "quote": "USDT", "asset_class": "CRYPTO",
        "create_time": "1790754389133797880",
    }  # fmt: skip


class TestGrvt:
    PAYLOAD: ClassVar[dict] = {"result": [_grvt("BTC_USDT_Perp", "ACTIVE"), _grvt("OLD_USDT_Perp", "DELISTED")]}

    def _fetch(self) -> tuple[list[dict], MagicMock]:
        post = MagicMock(return_value=_response(self.PAYLOAD))
        with patch("atlas.exchange_definitions.grvt.requests.post", post):
            return fetch_grvt_perps(timeout_seconds=5), post

    def test_delisted_instruments_are_requested(self):
        # `is_active: true` filters delisted instruments out server-side, which
        # would leave nothing to report them by.
        _, post = self._fetch()
        assert post.call_args.kwargs["json"] == {"is_active": False}

    def test_delisted_perp_ends_now(self):
        with _Clock() as clock:
            rows, _ = self._fetch()
        rows = _by_id(rows)
        clock.assert_now(rows["OLD_USDT_Perp"]["end_date"])
        _assert_listed(rows["BTC_USDT_Perp"])


def _avantis(base: str, quote: str, listed: bool) -> dict:
    return {"from": base, "to": quote, "isPairListed": listed, "groupIndex": 0, "feed": {}}


class TestAvantis:
    # `isPairListed` is the on-chain delisting flag; delisted pairs keep their
    # index (some with a blanked or "DELISTED" name).
    CATALOG: ClassVar[dict] = {
        "groupInfo": {"0": {"name": "crypto"}},
        "pairInfos": {
            "0": _avantis("ETH", "USD", True),
            "22": _avantis("SHIB", "USD", False),
            "32": _avantis("", "", False),
            "35": _avantis("DELISTED", "USD", False),
        },
    }

    def test_unlisted_pairs_end_now(self):
        with _Clock() as clock:
            rows = _by_id(avantis_symbols(self.CATALOG))
        for sid in ("22", "32", "35"):
            clock.assert_now(rows[sid]["end_date"])
        _assert_listed(rows["0"])


def _mexc(symbol: str, state: int) -> dict:
    return {
        "symbol": symbol, "state": state, "baseCoin": symbol.split("_")[0], "quoteCoin": "USDT",
        "settleCoin": "USDT", "contractSize": 0.0001, "openingTime": 0,
        "createTime": 1591242684000,
    }  # fmt: skip


class TestMexc:
    # state: 0 enabled, 1 delivering, 2 delivered, 3 offline, 4 paused.
    PAYLOAD: ClassVar[dict] = {
        "data": [
            _mexc("BTC_USDT", 0),
            _mexc("DLV_USDT", 1),
            _mexc("DONE_USDT", 2),
            _mexc("OFF_USDT", 3),
            _mexc("PAUSE_USDT", 4),
        ]
    }

    def test_paused_and_delivering_contracts_stay_listed(self):
        rows = _by_id(_fetch(fetch_mexc_perpetual, self.PAYLOAD))
        for sid in ("BTC_USDT", "DLV_USDT", "PAUSE_USDT"):
            _assert_listed(rows[sid])

    def test_offline_and_delivered_contracts_end_now(self):
        with _Clock() as clock:
            rows = _by_id(_fetch(fetch_mexc_perpetual, self.PAYLOAD))
        clock.assert_now(rows["OFF_USDT"]["end_date"])
        clock.assert_now(rows["DONE_USDT"]["end_date"])


def _gate(name: str, status: str) -> dict:
    return {
        "name": name, "status": status, "in_delisting": status == "delisting",
        "quanto_multiplier": "0.0001", "type": "direct", "contract_type": "",
        "create_time": 1758124392, "launch_time": 1758124392,
    }  # fmt: skip


class TestGate:
    # status: prelaunch, trading, delisting (reduce-only wind-down), delisted,
    # circuit_breaker (temporary halt).
    PAYLOAD: ClassVar[list] = [
        _gate("BTC_USDT", "trading"),
        _gate("HALT_USDT", "circuit_breaker"),
        _gate("WIND_USDT", "delisting"),
        _gate("GONE_USDT", "delisted"),
    ]

    def test_halted_and_winding_down_contracts_stay_listed(self):
        rows = _by_id(_fetch(fetch_gateio_perpetual_usdt, self.PAYLOAD))
        for sid in ("BTC_USDT", "HALT_USDT", "WIND_USDT"):
            _assert_listed(rows[sid])

    def test_delisted_contract_ends_now(self):
        with _Clock() as clock:
            rows = _by_id(_fetch(fetch_gateio_perpetual_usdt, self.PAYLOAD))
        clock.assert_now(rows["GONE_USDT"]["end_date"])

    def test_launch_time_is_the_listing_time(self):
        rows = _by_id(_fetch(fetch_gateio_perpetual_usdt, self.PAYLOAD))
        assert rows["BTC_USDT"]["first_capture"] == "2025-09-17T15:53:12.000Z"


# ---------------------------------------------------------------------------
# Venues whose API has no delisted status: a delisting is the instrument
# disappearing, which the updater confirms over two runs
# ---------------------------------------------------------------------------


def _apex(name: str, trade: bool, display: bool) -> dict:
    return {
        "crossSymbolName": name, "baseTokenId": name[:-4], "settleAssetId": "USDT",
        "enableTrade": trade, "enableDisplay": display, "category": "",
    }  # fmt: skip


class TestApex:
    # ApeX keeps retired contracts in the list with trading and display both off,
    # forever, and has no status that tells a retirement from a pause; a retired
    # contract is therefore left out, so the updater's two-run confirmation and
    # mass-disappearance guard decide.
    PAYLOAD: ClassVar[dict] = {
        "data": {
            "contractConfig": {
                "perpetualContract": [
                    _apex("BTCUSDT", True, True),
                    _apex("IOUSDT", True, False),
                    _apex("TONUSDT", False, False),
                ],
                "stockContract": [],
            }
        }
    }

    def test_tradeable_hidden_contract_stays_listed(self):
        rows = _by_id(apex_symbols(self.PAYLOAD))
        _assert_listed(rows["BTCUSDT"])
        _assert_listed(rows["IOUSDT"])

    def test_retired_contract_is_left_out(self):
        assert "TONUSDT" not in _by_id(apex_symbols(self.PAYLOAD))


class TestDeribit:
    PAYLOAD: ClassVar[dict] = {
        "result": [
            {
                "instrument_name": "BTC-PERPETUAL", "settlement_period": "perpetual",
                "instrument_type": "reversed", "base_currency": "BTC", "quote_currency": "USD",
                "settlement_currency": "BTC", "contract_size": 10, "is_active": True,
                "creation_timestamp": 1534242287000, "underlying_type": "crypto",
            }
        ]
    }  # fmt: skip

    def test_creation_timestamp_is_the_listing_time(self):
        rows = _by_id(_fetch(fetch_deribit_perpetual, self.PAYLOAD))
        assert rows["BTC-PERPETUAL"]["first_capture"] == "2018-08-14T10:24:47.000Z"


# ---------------------------------------------------------------------------
# End to end: a fetcher's delisting closes the row on file
# ---------------------------------------------------------------------------


def test_reported_delisting_closes_the_row_on_file(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    path = tmp_path / "extended-perps.json"
    open_row = {
        "id": "OLD-USD", "type": "perpetual", "base_asset": "OLD", "margin_asset": "USD",
        "first_capture": "2025-01-17T07:21:01.318Z",
    }  # fmt: skip
    path.write_text(json.dumps([open_row]))
    rows = extended_symbols({"data": [_extended("OLD-USD", "DELISTED")]})
    source = SimpleNamespace(
        lifecycle=SymbolLifecycle.COMPLETE,
        fetch_exchange=lambda _exchange: {"availableSymbols": rows},
    )

    with _Clock() as clock:
        assert updater.update(["extended-perps"], source) == []

    (row,) = json.loads(path.read_text())
    clock.assert_now(row["end_date"])
    assert row["first_capture"] == open_row["first_capture"]


@pytest.mark.parametrize(
    "exchange",
    [
        "aster-perps", "apex-perps", "avantis-perps", "extended-perps",
        "grvt-perps", "lighter-perps", "pacifica-perps", "variational-perps",
        "gateio-perps-usdt", "gateio-perps-btc", "gateio-futures-usdt",
        "htx-perps-usdt", "mexc-perps", "bitget-perps-usdt",
        "bitget-perps-usdc", "bitget-perps-coin", "bitget-futures-coin",
        "deribit-perps", "deribit-futures", "whitebit-perps",
        "cryptocom-perps", "cryptocom-futures",
    ],
)  # fmt: skip
def test_daily_update_refreshes_the_venue(exchange):
    # The daily update is what applies these signals; a venue it skips never
    # sees a delisting.
    from atlas.exchange_definitions import is_beta_exchange

    assert exchange in updater.EXCHANGES
    assert not is_beta_exchange(exchange)
