from integrations import binance


def test_fetch_spot_prices_uses_spot_ticker_endpoint(monkeypatch) -> None:
    calls = []

    def fake_get_json(path, **kwargs):
        calls.append((path, kwargs))
        return [{"symbol": "BTCUSDT"}, "ignored"]

    monkeypatch.setattr(binance, "_get_json", fake_get_json)

    assert binance.fetch_spot_prices(timeout_seconds=5) == [{"symbol": "BTCUSDT"}]
    assert calls == [("/api/v3/ticker/24hr", {"timeout_seconds": 5})]


def test_fetch_futures_prices_uses_futures_ticker_endpoint(monkeypatch) -> None:
    calls = []

    def fake_get_json(path, **kwargs):
        calls.append((path, kwargs))
        return [{"symbol": "BTCUSDT"}]

    monkeypatch.setattr(binance, "_get_json", fake_get_json)

    assert binance.fetch_futures_prices(timeout_seconds=5) == [{"symbol": "BTCUSDT"}]
    assert calls == [
        (
            "/fapi/v1/ticker/24hr",
            {
                "timeout_seconds": 5,
                "api_base": binance.BINANCE_FUTURES_API_BASE,
            },
        )
    ]


def test_fetch_public_assets_uses_binance_website_endpoint(monkeypatch) -> None:
    calls = []

    def fake_get_json(path, **kwargs):
        calls.append((path, kwargs))
        return {"data": [{"assetCode": "BTC"}, {"assetName": "missing-code"}]}

    monkeypatch.setattr(binance, "_get_json", fake_get_json)

    assert binance.fetch_public_assets(timeout_seconds=5) == [{"assetCode": "BTC"}]
    assert calls == [
        (
            "/bapi/asset/v2/public/asset/asset/get-all-asset",
            {
                "timeout_seconds": 5,
                "api_base": binance.BINANCE_WEBSITE_API_BASE,
            },
        )
    ]
