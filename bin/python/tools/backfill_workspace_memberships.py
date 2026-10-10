"""One-shot backfill: workspace memberships + role promotions.

Final step of the F4 → F5 → F8 substrate. Once F4 created the
singleton workspace and F5 reparented every org under it, this script
gives every existing Kavanah user a workspace-tier membership and a
workspace-tier role:

* For every user with at least one row in ``de_authz_org_membership``,
  write a ``de_authz_workspace_membership`` doc pointing at the Kavanah
  singleton with ``is_primary=true``.
* Promote each user's **highest** org-tier role to a workspace-tier
  role and write a ``de_authz_user_workspace_role`` doc. Mapping:

  * ``owner``    → ``workspace_owner``
  * ``org_admin`` → ``workspace_admin``
  * everything else (editor, viewer, team_admin, custom roles) →
    ``workspace_viewer`` (renamed from ``workspace_member``,
    which remains a seeded legacy alias)

  When a user has multiple org-role assignments (rare; possible for
  admins managing several orgs), the highest one in the precedence
  order ``owner > org_admin > anything_else`` wins. This is the
  promotion direction documented in the F8 ticket spec.

The script is **dry-run by default**. Use ``--apply`` to write.

Idempotency: deterministic doc ids (``{username}:{workspace_esid}`` for
the membership, ``{username}:{workspace_esid}:{role_esid}`` for the
role) + ``op_type=create`` + 409-as-success. Re-running after a
successful pass produces ``created=0 skipped=N``.

Usage::

    # Dry-run (default).
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_workspace_memberships

    # Apply.
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_workspace_memberships --apply

    # Target a different workspace (rare; mainly for test setups).
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_workspace_memberships --apply \\
        --workspace-slug my-workspace
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
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
    ORG_MEMBERSHIP_INDEX,
    USER_ORG_ROLE_INDEX,
    USER_WORKSPACE_ROLE_INDEX,
    WORKSPACE_INDEX,
    WORKSPACE_MEMBERSHIP_INDEX,
)
from asset_management.web.services.permissions_seeder_service import (  # noqa: E402
    ROLE_ORG_ADMIN,
    ROLE_OWNER,
    ROLE_WORKSPACE_ADMIN,
    ROLE_WORKSPACE_OWNER,
    ROLE_WORKSPACE_VIEWER,
)
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_SLUG = "kavanah-media"


def promote_role(org_role_esids: set[str]) -> str:
    """Map a user's set of org-tier roles to one workspace-tier role.

    Precedence: ``owner > org_admin > anything_else``. Returns the
    matching workspace role esid. Pure function — exported so tests can
    pin the mapping without spinning up an ES double.
    """
    if ROLE_OWNER in org_role_esids:
        return ROLE_WORKSPACE_OWNER
    if ROLE_ORG_ADMIN in org_role_esids:
        return ROLE_WORKSPACE_ADMIN
    # renamed from the former workspace_member role.
    return ROLE_WORKSPACE_VIEWER


def fetch_target_workspace(es, *, workspace_slug: str) -> str | None:
    """Resolve a workspace slug to its ESID (its ES _id under our convention)."""
    try:
        resp = es.get(index=WORKSPACE_INDEX, id=workspace_slug)
    except Exception as e:
        if "not_found" in str(e).lower() or "404" in str(e):
            return None
        raise
    return resp.get("_id")


def fetch_user_to_org_roles(es) -> dict[str, set[str]]:
    """Read every user's set of org-tier role esids from de_authz_user_org_role.

    Returns ``{username: {role_esid_1, role_esid_2, ...}}``. Users with
    no org-role assignment do not appear (the membership-only path is
    handled separately — see ``fetch_users_with_org_membership``).
    """
    users: dict[str, set[str]] = {}
    try:
        resp = es.search(
            index=USER_ORG_ROLE_INDEX,
            body={"query": {"match_all": {}}, "size": 10000},
        )
    except Exception as e:
        logger.error("Could not search %s: %s", USER_ORG_ROLE_INDEX, e)
        return users
    for hit in resp.get("hits", {}).get("hits", []):
        source = hit.get("_source") or {}
        username = source.get("username")
        role = source.get("role_esid")
        if not username or not role:
            continue
        users.setdefault(username, set()).add(role)
    return users


def fetch_users_with_org_membership(es) -> set[str]:
    """Return every distinct username that has a row in de_authz_org_membership."""
    usernames: set[str] = set()
    try:
        resp = es.search(
            index=ORG_MEMBERSHIP_INDEX,
            body={"query": {"match_all": {}}, "size": 10000},
        )
    except Exception as e:
        logger.error("Could not search %s: %s", ORG_MEMBERSHIP_INDEX, e)
        return usernames
    for hit in resp.get("hits", {}).get("hits", []):
        source = hit.get("_source") or {}
        username = source.get("username")
        if username:
            usernames.add(username)
    return usernames


def _safe_create(es, *, index: str, doc_id: str, body: dict) -> str:
    """op_type=create write that maps 409 to skip. Returns 'created' | 'skipped'."""
    try:
        es.index(index=index, id=doc_id, document=body, op_type="create", refresh="wait_for")
        return "created"
    except Exception as e:
        msg = str(e).lower()
        if "version_conflict" in msg or "conflict" in msg:
            return "skipped"
        raise


def run(*, apply: bool, workspace_slug: str) -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    workspace_esid = fetch_target_workspace(es, workspace_slug=workspace_slug)
    if workspace_esid is None:
        logger.error(
            "Workspace slug %r not found in %s. Re-run tools.bootstrap_workspaces --apply first.",
            workspace_slug,
            WORKSPACE_INDEX,
        )
        return 2

    user_to_roles = fetch_user_to_org_roles(es)
    membership_users = fetch_users_with_org_membership(es)
    # Union: every user with EITHER an org-role OR an org-membership
    # should land on the workspace. Membership-only users (rare —
    # legacy seeding without role docs) get the workspace_viewer role.
    all_users = sorted(set(user_to_roles.keys()) | membership_users)

    if not all_users:
        logger.info("No users with org memberships or roles; nothing to do.")
        return 0

    # Plan: per-user promoted role + the two doc writes.
    plan: list[dict] = []
    for username in all_users:
        org_roles = user_to_roles.get(username, set())
        workspace_role = promote_role(org_roles)
        plan.append(
            {
                "username": username,
                "org_roles": sorted(org_roles),
                "workspace_role": workspace_role,
            }
        )

    logger.info("Backfill plan (workspace_esid=%s, %d users):", workspace_esid, len(plan))
    for entry in plan:
        logger.info(
            "  %s | org_roles=%s -> workspace_role=%s",
            entry["username"],
            entry["org_roles"] or "(none)",
            entry["workspace_role"],
        )

    if not apply:
        logger.info("")
        logger.info("DRY-RUN: no writes. Pass --apply to backfill for real.")
        return 0

    logger.info("")
    logger.info("APPLY: writing memberships + role assignments...")
    now_iso = datetime.now(timezone.utc).isoformat()
    membership_created = 0
    membership_skipped = 0
    role_created = 0
    role_skipped = 0
    failed = 0

    for entry in plan:
        username = entry["username"]
        workspace_role = entry["workspace_role"]

        # Membership write
        try:
            result = _safe_create(
                es,
                index=WORKSPACE_MEMBERSHIP_INDEX,
                doc_id=f"{username}:{workspace_esid}",
                body={
                    "username": username,
                    "workspace_esid": workspace_esid,
                    "is_primary": True,
                    "created_at": now_iso,
                },
            )
            if result == "created":
                membership_created += 1
                logger.info("  CREATED membership %s -> %s", username, workspace_esid)
            else:
                membership_skipped += 1
                logger.info("  SKIPPED membership %s -> %s (already exists)", username, workspace_esid)
        except Exception as e:
            failed += 1
            logger.error("  FAILED membership %s: %s", username, e)
            continue

        # Role assignment write
        try:
            result = _safe_create(
                es,
                index=USER_WORKSPACE_ROLE_INDEX,
                doc_id=f"{username}:{workspace_esid}:{workspace_role}",
                body={
                    "username": username,
                    "workspace_esid": workspace_esid,
                    "role_esid": workspace_role,
                    "created_at": now_iso,
                },
            )
            if result == "created":
                role_created += 1
                logger.info("  CREATED role %s -> %s:%s", username, workspace_esid, workspace_role)
            else:
                role_skipped += 1
                logger.info("  SKIPPED role %s -> %s:%s (already exists)", username, workspace_esid, workspace_role)
        except Exception as e:
            failed += 1
            logger.error("  FAILED role %s: %s", username, e)

    logger.info("")
    logger.info(
        "Backfill complete: membership(created=%d skipped=%d) role(created=%d skipped=%d) failed=%d",
        membership_created,
        membership_skipped,
        role_created,
        role_skipped,
        failed,
    )
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the memberships + role assignments. Default is dry-run.",
    )
    parser.add_argument(
        "--workspace-slug",
        default=DEFAULT_WORKSPACE_SLUG,
        help=f"Target workspace slug. Default: {DEFAULT_WORKSPACE_SLUG!r}.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run(apply=args.apply, workspace_slug=args.workspace_slug)


if __name__ == "__main__":
    raise SystemExit(main())
