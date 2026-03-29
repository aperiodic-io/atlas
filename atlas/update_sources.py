from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from atlas.exchanges import get_exchange_fetcher
from atlas.utils import _fetch_exchange


class SymbolSource(Protocol):
    def fetch_exchange(self, exchange: str) -> dict[str, Any]:
        """Return exchange metadata shaped like {\"availableSymbols\": [...]}"""


@dataclass(frozen=True)
class TardisSymbolSource:
    def fetch_exchange(self, exchange: str) -> dict[str, Any]:
        return _fetch_exchange(exchange)


@dataclass(frozen=True)
class ExchangeApiSymbolSource:
    timeout_seconds: int = 20

    def fetch_exchange(self, exchange: str) -> dict[str, Any]:
        fetcher = get_exchange_fetcher(exchange)
        if fetcher is None:
            raise ValueError(f"Exchange API source is not supported for: {exchange}")
        return {"availableSymbols": fetcher(self.timeout_seconds)}


@dataclass(frozen=True)
class HybridSymbolSource:
    """
    Use exchange APIs for selected exchanges and fall back to Tardis for the rest.
    """

    exchange_source: ExchangeApiSymbolSource = field(default_factory=ExchangeApiSymbolSource)
    tardis_source: TardisSymbolSource = field(default_factory=TardisSymbolSource)

    def fetch_exchange(self, exchange: str) -> dict[str, Any]:
        fetcher = get_exchange_fetcher(exchange)
        if fetcher is None:
            return self.tardis_source.fetch_exchange(exchange)

        # When an API fetcher exists, we merge results:
        # 1. Start with Tardis data to get all historical/delisted symbols and metadata
        # 2. Update/Override with API data for current/accurate IDs and types
        try:
            tardis_data = self.tardis_source.fetch_exchange(exchange)
        except Exception:
            # Fallback if Tardis is down or doesn't have the exchange yet
            tardis_data = {"availableSymbols": []}

        api_data = self.exchange_source.fetch_exchange(exchange)

        # Index Tardis symbols by ID for easy lookup
        # We copy the dicts to avoid modifying the global cache in atlas.utils
        symbols_by_id = {
            s["id"]: s.copy() for s in tardis_data.get("availableSymbols", []) if "id" in s
        }

        # Merge API data: update existing or add new ones
        for api_symbol in api_data.get("availableSymbols", []):
            sid = api_symbol.get("id")
            if sid in symbols_by_id:
                symbols_by_id[sid].update(api_symbol)
            else:
                symbols_by_id[sid] = api_symbol.copy()

        return {"availableSymbols": list(symbols_by_id.values())}
