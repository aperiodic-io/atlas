"""ApeX Omni perpetuals: parser, fetcher and classification over a recorded payload.

``tests/data/apex_symbols.json`` is a slice of the live
``https://omni.apex.exchange/api/v3/symbols`` response of 2026-10-14 (fields
trimmed): listed crypto perps, delisted (TON) and hidden-but-tradeable (IO,
STBL) ones, stock/commodity/index perps of the stock list, one with no category
(SOXL), disabled ones (IWM, KORU, a test contract) and prediction contracts.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from atlas.exchange_definitions.apex import apex_symbols, fetch_apex_perps
from atlas.exchanges import get_allowed_types, get_exchange_fetcher
from atlas.parsers import SkipSymbol, parse_contract
from atlas.update import EXCHANGES

PAYLOAD = json.loads((Path(__file__).parent / "data" / "apex_symbols.json").read_text(encoding="utf-8"))
EXCHANGE = "apex-perps"


def _by_id() -> dict[str, dict]:
    return {sd["id"]: sd for sd in apex_symbols(PAYLOAD)}


def test_registered_as_an_api_only_exchange():
    assert EXCHANGE in EXCHANGES
    assert get_exchange_fetcher(EXCHANGE) is fetch_apex_perps
    assert get_allowed_types(EXCHANGE) == {"perpetual"}


def test_fetcher_keeps_listed_perpetual_and_stock_contracts():
    # The stock list is where ApeX keeps its RWA perps (ccxt misses it).
    # Retired contracts (trading and display both off) and prediction contracts
    # are left out. Hidden contracts that still trade (IOUSDT, STBLUSDT) are not
    # streamed but are still listed: leaving them out would let the daily
    # update close them as delisted.
    assert set(_by_id()) == {
        "BTCUSDT", "ETHUSDT", "DOGEUSDT", "PAXGUSDT", "IOUSDT", "STBLUSDT",
        "AAPLUSDT", "TSLAUSDT", "XAUUSDT", "NATGASUSDT", "SPYUSDT", "QQQUSDT", "SOXLUSDT",
    }


@pytest.mark.parametrize(
    ("sid", "internal_id"),
    [
        ("BTCUSDT", "perpetual-BTC-USDT:USDT"),
        ("AAPLUSDT", "perpetual-AAPL-USDT:USDT"),
        ("XAUUSDT", "perpetual-XAU-USDT:USDT"),
        ("SPYUSDT", "perpetual-SPY-USDT:USDT"),
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
        ("BTCUSDT", "crypto"),
        ("DOGEUSDT", "crypto"),
        ("PAXGUSDT", "crypto"),  # in ApeX's crypto list
        ("AAPLUSDT", "equity"),
        ("TSLAUSDT", "equity"),
        ("XAUUSDT", "commodity"),
        ("NATGASUSDT", "commodity"),
        ("SPYUSDT", "index"),  # ApeX files the ETF under INDEX
        ("QQQUSDT", "index"),
        ("SOXLUSDT", "unknown"),  # stock list, no category
    ],
)
def test_underlying_comes_from_apex_lists_and_categories(sid, underlying):
    assert _by_id()[sid]["underlying"] == underlying


def test_rows_without_assets_are_skipped():
    with pytest.raises(SkipSymbol):
        parse_contract(EXCHANGE, {"id": "BTCUSDT", "type": "perpetual"})


def _response(payload):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def test_fetch_retries_transient_failures():
    with patch(
        "atlas.exchange_definitions.apex.requests.get",
        side_effect=[requests.ConnectionError("reset"), _response({"data": {}}), _response(PAYLOAD)],
    ) as get, patch("time.sleep"):
        rows = fetch_apex_perps(timeout_seconds=5)
    assert get.call_count == 3
    assert len(rows) == 13
