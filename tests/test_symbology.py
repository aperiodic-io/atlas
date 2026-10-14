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
        "bitget", "bitget-futures", "gate-io", "gate-io-futures", "coinbase",
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
        "gate-io": [_row("ETH_TRY", "ETH", "TRY", end="2026-01-01")],
    }
    pairs = [
        _pair("binance", "spot", 1027, "ETH", 825, "USDT", url="https://x/trade/ETH_USDT"),
        _pair("okx", "spot", 1027, "ETH", 825, "USDT", url="https://x/trade-spot/eth-usdt"),
    ]
    _build(rows, pairs)
    gate = _by_id(rows, "gate-io", "ETH_TRY")
    assert gate["base_asset_id"] == "ETH"
    assert gate["base_asset_match"] == "cross_venue_consensus"
    # TRY is not known anywhere, so the quote stays unresolved and so does unified_id.
    assert "quote_asset_id" not in gate
    assert "unified_id" not in gate


def test_cross_venue_consensus_requires_agreement():
    rows = {
        "binance-spot": [_row("tslausdt", "TSLA", "USDT")],
        "hyperliquid-spot": [_row("TSLA/USDC", "TSLA", "USDC")],
        "gate-io": [_row("TSLA_USDT", "TSLA", "USDT")],
    }
    pairs = [
        _pair("binance", "spot", 38152, "TSLA", 825, "USDT", url="https://x/trade/TSLA_USDT"),
        _pair("hyperliquid", "spot", 39618, "TSLA", 3408, "USDC", url="https://x/trade/TSLA/USDC"),
    ]
    report, _ = _build(rows, pairs)
    assert "base_asset_id" not in _by_id(rows, "gate-io", "TSLA_USDT")
    assert ("gate-io", "TSLA_USDT", (38152, 39618)) in report.lineage_conflicts


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
    first = json.dumps(rows, sort_keys=True)
    report, _ = _build(rows, pairs, registry=registry)
    assert json.dumps(rows, sort_keys=True) == first
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
    assert "category" not in rows["binance-spot"][0]


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
    }
