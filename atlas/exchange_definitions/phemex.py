from __future__ import annotations

import re
from datetime import UTC, datetime

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import (
    upcoming,
    SkipSymbol,
    delisted_end_date,
    instrument_type,
    make_contract,
    ms_to_datetime,
    ms_to_utc_timestamp,
    resolve_margin,
    split_concat,
)
from .fields import parse_recorded_legs
from .http import get_json


def parse_phemex(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = instrument_type(sd)

    if sid.startswith("s"):
        pair = split_concat(sid[1:])
        if pair:
            symbol, denominator = pair
            return make_contract(exchange, sd, symbol, denominator, None, ctype)
        raise SkipSymbol(f"{exchange}: cannot split {sid!r} (s-prefixed spot)")

    pair = split_concat(sid, ["USDT", "USDC", "USD", "BTC", "ETH"])
    if pair:
        symbol, denominator = pair
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


# --- Exchange API (phemex-spot, phemex-perps) -------------------------------
#
# `/public/products-plus` lists every product Phemex ever had, delisted ones
# included, each with a `timeline` of epoch-ms milestones:
# [orders open, trading opens (= `listTime`), cancel-only, delisting starts,
# delisted]; unset milestones are 0. Symbols are Tardis's `phemex` ids:
#
# - spot (`products`, type `Spot`): `sBTCUSDT`, `s1000BONKUSDT`.
# - legacy contracts (`products`, type `Perpetual`): inverse `BTCUSD` and
#   `cETHUSD` (settled in the coin, each contract 1 USD) and the delisted
#   USD-settled book (`ETHUSD` at 0.005 ETH, `uBTCUSD`, `u100VETUSD` whose unit
#   is "100 VET").
# - linear contracts (`perpProductsV2`, type `PerpetualV2`): `BTCUSDT`,
#   `AAVEUSDC`, `u1000PEPEUSDT` (base "1000 PEPE"); quantities are base units.
# - pilot contracts (`perpProductsPilot`, type `PerpetualPilot`), settled in
#   Phemex's PUSD (`DOGEAIPUSD`), all delisted.
#
# `status` is `Listed` or `Delisted`. A `Delisted` product ends at its delisted
# milestone (the delisting start if that is unset, the run time if neither is).
# A `Listed` product stays listed even when its timeline shows a delisting
# that has passed: Phemex reports the status, the timeline is a schedule. A
# product whose `listTime` is still ahead has not started trading and is
# excluded. A status Atlas does not know is kept listed (no `end_date`).
_PRODUCTS_URL = "https://api.phemex.com/public/products-plus"
_DELISTED = "Delisted"

# `perpProductSubType` (and, for TradFi, `perpProductSubGroup`) classifies
# linear contracts. Phemex files ETFs (QQQ, EWY) under `Indices`, which Atlas
# follows; `Metals` includes tokenized gold (PAXG).
_SUBTYPE_CLASSES = {
    "Normal": UnderlyingType.crypto,
    # Phemex's pilot zone for newly launched memecoins.
    "Pilot": UnderlyingType.crypto,
    "PreMarket": UnderlyingType.pre_market,
}
_TRADFI_GROUPS = {
    "Stocks": UnderlyingType.equity,
    "Indices": UnderlyingType.index,
    "Metals": UnderlyingType.commodity,
    "Commodities": UnderlyingType.commodity,
}
# Spot and the legacy contract book carry no classification. Both list crypto,
# except tokenized stocks on spot, which Phemex's currency names say are
# tokenized (`Apple xStock`, `Apple Ondo Tokenized`), and the delisted legacy
# `GOLDUSD`, priced on a `.GOLD` index Phemex does not describe.
_TOKENIZED_NAME = re.compile(r"xstock|tokenized", re.IGNORECASE)
# Phemex files ETFs (QQQ, EWY, KORU, SPYX) under `Indices`; its currency names
# say what they are ("iShares MSCI South Korea ETF", "Invesco QQQ"), and ETFs
# are equity securities.
_ETF_NAME = re.compile(
    r"\bETF\b|iShares|Invesco|SPDR|Vanguard|Direxion|ProShares|KraneShares", re.IGNORECASE
)


def _phemex_products(timeout_seconds: int) -> dict:
    payload = get_json(_PRODUCTS_URL, timeout_seconds)
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError(f"Phemex products: unexpected response {str(payload)[:200]!r}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise ValueError("Phemex products: no `products` list in the response")
    for key in ("perpProductsV2", "perpProductsPilot", "currencies"):
        if not isinstance(data.get(key) or [], list):
            raise ValueError(f"Phemex products: `{key}` is not a list")
    return data


def _not_yet_listed(item: dict, now: datetime) -> bool:
    if item.get("status") == _DELISTED:
        return False
    listed = ms_to_datetime(item.get("listTime"))
    return listed is not None and listed > now


def _currency_names(data: dict) -> dict[str, str]:
    return {str(c.get("currency")): str(c.get("name") or "") for c in data.get("currencies") or []}


def _flag_upcoming(item: dict, sd: dict, now: datetime) -> dict:
    return upcoming(sd) if _not_yet_listed(item, now) else sd


def _lifecycle(item: dict) -> dict:
    fields = {"first_capture": ms_to_utc_timestamp(item.get("listTime"))}
    if item.get("status") == _DELISTED:
        timeline = item.get("timeline") or []
        milestones = [m for m in timeline[3:5] if isinstance(m, int) and m > 0]
        fields["end_date"] = delisted_end_date(milestones[-1] if milestones else None)
    return fields


def _asset(value: str) -> str:
    """Phemex writes multiplied units with a space (`1000 PEPE`)."""
    return value.replace(" ", "").upper()


def _clean(sd: dict) -> dict:
    return {k: v for k, v in sd.items() if v is not None}


def _spot_symbol(item: dict, names: dict[str, str]) -> dict:
    base = item["baseCurrency"]
    tokenized = _TOKENIZED_NAME.search(names.get(base, ""))
    sd = {
        "id": item["symbol"],
        "type": "spot",
        "base_coin": _asset(base),
        "quote_coin": _asset(item["quoteCurrency"]),
        "underlying": (UnderlyingType.equity if tokenized else UnderlyingType.crypto).value,
        **_lifecycle(item),
    }
    return _clean(sd)


def fetch_phemex_spot(timeout_seconds: int) -> list[dict]:
    data = _phemex_products(timeout_seconds)
    names = _currency_names(data)
    now = datetime.now(UTC)
    return [
        _flag_upcoming(item, _spot_symbol(item, names), now)
        for item in data["products"]
        if item.get("type") == "Spot"
    ]


def _legacy_perp_symbol(item: dict) -> dict:
    settle = item["settleCurrency"]
    quote = item["quoteCurrency"]
    if settle != quote:
        # Inverse: margined in the coin, each contract worth `contractSize` USD.
        base, unit = settle, "quote"
    else:
        # USD-settled: each contract worth `contractSize` of the underlying.
        base, unit = item["contractUnderlyingAssets"], "base"
    base = _asset(base)
    underlying = UnderlyingType.unknown if base == "GOLD" else UnderlyingType.crypto
    return _clean({
        "id": item["symbol"],
        "type": "perpetual",
        "base_coin": base,
        "quote_coin": _asset(quote),
        "margin_asset": _asset(settle),
        "contract_size": float(item.get("contractSize") or 1.0),
        "quantity_unit": unit,
        "underlying": underlying.value,
        **_lifecycle(item),
    })


def _linear_underlying(item: dict, names: dict[str, str]) -> UnderlyingType:
    subtype = item.get("perpProductSubType")
    if subtype == "TradFi":
        underlying = _TRADFI_GROUPS.get(item.get("perpProductSubGroup") or "", UnderlyingType.unknown)
        if underlying == UnderlyingType.index and _ETF_NAME.search(names.get(item["baseCurrency"], "")):
            return UnderlyingType.equity
        return underlying
    return _SUBTYPE_CLASSES.get(subtype or "", UnderlyingType.unknown)


def _linear_perp_symbol(item: dict, underlying: UnderlyingType) -> dict:
    return _clean({
        "id": item["symbol"],
        "type": "perpetual",
        "base_coin": _asset(item["baseCurrency"]),
        "quote_coin": _asset(item["quoteCurrency"]),
        "margin_asset": _asset(item["settleCurrency"]),
        "contract_size": 1.0,
        "quantity_unit": "base",
        "underlying": underlying.value,
        **_lifecycle(item),
    })


def fetch_phemex_perpetual(timeout_seconds: int) -> list[dict]:
    data = _phemex_products(timeout_seconds)
    names = _currency_names(data)
    now = datetime.now(UTC)
    rows = [
        _flag_upcoming(item, _legacy_perp_symbol(item), now)
        for item in data["products"]
        if item.get("type") == "Perpetual"
    ]
    rows += [
        _flag_upcoming(item, _linear_perp_symbol(item, _linear_underlying(item, names)), now)
        for item in data.get("perpProductsV2") or []
    ]
    rows += [
        _flag_upcoming(item, _linear_perp_symbol(item, UnderlyingType.crypto), now)
        for item in data.get("perpProductsPilot") or []
    ]
    return rows


# Spot quote assets Phemex has used, for Tardis-only rows (`sIRENPUSD` quotes
# Phemex's pilot dollar PUSD). No spot pair quotes plain USD, so `PUSD` is
# never a misread `...P` / `USD`.
_SPOT_QUOTES = ["USDT", "USDC", "USDQ", "USDR", "PUSD", "TRY", "BRZ", "BTC", "ETH"]

# Pilot contracts are `<COIN>PUSD` (`DOGEAIPUSD`); so are these USD-settled
# legacy contracts whose coin ends in P. The legacy book is closed (every
# contract delisted by 2024, none added since), so the list is complete.
_LEGACY_PUSD_LOOKALIKES = frozenset(
    {"COMPUSD", "ICPUSD", "OPUSD", "SFPUSD", "SXPUSD", "XRPUSD", "u100SLPUSD"}
)


def parse_phemex_api(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; Tardis-only rows are parsed from the Phemex id.

    `sBTCUSDT` spot; linear `BTCUSDT` / `AAVEUSDC` / `u1000PEPEUSDT`; inverse
    `BTCUSD` and `cETHUSD`; any other `...USD` is the USD-settled legacy book
    (`ETHUSD`, `uBTCUSD`, `u100VETUSD`).
    """
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    sid = sd["id"]
    if sid.startswith("s"):
        pair = split_concat(sid[1:], _SPOT_QUOTES)
        if pair is None:
            raise SkipSymbol(f"{exchange}: cannot split spot id {sid!r}")
        return make_contract(exchange, sd, pair[0], pair[1], None, ContractType.spot)

    ctype = ContractType.perpetual
    if sid.startswith("c") and sid.endswith("USD") and len(sid) > 4:
        coin = sid[1:-3]
        return make_contract(exchange, sd, coin, "USD", coin, ctype, quantity_unit="quote")
    if sid == "BTCUSD":
        return make_contract(exchange, sd, "BTC", "USD", "BTC", ctype, quantity_unit="quote")
    body = sid[1:] if sid.startswith("u") else sid
    if sid.endswith("PUSD") and sid not in _LEGACY_PUSD_LOOKALIKES and len(sid) > 4:
        coin = sid[:-4]
        return make_contract(exchange, sd, coin, "PUSD", "PUSD", ctype, quantity_unit="base")
    pair = split_concat(body, ["USDT", "USDC", "USD"])
    if pair is None or not re.fullmatch(r"[A-Z0-9]+", pair[0]):
        raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")
    base, quote = pair
    return make_contract(exchange, sd, base, quote, quote, ctype, quantity_unit="base")
