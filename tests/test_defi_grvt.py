"""GRVT: parser and exchange-API fetcher on recorded payloads.

The internal IDs must equal the IDs the live-producers publish under the Atlas
scheme for the same contracts (cross-checked against the live venues), so the
expectations below are the producer's.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.exchange_definitions import is_beta_exchange
from atlas.exchanges import (
    get_allowed_types,
    get_exchange_fetcher,
)
from atlas.parsers import SkipSymbol, parse_contract
from atlas.types import UnderlyingType
from atlas.update import EXCHANGES

DATA = Path(__file__).resolve().parent / "data" / "defi"


def _payload(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _response(payload) -> MagicMock:
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


def test_registered_as_stable_perpetual_venue_with_an_api_fetcher():
    # A beta exchange is skipped by the daily update; GRVT is fetched from the
    # venue (Tardis does not cover it).
    slug = "grvt-perpetual"
    assert not is_beta_exchange(slug)
    assert slug in EXCHANGES
    assert get_allowed_types(slug) == {"perpetual"}
    assert get_exchange_fetcher(slug) is not None


# ---------------------------------------------------------------------------
# GRVT
# ---------------------------------------------------------------------------


class TestGrvt:
    def _fetch(self):
        from atlas.exchange_definitions.grvt import fetch_grvt_perps

        with patch(
            "atlas.exchange_definitions.grvt.requests.post",
            return_value=_response(_payload("grvt_all_instruments.json")),
        ) as post:
            symbols = fetch_grvt_perps(timeout_seconds=5)
        assert post.call_args.kwargs["json"] == {"is_active": True}
        return symbols

    def test_perps_settle_in_their_usdt_quote(self):
        c = parse_contract(
            "grvt-perpetual", {"id": "BTC_USDT_Perp", "type": "perpetual"}
        )
        assert c.internal_id == "perpetual-BTC-USDT:USDT"

    def test_fetch_keeps_active_perps_with_venue_asset_class_and_listing_time(self):
        by_id = {sd["id"]: sd for sd in self._fetch()}
        assert set(by_id) == {
            "BTC_USDT_Perp",
            "TSLA_USDT_Perp",
            "XAU_USDT_Perp",
            "KPEPE_USDT_Perp",
            "QQQ_USDT_Perp",
            "ANTHROPIC_USDT_Perp",
            "OPENAI_USDT_Perp",
            "CL_USDT_Perp",
        }, "delisted instruments (AI16Z, SPCX) are not listed"
        assert by_id["TSLA_USDT_Perp"]["underlying"] == "equity"
        assert by_id["QQQ_USDT_Perp"]["underlying"] == "equity"
        assert by_id["XAU_USDT_Perp"]["underlying"] == "commodity"
        assert by_id["CL_USDT_Perp"]["underlying"] == "commodity"
        assert by_id["BTC_USDT_Perp"]["underlying"] == "crypto"
        # The venue labels private companies EQUITY.
        assert by_id["ANTHROPIC_USDT_Perp"]["underlying"] == "pre_market"
        assert by_id["OPENAI_USDT_Perp"]["underlying"] == "pre_market"
        assert by_id["BTC_USDT_Perp"]["first_capture"].endswith("Z")
        ids = {
            sd["id"]: parse_contract("grvt-perpetual", sd).internal_id
            for sd in by_id.values()
        }
        assert ids["KPEPE_USDT_Perp"] == "perpetual-KPEPE-USDT:USDT"
        assert ids["XAU_USDT_Perp"] == "perpetual-XAU-USDT:USDT"

    def test_listing_time_is_the_venue_create_time(self):
        from atlas.exchange_definitions.grvt import _ns_to_iso

        assert _ns_to_iso("1790754389133797880") == "2026-09-30T07:46:29.133Z"
        assert _ns_to_iso("0") is None
        assert _ns_to_iso("x") is None

    def test_unknown_asset_class_is_unknown(self):
        from atlas.exchange_definitions.grvt import grvt_underlying

        assert grvt_underlying("FOO", "SOMETHING_NEW") == UnderlyingType.unknown
        assert grvt_underlying("FOO", None) == UnderlyingType.unknown

    def test_instrument_without_base_and_quote_is_split(self):
        assert (
            parse_contract(
                "grvt-perpetual", {"id": "ETH_USDT_Perp", "type": "perpetual"}
            ).internal_id
            == "perpetual-ETH-USDT:USDT"
        )
        with pytest.raises(SkipSymbol):
            parse_contract(
                "grvt-perpetual", {"id": "BTC_USDT_Fut_20Oct23", "type": "perpetual"}
            )
