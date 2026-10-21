"""Rules added after the re-audit of atlas#58 at bbecb05. Each test names the
real case that motivated it."""

from __future__ import annotations

from typing import Any

import pytest

from integrations.web import build, sources, verify
from integrations.web.overrides import parse_overrides
from integrations.web.universe import (
    RegistryAsset,
    build_universe,
    normalize_symbol,
)

DAY = "2026-10-21T04:30:00Z"


def page(title: str, body: str = "") -> str:
    head = f"<title>{title}</title>" if title else ""
    return f"<html><head>{head}</head><body>{body}</body></html>"


def test_bitget_r_prefixed_tokenised_stocks_are_the_underlying_ticker() -> None:
    # Why: Bitget lists Reality's tokenised stocks as rGS, rPM, rBA; keeping the
    # r matched them to Regis, RPM International and Ritchie Bros.
    row = {"symbol": "RGS", "base_coin": "rGS", "underlying": "equity"}
    assert normalize_symbol("bitget-spot", row) == "GS"
    # A real ticker starting with R keeps it, and other venues are untouched.
    assert (
        normalize_symbol(
            "bitget-spot", {"symbol": "RIO", "base_coin": "RIO", "underlying": "equity"}
        )
        == "RIO"
    )
    assert (
        normalize_symbol(
            "gateio-spot", {"symbol": "RGS", "base_coin": "rGS", "underlying": "equity"}
        )
        == "RGS"
    )
    # Rows Atlas identified keep Atlas's asset (Reality's Apple token).
    registry = {
        "RAAPL": RegistryAsset(
            "RAAPL", "rAAPL", "Apple Tokenized Stock (Reality)", "crypto", cmc_id=40598
        )
    }
    snapshots = {
        "bitget-spot": [
            {
                "symbol": "RAAPL",
                "base_coin": "rAAPL",
                "underlying": "equity",
                "base_asset_id": "RAAPL",
            },
            {"symbol": "RGS", "base_coin": "rGS", "underlying": "equity"},
        ]
    }
    universe = build_universe(registry, snapshots, {})
    assert universe.assets["RAAPL"].contracts == 1
    assert universe.assets["GS"].symbols == {"RGS"}


def test_a_redirect_to_the_same_brand_on_another_tld_is_not_a_rebrand() -> None:
    # Why: visa.com and intel.com geo-redirect the runner to visa.de and
    # intel.de; the companies' sites are still visa.com and intel.com.
    check = verify.classify("visa.com", 200, "https://www.visa.de/", page("Visa"), DAY)
    lookup = build.Lookup(name="Visa Inc.", domain="visa.com", method="wikidata_ticker")
    assert build.apply_homepage(lookup, check, parse_overrides({}), "V") is None
    assert (lookup.domain, lookup.redirected_from) == ("visa.com", None)
    # A different label is still a rebrand.
    check = verify.classify(
        "makerdao.com", 200, "https://sky.money/", page("Sky (formerly MakerDAO)"), DAY
    )
    moved = build.Lookup(name="Maker", domain="makerdao.com")
    build.apply_homepage(moved, check, parse_overrides({}), "MKR")
    assert moved.domain == "sky.money"


def test_a_bot_wall_is_enough_when_the_domain_is_the_coins_name() -> None:
    # Why: LooksRare, Tokenlon, DappRadar and Aleph Zero sit behind Cloudflare
    # challenges; the readable-page rule dropped real sites whose very domain
    # names the coin.
    walled = build.Lookup(
        name="LooksRare", domain="looksrare.org", method="cmc_symbol_unique"
    )
    walled.evidence = ["cmc_website", "bot_blocked_403"]
    assert build.fallback_page_ok(walled, "LOOKS")
    other = build.Lookup(
        name="Ben the Dog", domain="pesatoken.io", method="cmc_symbol_unique"
    )
    other.evidence = ["cmc_website", "bot_blocked_403"]
    assert not build.fallback_page_ok(other, "BENDOG")
    readable = build.Lookup(
        name="X", domain="x.io", method="cmc_symbol_unique", evidence=["homepage_ok"]
    )
    assert build.fallback_page_ok(readable, "X")


@pytest.mark.parametrize(
    ("final_url", "body"),
    [
        ("http://ww19.lends.so/", page("lends.so", "<p>Related searches</p>")),
        ("https://achain.com/", "<html><body><script>x()</script></body></html>"),
        (
            "https://achain.com/",
            '<!DOCTYPE html><html><head><script>window.onload=function(){window.location.href="/lander"}</script></head></html>',
        ),
    ],
)
def test_parking_redirects_and_empty_pages_are_no_site(
    final_url: str, body: str
) -> None:
    # Why: lends.so forwards to ww19.lends.so, a parking lander; achain.com,
    # crypterium.com, riochain.io and ten more serve one identical 114-byte
    # untitled page.
    domain = verify.domain_of(final_url) or ""
    assert verify.classify(domain, 200, final_url, body, DAY).outcome == "parked"


def test_a_small_but_real_page_is_not_empty() -> None:
    # bnext.es answers with one line of script forwarding to its own pages.
    forward = "<script>location.href='https://bnext.es/b2b/es'</script>"
    assert (
        verify.classify("bnext.es", 200, "https://bnext.es/", forward, DAY).outcome
        == "ok"
    )
    shell = page(
        "", '<div id="root"></div>' + "<script src='/assets/app.js'></script>" * 6
    )
    assert verify.classify("app.io", 200, "https://app.io/", shell, DAY).outcome == "ok"


def test_old_cache_rows_count_one_strike_per_day() -> None:
    # Why: rows written before per-day strikes say "failures: 3" from one run;
    # read as three days, they unmapped live sites.
    row = {"outcome": "unreachable", "checked": DAY, "failures": 3}
    assert verify.Check.from_json(row).failures == 1
    assert not verify.Check.from_json(row).dead


def test_strong_identities_are_flagged_not_dropped_when_unreachable() -> None:
    # Why: eaton.com, caterpillar.com and analog.com stall bots; the company's
    # identity comes from Wikidata, so an unreachable homepage is the runner's
    # problem, not a dead site.
    dead = verify.Check(
        "ok", DAY, 200, "eaton.com", "Eaton", failures=2, failed="2026-10-21"
    )
    company = build.Lookup(name="Eaton", domain="eaton.com", method="wikidata_ticker")
    assert (
        build.apply_homepage(company, dead, parse_overrides({}), "ETN", strong=True)
        is None
    )
    assert company.domain == "eaton.com"
    assert "unreachable" in company.flags
    coin = build.Lookup(name="Gone", domain="gone.io", method="cmc_symbol_unique")
    assert build.apply_homepage(coin, dead, parse_overrides({}), "GONE") is not None


def test_affiliate_and_adult_spam_is_hijacked_even_when_it_names_the_project() -> None:
    # Why: jup.io (the 2017 Jupiter project) now serves "Best AI Girlfriend
    # Apps ... - Jupiter": the name in the title is the spammer's site name.
    assert (
        verify.title_problem(
            "Best AI Girlfriend Apps in 2026 - Jupiter", "Jupiter", "JUP"
        )
        or ""
    ).startswith("hijacked")
    assert (
        verify.title_problem("Jupiter: The Home of Onchain Finance", "Jupiter", "JUP")
        is None
    )


def test_a_venues_first_snapshot_day_is_not_a_listing_date() -> None:
    # Why: Gate's data starts 2022-06-08, so every Gate pair live then shows
    # that first_capture; it says nothing about when the coin was listed.
    # A few older rows came from another import (BTG_USDT shows 2020), so the
    # venue's first day is not enough: a bulk day is censored too.
    rows = [
        {"symbol": f"OLD{i}", "first_capture": "2022-06-08T00:00:00Z"}
        for i in range(60)
    ]
    rows += [
        {"symbol": "BTG", "first_capture": "2020-07-01T00:00:00Z"},
        {"symbol": "GM", "first_capture": "2022-06-08T00:00:00Z"},
        {"symbol": "NEW", "first_capture": "2024-01-02T00:00:00Z"},
    ]
    universe = build_universe({}, {"gateio-spot": rows}, {})
    assert universe.unresolved["GM"].first_seen is None
    assert universe.unresolved["BTG"].first_seen is None
    assert universe.unresolved["NEW"].first_seen == "2024-01-02"


def entry(
    key: str,
    contracts: int,
    trading: int,
    cmc_id: int | None,
    cls: str,
    asset_id: str | None,
) -> dict[str, Any]:
    return {
        "_key": key,
        "_trading": trading,
        "asset_id": asset_id,
        "ticker": key.split(".")[0],
        "asset_class": cls,
        "atlas_contracts": contracts,
        "cmc_id": cmc_id,
    }


def test_a_trading_asset_keeps_the_bare_ticker_against_a_delisted_one() -> None:
    # Why: RIO went to Realio (all contracts ended) over Rio Tinto, whose perps
    # trade today; consumers join live market data by ticker.
    kept, _ = build.assign_tickers(
        [
            entry("RIO", 6, 0, 1, "crypto", None),
            entry("RIO.EQUITY", 2, 2, None, "equity", "RIO"),
        ],
        {1: 900},
    )
    assert {e["_key"]: e["ticker"] for e in kept} == {
        "RIO.EQUITY": "RIO",
        "RIO": "RIO.CRYPTO",
    }


def test_wikidata_tickers_with_an_end_or_deprecated_are_not_current() -> None:
    # Why: a company that changed ticker keeps the old statement with an end
    # time; matching it maps the old ticker's new owner to the wrong company.
    query = sources.equity_query(["ECHO"], sources.US_EXCHANGES)
    assert "FILTER NOT EXISTS { ?st pq:P582 ?ended }" in query
    assert "wikibase:DeprecatedRank" in query


def test_an_override_may_use_a_blocked_host_for_that_platforms_own_token() -> None:
    # Why (owner decision): PUMP is pump.fun's own token; pump.fun is blocked
    # because it hosts every launched token, not because it is nobody's site.
    o = parse_overrides(
        {
            "blocked_hosts": ["pump.fun"],
            "entries": {
                "PUMP": {
                    "domain": "pump.fun",
                    "own_platform": True,
                    "reason": "pump.fun's own token",
                }
            },
        }
    )
    assert o.entries["PUMP"].domain == "pump.fun"
    with pytest.raises(ValueError, match="allowed"):
        parse_overrides(
            {
                "blocked_hosts": ["pump.fun"],
                "entries": {"X": {"domain": "pump.fun", "reason": "r"}},
            }
        )


def test_loser_tickers_never_take_another_entrys_key() -> None:
    # Why: B2Gold (rBTG on Bitget), Openverse (Atlas's BTG) and Bitcoin Gold
    # (the unresolved BTG.CRYPTO) all wanted BTG; two came out as BTG.CRYPTO.
    def e(
        key: str,
        asset_id: str | None,
        cls: str,
        contracts: int,
        trading: int,
        name: str,
    ) -> dict[str, Any]:
        return {
            **entry(key, contracts, trading, None, cls, asset_id),
            "ticker": "BTG",
            "name": name,
        }

    kept, _ = build.assign_tickers(
        [
            e("BTG.EQUITY", None, "equity", 1, 1, "B2Gold"),
            e("BTG", "BTG", "crypto", 1, 1, "Openverse Network"),
            e("BTG.CRYPTO", None, "crypto", 8, 0, "Bitcoin Gold"),
        ],
        {},
    )
    tickers = {x["_key"]: x["ticker"] for x in kept}
    assert len(set(tickers.values())) == 3
    assert tickers["BTG.CRYPTO"] == "BTG.CRYPTO"
    assert tickers["BTG"] == "BTG.OPENVERSE_NETWORK"


def test_a_wikidata_label_that_contradicts_the_article_is_not_the_name() -> None:
    # Why: ManpowerGroup's Wikidata label read "Alpha Tomo Global Resoures Sdn
    # Bhd"; its article and website were right, its published name was not.
    item = {
        "qid": "Q1472539",
        "label": "Alpha Tomo Global Resoures Sdn Bhd",
        "sites": ["https://www.manpowergroup.com/"],
        "articles": ["ManpowerGroup"],
        "etf": False,
    }
    company = sources.resolve_company([item], ())
    assert isinstance(company, sources.Company)
    assert company.name == "ManpowerGroup"
