"""Per-language Wikipedia articles (``wikipedia_sitelinks``) for the web
ranking dataset's pageviews in every Wikipedia edition."""

from __future__ import annotations

from typing import Any

import pytest

from integrations.web import sources


@pytest.mark.parametrize(
    ("site", "lang"),
    [
        ("dewiki", "de"),
        ("zh_yuewiki", "zh-yue"),
        # Its host is be-tarask.wikipedia.org, not be-x-old.
        ("be_x_oldwiki", "be-tarask"),
        # Closed editions have no readers.
        ("iiwiki", None),
        ("simplewiki", "simple"),
        ("enwiki", None),
        ("commonswiki", None),
        ("specieswiki", None),
        ("metawiki", None),
        ("wikidatawiki", None),
        ("sourceswiki", None),
        ("mediawikiwiki", None),
        ("abstractwiki", None),
        ("dewikinews", None),
        ("frwikiquote", None),
    ],
)
def test_only_wikipedia_language_editions_are_kept(site: str, lang: str | None) -> None:
    # Why: the consumer reads <lang>.wikipedia.org pageviews; Commons, Wikinews
    # or Wikiquote pages are other projects, and English is the existing field.
    assert sources.sitelink_lang(site) == lang


def test_wbgetentities_sitelinks_are_keyed_by_the_english_title() -> None:
    # Why: the map's articles are English titles with underscores; Wikidata
    # answers with spaces, and a title it cannot find has no entity.
    payload = {
        "entities": {
            "Q131821993": {
                "id": "Q131821993",
                "sitelinks": {
                    "enwiki": {"site": "enwiki", "title": "$Trump"},
                    "dewiki": {"site": "dewiki", "title": "$Trump"},
                    "zh_yuewiki": {"site": "zh_yuewiki", "title": "特朗普幣"},
                    "commonswiki": {"site": "commonswiki", "title": "Category:$Trump"},
                },
            },
            "Q478214": {
                "id": "Q478214",
                "sitelinks": {
                    "enwiki": {"site": "enwiki", "title": "Tesla, Inc."},
                    "jawiki": {"site": "jawiki", "title": "テスラ (企業)"},
                },
            },
            "-1": {"site": "enwiki", "title": "No such page", "missing": ""},
        }
    }
    got = sources.parse_sitelinks(payload)
    # The cache keeps raw site ids; the map shows wikipedia.org subdomains.
    assert got["$Trump"][0] == "Q131821993"
    assert sources.wikipedia_sitelinks(got["$Trump"][1]) == {
        "de": "$Trump",
        "zh-yue": "特朗普幣",
    }
    # Titles stay as Wikidata gives them (spaces); consumers add underscores.
    assert got["Tesla,_Inc."] == ("Q478214", {"jawiki": "テスラ (企業)"})
    assert "No_such_page" not in got


def test_maxlag_is_honoured() -> None:
    # Why: Wikidata asks bots to back off when replication lags (maxlag=5);
    # the client waits Retry-After and asks again instead of hammering.
    answers: list[dict[str, Any]] = [
        {"error": {"code": "maxlag", "info": "Waiting for a server: 6 seconds lagged"}},
        {"entities": {}},
    ]
    waits: list[float] = []

    class Response:
        def __init__(self, body: dict[str, Any]) -> None:
            self.body = body
            self.headers = {"Retry-After": "2"}

        def json(self) -> dict[str, Any]:
            return self.body

    class FakeClient:
        def get(
            self, url: str, params: dict[str, Any] | None = None, **kwargs: Any
        ) -> Response:
            assert params is not None
            assert params["maxlag"] == 5
            assert params["sites"] == "enwiki"
            assert params["props"] == "sitelinks"
            return Response(answers.pop(0))

    assert sources.fetch_sitelinks(FakeClient(), ["Bitcoin"], sleep=waits.append) == {}  # type: ignore[arg-type]
    assert waits == [2.0]
