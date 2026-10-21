"""``python -m integrations.web``: rebuild and verify the web domain map."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import report
from .build import ROOT, Budgets, Fetchers, Paths, build, dumps_map, now_utc
from .sources import make_client


def _git_head() -> str:
    try:
        out = subprocess.run(
            # The last commit that changed the Atlas data the map is built
            # from (not the map itself): stable across code-only commits.
            [
                "git",
                "-C",
                str(ROOT),
                "log",
                "-1",
                "--format=%H",
                "--",
                "atlas/data",
                ":(exclude)atlas/data/web",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="rebuild from the committed caches only (no network)",
    )
    defaults = Budgets()
    for name in (
        "cmc_refresh",
        "symbol_refresh",
        "wikidata_refresh",
        "homepage_refresh",
        "coingecko",
        "sitelinks_refresh",
    ):
        parser.add_argument(
            f"--{name.replace('_', '-')}",
            type=int,
            default=getattr(defaults, name),
            help=f"requests beyond new keys (default {getattr(defaults, name)})",
        )
    parser.add_argument(
        "--accept-large-change",
        action="store_true",
        help="write the map even when it loses more than "
        f"max({report.LARGE_CHANGE_ENTRIES}, {report.LARGE_CHANGE_SHARE:.0%}) entries or domains",
    )
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--summary-path", type=Path)
    args = parser.parse_args(argv)

    paths = Paths()
    budgets = Budgets(
        cmc_refresh=args.cmc_refresh,
        symbol_refresh=args.symbol_refresh,
        wikidata_refresh=args.wikidata_refresh,
        homepage_refresh=args.homepage_refresh,
        coingecko=args.coingecko,
        sitelinks_refresh=args.sitelinks_refresh,
    )
    client = make_client()
    fetchers = None if args.offline else Fetchers.live(client)
    old = (
        json.loads(paths.map.read_text(encoding="utf-8"))
        if paths.map.exists()
        else None
    )
    result = build(paths, fetchers, now_utc(), budgets, atlas_source=_git_head())
    meaningful = report.meaningful_change(old, result.map)
    lost = report.losses(old, result.map)
    blocked = report.large_change(old, result.map) and not args.accept_large_change
    if meaningful and not blocked:
        paths.map.write_text(dumps_map(result.map), encoding="utf-8")

    changes = report.diff(old, result.map)
    stats = report.counts(result.map)
    requests = {host: asdict(s) for host, s in client.stats.items()}
    text = report.markdown(
        changes, stats, result.warnings, result.upstream_errors, requests
    )
    if blocked:
        text = (
            f"## Large change: not written\n\nThis rebuild loses {lost} entries or domains "
            "(more than the guard allows). Upstream or runner outages look like this; "
            "check the errors below, and re-run with `--accept-large-change` (the "
            "workflow's `accept-large-change` input) only if the losses are real.\n\n"
            + text
        )
    print(text)
    if args.report_path:
        args.report_path.write_text(text, encoding="utf-8")
    if args.summary_path:
        summary = {
            "has_changes": meaningful and report.has_changes(changes),
            "blocked": blocked,
            "losses": lost,
            "entries": stats["entries"],
            "domains": stats["domains"],
            "flagged": stats["status"].get("flagged", 0),
            "added": len(changes["added"]),
            "removed": len(changes["removed"]),
            "changed": len(changes["changed_domain"]) + len(changes["changed_article"]),
            "newly_flagged": len(changes["newly_flagged"]),
            "upstream_errors": len(result.upstream_errors),
        }
        args.summary_path.write_text(
            json.dumps(summary, indent=1) + "\n", encoding="utf-8"
        )
    return 3 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
