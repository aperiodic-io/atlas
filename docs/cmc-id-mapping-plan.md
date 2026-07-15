# CoinMarketCap ID mapping and delisting plan

## Goal

Give every Atlas base asset a stable CoinMarketCap ID, then verify each
exchange symbol against its CMC USD price without treating a ticker as an
identity. A CMC ID belongs to an asset, not an exchange-specific contract, so
all spot, perpetual, and dated contracts for the same base asset reuse it.

## Prototype

Run the keyless exploration tool with:

```bash
python -m atlas.cmc_id_prototype --symbols BTC,ETH,SOL
```

It fetches CMC's website listing endpoint and Binance USD-M prices. This
endpoint returned data without a key during development, while the supported
CMC Pro quote endpoint returned `401 API key missing` without one. The tool is
not production code: the website endpoint is undocumented and may change.

The prototype only labels a result `verified` when one—and only one—same-ticker
CMC candidate is within the price threshold. For example, a wrapped asset can
share both a ticker and price with the native asset; this must remain ambiguous,
not be silently mapped.

## Production implementation

1. Add a `CoinMarketCapClient` configured with `CMC_PRO_API_KEY`; fail startup
   with a clear error when the key is absent. Use `/v1/cryptocurrency/map` for
   the full identity catalogue, `/v2/cryptocurrency/info` for metadata and
   platform information, and `/v3/cryptocurrency/quotes/latest?id=...&convert=USD`
   for batched price checks. Persist the CMC ID and query quotes by ID only.
2. Introduce an asset-level mapping store with `symbol`, `cmc_id`, `slug`,
   `status`, `confidence`, `verified_at`, source timestamps, and a JSON evidence
   record. Make `(cmc_id)` unique among active mappings; never use ticker as a
   unique key.
3. Build candidate sets from Atlas's `symbol` values across every snapshot.
   Exact ticker match is only the initial candidate set. Narrow it with the
   CMC name/slug, platform token address when available, and explicit aliases
   for exchange multipliers, wrappers, and legacy names.
4. Fetch the exchange's latest executable or mark price using a per-exchange
   adapter. Convert non-USD/USDT quotes to USD using a verified FX/crypto
   cross-rate. Compare timestamps and prices in one bounded time window;
   accept only one candidate inside a configurable tolerance. Store both
   prices, timestamps, tolerance, and relative difference as evidence.
5. Route zero-candidate, multi-candidate, stale, and out-of-tolerance results
   to a review queue. Do not overwrite a previous verified CMC ID automatically.
   Approved overrides are versioned, tested fixtures.
6. Run an initial backfill, then run an incremental job after the existing
   Atlas snapshot update. Batch CMC requests within plan limits, cache the map
   response, rate-limit/retry transient failures, and expose counts for
   verified, ambiguous, unmapped, stale, and rejected assets.

## Delisted assets

1. Retain every historical Atlas contract row. Add `listed_at`, `delisted_at`,
   `delisting_source`, and `last_seen_at`; absence from a single failed exchange
   fetch must never mean delisting.
2. On each successful exchange snapshot, mark unseen instruments as
   `suspected_delisted`. Confirm only after a configurable number of successful
   snapshots or an explicit exchange status. Record the first and final
   observation times.
3. Keep CMC mappings for delisted assets immutable and query historical quotes
   by their saved CMC ID. If CMC marks an asset inactive or removes it from the
   current map, set the mapping status to `cmc_inactive`; never recycle its ID
   or reassign the ticker to a new asset.
4. Exclude confirmed-delisted instruments from active discovery, while keeping
   them available for date-window and historical lookups. Add an explicit
   `include_delisted` option for consumers that need them.
5. Alert on an exchange-wide disappearance, a CMC-ID change proposal, or a
   delisting without prior successful snapshots; these are data-quality events,
   not routine lifecycle changes.

## Acceptance criteria

- Every active Atlas base symbol is classified as verified, ambiguous, unmapped,
  rejected, or awaiting price.
- A verified mapping has immutable CMC ID plus recorded identity and price
  evidence.
- Same-ticker wrappers and multiplier tokens cannot be auto-approved from
  price alone.
- Delisted contracts and inactive CMC assets remain historically queryable.
