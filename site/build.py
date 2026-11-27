"""Build the Atlas GitHub Pages site: landing page, searchable catalogue, venue pages.

Standard library only, so the Pages workflow needs nothing but Python::

    python3 site/build.py --data atlas/data --out _site

Everything a visitor sees is rendered from the snapshots at build time, so the
page is crawlable and readable without JavaScript; ``app.js`` adds the search,
filters, asset drawer and theme toggle on top. The raw snapshot JSONs are
published too, at ``data/<file>`` and, for consumers of the earlier data-only
site, at their old root paths.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

SITE_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "https://aperiodic-io.github.io/atlas/"
REPO_URL = "https://github.com/aperiodic-io/atlas"
APERIODIC_URL = "https://aperiodic.io/atlas?utm_source=atlas-pages&utm_medium=site&utm_campaign=atlas"

# Real-world assets first, crypto second: this is the order classes appear in
# everywhere on the site.
ASSET_CLASSES: tuple[str, ...] = (
    "equity",
    "commodity",
    "forex",
    "index",
    "pre_market",
    "crypto",
    "unknown",
)
RWA_CLASSES = frozenset({"equity", "commodity", "forex", "index", "pre_market"})
CLASS_LABELS = {
    "equity": "Equities",
    "commodity": "Commodities",
    "forex": "FX",
    "index": "Indices",
    "pre_market": "Pre-IPO",
    "crypto": "Crypto",
    "unknown": "Unclassified",
}
CLASS_SINGULAR = {
    "equity": "Equity",
    "commodity": "Commodity",
    "forex": "FX",
    "index": "Index",
    "pre_market": "Pre-IPO",
    "crypto": "Crypto",
    "unknown": "Unclassified",
}
# An asset registry class, for rows whose venue declares no underlying.
_REGISTRY_CLASS = {
    "crypto": "crypto",
    "equity": "equity",
    "commodity": "commodity",
    "index": "index",
    "forex": "forex",
    "fiat": "forex",
}
TYPES: tuple[str, ...] = ("perpetual", "future", "spot", "option", "unknown")

# slug -> (venue, product, kind). Unlisted slugs get a name derived from the slug.
VENUES: dict[str, tuple[str, str, str]] = {
    "avantis-perps": ("Avantis", "Perpetuals", "DEX"),
    "lighter-perps": ("Lighter", "Perpetuals", "DEX"),
    "grvt-perps": ("GRVT", "Perpetuals", "DEX"),
    "aster-perps": ("Aster", "Perpetuals", "DEX"),
    "variational-perps": ("Variational", "Perpetuals", "DEX"),
    "extended-perps": ("Extended", "Perpetuals", "DEX"),
    "apex-perps": ("ApeX Omni", "Perpetuals", "DEX"),
    "pacifica-perps": ("Pacifica", "Perpetuals", "DEX"),
    "hyperliquid-perps": ("Hyperliquid", "Perpetuals + HIP-3", "DEX"),
    "hyperliquid-spot": ("Hyperliquid", "Spot", "DEX"),
    "binance-spot": ("Binance", "Spot", "CEX"),
    "binance-futures": ("Binance", "USD-M futures", "CEX"),
    "binance-futures-cm": ("Binance", "COIN-M futures", "CEX"),
    "bybit-spot": ("Bybit", "Spot", "CEX"),
    "bybit-perps-usd": ("Bybit", "Linear perpetuals", "CEX"),
    "bybit-perps-coin": ("Bybit", "Inverse perpetuals", "CEX"),
    "bybit-futures": ("Bybit", "Dated futures", "CEX"),
    "okx-spot": ("OKX", "Spot", "CEX"),
    "okx-perps": ("OKX", "Perpetuals", "CEX"),
    "okx-futures": ("OKX", "Dated futures", "CEX"),
    "bitget-spot": ("Bitget", "Spot", "CEX"),
    "bitget-perps-usdt": ("Bitget", "USDT perpetuals", "CEX"),
    "bitget-perps-usdc": ("Bitget", "USDC perpetuals", "CEX"),
    "bitget-perps-coin": ("Bitget", "Coin-M perpetuals", "CEX"),
    "bitget-futures-coin": ("Bitget", "Coin-M futures", "CEX"),
    "gateio-spot": ("Gate", "Spot", "CEX"),
    "gateio-perps-usdt": ("Gate", "USDT perpetuals", "CEX"),
    "gateio-perps-btc": ("Gate", "BTC perpetuals", "CEX"),
    "gateio-futures-usdt": ("Gate", "USDT futures", "CEX"),
    "htx-spot": ("HTX", "Spot", "CEX"),
    "htx-perps-usdt": ("HTX", "USDT perpetuals", "CEX"),
    "htx-perps-coin": ("HTX", "Coin-M perpetuals", "CEX"),
    "htx-futures-usdt": ("HTX", "USDT futures", "CEX"),
    "htx-futures-coin": ("HTX", "Coin-M futures", "CEX"),
    "mexc-perps": ("MEXC", "Perpetuals", "CEX"),
    "deribit-perps": ("Deribit", "Perpetuals", "CEX"),
    "deribit-futures": ("Deribit", "Dated futures", "CEX"),
    "whitebit-perps": ("WhiteBIT", "Perpetuals", "CEX"),
    "cryptocom-spot": ("Crypto.com", "Spot", "CEX"),
    "cryptocom-perps": ("Crypto.com", "Perpetuals", "CEX"),
    "cryptocom-futures": ("Crypto.com", "Dated futures", "CEX"),
    "coinbase-spot": ("Coinbase", "Spot", "CEX"),
    "kraken-spot": ("Kraken", "Spot", "CEX"),
    "kraken-perps": ("Kraken", "Perpetuals", "CEX"),
    "kraken-futures": ("Kraken", "Dated futures", "CEX"),
    "bitfinex-spot": ("Bitfinex", "Spot", "CEX"),
    "bitfinex-perps": ("Bitfinex", "Perpetuals", "CEX"),
    "bitstamp-spot": ("Bitstamp", "Spot", "CEX"),
    "bitstamp-perps": ("Bitstamp", "Perpetuals", "CEX"),
    "gemini-spot": ("Gemini", "Spot", "CEX"),
    "gemini-perps": ("Gemini", "Perpetuals", "CEX"),
    "kucoin-spot": ("KuCoin", "Spot", "CEX"),
    "phemex-spot": ("Phemex", "Spot", "CEX"),
    "phemex-perps": ("Phemex", "Perpetuals", "CEX"),
    "poloniex-spot": ("Poloniex", "Spot", "CEX"),
    "upbit-spot": ("Upbit", "Spot", "CEX"),
}

# Venues tried, in order, for the "same market, different names" table.
EXAMPLE_VENUES = (
    "hyperliquid-perps",
    "lighter-perps",
    "avantis-perps",
    "aster-perps",
    "grvt-perps",
    "variational-perps",
    "extended-perps",
    "pacifica-perps",
    "apex-perps",
    "binance-futures",
    "bybit-perps-usd",
    "okx-perps",
    "bitget-perps-usdt",
)
EXAMPLE_ROWS = 7
_EPOCH = date(1970, 1, 1)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_snapshots(data_dir: Path) -> dict[str, list[dict]]:
    """Every venue snapshot: a top-level ``<slug>.json`` holding a list of rows."""
    snapshots: dict[str, list[dict]] = {}
    for path in sorted(data_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, list):
            snapshots[path.stem] = [row for row in payload if isinstance(row, dict)]
    return snapshots


def load_assets(data_dir: Path) -> dict[str, dict]:
    path = data_dir / "symbology" / "assets.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("assets") if isinstance(payload, dict) else None
    return {
        row["asset_id"]: row
        for row in rows or []
        if isinstance(row, dict) and isinstance(row.get("asset_id"), str)
    }


def venue_meta(slug: str) -> tuple[str, str, str]:
    if slug in VENUES:
        return VENUES[slug]
    venue, _, product = slug.partition("-")
    return (venue.capitalize(), product.replace("-", " ").capitalize() or "Instruments", "CEX")


def venue_label(slug: str) -> str:
    venue, product, _ = venue_meta(slug)
    return f"{venue} {product.lower()}" if product == "Spot" else f"{venue} {product}"


# ---------------------------------------------------------------------------
# Normalising rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Instrument:
    venue: str
    native_id: str
    type: str
    asset_class: str
    internal_id: str
    symbol: str
    asset_id: str
    unified_id: str
    first_capture: date | None
    end_date: date | None

    @property
    def active(self) -> bool:
        return self.end_date is None

    @property
    def ticker(self) -> str:
        """The venue ticker without a HIP-3 ``dex:`` prefix."""
        return self.symbol.split(":", 1)[-1]


def parse_day(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def asset_class_of(row: Mapping[str, Any], assets: Mapping[str, dict]) -> str:
    """The venue's declared underlying, else the class of the resolved base asset."""
    underlying = row.get("underlying")
    if isinstance(underlying, str) and underlying:
        return underlying if underlying in ASSET_CLASSES else "unknown"
    asset = assets.get(row.get("base_asset_id") or "")
    if asset is not None:
        return _REGISTRY_CLASS.get(asset.get("asset_class"), "unknown")
    return "unknown"


def normalise(snapshots: Mapping[str, list[dict]], assets: Mapping[str, dict]) -> list[Instrument]:
    instruments: list[Instrument] = []
    for venue, rows in snapshots.items():
        for row in rows:
            native_id = row.get("id")
            if not isinstance(native_id, str) or not native_id:
                continue
            kind = row.get("type") if row.get("type") in TYPES else "unknown"
            instruments.append(
                Instrument(
                    venue=venue,
                    native_id=native_id,
                    type=kind,
                    asset_class=asset_class_of(row, assets),
                    internal_id=str(row.get("internal_id") or ""),
                    symbol=str(row.get("symbol") or ""),
                    asset_id=str(row.get("base_asset_id") or ""),
                    unified_id=str(row.get("unified_id") or ""),
                    first_capture=parse_day(row.get("first_capture")),
                    end_date=parse_day(row.get("end_date")),
                )
            )
    return instruments


def sanitize(ticker: str) -> str:
    return "".join(ch for ch in ticker.upper() if ch.isascii() and ch.isalnum())


def display_name(asset: Mapping[str, Any] | None, fallback: str) -> str:
    if not asset:
        return fallback
    return re.sub(r"\s*\(Derivatives\)$", "", str(asset.get("name") or fallback))


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


@dataclass
class VenueStats:
    slug: str
    total: int = 0
    active: int = 0
    delisted: int = 0
    resolved: int = 0
    classes: Counter = field(default_factory=Counter)
    active_classes: Counter = field(default_factory=Counter)
    first_year: int | None = None

    @property
    def rwa_active(self) -> int:
        return sum(self.active_classes[c] for c in RWA_CLASSES)

    @property
    def rwa_total(self) -> int:
        return sum(self.classes[c] for c in RWA_CLASSES)

    def to_json(self) -> dict[str, Any]:
        venue, product, kind = venue_meta(self.slug)
        return {
            "slug": self.slug,
            "venue": venue,
            "product": product,
            "kind": kind,
            "total": self.total,
            "active": self.active,
            "delisted": self.delisted,
            "resolved": self.resolved,
            "rwa_total": self.rwa_total,
            "rwa_active": self.rwa_active,
            "first_year": self.first_year,
            "classes": {c: self.classes[c] for c in ASSET_CLASSES if self.classes[c]},
            "active_classes": {
                c: self.active_classes[c] for c in ASSET_CLASSES if self.active_classes[c]
            },
        }


def venue_stats(instruments: Iterable[Instrument]) -> list[VenueStats]:
    by_venue: dict[str, VenueStats] = {}
    for item in instruments:
        stats = by_venue.setdefault(item.venue, VenueStats(item.venue))
        stats.total += 1
        stats.classes[item.asset_class] += 1
        if item.active:
            stats.active += 1
            stats.active_classes[item.asset_class] += 1
        else:
            stats.delisted += 1
        if item.asset_id:
            stats.resolved += 1
        if item.first_capture is not None:
            year = item.first_capture.year
            stats.first_year = year if stats.first_year is None else min(stats.first_year, year)
    # RWA-first: the venues listing the most live real-world-asset contracts lead.
    return sorted(
        by_venue.values(), key=lambda s: (-s.rwa_active, -s.active, s.slug)
    )


def asset_key(item: Instrument) -> str:
    """Atlas's asset ID where symbology resolved one, else ``~<TICKER>``."""
    return item.asset_id or f"~{sanitize(item.ticker)}"


def top_assets(
    instruments: Iterable[Instrument], assets: Mapping[str, dict], per_class: int = 8
) -> dict[str, list[dict[str, Any]]]:
    """The assets of each class listed live on the most venues.

    A resolved row counts for its Atlas asset; an RWA row symbology has not
    resolved yet counts for that asset when exactly one Atlas asset of the
    class carries its ticker, and otherwise for its bare ticker.
    """
    # Venues disagree on the class of one name (OpenAI is pre-IPO on one venue
    # and an equity on the next), so any non-crypto registry asset may own it.
    by_ticker: dict[str, list[str]] = defaultdict(list)
    for asset_id, asset in assets.items():
        if asset.get("asset_class") not in {"equity", "commodity", "index", "forex", "fiat", "unknown"}:
            continue
        for ticker in {sanitize(t) for t in (asset.get("ticker"), *(asset.get("aliases") or ())) if isinstance(t, str)}:
            by_ticker[ticker].append(asset_id)

    venues: dict[tuple[str, str], set[str]] = defaultdict(set)
    listings: Counter = Counter()
    for item in instruments:
        if not item.active or item.asset_class == "unknown":
            continue
        key = item.asset_id
        if not key:
            if item.asset_class not in RWA_CLASSES:
                continue
            owners = by_ticker.get(sanitize(item.ticker), [])
            key = owners[0] if len(owners) == 1 else f"~{sanitize(item.ticker)}"
        venues[(item.asset_class, key)].add(venue_meta(item.venue)[0])
        listings[(item.asset_class, key)] += 1

    result: dict[str, list[dict[str, Any]]] = {}
    for klass in ASSET_CLASSES:
        ranked = sorted(
            ((key, names) for (c, key), names in venues.items() if c == klass),
            key=lambda pair: (-len(pair[1]), -listings[(klass, pair[0])], pair[0]),
        )[:per_class]
        result[klass] = [
            {
                "key": key,
                "asset_id": "" if key.startswith("~") else key,
                "label": key.lstrip("~") if key.startswith("~") else str(assets.get(key, {}).get("ticker") or key),
                "name": display_name(assets.get(key), key.lstrip("~")),
                "venues": len(names),
                "listings": listings[(klass, key)],
            }
            for key, names in ranked
        ]
    return result


def compute_stats(
    instruments: list[Instrument],
    assets: Mapping[str, dict],
    generated_at: datetime,
) -> dict[str, Any]:
    venues = venue_stats(instruments)
    classes = Counter(i.asset_class for i in instruments)
    active_classes = Counter(i.asset_class for i in instruments if i.active)
    years = [i.first_capture.year for i in instruments if i.first_capture]
    rwa_assets = sum(
        1 for a in assets.values() if a.get("asset_class") in {"equity", "commodity", "index"}
    )
    return {
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instruments": len(instruments),
        "active": sum(1 for i in instruments if i.active),
        "delisted": sum(1 for i in instruments if not i.active),
        "rwa": sum(classes[c] for c in RWA_CLASSES),
        "rwa_active": sum(active_classes[c] for c in RWA_CLASSES),
        "venues": len(venues),
        "dex_venues": sum(1 for v in venues if venue_meta(v.slug)[2] == "DEX"),
        "venue_groups": len({venue_meta(v.slug)[0] for v in venues}),
        "assets": len(assets),
        "rwa_assets": rwa_assets,
        "resolved": sum(1 for i in instruments if i.asset_id),
        "first_year": min(years) if years else None,
        "classes": {c: classes[c] for c in ASSET_CLASSES},
        "active_classes": {c: active_classes[c] for c in ASSET_CLASSES},
        "venue_stats": [v.to_json() for v in venues],
        "top_assets": top_assets(instruments, assets),
    }


# ---------------------------------------------------------------------------
# Worked examples, picked from the data
# ---------------------------------------------------------------------------


def _pick(instruments: Iterable[Instrument], venue: str, predicate) -> Instrument | None:
    candidates = [i for i in instruments if i.venue == venue and predicate(i)]
    # A live, undecorated listing reads best: `xyz:TSLA` over a delisted dex.
    candidates.sort(key=lambda i: (not i.active, ":" in i.symbol and not i.symbol.startswith("xyz:"), i.native_id))
    return candidates[0] if candidates else None


def cross_venue_example(
    instruments: list[Instrument], assets: Mapping[str, dict], ticker: str = "TSLA"
) -> dict[str, Any] | None:
    """One RWA contract under its native name on each venue, and its Atlas IDs."""

    def matches(item: Instrument, wanted: str) -> bool:
        return item.type == "perpetual" and (
            item.asset_id == wanted
            or (not item.asset_id and sanitize(item.ticker) == wanted and item.asset_class in RWA_CLASSES)
        )

    wanted = ticker
    if not any(matches(i, wanted) for i in instruments):
        top = compute_top_equity(instruments, assets)
        if top is None:
            return None
        wanted = top
    rows = []
    for venue in EXAMPLE_VENUES:
        item = _pick(instruments, venue, lambda i: matches(i, wanted))
        if item is not None:
            rows.append(item)
        if len(rows) == EXAMPLE_ROWS:
            break
    if len(rows) < 2:
        return None
    unified = next((i.unified_id for i in rows if i.unified_id), "")
    asset = assets.get(wanted)
    return {
        "asset_id": wanted if asset else "",
        "ticker": wanted,
        "name": display_name(asset, wanted),
        "asset_class": next((i.asset_class for i in rows), "equity"),
        "unified_id_example": unified,
        "venues": len({i.venue for i in instruments if matches(i, wanted) and i.active}),
        "rows": [
            {
                "venue": i.venue,
                "label": venue_label(i.venue),
                "native_id": i.native_id,
                "internal_id": i.internal_id,
                "asset_id": i.asset_id,
                "unified_id": i.unified_id,
            }
            for i in rows
        ],
    }


def compute_top_equity(instruments: list[Instrument], assets: Mapping[str, dict]) -> str | None:
    counts: dict[str, set[str]] = defaultdict(set)
    for item in instruments:
        if item.active and item.asset_class == "equity" and item.type == "perpetual":
            counts[item.asset_id or sanitize(item.ticker)].add(item.venue)
    if not counts:
        return None
    return max(sorted(counts), key=lambda key: len(counts[key]))


def _example_rank(venue: str) -> int:
    return EXAMPLE_VENUES.index(venue) if venue in EXAMPLE_VENUES else len(EXAMPLE_VENUES)


def ticker_examples(instruments: list[Instrument], preferred: str = "TSLA") -> dict[str, Any]:
    """Small facts the "why it matters" cards quote."""

    def live(predicate) -> list[Instrument]:
        return [i for i in instruments if i.active and i.type != "spot" and predicate(i)]

    spx_coin = live(lambda i: sanitize(i.ticker) == "SPX" and i.asset_class == "crypto")
    spx_index = live(lambda i: i.asset_class == "index" and sanitize(i.ticker) in {"SP500", "SPX500", "USA500", "US500"})
    gold = live(lambda i: i.asset_class == "commodity" and (i.asset_id == "XAU" or sanitize(i.ticker) in {"XAU", "GOLD"}))
    gold_names = sorted({i.ticker for i in gold})
    eur = live(lambda i: i.asset_class == "forex" and sanitize(i.ticker) == "EUR")
    pre_ipo = live(lambda i: i.asset_class == "pre_market")
    pre_ipo_names = Counter(sanitize(i.ticker) for i in pre_ipo)
    avantis = [i for i in instruments if i.venue == "avantis-perps" and i.active]
    delisted_rwa = [i for i in instruments if not i.active and i.asset_class in RWA_CLASSES]
    return {
        "spx_coin_venues": len({i.venue for i in spx_coin}),
        "spx_index_venues": len({i.venue for i in spx_index}),
        "gold_names": gold_names[:6],
        "gold_venues": len({i.venue for i in gold}),
        "eur_ids": [
            {"venue": venue_meta(i.venue)[0], "native_id": i.native_id}
            for i in sorted(eur, key=lambda i: (_example_rank(i.venue), i.venue))[:5]
        ],
        "pre_ipo_names": [name for name, _ in pre_ipo_names.most_common(5)],
        "pre_ipo_live": len(pre_ipo),
        "avantis_example": next(
            (
                {"native_id": i.native_id, "internal_id": i.internal_id}
                for i in sorted(
                    avantis,
                    key=lambda i: (
                        i.asset_id != preferred and sanitize(i.ticker) != preferred,
                        i.asset_class != "equity",
                        int(i.native_id) if i.native_id.isdigit() else 0,
                    ),
                )
            ),
            None,
        ),
        "delisted_rwa": len(delisted_rwa),
    }


# ---------------------------------------------------------------------------
# Catalogue payloads
# ---------------------------------------------------------------------------


def _days(value: date | None) -> int:
    return 0 if value is None else (value - _EPOCH).days


def catalogue_payload(
    instruments: list[Instrument], venue_order: list[str], generated_at: datetime
) -> dict[str, Any]:
    """Columnar instrument list: small, and it gzips well.

    ``fc``/``ed`` are days since 1970-01-01 (0 = unknown / still listed);
    ``uid`` is empty when symbology has not resolved the row.
    """
    venue_index = {slug: n for n, slug in enumerate(venue_order)}
    class_index = {c: n for n, c in enumerate(ASSET_CLASSES)}
    type_index = {t: n for n, t in enumerate(TYPES)}
    # The browsing order: live first, real-world assets first, and within a
    # class the assets listed on the most venues, each asset's listings together.
    spread: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in instruments:
        if item.active:
            spread[(item.asset_class, asset_key(item))].add(item.venue)
    ordered = sorted(
        instruments,
        key=lambda i: (
            not i.active,
            class_index[i.asset_class],
            -len(spread.get((i.asset_class, asset_key(i)), ())),
            asset_key(i),
            type_index[i.type],
            venue_index[i.venue],
            i.native_id,
        ),
    )
    return {
        "schema": 1,
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(ordered),
        "venues": [
            {"slug": slug, "venue": venue_meta(slug)[0], "product": venue_meta(slug)[1], "kind": venue_meta(slug)[2]}
            for slug in venue_order
        ],
        "classes": list(ASSET_CLASSES),
        "types": list(TYPES),
        "v": [venue_index[i.venue] for i in ordered],
        "c": [class_index[i.asset_class] for i in ordered],
        "t": [type_index[i.type] for i in ordered],
        "id": [i.native_id for i in ordered],
        "iid": [i.internal_id for i in ordered],
        "sym": [i.symbol for i in ordered],
        "a": [i.asset_id for i in ordered],
        "uid": [i.unified_id for i in ordered],
        "fc": [_days(i.first_capture) for i in ordered],
        "ed": [_days(i.end_date) for i in ordered],
    }


def assets_payload(assets: Mapping[str, dict]) -> dict[str, Any]:
    rows = []
    for asset_id in sorted(assets):
        asset = assets[asset_id]
        row = {
            "i": asset_id,
            "t": asset.get("ticker") or asset_id,
            "n": display_name(asset, asset_id),
            "c": _REGISTRY_CLASS.get(asset.get("asset_class"), "unknown"),
        }
        if asset.get("cmc_slug"):
            row["s"] = asset["cmc_slug"]
        if asset.get("categories"):
            row["g"] = asset["categories"]
        if asset.get("aliases"):
            row["l"] = asset["aliases"]
        rows.append(row)
    return {"schema": 1, "assets": rows}


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def fmt(number: int) -> str:
    return f"{number:,}"


def class_bar(counts: Mapping[str, int], label: str) -> str:
    total = sum(counts.values())
    if not total:
        return '<div class="bar" role="img" aria-label="no instruments"></div>'
    parts = []
    described = []
    for klass in ASSET_CLASSES:
        n = counts.get(klass, 0)
        if not n:
            continue
        parts.append(
            f'<span class="bar-seg cls-{klass}" style="flex-grow:{n}" title="{esc(CLASS_LABELS[klass])}: {fmt(n)}"></span>'
        )
        described.append(f"{CLASS_LABELS[klass]} {fmt(n)}")
    return (
        f'<div class="bar" role="img" aria-label="{esc(label)}: {esc(", ".join(described))}">'
        + "".join(parts)
        + "</div>"
    )


def class_legend(counts: Mapping[str, int], limit: int = 4) -> str:
    items = [
        (klass, counts.get(klass, 0)) for klass in ASSET_CLASSES if counts.get(klass, 0)
    ]
    rwa_first = [p for p in items if p[0] in RWA_CLASSES] + [p for p in items if p[0] not in RWA_CLASSES]
    shown = rwa_first[:limit]
    rest = sum(n for _, n in rwa_first[limit:])
    html_items = "".join(
        f'<li><span class="dot cls-{k}"></span>{esc(CLASS_LABELS[k])} <b>{fmt(n)}</b></li>' for k, n in shown
    )
    if rest:
        html_items += f'<li class="muted">+{fmt(rest)} other</li>'
    return f'<ul class="legend">{html_items}</ul>'


def render_stats(stats: Mapping[str, Any]) -> str:
    cells = [
        (fmt(stats["rwa_active"]), "live RWA contracts", "equities, commodities, FX, indices, pre-IPO"),
        (fmt(stats["instruments"]), "instruments", f"across {stats['venue_groups']} venues, {stats['dex_venues']} of them DEX snapshots"),
        (fmt(stats["delisted"]), "delisted, with end dates", "so backtests keep the dead"),
        (fmt(stats["assets"]), "Atlas assets", f"{fmt(stats['rwa_assets'])} of them real-world references"),
        ("Daily", "refresh, committed to git", "every venue read from its own API"),
    ]
    return "".join(
        f'<div class="stat"><div class="stat-value">{esc(v)}</div><div class="stat-label">{esc(label)}</div><div class="stat-note">{esc(note)}</div></div>'
        for v, label, note in cells
    )


def render_example(example: Mapping[str, Any] | None) -> str:
    if not example:
        return '<p class="muted">No cross-venue example in this build.</p>'
    rows = "".join(
        "<tr>"
        f'<td>{esc(r["label"])}</td>'
        f'<td><code>{esc(r["native_id"])}</code></td>'
        f'<td><code>{esc(r["internal_id"])}</code></td>'
        "</tr>"
        for r in example["rows"]
    )
    asset_cell = (
        f'<button type="button" class="asset-chip" data-asset="{esc(example["asset_id"])}">{esc(example["asset_id"])}</button>'
        if example["asset_id"]
        else f'<code>{esc(example["ticker"])}</code>'
    )
    unified = (
        f'<tr class="atlas-row"><td>Atlas unified ID</td><td colspan="2"><code>{esc(example["unified_id_example"])}</code> <span class="muted">(the same contract, written in asset IDs)</span></td></tr>'
        if example["unified_id_example"]
        else ""
    )
    return (
        '<div class="table-wrap"><table class="compare">'
        "<thead><tr><th>Venue</th><th>Native ID</th><th>Atlas ID</th></tr></thead>"
        f"<tbody>{rows}"
        f'<tr class="atlas-row"><td>Atlas asset</td><td colspan="2">{asset_cell} <span class="muted">{esc(example["name"])} · {esc(CLASS_SINGULAR.get(example["asset_class"], ""))} · listed live on {example["venues"]} venue snapshots</span></td></tr>'
        f"{unified}</tbody></table></div>"
    )


def render_why(examples: Mapping[str, Any], stats: Mapping[str, Any]) -> str:
    gold = ", ".join(f"<code>{esc(n)}</code>" for n in examples["gold_names"]) or "<code>XAU</code>"
    eur = ", ".join(
        f'<code>{esc(e["native_id"])}</code> on {esc(e["venue"])}' for e in examples["eur_ids"]
    ) or "<code>EURUSD</code>"
    pre = ", ".join(f"<code>{esc(n)}</code>" for n in examples["pre_ipo_names"]) or "<code>ANTHROPIC</code>"
    avantis = examples.get("avantis_example")
    avantis_text = (
        f'Avantis names its markets by pair index: <code>{esc(avantis["native_id"])}</code> is <code>{esc(avantis["internal_id"])}</code>.'
        if avantis
        else "Avantis names its markets by pair index, not by ticker."
    )
    cards = [
        (
            "A ticker is not an asset",
            f"<code>SPX</code> is a memecoin (SPX6900) on {examples['spx_coin_venues']} live perpetual venues, while the S&amp;P 500 trades as "
            f"<code>SP500</code>, <code>USA500</code> or <code>US500</code> on {examples['spx_index_venues']}. Atlas keys every contract on venue and native ID, "
            "then resolves it to an asset, so a stock never collides with a token that borrowed its ticker.",
        ),
        (
            "One asset, many names",
            f"Gold perpetuals trade as {gold} on {examples['gold_venues']} venue snapshots, and tokenized gold (<code>PAXG</code>) is a separate asset. "
            "Atlas gives the reference asset one ID, <code>XAU</code>, whatever the venue calls it.",
        ),
        (
            "FX has conventions",
            f"The euro trades as {eur}. Atlas reads them all as <code>EUR</code> priced in dollars, and keeps pairs not quoted in USD whole (<code>USDJPY</code>), "
            "so a quote is never silently inverted.",
        ),
        (
            "Pre-IPO and oracle-priced markets",
            f"{fmt(examples['pre_ipo_live'])} live pre-IPO contracts ({pre}) and oracle-priced venues have no exchange ticker to lean on. "
            f"{avantis_text}",
        ),
        (
            "Backtests that remember the dead",
            f"Exchange APIs only list what trades today. Atlas keeps all {fmt(stats['delisted'])} delisted instruments with the day they stopped trading, "
            f"{fmt(examples['delisted_rwa'])} of them real-world-asset contracts, so a universe can be rebuilt as it was on any date.",
        ),
        (
            "An audit trail for free",
            "The snapshots are plain JSON in git. Every listing, delisting and correction is a commit you can diff, blame and pin, "
            "so research can name the exact version of the universe it ran on.",
        ),
    ]
    return "".join(
        f'<article class="card"><h3>{esc(title)}</h3><p>{body}</p></article>' for title, body in cards
    )


def render_classes(stats: Mapping[str, Any]) -> str:
    tiles = []
    for klass in ASSET_CLASSES:
        if klass == "unknown":
            continue
        total = stats["classes"].get(klass, 0)
        live = stats["active_classes"].get(klass, 0)
        chips = "".join(
            (
                f'<button type="button" class="asset-chip" data-asset="{esc(a["asset_id"])}" title="{esc(a["name"])} · {a["venues"]} venues">{esc(a["label"])}</button>'
                if a["asset_id"]
                else f'<button type="button" class="asset-chip" data-ticker="{esc(a["label"])}" title="{esc(a["name"])} · {a["venues"]} venues">{esc(a["label"])}</button>'
            )
            for a in stats["top_assets"].get(klass, [])
        )
        tiles.append(
            f'<article class="class-tile{" rwa" if klass in RWA_CLASSES else ""}">'
            f'<div class="class-head"><span class="dot cls-{klass}"></span><h3>{esc(CLASS_LABELS[klass])}</h3></div>'
            f'<div class="class-count">{fmt(live)}<span> live</span></div>'
            f'<div class="class-sub">{fmt(total)} ever listed</div>'
            f'<div class="chips">{chips}</div>'
            f'<a class="text-link" href="?class={klass}&amp;status=active#catalogue">Browse {esc(CLASS_LABELS[klass].lower())} →</a>'
            "</article>"
        )
    return "".join(tiles)


def render_coverage(stats: Mapping[str, Any]) -> str:
    cards = []
    for v in stats["venue_stats"]:
        resolved = round(100 * v["resolved"] / v["total"]) if v["total"] else 0
        since = f"since {v['first_year']}" if v["first_year"] else ""
        cards.append(
            '<article class="venue">'
            f'<div class="venue-head"><a href="venues/{esc(v["slug"])}.html"><h3>{esc(v["venue"])}</h3></a>'
            f'<span class="badge badge-{v["kind"].lower()}">{esc(v["kind"])}</span></div>'
            f'<div class="venue-product">{esc(v["product"])} <span class="muted">{esc(since)}</span></div>'
            f'<code class="slug">{esc(v["slug"])}</code>'
            f'<div class="venue-nums"><span><b>{fmt(v["active"])}</b> live</span><span><b>{fmt(v["rwa_active"])}</b> RWA</span><span><b>{fmt(v["delisted"])}</b> delisted</span></div>'
            f"{class_bar(v['classes'], v['venue'] + ' ' + v['product'])}"
            f"{class_legend(v['classes'], 3)}"
            f'<div class="venue-foot"><span class="muted">{resolved}% resolved to Atlas assets</span>'
            f'<a class="text-link" href="?venue={esc(v["slug"])}#catalogue">Search →</a></div>'
            "</article>"
        )
    return "".join(cards)


def render_noscript_table(stats: Mapping[str, Any]) -> str:
    rows = "".join(
        f'<tr><td><a href="venues/{esc(v["slug"])}.html">{esc(v["venue"])} {esc(v["product"])}</a></td>'
        f'<td>{esc(v["kind"])}</td><td>{fmt(v["total"])}</td><td>{fmt(v["active"])}</td><td>{fmt(v["rwa_active"])}</td><td>{fmt(v["delisted"])}</td></tr>'
        for v in stats["venue_stats"]
    )
    return (
        '<div class="table-wrap"><table class="plain"><thead><tr><th>Venue</th><th>Kind</th><th>Instruments</th><th>Live</th><th>Live RWA</th><th>Delisted</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def render_class_options() -> str:
    return "".join(
        f'<button type="button" class="filter-chip" data-class="{c}" aria-pressed="false"><span class="dot cls-{c}"></span>{esc(CLASS_LABELS[c])}</button>'
        for c in ASSET_CLASSES
    )


def json_ld(stats: Mapping[str, Any], base_url: str) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "Atlas: open security master for RWA and crypto perpetuals",
        "description": (
            "Normalized reference data for "
            f"{stats['instruments']:,} exchange instruments ({stats['rwa']:,} real-world-asset contracts: "
            "equities, commodities, FX, indices and pre-IPO) across "
            f"{stats['venue_groups']} CEX and DEX venues, with one ID per contract, Atlas asset IDs, contract specs and listing windows."
        ),
        "url": base_url,
        "sameAs": REPO_URL,
        "license": "https://opensource.org/licenses/MIT",
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "Aperiodic", "url": "https://aperiodic.io"},
        "keywords": [
            "security master", "RWA", "real-world assets", "perpetual futures", "tokenized stocks",
            "symbology", "reference data", "crypto", "DEX", "Hyperliquid HIP-3",
        ],
        "dateModified": stats["generated_at"],
        "temporalCoverage": f"{stats['first_year']}/..",
        "distribution": [
            {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": base_url + "catalog/instruments.json"},
            {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": base_url + "catalog/assets.json"},
        ],
    }
    # `</` cannot end the script element early once escaped.
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def fill(template: str, values: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"template placeholder {{{{{key}}}}} has no value")
        return values[key]

    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", replace, template)


def render_index(
    stats: Mapping[str, Any],
    example: Mapping[str, Any] | None,
    examples: Mapping[str, Any],
    base_url: str,
) -> str:
    template = (SITE_DIR / "index.html").read_text(encoding="utf-8")
    description = (
        f"Atlas is a free, open-source security master for real-world-asset and crypto perpetuals: "
        f"{stats['rwa_active']:,} live equity, commodity, FX, index and pre-IPO contracts and {stats['instruments']:,} instruments "
        f"across {stats['venue_groups']} CEX and DEX venues, mapped to one ID and refreshed daily."
    )
    generated = stats["generated_at"]
    return fill(
        template,
        {
            "description": esc(description),
            "base_url": esc(base_url),
            "json_ld": json_ld(stats, base_url),
            "stats": render_stats(stats),
            "example": render_example(example),
            "example_ticker": esc((example or {}).get("ticker", "TSLA")),
            "why": render_why(examples, stats),
            "classes": render_classes(stats),
            "coverage": render_coverage(stats),
            "venue_table": render_noscript_table(stats),
            "class_filters": render_class_options(),
            "instruments": fmt(stats["instruments"]),
            "rwa_active": fmt(stats["rwa_active"]),
            "venue_groups": str(stats["venue_groups"]),
            "venue_count": str(stats["venues"]),
            "generated_at": esc(generated),
            "generated_day": esc(generated[:10]),
            "repo_url": REPO_URL,
            "aperiodic_url": esc(APERIODIC_URL),
        },
    )


def render_venue_page(
    slug: str,
    venue: Mapping[str, Any],
    instruments: list[Instrument],
    base_url: str,
    generated_at: str,
) -> str:
    template = (SITE_DIR / "venue.html").read_text(encoding="utf-8")
    class_order = {c: n for n, c in enumerate(ASSET_CLASSES)}
    rows = sorted(
        instruments,
        key=lambda i: (not i.active, class_order[i.asset_class], i.symbol, i.native_id),
    )
    body = "".join(_venue_row(i) for i in rows)
    title = f"{venue['venue']} {venue['product']}"
    description = (
        f"Every {venue['venue']} {venue['product'].lower()} instrument in Atlas: {venue['total']:,} contracts "
        f"({venue['active']:,} live, {venue['rwa_active']:,} live real-world-asset contracts, {venue['delisted']:,} delisted), "
        "with native IDs, Atlas IDs, asset classes and listing dates."
    )
    return fill(
        template,
        {
            "title": esc(title),
            "description": esc(description),
            "canonical": esc(f"{base_url}venues/{slug}.html"),
            "slug": esc(slug),
            "kind": esc(venue["kind"]),
            "kind_class": esc(venue["kind"].lower()),
            "total": fmt(venue["total"]),
            "active": fmt(venue["active"]),
            "rwa_active": fmt(venue["rwa_active"]),
            "delisted": fmt(venue["delisted"]),
            "bar": class_bar(venue["classes"], title),
            "legend": class_legend(venue["classes"], 7),
            "rows": body,
            "generated_at": esc(generated_at),
            "repo_url": REPO_URL,
            "aperiodic_url": esc(APERIODIC_URL),
        },
    )


def _venue_row(item: Instrument) -> str:
    asset = (
        f'<a class="asset-link" href="../?asset={esc(item.asset_id)}#catalogue">{esc(item.asset_id)}</a>'
        if item.asset_id
        else '<span class="muted">—</span>'
    )
    delisted = esc(item.end_date.isoformat()) if item.end_date else '<span class="live">live</span>'
    listed = esc(item.first_capture.isoformat()) if item.first_capture else ""
    return (
        "<tr>"
        f'<td data-label="Native"><code>{esc(item.native_id)}</code></td>'
        f'<td data-label="Atlas ID"><code>{esc(item.internal_id)}</code></td>'
        f'<td data-label="Asset">{asset}</td>'
        f'<td data-label="Class"><span class="dot cls-{item.asset_class}"></span>{esc(CLASS_SINGULAR[item.asset_class])}</td>'
        f'<td data-label="Type">{esc(item.type)}</td>'
        f'<td data-label="Listed">{listed}</td>'
        f'<td data-label="Delisted">{delisted}</td>'
        "</tr>"
    )


def render_files_page(files: list[str], generated_at: str) -> str:
    items = "".join(
        f'<li><a href="data/{esc(f)}"><code>{esc(f)}</code></a></li>' for f in files
    )
    return (
        '<!doctype html><html lang="en" data-theme="dark"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Atlas data files</title><link rel="stylesheet" href="style.css"></head>'
        '<body><main class="container page"><p class="eyebrow"><a href="./">Atlas</a> · Data files</p>'
        '<h1 class="page-title">Raw snapshots</h1>'
        '<p class="lede">Every snapshot exactly as committed to the repository, refreshed daily. '
        "Each file is also served at its old root path (<code>okx-perps.json</code>) for existing consumers.</p>"
        f'<p class="muted">Built {esc(generated_at)}.</p><ul class="file-list">{items}</ul></main></body></html>\n'
    )


def render_sitemap(base_url: str, venue_slugs: Iterable[str], day: str) -> str:
    urls = [base_url, base_url + "files.html", *(f"{base_url}venues/{s}.html" for s in venue_slugs)]
    entries = "".join(
        f"<url><loc>{esc(u)}</loc><lastmod>{day}</lastmod><changefreq>daily</changefreq></url>" for u in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>\n'
    )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def publish_data(data_dir: Path, out: Path) -> list[str]:
    """Copy every snapshot JSON to ``data/`` and, as before, to the site root."""
    files: list[str] = []
    for source in sorted(data_dir.rglob("*.json")):
        relative = source.relative_to(data_dir)
        for target in (out / "data" / relative, out / relative):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        files.append(relative.as_posix())
    return files


def build(
    data_dir: Path,
    out: Path,
    base_url: str = DEFAULT_BASE_URL,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not base_url.endswith("/"):
        base_url += "/"
    generated_at = (now or datetime.now(UTC)).astimezone(UTC)
    snapshots = load_snapshots(data_dir)
    assets = load_assets(data_dir)
    instruments = normalise(snapshots, assets)
    stats = compute_stats(instruments, assets, generated_at)
    example = cross_venue_example(instruments, assets)
    examples = ticker_examples(instruments, (example or {}).get("ticker", "TSLA"))
    stats["example"] = example
    venue_order = [v["slug"] for v in stats["venue_stats"]]

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    files = publish_data(data_dir, out)

    write_json(out / "catalog" / "instruments.json", catalogue_payload(instruments, venue_order, generated_at))
    write_json(out / "catalog" / "assets.json", assets_payload(assets))
    write_json(out / "catalog" / "stats.json", stats)

    for name in ("app.js", "style.css", "favicon.svg"):
        shutil.copyfile(SITE_DIR / name, out / name)
    (out / "index.html").write_text(render_index(stats, example, examples, base_url), encoding="utf-8")

    by_venue: dict[str, list[Instrument]] = defaultdict(list)
    for item in instruments:
        by_venue[item.venue].append(item)
    for venue in stats["venue_stats"]:
        page = render_venue_page(
            venue["slug"], venue, by_venue[venue["slug"]], base_url, stats["generated_at"]
        )
        target = out / "venues" / f"{venue['slug']}.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")

    (out / "files.html").write_text(render_files_page(files, stats["generated_at"]), encoding="utf-8")
    (out / "sitemap.xml").write_text(
        render_sitemap(base_url, venue_order, stats["generated_at"][:10]), encoding="utf-8"
    )
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {base_url}sitemap.xml\n", encoding="utf-8")
    (out / ".nojekyll").touch()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=Path("atlas/data"))
    parser.add_argument("--out", type=Path, default=Path("_site"))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args(argv)
    stats = build(args.data, args.out, args.base_url)
    print(
        f"built {args.out}: {stats['instruments']:,} instruments, {stats['rwa_active']:,} live RWA, "
        f"{stats['venues']} venue snapshots, {stats['assets']:,} assets"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
