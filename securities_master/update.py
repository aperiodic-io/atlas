"""Fetch securities master data from Tardis and save one JSON file per exchange."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from securities_master.parsers import SkipSymbol, parse_contract
from dotenv import load_dotenv
from utils import _fetch_exchange

_DATA_DIR = Path(__file__).parent / "data"

EXCHANGES = [
    "binance-delivery",
    "binance-futures",
    "binance",
    "bitfinex-derivatives",
    "bitfinex",
    "bitget-futures",
    "bitget",
    "bitmex",
    "bitstamp",
    "bybit-spot",
    "bybit",
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
    "hyperliquid",
    "kraken",
    "kucoin",
    "okex-futures",
    "okex-swap",
    "okex",
    "phemex",
    "poloniex",
    "upbit",
]


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
        # Spot instruments are not margined; do not persist a margin field.
        if c.contract_type.value == "spot":
            sd.pop("margin", None)
        else:
            sd["margin"] = c.margin
        sd["contract_type"] = c.contract_type.value
        sd["delivery_date"] = c.delivery_date.isoformat() if c.delivery_date else None
        sd["contract_size"] = c.contract_size
    except SkipSymbol:
        sd.update(_NONE)


def update(exchanges: list[str] = EXCHANGES) -> None:
    _DATA_DIR.mkdir(exist_ok=True)
    total = 0
    allowed_types = {"spot", "perpetual", "future"}
    for exchange in exchanges:
        print(f"Fetching {exchange}...", flush=True)
        try:
            data = _fetch_exchange(exchange)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            continue

        symbols = [
            sd
            for sd in data.get("availableSymbols", [])
            if sd.get("type") in allowed_types
        ]
        for sd in symbols:
            if "availableSince" in sd:
                sd["tardis_first_capture"] = sd.pop("availableSince")
            if "availableTo" in sd:
                sd["end_date"] = sd.pop("availableTo")
            _enrich(exchange, sd)
        symbols.sort(
            key=lambda sd: (
                sd.get("id", ""),
                sd.get("tardis_first_capture") or "",
                sd.get("end_date") or "",
            )
        )
        path = _DATA_DIR / f"{exchange}.json"
        path.write_text(json.dumps(symbols, indent=2))
        total += len(symbols)
        print(f"  {len(symbols)} symbols → {path.name}")

    print(f"\nTotal: {total} symbols across {len(exchanges)} exchanges")


if __name__ == "__main__":
    load_dotenv()
    import argparse

    parser = argparse.ArgumentParser(description="Update securities master from Tardis")
    parser.add_argument(
        "--exchanges",
        type=str,
        default="",
        help="Comma-separated list of exchanges (default: all)",
    )
    args = parser.parse_args()

    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()] or EXCHANGES
    update(exchanges)
