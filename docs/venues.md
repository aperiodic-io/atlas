# Venues, listings and asset classes

The reference behind [the README](../README.md): which venues Atlas reads, how
each one's listing and delisting dates are derived, and where every row's
`underlying` asset class comes from.

- [Supported exchanges](#supported-exchanges)
- [Underlying classification](#underlying-classification)
- [Refreshing the snapshots](#update-local-snapshots)
- [Listing and delisting dates](#listing-and-delisting-dates)
- [CoinMarketCap IDs](#coinmarketcap-ids)
- [Limitations](#limitations)

## Supported Exchanges

Every exchange below is read from its own API and refreshed by the daily update.
BitMEX (closed 2026-09-23) and FTX are no longer supported.

DeFi / RWA venues (DEX) first, then centralised exchanges (CEX).

| Exchange ID | Venue type | Instruments |
| --- | --- | --- |
| `apex-perps` | DEX | perpetual |
| `aster-perps` | DEX | perpetual |
| `avantis-perps` | DEX | perpetual |
| `extended-perps` | DEX | perpetual |
| `grvt-perps` | DEX | perpetual |
| `hyperliquid-perps` | DEX | perpetual |
| `hyperliquid-spot` | DEX | spot |
| `lighter-perps` | DEX | perpetual |
| `pacifica-perps` | DEX | perpetual |
| `variational-perps` | DEX | perpetual |
| `binance-futures` | CEX | perpetual, future |
| `binance-futures-cm` | CEX | perpetual, future |
| `binance-spot` | CEX | spot |
| `bitfinex-perps` | CEX | perpetual |
| `bitfinex-spot` | CEX | spot |
| `bitget-futures-coin` | CEX | future |
| `bitget-perps-coin` | CEX | perpetual |
| `bitget-perps-usdc` | CEX | perpetual |
| `bitget-perps-usdt` | CEX | perpetual |
| `bitget-spot` | CEX | spot |
| `bitstamp-perps` | CEX | perpetual |
| `bitstamp-spot` | CEX | spot |
| `bybit-futures` | CEX | future |
| `bybit-perps-coin` | CEX | perpetual |
| `bybit-perps-usd` | CEX | perpetual |
| `bybit-spot` | CEX | spot |
| `coinbase-spot` | CEX | spot |
| `cryptocom-futures` | CEX | future |
| `cryptocom-perps` | CEX | perpetual |
| `cryptocom-spot` | CEX | spot |
| `deribit-futures` | CEX | future |
| `deribit-perps` | CEX | perpetual |
| `gateio-futures-usdt` | CEX | future |
| `gateio-perps-btc` | CEX | perpetual |
| `gateio-perps-usdt` | CEX | perpetual |
| `gateio-spot` | CEX | spot |
| `gemini-perps` | CEX | perpetual |
| `gemini-spot` | CEX | spot |
| `htx-futures-coin` | CEX | future |
| `htx-futures-usdt` | CEX | future |
| `htx-perps-coin` | CEX | perpetual |
| `htx-perps-usdt` | CEX | perpetual |
| `htx-spot` | CEX | spot |
| `kraken-futures` | CEX | future |
| `kraken-perps` | CEX | perpetual |
| `kraken-spot` | CEX | spot |
| `kucoin-spot` | CEX | spot |
| `mexc-perps` | CEX | perpetual |
| `okx-futures` | CEX | future |
| `okx-perps` | CEX | perpetual |
| `okx-spot` | CEX | spot |
| `phemex-perps` | CEX | perpetual |
| `phemex-spot` | CEX | spot |
| `poloniex-spot` | CEX | spot |
| `upbit-spot` | CEX | spot |
| `whitebit-perps` | CEX | perpetual |

### DeFi / RWA perpetual venues

The eight venues [live-producers](https://github.com/aperiodic-io/live-producers)
added for real-world-asset perpetuals (aperiodic-io/live-producers#154) are registered under the producer's
slug, so a streamed record joins its Atlas row by `(exchange, original_id)`.
Every live contract's producer ID equals its Atlas `internal_id`. Each venue
settles in one collateral token, which is the row's `denominator` and `margin`
(the Hyperliquid precedent). Each venue is its own ticker namespace in
[symbology](symbology.md).

| Exchange ID | Native ID | Settles in | Asset classes | Symbology evidence |
| --- | --- | --- | --- | --- |
| `avantis-perps` | pair index (`86`) | USDC | equity, crypto, FX, commodity | not on CMC: cross-venue consensus and venue references |
| `aster-perps` | `tslausdt` | USDT, USD1, USDC, U | crypto, equity, commodity, pre-IPO | CMC `aster-pro` |
| `variational-perps` | ticker (`TSLA`) | USDC | crypto, equity, commodity, pre-IPO | CMC `variational` |
| `lighter-perps` | market index (`112`) | USDC | crypto, equity, FX, commodity, pre-IPO, index | CMC `lighter`, matched by market symbol |
| `grvt-perps` | `TSLA_USDT_Perp` | USDT | equity, crypto, commodity, pre-IPO | CMC `grvt` |
| `extended-perps` | `TSLA-USD` | USD | crypto, equity, commodity, index, FX, pre-IPO | CMC `extended` |
| `apex-perps` | `TSLAUSDT` | USDT | crypto, equity, commodity, index | CMC `apex-omni` |
| `pacifica-perps` | ticker (`TSLA`) | USDC | crypto, equity, commodity, FX, index | CMC `pacifica` |

Venue spellings to know:

- **FX.** A pair quoted in USD is named by its base currency (`EUR`); other
  pairs keep the full ticker (`USDJPY`). Symbology maps both to one
  currency-pair asset in market convention (`EURUSD`, `USDJPY`).
- **Extended** lists 24/5-session stock markets as `AAPL_24_5-USD`, and some
  indices with an `m` suffix (`SPX500m-USD`). Its collateral is reported as `USD`.
- **Avantis** keeps a non-USD price currency in the ticker (`005930KRW`, Samsung
  priced in won). Symbology resolves its base asset to the stock.
- **Lighter** and **Avantis** address markets by number. The ticker is in the
  row's `symbol` (and Lighter's `base_asset`).
- **Pre-IPO** names differ by venue (`ANTHROPIC`, Extended `ANTHROP`,
  Hyperliquid `xyz:ANT`). Symbology resolves the ones CoinMarketCap or a
  curated pin identifies.

### live-producers venues

The venues the live producers stream are registered under the producer's own
exchange id (`gateio-perps-usdt`, `htx-perps-usdt`, ...), so a
published record joins its Atlas row by `(exchange, internal_id)` without a
mapping table. Bitget, Gate and Coinbase spot take the same scheme
(`bitget-spot`, `gateio-spot`, `coinbase-spot`, as `okx-spot`) though the
producers do not stream them yet. Bybit's perpetuals are the producer's
`bybit-perps-usd` (linear: USDT- and USDC-settled) and `bybit-perps-coin`
(inverse). They replace `bybit-perps`, which held both: read the two venues
instead.

### Other centralised venues

The other centralised venues take the same scheme: Kraken (`kraken-spot`;
Kraken Futures as `kraken-perps` and `kraken-futures`), Bitfinex
(`bitfinex-spot`, `bitfinex-perps`), Bitstamp (`bitstamp-spot`,
`bitstamp-perps`), Gemini (`gemini-spot`, `gemini-perps`), HTX (`htx-spot`,
`htx-perps-coin`, `htx-futures-coin`, `htx-futures-usdt`), KuCoin
(`kucoin-spot`), Phemex (`phemex-spot`, `phemex-perps`), Poloniex
(`poloniex-spot`), Upbit (`upbit-spot`) and Crypto.com spot (`cryptocom-spot`).

- Kraken spot ids are Kraken's WebSocket v2 names (`BTC/USD`, `AAPLx/USD`,
  `AAPLSPV/USD`); history before 2026-07-10 holds v1 names (`XBT/USD`), which
  resolve to the same `internal_id`.
- `htx-futures-coin` ids are HTX's dated contract codes (`BTC261225`), not its
  rolling aliases (`BTC_CQ`).
- Poloniex history holds v1 `QUOTE_BASE` ids (ended 2022-08-02) next to current
  `BASE_QUOTE` ids; the parser tells them apart by that cut-over.
- Gemini publishes no bulk details endpoint, so its fetcher reads each symbol's
  details within Gemini's public rate limit (about three and a half minutes
  a run).

New rows take the venue's listing time as `first_capture` where it publishes
one, otherwise their first observation (see [Listing and delisting
dates](#listing-and-delisting-dates)).

## Underlying classification

Every exchange fetcher sets `underlying` from the venue's own metadata:
Binance `underlyingType`, Bybit `symbolType`, OKX `instCategory`, Hyperliquid
`perpCategories` (main-dex perps are crypto; HIP-3 assets the venue does not
categorise, mostly delisted, come from a curated table in
`exchange_definitions/hyperliquid.py`), Gate `contract_type` and
`is_pre_market`, HTX `tradfi_labels`, MEXC trade zones (`conceptPlate`),
Bitget `symbolType`/`isRwa`, Deribit `underlying_type`, WhiteBIT TradFi
membership of `/markets`, Crypto.com `product_type`, Kraken spot
`aclass_base` (xStocks are equity, fiat-base pairs forex) and Kraken Futures
`category`, HTX spot base-currency `tags` and USDT contracts `tradfi_labels`,
Bitfinex `pub:list:pair:securities` (tokenized securities are `unknown`),
Bitstamp perpetual `asset_class` and spot `/currencies/` `type`, KuCoin's
`market` zone and xStock currency names, Phemex
`perpProductSubType`/`perpProductSubGroup`. Venues that publish no
classification and list only crypto (Upbit, Poloniex, HTX coin-margined
contracts) are `crypto`; Gemini spot is `crypto` except fiat-base pairs
(`forex`), and Bitfinex perpetuals are classed by the venue's naming
(`…IX` indices, volatility indices, metals and oil, fiat bases). HTX tags its ETFs
(SPY, QQQ, leveraged funds) `indices` alongside real indices, with nothing in
its metadata to tell them apart, so on HTX (spot and USDT perps alike) they
are `index`; Phemex, whose currency names identify its ETFs, has them as
`equity`. Conventions across
venues: ETFs are `equity`; pre-IPO companies are `pre_market`; crypto and
cash indices, and treasury yields, are `index`; a venue value Atlas does not
know is `unknown`, never `crypto`. Venues disagree on tokenized gold
(PAXG, XAUT): Binance and Bybit call it crypto, MEXC and Bitget commodity;
Atlas follows each venue. History rows delisted before a fetcher saw them
carry no `underlying`.

Avantis (Veranta) is an oracle-priced RWA perp venue: each row's `underlying`
comes from the venue's category metadata (the price-feed asset type, falling
back to the pair group). Its `original_id` is what the venue addresses a market
by (the pair index), and a non-USD quote is kept in the symbol (`USDJPY`,
`005930KRW`). Avantis settles in USDC. See the module docstring in
`atlas/exchange_definitions/avantis.py`.


## Update local snapshots

```bash
# Every exchange, from its own API (what the daily workflow runs)
python3 -m atlas.update

# Restrict to selected exchanges
python3 -m atlas.update --exchanges binance-spot,okx-spot
```

### Listing and delisting dates

Each fetch is treated as the exchange's whole current
universe, so `first_capture` and `end_date` come from the exchanges:

- `first_capture` is the exchange listing time where one is published
  (Binance futures and Aster `onboardDate`, Bybit derivatives and Bitget
  `launchTime`, OKX `listTime`, Gate `launch_time`, HTX `create_date`, MEXC
  `openingTime`/`createTime`, Deribit `creation_timestamp`, Extended `createdAt`,
  Lighter `created_at`, GRVT `create_time`, Pacifica `created_at`, Bitfinex
  `pub:info:pair` creation time, HTX spot `toa` and contracts `create_date`,
  Kraken Futures `openingDate`, KuCoin `tradingStartTime`, Phemex `listTime`,
  Poloniex `tradableStartTime` except on markets migrated from its v1 API, which
  all report the 2022-07-28 migration), otherwise the
  time a run first saw the instrument. A value already on file is never
  overwritten.
- `end_date` is set from an explicit exchange signal when there is one: Binance
  futures `SETTLING`/`CLOSE`/`DELIVERED` (at `deliveryDate`, or the run time if
  that has not passed yet), Bybit derivatives `Closed` (at `deliveryTime`), and
  Hyperliquid `isDelisted` (at first sighting, as Hyperliquid publishes no time).
  Hyperliquid also keeps no candle or funding history for delisted HIP-3 perps,
  so HIP-3 perps already delisted when this tracking started end on the first
  run that saw the flag. Of the live-producers venues, Aster
  `SETTLING`/`CLOSE`/`DELIVERED` (as Binance), Bitget `offline` (at `offTime`)
  and HTX delisted/delivered or past its `delivery_time` (the scheduled
  delisting) are dated; Extended `DELISTED`, GRVT `DELISTED`, Lighter
  `inactive`, Avantis `isPairListed: false`, MEXC delivered/offline and Gate
  `delisted` end at first sighting. Of the other centralised venues, Phemex
  `Delisted` (at the delisted milestone of its `timeline`), Kraken Futures
  `isExpired` (at `lastTradingTime`) and HTX coin-margined and USDT-margined
  contracts (as HTX USDT perps) are dated; Kraken spot `delisted`, Gemini
  `closed`, HTX spot `offline` and Poloniex `OFFLINE` end at first sighting.
- An instrument that simply disappears (Binance spot and COIN-M, Bybit spot,
  OKX, Hyperliquid spot, and every venue without a delisted status: ApeX,
  Pacifica, Variational, Deribit, WhiteBIT, Crypto.com, Bitfinex, Bitstamp,
  KuCoin, Upbit) is marked
  `missing_since` on the first run that misses
  it. If a run at least 12 hours later still misses it, `end_date` is set to that
  first miss; if it comes back first, the mark is dropped. Expired dated futures
  end at delivery straight away.
- Halted, suspended and pre-market instruments (Binance spot `HALT`, Binance
  futures pending/delivering statuses, OKX `suspend`, Bybit
  `PreLaunch`/`Delivering`, Bitget `limit_open`/`limit_close`/`restrictedAPI`,
  HTX suspended and settling, MEXC paused and delivering, Gate `delisting` and
  `circuit_breaker`, Extended `PRELISTED`/`REDUCE_ONLY`, ApeX contracts that
  still trade or show, Kraken spot `cancel_only`/`post_only`/`limit_only`/
  `reduce_only`/`maintenance`, Kraken Futures `suspended`/`postOnly`, Bitstamp
  `trading: Disabled`, Gemini `cancel_only`/`post_only`/`limit_only`, HTX spot
  `suspend`/`fuse`, KuCoin `enableTrading: false` and `st`, Upbit
  warning/caution, Poloniex `PAUSE`/`POST_ONLY`) stay listed. Instruments that
  have not started trading are not added: Kraken spot `work_in_progress`, Kraken
  Futures before `openingDate`, HTX spot `pre-online`, KuCoin before
  `tradingStartTime` or in its opening call auction, Phemex before `listTime`,
  Poloniex before `tradableStartTime`, Bitget `listed`, HTX pending listing.
  An instrument already on file that the venue lists as not trading yet (a
  relaunch under the same id) stays exactly as it is, neither closed nor
  reopened, until it trades.
- A run that returns nothing for an exchange fails it and leaves its file
  untouched. A run that would newly drop more than 10% of an exchange's open
  instruments (and more than 5) looks like a broken API response: it still
  updates the instruments it returned, but marks nothing missing and fails the
  exchange. If the instruments really were delisted, rerun with
  `--accept-missing <exchange>` (or the `accept_missing` input when dispatching
  the daily workflow by hand).
- `SecurityMaster` ignores `missing_since`, so a row stays tradable until its
  `end_date` is set.
- An existing `end_date` is never overwritten, a relisted instrument has its
  `end_date` cleared, and delisted instruments that were never on file are not
  added.


## CoinMarketCap IDs

Snapshot rows carry a `cmc_id` identifying the underlying asset, plus its
Atlas `category` list. Many exchange instruments may share one `cmc_id` — it names an
asset, not a contract — and `atlas/update.py` treats the field as locally owned,
so a snapshot refresh never overwrites it. The symbology build
(`python -m integrations.symbology`, run daily) keeps `cmc_id` and `category` in
step with each row's `base_asset_id`, and resolves every listing CoinMarketCap
tracks on the seven supported venues from CMC's own exchange market pairs.

New `binance-futures` listings are resolved automatically by the
`cmc-new-symbol-mapping` workflow, which runs daily after the snapshot update:

```bash
# Resolve symbols first listed in the last 60 days
python integrations/cmc_new_symbol_mapping.py --new-within-days 60

# Offline staleness check: coverage only, no network, no writes
python integrations/cmc_new_symbol_mapping.py --coverage-only --new-within-days 60

# Check the LLM endpoint is usable, in seconds
python integrations/cmc_new_symbol_mapping.py --check-llm

# Deterministic evidence only, no LLM adjudication
python integrations/cmc_new_symbol_mapping.py --no-llm

# Resolve only these tickers, ignoring age and any prior decision
python integrations/cmc_new_symbol_mapping.py --symbols PEPE,CHEEMS
```

The workflow exposes these as one `mode` input (`resolve`, `preview`,
`coverage`, `check-llm`) and one `days` window.

Only crypto assets are in scope: rows whose `underlying` is `equity`, `index`,
`commodity`, `forex` or `pre_market` are skipped, since a tokenized equity's CMC ID would
name the wrapper rather than the asset. Rows with no `underlying` (legacy crypto)
and `unknown` are still resolved.

A ticker and an agreeing price never approve a mapping on their own. Approval
needs identity evidence — an exact match between the Binance asset name and the
CMC project name or slug — or an existing mapping on a same-ticker instrument
that was listed at the same time. An LLM disambiguates between candidates that
already carry name evidence, choosing *among the fetched candidates only*.
Confident matches are written to the snapshots; everything else is reported for
review. Every decision — candidates, price
evidence and rationale — is recorded per instrument instance in
`atlas/data/cmc_mappings.json`, and each run opens its own pull request covering
both the confident and the uncertain matches. See
[`docs/cmc-id-mapping.md`](cmc-id-mapping.md) for the decision ladder,
configuration and review process.


## Limitations

- No options mapping in the securities master map:
  - `update.py` ingests only `spot`, `perpetual`, and `future` from source metadata.
  - Option contracts are not fetched or mapped.
- Instruments without a derived `internal_id` (for example options, combos, or unrecognized formats) are intentionally absent from `SecurityMaster` lookups.
- History starts at what the bundled snapshots hold: a venue added later starts from the instruments its API lists at that time.
