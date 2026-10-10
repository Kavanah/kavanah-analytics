#!/usr/bin/env python3
"""Backfill no_data markers for FB Ad Insights gaps.

Acceptance criterion 4 of for any team currently missing markers
on legitimately-empty dates deploy (2026-05-04), write the
missing markers without re-running the full import.

For each team with ``ds_facebook=true``, query the team's
``*_fb_insights`` index, compute the set of dates in the requested window
that have no real (non-marker) doc, and upsert one no_data marker per gap.

Usage (inside imports_container):
    python3 /app/python/scripts/backfill_no_data_markers.py \\
        --start 2026-05-04 --end 2026-05-07 [--team_ids TEAM_A,TEAM_B] [--dry-run]

Idempotent — re-running upserts the same marker docs (deterministic ids).

The script does not delete or modify any real ad-insights docs and does
not call the Facebook API.
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, "/app/python")

import lib.logging  # noqa: E402  — sets up shared logging config

from import_scripts.facebook.lib.assets import resolve_team_filter_scope  # noqa: E402
from import_scripts.facebook.get_insights import get_all_teams_by_page_id  # noqa: E402
from lib.no_data_markers import backfill_missing_no_data_markers  # noqa: E402


def main():
    lib.logging.define("fb-backfill-markers")
    parser = argparse.ArgumentParser(description="no_data marker backfill")
    parser.add_argument("--start", required=True, help="ISO date (YYYY-MM-DD), inclusive")
    parser.add_argument("--end", required=True, help="ISO date (YYYY-MM-DD), inclusive")
    parser.add_argument(
        "--team_ids",
        default=None,
        help="Comma-separated team title filter — same semantics as get_insights.py. "
        "When omitted, every ds_facebook=true team is considered.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the gap counts per team without writing any markers.",
    )
    args = parser.parse_args()

    scope = resolve_team_filter_scope(args.team_ids) if args.team_ids else None
    if scope and not scope.get("team_esids"):
        logging.warning(f"team_ids={args.team_ids!r} resolved to zero teams — nothing to backfill")
        return 0

    scope_team_esids = scope.get("team_esids") if scope else None
    page_id_to_team = get_all_teams_by_page_id(team_esids=scope_team_esids)

    if not page_id_to_team:
        logging.warning("No ds_facebook teams with page_ids found — nothing to backfill")
        return 0

    if args.dry_run:
        # Build the unique team list and print expected gap counts.
        teams = {}
        for team in page_id_to_team.values():
            esid = team.get("_id")
            title = team.get("team_title", "unknown")
            if esid and esid not in teams:
                teams[esid] = title
        from datetime import datetime, timedelta
        from lib.no_data_markers import _team_dates_with_real_docs, sanitize_team_title
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            logging.error("ES client unavailable")
            return 1

        all_dates = []
        cur = datetime.strptime(args.start, "%Y-%m-%d").date()
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
        while cur <= end:
            all_dates.append(cur.isoformat())
            cur += timedelta(days=1)

        total_gaps = 0
        for esid, title in teams.items():
            idx = f"{sanitize_team_title(title)}_fb_insights"
            covered = _team_dates_with_real_docs(es, idx, args.start, args.end)
            gaps = [d for d in all_dates if d not in covered]
            if gaps:
                logging.info(f"DRY-RUN: {title} ({esid}) → {len(gaps)} gap(s): {gaps}")
                total_gaps += len(gaps)
        logging.info(f"DRY-RUN total: {total_gaps} marker(s) would be written")
        return 0

    wrote = backfill_missing_no_data_markers(
        start=args.start,
        end=args.end,
        index_suffix="_fb_insights",
        team_mapping=page_id_to_team,
    )
    logging.info(f"Backfill complete: wrote {wrote} no_data marker(s) in [{args.start}, {args.end}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
