"""Shared HTTP helper: retries transient upstream failures."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from atlas.exchange_definitions.http import get_json


def _response(status: int, payload: object = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status} Server Error")
    else:
        resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def test_recovers_after_a_run_of_503s():
    # Kraken Futures answered 503 to the CI runner for several seconds at a time.
    responses = [_response(503)] * 4 + [_response(200, {"ok": True})]
    with (
        patch("atlas.exchange_definitions.http.requests.get", side_effect=responses),
        patch.object(get_json.retry, "sleep", lambda _: None),
    ):
        assert get_json("https://example.test", 5) == {"ok": True}


def test_gives_up_on_a_persistent_failure():
    with (
        patch(
            "atlas.exchange_definitions.http.requests.get",
            side_effect=lambda *a, **k: _response(503),
        ),
        patch.object(get_json.retry, "sleep", lambda _: None),
        pytest.raises(requests.HTTPError),
    ):
        get_json("https://example.test", 5)

