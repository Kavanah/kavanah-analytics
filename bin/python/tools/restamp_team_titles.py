"""Restamp ``team_title`` on teams' data docs that still carry an old team name.

Dashboards filter imported data on its stored ``team_title``. Before rename
paths restamped the data (lib.team_title_restamp), every rename left the
team's history under the old name and out of every dashboard. This tool finds,
per team in ``de_asset_teams``, the data docs in its ``{team_esid}__*`` indices
whose ``team_title`` differs from the team's current name, and rewrites them.
It is also the repair when a rename-time restamp failed or an import that was
mid-run during a rename stamped the old name.

Report-only by default; ``--apply`` writes (synchronously, one team at a time).
Idempotent: a second run finds nothing to do. Output is team ids and counts
only — team names are tenant data.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 -m tools.restamp_team_titles            # report
    docker exec -w /app/python imports_container python3 -m tools.restamp_team_titles --apply
    docker exec -w /app/python imports_container python3 -m tools.restamp_team_titles --team-id <esid> --apply

Exit status: 0 when nothing is left to do (or a report completed), 2 when
``--apply`` left docs behind (version conflicts with a running import — re-run),
1 on a hard error (no ES).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "lib").exists():
        sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_all_documents_paginated, get_elasticsearch_client  # noqa: E402
from lib.team_title_restamp import count_stale_team_title_docs, restamp_team_title  # noqa: E402

logger = logging.getLogger(__name__)

TEAMS_INDEX = "de_asset_teams"


@dataclass
class Report:
    teams_scanned: int = 0
    teams_stale: int = 0
    docs_stale: int = 0
    docs_updated: int = 0
    teams_left: list[str] = field(default_factory=list)


def _teams(es: Any, team_ids: list[str] | None) -> list[tuple[str, str]]:
    if team_ids:
        pairs = []
        for team_id in team_ids:
            doc = es.get(index=TEAMS_INDEX, id=team_id)
            pairs.append((team_id, (doc.get("_source") or {}).get("team_title") or ""))
        return pairs
    return [
        (h["_id"], (h.get("_source") or {}).get("team_title") or "") for h in get_all_documents_paginated(TEAMS_INDEX, es=es)
    ]


def run(es: Any, *, apply: bool, team_ids: list[str] | None = None) -> Report:
    report = Report()
    for team_id, title in _teams(es, team_ids):
        report.teams_scanned += 1
        if not title.strip():
            logger.warning("skip %s: team has no title", team_id)
            continue
        try:
            stale = count_stale_team_title_docs(es, team_id, title)
        except ValueError as e:
            # build_data_index_name refuses an esid ES cannot use in an index name.
            logger.warning("skip %s: %s", team_id, e)
            continue
        if not stale:
            continue
        report.teams_stale += 1
        report.docs_stale += stale
        if not apply:
            logger.info("WOULD restamp %s: %d doc(s)", team_id, stale)
            continue
        result = restamp_team_title(es, team_id, title, wait_for_completion=True)
        updated = int(result.get("updated", 0))
        report.docs_updated += updated
        left = count_stale_team_title_docs(es, team_id, title)
        if left:
            report.teams_left.append(team_id)
        logger.info("restamped %s: %d doc(s), %d left", team_id, updated, left)
    return report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Restamp team_title on data docs that carry an old team name.")
    ap.add_argument("--apply", action="store_true", help="write the current names (default: report only)")
    ap.add_argument("--team-id", action="append", dest="team_ids", help="restrict to team esid(s)")
    args = ap.parse_args(argv)

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return 1

    report = run(es, apply=args.apply, team_ids=args.team_ids)
    logger.info(
        "%s teams_scanned=%d teams_stale=%d docs_stale=%d docs_updated=%d teams_left=%d",
        "APPLY" if args.apply else "REPORT",
        report.teams_scanned,
        report.teams_stale,
        report.docs_stale,
        report.docs_updated,
        len(report.teams_left),
    )
    if not args.apply:
        logger.info("Report only. Re-run with --apply to write.")
        return 0
    return 2 if report.teams_left else 0


if __name__ == "__main__":
    raise SystemExit(main())
