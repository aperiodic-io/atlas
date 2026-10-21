"""What changed between two maps, for the pull request and the job summary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

LIST_LIMIT = 60


def _key(entry: Mapping[str, Any]) -> str:
    return entry.get("asset_id") or entry["ticker"]


def diff(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> dict[str, Any]:
    before = {_key(e): e for e in (old or {}).get("entries", [])}
    after = {_key(e): e for e in new["entries"]}
    changed_domain, changed_article, newly_flagged = [], [], []
    for key in sorted(set(before) & set(after)):
        b, a = before[key], after[key]
        if b.get("domain") != a.get("domain"):
            changed_domain.append((key, b.get("domain"), a.get("domain")))
        if b.get("wikipedia") != a.get("wikipedia"):
            changed_article.append((key, b.get("wikipedia"), a.get("wikipedia")))
    for key in sorted(after):
        flags = set(after[key].get("verification", {}).get("flags", []))
        old_flags = set(before.get(key, {}).get("verification", {}).get("flags", []))
        if flags - old_flags:
            newly_flagged.append(
                (key, after[key].get("domain"), sorted(flags - old_flags))
            )
    return {
        "added": [
            (k, after[k].get("domain"), after[k].get("wikipedia"))
            for k in sorted(set(after) - set(before))
        ],
        "removed": [
            (k, before[k].get("domain"), new["unmapped"].get(k, ""))
            for k in sorted(set(before) - set(after))
        ],
        "changed_domain": changed_domain,
        "changed_article": changed_article,
        "newly_flagged": newly_flagged,
    }


def counts(file: Mapping[str, Any]) -> dict[str, Any]:
    by_class: dict[str, Counter[str]] = {}
    status: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    for entry in file["entries"]:
        c = by_class.setdefault(entry["asset_class"], Counter())
        c["entries"] += 1
        c["domain"] += "domain" in entry
        c["wikipedia"] += "wikipedia" in entry
        verification = entry.get("verification", {})
        status[verification.get("status", "")] += 1
        c[verification.get("status", "")] += 1
        for flag in verification.get("flags", []):
            flags[flag.split(":", 1)[0]] += 1
    return {
        "entries": len(file["entries"]),
        "domains": len({e["domain"] for e in file["entries"] if "domain" in e}),
        "unmapped": len(file["unmapped"]),
        "by_class": {k: dict(v) for k, v in sorted(by_class.items())},
        "status": dict(sorted(status.items())),
        "flags": dict(flags.most_common()),
    }


def _code(value: Any) -> str:
    """Untrusted text (page titles in reasons) as inline code, so a hijacked
    page cannot inject links or mentions into the PR body."""
    return "`" + str(value).replace("`", "'") + "`"


def _rows(rows: Sequence[tuple[Any, ...]]) -> list[str]:
    out = [
        f"- {_code(r[0])}: " + " → ".join(_code(x) for x in r[1:])
        for r in rows[:LIST_LIMIT]
    ]
    if len(rows) > LIST_LIMIT:
        out.append(f"- … and {len(rows) - LIST_LIMIT} more")
    return out


def markdown(
    changes: Mapping[str, Any],
    stats: Mapping[str, Any],
    warnings: Sequence[str],
    errors: Sequence[str],
    requests: Mapping[str, Any],
) -> str:
    lines = ["## Web domain map", ""]
    lines.append(
        f"{stats['entries']} entries ({stats['domains']} domains), {stats['unmapped']} unmapped. "
        f"Verification: "
        + ", ".join(f"{k} {v}" for k, v in stats["status"].items())
        + "."
    )
    lines += [
        "",
        "| class | entries | domain | wikipedia | ok | redirect | flagged |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for cls, c in stats["by_class"].items():
        lines.append(
            f"| {cls} | {c.get('entries', 0)} | {c.get('domain', 0)} | {c.get('wikipedia', 0)} | "
            f"{c.get('ok', 0)} | {c.get('redirect', 0)} | {c.get('flagged', 0)} |"
        )
    for title, key in (
        ("Added", "added"),
        ("Removed", "removed"),
        ("Domain changed", "changed_domain"),
        ("Article changed", "changed_article"),
        ("Newly flagged", "newly_flagged"),
    ):
        rows = changes[key]
        lines += ["", f"### {title} ({len(rows)})", ""]
        lines += _rows(rows) or ["- none"]
    if stats["flags"]:
        lines += ["", "### Flags", ""] + [
            f"- `{k}`: {v}" for k, v in stats["flags"].items()
        ]
    if warnings:
        lines += ["", "### Warnings", ""] + [f"- {w}" for w in warnings[:LIST_LIMIT]]
    if errors:
        lines += [
            "",
            "### Upstream errors (the affected rows keep their cached values)",
            "",
        ] + [f"- {e}" for e in errors[:LIST_LIMIT]]
    if requests:
        lines += ["", "### Requests", ""] + [
            f"- {host}: {s}" for host, s in sorted(requests.items())
        ]
    return "\n".join(lines) + "\n"


def has_changes(changes: Mapping[str, Any]) -> bool:
    return any(
        changes[k]
        for k in (
            "added",
            "removed",
            "changed_domain",
            "changed_article",
            "newly_flagged",
        )
    )


# A rebuild that loses more than this many entries or domains (or this share of
# the map) is not proposed without acknowledgement: an upstream or runner
# outage must not silently drop mappings.
LARGE_CHANGE_ENTRIES = 20
LARGE_CHANGE_SHARE = 0.02


def losses(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> int:
    """Entries of ``old`` that are gone, or whose domain no entry has any more."""
    after = {_key(e): e for e in new["entries"]}
    domains = {e.get("domain") for e in new["entries"]} - {None}
    lost = 0
    for entry in (old or {}).get("entries", []):
        now = after.get(_key(entry))
        if now is None or (entry.get("domain") and entry["domain"] not in domains):
            lost += 1
    return lost


def large_change(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> bool:
    size = len((old or {}).get("entries", []))
    return losses(old, new) > max(LARGE_CHANGE_ENTRIES, LARGE_CHANGE_SHARE * size)


def _comparable(file: Mapping[str, Any] | None) -> Any:
    if not file:
        return None
    entries = []
    for entry in file.get("entries", []):
        verification = {
            k: v for k, v in entry.get("verification", {}).items() if k != "checked"
        }
        entries.append({**entry, "verification": verification})
    return entries, file.get("unmapped", {})


def meaningful_change(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> bool:
    """Whether the map changed beyond ``generated_at`` and check dates."""
    return _comparable(old) != _comparable(new)
