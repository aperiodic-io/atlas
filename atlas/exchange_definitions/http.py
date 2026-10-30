"""Shared HTTP helpers for the exchange-API fetchers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# Some venues (Kraken Futures behind Cloudflare) answer the CI runner with 503
# for several seconds at a time, so back off for ~30s in total before giving up.
@retry(
    retry=retry_if_exception_type((requests.RequestException, ValueError)),
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=1, min=1, max=16),
    reraise=True,
)
def get_json(url: str, timeout_seconds: int, params: dict | None = None) -> Any:
    """GET a JSON document, retrying transient transport and decode failures."""
    response = requests.get(url, params=params, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


def iso_from_millis(value: Any) -> str | None:
    """ISO-8601 UTC timestamp for a positive epoch-milliseconds value."""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def iso_from_seconds(value: Any) -> str | None:
    """ISO-8601 UTC timestamp for a positive epoch-seconds value."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return iso_from_millis(seconds * 1000) if seconds > 0 else None


def metadata_delivery_date(sd: Any) -> datetime | None:
    """Parse the fetcher-supplied ISO `delivery_date` of a symbol record."""
    raw = sd.get("delivery_date")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
