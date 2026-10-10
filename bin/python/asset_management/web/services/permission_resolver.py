"""
Permission Resolver

Read-only resolver for effective permissions. Pure function over ES
reads — given a (username, team_esid) and a super-admin signal, return
the set of ``Permission`` enum values that user holds on that team.

Phase 1 (Build, no behavior change) — nothing calls this resolver yet.
The wiring into route handlers and template guards lands in Phase 4
(Cutover), gated by ``USE_NEW_PERMISSIONS=true``.

Cascading rule (per ticket)::

    effective(user, team) =
        permissions(user's team-scoped roles on team)
        UNION
        permissions(user's org-scoped roles on every org the team
                    belongs to — multi-org teams cascade from each)

Multi-role semantics: when a user has multiple roles in the same scope,
the effective set is the union of those roles' permission bundles.

Super-admin shortcut: when ``is_super_admin=True`` is passed by the
caller, the resolver returns ``ALL_PERMISSIONS`` immediately without
touching ES. The signal is the caller's responsibility — typically
``"superuser" in session.get("roles", [])`` from Flask.
"""

import logging
import sys

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client

from asset_management.web.services.permissions_enum import (
    ALL_PERMISSIONS,
    Permission,
)
from asset_management.web.services.permissions_index_service import (
    ROLE_INDEX,
    ROLE_PERMISSION_INDEX,
    USER_ORG_ROLE_INDEX,
    USER_TEAM_ROLE_INDEX,
    USER_WORKSPACE_ROLE_INDEX,
)

# de_asset_teams stores ``organization_esid`` as a string (single-org
# team) or an array (multi-org team). The cascade applies to every
# linked org.
TEAMS_INDEX = "de_asset_teams"


class PermissionResolver:
    """Resolve effective permissions for a user on a team."""

    @staticmethod
    def effective_permissions(
        username: str,
        team_esid: str,
        *,
        is_super_admin: bool = False,
    ) -> frozenset[Permission]:
        """Return the set of permissions ``username`` holds on team
        ``team_esid``.

        Returns ``ALL_PERMISSIONS`` immediately when ``is_super_admin``
        is true. Returns an empty frozenset when the user has no
        memberships/role assignments — orphaned-user state, handled
        upstream by displaying the "no access" page rather than raising
        from the resolver.

        Returns an empty frozenset when ES is unreachable. Deny-by-
        default is the safe failure mode; the caller's existing checks
        (Phase 1: the old ``de_role_permissions`` system) remain the
        authoritative gate, so this empty result has no observable
        effect during Phase 1.
        """
        if is_super_admin:
            return ALL_PERMISSIONS

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("PermissionResolver: ES unavailable; returning empty permission set")
            return frozenset()

        role_esids: set[str] = set()
        role_esids |= PermissionResolver._role_esids_for_user_team(es, username, team_esid)
        for org_esid in PermissionResolver._team_orgs(es, team_esid):
            role_esids |= PermissionResolver._role_esids_for_user_org(es, username, org_esid)

        if not role_esids:
            return frozenset()

        perms: set[Permission] = set()
        for role_esid in role_esids:
            perms |= PermissionResolver._permissions_for_role(es, role_esid)

        return frozenset(perms)

    @staticmethod
    def effective_permissions_anywhere(
        username: str,
        *,
        is_super_admin: bool = False,
    ) -> frozenset[Permission]:
        """Return the union of permissions ``username`` holds across
        every workspace, org, and team assignment in the new authz
        model.

        Used Phase 3 shadow comparison as a "user has any
        scope" fuzzy proxy AND as the resolver behind
        :meth:`has_permission` (the sidebar / route-gate consumer).

        Returns ``ALL_PERMISSIONS`` immediately for super admins (no
        ES queries). Returns an empty frozenset on ES outage
        (deny-by-default — same posture as
        :meth:`effective_permissions`).

        Note: this skips the per-team cascade context that
        :meth:`effective_permissions` provides. Sidebar / route-gate
        consumers only need "any access at all" (not "what permissions
        on this specific team"), so the simpler union over (user_team_role
        + user_org_role + user_workspace_role) is enough.

         / F8 added the workspace tier; wired it into
        this method so workspace_admin / workspace_owner are
        first-class for sidebar visibility and admin gates.
        """
        if is_super_admin:
            return ALL_PERMISSIONS

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("PermissionResolver: ES unavailable; returning empty 'anywhere' permission set")
            return frozenset()

        role_esids: set[str] = set()
        role_esids |= PermissionResolver._all_role_esids_by_username(es, USER_TEAM_ROLE_INDEX, username)
        role_esids |= PermissionResolver._all_role_esids_by_username(es, USER_ORG_ROLE_INDEX, username)
        role_esids |= PermissionResolver._all_role_esids_by_username(es, USER_WORKSPACE_ROLE_INDEX, username)

        if not role_esids:
            return frozenset()

        perms: set[Permission] = set()
        for role_esid in role_esids:
            perms |= PermissionResolver._permissions_for_role(es, role_esid)
        return frozenset(perms)

    @staticmethod
    def has_permission_in_workspace(
        username: str,
        workspace_esid: str,
        permission: Permission,
        *,
        is_super_admin: bool = False,
    ) -> bool:
        """does ``username`` hold ``permission`` via a
        WORKSPACE-TIER role assignment on ``workspace_esid``?

        Deliberately narrower than :meth:`has_permission`: that method is
        "anywhere", which is the wrong shape for verbs that act on a
        specific workspace's resources (re-auth overwrites a live
        credential — a workspace_admin of workspace A must not exercise
        it in workspace B). Only workspace-tier role docs are consulted;
        org/team roles never cascade UP to workspace verbs.

        Deny-by-default on ES outage or empty inputs.
        """
        if is_super_admin:
            return True
        if not username or not workspace_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("PermissionResolver: ES unavailable; denying workspace permission check")
            return False
        try:
            result = es.search(
                index=USER_WORKSPACE_ROLE_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"username": username}},
                                {"term": {"workspace_esid": workspace_esid}},
                            ]
                        }
                    },
                    "_source": ["role_esid"],
                    "size": 100,
                },
            )
        except Exception as e:  # noqa: BLE001 — deny-by-default boundary
            logging.warning("PermissionResolver.has_permission_in_workspace: %s", e)
            return False
        role_esids = {
            r for hit in result.get("hits", {}).get("hits", []) if (r := (hit.get("_source") or {}).get("role_esid"))
        }
        for role_esid in role_esids:
            if permission in PermissionResolver._permissions_for_role(es, role_esid):
                return True
        return False

    @staticmethod
    def has_permission(
        username: str,
        permission: Permission,
        *,
        is_super_admin: bool = False,
    ) -> bool:
        """Return True iff ``username`` holds ``permission`` on any
        scope (team / org / workspace).

        Thin wrapper over :meth:`effective_permissions_anywhere`.
        Used by the sidebar template + route gates to
        check capabilities without needing to resolve the full
        permission set at every call site.

        Deny-by-default on ES outage matches the underlying resolver.
        """
        if is_super_admin:
            return True
        return permission in PermissionResolver.effective_permissions_anywhere(username)

    @staticmethod
    def _all_role_esids_by_username(es, index: str, username: str) -> set[str]:
        """Fetch every ``role_esid`` assigned to ``username`` in the
        given role-assignment index, with no scope filter. Used by
        :meth:`effective_permissions_anywhere`.
        """
        try:
            result = es.search(
                index=index,
                body={
                    "query": {"term": {"username": username}},
                    "_source": ["role_esid"],
                    "size": 1000,
                },
            )
        except Exception as e:
            logging.error(
                "PermissionResolver: error querying %s for %s: %s",
                index,
                username,
                e,
            )
            return set()
        return {
            hit["_source"]["role_esid"]
            for hit in result.get("hits", {}).get("hits", [])
            if hit.get("_source", {}).get("role_esid")
        }

    @staticmethod
    def _role_esids_for_user_team(es, username: str, team_esid: str) -> set[str]:
        try:
            result = es.search(
                index=USER_TEAM_ROLE_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"username": username}},
                                {"term": {"team_esid": team_esid}},
                            ]
                        }
                    },
                    "_source": ["role_esid"],
                    "size": 100,
                },
            )
        except Exception as e:
            logging.error("PermissionResolver: error querying team role assignments: %s", e)
            return set()
        return {
            hit["_source"]["role_esid"]
            for hit in result.get("hits", {}).get("hits", [])
            if hit.get("_source", {}).get("role_esid")
        }

    @staticmethod
    def _role_esids_for_user_org(es, username: str, org_esid: str) -> set[str]:
        try:
            result = es.search(
                index=USER_ORG_ROLE_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"username": username}},
                                {"term": {"org_esid": org_esid}},
                            ]
                        }
                    },
                    "_source": ["role_esid"],
                    "size": 100,
                },
            )
        except Exception as e:
            logging.error("PermissionResolver: error querying org role assignments: %s", e)
            return set()
        return {
            hit["_source"]["role_esid"]
            for hit in result.get("hits", {}).get("hits", [])
            if hit.get("_source", {}).get("role_esid")
        }

    @staticmethod
    def _team_orgs(es, team_esid: str) -> list[str]:
        try:
            result = es.get(index=TEAMS_INDEX, id=team_esid)
        except Exception as e:
            logging.warning(
                "PermissionResolver: team %s not found in %s: %s",
                team_esid,
                TEAMS_INDEX,
                e,
            )
            return []
        org_esids = result.get("_source", {}).get("organization_esid", [])
        if isinstance(org_esids, str):
            org_esids = [org_esids]
        return [esid for esid in org_esids if esid]

    @staticmethod
    def effective_role_names(
        username: str,
        *,
        is_super_admin: bool = False,
    ) -> frozenset[str]:
        """Return the set of role *names* ``username`` holds across every
        team and org assignment in the new authz model.

        Used Phase 4 cutover as the data source for the
        existing role-name-based gates (``"asset_admin" in roles``,
        ``"superuser" in roles``). Cutover swaps the source of those
        membership checks from ``session['roles']`` to the new model
        without redesigning the gate semantics — that's Phase 5+.

        Returns ``frozenset({"superuser"})`` immediately for super
        admins (no ES queries — the caller already knows). Returns an
        empty frozenset on ES outage (deny-by-default — same posture
        as :meth:`effective_permissions`).
        """
        if is_super_admin:
            return frozenset({"superuser"})

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("PermissionResolver: ES unavailable; returning empty " "role-name set")
            return frozenset()

        role_esids: set[str] = set()
        role_esids |= PermissionResolver._all_role_esids_by_username(es, USER_TEAM_ROLE_INDEX, username)
        role_esids |= PermissionResolver._all_role_esids_by_username(es, USER_ORG_ROLE_INDEX, username)

        if not role_esids:
            return frozenset()

        try:
            result = es.search(
                index=ROLE_INDEX,
                body={
                    "query": {"terms": {"role_esid": list(role_esids)}},
                    "_source": ["role_name"],
                    "size": 1000,
                },
            )
        except Exception as e:
            logging.error("PermissionResolver: error fetching role names for %s: %s", username, e)
            return frozenset()

        return frozenset(
            hit["_source"]["role_name"]
            for hit in result.get("hits", {}).get("hits", [])
            if hit.get("_source", {}).get("role_name")
        )

    @staticmethod
    def _permissions_for_role(es, role_esid: str) -> set[Permission]:
        try:
            result = es.search(
                index=ROLE_PERMISSION_INDEX,
                body={
                    "query": {"term": {"role_esid": role_esid}},
                    "_source": ["permission_key"],
                    "size": 1000,
                },
            )
        except Exception as e:
            logging.error(
                "PermissionResolver: error querying role permissions for %s: %s",
                role_esid,
                e,
            )
            return set()

        keys = {
            hit["_source"]["permission_key"]
            for hit in result.get("hits", {}).get("hits", [])
            if hit.get("_source", {}).get("permission_key")
        }
        # Forward-compat: a permission removed from the enum could leave
        # orphan link rows. Drop unknown keys rather than raising on
        # Permission(unknown_key).
        valid: set[Permission] = set()
        for key in keys:
            try:
                valid.add(Permission(key))
            except ValueError:
                logging.warning(
                    "PermissionResolver: unknown permission key %r in role %s",
                    key,
                    role_esid,
                )
        return valid


# map sidebar items to OR-formulas over the existing 8-verb
# Permission enum. Three management-console items + the two header /
# dashboard nav surfaces migrate to the new model under this ticket;
# everything else stays on the OLD _DEFAULTS system.
SIDEBAR_ITEM_PERMISSIONS: dict[str, tuple[Permission, ...]] = {
    # Asset Explorer + User Management are workspace-tier-only
    # surfaces. Dropped ORG_MANAGE / TEAM_MANAGE (and the USER_* verbs for
    # user-management) so org_admin / team_admin no longer see these menus
    # via the System-2 gate. The full-privilege 'owner' role keeps them
    # (its bundle is list(Permission), so it holds WORKSPACE_MANAGE like
    # workspace_owner/workspace_admin). System-1 superuser / asset_admin
    # keep access through the perms.get('mc_*') OR-branch in
    # _sidebar_visible.
    "mc_asset_explorer": (Permission.WORKSPACE_MANAGE,),
    # follow-up (2026-06-02): User Management is OWNER-ONLY. Gate it on
    # WORKSPACE_DELETE (owner's list(Permission) has it; workspace_admin's
    # explicit bundle does NOT) instead of WORKSPACE_MANAGE. This drives BOTH
    # the sidebar menu (_sidebar_visible branch 2) AND the page route
    # (_require_mc) — previously gated on WORKSPACE_MANAGE, which admin holds,
    # so admin saw + could reach the page. superuser / asset_admin keep access
    # via effective_permissions_anywhere(is_super_admin=True) → ALL perms.
    "mc_user_management": (Permission.WORKSPACE_DELETE,),
    "mc_dashboard_builder": (Permission.REPORT_EDIT,),
    # Data Inventory — workspace-tier read-only data-coverage page.
    # WORKSPACE_MANAGE → workspace_owner + workspace_admin, matching the
    # bridge in get_merged_permissions and the other workspace-tier surfaces.
    "mc_data_inventory": (Permission.WORKSPACE_MANAGE,),
    # Header gear-icon + cross-nav: hdr_dashboards_link gates the
    # "Dashboards" entry in the gear dropdown when the user is inside
    # the management console; db_management_console gates the gear
    # itself + the "Management Console" entry when the user is on a
    # dashboard page. Both included so a scoped admin can move between
    # the two surfaces.
    "hdr_dashboards_link": (
        Permission.REPORT_VIEW,
        Permission.REPORT_EDIT,
    ),
    "db_management_console": (
        Permission.WORKSPACE_MANAGE,
        Permission.ORG_MANAGE,
        Permission.TEAM_MANAGE,
        Permission.USER_INVITE,
        Permission.USER_REMOVE,
        Permission.ROLE_ASSIGNMENT_MANAGE,
        Permission.REPORT_EDIT,
    ),
}
