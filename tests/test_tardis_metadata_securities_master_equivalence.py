import calendar
import json
from datetime import datetime
from pathlib import Path

import pytest

from securities_master.database import SecuritiesMaster
from securities_master.utils import _fetch_exchange, get_symbols

_ALLOWED_TYPES = {"spot", "perpetual", "future"}


@pytest.mark.parametrize(
    "exchange_file",
    sorted(
        (Path(__file__).resolve().parents[1] / "securities_master" / "data").glob(
            "*.json"
        )
    ),
    ids=lambda p: p.stem,
)
def test_get_symbols_matches_local_securities_master_for_all_covered_exchanges(
    exchange_file: Path,
) -> None:
    exchange = exchange_file.stem
    rows = json.loads(exchange_file.read_text())
    rows = [row for row in rows if row.get("tardis_first_capture")]
    assert rows, f"no rows with tardis_first_capture in {exchange_file}"

    live_symbols = [
        s
        for s in _fetch_exchange(exchange).get("availableSymbols", [])
        if s.get("type") in _ALLOWED_TYPES
    ]
    local_start_dates = {
        row["id"]: datetime.fromisoformat(
            row["tardis_first_capture"].replace("Z", "+00:00")
        ).replace(tzinfo=None)
        for row in rows
    }
    tardis_start_dates = {
        symbol["id"]: datetime.fromisoformat(
            symbol["availableSince"].replace("Z", "+00:00")
        ).replace(tzinfo=None)
        for symbol in live_symbols
        if symbol.get("availableSince")
    }
    assert sorted(tardis_start_dates.items(), key=lambda x: x[0]) == sorted(
        local_start_dates.items(), key=lambda x: x[0]
    ), (
        f"tardis_first_capture mismatch between tardis metadata and local securities master for {exchange}"
    )
    local_end_dates = {
        row["id"]: datetime.fromisoformat(
            row["end_date"].replace("Z", "+00:00")
        ).replace(tzinfo=None)
        for row in rows
        if row.get("end_date")
    }
    tardis_end_dates = {
        symbol["id"]: datetime.fromisoformat(
            symbol["availableTo"].replace("Z", "+00:00")
        ).replace(tzinfo=None)
        for symbol in live_symbols
        if symbol.get("availableTo")
    }
    assert sorted(tardis_end_dates.items(), key=lambda x: x[0]) == sorted(
        local_end_dates.items(), key=lambda x: x[0]
    ), (
        f"end_date mismatch between tardis metadata and local securities master for {exchange}"
    )

    sm = SecuritiesMaster.load(exchanges=[exchange])
    starts = [
        datetime.fromisoformat(r["tardis_first_capture"].replace("Z", "+00:00")) for r in rows
    ]
    ends = [
        datetime.fromisoformat(r["end_date"].replace("Z", "+00:00"))
        for r in rows
        if r.get("end_date")
    ]
    min_start = min(starts).replace(tzinfo=None)
    max_end = (max(ends) if ends else max(starts)).replace(tzinfo=None)
    midpoint = min_start + (max_end - min_start) / 2

    month_start = datetime(midpoint.year, midpoint.month, 1)
    month_end = datetime(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    )
    tardis_symbols = get_symbols(
        exchange=exchange,
        from_date=month_start.strftime("%Y-%m-%d"),
        to_date=month_end.strftime("%Y-%m-%d"),
    )
    local_symbols = sm.symbol_ids(
        exchange=exchange,
        tardis_first_capture=month_start,
        end_date=month_end,
    )
    assert sorted(tardis_symbols) == sorted(local_symbols), (
        f"symbol mismatch for exchange={exchange} "
        f"window={month_start:%Y-%m-%d}..{month_end:%Y-%m-%d}"
    )
