from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from .contracts import Contract, ContractType


class SkipSymbol(Exception):
    """Raised by a parser when a symbol is intentionally skipped or unrecognised."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Known quote currencies ordered longest-first to avoid ambiguous splits
# e.g. USDT before USD so BTCUSDT splits as BTC+USDT not BTCUS+T
_KNOWN_QUOTES = [
    "USDT",
    "BUSD",
    "USDC",
    "TUSD",
    "BIDR",
    "BVND",
    "IDRT",
    "FDUSD",
    "USDP",
    "BTC",
    "ETH",
    "BNB",
    "USD",
    "EUR",
    "GBP",
    "AUD",
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

# CME month codes (BitMEX, Kraken futures)
_CME_MONTHS = {
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

# Map the Tardis "type" field directly to ContractType
_TYPE_MAP: dict[str, ContractType] = {
    "spot": ContractType.spot,
    "perpetual": ContractType.perpetual,
    "future": ContractType.future,
    "option": ContractType.option,
}


def _contract_type(sd: dict) -> ContractType:
    return _TYPE_MAP.get(sd.get("type", ""), ContractType.unknown)


def _split_concat(
    symbol: str,
    quotes: list[str] = _KNOWN_QUOTES,
) -> tuple[str, str] | None:
    """Split a concatenated pair like BTCUSDT → (BTC, USDT)."""
    for q in quotes:
        if symbol.endswith(q):
            base = symbol[: -len(q)]
            if base:
                return base, q
    return None


def _parse_yymmdd(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%y%m%d")
    except ValueError:
        return None


def _parse_yyyymmdd(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return None


def _parse_ddmmmyy(s: str) -> datetime | None:
    """Parse 28MAR25 → datetime(2025, 3, 28)."""
    try:
        return datetime.strptime(s.upper(), "%d%b%y")
    except ValueError:
        return None


def _parse_cme_month_year(month_code: str, year_suffix: str) -> datetime | None:
    month = _CME_MONTHS.get(month_code.upper())
    if month is None:
        return None
    try:
        return datetime(2000 + int(year_suffix), month, 1)
    except ValueError:
        return None


def _resolve_margin(sym: str, denom: str, ct: ContractType) -> str | None:
    """
    For spot contracts margin is None (no leveraged margin asset).
    For derivatives, USD/EUR/etc. denominator means inverse (coin-margined);
    stablecoin or fiat non-USD denominators mean linear.
    """
    if ct == ContractType.spot:
        return None
    _LINEAR_DENOMS = {
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
    return denom if denom in _LINEAR_DENOMS else sym


def _make(
    exchange: str,
    sd: dict,
    sym: str,
    denom: str,
    margin: str | None,
    contract_type: ContractType,
    delivery_date: datetime | None = None,
    contract_size: float = 1.0,
) -> Contract:
    return Contract(
        exchange=exchange,
        original_id=sd["id"],
        symbol=sym.upper(),
        denominator=denom.upper(),
        margin=margin.upper() if margin is not None else None,
        delivery_date=delivery_date,
        contract_type=contract_type,
        contract_size=contract_size,
    )


# ---------------------------------------------------------------------------
# Per-exchange parsers
# All use sd["type"] for contract_type and parse only sym/denom/delivery from id.
# ---------------------------------------------------------------------------


def _parse_concat(
    exchange: str, sd: dict, quotes: list[str] = _KNOWN_QUOTES
) -> Contract:
    """Generic parser for exchanges with concatenated pairs: BTCUSDT → BTC/USDT."""
    pair = _split_concat(sd["id"].upper(), quotes)
    if pair is None:
        raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r} against known quotes")
    sym, denom = pair
    margin = _resolve_margin(sym, denom, _contract_type(sd))
    return _make(exchange, sd, sym, denom, margin, _contract_type(sd))


def _parse_dash(exchange: str, sd: dict) -> Contract:
    """Generic parser for dash-separated pairs: BTC-USD → BTC/USD."""
    parts = sd["id"].split("-")
    if len(parts) == 2:
        sym, denom = parts
        margin = _resolve_margin(sym, denom, _contract_type(sd))
        return _make(exchange, sd, sym, denom, margin, _contract_type(sd))
    raise SkipSymbol(f"{exchange}: expected 2 dash-separated parts in {sd['id']!r}")


def _parse_underscore_spot(exchange: str, sd: dict) -> Contract:
    """Generic parser for underscore-separated spot pairs: BTC_USDT → BTC/USDT."""
    parts = sd["id"].split("_")
    if len(parts) == 2:
        sym, denom = parts
        return _make(exchange, sd, sym, denom, None, ContractType.spot)
    raise SkipSymbol(
        f"{exchange}: expected 2 underscore-separated parts in {sd['id']!r}"
    )


# --- binance ---


def _parse_binance(exchange: str, sd: dict) -> Contract:
    return _parse_concat(exchange, sd)


def _parse_binance_futures(exchange: str, sd: dict) -> Contract:
    """
    BTCUSDT        → linear perp/future (USDT-margined)
    BTCUSD_PERP    → inverse perp (coin-margined)
    BTCUSD_250328  → inverse future (coin-margined, YYMMDD suffix)
    BTCUSDT250328  → linear future
    """
    sid = sd["id"].upper()
    ct = _contract_type(sd)

    if "_" in sid:
        base_str, suffix = sid.rsplit("_", 1)
        pair = _split_concat(base_str, ["USD", "BUSD", "USDT", "USDC"])
        sym, denom = pair if pair else (base_str, "USD")
        margin = _resolve_margin(sym, denom, _contract_type(sd))
        delivery = None if suffix == "PERP" else _parse_yymmdd(suffix)
        return _make(exchange, sd, sym, denom, margin, ct, delivery)

    # Letters followed by 6 digits: BTCUSDT250328
    m = re.match(r"^([A-Z]+)(\d{6})$", sid)
    if m:
        pair = _split_concat(m.group(1))
        if pair:
            sym, denom = pair
            margin = _resolve_margin(sym, denom, _contract_type(sd))
            return _make(
                exchange, sd, sym, denom, margin, ct, _parse_yymmdd(m.group(2))
            )

    return _parse_concat(exchange, sd)


def _parse_binance_delivery(exchange: str, sd: dict) -> Contract:
    """BTCUSD_250328 → BTC/USD coin-margined future."""
    sid = sd["id"].upper()
    if "_" not in sid:
        raise SkipSymbol(f"{exchange}: expected underscore in {sid!r}")
    base_str, suffix = sid.rsplit("_", 1)
    pair = _split_concat(base_str, ["USD", "BUSD", "USDT"])
    sym, denom = pair if pair else (base_str, "USD")
    margin = _resolve_margin(sym, denom, _contract_type(sd))
    ct = _contract_type(sd)
    delivery = None if suffix == "PERP" else _parse_yymmdd(suffix)
    return _make(exchange, sd, sym, denom, margin, ct, delivery)


# --- bitmex ---

_BITMEX_ASSET = {"XBT": "BTC", "XXBT": "BTC"}


def _norm_bitmex(a: str) -> str:
    return _BITMEX_ASSET.get(a, a)


def _parse_bitmex(exchange: str, sd: dict) -> Contract:
    sid = sd["id"]
    ct = _contract_type(sd)

    # Underscore-format spot/perp: XBT_USDT, ETH_XBT, RLUSD_USDT
    if "_" in sid:
        parts = sid.split("_", 1)
        if len(parts) == 2:
            sym = _norm_bitmex(parts[0])
            denom = _norm_bitmex(parts[1])
            margin = _resolve_margin(sym, denom, ct)
            return _make(exchange, sd, sym, denom, margin, ct)
        raise SkipSymbol(f"{exchange}: cannot parse underscore symbol {sid!r}")

    # CME-coded futures: XBTM25, ETHU25 (2+ letters + month code + 2-digit year)
    m = re.match(r"^([A-Z]{2,})([FGHJKMNQUVXZ])(\d{2})$", sid)
    if m:
        raw_base = m.group(1)
        # Try to split base into sym+denom (e.g. XBTUSDT → BTC/USDT)
        pair = _split_concat(raw_base, ["USDT", "USDC", "USD", "EUR", "ETH", "BTC"])
        if pair:
            sym, denom = pair
            sym = _norm_bitmex(sym)
            margin = _resolve_margin(sym, denom, ct)
            delivery = _parse_cme_month_year(m.group(2), m.group(3))
            return _make(exchange, sd, sym, denom, margin, ct, delivery)
        # Simple coin future (XBTM25, ETHU25)
        sym = _norm_bitmex(raw_base)
        delivery = _parse_cme_month_year(m.group(2), m.group(3))
        return _make(exchange, sd, sym, "USD", sym, ct, delivery)

    # Try concat split (handles XBTUSDT, XBTUSD, ETHUSDT, etc.)
    pair = _split_concat(sid, ["USDT", "USDC", "USD", "EUR", "ETH", "BTC"])
    if pair:
        sym, denom = pair
        sym = _norm_bitmex(sym)
        margin = _resolve_margin(sym, denom, _contract_type(sd))
        return _make(exchange, sd, sym, denom, margin, ct)

    raise SkipSymbol(f"{exchange}: cannot parse symbol {sid!r}")


# --- bitfinex ---


def _parse_bitfinex(exchange: str, sd: dict) -> Contract:
    """tBTCUSD, tETHUST (trading pairs), fBTC (funding — skip)."""
    sid = sd["id"]
    if sid.startswith("f"):
        raise SkipSymbol(f"{exchange}: funding symbol {sid!r} skipped")
    if not sid.startswith("t"):
        raise SkipSymbol(f"{exchange}: expected 't' prefix in {sid!r}")
    pair_str = sid[1:]

    ct = _contract_type(sd)
    if ":" in pair_str:
        sym, denom = pair_str.split(":", 1)
        denom = "USDT" if denom == "UST" else denom
        margin = _resolve_margin(sym, denom, ct)
        return _make(exchange, sd, sym, denom, margin, ct)

    _BFX_QUOTES = [
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
    pair = _split_concat(pair_str, _BFX_QUOTES)
    if pair:
        sym, denom = pair
        denom = "USDT" if denom == "UST" else denom
        margin = _resolve_margin(sym, denom, ct)
        return _make(exchange, sd, sym, denom, margin, ct)

    raise SkipSymbol(f"{exchange}: cannot split {sid!r}")


def _parse_bitfinex_derivatives(exchange: str, sd: dict) -> Contract:
    """BTCF0:USTF0 → BTC/USDT perpetual."""
    m = re.match(r"^([A-Z]+)F0:([A-Z]+)F0$", sd["id"])
    if m:
        sym = m.group(1)
        denom = "USDT" if m.group(2) == "UST" else m.group(2)
        return _make(exchange, sd, sym, denom, denom, _contract_type(sd))
    raise SkipSymbol(f"{exchange}: cannot parse derivative {sd['id']!r}")


# --- bitget ---


def _parse_bitget_futures(exchange: str, sd: dict) -> Contract:
    """
    Old format: BTCUSDT_UMCBL (linear), BTCUSD_DMCBL (inverse), BTCUSDT_CMCBL (USDC).
    New format (no suffix): BTCUSDT, BTCUSD, etc.
    """
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
        pair = _split_concat(base_str, quotes)
        if pair:
            sym, denom = pair
            margin = _resolve_margin(sym, denom, _contract_type(sd))
            return _make(exchange, sd, sym, denom, margin, _contract_type(sd))
        raise SkipSymbol(
            f"{exchange}: cannot split {base_str!r} with suffix {suffix!r}"
        )

    # New format: fall through to concat split
    return _parse_concat(exchange, sd)


# --- bitstamp ---


def _parse_bitstamp(exchange: str, sd: dict) -> Contract:
    """btcusd, ethbtc — lowercase, strip underscores."""
    sid = sd["id"].upper().replace("_", "")
    _BTS_QUOTES = ["USD", "EUR", "BTC", "ETH", "USDT", "GBP", "PAX", "USDC", "USDP"]
    pair = _split_concat(sid, _BTS_QUOTES)
    if pair:
        sym, denom = pair
        return _make(
            exchange,
            sd,
            sym,
            denom,
            _resolve_margin(sym, denom, _contract_type(sd)),
            _contract_type(sd),
        )
    raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r}")


# --- bybit ---


def _parse_bybit(exchange: str, sd: dict) -> Contract:
    """
    BTCUSDT / BTCUSD → perp
    BTCUSD-28MAR25 / BTCUSDT-28MAR25 → future
    """
    sid = sd["id"]
    ct = _contract_type(sd)

    if "-" in sid:
        dash_idx = sid.index("-")
        pair = _split_concat(sid[:dash_idx], ["USDT", "USDC", "USD", "BTC", "ETH"])
        delivery = _parse_ddmmmyy(sid[dash_idx + 1 :])
        if pair:
            sym, denom = pair
            margin = _resolve_margin(sym, denom, _contract_type(sd))
            return _make(exchange, sd, sym, denom, margin, ct, delivery)
        raise SkipSymbol(f"{exchange}: cannot split base of {sid!r}")

    pair = _split_concat(sid, ["USDT", "USDC", "USD", "BTC", "ETH"])
    if pair:
        sym, denom = pair
        margin = _resolve_margin(sym, denom, _contract_type(sd))
        return _make(exchange, sd, sym, denom, margin, ct)

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


# --- coinbase ---


def _parse_coinbase(exchange: str, sd: dict) -> Contract:
    return _parse_dash(exchange, sd)


# --- crypto-com ---


def _parse_crypto_com(exchange: str, sd: dict) -> Contract:
    return _parse_underscore_spot(exchange, sd)


# --- cryptofacilities ---


def _parse_cryptofacilities(exchange: str, sd: dict) -> Contract:
    """PI_XBTUSD (perp), FI_XBTUSD_210129 (future)."""
    _CF_ASSET = {"XBT": "BTC"}
    sid = sd["id"]
    parts = sid.split("_")
    if len(parts) < 2:
        raise SkipSymbol(f"{exchange}: expected at least 2 underscore parts in {sid!r}")

    pair = _split_concat(parts[1], ["USD", "EUR", "USDT"])
    if not pair:
        raise SkipSymbol(f"{exchange}: cannot split {parts[1]!r} in {sid!r}")
    sym, denom = pair
    sym = _CF_ASSET.get(sym, sym)
    margin = _resolve_margin(sym, denom, _contract_type(sd))
    ct = _contract_type(sd)
    delivery = _parse_yymmdd(parts[2]) if len(parts) >= 3 else None
    return _make(exchange, sd, sym, denom, margin, ct, delivery)


# --- deribit ---


def _parse_deribit(exchange: str, sd: dict) -> Contract:
    """
    BTC-PERPETUAL, BTC-28MAR25, BTC-28MAR25-50000-C/P (coin-margined, USD denom).
    BTC_USDC-PERPETUAL, BTC_USDC-28MAR25, BTC_USDC-28MAR25-50000-C/P (USDC denom).
    BTC_USDC (spot, underscore only).
    """
    ct = _contract_type(sd)
    if ct == ContractType.unknown:
        raise SkipSymbol(
            f"{exchange}: unsupported contract type {sd.get('type')!r} for {sd['id']!r}"
        )

    sid = sd["id"]

    # Spot with no dash: BTC_USDC, ETH_BTC
    if "-" not in sid:
        parts = sid.split("_", 1)
        if len(parts) == 2:
            sym, denom = parts
            return _make(exchange, sd, sym, denom, None, ct)
        raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")

    dash_parts = sid.split("-")

    # Determine sym and denom from the first segment (may contain underscore)
    first = dash_parts[0]
    if "_" in first:
        sym, denom = first.split("_", 1)
    else:
        sym, denom = first, "USD"

    if dash_parts[1] == "PERPETUAL":
        return _make(exchange, sd, sym, denom, sym, ct)

    delivery = _parse_ddmmmyy(dash_parts[1])
    return _make(exchange, sd, sym, denom, sym, ct, delivery)


# --- ftx ---


def _parse_ftx(exchange: str, sd: dict) -> Contract:
    """BTC/USD (spot), BTC-PERP, BTC-0325 (MMYY future)."""
    sid = sd["id"]
    ct = _contract_type(sd)

    if "/" in sid:
        sym, denom = sid.split("/", 1)
        return _make(exchange, sd, sym, denom, _resolve_margin(sym, denom, ct), ct)

    if sid.endswith("-PERP"):
        sym = sid[:-5]
        return _make(exchange, sd, sym, "USD", sym, ct)

    if "-" in sid:
        sym, date_str = sid.split("-", 1)
        m = re.match(r"^(\d{2})(\d{2})$", date_str)
        if m:
            try:
                delivery = datetime(2000 + int(m.group(2)), int(m.group(1)), 1)
                return _make(exchange, sd, sym, "USD", sym, ct, delivery)
            except ValueError:
                pass

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


# --- gate-io ---


def _parse_gate_io(exchange: str, sd: dict) -> Contract:
    return _parse_underscore_spot(exchange, sd)


def _parse_gate_io_futures(exchange: str, sd: dict) -> Contract:
    """BTC_USDT (perp), BTC_USD (inverse perp), BTC_USDT_20250328 (future)."""
    sid = sd["id"]
    parts = sid.split("_")
    ct = _contract_type(sd)

    if len(parts) == 2:
        sym, denom = parts
        margin = _resolve_margin(sym, denom, _contract_type(sd))
        return _make(exchange, sd, sym, denom, margin, ct)

    if len(parts) == 3:
        sym, denom, date_str = parts
        margin = _resolve_margin(sym, denom, _contract_type(sd))
        return _make(exchange, sd, sym, denom, margin, ct, _parse_yyyymmdd(date_str))

    raise SkipSymbol(f"{exchange}: expected 2 or 3 underscore parts in {sid!r}")


# --- gemini ---


def _parse_gemini(exchange: str, sd: dict) -> Contract:
    """btcusd, ethusd — lowercase concatenated."""
    _GMX_QUOTES = ["USDT", "GUSD", "USDC", "USD", "BTC", "ETH", "DAI"]
    return _parse_concat(exchange, sd, _GMX_QUOTES)


# --- huobi ---


def _parse_huobi(exchange: str, sd: dict) -> Contract:
    """btcusdt, ethbtc — lowercase concatenated."""
    pair = _split_concat(sd["id"].upper())
    if pair:
        sym, denom = pair
        margin = _resolve_margin(sym, denom, _contract_type(sd))
        return _make(exchange, sd, sym, denom, margin, _contract_type(sd))
    raise SkipSymbol(f"{exchange}: cannot split {sd['id']!r}")


_HUOBI_DM_ROLLING = {"CW", "NW", "CQ", "NQ"}


def _parse_huobi_dm(exchange: str, sd: dict) -> Contract:
    """BTC_CW / BTC_NQ (rolling), BTC201225 (dated)."""
    sid = sd["id"]
    ct = _contract_type(sd)

    if "_" in sid:
        sym, suffix = sid.split("_", 1)
        if suffix in _HUOBI_DM_ROLLING:
            return _make(exchange, sd, sym, "USD", sym, ct)
        raise SkipSymbol(f"{exchange}: unknown rolling suffix {suffix!r} in {sid!r}")

    m = re.match(r"^([A-Z]+)(\d{6})$", sid)
    if m:
        return _make(
            exchange, sd, m.group(1), "USD", m.group(1), ct, _parse_yymmdd(m.group(2))
        )

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


def _parse_huobi_dm_swap(exchange: str, sd: dict) -> Contract:
    """BTC-USD (inverse perp)."""
    return _parse_dash(exchange, sd)


def _parse_huobi_dm_linear_swap(exchange: str, sd: dict) -> Contract:
    """BTC-USDT (linear perp)."""
    return _parse_dash(exchange, sd)


# --- hyperliquid ---


def _parse_hyperliquid(exchange: str, sd: dict) -> Contract:
    """BTC, ETH, SOL → perpetual vs USDC. @<n> → skip."""
    sid = sd["id"]
    if sid.startswith("@"):
        raise SkipSymbol(f"{exchange}: index symbol {sid!r} skipped")
    if re.match(r"^[A-Z0-9]+$", sid):
        return _make(exchange, sd, sid, "USDC", "USDC", _contract_type(sd))
    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


# --- kraken ---

_KRAKEN_ASSET_MAP = {
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


def _norm_kraken(a: str) -> str:
    if a in _KRAKEN_ASSET_MAP:
        return _KRAKEN_ASSET_MAP[a]
    if len(a) == 4 and a[0] in "XZ":
        return a[1:]
    return a


def _parse_kraken(exchange: str, sd: dict) -> Contract:
    sid = sd["id"]
    ct = _contract_type(sd)

    if "/" in sid:
        sym, denom = (
            _norm_kraken(sid.split("/", 1)[0]),
            _norm_kraken(sid.split("/", 1)[1]),
        )
        return _make(exchange, sd, sym, denom, _resolve_margin(sym, denom, ct), ct)

    # Legacy 8-char: XXBTZUSD
    if len(sid) == 8 and sid[0] in "XZ" and sid[4] in "XZ":
        sym, denom = _norm_kraken(sid[:4]), _norm_kraken(sid[4:])
        return _make(exchange, sd, sym, denom, _resolve_margin(sym, denom, ct), ct)

    _KRK_QUOTES = [
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
    pair = _split_concat(sid, _KRK_QUOTES)
    if pair:
        sym, denom = _norm_kraken(pair[0]), _norm_kraken(pair[1])
        return _make(exchange, sd, sym, denom, _resolve_margin(sym, denom, ct), ct)

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


# --- kucoin ---


def _parse_kucoin(exchange: str, sd: dict) -> Contract:
    return _parse_dash(exchange, sd)


# --- okex ---


def _parse_okex(exchange: str, sd: dict) -> Contract:
    """BTC-USDT spot."""
    return _parse_dash(exchange, sd)


def _parse_okex_swap(exchange: str, sd: dict) -> Contract:
    """BTC-USDT-SWAP (linear), BTC-USD-SWAP (inverse)."""
    parts = sd["id"].split("-")
    if len(parts) == 3 and parts[2] == "SWAP":
        sym, denom = parts[0], parts[1]
        margin = _resolve_margin(sym, denom, _contract_type(sd))
        return _make(exchange, sd, sym, denom, margin, _contract_type(sd))
    raise SkipSymbol(f"{exchange}: expected 3-part SWAP format in {sd['id']!r}")


def _parse_okex_futures(exchange: str, sd: dict) -> Contract:
    """BTC-USD-250328 (inverse), BTC-USDT-250328 (linear)."""
    parts = sd["id"].split("-")
    if len(parts) != 3:
        raise SkipSymbol(f"{exchange}: expected 3 dash parts in {sd['id']!r}")
    sym, denom, date_str = parts
    delivery = _parse_yymmdd(date_str) or _parse_yyyymmdd(date_str)
    if delivery is None:
        raise SkipSymbol(f"{exchange}: cannot parse date {date_str!r} in {sd['id']!r}")
    margin = _resolve_margin(sym, denom, _contract_type(sd))
    return _make(exchange, sd, sym, denom, margin, _contract_type(sd), delivery)


# --- phemex ---


def _parse_phemex(exchange: str, sd: dict) -> Contract:
    """sBTCUSDT (spot, s prefix), BTCUSD (inverse perp), BTCUSDT (linear perp)."""
    sid = sd["id"]
    ct = _contract_type(sd)

    if sid.startswith("s"):
        pair = _split_concat(sid[1:])
        if pair:
            sym, denom = pair
            return _make(exchange, sd, sym, denom, None, ct)
        raise SkipSymbol(f"{exchange}: cannot split {sid!r} (s-prefixed spot)")

    pair = _split_concat(sid, ["USDT", "USDC", "USD", "BTC", "ETH"])
    if pair:
        sym, denom = pair
        margin = _resolve_margin(sym, denom, _contract_type(sd))
        return _make(exchange, sd, sym, denom, margin, ct)

    raise SkipSymbol(f"{exchange}: cannot parse {sid!r}")


# --- poloniex ---

_POLONIEX_OLD_QUOTES = {"USDT", "BTC", "ETH", "TRX", "BNB", "USDC"}


def _parse_poloniex(exchange: str, sd: dict) -> Contract:
    """BTC_USDT (new base_quote) or USDT_BTC (old quote_base)."""
    parts = sd["id"].split("_")
    if len(parts) != 2:
        raise SkipSymbol(f"{exchange}: expected 2 underscore parts in {sd['id']!r}")
    a, b = parts
    ct = _contract_type(sd)
    if a in _POLONIEX_OLD_QUOTES and b not in _POLONIEX_OLD_QUOTES:
        return _make(exchange, sd, b, a, None, ct)
    return _make(exchange, sd, a, b, None, ct)


# --- upbit ---


def _parse_upbit(exchange: str, sd: dict) -> Contract:
    """KRW-BTC: format is QUOTE-BASE."""
    parts = sd["id"].split("-")
    if len(parts) == 2:
        quote, base = parts
        return _make(exchange, sd, base, quote, None, _contract_type(sd))
    raise SkipSymbol(f"{exchange}: expected 2 dash parts in {sd['id']!r}")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_PARSERS: dict[str, Callable[[str, dict], Contract]] = {
    "binance": _parse_binance,
    "binance-futures": _parse_binance_futures,
    "binance-delivery": _parse_binance_delivery,
    "bitmex": _parse_bitmex,
    "bitfinex": _parse_bitfinex,
    "bitfinex-derivatives": _parse_bitfinex_derivatives,
    "bitget": _parse_concat,
    "bitget-futures": _parse_bitget_futures,
    "bitstamp": _parse_bitstamp,
    "bybit": _parse_bybit,
    "bybit-spot": _parse_concat,
    "coinbase": _parse_coinbase,
    "crypto-com": _parse_crypto_com,
    "cryptofacilities": _parse_cryptofacilities,
    "deribit": _parse_deribit,
    "ftx": _parse_ftx,
    "gate-io": _parse_gate_io,
    "gate-io-futures": _parse_gate_io_futures,
    "gemini": _parse_gemini,
    "huobi": _parse_huobi,
    "huobi-dm": _parse_huobi_dm,
    "huobi-dm-swap": _parse_huobi_dm_swap,
    "huobi-dm-linear-swap": _parse_huobi_dm_linear_swap,
    "hyperliquid": _parse_hyperliquid,
    "kraken": _parse_kraken,
    "kucoin": _parse_kucoin,
    "okex": _parse_okex,
    "okex-swap": _parse_okex_swap,
    "okex-futures": _parse_okex_futures,
    "phemex": _parse_phemex,
    "poloniex": _parse_poloniex,
    "upbit": _parse_upbit,
}


def parse_contract(exchange: str, symbol_data: dict) -> Contract:
    parser = _PARSERS.get(exchange)
    if parser is None:
        raise ValueError(f"Unknown exchange: {exchange}")
    return parser(exchange, symbol_data)
