"""Write keyless CMC probe evidence and price-verified IDs for Binance.

Probe evidence is stored separately under ``data/cmc_probe``. Main exchange
snapshots may receive a new ``cmc_id`` when the CMC and Binance prices match,
but an existing ID is never replaced or removed.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import requests

from integrations.binance import (
    fetch_futures_prices,
    fetch_public_assets,
    fetch_spot_prices,
)
from integrations.cmc_id_probe import (
    CmcCatalogue,
    CmcProbeError,
    PriceObservation,
    ProbeResult,
    candidates_for_symbol,
    classify_price_candidates,
    contract_multiplier_for_cmc_lookup,
    fetch_cmc_catalogue,
    normalize_cmc_lookup_symbol,
    price_observations_from_binance_tickers,
)


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"
BINANCE_EXCHANGES = frozenset({"binance-spot", "binance-futures", "binance-futures-cm"})


def populate_cmc_probe_metadata(
    data_dir: Path,
    catalogue: CmcCatalogue,
    observations_by_exchange: dict[str, dict[str, PriceObservation]],
    public_assets_by_symbol: dict[str, dict[str, object]] | None = None,
    max_relative_difference: float = 0.05,
    max_timestamp_skew: timedelta = timedelta(minutes=3),
    dry_run: bool = False,
    exchanges: frozenset[str] = BINANCE_EXCHANGES,
    probe_dir: Path | None = None,
) -> dict[str, dict[str, int]]:
    """Write probe evidence and add, but never replace or remove, CMC IDs."""
    cmc_symbols = {asset.symbol.upper() for asset in catalogue.assets}
    candidates_by_symbol = {
        symbol: candidates_for_symbol(catalogue.assets, symbol)
        for symbol in cmc_symbols
    }
    probe_dir = probe_dir or data_dir / "cmc_probe"
    public_assets_by_symbol = public_assets_by_symbol or {}
    stats: dict[str, dict[str, int]] = {}
    for json_file in sorted(data_dir.glob("*.json")):
        if json_file.stem not in exchanges:
            continue
        rows = json.loads(json_file.read_text())
        if not isinstance(rows, list):
            continue
        counts: Counter[str] = Counter()
        probe_rows: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            if not isinstance(symbol, str):
                continue
            lookup_symbol = normalize_cmc_lookup_symbol(symbol, cmc_symbols)
            observations = observations_by_exchange.get(json_file.stem, {})
            observation = observations.get(symbol.upper()) or observations.get(
                lookup_symbol
            )
            if observation is None:
                counts["no_binance_spot_price"] += 1
                row.pop("cmc_probe", None)
                probe_rows.append(
                    _probe_row(
                        row,
                        lookup_symbol,
                        _missing_price_record(),
                        public_assets_by_symbol.get(lookup_symbol),
                    )
                )
                continue
            observation = replace(
                observation,
                base_units_per_contract=(
                    observation.base_units_per_contract
                    * contract_multiplier_for_cmc_lookup(
                        symbol, lookup_symbol, cmc_symbols
                    )
                ),
            )
            result = classify_price_candidates(
                candidates_by_symbol.get(lookup_symbol, ()),
                observation,
                max_relative_difference,
                max_timestamp_skew,
            )
            record = _probe_record(result, observation)
            row.pop("cmc_probe", None)
            if result.match is not None and row.get("cmc_id") is None:
                row["cmc_id"] = result.match.asset.cmc_id
            probe_rows.append(
                _probe_row(
                    row,
                    lookup_symbol,
                    record,
                    public_assets_by_symbol.get(lookup_symbol),
                )
            )
            counts[result.status.value] += 1
        if not dry_run:
            json_file.write_text(json.dumps(rows, indent=2) + "\n")
            probe_dir.mkdir(parents=True, exist_ok=True)
            (probe_dir / json_file.name).write_text(json.dumps(probe_rows, indent=2) + "\n")
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
        "exchange_base_units_per_contract": observation.base_units_per_contract,
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


def _missing_price_record() -> dict[str, object]:
    return {
        "status": "no_binance_spot_price",
        "source": "coinmarketcap-website-probe",
    }


def _probe_row(
    row: dict,
    lookup_symbol: str,
    record: dict[str, object],
    public_asset: dict[str, object] | None,
) -> dict[str, object]:
    probe_row: dict[str, object] = {
        "id": row.get("id"),
        "symbol": row.get("symbol"),
        "cmc_lookup_symbol": lookup_symbol,
        "cmc_probe": record,
    }
    if "first_capture" in row:
        probe_row["first_capture"] = row["first_capture"]
    if public_asset is not None:
        probe_row["binance_public_asset"] = _public_asset_evidence(public_asset)
    return probe_row


def _public_asset_evidence(public_asset: dict[str, object]) -> dict[str, object]:
    """Keep identity and lifecycle fields useful for later mapping review."""
    fields = (
        "assetCode",
        "assetName",
        "assetDisplayName",
        "enLink",
        "logoUrl",
        "tags",
        "trading",
        "delisted",
        "preDelist",
        "pdTradeDeadline",
        "pdDepositDeadline",
        "pdAnnounceUrl",
        "oldAssetCode",
        "newAssetCode",
        "swapTag",
        "swapAnnounceUrl",
    )
    return {
        "source": "binance-public-asset-endpoint",
        **{field: public_asset[field] for field in fields if field in public_asset},
    }


def _snapshot_symbols_by_exchange(
    data_dir: Path, exchanges: frozenset[str], catalogue: CmcCatalogue
) -> dict[str, set[str]]:
    symbols_by_exchange: dict[str, set[str]] = {}
    cmc_symbols = {asset.symbol.upper() for asset in catalogue.assets}
    for json_file in data_dir.glob("*.json"):
        if json_file.stem not in exchanges:
            continue
        symbols = symbols_by_exchange.setdefault(json_file.stem, set())
        rows = json.loads(json_file.read_text())
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                    continue
                symbols.add(row["symbol"].upper())
                symbols.add(normalize_cmc_lookup_symbol(row["symbol"], cmc_symbols))
    return symbols_by_exchange


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
        symbols_by_exchange = _snapshot_symbols_by_exchange(
            args.data_dir, BINANCE_EXCHANGES, catalogue
        )
        spot_tickers = fetch_spot_prices()
        futures_tickers = fetch_futures_prices()
        public_assets_by_symbol = {
            asset["assetCode"].upper(): asset
            for asset in fetch_public_assets()
            if isinstance(asset.get("assetCode"), str)
        }
        observations_by_exchange = {
            "binance-spot": price_observations_from_binance_tickers(
                spot_tickers,
                symbols_by_exchange["binance-spot"],
                venue="binance-spot",
            ),
            "binance-futures": price_observations_from_binance_tickers(
                futures_tickers,
                symbols_by_exchange["binance-futures"],
                venue="binance-futures",
            ),
            "binance-futures-cm": price_observations_from_binance_tickers(
                futures_tickers,
                symbols_by_exchange["binance-futures-cm"],
                venue="binance-futures",
            ),
        }
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
            observations_by_exchange,
            public_assets_by_symbol,
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
