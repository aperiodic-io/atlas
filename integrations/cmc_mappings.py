"""Versioned, per-instrument CMC mapping storage.

Mappings are intentionally kept outside Atlas snapshots until an authenticated
identity workflow approves them. The store permits multiple instrument
instances to share a CMC ID and keeps a relisted original ID distinct.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class InstrumentInstance:
    exchange: str
    original_id: str
    first_capture: datetime

    @property
    def key(self) -> str:
        timestamp = (
            self.first_capture.astimezone(UTC).isoformat().replace("+00:00", "Z")
        )
        return f"{self.exchange}:{self.original_id}:{timestamp}"


@dataclass(frozen=True)
class CmcMapping:
    instrument: InstrumentInstance
    cmc_id: int
    slug: str
    status: str
    method: str
    recorded_at: datetime
    evidence: dict[str, Any] = field(default_factory=dict)


class MappingStore:
    def __init__(
        self, path: Path, mappings: dict[str, CmcMapping] | None = None
    ) -> None:
        self.path = path
        self._mappings = mappings or {}

    @classmethod
    def load(cls, path: Path) -> MappingStore:
        if not path.exists():
            return cls(path)
        payload = json.loads(path.read_text())
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported CMC mapping store schema")
        mappings: dict[str, CmcMapping] = {}
        for row in payload.get("mappings", []):
            mapping = _mapping_from_json(row)
            mappings[mapping.instrument.key] = mapping
        return cls(path, mappings)

    def get(self, instrument: InstrumentInstance) -> CmcMapping | None:
        return self._mappings.get(instrument.key)

    def upsert(self, mapping: CmcMapping, allow_replace: bool = False) -> None:
        existing = self.get(mapping.instrument)
        if (
            existing is not None
            and existing.status == "approved"
            and existing.cmc_id != mapping.cmc_id
            and not allow_replace
        ):
            raise ValueError("refusing to replace an approved CMC mapping")
        self._mappings[mapping.instrument.key] = mapping

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "mappings": [
                _mapping_to_json(mapping)
                for _, mapping in sorted(self._mappings.items())
            ],
        }
        temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temporary_path.replace(self.path)


def _mapping_to_json(mapping: CmcMapping) -> dict[str, Any]:
    payload = asdict(mapping)
    payload["instrument"]["first_capture"] = _format_timestamp(
        mapping.instrument.first_capture
    )
    payload["recorded_at"] = _format_timestamp(mapping.recorded_at)
    return payload


def _mapping_from_json(row: object) -> CmcMapping:
    if not isinstance(row, dict) or not isinstance(row.get("instrument"), dict):
        raise ValueError("invalid CMC mapping row")
    instrument = row["instrument"]
    return CmcMapping(
        instrument=InstrumentInstance(
            exchange=str(instrument["exchange"]),
            original_id=str(instrument["original_id"]),
            first_capture=_parse_timestamp(instrument["first_capture"]),
        ),
        cmc_id=int(row["cmc_id"]),
        slug=str(row["slug"]),
        status=str(row["status"]),
        method=str(row["method"]),
        recorded_at=_parse_timestamp(row["recorded_at"]),
        evidence=dict(row.get("evidence", {})),
    )


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("mapping timestamp is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("mapping timestamp has no timezone")
    return parsed.astimezone(UTC)
