from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.binance import fetch_historical_klines, fetch_spot_symbols
from integrations.coingecko import (
    fetch_derivatives_tickers,
    fetch_coin_list,
    match_binance_symbols_to_coingecko_ids,
)


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"
COINGECKO_KEY = "coingecko_id"
PREFERRED_QUOTES = ("USDT", "USD", "USDC", "BUSD")
PREFERRED_VERIFICATION_SYMBOLS = (
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "XRP",
    "ADA",
    "DOGE",
    "TRX",
    "LTC",
    "LINK",
)


def _normalize_symbol(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


def _parse_exchanges(raw: str) -> set[str] | None:
    values = {e.strip() for e in raw.split(",") if e.strip()}
    return values or None


def populate_coingecko_metadata(
    data_dir: Path,
    exchanges: set[str] | None = None,
    dry_run: bool = False,
    timeout_seconds: int = 30,
    max_verifications: int = 25,
    coingecko_min_interval_seconds: float = 1.2,
    max_relative_diff: float = 0.05,
) -> dict[str, dict[str, int]]:
    binance_symbols = fetch_spot_symbols(timeout_seconds=timeout_seconds)
    coingecko_coins = fetch_coin_list(timeout_seconds=timeout_seconds)
    symbol_to_coingecko = match_binance_symbols_to_coingecko_ids(
        binance_symbols,
        coingecko_coins,
    )
    base_to_binance_symbol: dict[str, str] = {}
    for quote in PREFERRED_QUOTES:
        for row in binance_symbols:
            if not isinstance(row, dict):
                continue
            base = row.get("baseAsset")
            symbol = row.get("symbol")
            row_quote = row.get("quoteAsset")
            if (
                isinstance(base, str)
                and isinstance(symbol, str)
                and isinstance(row_quote, str)
                and row_quote.upper() == quote
                and base.upper() not in base_to_binance_symbol
            ):
                base_to_binance_symbol[base.upper()] = symbol.upper()

    verified_by_symbol: dict[str, bool] = {}
    ordered_symbols: list[str] = []
    for symbol in PREFERRED_VERIFICATION_SYMBOLS:
        if symbol in symbol_to_coingecko:
            ordered_symbols.append(symbol)
    for symbol in sorted(symbol_to_coingecko):
        if symbol not in ordered_symbols:
            ordered_symbols.append(symbol)

    symbols_to_verify: list[tuple[str, str, str]] = []
    for base_symbol in ordered_symbols:
        if len(symbols_to_verify) >= max_verifications:
            break
        coingecko_id = symbol_to_coingecko[base_symbol]
        binance_symbol = base_to_binance_symbol.get(base_symbol)
        if not binance_symbol:
            continue
        symbols_to_verify.append((base_symbol, coingecko_id, binance_symbol))

    coingecko_last_by_binance_symbol: dict[str, float] = {}
    try:
        tickers = fetch_derivatives_tickers(
            timeout_seconds=timeout_seconds,
            min_interval_seconds=coingecko_min_interval_seconds,
        )
    except Exception:
        tickers = []
    for ticker in tickers:
        market = ticker.get("market")
        if not isinstance(market, str) or "binance" not in market.lower():
            continue
        symbol = ticker.get("symbol")
        if not isinstance(symbol, str):
            continue
        norm_symbol = _normalize_symbol(symbol)
        if not norm_symbol:
            continue
        last = ticker.get("last")
        if not isinstance(last, (float, int)):
            converted_last = ticker.get("converted_last")
            if isinstance(converted_last, dict):
                usd_price = converted_last.get("usd")
                if isinstance(usd_price, (float, int)):
                    last = usd_price
        if isinstance(last, (float, int)):
            coingecko_last_by_binance_symbol[norm_symbol] = float(last)

    verification_attempted = len(symbols_to_verify)
    for base_symbol in ordered_symbols:
        coingecko_id = symbol_to_coingecko[base_symbol]
        binance_symbol = base_to_binance_symbol.get(base_symbol)
        if not binance_symbol:
            verified_by_symbol[base_symbol] = False
            continue
        if (base_symbol, coingecko_id, binance_symbol) not in symbols_to_verify:
            verified_by_symbol[base_symbol] = False
            continue
        coingecko_price = coingecko_last_by_binance_symbol.get(_normalize_symbol(binance_symbol))
        if coingecko_price is None or coingecko_price == 0:
            verified_by_symbol[base_symbol] = False
            continue
        try:
            binance_klines = fetch_historical_klines(
                symbol=binance_symbol,
                interval="1d",
                limit=1,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            verified_by_symbol[base_symbol] = False
            continue
        if not binance_klines:
            verified_by_symbol[base_symbol] = False
            continue
        close = binance_klines[-1].get("close")
        if not isinstance(close, (float, int)) or close == 0:
            verified_by_symbol[base_symbol] = False
            continue
        rel_diff = abs(float(coingecko_price) - float(close)) / abs(float(close))
        verified_by_symbol[base_symbol] = rel_diff <= max_relative_diff

    stats: dict[str, dict[str, int]] = {}
    for json_file in sorted(data_dir.glob("*.json")):
        exchange = json_file.stem
        if exchanges and exchange not in exchanges:
            continue

        rows = json.loads(json_file.read_text())
        if not isinstance(rows, list):
            continue

        matched = 0
        unverified = 0
        unmatched = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if not isinstance(symbol, str):
                row.pop(COINGECKO_KEY, None)
                unmatched += 1
                continue

            coingecko_id = symbol_to_coingecko.get(symbol.upper())
            if coingecko_id and verified_by_symbol.get(symbol.upper(), False):
                row[COINGECKO_KEY] = coingecko_id
                matched += 1
            elif coingecko_id:
                row.pop(COINGECKO_KEY, None)
                unverified += 1
            else:
                row.pop(COINGECKO_KEY, None)
                unmatched += 1

        if not dry_run:
            json_file.write_text(json.dumps(rows, indent=2))

        stats[exchange] = {
            "matched": matched,
            "unverified": unverified,
            "unmatched": unmatched,
            "verification_attempted": verification_attempted,
            "rows": len(rows),
        }

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate atlas/data/*.json with coingecko_id matched by symbol via Binance spot data."
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="Atlas data directory containing per-exchange JSON snapshots.",
    )
    parser.add_argument(
        "--exchanges",
        default="",
        help="Comma-separated Atlas exchanges to process (default: all files in data-dir).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print match stats without writing files.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        help="HTTP timeout for Binance/CoinGecko requests.",
    )
    parser.add_argument(
        "--max-verifications",
        type=int,
        default=25,
        help="Maximum number of CoinGecko historical verification requests per run.",
    )
    parser.add_argument(
        "--coingecko-min-interval-seconds",
        type=float,
        default=1.2,
        help="Minimum delay between CoinGecko requests.",
    )
    parser.add_argument(
        "--max-relative-diff",
        type=float,
        default=0.05,
        help="Maximum allowed relative difference between CoinGecko last price and Binance latest close.",
    )
    args = parser.parse_args()

    stats = populate_coingecko_metadata(
        data_dir=Path(args.data_dir).expanduser().resolve(),
        exchanges=_parse_exchanges(args.exchanges),
        dry_run=args.dry_run,
        timeout_seconds=args.timeout_seconds,
        max_verifications=max(args.max_verifications, 0),
        coingecko_min_interval_seconds=max(args.coingecko_min_interval_seconds, 0.0),
        max_relative_diff=max(args.max_relative_diff, 0.0),
    )

    total_matched = 0
    total_unverified = 0
    total_unmatched = 0
    total_rows = 0
    for exchange, s in sorted(stats.items()):
        total_matched += s["matched"]
        total_unverified += s["unverified"]
        total_unmatched += s["unmatched"]
        total_rows += s["rows"]
        print(
            f"{exchange}: matched={s['matched']} unverified={s['unverified']} "
            f"unmatched={s['unmatched']} verification_attempted={s['verification_attempted']} "
            f"rows={s['rows']}"
        )
    print(
        f"\nTOTAL: matched={total_matched} unverified={total_unverified} "
        f"unmatched={total_unmatched} rows={total_rows}"
    )


if __name__ == "__main__":
    main()
