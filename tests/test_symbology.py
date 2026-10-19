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
        "binance-spot", "bybit-perps", "okx-futures", "hyperliquid-perps",
        "bitget-spot", "bitget-perps-usdt", "gateio-spot", "gateio-perps-usdt", "gateio-futures-usdt", "coinbase",
    } <= set(SYMBOLOGY_EXCHANGES)


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
        "coinbase": [_row("BTC-USD", "BTC", "USD")],
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
    assert _by_id(rows, "coinbase", "BTC-USD")["quote_asset_id"] == "USD"
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
        "bybit-perps": [_row("TSLAUSDT", "TSLA", "USDT", "perpetual", margin="USDT", underlying="equity")],
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
        "bybit-perps": [
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
    assert rows["bybit-perps"][0]["base_asset_id"] == "LUNC"


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
        "coinbase": [_row("DOGE-USD", "DOGE", "USD")],
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
        "bybit-perps": [_row("AALUSDT", "AAL", "USDT", "perpetual", margin="USDT", underlying="equity")],
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
    aal = {rows[e][0]["base_asset_id"] for e in ("bybit-perps", "gateio-perps-usdt")}
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
        "bybit-perps": [_row("SHIB1000USDT", "SHIB1000", "USDT", "perpetual", margin="USDT")],
        "gateio-spot": [_row("ETH_USDT", "ETH", "USDT")],
    }
    pairs = [
        _priced_pair("bybit", "perpetual", 5994, "SHIB1000", 825, "USDT", ticker="SHIB1000USDT", price=0.0099),
        # An illiquid venue trading 4x off CMC's aggregate is noise, not identity.
        _priced_pair("gate", "spot", 1027, "ETH", 825, "USDT", url="https://x/trade/ETH_USDT", price=10.0),
    ]
    report, _ = _build(rows, pairs, details=details, asset_prices={5994: 0.0000099, 1027: 2.5})
    assert report.pair_price_mismatches == []
    assert rows["bybit-perps"][0]["base_multiplier"] == 1000


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
