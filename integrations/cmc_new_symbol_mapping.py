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
import sys
from collections import Counter
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
from integrations.llm import ChatClient, LlmConfig, LlmError, LlmNotConfiguredError


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"
DEFAULT_MAPPING_FILENAME = "cmc_mappings.json"
BINANCE_EXCHANGES = ("binance-spot", "binance-futures", "binance-futures-cm")
DEFAULT_NEW_WITHIN_DAYS = 30
DEFAULT_MAX_RELATIVE_DIFFERENCE = 0.05
DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS = 180
DEFAULT_MAX_LLM_SYMBOLS = 40
DEFAULT_RECHECK_UNMAPPED_AFTER_DAYS = 90
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
class Candidate:
    asset: CmcAsset
    relative_price_difference: float | None = None


@dataclass(frozen=True)
class SymbolEvidence:
    new_symbol: NewSymbol
    candidates: tuple[Candidate, ...]
    probe_status: str
    price_compatible_cmc_id: int | None = None
    observation: PriceObservation | None = None
    binance_asset: dict | None = None


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


def symbols_to_skip(
    store: MappingStore,
    now: datetime,
    recheck_unmapped_after_days: int = DEFAULT_RECHECK_UNMAPPED_AFTER_DAYS,
) -> frozenset[str]:
    """Return tickers a previous run settled and this run should not re-decide.

    An ``unmapped`` verdict only means CoinMarketCap had no matching asset at the
    time, so it expires: the ticker becomes eligible again once the recheck
    window passes.
    """
    if recheck_unmapped_after_days < 0:
        raise ValueError("recheck_unmapped_after_days must not be negative")
    latest: dict[str, CmcMapping] = {}
    for mapping in store.mappings:
        if not mapping.symbol:
            continue
        symbol = mapping.symbol.upper()
        previous = latest.get(symbol)
        if previous is None or mapping.recorded_at > previous.recorded_at:
            latest[symbol] = mapping
    cutoff = now - timedelta(days=recheck_unmapped_after_days)
    return frozenset(
        symbol
        for symbol, mapping in latest.items()
        if not (
            mapping.status == MatchStatus.UNMAPPED.value and mapping.recorded_at < cutoff
        )
    )


def known_cmc_ids_by_symbol(rows_by_exchange: dict[str, list[dict]]) -> dict[str, int]:
    """Return tickers the snapshots already map unambiguously to one CMC ID."""
    ids_by_symbol: dict[str, set[int]] = {}
    for rows in rows_by_exchange.values():
        for row in rows:
            cmc_id = row.get("cmc_id")
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or not _is_cmc_id(cmc_id):
                continue
            ids_by_symbol.setdefault(symbol.upper(), set()).add(int(cmc_id))
    return {
        symbol: next(iter(ids))
        for symbol, ids in ids_by_symbol.items()
        if len(ids) == 1
    }


def collect_new_symbols(
    rows_by_exchange: dict[str, list[dict]],
    cmc_symbols: set[str],
    now: datetime,
    new_within_days: int = DEFAULT_NEW_WITHIN_DAYS,
    decided_symbols: frozenset[str] = frozenset(),
    only_symbols: frozenset[str] = frozenset(),
) -> list[NewSymbol]:
    """Group rows that still lack a CMC ID by lookup ticker.

    ``only_symbols`` narrows the run to those tickers and ignores the recency and
    already-decided filters, so a specific asset can be re-resolved by hand.
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
            lookup_symbol = normalize_cmc_lookup_symbol(occurrence.symbol, cmc_symbols)
            if only_symbols:
                if not {occurrence.symbol.upper(), lookup_symbol} & only_symbols:
                    continue
            elif (
                occurrence.first_capture < cutoff or lookup_symbol in decided_symbols
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
) -> SymbolEvidence:
    """Collect same-ticker CMC candidates plus price and Binance identity evidence."""
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
    candidates = tuple(
        Candidate(asset, _relative_difference(asset, observation))
        for asset in sorted(assets, key=lambda asset: asset.cmc_id)
    )
    binance_asset = (public_assets_by_symbol or {}).get(new_symbol.lookup_symbol)
    return SymbolEvidence(
        new_symbol=new_symbol,
        candidates=candidates,
        probe_status=probe_status,
        price_compatible_cmc_id=price_compatible_cmc_id,
        observation=observation,
        binance_asset=binance_asset,
    )


def decide_from_snapshots(
    new_symbol: NewSymbol, known_ids: dict[str, int]
) -> tuple[int, str] | None:
    """Return a CMC ID the snapshots already carry for this ticker, if any."""
    for symbol in (new_symbol.lookup_symbol, *new_symbol.exchange_symbols):
        cmc_id = known_ids.get(symbol.upper())
        if cmc_id is not None:
            return cmc_id, symbol.upper()
    return None


def decide(
    evidence: SymbolEvidence,
    client: ChatClient | None,
    max_relative_difference: float = DEFAULT_MAX_RELATIVE_DIFFERENCE,
) -> Decision:
    """Resolve one new ticker, escalating to the LLM only when it can help."""
    if not evidence.candidates:
        return _decision(
            evidence,
            MatchStatus.UNMAPPED,
            None,
            "no_ticker_candidate",
            "high",
            "CoinMarketCap lists no asset with this ticker.",
        )
    if (
        len(evidence.candidates) == 1
        and evidence.price_compatible_cmc_id == evidence.candidates[0].asset.cmc_id
    ):
        asset = evidence.candidates[0].asset
        return _decision(
            evidence,
            MatchStatus.APPROVED,
            asset,
            "unique_ticker_price_compatible",
            "high",
            "Only one CoinMarketCap asset uses this ticker and its price agrees.",
        )
    if client is None:
        return _decision(
            evidence,
            MatchStatus.UNCERTAIN,
            None,
            "deterministic_only",
            "low",
            f"{len(evidence.candidates)} same-ticker candidates need review; "
            f"no LLM was configured (probe status: {evidence.probe_status}).",
        )
    return _decide_with_llm(evidence, client, max_relative_difference)


def _decide_with_llm(
    evidence: SymbolEvidence,
    client: ChatClient,
    max_relative_difference: float,
) -> Decision:
    try:
        answer = client.complete_json(SYSTEM_PROMPT, build_prompt(evidence))
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
    price_contradicts = (
        candidate.relative_price_difference is not None
        and candidate.relative_price_difference > max_relative_difference
    )
    if confidence == "high" and not price_contradicts:
        return _decision(
            evidence, MatchStatus.APPROVED, candidate.asset, "llm_adjudicated", "high", reasoning
        )
    if price_contradicts:
        reasoning = (
            f"{reasoning} Price evidence disagrees by "
            f"{candidate.relative_price_difference:.2%}."
        )
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
            }
            for candidate in evidence.candidates[:MAX_CANDIDATES_IN_PROMPT]
        ],
        "price_probe_status": evidence.probe_status,
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
            }
            for candidate in evidence.candidates[:MAX_CANDIDATES_IN_PROMPT]
        ],
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
) -> dict[str, dict[str, PriceObservation]]:
    """Fetch Binance tickers once and select the prices the new symbols need."""
    wanted_by_exchange: dict[str, set[str]] = {exchange: set() for exchange in exchanges}
    for new_symbol in new_symbols:
        for occurrence in new_symbol.occurrences:
            wanted = wanted_by_exchange.setdefault(occurrence.exchange, set())
            wanted.add(occurrence.symbol.upper())
            wanted.add(new_symbol.lookup_symbol)
    spot_tickers = fetch_spot_prices() if wanted_by_exchange.get("binance-spot") else []
    futures_tickers = (
        fetch_futures_prices()
        if wanted_by_exchange.get("binance-futures")
        or wanted_by_exchange.get("binance-futures-cm")
        else []
    )
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
    return observations


def _chat_client(use_llm: bool) -> ChatClient | None:
    if not use_llm:
        return None
    try:
        config = LlmConfig.from_env()
    except LlmNotConfiguredError as error:
        print(f"LLM adjudication disabled: {error}", file=sys.stderr)
        return None
    print(f"LLM adjudication via {config.base_url} ({config.model})", file=sys.stderr)
    return ChatClient(config, requests.Session())


def _public_assets_by_symbol() -> dict[str, dict]:
    try:
        return {
            asset["assetCode"].upper(): asset
            for asset in fetch_public_assets()
            if isinstance(asset.get("assetCode"), str)
        }
    except OSError as error:
        print(f"Binance public asset metadata unavailable: {error}", file=sys.stderr)
        return {}


def resolve_new_symbols(
    new_symbols: list[NewSymbol],
    catalogue: CmcCatalogue,
    known_ids: dict[str, int],
    observations_by_exchange: dict[str, dict[str, PriceObservation]],
    public_assets_by_symbol: dict[str, dict],
    client: ChatClient | None,
    max_relative_difference: float = DEFAULT_MAX_RELATIVE_DIFFERENCE,
    max_timestamp_skew: timedelta = timedelta(
        seconds=DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS
    ),
    max_llm_symbols: int = DEFAULT_MAX_LLM_SYMBOLS,
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
        )
        known = decide_from_snapshots(new_symbol, known_ids)
        if known is not None:
            cmc_id, matched_symbol = known
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
                    method="existing_binance_mapping",
                    confidence="high",
                    rationale=(
                        f"Binance ticker {matched_symbol} is already mapped to CMC ID "
                        f"{cmc_id} in the snapshots."
                    ),
                )
            )
            continue
        needs_llm = not (
            not evidence.candidates
            or (
                len(evidence.candidates) == 1
                and evidence.price_compatible_cmc_id
                == evidence.candidates[0].asset.cmc_id
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
        decision = decide(evidence, client, max_relative_difference)
        if needs_llm and client is not None:
            llm_calls += 1
        decisions.append(decision)
    return decisions


def run(
    data_dir: Path = DEFAULT_DATA_DIR,
    mapping_path: Path | None = None,
    exchanges: tuple[str, ...] = BINANCE_EXCHANGES,
    new_within_days: int = DEFAULT_NEW_WITHIN_DAYS,
    max_relative_difference: float = DEFAULT_MAX_RELATIVE_DIFFERENCE,
    max_timestamp_skew_seconds: int = DEFAULT_MAX_TIMESTAMP_SKEW_SECONDS,
    max_llm_symbols: int = DEFAULT_MAX_LLM_SYMBOLS,
    recheck_unmapped_after_days: int = DEFAULT_RECHECK_UNMAPPED_AFTER_DAYS,
    use_llm: bool = True,
    only_symbols: frozenset[str] = frozenset(),
    recheck_decided: bool = False,
    dry_run: bool = False,
    report_path: Path | None = None,
    summary_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Resolve new Binance tickers and report what a reviewer has to look at."""
    now = now or datetime.now(UTC)
    mapping_path = mapping_path or data_dir / DEFAULT_MAPPING_FILENAME
    rows_by_exchange = load_snapshots(data_dir, exchanges)
    store = MappingStore.load(mapping_path)
    with requests.Session() as session:
        catalogue = fetch_cmc_catalogue(session)
    diagnostics = catalogue.diagnostics
    print(
        "CMC catalogue: "
        f"unique_ids={diagnostics.unique_ids} "
        f"reported_total={diagnostics.reported_total} "
        f"complete={diagnostics.is_complete}",
        file=sys.stderr,
    )

    cmc_symbols = {asset.symbol.upper() for asset in catalogue.assets}
    new_symbols = collect_new_symbols(
        rows_by_exchange,
        cmc_symbols,
        now=now,
        new_within_days=new_within_days,
        decided_symbols=(
            frozenset()
            if recheck_decided
            else symbols_to_skip(store, now, recheck_unmapped_after_days)
        ),
        only_symbols=only_symbols,
    )
    summary: dict[str, object] = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "new_symbols": len(new_symbols),
        "approved": 0,
        "uncertain": 0,
        "unmapped": 0,
        "rows_updated": {},
        "mapping_conflicts": 0,
        "has_changes": False,
        "catalogue_complete": diagnostics.is_complete,
    }
    if not new_symbols:
        print("No new Binance symbols need a CMC ID.")
        _write_outputs(summary, [], now, report_path, summary_path)
        return summary

    print(f"Resolving {len(new_symbols)} new Binance ticker(s)...", file=sys.stderr)
    client = _chat_client(use_llm)
    try:
        decisions = resolve_new_symbols(
            new_symbols,
            catalogue,
            known_cmc_ids_by_symbol(rows_by_exchange),
            fetch_price_observations(new_symbols, exchanges),
            _public_assets_by_symbol(),
            client,
            max_relative_difference=max_relative_difference,
            max_timestamp_skew=timedelta(seconds=max_timestamp_skew_seconds),
            max_llm_symbols=max_llm_symbols,
        )
    finally:
        if client is not None:
            client.close()

    counts = Counter(decision.status.value for decision in decisions)
    rows_updated = apply_decisions(rows_by_exchange, decisions)
    recording = record_decisions(store, decisions, recorded_at=now)
    summary.update(
        {
            "approved": counts[MatchStatus.APPROVED.value],
            "uncertain": counts[MatchStatus.UNCERTAIN.value],
            "unmapped": counts[MatchStatus.UNMAPPED.value],
            "rows_updated": rows_updated,
            "mapping_conflicts": recording["conflicts"],
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
        default=",".join(BINANCE_EXCHANGES),
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
        "--recheck-unmapped-after-days",
        type=int,
        default=DEFAULT_RECHECK_UNMAPPED_AFTER_DAYS,
        help="revisit a ticker CoinMarketCap had no asset for after this long "
        "(default: %(default)s)",
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
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument("--summary-path", type=Path, default=None)
    args = parser.parse_args()

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
            use_llm=not args.no_llm,
            only_symbols=frozenset(
                symbol.strip().upper()
                for symbol in args.symbols.split(",")
                if symbol.strip()
            ),
            recheck_decided=args.recheck_decided,
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
