import json

from integrations.coingecko_metadata import populate_coingecko_metadata


def test_populate_coingecko_metadata_persists_only_verified_ids(
    monkeypatch,
    tmp_path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "binance-spot.json"
    target.write_text(
        json.dumps(
            [
                {"id": "btcusdt", "symbol": "BTC"},
                {"id": "ethusdt", "symbol": "ETH"},
                {"id": "dogeusdt", "symbol": "DOGE"},
            ]
        )
    )

    monkeypatch.setattr(
        "integrations.coingecko_metadata.fetch_spot_symbols",
        lambda timeout_seconds=30: [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
            {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "status": "TRADING"},
            {"symbol": "DOGEUSDT", "baseAsset": "DOGE", "quoteAsset": "USDT", "status": "TRADING"},
        ],
    )
    monkeypatch.setattr(
        "integrations.coingecko_metadata.fetch_coin_list",
        lambda timeout_seconds=30: [
            {"id": "bitcoin", "symbol": "btc"},
            {"id": "ethereum", "symbol": "eth"},
            {"id": "dogecoin", "symbol": "doge"},
        ],
    )

    def fake_derivatives_tickers(timeout_seconds: int = 30, min_interval_seconds: float = 1.2):
        return [
            {"market": "Binance", "symbol": "BTCUSDT", "last": 100.0},
            {"market": "Binance", "symbol": "ETHUSDT", "last": 200.0},
            {"market": "Binance", "symbol": "DOGEUSDT", "last": 0.5},
        ]

    def fake_klines(symbol: str, interval: str = "1d", limit: int = 5, timeout_seconds: int = 30):
        if symbol == "BTCUSDT":
            return [
                {"close_time_ms": 1_700_086_400_000, "close": 100.1},
            ]
        if symbol == "ETHUSDT":
            return [
                {"close_time_ms": 1_700_086_400_000, "close": 300.0},
            ]
        return []

    monkeypatch.setattr("integrations.coingecko_metadata.fetch_derivatives_tickers", fake_derivatives_tickers)
    monkeypatch.setattr("integrations.coingecko_metadata.fetch_historical_klines", fake_klines)

    stats = populate_coingecko_metadata(data_dir=data_dir, dry_run=False)
    rows = json.loads(target.read_text())

    by_symbol = {row["symbol"]: row for row in rows}
    assert by_symbol["BTC"]["coingecko_id"] == "bitcoin"
    assert "coingecko_id" not in by_symbol["ETH"]
    assert "coingecko_id" not in by_symbol["DOGE"]

    assert stats["binance-spot"]["matched"] == 1
    assert stats["binance-spot"]["unverified"] == 2
