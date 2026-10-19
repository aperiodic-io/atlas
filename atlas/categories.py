"""Atlas asset categories.

The taxonomy started life as an allow list over CoinMarketCap tags; it is now
Atlas's own vocabulary. CMC remains the source the categories are populated
from, but an asset's categories live in the Atlas asset registry
(``atlas/data/symbology/assets.json``), so a consumer never needs CMC to read
or filter by them.

Order is significant: it is the order categories are listed on an asset.
"""

from __future__ import annotations

ASSET_CATEGORIES: tuple[str, ...] = (
    "DeFi",
    "Layer 1",
    "Layer 2",
    "Tokenized Stock",
    "Real World Assets Protocols",
    "Staking",
    "Play To Earn",
    "Generative AI",
    "Smart Contracts",
    "DApp",
    "Collectibles & NFTs",
    "Lending & Borrowing",
    "DAO",
    "Metaverse",
    "Privacy",
    "AI Agents",
    "AI Applications",
    "Interoperability",
    "Memes",
)

# CMC files most of the categories above under its ``CATEGORY`` tag type, but
# "Memes" sits under ``INDUSTRY``. No other INDUSTRY tag is wanted.
CMC_INDUSTRY_CATEGORIES: frozenset[str] = frozenset({"Memes"})


def categories_from_cmc_tags(tags: object) -> list[str]:
    """Return the Atlas categories carried by a CMC ``tags`` list, in Atlas order.

    Anything that is not a well-formed tag is ignored rather than raised: CMC
    attaches tags inconsistently, and a malformed one must not lose the rest.
    """
    if not isinstance(tags, list):
        return []
    names = {
        tag.get("name")
        for tag in tags
        if isinstance(tag, dict)
        and (
            tag.get("category") == "CATEGORY"
            or (
                tag.get("category") == "INDUSTRY"
                and tag.get("name") in CMC_INDUSTRY_CATEGORIES
            )
        )
    }
    return [name for name in ASSET_CATEGORIES if name in names]


def normalize_categories(categories: object) -> list[str]:
    """Keep only known Atlas categories, de-duplicated, in Atlas order."""
    if not isinstance(categories, list | tuple):
        return []
    wanted = set(categories)
    return [name for name in ASSET_CATEGORIES if name in wanted]


__all__ = [
    "ASSET_CATEGORIES",
    "CMC_INDUSTRY_CATEGORIES",
    "categories_from_cmc_tags",
    "normalize_categories",
]
