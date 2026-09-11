"""Propose CoinMarketCap IDs for newly listed Binance base assets.

Atlas needs one stable CMC ID per underlying asset, so every symbol that appears
on Binance without a ``cmc_id`` has to be resolved. This module deliberately
scopes itself to *new* assets: rows whose ``first_capture`` is recent, whose
``cmc_id`` is still missing, and whose ticker no earlier run already decided.

Resolution is layered, cheapest and most defensible first:

1. reuse a CMC ID the Binance snapshots already carry for the same ticker;
2. accept a single same-ticker CMC candidate whose price is compatible;
3. otherwise ask an LLM to adjudicate *among the fetched candidates only*.

An LLM answer is never trusted on its own: the chosen ID must be one of the
candidates, and a confident answer is downgraded to ``uncertain`` when the
candidate's price contradicts the exchange observation. Approved IDs are written
to the snapshots; uncertain and unmapped assets are recorded for human review so
one pull request can carry both.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import requests

from integrations.binance import (
    fetch_futures_prices,
    fetch_public_assets,
    fetch_spot_prices,
)
from integrations.cmc_id_probe import (
    CatalogueDiagnostics,
    CmcAsset,
    CmcCatalogue,
    CmcProbeError,
    PriceObservation,
    ProbeStatus,
    candidates_for_symbol,
    classify_price_candidates,
    contract_multiplier_for_cmc_lookup,
    fetch_cmc_catalogue,
    normalize_cmc_lookup_symbol,
    price_observations_from_binance_tickers,
)
from integrations.cmc_mappings import CmcMapping, InstrumentInstance, MappingStore
from integrations.llm import (
    ChatClient,
    LlmConfig,
    LlmConfigurationError,
    LlmError,
    LlmNotConfiguredError,
)


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"
DEFAULT_MAPPING_FILENAME = "cmc_mappings.json"
DEFAULT_EXCHANGES = ("binance-futures",)
DEFAULT_NEW_WITHIN_DAYS = 30
DEFAULT_MAX_RELATIVE_DIFFERENCE = 0.05
DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS = 180
DEFAULT_MAX_LLM_SYMBOLS = 40
DEFAULT_RECHECK_UNMAPPED_AFTER_DAYS = 90
# CMC's keyless listing reliably reports a few more assets than it returns
# unique IDs for -- 8,184 against 8,179 in production, 8,143 against 8,141 during
# the audit -- most likely because its pages overlap and dedup removes the
# repeats. Demanding exact equality therefore blocks every approval forever, so a
# proportionate gap is tolerated while the signals that mean "rows were genuinely
# lost" still fail closed.
DEFAULT_MAX_CATALOGUE_GAP_RATIO = 0.005
# Binance lists a great many tokenized equities, ETFs and index products, and
# they dominate new listings: 59 of the 67 unmapped rows in a 60-day window were
# `equity`. CoinMarketCap does carry entries for some of them, so this is a scope
# choice rather than a correctness one -- Atlas wants CMC IDs for crypto assets,
# and an equity contract's "ID" would be a tokenization wrapper, not the asset.
#
# Rows whose `underlying` is absent are kept: those are legacy crypto rows that
# predate the field (BTC, ADA, BNB). `unknown` is kept too, because silently
# skipping a genuine new listing is worse than a reviewer seeing a few rows that
# turn out not to be crypto.
DEFAULT_SKIPPED_UNDERLYINGS = frozenset({"equity", "index", "commodity", "pre_market"})
MAX_CANDIDATES_IN_PROMPT = 12

SYSTEM_PROMPT = """\
You map an exchange's base asset to its CoinMarketCap (CMC) asset.

Rules:
- Choose only from the candidate list. Never invent a CMC ID.
- Prefer the candidate whose project name matches the exchange asset name.
- Do not pick a wrapped, bridged, staked, pegged or leveraged derivative of an
  asset when a candidate for the plain underlying asset is present.
- A contract ticker may carry a multiplier prefix such as 1000; the underlying
  asset is the unmultiplied one.
- Treat price agreement as a weak sanity check, never as proof of identity.
- Answer null when no candidate is the same underlying asset. Tokenized
  equities, fiat currencies, indices and leveraged tokens often have no
  matching CMC crypto asset.
- Use confidence "high" only when the identity evidence, not just the ticker,
  establishes the match.

Reply with a JSON object: {"cmc_id": <integer or null>, "confidence":
"high" | "medium" | "low", "reasoning": "<one sentence>"}.
"""


class MatchStatus(StrEnum):
    APPROVED = "approved"
    UNCERTAIN = "uncertain"
    UNMAPPED = "unmapped"


@dataclass(frozen=True)
class SymbolOccurrence:
    """One snapshot row that needs a CMC ID."""

    exchange: str
    original_id: str
    symbol: str
    first_capture: datetime
    instrument_type: str | None = None
    end_date: datetime | None = None

    @property
    def instance(self) -> InstrumentInstance:
        return InstrumentInstance(self.exchange, self.original_id, self.first_capture)

    def overlaps(self, first_capture: datetime, end_date: datetime | None) -> bool:
        """Whether this instrument was listed at the same time as another."""
        if self.end_date is not None and first_capture > self.end_date:
            return False
        return end_date is None or self.first_capture <= end_date


@dataclass(frozen=True)
class NewSymbol:
    """Every new unmapped instrument that resolves to one CMC lookup ticker."""

    lookup_symbol: str
    occurrences: tuple[SymbolOccurrence, ...]

    @property
    def exchange_symbols(self) -> tuple[str, ...]:
        return tuple(sorted({occurrence.symbol for occurrence in self.occurrences}))

    @property
    def first_seen(self) -> datetime:
        return min(occurrence.first_capture for occurrence in self.occurrences)


@dataclass(frozen=True)
class MappedWindow:
    """An already-mapped instrument's availability window and CMC ID."""

    cmc_id: int
    first_capture: datetime
    end_date: datetime | None


@dataclass(frozen=True)
class Candidate:
    asset: CmcAsset
    relative_price_difference: float | None = None
    identity_match: str | None = None

    def price_agrees(self, max_relative_difference: float) -> bool:
        """Whether price corroborates, treating an absent observation as neutral."""
        return (
            self.relative_price_difference is None
            or self.relative_price_difference <= max_relative_difference
        )


@dataclass(frozen=True)
class SymbolEvidence:
    new_symbol: NewSymbol
    candidates: tuple[Candidate, ...]
    probe_status: str
    price_compatible_cmc_id: int | None = None
    observation: PriceObservation | None = None
    binance_asset: dict | None = None
    catalogue_trustworthy: bool = True
    catalogue_issue: str = ""
    exchange_prices_available: bool = True

    @property
    def identified_candidates(self) -> tuple[Candidate, ...]:
        """Candidates carrying identity evidence, not merely the same ticker."""
        return tuple(
            candidate for candidate in self.candidates if candidate.identity_match
        )


@dataclass(frozen=True)
class Decision:
    evidence: SymbolEvidence
    status: MatchStatus
    cmc_id: int | None
    slug: str
    method: str
    confidence: str
    rationale: str

    @property
    def lookup_symbol(self) -> str:
        return self.evidence.new_symbol.lookup_symbol


def instances_to_skip(
    store: MappingStore,
    now: datetime,
    recheck_unmapped_after_days: int = DEFAULT_RECHECK_UNMAPPED_AFTER_DAYS,
) -> frozenset[str]:
    """Return instrument instances a previous run settled, by instance key.

    Keyed by ``(exchange, original_id, first_capture)`` rather than by ticker, so
    a delisting-and-relisting or a reused exchange symbol is a new instance that
    still gets its own decision.

    An ``unmapped`` verdict only means CoinMarketCap had no matching asset at the
    time, so it expires: the instance becomes eligible again once the recheck
    window passes.
    """
    if recheck_unmapped_after_days < 0:
        raise ValueError("recheck_unmapped_after_days must not be negative")
    latest: dict[str, CmcMapping] = {}
    for mapping in store.mappings:
        key = mapping.instrument.key
        previous = latest.get(key)
        if previous is None or mapping.recorded_at > previous.recorded_at:
            latest[key] = mapping
    cutoff = now - timedelta(days=recheck_unmapped_after_days)
    return frozenset(
        key
        for key, mapping in latest.items()
        if not (
            mapping.status == MatchStatus.UNMAPPED.value and mapping.recorded_at < cutoff
        )
    )


def proposed_instances(store: MappingStore) -> frozenset[str]:
    """Return every instrument instance a store holds a decision for, no expiry.

    Used for the ledgers on still-open pull request branches: an instance already
    awaiting review must not be proposed again, whatever its verdict was.
    """
    return frozenset(mapping.instrument.key for mapping in store.mappings)


def pending_proposed_instances(paths: Iterable[Path]) -> frozenset[str]:
    """Union the instances proposed by mapping stores on open PR branches."""
    proposed: set[str] = set()
    for path in paths:
        try:
            proposed |= proposed_instances(MappingStore.load(path))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"ignoring unreadable pending mapping store {path}: {error}", file=sys.stderr)
    return frozenset(proposed)


def mapped_windows_by_symbol(
    rows_by_exchange: dict[str, list[dict]],
) -> dict[str, tuple[MappedWindow, ...]]:
    """Index already-mapped instruments by ticker, keeping their listing windows.

    A ticker alone is not identity: the same code can be reused by an unrelated
    project years later. Callers must additionally require that the windows
    overlap before treating an existing ID as evidence.
    """
    windows: dict[str, list[MappedWindow]] = {}
    for rows in rows_by_exchange.values():
        for row in rows:
            cmc_id = row.get("cmc_id")
            symbol = row.get("symbol")
            first_capture = _parse_optional_timestamp(row.get("first_capture"))
            if (
                not isinstance(symbol, str)
                or not _is_cmc_id(cmc_id)
                or first_capture is None
            ):
                continue
            windows.setdefault(symbol.upper(), []).append(
                MappedWindow(
                    cmc_id=int(cmc_id),
                    first_capture=first_capture,
                    end_date=_parse_optional_timestamp(row.get("end_date")),
                )
            )
    return {symbol: tuple(items) for symbol, items in windows.items()}


def collect_new_symbols(
    rows_by_exchange: dict[str, list[dict]],
    cmc_symbols: set[str],
    now: datetime,
    new_within_days: int = DEFAULT_NEW_WITHIN_DAYS,
    decided_instances: frozenset[str] = frozenset(),
    only_symbols: frozenset[str] = frozenset(),
    skip_underlyings: frozenset[str] = DEFAULT_SKIPPED_UNDERLYINGS,
) -> list[NewSymbol]:
    """Group rows that still lack a CMC ID by lookup ticker.

    Rows are filtered per instrument instance, so an earlier decision about one
    instance never suppresses a later relisting of the same ticker.
    ``only_symbols`` narrows the run to those tickers and ignores the recency and
    already-decided filters, so a specific asset can be re-resolved by hand --
    including an underlying this run would otherwise skip.
    """
    if new_within_days < 0:
        raise ValueError("new_within_days must not be negative")
    cutoff = now - timedelta(days=new_within_days)
    occurrences_by_lookup: dict[str, list[SymbolOccurrence]] = {}
    for exchange, rows in sorted(rows_by_exchange.items()):
        for row in rows:
            occurrence = _occurrence_from_row(exchange, row)
            if occurrence is None:
                continue
            if not only_symbols and row.get("underlying") in skip_underlyings:
                continue
            lookup_symbol = normalize_cmc_lookup_symbol(occurrence.symbol, cmc_symbols)
            if only_symbols:
                if not {occurrence.symbol.upper(), lookup_symbol} & only_symbols:
                    continue
            elif (
                occurrence.first_capture < cutoff
                or occurrence.instance.key in decided_instances
            ):
                continue
            occurrences_by_lookup.setdefault(lookup_symbol, []).append(occurrence)
    return [
        NewSymbol(lookup_symbol, tuple(occurrences))
        for lookup_symbol, occurrences in sorted(occurrences_by_lookup.items())
    ]


def build_symbol_evidence(
    new_symbol: NewSymbol,
    catalogue: CmcCatalogue,
    observations_by_exchange: dict[str, dict[str, PriceObservation]],
    public_assets_by_symbol: dict[str, dict] | None = None,
    max_relative_difference: float = DEFAULT_MAX_RELATIVE_DIFFERENCE,
    max_timestamp_skew: timedelta = timedelta(
        seconds=DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS
    ),
    exchange_prices_available: bool = True,
    max_catalogue_gap_ratio: float = DEFAULT_MAX_CATALOGUE_GAP_RATIO,
) -> SymbolEvidence:
    """Collect same-ticker CMC candidates plus price and Binance identity evidence."""
    trustworthy, issue = catalogue_trust(
        catalogue.diagnostics, max_catalogue_gap_ratio
    )
    cmc_symbols = {asset.symbol.upper() for asset in catalogue.assets}
    assets = candidates_for_symbol(catalogue.assets, new_symbol.lookup_symbol)
    observation = _observation_for(
        new_symbol, observations_by_exchange, cmc_symbols
    )
    probe_status = "no_exchange_price"
    price_compatible_cmc_id: int | None = None
    if observation is not None and assets:
        result = classify_price_candidates(
            assets, observation, max_relative_difference, max_timestamp_skew
        )
        probe_status = result.status.value
        if result.status is ProbeStatus.PRICE_COMPATIBLE and result.match is not None:
            price_compatible_cmc_id = result.match.asset.cmc_id
    elif not assets:
        probe_status = ProbeStatus.NO_TICKER_CANDIDATE.value
    binance_asset = (public_assets_by_symbol or {}).get(new_symbol.lookup_symbol)
    candidates = tuple(
        Candidate(
            asset,
            _relative_difference(asset, observation),
            identity_match(asset, binance_asset),
        )
        for asset in sorted(assets, key=lambda asset: asset.cmc_id)
    )
    return SymbolEvidence(
        new_symbol=new_symbol,
        candidates=candidates,
        probe_status=probe_status,
        price_compatible_cmc_id=price_compatible_cmc_id,
        observation=observation,
        binance_asset=binance_asset,
        catalogue_trustworthy=trustworthy,
        catalogue_issue=issue,
        exchange_prices_available=exchange_prices_available,
    )


def catalogue_trust(
    diagnostics: CatalogueDiagnostics,
    max_gap_ratio: float = DEFAULT_MAX_CATALOGUE_GAP_RATIO,
) -> tuple[bool, str]:
    """Whether the catalogue is sound enough to approve an identity match.

    Approval rests on *presence* -- an exact Binance-to-CMC name match -- not on a
    ticker being unique, so a handful of absent rows cannot manufacture a false
    match: a missing row would have to share both the ticker and the exact project
    name, and two candidates sharing both go to the LLM as ambiguous anyway.

    Every way a row can go missing -- deduplicated, dropped at parse time, lost to
    pagination -- already shows up as ``reported_total - unique_ids``, so there is
    one proportionate budget for all of them rather than a separate categorical
    veto per cause. A single unparseable row among 8,183 is as routine as the
    dedup gap, and a genuine schema break would blow the budget anyway because
    ``unique_ids`` would crater.

    What still fails closed is structural incoherence, where no tolerance can
    help because the numbers cannot be compared at all:

    - a reported total that changed mid-fetch, so no page is a consistent view;
    - a missing reported total, leaving nothing to compare against;
    - more unique IDs than the catalogue claims to hold;
    - a gap larger than ``max_gap_ratio`` of the reported total.
    """
    if max_gap_ratio < 0:
        raise ValueError("max_gap_ratio must not be negative")
    if diagnostics.total_count_changed:
        return False, "CoinMarketCap's reported total changed during the fetch."
    if diagnostics.reported_total is None:
        return False, "CoinMarketCap reported no total to compare against."
    gap = diagnostics.reported_total - diagnostics.unique_ids
    if gap < 0:
        return False, (
            f"CoinMarketCap returned {-gap} more unique IDs than it reported."
        )
    allowed = max_gap_ratio * diagnostics.reported_total
    causes = []
    if diagnostics.malformed_rows:
        causes.append(f"{diagnostics.malformed_rows} unparseable")
    if diagnostics.duplicate_ids:
        causes.append(f"{len(diagnostics.duplicate_ids)} duplicated")
    detail = f" ({', '.join(causes)})" if causes else ""
    if gap > allowed:
        return False, (
            f"{gap} of {diagnostics.reported_total} catalogue rows are missing"
            f"{detail}, above the {max_gap_ratio:.3%} tolerance."
        )
    if gap:
        return True, (
            f"tolerated a gap of {gap} of {diagnostics.reported_total} rows"
            f"{detail}: {gap / diagnostics.reported_total:.4%}, "
            f"within {max_gap_ratio:.3%}"
        )
    return True, ""


def identity_match(asset: CmcAsset, binance_asset: dict | None) -> str | None:
    """Return how a Binance asset's name identifies this CMC asset, or ``None``.

    Only an exact match after normalization counts. A substring rule would happily
    tie "Pepe" to "Pepe 2.0", which is the very confusion identity evidence has to
    resolve.
    """
    if not binance_asset:
        return None
    name = binance_asset.get("assetName")
    if not isinstance(name, str):
        return None
    normalized = _normalized_identity(name)
    if not normalized:
        return None
    if asset.name and normalized == _normalized_identity(asset.name):
        return "binance_asset_name"
    if asset.slug and normalized == _normalized_identity(asset.slug):
        return "cmc_slug"
    return None


def concurrent_cmc_id(
    new_symbol: NewSymbol, windows_by_symbol: dict[str, tuple[MappedWindow, ...]]
) -> tuple[int, str] | None:
    """Return a CMC ID already mapped to a *concurrently listed* same-ticker row.

    Every occurrence must overlap a mapped instrument carrying the same single ID.
    Requiring the windows to overlap is what keeps a reused or relisted ticker
    from inheriting an unrelated project's ID.
    """
    for symbol in (new_symbol.lookup_symbol, *new_symbol.exchange_symbols):
        windows = windows_by_symbol.get(symbol.upper())
        if not windows:
            continue
        ids: set[int] = set()
        for occurrence in new_symbol.occurrences:
            overlapping = {
                window.cmc_id
                for window in windows
                if occurrence.overlaps(window.first_capture, window.end_date)
            }
            if not overlapping:
                return None
            ids |= overlapping
        if len(ids) == 1:
            return next(iter(ids)), symbol.upper()
    return None


def decide(
    evidence: SymbolEvidence,
    client: ChatClient | None,
    max_relative_difference: float = DEFAULT_MAX_RELATIVE_DIFFERENCE,
) -> Decision:
    """Resolve one new ticker, escalating to the LLM only when it can help.

    A same ticker plus an agreeing price never approves on its own: an incomplete
    catalogue can hide a second same-ticker asset and turn ambiguity into a false
    unique match. Approval needs identity evidence, per this repository's
    acceptance criteria.
    """
    if not evidence.candidates:
        if not evidence.catalogue_trustworthy:
            return _decision(
                evidence,
                MatchStatus.UNCERTAIN,
                None,
                "untrustworthy_catalogue",
                "low",
                "Finding no same-ticker asset does not establish that none exists: "
                f"{evidence.catalogue_issue}",
            )
        return _decision(
            evidence,
            MatchStatus.UNMAPPED,
            None,
            "no_ticker_candidate",
            "high",
            "CoinMarketCap lists no asset with this ticker.",
        )
    identified = evidence.identified_candidates
    if len(identified) == 1 and identified[0].price_agrees(max_relative_difference):
        candidate = identified[0]
        blocker = _approval_blocker(evidence, candidate)
        rationale = (
            f"Binance asset name matches {candidate.asset.name or candidate.asset.slug} "
            f"({candidate.identity_match}) and the price does not contradict it."
        )
        if blocker is None:
            return _decision(
                evidence,
                MatchStatus.APPROVED,
                candidate.asset,
                f"identity_{candidate.identity_match}",
                "high",
                rationale,
            )
        return _decision(
            evidence,
            MatchStatus.UNCERTAIN,
            candidate.asset,
            "approval_withheld",
            "medium",
            f"{rationale} {blocker}",
        )
    if client is None:
        return _decision(
            evidence,
            MatchStatus.UNCERTAIN,
            None,
            "deterministic_only",
            "low",
            f"{len(evidence.candidates)} same-ticker candidate(s) and "
            f"{len(identified)} with name evidence need review; no LLM was "
            f"configured (probe status: {evidence.probe_status}).",
        )
    return _decide_with_llm(evidence, client, max_relative_difference)


def _approval_blocker(
    evidence: SymbolEvidence, candidate: Candidate | None
) -> str | None:
    """Return why this candidate may not be auto-approved, or ``None`` if it may.

    ``candidate`` is ``None`` when the caller is reusing an ID from a concurrently
    listed instrument, where identity rests on the overlapping listing windows
    rather than on a name match.
    """
    if not evidence.exchange_prices_available:
        return (
            "Holding for review because Binance prices were unavailable for this "
            "run, so nothing corroborated the name match."
        )
    if not evidence.catalogue_trustworthy:
        return f"Holding for review: {evidence.catalogue_issue}"
    if candidate is not None and not candidate.identity_match:
        return (
            "Holding for review because only the ticker and price agree, which "
            "cannot establish identity."
        )
    return None


def _decide_with_llm(
    evidence: SymbolEvidence,
    client: ChatClient,
    max_relative_difference: float,
) -> Decision:
    try:
        answer = client.complete_json(SYSTEM_PROMPT, build_prompt(evidence))
    except LlmConfigurationError:
        raise
    except LlmError as error:
        return _decision(
            evidence,
            MatchStatus.UNCERTAIN,
            None,
            "llm_unavailable",
            "low",
            f"LLM adjudication failed: {error}",
        )

    confidence = str(answer.get("confidence", "")).strip().lower()
    reasoning = str(answer.get("reasoning", "")).strip() or "no reasoning given"
    candidate = _candidate_by_id(evidence, answer.get("cmc_id"))
    if candidate is None:
        if answer.get("cmc_id") in (None, ""):
            return _decision(
                evidence,
                MatchStatus.UNMAPPED,
                None,
                "llm_rejected_all_candidates",
                confidence or "low",
                reasoning,
            )
        return _decision(
            evidence,
            MatchStatus.UNCERTAIN,
            None,
            "llm_chose_unlisted_id",
            "low",
            f"LLM returned CMC ID {answer.get('cmc_id')!r}, which is not a "
            f"candidate for this ticker: {reasoning}",
        )
    price_contradicts = not candidate.price_agrees(max_relative_difference)
    blocker = _approval_blocker(evidence, candidate)
    if confidence == "high" and not price_contradicts and blocker is None:
        return _decision(
            evidence,
            MatchStatus.APPROVED,
            candidate.asset,
            f"llm_adjudicated_identity_{candidate.identity_match}",
            "high",
            reasoning,
        )
    if price_contradicts:
        reasoning = (
            f"{reasoning} Price evidence disagrees by "
            f"{candidate.relative_price_difference:.2%}."
        )
    if blocker is not None:
        reasoning = f"{reasoning} {blocker}"
    return _decision(
        evidence,
        MatchStatus.UNCERTAIN,
        candidate.asset,
        "llm_adjudicated",
        confidence or "low",
        reasoning,
    )


def build_prompt(evidence: SymbolEvidence) -> str:
    """Render the candidate evidence an LLM may reason over, and nothing else."""
    new_symbol = evidence.new_symbol
    payload: dict[str, object] = {
        "exchange_asset": {
            "lookup_ticker": new_symbol.lookup_symbol,
            "exchange_tickers": list(new_symbol.exchange_symbols),
            "first_seen": new_symbol.first_seen.date().isoformat(),
            "instruments": [
                {
                    "exchange": occurrence.exchange,
                    "original_id": occurrence.original_id,
                    "type": occurrence.instrument_type,
                }
                for occurrence in new_symbol.occurrences[:10]
            ],
        },
        "candidates": [
            {
                "cmc_id": candidate.asset.cmc_id,
                "name": candidate.asset.name,
                "symbol": candidate.asset.symbol,
                "slug": candidate.asset.slug,
                "price_usd": candidate.asset.price_usd,
                "is_active": candidate.asset.is_active,
                "relative_price_difference": candidate.relative_price_difference,
                "binance_name_matches": candidate.identity_match is not None,
            }
            for candidate in evidence.candidates[:MAX_CANDIDATES_IN_PROMPT]
        ],
        "price_probe_status": evidence.probe_status,
        "catalogue_trustworthy": evidence.catalogue_trustworthy,
    }
    if evidence.binance_asset is not None:
        payload["binance_asset"] = {
            key: evidence.binance_asset[key]
            for key in ("assetCode", "assetName", "tags", "delisted", "preDelist")
            if key in evidence.binance_asset
        }
    if evidence.observation is not None:
        payload["exchange_price"] = {
            "venue": evidence.observation.venue,
            "instrument_id": evidence.observation.instrument_id,
            "price": evidence.observation.normalized_price,
            "quote_currency": evidence.observation.quote_currency,
            "base_units_per_contract": evidence.observation.base_units_per_contract,
            "observed_at": evidence.observation.observed_at.isoformat().replace(
                "+00:00", "Z"
            ),
        }
    return json.dumps(payload, indent=2, sort_keys=True)


def apply_decisions(
    rows_by_exchange: dict[str, list[dict]], decisions: list[Decision]
) -> dict[str, int]:
    """Write approved CMC IDs onto the new rows only, never replacing an ID."""
    approved_ids: dict[tuple[str, str], int] = {
        (occurrence.exchange, occurrence.original_id): decision.cmc_id
        for decision in decisions
        if decision.status is MatchStatus.APPROVED and decision.cmc_id is not None
        for occurrence in decision.evidence.new_symbol.occurrences
    }
    updated: Counter[str] = Counter()
    for exchange, rows in rows_by_exchange.items():
        for row in rows:
            original_id = row.get("id")
            if not isinstance(original_id, str) or row.get("cmc_id") is not None:
                continue
            cmc_id = approved_ids.get((exchange, original_id))
            if cmc_id is None:
                continue
            row["cmc_id"] = cmc_id
            updated[exchange] += 1
    return dict(updated)


def record_decisions(
    store: MappingStore, decisions: list[Decision], recorded_at: datetime
) -> dict[str, int]:
    """Persist one decision row per instrument instance, with its evidence.

    The store refuses to overwrite an approved mapping with a different CMC ID.
    Such a conflict is counted and reported rather than aborting the run or
    silently winning, because it means a previous approval needs a human.
    """
    recorded = 0
    conflicts = 0
    for decision in decisions:
        for occurrence in decision.evidence.new_symbol.occurrences:
            mapping = CmcMapping(
                instrument=InstrumentInstance(
                    exchange=occurrence.exchange,
                    original_id=occurrence.original_id,
                    first_capture=occurrence.first_capture,
                ),
                cmc_id=decision.cmc_id,
                slug=decision.slug,
                status=decision.status.value,
                method=decision.method,
                recorded_at=recorded_at,
                symbol=decision.lookup_symbol,
                evidence=_evidence_payload(decision, occurrence),
            )
            try:
                store.upsert(mapping)
            except ValueError as error:
                conflicts += 1
                print(
                    f"{decision.lookup_symbol} ({occurrence.exchange} "
                    f"{occurrence.original_id}): {error}",
                    file=sys.stderr,
                )
                continue
            recorded += 1
    return {"recorded": recorded, "conflicts": conflicts}


def partition_conflicts(
    store: MappingStore, decisions: list[Decision]
) -> tuple[list[Decision], list[Decision]]:
    """Split decisions into those safe to apply and those conflicting with the store.

    A decision conflicts when the ledger already holds an *approved* mapping for
    one of its instrument instances with a different CMC ID. Conflicts are found
    before any snapshot is touched, so a ledger write the store refuses can never
    leave the snapshot and the ledger disagreeing.
    """
    applicable: list[Decision] = []
    conflicting: list[Decision] = []
    for decision in decisions:
        conflicts = False
        for occurrence in decision.evidence.new_symbol.occurrences:
            existing = store.get(occurrence.instance)
            if (
                existing is not None
                and existing.status == MatchStatus.APPROVED.value
                and existing.cmc_id != decision.cmc_id
            ):
                conflicts = True
                break
        (conflicting if conflicts else applicable).append(decision)
    return applicable, conflicting


def render_report(decisions: list[Decision], generated_at: datetime) -> str:
    """Render a review-ready markdown summary for a pull request body."""
    counts = Counter(decision.status.value for decision in decisions)
    lines = [
        "## New Binance symbols needing a CMC ID",
        "",
        f"Run at {generated_at.isoformat().replace('+00:00', 'Z')}.",
        "",
        f"- approved (written to snapshots): **{counts[MatchStatus.APPROVED.value]}**",
        f"- uncertain (needs human review): **{counts[MatchStatus.UNCERTAIN.value]}**",
        f"- unmapped (no CMC asset found): **{counts[MatchStatus.UNMAPPED.value]}**",
    ]
    for status, heading in (
        (MatchStatus.APPROVED, "Approved"),
        (MatchStatus.UNCERTAIN, "Uncertain — please review"),
        (MatchStatus.UNMAPPED, "Unmapped"),
    ):
        rows = [decision for decision in decisions if decision.status is status]
        if not rows:
            continue
        lines += [
            "",
            f"### {heading}",
            "",
            "| ticker | cmc_id | slug | method | confidence | instruments | notes |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += [_report_row(decision) for decision in rows]
    lines += [
        "",
        "Uncertain and unmapped assets keep `cmc_id` unset in the snapshots; only "
        "approved matches are written. Every decision, including its candidates "
        "and price evidence, is recorded per instrument instance in the CMC "
        "mapping store.",
    ]
    return "\n".join(lines) + "\n"


def _report_row(decision: Decision) -> str:
    evidence = decision.evidence
    candidates = ", ".join(
        f"{candidate.asset.cmc_id} ({candidate.asset.slug})"
        for candidate in evidence.candidates[:5]
    )
    notes = decision.rationale.replace("|", "\\|").replace("\n", " ")
    if len(evidence.candidates) > 1:
        notes = f"{notes} Candidates: {candidates}."
    return (
        f"| `{decision.lookup_symbol}` "
        f"| {decision.cmc_id if decision.cmc_id is not None else '—'} "
        f"| {decision.slug or '—'} "
        f"| {decision.method} "
        f"| {decision.confidence} "
        f"| {len(evidence.new_symbol.occurrences)} "
        f"| {notes} |"
    )


def _evidence_payload(decision: Decision, occurrence: SymbolOccurrence) -> dict:
    evidence = decision.evidence
    payload: dict[str, object] = {
        "confidence": decision.confidence,
        "rationale": decision.rationale,
        "exchange_symbol": occurrence.symbol,
        "price_probe_status": evidence.probe_status,
        "candidates": [
            {
                "cmc_id": candidate.asset.cmc_id,
                "name": candidate.asset.name,
                "slug": candidate.asset.slug,
                "price_usd": candidate.asset.price_usd,
                "is_active": candidate.asset.is_active,
                "relative_price_difference": candidate.relative_price_difference,
                "identity_match": candidate.identity_match,
            }
            for candidate in evidence.candidates[:MAX_CANDIDATES_IN_PROMPT]
        ],
        "catalogue_trustworthy": evidence.catalogue_trustworthy,
        "catalogue_issue": evidence.catalogue_issue,
    }
    if evidence.observation is not None:
        payload["exchange_price"] = {
            "venue": evidence.observation.venue,
            "instrument_id": evidence.observation.instrument_id,
            "price": evidence.observation.normalized_price,
            "quote_currency": evidence.observation.quote_currency,
            "base_units_per_contract": evidence.observation.base_units_per_contract,
            "observed_at": evidence.observation.observed_at.isoformat().replace(
                "+00:00", "Z"
            ),
        }
    if evidence.binance_asset is not None:
        payload["binance_asset_name"] = evidence.binance_asset.get("assetName")
    return payload


def _decision(
    evidence: SymbolEvidence,
    status: MatchStatus,
    asset: CmcAsset | None,
    method: str,
    confidence: str,
    rationale: str,
) -> Decision:
    return Decision(
        evidence=evidence,
        status=status,
        cmc_id=None if asset is None else asset.cmc_id,
        slug="" if asset is None else asset.slug,
        method=method,
        confidence=confidence,
        rationale=rationale,
    )


def _candidate_by_id(evidence: SymbolEvidence, cmc_id: object) -> Candidate | None:
    try:
        wanted = int(cmc_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return next(
        (
            candidate
            for candidate in evidence.candidates
            if candidate.asset.cmc_id == wanted
        ),
        None,
    )


def _normalized_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _relative_difference(
    asset: CmcAsset, observation: PriceObservation | None
) -> float | None:
    if observation is None:
        return None
    exchange_price = observation.normalized_price
    if exchange_price <= 0:
        return None
    return abs(asset.price_usd - exchange_price) / exchange_price


def _observation_for(
    new_symbol: NewSymbol,
    observations_by_exchange: dict[str, dict[str, PriceObservation]],
    cmc_symbols: set[str],
) -> PriceObservation | None:
    """Prefer a spot observation, normalized to one base unit of the asset."""
    for occurrence in sorted(
        new_symbol.occurrences, key=lambda item: item.exchange != "binance-spot"
    ):
        observations = observations_by_exchange.get(occurrence.exchange, {})
        observation = observations.get(occurrence.symbol.upper()) or observations.get(
            new_symbol.lookup_symbol
        )
        if observation is None:
            continue
        multiplier = contract_multiplier_for_cmc_lookup(
            occurrence.symbol, new_symbol.lookup_symbol, cmc_symbols
        )
        return replace(
            observation,
            base_units_per_contract=observation.base_units_per_contract * multiplier,
        )
    return None


def _occurrence_from_row(exchange: str, row: object) -> SymbolOccurrence | None:
    if not isinstance(row, dict) or row.get("cmc_id") is not None:
        return None
    original_id = row.get("id")
    symbol = row.get("symbol")
    first_capture = _parse_optional_timestamp(row.get("first_capture"))
    if (
        not isinstance(original_id, str)
        or not isinstance(symbol, str)
        or not symbol
        or first_capture is None
    ):
        return None
    instrument_type = row.get("type")
    return SymbolOccurrence(
        exchange=exchange,
        original_id=original_id,
        symbol=symbol,
        first_capture=first_capture,
        instrument_type=instrument_type if isinstance(instrument_type, str) else None,
        end_date=_parse_optional_timestamp(row.get("end_date")),
    )


def _parse_optional_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _is_cmc_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def all_snapshot_exchanges(data_dir: Path) -> tuple[str, ...]:
    """Every per-exchange snapshot in the data directory, by stem."""
    return tuple(
        sorted(
            path.stem
            for path in data_dir.glob("*.json")
            if path.name != DEFAULT_MAPPING_FILENAME
        )
    )


def load_snapshots(data_dir: Path, exchanges: tuple[str, ...]) -> dict[str, list[dict]]:
    rows_by_exchange: dict[str, list[dict]] = {}
    for exchange in exchanges:
        path = data_dir / f"{exchange}.json"
        if not path.exists():
            continue
        rows = json.loads(path.read_text())
        if isinstance(rows, list):
            rows_by_exchange[exchange] = [row for row in rows if isinstance(row, dict)]
    return rows_by_exchange


def save_snapshots(
    data_dir: Path, rows_by_exchange: dict[str, list[dict]], exchanges: set[str]
) -> None:
    """Rewrite touched snapshots in ``atlas/update.py``'s exact serialization."""
    for exchange in sorted(exchanges):
        rows = rows_by_exchange.get(exchange)
        if rows is None:
            continue
        (data_dir / f"{exchange}.json").write_text(json.dumps(rows, indent=2))


def fetch_price_observations(
    new_symbols: list[NewSymbol], exchanges: tuple[str, ...]
) -> tuple[dict[str, dict[str, PriceObservation]], bool]:
    """Fetch Binance tickers once and select the prices the new symbols need.

    Returns the observations and whether the fetch succeeded. Binance answers
    451 from some hosts and can simply be down, and neither should abort an
    unattended run -- but losing the price check is recorded, because callers
    must not approve a mapping that nothing corroborated.
    """
    wanted_by_exchange: dict[str, set[str]] = {exchange: set() for exchange in exchanges}
    for new_symbol in new_symbols:
        for occurrence in new_symbol.occurrences:
            wanted = wanted_by_exchange.setdefault(occurrence.exchange, set())
            wanted.add(occurrence.symbol.upper())
            wanted.add(new_symbol.lookup_symbol)
    try:
        spot_tickers = (
            fetch_spot_prices() if wanted_by_exchange.get("binance-spot") else []
        )
        futures_tickers = (
            fetch_futures_prices()
            if wanted_by_exchange.get("binance-futures")
            or wanted_by_exchange.get("binance-futures-cm")
            else []
        )
    except OSError as error:
        print(f"Binance prices unavailable: {error}", file=sys.stderr)
        return {exchange: {} for exchange in wanted_by_exchange}, False
    tickers_by_exchange = {
        "binance-spot": (spot_tickers, "binance-spot"),
        "binance-futures": (futures_tickers, "binance-futures"),
        "binance-futures-cm": (futures_tickers, "binance-futures"),
    }
    observations: dict[str, dict[str, PriceObservation]] = {}
    for exchange, wanted in wanted_by_exchange.items():
        tickers, venue = tickers_by_exchange.get(exchange, ([], exchange))
        if not wanted or not tickers:
            observations[exchange] = {}
            continue
        observations[exchange] = price_observations_from_binance_tickers(
            tickers, wanted, venue=venue
        )
    return observations, True


def _chat_client(use_llm: bool) -> ChatClient | None:
    """Build an LLM client, proving the endpoint works before the run relies on it.

    A misconfigured endpoint used to be discovered once per symbol, so a dead URL
    cost four doomed requests per ticker and reported itself as dozens of
    unrelated per-symbol outages. One preflight request turns that into a single
    clear message, and the run continues on deterministic evidence alone.
    """
    if not use_llm:
        return None
    try:
        config = LlmConfig.from_env()
    except LlmNotConfiguredError as error:
        print(f"LLM adjudication disabled: {error}", file=sys.stderr)
        return None
    client = ChatClient(config, requests.Session())
    try:
        client.check()
    except LlmError as error:
        client.close()
        print(
            f"::error::LLM adjudication disabled, every ambiguous ticker will need "
            f"review: {error}",
            file=sys.stderr,
        )
        return None
    print(f"LLM adjudication via {config.base_url} ({config.model})", file=sys.stderr)
    return client


def _public_assets_by_symbol() -> dict[str, dict]:
    try:
        return {
            asset["assetCode"].upper(): asset
            for asset in fetch_public_assets()
            if isinstance(asset.get("assetCode"), str)
        }
    except OSError as error:
        print(
            "::warning::Binance asset names unavailable, so no mapping can be "
            f"approved on identity evidence this run: {error}",
            file=sys.stderr,
        )
        return {}


def resolve_new_symbols(
    new_symbols: list[NewSymbol],
    catalogue: CmcCatalogue,
    windows_by_symbol: dict[str, tuple[MappedWindow, ...]],
    observations_by_exchange: dict[str, dict[str, PriceObservation]],
    public_assets_by_symbol: dict[str, dict],
    client: ChatClient | None,
    max_relative_difference: float = DEFAULT_MAX_RELATIVE_DIFFERENCE,
    max_timestamp_skew: timedelta = timedelta(
        seconds=DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS
    ),
    max_llm_symbols: int = DEFAULT_MAX_LLM_SYMBOLS,
    exchange_prices_available: bool = True,
    max_catalogue_gap_ratio: float = DEFAULT_MAX_CATALOGUE_GAP_RATIO,
) -> list[Decision]:
    """Resolve each new ticker, spending LLM calls only on unresolved ones."""
    decisions: list[Decision] = []
    llm_calls = 0
    for new_symbol in new_symbols:
        evidence = build_symbol_evidence(
            new_symbol,
            catalogue,
            observations_by_exchange,
            public_assets_by_symbol,
            max_relative_difference=max_relative_difference,
            max_timestamp_skew=max_timestamp_skew,
            exchange_prices_available=exchange_prices_available,
            max_catalogue_gap_ratio=max_catalogue_gap_ratio,
        )
        concurrent = concurrent_cmc_id(new_symbol, windows_by_symbol)
        if concurrent is not None and _approval_blocker(evidence, None) is None:
            cmc_id, matched_symbol = concurrent
            slug = next(
                (
                    candidate.asset.slug
                    for candidate in evidence.candidates
                    if candidate.asset.cmc_id == cmc_id
                ),
                "",
            )
            decisions.append(
                Decision(
                    evidence=evidence,
                    status=MatchStatus.APPROVED,
                    cmc_id=cmc_id,
                    slug=slug,
                    method="concurrent_instrument_mapping",
                    confidence="high",
                    rationale=(
                        f"Every instrument was listed alongside an existing "
                        f"{matched_symbol} instrument already mapped to CMC ID {cmc_id}."
                    ),
                )
            )
            continue
        identified = evidence.identified_candidates
        needs_llm = not (
            not evidence.candidates
            or (
                len(identified) == 1
                and identified[0].price_agrees(max_relative_difference)
            )
        )
        if needs_llm and client is not None and llm_calls >= max_llm_symbols:
            decisions.append(
                _decision(
                    evidence,
                    MatchStatus.UNCERTAIN,
                    None,
                    "llm_budget_exhausted",
                    "low",
                    f"Reached the {max_llm_symbols}-symbol LLM budget for this run.",
                )
            )
            continue
        try:
            decision = decide(evidence, client, max_relative_difference)
        except LlmConfigurationError as error:
            # The endpoint is permanently unusable; stop asking it and finish the
            # remaining tickers on deterministic evidence alone.
            print(f"::error::LLM adjudication abandoned: {error}", file=sys.stderr)
            client = None
            decision = decide(evidence, None, max_relative_difference)
        if needs_llm and client is not None:
            llm_calls += 1
        decisions.append(decision)
    return decisions


def coverage_report(
    rows_by_exchange: dict[str, list[dict]],
    now: datetime,
    window_days: int,
    skip_underlyings: frozenset[str] = DEFAULT_SKIPPED_UNDERLYINGS,
) -> dict[str, object]:
    """Summarise CMC ID coverage over a window, for staleness checks.

    Deliberately offline: it reads only the snapshots, so it can answer "is
    anything stale?" without touching CoinMarketCap or Binance.
    """
    if window_days < 0:
        raise ValueError("window_days must not be negative")
    cutoff = now - timedelta(days=window_days)
    by_exchange: dict[str, dict[str, object]] = {}
    for exchange, rows in sorted(rows_by_exchange.items()):
        in_window = [
            row
            for row in rows
            if (captured := _parse_optional_timestamp(row.get("first_capture")))
            is not None
            and captured >= cutoff
        ]
        unmapped = [row for row in in_window if row.get("cmc_id") is None]
        out_of_scope = [
            row for row in unmapped if row.get("underlying") in skip_underlyings
        ]
        missing = [
            row for row in unmapped if row.get("underlying") not in skip_underlyings
        ]
        by_exchange[exchange] = {
            "rows_total": len(rows),
            "rows_missing_cmc_id_total": sum(
                1 for row in rows if row.get("cmc_id") is None
            ),
            "rows_in_window": len(in_window),
            "rows_in_window_missing_cmc_id": len(missing),
            "rows_in_window_out_of_scope": len(out_of_scope),
            "out_of_scope_underlyings": dict(
                sorted(Counter(str(row.get("underlying")) for row in out_of_scope).items())
            ),
            "tickers_in_window_missing_cmc_id": sorted(
                {
                    row["symbol"].upper()
                    for row in missing
                    if isinstance(row.get("symbol"), str) and row["symbol"]
                }
            ),
        }
    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "window_days": window_days,
        "skipped_underlyings": sorted(skip_underlyings),
        "exchanges": by_exchange,
        "rows_in_window_missing_cmc_id": sum(
            int(stats["rows_in_window_missing_cmc_id"]) for stats in by_exchange.values()
        ),
        "rows_in_window_out_of_scope": sum(
            int(stats["rows_in_window_out_of_scope"]) for stats in by_exchange.values()
        ),
    }


def render_coverage_report(coverage: dict[str, object]) -> str:
    """Render the coverage audit as markdown for a job summary or Slack."""
    window_days = coverage["window_days"]
    lines = [
        f"## CMC ID coverage over the last {window_days} days",
        "",
        f"Run at {coverage['generated_at']}.",
        "",
        "| exchange | rows | missing id (all time) | rows in window | missing id, in scope | skipped as non-crypto |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    exchanges: dict[str, dict[str, object]] = coverage["exchanges"]  # type: ignore[assignment]
    for exchange, stats in exchanges.items():
        lines.append(
            f"| `{exchange}` | {stats['rows_total']} "
            f"| {stats['rows_missing_cmc_id_total']} "
            f"| {stats['rows_in_window']} "
            f"| {stats['rows_in_window_missing_cmc_id']} "
            f"| {stats['rows_in_window_out_of_scope']} |"
        )
    for exchange, stats in exchanges.items():
        tickers: list[str] = stats["tickers_in_window_missing_cmc_id"]  # type: ignore[assignment]
        if not tickers:
            continue
        shown = ", ".join(f"`{ticker}`" for ticker in tickers[:60])
        more = "" if len(tickers) <= 60 else f" …and {len(tickers) - 60} more"
        lines += ["", f"**{exchange}** tickers still without a CMC ID: {shown}{more}"]
    if not coverage["rows_in_window_missing_cmc_id"]:
        lines += ["", "Nothing in scope in the window is missing a CMC ID."]
    skipped = coverage.get("skipped_underlyings") or []
    if skipped:
        lines += [
            "",
            f"Rows whose `underlying` is {', '.join(f'`{u}`' for u in skipped)} are "
            "out of scope and not counted as missing: Atlas maps CMC IDs for crypto "
            "assets, and a tokenized equity's ID would name the wrapper, not the "
            "asset. Rows with no `underlying` (legacy crypto) and `unknown` are "
            "still in scope.",
        ]
    return "\n".join(lines) + "\n"


def run(
    data_dir: Path = DEFAULT_DATA_DIR,
    mapping_path: Path | None = None,
    exchanges: tuple[str, ...] = DEFAULT_EXCHANGES,
    new_within_days: int = DEFAULT_NEW_WITHIN_DAYS,
    max_relative_difference: float = DEFAULT_MAX_RELATIVE_DIFFERENCE,
    max_timestamp_skew_seconds: int = DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS,
    max_llm_symbols: int = DEFAULT_MAX_LLM_SYMBOLS,
    recheck_unmapped_after_days: int = DEFAULT_RECHECK_UNMAPPED_AFTER_DAYS,
    max_catalogue_gap_ratio: float = DEFAULT_MAX_CATALOGUE_GAP_RATIO,
    skip_underlyings: frozenset[str] = DEFAULT_SKIPPED_UNDERLYINGS,
    pending_mapping_paths: tuple[Path, ...] = (),
    use_llm: bool = True,
    only_symbols: frozenset[str] = frozenset(),
    recheck_decided: bool = False,
    coverage_only: bool = False,
    dry_run: bool = False,
    report_path: Path | None = None,
    summary_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Resolve new Binance tickers and report what a reviewer has to look at."""
    now = now or datetime.now(UTC)
    mapping_path = mapping_path or data_dir / DEFAULT_MAPPING_FILENAME
    rows_by_exchange = load_snapshots(data_dir, exchanges)

    if coverage_only:
        coverage = coverage_report(
            rows_by_exchange, now, new_within_days, skip_underlyings
        )
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(render_coverage_report(coverage))
        summary: dict[str, object] = {
            "generated_at": coverage["generated_at"],
            "coverage_only": True,
            "window_days": new_within_days,
            "rows_in_window_missing_cmc_id": coverage["rows_in_window_missing_cmc_id"],
            "rows_in_window_out_of_scope": coverage["rows_in_window_out_of_scope"],
            "skipped_underlyings": coverage["skipped_underlyings"],
            "coverage": coverage["exchanges"],
            "has_changes": False,
        }
        if summary_path is not None:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        print(render_coverage_report(coverage))
        return summary

    store = MappingStore.load(mapping_path)
    with requests.Session() as session:
        catalogue = fetch_cmc_catalogue(session)
    diagnostics = catalogue.diagnostics
    trustworthy, catalogue_issue = catalogue_trust(diagnostics, max_catalogue_gap_ratio)
    print(
        "CMC catalogue: "
        f"unique_ids={diagnostics.unique_ids} "
        f"reported_total={diagnostics.reported_total} "
        f"complete={diagnostics.is_complete} "
        f"trustworthy={trustworthy}"
        + (f" ({catalogue_issue})" if catalogue_issue else ""),
        file=sys.stderr,
    )
    if not trustworthy:
        print(
            f"::warning::No mapping will be auto-approved this run: {catalogue_issue}",
            file=sys.stderr,
        )

    cmc_symbols = {asset.symbol.upper() for asset in catalogue.assets}
    new_symbols = collect_new_symbols(
        rows_by_exchange,
        cmc_symbols,
        now=now,
        new_within_days=new_within_days,
        decided_instances=(
            frozenset()
            if recheck_decided
            else instances_to_skip(store, now, recheck_unmapped_after_days)
            | pending_proposed_instances(pending_mapping_paths)
        ),
        only_symbols=only_symbols,
        skip_underlyings=skip_underlyings,
    )
    summary = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "new_symbols": len(new_symbols),
        "approved": 0,
        "uncertain": 0,
        "unmapped": 0,
        "rows_updated": {},
        "mapping_conflicts": 0,
        "has_changes": False,
        "catalogue_complete": diagnostics.is_complete,
        "catalogue_trustworthy": trustworthy,
        "catalogue_issue": catalogue_issue,
        "skipped_underlyings": sorted(skip_underlyings),
        "exchange_prices_available": True,
    }
    if not new_symbols:
        print("No new Binance symbols need a CMC ID.")
        _write_outputs(summary, [], now, report_path, summary_path)
        return summary

    print(f"Resolving {len(new_symbols)} new Binance ticker(s)...", file=sys.stderr)
    observations_by_exchange, prices_available = fetch_price_observations(
        new_symbols, exchanges
    )
    if not prices_available:
        print(
            "::warning::Binance prices were unavailable; no mapping will be "
            "auto-approved this run",
            file=sys.stderr,
        )
    client = _chat_client(use_llm)
    try:
        decisions = resolve_new_symbols(
            new_symbols,
            catalogue,
            # Reuse evidence reads every bundled snapshot, not just the
            # exchanges this run resolves.
            mapped_windows_by_symbol(load_snapshots(data_dir, all_snapshot_exchanges(data_dir))),
            observations_by_exchange,
            _public_assets_by_symbol(),
            client,
            max_relative_difference=max_relative_difference,
            max_timestamp_skew=timedelta(seconds=max_timestamp_skew_seconds),
            max_llm_symbols=max_llm_symbols,
            exchange_prices_available=prices_available,
            max_catalogue_gap_ratio=max_catalogue_gap_ratio,
        )
    finally:
        if client is not None:
            client.close()

    # Conflicts are resolved before anything is written, so the snapshots and the
    # ledger can never end up disagreeing about an approved instrument.
    decisions, conflicting = partition_conflicts(store, decisions)
    for decision in conflicting:
        print(
            f"{decision.lookup_symbol}: refusing to replace an approved CMC mapping; "
            f"proposed {decision.cmc_id} for an instrument already approved otherwise",
            file=sys.stderr,
        )
    counts = Counter(decision.status.value for decision in decisions)
    rows_updated = apply_decisions(rows_by_exchange, decisions)
    recording = record_decisions(store, decisions, recorded_at=now)
    summary.update(
        {
            "approved": counts[MatchStatus.APPROVED.value],
            "uncertain": counts[MatchStatus.UNCERTAIN.value],
            "unmapped": counts[MatchStatus.UNMAPPED.value],
            "rows_updated": rows_updated,
            "mapping_conflicts": len(conflicting) + recording["conflicts"],
            "exchange_prices_available": prices_available,
            "has_changes": True,
        }
    )
    if not dry_run:
        save_snapshots(data_dir, rows_by_exchange, set(rows_updated))
        store.save()
    _write_outputs(summary, decisions, now, report_path, summary_path)
    for decision in decisions:
        print(
            f"{decision.lookup_symbol}\t{decision.status.value}\t"
            f"{decision.cmc_id if decision.cmc_id is not None else ''}\t"
            f"{decision.method}\t{decision.confidence}"
        )
    return summary


def _write_outputs(
    summary: dict[str, object],
    decisions: list[Decision],
    now: datetime,
    report_path: Path | None,
    summary_path: Path | None,
) -> None:
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(decisions, now))
    if summary_path is not None:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def _check_llm() -> int:
    """Validate the LLM configuration on its own, for setup and debugging."""
    try:
        config = LlmConfig.from_env()
    except LlmNotConfiguredError as error:
        print(f"no LLM configured: {error}", file=sys.stderr)
        return 1
    print(f"checking {config.chat_completions_url} ({config.model})...")
    with requests.Session() as session:
        client = ChatClient(config, session)
        try:
            client.check()
        except LlmError as error:
            print(f"LLM check FAILED: {error}", file=sys.stderr)
            return 1
    print("LLM check OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--mapping-path",
        type=Path,
        default=None,
        help=f"CMC decision store (default: <data-dir>/{DEFAULT_MAPPING_FILENAME})",
    )
    parser.add_argument(
        "--exchanges",
        default=",".join(DEFAULT_EXCHANGES),
        help="comma-separated snapshots to scan (default: %(default)s)",
    )
    parser.add_argument(
        "--new-within-days",
        type=int,
        default=DEFAULT_NEW_WITHIN_DAYS,
        help="only consider rows first captured this recently (default: %(default)s)",
    )
    parser.add_argument(
        "--max-relative-difference",
        type=float,
        default=DEFAULT_MAX_RELATIVE_DIFFERENCE,
    )
    parser.add_argument(
        "--max-timestamp-skew-seconds",
        type=int,
        default=DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS,
    )
    parser.add_argument(
        "--max-llm-symbols",
        type=int,
        default=DEFAULT_MAX_LLM_SYMBOLS,
        help="cap LLM adjudications per run (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-underlyings",
        default=",".join(sorted(DEFAULT_SKIPPED_UNDERLYINGS)),
        help="comma-separated `underlying` values to leave out, so only crypto "
        "assets are resolved; pass an empty string to resolve everything "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--max-catalogue-gap-ratio",
        type=float,
        default=DEFAULT_MAX_CATALOGUE_GAP_RATIO,
        help="largest tolerated fraction of catalogue rows CoinMarketCap reports "
        "but does not return, above which nothing is auto-approved "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--recheck-unmapped-after-days",
        type=int,
        default=DEFAULT_RECHECK_UNMAPPED_AFTER_DAYS,
        help="revisit a ticker CoinMarketCap had no asset for after this long "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--pending-mapping-path",
        type=Path,
        action="append",
        default=[],
        help="mapping store from an open pull request whose tickers are already "
        "awaiting review; repeatable",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="use deterministic evidence only; unresolved tickers stay uncertain",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="resolve only these comma-separated tickers, ignoring age and prior decisions",
    )
    parser.add_argument(
        "--recheck-decided",
        action="store_true",
        help="re-resolve tickers a previous run already decided",
    )
    parser.add_argument(
        "--check-llm",
        action="store_true",
        help="send one request to the configured LLM endpoint, report the result "
        "and exit, without touching CoinMarketCap or Binance",
    )
    parser.add_argument(
        "--coverage-only",
        action="store_true",
        help="report CMC ID coverage over the window and exit, without "
        "contacting CoinMarketCap, Binance or an LLM",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument("--summary-path", type=Path, default=None)
    args = parser.parse_args()

    if args.check_llm:
        return _check_llm()

    try:
        run(
            data_dir=args.data_dir,
            mapping_path=args.mapping_path,
            exchanges=tuple(
                exchange.strip()
                for exchange in args.exchanges.split(",")
                if exchange.strip()
            ),
            new_within_days=args.new_within_days,
            max_relative_difference=args.max_relative_difference,
            max_timestamp_skew_seconds=args.max_timestamp_skew_seconds,
            max_llm_symbols=args.max_llm_symbols,
            recheck_unmapped_after_days=args.recheck_unmapped_after_days,
            max_catalogue_gap_ratio=args.max_catalogue_gap_ratio,
            skip_underlyings=frozenset(
                value.strip()
                for value in args.skip_underlyings.split(",")
                if value.strip()
            ),
            pending_mapping_paths=tuple(args.pending_mapping_path),
            use_llm=not args.no_llm,
            only_symbols=frozenset(
                symbol.strip().upper()
                for symbol in args.symbols.split(",")
                if symbol.strip()
            ),
            recheck_decided=args.recheck_decided,
            coverage_only=args.coverage_only,
            dry_run=args.dry_run,
            report_path=args.report_path,
            summary_path=args.summary_path,
        )
    except (CmcProbeError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"CMC new-symbol mapping failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
