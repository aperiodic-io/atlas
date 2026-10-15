"""dYdX v4, GRVT and Nado: parsers and exchange-API fetchers on recorded payloads.

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
    to_tardis_exchange_id,
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


@pytest.mark.parametrize("slug", ["dydx-perpetual", "grvt-perpetual", "nado-perpetual"])
def test_registered_as_stable_perpetual_venues_with_api_fetchers(slug):
    # A beta exchange is skipped by the daily update; these are fetched from the
    # venue (hybrid with Tardis where Tardis covers the venue).
    assert not is_beta_exchange(slug)
    assert slug in EXCHANGES
    assert get_allowed_types(slug) == {"perpetual"}
    assert get_exchange_fetcher(slug) is not None


def test_dydx_reads_tardis_dydx_v4_history():
    assert to_tardis_exchange_id("dydx-perpetual") == "dydx-v4"


# ---------------------------------------------------------------------------
# dYdX v4
# ---------------------------------------------------------------------------


class TestDydx:
    def _fetch(self, payload=None):
        from atlas.exchange_definitions.dydx import fetch_dydx_perps

        payload = payload or _payload("dydx_perpetual_markets.json")
        with patch(
            "atlas.exchange_definitions.dydx.requests.get",
            return_value=_response(payload),
        ):
            return fetch_dydx_perps(timeout_seconds=5)

    def test_markets_settle_in_usdc_not_the_usd_display_quote(self):
        c = parse_contract("dydx-perpetual", {"id": "BTC-USD", "type": "perpetual"})
        assert c.internal_id == "perpetual-BTC-USDC:USDC"

    def test_non_usd_quote_is_folded_into_the_symbol(self):
        c = parse_contract("dydx-perpetual", {"id": "EUR-GBP", "type": "perpetual"})
        assert c.internal_id == "perpetual-EURGBP-USDC:USDC"

    def test_permissionless_tickers_keep_their_full_base(self):
        sid = "CIG,RAYDIUM,EXRQAUXS967FDKYMNXO4EQZEQIHEUBSTBCXPXXWPUMP-USD"
        c = parse_contract("dydx-perpetual", {"id": sid, "type": "perpetual"})
        assert c.internal_id == f"perpetual-{sid[:-4]}-USDC:USDC"

    def test_malformed_ticker_is_skipped(self):
        with pytest.raises(SkipSymbol):
            parse_contract("dydx-perpetual", {"id": "BTC", "type": "perpetual"})

    def test_fetch_lists_every_market_with_its_underlying(self):
        symbols = self._fetch()
        by_id = {sd["id"]: sd for sd in symbols}
        # Delisted markets are kept: the hybrid update dates them from Tardis.
        assert "MATIC-USD" in by_id
        assert {sd["type"] for sd in symbols} == {"perpetual"}
        assert all(
            sd["margin_asset"] == "USDC" and sd["contract_size"] == 1.0
            for sd in symbols
        )
        assert by_id["WTI-USD"]["underlying"] == "commodity"
        assert by_id["XAG-USD"]["underlying"] == "commodity"
        assert by_id["EUR-USD"]["underlying"] == "forex"
        # Gold tokens are crypto assets, as Binance classifies them.
        assert by_id["PAXG-USD"]["underlying"] == "crypto"
        assert by_id["BTC-USD"]["underlying"] == "crypto"
        ids = [parse_contract("dydx-perpetual", sd).internal_id for sd in symbols]
        assert "perpetual-WTI-USDC:USDC" in ids

    @pytest.mark.parametrize(
        ("base", "want"),
        [
            ("WTI", "commodity"),
            ("XAU", "commodity"),
            ("TRY", "forex"),
            ("EUR", "forex"),
            ("TRUMPWIN", "unknown"),
            ("EURC", "crypto"),
            ("TSLAX", "crypto"),
            ("BTC", "crypto"),
        ],
    )
    def test_underlying_table(self, base, want):
        from atlas.exchange_definitions.dydx import dydx_underlying

        assert dydx_underlying(base).value == want

    def test_retries_a_malformed_response(self):
        from atlas.exchange_definitions.dydx import fetch_dydx_perps

        bad = _response({"oops": True})
        good = _response(
            {"markets": {"BTC-USD": {"ticker": "BTC-USD", "status": "ACTIVE"}}}
        )
        with patch(
            "atlas.exchange_definitions.dydx.requests.get", side_effect=[bad, good]
        ) as get:
            assert [sd["id"] for sd in fetch_dydx_perps(timeout_seconds=5)] == [
                "BTC-USD"
            ]
        assert get.call_count == 2


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


# ---------------------------------------------------------------------------
# Nado
# ---------------------------------------------------------------------------


class TestNado:
    def _fetch(self):
        from atlas.exchange_definitions.nado import fetch_nado_perps

        def get(url, **kwargs):
            assert kwargs["headers"] == {"Accept-Encoding": "gzip"}
            return _response(
                {
                    "https://gateway.prod.nado.xyz/v1/symbols": _payload(
                        "nado_symbols.json"
                    ),
                    "https://gateway.prod.nado.xyz/v2/pairs": _payload(
                        "nado_pairs.json"
                    ),
                    "https://archive.prod.nado.xyz/v2/symbols": _payload(
                        "nado_archive_symbols.json"
                    ),
                }[url]
            )

        with patch("atlas.exchange_definitions.nado.requests.get", side_effect=get):
            return fetch_nado_perps(timeout_seconds=5)

    def test_products_are_addressed_by_id_and_settle_in_usdt0(self):
        sd = {
            "id": "2",
            "type": "perpetual",
            "listing": "BTC-PERP",
            "margin_asset": "USDT0",
        }
        assert (
            parse_contract("nado-perpetual", sd).internal_id
            == "perpetual-BTC-USDT0:USDT0"
        )

    @pytest.mark.parametrize(
        ("listing", "want"),
        [
            ("EURUSD-PERP", "perpetual-EUR-USDT0:USDT0"),
            ("USDJPY-PERP", "perpetual-USDJPY-USDT0:USDT0"),
            ("kPEPE-PERP", "perpetual-KPEPE-USDT0:USDT0"),
            ("XAUT-PERP", "perpetual-XAUT-USDT0:USDT0"),
        ],
    )
    def test_symbols(self, listing, want):
        sd = {"id": "1", "type": "perpetual", "listing": listing}
        assert parse_contract("nado-perpetual", sd).internal_id == want

    def test_product_without_a_perp_listing_is_skipped(self):
        with pytest.raises(SkipSymbol):
            parse_contract(
                "nado-perpetual", {"id": "1", "type": "perpetual", "listing": "KBTC"}
            )

    def test_fetch_keeps_tradable_perps_and_classifies_rwa(self):
        by_listing = {sd["listing"]: sd for sd in self._fetch()}
        assert "ADA-PERP" not in by_listing, "not_tradable"
        assert "ARB-PERP" in by_listing, "post_only still trades"
        assert all(sd["margin_asset"] == "USDT0" for sd in by_listing.values())
        want = {
            "BTC-PERP": "crypto",
            "XAUT-PERP": "crypto",
            "kPEPE-PERP": "crypto",
            "WTI-PERP": "commodity",
            "EURUSD-PERP": "forex",
            "GBPUSD-PERP": "forex",
            "USDJPY-PERP": "forex",
            "QQQ-PERP": "equity",
            "AAPL-PERP": "equity",
            "TSLA-PERP": "equity",
            "SPCX-PERP": "pre_market",
            # Traditional-asset price feed, not in the tables: not guessed.
            "ANSEM-PERP": "unknown",
        }
        assert {k: by_listing[k]["underlying"] for k in want} == want
        assert by_listing["CASHCAT-PERP"]["underlying"] == "unknown"
        assert [sd["id"] for sd in self._fetch()] == sorted(
            (sd["id"] for sd in self._fetch()), key=int
        )
