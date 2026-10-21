"""Homepage checks: is the domain a live site of its own, and is it the asset's?"""

from __future__ import annotations

import pytest
import requests

from integrations.web import verify

DAY = "2026-10-20T04:30:00Z"


def page(title: str, body: str = "") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def test_a_live_page_on_its_own_domain_is_ok() -> None:
    check = verify.classify(
        "uniswap.org",
        200,
        "https://app.uniswap.org/swap",
        page("Uniswap Interface"),
        DAY,
    )
    assert (check.outcome, check.final_domain, check.title) == (
        "ok",
        "uniswap.org",
        "Uniswap Interface",
    )


def test_a_redirect_to_another_registrable_domain_is_a_rebrand() -> None:
    # makerdao.com now sends visitors to sky.money; the map must follow.
    check = verify.classify("makerdao.com", 200, "https://sky.money/", page("Sky"), DAY)
    assert (check.outcome, check.final_domain) == ("redirect", "sky.money")


@pytest.mark.parametrize(
    ("final_url", "title", "body"),
    [
        ("https://sedo.com/search/details/?domain=foo.io", "foo.io", ""),
        ("https://foo.io/", "foo.io is for sale", ""),
        ("https://foo.io/", "foo.io", "<p>This domain may be for sale!</p>"),
        (
            "https://foo.io/",
            "",
            "<script src='https://www.dan.com/lander.js'></script>",
        ),
        (
            "https://foo.io/",
            "foo.io - This domain is parked free, courtesy of GoDaddy.com",
            "",
        ),
        ("https://foo.io/", "Welcome to nginx!", ""),
        ("https://foo.io/", "Account Suspended", ""),
    ],
)
def test_parking_for_sale_and_placeholder_pages_are_no_site(
    final_url: str, title: str, body: str
) -> None:
    # Why: a parked domain's popularity is the parking network's; ranking it as
    # the asset's site publishes noise as data.
    assert (
        verify.classify("foo.io", 200, final_url, page(title, body), DAY).outcome
        == "parked"
    )


@pytest.mark.parametrize(
    "title",
    [
        "WINLIVE4D: Link Situs Slot88 & Referensi Game Slot Gacor Malam Ini",
        "RUMAHMPO x MPO SLOT Rasakan Game Kekinian",
        "BERJAYATOGEL: Portal Akses Resmi Situs Togel Online",
        "High class escort Barcelona - 24/7 VIP Models",
        "POL88 Official Indonesia | Platform yang Mengutamakan Kualitas",
        "KACANG99 » Platform Game Online dengan Panduan Fitur",
    ],
)
def test_hijacked_project_domains_are_no_site(title: str) -> None:
    # Why: evmos.org and loopring.org were serving slot-machine spam; their
    # "popularity" is a spam network's, published as the coin's.
    assert verify.title_problem(title, "Evmos").startswith("hijacked")


def test_casino_pages_are_spam_unless_they_name_the_asset() -> None:
    # A gambling project's own site names it; an expired domain's casino
    # affiliate page does not.
    assert verify.title_problem(
        "Top Crypto Casinos - Instant Payout Sites", "Test"
    ).startswith("gambling")
    assert (
        verify.title_problem(
            "Bitcoin Casino - Gamble with Crypto at Lucky Block", "Lucky Block"
        )
        is None
    )
    assert (
        verify.title_problem(
            "BC Game: Crypto Casino Games & Casino Slot Games", "BC.GAME"
        )
        is None
    )
    assert verify.title_problem("SLERF — The Sloth Who Lost $10M", "Slerf") is None
    assert verify.title_problem(
        "Get getloop.network — Afternic / GoDaddy", "Loop"
    ).startswith("parking")


def test_an_ordinary_page_mentioning_parking_is_not_parked() -> None:
    body = "Liquidity parked in vaults earns yield. Domain names and wallets."
    assert (
        verify.classify(
            "foo.io", 200, "https://foo.io/", page("Foo Vaults", body), DAY
        ).outcome
        == "ok"
    )


def test_firewalls_and_error_pages_are_told_apart() -> None:
    assert (
        verify.classify(
            "foo.io", 403, "https://foo.io/", page("Just a moment..."), DAY
        ).outcome
        == "bot_blocked"
    )
    assert (
        verify.classify("foo.io", 404, "https://foo.io/", page("Oops"), DAY).outcome
        == "http_error"
    )


def test_a_domain_is_dead_only_after_two_unreachable_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Why: one flaky night must not unmap a site; two misses in a row do.
    def fail(session: requests.Session, url: str) -> tuple[int, str, str]:
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(verify, "_fetch", fail)
    first = verify.check_homepage("gone.io", DAY)
    assert (first.outcome, first.failures, first.dead) == ("unreachable", 1, False)
    second = verify.check_homepage("gone.io", "2026-10-21T04:30:00Z", previous=first)
    assert (second.failures, second.dead) == (2, True)
    # A success in between resets the count.
    ok = verify.Check("ok", DAY)
    assert verify.check_homepage("gone.io", DAY, previous=ok).failures == 1


def test_falls_back_to_http_then_www(monkeypatch: pytest.MonkeyPatch) -> None:
    tried = []

    def fetch(session: requests.Session, url: str) -> tuple[int, str, str]:
        tried.append(url)
        if url.startswith("https://www."):
            return 200, url, page("Foo")
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(verify, "_fetch", fetch)
    check = verify.check_homepage("foo.io", DAY)
    assert check.outcome == "ok"
    assert tried == ["https://foo.io/", "http://foo.io/", "https://www.foo.io/"]


def test_check_round_trips_through_the_cache() -> None:
    check = verify.Check("redirect", DAY, 200, "sky.money", "Sky", "", 0)
    assert verify.Check.from_json(check.to_json()) == check


@pytest.mark.parametrize(
    ("name", "ticker", "domain", "title", "want"),
    [
        (
            "Bitcoin",
            "BTC",
            "bitcoin.org",
            "Bitcoin - Open source P2P money",
            ["name_in_domain", "name_in_title"],
        ),
        ("Uniswap", "UNI", "uniswap.org", "", ["name_in_domain"]),
        ("Maker", "MKR", "sky.money", "Sky", []),
        (
            "Render",
            "RENDER",
            "rendernetwork.com",
            "Render Network",
            ["name_in_domain", "name_in_title"],
        ),
        ("Pudgy Penguins", "PENGU", "pudgypenguins.com", "", ["name_in_domain"]),
        ("Dogecoin", "DOGE", "dogecoin.com", "", ["name_in_domain"]),
        # Generic words alone are no evidence.
        ("Some Token", "STK", "token.com", "The token network", []),
    ],
)
def test_identity_evidence(
    name: str, ticker: str, domain: str, title: str, want: list[str]
) -> None:
    assert verify.name_matches(name, ticker, domain, title) == want


def test_titles_cached_with_the_wrong_charset_are_repaired() -> None:
    # Pages without a declared charset were read as Latin-1 before the fix.
    broken = "Hyperliquid \u2013 Infrastructure".encode().decode("latin-1")
    assert verify.fix_mojibake(broken) == "Hyperliquid \u2013 Infrastructure"
    assert verify.fix_mojibake("Plain ASCII") == "Plain ASCII"
    russian = "Домен не прилинкован ни к одной из директорий".encode().decode("latin-1")
    row = {"outcome": "ok", "checked": DAY, "title": russian}
    assert verify.title_problem(verify.Check.from_json(row).title, "Bulla").startswith(
        "placeholder"
    )
