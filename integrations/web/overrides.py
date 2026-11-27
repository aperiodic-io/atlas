"""``atlas/data/web/overrides.json``: the hand-maintained part of the map.

```json
{
  "schema_version": 1,
  "blocked_hosts": ["x.com", "t.me", "pump.fun"],
  "aliases": {"TESLA": "TSLA"},
  "entries": {
    "XAU": {"asset_class": "commodity", "name": "Gold", "domain": null,
            "wikipedia": "Gold", "reason": "a commodity has no website"}
  }
}
```

* ``blocked_hosts``: hosts that are never an asset's own site (social networks,
  link-in-bio pages, explorers, launchpads, code and docs hosting,
  aggregators, tokenised-stock issuers); their subdomains are blocked too.
  Exchanges are not blocked: an exchange token's site is its exchange.
* ``aliases``: normalised symbol -> asset key, for snapshot rows Atlas
  symbology has not resolved (``TESLA`` perps on an uncovered venue).
* ``entries``: map key (Atlas ``asset_id``, or the symbol of a TradFi
  underlying Atlas has no asset for) -> forced fields. An absent field keeps
  what the sources found; ``null`` forces "none". Every entry says why in
  ``reason``. Overrides win over every source and over the homepage check.
  ``same_project_as`` names the asset this one is the same project as (a
  rename, a share class), so both may publish the project's domain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import is_blocked, registrable

SCHEMA_VERSION = 1
_FIELDS = frozenset(
    {
        "asset_class",
        "name",
        "domain",
        "wikipedia",
        "reason",
        "same_project_as",
        "own_platform",
    }
)
ASSET_CLASSES = frozenset(
    {"crypto", "equity", "pre_market", "commodity", "index", "forex"}
)
_UNSET = object()


@dataclass(frozen=True)
class Forced:
    reason: str
    asset_class: str | None = None
    # The key of the asset this one is the same project as (a rename, a share
    # class): both may publish the project's domain.
    same_project_as: str | None = None
    # The domain is a blocked platform host and this asset is the platform's
    # own (its token, or the company: Alphabet on google.com).
    own_platform: bool = False
    name: str | None = None
    # ``_UNSET`` = not forced; ``None`` = forced to none.
    domain: Any = _UNSET
    wikipedia: Any = _UNSET

    @property
    def forces_domain(self) -> bool:
        return self.domain is not _UNSET

    @property
    def forces_wikipedia(self) -> bool:
        return self.wikipedia is not _UNSET


@dataclass(frozen=True)
class Overrides:
    blocked_hosts: tuple[str, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)
    entries: dict[str, Forced] = field(default_factory=dict)

    def is_blocked(self, host: str) -> bool:
        return is_blocked(host, self.blocked_hosts)


def parse_overrides(payload: dict[str, Any]) -> Overrides:
    """Validate and load the overrides; a mistake fails the build, loudly."""
    unknown = set(payload) - {"schema_version", "blocked_hosts", "aliases", "entries"}
    if unknown or payload.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValueError(f"overrides: unexpected keys or schema {sorted(unknown)}")
    blocked = tuple(payload.get("blocked_hosts", ()))
    aliases = dict(payload.get("aliases", {}))
    for source, target in aliases.items():
        if target in aliases:
            raise ValueError(
                f"overrides: alias {source} -> {target} points at another alias"
            )
    entries = {}
    for key, row in payload.get("entries", {}).items():
        extra = set(row) - _FIELDS
        if extra:
            raise ValueError(f"overrides: {key} has unknown fields {sorted(extra)}")
        if not row.get("reason"):
            raise ValueError(f"overrides: {key} needs a reason")
        if key in aliases:
            raise ValueError(
                f"overrides: {key} is an alias; key the entry by its target"
            )
        if (
            row.get("asset_class") is not None
            and row["asset_class"] not in ASSET_CLASSES
        ):
            raise ValueError(
                f"overrides: {key}: unknown asset_class {row['asset_class']}"
            )
        domain = row.get("domain", _UNSET)
        # A blocked host is allowed only for the platform's own asset (PUMP on
        # pump.fun, Meta on facebook.com), and only when the override says so.
        own_platform = row.get("own_platform") is True
        if isinstance(domain, str) and (
            registrable(domain) != domain
            or (is_blocked(domain, blocked) and not own_platform)
        ):
            raise ValueError(
                f"overrides: {key}: {domain} is not an allowed registrable domain"
            )
        wikipedia = row.get("wikipedia", _UNSET)
        if isinstance(wikipedia, str) and " " in wikipedia:
            raise ValueError(f"overrides: {key}: use underscores in {wikipedia}")
        entries[key] = Forced(
            reason=row["reason"],
            asset_class=row.get("asset_class"),
            same_project_as=row.get("same_project_as"),
            own_platform=row.get("own_platform") is True,
            name=row.get("name"),
            domain=domain,
            wikipedia=wikipedia,
        )
    return Overrides(blocked_hosts=blocked, aliases=aliases, entries=entries)


def load_overrides(path: Path) -> Overrides:
    return parse_overrides(json.loads(path.read_text(encoding="utf-8")))
