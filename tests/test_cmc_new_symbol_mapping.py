import json
from datetime import UTC, datetime, timedelta

import pytest

from integrations.cmc_id_probe import (
    CatalogueDiagnostics,
    CmcAsset,
    CmcCatalogue,
    PriceObservation,
)
from integrations.cmc_mappings import MappingStore
from integrations.cmc_new_symbol_mapping import (
    BINANCE_EXCHANGES,
    Decision,
    MatchStatus,
    NewSymbol,
    SymbolOccurrence,
    apply_decisions,
    build_symbol_evidence,
    collect_new_symbols,
    decide,
    known_cmc_ids_by_symbol,
    pending_proposed_symbols,
    record_decisions,
    render_report,
    resolve_new_symbols,
    run,
    symbols_to_skip,
)
from integrations.llm import LlmError


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
OBSERVED_AT = NOW - timedelta(minutes=1)


class FakeChatClient:
    """Stand in for ``ChatClient``, recording prompts and replaying answers."""

    def __init__(self, answers: list[dict] | None = None) -> None:
        self._answers = list(answers or [])
        self.prompts: list[str] = []
        self.closed = False

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.prompts.append(user_prompt)
        if not self._answers:
            raise LlmError("no answer configured")
        return self._answers.pop(0)

    def close(self) -> None:
        self.closed = True


class ForbiddenChatClient:
    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        raise AssertionError("the LLM must not be called for this decision")

    def close(self) -> None:
        pass


def _catalogue(*assets: CmcAsset) -> CmcCatalogue:
    return CmcCatalogue(
        assets,
        CatalogueDiagnostics(len(assets), len(assets), len(assets), (), 0, False),
    )


def _asset(cmc_id: int, symbol: str, slug: str, price: float, name: str = "") -> CmcAsset:
    return CmcAsset(cmc_id, symbol, slug, price, OBSERVED_AT, True, name or slug.title())


def _observation(symbol: str, price: float, venue: str = "binance-spot") -> PriceObservation:
    return PriceObservation(price, "USDT", OBSERVED_AT, venue, f"{symbol}USDT")


def _occurrence(
    exchange_symbol: str, original_id: str | None = None, exchange: str = "binance-spot"
) -> SymbolOccurrence:
    return SymbolOccurrence(
        exchange,
        original_id or f"{exchange_symbol.lower()}usdt",
        exchange_symbol,
        NOW - timedelta(days=1),
        "spot",
    )


def _evidence_for(
    new_symbol: NewSymbol, assets: tuple[CmcAsset, ...], price: float | None
):
    observations: dict[str, dict[str, PriceObservation]] = {}
    if price is not None:
        for occurrence in new_symbol.occurrences:
            observations.setdefault(occurrence.exchange, {})[
                occurrence.symbol.upper()
            ] = _observation(occurrence.symbol, price, occurrence.exchange)
    return build_symbol_evidence(new_symbol, _catalogue(*assets), observations)


def _evidence(
    lookup_symbol: str,
    assets: tuple[CmcAsset, ...],
    price: float | None,
    exchange_symbol: str | None = None,
    exchange: str = "binance-spot",
):
    occurrence = _occurrence(exchange_symbol or lookup_symbol, exchange=exchange)
    return _evidence_for(NewSymbol(lookup_symbol, (occurrence,)), assets, price)


def test_collect_new_symbols_keeps_only_recent_rows_without_a_cmc_id():
    rows_by_exchange = {
        "binance-spot": [
            {"id": "newusdt", "symbol": "NEW", "first_capture": "2026-09-05T00:00:00.000Z"},
            {"id": "oldusdt", "symbol": "OLD", "first_capture": "2021-01-01T00:00:00.000Z"},
            {
                "id": "mappedusdt",
                "symbol": "MAPPED",
                "cmc_id": 5,
                "first_capture": "2026-09-05T00:00:00.000Z",
            },
            {"id": "123456", "first_capture": "2026-09-05T00:00:00.000Z"},
        ]
    }

    new_symbols = collect_new_symbols(rows_by_exchange, {"NEW"}, now=NOW)

    assert [new_symbol.lookup_symbol for new_symbol in new_symbols] == ["NEW"]
    assert new_symbols[0].occurrences[0].original_id == "newusdt"


def test_collect_new_symbols_groups_a_multiplier_contract_under_its_base_ticker():
    rows_by_exchange = {
        "binance-futures": [
            {
                "id": "1000cheemsusdt",
                "symbol": "1000CHEEMS",
                "first_capture": "2026-09-01T00:00:00.000Z",
            }
        ],
        "binance-spot": [
            {"id": "cheemsusdt", "symbol": "CHEEMS", "first_capture": "2026-09-02T00:00:00.000Z"}
        ],
    }

    new_symbols = collect_new_symbols(rows_by_exchange, {"CHEEMS"}, now=NOW)

    assert len(new_symbols) == 1
    assert new_symbols[0].lookup_symbol == "CHEEMS"
    assert new_symbols[0].exchange_symbols == ("1000CHEEMS", "CHEEMS")
    assert len(new_symbols[0].occurrences) == 2


def test_collect_new_symbols_skips_a_ticker_a_previous_run_decided():
    rows_by_exchange = {
        "binance-spot": [
            {"id": "newusdt", "symbol": "NEW", "first_capture": "2026-09-05T00:00:00.000Z"}
        ]
    }

    assert collect_new_symbols(
        rows_by_exchange, {"NEW"}, now=NOW, decided_symbols=frozenset({"NEW"})
    ) == []


def test_collect_new_symbols_narrows_to_requested_tickers_ignoring_age_and_history():
    rows_by_exchange = {
        "binance-spot": [
            {"id": "oldusdt", "symbol": "OLD", "first_capture": "2019-01-01T00:00:00.000Z"},
            {"id": "newusdt", "symbol": "NEW", "first_capture": "2026-09-05T00:00:00.000Z"},
        ]
    }

    new_symbols = collect_new_symbols(
        rows_by_exchange,
        {"OLD", "NEW"},
        now=NOW,
        decided_symbols=frozenset({"OLD"}),
        only_symbols=frozenset({"OLD"}),
    )

    assert [new_symbol.lookup_symbol for new_symbol in new_symbols] == ["OLD"]


def test_collect_new_symbols_matches_a_requested_ticker_through_its_multiplier():
    rows_by_exchange = {
        "binance-futures": [
            {
                "id": "1000cheemsusdt",
                "symbol": "1000CHEEMS",
                "first_capture": "2019-01-01T00:00:00.000Z",
            }
        ]
    }

    new_symbols = collect_new_symbols(
        rows_by_exchange, {"CHEEMS"}, now=NOW, only_symbols=frozenset({"CHEEMS"})
    )

    assert [new_symbol.lookup_symbol for new_symbol in new_symbols] == ["CHEEMS"]


def test_known_cmc_ids_by_symbol_ignores_a_ticker_with_conflicting_ids():
    rows_by_exchange = {
        "binance-spot": [
            {"id": "a", "symbol": "AAA", "cmc_id": 1},
            {"id": "b", "symbol": "AAA", "cmc_id": 1},
            {"id": "c", "symbol": "BBB", "cmc_id": 2},
            {"id": "d", "symbol": "BBB", "cmc_id": 3},
        ]
    }

    assert known_cmc_ids_by_symbol(rows_by_exchange) == {"AAA": 1}


def test_decide_approves_a_single_price_compatible_candidate_without_an_llm():
    evidence = _evidence("NEW", (_asset(100, "NEW", "new-token", 2.0),), price=2.0)

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.APPROVED
    assert decision.cmc_id == 100
    assert decision.method == "unique_ticker_price_compatible"


def test_decide_reports_an_unmapped_ticker_without_calling_the_llm():
    evidence = _evidence("AAPLB", (), price=255.0)

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.UNMAPPED
    assert decision.cmc_id is None
    assert decision.method == "no_ticker_candidate"


def test_decide_asks_the_llm_to_break_a_ticker_collision():
    evidence = _evidence(
        "PEPE",
        (
            _asset(22454, "PEPE", "pepe-2", 0.9, "Pepe 2.0"),
            _asset(24478, "PEPE", "pepe", 1.0, "Pepe"),
        ),
        price=1.0,
    )
    client = FakeChatClient(
        [{"cmc_id": 24478, "confidence": "high", "reasoning": "Binance lists Pepe."}]
    )

    decision = decide(evidence, client)

    assert decision.status is MatchStatus.APPROVED
    assert (decision.cmc_id, decision.slug) == (24478, "pepe")
    assert decision.method == "llm_adjudicated"
    prompt = json.loads(client.prompts[0])
    assert {candidate["cmc_id"] for candidate in prompt["candidates"]} == {22454, 24478}
    assert prompt["exchange_asset"]["lookup_ticker"] == "PEPE"


def test_decide_does_not_trust_an_llm_id_that_is_not_a_candidate():
    evidence = _evidence(
        "AAA",
        (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 5.0)),
        price=1.0,
    )

    decision = decide(evidence, FakeChatClient([{"cmc_id": 999, "confidence": "high"}]))

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.cmc_id is None
    assert decision.method == "llm_chose_unlisted_id"


def test_decide_downgrades_a_confident_match_that_the_price_contradicts():
    evidence = _evidence(
        "AAA",
        (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 50.0)),
        price=1.0,
    )

    decision = decide(
        evidence, FakeChatClient([{"cmc_id": 2, "confidence": "high", "reasoning": "name"}])
    )

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.cmc_id == 2
    assert "Price evidence disagrees" in decision.rationale


def test_decide_records_an_llm_rejection_as_unmapped():
    evidence = _evidence(
        "AAA",
        (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.01)),
        price=1.0,
    )

    decision = decide(
        evidence,
        FakeChatClient([{"cmc_id": None, "confidence": "high", "reasoning": "no match"}]),
    )

    assert decision.status is MatchStatus.UNMAPPED
    assert decision.method == "llm_rejected_all_candidates"


def test_decide_keeps_a_medium_confidence_match_for_human_review():
    evidence = _evidence(
        "AAA",
        (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.01)),
        price=1.0,
    )

    decision = decide(
        evidence, FakeChatClient([{"cmc_id": 1, "confidence": "medium", "reasoning": "maybe"}])
    )

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.cmc_id == 1
    assert decision.confidence == "medium"


def test_decide_without_an_llm_leaves_an_ambiguous_ticker_uncertain():
    evidence = _evidence(
        "AAA",
        (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.01)),
        price=1.0,
    )

    decision = decide(evidence, None)

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.method == "deterministic_only"


def test_decide_marks_an_llm_outage_as_uncertain_rather_than_failing():
    evidence = _evidence(
        "AAA",
        (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.01)),
        price=1.0,
    )

    decision = decide(evidence, FakeChatClient([]))

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.method == "llm_unavailable"


def test_resolve_new_symbols_reuses_an_id_the_snapshots_already_carry():
    occurrence = SymbolOccurrence(
        "binance-futures", "btcusdt", "BTC", NOW - timedelta(days=1), "perpetual"
    )
    decisions = resolve_new_symbols(
        [NewSymbol("BTC", (occurrence,))],
        _catalogue(_asset(1, "BTC", "bitcoin", 64000.0)),
        {"BTC": 1},
        {},
        {},
        ForbiddenChatClient(),
    )

    assert decisions[0].status is MatchStatus.APPROVED
    assert decisions[0].method == "existing_binance_mapping"
    assert decisions[0].cmc_id == 1
    assert decisions[0].slug == "bitcoin"


def test_resolve_new_symbols_stops_spending_llm_calls_at_the_budget():
    catalogue = _catalogue(
        _asset(1, "AAA", "alpha", 1.0),
        _asset(2, "AAA", "beta", 2.0),
        _asset(3, "BBB", "gamma", 1.0),
        _asset(4, "BBB", "delta", 2.0),
    )
    new_symbols = [
        NewSymbol(
            ticker,
            (
                SymbolOccurrence(
                    "binance-spot", f"{ticker.lower()}usdt", ticker, NOW, "spot"
                ),
            ),
        )
        for ticker in ("AAA", "BBB")
    ]

    decisions = resolve_new_symbols(
        new_symbols,
        catalogue,
        {},
        {},
        {},
        FakeChatClient([{"cmc_id": 1, "confidence": "high", "reasoning": "first"}]),
        max_llm_symbols=1,
    )

    assert decisions[0].status is MatchStatus.APPROVED
    assert decisions[1].status is MatchStatus.UNCERTAIN
    assert decisions[1].method == "llm_budget_exhausted"


def test_apply_decisions_fills_new_rows_and_never_replaces_an_existing_id():
    rows_by_exchange = {
        "binance-spot": [
            {"id": "newusdt", "symbol": "NEW"},
            {"id": "newbtc", "symbol": "NEW"},
            {"id": "oldusdt", "symbol": "NEW", "cmc_id": 777},
        ]
    }
    new_symbol = NewSymbol(
        "NEW", (_occurrence("NEW", "newusdt"), _occurrence("NEW", "newbtc"))
    )
    decision = Decision(
        evidence=_evidence_for(new_symbol, (_asset(100, "NEW", "new-token", 1.0),), 1.0),
        status=MatchStatus.APPROVED,
        cmc_id=100,
        slug="new-token",
        method="unique_ticker_price_compatible",
        confidence="high",
        rationale="ok",
    )

    updated = apply_decisions(rows_by_exchange, [decision])

    assert updated == {"binance-spot": 2}
    assert [row.get("cmc_id") for row in rows_by_exchange["binance-spot"]] == [100, 100, 777]


def test_apply_decisions_ignores_uncertain_and_unmapped_decisions():
    rows_by_exchange = {"binance-spot": [{"id": "newusdt", "symbol": "NEW"}]}
    evidence = _evidence("NEW", (_asset(100, "NEW", "new-token", 1.0),), price=1.0)
    decisions = [
        Decision(evidence, MatchStatus.UNCERTAIN, 100, "new-token", "llm_adjudicated", "low", "?"),
        Decision(evidence, MatchStatus.UNMAPPED, None, "", "no_ticker_candidate", "high", "-"),
    ]

    assert apply_decisions(rows_by_exchange, decisions) == {}
    assert "cmc_id" not in rows_by_exchange["binance-spot"][0]


def test_record_decisions_stores_one_reviewable_row_per_instrument(tmp_path):
    store = MappingStore(tmp_path / "cmc_mappings.json")
    evidence = _evidence(
        "AAA",
        (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.01)),
        price=1.0,
    )
    decisions = [
        Decision(evidence, MatchStatus.UNCERTAIN, 1, "alpha", "llm_adjudicated", "medium", "maybe")
    ]

    assert record_decisions(store, decisions, recorded_at=NOW) == {
        "recorded": 1,
        "conflicts": 0,
    }
    store.save()
    restored = MappingStore.load(tmp_path / "cmc_mappings.json")

    assert symbols_to_skip(restored, NOW) == frozenset({"AAA"})
    mapping = restored.mappings[0]
    assert (mapping.status, mapping.cmc_id, mapping.symbol) == ("uncertain", 1, "AAA")
    assert mapping.evidence["confidence"] == "medium"
    assert [candidate["cmc_id"] for candidate in mapping.evidence["candidates"]] == [1, 2]
    assert mapping.evidence["exchange_price"]["venue"] == "binance-spot"


def test_record_decisions_keeps_an_unmapped_ticker_so_it_is_not_re_asked(tmp_path):
    store = MappingStore(tmp_path / "cmc_mappings.json")
    evidence = _evidence("AAPLB", (), price=255.0)
    decisions = [
        Decision(evidence, MatchStatus.UNMAPPED, None, "", "no_ticker_candidate", "high", "none")
    ]

    record_decisions(store, decisions, recorded_at=NOW)
    store.save()
    restored = MappingStore.load(tmp_path / "cmc_mappings.json")

    assert restored.mappings[0].cmc_id is None
    assert symbols_to_skip(restored, NOW) == frozenset({"AAPLB"})


def test_render_report_separates_approved_from_uncertain_matches():
    approved = Decision(
        _evidence("NEW", (_asset(100, "NEW", "new-token", 1.0),), price=1.0),
        MatchStatus.APPROVED,
        100,
        "new-token",
        "unique_ticker_price_compatible",
        "high",
        "price agrees",
    )
    uncertain = Decision(
        _evidence("AAA", (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.01)), price=1.0),
        MatchStatus.UNCERTAIN,
        1,
        "alpha",
        "llm_adjudicated",
        "medium",
        "two projects share the ticker",
    )

    report = render_report([approved, uncertain], NOW)

    assert "approved (written to snapshots): **1**" in report
    assert "uncertain (needs human review): **1**" in report
    assert "### Approved" in report
    assert "### Uncertain — please review" in report
    assert "`NEW`" in report
    assert "new-token" in report
    assert "Candidates: 1 (alpha), 2 (beta)" in report


def test_run_writes_approved_ids_a_decision_store_and_a_review_report(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-spot.json").write_text(
        json.dumps(
            [
                {
                    "id": "newusdt",
                    "type": "spot",
                    "symbol": "NEW",
                    "first_capture": "2026-09-05T00:00:00.000Z",
                },
                {
                    "id": "ambigusdt",
                    "type": "spot",
                    "symbol": "AMBIG",
                    "first_capture": "2026-09-06T00:00:00.000Z",
                },
                {
                    "id": "staleusdt",
                    "type": "spot",
                    "symbol": "STALE",
                    "first_capture": "2020-01-01T00:00:00.000Z",
                },
            ],
            indent=2,
        )
    )
    catalogue = _catalogue(
        _asset(100, "NEW", "new-token", 2.0),
        _asset(200, "AMBIG", "ambig-one", 5.0),
        _asset(201, "AMBIG", "ambig-two", 5.0),
    )
    close_time_ms = int(OBSERVED_AT.timestamp() * 1000)
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_cmc_catalogue",
        lambda *_: catalogue,
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_spot_prices",
        lambda: [
            {"symbol": "NEWUSDT", "lastPrice": "2.0", "closeTime": close_time_ms},
            {"symbol": "AMBIGUSDT", "lastPrice": "5.0", "closeTime": close_time_ms},
        ],
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_futures_prices", list
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_public_assets",
        lambda: [{"assetCode": "NEW", "assetName": "New Token"}],
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping._chat_client",
        lambda *_: FakeChatClient(
            [{"cmc_id": 201, "confidence": "low", "reasoning": "unclear"}]
        ),
    )

    summary = run(
        data_dir=data_dir,
        exchanges=("binance-spot",),
        report_path=tmp_path / "report.md",
        summary_path=tmp_path / "summary.json",
        now=NOW,
    )

    rows = json.loads((data_dir / "binance-spot.json").read_text())
    assert {row["id"]: row.get("cmc_id") for row in rows} == {
        "newusdt": 100,
        "ambigusdt": None,
        "staleusdt": None,
    }
    assert summary["approved"] == 1
    assert summary["uncertain"] == 1
    assert summary["rows_updated"] == {"binance-spot": 1}
    assert summary["has_changes"] is True
    store = MappingStore.load(data_dir / "cmc_mappings.json")
    assert symbols_to_skip(store, NOW) == frozenset({"NEW", "AMBIG"})
    report = (tmp_path / "report.md").read_text()
    assert "### Approved" in report
    assert "### Uncertain" in report
    assert json.loads((tmp_path / "summary.json").read_text())["approved"] == 1


def test_run_reports_no_work_when_no_new_symbol_needs_an_id(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-spot.json").write_text(
        json.dumps([{"id": "btcusdt", "symbol": "BTC", "cmc_id": 1}], indent=2)
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_cmc_catalogue",
        lambda *_: _catalogue(_asset(1, "BTC", "bitcoin", 64000.0)),
    )

    summary = run(
        data_dir=data_dir,
        exchanges=("binance-spot",),
        report_path=tmp_path / "report.md",
        now=NOW,
    )

    assert summary["has_changes"] is False
    assert summary["new_symbols"] == 0
    assert not (data_dir / "cmc_mappings.json").exists()


def test_run_leaves_snapshots_untouched_on_a_dry_run(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    original = json.dumps(
        [
            {
                "id": "newusdt",
                "symbol": "NEW",
                "first_capture": "2026-09-05T00:00:00.000Z",
            }
        ],
        indent=2,
    )
    (data_dir / "binance-spot.json").write_text(original)
    close_time_ms = int(OBSERVED_AT.timestamp() * 1000)
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_cmc_catalogue",
        lambda *_: _catalogue(_asset(100, "NEW", "new-token", 2.0)),
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_spot_prices",
        lambda: [{"symbol": "NEWUSDT", "lastPrice": "2.0", "closeTime": close_time_ms}],
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_futures_prices", list
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_public_assets", list
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping._chat_client", lambda *_: None
    )

    summary = run(
        data_dir=data_dir, exchanges=("binance-spot",), dry_run=True, now=NOW
    )

    assert summary["approved"] == 1
    assert (data_dir / "binance-spot.json").read_text() == original
    assert not (data_dir / "cmc_mappings.json").exists()


def test_record_decisions_reports_a_conflict_with_an_earlier_approval(tmp_path, capsys):
    store = MappingStore(tmp_path / "cmc_mappings.json")
    new_symbol = NewSymbol("AAA", (_occurrence("AAA", "aaausdt"),))
    assets = (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.0))
    evidence = _evidence_for(new_symbol, assets, 1.0)
    approved = Decision(evidence, MatchStatus.APPROVED, 1, "alpha", "llm_adjudicated", "high", "a")
    record_decisions(store, [approved], recorded_at=NOW)

    contradicting = Decision(
        evidence, MatchStatus.APPROVED, 2, "beta", "llm_adjudicated", "high", "b"
    )
    recording = record_decisions(store, [contradicting], recorded_at=NOW)

    assert recording == {"recorded": 0, "conflicts": 1}
    assert store.mappings[0].cmc_id == 1
    assert "refusing to replace an approved CMC mapping" in capsys.readouterr().err


def test_symbols_to_skip_revisits_an_unmapped_ticker_after_the_recheck_window(tmp_path):
    store = MappingStore(tmp_path / "cmc_mappings.json")
    stale = _evidence("AAPLB", (), price=None)
    record_decisions(
        store,
        [Decision(stale, MatchStatus.UNMAPPED, None, "", "no_ticker_candidate", "high", "-")],
        recorded_at=NOW - timedelta(days=120),
    )
    fresh = _evidence("BOT", (), price=None)
    record_decisions(
        store,
        [Decision(fresh, MatchStatus.UNMAPPED, None, "", "no_ticker_candidate", "high", "-")],
        recorded_at=NOW - timedelta(days=10),
    )

    assert symbols_to_skip(store, NOW, recheck_unmapped_after_days=90) == frozenset({"BOT"})


def test_symbols_to_skip_never_expires_an_approved_ticker(tmp_path):
    store = MappingStore(tmp_path / "cmc_mappings.json")
    evidence = _evidence("NEW", (_asset(100, "NEW", "new-token", 1.0),), price=1.0)
    record_decisions(
        store,
        [
            Decision(
                evidence,
                MatchStatus.APPROVED,
                100,
                "new-token",
                "unique_ticker_price_compatible",
                "high",
                "ok",
            )
        ],
        recorded_at=NOW - timedelta(days=900),
    )

    assert symbols_to_skip(store, NOW) == frozenset({"NEW"})


def test_symbols_to_skip_rejects_a_negative_recheck_window(tmp_path):
    with pytest.raises(ValueError, match="recheck_unmapped_after_days"):
        symbols_to_skip(
            MappingStore(tmp_path / "cmc_mappings.json"), NOW, recheck_unmapped_after_days=-1
        )


def test_pending_proposed_symbols_unions_open_pull_request_ledgers(tmp_path):
    first = MappingStore(tmp_path / "pr-1.json")
    record_decisions(
        first,
        [
            Decision(
                _evidence("AAA", (), price=None),
                MatchStatus.UNMAPPED,
                None,
                "",
                "no_ticker_candidate",
                "high",
                "-",
            )
        ],
        recorded_at=NOW - timedelta(days=400),
    )
    first.save()
    second = MappingStore(tmp_path / "pr-2.json")
    record_decisions(
        second,
        [
            Decision(
                _evidence("BBB", (_asset(2, "BBB", "beta", 1.0),), price=1.0),
                MatchStatus.UNCERTAIN,
                2,
                "beta",
                "llm_adjudicated",
                "medium",
                "?",
            )
        ],
        recorded_at=NOW,
    )
    second.save()

    # A pending proposal is skipped whatever its verdict and however old it is:
    # it is already awaiting review, unlike a merged unmapped verdict.
    assert pending_proposed_symbols(
        [tmp_path / "pr-1.json", tmp_path / "pr-2.json"]
    ) == frozenset({"AAA", "BBB"})


def test_pending_proposed_symbols_ignores_a_missing_or_broken_ledger(tmp_path, capsys):
    (tmp_path / "broken.json").write_text("{not json")

    assert pending_proposed_symbols(
        [tmp_path / "absent.json", tmp_path / "broken.json"]
    ) == frozenset()
    assert "ignoring unreadable pending mapping store" in capsys.readouterr().err


def test_run_does_not_re_propose_a_ticker_awaiting_review(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-spot.json").write_text(
        json.dumps(
            [
                {
                    "id": "newusdt",
                    "symbol": "NEW",
                    "first_capture": "2026-09-05T00:00:00.000Z",
                }
            ],
            indent=2,
        )
    )
    pending = MappingStore(tmp_path / "pending.json")
    record_decisions(
        pending,
        [
            Decision(
                _evidence("NEW", (_asset(100, "NEW", "new-token", 2.0),), price=2.0),
                MatchStatus.UNCERTAIN,
                100,
                "new-token",
                "llm_adjudicated",
                "medium",
                "awaiting review",
            )
        ],
        recorded_at=NOW,
    )
    pending.save()
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_cmc_catalogue",
        lambda *_: _catalogue(_asset(100, "NEW", "new-token", 2.0)),
    )

    summary = run(
        data_dir=data_dir,
        exchanges=("binance-spot",),
        pending_mapping_paths=(tmp_path / "pending.json",),
        now=NOW,
    )

    assert summary["new_symbols"] == 0
    assert summary["has_changes"] is False
    assert json.loads((data_dir / "binance-spot.json").read_text())[0].get("cmc_id") is None


def test_binance_exchanges_cover_the_bundled_binance_snapshots():
    assert set(BINANCE_EXCHANGES) == {
        "binance-spot",
        "binance-futures",
        "binance-futures-cm",
    }


@pytest.mark.parametrize("days", [-1])
def test_collect_new_symbols_rejects_a_negative_window(days):
    with pytest.raises(ValueError, match="new_within_days"):
        collect_new_symbols({}, set(), now=NOW, new_within_days=days)
