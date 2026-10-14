from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from atlas.exchange_definitions import is_beta_exchange
from atlas.exchange_definitions.common import utc_timestamp
from atlas.exchanges import get_allowed_types, get_symbol_filter
from atlas.parsers import SkipSymbol, parse_contract
from atlas.update_sources import (
    ExchangeApiSymbolSource,
    HybridSymbolSource,
    SymbolSource,
    TardisSymbolSource,
)

_DATA_DIR = Path(__file__).parent / "data"

# A complete snapshot that newly loses more than this share of open rows (and at
# least the minimum count) is treated as a partial API response, not delistings.
MAX_NEWLY_MISSING_FRACTION = 0.1
MIN_NEWLY_MISSING_GUARD = 5

# A missing row is only confirmed delisted by a run at least this long after the
# first miss, so quick manual reruns during an API blip can't close rows.
CONFIRM_MISSING_AFTER = timedelta(hours=12)

# These fields are maintained locally and must not be replaced or removed by
# exchange or Tardis payloads during a snapshot refresh.
LOCALLY_OWNED_FIELDS = frozenset({"cmc_id", "contract_size_history"})

EXCHANGES = [
    "binance-futures",
    "binance-futures-cm",
    "binance-spot",
    "bitfinex-derivatives",
    "bitfinex",
    "bitget-futures",
    "bitget",
    "bitmex",
    "bitstamp",
    "bybit-spot",
    "bybit-perps",
    "bybit-futures",
    "coinbase",
    "crypto-com",
    "cryptofacilities",
    "deribit",
    "ftx",
    "gate-io-futures",
    "gate-io",
    "gemini",
    "huobi-dm-linear-swap",
    "huobi-dm-swap",
    "huobi-dm",
    "huobi",
    "hyperliquid-spot",
    "hyperliquid-perps",
    "kraken",
    "kucoin",
    "okx-perps",
    "okx-futures",
    "okx-spot",
    "phemex",
    "poloniex",
    "upbit",
]


def _append_contract_size_change(sd: dict, new_size: float, effective_from: str) -> dict:
    history = list(sd.get("contract_size_history") or [])
    if not history or history[-1]["value"] != new_size:
        history.append({"effective_from": effective_from, "value": new_size})
    result = {k: v for k, v in sd.items() if k != "contract_size_history"}
    if len(history) > 1:
        result["contract_size_history"] = history
    return result


def _load_existing_symbols(path: Path) -> tuple[list[dict], dict[str, dict]]:
    if not path.exists():
        return [], {}
    try:
        rows = json.loads(path.read_text())
    except json.JSONDecodeError:
        return [], {}
    rows = [row for row in rows if isinstance(row, dict) and "id" in row]
    return rows, {row["id"]: row for row in rows}


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


def _merge_existing_fields(
    symbols: list[dict], existing_by_id: dict[str, dict], ignore_metadata: bool = False
) -> list[dict]:
    """
    Keep existing metadata for symbols when the current source does not provide it.
    Source payload values take precedence except for locally owned fields.
    """
    metadata_keys = {"first_capture", "end_date"}
    merged_symbols: list[dict] = []
    for sd in symbols:
        existing = existing_by_id.get(sd.get("id"))
        existing_values = None if existing is None else {
            key: value
            for key, value in existing.items()
            if not (ignore_metadata and key in metadata_keys)
        }
        merged_symbols.append(_merge_symbol(existing_values, sd))
    return merged_symbols


def _capture_first_seen(symbols: list[dict], captured_at: str) -> None:
    """Stamp listed rows that have no start date with the time we first saw them."""
    for symbol in symbols:
        if not symbol.get("first_capture") and not symbol.get("end_date"):
            symbol["first_capture"] = captured_at


class SuspiciousSnapshotError(ValueError):
    """A complete-universe snapshot looks partial, so delistings can't be trusted."""


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _delivery_end_date(row: dict, now: datetime) -> str | None:
    """End date for a dated future whose delivery has passed, else None.

    Date-only delivery dates cover the whole delivery day, matching the
    exclusive day bound Tardis used for the rows already on file.
    """
    raw = row.get("delivery_date")
    if not raw:
        return None
    delivery = _parse_timestamp(raw)
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
        raise SuspiciousSnapshotError("source returned no symbols")
    newly_missing = [
        row["id"]
        for row in open_rows
        if row["id"] not in incoming_ids and _delivery_end_date(row, now) is None
    ]
    limit = max(MIN_NEWLY_MISSING_GUARD, MAX_NEWLY_MISSING_FRACTION * len(open_rows))
    if len(newly_missing) > limit:
        raise SuspiciousSnapshotError(
            f"{len(newly_missing)} of {len(open_rows)} open symbols vanished at once "
            f"(limit {int(limit)}), e.g. {', '.join(sorted(newly_missing)[:5])}"
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
        elif now - _parse_timestamp(row["missing_since"]) >= CONFIRM_MISSING_AFTER:
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


def _apply_snapshot_metadata(symbols: list[dict]) -> list[dict]:
    mapped_symbols: list[dict] = []
    for sd in symbols:
        mapped = {
            key: value
            for key, value in sd.items()
            if key not in {"availableSince", "availableTo"}
        }
        if "availableSince" in sd:
            mapped["first_capture"] = sd["availableSince"]
        if "availableTo" in sd:
            mapped["end_date"] = sd["availableTo"]
        mapped_symbols.append(mapped)
    return mapped_symbols


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


def _fetch_incoming_symbols(
    source: SymbolSource, exchange: str, allowed_types: set[str], symbol_filter
) -> list[dict]:
    data = source.fetch_exchange(exchange)
    return [
        sd.copy()
        for sd in data.get("availableSymbols", [])
        if _matches_exchange_constraints(sd, allowed_types, symbol_filter)
    ]


def _combine_with_existing_rows(
    source: SymbolSource,
    incoming_symbols: list[dict],
    existing_rows: list[dict],
    now: datetime,
    captured_at: str,
) -> list[dict]:
    """Add back existing rows the source omitted; Tardis output replaces them."""
    if isinstance(source, TardisSymbolSource):
        return incoming_symbols
    if getattr(source, "complete_universe", False):
        incoming_ids = {sd["id"] for sd in incoming_symbols}
        existing_rows = _close_missing_rows(existing_rows, incoming_ids, now, captured_at)
    return _merge_missing_rows(incoming_symbols, existing_rows)


def update(
    exchanges: list[str] = EXCHANGES,
    source: SymbolSource | None = None,
) -> list[str]:
    if source is None:
        source = ExchangeApiSymbolSource()
    complete_universe = getattr(source, "complete_universe", False)
    _DATA_DIR.mkdir(exist_ok=True)
    total = 0
    failed_exchanges: list[str] = []
    for exchange in exchanges:
        path = _DATA_DIR / f"{exchange}.json"
        existing_rows, existing_by_id = _load_existing_symbols(path)
        if is_beta_exchange(exchange):
            print(f"Skipping {exchange} (beta exchange)", flush=True)
            continue
        print(f"Fetching {exchange}...", flush=True)
        now = datetime.now(UTC)
        captured_at = utc_timestamp(now)
        allowed_types = get_allowed_types(exchange) or {"spot", "perpetual", "future"}
        symbol_filter = get_symbol_filter(exchange)
        try:
            incoming_symbols = _fetch_incoming_symbols(
                source, exchange, allowed_types, symbol_filter
            )
            if complete_universe:
                _check_snapshot_plausible(incoming_symbols, existing_rows, now)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            failed_exchanges.append(exchange)
            continue

        is_tardis_data = any("availableSince" in sd for sd in incoming_symbols)

        incoming_symbols = _normalize_exchange_symbols(exchange, incoming_symbols)
        incoming_symbols = _apply_snapshot_metadata(incoming_symbols)
        if complete_universe:
            incoming_symbols = _resolve_reported_lifecycle(incoming_symbols, existing_by_id)
        if exchange in {"hyperliquid-perps", "hyperliquid-spot"}:
            # Hybrid metadata is partial: clear lifecycle fields only on rows
            # that actually supplied Tardis availability bounds. API-only rows
            # must retain their captured instance and approved CMC mapping.
            incoming_symbols = [
                _merge_existing_fields([sd], existing_by_id, ignore_metadata=is_tardis_data and bool(sd.get("first_capture")))[0]
                for sd in incoming_symbols
            ]
        else:
            incoming_symbols = _merge_existing_fields(
                incoming_symbols, existing_by_id, ignore_metadata=is_tardis_data
            )
        if complete_universe:
            _capture_first_seen(incoming_symbols, captured_at)
        else:
            _capture_first_seen(
                [sd for sd in incoming_symbols if sd.get("id") not in existing_by_id],
                captured_at,
            )
        incoming_symbols = [_enrich(exchange, sd) for sd in incoming_symbols]

        symbols = _combine_with_existing_rows(
            source, incoming_symbols, existing_rows, now, captured_at
        )
        # Existing rows are retained for historical coverage, but only if they
        # belong to this virtual exchange's product universe. This prevents
        # shared Tardis feeds from leaking spot/futures into Bybit perpetuals.
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
        symbols = _drop_none_fields(symbols)
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


def parse_args(argv: list[str] | None = None):
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Update securities master from exchange APIs (Binance, Bybit, "
            "Hyperliquid, OKX), detecting delistings from the live listings"
        )
    )
    parser.add_argument(
        "--exchanges",
        type=str,
        default="",
        help="Comma-separated list of exchanges (default: all)",
    )
    parser.add_argument(
        "--source",
        choices=["hybrid", "tardis", "exchange"],
        default="exchange",
        help=(
            "Data source mode: exchange (default, direct exchange APIs only), "
            "hybrid (exchange APIs overlaid on Tardis), tardis (all via Tardis)"
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    load_dotenv()
    args = parse_args()

    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()] or EXCHANGES
    source: SymbolSource
    if args.source == "tardis":
        source = TardisSymbolSource()
    elif args.source == "exchange":
        source = ExchangeApiSymbolSource()
    else:
        source = HybridSymbolSource()

    failed_exchanges = update(exchanges, source=source)
    if failed_exchanges:
        sys.exit(1)
