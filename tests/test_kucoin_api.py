"""KuCoin spot from the exchange API (`kucoin-spot`).

Fixtures under `tests/fixtures/kucoin/` are excerpts of the real
`/api/v2/symbols` and `/api/v3/currencies` responses and of Tardis's `kucoin`
symbol list, captured on 2026-10-20.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.contracts import ContractType
from atlas.exchange_definitions.common import SkipSymbol
from atlas.exchange_definitions.kucoin import fetch_kucoin_spot, parse_kucoin, parse_kucoin_spot

_FIXTURES = Path(__file__).parent / "fixtures" / "kucoin"
SLUG = "kucoin-spot"


def _load(name: str):
    return json.loads((_FIXTURES / name).read_text())


SYMBOLS = _load("symbols.json")
CURRENCIES = _load("currencies.json")
TARDIS = _load("tardis.json")


def _response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _fetch(symbols=SYMBOLS, currencies=CURRENCIES) -> list[dict]:
    def get(url, params=None, timeout=None):
        if url.endswith("/api/v2/symbols"):
            return _response(symbols)
        if url.endswith("/api/v3/currencies"):
            return _response(currencies)
        raise AssertionError(f"unexpected request {url}")

    with patch("atlas.exchange_definitions.http.requests.get", side_effect=get):
        return fetch_kucoin_spot(timeout_seconds=5)


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {sd["id"]: sd for sd in rows}


def _with(symbol: str, **changes) -> dict:
    payload = copy.deepcopy(SYMBOLS)
    for item in payload["data"]:
        if item["symbol"] == symbol:
            item.update(changes)
    return payload


def _internal(sd: dict) -> str:
    return parse_kucoin_spot(SLUG, sd).internal_id


class TestFetch:
    def test_every_listed_pair_is_returned_with_its_tardis_id(self):
        rows = _fetch()
        assert [sd["id"] for sd in rows] == [item["symbol"] for item in SYMBOLS["data"]]
        assert all(sd["type"] == "spot" for sd in rows)

    def test_internal_ids_and_legs(self):
        rows = _by_id(_fetch())
        assert {i: _internal(sd) for i, sd in rows.items()} == {
            "BTC-USDT": "spot-BTC-USDT",
            "ETH-BTC": "spot-ETH-BTC",
            "BTC-EUR": "spot-BTC-EUR",
            "SOL-KCS": "spot-SOL-KCS",
            "BTC-USD1": "spot-BTC-USD1",
            "SHIB-DOGE": "spot-SHIB-DOGE",
            "PAXG-USDT": "spot-PAXG-USDT",
            "AAPLX-USDT": "spot-AAPLX-USDT",
            "TSLAX-USDT": "spot-TSLAX-USDT",
            "SPCXX-USDT": "spot-SPCXX-USDT",
            "POLS-USDT": "spot-POLS-USDT",
            "BSV-USDT": "spot-BSV-USDT",
            "BNRENSHENG-USDT": "spot-BNRENSHENG-USDT",
            "AVA-USDT": "spot-AVA-USDT",
            "USDT-EUR": "spot-USDT-EUR",
        }
        assert rows["ETH-BTC"]["base_coin"] == "ETH"
        assert rows["ETH-BTC"]["quote_coin"] == "BTC"

    def test_spot_rows_carry_no_derivative_fields(self):
        for sd in _fetch():
            contract = parse_kucoin_spot(SLUG, sd)
            assert contract.instrument_type == ContractType.spot
            assert contract.margin is None
            assert contract.contract_size is None
            assert not {"margin_asset", "contract_size", "delivery_date", "end_date"} & set(sd)

    def test_chinese_named_pair_uses_the_symbol_code(self):
        # `name` is `币安人生-USDT`; the base code is the ASCII `BNRENSHENG`.
        sd = _by_id(_fetch())["BNRENSHENG-USDT"]
        assert sd["base_coin"] == "BNRENSHENG"

    def test_first_capture_is_the_trading_start(self):
        rows = _by_id(_fetch())
        assert rows["BTC-EUR"]["first_capture"] == "2023-03-03T07:00:00.000Z"
        # A relaunched pair starts at its relaunch.
        assert rows["BSV-USDT"]["first_capture"] == "2026-09-22T09:00:00.000Z"
        # Old pairs have no `tradingStartTime`.
        assert "first_capture" not in rows["BTC-USDT"]
        assert "first_capture" not in rows["AVA-USDT"]

    def test_no_none_values(self):
        for sd in _fetch():
            assert None not in sd.values()


class TestUnderlying:
    def test_classes(self):
        rows = _by_id(_fetch())
        assert {i: sd["underlying"] for i, sd in rows.items()} == {
            "BTC-USDT": "crypto",
            "ETH-BTC": "crypto",
            "BTC-EUR": "crypto",
            "SOL-KCS": "crypto",
            "BTC-USD1": "crypto",
            "SHIB-DOGE": "crypto",
            # KuCoin files tokenized gold with crypto.
            "PAXG-USDT": "crypto",
            "AAPLX-USDT": "equity",
            "TSLAX-USDT": "equity",
            # Filed under `USDS`, but KuCoin names it `SpaceX xStock`.
            "SPCXX-USDT": "equity",
            "POLS-USDT": "crypto",
            "BSV-USDT": "crypto",
            "BNRENSHENG-USDT": "crypto",
            "AVA-USDT": "crypto",
            "USDT-EUR": "crypto",
        }

    def test_stocks_market_is_equity(self):
        assert _by_id(_fetch(_with("BTC-USDT", market="Stocks")))["BTC-USDT"]["underlying"] == "equity"

    def test_rwa_market_is_unknown(self):
        assert _by_id(_fetch(_with("BTC-USDT", market="RWA")))["BTC-USDT"]["underlying"] == "unknown"

    def test_unknown_market_is_unknown_not_crypto(self):
        assert _by_id(_fetch(_with("BTC-USDT", market="Bonds")))["BTC-USDT"]["underlying"] == "unknown"

    def test_every_published_theme_zone_is_crypto(self):
        for market in ["Majors", "New", "TON", "Meme", "Solana", "AI", "DeFi", "Layer1", "Layer2", "NFT",
                       "DePIN", "PoW", "GameFi", "Storage", "Payment", "USDS", "KCS", "FIAT", "VR-AR",
                       "BTC", "ALTS"]:
            row = _by_id(_fetch(_with("AVA-USDT", market=market)))["AVA-USDT"]
            assert row["underlying"] == "crypto", market


class TestLifecycle:
    def test_halted_pair_stays_listed(self):
        row = _by_id(_fetch(_with("BTC-USDT", enableTrading=False)))["BTC-USDT"]
        assert "end_date" not in row

    def test_special_treatment_pair_stays_listed(self):
        rows = _by_id(_fetch())
        assert next(i for i in SYMBOLS["data"] if i["symbol"] == "POLS-USDT")["st"] is True
        assert "end_date" not in rows["POLS-USDT"]

    def test_pair_not_yet_trading_is_flagged_upcoming(self):
        future = int((datetime.now(UTC) + timedelta(days=2)).timestamp() * 1000)
        row = _by_id(_fetch(_with("BTC-USDT", tradingStartTime=future)))["BTC-USDT"]
        assert row == {"id": "BTC-USDT", "type": "spot", "upcoming": True}

    def test_pair_in_opening_call_auction_is_flagged_upcoming(self):
        row = _by_id(_fetch(_with("AVA-USDT", callauctionIsEnabled=True)))["AVA-USDT"]
        assert row == {"id": "AVA-USDT", "type": "spot", "upcoming": True}

    def test_pair_that_started_trading_is_kept(self):
        past = int((datetime.now(UTC) - timedelta(minutes=5)).timestamp() * 1000)
        row = _by_id(_fetch(_with("AVA-USDT", tradingStartTime=past)))["AVA-USDT"]
        assert not row.get("upcoming")
        assert row["base_coin"] == "AVA"

    def test_fetch_never_sets_end_date(self):
        # KuCoin has no delisted status: delisted pairs drop out of the listing.
        assert all("end_date" not in sd for sd in _fetch())


class TestMalformed:
    @pytest.mark.parametrize(
        "payload",
        [
            {"code": "400100", "msg": "error"},
            {"code": "200000", "data": None},
            {"code": "200000", "data": {"symbol": "BTC-USDT"}},
            [],
            "oops",
        ],
    )
    def test_bad_symbols_payload_raises(self, payload):
        with pytest.raises(ValueError):
            _fetch(symbols=payload)

    def test_bad_currencies_payload_raises(self):
        with pytest.raises(ValueError):
            _fetch(currencies={"code": "429000", "msg": "too many requests"})


class TestTardisFallback:
    def test_every_tardis_row_parses(self):
        for row in TARDIS:
            contract = parse_kucoin_spot(SLUG, {"id": row["id"], "type": row["type"]})
            assert contract.instrument_type == ContractType.spot

    def test_tardis_rows_match_api_rows(self):
        api = {sd["id"]: _internal(sd) for sd in _fetch()}
        tardis = {row["id"]: _internal({"id": row["id"], "type": "spot"}) for row in TARDIS}
        shared = set(api) & set(tardis)
        assert shared == {"BTC-USDT", "ETH-BTC", "BTC-EUR", "SOL-KCS", "BTC-USD1"}
        assert {i: api[i] for i in shared} == {i: tardis[i] for i in shared}

    def test_renamed_asset_codes_map_to_their_tickers(self):
        # KuCoin renamed `BCHSV-USDT` to `BSV-USDT` on 2026-09-22.
        assert _internal({"id": "BCHSV-USDT", "type": "spot"}) == "spot-BSV-USDT"
        assert _internal({"id": "BCHSV-BTC", "type": "spot"}) == "spot-BSV-BTC"
        assert _internal({"id": "WAX-USDT", "type": "spot"}) == "spot-WAXP-USDT"
        assert _internal({"id": "LOKI-BTC", "type": "spot"}) == "spot-OXEN-BTC"
        assert _internal({"id": "R-USDT", "type": "spot"}) == "spot-REV-USDT"

    def test_other_history_is_parsed_verbatim(self):
        assert _internal({"id": "MATIC-USDT", "type": "spot"}) == "spot-MATIC-USDT"
        assert _internal({"id": "BTC-DAI", "type": "spot"}) == "spot-BTC-DAI"

    def test_api_codes_are_renamed_too(self):
        payload = _with("BTC-USDT", baseCurrency="BCHSV")
        row = _by_id(_fetch(payload))["BTC-USDT"]
        assert row["base_coin"] == "BSV"

    @pytest.mark.parametrize("sid", ["BTCUSDT", "BTC-USDT-SWAP", "-USDT", "BTC-"])
    def test_malformed_ids_are_skipped(self, sid):
        with pytest.raises(SkipSymbol):
            parse_kucoin_spot(SLUG, {"id": sid, "type": "spot"})

    def test_original_id_is_preserved(self):
        assert parse_kucoin_spot(SLUG, {"id": "BCHSV-USDT", "type": "spot"}).original_id == "BCHSV-USDT"


def test_legacy_kucoin_parser_is_unchanged():
    assert parse_kucoin("kucoin", {"id": "BCHSV-USDT", "type": "spot"}).internal_id == "spot-BCHSV-USDT"
