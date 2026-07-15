"""Keyless exploratory matcher for CoinMarketCap IDs.

This module calls CoinMarketCap's website data API. That endpoint is useful for
investigation only; the production integration described in
``docs/cmc-id-mapping-plan.md`` must use the authenticated CMC Pro API.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import requests


CMC_LISTING_URL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing"
BINANCE_USDM_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/price"
PAGE_SIZE = 5000
REQUEST_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class CmcAsset:
    cmc_id: int
    symbol: str
    slug: str
    price_usd: float


@dataclass(frozen=True)
class PriceMatch:
    asset: CmcAsset
    relative_difference: float


def select_verified_candidate(
    candidates: Iterable[CmcAsset], exchange_price: float, threshold: float
) -> PriceMatch | None:
    """Return the sole price-compatible asset, otherwise reject the match.

    Tickers are not unique. Price is therefore corroborating evidence, never a
    tie-breaker: more than one price-compatible CMC asset remains ambiguous.
    """
    if not math.isfinite(exchange_price) or exchange_price <= 0:
        raise ValueError("exchange_price must be finite and positive")
    if threshold <= 0:
        raise ValueError("threshold must be positive")

    matches = [
        PriceMatch(
            asset=candidate,
            relative_difference=abs(candidate.price_usd - exchange_price)
            / exchange_price,
        )
        for candidate in candidates
        if math.isfinite(candidate.price_usd)
        and candidate.price_usd > 0
        and abs(candidate.price_usd - exchange_price) / exchange_price <= threshold
    ]
    return matches[0] if len(matches) == 1 else None


def fetch_cmc_assets(session: requests.Session) -> list[CmcAsset]:
    """Return all assets available from the current website listing response."""
    assets: list[CmcAsset] = []
    for start in range(1, sys.maxsize, PAGE_SIZE):
        response = session.get(
            CMC_LISTING_URL,
            params={"start": start, "limit": PAGE_SIZE, "convert": "USD"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        page = response.json()["data"]["cryptoCurrencyList"]
        assets.extend(_parse_cmc_asset(item) for item in page)
        if len(page) < PAGE_SIZE:
            return assets
    raise RuntimeError("CMC listing pagination did not terminate")


def fetch_binance_usdm_price(session: requests.Session, base_symbol: str) -> float:
    response = session.get(
        BINANCE_USDM_TICKER_URL,
        params={"symbol": f"{base_symbol}USDT"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    price = float(response.json()["price"])
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"invalid Binance USD-M price for {base_symbol}: {price}")
    return price


def candidates_for_symbol(assets: Iterable[CmcAsset], symbol: str) -> list[CmcAsset]:
    return [asset for asset in assets if asset.symbol.upper() == symbol.upper()]


def _parse_cmc_asset(item: dict[str, Any]) -> CmcAsset:
    return CmcAsset(
        cmc_id=int(item["id"]),
        symbol=str(item["symbol"]),
        slug=str(item["slug"]),
        price_usd=float(item["quotes"][0]["price"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTC,ETH,SOL")
    parser.add_argument("--threshold", type=float, default=0.05)
    args = parser.parse_args()

    with requests.Session() as session:
        assets = fetch_cmc_assets(session)
        print(
            "symbol\tcmc_id\tslug\texchange_price_usdt\tcmc_price_usd\tdifference\tstatus"
        )
        for raw_symbol in args.symbols.split(","):
            symbol = raw_symbol.strip().upper()
            if not symbol:
                continue
            try:
                exchange_price = fetch_binance_usdm_price(session, symbol)
                candidates = candidates_for_symbol(assets, symbol)
                match = select_verified_candidate(
                    candidates, exchange_price, args.threshold
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                requests.RequestException,
            ) as error:
                print(f"{symbol}\t\t\t\t\t\tfailed: {error}")
                continue

            if match is None:
                print(
                    f"{symbol}\t\t\t{exchange_price:.8f}\t\t\tambiguous_or_unmatched "
                    f"({len(candidates)} ticker candidates)"
                )
                continue
            print(
                f"{symbol}\t{match.asset.cmc_id}\t{match.asset.slug}\t{exchange_price:.8f}\t"
                f"{match.asset.price_usd:.8f}\t{match.relative_difference:.4%}\tverified"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
