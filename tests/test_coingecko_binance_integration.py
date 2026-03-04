from integrations.coingecko import (
    fetch_derivatives_tickers,
    fetch_historical_market_chart,
    fetch_coin_list,
    coingecko_prices_match_binance_klines,
    match_binance_symbols_to_coingecko_ids,
)
from integrations.binance import fetch_historical_klines, fetch_spot_symbols


def test_match_binance_symbols_to_coingecko_ids_unique_only() -> None:
    binance_spot_symbols = [
        {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "status": "TRADING"},
        {"symbol": "DOGEUSDT", "baseAsset": "DOGE", "quoteAsset": "USDT", "status": "TRADING"},
    ]
    coingecko_coins = [
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
        {"id": "ethereum", "symbol": "eth", "name": "Ethereum"},
        {"id": "dogecoin", "symbol": "doge", "name": "Dogecoin"},
        {"id": "doge-token", "symbol": "doge", "name": "Doge Token"},
    ]

    mapping = match_binance_symbols_to_coingecko_ids(binance_spot_symbols, coingecko_coins)

    assert mapping["BTC"] == "bitcoin"
    assert mapping["ETH"] == "ethereum"
    assert "DOGE" not in mapping


def test_coingecko_prices_match_binance_klines() -> None:
    coingecko_prices = [
        (1_700_000_000_000, 100.0),
        (1_700_086_400_000, 101.0),
        (1_700_172_800_000, 99.0),
    ]
    binance_klines = [
        {"close_time_ms": 1_700_000_000_000, "close": 100.4},
        {"close_time_ms": 1_700_086_400_000, "close": 100.8},
        {"close_time_ms": 1_700_172_800_000, "close": 99.2},
    ]

    assert coingecko_prices_match_binance_klines(
        coingecko_prices,
        binance_klines,
        max_relative_diff=0.01,
    )


def test_match_binance_symbols_to_coingecko_ids_live_all_symbols() -> None:
    try:
        binance_spot_symbols = fetch_spot_symbols(timeout_seconds=30)
        coingecko_coins = fetch_coin_list(timeout_seconds=30)
    except Exception as exc:  # pragma: no cover - network-dependent test guard
        import pytest

        pytest.skip(f"live endpoint fetch failed: {exc}")

    mapping = match_binance_symbols_to_coingecko_ids(binance_spot_symbols, coingecko_coins)

    coin_ids_by_symbol: dict[str, set[str]] = {}
    for coin in coingecko_coins:
        symbol = coin.get("symbol")
        coin_id = coin.get("id")
        if not isinstance(symbol, str) or not isinstance(coin_id, str):
            continue
        key = symbol.upper()
        coin_ids_by_symbol.setdefault(key, set()).add(coin_id)

    base_symbols = {
        str(row.get("baseAsset", "")).upper()
        for row in binance_spot_symbols
        if isinstance(row, dict) and row.get("baseAsset")
    }

    assert base_symbols, "binance spot symbols response is empty"
    assert coingecko_coins, "coingecko coin list response is empty"

    for base in base_symbols:
        ids = coin_ids_by_symbol.get(base, set())
        if len(ids) == 1:
            assert mapping.get(base) == next(iter(ids))
        else:
            assert base not in mapping

    # Live last-price comparison using CoinGecko derivatives/tickers (single call).
    base_to_usdt_symbol: dict[str, str] = {}
    for row in binance_spot_symbols:
        if not isinstance(row, dict):
            continue
        base = row.get("baseAsset")
        quote = row.get("quoteAsset")
        symbol = row.get("symbol")
        if not isinstance(base, str) or not isinstance(quote, str) or not isinstance(symbol, str):
            continue
        if quote.upper() == "USDT":
            base_to_usdt_symbol.setdefault(base.upper(), symbol.upper())

    compared = 0
    matched = 0
    max_symbols_to_compare = 15
    preferred = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "TRX", "LTC", "LINK"]
    ordered_bases: list[str] = []
    for base in preferred:
        if base in mapping:
            ordered_bases.append(base)
    for base in sorted(mapping):
        if base not in ordered_bases:
            ordered_bases.append(base)

    try:
        derivatives_tickers = fetch_derivatives_tickers(timeout_seconds=30, min_interval_seconds=1.2)
    except Exception:
        derivatives_tickers = []
    coingecko_last_by_symbol: dict[str, float] = {}
    for ticker in derivatives_tickers:
        if not isinstance(ticker, dict):
            continue
        market = ticker.get("market")
        symbol = ticker.get("symbol")
        last = ticker.get("last")
        if not isinstance(market, str) or "binance" not in market.lower():
            continue
        if not isinstance(symbol, str) or not isinstance(last, (float, int)):
            continue
        norm = "".join(ch for ch in symbol.upper() if ch.isalnum())
        if norm:
            coingecko_last_by_symbol[norm] = float(last)

    for base in ordered_bases:
        if compared >= max_symbols_to_compare:
            break
        symbol = base_to_usdt_symbol.get(base)
        if not symbol:
            continue
        coingecko_last = coingecko_last_by_symbol.get("".join(ch for ch in symbol.upper() if ch.isalnum()))
        if coingecko_last is None:
            continue
        try:
            bn_klines = fetch_historical_klines(
                symbol,
                interval="1d",
                limit=1,
                timeout_seconds=30,
            )
        except Exception:
            continue

        compared += 1
        if not bn_klines:
            continue
        close = bn_klines[-1].get("close")
        if not isinstance(close, (float, int)) or close == 0:
            continue
        rel_diff = abs(float(coingecko_last) - float(close)) / abs(float(close))
        if rel_diff <= 0.05:
            matched += 1

    if compared < 1:
        import pytest

        pytest.skip("insufficient live historical comparisons (endpoint variability)")
    assert matched / compared >= 0.8, "coingecko/binance historical price match rate too low"
