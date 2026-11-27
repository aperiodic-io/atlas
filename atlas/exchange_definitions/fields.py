"""Parse symbol records whose fetcher recorded the venue's own legs.

Exchange-API fetchers store the venue's base asset, quote asset and settle
asset (`base_coin`, `quote_coin`, `margin_asset`) and, for dated contracts, an
ISO `delivery_date`. Those fields are authoritative; rows that lack them
(history recorded before a fetcher) fall back to each venue's id parser.
"""

from __future__ import annotations

from ..contracts import Contract, ContractType
from ..parser_interface import SymbolData
from .common import instrument_type, make_contract, venue_margin
from .http import metadata_delivery_date


def parse_recorded_legs(exchange: str, sd: SymbolData) -> Contract | None:
    base = sd.get("base_coin")
    quote = sd.get("quote_coin")
    if not base or not quote:
        return None
    ctype = instrument_type(sd)
    delivery = metadata_delivery_date(sd) if ctype == ContractType.future else None
    margin = venue_margin(sd, str(base), str(quote), ctype)
    return make_contract(exchange, sd, str(base), str(quote), margin, ctype, delivery)
