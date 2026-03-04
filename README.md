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

Atlas - Crypto Security Master is a Python securities master for crypto venues. It normalizes exchange-native instrument IDs into a consistent `Contract` model and builds a fast lookup map (`internal_id`) from precomputed JSON snapshots.

**Daily updates:** snapshot JSONs are refreshed automatically every day via GitHub Actions, and committed back to this repository.

## What It Does

- Normalizes instrument metadata across exchanges into:
  - `symbol` (base)
  - `denominator` (quote / settlement)
  - `margin` (for derivatives)
  - `contract_type` (`spot`, `perpetual`, `future`, `option`, `unknown`)
  - `delivery_date` (for dated contracts)
  - `internal_id` string
- Loads local securities-master snapshots and supports:
  - lookup by `(exchange, original_id)` -> `internal_id`
  - active-symbol filtering by date window
- Refreshes snapshot data from:
  - exchange APIs (where supported)
  - Tardis
  - hybrid mode (exchange API first, Tardis fallback)

## Symbology

Atlas uses a normalized `internal_id` format:

`<contract_type>-<symbol>-<denominator>[:<margin>][-<delivery_yyyymmdd>]`

- Spot omits margin and delivery date.
- Perpetual includes margin, but no delivery date.
- Dated futures include both margin and delivery date.

Examples:

- Spot: `btcusdt` (Binance spot) -> `spot-BTC-USDT`
- Perpetual: `btcusdt` (Binance futures) -> `perpetual-BTC-USDT:USDT`
- Future: `BTCUSDT-27MAR26` (Bybit futures) -> `future-BTC-USDT:USDT-20260327`

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
# Hybrid mode (default): Binance/OKX direct API + Tardis fallback
python3 atlas/update.py --source hybrid

# Tardis only
python3 atlas/update.py --source tardis

# Exchange API only (supported exchanges only)
python3 atlas/update.py --source exchange

# Restrict to selected exchanges
python3 atlas/update.py --exchanges binance-spot,okx-spot
```

### Populate ARC metadata

```bash
# Dry-run (prints match stats, does not modify files)
python3 integrations/arc.py --dry-run

# Write ARC instrument + asset metadata into atlas/data/*.json
python3 integrations/arc.py

# Use an existing local ARC clone and process only selected exchanges
python3 integrations/arc.py \
  --arc-path /path/to/arc \
  --exchanges binance-spot,bybit-perps,okx-perps
```

### Populate CoinGecko metadata

```bash
# Dry-run (prints match stats, does not modify files)
python3 integrations/coingecko_metadata.py --dry-run

# Write verified coingecko_id into atlas/data/*.json
# Verification compares CoinGecko last price from derivatives/tickers
# (single call per run) against Binance latest close.
python3 integrations/coingecko_metadata.py

# Rate-limit verification requests to avoid HTTP 429
python3 integrations/coingecko_metadata.py \
  --max-verifications 25 \
  --coingecko-min-interval-seconds 1.2
```

### Historical Data Integrations

- `integrations/binance.py`: fetches Binance historical klines via `/api/v3/klines`.
- `integrations/coingecko.py`: fetches CoinGecko historical market chart data via `/api/v3/coins/{id}/market_chart`.

## Supported Exchanges

Atlas - Crypto Security Master currently contains parsers for the following exchange IDs.

**Stability warning:** any row marked **UNSTABLE (BETA)** is treated as beta by the codebase and is skipped by `update.py` in default runs.

| Exchange ID | Stability |
| --- | --- |
| `binance` | ✅ **`STABLE`** |
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
| `bybit-perps` | ✅ **`STABLE`** |
| `bybit-futures` | ✅ **`STABLE`** |
| `coinbase` | **`UNSTABLE (BETA)`** |
| `crypto-com` | **`UNSTABLE (BETA)`** |
| `cryptofacilities` | **`UNSTABLE (BETA)`** |
| `deribit` | **`UNSTABLE (BETA)`** |
| `ftx` | **`UNSTABLE (BETA)`** |
| `gate-io` | **`UNSTABLE (BETA)`** |
| `gate-io-futures` | **`UNSTABLE (BETA)`** |
| `gemini` | **`UNSTABLE (BETA)`** |
| `huobi` | **`UNSTABLE (BETA)`** |
| `huobi-dm` | **`UNSTABLE (BETA)`** |
| `huobi-dm-swap` | **`UNSTABLE (BETA)`** |
| `huobi-dm-linear-swap` | **`UNSTABLE (BETA)`** |
| `hyperliquid-spot` | ✅ **`STABLE`** |
| `hyperliquid-perps` | ✅ **`STABLE`** |
| `kraken` | **`UNSTABLE (BETA)`** |
| `kucoin` | **`UNSTABLE (BETA)`** |
| `okx-spot` | ✅ **`STABLE`** |
| `okx-perps` | ✅ **`STABLE`** |
| `okx-futures` | ✅ **`STABLE`** |
| `phemex` | **`UNSTABLE (BETA)`** |
| `poloniex` | **`UNSTABLE (BETA)`** |
| `upbit` | **`UNSTABLE (BETA)`** |

### Bundled Snapshot Coverage

The repository currently ships precomputed JSON snapshots for:

- `binance-spot`
- `binance-futures`
- `binance-futures-cm`
- `bybit-spot`
- `bybit-perps`
- `bybit-futures`
- `hyperliquid-spot`
- `hyperliquid-perps`
- `okx-spot`
- `okx-perps`
- `okx-futures`

## Features

- Unified parser layer with exchange-specific symbol rules.
- Stable-vs-beta exchange classification (beta exchanges are skipped in `update.py` by default).
- Exchange ID mapping to Tardis IDs (`to_tardis_exchange_id`).
- Metadata merge logic that preserves previously-known fields when the current source omits them.
- Date-window filtering over locally stored symbol availability intervals.

## Limitations

- No options mapping in the securities master map:
  - `update.py` ingests only `spot`, `perpetual`, and `future` from source metadata.
  - Option contracts may parse for some exchanges (for example, Deribit), but they are not included in the precomputed `internal_id` map.
- Instruments without a derived `internal_id` (for example options, combos, or unrecognized formats) are intentionally absent from `SecurityMaster` lookups.
- `--source exchange` only works for exchanges that have direct API fetchers implemented (currently Binance/OKX variants).
- The bundled data is limited to the snapshot files present in `atlas/data`; all other exchange coverage depends on running updates with an available upstream source.

## Project Layout

- `atlas/contracts.py`: normalized `Contract` model.
- `atlas/exchanges.py`: exchange registry (parser + source metadata).
- `atlas/parsers.py`: parse entrypoint (`parse_contract`).
- `atlas/database.py`: load/query local securities master snapshots.
- `atlas/update.py`: snapshot updater CLI.
- `atlas/data/*.json`: generated per-exchange snapshot files.

## Testing

```bash
pytest tests/
```

## License

MIT (see `LICENSE`).
