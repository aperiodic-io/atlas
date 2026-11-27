from __future__ import annotations

import json
import re
import sys
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from atlas.exchange_definitions.common import parse_utc_timestamp, utc_timestamp
from atlas.exchanges import get_allowed_types, get_symbol_filter
from atlas.parsers import SkipSymbol, parse_contract
from atlas.update_sources import (
    ExchangeApiSymbolSource,
    SymbolLifecycle,
    SymbolSource,
    source_lifecycle,
)

_DATA_DIR = Path(__file__).parent / "data"

# A complete snapshot that newly loses more than this share of open rows (and at
# least the minimum count) is treated as a partial API response, not delistings,
# unless the run is told to accept the losses for that exchange.
MAX_NEWLY_MISSING_FRACTION = 0.1
MIN_NEWLY_MISSING_GUARD = 5

# A missing row is only confirmed delisted by a run at least this long after the
# first miss, so quick manual reruns during an API blip can't close rows.
CONFIRM_MISSING_AFTER = timedelta(hours=12)

# These fields are maintained locally and must not be replaced or removed by
# exchange payloads during a snapshot refresh. The symbology fields
# are written by ``integrations/symbology.py``.
LOCALLY_OWNED_FIELDS = frozenset(
    {
        "cmc_id",
        "category",
        "contract_size_history",
        "base_asset_id",
        "quote_asset_id",
        "margin_asset_id",
        "base_multiplier",
        "base_asset_match",
        "base_asset_history",
        "unified_id",
    }
)

EXCHANGES = [
    "aster-perps",
    "apex-perps",
    "binance-futures",
    "binance-futures-cm",
    "binance-spot",
    "bybit-spot",
    "bybit-perps-usd",
    "bybit-perps-coin",
    "bybit-futures",
    "grvt-perps",
    "hyperliquid-spot",
    "hyperliquid-perps",
    "lighter-perps",
    "okx-perps",
    "okx-futures",
    "okx-spot",
    "extended-perps",
    "pacifica-perps",
    "gateio-spot",
    "gateio-perps-usdt",
    "gateio-futures-usdt",
    "gateio-perps-btc",
    "htx-perps-usdt",
    "mexc-perps",
    "bitget-perps-usdt",
    "bitget-perps-usdc",
    "bitget-perps-coin",
    "bitget-spot",
    "coinbase-spot",
    "bitget-futures-coin",
    "deribit-perps",
    "deribit-futures",
    "whitebit-perps",
    "cryptocom-perps",
    "cryptocom-futures",
    "avantis-perps",
    "variational-perps",
    "bitfinex-spot",
    "bitfinex-perps",
    "bitstamp-spot",
    "bitstamp-perps",
    "cryptocom-spot",
    "gemini-spot",
    "gemini-perps",
    "htx-spot",
    "htx-perps-coin",
    "htx-futures-coin",
    "htx-futures-usdt",
    "kraken-spot",
    "kraken-perps",
    "kraken-futures",
    "kucoin-spot",
    "phemex-spot",
    "phemex-perps",
    "poloniex-spot",
    "upbit-spot",
]



def _append_contract_size_change(sd: dict, new_size: float, effective_from: str) -> dict:
    history = list(sd.get("contract_size_history") or [])
    if not history or history[-1]["value"] != new_size:
        history.append({"effective_from": effective_from, "value": new_size})
    result = {k: v for k, v in sd.items() if k != "contract_size_history"}
    if len(history) > 1:
        result["contract_size_history"] = history
    return result


def _load_existing_symbols(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    return [row for row in rows if isinstance(row, dict) and "id" in row]


def _merge_symbol(
    existing: dict | None,
    incoming: dict,
    protected_fields: frozenset[str] = LOCALLY_OWNED_FIELDS,
) -> dict:
    """Merge one source record while retaining non-null locally owned fields."""
    if existing is None:
        return incoming.copy()

    merged = {**existing, **incoming}
    for key in protected_fields:
        if existing.get(key) is not None:
            merged[key] = existing[key]
    return merged


def _merge_existing_fields(symbols: list[dict], existing_by_id: dict[str, dict]) -> list[dict]:
    """
    Keep existing metadata for symbols when the current source does not provide it.
    Source payload values take precedence except for locally owned fields.
    """
    return [_merge_symbol(existing_by_id.get(sd.get("id")), sd) for sd in symbols]


def _capture_first_seen(symbols: list[dict], captured_at: str) -> None:
    """Stamp listed rows that have no start date with the time we first saw them."""
    for symbol in symbols:
        if not symbol.get("first_capture") and not symbol.get("end_date"):
            symbol["first_capture"] = captured_at


class SuspiciousSnapshotError(ValueError):
    """A complete-universe snapshot looks partial, so delistings can't be trusted."""


class EmptySnapshotError(ValueError):
    """The source returned nothing for an exchange that has open rows on file."""


def _delivery_end_date(row: dict, now: datetime) -> str | None:
    """End date for a dated future whose delivery has passed, else None.

    Date-only delivery dates cover the whole delivery day, matching the
    exclusive day bound the historical rows on file use.
    """
    raw = row.get("delivery_date")
    if not raw:
        return None
    delivery = parse_utc_timestamp(raw)
    if delivery.time() == datetime.min.time():
        delivery += timedelta(days=1)
    return utc_timestamp(delivery) if delivery <= now else None


def _is_open(row: dict) -> bool:
    return not row.get("end_date") and not row.get("missing_since")


def _check_snapshot_plausible(
    incoming_symbols: list[dict], existing_rows: list[dict], now: datetime
) -> None:
    """Refuse snapshots that would newly drop implausibly many open rows."""
    incoming_ids = {sd.get("id") for sd in incoming_symbols}
    open_rows = [row for row in existing_rows if _is_open(row)]
    if open_rows and not incoming_symbols:
        raise EmptySnapshotError("source returned no symbols")
    newly_missing = [
        row["id"]
        for row in open_rows
        if row["id"] not in incoming_ids and _delivery_end_date(row, now) is None
    ]
    limit = max(MIN_NEWLY_MISSING_GUARD, MAX_NEWLY_MISSING_FRACTION * len(open_rows))
    if len(newly_missing) > limit:
        raise SuspiciousSnapshotError(
            f"{len(newly_missing)} of {len(open_rows)} open symbols vanished at once "
            f"(limit {int(limit)}), e.g. {', '.join(sorted(newly_missing)[:5])}; "
            "none were marked missing. If the exchange really dropped them, rerun "
            "with --accept-missing for this exchange"
        )


def _resolve_reported_lifecycle(
    incoming_symbols: list[dict], existing_by_id: dict[str, dict]
) -> list[dict]:
    """Apply what the exchange says about each symbol it returned.

    A row the exchange reports as delisted (it carries `end_date`) only closes
    the matching existing row: it never overwrites an end date or other fields
    already on file, and never adds an instrument we did not track while it was
    live. Every other returned row is listed, so any stale end date or pending
    miss is cleared. An exchange listing time (`first_capture`) only fills a
    missing start date.
    """
    resolved: list[dict] = []
    for sd in incoming_symbols:
        existing = existing_by_id.get(sd.get("id"))
        if sd.get("end_date"):
            if existing is None:
                continue
            resolved.append({
                **existing,
                "end_date": existing.get("end_date") or sd["end_date"],
                "missing_since": None,
            })
            continue
        listed = {**sd, "end_date": None, "missing_since": None}
        if existing is not None and existing.get("first_capture"):
            listed["first_capture"] = existing["first_capture"]
        resolved.append(listed)
    return resolved


def _close_missing_rows(
    existing_rows: list[dict], incoming_ids: set[str], now: datetime, captured_at: str
) -> list[dict]:
    """Track existing rows that a complete snapshot no longer lists.

    Expired futures end at delivery. Anything else is first marked
    `missing_since`; if a run at least CONFIRM_MISSING_AFTER later still misses
    it, it ends at that first miss. A row that comes back before then is simply
    listed again.
    """
    rows: list[dict] = []
    for row in existing_rows:
        if row.get("id") in incoming_ids or row.get("end_date"):
            rows.append(row)
        elif end_date := _delivery_end_date(row, now):
            rows.append({**row, "end_date": end_date, "missing_since": None})
        elif not row.get("missing_since"):
            rows.append({**row, "missing_since": captured_at})
        elif now - parse_utc_timestamp(row["missing_since"]) >= CONFIRM_MISSING_AFTER:
            rows.append({**row, "end_date": row["missing_since"], "missing_since": None})
        else:
            rows.append(row)
    return rows


def _enrich(exchange: str, sd: dict) -> dict:
    """Return a symbol dict enriched with pre-computed Contract fields."""
    _NONE = {
        "internal_id": None,
        "symbol": None,
        "denominator": None,
        "margin": None,
        "contract_size": None,
        "quantity_unit": None,
        "delivery_date": None,
    }
    try:
        c = parse_contract(exchange, sd)
        existing_contract_size = sd.get("contract_size")
        if c.contract_size is not None:
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            sd = _append_contract_size_change(sd, new_size=c.contract_size, effective_from=now)
        history = sd.get("contract_size_history")
        contract_size = c.contract_size if c.contract_size is not None else (
            history[-1]["value"] if history else existing_contract_size
        )
        return {
            **sd,
            "internal_id": c.internal_id,
            "symbol": c.symbol,
            "denominator": c.denominator,
            "margin": c.margin,
            "contract_size": contract_size,
            "quantity_unit": c.quantity_unit,
            "delivery_date": c.delivery_date.isoformat() if c.delivery_date else None,
        }
    except SkipSymbol:
        return {**sd, **_NONE}


def _normalize_binance_derivative_type(
    symbol_id: str, current_type: str | None
) -> str | None:
    sid = symbol_id.upper()
    if "_PERP" in sid or sid.endswith("PERP"):
        return "perpetual"
    if re.search(r"_\d{6}$", sid) or re.search(r"\d{6}$", sid):
        return "future"
    if current_type in {None, "", "future", "perpetual"}:
        # On Binance futures endpoints, a non-dated symbol is perpetual.
        return "perpetual"
    return current_type


def _normalize_exchange_symbols(exchange: str, symbols: list[dict]) -> list[dict]:
    if exchange not in {"binance-futures", "binance-futures-cm"}:
        return [sd.copy() for sd in symbols]
    normalized_symbols: list[dict] = []
    for sd in symbols:
        sid = sd.get("id")
        if not sid:
            normalized_symbols.append(sd.copy())
            continue
        normalized_symbols.append(
            {
                **sd,
                "type": _normalize_binance_derivative_type(sid, sd.get("type")),
            }
        )
    return normalized_symbols


def _drop_none_fields(symbols: list[dict], keys: set[str] | None = None) -> list[dict]:
    filtered_symbols: list[dict] = []
    for sd in symbols:
        if keys is not None:
            filtered_symbols.append(
                {
                    key: value
                    for key, value in sd.items()
                    if key not in keys or value is not None
                }
            )
        else:
            filtered_symbols.append({key: value for key, value in sd.items() if value is not None})
    return filtered_symbols


def _merge_missing_rows(incoming_symbols: list[dict], existing_rows: list[dict]) -> list[dict]:
    symbols_by_id: dict[str, dict] = {
        sd["id"]: sd for sd in incoming_symbols if isinstance(sd, dict) and "id" in sd
    }
    for row in existing_rows:
        row_id = row.get("id")
        if row_id and row_id not in symbols_by_id:
            symbols_by_id[row_id] = row
    return list(symbols_by_id.values())


def _matches_exchange_constraints(
    symbol: dict,
    allowed_types: set[str],
    symbol_filter,
) -> bool:
    return symbol.get("type") in allowed_types and (
        symbol_filter(symbol) if symbol_filter else True
    )


def _fetch_incoming_symbols(source: SymbolSource, exchange: str) -> list[dict]:
    allowed_types = get_allowed_types(exchange) or {"spot", "perpetual", "future"}
    symbol_filter = get_symbol_filter(exchange)
    data = source.fetch_exchange(exchange)
    return [
        sd.copy()
        for sd in data.get("availableSymbols", [])
        if _matches_exchange_constraints(sd, allowed_types, symbol_filter)
    ]


def _combine_with_existing_rows(
    incoming_symbols: list[dict],
    existing_rows: list[dict],
    *,
    infer_missing: bool,
    now: datetime,
    captured_at: str,
) -> list[dict]:
    """Add back existing rows the source omitted, tracking misses when trusted."""
    if infer_missing:
        incoming_ids = {sd["id"] for sd in incoming_symbols}
        existing_rows = _close_missing_rows(existing_rows, incoming_ids, now, captured_at)
    return _merge_missing_rows(incoming_symbols, existing_rows)


def _clear_pending_misses(symbols: list[dict]) -> list[dict]:
    """Drop `missing_since` from rows written by a source that cannot see delistings."""
    return [{k: v for k, v in sd.items() if k != "missing_since"} for sd in symbols]


def _infers_missing(
    exchange: str,
    lifecycle: SymbolLifecycle,
    incoming_symbols: list[dict],
    existing_rows: list[dict],
    now: datetime,
    accept_missing: Collection[str],
) -> bool:
    """Whether rows this snapshot omits may be marked missing.

    An implausible snapshot raises unless the exchange's losses are accepted;
    an empty one always raises.
    """
    if lifecycle is not SymbolLifecycle.COMPLETE:
        return False
    try:
        _check_snapshot_plausible(incoming_symbols, existing_rows, now)
    except SuspiciousSnapshotError:
        if exchange not in accept_missing:
            raise
    return True


def _hold_upcoming(
    incoming_symbols: list[dict], existing_rows: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split off instruments the venue lists but that have not started trading.

    Fetchers flag them `upcoming`. One Atlas has never tracked is not added
    until it trades. One already on file (a relaunch, or a listing the venue
    re-staged) is still listed, so it is kept exactly as it is rather than
    marked missing; only a pending miss is dropped.

    Returns the trading rows, the rows on file to process, and the held rows.
    """
    upcoming_ids = {sd.get("id") for sd in incoming_symbols if sd.get("upcoming")}
    trading = [sd for sd in incoming_symbols if not sd.get("upcoming")]
    remaining = [row for row in existing_rows if row["id"] not in upcoming_ids]
    held = [
        {k: v for k, v in row.items() if k != "missing_since"}
        for row in existing_rows
        if row["id"] in upcoming_ids
    ]
    return trading, remaining, held


def _build_exchange_rows(
    exchange: str,
    lifecycle: SymbolLifecycle,
    incoming_symbols: list[dict],
    existing_rows: list[dict],
    *,
    infer_missing: bool,
    now: datetime,
) -> list[dict]:
    """Turn one exchange's fetched rows plus the rows on file into the rows to write."""
    incoming_symbols, existing_rows, held_rows = _hold_upcoming(incoming_symbols, existing_rows)
    existing_by_id = {row["id"]: row for row in existing_rows}
    complete = lifecycle is SymbolLifecycle.COMPLETE
    captured_at = utc_timestamp(now)
    allowed_types = get_allowed_types(exchange) or {"spot", "perpetual", "future"}
    symbol_filter = get_symbol_filter(exchange)

    incoming_symbols = _normalize_exchange_symbols(exchange, incoming_symbols)
    if complete:
        incoming_symbols = _resolve_reported_lifecycle(incoming_symbols, existing_by_id)
    incoming_symbols = _merge_existing_fields(incoming_symbols, existing_by_id)
    # Only a complete source can say an existing row is still listed.
    _capture_first_seen(
        [sd for sd in incoming_symbols if complete or sd.get("id") not in existing_by_id],
        captured_at,
    )
    incoming_symbols = [_enrich(exchange, sd) for sd in incoming_symbols]

    symbols = _combine_with_existing_rows(
        incoming_symbols,
        existing_rows,
        infer_missing=infer_missing,
        now=now,
        captured_at=captured_at,
    )
    symbols = [*symbols, *held_rows]
    if not complete:
        symbols = _clear_pending_misses(symbols)
    # Existing rows are retained for historical coverage, but only if they
    # belong to this virtual exchange's product universe. This prevents
    # rows of another product (spot or futures) from leaking into perpetuals.
    symbols = [
        sd
        for sd in symbols
        if _matches_exchange_constraints(sd, allowed_types, symbol_filter)
    ]
    symbols = sorted(
        symbols,
        key=lambda sd: (
            sd.get("id", ""),
            sd.get("first_capture") or "",
            sd.get("end_date") or "",
        )
    )
    return _drop_none_fields(symbols)


def update(
    exchanges: list[str] = EXCHANGES,
    source: SymbolSource | None = None,
    accept_missing: Collection[str] = (),
) -> list[str]:
    """Refresh each exchange's file and return the exchanges that failed.

    A suspicious complete snapshot still updates the rows it returned but
    marks nothing missing, and the exchange is reported as failed.
    """
    if source is None:
        source = ExchangeApiSymbolSource()
    lifecycle = source_lifecycle(source)
    _DATA_DIR.mkdir(exist_ok=True)
    total = 0
    failed_exchanges: list[str] = []
    for exchange in exchanges:
        path = _DATA_DIR / f"{exchange}.json"
        existing_rows = _load_existing_symbols(path)
        print(f"Fetching {exchange}...", flush=True)
        now = datetime.now(UTC)
        try:
            incoming_symbols = _fetch_incoming_symbols(source, exchange)
            infer_missing = _infers_missing(
                exchange, lifecycle, incoming_symbols, existing_rows, now, accept_missing
            )
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            failed_exchanges.append(exchange)
            if not isinstance(e, SuspiciousSnapshotError):
                continue
            # A suspicious but non-empty snapshot still updates what it returned.
            infer_missing = False

        symbols = _build_exchange_rows(
            exchange,
            lifecycle,
            incoming_symbols,
            existing_rows,
            infer_missing=infer_missing,
            now=now,
        )
        path.write_text(json.dumps(symbols, indent=2))
        total += len(symbols)
        print(f"  {len(symbols)} symbols written to {path.name}")

    print(f"\nTotal: {total} symbols across {len(exchanges)} exchanges")
    if failed_exchanges:
        print(
            f"\nFailed to fetch {len(failed_exchanges)} exchange(s): "
            f"{', '.join(failed_exchanges)}",
            file=sys.stderr,
        )
    return failed_exchanges


def _exchange_list(value: str) -> list[str]:
    return [e.strip() for e in value.split(",") if e.strip()]


def select_exchanges(include: str, exclude: str) -> list[str]:
    """The exchanges a run refreshes: `include` (default all) minus `exclude`."""
    excluded = _exchange_list(exclude)
    unknown = [e for e in excluded if e not in EXCHANGES]
    if unknown:
        raise ValueError(f"Cannot exclude unknown exchange(s): {', '.join(unknown)}")
    return [e for e in _exchange_list(include) or EXCHANGES if e not in excluded]


def parse_args(argv: list[str] | None = None):
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Update securities master from exchange APIs, detecting delistings "
            "from the live listings"
        )
    )
    parser.add_argument(
        "--exchanges",
        type=str,
        default="",
        help="Comma-separated list of exchanges (default: all)",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default="",
        help="Comma-separated exchanges to skip (for runners that cannot reach them)",
    )
    parser.add_argument(
        "--accept-missing",
        type=_exchange_list,
        default=[],
        help=(
            "Comma-separated exchanges whose many vanished symbols are real "
            "delistings, overriding the partial-snapshot guard for this run"
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    load_dotenv()
    args = parse_args()

    exchanges = select_exchanges(args.exchanges, args.exclude)
    failed_exchanges = update(exchanges, accept_missing=args.accept_missing)
    if failed_exchanges:
        sys.exit(1)
