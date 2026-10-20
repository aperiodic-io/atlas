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
import math
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
from integrations.cmc_id_probe import CmcProbeError, fetch_cmc_catalogue
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
    "bitget-spot": "bitget",
    "bitget-perps-usdt": "bitget",
    "bitget-perps-usdc": "bitget",
    "bitget-perps-coin": "bitget",
    "bitget-futures-coin": "bitget",
    "gateio-spot": "gate",
    "gateio-perps-usdt": "gate",
    "gateio-perps-btc": "gate",
    "gateio-futures-usdt": "gate",
    "coinbase-spot": "coinbase",
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

# A numeric prefix that scales a contract (1000PEPE, 1MBABYDOGE). Letter forms
# (kPEPE, MBABYDOGE) are only multipliers when the rest is the asset's ticker,
# which ``contract_multiplier`` checks; tables that cannot, use this form only.
_NUMERIC_MULTIPLIER_PREFIX = re.compile(r"(10{3,}|1M)([A-Z].*)")
_NON_CRYPTO_UNDERLYINGS = ("equity", "commodity", "index")
# Underlyings no crypto token may stand in for. Forex pairs and pre-IPO
# (`pre_market`) contracts get no Atlas reference asset either: they stay
# unresolved rather than take a same-ticker token.
_NOT_A_TOKEN_UNDERLYINGS = (*_NON_CRYPTO_UNDERLYINGS, "forex", "pre_market")
# Venues' own test listings (OKX's TEST002-USD_UM_XPERP-310822): no asset.
_TEST_MARKET_TICKER = re.compile(r"TEST\d*")
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)
_FAR_PAST = datetime.min.replace(tzinfo=UTC)


# CoinMarketCap's fiat currency IDs, verified against its detail endpoint. A
# quote currency a venue has stopped listing pairs in (RUB on Binance) has no
# live CMC evidence left, but its ISO code still names exactly one currency.
FIAT_CMC_IDS: dict[str, tuple[int, str]] = {
    "USD": (2781, "United States Dollar"), "AUD": (2782, "Australian Dollar"),
    "BRL": (2783, "Brazilian Real"), "CAD": (2784, "Canadian Dollar"),
    "CHF": (2785, "Swiss Franc"), "CLP": (2786, "Chilean Peso"),
    "CNY": (2787, "Chinese Yuan"), "CZK": (2788, "Czech Koruna"),
    "DKK": (2789, "Danish Krone"), "EUR": (2790, "Euro"),
    "GBP": (2791, "Pound Sterling"), "HKD": (2792, "Hong Kong Dollar"),
    "HUF": (2793, "Hungarian Forint"), "IDR": (2794, "Indonesian Rupiah"),
    "ILS": (2795, "Israeli New Shekel"), "INR": (2796, "Indian Rupee"),
    "JPY": (2797, "Japanese Yen"), "KRW": (2798, "South Korean Won"),
    "MXN": (2799, "Mexican Peso"), "MYR": (2800, "Malaysian Ringgit"),
    "NOK": (2801, "Norwegian Krone"), "NZD": (2802, "New Zealand Dollar"),
    "PHP": (2803, "Philippine Peso"), "PKR": (2804, "Pakistani Rupee"),
    "PLN": (2805, "Polish Złoty"), "RUB": (2806, "Russian Ruble"),
    "SEK": (2807, "Swedish Krona"), "SGD": (2808, "Singapore Dollar"),
    "THB": (2809, "Thai Baht"), "TRY": (2810, "Turkish Lira"),
    "TWD": (2811, "New Taiwan Dollar"), "ZAR": (2812, "South African Rand"),
    "AED": (2813, "United Arab Emirates Dirham"), "BGN": (2814, "Bulgarian Lev"),
    "RON": (2817, "Romanian Leu"), "NGN": (2819, "Nigerian Naira"),
    "COP": (2820, "Colombian Peso"), "ARS": (2821, "Argentine Peso"),
    "PEN": (2822, "Peruvian Sol"), "VND": (2823, "Vietnamese Dong"),
    "UAH": (2824, "Ukrainian Hryvnia"), "KZT": (3551, "Kazakhstani Tenge"),
}


def fiat_detail(code: str) -> CmcAssetDetail | None:
    entry = FIAT_CMC_IDS.get(code)
    if entry is None:
        return None
    cmc_id, name = entry
    return CmcAssetDetail(
        cmc_id=cmc_id,
        name=name,
        symbol=code,
        slug=name.lower().replace(" ", "-"),
        kind="fiat",
        status="active",
        date_added=None,
        rank=None,
        price_usd=None,
        categories=(),
    )


class Match(StrEnum):
    override = "override"
    cmc_ledger = "cmc_ledger"
    cmc_market_pair = "cmc_market_pair"
    snapshot = "snapshot_cmc_id"
    venue_lineage = "venue_lineage"
    cross_venue = "cross_venue_consensus"
    venue_reference = "venue_reference"
    venue_majority = "venue_majority"


DIRECT_MATCHES = frozenset(
    {
        Match.override,
        Match.cmc_ledger,
        Match.cmc_market_pair,
        Match.venue_majority,
        Match.snapshot,
    }
)
# Inferred matches: recomputed every run, never read back as evidence.
INFERRED_MATCHES = frozenset({Match.venue_lineage, Match.cross_venue, Match.venue_reference})
# Direct evidence with known provenance. A legacy ``cmc_id`` of unknown origin
# (``snapshot_cmc_id``) is direct too, but yields to these when they disagree.
STRONG_MATCHES = frozenset({Match.override, Match.cmc_ledger, Match.cmc_market_pair})


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
class TickerEra:
    """A ticker that meant another asset until ``until``, on every venue.

    Exchanges re-ticker an asset when its old ticker is handed to a successor:
    Terra's LUNA became LUNC (and UST became USTC) when Terra 2.0 took ``LUNA``.
    Rows that stopped trading before ``until`` keep the old ticker but are the
    old asset; rows that traded across it changed asset under one ticker.
    """

    ticker: str
    cmc_id: int
    until: datetime
    reason: str

    def applies_to(self, instrument: Instrument) -> bool:
        return instrument.symbol == self.ticker and instrument.first_capture < self.until

    def contains(self, instrument: Instrument) -> bool:
        return instrument.end_date is not None and instrument.end_date <= self.until


@dataclass(frozen=True)
class VenueTicker:
    """What a ticker means on one venue family, as base, quote and margin.

    For tickers two assets share where CMC's evidence from other venues points
    at the wrong one: Binance listed Paxos's renamed PAX as ``USDP``, while
    Gate's ``USDP`` is an unrelated stablecoin. A row override still wins.
    """

    family: str
    ticker: str
    cmc_id: int
    reason: str


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
    # (venue family, ticker, CMC IDs) where CMC lists one live ticker on one
    # venue under several assets and no listing outvotes the others.
    venue_ticker_conflicts: list[tuple[str, str, tuple[int, ...]]] = field(default_factory=list)
    rejected_reused_tickers: list[tuple[str, str, int]] = field(default_factory=list)
    missing_details: set[int] = field(default_factory=set)
    unresolved_active: list[tuple[str, str, str]] = field(default_factory=list)
    pair_symbol_mismatches: list[tuple[str, str, str, int]] = field(default_factory=list)
    # (exchange, id, CMC ID the pair named, pair price / asset price)
    pair_price_mismatches: list[tuple[str, str, int, float]] = field(default_factory=list)
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
            "venue_ticker_conflicts": len(self.venue_ticker_conflicts),
            "rejected_reused_tickers": len(self.rejected_reused_tickers),
            "missing_details": len(self.missing_details),
            "unresolved_active": len(self.unresolved_active),
            "pair_symbol_mismatches": len(self.pair_symbol_mismatches),
            "pair_price_mismatches": len(self.pair_price_mismatches),
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
    payload = json.loads(path.read_text(encoding="utf-8"))
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
                    sorted(
                        (
                            AssetSegment(int(segment["cmc_id"]), _require_timestamp(segment["until"]))
                            for segment in row.get("history", [])
                        ),
                        key=lambda segment: segment.until,
                    )
                ),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ValueError(f"malformed symbology override {row!r}: {error}") from error
        key = (override.exchange, override.original_id)
        if key in overrides:
            raise ValueError(f"duplicate symbology override for {key}")
        overrides[key] = override
    return overrides


def load_ticker_eras(path: Path) -> tuple[TickerEra, ...]:
    if not path.exists():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != OVERRIDES_SCHEMA_VERSION:
        raise ValueError("unsupported symbology overrides schema")
    eras: list[TickerEra] = []
    for row in payload.get("ticker_eras", []):
        try:
            eras.append(
                TickerEra(
                    ticker=str(row["ticker"]).upper(),
                    cmc_id=int(row["cmc_id"]),
                    until=_require_timestamp(row["until"]),
                    reason=str(row["reason"]),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"malformed ticker era {row!r}: {error}") from error
    return tuple(sorted(eras, key=lambda era: (era.ticker, era.until)))


def load_venue_tickers(path: Path) -> tuple[VenueTicker, ...]:
    if not path.exists():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != OVERRIDES_SCHEMA_VERSION:
        raise ValueError("unsupported symbology overrides schema")
    families = set(VENUE_FAMILIES.values())
    pins: dict[tuple[str, str], VenueTicker] = {}
    for row in payload.get("venue_tickers", []):
        try:
            pin = VenueTicker(
                family=str(row["family"]),
                ticker=str(row["ticker"]).upper(),
                cmc_id=int(row["cmc_id"]),
                reason=str(row["reason"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"malformed venue ticker {row!r}: {error}") from error
        if pin.family not in families:
            raise ValueError(f"venue ticker for unknown venue family {pin.family!r}")
        if (pin.family, pin.ticker) in pins:
            raise ValueError(f"duplicate venue ticker for {(pin.family, pin.ticker)}")
        pins[(pin.family, pin.ticker)] = pin
    return tuple(pins.values())


def _pinned_tickers(venue_tickers: Iterable[VenueTicker]) -> dict[tuple[str, str], int]:
    return {(pin.family, pin.ticker): pin.cmc_id for pin in venue_tickers}


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
    ticker_eras: Iterable[TickerEra] = (),
    rejected_pairs: frozenset[tuple[str, str, int]] = frozenset(),
    venue_tickers: Iterable[VenueTicker] = (),
) -> dict[tuple[str, str], BaseResolution]:
    pinned = _pinned_tickers(venue_tickers)
    eras_by_ticker: dict[str, list[TickerEra]] = defaultdict(list)
    for era in ticker_eras:
        eras_by_ticker[era.ticker].append(era)

    resolutions: dict[tuple[str, str], BaseResolution] = {}
    excluded: set[tuple[str, str]] = set()
    spanning: dict[tuple[str, str], list[TickerEra]] = {}
    for instrument in instruments:
        eras = [era for era in eras_by_ticker.get(instrument.symbol, ()) if era.applies_to(instrument)]
        override = overrides.get(instrument.key)
        if override is None and (pin := pinned.get((instrument.family, instrument.symbol))) is not None:
            resolutions[instrument.key] = BaseResolution(pin, Match.override)
            continue
        if override is None and (closed := next((e for e in eras if e.contains(instrument)), None)):
            resolutions[instrument.key] = BaseResolution(closed.cmc_id, Match.override)
            continue
        if eras and override is None:
            spanning[instrument.key] = eras
        resolution = _direct_resolution(
            instrument, pair_matches, ledger, overrides, report, rejected_pairs
        )
        if resolution is None:
            if override is not None and override.cmc_id is None:
                excluded.add(instrument.key)
            continue
        resolutions[instrument.key] = resolution
    _attach_era_history(resolutions, spanning)
    _settle_venue_ticker_conflicts(instruments, resolutions, report)
    _overrule_contradicted_legacy_ids(instruments, resolutions, details, report, spanning)

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
        group_key=_lineage_key,
        same_family=True,
        spanning=spanning,
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
        spanning=spanning,
    )
    _attach_era_history(resolutions, spanning)
    # Keep one entry per row that inference left unresolved.
    report.lineage_conflicts = list(
        {
            (exchange, original_id): (exchange, original_id, cmc_ids)
            for exchange, original_id, cmc_ids in report.lineage_conflicts
            if (exchange, original_id) not in resolutions
        }.values()
    )
    return resolutions


def _attach_era_history(
    resolutions: dict[tuple[str, str], BaseResolution],
    spanning: Mapping[tuple[str, str], list[TickerEra]],
) -> None:
    """Record the earlier asset of rows that traded across a ticker change."""
    for key, eras in spanning.items():
        resolution = resolutions.get(key)
        if resolution is None or resolution.history:
            continue
        history = tuple(
            AssetSegment(era.cmc_id, era.until) for era in eras if era.cmc_id != resolution.cmc_id
        )
        if history:
            resolutions[key] = BaseResolution(resolution.cmc_id, resolution.method, history)


def _direct_resolution(
    instrument: Instrument,
    pair_matches: Mapping[tuple[str, str], MarketPair],
    ledger: Mapping[tuple[str, str, datetime], tuple[int, bool]],
    overrides: Mapping[tuple[str, str], Override],
    report: SymbologyReport,
    rejected_pairs: frozenset[tuple[str, str, int]] = frozenset(),
) -> BaseResolution | None:
    override = overrides.get(instrument.key)
    if override is not None:
        if override.cmc_id is None:
            return None
        return BaseResolution(override.cmc_id, Match.override, override.history)

    # Ranked by source, so a stored match ranks where its source would: a
    # rebuild without CMC then labels every row exactly as the last one did.
    candidates: list[tuple[int, int, Match]] = []
    ledger_hit = ledger.get(
        (instrument.exchange, instrument.original_id, instrument.first_capture)
    )
    if ledger_hit is not None:
        candidates.append((0 if ledger_hit[1] else 2, ledger_hit[0], Match.cmc_ledger))
    if (pair := pair_matches.get(instrument.key)) is not None:
        candidates.append((1, pair.base_cmc_id, Match.cmc_market_pair))
    stored = _stored_direct_match(instrument.row)
    # A pair the price check rejects today discards the match it left before.
    if stored is not None and (*instrument.key, stored[0]) in rejected_pairs:
        stored = None
    if stored is not None:
        candidates.append((_EVIDENCE_RANK[stored[1]], stored[0], stored[1]))
    if not candidates:
        return None
    candidates = [(cmc_id, method) for _, cmc_id, method in sorted(candidates, key=lambda c: c[0])]

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


_EVIDENCE_RANK = {
    Match.cmc_ledger: 2,
    Match.cmc_market_pair: 1,
    Match.venue_majority: 1,
    Match.snapshot: 3,
}


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


def _anchor_index(
    instruments: Iterable[Instrument],
    resolutions: Mapping[tuple[str, str], BaseResolution],
    methods: frozenset[Match],
    group_key,
) -> dict[tuple, list[Instrument]]:
    anchors_by_group: dict[tuple, list[Instrument]] = defaultdict(list)
    for instrument in instruments:
        resolution = resolutions.get(instrument.key)
        if resolution is not None and resolution.method in methods:
            anchors_by_group[group_key(instrument)].append(instrument)
    return anchors_by_group


def _lineage_key(instrument: Instrument) -> tuple:
    """Same venue, same ticker.

    Hyperliquid's HIP-3 dexes list the ticker of the asset they track after a
    ``dex:`` prefix, so ``cash:ETH`` and ``flx:ETH`` are grouped. The main dex
    is not: a deployer of equities lists Seagate as ``para:STX``, which is not
    the main dex's Stacks.
    """
    if instrument.family == "hyperliquid-perps" and ":" in instrument.symbol:
        return ("hyperliquid-perps:hip3", instrument.symbol.split(":", 1)[1])
    return (instrument.family, instrument.symbol)


def _consensus_votes(
    instrument: Instrument,
    anchors: Iterable[Instrument],
    resolutions: Mapping[tuple[str, str], BaseResolution],
    same_family: bool,
    spanning: Mapping[tuple[str, str], list[TickerEra]] | None,
) -> dict[str, set[int]]:
    """The CMC IDs that anchors trading alongside ``instrument`` resolve to, by venue."""
    # A row that traded across a ticker change is identified by what the
    # ticker means *after* it, so anchors that ended before do not count.
    eras = (spanning or {}).get(instrument.key, ())
    after = max((era.until for era in eras), default=None)
    votes: dict[str, set[int]] = defaultdict(set)
    for anchor in anchors:
        if (
            anchor.key != instrument.key
            and anchor.overlaps(instrument)
            and (same_family or anchor.family != instrument.family)
            and (after is None or anchor.end_date is None or anchor.end_date > after)
        ):
            votes[anchor.family].add(_cmc_id_at(resolutions[anchor.key], instrument))
    return votes


def _consensus(
    instrument: Instrument,
    anchors: Iterable[Instrument],
    resolutions: Mapping[tuple[str, str], BaseResolution],
    same_family: bool,
    spanning: Mapping[tuple[str, str], list[TickerEra]] | None,
) -> set[int]:
    votes = _consensus_votes(instrument, anchors, resolutions, same_family, spanning)
    return set().union(*votes.values()) if votes else set()


# Cross-venue consensus is decided by venues, not listings: each venue votes for
# the one asset its listings agree on, and the winner needs this share of the
# votes and at least two venues behind it. One venue whose CMC listing names a
# same-ticker impostor cannot then block, or win, a consensus.
CROSS_VENUE_MAJORITY = 0.75


def _cross_venue_winner(votes: Mapping[str, set[int]]) -> int | None:
    ballots = Counter(next(iter(ids)) for ids in votes.values() if len(ids) == 1)
    if not ballots:
        return None
    winner, count = ballots.most_common(1)[0]
    if count >= 2 and count >= CROSS_VENUE_MAJORITY * len(votes):
        return winner
    return None


def _listed_after_ended(
    instrument: Instrument, cmc_id: int, details: Mapping[int, CmcAssetDetail]
) -> bool:
    """Whether CMC added the asset only after the instrument stopped trading.

    Only a live asset's listing date is trusted: CMC re-dates assets it retires
    (Fantom, migrated to Sonic, is "added" 2024-05-01), and a ticker's reuser
    is by its nature the live asset.
    """
    detail = details.get(cmc_id)
    return (
        detail is not None
        and detail.status in ("", "active")
        and detail.date_added is not None
        and instrument.end_date is not None
        and instrument.end_date < detail.date_added
    )


def _settle_venue_ticker_conflicts(
    instruments: list[Instrument],
    resolutions: dict[tuple[str, str], BaseResolution],
    report: SymbologyReport,
) -> None:
    """Make one live ticker on one venue mean one asset, when the venue says which.

    CMC sometimes files one of a venue's markets for a ticker under a 1:1 twin
    of the asset its other markets trade (Wrapped Solana for one SOL pair, the
    bridged ethDYDX for one DYDX perpetual) -- twins a price check cannot tell
    apart. Each of the venue's snapshots votes for the asset most of its live
    rows of the ticker are listed under; an asset with a strict majority of the
    snapshots' votes is the ticker's meaning, and the venue's other live rows
    follow it. A tie is left as CMC lists it and reported.
    """
    groups: dict[tuple[str, str], list[Instrument]] = defaultdict(list)
    for instrument in instruments:
        resolution = resolutions.get(instrument.key)
        if (
            instrument.active
            and resolution is not None
            and resolution.method is Match.cmc_market_pair
        ):
            groups[(instrument.family, instrument.symbol)].append(instrument)
    for (family, ticker), members in sorted(groups.items()):
        cmc_ids = {resolutions[member.key].cmc_id for member in members}
        if len(cmc_ids) < 2:
            continue
        by_snapshot: dict[str, Counter] = defaultdict(Counter)
        for member in members:
            by_snapshot[member.exchange][resolutions[member.key].cmc_id] += 1
        ballots = Counter()
        for counts in by_snapshot.values():
            (top, top_count), *rest = counts.most_common()
            if not rest or rest[0][1] < top_count:
                ballots[top] += 1
        winner = None
        if ballots:
            (leader, votes), *others = ballots.most_common()
            if votes * 2 > len(by_snapshot) and (not others or others[0][1] < votes):
                winner = leader
        if winner is None:
            report.venue_ticker_conflicts.append((family, ticker, tuple(sorted(cmc_ids))))
            continue
        for member in members:
            current = resolutions[member.key]
            if current.cmc_id == winner:
                continue
            report.disagreements.append(
                (
                    member.exchange,
                    member.original_id,
                    winner,
                    Match.venue_majority.value,
                    current.cmc_id,
                    Match.cmc_market_pair.value,
                )
            )
            resolutions[member.key] = BaseResolution(winner, Match.venue_majority, current.history)


def _overrule_contradicted_legacy_ids(
    instruments: list[Instrument],
    resolutions: dict[tuple[str, str], BaseResolution],
    details: Mapping[int, CmcAssetDetail],
    report: SymbologyReport,
    spanning: Mapping[tuple[str, str], list[TickerEra]],
) -> None:
    """Replace a legacy ``cmc_id`` that the venue's own identified listings refute.

    An exchange does not list two assets under one ticker at the same time, so
    when every strongly identified listing of the ticker on the venue during
    the row's lifetime names one asset and the row's legacy ID names another,
    the legacy ID is the error.
    """

    def group_key(instrument: Instrument) -> tuple:
        return (instrument.family, instrument.symbol)

    anchors_by_group = _anchor_index(instruments, resolutions, STRONG_MATCHES, group_key)
    for instrument in instruments:
        resolution = resolutions.get(instrument.key)
        if resolution is None or resolution.method is not Match.snapshot:
            continue
        cmc_ids = _consensus(
            instrument,
            anchors_by_group.get(group_key(instrument), ()),
            resolutions,
            same_family=True,
            spanning=spanning,
        )
        if len(cmc_ids) != 1:
            continue
        (cmc_id,) = cmc_ids
        if cmc_id == resolution.cmc_id or _listed_after_ended(instrument, cmc_id, details):
            continue
        report.disagreements.append(
            (
                instrument.exchange,
                instrument.original_id,
                cmc_id,
                Match.venue_majority.value,
                resolution.cmc_id,
                Match.snapshot.value,
            )
        )
        resolutions[instrument.key] = BaseResolution(cmc_id, Match.venue_majority)


def _infer(
    unresolved: list[Instrument],
    instruments: list[Instrument],
    resolutions: dict[tuple[str, str], BaseResolution],
    details: Mapping[int, CmcAssetDetail],
    report: SymbologyReport,
    method: Match,
    group_key,
    same_family: bool,
    spanning: Mapping[tuple[str, str], list[TickerEra]] | None = None,
) -> None:
    anchors_by_group = _anchor_index(instruments, resolutions, DIRECT_MATCHES, group_key)
    inferred: dict[tuple[str, str], BaseResolution] = {}
    for instrument in unresolved:
        votes = _consensus_votes(
            instrument,
            anchors_by_group.get(group_key(instrument), ()),
            resolutions,
            same_family=same_family,
            spanning=spanning,
        )
        if not votes:
            continue
        cmc_ids = set().union(*votes.values())
        cmc_id = (
            next(iter(cmc_ids)) if len(cmc_ids) == 1 else None
        ) if same_family else _cross_venue_winner(votes)
        if cmc_id is None:
            report.lineage_conflicts.append(
                (instrument.exchange, instrument.original_id, tuple(sorted(cmc_ids)))
            )
            continue
        if _wraps_declared_underlying(instrument, details.get(cmc_id)):
            continue
        if _listed_after_ended(instrument, cmc_id, details):
            report.rejected_reused_tickers.append(
                (instrument.exchange, instrument.original_id, cmc_id)
            )
            continue
        inferred[instrument.key] = BaseResolution(cmc_id, method)
    resolutions.update(inferred)


def _wraps_declared_underlying(instrument: Instrument, detail: CmcAssetDetail | None) -> bool:
    """Whether inference would hand a TradFi derivative a token instead of its reference.

    Other venues' CMC listings sometimes name a tokenized wrapper (an Ondo
    stock token) for a ticker; a row whose venue declares the underlying an
    equity, index, commodity, forex pair or pre-IPO company tracks a reference
    price, not the token.
    """
    return (
        detail is not None
        and instrument.row.get("underlying") in _NOT_A_TOKEN_UNDERLYINGS
        and not is_reference_detail(detail)
    )


def is_reference_detail(detail: CmcAssetDetail) -> bool:
    """Whether a CMC asset is the reference price of a TradFi asset, not a token."""
    return detail.name.endswith("(Derivatives)") or "TradFi Assets (Derivatives)" in detail.tags


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
    # Curated meanings (``venue_tickers``) outrank every vote.
    pinned: dict[tuple[str, str], int] = field(default_factory=dict)
    family_quotes: dict[tuple[str, str], Counter] = field(default_factory=lambda: defaultdict(Counter))
    global_quotes: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    family_bases: dict[tuple[str, str], Counter] = field(default_factory=lambda: defaultdict(Counter))
    global_bases: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    # Identified base assets of delisted rows too: BUSD has no live listing left.
    family_bases_ever: dict[tuple[str, str], Counter] = field(default_factory=lambda: defaultdict(Counter))
    global_bases_ever: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))

    def resolve(self, family: str, ticker: str | None) -> int | None:
        if not ticker:
            return None
        if (pinned := self.pinned.get((family, ticker))) is not None:
            return pinned
        for votes in (
            self.family_quotes.get((family, ticker)),
            self.family_bases.get((family, ticker)),
            self.global_quotes.get(ticker),
            self.global_bases.get(ticker),
        ):
            if winner := _dominant(votes):
                return winner
        if ticker in FIAT_CMC_IDS:
            return FIAT_CMC_IDS[ticker][0]
        for votes in (
            self.family_bases_ever.get((family, ticker)),
            self.global_bases_ever.get(ticker),
        ):
            if winner := _dominant(votes):
                return winner
        return None


def build_ticker_tables(
    instruments: Iterable[Instrument],
    pair_matches: Mapping[tuple[str, str], MarketPair],
    resolutions: Mapping[tuple[str, str], BaseResolution],
    registry: AssetRegistry | None = None,
    venue_tickers: Iterable[VenueTicker] = (),
) -> TickerTables:
    """Learn what each quote ticker means, per venue and across venues.

    Votes come from CMC's quote IDs on matched pairs and, so that a rebuild
    without CMC reproduces the last one, from the quote assets rows already
    carry. A venue's identified base assets cover tickers CMC never quotes.
    """
    tables = TickerTables(pinned=_pinned_tickers(venue_tickers))
    for instrument in instruments:
        quote_cmc_id = _quote_evidence(instrument, pair_matches.get(instrument.key), registry)
        if quote_cmc_id is not None and instrument.denominator:
            tables.family_quotes[(instrument.family, instrument.denominator)][quote_cmc_id] += 1
            tables.global_quotes[instrument.denominator][quote_cmc_id] += 1
        resolution = resolutions.get(instrument.key)
        if (
            resolution is not None
            and resolution.method in DIRECT_MATCHES
            and _NUMERIC_MULTIPLIER_PREFIX.fullmatch(instrument.symbol) is None
        ):
            tables.family_bases_ever[(instrument.family, instrument.symbol)][resolution.cmc_id] += 1
            tables.global_bases_ever[instrument.symbol][resolution.cmc_id] += 1
            if instrument.active:
                tables.family_bases[(instrument.family, instrument.symbol)][resolution.cmc_id] += 1
                tables.global_bases[instrument.symbol][resolution.cmc_id] += 1
    return tables


def _quote_evidence(
    instrument: Instrument, pair: MarketPair | None, registry: AssetRegistry | None
) -> int | None:
    if (
        pair is not None
        and pair.quote_cmc_id is not None
        and instrument.denominator
        and sanitize_ticker(pair.quote_symbol) == sanitize_ticker(instrument.denominator)
    ):
        return pair.quote_cmc_id
    stored = instrument.row.get("quote_asset_id")
    if registry is not None and isinstance(stored, str):
        asset = registry.get(stored)
        if asset is not None and asset.cmc_id is not None:
            return asset.cmc_id
    return None


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
    """What the row's quote ticker means on its venue.

    The venue-wide meaning wins over one pair's quote ID: CMC occasionally
    labels a pair's quote wrongly (Binance's ``HK0625USDT`` "quoted in HKD").
    """
    resolved = tables.resolve(instrument.family, instrument.denominator)
    if resolved is not None:
        return resolved
    if (
        pair is not None
        and pair.quote_cmc_id is not None
        and instrument.denominator
        and sanitize_ticker(pair.quote_symbol) == sanitize_ticker(instrument.denominator)
    ):
        return pair.quote_cmc_id
    return None


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

    ``1000PEPE`` is 1,000 PEPE, as are ``kPEPE`` and Bybit's ``SHIB1000``-style
    suffix; ``1MBABYDOGE`` and Gate's ``MBABYDOGE`` are a million. A ticker that
    is the asset's own (or an alias) has none.
    """
    tickers = {asset.ticker, *asset.aliases}
    if sanitize_ticker(symbol.split(":", 1)[-1]) in {sanitize_ticker(t) for t in tickers}:
        return None
    if (scale := contract_multiplier(symbol, tickers)) is not None:
        return scale
    # A venue's numeric prefix declares the multiplier even when the rest is not
    # quite the asset's ticker (Bybit's 1000NEIROCTO for Neiro).
    return _numeric_prefix(symbol)


def _numeric_prefix(symbol: str) -> int | None:
    match = _NUMERIC_MULTIPLIER_PREFIX.fullmatch(sanitize_ticker(symbol.split(":", 1)[-1]))
    if match is None:
        return None
    return 1_000_000 if match.group(1) == "1M" else int(match.group(1))


_SCALE_WORDS = {"K": 1000, "M": 1_000_000, "1M": 1_000_000}


def contract_multiplier(symbol: str, tickers: Iterable[str]) -> int | None:
    names = {sanitize_ticker(name) for name in tickers if name}
    ticker = sanitize_ticker(symbol.split(":", 1)[-1])
    if not ticker or ticker in names:
        return None
    for name in names:
        if not name or (not ticker.endswith(name) and not ticker.startswith(name)):
            continue
        if ticker.endswith(name):
            prefix = ticker[: -len(name)]
            if re.fullmatch(r"10{3,}", prefix):
                return int(prefix)
            if prefix in _SCALE_WORDS:
                return _SCALE_WORDS[prefix]
        if ticker.startswith(name):
            suffix = ticker[len(name) :]
            if re.fullmatch(r"10{3,}", suffix):
                return int(suffix)
    return None


# ---------------------------------------------------------------------------
# Asset registry
# ---------------------------------------------------------------------------


def classify_asset(detail: CmcAssetDetail, underlyings: Counter) -> AssetClass:
    if detail.is_fiat:
        return AssetClass.fiat
    if not is_reference_detail(detail):
        return AssetClass.crypto
    if " index" in f" {detail.name.lower()}":
        return AssetClass.index
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
                cmc_date_added=_iso_or_none(detail.date_added),
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
                cmc_date_added=_iso_or_none(detail.date_added) or existing.cmc_date_added,
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
    ticker_eras: Iterable[TickerEra] = (),
    asset_prices: Mapping[int, float] | None = None,
    venue_tickers: Iterable[VenueTicker] = (),
) -> SymbologyReport:
    """Resolve, mint and write symbology fields onto ``rows_by_exchange`` in place."""
    report = SymbologyReport(generated_at=now or datetime.now(UTC))
    pair_matches = pair_result.matches if pair_result is not None else {}
    details = details or {}
    if pair_result is not None:
        _record_pair_diagnostics(report, pair_result)

    instruments = instruments_from_rows(rows_by_exchange)
    # The guards need each candidate's listing date, status and kind; fall back
    # to what the registry recorded so an offline rebuild decides the same way.
    facts = {**_registry_facts(registry), **details}
    pair_matches, rejected_pairs = _price_checked(
        instruments, pair_matches, asset_prices or {}, facts, registry, report
    )
    resolutions = resolve_base_assets(
        instruments,
        pair_matches,
        ledger or {},
        overrides or {},
        facts,
        report,
        ticker_eras,
        rejected_pairs,
        venue_tickers,
    )
    tables = build_ticker_tables(instruments, pair_matches, resolutions, registry, venue_tickers)

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

    minting_details = dict(details)
    for code, (cmc_id, _) in FIAT_CMC_IDS.items():
        if cmc_id in usage and cmc_id not in minting_details and registry.by_cmc_id(cmc_id) is None:
            minting_details[cmc_id] = fiat_detail(code)
    ensure_assets(registry, usage, underlyings, minting_details, report)
    _record_aliases(registry, instruments, resolutions)

    for instrument in instruments:
        base_cmc_id, quote_cmc_id, margin_cmc_id = legs[instrument.key]
        resolution = resolutions.get(instrument.key)
        base = registry.by_cmc_id(base_cmc_id) if base_cmc_id is not None else None
        method = resolution.method if resolution is not None else None
        history = resolution.history if resolution is not None else ()
        if resolution is None and instrument.key not in (overrides or {}):
            base = _reference_asset(registry, instrument, report)
            method = Match.venue_reference if base is not None else None
        margin = registry.by_cmc_id(margin_cmc_id) if margin_cmc_id is not None else None
        if margin is None and base is not None and instrument.margin == instrument.symbol:
            margin = base
        _write_row(
            instrument,
            registry,
            base if method is not None else None,
            method,
            history,
            registry.by_cmc_id(quote_cmc_id) if quote_cmc_id is not None else None,
            margin if instrument.type != "spot" else None,
            report,
            clear_cmc_id=_withdrawn_cmc_id(instrument, overrides, rejected_pairs),
        )
    _classify_unknown_references(registry, rows_by_exchange)
    return report


def _withdrawn_cmc_id(
    instrument: Instrument,
    overrides: Mapping[tuple[str, str], Override] | None,
    rejected_pairs: frozenset[tuple[str, str, int]],
) -> bool:
    """Whether a row's cmc_id lost its evidence and must not outlive this build.

    An override can withhold an asset, and today's price check can reject the
    CMC listing an earlier build took the cmc_id from. A cmc_id that predates
    symbology is the row's own record and is never withdrawn by a price check.
    """
    override = (overrides or {}).get(instrument.key)
    if override is not None and override.cmc_id is None:
        return True
    cmc_id = instrument.row.get("cmc_id")
    return (
        instrument.row.get("base_asset_match") == Match.cmc_market_pair.value
        and isinstance(cmc_id, int)
        and (*instrument.key, cmc_id) in rejected_pairs
    )


def _classify_unknown_references(
    registry: AssetRegistry, rows_by_exchange: Mapping[str, list[dict]]
) -> None:
    """Give a CMC reference asset of unknown class the class its venues declare."""
    declared: dict[str, Counter] = defaultdict(Counter)
    for rows in rows_by_exchange.values():
        for row in rows:
            if (asset_id := row.get("base_asset_id")) and row.get("underlying") in _NON_CRYPTO_UNDERLYINGS:
                declared[asset_id][row["underlying"]] += 1
    for asset_id, counts in declared.items():
        asset = registry.get(asset_id)
        if asset is not None and asset.asset_class is AssetClass.unknown:
            registry.update(asset_id, asset_class=AssetClass(counts.most_common(1)[0][0]))


def _record_pair_diagnostics(report: SymbologyReport, pair_result: MatchResult) -> None:
    report.pair_symbol_mismatches = [
        (key[0], key[1], pair.base_symbol, pair.base_cmc_id)
        for key, pair in pair_result.symbol_mismatches
    ]
    report.pair_conflicts = list(pair_result.conflicts)
    for pair in pair_result.unmatched:
        report.pairs_unmatched[f"{pair.venue}/{pair.category}"] += 1


# A CMC pair is rejected when its price and its asset's price differ by more
# than this factor (after any contract multiplier). Same-asset pairs agree to a
# few percent on liquid venues and within a few times on illiquid ones; the
# misfiled pairs this catches are 13x to 10^8x apart.
PAIR_PRICE_TOLERANCE = 10.0


def _price_checked(
    instruments: Iterable[Instrument],
    pair_matches: Mapping[tuple[str, str], MarketPair],
    asset_prices: Mapping[int, float],
    facts: Mapping[int, CmcAssetDetail],
    registry: AssetRegistry,
    report: SymbologyReport,
) -> tuple[dict[tuple[str, str], MarketPair], frozenset[tuple[str, str, int]]]:
    """Drop CMC pairs whose own price contradicts the asset they name.

    CMC occasionally files a venue's market under a same-ticker impostor (Gate's
    DOGE perpetual under a 120x cheaper "DOGE" token). The pair's price comes
    from the venue, the asset's from CMC's aggregate, so a large gap means the
    pair names the wrong asset -- whatever the ticker says.
    """
    symbols = {instrument.key: instrument.symbol for instrument in instruments}
    checked: dict[tuple[str, str], MarketPair] = {}
    rejected: set[tuple[str, str, int]] = set()

    def tickers(cmc_id: int) -> set[str]:
        names = {facts[cmc_id].symbol} if cmc_id in facts else set()
        if (asset := registry.by_cmc_id(cmc_id)) is not None:
            names |= {asset.ticker, *asset.aliases}
        return names

    for key, pair in pair_matches.items():
        reference = asset_prices.get(pair.base_cmc_id)
        if reference is None and (detail := facts.get(pair.base_cmc_id)) is not None:
            reference = detail.price_usd
        if not pair.price_usd or not reference:
            checked[key] = pair
            continue
        ratio = pair.price_usd / reference
        symbol = symbols.get(key, "")
        scales = {
            1.0,
            float(contract_multiplier(symbol, tickers(pair.base_cmc_id)) or 1),
            float(_numeric_prefix(symbol) or 1),
        }
        if any(abs(math.log(ratio / scale)) <= math.log(PAIR_PRICE_TOLERANCE) for scale in scales):
            checked[key] = pair
        else:
            report.pair_price_mismatches.append((key[0], key[1], pair.base_cmc_id, ratio))
            rejected.add((key[0], key[1], pair.base_cmc_id))
    return checked, frozenset(rejected)


def _registry_facts(registry: AssetRegistry) -> dict[int, CmcAssetDetail]:
    """What the registry remembers about each CMC asset, shaped like CMC details."""
    facts: dict[int, CmcAssetDetail] = {}
    for asset in registry:
        if asset.cmc_id is None:
            continue
        reference = asset.asset_class in _REFERENCE_CLASSES
        facts[asset.cmc_id] = CmcAssetDetail(
            cmc_id=asset.cmc_id,
            name=asset.name,
            symbol=asset.ticker,
            slug=asset.cmc_slug or "",
            kind="fiat" if asset.asset_class is AssetClass.fiat else "",
            status=asset.status,
            date_added=_parse_timestamp(asset.cmc_date_added),
            rank=None,
            price_usd=None,
            categories=asset.categories,
            tags=("TradFi Assets (Derivatives)",) if reference else (),
        )
    return facts


def _record_aliases(
    registry: AssetRegistry,
    instruments: Iterable[Instrument],
    resolutions: Mapping[tuple[str, str], BaseResolution],
) -> None:
    """Remember the venue tickers an asset trades under (``BTTC`` for BTT).

    A multiplied ticker (``1000BTTC``, ``kPEPE``) is not an alias but a contract
    size, so it is left out -- and the alias is what lets ``1000BTTC`` be read
    as 1,000 of it. Only direct evidence names aliases: an inferred match is a
    guess, and an alias outlives it.
    """
    aliases: dict[str, set[str]] = defaultdict(set)
    for instrument in instruments:
        resolution = resolutions.get(instrument.key)
        if resolution is None or resolution.method not in DIRECT_MATCHES or resolution.history:
            continue
        asset = registry.by_cmc_id(resolution.cmc_id)
        if asset is None:
            continue
        ticker = sanitize_ticker(instrument.symbol.split(":", 1)[-1])
        own = sanitize_ticker(asset.ticker)
        if not ticker or ticker == own or _NUMERIC_MULTIPLIER_PREFIX.fullmatch(ticker):
            continue
        if contract_multiplier(ticker, {own}) is not None:
            continue
        aliases[asset.asset_id].add(ticker)
    for asset_id, names in aliases.items():
        registry.add_aliases(asset_id, names)


def _reference_asset(
    registry: AssetRegistry, instrument: Instrument, report: SymbologyReport
) -> Asset | None:
    """The TradFi reference asset a derivative tracks, by the venue's own word.

    A venue that labels a perpetual's underlying as an equity, index or
    commodity has named the asset class; with the ticker that identifies the
    reference asset (``AAL`` the stock). A CMC-tracked reference asset of that
    class and ticker is used when there is one; otherwise an Atlas-native asset
    without a CMC ID is minted, once, and found again on later runs. Spot rows
    are tokens wrapping such an asset, not the asset, and are left alone.
    """
    underlying = instrument.row.get("underlying")
    if instrument.type == "spot" or underlying not in _NON_CRYPTO_UNDERLYINGS:
        return None
    asset_class = AssetClass(underlying)
    ticker = instrument.symbol.split(":", 1)[-1]
    if not sanitize_ticker(ticker) or _TEST_MARKET_TICKER.fullmatch(ticker.upper()):
        return None
    candidates = registry.find_by_ticker(ticker)
    # A CMC-tracked reference asset is the one, whatever class the venue or CMC
    # gives it (Bitget calls the S&P 500 an equity); natives must match class.
    tracked = [
        asset
        for asset in candidates
        if asset.cmc_id is not None and asset.asset_class in _REFERENCE_CLASSES
    ]
    native = [
        asset for asset in candidates if asset.cmc_id is None and asset.asset_class is asset_class
    ]
    if tracked:
        return tracked[0] if len(tracked) == 1 else None
    if native:
        return native[0] if len(native) == 1 else None
    asset = registry.mint(
        ticker=ticker, name=ticker, asset_class=asset_class, qualifier=asset_class.value
    )
    report.minted.append(asset.asset_id)
    return asset


_REFERENCE_CLASSES = frozenset(
    {AssetClass.equity, AssetClass.commodity, AssetClass.index, AssetClass.unknown}
)


def _write_row(
    instrument: Instrument,
    registry: AssetRegistry,
    base: Asset | None,
    method: Match | None,
    history: tuple[AssetSegment, ...],
    quote: Asset | None,
    margin: Asset | None,
    report: SymbologyReport,
    clear_cmc_id: bool = False,
) -> None:
    row = instrument.row
    previous_base = row.get("base_asset_id")
    previous_match = row.get("base_asset_match")
    for key in SYMBOLOGY_FIELDS:
        row.pop(key, None)
    if base is None and (clear_cmc_id or previous_match in INFERRED_MATCHES):
        # The CMC ID was this build's own guess (or an override withdrew it):
        # left behind, it would read as legacy evidence next run.
        row.pop("cmc_id", None)
        row.pop("category", None)
    exchange = instrument.exchange
    report.rows[exchange] += 1
    if instrument.active:
        report.active_rows[exchange] += 1

    if base is not None and method is not None:
        _write_base(instrument, registry, base, method, history, report)
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
    if base is not None and method is not None and quote is not None:
        unified = unified_id(instrument, base, quote, margin)
        if unified is not None:
            row["unified_id"] = unified


def _write_base(
    instrument: Instrument,
    registry: AssetRegistry,
    base: Asset,
    method: Match,
    history: tuple[AssetSegment, ...],
    report: SymbologyReport,
) -> None:
    row = instrument.row
    # cmc_id and category survive the strip of symbology fields, so they go
    # first: a rebuild then appends the rest in the same order as a first build.
    # Rows with a cmc_id always carry category, empty or not, as before.
    if base.cmc_id is not None:
        row["cmc_id"] = base.cmc_id
        row["category"] = list(base.categories)
    else:
        row.pop("cmc_id", None)
        row.pop("category", None)
    row["base_asset_id"] = base.asset_id
    row["base_asset_match"] = method.value
    if (multiplier := base_multiplier(instrument.symbol, base)) is not None:
        row["base_multiplier"] = multiplier
    history_rows = [
        {"asset_id": asset.asset_id, "until": _format_timestamp(segment.until)}
        for segment in history
        if (asset := registry.by_cmc_id(segment.cmc_id)) is not None
    ]
    if history_rows:
        row["base_asset_history"] = history_rows
    report.base_resolved[instrument.exchange] += 1
    report.methods[method.value] += 1
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
        "CMC pairs priced unlike the asset they name (not used)",
        report.pair_price_mismatches,
        lambda e: f"`{e[0]}` `{e[1]}`: {asset_name(e[2])} at {e[3]:.3g}x its price",
    )
    section(
        "Rows claimed by conflicting CMC pairs (not used)",
        report.pair_conflicts,
        lambda e: f"`{e[0]}` `{e[1]}`",
    )
    section(
        "One live ticker listed under several assets on one venue (left as CMC lists them)",
        report.venue_ticker_conflicts,
        lambda e: f"`{e[0]}` `{e[1]}`: {', '.join(asset_name(i) for i in e[2])}",
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
        rows = json.loads(path.read_text(encoding="utf-8"))
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
        for row in json.loads(path.read_text(encoding="utf-8")):
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
    # Every snapshot is evidence for every other (lineage, consensus, quote
    # tables), so a subset run resolves against all of them and writes its own.
    rows_by_exchange = load_snapshots(data_dir, SYMBOLOGY_EXCHANGES)
    registry = AssetRegistry.load(registry_path)
    overrides = load_overrides(overrides_path)
    ticker_eras = load_ticker_eras(overrides_path)
    venue_tickers = load_venue_tickers(overrides_path)
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
        asset_prices: dict[int, float] = {}
        if not offline:
            asset_prices = fetch_asset_prices(session)
            if pair_result is not None and not asset_prices:
                # Unchecked pairs would be stored as evidence, impostors included,
                # so without prices this run keeps the evidence already stored.
                print("  Not using CMC market pairs without CMC prices", flush=True)
                pair_result = None

        if not offline:
            wanted = _referenced_cmc_ids(
                rows_by_exchange, pair_result, ledger, overrides, ticker_eras, venue_tickers
            )
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
        ticker_eras=ticker_eras,
        asset_prices=asset_prices,
        venue_tickers=venue_tickers,
    )
    if not dry_run:
        registry.save()
        save_snapshots(
            data_dir,
            {exchange: rows for exchange, rows in rows_by_exchange.items() if exchange in exchanges},
        )
    return report, registry


def fetch_asset_prices(session: requests.Session) -> dict[int, float]:
    """CMC's aggregate USD price for every active asset, for the pair price check.

    A failure only disables the check, so it is reported and swallowed.
    """
    try:
        catalogue = fetch_cmc_catalogue(session)
    except (CmcProbeError, requests.RequestException, ValueError) as error:
        print(f"  CMC prices unavailable, skipping the pair price check: {error}", flush=True)
        return {}
    return {asset.cmc_id: asset.price_usd for asset in catalogue.assets if asset.price_usd > 0}


def _referenced_cmc_ids(
    rows_by_exchange: Mapping[str, list[dict]],
    pair_result: MatchResult | None,
    ledger: Mapping[tuple[str, str, datetime], tuple[int, bool]],
    overrides: Mapping[tuple[str, str], Override],
    ticker_eras: Iterable[TickerEra] = (),
    venue_tickers: Iterable[VenueTicker] = (),
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
    ids.update(era.cmc_id for era in ticker_eras)
    ids.update(pin.cmc_id for pin in venue_tickers)
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
        args.report_path.write_text(rendered, encoding="utf-8")
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


def _iso_or_none(value: datetime | None) -> str | None:
    return None if value is None else _format_timestamp(value)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
