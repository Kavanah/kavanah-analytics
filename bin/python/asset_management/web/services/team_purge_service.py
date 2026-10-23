"""Purge requests for a deleted team's data — the tombstone half.

Deleting a team (or the empty workspace that holds it) used to leave the
team's per-person data behind for ever: the ``{team_esid}__*`` data indices,
the legacy ``{sanitized_team_title}_{source}`` indices and the raw import
files under ``/app/data``. Nothing ever removed them.

The data is irreplaceable, so a delete never removes it straight away. Every
path that deletes a ``de_asset_teams`` doc writes a *purge request* (a
tombstone) here instead::

    de_team_purge_requests/_doc/team:<team_esid>
        team_esid, index_prefix, legacy_slug, team_title, workspace_esid,
        reason, requested_by, requested_at, purge_after (= now + 7 days),
        status = pending

``asset_management/jobs/team_data_purge.py`` acts on a request only after
``purge_after`` and only if the team doc is STILL absent (it may have been
restored with the same esid). Until then a mistaken delete is recoverable:
restore the team doc and the next sweep cancels the request.

Writing a request is best-effort and never raises: the team is already gone
when it runs, and failing the completed delete would not bring it back. A
missed request is caught later by the sweep's orphan report (indices whose
prefix names no live team).

The delete paths that call :func:`record_team_purge_tombstones`:

* ``POST /asset/teams/delete/<id>``           (app.py ``delete_asset``)
* ``POST /asset/teams/bulk-delete``           (app.py ``bulk_delete_assets``)
* ``POST /api/user-management/workspaces/<id>/delete``
  (``permissions_index_service.cascade_purge_empty_workspace``)
* the startup orphan sweep's ``de_asset_teams`` leg
  (``OrphanSweepService.sweep_workspace_tier``)
* the asset-management CLI team delete (``handlers/teams.py``)

Organizations cannot cascade to teams (refuses an org that still
has teams), and the workspace asset route refuses deletes outright.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from lib.es_helpers import create_index_if_not_exists, sanitize_team_title

TOMBSTONE_INDEX = "de_team_purge_requests"

# a teammate: a mistaken delete must be recoverable, so data waits a week.
GRACE_DAYS = 7

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

KIND_TEAM = "team"
# A data index whose prefix names no live team and has no request — written by
# the sweep's ``--apply-orphans``, never by a delete path.
KIND_ORPHAN = "orphan_index"

# Explicit keyword mapping: a bulk write into a missing index would dynamic-map
# team_esid as text +.keyword (the drift), and the sweep filters on
# these fields with bare ``term`` queries. ``dynamic: false`` keeps any future
# field from drifting the mapping; ``result`` is a report blob, not queried.
TOMBSTONE_MAPPING: dict[str, Any] = {
    "dynamic": False,
    "properties": {
        "kind": {"type": "keyword"},
        "team_esid": {"type": "keyword"},
        "index_prefix": {"type": "keyword"},
        "legacy_slug": {"type": "keyword"},
        "team_title": {"type": "keyword"},
        "workspace_esid": {"type": "keyword"},
        "reason": {"type": "keyword"},
        "requested_by": {"type": "keyword"},
        "requested_at": {"type": "date"},
        "purge_after": {"type": "date"},
        "status": {"type": "keyword"},
        "done_at": {"type": "date"},
        "cancelled_reason": {"type": "keyword"},
        "result": {"type": "object", "enabled": False},
    },
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def legacy_slug_for(team_title: str | None) -> str:
    """The legacy ``{slug}_{source}`` index slug for a team title, or ``""``.

    ``sanitize_team_title`` maps an empty or non-Latin title to ``"unknown"``;
    that is shared by every such team, so it never names one team's indices
    and is returned as ``""`` (no legacy purge).
    """
    slug = sanitize_team_title(team_title or "")
    return "" if slug == "unknown" else slug


def tombstone_id(kind: str, key: str) -> str:
    """``team:<esid>`` / ``orphan_index:<prefix>``. Re-deleting a team that was
    restored with the same esid overwrites its earlier request (a new grace)."""
    return f"{kind}:{key}"


def build_team_tombstone(
    *,
    team_esid: str,
    team_title: str | None,
    workspace_esid: str | None,
    requested_by: str,
    reason: str,
    now: datetime,
    grace_days: int = GRACE_DAYS,
) -> dict[str, Any]:
    """The request document for one deleted team (pure)."""
    return {
        "kind": KIND_TEAM,
        "team_esid": team_esid,
        "index_prefix": team_esid.lower(),
        "legacy_slug": legacy_slug_for(team_title),
        "team_title": team_title or "",
        "workspace_esid": workspace_esid or "",
        "reason": reason,
        "requested_by": requested_by or "unknown",
        "requested_at": _iso(now),
        "purge_after": _iso(now + timedelta(days=grace_days)),
        "status": STATUS_PENDING,
    }


def ensure_tombstone_index(es: Any) -> bool:  # Any: the ES client is untyped
    """Create ``de_team_purge_requests`` with its mapping if missing."""
    return bool(create_index_if_not_exists(es, TOMBSTONE_INDEX, TOMBSTONE_MAPPING))


def record_team_purge_tombstones(
    es: Any,  # Any: the ES client is untyped
    teams: Iterable[Mapping[str, Any]],
    *,
    requested_by: str,
    reason: str,
    now: datetime | None = None,
) -> int:
    """Write a pending purge request for each deleted team. Never raises.

    ``teams`` are mappings with ``esid`` (required), ``team_title`` and
    ``workspace_esid``. A team with a blank esid is skipped and logged — a
    blank esid would name no team's data. Returns how many were written.
    Deletes nothing.
    """
    rows = [dict(t) for t in teams]
    if not rows:
        return 0
    if es is None:
        logging.warning("no ES client — purge request(s) NOT recorded for %d team(s)", len(rows))
        return 0
    now = now or datetime.now(timezone.utc)
    written = 0
    try:
        if not ensure_tombstone_index(es):
            logging.warning("could not ensure %s — %d purge request(s) NOT recorded", TOMBSTONE_INDEX, len(rows))
            return 0
    except Exception as exc:  # noqa: BLE001 — the team is already deleted; never fail that on bookkeeping
        logging.warning("could not ensure %s: %s", TOMBSTONE_INDEX, exc)
        return 0
    for team in rows:
        esid = str(team.get("esid") or "").strip()
        if not esid:
            logging.warning("deleted team with a blank esid — no purge request (reason=%s)", reason)
            continue
        doc = build_team_tombstone(
            team_esid=esid,
            team_title=team.get("team_title"),
            workspace_esid=team.get("workspace_esid"),
            requested_by=requested_by,
            reason=reason,
            now=now,
        )
        try:
            es.index(index=TOMBSTONE_INDEX, id=tombstone_id(KIND_TEAM, esid), body=doc, refresh="wait_for")
            written += 1
            logging.info(
                "purge request recorded for team %s (reason=%s, by=%s, purge_after=%s)",
                esid,
                reason,
                doc["requested_by"],
                doc["purge_after"],
            )
        except Exception as exc:  # noqa: BLE001 — best-effort; the orphan report is the backstop
            logging.warning("purge request NOT recorded for team %s: %s", esid, exc)
    return written
