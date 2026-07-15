# CoinMarketCap ID mapping and delisting plan

## Goal

Give every Atlas base asset a stable CoinMarketCap ID, then verify each
exchange symbol against its CMC USD price without treating a ticker as an
identity. A CMC ID belongs to an asset, not an exchange-specific contract, so
all spot, perpetual, and dated contracts for the same base asset reuse it.

## Prototype

Run the keyless exploration tool with:

```bash
python -m integrations.cmc_id_probe --symbols BTC,ETH,SOL
```

It fetches CMC's website listing endpoint and timestamped Binance spot prices.
This endpoint returned data without a key during development, while the supported
CMC Pro quote endpoint returned `401 API key missing` without one. The tool is
not production code: the website endpoint is undocumented, may paginate
inconsistently, and may change. The probe emits catalogue diagnostics and never
persists a mapping.

The prototype labels a result `price_compatible` only when one—and only one—
same-ticker CMC candidate is within the price and timestamp thresholds. This is
not identity verification. For example, a wrapped asset can share both a ticker
and price with the native asset; it must remain ambiguous unless stronger
identity evidence is available.

To attach this non-authoritative evidence to every bundled exchange row, run:

```bash
python -m integrations.cmc_probe_metadata
```

The updater writes a `cmc_probe` object containing the status, reference
Binance spot observation, and CMC evidence when price-compatible. It never
writes an approved `cmc_id`; use `--dry-run` to inspect the per-exchange status
counts without changing snapshots.

## Production implementation

1. Add a `CoinMarketCapClient` configured with `CMC_PRO_API_KEY`; fail the
   enrichment command with a clear error when the key is absent, while keeping
   normal Atlas imports and loading keyless. Use `/v1/cryptocurrency/map` for
   the full identity catalogue, `/v2/cryptocurrency/info` for metadata and
   platform information, and `/v3/cryptocurrency/quotes/latest?id=...&convert=USD`
   for batched price checks. Persist the CMC ID and query quotes by ID only.
2. Store mappings by instrument instance: `(exchange, original_id,
   first_capture)`. The versioned `integrations/cmc_mappings.py` sidecar model
   allows many contracts to share one `cmc_id`, preserves a relisted symbol as a
   distinct instance, records structured evidence, and refuses to replace an
   approved mapping unless an explicit override is supplied.
3. Build candidate sets from Atlas's `symbol` values across every snapshot.
   Exact ticker match is only the initial candidate set. Narrow it with the
   CMC name/slug, platform token address when available, and explicit aliases
   for exchange multipliers, wrappers, and legacy names.
4. Fetch the exchange's latest executable or mark price using a per-exchange
   adapter. Normalize contract multipliers to one base unit and convert
   non-USD/USDT quotes to USD using a verified FX/crypto cross-rate. Compare
   timestamps and prices in one bounded time window; classify the result as
   price-compatible only. Store both prices, timestamps, tolerance, and
   relative difference as evidence; never use the comparison as identity proof.
5. Route zero-candidate, multi-candidate, stale, and out-of-tolerance results
   to a review queue. Do not overwrite a previous verified CMC ID automatically.
   Approved overrides are versioned, tested fixtures.
6. Run an initial backfill, then run an incremental job after the existing
   Atlas snapshot update. Batch CMC requests within plan limits, cache the map
   response, rate-limit/retry transient failures, and expose counts for
   verified, ambiguous, unmapped, stale, and rejected assets.

## Delisted assets

1. Retain every historical Atlas contract row. `first_capture` and `end_date`
   remain the authoritative availability interval. When Tardis provides an
   `availableTo` value, Atlas now retains `end_date_source=tardis` and
   `end_date_confidence=authoritative`; absence from a failed or partial
   exchange fetch must never mean delisting.
2. On each successful exchange snapshot, mark unseen instruments as
   `suspected_delisted`. Confirm only after a configurable number of successful
   snapshots or an explicit exchange status. Record the first and final
   observation times.
3. Keep CMC mappings for delisted assets immutable and query historical quotes
   by their saved CMC ID. If CMC marks an asset inactive or removes it from the
   current map, set the mapping status to `cmc_inactive`; never recycle its ID
   or reassign the ticker to a new instrument instance.
4. Exclude confirmed-delisted instruments from active discovery, while keeping
   them available for date-window and historical lookups. Add an explicit
   `include_delisted` option for consumers that need them.
5. Alert on an exchange-wide disappearance, a CMC-ID change proposal, or a
   delisting without prior successful snapshots; these are data-quality events,
   not routine lifecycle changes.

## Acceptance criteria

- Every active Atlas instrument instance is classified as approved, ambiguous,
  unmapped, rejected, or awaiting price.
- An approved mapping has immutable CMC ID plus recorded identity and price
  evidence.
- Same-ticker wrappers and multiplier tokens cannot be auto-approved from
  price alone.
- Delisted contracts and inactive CMC assets remain historically queryable.
