"""Bitfinex exchange-API fetchers (`bitfinex-spot`, `bitfinex-perps`).

Fixtures under `tests/fixtures/bitfinex/` are the real `conf` payloads captured
on 2026-10-20 (`pub:list:pair:exchange`, `pub:info:pair`,
`pub:list:pair:securities`, `pub:list:pair:futures`, `pub:info:pair:futures`)
and the history symbol ids of `bitfinex` / `bitfinex-derivatives` on that day.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.exchange_definitions.bitfinex import (
    fetch_bitfinex_perpetual,
    fetch_bitfinex_spot,
    parse_bitfinex_api,
    parse_bitfinex_perpetual_api,
)
from atlas.exchange_definitions.common import SkipSymbol

FIXTURES = Path(__file__).parent / "fixtures" / "bitfinex"
SPOT = "bitfinex-spot"
PERPS = "bitfinex-perps"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _routes(**overrides) -> dict[str, object]:
    routes = {
        "pub:list:pair:exchange": _load("pairs_exchange.json"),
        "pub:info:pair": _load("info_pair.json"),
        "pub:list:pair:securities": _load("pairs_securities.json"),
        "pub:list:pair:futures": _load("pairs_futures.json"),
        "pub:info:pair:futures": _load("info_futures.json"),
    }
    routes.update(overrides)
    return routes


def _fetch(fetcher, routes: dict[str, object] | None = None) -> list[dict]:
    routes = routes or _routes()

    def get(url, params=None, timeout=None):
        key = url.rsplit("/", 1)[-1]
        if key not in routes:
            raise AssertionError(f"unexpected request {url}")
        return _response(routes[key])

    with patch("atlas.exchange_definitions.http.requests.get", side_effect=get):
        return fetcher(timeout_seconds=5)


@pytest.fixture(scope="module")
def spot() -> dict[str, dict]:
    return {sd["id"]: sd for sd in _fetch(fetch_bitfinex_spot)}


@pytest.fixture(scope="module")
def perps() -> dict[str, dict]:
    return {sd["id"]: sd for sd in _fetch(fetch_bitfinex_perpetual)}


def _spot_id(sd: dict) -> str:
    return parse_bitfinex_api(SPOT, sd).internal_id


def _perp_id(sd: dict) -> str:
    return parse_bitfinex_perpetual_api(PERPS, sd).internal_id


# ---------------------------------------------------------------------------
# Spot
# ---------------------------------------------------------------------------


class TestSpotUniverse:
    def test_every_listed_pair_except_paper_pairs(self, spot):
        listed = _load("pairs_exchange.json")[0]
        paper = {pid for pid in listed if pid.startswith("TEST")}
        assert len(paper) == 14
        assert set(spot) == set(listed) - paper
        assert len(spot) == 180

    def test_ids_are_history_ids(self, spot):
        # History ids drop the `t` prefix of Bitfinex trading symbols.
        history = _load("history_bitfinex_ids.json")
        assert set(spot) == set(history["active"])
        assert not any(sid.startswith("t") and sid[1:] in spot for sid in spot)

    def test_rows_are_spot_without_derivative_fields(self, spot):
        for sd in spot.values():
            assert sd["type"] == "spot"
            assert not {"margin_asset", "contract_size", "quantity_unit", "delivery_date"} & set(sd)
            assert None not in sd.values()

    def test_no_end_date_since_bitfinex_reports_no_delisting(self, spot):
        assert not any("end_date" in sd for sd in spot.values())


class TestSpotLegs:
    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTCUSD", "spot-BTC-USD"),
            ("BTCUST", "spot-BTC-USDT"),  # UST is Bitfinex's code for Tether USDt
            ("ADABTC", "spot-ADA-BTC"),
            ("AAVE:USD", "spot-AAVE-USD"),
            ("AAVE:UST", "spot-AAVE-USDT"),
            ("ALGUSD", "spot-ALGO-USD"),  # Bitfinex currency aliases
            ("IOTUSD", "spot-IOTA-USD"),
            ("DSHUSD", "spot-DASH-USD"),
            ("UDCUSD", "spot-USDC-USD"),
            ("UDCUST", "spot-USDC-USDT"),
            ("USTUSD", "spot-USDT-USD"),
            ("OPXUSD", "spot-OP-USD"),
            ("WBTUSD", "spot-WBTC-USD"),  # WBT is wrapped bitcoin ...
            ("WHBT:USD", "spot-WBT-USD"),  # ... and WHBT the WhiteBIT token
            ("BCHN:USD", "spot-BCH-USD"),
            ("LIFIII:UST", "spot-LIF3-USDT"),
            ("ETH2X:ETH", "spot-ETH2-ETH"),
            ("BTC:XAUT", "spot-BTC-XAUT"),
            ("XAUT:BTC", "spot-XAUT-BTC"),
            ("EURUST", "spot-EUR-USDT"),
            ("TITAN1:GBP", "spot-TITAN1-GBP"),
            ("ALT2612:UST", "spot-ALT2612-USDT"),
            ("TONUSD", "spot-TON-USD"),  # not Bitfinex's legacy GRAM label
            ("MXNT:USD", "spot-MXNT-USD"),
        ],
    )
    def test_internal_ids(self, spot, sid, internal_id):
        assert _spot_id(spot[sid]) == internal_id

    def test_legs_recorded(self, spot):
        assert (spot["ALGUSD"]["base_coin"], spot["ALGUSD"]["quote_coin"]) == ("ALGO", "USD")
        assert (spot["AAVE:UST"]["base_coin"], spot["AAVE:UST"]["quote_coin"]) == ("AAVE", "USDT")


class TestSpotUnderlying:
    def test_securities_are_unknown(self, spot):
        securities = set(_load("pairs_securities.json")[0])
        assert len(securities) == 28
        for sid in securities:
            assert spot[sid]["underlying"] == "unknown", sid

    def test_fiat_base_is_forex(self, spot):
        assert spot["EURUST"]["underlying"] == "forex"
        assert spot["GBPUST"]["underlying"] == "forex"

    def test_crypto_and_tokenized_gold_follow_the_venue(self, spot):
        # Bitfinex lists XAUt as an ordinary currency, not a security.
        for sid in ("BTCUSD", "XAUT:USD", "USTUSD", "EURQ:EUR", "PEPE:UST"):
            assert spot[sid]["underlying"] == "crypto", sid

    def test_counts(self, spot):
        from collections import Counter

        assert Counter(sd["underlying"] for sd in spot.values()) == {
            "crypto": 150,
            "unknown": 28,
            "forex": 2,
        }


class TestSpotFirstCapture:
    def test_listing_time_from_info_pair(self, spot):
        # `pub:info:pair` leads each entry with the pair's creation time.
        assert spot["BTCUSD"]["first_capture"] == "2013-01-14T16:47:23.000Z"
        assert spot["AAVE:USD"]["first_capture"] == "2021-02-05T09:28:18.684Z"

    def test_missing_listing_time_is_omitted(self, spot):
        assert spot["CH100:USD"].get("first_capture") is None
        assert "first_capture" not in spot["CH100:USD"]

    def test_pair_absent_from_info_still_listed(self):
        info = [[entry for entry in _load("info_pair.json")[0] if entry[0] != "BTCUSD"]]
        rows = {sd["id"]: sd for sd in _fetch(fetch_bitfinex_spot, _routes(**{"pub:info:pair": info}))}
        assert "first_capture" not in rows["BTCUSD"]
        assert _spot_id(rows["BTCUSD"]) == "spot-BTC-USD"


class TestSpotMalformed:
    @pytest.mark.parametrize(
        "payload",
        [
            ["error", 10020, "symbol: invalid"],
            {"error": "ratelimit"},
            [],
            [None],
            "nope",
        ],
    )
    def test_bad_pair_list_raises(self, payload):
        with pytest.raises(ValueError):
            _fetch(fetch_bitfinex_spot, _routes(**{"pub:list:pair:exchange": payload}))

    def test_bad_info_raises(self):
        with pytest.raises(ValueError):
            _fetch(fetch_bitfinex_spot, _routes(**{"pub:info:pair": {"error": "x"}}))

    def test_bad_securities_raises(self):
        with pytest.raises(ValueError):
            _fetch(fetch_bitfinex_spot, _routes(**{"pub:list:pair:securities": ["error", 1, "x"]}))

    def test_empty_securities_list_is_allowed(self):
        # Bitfinex answers `[null]` for an empty conf list.
        rows = _fetch(fetch_bitfinex_spot, _routes(**{"pub:list:pair:securities": [None]}))
        assert {sd["id"]: sd["underlying"] for sd in rows}["CH100:USD"] == "crypto"


# ---------------------------------------------------------------------------
# Spot history fallback
# ---------------------------------------------------------------------------


class TestSpotHistoryFallback:
    def test_every_history_id_parses(self):
        history = _load("history_bitfinex_ids.json")
        for sid in history["active"] + history["delisted"]:
            contract = parse_bitfinex_api(SPOT, {"id": sid, "type": "spot"})
            assert contract.symbol, sid
            assert contract.denominator, sid
            assert contract.margin is None

    def test_fallback_equals_api_rows(self, spot):
        mismatches = {
            sid: (_spot_id(sd), parse_bitfinex_api(SPOT, {"id": sid, "type": "spot"}).internal_id)
            for sid, sd in spot.items()
            if _spot_id(sd) != parse_bitfinex_api(SPOT, {"id": sid, "type": "spot"}).internal_id
        }
        assert mismatches == {}

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTCEUT", "spot-BTC-EURT"),
            ("MNAUSD", "spot-MANA-USD"),
            ("QTMBTC", "spot-QTUM-BTC"),
            ("STJUSD", "spot-STORJ-USD"),
            ("DATBTC", "spot-DATA-BTC"),
            ("TSDUSD", "spot-TUSD-USD"),
            ("BCHABC:USD", "spot-XEC-USD"),
            ("TERRAUST:USD", "spot-USTC-USD"),
            ("LUNA:USD", "spot-LUNC-USD"),
            ("LUNA2:UST", "spot-LUNA-USDT"),
            ("UST:CNHT", "spot-USDT-CNHT"),
            ("WLOXLM", "spot-WLO-XLM"),
            ("1INCH:USD", "spot-1INCH-USD"),
            ("ALT11M250830:UST", "spot-ALT11M250830-USDT"),
        ],
    )
    def test_delisted_ids(self, sid, internal_id):
        assert parse_bitfinex_api(SPOT, {"id": sid, "type": "spot"}).internal_id == internal_id

    @pytest.mark.parametrize("sid", ["TESTBTC:TESTUSD", "BTCUSDX", "BTC:", ":USD", "BTCUS"])
    def test_unparseable_or_paper_ids_skip(self, sid):
        with pytest.raises(SkipSymbol):
            parse_bitfinex_api(SPOT, {"id": sid, "type": "spot"})


# ---------------------------------------------------------------------------
# Perpetuals
# ---------------------------------------------------------------------------


class TestPerpUniverse:
    def test_every_listed_perp_except_paper_ones(self, perps):
        listed = _load("pairs_futures.json")[0]
        assert set(perps) == {pid for pid in listed if not pid.startswith("TEST")}
        assert len(perps) == 80

    def test_ids_are_history_ids(self, perps):
        assert set(perps) == set(_load("history_bitfinex_derivatives_ids.json")["active"])

    def test_rows_are_linear_perpetuals(self, perps):
        for sd in perps.values():
            assert sd["type"] == "perpetual"
            assert sd["contract_size"] == 1.0
            assert sd["quantity_unit"] == "base"
            assert sd["margin_asset"] == sd["quote_coin"]
            assert "delivery_date" not in sd
            assert "end_date" not in sd
            assert None not in sd.values()


class TestPerpLegs:
    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTCF0:USTF0", "perpetual-BTC-USDT:USDT"),
            # BTCF0 is Bitfinex's BTC collateral currency: these settle in BTC.
            ("ETHF0:BTCF0", "perpetual-ETH-BTC:BTC"),
            ("XAUTF0:BTCF0", "perpetual-XAUT-BTC:BTC"),
            ("ALGF0:USTF0", "perpetual-ALGO-USDT:USDT"),
            ("ATOF0:USTF0", "perpetual-ATOM-USDT:USDT"),
            ("IOTF0:USTF0", "perpetual-IOTA-USDT:USDT"),
            ("AUSTRALIA200IXF0:USTF0", "perpetual-AUSTRALIA200IX-USDT:USDT"),
            ("UKOILF0:USTF0", "perpetual-UKOIL-USDT:USDT"),
            ("EURF0:USTF0", "perpetual-EUR-USDT:USDT"),
            ("TONF0:USTF0", "perpetual-TON-USDT:USDT"),
        ],
    )
    def test_internal_ids(self, perps, sid, internal_id):
        assert _perp_id(perps[sid]) == internal_id

    def test_settle_assets(self, perps):
        from collections import Counter

        assert Counter(sd["margin_asset"] for sd in perps.values()) == {"USDT": 76, "BTC": 4}


class TestPerpUnderlying:
    @pytest.mark.parametrize(
        ("sid", "underlying"),
        [
            ("BTCF0:USTF0", "crypto"),
            ("XAUTF0:USTF0", "crypto"),
            ("AUSTRALIA200IXF0:USTF0", "index"),
            ("JAPAN225IXF0:USTF0", "index"),
            ("UK100IXF0:USTF0", "index"),
            ("BVIVF0:USTF0", "index"),
            ("EVIVF0:USTF0", "index"),
            ("EURF0:USTF0", "forex"),
            ("GBPF0:USTF0", "forex"),
            ("XAGF0:USTF0", "commodity"),
            ("XPTF0:USTF0", "commodity"),
            ("XPDF0:USTF0", "commodity"),
            ("UKOILF0:USTF0", "commodity"),
        ],
    )
    def test_classes(self, perps, sid, underlying):
        assert perps[sid]["underlying"] == underlying

    def test_counts(self, perps):
        from collections import Counter

        assert Counter(sd["underlying"] for sd in perps.values()) == {
            "crypto": 64,
            "index": 10,
            "commodity": 4,
            "forex": 2,
        }


class TestPerpFirstCapture:
    def test_listing_time_from_info_futures(self, perps):
        assert perps["AAVEF0:USTF0"]["first_capture"] == "2021-10-04T12:22:58.515Z"
        assert perps["BTCF0:USTF0"]["first_capture"] == "2019-07-03T14:35:42.332Z"


class TestPerpMalformed:
    @pytest.mark.parametrize("payload", [["error", 10020, "x"], {"a": 1}, [], [None]])
    def test_bad_pair_list_raises(self, payload):
        with pytest.raises(ValueError):
            _fetch(fetch_bitfinex_perpetual, _routes(**{"pub:list:pair:futures": payload}))

    def test_bad_info_raises(self):
        with pytest.raises(ValueError):
            _fetch(fetch_bitfinex_perpetual, _routes(**{"pub:info:pair:futures": [[["BTCF0:USTF0"]], 5]}))

    def test_pair_without_f0_suffix_raises(self):
        with pytest.raises(SkipSymbol):
            _fetch(fetch_bitfinex_perpetual, _routes(**{"pub:list:pair:futures": [["BTC:UST"]]}))


class TestPerpHistoryFallback:
    def test_every_history_id_parses(self):
        history = _load("history_bitfinex_derivatives_ids.json")
        for sid in history["active"] + history["delisted"]:
            contract = parse_bitfinex_perpetual_api(PERPS, {"id": sid, "type": "perpetual"})
            assert contract.margin == contract.denominator, sid

    def test_fallback_equals_api_rows(self, perps):
        mismatches = [
            sid
            for sid, sd in perps.items()
            if _perp_id(sd) != parse_bitfinex_perpetual_api(PERPS, {"id": sid, "type": "perpetual"}).internal_id
        ]
        assert mismatches == []

    def test_fallback_carries_contract_shape(self):
        contract = parse_bitfinex_perpetual_api(PERPS, {"id": "BTCF0:USTF0", "type": "perpetual"})
        assert (contract.contract_size, contract.quantity_unit) == (1.0, "base")

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTCF0:EUTF0", "perpetual-BTC-EURT:EURT"),
            ("GERMANY30IXF0:USTF0", "perpetual-GERMANY30IX-USDT:USDT"),
            ("LUNAF0:BTCF0", "perpetual-LUNC-BTC:BTC"),
            ("AMPF0:USTF0", "perpetual-AMPL-USDT:USDT"),
        ],
    )
    def test_delisted_ids(self, sid, internal_id):
        assert parse_bitfinex_perpetual_api(PERPS, {"id": sid, "type": "perpetual"}).internal_id == internal_id

    @pytest.mark.parametrize("sid", ["TESTBTCF0:TESTUSDTF0", "BTCUSD", "BTCF0:UST", "F0:USTF0"])
    def test_paper_and_malformed_skip(self, sid):
        with pytest.raises(SkipSymbol):
            parse_bitfinex_perpetual_api(PERPS, {"id": sid, "type": "perpetual"})
