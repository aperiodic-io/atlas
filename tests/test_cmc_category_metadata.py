import json

from integrations.cmc_category_metadata import (
    CMC_DETAIL_URL,
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
        return FakeResponse({"data": {"category": "coin" if cmc_id == 1 else "token"}})


def test_fetch_cmc_categories_uses_detail_endpoint_and_ids():
    session = FakeSession()

    assert fetch_cmc_categories(session, {2, 1}) == {1: "coin", 2: "token"}
    assert [call[0] for call in session.calls] == [CMC_DETAIL_URL, CMC_DETAIL_URL]
    assert [call[1]["params"] for call in session.calls] == [{"id": 1}, {"id": 2}]


def test_populate_cmc_categories_updates_rows_with_ids(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "binance-spot.json"
    target.write_text(
        json.dumps(
            [
                {"symbol": "BTC", "cmc_id": 1},
                {"symbol": "ETH", "cmc_id": 2, "cmc_category": "old"},
                {"symbol": "NONE"},
            ]
        )
    )

    def fake_fetch(_session, _cmc_ids, **_kwargs):
        return {1: "coin", 2: "token"}

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
    assert rows[0]["cmc_category"] == "coin"
    assert rows[1]["cmc_category"] == "token"
    assert "cmc_category" not in rows[2]
