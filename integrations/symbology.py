"""Build Atlas's unified symbology: an Atlas asset ID for every instrument leg.

Every instrument row in the supported snapshots gets ``base_asset_id``,
``quote_asset_id`` and, for derivatives, ``margin_asset_id`` -- Atlas asset IDs
from ``atlas/data/symbology/assets.json`` -- plus a ``unified_id`` built from
them, so the same contract on any exchange reads the same:
``perpetual-BTC-USDT:USDT``.

The base asset of each row is resolved from the strongest available evidence,
and the method is stored on the row as ``base_asset_match``:

``override``
    A curated decision in ``atlas/data/symbology/overrides.json``.
``cmc_ledger``
    An approved decision in the CMC mapping ledger (``cmc_mappings.json``).
``cmc_market_pair``
    CoinMarketCap's own exchange market listing names the asset for this
    venue ticker (see ``integrations/cmc_market_pairs.py``).
``snapshot_cmc_id``
    A ``cmc_id`` the row already carried from an earlier mapping run.
``venue_lineage``
    The same ticker on the same exchange, listed at an overlapping time, is
    already identified -- an exchange does not list two assets under one
    ticker at once -- and every such anchor agrees.
``cross_venue_consensus``
    Other exchanges list the ticker at an overlapping time and every one of
    them that is identified agrees on the asset.

Direct evidence (the first four) is sticky: a row keeps it on later runs even
after it delists and CMC stops listing it. Inferred matches are recomputed
every run. A CMC asset that CMC added only after an instrument stopped trading
is never inferred for it: the ticker was reused by a later project.

Quote and margin assets are resolved per venue: a quote ticker means what CMC
says it means on that venue's live pairs (``USDT`` is Tether everywhere), or
what the venue's own identified base asset of that ticker is.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import requests

from atlas.assets import (
    DEFAULT_REGISTRY_PATH,
    Asset,
    AssetClass,
    AssetRegistry,
    sanitize_ticker,
)
from integrations.cmc_asset_details import (
    CmcAssetDetail,
    fetch_cmc_asset_details,
    load_detail_cache,
    save_detail_cache,
)
from integrations.cmc_market_pairs import (
    CMC_VENUES,
    MarketPair,
    MatchResult,
    fetch_venue_market_pairs,
    match_market_pairs,
)
from integrations.cmc_mappings import MappingStore


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"
DEFAULT_OVERRIDES_PATH = DEFAULT_REGISTRY_PATH.parent / "overrides.json"
DEFAULT_LEDGER_FILENAME = "cmc_mappings.json"
OVERRIDES_SCHEMA_VERSION = 1

# Exchange snapshot -> the namespace its tickers live in. Centralized venues
# share one coin code across spot and derivatives; Hyperliquid's spot tokens
# (``UBTC``) and perpetual tickers (``BTC``) are separate namespaces.
VENUE_FAMILIES: dict[str, str] = {
    "binance-spot": "binance",
    "binance-futures": "binance",
    "binance-futures-cm": "binance",
    "bybit-spot": "bybit",
    "bybit-perps": "bybit",
    "bybit-futures": "bybit",
    "okx-spot": "okx",
    "okx-perps": "okx",
    "okx-futures": "okx",
    "hyperliquid-spot": "hyperliquid-spot",
    "hyperliquid-perps": "hyperliquid-perps",
    "bitget": "bitget",
    "bitget-futures": "bitget",
    "gate-io": "gate",
    "gate-io-futures": "gate",
    "coinbase": "coinbase",
}
SYMBOLOGY_EXCHANGES: tuple[str, ...] = tuple(VENUE_FAMILIES)

SYMBOLOGY_FIELDS = (
    "base_asset_id",
    "quote_asset_id",
    "margin_asset_id",
    "base_multiplier",
    "base_asset_match",
    "base_asset_history",
    "unified_id",
)

# A ticker prefix that scales the contract: 1000PEPE, 1MBABYDOGE, kPEPE.
_MULTIPLIER_PREFIX = re.compile(r"(10{3,}|1M|K)([A-Z0-9].*)")
_NON_CRYPTO_UNDERLYINGS = ("equity", "commodity", "index")
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)
_FAR_PAST = datetime.min.replace(tzinfo=UTC)


class Match(StrEnum):
    override = "override"
    cmc_ledger = "cmc_ledger"
    cmc_market_pair = "cmc_market_pair"
    snapshot = "snapshot_cmc_id"
    venue_lineage = "venue_lineage"
    cross_venue = "cross_venue_consensus"


DIRECT_MATCHES = frozenset(
    {Match.override, Match.cmc_ledger, Match.cmc_market_pair, Match.snapshot}
)


@dataclass(frozen=True)
class Instrument:
    exchange: str
    original_id: str
    row: dict
    symbol: str
    denominator: str | None
    margin: str | None
    type: str
    first_capture: datetime
    end_date: datetime | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.exchange, self.original_id)

    @property
    def family(self) -> str:
        return VENUE_FAMILIES.get(self.exchange, self.exchange)

    @property
    def active(self) -> bool:
        return self.end_date is None

    def overlaps(self, other: Instrument) -> bool:
        return self.first_capture <= (other.end_date or _FAR_FUTURE) and (
            other.first_capture <= (self.end_date or _FAR_FUTURE)
        )


@dataclass(frozen=True)
class AssetSegment:
    """An earlier base asset of a row whose ticker changed hands (LUNA)."""

    cmc_id: int
    until: datetime


@dataclass(frozen=True)
class Override:
    exchange: str
    original_id: str
    # ``None`` records that no asset should be assigned.
    cmc_id: int | None
    reason: str
    history: tuple[AssetSegment, ...] = ()


@dataclass(frozen=True)
class BaseResolution:
    cmc_id: int
    method: Match
    history: tuple[AssetSegment, ...] = ()


@dataclass
class SymbologyReport:
    generated_at: datetime
    rows: Counter = field(default_factory=Counter)
    base_resolved: Counter = field(default_factory=Counter)
    quote_resolved: Counter = field(default_factory=Counter)
    active_rows: Counter = field(default_factory=Counter)
    active_base_resolved: Counter = field(default_factory=Counter)
    methods: Counter = field(default_factory=Counter)
    minted: list[str] = field(default_factory=list)
    # (exchange, id, chosen CMC ID, chosen method, other CMC ID, other method)
    disagreements: list[tuple[str, str, int, str, int, str]] = field(default_factory=list)
    # (exchange, id, previous base asset, new base asset)
    reassigned: list[tuple[str, str, str, str]] = field(default_factory=list)
    lineage_conflicts: list[tuple[str, str, tuple[int, ...]]] = field(default_factory=list)
    rejected_reused_tickers: list[tuple[str, str, int]] = field(default_factory=list)
    missing_details: set[int] = field(default_factory=set)
    unresolved_active: list[tuple[str, str, str]] = field(default_factory=list)
    pair_symbol_mismatches: list[tuple[str, str, str, int]] = field(default_factory=list)
    pair_conflicts: list[tuple[str, str]] = field(default_factory=list)
    pairs_unmatched: Counter = field(default_factory=Counter)
    pairs_total: Counter = field(default_factory=Counter)

    def summary(self) -> dict[str, Any]:
        rows = sum(self.rows.values())
        active = sum(self.active_rows.values())
        return {
            "generated_at": self.generated_at.isoformat(),
            "rows": rows,
            "base_resolved": sum(self.base_resolved.values()),
            "quote_resolved": sum(self.quote_resolved.values()),
            "active_rows": active,
            "active_base_resolved": sum(self.active_base_resolved.values()),
            "methods": dict(sorted(self.methods.items())),
            "minted": len(self.minted),
            "disagreements": len(self.disagreements),
            "reassigned": len(self.reassigned),
            "lineage_conflicts": len(self.lineage_conflicts),
            "rejected_reused_tickers": len(self.rejected_reused_tickers),
            "missing_details": len(self.missing_details),
            "unresolved_active": len(self.unresolved_active),
            "pair_symbol_mismatches": len(self.pair_symbol_mismatches),
            "pair_conflicts": len(self.pair_conflicts),
            "per_exchange": {
                exchange: {
                    "rows": self.rows[exchange],
                    "base_resolved": self.base_resolved[exchange],
                    "quote_resolved": self.quote_resolved[exchange],
                    "active_rows": self.active_rows[exchange],
                    "active_base_resolved": self.active_base_resolved[exchange],
                }
                for exchange in sorted(self.rows)
            },
        }


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def instruments_from_rows(rows_by_exchange: Mapping[str, list[dict]]) -> list[Instrument]:
    instruments: list[Instrument] = []
    for exchange, rows in rows_by_exchange.items():
        for row in rows:
            original_id = row.get("id")
            symbol = row.get("symbol")
            if not isinstance(original_id, str) or not isinstance(symbol, str) or not symbol:
                continue
            denominator = row.get("denominator")
            margin = row.get("margin")
            instruments.append(
                Instrument(
                    exchange=exchange,
                    original_id=original_id,
                    row=row,
                    symbol=symbol.upper(),
                    denominator=denominator.upper() if isinstance(denominator, str) else None,
                    margin=margin.upper() if isinstance(margin, str) else None,
                    type=str(row.get("type") or ""),
                    first_capture=_parse_timestamp(row.get("first_capture")) or _FAR_PAST,
                    end_date=_parse_timestamp(row.get("end_date")),
                )
            )
    return instruments


def load_overrides(path: Path) -> dict[tuple[str, str], Override]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict) or payload.get("schema_version") != OVERRIDES_SCHEMA_VERSION:
        raise ValueError("unsupported symbology overrides schema")
    overrides: dict[tuple[str, str], Override] = {}
    for row in payload.get("overrides", []):
        try:
            cmc_id = row.get("cmc_id")
            override = Override(
                exchange=str(row["exchange"]),
                original_id=str(row["original_id"]),
                cmc_id=None if cmc_id is None else int(cmc_id),
                reason=str(row["reason"]),
                history=tuple(
                    AssetSegment(int(segment["cmc_id"]), _require_timestamp(segment["until"]))
                    for segment in row.get("history", [])
                ),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ValueError(f"malformed symbology override {row!r}: {error}") from error
        key = (override.exchange, override.original_id)
        if key in overrides:
            raise ValueError(f"duplicate symbology override for {key}")
        overrides[key] = override
    return overrides


def ledger_decisions(store: MappingStore | None) -> dict[tuple[str, str, datetime], tuple[int, bool]]:
    """Approved ledger IDs keyed by instrument instance; flag manual approvals."""
    if store is None:
        return {}
    decisions: dict[tuple[str, str, datetime], tuple[int, bool]] = {}
    for mapping in store.mappings:
        if mapping.status != "approved" or mapping.cmc_id is None:
            continue
        instance = mapping.instrument
        decisions[(instance.exchange, instance.original_id, instance.first_capture)] = (
            mapping.cmc_id,
            mapping.method == "manual",
        )
    return decisions


# ---------------------------------------------------------------------------
# Base asset resolution
# ---------------------------------------------------------------------------


def resolve_base_assets(
    instruments: list[Instrument],
    pair_matches: Mapping[tuple[str, str], MarketPair],
    ledger: Mapping[tuple[str, str, datetime], tuple[int, bool]],
    overrides: Mapping[tuple[str, str], Override],
    details: Mapping[int, CmcAssetDetail],
    report: SymbologyReport,
) -> dict[tuple[str, str], BaseResolution]:
    resolutions: dict[tuple[str, str], BaseResolution] = {}
    excluded: set[tuple[str, str]] = set()
    for instrument in instruments:
        resolution = _direct_resolution(instrument, pair_matches, ledger, overrides, report)
        if resolution is None:
            if (override := overrides.get(instrument.key)) is not None and override.cmc_id is None:
                excluded.add(instrument.key)
            continue
        resolutions[instrument.key] = resolution

    unresolved = [
        instrument
        for instrument in instruments
        if instrument.key not in resolutions and instrument.key not in excluded
    ]
    _infer(
        unresolved,
        instruments,
        resolutions,
        details,
        report,
        method=Match.venue_lineage,
        group_key=lambda instrument: (instrument.family, instrument.symbol),
        same_family=True,
    )
    unresolved = [instrument for instrument in unresolved if instrument.key not in resolutions]
    _infer(
        unresolved,
        instruments,
        resolutions,
        details,
        report,
        method=Match.cross_venue,
        group_key=lambda instrument: (instrument.symbol,),
        same_family=False,
    )
    return resolutions


def _direct_resolution(
    instrument: Instrument,
    pair_matches: Mapping[tuple[str, str], MarketPair],
    ledger: Mapping[tuple[str, str, datetime], tuple[int, bool]],
    overrides: Mapping[tuple[str, str], Override],
    report: SymbologyReport,
) -> BaseResolution | None:
    override = overrides.get(instrument.key)
    if override is not None:
        if override.cmc_id is None:
            return None
        return BaseResolution(override.cmc_id, Match.override, override.history)

    candidates: list[tuple[int, Match]] = []
    ledger_hit = ledger.get(
        (instrument.exchange, instrument.original_id, instrument.first_capture)
    )
    if ledger_hit is not None and ledger_hit[1]:
        candidates.append((ledger_hit[0], Match.cmc_ledger))
    if (pair := pair_matches.get(instrument.key)) is not None:
        candidates.append((pair.base_cmc_id, Match.cmc_market_pair))
    if ledger_hit is not None and not ledger_hit[1]:
        candidates.append((ledger_hit[0], Match.cmc_ledger))
    stored = _stored_direct_match(instrument.row)
    if stored is not None:
        candidates.append(stored)
    if not candidates:
        return None

    chosen_id, chosen_method = candidates[0]
    for other_id, other_method in candidates[1:]:
        if other_id != chosen_id:
            report.disagreements.append(
                (
                    instrument.exchange,
                    instrument.original_id,
                    chosen_id,
                    chosen_method.value,
                    other_id,
                    other_method.value,
                )
            )
            break
    return BaseResolution(chosen_id, chosen_method)


def _stored_direct_match(row: Mapping[str, Any]) -> tuple[int, Match] | None:
    """A CMC ID the row carries from earlier direct evidence, if any.

    Inferred matches are deliberately excluded so that a lineage or consensus
    guess never hardens into evidence on the next run.
    """
    cmc_id = row.get("cmc_id")
    if not isinstance(cmc_id, int) or isinstance(cmc_id, bool) or cmc_id <= 0:
        return None
    method = row.get("base_asset_match")
    if method is None:
        return (cmc_id, Match.snapshot)
    try:
        parsed = Match(method)
    except ValueError:
        return (cmc_id, Match.snapshot)
    # Overrides are re-read from their file every run, so a removed override
    # must not survive through the row it was written to.
    if parsed not in DIRECT_MATCHES or parsed is Match.override:
        return None
    return (cmc_id, parsed)


def _infer(
    unresolved: list[Instrument],
    instruments: list[Instrument],
    resolutions: dict[tuple[str, str], BaseResolution],
    details: Mapping[int, CmcAssetDetail],
    report: SymbologyReport,
    method: Match,
    group_key,
    same_family: bool,
) -> None:
    anchors_by_group: dict[tuple, list[Instrument]] = defaultdict(list)
    for instrument in instruments:
        resolution = resolutions.get(instrument.key)
        if resolution is not None and resolution.method in DIRECT_MATCHES:
            anchors_by_group[group_key(instrument)].append(instrument)

    inferred: dict[tuple[str, str], BaseResolution] = {}
    for instrument in unresolved:
        anchors = [
            anchor
            for anchor in anchors_by_group.get(group_key(instrument), [])
            if anchor.overlaps(instrument)
            and (same_family or anchor.family != instrument.family)
        ]
        if not anchors:
            continue
        cmc_ids = {
            _cmc_id_at(resolutions[anchor.key], instrument) for anchor in anchors
        }
        if len(cmc_ids) != 1:
            report.lineage_conflicts.append(
                (instrument.exchange, instrument.original_id, tuple(sorted(cmc_ids)))
            )
            continue
        (cmc_id,) = cmc_ids
        detail = details.get(cmc_id)
        if (
            detail is not None
            and detail.date_added is not None
            and instrument.end_date is not None
            and instrument.end_date < detail.date_added
        ):
            report.rejected_reused_tickers.append(
                (instrument.exchange, instrument.original_id, cmc_id)
            )
            continue
        inferred[instrument.key] = BaseResolution(cmc_id, method)
    resolutions.update(inferred)


def _cmc_id_at(resolution: BaseResolution, instrument: Instrument) -> int:
    """The anchor's asset at the time ``instrument`` traded, honouring history."""
    end = instrument.end_date or _FAR_FUTURE
    for segment in resolution.history:
        if end <= segment.until:
            return segment.cmc_id
    return resolution.cmc_id


# ---------------------------------------------------------------------------
# Quote and margin resolution
# ---------------------------------------------------------------------------


@dataclass
class TickerTables:
    family_quotes: dict[tuple[str, str], Counter] = field(default_factory=lambda: defaultdict(Counter))
    global_quotes: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    family_bases: dict[tuple[str, str], Counter] = field(default_factory=lambda: defaultdict(Counter))
    global_bases: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))

    def resolve(self, family: str, ticker: str | None) -> int | None:
        if not ticker:
            return None
        for votes in (
            self.family_quotes.get((family, ticker)),
            self.family_bases.get((family, ticker)),
            self.global_quotes.get(ticker),
            self.global_bases.get(ticker),
        ):
            if winner := _dominant(votes):
                return winner
        return None


def build_ticker_tables(
    instruments: Iterable[Instrument],
    pair_matches: Mapping[tuple[str, str], MarketPair],
    resolutions: Mapping[tuple[str, str], BaseResolution],
) -> TickerTables:
    tables = TickerTables()
    for instrument in instruments:
        pair = pair_matches.get(instrument.key)
        if (
            pair is not None
            and pair.quote_cmc_id is not None
            and instrument.denominator
            and sanitize_ticker(pair.quote_symbol) == sanitize_ticker(instrument.denominator)
        ):
            tables.family_quotes[(instrument.family, instrument.denominator)][pair.quote_cmc_id] += 1
            tables.global_quotes[instrument.denominator][pair.quote_cmc_id] += 1
        resolution = resolutions.get(instrument.key)
        if (
            resolution is not None
            and instrument.active
            and resolution.method in DIRECT_MATCHES
            and _MULTIPLIER_PREFIX.fullmatch(instrument.symbol) is None
        ):
            tables.family_bases[(instrument.family, instrument.symbol)][resolution.cmc_id] += 1
            tables.global_bases[instrument.symbol][resolution.cmc_id] += 1
    return tables


def _dominant(votes: Counter | None, share: float = 0.9) -> int | None:
    """The ID holding at least ``share`` of the votes, if one does."""
    if not votes:
        return None
    winner, count = votes.most_common(1)[0]
    return winner if count >= share * sum(votes.values()) else None


def resolve_quote(
    instrument: Instrument,
    pair: MarketPair | None,
    tables: TickerTables,
) -> int | None:
    if (
        pair is not None
        and pair.quote_cmc_id is not None
        and instrument.denominator
        and sanitize_ticker(pair.quote_symbol) == sanitize_ticker(instrument.denominator)
    ):
        return pair.quote_cmc_id
    return tables.resolve(instrument.family, instrument.denominator)


def resolve_margin(
    instrument: Instrument,
    base_cmc_id: int | None,
    quote_cmc_id: int | None,
    tables: TickerTables,
) -> int | None:
    if instrument.type == "spot" or not instrument.margin:
        return None
    if instrument.margin == instrument.symbol:
        return base_cmc_id
    if instrument.margin == instrument.denominator:
        return quote_cmc_id
    return tables.resolve(instrument.family, instrument.margin)


def base_multiplier(symbol: str, asset: Asset) -> int | None:
    """Units of ``asset`` one unit of a venue ticker represents, when not one.

    ``1000PEPE`` is 1,000 PEPE, ``kPEPE`` likewise and ``1MBABYDOGE`` one
    million BABYDOGE. A ticker that is the asset's own (or an alias) has none.
    """
    ticker = sanitize_ticker(symbol.split(":", 1)[-1])
    names = {sanitize_ticker(asset.ticker), *(sanitize_ticker(a) for a in asset.aliases)}
    if ticker in names:
        return None
    match = _MULTIPLIER_PREFIX.fullmatch(ticker)
    if match is None or match.group(2) not in names:
        return None
    prefix = match.group(1)
    if prefix == "K":
        return 1000
    if prefix == "1M":
        return 1_000_000
    return int(prefix)


# ---------------------------------------------------------------------------
# Asset registry
# ---------------------------------------------------------------------------


def classify_asset(detail: CmcAssetDetail, underlyings: Counter) -> AssetClass:
    if detail.is_fiat:
        return AssetClass.fiat
    is_reference = detail.name.endswith("(Derivatives)") or (
        "TradFi Assets (Derivatives)" in detail.tags
    )
    if not is_reference:
        return AssetClass.crypto
    for underlying, _ in underlyings.most_common():
        if underlying in _NON_CRYPTO_UNDERLYINGS:
            return AssetClass(underlying)
    if "Tokenized Stock" in detail.tags or "Tokenized Stock" in detail.categories:
        return AssetClass.equity
    return AssetClass.unknown


def ensure_assets(
    registry: AssetRegistry,
    usage: Counter,
    underlyings: Mapping[int, Counter],
    details: Mapping[int, CmcAssetDetail],
    report: SymbologyReport,
) -> None:
    """Mint an asset for every used CMC ID, most used first; refresh the rest."""

    def priority(cmc_id: int) -> tuple:
        detail = details.get(cmc_id)
        return (
            0 if detail is not None and detail.is_fiat else 1,
            -usage[cmc_id],
            detail.rank if detail is not None and detail.rank is not None else sys.maxsize,
            cmc_id,
        )

    for cmc_id in sorted(usage, key=priority):
        detail = details.get(cmc_id)
        existing = registry.by_cmc_id(cmc_id)
        if detail is None:
            if existing is None:
                report.missing_details.add(cmc_id)
            continue
        asset_class = classify_asset(detail, underlyings.get(cmc_id, Counter()))
        status = "active" if detail.status in ("", "active") else detail.status
        if existing is None:
            asset = registry.mint(
                ticker=detail.symbol,
                name=detail.name,
                asset_class=asset_class,
                cmc_id=cmc_id,
                cmc_slug=detail.slug,
                categories=detail.categories,
                status=status,
            )
            report.minted.append(asset.asset_id)
        else:
            registry.update(
                existing.asset_id,
                ticker=detail.symbol,
                name=detail.name,
                asset_class=asset_class,
                cmc_slug=detail.slug,
                categories=detail.categories,
                status=status,
            )


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def build_symbology(
    rows_by_exchange: Mapping[str, list[dict]],
    registry: AssetRegistry,
    pair_result: MatchResult | None = None,
    ledger: Mapping[tuple[str, str, datetime], tuple[int, bool]] | None = None,
    overrides: Mapping[tuple[str, str], Override] | None = None,
    details: Mapping[int, CmcAssetDetail] | None = None,
    now: datetime | None = None,
) -> SymbologyReport:
    """Resolve, mint and write symbology fields onto ``rows_by_exchange`` in place."""
    report = SymbologyReport(generated_at=now or datetime.now(UTC))
    pair_matches = pair_result.matches if pair_result is not None else {}
    details = details or {}
    if pair_result is not None:
        report.pair_symbol_mismatches = [
            (key[0], key[1], pair.base_symbol, pair.base_cmc_id)
            for key, pair in pair_result.symbol_mismatches
        ]
        report.pair_conflicts = list(pair_result.conflicts)
        for pair in pair_result.unmatched:
            report.pairs_unmatched[f"{pair.venue}/{pair.category}"] += 1

    instruments = instruments_from_rows(rows_by_exchange)
    resolutions = resolve_base_assets(
        instruments, pair_matches, ledger or {}, overrides or {}, details, report
    )
    tables = build_ticker_tables(instruments, pair_matches, resolutions)

    legs: dict[tuple[str, str], tuple[int | None, int | None, int | None]] = {}
    usage: Counter = Counter()
    underlyings: dict[int, Counter] = defaultdict(Counter)
    for instrument in instruments:
        resolution = resolutions.get(instrument.key)
        base = resolution.cmc_id if resolution is not None else None
        quote = resolve_quote(instrument, pair_matches.get(instrument.key), tables)
        margin = resolve_margin(instrument, base, quote, tables)
        legs[instrument.key] = (base, quote, margin)
        for cmc_id in (base, quote, margin):
            if cmc_id is not None:
                usage[cmc_id] += 1
        if resolution is not None:
            for segment in resolution.history:
                usage[segment.cmc_id] += 1
        if base is not None and isinstance(instrument.row.get("underlying"), str):
            underlyings[base][instrument.row["underlying"]] += 1

    ensure_assets(registry, usage, underlyings, details, report)

    for instrument in instruments:
        base, quote, margin = legs[instrument.key]
        _write_row(instrument, registry, resolutions.get(instrument.key), base, quote, margin, report)
    return report


def _write_row(
    instrument: Instrument,
    registry: AssetRegistry,
    resolution: BaseResolution | None,
    base_cmc_id: int | None,
    quote_cmc_id: int | None,
    margin_cmc_id: int | None,
    report: SymbologyReport,
) -> None:
    row = instrument.row
    previous_base = row.get("base_asset_id")
    for key in SYMBOLOGY_FIELDS:
        row.pop(key, None)

    base = registry.by_cmc_id(base_cmc_id) if base_cmc_id is not None else None
    quote = registry.by_cmc_id(quote_cmc_id) if quote_cmc_id is not None else None
    margin = registry.by_cmc_id(margin_cmc_id) if margin_cmc_id is not None else None
    exchange = instrument.exchange
    report.rows[exchange] += 1
    if instrument.active:
        report.active_rows[exchange] += 1

    if base is not None and resolution is not None:
        _write_base(instrument, registry, base, resolution, report)
        if isinstance(previous_base, str) and previous_base != base.asset_id:
            report.reassigned.append(
                (exchange, instrument.original_id, previous_base, base.asset_id)
            )
    elif instrument.active:
        report.unresolved_active.append((exchange, instrument.original_id, instrument.symbol))

    if quote is not None:
        row["quote_asset_id"] = quote.asset_id
        report.quote_resolved[exchange] += 1
    if margin is not None:
        row["margin_asset_id"] = margin.asset_id
    if base is not None and quote is not None:
        unified = unified_id(instrument, base, quote, margin)
        if unified is not None:
            row["unified_id"] = unified


def _write_base(
    instrument: Instrument,
    registry: AssetRegistry,
    base: Asset,
    resolution: BaseResolution,
    report: SymbologyReport,
) -> None:
    row = instrument.row
    row["base_asset_id"] = base.asset_id
    row["cmc_id"] = base.cmc_id
    if base.categories:
        row["category"] = list(base.categories)
    else:
        row.pop("category", None)
    row["base_asset_match"] = resolution.method.value
    if (multiplier := base_multiplier(instrument.symbol, base)) is not None:
        row["base_multiplier"] = multiplier
    history = [
        {"asset_id": asset.asset_id, "until": _format_timestamp(segment.until)}
        for segment in resolution.history
        if (asset := registry.by_cmc_id(segment.cmc_id)) is not None
    ]
    if history:
        row["base_asset_history"] = history
    report.base_resolved[instrument.exchange] += 1
    report.methods[resolution.method.value] += 1
    if instrument.active:
        report.active_base_resolved[instrument.exchange] += 1


def unified_id(
    instrument: Instrument, base: Asset, quote: Asset, margin: Asset | None
) -> str | None:
    """``<type>-<base>-<quote>[:<margin>][-<yyyymmdd>]`` in Atlas asset IDs.

    Mirrors ``internal_id`` but is built from asset IDs, so it reads the same
    on every exchange. A derivative without a resolved margin asset has none.
    """
    unified = f"{instrument.type}-{base.asset_id}-{quote.asset_id}"
    if instrument.type != "spot":
        if margin is None:
            return None
        unified += f":{margin.asset_id}"
    delivery = _parse_timestamp(instrument.row.get("delivery_date"))
    if delivery is not None:
        unified += f"-{delivery.strftime('%Y%m%d')}"
    return unified


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def render_report(report: SymbologyReport, registry: AssetRegistry, limit: int = 50) -> str:
    summary = report.summary()
    lines = [
        "# Unified symbology build",
        "",
        f"Generated {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')}. "
        f"{len(registry)} assets in the registry, {summary['minted']} minted in this run.",
        "",
        "| Exchange | Rows | Base resolved | Quote resolved | Active | Active base resolved |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for exchange, stats in summary["per_exchange"].items():
        lines.append(
            f"| `{exchange}` | {stats['rows']} | {_pct(stats['base_resolved'], stats['rows'])} "
            f"| {_pct(stats['quote_resolved'], stats['rows'])} | {stats['active_rows']} "
            f"| {_pct(stats['active_base_resolved'], stats['active_rows'])} |"
        )
    lines.append(
        f"| **total** | {summary['rows']} | {_pct(summary['base_resolved'], summary['rows'])} "
        f"| {_pct(summary['quote_resolved'], summary['rows'])} | {summary['active_rows']} "
        f"| {_pct(summary['active_base_resolved'], summary['active_rows'])} |"
    )
    lines += ["", "## Match methods", ""]
    lines += [f"- `{method}`: {count}" for method, count in summary["methods"].items()]

    def section(title: str, entries: list, render) -> None:
        if not entries:
            return
        lines.extend(["", f"## {title} ({len(entries)})", ""])
        lines.extend(f"- {render(entry)}" for entry in entries[:limit])
        if len(entries) > limit:
            lines.append(f"- ... and {len(entries) - limit} more")

    def asset_name(cmc_id: int) -> str:
        asset = registry.by_cmc_id(cmc_id)
        return f"{asset.asset_id} ({cmc_id})" if asset else str(cmc_id)

    section(
        "Evidence disagreements (first source wins)",
        report.disagreements,
        lambda e: f"`{e[0]}` `{e[1]}`: {asset_name(e[2])} via {e[3]} over {asset_name(e[4])} via {e[5]}",
    )
    section(
        "Reassigned base assets",
        report.reassigned,
        lambda e: f"`{e[0]}` `{e[1]}`: {e[2]} -> {e[3]}",
    )
    section(
        "CMC pairs whose base ticker disagrees with the Atlas row (not used)",
        report.pair_symbol_mismatches,
        lambda e: f"`{e[0]}` `{e[1]}`: CMC says {e[2]} ({e[3]})",
    )
    section(
        "Rows claimed by conflicting CMC pairs (not used)",
        report.pair_conflicts,
        lambda e: f"`{e[0]}` `{e[1]}`",
    )
    section(
        "Inference conflicts (anchors disagree)",
        report.lineage_conflicts,
        lambda e: f"`{e[0]}` `{e[1]}`: {', '.join(asset_name(i) for i in e[2])}",
    )
    section(
        "Rejected ticker reuse (CMC added the asset after the row stopped trading)",
        report.rejected_reused_tickers,
        lambda e: f"`{e[0]}` `{e[1]}`: {asset_name(e[2])}",
    )
    section(
        "Active rows without a base asset",
        report.unresolved_active,
        lambda e: f"`{e[0]}` `{e[1]}` ({e[2]})",
    )
    section(
        "CMC IDs without fetched details (not minted)",
        sorted(report.missing_details),
        str,
    )
    if report.pairs_unmatched:
        lines += ["", "## CMC pairs without an active Atlas row", ""]
        lines += [
            f"- `{venue}`: {count}" for venue, count in sorted(report.pairs_unmatched.items())
        ]
    return "\n".join(lines) + "\n"


def _pct(part: int, whole: int) -> str:
    return f"{part} ({100 * part / whole:.1f}%)" if whole else "0"


# ---------------------------------------------------------------------------
# IO and CLI
# ---------------------------------------------------------------------------


def load_snapshots(data_dir: Path, exchanges: Iterable[str]) -> dict[str, list[dict]]:
    rows_by_exchange: dict[str, list[dict]] = {}
    for exchange in exchanges:
        path = data_dir / f"{exchange}.json"
        if not path.exists():
            continue
        rows = json.loads(path.read_text())
        if isinstance(rows, list):
            rows_by_exchange[exchange] = [row for row in rows if isinstance(row, dict)]
    return rows_by_exchange


def save_snapshots(data_dir: Path, rows_by_exchange: Mapping[str, list[dict]]) -> None:
    """Rewrite snapshots in ``atlas/update.py``'s exact serialization."""
    for exchange, rows in rows_by_exchange.items():
        (data_dir / f"{exchange}.json").write_text(json.dumps(rows, indent=2))


def fetch_pair_listings(
    session: requests.Session,
    venues: Iterable[str],
    cache_dir: Path | None = None,
) -> list[MarketPair]:
    pairs: list[MarketPair] = []
    for venue in venues:
        print(f"Fetching CMC market pairs for {venue}...", flush=True)
        listings = fetch_venue_market_pairs(session, venue)
        for category, listing in listings.items():
            print(
                f"  {category}: {len(listing.pairs)} pairs "
                f"(reported {listing.reported_total}, malformed {listing.malformed})",
                flush=True,
            )
            pairs.extend(listing.pairs)
            if cache_dir is not None:
                cache_dir.mkdir(parents=True, exist_ok=True)
                (cache_dir / f"{venue}-{category}.json").write_text(
                    json.dumps([_pair_to_json(pair) for pair in listing.pairs])
                )
    return pairs


def load_cached_pairs(cache_dir: Path) -> list[MarketPair]:
    pairs: list[MarketPair] = []
    for path in sorted(cache_dir.glob("*.json")):
        for row in json.loads(path.read_text()):
            pairs.append(_pair_from_json(row))
    return pairs


def _pair_to_json(pair: MarketPair) -> dict[str, Any]:
    return {
        **pair.__dict__,
        "last_updated": pair.last_updated.isoformat() if pair.last_updated else None,
    }


def _pair_from_json(row: dict[str, Any]) -> MarketPair:
    return MarketPair(**{**row, "last_updated": _parse_timestamp(row.get("last_updated"))})


def run(
    data_dir: Path = DEFAULT_DATA_DIR,
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    overrides_path: Path = DEFAULT_OVERRIDES_PATH,
    exchanges: Iterable[str] = SYMBOLOGY_EXCHANGES,
    offline: bool = False,
    pairs_cache: Path | None = None,
    use_pairs_cache: bool = False,
    details_cache: Path | None = None,
    refresh_details: bool = False,
    dry_run: bool = False,
) -> tuple[SymbologyReport, AssetRegistry]:
    exchanges = tuple(exchanges)
    rows_by_exchange = load_snapshots(data_dir, exchanges)
    registry = AssetRegistry.load(registry_path)
    overrides = load_overrides(overrides_path)
    ledger_path = data_dir / DEFAULT_LEDGER_FILENAME
    ledger = ledger_decisions(MappingStore.load(ledger_path) if ledger_path.exists() else None)
    details: dict[int, CmcAssetDetail] = (
        load_detail_cache(details_cache) if details_cache is not None else {}
    )

    pair_result: MatchResult | None = None
    with requests.Session() as session:
        if use_pairs_cache and pairs_cache is not None:
            pairs = load_cached_pairs(pairs_cache)
        elif offline:
            pairs = []
        else:
            venues = sorted(
                venue.slug
                for venue in CMC_VENUES.values()
                if any(e in rows_by_exchange for es in venue.exchanges.values() for e in es)
            )
            pairs = fetch_pair_listings(session, venues, pairs_cache)
        if pairs:
            pair_result = match_market_pairs(pairs, rows_by_exchange)

        if not offline:
            wanted = _referenced_cmc_ids(rows_by_exchange, pair_result, ledger, overrides)
            if not refresh_details:
                wanted = {
                    cmc_id
                    for cmc_id in wanted
                    if registry.by_cmc_id(cmc_id) is None and cmc_id not in details
                }
            wanted -= set(details) if not refresh_details else set()
            if wanted:
                print(f"Fetching CMC details for {len(wanted)} assets...", flush=True)
                fetched = fetch_cmc_asset_details(session, wanted, progress_every=250)
                details.update(fetched.details)
                if fetched.failures:
                    print(f"  {len(fetched.failures)} detail fetches failed", flush=True)
                if details_cache is not None:
                    save_detail_cache(details_cache, details)

    report = build_symbology(
        rows_by_exchange,
        registry,
        pair_result=pair_result,
        ledger=ledger,
        overrides=overrides,
        details=details,
    )
    if not dry_run:
        registry.save()
        save_snapshots(data_dir, rows_by_exchange)
    return report, registry


def _referenced_cmc_ids(
    rows_by_exchange: Mapping[str, list[dict]],
    pair_result: MatchResult | None,
    ledger: Mapping[tuple[str, str, datetime], tuple[int, bool]],
    overrides: Mapping[tuple[str, str], Override],
) -> set[int]:
    ids: set[int] = set()
    for rows in rows_by_exchange.values():
        for row in rows:
            cmc_id = row.get("cmc_id")
            if isinstance(cmc_id, int) and not isinstance(cmc_id, bool) and cmc_id > 0:
                ids.add(cmc_id)
    if pair_result is not None:
        for pair in pair_result.matches.values():
            ids.add(pair.base_cmc_id)
            if pair.quote_cmc_id is not None:
                ids.add(pair.quote_cmc_id)
    ids.update(cmc_id for cmc_id, _ in ledger.values())
    for override in overrides.values():
        if override.cmc_id is not None:
            ids.add(override.cmc_id)
        ids.update(segment.cmc_id for segment in override.history)
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--registry-path", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--overrides-path", type=Path, default=DEFAULT_OVERRIDES_PATH)
    parser.add_argument(
        "--exchanges",
        default=",".join(SYMBOLOGY_EXCHANGES),
        help="comma-separated snapshots to build (default: all supported)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="no network: rebuild from stored evidence, the ledger and overrides",
    )
    parser.add_argument("--pairs-cache", type=Path, help="directory to write/read CMC market pairs")
    parser.add_argument(
        "--use-pairs-cache",
        action="store_true",
        help="read CMC market pairs from --pairs-cache instead of fetching them",
    )
    parser.add_argument("--details-cache", type=Path, help="JSON cache of CMC asset details")
    parser.add_argument(
        "--refresh-details",
        action="store_true",
        help="refetch details (names, tickers, categories) for every referenced asset",
    )
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--summary-path", type=Path)
    args = parser.parse_args(argv)

    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()]
    unknown = sorted(set(exchanges) - set(SYMBOLOGY_EXCHANGES))
    if unknown:
        parser.error(f"unsupported exchanges: {', '.join(unknown)}")
    report, registry = run(
        data_dir=args.data_dir,
        registry_path=args.registry_path,
        overrides_path=args.overrides_path,
        exchanges=exchanges,
        offline=args.offline,
        pairs_cache=args.pairs_cache,
        use_pairs_cache=args.use_pairs_cache,
        details_cache=args.details_cache,
        refresh_details=args.refresh_details,
        dry_run=args.dry_run,
    )
    rendered = render_report(report, registry)
    if args.report_path:
        args.report_path.write_text(rendered)
    if args.summary_path:
        args.summary_path.write_text(json.dumps(report.summary(), indent=2))
    summary = report.summary()
    print(
        f"rows={summary['rows']} base_resolved={summary['base_resolved']} "
        f"active={summary['active_rows']} active_base_resolved={summary['active_base_resolved']} "
        f"minted={summary['minted']} assets={len(registry)}"
    )
    return 0


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _require_timestamp(value: object) -> datetime:
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"invalid timestamp {value!r}")
    return parsed


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
