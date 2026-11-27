"""Where websites and articles come from: CoinMarketCap, Wikidata, Wikipedia and
CoinGecko. Parsers are pure; fetchers go through :class:`http.Client`."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests

from .domain import is_blocked, normalize_host, registrable
from .http import Client
from .verify import names_agree

CMC_HOST = "api.coinmarketcap.com"
CMC_DETAIL_URL = f"https://{CMC_HOST}/data-api/v3/cryptocurrency/detail"
CMC_LISTING_URL = f"https://{CMC_HOST}/data-api/v3/cryptocurrency/listing"
CMC_LISTING_PAGE = 5000
WIKIDATA_HOST = "query.wikidata.org"
WIKIDATA_ENDPOINT = f"https://{WIKIDATA_HOST}/sparql"
WIKIDATA_API_HOST = "www.wikidata.org"
WIKIDATA_API = f"https://{WIKIDATA_API_HOST}/w/api.php"
# Titles per wbgetentities query (the API's limit for anonymous clients).
SITELINK_TITLES_PER_QUERY = 50
WIKIPEDIA_HOST = "en.wikipedia.org"
WIKIPEDIA_API = f"https://{WIKIPEDIA_HOST}/w/api.php"
# Titles per Wikipedia API query (the limit for anonymous clients).
TITLES_PER_QUERY = 50
COINGECKO_HOST = "api.coingecko.com"
COINGECKO_API = f"https://{COINGECKO_HOST}/api/v3"

# Minimum spacing per host, in seconds.
INTERVALS = {
    CMC_HOST: 1.0,
    WIKIDATA_HOST: 5.0,
    WIKIDATA_API_HOST: 1.0,
    WIKIPEDIA_HOST: 1.0,
    # The keyless API allows roughly 5-15 calls a minute, shared per IP.
    COINGECKO_HOST: 12.0,
}

# Attempts per request; Wikidata's 5xx/timeouts are load: retry once only.
MAX_ATTEMPTS = {WIKIDATA_HOST: 2}


def make_client(session: requests.Session | None = None) -> Client:
    client = Client(intervals=INTERVALS, max_attempts_by_host=MAX_ATTEMPTS)
    if session is not None:
        client.session = session
    return client


# ----------------------------------------------------------------------------
# CoinMarketCap


def parse_cmc_detail(payload: object, cmc_id: int) -> dict[str, Any]:
    """What the map keeps of a coin's detail (also the cache row, minus ``fetched_at``)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("id") != cmc_id:
        raise ValueError(f"CMC detail for {cmc_id} has no matching data")
    urls: dict[str, Any] = data["urls"] if isinstance(data.get("urls"), dict) else {}
    websites = [w for w in urls.get("website") or [] if isinstance(w, str)]
    statistics: dict[str, Any] = (
        data["statistics"] if isinstance(data.get("statistics"), dict) else {}
    )
    rank = statistics.get("rank")
    return {
        "name": str(data.get("name") or ""),
        "symbol": str(data.get("symbol") or ""),
        "slug": str(data.get("slug") or ""),
        "status": str(data.get("status") or ""),
        "websites": websites,
        "rank": rank
        if isinstance(rank, int) and not isinstance(rank, bool) and rank > 0
        else None,
    }


def fetch_cmc_detail(client: Client, cmc_id: int) -> dict[str, Any]:
    response = client.get(
        CMC_DETAIL_URL, params={"id": cmc_id}, accept=frozenset({200, 400, 404})
    )
    if response.status_code != 200:
        # A coin CMC no longer serves: it has no website any more.
        return {
            "name": "",
            "symbol": "",
            "slug": "",
            "status": "not_found",
            "websites": [],
            "rank": None,
        }
    return parse_cmc_detail(response.json(), cmc_id)


@dataclass(frozen=True)
class ListedCoin:
    cmc_id: int
    name: str
    symbol: str
    rank: int | None
    date_added: str


def parse_cmc_listing(payload: object) -> tuple[list[ListedCoin], int]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("CMC listing has no data")
    coins = []
    for row in data.get("cryptoCurrencyList") or []:
        rank = row.get("cmcRank")
        coins.append(
            ListedCoin(
                cmc_id=int(row["id"]),
                name=str(row.get("name") or ""),
                symbol=str(row.get("symbol") or ""),
                rank=rank if isinstance(rank, int) and rank > 0 else None,
                date_added=str(row.get("dateAdded") or ""),
            )
        )
    return coins, int(data.get("totalCount") or 0)


def fetch_cmc_listing(client: Client) -> list[ListedCoin]:
    """Every coin CoinMarketCap lists (two or three pages)."""
    coins: list[ListedCoin] = []
    start = 1
    while True:
        response = client.get(
            CMC_LISTING_URL,
            params={
                "start": start,
                "limit": CMC_LISTING_PAGE,
                "sortBy": "rank",
                "sortType": "asc",
                "cryptoType": "all",
                "tagType": "all",
                "audited": "false",
            },
        )
        page, total = parse_cmc_listing(response.json())
        coins.extend(page)
        if len(page) < CMC_LISTING_PAGE or len(coins) >= total:
            return coins
        start += CMC_LISTING_PAGE


# Best rank a symbol match may have among several coins, and how far ahead of
# the next candidate it must be.
DOMINANT_MAX_RANK = 1500
DOMINANT_FACTOR = 5


# A coin CMC added this long after Atlas first saw the ticker can still be the
# coin the venue listed (CMC lags new listings); later than that, it is not.
LISTING_LAG = timedelta(days=60)


def resolve_symbol(
    ticker: str,
    listing: Sequence[ListedCoin],
    ended: str | None,
    first_seen: str | None = None,
) -> tuple[int | None, str]:
    """The CMC coin an unresolved crypto ticker means, and how, or ``(None, reason)``.

    The only coin with the symbol (``cmc_symbol_unique``) or a clearly dominant
    one (``cmc_symbol_dominant``: ranked within 1,500 and the next candidate at
    least 5x further down). A coin CMC added more than two months after Atlas
    first saw the ticker (``first_seen``), or after every contract had ended
    (``ended``), reuses the symbol and is not a candidate: GreenTrust (2021) is
    not the Golem GNT Binance listed in 2019.
    """

    def rank(coin: ListedCoin) -> int:
        return coin.rank or 10**12

    latest = ended
    if first_seen:
        bound = (date.fromisoformat(first_seen[:10]) + LISTING_LAG).isoformat()
        latest = min(latest, bound) if latest else bound
    same = [c for c in listing if c.symbol.upper() == ticker]
    candidates = sorted(
        (
            c
            for c in same
            if not (latest and c.date_added and c.date_added[:10] > latest)
        ),
        key=lambda c: (rank(c), c.cmc_id),
    )
    if not candidates:
        if same:
            return (
                None,
                "reused symbol: every CoinMarketCap coin with it was added after "
                f"Atlas first listed the ticker or after its contracts ended ({latest})",
            )
        return None, "no CoinMarketCap coin with this symbol"
    if len(candidates) == 1:
        return candidates[0].cmc_id, "cmc_symbol_unique"
    best, nxt = candidates[0], candidates[1]
    if rank(best) <= DOMINANT_MAX_RANK and rank(nxt) >= rank(best) * DOMINANT_FACTOR:
        return best.cmc_id, "cmc_symbol_dominant"
    return None, (
        f"ambiguous CoinMarketCap symbol: {len(candidates)} coins, best ranks "
        f"{best.rank} ({best.name}) and {nxt.rank} ({nxt.name})"
    )


_BORROWED_NAME = re.compile(
    r"tokeni[sz]ed|fan token|pre-ipo|\(derivatives\)", re.IGNORECASE
)


def borrowed_site(name: str, categories: Iterable[str] = ()) -> bool:
    """Whether a coin's CMC website is someone else's by construction.

    Tokenised stocks and funds (the issuer's platform, shared by every token it
    issues), pre-IPO tokens and fan tokens (the club's or fan platform's site):
    their site's popularity says nothing about the token.
    """
    return bool(_BORROWED_NAME.search(name)) or "Tokenized Stock" in categories


def choose_website(websites: Iterable[str], blocked: Sequence[str]) -> str | None:
    """The registrable domain of the first website off blocked hosts.

    CMC lists the official site first; later ones are often a docs site.
    """
    for website in websites:
        host = normalize_host(website)
        domain = registrable(host) if host else None
        if (
            domain
            and not is_blocked(host or "", blocked)
            and not is_blocked(domain, blocked)
        ):
            return domain
    return None


# ----------------------------------------------------------------------------
# Wikidata

# NASDAQ, NYSE, NYSE American; KRX; Tokyo.
US_EXCHANGES = ("Q82059", "Q13677", "Q846626")
KRX_EXCHANGES = ("Q495372",)
TOKYO_EXCHANGES = ("Q217475",)
EXCHANGE_SETS = {"US": US_EXCHANGES, "KRX": KRX_EXCHANGES, "TOKYO": TOKYO_EXCHANGES}
ETF = "Q11461862"
CRYPTOCURRENCY = "Q13479982"
EQUITY_BATCH = 200


def exchange_set(ticker: str) -> str | None:
    """Where a ticker can be listed, by its shape: 1-5 letters US, 6 digits KRX,
    4 digits Tokyo. Anything else (``TOYOTA``, ``BRK.B``) is an override's job."""
    if (
        1 <= len(ticker) <= 5
        and ticker.isascii()
        and ticker.isalpha()
        and ticker.isupper()
    ):
        return "US"
    if len(ticker) == 6 and ticker.isascii() and ticker.isdigit():
        return "KRX"
    if len(ticker) == 4 and ticker.isascii() and ticker.isdigit():
        return "TOKYO"
    return None


def equity_query(tickers: Sequence[str], exchanges: Sequence[str]) -> str:
    """Current listings of ``tickers`` on ``exchanges`` with website, English
    article, label and an ETF flag (no end time, not deprecated, not dissolved)."""
    values = " ".join(f'"{t}"' for t in tickers)
    exch = " ".join(f"wd:{q}" for q in exchanges)
    return (
        "SELECT ?ticker ?item ?label ?site ?article ?etf WHERE { "
        f"VALUES ?ticker {{ {values} }} VALUES ?exch {{ {exch} }} "
        "?item p:P414 ?st . ?st pq:P249 ?ticker ; ps:P414 ?exch ; wikibase:rank ?rank . "
        "FILTER(?rank != wikibase:DeprecatedRank) FILTER NOT EXISTS { ?st pq:P582 ?ended } "
        "FILTER NOT EXISTS { ?item wdt:P576 ?dissolved } "
        "OPTIONAL { ?item wdt:P856 ?site } "
        "OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> } "
        'OPTIONAL { ?item rdfs:label ?label FILTER(LANG(?label) = "en") } '
        f"BIND(EXISTS {{ ?item wdt:P31 wd:{ETF} }} AS ?etf) }}"
    )


def crypto_query() -> str:
    """Cryptocurrency items with an English article (subclasses three levels deep:
    the unbounded path runs into the endpoint's 60 s limit)."""
    return (
        "SELECT ?item ?site ?article WHERE { "
        f"?item wdt:P31/wdt:P279?/wdt:P279?/wdt:P279? wd:{CRYPTOCURRENCY} . "
        "?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . "
        "OPTIONAL { ?item wdt:P856 ?site } }"
    )


def cmc_slug_query() -> str:
    """Items with a CoinMarketCap slug (P5777) and an English article."""
    return (
        "SELECT ?item ?slug ?site ?article WHERE { ?item wdt:P5777 ?slug . "
        "?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . "
        "OPTIONAL { ?item wdt:P856 ?site } }"
    )


def parse_sparql(payload: object) -> list[dict[str, str]]:
    bindings = payload["results"]["bindings"] if isinstance(payload, dict) else []
    return [{k: v["value"] for k, v in b.items()} for b in bindings]


def sparql(client: Client, query: str) -> list[dict[str, str]]:
    response = client.get(
        WIKIDATA_ENDPOINT,
        params={"query": query},
        headers={"Accept": "application/sparql-results+json"},
    )
    return parse_sparql(response.json())


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def article_title(url: str) -> str | None:
    """The title of an English Wikipedia URL, percent-decoded, underscores kept."""
    if "/wiki/" not in url:
        return None
    try:
        title = unquote(url.split("/wiki/", 1)[1], errors="strict")
    except UnicodeDecodeError:
        return None
    return title or None


def pick_domain(sites: Iterable[str], blocked: Sequence[str]) -> str | None:
    """The registrable domain most of an item's websites share (Apple lists ~100
    country pages of apple.com), ties to the shortest; blocked hosts never count."""
    counts: Counter[str] = Counter()
    for site in sites:
        host = normalize_host(site)
        domain = registrable(host) if host else None
        if not domain or is_blocked(host or "", blocked) or is_blocked(domain, blocked):
            continue
        counts[domain] += 1
    if not counts:
        return None
    return min(counts, key=lambda d: (-counts[d], len(d), d))


def equity_items(rows: Iterable[Mapping[str, str]]) -> dict[str, list[dict[str, Any]]]:
    """The rows of :func:`equity_query` grouped per ticker and item (the cache form)."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        ticker, item = row.get("ticker"), row.get("item")
        if not ticker or not item:
            continue
        it = out.setdefault(ticker, {}).setdefault(
            qid(item),
            {
                "qid": qid(item),
                "label": "",
                "sites": set(),
                "articles": set(),
                "etf": False,
            },
        )
        if row.get("label"):
            it["label"] = row["label"]
        if row.get("site"):
            it["sites"].add(row["site"])
        title = article_title(row.get("article", ""))
        if title:
            it["articles"].add(title)
        it["etf"] = it["etf"] or row.get("etf") == "true"
    return {
        t: [
            {**it, "sites": sorted(it["sites"]), "articles": sorted(it["articles"])}
            for _, it in sorted(items.items())
        ]
        for t, items in out.items()
    }


@dataclass(frozen=True)
class Company:
    qid: str
    name: str
    domain: str | None
    wikipedia: str | None


def resolve_company(
    items: Sequence[Mapping[str, Any]], blocked: Sequence[str]
) -> Company | str:
    """A ticker's one non-ETF item (items with an English article win when there
    are several), or why it is unmapped."""
    if not items:
        return "no current Wikidata listing on the queried exchanges (ETFs: NYSE Arca)"
    candidates = [i for i in items if not i["etf"]]
    if not candidates:
        return "etf: a fund's site is its issuer's"
    if len(candidates) > 1:
        with_article = [i for i in candidates if i["articles"]]
        candidates = with_article or candidates
    if len(candidates) > 1:
        return "ambiguous Wikidata ticker: " + ", ".join(i["qid"] for i in candidates)
    item = candidates[0]
    domain = pick_domain(item["sites"], blocked)
    wikipedia = item["articles"][0] if item["articles"] else None
    if domain is None and wikipedia is None:
        return f"{item['qid']} has no usable website or English article"
    name = item["label"]
    if wikipedia and not names_agree(name, wikipedia.replace("_", " ")):
        # A vandalised or stale label (ManpowerGroup's read "Alpha Tomo Global
        # Resoures Sdn Bhd"): the article title names the company.
        name = wikipedia.replace("_", " ")
    return Company(qid=item["qid"], name=name, domain=domain, wikipedia=wikipedia)


@dataclass
class CryptoArticles:
    """English articles of cryptocurrencies by CMC slug and by website domain."""

    by_slug: dict[str, set[str]]
    by_domain: dict[str, set[str]]

    @classmethod
    def index(
        cls, rows: Iterable[Mapping[str, str]], blocked: Sequence[str]
    ) -> CryptoArticles:
        out = cls({}, {})
        for row in rows:
            title = article_title(row.get("article", ""))
            if not title:
                continue
            if row.get("slug"):
                out.by_slug.setdefault(row["slug"], set()).add(title)
            domain = pick_domain([row["site"]], blocked) if row.get("site") else None
            if domain:
                out.by_domain.setdefault(domain, set()).add(title)
        return out

    def slug(self, slug: str | None) -> str | None:
        return _unique(self.by_slug.get(slug or ""))

    def domain(self, domain: str | None) -> str | None:
        """The article of a domain when exactly one has it (a token and its
        company on one domain stay unset)."""
        return _unique(self.by_domain.get(domain or ""))


def _unique(values: set[str] | None) -> str | None:
    return next(iter(values)) if values and len(values) == 1 else None


# ----------------------------------------------------------------------------
# Wikipedia


def parse_titles(payload: object) -> dict[str, str | None]:
    """Each queried title -> its canonical title (a redirect's target), or ``None``
    when the article does not exist. Keys and values use underscores."""
    query = payload.get("query", {}) if isinstance(payload, dict) else {}

    def under(title: str) -> str:
        return title.replace(" ", "_")

    normalized = {under(n["from"]): under(n["to"]) for n in query.get("normalized", [])}
    redirects = {under(r["from"]): under(r["to"]) for r in query.get("redirects", [])}
    missing = {
        under(p["title"])
        for p in query.get("pages", [])
        if p.get("missing") or p.get("invalid")
    }
    present = {
        under(p["title"])
        for p in query.get("pages", [])
        if not (p.get("missing") or p.get("invalid"))
    }
    out: dict[str, str | None] = {}
    for title in present | missing | set(normalized) | set(redirects):
        target = normalized.get(title, title)
        target = redirects.get(target, target)
        out[title] = None if target in missing else target
    return out


def check_titles(client: Client, titles: Iterable[str]) -> dict[str, str | None]:
    """Canonical title (or ``None`` for missing) of every title, batched."""
    pending = sorted(set(titles))
    out: dict[str, str | None] = {}
    for start in range(0, len(pending), TITLES_PER_QUERY):
        chunk = pending[start : start + TITLES_PER_QUERY]
        response = client.get(
            WIKIPEDIA_API,
            params={
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "redirects": "1",
                "titles": "|".join(chunk),
            },
        )
        resolved = parse_titles(response.json())
        for title in chunk:
            out[title] = resolved.get(title, title)
    return out


# ----------------------------------------------------------------------------
# CoinGecko


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def match_coingecko(
    coins: Sequence[Mapping[str, str]], slug: str | None, symbol: str, name: str
) -> str | None:
    """The CoinGecko id of a CMC coin: the id equal to the CMC slug with the same
    symbol, else the one coin with the same symbol and name. ``None`` if unsure."""
    sym = symbol.lower()
    same_symbol = [c for c in coins if c.get("symbol", "").lower() == sym]
    for coin in same_symbol:
        if slug and coin.get("id") == slug:
            return coin["id"]
    named = [c for c in same_symbol if _norm(c.get("name", "")) == _norm(name)]
    return named[0]["id"] if len(named) == 1 else None


def fetch_coingecko_list(client: Client) -> list[dict[str, str]]:
    return client.get(f"{COINGECKO_API}/coins/list").json()


def fetch_coingecko_homepages(client: Client, cg_id: str) -> list[str]:
    response = client.get(
        f"{COINGECKO_API}/coins/{cg_id}",
        params={
            "localization": "false",
            "tickers": "false",
            "market_data": "false",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false",
        },
        accept=frozenset({200, 404}),
    )
    if response.status_code == 404:
        return []
    links = response.json().get("links") or {}
    return [h for h in links.get("homepage") or [] if isinstance(h, str) and h.strip()]


# ----------------------------------------------------------------------------
# Wikipedia articles in other languages (Wikidata sitelinks)

# Open Wikipedia language editions (Wikidata site id -> wikipedia.org
# subdomain), from a Wikimedia sitematrix snapshot: closed wikis (iiwiki) have
# no readers, and one host differs from its id (be_x_oldwiki is be-tarask).
_EDITIONS: dict[str, str] = json.loads(
    (Path(__file__).with_name("wikipedias.json")).read_text(encoding="utf-8")
)["editions"]


def sitelink_lang(site: str) -> str | None:
    """The Wikipedia subdomain of a Wikidata site id (``dewiki`` -> ``de``,
    ``zh_yuewiki`` -> ``zh-yue``, ``be_x_oldwiki`` -> ``be-tarask``), or
    ``None`` for other projects, closed editions and English."""
    return _EDITIONS.get(site)


def parse_sitelinks(payload: object) -> dict[str, tuple[str, dict[str, str]]]:
    """A wbgetentities answer as English title (underscores) -> (QID, {Wikidata
    site id: title}) for every ``*wiki`` site. The raw site ids are what the
    cache keeps, so a change of the edition table needs no refetch. Titles keep
    Wikidata's spaces; missing titles are left out."""
    entities = payload.get("entities", {}) if isinstance(payload, dict) else {}
    out = {}
    for entity in entities.values():
        links = entity.get("sitelinks") or {}
        english = (links.get("enwiki") or {}).get("title")
        if "missing" in entity or not english:
            continue
        sites = {
            site: link["title"]
            for site, link in links.items()
            if site.endswith("wiki") and site != "enwiki" and link.get("title")
        }
        out[english.replace(" ", "_")] = (
            entity.get("id", ""),
            dict(sorted(sites.items())),
        )
    return out


def wikipedia_sitelinks(sites: Mapping[str, str]) -> dict[str, str]:
    """Raw sitelinks as {wikipedia.org subdomain: title}, open editions only."""
    out = {}
    for site, title in sites.items():
        lang = sitelink_lang(site)
        if lang:
            out[lang] = title
    return dict(sorted(out.items()))


def fetch_sitelinks(
    client: Client,
    titles: Sequence[str],
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 4,
) -> dict[str, tuple[str, dict[str, str]]]:
    """Sitelinks of up to 50 English titles in one wbgetentities query, with
    ``maxlag=5``: when Wikidata reports replication lag it is asked again after
    its Retry-After, a bounded number of times."""
    params = {
        "action": "wbgetentities",
        "sites": "enwiki",
        "titles": "|".join(t.replace("_", " ") for t in titles),
        "props": "sitelinks",
        "format": "json",
        "maxlag": 5,
    }
    for _ in range(attempts):
        response = client.get(WIKIDATA_API, params=params)
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if not error:
            return parse_sitelinks(payload)
        if error.get("code") != "maxlag":
            raise RuntimeError(f"wbgetentities: {error.get('info') or error}")
        try:
            wait = float(response.headers.get("Retry-After", 5))
        except (TypeError, ValueError):
            wait = 5.0
        sleep(wait)
    raise RuntimeError("wbgetentities: Wikidata still lagged after backing off")
