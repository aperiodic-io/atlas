from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BINANCE_API_BASE = "https://api.binance.com"
BINANCE_FUTURES_API_BASE = "https://fapi.binance.com"
BINANCE_WEBSITE_API_BASE = "https://www.binance.com"


def _get_json(
    path: str,
    params: dict[str, Any] | None = None,
    timeout_seconds: int = 30,
    api_base: str = BINANCE_API_BASE,
) -> Any:
    query = f"?{urlencode(params)}" if params else ""
    request = Request(
        url=f"{api_base}{path}{query}",
        headers={"Accept": "application/json", "User-Agent": "atlas-integrations/1.0"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_spot_symbols(timeout_seconds: int = 30) -> list[dict]:
    payload = _get_json("/api/v3/exchangeInfo", timeout_seconds=timeout_seconds)
    symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
    return [
        s
        for s in symbols
        if isinstance(s, dict)
        and s.get("status") == "TRADING"
        and s.get("symbol")
        and s.get("baseAsset")
        and s.get("quoteAsset")
    ]


def fetch_spot_prices(timeout_seconds: int = 30) -> list[dict]:
    """Fetch Binance spot 24-hour tickers, including last-price timestamps."""
    payload = _get_json("/api/v3/ticker/24hr", timeout_seconds=timeout_seconds)
    return [ticker for ticker in payload if isinstance(ticker, dict)] if isinstance(payload, list) else []


def fetch_futures_prices(timeout_seconds: int = 30) -> list[dict]:
    """Fetch Binance USD-M futures 24-hour tickers, including last prices."""
    payload = _get_json(
        "/fapi/v1/ticker/24hr",
        timeout_seconds=timeout_seconds,
        api_base=BINANCE_FUTURES_API_BASE,
    )
    return [ticker for ticker in payload if isinstance(ticker, dict)] if isinstance(payload, list) else []


def fetch_public_assets(timeout_seconds: int = 30) -> list[dict]:
    """Fetch Binance website asset metadata, including delisting and rename state.

    This website endpoint is not part of Binance's documented trading API; callers
    should persist it as non-authoritative enrichment evidence.
    """
    payload = _get_json(
        "/bapi/asset/v2/public/asset/asset/get-all-asset",
        timeout_seconds=timeout_seconds,
        api_base=BINANCE_WEBSITE_API_BASE,
    )
    assets = payload.get("data", []) if isinstance(payload, dict) else []
    return [asset for asset in assets if isinstance(asset, dict) and asset.get("assetCode")]


def fetch_historical_klines(
    symbol: str,
    interval: str = "1d",
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    limit: int = 1000,
    timeout_seconds: int = 30,
) -> list[dict]:
    params: dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    if start_time_ms is not None:
        params["startTime"] = int(start_time_ms)
    if end_time_ms is not None:
        params["endTime"] = int(end_time_ms)
    payload = _get_json("/api/v3/klines", params=params, timeout_seconds=timeout_seconds)
    if not isinstance(payload, list):
        return []

    out: list[dict] = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 7:
            continue
        out.append(
            {
                "open_time_ms": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time_ms": int(row[6]),
            }
        )
    return out
