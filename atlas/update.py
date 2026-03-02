from __future__ import annotations

import json
import re
import sys
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.exchange_definitions import is_beta_exchange
from atlas.parsers import SkipSymbol, parse_contract
from atlas.update_sources import (
    ExchangeApiSymbolSource,
    HybridSymbolSource,
    SymbolSource,
    TardisSymbolSource,
)
from dotenv import load_dotenv

_DATA_DIR = Path(__file__).parent / "data"

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


def _load_existing_symbols(path: Path) -> tuple[list[dict], dict[str, dict]]:
    if not path.exists():
        return [], {}
    try:
        rows = json.loads(path.read_text())
    except json.JSONDecodeError:
        return [], {}
    rows = [row for row in rows if isinstance(row, dict) and "id" in row]
    return rows, {row["id"]: row for row in rows}


def _merge_existing_fields(symbols: list[dict], existing_by_id: dict[str, dict]) -> None:
    """
    Keep existing metadata for symbols when the current source does not provide it.
    Source payload values take precedence; existing values fill only missing keys.
    """
    for sd in symbols:
        existing = existing_by_id.get(sd.get("id"))
        if existing is None:
            continue
        for key, value in existing.items():
            sd.setdefault(key, value)


def _enrich(exchange: str, sd: dict) -> None:
    """Add pre-computed Contract fields to a symbol dict in-place."""
    _NONE = {
        "internal_id": None,
        "symbol": None,
        "denominator": None,
        "margin": None,
        "contract_type": None,
        "delivery_date": None,
        "contract_size": None,
    }
    try:
        c = parse_contract(exchange, sd)
        sd["internal_id"] = c.internal_id
        sd["symbol"] = c.symbol
        sd["denominator"] = c.denominator
        sd["margin"] = c.margin
        sd["contract_type"] = c.contract_type.value
        sd["delivery_date"] = c.delivery_date.isoformat() if c.delivery_date else None
        sd["contract_size"] = c.contract_size
    except SkipSymbol:
        sd.update(_NONE)


def _normalize_binance_derivative_type(symbol_id: str, current_type: str | None) -> str | None:
    sid = symbol_id.upper()
    if "_PERP" in sid or sid.endswith("PERP"):
        return "perpetual"
    if re.search(r"_\d{6}$", sid) or re.search(r"\d{6}$", sid):
        return "future"
    if current_type in {None, "", "future", "perpetual"}:
        # On Binance futures endpoints, a non-dated symbol is perpetual.
        return "perpetual"
    return current_type


def _normalize_exchange_symbols(exchange: str, symbols: list[dict]) -> None:
    if exchange not in {"binance-futures", "binance-futures-cm"}:
        return
    for sd in symbols:
        sid = sd.get("id")
        if not sid:
            continue
        sd["type"] = _normalize_binance_derivative_type(sid, sd.get("type"))


def _apply_snapshot_metadata(symbols: list[dict]) -> None:
    for sd in symbols:
        if "availableSince" in sd:
            sd["first_capture"] = sd["availableSince"]
        # Persist the normalized project field name, not raw Tardis key.
        sd.pop("availableSince", None)
        if "availableTo" in sd:
            sd["end_date"] = sd["availableTo"]


def _merge_missing_rows(
    incoming_symbols: list[dict], existing_rows: list[dict]
) -> list[dict]:
    symbols_by_id: dict[str, dict] = {
        sd["id"]: sd for sd in incoming_symbols if isinstance(sd, dict) and "id" in sd
    }
    for row in existing_rows:
        row_id = row.get("id")
        if row_id and row_id not in symbols_by_id:
            symbols_by_id[row_id] = row
    return list(symbols_by_id.values())


def update(
    exchanges: list[str] = EXCHANGES,
    source: SymbolSource | None = None,
) -> None:
    if source is None:
        source = HybridSymbolSource()
    _DATA_DIR.mkdir(exist_ok=True)
    total = 0
    for exchange in exchanges:
        path = _DATA_DIR / f"{exchange}.json"
        existing_rows, existing_by_id = _load_existing_symbols(path)
        if is_beta_exchange(exchange):
            print(f"Skipping {exchange} (beta exchange)", flush=True)
            continue
        print(f"Fetching {exchange}...", flush=True)
        try:
            data = source.fetch_exchange(exchange)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            continue

        allowed_types = {"spot", "perpetual", "future"}
        incoming_symbols = [
            sd
            for sd in data.get("availableSymbols", [])
            if sd.get("type") in allowed_types
        ]
        _normalize_exchange_symbols(exchange, incoming_symbols)
        _merge_existing_fields(incoming_symbols, existing_by_id)
        _apply_snapshot_metadata(incoming_symbols)
        for sd in incoming_symbols:
            _enrich(exchange, sd)

        # Never drop existing rows if the source omits them.
        symbols = _merge_missing_rows(incoming_symbols, existing_rows)
        symbols.sort(
            key=lambda sd: (
                sd.get("id", ""),
                sd.get("first_capture") or "",
                sd.get("end_date") or "",
            )
        )
        path.write_text(json.dumps(symbols, indent=2))
        total += len(symbols)
        print(f"  {len(symbols)} symbols → {path.name}")

    print(f"\nTotal: {total} symbols across {len(exchanges)} exchanges")


if __name__ == "__main__":
    load_dotenv()
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Update securities master using exchange APIs for Binance/OKX and "
            "Tardis fallback for all other exchanges"
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
        default="hybrid",
        help=(
            "Data source mode: hybrid (default, Binance/OKX direct + Tardis fallback), "
            "tardis (all via Tardis), exchange (direct only; only Binance/OKX supported)"
        ),
    )
    args = parser.parse_args()

    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()] or EXCHANGES
    source: SymbolSource
    if args.source == "tardis":
        source = TardisSymbolSource()
    elif args.source == "exchange":
        source = ExchangeApiSymbolSource()
    else:
        source = HybridSymbolSource()

    update(exchanges, source=source)
