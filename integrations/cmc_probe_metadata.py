"""Write keyless CMC probe evidence to Atlas exchange snapshot rows.

The resulting ``cmc_probe`` field is explicitly non-authoritative. It contains
price compatibility evidence only and never writes an approved ``cmc_id``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

import requests

from integrations.cmc_id_probe import (
    CmcCatalogue,
    CmcProbeError,
    PriceObservation,
    ProbeResult,
    candidates_for_symbol,
    classify_price_candidates,
    fetch_binance_spot_prices,
    fetch_cmc_catalogue,
)


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"
BINANCE_EXCHANGES = frozenset({"binance-spot", "binance-futures", "binance-futures-cm"})


def populate_cmc_probe_metadata(
    data_dir: Path,
    catalogue: CmcCatalogue,
    observations: dict[str, PriceObservation],
    max_relative_difference: float = 0.05,
    max_timestamp_skew: timedelta = timedelta(minutes=3),
    dry_run: bool = False,
    exchanges: frozenset[str] = BINANCE_EXCHANGES,
) -> dict[str, dict[str, int]]:
    """Attach non-authoritative CMC price evidence to selected snapshot rows."""
    results = {
        symbol: classify_price_candidates(
            candidates_for_symbol(catalogue.assets, symbol),
            observation,
            max_relative_difference,
            max_timestamp_skew,
        )
        for symbol, observation in observations.items()
    }
    stats: dict[str, dict[str, int]] = {}
    for json_file in sorted(data_dir.glob("*.json")):
        if json_file.stem not in exchanges:
            continue
        rows = json.loads(json_file.read_text())
        if not isinstance(rows, list):
            continue
        counts: Counter[str] = Counter()
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if not isinstance(symbol, str):
                continue
            result = results.get(symbol.upper())
            if result is None:
                counts["no_binance_spot_price"] += 1
                continue
            row["cmc_probe"] = _probe_record(result, observations[symbol.upper()])
            counts[result.status.value] += 1
        if not dry_run:
            json_file.write_text(json.dumps(rows, indent=2) + "\n")
        stats[json_file.stem] = dict(counts)
    return stats


def remove_cmc_probe_metadata(data_dir: Path, exchanges: set[str]) -> dict[str, int]:
    """Remove probe evidence from exchanges outside the supported Binance scope."""
    removed: dict[str, int] = {}
    for json_file in sorted(data_dir.glob("*.json")):
        if json_file.stem not in exchanges:
            continue
        rows = json.loads(json_file.read_text())
        if not isinstance(rows, list):
            continue
        count = 0
        for row in rows:
            if isinstance(row, dict) and "cmc_probe" in row:
                row.pop("cmc_probe")
                count += 1
        if count:
            json_file.write_text(json.dumps(rows, indent=2) + "\n")
            removed[json_file.stem] = count
    return removed


def _probe_record(
    result: ProbeResult, observation: PriceObservation
) -> dict[str, object]:
    record: dict[str, object] = {
        "status": result.status.value,
        "source": "coinmarketcap-website-probe",
        "exchange_venue": observation.venue,
        "exchange_instrument_id": observation.instrument_id,
        "exchange_price": observation.normalized_price,
        "exchange_quote_currency": observation.quote_currency,
        "exchange_observed_at": observation.observed_at.isoformat().replace(
            "+00:00", "Z"
        ),
    }
    if result.match is not None:
        record.update(
            {
                "cmc_id": result.match.asset.cmc_id,
                "cmc_slug": result.match.asset.slug,
                "cmc_price_usd": result.match.asset.price_usd,
                "cmc_last_updated": result.match.asset.last_updated.isoformat().replace(
                    "+00:00", "Z"
                ),
                "relative_difference": result.match.relative_difference,
                "timestamp_skew_seconds": result.match.timestamp_skew.total_seconds(),
            }
        )
    return record


def _all_snapshot_symbols(data_dir: Path, exchanges: frozenset[str]) -> set[str]:
    symbols: set[str] = set()
    for json_file in data_dir.glob("*.json"):
        if json_file.stem not in exchanges:
            continue
        rows = json.loads(json_file.read_text())
        if isinstance(rows, list):
            symbols.update(
                row["symbol"].upper()
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("symbol"), str)
            )
    return symbols


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-relative-difference", type=float, default=0.05)
    parser.add_argument("--max-timestamp-skew-seconds", type=int, default=180)
    args = parser.parse_args()
    try:
        with requests.Session() as session:
            catalogue = fetch_cmc_catalogue(session)
            observations = fetch_binance_spot_prices(
                session, _all_snapshot_symbols(args.data_dir, BINANCE_EXCHANGES)
            )
        if args.dry_run:
            removed = {}
        else:
            all_exchanges = {path.stem for path in args.data_dir.glob("*.json")}
            removed = remove_cmc_probe_metadata(
                args.data_dir, all_exchanges - BINANCE_EXCHANGES
            )
        stats = populate_cmc_probe_metadata(
            args.data_dir,
            catalogue,
            observations,
            max_relative_difference=args.max_relative_difference,
            max_timestamp_skew=timedelta(seconds=args.max_timestamp_skew_seconds),
            dry_run=args.dry_run,
            exchanges=BINANCE_EXCHANGES,
        )
    except (CmcProbeError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"CMC probe metadata update failed: {error}")
        return 1

    for exchange, counts in sorted(stats.items()):
        print(f"{exchange}: {dict(sorted(counts.items()))}")
    if removed:
        print(f"removed unsupported probe metadata: {dict(sorted(removed.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
