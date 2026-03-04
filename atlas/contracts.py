from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ContractType(str, Enum):
    spot = "spot"
    perpetual = "perpetual"
    future = "future"
    option = "option"
    unknown = "unknown"


@dataclass
class Contract:
    exchange: str
    original_id: str
    symbol: str  # e.g. BTC
    denominator: str  # e.g. USDT (quote / settlement currency)
    margin: str | None  # e.g. USDT for linear, BTC for inverse (coin-margined)
    contract_type: ContractType
    delivery_date: datetime | None = None

    def __str__(self) -> str:
        core = f"{self.contract_type.value}-{self.symbol}-{self.denominator}"
        # Spot instruments are not margined.
        if self.contract_type != ContractType.spot and self.margin is not None:
            core = f"{core}:{self.margin}"
        return core + (
            f"-{self.delivery_date.strftime('%Y%m%d')}" if self.delivery_date else ""
        )

    def __repr__(self) -> str:
        return self.__str__()

    @property
    def internal_id(self) -> str:
        return self.__str__()
