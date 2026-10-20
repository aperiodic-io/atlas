from __future__ import annotations

from datetime import UTC, datetime

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import (
    upcoming,
    SkipSymbol,
    delisted_end_date,
    parse_utc_timestamp,
    instrument_type,
    make_contract,
    parse_yymmdd,
    resolve_margin,
    split_concat,
    utc_timestamp,
)
from .fields import parse_recorded_legs
from .http import get_json

CF_ASSET = {"XBT": "BTC"}


def parse_cryptofacilities(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    parts = sid.split("_")
    if len(parts) < 2:
        raise SkipSymbol(f"{exchange}: expected at least 2 underscore parts in {sid!r}")

    pair = split_concat(parts[1], ["USD", "EUR", "USDT"])
    if not pair:
        raise SkipSymbol(f"{exchange}: cannot split {parts[1]!r} in {sid!r}")

    symbol, denominator = pair
    symbol = CF_ASSET.get(symbol, symbol)
    ctype = instrument_type(sd)
    margin = resolve_margin(symbol, denominator, ctype)
    delivery = parse_yymmdd(parts[2]) if len(parts) >= 3 else None
    return make_contract(exchange, sd, symbol, denominator, margin, ctype, delivery)


# --- Exchange API (kraken-perps / kraken-futures) -----------------------------
#
# Kraken Futures (formerly Crypto Facilities; Tardis `cryptofacilities`) names
# contracts `<prefix>_<BASE><QUOTE>[_<yymmdd>]`. Prefixes: `PF_`/`FF_`
# multi-collateral linear perpetuals/futures, quantities in base units, PnL
# and settlement in USD; `PI_`/`FI_` inverse contracts worth 1 USD each,
# margined and settled in the base coin; `PV_`/`FV_` the 2019-20 XRP/XBT
# vanilla contracts, margined and settled in bitcoin; `OF_` options (excluded).
# Contracts name bitcoin `XBT`.

_KRAKEN_FUTURES_INSTRUMENTS = "https://futures.kraken.com/derivatives/api/v3/instruments"

_PREFIXES: dict[str, tuple[ContractType, str, str]] = {
    # prefix: (type, quote in the id, quantity unit)
    "PF": (ContractType.perpetual, "USD", "base"),
    "FF": (ContractType.future, "USD", "base"),
    "PI": (ContractType.perpetual, "USD", "quote"),
    "FI": (ContractType.future, "USD", "quote"),
    "PV": (ContractType.perpetual, "XBT", "base"),
    "FV": (ContractType.future, "XBT", "base"),
}
_INVERSE_PREFIXES = {"PI", "FI"}

# API `type`: `flexible_futures` (PF_/FF_) and `futures_inverse` (PI_/FI_);
# anything else (options, indices) is not a contract these venues list.
_API_TYPES = {"flexible_futures", "futures_inverse"}

# `category` is Kraken's sector label. Crypto sectors, then the tradfi ones.
# `Stablecoin` includes tokenized gold (PAXG, XAUT): Atlas follows the venue.
# `Real-world assets` is a crypto sector (the Centrifuge token, CFG), not a
# tradfi underlying, which Kraken files under the dedicated categories below.
_CRYPTO_CATEGORIES = frozenset(
    {
        "AI", "Community", "DeFi", "DePIN", "DEX", "Gaming", "Infrastructure", "Layer 1",
        "Layer 2", "Meme", "Privacy", "Real-world assets", "Stablecoin", "Utility", "Web3",
    }
)
_TRADFI_CATEGORIES = {
    "xStocks": UnderlyingType.equity,
    "Equities": UnderlyingType.equity,
    "Commodities": UnderlyingType.commodity,
    "Forex": UnderlyingType.forex,
    "Indices": UnderlyingType.index,
    "Pre-IPO": UnderlyingType.pre_market,
}


def _kraken_underlying(item: dict) -> UnderlyingType:
    category = item.get("category") or ""
    if category in _TRADFI_CATEGORIES:
        return _TRADFI_CATEGORIES[category]
    if category in _CRYPTO_CATEGORIES:
        return UnderlyingType.unknown if item.get("tradfi") else UnderlyingType.crypto
    if not category and item.get("type") == "futures_inverse" and not item.get("tradfi"):
        # The legacy inverse contracts carry no category; a contract margined
        # and settled in its base coin can only be on a crypto asset.
        return UnderlyingType.crypto
    return UnderlyingType.unknown


def _parse_iso(value: object) -> datetime | None:
    """A venue timestamp, or None when it is missing or unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_utc_timestamp(value)
    except ValueError:
        return None


def _millis(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value else None


# Lifecycle: the endpoint lists live contracts (`tradeable`, `suspended` and
# `postOnly` are trading restrictions; such contracts stay listed) and drops
# delisted ones. `isExpired` ends a contract at its `lastTradingTime` (or at
# first sighting without one); an `openingDate` still in the future is a
# contract not trading yet (`upcoming`).
def _kraken_derivative_symbol(item: dict) -> dict:
    inverse = item["type"] == "futures_inverse"
    base, quote = item["base"], item["quote"]
    prefix = item["symbol"].split("_", 1)[0]
    ctype = _PREFIXES[prefix][0]
    last_trading = _parse_iso(item.get("lastTradingTime"))
    opening = _parse_iso(item.get("openingDate"))
    sd = {
        "id": item["symbol"],
        "type": ctype.value,
        "base_coin": base,
        "quote_coin": quote,
        "margin_asset": base if inverse else quote,
        # Inverse: 1 USD per contract. Linear: `contractSize` base units.
        "contract_size": float(item.get("contractSize") or 1.0),
        "quantity_unit": "quote" if inverse else "base",
        "underlying": _kraken_underlying(item).value,
        "first_capture": utc_timestamp(opening) if opening else None,
    }
    if ctype == ContractType.future:
        sd["delivery_date"] = last_trading.isoformat().replace("+00:00", "Z") if last_trading else None
    if item.get("isExpired"):
        sd["end_date"] = delisted_end_date(_millis(last_trading))
    return {k: v for k, v in sd.items() if v is not None}


def _is_contract(item: dict) -> bool:
    return (
        item.get("type") in _API_TYPES
        and str(item.get("symbol", "")).split("_", 1)[0] in _PREFIXES
    )


def _has_opened(item: dict, now: datetime) -> bool:
    opening = _parse_iso(item.get("openingDate"))
    return opening is None or opening <= now


def _fetch_kraken_derivatives(timeout_seconds: int) -> list[dict]:
    payload = get_json(_KRAKEN_FUTURES_INSTRUMENTS, timeout_seconds)
    if not isinstance(payload, dict) or payload.get("result") != "success":
        error = payload.get("error") if isinstance(payload, dict) else type(payload).__name__
        raise ValueError(f"Kraken Futures instruments: unexpected response {error!r}")
    instruments = payload.get("instruments")
    if not isinstance(instruments, list) or not instruments:
        raise ValueError("Kraken Futures instruments: expected a non-empty instruments list")
    now = datetime.now(UTC)
    rows = []
    for item in instruments:
        if not isinstance(item, dict) or not _is_contract(item):
            continue
        sd = _kraken_derivative_symbol(item)
        rows.append(sd if _has_opened(item, now) else upcoming(sd))
    return rows


def fetch_kraken_perpetual(timeout_seconds: int) -> list[dict]:
    return [sd for sd in _fetch_kraken_derivatives(timeout_seconds) if sd["type"] == "perpetual"]


def fetch_kraken_futures(timeout_seconds: int) -> list[dict]:
    return [sd for sd in _fetch_kraken_derivatives(timeout_seconds) if sd["type"] == "future"]


def parse_kraken_derivative(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; Tardis-only rows are parsed from the naming scheme."""
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    sid = sd["id"]
    parts = sid.split("_")
    spec = _PREFIXES.get(parts[0])
    if spec is None or len(parts) not in (2, 3):
        raise SkipSymbol(f"{exchange}: unsupported Kraken Futures id {sid!r}")
    ctype, id_quote, quantity_unit = spec
    if (len(parts) == 3) != (ctype == ContractType.future):
        raise SkipSymbol(f"{exchange}: expiry does not match the prefix of {sid!r}")
    pair = parts[1]
    if not pair.endswith(id_quote) or len(pair) == len(id_quote):
        raise SkipSymbol(f"{exchange}: expected <BASE>{id_quote} in {sid!r}")
    base = CF_ASSET.get(pair[: -len(id_quote)], pair[: -len(id_quote)])
    quote = CF_ASSET.get(id_quote, id_quote)
    delivery = None
    if ctype == ContractType.future:
        delivery = parse_yymmdd(parts[2]) if len(parts[2]) == 6 else None
        if delivery is None:
            raise SkipSymbol(f"{exchange}: bad expiry in {sid!r}")
    if parts[0] in _INVERSE_PREFIXES:
        margin = base
    else:
        margin = "BTC" if id_quote == "XBT" else "USD"
    return make_contract(exchange, sd, base, quote, margin, ctype, delivery, quantity_unit)
