"""One-shot cleanup: template dashboards whose team no longer exists.

Starter-pack / first-look templates are installed once per team
(``TemplateService.instantiate`` is idempotent on ``(team_esid,
template_id)``), so several same-titled dashboards is normal: one per team.
Deleting a team leaves its copies behind, and
``ReferenceCleanupService`` then strips the team from their
``allowed_team_esids`` — so the copy is ``show_teams`` with nobody allowed:
visible only to privileged users, who see it as an unexplained duplicate.

The plan sorts every dashboard in ``de_asset_echarts_dashboards`` into:

* ``delete`` — ``from_template`` copies whose ``template_team_esid`` is no
  longer a team, that no live team or org can still see (``show_teams`` with
  no live ``allowed_team_esids`` / ``allowed_org_esids``), AND that have no
  ``created_by`` (the Builder read gate always lets the creator see their own
  dashboard; pack installs record none). Nothing a non-privileged user can
  open is ever in this list.
* ``shared`` (report only) — copies whose team is gone but which an admin
  re-shared with a live team / org, set to ``show_all``, or that a creator
  still owns.
* ``invisible_manual`` (report only) — hand-built ``show_teams`` dashboards
  shared with no live team or org: private drafts only their creator (and
  privileged users) can see. A person made these; a person decides.
* ``same_title_manual`` (report only) — hand-built dashboards sharing a title
  with another dashboard in the same workspace.

Dry-run by default — the dry run IS the census; read it on prod before any
``--apply``. ``--apply`` deletes the ``delete`` list only, through the same
path as ``DELETE /api/dashboards/<id>`` (``delete_document_by_id`` then
``ReferenceCleanupService.cleanup_references``) and logs every id.

Fail-closed: the teams, orgs and dashboards reads are each checked against
``es.count``; a short or failed read aborts before planning (an empty teams
read would otherwise make every template copy look orphaned).

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.cleanup_orphan_template_dashboards [--apply]
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import delete_document_by_id, get_all_documents_paginated, get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"
TEAMS_INDEX = "de_asset_teams"
ORGS_INDEX = "de_asset_organization"


class CleanupAbort(RuntimeError):
    """A read could not be verified complete; nothing was planned or changed."""


@dataclass
class Row:
    dashboard_id: str
    title: str
    workspace_esid: str
    reason: str


@dataclass
class Plan:
    delete: list[Row] = field(default_factory=list)
    shared: list[Row] = field(default_factory=list)
    invisible_manual: list[Row] = field(default_factory=list)
    same_title_manual: list[Row] = field(default_factory=list)


def load_all(es: Any, index: str) -> dict[str, dict[str, Any]]:  # Any: the ES client is untyped
    """Every doc in ``index`` as ``{_id: _source}``, verified against
    ``es.count``. Raises CleanupAbort on any error or a short read."""
    try:
        expected = int(es.count(index=index)["count"])
    except Exception as e:  # noqa: BLE001 — any failure aborts the run
        raise CleanupAbort(f"could not count {index}: {e}") from e
    hits = get_all_documents_paginated(index, {"match_all": {}}, es=es)
    if len(hits) != expected:
        raise CleanupAbort(f"read {len(hits)} of {expected} docs from {index}; refusing to plan on a partial read")
    return {h["_id"]: h.get("_source") or {} for h in hits}


def _row(dashboard_id: str, doc: dict[str, Any], reason: str) -> Row:
    return Row(dashboard_id, doc.get("title") or "", doc.get("workspace_esid") or "", reason)


def build_plan(dashboards: dict[str, dict[str, Any]], team_ids: set[str], org_ids: set[str]) -> Plan:
    """Sort the dashboards into the four lists (see the module docstring)."""
    plan = Plan()
    manual_by_title: dict[tuple[str, str], list[str]] = defaultdict(list)
    all_by_title: dict[tuple[str, str], int] = defaultdict(int)

    for dashboard_id, doc in dashboards.items():
        key = (doc.get("workspace_esid") or "", (doc.get("title") or "").strip().lower())
        all_by_title[key] += 1
        live_teams = [t for t in doc.get("allowed_team_esids") or [] if t in team_ids]
        live_orgs = [o for o in doc.get("allowed_org_esids") or [] if o in org_ids]
        mode = doc.get("visibility_mode")
        creator = doc.get("created_by") or ""
        unshared = mode == "show_teams" and not live_teams and not live_orgs

        if doc.get("from_template") is True:
            owner = doc.get("template_team_esid")
            if not owner or owner in team_ids:
                continue  # a live team's copy, or a teamless install — leave it
            if unshared and not creator:
                plan.delete.append(_row(dashboard_id, doc, f"team {owner} deleted; no live team, org or creator can see it"))
            else:
                reason = f"team {owner} deleted but still visible (mode={mode}, creator={creator or '-'})"
                plan.shared.append(_row(dashboard_id, doc, reason))
            continue

        manual_by_title[key].append(dashboard_id)
        if unshared:
            reason = f"hand-built; shared with no live team or org; creator={creator or '-'}"
            plan.invisible_manual.append(_row(dashboard_id, doc, reason))

    for key, ids in manual_by_title.items():
        if all_by_title[key] > 1:
            for dashboard_id in ids:
                plan.same_title_manual.append(_row(dashboard_id, dashboards[dashboard_id], "hand-built; title shared"))
    return plan


def apply_plan(plan: Plan) -> tuple[int, list[str]]:
    """Delete the ``delete`` list; returns (deleted count, ids that failed)."""
    from asset_management.web.services.reference_cleanup_service import ReferenceCleanupService

    deleted, failed = 0, []
    for row in plan.delete:
        if not delete_document_by_id(DASHBOARDS_INDEX, row.dashboard_id):
            failed.append(row.dashboard_id)
            continue
        ReferenceCleanupService.cleanup_references(DASHBOARDS_INDEX, row.dashboard_id)
        logger.info("deleted dashboard %s (%r): %s", row.dashboard_id, row.title, row.reason)
        deleted += 1
    return deleted, failed


def _print_section(name: str, rows: list[Row]) -> None:
    logger.info("== %s: %d ==", name, len(rows))
    for row in sorted(rows, key=lambda r: (r.workspace_esid, r.title.lower(), r.dashboard_id)):
        logger.info("  %s  ws=%s  %r  — %s", row.dashboard_id, row.workspace_esid or "-", row.title, row.reason)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="delete the 'delete' list (default: dry run)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ABORT: Elasticsearch unavailable")
        return 2
    try:
        teams = load_all(es, TEAMS_INDEX)
        orgs = load_all(es, ORGS_INDEX)
        dashboards = load_all(es, DASHBOARDS_INDEX)
    except CleanupAbort as e:
        logger.error("ABORT: %s", e)
        return 2
    if not teams:
        logger.error("ABORT: %s is empty; refusing to treat every template copy as orphaned", TEAMS_INDEX)
        return 2

    plan = build_plan(dashboards, set(teams), set(orgs))
    mode = "APPLY" if args.apply else "DRY RUN"
    logger.info("dashboards=%d teams=%d orgs=%d mode=%s", len(dashboards), len(teams), len(orgs), mode)
    _print_section("delete (template copy, team gone, no team / org / creator can see it)", plan.delete)
    _print_section("report only: team gone but still shared", plan.shared)
    _print_section("report only: hand-built, shared with no live team or org", plan.invisible_manual)
    _print_section("report only: hand-built, title shared in its workspace", plan.same_title_manual)

    if not args.apply:
        logger.info("Dry run — nothing changed. Re-run with --apply to delete the first list.")
        return 0
    deleted, failed = apply_plan(plan)
    logger.info("Deleted %d dashboard(s).", deleted)
    if failed:
        logger.error("FAILED to delete %d: %s", len(failed), ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
