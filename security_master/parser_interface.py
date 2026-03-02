from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, TypeAlias

from .contracts import Contract

SymbolData: TypeAlias = Mapping[str, Any]


class Parser(Protocol):
    def __call__(self, exchange: str, symbol_data: SymbolData) -> Contract: ...
