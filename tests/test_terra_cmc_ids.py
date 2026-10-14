"""Pin the Terra ticker split in the bundled snapshots.

After the May 2022 collapse the original chain was renamed Terra Classic (LUNC,
CMC 4172) and a new chain took the ``LUNA`` ticker as CMC 20314, which CMC added
on 2022-05-26. A ``LUNA`` instrument that stopped trading before then can only
have traded Terra Classic.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from integrations.cmc_mappings import InstrumentInstance, MappingStore


DATA_DIR = Path(__file__).resolve().parents[1] / "atlas" / "data"
TERRA_CLASSIC = 4172
TERRA_V2_ADDED = datetime(2022, 5, 26, tzinfo=UTC)
EXCHANGES = ("binance-futures", "binance-futures-cm", "binance-spot")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _pre_split_luna_rows() -> list[tuple[str, dict]]:
    rows = []
    for exchange in EXCHANGES:
        for row in json.loads((DATA_DIR / f"{exchange}.json").read_text()):
            end_date = row.get("end_date")
            if (
                row.get("symbol") == "LUNA"
                and end_date
                and _parse(end_date) < TERRA_V2_ADDED
            ):
                rows.append((exchange, row))
    return rows


PRE_SPLIT_LUNA_ROWS = _pre_split_luna_rows()


def test_the_snapshots_still_hold_pre_split_luna_instruments():
    ids = {(exchange, row["id"]) for exchange, row in PRE_SPLIT_LUNA_ROWS}

    assert ("binance-futures", "lunausdt") in ids
    assert ("binance-futures-cm", "lunausd_perp") in ids
    assert ("binance-spot", "lunabtc") in ids


@pytest.mark.parametrize(
    ("exchange", "row"),
    PRE_SPLIT_LUNA_ROWS,
    ids=[f"{exchange}:{row['id']}" for exchange, row in PRE_SPLIT_LUNA_ROWS],
)
def test_pre_split_luna_instruments_map_to_terra_classic(exchange, row):
    assert row.get("cmc_id") == TERRA_CLASSIC

    mapping = MappingStore.load(DATA_DIR / "cmc_mappings.json").get(
        InstrumentInstance(exchange, row["id"], _parse(row["first_capture"]))
    )
    assert mapping is not None
    assert (mapping.cmc_id, mapping.status) == (TERRA_CLASSIC, "approved")
