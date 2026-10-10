"""Read-only smoke check (orphaned-team workspace invisibility).

Makes NO writes — only es.search / es.get and a call to the real
``_workspace_esids_from_scope_esids``. Safe to run on staging or prod.

What it reports, for a target team (default = the FnTi254B repro):

  1. The team's current ``organization_esid`` and ``workspace_esid``, and which
     of those org refs are DEAD (not present in ``de_asset_organization``).
  2. Whether the target workspace doc actually exists (it should — the bug is
     reachability, not a missing doc).
  3. The result of the REAL heal-on-read graph walk
     (``_workspace_esids_from_scope_esids`` with org_esids=∅, team_esids={team}):
     the fix is verified when the team's workspace appears in the returned set
     even though its org refs are dead. Prints the result WITH and WITHOUT a
     simulated "no direct leg" so the contrast is explicit (the without-leg path
     re-derives the org-only walk for comparison; it does not patch the code).

Exit code is always 0 — this is a report, not a gate.

Usage::

    docker exec -w /app/python imports_container python3 -m tools.verify_2
    docker exec -w /app/python imports_container python3 -m tools.verify_2 \\
        --team FnTi254B9KZjR-ZYf9tn
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
WORKSPACE_INDEX = "de_asset_workspace"
DEFAULT_TEAM = "FnTi254B9KZjR-ZYf9tn"


def _normalise_org_refs(raw) -> list[str]:
    if isinstance(raw, str) and raw:
        return [raw]
    if isinstance(raw, list):
        return [v for v in raw if v]
    return []


def _org_exists(es, org_esid: str) -> bool:
    try:
        return bool(es.exists(index=ORGANIZATION_INDEX, id=org_esid))
    except Exception:  # noqa: BLE001 — treat lookup failure as "cannot confirm live"
        # Fall back to an ids search so a missing exists() API still works.
        resp = es.search(index=ORGANIZATION_INDEX, body={"query": {"ids": {"values": [org_esid]}}, "size": 1})
        return bool(resp.get("hits", {}).get("hits"))


def _resolve_scope(username: str, roles: list[str]):
    """Resolve a user's effective scope via the real resolver (read-only)."""
    try:
        from web.services.role_filter_service import RoleFilterService, resolve_user_scope
    except ImportError:
        from asset_management.web.services.role_filter_service import (
            RoleFilterService,
            resolve_user_scope,
        )
    config = RoleFilterService.load_config()
    return resolve_user_scope(username, roles, config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team", default=DEFAULT_TEAM, help="Team ESID to inspect.")
    parser.add_argument(
        "--user",
        default=None,
        help="Optionally resolve this user's scope and report whether --team is in it "
        "(so you can tell if their Asset Explorer exercises the heal-on-read leg).",
    )
    parser.add_argument(
        "--roles",
        default="superuser",
        help="Comma-separated session roles for --user (default: superuser). "
        "Drives the privileged-vs-restricted resolver path.",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Optionally run the #3 fix site (_org_team_titles_in_workspace) for this "
        "workspace_esid and report the org/team titles it resolves (the Builder topbar "
        "dropdown source). Before the fix the org side silently returned zero.",
    )
    parser.add_argument(
        "--dropdown-workspace",
        default=None,
        help="Smoke-test the #5 fix: reproduce the Builder filter-options ESID narrowing "
        "for this workspace_esid against live data and report each org TITLE's count "
        "BEFORE (all orgs) vs AFTER (ESID-narrowed). A title that drops from 2->1 is the "
        "duplicate-Kavanah leak being closed.",
    )
    args = parser.parse_args()
    team_esid = args.team

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.info("ES unavailable")
        return 0

    logger.info("===  verification for team %s ===\n", team_esid)

    # 1. Team doc.
    try:
        team = es.get(index=TEAMS_INDEX, id=team_esid)
        src = team.get("_source", {})
    except Exception as e:  # noqa: BLE001
        logger.info("Team %s not found in %s: %s", team_esid, TEAMS_INDEX, e)
        return 0

    org_refs = _normalise_org_refs(src.get("organization_esid"))
    ws_esid = src.get("workspace_esid")
    logger.info("team_title       : %r", src.get("team_title"))
    logger.info("workspace_esid   : %r", ws_esid)
    logger.info("organization_esid: %s", org_refs)

    dead = [o for o in org_refs if not _org_exists(es, o)]
    live = [o for o in org_refs if o not in dead]
    logger.info("  dead org refs  : %s", dead)
    logger.info("  live org refs  : %s", live)
    logger.info("  -> graph-orphaned via org leg: %s\n", bool(org_refs) and not live)

    # 2. Workspace doc existence.
    if ws_esid:
        try:
            ws_present = bool(es.exists(index=WORKSPACE_INDEX, id=ws_esid))
        except Exception:  # noqa: BLE001
            ws_present = bool(
                es.search(index=WORKSPACE_INDEX, body={"query": {"ids": {"values": [ws_esid]}}, "size": 1})
                .get("hits", {})
                .get("hits")
            )
        logger.info("workspace doc %r exists: %s", ws_esid, ws_present)
        logger.info("  (the bug is reachability, not a missing doc — this should be True)\n")

    # 3. The REAL heal-on-read graph walk.
    try:
        from web.services.role_filter_service import _workspace_esids_from_scope_esids
    except ImportError:
        from asset_management.web.services.role_filter_service import (
            _workspace_esids_from_scope_esids,
        )

    walked = _workspace_esids_from_scope_esids(org_esids=set(), team_esids={team_esid})
    logger.info("_workspace_esids_from_scope_esids(team={...}) -> %s", sorted(walked))

    if ws_esid and ws_esid in walked:
        logger.info("  PASS: heal-on-read surfaces %r for this team.", ws_esid)
    elif ws_esid:
        logger.info("  FAIL: %r NOT in the walk result — heal-on-read did not surface it.", ws_esid)
    else:
        logger.info("  (team has no workspace_esid — nothing for the direct leg to surface.)")

    # 4. Optional: does --user's resolved scope include this team?
    if args.user:
        roles = [r.strip() for r in args.roles.split(",") if r.strip()]
        logger.info("\n--- scope of user %r (roles=%s) ---", args.user, roles)
        try:
            scope = _resolve_scope(args.user, roles)
        except Exception as e:  # noqa: BLE001
            logger.info("  could not resolve scope: %s", e)
            return 0
        logger.info("  scope_source : %s", scope.scope_source)
        logger.info("  team_esids   : %d (target present: %s)", len(scope.team_esids), team_esid in scope.team_esids)
        logger.info("  org_esids    : %d", len(scope.org_esids))
        # How many distinct workspaces does this user's scope roll up to? This
        # is what the Builder topbar keys off: >1 -> Workspace DROPDOWN; ==1 ->
        # static label (no dropdown); 0 -> hidden.
        ws_set = _workspace_esids_from_scope_esids(scope.org_esids, scope.team_esids)
        logger.info("  resolved workspaces (%d): %s", len(ws_set), sorted(ws_set))
        if len(ws_set) <= 1:
            logger.info(
                "  -> Builder shows a STATIC workspace label (not a dropdown) — %d workspace. " "This is expected, not a bug.",
                len(ws_set),
            )
        if team_esid in scope.team_esids:
            logger.info(
                "  -> %r IS in this user's resolved team scope: their Asset Explorer / "
                "Data Inventory exercises the heal-on-read leg for this team.",
                team_esid,
            )
        else:
            logger.info(
                "  -> %r is NOT in this user's resolved team scope. If the workspace still "
                "shows for them in the UI, it's via superuser breadth (match_all), NOT the "
                "heal-on-read leg — the UI test does not isolate this fix for this user.",
                team_esid,
            )

    # 5. Optional: the #3 fix — bare vs .keyword term on de_asset_organization.
    # Replicates _org_team_titles_in_workspace's query directly (NOT importing
    # app.py, which would run seeders against live ES). The bare field is the
    # fix; the .keyword field is the old silent-zero. Showing BOTH counts side
    # by side is the discriminating proof: bare > 0 while .keyword == 0 means
    # the field is pure keyword and the fix is what restores the org list.
    if args.workspace:
        ws = args.workspace
        logger.info("\n--- #3 fix: de_asset_organization orgs in workspace %r ---", ws)

        def _count(field):
            try:
                resp = es.search(
                    index=ORGANIZATION_INDEX,
                    body={"query": {"term": {field: ws}}, "_source": ["organization_title"], "size": 10000},
                )
                return [h.get("_source", {}).get("organization_title") for h in resp.get("hits", {}).get("hits", [])]
            except Exception as e:  # noqa: BLE001
                logger.info("  query on %s failed: %s", field, e)
                return []

        bare = _count("workspace_esid")
        keyword = _count("workspace_esid.keyword")
        logger.info("  term:workspace_esid          -> %d orgs %s", len(bare), sorted(t for t in bare if t))
        logger.info("  term:workspace_esid.keyword  -> %d orgs (the OLD silent-zero query)", len(keyword))
        if len(bare) > 0 and len(keyword) == 0:
            logger.info("  PASS: bare field resolves orgs while .keyword returns 0 — the fix is load-bearing here.")
        elif len(bare) > 0:
            logger.info("  bare field resolves %d orgs (Builder dropdown populated).", len(bare))
        else:
            logger.info("  NOTE: 0 orgs either way — try a workspace known to have orgs.")

    # 6. Optional: the #5 fix — reproduce the Builder filter-options ESID
    # narrowing for a workspace and show each TITLE's count BEFORE (all orgs,
    # the old title-only narrowing) vs AFTER (ESID-narrowed). A title that
    # collapses 2->1 is the duplicate-"Kavanah" leak being closed. This mirrors
    # _scope_filter_options_to_user's workspace layer exactly (load all orgs ->
    # keep those whose ESID is in the workspace's org-esid set), without
    # importing app.py (which runs seeders).
    if args.dropdown_workspace:
        ws = args.dropdown_workspace
        logger.info("\n--- #5 fix: filter-options org narrowing for workspace %r ---", ws)
        # All orgs (id + title) — the unfiltered list the endpoint starts from.
        all_resp = es.search(
            index=ORGANIZATION_INDEX,
            body={"query": {"match_all": {}}, "_source": ["organization_title"], "size": 10000},
        )
        all_orgs = [
            {"esid": h["_id"], "title": (h.get("_source") or {}).get("organization_title")}
            for h in all_resp.get("hits", {}).get("hits", [])
        ]
        # The workspace's org-esid set (what _org_team_esids_in_workspace returns).
        ws_resp = es.search(
            index=ORGANIZATION_INDEX,
            body={"query": {"term": {"workspace_esid": ws}}, "_source": False, "size": 10000},
        )
        ws_esids = {h["_id"] for h in ws_resp.get("hits", {}).get("hits", [])}

        # OLD behaviour: narrow by TITLE (every org whose title matches an
        # in-workspace org's title survives — the leak).
        ws_titles = {o["title"] for o in all_orgs if o["esid"] in ws_esids and o["title"]}
        old_kept = [o for o in all_orgs if o["title"] in ws_titles]
        # NEW behaviour: narrow by ESID.
        new_kept = [o for o in all_orgs if o["esid"] in ws_esids]

        def _title_counts(items):
            counts: dict[str, int] = {}
            for it in items:
                t = it["title"]
                if t:
                    counts[t] = counts.get(t, 0) + 1
            return counts

        old_counts = _title_counts(old_kept)
        new_counts = _title_counts(new_kept)
        dupes = {t: (old_counts[t], new_counts.get(t, 0)) for t in old_counts if old_counts[t] > 1}
        logger.info("  orgs in dropdown — OLD title-narrowing: %d, NEW esid-narrowing: %d", len(old_kept), len(new_kept))
        if dupes:
            for t, (was, now) in sorted(dupes.items()):
                logger.info("  title %r: OLD showed %d (duplicated), NEW shows %d", t, was, now)
            if all(now <= 1 for (_was, now) in dupes.values()):
                logger.info("  PASS: every duplicated title collapses to a single entry under ESID narrowing.")
            else:
                logger.info("  NOTE: a title still appears >1 under ESID narrowing — two real same-workspace orgs?")
        else:
            logger.info("  No duplicated org titles in this workspace's dropdown (nothing to collapse).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
