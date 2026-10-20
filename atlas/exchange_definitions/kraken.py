from __future__ import annotations

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import (
    SkipSymbol,
    delisted_end_date,
    instrument_type,
    make_contract,
    resolve_margin,
    split_concat,
)
from .fields import parse_recorded_legs
from .http import get_json

KRAKEN_ASSET_MAP = {
    "XXBT": "BTC",
    "XBT": "BTC",
    "XETH": "ETH",
    "XXLM": "XLM",
    "XXRP": "XRP",
    "XLTC": "LTC",
    "XXDG": "DOGE",
    "ZUSD": "USD",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZCAD": "CAD",
    "ZJPY": "JPY",
    "ZAUD": "AUD",
    "ZCHF": "CHF",
}


def _norm_kraken(asset: str) -> str:
    if asset in KRAKEN_ASSET_MAP:
        return KRAKEN_ASSET_MAP[asset]
    if len(asset) == 4 and asset[0] in "XZ":
        return asset[1:]
    return asset


def parse_kraken(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"]
    ctype = instrument_type(sd)

    if "/" in sid:
        symbol = _norm_kraken(sid.split("/", 1)[0])
        denominator = _norm_kraken(sid.split("/", 1)[1])
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            resolve_margin(symbol, denominator, ctype),
            ctype,
        )

    if len(sid) == 8 and sid[0] in "XZ" and sid[4] in "XZ":
        symbol = _norm_kraken(sid[:4])
        denominator = _norm_kraken(sid[4:])
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            resolve_margin(symbol, denominator, ctype),
            ctype,
        )

    quotes = [
        "USDT",
        "USDC",
        "DAI",
        "USD",
        "EUR",
        "GBP",
        "AUD",
        "JPY",
        "CAD",
        "BTC",
        "ETH",
    ]
    pair = split_concat(sid, quotes)
    if pair:
        symbol = _norm_kraken(pair[0])
        denominator = _norm_kraken(pair[1])
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            resolve_margin(symbol, denominator, ctype),
            ctype,
        )

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


# --- Exchange API (kraken-spot) ----------------------------------------------
#
# Tardis records Kraken spot from WebSocket v2 since 2026-07-10 (v1 before), so
# current Tardis ids are v2 symbols: `BTC/USD`, `DOGE/EUR`, `AAPLx/USD`. REST
# `AssetPairs` names a pair by its v1 `wsname` (`XBT/USD`, `XDG/EUR`); v2 renames
# only XBT -> BTC and XDG -> DOGE. v2 also lists every xStock twice, under its
# `x` token name (`AAPLx/USD`) and under its `SPV` pair key (`AAPLSPV/USD`),
# both trading the `AAPLx` token; REST has a pair entry for each name.
# Derived this way the REST universe equals the v2 `instrument` channel's
# (1815 pairs on 2026-10-20, identical statuses).
#
# Tardis-only history also has v1 ids (`XBT/USD`, `XDG/XBT`) and, for two days
# in 2026-04/05, copies of 19 pairs on Kraken's `BTNL` execution venue
# (`XBT/USD:BTNL`): another order book of the same instrument, so the same
# internal_id.

_ASSET_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"
_V2_ASSETS = {"XBT": "BTC", "XDG": "DOGE"}
_TOKENIZED = "tokenized_asset"
_SPV = "SPV"

# Kraken's `aclass` puts crypto and fiat money alike in `currency`, so fiat is
# recognised by ISO code: the fiat currencies Kraken's Assets endpoint lists.
_FIAT = frozenset(
    {
        "AED", "ARS", "AUD", "CAD", "CHF", "CLP", "COP", "DKK", "EUR", "GBP", "GEL", "GHS",
        "JPY", "LKR", "MXN", "PLN", "SEK", "UGX", "USD", "VND", "XOF",
    }
)

# `AssetPairs` `status`: `online`; `cancel_only`, `post_only`, `limit_only`,
# `reduce_only` and `maintenance` are temporary restrictions (Kraken also moves
# pairs it is winding down to `cancel_only`), all still listed. `delisted`
# ends a pair at first sighting (Kraken publishes no delisting time);
# `work_in_progress` pairs are being set up and are not listed yet. Delisted
# pairs usually just disappear from the endpoint.
_DELISTED = "delisted"
_NOT_YET_LISTED = "work_in_progress"


def _v2_asset(code: str) -> str:
    return _V2_ASSETS.get(code, code)


def kraken_v2_symbol(key: str, pair: dict) -> str:
    """The WebSocket v2 symbol (the Tardis id) of a REST `AssetPairs` entry."""
    base, quote = pair["wsname"].split("/")
    if pair.get("aclass_base") == _TOKENIZED and key.endswith(quote):
        # `AAPLxUSD` -> `AAPLx`, `AAPLSPVUSD` -> `AAPLSPV`.
        base = key[: -len(quote)]
    return f"{_v2_asset(base)}/{_v2_asset(quote)}"


def _spot_underlying(pair: dict, base: str) -> UnderlyingType:
    aclass = pair.get("aclass_base")
    if aclass == _TOKENIZED:
        # Kraken's tokenized assets are xStocks: tokenized US stocks and ETFs.
        return UnderlyingType.equity
    if aclass == "currency":
        return UnderlyingType.forex if base in _FIAT else UnderlyingType.crypto
    return UnderlyingType.unknown


def _kraken_spot_symbol(key: str, pair: dict) -> dict:
    sid = kraken_v2_symbol(key, pair)
    base, quote = sid.split("/")
    if pair.get("aclass_base") == _TOKENIZED:
        base = pair["base"]  # both names of an xStock trade the `x` token
    sd = {
        "id": sid,
        "type": "spot",
        "base_coin": base,
        "quote_coin": quote,
        "underlying": _spot_underlying(pair, base).value,
    }
    if pair.get("status") == _DELISTED:
        sd["end_date"] = delisted_end_date()
    return sd


def _asset_pairs(timeout_seconds: int, params: dict | None) -> dict:
    payload = get_json(_ASSET_PAIRS_URL, timeout_seconds, params=params)
    if not isinstance(payload, dict) or payload.get("error"):
        error = payload.get("error") if isinstance(payload, dict) else type(payload).__name__
        raise ValueError(f"Kraken AssetPairs {params}: unexpected response {error!r}")
    result = payload.get("result")
    if not isinstance(result, dict) or not result:
        raise ValueError(f"Kraken AssetPairs {params}: expected a non-empty result object")
    return result


def fetch_kraken_spot(timeout_seconds: int) -> list[dict]:
    """Every Kraken spot pair: crypto and fiat pairs, then tokenized (xStock) pairs.

    `AssetPairs` without `aclass_base` lists only `currency` pairs.
    """
    rows: dict[str, dict] = {}
    for params in (None, {"aclass_base": _TOKENIZED}):
        for key, pair in _asset_pairs(timeout_seconds, params).items():
            if pair.get("status") == _NOT_YET_LISTED:
                continue
            sd = _kraken_spot_symbol(key, pair)
            rows.setdefault(sd["id"], sd)
    return list(rows.values())


def _spot_id_asset(code: str) -> str:
    code = _v2_asset(code)
    # v2's `AAPLSPV/USD` trades the `AAPLx` token.
    if code.endswith(_SPV) and len(code) > len(_SPV):
        return code[: -len(_SPV)] + "x"
    return code


def parse_kraken_spot(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; Tardis-only rows are `BASE/QUOTE[:VENUE]` (v1 or v2 names)."""
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    pair = sd["id"].split(":", 1)[0]
    parts = pair.split("/")
    if len(parts) != 2 or not all(parts):
        raise SkipSymbol(f"{exchange}: expected BASE/QUOTE in {sd['id']!r}")
    base, quote = (_spot_id_asset(part) for part in parts)
    return make_contract(exchange, sd, base, quote, None, ContractType.spot)
