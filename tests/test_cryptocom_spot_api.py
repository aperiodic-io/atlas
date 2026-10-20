"""Crypto.com spot from the exchange API (`cryptocom-spot`).

Fixtures under `tests/fixtures/cryptocom/` are excerpts of the real
`public/get-instruments` response and of Tardis's `crypto-com` symbol list,
captured on 2026-10-20. The same endpoint feeds `cryptocom-perps` and
`cryptocom-futures`, whose behaviour must not change.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.contracts import ContractType
from atlas.exchange_definitions.common import SkipSymbol
from atlas.exchange_definitions.crypto_com import (
    fetch_cryptocom_futures,
    fetch_cryptocom_perpetual,
    fetch_cryptocom_spot,
    parse_crypto_com,
    parse_cryptocom_spot,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "cryptocom"
INSTRUMENTS = json.loads((_FIXTURES / "instruments.json").read_text())
TARDIS = json.loads((_FIXTURES / "tardis.json").read_text())
SLUG = "cryptocom-spot"


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _fetch(fetcher=fetch_cryptocom_spot, payload=INSTRUMENTS) -> list[dict]:
    def get(url, params=None, timeout=None):
        if url.endswith("/public/get-instruments"):
            return _response(payload)
        raise AssertionError(f"unexpected request {url}")

    with patch("atlas.exchange_definitions.http.requests.get", side_effect=get):
        return fetcher(timeout_seconds=5)


def _spot(payload=INSTRUMENTS) -> dict[str, dict]:
    return {sd["id"]: sd for sd in _fetch(payload=payload)}


def _with(symbol: str, **changes) -> dict:
    payload = copy.deepcopy(INSTRUMENTS)
    for item in payload["result"]["data"]:
        if item["symbol"] == symbol:
            item.update(changes)
    return payload


def _internal(sd: dict) -> str:
    return parse_cryptocom_spot(SLUG, sd).internal_id


class TestFetch:
    def test_ids_and_internal_ids(self):
        assert {i: _internal(sd) for i, sd in _spot().items()} == {
            "BTC_USD": "spot-BTC-USD",
            "1INCH_USDT": "spot-1INCH-USDT",
            "ETH_BTC": "spot-ETH-BTC",
            "BTC_EUR": "spot-BTC-EUR",
            "CRO_USD": "spot-CRO-USD",
            "EUR_USDT": "spot-EUR-USDT",
            "SHIB_USD": "spot-SHIB-USD",
            # A venue-suffixed pair keeps its id; its legs are SHIB/BTC.
            "SHIB_BTC@OEX_HK": "spot-SHIB-BTC",
            "BTC_PYUSD": "spot-BTC-PYUSD",
            "ATOM_CRO": "spot-ATOM-CRO",
        }

    def test_only_currency_pairs(self):
        rows = _spot()
        assert all(sd["type"] == "spot" for sd in rows.values())
        assert "BTCUSD-PERP" not in rows
        assert "BTCUSD-261030" not in rows

    def test_spot_rows_have_no_derivative_fields(self):
        for sd in _spot().values():
            contract = parse_cryptocom_spot(SLUG, sd)
            assert contract.instrument_type == ContractType.spot
            assert contract.margin is None
            assert not {"margin_asset", "contract_size", "quantity_unit", "delivery_date"} & set(sd)

    def test_no_listing_time_or_end_date(self):
        # Crypto.com publishes neither; delisted pairs disappear.
        for sd in _spot().values():
            assert "first_capture" not in sd
            assert "end_date" not in sd

    def test_untradable_pair_is_left_out(self):
        assert "BTC_USD" not in _spot(_with("BTC_USD", tradable=False))

    def test_no_none_values(self):
        for sd in _spot().values():
            assert None not in sd.values()


class TestUnderlying:
    def test_classes_follow_product_type(self):
        assert {i: sd["underlying"] for i, sd in _spot().items()} == {
            "BTC_USD": "crypto",
            "1INCH_USDT": "crypto",
            "ETH_BTC": "crypto",
            "BTC_EUR": "crypto",
            "CRO_USD": "crypto",
            # `product_type: CURRENCIES`.
            "EUR_USDT": "forex",
            "SHIB_USD": "crypto",
            # No `product_type`: SHIB is `DIGITAL_CURRENCIES` on `SHIB_USD`.
            "SHIB_BTC@OEX_HK": "crypto",
            "BTC_PYUSD": "crypto",
            "ATOM_CRO": "crypto",
        }

    def test_untagged_pair_of_an_unknown_base_is_unknown(self):
        payload = _with("SHIB_USD", product_type=None)
        assert _spot(payload)["SHIB_BTC@OEX_HK"]["underlying"] == "unknown"

    def test_unknown_product_type_is_unknown(self):
        assert _spot(_with("BTC_USD", product_type="BONDS"))["BTC_USD"]["underlying"] == "unknown"

    @pytest.mark.parametrize(
        ("product_type", "expected"),
        [("EQUITY", "equity"), ("COMMODITIES", "commodity"), ("PRE_IPO", "pre_market")],
    )
    def test_tradfi_product_types(self, product_type, expected):
        assert _spot(_with("CRO_USD", product_type=product_type))["CRO_USD"]["underlying"] == expected


class TestMalformed:
    @pytest.mark.parametrize(
        "payload",
        [
            {"id": 1, "method": "public/get-instruments", "code": 10004, "message": "BAD_REQUEST"},
            {"id": 1, "code": 0, "result": {}},
            {"id": 1, "code": 0, "result": {"data": {"symbol": "BTC_USD"}}},
            {"id": 1, "code": 0},
            [],
            "oops",
        ],
    )
    def test_bad_payload_raises(self, payload):
        with pytest.raises(ValueError):
            _fetch(payload=payload)


class TestDerivativesUnchanged:
    def test_perpetuals_and_futures_still_come_from_the_same_payload(self):
        perps = {sd["id"] for sd in _fetch(fetch_cryptocom_perpetual)}
        futures = {sd["id"] for sd in _fetch(fetch_cryptocom_futures)}
        assert perps == {"BTCUSD-PERP"}
        assert futures == {"BTCUSD-261030"}

    def test_spot_and_derivatives_do_not_overlap(self):
        spot = {sd["id"] for sd in _fetch()}
        derivatives = {sd["id"] for sd in _fetch(fetch_cryptocom_perpetual) + _fetch(fetch_cryptocom_futures)}
        assert not spot & derivatives


class TestTardisFallback:
    def test_every_tardis_spot_row_parses(self):
        for row in TARDIS:
            if row["type"] == "spot":
                contract = parse_cryptocom_spot(SLUG, {"id": row["id"], "type": "spot"})
                assert contract.instrument_type == ContractType.spot

    def test_shared_tardis_feed_is_split_by_type(self):
        # `crypto-com` carries spot, perpetuals, futures and options:
        # `cryptocom-spot` keeps only `spot` rows (allowed_types={"spot"}).
        assert {row["type"] for row in TARDIS} == {"spot", "perpetual"}

    def test_tardis_rows_match_api_rows(self):
        api = {i: _internal(sd) for i, sd in _spot().items()}
        tardis = {row["id"]: _internal({"id": row["id"], "type": "spot"}) for row in TARDIS if row["type"] == "spot"}
        shared = set(api) & set(tardis)
        assert len(shared) == 9
        assert {i: api[i] for i in shared} == {i: tardis[i] for i in shared}

    @pytest.mark.parametrize(
        ("sid", "expected"),
        [
            ("BTC_USD", "spot-BTC-USD"),
            ("1INCH_USDT", "spot-1INCH-USDT"),
            ("1INCH_USDC", "spot-1INCH-USDC"),
            ("SHIB_BTC@OEX_HK", "spot-SHIB-BTC"),
            ("BTC_PYUSD", "spot-BTC-PYUSD"),
        ],
    )
    def test_id_parser(self, sid, expected):
        contract = parse_cryptocom_spot(SLUG, {"id": sid, "type": "spot"})
        assert contract.internal_id == expected
        assert contract.original_id == sid

    @pytest.mark.parametrize("sid", ["BTCUSD-PERP", "BTC_USD_X", "_USD", "BTC_", "@OEX"])
    def test_non_pair_ids_are_skipped(self, sid):
        with pytest.raises(SkipSymbol):
            parse_cryptocom_spot(SLUG, {"id": sid, "type": "spot"})


def test_legacy_crypto_com_parser_is_unchanged():
    with pytest.raises(SkipSymbol):
        parse_crypto_com("crypto-com", {"id": "SHIB_BTC@OEX_HK", "type": "spot"})
    assert parse_crypto_com("crypto-com", {"id": "BTC_USD", "type": "spot"}).internal_id == "spot-BTC-USD"
