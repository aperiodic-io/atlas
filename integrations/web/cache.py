"""Committed caches under ``atlas/data/web/cache/``.

Each file maps a key to a row carrying the time it was fetched or checked, so a
scheduled run only asks upstream about new keys plus a bounded slice of the
oldest rows (websites change on rebrands; a rolling refresh keeps every row
younger than a known number of days without refetching everything daily).
Files are written one row per line, sorted, so a refresh diffs cleanly.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def load(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported cache schema")
    return dict(payload["rows"])


def dumps(rows: Mapping[str, Mapping[str, Any]]) -> str:
    lines = [
        f"  {json.dumps(key, ensure_ascii=False)}: "
        f"{json.dumps(rows[key], ensure_ascii=False, sort_keys=True)}"
        for key in sorted(rows, key=_sort_key)
    ]
    body = ",\n".join(lines)
    return f'{{\n "schema_version": {SCHEMA_VERSION},\n "rows": {{\n{body}\n }}\n}}\n'


def save(path: Path, rows: Mapping[str, Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(rows), encoding="utf-8")


def _sort_key(key: str) -> tuple[int, int, str]:
    # Numeric keys (CMC ids) sort numerically, the rest alphabetically.
    return (0, int(key), "") if key.isdigit() else (1, 0, key)


def select_refresh(
    wanted: Iterable[str],
    rows: Mapping[str, Mapping[str, Any]],
    stamp: str,
    budget: int,
    always: Iterable[str] = (),
) -> list[str]:
    """Keys to fetch this run: every wanted key without a row, every key in
    ``always`` (e.g. a domain that failed last time), then the ``budget`` wanted
    keys with the oldest ``stamp``. Missing keys are not limited by the budget:
    an asset new to the map is never left unchecked."""
    wanted = sorted(set(wanted), key=_sort_key)
    missing = [k for k in wanted if k not in rows]
    forced = [k for k in sorted(set(always) & set(wanted), key=_sort_key) if k in rows]
    rest = sorted(
        (k for k in wanted if k in rows and k not in set(forced)),
        key=lambda k: (str(rows[k].get(stamp, "")), _sort_key(k)),
    )
    return missing + forced + rest[: max(budget, 0)]
