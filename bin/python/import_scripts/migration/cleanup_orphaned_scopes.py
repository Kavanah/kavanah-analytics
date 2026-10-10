"""one-shot cleanup of debris left by team/org deletes.

Two debris classes, one sweep:

1. **Stale authz docs.** Deleting a team/org never cascaded into
   the authz tier, so ``de_authz_user_team_role`` / ``de_authz_user_org_role``
   / ``de_authz_team_membership`` / ``de_authz_org_membership`` docs — and
   ``de_authz_data_grant`` docs with a team/org scope — can name scopes whose
   asset doc no longer exists. Their titles flow into the UserScope resolver's
   grant + membership legs forever (tree badge counts, get_allowed_teams_*).
   This sweep deletes them, EXCLUDING the ``__all__`` system-admin sentinel
   (``role_filter_service.ALL_SCOPES_SENTINEL`` — those docs intentionally
   name no real team and must never be swept).

2. **Orphaned teams.** The old org-delete cascade emptied each
   child team's ``organization_esid`` instead of refusing, leaving parentless
   teams that stay in importer scope (``ds_facebook=true``) and pollute the
   continuity report (an orphaned team, prod team 4VNKAZwBIm2AdJKuKErz).
   Orphans are ALWAYS reported; they are only deactivated when explicitly
   named via ``--deactivate-teams`` — an org-less team is not proof of a bad
   delete (the internal Kavanah Team is legitimately org-less), so the
   operator names the ones to deactivate. Deactivation sets every ``ds_*``
   flag to ``"false"`` and ``team_fb_integration_status`` to ``"Retired"``
   (the exclusion value — precedent from an earlier team), keeping the team doc and
   its data indices intact.

**Safety.** DRY-RUN BY DEFAULT; ``--apply`` must be an explicit true value
(the contract). Every deleted doc's full ``_source`` is logged
before deletion so the state is recoverable from the import log. Any
enumeration failure refuses (fail closed, exit non-zero) — a partial view of
live scopes must never widen the stale set. Going forward the web app's
delete path prevents new debris (refuse-gate + cascade in
app.py / permissions_index_service.cascade_purge_authz_for_scope).

Execution:
    # Dry-run (default) — report only.
    python3 /app/python/import.py migration cleanup_orphaned_scopes
    # Apply the authz sweep.
    python3 /app/python/import.py migration cleanup_orphaned_scopes --apply
    # Apply + deactivate named orphaned teams.
    python3 /app/python/import.py migration cleanup_orphaned_scopes --apply --deactivate-teams=ESID1,ESID2
"""

import logging

import lib.logging
from lib.es_helpers import get_elasticsearch_client
from migration.snapshot_gate import (  # the shared parse replaces the inline copy
    MigrationRefused,
    resolve_apply_mode,
)

# Mirrors role_filter_service.ALL_SCOPES_SENTINEL — defined locally so this
# importer-container script doesn't drag in the web-app import chain. The
# staging probe (2026-09-02) found two system_admin principals' docs
# with team_esid="__all__": a naive "not a live team" sweep would strip
# system-admin scope. Never delete sentinel-scoped docs.
_SENTINEL_SCOPES = {"__all__"}

_DS_FLAGS = ("ds_facebook", "ds_instagram", "ds_chatwoot", "ds_google_analytics", "ds_google_ads")
_RETIRED_STATUS = "Retired"

# (index, esid_field, live_set_name) legs for the stale-authz sweep.
_AUTHZ_LEGS = (
    ("de_authz_user_team_role", "team_esid", "teams"),
    ("de_authz_team_membership", "team_esid", "teams"),
    ("de_authz_user_org_role", "org_esid", "orgs"),
    ("de_authz_org_membership", "org_esid", "orgs"),
)


def _search_all(es, index, body):
    """Search with a fixed 10k window (all these indices are far smaller).
    Raises on failure — every caller must fail CLOSED."""
    resp = es.search(index=index, body=dict(body, size=10000))
    return resp.get("hits", {}).get("hits", [])


def _live_ids(es, index):
    return {h["_id"] for h in _search_all(es, index, {"query": {"match_all": {}}, "_source": False})}


def _collect_stale_authz(es, live_teams, live_orgs):
    """Return [(index, doc_id, source), ...] for authz docs naming a
    nonexistent (non-sentinel) team/org scope."""
    live_by_name = {"teams": live_teams, "orgs": live_orgs}
    stale = []
    for index, field, live_name in _AUTHZ_LEGS:
        try:
            hits = _search_all(es, index, {"query": {"match_all": {}}})
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                continue  # pre-F4 envs
            raise
        live = live_by_name[live_name]
        for h in hits:
            esid = (h.get("_source") or {}).get(field)
            if esid and esid not in _SENTINEL_SCOPES and esid not in live:
                stale.append((index, h["_id"], h.get("_source") or {}))

    try:
        grant_hits = _search_all(
            es,
            "de_authz_data_grant",
            {"query": {"terms": {"scope_type": ["team", "org"]}}},
        )
    except Exception as e:
        err = str(e).lower()
        grant_hits = [] if ("index_not_found" in err or "404" in err) else None
        if grant_hits is None:
            raise
    for h in grant_hits:
        src = h.get("_source") or {}
        esid = src.get("scope_esid")
        live = live_teams if src.get("scope_type") == "team" else live_orgs
        if esid and esid not in _SENTINEL_SCOPES and esid not in live:
            stale.append(("de_authz_data_grant", h["_id"], src))
    return stale


def _collect_orphan_teams(es, live_orgs):
    """Return [{esid, title, dangling, active_ds, status}, ...] for teams
    whose organization_esid is empty or names no live org."""
    orphans = []
    hits = _search_all(
        es,
        "de_asset_teams",
        {
            "query": {"match_all": {}},
            "_source": ["team_title", "organization_esid", "team_fb_integration_status", *_DS_FLAGS],
        },
    )
    for h in hits:
        src = h.get("_source") or {}
        orgs = src.get("organization_esid")
        orgs = orgs if isinstance(orgs, list) else ([orgs] if orgs else [])
        orgs = [o for o in orgs if o]
        dangling = [o for o in orgs if o not in live_orgs]
        if orgs and not dangling:
            continue
        orphans.append(
            {
                "esid": h["_id"],
                "title": src.get("team_title", ""),
                "dangling": dangling,
                "active_ds": [f for f in _DS_FLAGS if str(src.get(f)).lower() == "true"],
                "status": src.get("team_fb_integration_status", ""),
            }
        )
    return orphans


def main(params):
    lib.logging.define("cleanup-orphaned-scopes")

    # contract via the shared dry-run by default;
    # --apply must be an EXPLICIT true value; contradictory flags refuse.
    try:
        dry_run, _ = resolve_apply_mode(params)
    except MigrationRefused as e:
        logging.error("Refusing: %s", e)
        return 1
    if dry_run:
        logging.info("DRY-RUN: nothing will be deleted or deactivated. Pass --apply to mutate.")

    deactivate_raw = params.get("deactivate-teams") or params.get("deactivate_teams") or ""
    deactivate_esids = [e.strip() for e in str(deactivate_raw).split(",") if e.strip()]

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return 1

    # Fail CLOSED: any enumeration error refuses the whole run — a partial
    # live-scope view would misclassify live docs as stale.
    try:
        live_teams = _live_ids(es, "de_asset_teams")
        live_orgs = _live_ids(es, "de_asset_organization")
        stale = _collect_stale_authz(es, live_teams, live_orgs)
        orphans = _collect_orphan_teams(es, live_orgs)
    except Exception as e:  # noqa: BLE001 — refusal boundary
        logging.error("Refusing: enumeration failed (%s). Re-run when ES is healthy.", e)
        return 1

    logging.info("Live scopes: %d teams, %d orgs.", len(live_teams), len(live_orgs))

    # --- Part A: stale authz docs ----
    logging.info("Stale authz docs (excluding %s sentinels): %d", sorted(_SENTINEL_SCOPES), len(stale))
    deleted = 0
    for index, doc_id, src in stale:
        if dry_run:
            logging.info("DRY-RUN would delete %s/%s  source=%s", index, doc_id, src)
            continue
        # Full source logged BEFORE the delete so the doc is recoverable
        # from the import log if a sweep decision is ever questioned.
        logging.info("Deleting %s/%s  source=%s", index, doc_id, src)
        try:
            es.delete(index=index, id=doc_id, refresh=True)
            deleted += 1
        except Exception as e:
            err = str(e).lower()
            if "not_found" in err or "404" in err:
                continue  # already gone — idempotent re-run
            logging.error("Delete failed for %s/%s: %s", index, doc_id, e)

    # --- Part B: orphaned teams — report always ----
    logging.info("Org-less / dangling-org teams: %d", len(orphans))
    orphan_by_esid = {o["esid"]: o for o in orphans}
    for o in orphans:
        marked = " [WILL DEACTIVATE]" if o["esid"] in deactivate_esids else ""
        logging.info(
            "  %s %r dangling=%s active_ds=%s status=%r%s",
            o["esid"],
            o["title"],
            o["dangling"] or "-",
            o["active_ds"] or "-",
            o["status"],
            marked,
        )

    unknown = [e for e in deactivate_esids if e not in orphan_by_esid]
    if unknown:
        # Refuse the whole run: --deactivate-teams may only name teams this
        # sweep itself classified as orphaned — never an arbitrary team.
        logging.error("Refusing: --deactivate-teams names non-orphaned or unknown team(s): %s", unknown)
        return 1

    deactivated = 0
    for esid in deactivate_esids:
        if dry_run:
            logging.info(
                "DRY-RUN would deactivate team %s (%r): ds_*=false, status=%s",
                esid,
                orphan_by_esid[esid]["title"],
                _RETIRED_STATUS,
            )
            continue
        doc = {f: "false" for f in _DS_FLAGS}
        doc["team_fb_integration_status"] = _RETIRED_STATUS
        try:
            es.update(index="de_asset_teams", id=esid, body={"doc": doc}, refresh=True)
            deactivated += 1
            logging.info(
                "Deactivated team %s (%r): ds_*=false, status=%s", esid, orphan_by_esid[esid]["title"], _RETIRED_STATUS
            )
        except Exception as e:
            logging.error("Deactivation failed for team %s: %s", esid, e)

    logging.info(
        "Cleanup complete%s: %d stale authz doc(s) %s, %d team(s) deactivated, %d orphan team(s) reported.",
        " (dry-run)" if dry_run else "",
        len(stale),
        "would be deleted" if dry_run else f"deleted ({deleted} removed)",
        deactivated,
        len(orphans),
    )
    return {
        "dry_run": dry_run,
        "stale_authz": len(stale),
        "deleted": deleted,
        "orphan_teams": [o["esid"] for o in orphans],
        "deactivated": deactivated,
    }


if __name__ == "__main__":
    # Direct execution parses no flags — always a report-only dry-run.
    main({})
