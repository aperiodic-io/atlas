from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from atlas.exchanges import get_exchange_fetcher


class SymbolLifecycle(StrEnum):
    """How a source's rows relate to the rows already on file."""

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
            raise ValueError(f"Unknown exchange: {exchange}")
        return {"availableSymbols": fetcher(self.timeout_seconds)}
