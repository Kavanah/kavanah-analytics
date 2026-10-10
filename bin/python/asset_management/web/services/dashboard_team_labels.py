"""Team labels for same-titled dashboards.

Starter-pack templates install one copy per team, all with the template's
name (``TemplateService.instantiate`` is idempotent per team). A privileged
user — superuser, asset_admin, workspace admin / owner — sees every team's
copy in the selected workspace, and a user in several teams sees one per
team, so a sidebar showed a stack of identical titles with nothing to tell
them apart.

:func:`apply_duplicate_team_labels` adds ``team_label`` (the owning team's
title) to a list row only when that title appears more than once in the same
workspace in the list this viewer is shown. A single-team user sees one copy
and gets no label — their view is unchanged. Stored titles are never renamed:
renaming would change what each team sees.

Tenant safety: a label names a team. A non-privileged viewer is only ever
shown the title of a team in their own scope (``viewer_team_esids``); a
privileged viewer (``viewer_team_esids is None``) already sees every team's
dashboards in the workspace, so any owning team may be named.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from lib.es_helpers import get_elasticsearch_client

TEAMS_INDEX = "de_asset_teams"


def owner_team_esid(source: dict[str, Any]) -> str | None:
    """The team a dashboard belongs to, when there is exactly one: a template
    copy's ``template_team_esid``, else the sole team of a ``show_teams``
    dashboard shared with one team and no orgs. None otherwise."""
    if source.get("from_template") is True and source.get("template_team_esid"):
        return str(source["template_team_esid"])
    allowed_teams = source.get("allowed_team_esids") or []
    if source.get("visibility_mode") == "show_teams" and len(allowed_teams) == 1 and not source.get("allowed_org_esids"):
        return str(allowed_teams[0])
    return None


def _team_titles(team_esids: set[str]) -> dict[str, str]:
    """``{team_esid: team_title}`` for the given teams. A lookup failure only
    costs the labels (the list itself is unaffected), so it is logged and
    yields no titles rather than failing the page."""
    es = get_elasticsearch_client()
    if es is None or not team_esids:
        return {}
    try:
        docs = es.mget(index=TEAMS_INDEX, body={"ids": sorted(team_esids)})["docs"]
    except Exception as e:  # noqa: BLE001 — cosmetic enrichment; the list renders without labels
        logging.warning("team label lookup failed for %d team(s): %s", len(team_esids), e)
        return {}
    return {
        d["_id"]: d["_source"]["team_title"] for d in docs if d.get("found") and (d.get("_source") or {}).get("team_title")
    }


def apply_duplicate_team_labels(
    rows: list[dict[str, Any]],
    meta_by_id: dict[str, tuple[str, str | None]],
    viewer_team_esids: set[str] | None,
) -> None:
    """Set ``row["team_label"]`` on rows whose title repeats in its workspace.

    ``meta_by_id`` maps a row's ``id`` to ``(workspace_esid, owner_team_esid)``
    (see :func:`owner_team_esid`). ``viewer_team_esids`` is the viewer's team
    scope, or None for a privileged viewer. Rows are changed in place.
    """
    groups: dict[tuple[str, str], list[tuple[dict[str, Any], str | None]]] = defaultdict(list)
    for row in rows:
        workspace_esid, owner = meta_by_id.get(row.get("id"), ("", None))
        title_key = (row.get("title") or "").strip().lower()
        groups[(workspace_esid or "", title_key)].append((row, owner))

    nameable = {
        owner
        for group in groups.values()
        if len(group) > 1
        for _row, owner in group
        if owner and (viewer_team_esids is None or owner in viewer_team_esids)
    }
    if not nameable:
        return
    titles = _team_titles(nameable)
    for group in groups.values():
        if len(group) < 2:
            continue
        for row, owner in group:
            if owner in nameable and owner in titles:
                row["team_label"] = titles[owner]
