from __future__ import annotations

from .common import SkipSymbol


def is_beta_exchange(exchange: str) -> bool:
    # Lazy import to avoid circular dependency during package initialization.
    from ..exchanges import is_beta_exchange as _is_beta_exchange

    return _is_beta_exchange(exchange)


__all__ = ["SkipSymbol", "is_beta_exchange"]
