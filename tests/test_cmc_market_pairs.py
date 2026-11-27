import pytest
import requests

from integrations.cmc_market_pairs import (
    CMC_MARKET_PAIRS_URL,
    CMC_VENUES,
    CmcMarketPairsError,
    MarketPair,
    fetch_market_pairs,
    fetch_venue_market_pairs,
    match_market_pairs,
    native_id_candidates,
    parse_market_pair,
    symbols_consistent,
)


def _item(**overrides):
    item = {
        "marketId": 9933,
        "marketPair": "BTC/USDT",
        "category": "spot",
        "marketUrl": "https://www.binance.com/en/trade/BTC_USDT?ref=1",
        "baseSymbol": "BTC",
        "baseCurrencyId": 1,
        "baseCurrencyName": "Bitcoin",
        "baseCurrencySlug": "bitcoin",
        "quoteSymbol": "USDT",
        "quoteCurrencyId": 825,
        "price": 81575.8,
        "lastUpdated": "2026-10-14T20:50:42.000Z",
    }
    item.update(overrides)
    return item


def _pair(venue="binance", category="spot", **overrides):
    return parse_market_pair(_item(**overrides), venue, category)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code}")
            error.response = self
            self.headers = {"Retry-After": "0"}
            raise error

    def json(self):
        return self.payload


class PagedSession:
    def __init__(self, items, page_size, failures=0, reported=None):
        self.items = items
        self.page_size = page_size
        self.failures = failures
        self.reported = len(items) if reported is None else reported
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs["params"]))
        if self.failures:
            self.failures -= 1
            return FakeResponse({}, status=429)
        start = kwargs["params"]["start"] - 1
        page = self.items[start : start + kwargs["params"]["limit"]]
        return FakeResponse(
            {"data": {"numMarketPairs": self.reported, "marketPairs": page}}
        )


def test_parse_market_pair_reads_identity_and_quote_fields():
    pair = _pair()
    assert pair.base_cmc_id == 1
    assert pair.base_symbol == "BTC"
    assert pair.base_slug == "bitcoin"
    assert pair.quote_cmc_id == 825
    assert pair.quote_symbol == "USDT"
    assert pair.price_usd == pytest.approx(81575.8)
    assert pair.last_updated.year == 2026


@pytest.mark.parametrize(
    "overrides",
    [{"baseCurrencyId": None}, {"baseCurrencyId": 0}, {"baseCurrencyId": "x"}, {"baseSymbol": None}],
)
def test_parse_market_pair_rejects_rows_without_a_base_identity(overrides):
    with pytest.raises(ValueError):
        _pair(**overrides)


def test_parse_market_pair_tolerates_missing_optional_fields():
    pair = _pair(quoteCurrencyId=None, price=None, lastUpdated=None, marketUrl=None)
    assert pair.quote_cmc_id is None
    assert pair.price_usd is None
    assert pair.last_updated is None


@pytest.mark.parametrize(
    ("venue", "category", "overrides", "expected"),
    [
        ("binance", "spot", {}, "BTCUSDT"),
        ("binance", "perpetual", {"derivativeTickerId": "BTCUSDT"}, "BTCUSDT"),
        ("binance", "perpetual", {"derivativeTickerId": "ETCUSD_PERP"}, "ETCUSD_PERP"),
        (
            "bybit",
            "spot",
            {"marketUrl": "https://www.bybit.com/en-US/trade/spot/BTC/USDT?affiliate_id=1"},
            "BTCUSDT",
        ),
        (
            "okx",
            "spot",
            {"marketUrl": "https://www.okx.com/trade-spot/btc-usdt?channelid=1"},
            "BTC-USDT",
        ),
        ("okx", "perpetual", {"derivativeTickerId": "ETH-USDT-SWAP"}, "ETH-USDT-SWAP"),
        (
            "hyperliquid",
            "perpetual",
            {
                "derivativeTickerId": "BTC_USDC",
                "marketUrl": "https://app.hyperliquid.xyz/trade/BTC",
                "quoteSymbol": "USDC",
            },
            "BTC",
        ),
        (
            "hyperliquid",
            "perpetual",
            {
                "derivativeTickerId": "xyz:TSLA_USDC",
                "marketUrl": "https://app.hyperliquid.xyz/trade/xyz:TSLA",
                "quoteSymbol": "USDC",
            },
            "XYZ:TSLA",
        ),
        (
            "hyperliquid",
            "spot",
            {"marketUrl": "https://app.hyperliquid.xyz/trade/UBTC/USDC"},
            "UBTC/USDC",
        ),
        (
            "coinbase-exchange",
            "spot",
            {"marketUrl": "https://exchange.coinbase.com/trade/BTC-USD"},
            "BTC-USD",
        ),
        (
            "bitget",
            "perpetual",
            {
                "derivativeTickerId": "BTCUSDT_UMCBL",
                "marketUrl": "https://www.bitget.com/futures/usdt/BTCUSDT?channelCode=x",
            },
            "BTCUSDT",
        ),
        (
            "gate",
            "spot",
            {"marketUrl": "https://www.gate.com/trade/BTC_USDT?ref=1"},
            "BTC_USDT",
        ),
        ("gate", "futures", {"derivativeTickerId": "ETH_USDT_20261023"}, "ETH_USDT_20261023"),
        # DeFi / RWA perpetual venues, as CMC lists them.
        (
            "aster-pro",
            "perpetual",
            {
                "derivativeTickerId": "TSLAUSDT",
                "marketUrl": "https://www.asterdex.com/en/futures/v1/TSLAUSDT?ref=2b57B1",
            },
            "TSLAUSDT",
        ),
        (
            "grvt",
            "perpetual",
            {
                "derivativeTickerId": "TSLA_USDT_Perp",
                "marketUrl": "https://grvt.io/exchange/perpetual/TSLA-USDT?ref=11Q559V",
            },
            "TSLA_USDT_PERP",
        ),
        (
            "extended",
            "perpetual",
            {
                "derivativeTickerId": "XAU-USD",
                "marketUrl": "https://app.extended.exchange/",
                "quoteSymbol": "USD",
            },
            "XAU-USD",
        ),
        (
            "apex-omni",
            "perpetual",
            {
                "derivativeTickerId": "BTCUSDT",
                "marketUrl": "https://omni.apex.exchange/trade/BTCUSDT?referralCode=x",
            },
            "BTCUSDT",
        ),
        (
            "pacifica",
            "perpetual",
            {
                "derivativeTickerId": "TSLA-USD",
                "marketUrl": "https://app.pacifica.fi/trade/TSLA",
                "quoteSymbol": "USD",
            },
            "TSLA",
        ),
        (
            "variational",
            "perpetual",
            {
                "derivativeTickerId": "TSLA-PERP",
                "marketUrl": "https://omni.variational.io/perpetual/TSLA",
                "quoteSymbol": "USDC",
            },
            "TSLA",
        ),
    ],
)
def test_native_id_candidates_include_the_atlas_id(venue, category, overrides, expected):
    pair = _pair(venue=venue, category=category, **overrides)
    assert expected in native_id_candidates(pair)


def test_native_id_candidates_are_upper_case_and_unique():
    candidates = native_id_candidates(_pair(derivativeTickerId="btcusdt"))
    assert candidates == tuple(dict.fromkeys(candidates))
    assert all(candidate == candidate.upper() for candidate in candidates)


def test_fetch_market_pairs_paginates_and_deduplicates():
    items = [
        _item(marketId=index, marketUrl=f"https://www.binance.com/en/trade/T{index}_USDT")
        for index in range(5)
    ]
    items.append(items[0])  # CMC pages overlap
    session = PagedSession(items, page_size=2, reported=5)

    result = fetch_market_pairs(session, "binance", "spot", page_size=2, min_interval_seconds=0)

    assert len(result.pairs) == 5
    assert result.reported_total == 5
    assert result.duplicates == 1
    assert result.malformed == 0
    assert result.complete
    assert [call[1]["start"] for call in session.calls] == [1, 3, 5, 7]
    assert all(call[0] == CMC_MARKET_PAIRS_URL for call in session.calls)
    assert session.calls[0][1]["slug"] == "binance"
    assert session.calls[0][1]["category"] == "spot"


def test_fetch_market_pairs_counts_malformed_rows():
    items = [_item(), _item(marketId=2, baseCurrencyId=None)]
    session = PagedSession(items, page_size=10)
    result = fetch_market_pairs(session, "binance", "spot", page_size=10, min_interval_seconds=0)
    assert len(result.pairs) == 1
    assert result.malformed == 1
    assert not result.complete


def test_fetch_market_pairs_retries_transient_failures(monkeypatch):
    monkeypatch.setattr("integrations.cmc_market_pairs.time.sleep", lambda _: None)
    session = PagedSession([_item()], page_size=10, failures=2)
    result = fetch_market_pairs(session, "binance", "spot", page_size=10, min_interval_seconds=0)
    assert len(result.pairs) == 1


def test_fetch_market_pairs_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("integrations.cmc_market_pairs.time.sleep", lambda _: None)
    session = PagedSession([_item()], page_size=10, failures=10)
    with pytest.raises(CmcMarketPairsError):
        fetch_market_pairs(
            session, "binance", "spot", page_size=10, max_attempts=3, min_interval_seconds=0
        )


def test_fetch_market_pairs_rejects_an_unusable_envelope():
    class BadSession:
        def get(self, url, **kwargs):
            return FakeResponse({"status": {"error_message": "nope"}})

    with pytest.raises(CmcMarketPairsError):
        fetch_market_pairs(
            BadSession(), "binance", "spot", max_attempts=1, min_interval_seconds=0
        )


def test_fetch_venue_market_pairs_skips_categories_the_venue_has_no_snapshot_for():
    session = PagedSession([_item()], page_size=10)
    results = fetch_venue_market_pairs(session, "coinbase-exchange", min_interval_seconds=0)
    assert set(results) == {"spot"}


def _row(row_id, symbol, row_type="spot", **extra):
    return {"id": row_id, "symbol": symbol, "type": row_type, **extra}


def test_match_market_pairs_finds_rows_by_native_id_and_type():
    rows = {
        "binance-spot": [_row("btcusdt", "BTC")],
        "binance-futures": [_row("btcusdt", "BTC", "perpetual")],
    }
    spot = _pair()
    perp = _pair(category="perpetual", derivativeTickerId="BTCUSDT")

    result = match_market_pairs([spot, perp], rows)

    assert result.matches[("binance-spot", "btcusdt")].category == "spot"
    assert result.matches[("binance-futures", "btcusdt")].category == "perpetual"
    assert result.unmatched == ()


def test_match_market_pairs_finds_lighter_rows_by_market_symbol():
    """Lighter's native ID is a market index; CMC knows the market by its symbol."""
    rows = {
        "lighter-perps": [
            _row("1", "BTC", "perpetual", base_asset="BTC"),
            _row("3", "TSLA", "perpetual", base_asset="TSLA"),
        ]
    }
    pair = _pair(
        venue="lighter",
        category="perpetual",
        marketPair="TSLA/USDC",
        derivativeTickerId="TSLA_USDC",
        marketUrl="https://app.lighter.xyz/trade/TSLA",
        baseSymbol="TSLA",
        baseCurrencyId=38152,
        quoteSymbol="USDC",
        quoteCurrencyId=3408,
    )

    result = match_market_pairs([pair], rows)

    assert set(result.matches) == {("lighter-perps", "3")}
    assert result.matches[("lighter-perps", "3")].base_cmc_id == 38152


def test_match_market_pairs_prefers_a_native_id_over_a_market_symbol():
    """The market-symbol index is a fallback: a row whose native ID matches wins."""
    rows = {
        "lighter-perps": [
            _row("TSLA", "TSLA", "perpetual", base_asset="OTHER"),
            _row("7", "TSLA", "perpetual", base_asset="TSLA"),
        ]
    }
    pair = _pair(
        venue="lighter",
        category="perpetual",
        derivativeTickerId="TSLA_USDC",
        marketUrl="https://app.lighter.xyz/trade/TSLA",
        baseSymbol="TSLA",
        quoteSymbol="USDC",
    )

    result = match_market_pairs([pair], rows)

    assert set(result.matches) == {("lighter-perps", "TSLA")}


def test_match_market_pairs_prefers_the_active_row():
    rows = {
        "okx-spot": [
            # A different asset once listed under the same ID; picking it
            # would read as a symbol mismatch.
            _row("BTC-USDT", "BTCX", end_date="2020-01-01T00:00:00Z", first_capture="2019-01-01T00:00:00Z"),
            _row("BTC-USDT", "BTC", first_capture="2021-01-01T00:00:00Z"),
        ],
    }
    pair = _pair(venue="okx", marketUrl="https://www.okx.com/trade-spot/btc-usdt")
    result = match_market_pairs([pair], rows)
    assert result.matches == {("okx-spot", "BTC-USDT"): pair}
    assert result.symbol_mismatches == ()


def test_match_market_pairs_falls_back_to_a_delisted_row():
    # Hyperliquid keeps delisted perpetuals in its universe and CMC keeps
    # tracking them, so a pair no active row trades is the delisted row's.
    rows = {
        "hyperliquid-perps": [
            _row("MKR", "MKR", "perpetual", first_capture="2024-10-29T00:00:00Z", end_date="2025-09-06T00:00:00Z"),
        ],
        "hyperliquid-spot": [_row("MKR", "MKR")],
    }
    pair = _pair(
        venue="hyperliquid",
        category="perpetual",
        marketPair="MKR/USDC",
        marketUrl="https://app.hyperliquid.xyz/trade/MKR",
        baseSymbol="MKR",
        baseCurrencyId=1518,
    )
    result = match_market_pairs([pair], rows)
    assert result.matches == {("hyperliquid-perps", "MKR"): pair}
    assert result.unmatched == ()


def test_match_market_pairs_tries_the_last_delisted_row_first():
    rows = {
        "okx-spot": [
            _row("BTC-USDT", "BTCX", end_date="2018-01-01T00:00:00Z", first_capture="2017-01-01T00:00:00Z"),
            _row("BTC-USDT", "BTC", end_date="2020-01-01T00:00:00Z", first_capture="2019-01-01T00:00:00Z"),
        ],
    }
    pair = _pair(venue="okx", marketUrl="https://www.okx.com/trade-spot/btc-usdt")
    result = match_market_pairs([pair], rows)
    assert result.matches == {("okx-spot", "BTC-USDT"): pair}


def test_match_market_pairs_reports_a_symbol_mismatch():
    rows = {"binance-spot": [_row("arbidr", "AR")]}
    pair = _pair(
        marketPair="ARB/IDR",
        marketUrl="https://www.binance.com/en/trade/ARB_IDR",
        baseSymbol="ARB",
        baseCurrencyId=11841,
        quoteSymbol="IDR",
    )
    result = match_market_pairs([pair], rows)
    assert result.matches == {}
    assert result.symbol_mismatches == ((("binance-spot", "arbidr"), pair),)


def test_match_market_pairs_reports_conflicting_pairs_for_one_row():
    rows = {"binance-spot": [_row("btcusdt", "BTC")]}
    first = _pair()
    second = _pair(marketId=2, baseCurrencyId=2)
    result = match_market_pairs([first, second], rows)
    assert ("binance-spot", "btcusdt") not in result.matches
    assert result.conflicts == (("binance-spot", "btcusdt"),)


def test_match_market_pairs_tolerates_repeated_identical_pairs():
    rows = {"gateio-futures-usdt": [_row("ETH_USDT_20261023", "ETH", "future")]}
    pair = _pair(
        venue="gate", category="futures", derivativeTickerId="ETH_USDT_20261023", baseSymbol="ETH"
    )
    result = match_market_pairs([pair, pair], rows)
    assert ("gateio-futures-usdt", "ETH_USDT_20261023") in result.matches


@pytest.mark.parametrize(
    ("row_symbol", "pair_symbol", "expected"),
    [
        ("BTC", "BTC", True),
        ("XAUT", "XAUt", True),
        ("1000PEPE", "1000PEPE", True),
        ("1000PEPE", "PEPE", True),
        ("PEPE", "1000PEPE", True),
        ("KPEPE", "kPEPE", True),
        ("KPEPE", "PEPE", True),
        ("XYZ:TSLA", "xyz:TSLA", True),
        ("XYZ:TSLA", "TSLA", True),
        ("AR", "ARB", False),
        ("ETH", "BTC", False),
        ("XBT", "BTC", True),
    ],
)
def test_symbols_consistent(row_symbol, pair_symbol, expected):
    assert symbols_consistent(row_symbol, pair_symbol) is expected


def test_every_venue_maps_categories_to_known_exchanges():
    from atlas.exchanges import EXCHANGE_DEFINITIONS

    assert set(CMC_VENUES) == {
        "binance",
        "bybit",
        "okx",
        "hyperliquid",
        "coinbase-exchange",
        "bitget",
        "gate",
        "aster-pro",
        "lighter",
        "grvt",
        "extended",
        "apex-omni",
        "pacifica",
        "variational",
    }
    for venue in CMC_VENUES.values():
        for exchanges in venue.exchanges.values():
            for exchange in exchanges:
                assert exchange in EXCHANGE_DEFINITIONS


def test_market_pair_is_hashable():
    assert hash(_pair()) == hash(_pair())
    assert isinstance(_pair(), MarketPair)


@pytest.mark.parametrize(
    ("row_symbol", "pair_symbol", "expected"),
    [("KAVA", "AVA", False), ("KNC", "NC", False), ("KMNO", "MNO", False), ("MBABYDOGE", "BABYDOGE", True)],
)
def test_letter_multiplier_prefixes_need_a_real_ticker(row_symbol, pair_symbol, expected):
    assert symbols_consistent(row_symbol, pair_symbol) is expected
