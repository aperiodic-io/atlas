"""A polite JSON client: one request at a time per host, a minimum interval
between them, ``Retry-After`` honoured, and a block reported rather than worked
around."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

from integrations.http_retry import retry_delay_seconds

# Names the project and where to find it; no personal contact details.
USER_AGENT = "AtlasWebDomains/1.0 (+https://github.com/aperiodic-io/atlas)"

MAX_RETRY_DELAY_SECONDS = 300
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class Blocked(RuntimeError):
    """The upstream refuses us (403/451, or still rate limiting after backing off)."""


@dataclass
class HostStats:
    requests: int = 0
    rate_limited: int = 0
    failures: int = 0


@dataclass
class Client:
    """Per-host paced GETs. ``intervals`` maps a host to its minimum spacing."""

    intervals: Mapping[str, float]
    session: requests.Session = field(default_factory=requests.Session)
    default_interval: float = 1.0
    max_attempts: int = 4
    max_attempts_by_host: Mapping[str, int] = field(default_factory=dict)
    timeout: float = 60.0
    stats: dict[str, HostStats] = field(default_factory=dict)
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _next_at: dict[str, float] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    def _lock(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        accept: frozenset[int] = frozenset({200}),
    ) -> requests.Response:
        """GET ``url``; returns a response whose status is in ``accept``."""
        host = urlsplit(url).hostname or ""
        interval = self.intervals.get(host, self.default_interval)
        stats = self.stats.setdefault(host, HostStats())
        last_error: Exception | None = None
        attempts = self.max_attempts_by_host.get(host, self.max_attempts)
        with self._lock(host):
            for attempt in range(attempts):
                delay = self._next_at.get(host, 0.0) - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                stats.requests += 1
                try:
                    response = self.session.get(
                        url,
                        params=params,
                        headers={"User-Agent": USER_AGENT, **(headers or {})},
                        timeout=self.timeout,
                    )
                except requests.RequestException as error:
                    stats.failures += 1
                    last_error = error
                    self._next_at[host] = time.monotonic() + max(
                        interval,
                        retry_delay_seconds(error, attempt, MAX_RETRY_DELAY_SECONDS),
                    )
                    continue
                self._next_at[host] = time.monotonic() + interval
                if response.status_code in accept:
                    return response
                if response.status_code in (403, 451):
                    stats.failures += 1
                    raise Blocked(f"{host} answered {response.status_code}; stopping")
                if response.status_code in _RETRYABLE:
                    stats.rate_limited += response.status_code == 429
                    http_error = requests.HTTPError(
                        f"{host} answered {response.status_code}", response=response
                    )
                    last_error = http_error
                    self._next_at[host] = time.monotonic() + max(
                        interval,
                        retry_delay_seconds(
                            http_error, attempt, MAX_RETRY_DELAY_SECONDS
                        ),
                    )
                    continue
                stats.failures += 1
                response.raise_for_status()
                raise requests.HTTPError(f"{host} answered {response.status_code}")
        last_response = getattr(last_error, "response", None)
        if last_response is not None and last_response.status_code == 429:
            raise Blocked(f"{host} still rate limits after {attempts} attempts")
        raise RuntimeError(f"{url}: {last_error}")
