"""Backfill contract_size_history in okx-perps.json from historical parquet."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


PARQUET = Path(__file__).parent.parent / "okx_swap_hourly_pit_contract_size.parquet"
METADATA = Path(__file__).parent.parent / "atlas" / "data" / "okx-perps.json"


def ticker_to_internal_id(ticker: str) -> str:
    base, rest = ticker.split("/")
    denominator, margin = rest.split(":")
    return f"perpetual-{base}-{denominator}:{margin}"


def build_history(group: pd.DataFrame) -> list[dict]:
    group = group.sort_values("date").dropna(subset=["contract_size"])
    mask = group["contract_size"] != group["contract_size"].shift()
    changes = group[mask]
    return [
        {
            "effective_from": row.date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "value": row.contract_size,
        }
        for row in changes.itertuples()
    ]


def main() -> None:
    print(f"Reading {PARQUET} ...")
    df = pd.read_parquet(PARQUET)
    df = df.dropna(subset=["contract_size"])

    print(f"Building change-logs for {df['ticker'].nunique()} tickers ...")
    histories: dict[str, list[dict]] = {}
    for ticker, group in df.groupby("ticker"):
        internal_id = ticker_to_internal_id(ticker)
        histories[internal_id] = build_history(group)

    print(f"Loading {METADATA} ...")
    entries = json.loads(METADATA.read_text())

    matched = 0
    for entry in entries:
        iid = entry.get("internal_id", "")
        if iid in histories:
            history = histories[iid]
            entry["contract_size_history"] = history
            entry["contract_size"] = history[-1]["value"]
            matched += 1

    METADATA.write_text(json.dumps(entries, indent=2))
    print(f"Done. {matched}/{len(entries)} entries populated.")


if __name__ == "__main__":
    main()
