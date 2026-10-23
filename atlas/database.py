from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from atlas.exchange_definitions.common import parse_utc_timestamp
from .assets import Asset, AssetRegistry
from .categories import ASSET_CATEGORIES


_DATA_DIR = Path(__file__).parent / "data"
_REGISTRY_RELATIVE_PATH = Path("symbology") / "assets.json"


class SecurityMaster:
    def __init__(
        self,
        internal_id_map: dict[tuple[str, str], str],
        symbol_windows: dict[str, list[tuple[str, datetime, datetime | None]]],
        contract_index: dict[tuple[str, str, str | None], set[str]],
        metadata_map: dict[tuple[str, str], dict],
        registry: AssetRegistry | None = None,
    ) -> None:
        self._internal_id_map = internal_id_map
        self._symbol_windows = symbol_windows
        self._contract_index = contract_index
        self._metadata_map = metadata_map
        self._registry = registry if registry is not None else AssetRegistry()
        self._asset_index: dict[str, set[tuple[str, str]]] = {}
        self._unified_index: dict[str, set[tuple[str, str]]] = {}
        for key, row in metadata_map.items():
            asset_ids = {row.get("base_asset_id")} | {
                segment.get("asset_id")
                for segment in row.get("base_asset_history") or []
                if isinstance(segment, dict)
            }
            for asset_id in asset_ids:
                if isinstance(asset_id, str):
                    self._asset_index.setdefault(asset_id, set()).add(key)
            if isinstance(unified := row.get("unified_id"), str):
                self._unified_index.setdefault(unified, set()).add(key)

    @classmethod
    def load(
        cls,
        data_dir: Path = _DATA_DIR,
        exchanges: list[str] | None = None,
        registry_path: Path | None = None,
    ) -> SecurityMaster:
        """Load pre-computed internal_ids from per-exchange JSON files.

        The internal_id field is written by update.py; symbols without one
        (options, combos, unrecognised formats) are silently absent from the map.
        The asset registry is read from ``<data_dir>/symbology/assets.json``
        unless ``registry_path`` names another file.
        """
        internal_id_map: dict[tuple[str, str], str] = {}
        symbol_windows: dict[str, list[tuple[str, datetime, datetime | None]]] = {}
        contract_index: dict[tuple[str, str, str | None], set[str]] = {}
        metadata_map: dict[tuple[str, str], dict] = {}
        for json_file in sorted(data_dir.glob("*.json")):
            exchange = json_file.stem
            if exchanges is not None and exchange not in exchanges:
                continue
            rows = json.loads(json_file.read_text())
            if not isinstance(rows, list):
                continue
            windows: list[tuple[str, datetime, datetime | None]] = []
            for sd in rows:
                if not isinstance(sd, dict):
                    continue
                if isinstance(sd.get("id"), str):
                    metadata_map[(exchange, sd["id"])] = sd.copy()
                start_dt = parse_utc_timestamp(sd.get("first_capture"))
                if start_dt is None:
                    continue
                end_dt = parse_utc_timestamp(sd.get("end_date"))
                windows.append((sd["id"], start_dt, end_dt))
                if iid := sd.get("internal_id"):
                    internal_id_map[(exchange, sd["id"])] = iid
                symbol = sd.get("symbol")
                denominator = sd.get("denominator")
                if symbol and denominator:
                    margin = sd.get("margin")
                    key = (
                        str(symbol).upper(),
                        str(denominator).upper(),
                        str(margin).upper() if isinstance(margin, str) else None,
                    )
                    contract_index.setdefault(key, set()).add(exchange)
            symbol_windows[exchange] = windows
        registry = AssetRegistry.load(registry_path or data_dir / _REGISTRY_RELATIVE_PATH)
        return cls(internal_id_map, symbol_windows, contract_index, metadata_map, registry)

    def tick_size(
        self, exchange: str, original_id: str, at: datetime | None = None
    ) -> float | None:
        """An instrument's tick size, optionally as of ``at``.

        Venues re-tick contracts in place; ``tick_size_history`` records each
        tick with the time it took effect (or, when found by the daily
        refresh, was first seen). Before the first entry the tick is unknown.
        """
        row = self._metadata_map.get((exchange, original_id))
        if row is None:
            return None
        if at is None:
            return row.get("tick_size")
        moment = at.replace(tzinfo=UTC) if at.tzinfo is None else at
        tick = None
        for entry in row.get("tick_size_history") or []:
            if parse_utc_timestamp(entry["effective_from"]) > moment:
                break
            tick = entry["value"]
        return tick

    def by_exchange_and_original_id(
        self, exchange: str, original_id: str
    ) -> str | None:
        """Return the pre-computed internal_id for a given exchange + raw symbol id."""
        return self._internal_id_map.get((exchange, original_id))

    def instrument_metadata(self, exchange: str, original_id: str) -> dict | None:
        """Return a copy of the security-master record for one raw instrument."""
        metadata = self._metadata_map.get((exchange, original_id))
        return metadata.copy() if metadata is not None else None

    def exchanges_for_original_id(self, original_id: str) -> list[str]:
        """Return all exchanges that contain the given raw symbol id."""
        exchanges = {
            exchange
            for (exchange, oid) in self._internal_id_map
            if oid == original_id
        }
        return sorted(exchanges)

    def exchanges_for_contract(
        self, symbol: str, denominator: str, margin: str | None
    ) -> list[str]:
        """Return all exchanges that contain the given symbol/denominator/margin."""
        key = (
            symbol.upper(),
            denominator.upper(),
            margin.upper() if isinstance(margin, str) else None,
        )
        return sorted(self._contract_index.get(key, set()))

    def symbol_ids(
        self, exchange: str, first_capture: datetime, end_date: datetime
    ) -> list[str]:
        """Return symbol ids that were available for any part of the requested
        date window (day-granularity overlap, not full-window containment).

        A symbol listed mid-window (e.g. a mid-month listing within a
        whole-month window) still overlaps the window and must be included,
        even though it wasn't available for the window's full duration.
        """
        windows = self._symbol_windows.get(exchange, [])
        if not windows:
            return []

        start = (
            first_capture.replace(tzinfo=UTC)
            if first_capture.tzinfo is None
            else first_capture.astimezone(UTC)
        )
        end = (
            end_date.replace(tzinfo=UTC)
            if end_date.tzinfo is None
            else end_date.astimezone(UTC)
        )
        ids: list[str] = []
        for symbol_id, available_since, available_to in windows:
            if available_since < end and (
                available_to is None or available_to > start
            ):
                ids.append(symbol_id)
        return ids

    # -- Assets ---------------------------------------------------------------

    @property
    def assets(self) -> AssetRegistry:
        """The Atlas asset registry the snapshots' asset IDs refer to."""
        return self._registry

    def asset(self, asset_id: str) -> Asset | None:
        return self._registry.get(asset_id)

    def asset_by_cmc_id(self, cmc_id: int) -> Asset | None:
        return self._registry.by_cmc_id(cmc_id)

    def find_assets(self, ticker: str) -> list[Asset]:
        """Every asset with this ticker or alias; several assets may share one."""
        return self._registry.find_by_ticker(ticker)

    def assets_in_category(self, category: str) -> list[Asset]:
        if category not in ASSET_CATEGORIES:
            raise ValueError(f"unknown asset category {category!r}")
        return [asset for asset in self._registry if category in asset.categories]

    def base_asset(
        self, exchange: str, original_id: str, at: datetime | None = None
    ) -> Asset | None:
        """The asset an instrument trades, optionally as of ``at``.

        A venue that handed a ticker to a new project without renaming the
        instrument (Terra's ``LUNA``) records the earlier asset in
        ``base_asset_history``; ``at`` selects the asset in effect then.
        """
        row = self._metadata_map.get((exchange, original_id))
        if row is None:
            return None
        if at is not None:
            moment = at.replace(tzinfo=UTC) if at.tzinfo is None else at
            for segment in row.get("base_asset_history") or []:
                until = parse_utc_timestamp(segment.get("until"))
                if until is not None and moment < until:
                    return self.asset(segment["asset_id"])
        return self._asset_field(row, "base_asset_id")

    def quote_asset(self, exchange: str, original_id: str) -> Asset | None:
        return self._asset_field(self._metadata_map.get((exchange, original_id)), "quote_asset_id")

    def margin_asset(self, exchange: str, original_id: str) -> Asset | None:
        return self._asset_field(self._metadata_map.get((exchange, original_id)), "margin_asset_id")

    def unified_id(self, exchange: str, original_id: str) -> str | None:
        """The instrument's ID in Atlas asset terms: ``perpetual-PEPE-USDT:USDT``."""
        row = self._metadata_map.get((exchange, original_id))
        return None if row is None else row.get("unified_id")

    def by_unified_id(self, unified_id: str) -> list[tuple[str, str]]:
        """Every ``(exchange, original_id)`` trading this unified contract."""
        return sorted(self._unified_index.get(unified_id, ()))

    def instruments_for_asset(
        self, asset_id: str, exchange: str | None = None
    ) -> list[tuple[str, str]]:
        """Every ``(exchange, original_id)`` whose base asset is, or was, ``asset_id``."""
        return sorted(
            key
            for key in self._asset_index.get(asset_id, ())
            if exchange is None or key[0] == exchange
        )

    def exchanges_for_asset(self, asset_id: str) -> list[str]:
        return sorted({exchange for exchange, _ in self._asset_index.get(asset_id, ())})

    def _asset_field(self, row: dict | None, key: str) -> Asset | None:
        if row is None or not isinstance(asset_id := row.get(key), str):
            return None
        return self.asset(asset_id)

    def __len__(self) -> int:
        return len(self._internal_id_map)

    def __repr__(self) -> str:
        exchanges = len({ex for ex, _ in self._internal_id_map})
        return f"SecurityMaster({len(self._internal_id_map)} symbols, {exchanges} exchanges)"
