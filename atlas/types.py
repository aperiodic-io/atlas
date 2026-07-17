from enum import Enum


class UnderlyingType(str, Enum):
    """Normalized class of an instrument's underlying asset."""

    crypto = "crypto"
    commodity = "commodity"
    equity = "equity"
    index = "index"
    pre_market = "pre_market"
    unknown = "unknown"
