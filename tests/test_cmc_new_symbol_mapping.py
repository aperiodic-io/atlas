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
    DEFAULT_EXCHANGES,
    DEFAULT_SKIPPED_UNDERLYINGS,
    LlmAvailability,
    Decision,
    MatchStatus,
    NewSymbol,
    SymbolOccurrence,
    apply_decisions,
    build_symbol_evidence,
    collect_new_symbols,
    catalogue_trust,
    concurrent_cmc_id,
    coverage_report,
    fetch_price_observations,
    decide,
    identity_match,
    instances_to_skip,
    mapped_windows_by_symbol,
    partition_conflicts,
    pending_proposed_instances,
    record_decisions,
    render_coverage_report,
    render_report,
    resolve_new_symbols,
    run,
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


def _catalogue(*assets: CmcAsset, complete: bool = True) -> CmcCatalogue:
    count = len(assets)
    return CmcCatalogue(
        assets,
        CatalogueDiagnostics(
            # A reported total above the unique count is how the keyless endpoint
            # signals that a row may be missing from this page.
            count if complete else count + 1,
            count,
            count,
            (),
            0,
            False,
        ),
    )


def _asset(cmc_id: int, symbol: str, slug: str, price: float, name: str = "") -> CmcAsset:
    return CmcAsset(cmc_id, symbol, slug, price, OBSERVED_AT, True, name or slug.title())


def _observation(
    symbol: str, price: float, venue: str = "binance-futures"
) -> PriceObservation:
    return PriceObservation(price, "USDT", OBSERVED_AT, venue, f"{symbol}USDT")


def _occurrence(
    exchange_symbol: str,
    original_id: str | None = None,
    exchange: str = "binance-futures",
    first_capture: datetime | None = None,
    end_date: datetime | None = None,
) -> SymbolOccurrence:
    return SymbolOccurrence(
        exchange,
        original_id or f"{exchange_symbol.lower()}usdt",
        exchange_symbol,
        first_capture or NOW - timedelta(days=1),
        "perpetual",
        end_date,
    )


def _evidence_for(
    new_symbol: NewSymbol,
    assets: tuple[CmcAsset, ...],
    price: float | None,
    binance_name: str | None = None,
    catalogue: CmcCatalogue | None = None,
):
    observations: dict[str, dict[str, PriceObservation]] = {}
    if price is not None:
        for occurrence in new_symbol.occurrences:
            observations.setdefault(occurrence.exchange, {})[
                occurrence.symbol.upper()
            ] = _observation(occurrence.symbol, price, occurrence.exchange)
    public_assets = (
        {}
        if binance_name is None
        else {
            new_symbol.lookup_symbol: {
                "assetCode": new_symbol.lookup_symbol,
                "assetName": binance_name,
            }
        }
    )
    return build_symbol_evidence(
        new_symbol, catalogue or _catalogue(*assets), observations, public_assets
    )


def _evidence(
    lookup_symbol: str,
    assets: tuple[CmcAsset, ...],
    price: float | None,
    binance_name: str | None = None,
    catalogue: CmcCatalogue | None = None,
):
    return _evidence_for(
        NewSymbol(lookup_symbol, (_occurrence(lookup_symbol),)),
        assets,
        price,
        binance_name=binance_name,
        catalogue=catalogue,
    )


# --------------------------------------------------------------------------- #
# scope and collection
# --------------------------------------------------------------------------- #


def test_fetch_price_observations_reports_a_binance_refusal_instead_of_raising(monkeypatch, capsys):
    """Regression: Binance answers 451 from some hosts; that must not abort a run."""
    import urllib.error

    def _refuse():
        raise urllib.error.HTTPError("https://fapi.binance.com", 451, "", {}, None)

    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_futures_prices", _refuse
    )
    monkeypatch.setattr("integrations.cmc_new_symbol_mapping.fetch_spot_prices", list)
    new_symbols = [NewSymbol("NEW", (_occurrence("NEW"),))]

    observations, available = fetch_price_observations(new_symbols, ("binance-futures",))

    assert available is False
    assert observations == {"binance-futures": {}}
    assert "Binance prices unavailable" in capsys.readouterr().err


def test_decide_withholds_approval_when_binance_prices_were_unavailable():
    """Without a price, nothing corroborates the name match, so hold for review."""
    new_symbol = NewSymbol("NEW", (_occurrence("NEW"),))
    evidence = build_symbol_evidence(
        new_symbol,
        _catalogue(_asset(100, "NEW", "new-token", 2.0, "New Token")),
        {},
        {"NEW": {"assetCode": "NEW", "assetName": "New Token"}},
        exchange_prices_available=False,
    )

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.cmc_id == 100
    assert "Binance prices were unavailable" in decision.rationale


def test_run_completes_and_approves_nothing_when_binance_refuses(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-futures.json").write_text(
        json.dumps(
            [{"id": "newusdt", "symbol": "NEW", "first_capture": "2026-09-05T00:00:00.000Z"}],
            indent=2,
        )
    )
    import urllib.error

    def _refuse():
        raise urllib.error.HTTPError("https://fapi.binance.com", 451, "", {}, None)

    _patch_fetchers(
        monkeypatch,
        _catalogue(_asset(100, "NEW", "new-token", 2.0, "New Token")),
        [],
        [{"assetCode": "NEW", "assetName": "New Token"}],
        None,
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_futures_prices", _refuse
    )

    summary = run(
        data_dir=data_dir, exchanges=("binance-futures",), dry_run=True, now=NOW
    )

    assert summary["exchange_prices_available"] is False
    assert summary["approved"] == 0
    assert summary["uncertain"] == 1


# --------------------------------------------------------------------------- #
# catalogue trust
# --------------------------------------------------------------------------- #


def _diagnostics(
    reported_total: int | None,
    unique_ids: int,
    duplicate_ids: tuple[int, ...] = (),
    malformed_rows: int = 0,
    total_count_changed: bool = False,
    reported_total_spread: int = 0,
) -> CatalogueDiagnostics:
    return CatalogueDiagnostics(
        reported_total,
        unique_ids,
        unique_ids,
        duplicate_ids,
        malformed_rows,
        total_count_changed,
        reported_total_spread,
    )


def test_catalogue_trust_tolerates_the_gap_production_actually_returns():
    """The keyless endpoint reported 8184 and returned 8179 unique IDs.

    Demanding exact equality blocks every approval forever, so a proportionate
    gap has to pass while still being recorded.
    """
    trustworthy, issue = catalogue_trust(_diagnostics(8184, 8179))

    assert trustworthy is True
    assert "tolerated 5 unreadable of 8184 rows" in issue


def test_catalogue_trust_tolerates_a_total_that_moved_mid_fetch():
    """Regression: run 4 reported 8183/8182 with the total moving by 1.

    CMC lists assets continuously, so the total shifting between pages is routine
    churn. Vetoing on it blocked a whole run whose real gap was one row.
    """
    trustworthy, issue = catalogue_trust(
        _diagnostics(8183, 8182, total_count_changed=True, reported_total_spread=1)
    )

    assert trustworthy is True
    assert "total moved by 1 mid-fetch" in issue
    assert "within 0.500%" in issue


def test_catalogue_trust_blocks_a_total_that_moved_wildly():
    trustworthy, issue = catalogue_trust(
        _diagnostics(8183, 8183, total_count_changed=True, reported_total_spread=900)
    )

    assert trustworthy is False
    assert "above the" in issue


def test_catalogue_trust_accepts_a_catalogue_that_grew_while_being_read():
    """More unique IDs than the first page claimed is growth, not incoherence."""
    trustworthy, issue = catalogue_trust(_diagnostics(8183, 8185))

    assert trustworthy is True
    assert issue == ""


def test_catalogue_trust_is_silent_on_a_perfect_catalogue():
    assert catalogue_trust(_diagnostics(8184, 8184)) == (True, "")


def test_catalogue_trust_blocks_a_gap_beyond_the_tolerance():
    trustworthy, issue = catalogue_trust(_diagnostics(8184, 7000))

    assert trustworthy is False
    assert "above the" in issue


def test_catalogue_trust_fails_closed_without_a_total_to_compare_against():
    """The one defect no tolerance can rescue: nothing to measure against."""
    trustworthy, issue = catalogue_trust(_diagnostics(None, 100))

    assert trustworthy is False
    assert "no total to compare" in issue


def test_catalogue_trust_tolerates_the_numbers_the_first_real_run_produced():
    """Regression: the run reported 8183/8176 with one unparseable row.

    A hard veto on any unparseable row blocked every approval, including a clean
    name match on MARSCOIN. One bad row among 8,183 is as routine as the dedup
    gap, so it belongs in the same proportionate budget, not in a categorical veto.
    """
    trustworthy, issue = catalogue_trust(_diagnostics(8183, 8176, malformed_rows=1))

    assert trustworthy is True
    assert "1 unparseable" in issue
    assert "within 0.500%" in issue


def test_catalogue_trust_reports_every_cause_without_vetoing_on_any():
    trustworthy, issue = catalogue_trust(
        _diagnostics(
            8183, 8180, duplicate_ids=(1, 2), malformed_rows=1, reported_total_spread=1
        )
    )

    assert trustworthy is True
    assert "1 unparseable" in issue
    assert "2 duplicated" in issue
    assert "total moved by 1 mid-fetch" in issue


def test_catalogue_trust_reports_duplicates_without_vetoing_on_them():
    trustworthy, issue = catalogue_trust(
        _diagnostics(8183, 8176, duplicate_ids=(1, 2, 3))
    )

    assert trustworthy is True
    assert "3 duplicated" in issue
    assert "within 0.500%" in issue


def test_catalogue_trust_still_blocks_a_schema_break_that_drops_many_rows():
    trustworthy, issue = catalogue_trust(_diagnostics(8183, 4000, malformed_rows=4183))

    assert trustworthy is False
    assert "above the" in issue
    assert "4183 unparseable" in issue


def test_catalogue_trust_rejects_a_negative_tolerance():
    with pytest.raises(ValueError, match="max_gap_ratio"):
        catalogue_trust(_diagnostics(100, 100), max_gap_ratio=-0.1)


def test_decide_approves_through_a_tolerated_catalogue_gap():
    """The whole point: a 5-in-8184 gap must not block an identity match."""
    assets = (_asset(100, "NEW", "new-token", 2.0, "New Token"),)
    catalogue = CmcCatalogue(assets, _diagnostics(8184, 8179))
    evidence = _evidence(
        "NEW", assets, price=2.0, binance_name="New Token", catalogue=catalogue
    )

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.APPROVED
    assert decision.cmc_id == 100


def test_decide_approves_despite_one_unparseable_catalogue_row():
    """Regression: MARSCOIN had a name match and an agreeing price, and was held
    for review only because one of 8,183 catalogue rows failed to parse."""
    assets = (_asset(100, "NEW", "new-token", 2.0, "New Token"),)
    catalogue = CmcCatalogue(assets, _diagnostics(8183, 8176, malformed_rows=1))
    evidence = _evidence(
        "NEW", assets, price=2.0, binance_name="New Token", catalogue=catalogue
    )

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.APPROVED
    assert decision.cmc_id == 100


def test_decide_withholds_approval_when_the_catalogue_lost_too_many_rows():
    assets = (_asset(100, "NEW", "new-token", 2.0, "New Token"),)
    catalogue = CmcCatalogue(assets, _diagnostics(8183, 4000))
    evidence = _evidence(
        "NEW", assets, price=2.0, binance_name="New Token", catalogue=catalogue
    )

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.UNCERTAIN
    assert "above the" in decision.rationale


# --------------------------------------------------------------------------- #
# crypto-only scope
# --------------------------------------------------------------------------- #


def test_run_opens_nothing_to_review_when_a_configured_llm_is_dead(tmp_path, monkeypatch):
    """Regression: two PRs full of LLM-outage notices once blocked every later run.

    The pending-ledger skip treats an open PR as work already awaiting review, so
    a junk PR is worse than no PR. With nothing approved and a broken endpoint,
    there is nothing a reviewer can act on.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-futures.json").write_text(
        json.dumps(
            [
                {
                    "id": "aaausdt",
                    "symbol": "AAA",
                    "underlying": "crypto",
                    "first_capture": "2026-09-05T00:00:00.000Z",
                }
            ],
            indent=2,
        )
    )
    close_time_ms = int(OBSERVED_AT.timestamp() * 1000)
    _patch_fetchers(
        monkeypatch,
        _catalogue(_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.0)),
        [{"symbol": "AAAUSDT", "lastPrice": "1.0", "closeTime": close_time_ms}],
        [],
        None,
        llm_status="unavailable",
        llm_error="410 Client Error: Gone",
    )

    summary = run(
        data_dir=data_dir,
        exchanges=("binance-futures",),
        report_path=tmp_path / "report.md",
        now=NOW,
    )

    assert summary["llm_status"] == "unavailable"
    assert summary["approved"] == 0
    assert summary["worth_reviewing"] is False
    report = (tmp_path / "report.md").read_text()
    assert "had no working LLM" in report
    assert "410 Client Error: Gone" in report


def test_run_is_still_worth_reviewing_when_a_dead_llm_did_not_stop_approvals(
    tmp_path, monkeypatch
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-futures.json").write_text(
        json.dumps(
            [
                {
                    "id": "newusdt",
                    "symbol": "NEW",
                    "underlying": "crypto",
                    "first_capture": "2026-09-05T00:00:00.000Z",
                }
            ],
            indent=2,
        )
    )
    close_time_ms = int(OBSERVED_AT.timestamp() * 1000)
    _patch_fetchers(
        monkeypatch,
        _catalogue(_asset(100, "NEW", "new-token", 2.0, "New Token")),
        [{"symbol": "NEWUSDT", "lastPrice": "2.0", "closeTime": close_time_ms}],
        [{"assetCode": "NEW", "assetName": "New Token"}],
        None,
        llm_status="unavailable",
        llm_error="410 Client Error: Gone",
    )

    summary = run(data_dir=data_dir, exchanges=("binance-futures",), now=NOW)

    assert summary["approved"] == 1
    assert summary["worth_reviewing"] is True


def test_run_is_worth_reviewing_when_no_llm_was_configured_on_purpose(tmp_path, monkeypatch):
    """A deliberate absence is not a misconfiguration, so the PR still opens."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-futures.json").write_text(
        json.dumps(
            [
                {
                    "id": "aaausdt",
                    "symbol": "AAA",
                    "underlying": "crypto",
                    "first_capture": "2026-09-05T00:00:00.000Z",
                }
            ],
            indent=2,
        )
    )
    close_time_ms = int(OBSERVED_AT.timestamp() * 1000)
    _patch_fetchers(
        monkeypatch,
        _catalogue(_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.0)),
        [{"symbol": "AAAUSDT", "lastPrice": "1.0", "closeTime": close_time_ms}],
        [],
        None,
        llm_status="not_configured",
    )

    summary = run(data_dir=data_dir, exchanges=("binance-futures",), now=NOW)

    assert summary["llm_status"] == "not_configured"
    assert summary["worth_reviewing"] is True


def test_default_scope_skips_tokenized_equities_and_index_products():
    assert set(DEFAULT_SKIPPED_UNDERLYINGS) == {
        "equity",
        "index",
        "commodity",
        "pre_market",
    }


def _row(symbol: str, underlying: str | None, captured: str = "2026-09-05T00:00:00.000Z"):
    row = {"id": f"{symbol.lower()}usdt", "symbol": symbol, "first_capture": captured}
    if underlying is not None:
        row["underlying"] = underlying
    return row


def test_collect_new_symbols_leaves_out_declared_non_crypto_underlyings():
    """Tokenized equities dominated a real run: 32 of 38 review rows were `equity`."""
    rows_by_exchange = {
        "binance-futures": [
            _row("MARSCOIN", "crypto"),
            _row("DDOG", "equity"),
            _row("GDX", "index"),
            _row("XAU", "commodity"),
            _row("PREIPO", "pre_market"),
        ]
    }

    new_symbols = collect_new_symbols(
        rows_by_exchange, {"MARSCOIN", "DDOG", "GDX", "XAU", "PREIPO"}, now=NOW
    )

    assert [s.lookup_symbol for s in new_symbols] == ["MARSCOIN"]


def test_collect_new_symbols_keeps_rows_with_no_underlying_and_unknown():
    """Absent `underlying` means a legacy crypto row (BTC, ADA, BNB), not an equity.

    Silently skipping a genuine new listing would be worse than a reviewer seeing
    a row that turns out not to be crypto, so `unknown` is kept too.
    """
    rows_by_exchange = {
        "binance-futures": [_row("BTC", None), _row("UNITREE", "unknown")]
    }

    new_symbols = collect_new_symbols(rows_by_exchange, {"BTC", "UNITREE"}, now=NOW)

    assert [s.lookup_symbol for s in new_symbols] == ["BTC", "UNITREE"]


def test_collect_new_symbols_honours_an_empty_skip_set():
    rows_by_exchange = {"binance-futures": [_row("DDOG", "equity")]}

    new_symbols = collect_new_symbols(
        rows_by_exchange, {"DDOG"}, now=NOW, skip_underlyings=frozenset()
    )

    assert [s.lookup_symbol for s in new_symbols] == ["DDOG"]


def test_only_symbols_overrides_the_underlying_filter():
    """Asking for a ticker by hand must not be silently refused by scope."""
    rows_by_exchange = {"binance-futures": [_row("DDOG", "equity")]}

    new_symbols = collect_new_symbols(
        rows_by_exchange, {"DDOG"}, now=NOW, only_symbols=frozenset({"DDOG"})
    )

    assert [s.lookup_symbol for s in new_symbols] == ["DDOG"]


def test_coverage_report_separates_out_of_scope_rows_from_missing_ones():
    rows_by_exchange = {
        "binance-futures": [
            _row("MARSCOIN", "crypto"),
            _row("DDOG", "equity"),
            _row("MRNA", "equity"),
            _row("BTC", None),
        ]
    }

    coverage = coverage_report(rows_by_exchange, NOW, 60)
    stats = coverage["exchanges"]["binance-futures"]

    assert stats["rows_in_window_missing_cmc_id"] == 2
    assert stats["rows_in_window_out_of_scope"] == 2
    assert stats["out_of_scope_underlyings"] == {"equity": 2}
    assert stats["tickers_in_window_missing_cmc_id"] == ["BTC", "MARSCOIN"]
    assert coverage["rows_in_window_out_of_scope"] == 2


def test_render_coverage_report_explains_what_was_skipped():
    rows_by_exchange = {"binance-futures": [_row("DDOG", "equity")]}

    report = render_coverage_report(coverage_report(rows_by_exchange, NOW, 60))

    assert "skipped as non-crypto" in report
    assert "`equity`" in report
    assert "Nothing in scope in the window is missing a CMC ID." in report


def test_run_does_not_resolve_a_tokenized_equity(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-futures.json").write_text(
        json.dumps(
            [
                {
                    "id": "ddogusdt",
                    "symbol": "DDOG",
                    "underlying": "equity",
                    "first_capture": "2026-09-05T00:00:00.000Z",
                }
            ],
            indent=2,
        )
    )
    _patch_fetchers(
        monkeypatch,
        _catalogue(_asset(41970, "DDOG", "datadog-inc-derivatives", 120.0, "Datadog")),
        [],
        [],
        ForbiddenChatClient(),
    )

    summary = run(data_dir=data_dir, exchanges=("binance-futures",), now=NOW)

    assert summary["new_symbols"] == 0
    assert summary["has_changes"] is False


def test_default_scope_is_binance_futures():
    assert DEFAULT_EXCHANGES == ("binance-futures",)


def test_collect_new_symbols_keeps_only_recent_rows_without_a_cmc_id():
    rows_by_exchange = {
        "binance-futures": [
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
            },
            {"id": "cheemsusdc", "symbol": "CHEEMS", "first_capture": "2026-09-02T00:00:00.000Z"},
        ]
    }

    new_symbols = collect_new_symbols(rows_by_exchange, {"CHEEMS"}, now=NOW)

    assert len(new_symbols) == 1
    assert new_symbols[0].lookup_symbol == "CHEEMS"
    assert new_symbols[0].exchange_symbols == ("1000CHEEMS", "CHEEMS")


def test_collect_new_symbols_skips_an_instrument_instance_a_previous_run_decided():
    rows_by_exchange = {
        "binance-futures": [
            {"id": "newusdt", "symbol": "NEW", "first_capture": "2026-09-05T00:00:00.000Z"}
        ]
    }
    decided = frozenset({"binance-futures:newusdt:2026-09-05T00:00:00Z"})

    assert collect_new_symbols(
        rows_by_exchange, {"NEW"}, now=NOW, decided_instances=decided
    ) == []


def test_collect_new_symbols_still_resolves_a_relisted_instrument_of_a_decided_ticker():
    """Regression: a decision about one instance must not silence a relisting.

    Keying the skip set by ticker meant a 2025 ABC decision suppressed a 2026 ABC
    instance forever, so a reused or relisted symbol could never get its own ID.
    """
    rows_by_exchange = {
        "binance-futures": [
            {
                "id": "abcusdt",
                "symbol": "ABC",
                "first_capture": "2026-09-05T00:00:00.000Z",
            }
        ]
    }
    # The ledger holds a *different* instance of the same ticker.
    decided = frozenset({"binance-futures:abcusdt:2025-01-01T00:00:00Z"})

    new_symbols = collect_new_symbols(
        rows_by_exchange, {"ABC"}, now=NOW, decided_instances=decided
    )

    assert [new_symbol.lookup_symbol for new_symbol in new_symbols] == ["ABC"]


def test_collect_new_symbols_narrows_to_requested_tickers_ignoring_age_and_history():
    rows_by_exchange = {
        "binance-futures": [
            {"id": "oldusdt", "symbol": "OLD", "first_capture": "2019-01-01T00:00:00.000Z"},
            {"id": "newusdt", "symbol": "NEW", "first_capture": "2026-09-05T00:00:00.000Z"},
        ]
    }

    new_symbols = collect_new_symbols(
        rows_by_exchange,
        {"OLD", "NEW"},
        now=NOW,
        decided_instances=frozenset({"binance-futures:oldusdt:2019-01-01T00:00:00Z"}),
        only_symbols=frozenset({"OLD"}),
    )

    assert [new_symbol.lookup_symbol for new_symbol in new_symbols] == ["OLD"]


def test_collect_new_symbols_rejects_a_negative_window():
    with pytest.raises(ValueError, match="new_within_days"):
        collect_new_symbols({}, set(), now=NOW, new_within_days=-1)


# --------------------------------------------------------------------------- #
# skip ledger
# --------------------------------------------------------------------------- #


def test_instances_to_skip_is_keyed_by_instrument_instance(tmp_path):
    store = MappingStore(tmp_path / "m.json")
    record_decisions(
        store,
        [
            Decision(
                _evidence("NEW", (_asset(1, "NEW", "new-token", 1.0),), 1.0),
                MatchStatus.APPROVED,
                1,
                "new-token",
                "identity_binance_asset_name",
                "high",
                "ok",
            )
        ],
        recorded_at=NOW,
    )

    assert instances_to_skip(store, NOW) == frozenset(
        {"binance-futures:newusdt:2026-09-10T12:00:00Z"}
    )


def test_instances_to_skip_revisits_an_unmapped_instance_after_the_recheck_window(tmp_path):
    store = MappingStore(tmp_path / "m.json")
    record_decisions(
        store,
        [
            Decision(
                _evidence("AAPLB", (), None),
                MatchStatus.UNMAPPED,
                None,
                "",
                "no_ticker_candidate",
                "high",
                "-",
            )
        ],
        recorded_at=NOW - timedelta(days=120),
    )
    record_decisions(
        store,
        [
            Decision(
                _evidence("BOT", (), None),
                MatchStatus.UNMAPPED,
                None,
                "",
                "no_ticker_candidate",
                "high",
                "-",
            )
        ],
        recorded_at=NOW - timedelta(days=10),
    )

    assert instances_to_skip(store, NOW, recheck_unmapped_after_days=90) == frozenset(
        {"binance-futures:botusdt:2026-09-10T12:00:00Z"}
    )


def test_instances_to_skip_never_expires_an_approved_decision(tmp_path):
    store = MappingStore(tmp_path / "m.json")
    record_decisions(
        store,
        [
            Decision(
                _evidence("NEW", (_asset(100, "NEW", "new-token", 1.0),), 1.0),
                MatchStatus.APPROVED,
                100,
                "new-token",
                "identity_binance_asset_name",
                "high",
                "ok",
            )
        ],
        recorded_at=NOW - timedelta(days=900),
    )

    assert len(instances_to_skip(store, NOW)) == 1


def test_instances_to_skip_rejects_a_negative_recheck_window(tmp_path):
    with pytest.raises(ValueError, match="recheck_unmapped_after_days"):
        instances_to_skip(
            MappingStore(tmp_path / "m.json"), NOW, recheck_unmapped_after_days=-1
        )


def test_pending_proposed_instances_unions_open_pull_request_ledgers(tmp_path):
    for name, ticker, recorded_at in (
        ("pr-1.json", "AAA", NOW - timedelta(days=400)),
        ("pr-2.json", "BBB", NOW),
    ):
        store = MappingStore(tmp_path / name)
        record_decisions(
            store,
            [
                Decision(
                    _evidence(ticker, (), None),
                    MatchStatus.UNMAPPED,
                    None,
                    "",
                    "no_ticker_candidate",
                    "high",
                    "-",
                )
            ],
            recorded_at=recorded_at,
        )
        store.save()

    # A pending proposal is skipped whatever its verdict and however old it is.
    assert pending_proposed_instances(
        [tmp_path / "pr-1.json", tmp_path / "pr-2.json"]
    ) == frozenset(
        {
            "binance-futures:aaausdt:2026-09-10T12:00:00Z",
            "binance-futures:bbbusdt:2026-09-10T12:00:00Z",
        }
    )


def test_pending_proposed_instances_ignores_a_missing_or_unparseable_ledger(tmp_path, capsys):
    (tmp_path / "broken.json").write_text("{not json")

    assert pending_proposed_instances(
        [tmp_path / "absent.json", tmp_path / "broken.json"]
    ) == frozenset()
    assert "ignoring unreadable pending mapping store" in capsys.readouterr().err


def test_pending_proposed_instances_ignores_valid_json_in_the_wrong_shape(tmp_path, capsys):
    """Regression: a reviewer-editable ledger must not abort a scheduled run.

    ``{"mappings": [{"instrument": {}}]}`` is valid JSON, so the loader used to
    raise ``KeyError`` straight past this boundary.
    """
    (tmp_path / "wrong-shape.json").write_text(
        json.dumps({"schema_version": 1, "mappings": [{"instrument": {}}]})
    )

    assert pending_proposed_instances([tmp_path / "wrong-shape.json"]) == frozenset()
    assert "ignoring unreadable pending mapping store" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# identity evidence
# --------------------------------------------------------------------------- #


def test_identity_match_accepts_an_exact_name_or_slug_match():
    asset = _asset(1, "PEPE", "pepe", 1.0, "Pepe")

    assert identity_match(asset, {"assetName": "Pepe"}) == "binance_asset_name"
    assert identity_match(asset, {"assetName": "pepe"}) == "binance_asset_name"
    assert (
        identity_match(_asset(2, "A", "arena-z", 1.0, "Other"), {"assetName": "Arena Z"})
        == "cmc_slug"
    )


def test_identity_match_refuses_a_near_miss_and_a_missing_asset():
    # "Pepe" must not identify "Pepe 2.0" -- the exact confusion it has to resolve.
    assert identity_match(_asset(1, "PEPE", "pepe-2", 1.0, "Pepe 2.0"), {"assetName": "Pepe"}) is None
    assert identity_match(_asset(1, "PEPE", "pepe", 1.0, "Pepe"), None) is None
    assert identity_match(_asset(1, "PEPE", "pepe", 1.0, "Pepe"), {}) is None
    assert identity_match(_asset(1, "PEPE", "pepe", 1.0, "Pepe"), {"assetName": ""}) is None


# --------------------------------------------------------------------------- #
# reuse of an existing mapping
# --------------------------------------------------------------------------- #


def test_concurrent_cmc_id_reuses_an_overlapping_instrument():
    windows = mapped_windows_by_symbol(
        {
            "binance-spot": [
                {
                    "id": "btcusdt",
                    "symbol": "BTC",
                    "cmc_id": 1,
                    "first_capture": "2020-01-01T00:00:00.000Z",
                }
            ]
        }
    )
    new_symbol = NewSymbol("BTC", (_occurrence("BTC", "btcusdc"),))

    assert concurrent_cmc_id(new_symbol, windows) == (1, "BTC")


def test_concurrent_cmc_id_refuses_a_relisted_ticker_whose_window_does_not_overlap():
    """Regression: a reused ticker must not inherit a delisted project's ID."""
    windows = mapped_windows_by_symbol(
        {
            "binance-spot": [
                {
                    "id": "abcusdt",
                    "symbol": "ABC",
                    "cmc_id": 10,
                    "first_capture": "2019-01-01T00:00:00.000Z",
                    "end_date": "2021-01-01T00:00:00.000Z",
                }
            ]
        }
    )
    relisted = NewSymbol(
        "ABC",
        (_occurrence("ABC", "abcusdt", first_capture=datetime(2026, 9, 1, tzinfo=UTC)),),
    )

    assert concurrent_cmc_id(relisted, windows) is None


def test_concurrent_cmc_id_refuses_a_ticker_mapped_to_several_ids():
    windows = mapped_windows_by_symbol(
        {
            "binance-spot": [
                {"id": "a", "symbol": "AAA", "cmc_id": 2, "first_capture": "2020-01-01T00:00:00.000Z"},
                {"id": "b", "symbol": "AAA", "cmc_id": 3, "first_capture": "2020-01-01T00:00:00.000Z"},
            ]
        }
    )

    assert concurrent_cmc_id(NewSymbol("AAA", (_occurrence("AAA"),)), windows) is None


# --------------------------------------------------------------------------- #
# the decision ladder
# --------------------------------------------------------------------------- #


def test_decide_never_approves_from_ticker_and_price_alone():
    """Regression: the repo's acceptance criteria forbid this approval.

    A lone same-ticker candidate with an agreeing price used to be approved
    automatically, so a catalogue that silently omitted a second same-ticker
    asset turned ambiguity into a false unique match and wrote the wrong ID.
    """
    evidence = _evidence("NEW", (_asset(100, "NEW", "new-token", 2.0),), price=2.0)

    decision = decide(evidence, None)

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.method == "deterministic_only"


def test_decide_approves_a_name_match_corroborated_by_price():
    evidence = _evidence(
        "NEW",
        (_asset(100, "NEW", "new-token", 2.0, "New Token"),),
        price=2.0,
        binance_name="New Token",
    )

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.APPROVED
    assert decision.cmc_id == 100
    assert decision.method == "identity_binance_asset_name"


def test_decide_withholds_approval_when_the_catalogue_is_incomplete():
    """Regression: an inconsistent catalogue must not produce an approval."""
    assets = (_asset(100, "NEW", "new-token", 2.0, "New Token"),)
    evidence = _evidence(
        "NEW",
        assets,
        price=2.0,
        binance_name="New Token",
        catalogue=_catalogue(*assets, complete=False),
    )

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.method == "approval_withheld"
    assert decision.cmc_id == 100


def test_decide_does_not_claim_unmapped_from_an_incomplete_catalogue():
    """Regression: absence of evidence is not evidence of absence."""
    evidence = _evidence("AAPLB", (), price=None, catalogue=_catalogue(complete=False))

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.method == "untrustworthy_catalogue"


def test_decide_reports_an_unmapped_ticker_from_a_complete_catalogue():
    evidence = _evidence("AAPLB", (), price=255.0)

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.UNMAPPED
    assert decision.cmc_id is None
    assert decision.method == "no_ticker_candidate"


def test_decide_resolves_an_exact_name_match_without_spending_an_llm_call():
    """One exact name match is sufficient evidence, so the LLM is not consulted."""
    evidence = _evidence(
        "PEPE",
        (
            _asset(22454, "PEPE", "pepe-2", 0.9, "Pepe 2.0"),
            _asset(24478, "PEPE", "pepe", 1.0, "Pepe"),
        ),
        price=1.0,
        binance_name="Pepe",
    )

    decision = decide(evidence, ForbiddenChatClient())

    assert decision.status is MatchStatus.APPROVED
    assert (decision.cmc_id, decision.slug) == (24478, "pepe")


def test_decide_asks_the_llm_when_two_candidates_share_the_same_project_name():
    evidence = _evidence(
        "PEPE",
        (
            _asset(22454, "PEPE", "pepe-bsc", 0.9, "Pepe"),
            _asset(24478, "PEPE", "pepe", 1.0, "Pepe"),
        ),
        price=1.0,
        binance_name="Pepe",
    )
    client = FakeChatClient(
        [{"cmc_id": 24478, "confidence": "high", "reasoning": "the Ethereum Pepe."}]
    )

    decision = decide(evidence, client)

    assert decision.status is MatchStatus.APPROVED
    assert (decision.cmc_id, decision.slug) == (24478, "pepe")
    prompt = json.loads(client.prompts[0])
    assert {candidate["cmc_id"] for candidate in prompt["candidates"]} == {22454, 24478}
    assert prompt["catalogue_trustworthy"] is True


def test_decide_does_not_approve_an_llm_pick_without_identity_evidence():
    """Regression: the LLM's confidence is not a substitute for identity."""
    evidence = _evidence(
        "AAA",
        (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.01)),
        price=1.0,
    )

    decision = decide(
        evidence, FakeChatClient([{"cmc_id": 1, "confidence": "high", "reasoning": "hunch"}])
    )

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.cmc_id == 1
    assert "cannot establish identity" in decision.rationale


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
        (
            _asset(1, "AAA", "alpha", 1.0, "Alpha"),
            _asset(2, "AAA", "beta", 50.0, "Beta"),
        ),
        price=1.0,
        binance_name="Beta",
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
        (
            _asset(1, "AAA", "alpha-one", 1.0, "Alpha"),
            _asset(2, "AAA", "alpha-two", 1.01, "Alpha"),
        ),
        price=1.0,
        binance_name="Alpha",
    )

    decision = decide(
        evidence, FakeChatClient([{"cmc_id": 1, "confidence": "medium", "reasoning": "maybe"}])
    )

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.confidence == "medium"


def test_decide_marks_an_llm_outage_as_uncertain_rather_than_failing():
    evidence = _evidence(
        "AAA",
        (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.01)),
        price=1.0,
    )

    decision = decide(evidence, FakeChatClient([]))

    assert decision.status is MatchStatus.UNCERTAIN
    assert decision.method == "llm_unavailable"


def test_resolve_new_symbols_reuses_a_concurrently_listed_mapping_without_an_llm():
    occurrence = _occurrence("BTC", "btcusdc")
    windows = mapped_windows_by_symbol(
        {
            "binance-spot": [
                {
                    "id": "btcusdt",
                    "symbol": "BTC",
                    "cmc_id": 1,
                    "first_capture": "2020-01-01T00:00:00.000Z",
                }
            ]
        }
    )

    decisions = resolve_new_symbols(
        [NewSymbol("BTC", (occurrence,))],
        _catalogue(_asset(1, "BTC", "bitcoin", 64000.0)),
        windows,
        {},
        {},
        ForbiddenChatClient(),
    )

    assert decisions[0].status is MatchStatus.APPROVED
    assert decisions[0].method == "concurrent_instrument_mapping"
    assert (decisions[0].cmc_id, decisions[0].slug) == (1, "bitcoin")


def test_resolve_new_symbols_stops_spending_llm_calls_at_the_budget():
    catalogue = _catalogue(
        _asset(1, "AAA", "alpha", 1.0),
        _asset(2, "AAA", "beta", 2.0),
        _asset(3, "BBB", "gamma", 1.0),
        _asset(4, "BBB", "delta", 2.0),
    )
    new_symbols = [
        NewSymbol(ticker, (_occurrence(ticker),)) for ticker in ("AAA", "BBB")
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

    assert decisions[1].method == "llm_budget_exhausted"
    assert decisions[1].status is MatchStatus.UNCERTAIN


# --------------------------------------------------------------------------- #
# writing decisions
# --------------------------------------------------------------------------- #


def test_apply_decisions_fills_new_rows_and_never_replaces_an_existing_id():
    rows_by_exchange = {
        "binance-futures": [
            {"id": "newusdt", "symbol": "NEW"},
            {"id": "newusdc", "symbol": "NEW"},
            {"id": "oldusdt", "symbol": "NEW", "cmc_id": 777},
        ]
    }
    new_symbol = NewSymbol(
        "NEW", (_occurrence("NEW", "newusdt"), _occurrence("NEW", "newusdc"))
    )
    decision = Decision(
        evidence=_evidence_for(
            new_symbol,
            (_asset(100, "NEW", "new-token", 1.0, "New Token"),),
            1.0,
            binance_name="New Token",
        ),
        status=MatchStatus.APPROVED,
        cmc_id=100,
        slug="new-token",
        method="identity_binance_asset_name",
        confidence="high",
        rationale="ok",
    )

    updated = apply_decisions(rows_by_exchange, [decision])

    assert updated == {"binance-futures": 2}
    assert [row.get("cmc_id") for row in rows_by_exchange["binance-futures"]] == [
        100,
        100,
        777,
    ]


def test_apply_decisions_ignores_uncertain_and_unmapped_decisions():
    rows_by_exchange = {"binance-futures": [{"id": "newusdt", "symbol": "NEW"}]}
    evidence = _evidence("NEW", (_asset(100, "NEW", "new-token", 1.0),), 1.0)
    decisions = [
        Decision(evidence, MatchStatus.UNCERTAIN, 100, "new-token", "llm_adjudicated", "low", "?"),
        Decision(evidence, MatchStatus.UNMAPPED, None, "", "no_ticker_candidate", "high", "-"),
    ]

    assert apply_decisions(rows_by_exchange, decisions) == {}
    assert "cmc_id" not in rows_by_exchange["binance-futures"][0]


def test_partition_conflicts_holds_back_a_decision_contradicting_an_approval(tmp_path):
    """Regression: conflicts must be caught before any snapshot is mutated."""
    store = MappingStore(tmp_path / "m.json")
    new_symbol = NewSymbol("AAA", (_occurrence("AAA", "aaausdt"),))
    assets = (_asset(10, "AAA", "alpha", 1.0), _asset(20, "AAA", "beta", 1.0))
    evidence = _evidence_for(new_symbol, assets, 1.0)
    record_decisions(
        store,
        [Decision(evidence, MatchStatus.APPROVED, 10, "alpha", "manual", "high", "a")],
        recorded_at=NOW,
    )

    applicable, conflicting = partition_conflicts(
        store,
        [Decision(evidence, MatchStatus.APPROVED, 20, "beta", "llm_adjudicated", "high", "b")],
    )

    assert applicable == []
    assert len(conflicting) == 1


def test_record_decisions_stores_one_reviewable_row_per_instrument(tmp_path):
    store = MappingStore(tmp_path / "cmc_mappings.json")
    evidence = _evidence(
        "AAA",
        (
            _asset(1, "AAA", "alpha", 1.0, "Alpha"),
            _asset(2, "AAA", "beta", 1.01, "Beta"),
        ),
        price=1.0,
        binance_name="Alpha",
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
    mapping = restored.mappings[0]

    assert (mapping.status, mapping.cmc_id, mapping.symbol) == ("uncertain", 1, "AAA")
    assert [c["cmc_id"] for c in mapping.evidence["candidates"]] == [1, 2]
    assert mapping.evidence["candidates"][0]["identity_match"] == "binance_asset_name"
    assert mapping.evidence["catalogue_trustworthy"] is True


def test_record_decisions_keeps_an_unmapped_verdict_with_a_null_id(tmp_path):
    store = MappingStore(tmp_path / "cmc_mappings.json")
    decisions = [
        Decision(
            _evidence("AAPLB", (), None),
            MatchStatus.UNMAPPED,
            None,
            "",
            "no_ticker_candidate",
            "high",
            "none",
        )
    ]

    record_decisions(store, decisions, recorded_at=NOW)
    store.save()

    assert MappingStore.load(tmp_path / "cmc_mappings.json").mappings[0].cmc_id is None


def test_render_report_separates_approved_from_uncertain_matches():
    approved = Decision(
        _evidence(
            "NEW",
            (_asset(100, "NEW", "new-token", 1.0, "New Token"),),
            1.0,
            binance_name="New Token",
        ),
        MatchStatus.APPROVED,
        100,
        "new-token",
        "identity_binance_asset_name",
        "high",
        "name and price agree",
    )
    uncertain = Decision(
        _evidence("AAA", (_asset(1, "AAA", "alpha", 1.0), _asset(2, "AAA", "beta", 1.01)), 1.0),
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
    assert "### Uncertain" in report
    assert "Candidates: 1 (alpha), 2 (beta)" in report


# --------------------------------------------------------------------------- #
# coverage audit
# --------------------------------------------------------------------------- #


def test_coverage_report_counts_missing_ids_inside_and_outside_the_window():
    rows_by_exchange = {
        "binance-futures": [
            {"id": "a", "symbol": "AAA", "first_capture": "2026-08-20T00:00:00.000Z"},
            {"id": "b", "symbol": "BBB", "cmc_id": 2, "first_capture": "2026-08-21T00:00:00.000Z"},
            {"id": "c", "symbol": "CCC", "first_capture": "2019-01-01T00:00:00.000Z"},
        ]
    }

    coverage = coverage_report(rows_by_exchange, NOW, window_days=60)
    stats = coverage["exchanges"]["binance-futures"]

    assert stats["rows_total"] == 3
    assert stats["rows_missing_cmc_id_total"] == 2
    assert stats["rows_in_window"] == 2
    assert stats["rows_in_window_missing_cmc_id"] == 1
    assert stats["tickers_in_window_missing_cmc_id"] == ["AAA"]
    assert coverage["rows_in_window_missing_cmc_id"] == 1


def test_render_coverage_report_lists_the_stale_tickers():
    rows_by_exchange = {
        "binance-futures": [
            {"id": "a", "symbol": "AAA", "first_capture": "2026-08-20T00:00:00.000Z"}
        ]
    }

    report = render_coverage_report(coverage_report(rows_by_exchange, NOW, 60))

    assert "coverage over the last 60 days" in report
    assert "`AAA`" in report


def test_coverage_report_rejects_a_negative_window():
    with pytest.raises(ValueError, match="window_days"):
        coverage_report({}, NOW, window_days=-1)


def test_run_in_coverage_only_mode_touches_no_network_and_writes_no_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    original = json.dumps(
        [{"id": "newusdt", "symbol": "NEW", "first_capture": "2026-09-05T00:00:00.000Z"}],
        indent=2,
    )
    (data_dir / "binance-futures.json").write_text(original)

    # No fetchers are patched: reaching the network at all would fail the test.
    summary = run(
        data_dir=data_dir,
        exchanges=("binance-futures",),
        new_within_days=60,
        coverage_only=True,
        report_path=tmp_path / "coverage.md",
        summary_path=tmp_path / "coverage.json",
        now=NOW,
    )

    assert summary["coverage_only"] is True
    assert summary["window_days"] == 60
    assert summary["rows_in_window_missing_cmc_id"] == 1
    assert summary["has_changes"] is False
    assert (data_dir / "binance-futures.json").read_text() == original
    assert not (data_dir / "cmc_mappings.json").exists()
    assert "`NEW`" in (tmp_path / "coverage.md").read_text()


# --------------------------------------------------------------------------- #
# the full run
# --------------------------------------------------------------------------- #


def _patch_fetchers(
    monkeypatch, catalogue, tickers, public_assets, client, llm_status="ok", llm_error=""
):
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_cmc_catalogue", lambda *_: catalogue
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_futures_prices", lambda: tickers
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_spot_prices", lambda: tickers
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping.fetch_public_assets", lambda: public_assets
    )
    availability = LlmAvailability(
        client, llm_status if client is not None else (llm_status or "not_configured"), llm_error
    )
    monkeypatch.setattr(
        "integrations.cmc_new_symbol_mapping._chat_client", lambda *_: availability
    )


def test_run_writes_approved_ids_a_decision_store_and_a_review_report(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-futures.json").write_text(
        json.dumps(
            [
                {"id": "newusdt", "type": "perpetual", "symbol": "NEW", "first_capture": "2026-09-05T00:00:00.000Z"},
                {"id": "ambigusdt", "type": "perpetual", "symbol": "AMBIG", "first_capture": "2026-09-06T00:00:00.000Z"},
                {"id": "staleusdt", "type": "perpetual", "symbol": "STALE", "first_capture": "2020-01-01T00:00:00.000Z"},
            ],
            indent=2,
        )
    )
    close_time_ms = int(OBSERVED_AT.timestamp() * 1000)
    _patch_fetchers(
        monkeypatch,
        _catalogue(
            _asset(100, "NEW", "new-token", 2.0, "New Token"),
            _asset(200, "AMBIG", "ambig-one", 5.0, "Ambig One"),
            _asset(201, "AMBIG", "ambig-two", 5.0, "Ambig Two"),
        ),
        [
            {"symbol": "NEWUSDT", "lastPrice": "2.0", "closeTime": close_time_ms},
            {"symbol": "AMBIGUSDT", "lastPrice": "5.0", "closeTime": close_time_ms},
        ],
        [{"assetCode": "NEW", "assetName": "New Token"}],
        FakeChatClient([{"cmc_id": 201, "confidence": "low", "reasoning": "unclear"}]),
    )

    summary = run(
        data_dir=data_dir,
        exchanges=("binance-futures",),
        report_path=tmp_path / "report.md",
        summary_path=tmp_path / "summary.json",
        now=NOW,
    )

    rows = json.loads((data_dir / "binance-futures.json").read_text())
    assert {row["id"]: row.get("cmc_id") for row in rows} == {
        "newusdt": 100,
        "ambigusdt": None,
        "staleusdt": None,
    }
    assert (summary["approved"], summary["uncertain"]) == (1, 1)
    assert summary["rows_updated"] == {"binance-futures": 1}
    assert summary["mapping_conflicts"] == 0
    store = MappingStore.load(data_dir / "cmc_mappings.json")
    assert {mapping.symbol for mapping in store.mappings} == {"NEW", "AMBIG"}
    assert "### Approved" in (tmp_path / "report.md").read_text()
    assert json.loads((tmp_path / "summary.json").read_text())["approved"] == 1


def test_run_leaves_the_snapshot_untouched_when_a_decision_conflicts(tmp_path, monkeypatch):
    """Regression: the full run path must not write a snapshot the ledger refuses."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    snapshot = json.dumps(
        [{"id": "aaausdt", "type": "perpetual", "symbol": "AAA", "first_capture": "2026-09-05T00:00:00.000Z"}],
        indent=2,
    )
    (data_dir / "binance-futures.json").write_text(snapshot)
    store = MappingStore(data_dir / "cmc_mappings.json")
    evidence = _evidence_for(
        NewSymbol(
            "AAA",
            (
                _occurrence(
                    "AAA", "aaausdt", first_capture=datetime(2026, 9, 5, tzinfo=UTC)
                ),
            ),
        ),
        (_asset(10, "AAA", "alpha", 1.0, "Alpha"),),
        1.0,
    )
    record_decisions(
        store,
        [Decision(evidence, MatchStatus.APPROVED, 10, "alpha", "manual", "high", "a")],
        recorded_at=NOW,
    )
    store.save()
    close_time_ms = int(OBSERVED_AT.timestamp() * 1000)
    _patch_fetchers(
        monkeypatch,
        _catalogue(_asset(20, "AAA", "beta", 1.0, "Alpha")),
        [{"symbol": "AAAUSDT", "lastPrice": "1.0", "closeTime": close_time_ms}],
        [{"assetCode": "AAA", "assetName": "Alpha"}],
        ForbiddenChatClient(),
    )

    summary = run(
        data_dir=data_dir,
        exchanges=("binance-futures",),
        recheck_decided=True,
        now=NOW,
    )

    assert summary["mapping_conflicts"] == 1
    assert summary["rows_updated"] == {}
    # Neither file moved: the snapshot still has no ID and the ledger keeps 10.
    assert json.loads((data_dir / "binance-futures.json").read_text())[0].get("cmc_id") is None
    assert MappingStore.load(data_dir / "cmc_mappings.json").mappings[0].cmc_id == 10


def test_run_reports_no_work_when_no_new_symbol_needs_an_id(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-futures.json").write_text(
        json.dumps([{"id": "btcusdt", "symbol": "BTC", "cmc_id": 1}], indent=2)
    )
    _patch_fetchers(
        monkeypatch, _catalogue(_asset(1, "BTC", "bitcoin", 64000.0)), [], [], None
    )

    summary = run(
        data_dir=data_dir,
        exchanges=("binance-futures",),
        report_path=tmp_path / "report.md",
        now=NOW,
    )

    assert summary["has_changes"] is False
    assert summary["new_symbols"] == 0
    assert not (data_dir / "cmc_mappings.json").exists()


def test_run_does_not_re_propose_an_instance_awaiting_review(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-futures.json").write_text(
        json.dumps(
            [{"id": "newusdt", "symbol": "NEW", "first_capture": "2026-09-05T00:00:00.000Z"}],
            indent=2,
        )
    )
    pending = MappingStore(tmp_path / "pending.json")
    record_decisions(
        pending,
        [
            Decision(
                _evidence_for(
                    NewSymbol(
                        "NEW",
                        (
                            _occurrence(
                                "NEW",
                                "newusdt",
                                first_capture=datetime(2026, 9, 5, tzinfo=UTC),
                            ),
                        ),
                    ),
                    (_asset(100, "NEW", "new-token", 2.0),),
                    2.0,
                ),
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
    _patch_fetchers(
        monkeypatch, _catalogue(_asset(100, "NEW", "new-token", 2.0)), [], [], None
    )

    summary = run(
        data_dir=data_dir,
        exchanges=("binance-futures",),
        pending_mapping_paths=(tmp_path / "pending.json",),
        now=NOW,
    )

    assert summary["new_symbols"] == 0
    assert summary["has_changes"] is False


def test_run_leaves_snapshots_untouched_on_a_dry_run(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    original = json.dumps(
        [{"id": "newusdt", "symbol": "NEW", "first_capture": "2026-09-05T00:00:00.000Z"}],
        indent=2,
    )
    (data_dir / "binance-futures.json").write_text(original)
    close_time_ms = int(OBSERVED_AT.timestamp() * 1000)
    _patch_fetchers(
        monkeypatch,
        _catalogue(_asset(100, "NEW", "new-token", 2.0, "New Token")),
        [{"symbol": "NEWUSDT", "lastPrice": "2.0", "closeTime": close_time_ms}],
        [{"assetCode": "NEW", "assetName": "New Token"}],
        None,
    )

    summary = run(
        data_dir=data_dir, exchanges=("binance-futures",), dry_run=True, now=NOW
    )

    assert summary["approved"] == 1
    assert (data_dir / "binance-futures.json").read_text() == original
    assert not (data_dir / "cmc_mappings.json").exists()
