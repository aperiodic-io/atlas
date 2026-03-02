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

## Installation

Python 3.12+ is required.

```bash
pip install -e .
```

## Quick Start

### Parse a single symbol

```python
from security_master.parsers import parse_contract

symbol_data = {"id": "BTCUSDT", "type": "spot"}
contract = parse_contract("binance-spot", symbol_data)

print(contract.internal_id)  # spot-BTC-USDT
```

### Load the local securities master

```python
from datetime import datetime
from security_master import SecuritiesMaster

sm = SecuritiesMaster.load()

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
python3 security_master/update.py --source hybrid

# Tardis only
python3 security_master/update.py --source tardis

# Exchange API only (supported exchanges only)
python3 security_master/update.py --source exchange

# Restrict to selected exchanges
python3 security_master/update.py --exchanges binance-spot,okx-spot
```

## Supported Exchanges

Atlas - Crypto Security Master currently contains parsers for the following exchange IDs.

**Stability warning:** any row marked **UNSTABLE (BETA)** is treated as beta by the codebase and is skipped by `update.py` in default runs.

| Exchange ID | Stability |
| --- | --- |
| `binance` | `STABLE` |
| `binance-spot` | `STABLE` |
| `binance-futures` | `STABLE` |
| `binance-futures-cm` | `STABLE` |
| `bitmex` | **`UNSTABLE (BETA)`** |
| `bitfinex` | **`UNSTABLE (BETA)`** |
| `bitfinex-derivatives` | **`UNSTABLE (BETA)`** |
| `bitget` | **`UNSTABLE (BETA)`** |
| `bitget-futures` | **`UNSTABLE (BETA)`** |
| `bitstamp` | **`UNSTABLE (BETA)`** |
| `bybit` | **`UNSTABLE (BETA)`** |
| `bybit-spot` | **`UNSTABLE (BETA)`** |
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
| `hyperliquid` | **`UNSTABLE (BETA)`** |
| `kraken` | **`UNSTABLE (BETA)`** |
| `kucoin` | **`UNSTABLE (BETA)`** |
| `okx-spot` | `STABLE` |
| `okx-perps` | `STABLE` |
| `okx-futures` | `STABLE` |
| `phemex` | **`UNSTABLE (BETA)`** |
| `poloniex` | **`UNSTABLE (BETA)`** |
| `upbit` | **`UNSTABLE (BETA)`** |

### Bundled Snapshot Coverage

The repository currently ships precomputed JSON snapshots for:

- `binance-spot`
- `binance-futures`
- `binance-futures-cm`
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
- Instruments without a derived `internal_id` (for example options, combos, or unrecognized formats) are intentionally absent from `SecuritiesMaster` lookups.
- `--source exchange` only works for exchanges that have direct API fetchers implemented (currently Binance/OKX variants).
- The bundled data is limited to the snapshot files present in `security_master/data`; all other exchange coverage depends on running updates with an available upstream source.

## Project Layout

- `security_master/contracts.py`: normalized `Contract` model.
- `security_master/exchanges.py`: exchange registry (parser + source metadata).
- `security_master/parsers.py`: parse entrypoint (`parse_contract`).
- `security_master/database.py`: load/query local securities master snapshots.
- `security_master/update.py`: snapshot updater CLI.
- `security_master/data/*.json`: generated per-exchange snapshot files.

## Testing

```bash
pytest tests/
```

## License

MIT (see `LICENSE`).
