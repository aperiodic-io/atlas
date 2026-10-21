"""Atlas reads every venue from the venue's own API; no third-party listing feed remains.

Every registered exchange has an exchange-API fetcher, the updater has one
source, and parsers read the historical rows already in the snapshots from
their ids. Exchanges that only existed as parsers for an external feed's ids
(`kraken`, `huobi-dm`, `bitmex`, ...) are gone: their venues live on under the
API-backed slugs (`kraken-spot`, `htx-futures-coin`, ...), or, for closed
venues, not at all.
"""

from __future__ import annotations

import dataclasses
import importlib
import re
from pathlib import Path

import pytest

from atlas.exchanges import EXCHANGE_DEFINITIONS, ExchangeDefinition
from atlas.parsers import parse_contract
from atlas.update import EXCHANGES, parse_args

_ROOT = Path(__file__).resolve().parents[1]

REMOVED_EXCHANGES = [
    "bitfinex",
    "bitfinex-derivatives",
    "bitget",
    "bitget-futures",
    "bitmex",
    "bitstamp",
    "coinbase",
    "crypto-com",
    "cryptofacilities",
    "deribit",
    "ftx",
    "gate-io",
    "gate-io-futures",
    "gemini",
    "huobi",
    "huobi-dm",
    "huobi-dm-linear-swap",
    "huobi-dm-swap",
    "kraken",
    "kucoin",
    "phemex",
    "poloniex",
    "upbit",
]


class TestRegistry:
    def test_every_exchange_is_read_from_its_own_api(self):
        assert [e for e, d in EXCHANGE_DEFINITIONS.items() if d.exchange_fetcher is None] == []

    def test_definitions_carry_no_external_feed_mapping(self):
        fields = {f.name for f in dataclasses.fields(ExchangeDefinition)}
        assert fields.isdisjoint({"tardis_id", "on_tardis", "beta"})

    def test_a_fetcher_is_required(self):
        field = next(f for f in dataclasses.fields(ExchangeDefinition) if f.name == "exchange_fetcher")
        assert field.default is dataclasses.MISSING

    @pytest.mark.parametrize("exchange", REMOVED_EXCHANGES)
    def test_feed_named_exchanges_are_gone(self, exchange):
        assert exchange not in EXCHANGE_DEFINITIONS
        assert exchange not in EXCHANGES
        with pytest.raises(ValueError, match="Unknown exchange"):
            parse_contract(exchange, {"id": "BTCUSDT", "type": "spot"})

    def test_every_exchange_has_a_bundled_snapshot(self):
        missing = [e for e in EXCHANGE_DEFINITIONS if not (_ROOT / "atlas" / "data" / f"{e}.json").exists()]
        assert missing == []


class TestUpdater:
    def test_only_the_exchange_api_source_exists(self):
        sources = importlib.import_module("atlas.update_sources")
        assert not hasattr(sources, "TardisSymbolSource")
        assert not hasattr(sources, "HybridSymbolSource")
        assert {lc.name for lc in sources.SymbolLifecycle} == {"MERGE", "COMPLETE"}

    @pytest.mark.parametrize("module", ["atlas.utils", "atlas.exchange_ids"])
    def test_feed_client_modules_are_gone(self, module):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)

    def test_cli_has_no_source_choice(self):
        with pytest.raises(SystemExit):
            parse_args(["--source", "exchange"])


_SCANNED = [
    *sorted((_ROOT / "atlas").rglob("*.py")),
    *sorted((_ROOT / "integrations").rglob("*.py")),
    *sorted((_ROOT / ".github").rglob("*.y*ml")),
    *sorted((_ROOT / "docs").rglob("*.md")),
    _ROOT / "README.md",
    _ROOT / "pyproject.toml",
]
if (_ROOT / ".vscode" / "launch.json").exists():
    _SCANNED.append(_ROOT / ".vscode" / "launch.json")


@pytest.mark.parametrize("path", _SCANNED, ids=lambda p: str(p.relative_to(_ROOT)))
def test_no_reference_to_the_external_feed_remains(path):
    hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search("tardis", line, re.IGNORECASE)
    ]
    assert hits == []
