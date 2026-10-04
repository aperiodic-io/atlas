"""Recover missing Hyperliquid first-capture dates from committed snapshots.

Run once before a historical CMC backfill, from a full Git checkout. Dates refer
to Atlas's first recorded observation, never to an inferred exchange listing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


EXCHANGES = ("hyperliquid-perps", "hyperliquid-spot")


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repository, check=True, capture_output=True, text=True
    ).stdout


def restore_first_captures(repository: Path, dry_run: bool = False) -> dict[str, int]:
    if _git(repository, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise ValueError("first-capture recovery requires full Git history")
    counts = {}
    for exchange in EXCHANGES:
        relative_path = f"atlas/data/{exchange}.json"
        path = repository / relative_path
        if not path.exists():
            continue
        rows = json.loads(path.read_text())
        if all(row.get("first_capture") for row in rows):
            continue
        captures = {}
        for line in _git(
            repository, "log", "--reverse", "--format=%H %aI", "--", relative_path
        ).splitlines():
            revision, date = line.split(" ", 1)
            historical = json.loads(
                _git(repository, "show", f"{revision}:{relative_path}")
            )
            ids = {row["id"] for row in historical}
            # Disappearance ends the instance. A later reappearance starts a
            # fresh capture rather than inheriting an unrelated old listing.
            captures = {key: value for key, value in captures.items() if key in ids}
            for row in historical:
                captures.setdefault(
                    row["id"],
                    (row.get("first_capture") or date.replace("+00:00", "Z"), revision),
                )
        updated = 0
        for row in rows:
            if not row.get("first_capture") and row["id"] in captures:
                row["first_capture"], revision = captures[row["id"]]
                row["first_capture_source"] = {"source": "git", "commit": revision}
                updated += 1
        if updated:
            counts[exchange] = updated
            if not dry_run:
                path.write_text(json.dumps(rows, indent=2))
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            restore_first_captures(args.repository, args.dry_run), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
