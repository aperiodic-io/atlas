"""Rules added after the independent accuracy audit and code review of the
first map (atlas#58). Each test names the real case that motivated it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from integrations.web import build, report, sources, verify
from integrations.web.overrides import parse_overrides
from integrations.web.sources import ListedCoin
from integrations.web.universe import (
    RegistryAsset,
    build_universe,
    load_snapshots,
)

DAY = "2026-10-20T04:30:00Z"
NEXT_DAY = "2026-10-21T04:30:00Z"


def coin(cmc_id: int, symbol: str, rank: int | None, added: str) -> ListedCoin:
    return ListedCoin(
        cmc_id=cmc_id, name=f"coin{cmc_id}", symbol=symbol, rank=rank, date_added=added
    )


# ----------------------------------------------------------------------------
# Symbol fallback


def test_a_coin_listed_after_atlas_first_saw_the_ticker_is_another_asset() -> None:
    # Why: Atlas's GNT is Golem (Binance from 2019); the only GNT left on CMC is
    # GreenTrust, added in 2021. The old guard compared against the *last*
    # contract (2022) and published greentrusttoken.com as Golem's site.
    listing = [coin(9533, "GNT", 3000, "2021-05-07T00:00:00Z")]
    cmc_id, reason = sources.resolve_symbol(
        "GNT", listing, "2022-03-01", first_seen="2019-03-30"
    )
    assert cmc_id is None
    assert reason.startswith("reused symbol")
    # A coin CMC lists within two months of the venue is the same launch.
    listing = [coin(1, "NEW", 900, "2024-07-15T00:00:00Z")]
    assert sources.resolve_symbol("NEW", listing, None, first_seen="2024-06-01") == (
        1,
        "cmc_symbol_unique",
    )


def test_hyperliquid_spot_only_tokens_are_not_symbol_matched() -> None:
    # Why: Hyperliquid spot tickers are free-for-all HIP-1 names (DROP, PEG).
    # Symbology already applies CMC's Hyperliquid listings, so an unresolved
    # Hyperliquid-spot-only token is by construction not the CMC coin of that name.
    registry: dict[str, RegistryAsset] = {}
    snapshots = {
        "hyperliquid-spot": [
            {"symbol": "DROP", "first_capture": "2025-01-01T00:00:00Z"}
        ]
    }
    universe = build_universe(registry, snapshots, {})
    assert build.symbol_fallback_allowed(universe.unresolved["DROP"]) is False
    snapshots["gateio-spot"] = [
        {"symbol": "DROP", "first_capture": "2025-01-01T00:00:00Z"}
    ]
    universe = build_universe(registry, snapshots, {})
    assert build.symbol_fallback_allowed(universe.unresolved["DROP"]) is True


def test_a_tradfi_asset_and_an_unresolved_coin_keep_separate_keys() -> None:
    # Why: Honeywell's HON perps (no Atlas id) were overwritten by an
    # unresolved HON coin (Heroes of NFT): the company vanished from the map.
    snapshots = {
        "mexc-perps": [{"symbol": "HONSTOCK", "underlying": "equity"}],
        "gateio-spot": [{"symbol": "HON"}],
    }
    universe = build_universe({}, snapshots, {})
    assert universe.assets["HON"].asset_class == "equity"
    assert "HON.CRYPTO" in universe.unresolved
    assert set(universe.assets).isdisjoint(universe.unresolved)


def test_only_exchange_snapshots_are_read(tmp_path: Path) -> None:
    # Why: atlas/data also holds non-snapshot JSON; reading it as contracts
    # invents assets.
    (tmp_path / "binance-futures.json").write_text(json.dumps([{"symbol": "BTC"}]))
    (tmp_path / "cmc_mappings.json").write_text(json.dumps([{"symbol": "X"}]))
    (tmp_path / "notes.json").write_text(json.dumps([{"symbol": "Y"}]))
    assert set(load_snapshots(tmp_path, venues={"binance-futures"})) == {
        "binance-futures"
    }


# ----------------------------------------------------------------------------
# One domain, one asset


def claim(key: str, domain: str, **kw: Any) -> build.Claim:
    defaults: dict[str, Any] = {
        "company": False,
        "redirected": False,
        "product": False,
        "name_in_domain": True,
        "contracts": 1,
        "trading": 1,
        "rank": None,
        "article": None,
        "same_project_as": None,
        "forced": False,
    }
    defaults.update(kw)
    return build.Claim(key=key, domain=domain, **defaults)


def test_a_company_owns_its_domain_against_its_token() -> None:
    # Why: circle.com's popularity is Circle the company's (CRCL), not USDC's,
    # however many more USDC contracts there are.
    owners = build.own_domains(
        [
            claim("USDC", "circle.com", contracts=47, name_in_domain=False),
            claim("CRCL", "circle.com", company=True, contracts=22),
        ]
    )
    assert owners == {"CRCL": "CRCL", "USDC": "CRCL"}


def test_a_protocol_owns_its_domain_against_its_products() -> None:
    # Why: mantle.xyz is Mantle's (MNT); its staked and wrapped tokens (METH,
    # WMNT) and Kinetiq's staked HYPE are products whose own popularity the
    # site does not measure, even on a contract tie.
    owners = build.own_domains(
        [
            claim("MNT", "mantle.xyz", contracts=74),
            claim("METH", "mantle.xyz", contracts=2, product=True),
            claim("WMNT", "mantle.xyz", contracts=1, product=True),
            claim("KHYPE", "kinetiq.xyz", contracts=2, product=True),
            claim("KNTQ", "kinetiq.xyz", contracts=2),
        ]
    )
    assert owners["METH"] == owners["WMNT"] == "MNT"
    assert owners["KHYPE"] == "KNTQ"


def test_a_product_alone_on_its_issuers_site_publishes_no_domain() -> None:
    # Why: Pax Dollar's site is Paxos's (paxos.com): the issuer's popularity,
    # as for EURC and BETH, even when no Paxos asset claims the domain.
    owners = build.own_domains(
        [claim("USDP", "paxos.com", product=True, name_in_domain=False)]
    )
    assert owners == {"USDP": None}
    # A product named after its own site keeps it (StraitsX USD, straitsx.com).
    assert build.own_domains([claim("XUSD", "straitsx.com", product=True)]) == {
        "XUSD": "XUSD"
    }


def test_a_redirect_into_an_owned_domain_loses_it() -> None:
    # Why: gho.xyz forwards to aave.com; following it handed Aave's site to GHO.
    owners = build.own_domains(
        [
            claim("AAVE", "aave.com", contracts=67),
            claim("GHO", "aave.com", contracts=90, redirected=True),
        ]
    )
    assert owners["GHO"] == "AAVE"


def test_one_project_under_several_assets_shares_its_domain() -> None:
    # Why: GOOG and GOOGL are one company (same article); MATIC and POL are one
    # project renamed (an override says so). Both keep the domain.
    owners = build.own_domains(
        [
            claim(
                "GOOGL", "abc.xyz", company=True, contracts=20, article="Alphabet_Inc."
            ),
            claim(
                "GOOG", "abc.xyz", company=True, contracts=2, article="Alphabet_Inc."
            ),
            claim("MATIC", "polygon.technology", contracts=46),
            claim("POL", "polygon.technology", contracts=45, same_project_as="MATIC"),
        ]
    )
    assert owners == {"GOOGL": "GOOGL", "GOOG": "GOOG", "MATIC": "MATIC", "POL": "POL"}


def test_product_names_are_recognised() -> None:
    for name in (
        "Lido Staked ETH",
        "Wrapped Mantle",
        "Bitget SOL Staking",
        "Celo Dollar",
        "Pax Dollar",
        "Mantle Restaked Ether",
    ):
        assert build.is_product(name), name
    for name in ("Lido DAO", "Mantle", "Ethereum", "USD.AI", "Wormhole"):
        assert not build.is_product(name), name


# ----------------------------------------------------------------------------
# Homepages


def page(title: str, body: str = "") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def test_a_redirect_onto_a_gambling_site_is_hijacked() -> None:
    # Why: benthedog.io, smolecoin.com and sillydragon.io forward to slot sites
    # whose titles are bland; the spam is in the body. But a Solana page's
    # "slot" or a prediction market's "betting" on a rebrand target that names
    # the asset (cronos.com, sonic.game) is not spam.
    body = "Situs slot gacor terpercaya, togel macau, judi online"
    check = verify.classify(
        "smolecoin.com", 200, "https://pesatoken.io/", page("Home", body), DAY
    )
    assert check.detail.startswith(verify.SPAM)
    lookup = build.Lookup(name="Smole", domain="smolecoin.com")
    assert "hijacked" in (
        build.apply_homepage(lookup, check, parse_overrides({}), "SMOLE") or ""
    )
    assert lookup.domain is None
    assert verify.spam_words("Every slot finalises in 400 ms") == []
    rebrand = verify.classify(
        "crypto.org",
        200,
        "https://cronos.com/",
        page("Cronos", "slots, betting, casino, macau"),
        DAY,
    )
    kept = build.Lookup(name="Cronos", domain="crypto.org")
    assert build.apply_homepage(kept, rebrand, parse_overrides({}), "CRO") is None
    assert kept.domain == "cronos.com"


def test_parking_phrases_in_an_ordinary_body_are_not_parking() -> None:
    # Why: milkyway.zone (a live staking protocol) was marked parked because
    # "parked domain" appeared somewhere in its page; only parking networks'
    # own markers count in a body.
    assert (
        verify.classify(
            "milkyway.zone",
            200,
            "https://milkyway.zone/",
            page("MilkyWay", "a parked domain guide"),
            DAY,
        ).outcome
        == "ok"
    )
    assert (
        verify.classify(
            "kap.gg", 200, "https://kap.gg/", page("Domain Parked"), DAY
        ).outcome
        == "parked"
    )
    assert (
        verify.classify(
            "x.io",
            200,
            "https://x.io/",
            page("x.io", "<script src='//sedoparking.com/a.js'>"),
            DAY,
        ).outcome
        == "parked"
    )
    # Parity's and Gandi's parking pages (goatchan.xyz, btcst.finance) and a
    # "Parking Page" title (luce.fun).
    for body in (
        "<script src='https://lander.parity.domains/js/pa.js'></script>",
        '<meta content="It is currently parked by the owner.">',
    ):
        assert (
            verify.classify(
                "p.io", 200, "http://p.io/", page("p.io", body), DAY
            ).outcome
            == "parked"
        )
    assert verify.title_problem("Parking Page", "Luce", "LUCE").startswith("parking")
    assert verify.title_problem("Хостинг успешно создан!", "DoctorX", "DRX").startswith(
        "placeholder"
    )


def test_a_casino_coin_named_in_its_title_is_not_spam() -> None:
    # Why: BC.GAME's token is "BCGame Coin" on CMC; its site is a casino.
    title = "BC Game: Crypto Casino Games & Casino Slot Games - Crypto Gambling"
    assert verify.title_problem(title, "BCGame Coin", "BC") is None


def test_an_unreachable_recheck_keeps_the_last_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Why: one failed recheck used to wipe a parked verdict or a rebrand
    # redirect, so the domain flapped in and out of the map day to day.
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(verify, "_fetch", fail)
    parked = verify.Check("parked", DAY, 200, "sedo.com", "foo.io", "lands on sedo.com")
    again = verify.check_homepage("foo.io", NEXT_DAY, parked)
    assert (again.outcome, again.final_domain, again.detail) == (
        "parked",
        "sedo.com",
        "lands on sedo.com",
    )
    assert again.failures == 1
    assert not again.dead


def test_the_second_dead_strike_must_fall_on_another_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Why: a re-run on the same day after a network blip must not count twice.
    monkeypatch.setattr(
        verify,
        "_fetch",
        lambda *_a, **_k: (_ for _ in ()).throw(requests.ConnectionError("x")),
    )
    first = verify.check_homepage("gone.io", DAY)
    same_day = verify.check_homepage("gone.io", "2026-10-20T09:00:00Z", first)
    assert (same_day.failures, same_day.dead) == (1, False)
    next_day = verify.check_homepage("gone.io", NEXT_DAY, same_day)
    assert (next_day.failures, next_day.dead) == (2, True)


def test_a_run_where_many_homepages_fail_is_the_runner_not_the_sites() -> None:
    # Why: an outage on the runner made every domain "unreachable"; two such
    # nights would unmap the whole map. Above 20% unreachable, nothing counts.
    previous = {
        f"d{i}.io": verify.Check("ok", DAY, 200, f"d{i}.io", "D") for i in range(20)
    }
    fresh = {
        d: verify.Check("unreachable", NEXT_DAY, failures=1, failed=NEXT_DAY[:10])
        for d in list(previous)[:10]
    }
    fresh.update(
        {d: verify.Check("ok", NEXT_DAY, 200, d, "D") for d in list(previous)[10:]}
    )
    kept = build.discount_outage(fresh, previous)
    assert all(kept[d] == previous[d] for d in list(previous)[:10])
    one_bad = dict(fresh)
    for d in list(previous)[1:10]:
        one_bad[d] = verify.Check("ok", NEXT_DAY, 200, d, "D")
    assert build.discount_outage(one_bad, previous)["d0.io"].failures == 1


def test_homepage_redirects_are_followed_one_host_at_a_time() -> None:
    # Why: many dead domains forward to one parking or hosting host; requests
    # must queue per host instead of hitting it with every worker at once.
    calls: list[str] = []

    class Response:
        def __init__(self, url: str, status: int, location: str | None) -> None:
            self.url, self.status_code = url, status
            self.headers = (
                {"Location": location} if location else {"Content-Type": "text/html"}
            )
            self.encoding = "utf-8"

        def iter_content(self, size: int) -> list[bytes]:
            return [page("Final").encode()]

        def close(self) -> None:
            pass

    class Session:
        def get(self, url: str, **kwargs: Any) -> Response:
            calls.append(url)
            assert kwargs["allow_redirects"] is False
            if url == "https://a.io/":
                return Response(url, 301, "https://hub.example/x")
            return Response(url, 200, None)

    status, final_url, _body = verify._fetch(Session(), "https://a.io/")  # type: ignore[arg-type]
    assert (status, final_url) == (200, "https://hub.example/x")
    assert calls == ["https://a.io/", "https://hub.example/x"]


# ----------------------------------------------------------------------------
# Wikipedia


def test_article_redirects_to_another_entity_are_dropped_and_forced_titles_kept() -> (
    None
):
    # Why: "Basic_Attention_Token" redirects to the Brave browser's article and
    # "Arweave" to Odysee; following them credits another entity's views.
    # USD1's slug article redirects to World Liberty Financial, WLFI's article.
    # A title an override forces is the owner's decision: never rewritten.
    titles = {
        "BAT": build.TitleClaim(
            "Basic_Attention_Token", False, "Basic Attention Token", "BAT"
        ),
        "AR": build.TitleClaim("Arweave", True, "Arweave", "AR"),
        "USD1": build.TitleClaim("USD1", False, "World Liberty Financial USD", "USD1"),
        "WLFI": build.TitleClaim(
            "World_Liberty_Financial", True, "World Liberty Financial", "WLFI"
        ),
        "BRENT": build.TitleClaim("Brent_Crude", False, "Brent crude", "BZ"),
    }
    canonical = {
        "Basic_Attention_Token": "Brave_(web_browser)",
        "Arweave": "Odysee",
        "USD1": "World_Liberty_Financial",
        "World_Liberty_Financial": "World_Liberty_Financial",
        "Brent_Crude": "Brent_crude",
    }
    got = build.resolve_titles(titles, canonical)
    assert got["BAT"] is None
    assert got["AR"] == "Arweave"
    assert got["USD1"] is None
    assert got["WLFI"] == "World_Liberty_Financial"
    assert got["BRENT"] == "Brent_crude"


# ----------------------------------------------------------------------------
# Tickers


def entry(
    key: str,
    contracts: int,
    cmc_id: int | None,
    cls: str = "crypto",
    asset_id: str | None = "",
) -> dict[str, Any]:
    return {
        "_key": key,
        "_trading": contracts,
        "asset_id": key if asset_id == "" else asset_id,
        "ticker": key.split(".")[0],
        "asset_class": cls,
        "atlas_contracts": contracts,
        "cmc_id": cmc_id,
    }


def test_an_asset_losing_its_bare_ticker_is_published_under_its_own_key() -> None:
    # Why (owner decision): Citigroup and AT&T lost C and T to coins and were
    # dropped; every asset must stay in the map under a unique ticker.
    kept, dropped = build.assign_tickers(
        [
            entry("C", 15, 1),
            entry("C.EQUITY", 2, None, "equity", asset_id=None),
            entry("NU.EQUITY", 2, None, "equity"),
            entry("NU", 15, 2),
        ],
        {1: 300, 2: None},
    )
    tickers = {e["_key"]: e["ticker"] for e in kept}
    assert tickers == {
        "C": "C",
        "C.EQUITY": "C.EQUITY",
        "NU.EQUITY": "NU",
        "NU": "NU.CRYPTO",
    }
    assert dropped == {}


def test_duplicate_tickers_fail_the_build() -> None:
    with pytest.raises(ValueError, match="duplicate ticker"):
        build.check_unique_tickers([{"ticker": "X"}, {"ticker": "X"}])


# ----------------------------------------------------------------------------
# Upstream failures


def test_a_failing_upstream_is_abandoned_after_a_few_failures() -> None:
    # Why: CMC failing every request would otherwise take the whole job's
    # time limit, one timeout per coin.
    calls = []

    def fetch(cmc_id: int) -> dict[str, Any]:
        calls.append(cmc_id)
        raise RuntimeError("timeout")

    caches = build.Caches(**{name: {} for name in build.CACHE_FILES})
    errors: list[str] = []
    build.refresh_cmc(caches, range(1, 100), fetch, build.now_utc(), 0, errors)
    assert len(calls) == build.MAX_CONSECUTIVE_FAILURES
    assert any("giving up" in e for e in errors)


def test_a_coin_cmc_stops_serving_keeps_its_websites_for_one_strike() -> None:
    # Why: one 404 from CMC's detail endpoint used to erase a coin's websites.
    caches = build.Caches(**{name: {} for name in build.CACHE_FILES})
    caches.cmc["7"] = {
        "name": "Seven",
        "websites": ["https://seven.io"],
        "status": "active",
        "fetched_at": "2026-01-01T00:00:00Z",
    }
    gone = {
        "name": "",
        "symbol": "",
        "slug": "",
        "status": "not_found",
        "websites": [],
        "rank": None,
    }
    build.refresh_cmc(caches, [7], lambda _i: dict(gone), build.now_utc(), 10, [])
    assert caches.cmc["7"]["websites"] == ["https://seven.io"]
    assert caches.cmc["7"]["not_found"] == 1
    build.refresh_cmc(caches, [7], lambda _i: dict(gone), build.now_utc(), 10, [])
    assert caches.cmc["7"]["websites"] == []


# ----------------------------------------------------------------------------
# Guards


def _map(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"generated_at": DAY, "entries": entries, "unmapped": {}}


def test_a_large_drop_is_not_proposed_without_acknowledgement() -> None:
    # Why: two nights of upstream outage removed 227 entries in a simulation,
    # and the PR would have been opened as usual.
    old = _map([{"ticker": f"T{i}", "domain": f"t{i}.io"} for i in range(1000)])
    small = _map(old["entries"][:-15])
    large = _map(old["entries"][:-25])
    assert not report.large_change(old, small)
    assert report.large_change(old, large)
    lost_domains = _map(
        [{**e, "domain": None} if i < 21 else e for i, e in enumerate(old["entries"])]
    )
    assert report.large_change(old, lost_domains)


def test_a_map_that_only_changed_dates_is_not_rewritten() -> None:
    # Why: a daily PR whose only change is generated_at and check dates is noise.
    e = {
        "ticker": "A",
        "domain": "a.io",
        "verification": {"status": "ok", "checked": "2026-10-20"},
    }
    old = _map([e])
    same = {
        **_map([{**e, "verification": {"status": "ok", "checked": "2026-10-21"}}]),
        "generated_at": NEXT_DAY,
    }
    assert not report.meaningful_change(old, same)
    assert report.meaningful_change(old, _map([{**e, "domain": "b.io"}]))


def test_untrusted_titles_are_code_in_the_pr_body() -> None:
    # Why: a hijacked page's title must not render as markdown (links, mentions).
    rows = report._rows([("KEY", "a.io", "hijacked: [click](http://x) @team")])
    assert "`hijacked: [click](http://x) @team`" in rows[0]


def test_override_asset_class_and_same_project_are_validated() -> None:
    with pytest.raises(ValueError, match="asset_class"):
        parse_overrides({"entries": {"X": {"asset_class": "stock", "reason": "r"}}})
    o = parse_overrides(
        {
            "entries": {
                "POL": {"same_project_as": "MATIC", "reason": "MATIC renamed POL"}
            }
        }
    )
    assert o.entries["POL"].same_project_as == "MATIC"


def test_wikidata_server_errors_are_retried_once() -> None:
    # Why: the SPARQL endpoint's 5xx/timeouts are usually load; hammering it
    # four times per query is impolite.
    attempts = []

    class Session:
        def get(self, url: str, **kwargs: Any) -> Any:
            attempts.append(url)
            raise requests.ConnectTimeout("slow")

    client = sources.make_client(session=Session())  # type: ignore[arg-type]
    client.intervals = {}
    client.default_interval = 0
    with pytest.raises(RuntimeError):
        client.get(sources.WIKIDATA_ENDPOINT)
    assert len(attempts) == 2


def test_a_rebranded_main_asset_keeps_its_domain_against_a_side_token() -> None:
    # Why: SHIB's shibatoken.com now forwards to shib.io, where Shiba's TREAT
    # token is listed directly; the redirect must not hand the site to TREAT.
    owners = build.own_domains(
        [
            claim("SHIB", "shib.io", contracts=56, redirected=True),
            claim("TREAT", "shib.io", contracts=3, name_in_domain=False),
        ]
    )
    assert owners == {"SHIB": "SHIB", "TREAT": "SHIB"}


def test_a_sites_article_goes_only_to_an_asset_it_is_about() -> None:
    # Why: Wikidata files makerdao.com under the Dai article; MKR, the site's
    # primary asset, got "Dai_(cryptocurrency)" as its own article.
    articles = sources.CryptoArticles({}, {"makerdao.com": {"Dai_(cryptocurrency)"}})
    lookups = {"MKR": build.Lookup(name="Maker", domain="makerdao.com")}
    build.fill_domain_articles(lookups, {"MKR": 30}, articles)
    assert lookups["MKR"].wikipedia is None
    lookups = {"DAI": build.Lookup(name="Dai", domain="makerdao.com")}
    build.fill_domain_articles(lookups, {"DAI": 30}, articles)
    assert lookups["DAI"].wikipedia == "Dai_(cryptocurrency)"
