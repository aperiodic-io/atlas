# Hyperliquid CoinMarketCap mapping

The 2026-10-04 backfill approved **206 instrument instances**: **178 perpetuals**
and **28 spot pairs**. The ledger also records 203 uncertain and 183 unmapped
instances. The remaining 271 builder rows are outside the default scope. Every
original row and lifecycle field was retained; the existing Binance mappings
were preserved. Each approval was audited against its exact snapshot instance,
identity evidence, normalized price, 5% tolerance and 180-second source-time
window. Targeted retries resolved initially stale comparisons, and native token-ID evidence added eight spot mappings.

Hyperliquid uses the existing Binance resolver, immutable instrument-instance
ledger, catalogue trust gate, price tolerances, review report, pending-PR
deduplication and daily automation. Default scope now includes
`binance-futures,hyperliquid-perps,hyperliquid-spot`. Imports and snapshot loading
remain keyless. Network enrichment stays under `integrations/`.

## Identity evidence

Perpetuals use CMC's explicit link from an exact Hyperliquid instrument trade URL
to a stable CMC ID, or the existing concurrently listed underlying mapping.
Market links must identify CMC exchange 8112, category `perpetual`, and an exact
`https://app.hyperliquid.xyz/trade/<instrument>` URL whose instrument agrees with
the market's base symbol. The ID must also exist in the fetched catalogue.
Conflicting market links remain ambiguous. An explicit identity link takes
precedence over a different concurrent mapping; approved ledger IDs are immutable.

Spot tokens resolve through an exact HyperEVM contract address, native Hyperliquid
token ID and network, or
an exact normalized token `fullName` match to the CMC name/slug. Address evidence
includes both the Hyperliquid token ID/EVM address and CMC platform metadata.
When CMC omits its native platform field, an exact token-ID link on
`https://app.hyperliquid.xyz/explorer/token/<tokenId>` supplies that evidence.
The host, path and full 16-byte token ID are validated; unrelated explorers,
queries and partial addresses cannot identify a token. Source links are retained
in the ledger.
An explicit conflicting CMC contract prevents name-only approval. Binance names,
concurrent perpetual tickers, and CMC's spot-market links cannot approve HIP-1
tokens: CMC sometimes groups bridged spot tokens with the native underlying.
UBTC, UETH, KHYPE and other wrapped/staked assets keep their own identities.

`kBONK`, `kDOGS`, `kFLOKI`, `kLUNC`, `kNEIRO`, `kPEPE` and `kSHIB` represent
1,000 underlying units. These are explicit aliases; `KHYPE` is not a multiplier.
Reviewed crypto aliases on `cash`, `flx`, and `hyna` are listed in
`integrations/hyperliquid.py`. Other builder symbols stay namespaced and are
outside the default crypto scope, including Seagate `xyz:STX` and natural gas
`flx:GAS`. Coverage applies the same scope. An explicit `--symbols` selection
or empty `--skip-underlyings` can still report those rows for review.

## Price checks and failures

The [spot API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot)
joins token indices to pair metadata and live contexts by API coin ID. Contexts
for outcome markets are ignored. Spot prices use the actual pair (`@107`, for
example), rather than guessing from a display ticker. Only USDC quotes are used;
USDC is an explicit dollar proxy, as Binance's adapter treats USDT. Unsupported
quote assets require a future conversion adapter.

The [perpetual API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
supplies live mark contexts. Delisted markets and invalid prices are withheld.
Only the main USDC market and builders explicitly identifying USDC collateral
(token index zero) supply prices. USDH/USDe/USDT0 collateral is not assumed to be
one dollar. One instrument's missing price never validates another instrument.

CMC listing quotes can be cached beyond the alignment window. Identity-selected
quotes are refreshed through the existing CMC detail endpoint by immutable ID,
with cache-busting request timestamps. A failed refresh withholds approval.
Both the CMC price and its actual `latestUpdateTime` are retained in evidence.
Where needed, a closed Hyperliquid one-minute
[candle](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#candle-snapshot)
at the CMC quote's timestamp provides the exchange comparison. Candles must
identify the exact coin and interval and carry a positive finite close. Only
recent quotes (within one hour) on an independently observed live market are
eligible for this fallback. Missing candles keep the mapping uncertain.

The default 5% price tolerance and 180-second source timestamp window are
unchanged. Multiplier marks and candle closes are normalized once. Neither a
ticker/price match nor an LLM answer establishes identity. Catalogue, identity,
quote-refresh and per-instrument price failures fail closed; HTTP retries are
bounded and respect `Retry-After`.

## Historical backfill

Legacy direct-exchange rows without `first_capture` first need a durable instance
key. Recover it from a full Git checkout:

```bash
python -m integrations.hyperliquid_cmc_history --dry-run
python -m integrations.hyperliquid_cmc_history
python -m integrations.cmc_new_symbol_mapping \
  --exchanges hyperliquid-perps,hyperliquid-spot \
  --new-within-days 5000 --no-llm --dry-run \
  --report-path /tmp/hyperliquid-review.md \
  --summary-path /tmp/hyperliquid-summary.json
```

Remove `--dry-run` from the resolver to persist reviewed results. The recovery
records Atlas's first committed observation as `first_capture`; it does not claim
the exchange listing date. Existing captures are preserved. Disappearance/reappearance
starts a new instance, uncommitted instruments are not assigned invented dates, and
shallow history is rejected. Tardis refreshes replace recovered dates with their
own lifecycle metadata. Strict Tardis equivalence compares instruments covered by
Tardis; Git capture recovery and direct-exchange lifecycles have fixture tests.

Future daily updates stamp newly observed Hyperliquid API rows with a durable
`first_capture`. Repeated updates preserve that capture date and CMC ID.
Partial hybrid Tardis/API responses clear
lifecycle fields only for rows actually supplying Tardis bounds, so the
scheduled new-symbol resolver can continue mapping future direct-API listings.

Recovery added captures to 59 perpetual and 329 spot rows. The backfill scans all
863 existing rows, retaining every instrument and its lifecycle. Non-crypto builder
rows are outside scope. Only approved decisions add snapshot IDs; uncertain and
unmapped decisions remain in `atlas/data/cmc_mappings.json` with their evidence.
`--recheck-decided` retries undecided snapshots after better identity/price evidence
becomes available. An existing snapshot ID is never replaced, and applying a
decision requires the exact `(exchange, original_id, first_capture)` instance.

The keyless CMC detail/market endpoints are website APIs, like the existing
Binance enrichment, and remain less stable than authenticated CMC Pro APIs.
The repository's authenticated-client roadmap still applies. Delisted instruments,
unsupported quote/collateral currencies, missing CMC assets and ambiguous spot
identities stay available for review rather than receiving speculative IDs.
