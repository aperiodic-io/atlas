"""The assets the domain map covers, and how many Atlas contracts each has.

Identity comes from Atlas: a snapshot row's ``base_asset_id`` names the asset
and the registry (``atlas/data/symbology/assets.json``) its ticker, class, name
and CoinMarketCap ID. Symbology does not cover every venue yet, so a row
without a ``base_asset_id`` is attributed by its normalised symbol, and only
when that is safe:

* to the registry asset whose ID, ticker or alias is the symbol, among assets of
  the row's own class (an equity perp never lands on a same-ticker coin), and
  only when exactly one asset qualifies;
* never to an asset CoinMarketCap added after the row stopped trading (the
  ticker was reused);
* a TradFi row no asset matches (pre-IPO names Atlas does not mint, equities
  only listed on uncovered venues) becomes an asset of its own, keyed by the
  symbol, which only Wikidata's ticker listing or an override can map;
* a crypto row no asset matches is left out: guessing a coin from its symbol is
  what Atlas symbology refused to do, and a wrong site is worse than none.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Files in ``atlas/data`` that are not exchange snapshots.
NOT_SNAPSHOTS = frozenset({"cmc_mappings.json"})

# Multiplier prefixes of crypto contracts, longest first (``1000000BOB`` is not
# ``1000`` + ``000BOB``). ``k`` only matches lower case (Hyperliquid ``kPEPE``),
# so ``KAITO`` keeps its ``K``.
MULTIPLIERS = ("10000000", "1000000", "100000", "10000", "1000", "1M", "k")

_BITGET_TOKENISED = re.compile(r"r[A-Z0-9][A-Z0-9.]*")

TRADFI_CLASSES = frozenset({"equity", "pre_market", "commodity", "index", "forex"})

# Registry classes an unresolved row of a given class may be attributed to.
_COMPATIBLE = {
    "crypto": frozenset({"crypto"}),
    "equity": frozenset({"equity", "unknown"}),
    "pre_market": frozenset({"equity"}),
    "commodity": frozenset({"commodity"}),
    "index": frozenset({"index"}),
    "forex": frozenset({"forex", "fiat", "unknown"}),
}


@dataclass(frozen=True)
class RegistryAsset:
    asset_id: str
    ticker: str
    name: str
    asset_class: str
    cmc_id: int | None = None
    cmc_slug: str | None = None
    categories: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    cmc_date_added: str | None = None
    status: str = "active"


@dataclass
class Asset:
    """One asset of the map's universe and the Atlas contracts on it."""

    key: str
    # ``None`` for a TradFi underlying Atlas has no asset for.
    asset_id: str | None
    ticker: str
    asset_class: str
    name: str = ""
    cmc_id: int | None = None
    cmc_slug: str | None = None
    categories: tuple[str, ...] = ()
    symbols: set[str] = field(default_factory=set)
    contracts: int = 0
    trading: int = 0
    # Contracts attributed by symbol rather than by Atlas symbology.
    by_symbol: int = 0
    venues: set[str] = field(default_factory=set)
    # Row classes of the contracts, for an asset whose registry class is unknown.
    row_classes: Counter[str] = field(default_factory=Counter)
    # The day the last contract ended, while none trades.
    last_end: str | None = None
    # The day Atlas first saw any of its contracts.
    first_seen: str | None = None

    def add(
        self,
        symbol: str,
        venue: str,
        cls: str,
        ended: str | None,
        by_symbol: bool,
        first_seen: str | None = None,
    ) -> None:
        self.symbols.add(symbol)
        self.contracts += 1
        self.trading += ended is None
        self.by_symbol += by_symbol
        self.venues.add(venue)
        self.row_classes[cls] += 1
        if ended and (self.last_end is None or ended > self.last_end):
            self.last_end = ended
        if first_seen and (self.first_seen is None or first_seen < self.first_seen):
            self.first_seen = first_seen

    @property
    def ended(self) -> str | None:
        """When every contract has ended, the day the last one did."""
        return None if self.trading else self.last_end


@dataclass
class Universe:
    assets: dict[str, Asset]
    # Crypto rows no registry asset was found for, grouped by normalised symbol
    # (keyed like :func:`synthetic_key`). Not part of the map unless the
    # builder's guarded CoinMarketCap symbol fallback identifies them.
    unresolved: dict[str, Asset]


def synthetic_key(symbol: str, cls: str, registry: Mapping[str, RegistryAsset]) -> str:
    """The key of an asset Atlas has no ID for: its symbol, qualified by class
    when an Atlas asset already has that ID (Atlas's own ``COP.EQUITY`` style)."""
    return symbol if symbol not in registry else f"{symbol}.{cls.upper()}"


def load_registry(path: Path) -> dict[str, RegistryAsset]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for row in payload["assets"]:
        out[row["asset_id"]] = RegistryAsset(
            asset_id=row["asset_id"],
            ticker=row["ticker"],
            name=row.get("name", ""),
            asset_class=row["asset_class"],
            cmc_id=row.get("cmc_id"),
            cmc_slug=row.get("cmc_slug"),
            categories=tuple(row.get("categories") or ()),
            aliases=tuple(row.get("aliases") or ()),
            cmc_date_added=row.get("cmc_date_added"),
            status=row.get("status", "active"),
        )
    return out


def load_snapshots(
    data_dir: Path, venues: Iterable[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Every exchange snapshot in ``atlas/data``, by venue: only files named
    after an Atlas exchange definition (``venues``), never other JSON there."""
    if venues is None:
        from atlas.exchanges import EXCHANGE_DEFINITIONS

        venues = EXCHANGE_DEFINITIONS
    allowed = set(venues)
    out = {}
    for path in sorted(data_dir.glob("*.json")):
        if path.name in NOT_SNAPSHOTS or path.stem not in allowed:
            continue
        out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return out


def row_class(row: Mapping[str, Any]) -> str:
    """The class a venue declares: absent = crypto, ``unknown`` = equity."""
    underlying = row.get("underlying")
    if underlying is None:
        return "crypto"
    if underlying == "unknown":
        return "equity"
    return str(underlying)


def raw_symbol(row: Mapping[str, Any]) -> str | None:
    """The row's ``symbol`` in the venue's own case (``kPEPE``) when a field has it."""
    symbol = row.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        return None
    for key in ("base_asset", "base", "base_coin", "id"):
        value = row.get(key)
        if isinstance(value, str) and value.upper() == symbol:
            return value
    return symbol


def normalize_symbol(venue: str, row: Mapping[str, Any]) -> str | None:
    """The asset key of an unresolved row: its symbol without venue decorations."""
    raw = raw_symbol(row)
    if raw is None:
        return None
    cls = row_class(row)
    s = raw.split(":", 1)[1] if ":" in raw else raw
    if (
        venue.startswith("bitget")
        and cls != "crypto"
        and _BITGET_TOKENISED.fullmatch(s)
    ):
        # Bitget lists Reality's tokenised stocks as rGS, rPM: the lowercase r
        # is the issuer's, not the company's ticker.
        s = s[1:]
    if cls == "crypto":
        for prefix in MULTIPLIERS:
            rest = s.removeprefix(prefix)
            if (
                rest != s
                and rest[:1].isascii()
                and (rest[:1].isupper() or rest[:1].isdigit())
            ):
                s = rest
                break
    key = s.upper()
    if cls == "equity" and venue == "okx-spot" and len(key) > 1 and key.startswith("X"):
        # OKX spot lists xStocks as ``X<ticker>``; elsewhere X is part of the ticker.
        key = key[1:]
    if cls != "crypto":
        for suffix in ("STOCK", "_24_5"):
            if len(key) > len(suffix) and key.endswith(suffix):
                key = key[: -len(suffix)]
        # Korean listings: KRX code + KRW (``005930KRW`` = Samsung Electronics).
        if len(key) == 9 and key.endswith("KRW") and key[:6].isdigit():
            key = key[:6]
    return key or None


# A day on which this share of a venue's rows (and at least this many) were
# first captured is a bulk import, not a listing day.
CENSORED_SHARE = 0.05
CENSORED_MIN_ROWS = 50


def censored_days(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Days whose first_capture says when Atlas started recording a venue, not
    when a pair was listed: the venue's first day, and any day on which a bulk
    of its rows first appear (Gate's data starts 2022-06-08, with a few older
    rows imported from elsewhere). A first_seen on such a day is unknown."""
    days = Counter(d for d in (_day(r.get("first_capture")) for r in rows) if d)
    if not days:
        return set()
    total = sum(days.values())
    bulk = {
        d
        for d, n in days.items()
        if n >= max(CENSORED_MIN_ROWS, CENSORED_SHARE * total)
    }
    return bulk | {min(days)}


def _day(value: object) -> str | None:
    return value[:10] if isinstance(value, str) and value else None


def map_class(registry_class: str, row_classes: Mapping[str, int]) -> str:
    """The map's ``asset_class`` of a registry class (the map says ``forex``)."""
    if registry_class == "fiat":
        return "forex"
    if registry_class == "unknown":
        tradfi = Counter({c: n for c, n in row_classes.items() if c in TRADFI_CLASSES})
        return tradfi.most_common(1)[0][0] if tradfi else "equity"
    return registry_class


class _SymbolIndex:
    """Registry assets by ID, ticker and alias, for attributing unresolved rows."""

    def __init__(self, registry: Mapping[str, RegistryAsset]) -> None:
        self.registry = registry
        self.by_ticker: dict[str, list[RegistryAsset]] = {}
        self.by_alias: dict[str, list[RegistryAsset]] = {}
        for asset in registry.values():
            self.by_ticker.setdefault(asset.ticker.upper(), []).append(asset)
            for alias in asset.aliases:
                self.by_alias.setdefault(alias.upper(), []).append(asset)

    def find(self, key: str, cls: str, ended: str | None) -> RegistryAsset | None:
        classes = _COMPATIBLE.get(cls, frozenset())

        def usable(asset: RegistryAsset) -> bool:
            if asset.asset_class not in classes:
                return False
            # A coin CoinMarketCap added after the contract stopped trading
            # reused the ticker; it is not the asset the venue listed.
            return not (
                ended and asset.cmc_date_added and asset.cmc_date_added[:10] > ended
            )

        exact = self.registry.get(key)
        if exact is not None and usable(exact):
            return exact
        for index in (self.by_ticker, self.by_alias):
            candidates = [a for a in index.get(key, []) if usable(a)]
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                return None
        return None


def build_universe(
    registry: Mapping[str, RegistryAsset],
    snapshots: Mapping[str, Iterable[Mapping[str, Any]]],
    aliases: Mapping[str, str],
) -> Universe:
    index = _SymbolIndex(registry)
    assets: dict[str, Asset] = {}
    unresolved: dict[str, Asset] = {}

    def asset_for(reg: RegistryAsset) -> Asset:
        if reg.asset_id not in assets:
            assets[reg.asset_id] = Asset(
                key=reg.asset_id,
                asset_id=reg.asset_id,
                ticker=reg.ticker.upper(),
                asset_class=reg.asset_class,
                name=reg.name,
                cmc_id=reg.cmc_id,
                cmc_slug=reg.cmc_slug,
                categories=reg.categories,
            )
        return assets[reg.asset_id]

    venue_start = {venue: censored_days(rows) for venue, rows in snapshots.items()}
    for venue, rows in snapshots.items():
        for row in rows:
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                # Rows without a symbol name no asset (Bybit's dated "laptop" rows).
                continue
            end = row.get("end_date")
            ended = end[:10] if isinstance(end, str) and end else None
            cls = row_class(row)
            reg = registry.get(row.get("base_asset_id") or "")
            by_symbol = False
            asset: Asset | None = None
            if reg is not None:
                asset = asset_for(reg)
            else:
                key = normalize_symbol(venue, row)
                if key is None:
                    continue
                key = aliases.get(key, key)
                found = index.find(key, cls, ended)
                if found is not None:
                    asset, by_symbol = asset_for(found), True
                else:
                    target = assets if cls in TRADFI_CLASSES else unresolved
                    new_key = synthetic_key(key, cls, registry)
                    asset = target.setdefault(
                        new_key,
                        Asset(key=new_key, asset_id=None, ticker=key, asset_class=cls),
                    )
                    by_symbol = True
            first_seen = _day(row.get("first_capture"))
            if first_seen in venue_start[venue]:
                first_seen = None
            asset.add(symbol, venue, cls, ended, by_symbol, first_seen)
    for asset in assets.values():
        if asset.asset_id is not None:
            asset.asset_class = map_class(asset.asset_class, asset.row_classes)
    # A coin Atlas could not identify never shares a key with a TradFi asset
    # (Honeywell's HON perps and an unresolved HON coin are two assets).
    for key in [k for k in unresolved if k in assets]:
        moved = unresolved.pop(key)
        moved.key = f"{key}.CRYPTO"
        unresolved[moved.key] = moved
    return Universe(assets=assets, unresolved=unresolved)
