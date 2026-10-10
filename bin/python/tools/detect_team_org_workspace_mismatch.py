"""Read-only detector: teams whose ``workspace_esid`` disagrees with their orgs'
workspace.

Makes NO writes — only ``es.search``. Safe on staging or prod.

The smoke test surfaced team ``FnTi254B`` (title ``test``) sitting in
``kavanah-workspace`` while its orgs (``m_jAmJ4B``, ``iDh0T50B``) live in
``kavanah-media`` — the team's own workspace field and its org-graph parent
point at different workspaces, so the team is reachable from two workspaces and
"foreign" teams leak into Asset Explorer when a user grants into the wrong one.

This tool finds the WHOLE class of such teams so we know how big it is before
anyone decides the heal direction (repoint the team's workspace_esid vs. repoint
its org refs). It is the read-only foundation for an eventual heal tool — same
detection, add ``--apply`` once the owning-workspace decision is made and a teammate
has explained the restored-orgs question.

A team is FLAGGED when it has a non-empty ``workspace_esid`` AND at least one
live org, AND the team's own ``workspace_esid`` is NOT among the workspaces its
live orgs resolve to. Teams with no orgs, no workspace_esid, or only dead org
refs are reported separately (those are the plain orphan, not a
mismatch).

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.detect_team_org_workspace_mismatch
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

from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

TEAMS_INDEX = "de_asset_teams"
ORGANIZATION_INDEX = "de_asset_organization"


def _normalise_org_refs(raw) -> list[str]:
    if isinstance(raw, str) and raw:
        return [raw]
    if isinstance(raw, list):
        return [v for v in raw if v]
    return []


def _org_workspace_map(es, org_esids: set[str]) -> dict[str, str | None]:
    """Resolve each org esid -> its workspace_esid (None if the org is missing).

    One ``ids`` query (chunked) over de_asset_organization. A missing org id
    simply isn't in the response, so it maps to None (a dead ref).
    """
    out: dict[str, str | None] = {e: None for e in org_esids}
    ids = list(org_esids)
    for i in range(0, len(ids), 1000):
        chunk = ids[i : i + 1000]
        resp = es.search(
            index=ORGANIZATION_INDEX,
            body={"query": {"ids": {"values": chunk}}, "_source": ["workspace_esid"], "size": len(chunk)},
        )
        for hit in resp.get("hits", {}).get("hits", []):
            out[hit["_id"]] = (hit.get("_source") or {}).get("workspace_esid")
    return out


def detect(es) -> dict:
    """Scan every team and classify it. Returns a summary dict and logs each
    flagged team. Read-only."""
    summary = {"teams_scanned": 0, "mismatched": 0, "orphaned": 0, "ok": 0, "no_link": 0}

    # First pass: collect every team's (id, workspace_esid, org refs), and the
    # union of all org esids referenced, so we resolve orgs in bulk.
    #
    # NB: do NOT sort/paginate by ``_id`` — this cluster has
    # ``indices.id_field_data.enabled`` off, so a sort on ``_id`` 400s. The
    # team count is small (tens), so a single large ``size`` scan is correct
    # and avoids needing a sort key at all.
    teams: list[tuple[str, str | None, list[str], str | None]] = []
    all_org_esids: set[str] = set()
    resp = es.search(
        index=TEAMS_INDEX,
        body={
            "size": 10000,
            "query": {"match_all": {}},
            "_source": ["team_title", "workspace_esid", "organization_esid"],
        },
    )
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        refs = _normalise_org_refs(src.get("organization_esid"))
        teams.append((hit["_id"], src.get("workspace_esid"), refs, src.get("team_title")))
        all_org_esids.update(refs)

    org_ws = _org_workspace_map(es, all_org_esids) if all_org_esids else {}

    for team_esid, ws_esid, refs, title in teams:
        summary["teams_scanned"] += 1
        if not refs or not ws_esid:
            summary["no_link"] += 1
            continue
        live_ws = {org_ws.get(o) for o in refs if org_ws.get(o)}
        if not live_ws:
            # All org refs dead — the plain orphan, not a mismatch.
            summary["orphaned"] += 1
            continue
        if ws_esid in live_ws:
            summary["ok"] += 1
            continue
        # Mismatch: the team's own workspace is not among its orgs' workspaces.
        summary["mismatched"] += 1
        logger.info(
            "MISMATCH team %s (%r): workspace_esid=%r but orgs resolve to %s",
            team_esid,
            title,
            ws_esid,
            sorted(w for w in live_ws if w),
        )

    return summary


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    logger.info("===  team↔org workspace-mismatch detector (read-only) ===\n")
    summary = detect(es)
    logger.info("")
    logger.info("Summary:")
    logger.info("  teams scanned          %d", summary["teams_scanned"])
    logger.info("  MISMATCHED (ws != orgs') %d  <- the FnTi254B class", summary["mismatched"])
    logger.info("  orphaned (all orgs dead) %d  <- plain  orphan", summary["orphaned"])
    logger.info("  ok (ws among orgs')     %d", summary["ok"])
    logger.info("  no link (no org/ws)     %d", summary["no_link"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
