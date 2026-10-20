"""Integrity checks over the bundled symbology: the registry and the snapshots.

These run against ``atlas/data`` as committed, so a symbology build that leaves
the data inconsistent -- an asset ID no registry entry backs, a ``unified_id``
that disagrees with its legs, a famous instrument mapped to the wrong asset --
fails CI rather than shipping.
"""

from __future__ import annotations

import copy
import json
from collections import Counter, defaultdict
from datetime import datetime

import pytest

from atlas import ASSET_CATEGORIES, AssetClass, SecurityMaster
from atlas.assets import DEFAULT_REGISTRY_PATH, AssetRegistry, is_valid_asset_id
from integrations.symbology import (
    DEFAULT_DATA_DIR,
    DEFAULT_OVERRIDES_PATH,
    SYMBOLOGY_EXCHANGES,
    Match,
    build_symbology,
    load_overrides,
    load_snapshots,
    load_ticker_eras,
    load_venue_tickers,
)

# Share of *active* rows whose base asset must be identified, per snapshot, set
# a little below the coverage the first build reached. Bitget spot lists
# thousands of tokenized stocks CMC does not track, Gate spot hundreds of
# leveraged tokens, Hyperliquid spot mostly HIP-1 tokens CMC has never listed,
# and Hyperliquid's HIP-3 dexes many pre-IPO and index markets.
ACTIVE_BASE_COVERAGE_FLOORS = {
    "binance-spot": 0.98,
    "binance-futures": 0.98,
    "binance-futures-cm": 0.98,
    "bybit-spot": 0.97,
    "bybit-perps-usd": 0.97,
    "bybit-perps-coin": 0.97,
    "bybit-futures": 0.97,
    "okx-spot": 0.92,
    "okx-perps": 0.97,
    "okx-futures": 0.97,
    "hyperliquid-perps": 0.92,
    "hyperliquid-spot": 0.25,
    # Bitget spot is mostly tokenized stocks CMC does not track.
    "bitget-spot": 0.16,
    "bitget-perps-usdt": 0.95,
    "bitget-perps-usdc": 0.95,
    "bitget-perps-coin": 0.95,
    "gateio-spot": 0.72,
    "gateio-perps-usdt": 0.95,
    "gateio-futures-usdt": 0.95,
    "coinbase-spot": 0.97,
}


@pytest.fixture(scope="module")
def registry() -> AssetRegistry:
    return AssetRegistry.load(DEFAULT_REGISTRY_PATH)


@pytest.fixture(scope="module")
def snapshots() -> dict[str, list[dict]]:
    return load_snapshots(DEFAULT_DATA_DIR, SYMBOLOGY_EXCHANGES)


@pytest.fixture(scope="module")
def sm() -> SecurityMaster:
    return SecurityMaster.load()


def _rows(snapshots):
    for exchange, rows in snapshots.items():
        for row in rows:
            yield exchange, row


def test_every_symbology_snapshot_is_bundled(snapshots):
    assert set(snapshots) == set(SYMBOLOGY_EXCHANGES)


def test_registry_ids_are_valid_and_unique(registry):
    assert len(registry) > 1000
    ids = [asset.asset_id for asset in registry]
    assert len(ids) == len(set(ids))
    assert all(is_valid_asset_id(asset_id) for asset_id in ids)
    cmc_ids = [asset.cmc_id for asset in registry if asset.cmc_id is not None]
    assert len(cmc_ids) == len(set(cmc_ids))


def test_registry_categories_are_atlas_categories(registry):
    used = Counter(category for asset in registry for category in asset.categories)
    assert set(used) <= set(ASSET_CATEGORIES)
    # The promoted CMC categories, Memes included, are actually populated.
    for category in ("Memes", "Layer 1", "DeFi", "AI Agents"):
        assert used[category] > 0, category


def test_rows_reference_registered_assets_consistently(snapshots, registry):
    problems = []
    for exchange, row in _rows(snapshots):
        base_id = row.get("base_asset_id")
        if base_id is None:
            if row.get("base_asset_match") or row.get("base_multiplier"):
                problems.append((exchange, row["id"], "base fields without a base asset"))
            continue
        asset = registry.get(base_id)
        if asset is None:
            problems.append((exchange, row["id"], f"unknown base {base_id}"))
            continue
        if row.get("cmc_id") != asset.cmc_id:
            problems.append((exchange, row["id"], f"cmc_id {row.get('cmc_id')} != {asset.cmc_id}"))
        if (row.get("category") or []) != list(asset.categories):
            problems.append((exchange, row["id"], "category differs from the asset's"))
        if row.get("base_asset_match") not in {m.value for m in Match}:
            problems.append((exchange, row["id"], f"bad match {row.get('base_asset_match')}"))
        for key in ("quote_asset_id", "margin_asset_id"):
            if (value := row.get(key)) is not None and registry.get(value) is None:
                problems.append((exchange, row["id"], f"unknown {key} {value}"))
        for segment in row.get("base_asset_history", []):
            if registry.get(segment["asset_id"]) is None:
                problems.append((exchange, row["id"], "unknown history asset"))
    assert problems == []


def test_unified_ids_agree_with_their_legs(snapshots):
    problems = []
    for exchange, row in _rows(snapshots):
        unified = row.get("unified_id")
        if unified is None:
            continue
        expected = f"{row['type']}-{row['base_asset_id']}-{row['quote_asset_id']}"
        if row["type"] != "spot":
            expected += f":{row['margin_asset_id']}"
        if row.get("delivery_date"):
            expected += "-" + datetime.fromisoformat(row["delivery_date"]).strftime("%Y%m%d")
        if unified != expected:
            problems.append((exchange, row["id"], unified, expected))
    assert problems == []


def test_rows_with_a_cmc_id_keep_a_category_list(snapshots):
    """The pre-symbology contract: ``category`` sits beside ``cmc_id``, even when empty."""
    missing = [
        (exchange, row["id"])
        for exchange, row in _rows(snapshots)
        if "cmc_id" in row and not isinstance(row.get("category"), list)
    ]
    assert missing == []


def test_derivatives_margin_and_spot_has_none(snapshots):
    for exchange, row in _rows(snapshots):
        if row.get("type") == "spot":
            assert "margin_asset_id" not in row, (exchange, row["id"])


@pytest.mark.parametrize("exchange", sorted(ACTIVE_BASE_COVERAGE_FLOORS))
def test_active_base_coverage(snapshots, exchange):
    active = [row for row in snapshots[exchange] if not row.get("end_date")]
    resolved = [row for row in active if row.get("base_asset_id")]
    coverage = len(resolved) / len(active)
    assert coverage >= ACTIVE_BASE_COVERAGE_FLOORS[exchange], f"{exchange}: {coverage:.3f}"


def test_quote_coverage(snapshots):
    rows = [row for _, row in _rows(snapshots)]
    resolved = [row for row in rows if row.get("quote_asset_id")]
    assert len(resolved) / len(rows) >= 0.97


@pytest.mark.parametrize(
    ("exchange", "original_id", "base", "quote", "margin", "unified"),
    [
        ("binance-spot", "btcusdt", "BTC", "USDT", None, "spot-BTC-USDT"),
        ("binance-futures", "btcusdt", "BTC", "USDT", "USDT", "perpetual-BTC-USDT:USDT"),
        ("binance-futures-cm", "btcusd_perp", "BTC", "USD", "BTC", "perpetual-BTC-USD:BTC"),
        ("bybit-spot", "BTCUSDT", "BTC", "USDT", None, "spot-BTC-USDT"),
        ("bybit-perps-usd", "BTCUSDT", "BTC", "USDT", "USDT", "perpetual-BTC-USDT:USDT"),
        ("okx-spot", "BTC-USDT", "BTC", "USDT", None, "spot-BTC-USDT"),
        ("okx-perps", "BTC-USDT-SWAP", "BTC", "USDT", "USDT", "perpetual-BTC-USDT:USDT"),
        ("hyperliquid-perps", "BTC", "BTC", "USDC", "USDC", "perpetual-BTC-USDC:USDC"),
        ("coinbase-spot", "BTC-USD", "BTC", "USD", None, "spot-BTC-USD"),
        ("coinbase-spot", "ETH-EUR", "ETH", "EUR", None, "spot-ETH-EUR"),
        ("bitget-spot", "BTCUSDT", "BTC", "USDT", None, "spot-BTC-USDT"),
        ("bitget-perps-usdt", "BTCUSDT", "BTC", "USDT", "USDT", "perpetual-BTC-USDT:USDT"),
        ("bitget-perps-coin", "BTCUSD_CM", "BTC", "USD", "BTC", "perpetual-BTC-USD:BTC"),
        ("gateio-spot", "BTC_USDT", "BTC", "USDT", None, "spot-BTC-USDT"),
        ("gateio-perps-usdt", "BTC_USDT", "BTC", "USDT", "USDT", "perpetual-BTC-USDT:USDT"),
        ("gateio-perps-btc", "BTC_USD", "BTC", "USD", "BTC", "perpetual-BTC-USD:BTC"),
        ("binance-spot", "ethbtc", "ETH", "BTC", None, "spot-ETH-BTC"),
        # Parser split fixed by the venue's own base/quote fields.
        ("binance-spot", "arbidr", "ARB", "IDR", None, "spot-ARB-IDR"),
        # Multiplier contracts point at the underlying asset.
        ("binance-futures", "1000pepeusdt", "PEPE", "USDT", "USDT", "perpetual-PEPE-USDT:USDT"),
        ("hyperliquid-perps", "kPEPE", "PEPE", "USDC", "USDC", "perpetual-PEPE-USDC:USDC"),
    ],
)
def test_known_instruments(sm, exchange, original_id, base, quote, margin, unified):
    assert sm.base_asset(exchange, original_id).asset_id == base
    assert sm.quote_asset(exchange, original_id).asset_id == quote
    margin_asset = sm.margin_asset(exchange, original_id)
    assert (margin_asset.asset_id if margin_asset else None) == margin
    assert sm.unified_id(exchange, original_id) == unified


def test_multiplier_is_recorded(sm):
    assert sm.instrument_metadata("binance-futures", "1000pepeusdt")["base_multiplier"] == 1000
    assert sm.instrument_metadata("hyperliquid-perps", "kPEPE")["base_multiplier"] == 1000
    assert "base_multiplier" not in sm.instrument_metadata("binance-spot", "pepeusdt")


def test_known_asset_identities(sm):
    assert sm.asset("BTC").cmc_id == 1
    assert sm.asset("ETH").cmc_id == 1027
    assert sm.asset("USDT").cmc_id == 825
    assert sm.asset("USD").asset_class is AssetClass.fiat
    assert sm.asset("EUR").asset_class is AssetClass.fiat
    # Wrong legacy IDs corrected by CMC's own exchange listings.
    assert sm.base_asset("binance-futures", "apeusdt").cmc_id == 18876  # ApeCoin
    assert sm.base_asset("binance-futures", "avaaiusdt").cmc_id == 34116  # AVA (Solana)


def test_terra_ticker_eras(sm):
    # Instruments that stopped trading in the crash are Terra Classic.
    assert sm.base_asset("binance-futures-cm", "lunausd_perp").asset_id == "LUNC"
    assert sm.base_asset("binance-spot", "lunabtc").asset_id == "LUNC"
    assert sm.base_asset("binance-spot", "ustusdt").cmc_id == 7129
    # Instruments that traded across it changed asset under one ticker.
    for exchange, original_id in [("binance-spot", "lunausdt"), ("bybit-spot", "LUNAUSDT")]:
        assert sm.base_asset(exchange, original_id).asset_id == "LUNA"
        assert sm.base_asset(exchange, original_id, at=datetime(2022, 1, 1)).asset_id == "LUNC"
    assert sm.base_asset("binance-spot", "luncusdt").asset_id == "LUNC"


def test_tradfi_underlyings_share_one_asset_across_venues(sm):
    tesla = {
        sm.base_asset(exchange, original_id).asset_id
        for exchange, original_id in [
            ("binance-futures", "tslausdt"),
            ("bybit-perps-usd", "TSLAUSDT"),
            ("okx-perps", "TSLA-USDT-SWAP"),
            ("gateio-perps-usdt", "TSLA_USDT"),
            ("hyperliquid-perps", "xyz:TSLA"),
        ]
    }
    assert tesla == {"TSLA"}
    assert sm.asset("TSLA").asset_class is AssetClass.equity
    gold = sm.base_asset("binance-futures", "xauusdt")
    assert gold.asset_class is AssetClass.commodity


def test_unified_id_groups_a_contract_across_venues(sm):
    venues = {exchange for exchange, _ in sm.by_unified_id("perpetual-BTC-USDT:USDT")}
    assert {
        "binance-futures",
        "bybit-perps-usd",
        "okx-perps",
        "bitget-perps-usdt",
        "gateio-perps-usdt",
    } <= venues
    assert {"binance-spot", "bybit-spot", "okx-spot", "bitget-spot", "gateio-spot"} <= {
        exchange for exchange, _ in sm.by_unified_id("spot-BTC-USDT")
    }


def test_one_ticker_per_venue_means_one_asset_at_a_time(snapshots):
    """A venue lists one asset under one live ticker.

    The only exception tolerated is the one the build reports and leaves alone:
    two of a venue's snapshots, each consistent within itself, that CMC files
    under 1:1 twins (ethDYDX and dYdX) with neither outvoting the other.
    """
    by_ticker: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for exchange, row in _rows(snapshots):
        if row.get("base_asset_id") and row.get("symbol") and not row.get("end_date"):
            family = exchange if exchange.startswith("hyperliquid") else exchange.split("-")[0]
            by_ticker[(family, row["symbol"].upper())][exchange].add(row["base_asset_id"])
    clashes = []
    for key, snapshots_ in by_ticker.items():
        assets = set().union(*snapshots_.values())
        if len(assets) == 1:
            continue
        tie = (
            len(assets) == 2
            and all(len(ids) == 1 for ids in snapshots_.values())
            and len(snapshots_) == 2
        )
        if not tie:
            clashes.append((key, {e: sorted(ids) for e, ids in snapshots_.items()}))
    assert clashes == []


def test_offline_rebuild_is_a_fixpoint(snapshots, registry):
    """Rebuilding without CMC reproduces the committed data exactly, key order included."""
    rows = copy.deepcopy(snapshots)
    rebuilt_registry = AssetRegistry.load(DEFAULT_REGISTRY_PATH)
    ledger_path = DEFAULT_DATA_DIR / "cmc_mappings.json"
    from integrations.cmc_mappings import MappingStore
    from integrations.symbology import ledger_decisions

    report = build_symbology(
        rows,
        rebuilt_registry,
        ledger=ledger_decisions(MappingStore.load(ledger_path)),
        overrides=load_overrides(DEFAULT_OVERRIDES_PATH),
        ticker_eras=load_ticker_eras(DEFAULT_OVERRIDES_PATH),
        venue_tickers=load_venue_tickers(DEFAULT_OVERRIDES_PATH),
    )
    assert report.minted == []
    changed = [
        (exchange, before["id"])
        for exchange in rows
        for before, after in zip(snapshots[exchange], rows[exchange], strict=True)
        if json.dumps(before) != json.dumps(after)
    ]
    assert changed == []
    assert [a.to_json() for a in rebuilt_registry] == [a.to_json() for a in registry]
