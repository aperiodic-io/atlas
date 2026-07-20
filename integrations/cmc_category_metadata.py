"""Populate CMC categories for snapshot rows with a CoinMarketCap ID."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Protocol

import requests


CMC_DETAIL_URL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"
REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_MAX_ATTEMPTS = 3


class JsonResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class JsonSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> JsonResponse: ...


class CmcCategoryError(RuntimeError):
    """The CMC detail endpoint did not return usable metadata."""


def fetch_cmc_categories(
    session: JsonSession,
    cmc_ids: set[int],
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_interval_seconds: float = 0.0,
) -> dict[int, str]:
    """Fetch categories by stable CMC ID from the CMC detail endpoint."""
    if timeout_seconds <= 0 or max_attempts <= 0 or min_interval_seconds < 0:
        raise ValueError("invalid CMC category request settings")

    categories: dict[int, str] = {}
    for cmc_id in sorted(cmc_ids):
        if cmc_id <= 0:
            raise ValueError(f"invalid CMC ID: {cmc_id}")
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                response = session.get(
                    CMC_DETAIL_URL,
                    params={"id": cmc_id},
                    timeout=timeout_seconds,
                    headers={"User-Agent": "atlas-cmc-category/1.0"},
                )
                response.raise_for_status()
                payload = response.json()
                category = _parse_category(payload, cmc_id)
                categories[cmc_id] = category
                break
            except (CmcCategoryError, requests.RequestException, ValueError) as error:
                last_error = error
                if attempt + 1 < max_attempts:
                    time.sleep(min(2**attempt, 8))
        else:
            raise CmcCategoryError(
                f"failed to fetch category for CMC ID {cmc_id}: {last_error}"
            ) from last_error
        if min_interval_seconds:
            time.sleep(min_interval_seconds)
    return categories


def populate_cmc_categories(
    data_dir: Path,
    dry_run: bool = False,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_interval_seconds: float = 0.0,
) -> dict[str, int]:
    """Add ``cmc_category`` to every row that has a CMC ID."""
    files = sorted(data_dir.glob("*.json"))
    rows_by_file: dict[Path, list[dict[str, object]]] = {}
    cmc_ids: set[int] = set()
    for json_file in files:
        rows = json.loads(json_file.read_text())
        if not isinstance(rows, list):
            continue
        valid_rows = [row for row in rows if isinstance(row, dict)]
        rows_by_file[json_file] = valid_rows
        cmc_ids.update(
            int(row["cmc_id"])
            for row in valid_rows
            if isinstance(row.get("cmc_id"), int) and not isinstance(row.get("cmc_id"), bool)
        )

    with requests.Session() as session:
        categories = fetch_cmc_categories(
            session,
            cmc_ids,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            min_interval_seconds=min_interval_seconds,
        )

    stats = {"files": 0, "rows": 0, "updated": 0, "missing": 0}
    for json_file, rows in rows_by_file.items():
        changed = False
        for row in rows:
            cmc_id = row.get("cmc_id")
            if not isinstance(cmc_id, int) or isinstance(cmc_id, bool):
                continue
            category = categories.get(cmc_id)
            if category is None:
                stats["missing"] += 1
                continue
            stats["rows"] += 1
            if row.get("cmc_category") != category:
                row["cmc_category"] = category
                stats["updated"] += 1
                changed = True
        if changed:
            stats["files"] += 1
            if not dry_run:
                json_file.write_text(json.dumps(rows, indent=2) + "\n")
    return stats


def _parse_category(payload: object, cmc_id: int) -> str:
    try:
        category = payload["data"]["category"]  # type: ignore[index]
    except (KeyError, TypeError):
        raise CmcCategoryError(f"CMC response has no category for ID {cmc_id}") from None
    if not isinstance(category, str) or not category:
        raise CmcCategoryError(f"CMC category is invalid for ID {cmc_id}")
    return category


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=REQUEST_TIMEOUT_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--min-interval-seconds", type=float, default=0.0)
    args = parser.parse_args()
    try:
        stats = populate_cmc_categories(
            args.data_dir,
            dry_run=args.dry_run,
            timeout_seconds=args.timeout_seconds,
            max_attempts=args.max_attempts,
            min_interval_seconds=args.min_interval_seconds,
        )
    except (CmcCategoryError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"CMC category metadata update failed: {error}")
        return 1
    print(" ".join(f"{key}={value}" for key, value in sorted(stats.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
