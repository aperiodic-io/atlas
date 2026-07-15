from datetime import UTC, datetime, timedelta

from integrations.cmc_id_probe import (
    CmcAsset,
    PriceObservation,
    ProbeStatus,
    classify_price_candidates,
    contract_multiplier_for_cmc_lookup,
    fetch_binance_spot_prices,
    fetch_cmc_catalogue,
    normalize_cmc_lookup_symbol,
    price_observations_from_binance_tickers,
)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = iter(payloads)
        self.calls: list[dict] = []

    def get(self, _url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        return FakeResponse(next(self.payloads))


def _asset(cmc_id: int, symbol: str, price: float) -> dict:
    return {
        "id": cmc_id,
        "symbol": symbol,
        "slug": f"{symbol.lower()}-{cmc_id}",
        "isActive": 1,
        "lastUpdated": "2026-07-15T14:00:00.000Z",
        "quotes": [{"name": "USD", "price": price}],
    }


def _observation(price: float, observed_at: datetime) -> PriceObservation:
    return PriceObservation(
        price=price,
        quote_currency="USD",
        observed_at=observed_at,
        venue="binance-spot",
        instrument_id="BTCUSDT",
    )


def test_catalogue_reports_duplicate_ids_and_skips_malformed_rows() -> None:
    session = FakeSession(
        [
            {
                "data": {
                    "totalCount": "3",
                    "cryptoCurrencyList": [_asset(1, "BTC", 100), _asset(2, "ETH", 50)],
                }
            },
            {
                "data": {
                    "totalCount": "3",
                    "cryptoCurrencyList": [_asset(2, "ETH", 50), _asset(3, "SOL", 20)],
                }
            },
            {
                "data": {
                    "totalCount": "3",
                    "cryptoCurrencyList": [{"id": 4, "symbol": "BAD", "quotes": []}],
                }
            },
        ]
    )

    catalogue = fetch_cmc_catalogue(session, page_size=2, max_attempts=1)

    assert [asset.cmc_id for asset in catalogue.assets] == [1, 2, 3]
    assert catalogue.diagnostics.returned_rows == 5
    assert catalogue.diagnostics.unique_ids == 3
    assert catalogue.diagnostics.duplicate_ids == (2,)
    assert catalogue.diagnostics.malformed_rows == 1
    assert not catalogue.diagnostics.is_complete
    assert [call["params"]["start"] for call in session.calls] == [1, 3, 5]


def test_catalogue_reports_total_count_changes_without_aborting() -> None:
    session = FakeSession(
        [
            {
                "data": {
                    "totalCount": "2",
                    "cryptoCurrencyList": [_asset(1, "BTC", 100)],
                }
            },
            {"data": {"totalCount": "3", "cryptoCurrencyList": []}},
        ]
    )

    catalogue = fetch_cmc_catalogue(session, page_size=1, max_attempts=1)

    assert catalogue.diagnostics.reported_total == 2
    assert catalogue.diagnostics.total_count_changed
    assert not catalogue.diagnostics.is_complete


def test_bulk_binance_prices_select_requested_usdt_spot_pairs() -> None:
    session = FakeSession(
        [
            [
                {
                    "symbol": "BTCUSDT",
                    "lastPrice": "100",
                    "closeTime": 1_784_091_600_000,
                },
                {
                    "symbol": "ETHUSDC",
                    "lastPrice": "50",
                    "closeTime": 1_784_091_600_000,
                },
            ]
        ]
    )

    observations = fetch_binance_spot_prices(session, ["BTC", "ETH"])

    assert set(observations) == {"BTC"}
    assert observations["BTC"].instrument_id == "BTCUSDT"


def test_ticker_observations_can_be_labeled_as_futures() -> None:
    observations = price_observations_from_binance_tickers(
        [
            {
                "symbol": "BTCUSDT",
                "lastPrice": "100",
                "closeTime": 1_784_091_600_000,
            }
        ],
        ["BTC"],
        venue="binance-futures",
    )

    assert observations["BTC"].venue == "binance-futures"


def test_normalize_cmc_lookup_symbol_drops_known_contract_multipliers() -> None:
    cmc_symbols = {"CHEEMS", "1000SATS", "0G"}

    assert normalize_cmc_lookup_symbol("1000cheemsusdt", cmc_symbols) == "CHEEMS"
    assert normalize_cmc_lookup_symbol("1000SATS", cmc_symbols) == "1000SATS"
    assert normalize_cmc_lookup_symbol("0G", cmc_symbols) == "0G"
    assert contract_multiplier_for_cmc_lookup(
        "1000cheemsusdt", "CHEEMS", cmc_symbols
    ) == 1000
    assert contract_multiplier_for_cmc_lookup("1000SATS", "1000SATS", cmc_symbols) == 1


def test_price_compatible_requires_timestamp_alignment() -> None:
    observed_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    candidate = CmcAsset(
        cmc_id=1,
        symbol="BTC",
        slug="bitcoin",
        price_usd=100,
        last_updated=observed_at - timedelta(seconds=30),
        is_active=True,
    )

    result = classify_price_candidates(
        [candidate],
        _observation(101, observed_at),
        max_relative_difference=0.05,
        max_timestamp_skew=timedelta(minutes=1),
    )

    assert result.status is ProbeStatus.PRICE_COMPATIBLE
    assert result.match is not None


def test_price_comparison_rejects_stale_candidates() -> None:
    observed_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    candidate = CmcAsset(
        cmc_id=1,
        symbol="BTC",
        slug="bitcoin",
        price_usd=100,
        last_updated=observed_at - timedelta(minutes=5),
        is_active=True,
    )

    result = classify_price_candidates(
        [candidate],
        _observation(100, observed_at),
        max_relative_difference=0.05,
        max_timestamp_skew=timedelta(minutes=1),
    )

    assert result.status is ProbeStatus.STALE_PRICE


def test_price_comparison_rejects_multiple_compatible_candidates() -> None:
    observed_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    candidates = [
        CmcAsset(1, "SOL", "solana", 100, observed_at, True),
        CmcAsset(2, "SOL", "wrapped-solana", 100.2, observed_at, True),
    ]

    result = classify_price_candidates(
        candidates,
        _observation(100, observed_at),
        max_relative_difference=0.05,
        max_timestamp_skew=timedelta(minutes=1),
    )

    assert result.status is ProbeStatus.MULTIPLE_PRICE_MATCHES
