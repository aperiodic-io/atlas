"""Hyperliquid identity and price evidence for the shared CMC mapping workflow.

Spot API indices are joined through metadata, never guessed from tickers. Wrapped
and staked assets retain their own ticker. Builder aliases are explicit because
HIP-3 names such as STX and GAS can represent equities and commodities.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

import requests

from integrations.cmc_id_probe import PriceObservation, normalize_cmc_lookup_symbol
from integrations.http_retry import retry_delay_seconds
from integrations.cmc_category_metadata import CMC_DETAIL_URL


INFO_URL = "https://api.hyperliquid.xyz/info"
CMC_MARKETS_URL = (
    "https://api.coinmarketcap.com/data-api/v3/exchange/market-pairs/latest"
)
CMC_EXCHANGE_ID = 8112
# Hyperliquid's k-prefixed contracts represent 1,000 underlying tokens. This is
# an explicit list, not a general rule that would corrupt KHYPE (staked HYPE).
THOUSAND_CONTRACTS = frozenset(
    {"KBONK", "KDOGS", "KFLOKI", "KLUNC", "KNEIRO", "KPEPE", "KSHIB"}
)
# Reviewed crypto contracts on builder dexes. Other names remain namespaced,
# including STX (Seagate), GAS (natural gas), GOLD, indices and equities.
BUILDER_CRYPTO_ASSETS = {
    "CASH": frozenset({"BTC", "ETH"}),
    "FLX": frozenset({"BTC", "XMR", "USDE"}),
    "HYNA": frozenset(
        {
            "1000PEPE",
            "ADA",
            "BASED",
            "BCH",
            "BNB",
            "BTC",
            "DOGE",
            "ENA",
            "ETH",
            "FARTCOIN",
            "HYPE",
            "IP",
            "LIGHTER",
            "LINK",
            "LIT",
            "LTC",
            "PUMP",
            "SOL",
            "SUI",
            "XMR",
            "XPL",
            "XRP",
            "ZEC",
        }
    ),
}


class HyperliquidError(RuntimeError):
    """Hyperliquid did not return a usable metadata/context response."""


def contract_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if ":" in symbol:
        dex, asset = symbol.split(":", 1)
        if asset not in BUILDER_CRYPTO_ASSETS.get(dex, ()):
            return symbol
        symbol = "LIT" if asset == "LIGHTER" else asset
    if symbol in THOUSAND_CONTRACTS:
        return "1000" + symbol[1:]
    return symbol


def lookup_symbol(symbol: str, cmc_symbols: set[str]) -> str:
    normalized = contract_symbol(symbol)
    # Unknown namespaces must never be folded onto a CMC crypto ticker.
    if ":" in normalized:
        return normalized
    return normalize_cmc_lookup_symbol(normalized, cmc_symbols)


def contract_multiplier(symbol: str) -> int:
    normalized = contract_symbol(symbol)
    return (
        1000 if symbol.upper() in THOUSAND_CONTRACTS or normalized == "1000PEPE" else 1
    )


def _fetch_contexts(session, request_type, dex, timeout_seconds, max_attempts):
    if timeout_seconds <= 0 or max_attempts <= 0:
        raise ValueError("invalid Hyperliquid request settings")
    body = {"type": request_type}
    if dex is not None:
        body["dex"] = dex
    last_error = None
    for attempt in range(max_attempts):
        try:
            response = session.post(INFO_URL, json=body, timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or len(payload) != 2:
                raise HyperliquidError("expected metadata and asset contexts")
            meta, contexts = payload
            if not isinstance(meta, dict) or not isinstance(contexts, list):
                raise HyperliquidError("invalid metadata/context types")
            universe = meta.get("universe")
            if not isinstance(universe, list) or (
                request_type != "spotMetaAndAssetCtxs"
                and len(universe) != len(contexts)
            ):
                raise HyperliquidError("universe/context length mismatch")
            if not all(isinstance(item, dict) for item in [*universe, *contexts]):
                raise HyperliquidError("invalid universe/context rows")
            return meta, contexts, datetime.now(UTC)
        except (requests.RequestException, ValueError, HyperliquidError) as error:
            last_error = error
            if attempt + 1 < max_attempts:
                time.sleep(retry_delay_seconds(error, attempt))
    raise HyperliquidError(f"{request_type} failed: {last_error}") from last_error


def _price(context):
    try:
        value = float(context.get("markPx"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def fetch_spot_market_data(session, timeout_seconds=30, max_attempts=3):
    """Return pair-keyed mark prices and full token identity metadata.

    USDC is treated as a USD proxy, as Binance's adapter treats USDT. Other quote
    tokens are withheld rather than assuming a dollar conversion. The timestamp
    is receipt time of the live mark context, not a last-trade timestamp.
    """
    meta, contexts, observed_at = _fetch_contexts(
        session, "spotMetaAndAssetCtxs", None, timeout_seconds, max_attempts
    )
    tokens = meta.get("tokens")
    if not isinstance(tokens, list) or not all(
        isinstance(token, dict)
        and isinstance(token.get("index"), int)
        and isinstance(token.get("name"), str)
        for token in tokens
    ):
        raise HyperliquidError("invalid spot tokens")
    by_index = {token["index"]: token for token in tokens}
    if len(by_index) != len(tokens):
        raise HyperliquidError("duplicate spot token indices")
    observations, identities = {}, {}
    by_coin = {}
    for context in contexts:
        coin = context.get("coin")
        if not isinstance(coin, str) or coin in by_coin:
            raise HyperliquidError("invalid or duplicate spot context coin")
        by_coin[coin] = context
    for pair in meta["universe"]:
        indices = pair.get("tokens")
        if (
            not isinstance(indices, list)
            or len(indices) != 2
            or any(
                not isinstance(index, int) or index not in by_index for index in indices
            )
            or not isinstance(pair.get("name"), str)
        ):
            raise HyperliquidError("invalid spot pair")
        context = by_coin.get(pair["name"], {})
        base, quote = (by_index[index] for index in indices)
        pair_id = f"{base['name']}/{quote['name']}".upper()
        if pair_id in identities:
            # Duplicate ticker/pair identities cannot be represented by Atlas's
            # existing pair ID; fail closed instead of silently choosing one.
            raise HyperliquidError(f"duplicate spot identity: {pair_id}")
        identities[pair_id] = {**base, "pair": pair["name"], "quote": quote["name"]}
        price = _price(context)
        if price is not None and quote["name"] == "USDC":
            observations[pair_id] = PriceObservation(
                price, "USDC", observed_at, "hyperliquid-spot", pair["name"]
            )
    return observations, identities


def fetch_perp_market_data(session, dex=None, timeout_seconds=30, max_attempts=3):
    """Read USDC mark contexts on the main or a USDC-collateral builder dex.

    Other collateral tokens need an explicit USD conversion and are withheld.
    USDC is Hyperliquid spot token index zero; a builder's collateral is never
    inferred from its name or assumed to trade at one dollar.
    """
    meta, contexts, observed_at = _fetch_contexts(
        session, "metaAndAssetCtxs", dex, timeout_seconds, max_attempts
    )
    if meta.get("collateralToken", 0 if dex is None else None) != 0:
        return {}
    observations = {}
    for instrument, context in zip(meta["universe"], contexts, strict=True):
        name = instrument.get("name")
        if not isinstance(name, str) or not name:
            raise HyperliquidError("invalid perp name")
        price = _price(context)
        if instrument.get("isDelisted") or price is None:
            continue
        if name.upper() in observations:
            raise HyperliquidError(f"duplicate perp name: {name}")
        observations[name.upper()] = PriceObservation(
            price, "USDC", observed_at, "hyperliquid-perps", name
        )
    return observations


def enrich_contract_identity(
    session,
    identity_assets,
    catalogue,
    timeout_seconds=20,
    max_attempts=3,
    min_interval_seconds=0.25,
    observations=None,
    max_relative_difference=0.05,
):
    """Corroborate HIP-1 tokens using CMC's ID-addressed HyperEVM contracts.

    Contract/platform evidence comes from the same keyless detail endpoint used
    by category enrichment. Only exact HyperEVM address matches count. Failures
    are retained per candidate and never treated as identity evidence.
    """
    if timeout_seconds <= 0 or max_attempts <= 0 or min_interval_seconds < 0:
        raise ValueError("invalid CMC contract request settings")
    by_symbol = {}
    for asset in catalogue.assets:
        by_symbol.setdefault(asset.symbol.upper(), []).append(asset)
    checked = {}
    next_request_at = 0.0
    for key, token in sorted(identity_assets.items()):
        if not key.startswith("hyperliquid-spot:"):
            continue
        contract = token.get("evmContract")
        address = contract.get("address") if isinstance(contract, dict) else None
        token_id = token.get("tokenId")
        if not address and not token_id:
            continue
        matches, checks = [], {}
        for asset in by_symbol.get(str(token.get("name", "")).upper(), []):
            if observations is not None:
                price = observations.get(key.split(":", 1)[1])
                if (
                    price is None
                    or abs(asset.price_usd - price.normalized_price)
                    / price.normalized_price
                    > max_relative_difference
                ):
                    continue
            if asset.cmc_id not in checked:
                checked[asset.cmc_id], next_request_at = _fetch_cmc_platforms(
                    session,
                    asset.cmc_id,
                    timeout_seconds,
                    max_attempts,
                    min_interval_seconds,
                    next_request_at,
                )
            platforms = checked[asset.cmc_id]
            if platforms is None:
                continue
            checks[str(asset.cmc_id)] = platforms
            if any(
                _platform_matches_token(platform, address, token_id)
                for platform in platforms
            ):
                matches.append(asset.cmc_id)
        token["cmc_contract_matches"] = matches
        token["cmc_contract_checks"] = checks


def _platform_matches_token(platform, address, token_id):
    native = platform.get("contractPlatform") == "Hyperliquid" and platform.get(
        "contractChainId"
    ) in (None, -1)
    evm = platform.get("contractPlatform") == "HyperEVM" and platform.get(
        "contractChainId"
    ) in (None, 999)
    expected = token_id if native else address if evm else None
    return (
        isinstance(expected, str)
        and bool(expected)
        and str(platform.get("contractAddress", "")).lower() == expected.lower()
    )


def _native_explorer_contracts(data):
    urls = data.get("urls") or {}
    explorers = urls.get("explorer", []) if isinstance(urls, dict) else []
    contracts = []
    if not isinstance(explorers, list):
        return contracts
    for value in explorers:
        if not isinstance(value, str):
            continue
        try:
            url = urlsplit(value)
        except ValueError:
            continue
        prefix = "/explorer/token/"
        if (
            url.scheme != "https"
            or url.netloc != "app.hyperliquid.xyz"
            or not url.path.startswith(prefix)
            or url.query
            or url.fragment
        ):
            continue
        token_id = url.path[len(prefix) :]
        if (
            len(token_id) != 34
            or not token_id.startswith("0x")
            or any(char not in "0123456789abcdefABCDEF" for char in token_id[2:])
        ):
            continue
        contracts.append(
            {
                "contractPlatform": "Hyperliquid",
                "contractChainId": -1,
                "contractAddress": token_id,
                "source_url": value,
            }
        )
    return contracts


def _fetch_cmc_platforms(
    session,
    cmc_id,
    timeout_seconds,
    max_attempts,
    min_interval_seconds,
    next_request_at,
):
    for attempt in range(max_attempts):
        delay = next_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            response = session.get(
                CMC_DETAIL_URL, params={"id": cmc_id}, timeout=timeout_seconds
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if (
                not isinstance(data, dict)
                or type(data.get("id")) is not int
                or data["id"] != cmc_id
                or (
                    data.get("platforms") is not None
                    and not isinstance(data["platforms"], list)
                )
            ):
                raise HyperliquidError("invalid CMC contract detail identity")
            platforms = data.get("platforms") or []
            if not all(isinstance(platform, dict) for platform in platforms):
                raise HyperliquidError("invalid CMC contract platforms")
            evidence = [
                {
                    field: platform.get(field)
                    for field in (
                        "contractPlatform",
                        "contractChainId",
                        "contractAddress",
                    )
                }
                for platform in platforms
            ] + _native_explorer_contracts(data)
            return evidence, time.monotonic() + min_interval_seconds
        except (requests.RequestException, ValueError, HyperliquidError) as error:
            if attempt + 1 == max_attempts:
                print(
                    f"CMC contract evidence unavailable for {cmc_id}: {error}",
                    file=sys.stderr,
                )
            else:
                next_request_at = time.monotonic() + retry_delay_seconds(error, attempt)
    return None, time.monotonic() + min_interval_seconds


def fetch_cmc_perp_identities(
    session, timeout_seconds=20, max_attempts=3, page_size=500
):
    """Read CMC's explicit instrument-to-asset links for Hyperliquid perps.

    Unlike a ticker match, a row links a specific Hyperliquid trade URL to a CMC
    ID. Spot links are excluded: CMC sometimes collapses bridged spot tokens onto
    the native asset, so HIP-1 identity still needs a full name or exact contract.
    """
    if timeout_seconds <= 0 or max_attempts <= 0 or page_size <= 0:
        raise ValueError("invalid CMC market request settings")
    identities = {}
    start = 1
    for _ in range(20):
        data = _fetch_cmc_market_page(
            session, start, page_size, timeout_seconds, max_attempts
        )
        markets = data["marketPairs"]
        for market in markets:
            if (
                not isinstance(market, dict)
                or market.get("exchangeId") != CMC_EXCHANGE_ID
                or market.get("category") != "perpetual"
            ):
                continue
            cmc_id = market.get("baseCurrencyId")
            if type(cmc_id) is not int or cmc_id <= 0:
                continue
            url = urlsplit(str(market.get("marketUrl", "")))
            if (
                url.scheme != "https"
                or url.netloc != "app.hyperliquid.xyz"
                or not url.path.startswith("/trade/")
                or url.query
                or url.fragment
            ):
                continue
            instrument = unquote(url.path[len("/trade/") :])
            if (
                not instrument
                or "/" in instrument
                or instrument.upper() != str(market.get("baseSymbol", "")).upper()
            ):
                continue
            key = f"hyperliquid-perps:{instrument.upper()}"
            metadata = identities.setdefault(
                key, {"cmc_market_ids": [], "cmc_markets": []}
            )
            if cmc_id not in metadata["cmc_market_ids"]:
                metadata["cmc_market_ids"].append(cmc_id)
                metadata["cmc_markets"].append(
                    {
                        field: market.get(field)
                        for field in (
                            "exchangeId",
                            "marketId",
                            "category",
                            "marketUrl",
                            "baseSymbol",
                            "baseCurrencyId",
                            "baseCurrencyName",
                            "baseCurrencySlug",
                            "lastUpdated",
                        )
                    }
                )
        start += len(markets)
        if start > data["numMarketPairs"]:
            return identities
        if not markets:
            raise HyperliquidError("incomplete CMC Hyperliquid markets")
    raise HyperliquidError("CMC market pagination exceeded its bound")


def _fetch_cmc_market_page(session, start, page_size, timeout_seconds, max_attempts):
    last_error = None
    for attempt in range(max_attempts):
        try:
            response = session.get(
                CMC_MARKETS_URL,
                params={
                    "id": CMC_EXCHANGE_ID,
                    "category": "perpetual",
                    "start": start,
                    "limit": page_size,
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if (
                not isinstance(data, dict)
                or data.get("id") != CMC_EXCHANGE_ID
                or data.get("slug") != "hyperliquid"
                or type(data.get("numMarketPairs")) is not int
                or data["numMarketPairs"] < 0
                or not isinstance(data.get("marketPairs"), list)
            ):
                raise HyperliquidError("invalid CMC Hyperliquid markets envelope")
            return data
        except (requests.RequestException, ValueError, HyperliquidError) as error:
            last_error = error
            if attempt + 1 < max_attempts:
                time.sleep(retry_delay_seconds(error, attempt))
    raise HyperliquidError(
        f"CMC Hyperliquid markets failed: {last_error}"
    ) from last_error


def refresh_cmc_id_quotes(
    session,
    catalogue,
    cmc_ids,
    timeout_seconds=20,
    max_attempts=3,
    min_interval_seconds=0.25,
    failed_ids=None,
):
    """Refresh identity-selected quotes by immutable ID, retaining source time.

    The listing endpoint can cache quotes beyond the alignment window. Refresh
    only IDs already carrying identity evidence; this never expands a catalogue
    or relaxes its trust gate. Failed or malformed quotes remain stale/unusable.
    """
    if timeout_seconds <= 0 or max_attempts <= 0 or min_interval_seconds < 0:
        raise ValueError("invalid CMC quote request settings")
    refreshed = {}
    for cmc_id in sorted(cmc_ids):
        for attempt in range(max_attempts):
            try:
                response = session.get(
                    CMC_DETAIL_URL,
                    params={"id": cmc_id, "t": int(time.time() * 1000)},
                    timeout=timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if (
                    not isinstance(data, dict)
                    or type(data.get("id")) is not int
                    or data["id"] != cmc_id
                ):
                    raise HyperliquidError("CMC quote returned a different identity")
                if not isinstance(data.get("latestUpdateTime"), str):
                    raise HyperliquidError("CMC quote has no timestamp")
                price = float(data["statistics"]["price"])
                timestamp = datetime.fromisoformat(
                    data["latestUpdateTime"].replace("Z", "+00:00")
                )
                if not math.isfinite(price) or price <= 0 or timestamp.tzinfo is None:
                    raise HyperliquidError("invalid CMC price/timestamp")
                refreshed[cmc_id] = (price, timestamp.astimezone(UTC))
                break
            except (
                requests.RequestException,
                ValueError,
                TypeError,
                KeyError,
                HyperliquidError,
            ) as error:
                if attempt + 1 == max_attempts:
                    print(
                        f"CMC quote unavailable for {cmc_id}: {error}", file=sys.stderr
                    )
                    if failed_ids is not None:
                        failed_ids.add(cmc_id)
                else:
                    time.sleep(retry_delay_seconds(error, attempt))
        if min_interval_seconds:
            time.sleep(min_interval_seconds)
    return replace(
        catalogue,
        assets=tuple(
            replace(
                asset,
                price_usd=refreshed[asset.cmc_id][0],
                last_updated=refreshed[asset.cmc_id][1],
            )
            if asset.cmc_id in refreshed
            else asset
            for asset in catalogue.assets
        ),
    )


def fetch_aligned_candle(
    session, observation, target_time, timeout_seconds=20, max_attempts=3
):
    """Return a closed one-minute candle at the CMC quote's source time.

    This aligns an independently observed exchange close with a cached CMC
    quote. Missing, unrelated, unfinished or invalid candles never become price
    evidence. Only recent quotes are considered; delisted markets are excluded
    upstream by requiring a live market observation first.
    """
    if timeout_seconds <= 0 or max_attempts <= 0 or target_time.tzinfo is None:
        raise ValueError("invalid candle request settings")
    now = datetime.now(UTC)
    if not timedelta(minutes=1) <= now - target_time <= timedelta(hours=1):
        return None
    start_ms = int(target_time.timestamp() // 60 * 60000)
    end_ms = start_ms + 59999
    for attempt in range(max_attempts):
        try:
            response = session.post(
                INFO_URL,
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": observation.instrument_id,
                        "interval": "1m",
                        "startTime": start_ms,
                        "endTime": end_ms,
                    },
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise HyperliquidError("invalid candle envelope")
            for candle in payload:
                if (
                    not isinstance(candle, dict)
                    or candle.get("s") != observation.instrument_id
                    or candle.get("i") != "1m"
                    or candle.get("t") != start_ms
                    or candle.get("T") != end_ms
                ):
                    continue
                price = _price({"markPx": candle.get("c")})
                if price is not None:
                    return replace(
                        observation,
                        price=price,
                        observed_at=datetime.fromtimestamp(end_ms / 1000, tz=UTC),
                    )
            return None
        except (requests.RequestException, ValueError, HyperliquidError) as error:
            if attempt + 1 == max_attempts:
                raise HyperliquidError(f"candle price unavailable: {error}") from error
            time.sleep(retry_delay_seconds(error, attempt))
    return None
