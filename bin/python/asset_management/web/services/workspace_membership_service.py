"""Workspace Membership Service.

Read surface for workspace-tier membership and role-assignment lookups.
This module is read-only.

 (P3a, 2026-06-01): the source of truth is now the workspace
ROLE index (``de_authz_user_workspace_role``). "Belongs to a workspace"
== "holds a workspace role for it"; the role doc carries ``is_primary``
(migrated from the retired membership doc). This service is kept as the
STABLE INTERFACE so its 11+ call sites are unchanged while the
membership indices are retired — only the internals moved off
``de_authz_workspace_membership``. ``get_role`` / ``is_agency_*``
already read the role / workspace indices.

Write paths use the same deterministic-doc-id + ``op_type=create``
pattern as ``OnboardingService._safe_create`` (Phase 14
convention). Future writers go through that helper, not through new
methods on this service.

Design notes:

* The Phase-1 default is **one workspace per user**, but the data model
  permits N>1 from day one. Methods like
  :meth:`get_workspaces_for_user` return a list so call sites that
  iterate keep working when the multi-workspace UI flips on. The
  switcher UI itself is gated by
  ``workspace.is_multi_workspace_user_supported`` on the workspace doc;
  this service does not enforce that.
* All methods fail soft on ES error (return ``None`` / ``False`` /
  empty list) so a transient ES outage degrades to "no privileges"
  rather than raising into the request path. The RoleFilterService
  consumer already knows how to handle the empty-grant case.

is_agency_for_user is the load-bearing helper (hide
Organizations menu unless agency) will consume. It looks up the user's
primary workspace and reads the ``is_agency`` flag — matching the spike
contract that agency is a workspace-tier concept, not an org-tier flag
(the deprecated ``de_asset_organization.is_multi_workspace``).
"""

from __future__ import annotations

import logging
import sys

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client

try:
    from web.services.permissions_index_service import (
        USER_WORKSPACE_ROLE_INDEX,
        WORKSPACE_INDEX,
    )
except ImportError:
    from asset_management.web.services.permissions_index_service import (
        USER_WORKSPACE_ROLE_INDEX,
        WORKSPACE_INDEX,
    )


class WorkspaceMembershipService:
    """Read-only lookups against the F8 workspace authz indices.

    Methods are stateless ``@staticmethod`` so callers don't pass an
    instance around; mirrors the convention used by ``DataGrantService``
    and ``PermissionsSeederService``.
    """

    @staticmethod
    def get_workspaces_for_user(username: str) -> list[str]:
        """Return the list of workspace ESIDs the user belongs to.

         (P3a): the source of truth is now the workspace ROLE
        index (``de_authz_user_workspace_role``), not the retired
        membership index. "Belongs to a workspace" == "holds a workspace
        role for it" — the single-source-of-truth model a teammate asked for.
        The role doc carries ``is_primary`` (migrated from the membership
        doc), so the sort contract is unchanged: ``is_primary`` desc then
        ``created_at`` asc, primary first, ``[0]`` is the best-guess
        workspace. A user with N role docs for one workspace (one per
        role) collapses to a single entry — de-duped preserving sort
        order.

        Phase-1 default is still one workspace per user.
        """
        if not username:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            resp = es.search(
                index=USER_WORKSPACE_ROLE_INDEX,
                body={
                    "query": {"term": {"username": username}},
                    "size": 100,
                    "sort": [
                        {"is_primary": {"order": "desc"}},
                        {"created_at": {"order": "asc"}},
                    ],
                },
            )
        except Exception as e:
            logging.warning(
                "WorkspaceMembershipService.get_workspaces_for_user(%s): %s",
                username,
                e,
            )
            return []
        hits = resp.get("hits", {}).get("hits", [])
        # De-dup by workspace_esid, preserving the is_primary/created_at
        # sort order (multiple role docs per workspace are possible).
        out: list[str] = []
        seen: set[str] = set()
        for hit in hits:
            ws = (hit.get("_source") or {}).get("workspace_esid")
            if ws and ws not in seen:
                seen.add(ws)
                out.append(ws)
        return out

    @staticmethod
    def get_primary_workspace(username: str) -> str | None:
        """Return the user's primary workspace ESID, or ``None``.

        ``None`` covers three cases the caller usually treats the same:
        unknown user, user with no memberships, or ES outage. The
        RoleFilterService consumer maps all three to deny-all.
        """
        workspaces = WorkspaceMembershipService.get_workspaces_for_user(username)
        return workspaces[0] if workspaces else None

    @staticmethod
    def resolve_curation_workspace(username: str) -> str | None:
        """the workspace whose Dashboard Builder menus this user
        curates — the SINGLE source of truth shared by the Builder topbar
        default and the read-only dashboards sidebar.

        These two surfaces previously disagreed: the read-only view used
        ``get_primary_workspace`` (which returns ``workspaces[0]`` — an
        arbitrary positional pick when no role is flagged ``is_primary``),
        while the Builder defaulted to a workspace DATA GRANT. For a
        superuser/multi-workspace user whose ``[0]`` role is a different
        workspace than the one they actually work in (via a grant), the
        Builder edited workspace A's menu order while the read-only view
        rendered workspace B's — so reorders never showed up.

        Preference order (mirrors the Builder's ``_default_workspace_esid``):
          1. A genuinely-PRIMARY workspace role (``is_primary == True``) —
             the only non-arbitrary signal in the role index.
          2. A workspace-scope DATA GRANT (``de_authz_data_grant``,
             ``scope_type == "workspace"``) — a superuser's working context.
          3. the workspace reached by walking the user's OWN
             org/team scope up to ``de_asset_organization.workspace_esid``
             (``_workspaces_from_org_team_scope``). This is the ORG-OWNER
             leg: an org owner holds an org *role* but typically no
             ``is_primary`` workspace role and no workspace-scope grant, so
             legs 1-2 return nothing even though their org points straight
             at a workspace. Without this leg the template-install path
             (and any other caller) resolved ``None`` for an org owner and
             stamped an empty ``workspace_esid``. Same
             derivation added to the Data Inventory switcher;
             reused here so the two stay in sync.
          4. The positional ``get_primary_workspace`` fallback (preserves
             the single-workspace owner/member case exactly).

        Returns ``None`` only when nothing resolves (unknown user / ES
        outage / no workspaces) — callers degrade to the alphabetical menu
        fallback, same as before.
        """
        if not username:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return WorkspaceMembershipService.get_primary_workspace(username)

        # 1. is_primary workspace role.
        try:
            resp = es.search(
                index=USER_WORKSPACE_ROLE_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"username": username}},
                                {"term": {"is_primary": True}},
                            ]
                        }
                    },
                    "_source": ["workspace_esid"],
                    "size": 5,
                },
            )
            for h in resp.get("hits", {}).get("hits", []):
                ws = (h.get("_source") or {}).get("workspace_esid")
                if ws:
                    return ws
        except Exception as e:  # noqa: BLE001 — degrade to the next signal
            if not ("index_not_found" in str(e).lower() or "404" in str(e)):
                logging.warning("resolve_curation_workspace primary(%s): %s", username, e)

        # 2. Workspace-scope data grant.
        try:
            from asset_management.web.services.permissions_index_service import (
                DATA_GRANT_INDEX,
            )

            resp = es.search(
                index=DATA_GRANT_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"username": username}},
                                {"term": {"scope_type": "workspace"}},
                            ]
                        }
                    },
                    "_source": ["scope_esid"],
                    "size": 5,
                },
            )
            for h in resp.get("hits", {}).get("hits", []):
                ws = (h.get("_source") or {}).get("scope_esid")
                if ws:
                    return ws
        except Exception as e:  # noqa: BLE001 — degrade to the org-walk / positional fallback
            if not ("index_not_found" in str(e).lower() or "404" in str(e)):
                logging.warning("resolve_curation_workspace grant(%s): %s", username, e)

        # 3. ORG-OWNER leg: walk the user's own org/team scope up to
        # its workspace. Bounded to the user's resolved org_esids/team_esids
        # (never a cluster-wide scan), so a broadly-granted superuser with no
        # concrete org/team scope contributes nothing here. Returns the first
        # workspace; an org owner's org points at exactly one workspace in the
        # current model. Fail-soft to an empty list inside the helper.
        for ws in WorkspaceMembershipService._workspaces_from_org_team_scope(username):
            if ws:
                return ws

        # 4. Positional fallback (unchanged single-workspace behavior).
        return WorkspaceMembershipService.get_primary_workspace(username)

    @staticmethod
    def data_grant_and_primary_workspaces(username: str) -> list[str]:
        """the set of workspaces a user may INSPECT in Data Inventory.

        Deliberately NARROWER than ``RoleFilterService.get_allowed_workspaces``
        (which unions grants + memberships + the broad inferred walk). For the
        Data Inventory switcher a superuser should see ONLY their own scope, so
        the legs are bounded to the user:
          * every workspace-scope data grant they hold
            (``de_authz_data_grant``, ``scope_type == "workspace"``),
          * their primary workspace (``is_primary`` role, else positional), and
          * (2026-06-18): workspaces reached by walking the
            user's OWN org/team scope up to their workspace
            (``de_asset_organization.workspace_esid`` /
            ``de_asset_teams.organization_esid → org.workspace_esid``).

        The third leg is what fixes the **org-owner** auto-select regression:
        an org owner holds an org *role* (``de_authz_user_org_role``) but
        typically no workspace-scope grant and no ``is_primary`` workspace
        role, so the first two legs returned ``[]`` and the front-end never
        saw a single workspace to auto-select. The walk is bounded to the
        user's resolved ``org_esids`` / ``team_esids`` (NOT a cluster-wide
        scan), so a broadly-granted superuser with no concrete org/team scope
        still contributes nothing here — the superuser-narrowing intent that
        motivated this method's existence is preserved.

        Returns a de-duplicated list (possibly empty). ES outage → empty list
        (deny-all for the switcher).
        """
        # Legs 1 + 2 (workspace data grants + primary), then Leg 3 (org/team
        # scope walk). Legs 1+2 are shared with primary_and_grant_workspaces.
        found = WorkspaceMembershipService.primary_and_grant_workspaces(username)
        if not found and get_elasticsearch_client() is None:
            return []

        seen: set[str] = set(found)

        # Leg 3 — walk the user's OWN org/team scope up to its workspace(s).
        for ws in WorkspaceMembershipService._workspaces_from_org_team_scope(username):
            if ws and ws not in seen:
                seen.add(ws)
                found.append(ws)

        return found

    @staticmethod
    def primary_and_grant_workspaces(username: str) -> list[str]:
        """the STRICT primary-workspace + explicit-workspace-grant set
        — ``data_grant_and_primary_workspaces`` minus its org/team-scope walk
        (Leg 3).

        This is what "scope a privileged user to their primary workspace"
        actually means for the Report Scheduler / Data Connectors SELECTION
        surfaces. ``data_grant_and_primary_workspaces`` adds Leg 3
        (``_workspaces_from_org_team_scope``) so an org owner's Data Inventory
        switcher auto-resolves their own workspace — but for a
        SUPERUSER who owns/created teams seeded across several workspaces, that
        walk drags in every such workspace (one superuser resolved to 4 of 7 on
        staging, 2026-06-19), defeating the narrowing. The selection surfaces
        therefore use THIS tighter set instead:

          * Leg 1 — every workspace-scope data grant they hold
            (``de_authz_data_grant``, ``scope_type == "workspace"``), and
          * Leg 2 — their primary workspace (``resolve_curation_workspace``:
            ``is_primary`` role → positional fallback).

        A superuser with a genuine workspace data-grant on another workspace can
        still reach it (Leg 1); one who merely owns scattered teams cannot. For
        such a superuser this returns just ``["kavanah-media"]``.

        Returns a de-duplicated list (possibly empty). Unknown user / ES outage
        → empty list (deny-all for the caller, same posture as
        data_grant_and_primary_workspaces).
        """
        if not username:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []

        found: list[str] = []
        seen: set[str] = set()

        def _add(ws: str | None) -> None:
            if ws and ws not in seen:
                seen.add(ws)
                found.append(ws)

        # Leg 1 — all workspace-scope data grants (not just the first, unlike
        # resolve_curation_workspace which returns one working context).
        try:
            from asset_management.web.services.permissions_index_service import (
                DATA_GRANT_INDEX,
            )

            resp = es.search(
                index=DATA_GRANT_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"username": username}},
                                {"term": {"scope_type": "workspace"}},
                            ]
                        }
                    },
                    "_source": ["scope_esid"],
                    "size": 1000,
                },
            )
            for h in resp.get("hits", {}).get("hits", []):
                _add((h.get("_source") or {}).get("scope_esid"))
        except Exception as e:  # noqa: BLE001 — degrade; the primary leg still runs
            if not ("index_not_found" in str(e).lower() or "404" in str(e)):
                logging.warning("primary_and_grant_workspaces grant(%s): %s", username, e)

        # Leg 2 — the primary workspace (is_primary role → positional fallback).
        _add(WorkspaceMembershipService.resolve_curation_workspace(username))

        return found

    @staticmethod
    def _workspaces_from_org_team_scope(username: str) -> list[str]:
        """workspaces reached by walking the user's resolved
        org/team scope up to ``de_asset_organization.workspace_esid``.

        Reuses ``resolve_user_scope`` (the single source of truth for "what
        scope does this user have?") and the exact ESID-based walk
        ``get_allowed_workspaces`` uses (``_workspace_esids_from_scope_esids``),
        so org owners resolve to their own workspace without re-deriving the
        traversal. The walk is keyed on the user's ``org_esids`` / ``team_esids``
        only — never a cluster-wide scan — so it cannot widen a superuser's
        switcher to every workspace.

        Fails soft to an empty list on any error (missing session, ES outage,
        import issue) — the caller's other legs still contribute.
        """
        try:
            from flask import session

            try:
                from web.services.role_filter_service import (
                    RoleFilterService,
                    _workspace_esids_from_scope_esids,
                    resolve_user_scope,
                )
            except ImportError:
                from asset_management.web.services.role_filter_service import (
                    RoleFilterService,
                    _workspace_esids_from_scope_esids,
                    resolve_user_scope,
                )

            user_roles = session.get("roles", []) if session else []
            scope = resolve_user_scope(username, user_roles, RoleFilterService.load_config())
            if not scope.org_esids and not scope.team_esids:
                return []
            return list(_workspace_esids_from_scope_esids(scope.org_esids, scope.team_esids))
        except Exception as e:  # noqa: BLE001 — fail soft; other legs still run
            logging.warning("_workspaces_from_org_team_scope(%s): %s", username, e)
            return []

    @staticmethod
    def is_member(username: str, workspace_esid: str) -> bool:
        """Is this user authorised for THIS workspace?

         (P3a): "member of a workspace" == "holds a workspace role
        for it". The membership doc had a single deterministic id
        (``{username}:{workspace_esid}``) so this was an ``es.get``; the
        role doc id carries a ``:role_esid`` suffix we don't know here, so
        we count role docs matching the two keyword fields. ``count`` with
        a ``terminate_after`` is still a cheap one-shot for the common
        per-request guard.
        """
        if not username or not workspace_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            resp = es.count(
                index=USER_WORKSPACE_ROLE_INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"username": username}},
                                {"term": {"workspace_esid": workspace_esid}},
                            ]
                        }
                    }
                },
            )
            return int(resp.get("count", 0)) > 0
        except Exception as e:
            if "index_not_found" in str(e).lower() or "404" in str(e):
                return False
            logging.warning(
                "WorkspaceMembershipService.is_member(%s, %s): %s",
                username,
                workspace_esid,
                e,
            )
            return False

    @staticmethod
    def get_role(username: str, workspace_esid: str) -> str | None:
        """Return the user's workspace-tier role ESID for that workspace.

        Returns ``None`` if the user has no role assignment for the
        workspace, or on ES outage. When a user has multiple role
        assignments (rare; admin tools could in theory create them),
        returns the alphabetically-first ESID for determinism — callers
        that need finer resolution should query the index directly.
        """
        if not username or not workspace_esid:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            resp = es.search(
                index=USER_WORKSPACE_ROLE_INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"username": username}},
                                {"term": {"workspace_esid": workspace_esid}},
                            ]
                        }
                    },
                    "size": 10,
                    "sort": [{"role_esid": {"order": "asc"}}],
                },
            )
        except Exception as e:
            logging.warning(
                "WorkspaceMembershipService.get_role(%s, %s): %s",
                username,
                workspace_esid,
                e,
            )
            return None
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            return None
        return hits[0]["_source"].get("role_esid")

    @staticmethod
    def is_agency_for_workspace(workspace_esid: str) -> bool:
        """does a workspace operate as an agency?

        Variant of ``is_agency_for_user`` that takes a workspace_esid
        directly instead of resolving via the user's primary workspace.
        ``api_create_user``'s org-requirement check needs this to look
        up each requested ``workspace_<slug>`` membership and decide
        whether the agency exemption applies — there's no user yet to
        ask "what's their primary workspace".

        Returns False on no-workspace, missing flag, or ES outage.
        The safe default is "not an agency" so the org-requirement gate
        in api_create_user errs on the strict side: a transient ES
        glitch produces a 400 the operator can retry rather than
        silently letting a workspace-only user creation through.
        """
        if not workspace_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            resp = es.get(index=WORKSPACE_INDEX, id=workspace_esid)
        except Exception as e:
            if "not_found" in str(e).lower() or "404" in str(e):
                return False
            logging.warning(
                "WorkspaceMembershipService.is_agency_for_workspace(%s): %s",
                workspace_esid,
                e,
            )
            return False
        return bool(resp.get("_source", {}).get("is_agency"))

    @staticmethod
    def ai_assistant_enabled_for_workspace(workspace_esid: str) -> bool:
        """has a superuser enabled the AI Assistant for this workspace?

        Gate 1 of the three-gate AI Assistant model (workspace enabled AND
        ``mc_dashboard_builder`` AND the user's ``ai_assistant`` role). Same
        read shape as :meth:`is_agency_for_workspace`: reads the workspace
        doc's ``ai_assistant_enabled`` flag directly by ESID.

        Returns False on no-workspace, missing flag, or ES outage — the AI
        Assistant is a spend-metered feature, so any broken or unknown state
        must fail closed (deny) rather than silently bill.
        """
        if not workspace_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            resp = es.get(index=WORKSPACE_INDEX, id=workspace_esid)
        except Exception as e:
            if "not_found" in str(e).lower() or "404" in str(e):
                return False
            logging.warning(
                "WorkspaceMembershipService.ai_assistant_enabled_for_workspace(%s): %s",
                workspace_esid,
                e,
            )
            return False
        return bool(resp.get("_source", {}).get("ai_assistant_enabled"))

    @staticmethod
    def hide_empty_dashboards_for_workspace(workspace_esid: str) -> bool:
        """does this workspace opt in to data-driven menu hiding?

        When True, the user-facing dashboards sidebar hides any dashboard
        whose underlying indices currently hold zero documents (the legacy
        ``updatePlatformVisibility`` behavior). When False (the default),
        flag-visible dashboards stay in the menu regardless of data so the
        in-dashboard "pull data" control is always reachable.

        Only the legacy Kavanah Media workspace is seeded True (see
        ``bin/python/tools/seed_hide_empty_dashboards.py``); every other
        workspace inherits the safe default.

        Returns False on no-workspace, missing flag, or ES outage — the
        safe default is "show", which can never strand a user behind a
        hidden dashboard with no way to populate it.
        """
        if not workspace_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            resp = es.get(index=WORKSPACE_INDEX, id=workspace_esid)
        except Exception as e:
            if "not_found" in str(e).lower() or "404" in str(e):
                return False
            logging.warning(
                "WorkspaceMembershipService.hide_empty_dashboards_for_workspace(%s): %s",
                workspace_esid,
                e,
            )
            return False
        return bool(resp.get("_source", {}).get("hide_empty_dashboards"))

    @staticmethod
    def is_agency_for_user(username: str) -> bool:
        """anchor: does the user's primary workspace operate as an agency?

        Returns the ``is_agency`` flag on the user's primary workspace.
        False on no-workspace, missing flag, or ES outage — the safe
        default is "not an agency" because that hides the Organizations
        menu (per the spike's UX claim: solo users should never
        see the word "organization" in the navigation).

        This is the canonical forward-looking read. **Do not** call
        ``org.is_multi_workspace`` from new code — that field is the
        deprecated F4-era marker and will be dropped post-F7.
        """
        workspace_esid = WorkspaceMembershipService.get_primary_workspace(username)
        if not workspace_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            resp = es.get(index=WORKSPACE_INDEX, id=workspace_esid)
        except Exception as e:
            if "not_found" in str(e).lower() or "404" in str(e):
                return False
            logging.warning(
                "WorkspaceMembershipService.is_agency_for_user(%s): workspace lookup failed: %s",
                username,
                e,
            )
            return False
        return bool(resp.get("_source", {}).get("is_agency"))
