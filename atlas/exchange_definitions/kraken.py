from __future__ import annotations

from ..contracts import Contract
from ..parser_interface import SymbolData
from .common import SkipSymbol, contract_type, make_contract, resolve_margin, split_concat

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
    ctype = contract_type(sd)

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
