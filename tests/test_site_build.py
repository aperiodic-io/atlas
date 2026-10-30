"""The GitHub Pages site build (``site/build.py``) on a small fixture snapshot."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

BUILD_PATH = Path(__file__).resolve().parents[1] / "site" / "build.py"


def _load_build():
    spec = importlib.util.spec_from_file_location("atlas_site_build", BUILD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build = _load_build()
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    _write(
        data / "hyperliquid-perps.json",
        [
            {"id": "xyz:TSLA", "type": "perpetual", "symbol": "XYZ:TSLA", "internal_id": "perpetual-XYZ:TSLA-USDC:USDC",
             "underlying": "equity", "first_capture": "2026-01-01T00:00:00Z", "base_asset_id": "TSLA",
             "unified_id": "perpetual-TSLA-USDC:USDC"},
            {"id": "flx:TSLA", "type": "perpetual", "symbol": "FLX:TSLA", "internal_id": "perpetual-FLX:TSLA-USDH:USDH",
             "underlying": "equity", "first_capture": "2025-11-01T00:00:00Z", "end_date": "2026-02-01T00:00:00Z",
             "base_asset_id": "TSLA"},
            {"id": "BTC", "type": "perpetual", "symbol": "BTC", "internal_id": "perpetual-BTC-USDC:USDC",
             "underlying": "crypto", "first_capture": "2024-01-01T00:00:00Z", "base_asset_id": "BTC"},
            {"id": "xyz:GOLD", "type": "perpetual", "symbol": "XYZ:GOLD", "internal_id": "perpetual-XYZ:GOLD-USDC:USDC",
             "underlying": "commodity", "first_capture": "2026-01-01T00:00:00Z", "base_asset_id": "XAU"},
        ],
    )
    # A venue symbology has not resolved yet: rows carry no asset IDs.
    _write(
        data / "lighter-perps.json",
        [
            {"id": "112", "type": "perpetual", "symbol": "TSLA", "internal_id": "perpetual-TSLA-USDC:USDC",
             "underlying": "equity", "first_capture": "2026-04-17T00:00:00Z"},
            {"id": "96", "type": "perpetual", "symbol": "EUR", "internal_id": "perpetual-EUR-USDC:USDC",
             "underlying": "forex", "first_capture": "2026-04-17T00:00:00Z"},
            {"id": "0", "type": "perpetual", "symbol": "ETH", "internal_id": "perpetual-ETH-USDC:USDC",
             "underlying": "crypto", "first_capture": "2026-04-17T00:00:00Z"},
        ],
    )
    # No declared underlying: the class comes from the resolved base asset.
    _write(
        data / "binance-spot.json",
        [
            {"id": "BTCUSDT", "type": "spot", "symbol": "BTC", "internal_id": "spot-BTC-USDT",
             "first_capture": "2019-01-01T00:00:00Z", "base_asset_id": "BTC"},
            {"id": "OLDUSDT", "type": "spot", "symbol": "OLD", "internal_id": "spot-OLD-USDT",
             "first_capture": "2019-01-01T00:00:00Z", "end_date": "2020-01-01T00:00:00Z"},
            {"type": "spot"},
        ],
    )
    _write(data / "cmc_mappings.json", {"schema_version": 1, "mappings": []})
    _write(
        data / "symbology" / "assets.json",
        {
            "schema_version": 1,
            "assets": [
                {"asset_id": "TSLA", "ticker": "TSLA", "name": "Tesla (Derivatives)", "asset_class": "equity",
                 "cmc_id": 38152, "cmc_slug": "tesla-derivatives", "categories": ["Tokenized Stock"]},
                {"asset_id": "XAU", "ticker": "XAU", "name": "Gold (Derivatives)", "asset_class": "commodity",
                 "aliases": ["GOLD"]},
                {"asset_id": "BTC", "ticker": "BTC", "name": "Bitcoin", "asset_class": "crypto", "cmc_id": 1},
            ],
        },
    )
    return data


@pytest.fixture
def site(tmp_path: Path, data_dir: Path) -> tuple[Path, dict]:
    out = tmp_path / "_site"
    stats = build.build(data_dir, out, base_url="https://example.test/atlas", now=NOW)
    return out, stats


def test_stats_count_instruments_and_rwa_first(site):
    _, stats = site
    assert stats["instruments"] == 9  # the row without an id is skipped
    assert stats["delisted"] == 2
    assert stats["rwa"] == 5  # three TSLA rows, gold and the euro
    assert stats["rwa"] == sum(stats["classes"][c] for c in ("equity", "commodity", "forex", "index", "pre_market"))
    assert stats["classes"]["crypto"] == 3  # BTCUSDT spot is crypto through its BTC asset
    assert stats["classes"]["unknown"] == 1  # OLD: no underlying, no asset
    assert stats["venues"] == 3
    assert stats["dex_venues"] == 2
    assert stats["first_year"] == 2019
    assert list(stats["classes"]) == list(build.ASSET_CLASSES)
    # Venues with the most live RWA contracts lead.
    assert [v["slug"] for v in stats["venue_stats"]][:2] == ["hyperliquid-perps", "lighter-perps"]


def test_catalogue_is_columnar_and_complete(site):
    out, _ = site
    payload = json.loads((out / "catalog" / "instruments.json").read_text())
    n = payload["count"]
    for column in ("v", "c", "t", "id", "iid", "sym", "a", "uid", "fc", "ed"):
        assert len(payload[column]) == n, column
    rows = {payload["id"][i]: i for i in range(n)}
    lighter = rows["112"]
    assert payload["venues"][payload["v"][lighter]]["slug"] == "lighter-perps"
    assert payload["classes"][payload["c"][lighter]] == "equity"
    assert payload["a"][lighter] == ""  # unresolved, not invented
    old = rows["OLDUSDT"]
    assert payload["ed"][old] == (datetime(2020, 1, 1).date() - build._EPOCH).days
    assert payload["ed"][rows["BTC"]] == 0
    # Live rows come first.
    assert all(payload["ed"][i] == 0 for i in range(n - 2))


def test_assets_payload_strips_derivatives_suffix(site):
    out, _ = site
    assets = {a["i"]: a for a in json.loads((out / "catalog" / "assets.json").read_text())["assets"]}
    assert assets["TSLA"]["n"] == "Tesla"
    assert assets["TSLA"]["s"] == "tesla-derivatives"
    assert assets["XAU"]["l"] == ["GOLD"]


def test_cross_venue_example_uses_real_rows(site):
    _, stats = site
    example = stats["example"]
    assert example["asset_id"] == "TSLA"
    natives = {(r["venue"], r["native_id"]) for r in example["rows"]}
    assert ("hyperliquid-perps", "xyz:TSLA") in natives  # the live dex, not the delisted one
    assert ("lighter-perps", "112") in natives  # matched by ticker before symbology resolves it
    assert example["unified_id_example"] == "perpetual-TSLA-USDC:USDC"


def test_pages_are_rendered_and_link_the_app(site):
    out, _stats = site
    index = (out / "index.html").read_text()
    assert '<script src="app.js" defer></script>' in index
    assert "{{" not in index
    assert '"@type": "Dataset"' in index
    assert "https://example.test/atlas/" in index
    assert "xyz:TSLA" in index  # server-rendered example table
    assert "<noscript>" in index
    assert "venues/lighter-perps.html" in index
    for name in ("app.js", "style.css", "favicon.svg", "robots.txt", "sitemap.xml", ".nojekyll", "files.html"):
        assert (out / name).exists(), name
    venue_page = (out / "venues" / "lighter-perps.html").read_text()
    assert "{{" not in venue_page
    assert "perpetual-EUR-USDC:USDC" in venue_page
    sitemap = (out / "sitemap.xml").read_text()
    assert "https://example.test/atlas/venues/hyperliquid-perps.html" in sitemap
    assert "Sitemap: https://example.test/atlas/sitemap.xml" in (out / "robots.txt").read_text()


def test_raw_snapshots_published_at_data_and_legacy_root(site, data_dir):
    out, _ = site
    for relative in ("lighter-perps.json", "cmc_mappings.json", "symbology/assets.json"):
        original = (data_dir / relative).read_bytes()
        assert (out / "data" / relative).read_bytes() == original
        assert (out / relative).read_bytes() == original
    assert 'href="data/symbology/assets.json"' in (out / "files.html").read_text()


def test_rebuild_replaces_stale_output(tmp_path, data_dir):
    out = tmp_path / "_site"
    out.mkdir()
    (out / "stale.json").write_text("{}")
    build.build(data_dir, out, now=NOW)
    assert not (out / "stale.json").exists()


def test_cli(tmp_path, data_dir, capsys):
    out = tmp_path / "cli_site"
    assert build.main(["--data", str(data_dir), "--out", str(out)]) == 0
    assert "9 instruments" in capsys.readouterr().out
    assert (out / "index.html").exists()


def test_forex_pair_assets_are_fx():
    assert build._REGISTRY_CLASS["forex"] == "forex"
