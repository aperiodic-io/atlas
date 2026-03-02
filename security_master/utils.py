import logging

import pandas as pd
import requests
import tenacity

from security_master.exchange_ids import to_tardis_exchange_id

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING)

_exchange_cache: dict[str, dict] = {}


@tenacity.retry(
    stop=tenacity.stop_after_attempt(5),
    wait=tenacity.wait_fixed(60),
    before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
)
def _fetch_exchange(exchange: str) -> dict:
    """Fetch and cache the full exchange details from Tardis."""
    tardis_exchange_id = to_tardis_exchange_id(exchange)
    if tardis_exchange_id not in _exchange_cache:
        _exchange_cache[tardis_exchange_id] = requests.get(
            f"https://api.tardis.dev/v1/exchanges/{tardis_exchange_id}"
        ).json()
    return _exchange_cache[tardis_exchange_id]


def get_symbols(exchange: str, from_date: str, to_date: str) -> list[str]:
    exchange_details = _fetch_exchange(exchange)
    return [
        s["id"]
        for s in exchange_details["availableSymbols"]
        if pd.to_datetime(s["availableSince"]).tz_localize(None)
        <= pd.to_datetime(from_date)
        and (
            pd.to_datetime(s["availableTo"]).tz_localize(None)
            >= pd.to_datetime(to_date)
            if "availableTo" in s and s["availableTo"] is not None
            else True
        )
    ]


def get_symbols_all_time(exchange: str) -> dict[str, tuple[str, str]]:
    """Return all symbols ever available on the exchange mapped to (availableSince, availableTo)."""
    exchange_details = _fetch_exchange(exchange)
    return {
        s["id"]: (s["availableSince"], s.get("availableTo") or "present")
        for s in exchange_details["availableSymbols"]
    }
