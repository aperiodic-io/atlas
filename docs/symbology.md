# Unified symbology

Atlas gives every asset one stable, descriptive string ID and points every leg
of every instrument at it, so the same asset reads the same on every venue.
Tesla's perpetual is `xyz:TSLA` on Hyperliquid, market `112` on Lighter, pair
`86` on Avantis, `tslausdt` on Aster and `TSLA_USDT_Perp` on GRVT. All of them
have the base asset `TSLA`, the stock. Bitcoin is `btcusdt` on Binance and
`BTC-USDT` on OKX, and both have the base asset `BTC`. Two different assets that
share a ticker never share an ID: `SPX` the S&P 500 is not SPX6900 the memecoin.

## Asset IDs

Asset IDs are minted by Atlas, once, and never change afterwards.

| Rule | Example |
| --- | --- |
| The upper-case ticker when no other asset holds it | `BTC`, `ETH`, `USDT`, `USD` |
| `<TICKER>.<QUALIFIER>` when the ticker is taken, the qualifier being the asset's descriptive slug | `TSLA.TESLA_TOKENIZED_STOCK_HYPERLIQUID` |
| A numeric suffix if even that is taken (not expected in practice) | `NEIRO.NEIRO_ETH_2` |

The grammar is `[A-Z0-9]+(_[A-Z0-9]+)*(\.[A-Z0-9]+(_[A-Z0-9]+)*)?`: no `-` or
`:`, so asset IDs compose into instrument IDs unambiguously. Tickers are
reduced to ASCII letters and digits (`XAUt` → `XAUT`, `USD₮0` → `USD0`).

When several assets want one ticker the first time the registry is built, the
asset with the most instruments on the supported exchanges gets the bare
ticker, then the higher-ranked one on CoinMarketCap. Fiat currencies are minted
first, so `USD`, `EUR` and `TRY` always name the currency.

An ID is *descriptive at mint time*: if a project renames its ticker later, its
asset keeps the ID and the registry's `ticker` and `name` follow the rename.

The registry lives in [`atlas/data/symbology/assets.json`](../atlas/data/symbology/assets.json):

```json
{
 "asset_id": "PEPE",
 "ticker": "PEPE",
 "name": "Pepe",
 "asset_class": "crypto",
 "cmc_id": 24478,
 "cmc_slug": "pepe",
 "categories": ["Memes"],
 "status": "active",
 "minted_at": "2026-10-14"
}
```

`asset_class` is one of `equity`, `commodity`, `index`, `forex`, `crypto`,
`fiat`, `unknown`. Equities, commodities, indices and currency pairs are the
*reference assets* that real-world-asset perpetuals track. Tesla perpetuals on
15 venues all have base asset `TSLA` (an `equity`), gold perpetuals have `XAU`
(a `commodity`), and every venue's euro perpetual has `EURUSD` (a `forex` pair).
Tokenized wrappers (Ondo's `AAPLON`, Robinhood's stock tokens) are separate
`crypto` assets; a perpetual on the stock is never one of them.

A reference asset Atlas minted before CoinMarketCap tracked it adopts the CMC
ID once CMC lists it. `MELI` stays `MELI` and gains MercadoLibre's CMC ID,
rather than CMC's listing becoming a second asset.

The CoinMarketCap ID is an attribute of an asset, not its identity, so the
registry can be resolved against other providers later without renumbering.

## Instrument fields

`integrations/symbology.py` writes these fields to every snapshot row it can
resolve:

| Field | Meaning |
| --- | --- |
| `base_asset_id` | The asset the instrument trades. |
| `quote_asset_id` | The asset it is priced in. |
| `margin_asset_id` | The asset it is margined and settled in (derivatives only). |
| `base_multiplier` | Units of the base asset per unit of the venue ticker, when not one: `1000PEPEUSDT` and `kPEPE` are 1,000 PEPE. |
| `unified_id` | `<type>-<base>-<quote>[:<margin>][-<yyyymmdd>]` in asset IDs. Mirrors `internal_id`, but is the same on every venue: `perpetual-PEPE-USDT:USDT`. |
| `base_asset_match` | How the base asset was identified (see below). |
| `base_asset_history` | Earlier base assets of an instrument whose ticker changed hands while it traded, as `[{"asset_id": "LUNC", "until": "2022-05-28T00:00:00Z"}]`. |
| `cmc_id`, `category` | Kept in step with the base asset, for existing consumers. |

`unified_id` is a grouping key, not a primary key. It ignores the multiplier,
so it groups economically identical contracts across venues, and it can match
several instruments on one venue: `PEPEUSDT` and `1000PEPEUSDT` share
`perpetual-PEPE-USDT:USDT`, told apart by `base_multiplier`. Use
`(exchange, original_id)` when you need a per-venue key.

## How base assets are identified

Each row's base asset is resolved from the strongest evidence available;
`base_asset_match` records which.

| Method | Evidence |
| --- | --- |
| `override` | A curated decision in [`overrides.json`](../atlas/data/symbology/overrides.json), including ticker eras and venue tickers (below). |
| `cmc_ledger` | An approved decision in the CMC mapping ledger, `atlas/data/cmc_mappings.json` (manual approvals rank above everything but overrides). |
| `cmc_market_pair` | CoinMarketCap's own exchange listing: for every market it tracks on a venue, CMC publishes the venue's native ticker next to the base and quote CMC IDs. Atlas finds its row for that ticker and checks that the tickers agree. Lighter addresses markets by index (`112`), so its rows are found by market symbol instead. |
| `venue_majority` | CMC listed this row under another asset than the venue's other markets of the ticker, and the venue's majority decided (see below). The same label marks a legacy `cmc_id` that the venue's concurrent CMC-identified listings refuted. |
| `snapshot_cmc_id` | A `cmc_id` the row carried from an earlier mapping run. |
| `venue_lineage` | The same ticker on the same exchange, listed at an overlapping time, is identified, and every such listing agrees. An exchange does not list two assets under one ticker at once. |
| `cross_venue_consensus` | Other exchanges list the ticker at an overlapping time; each venue votes for the asset its identified listings agree on, and one asset holds at least 75% of the votes, from two or more venues. This is how Avantis, which CMC does not track, is identified. A multiplied ticker only one venue lists (GRVT's `KBONK`, Lighter's `1000NOT`) takes the consensus of its unit ticker (`BONK`, `NOT`); a letter prefix needs four characters after it, so `KNOT` is not read as 1,000 NOT. |
| `venue_reference` | The venue declares the derivative's underlying an equity, index or commodity CMC does not track (`AAL`, `NAS100`); its Atlas reference asset is reused, or minted once without a CMC ID. Venue decorations are stripped first: Extended's 24/5 sessions (`AAPL_24_5` → `AAPL`) and Avantis' won-priced Korean listings (`005930KRW` → `005930`). Forex perpetuals always take their currency pair (below). |

On Hyperliquid, HIP-3 perpetuals (`cash:ETH`, `flx:ETH`) learn from each other
but never from the main dex: deployers reuse tickers (`para:STX` is Seagate, not
Stacks).

Direct evidence (everything above `venue_lineage`) is sticky: a row keeps its match after it
delists and CMC stops listing it. Inferred matches are recomputed on every run,
so a guess never hardens into evidence.

CMC's own listings are checked before they are used:

* **Tickers must agree.** A pair whose base ticker does not resemble the Atlas
  row's is not used. This is how parser splits surface (`ARBIDR` read as
  AR/BIDR).
* **Prices must agree.** A pair is rejected when its price and CMC's aggregate
  price for the asset it names differ by more than 10x, after any contract
  multiplier. CMC files a handful of markets under a same-ticker impostor
  (Gate's DOGE perpetual under a "DOGE" token 120x cheaper, Gate's `CAT_USDT`
  Caterpillar perpetual under Simon's Cat). Genuine pairs agree to a few
  percent; the rejected ones are 13x to 10^8x off.
* **One ticker, one asset per venue.** When CMC files one live ticker on one
  venue under several assets, each of the venue's snapshots votes. An asset
  with a strict majority becomes the ticker's meaning on that venue (Gate's
  `SOL_USDC`, filed under Wrapped Solana, follows Gate's other SOL markets).
  A tie is left as CMC lists it and reported. In practice these are 1:1 twins:
  dYdX and the bridged ethDYDX.
* **Stock perpetuals are not stock tokens.** CMC files some venues' stock
  perpetuals under a tokenized-stock wrapper: Bitget's `AAPLUSDT` under Ondo's
  `AAPLON`, Extended's `ASTS-USD` under Robinhood's AST SpaceMobile token. A
  derivative whose venue declares a TradFi underlying ignores such a pair and
  resolves to the reference asset. Robinhood's and Backpack's tokens take the
  stock's own ticker, so a perpetual on `QUBT` is the stock even before any
  venue identified it (`QUBT.EQUITY`). A venue that lists a token under the
  token's own decorated ticker trades the token and keeps the pair: Gate's
  `FUTUON_USDT` is Ondo's FUTUON, its `TQQQX_USDT` the xStock. Spot markets
  trade the token itself and keep it, as do tokens a venue classes as TradFi
  that wrap no stock (tokenized gold, Binance's pre-launch `PREDICT`).
* **Delisted markets are checked twice.** CMC keeps listing some markets a
  venue has delisted: Hyperliquid keeps delisted perpetuals in its universe
  (`MKR`, `hyna:BTC`). Such a pair identifies the delisted row when no active
  row trades the ticker, but only if the asset's own ticker matches the row's,
  since CMC's base ticker for these is often just the venue's (it files
  `vntl:ANTHROPIC` under OpenAI), and only if CMC added the asset before the
  row stopped trading.

Three safeguards hold back a wrong inference:

* **Ticker reuse.** A live CMC asset is never inferred for an instrument that
  stopped trading before CMC added the asset: the ticker was reused by a later
  project. (Retired assets are exempt, because CMC re-dates them.)
* **Wrappers.** A derivative whose venue declares a TradFi underlying is never
  inferred to be a token that wraps it (an Ondo stock token); it gets the
  reference asset. Forex and pre-IPO (`pre_market`) contracts are never
  inferred to be a same-ticker token either. Pre-IPO contracts get no Atlas
  reference asset of their own (venues spell them `ANTHROPIC`, `ANTHROP`,
  `ANTH`); CMC's references and curated pins identify them. Venues' own test
  listings (`TEST002`) get no asset at all.
* **Withdrawn evidence.** When today's price check rejects the CMC listing a
  row's `cmc_id` came from, and nothing else identifies the row, the build
  drops that `cmc_id`. A `cmc_id` that predates symbology is never dropped.
* **Conflicts.** Inference needs unanimity. If identified listings disagree
  (Tesla the stock and a Tesla token both trade as `TSLA`), the row is left
  unresolved and reported.
* **Legacy IDs.** A `cmc_id` of unknown provenance yields when every strongly
  identified listing of the ticker on the venue at the same time names another
  asset. The first build found four wrong legacy IDs. CMC's own listings
  corrected `binance-futures` `apeusdt` (APEcoin.dev → ApeCoin) and `avaaiusdt`
  (avaai.top → AVA). Its delisted sibling `apebusd` then followed through
  lineage. `binance-spot` `arbidr` was a parser split (AR/BIDR → ARB/IDR) that
  venue-reported base/quote fields now fix.

### Ticker eras

When a ticker passes to a successor, exchanges usually rename the old asset's
live listings while delisted rows keep the old ticker. A *ticker era* in
`overrides.json` records that the ticker meant another asset until a date, on
every venue:

```json
{"ticker": "LUNA", "cmc_id": 4172, "until": "2022-05-28T00:00:00Z", "reason": "..."}
```

Rows that stopped trading by then get the old asset (`LUNC`). Rows that traded
across the date are identified by what the ticker means afterwards (`LUNA`,
Terra 2.0), and the old asset is recorded in `base_asset_history`, so
`SecurityMaster.base_asset(exchange, id, at=...)` answers correctly for any
date.

### Venue tickers

Some tickers belong to different assets on different venues, and the evidence
from one venue would mislead another. A *venue ticker* in `overrides.json` pins
what a ticker means on one venue family, as base, quote and margin:

```json
{"family": "binance", "ticker": "USDP", "cmc_id": 3330, "reason": "..."}
```

Binance and OKX relisted Paxos's PAX as `USDP` (Pax Dollar) after the 2021
rename, while Gate's `USDP` is the unrelated USDP Stablecoin (`USDP.USDP`).
Venue tickers beat every vote and market pair, but a per-row override still
wins.

The DeFi venues need a few. CMC files `MON` on ApeX, Extended, GRVT and
Pacifica under MoneyByte, a 2014 coin whose CMC price these misfiled markets
set, so no price check catches it; they are Monad. Venues also spell some
references their own way: ApeX `ORACLE` is `ORCL`, Variational `VISA` is `V`,
Avantis `BRENT` and Extended `XBR` are Brent (`BZ`), Extended `XNG` is natural
gas and `ANTHROP` is Anthropic.

### Forex pairs

A forex perpetual tracks an exchange rate, so its base asset is the currency
pair in market convention: a `forex` asset such as `EURUSD` or `USDJPY`.
Venues spell one rate as the base currency (`EUR`), the pair (`EURUSD`), a
HIP-3 ticker (`xyz:EUR`) or, for dollar-based pairs, the quoted currency
alone. Hyperliquid's `xyz:JPY` trades near 158, so it is `USDJPY`. EUR, GBP,
AUD and NZD are quoted `<CCY>USD`; every other currency is `USD<CCY>`. When
CoinMarketCap tracks the rate (`USDBRL`), its asset is reused. Forex rows take
no other evidence: a CMC listing of the euro or a euro stablecoin is ignored.
A forex row that is not a currency pair (`DXY`) stays unresolved. Spot rows
are tokens, not the rate.

## Quote and margin assets

A quote ticker means what CMC says it means on the venue's live pairs (`USDT`
is Tether everywhere; Coinbase's `USD` is the dollar), or what the venue's own
identified base asset of that ticker is (`BTC` in `ETHBTC`). Votes are taken
per venue first, then across venues, and a winner needs 90% of them. A quote
currency no venue lists live any more resolves through its ISO code (RUB, NGN,
UAH; CMC's fiat IDs are in `FIAT_CMC_IDS`) or the venue's delisted base rows
(BUSD). The venue-wide meaning beats one pair's quote ID, which CMC
occasionally mislabels. Inverse contracts are margined in their base asset.

## Categories

The CoinMarketCap categories Atlas allow-lists are Atlas's own taxonomy now,
defined in [`atlas/categories.py`](../atlas/categories.py) and stored on each
asset: DeFi, Layer 1, Layer 2, Tokenized Stock, Real World Assets Protocols,
Staking, Play To Earn, Generative AI, Smart Contracts, DApp, Collectibles &
NFTs, Lending & Borrowing, DAO, Metaverse, Privacy, AI Agents, AI
Applications, Interoperability and Memes. CMC files Memes under its
`INDUSTRY` tag type rather than `CATEGORY`; it is the only industry tag Atlas
takes.

## Using it

```python
from datetime import datetime
from atlas import SecurityMaster

sm = SecurityMaster.load()

sm.base_asset("lighter-perps", "112")              # Asset(asset_id='TSLA', ...)
sm.base_asset("hyperliquid-perps", "xyz:JPY")      # Asset(asset_id='USDJPY', ...)
sm.instruments(underlying="forex", exchange="avantis-perps")
sm.base_asset("binance-futures", "1000pepeusdt")   # Asset(asset_id='PEPE', ...)
sm.unified_id("hyperliquid-perps", "kPEPE")        # 'perpetual-PEPE-USDC:USDC'
sm.by_unified_id("perpetual-BTC-USDT:USDT")        # [('binance-futures', 'btcusdt'), ...]
sm.instruments_for_asset("SOL", exchange="okx-spot")
sm.exchanges_for_asset("TSLA")
sm.assets_in_category("Memes")
sm.find_assets("NEIRO")                            # every asset trading as NEIRO
sm.base_asset("binance-spot", "lunausdt", at=datetime(2022, 1, 1))  # LUNC
```

## Building it

```bash
# Fetch CMC market pairs for the 14 venues CMC tracks, fetch details for new assets, write
python -m integrations.symbology --report-path report.md

# Report only
python -m integrations.symbology --dry-run --report-path report.md

# No network: rebuild from stored evidence, the ledger and overrides
python -m integrations.symbology --offline

# Refetch every asset's CMC details (names, tickers, categories)
python -m integrations.symbology --refresh-details
```

`--pairs-cache DIR` and `--details-cache FILE` keep CMC responses on disk;
`--use-pairs-cache` reads market pairs back from `DIR` instead of fetching.

The `daily-update` workflow runs the build after refreshing the snapshots, and
`symbology-refresh` refetches every asset's details weekly. The report lists
coverage per snapshot, every evidence disagreement and reassignment, CMC pairs
whose ticker disagrees with the Atlas row (usually a parser split), inference
conflicts, rejected ticker reuse and the active rows still without an asset.

Supported snapshots: `binance-spot`, `binance-futures`, `binance-futures-cm`,
`bybit-spot`, `bybit-perps-usd`, `bybit-perps-coin`,
`bybit-futures`, `okx-spot`, `okx-perps`,
`okx-futures`, `hyperliquid-spot`, `hyperliquid-perps`, `bitget-spot`,
`bitget-perps-usdt`, `bitget-perps-usdc`, `bitget-perps-coin`,
`bitget-futures-coin`, `gateio-spot`, `gateio-perps-usdt`, `gateio-perps-btc`,
`gateio-futures-usdt`, `coinbase-spot`, and the DeFi / RWA perpetual venues
`aster-perps`, `avantis-perps`, `variational-perps`, `lighter-perps`,
`grvt-perps`, `extended-perps`, `apex-perps` and `pacifica-perps`. Each DeFi
venue is its own ticker namespace. CMC tracks all of them but Avantis (as
`aster-pro`, `lighter`, `grvt`, `extended`, `apex-omni`, `pacifica`,
`variational`).

## Coverage

The build of 29 October 2026 holds 2,788 assets: 2,066 crypto, 604 equity,
53 index, 27 commodity, 25 fiat and 13 forex. It identifies the base asset
of 24,663 of 34,339 parsed instrument rows (12,162 from CMC market pairs), and
the quote asset of 33,845 of them.

| Snapshot | Active rows | Active base resolved |
| --- | ---: | ---: |
| `aster-perps` | 602 | 100.0% |
| `avantis-perps` | 175 | 97.1% |
| `variational-perps` | 566 | 98.9% |
| `lighter-perps` | 213 | 98.6% |
| `grvt-perps` | 184 | 100.0% |
| `extended-perps` | 354 | 99.4% |
| `apex-perps` | 130 | 100.0% |
| `pacifica-perps` | 78 | 100.0% |
| `hyperliquid-perps` | 348 | 96.8% |
| `hyperliquid-spot` | 347 | 36.0% |
| `binance-spot` | 1,385 | 100.0% |
| `binance-futures` | 799 | 100.0% |
| `binance-futures-cm` | 30 | 100.0% |
| `bybit-spot` | 527 | 99.6% |
| `bybit-perps-usd` | 888 | 99.9% |
| `bybit-perps-coin` | 22 | 100.0% |
| `bybit-futures` | 44 | 100.0% |
| `okx-spot` | 1,212 | 91.0% |
| `okx-perps` | 526 | 99.8% |
| `okx-futures` | 285 | 99.6% |
| `coinbase-spot` | 517 | 99.4% |
| `bitget-spot` | 3,594 | 17.9% |
| `bitget-perps-usdt` | 866 | 97.6% |
| `bitget-perps-usdc` | 49 | 100.0% |
| `bitget-perps-coin` | 20 | 100.0% |
| `bitget-futures-coin` | 4 | 100.0% |
| `gateio-spot` | 2,187 | 73.5% |
| `gateio-perps-usdt` | 1,066 | 97.6% |
| `gateio-perps-btc` | 1 | 100.0% |
| `gateio-futures-usdt` | 30 | 100.0% |

The gaps are assets CMC does not track: Bitget's 3,000 tokenized stocks, Gate's
leveraged tokens (`ADA3L`, `ADA3S`), Hyperliquid's HIP-1 spot tokens and
HIP-3 pre-IPO markets, OKX's xStocks, Avantis' structured `*_UPSIDE` markets
and a handful of pre-IPO names only one venue lists. `tests/test_symbology_data.py`
holds each snapshot's coverage to a floor just below these numbers. The same
test module also checks that the bundled data is consistent and that an
offline rebuild reproduces it exactly.

## Limitations

* CMC lists mostly live markets, so a delisted instrument is identified only
  through evidence recorded while it traded, a market CMC still lists, a venue
  lineage or a cross-venue consensus. Tickers no other venue identified while
  they traded need a curated override; `overrides.json` holds those for the
  delisted USD perpetuals of OKX, Hyperliquid and Binance, each with its
  evidence, and `tests/test_symbology_data.py` lists the few left unresolved.
* Crypto assets CMC does not track (Hyperliquid HIP-1 tokens, Bitget's tokenized
  stocks, leveraged tokens) have no asset yet. The registry already holds assets
  without a CMC ID (the TradFi reference assets), so minting these from another
  source is the natural next step.
* Price checks need CMC's live aggregate prices. An online build that cannot
  fetch them does not use market pairs at all, so it never stores an unchecked
  pair; it keeps the evidence earlier builds stored, as an offline build does.
* `unified_id` needs all legs resolved; a derivative without a margin asset has
  none.
