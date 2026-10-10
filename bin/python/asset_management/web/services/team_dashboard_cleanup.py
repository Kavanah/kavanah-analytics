"""Delete a deleted team's starter-pack dashboards.

Starter-pack templates install one dashboard per team (``from_template``,
``template_team_esid`` = the team, ``show_teams`` with only that team). Before
this, deleting the team left those copies behind; ``ReferenceCleanupService``
then stripped the team from their ``allowed_team_esids``, so they were visible
to nobody but privileged users, who saw them as unexplained duplicates
.

The cleanup is split in two because the delete itself strips the team id from
every dashboard:

* :func:`plan_team_dashboard_cleanup` — read-only, run BEFORE the team is
  deleted, while the dashboards still name it;
* :func:`apply_team_dashboard_cleanup` — run only AFTER the delete succeeded,
  so a failed team delete never costs the team its dashboards.

The rule matches tool: a template copy of this team is deleted only
when nobody else can still see it — ``show_teams``, no other team or org
allowed, and no ``created_by`` (the Builder read gate always lets a creator
see their own dashboard). Everything else is kept. Hand-built dashboards are
never deleted; one that was shared only with this team is logged, because it
is now visible only to its creator and admins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lib.es_helpers import delete_document_by_id, get_all_documents_paginated

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"


@dataclass
class TeamDashboardPlan:
    team_esid: str
    delete: list[tuple[str, str]] = field(default_factory=list)  # (dashboard id, title)
    kept_shared: list[tuple[str, str]] = field(default_factory=list)
    manual: list[tuple[str, str]] = field(default_factory=list)


def _dual_term(field_name: str, value: str) -> dict[str, Any]:
    # Mapping drift: the field is keyword on one cluster,
    # text + .keyword on another. Match either shape.
    return {"bool": {"should": [{"term": {field_name: value}}, {"term": {f"{field_name}.keyword": value}}]}}


def plan_team_dashboard_cleanup(es: Any, team_esid: str) -> TeamDashboardPlan:  # Any: the ES client is untyped
    """Read-only: what deleting ``team_esid`` should do to dashboards. Call it
    before the team is deleted. A failed read yields an empty plan, so nothing
    is deleted (the tool remains the backstop)."""
    plan = TeamDashboardPlan(team_esid=team_esid)
    query = {
        "bool": {
            "should": [_dual_term("template_team_esid", team_esid), _dual_term("allowed_team_esids", team_esid)],
            "minimum_should_match": 1,
        }
    }
    for hit in get_all_documents_paginated(DASHBOARDS_INDEX, query, es=es):
        src = hit.get("_source") or {}
        dashboard_id = src.get("dashboard_id") or hit["_id"]
        title = src.get("title") or ""
        other_teams = [t for t in src.get("allowed_team_esids") or [] if t != team_esid]
        unshared = src.get("visibility_mode") == "show_teams" and not other_teams and not src.get("allowed_org_esids")

        if src.get("from_template") is True and src.get("template_team_esid") == team_esid:
            if unshared and not src.get("created_by"):
                plan.delete.append((dashboard_id, title))
            else:
                plan.kept_shared.append((dashboard_id, title))
        elif src.get("from_template") is not True and unshared and team_esid in (src.get("allowed_team_esids") or []):
            plan.manual.append((dashboard_id, title))
    return plan


def apply_team_dashboard_cleanup(plan: TeamDashboardPlan) -> dict[str, int]:
    """Carry out ``plan`` after the team delete succeeded. Deletes through the
    same path as ``DELETE /api/dashboards/<id>``; never raises."""
    from asset_management.web.services.reference_cleanup_service import ReferenceCleanupService

    deleted = failed = 0
    for dashboard_id, title in plan.delete:
        try:
            if not delete_document_by_id(DASHBOARDS_INDEX, dashboard_id):
                failed += 1
                continue
            ReferenceCleanupService.cleanup_references(DASHBOARDS_INDEX, dashboard_id)
            deleted += 1
            logging.info(
                "deleted template dashboard %s (%r) of deleted team %s", dashboard_id, title, plan.team_esid
            )
        except Exception as e:  # noqa: BLE001 — cascade bookkeeping must never fail the completed team delete
            failed += 1
            logging.warning("could not delete dashboard %s of team %s: %s", dashboard_id, plan.team_esid, e)
    for dashboard_id, title in plan.manual:
        logging.warning(
            "hand-built dashboard %s (%r) was shared only with deleted team %s; "
            "it is now visible only to its creator and admins",
            dashboard_id,
            title,
            plan.team_esid,
        )
    return {"deleted": deleted, "failed": failed, "kept_shared": len(plan.kept_shared), "manual": len(plan.manual)}
