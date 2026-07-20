"""Populate selected CMC CATEGORY tags for snapshot rows with a CoinMarketCap ID."""

from __future__ import annotations

import argparse
import json
import time
from email.utils import parsedate_to_datetime
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import requests


CMC_DETAIL_URL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"
REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MIN_INTERVAL_SECONDS = 0.25
MAX_RETRY_DELAY_SECONDS = 120
INCLUDED_CATEGORY_TAGS = (
    "DeFi",
    "Layer 1",
    "Layer 2",
    "Tokenized Stock",
    "Real World Assets Protocols",
    "Staking",
    "Play To Earn",
    "Generative AI",
    "Smart Contracts",
    "DApp",
    "Collectibles & NFTs",
    "Lending & Borrowing",
    "DAO",
    "Metaverse",
    "Privacy",
    "AI Agents",
    "AI Applications",
    "Interoperability",
)


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
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
) -> dict[int, list[str]]:
    """Fetch selected CATEGORY tags by stable CMC ID from the CMC detail endpoint."""
    if timeout_seconds <= 0 or max_attempts <= 0 or min_interval_seconds < 0:
        raise ValueError("invalid CMC category request settings")

    categories: dict[int, list[str]] = {}
    next_request_at = 0.0
    for cmc_id in sorted(cmc_ids):
        if cmc_id <= 0:
            raise ValueError(f"invalid CMC ID: {cmc_id}")
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            delay = next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                response = session.get(
                    CMC_DETAIL_URL,
                    params={"id": cmc_id},
                    timeout=timeout_seconds,
                    headers={"User-Agent": "atlas-cmc-category/1.0"},
                )
                response.raise_for_status()
                payload = response.json()
                category = _parse_categories(payload, cmc_id)
                categories[cmc_id] = category
                next_request_at = time.monotonic() + min_interval_seconds
                break
            except (CmcCategoryError, requests.RequestException, ValueError) as error:
                last_error = error
                if attempt + 1 < max_attempts:
                    next_request_at = time.monotonic() + _retry_delay(error, attempt)
        else:
            raise CmcCategoryError(
                f"failed to fetch category for CMC ID {cmc_id}: {last_error}"
            ) from last_error
    return categories


def populate_cmc_categories(
    data_dir: Path,
    dry_run: bool = False,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
) -> dict[str, int]:
    """Replace ``cmc_category`` with selected CMC tags in ``category``."""
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
            if row.get("category") != category or "cmc_category" in row:
                row["category"] = category
                row.pop("cmc_category", None)
                stats["updated"] += 1
                changed = True
        if changed:
            stats["files"] += 1
            if not dry_run:
                json_file.write_text(json.dumps(rows, indent=2) + "\n")
    return stats


def _parse_categories(payload: object, cmc_id: int) -> list[str]:
    try:
        tags = payload["data"]["tags"]  # type: ignore[index]
    except (KeyError, TypeError):
        raise CmcCategoryError(f"CMC response has no tags for ID {cmc_id}") from None
    if not isinstance(tags, list):
        raise CmcCategoryError(f"CMC tags are invalid for ID {cmc_id}")

    names = {
        tag.get("name")
        for tag in tags
        if isinstance(tag, dict) and tag.get("category") == "CATEGORY"
    }
    return [name for name in INCLUDED_CATEGORY_TAGS if name in names]


def _retry_delay(error: Exception, attempt: int) -> float:
    """Return a bounded CMC cooldown, preferring its Retry-After instruction."""
    response = getattr(error, "response", None)
    retry_after = getattr(response, "headers", {}).get("Retry-After")
    try:
        requested_delay = float(retry_after)
    except (TypeError, ValueError):
        requested_delay = _http_date_delay(retry_after)
    return min(max(requested_delay, 2**attempt), MAX_RETRY_DELAY_SECONDS)


def _http_date_delay(retry_after: object) -> float:
    if not isinstance(retry_after, str):
        return 0
    try:
        retry_at = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError, IndexError):
        return 0
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max((retry_at - datetime.now(UTC)).total_seconds(), 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=REQUEST_TIMEOUT_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument(
        "--min-interval-seconds",
        type=float,
        default=DEFAULT_MIN_INTERVAL_SECONDS,
        help="minimum delay between CMC requests (default: %(default)s)",
    )
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
