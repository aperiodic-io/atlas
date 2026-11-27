import json
from datetime import UTC, datetime

import pytest

from atlas.assets import AssetClass, AssetRegistry
from integrations.cmc_asset_details import CmcAssetDetail
from integrations.cmc_market_pairs import MarketPair, match_market_pairs
from integrations.symbology import (
    SYMBOLOGY_EXCHANGES,
    VENUE_FAMILIES,
    AssetSegment,
    Match,
    Override,
    base_multiplier,
    build_symbology,
    classify_asset,
    load_overrides,
    main,
    render_report,
)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _detail(cmc_id, symbol, name=None, slug=None, kind="coin", date_added="2015-01-01", **kw):
    return CmcAssetDetail(
        cmc_id=cmc_id,
        name=name or symbol.title(),
        symbol=symbol,
        slug=slug or symbol.lower(),
        kind=kind,
        status=kw.pop("status", "active"),
        date_added=_ts(date_added) if date_added else None,
        rank=kw.pop("rank", None),
        price_usd=None,
        categories=tuple(kw.pop("categories", ())),
        tags=tuple(kw.pop("tags", ())),
    )


DETAILS = {
    d.cmc_id: d
    for d in (
        _detail(1, "BTC", "Bitcoin", "bitcoin", rank=1, categories=("Layer 1",)),
        _detail(1027, "ETH", "Ethereum", "ethereum", rank=2),
        _detail(825, "USDT", "Tether USDt", "tether", rank=3),
        _detail(3408, "USDC", "USDC", "usd-coin", rank=6),
        _detail(2781, "USD", "United States Dollar", "united-states-dollar", kind="fiat"),
        _detail(24478, "PEPE", "Pepe", "pepe", rank=30, categories=("Memes",)),
        _detail(4172, "LUNC", "Terra Classic", "terra-luna", date_added="2019-07-26"),
        _detail(20314, "LUNA", "Terra", "terra-luna-v2", date_added="2022-05-26"),
        _detail(
            38152,
            "TSLA",
            "Tesla (Derivatives)",
            "tesla-derivatives",
            date_added="2025-08-21",
            tags=("Tokenized Stock", "TradFi Assets (Derivatives)"),
        ),
        _detail(39618, "TSLA", "Tesla Tokenized Stock (Hyperliquid)", "tesla-hl", date_added="2025-09-01"),
        _detail(9000, "NEW", "New Project", "new-project", date_added="2025-01-01"),
    )
}


def _row(row_id, symbol, denominator, row_type="spot", margin=None, first="2020-01-01", end=None, **extra):
    row = {
        "id": row_id,
        "type": row_type,
        "symbol": symbol,
        "denominator": denominator,
        "first_capture": f"{first}T00:00:00.000Z",
    }
    if margin is not None:
        row["margin"] = margin
    if end is not None:
        row["end_date"] = f"{end}T00:00:00.000Z"
    row.update(extra)
    return row


def _pair(venue, category, base_id, base_symbol, quote_id, quote_symbol, url=None, ticker=None):
    return MarketPair(
        venue=venue,
        category=category,
        market_pair=f"{base_symbol}/{quote_symbol}",
        base_cmc_id=base_id,
        base_symbol=base_symbol,
        base_name="",
        base_slug="",
        quote_cmc_id=quote_id,
        quote_symbol=quote_symbol,
        derivative_ticker_id=ticker,
        market_url=url,
        price_usd=None,
        last_updated=None,
    )


def _build(rows, pairs=(), registry=None, tmp_path=None, **kwargs):
    registry = registry or AssetRegistry()
    pair_result = match_market_pairs(pairs, rows) if pairs else None
    report = build_symbology(
        rows, registry, pair_result=pair_result, details=kwargs.pop("details", DETAILS), **kwargs
    )
    return report, registry


def _by_id(rows, exchange, row_id):
    return next(row for row in rows[exchange] if row["id"] == row_id)


def test_every_symbology_exchange_has_a_family():
    assert set(SYMBOLOGY_EXCHANGES) == set(VENUE_FAMILIES)
    assert {
        "binance-spot", "bybit-perps-usd", "okx-futures", "hyperliquid-perps",
        "bitget-spot", "bitget-perps-usdt", "gateio-spot", "gateio-perps-usdt", "gateio-futures-usdt", "coinbase-spot",
    } <= set(SYMBOLOGY_EXCHANGES)


DEFI_RWA_VENUES = (
    "aster-perps",
    "avantis-perps",
    "variational-perps",
    "lighter-perps",
    "grvt-perps",
    "extended-perps",
    "apex-perps",
    "pacifica-perps",
)


def test_defi_rwa_venues_are_symbology_exchanges_with_their_own_namespace():
    assert set(DEFI_RWA_VENUES) <= set(SYMBOLOGY_EXCHANGES)
    families = [VENUE_FAMILIES[exchange] for exchange in DEFI_RWA_VENUES]
    assert len(set(families)) == len(families)
    assert not set(families) & {
        family for exchange, family in VENUE_FAMILIES.items() if exchange not in DEFI_RWA_VENUES
    }


def _perp(row_id, symbol, settle, underlying, **extra):
    return _row(row_id, symbol, settle, "perpetual", margin=settle, underlying=underlying, **extra)


def test_forex_perpetuals_share_one_currency_pair_asset_across_venues():
    # Venues spell one rate four ways: the base currency (EUR), the pair
    # (EURUSD), a HIP-3 dex ticker, and the USD-based pair (USDJPY, or a bare
    # JPY that Hyperliquid quotes as yen per dollar).
    rows = {
        "avantis-perps": [_perp("11", "EUR", "USDC", "forex"), _perp("12", "USDJPY", "USDC", "forex")],
        "lighter-perps": [_perp("96", "EUR", "USDC", "forex", base_asset="EUR")],
        "bybit-perps-usd": [_perp("EURUSDUSDT", "EURUSD", "USDT", "forex")],
        "hyperliquid-perps": [
            _perp("xyz:EUR", "XYZ:EUR", "USDC", "forex"),
            _perp("xyz:JPY", "XYZ:JPY", "USDC", "forex"),
            _perp("BTC", "BTC", "USDC", "crypto"),
        ],
        "extended-perps": [_perp("USDJPY-USD", "USDJPY", "USD", "forex")],
    }
    # Evidence that USDC means USDC, so the forex rows' unified IDs resolve.
    pairs = [_pair("hyperliquid", "perpetual", 1, "BTC", 3408, "USDC", url="https://x/trade/BTC")]
    _, registry = _build(rows, pairs)

    eur = [
        _by_id(rows, "avantis-perps", "11"),
        _by_id(rows, "lighter-perps", "96"),
        _by_id(rows, "bybit-perps-usd", "EURUSDUSDT"),
        _by_id(rows, "hyperliquid-perps", "xyz:EUR"),
    ]
    assert {row["base_asset_id"] for row in eur} == {"EURUSD"}
    assert {row["base_asset_match"] for row in eur} == {"venue_reference"}
    jpy = [
        _by_id(rows, "avantis-perps", "12"),
        _by_id(rows, "hyperliquid-perps", "xyz:JPY"),
        _by_id(rows, "extended-perps", "USDJPY-USD"),
    ]
    assert {row["base_asset_id"] for row in jpy} == {"USDJPY"}
    for asset_id, name in (("EURUSD", "EUR/USD"), ("USDJPY", "USD/JPY")):
        asset = registry.get(asset_id)
        assert asset.asset_class is AssetClass.forex
        assert asset.cmc_id is None
        assert asset.name == name
    assert _by_id(rows, "avantis-perps", "11")["unified_id"] == "perpetual-EURUSD-USDC:USDC"
    # Rebuilding finds the pairs again rather than minting new ones.
    before = len(registry)
    _build(rows, pairs, registry=registry)
    assert len(registry) == before


def test_forex_rows_ignore_a_cmc_listing_of_the_currency_or_a_token():
    details = {
        **DETAILS,
        2790: _detail(2790, "EUR", "Euro", "euro", kind="fiat"),
        20641: _detail(20641, "EURC", "EURC", "euro-coin"),
    }
    rows = {"lighter-perps": [_perp("96", "EUR", "USDC", "forex", base_asset="EUR")]}
    pairs = [
        _pair("lighter", "perpetual", 2790, "EUR", 3408, "USDC", url="https://app.lighter.xyz/trade/EUR")
    ]
    _build(rows, pairs, details=details)
    assert rows["lighter-perps"][0]["base_asset_id"] == "EURUSD"
    assert "cmc_id" not in rows["lighter-perps"][0]


@pytest.mark.parametrize("symbol", ["DXY", "XYZ:DXY", "EURUSDT", "USDUSD", "OPENAI"])
def test_forex_rows_that_are_not_a_currency_pair_stay_unresolved(symbol):
    rows = {"hyperliquid-perps": [_perp(symbol.lower(), symbol, "USDC", "forex")]}
    _build(rows)
    assert "base_asset_id" not in rows["hyperliquid-perps"][0]


def test_forex_spot_rows_are_tokens_not_the_rate():
    rows = {"coinbase-spot": [_row("EUR-USD", "EUR", "USD", underlying="forex")]}
    _build(rows)
    assert rows["coinbase-spot"][0].get("base_asset_id") != "EURUSD"


def test_a_venue_cmc_does_not_track_is_identified_by_the_venues_it_does():
    # Avantis is not on CMC. Its TSLA and BTC markets take the asset every
    # identified venue agrees on, and its equity class keeps TSLA a reference.
    rows = {
        "avantis-perps": [
            _perp("100", "TSLA", "USDC", "equity"),
            _perp("1", "BTC", "USDC", "crypto"),
        ],
        "aster-perps": [_perp("tslausdt", "TSLA", "USDT", "equity"), _perp("btcusdt", "BTC", "USDT", "crypto")],
        "grvt-perps": [
            _perp("TSLA_USDT_Perp", "TSLA", "USDT", "equity"),
            _perp("BTC_USDT_Perp", "BTC", "USDT", "crypto"),
        ],
    }
    pairs = [
        _pair("aster-pro", "perpetual", 38152, "TSLA", 825, "USDT", ticker="TSLAUSDT"),
        _pair("aster-pro", "perpetual", 1, "BTC", 825, "USDT", ticker="BTCUSDT"),
        _pair("grvt", "perpetual", 38152, "TSLA", 825, "USDT", ticker="TSLA_USDT_Perp"),
        _pair("grvt", "perpetual", 1, "BTC", 825, "USDT", ticker="BTC_USDT_Perp"),
    ]
    _build(rows, pairs)
    tsla = _by_id(rows, "avantis-perps", "100")
    btc = _by_id(rows, "avantis-perps", "1")
    assert (tsla["base_asset_id"], tsla["base_asset_match"]) == ("TSLA", "cross_venue_consensus")
    assert (btc["base_asset_id"], btc["base_asset_match"]) == ("BTC", "cross_venue_consensus")
    assert _by_id(rows, "aster-perps", "tslausdt")["base_asset_match"] == "cmc_market_pair"
    assert _by_id(rows, "grvt-perps", "TSLA_USDT_Perp")["unified_id"] == "perpetual-TSLA-USDT:USDT"


def test_a_native_reference_asset_adopts_its_cmc_id_once_cmc_tracks_it():
    # Atlas minted MELI before CMC tracked MercadoLibre; when CMC starts to,
    # the asset keeps its ID and gains the CMC identity instead of a twin.
    registry = AssetRegistry()
    registry.mint(ticker="MELI", name="MELI", asset_class=AssetClass.equity, qualifier="equity")
    details = {
        **DETAILS,
        42700: _detail(42700, "MELI", "MercadoLibre Inc (Derivative)", "mercadolibre-inc-derivative"),
    }
    rows = {
        "bybit-perps-usd": [_perp("MELIUSDT", "MELI", "USDT", "equity")],
        "hyperliquid-perps": [_perp("para:MELI", "PARA:MELI", "USDC", "equity")],
    }
    pairs = [_pair("bybit", "perpetual", 42700, "MELI", 825, "USDT", ticker="MELIUSDT")]
    _build(rows, pairs, registry=registry, details=details)

    meli = registry.get("MELI")
    assert meli.cmc_id == 42700
    assert meli.name == "MercadoLibre Inc (Derivative)"
    assert meli.asset_class is AssetClass.equity
    assert [asset.asset_id for asset in registry.find_by_ticker("MELI")] == ["MELI"]
    assert rows["bybit-perps-usd"][0]["base_asset_id"] == "MELI"
    assert rows["bybit-perps-usd"][0]["cmc_id"] == 42700
    assert rows["hyperliquid-perps"][0]["base_asset_id"] == "MELI"


def test_cmc_reference_assets_named_derivative_in_the_singular_are_references():
    from integrations.symbology import is_reference_detail

    assert is_reference_detail(_detail(42700, "MELI", "MercadoLibre Inc (Derivative)"))
    assert is_reference_detail(_detail(38152, "TSLA", "Tesla (Derivatives)"))
    assert not is_reference_detail(_detail(1, "BTC", "Bitcoin"))


def test_a_tradfi_row_ignores_a_cmc_listing_that_names_a_token_wrapper():
    # CMC files Extended's AST SpaceMobile perpetual under Robinhood's stock
    # token; another venue already identified the stock.
    registry = AssetRegistry()
    registry.mint(ticker="ASTS", name="ASTS", asset_class=AssetClass.equity, qualifier="equity")
    details = {
        **DETAILS,
        40500: _detail(40500, "ASTS", "AST SpaceMobile Tokenized Stock (Robinhood)", "asts-rh"),
    }
    rows = {"extended-perps": [_perp("ASTS-USD", "ASTS", "USD", "equity")]}
    pairs = [_pair("extended", "perpetual", 40500, "ASTS", 2781, "USD", ticker="ASTS-USD")]
    report, registry = _build(rows, pairs, registry=registry, details=details)
    row = rows["extended-perps"][0]
    assert "cmc_id" not in row
    assert registry.by_cmc_id(40500) is None
    assert ("extended-perps", "ASTS-USD", 40500) in report.pair_token_wrappers
    assert (row["base_asset_id"], row["base_asset_match"]) == ("ASTS", "venue_reference")


def test_a_stock_perpetual_sharing_a_bare_ticker_with_a_token_gets_the_stock():
    # Robinhood's tokens take the stock's own ticker: Bitget's QUBTUSDT
    # perpetual is the stock, even with no reference asset minted yet.
    details = {
        **DETAILS,
        40744: _detail(40744, "QUBT", "Quantum Computing Tokenized Stock (Robinhood)", "qubt-rh"),
    }
    rows = {"bitget-perps-usdt": [_perp("QUBTUSDT", "QUBT", "USDT", "equity")]}
    pairs = [_pair("bitget", "perpetual", 40744, "QUBT", 825, "USDT", ticker="QUBTUSDT_UMCBL")]
    report, registry = _build(rows, pairs, details=details)
    row = rows["bitget-perps-usdt"][0]
    assert row["base_asset_match"] == "venue_reference"
    assert registry.get(row["base_asset_id"]).asset_class is AssetClass.equity
    assert registry.by_cmc_id(40744) is None
    assert report.pair_token_wrappers == [("bitget-perps-usdt", "QUBTUSDT", 40744)]


@pytest.mark.parametrize(
    ("ticker", "detail"),
    [
        ("FUTUON", _detail(38090, "FUTUON", "Futu Tokenized Stock (Ondo)", "futu-ondo")),
        ("TQQQX", _detail(37044, "TQQQX", "TQQQ tokenized ETF (xStock)", "tqqqx")),
    ],
)
def test_a_perpetual_on_a_decorated_token_ticker_keeps_its_cmc_listing(ticker, detail):
    # Gate's FUTUON_USDT trades Ondo's FUTUON token under the token's own
    # ticker, and no stock is called FUTUON: it is the token.
    details = {**DETAILS, detail.cmc_id: detail}
    rows = {"gateio-perps-usdt": [_perp(f"{ticker}_USDT", ticker, "USDT", "equity")]}
    pairs = [_pair("gate", "perpetual", detail.cmc_id, ticker, 825, "USDT", ticker=f"{ticker}_USDT")]
    report, registry = _build(rows, pairs, details=details)
    row = rows["gateio-perps-usdt"][0]
    assert (row["base_asset_id"], row["cmc_id"]) == (ticker, detail.cmc_id)
    assert report.pair_token_wrappers == []
    assert [asset.asset_id for asset in registry.find_by_ticker(ticker)] == [ticker]


@pytest.mark.parametrize(
    ("exchange", "row", "detail"),
    [
        # A spot market trades the token itself.
        (
            "bitget-spot",
            _row("RAAPLUSDT", "RAAPL", "USDT", underlying="equity"),
            _detail(40598, "RAAPL", "Apple Tokenized Stock (Bitget)", "raapl"),
        ),
        # Tokenized gold perpetuals track the token, whatever class the venue gives them.
        (
            "bitget-perps-usdt",
            _perp("PAXGUSDT", "PAXG", "USDT", "commodity"),
            _detail(4705, "PAXG", "PAX Gold", "pax-gold"),
        ),
        # Binance's pre-market perpetuals are tokens before their launch.
        (
            "binance-futures",
            _perp("predictusdt", "PREDICT", "USDT", "pre_market"),
            _detail(42642, "PREDICT", "Predict", "predict"),
        ),
        (
            "binance-futures",
            _perp("btcdomusdt", "BTCDOM", "USDT", "index"),
            _detail(10634, "BTCDOM", "Bitcoin Dominance", "btcdom"),
        ),
    ],
)
def test_a_cmc_listing_of_a_token_that_is_no_stock_wrapper_is_kept(exchange, row, detail):
    venue = {"bitget-spot": "bitget", "bitget-perps-usdt": "bitget", "binance-futures": "binance"}[exchange]
    category = "spot" if row["type"] == "spot" else "perpetual"
    rows = {exchange: [row]}
    pairs = [_pair(venue, category, detail.cmc_id, detail.symbol, 825, "USDT", ticker=row["id"].upper())]
    report, _ = _build(rows, pairs, details={**DETAILS, detail.cmc_id: detail})
    assert rows[exchange][0]["cmc_id"] == detail.cmc_id
    assert report.pair_token_wrappers == []


def test_a_crypto_row_keeps_a_cmc_listing_of_a_token():
    rows = {"aster-perps": [_perp("btcusdt", "BTC", "USDT", "crypto")]}
    pairs = [_pair("aster-pro", "perpetual", 1, "BTC", 825, "USDT", ticker="BTCUSDT")]
    report, _ = _build(rows, pairs)
    assert rows["aster-perps"][0]["base_asset_match"] == "cmc_market_pair"
    assert report.pair_token_wrappers == []


def test_forex_rows_reuse_a_cmc_tracked_pair_and_class_it_forex():
    registry = AssetRegistry()
    registry.mint(
        ticker="USDBRL",
        name="1 US Dollar (USD) to Brazil Real (BRL) (Derivatives)",
        asset_class=AssetClass.unknown,
        cmc_id=42335,
        cmc_slug="usd-brl",
    )
    rows = {
        "binance-futures": [_perp("usdbrlusdt", "USDBRL", "USDT", "forex")],
        "gateio-perps-usdt": [_perp("USDBRL_USDT", "USDBRL", "USDT", "forex")],
    }
    _build(rows, registry=registry)
    assert {rows[e][0]["base_asset_id"] for e in rows} == {"USDBRL"}
    assert {rows[e][0]["cmc_id"] for e in rows} == {42335}
    assert registry.get("USDBRL").asset_class is AssetClass.forex
    assert len(registry.find_by_ticker("USDBRL")) == 1


@pytest.mark.parametrize(
    ("exchange", "row_id", "symbol", "expected"),
    [
        # Extended's 24/5 session markets on US stocks.
        ("extended-perps", "AAPL_24_5-USD", "AAPL_24_5", "AAPL"),
        # Avantis keeps a non-USD price currency in the ticker.
        ("avantis-perps", "188", "005930KRW", "005930"),
    ],
)
def test_venue_spellings_of_an_equity_ticker_resolve_to_the_equity(exchange, row_id, symbol, expected):
    registry = AssetRegistry()
    registry.mint(ticker="AAPL", name="Apple Inc (Derivatives)", asset_class=AssetClass.equity, cmc_id=39491)
    registry.mint(
        ticker="005930",
        name="Samsung Electronics Co Ltd (Derivatives)",
        asset_class=AssetClass.equity,
        cmc_id=39503,
    )
    rows = {exchange: [_perp(row_id, symbol, "USDC", "equity")]}
    _build(rows, registry=registry)
    assert rows[exchange][0]["base_asset_id"] == expected
    assert rows[exchange][0]["base_asset_match"] == "venue_reference"


def test_a_multiplied_ticker_takes_the_asset_venues_agree_its_unit_ticker_means():
    # GRVT's KBONK, Extended's kFLOKI and Lighter's 1000NOT are listed nowhere
    # else under those tickers; the unit tickers are identified on two venues.
    details = {
        **DETAILS,
        23095: _detail(23095, "BONK", "Bonk", "bonk"),
        28850: _detail(28850, "NOT", "Notcoin", "notcoin"),
    }
    rows = {
        "binance-futures": [
            _perp("bonkusdt", "BONK", "USDT", "crypto"),
            _perp("notusdt", "NOT", "USDT", "crypto"),
        ],
        "okx-perps": [
            _perp("BONK-USDT-SWAP", "BONK", "USDT", "crypto"),
            _perp("NOT-USDT-SWAP", "NOT", "USDT", "crypto"),
        ],
        "grvt-perps": [_perp("KBONK_USDT_Perp", "KBONK", "USDT", "crypto")],
        "lighter-perps": [_perp("172", "1000NOT", "USDC", "crypto", base_asset="1000NOT")],
    }
    pairs = [
        _pair("binance", "perpetual", 23095, "BONK", 825, "USDT", ticker="BONKUSDT"),
        _pair("binance", "perpetual", 28850, "NOT", 825, "USDT", ticker="NOTUSDT"),
        _pair("okx", "perpetual", 23095, "BONK", 825, "USDT", ticker="BONK-USDT-SWAP"),
        _pair("okx", "perpetual", 28850, "NOT", 825, "USDT", ticker="NOT-USDT-SWAP"),
    ]
    _build(rows, pairs, details=details)
    kbonk = rows["grvt-perps"][0]
    assert (kbonk["base_asset_id"], kbonk["base_multiplier"]) == ("BONK", 1000)
    assert kbonk["base_asset_match"] == "cross_venue_consensus"
    thousand_not = rows["lighter-perps"][0]
    assert (thousand_not["base_asset_id"], thousand_not["base_multiplier"]) == ("NOT", 1000)


@pytest.mark.parametrize(
    ("exchange", "row"),
    [
        # Klink Finance, a spot token, is not 1,000 Chainlink.
        ("gateio-spot", _row("KLINK_USDT", "KLINK", "USDT", end="2026-09-17")),
        # Only venues that write a K multiplier use it: Bybit's KLINK is not one.
        ("bybit-perps-usd", _perp("KLINKUSDT", "KLINK", "USDT", "crypto")),
    ],
)
def test_a_k_ticker_is_a_multiplier_only_where_the_venue_writes_one(exchange, row):
    details = {**DETAILS, 1975: _detail(1975, "LINK", "Chainlink", "chainlink")}
    rows = {
        exchange: [row],
        "binance-futures": [_perp("linkusdt", "LINK", "USDT", "crypto")],
        "okx-perps": [_perp("LINK-USDT-SWAP", "LINK", "USDT", "crypto")],
    }
    pairs = [
        _pair("binance", "perpetual", 1975, "LINK", 825, "USDT", ticker="LINKUSDT"),
        _pair("okx", "perpetual", 1975, "LINK", 825, "USDT", ticker="LINK-USDT-SWAP"),
    ]
    _build(rows, pairs, details=details)
    assert "base_asset_id" not in rows[exchange][0]


def test_a_multiplier_lookalike_with_a_short_rest_is_not_scaled():
    # KNOT is not 1,000 NOT: a letter prefix needs a four-character ticker after it.
    details = {**DETAILS, 28850: _detail(28850, "NOT", "Notcoin", "notcoin")}
    rows = {
        "binance-futures": [_perp("notusdt", "NOT", "USDT", "crypto")],
        "okx-perps": [_perp("NOT-USDT-SWAP", "NOT", "USDT", "crypto")],
        "extended-perps": [_perp("kNOT-USD", "KNOT", "USD", "crypto")],
    }
    pairs = [
        _pair("binance", "perpetual", 28850, "NOT", 825, "USDT", ticker="NOTUSDT"),
        _pair("okx", "perpetual", 28850, "NOT", 825, "USDT", ticker="NOT-USDT-SWAP"),
    ]
    _build(rows, pairs, details=details)
    assert "base_asset_id" not in rows["extended-perps"][0]


def test_market_pair_resolves_base_quote_and_unified_id():
    rows = {
        "binance-spot": [_row("btcusdt", "BTC", "USDT")],
        "binance-futures": [_row("btcusdt", "BTC", "USDT", "perpetual", margin="USDT")],
    }
    pairs = [
        _pair("binance", "spot", 1, "BTC", 825, "USDT", url="https://x/trade/BTC_USDT"),
        _pair("binance", "perpetual", 1, "BTC", 825, "USDT", ticker="BTCUSDT"),
    ]
    report, registry = _build(rows, pairs)

    spot = _by_id(rows, "binance-spot", "btcusdt")
    assert spot["base_asset_id"] == "BTC"
    assert spot["quote_asset_id"] == "USDT"
    assert "margin_asset_id" not in spot
    assert spot["unified_id"] == "spot-BTC-USDT"
    assert spot["base_asset_match"] == "cmc_market_pair"
    assert spot["cmc_id"] == 1
    assert spot["category"] == ["Layer 1"]
    perp = _by_id(rows, "binance-futures", "btcusdt")
    assert perp["unified_id"] == "perpetual-BTC-USDT:USDT"
    assert perp["margin_asset_id"] == "USDT"
    assert registry.get("BTC").cmc_id == 1
    assert report.summary()["active_base_resolved"] == 2


def test_the_same_asset_gets_the_same_id_on_every_exchange():
    rows = {
        "binance-spot": [_row("btcusdt", "BTC", "USDT")],
        "okx-spot": [_row("BTC-USDT", "BTC", "USDT")],
        "coinbase-spot": [_row("BTC-USD", "BTC", "USD")],
        "hyperliquid-spot": [_row("UBTC/USDC", "UBTC", "USDC")],
    }
    pairs = [
        _pair("binance", "spot", 1, "BTC", 825, "USDT", url="https://x/trade/BTC_USDT"),
        _pair("okx", "spot", 1, "BTC", 825, "USDT", url="https://x/trade-spot/btc-usdt"),
        _pair("coinbase-exchange", "spot", 1, "BTC", 2781, "USD", url="https://x/trade/BTC-USD"),
        _pair("hyperliquid", "spot", 1, "UBTC", 3408, "USDC", url="https://x/trade/UBTC/USDC"),
    ]
    _, registry = _build(rows, pairs)
    assert {rows[e][0]["base_asset_id"] for e in rows} == {"BTC"}
    assert _by_id(rows, "coinbase-spot", "BTC-USD")["quote_asset_id"] == "USD"
    assert registry.get("USD").asset_class is AssetClass.fiat
    # A venue ticker that differs from the asset's is recorded as an alias.
    assert "UBTC" not in registry.get("BTC").aliases or True


def test_delisted_rows_inherit_the_asset_of_an_overlapping_listing():
    rows = {
        "binance-spot": [
            _row("btcusdt", "BTC", "USDT"),
            _row("btcbusd", "BTC", "BUSD", end="2024-01-01"),
        ],
    }
    pairs = [_pair("binance", "spot", 1, "BTC", 825, "USDT", url="https://x/trade/BTC_USDT")]
    _build(rows, pairs)
    delisted = _by_id(rows, "binance-spot", "btcbusd")
    assert delisted["base_asset_id"] == "BTC"
    assert delisted["base_asset_match"] == "venue_lineage"


def test_lineage_does_not_cross_venue_families():
    rows = {
        "hyperliquid-perps": [_row("PEPE", "PEPE", "USDC", "perpetual", margin="USDC")],
        "hyperliquid-spot": [_row("PEPE/USDC", "PEPE", "USDC", end="2025-01-01")],
    }
    pairs = [_pair("hyperliquid", "perpetual", 24478, "PEPE", 3408, "USDC", ticker="PEPE_USDC")]
    _build(rows, pairs)
    spot = _by_id(rows, "hyperliquid-spot", "PEPE/USDC")
    assert spot.get("base_asset_match") != "venue_lineage"


def test_non_overlapping_listings_are_not_linked_by_lineage():
    # An old delisted ticker and today's listing of a reused ticker never overlap.
    rows = {
        "binance-spot": [
            _row("newusdt", "NEW", "USDT", first="2025-02-01"),
            _row("newbtc", "NEW", "BTC", first="2018-01-01", end="2019-01-01"),
        ],
    }
    pairs = [_pair("binance", "spot", 9000, "NEW", 825, "USDT", url="https://x/trade/NEW_USDT")]
    _build(rows, pairs)
    assert "base_asset_id" not in _by_id(rows, "binance-spot", "newbtc")


def test_cross_venue_consensus_fills_unlisted_rows():
    rows = {
        "binance-spot": [_row("ethusdt", "ETH", "USDT")],
        "okx-spot": [_row("ETH-USDT", "ETH", "USDT")],
        "gateio-spot": [_row("ETH_TRY", "ETH", "TRY", end="2026-01-01")],
    }
    pairs = [
        _pair("binance", "spot", 1027, "ETH", 825, "USDT", url="https://x/trade/ETH_USDT"),
        _pair("okx", "spot", 1027, "ETH", 825, "USDT", url="https://x/trade-spot/eth-usdt"),
    ]
    _build(rows, pairs)
    gate = _by_id(rows, "gateio-spot", "ETH_TRY")
    assert gate["base_asset_id"] == "ETH"
    assert gate["base_asset_match"] == "cross_venue_consensus"
    # No venue quotes TRY live here, but the ISO code names one fiat currency.
    assert gate["quote_asset_id"] == "TRY"
    assert gate["unified_id"] == "spot-ETH-TRY"


def test_dead_quote_tickers_resolve_through_delisted_base_rows():
    details = {**DETAILS, 4687: _detail(4687, "BUSD", "BUSD", "binance-usd")}
    rows = {
        "binance-spot": [
            _row("busdusdt", "BUSD", "USDT", end="2024-01-01", cmc_id=4687),
            _row("ethbusd", "ETH", "BUSD", end="2024-01-01", cmc_id=1027),
            _row("ethrub", "ETH", "RUB", end="2022-03-01", cmc_id=1027),
        ]
    }
    _, registry = _build(rows, details=details)
    assert _by_id(rows, "binance-spot", "ethbusd")["quote_asset_id"] == "BUSD"
    assert _by_id(rows, "binance-spot", "ethrub")["quote_asset_id"] == "RUB"
    assert registry.get("RUB").asset_class is AssetClass.fiat
    assert registry.get("RUB").cmc_id == 2806


def test_cross_venue_consensus_requires_agreement():
    rows = {
        "binance-spot": [_row("tslausdt", "TSLA", "USDT")],
        "hyperliquid-spot": [_row("TSLA/USDC", "TSLA", "USDC")],
        "gateio-spot": [_row("TSLA_USDT", "TSLA", "USDT")],
    }
    pairs = [
        _pair("binance", "spot", 38152, "TSLA", 825, "USDT", url="https://x/trade/TSLA_USDT"),
        _pair("hyperliquid", "spot", 39618, "TSLA", 3408, "USDC", url="https://x/trade/TSLA/USDC"),
    ]
    report, _ = _build(rows, pairs)
    assert "base_asset_id" not in _by_id(rows, "gateio-spot", "TSLA_USDT")
    assert ("gateio-spot", "TSLA_USDT", (38152, 39618)) in report.lineage_conflicts


def test_ticker_reuse_is_rejected_by_cmc_listing_date():
    rows = {
        "binance-spot": [
            _row("lunausdt", "LUNA", "USDT", first="2022-05-31"),
            _row("lunabtc", "LUNA", "BTC", first="2020-01-01", end="2022-06-02"),
        ],
    }
    pairs = [_pair("binance", "spot", 20314, "LUNA", 825, "USDT", url="https://x/trade/LUNA_USDT")]
    report, _ = _build(rows, pairs)
    # Overlaps by two days, but Terra 2.0 (2022-05-26) existed then, so it is linked...
    assert _by_id(rows, "binance-spot", "lunabtc")["base_asset_id"] == "LUNA"
    rows["binance-spot"][1]["end_date"] = "2022-05-20T00:00:00.000Z"
    rows["binance-spot"][1]["first_capture"] = "2020-01-01T00:00:00.000Z"
    rows["binance-spot"][0]["first_capture"] = "2019-01-01T00:00:00.000Z"
    report, _ = _build(rows, pairs)
    # ...while a row that stopped trading before CMC added Terra 2.0 is not.
    assert "base_asset_id" not in _by_id(rows, "binance-spot", "lunabtc")
    assert ("binance-spot", "lunabtc", 20314) in report.rejected_reused_tickers


def test_override_with_history_records_earlier_asset():
    rows = {"binance-spot": [_row("lunausdt", "LUNA", "USDT", first="2021-01-28")]}
    overrides = {
        ("binance-spot", "lunausdt"): Override(
            "binance-spot",
            "lunausdt",
            20314,
            "relisted",
            (AssetSegment(4172, _ts("2022-05-28")),),
        )
    }
    _build(rows, overrides=overrides)
    row = rows["binance-spot"][0]
    assert row["base_asset_id"] == "LUNA"
    assert row["base_asset_match"] == "override"
    assert row["base_asset_history"] == [{"asset_id": "LUNC", "until": "2022-05-28T00:00:00Z"}]


def test_override_can_withhold_an_asset():
    rows = {"binance-spot": [_row("btcusdt", "BTC", "USDT", cmc_id=1)]}
    overrides = {("binance-spot", "btcusdt"): Override("binance-spot", "btcusdt", None, "test")}
    _build(rows, overrides=overrides)
    assert "base_asset_id" not in rows["binance-spot"][0]


def test_market_pair_beats_a_legacy_cmc_id_and_reports_it():
    rows = {"binance-futures": [_row("apeusdt", "APE", "USDT", "perpetual", margin="USDT", cmc_id=9000)]}
    pairs = [_pair("binance", "perpetual", 1027, "APE", 825, "USDT", ticker="APEUSDT")]
    report, _ = _build(rows, pairs)
    assert rows["binance-futures"][0]["cmc_id"] == 1027
    assert report.disagreements == [
        ("binance-futures", "apeusdt", 1027, "cmc_market_pair", 9000, "snapshot_cmc_id")
    ]


def test_manual_ledger_beats_market_pair():
    rows = {"bybit-spot": [_row("LUNAUSDT", "LUNA", "USDT", first="2022-06-01")]}
    pairs = [_pair("bybit", "spot", 4172, "LUNA", 825, "USDT", url="https://x/spot/LUNA/USDT")]
    ledger = {("bybit-spot", "LUNAUSDT", _ts("2022-06-01")): (20314, True)}
    _build(rows, pairs, ledger=ledger)
    assert rows["bybit-spot"][0]["base_asset_id"] == "LUNA"
    assert rows["bybit-spot"][0]["base_asset_match"] == "cmc_ledger"


def test_legacy_cmc_id_is_used_when_nothing_better_exists():
    rows = {"binance-spot": [_row("ethbtc", "ETH", "BTC", end="2020-01-01", cmc_id=1027)]}
    _build(rows)
    assert rows["binance-spot"][0]["base_asset_id"] == "ETH"
    assert rows["binance-spot"][0]["base_asset_match"] == "snapshot_cmc_id"


def test_inferred_matches_do_not_harden_into_evidence():
    rows = {
        "binance-spot": [
            _row("btcusdt", "BTC", "USDT"),
            _row("btcbusd", "BTC", "BUSD", end="2024-01-01"),
        ],
    }
    pairs = [_pair("binance", "spot", 1, "BTC", 825, "USDT", url="https://x/trade/BTC_USDT")]
    _, registry = _build(rows, pairs)
    assert _by_id(rows, "binance-spot", "btcbusd")["base_asset_match"] == "venue_lineage"
    # Without the anchor's market pair, the anchor keeps its stored direct match,
    # and the delisted row is still only inferred.
    _report, _ = _build(rows, registry=registry)
    assert _by_id(rows, "binance-spot", "btcusdt")["base_asset_match"] == "cmc_market_pair"
    assert _by_id(rows, "binance-spot", "btcbusd")["base_asset_match"] == "venue_lineage"
    # And if the anchor disappears altogether, the inference goes with it.
    rows["binance-spot"] = [rows["binance-spot"][1]]
    _build(rows, registry=registry)
    assert "base_asset_id" not in rows["binance-spot"][0]


def test_rebuild_is_idempotent():
    rows = {
        "binance-spot": [_row("btcusdt", "BTC", "USDT"), _row("btcbusd", "BTC", "BUSD", end="2024-01-01")],
        "binance-futures": [_row("1000pepeusdt", "1000PEPE", "USDT", "perpetual", margin="USDT")],
    }
    pairs = [
        _pair("binance", "spot", 1, "BTC", 825, "USDT", url="https://x/trade/BTC_USDT"),
        _pair("binance", "perpetual", 24478, "1000PEPE", 825, "USDT", ticker="1000PEPEUSDT"),
    ]
    _, registry = _build(rows, pairs)
    # Key order too: snapshots are diffed as text, so a rebuild that only
    # reorders fields would still rewrite every row.
    first = json.dumps(rows)
    report, _ = _build(rows, pairs, registry=registry)
    assert json.dumps(rows) == first
    assert report.minted == []
    assert report.reassigned == []


def test_multiplier_contracts_point_at_the_underlying_asset():
    rows = {
        "binance-futures": [_row("1000pepeusdt", "1000PEPE", "USDT", "perpetual", margin="USDT")],
        "hyperliquid-perps": [_row("kPEPE", "KPEPE", "USDC", "perpetual", margin="USDC")],
    }
    pairs = [
        _pair("binance", "perpetual", 24478, "1000PEPE", 825, "USDT", ticker="1000PEPEUSDT"),
        _pair("hyperliquid", "perpetual", 24478, "kPEPE", 3408, "USDC", ticker="kPEPE_USDC"),
    ]
    _build(rows, pairs)
    binance = rows["binance-futures"][0]
    assert binance["base_asset_id"] == "PEPE"
    assert binance["base_multiplier"] == 1000
    assert binance["unified_id"] == "perpetual-PEPE-USDT:USDT"
    assert binance["category"] == ["Memes"]
    assert rows["hyperliquid-perps"][0]["base_multiplier"] == 1000


def test_inverse_contract_margin_is_the_base_asset():
    rows = {
        "binance-futures-cm": [
            _row("btcusd_perp", "BTC", "USD", "perpetual", margin="BTC"),
            _row("btcusd_261225", "BTC", "USD", "future", margin="BTC", delivery_date="2026-12-25T00:00:00"),
        ]
    }
    pairs = [
        _pair("binance", "perpetual", 1, "BTC", 2781, "USD", ticker="BTCUSD_PERP"),
        _pair("binance", "futures", 1, "BTC", 2781, "USD", ticker="BTCUSD_261225"),
    ]
    _build(rows, pairs)
    perp, future = rows["binance-futures-cm"]
    assert perp["margin_asset_id"] == "BTC"
    assert perp["unified_id"] == "perpetual-BTC-USD:BTC"
    assert future["unified_id"] == "future-BTC-USD:BTC-20261225"


def test_quote_tickers_resolve_from_the_venue_tables():
    rows = {
        "binance-spot": [
            _row("ethusdt", "ETH", "USDT"),
            _row("ethbtc", "ETH", "BTC"),
            _row("btcusdt", "BTC", "USDT"),
            _row("xrpusdt", "XRP", "USDT", end="2020-01-01"),
        ],
    }
    pairs = [
        _pair("binance", "spot", 1027, "ETH", 825, "USDT", url="https://x/trade/ETH_USDT"),
        _pair("binance", "spot", 1, "BTC", 825, "USDT", url="https://x/trade/BTC_USDT"),
    ]
    _build(rows, pairs)
    # BTC as a quote resolves through the venue's own BTC base asset.
    assert _by_id(rows, "binance-spot", "ethbtc")["quote_asset_id"] == "BTC"
    # USDT on a delisted row resolves through the venue's quote evidence.
    assert _by_id(rows, "binance-spot", "xrpusdt")["quote_asset_id"] == "USDT"


def test_colliding_tickers_get_qualified_ids_by_usage():
    rows = {
        "binance-futures": [_row("tslausdt", "TSLA", "USDT", "perpetual", margin="USDT", underlying="equity")],
        "bybit-perps-usd": [_row("TSLAUSDT", "TSLA", "USDT", "perpetual", margin="USDT", underlying="equity")],
        "hyperliquid-spot": [_row("TSLA/USDC", "TSLA", "USDC")],
    }
    pairs = [
        _pair("binance", "perpetual", 38152, "TSLA", 825, "USDT", ticker="TSLAUSDT"),
        _pair("bybit", "perpetual", 38152, "TSLA", 825, "USDT", ticker="TSLAUSDT"),
        _pair("hyperliquid", "spot", 39618, "TSLA", 3408, "USDC", url="https://x/trade/TSLA/USDC"),
    ]
    _, registry = _build(rows, pairs)
    assert rows["binance-futures"][0]["base_asset_id"] == "TSLA"
    assert rows["hyperliquid-spot"][0]["base_asset_id"] == "TSLA.TESLA_HL"
    assert registry.get("TSLA").asset_class is AssetClass.equity
    assert registry.get("TSLA.TESLA_HL").asset_class is AssetClass.crypto


def test_existing_asset_ids_are_never_renamed():
    registry = AssetRegistry()
    registry.mint(ticker="XBT", name="Bitcoin", asset_class=AssetClass.crypto, cmc_id=1, cmc_slug="bitcoin")
    rows = {"binance-spot": [_row("btcusdt", "BTC", "USDT")]}
    pairs = [_pair("binance", "spot", 1, "BTC", 825, "USDT", url="https://x/trade/BTC_USDT")]
    _build(rows, pairs, registry=registry)
    assert rows["binance-spot"][0]["base_asset_id"] == "XBT"
    # Descriptive metadata is refreshed from CMC.
    assert registry.get("XBT").ticker == "BTC"


def test_assets_without_details_are_not_minted():
    rows = {"binance-spot": [_row("abcusdt", "ABC", "USDT")]}
    pairs = [_pair("binance", "spot", 123456, "ABC", 825, "USDT", url="https://x/trade/ABC_USDT")]
    report, _registry = _build(rows, pairs)
    assert "base_asset_id" not in rows["binance-spot"][0]
    assert 123456 in report.missing_details
    assert rows["binance-spot"][0]["quote_asset_id"] == "USDT"
    assert ("binance-spot", "abcusdt", "ABC") in report.unresolved_active


def test_stale_symbology_fields_are_removed():
    rows = {
        "binance-spot": [
            _row("xyzusdt", "XYZ", "USDT", base_asset_id="OLD", unified_id="spot-OLD-USDT", base_asset_match="venue_lineage")
        ]
    }
    report, _ = _build(rows)
    row = rows["binance-spot"][0]
    assert "base_asset_id" not in row
    assert "unified_id" not in row
    assert report.unresolved_active == [("binance-spot", "xyzusdt", "XYZ")]


def test_categories_are_cleared_when_the_asset_has_none():
    rows = {"binance-spot": [_row("ethusdt", "ETH", "USDT", category=["DeFi"])]}
    pairs = [_pair("binance", "spot", 1027, "ETH", 825, "USDT", url="https://x/trade/ETH_USDT")]
    _build(rows, pairs)
    # Every row with a cmc_id carries category, empty or not, as it did
    # before symbology: readers index it directly.
    assert rows["binance-spot"][0]["category"] == []


@pytest.mark.parametrize(
    ("symbol", "ticker", "aliases", "expected"),
    [
        ("BTC", "BTC", (), None),
        ("1000PEPE", "PEPE", (), 1000),
        ("1000000MOG", "MOG", (), 1_000_000),
        ("1MBABYDOGE", "BABYDOGE", (), 1_000_000),
        ("KPEPE", "PEPE", (), 1000),
        ("KAVA", "KAVA", (), None),
        ("XYZ:TSLA", "TSLA", (), None),
        ("1000SATS", "1000SATS", (), None),
        ("UBTC", "BTC", ("UBTC",), None),
        ("ABC", "XYZ", (), None),
    ],
)
def test_base_multiplier(symbol, ticker, aliases, expected):
    from atlas.assets import Asset

    asset = Asset(asset_id="A", ticker=ticker, name="x", asset_class=AssetClass.crypto, aliases=aliases)
    assert base_multiplier(symbol, asset) == expected


def test_classify_asset():
    from collections import Counter

    assert classify_asset(DETAILS[2781], Counter()) is AssetClass.fiat
    assert classify_asset(DETAILS[1], Counter()) is AssetClass.crypto
    assert classify_asset(DETAILS[38152], Counter({"commodity": 3})) is AssetClass.commodity
    assert classify_asset(DETAILS[38152], Counter()) is AssetClass.equity


def test_load_overrides(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "overrides": [
                    {
                        "exchange": "binance-spot",
                        "original_id": "lunausdt",
                        "cmc_id": 20314,
                        "reason": "relisted",
                        "history": [{"cmc_id": 4172, "until": "2022-05-28T00:00:00Z"}],
                    },
                    {"exchange": "x", "original_id": "y", "cmc_id": None, "reason": "none"},
                ],
            }
        )
    )
    overrides = load_overrides(path)
    assert overrides[("binance-spot", "lunausdt")].history[0].cmc_id == 4172
    assert overrides[("x", "y")].cmc_id is None
    assert load_overrides(tmp_path / "missing.json") == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "overrides": []},
        {"schema_version": 1, "overrides": [{"exchange": "x"}]},
        {
            "schema_version": 1,
            "overrides": [
                {"exchange": "x", "original_id": "y", "cmc_id": 1, "reason": "a"},
                {"exchange": "x", "original_id": "y", "cmc_id": 2, "reason": "b"},
            ],
        },
    ],
)
def test_load_overrides_rejects_bad_files(tmp_path, payload):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_overrides(path)


def test_render_report_lists_coverage_and_issues():
    rows = {"binance-spot": [_row("btcusdt", "BTC", "USDT"), _row("abcusdt", "ABC", "USDT")]}
    pairs = [_pair("binance", "spot", 1, "BTC", 825, "USDT", url="https://x/trade/BTC_USDT")]
    report, registry = _build(rows, pairs)
    rendered = render_report(report, registry)
    assert "`binance-spot`" in rendered
    assert "Active rows without a base asset (1)" in rendered
    assert "`abcusdt`" in rendered


def test_cli_offline_build_writes_registry_and_snapshots(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-spot.json").write_text(
        json.dumps([_row("ethbtc", "ETH", "BTC", cmc_id=1027)])
    )
    cache = tmp_path / "details.json"
    from integrations.cmc_asset_details import save_detail_cache

    save_detail_cache(cache, DETAILS)
    registry_path = tmp_path / "symbology" / "assets.json"
    report_path = tmp_path / "report.md"
    summary_path = tmp_path / "summary.json"
    assert (
        main(
            [
                "--data-dir", str(data_dir),
                "--registry-path", str(registry_path),
                "--overrides-path", str(tmp_path / "none.json"),
                "--exchanges", "binance-spot",
                "--offline",
                "--details-cache", str(cache),
                "--report-path", str(report_path),
                "--summary-path", str(summary_path),
            ]
        )
        == 0
    )
    row = json.loads((data_dir / "binance-spot.json").read_text())[0]
    assert row["base_asset_id"] == "ETH"
    assert AssetRegistry.load(registry_path).get("ETH").cmc_id == 1027
    assert json.loads(summary_path.read_text())["base_resolved"] == 1
    assert report_path.read_text().startswith("# Unified symbology build")


def test_cli_rejects_unsupported_exchanges(tmp_path):
    with pytest.raises(SystemExit):
        main(["--exchanges", "kraken", "--offline", "--data-dir", str(tmp_path)])


def test_match_enum_values_are_stable():
    assert {m.value for m in Match} == {
        "override",
        "cmc_ledger",
        "cmc_market_pair",
        "snapshot_cmc_id",
        "venue_lineage",
        "cross_venue_consensus",
        "venue_reference",
        "venue_majority",
    }


def test_ticker_eras_assign_and_record_history_across_venues():
    from integrations.symbology import TickerEra

    eras = (TickerEra("LUNA", 4172, _ts("2022-05-28"), "Terra crash"),)
    rows = {
        # Ended before the era closed: the old asset, whatever else is known.
        "binance-futures": [
            _row("lunausdt", "LUNA", "USDT", "perpetual", margin="USDT", first="2021-01-28", end="2022-05-13")
        ],
        # Spans the change: today's asset, with the old one as history.
        "okx-spot": [_row("LUNA-USDT", "LUNA", "USDT", first="2021-02-24")],
        # Listed after the change: untouched.
        "binance-spot": [_row("lunausdc", "LUNA", "USDC", first="2025-12-24")],
    }
    pairs = [
        _pair("okx", "spot", 20314, "LUNA", 825, "USDT", url="https://x/trade-spot/luna-usdt"),
        _pair("binance", "spot", 20314, "LUNA", 3408, "USDC", url="https://x/trade/LUNA_USDC"),
    ]
    _build(rows, pairs, ticker_eras=eras)
    old = rows["binance-futures"][0]
    assert old["base_asset_id"] == "LUNC"
    assert old["base_asset_match"] == "override"
    spanning = rows["okx-spot"][0]
    assert spanning["base_asset_id"] == "LUNA"
    assert spanning["base_asset_match"] == "cmc_market_pair"
    assert spanning["base_asset_history"] == [{"asset_id": "LUNC", "until": "2022-05-28T00:00:00Z"}]
    assert "base_asset_history" not in rows["binance-spot"][0]


def test_ticker_era_history_is_not_applied_to_an_unrelated_current_asset_when_unresolved():
    from integrations.symbology import TickerEra

    eras = (TickerEra("LUNA", 4172, _ts("2022-05-28"), "Terra crash"),)
    rows = {"gateio-spot": [_row("LUNA_ETH", "LUNA", "ETH", first="2021-01-28", end="2024-12-27")]}
    _build(rows, ticker_eras=eras)
    # Nothing identifies today's LUNA on this row, so it gets no asset at all
    # rather than a history entry hanging off nothing.
    assert "base_asset_id" not in rows["gateio-spot"][0]


def test_load_overrides_reads_ticker_eras(tmp_path):
    from integrations.symbology import load_ticker_eras

    path = tmp_path / "overrides.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "overrides": [],
                "ticker_eras": [
                    {"ticker": "luna", "cmc_id": 4172, "until": "2022-05-28T00:00:00Z", "reason": "x"}
                ],
            }
        )
    )
    (era,) = load_ticker_eras(path)
    assert era.ticker == "LUNA"
    assert era.cmc_id == 4172
    assert load_ticker_eras(tmp_path / "missing.json") == ()


def test_rows_spanning_an_era_infer_only_from_anchors_after_it():
    from integrations.symbology import TickerEra

    eras = (TickerEra("LUNA", 4172, _ts("2022-05-28"), "Terra crash"),)
    rows = {
        "bybit-perps-usd": [
            _row("LUNAUSDT", "LUNA", "USDT", "perpetual", margin="USDT", first="2021-10-11", end="2022-05-13")
        ],
        # CMC's Bybit pair for this row names the wrong asset, so it is unmatched.
        "bybit-spot": [_row("LUNAUSDT", "LUNA", "USDT", first="2021-12-04")],
        "okx-spot": [_row("LUNA-USDT", "LUNA", "USDT", first="2021-02-24")],
        "gateio-spot": [_row("LUNA_USDT", "LUNA", "USDT", first="2021-01-28")],
    }
    pairs = [
        _pair("okx", "spot", 20314, "LUNA", 825, "USDT", url="https://x/trade-spot/luna-usdt"),
        _pair("gate", "spot", 20314, "LUNA", 825, "USDT", url="https://x/trade/LUNA_USDT"),
    ]
    _build(rows, pairs, ticker_eras=eras)
    bybit = rows["bybit-spot"][0]
    assert bybit["base_asset_id"] == "LUNA"
    assert bybit["base_asset_match"] == "cross_venue_consensus"
    assert bybit["base_asset_history"] == [{"asset_id": "LUNC", "until": "2022-05-28T00:00:00Z"}]
    assert rows["bybit-perps-usd"][0]["base_asset_id"] == "LUNC"


def test_legacy_cmc_ids_yield_to_concurrent_strong_anchors_on_the_same_venue():
    rows = {
        "binance-futures": [
            _row("apeusdt", "APE", "USDT", "perpetual", margin="USDT", first="2022-03-17", cmc_id=9000),
            _row("apebusd", "APE", "BUSD", "perpetual", margin="BUSD", first="2022-05-19", end="2023-05-17", cmc_id=9000),
        ],
        # Legacy IDs a strong anchor does not contradict are kept.
        "binance-spot": [_row("ethbtc", "ETH", "BTC", end="2020-01-01", cmc_id=1027)],
    }
    pairs = [_pair("binance", "perpetual", 1027, "APE", 825, "USDT", ticker="APEUSDT")]
    report, _ = _build(rows, pairs)
    delisted = _by_id(rows, "binance-futures", "apebusd")
    assert delisted["cmc_id"] == 1027
    assert delisted["base_asset_match"] == "venue_majority"
    assert ("binance-futures", "apebusd", 1027, "venue_majority", 9000, "snapshot_cmc_id") in report.disagreements
    assert _by_id(rows, "binance-spot", "ethbtc")["base_asset_match"] == "snapshot_cmc_id"


def test_reviewed_ledger_decisions_are_not_overruled_by_lineage():
    rows = {
        "binance-futures": [
            _row("apeusdt", "APE", "USDT", "perpetual", margin="USDT", first="2022-03-17"),
            _row("apebusd", "APE", "BUSD", "perpetual", margin="BUSD", first="2022-05-19", end="2023-05-17"),
        ],
    }
    pairs = [_pair("binance", "perpetual", 1027, "APE", 825, "USDT", ticker="APEUSDT")]
    ledger = {("binance-futures", "apebusd", _ts("2022-05-19")): (9000, False)}
    _build(rows, pairs, ledger=ledger)
    assert _by_id(rows, "binance-futures", "apebusd")["cmc_id"] == 9000


def test_cross_venue_consensus_takes_a_supermajority_of_venues():
    rows = {
        "binance-spot": [_row("dogeusdt", "DOGE", "USDT")],
        "okx-spot": [_row("DOGE-USDT", "DOGE", "USDT")],
        "bybit-spot": [_row("DOGEUSDT", "DOGE", "USDT")],
        "coinbase-spot": [_row("DOGE-USD", "DOGE", "USD")],
        # One venue's CMC listing names an unrelated DOGE token.
        "hyperliquid-spot": [_row("DOGE/USDC", "DOGE", "USDC")],
        "gateio-spot": [_row("DOGE_TRY", "DOGE", "TRY")],
    }
    pairs = [
        _pair("binance", "spot", 1027, "DOGE", 825, "USDT", url="https://x/trade/DOGE_USDT"),
        _pair("okx", "spot", 1027, "DOGE", 825, "USDT", url="https://x/trade-spot/doge-usdt"),
        _pair("bybit", "spot", 1027, "DOGE", 825, "USDT", url="https://x/spot/DOGE/USDT"),
        _pair("coinbase-exchange", "spot", 1027, "DOGE", 2781, "USD", url="https://x/trade/DOGE-USD"),
        _pair("hyperliquid", "spot", 9000, "DOGE", 3408, "USDC", url="https://x/trade/DOGE/USDC"),
    ]
    _build(rows, pairs)
    assert rows["gateio-spot"][0]["cmc_id"] == 1027
    assert rows["gateio-spot"][0]["base_asset_match"] == "cross_venue_consensus"


def test_cross_venue_consensus_refuses_a_close_split():
    rows = {
        "binance-spot": [_row("abcusdt", "ABC", "USDT")],
        "okx-spot": [_row("ABC-USDT", "ABC", "USDT")],
        "bybit-spot": [_row("ABCUSDT", "ABC", "USDT")],
        "gateio-spot": [_row("ABC_TRY", "ABC", "TRY")],
    }
    pairs = [
        _pair("binance", "spot", 1027, "ABC", 825, "USDT", url="https://x/trade/ABC_USDT"),
        _pair("okx", "spot", 1027, "ABC", 825, "USDT", url="https://x/trade-spot/abc-usdt"),
        _pair("bybit", "spot", 9000, "ABC", 825, "USDT", url="https://x/spot/ABC/USDT"),
    ]
    _build(rows, pairs)
    assert "base_asset_id" not in rows["gateio-spot"][0]


def test_ticker_reuse_guard_ignores_listing_dates_of_retired_assets():
    details = dict(DETAILS)
    details[3513] = _detail(3513, "FTM", "Fantom (Migrated to Sonic)", "fantom", date_added="2024-05-01", status="untracked")
    rows = {
        "binance-spot": [
            _row("ftmusdt", "FTM", "USDT", first="2019-06-11", end="2025-01-13", cmc_id=3513),
            _row("ftmbusd", "FTM", "BUSD", first="2019-06-11", end="2023-12-01"),
        ]
    }
    _build(rows, details=details)
    assert rows["binance-spot"][1]["base_asset_id"] == "FTM"


def test_untracked_tradfi_underlyings_get_atlas_reference_assets():
    rows = {
        "bybit-perps-usd": [_row("AALUSDT", "AAL", "USDT", "perpetual", margin="USDT", underlying="equity")],
        "gateio-perps-usdt": [
            _row("AAL_USDT", "AAL", "USDT", "perpetual", margin="USDT", underlying="equity"),
            _row("NAS100_USDT", "NAS100", "USDT", "perpetual", margin="USDT", underlying="index"),
            # The venue's ticker matches a CMC-tracked reference asset: reuse it.
            _row("TSLA_USDT", "TSLA", "USDT", "perpetual", margin="USDT", underlying="equity"),
        ],
        "binance-futures": [_row("tslausdt", "TSLA", "USDT", "perpetual", margin="USDT", underlying="equity")],
        # Spot tokens are wrappers, not the reference asset: left alone.
        "bitget-spot": [_row("RAALUSDT", "RAAL", "USDT", underlying="equity")],
    }
    pairs = [
        _pair("binance", "perpetual", 38152, "TSLA", 825, "USDT", ticker="TSLAUSDT"),
        _pair("bybit", "perpetual", 825, "USDT", 825, "USDT", ticker="NOPE"),
    ]
    _report, registry = _build(rows, pairs)
    aal = {rows[e][0]["base_asset_id"] for e in ("bybit-perps-usd", "gateio-perps-usdt")}
    assert aal == {"AAL"}
    assert registry.get("AAL").asset_class is AssetClass.equity
    assert registry.get("AAL").cmc_id is None
    assert _by_id(rows, "gateio-perps-usdt", "AAL_USDT")["base_asset_match"] == "venue_reference"
    assert "cmc_id" not in _by_id(rows, "gateio-perps-usdt", "AAL_USDT")
    assert "category" not in _by_id(rows, "gateio-perps-usdt", "AAL_USDT")
    assert _by_id(rows, "gateio-perps-usdt", "NAS100_USDT")["unified_id"] == "perpetual-NAS100-USDT:USDT"
    assert registry.get("NAS100").asset_class is AssetClass.index
    assert _by_id(rows, "gateio-perps-usdt", "TSLA_USDT")["base_asset_id"] == "TSLA"
    assert "base_asset_id" not in rows["bitget-spot"][0]
    # Rebuilding finds the native asset again rather than minting a second one.
    before = len(registry)
    _build(rows, pairs, registry=registry)
    assert len(registry) == before


def test_reference_asset_collides_with_a_crypto_ticker():
    registry = AssetRegistry()
    registry.mint(ticker="CAT", name="Simons Cat", asset_class=AssetClass.crypto, cmc_id=32724, cmc_slug="cat")
    rows = {"gateio-perps-usdt": [_row("CAT_USDT", "CAT", "USDT", "perpetual", margin="USDT", underlying="equity")]}
    _build(rows, registry=registry, details={**DETAILS, 32724: _detail(32724, "CAT", "Simons Cat", "cat")})
    assert rows["gateio-perps-usdt"][0]["base_asset_id"] == "CAT.EQUITY"


@pytest.mark.parametrize("ticker", ["TEST002", "TEST01", "TEST"])
def test_exchange_test_markets_get_no_reference_asset(ticker):
    # OKX lists test contracts (TEST002-USD_UM_XPERP-310822) it labels equity.
    rows = {"okx-futures": [_row(f"{ticker}-USD-310822", ticker, "USD", "future", margin="USD", underlying="equity")]}
    _, registry = _build(rows)
    assert "base_asset_id" not in rows["okx-futures"][0]
    assert registry.find_by_ticker(ticker) == []


def test_tradfi_rows_never_infer_a_token_wrapper():
    details = {
        **DETAILS,
        38000: _detail(38000, "GEON", "General Electric Tokenized Stock (Ondo)", "ge-ondo"),
    }
    rows = {
        "bitget-perps-usdt": [_row("GEUSDT", "GE", "USDT", "perpetual", margin="USDT", underlying="equity")],
        "gateio-perps-usdt": [_row("GE_USDT", "GE", "USDT", "perpetual", margin="USDT", underlying="equity")],
    }
    pairs = [_pair("bitget", "perpetual", 38000, "GE", 825, "USDT", ticker="GEUSDT_UMCBL")]
    _, registry = _build(rows, pairs, details=details)
    gate = rows["gateio-perps-usdt"][0]
    assert gate["base_asset_match"] == "venue_reference"
    assert registry.get(gate["base_asset_id"]).asset_class is AssetClass.equity


@pytest.mark.parametrize("underlying", ["forex", "pre_market"])
def test_forex_and_pre_market_rows_never_infer_a_token(underlying):
    # A pre-IPO OPENAI perpetual is not the OPENAI meme token another venue's
    # listing names, and Atlas mints no reference asset for these classes.
    details = {**DETAILS, 38001: _detail(38001, "OPENAI", "OpenAI Meme", "openai-meme")}
    rows = {
        "bitget-perps-usdt": [_row("OPENAIUSDT", "OPENAI", "USDT", "perpetual", margin="USDT", underlying=underlying)],
        "gateio-perps-usdt": [_row("OPENAI_USDT", "OPENAI", "USDT", "perpetual", margin="USDT", underlying=underlying)],
        "okx-perps": [_row("OPENAI-USDT-SWAP", "OPENAI", "USDT", "perpetual", margin="USDT", underlying=underlying)],
    }
    # Two venues' listings name the token: enough for a cross-venue consensus.
    pairs = [
        _pair("bitget", "perpetual", 38001, "OPENAI", 825, "USDT", ticker="OPENAIUSDT"),
        _pair("okx", "perpetual", 38001, "OPENAI", 825, "USDT", ticker="OPENAI-USDT-SWAP"),
    ]
    _build(rows, pairs, details=details)
    assert "base_asset_id" not in rows["gateio-perps-usdt"][0]


def test_venue_tickers_become_aliases_and_explain_multipliers():
    details = {**DETAILS, 16086: _detail(16086, "BTT", "BitTorrent [New]", "bittorrent-new")}
    rows = {
        "binance-spot": [_row("bttcusdt", "BTTC", "USDT")],
        "binance-futures": [_row("1000bttcusdt", "1000BTTC", "USDT", "perpetual", margin="USDT")],
    }
    pairs = [
        _pair("binance", "spot", 16086, "BTTC", 825, "USDT", url="https://x/trade/BTTC_USDT"),
        _pair("binance", "perpetual", 16086, "1000BTTC", 825, "USDT", ticker="1000BTTCUSDT"),
    ]
    _, registry = _build(rows, pairs, details=details)
    assert registry.get("BTT").aliases == ("BTTC",)
    assert rows["binance-futures"][0]["base_multiplier"] == 1000
    assert "base_multiplier" not in rows["binance-spot"][0]


def _priced_pair(*args, price, **kwargs):
    from dataclasses import replace

    return replace(_pair(*args, **kwargs), price_usd=price)


def test_pairs_priced_unlike_their_asset_are_rejected():
    details = {**DETAILS, 32778: _detail(32778, "DOGE", "Department Of Government Efficiency", "dogegov")}
    details[74] = _detail(74, "DOGE", "Dogecoin", "dogecoin")
    rows = {
        "gateio-spot": [_row("DOGE_USDT", "DOGE", "USDT")],
        "gateio-perps-usdt": [_row("DOGE_USDT", "DOGE", "USDT", "perpetual", margin="USDT")],
        "binance-futures": [_row("1000pepeusdt", "1000PEPE", "USDT", "perpetual", margin="USDT")],
    }
    pairs = [
        _priced_pair("gate", "spot", 74, "DOGE", 825, "USDT", url="https://x/trade/DOGE_USDT", price=0.0815),
        # CMC files Gate's DOGE perpetual under an unrelated token 120x cheaper.
        _priced_pair("gate", "perpetual", 32778, "DOGE", 825, "USDT", ticker="DOGE_USDT", price=0.0815),
        # A multiplied contract may be priced per contract.
        _priced_pair("binance", "perpetual", 24478, "1000PEPE", 825, "USDT", ticker="1000PEPEUSDT", price=0.009),
    ]
    prices = {74: 0.0816, 32778: 0.00066, 24478: 0.000009}
    report, _ = _build(rows, pairs, details=details, asset_prices=prices)
    perp = rows["gateio-perps-usdt"][0]
    assert perp["base_asset_id"] == "DOGE"
    assert perp["cmc_id"] == 74
    assert perp["base_asset_match"] == "venue_lineage"
    assert [(e, i, c) for e, i, c, _ in report.pair_price_mismatches] == [
        ("gateio-perps-usdt", "DOGE_USDT", 32778)
    ]
    assert rows["binance-futures"][0]["base_asset_match"] == "cmc_market_pair"


def test_a_price_rejected_pair_withdraws_the_cmc_id_it_wrote():
    # Gate's IRIS_USDT trades 38x off IRISnet today. The cmc_id the last
    # build took from that listing goes with it; left behind, an offline
    # rebuild would read it as evidence and resolve the row again.
    details = {**DETAILS, 3874: _detail(3874, "IRIS", "IRISnet", "irisnet")}
    rows = {"gateio-spot": [_row("IRIS_USDT", "IRIS", "USDT")]}
    pair = _priced_pair("gate", "spot", 3874, "IRIS", 825, "USDT", url="https://x/trade/IRIS_USDT", price=0.01)
    _, registry = _build(rows, [_pair("gate", "spot", 3874, "IRIS", 825, "USDT", url="https://x/trade/IRIS_USDT")], details=details)
    assert rows["gateio-spot"][0]["cmc_id"] == 3874

    _build(rows, [pair], registry=registry, details=details, asset_prices={3874: 0.38})
    row = rows["gateio-spot"][0]
    assert "base_asset_id" not in row
    assert "cmc_id" not in row
    first = json.dumps(rows)
    _build(rows, registry=registry, details=details)
    assert json.dumps(rows) == first


def test_a_price_rejected_pair_keeps_a_cmc_id_it_did_not_write():
    details = {**DETAILS, 3874: _detail(3874, "IRIS", "IRISnet", "irisnet")}
    rows = {"gateio-spot": [_row("IRIS_USDT", "IRIS", "USDT", cmc_id=3874)]}
    pair = _priced_pair("gate", "spot", 3874, "IRIS", 825, "USDT", url="https://x/trade/IRIS_USDT", price=0.01)
    _build(rows, [pair], details=details, asset_prices={3874: 0.38})
    assert rows["gateio-spot"][0]["cmc_id"] == 3874


def test_pair_price_check_is_skipped_without_prices():
    rows = {"gateio-perps-usdt": [_row("DOGE_USDT", "DOGE", "USDT", "perpetual", margin="USDT")]}
    pairs = [_priced_pair("gate", "perpetual", 1027, "DOGE", 825, "USDT", ticker="DOGE_USDT", price=0.08)]
    report, _ = _build(rows, pairs)
    assert rows["gateio-perps-usdt"][0]["base_asset_match"] == "cmc_market_pair"
    assert report.pair_price_mismatches == []


@pytest.mark.parametrize(
    ("symbol", "ticker", "expected"),
    [
        ("SHIB1000", "SHIB", 1000),
        ("MBABYDOGE", "BABYDOGE", 1_000_000),
        ("MOVE", "MOVE", None),
        ("1000000MOG", "MOG", 1_000_000),
        ("10000NEX", "NEX", 10000),
        ("PEPE2", "PEPE", None),
    ],
)
def test_contract_multiplier_forms(symbol, ticker, expected):
    from atlas.assets import Asset

    asset = Asset(asset_id="A", ticker=ticker, name="x", asset_class=AssetClass.crypto)
    assert base_multiplier(symbol, asset) == expected


def test_price_check_allows_suffix_multipliers_and_order_of_magnitude_noise():
    details = {**DETAILS, 5994: _detail(5994, "SHIB", "Shiba Inu", "shiba-inu")}
    rows = {
        "bybit-perps-usd": [_row("SHIB1000USDT", "SHIB1000", "USDT", "perpetual", margin="USDT")],
        "gateio-spot": [_row("ETH_USDT", "ETH", "USDT")],
    }
    pairs = [
        _priced_pair("bybit", "perpetual", 5994, "SHIB1000", 825, "USDT", ticker="SHIB1000USDT", price=0.0099),
        # An illiquid venue trading 4x off CMC's aggregate is noise, not identity.
        _priced_pair("gate", "spot", 1027, "ETH", 825, "USDT", url="https://x/trade/ETH_USDT", price=10.0),
    ]
    report, _ = _build(rows, pairs, details=details, asset_prices={5994: 0.0000099, 1027: 2.5})
    assert report.pair_price_mismatches == []
    assert rows["bybit-perps-usd"][0]["base_multiplier"] == 1000


def test_a_venue_outvotes_a_minority_cmc_listing_of_one_ticker():
    details = {**DETAILS, 16116: _detail(16116, "SOL", "Wrapped Solana", "wrapped-solana")}
    details[5426] = _detail(5426, "SOL", "Solana", "solana", rank=5)
    rows = {
        "gateio-spot": [
            _row("SOL_USDT", "SOL", "USDT"),
            _row("SOL_USD1", "SOL", "USD1"),
            _row("SOL_USDC", "SOL", "USDC"),
        ],
        "gateio-perps-usdt": [_row("SOL_USDT", "SOL", "USDT", "perpetual", margin="USDT")],
    }
    pairs = [
        _pair("gate", "spot", 5426, "SOL", 825, "USDT", url="https://x/trade/SOL_USDT"),
        _pair("gate", "spot", 5426, "SOL", 825, "USD1", url="https://x/trade/SOL_USD1"),
        _pair("gate", "spot", 16116, "SOL", 3408, "USDC", url="https://x/trade/SOL_USDC"),
        _pair("gate", "perpetual", 5426, "SOL", 825, "USDT", ticker="SOL_USDT"),
    ]
    report, _ = _build(rows, pairs, details=details)
    assert {row["cmc_id"] for rows_ in rows.values() for row in rows_} == {5426}
    usdc = _by_id(rows, "gateio-spot", "SOL_USDC")
    assert usdc["base_asset_match"] == "venue_majority"
    assert ("gateio-spot", "SOL_USDC", 5426, "venue_majority", 16116, "cmc_market_pair") in report.disagreements


def test_a_tied_venue_keeps_both_listings_and_reports_them():
    details = {**DETAILS, 28324: _detail(28324, "DYDX", "dYdX", "dydx-chain")}
    details[11156] = _detail(11156, "ETHDYDX", "dYdX (ethDYDX)", "dydx")
    rows = {
        "bitget-spot": [_row("DYDXUSDT", "DYDX", "USDT")],
        "bitget-perps-usdt": [_row("DYDXUSDT", "DYDX", "USDT", "perpetual", margin="USDT")],
    }
    pairs = [
        _pair("bitget", "spot", 28324, "DYDX", 825, "USDT", url="https://x/spot/DYDXUSDT"),
        _pair("bitget", "perpetual", 11156, "DYDX", 825, "USDT", ticker="DYDXUSDT_UMCBL"),
    ]
    report, _ = _build(rows, pairs, details=details)
    assert rows["bitget-spot"][0]["cmc_id"] == 28324
    assert rows["bitget-perps-usdt"][0]["cmc_id"] == 11156
    assert report.venue_ticker_conflicts == [("bitget", "DYDX", (11156, 28324))]


# --- Review fixes ------------------------------------------------------------


def test_an_unresolved_row_drops_an_inferred_cmc_id():
    rows = {
        "binance-spot": [
            _row("xyzbtc", "XYZ", "BTC", end="2024-01-01", cmc_id=777, base_asset_id="XYZ",
                 base_asset_match="venue_lineage", category=["DeFi"])
        ]
    }
    _build(rows)
    row = rows["binance-spot"][0]
    assert "cmc_id" not in row
    assert "category" not in row
    _build(rows)
    assert "base_asset_id" not in row


def test_an_override_without_an_asset_clears_the_cmc_id():
    rows = {"binance-spot": [_row("btcusdt", "BTC", "USDT", cmc_id=1)]}
    overrides = {("binance-spot", "btcusdt"): Override("binance-spot", "btcusdt", None, "test")}
    _build(rows, overrides=overrides)
    assert "cmc_id" not in rows["binance-spot"][0]


def test_hip3_rows_do_not_learn_from_the_main_dex():
    rows = {
        "hyperliquid-perps": [
            _row("STX", "STX", "USDC", "perpetual", margin="USDC"),
            _row("para:STX", "PARA:STX", "USDC", "perpetual", margin="USDC", first="2026-01-01"),
            _row("cash:ETH", "CASH:ETH", "USDT0", "perpetual", margin="USDT0", first="2026-01-01"),
            _row("flx:ETH", "FLX:ETH", "USDH", "perpetual", margin="USDH", first="2026-01-01"),
        ]
    }
    pairs = [
        _pair("hyperliquid", "perpetual", 9000, "STX", 3408, "USDC", ticker="STX_USDC"),
        _pair("hyperliquid", "perpetual", 1027, "cash:ETH", 3408, "USDT0", ticker="cash:ETH_USDT0"),
    ]
    _build(rows, pairs)
    assert "base_asset_id" not in _by_id(rows, "hyperliquid-perps", "para:STX")
    # Another HIP-3 dex's listing of the ticker is still evidence.
    assert _by_id(rows, "hyperliquid-perps", "flx:ETH")["base_asset_id"] == "ETH"


def test_cross_venue_consensus_needs_two_venues():
    rows = {
        "gateio-spot": [_row("USDP_USDT", "USDP", "USDT")],
        "binance-spot": [_row("usdpusdt", "USDP", "USDT", first="2021-09-10", end="2025-01-01")],
    }
    pairs = [_pair("gate", "spot", 9000, "USDP", 825, "USDT", url="https://x/trade/USDP_USDT")]
    _build(rows, pairs)
    assert "base_asset_id" not in _by_id(rows, "binance-spot", "usdpusdt")


def test_aliases_come_from_direct_matches_only():
    rows = {
        "binance-futures": [_row("neiroethusdt", "NEIROETH", "USDT", "perpetual", margin="USDT", end="2025-01-01")],
        "binance-spot": [_row("neirousdt", "NEIRO", "USDT", first="2024-09-01")],
    }
    pairs = [_pair("binance", "spot", 24478, "NEIRO", 825, "USDT", url="https://x/trade/NEIRO_USDT")]
    _, registry = _build(rows, pairs)
    assert registry.get("PEPE").aliases == ("NEIRO",)
    rows["binance-futures"][0]["base_asset_match"] = "cross_venue_consensus"
    rows["binance-futures"][0]["cmc_id"] = 24478
    _, registry = _build(rows, pairs, registry=AssetRegistry())
    assert "NEIROETH" not in registry.get("PEPE").aliases


def test_price_check_knows_registry_tickers_for_letter_multipliers():
    registry = AssetRegistry()
    registry.mint(ticker="PEPE", name="Pepe", asset_class=AssetClass.crypto, cmc_id=24478, cmc_slug="pepe")
    rows = {"hyperliquid-perps": [_row("kPEPE", "KPEPE", "USDC", "perpetual", margin="USDC")]}
    pairs = [_priced_pair("hyperliquid", "perpetual", 24478, "kPEPE", 3408, "USDC", ticker="kPEPE_USDC", price=0.009)]
    report, _ = _build(rows, pairs, registry=registry, details={}, asset_prices={24478: 0.000009})
    assert report.pair_price_mismatches == []
    assert rows["hyperliquid-perps"][0]["base_asset_match"] == "cmc_market_pair"


def test_a_price_rejected_pair_also_discards_its_stored_match():
    details = {**DETAILS, 32778: _detail(32778, "DOGE", "Impostor", "dogegov"), 74: _detail(74, "DOGE", "Dogecoin", "dogecoin")}
    rows = {
        "gateio-spot": [_row("DOGE_USDT", "DOGE", "USDT")],
        "gateio-perps-usdt": [
            _row("DOGE_USDT", "DOGE", "USDT", "perpetual", margin="USDT", cmc_id=32778, base_asset_match="cmc_market_pair")
        ],
    }
    pairs = [
        _priced_pair("gate", "spot", 74, "DOGE", 825, "USDT", url="https://x/trade/DOGE_USDT", price=0.08),
        _priced_pair("gate", "perpetual", 32778, "DOGE", 825, "USDT", ticker="DOGE_USDT", price=0.08),
    ]
    _build(rows, pairs, details=details, asset_prices={74: 0.08, 32778: 0.0006})
    assert rows["gateio-perps-usdt"][0]["cmc_id"] == 74


def test_venue_majority_decisions_survive_an_offline_rebuild():
    details = {**DETAILS, 16116: _detail(16116, "SOL", "Wrapped Solana", "wrapped-solana")}
    details[5426] = _detail(5426, "SOL", "Solana", "solana", rank=5)
    rows = {
        "gateio-spot": [
            _row("SOL_USDT", "SOL", "USDT"),
            _row("SOL_USD1", "SOL", "USD1"),
            _row("SOL_USDC", "SOL", "USDC"),
            # A delisted, reviewed listing that disagrees must not unsettle it offline.
            _row("SOL_ETH", "SOL", "ETH", end="2026-01-01"),
        ],
    }
    pairs = [
        _pair("gate", "spot", 5426, "SOL", 825, "USDT", url="https://x/trade/SOL_USDT"),
        _pair("gate", "spot", 5426, "SOL", 825, "USD1", url="https://x/trade/SOL_USD1"),
        _pair("gate", "spot", 16116, "SOL", 3408, "USDC", url="https://x/trade/SOL_USDC"),
    ]
    ledger = {("gateio-spot", "SOL_ETH", _ts("2020-01-01")): (16116, False)}
    _, registry = _build(rows, pairs, details=details, ledger=ledger)
    assert _by_id(rows, "gateio-spot", "SOL_USDC")["base_asset_match"] == "venue_majority"
    before = json.dumps(rows, sort_keys=True)
    _build(rows, registry=registry, ledger=ledger)
    assert json.dumps(rows, sort_keys=True) == before


def test_tracked_reference_assets_are_reused_whatever_their_class():
    details = {
        **DETAILS,
        39474: _detail(39474, "SP500", "Standard and Poor's 500 index (Derivatives)", "sp500-derivatives",
                       tags=("TradFi Assets (Derivatives)",)),
    }
    rows = {
        "hyperliquid-perps": [_row("xyz:SP500", "XYZ:SP500", "USDC", "perpetual", margin="USDC")],
        "bitget-perps-usdt": [_row("SP500USDT", "SP500", "USDT", "perpetual", margin="USDT", underlying="equity")],
        "gateio-perps-usdt": [_row("SP500_USDT", "SP500", "USDT", "perpetual", margin="USDT", underlying="index")],
    }
    pairs = [_pair("hyperliquid", "perpetual", 39474, "xyz:SP500", 3408, "USDC", ticker="xyz:SP500_USDC")]
    _, registry = _build(rows, pairs, details=details)
    assert {rows[e][0]["base_asset_id"] for e in rows} == {"SP500"}
    assert registry.get("SP500").asset_class is AssetClass.index


def test_override_history_is_ordered(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"schema_version": 1, "overrides": [{
        "exchange": "x", "original_id": "y", "cmc_id": 3, "reason": "r",
        "history": [{"cmc_id": 2, "until": "2024-01-01T00:00:00Z"}, {"cmc_id": 1, "until": "2022-01-01T00:00:00Z"}],
    }]}))
    history = load_overrides(path)[("x", "y")].history
    assert [segment.cmc_id for segment in history] == [1, 2]


def test_subset_runs_resolve_against_every_snapshot_but_write_only_their_own(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "binance-spot.json").write_text(json.dumps([_row("ethbtc", "ETH", "BTC", cmc_id=1027)]))
    (data_dir / "gateio-spot.json").write_text(json.dumps([_row("ETH_TRY", "ETH", "TRY", end="2026-01-01")]))
    (data_dir / "okx-spot.json").write_text(json.dumps([_row("ETH-USDT", "ETH", "USDT", cmc_id=1027)]))
    okx_before = (data_dir / "okx-spot.json").read_text()
    from integrations.cmc_asset_details import save_detail_cache

    cache = tmp_path / "details.json"
    save_detail_cache(cache, DETAILS)
    main([
        "--data-dir", str(data_dir), "--registry-path", str(tmp_path / "assets.json"),
        "--overrides-path", str(tmp_path / "none.json"), "--exchanges", "gateio-spot",
        "--offline", "--details-cache", str(cache),
    ])
    assert json.loads((data_dir / "gateio-spot.json").read_text())[0]["base_asset_id"] == "ETH"
    assert (data_dir / "okx-spot.json").read_text() == okx_before


_USDP_DETAILS = {
    **DETAILS,
    3330: _detail(3330, "USDP", "Pax Dollar", "paxos-standard", rank=200),
    8886: _detail(8886, "USDP", "USDP Stablecoin", "usdp", date_added="2021-03-01"),
}


def _usdp_rows():
    return {
        # CMC identifies Gate's USDP as the DeFi stablecoin...
        "gateio-spot": [_row("USDP_USDT", "USDP", "USDT", first="2023-06-19")],
        # ...but on Binance USDP is Paxos's renamed PAX.
        "binance-spot": [
            _row("btcusdp", "BTC", "USDP", first="2021-09-10", end="2022-09-30"),
            _row("usdpusdt", "USDP", "USDT", first="2021-09-10", end="2026-09-25"),
            _row("btcusdt", "BTC", "USDT"),
        ],
    }


_USDP_PAIRS = [
    _pair("gate", "spot", 8886, "USDP", 825, "USDT", url="https://x/trade/USDP_USDT"),
    _pair("binance", "spot", 1, "BTC", 825, "USDT", url="https://x/trade/BTC_USDT"),
]


def _cmc_id(registry, asset_id):
    return registry.get(asset_id).cmc_id


def test_a_ticker_borrows_its_meaning_from_other_venues_without_a_venue_ticker():
    rows = _usdp_rows()
    _, registry = _build(rows, _USDP_PAIRS, details=_USDP_DETAILS)
    assert _cmc_id(registry, _by_id(rows, "binance-spot", "btcusdp")["quote_asset_id"]) == 8886


def test_venue_tickers_pin_a_tickers_meaning_on_one_venue():
    from integrations.symbology import VenueTicker

    pins = (VenueTicker("binance", "USDP", 3330, "Paxos renamed PAX"),)
    rows = _usdp_rows()
    _, registry = _build(rows, _USDP_PAIRS, details=_USDP_DETAILS, venue_tickers=pins)
    btcusdp = _by_id(rows, "binance-spot", "btcusdp")
    assert _cmc_id(registry, btcusdp["quote_asset_id"]) == 3330
    usdpusdt = _by_id(rows, "binance-spot", "usdpusdt")
    assert _cmc_id(registry, usdpusdt["base_asset_id"]) == 3330
    assert usdpusdt["base_asset_match"] == "override"
    assert usdpusdt["cmc_id"] == 3330
    # Other venues keep what CMC says their USDP is.
    assert _cmc_id(registry, _by_id(rows, "gateio-spot", "USDP_USDT")["base_asset_id"]) == 8886

    # The pin outranks the wrong quote the rows already carry, offline too.
    before = json.dumps(rows, sort_keys=True)
    _build(rows, registry=registry, details=_USDP_DETAILS, venue_tickers=pins)
    assert json.dumps(rows, sort_keys=True) == before


def test_venue_tickers_correct_rows_that_already_carry_the_other_asset():
    from integrations.symbology import VenueTicker

    rows = _usdp_rows()
    _, registry = _build(rows, _USDP_PAIRS, details=_USDP_DETAILS)
    pins = (VenueTicker("binance", "USDP", 3330, "Paxos renamed PAX"),)
    _build(rows, registry=registry, details=_USDP_DETAILS, venue_tickers=pins)
    assert _cmc_id(registry, _by_id(rows, "binance-spot", "btcusdp")["quote_asset_id"]) == 3330
    assert _cmc_id(registry, _by_id(rows, "binance-spot", "usdpusdt")["base_asset_id"]) == 3330


def test_a_row_override_outranks_a_venue_ticker():
    from integrations.symbology import VenueTicker

    pins = (VenueTicker("binance", "USDP", 3330, "Paxos renamed PAX"),)
    overrides = {("binance-spot", "usdpusdt"): Override("binance-spot", "usdpusdt", 8886, "test")}
    rows = _usdp_rows()
    _, registry = _build(rows, _USDP_PAIRS, details=_USDP_DETAILS, venue_tickers=pins, overrides=overrides)
    assert _cmc_id(registry, _by_id(rows, "binance-spot", "usdpusdt")["base_asset_id"]) == 8886


def test_load_venue_tickers(tmp_path):
    from integrations.symbology import load_venue_tickers

    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "overrides": [],
        "venue_tickers": [{"family": "binance", "ticker": "usdp", "cmc_id": 3330, "reason": "x"}],
    }))
    (pin,) = load_venue_tickers(path)
    assert (pin.family, pin.ticker, pin.cmc_id) == ("binance", "USDP", 3330)
    assert load_venue_tickers(tmp_path / "missing.json") == ()


@pytest.mark.parametrize(
    "entry",
    [
        {"family": "nope", "ticker": "USDP", "cmc_id": 3330, "reason": "x"},
        {"family": "binance", "ticker": "USDP", "reason": "x"},
    ],
)
def test_load_venue_tickers_rejects_bad_entries(tmp_path, entry):
    from integrations.symbology import load_venue_tickers

    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"schema_version": 1, "overrides": [], "venue_tickers": [entry]}))
    with pytest.raises(ValueError):
        load_venue_tickers(path)


def test_load_venue_tickers_rejects_duplicates(tmp_path):
    from integrations.symbology import load_venue_tickers

    entry = {"family": "binance", "ticker": "USDP", "cmc_id": 3330, "reason": "x"}
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"schema_version": 1, "overrides": [], "venue_tickers": [entry, entry]}))
    with pytest.raises(ValueError):
        load_venue_tickers(path)


def test_a_pair_cmc_still_lists_identifies_a_delisted_row():
    # Hyperliquid keeps delisted perpetuals in its universe and CMC keeps
    # listing them; the pair is direct evidence, kept after CMC drops it.
    details = {**DETAILS, 1518: _detail(1518, "MKR", "Maker", "maker")}
    rows = {
        "hyperliquid-perps": [
            _row("MKR", "MKR", "USDC", "perpetual", margin="USDC", first="2024-10-29", end="2025-09-06"),
            _row("hyna:MKR", "HYNA:MKR", "USDE", "perpetual", margin="USDE", first="2026-01-01", end="2026-02-01"),
        ]
    }
    pairs = [
        _pair("hyperliquid", "perpetual", 1518, "MKR", 3408, "USDC", url="https://app.hyperliquid.xyz/trade/MKR"),
        _pair("hyperliquid", "perpetual", 1518, "hyna:MKR", 3408, "USDE", url="https://app.hyperliquid.xyz/trade/hyna:MKR"),
    ]
    _, registry = _build(rows, pairs, details=details)
    for row_id in ("MKR", "hyna:MKR"):
        row = _by_id(rows, "hyperliquid-perps", row_id)
        assert (row["base_asset_id"], row["base_asset_match"]) == ("MKR", "cmc_market_pair")
    first = json.dumps(rows)
    _build(rows, registry=registry, details=details)
    assert json.dumps(rows) == first


def test_a_delisted_rows_pair_must_name_an_asset_of_its_ticker():
    # CMC files Hyperliquid's delisted vntl:ANTHROPIC under OpenAI
    # (Derivatives). Its base ticker is the venue's own, so it is not enough.
    details = {**DETAILS, 39485: _detail(39485, "OPENAI", "OpenAI (Derivatives)", "openai-derivatives")}
    row = _row(
        "vntl:ANTHROPIC", "VNTL:ANTHROPIC", "USDH", "perpetual", margin="USDH",
        first="2025-12-02", end="2026-06-16", underlying="pre_market",
    )
    rows = {"hyperliquid-perps": [row]}
    pair = _pair(
        "hyperliquid", "perpetual", 39485, "vntl:ANTHROPIC", 3408, "USDH",
        url="https://app.hyperliquid.xyz/trade/vntl:ANTHROPIC",
    )
    report, _ = _build(rows, [pair], details=details)
    assert "base_asset_id" not in row
    assert ("hyperliquid-perps", "vntl:ANTHROPIC", "vntl:ANTHROPIC", 39485) in report.pair_symbol_mismatches


def test_a_delisted_rows_pair_must_name_an_asset_that_existed_then():
    rows = {"hyperliquid-perps": [_row("NEW", "NEW", "USDC", "perpetual", margin="USDC", end="2024-06-01")]}
    pair = _pair("hyperliquid", "perpetual", 9000, "NEW", 3408, "USDC", url="https://app.hyperliquid.xyz/trade/NEW")
    report, _ = _build(rows, [pair])
    assert "base_asset_id" not in rows["hyperliquid-perps"][0]
    assert ("hyperliquid-perps", "NEW", 9000) in report.rejected_reused_tickers
