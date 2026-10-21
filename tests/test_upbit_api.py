"""Upbit exchange-API fetcher (upbit-spot) and its parser.

Fixtures are excerpts of real responses captured on 2026-10-20:
`markets.json` from `/v1/market/all?is_details=true` (including markets
under an investment warning and caution) and `history.json` the historical Upbit
symbol list for the same and some delisted markets.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.exchange_definitions.common import SkipSymbol
from atlas.exchange_definitions.upbit import fetch_upbit_spot, parse_upbit, parse_upbit_api

FIXTURES = Path(__file__).parent / "fixtures" / "upbit"
MARKETS = json.loads((FIXTURES / "markets.json").read_text(encoding="utf-8"))
HISTORY = json.loads((FIXTURES / "history.json").read_text(encoding="utf-8"))


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _fetch(payload=MARKETS) -> tuple[list[dict], MagicMock]:
    with patch(
        "atlas.exchange_definitions.http.requests.get", return_value=_response(payload)
    ) as get:
        return fetch_upbit_spot(timeout_seconds=5), get


@pytest.fixture(scope="module")
def rows() -> dict[str, dict]:
    fetched, _ = _fetch()
    return {sd["id"]: sd for sd in fetched}


def _iid(sd: dict) -> str:
    return parse_upbit_api("upbit-spot", sd).internal_id


class TestFetch:
    def test_requests_market_details(self):
        _, get = _fetch()
        url = get.call_args.args[0]
        assert url == "https://api.upbit.com/v1/market/all"
        assert get.call_args.kwargs["params"] == {"is_details": "true"}

    def test_universe_is_every_market(self, rows):
        assert set(rows) == {m["market"] for m in MARKETS}

    def test_ids_are_the_history_ids(self, rows):
        history_ids = {s["id"] for s in HISTORY}
        # KRW-MPLX was listed after the historical symbol list was taken.
        assert set(rows) - history_ids == {"KRW-MPLX"}

    def test_row_shape(self, rows):
        assert rows["KRW-BTC"] == {
            "id": "KRW-BTC",
            "type": "spot",
            "base_coin": "BTC",
            "quote_coin": "KRW",
            "underlying": "crypto",
        }

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("KRW-BTC", "spot-BTC-KRW"),
            ("BTC-ETH", "spot-ETH-BTC"),
            ("USDT-BTC", "spot-BTC-USDT"),
            ("BTC-0G", "spot-0G-BTC"),
            ("KRW-USDT", "spot-USDT-KRW"),
            ("USDT-BREV", "spot-BREV-USDT"),
        ],
    )
    def test_internal_ids(self, rows, sid, internal_id):
        assert _iid(rows[sid]) == internal_id

    def test_every_quote_market_is_covered(self, rows):
        assert {sd["quote_coin"] for sd in rows.values()} == {"KRW", "BTC", "USDT"}

    def test_warning_and_caution_markets_stay_listed(self, rows):
        flagged = [
            m["market"]
            for m in MARKETS
            if m["market_event"]["warning"] or any(m["market_event"]["caution"].values())
        ]
        assert len(flagged) >= 3
        for sid in flagged:
            assert "end_date" not in rows[sid]

    def test_no_listing_or_delisting_times(self, rows):
        assert not any({"first_capture", "end_date"} & set(sd) for sd in rows.values())

    def test_all_crypto(self, rows):
        assert {sd["underlying"] for sd in rows.values()} == {"crypto"}

    def test_spot_has_no_margin(self, rows):
        assert all(parse_upbit_api("upbit-spot", sd).margin is None for sd in rows.values())


class TestMalformedPayloads:
    @pytest.mark.parametrize(
        "payload",
        [
            {"error": {"name": "too_many_requests", "message": "slow down"}},
            None,
            "KRW-BTC",
            [{"korean_name": "x"}],
            [{"market": 1}],
        ],
    )
    def test_unexpected_shape_raises(self, payload):
        with pytest.raises(ValueError):
            _fetch(payload)

    @pytest.mark.parametrize("market", ["KRWBTC", "KRW-BTC-X", "-BTC", "KRW-"])
    def test_unexpected_market_name_raises(self, market):
        with pytest.raises(ValueError):
            _fetch([{"market": market}])


class TestHistoryFallback:
    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("KRW-BTC", "spot-BTC-KRW"),
            ("BTC-0G", "spot-0G-BTC"),
            ("BTC-BCHA", "spot-BCHA-BTC"),
            ("KRW-ICX", "spot-ICX-KRW"),
        ],
    )
    def test_quote_base_ids(self, sid, internal_id):
        assert _iid({"id": sid, "type": "spot"}) == internal_id

    @pytest.mark.parametrize("sid", ["KRWBTC", "KRW-BTC-X"])
    def test_unparseable_ids_are_skipped(self, sid):
        with pytest.raises(SkipSymbol):
            parse_upbit_api("upbit-spot", {"id": sid, "type": "spot"})

    def test_every_history_fixture_row_parses(self):
        for row in HISTORY:
            parse_upbit_api("upbit-spot", {"id": row["id"], "type": row["type"]})

    def test_fallback_matches_api_rows(self, rows):
        history_ids = {s["id"] for s in HISTORY}
        overlap = set(rows) & history_ids
        assert len(overlap) == len(rows) - 1
        for sid in overlap:
            assert _iid({"id": sid, "type": "spot"}) == _iid(rows[sid]), sid

    def test_legacy_parser_unchanged(self):
        assert parse_upbit("upbit", {"id": "BTC-ETH", "type": "spot"}).internal_id == "spot-ETH-BTC"
