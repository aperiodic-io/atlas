from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ARC_REPO = "https://github.com/amberdata/arc"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"

ARC_TO_ATLAS_EXCHANGES: dict[tuple[str, str], tuple[str, ...]] = {
    ("binance", "spot"): ("binance-spot",),
    ("binance", "futures"): ("binance-futures", "binance-futures-cm"),
    ("bybit", "spot"): ("bybit-spot",),
    ("bybit", "futures"): ("bybit-perps", "bybit-futures"),
    ("okex", "spot"): ("okx-spot",),
    ("okex", "futures"): ("okx-perps", "okx-futures"),
}

ARC_META_KEYS = {"arc_instrument", "arc_asset", "arc_asset_arc_id"}


@dataclass(frozen=True)
class MatchResult:
    instrument: dict | None
    ambiguous: bool


def _partition_value(path: Path, key: str) -> str | None:
    prefix = f"{key}="
    for part in path.parts:
        if part.startswith(prefix):
            return part[len(prefix) :]
    return None


def _normalize_symbol(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


def _instrument_bucket(record: dict) -> str | None:
    instrument_type = str(record.get("instrumentType", "")).lower()
    if instrument_type == "spot":
        return "spot"
    if instrument_type != "futures":
        return None
    contract_type = str(record.get("contractType", "")).lower()
    if contract_type == "perpetual":
        return "perpetual"
    return "future"


def _atlas_bucket(row: dict) -> str | None:
    value = str(row.get("contract_type") or row.get("type") or "").lower()
    if value in {"spot", "perpetual", "future"}:
        return value
    return None


def _candidate_keys(symbol_id: str) -> tuple[str, str]:
    upper = symbol_id.upper()
    return upper, _normalize_symbol(upper)


def _score_match(row: dict, candidate: dict) -> int:
    symbol_id = str(row.get("id", ""))
    id_upper, id_compact = _candidate_keys(symbol_id)
    score = 0
    native = str(candidate.get("nativeInstrument", ""))
    normalized = str(candidate.get("normalizedInstrument", ""))
    native_upper = native.upper()
    normalized_upper = normalized.upper()
    native_compact = _normalize_symbol(native_upper)
    normalized_compact = _normalize_symbol(normalized_upper)

    if native_upper == id_upper:
        score += 4
    if normalized_upper == id_upper:
        score += 3
    if native_compact and native_compact == id_compact:
        score += 2
    if normalized_compact and normalized_compact == id_compact:
        score += 1
    if bool(candidate.get("active")):
        score += 1
    if _instrument_bucket(candidate) == _atlas_bucket(row):
        score += 2
    return score


def _pick_best_match(row: dict, candidates: list[dict]) -> MatchResult:
    if not candidates:
        return MatchResult(instrument=None, ambiguous=False)

    atlas_bucket = _atlas_bucket(row)
    bucket_filtered = [c for c in candidates if _instrument_bucket(c) == atlas_bucket]
    pool = bucket_filtered or candidates

    scored: list[tuple[int, dict]] = sorted(
        ((_score_match(row, c), c) for c in pool),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored or scored[0][0] <= 0:
        return MatchResult(instrument=None, ambiguous=False)

    top_score = scored[0][0]
    top_records = [c for score, c in scored if score == top_score]
    top_keys = {
        (str(c.get("assetArcId", "")), str(c.get("nativeInstrument", "")).upper())
        for c in top_records
    }
    if len(top_keys) > 1:
        return MatchResult(instrument=None, ambiguous=True)
    return MatchResult(instrument=top_records[0], ambiguous=False)


def _build_arc_assets_index(arc_root: Path) -> dict[str, dict]:
    assets_dir = arc_root / "packages" / "assets"
    assets: dict[str, dict] = {}
    for path in assets_dir.rglob("data.json"):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        arc_id = data.get("assetArcId")
        if isinstance(arc_id, str):
            assets[arc_id] = data
    return assets


def _build_arc_instrument_index(arc_root: Path) -> dict[tuple[str, str], list[dict]]:
    instruments_dir = arc_root / "packages" / "instruments"
    index: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for path in instruments_dir.rglob("data.json"):
        instrument_type = _partition_value(path, "type")
        exchange = _partition_value(path, "exchange")
        if not instrument_type or not exchange:
            continue
        mapping = ARC_TO_ATLAS_EXCHANGES.get((exchange, instrument_type))
        if mapping is None:
            continue

        try:
            records = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(records, list):
            continue

        for record in records:
            if not isinstance(record, dict):
                continue
            keys = set()
            native = record.get("nativeInstrument")
            normalized = record.get("normalizedInstrument")
            if isinstance(native, str):
                upper, compact = _candidate_keys(native)
                keys.add(upper)
                keys.add(compact)
            if isinstance(normalized, str):
                upper, compact = _candidate_keys(normalized)
                keys.add(upper)
                keys.add(compact)
            keys.discard("")
            if not keys:
                continue
            for atlas_exchange in mapping:
                for key in keys:
                    index[(atlas_exchange, key)].append(record)
    return index


def _resolve_arc_path(arc_path: str | None, arc_repo: str) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    if arc_path:
        return Path(arc_path).expanduser().resolve(), None

    temp_dir = tempfile.TemporaryDirectory(prefix="arc-repo-")
    clone_target = Path(temp_dir.name) / "arc"
    subprocess.run(
        ["git", "clone", "--depth=1", arc_repo, str(clone_target)],
        check=True,
    )
    return clone_target, temp_dir


def populate_arc_metadata(
    data_dir: Path,
    arc_root: Path,
    exchanges: set[str] | None = None,
    dry_run: bool = False,
) -> dict[str, dict[str, int]]:
    assets_by_id = _build_arc_assets_index(arc_root)
    instrument_index = _build_arc_instrument_index(arc_root)

    stats: dict[str, dict[str, int]] = {}
    for json_file in sorted(data_dir.glob("*.json")):
        exchange = json_file.stem
        if exchanges and exchange not in exchanges:
            continue
        rows = json.loads(json_file.read_text())
        if not isinstance(rows, list):
            continue

        matched = 0
        unmatched = 0
        ambiguous = 0
        for row in rows:
            if not isinstance(row, dict) or "id" not in row:
                continue

            symbol_id = str(row["id"])
            keys = _candidate_keys(symbol_id)
            candidates: list[dict] = []
            for key in keys:
                candidates.extend(instrument_index.get((exchange, key), []))

            result = _pick_best_match(row, candidates)
            if result.ambiguous:
                ambiguous += 1
                for k in ARC_META_KEYS:
                    row.pop(k, None)
                continue
            if result.instrument is None:
                unmatched += 1
                for k in ARC_META_KEYS:
                    row.pop(k, None)
                continue

            instrument = result.instrument
            row["arc_instrument"] = instrument
            arc_id = instrument.get("assetArcId")
            if isinstance(arc_id, str):
                row["arc_asset_arc_id"] = arc_id
                asset = assets_by_id.get(arc_id)
                if asset is not None:
                    row["arc_asset"] = asset
                else:
                    row.pop("arc_asset", None)
            else:
                row.pop("arc_asset_arc_id", None)
                row.pop("arc_asset", None)
            matched += 1

        if not dry_run:
            json_file.write_text(json.dumps(rows, indent=2))
        stats[exchange] = {
            "matched": matched,
            "unmatched": unmatched,
            "ambiguous": ambiguous,
            "rows": len(rows),
        }
    return stats


def _parse_exchanges(raw: str) -> set[str] | None:
    values = {e.strip() for e in raw.split(",") if e.strip()}
    return values or None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Populate Atlas snapshot rows with ARC instrument/asset metadata by matching "
            "exchange + symbol IDs."
        )
    )
    parser.add_argument(
        "--arc-path",
        default="",
        help="Path to a local clone of amberdata/arc. If omitted, the script clones it.",
    )
    parser.add_argument(
        "--arc-repo",
        default=DEFAULT_ARC_REPO,
        help="ARC Git repository URL used when --arc-path is not provided.",
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
    args = parser.parse_args()

    arc_root, temp_dir = _resolve_arc_path(args.arc_path or None, args.arc_repo)
    try:
        stats = populate_arc_metadata(
            data_dir=Path(args.data_dir).expanduser().resolve(),
            arc_root=arc_root,
            exchanges=_parse_exchanges(args.exchanges),
            dry_run=args.dry_run,
        )
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    total_matched = 0
    total_unmatched = 0
    total_ambiguous = 0
    total_rows = 0
    for exchange, s in sorted(stats.items()):
        total_matched += s["matched"]
        total_unmatched += s["unmatched"]
        total_ambiguous += s["ambiguous"]
        total_rows += s["rows"]
        print(
            f"{exchange}: matched={s['matched']} unmatched={s['unmatched']} "
            f"ambiguous={s['ambiguous']} rows={s['rows']}"
        )
    print(
        f"\nTOTAL: matched={total_matched} unmatched={total_unmatched} "
        f"ambiguous={total_ambiguous} rows={total_rows}"
    )


if __name__ == "__main__":
    main()
