# Unified symbology

Atlas gives every asset one stable, descriptive string ID and points every leg
of every instrument at it, so the same asset reads the same on every exchange:
Binance `btcusdt`, OKX `BTC-USDT`, Coinbase `BTC-USD` and Hyperliquid `BTC` all
have the base asset `BTC`. Two different assets that share a ticker never share
an ID.

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

`asset_class` is one of `crypto`, `fiat`, `equity`, `commodity`, `index`,
`unknown`. Equities, commodities and indices are the *reference assets* that
TradFi perpetuals track: Tesla perpetuals on Binance, Bybit, OKX, Gate and
Hyperliquid all have base asset `TSLA` (an `equity`), and Binance's `XAUUSDT`
has gold (a `commodity`). Tokenized wrappers are separate `crypto` assets.

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

`unified_id` ignores the multiplier: it groups economically identical contracts
across venues. Use `(exchange, original_id)` when you need a per-venue key.

## How base assets are identified

Each row's base asset is resolved from the strongest evidence available;
`base_asset_match` records which.

| Method | Evidence |
| --- | --- |
| `override` | A curated decision in [`overrides.json`](../atlas/data/symbology/overrides.json), including ticker eras (below). |
| `cmc_ledger` | An approved decision in the CMC mapping ledger, `atlas/data/cmc_mappings.json` (manual approvals rank above everything but overrides). |
| `cmc_market_pair` | CoinMarketCap's own exchange listing: for every market it tracks on a venue, CMC publishes the venue's native ticker next to the base and quote CMC IDs. Atlas finds its row for that ticker and checks that the tickers agree. |
| `venue_majority` | CMC listed this row under another asset than the venue's other markets of the ticker, and the venue's majority decided (see below). The same label marks a legacy `cmc_id` that the venue's concurrent CMC-identified listings refuted. |
| `snapshot_cmc_id` | A `cmc_id` the row carried from an earlier mapping run. |
| `venue_lineage` | The same ticker on the same exchange, listed at an overlapping time, is identified, and every such listing agrees. An exchange does not list two assets under one ticker at once. |
| `cross_venue_consensus` | Other exchanges list the ticker at an overlapping time; each venue votes for the asset its identified listings agree on, and one asset holds at least 75% of the votes, from two or more venues. |
| `venue_reference` | The venue declares the derivative's underlying an equity, index or commodity CMC does not track (`AAL`, `NAS100`); its Atlas reference asset is reused, or minted once without a CMC ID. |

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

Three safeguards hold back a wrong inference:

* **Ticker reuse.** A live CMC asset is never inferred for an instrument that
  stopped trading before CMC added the asset: the ticker was reused by a later
  project. (Retired assets are exempt, because CMC re-dates them.)
* **Wrappers.** A derivative whose venue declares a TradFi underlying is never
  inferred to be a token that wraps it (an Ondo stock token); it gets the
  reference asset.
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
# Fetch CMC market pairs for the 7 venues, fetch details for new assets, write
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
`bybit-spot`, `bybit-perps`, `bybit-futures`, `okx-spot`, `okx-perps`,
`okx-futures`, `hyperliquid-spot`, `hyperliquid-perps`, `bitget`,
`bitget-futures`, `gate-io`, `gate-io-futures` and `coinbase`.

## Coverage

The first build, on 14 October 2026, minted 2,550 assets: 1,983 crypto,
504 equity, 25 fiat, 20 index, 14 commodity and 4 unknown. It identified the
base asset of 20,992 of 31,837 instrument rows (10,019 from CMC market pairs), and
the quote asset of 31,082 of them.

| Snapshot | Active rows | Active base resolved |
| --- | ---: | ---: |
| `binance-spot` | 1,380 | 100.0% |
| `binance-futures` | 794 | 100.0% |
| `binance-futures-cm` | 30 | 100.0% |
| `bybit-spot` | 528 | 99.6% |
| `bybit-perps` | 894 | 98.0% |
| `bybit-futures` | 44 | 100.0% |
| `okx-spot` | 1,171 | 93.5% |
| `okx-perps` | 501 | 97.8% |
| `okx-futures` | 274 | 97.8% |
| `coinbase` | 515 | 99.2% |
| `bitget` | 3,464 | 18.5% |
| `bitget-futures` | 906 | 98.3% |
| `gate-io` | 2,200 | 73.7% |
| `gate-io-futures` | 1,070 | 97.4% |
| `hyperliquid-perps` | 396 | 79.3% |
| `hyperliquid-spot` | 331 | 35.6% |

The gaps are assets CMC does not track: Bitget's 2,800 tokenized stocks, Gate's
leveraged tokens (`ADA3L`, `ADA3S`), Hyperliquid's HIP-1 spot tokens and
HIP-3 pre-IPO markets, and OKX's xStocks. `tests/test_symbology_data.py` holds
each snapshot's coverage to a floor just below these numbers. The same test
module also checks that the bundled data is consistent and that an offline
rebuild reproduces it exactly.

## Limitations

* CMC lists only live markets, so a delisted instrument is identified only
  through evidence recorded while it traded, a venue lineage or a cross-venue
  consensus. Long-dead tickers no live listing shares stay unresolved.
* Crypto assets CMC does not track (Hyperliquid HIP-1 tokens, Bitget's tokenized
  stocks, leveraged tokens) have no asset yet. The registry already holds assets
  without a CMC ID (the TradFi reference assets), so minting these from another
  source is the natural next step.
* Price checks need CMC's live aggregate prices. An online build that cannot
  fetch them does not use market pairs at all, so it never stores an unchecked
  pair; it keeps the evidence earlier builds stored, as an offline build does.
* `unified_id` needs all legs resolved; a derivative without a margin asset has
  none.
