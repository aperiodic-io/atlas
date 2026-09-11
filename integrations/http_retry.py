"""Bounded HTTP retry delays that respect a server's ``Retry-After`` instruction."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


DEFAULT_MAX_RETRY_DELAY_SECONDS = 120


def retry_delay_seconds(
    error: Exception,
    attempt: int,
    max_delay_seconds: float = DEFAULT_MAX_RETRY_DELAY_SECONDS,
) -> float:
    """Return a bounded cooldown, preferring the response's Retry-After header."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or {}
    retry_after = headers.get("Retry-After")
    try:
        requested_delay = float(retry_after)
    except (TypeError, ValueError):
        requested_delay = _http_date_delay(retry_after)
    return min(max(requested_delay, 2**attempt), max_delay_seconds)


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
