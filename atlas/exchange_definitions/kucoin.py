from __future__ import annotations

from datetime import UTC, datetime

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from ..types import UnderlyingType
from .common import (
    SkipSymbol,
    make_contract,
    ms_to_datetime,
    ms_to_utc_timestamp,
    parse_dash,
    upcoming,
)
from .fields import parse_recorded_legs
from .http import get_json


def parse_kucoin(exchange: str, sd: SymbolData) -> Contract:
    return parse_dash(exchange, sd)


# --- Exchange API (kucoin-spot) ---------------------------------------------
#
# KuCoin spot symbols are `BASE-QUOTE` (`BTC-USDT`), byte-identical to Tardis's
# `kucoin` ids. `/api/v2/symbols` lists only the pairs KuCoin still carries: a
# delisted pair simply disappears (KuCoin publishes no delisted status), so the
# updater's missing-row confirmation closes it.
#
# Lifecycle fields:
# - `tradingStartTime` (epoch ms, null on old pairs) is when continuous trading
#   opens. A pair whose start is still ahead, or that is in its opening call
#   auction (`callauctionIsEnabled`), has not started trading and is excluded.
#   For a relaunched pair (`BSV-USDT`, `GALA-USDT`) it is the relaunch time.
# - `enableTrading: false` is a halt; the pair stays listed.
# - `st` (special treatment, a delisting-risk warning) stays listed.
_SYMBOLS_URL = "https://api.kucoin.com/api/v2/symbols"
_CURRENCIES_URL = "https://api.kucoin.com/api/v3/currencies"
_OK = "200000"

# KuCoin keeps some assets under a code other than their ticker; its currency
# table shows the ticker as `name` (`BCHSV` -> `BSV`, `LOKI` -> `OXEN`). Only
# history uses these codes: KuCoin renamed `BCHSV-USDT` to `BSV-USDT` on
# 2026-09-22, and the other pairs were delisted under their old codes.
_KUCOIN_RENAMES = {"BCHSV": "BSV", "LOKI": "OXEN", "R": "REV", "WAX": "WAXP"}

# `market` is the trading zone a pair is filed under (`/api/v1/markets`).
# `Stocks` holds tokenized equities (xStocks); `RWA` mixes tokenized assets
# without saying which kind, so it is `unknown`. Every other zone is a crypto
# theme or quote zone. A zone Atlas does not know is `unknown`.
_CRYPTO_MARKETS = {
    "ALTS", "AI", "BTC", "DeFi", "DePIN", "FIAT", "GameFi", "KCS", "Layer1", "Layer2",
    "Majors", "Meme", "New", "NFT", "Payment", "PoW", "Solana", "Storage", "TON",
    "USDS", "VR-AR",
}
_MARKET_CLASSES = {
    **dict.fromkeys(_CRYPTO_MARKETS, UnderlyingType.crypto),
    "Stocks": UnderlyingType.equity,
    "RWA": UnderlyingType.unknown,
}


def kucoin_asset(code: str) -> str:
    return _KUCOIN_RENAMES.get(code.upper(), code.upper())


def _kucoin_data(url: str, timeout_seconds: int) -> list[dict]:
    payload = get_json(url, timeout_seconds)
    if not isinstance(payload, dict) or payload.get("code") != _OK:
        raise ValueError(f"KuCoin {url}: unexpected response {str(payload)[:200]!r}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError(f"KuCoin {url}: expected a data list, got {type(data).__name__}")
    return data


def _kucoin_underlying(item: dict, full_names: dict[str, str]) -> UnderlyingType:
    # Tokenized stocks are sometimes filed outside `Stocks` (`SPCXX-USDT`, SpaceX
    # xStock, sits in `USDS`); KuCoin's currency name still says what they are.
    if full_names.get(item["baseCurrency"], "").lower().endswith("xstock"):
        return UnderlyingType.equity
    return _MARKET_CLASSES.get(item.get("market") or "", UnderlyingType.unknown)


def _kucoin_not_yet_trading(item: dict, now: datetime) -> bool:
    if item.get("callauctionIsEnabled"):
        return True
    start = ms_to_datetime(item.get("tradingStartTime"))
    return start is not None and start > now


def _kucoin_symbol(item: dict, full_names: dict[str, str]) -> dict:
    sd = {
        "id": item["symbol"],
        "type": "spot",
        "base_coin": kucoin_asset(item["baseCurrency"]),
        "quote_coin": kucoin_asset(item["quoteCurrency"]),
        "underlying": _kucoin_underlying(item, full_names).value,
        "first_capture": ms_to_utc_timestamp(item.get("tradingStartTime")),
    }
    return {k: v for k, v in sd.items() if v is not None}


def fetch_kucoin_spot(timeout_seconds: int) -> list[dict]:
    items = _kucoin_data(_SYMBOLS_URL, timeout_seconds)
    full_names = {
        str(c.get("currency")): str(c.get("fullName") or "")
        for c in _kucoin_data(_CURRENCIES_URL, timeout_seconds)
    }
    now = datetime.now(UTC)
    return [
        upcoming(sd) if _kucoin_not_yet_trading(item, now) else sd
        for item in items
        for sd in [_kucoin_symbol(item, full_names)]
    ]


def parse_kucoin_spot(exchange: str, sd: SymbolData) -> Contract:
    """API legs first; Tardis-only rows are `BASE-QUOTE` under KuCoin's codes."""
    contract = parse_recorded_legs(exchange, sd)
    if contract is not None:
        return contract
    parts = sd["id"].split("-")
    if len(parts) != 2 or not all(parts):
        raise SkipSymbol(f"{exchange}: expected BASE-QUOTE, got {sd['id']!r}")
    base, quote = (kucoin_asset(p) for p in parts)
    return make_contract(exchange, sd, base, quote, None, ContractType.spot)
