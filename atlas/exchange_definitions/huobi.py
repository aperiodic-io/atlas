from __future__ import annotations

import re
from datetime import UTC, datetime

from ..contracts import Contract
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .fields import parse_recorded_legs
from .http import get_json, iso_from_millis
from .common import (
    upcoming,
    SkipSymbol,
    delisted_end_date,
    instrument_type,
    make_contract,
    ms_to_datetime,
    ms_to_utc_timestamp,
    parse_dash,
    parse_yymmdd,
    split_concat,
    utc_timestamp,
)

HUOBI_DM_ROLLING = {"CW", "NW", "CQ", "NQ"}


def parse_huobi_dm(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = instrument_type(sd)

    if "_" in sid:
        symbol, suffix = sid.split("_", 1)
        if suffix in HUOBI_DM_ROLLING:
            return make_contract(exchange, sd, symbol, "USD", symbol, ctype)
        raise SkipSymbol(f"{exchange}: unknown rolling suffix {suffix!r} in {sid!r}")

    match = re.match(r"^([A-Z]+)(\d{6})$", sid)
    if match:
        return make_contract(
            exchange,
            sd,
            match.group(1),
            "USD",
            match.group(1),
            ctype,
            parse_yymmdd(match.group(2)),
        )

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


def parse_huobi_dm_swap(exchange: str, sd: SymbolData) -> Contract:
    return parse_dash(exchange, sd)


def parse_huobi_dm_linear_swap(exchange: str, sd: SymbolData) -> Contract:
    return parse_dash(exchange, sd)


# --- Exchange API (live-producers' htx-perps-usdt) ----------------------
#
# contract_status: 0 delisted, 1 listed, 2 pending listing, 3 suspended,
# 4 listing suspended, 5 settling, 6 delivering, 7 settled, 8 delivered. Swaps
# pass through 5-7 at every funding settlement, and a suspension is temporary,
# so all of those are still listed (live-producers streams only 1, 5 and 6).
# A swap scheduled for delisting carries the time in `delivery_time` (empty
# otherwise) and is suspended until then.
_HTX_NOT_YET_LISTED = 2
_HTX_DELISTED_STATUSES = {0, 8}


def _htx_end_date(item: dict) -> str | None:
    delisting = ms_to_datetime(item.get("delivery_time"))
    if item.get("contract_status") in _HTX_DELISTED_STATUSES:
        return delisted_end_date(item.get("delivery_time"))
    if delisting and delisting <= datetime.now(UTC):
        return utc_timestamp(delisting)
    return None


def _htx_listed_at(create_date: object) -> str | None:
    """`create_date` is a `YYYYMMDD` day."""
    try:
        return utc_timestamp(datetime.strptime(str(create_date), "%Y%m%d"))
    except ValueError:
        return None

_HTX_UNDERLYING = (
    # An ETF is listed under both labels; it is an equity security.
    ("Stocks", UnderlyingType.equity),
    ("Indices", UnderlyingType.index),
    ("Metals", UnderlyingType.commodity),
    ("Commodities", UnderlyingType.commodity),
)


def _htx_underlying(labels: list[str]) -> UnderlyingType:
    if not labels:
        return UnderlyingType.crypto
    for label, underlying in _HTX_UNDERLYING:
        if label in labels:
            return underlying
    return UnderlyingType.unknown


def fetch_htx_perpetual_usdt(timeout_seconds: int) -> list[dict]:
    payload = get_json(
        "https://api.hbdm.vn/linear-swap-api/v1/swap_contract_info", timeout_seconds
    )
    symbols = []
    for item in payload.get("data", []):
        if item.get("business_type") != "swap":
            continue
        code = item["contract_code"]
        if item.get("contract_status") == _HTX_NOT_YET_LISTED:
            symbols.append(upcoming({"id": code, "type": "perpetual"}))
            continue
        base, quote = code.split("-", 1)
        sd = {
            "id": code,
            "type": "perpetual",
            "base_coin": base,
            "quote_coin": quote,
            "margin_asset": item.get("trade_partition") or quote,
            "contract_size": float(item.get("contract_size") or 1.0),
            "quantity_unit": "base",
            "underlying": _htx_underlying(item.get("tradfi_labels") or []).value,
            "first_capture": _htx_listed_at(item.get("create_date")),
            "end_date": _htx_end_date(item),
        }
        symbols.append({k: v for k, v in sd.items() if v is not None})
    return symbols


def parse_htx_linear_swap(exchange: str, sd: SymbolData) -> Contract:
    return parse_recorded_legs(exchange, sd) or parse_huobi_dm_linear_swap(exchange, sd)


# --- Exchange API: htx-spot, htx-perps-coin, htx-futures-coin, htx-futures-usdt
#
# HTX serves the same contract catalogue from api.hbdm.com and api.hbdm.vn;
# like htx-perps-usdt these fetchers use the .vn host.
_HTX_DM = "https://api.hbdm.vn"
_HTX_SPOT = "https://api.huobi.pro"


def _htx_data(payload: object, what: str) -> list[dict]:
    """The `data` list of an HTX `{"status": "ok", "data": [...]}` envelope."""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        detail = (
            payload.get("err_msg") or payload.get("err-msg")
            if isinstance(payload, dict)
            else type(payload).__name__
        )
        raise ValueError(f"HTX {what}: unexpected response ({detail})")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError(f"HTX {what}: `data` is not a list")
    return data


def _htx_contract_row(item: dict, sd: dict) -> dict:
    """Lifecycle fields shared by every HTX contract (see the status table above)."""
    sd["first_capture"] = _htx_listed_at(item.get("create_date"))
    sd["end_date"] = _htx_end_date(item)
    sd = {k: v for k, v in sd.items() if v is not None}
    return upcoming(sd) if item.get("contract_status") == _HTX_NOT_YET_LISTED else sd


def _htx_contracts(path: str, timeout_seconds: int, params: dict | None = None) -> list[dict]:
    payload = get_json(f"{_HTX_DM}{path}", timeout_seconds, params=params)
    return _htx_data(payload, path)


def _htx_coin_row(item: dict, kind: str) -> dict:
    """A coin-margined (inverse) contract: margined in the base coin, one
    contract is `contract_size` USD. HTX publishes no underlying classification
    for coin-margined contracts and lists only crypto there (BTC, ETH, ...)."""
    base = item["symbol"]
    sd = {
        "id": item["contract_code"],
        "type": kind,
        "base_coin": base,
        "quote_coin": "USD",
        "margin_asset": base,
        "contract_size": float(item.get("contract_size") or 1.0),
        "quantity_unit": "quote",
        "underlying": UnderlyingType.crypto.value,
    }
    if kind == "future":
        sd["delivery_date"] = iso_from_millis(item.get("delivery_time"))
    return _htx_contract_row(item, sd)


def fetch_htx_perpetual_coin(timeout_seconds: int) -> list[dict]:
    """Coin-margined perpetual swaps (`BTC-USD`)."""
    items = _htx_contracts("/swap-api/v1/swap_contract_info", timeout_seconds)
    return [_htx_coin_row(item, "perpetual") for item in items]


def fetch_htx_futures_coin(timeout_seconds: int) -> list[dict]:
    """Coin-margined delivery futures, by dated code (`BTC261023`).

    Not by HTX's rolling aliases (`BTC_CW`, `BTC_CQ`), which re-point at a
    new contract every week or quarter.
    """
    items = _htx_contracts("/api/v1/contract_contract_info", timeout_seconds)
    return [_htx_coin_row(item, "future") for item in items]


def fetch_htx_futures_usdt(timeout_seconds: int) -> list[dict]:
    """USDT-margined delivery futures (`BTC-USDT-261023`).

    They share `swap_contract_info` with the perpetuals, which the endpoint
    returns only for `business_type=futures` (or `all`).
    """
    items = _htx_contracts(
        "/linear-swap-api/v1/swap_contract_info",
        timeout_seconds,
        params={"business_type": "futures"},
    )
    rows = []
    for item in items:
        if item.get("business_type") != "futures":
            continue
        base, quote = item["pair"].split("-", 1)
        sd = {
            "id": item["contract_code"],
            "type": "future",
            "base_coin": base,
            "quote_coin": quote,
            "margin_asset": item.get("trade_partition") or quote,
            "contract_size": float(item.get("contract_size") or 1.0),
            "quantity_unit": "base",
            "delivery_date": iso_from_millis(item.get("delivery_time")),
            "underlying": _htx_underlying(item.get("tradfi_labels") or []).value,
        }
        rows.append(_htx_contract_row(item, sd))
    return rows


def parse_htx_coin_swap(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; history rows are `BTC-USD` (coin-margined)."""
    return parse_recorded_legs(exchange, sd) or parse_huobi_dm_swap(exchange, sd)


def parse_htx_coin_futures(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; otherwise the dated code `BTC261023`."""
    return parse_recorded_legs(exchange, sd) or parse_huobi_dm(exchange, sd)


def parse_htx_usdt_futures(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; history rows are `BTC-USDT-261023`."""
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    match = re.fullmatch(r"([^-]+)-([A-Z]+)-(\d{6})", sd["id"])
    delivery = parse_yymmdd(match.group(3)) if match else None
    if match is None or delivery is None:
        raise SkipSymbol(f"{exchange}: cannot parse {sd['id']!r}")
    symbol, quote = match.group(1), match.group(2)
    return make_contract(exchange, sd, symbol, quote, quote, instrument_type(sd), delivery)


# Spot `state` (v2 settings/common/symbols): `online` trades; `suspend`,
# `fuse` (circuit breaker) and `transfer-board` (moving between boards) are
# temporary and stay listed; `offline` is delisted, with no delisting time
# published, so it ends at first sighting; `pre-online` and `not-online` have
# not opened yet and are left out. `toa` is the trading-open time (1514779200000,
# 2018-01-01, on everything listed before HTX recorded it).
_HTX_SPOT_NOT_YET_LISTED = {"pre-online", "not-online"}
_HTX_SPOT_DELISTED = "offline"

# Spot symbols carry no asset class; the base currency's `tags`
# (v2 settings/common/currencies) do. HTX opened TradFi markets in 2026 and
# re-tagged existing codes for them, so a market that opened before then on a
# code now tagged TradFi traded an older crypto token of that name (BOT 2020,
# WMT 2022, GME 2024): its class is `unknown`. Tokenised assets that are also
# tagged `stablecoin` (XAUT) are exempt.
_HTX_TRADFI_SINCE_MS = 1767225600000  # 2026-01-01T00:00:00Z
_HTX_SPOT_TAGS = (
    ("stock", UnderlyingType.equity),
    ("indices", UnderlyingType.index),
    ("commodities", UnderlyingType.commodity),
    ("fiatassets", UnderlyingType.forex),
    ("tradfi", UnderlyingType.unknown),
)


def _htx_spot_underlying(currency_tags: str | None, opened_ms: object) -> UnderlyingType:
    if currency_tags is None:
        return UnderlyingType.unknown
    tags = set(currency_tags.split(","))
    try:
        opened = int(opened_ms)
    except (TypeError, ValueError):
        opened = 0
    if "tradfi" in tags and "stablecoin" not in tags and opened < _HTX_TRADFI_SINCE_MS:
        return UnderlyingType.unknown
    for tag, underlying in _HTX_SPOT_TAGS:
        if tag in tags:
            return underlying
    return UnderlyingType.crypto


def fetch_htx_spot(timeout_seconds: int) -> list[dict]:
    """HTX spot (lowercase ids, `btcusdt`).

    The symbol list keeps delisted markets (`offline`) indefinitely. `bc` and
    `qc` are the assets' current codes: a market keeps its id when HTX renames
    an asset (`maticeth` trades POL, `lunaust` LUNC/USTC).
    """
    symbols = _htx_data(
        get_json(f"{_HTX_SPOT}/v2/settings/common/symbols", timeout_seconds), "spot symbols"
    )
    currencies = _htx_data(
        get_json(f"{_HTX_SPOT}/v2/settings/common/currencies", timeout_seconds), "currencies"
    )
    tags = {c["cc"]: c.get("tags") or "" for c in currencies if c.get("cc")}
    rows = []
    for item in symbols:
        state = item.get("state")
        sd = {
            "id": item["sc"],
            "type": "spot",
            "base_coin": item["bc"].upper(),
            "quote_coin": item["qc"].upper(),
            "underlying": _htx_spot_underlying(tags.get(item["bc"]), item.get("toa")).value,
            "first_capture": ms_to_utc_timestamp(item.get("toa")),
            "end_date": delisted_end_date() if state == _HTX_SPOT_DELISTED else None,
        }
        sd = {k: v for k, v in sd.items() if v is not None}
        rows.append(upcoming(sd) if state in _HTX_SPOT_NOT_YET_LISTED else sd)
    return rows


# Spot quote assets, longest first, for history rows (`btcusdt`).
_HTX_SPOT_QUOTES = [
    "EUROC", "PYUSD",
    "USDT", "HUSD", "USDC", "USDD", "TUSD", "USD1", "USTC", "KGST",
    "BTC", "ETH", "TRX", "TRY", "RUB", "EUR", "UAH", "BRL", "GBP",
    "HT",
]  # fmt: skip

# Markets whose id predates an asset rename (the API reports the current
# codes); the id alone would give the old ones.
_HTX_SPOT_RENAMED = {
    "akrobtc": ("KAON", "BTC"),
    "akroht": ("KAON", "HT"),
    "akrohusd": ("KAON", "HUSD"),
    "cvcoineth": ("CVNX", "ETH"),
    "hotbtc": ("HYDRO", "BTC"),
    "hoteth": ("HYDRO", "ETH"),
    "lunabtc": ("LUNC", "BTC"),
    "lunaht": ("LUNC", "HT"),
    "lunaust": ("LUNC", "USTC"),
    "maticeth": ("POL", "ETH"),
    "maticusdc": ("POL", "USDC"),
    "maticusdd": ("POL", "USDD"),
    "mirust": ("MIR", "USTC"),
    "mtlbtc": ("MTLOLD", "BTC"),
    "nanobtc": ("XNO", "BTC"),
    "nanoeth": ("XNO", "ETH"),
    "newbtc": ("AB", "BTC"),
    "newht": ("AB", "HT"),
    "noeusdt": ("NOW", "USDT"),
    "tonusdd": ("GRAM", "USDD"),
    "xptusdt": ("XPT1", "USDT"),
}


def parse_htx_spot(exchange: str, sd: SymbolData) -> Contract:
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    pair = _HTX_SPOT_RENAMED.get(sd["id"]) or split_concat(sd["id"].upper(), _HTX_SPOT_QUOTES)
    if pair is None:
        raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r}")
    return make_contract(exchange, sd, pair[0], pair[1], None, instrument_type(sd))
