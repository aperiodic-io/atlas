from __future__ import annotations

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import SkipSymbol, instrument_type, make_contract, resolve_margin, split_concat
from .fields import parse_recorded_legs
from .http import get_json


def parse_bitstamp(exchange: str, sd: SymbolData) -> Contract:
    sid = sd["id"].upper().replace("_", "")
    quotes = ["USD", "EUR", "BTC", "ETH", "USDT", "GBP", "PAX", "USDC", "USDP"]
    pair = split_concat(sid, quotes)
    if pair:
        symbol, denominator = pair
        ctype = instrument_type(sd)
        return make_contract(
            exchange,
            sd,
            symbol,
            denominator,
            resolve_margin(symbol, denominator, ctype),
            ctype,
        )
    raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r}")


# --- Exchange API (`bitstamp-spot`, `bitstamp-perps`) -----------------------
#
# `/api/v2/markets/` lists every market Bitstamp currently has, spot and
# perpetual (`market_type` SPOT / PERPETUAL), with `market_symbol` equal to the
# Tardis id (`btcusd`, `btcusd-perp`). Its `trading` status is `Enabled` or
# `Disabled`; a disabled market is halted, not delisted (Bitstamp drops
# delisted markets from the list), so both stay listed and no row gets an
# `end_date` from the venue. Bitstamp publishes no listing time and has no
# pre-launch state.
_BITSTAMP_API = "https://www.bitstamp.net/api/v2"

# Quote assets for Tardis-only rows, longest first so `btceurcv` is BTC/EURCV,
# `eurceur` EURC/EUR and `ethrlusd` ETH/RLUSD.
_BITSTAMP_QUOTES = [
    "EURCV",
    "RLUSD",
    "PYUSD",
    "USDT",
    "USDC",
    "USDP",
    "EURC",
    "USD",
    "EUR",
    "GBP",
    "BTC",
    "ETH",
    "PAX",
]

# Perpetual `asset_class` values; ETFs (QQQ, EWY) are equity.
_BITSTAMP_ASSET_CLASSES = {
    "CRYPTO": UnderlyingType.crypto,
    "COMMODITIES": UnderlyingType.commodity,
    "ETF": UnderlyingType.equity,
    "EQUITY": UnderlyingType.equity,
    "EQUITIES": UnderlyingType.equity,
    "FX": UnderlyingType.forex,
    "INDEX": UnderlyingType.index,
    "INDICES": UnderlyingType.index,
}

# `/currencies/` `type` of a spot market's base asset.
_BITSTAMP_CURRENCY_TYPES = {"crypto": UnderlyingType.crypto, "fiat": UnderlyingType.forex}

_PERP_SUFFIX = "-perp"


def parse_bitstamp_api(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; Tardis-only rows are `btcusd` or `btcusd-perp`.

    Bitstamp perpetuals are linear, USD-settled and quantity is in base units.
    """
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    sid = sd["id"]
    perpetual = sid.endswith(_PERP_SUFFIX)
    pair = split_concat(sid.removesuffix(_PERP_SUFFIX).upper(), _BITSTAMP_QUOTES)
    if pair is None:
        raise SkipSymbol(f"{exchange}: cannot split {sid!r}")
    base, quote = pair
    if perpetual:
        shape = {"contract_size": 1.0, "quantity_unit": "base", **sd}
        return make_contract(exchange, shape, base, quote, quote, ContractType.perpetual)
    return make_contract(exchange, sd, base, quote, None, ContractType.spot)


def _bitstamp_list(path: str, timeout_seconds: int) -> list[dict]:
    payload = get_json(f"{_BITSTAMP_API}/{path}/", timeout_seconds)
    if not isinstance(payload, list) or not payload or not all(isinstance(x, dict) for x in payload):
        raise ValueError(f"Bitstamp {path}: unexpected payload {str(payload)[:200]}")
    return payload


def _markets(market_type: str, timeout_seconds: int) -> list[dict]:
    return [m for m in _bitstamp_list("markets", timeout_seconds) if m.get("market_type") == market_type]


def fetch_bitstamp_spot(timeout_seconds: int) -> list[dict]:
    markets = _markets("SPOT", timeout_seconds)
    currency_types = {
        c.get("currency"): c.get("type") for c in _bitstamp_list("currencies", timeout_seconds)
    }
    rows = []
    for market in markets:
        base = market["base_currency"]
        underlying = _BITSTAMP_CURRENCY_TYPES.get(currency_types.get(base) or "", UnderlyingType.unknown)
        rows.append(
            {
                "id": market["market_symbol"],
                "type": "spot",
                "base_coin": base,
                "quote_coin": market["counter_currency"],
                "underlying": underlying.value,
            }
        )
    return rows


def fetch_bitstamp_perpetual(timeout_seconds: int) -> list[dict]:
    rows = []
    for market in _markets("PERPETUAL", timeout_seconds):
        if market.get("payoff_type") != "Linear":
            raise ValueError(
                f"Bitstamp {market['market_symbol']}: unexpected payoff_type {market.get('payoff_type')!r}"
            )
        quote = market["counter_currency"]
        rows.append(
            {
                "id": market["market_symbol"],
                "type": "perpetual",
                "base_coin": market["base_currency"],
                "quote_coin": quote,
                # Linear contracts priced in and settled to the USD counter currency.
                "margin_asset": quote,
                "contract_size": float(market.get("contract_size") or 1.0),
                "quantity_unit": "base",
                "underlying": _BITSTAMP_ASSET_CLASSES.get(
                    market.get("asset_class") or "", UnderlyingType.unknown
                ).value,
            }
        )
    return rows
