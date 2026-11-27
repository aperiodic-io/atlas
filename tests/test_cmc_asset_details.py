import json

import pytest
import requests

from integrations.cmc_asset_details import (
    CMC_DETAIL_URL,
    CmcAssetDetail,
    fetch_cmc_asset_details,
    load_detail_cache,
    parse_cmc_asset_detail,
    save_detail_cache,
)


def _payload(cmc_id=1, **overrides):
    data = {
        "id": cmc_id,
        "name": "Bitcoin",
        "symbol": "BTC",
        "slug": "bitcoin",
        "category": "coin",
        "status": "active",
        "dateAdded": "2010-07-13T00:00:00.000Z",
        "statistics": {"rank": 1, "price": 81000.5},
        "tags": [
            {"name": "Layer 1", "category": "CATEGORY"},
            {"name": "Memes", "category": "INDUSTRY"},
            {"name": "PoW", "category": "ALGORITHM"},
        ],
    }
    data.update(overrides)
    return {"data": data}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.headers = {"Retry-After": "0"}

    def raise_for_status(self):
        if self.status >= 400:
            error = requests.HTTPError(str(self.status))
            error.response = self
            raise error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        cmc_id = kwargs["params"]["id"]
        self.calls.append((url, cmc_id))
        queue = self.responses[cmc_id]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def test_parse_detail_reads_identity_rank_price_and_categories():
    detail = parse_cmc_asset_detail(_payload(), 1)
    assert detail == CmcAssetDetail(
        cmc_id=1,
        name="Bitcoin",
        symbol="BTC",
        slug="bitcoin",
        kind="coin",
        status="active",
        date_added=detail.date_added,
        rank=1,
        price_usd=81000.5,
        categories=("Layer 1", "Memes"),
        tags=("Layer 1", "Memes", "PoW"),
    )
    assert detail.date_added.year == 2010
    assert not detail.is_fiat


def test_parse_detail_marks_fiat():
    payload = _payload(2781, name="United States Dollar", symbol="USD", category="fiat", tags=[])
    assert parse_cmc_asset_detail(payload, 2781).is_fiat


def test_parse_detail_rejects_a_mismatched_id():
    with pytest.raises(ValueError):
        parse_cmc_asset_detail(_payload(2), 1)


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": {"id": 1}}])
def test_parse_detail_rejects_incomplete_payloads(payload):
    with pytest.raises(ValueError):
        parse_cmc_asset_detail(payload, 1)


def test_parse_detail_tolerates_missing_optional_fields():
    payload = _payload(statistics=None, tags=None, dateAdded=None, category=None)
    detail = parse_cmc_asset_detail(payload, 1)
    assert detail.rank is None
    assert detail.price_usd is None
    assert detail.categories == ()
    assert detail.date_added is None
    assert detail.kind == ""


def test_fetch_details_collects_failures_without_aborting(monkeypatch):
    monkeypatch.setattr("integrations.cmc_asset_details.time.sleep", lambda _: None)
    session = FakeSession(
        {
            1: [FakeResponse(_payload(1))],
            2: [FakeResponse({}, status=500)],
            3: [FakeResponse({}, status=429), FakeResponse(_payload(3, symbol="X"))],
        }
    )
    result = fetch_cmc_asset_details(
        session, {3, 1, 2}, max_attempts=2, min_interval_seconds=0
    )
    assert sorted(result.details) == [1, 3]
    assert result.details[3].symbol == "X"
    assert sorted(result.failures) == [2]
    assert all(url == CMC_DETAIL_URL for url, _ in session.calls)


def test_fetch_details_rejects_invalid_ids():
    with pytest.raises(ValueError):
        fetch_cmc_asset_details(FakeSession({}), {0}, min_interval_seconds=0)


def test_detail_cache_round_trip(tmp_path):
    path = tmp_path / "details.json"
    details = {1: parse_cmc_asset_detail(_payload(), 1)}
    save_detail_cache(path, details)
    assert load_detail_cache(path) == details
    assert json.loads(path.read_text())["schema_version"] == 1


def test_detail_cache_missing_file_is_empty(tmp_path):
    assert load_detail_cache(tmp_path / "nope.json") == {}
