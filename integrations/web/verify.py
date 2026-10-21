"""Is a mapped domain a live site, and is it the asset's?

One GET of ``https://<domain>/`` per domain and run (falling back to plain
HTTP, then to ``www.``), at most five redirects, a short timeout and a capped
body. The outcome:

* ``ok``: the site answered with its own page;
* ``redirect``: it now lives on another registrable domain (a rebrand:
  makerdao.com -> sky.money), which the map follows;
* ``parked``: a for-sale or parking page, or a hosting placeholder (no site);
* ``bot_blocked``: a firewall refused our client (401/403/429): alive, but
  nothing to read;
* ``http_error``: an error page (404, 5xx);
* ``unreachable``: no answer at all. A domain is only treated as dead after
  two consecutive unreachable checks, so one flaky night unmaps nothing.
"""

from __future__ import annotations

import html
import re
import threading
import time
import unicodedata
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from .domain import domain_of
from .http import USER_AGENT

MAX_REDIRECTS = 5
TIMEOUT = (5.0, 10.0)
# A server that drips its body slowly is cut off after this many seconds.
DEADLINE_SECONDS = 20.0
MAX_BODY_BYTES = 256 * 1024
WORKERS = 16
DEAD_AFTER_FAILURES = 2

# Domain marketplaces and parking services: landing on one means "for sale".
PARKING_DOMAINS = frozenset(
    {
        "afternic.com",
        "atom.com",
        "bodis.com",
        "brandbucket.com",
        "buydomains.com",
        "dan.com",
        "domainmarket.com",
        "efty.com",
        "hugedomains.com",
        "parkingcrew.net",
        "perfectdomain.com",
        "sav.com",
        "sedo.com",
        "sedoparking.com",
        "squadhelp.com",
        "undeveloped.com",
    }
)

# Parking and for-sale phrases, matched in the page title.
_PARKED = re.compile(
    r"domain(?: name)? (?:is|may be) for sale"
    r"|buy this domain"
    r"|this domain is parked"
    r"|parked (?:free|domain|by)|domain parked|\bparking page\b"
    r"|domain (?:has )?expired"
    r"|is for sale[!.]? *$"
    r"|\bafternic\b|\bhugedomains\b|\bsedo\b",
    re.IGNORECASE,
)
# In a body only the parking networks' own markers count: an ordinary page can
# mention "a parked domain" (milkyway.zone did).
_PARKED_BODY = re.compile(
    r"sedoparking|parkingcrew|bodis\.com|window\.park\b|afternic\.com|hugedomains\.com"
    r"|dan\.com/(?:buy|lander)|this domain (?:name )?(?:is|may be) for sale|buy this domain"
    r"|lander\.parity\.domains|currently parked by the owner|parkingpage_",
    re.IGNORECASE,
)
# Gambling vocabulary on a page another domain forwards to: an expired
# project domain sold to a slot site (benthedog.io, smolecoin.com). Indonesian
# slot-site words are unmistakable alone; the generic ones (a Solana "slot", a
# prediction market's "betting") only in numbers, and never on a page that
# names the asset (the build checks that).
_SPAM_STRONG = re.compile(
    r"\bgacor\b|\btogel\b|\bjudi\b|\bsitus\b|\bmaxwin\b|bandarqq|dominoqq|\bslot(?:88|777)\b",
    re.IGNORECASE,
)
_SPAM_WEAK = re.compile(
    r"\bslots?\b|\bcasinos?\b|\bbetting\b|\btoto\b|\bmacau\b", re.IGNORECASE
)
SPAM = "spam words: "


def spam_words(text: str) -> list[str]:
    """The gambling words that make a page look like slot spam, or ``[]``."""
    strong = {m.group(0).lower() for m in _SPAM_STRONG.finditer(text)}
    weak = {m.group(0).lower().rstrip("s") for m in _SPAM_WEAK.finditer(text)}
    if strong or len(weak) >= 3:
        return sorted(strong | weak)
    return []


_PLACEHOLDER_TITLE = re.compile(
    r"^(?:welcome to nginx!?|apache2 \w+ default page.*|it works!?|index of /.*"
    r"|account suspended|this account has been suspended|website (?:disabled|suspended)"
    r"|domain default page|default web site page|iis windows server"
    r"|site not found.*|404 not found|502 bad gateway|503 service (?:temporarily )?unavailable"
    # Hosting platforms' pages for a site that is gone (Vercel, Wix, Kinsta, Netlify).
    r"|deployment paused|404: not_found|deployment not found|there is no website configured at this address"
    r"|domain configuration pending.*|connectyourdomain error.*|a brand new domain!?"
    r"|under construction.*|cname cross-user banned.*"
    # A Russian host's "domain not linked to any directory".
    r"|домен не прилинкован.*|хостинг успешно создан.*)$",
    re.IGNORECASE,
)
# An expired project domain bought for gambling or adult spam (Indonesian slot
# sites are the most common): unmistakable in the title alone.
_HIJACKED = re.compile(
    r"\bslot(?:s|88|777)?\b|\bgacor\b|\btogel\b|\bjudi\b|\bsitus\b|bandarqq|dominoqq"
    r"|\bmaxwin\b|\brtp\b|\bescorts?\b|\bterpercaya\b|\bhiburan\b|platform game online"
    r"|kenali platform|official indonesia|\bkubet\b|คาสิโน|최신 주소",
    re.IGNORECASE,
)
# Casino content; only spam when the page does not name the asset (BC.GAME and
# Lucky Block are casinos with tokens of their own).
_GAMBLING = re.compile(r"\bcasinos?\b|\bgambling\b|\bsportsbooks?\b", re.IGNORECASE)
# Affiliate and adult spam on an expired domain: spam even when the title
# carries the old project's name ("Best AI Girlfriend Apps ... - Jupiter").
_AFFILIATE = re.compile(
    r"\b(?:ai )?girlfriends?\b|\bporn|\bxxx\b|\bonlyfans\b|\bsex (?:chat|cam)|\bdating (?:site|app)s?\b"
    r"|\bviagra\b|\bpayday loans?\b|\bessay writ|\bcbd (?:oil|gumm)",
    re.IGNORECASE,
)
# An untitled page smaller than this is a hosting placeholder (13 fallback
# domains serve one identical 114-byte page).
EMPTY_PAGE_BYTES = 200
# The 114-byte page of a parking network: a script that sends visitors to
# /lander (achain.com, bakeryswap.org, riochain.io).
_LANDER = re.compile(r"""location(?:\.href)?\s*=\s*["']/lander["']""", re.IGNORECASE)
# A tiny page that only forwards by script is a site, not a placeholder.
_SCRIPT_REDIRECT = re.compile(
    r"""location(?:\.href)?\s*=\s*["'][^"']+["']""", re.IGNORECASE
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_BOT_BLOCK = frozenset({401, 403, 429})


@dataclass
class Check:
    """One homepage check, as cached in ``homepages.json``."""

    outcome: str
    checked: str
    status: int | None = None
    final_domain: str | None = None
    title: str = ""
    detail: str = ""
    # Consecutive failed checks on distinct days, and the day of the last one.
    failures: int = 0
    failed: str = ""

    def to_json(self) -> dict[str, Any]:
        row: dict[str, Any] = {"outcome": self.outcome, "checked": self.checked}
        for key in ("status", "final_domain", "title", "detail"):
            value = getattr(self, key)
            if value:
                row[key] = value
        if self.failures:
            row["failures"] = self.failures
            row["failed"] = self.failed
        return row

    @classmethod
    def from_json(cls, row: Mapping[str, Any]) -> Check:
        return cls(
            outcome=row["outcome"],
            checked=row["checked"],
            status=row.get("status"),
            final_domain=row.get("final_domain"),
            title=fix_mojibake(row.get("title", "")),
            detail=row.get("detail", ""),
            # Rows written before per-day strikes counted every attempt of
            # one run; without a day they are one strike.
            failures=row.get("failures", 0)
            if row.get("failed")
            else min(row.get("failures", 0), 1),
            failed=row.get("failed", ""),
        )

    @property
    def dead(self) -> bool:
        return self.failures >= DEAD_AFTER_FAILURES


def fix_mojibake(text: str) -> str:
    """UTF-8 read as Latin-1 (``Ã©`` for ``é``), as cached before the decoding fix."""
    if not re.search("[ÃÂÐÑâ][\x80-\xbf]", text):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def extract_title(body: str) -> str:
    match = _TITLE.search(body)
    if not match:
        return ""
    return " ".join(html.unescape(match.group(1)).split())[:200]


def parked_reason(title: str, body: str, final_domain: str | None) -> str | None:
    """Why a page is a parking, for-sale or placeholder page, or ``None``."""
    if final_domain in PARKING_DOMAINS:
        return f"lands on {final_domain}"
    match = _PARKED.search(title) or _PARKED_BODY.search(body)
    if match:
        return f"parking page ({match.group(0).strip().lower()})"
    if _PLACEHOLDER_TITLE.match(title.strip()):
        return f"placeholder page ({title.strip()})"
    return None


def title_problem(title: str, name: str, ticker: str = "") -> str | None:
    """Why a page title alone shows the domain is no longer the asset's site:
    a parking or placeholder page, or spam on an expired domain (gambling or
    adult content that does not name the asset: BC.GAME and Lucky Block are
    casinos with tokens of their own). Applied to cached titles on every build,
    so a new rule takes effect without refetching."""
    parked = parked_reason(title, "", None)
    if parked:
        return parked
    affiliate = _AFFILIATE.search(title)
    if affiliate:
        return f"hijacked: affiliate or adult spam ({affiliate.group(0).lower()})"
    if name_matches(name, ticker, "", title):
        return None
    hijacked = _HIJACKED.search(title)
    if hijacked:
        return f"hijacked: spam page ({hijacked.group(0).lower()})"
    if _GAMBLING.search(title):
        return f"gambling content that does not name the asset ({title[:60]})"
    return None


def classify(
    domain: str, status: int, final_url: str, body: str, checked: str
) -> Check:
    """The outcome of a page that answered (pure: the network part is elsewhere)."""
    title = extract_title(body)
    final_domain = domain_of(final_url) or domain
    parked = parked_reason(title, body, final_domain)
    final_host = urlsplit(final_url).hostname or ""
    if not parked and re.match(r"ww\d+\.", final_host):
        parked = f"parking redirect ({final_host})"
    if not parked and _LANDER.search(body):
        parked = "parking lander (a script sends visitors to /lander)"
    script_redirect = _SCRIPT_REDIRECT.search(body)
    tiny = not title and status < 400 and len(body.strip()) < EMPTY_PAGE_BYTES
    if not parked and tiny and not script_redirect:
        parked = "placeholder page (an empty page)"
    if parked:
        return Check("parked", checked, status, final_domain, title, parked)
    if final_domain != domain:
        words = spam_words(f"{title} {body}")
        if words:
            # The build decides: hijacked unless the page names the asset.
            return Check(
                "redirect", checked, status, final_domain, title, SPAM + ",".join(words)
            )
    if status in _BOT_BLOCK:
        return Check("bot_blocked", checked, status, final_domain, title)
    if status >= 400:
        return Check("http_error", checked, status, final_domain, title)
    if final_domain != domain:
        return Check("redirect", checked, status, final_domain, title)
    return Check("ok", checked, status, final_domain, title)


_REDIRECTS = frozenset({301, 302, 303, 307, 308})
# One request at a time per host, across workers: many dead domains forward to
# the same parking or hosting host.
_HOST_LOCKS: dict[str, threading.Lock] = {}
_HOST_LOCKS_GUARD = threading.Lock()


def _host_lock(url: str) -> threading.Lock:
    host = urlsplit(url).hostname or ""
    with _HOST_LOCKS_GUARD:
        return _HOST_LOCKS.setdefault(host, threading.Lock())


def _fetch(session: requests.Session, url: str) -> tuple[int, str, str]:
    """GET ``url``, following at most ``MAX_REDIRECTS`` redirects by hand so
    each hop waits for its host's lock; returns status, final URL and body."""
    for _ in range(MAX_REDIRECTS + 1):
        with _host_lock(url):
            response = session.get(
                url,
                timeout=TIMEOUT,
                stream=True,
                allow_redirects=False,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"},
            )
            try:
                location = response.headers.get("Location")
                if response.status_code in _REDIRECTS and location:
                    url = urljoin(url, location)
                    continue
                return response.status_code, url, _read_body(response)
            finally:
                response.close()
    raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")


def _read_body(response: requests.Response) -> str:
    chunks, size = [], 0
    content_type = response.headers.get("Content-Type", "")
    if "html" in content_type or not content_type:
        deadline = time.monotonic() + DEADLINE_SECONDS
        for chunk in response.iter_content(16 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_BODY_BYTES or time.monotonic() > deadline:
                break
    raw = b"".join(chunks)[:MAX_BODY_BYTES]
    # Without a declared charset requests assumes Latin-1; most pages are UTF-8.
    declared = "charset" in content_type.lower()
    encoding = response.encoding if declared and response.encoding else "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def check_homepage(domain: str, checked: str, previous: Check | None = None) -> Check:
    """Fetch a domain's homepage and classify it.

    A failed fetch keeps the previous verdict (outcome, title, final domain):
    only the dead rule acts on unreachability, and a second strike counts on a
    later calendar day only, so neither a blip nor a same-day re-run unmaps a
    domain or reverts a rebrand.
    """
    session = requests.Session()
    errors = []
    for url in (f"https://{domain}/", f"http://{domain}/", f"https://www.{domain}/"):
        try:
            status, final_url, body = _fetch(session, url)
        except requests.TooManyRedirects:
            return Check("http_error", checked, None, None, "", "too many redirects")
        except (requests.RequestException, UnicodeError, LookupError) as error:
            errors.append(f"{url}: {type(error).__name__}")
            continue
        return classify(domain, status, final_url, body, checked)
    day = checked[:10]
    failures = previous.failures if previous else 0
    if not (previous and previous.failed == day):
        failures += 1
    detail = "; ".join(errors)[:300]
    if previous and previous.outcome != "unreachable":
        return replace(previous, failures=failures, failed=day)
    return Check("unreachable", checked, detail=detail, failures=failures, failed=day)


def check_all(
    domains: Iterable[str],
    checked: str,
    previous: Mapping[str, Check],
    workers: int = WORKERS,
) -> dict[str, Check]:
    """Check each domain once, ``workers`` at a time."""
    todo = sorted(set(domains))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda d: check_homepage(d, checked, previous.get(d)), todo)
        return dict(zip(todo, results, strict=True))


# ----------------------------------------------------------------------------
# Identity evidence

_GENERIC_WORDS = frozenset(
    {
        "token",
        "coin",
        "network",
        "protocol",
        "finance",
        "the",
        "chain",
        "labs",
        "dao",
        "inc",
        "corp",
        "corporation",
        "company",
        "group",
        "holdings",
        "ltd",
        "plc",
        "and",
        "of",
        "ai",
        "swap",
        "exchange",
        "wrapped",
        "classic",
        "new",
        "official",
        "project",
        "foundation",
        "digital",
        "global",
        "world",
        "money",
    }
)


def _fold(text: str) -> str:
    return (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    )


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(text))


def name_matches(name: str, ticker: str, domain: str, title: str) -> list[str]:
    """Evidence that a page is about the asset: its name or ticker in the title
    or the domain's own label."""
    evidence = []
    label = _alnum(domain.split(".", 1)[0]) if domain else ""
    name_key = _alnum(name)
    words = [
        w
        for w in re.findall(r"[a-z0-9]+", _fold(name))
        if len(w) >= 3 and w not in _GENERIC_WORDS
    ]
    tick = ticker.lower()
    if label and (
        (len(label) >= 3 and label not in _GENERIC_WORDS and label in name_key)
        or (len(name_key) >= 4 and name_key in label)
        or any(len(w) >= 4 and w in label for w in words)
        # A ticker inside a label is weak (ABCD in cabcd.org): it must lead it.
        or (len(tick) >= 3 and label.startswith(tick))
    ):
        evidence.append("name_in_domain")
    title_words = set(re.findall(r"[a-z0-9]+", _fold(title)))
    title_key = _alnum(title)
    if title and (
        (len(name_key) >= 4 and name_key in title_key)
        or any(w in title_words for w in words if len(w) >= 4)
        # "BCGame Coin" in "BC Game: Crypto Casino": long words, spaces ignored.
        or any(w in title_key for w in words if len(w) >= 5)
        or (len(tick) >= 3 and tick in title_words)
    ):
        evidence.append("name_in_title")
    return evidence


def names_agree(a: str, b: str) -> bool:
    """Whether two names of one company share a distinctive word or contain each other."""
    ka, kb = _alnum(a), _alnum(b)
    if len(ka) >= 3 and len(kb) >= 3 and (ka in kb or kb in ka):
        return True
    for long, short in ((a, kb), (b, ka)):
        # Advanced Micro Devices = AMD, Taiwan Semiconductor Manufacturing = TSMC.
        initials = "".join(w[0] for w in re.findall(r"[a-z0-9]+", _fold(long)))
        if (
            len(short) >= 3
            and len(initials) >= 3
            and (initials.startswith(short) or short.startswith(initials))
        ):
            return True

    def words(text: str) -> set[str]:
        return {
            w
            for w in re.findall(r"[a-z0-9]+", _fold(text))
            if len(w) >= 3 and w not in _GENERIC_WORDS
        }

    return bool(words(a) & words(b))
