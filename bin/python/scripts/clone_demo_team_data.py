#!/usr/bin/env python3
"""Clone a real team's platform data into the demo team, anonymized.

Daily cron (prod, after the real imports have landed)::

    docker exec imports_container python3 /app/python/scripts/clone_demo_team_data.py \\
        --source-team "<consenting team_title>" --apply --since 3d

One-time backfill at cutover (destination indices rebuilt under the schema)::

    docker exec imports_container python3 /app/python/scripts/clone_demo_team_data.py \\
        --source-team "<consenting team_title>" --apply --full --recreate --snapshot-repo daily_repo

Dry-run is the default and writes nothing. ``--apply`` must be given
explicitly; ``--recreate`` refuses without a restore point. See
``demo_clone/policy.py`` for what happens to every field.

the pseudonym salt is the secret ``DEMO_CLONE_SALT`` environment
variable (SSM → compose .env). Without it the run refuses before touching
Elasticsearch; after rotating it, run once with ``--full --recreate``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from demo_clone import DemoCloneError  # noqa: E402
from demo_clone.runner import CLONED_SOURCES, run  # noqa: E402
from lib.es_helpers import get_elasticsearch_client  # noqa: E402
from migration.snapshot_gate import MigrationRefused, finalize_report, resolve_apply_mode  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # No default on purpose: the consenting source team is an operational
    # fact (runbook / crontab), not something the open-source code names.
    parser.add_argument("--source-team", required=True, help="team_title to copy FROM (exact match)")
    parser.add_argument(
        "--target-team", default="Kavanah Team", help="team_title to copy INTO (exact; default 'Kavanah Team')"
    )
    parser.add_argument(
        "--sources",
        default=",".join(CLONED_SOURCES),
        help="comma-separated source slugs (default: all clonable sources)",
    )
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--since", default="3d", help="only docs whose @timestamp >= now-<since> (ES date math; default 3d)")
    window.add_argument("--full", action="store_true", help="every document in each source index")
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run that changes nothing)")
    parser.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    parser.add_argument("--recreate", action="store_true", help="delete each destination index first (needs --snapshot-repo)")
    parser.add_argument("--snapshot-repo", default=None, help="snapshot repository for the --recreate restore point")
    parser.add_argument("--report-json", default=None, help="write the run report to this path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    try:
        dry_run, snapshot_repo = resolve_apply_mode(
            {
                "apply": "true" if args.apply else None,
                "dry-run": "true" if args.dry_run else None,
                "snapshot-repo": args.snapshot_repo,
            }
        )
    except MigrationRefused as exc:
        logger.error("%s", exc)
        return 1

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Failed to connect to Elasticsearch.")
        return 1

    try:
        report = run(
            es,
            source_team_title=args.source_team,
            target_team_title=args.target_team,
            sources=[s.strip() for s in args.sources.split(",") if s.strip()],
            since=None if args.full else args.since,
            dry_run=dry_run,
            recreate=args.recreate,
            snapshot_repo=snapshot_repo,
        )
    except DemoCloneError as exc:
        logger.error("Refused: %s", exc)
        return 1

    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)

    outcome = finalize_report(report, dry_run=dry_run)
    return outcome if isinstance(outcome, int) else 0


if __name__ == "__main__":
    sys.exit(main())
