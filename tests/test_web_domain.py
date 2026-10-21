"""Host normalisation is part of the map's published format.

Ranking lists (Tranco, Umbrella, ...) are matched to map entries by this
normalised domain, so any consumer must normalise hosts identically; a single
disagreement silently drops an asset's rows. These are the rule's test vectors,
plus a file of expected outputs for 3,467 real website strings
(``fixtures/web/psl_parity.tsv``: input, host, registrable domain).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from integrations.web.domain import is_blocked, normalize_host, registrable

PARITY = Path(__file__).parent / "fixtures" / "web" / "psl_parity.tsv"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("https://www.Tesla.com/en_us?x=1", "tesla.com"),
        ("http://user:pw@solana.com:8080/path", "solana.com"),
        ("WWW.BINANCE.COM.", "binance.com"),
        ("https://m.cricbuzz.com", "m.cricbuzz.com"),
        ("bitcoin.org", "bitcoin.org"),
    ],
)
def test_strips_everything_but_the_host(raw: str, want: str) -> None:
    assert normalize_host(raw) == want


@pytest.mark.parametrize(
    "raw", ["", "localhost", "https://", "domain", "a..b", "exa mple.com", ".com"]
)
def test_rejects_non_hosts(raw: str) -> None:
    # A CSV header or a junk website field must never become a matchable domain.
    assert normalize_host(raw) is None


def test_registrable_domain_respects_private_suffixes() -> None:
    assert registrable("app.uniswap.org") == "uniswap.org"
    assert registrable("news.bbc.co.uk") == "bbc.co.uk"
    # github.io is a private suffix: each user site is its own registrable domain.
    assert registrable("foo.github.io") == "foo.github.io"
    # A bare public suffix names no site.
    assert registrable("co.uk") is None


def test_expected_outputs_for_real_websites() -> None:
    lines = PARITY.read_text(encoding="utf-8").rstrip("\n").split("\n")
    assert len(lines) > 3000
    mismatches = []
    for line in lines:
        raw, host, domain = line.split("\t")
        got_host = normalize_host(raw)
        got_domain = registrable(got_host) if got_host else None
        if (got_host or "-", got_domain or "-") != (host, domain):
            mismatches.append((raw, host, domain, got_host, got_domain))
    assert mismatches == []


def test_blocked_hosts_cover_their_subdomains_only() -> None:
    blocked = ["x.com", "gitbook.io"]
    assert is_blocked("x.com", blocked)
    assert is_blocked("docs.gitbook.io", blocked)
    assert not is_blocked("spacex.com", blocked)
    assert not is_blocked("x.com.evil.org", blocked)
