from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


_DATA_DIR = Path(__file__).parent / "data"


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class SecuritiesMaster:
    def __init__(
        self,
        internal_id_map: dict[tuple[str, str], str],
        symbol_windows: dict[str, list[tuple[str, datetime, datetime | None]]],
    ) -> None:
        self._internal_id_map = internal_id_map
        self._symbol_windows = symbol_windows

    @classmethod
    def load(
        cls,
        data_dir: Path = _DATA_DIR,
        exchanges: list[str] | None = None,
    ) -> SecuritiesMaster:
        """Load pre-computed internal_ids from per-exchange JSON files.

        The internal_id field is written by update.py; symbols without one
        (options, combos, unrecognised formats) are silently absent from the map.
        """
        internal_id_map: dict[tuple[str, str], str] = {}
        symbol_windows: dict[str, list[tuple[str, datetime, datetime | None]]] = {}
        for json_file in sorted(data_dir.glob("*.json")):
            exchange = json_file.stem
            if exchanges is not None and exchange not in exchanges:
                continue
            rows = json.loads(json_file.read_text())
            windows: list[tuple[str, datetime, datetime | None]] = []
            for sd in rows:
                start_dt = _parse_iso_timestamp(sd.get("tardis_first_capture"))
                if start_dt is None:
                    continue
                end_dt = _parse_iso_timestamp(sd.get("end_date"))
                windows.append((sd["id"], start_dt, end_dt))
                if iid := sd.get("internal_id"):
                    internal_id_map[(exchange, sd["id"])] = iid
            symbol_windows[exchange] = windows
        return cls(internal_id_map, symbol_windows)

    def by_exchange_and_original_id(
        self, exchange: str, original_id: str
    ) -> str | None:
        """Return the pre-computed internal_id for a given exchange + raw symbol id."""
        return self._internal_id_map.get((exchange, original_id))

    def symbol_ids(
        self, exchange: str, tardis_first_capture: datetime, end_date: datetime
    ) -> list[str]:
        """Return symbol ids active for the requested date window."""
        windows = self._symbol_windows.get(exchange, [])
        if not windows:
            return []

        start = (
            tardis_first_capture.replace(tzinfo=UTC)
            if tardis_first_capture.tzinfo is None
            else tardis_first_capture.astimezone(UTC)
        )
        end = (
            end_date.replace(tzinfo=UTC)
            if end_date.tzinfo is None
            else end_date.astimezone(UTC)
        )
        ids: list[str] = []
        for symbol_id, available_since, available_to in windows:
            if available_since <= start and (
                available_to is None or available_to >= end
            ):
                ids.append(symbol_id)
        return ids

    def __len__(self) -> int:
        return len(self._internal_id_map)

    def __repr__(self) -> str:
        exchanges = len({ex for ex, _ in self._internal_id_map})
        return f"SecuritiesMaster({len(self._internal_id_map)} symbols, {exchanges} exchanges)"
