"""Kraken exchange-API fetchers: `kraken-spot`, `kraken-perps`, `kraken-futures`.

Fixtures under `tests/fixtures/kraken/` are excerpts of real responses
captured on 2026-10-20: Kraken spot `AssetPairs` (crypto/fiat and
`aclass_base=tokenized_asset`), Kraken Futures `/derivatives/api/v3/instruments`
(margin tables dropped), and the Tardis symbol lists of `kraken` and
`cryptofacilities`. Each fetcher must reproduce the Tardis symbol id of an
instrument byte for byte, so Tardis history and API rows merge, and a Tardis-only
row must parse to the same internal_id as the API row of its instrument.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.exchange_definitions.common import SkipSymbol
from atlas.exchange_definitions.cryptofacilities import (
    fetch_kraken_futures,
    fetch_kraken_perpetual,
    parse_kraken_derivative,
)
from atlas.exchange_definitions.kraken import (
    fetch_kraken_spot,
    kraken_v2_symbol,
    parse_kraken_spot,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kraken"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


PAIRS = _load("asset_pairs.json")
TOKENIZED = _load("asset_pairs_tokenized.json")
INSTRUMENTS = _load("futures_instruments.json")
TARDIS_SPOT = _load("tardis_kraken.json")
TARDIS_DERIV = _load("tardis_cryptofacilities.json")


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _spot_get(pairs: object = PAIRS, tokenized: object = TOKENIZED):
    calls: list[dict | None] = []

    def get(url, params=None, timeout=None, **kwargs):
        assert url == "https://api.kraken.com/0/public/AssetPairs", url
        calls.append(params)
        if params and params.get("aclass_base") == "tokenized_asset":
            return _response(tokenized)
        return _response(pairs)

    get.calls = calls
    return get


def _fetch_spot(pairs: object = PAIRS, tokenized: object = TOKENIZED) -> list[dict]:
    with patch("atlas.exchange_definitions.http.requests.get", side_effect=_spot_get(pairs, tokenized)):
        return fetch_kraken_spot(timeout_seconds=5)


def _fetch_deriv(fetcher, payload: object = INSTRUMENTS) -> list[dict]:
    def get(url, params=None, timeout=None, **kwargs):
        assert url == "https://futures.kraken.com/derivatives/api/v3/instruments", url
        return _response(payload)

    with patch("atlas.exchange_definitions.http.requests.get", side_effect=get):
        return fetcher(timeout_seconds=5)


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {sd["id"]: sd for sd in rows}


def _spot_iid(sd: dict) -> str:
    return parse_kraken_spot("kraken-spot", sd).internal_id


def _deriv_iid(sd: dict, exchange: str = "kraken-perps") -> str:
    return parse_kraken_derivative(exchange, sd).internal_id


def _with_pair(key: str, **changes) -> dict:
    payload = copy.deepcopy(PAIRS)
    payload["result"][key].update(changes)
    return payload


def _with_instrument(symbol: str, **changes) -> dict:
    payload = copy.deepcopy(INSTRUMENTS)
    for item in payload["instruments"]:
        if item["symbol"] == symbol:
            item.update(changes)
    return payload


# ---------------------------------------------------------------------------
# kraken-spot: ids
# ---------------------------------------------------------------------------


class TestKrakenV2Symbol:
    """Tardis records Kraken spot from WebSocket v2 since 2026-07-10.

    v2 names BTC and DOGE where REST `wsname` (the v1 name) says XBT and XDG,
    and lists each xStock under both its `x` name and its `SPV` pair key.
    """

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("XXBTZUSD", "BTC/USD"),
            ("XETHXXBT", "ETH/BTC"),
            ("XDGUSD", "DOGE/USD"),
            ("XXDGXXBT", "DOGE/BTC"),
            ("XBTSOFID", "BTC/SOFID"),
            ("ZEURZUSD", "EUR/USD"),
            ("ZCHFCHF", "ZCHF/CHF"),
            ("0GEUR", "0G/EUR"),
        ],
    )
    def test_currency_pairs(self, key, expected):
        assert kraken_v2_symbol(key, PAIRS["result"][key]) == expected

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("AAPLxUSD", "AAPLx/USD"),
            ("AAPLSPVUSD", "AAPLSPV/USD"),
            ("BRK.BxUSD", "BRK.Bx/USD"),
            ("BRK.BSPVUSD", "BRK.BSPV/USD"),
        ],
    )
    def test_tokenized_pairs_keep_both_names(self, key, expected):
        assert kraken_v2_symbol(key, TOKENIZED["result"][key]) == expected


class TestKrakenSpotFetch:
    def test_ids_are_the_tardis_websocket_v2_symbols(self):
        rows = _fetch_spot()
        ids = {sd["id"] for sd in rows}
        assert ids == {
            "BTC/USD", "ETH/BTC", "DOGE/USD", "DOGE/BTC", "EUR/USD", "USD/CHF", "ZCHF/CHF",
            "USDT/USD", "0G/EUR", "BTC/SOFID", "AUSD/EUR", "BTC/USD1", "PAXG/USD", "ETH/EUR",
            "ACA/EUR", "AAPLx/USD", "AAPLSPV/USD", "BRK.Bx/USD", "BRK.BSPV/USD", "WBDx/USD",
            "WBDSPV/USD", "GLDx/USD", "GLDSPV/USD",
        }
        assert len(rows) == len(ids)

    def test_ids_present_in_tardis_are_identical(self):
        tardis_ids = {row["id"] for row in TARDIS_SPOT}
        api_ids = {sd["id"] for sd in _fetch_spot()}
        # AUSD/EUR (post-only, newly listed) has not reached Tardis yet.
        assert api_ids - tardis_ids == {"AUSD/EUR"}

    def test_requests_currency_and_tokenized_universes(self):
        get = _spot_get()
        with patch("atlas.exchange_definitions.http.requests.get", side_effect=get):
            fetch_kraken_spot(timeout_seconds=5)
        assert sorted(str(p) for p in get.calls) == sorted(
            [str(None), str({"aclass_base": "tokenized_asset"})]
        )

    def test_legs_are_normalized_tickers(self):
        rows = _by_id(_fetch_spot())
        assert (rows["BTC/USD"]["base_coin"], rows["BTC/USD"]["quote_coin"]) == ("BTC", "USD")
        assert (rows["ETH/BTC"]["base_coin"], rows["ETH/BTC"]["quote_coin"]) == ("ETH", "BTC")
        assert rows["DOGE/BTC"]["base_coin"] == "DOGE"
        # Frankencoin is ZCHF, not the Swiss franc: no X/Z prefix stripping.
        assert (rows["ZCHF/CHF"]["base_coin"], rows["ZCHF/CHF"]["quote_coin"]) == ("ZCHF", "CHF")
        # Both names of an xStock carry its token, `AAPLx`.
        assert rows["AAPLSPV/USD"]["base_coin"] == "AAPLx"
        assert rows["BRK.BSPV/USD"]["base_coin"] == "BRK.Bx"
        assert all(sd["type"] == "spot" for sd in rows.values())

    def test_internal_ids(self):
        rows = _by_id(_fetch_spot())
        assert {i: _spot_iid(sd) for i, sd in rows.items()} == {
            "BTC/USD": "spot-BTC-USD",
            "ETH/BTC": "spot-ETH-BTC",
            "DOGE/USD": "spot-DOGE-USD",
            "DOGE/BTC": "spot-DOGE-BTC",
            "EUR/USD": "spot-EUR-USD",
            "USD/CHF": "spot-USD-CHF",
            "ZCHF/CHF": "spot-ZCHF-CHF",
            "USDT/USD": "spot-USDT-USD",
            "0G/EUR": "spot-0G-EUR",
            "BTC/SOFID": "spot-BTC-SOFID",
            "AUSD/EUR": "spot-AUSD-EUR",
            "BTC/USD1": "spot-BTC-USD1",
            "PAXG/USD": "spot-PAXG-USD",
            "ETH/EUR": "spot-ETH-EUR",
            "ACA/EUR": "spot-ACA-EUR",
            "AAPLx/USD": "spot-AAPLX-USD",
            "AAPLSPV/USD": "spot-AAPLX-USD",
            "BRK.Bx/USD": "spot-BRK.BX-USD",
            "BRK.BSPV/USD": "spot-BRK.BX-USD",
            "WBDx/USD": "spot-WBDX-USD",
            "WBDSPV/USD": "spot-WBDX-USD",
            "GLDx/USD": "spot-GLDX-USD",
            "GLDSPV/USD": "spot-GLDX-USD",
        }

    def test_spot_rows_carry_no_margin_or_derivative_fields(self):
        for sd in _fetch_spot():
            contract = parse_kraken_spot("kraken-spot", sd)
            assert contract.margin is None
            assert contract.delivery_date is None
            assert "margin_asset" not in sd
            assert "delivery_date" not in sd
            assert None not in sd.values()


class TestKrakenSpotUnderlying:
    def test_classes(self):
        rows = _by_id(_fetch_spot())
        assert {i: sd["underlying"] for i, sd in rows.items()} == {
            "BTC/USD": "crypto",
            "ETH/BTC": "crypto",
            "DOGE/USD": "crypto",
            "DOGE/BTC": "crypto",
            # Fiat-to-fiat pairs are FX.
            "EUR/USD": "forex",
            "USD/CHF": "forex",
            # Stablecoins (even a franc stablecoin) and tokenized gold are crypto.
            "ZCHF/CHF": "crypto",
            "USDT/USD": "crypto",
            "PAXG/USD": "crypto",
            "0G/EUR": "crypto",
            "BTC/SOFID": "crypto",
            "AUSD/EUR": "crypto",
            "BTC/USD1": "crypto",
            "ETH/EUR": "crypto",
            "ACA/EUR": "crypto",
            # xStocks (stocks and ETFs, incl. the GLD gold ETF) are equity.
            "AAPLx/USD": "equity",
            "AAPLSPV/USD": "equity",
            "BRK.Bx/USD": "equity",
            "BRK.BSPV/USD": "equity",
            "WBDx/USD": "equity",
            "WBDSPV/USD": "equity",
            "GLDx/USD": "equity",
            "GLDSPV/USD": "equity",
        }

    def test_unknown_asset_class_is_unknown_not_crypto(self):
        rows = _by_id(_fetch_spot(_with_pair("XXBTZUSD", aclass_base="bond")))
        assert rows["BTC/USD"]["underlying"] == "unknown"


class TestKrakenSpotLifecycle:
    def test_no_listing_time_is_published(self):
        assert all("first_capture" not in sd for sd in _fetch_spot())

    @pytest.mark.parametrize(
        "status", ["online", "cancel_only", "post_only", "limit_only", "reduce_only", "maintenance"]
    )
    def test_trading_and_restricted_pairs_stay_listed(self, status):
        rows = _by_id(_fetch_spot(_with_pair("XXBTZUSD", status=status)))
        assert "end_date" not in rows["BTC/USD"]

    def test_real_restricted_pairs_stay_listed(self):
        rows = _by_id(_fetch_spot())
        assert "end_date" not in rows["ACA/EUR"]  # cancel_only
        assert "end_date" not in rows["AUSD/EUR"]  # post_only
        assert "end_date" not in rows["WBDx/USD"]  # cancel_only xStock

    def test_delisted_pair_ends_at_first_sighting(self):
        before = datetime.now(UTC) - timedelta(seconds=1)
        rows = _by_id(_fetch_spot(_with_pair("XXBTZUSD", status="delisted")))
        end = datetime.fromisoformat(rows["BTC/USD"]["end_date"].replace("Z", "+00:00"))
        assert before <= end <= datetime.now(UTC) + timedelta(seconds=1)

    def test_pair_being_set_up_is_flagged_upcoming(self):
        rows = _by_id(_fetch_spot(_with_pair("XXBTZUSD", status="work_in_progress")))
        assert rows["BTC/USD"] == {"id": "BTC/USD", "type": "spot", "upcoming": True}


class TestKrakenSpotPayloadShape:
    @pytest.mark.parametrize(
        "payload",
        [
            {"error": ["EGeneral:Invalid arguments"]},
            {"error": ["EService:Unavailable"], "result": {}},
            {"error": [], "result": []},
            {"error": [], "result": {}},
            [],
            "nope",
        ],
    )
    def test_malformed_currency_payload_raises(self, payload):
        with pytest.raises(ValueError):
            _fetch_spot(pairs=payload)

    def test_malformed_tokenized_payload_raises(self):
        with pytest.raises(ValueError):
            _fetch_spot(tokenized={"error": ["EGeneral:Internal error"]})


class TestKrakenSpotTardisFallback:
    @pytest.mark.parametrize(
        ("sid", "expected"),
        [
            ("BTC/USD", "spot-BTC-USD"),
            # WebSocket v1 names (Tardis until 2026-07-10).
            ("XBT/USD", "spot-BTC-USD"),
            ("ETH/XBT", "spot-ETH-BTC"),
            ("XDG/USD", "spot-DOGE-USD"),
            ("XDG/XBT", "spot-DOGE-BTC"),
            ("ZRX/XBT", "spot-ZRX-BTC"),
            ("ZCHF/CHF", "spot-ZCHF-CHF"),
            ("ETH2.S/ETH", "spot-ETH2.S-ETH"),
            ("AAPLx/USD", "spot-AAPLX-USD"),
            ("AAPLSPV/USD", "spot-AAPLX-USD"),
            ("BRK.BSPV/USD", "spot-BRK.BX-USD"),
            # Pairs Kraken routed to its `BTNL` execution venue for two days in
            # 2026: another order book, same instrument economics.
            ("AAVE/USD:BTNL", "spot-AAVE-USD"),
            ("XBT/USD:BTNL", "spot-BTC-USD"),
            ("XDG/USD:BTNL", "spot-DOGE-USD"),
        ],
    )
    def test_tardis_ids(self, sid, expected):
        assert _spot_iid({"id": sid, "type": "spot"}) == expected

    def test_every_tardis_row_parses(self):
        for row in TARDIS_SPOT:
            assert parse_kraken_spot("kraken-spot", row).internal_id.startswith("spot-")

    @pytest.mark.parametrize("sid", ["BTCUSD", "BTC/", "/USD", "A/B/C", ""])
    def test_unparseable_ids_skip(self, sid):
        with pytest.raises(SkipSymbol):
            parse_kraken_spot("kraken-spot", {"id": sid, "type": "spot"})

    def test_tardis_rows_match_api_rows(self):
        api = {sd["id"]: _spot_iid(sd) for sd in _fetch_spot()}
        tardis = {row["id"]: _spot_iid(row) for row in TARDIS_SPOT}
        shared = api.keys() & tardis.keys()
        assert len(shared) >= 20
        assert {i: tardis[i] for i in shared} == {i: api[i] for i in shared}

    def test_renamed_v1_rows_match_v2_api_rows(self):
        api = {sd["id"]: _spot_iid(sd) for sd in _fetch_spot()}
        for old, new in [("XBT/USD", "BTC/USD"), ("ETH/XBT", "ETH/BTC"), ("XDG/XBT", "DOGE/BTC")]:
            assert _spot_iid({"id": old, "type": "spot"}) == api[new]


# ---------------------------------------------------------------------------
# kraken-perps / kraken-futures (Kraken Futures, Tardis `cryptofacilities`)
# ---------------------------------------------------------------------------


class TestKrakenDerivativeFetch:
    def test_perpetual_ids(self):
        ids = [sd["id"] for sd in _fetch_deriv(fetch_kraken_perpetual)]
        assert ids == [
            "PF_XBTUSD", "PI_XBTUSD", "PI_ETHUSD", "PF_AAPLXUSD", "PF_EURUSD", "PF_XAUUSD",
            "PF_US100USD", "PF_ANTHROPICXUSD", "PF_NBISUSD", "PF_USDTUSD", "PF_PAXGUSD",
            "PF_CFGUSD", "PF_DOGEUSD", "PF_ETHUSD",
        ]
        assert all(sd["type"] == "perpetual" for sd in _fetch_deriv(fetch_kraken_perpetual))

    def test_future_ids(self):
        rows = _fetch_deriv(fetch_kraken_futures)
        assert [sd["id"] for sd in rows] == ["FF_XBTUSD_261225", "FI_XBTUSD_261225", "FF_ETHUSD_261023"]
        assert all(sd["type"] == "future" for sd in rows)

    def test_every_api_id_is_a_tardis_id(self):
        tardis_ids = {row["id"] for row in TARDIS_DERIV}
        rows = _fetch_deriv(fetch_kraken_perpetual) + _fetch_deriv(fetch_kraken_futures)
        assert {sd["id"] for sd in rows} <= tardis_ids

    def test_internal_ids(self):
        perps = {sd["id"]: _deriv_iid(sd) for sd in _fetch_deriv(fetch_kraken_perpetual)}
        futures = {
            sd["id"]: _deriv_iid(sd, "kraken-futures") for sd in _fetch_deriv(fetch_kraken_futures)
        }
        assert perps == {
            "PF_XBTUSD": "perpetual-BTC-USD:USD",
            "PI_XBTUSD": "perpetual-BTC-USD:BTC",
            "PI_ETHUSD": "perpetual-ETH-USD:ETH",
            "PF_AAPLXUSD": "perpetual-AAPLX-USD:USD",
            "PF_EURUSD": "perpetual-EUR-USD:USD",
            "PF_XAUUSD": "perpetual-XAU-USD:USD",
            "PF_US100USD": "perpetual-US100-USD:USD",
            "PF_ANTHROPICXUSD": "perpetual-ANTHROPICX-USD:USD",
            "PF_NBISUSD": "perpetual-NBIS-USD:USD",
            "PF_USDTUSD": "perpetual-USDT-USD:USD",
            "PF_PAXGUSD": "perpetual-PAXG-USD:USD",
            "PF_CFGUSD": "perpetual-CFG-USD:USD",
            "PF_DOGEUSD": "perpetual-DOGE-USD:USD",
            "PF_ETHUSD": "perpetual-ETH-USD:USD",
        }
        assert futures == {
            "FF_XBTUSD_261225": "future-BTC-USD:USD-20261225",
            "FI_XBTUSD_261225": "future-BTC-USD:BTC-20261225",
            "FF_ETHUSD_261023": "future-ETH-USD:USD-20261023",
        }

    def test_linear_multi_collateral_contracts(self):
        rows = _by_id(_fetch_deriv(fetch_kraken_perpetual) + _fetch_deriv(fetch_kraken_futures))
        for sid in ["PF_XBTUSD", "FF_XBTUSD_261225", "PF_AAPLXUSD"]:
            sd = rows[sid]
            assert sd["margin_asset"] == "USD"
            assert sd["quantity_unit"] == "base"
            assert sd["contract_size"] == 1.0
            contract = parse_kraken_derivative("kraken-perps", sd)
            assert contract.margin == "USD"
            assert contract.quantity_unit == "base"
            assert contract.contract_size == 1.0

    def test_inverse_contracts_are_one_usd_and_settle_in_base(self):
        rows = _by_id(_fetch_deriv(fetch_kraken_perpetual) + _fetch_deriv(fetch_kraken_futures))
        for sid, base in [("PI_XBTUSD", "BTC"), ("PI_ETHUSD", "ETH"), ("FI_XBTUSD_261225", "BTC")]:
            sd = rows[sid]
            assert (sd["base_coin"], sd["quote_coin"], sd["margin_asset"]) == (base, "USD", base)
            assert sd["quantity_unit"] == "quote"
            assert sd["contract_size"] == 1.0

    def test_legs_use_the_venue_base(self):
        rows = _by_id(_fetch_deriv(fetch_kraken_perpetual))
        assert rows["PF_XBTUSD"]["base_coin"] == "BTC"
        assert rows["PF_AAPLXUSD"]["base_coin"] == "AAPLx"
        assert rows["PF_EURUSD"]["base_coin"] == "EUR"

    def test_delivery_dates(self):
        rows = _by_id(_fetch_deriv(fetch_kraken_futures))
        assert rows["FF_XBTUSD_261225"]["delivery_date"] == "2026-12-25T08:00:00Z"
        assert rows["FI_XBTUSD_261225"]["delivery_date"] == "2026-12-25T16:00:00Z"
        assert rows["FF_ETHUSD_261023"]["delivery_date"] == "2026-10-23T08:00:00Z"
        contract = parse_kraken_derivative("kraken-futures", rows["FI_XBTUSD_261225"])
        assert contract.delivery_date == datetime(2026, 12, 25, 16, tzinfo=UTC)
        assert all("delivery_date" not in sd for sd in _fetch_deriv(fetch_kraken_perpetual))

    def test_first_capture_is_the_opening_date(self):
        rows = _by_id(_fetch_deriv(fetch_kraken_perpetual) + _fetch_deriv(fetch_kraken_futures))
        assert rows["PI_XBTUSD"]["first_capture"] == "2018-08-31T00:00:00.000Z"
        assert rows["PF_XBTUSD"]["first_capture"] == "2022-03-22T13:15:36.000Z"
        assert rows["FF_ETHUSD_261023"]["first_capture"] == "2026-10-16T08:00:36.929Z"

    def test_no_none_values(self):
        for sd in _fetch_deriv(fetch_kraken_perpetual) + _fetch_deriv(fetch_kraken_futures):
            assert None not in sd.values()

    def test_options_and_unknown_types_are_ignored(self):
        payload = copy.deepcopy(INSTRUMENTS)
        option = dict(payload["instruments"][0], symbol="OF_XBTUSD_261225_100000_C", type="options")
        spot = dict(payload["instruments"][0], symbol="SOMETHING", type="spot_index")
        payload["instruments"] += [option, spot]
        ids = {sd["id"] for sd in _fetch_deriv(fetch_kraken_perpetual, payload)}
        ids |= {sd["id"] for sd in _fetch_deriv(fetch_kraken_futures, payload)}
        assert "OF_XBTUSD_261225_100000_C" not in ids
        assert "SOMETHING" not in ids


class TestKrakenDerivativeUnderlying:
    def test_classes_from_category(self):
        rows = _by_id(_fetch_deriv(fetch_kraken_perpetual) + _fetch_deriv(fetch_kraken_futures))
        assert {i: sd["underlying"] for i, sd in rows.items()} == {
            "PF_XBTUSD": "crypto",  # Layer 1
            # The legacy inverse contracts have no category; coin-margined
            # contracts can only be on crypto.
            "PI_XBTUSD": "crypto",
            "PI_ETHUSD": "crypto",
            "FF_XBTUSD_261225": "crypto",
            "FI_XBTUSD_261225": "crypto",
            "FF_ETHUSD_261023": "crypto",
            "PF_AAPLXUSD": "equity",  # xStocks
            "PF_NBISUSD": "equity",  # Equities (Pyth 24/7 index of a stock)
            "PF_EURUSD": "forex",  # Forex, tradfi
            "PF_XAUUSD": "commodity",  # Commodities
            "PF_US100USD": "index",  # Indices
            "PF_ANTHROPICXUSD": "pre_market",  # Pre-IPO
            "PF_USDTUSD": "crypto",  # Stablecoin
            "PF_PAXGUSD": "crypto",  # Stablecoin: Kraken files tokenized gold there
            "PF_CFGUSD": "crypto",  # Real-world assets: the Centrifuge token
            "PF_DOGEUSD": "crypto",  # Meme
            "PF_ETHUSD": "crypto",  # Layer 1
        }

    def test_unknown_category_is_unknown_not_crypto(self):
        payload = _with_instrument("PF_DOGEUSD", category="Bonds")
        rows = _by_id(_fetch_deriv(fetch_kraken_perpetual, payload))
        assert rows["PF_DOGEUSD"]["underlying"] == "unknown"

    def test_empty_category_on_linear_contract_is_unknown(self):
        payload = _with_instrument("PF_DOGEUSD", category="")
        rows = _by_id(_fetch_deriv(fetch_kraken_perpetual, payload))
        assert rows["PF_DOGEUSD"]["underlying"] == "unknown"

    def test_tradfi_flag_overrides_a_crypto_sector(self):
        payload = _with_instrument("PF_DOGEUSD", tradfi=True)
        rows = _by_id(_fetch_deriv(fetch_kraken_perpetual, payload))
        assert rows["PF_DOGEUSD"]["underlying"] == "unknown"


class TestKrakenDerivativeLifecycle:
    def test_listed_contracts_have_no_end_date(self):
        for sd in _fetch_deriv(fetch_kraken_perpetual) + _fetch_deriv(fetch_kraken_futures):
            assert "end_date" not in sd

    @pytest.mark.parametrize(
        "changes", [{"suspended": True}, {"postOnly": True}, {"tradeable": False}]
    )
    def test_halted_contracts_stay_listed(self, changes):
        rows = _by_id(_fetch_deriv(fetch_kraken_perpetual, _with_instrument("PF_DOGEUSD", **changes)))
        assert "end_date" not in rows["PF_DOGEUSD"]

    def test_expired_future_ends_at_last_trading_time(self):
        payload = _with_instrument(
            "FF_ETHUSD_261023", isExpired=True, lastTradingTime="2026-10-16T08:00:00Z"
        )
        rows = _by_id(_fetch_deriv(fetch_kraken_futures, payload))
        assert rows["FF_ETHUSD_261023"]["end_date"] == "2026-10-16T08:00:00.000Z"

    def test_expired_contract_without_time_ends_at_first_sighting(self):
        payload = _with_instrument("PF_DOGEUSD", isExpired=True)
        rows = _by_id(_fetch_deriv(fetch_kraken_perpetual, payload))
        end = datetime.fromisoformat(rows["PF_DOGEUSD"]["end_date"].replace("Z", "+00:00"))
        assert abs(end - datetime.now(UTC)) < timedelta(seconds=5)

    def test_pre_launch_contracts_are_flagged_upcoming(self):
        later = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = _with_instrument("PF_DOGEUSD", openingDate=later)
        rows = {sd["id"]: sd for sd in _fetch_deriv(fetch_kraken_perpetual, payload)}
        assert rows["PF_DOGEUSD"] == {"id": "PF_DOGEUSD", "type": "perpetual", "upcoming": True}
        assert not rows["PF_XBTUSD"].get("upcoming")


class TestKrakenDerivativePayloadShape:
    @pytest.mark.parametrize(
        "payload",
        [
            {"result": "error", "error": "apiLimitExceeded"},
            {"result": "success"},
            {"result": "success", "instruments": {}},
            {"result": "success", "instruments": []},
            [],
        ],
    )
    def test_malformed_payload_raises(self, payload):
        with pytest.raises(ValueError):
            _fetch_deriv(fetch_kraken_perpetual, payload)


class TestKrakenDerivativeTardisFallback:
    @pytest.mark.parametrize(
        ("sid", "ctype", "expected"),
        [
            ("PF_XBTUSD", "perpetual", "perpetual-BTC-USD:USD"),
            ("PF_AAPLXUSD", "perpetual", "perpetual-AAPLX-USD:USD"),
            ("PF_USDTUSD", "perpetual", "perpetual-USDT-USD:USD"),
            ("PF_EURUSD", "perpetual", "perpetual-EUR-USD:USD"),
            ("PF_ETHBTCUSD", "perpetual", "perpetual-ETHBTC-USD:USD"),
            ("PI_XBTUSD", "perpetual", "perpetual-BTC-USD:BTC"),
            ("PI_DOGEUSD", "perpetual", "perpetual-DOGE-USD:DOGE"),
            ("FF_XBTUSD_261225", "future", "future-BTC-USD:USD-20261225"),
            ("FI_XBTUSD_191227", "future", "future-BTC-USD:BTC-20191227"),
            # 2019-20 XRP/XBT vanilla contracts, margined and settled in bitcoin.
            ("PV_XRPXBT", "perpetual", "perpetual-XRP-BTC:BTC"),
            ("FV_XRPXBT_190628", "future", "future-XRP-BTC:BTC-20190628"),
        ],
    )
    def test_tardis_ids(self, sid, ctype, expected):
        assert _deriv_iid({"id": sid, "type": ctype}) == expected

    def test_quantity_units_from_the_naming_scheme(self):
        def unit(sid, ctype="perpetual"):
            return parse_kraken_derivative("kraken-perps", {"id": sid, "type": ctype}).quantity_unit

        assert unit("PF_XBTUSD") == "base"
        assert unit("PI_XBTUSD") == "quote"
        assert unit("FI_XBTUSD_191227", "future") == "quote"
        assert unit("FV_XRPXBT_190628", "future") == "base"

    def test_every_non_option_tardis_row_parses(self):
        for row in TARDIS_DERIV:
            if row["type"] == "option":
                continue
            parse_kraken_derivative("kraken-perps", row)

    @pytest.mark.parametrize(
        ("sid", "ctype"),
        [
            ("OF_ETHUSD_261023_2100_C", "option"),
            ("XX_XBTUSD", "perpetual"),
            ("PF_XBTEUR", "perpetual"),
            ("PF_USD", "perpetual"),
            ("FF_XBTUSD_2612", "future"),
            ("FF_XBTUSD", "future"),
            ("PF", "perpetual"),
        ],
    )
    def test_unparseable_ids_skip(self, sid, ctype):
        with pytest.raises(SkipSymbol):
            parse_kraken_derivative("kraken-perps", {"id": sid, "type": ctype})

    def test_tardis_rows_match_api_rows(self):
        rows = _fetch_deriv(fetch_kraken_perpetual) + _fetch_deriv(fetch_kraken_futures)
        api = {sd["id"]: _deriv_iid(sd) for sd in rows}
        tardis = {row["id"]: _deriv_iid(row) for row in TARDIS_DERIV if row["id"] in api}
        assert len(tardis) == len(api)
        assert tardis == api
