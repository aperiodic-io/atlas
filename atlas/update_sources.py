from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from atlas.exchanges import get_exchange_fetcher, is_on_tardis
from atlas.utils import _fetch_exchange


class SymbolLifecycle(StrEnum):
    """How a source's rows relate to the rows already on file."""

    # The source carries the full history; its output replaces the file.
    REPLACE = "replace"
    # The source is partial; rows it omits are kept as they are.
    MERGE = "merge"
    # The source lists the whole current universe; rows it omits are delisting
    # candidates, and its rows may report delistings themselves.
    COMPLETE = "complete"


class SymbolSource(Protocol):
    def fetch_exchange(self, exchange: str) -> dict[str, Any]:
        """Return exchange metadata shaped like {\"availableSymbols\": [...]}"""


def source_lifecycle(source: SymbolSource) -> SymbolLifecycle:
    """A source's lifecycle; sources that do not declare one are treated as partial."""
    return getattr(source, "lifecycle", SymbolLifecycle.MERGE)


@dataclass(frozen=True)
class TardisSymbolSource:
    lifecycle: ClassVar[SymbolLifecycle] = SymbolLifecycle.REPLACE

    def fetch_exchange(self, exchange: str) -> dict[str, Any]:
        # Fail loudly rather than refresh a venue Tardis does not list from an
        # empty (or, under a shared name such as "mexc", the wrong) listing.
        if not is_on_tardis(exchange):
            raise ValueError(f"Tardis does not list {exchange}")
        data = _fetch_exchange(exchange)
        # Tardis answers an exchange it does not cover with an error object; a
        # Tardis refresh replaces the snapshot wholesale, so that must fail the
        # exchange rather than empty it.
        if not isinstance(data.get("availableSymbols"), list):
            raise ValueError(f"Tardis has no symbol list for {exchange}: {data.get('message', data)}")
        return data


@dataclass(frozen=True)
class ExchangeApiSymbolSource:
    """Live exchange listings; each fetch returns the exchange's whole current universe.

    Rows the exchange reports as delisted carry an `end_date`. Anything absent
    from a fetch is no longer listed, which lets the updater detect delistings.
    """

    lifecycle: ClassVar[SymbolLifecycle] = SymbolLifecycle.COMPLETE
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

    lifecycle: ClassVar[SymbolLifecycle] = SymbolLifecycle.MERGE

    exchange_source: ExchangeApiSymbolSource = field(default_factory=ExchangeApiSymbolSource)
    tardis_source: TardisSymbolSource = field(default_factory=TardisSymbolSource)

    def fetch_exchange(self, exchange: str) -> dict[str, Any]:
        fetcher = get_exchange_fetcher(exchange)
        if fetcher is None:
            return self.tardis_source.fetch_exchange(exchange)
        if not is_on_tardis(exchange):
            return self.exchange_source.fetch_exchange(exchange)

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
