"""One-shot bootstrap: grant every ES Security superuser access to every existing org.

 ships the data-grant model (``de_authz_data_grant``) and routes
``RoleFilterService.should_apply_filter`` + the three ``get_allowed_*``
methods through the grant lookup for privileged users. A bare superuser
with zero grants sees nothing — that's the privacy boundary.

This script handles the *transition*: for every ES Security user whose
``roles`` array contains ``"superuser"``, write a
``de_authz_data_grant`` doc for each existing organization in
``de_asset_organization``. After running, every current superuser
sees what they saw earlier (every org), but their access is now
audit-logged and revocable per (user, org).

The script is **dry-run by default**. Use ``--apply`` to write.

Each write goes through ``DataGrantService.create`` which:

* Uses op_type=create + 409-as-success → idempotent. Re-running after
  a successful pass produces zero new writes, zero new audit-log rows.
* Audit-logs ``permission.data_grant.create`` with
  ``granted_by=system_migration`` and a fixed reason string so the
  bootstrap-era grants are greppable in ``de_audit_log``.
* Has ``expires_at=None`` → durable grants (no expiry).

Asset_admin users are **not** granted. They get their tenant data via
the ``db.view_tenant_data`` permission seeded onto their role
assignment; they don't need data grants.

Usage::

    # Dry-run (default) — prints what would be written, no writes.
    docker exec -w /app/python imports_container python3 \\
        -m tools.bootstrap_data_grants

    # Apply on staging or prod.
    docker exec -w /app/python imports_container python3 \\
        -m tools.bootstrap_data_grants --apply

    # Limit to a single user (e.g. testing on staging before a full run).
    docker exec -w /app/python imports_container python3 \\
        -m tools.bootstrap_data_grants --apply --username <username>
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as bin/python/tools/cleanup_legacy_org_team_roles.py.
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from asset_management.web.services.data_grant_service import DataGrantService, SCOPE_TYPE_ORG  # noqa: E402
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

GRANTED_BY_ACTOR = "system_migration"
GRANT_REASON = "transition bootstrap: pre-existing superuser, all current orgs"


def _superuser_usernames(es) -> list[str]:
    """Return ES Security usernames whose roles include ``superuser``.

    Excludes ``elastic`` (the root account) and built-in service users
    (``kibana_system``, ``logstash_system``, etc.) — those don't log
    into DEAM and never query through ``RoleFilterService``.
    """
    users = es.security.get_user()
    out: list[str] = []
    for username, info in users.items():
        if username == "elastic":
            continue
        roles = info.get("roles", []) or []
        if "superuser" in roles:
            out.append(username)
    return sorted(out)


def _existing_org_esids(es) -> list[tuple[str, str]]:
    """Return ``[(org_esid, org_title), ...]`` from ``de_asset_organization``.

    Title is included for human-readable dry-run output only — the grant
    itself is keyed by esid.
    """
    try:
        resp = es.search(
            index="de_asset_organization",
            body={"query": {"match_all": {}}, "size": 1000},
        )
    except Exception as e:
        logger.error("Could not search de_asset_organization: %s", e)
        return []
    out: list[tuple[str, str]] = []
    for hit in resp.get("hits", {}).get("hits", []):
        esid = hit.get("_id")
        title = (hit.get("_source") or {}).get("organization_title") or "<no-title>"
        if esid:
            out.append((esid, title))
    return sorted(out, key=lambda pair: pair[1].lower())


def run(*, apply: bool, only_username: str | None) -> int:
    """Plan and (optionally) apply the bootstrap. Returns the exit code."""
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    superusers = _superuser_usernames(es)
    if only_username is not None:
        if only_username not in superusers:
            logger.error(
                "User %r is not an ES Security superuser; aborting. " "Run without --username to see the full superuser list.",
                only_username,
            )
            return 2
        superusers = [only_username]

    orgs = _existing_org_esids(es)

    logger.info("Bootstrap plan:")
    logger.info("  superusers: %d", len(superusers))
    for u in superusers:
        logger.info("    - %s", u)
    logger.info("  orgs:       %d", len(orgs))
    for esid, title in orgs:
        logger.info("    - %s (%s)", title, esid)
    logger.info("  total grants to write: %d (superusers x orgs)", len(superusers) * len(orgs))
    logger.info("  granted_by=%s  reason=%r", GRANTED_BY_ACTOR, GRANT_REASON)

    if not apply:
        logger.info("")
        logger.info("DRY-RUN: no writes. Pass --apply to bootstrap for real.")
        return 0

    if not superusers or not orgs:
        logger.warning("Nothing to do (zero superusers or zero orgs).")
        return 0

    logger.info("")
    logger.info("APPLY: writing %d grants...", len(superusers) * len(orgs))
    created = 0
    skipped = 0
    failed = 0
    for username in superusers:
        for esid, title in orgs:
            try:
                wrote = DataGrantService.create(
                    username=username,
                    scope_type=SCOPE_TYPE_ORG,
                    scope_esid=esid,
                    granted_by=GRANTED_BY_ACTOR,
                    reason=GRANT_REASON,
                    expires_at=None,
                )
                if wrote:
                    created += 1
                    logger.info("  CREATED %s -> %s (%s)", username, title, esid)
                else:
                    skipped += 1
                    logger.info("  SKIPPED %s -> %s (%s) — already granted", username, title, esid)
            except Exception as e:
                failed += 1
                logger.error("  FAILED  %s -> %s (%s): %s", username, title, esid, e)

    logger.info("")
    logger.info("Bootstrap complete: created=%d skipped=%d failed=%d", created, skipped, failed)
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the grants. Default is dry-run.",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="Limit the bootstrap to a single superuser (must already be an ES Security superuser).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run(apply=args.apply, only_username=args.username)


if __name__ == "__main__":
    raise SystemExit(main())
