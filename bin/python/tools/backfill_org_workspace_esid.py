"""One-shot backfill: point every existing org at the workspace tier.

F5 of the spike's migration plan. F4 created the
``de_asset_workspace`` index and wrote the Kavanah singleton. F5
populates the two new fields on every existing ``de_asset_organization``
doc:

* ``workspace_esid`` — points at the Kavanah singleton (default
  ``--workspace-slug kavanah-media``). Required for F6+ tenant-scoped
  queries.
* ``organization_slug`` — derived from ``organization_title`` via
  ``WorkspaceSlugService.slugify``. Becomes the natural per-workspace
  uniqueness key for the rescoped slug check.

The script is **dry-run by default**. Use ``--apply`` to write.

Collision detection runs before any write. Two existing org titles that
slugify to the same value would, after the backfill, collide within the
singleton workspace's namespace. The script halts with a diagnostic
listing every colliding pair so the operator can rename one side or
re-run with ``--auto-suffix`` to apply ``acme``, ``acme-2``, ... per the
documented Slack pattern. ``--auto-suffix`` is disabled by default —
silently rewriting a tenant's organization slug is the W3 pattern the
 spike explicitly rejected, so we make the operator opt in.

Idempotency: each org doc gets a partial update via ``es.update``. A doc
that already has BOTH ``workspace_esid`` and ``organization_slug``
populated is skipped (the update payload is empty after the diff). Two
consecutive ``--apply`` runs produce ``created=0 skipped=N`` on the
second pass — no churn, no spurious version bumps.

Usage::

    # Dry-run (default) — prints what would be written, no writes.
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_org_workspace_esid

    # Apply against the Kavanah singleton (default).
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_org_workspace_esid --apply

    # Apply against a different workspace (rare; mainly for test setups).
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_org_workspace_esid --apply --workspace-slug my-workspace

    # If a collision halt fires, re-run with --auto-suffix to apply
    # acme/acme-2/... — operator opt-in only.
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_org_workspace_esid --apply --auto-suffix
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as bootstrap_workspaces.py.
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
    WORKSPACE_INDEX,
)
from asset_management.web.services.workspace_slug_service import slugify  # noqa: E402
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_SLUG = "kavanah-media"


def fetch_workspace_esid(es, *, workspace_slug: str) -> str | None:
    """Resolve a workspace slug to its ES _id.

    Returns None if no workspace matches; the run() entrypoint surfaces
    a hard error in that case so the operator can re-check the slug or
    re-run the F4 bootstrap.
    """
    try:
        resp = es.get(index=WORKSPACE_INDEX, id=workspace_slug)
    except Exception as e:
        if "not_found" in str(e).lower() or "404" in str(e):
            return None
        raise
    return resp.get("_id")


def fetch_all_orgs(es) -> list[dict]:
    """Return every existing org doc as ``{esid, title, workspace_esid, slug}``.

    Reads the full set in one search call — beta org count is small
    enough that a 10000-cap match-all is fine. F5 will not run at a
    scale where pagination matters; that's a later cleanup if needed.
    """
    try:
        resp = es.search(
            index=ORGANIZATION_INDEX,
            body={"query": {"match_all": {}}, "size": 10000},
        )
    except Exception as e:
        logger.error("Could not search %s: %s", ORGANIZATION_INDEX, e)
        return []

    orgs: list[dict] = []
    for hit in resp.get("hits", {}).get("hits", []):
        source = hit.get("_source") or {}
        orgs.append(
            {
                "esid": hit["_id"],
                "title": source.get("organization_title") or "",
                "workspace_esid": source.get("workspace_esid"),
                "organization_slug": source.get("organization_slug"),
            }
        )
    return sorted(orgs, key=lambda o: o["title"].lower())


def plan_assignments(
    orgs: list[dict],
    *,
    target_workspace_esid: str,
    auto_suffix: bool,
) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Compute ``{esid: {workspace_esid, organization_slug}}`` per org.

    Returns ``(plan, collisions)``. ``collisions`` is non-empty when two
    orgs slugify to the same value AND ``auto_suffix=False`` — caller
    halts in that case. With ``auto_suffix=True`` the second occurrence
    becomes ``slug-2``, third ``slug-3``, etc.; ``collisions`` returns
    empty.

    Orgs that already have BOTH workspace_esid and organization_slug
    populated keep their existing values (idempotent re-run).
    """
    plan: dict[str, dict] = {}
    by_slug: dict[str, list[str]] = defaultdict(list)
    collisions: dict[str, list[str]] = {}

    # First pass: derive the proposed slug for each org.
    proposals: list[tuple[str, str, str]] = []  # (esid, title, base_slug)
    for org in orgs:
        base_slug = slugify(org["title"]) or "untitled"
        proposals.append((org["esid"], org["title"], base_slug))

    # Second pass: detect collisions, optionally auto-suffix.
    assigned_slugs: set[str] = set()
    # Seed assigned_slugs with anything that's already set on an org
    # (we don't want to overwrite a previously-backfilled slug or
    # let a new derivation collide with an existing one).
    for org in orgs:
        if org["organization_slug"]:
            assigned_slugs.add(org["organization_slug"])

    for esid, title, base_slug in proposals:
        existing_org = next(o for o in orgs if o["esid"] == esid)
        # Idempotent path: doc already has both fields — keep them.
        if existing_org["workspace_esid"] and existing_org["organization_slug"]:
            plan[esid] = {
                "workspace_esid": existing_org["workspace_esid"],
                "organization_slug": existing_org["organization_slug"],
                "_already_done": True,
            }
            by_slug[existing_org["organization_slug"]].append(esid)
            continue

        chosen = base_slug
        if chosen in assigned_slugs:
            if auto_suffix:
                n = 2
                while f"{base_slug}-{n}" in assigned_slugs:
                    n += 1
                chosen = f"{base_slug}-{n}"
            else:
                collisions.setdefault(base_slug, []).append(esid)
                # still record a tentative plan so the dry-run output is
                # complete; the apply path halts before executing it.
        assigned_slugs.add(chosen)
        plan[esid] = {
            "workspace_esid": target_workspace_esid,
            "organization_slug": chosen,
            "_already_done": False,
        }
        by_slug[chosen].append(esid)

    # Strip the working set if every collision was resolved by
    # auto-suffix (collisions only records cases where suffix wasn't
    # applied).
    return plan, dict(collisions)


def apply_plan(es, plan: dict[str, dict]) -> tuple[int, int, int]:
    """Execute the plan via es.update. Returns (updated, skipped, failed)."""
    updated = 0
    skipped = 0
    failed = 0
    for esid, fields in plan.items():
        if fields.get("_already_done"):
            skipped += 1
            logger.info("  SKIPPED %s — workspace_esid + slug already set", esid)
            continue
        body = {
            "workspace_esid": fields["workspace_esid"],
            "organization_slug": fields["organization_slug"],
        }
        try:
            es.update(
                index=ORGANIZATION_INDEX,
                id=esid,
                doc=body,
                refresh="wait_for",
            )
            updated += 1
            logger.info(
                "  UPDATED %s -> workspace=%s slug=%s",
                esid,
                fields["workspace_esid"],
                fields["organization_slug"],
            )
        except Exception as e:
            failed += 1
            logger.error("  FAILED %s: %s", esid, e)
    return updated, skipped, failed


def run(*, apply: bool, workspace_slug: str, auto_suffix: bool) -> int:
    """Plan and (optionally) apply the backfill. Returns the exit code."""
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    target_workspace_esid = fetch_workspace_esid(es, workspace_slug=workspace_slug)
    if target_workspace_esid is None:
        logger.error(
            "Workspace slug %r not found in %s. Re-run tools.bootstrap_workspaces --apply first.",
            workspace_slug,
            WORKSPACE_INDEX,
        )
        return 2

    orgs = fetch_all_orgs(es)
    if not orgs:
        logger.info("No orgs in %s; nothing to do.", ORGANIZATION_INDEX)
        return 0

    plan, collisions = plan_assignments(
        orgs,
        target_workspace_esid=target_workspace_esid,
        auto_suffix=auto_suffix,
    )

    logger.info("Backfill plan (workspace_esid=%s, %d orgs):", target_workspace_esid, len(orgs))
    for org in orgs:
        fields = plan[org["esid"]]
        marker = "(noop)" if fields.get("_already_done") else ""
        logger.info(
            "  %s | title=%r -> workspace=%s slug=%s %s",
            org["esid"],
            org["title"],
            fields["workspace_esid"],
            fields["organization_slug"],
            marker,
        )

    if collisions:
        logger.error("")
        logger.error("HALT: %d slug collision(s) detected within target workspace.", len(collisions))
        for slug, esids in collisions.items():
            logger.error("  slug %r is claimed by multiple orgs: %s", slug, esids)
            for esid in esids:
                title = next(o["title"] for o in orgs if o["esid"] == esid)
                logger.error("    - %s (%s) -> would slugify to %r", esid, title, slug)
        logger.error("")
        logger.error("Resolutions:")
        logger.error("  (a) rename one side via Asset Explorer, then re-run.")
        logger.error("  (b) re-run with --auto-suffix to apply slug, slug-2, ... per the W1 pattern.")
        return 2

    if not apply:
        logger.info("")
        logger.info("DRY-RUN: no writes. Pass --apply to backfill for real.")
        return 0

    logger.info("")
    logger.info("APPLY: writing %d org updates...", len(plan))
    updated, skipped, failed = apply_plan(es, plan)

    logger.info("")
    logger.info("Backfill complete: updated=%d skipped=%d failed=%d", updated, skipped, failed)
    if failed:
        return 1

    # Verification: query the count of orgs MISSING workspace_esid.
    # Acceptance criterion: this must be zero after --apply.
    try:
        missing = es.count(
            index=ORGANIZATION_INDEX,
            body={"query": {"bool": {"must_not": {"exists": {"field": "workspace_esid"}}}}},
        )
        missing_count = missing.get("count", 0)
        logger.info("Verification: orgs without workspace_esid = %d (expected 0)", missing_count)
        if missing_count > 0:
            logger.error("Verification FAILED: %d orgs still missing workspace_esid.", missing_count)
            return 1
    except Exception as e:
        logger.warning("Verification query failed: %s", e)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the org updates. Default is dry-run.",
    )
    parser.add_argument(
        "--workspace-slug",
        default=DEFAULT_WORKSPACE_SLUG,
        help=f"Target workspace slug. Default: {DEFAULT_WORKSPACE_SLUG!r}.",
    )
    parser.add_argument(
        "--auto-suffix",
        action="store_true",
        help=(
            "Apply slug, slug-2, slug-3, ... when two orgs would otherwise "
            "collide. Default: halt and surface the collisions for manual review."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run(
        apply=args.apply,
        workspace_slug=args.workspace_slug,
        auto_suffix=args.auto_suffix,
    )


if __name__ == "__main__":
    raise SystemExit(main())
