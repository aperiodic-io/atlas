"""The map build end to end, with every upstream replaced by a fixture."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from integrations.web import cache
from integrations.web.build import (
    Budgets,
    Fetchers,
    Paths,
    assign_tickers,
    build,
    dumps_map,
)
from integrations.web.sources import ListedCoin
from integrations.web.verify import Check

NOW = datetime(2026, 10, 20, 4, 30, tzinfo=UTC)
STAMP = "2026-10-20T04:30:00Z"
BLOCKED = ["x.com", "linktr.ee", "binance.com/en/fan-token"]


def asset(asset_id: str, cls: str = "crypto", **kw: Any) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "ticker": kw.pop("ticker", asset_id.split(".")[0]),
        "name": kw.pop("name", asset_id),
        "asset_class": cls,
        "status": "active",
        **kw,
    }


REGISTRY = [
    asset("BTC", cmc_id=1, cmc_slug="bitcoin", name="Bitcoin"),
    asset("DAI", cmc_id=4943, cmc_slug="multi-collateral-dai", name="Dai"),
    asset("MKR", cmc_id=1518, cmc_slug="maker", name="Maker"),
    asset("FOO", cmc_id=10, cmc_slug="foo", name="Foo"),
    asset("PARK", cmc_id=11, cmc_slug="park", name="Park Coin"),
    asset("JUV", cmc_id=12, cmc_slug="juventus", name="Juventus Fan Token"),
    asset(
        "TSLA.TESLA_TOKENIZED",
        cmc_id=13,
        ticker="TSLA",
        name="Tesla Tokenized Stock",
        categories=["Tokenized Stock"],
    ),
    asset("TSLA", "equity", name="Tesla Inc (Derivatives)"),
    asset("MA", cmc_id=14, cmc_slug="mind-ai", name="Mind AI"),
    asset("MA.EQUITY", "equity", ticker="MA", name="MA"),
    asset("XAU", "commodity", name="Gold (Derivatives)"),
    asset("STX", cmc_id=4847, cmc_slug="stacks", name="Stacks"),
    asset(
        "STX.SEAGATE", "equity", ticker="STX", name="Seagate Technology (Derivatives)"
    ),
    asset("GONE", cmc_id=15, cmc_slug="gone", name="Gone"),
    asset("ODD", cmc_id=16, cmc_slug="odd", name="Odd"),
    asset("HIJ", cmc_id=17, cmc_slug="hij", name="Hijacked Coin"),
    asset("GEO", cmc_id=19, cmc_slug="geo", name="Geo Coin"),
    asset("LUCKY", cmc_id=18, cmc_slug="lucky", name="Lucky Casino"),
]

ROWS = {
    "binance-futures": [
        *[{"symbol": "BTC", "base_asset_id": "BTC"}] * 5,
        *[{"symbol": "DAI", "base_asset_id": "DAI", "end_date": "2023-01-01T00:00:00Z"}]
        * 3,
        {"symbol": "MKR", "base_asset_id": "MKR"},
        {"symbol": "FOO", "base_asset_id": "FOO"},
        {"symbol": "PARK", "base_asset_id": "PARK"},
        {"symbol": "JUV", "base_asset_id": "JUV"},
        {"symbol": "TSLA", "base_asset_id": "TSLA.TESLA_TOKENIZED"},
        *[{"symbol": "TSLA", "base_asset_id": "TSLA", "underlying": "equity"}] * 2,
        *[{"symbol": "MA", "base_asset_id": "MA"}] * 3,
        *[{"symbol": "MA", "base_asset_id": "MA.EQUITY", "underlying": "equity"}] * 2,
        {"symbol": "XAU", "base_asset_id": "XAU", "underlying": "commodity"},
        *[{"symbol": "STX", "base_asset_id": "STX"}] * 4,
        {"symbol": "STX", "base_asset_id": "STX.SEAGATE", "underlying": "equity"},
        {"symbol": "GONE", "base_asset_id": "GONE"},
        {"symbol": "ODD", "base_asset_id": "ODD"},
        {"symbol": "HIJ", "base_asset_id": "HIJ"},
        {"symbol": "GEO", "base_asset_id": "GEO"},
        {"symbol": "LUCKY", "base_asset_id": "LUCKY"},
    ],
    # A venue symbology does not cover yet.
    "mexc-perps": [
        {"symbol": "1000BTC"},
        {"symbol": "OLDCOIN", "end_date": "2022-01-01T00:00:00Z"},
        {"symbol": "OLDCOIN", "end_date": "2022-01-01T00:00:00Z"},
        {"symbol": "FAKECOIN"},
    ],
}

CMC = {
    1: ("Bitcoin", "bitcoin", "BTC", ["https://bitcoin.org/"], 1),
    4943: ("Dai", "multi-collateral-dai", "DAI", ["https://makerdao.com/"], 20),
    1518: ("Maker", "maker", "MKR", ["https://makerdao.com/"], 60),
    10: (
        "Foo",
        "foo",
        "FOO",
        ["https://linktr.ee/foo", "https://app.foo.finance/"],
        900,
    ),
    11: ("Park Coin", "park", "PARK", ["https://parkcoin.io"], 3000),
    12: ("Juventus Fan Token", "juventus", "JUV", ["https://www.juventus.com"], 400),
    13: ("Tesla Tokenized Stock", "tsla", "TSLA", ["https://issuer.example"], 1000),
    14: ("Mind AI", "mind-ai", "MA", ["https://mindai.io"], 3500),
    4847: ("Stacks", "stacks", "STX", ["https://stacks.co"], 80),
    15: ("Gone", "gone", "GONE", ["https://gone.io"], 2000),
    16: ("Odd", "odd", "ODD", ["https://unrelated.example"], 2500),
    17: ("Hijacked Coin", "hij", "HIJ", ["https://hijacked.example"], 2600),
    19: ("Geo Coin", "geo", "GEO", ["https://geo.example"], 2800),
    18: ("Lucky Casino", "lucky", "LUCKY", ["https://luckycasino.io"], 2700),
    500: ("Old Coin", "old-coin", "OLDCOIN", ["https://oldcoin.org"], 4000),
    501: ("Fake Coin", "fake-coin", "FAKECOIN", ["https://scam.example"], 5000),
}

PAGES = {
    "bitcoin.org": Check(
        "ok", STAMP, 200, "bitcoin.org", "Bitcoin - Open source P2P money"
    ),
    "makerdao.com": Check(
        "redirect", STAMP, 200, "sky.money", "Sky (formerly MakerDAO)"
    ),
    "geo.example": Check(
        "redirect", STAMP, 200, "elsewhere.example", "Not available in your region"
    ),
    "foo.finance": Check("redirect", STAMP, 200, "x.com", "Foo on X"),
    "parkcoin.io": Check(
        "parked", STAMP, 200, "sedo.com", "parkcoin.io", "lands on sedo.com"
    ),
    "mindai.io": Check("ok", STAMP, 200, "mindai.io", "Mind AI"),
    "stacks.co": Check("ok", STAMP, 200, "stacks.co", "Stacks"),
    "gone.io": Check("unreachable", STAMP, failures=1),
    "unrelated.example": Check(
        "ok", STAMP, 200, "unrelated.example", "Garden furniture"
    ),
    "oldcoin.org": Check("ok", STAMP, 200, "oldcoin.org", "Old Coin home"),
    # Cached before the casino rule existed: rules apply to cached titles too.
    "hijacked.example": Check(
        "ok", STAMP, 200, "hijacked.example", "Best Crypto Casinos 2026"
    ),
    "luckycasino.io": Check(
        "ok", STAMP, 200, "luckycasino.io", "Lucky Casino - Crypto Casino"
    ),
    "scam.example": Check("ok", STAMP, 200, "scam.example", "Win prizes"),
    "tesla.com": Check("ok", STAMP, 200, "tesla.com", "Electric Cars | Tesla"),
    "mastercard.com": Check("ok", STAMP, 200, "mastercard.com", "Mastercard"),
    "seagate.com": Check("ok", STAMP, 200, "seagate.com", "Seagate"),
}

WIKIDATA_EQUITIES = [
    {
        "ticker": "TSLA",
        "item": "http://www.wikidata.org/entity/Q478214",
        "label": "Tesla, Inc.",
        "site": "https://www.tesla.com/",
        "article": "https://en.wikipedia.org/wiki/Tesla,_Inc.",
        "etf": "false",
    },
    {
        "ticker": "MA",
        "item": "http://www.wikidata.org/entity/Q489097",
        "label": "Mastercard",
        "site": "https://www.mastercard.com",
        "article": "https://en.wikipedia.org/wiki/Mastercard",
        "etf": "false",
    },
    {
        "ticker": "STX",
        "item": "http://www.wikidata.org/entity/Q1056451",
        "label": "Seagate Technology",
        "site": "https://www.seagate.com",
        "article": "https://en.wikipedia.org/wiki/Seagate_Technology",
        "etf": "false",
    },
]
WIKIDATA_CRYPTO = [
    {
        "slug": "bitcoin",
        "article": "https://en.wikipedia.org/wiki/Bitcoin",
        "site": "https://bitcoin.org",
    },
    {
        "article": "https://en.wikipedia.org/wiki/Dai_(cryptocurrency)",
        "site": "https://makerdao.com",
    },
]
OVERRIDES = {
    "schema_version": 1,
    "blocked_hosts": BLOCKED,
    "aliases": {},
    "entries": {
        "XAU": {
            "asset_class": "commodity",
            "name": "Gold",
            "domain": None,
            "wikipedia": "Gold",
            "reason": "a commodity has no website",
        },
        "BTC": {
            "wikipedia": "Bitcoin_(cryptocurrency)",
            "reason": "test: an article-only override keeps the domain",
        },
        "ODD": {"domain": "odd.example", "reason": "test: CMC lists an unrelated site"},
    },
}


class FakeUpstream:
    def __init__(self, pages: Mapping[str, Check] | None = None) -> None:
        self.pages = dict(PAGES if pages is None else pages)
        self.calls: dict[str, int] = {}

    def count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def cmc_detail(self, cmc_id: int) -> dict[str, Any]:
        self.count("cmc_detail")
        name, slug, symbol, websites, rank = CMC[cmc_id]
        return {
            "name": name,
            "slug": slug,
            "symbol": symbol,
            "status": "active",
            "websites": websites,
            "rank": rank,
        }

    def cmc_listing(self) -> list[ListedCoin]:
        self.count("cmc_listing")
        listing = [
            ListedCoin(i, v[0], v[2], v[4], "2020-01-01T00:00:00Z")
            for i, v in CMC.items()
        ]
        listing.append(
            ListedCoin(502, "Fake Coin 2", "FAKECOIN", 5100, "2021-01-01T00:00:00Z")
        )
        return listing

    def sparql(self, query: str) -> list[dict[str, str]]:
        self.count("sparql")
        if "P249" in query:
            return [r for r in WIKIDATA_EQUITIES if f'"{r["ticker"]}"' in query]
        return WIKIDATA_CRYPTO if "P5777" not in query else []

    def titles(self, titles: Iterable[str]) -> dict[str, str | None]:
        self.count("titles")
        return {
            t: ("Dai_(stablecoin)" if t == "Dai_(cryptocurrency)" else t)
            for t in titles
        }

    def homepages(
        self, domains: Iterable[str], checked: str, previous: Mapping[str, Check]
    ) -> dict[str, Check]:
        self.count("homepages")
        out = {}
        for d in domains:
            page = self.pages.get(d, Check("ok", checked, 200, d, ""))
            if page.outcome == "unreachable":
                prior = previous.get(d)
                page = Check(
                    "unreachable",
                    checked,
                    failures=(prior.failures if prior else 0) + 1,
                    failed=checked[:10],
                )
            out[d] = page
        return out

    def coingecko_list(self) -> list[dict[str, str]]:
        self.count("coingecko_list")
        return [
            {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
            {"id": "stacks", "symbol": "stx", "name": "Stacks"},
        ]

    def coingecko_homepages(self, cg_id: str) -> list[str]:
        self.count("coingecko")
        return {
            "bitcoin": ["http://www.bitcoin.org"],
            "stacks": ["https://www.hiro.so"],
        }[cg_id]

    def fetchers(self) -> Fetchers:
        return Fetchers(
            self.cmc_detail,
            self.cmc_listing,
            self.sparql,
            self.titles,
            self.homepages,
            self.coingecko_list,
            self.coingecko_homepages,
            self.sitelinks,
        )

    def sitelinks(self, titles: Sequence[str]) -> dict[str, tuple[str, dict[str, str]]]:
        self.count("sitelinks")
        assert len(titles) <= 50
        known = {
            "Bitcoin_(cryptocurrency)": (
                "Q131723",
                # Raw Wikidata site ids; the closed iiwiki is left out of the map.
                {"dewiki": "Bitcoin", "zh_yuewiki": "比特幣", "iiwiki": "x"},
            ),
            "Gold": ("Q897", {"frwiki": "Or"}),
        }
        return {t: known[t] for t in titles if t in known}


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    data = tmp_path / "data"
    (data / "symbology").mkdir(parents=True)
    (data / "symbology" / "assets.json").write_text(
        json.dumps({"schema_version": 1, "assets": REGISTRY})
    )
    for venue, rows in ROWS.items():
        (data / f"{venue}.json").write_text(json.dumps(rows))
    out = data / "web"
    out.mkdir()
    (out / "overrides.json").write_text(json.dumps(OVERRIDES))
    return Paths(data=data, out=out)


def by_ticker(result_map: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["ticker"]: e for e in result_map["entries"]}


def test_build_maps_verifies_and_explains(paths: Paths) -> None:
    upstream = FakeUpstream()
    result = build(paths, upstream.fetchers(), NOW, Budgets())
    entries = by_ticker(result.map)
    unmapped = result.map["unmapped"]

    btc = entries["BTC"]
    assert (btc["asset_id"], btc["domain"], btc["method"]) == (
        "BTC",
        "bitcoin.org",
        "atlas_cmc_id",
    )
    # The article-only override kept the looked-up domain.
    assert btc["wikipedia"] == "Bitcoin_(cryptocurrency)"
    # The article in other Wikipedia editions, also for an override's article.
    assert btc["wikipedia_sitelinks"] == {"de": "Bitcoin", "zh-yue": "比特幣"}
    assert entries["XAU"]["wikipedia_sitelinks"] == {"fr": "Or"}
    # Atlas identity, and every contract counted (a 1000BTC row on an uncovered venue too).
    assert (btc["atlas_contracts"], btc["atlas_symbols"]) == (6, ["1000BTC", "BTC"])
    assert btc["verification"]["status"] == "ok"
    assert {"homepage_ok", "name_in_domain", "coingecko_agrees"} <= set(
        btc["verification"]["evidence"]
    )

    # makerdao.com now redirects to sky.money, whose page names Maker: MKR
    # follows the rebrand. Nothing there names Dai, so DAI keeps makerdao.com
    # and is flagged for review rather than moved on trust.
    dai, mkr = entries["DAI"], entries["MKR"]
    assert (mkr["domain"], mkr["verification"]["status"]) == ("sky.money", "redirect")
    assert mkr["verification"]["redirected_from"] == "makerdao.com"
    assert dai["domain"] == "makerdao.com"
    assert "redirect_unverified:sky.money" in dai["verification"]["flags"]
    # The article by domain goes to the domain's primary (DAI, most contracts)
    # only, under Wikipedia's canonical title.
    assert dai["wikipedia"] == "Dai_(stablecoin)"
    assert "wikipedia" not in mkr

    # A redirect whose target does not name the asset (a geo-redirect, a sold
    # domain) is not followed: the domain stays, flagged for review.
    geo = entries["GEO"]
    assert geo["domain"] == "geo.example"
    assert "redirect_unverified:elsewhere.example" in geo["verification"]["flags"]

    # A redirect onto a blocked host keeps the asset's own domain, flagged.
    foo = entries["FOO"]
    assert foo["domain"] == "foo.finance"
    assert "redirects_to_blocked:x.com" in foo["verification"]["flags"]

    # Parked domains and borrowed sites are not mapped, with the reason.
    assert unmapped["PARK"].startswith("parkcoin.io: lands on sedo.com")
    assert "fan token" in unmapped["JUV"]
    assert "issuer" in unmapped["TSLA.TESLA_TOKENIZED"]

    # A casino page is spam unless it names the asset.
    assert unmapped["HIJ"].startswith("hijacked.example: gambling content")
    assert entries["LUCKY"]["domain"] == "luckycasino.io"

    # One unreachable check is a flag, not a verdict.
    assert entries["GONE"]["verification"]["flags"] == ["unreachable"]

    # The override wins over CMC and over evidence.
    odd = entries["ODD"]
    assert (odd["domain"], odd["method"], odd["verification"]["evidence"][0]) == (
        "odd.example",
        "override",
        "override",
    )

    # A commodity's article override.
    assert entries["XAU"] == {
        **entries["XAU"],
        "asset_class": "commodity",
        "wikipedia": "Gold",
    }
    assert "domain" not in entries["XAU"]


def test_ticker_collisions_follow_the_company_vs_coin_rule(paths: Paths) -> None:
    result = build(paths, FakeUpstream().fetchers(), NOW, Budgets())
    entries = by_ticker(result.map)
    # Mind AI (rank 3500) has more contracts but is no significant coin: MA is
    # Mastercard. Mind AI's asset_id is the bare ticker, so it is published as
    # MA.CRYPTO rather than dropped or duplicated.
    assert entries["MA"]["asset_id"] == "MA.EQUITY"
    assert entries["MA"]["domain"] == "mastercard.com"
    assert entries["MA.CRYPTO"]["asset_id"] == "MA"
    # Stacks (rank 80, more contracts) keeps STX; Seagate takes its asset_id.
    assert entries["STX"]["asset_id"] == "STX"
    assert entries["STX.SEAGATE"]["domain"] == "seagate.com"
    # CoinGecko lists another site for Stacks: flagged, not changed.
    assert "coingecko_disagrees:hiro.so" in entries["STX"]["verification"]["flags"]


def test_symbol_fallback_needs_identity_evidence(paths: Paths) -> None:
    # Why: a coin Atlas could not identify is only mapped through a unique CMC
    # symbol match *and* a page that names it; a wrong site is worse than none.
    result = build(paths, FakeUpstream().fetchers(), NOW, Budgets())
    entries = by_ticker(result.map)
    old = entries["OLDCOIN"]
    assert (old["domain"], old["method"], old.get("asset_id")) == (
        "oldcoin.org",
        "cmc_symbol_unique",
        None,
    )
    # Two CMC coins share FAKECOIN with no dominant one: not guessed.
    assert "ambiguous" in result.map["unmapped"]["FAKECOIN"]


def test_dead_after_two_runs_and_caches_spare_upstream(paths: Paths) -> None:
    upstream = FakeUpstream()
    build(paths, upstream.fetchers(), NOW, Budgets())
    first = dict(upstream.calls)
    # The second strike must fall on another day.
    second = build(
        paths,
        upstream.fetchers(),
        NOW + timedelta(days=1),
        Budgets(cmc_refresh=0, homepage_refresh=0, coingecko=0),
    )
    # Every CMC detail was cached by the first run.
    assert upstream.calls["cmc_detail"] == first["cmc_detail"]
    # The unreachable domain was retried and is now dead.
    assert "GONE" not in by_ticker(second.map)
    assert "unreachable on 2 days in a row" in second.map["unmapped"]["GONE"]
    homepages = cache.load(paths.cache("homepages"))
    assert homepages["gone.io"]["failures"] == 2


def test_offline_rebuild_is_identical(paths: Paths) -> None:
    online = build(paths, FakeUpstream().fetchers(), NOW, Budgets())
    offline = build(paths, None, NOW, Budgets())
    assert dumps_map(offline.map) == dumps_map(online.map)


def test_map_file_is_stable_sorted_and_keeps_the_rust_field_order(paths: Paths) -> None:
    result = build(paths, FakeUpstream().fetchers(), NOW, Budgets())
    text = dumps_map(result.map)
    file = json.loads(text)
    assert list(file) == [
        "schema_version",
        "generated_at",
        "atlas_source",
        "entries",
        "unmapped",
    ]
    tickers = [e["ticker"] for e in file["entries"]]
    assert tickers == sorted(tickers)
    assert len(set(tickers)) == len(tickers)
    assert list(file["unmapped"]) == sorted(file["unmapped"])
    known = [
        "ticker",
        "asset_id",
        "asset_class",
        "name",
        "domain",
        "wikipedia",
        "wikipedia_sitelinks",
        "atlas_symbols",
        "atlas_contracts",
        "cmc_id",
        "wikidata",
        "method",
        "verification",
    ]
    for entry in file["entries"]:
        assert [k for k in known if k in entry] == list(entry)
    assert text.endswith("}\n")
    assert '\n  "entries": [\n    {\n      "ticker"' in text


def test_assign_tickers_prefers_contracts_then_rank() -> None:
    def entry(
        key: str, contracts: int, cmc_id: int | None, cls: str = "crypto"
    ) -> dict[str, Any]:
        return {
            "_key": key,
            "asset_id": key,
            "ticker": key.split(".")[0],
            "asset_class": cls,
            "atlas_contracts": contracts,
            "cmc_id": cmc_id,
        }

    kept, dropped = assign_tickers(
        [
            entry("AI", 26, 1),
            entry("AI.SLEEPLESS_AI", 14, 2),
            entry("VELO.VELODROME", 15, 3),
            entry("VELO", 15, 4),
        ],
        {1: 500, 2: 900, 3: 300, 4: 700},
    )
    tickers = {e["_key"]: e["ticker"] for e in kept}
    assert tickers == {
        "AI": "AI",
        "AI.SLEEPLESS_AI": "AI.SLEEPLESS_AI",
        "VELO.VELODROME": "VELO",
        "VELO": "VELO.CRYPTO",
    }
    assert dropped == {}


def test_coingecko_vouches_for_the_domain_it_lists_not_for_a_redirect_target() -> None:
    # Why: starlproject.com, listed by CMC and CoinGecko alike, now forwards to
    # an unrelated site; agreement on the old domain says nothing about the new.
    from integrations.web.build import Lookup, identity
    from integrations.web.universe import Asset

    asset = Asset(
        key="STARL",
        asset_id=None,
        ticker="STARL",
        asset_class="crypto",
        name="Starlink",
    )
    moved = Lookup(
        name="Starlink",
        domain="spam.example",
        redirected_from="starlproject.com",
        method="cmc_symbol_unique",
    )
    identity(moved, asset, ["https://starlproject.com"])
    assert "coingecko_agrees" not in moved.evidence
    assert "no_identity_evidence" in moved.flags
    same = Lookup(name="Bitcoin", domain="bitcoin.org", method="atlas_cmc_id")
    identity(same, asset, ["https://www.bitcoin.org/en/"])
    assert "coingecko_agrees" in same.evidence
