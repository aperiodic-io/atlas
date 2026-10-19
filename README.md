```
      ___                                     ___           ___     
     /  /\          ___                      /  /\         /  /\    
    /  /::\        /  /\                    /  /::\       /  /:/_   
   /  /:/\:\      /  /:/    ___     ___    /  /:/\:\     /  /:/ /\  
  /  /:/~/::\    /  /:/    /__/\   /  /\  /  /:/~/::\   /  /:/ /::\ 
 /__/:/ /:/\:\  /  /::\    \  \:\ /  /:/ /__/:/ /:/\:\ /__/:/ /:/\:\
 \  \:\/:/__\/ /__/:/\:\    \  \:\  /:/  \  \:\/:/__\/ \  \:\/:/~/:/
  \  \::/      \__\/  \:\    \  \:\/:/    \  \::/       \  \::/ /:/ 
   \  \:\           \  \:\    \  \::/      \  \:\        \__\/ /:/  
    \  \:\           \__\/     \__\/        \  \:\         /__/:/   
     \__\/                                   \__\/         \__\/    
     
```

# Atlas - Crypto Security Master

> Built and maintained by **[Aperiodic](https://aperiodic.io/?utm_source=github&utm_medium=readme&utm_campaign=atlas)**. Atlas is the open-source securities master behind Aperiodic's crypto market data and research. [Check out Aperiodic →](https://aperiodic.io/?utm_source=github&utm_medium=readme&utm_campaign=atlas)

Atlas - Crypto Security Master is a Python securities master for crypto venues. It normalizes exchange-native instrument IDs into a consistent `Contract` model and builds a fast lookup map (`internal_id`) from precomputed JSON snapshots.

**Daily updates:** snapshot JSONs are refreshed automatically every day via GitHub Actions, and committed back to this repository.

## What It Does

- Normalizes instrument metadata across exchanges into:
  - `symbol` (base)
  - `denominator` (quote / settlement)
  - `margin` (for derivatives)
  - `type` (`spot`, `perpetual`, `future`, `option`, `unknown`)
  - `underlying` (`crypto`, `commodity`, `equity`, `index`, `forex`, `pre_market`, `unknown`), when supplied by the venue
  - `contract_size` (when available from source metadata)
  - `delivery_date` (for dated contracts)
  - `internal_id` string
- Loads local securities-master snapshots and supports:
  - lookup by `(exchange, original_id)` -> `internal_id`
  - active-symbol filtering by date window
- Refreshes snapshot data from:
  - exchange APIs (default; Binance, Bybit, Hyperliquid, OKX)
  - Tardis
  - hybrid mode (exchange API overlaid on Tardis)

## Symbology

Atlas uses a normalized `internal_id` format:

`<type>-<symbol>-<denominator>[:<margin>][-<delivery_yyyymmdd>]`

- Spot omits margin and delivery date.
- Perpetual includes margin, but no delivery date.
- Dated futures include both margin and delivery date.

Examples:

- Spot: `btcusdt` (Binance spot) -> `spot-BTC-USDT`
- Perpetual: `btcusdt` (Binance futures) -> `perpetual-BTC-USDT:USDT`
- Future: `BTCUSDT-27MAR26` (Bybit futures) -> `future-BTC-USDT:USDT-20260327`
- Hyperliquid HIP-3 perpetual: `flx:TSLA` -> `perpetual-FLX:TSLA-USDH:USDH` (the `<dex>:` prefix stays in the symbol; denominator and margin are the dex's collateral token, e.g. USDC, USDH, USDE or USDT0)
- Lighter perpetual: market `96` (`EURUSD`) -> `perpetual-EUR-USDC:USDC` (the numeric market id is the original id; a USD-quoted FX pair is named by its base, other pairs keep the full ticker: `USDJPY` -> `perpetual-USDJPY-USDC:USDC`)
- Extended perpetual: `SPX500m-USD` -> `perpetual-SPX500M-USD:USD` (the asset as Extended names it, settled in its USD collateral)

### Unified symbology: Atlas asset IDs

`internal_id` is built from each venue's own tickers. On top of it, Atlas gives
every **asset** one stable, descriptive string ID of its own -- `BTC`, `ETH`,
`USDT`, `USD`, `PEPE`, `TSLA` -- and points every instrument leg at it:

| Field | Example (`1000PEPEUSDT` on Binance futures) |
| --- | --- |
| `base_asset_id` | `PEPE` |
| `base_multiplier` | `1000` |
| `quote_asset_id` | `USDT` |
| `margin_asset_id` | `USDT` |
| `unified_id` | `perpetual-PEPE-USDT:USDT` (the same on Bybit, OKX, Bitget and Gate; `kPEPE` on Hyperliquid is `perpetual-PEPE-USDC:USDC`) |
| `base_asset_match` | `cmc_market_pair` (how the asset was identified) |

Asset IDs are the bare ticker when it is free, and `<TICKER>.<QUALIFIER>` when
another asset already holds the ticker (`TSLA` is Tesla the stock that
perpetuals track; Hyperliquid's tokenized Tesla is
`TSLA.TESLA_TOKENIZED_STOCK_HYPERLIQUID`). Once minted, an ID never changes. The
registry, [`atlas/data/symbology/assets.json`](atlas/data/symbology/assets.json),
records each asset's name, class (`crypto`, `fiat`, `equity`, `commodity`,
`index`), categories and CoinMarketCap ID. The CMC ID is an attribute, not the
identity.

Identification is seeded from CoinMarketCap's own exchange listings across
Binance, OKX, Bybit, Hyperliquid, Bitget, Gate and Coinbase. Each venue ticker
CMC tracks comes with its base and quote assets. Delisted rows are resolved
through overlapping listings on the same venue, then a unanimous consensus
across venues, and are guarded against ticker reuse. See
[`docs/symbology.md`](docs/symbology.md).

```python
from datetime import datetime
from atlas import SecurityMaster

sm = SecurityMaster.load()
sm.base_asset("binance-futures", "1000pepeusdt").asset_id   # 'PEPE'
sm.by_unified_id("perpetual-BTC-USDT:USDT")                 # every venue's BTC/USDT perp
sm.assets_in_category("Memes")
sm.base_asset("binance-spot", "lunausdt", at=datetime(2022, 1, 1)).asset_id  # 'LUNC'
```

## Installation

Python 3.12+ is required.

```bash
pip install -e .
```

## Quick Start

### Parse a single symbol

```python
from atlas.parsers import parse_contract

symbol_data = {"id": "BTCUSDT", "type": "spot"}
contract = parse_contract("binance-spot", symbol_data)

print(contract.internal_id)  # spot-BTC-USDT
```

### Load the local securities master

```python
from datetime import datetime
from atlas import SecurityMaster

sm = SecurityMaster.load()

iid = sm.by_exchange_and_original_id("binance-spot", "BTCUSDT")
print(iid)

exchanges = sm.exchanges_for_original_id("BTCUSDT")
print(exchanges)

exchanges = sm.exchanges_for_contract("BTC", "USDT", "USDT")
print(exchanges)

active = sm.symbol_ids(
    exchange="binance-spot",
    first_capture=datetime(2024, 1, 1),
    end_date=datetime(2024, 1, 31),
)
print(len(active))
```

### Update local snapshots

```bash
# Exchange API only (default, used by the daily workflow)
python3 -m atlas.update --source exchange

# Hybrid mode: exchange APIs overlaid on Tardis
python3 -m atlas.update --source hybrid

# Tardis only
python3 -m atlas.update --source tardis

# Restrict to selected exchanges
python3 -m atlas.update --exchanges binance-spot,okx-spot
```

#### Listing and delisting dates

In exchange mode each fetch is treated as the exchange's whole current
universe, so `first_capture` and `end_date` come from the exchanges:

- `first_capture` is the exchange listing time where one is published
  (Binance futures and Aster `onboardDate`, Bybit derivatives and Bitget
  `launchTime`, OKX `listTime`, Gate `launch_time`, HTX `create_date`, MEXC
  `openingTime`/`createTime`, Deribit `creation_timestamp`, Extended `createdAt`,
  Lighter `created_at`, GRVT `create_time`, Pacifica `created_at`), otherwise the
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
  `delisted` end at first sighting.
- An instrument that simply disappears (Binance spot and COIN-M, Bybit spot,
  OKX, Hyperliquid spot, and every venue without a delisted status: ApeX,
  Pacifica, Variational, Deribit, WhiteBIT, Crypto.com) is marked
  `missing_since` on the first run that misses
  it. If a run at least 12 hours later still misses it, `end_date` is set to that
  first miss; if it comes back first, the mark is dropped. Expired dated futures
  end at delivery straight away.
- Halted, suspended and pre-market instruments (Binance spot `HALT`, Binance
  futures pending/delivering statuses, OKX `suspend`, Bybit
  `PreLaunch`/`Delivering`, Bitget `limit_open`/`limit_close`/`restrictedAPI`,
  HTX suspended and settling, MEXC paused and delivering, Gate `delisting` and
  `circuit_breaker`, Extended `PRELISTED`/`REDUCE_ONLY`, ApeX contracts that
  still trade or show) stay listed.
- A run that returns nothing for an exchange fails it and leaves its file
  untouched. A run that would newly drop more than 10% of an exchange's open
  instruments (and more than 5) looks like a broken API response: it still
  updates the instruments it returned, but marks nothing missing and fails the
  exchange. If the instruments really were delisted, rerun with
  `--accept-missing <exchange>` (or the `accept_missing` input when dispatching
  the daily workflow by hand).
- `missing_since` is only maintained in exchange mode; `hybrid` and `tardis`
  runs cannot see delistings, so they clear it. `SecurityMaster` ignores it, so
  a row stays tradable until its `end_date` is set.
- An existing `end_date` is never overwritten, a relisted instrument has its
  `end_date` cleared, and delisted instruments that were never on file are not
  added.

## Supported Exchanges

Atlas - Crypto Security Master currently contains parsers for the following exchange IDs.

**Stability warning:** any row marked **UNSTABLE (BETA)** is treated as beta by the codebase and is skipped by `update.py` in default runs.

| Exchange ID | Stability |
| --- | --- |
| `binance-spot` | ✅ **`STABLE`** |
| `binance-futures` | ✅ **`STABLE`** |
| `binance-futures-cm` | ✅ **`STABLE`** |
| `bitmex` | **`UNSTABLE (BETA)`** |
| `bitfinex` | **`UNSTABLE (BETA)`** |
| `bitfinex-derivatives` | **`UNSTABLE (BETA)`** |
| `bitget` | **`UNSTABLE (BETA)`** |
| `bitget-futures` | **`UNSTABLE (BETA)`** |
| `bitstamp` | **`UNSTABLE (BETA)`** |
| `bybit-spot` | ✅ **`STABLE`** |
| `apex-perps` | ✅ **`STABLE`** (venue API only) |
| `aster-perps` | ✅ **`STABLE`** |
| `bybit-perps` | ✅ **`STABLE`** |
| `bybit-futures` | ✅ **`STABLE`** |
| `coinbase` | ✅ **`STABLE`** |
| `crypto-com` | **`UNSTABLE (BETA)`** |
| `cryptofacilities` | **`UNSTABLE (BETA)`** |
| `deribit` | **`UNSTABLE (BETA)`** |
| `ftx` | **`UNSTABLE (BETA)`** |
| `gate-io` | **`UNSTABLE (BETA)`** |
| `gate-io-futures` | **`UNSTABLE (BETA)`** |
| `gemini` | **`UNSTABLE (BETA)`** |
| `grvt-perps` | ✅ **`STABLE`** |
| `huobi` | **`UNSTABLE (BETA)`** |
| `huobi-dm` | **`UNSTABLE (BETA)`** |
| `huobi-dm-swap` | **`UNSTABLE (BETA)`** |
| `huobi-dm-linear-swap` | **`UNSTABLE (BETA)`** |
| `hyperliquid-spot` | ✅ **`STABLE`** |
| `hyperliquid-perps` | ✅ **`STABLE`** |
| `kraken` | **`UNSTABLE (BETA)`** |
| `kucoin` | **`UNSTABLE (BETA)`** |
| `lighter-perps` | ✅ **`STABLE`** |
| `okx-spot` | ✅ **`STABLE`** |
| `okx-perps` | ✅ **`STABLE`** |
| `okx-futures` | ✅ **`STABLE`** |
| `extended-perps` | ✅ **`STABLE`** |
| `phemex` | **`UNSTABLE (BETA)`** |
| `poloniex` | **`UNSTABLE (BETA)`** |
| `upbit` | **`UNSTABLE (BETA)`** |
| `pacifica-perps` | ✅ **`STABLE`** (exchange API only) |
| `gateio-spot` | ✅ **`STABLE`** |
| `gateio-perps-usdt` | ✅ **`STABLE`** |
| `gateio-futures-usdt` | ✅ **`STABLE`** |
| `gateio-perps-btc` | ✅ **`STABLE`** |
| `htx-perps-usdt` | ✅ **`STABLE`** |
| `mexc-perps` | ✅ **`STABLE`** |
| `bitget-spot` | ✅ **`STABLE`** |
| `bitget-perps-usdt` | ✅ **`STABLE`** |
| `bitget-perps-usdc` | ✅ **`STABLE`** |
| `bitget-perps-coin` | ✅ **`STABLE`** |
| `bitget-futures-coin` | ✅ **`STABLE`** |
| `deribit-perps` | ✅ **`STABLE`** |
| `deribit-futures` | ✅ **`STABLE`** |
| `whitebit-perps` | ✅ **`STABLE`** |
| `cryptocom-perps` | ✅ **`STABLE`** |
| `cryptocom-futures` | ✅ **`STABLE`** |

An exchange is stable exactly when it has its own exchange-API fetcher.

### live-producers venues

The venues the live producers stream are registered under the producer's own
exchange id (`gateio-perps-usdt`, `htx-perps-usdt`, ...), so a
published record joins its Atlas row by `(exchange, internal_id)` without a
mapping table. Where Tardis carries the same instruments under the same ids,
hybrid updates merge its history through `tardis_id` (Gate's two perpetual
families share `gate-io-futures` and are split by settlement suffix; Bitget's
USDT and USDC perpetuals share `bitget-futures`). Bitget and Gate spot take the
same scheme (`bitget-spot`, `gateio-spot`, as `okx-spot`) though the producers
do not stream them yet, and merge Tardis's `bitget` and `gate-io`. The older
Tardis-named exchanges (`gate-io-futures`, `huobi-dm-linear-swap`,
`bitget-futures`, `bitget`, `gate-io`, `deribit`, `crypto-com`) remain beta
parsers. Bybit keeps its existing
`bybit-perps` / `bybit-futures` (the producer's `bybit-perps-usd` and
`bybit-perps-coin` both live in `bybit-perps`).

New rows take the venue's listing time as `first_capture` where it publishes
one, otherwise their first observation (see [Listing and delisting
dates](#listing-and-delisting-dates)).

### Underlying classification

Every exchange fetcher sets `underlying` from the venue's own metadata:
Binance `underlyingType`, Bybit `symbolType`, OKX `instCategory`, Hyperliquid
`perpCategories` (main-dex perps are crypto; HIP-3 assets the venue does not
categorise, mostly delisted, come from a curated table in
`exchange_definitions/hyperliquid.py`), Gate `contract_type` and
`is_pre_market`, HTX `tradfi_labels`, MEXC trade zones (`conceptPlate`),
Bitget `symbolType`/`isRwa`, Deribit `underlying_type`, WhiteBIT TradFi
membership of `/markets`, Crypto.com `product_type`. Conventions across
venues: ETFs are `equity`; pre-IPO companies are `pre_market`; crypto and
cash indices, and treasury yields, are `index`; a venue value Atlas does not
know is `unknown`, never `crypto`. Venues disagree on tokenized gold
(PAXG, XAUT): Binance and Bybit call it crypto, MEXC and Bitget commodity;
Atlas follows each venue. Rows only Tardis knows (delisted before a fetcher
saw them) carry no `underlying`.
| `avantis-perps` | ✅ **`STABLE`** (exchange API only) |

Avantis (Veranta) is an oracle-priced RWA perp venue that Tardis does not
cover: its rows come from the venue's own API only, and each row's `underlying`
comes from the venue's category metadata (the price-feed asset type, falling
back to the pair group). Its `original_id` is what the venue addresses a market
by (the pair index), and a non-USD quote is kept in the symbol (`USDJPY`,
`005930KRW`). Avantis settles in USDC. See the module docstring in
`atlas/exchange_definitions/avantis.py`.

### Bundled Snapshot Coverage

The repository currently ships precomputed JSON snapshots for:

- `apex-perps`
- `aster-perps`
- `binance-spot`
- `binance-futures`
- `binance-futures-cm`
- `bybit-spot`
- `bybit-perps`
- `bybit-futures`
- `grvt-perps` (venue API only; `first_capture` is the venue listing time)
- `coinbase` (spot)
- `hyperliquid-spot`
- `hyperliquid-perps`
- `lighter-perps`
- `okx-spot`
- `okx-perps`
- `okx-futures`
- `pacifica-perps` (DeFi perpetual venue Tardis does not cover: listed from
  the venue's own API, with the settlement token as denominator and margin, the
  venue's listing time as `first_capture`, and `underlying` from the venue's
  market categories)
- `gateio-spot`
- `gateio-perps-usdt`
- `gateio-futures-usdt`
- `gateio-perps-btc`
- `htx-perps-usdt`
- `mexc-perps`
- `bitget-spot`
- `bitget-perps-usdt`
- `bitget-perps-usdc`
- `bitget-perps-coin`
- `bitget-futures-coin`
- `deribit-perps`
- `deribit-futures`
- `whitebit-perps`
- `cryptocom-perps`
- `cryptocom-futures`
- `extended-perps`
- `avantis-perps`

## Features

- Unified parser layer with exchange-specific symbol rules.
- Stable-vs-beta exchange classification (beta exchanges are skipped in `update.py` by default). An exchange read from its own API is stable.
- Venues Tardis does not list (`on_tardis=False`, e.g. ApeX, GRVT) are skipped by `--source tardis` and read from their API by `--source hybrid`; a new row's `first_capture` is Atlas's first observation.
- Exchange ID mapping to Tardis IDs (`to_tardis_exchange_id`).
- Metadata merge logic that preserves previously-known fields when the current source omits them.
- Date-window filtering over locally stored symbol availability intervals.
- CoinMarketCap ID enrichment for newly listed Binance assets, with confident matches written to snapshots and ambiguous ones raised for review.
- Unified symbology: Atlas-minted asset IDs for every instrument's base, quote and margin assets, plus a venue-independent `unified_id`, across the top 7 exchanges.
- Atlas asset categories (promoted from CoinMarketCap's allow-listed tags, including Memes) on every asset.

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
[`docs/cmc-id-mapping.md`](docs/cmc-id-mapping.md) for the decision ladder,
configuration and review process.

## Limitations

- No options mapping in the securities master map:
  - `update.py` ingests only `spot`, `perpetual`, and `future` from source metadata.
  - Option contracts may parse for some exchanges (for example, Deribit), but they are not included in the precomputed `internal_id` map.
- Instruments without a derived `internal_id` (for example options, combos, or unrecognized formats) are intentionally absent from `SecurityMaster` lookups.
- `--source exchange` only works for exchanges that have direct API fetchers implemented (the stable exchanges above).
- `--source tardis` skips exchanges Tardis does not cover (the DeFi venues, `mexc-perps`, `whitebit-perps`, `gateio-futures-usdt`, `bitget-perps-coin`, `bitget-futures-coin`) rather than writing an empty snapshot.
- The bundled data is limited to the snapshot files present in `atlas/data`; all other exchange coverage depends on running updates with an available upstream source.

## Project Layout

- `atlas/contracts.py`: normalized `Contract` model.
- `atlas/exchanges.py`: exchange registry (parser + source metadata).
- `atlas/parsers.py`: parse entrypoint (`parse_contract`).
- `atlas/database.py`: load/query local securities master snapshots.
- `atlas/update.py`: snapshot updater CLI.
- `atlas/data/*.json`: generated per-exchange snapshot files.
- `integrations/cmc_new_symbol_mapping.py`: CMC ID resolution for new Binance symbols.
- `integrations/cmc_mappings.py`: versioned per-instrument CMC decision store.
- `atlas/assets.py`: the asset registry and asset-ID minting.
- `atlas/categories.py`: the Atlas asset category taxonomy.
- `atlas/data/symbology/assets.json`: the asset registry; `overrides.json` holds curated decisions and ticker eras.
- `integrations/cmc_market_pairs.py`: CoinMarketCap exchange market pairs and their match to Atlas rows.
- `integrations/cmc_asset_details.py`: per-asset CoinMarketCap metadata (names, tickers, categories).
- `integrations/symbology.py`: the unified symbology build.

## Testing

```bash
pytest tests/
```

## About Aperiodic

Atlas is open-sourced by [Aperiodic](https://aperiodic.io/?utm_source=github&utm_medium=readme&utm_campaign=atlas). If Atlas is useful to you, take a look at what else we build at [aperiodic.io](https://aperiodic.io/?utm_source=github&utm_medium=readme&utm_campaign=atlas), and give this repo a ⭐ so more people find it.

## License

MIT (see `LICENSE`).
