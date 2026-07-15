# CoinMarketCap ID mapping PR audit

**PR:** [#18 — Add CMC ID matching prototype](https://github.com/aperiodic-io/atlas/pull/18)  
**Audit date:** 2026-07-15  
**Recommendation:** Keep the PR in draft and do not merge it in its current form.

## Executive summary

The prototype is useful because it demonstrates that ticker matching is
ambiguous and that prices can help reject implausible candidates. It does not
yet establish asset identity, cover all Atlas exchanges, or provide a reliable
complete CMC catalogue.

The most important correction is conceptual: a price match is corroborating
evidence, not identity proof. Wrapped, bridged, liquid-staking, and pegged
assets can have the same ticker and nearly the same price as their underlying
asset. The current `verified` status is therefore stronger than the available
evidence supports.

## Findings

### 1. High — price cannot verify identity

[`select_verified_candidate()`](../atlas/cmc_id_prototype.py#L40) approves an
asset when it is the only same-ticker candidate within the configured price
threshold. The CLI then labels that result `verified`.

A sole price-compatible candidate can still be the wrong asset. A wrapper or
bridged representation may track the underlying asset closely, and the native
asset may be missing from the incomplete website catalogue. Price should only
be used as a sanity check after identity has been established through stronger
evidence such as platform and contract address, trusted aliases, or manual
approval.

### 2. High — the prototype does not cover Atlas exchanges

[`fetch_binance_usdm_price()`](../atlas/cmc_id_prototype.py#L84) checks only a
Binance USD-M `<SYMBOL>USDT` contract. The prototype does not read Atlas
snapshots and does not support:

- Binance spot or coin-margined instruments;
- USDC-only and non-dollar quote pairs;
- exchanges other than Binance;
- dated futures;
- multiplier contracts such as `1000BONK`; or
- synthetic, commodity, equity, and index underlyings that have no CMC asset.

The current implementation should be described as a Binance-only probe rather
than an all-exchange matcher.

### 3. High — keyless CMC pagination is incomplete and non-unique

[`fetch_cmc_assets()`](../atlas/cmc_id_prototype.py#L67) increments `start` by
5,000 and decides that pagination is complete from the returned page length.

During this audit, the website endpoint reported a total of 8,143 assets. Two
requests returned 8,145 rows but only 8,141 unique CMC IDs, with four IDs
duplicated. The result therefore contained duplicates while omitting at least
two assets reported by the endpoint itself.

This behavior can create false ambiguity, hide valid candidates, or make the
result depend on CMC's current insertion and sorting behavior. The undocumented
website endpoint must not be treated as a complete production catalogue.

### 4. High — the proposed persistence model does not fit Atlas

The [mapping plan](cmc-id-mapping-plan.md#production-implementation) proposes
making CMC IDs unique among active mappings. Atlas currently stores exchange
instrument rows rather than canonical asset entities. Many spot, perpetual,
and dated contracts should legitimately share the same CMC ID.

For the current model, mappings need to be attached to an instrument instance,
initially keyed by `(exchange, original_id, first_capture)`. Repeating the same
`cmc_id` across related instruments is expected. A separate canonical asset
table can be introduced later if its ownership and migration are designed
explicitly.

### 5. Medium — the compared prices are not temporally or economically aligned

The prototype discards both source timestamps and compares CMC's aggregate USD
spot price with a Binance USDT-margined futures last price. The difference can
include:

- source timestamp skew;
- USDT/USD deviation;
- futures basis;
- low-liquidity last trades; and
- exchange contract multipliers.

A fixed 5% threshold can consequently accept incorrect mappings and reject
valid ones. Verification needs unit normalization, explicit quote conversion,
bounded timestamp skew, and preferably multiple aligned observations.

### 6. Medium — one malformed CMC row can abort catalogue loading

[`_parse_cmc_asset()`](../atlas/cmc_id_prototype.py#L101) assumes every item has
`quotes[0].price`. An empty quote array, null price, API error envelope, or
website schema change can terminate the entire run. The initial CMC fetch also
occurs outside the per-symbol exception handling.

The client has no response-envelope validation, retry/backoff, rate-limit
handling, partial-result reporting, or failure exit code suitable for
automation.

### 7. Medium — the implementation duplicates an existing integration pattern

Atlas already implements client behavior, identity matching, price checks,
metadata persistence, retries, and CLI orchestration in
[`integrations/coingecko.py`](../integrations/coingecko.py) and
[`integrations/coingecko_metadata.py`](../integrations/coingecko_metadata.py).

The CMC client and enrichment command should follow that layout. Keeping an
explicitly unsupported website scraper inside `atlas/` also ships it as part of
the published package.

### 8. Medium — the delisting proposal duplicates existing lifecycle fields

Atlas already translates upstream availability to `first_capture` and
`end_date` in [`atlas/update.py`](../atlas/update.py#L175), and
[`SecurityMaster`](../atlas/database.py#L49) uses those fields for historical
availability.

Adding parallel `listed_at` and `delisted_at` fields would create competing
lifecycle definitions. More useful additions are `last_seen_at`,
`end_date_source`, and `end_date_confidence`.

Symbol reuse also needs explicit handling. If an exchange reuses an original ID
for a different asset, a mapping keyed only by `(exchange, original_id)` can
contaminate historical records. A relisting or reuse must create a new
instrument instance.

### 9. CI is red, although the failures are unrelated to this diff

The Linux CI job reports 131 passed, 4 skipped, and 3 failures. The failures are
strict live Tardis-to-snapshot comparisons for Binance spot, OKX futures, and
OKX perpetuals. Live metadata advanced after the committed snapshots; this PR
changes neither those snapshots nor those tests.

Because the workflow uses the matrix's default fail-fast behavior, the Linux
failure cancels the macOS and Windows jobs. Required PR CI should be
deterministic; strict live equivalence belongs in scheduled monitoring.

## Remediation plan

### Phase 1 — correctly position and harden the prototype

1. Keep PR #18 in draft.
2. Move the prototype under `integrations/` so it is not part of the published
   Atlas package.
3. Rename `verified` to `price_compatible` and state clearly that the probe
   covers Binance USD-M only.
4. Do not persist any mapping produced by the keyless website probe.
5. Deduplicate results by CMC ID and report:
   - returned row count;
   - unique ID count;
   - duplicated IDs;
   - CMC-reported total; and
   - the discrepancy between reported and observed totals.
6. Represent outcomes explicitly as `no_ticker_candidate`, `price_mismatch`,
   `multiple_price_matches`, `price_compatible`, and `fetch_failed`.
7. Add fixture-based tests for overlapping pages, incomplete totals, missing
   quotes, malformed envelopes, HTTP errors, and partial symbol failures.

### Phase 2 — add the authenticated CMC integration

1. Add `integrations/coinmarketcap.py`, following the existing CoinGecko client
   conventions.
2. Use the authenticated CMC map endpoint for identity discovery, metadata/info
   for platform and address evidence, and batched quotes by CMC ID.
3. Request both active and inactive identities needed by Atlas historical rows.
4. Add explicit response validation, request timeouts, retry/backoff,
   rate-limit handling, batching, and caching.
5. Require `CMC_PRO_API_KEY` only when the CMC enrichment command is invoked;
   importing or loading Atlas must remain keyless.

### Phase 3 — establish identity before checking price

Use candidate evidence in descending order of strength:

1. exact platform/network and token contract address;
2. versioned manual aliases and approved overrides;
3. project name, CMC slug, and known rebrand history;
4. unique ticker as candidate generation only; and
5. price as a final sanity check, never as the identity source.

Every accepted mapping should retain its method, evidence, source timestamps,
approval provenance, and verification time. An approved CMC ID must never be
replaced automatically.

### Phase 4 — store mappings per instrument instance

1. Identify rows by `(exchange, original_id, first_capture)` or introduce a
   dedicated instrument-instance ID.
2. Permit many instrument rows to share one `cmc_id`.
3. Persist match status and structured evidence with the row or in a versioned
   sidecar keyed by instrument instance.
4. Add explicit handling for symbol reuse, relisting, wrapped assets, exchange
   multipliers, and non-crypto underlyings.

### Phase 5 — make price verification comparable

1. Normalize multiplier contracts to the price of one base unit.
2. Prefer exchange spot prices where available; otherwise use a documented mark
   price or aligned historical candle.
3. Convert the exchange quote currency explicitly to the CMC quote currency.
4. Require source timestamps inside a bounded window.
5. Compare multiple observations when possible rather than one last trade.
6. Implement exchange price adapters incrementally, each with recorded fixtures
   and normalization tests.

### Phase 6 — align delisting with Atlas

1. Keep `first_capture` and `end_date` as the authoritative availability
   interval.
2. Add `last_seen_at`, source, and confidence metadata only where required.
3. Infer a delisting only after successful, complete source snapshots or an
   explicit exchange status; a failed or partial fetch must never delist rows.
4. Verify delisted assets with exchange and CMC historical prices near their
   last active date.
5. Preserve inactive CMC IDs indefinitely and never reassign an old mapping
   because a ticker was reused.
6. Represent a relisting or reused exchange symbol as a new instrument instance.

### Phase 7 — make CI deterministic

1. Move strict live Tardis equivalence checks to a scheduled monitoring
   workflow.
2. Keep fixture-based mapping, schema, and lifecycle tests in required PR CI.
3. Refresh exchange snapshots through a separate automated update PR.
4. Rebase PR #18 and rerun its complete matrix after the CI separation lands.

## Acceptance criteria

- No result is called verified solely because its ticker and price match.
- The keyless probe reports catalogue incompleteness and never persists data.
- Production lookups use authenticated CMC endpoints and query quotes by CMC ID.
- Every active Atlas instrument has a classified CMC mapping outcome.
- Many exchange instruments may safely share one CMC ID.
- Price evidence is normalized and timestamp-aligned.
- Delisted and relisted instruments remain historically distinguishable.
- Required PR CI is deterministic and independent of live metadata drift.
