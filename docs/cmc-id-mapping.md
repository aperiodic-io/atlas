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
on `workflow_dispatch`. It must run on `ubicloud-standard-2`, as `daily-update`
does: **Binance answers HTTP 451 to GitHub-hosted runner IPs**, so any job that
calls Binance from `ubuntu-latest` fails. CoinMarketCap is not geo-blocked, which
makes the failure look puzzling — the catalogue fetch succeeds and the run dies at
the first price call. It invokes:

The dispatch form takes one **mode** and one **window**, so there is no pair of
similar-looking inputs to choose between:

| mode | what it does | network | writes | opens a PR |
| --- | --- | --- | --- | --- |
| `resolve` (default) | map new symbols, write confident matches | CMC + Binance + LLM | yes | yes |
| `preview` | same work, reported only | CMC + Binance + LLM | no | no |
| `coverage` | count rows still missing an ID | none | no | no |
| `check-llm` | one request to the LLM endpoint, then stop | LLM only | no | no |

`days` is that single window: in `resolve` and `preview` it bounds how recently a
symbol was first listed; in `coverage` it bounds which rows are counted. It
defaults to 60, which costs almost nothing to widen because an instrument instance
already decided is skipped — so a wider window only re-examines genuinely
undecided rows, and a few failed runs cannot open a gap.

`max-llm-symbols` defaults to 100 so a backlog clears in one run; in steady state
a handful of symbols a day never approaches it.

Equivalent on the command line:

```bash
python integrations/cmc_new_symbol_mapping.py \
  --new-within-days 60 --max-llm-symbols 100 \
  --report-path <report.md> --summary-path <summary.json>
```

Scope is `binance-futures` by default (`--exchanges` widens it). A row enters the
run when all of the following hold:

- the row has no `cmc_id`;
- its `underlying` is not one Atlas maps no CMC ID for (see *Crypto only* below);
- its `first_capture` is within `--new-within-days`; and
- no earlier run already decided that *instrument instance* (see *Not
  re-deciding* below).

### Crypto only

Binance lists a great many tokenized equities, ETFs and index products, and they
dominate new listings: in one 60-day window, **59 of the 67** unmapped
`binance-futures` rows had `underlying: equity` — Datadog, Moderna, Shopify,
Zscaler, leveraged Tesla and NVIDIA ETFs, Korean index products. Left in, they
drown the review queue.

`--skip-underlyings` (default `commodity,equity,index,pre_market`) leaves them
out. CoinMarketCap does carry entries for some of them, so this is a **scope**
choice, not a correctness one: Atlas wants a CMC ID for a crypto asset, and a
tokenized equity's ID would name the tokenization wrapper rather than the asset.

Two deliberate inclusions:

- **No `underlying` at all** is kept. Those are legacy rows that predate the
  field — BTC, ADA, BNB, AVAX — and are crypto.
- **`unknown`** is kept, because silently skipping a genuine new listing is worse
  than a reviewer seeing a couple of rows that turn out not to be crypto.

`--symbols` overrides the filter, so a specific ticker can always be resolved by
hand. Pass `--skip-underlyings ""` to resolve everything. The coverage audit
applies the same scope and reports what it left out, so its headline count is
work somebody actually wants done.

Contract tickers are folded onto their underlying asset before grouping, so
`1000CHEEMS` and `CHEEMS` are resolved once, together.

### Decision ladder

Each ticker is resolved by the cheapest sufficient evidence, in order:

**Nothing is approved from a ticker and a price alone.** That is an acceptance
criterion above, and for good reason: the keyless catalogue can omit a row, and a
missing second same-ticker asset would turn ambiguity into a false unique match
and write the wrong ID. Approval requires *identity* evidence — an exact match
(after case and punctuation normalization) between the Binance `assetName` and
the candidate's CMC name or slug. The match must be exact: a substring rule would
tie "Pepe" to "Pepe 2.0", the very confusion identity evidence exists to settle.

| method | status | evidence |
| --- | --- | --- |
| `concurrent_instrument_mapping` | `approved` | every instrument was listed *at the same time as* an existing same-ticker instrument already mapped to one CMC ID |
| `identity_binance_asset_name`, `identity_cmc_slug` | `approved` | exactly one candidate's name or slug matches the Binance asset name, and the price does not contradict it |
| `llm_adjudicated_identity_*` | `approved` | an LLM picked between candidates that *all* carry name evidence, at high confidence |
| `no_ticker_candidate` | `unmapped` | a complete catalogue lists no asset with this ticker |
| `llm_rejected_all_candidates` | `unmapped` | the LLM judged no candidate to be the same underlying asset |
| `untrustworthy_catalogue` | `uncertain` | the catalogue failed a trust check, so no absence is asserted |
| `approval_withheld` | `uncertain` | identity evidence was found but the catalogue or the price feed could not corroborate it |
| `llm_adjudicated`, `llm_chose_unlisted_id`, `llm_unavailable`, `deterministic_only`, `llm_budget_exhausted` | `uncertain` | no identity evidence, or adjudication could not be trusted or could not run |

Two consequences worth stating plainly:

- **An untrustworthy catalogue blocks every approval for that run** and also stops
  `unmapped` being claimed, since absence of evidence is not evidence of absence.
  The run still completes and still reports. See *Catalogue trust* below for what
  counts as untrustworthy, which is deliberately not "not perfectly complete".
- **A ticker alone is never identity across time.** Reusing an ID already in the
  snapshots requires the existing instrument's listing window to *overlap* the new
  one, so a delisted ticker that a different project later reuses cannot inherit
  the old ID.
- **No price, no approval.** If the Binance price fetch fails for the whole run,
  the run still completes and still reports, but nothing is approved: the price
  check is a weak signal, and silently dropping it would be worse than holding
  the work for a human.

### Catalogue trust: magnitude, not category

The keyless listing is a live, continuously updated, paginated, scraped feed, and
**every categorical signal of "something is wrong" turned out to be its normal
behaviour**. Each was tried as a veto and each blocked a production run on its own:

| signal vetoed | why it fires normally | run it blocked |
| --- | --- | --- |
| reported total ≠ unique IDs | pages overlap; dedup removes repeats | every run, by construction |
| any unparseable row | assets with no USD quote | 8,183/8,176 with 1 bad row |
| any duplicate ID | overlapping pages again | — |
| any change in the reported total | CMC lists assets mid-fetch | 8,183/8,182, total moved by 1 |

So `catalogue_trust()` judges only **magnitude**. Everything that could hide a
candidate — deduplicated, unparseable, lost to pagination, or added after the
first page — is counted into one figure and compared against
`--max-catalogue-gap-ratio` (default 0.5%). Observed production figures sit
between 0.02% and 0.09%. Each cause is still named in the evidence, so a reviewer
sees *why* rows were unreadable without it being a veto.

A catalogue that **grew** while being read can legitimately yield more unique IDs
than the first page claimed, so the gap floors at zero rather than being treated
as incoherence.

Two things still fail closed:

- **no reported total at all** — nothing to measure against, and no tolerance can
  rescue that;
- **unreadable rows above the tolerance** — a real schema break craters
  `unique_ids` and blows the budget, which is what the gate is for.


The reason a small gap is safe *here* is that approval rests on **presence** — an
exact Binance-to-CMC name match — not on a ticker being unique. The original rule
argued from absence ("only one same-ticker candidate exists"), which a missing row
destroys; that rule is gone. For a missing row to mislead the current rule it
would have to share both the ticker *and* the exact project name, and two
candidates sharing both go to the LLM as ambiguous anyway.

The LLM is constrained, not trusted:

- it only ever sees candidates fetched from CMC for that ticker, and an ID that
  is not among them is discarded rather than written;
- it can never substitute for identity evidence: a confident pick with no name
  match stays `uncertain`;
- a high-confidence answer is downgraded to `uncertain` when the chosen
  candidate's price disagrees with the exchange observation by more than
  `--max-relative-difference`; and
- it is asked at most `--max-llm-symbols` times per run, and never for tickers
  the deterministic rules already settled.

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
- **A pull request** on a fresh `automation/cmc-new-symbols-<date>-<run>` branch,
  labelled `cmc-mapping`, carrying both the approved and the uncertain work, with
  the review table as its body.
- **A Slack message** naming the counts and linking the PR.

### Reviewing a run

Approved rows need a spot check; uncertain rows are the actual work. For each
one, confirm identity from a contract address, the project name or a known
rebrand — not from the ticker or the price — then set `cmc_id` on the branch. An
`unmapped` verdict is usually correct for tokenized equities, fiat pairs,
leveraged tokens and index products, which have no CMC crypto asset.

### One branch per run

Each run branches from the default branch and opens its own pull request, so a
review that sits for a week never blocks the next run and nothing a reviewer
pushes is ever overwritten.

That leaves one thing to handle: a ticker proposed in a PR that has not merged
yet is absent from the default branch's ledger, so the next run would propose it
again. Before resolving anything, the workflow lists the open `cmc-mapping` pull
requests and reads `atlas/data/cmc_mappings.json` from each of their branches,
passing them as `--pending-mapping-path`. A ticker already awaiting review is
skipped regardless of its verdict or age — unlike a *merged* `unmapped` verdict,
which expires. If the PR listing fails the run still proceeds, warning that it
may duplicate a pending proposal.

Concurrent pull requests merge cleanly where they add `cmc_id` to different
snapshot rows, even adjacent ones, and a PR still merges cleanly after the daily
update has rewritten rows around it. The one file that can conflict is
`atlas/data/cmc_mappings.json`, when two runs insert entries that sort next to
each other. The resolution is always to keep both sides' entries: dropping one
loses the record that stops its ticker being proposed again.

### Not re-deciding

`instances_to_skip()` makes runs idempotent, keyed by instrument instance
(`exchange`, `original_id`, `first_capture`) rather than by ticker. Keying by
ticker would mean a decision about a 2025 `ABC` silenced a 2026 relisting of
`ABC` forever, so a reused or relisted symbol could never get its own ID.
`unmapped` is the one verdict that expires — CoinMarketCap may list the
asset later — so it becomes eligible again after
`--recheck-unmapped-after-days` (default 90). `--recheck-decided` lifts the skip for a whole run and
`--symbols BTC,ETH` narrows a manual run to those tickers alone, ignoring both
age and history. `--dry-run` reports without writing.

A decision that would overwrite an earlier *approved* mapping with a different
CMC ID is detected by `partition_conflicts()` **before any snapshot is touched**,
counted as `mapping_conflicts` in the run summary and named on stderr: an approval
that needs revising is a human's call. Detecting it first is what keeps a refused
ledger write from leaving the snapshot and the ledger disagreeing.

### Configuration

LLM access uses any OpenAI-compatible chat-completions endpoint
([`integrations/llm.py`](../integrations/llm.py)), reached with bearer auth at
`<base-url>/chat/completions`.

| variable | where | purpose |
| --- | --- | --- |
| `ATLAS_LLM_API_KEY` | secret | provider key; falls back to `GITHUB_TOKEN` |
| `ATLAS_LLM_BASE_URL` | variable | endpoint root, no `/chat/completions` suffix |
| `ATLAS_LLM_MODEL` | variable | model id as that provider spells it |
| `ATLAS_LLM_API_VERSION` | variable | sent as `X-GitHub-Api-Version`; only GitHub Models wants it |
| `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID` | secrets | post via `chat.postMessage` |
| `SLACK_WEBHOOK_URL` | secret | fallback transport, shared with `daily-update` |

The built-in default targets GitHub Models, free inside Actions via the
workflow's own `GITHUB_TOKEN` and the `models: read` permission. **That default
is not currently working in this repository**: every request to
`https://models.github.ai/inference/chat/completions` comes back `410 Gone`, with
and without a version header. So configure a provider explicitly.

#### opencode Zen

Zen is OpenAI-compatible at `https://opencode.ai/zen/v1`, so two variables and a
key are the whole configuration:

| name | where | value |
| --- | --- | --- |
| `ATLAS_LLM_BASE_URL` | repository **variable** | `https://opencode.ai/zen/v1` |
| `ATLAS_LLM_MODEL` | repository **variable** | a bare model id, e.g. `mimo-v2-pro-free` |
| `ATLAS_LLM_API_KEY` | repository **secret** | a key from `opencode.ai/auth`, or the literal `public` |

Two details decide whether this works, and both are easy to get wrong:

- **Model ids carry no provider prefix.** Zen wants the id as the provider spells
  it — `big-pickle`, `nemotron-3-super-free` — *not* `opencode/big-pickle`. The
  `provider/model` form is how opencode's own client config namespaces providers,
  not what the HTTP API accepts.
- **The free tier rotates.** Models are added and retired without notice, so an id
  that works today can be rejected next month, and a daily run meets that as an
  unexplained failure. Take the current list from
  [opencode.ai/zen](https://opencode.ai/zen) and expect to revisit it. Free ids
  conventionally end in `-free`, and `ATLAS_LLM_API_KEY=public` reaches the free
  models without an account at all.

Three diagnostics exist because each of these cost a run:

- A permanent rejection quotes the endpoint's own response body, not just the
  status, because a bare `410` cannot distinguish a retired path from a retired
  model from a token the endpoint will not serve.
- `--check-llm` prints the URL, model, version header and whether a key is set
  *before* it dials, so a wrong variable is visible without reading code.
- On a failure it then asks the endpoint which models it serves and prints them,
  which is the whole fix once the free tier has rotated. That listing is optional
  in practice — Zen was only asked to add a `/models` route — so when it is absent
  the check stays silent rather than reporting a second, less useful error.

### One run is the whole loop

`mode: resolve` is the only mode a normal day needs: it resolves, writes the
confident matches, opens a pull request, and pings Slack. Review the PR, merge it
if it is good. `check-llm` exists for debugging a provider, not as a step in that
loop.

A run whose LLM was **configured but unusable** is the exception. It approves
nothing the LLM would have settled, so a pull request would be a page of outage
notices — and an open PR is worse than none, because the pending-ledger skip
treats it as work already awaiting review and every later run then finds nothing
to do. Two such PRs did exactly that. So the run reports
`worth_reviewing: false`, opens no PR unless something was approved regardless,
and **fails the job** with the endpoint error. A deliberate absence of an LLM is
not a misconfiguration and still opens its PR.

Validate a provider in seconds, without spending a run on it:

```bash
python integrations/cmc_new_symbol_mapping.py --check-llm
```

A misconfigured endpoint is now a single loud message, not one failure per
symbol: the run preflights the endpoint once, and a permanent rejection (any 4xx
that is not 408 or 429) is never retried. The first real run predated this and
answered 410 Gone four times for each of 38 tickers, burning nine minutes and
reporting one dead URL as 38 unrelated per-symbol outages.

Any other OpenAI-compatible provider works the same way — OpenRouter is
`https://openrouter.ai/api/v1` with a `:free` model. With no key at all the run still
completes: adjudication is skipped and ambiguous tickers are reported as
`uncertain` with method `deterministic_only`. Slack is skipped without failing
the job until its secrets exist.

### Coverage audit

To check whether coverage has gone stale, dispatch with **mode** `coverage`. It
runs:

```bash
python integrations/cmc_new_symbol_mapping.py --coverage-only --new-within-days 60
```

This mode is entirely offline — no CoinMarketCap, no Binance, no LLM — and writes
nothing. It reports, per exchange, total rows, rows missing a `cmc_id` all-time,
rows first captured inside the window, and which of those tickers still have no
ID. It opens no pull request; the table goes to the job summary and the count to
Slack.

### Known limits

- Candidate discovery still reads the keyless CMC website listing, which is
  reliably self-inconsistent; see *Catalogue trust* above. The authenticated
  `/v1/cryptocurrency/map` client in *Phase 2* removes the need for a tolerance at
  all.
- Binance must be reachable from the runner. Use `ubicloud-standard-2`;
  GitHub-hosted runners get HTTP 451.
- Approval needs a Binance `assetName` to compare against, which comes from an
  undocumented website endpoint. An asset missing from it cannot be auto-approved.
- Resolution scope is `binance-futures`; other venues have no `cmc_id` yet.
- Price evidence compares CMC USD aggregates with Binance USDT last prices, so
  it remains a sanity check, never identity proof.
