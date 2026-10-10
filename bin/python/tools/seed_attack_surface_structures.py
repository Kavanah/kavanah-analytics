"""Seed the attack-surface persona STRUCTURES (workspaces / orgs / teams).

Companion to ``seed_attack_surface_personas.py``. That script seeds the persona
*accounts* + scope role docs and FAILS LOUD if the structures they're scoped to
don't exist (``_resolve_scope_esid``). This script creates those structures.

WHY THIS EXISTS
---------------
The structures were originally hand-created in the Asset Explorer UI (per
tests/security/PERSONAS.md "Manual structure setup"), which has two problems:

  1. The add-form stamps the CREATOR's primary workspace, not the selected one
      — so every cross-workspace asset needs a manual ES re-stamp.
  2. They're invisible to git and have been silently dropped from staging at
     least once (2026-06-16: a team-index refresh wiped Team A1/A2/B1/B2 + the
     Acme collision team while leaving the workspaces + orgs intact).

This script makes the structure setup reproducible, idempotent, and re-runnable
after any future wipe — it writes ES docs directly with the correct
``workspace_esid`` / ``organization_esid`` already set, no UI, no re-stamp.

WHAT IT CREATES (idempotent — existing docs by stable _id are skipped)
----------------------------------------------------------------------
Workspaces (slug-as-_id, the codebase convention) — VERIFY ONLY, never created
here (they pre-exist on staging and carry settings we shouldn't fabricate):
    workspace-a, workspace-b, control-workspace
Organizations — VERIFY ONLY (resolved by title within their workspace):
    Org A (workspace-a), Org B (workspace-b),
    Control Org + Acme (control-workspace)
Teams — CREATED (stable ``as-team-*`` _ids so re-runs upsert, never duplicate):
    Team A1, Team A2  -> Org A / workspace-a
    Team B1, Team B2  -> Org B / workspace-b
    Acme -> Acme org / control-workspace

DESIGN NOTES
------------
* Stable, prefixed ``_id``s (``as-team-team-a1`` etc.) — NOT the random ES ids
  real teams use. This makes the synthetic rows trivially distinguishable from
  production data AND makes the script idempotent (re-run = upsert same id, no
  duplicate "Team A1"s). The seeder resolves teams by ``team_title`` and reads
  ``_id`` as the esid, so any unique id works.
* Team docs mirror the live clean-team shape: team_title, workspace_esid,
  workspace_title, organization_esid (LIST), organization_title (LIST),
  @timestamp. The ds_*/connector fields are optional and intentionally omitted
  (a clean team has none).
* VERIFY-ONLY for workspaces + orgs: if one is missing the script ABORTS with a
  clear message rather than fabricating it — a missing workspace/org means a
  bigger wipe than this script is scoped to repair, and silently inventing one
  (with guessed settings) would mask that.

Usage (inside imports_container, mirrors the persona seeder)::

    docker compose -f deployment/docker-compose.yml exec \
      -e PYTHONPATH=/app/python:/app/python/asset_management/web -w /app/python \
      imports_container python3 -m tools.seed_attack_surface_structures \
        --base-url https://<staging-host>          # dry run
    # then add --apply to commit.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from lib.prod_hosts import is_prod_host

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# Prod hostnames: lib.prod_hosts.
_ALLOW_PROD_ENV = "ATTACK_SURFACE_ALLOW_PROD"


class SeedError(Exception):
    """Structure-seed failure."""


def _log() -> logging.Logger:
    return logging.getLogger("seed_attack_surface_structures")


def _refuse_prod(base_url: str, prod_flag: bool) -> None:
    import os

    lower = base_url.lower()
    if not is_prod_host(lower):
        return
    if prod_flag and os.environ.get(_ALLOW_PROD_ENV) == "1":
        _log().warning("PROD opt-in present. Proceeding against %s.", base_url)
        return
    raise SeedError(
        f"Refusing to seed structures against production ({base_url}). "
        f"Pass --prod AND set {_ALLOW_PROD_ENV}=1 to override."
    )


def _get_es():
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        raise SeedError("Elasticsearch unavailable (get_elasticsearch_client returned None).")
    return es


# Workspaces are slug-as-_id, so workspace_esid == slug. VERIFY-ONLY.
_WORKSPACE_SLUGS = ("workspace-a", "workspace-b", "control-workspace")

# Orgs: (title, workspace_esid). Resolved to their real _id at runtime.
_ORGS = (
    ("Org A", "workspace-a"),
    ("Org B", "workspace-b"),
    ("Control Org", "control-workspace"),
    ("Acme", "control-workspace"),
)

# Teams to create: (team_title, org_title, workspace_esid). org_esid resolved
# from (org_title, workspace_esid) so we link to the RIGHT org when a title
# (e.g. "Acme") could collide across workspaces.
_TEAMS = (
    ("Team A1", "Org A", "workspace-a"),
    ("Team A2", "Org A", "workspace-a"),
    ("Team B1", "Org B", "workspace-b"),
    ("Team B2", "Org B", "workspace-b"),
    ("Acme", "Acme", "control-workspace"),  # same-title collision tripwire
)


def _stable_team_id(team_title: str, workspace_esid: str) -> str:
    """Recognizable, collision-free synthetic _id. Includes the workspace so the
    two 'Acme' teams (control vs a persona's own) never share an id."""
    slug = team_title.lower().replace(" ", "-")
    return f"as-team-{workspace_esid}-{slug}"


def _verify_workspace(es, slug: str) -> str:
    """Return the workspace esid (== _id) or abort. VERIFY-ONLY."""
    if not es.exists(index="de_asset_workspace", id=slug):
        raise SeedError(
            f"Workspace {slug!r} not found in de_asset_workspace. This script "
            f"repairs TEAMS only; a missing workspace means a larger wipe — "
            f"recreate it per PERSONAS.md before re-running."
        )
    return slug


def _resolve_org_id(es, org_title: str, workspace_esid: str) -> str:
    """Resolve (org_title, workspace_esid) to the org's _id, or abort.

    Title is matched on ``organization_title.keyword`` (text+keyword multi-field;
    a bare ``term`` on the analyzed field returns 0 — the recurring
    trap) AND filtered to the right workspace so a cross-workspace title
    collision (e.g. 'Acme') resolves to the intended org.
    """
    body = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"organization_title.keyword": org_title}},
                    {"term": {"workspace_esid": workspace_esid}},
                ]
            }
        },
        "size": 2,
    }
    hits = es.search(index="de_asset_organization", body=body)["hits"]["hits"]
    if not hits:
        raise SeedError(
            f"Organization {org_title!r} in workspace {workspace_esid!r} not found. "
            f"This script repairs TEAMS only; recreate the org per PERSONAS.md first."
        )
    if len(hits) > 1:
        raise SeedError(
            f"Organization {org_title!r} in workspace {workspace_esid!r} is ambiguous "
            f"({len(hits)} matches): {[h['_id'] for h in hits]}. Resolve by hand."
        )
    return hits[0]["_id"]


def _seed_team(es, *, team_title, org_title, workspace_esid, apply: bool) -> None:
    workspace_esid = _verify_workspace(es, workspace_esid)
    org_id = _resolve_org_id(es, org_title, workspace_esid)
    team_id = _stable_team_id(team_title, workspace_esid)

    if es.exists(index="de_asset_teams", id=team_id):
        _log().info("SKIP team %r (%s already exists)", team_title, team_id)
        return

    # Mirror the live clean-team shape. organization_esid/title are LISTS
    # (the form's multi_select shape, confirmed from a real team doc).
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "team_title": team_title,
        "workspace_esid": workspace_esid,
        "workspace_title": workspace_esid,  # cosmetic; real teams carry the title
        "organization_esid": [org_id],
        "organization_title": [org_title],
        "@timestamp": now,
    }
    if not apply:
        _log().info("[DRY] would CREATE team %r -> %s (org=%s, ws=%s)", team_title, team_id, org_id, workspace_esid)
        return
    es.index(index="de_asset_teams", id=team_id, document=doc)
    es.indices.refresh(index="de_asset_teams")
    _log().info("CREATED team %r -> %s (org=%s, ws=%s)", team_title, team_id, org_id, workspace_esid)


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed attack-surface persona structures (teams).")
    parser.add_argument("--base-url", required=True, help="Target base URL (prod guard).")
    parser.add_argument("--apply", action="store_true", help="Actually write. Default is a dry run.")
    parser.add_argument("--prod", action="store_true", help="Deliberate prod opt-in (with ATTACK_SURFACE_ALLOW_PROD=1).")
    args = parser.parse_args()

    log = _log()
    if not args.apply:
        log.info("=== DRY RUN (no writes). Re-run with --apply to commit. ===")

    try:
        _refuse_prod(args.base_url, args.prod)
        es = _get_es()

        # VERIFY-ONLY: workspaces + orgs must already exist.
        for slug in _WORKSPACE_SLUGS:
            _verify_workspace(es, slug)
            log.info("OK workspace %r", slug)
        for org_title, ws in _ORGS:
            oid = _resolve_org_id(es, org_title, ws)
            log.info("OK org %r in %s -> %s", org_title, ws, oid)

        # CREATE the teams.
        for team_title, org_title, ws in _TEAMS:
            _seed_team(es, team_title=team_title, org_title=org_title, workspace_esid=ws, apply=args.apply)

    except SeedError as e:
        log.error("ABORTED: %s", e)
        return 1

    log.info("Done.%s", "" if args.apply else "  (dry run — nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
