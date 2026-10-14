"""Hibachi perpetuals: parser, fetcher, classification and the API-only update path.

``tests/data/hibachi_inventory.json`` is the live
``https://data-api.hibachi.xyz/market/inventory`` response of 2026-10-14 (every
listed market plus two delisted ``CLOSED`` ones): crypto, FX majors, JPY/CAD
quoted in USD, and XAG, which Hibachi files under FX but tags ``commodity``.
"""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from atlas.exchange_definitions import is_beta_exchange
from atlas.exchange_definitions.hibachi import fetch_hibachi_perps, hibachi_symbols
from atlas.exchanges import get_allowed_types, get_exchange_fetcher, is_on_tardis
from atlas.parsers import SkipSymbol, parse_contract
from atlas.update import EXCHANGES
from atlas.update_sources import HybridSymbolSource, TardisSymbolSource

PAYLOAD = json.loads((Path(__file__).parent / "data" / "hibachi_inventory.json").read_text())
EXCHANGE = "hibachi-perpetual"


def _by_id() -> dict[str, dict]:
    return {sd["id"]: sd for sd in hibachi_symbols(PAYLOAD)}


def test_registered_as_an_api_only_exchange():
    assert EXCHANGE in EXCHANGES
    assert not is_beta_exchange(EXCHANGE)
    assert get_exchange_fetcher(EXCHANGE) is fetch_hibachi_perps
    assert get_allowed_types(EXCHANGE) == {"perpetual"}
    # Tardis does not list Hibachi.
    assert not is_on_tardis(EXCHANGE)


def test_fetcher_keeps_listed_markets_only():
    rows = _by_id()
    assert len(rows) == 15
    assert "FARTCOIN/USDT-P" not in rows  # CLOSED: delisted
    assert all(sd["type"] == "perpetual" and sd["contract_size"] == 1.0 for sd in rows.values())


@pytest.mark.parametrize(
    ("sid", "internal_id"),
    [
        # Must equal the producer's ID(IDSchemeAtlas): underlying-settle:settle.
        ("BTC/USDT-P", "perpetual-BTC-USDT:USDT"),
        ("EUR/USDT-P", "perpetual-EUR-USDT:USDT"),
        ("JPY/USDT-P", "perpetual-JPY-USDT:USDT"),
        ("XAG/USDT-P", "perpetual-XAG-USDT:USDT"),
        ("PAXG/USDT-P", "perpetual-PAXG-USDT:USDT"),
    ],
)
def test_parses_to_producer_atlas_ids(sid, internal_id):
    contract = parse_contract(EXCHANGE, _by_id()[sid])
    assert contract.internal_id == internal_id
    assert contract.original_id == sid


@pytest.mark.parametrize(
    ("sid", "underlying"),
    [
        ("BTC/USDT-P", "crypto"),
        ("HYPE/USDT-P", "crypto"),
        ("PAXG/USDT-P", "crypto"),  # Hibachi lists the gold token under CRYPTO
        ("EUR/USDT-P", "forex"),
        ("JPY/USDT-P", "forex"),
        ("CAD/USDT-P", "forex"),
        ("XAG/USDT-P", "commodity"),  # category FX, but tagged commodity
    ],
)
def test_underlying_comes_from_hibachi_metadata(sid, underlying):
    assert _by_id()[sid]["underlying"] == underlying


def test_unknown_category_is_not_guessed():
    from atlas.exchange_definitions.hibachi import classify_hibachi_underlying

    assert classify_hibachi_underlying({"category": "INDEX"}, []).value == "unknown"


def test_rows_without_assets_are_skipped():
    with pytest.raises(SkipSymbol):
        parse_contract(EXCHANGE, {"id": "BTC/USDT-P", "type": "perpetual"})


def _response(payload):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def test_fetch_retries_transient_failures():
    with patch(
        "atlas.exchange_definitions.hibachi.requests.get",
        side_effect=[requests.Timeout("slow"), _response({"status": "failed"}), _response(PAYLOAD)],
    ) as get, patch("time.sleep"):
        rows = fetch_hibachi_perps(timeout_seconds=5)
    assert get.call_count == 3
    assert len(rows) == 15


def test_tardis_source_skips_api_only_exchanges(tmp_path, monkeypatch):
    # A Tardis refresh must not replace the snapshot with Tardis's empty
    # listing of a venue it does not cover.
    updater = importlib.import_module("atlas.update")
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    path = tmp_path / f"{EXCHANGE}.json"
    path.write_text(json.dumps([{"id": "BTC/USDT-P", "type": "perpetual"}]))
    source = TardisSymbolSource()
    with patch.object(TardisSymbolSource, "fetch_exchange", side_effect=AssertionError("Tardis asked")):
        assert updater.update([EXCHANGE], source) == []
    assert json.loads(path.read_text()) == [{"id": "BTC/USDT-P", "type": "perpetual"}]


def test_hybrid_source_reads_api_only_exchanges_from_the_api():
    source = HybridSymbolSource()
    with patch.object(TardisSymbolSource, "fetch_exchange", side_effect=AssertionError("Tardis asked")), patch(
        "atlas.exchange_definitions.hibachi.requests.get", return_value=_response(PAYLOAD)
    ):
        data = source.fetch_exchange(EXCHANGE)
    assert len(data["availableSymbols"]) == 15


def test_new_api_only_rows_get_a_stable_first_capture(tmp_path, monkeypatch):
    # Rows without a first_capture are invisible to SecurityMaster; with no
    # Tardis bounds the first observation is the start, kept on later updates.
    updater = importlib.import_module("atlas.update")
    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    source = SimpleNamespace(fetch_exchange=lambda _e: {"availableSymbols": hibachi_symbols(PAYLOAD)})
    assert updater.update([EXCHANGE], source) == []
    rows = json.loads((tmp_path / f"{EXCHANGE}.json").read_text())
    first = {r["id"]: r["first_capture"] for r in rows}
    assert len(first) == 15
    assert all(first.values())
    assert updater.update([EXCHANGE], source) == []
    again = {r["id"]: r["first_capture"] for r in json.loads((tmp_path / f"{EXCHANGE}.json").read_text())}
    assert again == first
