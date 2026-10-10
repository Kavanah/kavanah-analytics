"""One-shot cleanup: purge orphan authz docs for deleted users.

 added a cascade-on-delete path so future `api_delete_user` calls
purge the user's docs from the seven authz indices. This script cleans
up the **historical** orphans: docs that accumulated for the ~3 months
between Phase 14 (user-create dual-write) and
(delete-cascade), where users were deleted from ES Security but their
authz rows were left behind.

Algorithm per index:

1. Fetch the current ES Security username set via `es.security.get_user()`.
2. Run `delete_by_query` against the index with::

       {"bool": {"must_not": [{"terms": {"username": [<current users>]}}]}}

3. Report deleted counts.

Idempotent: a second run after `--apply` reports zero deletes.

The script is **dry-run by default**. Use ``--apply`` to write.

Usage::

    # Dry-run (counts only; no writes).
    docker exec -w /app/python imports_container python3 \\
        -m tools.purge_authz_orphans

    # Apply on staging.
    docker exec -w /app/python imports_container python3 \\
        -m tools.purge_authz_orphans --apply

    # Limit to one index for diagnosis.
    docker exec -w /app/python imports_container python3 \\
        -m tools.purge_authz_orphans --index de_authz_user_workspace_role
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as the other tools/ scripts.
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management" / "__init__.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_elasticsearch_client  # noqa: E402


# The username-keyed indices the cascade fans out to. Kept in sync
# with permissions_index_service.AUTHZ_USERNAME_KEYED_INDICES by hand —
# importing that module here would pull in the entire Flask stack.
# follow-up (2026-05-21) added de_asset_user_mfa as the 8th
# index after a stale MFA record was matching re-signup with a
# recycled username.
AUTHZ_USERNAME_INDICES: tuple[str, ...] = (
    "de_authz_workspace_membership",
    "de_authz_org_membership",
    "de_authz_team_membership",
    "de_authz_user_workspace_role",
    "de_authz_user_org_role",
    "de_authz_user_team_role",
    "de_authz_data_grant",
    "de_asset_user_mfa",
)


def _fetch_es_security_usernames(es) -> set[str]:
    """Return the set of every username currently registered in ES Security."""
    resp = dict(es.security.get_user())
    return set(resp.keys())


def _orphan_query(current_usernames: set[str]) -> dict:
    """delete_by_query body that matches every doc whose ``username``
    is NOT in the current ES Security user set.

    Empty username set is treated as a safety guard — we refuse to
    delete *every* row (an outage of es.security.get_user must not
    cascade into a full authz wipe). Caller must check.
    """
    if not current_usernames:
        raise ValueError("refusing to build orphan-purge query with empty username set " "(would match every doc)")
    return {
        "query": {
            "bool": {
                "must_not": [{"terms": {"username": sorted(current_usernames)}}],
            }
        }
    }


def _count_orphans(es, index: str, query: dict) -> int:
    """Return the number of orphan docs the query matches (dry-run path)."""
    try:
        resp = es.count(index=index, body=query)
        return int(resp.get("count", 0))
    except Exception as e:
        err = str(e).lower()
        if "index_not_found" in err or "not_found" in err or "404" in err:
            return 0
        raise


def _delete_orphans(es, index: str, query: dict) -> int:
    """Run delete_by_query and return the deleted count."""
    resp = es.delete_by_query(
        index=index,
        body=query,
        refresh=True,
        conflicts="proceed",
    )
    return int(resp.get("deleted", 0))


def purge(es, *, apply: bool, indices: tuple[str, ...] | None = None) -> dict[str, int]:
    """Main entry point.

    Returns ``{index: count}`` where count is the matched (dry-run) or
    deleted (apply) row count per index.
    """
    targets = indices if indices is not None else AUTHZ_USERNAME_INDICES
    current = _fetch_es_security_usernames(es)
    logging.info("purge: %d current ES Security users", len(current))
    query = _orphan_query(current)

    results: dict[str, int] = {}
    for index in targets:
        try:
            if apply:
                n = _delete_orphans(es, index, query)
                logging.info("purge: %s deleted=%d (apply)", index, n)
            else:
                n = _count_orphans(es, index, query)
                logging.info("purge: %s orphans=%d (dry-run)", index, n)
            results[index] = n
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "not_found" in err or "404" in err:
                logging.info("purge: %s (index missing — skipped)", index)
                results[index] = 0
                continue
            logging.warning("purge: %s failed: %s", index, e)
            results[index] = 0
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run, counts only).")
    parser.add_argument(
        "--index",
        action="append",
        choices=AUTHZ_USERNAME_INDICES,
        help="Limit to a specific index (repeatable). Default: all 7.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Elasticsearch unavailable — exiting.")
        return 1

    indices = tuple(args.index) if args.index else AUTHZ_USERNAME_INDICES
    results = purge(es, apply=args.apply, indices=indices)

    total = sum(results.values())
    mode = "deleted" if args.apply else "would delete"
    logging.info("purge summary — %s %d orphan docs across %d indices:", mode, total, len(results))
    for index, n in results.items():
        logging.info("  %s: %d", index, n)
    if not args.apply and total > 0:
        logging.info("Re-run with --apply to actually delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
