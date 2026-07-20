import json
from types import SimpleNamespace

from integrations.cmc_category_metadata import (
    CMC_DETAIL_URL,
    _retry_delay,
    fetch_cmc_categories,
    populate_cmc_categories,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        cmc_id = kwargs["params"]["id"]
        tags = [
            {"name": "Layer 1", "category": "CATEGORY"},
            {"name": "PoW", "category": "ALGORITHM"},
        ]
        if cmc_id == 2:
            tags.append({"name": "DeFi", "category": "CATEGORY"})
        return FakeResponse({"data": {"tags": tags}})


def test_fetch_cmc_categories_uses_detail_endpoint_and_ids():
    session = FakeSession()

    assert fetch_cmc_categories(session, {2, 1}) == {
        1: ["Layer 1"],
        2: ["DeFi", "Layer 1"],
    }
    assert [call[0] for call in session.calls] == [CMC_DETAIL_URL, CMC_DETAIL_URL]
    assert [call[1]["params"] for call in session.calls] == [{"id": 1}, {"id": 2}]


def test_populate_cmc_categories_updates_rows_with_ids(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "binance-spot.json"
    target.write_text(
        json.dumps(
            [
                {"symbol": "BTC", "cmc_id": 1, "cmc_category": "coin"},
                {"symbol": "ETH", "cmc_id": 2, "cmc_category": "old"},
                {"symbol": "NONE"},
            ]
        )
    )

    def fake_fetch(_session, _cmc_ids, **_kwargs):
        return {1: ["Layer 1"], 2: ["DeFi"]}

    monkeypatch.setattr(
        "integrations.cmc_category_metadata.fetch_cmc_categories", fake_fetch
    )

    assert populate_cmc_categories(data_dir) == {
        "files": 1,
        "rows": 2,
        "updated": 2,
        "missing": 0,
    }
    rows = json.loads(target.read_text())
    assert rows[0]["category"] == ["Layer 1"]
    assert rows[1]["category"] == ["DeFi"]
    assert "cmc_category" not in rows[0]
    assert "cmc_category" not in rows[1]
    assert "category" not in rows[2]


def test_retry_delay_respects_retry_after_header():
    error = Exception()
    error.response = SimpleNamespace(headers={"Retry-After": "12"})

    assert _retry_delay(error, 0) == 12


def test_retry_delay_uses_exponential_backoff_and_is_bounded():
    assert _retry_delay(Exception(), 3) == 8

    error = Exception()
    error.response = SimpleNamespace(headers={"Retry-After": "999"})
    assert _retry_delay(error, 0) == 120
