"""Keep `v1/_coverage.json` in the raw data buckets up to date.

The raw buckets (`aperiodic-raw-trades`, `-quotes`, `-derivatives`) are written
by several workflows (the backfill, the daily export, our own live capture, gap
backfills), none of which know about each other. This job is the single writer
of each bucket's coverage file: it lists the bucket, so the file is always what
is actually there, whoever wrote it.

Per series (dataset, exchange, symbol) the file holds `first` and `last`
exchange-time days (for a monthly file from its footer statistics), the
`missing` periods between them with no file, `days` and `bytes`. aperiodic.io
reads the three files and merges them.

The file stays in the private buckets rather than in this repository: the site
publishes each series from a delayed start, and the real first days are not
public.

    python integrations/raw_coverage.py                 # write, fail if stale
    python integrations/raw_coverage.py --dry-run       # print the summary only
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = 1
PREFIX = f"v{SCHEMA_VERSION}"
COVERAGE_KEY = f"{PREFIX}/_coverage.json"
DEFAULT_BUCKET_PREFIX = "aperiodic-raw"
EXCHANGES = ("binance-futures", "okx-perps", "hyperliquid-perps")
GROUP_DATASETS: dict[str, tuple[str, ...]] = {
    "trades": ("trades",),
    "quotes": ("quotes",),
    "derivatives": ("mark_price", "index_price", "funding_rate", "open_interest"),
}
# Served files are monthly before this day, daily from it.
DAILY_FROM = date(2026, 8, 1)
DAY_US = 86_400 * 1_000_000
_EPOCH = date(1970, 1, 1)


class Bucket(Protocol):
    def list(self, prefix: str) -> list[tuple[str, int]]: ...

    def get_range(self, key: str, start: int, end: int) -> bytes: ...

    def put(self, key: str, body: bytes) -> None: ...


class R2Bucket:
    def __init__(self, client: Any, name: str) -> None:
        self.client = client
        self.name = name

    def list(self, prefix: str) -> list[tuple[str, int]]:
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.name, Prefix=prefix
        )
        return [
            (o["Key"], o["Size"]) for page in pages for o in page.get("Contents", [])
        ]

    def get_range(self, key: str, start: int, end: int) -> bytes:
        response = self.client.get_object(
            Bucket=self.name, Key=key, Range=f"bytes={start}-{end - 1}"
        )
        return response["Body"].read()

    def put(self, key: str, body: bytes) -> None:
        self.client.put_object(
            Bucket=self.name, Key=key, Body=body, ContentType="application/json"
        )


def period_of(day: date) -> str:
    return f"{day:%Y-%m}" if day < DAILY_FROM else f"{day:%Y-%m-%d}"


def is_monthly(period: str) -> bool:
    return len(period) == 7


def period_bounds(period: str) -> tuple[date, date]:
    """First day and the day after the last day of a period."""
    if is_monthly(period):
        first = date(int(period[:4]), int(period[5:7]), 1)
        after = date(first.year + first.month // 12, first.month % 12 + 1, 1)
        return first, after
    first = date.fromisoformat(period)
    return first, first + timedelta(days=1)


def periods_between(first: str, last: str) -> list[str]:
    """Every period from `first` to `last`: months before DAILY_FROM, then days."""
    out = []
    day = period_bounds(first)[0]
    end = period_bounds(last)[1]
    while day < end:
        period = period_of(day)
        out.append(period)
        day = period_bounds(period)[1]
    return out


def parse_object_key(key: str) -> tuple[str, str, str, str] | None:
    """(dataset, exchange, symbol, period) of a served file's key, or None.

    `v1/{dataset}/exchange=…/symbol=…/year=YYYY/month=MM[/day=DD]/data.parquet`
    """
    parts = key.split("/")
    if len(parts) not in (7, 8) or parts[0] != PREFIX or parts[-1] != "data.parquet":
        return None
    try:
        exchange = parts[2].removeprefix("exchange=")
        symbol = parts[3].removeprefix("symbol=")
        period = f"{parts[4].removeprefix('year=')}-{parts[5].removeprefix('month=')}"
        if len(parts) == 8:
            period += "-" + parts[6].removeprefix("day=")
        period_bounds(period)
    except ValueError:
        return None
    return parts[1], exchange, symbol, period


def footer_days(bucket: Bucket, key: str, size: int) -> tuple[date, date] | None:
    """First and last exchange-time day of a file, from its footer statistics.

    Two small range reads; the footer is parsed on its own (column chunk
    offsets aren't needed for statistics).
    """
    import pyarrow.parquet as pq

    tail = bucket.get_range(key, size - 8, size)
    length = int.from_bytes(tail[:4], "little")
    footer = bucket.get_range(key, size - 8 - length, size - 8)
    meta = pq.read_metadata(io.BytesIO(b"PAR1" + footer + tail))
    index = meta.schema.names.index("exchange_timestamp")
    lows, highs = [], []
    for group in range(meta.num_row_groups):
        stats = meta.row_group(group).column(index).statistics
        if stats is None or not stats.has_min_max:
            return None
        lows.append(stats.min_raw)
        highs.append(stats.max_raw)
    if not lows:
        return None
    return (
        _EPOCH + timedelta(days=min(lows) // DAY_US),
        _EPOCH + timedelta(days=max(highs) // DAY_US),
    )


def build_coverage(bucket: Bucket, datasets: tuple[str, ...]) -> dict[str, Any]:
    """The coverage file for `datasets`, from a listing of `bucket`.

    Gaps inside a monthly file are not visible here.
    """
    prefixes = [f"{PREFIX}/{d}/exchange={e}/" for d in datasets for e in EXCHANGES]
    with ThreadPoolExecutor(len(prefixes)) as pool:
        listings = list(pool.map(bucket.list, prefixes))
    series: dict[tuple[str, str, str], dict[str, tuple[str, int]]] = defaultdict(dict)
    for listing in listings:
        for key, size in listing:
            parsed = parse_object_key(key)
            if parsed is not None:
                dataset, exchange, symbol, period = parsed
                series[dataset, exchange, symbol][period] = (key, size)

    def edges(files: dict[str, tuple[str, int]]) -> tuple[date, date]:
        ordered = sorted(files)
        first_period, last_period = ordered[0], ordered[-1]
        first = last = None
        if is_monthly(first_period):
            span = footer_days(bucket, *files[first_period])
            first = span[0] if span else None
        if is_monthly(last_period):
            span = footer_days(bucket, *files[last_period])
            last = span[1] if span else None
        return (
            first or period_bounds(first_period)[0],
            last or period_bounds(last_period)[1] - timedelta(days=1),
        )

    items = sorted(series.items())
    with ThreadPoolExecutor(32) as pool:
        spans = list(pool.map(edges, [files for _, files in items]))
    out: dict[str, Any] = {}
    for ((dataset, exchange, symbol), files), (first, last) in zip(
        items, spans, strict=True
    ):
        ordered = sorted(files)
        missing = [
            p for p in periods_between(ordered[0], ordered[-1]) if p not in files
        ]
        missing_days = sum(
            (period_bounds(p)[1] - period_bounds(p)[0]).days for p in missing
        )
        out.setdefault(dataset, {}).setdefault(exchange, {})[symbol] = {
            "first": str(first),
            "last": str(last),
            "days": (last - first).days + 1 - missing_days,
            "missing": missing,
            "bytes": sum(size for _, size in files.values()),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datasets": out,
    }


def write_coverage(buckets: dict[str, Bucket], dry_run: bool = False) -> dict[str, Any]:
    """Write each bucket's coverage file; return them merged."""
    datasets: dict[str, Any] = {}
    for group, group_datasets in GROUP_DATASETS.items():
        part = build_coverage(buckets[group], group_datasets)
        if not dry_run:
            buckets[group].put(COVERAGE_KEY, json.dumps(part, indent=1).encode())
        datasets.update(part["datasets"])
    return {"schema_version": SCHEMA_VERSION, "datasets": datasets}


def latest_day(coverage: dict[str, Any]) -> date | None:
    """The newest `last` of any series."""
    lasts = [
        s["last"]
        for exchanges in coverage["datasets"].values()
        for symbols in exchanges.values()
        for s in symbols.values()
    ]
    return date.fromisoformat(max(lasts)) if lasts else None


def summary(coverage: dict[str, Any]) -> str:
    lines = [
        "| dataset | exchange | symbols | first | last |",
        "| --- | --- | --- | --- | --- |",
    ]
    for dataset, exchanges in coverage["datasets"].items():
        for exchange, symbols in exchanges.items():
            lines.append(
                f"| {dataset} | {exchange} | {len(symbols)} "
                f"| {min(s['first'] for s in symbols.values())} "
                f"| {max(s['last'] for s in symbols.values())} |"
            )
    return "\n".join(lines)


def r2_buckets(prefix: str) -> dict[str, Bucket]:
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_DATA_KEY_ID"],
        aws_secret_access_key=os.environ["R2_DATA_KEY_SECRET"],
        region_name="auto",
        config=Config(max_pool_connections=64, retries={"mode": "adaptive"}),
    )
    return {group: R2Bucket(client, f"{prefix}-{group}") for group in GROUP_DATASETS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bucket-prefix", default=DEFAULT_BUCKET_PREFIX)
    parser.add_argument("--dry-run", action="store_true", help="don't write the files")
    parser.add_argument(
        "--max-lag-days",
        type=int,
        default=3,
        help="fail when the newest day of any series is older than this",
    )
    args = parser.parse_args()

    coverage = write_coverage(r2_buckets(args.bucket_prefix), dry_run=args.dry_run)
    newest = latest_day(coverage)
    lag = (datetime.now(UTC).date() - newest).days if newest else None
    report = (
        f"### Raw coverage\n\n{summary(coverage)}\n\n"
        f"Newest day: {newest} ({lag} days ago)"
        f"{' — dry run, nothing written' if args.dry_run else ''}\n"
    )
    print(report)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(step_summary).open("a") as out:
            out.write(report)

    if lag is None or lag > args.max_lag_days:
        print(
            f"Raw data is stale: newest day {newest}, limit {args.max_lag_days} days",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
