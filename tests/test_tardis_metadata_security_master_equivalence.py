import calendar
import json
from datetime import datetime
from pathlib import Path

import pytest

from security_master.database import SecuritiesMaster
from security_master.exchange_ids import to_tardis_exchange_id
from security_master.utils import _fetch_exchange

_LEGACY_TO_INTERNAL_EXCHANGE = {
    "binance": "binance-spot",
    "binance-futures": "binance-futures",
    "okex": "okx-spot",
    "okex-futures": "okx-futures",
    "okex-swap": "okx-perps",
}


def _internal_exchange_for_data_file(exchange_file_stem: str) -> str:
    return _LEGACY_TO_INTERNAL_EXCHANGE.get(exchange_file_stem, exchange_file_stem)


    def _row_first_capture(row: dict) -> str | None:
        return row.get("first_capture")


def _is_active_in_window(symbol: dict, start: datetime, end: datetime) -> bool:
    available_since = datetime.fromisoformat(
        symbol["availableSince"].replace("Z", "+00:00")
    ).replace(tzinfo=None)
    available_to_raw = symbol.get("availableTo")
    available_to = (
        datetime.fromisoformat(available_to_raw.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
        if available_to_raw
        else None
    )
    return available_since <= start and (available_to is None or available_to >= end)


@pytest.mark.parametrize(
    "exchange_file",
    sorted(
        (Path(__file__).resolve().parents[1] / "security_master" / "data").glob(
            "*.json"
        )
    ),
    ids=lambda p: p.stem,
)
def test_get_symbols_matches_local_security_master_for_all_covered_exchanges(
    exchange_file: Path,
) -> None:
    exchange = _internal_exchange_for_data_file(exchange_file.stem)
    tardis_exchange = to_tardis_exchange_id(exchange)
    rows = json.loads(exchange_file.read_text())
    rows = [row for row in rows if _row_first_capture(row)]
    if not rows:
        pytest.skip(
            "snapshot has no first_capture metadata "
            f"(likely generated from direct exchange APIs): {exchange_file}"
        )

    live_symbols = [
        s
        for s in _fetch_exchange(exchange).get("availableSymbols", [])
        if s.get("type") in {"spot", "perpetual", "future"}
    ]
    local_start_dates = {
        row["id"]: datetime.fromisoformat(_row_first_capture(row).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
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
        "first_capture mismatch between tardis metadata and local "
        f"securities master for {exchange} (tardis id: {tardis_exchange})"
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
        "end_date mismatch between tardis metadata and local securities master "
        f"for {exchange} (tardis id: {tardis_exchange})"
    )

    sm = SecuritiesMaster.load(exchanges=[exchange])
    starts = [
        datetime.fromisoformat(_row_first_capture(r).replace("Z", "+00:00")) for r in rows
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
    tardis_symbols = [
        symbol["id"] for symbol in live_symbols if _is_active_in_window(symbol, month_start, month_end)
    ]
    local_symbols = sm.symbol_ids(
        exchange=exchange,
        first_capture=month_start,
        end_date=month_end,
    )
    assert sorted(tardis_symbols) == sorted(local_symbols), (
        f"symbol mismatch for exchange={exchange} "
        f"window={month_start:%Y-%m-%d}..{month_end:%Y-%m-%d}"
    )
