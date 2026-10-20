from __future__ import annotations

import re
from typing import Any

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import (
    SkipSymbol,
    instrument_type,
    make_contract,
    ms_to_utc_timestamp,
    resolve_margin,
    split_concat,
)
from .fields import parse_recorded_legs
from .http import get_json


def parse_bitfinex(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    if sid.startswith("f"):
        raise SkipSymbol(f"{exchange}: funding symbol {sid!r} skipped")
    if not sid.startswith("t"):
        raise SkipSymbol(f"{exchange}: expected 't' prefix in {sid!r}")

    pair_str = sid[1:]
    ctype = instrument_type(sd)

    if ":" in pair_str:
        symbol, denominator = pair_str.split(":", 1)
        denominator = "USDT" if denominator == "UST" else denominator
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    quotes = [
        "USDT",
        "UST",
        "USD",
        "BTC",
        "ETH",
        "EOS",
        "EUR",
        "GBP",
        "JPY",
        "CNHT",
        "XAUT",
    ]
    pair = split_concat(pair_str, quotes)
    if pair:
        symbol, denominator = pair
        denominator = "USDT" if denominator == "UST" else denominator
        margin = resolve_margin(symbol, denominator, ctype)
        return make_contract(exchange, sd, symbol, denominator, margin, ctype)

    raise SkipSymbol(f"{exchange}: cannot split {sid!r}")


def parse_bitfinex_derivatives(exchange: str, sd: SymbolData) -> Contract:
    match = re.match(r"^([A-Z]+)F0:([A-Z]+)F0$", sd["id"])
    if match:
        symbol = match.group(1)
        denominator = "USDT" if match.group(2) == "UST" else match.group(2)
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            denominator,
            instrument_type(sd),
        )
    raise SkipSymbol(f"{exchange}: cannot parse derivative {sd['id']!r}")


# --- Exchange API (`bitfinex-spot`, `bitfinex-perps`) -----------------------
#
# Bitfinex publishes its markets as `conf` lists. Tardis ids are the trading
# symbols without the `t` prefix: spot `BTCUSD` (two three-letter codes) or
# `AAVE:USD` (longer codes, colon-separated), perpetuals `BTCF0:USTF0`. The
# legacy parsers above expect the `t` prefix and stay as they are.
#
# Bitfinex reports no per-pair status: the `pub:list:pair:*` lists are the
# pairs currently on the venue, and a delisted pair simply leaves them (the
# updater dates it from its disappearance). There is no pre-launch state to
# exclude. Paper-trading pairs (`TESTBTC:TESTUSD`, `TESTBTCF0:TESTUSDTF0`; the
# `pub:list:currency:paper` currencies) are excluded.
_BITFINEX_CONF = "https://api-pub.bitfinex.com/v2/conf"

# Bitfinex currency codes -> common tickers. From Bitfinex's own
# `pub:map:currency:sym` (ALG->ALGO, UST->USDt, WBT->WBTC, WHBT->WBT, ...) and,
# for codes of delisted pairs only Tardis still knows, ccxt's Bitfinex
# `commonCurrencies`. Deliberately not followed from Bitfinex's map: TON->GRAM
# (Toncoin trades as TON) and EOS->A (the Vaulta rebrand; Bitfinex's EOS pairs
# traded as EOS). A static table so that a Tardis-only row of a pair gets the
# same internal_id its API row did.
_BITFINEX_ALIASES = {
    "ALG": "ALGO",
    "AMP": "AMPL",
    "ATO": "ATOM",
    "BAB": "BCH",
    "BCHABC": "XEC",
    "BCHN": "BCH",
    "DAT": "DATA",
    "DSH": "DASH",
    "ETH2X": "ETH2",
    "EUS": "EURS",
    "EUT": "EURT",
    "GTX": "GT",
    "IDX": "ID",
    "IOT": "IOTA",
    "IQX": "IQ",
    "LIFIII": "LIF3",
    "LUNA": "LUNC",
    "LUNA2": "LUNA",
    "MNA": "MANA",
    "OMN": "OMNI",
    "OPX": "OP",
    "PAS": "PASS",
    "QSH": "QASH",
    "QTM": "QTUM",
    "RBT": "RBTC",
    "STJ": "STORJ",
    "TERRAUST": "USTC",
    "TSD": "TUSD",
    "UDC": "USDC",
    "UST": "USDT",
    "VSY": "VSYS",
    "WAX": "WAXP",
    "WBT": "WBTC",
    "WHBT": "WBT",
    "XCH": "XCHF",
    "YGG": "YEED",
}

# Fiat currencies Bitfinex trades or has traded against (`pub:list:currency:fiat`
# plus JPY, TRY and offshore CNH): a pair or perpetual on one is FX.
_BITFINEX_FIAT = frozenset(
    {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "HKD", "SGD", "THB", "TWD", "TRY", "CNH", "AUD"}
)


def _bitfinex_currency(code: str) -> str:
    return _BITFINEX_ALIASES.get(code, code)


def _bitfinex_paper(sid: str) -> bool:
    return sid.startswith("TEST") or ":TEST" in sid


def _bitfinex_spot_codes(exchange: str, sid: str) -> tuple[str, str]:
    """Bitfinex's own codes of a spot pair: `BTCUSD` or `AAVE:USD`."""
    if _bitfinex_paper(sid):
        raise SkipSymbol(f"{exchange}: paper-trading pair {sid!r} skipped")
    if ":" in sid:
        base, _, quote = sid.partition(":")
    elif len(sid) == 6:
        base, quote = sid[:3], sid[3:]
    else:
        raise SkipSymbol(f"{exchange}: cannot split {sid!r}")
    if not base or not quote or ":" in quote:
        raise SkipSymbol(f"{exchange}: cannot split {sid!r}")
    return base, quote


def _bitfinex_perp_codes(exchange: str, sid: str) -> tuple[str, str]:
    """Bitfinex's codes of a perpetual `BTCF0:USTF0`, without the `F0`."""
    if _bitfinex_paper(sid):
        raise SkipSymbol(f"{exchange}: paper-trading perpetual {sid!r} skipped")
    match = re.fullmatch(r"([A-Z0-9]+)F0:([A-Z0-9]+)F0", sid)
    if match is None:
        raise SkipSymbol(f"{exchange}: cannot parse perpetual {sid!r}")
    return match.group(1), match.group(2)


def _bitfinex_spot_contract(exchange: str, sd: SymbolData) -> Contract:
    base, quote = _bitfinex_spot_codes(exchange, sd["id"])
    return make_contract(
        exchange, sd, _bitfinex_currency(base), _bitfinex_currency(quote), None, ContractType.spot
    )


def _bitfinex_perp_contract(exchange: str, sd: SymbolData) -> Contract:
    base, settle = _bitfinex_perp_codes(exchange, sd["id"])
    settle = _bitfinex_currency(settle)
    shape = {"contract_size": 1.0, "quantity_unit": "base", **sd}
    return make_contract(
        exchange, shape, _bitfinex_currency(base), settle, settle, ContractType.perpetual
    )


def parse_bitfinex_api(exchange: str, sd: SymbolData) -> Contract:
    """`bitfinex-spot`: API legs first; Tardis-only rows parse the pair id."""
    return parse_recorded_legs(exchange, sd) or _bitfinex_spot_contract(exchange, sd)


def parse_bitfinex_perpetual_api(exchange: str, sd: SymbolData) -> Contract:
    """`bitfinex-perps`: API legs first; Tardis-only rows parse `BASEF0:SETTLEF0`."""
    return parse_recorded_legs(exchange, sd) or _bitfinex_perp_contract(exchange, sd)


def _conf_list(payload: Any, key: str, *, allow_empty: bool = False) -> list:
    """The single list a `conf/<key>` request answers, as `[[...]]`.

    Bitfinex answers an empty list as `[null]` and an error as
    `["error", code, message]`; anything but the expected shape raises.
    """
    if not isinstance(payload, list) or len(payload) != 1:
        raise ValueError(f"Bitfinex {key}: unexpected payload {str(payload)[:200]}")
    (items,) = payload
    if items is None and allow_empty:
        return []
    if not isinstance(items, list) or not (items or allow_empty):
        raise ValueError(f"Bitfinex {key}: unexpected payload {str(payload)[:200]}")
    return items


def _conf(key: str, timeout_seconds: int, *, allow_empty: bool = False) -> list:
    return _conf_list(get_json(f"{_BITFINEX_CONF}/{key}", timeout_seconds), key, allow_empty=allow_empty)


def _pair_ids(key: str, timeout_seconds: int) -> list[str]:
    pairs = _conf(key, timeout_seconds)
    if not all(isinstance(pid, str) and pid for pid in pairs):
        raise ValueError(f"Bitfinex {key}: expected a list of pair names")
    return pairs


def _listing_times(key: str, timeout_seconds: int) -> dict[str, Any]:
    """Pair -> creation time (epoch ms) from `pub:info:pair[:futures]`.

    Each entry is `[pair, [created_ms, null, null, min_size, max_size, ...]]`.
    Bitfinex documents the leading field only as a placeholder, but it is the
    pair's creation time (it matches Tardis's `availableSince` for every pair
    Tardis has seen listed); `null` on some securities pairs.
    """
    times = {}
    for entry in _conf(key, timeout_seconds):
        if not (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], list)):
            raise ValueError(f"Bitfinex {key}: unexpected entry {str(entry)[:200]}")
        times[entry[0]] = entry[1][0] if entry[1] else None
    return times


def _bitfinex_spot_underlying(sid: str, base: str, securities: set[str]) -> UnderlyingType:
    # `pub:list:pair:securities` are Bitfinex Securities' tokenized notes,
    # bonds and fund shares (BMN2, ALT2612, USTBL, TITAN1, ...); Bitfinex does
    # not say which kind, so they are unknown. Every other pair is a currency
    # pair: FX when the base is fiat (EURUST), crypto otherwise (Bitfinex lists
    # tokenized gold XAUt as an ordinary currency).
    if sid in securities:
        return UnderlyingType.unknown
    if base in _BITFINEX_FIAT:
        return UnderlyingType.forex
    return UnderlyingType.crypto


def fetch_bitfinex_spot(timeout_seconds: int) -> list[dict]:
    pairs = _pair_ids("pub:list:pair:exchange", timeout_seconds)
    listed = _listing_times("pub:info:pair", timeout_seconds)
    securities = set(_conf("pub:list:pair:securities", timeout_seconds, allow_empty=True))
    rows = []
    for sid in pairs:
        if _bitfinex_paper(sid):
            continue
        base, quote = (_bitfinex_currency(code) for code in _bitfinex_spot_codes("bitfinex-spot", sid))
        sd = {
            "id": sid,
            "type": "spot",
            "base_coin": base,
            "quote_coin": quote,
            "underlying": _bitfinex_spot_underlying(sid, base, securities).value,
            "first_capture": ms_to_utc_timestamp(listed.get(sid)),
        }
        rows.append({k: v for k, v in sd.items() if v is not None})
    return rows


# Bitfinex publishes no underlying class for its perpetuals; TradFi ones are
# recognisable by the venue's naming: equity indices end in `<digits>IX`
# (`JAPAN225IXF0`), BVIV/EVIV are its BTC/ETH implied-volatility indices and
# BTCDOM was its bitcoin-dominance index, XAG/XPT/XPD and UKOIL (Brent) are
# commodities, a fiat base (EURF0) is FX. Everything else is a crypto asset;
# XAUt (tokenized gold) is crypto as on Bitfinex spot. A new TradFi perpetual
# outside these patterns would be read as crypto: extend the table.
_BITFINEX_PERP_CLASSES = {
    "BVIV": UnderlyingType.index,
    "EVIV": UnderlyingType.index,
    "BTCDOM": UnderlyingType.index,
    "XAG": UnderlyingType.commodity,
    "XPT": UnderlyingType.commodity,
    "XPD": UnderlyingType.commodity,
    "XAU": UnderlyingType.commodity,
    "UKOIL": UnderlyingType.commodity,
    "USOIL": UnderlyingType.commodity,
}


def _bitfinex_perp_underlying(base: str) -> UnderlyingType:
    if base in _BITFINEX_PERP_CLASSES:
        return _BITFINEX_PERP_CLASSES[base]
    if re.fullmatch(r"[A-Z]+\d+IX", base):
        return UnderlyingType.index
    if base in _BITFINEX_FIAT:
        return UnderlyingType.forex
    return UnderlyingType.crypto


def fetch_bitfinex_perpetual(timeout_seconds: int) -> list[dict]:
    """Bitfinex perpetuals; each settles in its quote collateral currency.

    The quote of `BASEF0:QUOTEF0` is the collateral: USTF0 (USDt) or BTCF0
    (`ETHF0:BTCF0` is priced and margined in BTC). Quantities are base units.
    """
    pairs = _pair_ids("pub:list:pair:futures", timeout_seconds)
    listed = _listing_times("pub:info:pair:futures", timeout_seconds)
    rows = []
    for sid in pairs:
        if _bitfinex_paper(sid):
            continue
        base_code, settle_code = _bitfinex_perp_codes("bitfinex-perps", sid)
        base, settle = _bitfinex_currency(base_code), _bitfinex_currency(settle_code)
        sd = {
            "id": sid,
            "type": "perpetual",
            "base_coin": base,
            "quote_coin": settle,
            "margin_asset": settle,
            "contract_size": 1.0,
            "quantity_unit": "base",
            "underlying": _bitfinex_perp_underlying(base).value,
            "first_capture": ms_to_utc_timestamp(listed.get(sid)),
        }
        rows.append({k: v for k, v in sd.items() if v is not None})
    return rows
