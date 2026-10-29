import json

import pytest

from atlas.assets import (
    ASSET_ID_PATTERN,
    Asset,
    AssetClass,
    AssetRegistry,
    is_valid_asset_id,
    mint_asset_id,
    sanitize_ticker,
)


def _asset(asset_id="BTC", cmc_id=1, **kwargs):
    defaults = {
        "ticker": asset_id.split(".")[0],
        "name": "Bitcoin",
        "asset_class": AssetClass.crypto,
        "cmc_id": cmc_id,
        "cmc_slug": "bitcoin",
    }
    defaults.update(kwargs)
    return Asset(asset_id=asset_id, **defaults)


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("BTC", "BTC"),
        ("btc", "BTC"),
        ("XAUt", "XAUT"),
        ("USD₮0", "USD0"),
        ("$WIF", "WIF"),
        ("1000PEPE", "1000PEPE"),
        ("wstETH", "WSTETH"),
        ("Ñ", "N"),
        ("  sol ", "SOL"),
        ("", ""),
        ("₮", ""),
    ],
)
def test_sanitize_ticker(ticker, expected):
    assert sanitize_ticker(ticker) == expected


def test_mint_uses_bare_ticker_when_free():
    assert mint_asset_id("btc", "bitcoin", taken=set()) == "BTC"


def test_mint_qualifies_a_taken_ticker_with_the_slug():
    assert mint_asset_id("NEIRO", "neiro-eth", taken={"NEIRO"}) == "NEIRO.NEIRO_ETH"


def test_mint_numbers_a_taken_qualified_id():
    taken = {"NEIRO", "NEIRO.NEIRO_ETH"}
    assert mint_asset_id("NEIRO", "neiro-eth", taken=taken) == "NEIRO.NEIRO_ETH_2"
    taken.add("NEIRO.NEIRO_ETH_2")
    assert mint_asset_id("NEIRO", "neiro-eth", taken=taken) == "NEIRO.NEIRO_ETH_3"


def test_mint_falls_back_to_the_qualifier_when_the_ticker_has_no_usable_characters():
    assert mint_asset_id("₮", "tether-unicode", taken=set()) == "TETHER_UNICODE"


def test_mint_without_any_usable_text_is_an_error():
    with pytest.raises(ValueError):
        mint_asset_id("", "", taken=set())


def test_mint_never_returns_an_id_that_is_taken_case_insensitively():
    assert mint_asset_id("btc", "bitcoin", taken={"BTC"}) == "BTC.BITCOIN"


@pytest.mark.parametrize(
    "asset_id",
    ["BTC", "1000SATS", "NEIRO.NEIRO_ETH", "TSLA.TESLA_DERIVATIVES", "NEIRO.NEIRO_ETH_2"],
)
def test_valid_asset_ids(asset_id):
    assert is_valid_asset_id(asset_id)
    assert ASSET_ID_PATTERN.fullmatch(asset_id)


@pytest.mark.parametrize(
    "asset_id",
    ["", "btc", "BTC-USDT", "BTC:USDT", "BTC.", ".BTC", "BTC..X", "BTC.X.Y", "BTC._X", "BTC X"],
)
def test_invalid_asset_ids(asset_id):
    assert not is_valid_asset_id(asset_id)


def test_asset_rejects_an_invalid_id():
    with pytest.raises(ValueError):
        _asset(asset_id="btc")


def test_asset_rejects_a_non_positive_cmc_id():
    with pytest.raises(ValueError):
        _asset(cmc_id=0)


def test_asset_normalizes_categories_to_the_atlas_taxonomy():
    asset = _asset(categories=("Memes", "Not A Category", "Layer 1", "Memes"))
    assert asset.categories == ("Layer 1", "Memes")


def test_registry_round_trip(tmp_path):
    path = tmp_path / "assets.json"
    registry = AssetRegistry(path)
    registry.add(_asset(categories=("Layer 1",), aliases=("XBT",)))
    registry.add(
        _asset(
            asset_id="USD",
            cmc_id=2781,
            ticker="USD",
            name="United States Dollar",
            asset_class=AssetClass.fiat,
            cmc_slug="united-states-dollar",
        )
    )
    registry.save()

    loaded = AssetRegistry.load(path)
    assert loaded.get("BTC") == registry.get("BTC")
    assert loaded.get("USD").asset_class is AssetClass.fiat
    assert loaded.by_cmc_id(2781).asset_id == "USD"
    assert [asset.asset_id for asset in loaded] == ["BTC", "USD"]
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert payload["assets"][0]["asset_id"] == "BTC"


def test_registry_load_missing_file_is_empty(tmp_path):
    assert len(AssetRegistry.load(tmp_path / "missing.json")) == 0


def test_registry_refuses_duplicate_ids(tmp_path):
    registry = AssetRegistry(tmp_path / "assets.json")
    registry.add(_asset())
    with pytest.raises(ValueError, match="already"):
        registry.add(_asset(cmc_id=2))


def test_registry_refuses_a_second_asset_for_one_cmc_id(tmp_path):
    registry = AssetRegistry(tmp_path / "assets.json")
    registry.add(_asset())
    with pytest.raises(ValueError, match="CMC ID 1"):
        registry.add(_asset(asset_id="BTC.OTHER", cmc_id=1))


def test_registry_mint_is_idempotent_per_cmc_id(tmp_path):
    registry = AssetRegistry(tmp_path / "assets.json")
    first = registry.mint(
        ticker="BTC", name="Bitcoin", asset_class=AssetClass.crypto, cmc_id=1, cmc_slug="bitcoin"
    )
    again = registry.mint(
        ticker="XBT", name="Bitcoin", asset_class=AssetClass.crypto, cmc_id=1, cmc_slug="bitcoin"
    )
    assert first.asset_id == again.asset_id == "BTC"
    assert len(registry) == 1


def test_registry_mint_disambiguates_tickers(tmp_path):
    registry = AssetRegistry(tmp_path / "assets.json")
    registry.mint(ticker="NEIRO", name="Neiro", asset_class=AssetClass.crypto, cmc_id=10, cmc_slug="neiro")
    other = registry.mint(
        ticker="NEIRO", name="Neiro on ETH", asset_class=AssetClass.crypto, cmc_id=11, cmc_slug="neiro-eth"
    )
    assert other.asset_id == "NEIRO.NEIRO_ETH"


def test_registry_update_keeps_identity_fields(tmp_path):
    registry = AssetRegistry(tmp_path / "assets.json")
    registry.add(_asset())
    updated = registry.update("BTC", ticker="BTC", name="Bitcoin (renamed)", categories=("Layer 1",))
    assert updated.name == "Bitcoin (renamed)"
    assert updated.categories == ("Layer 1",)
    with pytest.raises(ValueError):
        registry.update("BTC", cmc_id=2)
    with pytest.raises(ValueError):
        registry.update("BTC", asset_id="XBT")
    with pytest.raises(KeyError):
        registry.update("NOPE", name="x")


def test_registry_add_alias(tmp_path):
    registry = AssetRegistry(tmp_path / "assets.json")
    registry.add(_asset())
    registry.add_aliases("BTC", ["XBT", "BTC", "xbt", "UBTC"])
    assert registry.get("BTC").aliases == ("UBTC", "XBT")


def test_registry_rejects_unknown_schema(tmp_path):
    path = tmp_path / "assets.json"
    path.write_text(json.dumps({"schema_version": 99, "assets": []}))
    with pytest.raises(ValueError, match="schema"):
        AssetRegistry.load(path)


def test_registry_rejects_malformed_rows(tmp_path):
    path = tmp_path / "assets.json"
    path.write_text(json.dumps({"schema_version": 1, "assets": [{"asset_id": "BTC"}]}))
    with pytest.raises(ValueError, match="malformed"):
        AssetRegistry.load(path)


def test_registry_rejects_duplicate_rows_on_load(tmp_path):
    path = tmp_path / "assets.json"
    row = {
        "asset_id": "BTC",
        "ticker": "BTC",
        "name": "Bitcoin",
        "asset_class": "crypto",
        "cmc_id": 1,
    }
    path.write_text(json.dumps({"schema_version": 1, "assets": [row, row]}))
    with pytest.raises(ValueError, match="already"):
        AssetRegistry.load(path)


def test_registry_search_by_ticker(tmp_path):
    registry = AssetRegistry(tmp_path / "assets.json")
    registry.add(_asset())
    registry.add(_asset(asset_id="BTC.BITCOIN_WORMHOLE", cmc_id=99, ticker="BTC", cmc_slug="bitcoin-wormhole"))
    registry.add_aliases("BTC", ["XBT"])
    assert [asset.asset_id for asset in registry.find_by_ticker("btc")] == [
        "BTC",
        "BTC.BITCOIN_WORMHOLE",
    ]
    assert [asset.asset_id for asset in registry.find_by_ticker("XBT")] == ["BTC"]


def test_registry_round_trips_non_ascii_names_as_utf8(tmp_path, monkeypatch):
    path = tmp_path / "assets.json"
    registry = AssetRegistry(path)
    registry.add(_asset(asset_id="PLN", cmc_id=2805, ticker="PLN", name="Polish Złoty", asset_class=AssetClass.fiat))
    registry.save()
    assert "Złoty".encode() in path.read_bytes()
    assert AssetRegistry.load(path).get("PLN").name == "Polish Złoty"


def test_adopt_cmc_id_gives_a_native_asset_its_cmc_identity():
    registry = AssetRegistry()
    registry.mint(ticker="MELI", name="MELI", asset_class=AssetClass.equity)
    adopted = registry.adopt_cmc_id("MELI", 42700)
    assert adopted.asset_id == "MELI"
    assert registry.by_cmc_id(42700).asset_id == "MELI"


def test_adopt_cmc_id_never_replaces_a_cmc_identity_or_steals_one():
    registry = AssetRegistry()
    registry.mint(ticker="BTC", name="Bitcoin", asset_class=AssetClass.crypto, cmc_id=1)
    registry.mint(ticker="MELI", name="MELI", asset_class=AssetClass.equity)
    with pytest.raises(ValueError):
        registry.adopt_cmc_id("BTC", 2)
    with pytest.raises(ValueError):
        registry.adopt_cmc_id("MELI", 1)
