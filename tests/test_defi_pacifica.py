"""Pacifica: parser, exchange-API fetcher (recorded live payload from
2026-10-14) and the update path with ``--source exchange``.

The IDs asserted here are the ones the live producer publishes under its
Atlas scheme (live-producers ``streaming/feeds/pacifica``):
symbol = the venue's base asset, denominator and margin = the settlement
token, so producer and securities master agree on every live contract.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.contracts import ContractType
from atlas.exchange_definitions import is_beta_exchange
from atlas.exchange_definitions.pacifica import (
    fetch_pacifica_perps,
    pacifica_contract_symbol,
    pacifica_underlying,
)
from atlas.exchanges import get_allowed_types, get_exchange_fetcher
from atlas.parsers import SkipSymbol, parse_contract
from atlas.types import UnderlyingType
from atlas.update_sources import ExchangeApiSymbolSource

FIXTURES = Path(__file__).parent / "fixtures" / "defi"


def _payload(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _response(payload) -> MagicMock:
    r = MagicMock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


def _ids(exchange: str, symbols: list[dict]) -> dict[str, str]:
    return {sd["id"]: parse_contract(exchange, sd).internal_id for sd in symbols}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registered_as_stable_perpetual_api_venue() -> None:
    # Beta exchanges are skipped by atlas.update, so this must not be beta.
    exchange = "pacifica-perpetual"
    assert is_beta_exchange(exchange) is False
    assert get_allowed_types(exchange) == {"perpetual"}
    assert get_exchange_fetcher(exchange) is not None
    from atlas.update import EXCHANGES

    assert exchange in EXCHANGES


# ---------------------------------------------------------------------------
# Pacifica
# ---------------------------------------------------------------------------


def _pacifica_symbols() -> list[dict]:
    with patch(
        "atlas.exchange_definitions.pacifica.requests.get",
        return_value=_response(_payload("pacifica_info.json")),
    ):
        return fetch_pacifica_perps(timeout_seconds=5)


def test_pacifica_fetches_every_perpetual_but_not_spot() -> None:
    symbols = _pacifica_symbols()
    assert len(symbols) == 77
    assert "SOL-USDC" not in {sd["id"] for sd in symbols}
    assert all(sd["type"] == "perpetual" for sd in symbols)
    assert all(
        sd["contract_size"] == 1.0 and sd["margin_asset"] == "USDC" for sd in symbols
    )


def test_pacifica_ids_carry_the_usdc_settlement_and_fx_rule() -> None:
    ids = _ids("pacifica-perpetual", _pacifica_symbols())
    assert ids["BTC"] == "perpetual-BTC-USDC:USDC"
    assert ids["kBONK"] == "perpetual-KBONK-USDC:USDC"
    assert ids["XAU"] == "perpetual-XAU-USDC:USDC"
    # A USD-quoted FX pair publishes its base; any other pair keeps both legs.
    assert ids["EURUSD"] == "perpetual-EUR-USDC:USDC"
    assert ids["USDJPY"] == "perpetual-USDJPY-USDC:USDC"
    assert len(set(ids.values())) == len(ids)


def test_pacifica_underlying_comes_from_the_venue_categories() -> None:
    by_id = {sd["id"]: sd["underlying"] for sd in _pacifica_symbols()}
    assert by_id["BTC"] == "crypto"
    assert by_id["kBONK"] == "crypto"
    assert by_id["TSLA"] == "equity"
    assert by_id["SKHYNIX"] == "equity"
    assert by_id["XAU"] == "commodity"
    assert by_id["CL"] == "commodity"
    assert by_id["PAXG"] == "commodity"
    assert by_id["EURUSD"] == "forex"
    assert by_id["USDJPY"] == "forex"
    # Pacifica files the S&P 500 perp under Equities; its underlying is the index.
    assert by_id["SP500"] == "index"
    counts = Counter(by_id.values())
    assert counts["unknown"] == 0
    assert counts == Counter(
        {"crypto": 52, "equity": 14, "commodity": 8, "forex": 2, "index": 1}
    )


def test_pacifica_unlisted_rwa_market_is_unknown_not_crypto() -> None:
    # A market missing from the venue table is crypto, unless the venue trades
    # it through its RWA request-for-quote mode.
    assert pacifica_underlying("NEWCOIN", ["orderbook"]) == UnderlyingType.crypto
    assert (
        pacifica_underlying("NEWSTOCK", ["orderbook", "rfq"]) == UnderlyingType.unknown
    )
    assert pacifica_underlying("ANTHROPIC") == UnderlyingType.pre_market


@pytest.mark.parametrize(
    ("base", "symbol"),
    [
        ("EURUSD", "EUR"),
        ("GBPUSD", "GBP"),
        ("USDJPY", "USDJPY"),
        ("EURGBP", "EURGBP"),
        ("USDUSD", "USDUSD"),
        ("COPPER", "COPPER"),
        ("ABCUSD", "ABCUSD"),
        ("kBONK", "kBONK"),
    ],
)
def test_pacifica_contract_symbol_normalizes_only_fx_pairs(
    base: str, symbol: str
) -> None:
    assert pacifica_contract_symbol(base) == symbol


def test_pacifica_listing_time_is_first_capture() -> None:
    by_id = {sd["id"]: sd for sd in _pacifica_symbols()}
    assert by_id["BTC"]["first_capture"] == "2025-06-02T16:22:13.944Z"


def test_pacifica_parser_without_fetcher_fields() -> None:
    c = parse_contract("pacifica-perpetual", {"id": "ETH", "type": "perpetual"})
    assert c.internal_id == "perpetual-ETH-USDC:USDC"
    assert c.instrument_type == ContractType.perpetual
    with pytest.raises(SkipSymbol):
        parse_contract("pacifica-perpetual", {"id": "SOL-USDC", "type": "spot"})


def test_pacifica_fetch_retries_a_failed_response() -> None:
    bad = _response({"success": False, "data": None})
    good = _response(_payload("pacifica_info.json"))
    with patch(
        "atlas.exchange_definitions.pacifica.requests.get", side_effect=[bad, good]
    ) as get:
        assert len(fetch_pacifica_perps(timeout_seconds=5)) == 77
    assert get.call_count == 2


# ---------------------------------------------------------------------------
# The update path (--source exchange)
# ---------------------------------------------------------------------------


def test_update_writes_snapshots_from_the_exchange_source(
    tmp_path, monkeypatch
) -> None:
    import atlas.update as updater

    monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
    payloads = {
        "https://api.pacifica.fi/api/v1/info": _payload("pacifica_info.json"),
    }

    def get(url, timeout):
        return _response(payloads[url])

    exchanges = ["pacifica-perpetual"]
    with patch("requests.get", side_effect=get):
        assert updater.update(exchanges, ExchangeApiSymbolSource()) == []

    counts = {}
    for exchange in exchanges:
        rows = json.loads((tmp_path / f"{exchange}.json").read_text())
        counts[exchange] = len(rows)
        for row in rows:
            assert row["internal_id"] == parse_contract(exchange, row).internal_id
            assert row["underlying"] in {u.value for u in UnderlyingType}
    assert counts == {
        "pacifica-perpetual": 77,
    }
