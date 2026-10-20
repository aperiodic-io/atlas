"""Gemini exchange-API fetchers (gemini-spot, gemini-perps) and their parser.

Fixtures are excerpts of real responses captured on 2026-10-20:
`symbols.json` from `/v1/symbols`, `details.json` the matching
`/v1/symbols/details/<symbol>` responses keyed by symbol, and `tardis.json`
Tardis's Gemini symbol list for the same and some delisted markets.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import time

import pytest
import requests

from atlas.exchange_definitions import gemini
from atlas.exchange_definitions.common import SkipSymbol
from atlas.exchange_definitions.http import get_json
from atlas.exchange_definitions.gemini import (
    _RateLimiter,
    fetch_gemini_perpetual,
    fetch_gemini_spot,
    parse_gemini,
    parse_gemini_api,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gemini"
SYMBOLS = json.loads((FIXTURES / "symbols.json").read_text())
DETAILS = json.loads((FIXTURES / "details.json").read_text())
TARDIS = json.loads((FIXTURES / "tardis.json").read_text())
# Read before the autouse fixture below zeroes it for the fake API.
DEFAULT_INTERVAL = gemini._REQUEST_INTERVAL_SECONDS


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


class _Gemini:
    """Fake `requests.get` serving the fixture, recording requested URLs."""

    def __init__(self, symbols=SYMBOLS, details=DETAILS):
        self.symbols, self.details, self.urls = symbols, details, []

    def __call__(self, url, params=None, timeout=None):
        self.urls.append(url)
        if url == "https://api.gemini.com/v1/symbols":
            return _response(self.symbols)
        prefix = "https://api.gemini.com/v1/symbols/details/"
        if url.startswith(prefix):
            return _response(self.details[url[len(prefix):]])
        raise AssertionError(f"unexpected request {url}")


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(gemini, "_REQUEST_INTERVAL_SECONDS", 0)


def _fetch(fetcher, fake=None) -> list[dict]:
    fake = fake or _Gemini()
    with patch("atlas.exchange_definitions.http.requests.get", side_effect=fake):
        return fetcher(timeout_seconds=5)


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {sd["id"]: sd for sd in rows}


def _iid(sd: dict) -> str:
    return parse_gemini_api("gemini-spot", sd).internal_id


@pytest.fixture(scope="module")
def spot() -> dict[str, dict]:
    with patch.object(gemini, "_REQUEST_INTERVAL_SECONDS", 0):
        return _by_id(_fetch(fetch_gemini_spot))


@pytest.fixture(scope="module")
def perps() -> dict[str, dict]:
    with patch.object(gemini, "_REQUEST_INTERVAL_SECONDS", 0):
        return _by_id(_fetch(fetch_gemini_perpetual))


# ---------------------------------------------------------------------------
# Spot fetcher
# ---------------------------------------------------------------------------


class TestSpotFetch:
    def test_universe_is_every_spot_market(self, spot):
        spot_symbols = {s.upper() for s in SYMBOLS if not s.endswith("perp")}
        assert set(spot) == spot_symbols

    def test_ids_are_the_uppercase_tardis_ids(self, spot):
        tardis_ids = {s["id"] for s in TARDIS}
        assert set(spot) <= tardis_ids
        assert all(sid == sid.upper() for sid in spot)

    def test_rows_are_spot_with_venue_legs(self, spot):
        assert spot["BTCUSD"] == {
            "id": "BTCUSD",
            "type": "spot",
            "base_coin": "BTC",
            "quote_coin": "USD",
            "underlying": "crypto",
        }

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTCUSD", "spot-BTC-USD"),
            ("BTCGUSD", "spot-BTC-GUSD"),
            ("BTCRLUSD", "spot-BTC-RLUSD"),
            ("BTCUSDC", "spot-BTC-USDC"),
            ("2ZRLUSD", "spot-2Z-RLUSD"),
            ("2ZUSD", "spot-2Z-USD"),
            ("USDGUSD", "spot-USDG-USD"),
            ("USDGGUSD", "spot-USDG-GUSD"),
            ("RLUSDGUSD", "spot-RLUSD-GUSD"),
            ("PAXGUSD", "spot-PAXG-USD"),
            ("JITOSOLSOL", "spot-JITOSOL-SOL"),
            ("SOLETH", "spot-SOL-ETH"),
            ("LINKBTC", "spot-LINK-BTC"),
            ("ETHUSDT", "spot-ETH-USDT"),
            ("BTCEUR", "spot-BTC-EUR"),
            ("ETHSGD", "spot-ETH-SGD"),
            ("GUSDGBP", "spot-GUSD-GBP"),
            ("EFILFIL", "spot-EFIL-FIL"),
        ],
    )
    def test_internal_ids(self, spot, sid, internal_id):
        assert _iid(spot[sid]) == internal_id

    def test_no_margin_or_contract_fields_on_spot(self, spot):
        for sd in spot.values():
            assert not {"margin_asset", "contract_size", "quantity_unit"} & set(sd)
            assert parse_gemini_api("gemini-spot", sd).margin is None

    def test_no_listing_time_is_published(self, spot):
        assert not any("first_capture" in sd for sd in spot.values())

    def test_closed_markets_end(self, spot):
        closed = {sid for sid, sd in spot.items() if "end_date" in sd}
        assert closed == {"GUSDUSD", "EFILFIL", "GRAMSGD"}
        assert spot["GRAMSGD"]["end_date"].endswith("Z")

    def test_limit_only_markets_stay_listed(self, spot):
        assert DETAILS["linkbtc"]["status"] == "limit_only"
        assert "end_date" not in spot["LINKBTC"]
        assert "end_date" not in spot["LINKETH"]

    @pytest.mark.parametrize("status", ["cancel_only", "post_only", "limit_only", "open"])
    def test_restricted_statuses_stay_listed(self, status):
        details = {**DETAILS, "btcusd": {**DETAILS["btcusd"], "status": status}}
        rows = _by_id(_fetch(fetch_gemini_spot, _Gemini(details=details)))
        assert "end_date" not in rows["BTCUSD"]

    def test_fiat_bases_are_forex(self, spot):
        for sid in ("AUDUSD", "AUDGUSD", "EURUSD", "EURUSDC"):
            assert spot[sid]["underlying"] == "forex"

    def test_everything_else_is_crypto(self, spot):
        for sid in ("BTCUSD", "GUSDGBP", "USDTUSD", "PAXGUSD", "XAUTUSD", "BTCEUR"):
            assert spot[sid]["underlying"] == "crypto"

    def test_requests_one_details_call_per_spot_market_and_none_for_perps(self):
        fake = _Gemini()
        _fetch(fetch_gemini_spot, fake)
        detail_urls = [u for u in fake.urls if "/details/" in u]
        assert len(detail_urls) == len([s for s in SYMBOLS if not s.endswith("perp")])
        assert not any(u.endswith("perp") for u in detail_urls)

    def test_other_product_types_are_left_out(self):
        details = {**DETAILS, "btcusd": {**DETAILS["btcusd"], "product_type": "future"}}
        rows = _by_id(_fetch(fetch_gemini_spot, _Gemini(details=details)))
        assert "BTCUSD" not in rows
        assert "ETHUSDT" in rows


# ---------------------------------------------------------------------------
# Perpetual fetcher
# ---------------------------------------------------------------------------


class TestPerpetualFetch:
    def test_universe_is_every_perp(self, perps):
        assert set(perps) == {s.upper() for s in SYMBOLS if s.endswith("perp")}

    def test_gusd_perp(self, perps):
        assert perps["BTCGUSDPERP"] == {
            "id": "BTCGUSDPERP",
            "type": "perpetual",
            "base_coin": "BTC",
            "quote_coin": "GUSD",
            "margin_asset": "GUSD",
            "contract_size": 1.0,
            "quantity_unit": "base",
            "underlying": "crypto",
        }

    def test_usdc_perp_settles_in_usdc_despite_gusd_price_currency(self, perps):
        assert DETAILS["btcusdcperp"]["contract_price_currency"] == "GUSD"
        contract = parse_gemini_api("gemini-perps", perps["BTCUSDCPERP"])
        assert contract.internal_id == "perpetual-BTC-USDC:USDC"
        assert contract.margin == "USDC"
        assert contract.contract_size == 1.0
        assert contract.quantity_unit == "base"

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("ETHGUSDPERP", "perpetual-ETH-GUSD:GUSD"),
            ("HYPEUSDCPERP", "perpetual-HYPE-USDC:USDC"),
            ("TRUMPGUSDPERP", "perpetual-TRUMP-GUSD:GUSD"),
            ("XRPUSDCPERP", "perpetual-XRP-USDC:USDC"),
        ],
    )
    def test_internal_ids(self, perps, sid, internal_id):
        assert parse_gemini_api("gemini-perps", perps[sid]).internal_id == internal_id

    def test_open_perps_are_listed(self, perps):
        assert not any("end_date" in sd for sd in perps.values())

    def test_closed_perp_ends(self):
        details = {**DETAILS, "ethgusdperp": {**DETAILS["ethgusdperp"], "status": "closed"}}
        rows = _by_id(_fetch(fetch_gemini_perpetual, _Gemini(details=details)))
        assert "end_date" in rows["ETHGUSDPERP"]

    def test_requests_details_only_for_perps(self):
        fake = _Gemini()
        _fetch(fetch_gemini_perpetual, fake)
        detail_urls = [u for u in fake.urls if "/details/" in u]
        assert sorted(detail_urls) == sorted(
            f"https://api.gemini.com/v1/symbols/details/{s}" for s in SYMBOLS if s.endswith("perp")
        )


# ---------------------------------------------------------------------------
# Malformed payloads
# ---------------------------------------------------------------------------


class TestMalformedPayloads:
    @pytest.mark.parametrize(
        "payload",
        [
            {"result": "error", "reason": "Maintenance", "message": "down"},
            None,
            [1, 2],
            "btcusd",
        ],
    )
    def test_bad_symbol_list_raises(self, payload):
        with pytest.raises(ValueError):
            _fetch(fetch_gemini_spot, _Gemini(symbols=payload))

    @pytest.mark.parametrize(
        "payload",
        [
            {"result": "error", "reason": "InvalidSymbol", "message": "Received unsupported symbol"},
            [],
            {"symbol": "BTCUSD", "status": "open"},
        ],
    )
    def test_bad_details_raise(self, payload):
        details = {**DETAILS, "btcusd": payload}
        with pytest.raises(ValueError):
            _fetch(fetch_gemini_spot, _Gemini(details=details))

    def test_http_error_on_details_propagates(self):
        error = requests.HTTPError("400 Bad Request")

        def get(url, params=None, timeout=None):
            if url.endswith("/details/btcusd"):
                resp = MagicMock()
                resp.raise_for_status.side_effect = error
                return resp
            return _Gemini()(url)

        with (
            patch("atlas.exchange_definitions.http.requests.get", side_effect=get),
            patch.object(get_json.retry, "sleep", lambda _: None),
            pytest.raises(requests.HTTPError),
        ):
            fetch_gemini_spot(timeout_seconds=5)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_spaces_request_starts(self, monkeypatch):
        clock = {"now": 100.0}
        sleeps: list[float] = []
        monkeypatch.setattr(gemini.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(gemini.time, "sleep", sleeps.append)
        limiter = _RateLimiter()
        for _ in range(3):
            limiter.wait(0.5)
        assert sleeps == [0.5, 1.0]

    def test_no_wait_once_the_interval_has_passed(self, monkeypatch):
        clock = {"now": 100.0}
        sleeps: list[float] = []
        monkeypatch.setattr(gemini.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(gemini.time, "sleep", sleeps.append)
        limiter = _RateLimiter()
        limiter.wait(0.5)
        clock["now"] = 101.0
        limiter.wait(0.5)
        assert sleeps == []

    def test_default_interval_leaves_headroom_under_the_public_limit(self):
        # Gemini allows 120 public requests a minute; retries are not spaced.
        assert DEFAULT_INTERVAL > 0
        assert 60 / DEFAULT_INTERVAL <= 100
        assert gemini._MAX_WORKERS >= 1

    def test_every_request_of_both_slugs_shares_one_limiter(self, monkeypatch):
        """Spot and perps run back to back; one budget must cover both."""
        waits: list[float] = []
        monkeypatch.setattr(gemini._LIMITER, "wait", waits.append)
        fake = _Gemini()
        with patch("atlas.exchange_definitions.http.requests.get", side_effect=fake):
            fetch_gemini_spot(timeout_seconds=5)
            fetch_gemini_perpetual(timeout_seconds=5)
        # `/v1/symbols` included.
        assert len(waits) == len(fake.urls)
        assert fake.urls.count("https://api.gemini.com/v1/symbols") == 2

    def test_a_failed_request_cancels_the_queued_ones(self):
        """A failure surfaces without first running every remaining request."""
        first = SYMBOLS[0]
        details = {**DETAILS, first: {"result": "error", "reason": "InvalidSymbol"}}
        fake = _Gemini(details=details)

        def slow(url, **_kwargs):
            if first not in url:
                time.sleep(0.02)
            return fake(url)

        spot_symbols = [s for s in SYMBOLS if not s.endswith("perp")]
        assert spot_symbols[0] == first
        assert len(spot_symbols) > 2 * gemini._MAX_WORKERS
        with patch("atlas.exchange_definitions.http.requests.get", side_effect=slow), pytest.raises(ValueError):
            fetch_gemini_spot(timeout_seconds=5)
        detail_requests = [u for u in fake.urls if "/details/" in u]
        assert len(detail_requests) < len(spot_symbols)


# ---------------------------------------------------------------------------
# Tardis-id fallback
# ---------------------------------------------------------------------------


def _tardis(sid: str, kind: str = "spot") -> dict:
    return {"id": sid, "type": kind}


class TestTardisFallback:
    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTCUSD", "spot-BTC-USD"),
            ("BTCGUSD", "spot-BTC-GUSD"),
            ("BTCRLUSD", "spot-BTC-RLUSD"),
            ("2ZRLUSD", "spot-2Z-RLUSD"),
            ("USDGUSD", "spot-USDG-USD"),
            ("USDGGUSD", "spot-USDG-GUSD"),
            ("GUSDUSD", "spot-GUSD-USD"),
            ("RLUSDUSD", "spot-RLUSD-USD"),
            ("USDCUSD", "spot-USDC-USD"),
            ("PAXGUSD", "spot-PAXG-USD"),
            ("PAXGGUSD", "spot-PAXG-GUSD"),
            ("MOODENGUSD", "spot-MOODENG-USD"),
            ("MOODENGGUSD", "spot-MOODENG-GUSD"),
            ("WCFGUSD", "spot-WCFG-USD"),
            ("LUSDUSD", "spot-LUSD-USD"),
            ("BUSDUSD", "spot-BUSD-USD"),
            ("BTCDAI", "spot-BTC-DAI"),
            ("DAIBTC", "spot-DAI-BTC"),
            ("LTCBCH", "spot-LTC-BCH"),
            ("ZECLTC", "spot-ZEC-LTC"),
            ("EFILFIL", "spot-EFIL-FIL"),
            ("JITOSOLSOL", "spot-JITOSOL-SOL"),
            ("ETHSGD", "spot-ETH-SGD"),
            ("GUSDGBP", "spot-GUSD-GBP"),
            ("BTCEUR", "spot-BTC-EUR"),
            ("1INCHUSD", "spot-1INCH-USD"),
        ],
    )
    def test_spot_ids(self, sid, internal_id):
        assert _iid(_tardis(sid)) == internal_id

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTCGUSDPERP", "perpetual-BTC-GUSD:GUSD"),
            ("BTCUSDCPERP", "perpetual-BTC-USDC:USDC"),
            ("KSL2GUSDPERP", "perpetual-KSL2-GUSD:GUSD"),
            ("KT5USDCPERP", "perpetual-KT5-USDC:USDC"),
            ("MATICGUSDPERP", "perpetual-MATIC-GUSD:GUSD"),
        ],
    )
    def test_perp_ids(self, sid, internal_id):
        assert parse_gemini_api("gemini-perps", _tardis(sid, "perpetual")).internal_id == internal_id

    def test_lowercase_ids_parse_too(self):
        assert _iid(_tardis("btcgusd")) == "spot-BTC-GUSD"

    @pytest.mark.parametrize(
        "sd",
        [
            _tardis("BTCPERP", "perpetual"),
            _tardis("BTCUSDTPERP", "perpetual"),
            _tardis("BTCUSD", "perpetual"),
            _tardis("XYZ"),
            _tardis("USD"),
            _tardis("BTCUSD", "option"),
        ],
    )
    def test_unparseable_ids_are_skipped(self, sd):
        with pytest.raises(SkipSymbol):
            parse_gemini_api("gemini-spot", sd)

    def test_every_tardis_fixture_row_parses(self):
        for row in TARDIS:
            parse_gemini_api("gemini", {"id": row["id"], "type": row["type"]})

    def test_fallback_matches_api_rows(self, spot, perps):
        rows = {**spot, **perps}
        tardis = {row["id"]: row for row in TARDIS}
        overlap = set(rows) & set(tardis)
        assert len(overlap) == len(rows)
        for sid in overlap:
            api = parse_gemini_api("gemini-spot", rows[sid])
            fallback = parse_gemini_api("gemini-spot", _tardis(sid, tardis[sid]["type"]))
            assert fallback.internal_id == api.internal_id, sid

    def test_api_legs_win_over_the_id(self):
        sd = {"id": "PAXGUSD", "type": "spot", "base_coin": "PAX", "quote_coin": "GUSD"}
        assert _iid(sd) == "spot-PAX-GUSD"


class TestLegacyParserUnchanged:
    def test_legacy_parser_still_splits_lowercase_ids(self):
        contract = parse_gemini("gemini", {"id": "btcgusd", "type": "spot"})
        assert contract.internal_id == "spot-BTC-GUSD"
