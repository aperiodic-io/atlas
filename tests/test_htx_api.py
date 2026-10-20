"""HTX (Huobi) exchange-API fetchers: htx-spot, htx-perps-coin, htx-futures-coin
and htx-futures-usdt.

Fixtures under `fixtures/htx/` are trimmed excerpts of the real responses
captured on 2026-10-20 (every status, quote asset and asset class the full
payloads contain). Rows for statuses that were absent that day are derived
from a real row in the tests below.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.exchange_definitions.common import SkipSymbol, parse_utc_timestamp
from atlas.exchange_definitions.huobi import (
    fetch_htx_futures_coin,
    fetch_htx_futures_usdt,
    fetch_htx_perpetual_coin,
    fetch_htx_spot,
    parse_htx_coin_futures,
    parse_htx_coin_swap,
    parse_htx_spot,
    parse_htx_usdt_futures,
)

FIXTURES = Path(__file__).parent / "fixtures" / "htx"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


SPOT_SYMBOLS = _load("spot_symbols.json")
SPOT_CURRENCIES = _load("spot_currencies.json")
COIN_SWAP = _load("coin_swap.json")
COIN_FUTURES = _load("coin_futures.json")
USDT_FUTURES = _load("usdt_futures.json")

SPOT_SYMBOLS_URL = "https://api.huobi.pro/v2/settings/common/symbols"
SPOT_CURRENCIES_URL = "https://api.huobi.pro/v2/settings/common/currencies"
COIN_SWAP_URL = "https://api.hbdm.vn/swap-api/v1/swap_contract_info"
COIN_FUTURES_URL = "https://api.hbdm.vn/api/v1/contract_contract_info"
LINEAR_URL = "https://api.hbdm.vn/linear-swap-api/v1/swap_contract_info"


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _fetch(fetcher, routes: dict[str, object], calls: list | None = None) -> list[dict]:
    def get(url, params=None, timeout=None):
        if calls is not None:
            calls.append((url, params))
        if url not in routes:
            raise AssertionError(f"unexpected request {url}")
        return _response(routes[url])

    with patch("atlas.exchange_definitions.http.requests.get", side_effect=get):
        return fetcher(timeout_seconds=5)


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {sd["id"]: sd for sd in rows}


def _with_rows(payload: dict, rows: list[dict]) -> dict:
    return {**payload, "data": rows}


def _variant(payload: dict, index: int = 0, **changes) -> dict:
    row = copy.deepcopy(payload["data"][index])
    row.update(changes)
    return row


def _assert_recent(timestamp: str) -> None:
    parsed = parse_utc_timestamp(timestamp)
    assert parsed is not None
    assert abs(datetime.now(UTC) - parsed) < timedelta(minutes=5)


def _fallback(parser, exchange: str, sd: dict) -> str:
    """The internal_id a Tardis-only row (id and type only) gets."""
    return parser(exchange, {"id": sd["id"], "type": sd["type"]}).internal_id


# ---------------------------------------------------------------------------
# htx-spot
# ---------------------------------------------------------------------------


def _spot(symbols: dict = SPOT_SYMBOLS, currencies: dict = SPOT_CURRENCIES, calls=None) -> list[dict]:
    return _fetch(fetch_htx_spot, {SPOT_SYMBOLS_URL: symbols, SPOT_CURRENCIES_URL: currencies}, calls)


def _spot_ids(rows: list[dict]) -> dict[str, str]:
    return {sd["id"]: parse_htx_spot("htx-spot", sd).internal_id for sd in rows}


class TestHtxSpot:
    def test_queries_the_v2_symbol_and_currency_settings(self):
        calls: list = []
        _spot(calls=calls)
        assert [url for url, _ in calls] == [SPOT_SYMBOLS_URL, SPOT_CURRENCIES_URL]

    def test_every_fixture_row_is_returned_with_its_tardis_id(self):
        rows = _spot()
        assert [sd["id"] for sd in rows] == [item["sc"] for item in SPOT_SYMBOLS["data"]]
        assert all(sd["type"] == "spot" for sd in rows)
        # Tardis `huobi` ids are the lowercase symbol codes.
        assert all(sd["id"] == sd["id"].lower() for sd in rows)

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("btcusdt", "spot-BTC-USDT"),
            ("0gusdt", "spot-0G-USDT"),
            ("ethbtc", "spot-ETH-BTC"),
            ("trxusdt", "spot-TRX-USDT"),
            ("usdcusdt", "spot-USDC-USDT"),
            ("usdthusd", "spot-USDT-HUSD"),
            ("tusdusdd", "spot-TUSD-USDD"),
            ("btmhusd", "spot-BTM-HUSD"),
            ("nftht", "spot-NFT-HT"),
            ("trxkgst", "spot-TRX-KGST"),
            ("btcpyusd", "spot-BTC-PYUSD"),
            ("ethusd1", "spot-ETH-USD1"),
            ("ethtusd", "spot-ETH-TUSD"),
            ("strxtrx", "spot-STRX-TRX"),
            ("eurocusdc", "spot-EUROC-USDC"),
            ("etheuroc", "spot-ETH-EUROC"),
            ("euroceur", "spot-EUROC-EUR"),
            ("btctry", "spot-BTC-TRY"),
            ("usdttry", "spot-USDT-TRY"),
            ("usdtrub", "spot-USDT-RUB"),
            ("usdtuah", "spot-USDT-UAH"),
            ("btcbrl", "spot-BTC-BRL"),
            ("btcgbp", "spot-BTC-GBP"),
            ("eurusdt", "spot-EUR-USDT"),
            # Leveraged tokens keep their own code (`bcdn` reads `BTC*3`).
            ("btc3lusdt", "spot-BTC3L-USDT"),
            ("eth1susdt", "spot-ETH1S-USDT"),
            ("币安人生usdt", "spot-币安人生-USDT"),
        ],
    )
    def test_internal_ids(self, sid, internal_id):
        assert _spot_ids(_spot())[sid] == internal_id

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            # The id predates an asset rename; `bc`/`qc` are the current codes.
            ("lunaust", "spot-LUNC-USTC"),
            ("mirust", "spot-MIR-USTC"),
            ("maticeth", "spot-POL-ETH"),
            ("hoteth", "spot-HYDRO-ETH"),
            ("mtlbtc", "spot-MTLOLD-BTC"),
            ("noeusdt", "spot-NOW-USDT"),
            ("xptusdt", "spot-XPT1-USDT"),
        ],
    )
    def test_renamed_assets_take_the_venues_current_codes(self, sid, internal_id):
        rows = _by_id(_spot())
        assert parse_htx_spot("htx-spot", rows[sid]).internal_id == internal_id
        # A Tardis-only row of the same market reads the same.
        assert _fallback(parse_htx_spot, "htx-spot", rows[sid]) == internal_id

    def test_legs_are_uppercase_venue_codes(self):
        row = _by_id(_spot())["lunaust"]
        assert (row["base_coin"], row["quote_coin"]) == ("LUNC", "USTC")
        assert "margin_asset" not in row
        assert "contract_size" not in row

    @pytest.mark.parametrize(
        ("sid", "underlying"),
        [
            ("btcusdt", "crypto"),
            ("usdcusdt", "crypto"),  # stablecoin
            ("btc3lusdt", "crypto"),  # leveraged token (etp)
            ("tornusdt", "crypto"),  # privacycoin
            ("币安人生usdt", "crypto"),
            ("aaplusdt", "equity"),  # tradfi,stock
            ("noeusdt", "equity"),  # NOW (ServiceNow)
            ("spx500usdt", "index"),  # tradfi,indices
            ("xagusdt", "commodity"),  # tradfi,commodities
            ("xautusdt", "commodity"),  # tradfi,stablecoin,commodities (2023)
            ("eurusdt", "forex"),  # fiatassets
            # Markets that opened before HTX's 2026 TradFi launch traded a
            # crypto token whose code HTX has since re-tagged as a stock.
            ("botusdt", "unknown"),
            ("wmtusdt", "unknown"),
            ("gmeusdt", "unknown"),
        ],
    )
    def test_underlying_from_base_currency_tags(self, sid, underlying):
        assert _by_id(_spot())[sid]["underlying"] == underlying

    def test_base_currency_missing_from_currency_list_is_unknown(self):
        currencies = _with_rows(
            SPOT_CURRENCIES, [c for c in SPOT_CURRENCIES["data"] if c["cc"] != "btc"]
        )
        rows = _by_id(_spot(currencies=currencies))
        assert rows["btcusdt"]["underlying"] == "unknown"
        assert rows["ethbtc"]["underlying"] == "crypto"  # only the base matters

    def test_first_capture_is_the_trading_open_time(self):
        rows = _by_id(_spot())
        assert rows["0gusdt"]["first_capture"] == "2025-09-22T10:00:00.000Z"
        assert rows["xautusdt"]["first_capture"] == "2023-01-30T11:30:00.000Z"
        # HTX's placeholder for markets listed before it recorded the time.
        assert rows["btcusdt"]["first_capture"] == "2018-01-01T04:00:00.000Z"

    def test_offline_is_delisted_at_first_sighting(self):
        rows = _by_id(_spot())
        for sid in ("qtumbtc", "lunaust", "aaplusdt", "btc3lusdt"):
            _assert_recent(rows[sid]["end_date"])

    @pytest.mark.parametrize("sid", ["btcusdt", "0gusdt", "xautusdt", "wlfiusdt", "stgusdt"])
    def test_online_and_suspended_markets_stay_listed(self, sid):
        assert "end_date" not in _by_id(_spot())[sid]

    @pytest.mark.parametrize("state", ["pre-online", "not-online"])
    def test_markets_that_have_not_opened_are_left_out(self, state):
        launch = _variant(SPOT_SYMBOLS, sc="newusdt", bc="new", state=state, toa=4133404800000)
        rows = _by_id(_spot(_with_rows(SPOT_SYMBOLS, [*SPOT_SYMBOLS["data"], launch])))
        assert "newusdt" not in rows
        assert "btcusdt" in rows

    @pytest.mark.parametrize("state", ["fuse", "transfer-board", "something-new"])
    def test_other_states_stay_listed(self, state):
        row = _variant(SPOT_SYMBOLS, state=state)
        out = _spot(_with_rows(SPOT_SYMBOLS, [row]))
        assert out == [
            {
                "id": "btcusdt",
                "type": "spot",
                "base_coin": "BTC",
                "quote_coin": "USDT",
                "underlying": "crypto",
                "first_capture": "2018-01-01T04:00:00.000Z",
            }
        ]

    def test_missing_open_time_is_omitted(self):
        row = _variant(SPOT_SYMBOLS, toa=None)
        (sd,) = _spot(_with_rows(SPOT_SYMBOLS, [row]))
        assert "first_capture" not in sd
        assert None not in sd.values()

    @pytest.mark.parametrize(
        "payload",
        [
            {"status": "error", "err-code": "invalid-parameter", "err-msg": "invalid"},
            {"status": "ok", "data": None},
            {"status": "ok", "data": {"sc": "btcusdt"}},
            {"data": []},
            [],
        ],
    )
    def test_malformed_symbols_raise(self, payload):
        with pytest.raises(ValueError):
            _spot(symbols=payload)

    def test_malformed_currencies_raise(self):
        with pytest.raises(ValueError):
            _spot(currencies={"status": "error", "err-msg": "busy"})

    def test_tardis_fallback_equals_the_api_row_for_every_fixture_market(self):
        mismatches = {
            sd["id"]: (parse_htx_spot("htx-spot", sd).internal_id, _fallback(parse_htx_spot, "htx-spot", sd))
            for sd in _spot()
        }
        assert {k: v for k, v in mismatches.items() if v[0] != v[1]} == {}

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            # Tardis-only markets the API no longer lists.
            ("maticusdt", "spot-MATIC-USDT"),
            ("paxhusd", "spot-PAX-HUSD"),
            ("btttrx", "spot-BTT-TRX"),
            ("ustusdt", "spot-UST-USDT"),
            ("holousdt", "spot-HOLO-USDT"),
            ("portalbtc", "spot-PORTAL-BTC"),
            ("我踏马来了usdt", "spot-我踏马来了-USDT"),
        ],
    )
    def test_tardis_only_ids(self, sid, internal_id):
        assert parse_htx_spot("htx-spot", {"id": sid, "type": "spot"}).internal_id == internal_id

    def test_unsplittable_tardis_id_is_skipped(self):
        with pytest.raises(SkipSymbol):
            parse_htx_spot("htx-spot", {"id": "nothing", "type": "spot"})


# ---------------------------------------------------------------------------
# htx-perps-coin
# ---------------------------------------------------------------------------


def _coin_swap(payload: dict = COIN_SWAP, calls=None) -> list[dict]:
    return _fetch(fetch_htx_perpetual_coin, {COIN_SWAP_URL: payload}, calls)


class TestHtxPerpsCoin:
    def test_all_listed_swaps(self):
        rows = _coin_swap()
        assert [sd["id"] for sd in rows] == ["BTC-USD", "ETH-USD", "XRP-USD", "TRX-USD", "DOGE-USD"]

    def test_inverse_contract_fields(self):
        btc = _by_id(_coin_swap())["BTC-USD"]
        assert btc == {
            "id": "BTC-USD",
            "type": "perpetual",
            "base_coin": "BTC",
            "quote_coin": "USD",
            "margin_asset": "BTC",
            "contract_size": 100.0,
            "quantity_unit": "quote",
            "underlying": "crypto",
            "first_capture": "2020-03-25T00:00:00.000Z",
        }
        assert _by_id(_coin_swap())["ETH-USD"]["contract_size"] == 10.0

    def test_internal_ids_and_contract(self):
        rows = _by_id(_coin_swap())
        contract = parse_htx_coin_swap("htx-perps-coin", rows["BTC-USD"])
        assert contract.internal_id == "perpetual-BTC-USD:BTC"
        assert contract.contract_size == 100.0
        assert contract.quantity_unit == "quote"
        assert parse_htx_coin_swap("htx-perps-coin", rows["DOGE-USD"]).internal_id == "perpetual-DOGE-USD:DOGE"

    def test_tardis_fallback_equals_the_api_row(self):
        for sd in _coin_swap():
            assert _fallback(parse_htx_coin_swap, "htx-perps-coin", sd) == parse_htx_coin_swap(
                "htx-perps-coin", sd
            ).internal_id
        # A Tardis-only (delisted) swap.
        assert (
            parse_htx_coin_swap("htx-perps-coin", {"id": "YFII-USD", "type": "perpetual"}).internal_id
            == "perpetual-YFII-USD:YFII"
        )

    def test_statuses(self):
        rows = [
            _variant(COIN_SWAP, 0, contract_status=1),
            _variant(COIN_SWAP, 1, contract_status=3),  # suspended
            _variant(COIN_SWAP, 2, contract_status=5),  # settling (funding)
            _variant(COIN_SWAP, 3, contract_status=0, delivery_time="1700000000000"),
            _variant(COIN_SWAP, 4, contract_status=2),  # pending listing
        ]
        out = _by_id(_coin_swap(_with_rows(COIN_SWAP, rows)))
        assert set(out) == {"BTC-USD", "ETH-USD", "XRP-USD", "TRX-USD"}
        for sid in ("BTC-USD", "ETH-USD", "XRP-USD"):
            assert "end_date" not in out[sid]
        assert out["TRX-USD"]["end_date"] == "2023-11-14T22:13:20.000Z"

    def test_scheduled_delisting(self):
        past = _variant(COIN_SWAP, 0, contract_status=3, delivery_time="1700000000000")
        future = _variant(COIN_SWAP, 1, contract_status=3, delivery_time="4133404800000")
        out = _by_id(_coin_swap(_with_rows(COIN_SWAP, [past, future])))
        assert out["BTC-USD"]["end_date"] == "2023-11-14T22:13:20.000Z"
        assert "end_date" not in out["ETH-USD"]

    def test_uses_the_hbdm_vn_host(self):
        calls: list = []
        _coin_swap(calls=calls)
        assert calls == [(COIN_SWAP_URL, None)]

    @pytest.mark.parametrize(
        "payload",
        [
            {"status": "error", "err_code": 1014, "err_msg": "This contract doesnt exist."},
            {"status": "ok"},
            {"status": "ok", "data": "BTC-USD"},
            [],
        ],
    )
    def test_malformed_payload_raises(self, payload):
        with pytest.raises(ValueError):
            _coin_swap(payload)


# ---------------------------------------------------------------------------
# htx-futures-coin
# ---------------------------------------------------------------------------


def _coin_futures(payload: dict = COIN_FUTURES, calls=None) -> list[dict]:
    return _fetch(fetch_htx_futures_coin, {COIN_FUTURES_URL: payload}, calls)


class TestHtxFuturesCoin:
    def test_ids_are_the_dated_contract_codes(self):
        assert [sd["id"] for sd in _coin_futures()] == [
            "BTC261023", "BTC261030", "BTC261225",
            "ETH261023", "ETH261030", "ETH261225",
            "TRX261023", "TRX261030",
        ]  # fmt: skip

    def test_inverse_dated_contract_fields(self):
        btc = _by_id(_coin_futures())["BTC261225"]
        assert btc == {
            "id": "BTC261225",
            "type": "future",
            "base_coin": "BTC",
            "quote_coin": "USD",
            "margin_asset": "BTC",
            "contract_size": 100.0,
            "quantity_unit": "quote",
            "delivery_date": "2026-12-25T08:00:00Z",
            "underlying": "crypto",
            "first_capture": "2026-09-11T00:00:00.000Z",
        }

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTC261023", "future-BTC-USD:BTC-20261023"),
            ("BTC261225", "future-BTC-USD:BTC-20261225"),
            ("ETH261030", "future-ETH-USD:ETH-20261030"),
            ("TRX261023", "future-TRX-USD:TRX-20261023"),
        ],
    )
    def test_internal_ids(self, sid, internal_id):
        sd = _by_id(_coin_futures())[sid]
        assert parse_htx_coin_futures("htx-futures-coin", sd).internal_id == internal_id
        assert _fallback(parse_htx_coin_futures, "htx-futures-coin", sd) == internal_id

    def test_contract_carries_delivery_and_size(self):
        contract = parse_htx_coin_futures("htx-futures-coin", _by_id(_coin_futures())["ETH261225"])
        assert contract.delivery_date == datetime(2026, 12, 25, 8, tzinfo=UTC)
        assert contract.contract_size == 10.0
        assert contract.quantity_unit == "quote"
        assert contract.margin == "ETH"

    def test_statuses(self):
        rows = [
            _variant(COIN_FUTURES, 0, contract_status=6),  # delivering, not yet past
            _variant(COIN_FUTURES, 1, contract_status=2),  # pending listing
            _variant(
                COIN_FUTURES, 2, contract_code="BTC231117", contract_status=8, delivery_time="1700208000000"
            ),
            _variant(COIN_FUTURES, 3, contract_status=3),  # suspended
        ]
        out = _by_id(_coin_futures(_with_rows(COIN_FUTURES, rows)))
        assert set(out) == {"BTC261023", "BTC231117", "ETH261023"}
        assert "end_date" not in out["BTC261023"]
        assert "end_date" not in out["ETH261023"]
        # Delivered: ends at its delivery.
        assert out["BTC231117"]["end_date"] == "2023-11-17T08:00:00.000Z"

    def test_rolling_aliases_are_not_dated(self):
        # Tardis `huobi-dm` only has aliases; they still parse but carry no date.
        contract = parse_htx_coin_futures("htx-futures-coin", {"id": "BTC_CQ", "type": "future"})
        assert contract.delivery_date is None

    def test_uses_the_hbdm_vn_host(self):
        calls: list = []
        _coin_futures(calls=calls)
        assert calls == [(COIN_FUTURES_URL, None)]

    def test_malformed_payload_raises(self):
        with pytest.raises(ValueError):
            _coin_futures({"status": "error", "err_code": 1000, "err_msg": "system error"})


# ---------------------------------------------------------------------------
# htx-futures-usdt
# ---------------------------------------------------------------------------


def _usdt_futures(payload: dict = USDT_FUTURES, calls=None) -> list[dict]:
    return _fetch(fetch_htx_futures_usdt, {LINEAR_URL: payload}, calls)


class TestHtxFuturesUsdt:
    def test_requests_the_futures_business_type(self):
        calls: list = []
        _usdt_futures(calls=calls)
        assert calls == [(LINEAR_URL, {"business_type": "futures"})]

    def test_ids_and_fields(self):
        rows = _by_id(_usdt_futures())
        assert set(rows) == {"BTC-USDT-261023", "BTC-USDT-261030", "ETH-USDT-261023", "ETH-USDT-261030"}
        assert rows["BTC-USDT-261023"] == {
            "id": "BTC-USDT-261023",
            "type": "future",
            "base_coin": "BTC",
            "quote_coin": "USDT",
            "margin_asset": "USDT",
            "contract_size": 0.001,
            "quantity_unit": "base",
            "delivery_date": "2026-10-23T08:00:00Z",
            "underlying": "crypto",
            "first_capture": "2026-10-09T00:00:00.000Z",
        }

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTC-USDT-261023", "future-BTC-USDT:USDT-20261023"),
            ("BTC-USDT-261030", "future-BTC-USDT:USDT-20261030"),
            ("ETH-USDT-261030", "future-ETH-USDT:USDT-20261030"),
        ],
    )
    def test_internal_ids(self, sid, internal_id):
        sd = _by_id(_usdt_futures())[sid]
        assert parse_htx_usdt_futures("htx-futures-usdt", sd).internal_id == internal_id
        assert _fallback(parse_htx_usdt_futures, "htx-futures-usdt", sd) == internal_id

    def test_swaps_in_the_response_are_ignored(self):
        swap = _variant(
            USDT_FUTURES, 0, contract_code="BTC-USDT", business_type="swap", contract_type="swap", delivery_time=""
        )
        out = _usdt_futures(_with_rows(USDT_FUTURES, [swap, *USDT_FUTURES["data"]]))
        assert "BTC-USDT" not in _by_id(out)
        assert len(out) == 4

    def test_tradfi_labels_classify_the_underlying(self):
        stock = _variant(
            USDT_FUTURES, 0, contract_code="AAPL-USDT-261023", pair="AAPL-USDT", symbol="AAPL", tradfi_labels=["Stocks"]
        )
        odd = _variant(
            USDT_FUTURES, 1, contract_code="ODD-USDT-261030", pair="ODD-USDT", symbol="ODD", tradfi_labels=["Bonds"]
        )
        out = _by_id(_usdt_futures(_with_rows(USDT_FUTURES, [stock, odd])))
        assert out["AAPL-USDT-261023"]["underlying"] == "equity"
        assert out["ODD-USDT-261030"]["underlying"] == "unknown"

    def test_statuses(self):
        rows = [
            _variant(USDT_FUTURES, 0, contract_status=2),
            _variant(USDT_FUTURES, 1, contract_status=5),
            _variant(
                USDT_FUTURES, 2, contract_code="ETH-USDT-231117", contract_status=7, delivery_time="1700208000000"
            ),
        ]
        out = _by_id(_usdt_futures(_with_rows(USDT_FUTURES, rows)))
        assert set(out) == {"BTC-USDT-261030", "ETH-USDT-231117"}
        assert "end_date" not in out["BTC-USDT-261030"]
        # Settled and past its delivery time.
        assert out["ETH-USDT-231117"]["end_date"] == "2023-11-17T08:00:00.000Z"

    @pytest.mark.parametrize(
        ("sid", "internal_id"),
        [
            ("BTC-USDT-211231", "future-BTC-USDT:USDT-20211231"),
            ("DOSE-USDT-250103", "future-DOSE-USDT:USDT-20250103"),
        ],
    )
    def test_tardis_only_ids(self, sid, internal_id):
        assert parse_htx_usdt_futures("htx-futures-usdt", {"id": sid, "type": "future"}).internal_id == internal_id

    @pytest.mark.parametrize("sid", ["BTC-USDT", "BTC-USDT-CW", "BTC-USDT-261399"])
    def test_unparseable_tardis_ids_are_skipped(self, sid):
        with pytest.raises(SkipSymbol):
            parse_htx_usdt_futures("htx-futures-usdt", {"id": sid, "type": "future"})

    def test_malformed_payload_raises(self):
        with pytest.raises(ValueError):
            _usdt_futures({"status": "ok", "data": {"contract_code": "BTC-USDT-261023"}})
