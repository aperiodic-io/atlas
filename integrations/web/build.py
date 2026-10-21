"""Builds ``atlas/data/web/domain_map.json``.

Steps (``docs/web-domain-map.md`` explains each rule and why):

1. The universe: Atlas assets of the snapshot rows, counted per asset
   (:mod:`.universe`).
2. Sources: crypto websites from CoinMarketCap's detail of the Atlas
   ``cmc_id``; equities from Wikidata's ticker listings; crypto Wikipedia
   articles from Wikidata by CMC slug or website (:mod:`.sources`).
3. ``overrides.json`` on top (:mod:`.overrides`).
4. Verification: every domain's homepage (:mod:`.verify`), a CoinGecko
   cross-check, and every Wikipedia title.
5. ``ticker``: the bare ticker for the primary asset of a ticker, the
   ``asset_id`` for the others.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from . import cache, sources, verify
from .domain import normalize_host, registrable
from .http import Blocked, Client
from .overrides import Overrides, load_overrides
from .universe import Asset, Universe, build_universe, load_registry, load_snapshots

ROOT = Path(__file__).resolve().parents[2]
ATLAS_DATA = ROOT / "atlas" / "data"
OUT_DIR = ATLAS_DATA / "web"
SCHEMA_VERSION = 1

# A coin ranked at most this on CMC may own a ticker a company also trades under.
SIGNIFICANT_RANK = 1500
# An upstream that fails this many requests in a row is abandoned for the run.
MAX_CONSECUTIVE_FAILURES = 5
# More than this share of a run's homepage checks unreachable means the runner,
# not the sites, is down: the run's failures do not count.
OUTAGE_SHARE = 0.2
OUTAGE_MIN_CHECKS = 10
# Evidence that a page is the asset's own.
IDENTITY = frozenset(
    {
        "name_in_domain",
        "name_in_title",
        "coingecko_agrees",
        "override",
        "wikidata_website",
        "atlas_name_agrees",
    }
)


@dataclass
class Budgets:
    """Upstream requests a run may spend beyond what new keys need."""

    cmc_refresh: int = 150
    symbol_refresh: int = 300
    wikidata_refresh: int = 200
    homepage_refresh: int = 300
    coingecko: int = 30
    # English articles whose other-language sitelinks are refetched per run.
    sitelinks_refresh: int = 200
    # Wikidata's crypto article index is two large queries: weekly is enough.
    wikidata_crypto_days: int = 7
    coingecko_max_age_days: int = 90
    # Wall-clock budget for upstream work, well under the job's 60 minutes;
    # what is left undone is picked up by the next run.
    seconds: float = 35 * 60


@dataclass
class Paths:
    data: Path = ATLAS_DATA
    out: Path = OUT_DIR

    @property
    def registry(self) -> Path:
        return self.data / "symbology" / "assets.json"

    @property
    def map(self) -> Path:
        return self.out / "domain_map.json"

    @property
    def overrides(self) -> Path:
        return self.out / "overrides.json"

    def cache(self, name: str) -> Path:
        return self.out / "cache" / f"{name}.json"


@dataclass
class Lookup:
    """What the sources found for one asset, before overrides and verification."""

    name: str = ""
    domain: str | None = None
    wikipedia: str | None = None
    cmc_id: int | None = None
    wikidata: str | None = None
    method: str = ""
    note: str = ""
    evidence: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    redirected_from: str | None = None
    checked: str | None = None
    # The title of the page the domain answered with.
    title: str = ""
    forced: bool = False
    forced_wikipedia: bool = False
    # The article in other Wikipedia editions: {lang: title}.
    sitelinks: dict[str, str] = field(default_factory=dict)


# Cache attribute -> file under ``atlas/data/web/cache``.
CACHE_FILES = {
    "cmc": "cmc_websites",
    "symbols": "cmc_symbols",
    "equities": "wikidata_equities",
    "crypto_articles": "wikidata_crypto",
    "titles": "wikipedia_titles",
    "homepages": "homepages",
    "coingecko": "coingecko",
    "sitelinks": "wikidata_sitelinks",
}


@dataclass
class Caches:
    cmc: dict[str, dict[str, Any]]
    symbols: dict[str, dict[str, Any]]
    equities: dict[str, dict[str, Any]]
    crypto_articles: dict[str, dict[str, Any]]
    titles: dict[str, dict[str, Any]]
    homepages: dict[str, dict[str, Any]]
    coingecko: dict[str, dict[str, Any]]
    sitelinks: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, paths: Paths) -> Caches:
        return cls(
            **{
                name: cache.load(paths.cache(file))
                for name, file in CACHE_FILES.items()
            }
        )

    def save(self, paths: Paths) -> None:
        for name, file in CACHE_FILES.items():
            cache.save(paths.cache(file), getattr(self, name))


@dataclass
class Fetchers:
    """The network, behind seams the tests replace."""

    cmc_detail: Callable[[int], dict[str, Any]]
    cmc_listing: Callable[[], list[sources.ListedCoin]]
    sparql: Callable[[str], list[dict[str, str]]]
    titles: Callable[[Iterable[str]], dict[str, str | None]]
    homepages: Callable[
        [Iterable[str], str, Mapping[str, verify.Check]], dict[str, verify.Check]
    ]
    coingecko_list: Callable[[], list[dict[str, str]]]
    coingecko_homepages: Callable[[str], list[str]]
    # English titles (at most 50) -> {title: (QID, {lang: title})}.
    sitelinks: Callable[[Sequence[str]], dict[str, tuple[str, dict[str, str]]]] = (
        lambda _titles: {}
    )

    @classmethod
    def live(cls, client: Client) -> Fetchers:
        return cls(
            cmc_detail=lambda i: sources.fetch_cmc_detail(client, i),
            cmc_listing=lambda: sources.fetch_cmc_listing(client),
            sparql=lambda q: sources.sparql(client, q),
            titles=lambda t: sources.check_titles(client, t),
            homepages=verify.check_all,
            coingecko_list=lambda: sources.fetch_coingecko_list(client),
            coingecko_homepages=lambda i: sources.fetch_coingecko_homepages(client, i),
            sitelinks=lambda t: sources.fetch_sitelinks(client, t),
        )


@dataclass
class Result:
    map: dict[str, Any]
    warnings: list[str]
    upstream_errors: list[str]


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(message: str) -> None:
    print(f"web: {message}", flush=True)


# ----------------------------------------------------------------------------
# Sources


def refresh_cmc(
    caches: Caches,
    ids: Iterable[int],
    fetch: Callable[[int], dict[str, Any]],
    now: datetime,
    budget: int,
    errors: list[str],
    stop: Callable[[], bool] = lambda: False,
) -> None:
    keys = cache.select_refresh((str(i) for i in ids), caches.cmc, "fetched_at", budget)
    if keys:
        _log(f"CoinMarketCap: {len(keys)} coin details to fetch")
    failures = 0
    for position, key in enumerate(keys, 1):
        if stop():
            errors.append("CoinMarketCap: out of time, the rest waits for the next run")
            return
        try:
            row = fetch(int(key))
        except Blocked as error:
            errors.append(f"CoinMarketCap: {error}")
            return
        except Exception as error:
            errors.append(f"CoinMarketCap {key}: {error}")
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                errors.append(
                    f"CoinMarketCap: {failures} failures in a row, giving up for this run"
                )
                return
            continue
        failures = 0
        caches.cmc[key] = _merge_detail(caches.cmc.get(key), row, now)
        if position % 200 == 0:
            _log(f"CoinMarketCap: {position}/{len(keys)}")


def _merge_detail(
    previous: Mapping[str, Any] | None, row: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """A coin CMC stops serving loses its websites on the second strike only:
    one 400/404 is often a transient error of the keyless endpoint."""
    if row.get("status") == "not_found" and previous and previous.get("websites"):
        strikes = previous.get("not_found", 0) + 1
        if strikes < 2:
            return {**previous, "not_found": strikes, "fetched_at": _stamp(now)}
        return {**row, "not_found": strikes, "fetched_at": _stamp(now)}
    return {**row, "fetched_at": _stamp(now)}


def symbol_fallback_allowed(asset: Asset) -> bool:
    """Whether an unresolved crypto asset may be matched by symbol at all.

    Not a token only listed on Hyperliquid spot: its tickers are free-for-all
    HIP-1 names, and symbology already applies CMC's own Hyperliquid listings,
    so an unresolved one is by construction not the CMC coin of that name.
    """
    return not asset.venues <= {"hyperliquid-spot"}


def resolve_unresolved(
    universe: Universe,
    caches: Caches,
    fetch_listing: Callable[[], list[sources.ListedCoin]] | None,
    registry_cmc_ids: Mapping[int, str],
    now: datetime,
    budget: int,
    errors: list[str],
) -> dict[str, tuple[int, str] | str]:
    """CoinMarketCap symbol matches of crypto rows Atlas could not identify:
    key -> (cmc_id, method), or the reason there is none.

    The cache keeps every CMC coin with the symbol (id, name, rank, date added),
    so the matching rules apply to cached candidates without refetching.
    """
    allowed = {
        k: a for k, a in universe.unresolved.items() if symbol_fallback_allowed(a)
    }
    current = {k: r for k, r in caches.symbols.items() if "coins" in r}
    keys = cache.select_refresh(allowed, current, "checked", budget)
    if keys and fetch_listing is not None:
        try:
            listing = fetch_listing()
        except Exception as error:
            errors.append(f"CoinMarketCap listing: {error}")
            listing = None
        if listing is not None:
            by_symbol: dict[str, list[sources.ListedCoin]] = {}
            for coin in listing:
                by_symbol.setdefault(coin.symbol.upper(), []).append(coin)
            for key in keys:
                coins = by_symbol.get(universe.unresolved[key].ticker, [])
                caches.symbols[key] = {
                    "coins": [
                        [c.cmc_id, c.name, c.rank, c.date_added[:10]] for c in coins
                    ],
                    "checked": _stamp(now),
                }
    out: dict[str, tuple[int, str] | str] = {}
    for key, asset in universe.unresolved.items():
        row = caches.symbols.get(key)
        if key not in allowed:
            out[key] = (
                "Hyperliquid spot only: its HIP-1 tickers are not matched by symbol"
            )
            continue
        if row is None or "coins" not in row:
            out[key] = "not yet looked up on CoinMarketCap"
            continue
        coins = [
            sources.ListedCoin(i, n, asset.ticker, r, d) for i, n, r, d in row["coins"]
        ]
        cmc_id, how = sources.resolve_symbol(
            asset.ticker, coins, asset.ended, asset.first_seen
        )
        if cmc_id is None:
            out[key] = how
        elif cmc_id in registry_cmc_ids:
            out[key] = (
                f"CoinMarketCap's {asset.ticker} is Atlas asset {registry_cmc_ids[cmc_id]}, "
                "which symbology did not assign these contracts to"
            )
        else:
            out[key] = (cmc_id, how)
    return out


def refresh_wikidata_crypto(
    caches: Caches,
    sparql: Callable[[str], list[dict[str, str]]],
    now: datetime,
    max_age_days: int,
    errors: list[str],
) -> None:
    row = caches.crypto_articles.get("index")
    if row and row["fetched_at"] >= _stamp(now - timedelta(days=max_age_days)):
        return
    try:
        rows = sparql(sources.crypto_query()) + sparql(sources.cmc_slug_query())
    except Exception as error:
        errors.append(f"Wikidata crypto articles: {error}")
        return
    keep = ("slug", "site", "article")
    compact = sorted(
        {tuple(r.get(k, "") for k in keep) for r in rows if r.get("article")}
    )
    caches.crypto_articles["index"] = {
        "rows": [list(r) for r in compact],
        "fetched_at": _stamp(now),
    }


def crypto_articles(caches: Caches, blocked: Sequence[str]) -> sources.CryptoArticles:
    rows = (caches.crypto_articles.get("index") or {}).get("rows", [])
    return sources.CryptoArticles.index(
        ({"slug": s, "site": w, "article": a} for s, w, a in rows), blocked
    )


def refresh_equities(
    caches: Caches,
    tickers: Iterable[str],
    sparql: Callable[[str], list[dict[str, str]]],
    now: datetime,
    budget: int,
    errors: list[str],
) -> None:
    by_set: dict[str, list[str]] = {}
    for ticker in cache.select_refresh(tickers, caches.equities, "fetched_at", budget):
        exchanges = sources.exchange_set(ticker)
        if exchanges:
            by_set.setdefault(exchanges, []).append(ticker)
    for name, batch_all in sorted(by_set.items()):
        for start in range(0, len(batch_all), sources.EQUITY_BATCH):
            batch = batch_all[start : start + sources.EQUITY_BATCH]
            try:
                rows = sparql(sources.equity_query(batch, sources.EXCHANGE_SETS[name]))
            except Exception as error:
                errors.append(f"Wikidata equities: {error}")
                return
            items = sources.equity_items(rows)
            for ticker in batch:
                caches.equities[ticker] = {
                    "items": items.get(ticker, []),
                    "fetched_at": _stamp(now),
                }


# ----------------------------------------------------------------------------
# Lookups


def crypto_lookup(
    asset: Asset,
    cmc_id: int,
    method: str,
    caches: Caches,
    overrides: Overrides,
    articles: sources.CryptoArticles,
) -> Lookup:
    detail = caches.cmc.get(str(cmc_id))
    if detail is None:
        return Lookup(
            cmc_id=cmc_id, note=f"CoinMarketCap {cmc_id} details not fetched yet"
        )
    name = detail.get("name") or asset.name
    if sources.borrowed_site(name, asset.categories) or sources.borrowed_site(
        asset.name
    ):
        return Lookup(
            cmc_id=cmc_id,
            name=name,
            note=f"{name}: a tokenised stock, pre-IPO or fan token's website is its issuer's or club's",
        )
    domain = sources.choose_website(detail.get("websites", []), overrides.blocked_hosts)
    if domain is None:
        websites = detail.get("websites") or []
        note = (
            f"CoinMarketCap {cmc_id}: no website off blocked hosts ({websites[0]})"
            if websites
            else f"CoinMarketCap {cmc_id} lists no website"
        )
        if detail.get("status") == "not_found":
            note = f"CoinMarketCap no longer serves {cmc_id}"
    else:
        note = ""
    return Lookup(
        name=name,
        domain=domain,
        wikipedia=articles.slug(asset.cmc_slug or detail.get("slug")),
        cmc_id=cmc_id,
        method=method,
        note=note,
        evidence=["cmc_website"] if domain else [],
    )


def equity_lookup(asset: Asset, caches: Caches, overrides: Overrides) -> Lookup:
    if sources.exchange_set(asset.ticker) is None:
        return Lookup(note="not a US ticker or KRX/Tokyo code (overrides only)")
    row = caches.equities.get(asset.ticker)
    if row is None:
        return Lookup(note="Wikidata not queried yet")
    company = sources.resolve_company(row["items"], overrides.blocked_hosts)
    if isinstance(company, str):
        return Lookup(note=company)
    return Lookup(
        name=company.name,
        domain=company.domain,
        wikipedia=company.wikipedia,
        wikidata=company.qid,
        method="wikidata_ticker",
        evidence=["wikidata_website"] if company.domain else [],
    )


def fill_domain_articles(
    lookups: Mapping[str, Lookup],
    contracts: Mapping[str, int],
    articles: sources.CryptoArticles,
) -> None:
    """Give a coin without a slug-matched article its website's article, but only
    when it is the domain's primary asset (most contracts) and no other asset has
    that article already: ethereum.org's article is ETH's, not every token's
    that lists it, and makerdao.com's article is DAI's, not MKR's."""
    primary: dict[str, str] = {}
    for key, lookup in sorted(lookups.items()):
        if lookup.domain:
            best = primary.setdefault(lookup.domain, key)
            if contracts[key] > contracts[best]:
                primary[lookup.domain] = key
    taken = {lookup.wikipedia for lookup in lookups.values() if lookup.wikipedia}
    for key, lookup in sorted(lookups.items()):
        if lookup.wikipedia or not lookup.domain or primary.get(lookup.domain) != key:
            continue
        article = articles.domain(lookup.domain)
        # The site's article must be about the asset: makerdao.com's is Dai's,
        # not Maker's; arweave.org's is Odysee's.
        about = article and verify.names_agree(lookup.name, article.replace("_", " "))
        if article and about and article not in taken:
            lookup.wikipedia = article
            taken.add(article)


def apply_override(
    asset: Asset, lookup: Lookup, overrides: Overrides
) -> tuple[Lookup, str]:
    """The lookup with the asset's override on top, and its map class."""
    forced = overrides.entries.get(asset.key)
    cls = asset.asset_class
    if forced is None:
        return lookup, cls
    if forced.asset_class:
        cls = forced.asset_class
    if forced.name:
        lookup.name = forced.name
    if forced.forces_domain:
        lookup.domain = forced.domain
        lookup.forced = True
    if forced.forces_wikipedia:
        lookup.wikipedia = forced.wikipedia
        lookup.forced_wikipedia = True
    # ``method`` says where the domain came from: an article-only override
    # leaves a looked-up domain's method alone.
    if forced.forces_domain or not lookup.method:
        lookup.method = "override"
        lookup.evidence = ["override"]
    lookup.note = f"override: {forced.reason}"
    return lookup, cls


# ----------------------------------------------------------------------------
# Verification


def apply_homepage(
    lookup: Lookup,
    check: verify.Check | None,
    overrides: Overrides,
    ticker: str = "",
    strong: bool = False,
) -> str | None:
    """Fold a homepage check into the lookup; returns why the domain was dropped.

    ``strong``: the identity does not rest on the page (a Wikidata company, an
    override): an unreachable homepage is flagged, never a reason to drop it
    (eaton.com and caterpillar.com stall bots)."""
    domain = lookup.domain
    if domain is None:
        return None
    if check is None:
        lookup.flags.append("not_checked")
        return None
    lookup.checked, lookup.title = check.checked[:10], check.title
    drop = _no_site(lookup.name, ticker, domain, check, strong)
    if drop is None:
        if check.outcome == "redirect" and check.final_domain:
            _follow_redirect(lookup, domain, check, overrides, ticker)
        else:
            lookup.evidence.extend(_OUTCOME_EVIDENCE.get(check.outcome, []))
            if check.outcome == "http_error":
                lookup.flags.append(f"http_{check.status or 'error'}")
            if check.outcome == "bot_blocked":
                lookup.evidence.append(f"bot_blocked_{check.status}")
        if check.failures:
            # The last check failed; the verdict above is the one before it.
            lookup.flags.append("unreachable")
        return None
    if lookup.forced:
        lookup.flags.append(f"override_domain_suspect:{drop}")
        return None
    lookup.domain = None
    return drop


def _no_site(
    name: str, ticker: str, domain: str, check: verify.Check, strong: bool = False
) -> str | None:
    """Why a checked domain is no site of the asset's, or ``None``."""
    if check.dead and not strong:
        return f"{domain}: unreachable on {check.failures} days in a row"
    problem = verify.title_problem(check.title, name, ticker)
    if problem:
        return f"{domain}: {problem}"
    if check.outcome == "parked":
        return f"{domain}: {check.detail}"
    if check.detail.startswith(verify.SPAM) and not verify.name_matches(
        name, ticker, "", check.title
    ):
        words = check.detail.removeprefix(verify.SPAM)
        return f"{domain}: hijacked, redirects to {check.final_domain}, a gambling page ({words})"
    return None


_OUTCOME_EVIDENCE = {"ok": ["homepage_ok"]}


def _follow_redirect(
    lookup: Lookup, domain: str, check: verify.Check, overrides: Overrides, ticker: str
) -> None:
    """A redirect to another registrable domain is a rebrand: follow it, unless
    the target is a blocked host, the domain is an override's, or nothing on the
    target names the asset (a geo-redirect, a sold domain): then the domain stays
    and the redirect is flagged for review."""
    final = check.final_domain or domain
    if _label(final) == _label(domain):
        # The same brand on another TLD (visa.com -> visa.de): a geo-redirect,
        # not a rebrand; the asset's domain stays.
        lookup.evidence.extend(["homepage_ok", f"same_brand_redirect:{final}"])
    elif overrides.is_blocked(final):
        lookup.flags.append(f"redirects_to_blocked:{final}")
    elif lookup.forced:
        lookup.flags.append(f"override_redirects:{final}")
    elif not verify.name_matches(lookup.name, ticker, final, check.title):
        lookup.flags.append(f"redirect_unverified:{final}")
    else:
        lookup.redirected_from, lookup.domain = domain, final
        lookup.evidence.append("homepage_redirect")


def _label(domain: str) -> str:
    """The brand label of a registrable domain (visa.de -> visa)."""
    return domain.split(".", 1)[0]


def fallback_page_ok(lookup: Lookup, ticker: str) -> bool:
    """Whether a symbol-matched coin's homepage is evidence enough: a page that
    answered, or a bot wall on a domain that *is* the coin's name
    (looksrare.org behind Cloudflare). The domain is CMC's website for the coin
    by construction."""
    if {"homepage_ok", "homepage_redirect"} & set(lookup.evidence):
        return True
    if (
        not any(e.startswith("bot_blocked") for e in lookup.evidence)
        or not lookup.domain
    ):
        return False
    label = re.sub(r"[^a-z0-9]", "", _label(lookup.domain))
    name = re.sub(r"[^a-z0-9]", "", lookup.name.lower())
    return (len(name) >= 4 and (name in label or label in name)) or (
        len(ticker) >= 3 and label == ticker.lower()
    )


def identity(lookup: Lookup, asset: Asset, cg_homepages: list[str] | None) -> None:
    """Add evidence that the (final) domain is the asset's, or flag its absence."""
    if lookup.method == "wikidata_ticker":
        # Atlas's own name for the asset (from CMC) against the company Wikidata
        # found by ticker: a Hong Kong code queried on Tokyo finds another company.
        atlas_name = re.sub(
            r"\s*\(derivatives\)\s*$", "", asset.name, flags=re.IGNORECASE
        )
        wikidata_name = lookup.name or (lookup.wikipedia or "").replace("_", " ")
        if atlas_name and atlas_name.upper() != asset.ticker:
            if verify.names_agree(atlas_name, wikidata_name):
                lookup.evidence.append("atlas_name_agrees")
            else:
                lookup.flags.append(f"atlas_name_differs:{atlas_name}")
    if not lookup.domain:
        return
    lookup.evidence.extend(
        verify.name_matches(lookup.name, asset.ticker, lookup.domain, lookup.title)
    )
    if cg_homepages:
        cg_domains = sorted(
            {
                d
                for d in (
                    registrable(h)
                    for h in (normalize_host(x) for x in cg_homepages)
                    if h
                )
                if d
            }
        )
        if lookup.domain in cg_domains:
            lookup.evidence.append("coingecko_agrees")
        elif lookup.redirected_from in cg_domains:
            # CoinGecko vouches for the domain the asset had, not for where it
            # forwards now: the target needs evidence of its own.
            lookup.evidence.append("coingecko_agrees_before_redirect")
        elif cg_domains:
            lookup.flags.append(f"coingecko_disagrees:{cg_domains[0]}")
    if not IDENTITY & set(lookup.evidence):
        lookup.flags.append("no_identity_evidence")


def refresh_coingecko(
    caches: Caches,
    candidates: Sequence[tuple[str, str | None, str, str]],
    fetchers: Fetchers,
    now: datetime,
    budget: Budgets,
    errors: list[str],
    stop: Callable[[], bool] = lambda: False,
) -> None:
    """Cross-check up to ``budget.coingecko`` coins, in the order given
    (``(cmc_id, slug, symbol, name)``, highest priority first)."""
    stale = _stamp(now - timedelta(days=budget.coingecko_max_age_days))
    todo = [
        c
        for c in candidates
        if caches.coingecko.get(c[0], {}).get("fetched_at", "") < stale
    ]
    todo = todo[: budget.coingecko]
    if not todo:
        return
    try:
        coins = fetchers.coingecko_list()
    except Exception as error:
        errors.append(f"CoinGecko coin list: {error}")
        return
    _log(f"CoinGecko: cross-checking {len(todo)} coins")
    failures = 0
    for cmc_id, slug, symbol, name in todo:
        if stop():
            errors.append("CoinGecko: out of time, the rest waits for the next run")
            return
        cg_id = sources.match_coingecko(coins, slug, symbol, name)
        homepages: list[str] = []
        if cg_id:
            try:
                homepages = fetchers.coingecko_homepages(cg_id)
            except Blocked as error:
                errors.append(f"CoinGecko: {error}")
                return
            except Exception as error:
                errors.append(f"CoinGecko {cg_id}: {error}")
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    errors.append(
                        f"CoinGecko: {failures} failures in a row, giving up for this run"
                    )
                    return
                continue
            failures = 0
        caches.coingecko[cmc_id] = {
            "cg_id": cg_id,
            "homepages": homepages,
            "fetched_at": _stamp(now),
        }


# ----------------------------------------------------------------------------
# Tickers


def assign_tickers(
    entries: Sequence[dict[str, Any]], ranks: Mapping[int, int | None]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Give each entry its ``ticker``: the primary asset of a bare ticker keeps
    it, the others are published under a unique key of their own.

    The primary: a company keeps its ticker against a coin unless the coin ranks
    within CoinMarketCap's top 1,500 *and* has more contracts (spot venues list
    every small token: three spot listings of Mind AI must not take Mastercard's
    MA, while Stacks keeps STX). Otherwise the most contracts, then the most
    still trading, then the better CMC rank.

    The others take their ``asset_id`` (``STX.SEAGATE_...``); one without an
    Atlas ID takes its map key (``C.EQUITY``, Citigroup against the coin C); one
    whose ``asset_id`` *is* the bare ticker takes ``<TICKER>.<CLASS>``
    (NuCypher becomes ``NU.CRYPTO`` against Nu Holdings), or, when another
    entry has that key, ``<TICKER>.<NAME>`` (``BTG.OPENVERSE_NETWORK``). An
    asset that trades today beats one whose contracts all ended. Nothing is
    dropped.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(entry["ticker"], []).append(entry)
    kept: list[dict[str, Any]] = []
    losers: list[tuple[str, dict[str, Any]]] = []

    def rank(entry: dict[str, Any]) -> int:
        return ranks.get(entry.get("cmc_id") or 0) or 10**9

    def order(entry: dict[str, Any]) -> tuple[bool, int, int, int, str]:
        trading = entry.get("_trading", 0)
        return (
            not trading,
            -entry["atlas_contracts"],
            -trading,
            rank(entry),
            entry["_key"],
        )

    for ticker, group in sorted(groups.items()):
        crypto = sorted((e for e in group if e["asset_class"] == "crypto"), key=order)
        tradfi = sorted((e for e in group if e["asset_class"] != "crypto"), key=order)
        if crypto and tradfi:
            coin, company = crypto[0], tradfi[0]
            coin_trades, company_trades = (
                bool(coin.get("_trading")),
                bool(company.get("_trading")),
            )
            if coin_trades != company_trades:
                # What trades today owns the ticker: consumers join live data by it.
                coin_wins = coin_trades
            else:
                coin_wins = (
                    rank(coin) <= SIGNIFICANT_RANK
                    and coin["atlas_contracts"] > company["atlas_contracts"]
                )
            ordered = crypto + tradfi if coin_wins else tradfi + crypto
        else:
            ordered = crypto or tradfi
        kept.append(ordered[0])
        losers.extend((ticker, entry) for entry in ordered[1:])
    # Losers' tickers come last, never taking a name another entry has.
    taken = {e["ticker"] for e in kept} | {
        n for e in entries for n in (e.get("asset_id"), e["_key"]) if n
    }
    for ticker, entry in losers:
        slug = re.sub(r"[^A-Z0-9]+", "_", (entry.get("name") or "").upper()).strip("_")
        own = {entry.get("asset_id"), entry["_key"]}
        for candidate in (
            entry.get("asset_id"),
            entry["_key"],
            f"{ticker}.{entry['asset_class'].upper()}",
            f"{ticker}.{slug}" if slug else None,
        ):
            if (
                candidate
                and candidate != ticker
                and (candidate in own or candidate not in taken)
            ):
                break
        else:
            raise ValueError(f"no unique ticker for {entry['_key']}")
        taken.add(candidate)
        kept.append({**entry, "ticker": candidate})
    check_unique_tickers(kept)
    return kept, {}


def check_unique_tickers(entries: Iterable[Mapping[str, Any]]) -> None:
    """A ticker names one entry: consumers aggregate by it."""
    seen = Counter(e["ticker"] for e in entries)
    dupes = sorted(t for t, n in seen.items() if n > 1)
    if dupes:
        raise ValueError(f"duplicate ticker(s) in the map: {', '.join(dupes)}")


# ----------------------------------------------------------------------------
# One domain, one asset

_PRODUCT = re.compile(
    r"\b(?:staked|staking|restaked|wrapped|bridged|liquid staking|dollar|euro)\b|\busd\b(?!\.)",
    re.IGNORECASE,
)


def is_product(name: str) -> bool:
    """Whether a name reads as a product of another asset's project: staked,
    wrapped or bridged tokens and stablecoins (Lido Staked ETH, Celo Dollar)."""
    return bool(_PRODUCT.search(name))


@dataclass(frozen=True)
class Claim:
    """One entry's claim on a domain, with what decides who owns it."""

    key: str
    domain: str
    company: bool
    redirected: bool
    product: bool
    name_in_domain: bool
    contracts: int
    trading: int
    rank: int | None
    article: str | None
    same_project_as: str | None
    forced: bool


def own_domains(claims: Iterable[Claim]) -> dict[str, str | None]:
    """Each claimant's domain owner: a registrable domain is published for one
    asset, the project's main one.

    A company beats a token (circle.com is Circle's, not USDC's); an override
    beats a looked-up site; a product (staked, wrapped, a stablecoin) loses to
    the protocol; an asset the domain names beats one it does not; a domain
    reached only by following a redirect loses to its own asset (gho.xyz
    forwards to aave.com, which is AAVE's); then the most contracts, trading
    contracts and CMC rank. Assets of
    one project keep the domain too: same Wikipedia article (GOOG and GOOGL),
    or an override's ``same_project_as`` (MATIC and POL).

    A product that owns a domain only because nothing else claims it, on a
    site that does not even carry its name, is an issuer's site (Pax Dollar on
    paxos.com): it publishes no domain either.

    Returns key -> owner key (the key itself when it keeps the domain, ``None``
    for an issuer's site).
    """
    by_domain: dict[str, list[Claim]] = {}
    for c in claims:
        by_domain.setdefault(c.domain, []).append(c)
    owners: dict[str, str | None] = {}
    for group in by_domain.values():
        owner = min(
            group,
            key=lambda c: (
                not c.company,
                not c.forced,
                c.product,
                not c.name_in_domain,
                c.redirected,
                -c.contracts,
                -c.trading,
                c.rank or 10**9,
                c.key,
            ),
        )
        project = {owner.key, owner.same_project_as} - {None}
        for c in group:
            same = (
                c.key in project
                or (c.same_project_as is not None and c.same_project_as in project)
                or (c.article is not None and c.article == owner.article)
            )
            owners[c.key] = c.key if same else owner.key
        if owner.product and not owner.name_in_domain and not owner.forced:
            for c in group:
                if owners[c.key] == c.key:
                    owners[c.key] = None
    return owners


# ----------------------------------------------------------------------------
# Wikipedia


@dataclass(frozen=True)
class TitleClaim:
    title: str
    forced: bool
    name: str
    ticker: str


def resolve_titles(
    claims: Mapping[str, TitleClaim], canonical: Mapping[str, str | None]
) -> dict[str, str | None]:
    """Each entry's article after Wikipedia's redirects.

    A forced (override) title is never rewritten. A missing article is
    dropped. A redirect is followed only when the target is still about the
    asset: not when it names another entity ("Basic_Attention_Token" goes to
    the Brave browser, "Arweave" to Odysee) and not when it is another entry's
    own article (USD1's article redirects to WLFI's World_Liberty_Financial).
    """
    own = {
        c.title for c in claims.values() if canonical.get(c.title, c.title) == c.title
    }
    out: dict[str, str | None] = {}
    for key, c in claims.items():
        target = canonical.get(c.title, c.title)
        if c.forced:
            out[key] = c.title
        elif target is None:
            out[key] = None
        elif target == c.title:
            out[key] = target
        elif target in own:
            out[key] = None
        else:
            spaced = target.replace("_", " ")
            words = set(re.findall(r"[a-z0-9]+", spaced.lower()))
            about = verify.names_agree(c.name, spaced) or (
                len(c.ticker) >= 3 and c.ticker.lower() in words
            )
            out[key] = target if about else None
    return out


# ----------------------------------------------------------------------------
# The build


@dataclass
class _Run:
    """The state one build carries from step to step."""

    paths: Paths
    fetchers: Fetchers | None
    now: datetime
    budget: Budgets
    overrides: Overrides
    universe: Universe
    caches: Caches
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fallback: dict[str, tuple[int, str] | str] = field(default_factory=dict)
    candidates: dict[str, Asset] = field(default_factory=dict)
    lookups: dict[str, Lookup] = field(default_factory=dict)
    classes: dict[str, str] = field(default_factory=dict)
    dropped: dict[str, str] = field(default_factory=dict)
    deadline: float = float("inf")
    # Cache keys this run used: the rest left the universe and are pruned.
    used: dict[str, set[str]] = field(default_factory=dict)

    @property
    def online(self) -> bool:
        return self.fetchers is not None

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline


def _identify(run: _Run, registry: Mapping[str, Any]) -> dict[str, tuple[int, str]]:
    """The CMC coin of every crypto candidate: Atlas's, else the symbol fallback's."""
    registry_cmc = {a.cmc_id: a.asset_id for a in registry.values() if a.cmc_id}
    listing = run.fetchers.cmc_listing if run.fetchers else None
    run.fallback = resolve_unresolved(
        run.universe,
        run.caches,
        listing,
        registry_cmc,
        run.now,
        run.budget.symbol_refresh,
        run.errors,
    )
    crypto_ids = {
        a.key: (a.cmc_id, "atlas_cmc_id")
        for a in run.universe.assets.values()
        if a.asset_class == "crypto" and a.cmc_id
    }
    run.candidates = dict(run.universe.assets)
    for key, found in run.fallback.items():
        if isinstance(found, tuple):
            crypto_ids[key] = found
            run.candidates[key] = run.universe.unresolved[key]
    # An override can identify a coin neither Atlas nor the fallback could
    # (Golem's GNT, whose symbol CMC now lists for another coin).
    for key, forced in run.overrides.entries.items():
        unresolved = key in run.universe.unresolved and key not in run.candidates
        if unresolved and (forced.forces_domain or forced.forces_wikipedia):
            run.candidates[key] = run.universe.unresolved[key]
    run.used["symbols"] = set(run.universe.unresolved)
    run.used["cmc"] = {str(i) for i, _ in crypto_ids.values()}
    return crypto_ids


def _look_up(run: _Run, crypto_ids: Mapping[str, tuple[int, str]]) -> None:
    tickers = {
        a.ticker
        for a in run.candidates.values()
        if a.asset_class in ("equity", "pre_market")
    }
    run.used["equities"] = tickers
    if run.fetchers is not None:
        refresh_wikidata_crypto(
            run.caches,
            run.fetchers.sparql,
            run.now,
            run.budget.wikidata_crypto_days,
            run.errors,
        )
        refresh_cmc(
            run.caches,
            (i for i, _ in crypto_ids.values()),
            run.fetchers.cmc_detail,
            run.now,
            run.budget.cmc_refresh,
            run.errors,
            run.out_of_time,
        )
        if not run.out_of_time():
            refresh_equities(
                run.caches,
                tickers,
                run.fetchers.sparql,
                run.now,
                run.budget.wikidata_refresh,
                run.errors,
            )
    articles = crypto_articles(run.caches, run.overrides.blocked_hosts)
    for key, asset in run.candidates.items():
        if key in crypto_ids:
            cmc_id, method = crypto_ids[key]
            run.lookups[key] = crypto_lookup(
                asset, cmc_id, method, run.caches, run.overrides, articles
            )
        elif asset.asset_class in ("equity", "pre_market"):
            run.lookups[key] = equity_lookup(asset, run.caches, run.overrides)
        else:
            run.lookups[key] = Lookup(
                note=f"no source for {asset.asset_class} (overrides only)"
            )
    fill_domain_articles(
        run.lookups, {k: run.candidates[k].contracts for k in run.lookups}, articles
    )
    for key, asset in run.candidates.items():
        run.lookups[key], run.classes[key] = apply_override(
            asset, run.lookups[key], run.overrides
        )
    for key in sorted(set(run.overrides.entries) - set(run.candidates)):
        run.warnings.append(f"override {key} is not an asset of the map's universe")


def discount_outage(
    fresh: Mapping[str, verify.Check], previous: Mapping[str, verify.Check]
) -> dict[str, verify.Check]:
    """The checks to record. When more than ``OUTAGE_SHARE`` of a run's checks
    failed, the runner (its network, DNS) is the likelier culprit than the
    sites: the failures are not recorded and the previous rows stand."""

    def failed(domain: str, check: verify.Check) -> bool:
        before = previous.get(domain)
        return check.failures > (before.failures if before else 0)

    failing = [d for d, c in fresh.items() if failed(d, c)]
    if len(fresh) < OUTAGE_MIN_CHECKS or len(failing) <= OUTAGE_SHARE * len(fresh):
        return dict(fresh)
    kept = {d: c for d, c in fresh.items() if d not in failing}
    kept.update({d: previous[d] for d in failing if d in previous})
    return kept


def _check_homepages(run: _Run) -> None:
    """New domains, domains whose last check failed (two failed days in a row
    unmap a domain), then the oldest checks."""
    homepages = run.caches.homepages
    domains = {lookup.domain for lookup in run.lookups.values() if lookup.domain}
    run.used["homepages"] = set(domains)
    if run.fetchers is not None and not run.out_of_time():
        retry = {
            d
            for d, row in homepages.items()
            if 0 < row.get("failures", 0) < verify.DEAD_AFTER_FAILURES
        }
        todo = cache.select_refresh(
            domains, homepages, "checked", run.budget.homepage_refresh, always=retry
        )
        if todo:
            _log(f"homepages: checking {len(todo)} of {len(domains)} domains")
            previous = {
                d: verify.Check.from_json(homepages[d]) for d in todo if d in homepages
            }
            fresh = run.fetchers.homepages(todo, _stamp(run.now), previous)
            kept = discount_outage(fresh, previous)
            if len(kept) < len(fresh) or any(kept[d] is previous.get(d) for d in kept):
                run.errors.append(
                    "homepages: most checks failed; treated as a runner outage, "
                    "no domain was marked unreachable"
                )
            for domain, check in kept.items():
                homepages[domain] = check.to_json()
    for key, lookup in run.lookups.items():
        row = homepages.get(lookup.domain or "")
        strong = lookup.method in ("wikidata_ticker", "override") or run.classes[
            key
        ] in (
            "equity",
            "pre_market",
        )
        reason = apply_homepage(
            lookup,
            verify.Check.from_json(row) if row else None,
            run.overrides,
            run.candidates[key].ticker,
            strong,
        )
        if reason:
            run.dropped[key] = reason


def _cross_check(run: _Run) -> None:
    """CoinGecko (new entries first, then weak evidence, then the most traded),
    then the identity evidence of every entry."""
    previous_map = _previous_entries(run.paths.map)
    queue = []
    for key, lookup in run.lookups.items():
        if not (lookup.domain and lookup.cmc_id and run.classes[key] == "crypto"):
            continue
        asset = run.candidates[key]
        detail = run.caches.cmc.get(str(lookup.cmc_id), {})
        weak = not verify.name_matches(
            lookup.name, asset.ticker, lookup.domain, lookup.title
        )
        new = previous_map.get(key, {}).get("domain") != lookup.domain
        priority = (0 if new else 1 if weak else 2, -asset.contracts, key)
        symbol = detail.get("symbol") or asset.ticker
        queue.append(
            (priority, (str(lookup.cmc_id), detail.get("slug"), symbol, lookup.name))
        )
    run.used["coingecko"] = {c[0] for _, c in queue}
    if run.fetchers is not None:
        refresh_coingecko(
            run.caches,
            [c for _, c in sorted(queue)],
            run.fetchers,
            run.now,
            run.budget,
            run.errors,
            run.out_of_time,
        )
    for key, lookup in run.lookups.items():
        cg = run.caches.coingecko.get(str(lookup.cmc_id)) if lookup.cmc_id else None
        identity(lookup, run.candidates[key], cg["homepages"] if cg else None)
        if not (lookup.domain and lookup.method.startswith("cmc_symbol")):
            continue
        # A symbol match is the weakest identity: it needs a page that answered
        # (not a bot wall or an error page) and that agrees.
        if not fallback_page_ok(lookup, run.candidates[key].ticker):
            run.dropped[key] = (
                f"{lookup.domain}: CoinMarketCap symbol match whose homepage could not be read"
            )
            _drop_domain(lookup)
        elif "no_identity_evidence" in lookup.flags:
            run.dropped[key] = (
                f"{lookup.domain}: CoinMarketCap symbol match without identity evidence"
            )
            _drop_domain(lookup)


def _drop_domain(lookup: Lookup) -> None:
    """Remove a lookup's domain and everything said about it."""
    lookup.domain = lookup.redirected_from = lookup.checked = None
    lookup.evidence = [e for e in lookup.evidence if e == "override"]
    lookup.flags = [f for f in lookup.flags if f.startswith("wikipedia")]


def _own_domains(run: _Run) -> None:
    """One domain, one asset (``own_domains``)."""
    ranks = {int(k): v.get("rank") for k, v in run.caches.cmc.items()}
    claims = []
    for key, lookup in run.lookups.items():
        if not lookup.domain:
            continue
        forced = run.overrides.entries.get(key)
        asset = run.candidates[key]
        claims.append(
            Claim(
                key=key,
                domain=lookup.domain,
                company=run.classes[key] in ("equity", "pre_market"),
                redirected=lookup.redirected_from is not None,
                product=is_product(lookup.name or asset.name),
                name_in_domain="name_in_domain" in lookup.evidence,
                contracts=asset.contracts,
                trading=asset.trading,
                rank=ranks.get(lookup.cmc_id or 0),
                article=lookup.wikipedia,
                same_project_as=forced.same_project_as if forced else None,
                forced=lookup.forced,
            )
        )
    for key, owner in own_domains(claims).items():
        if owner == key:
            continue
        lookup = run.lookups[key]
        if owner is None:
            run.dropped[key] = f"{lookup.domain}: the issuer's site, not this product's"
        else:
            run.dropped[key] = (
                f"{lookup.domain}: the domain is {owner}'s (one domain, one asset)"
            )
        _drop_domain(lookup)


def _check_titles(run: _Run) -> None:
    """Every article must exist under a canonical title still about the asset
    (``resolve_titles``)."""
    titles: set[str] = {
        lookup.wikipedia for lookup in run.lookups.values() if lookup.wikipedia
    }
    run.used["titles"] = titles
    if run.fetchers is not None and titles:
        try:
            canonical = run.fetchers.titles(titles)
        except Exception as error:
            run.errors.append(f"Wikipedia titles: {error}")
            canonical = {}
        for title, target in canonical.items():
            run.caches.titles[title] = {"canonical": target, "checked": _stamp(run.now)}
    claims = {
        key: TitleClaim(
            lookup.wikipedia,
            lookup.forced_wikipedia,
            lookup.name or run.candidates[key].name,
            run.candidates[key].ticker,
        )
        for key, lookup in run.lookups.items()
        if lookup.wikipedia
    }
    known = {t: row["canonical"] for t, row in run.caches.titles.items()}
    resolved = resolve_titles(claims, known)
    for key, resolved_title in resolved.items():
        lookup = run.lookups[key]
        before = lookup.wikipedia or ""
        target = known.get(before, before)
        if resolved_title is None:
            why = (
                "does not exist"
                if target is None
                else f"redirects to {target}, not about {key}"
            )
            run.warnings.append(f"{key}: article {before} {why}; dropped")
        elif lookup.forced_wikipedia and target != before:
            run.warnings.append(
                f"{key}: override article {before} "
                + ("does not exist" if target is None else f"redirects to {target}")
            )
        lookup.wikipedia = resolved_title


def _sitelinks(run: _Run) -> None:
    """The article in every other Wikipedia edition, from Wikidata's sitelinks
    of the English title (50 titles a query; new titles, then the oldest)."""
    titles = {lookup.wikipedia for lookup in run.lookups.values() if lookup.wikipedia}
    run.used["sitelinks"] = set(titles)
    rows = run.caches.sitelinks
    if run.fetchers is not None and not run.out_of_time():
        todo = cache.select_refresh(
            titles, rows, "fetched_at", run.budget.sitelinks_refresh
        )
        if todo:
            _log(f"Wikidata sitelinks: {len(todo)} articles")
        for start in range(0, len(todo), sources.SITELINK_TITLES_PER_QUERY):
            batch = todo[start : start + sources.SITELINK_TITLES_PER_QUERY]
            try:
                found = run.fetchers.sitelinks(batch)
            except Exception as error:
                run.errors.append(f"Wikidata sitelinks: {error}")
                break
            for title in batch:
                qid, links = found.get(title, ("", {}))
                rows[title] = {
                    "qid": qid,
                    "sitelinks": links,
                    "fetched_at": _stamp(run.now),
                }
    for lookup in run.lookups.values():
        row = rows.get(lookup.wikipedia or "")
        lookup.sitelinks = sources.wikipedia_sitelinks(row["sitelinks"]) if row else {}


def _assemble(run: _Run) -> tuple[list[dict[str, Any]], dict[str, str]]:
    entries: list[dict[str, Any]] = []
    unmapped: dict[str, str] = {}
    for key, asset in sorted(run.candidates.items()):
        lookup = run.lookups[key]
        if not lookup.domain and not lookup.wikipedia:
            unmapped[key] = (
                run.dropped.get(key) or lookup.note or "no domain or article found"
            )
            continue
        entries.append(_entry(key, asset, lookup, run.classes[key]))
    for key, asset in run.universe.unresolved.items():
        if key not in run.candidates:
            reason = run.fallback.get(key)
            unmapped[key] = (
                f"Atlas has no asset for these {asset.contracts} crypto contracts; "
                f"symbol fallback: {reason if isinstance(reason, str) else 'not used'}"
            )
    ranks = {int(k): v.get("rank") for k, v in run.caches.cmc.items()}
    entries, _ = assign_tickers(entries, ranks)
    for entry in entries:
        del entry["_key"], entry["_trading"]
    entries.sort(key=lambda e: e["ticker"])
    return entries, dict(sorted(unmapped.items()))


def _prune(run: _Run) -> None:
    """Drop cache rows of keys that left the universe."""
    for name, keep in run.used.items():
        rows = getattr(run.caches, name)
        for key in [k for k in rows if k not in keep]:
            del rows[key]


def build(
    paths: Paths,
    fetchers: Fetchers | None,
    now: datetime,
    budget: Budgets | None = None,
    atlas_source: str = "",
) -> Result:
    """Build the map. ``fetchers=None`` rebuilds from the caches alone."""
    overrides = load_overrides(paths.overrides)
    registry = load_registry(paths.registry)
    run = _Run(
        paths=paths,
        fetchers=fetchers,
        now=now,
        budget=budget or Budgets(),
        overrides=overrides,
        universe=build_universe(
            registry, load_snapshots(paths.data), overrides.aliases
        ),
        caches=Caches.load(paths),
    )
    run.deadline = time.monotonic() + run.budget.seconds
    _log(
        f"{len(run.universe.assets)} assets, {len(run.universe.unresolved)} unresolved crypto symbols"
    )
    try:
        crypto_ids = _identify(run, registry)
        # Caches are saved after every step and on the way out: a run that dies
        # half way keeps what it fetched.
        for step in (
            partial(_look_up, run, crypto_ids),
            partial(_check_homepages, run),
            partial(_cross_check, run),
            partial(_own_domains, run),
            partial(_check_titles, run),
            partial(_sitelinks, run),
        ):
            step()
            if run.online:
                run.caches.save(paths)
        entries, unmapped = _assemble(run)
        if run.online:
            _prune(run)
    finally:
        if run.online:
            run.caches.save(paths)
    file = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _stamp(now),
        "atlas_source": atlas_source,
        "entries": entries,
        "unmapped": unmapped,
    }
    return Result(map=file, warnings=run.warnings, upstream_errors=run.errors)


def _entry(key: str, asset: Asset, lookup: Lookup, cls: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ticker": asset.ticker,
        "_key": key,
        "_trading": asset.trading,
    }
    if asset.asset_id:
        entry["asset_id"] = asset.asset_id
    entry["asset_class"] = cls
    if lookup.name or asset.name:
        entry["name"] = lookup.name or asset.name
    if lookup.domain:
        entry["domain"] = lookup.domain
    if lookup.wikipedia:
        entry["wikipedia"] = lookup.wikipedia
        if lookup.sitelinks:
            entry["wikipedia_sitelinks"] = lookup.sitelinks
    entry["atlas_symbols"] = sorted(asset.symbols)
    entry["atlas_contracts"] = asset.contracts
    if lookup.cmc_id:
        entry["cmc_id"] = lookup.cmc_id
    if lookup.wikidata:
        entry["wikidata"] = lookup.wikidata
    entry["method"] = lookup.method or "override"
    evidence = list(
        dict.fromkeys(
            lookup.evidence + (["wikipedia_article"] if lookup.wikipedia else [])
        )
    )
    flags = list(dict.fromkeys(lookup.flags))
    status = "flagged" if flags else "redirect" if lookup.redirected_from else "ok"
    verification: dict[str, Any] = {"status": status, "evidence": evidence}
    if flags:
        verification["flags"] = flags
    if lookup.redirected_from:
        verification["redirected_from"] = lookup.redirected_from
    if lookup.checked:
        # The day the homepage last answered; absent when it never was checked.
        verification["checked"] = lookup.checked
    entry["verification"] = verification
    return entry


def _previous_entries(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {e.get("asset_id") or e["ticker"]: e for e in payload.get("entries", [])}


def dumps_map(file: Mapping[str, Any]) -> str:
    """The map file: two-space indent, UTF-8 (no escapes), trailing newline."""
    return json.dumps(file, indent=2, ensure_ascii=False) + "\n"


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
