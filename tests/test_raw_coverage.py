import io
import json
from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.parquet as pq

from integrations.raw_coverage import (
    build_all,
    build_coverage,
    footer_days,
    latest_day,
    parse_object_key,
    periods_between,
    write_coverage,
)


class MemoryBucket:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})

    def list(self, prefix: str) -> list[tuple[str, int]]:
        return [
            (key, len(body))
            for key, body in sorted(self.objects.items())
            if key.startswith(prefix)
        ]

    def get_range(self, key: str, start: int, end: int) -> bytes:
        return self.objects[key][start:end]


def parquet(*days: date) -> bytes:
    stamps = [datetime(d.year, d.month, d.day, 12, tzinfo=UTC) for d in days]
    table = pa.table(
        {"exchange_timestamp": pa.array(stamps, pa.timestamp("us", tz="UTC"))}
    )
    out = io.BytesIO()
    pq.write_table(table, out)
    return out.getvalue()


def key(dataset: str, exchange: str, symbol: str, period: str) -> str:
    base = f"v1/{dataset}/exchange={exchange}/symbol={symbol}/year={period[:4]}/month={period[5:7]}"
    if len(period) == 7:
        return f"{base}/data.parquet"
    return f"{base}/day={period[8:10]}/data.parquet"


BTC = "perpetual-BTC-USDT:USDT"


def test_parse_object_key_reads_monthly_and_daily_files():
    assert parse_object_key(key("trades", "binance-futures", BTC, "2024-03")) == (
        "trades",
        "binance-futures",
        BTC,
        "2024-03",
    )
    assert parse_object_key(key("quotes", "okx-perps", BTC, "2026-09-02"))[3] == (
        "2026-09-02"
    )
    assert parse_object_key("v1/_coverage.json") is None
    assert parse_object_key("_build/run/plan.jsonl") is None
    assert parse_object_key(key("trades", "okx-perps", BTC, "2024-13")) is None


def test_periods_between_switches_from_months_to_days_in_august_2026():
    assert periods_between("2026-06", "2026-08-02") == [
        "2026-06",
        "2026-07",
        "2026-08-01",
        "2026-08-02",
    ]


def test_footer_days_reads_the_exchange_time_span():
    body = parquet(date(2024, 3, 9), date(2024, 3, 21))
    bucket = MemoryBucket({"f": body})

    assert footer_days(bucket, "f", len(body)) == (date(2024, 3, 9), date(2024, 3, 21))


def test_build_coverage_uses_footers_for_monthly_edges_and_lists_gaps():
    bucket = MemoryBucket(
        {
            key("trades", "binance-futures", BTC, "2026-05"): parquet(
                date(2026, 5, 17), date(2026, 5, 31)
            ),
            key("trades", "binance-futures", BTC, "2026-07"): parquet(
                date(2026, 7, 1), date(2026, 7, 31)
            ),
            key("trades", "binance-futures", BTC, "2026-08-01"): b"x" * 10,
            key("trades", "binance-futures", BTC, "2026-08-03"): b"x" * 10,
        }
    )

    series = build_coverage(bucket, ("trades",))["datasets"]["trades"][
        "binance-futures"
    ][BTC]

    assert series["first"] == "2026-05-17"
    assert series["last"] == "2026-08-03"
    assert series["missing"] == ["2026-06", "2026-08-02"]
    # 2026-05-17..2026-08-03 is 79 days, less June (30) and 2026-08-02 (1).
    assert series["days"] == 48
    assert series["bytes"] == sum(len(body) for body in bucket.objects.values())


def test_build_coverage_ignores_other_datasets_and_venues():
    bucket = MemoryBucket(
        {
            key("trades", "binance-futures", BTC, "2026-09-01"): b"x",
            key("quotes", "binance-futures", BTC, "2026-09-01"): b"x",
            key("trades", "bybit-perps", BTC, "2026-09-01"): b"x",
        }
    )

    coverage = build_coverage(bucket, ("trades",))

    assert list(coverage["datasets"]) == ["trades"]
    assert list(coverage["datasets"]["trades"]) == ["binance-futures"]


def test_build_all_merges_the_three_buckets_and_writes_one_file(tmp_path):
    buckets = {
        "trades": MemoryBucket({key("trades", "okx-perps", BTC, "2026-09-01"): b"x"}),
        "quotes": MemoryBucket({key("quotes", "okx-perps", BTC, "2026-09-02"): b"x"}),
        "derivatives": MemoryBucket(
            {key("funding_rate", "okx-perps", BTC, "2026-09-03"): b"x"}
        ),
    }

    coverage = build_all(buckets)
    path = tmp_path / "raw" / "coverage.json"
    write_coverage(coverage, path)

    written = json.loads(path.read_text())
    assert written["schema_version"] == 1
    assert written["generated_at"].endswith("Z")
    assert set(written["datasets"]) == {"trades", "quotes", "funding_rate"}
    assert latest_day(written) == date(2026, 9, 3)


def test_latest_day_is_none_without_series():
    assert latest_day({"datasets": {}}) is None
