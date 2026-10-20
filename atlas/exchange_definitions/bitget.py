from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .fields import parse_recorded_legs
from .http import get_json, iso_from_millis
from .common import (
    KNOWN_QUOTES,
    SkipSymbol,
    delisted_end_date,
    instrument_type,
    make_contract,
    ms_to_utc_timestamp,
    parse_concat,
    resolve_margin,
    split_concat,
)

# Spot quote assets for Tardis-only rows without API legs. Bitget adds `RLUSD`
# (tried before `USD`) and `VND`, and has never quoted in `TUSD`, which would
# otherwise split `USDTUSD` (USDT/USD) as USD/TUSD.
_SPOT_QUOTES = ["RLUSD", *(q for q in KNOWN_QUOTES if q != "TUSD"), "VND"]


def parse_bitget(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; Tardis-only spot rows are concatenated (`BTCUSDT`)."""
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    return parse_concat(exchange, sd, _SPOT_QUOTES)


def parse_bitget_futures(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]

    if "_" in sid:
        base_str, suffix = sid.rsplit("_", 1)
        suffix_quotes = {
            "UMCBL": ["USDT"],
            "SUMCBL": ["USDT"],
            "CMCBL": ["USDC"],
            "DMCBL": ["USD"],
        }
        quotes = suffix_quotes.get(suffix)
        if quotes is None:
            raise SkipSymbol(f"{exchange}: unknown suffix {suffix!r} in {sid!r}")

        pair = split_concat(base_str, quotes)
        if pair:
            symbol, denominator = pair
            ctype = instrument_type(sd)
            margin = resolve_margin(symbol, denominator, ctype)
            return make_contract(exchange, sd, symbol, denominator, margin, ctype)
        raise SkipSymbol(f"{exchange}: cannot split {base_str!r} with suffix {suffix!r}")

    return parse_concat(exchange, sd)


# --- Exchange API (live-producers' bitget-* venues) -------------------------
#
# Bitget's v3 (unified account) instruments endpoint. Coin-margined contracts
# exist only there: v2 lists none since the migration to `BTCUSD_CM` /
# `BTCCMZ26` symbols. USDT/USDC symbols are identical in v2 and v3.
_BITGET_V3_INSTRUMENTS = "https://api.bitget.com/api/v3/market/instruments"

# `symbolType` classifies the underlying. Bitget leaves some RWA contracts
# (FX pairs such as EURUSDUSDT, Japanese stocks, GPU-rental indices) at
# `crypto` while flagging them `isRwa: YES`; those are `unknown` rather than
# crypto, since the venue says they are not crypto but not what they are.
_BITGET_SYMBOL_TYPES = {
    "crypto": UnderlyingType.crypto,
    "stock": UnderlyingType.equity,
    "metal": UnderlyingType.commodity,
    "commodity": UnderlyingType.commodity,
}


def _bitget_underlying(item: dict) -> UnderlyingType:
    underlying = _BITGET_SYMBOL_TYPES.get(item.get("symbolType") or "", UnderlyingType.unknown)
    if underlying == UnderlyingType.crypto and item.get("isRwa") == "YES":
        return UnderlyingType.unknown
    return underlying


# v3 `status`: `listed` has not started trading, `offline` is delisted (at
# `offTime`, -1 until set), and `online`, `limit_open`, `limit_close` and
# `restrictedAPI` are listed, the last three with temporary restrictions.
_BITGET_NOT_YET_LISTED = "listed"
_BITGET_DELISTED = "offline"


def _bitget_symbol(item: dict, category: str) -> dict:
    base, quote = item["baseCoin"], item["quoteCoin"]
    coin_margined = category == "COIN-FUTURES"
    sd = {
        "id": item["symbol"],
        "type": "perpetual" if item.get("type") == "perpetual" else "future",
        "base_coin": base,
        "quote_coin": quote,
        "margin_asset": base if coin_margined else category.split("-", 1)[0],
        # Linear quantities are base units (`quantityMultiplier` is the order
        # step, not a multiplier). Coin-margined quantities are 1-USD contracts.
        "contract_size": 1.0,
        "quantity_unit": "quote" if coin_margined else "base",
        "underlying": _bitget_underlying(item).value,
    }
    if sd["type"] == "future":
        sd["delivery_date"] = iso_from_millis(item.get("deliveryTime"))
    # `launchTime` is 0 on contracts listed before Bitget recorded it.
    sd["first_capture"] = ms_to_utc_timestamp(item.get("launchTime"))
    if item.get("status") == _BITGET_DELISTED:
        sd["end_date"] = delisted_end_date(item.get("offTime"))
    return {k: v for k, v in sd.items() if v is not None}


def _fetch_bitget(category: str, timeout_seconds: int) -> list[dict]:
    payload = get_json(_BITGET_V3_INSTRUMENTS, timeout_seconds, params={"category": category})
    return [
        _bitget_symbol(item, category)
        for item in payload.get("data") or []
        if item.get("status") != _BITGET_NOT_YET_LISTED
    ]


def fetch_bitget_perpetual_usdt(timeout_seconds: int) -> list[dict]:
    return _fetch_bitget("USDT-FUTURES", timeout_seconds)


def fetch_bitget_perpetual_usdc(timeout_seconds: int) -> list[dict]:
    return _fetch_bitget("USDC-FUTURES", timeout_seconds)


def fetch_bitget_perpetual_coin(timeout_seconds: int) -> list[dict]:
    rows = _fetch_bitget("COIN-FUTURES", timeout_seconds)
    return [sd for sd in rows if sd["type"] == "perpetual"]


def fetch_bitget_futures_coin(timeout_seconds: int) -> list[dict]:
    rows = _fetch_bitget("COIN-FUTURES", timeout_seconds)
    return [sd for sd in rows if sd["type"] == "future"]


def parse_bitget_v3(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; Tardis-only rows are `BTCUSDT` or USDC `BTCPERP`."""
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    sid = sd["id"]
    if sid.endswith("PERP") and len(sid) > 4:
        ctype = instrument_type(sd)
        return make_contract(exchange, sd, sid[:-4], "USDC", "USDC", ctype)
    return parse_bitget_futures(exchange, sd)


def _bitget_spot_symbol(item: dict) -> dict:
    sd = {
        "id": item["symbol"],
        "type": "spot",
        "base_coin": item["baseCoin"],
        "quote_coin": item["quoteCoin"],
        "underlying": _bitget_underlying(item).value,
        "first_capture": ms_to_utc_timestamp(item.get("launchTime")),
    }
    if item.get("status") == _BITGET_DELISTED:
        sd["end_date"] = delisted_end_date(item.get("offTime"))
    return {k: v for k, v in sd.items() if v is not None}


def fetch_bitget_spot(timeout_seconds: int) -> list[dict]:
    """Bitget spot, from the same v3 endpoint and statuses as the contracts."""
    payload = get_json(_BITGET_V3_INSTRUMENTS, timeout_seconds, params={"category": "SPOT"})
    return [
        _bitget_spot_symbol(item)
        for item in payload.get("data") or []
        if item.get("status") != _BITGET_NOT_YET_LISTED
    ]
