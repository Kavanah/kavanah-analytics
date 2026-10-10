"""One-shot backfill: stamp ``workspace_esid`` on existing team docs.

The wizard's onboarding-service team-create path didn't include
``workspace_esid`` in the new team doc was fixed. As a
result, every wizard-created team in production is missing the field.
The symptom: ``cascade_purge_empty_workspace`` enumerates child teams
by ``term:workspace_esid`` and finds zero matches → wizard-created
teams aren't purged when their workspace is deleted → orphan teams
remain in the cluster and show up in dropdowns that pull from
``de_asset_teams``.

The backfill joins each team → its first org_esid → the org's
``workspace_esid`` (which backfill already stamped on every
org). Idempotent: a team that already has ``workspace_esid`` is
skipped. Best-effort: a team whose org join fails (org missing or org
itself lacks ``workspace_esid``) is reported and skipped, not aborted.

**Dry-run by default.** Use ``--apply`` to write.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_team_workspace_esid

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_team_workspace_esid --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from asset_management.web.services.permissions_index_service import (  # noqa: E402
    ORGANIZATION_INDEX,
)
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

TEAMS_INDEX = "de_asset_teams"


def _fetch_all_teams(es) -> list[dict]:
    try:
        resp = es.search(
            index=TEAMS_INDEX,
            body={"query": {"match_all": {}}, "size": 10000},
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.error("Could not search %s: %s", TEAMS_INDEX, e)
        return []
    return resp.get("hits", {}).get("hits", []) or []


def _fetch_org_workspace_map(es) -> dict[str, str]:
    """Return ``{org_esid: workspace_esid}`` for every org with the field set.

    Orgs missing ``workspace_esid`` are silently absent from the map —
    the team-backfill loop reports the miss when it tries to join.
    """
    try:
        resp = es.search(
            index=ORGANIZATION_INDEX,
            body={"query": {"match_all": {}}, "size": 10000},
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.error("Could not search %s: %s", ORGANIZATION_INDEX, e)
        return {}
    out: dict[str, str] = {}
    for hit in resp.get("hits", {}).get("hits", []) or []:
        src = hit.get("_source") or {}
        ws = src.get("workspace_esid")
        if ws:
            out[hit["_id"]] = ws
    return out


def _resolve_team_workspace(team_doc: dict, org_to_ws: dict[str, str]) -> str | None:
    """Resolve a team's intended workspace_esid via its first org.

    ``organization_esid`` is the join key. Wizard-created teams always
    have exactly one entry in this list; the field is mapped as a list
    for the multi-org edge case (a single team referenced by multiple
    orgs — rare but supported by the schema).
    """
    org_field = team_doc.get("organization_esid")
    if isinstance(org_field, list):
        first_org = org_field[0] if org_field else None
    else:
        first_org = org_field
    if not first_org:
        return None
    return org_to_ws.get(first_org)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the workspace_esid stamp to ES. Default is dry-run.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    teams = _fetch_all_teams(es)
    if not teams:
        logger.info("No team docs found in %s — nothing to do.", TEAMS_INDEX)
        return 0

    org_to_ws = _fetch_org_workspace_map(es)
    if not org_to_ws:
        logger.error(
            "No orgs with workspace_esid found in %s. Run " "tools.backfill_org_workspace_esid first.",
            ORGANIZATION_INDEX,
        )
        return 2

    planned = 0
    skipped_already_set = 0
    skipped_no_org_link = 0
    applied = 0

    for hit in teams:
        team_esid = hit["_id"]
        src = hit.get("_source") or {}
        title = src.get("team_title", "(no title)")
        if src.get("workspace_esid"):
            skipped_already_set += 1
            continue
        ws = _resolve_team_workspace(src, org_to_ws)
        if not ws:
            logger.warning(
                "SKIP team=%r title=%r — no org link to a workspace_esid",
                team_esid,
                title,
            )
            skipped_no_org_link += 1
            continue
        planned += 1
        logger.info(
            "%s team=%r title=%r workspace_esid=%r",
            "APPLY" if args.apply else "WOULD WRITE",
            team_esid,
            title,
            ws,
        )
        if args.apply:
            try:
                es.update(
                    index=TEAMS_INDEX,
                    id=team_esid,
                    body={"doc": {"workspace_esid": ws}},
                    refresh="wait_for",
                )
                applied += 1
            except Exception as e:  # noqa: BLE001 — top-level boundary
                logger.error("Update failed for team %r: %s", team_esid, e)

    logger.info("")
    logger.info("Backfill summary:")
    logger.info("  Total teams:          %d", len(teams))
    logger.info("  Already had field:    %d", skipped_already_set)
    logger.info("  Missing org link:     %d", skipped_no_org_link)
    logger.info("  Planned:              %d", planned)
    logger.info("  Applied:              %d", applied)
    if not args.apply:
        logger.info("")
        logger.info("(dry-run — re-run with --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
