"""Bring teams' org names, on their records and imported data, in step with their orgs.

Importers copy ``organization_title`` from the team record onto every data doc,
and org-scoped dashboards filter on that copy. Org renames used to update only
the org record, and wizard / invite team creation never wrote the name at all,
so team records and their data carry old names or "Unknown". This
tool resolves each team's current org name(s) from its ``organization_esid``,
then reports or repairs the team record and the team's data docs.

A team whose orgs cannot all be resolved is skipped and listed, never guessed.

Report-only by default; ``--apply`` writes (synchronously, one team at a time).
Idempotent: a second run finds nothing to do. Output is team ids and counts
only — org names are tenant data.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 -m tools.restamp_org_titles            # report
    docker exec -w /app/python imports_container python3 -m tools.restamp_org_titles --apply
    docker exec -w /app/python imports_container python3 -m tools.restamp_org_titles --team-id <esid> --apply

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
from lib.org_title_restamp import (  # noqa: E402
    TEAM_INDEX,
    count_stale_org_title_docs,
    resolve_org_titles,
    restamp_org_titles,
    sync_team_org_titles,
)

logger = logging.getLogger(__name__)


@dataclass
class Report:
    teams_scanned: int = 0
    teams_unresolved: list[str] = field(default_factory=list)
    records_stale: int = 0
    records_updated: int = 0
    teams_stale: int = 0
    docs_stale: int = 0
    docs_updated: int = 0
    teams_left: list[str] = field(default_factory=list)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in (value or []) if str(v or "").strip()]


def _teams(es: Any, team_ids: list[str] | None) -> list[tuple[str, dict]]:
    if team_ids:
        return [(team_id, es.get(index=TEAM_INDEX, id=team_id).get("_source") or {}) for team_id in team_ids]
    return [(h["_id"], h.get("_source") or {}) for h in get_all_documents_paginated(TEAM_INDEX, es=es)]


def run(es: Any, *, apply: bool, team_ids: list[str] | None = None) -> Report:
    report = Report()
    for team_id, team in _teams(es, team_ids):
        report.teams_scanned += 1
        titles = resolve_org_titles(es, team.get("organization_esid"))
        if titles is None:
            logger.warning("skip %s: cannot resolve its org name(s)", team_id)
            report.teams_unresolved.append(team_id)
            continue
        record_stale = _as_list(team.get("organization_title")) != titles
        if record_stale:
            report.records_stale += 1
        try:
            stale = count_stale_org_title_docs(es, team_id, titles)
        except ValueError as e:
            # build_data_index_name refuses an esid ES cannot use in an index name.
            logger.warning("skip %s: %s", team_id, e)
            continue
        if stale:
            report.teams_stale += 1
            report.docs_stale += stale
        if not apply:
            if record_stale or stale:
                logger.info(
                    "WOULD update %s: team record %s, %d data doc(s)", team_id, "stale" if record_stale else "ok", stale
                )
            continue
        if record_stale:
            sync_team_org_titles(es, team_id)
            report.records_updated += 1
        if not stale:
            continue
        result = restamp_org_titles(es, team_id, titles, wait_for_completion=True)
        updated = int(result.get("updated", 0))
        report.docs_updated += updated
        left = count_stale_org_title_docs(es, team_id, titles)
        if left:
            report.teams_left.append(team_id)
        logger.info("restamped %s: %d doc(s), %d left", team_id, updated, left)
    return report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Bring teams' org names, on their records and data, in step with their orgs.")
    ap.add_argument("--apply", action="store_true", help="write the current names (default: report only)")
    ap.add_argument("--team-id", action="append", dest="team_ids", help="restrict to team esid(s)")
    args = ap.parse_args(argv)

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return 1

    report = run(es, apply=args.apply, team_ids=args.team_ids)
    logger.info(
        "%s teams_scanned=%d teams_unresolved=%d records_stale=%d records_updated=%d "
        "teams_stale=%d docs_stale=%d docs_updated=%d teams_left=%d",
        "APPLY" if args.apply else "REPORT",
        report.teams_scanned,
        len(report.teams_unresolved),
        report.records_stale,
        report.records_updated,
        report.teams_stale,
        report.docs_stale,
        report.docs_updated,
        len(report.teams_left),
    )
    if report.teams_unresolved:
        logger.info("unresolved (fix their organization_esid by hand): %s", ", ".join(report.teams_unresolved))
    if not args.apply:
        logger.info("Report only. Re-run with --apply to write.")
        return 0
    return 2 if report.teams_left else 0


if __name__ == "__main__":
    raise SystemExit(main())
