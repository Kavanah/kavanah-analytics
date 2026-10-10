"""One-shot migration: write new-authz docs for users who still depend on legacy role strings.

 (2026-05-26) placed ``_legacy_es_security_titles`` as a
first-class scope source in ``RoleFilterService._resolve_user_scope_uncached``
so that the 6 prod beta-tester users whose ``session["roles"]`` still
carries ``team_<title>`` / ``org_<title>`` strings — but whose
``de_authz_user_team_role`` / ``de_authz_user_org_role`` docs are empty —
could continue to resolve scope while the migration finished.

 (2026-05-28) retires the legacy regex. Pre-retirement, every
such hybrid user needs their new-authz docs written so they keep
resolving the same scope after the regex is gone.

This script:

1. Reads every user doc in ``de_users``.
2. For each user with legacy ``team_<title>`` or ``org_<title>`` strings
   in ``roles`` / ``es_security_roles``, checks whether they have any
   ``de_authz_user_team_role`` / ``de_authz_user_org_role`` doc.
3. If yes, no-op (the new-authz path already covers them).
4. If no, resolves each legacy title to its ESID via ``de_asset_teams``
   / ``de_asset_organization``, then upserts the corresponding
   membership doc with ``upsert_membership_doc`` semantics that match
   ``RoleFilterService._resolve_titles_from_user_role_index``.

**Idempotent.** Re-running after a successful pass produces zero
writes. Each ``(username, team_esid)`` and ``(username, org_esid)``
pair is one doc; the script uses ``term`` queries to detect existing
docs and skips them.

**Dry-run by default.** Use ``--apply`` to write docs. Use ``--verbose``
to print every resolution decision (helpful on the first dry-run).

Sequencing (do NOT skip):

    # 1. Dry-run on staging — confirms the cohort and the resolutions.
    ssh ubuntu@<server-ip>
    sudo docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_legacy_authz_users --verbose

    # 2. Apply on staging.
    sudo docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_legacy_authz_users --apply

    # 3. Smoke a hybrid user on staging — they still see their team's data.

    # 4. Dry-run on prod.
    ssh ubuntu@<prod-host>
    sudo docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_legacy_authz_users --verbose

    # 5. Apply on prod after a teammate reviews the dry-run output.

After both prod and staging report "Hybrid cohort: 0" on a fresh
dry-run, the legacy regex deletion in role_filter_service.py is safe
to ship.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from typing import Iterable

# Three-candidate path resolver — tools live in bin/python/tools, run
# from imports_container with /app/python on PYTHONPATH; cover both.
_HERE = os.path.dirname(os.path.abspath(__file__))
for candidate in (
    os.path.abspath(os.path.join(_HERE, "..")),
    os.path.abspath(os.path.join(_HERE, "..", "..")),
    "/app/python",
):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logging.basicConfig(format="[%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("backfill_legacy_authz_users")

TEAM_ROLE_INDEX = "de_authz_user_team_role"
ORG_ROLE_INDEX = "de_authz_user_org_role"
TEAMS_INDEX = "de_asset_teams"
ORGS_INDEX = "de_asset_organization"


def _iter_legacy_titles(roles: list) -> tuple[list[str], list[str]]:
    """Return (team_titles, org_titles) extracted from an ES Security
    user record's ``roles`` array (the legacy ``team_<title>`` /
    ``org_<title>`` strings)."""
    teams: list[str] = []
    orgs: list[str] = []
    for r in roles or []:
        if not isinstance(r, str):
            continue
        if r.startswith("team_"):
            teams.append(r[len("team_") :])
        elif r.startswith("org_"):
            orgs.append(r[len("org_") :])
    return teams, orgs


def _resolve_title_to_esid(es, index: str, title_field: str, title: str) -> str | None:
    """Match the legacy convention used by
    ``DashboardService._convert_name_to_esid``: match_phrase
    on the text field, take the first hit."""
    try:
        result = es.search(
            index=index,
            body={"query": {"match_phrase": {title_field: title}}, "size": 1},
        )
    except Exception as exc:
        log.error("ES lookup failed for %s '%s': %s", index, title, exc)
        return None
    hits = result.get("hits", {}).get("hits", [])
    if not hits:
        return None
    return hits[0].get("_id")


def _has_new_authz_doc(es, index: str, username: str) -> bool:
    try:
        result = es.search(
            index=index,
            body={"query": {"term": {"username": username}}, "size": 1},
        )
    except Exception as exc:
        log.error("ES lookup failed for %s username=%s: %s", index, username, exc)
        return False
    return bool(result.get("hits", {}).get("hits"))


def _doc_exists(es, index: str, username: str, esid_field: str, esid: str) -> bool:
    try:
        result = es.search(
            index=index,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"username": username}},
                            {"term": {esid_field: esid}},
                        ]
                    }
                },
                "size": 1,
            },
        )
    except Exception as exc:
        log.error("ES lookup failed for %s/%s/%s: %s", index, username, esid, exc)
        return True  # Fail closed — treat as exists, do not double-write.
    return bool(result.get("hits", {}).get("hits"))


def _write_doc(es, index: str, username: str, esid_field: str, esid: str, dry_run: bool) -> bool:
    """Returns True if a doc was (or would be) written."""
    if _doc_exists(es, index, username, esid_field, esid):
        return False
    if dry_run:
        log.info("DRY-RUN would write %s username=%s %s=%s", index, username, esid_field, esid)
        return True
    body = {"username": username, esid_field: esid, "source": "backfill_2"}
    try:
        es.index(index=index, document=body, refresh=True)
        log.info("WROTE %s username=%s %s=%s", index, username, esid_field, esid)
        return True
    except Exception as exc:
        log.error("WRITE FAILED %s username=%s %s=%s: %s", index, username, esid_field, esid, exc)
        return False


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="actually write docs (default: dry-run)")
    p.add_argument("--verbose", action="store_true", help="log per-user decisions")
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.verbose:
        log.setLevel(logging.DEBUG)

    dry_run = not args.apply
    log.info("Mode: %s", "DRY-RUN" if dry_run else "APPLY")

    es = get_elasticsearch_client()
    if es is None:
        log.error("ES client unavailable; aborting.")
        return 1

    # Users live in ES Security, not in a regular index. `get_user()`
    # without a username returns every user — pattern matches
    # ``GoogleSsoService.find_user_by_email`` and
    # ``role_permissions_service.py``. <100 users in practice.
    try:
        all_users = dict(es.security.get_user())
    except Exception as exc:
        log.error("es.security.get_user() failed: %s", exc)
        return 1
    log.info("Scanning %d ES Security users", len(all_users))

    hybrid_users: list[tuple[str, list[str], list[str]]] = []
    for username, record in all_users.items():
        username = (username or "").strip()
        if not username:
            continue
        team_titles, org_titles = _iter_legacy_titles(record.get("roles") or [])
        if not team_titles and not org_titles:
            continue
        # Hybrid = legacy strings present AND no new-authz doc.
        if _has_new_authz_doc(es, TEAM_ROLE_INDEX, username) or _has_new_authz_doc(es, ORG_ROLE_INDEX, username):
            if args.verbose:
                log.debug("skip username=%s (already has new-authz doc)", username)
            continue
        hybrid_users.append((username, team_titles, org_titles))

    log.info("Hybrid cohort: %d user(s)", len(hybrid_users))
    if not hybrid_users:
        log.info("Nothing to do.")
        return 0

    written_by_index: dict[str, int] = defaultdict(int)
    skipped_unresolved: list[tuple[str, str, str]] = []

    for username, team_titles, org_titles in hybrid_users:
        log.info("User: %s  team_titles=%s  org_titles=%s", username, team_titles, org_titles)
        for title in team_titles:
            esid = _resolve_title_to_esid(es, TEAMS_INDEX, "team_title", title)
            if not esid:
                skipped_unresolved.append((username, "team", title))
                log.warning("  UNRESOLVED team_title=%r — no match in %s", title, TEAMS_INDEX)
                continue
            if _write_doc(es, TEAM_ROLE_INDEX, username, "team_esid", esid, dry_run):
                written_by_index[TEAM_ROLE_INDEX] += 1
        for title in org_titles:
            esid = _resolve_title_to_esid(es, ORGS_INDEX, "organization_title", title)
            if not esid:
                skipped_unresolved.append((username, "org", title))
                log.warning("  UNRESOLVED org_title=%r — no match in %s", title, ORGS_INDEX)
                continue
            if _write_doc(es, ORG_ROLE_INDEX, username, "org_esid", esid, dry_run):
                written_by_index[ORG_ROLE_INDEX] += 1

    log.info("Summary:")
    for idx, n in written_by_index.items():
        log.info("  %s: %d %s", idx, n, "would-write" if dry_run else "wrote")
    if skipped_unresolved:
        log.warning(
            "  Unresolved titles (no ESID match): %d — these users will lose scope after legacy regex retires.",
            len(skipped_unresolved),
        )
        for u, kind, t in skipped_unresolved:
            log.warning("    %s  %s_title=%r", u, kind, t)
        log.warning(
            "Investigate before applying. A missing ESID means the title was renamed/deleted, "
            "or the user's legacy role refers to a team/org that no longer exists."
        )
        return 2 if dry_run else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
