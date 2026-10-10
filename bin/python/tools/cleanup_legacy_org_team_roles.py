"""One-shot cleanup: remove legacy ``org_*`` / ``team_*`` ES Security roles.

 retired the *runtime* read path for legacy org_*/team_* ES Security
roles in favor of the de_authz_* membership model. The role *definitions*
and their user-record assignments still exist on prod (56+ definitions per
the post- audit). This script removes them in three idempotent passes:

1. **User records** — for every ES Security user, strip ``org_*`` / ``team_*``
   entries from their ``roles`` array via ``es.security.put_user`` (metadata-merge).
   Runs first so role deletion doesn't orphan access.

2. **ES Security role definitions** — for every role whose name matches
   ``^(org|team)_.+``, call ``es.security.delete_role``.

3. **``de_role_permissions`` index** — delete any docs whose ``role_name``
   field matches ``^(org|team)_.+``. collapsed runtime permission
   reads to a hardcoded three-case map; these legacy docs are dead weight.

Each surface is idempotent. Re-running after a successful pass produces
zero writes. Each phase can be enabled / disabled independently.

The script is **dry-run by default**. Use ``--apply`` to write.

Usage::

    # Dry-run everything (default).
    docker exec -w /app/python imports_container python3 \\
        -m tools.cleanup_legacy_org_team_roles

    # Apply user-record cleanup only (Phase 1).
    docker exec -w /app/python imports_container python3 \\
        -m tools.cleanup_legacy_org_team_roles --apply --surface user-roles

    # Apply all three surfaces.
    docker exec -w /app/python imports_container python3 \\
        -m tools.cleanup_legacy_org_team_roles --apply --surface all

    # Cap dry-run output at 10 examples per surface (full counts always log).
    docker exec -w /app/python imports_container python3 \\
        -m tools.cleanup_legacy_org_team_roles --limit 10

Per the acceptance criteria, run user-roles BEFORE es-roles. The
``--surface all`` flow runs them in the correct order regardless of CLI
argument order.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as bin/python/tools/connection_backfill.py.
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

LEGACY_ROLE_PATTERN = re.compile(r"^(org|team)_.+")

# New-model role esids defined in permissions_seeder_service.ROLE_* that
# happen to share the legacy prefix. These are NOT legacy and must not be
# deleted. Kept in sync with permissions_seeder_service by hand because
# importing the service would pull in the entire Flask app stack.
_PRESERVED_NEW_MODEL_ROLES = frozenset({"org_admin", "team_admin"})

DE_ROLE_PERMISSIONS_INDEX = "de_role_permissions"
SURFACES = ("user-roles", "es-roles", "de-role-permissions", "all")


def _is_legacy_role(name: str) -> bool:
    if name in _PRESERVED_NEW_MODEL_ROLES:
        return False
    return bool(LEGACY_ROLE_PATTERN.match(name))


# ---------------------------------------------------------------------------
# Phase 1 — strip legacy role names from every ES Security user's roles array
# ---------------------------------------------------------------------------


def cleanup_user_roles(es, *, apply: bool, limit: int | None) -> dict:
    summary: dict[str, int] = {
        "scanned": 0,
        "users_with_legacy": 0,
        "legacy_assignments": 0,
        "would_update": 0,
        "updated": 0,
        "errors": 0,
    }

    users = es.security.get_user()
    summary["scanned"] = len(users)
    examples_logged = 0

    for username, record in users.items():
        roles = list(record.get("roles") or [])
        legacy = [r for r in roles if _is_legacy_role(r)]
        if not legacy:
            continue

        kept = [r for r in roles if r not in legacy]
        summary["users_with_legacy"] += 1
        summary["legacy_assignments"] += len(legacy)

        if not apply:
            summary["would_update"] += 1
            if limit is None or examples_logged < limit:
                logging.info(
                    "[dry-run] would strip %d legacy role(s) from username=%s: %s",
                    len(legacy),
                    username,
                    legacy,
                )
                examples_logged += 1
            continue

        try:
            # put_user requires the full user body. Carry every other field
            # through unchanged.
            body = {
                "roles": kept,
                "enabled": record.get("enabled", True),
                "full_name": record.get("full_name"),
                "email": record.get("email"),
                "metadata": record.get("metadata") or {},
            }
            # Drop keys we don't have a value for — put_user rejects null
            # full_name / email on some ES versions.
            body = {k: v for k, v in body.items() if v is not None}
            es.security.put_user(username=username, **body)
            summary["updated"] += 1
            logging.info(
                "stripped %d legacy role(s) from username=%s: %s",
                len(legacy),
                username,
                legacy,
            )
        except Exception as exc:
            summary["errors"] += 1
            logging.error("failed to strip roles from username=%s: %s", username, exc)

    return summary


# ---------------------------------------------------------------------------
# Phase 2 — delete legacy ES Security role definitions
# ---------------------------------------------------------------------------


def cleanup_es_roles(es, *, apply: bool, limit: int | None) -> dict:
    summary: dict[str, int] = {
        "scanned": 0,
        "legacy_found": 0,
        "would_delete": 0,
        "deleted": 0,
        "errors": 0,
    }

    response = es.security.get_role()
    summary["scanned"] = len(response)
    examples_logged = 0

    for role_name in sorted(response.keys()):
        if not _is_legacy_role(role_name):
            continue

        summary["legacy_found"] += 1

        if not apply:
            summary["would_delete"] += 1
            if limit is None or examples_logged < limit:
                logging.info("[dry-run] would delete ES role: %s", role_name)
                examples_logged += 1
            continue

        try:
            es.security.delete_role(name=role_name)
            summary["deleted"] += 1
            logging.info("deleted ES role: %s", role_name)
        except Exception as exc:
            summary["errors"] += 1
            logging.error("failed to delete ES role %s: %s", role_name, exc)

    return summary


# ---------------------------------------------------------------------------
# Phase 3 — delete legacy docs from de_role_permissions
# ---------------------------------------------------------------------------


def cleanup_de_role_permissions(es, *, apply: bool, limit: int | None) -> dict:
    summary: dict[str, int] = {
        "scanned": 0,
        "legacy_found": 0,
        "would_delete": 0,
        "deleted": 0,
        "errors": 0,
    }

    if not es.indices.exists(index=DE_ROLE_PERMISSIONS_INDEX):
        logging.info(
            "%s index does not exist — Phase 3 has nothing to do",
            DE_ROLE_PERMISSIONS_INDEX,
        )
        return summary

    # Scan every doc; we'll filter by role_name regex on the client because
    # left the index small (only ~3 active rows + the legacy crud).
    page_size = 500
    examples_logged = 0
    search_after = None

    while True:
        body = {
            "size": page_size,
            "sort": [{"role_name": "asc"}],
            "query": {"match_all": {}},
        }
        if search_after is not None:
            body["search_after"] = search_after
        resp = es.search(index=DE_ROLE_PERMISSIONS_INDEX, body=body)
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            summary["scanned"] += 1
            doc_id = hit.get("_id") or ""
            role_name = (hit.get("_source") or {}).get("role_name") or doc_id
            if not _is_legacy_role(role_name):
                continue
            summary["legacy_found"] += 1

            if not apply:
                summary["would_delete"] += 1
                if limit is None or examples_logged < limit:
                    logging.info(
                        "[dry-run] would delete %s/_doc/%s (role_name=%s)",
                        DE_ROLE_PERMISSIONS_INDEX,
                        doc_id,
                        role_name,
                    )
                    examples_logged += 1
                continue

            try:
                es.delete(index=DE_ROLE_PERMISSIONS_INDEX, id=doc_id, refresh="wait_for")
                summary["deleted"] += 1
                logging.info(
                    "deleted %s/_doc/%s (role_name=%s)",
                    DE_ROLE_PERMISSIONS_INDEX,
                    doc_id,
                    role_name,
                )
            except Exception as exc:
                summary["errors"] += 1
                logging.error(
                    "failed to delete %s/_doc/%s: %s",
                    DE_ROLE_PERMISSIONS_INDEX,
                    doc_id,
                    exc,
                )

        if len(hits) < page_size:
            break
        search_after = hits[-1].get("sort")

    return summary


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="legacy role cleanup")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply writes. Without this flag, the script is a no-op dry-run.",
    )
    parser.add_argument(
        "--surface",
        default="all",
        choices=SURFACES,
        help="Which surface to clean (default: all).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of dry-run example log lines per surface "
        "(summary counts are always complete). No effect with --apply.",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    mode = "APPLY" if args.apply else "DRY-RUN"
    logging.info(
        "legacy role cleanup (%s) — surface=%s limit=%s",
        mode,
        args.surface,
        args.limit if args.limit is not None else "<none>",
    )

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Elasticsearch client unavailable — aborting")
        return 1

    overall: dict[str, dict] = {}

    # Phase 1 first so role deletion doesn't orphan access.
    if args.surface in ("user-roles", "all"):
        logging.info("=== Phase 1: strip legacy roles from user records ===")
        overall["user-roles"] = cleanup_user_roles(es, apply=args.apply, limit=args.limit)
        logging.info("Phase 1 summary: %s", overall["user-roles"])

    if args.surface in ("es-roles", "all"):
        logging.info("=== Phase 2: delete legacy ES Security role definitions ===")
        overall["es-roles"] = cleanup_es_roles(es, apply=args.apply, limit=args.limit)
        logging.info("Phase 2 summary: %s", overall["es-roles"])

    if args.surface in ("de-role-permissions", "all"):
        logging.info("=== Phase 3: delete legacy de_role_permissions docs ===")
        overall["de-role-permissions"] = cleanup_de_role_permissions(es, apply=args.apply, limit=args.limit)
        logging.info("Phase 3 summary: %s", overall["de-role-permissions"])

    total_errors = sum(s.get("errors", 0) for s in overall.values())
    logging.info("=== Overall: %s ===", overall)
    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
