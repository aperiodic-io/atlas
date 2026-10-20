"""Bitstamp exchange-API fetchers (`bitstamp-spot`, `bitstamp-perps`).

Fixtures under `tests/fixtures/bitstamp/` are excerpts of the real
`/api/v2/markets/` and `/api/v2/currencies/` payloads captured on 2026-10-20
(every perpetual, a spread of spot markets incl. the one `Disabled` market) and
the Tardis `bitstamp` symbol ids of that day. Tardis's `bitstamp` feed carries
both spot (`btcusd`) and perpetuals (`btcusd-perp`).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.exchange_definitions.bitstamp import (
    fetch_bitstamp_perpetual,
    fetch_bitstamp_spot,
    parse_bitstamp,
    parse_bitstamp_api,
)
from atlas.exchange_definitions.common import SkipSymbol

FIXTURES = Path(__file__).parent / "fixtures" / "bitstamp"
SPOT = "bitstamp-spot"
PERPS = "bitstamp-perps"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _fetch(fetcher, markets=None, currencies=None) -> list[dict]:
    routes = {
        "/markets/": _load("markets.json") if markets is None else markets,
        "/currencies/": _load("currencies.json") if currencies is None else currencies,
    }

    def get(url, params=None, timeout=None, **kwargs):
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                return _response(payload)
        raise AssertionError(f"unexpected request {url}")

    with patch("atlas.exchange_definitions.http.requests.get", side_effect=get):
        return fetcher(timeout_seconds=5)


@pytest.fixture(scope="module")
def spot() -> dict[str, dict]:
    return {sd["id"]: sd for sd in _fetch(fetch_bitstamp_spot)}


@pytest.fixture(scope="module")
def perps() -> dict[str, dict]:
    return {sd["id"]: sd for sd in _fetch(fetch_bitstamp_perpetual)}


def _iid(exchange: str, sd: dict) -> str:
    return parse_bitstamp_api(exchange, sd).internal_id


def _market(symbol: str) -> dict:
    return next(m for m in _load("markets.json") if m["market_symbol"] == symbol)


# ---------------------------------------------------------------------------
# Spot
# ---------------------------------------------------------------------------


class TestSpot:
    def test_universe_is_the_spot_markets(self, spot):
        markets = _load("markets.json")
        assert set(spot) == {m["market_symbol"] for m in markets if m["market_type"] == "SPOT"}
        assert len(spot) == 40
        assert not any(sid.endswith("-perp") for sid in spot)

    def test_rows(self, spot):
        for sd in spot.values():
            assert sd["type"] == "spot"
            assert not {"margin_asset", "contract_size", "quantity_unit", "end_date"} & set(sd)
            assert None not in sd.values()

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("btcusd", "spot-BTC-USD"),
            ("ethbtc", "spot-ETH-BTC"),
            ("btceurc", "spot-BTC-EURC"),
            ("btceurcv", "spot-BTC-EURCV"),
            ("eurceur", "spot-EURC-EUR"),
            ("eurcvusdc", "spot-EURCV-USDC"),
            ("rlusdusdt", "spot-RLUSD-USDT"),
            ("ethrlusd", "spot-ETH-RLUSD"),
            ("pyusdusd", "spot-PYUSD-USD"),
            ("xsgdusdt", "spot-XSGD-USDT"),
            ("susd", "spot-S-USD"),
            ("seur", "spot-S-EUR"),
            ("reusd", "spot-RE-USD"),
            ("eth2eth", "spot-ETH2-ETH"),
            ("eurusd", "spot-EUR-USD"),
        ],
    )
    def test_internal_ids(self, spot, sid, internal_id):
        assert _iid(SPOT, spot[sid]) == internal_id

    def test_disabled_market_stays_listed(self, spot):
        # `trading: Disabled` (ZK/EUR) is a halt; Bitstamp drops delisted
        # markets from /markets/, so the row must not be closed.
        assert _market("zkeur")["trading"] == "Disabled"
        assert "zkeur" in spot
        assert "end_date" not in spot["zkeur"]

    def test_underlying_from_currency_type(self, spot):
        assert spot["btcusd"]["underlying"] == "crypto"
        assert spot["paxgusd"]["underlying"] == "crypto"  # Bitstamp: type crypto
        assert spot["usdcusd"]["underlying"] == "crypto"
        for sid in ("eurusd", "gbpusd", "sgdusd"):
            assert spot[sid]["underlying"] == "forex", sid

    def test_base_missing_from_currencies_is_unknown(self):
        currencies = [c for c in _load("currencies.json") if c["currency"] != "ZK"]
        rows = {sd["id"]: sd for sd in _fetch(fetch_bitstamp_spot, currencies=currencies)}
        assert rows["zkeur"]["underlying"] == "unknown"

    def test_unknown_currency_type_is_unknown(self):
        currencies = [dict(c, type="token") if c["currency"] == "ZK" else c for c in _load("currencies.json")]
        rows = {sd["id"]: sd for sd in _fetch(fetch_bitstamp_spot, currencies=currencies)}
        assert rows["zkeur"]["underlying"] == "unknown"

    def test_underlying_counts(self, spot):
        assert Counter(sd["underlying"] for sd in spot.values()) == {"crypto": 37, "forex": 3}


# ---------------------------------------------------------------------------
# Perpetuals
# ---------------------------------------------------------------------------


class TestPerps:
    def test_universe_is_the_perpetual_markets(self, perps):
        assert len(perps) == 20
        assert all(sid.endswith("-perp") for sid in perps)

    def test_ids_are_tardis_ids(self, perps):
        tardis = _load("tardis_bitstamp_ids.json")
        assert set(perps) == {sid for sid in tardis["active"] if sid.endswith("-perp")}

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("btcusd-perp", "perpetual-BTC-USD:USD"),
            ("brentusd-perp", "perpetual-BRENT-USD:USD"),
            ("goldusd-perp", "perpetual-GOLD-USD:USD"),
            ("eurusd-perp", "perpetual-EUR-USD:USD"),
            ("qqqusd-perp", "perpetual-QQQ-USD:USD"),
            ("paxgusd-perp", "perpetual-PAXG-USD:USD"),
        ],
    )
    def test_internal_ids(self, perps, sid, internal_id):
        assert _iid(PERPS, perps[sid]) == internal_id

    def test_linear_usd_settled_contracts(self, perps):
        for sd in perps.values():
            assert sd["type"] == "perpetual"
            assert sd["margin_asset"] == "USD"
            assert sd["contract_size"] == 1.0
            assert sd["quantity_unit"] == "base"
            assert not {"delivery_date", "end_date"} & set(sd)

    def test_contract_size_from_the_venue(self):
        markets = [dict(m, contract_size="0.01") if m["market_symbol"] == "goldusd-perp" else m for m in _load("markets.json")]
        rows = {sd["id"]: sd for sd in _fetch(fetch_bitstamp_perpetual, markets=markets)}
        assert rows["goldusd-perp"]["contract_size"] == 0.01

    def test_inverse_payoff_is_rejected(self):
        markets = [dict(m, payoff_type="Inverse") if m["market_symbol"] == "btcusd-perp" else m for m in _load("markets.json")]
        with pytest.raises(ValueError):
            _fetch(fetch_bitstamp_perpetual, markets=markets)

    @pytest.mark.parametrize(
        ("sid", "underlying"),
        [
            ("btcusd-perp", "crypto"),
            ("paxgusd-perp", "crypto"),  # Bitstamp: asset_class CRYPTO
            ("goldusd-perp", "commodity"),
            ("silverusd-perp", "commodity"),
            ("wtiusd-perp", "commodity"),
            ("brentusd-perp", "commodity"),
            ("qqqusd-perp", "equity"),  # ETFs are equity
            ("ewyusd-perp", "equity"),
            ("eurusd-perp", "forex"),
        ],
    )
    def test_underlying_from_asset_class(self, perps, sid, underlying):
        assert perps[sid]["underlying"] == underlying

    def test_unknown_asset_class(self):
        markets = [dict(m, asset_class="BONDS") if m["market_symbol"] == "btcusd-perp" else m for m in _load("markets.json")]
        rows = {sd["id"]: sd for sd in _fetch(fetch_bitstamp_perpetual, markets=markets)}
        assert rows["btcusd-perp"]["underlying"] == "unknown"

    def test_underlying_counts(self, perps):
        assert Counter(sd["underlying"] for sd in perps.values()) == {
            "crypto": 13,
            "commodity": 4,
            "equity": 2,
            "forex": 1,
        }

    def test_perps_do_not_request_currencies(self):
        rows = _fetch(fetch_bitstamp_perpetual, currencies={"error": "must not be read"})
        assert len(rows) == 20


class TestMalformed:
    @pytest.mark.parametrize("payload", [{"message": "Not found."}, [], "x", [1, 2]])
    def test_bad_markets_raise(self, payload):
        with pytest.raises(ValueError):
            _fetch(fetch_bitstamp_spot, markets=payload)
        with pytest.raises(ValueError):
            _fetch(fetch_bitstamp_perpetual, markets=payload)

    def test_bad_currencies_raise(self):
        with pytest.raises(ValueError):
            _fetch(fetch_bitstamp_spot, currencies={"message": "x"})

    def test_unknown_market_type_is_ignored(self):
        markets = [*_load("markets.json"), dict(_market("btcusd"), market_symbol="btcusd-opt", market_type="OPTION")]
        spot = {sd["id"] for sd in _fetch(fetch_bitstamp_spot, markets=markets)}
        perps = {sd["id"] for sd in _fetch(fetch_bitstamp_perpetual, markets=markets)}
        assert "btcusd-opt" not in spot | perps


# ---------------------------------------------------------------------------
# Tardis fallback
# ---------------------------------------------------------------------------


def _tardis_rows() -> list[dict]:
    tardis = _load("tardis_bitstamp_ids.json")
    return [
        {"id": sid, "type": "perpetual" if sid.endswith("-perp") else "spot"}
        for sid in tardis["active"] + tardis["delisted"]
    ]


class TestTardisFallback:
    def test_every_tardis_id_parses(self):
        rows = _tardis_rows()
        assert len(rows) == 436
        for sd in rows:
            exchange = PERPS if sd["type"] == "perpetual" else SPOT
            parse_bitstamp_api(exchange, sd)

    @pytest.mark.parametrize("fixture", ["spot", "perps"])
    def test_fallback_equals_api_rows(self, request, fixture):
        rows = request.getfixturevalue(fixture)
        exchange = SPOT if fixture == "spot" else PERPS
        mismatches = [
            sid
            for sid, sd in rows.items()
            if _iid(exchange, sd) != _iid(exchange, {"id": sid, "type": sd["type"]})
        ]
        assert mismatches == []

    def test_fallback_perpetual_shape(self):
        contract = parse_bitstamp_api(PERPS, {"id": "brentusd-perp", "type": "perpetual"})
        assert contract.internal_id == "perpetual-BRENT-USD:USD"
        assert (contract.contract_size, contract.quantity_unit) == (1.0, "base")

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("btcpax", "spot-BTC-PAX"),
            ("usdcusdt", "spot-USDC-USDT"),
            ("xrpusdt", "spot-XRP-USDT"),
            ("gbpeur", "spot-GBP-EUR"),
        ],
    )
    def test_historical_spot_ids(self, sid, internal_id):
        assert parse_bitstamp_api(SPOT, {"id": sid, "type": "spot"}).internal_id == internal_id

    @pytest.mark.parametrize("sid", ["usd", "foo", "-perp", "btcxyz-perp"])
    def test_unsplittable_ids_skip(self, sid):
        kind = "perpetual" if sid.endswith("-perp") else "spot"
        with pytest.raises(SkipSymbol):
            parse_bitstamp_api(SPOT, {"id": sid, "type": kind})

    def test_legacy_parser_untouched(self):
        assert parse_bitstamp("bitstamp", {"id": "btcusd", "type": "spot"}).internal_id == "spot-BTC-USD"
