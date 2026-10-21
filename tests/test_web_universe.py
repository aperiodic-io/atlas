"""Which assets the web domain map covers, and how their contracts are counted."""

from __future__ import annotations

import pytest

from integrations.web.universe import (
    RegistryAsset,
    build_universe,
    normalize_symbol,
)


def reg(asset_id: str, cls: str = "crypto", **kw: object) -> RegistryAsset:
    return RegistryAsset(
        asset_id=asset_id,
        ticker=kw.pop("ticker", asset_id.split(".")[0]),  # type: ignore[arg-type]
        name=kw.pop("name", asset_id),  # type: ignore[arg-type]
        asset_class=cls,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("venue", "row", "want"),
    [
        (
            "hyperliquid-perps",
            {"id": "xyz:TSLA", "symbol": "XYZ:TSLA", "underlying": "equity"},
            "TSLA",
        ),
        ("bybit-perps", {"symbol": "1000BONK", "underlying": "crypto"}, "BONK"),
        ("bybit-perps", {"symbol": "1000000MOG"}, "MOG"),
        ("bitget-perps-usdt", {"symbol": "10000000AIDOGE"}, "AIDOGE"),
        ("binance-futures", {"symbol": "1MBABYDOGE"}, "BABYDOGE"),
        ("hyperliquid-perps", {"id": "kPEPE", "symbol": "KPEPE"}, "PEPE"),
        (
            "pacifica-perps",
            {"id": "kBONK", "base_asset": "kBONK", "symbol": "KBONK"},
            "BONK",
        ),
        (
            "hyperliquid-perps",
            {"id": "HYNA:1000PEPE", "symbol": "HYNA:1000PEPE"},
            "PEPE",
        ),
        ("mexc-perps", {"symbol": "TSLASTOCK", "underlying": "equity"}, "TSLA"),
        ("extended-perps", {"symbol": "TSLA_24_5", "underlying": "equity"}, "TSLA"),
        ("okx-spot", {"symbol": "XAAPL", "underlying": "equity"}, "AAPL"),
        (
            "bitget-perps-usdt",
            {"symbol": "005930KRW", "underlying": "equity"},
            "005930",
        ),
        ("mexc-perps", {"symbol": "OURASTOCK", "underlying": "pre_market"}, "OURA"),
        # Real tickers that look decorated are kept.
        ("binance-spot", {"symbol": "1INCH"}, "1INCH"),
        (
            "hyperliquid-perps",
            {"id": "KAITO", "symbol": "KAITO", "underlying": "crypto"},
            "KAITO",
        ),
        ("binance-futures", {"symbol": "XOM", "underlying": "equity"}, "XOM"),
        ("binance-spot", {"symbol": "1000STOCK"}, "STOCK"),
        ("bybit-perps", {"symbol": "1000X", "underlying": "crypto"}, "X"),
        ("bybit-perps", {"symbol": "KO", "underlying": "equity"}, "KO"),
    ],
)
def test_unresolved_symbols_normalise_to_one_key_per_asset(
    venue: str, row: dict, want: str
) -> None:
    # Rows Atlas symbology has not resolved fall back to this key; it must land
    # every venue's decoration of one asset on one key.
    assert normalize_symbol(venue, row) == want


def test_identity_comes_from_atlas_and_every_contract_counts() -> None:
    # Why: the primary of a shared domain or ticker is the asset with the most
    # Atlas contracts; multiplier contracts (1000PEPE) and delisted ones count,
    # and the asset is Atlas's, whatever the venue's symbol.
    registry = {
        "PEPE": reg("PEPE", cmc_id=24478),
        "LUNC": reg("LUNC", cmc_id=4172),
        "LUNA": reg("LUNA", cmc_id=20314),
    }
    snapshots = {
        "binance-futures": [
            {"symbol": "1000PEPE", "base_asset_id": "PEPE", "base_multiplier": 1000},
            {
                "symbol": "PEPE",
                "base_asset_id": "PEPE",
                "end_date": "2024-01-01T00:00:00Z",
            },
            # Terra's ticker era: the ended LUNA rows are LUNC to Atlas.
            {
                "symbol": "LUNA",
                "base_asset_id": "LUNC",
                "end_date": "2022-05-13T00:00:00Z",
            },
            {"symbol": "LUNA", "base_asset_id": "LUNA"},
        ],
        "bybit-futures": [{"id": "laptop"}],
    }
    universe = build_universe(registry, snapshots, {})
    pepe = universe.assets["PEPE"]
    assert (pepe.contracts, pepe.trading, pepe.by_symbol) == (2, 1, 0)
    assert pepe.symbols == {"1000PEPE", "PEPE"}
    assert universe.assets["LUNC"].contracts == 1
    assert universe.assets["LUNC"].ended == "2022-05-13"
    assert universe.assets["LUNA"].ended is None
    assert set(universe.assets) == {"PEPE", "LUNC", "LUNA"}


def test_unresolved_rows_join_only_an_asset_of_their_own_class() -> None:
    # Why: an equity perp on a venue symbology does not cover yet must count for
    # the company, never for a coin that happens to share the ticker.
    registry = {
        "CAT": reg("CAT", cmc_id=1, name="Simons Cat"),
        "CAT.CATERPILLAR": reg("CAT.CATERPILLAR", "equity", ticker="CAT"),
        "SKHYNIX.X": reg("000660", "equity", aliases=("SKHYNIX",)),
    }
    registry = {a.asset_id: a for a in registry.values()}
    snapshots = {
        "mexc-perps": [
            {"symbol": "CATSTOCK", "underlying": "equity"},
            {"symbol": "1000CAT"},
            {"symbol": "SKHYNIXSTOCK", "underlying": "equity"},
            {"symbol": "OPENAISTOCK", "underlying": "pre_market"},
            {"symbol": "NOSUCHCOIN"},
        ]
    }
    universe = build_universe(registry, snapshots, {})
    assert universe.assets["CAT.CATERPILLAR"].symbols == {"CATSTOCK"}
    assert universe.assets["CAT"].symbols == {"1000CAT"}
    assert universe.assets["000660"].symbols == {"SKHYNIXSTOCK"}
    # A TradFi underlying Atlas has no asset for becomes one of its own...
    assert universe.assets["OPENAI"].asset_id is None
    assert universe.assets["OPENAI"].asset_class == "pre_market"
    # ...a coin Atlas could not identify does not.
    assert "NOSUCHCOIN" not in universe.assets
    assert universe.unresolved["NOSUCHCOIN"].contracts == 1


def test_a_reused_ticker_does_not_absorb_the_delisted_asset() -> None:
    # Why: Bitcoin Gold's BTG contracts ended in 2022; the coin Atlas calls BTG
    # today was added to CMC in 2025. Old contracts must not count (or vote) for it.
    registry = {"BTG": reg("BTG", cmc_id=38530, cmc_date_added="2025-06-01T00:00:00Z")}
    snapshots = {
        "okx-spot": [{"symbol": "BTG", "end_date": "2023-03-30T00:00:00Z"}],
        "gateio-spot": [{"symbol": "BTG", "base_asset_id": "BTG"}],
    }
    universe = build_universe(registry, snapshots, {})
    assert universe.assets["BTG"].contracts == 1
    assert universe.unresolved["BTG.CRYPTO"].contracts == 1
    assert universe.unresolved["BTG.CRYPTO"].ended == "2023-03-30"


def test_aliases_route_unresolved_symbols_and_classes_map_to_the_map_vocabulary() -> (
    None
):
    registry = {
        "EUR": reg("EUR", "fiat"),
        "TSLA": reg("TSLA", "equity"),
        "USDBRL": reg("USDBRL", "unknown"),
    }
    snapshots = {
        "bitget-perps-usdt": [
            {"symbol": "EURUSD", "underlying": "forex"},
            {"symbol": "TESLA", "underlying": "equity"},
            {"symbol": "USDBRL", "base_asset_id": "USDBRL", "underlying": "forex"},
        ]
    }
    universe = build_universe(registry, snapshots, {"EURUSD": "EUR", "TESLA": "TSLA"})
    assert universe.assets["EUR"].asset_class == "forex"
    assert universe.assets["TSLA"].symbols == {"TESLA"}
    # An Atlas asset of class unknown takes the class its venues declare.
    assert universe.assets["USDBRL"].asset_class == "forex"
