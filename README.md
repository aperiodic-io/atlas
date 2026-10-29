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

# Atlas: the open security master for real-world-asset and crypto perpetuals

> Built and maintained by **[Aperiodic](https://aperiodic.io/?utm_source=github&utm_medium=readme&utm_campaign=atlas)**. Atlas is the open-source security master behind Aperiodic's RWA and crypto market data. [Check out Aperiodic →](https://aperiodic.io/?utm_source=github&utm_medium=readme&utm_campaign=atlas)

**[Search the catalogue →](https://aperiodic-io.github.io/atlas/)**

Stocks, gold, oil, FX, indices and pre-IPO companies now trade as perpetuals on
DeFi venues like Hyperliquid, Avantis, Lighter, GRVT, Aster, Variational,
Extended, ApeX and Pacifica, and on centralised exchanges like Binance, Bybit,
OKX, Bitget and Gate. No two venues name a market the same way. Atlas gives
every contract one ID and every underlying asset one ID, the same on every
venue, and keeps the contracts that were delisted.

- **7,000+ live RWA contracts**: equities, commodities, FX, indices and pre-IPO,
  next to every crypto spot, perpetual and futures market.
- **57,000+ instruments** on 28 venues (56 snapshots), **30,000+ of them
  delisted** with the date they stopped trading.
- **One asset registry**: `TSLA` is Tesla on all 15 venues that list it.
- **Plain JSON in git, refreshed daily** from each venue's own API. No key, no
  network at lookup time.

## Install

Python 3.12+.

```bash
pip install git+https://github.com/aperiodic-io/atlas
```

The snapshots ship with the package, so every lookup is a local dictionary read.

## What it does, in five examples

### 1. One Tesla, nine spellings

Each venue names its Tesla perpetual differently. Lighter and Avantis use a
bare number:

| Venue | Native ID | Atlas ID (`internal_id`) | Atlas asset |
| --- | --- | --- | --- |
| Hyperliquid (HIP-3) | `xyz:TSLA` | `perpetual-XYZ:TSLA-USDC:USDC` | `TSLA` |
| Lighter | `112` | `perpetual-TSLA-USDC:USDC` | `TSLA` |
| Avantis | `86` | `perpetual-TSLA-USDC:USDC` | `TSLA` |
| Aster | `tslausdt` | `perpetual-TSLA-USDT:USDT` | `TSLA` |
| GRVT | `TSLA_USDT_Perp` | `perpetual-TSLA-USDT:USDT` | `TSLA` |
| Binance | `tslausdt` | `perpetual-TSLA-USDT:USDT` | `TSLA` |

```python
from atlas import SecurityMaster

sm = SecurityMaster.load()

sm.base_asset("lighter-perps", "112").asset_id        # 'TSLA'
sm.base_asset("avantis-perps", "86").asset_id         # 'TSLA'
sm.asset("TSLA")                                      # Tesla (Derivatives), equity
sm.exchanges_for_asset("TSLA")                        # 15 venues, DEX and CEX
sm.by_unified_id("perpetual-TSLA-USDC:USDC")          # every USDC-settled Tesla perp:
# [('avantis-perps', '86'), ('hyperliquid-perps', 'mkts:TSLA'),
#  ('hyperliquid-perps', 'xyz:TSLA'), ('lighter-perps', '112'),
#  ('pacifica-perps', 'TSLA'), ('variational-perps', 'TSLA')]
```

### 2. A ticker is not an asset

`SPX` is the S&P 500 to an equity trader, and SPX6900, a memecoin, on Aster,
Lighter, ApeX, Extended and Variational. `MON` on four DEXs is filed by
CoinMarketCap under a 2014 coin called MoneyByte; it is Monad. Atlas resolves
identity from evidence (venue metadata, CoinMarketCap's own exchange listings,
price checks, cross-venue consensus, curated pins), never from the ticker alone:

```python
sm.base_asset("aster-perps", "spxusdt").name          # 'SPX6900'
sm.base_asset("hyperliquid-perps", "xyz:SP500").asset_class   # AssetClass.index
sm.base_asset("extended-perps", "MON-USD").name       # 'Monad'
sm.base_asset("bitget-perps-usdt", "AAPLUSDT").asset_id
# 'AAPL': the stock, not Ondo's AAPLON token that CMC files the market under
```

### 3. Every RWA contract trading on a given day

Filter by the asset class the venue declares, by venue, and by listing window:

```python
from datetime import datetime

rwa = sm.instruments(
    underlying=["equity", "commodity", "forex", "index", "pre_market"],
    active_at=datetime(2026, 10, 29),
)
len(rwa)                                              # 7,493

sm.instruments(exchange="lighter-perps", underlying="forex")
# EURUSD, GBPUSD, AUDUSD, NZDUSD, USDJPY, USDCAD, USDCHF, USDHKD, USDKRW perps
```

### 4. FX has conventions; Atlas keeps them

Venues write the euro as `EUR`, `EURUSD`, `EUR-USD` or `xyz:EUR`, and the yen
as `USDJPY` or a bare `JPY` quoted at ~158 yen per dollar. Atlas maps each to
one currency-pair asset in market convention:

```python
sm.base_asset("pacifica-perps", "EURUSD").asset_id    # 'EURUSD'
sm.base_asset("hyperliquid-perps", "xyz:JPY").asset_id  # 'USDJPY'
sm.instruments_for_asset("EURUSD")                    # Avantis, Bybit, Extended, Gate,
                                                      # Hyperliquid, Lighter, Pacifica
```

### 5. Backtests that remember the dead

Exchange APIs only list what trades today. Atlas keeps every delisted contract
with its listing window, including 280 delisted RWA contracts such as Lighter's
SpaceX pre-IPO perpetual (8 May to 19 June 2026):

```python
sm.instrument_metadata("lighter-perps", "173")
# {'symbol': 'SPACEX', 'underlying': 'pre_market',
#  'first_capture': '2026-05-08T00:00:00.000Z', 'end_date': '2026-06-19T00:00:00.000Z', ...}

# Every Aster market that traded at any point in September 2026
sm.symbol_ids("aster-perps", datetime(2026, 9, 1), datetime(2026, 9, 30))
```

## The data model

Every instrument row in `atlas/data/<venue>.json` carries:

| Field | Example (Lighter market `112`) | |
| --- | --- | --- |
| `id` | `112` | the venue's native ID |
| `internal_id` | `perpetual-TSLA-USDC:USDC` | `<type>-<symbol>-<denominator>[:<margin>][-<yyyymmdd>]` in the venue's tickers |
| `underlying` | `equity` | `equity`, `commodity`, `forex`, `index`, `pre_market`, `crypto` or `unknown`, as the venue declares it |
| `base_asset_id` | `TSLA` | the Atlas asset the contract tracks |
| `quote_asset_id`, `margin_asset_id` | `USDC`, `USDC` | the quote and settlement assets |
| `unified_id` | `perpetual-TSLA-USDC:USDC` | `internal_id` written in Atlas asset IDs: the same contract reads the same on every venue |
| `base_multiplier` | (`1000` for `kBONK`) | units of the asset per unit of the ticker |
| `contract_size`, `delivery_date` | `1.0` | contract specs |
| `first_capture`, `end_date` | `2026-04-17T…`, — | the listing window; `end_date` only once delisted |

`internal_id` types are `spot`, `perpetual` and `future`. Spot has no margin;
dated futures add the delivery date (`future-BTC-USDT:USDT-20260327`).

The asset registry, [`atlas/data/symbology/assets.json`](atlas/data/symbology/assets.json),
holds 2,700+ assets. Each has a stable ID (`TSLA`, `XAU`, `EURUSD`, `BTC`), a
name, a class (`equity`, `commodity`, `index`, `forex`, `crypto`, `fiat`), its
CoinMarketCap ID where CMC tracks it, and Atlas categories. IDs never change
once minted; a ticker two assets share gets a qualified ID
(`TSLA.TESLA_TOKENIZED_STOCK_HYPERLIQUID` is Hyperliquid's Tesla token, not the
stock). See [`docs/symbology.md`](docs/symbology.md).

## Coverage

**DeFi / RWA venues:** Hyperliquid (perps incl. HIP-3 dexes, spot), Avantis,
Lighter, GRVT, Aster, Variational, Extended, ApeX Omni, Pacifica.

**Centralised venues:** Binance (spot, USDⓈ-M, COIN-M), Bybit, OKX, Bitget,
Gate, HTX, MEXC, Deribit, WhiteBIT, Crypto.com, Kraken, Bitfinex, Bitstamp,
Gemini, KuCoin, Phemex, Poloniex, Upbit, Coinbase.

The venue slugs match [live-producers](https://github.com/aperiodic-io/live-producers),
so a streamed record joins its Atlas row by `(exchange, original_id)`.
[`docs/venues.md`](docs/venues.md) lists every snapshot, each venue's ID
scheme, and where its listing dates and asset classes come from.

## How it stays current

Every day at 02:00 UTC a GitHub Action:

1. **Fetches** each venue's whole instrument list from its own API.
2. **Merges** it: new listings get Atlas IDs, delistings an `end_date`, and a
   venue whose response looks broken is left untouched.
3. **Resolves symbology**: every row's base, quote and margin asset, from
   CoinMarketCap's exchange listings, the venue's declared asset class,
   cross-venue consensus and curated overrides
   ([`overrides.json`](atlas/data/symbology/overrides.json)).
4. **Commits** the snapshots, and republishes the
   [catalogue](https://aperiodic-io.github.io/atlas/) and its raw JSON.

```bash
python -m atlas.update                         # refresh every venue
python -m atlas.update --exchanges lighter-perps,avantis-perps
python -m integrations.symbology --report-path report.md
python site/build.py --data atlas/data --out _site   # build the catalogue site
```

## Documentation

- [`docs/symbology.md`](docs/symbology.md): asset IDs, `unified_id`, and how each
  asset is identified.
- [`docs/venues.md`](docs/venues.md): venues, listing and delisting dates,
  asset-class sources, CoinMarketCap IDs, limitations.
- [`docs/cmc-id-mapping.md`](docs/cmc-id-mapping.md): the CoinMarketCap ID ledger
  for new crypto listings.
- [`docs/web-domain-map.md`](docs/web-domain-map.md): each asset's verified website
  and Wikipedia article.

## Project layout

- `atlas/`: the library: `SecurityMaster` (`database.py`), the asset registry
  (`assets.py`), one fetcher and parser per venue (`exchange_definitions/`), and
  the updater (`update.py`).
- `atlas/data/`: the snapshots, the asset registry and the curated overrides.
- `integrations/`: symbology (`symbology.py`, `cmc_market_pairs.py`), CMC ID
  mapping, the web domain map.
- `site/`: the GitHub Pages catalogue, built from the snapshots.

```bash
pytest tests/
```

## About Aperiodic

Atlas is open-sourced by [Aperiodic](https://aperiodic.io/?utm_source=github&utm_medium=readme&utm_campaign=atlas). If Atlas is useful to you, take a look at what else we build at [aperiodic.io](https://aperiodic.io/?utm_source=github&utm_medium=readme&utm_campaign=atlas), and give this repo a ⭐ so more people find it.

## License

MIT (see `LICENSE`).
