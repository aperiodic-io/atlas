"""Orderly (orderly-perpetual): parser, fetcher and asset classes, on the venue's
recorded /v1/public/info response of 2026-10-14 (tests/data/orderly/public_info.json)."""

import json
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.contracts import ContractType
from atlas.exchange_definitions import is_beta_exchange
from atlas.exchange_definitions.orderly import fetch_orderly_perps, orderly_underlying
from atlas.exchanges import get_allowed_types, get_exchange_fetcher
from atlas.parsers import SkipSymbol, parse_contract
from atlas.types import UnderlyingType
from atlas.update import EXCHANGES

EX = "orderly-perpetual"
RECORDED = json.loads(
    (Path(__file__).parent / "data/orderly/public_info.json").read_text()
)


def _response(payload):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _fetch(payload=RECORDED):
    with patch(
        "atlas.exchange_definitions.orderly.requests.get",
        return_value=_response(payload),
    ) as get:
        symbols = fetch_orderly_perps(timeout_seconds=5)
    get.assert_called_once()
    assert get.call_args.args[0] == "https://api.orderly.org/v1/public/info"
    return symbols


class TestOrderlyRegistration:
    def test_registered_for_the_exchange_api_update(self):
        # Not beta: the daily update would otherwise skip it, and Tardis has no Orderly.
        assert EX in EXCHANGES
        assert is_beta_exchange(EX) is False
        assert get_exchange_fetcher(EX) is fetch_orderly_perps
        assert get_allowed_types(EX) == {"perpetual"}


class TestParseOrderly:
    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("PERP_BTC_USDC", "perpetual-BTC-USDC:USDC"),
            ("PERP_1000PEPE_USDC", "perpetual-1000PEPE-USDC:USDC"),
            ("PERP_EURUSD_USDC", "perpetual-EURUSD-USDC:USDC"),
            # A broker's own market keeps the broker, so it cannot collide with a
            # main-book market of the same base (the producer publishes the same ID).
            ("PERP_AAPL_USDC_mythos", "perpetual-MYTHOS:AAPL-USDC:USDC"),
            ("PERP_ALPIX_USDC_alpix", "perpetual-ALPIX:ALPIX-USDC:USDC"),
        ],
    )
    def test_internal_ids(self, sid, internal_id):
        c = parse_contract(
            EX,
            {
                "id": sid,
                "type": "perpetual",
                "margin_asset": "USDC",
                "contract_size": 1.0,
            },
        )
        assert c.internal_id == internal_id
        assert c.instrument_type == ContractType.perpetual
        assert c.contract_size == 1.0

    @pytest.mark.parametrize(
        "sid",
        ["SPOT_BTC_USDC", "PERP_BTC", "PERP__USDC", "PERP_A_USDC_b_c", "PERP_A_USDC_"],
    )
    def test_non_perpetual_names_are_skipped(self, sid):
        with pytest.raises(SkipSymbol):
            parse_contract(EX, {"id": sid, "type": "perpetual"})


class TestFetchOrderlyPerps:
    def test_recorded_market_list(self):
        symbols = _fetch()
        assert len(symbols) == 140
        ids = [parse_contract(EX, sd).internal_id for sd in symbols]
        assert len(set(ids)) == 140
        btc = next(sd for sd in symbols if sd["id"] == "PERP_BTC_USDC")
        assert btc == {
            "id": "PERP_BTC_USDC",
            "type": "perpetual",
            "contract_size": 1.0,
            "margin_asset": "USDC",
            "underlying": "crypto",
            "first_capture": btc["first_capture"],
        }
        # created_time (ms) is the listing date.
        bonk = next(sd for sd in symbols if sd["id"] == "PERP_1000BONK_USDC")
        assert bonk["first_capture"] == "2024-05-31T06:00:14.451Z"

    def test_underlying_breakdown_of_the_recorded_list(self):
        # Pins the classification of every live market of 2026-10-14: a change in
        # the tables shows up here as a changed count.
        counts = Counter(sd["underlying"] for sd in _fetch())
        assert counts == {
            "crypto": 84,
            "equity": 40,
            "commodity": 6,
            "index": 3,
            "forex": 2,
            "pre_market": 3,
            "unknown": 2,  # UNITREE, ZHIPU: listing status unverified
        }

    def test_failed_response_is_retried_then_raised(self):
        bad = _response({"success": False, "code": -1000})
        with (
            patch(
                "atlas.exchange_definitions.orderly.requests.get", return_value=bad
            ) as get,
            pytest.raises(ValueError),
        ):
            fetch_orderly_perps(timeout_seconds=5)
        assert get.call_count == 3

    def test_unnamed_rows_are_dropped(self):
        payload = {
            "success": True,
            "data": {
                "rows": [
                    {"symbol": "SPOT_BTC_USDC"},
                    {"symbol": None},
                    {"symbol": "PERP_ETH_USDC", "created_time": None},
                ]
            },
        }
        assert _fetch(payload) == [
            {
                "id": "PERP_ETH_USDC",
                "type": "perpetual",
                "contract_size": 1.0,
                "margin_asset": "USDC",
                "underlying": "crypto",
            }
        ]


class TestOrderlyUnderlying:
    @pytest.mark.parametrize(
        ("sid", "pretge", "want"),
        [
            ("PERP_BTC_USDC", False, UnderlyingType.crypto),
            ("PERP_SPX_USDC", False, UnderlyingType.crypto),  # SPX6900, a meme coin
            ("PERP_SPX500_USDC", False, UnderlyingType.index),
            ("PERP_NAS100_USDC", False, UnderlyingType.index),
            ("PERP_XAU_USDC", False, UnderlyingType.commodity),
            ("PERP_CL_USDC", False, UnderlyingType.commodity),
            ("PERP_NATGAS_USDC_mythos", False, UnderlyingType.commodity),
            ("PERP_EURUSD_USDC", False, UnderlyingType.forex),
            ("PERP_USDJPY_USDC", False, UnderlyingType.forex),
            ("PERP_TSLA_USDC", False, UnderlyingType.equity),
            ("PERP_AAPL_USDC_mythos", False, UnderlyingType.equity),
            ("PERP_QQQ_USDC_mythos", False, UnderlyingType.equity),
            ("PERP_SPCX_USDC_mythos", False, UnderlyingType.pre_market),
            ("PERP_ALPIX_USDC_alpix", True, UnderlyingType.pre_market),
            ("PERP_PAXG_USDC_mythos", False, UnderlyingType.crypto),
            ("PERP_XLM_USDC_mythos", False, UnderlyingType.crypto),
            # A broker market the tables do not know is not guessed.
            ("PERP_NEWCO_USDC_mythos", False, UnderlyingType.unknown),
            ("SPOT_BTC_USDC", False, UnderlyingType.unknown),
        ],
    )
    def test_classes(self, sid, pretge, want):
        assert orderly_underlying(sid, pretge) == want
