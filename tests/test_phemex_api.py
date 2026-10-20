"""Phemex spot and perpetuals from the exchange API (`phemex-spot`, `phemex-perps`).

Fixtures under `tests/fixtures/phemex/` are excerpts (trimmed to the fields
the fetchers read) of the real `/public/products-plus` response and of
Tardis's `phemex` symbol list, captured on 2026-10-20.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.contracts import ContractType
from atlas.exchange_definitions.common import SkipSymbol, parse_utc_timestamp
from atlas.exchange_definitions.phemex import (
    fetch_phemex_perpetual,
    fetch_phemex_spot,
    parse_phemex,
    parse_phemex_api,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "phemex"
PRODUCTS = json.loads((_FIXTURES / "products_plus.json").read_text())
TARDIS = json.loads((_FIXTURES / "tardis.json").read_text())


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _fetch(fetcher, payload=PRODUCTS) -> list[dict]:
    def get(url, params=None, timeout=None):
        if url.endswith("/public/products-plus"):
            return _response(payload)
        raise AssertionError(f"unexpected request {url}")

    with patch("atlas.exchange_definitions.http.requests.get", side_effect=get):
        return fetcher(timeout_seconds=5)


def _spot(payload=PRODUCTS) -> dict[str, dict]:
    return {sd["id"]: sd for sd in _fetch(fetch_phemex_spot, payload)}


def _perps(payload=PRODUCTS) -> dict[str, dict]:
    return {sd["id"]: sd for sd in _fetch(fetch_phemex_perpetual, payload)}


def _with(symbol: str, **changes) -> dict:
    payload = copy.deepcopy(PRODUCTS)
    data = payload["data"]
    for item in data["products"] + data["perpProductsV2"] + data["perpProductsPilot"]:
        if item["symbol"] == symbol:
            item.update(changes)
    return payload


def _internal(slug: str, sd: dict) -> str:
    return parse_phemex_api(slug, sd).internal_id


def _ms(delta: timedelta) -> int:
    return int((datetime.now(UTC) + delta).timestamp() * 1000)


SPOT_IDS = {
    "sBTCUSDT": "spot-BTC-USDT",
    "sETHBTC": "spot-ETH-BTC",
    "sUSDTTRY": "spot-USDT-TRY",
    "sBTCUSDQ": "spot-BTC-USDQ",
    "sVOXELUSDT": "spot-VOXEL-USDT",
    "sEPICUSDT": "spot-EPIC-USDT",
    "sAAPLONUSDT": "spot-AAPLON-USDT",
    "sAAPLXUSDT": "spot-AAPLX-USDT",
    "sSPCXXUSDT": "spot-SPCXX-USDT",
    "s1000BONKUSDT": "spot-1000BONK-USDT",
    "sPAXGUSDT": "spot-PAXG-USDT",
    "sXAUTUSDT": "spot-XAUT-USDT",
    "s0GUSDT": "spot-0G-USDT",
}

PERP_IDS = {
    # Legacy book: inverse, coin-margined, and the USD-settled contracts.
    "BTCUSD": "perpetual-BTC-USD:BTC",
    "cETHUSD": "perpetual-ETH-USD:ETH",
    "ETHUSD": "perpetual-ETH-USD:USD",
    "XRPUSD": "perpetual-XRP-USD:USD",
    "uBTCUSD": "perpetual-BTC-USD:USD",
    "u100VETUSD": "perpetual-100VET-USD:USD",
    "GOLDUSD": "perpetual-GOLD-USD:USD",
    # Linear (`perpProductsV2`).
    "BTCUSDT": "perpetual-BTC-USDT:USDT",
    "BTCUSDC": "perpetual-BTC-USDC:USDC",
    "AAVEUSDC": "perpetual-AAVE-USDC:USDC",
    "u1000PEPEUSDT": "perpetual-1000PEPE-USDT:USDT",
    "u1000BONKUSDT": "perpetual-1000BONK-USDT:USDT",
    "AAPLUSDT": "perpetual-AAPL-USDT:USDT",
    "AAOIUSDT": "perpetual-AAOI-USDT:USDT",
    "SOXLUSDT": "perpetual-SOXL-USDT:USDT",
    "NAS100USDT": "perpetual-NAS100-USDT:USDT",
    "QQQUSDT": "perpetual-QQQ-USDT:USDT",
    "XAUUSDT": "perpetual-XAU-USDT:USDT",
    "PAXGUSDT": "perpetual-PAXG-USDT:USDT",
    "XTIUSDT": "perpetual-XTI-USDT:USDT",
    "OPENAIUSDT": "perpetual-OPENAI-USDT:USDT",
    "SPACEXUSDT": "perpetual-SPACEX-USDT:USDT",
    "DOGEAIUSDT": "perpetual-DOGEAI-USDT:USDT",
    "1INCHUSDT": "perpetual-1INCH-USDT:USDT",
    "GOLDUSDT": "perpetual-GOLD-USDT:USDT",
    # Pilot book, settled in Phemex's PUSD.
    "DOGEAIPUSD": "perpetual-DOGEAI-PUSD:PUSD",
    "GREED3PUSD": "perpetual-GREED3-PUSD:PUSD",
}


class TestSpotFetch:
    def test_ids_and_internal_ids(self):
        rows = _spot()
        assert {i: _internal("phemex-spot", sd) for i, sd in rows.items()} == SPOT_IDS

    def test_spot_excludes_contracts(self):
        assert all(sd["type"] == "spot" and sd["id"].startswith("s") for sd in _spot().values())

    def test_legs_come_from_the_venue(self):
        row = _spot()["s1000BONKUSDT"]
        assert (row["base_coin"], row["quote_coin"]) == ("1000BONK", "USDT")
        contract = parse_phemex_api("phemex-spot", row)
        assert contract.margin is None
        assert contract.contract_size is None

    def test_underlying(self):
        rows = _spot()
        assert {i: sd["underlying"] for i, sd in rows.items()} == {
            "sBTCUSDT": "crypto",
            "sETHBTC": "crypto",
            "sUSDTTRY": "crypto",
            "sBTCUSDQ": "crypto",
            "sVOXELUSDT": "crypto",
            "sEPICUSDT": "crypto",
            # Currency names: `Apple Ondo Tokenized`, `Apple xStock`,
            # `SpaceX tokenized stock (xStock)`.
            "sAAPLONUSDT": "equity",
            "sAAPLXUSDT": "equity",
            "sSPCXXUSDT": "equity",
            "s1000BONKUSDT": "crypto",
            # Phemex spot does not classify tokenized gold.
            "sPAXGUSDT": "crypto",
            "sXAUTUSDT": "crypto",
            "s0GUSDT": "crypto",
        }

    def test_first_capture_is_the_listing_time(self):
        assert _spot()["sBTCUSDT"]["first_capture"] == "2020-05-13T03:00:00.000Z"

    def test_no_none_values(self):
        for sd in _spot().values():
            assert None not in sd.values()


class TestSpotLifecycle:
    def test_delisted_pair_ends_at_its_delisted_milestone(self):
        rows = _spot()
        assert rows["sUSDTTRY"]["end_date"] == "2024-09-24T10:00:00.000Z"
        assert rows["sAAPLXUSDT"]["end_date"] == "2026-09-10T10:00:00.000Z"

    def test_delisted_pair_without_a_timeline_ends_now(self):
        before = datetime.now(UTC) - timedelta(seconds=1)
        end = parse_utc_timestamp(_spot()["sBTCUSDQ"]["end_date"])
        assert before <= end <= datetime.now(UTC) + timedelta(seconds=1)

    def test_delisting_start_is_used_when_the_end_milestone_is_unset(self):
        item = next(p for p in PRODUCTS["data"]["products"] if p["symbol"] == "sUSDTTRY")
        timeline = [*item["timeline"][:4], 0]
        assert _spot(_with("sUSDTTRY", timeline=timeline))["sUSDTTRY"]["end_date"] == "2024-09-24T10:00:00.000Z"

    def test_listed_pair_with_a_passed_delisting_schedule_stays_listed(self):
        # Phemex still reports `sVOXELUSDT` Listed after its 2025-12-05 schedule.
        rows = _spot()
        assert rows["sVOXELUSDT"]["first_capture"]
        assert "end_date" not in rows["sVOXELUSDT"]
        assert "end_date" not in rows["sEPICUSDT"]

    def test_listed_pairs_have_no_end_date(self):
        listed = {p["symbol"] for p in PRODUCTS["data"]["products"] if p["status"] == "Listed"}
        rows = _spot()
        assert all("end_date" not in rows[i] for i in listed if i in rows)

    def test_pair_not_yet_listed_is_excluded(self):
        assert "sBTCUSDT" not in _spot(_with("sBTCUSDT", listTime=_ms(timedelta(days=1))))

    def test_unknown_status_stays_listed(self):
        row = _spot(_with("sBTCUSDT", status="Suspended"))["sBTCUSDT"]
        assert "end_date" not in row

    def test_delisted_pair_with_a_future_list_time_is_still_closed(self):
        row = _spot(_with("sUSDTTRY", listTime=_ms(timedelta(days=1))))["sUSDTTRY"]
        assert "end_date" in row


class TestPerpFetch:
    def test_ids_and_internal_ids(self):
        rows = _perps()
        assert {i: _internal("phemex-perps", sd) for i, sd in rows.items()} == PERP_IDS

    def test_inverse_and_linear_btc_both_map(self):
        rows = _perps()
        assert _internal("phemex-perps", rows["BTCUSD"]) == "perpetual-BTC-USD:BTC"
        assert _internal("phemex-perps", rows["BTCUSDT"]) == "perpetual-BTC-USDT:USDT"

    def test_perps_exclude_spot(self):
        assert all(sd["type"] == "perpetual" for sd in _perps().values())
        assert not any(i.startswith("s") for i in _perps())

    def test_contract_sizes_and_units(self):
        rows = _perps()
        sizes = {i: (sd["contract_size"], sd["quantity_unit"]) for i, sd in rows.items()}
        # Inverse: each contract is 1 USD.
        assert sizes["BTCUSD"] == (1.0, "quote")
        assert sizes["cETHUSD"] == (1.0, "quote")
        # USD-settled legacy contracts are fractions of the coin.
        assert sizes["ETHUSD"] == (0.005, "base")
        assert sizes["XRPUSD"] == (5.0, "base")
        assert sizes["uBTCUSD"] == (0.001, "base")
        assert sizes["u100VETUSD"] == (1.0, "base")
        # Linear quantities are base units.
        assert sizes["BTCUSDT"] == (1.0, "base")
        assert sizes["u1000PEPEUSDT"] == (1.0, "base")
        assert sizes["DOGEAIPUSD"] == (1.0, "base")

    def test_contract_fields_reach_the_contract(self):
        contract = parse_phemex_api("phemex-perps", _perps()["ETHUSD"])
        assert contract.contract_size == 0.005
        assert contract.quantity_unit == "base"
        assert contract.margin == "USD"
        inverse = parse_phemex_api("phemex-perps", _perps()["BTCUSD"])
        assert (inverse.margin, inverse.quantity_unit) == ("BTC", "quote")

    def test_multiplied_units_drop_the_space(self):
        rows = _perps()
        assert rows["u1000PEPEUSDT"]["base_coin"] == "1000PEPE"
        assert rows["u100VETUSD"]["base_coin"] == "100VET"

    def test_margin_is_the_settle_currency(self):
        rows = _perps()
        assert {i: sd["margin_asset"] for i, sd in rows.items() if i in {"BTCUSD", "cETHUSD", "ETHUSD", "BTCUSDT", "AAVEUSDC", "DOGEAIPUSD"}} == {
            "BTCUSD": "BTC",
            "cETHUSD": "ETH",
            "ETHUSD": "USD",
            "BTCUSDT": "USDT",
            "AAVEUSDC": "USDC",
            "DOGEAIPUSD": "PUSD",
        }

    def test_no_dated_contracts(self):
        assert all("delivery_date" not in sd for sd in _perps().values())

    def test_no_none_values(self):
        for sd in _perps().values():
            assert None not in sd.values()


class TestPerpUnderlying:
    def test_classes(self):
        rows = _perps()
        assert {i: sd["underlying"] for i, sd in rows.items()} == {
            "BTCUSD": "crypto",
            "cETHUSD": "crypto",
            "ETHUSD": "crypto",
            "XRPUSD": "crypto",
            "uBTCUSD": "crypto",
            "u100VETUSD": "crypto",
            # Legacy `.GOLD` index, which Phemex does not describe.
            "GOLDUSD": "unknown",
            "BTCUSDT": "crypto",
            "BTCUSDC": "crypto",
            "AAVEUSDC": "crypto",
            "u1000PEPEUSDT": "crypto",
            "u1000BONKUSDT": "crypto",
            "AAPLUSDT": "equity",
            "AAOIUSDT": "equity",
            "SOXLUSDT": "equity",
            "NAS100USDT": "index",
            # Phemex files the QQQ ETF under `Indices`.
            "QQQUSDT": "index",
            "XAUUSDT": "commodity",
            "PAXGUSDT": "commodity",
            "XTIUSDT": "commodity",
            "OPENAIUSDT": "pre_market",
            "SPACEXUSDT": "pre_market",
            # `Pilot` sub-type in the linear book.
            "DOGEAIUSDT": "crypto",
            "1INCHUSDT": "crypto",
            # Phemex files this one `Normal`.
            "GOLDUSDT": "crypto",
            "DOGEAIPUSD": "crypto",
            "GREED3PUSD": "crypto",
        }

    def test_unknown_tradfi_group_is_unknown(self):
        row = _perps(_with("AAPLUSDT", perpProductSubGroup="Bonds"))["AAPLUSDT"]
        assert row["underlying"] == "unknown"

    def test_unknown_sub_type_is_unknown(self):
        row = _perps(_with("BTCUSDT", perpProductSubType="Innovation"))["BTCUSDT"]
        assert row["underlying"] == "unknown"


class TestPerpLifecycle:
    def test_delisted_contracts_end_at_their_delisted_milestone(self):
        rows = _perps()
        assert rows["ETHUSD"]["end_date"] == "2024-05-16T11:00:00.000Z"
        assert rows["AAOIUSDT"]["end_date"] == "2026-08-19T09:30:00.000Z"
        assert rows["DOGEAIPUSD"]["end_date"] == "2025-04-17T08:00:00.000Z"

    def test_listed_contracts_stay_open(self):
        rows = _perps()
        for sid in ["BTCUSD", "cETHUSD", "BTCUSDT", "AAPLUSDT", "OPENAIUSDT"]:
            assert "end_date" not in rows[sid], sid

    def test_first_capture_is_the_listing_time(self):
        rows = _perps()
        assert rows["BTCUSD"]["first_capture"] == "2019-11-25T00:00:00.000Z"
        assert rows["BTCUSDC"]["first_capture"] == "2025-05-20T10:00:00.000Z"

    @pytest.mark.parametrize("sid", ["BTCUSD", "BTCUSDT", "DOGEAIPUSD"])
    def test_contract_not_yet_listed_is_excluded(self, sid):
        payload = _with(sid, listTime=_ms(timedelta(hours=3)), status="Listed")
        assert sid not in _perps(payload)

    def test_contract_listed_a_moment_ago_is_kept(self):
        assert "BTCUSDT" in _perps(_with("BTCUSDT", listTime=_ms(-timedelta(minutes=1))))


class TestMalformed:
    @pytest.mark.parametrize(
        "payload",
        [
            {"code": 10001, "msg": "error", "data": None},
            {"code": 0, "msg": "", "data": None},
            {"code": 0, "msg": "", "data": {"products": None}},
            {"code": 0, "msg": "", "data": {"products": [], "perpProductsV2": {"BTCUSDT": {}}}},
            [],
            "oops",
        ],
    )
    @pytest.mark.parametrize("fetcher", [fetch_phemex_spot, fetch_phemex_perpetual])
    def test_bad_payload_raises(self, fetcher, payload):
        with pytest.raises(ValueError):
            _fetch(fetcher, payload)

    def test_missing_pilot_book_is_fine(self):
        payload = copy.deepcopy(PRODUCTS)
        payload["data"]["perpProductsPilot"] = None
        rows = _perps(payload)
        assert "DOGEAIPUSD" not in rows
        assert "BTCUSDT" in rows


class TestTardisFallback:
    def test_every_tardis_row_parses(self):
        for row in TARDIS:
            slug = "phemex-spot" if row["type"] == "spot" else "phemex-perps"
            contract = parse_phemex_api(slug, {"id": row["id"], "type": row["type"]})
            assert contract.instrument_type.value == row["type"]

    def test_tardis_rows_match_api_rows(self):
        api = {**{i: _internal("phemex-spot", sd) for i, sd in _spot().items()},
               **{i: _internal("phemex-perps", sd) for i, sd in _perps().items()}}
        shared = 0
        for row in TARDIS:
            if row["id"] in api:
                shared += 1
                assert _internal("phemex", {"id": row["id"], "type": row["type"]}) == api[row["id"]], row["id"]
        assert shared == 18

    @pytest.mark.parametrize(
        ("sid", "kind", "expected", "unit"),
        [
            ("sBTCUSDT", "spot", "spot-BTC-USDT", None),
            ("s1000BONKUSDT", "spot", "spot-1000BONK-USDT", None),
            ("sIRENPUSD", "spot", "spot-IREN-PUSD", None),
            ("sINSUSDC", "spot", "spot-INS-USDC", None),
            ("sUSDTTRY", "spot", "spot-USDT-TRY", None),
            ("BTCUSD", "perpetual", "perpetual-BTC-USD:BTC", "quote"),
            ("cETHUSD", "perpetual", "perpetual-ETH-USD:ETH", "quote"),
            ("cXRPUSD", "perpetual", "perpetual-XRP-USD:XRP", "quote"),
            ("ETHUSD", "perpetual", "perpetual-ETH-USD:USD", "base"),
            ("XRPUSD", "perpetual", "perpetual-XRP-USD:USD", "base"),
            ("OPUSD", "perpetual", "perpetual-OP-USD:USD", "base"),
            ("u100SLPUSD", "perpetual", "perpetual-100SLP-USD:USD", "base"),
            ("uBTCUSD", "perpetual", "perpetual-BTC-USD:USD", "base"),
            ("u100VETUSD", "perpetual", "perpetual-100VET-USD:USD", "base"),
            ("BNXUSD", "perpetual", "perpetual-BNX-USD:USD", "base"),
            ("BTCUSDT", "perpetual", "perpetual-BTC-USDT:USDT", "base"),
            ("AAVEUSDC", "perpetual", "perpetual-AAVE-USDC:USDC", "base"),
            ("u1000PEPEUSDT", "perpetual", "perpetual-1000PEPE-USDT:USDT", "base"),
            ("AAPLUSDT", "perpetual", "perpetual-AAPL-USDT:USDT", "base"),
            ("LSKUSDT", "perpetual", "perpetual-LSK-USDT:USDT", "base"),
            ("DOGEAIPUSD", "perpetual", "perpetual-DOGEAI-PUSD:PUSD", "base"),
            ("GREED3PUSD", "perpetual", "perpetual-GREED3-PUSD:PUSD", "base"),
        ],
    )
    def test_id_parser(self, sid, kind, expected, unit):
        contract = parse_phemex_api("phemex", {"id": sid, "type": kind})
        assert contract.internal_id == expected
        assert contract.quantity_unit == unit
        assert contract.original_id == sid

    @pytest.mark.parametrize("sid", ["sBTC", "s", "BTCEUR", "uusd", "uBTC-USDT"])
    def test_unparseable_ids_are_skipped(self, sid):
        kind = "spot" if sid.startswith("s") else "perpetual"
        with pytest.raises(SkipSymbol):
            parse_phemex_api("phemex", {"id": sid, "type": kind})

    def test_spot_fallback_is_spot(self):
        assert parse_phemex_api("phemex", {"id": "sBTCUSDT", "type": "spot"}).instrument_type == ContractType.spot


def test_legacy_phemex_parser_is_unchanged():
    # The Tardis-named `phemex` slug keeps its heuristic margins.
    assert parse_phemex("phemex", {"id": "ETHUSD", "type": "perpetual"}).internal_id == "perpetual-ETH-USD:ETH"
    assert parse_phemex("phemex", {"id": "BTCUSD", "type": "perpetual"}).internal_id == "perpetual-BTC-USD:BTC"
