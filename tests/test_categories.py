from atlas.categories import (
    ASSET_CATEGORIES,
    categories_from_cmc_tags,
    normalize_categories,
)
from integrations import cmc_category_metadata


def test_memes_is_an_atlas_category():
    assert "Memes" in ASSET_CATEGORIES
    assert len(ASSET_CATEGORIES) == len(set(ASSET_CATEGORIES)) == 19


def test_cmc_integration_uses_the_atlas_taxonomy():
    assert cmc_category_metadata.INCLUDED_CATEGORY_TAGS is ASSET_CATEGORIES


def test_categories_from_cmc_tags_keeps_category_tags_and_memes_only():
    tags = [
        {"name": "Memes", "category": "INDUSTRY"},
        {"name": "Payments", "category": "INDUSTRY"},
        {"name": "DeFi", "category": "INDUSTRY"},
        {"name": "Layer 1", "category": "CATEGORY"},
        {"name": "Stablecoin", "category": "CATEGORY"},
        {"name": "Memes", "category": "PLATFORM"},
        "not-a-tag",
        {"category": "CATEGORY"},
    ]
    assert categories_from_cmc_tags(tags) == ["Layer 1", "Memes"]


def test_categories_from_cmc_tags_tolerates_missing_tags():
    assert categories_from_cmc_tags(None) == []
    assert categories_from_cmc_tags({"name": "DeFi"}) == []


def test_normalize_categories_orders_and_filters():
    assert normalize_categories(["Memes", "DeFi", "bogus", "DeFi"]) == ["DeFi", "Memes"]
    assert normalize_categories("DeFi") == []
