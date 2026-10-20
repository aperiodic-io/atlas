"""Poloniex exchange-API fetcher (poloniex-spot) and its parser.

Fixtures are excerpts of real responses captured on 2026-10-20:
`markets.json` from `/markets` (NORMAL and PAUSE markets, markets migrated
from the v1 API, later listings, and paused markets whose tradable start is a
2099 placeholder) and `tardis.json` Tardis's Poloniex symbol list for the
same markets plus v1-era `QUOTE_BASE` ids.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.exchange_definitions import poloniex
from atlas.exchange_definitions.common import SkipSymbol
from atlas.exchange_definitions.poloniex import (
    fetch_poloniex_spot,
    parse_poloniex,
    parse_poloniex_api,
)

FIXTURES = Path(__file__).parent / "fixtures" / "poloniex"
MARKETS = json.loads((FIXTURES / "markets.json").read_text())
TARDIS = json.loads((FIXTURES / "tardis.json").read_text())
MARKET = {m["symbol"]: m for m in MARKETS}


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _fetch(payload=MARKETS) -> list[dict]:
    with patch("atlas.exchange_definitions.http.requests.get", return_value=_response(payload)):
        return fetch_poloniex_spot(timeout_seconds=5)


@pytest.fixture(scope="module")
def rows() -> dict[str, dict]:
    return {sd["id"]: sd for sd in _fetch() if not sd.get("upcoming")}


def _iid(sd: dict) -> str:
    return parse_poloniex_api("poloniex-spot", sd).internal_id


def _tardis_row(row: dict) -> dict:
    """A Tardis row as the snapshot holds it (availability mapped to dates)."""
    sd = {"id": row["id"], "type": row["type"], "first_capture": row["availableSince"]}
    if "availableTo" in row:
        sd["end_date"] = row["availableTo"]
    return sd


class TestFetch:
    def test_markets_not_yet_tradable_are_flagged_upcoming(self, rows):
        placeholder = {"COOL_USDT", "GASSOL_USDT"}
        assert all(MARKET[s]["tradableStartTime"] > 4_000_000_000_000 for s in placeholder)
        assert set(rows) == set(MARKET) - placeholder
        upcoming = [sd for sd in _fetch() if sd.get("upcoming")]
        assert upcoming == [
            {"id": sid, "type": "spot", "upcoming": True} for sid in MARKET if sid in placeholder
        ]

    def test_row_shape(self, rows):
        assert rows["A_USDT"] == {
            "id": "A_USDT",
            "type": "spot",
            "base_coin": "A",
            "quote_coin": "USDT",
            "underlying": "crypto",
            "first_capture": "2025-05-31T15:00:00.000Z",
        }

    def test_ids_are_the_tardis_ids(self, rows):
        tardis_ids = {s["id"] for s in TARDIS}
        # Paused markets that never traded are unknown to Tardis.
        assert set(rows) - tardis_ids == {"EPIC_USDT", "BOBOOLD_USDT"}

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTC_USDT", "spot-BTC-USDT"),
            ("ETH_BTC", "spot-ETH-BTC"),
            ("TRX_BTC", "spot-TRX-BTC"),
            ("BTC_BNB", "spot-BTC-BNB"),
            ("ETH_TRX", "spot-ETH-TRX"),
            ("USDT_USDC", "spot-USDT-USDC"),
            ("USDD_USDT", "spot-USDD-USDT"),
            ("BTC_USDD", "spot-BTC-USDD"),
            ("OETH_ETH", "spot-OETH-ETH"),
            ("A_USDT", "spot-A-USDT"),
        ],
    )
    def test_internal_ids(self, rows, sid, internal_id):
        assert _iid(rows[sid]) == internal_id

    def test_every_quote_asset_is_covered(self, rows):
        assert {sd["quote_coin"] for sd in rows.values()} >= {"USDT", "BTC", "USDC", "TRX", "ETH", "BNB", "USDD"}

    def test_paused_markets_stay_listed(self, rows):
        for sid in ("XRPBULL_USDT", "OCEAN_USDT", "OETH_ETH", "EPIC_USDT"):
            assert MARKET[sid]["state"] == "PAUSE"
            assert "end_date" not in rows[sid]

    def test_post_only_markets_stay_listed(self):
        payload = [{**MARKET["BTC_USDT"], "state": "POST_ONLY"}]
        assert "end_date" not in _fetch(payload)[0]

    def test_offline_markets_end(self):
        payload = [{**MARKET["BTC_USDT"], "state": "OFFLINE"}, MARKET["ETH_BTC"]]
        fetched = {sd["id"]: sd for sd in _fetch(payload)}
        assert fetched["BTC_USDT"]["end_date"].endswith("Z")
        assert "end_date" not in fetched["ETH_BTC"]

    def test_listing_time_is_tradable_start(self, rows):
        assert rows["RENDER_USDT"]["first_capture"] == "2024-08-01T08:10:00.000Z"
        assert rows["OETH_ETH"]["first_capture"] == "2024-03-14T12:00:00.000Z"

    def test_v1_migration_time_is_not_a_listing_time(self, rows):
        # Markets carried over from the v1 API report 2022-07-28.
        for sid in ("BTC_USDT", "DASH_BTC", "OCEAN_USDT", "EPIC_USDT"):
            assert MARKET[sid]["tradableStartTime"] < 1659398400000
            assert "first_capture" not in rows[sid]

    def test_market_becomes_listed_once_tradable(self):
        listing = {**MARKET["A_USDT"], "tradableStartTime": 1_800_000_000_000}
        with patch.object(poloniex.time, "time", return_value=1_799_999_999.0):
            assert _fetch([listing]) == [{"id": "A_USDT", "type": "spot", "upcoming": True}]
        with patch.object(poloniex.time, "time", return_value=1_800_000_000.0):
            assert [sd["id"] for sd in _fetch([listing])] == ["A_USDT"]

    def test_all_crypto(self, rows):
        assert {sd["underlying"] for sd in rows.values()} == {"crypto"}

    def test_spot_has_no_margin_even_with_cross_margin(self, rows):
        margined = [s for s, m in MARKET.items() if m["crossMargin"]["supportCrossMargin"] and s in rows]
        assert margined
        for sid in margined:
            assert "margin_asset" not in rows[sid]
            assert parse_poloniex_api("poloniex-spot", rows[sid]).margin is None


class TestMalformedPayloads:
    @pytest.mark.parametrize(
        "payload",
        [
            {"code": 500, "message": "System error"},
            None,
            "BTC_USDT",
            [{"state": "NORMAL"}],
            ["BTC_USDT"],
        ],
    )
    def test_unexpected_shape_raises(self, payload):
        with pytest.raises(ValueError):
            _fetch(payload)


class TestTardisFallback:
    @pytest.mark.parametrize(
        ("sid", "end_date", "internal_id"),
        [
            # Current BASE_QUOTE ids, open or delisted after the 2022-08-02 switch.
            ("BTC_USDT", None, "spot-BTC-USDT"),
            ("ETH_BTC", None, "spot-ETH-BTC"),
            ("BTC_BNB", None, "spot-BTC-BNB"),
            ("USDT_USDC", None, "spot-USDT-USDC"),
            ("ADEL_USDT", "2022-08-10T00:00:00.000Z", "spot-ADEL-USDT"),
            ("BTT_USDJ", "2022-08-11T00:00:00.000Z", "spot-BTT-USDJ"),
            ("BNB_BUSD", "2023-04-28T00:00:00.000Z", "spot-BNB-BUSD"),
            # v1 QUOTE_BASE ids, all ended at the switch.
            ("USDT_BTC", "2022-08-02T00:00:00.000Z", "spot-BTC-USDT"),
            ("BTC_ETH", "2022-08-02T00:00:00.000Z", "spot-ETH-BTC"),
            ("BNB_BTC", "2022-08-02T00:00:00.000Z", "spot-BTC-BNB"),
            ("USDC_USDT", "2022-08-02T00:00:00.000Z", "spot-USDT-USDC"),
            ("TRX_ETH", "2022-08-02T00:00:00.000Z", "spot-ETH-TRX"),
            ("USDT_USDD", "2022-08-02T00:00:00.000Z", "spot-USDD-USDT"),
            ("BUSD_BNB", "2022-08-02T00:00:00.000Z", "spot-BNB-BUSD"),
            ("DAI_BTC", "2022-08-02T00:00:00.000Z", "spot-BTC-DAI"),
            ("PAX_ETH", "2022-08-02T00:00:00.000Z", "spot-ETH-PAX"),
            ("USDJ_BTT", "2022-08-02T00:00:00.000Z", "spot-BTT-USDJ"),
            ("USDT_ADEL", "2022-08-02T00:00:00.000Z", "spot-ADEL-USDT"),
            ("USDT_eRSDL", "2022-08-02T00:00:00.000Z", "spot-ERSDL-USDT"),
            ("USDT_BTC", "2020-11-25T00:00:00.000Z", "spot-BTC-USDT"),
        ],
    )
    def test_ids(self, sid, end_date, internal_id):
        sd = {"id": sid, "type": "spot"}
        if end_date:
            sd["end_date"] = end_date
        assert _iid(sd) == internal_id

    def test_raw_tardis_availability_is_read_too(self):
        sd = {"id": "BTC_ETH", "type": "spot", "availableTo": "2022-08-02T00:00:00.000Z"}
        assert _iid(sd) == "spot-ETH-BTC"

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("USDT_LINK", "spot-LINK-USDT"),
            ("USDD_SUN", "spot-SUN-USDD"),
            ("BTC_USDT", "spot-BTC-USDT"),
            ("LINK_USDT", "spot-LINK-USDT"),
        ],
    )
    def test_undated_rows_use_the_v1_quote_heuristic(self, sid, internal_id):
        assert _iid({"id": sid, "type": "spot"}) == internal_id

    @pytest.mark.parametrize("sid", ["BTCUSDT", "A_B_C", "_USDT", "BTC_"])
    def test_unparseable_ids_are_skipped(self, sid):
        with pytest.raises(SkipSymbol):
            parse_poloniex_api("poloniex-spot", {"id": sid, "type": "spot"})

    def test_every_tardis_fixture_row_parses(self):
        for row in TARDIS:
            parse_poloniex_api("poloniex-spot", _tardis_row(row))

    def test_v1_ids_map_onto_their_current_market(self):
        by_id = {row["id"]: _iid(_tardis_row(row)) for row in TARDIS}
        for old, new in [
            ("USDT_BTC", "BTC_USDT"),
            ("BTC_ETH", "ETH_BTC"),
            ("BTC_TRX", "TRX_BTC"),
            ("BNB_BTC", "BTC_BNB"),
            ("TRX_ETH", "ETH_TRX"),
            ("USDC_USDT", "USDT_USDC"),
            ("USDT_USDD", "USDD_USDT"),
            ("USDD_BTC", "BTC_USDD"),
            ("BTC_DASH", "DASH_BTC"),
            ("USDT_ADEL", "ADEL_USDT"),
            ("BUSD_BNB", "BNB_BUSD"),
            ("USDJ_BTT", "BTT_USDJ"),
        ]:
            assert by_id[old] == by_id[new], old

    def test_fallback_matches_api_rows(self, rows):
        tardis = {row["id"]: row for row in TARDIS}
        overlap = set(rows) & set(tardis)
        assert len(overlap) == len(rows) - 2
        for sid in overlap:
            assert _iid(_tardis_row(tardis[sid])) == _iid(rows[sid]), sid

    def test_api_legs_win_over_the_id(self):
        sd = {"id": "BTC_ETH", "type": "spot", "base_coin": "BTC", "quote_coin": "ETH",
              "end_date": "2022-08-02T00:00:00.000Z"}
        assert _iid(sd) == "spot-BTC-ETH"


class TestLegacyParserUnchanged:
    def test_quote_base(self):
        assert parse_poloniex("poloniex", {"id": "USDT_LINK", "type": "spot"}).internal_id == "spot-LINK-USDT"

    def test_base_quote(self):
        assert parse_poloniex("poloniex", {"id": "BTC_USDT", "type": "spot"}).internal_id == "spot-BTC-USDT"
