"""Per-asset CoinMarketCap metadata from CMC's keyless detail endpoint.

The symbology build needs a name, ticker, slug, kind (coin, token, fiat),
status, listing date and categories for every CMC ID it mints an Atlas asset
for. One request per ID is slow, so failures are collected rather than raised:
an asset whose details could not be fetched is simply left for the next run.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import requests

from atlas.categories import categories_from_cmc_tags
from integrations.http_retry import retry_delay_seconds


CMC_DETAIL_URL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail"
REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_MIN_INTERVAL_SECONDS = 0.25
MAX_RETRY_DELAY_SECONDS = 120
CACHE_SCHEMA_VERSION = 1


class JsonResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class JsonSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> JsonResponse: ...


@dataclass(frozen=True)
class CmcAssetDetail:
    cmc_id: int
    name: str
    symbol: str
    slug: str
    # CMC's own classification: "coin", "token" or "fiat".
    kind: str
    status: str
    date_added: datetime | None
    rank: int | None
    price_usd: float | None
    # Atlas categories (the allow-listed subset of ``tags``).
    categories: tuple[str, ...]
    tags: tuple[str, ...] = ()

    @property
    def is_fiat(self) -> bool:
        return self.kind == "fiat"


@dataclass(frozen=True)
class DetailFetchResult:
    details: dict[int, CmcAssetDetail]
    failures: dict[int, str]


def parse_cmc_asset_detail(payload: object, cmc_id: int) -> CmcAssetDetail:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError(f"CMC detail for {cmc_id} has no data")
    if data.get("id") != cmc_id:
        raise ValueError(f"CMC detail for {cmc_id} describes {data.get('id')!r}")
    name, symbol, slug = data.get("name"), data.get("symbol"), data.get("slug")
    if not all(isinstance(value, str) and value for value in (name, symbol, slug)):
        raise ValueError(f"CMC detail for {cmc_id} lacks a name, symbol or slug")
    statistics = data.get("statistics") if isinstance(data.get("statistics"), dict) else {}
    tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    rank = statistics.get("rank")
    price = statistics.get("price")
    return CmcAssetDetail(
        cmc_id=cmc_id,
        name=name,
        symbol=symbol,
        slug=slug,
        kind=str(data.get("category") or ""),
        status=str(data.get("status") or ""),
        date_added=_parse_timestamp(data.get("dateAdded")),
        rank=rank if isinstance(rank, int) and not isinstance(rank, bool) and rank > 0 else None,
        price_usd=float(price) if isinstance(price, int | float) and price > 0 else None,
        categories=tuple(categories_from_cmc_tags(tags)),
        tags=tuple(
            tag["name"]
            for tag in tags
            if isinstance(tag, dict) and isinstance(tag.get("name"), str)
        ),
    )


def fetch_cmc_asset_details(
    session: JsonSession,
    cmc_ids: Iterable[int],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
    progress_every: int = 0,
) -> DetailFetchResult:
    """Fetch details for every ID, recording rather than raising per-ID failures."""
    ids = sorted(set(cmc_ids))
    if any(cmc_id <= 0 for cmc_id in ids):
        raise ValueError("CMC IDs must be positive")
    if max_attempts <= 0 or min_interval_seconds < 0:
        raise ValueError("invalid CMC detail request settings")
    details: dict[int, CmcAssetDetail] = {}
    failures: dict[int, str] = {}
    next_request_at = 0.0
    for position, cmc_id in enumerate(ids, start=1):
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
                    headers={"User-Agent": "Mozilla/5.0 (atlas-cmc-details/1.0)"},
                )
                response.raise_for_status()
                details[cmc_id] = parse_cmc_asset_detail(response.json(), cmc_id)
                next_request_at = time.monotonic() + min_interval_seconds
                last_error = None
                break
            except (requests.RequestException, ValueError) as error:
                last_error = error
                next_request_at = time.monotonic() + retry_delay_seconds(
                    error, attempt, MAX_RETRY_DELAY_SECONDS
                )
        if last_error is not None:
            failures[cmc_id] = str(last_error)
        if progress_every and position % progress_every == 0:
            print(f"  CMC details: {position}/{len(ids)} ({len(failures)} failed)", flush=True)
    return DetailFetchResult(details, failures)


def save_detail_cache(path: Path, details: dict[int, CmcAssetDetail]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for _, detail in sorted(details.items()):
        row = asdict(detail)
        row["date_added"] = detail.date_added.isoformat() if detail.date_added else None
        row["categories"] = list(detail.categories)
        row["tags"] = list(detail.tags)
        rows.append(row)
    path.write_text(json.dumps({"schema_version": CACHE_SCHEMA_VERSION, "details": rows}))


def load_detail_cache(path: Path) -> dict[int, CmcAssetDetail]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ValueError("unsupported CMC detail cache schema")
    details: dict[int, CmcAssetDetail] = {}
    for row in payload.get("details", []):
        detail = CmcAssetDetail(
            **{
                **row,
                "date_added": _parse_timestamp(row.get("date_added")),
                "categories": tuple(row.get("categories") or ()),
                "tags": tuple(row.get("tags") or ()),
            }
        )
        details[detail.cmc_id] = detail
    return details


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
