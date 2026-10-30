"""The Atlas asset registry: one stable, descriptive string ID per asset.

An *asset* is the thing an instrument trades -- Bitcoin, Tether, the US dollar,
Tesla stock -- as opposed to an exchange contract on it. Every instrument row in
an Atlas snapshot points at up to three assets (``base_asset_id``,
``quote_asset_id`` and, for derivatives, ``margin_asset_id``), so the same asset
gets the same ID on every exchange, and two different assets that happen to
share a ticker never do.

Asset IDs are Atlas's own. They are minted once, from the asset's ticker, and
never change afterwards, even if the ticker does:

* the bare upper-case ticker when it is free: ``BTC``, ``ETH``, ``USD``;
* ``<TICKER>.<QUALIFIER>`` when another asset already holds the ticker, where
  the qualifier is the asset's descriptive slug: ``NEIRO.NEIRO_ETH``;
* a numeric suffix only in the vanishingly rare case the qualified form is taken
  too: ``NEIRO.NEIRO_ETH_2``.

External identifiers -- currently the CoinMarketCap ID and slug -- are recorded
as attributes of an asset, so the registry can be resolved against other
providers later without renumbering anything.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .categories import normalize_categories

SCHEMA_VERSION = 1
DEFAULT_REGISTRY_PATH = Path(__file__).parent / "data" / "symbology" / "assets.json"

_SEGMENT = r"[A-Z0-9]+(?:_[A-Z0-9]+)*"
ASSET_ID_PATTERN = re.compile(rf"{_SEGMENT}(?:\.{_SEGMENT})?")

# Fields that make up an asset's identity. They are fixed when it is minted.
_IDENTITY_FIELDS = frozenset({"asset_id", "cmc_id"})


class AssetClass(StrEnum):
    crypto = "crypto"
    fiat = "fiat"
    equity = "equity"
    commodity = "commodity"
    index = "index"
    # A currency pair's exchange rate (``EURUSD``, ``USDJPY``): what a forex
    # perpetual tracks. The currencies themselves are ``fiat``.
    forex = "forex"
    unknown = "unknown"


def is_valid_asset_id(value: object) -> bool:
    return isinstance(value, str) and ASSET_ID_PATTERN.fullmatch(value) is not None


def sanitize_ticker(value: str) -> str:
    """Reduce a ticker to the characters an asset ID may contain.

    Accents are folded (``Ñ`` -> ``N``), everything else that is not an ASCII
    letter or digit is dropped (``USD₮0`` -> ``USD0``, ``$WIF`` -> ``WIF``).
    """
    folded = unicodedata.normalize("NFKD", value)
    return "".join(
        char for char in folded.upper() if char.isascii() and char.isalnum()
    )


def _sanitize_qualifier(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).upper()
    words = re.split(r"[^A-Z0-9]+", "".join(c for c in folded if c.isascii()))
    return "_".join(word for word in words if word)


def mint_asset_id(ticker: str, qualifier: str, taken: Iterable[str]) -> str:
    """Return a new asset ID for ``ticker`` that is not in ``taken``.

    ``qualifier`` disambiguates a ticker that is already in use and should be
    descriptive -- an asset's slug, for example ``neiro-eth``.
    """
    taken_ids = {asset_id.upper() for asset_id in taken}
    base = sanitize_ticker(ticker)
    suffix = _sanitize_qualifier(qualifier)
    if not base:
        if not suffix:
            raise ValueError(f"cannot mint an asset ID from {ticker!r}/{qualifier!r}")
        base, suffix = suffix, ""
    if base not in taken_ids:
        return base
    candidate = f"{base}.{suffix}" if suffix else f"{base}.X"
    if candidate not in taken_ids:
        return candidate
    number = 2
    while f"{candidate}_{number}" in taken_ids:
        number += 1
    return f"{candidate}_{number}"


@dataclass(frozen=True)
class Asset:
    asset_id: str
    ticker: str
    name: str
    asset_class: AssetClass
    cmc_id: int | None = None
    cmc_slug: str | None = None
    categories: tuple[str, ...] = ()
    # Venue tickers, other than ``ticker``, that were resolved to this asset:
    # ``XBT`` for Bitcoin, ``UBTC`` for Hyperliquid's unit Bitcoin.
    aliases: tuple[str, ...] = ()
    status: str = "active"
    # When CoinMarketCap first listed the asset (ISO 8601). Kept so the
    # ticker-reuse guard works without fetching CMC.
    cmc_date_added: str | None = None
    minted_at: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d")
    )

    def __post_init__(self) -> None:
        if not is_valid_asset_id(self.asset_id):
            raise ValueError(f"invalid asset ID {self.asset_id!r}")
        if self.cmc_id is not None and (
            not isinstance(self.cmc_id, int)
            or isinstance(self.cmc_id, bool)
            or self.cmc_id <= 0
        ):
            raise ValueError(f"invalid CMC ID {self.cmc_id!r} for {self.asset_id}")
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        object.__setattr__(
            self, "categories", tuple(normalize_categories(list(self.categories)))
        )
        object.__setattr__(self, "aliases", tuple(sorted(set(self.aliases))))

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["asset_class"] = self.asset_class.value
        payload["categories"] = list(self.categories)
        payload["aliases"] = list(self.aliases)
        return {
            key: value
            for key, value in payload.items()
            if value is not None and value != []
        }

    @classmethod
    def from_json(cls, row: object) -> Asset:
        if not isinstance(row, dict):
            raise ValueError("malformed asset row: not an object")
        try:
            return cls(
                asset_id=str(row["asset_id"]),
                ticker=str(row["ticker"]),
                name=str(row["name"]),
                asset_class=AssetClass(row["asset_class"]),
                cmc_id=row.get("cmc_id"),
                cmc_slug=row.get("cmc_slug"),
                categories=tuple(row.get("categories") or ()),
                aliases=tuple(row.get("aliases") or ()),
                status=str(row.get("status") or "active"),
                cmc_date_added=row.get("cmc_date_added"),
                minted_at=str(row.get("minted_at") or ""),
            )
        except (KeyError, TypeError) as error:
            raise ValueError(f"malformed asset row {row.get('asset_id')!r}: {error}") from error


class AssetRegistry:
    """The set of known assets, keyed by asset ID and by CoinMarketCap ID."""

    def __init__(self, path: Path = DEFAULT_REGISTRY_PATH) -> None:
        self.path = path
        self._assets: dict[str, Asset] = {}
        self._by_cmc_id: dict[int, str] = {}

    @classmethod
    def load(cls, path: Path = DEFAULT_REGISTRY_PATH) -> AssetRegistry:
        registry = cls(path)
        if not path.exists():
            return registry
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported asset registry schema")
        rows = payload.get("assets")
        if not isinstance(rows, list):
            raise ValueError("malformed asset registry: assets is not a list")
        for row in rows:
            registry.add(Asset.from_json(row))
        return registry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "assets": [asset.to_json() for asset in self],
        }
        temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary_path.replace(self.path)

    def __len__(self) -> int:
        return len(self._assets)

    def __iter__(self) -> Iterator[Asset]:
        return iter(self._assets[asset_id] for asset_id in sorted(self._assets))

    def __contains__(self, asset_id: object) -> bool:
        return asset_id in self._assets

    def get(self, asset_id: str) -> Asset | None:
        return self._assets.get(asset_id)

    def by_cmc_id(self, cmc_id: int) -> Asset | None:
        asset_id = self._by_cmc_id.get(cmc_id)
        return None if asset_id is None else self._assets[asset_id]

    def find_by_ticker(self, ticker: str) -> list[Asset]:
        """Every asset whose ticker or alias is ``ticker``, ignoring case."""
        wanted = sanitize_ticker(ticker)
        return [
            asset
            for asset in self
            if sanitize_ticker(asset.ticker) == wanted
            or any(sanitize_ticker(alias) == wanted for alias in asset.aliases)
        ]

    def add(self, asset: Asset) -> Asset:
        if asset.asset_id in self._assets:
            raise ValueError(f"asset ID {asset.asset_id} is already registered")
        if asset.cmc_id is not None and asset.cmc_id in self._by_cmc_id:
            raise ValueError(
                f"CMC ID {asset.cmc_id} is already registered as "
                f"{self._by_cmc_id[asset.cmc_id]}"
            )
        self._assets[asset.asset_id] = asset
        if asset.cmc_id is not None:
            self._by_cmc_id[asset.cmc_id] = asset.asset_id
        return asset

    def mint(
        self,
        *,
        ticker: str,
        name: str,
        asset_class: AssetClass,
        cmc_id: int | None = None,
        cmc_slug: str | None = None,
        qualifier: str | None = None,
        categories: Iterable[str] = (),
        status: str = "active",
        cmc_date_added: str | None = None,
    ) -> Asset:
        """Register a new asset, or return the one already holding ``cmc_id``."""
        if cmc_id is not None and (existing := self.by_cmc_id(cmc_id)) is not None:
            return existing
        asset_id = mint_asset_id(
            ticker, qualifier or cmc_slug or name, taken=self._assets.keys()
        )
        return self.add(
            Asset(
                asset_id=asset_id,
                ticker=ticker,
                name=name,
                asset_class=asset_class,
                cmc_id=cmc_id,
                cmc_slug=cmc_slug,
                categories=tuple(categories),
                status=status,
                cmc_date_added=cmc_date_added,
            )
        )

    def update(self, asset_id: str, /, **changes: Any) -> Asset:
        """Replace an asset's descriptive metadata; its identity never changes."""
        current = self._assets.get(asset_id)
        if current is None:
            raise KeyError(asset_id)
        blocked = _IDENTITY_FIELDS.intersection(changes)
        if any(changes[key] != getattr(current, key) for key in blocked):
            raise ValueError(f"cannot change {sorted(blocked)} of {asset_id}")
        updated = replace(current, **changes)
        self._assets[asset_id] = updated
        return updated

    def adopt_cmc_id(self, asset_id: str, cmc_id: int) -> Asset:
        """Give an Atlas-native asset the CMC ID of the asset it is.

        For a reference asset Atlas minted before CoinMarketCap tracked it:
        the asset keeps its ID and gains the CMC identity, rather than CMC's
        listing becoming a second asset. An asset that has a CMC ID keeps it.
        """
        current = self._assets[asset_id]
        if current.cmc_id is not None:
            raise ValueError(f"{asset_id} already has CMC ID {current.cmc_id}")
        if cmc_id in self._by_cmc_id:
            raise ValueError(f"CMC ID {cmc_id} is already registered as {self._by_cmc_id[cmc_id]}")
        adopted = replace(current, cmc_id=cmc_id)
        self._assets[asset_id] = adopted
        self._by_cmc_id[cmc_id] = asset_id
        return adopted

    def add_aliases(self, asset_id: str, aliases: Iterable[str]) -> Asset:
        current = self._assets[asset_id]
        own = sanitize_ticker(current.ticker)
        merged = set(current.aliases)
        for alias in aliases:
            normalized = alias.strip().upper()
            if normalized and sanitize_ticker(normalized) != own:
                merged.add(normalized)
        if merged == set(current.aliases):
            return current
        return self.update(asset_id, aliases=tuple(merged))


__all__ = [
    "ASSET_ID_PATTERN",
    "DEFAULT_REGISTRY_PATH",
    "Asset",
    "AssetClass",
    "AssetRegistry",
    "is_valid_asset_id",
    "mint_asset_id",
    "sanitize_ticker",
]
