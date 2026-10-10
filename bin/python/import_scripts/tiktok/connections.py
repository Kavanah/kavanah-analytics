"""resolve a team-scoped TikTok run to the accounts it must fetch.

A team names its TikTok connections in ``team_tiktok_connection_esids`` (one
TikTok account per connection). Several teams may name the same connection, so
targets are grouped by connection: each account is refreshed and fetched once
per run and its snapshot written to every team that names it.

Problems are returned, not raised: one team's broken link must not stop another
team's import. The caller records each problem so it reaches the Report
Scheduler's Errors button, and fails the run when nothing was importable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from asset_management.web.services.connection_service import (
    PROVIDER_TIKTOK,
    STATUS_ACTIVE,
    ConnectionService,
)
from import_scripts.facebook.lib.team_helpers import _resolve_team_country
from lib.es_helpers import get_all_documents_paginated
from lib.tiktok_constants import TEAM_CONNECTIONS_FIELD, TEAM_FLAG_FIELD


@dataclass
class TikTokTarget:
    """One TikTok account to fetch, and the teams its snapshot goes to."""

    connection_esid: str
    open_id: str
    label: str
    scopes: list[str]
    teams: list[dict] = field(default_factory=list)
    # For the credential-health row (the banner is workspace-scoped): the
    # connection's principal and the workspace of the first team it serves.
    principal: str = ""
    workspace_esid: str = ""


@dataclass
class TargetProblem:
    """A team (or connection) that could not be imported, in words the
    import-error classifier routes to the right user advice."""

    message: str
    team_esid: str = ""
    detail: str = ""


def team_metadata(team_esid: str, team: dict) -> dict:
    """The team fields every TikTok doc carries (YouTube's set)."""
    return {
        "team_esid": team_esid,
        "team_title": team.get("team_title", "Unknown"),
        "team_url": team.get("team_url", "unknown"),
        "team_country": _resolve_team_country(team) or "unknown",
        "team_city": team.get("team_city", "unknown"),
        "organization_title": team.get("organization_title", "Unknown"),
    }


def _flag_on(value) -> bool:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip().lower() == "true"


def resolve_targets(team_esids: set[str]) -> tuple[list[TikTokTarget], list[TargetProblem]]:
    """Map in-scope teams to TikTok targets.

    ES failures propagate (an outage must not read as "nothing to
    import"). Everything else becomes a :class:`TargetProblem`.
    """
    problems: list[TargetProblem] = []
    teams_by_connection: dict[str, list[dict]] = {}
    workspace_by_team: dict[str, str] = {}

    team_docs = get_all_documents_paginated("de_asset_teams", {"ids": {"values": sorted(team_esids)}})
    for doc in team_docs:
        team_esid = doc["_id"]
        src = doc.get("_source") or {}
        title = src.get("team_title", team_esid)
        if not _flag_on(src.get(TEAM_FLAG_FIELD)):
            problems.append(
                TargetProblem(f"TikTok is not enabled for team {title} — resolved to zero teams", team_esid, title)
            )
            continue
        connection_esids = [str(c).strip() for c in (src.get(TEAM_CONNECTIONS_FIELD) or []) if str(c).strip()]
        if not connection_esids:
            problems.append(
                TargetProblem(f"TikTok account not linked to a team: {title} has no TikTok account", team_esid, title)
            )
            continue
        workspace_by_team[team_esid] = str(src.get("workspace_esid") or "")
        for connection_esid in dict.fromkeys(connection_esids):
            teams_by_connection.setdefault(connection_esid, []).append(team_metadata(team_esid, src))

    targets: list[TikTokTarget] = []
    for connection_esid, teams in teams_by_connection.items():
        team_titles = ", ".join(t["team_title"] for t in teams)
        conn = ConnectionService.get_connection(connection_esid)
        if not conn or conn.get("provider") != PROVIDER_TIKTOK:
            # Missing, or the esid names another provider's connection — never
            # read a foreign token on TikTok's behalf.
            logging.error(
                "tiktok: connection %s linked from %s is missing or not a TikTok connection", connection_esid, team_titles
            )
            problems.append(
                TargetProblem(
                    f"TikTok account not linked to a TikTok connection (teams: {team_titles})",
                    teams[0]["team_esid"] if len(teams) == 1 else "",
                    connection_esid,
                )
            )
            continue
        label = conn.get("label") or connection_esid
        if (conn.get("status") or "") != STATUS_ACTIVE:
            problems.append(
                TargetProblem(
                    f"TikTok connection {label} is disconnected or expired (status={conn.get('status')}) — reconnect it",
                    teams[0]["team_esid"] if len(teams) == 1 else "",
                    label,
                )
            )
            continue
        if not conn.get("oauth_token_enc"):
            problems.append(
                TargetProblem(
                    f"no oauth token on TikTok connection {label} — authorize it again",
                    teams[0]["team_esid"] if len(teams) == 1 else "",
                    label,
                )
            )
            continue
        targets.append(
            TikTokTarget(
                connection_esid=connection_esid,
                open_id=str(conn.get("provider_account_id") or ""),
                label=label,
                scopes=list(conn.get("scopes") or []),
                teams=teams,
                principal=str(conn.get("principal") or ""),
                workspace_esid=next(
                    (workspace_by_team[t["team_esid"]] for t in teams if workspace_by_team.get(t["team_esid"])), ""
                ),
            )
        )
    return targets, problems
