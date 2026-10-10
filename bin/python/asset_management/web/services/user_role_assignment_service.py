"""User Role Assignment Service

Per-user CRUD for the System 2 role assignment indices:

* ``de_authz_user_workspace_role`` — workspace-tier admin bundles
* ``de_authz_user_org_role`` — org-tier bundles
* ``de_authz_user_team_role`` — team-tier bundles

Existing surfaces:

* ``_provision_new_authz_assignments`` writes assignments on
  user creation from membership strings.
* v3's ``_sync_new_authz_memberships`` diffs the Edit Roles
  modal's bulk memberships array against the user's current state.
  Hard-codes ``viewer`` on delete (line 5623 in app.py) so it can only
  remove default-role assignments — anything else (a workspace_admin,
  an org_admin) survives the diff.
* Phase 15's ``api_get_authz_memberships`` reads team + org
  assignments for the Edit Roles modal's "Current memberships"
  read-only summary. Does NOT include workspace.

This service is the per-assignment CRUD needs:

* ``list_for_user`` returns a flat union across all three tiers,
  joined to asset titles + role names, so the Edit Roles modal can
  render the Scoped roles section.
* ``assign`` writes a single ``<username>:<scope_esid>:<role_esid>``
  doc with op_type=create (409-as-success = idempotent).
* ``revoke`` deletes one doc by id.

Validation lives at the route boundary in app.py (it consumes the
same ``_AUTHZ_TIER_BY_PREFIX`` / ``_validate_role_for_tier`` helpers
from single source of truth for tier↔role bundle pairing).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client

# Tier metadata. Mirrors app.py:_AUTHZ_TIER_BY_PREFIX but keyed by
# scope_type (workspace/org/team) so the API route can dispatch
# without re-parsing membership strings.
_TIER_BY_SCOPE_TYPE: dict[str, dict] = {
    "workspace": {
        "assignment_index": "de_authz_user_workspace_role",
        "membership_index": "de_authz_workspace_membership",
        "scope_esid_field": "workspace_esid",
        "asset_index": "de_asset_workspace",
        "asset_title_field": "workspace_title",
    },
    "org": {
        "assignment_index": "de_authz_user_org_role",
        "membership_index": "de_authz_org_membership",
        "scope_esid_field": "org_esid",
        "asset_index": "de_asset_organization",
        "asset_title_field": "organization_title",
    },
    "team": {
        "assignment_index": "de_authz_user_team_role",
        "membership_index": "de_authz_team_membership",
        "scope_esid_field": "team_esid",
        "asset_index": "de_asset_teams",
        "asset_title_field": "team_title",
    },
}


SUPPORTED_SCOPE_TYPES: tuple[str, ...] = tuple(_TIER_BY_SCOPE_TYPE.keys())


@dataclass(frozen=True)
class Assignment:
    """One row in the Scoped roles section."""

    scope_type: str  # "workspace" | "org" | "team"
    scope_esid: str
    scope_title: str
    role_esid: str
    role_name: str


class UserRoleAssignmentService:
    """Per-user CRUD on the three System 2 role-assignment indices."""

    # ─────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_role_name(es, role_esid: str, cache: dict[str, str]) -> str:
        """Look up ``de_authz_role.name`` for a role esid, falling back
        to the esid string. Cached per call.
        """
        if role_esid in cache:
            return cache[role_esid]
        try:
            doc = es.get(index="de_authz_role", id=role_esid)
            name = doc.get("_source", {}).get("name") or role_esid
        except Exception:
            name = role_esid
        cache[role_esid] = name
        return name

    @staticmethod
    def _resolve_scope_title(
        es, asset_index: str, asset_title_field: str, scope_esid: str, cache: dict[tuple[str, str], str]
    ) -> str:
        """Look up the human-readable title for a scope esid. Cached.
        Sentinel ``__all__`` (system-admin team sentinel) maps to the
        existing friendly label so the Scoped roles modal matches the
        legacy memberships panel.
        """
        if scope_esid == "__all__":
            return "All teams (system admin)"
        key = (asset_index, scope_esid)
        if key in cache:
            return cache[key]
        try:
            doc = es.get(index=asset_index, id=scope_esid)
            title = doc.get("_source", {}).get(asset_title_field) or scope_esid
        except Exception:
            title = scope_esid
        cache[key] = title
        return title

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def list_for_user(username: str) -> list[Assignment]:
        """Return every System 2 role assignment for ``username`` across
        the three tier indices. Ordered: workspace, then org, then team
        (matches the hierarchy the modal renders).

        Best-effort per tier — a failure on one index logs a warning and
        skips that tier rather than returning a partial-with-error
        state. The Edit Roles modal can still surface the rest.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("UserRoleAssignmentService: ES unavailable for %s", username)
            return []

        role_name_cache: dict[str, str] = {}
        scope_title_cache: dict[tuple[str, str], str] = {}

        out: list[Assignment] = []
        for scope_type in SUPPORTED_SCOPE_TYPES:
            tier = _TIER_BY_SCOPE_TYPE[scope_type]
            try:
                hits = es.search(
                    index=tier["assignment_index"],
                    body={"query": {"term": {"username": username}}, "size": 500},
                )
            except Exception as e:
                err_text = str(e).lower()
                if "index_not_found" in err_text or "404" in err_text:
                    continue  # workspace index missing on this cluster — acceptable
                logging.warning(
                    "UserRoleAssignmentService: read failed on %s for %s: %s",
                    tier["assignment_index"],
                    username,
                    e,
                )
                continue
            for hit in hits.get("hits", {}).get("hits", []):
                src = hit.get("_source", {}) or {}
                scope_esid = src.get(tier["scope_esid_field"])
                role_esid = src.get("role_esid")
                if not scope_esid or not role_esid:
                    continue
                title = UserRoleAssignmentService._resolve_scope_title(
                    es,
                    tier["asset_index"],
                    tier["asset_title_field"],
                    scope_esid,
                    scope_title_cache,
                )
                role_name = UserRoleAssignmentService._resolve_role_name(es, role_esid, role_name_cache)
                out.append(
                    Assignment(
                        scope_type=scope_type,
                        scope_esid=scope_esid,
                        scope_title=title,
                        role_esid=role_esid,
                        role_name=role_name,
                    )
                )
        return out

    @staticmethod
    def _resolve_org_workspace_esid(es, scope_type: str, scope_esid: str) -> str | None:
        """Resolve the ORG (top-entity, == workspace) esid for a scope.

        A ``workspace`` scope IS the org. An ``org``/``team`` scope walks up to
        its workspace via the shared role-filter helper. None if unresolvable.
        """
        if scope_type == "workspace":
            return scope_esid
        try:
            from asset_management.web.services.role_filter_service import (
                _workspace_esids_from_scope_esids,
            )

            org_ids = frozenset([scope_esid]) if scope_type == "org" else frozenset()
            team_ids = frozenset([scope_esid]) if scope_type == "team" else frozenset()
            ws = _workspace_esids_from_scope_esids(org_ids, team_ids)
            return next(iter(ws), None) if ws else None
        except Exception as e:  # noqa: BLE001
            logging.warning("cap: workspace resolve failed for %s/%s: %s", scope_type, scope_esid, e)
            return None

    @staticmethod
    def _org_cap_guard(es, username: str, scope_type: str, scope_esid: str) -> dict | None:
        """Return a 409 error dict if granting ``username`` a role on this scope
        would exceed the org's 5-user cap, else None.

        Block-new-only + already-a-member-doesn't-count: an over-cap result only
        blocks when ``username`` is NOT already one of the org's members (so an
        existing member gaining a second role is never blocked, and an already-
        over-cap org can still re-role its existing people). Comped orgs are
        exempt. Fail-CLOSED on a count error.
        """
        from asset_management.web.services import permissions_index_service as _pix

        workspace_esid = UserRoleAssignmentService._resolve_org_workspace_esid(es, scope_type, scope_esid)
        if not workspace_esid:
            # Can't resolve the org → can't enforce a per-org cap. Don't block
            # (the scope already validated as a real asset); log for visibility.
            logging.warning("cap: unresolved org for %s/%s; skipping cap", scope_type, scope_esid)
            return None

        # Comped orgs are exempt (complimentary access → no seat cap).
        try:
            from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

            if WorkspaceBillingService.get_workspace_state(workspace_esid).get("is_comped"):
                return None
        except Exception as e:  # noqa: BLE001
            # A billing-state blip must not silently disable the cap; fall
            # through to the count (fail-closed posture).
            logging.warning("cap: comp check failed for %s: %s", workspace_esid, e)

        count = _pix.count_org_members(es, workspace_esid)
        if count < 0:
            return {
                "success": False,
                "status": 503,
                "error": "Couldn't verify the organization's member count. Please try again.",
            }
        if count < _pix.ORG_USER_CAP:
            return None  # room for a new member regardless of who they are

        # At/over cap — allow ONLY if this user is already a member (re-roling
        # an existing person doesn't add a seat).
        already_member = UserRoleAssignmentService._is_org_member(es, username, workspace_esid)
        if already_member:
            return None
        return {
            "success": False,
            "status": 409,
            "error": (
                f"This organization has reached its {_pix.ORG_USER_CAP}-user limit. "
                "Remove a member or contact sales to add more users."
            ),
        }

    @staticmethod
    def _is_org_member(es, username: str, workspace_esid: str) -> bool:
        """True iff ``username`` already holds a role at ANY tier under the org
        (workspace/org/team). Mirrors count_org_members' tier union, scoped to
        one user. Fail-CLOSED (treat as NOT-member on error → the cap errs
        toward blocking, never toward over-filling)."""
        from asset_management.web.services import permissions_index_service as _pix

        try:
            # Workspace tier — direct.
            resp = es.search(
                index=_pix.USER_WORKSPACE_ROLE_INDEX,
                body={
                    "size": 0,
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"workspace_esid": workspace_esid}},
                                {"term": {"username": username}},
                            ]
                        }
                    },
                },
            )
            if resp.get("hits", {}).get("total", {}).get("value", 0):
                return True
            # Org/team tiers — child esids of this workspace.
            child_org_esids: list[str] = []
            child_team_esids: list[str] = []
            for index, field, sink in (
                (_pix.ORGANIZATION_INDEX, "workspace_esid", child_org_esids),
                # de_asset_teams.workspace_esid reindexed to keyword;
                # bare field works again.
                ("de_asset_teams", "workspace_esid", child_team_esids),
            ):
                try:
                    r = es.search(
                        index=index, body={"query": {"term": {field: workspace_esid}}, "_source": False, "size": 10000}
                    )
                    sink.extend(h["_id"] for h in r.get("hits", {}).get("hits", []))
                except Exception:  # noqa: BLE001
                    continue
            for index, field, ids in (
                (_pix.USER_ORG_ROLE_INDEX, "org_esid", child_org_esids),
                (_pix.USER_TEAM_ROLE_INDEX, "team_esid", child_team_esids),
            ):
                if not ids:
                    continue
                r = es.search(
                    index=index,
                    body={
                        "size": 0,
                        "query": {
                            "bool": {
                                "filter": [
                                    {"terms": {field: ids}},
                                    {"term": {"username": username}},
                                ]
                            }
                        },
                    },
                )
                if r.get("hits", {}).get("total", {}).get("value", 0):
                    return True
        except Exception as e:  # noqa: BLE001
            logging.warning("cap: membership check failed for %s/%s: %s", username, workspace_esid, e)
            return False
        return False

    @staticmethod
    def assign(*, username: str, scope_type: str, scope_esid: str, role_esid: str, bypass_cap: bool = False) -> dict:
        """Create or replace a role assignment for ``username``.

        Writes the membership doc (idempotent) AND the assignment doc.
        Doc ID convention matches ``_provision_new_authz_assignments``:
        ``<username>:<scope_esid>`` for membership, ``<username>:<scope_esid>:<role_esid>``
        for assignment. ``op_type=create`` + 409-as-success keeps re-runs
        no-ops.

        ``bypass_cap`` skips the item-8 org user cap (the route sets it
        for a superuser actor — the cap is for org admins, not platform admins).

        Returns ``{"success": True, "scope_type", "scope_esid", "role_esid"}``
        or ``{"success": False, "error": "..."}`` (with ``status`` for the
        route to surface as the HTTP code).
        """
        if scope_type not in SUPPORTED_SCOPE_TYPES:
            return {"success": False, "status": 400, "error": f"Unsupported scope_type: {scope_type}"}
        if not username or not scope_esid or not role_esid:
            return {"success": False, "status": 400, "error": "username, scope_esid, role_esid are required"}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "status": 503, "error": "Elasticsearch unavailable"}

        tier = _TIER_BY_SCOPE_TYPE[scope_type]

        # Confirm the scope exists. A POST referencing a stale or fake
        # scope_esid would silently land a dangling assignment with no
        # backing asset — surface that as a 400.
        try:
            es.get(index=tier["asset_index"], id=scope_esid)
        except Exception:
            return {
                "success": False,
                "status": 400,
                "error": f"{scope_type} {scope_esid!r} not found in {tier['asset_index']}",
            }

        # item 8 — hard 5-user cap per organization. Enforced here (the
        # shared admin grant chokepoint) BEFORE the write. Block-new-only: only
        # refuses when adding a NOT-yet-member would push a not-yet-over org
        # past the cap (an existing member gaining a second role, or an already-
        # over-cap org's existing members, are unaffected). Superuser actor
        # (bypass_cap) and fully-comped orgs are exempt. Fail-CLOSED on a count
        # error (refuse rather than silently allow an over-cap grant).
        if not bypass_cap:
            cap_err = UserRoleAssignmentService._org_cap_guard(es, username, scope_type, scope_esid)
            if cap_err is not None:
                return cap_err

        now_iso = datetime.now(timezone.utc).isoformat()
        assignment_doc = {
            "username": username,
            tier["scope_esid_field"]: scope_esid,
            "role_esid": role_esid,
            # the role doc carries is_primary (the signal the retired
            # membership doc used to own). An admin-assigned role is
            # non-primary — the user's onboarding-created workspace stays
            # primary.
            "is_primary": False,
            "created_at": now_iso,
        }

        # (P3b): membership docs are no longer written. The role doc
        # is the single source of truth; readers resolve "belongs to" from it.

        # Assignment write. ``refresh="wait_for"`` blocks until the next ES
        # refresh so a follow-on
        # ``/api/user-management/role-assignments/<user>`` GET (which the Edit
        # Roles modal fires immediately after a successful POST) sees the new
        # doc. If the user already has the SAME role
        # on this scope, this is a no-op (409). If they had a DIFFERENT
        # role, that one stays — the caller is expected to revoke the
        # previous role first via the role-change flow. Multiple roles
        # on the same scope are not a supported state in the UI; the
        # resolver picks the most permissive role today, but the policy
        # is "one role per scope per user."
        try:
            es.index(
                index=tier["assignment_index"],
                id=f"{username}:{scope_esid}:{role_esid}",
                document=assignment_doc,
                op_type="create",
                refresh="wait_for",
            )
        except Exception as e:
            if "version_conflict" not in str(e).lower():
                logging.error(
                    "UserRoleAssignmentService: assignment write failed for %s/%s/%s/%s: %s",
                    username,
                    scope_type,
                    scope_esid,
                    role_esid,
                    e,
                )
                return {"success": False, "status": 500, "error": "Failed to write assignment"}

        return {
            "success": True,
            "scope_type": scope_type,
            "scope_esid": scope_esid,
            "role_esid": role_esid,
        }

    @staticmethod
    def revoke(*, username: str, scope_type: str, scope_esid: str, role_esid: str) -> dict:
        """Delete one role-assignment doc.

         (P3b): membership docs are retired, so there is no longer
        a parallel membership doc to clean up. The role doc IS the
        authorization; deleting it fully revokes the grant. (The P1/P3a
        interim kept an orphan-membership cleanup here; with no membership
        docs that step is gone.)

        Returns ``{"success": True}`` (deletes nothing if the doc is
        already gone) or ``{"success": False, "status", "error"}``.
        """
        if scope_type not in SUPPORTED_SCOPE_TYPES:
            return {"success": False, "status": 400, "error": f"Unsupported scope_type: {scope_type}"}
        if not username or not scope_esid or not role_esid:
            return {"success": False, "status": 400, "error": "username, scope_esid, role_esid are required"}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "status": 503, "error": "Elasticsearch unavailable"}

        tier = _TIER_BY_SCOPE_TYPE[scope_type]
        try:
            es.delete(
                index=tier["assignment_index"],
                id=f"{username}:{scope_esid}:{role_esid}",
                refresh="wait_for",
            )
        except Exception as e:
            err_text = str(e).lower()
            if "not_found" in err_text or "404" in err_text:
                # Already gone — caller intent satisfied.
                return {"success": True, "already_absent": True}
            logging.error(
                "UserRoleAssignmentService: revoke failed for %s/%s/%s/%s: %s",
                username,
                scope_type,
                scope_esid,
                role_esid,
                e,
            )
            return {"success": False, "status": 500, "error": "Failed to revoke assignment"}

        return {"success": True}
