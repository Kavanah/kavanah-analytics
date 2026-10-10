"""One-shot cleanup: delete legacy `__all__:system_admin` sentinel docs.

 introduced the ``__all__:system_admin`` sentinel doc in
``de_authz_user_team_role`` to flag users as system-admin so
``RoleFilterService`` could route them through the grant-scoped
filtering path. retires that pattern: ``asset_admin`` (and any
future privileged role in ``_DATA_PRIVILEGED_ROLES``) is now handled
via role-name detection in ``_user_is_privileged``, with scope derived
from real memberships + active grants.

The sentinel docs that remain on staging (re-seeded 2026-05-14 during
the rollback for ``team_test``, ``cody``, ``zap-scanner``,
``team_user``) are now:

1. **Functionally redundant** — ``_user_is_system_admin`` and the
   ``_DATA_PRIVILEGED_ROLES`` branch both flag the same users as
   privileged, so removing the sentinel does not change scope
   resolution for ``asset_admin`` users (they keep being privileged via
   the role-name path).

2. **Causing UX bugs** — the Edit Roles modal at ``app.py:5426``
   renders the sentinel doc as a membership labelled "All teams
   (system admin)", which is confusing for admins viewing
   ``team_test``'s memberships.

This script deletes every ``de_authz_user_team_role`` doc whose
``team_esid == "__all__"``. Idempotent: re-running after a successful
pass produces zero deletes.

**Dry-run by default.** Use ``--apply`` to perform the deletes.

Asset_admin scope still works after this script runs because:

* ``_user_is_privileged`` returns True via the
  ``_DATA_PRIVILEGED_ROLES`` branch (role-name check).
* ``should_apply_filter`` therefore returns True.
* ``get_allowed_*`` consults ``_privileged_user_scope`` which queries
  ``de_authz_user_team_role`` for real memberships and
  ``de_authz_data_grant`` for grants.

A bare ``asset_admin`` with no memberships and no grants ends up at the
``__NO_TEAMS_FOR_ORG__`` deny-all sentinel — the load-bearing privacy
boundary.

Usage::

    # Dry-run (default) — prints what would be deleted, no writes.
    docker exec -w /app/python imports_container python3 \\
        -m tools.strip_legacy_sentinel_docs

    # Apply on staging.
    docker exec -w /app/python imports_container python3 \\
        -m tools.strip_legacy_sentinel_docs --apply

    # Limit to a single user (e.g. spot-check on staging before broad run).
    docker exec -w /app/python imports_container python3 \\
        -m tools.strip_legacy_sentinel_docs --apply --username team_test
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package — mirror
# bootstrap_data_grants.py's three-candidate resolver.
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

INDEX = "de_authz_user_team_role"
SENTINEL_ESID = "__all__"


def _sentinel_docs(es, username_filter: str | None = None) -> list[dict]:
    """Return all sentinel docs matching the optional username filter."""
    must: list[dict] = [{"term": {"team_esid": SENTINEL_ESID}}]
    if username_filter:
        must.append({"term": {"username": username_filter}})

    resp = es.search(
        index=INDEX,
        body={"query": {"bool": {"must": must}}, "size": 1000},
    )
    return resp.get("hits", {}).get("hits", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete docs. Without this flag, runs as a dry-run.",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="Limit to a single username (default: all users with sentinel docs).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch client unavailable; aborting.")
        return 1

    docs = _sentinel_docs(es, args.username)
    if not docs:
        logger.info("No sentinel docs found%s — nothing to do.", f" for {args.username}" if args.username else "")
        return 0

    logger.info(
        "Found %d sentinel doc(s)%s:",
        len(docs),
        f" for {args.username}" if args.username else "",
    )
    for hit in docs:
        src = hit.get("_source", {})
        logger.info(
            "  _id=%s  username=%s  role_esid=%s",
            hit["_id"],
            src.get("username", "?"),
            src.get("role_esid", "?"),
        )

    if not args.apply:
        logger.info("Dry-run: %d doc(s) would be deleted. Pass --apply to write.", len(docs))
        return 0

    deleted = 0
    failed = 0
    for hit in docs:
        try:
            es.delete(index=INDEX, id=hit["_id"], refresh=True)
            deleted += 1
        except Exception as exc:  # noqa: BLE001 — top-level script boundary
            logger.error("Failed to delete %s: %s", hit["_id"], exc)
            failed += 1

    logger.info("Deleted %d doc(s); %d failed.", deleted, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
