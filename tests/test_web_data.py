"""The committed overrides and map: what consumers load must be sound."""

from __future__ import annotations

import json

import pytest

from atlas.web import DOMAIN_MAP_PATH, OVERRIDES_PATH, load_domain_map
from integrations.web.domain import registrable
from integrations.web.sources import _EDITIONS
from integrations.web.overrides import load_overrides, parse_overrides


EDITIONS = set(_EDITIONS.values())


def test_null_forces_none_and_absent_keeps_the_lookup() -> None:
    # Why: "gold has no website" and "nothing said about SOL's website" must not
    # mean the same, or an article-only override would erase a found domain.
    o = parse_overrides(
        {
            "entries": {
                "XAU": {"domain": None, "wikipedia": "Gold", "reason": "commodity"},
                "SOL": {
                    "wikipedia": "Solana_(blockchain_platform)",
                    "reason": "article",
                },
            }
        }
    )
    assert o.entries["XAU"].forces_domain
    assert o.entries["XAU"].domain is None
    assert not o.entries["SOL"].forces_domain
    assert o.entries["SOL"].wikipedia == "Solana_(blockchain_platform)"


@pytest.mark.parametrize(
    "payload",
    [
        {"entries": {"BTC": {"domain": "bitcoin.org"}}},  # no reason
        {
            "entries": {"BTC": {"domain": "www.bitcoin.org", "reason": "r"}}
        },  # not registrable
        {
            "blocked_hosts": ["x.com"],
            "entries": {"BTC": {"domain": "x.com", "reason": "r"}},
        },
        {"entries": {"BTC": {"wikipedia": "Bit coin", "reason": "r"}}},
        {"entries": {"BTC": {"website": "bitcoin.org", "reason": "r"}}},
        {"aliases": {"A": "B", "B": "C"}},
        {
            "aliases": {"XBT": "BTC"},
            "entries": {"XBT": {"domain": "bitcoin.org", "reason": "r"}},
        },
    ],
)
def test_mistakes_in_the_overrides_fail_loudly(payload: dict) -> None:
    with pytest.raises(ValueError, match="overrides"):
        parse_overrides(payload)


def test_committed_overrides_are_consistent() -> None:
    o = load_overrides(OVERRIDES_PATH)
    assert o.is_blocked("twitter.com")
    assert o.is_blocked("pump.fun")
    assert o.is_blocked("docs.gitbook.io")
    assert not o.is_blocked("binance.com"), "an exchange token's site is its exchange"


def test_committed_map_is_what_consumers_expect() -> None:
    # Why: consumers parse this file in the published format; a builder
    # regression (wrong coin, a social profile, a duplicate ticker) must fail
    # here, not as missing BTC rows downstream.
    file = load_domain_map()
    assert file["schema_version"] == 1
    entries = file["entries"]
    tickers = [e["ticker"] for e in entries]
    assert tickers == sorted(tickers)
    assert len(set(tickers)) == len(tickers)
    overrides = load_overrides(OVERRIDES_PATH)
    for entry in entries:
        assert entry.get("domain") or entry.get("wikipedia"), entry["ticker"]
        if "domain" in entry:
            assert registrable(entry["domain"]) == entry["domain"], entry["ticker"]
            # A blocked host only for the platform's own token (PUMP, pump.fun).
            own = {f.domain for f in overrides.entries.values() if f.own_platform}
            if entry["domain"] not in own:
                assert not overrides.is_blocked(entry["domain"]), entry["ticker"]
        assert entry["verification"]["status"] in ("ok", "redirect", "flagged")
        assert entry["atlas_contracts"] > 0
        # Other-language articles: Wikipedia subdomains only, never English,
        # and only beside an English article.
        links = entry.get("wikipedia_sitelinks", {})
        assert not links or entry.get("wikipedia"), entry["ticker"]
        # Every key is an open <lang>.wikipedia.org edition of the sitematrix
        # snapshot (be-tarask, not be-x-old; no closed wikis).
        assert set(links) <= EDITIONS, (entry["ticker"], set(links) - EDITIONS)
        assert entry["atlas_symbols"]
    by_domain = {}
    for entry in entries:
        if "domain" in entry:
            by_domain.setdefault(entry["domain"], []).append(entry)
    for domain, ticker in [
        ("bitcoin.org", "BTC"),
        ("ethereum.org", "ETH"),
        ("solana.com", "SOL"),
        ("tesla.com", "TSLA"),
        ("nvidia.com", "NVDA"),
    ]:
        primary = max(by_domain[domain], key=lambda e: e["atlas_contracts"])
        assert primary["ticker"] == ticker, domain
    assert (
        DOMAIN_MAP_PATH.read_text(encoding="utf-8")
        == json.dumps(file, indent=2, ensure_ascii=False) + "\n"
    )
