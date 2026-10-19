"""Aster perpetuals: parser, fetcher and classification over a recorded payload.

``tests/data/aster_exchangeinfo.json`` is a slice of the live
``https://fapi.asterdex.com/fapi/v1/exchangeInfo`` response of 2026-10-14
(filters dropped): crypto perps quoted in USDT, USD1 and Aster's ``U``, stock,
ETF, commodity and pre-launch perps, a delisting (``SETTLING``) and a not yet
listed (``PENDING_TRADING``) contract.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from atlas.exchange_definitions import is_beta_exchange
from atlas.exchange_definitions.aster import aster_symbols, fetch_aster_perps
from atlas.exchanges import get_allowed_types, get_exchange_fetcher, to_tardis_exchange_id
from atlas.parsers import SkipSymbol, parse_contract
from atlas.update import EXCHANGES

PAYLOAD = json.loads((Path(__file__).parent / "data" / "aster_exchangeinfo.json").read_text(encoding="utf-8"))
EXCHANGE = "aster-perpetual"


def _by_id() -> dict[str, dict]:
    return {sd["id"]: sd for sd in aster_symbols(PAYLOAD)}


def test_registered_for_the_daily_update_with_its_own_fetcher():
    # The update skips beta exchanges; Aster's API is its only full source, so
    # it must be maintained (not beta) and refreshed by the daily job.
    assert EXCHANGE in EXCHANGES
    assert not is_beta_exchange(EXCHANGE)
    assert get_exchange_fetcher(EXCHANGE) is fetch_aster_perps
    assert get_allowed_types(EXCHANGE) == {"perpetual"}
    # Tardis lists Aster as aster-futures (same lower-cased ids).
    assert to_tardis_exchange_id(EXCHANGE) == "aster-futures"


def test_fetcher_keeps_every_perpetual_and_drops_unlisted_ones():
    rows = _by_id()
    # PENDING_TRADING contracts have no contract type yet (MBLUSDT).
    assert "mblusdt" not in rows
    assert {"btcusdt", "tonusdt", "aaplusdt", "btcu"} <= set(rows)
    assert all(sd["type"] == "perpetual" and sd["contract_size"] == 1.0 for sd in rows.values())


@pytest.mark.parametrize(
    ("sid", "internal_id"),
    [
        # Must equal the producer's ID(IDSchemeAtlas): base-quote:settle.
        ("btcusdt", "perpetual-BTC-USDT:USDT"),
        ("btcusd1", "perpetual-BTC-USD1:USD1"),
        ("btcu", "perpetual-BTC-U:U"),
        ("aaplusdt", "perpetual-AAPL-USDT:USDT"),
        ("xauusd1", "perpetual-XAU-USD1:USD1"),
        ("1000shibusdt", "perpetual-1000SHIB-USDT:USDT"),
        ("skhynixusdt", "perpetual-SKHYNIX-USDT:USDT"),
        ("openaiusdt", "perpetual-OPENAI-USDT:USDT"),
    ],
)
def test_parses_to_producer_atlas_ids(sid, internal_id):
    contract = parse_contract(EXCHANGE, _by_id()[sid])
    assert contract.internal_id == internal_id
    assert contract.original_id == sid
    assert contract.contract_size == 1.0


@pytest.mark.parametrize(
    ("sid", "underlying"),
    [
        ("btcusdt", "crypto"),
        ("btcu", "crypto"),
        ("aaplusdt", "equity"),
        ("skhynixusdt", "equity"),
        ("qqqusdt", "equity"),  # STOCK + ETF
        ("spyusdt", "equity"),  # ETF
        ("ewyusdt", "equity"),  # ETF
        ("xauusdt", "commodity"),
        ("xauusd1", "commodity"),
        ("paxgusdt", "commodity"),  # Aster tags the gold token Commodities
        ("openaiusdt", "pre_market"),
        ("polymarketusd1", "pre_market"),  # pre-launch on a symbolType 0 row
    ],
)
def test_underlying_comes_from_aster_tags(sid, underlying):
    assert _by_id()[sid]["underlying"] == underlying


def test_unknown_classification_is_not_guessed():
    from atlas.exchange_definitions.aster import classify_aster_underlying

    assert classify_aster_underlying({"symbolType": 1, "underlyingSubType": ["Index"]}).value == "unknown"
    assert classify_aster_underlying({"symbolType": 0, "underlyingType": "INDEX"}).value == "unknown"


def test_availability_bounds_are_left_to_tardis():
    # first_capture/end_date are Tardis's capture window (the equivalence test
    # compares them); Aster's onboardDate/deliveryDate are listing times.
    assert all("first_capture" not in sd and "end_date" not in sd for sd in _by_id().values())


def test_tardis_only_rows_parse_without_asset_fields():
    for sid, internal_id in [
        ("btcusdt", "perpetual-BTC-USDT:USDT"),
        ("btcusd1", "perpetual-BTC-USD1:USD1"),
        ("btcu", "perpetual-BTC-U:U"),
    ]:
        assert parse_contract(EXCHANGE, {"id": sid, "type": "perpetual"}).internal_id == internal_id
    with pytest.raises(SkipSymbol):
        parse_contract(EXCHANGE, {"id": "???", "type": "perpetual"})


def _response(payload):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def test_fetch_retries_transient_failures():
    malformed = _response({"code": -1})  # no symbols list
    with patch(
        "atlas.exchange_definitions.aster.requests.get",
        side_effect=[requests.ConnectionError("reset"), malformed, _response(PAYLOAD)],
    ) as get, patch("time.sleep"):
        rows = fetch_aster_perps(timeout_seconds=5)
    assert get.call_count == 3
    assert len(rows) == len(aster_symbols(PAYLOAD))


def test_fetch_gives_up_after_three_attempts():
    with patch(
        "atlas.exchange_definitions.aster.requests.get",
        side_effect=requests.ConnectionError("down"),
    ), patch("time.sleep"), pytest.raises(requests.ConnectionError):
        fetch_aster_perps(timeout_seconds=5)
