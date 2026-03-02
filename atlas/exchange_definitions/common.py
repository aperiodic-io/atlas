from __future__ import annotations

from datetime import datetime

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData


class SkipSymbol(Exception):
    """Raised by a parser when a symbol is intentionally skipped or unrecognised."""


# Known quote currencies ordered longest-first to avoid ambiguous splits.
KNOWN_QUOTES = [
    "USDT",
    "BUSD",
    "USDC",
    "USD1",
    "TUSD",
    "BIDR",
    "BVND",
    "IDRT",
    "FDUSD",
    "USDP",
    "BTC",
    "ETH",
    "BNB",
    "XRP",
    "TRX",
    "DOGE",
    "SOL",
    "DOT",
    "MNT",
    "USDE",
    "USDS",
    "USD",
    "JPY",
    "MXN",
    "COP",
    "IDR",
    "RON",
    "CZK",
    "GYEN",
    "EURI",
    "U",
    "EUR",
    "GBP",
    "AUD",
    "PLN",
    "ARS",
    "TRY",
    "BRL",
    "UAH",
    "NGN",
    "ZAR",
    "RUB",
    "DAI",
    "VAI",
    "KRW",
    "UST",
    "GUSD",
    "CNHT",
    "XAUT",
    "PAX",
]

CME_MONTHS = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}

TYPE_MAP: dict[str, ContractType] = {
    "spot": ContractType.spot,
    "perpetual": ContractType.perpetual,
    "future": ContractType.future,
    "option": ContractType.option,
}


def contract_type(sd: SymbolData) -> ContractType:
    return TYPE_MAP.get(sd.get("type", ""), ContractType.unknown)


def split_concat(symbol: str, quotes: list[str] = KNOWN_QUOTES) -> tuple[str, str] | None:
    """Split a concatenated pair like BTCUSDT -> (BTC, USDT)."""
    for quote in quotes:
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            if base:
                return base, quote
    return None


def parse_yymmdd(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%y%m%d")
    except ValueError:
        return None


def parse_yyyymmdd(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None


def parse_ddmmmyy(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.upper(), "%d%b%y")
    except ValueError:
        return None


def parse_cme_month_year(month_code: str, year_suffix: str) -> datetime | None:
    month = CME_MONTHS.get(month_code.upper())
    if month is None:
        return None
    try:
        return datetime(2000 + int(year_suffix), month, 1)
    except ValueError:
        return None


def resolve_margin(symbol: str, denominator: str, ctype: ContractType) -> str | None:
    if ctype == ContractType.spot:
        return None

    linear_denoms = {
        "USDT",
        "USDC",
        "BUSD",
        "TUSD",
        "USDP",
        "FDUSD",
        "DAI",
        "EUR",
        "GBP",
        "AUD",
        "TRY",
        "KRW",
        "GUSD",
        "CNHT",
    }
    return denominator if denominator in linear_denoms else symbol


def make_contract(
    exchange: str,
    sd: SymbolData,
    symbol: str,
    denominator: str,
    margin: str | None,
    ctype: ContractType,
    delivery_date: datetime | None = None,
    contract_size: float = 1.0,
) -> Contract:
    return Contract(
        exchange=exchange,
        original_id=sd["id"],
        symbol=symbol.upper(),
        denominator=denominator.upper(),
        margin=margin.upper() if margin is not None else None,
        delivery_date=delivery_date,
        contract_type=ctype,
        contract_size=contract_size,
    )


def parse_concat(exchange: str, sd: SymbolData, quotes: list[str] = KNOWN_QUOTES) -> Contract:
    pair = split_concat(sd["id"].upper(), quotes)
    if pair is None:
        raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r} against known quotes")
    symbol, denominator = pair
    margin = resolve_margin(symbol, denominator, contract_type(sd))
    return make_contract(exchange, sd, symbol, denominator, margin, contract_type(sd))


def parse_dash(exchange: str, sd: SymbolData) -> Contract:
    parts = sd["id"].split("-")
    if len(parts) == 2:
        symbol, denominator = parts
        margin = resolve_margin(symbol, denominator, contract_type(sd))
        return make_contract(exchange, sd, symbol, denominator, margin, contract_type(sd))
    raise SkipSymbol(f"{exchange}: expected 2 dash-separated parts in {sd['id']!r}")


def parse_underscore_spot(exchange: str, sd: SymbolData) -> Contract:
    parts = sd["id"].split("_")
    if len(parts) == 2:
        symbol, denominator = parts
        return make_contract(exchange, sd, symbol, denominator, None, ContractType.spot)
    raise SkipSymbol(
        f"{exchange}: expected 2 underscore-separated parts in {sd['id']!r}"
    )
