"""One-shot heal: scrub dead ``organization_esid`` refs from team docs.

A ``de_asset_teams`` doc holds ``organization_esid`` (a list,
occasionally a legacy scalar string) linking it to its parent org(s). When an
org is deleted by a path that did not cascade the reference cleanup
(``ReferenceCleanupService``) — or before that cleanup covered orgs — the dead
ESID lingers on the team. A team whose org refs ALL dead-end breaks the
team→org→workspace graph walk (``_workspace_esids_from_scope_esids``), so the
team's workspace disappears from the graph-walking surfaces (Asset Explorer,
Data Inventory). That is the ``kavanah-workspace`` invisible
because its only team (``FnTi254B...``) points at two orgs that no longer exist.

This tool finds every team referencing a non-existent org and removes ONLY the
dead ESIDs, leaving live refs intact. It is the operator-run companion to the
startup ``OrphanSweepService.sweep_orphan_team_org_refs`` defense layer — same
heal, but with per-team reporting so the operator can see exactly what changes
before/after applying, and so the live repro can be inspected first.

A team whose only org was deleted ends up with an empty ``organization_esid``;
it is still graph-orphaned via the org leg, but the heal-on-read
direct ``workspace_esid`` leg surfaces its workspace, and the stale ref no
longer pollutes any org-keyed walk.

Idempotent: a team with no dead refs is skipped; a second --apply run reports
healed=0. Dry-run by default; --apply writes. Read-only until --apply.

Safety: refuses to run if ``de_asset_organization`` is empty or missing — an
empty org index would make EVERY team's refs look dead and scrub them all.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_orphan_team_org_refs

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_orphan_team_org_refs --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Same three-candidate resolver as the other tools.* scripts — a tools/* module
# that imports asset_management.* / lib.* and runs inside imports_container
# needs this, not parents[2]/bin/python (see CLAUDE memory).
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

TEAMS_INDEX = "de_asset_teams"
ORGANIZATION_INDEX = "de_asset_organization"
ORG_FIELD = "organization_esid"


def _live_org_esids(es) -> set[str] | None:
    """Return the set of live org doc ids, or ``None`` to abort the heal.

    ``None`` means "do not heal" — ES unreachable or the org index
    missing/empty. An empty org index would make every team's refs look dead.
    """
    try:
        resp = es.search(
            index=ORGANIZATION_INDEX,
            body={"query": {"match_all": {}}, "_source": False, "size": 10000},
        )
    except Exception as e:  # noqa: BLE001 — boundary; abort rather than guess
        err = str(e).lower()
        if "index_not_found" in err or "404" in err:
            logger.error("%s is missing — refusing to heal (cannot tell which refs are dead).", ORGANIZATION_INDEX)
        else:
            logger.error("could not enumerate live orgs — refusing to heal: %s", e)
        return None
    live = {h["_id"] for h in resp.get("hits", {}).get("hits", [])}
    if not live:
        logger.error("%s is empty — refusing to heal (an empty org index would scrub every team).", ORGANIZATION_INDEX)
        return None
    return live


def _normalise_org_refs(raw) -> list[str]:
    """Return the team's org refs as a clean list (handles list or scalar)."""
    if isinstance(raw, str) and raw:
        return [raw]
    if isinstance(raw, list):
        return [v for v in raw if v]
    return []


def heal(es, live_orgs: set[str], *, apply: bool) -> dict[str, int]:
    """Scrub dead org refs from every team. Returns summary counts.

    Reports one line per healed team (esid, title, dead refs removed, refs
    kept). Returns ``{"teams_scanned", "teams_healed", "dead_refs_removed"}``.
    """
    summary = {"teams_scanned": 0, "teams_healed": 0, "dead_refs_removed": 0}

    # Scan every team that has the field at all.
    # NB: do NOT sort/paginate by ``_id`` — this cluster has
    # ``indices.id_field_data.enabled`` off, so a sort on ``_id`` 400s (hit on
    # staging 2026-06-24). The team count is small (tens), so a single large
    # ``size`` scan is correct and needs no sort key.
    resp = es.search(
        index=TEAMS_INDEX,
        body={
            "size": 10000,
            "query": {"bool": {"must": [{"exists": {"field": ORG_FIELD}}]}},
            "_source": [ORG_FIELD, "team_title", "workspace_esid"],
        },
    )
    for hit in resp.get("hits", {}).get("hits", []):
        summary["teams_scanned"] += 1
        src = hit.get("_source", {})
        refs = _normalise_org_refs(src.get(ORG_FIELD))
        if not refs:
            continue
        kept = [r for r in refs if r in live_orgs]
        dead = [r for r in refs if r not in live_orgs]
        if not dead:
            continue

        team_esid = hit.get("_id")
        summary["teams_healed"] += 1
        summary["dead_refs_removed"] += len(dead)
        logger.info(
            "%s team %s (%r, ws=%s): remove dead org ref(s) %s; keep %s",
            "HEAL" if apply else "WOULD HEAL",
            team_esid,
            src.get("team_title"),
            src.get("workspace_esid"),
            dead,
            kept,
        )
        if apply:
            # Write the cleaned list back (empty list when every ref was
            # dead — the heal-on-read direct workspace_esid leg keeps the
            # workspace reachable). doc update, not delete: the team stays.
            es.update(
                index=TEAMS_INDEX,
                id=team_esid,
                body={"doc": {ORG_FIELD: kept}},
                refresh=True,
            )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the scrubbed org refs. Default is dry-run (report only).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    live_orgs = _live_org_esids(es)
    if live_orgs is None:
        return 2

    if args.apply:
        logger.warning("APPLY mode: scrubbing dead org refs from team docs.\n")

    summary = heal(es, live_orgs, apply=args.apply)

    logger.info("")
    logger.info("Heal summary:")
    logger.info("  teams scanned       %d", summary["teams_scanned"])
    logger.info("  teams %s %d", "healed " if args.apply else "to heal", summary["teams_healed"])
    logger.info("  dead refs removed   %d", summary["dead_refs_removed"])
    if not args.apply:
        logger.info("")
        logger.info("(dry-run — re-run with --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
