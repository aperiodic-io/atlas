from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from securities_master.exchanges import get_exchange_fetcher
from securities_master.utils import _fetch_exchange


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

    exchange_source: ExchangeApiSymbolSource = ExchangeApiSymbolSource()
    tardis_source: TardisSymbolSource = TardisSymbolSource()

    def fetch_exchange(self, exchange: str) -> dict[str, Any]:
        if get_exchange_fetcher(exchange) is not None:
            return self.exchange_source.fetch_exchange(exchange)
        return self.tardis_source.fetch_exchange(exchange)
