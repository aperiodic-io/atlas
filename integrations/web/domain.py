"""The one definition of an asset's "domain".

Atlas's rule for the ``domain`` of a map entry: ``normalize_host`` reduces a URL
or host to a lower-case host name and ``registrable`` keeps its public suffix
plus one label (Public Suffix List, private suffixes included). Consumers that
match ranking lists against this map must normalise hosts identically;
``tests/test_web_domain.py`` holds the rule's test vectors.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache

from publicsuffixlist import PublicSuffixList

_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def normalize_host(value: str) -> str | None:
    """A URL, origin or bare host reduced to its lower-case host name.

    Scheme, userinfo, port, path, query, fragment, a trailing dot and one
    leading ``www.`` are dropped. ``None`` when what is left is not a plausible
    host name.
    """
    s = value.strip()
    if "://" in s:
        s = s[s.index("://") + 3 :]
    for separator in "/?#":
        s = s.split(separator, 1)[0]
    if "@" in s:
        s = s[s.rindex("@") + 1 :]
    if ":" in s:
        s = s[: s.rindex(":")]
    host = s.rstrip(".").translate(_ASCII_LOWER)
    host = host.removeprefix("www.")
    plausible = (
        "." in host
        and not host.startswith(".")
        and ".." not in host
        and all(char in _HOST_CHARS for char in host)
    )
    return host if plausible else None


@cache
def _psl() -> PublicSuffixList:
    # The list bundled with the package: no network, and private suffixes
    # (github.io, vercel.app) count.
    return PublicSuffixList()


def registrable(host: str) -> str | None:
    """The registrable domain (public suffix plus one label) of a normalised host."""
    return _psl().privatesuffix(host)


def domain_of(value: str) -> str | None:
    """The registrable domain of a URL or host, or ``None``."""
    host = normalize_host(value)
    return registrable(host) if host else None


def is_blocked(host: str, blocked_hosts: Iterable[str]) -> bool:
    """Whether ``host`` is, or is a subdomain of, one of ``blocked_hosts``."""
    return any(host == b or host.endswith("." + b) for b in blocked_hosts)
