# CoinMarketCap ID mapping: audit and implementation plan

**Related PR:** [#18 — Add CMC ID matching prototype](https://github.com/aperiodic-io/atlas/pull/18)
**Audit date:** 2026-07-15
**Recommendation:** Keep the PR in draft until the high-severity findings below are addressed.

## Purpose and principles

Atlas needs one stable CoinMarketCap (CMC) ID per underlying asset. The same ID
may legitimately be shared by spot, perpetual, and dated contracts for that
asset; a CMC ID identifies an asset, not an exchange-specific contract.

Ticker and price matches are candidate evidence, never identity proof. Wrapped,
bridged, liquid-staking, pegged, multiplier, and legacy assets can share a
ticker or closely track another asset's price. An approved mapping must retain
identity evidence, price evidence, provenance, and an immutable CMC ID.

## Current prototype

The keyless exploration tool can be run with:

```bash
python -m integrations.cmc_id_probe --symbols BTC,ETH,SOL
```

It reads CMC's undocumented website listing endpoint and timestamped Binance
spot prices. The supported CMC Pro quotes endpoint returns `401 API key
missing` without a key. The probe is exploratory only: its website endpoint
can paginate inconsistently and its output must not become a production
mapping.

The probe uses exact ticker matches, then removes known contract multipliers
for lookup (for example, `1000CHEEMSUSDT` becomes `CHEEMS`). It reports
`no_ticker_candidate`, `price_mismatch`, `multiple_price_matches`,
`price_compatible`, or `fetch_failed`. `price_compatible` means only that one
same-ticker candidate met the configured price and timestamp thresholds; it
does not establish identity.

To attach non-authoritative evidence to bundled Binance rows only, run:

```bash
python -m integrations.cmc_probe_metadata
```

The updater writes a `cmc_probe` object to
`atlas/data/cmc_probe/<exchange>.json`. It supports `binance-spot`,
`binance-futures`, and `binance-futures-cm`; `--dry-run` reports status counts
without changing snapshots.

## Audit findings

### 1. High — price cannot verify identity

[`select_verified_candidate()`](../atlas/cmc_id_prototype.py#L40) approves an
asset when it is the only same-ticker candidate inside the price threshold and
the CLI labels it `verified`. That is too strong: a wrapped or bridged asset can
track the native asset, while the native asset may be absent from an incomplete
catalogue. Price is a sanity check only after identity is established by a
contract address, trusted alias, project name/slug, or manual approval.

### 2. High — prototype coverage is Binance USD-M only

[`fetch_binance_usdm_price()`](../atlas/cmc_id_prototype.py#L84) does not read
Atlas snapshots and checks only Binance USD-M `<SYMBOL>USDT` contracts. It does
not cover spot, coin-margined or dated contracts, USDC/non-dollar quotes,
non-Binance exchanges, multiplier contracts, or synthetic/commodity/equity/index
underlyings that have no CMC asset.

### 3. High — keyless CMC pagination is incomplete and non-unique

[`fetch_cmc_assets()`](../atlas/cmc_id_prototype.py#L67) increments `start` by
5,000 and treats returned page length as completion. During the audit, CMC
reported 8,143 assets while two requests returned 8,145 rows but only 8,141
unique IDs. This can create false ambiguity or hide valid candidates; the
undocumented endpoint is not a complete production catalogue.

### 4. High — mapping persistence must model instrument instances

Atlas stores exchange instrument rows, so multiple contracts must be allowed to
share one CMC ID. A mapping needs to be attached to an instance, initially
identified by `(exchange, original_id, first_capture)`, so a relisting or symbol
reuse cannot contaminate historical rows.

### 5. Medium — compared prices are not aligned

The prototype compares CMC aggregate USD spot prices with Binance USDT futures
last prices after discarding source timestamps. Basis, USDT/USD deviation,
liquidity, last-trade staleness, and contract multipliers can all invalidate a
fixed threshold. Price checks need unit normalization, explicit quote conversion,
bounded timestamp skew, and preferably multiple aligned observations.

### 6. Medium — catalogue loading lacks resilience

`_parse_cmc_asset()` assumes `quotes[0].price`; malformed rows, empty quotes,
API envelopes, or schema changes can abort the run. The client needs envelope
validation, timeouts, retry/backoff, rate-limit handling, and partial-result
reporting.

### 7. Medium — reuse existing integration conventions

[`integrations/coingecko.py`](../integrations/coingecko.py) and
[`integrations/coingecko_metadata.py`](../integrations/coingecko_metadata.py)
already provide client, metadata, retry, price-check, and CLI patterns. The CMC
implementation should follow them and keep unsupported scraping out of the
published `atlas` package.

### 8. Medium — retain Atlas lifecycle fields

[`atlas/update.py`](../atlas/update.py#L175) and
[`atlas/database.py`](../atlas/database.py#L49) already use `first_capture` and
`end_date` for availability. Avoid parallel `listed_at` and `delisted_at`
fields. Add `last_seen_at` only where necessary.

### 9. CI is red for unrelated live-data drift

The audited Linux job had 131 passed, 4 skipped, and 3 failures in strict live
Tardis-to-snapshot comparisons for Binance spot, OKX futures, and OKX
perpetuals. The prototype did not change those tests. Required PR CI should use
fixtures; strict live equivalence belongs in scheduled monitoring.

## Target implementation

### Production CMC client

Use a `CoinMarketCapClient` configured with `CMC_PRO_API_KEY`, but require the
key only when enrichment runs. Atlas imports and normal snapshot loading remain
keyless. Use:

- `/v1/cryptocurrency/map` for identity discovery;
- `/v2/cryptocurrency/info` for metadata, platforms, and addresses; and
- `/v3/cryptocurrency/quotes/latest?id=...&convert=USD` for batched ID-based
  quote checks.

Cache the map response, batch within plan limits, rate-limit and retry
transient failures, and expose verified/ambiguous/unmapped/stale/rejected
counts. Persist and query by CMC ID, never by ticker after approval.

### Identity and evidence model

Candidate generation starts with an Atlas `symbol`, but candidates are narrowed
in this order:

1. Exact platform/network and token contract address.
2. Versioned manual aliases and approved overrides.
3. Project name, CMC slug, and known rebrand history.
4. Unique ticker as candidate generation only.
5. Price as a final sanity check only.

Store the mapping by instrument instance, permit many instances to share an
ID, and record method, evidence, timestamps, approval provenance, and
verification time. Do not replace an approved CMC ID automatically; approved
overrides are versioned fixtures.

### Price verification

Normalize multiplier contracts to one base unit. Prefer exchange spot prices;
otherwise use a documented mark price or aligned historical candle. Convert the
exchange quote currency to USD explicitly, require a bounded timestamp window,
and compare multiple observations when possible. Store both prices, timestamps,
tolerance, and relative difference.

### Delisted, inactive, and relisted assets

Keep every historical Atlas contract row. `first_capture` and `end_date` remain
the availability bounds; Tardis `availableTo` can supply `end_date`. Absence
from a failed or partial fetch must never mean delisting.

Mark an unseen instrument `suspected_delisted` only after successful snapshots;
confirm after a configurable threshold or explicit exchange status. Keep CMC
mappings immutable for delisted assets, query their saved IDs historically, and
mark inactive CMC assets `cmc_inactive` rather than recycling an ID. A relisting
or reused exchange symbol creates a new instrument instance.

## Remediation phases

### Phase 1 — position and harden the prototype

1. Keep PR #18 in draft and move the prototype under `integrations/`.
2. Rename `verified` to `price_compatible`; state Binance USD-M-only coverage.
3. Do not persist mappings from the keyless website probe.
4. Deduplicate CMC results and report returned rows, unique IDs, duplicates,
   reported totals, and discrepancies.
5. Add fixture tests for overlap, incomplete totals, missing quotes, malformed
   envelopes, HTTP errors, and partial symbol failures.

### Phase 2 — authenticated integration and identity-first matching

1. Add `integrations/coinmarketcap.py` following CoinGecko conventions.
2. Use authenticated map/info/quote endpoints for active and inactive IDs.
3. Implement batching, caching, validation, retry/backoff, and rate limits.
4. Persist approved mappings per instrument instance with structured evidence.

### Phase 3 — comparable prices and lifecycle handling

1. Add per-exchange price adapters and multiplier/quote normalization tests.
2. Use aligned historical prices for delisted assets near their last active date.
3. Preserve `first_capture`/`end_date`; add lifecycle provenance only where
   needed.
4. Separate strict live equivalence into scheduled monitoring and keep required
   CI fixture based.

## Acceptance criteria

- No mapping is approved solely because ticker and price match.
- The keyless probe reports catalogue incompleteness and never persists IDs.
- Production lookups use authenticated CMC endpoints and quote by CMC ID.
- Every active Atlas instrument is classified as approved, ambiguous, unmapped,
  rejected, or awaiting price.
- Many exchange instruments may safely share a CMC ID.
- Price evidence is normalized and timestamp-aligned.
- Delisted and relisted instruments remain historically distinguishable.
- Required CI is deterministic and independent of live metadata drift.

## Automated new-symbol mapping (implemented)

A scheduled workflow keeps the Binance snapshots from accumulating rows without a
`cmc_id`. It is deliberately scoped to **new** assets: backfilling the historical
tail is a separate, manual exercise.

### What runs

[`.github/workflows/cmc_new_symbol_mapping.yaml`](../.github/workflows/cmc_new_symbol_mapping.yaml)
runs daily at 03:30 UTC — after `daily-update` has refreshed the snapshots — and
on `workflow_dispatch`. It invokes:

```bash
python integrations/cmc_new_symbol_mapping.py \
  --new-within-days 30 --max-llm-symbols 40 \
  --report-path <report.md> --summary-path <summary.json>
```

A symbol enters the run when all of the following hold in `binance-spot`,
`binance-futures` or `binance-futures-cm`:

- the row has no `cmc_id`;
- its `first_capture` is within `--new-within-days`; and
- no earlier run already decided its ticker (see *Not re-deciding* below).

Contract tickers are folded onto their underlying asset before grouping, so
`1000CHEEMS` and `CHEEMS` are resolved once, together.

### Decision ladder

Each ticker is resolved by the cheapest sufficient evidence, in order:

| method | status | evidence |
| --- | --- | --- |
| `existing_binance_mapping` | `approved` | another Binance row already maps this exact ticker to one CMC ID |
| `unique_ticker_price_compatible` | `approved` | exactly one CMC asset uses the ticker and its USD price matches the Binance observation inside the threshold |
| `llm_adjudicated` | `approved` / `uncertain` | an LLM chose among the fetched candidates; `approved` only at high confidence |
| `no_ticker_candidate` | `unmapped` | CoinMarketCap lists no asset with this ticker |
| `llm_rejected_all_candidates` | `unmapped` | the LLM judged no candidate to be the same underlying asset |
| `llm_chose_unlisted_id`, `llm_unavailable`, `deterministic_only`, `llm_budget_exhausted` | `uncertain` | adjudication could not be trusted or could not run |

The LLM is constrained, not trusted:

- it only ever sees candidates fetched from CMC for that ticker, and an ID that
  is not among them is discarded rather than written;
- a high-confidence answer is downgraded to `uncertain` when the chosen
  candidate's price disagrees with the exchange observation by more than
  `--max-relative-difference`; and
- it is asked at most `--max-llm-symbols` times per run, and never for tickers
  the two deterministic rules already settled.

Only `approved` decisions write `cmc_id`, and only onto the new rows that
triggered the run — an existing `cmc_id` is never replaced. `uncertain` and
`unmapped` assets leave the snapshots untouched.

### Outputs

- **Snapshots** gain `cmc_id` for approved assets. `cmc_id` is already listed in
  `LOCALLY_OWNED_FIELDS`, so `atlas/update.py` preserves it.
- **`atlas/data/cmc_mappings.json`** records every decision per instrument
  instance through [`integrations/cmc_mappings.py`](../integrations/cmc_mappings.py):
  status, method, confidence, rationale, the full candidate list, the price
  observation and the Binance asset name. `cmc_id` is `null` for an `unmapped`
  verdict.
- **A pull request** on the `automation/cmc-new-symbol-mapping` branch, labelled
  `cmc-mapping`, carrying both the approved and the uncertain work, with the
  review table as its body.
- **A Slack message** naming the counts and linking the PR.

### Reviewing a run

Approved rows need a spot check; uncertain rows are the actual work. For each
one, confirm identity from a contract address, the project name or a known
rebrand — not from the ticker or the price — then set `cmc_id` on the branch. An
`unmapped` verdict is usually correct for tokenized equities, fiat pairs,
leveraged tokens and index products, which have no CMC crypto asset.

The workflow regenerates the automation branch on each run, so it checks the
branch head's author first and leaves the branch alone once a reviewer has
committed to it.

### Not re-deciding

`symbols_to_skip()` makes runs idempotent: a ticker with any recorded decision is
skipped. `unmapped` is the one verdict that expires — CoinMarketCap may list the
asset later — so it becomes eligible again after
`--recheck-unmapped-after-days` (default 90). `--recheck-decided` lifts the skip for a whole run and
`--symbols BTC,ETH` narrows a manual run to those tickers alone, ignoring both
age and history. `--dry-run` reports without writing.

A decision that would overwrite an earlier *approved* mapping with a different
CMC ID is refused by the store, counted as `mapping_conflicts` in the run summary
and named on stderr: an approval that needs revising is a human's call.

### Configuration

LLM access uses any OpenAI-compatible chat-completions endpoint
([`integrations/llm.py`](../integrations/llm.py)). With nothing configured it
uses GitHub Models, which is free inside Actions via the workflow's own
`GITHUB_TOKEN` and the `models: read` permission.

| variable | purpose |
| --- | --- |
| `ATLAS_LLM_API_KEY` | provider key; falls back to `GITHUB_TOKEN` |
| `ATLAS_LLM_BASE_URL` | default `https://models.github.ai/inference` |
| `ATLAS_LLM_MODEL` | default `openai/gpt-4o-mini` |
| `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID` | post via `chat.postMessage` |
| `SLACK_WEBHOOK_URL` | fallback transport, shared with `daily-update` |

To use a free hosted model instead, set `ATLAS_LLM_BASE_URL` to
`https://openrouter.ai/api/v1`, `ATLAS_LLM_MODEL` to a `:free` model and
`ATLAS_LLM_API_KEY` to the provider key. With no key at all the run still
completes: adjudication is skipped and ambiguous tickers are reported as
`uncertain` with method `deterministic_only`. Slack is skipped without failing
the job until its secrets exist.

### Known limits

- Candidate discovery still reads the keyless CMC website listing, so the
  catalogue-completeness caveats in this document's audit findings still apply.
  The authenticated client in *Phase 2* above remains the target.
- Coverage is the three Binance snapshots; other venues have no `cmc_id` yet.
- Price evidence compares CMC USD aggregates with Binance USDT last prices, so
  it remains a sanity check, never identity proof.
