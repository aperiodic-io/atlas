from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


COINGECKO_API_BASE = "https://api.coingecko.com/api/v3"
_LAST_REQUEST_TS = 0.0


def _get_json(
    path: str,
    params: dict[str, Any] | None = None,
    timeout_seconds: int = 30,
    max_retries: int = 4,
    min_interval_seconds: float = 1.2,
) -> Any:
    global _LAST_REQUEST_TS
    query = f"?{urlencode(params)}" if params else ""
    backoff_seconds = 1.0
    for attempt in range(max_retries + 1):
        now = time.monotonic()
        elapsed = now - _LAST_REQUEST_TS
        if elapsed < min_interval_seconds:
            time.sleep(min_interval_seconds - elapsed)

        request = Request(
            url=f"{COINGECKO_API_BASE}{path}{query}",
            headers={"Accept": "application/json", "User-Agent": "atlas-integrations/1.0"},
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                _LAST_REQUEST_TS = time.monotonic()
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            _LAST_REQUEST_TS = time.monotonic()
            if exc.code != 429 or attempt >= max_retries:
                raise
            retry_after = exc.headers.get("Retry-After")
            if retry_after is not None and retry_after.isdigit():
                sleep_seconds = max(float(retry_after), min_interval_seconds)
            else:
                sleep_seconds = backoff_seconds
                backoff_seconds = min(backoff_seconds * 2.0, 30.0)
            time.sleep(sleep_seconds)

    raise RuntimeError("unreachable")


def fetch_coin_list(timeout_seconds: int = 30) -> list[dict]:
    payload = _get_json(
        "/coins/list",
        params={"include_platform": "false"},
        timeout_seconds=timeout_seconds,
    )
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def fetch_historical_market_chart(
    coin_id: str,
    vs_currency: str = "usd",
    days: int = 30,
    timeout_seconds: int = 30,
    min_interval_seconds: float = 1.2,
) -> list[tuple[int, float]]:
    payload = _get_json(
        f"/coins/{coin_id}/market_chart",
        params={"vs_currency": vs_currency, "days": int(days)},
        timeout_seconds=timeout_seconds,
        min_interval_seconds=min_interval_seconds,
    )
    prices = payload.get("prices", []) if isinstance(payload, dict) else []
    out: list[tuple[int, float]] = []
    for point in prices:
        if not isinstance(point, list) or len(point) < 2:
            continue
        out.append((int(point[0]), float(point[1])))
    return out


def fetch_coins_markets(
    coin_ids: list[str],
    vs_currency: str = "usd",
    timeout_seconds: int = 30,
    min_interval_seconds: float = 1.2,
) -> dict[str, float]:
    ids = [coin_id for coin_id in coin_ids if isinstance(coin_id, str) and coin_id]
    if not ids:
        return {}

    prices: dict[str, float] = {}
    chunk_size = 100
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start : start + chunk_size]
        payload = _get_json(
            "/coins/markets",
            params={
                "vs_currency": vs_currency,
                "ids": ",".join(chunk),
                "order": "market_cap_desc",
                "per_page": len(chunk),
                "page": 1,
                "sparkline": "false",
            },
            timeout_seconds=timeout_seconds,
            min_interval_seconds=min_interval_seconds,
        )
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            coin_id = row.get("id")
            current_price = row.get("current_price")
            if isinstance(coin_id, str) and isinstance(current_price, (float, int)):
                prices[coin_id] = float(current_price)
    return prices


def fetch_derivatives_tickers(
    timeout_seconds: int = 30,
    min_interval_seconds: float = 1.2,
) -> list[dict]:
    payload = _get_json(
        "/derivatives",
        timeout_seconds=timeout_seconds,
        min_interval_seconds=min_interval_seconds,
    )
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def match_binance_symbols_to_coingecko_ids(
    binance_spot_symbols: list[dict],
    coingecko_coins: list[dict],
) -> dict[str, str]:
    coin_ids_by_symbol: dict[str, list[str]] = defaultdict(list)
    for coin in coingecko_coins:
        symbol = coin.get("symbol")
        coin_id = coin.get("id")
        if not isinstance(symbol, str) or not isinstance(coin_id, str):
            continue
        coin_ids_by_symbol[symbol.upper()].append(coin_id)

    binance_base_symbols: set[str] = {
        str(row.get("baseAsset", "")).upper()
        for row in binance_spot_symbols
        if isinstance(row, dict) and row.get("baseAsset")
    }

    mapping: dict[str, str] = {}
    for base in sorted(binance_base_symbols):
        ids = sorted(set(coin_ids_by_symbol.get(base, [])))
        if len(ids) == 1:
            mapping[base] = ids[0]
    return mapping


def coingecko_prices_match_binance_klines(
    coingecko_prices: list[tuple[int, float]],
    binance_klines: list[dict],
    max_relative_diff: float = 0.05,
) -> bool:
    if not coingecko_prices or not binance_klines:
        return False
    binance_by_day: dict[int, float] = {}
    for row in binance_klines:
        if not isinstance(row, dict):
            continue
        close = row.get("close")
        close_time = row.get("close_time_ms")
        if not isinstance(close, (float, int)) or not isinstance(close_time, int):
            continue
        day_key = close_time // 86_400_000
        binance_by_day[day_key] = float(close)

    if not binance_by_day:
        return False

    diffs: list[float] = []
    for ts_ms, cg_price in coingecko_prices:
        day_key = ts_ms // 86_400_000
        binance_close = binance_by_day.get(day_key)
        if binance_close is None or binance_close == 0:
            continue
        diffs.append(abs(cg_price - binance_close) / abs(binance_close))

    if not diffs:
        return False
    return (sum(diffs) / len(diffs)) <= max_relative_diff
