from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .contracts import Contract

type SymbolData = Mapping[str, Any]


class Parser(Protocol):
    def __call__(self, exchange: str, symbol_data: SymbolData) -> Contract: ...
