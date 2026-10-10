"""
Role-based filter resolution for Kibana visualizations.

This service determines which organization/team filters to apply to Kibana
visualizations based on the current user's Elasticsearch roles.

Role Naming Convention:
    - Organization roles: org_<organization_title> (e.g., org_ACME, org_Kavanah)
      Maps to organization_title field in de_asset_organization index
    - Team roles: team_<team_title> (e.g., team_NORTH, team_ACME)
      Maps to team_title field in de_asset_teams index

Usage:
    from services.role_filter_service import RoleFilterService

    # Get filter for current logged-in user
    filter_clause = RoleFilterService.get_filter_for_current_user()

    if filter_clause:
        # Apply filter to Elasticsearch query
        # e.g., {"term": {"organization_title": "Kavanah Media"}}
        pass
"""

import yaml
import os
import logging
import re
import sys
import threading
from dataclasses import dataclass, field
from flask import session

# Add Python path for imports
sys.path.insert(0, "/app/python")
from lib.es_helpers import (
    KNOWN_SOURCES,
    get_document_by_id,
    get_elasticsearch_client,
    scoped_index_pattern as _scoped_index_pattern_impl,
)

# Partnership detection cache (TTL-based)
_partnership_cache = {}  # Format: {org_name: (has_partnerships: bool, timestamp: float)}
PARTNERSHIP_CACHE_TTL = 300  # 5 minutes (same as other caches)
# guards the read-check and the publish; the ES probe itself runs
# outside the lock (a duplicate probe on a cold cache is acceptable, a lock
# held across five index searches is not).
_partnership_cache_lock = threading.Lock()


# Phase A: single source of truth for "what scope does this user have?"
#
# earlier, four helpers (should_apply_filter / get_allowed_organizations /
# get_allowed_teams_for_org_users / get_filter_for_current_user) each walked
# their own fall-through chain over ES Security roles + memberships + grants +
# (forward-looking) workspace tier. patched each chain independently
# to add workspace fall-through and produced four rounds of leak-and-patch. The
# resolver below collapses all four sources into one structured answer; the
# consumer helpers become thin projections of UserScope fields.
#
# Source order is **additive within tier** — a privileged user with both a
# membership and a grant gets the UNION. ``scope_source``
# names the first source that contributed; ``scope_source='none'`` is the
# load-bearing privacy signal — empty UserScope, no fall-through to "unscoped."
#
# Phase B: ``allowed_indices`` is now populated — the concrete set of
# ``{team_esid}__{source}`` ES index names this user may read. Layer-1 of the
# two-layer tenant-isolation model; ``RoleFilterService.scoped_index_pattern``
# is the entry point that wraps ``es.search`` call sites.
@dataclass(frozen=True)
class UserScope:
    workspace_esids: tuple[str, ...] = ()
    org_titles: tuple[str, ...] = ()
    team_titles: tuple[str, ...] = ()
    # (2026-05-26): carry ESIDs alongside titles. Titles are NOT
    # unique across workspaces — two wizard signups both create a
    # ``General`` team in their own workspace, and joining authz scope
    # on ``team_title`` would walk both up to their respective
    # workspaces and produce a cross-workspace leak in
    # ``get_allowed_workspaces``. ESIDs are the ES ``_id`` of the
    # asset doc and are globally unique by construction. Any authz-
    # resolution path that walks scope → workspace MUST use ESIDs,
    # not titles. Title fields are kept because the data-layer filter
    # (``team_title`` on data docs) and the legacy display path still
    # need them.
    team_esids: frozenset[str] = field(default_factory=frozenset)
    org_esids: frozenset[str] = field(default_factory=frozenset)
    allowed_indices: frozenset[str] = field(default_factory=frozenset)
    scope_source: str = "none"

    @property
    def is_empty(self) -> bool:
        """True iff the user has no resolvable scope from any source.

        ``scope_source='none'`` is the canonical signal; the
        team_titles / org_titles / workspace_esids check is a belt-and-
        suspenders cross-check so a mis-populated source can't smuggle
        in a falsy ``scope_source`` while still claiming scope.
        """
        return self.scope_source == "none" and not (self.team_titles or self.org_titles or self.workspace_esids)


# follow-up: sentinel scope ID indicating "all teams" — present in
# de_authz_team_membership / de_authz_user_team_role docs for users with
# system-admin status. Lets the normal scope-resolution path return
# unrestricted for admins without a separate `unrestricted_roles` config
# bypass list.
ALL_SCOPES_SENTINEL = "__all__"


def _user_is_system_admin(username: str) -> bool:
    """Return True if the user has a system-admin assignment in the new
    authz model (a `de_authz_user_team_role` doc with team_esid=__all__
    and role_esid=system_admin)."""
    if not username:
        return False
    es = get_elasticsearch_client()
    if es is None:
        return False
    try:
        doc_id = f"{username}:{ALL_SCOPES_SENTINEL}:system_admin"
        es.get(index="de_authz_user_team_role", id=doc_id)
        return True
    except Exception:
        return False


# / F1: the deny-all filter for a privileged user (system_admin
# / unrestricted-role) who holds zero active data-access grants. Reuses
# the existing ``__NO_TEAMS_FOR_ORG__`` sentinel from line 662 below —
# that string already produces zero hits in every downstream query
# (test_role_filter_service.py covers the org-user-with-no-teams case)
# and the codebase has years of integration history with it. F1
# piggy-backs rather than introducing a parallel "deny-all" shape.
_DENY_ALL_FILTER = {"term": {"team_title": "__NO_TEAMS_FOR_ORG__"}}


# roles that hold full management-console access but whose
# *data* visibility must come from explicit memberships and/or active
# de_authz_data_grant records (NOT from the role itself). Today this
# is ``asset_admin``; the rationale lives in
# permissions_enum.py:75-82 — ``asset_admin`` and ``superuser`` are
# admins for UI surfaces but their data scope is grant-derived so the
# privacy boundary stays enforceable. Defined here as a constant
# (rather than only in the YAML's ``unrestricted_roles`` list) because
# the role name is referenced in ~10 other call sites and a config
# misedit must not silently re-open the leak.
_DATA_PRIVILEGED_ROLES = frozenset({"asset_admin"})


def _user_is_privileged(user_roles: list[str], username: str, config: dict) -> bool:
    """return True if the user is on the system-admin,
    legacy unrestricted-role, OR data-privileged-role path. These users
    formerly got blanket no-filter access; under F1 their access is
    derived from ``_privileged_user_scope`` (memberships UNION grants).
    A bare privileged user with neither memberships nor grants surfaces
    the ``__NO_TEAMS_FOR_ORG__`` sentinel — sees nothing.

     follow-up (smoke-test 2026-05-18): a user with a
    ``de_authz_user_workspace_role`` assignment but no asset_admin /
    sentinel / unrestricted_role was being routed to the legacy
    ES-Security branch which never reads the workspace role index.
    Their resolved scope was empty filter denied
    everything. workspace_admin is a scoped admin role with explicit
    authority over its workspace — we now treat any workspace role
    assignment as privileged so the resolver computes their scope
    through the same memberships ∪ grants ∪ workspace path as
    asset_admin. The data-isolation filter still narrows them to the
    workspace they have a role for (via the existing union logic);
    privilege here only opens the resolver door, not the asset doors.
    """
    if _memoize_on_g(("system_admin", username), lambda: _user_is_system_admin(username)):
        return True
    unrestricted_roles = config.get("unrestricted_roles", [])
    if any(role in unrestricted_roles for role in user_roles):
        return True
    # asset_admin is privileged on the data path. Their menu
    # access stays via the existing role-name checks in app.py /
    # auth.py; this branch only governs how their data queries are
    # scoped.
    if any(role in _DATA_PRIVILEGED_ROLES for role in user_roles):
        return True
    # follow-up: a user with a privileged workspace role
    # assignment (workspace_admin / workspace_owner) is privileged for
    # resolver purposes. workspace_member is a viewer-tier role and
    # does NOT confer privileged-resolver status —. The
    # memoization key is distinct from the resolver's non-privileged
    # ``_workspace_role_assignment_esids`` call (which passes
    # privileged_roles_only=False) so both forms can coexist in the
    # per-request cache.
    return bool(
        _memoize_on_g(
            ("privileged_workspace_role_holder", username),
            lambda: _workspace_role_assignment_esids(username, privileged_roles_only=True),
        )
    )


def _grant_scoped_titles(username: str) -> dict:
    """derive the active grant scopes for a privileged user.

    Returns a dict with keys:

    * ``team_titles``   — list of team_title strings (possibly empty)
    * ``org_titles``    — list of organization_title strings (possibly empty)
    * ``has_grants``    — bool: True if the user holds at least one
                          active grant (regardless of whether the join
                          resolved to any titles)

    An empty result (``has_grants=False``) means the privileged user
    holds no active grants and should see nothing. Callers translate
    this into either the deny-all sentinel filter, an empty allowed-list
    return, or a hard-block, per their contract.

    The org-grant → team-titles join mirrors the org_admin scope
    resolution at line 644+ below.
    """
    try:
        from web.services.data_grant_service import (
            DataGrantService,
            SCOPE_TYPE_ORG,
            SCOPE_TYPE_TEAM,
            SCOPE_TYPE_WORKSPACE,
        )
    except ImportError:
        from asset_management.web.services.data_grant_service import (
            DataGrantService,
            SCOPE_TYPE_ORG,
            SCOPE_TYPE_TEAM,
            SCOPE_TYPE_WORKSPACE,
        )

    grants = DataGrantService.list_for_user(username)
    has_grants = bool(grants)
    if not has_grants:
        return {"team_titles": [], "org_titles": [], "team_esids": [], "org_esids": [], "has_grants": False}

    org_esids: list[str] = []
    team_esids: list[str] = []
    # workspace-scoped grants. Resolved at query time via the
    # same workspace→orgs→teams join _workspace_scoped_titles uses, so a
    # grant survives org migrations cleanly — when team X moves to a new
    # workspace, the grant on workspace A stops resolving to X without
    # any rewrite.
    workspace_esids: list[str] = []
    for grant in grants:
        scope_type = grant.get("scope_type")
        esid = grant.get("scope_esid")
        if not esid:
            continue
        if scope_type == SCOPE_TYPE_ORG:
            org_esids.append(esid)
        elif scope_type == SCOPE_TYPE_TEAM:
            team_esids.append(esid)
        elif scope_type == SCOPE_TYPE_WORKSPACE:
            workspace_esids.append(esid)

    # Resolve ESIDs to titles via ES ``ids`` queries — the same pattern
    # ``_resolve_titles_from_user_role_index`` uses for the new-authz
    # org_admin / team_admin path. NOT ``get_org_titles_by_ids`` /
    # ``get_team_titles_by_ids`` — those are misnamed: despite the
    # ``_by_ids`` suffix, they match against the ``organization_title`` /
    # ``team_title`` text fields, designed for legacy ``org_<title>`` /
    # ``team_<title>`` role-name extraction. Our grant ``scope_esid`` is
    # the ES ``_id`` of the asset doc (matches the
    # ``de_authz_user_org_role.org_esid`` convention shipped with).
    es = get_elasticsearch_client()
    team_titles: list[str] = []
    org_titles: list[str] = []
    if es is not None and org_esids:
        try:
            resp = es.search(
                index="de_asset_organization",
                body={
                    "query": {"ids": {"values": org_esids}},
                    "_source": ["organization_title"],
                    "size": len(org_esids),
                },
            )
            org_titles = [
                hit["_source"]["organization_title"]
                for hit in resp.get("hits", {}).get("hits", [])
                if hit.get("_source", {}).get("organization_title")
            ]
        except Exception as e:
            logging.error("RoleFilterService._grant_scoped_titles: org join failed: %s", e)
        # expand org → teams by the unique organization_esid (the
        # grant's own scope_esid), not the collision-prone org title. A
        # grant on workspace A's "Acme" must not pull workspace B's "Acme"
        # teams. ``org_titles`` is still carried (above) for the title-keyed
        # data-layer filter; only the team expansion switches to ESID.
        if org_esids:
            team_titles.extend(RoleFilterService.get_team_titles_by_organization_esids(org_esids) or [])
    if es is not None and team_esids:
        try:
            resp = es.search(
                index="de_asset_teams",
                body={
                    "query": {"ids": {"values": team_esids}},
                    "_source": ["team_title"],
                    "size": len(team_esids),
                },
            )
            team_titles.extend(
                hit["_source"]["team_title"]
                for hit in resp.get("hits", {}).get("hits", [])
                if hit.get("_source", {}).get("team_title")
            )
        except Exception as e:
            logging.error("RoleFilterService._grant_scoped_titles: team join failed: %s", e)

    # workspace-scoped grants. Workspace → orgs join uses the
    # F5 ``workspace_esid`` field on ``de_asset_organization`` (same
    # path _workspace_scoped_titles takes — keeping the join symmetric
    # so a workspace migration affects grants and memberships
    # identically). Org → teams reuses the canonical helper so the
    # grant path inherits any future change to org-tier scope expansion.
    if es is not None and workspace_esids:
        ws_org_titles: list[str] = []
        ws_org_esids: list[str] = []
        try:
            resp = es.search(
                index="de_asset_organization",
                body={
                    "query": {"terms": {"workspace_esid": workspace_esids}},
                    "_source": ["organization_title"],
                    "size": 10000,
                },
            )
            for hit in resp.get("hits", {}).get("hits", []):
                title = hit.get("_source", {}).get("organization_title")
                if title:
                    ws_org_titles.append(title)
                    ws_org_esids.append(hit["_id"])
        except Exception as e:
            logging.error("RoleFilterService._grant_scoped_titles: workspace org join failed: %s", e)
        if ws_org_titles:
            org_titles.extend(ws_org_titles)
            # capture the org ESIDs from the workspace expansion
            # so the downstream workspace walk on UserScope.org_esids
            # resolves back to the same workspace correctly.
            org_esids.extend(ws_org_esids)
            # expand workspace-orgs → teams by the unique
            # organization_esid gathered just above, not the org title — a
            # same-titled org in another workspace would otherwise leak its
            # teams into a workspace-grant holder's scope.
            team_titles.extend(RoleFilterService.get_team_titles_by_organization_esids(ws_org_esids) or [])

    # De-duplicate while preserving order.
    def _dedup(values: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for v in values:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    return {
        "team_titles": _dedup(team_titles),
        "org_titles": _dedup(org_titles),
        # ESIDs from grant scope_esids (already unique by
        # data-grant contract). org_esids includes both direct org
        # grants AND workspace→orgs expansion below — the workspace-
        # walk consumer treats them uniformly.
        "team_esids": _dedup(list(team_esids)),
        "org_esids": _dedup(list(org_esids)),
        "has_grants": True,
    }


def _workspace_scoped_titles(username: str) -> dict:
    """/ F8: derive scope titles via the workspace tier.

    Forward-looking helper. NOT yet wired into
    ``get_filter_for_current_user`` — call sites will adopt this in a
    follow-up ticket once F7 produces workspace-typed memberships for
    organic signups. F8 ships it now so RoleFilterService has the
    surface available the moment a consumer needs it (is the
    likely first reader).

    Returns the same dict shape as ``_grant_scoped_titles`` and
    ``_membership_scoped_titles`` so the three can be unioned by a
    future ``_privileged_user_scope`` extension:

    * ``team_titles`` — list of team_title strings (possibly empty)
    * ``org_titles``  — list of organization_title strings (possibly empty)
    * ``has_workspace`` — bool: True if the user holds a workspace ROLE
                          for at least one workspace (regardless of
                          whether the join resolved to any orgs / teams)

    Joins ``de_authz_workspace_membership`` → ``de_asset_organization``
    (via the F5 ``workspace_esid`` field) → team_titles (via the
    existing ``get_team_titles_by_organization`` helper). The double
    join is the price of keeping the data layer team-keyed while the
    authz layer reaches up to workspace.

     (2026-06-01): the workspace ESIDs are role-gated before the
    join. A ``de_authz_workspace_membership`` doc is "belongs-to"
    metadata, NOT data authorization — the ROLE
    (``de_authz_user_workspace_role``) is. earlier this helper
    expanded EVERY membership workspace to its full org/team tree, so a
    user who had a workspace role assigned then revoked (revoke keeps
    the membership doc by design) kept full-workspace data
    scope with no active role. That is the cross-tenant leak reproduced
    on prod 2026-05-31 (team-scoped user saw all 32 teams / 20 orgs).
    We now intersect the membership workspaces with the set the user
    actually holds a role for (``_workspace_role_assignment_esids``,
    ``privileged_roles_only=False`` — a ``workspace_viewer`` /
    ``workspace_member`` viewer role legitimately confers whole-
    workspace read scope; bare membership with no role does not). A
    privileged ``workspace_admin`` / ``workspace_owner`` also has a role
    doc, so this gate never strips their workspace (no
    regression).

    Fail-soft: ES outage or missing/role-less workspace returns the
    empty-titles dict with ``has_workspace=False``. Callers wiring this
    in later can choose to deny-all on empty (privacy-first) or fall
    back to the legacy team/org scope (compat-first) — F8 does not
    impose a choice.
    """
    try:
        from web.services.workspace_membership_service import WorkspaceMembershipService
    except ImportError:
        from asset_management.web.services.workspace_membership_service import (
            WorkspaceMembershipService,
        )

    if not username:
        return {"team_titles": [], "org_titles": [], "org_esids": [], "has_workspace": False}

    membership_workspaces = WorkspaceMembershipService.get_workspaces_for_user(username)
    if not membership_workspaces:
        return {"team_titles": [], "org_titles": [], "org_esids": [], "has_workspace": False}

    # gate on the workspace ROLE. Only membership workspaces the
    # user actually holds a role for expand to the workspace-wide org/team
    # tree. Bare membership (e.g. left behind by revoke, which keeps the
    # membership doc) contributes nothing here.
    role_workspaces = set(_workspace_role_assignment_esids(username))
    workspace_esids = [ws for ws in membership_workspaces if ws in role_workspaces]
    if not workspace_esids:
        return {"team_titles": [], "org_titles": [], "org_esids": [], "has_workspace": False}

    es = get_elasticsearch_client()
    if es is None:
        return {"team_titles": [], "org_titles": [], "org_esids": [], "has_workspace": True}

    # Workspace → orgs: term-on-keyword scan of de_asset_organization.
    # F5 backfill guarantees every existing org has workspace_esid set.
    # capture the org ESIDs alongside titles so the downstream
    # workspace walk can re-derive the workspace via the unique _id
    # instead of risking a title collision with another workspace's
    # org.
    org_titles: list[str] = []
    org_esids: list[str] = []
    try:
        resp = es.search(
            index="de_asset_organization",
            body={
                "query": {"terms": {"workspace_esid": workspace_esids}},
                "_source": ["organization_title"],
                "size": 10000,
            },
        )
        for hit in resp.get("hits", {}).get("hits", []):
            title = hit.get("_source", {}).get("organization_title")
            if title:
                org_titles.append(title)
                org_esids.append(hit["_id"])
    except Exception as e:
        logging.error("RoleFilterService._workspace_scoped_titles: org join failed: %s", e)

    # Orgs → teams: join on the unique organization_esid gathered
    # from the workspace walk above, not the org title. The workspace tier
    # is exactly where same-title cross-workspace collisions surface (two
    # wizard signups each create a "Kavanah"/"General" org), so the title
    # join here was the live-firing leak.
    team_titles: list[str] = []
    if org_esids:
        team_titles = RoleFilterService.get_team_titles_by_organization_esids(org_esids) or []

    # De-duplicate while preserving order — same shape as the sibling
    # helpers above.
    def _dedup(values: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for v in values:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    return {
        "team_titles": _dedup(team_titles),
        "org_titles": _dedup(org_titles),
        "org_esids": _dedup(org_esids),
        "has_workspace": True,
    }


# (2026-05-26): workspace role_esids that confer privileged-
# resolver status. Distinct from workspace_member, which is a viewer-
# tier role and must NOT trigger the privileged code path. Without
# this filter, writing a workspace_member row over-promoted the user
# to the full workspace's org/team scope (privilege escalation
# reproduced on prod 2026-05-26 while attempting a backfill).
_PRIVILEGED_WORKSPACE_ROLES = frozenset({"workspace_admin", "workspace_owner"})


def _workspace_role_assignment_esids(username: str, *, privileged_roles_only: bool = False) -> list[str]:
    """follow-up: workspace ESIDs the user has any role assignment for.

    Why: the 2026-05-18 smoke test surfaced a user (``org_user``) with a
    ``workspace_admin`` doc in ``de_authz_user_workspace_role`` and ZERO
    docs in ``de_authz_workspace_membership`` / ``de_authz_org_membership``
    / ``de_authz_team_membership``. ``_workspace_scoped_titles`` only
    reads the membership index, so this user resolved to ``is_empty``
    and saw nothing.

     Create User modal writes the role assignment but does NOT
    write a parallel membership doc. The resolver needs to treat the
    role-assignment index as a fourth scope source so a workspace_admin
    can administer their workspace without also being listed as a
    member. (Long-term we may want write paths to keep the two indices
    consistent, but the resolver fix unblocks today.)

    Reads ``de_authz_user_workspace_role`` directly — this is the only
    consumer outside the WorkspaceMembershipService.get_role path, so
    there's no helper to call yet.

     (2026-05-26): ``privileged_roles_only=True`` filters the
    result to rows whose ``role_esid`` is in
    ``_PRIVILEGED_WORKSPACE_ROLES``. ``_user_is_privileged`` passes the
    flag; the resolver's scope-resolution code path passes False so a
    workspace_member assignment still contributes workspace ESIDs to
    UserScope (the user is a member of the workspace, just not
    privileged enough to bypass scope filtering). Default False
    preserves the earlier behavior for any other caller; only the
    privileged-check switches to True.

    Returns an empty list on ES outage or no docs — fail-soft like
    every other ``_*_scoped_*`` helper in this module.
    """
    if not username:
        return []
    es = get_elasticsearch_client()
    if es is None:
        return []
    try:
        from web.services.permissions_index_service import USER_WORKSPACE_ROLE_INDEX
    except ImportError:
        from asset_management.web.services.permissions_index_service import (
            USER_WORKSPACE_ROLE_INDEX,
        )
    source_fields = ["workspace_esid"]
    if privileged_roles_only:
        source_fields.append("role_esid")
    try:
        resp = es.search(
            index=USER_WORKSPACE_ROLE_INDEX,
            body={
                "query": {"term": {"username": username}},
                "_source": source_fields,
                "size": 100,
            },
        )
    except Exception as e:
        logging.warning("_workspace_role_assignment_esids(%s): %s", username, e)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        ws = src.get("workspace_esid")
        if not ws or ws in seen:
            continue
        if privileged_roles_only:
            role = src.get("role_esid")
            if role not in _PRIVILEGED_WORKSPACE_ROLES:
                continue
        seen.add(ws)
        out.append(ws)
    return out


def _membership_scoped_titles(username: str) -> dict:
    """derive scope titles from the user's
    ``de_authz_user_team_role`` and ``de_authz_user_org_role`` memberships,
    regardless of role. Returns the same dict shape as
    ``_grant_scoped_titles`` (``team_titles`` / ``org_titles``) so the
    two can be unioned by ``_privileged_user_scope``.

    Wraps ``RoleFilterService._resolve_titles_from_user_role_index`` —
    the same ES query path the regular org-admin / team-admin scope
    resolution uses for non-privileged users. The difference is that
     calls this path for privileged users too (asset_admin /
    superuser with explicit memberships) so a single asset_admin with a
    NORTH membership sees NORTH data.

    System-admin sentinel docs (team_esid=``__all__``) are intentionally
    NOT a membership for this purpose — retired the sentinel as
    a data-access signal. A user with only the sentinel and no real
    membership doc is still a bare privileged user.
    """
    if not username:
        return {"team_titles": [], "org_titles": [], "team_esids": [], "org_esids": []}

    # capture ESIDs alongside titles so the resolver can store
    # them on UserScope and downstream workspace-walk helpers can join
    # on the unique _id instead of the non-unique title.
    team_esids, team_titles = RoleFilterService._resolve_esids_and_titles_from_user_role_index(
        index="de_authz_user_team_role",
        esid_field="team_esid",
        title_index="de_asset_teams",
        title_field="team_title",
        username=username,
    )
    org_esids, org_titles = RoleFilterService._resolve_esids_and_titles_from_user_role_index(
        index="de_authz_user_org_role",
        esid_field="org_esid",
        title_index="de_asset_organization",
        title_field="organization_title",
        username=username,
    )

    # Org memberships also expand to all teams under each org — mirrors
    # the regular org-admin scope resolution at line 754+. Without this
    # an asset_admin with an org-only membership would see no teams.
    # this expansion now joins on the unique ``org_esids`` from
    # de_authz_user_org_role (above), not the org title — a same-titled
    # org in another workspace would otherwise contribute its teams'
    # titles to this user's data-layer filter set. Teams reached only via
    # this org expansion still contribute their TITLE (not their ESID) for
    # data-layer filtering; the workspace-walk path remains driven by the
    # ``team_esids`` / ``org_esids`` captured above.
    if org_esids:
        team_titles = list(team_titles) + (RoleFilterService.get_team_titles_by_organization_esids(org_esids) or [])

    def _dedup(values: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for v in values:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    return {
        "team_titles": _dedup(team_titles),
        "org_titles": _dedup(org_titles),
        "team_esids": team_esids,
        "org_esids": org_esids,
    }


def _privileged_user_scope(username: str) -> dict:
    """compose memberships UNION grants for a privileged user.

    The load-bearing semantic decision from 2026-05-15: grants are
    additive on top of standing memberships, not override and not
    fallback. When a grant expires, the user keeps their
    membership-derived access (no surprise loss).

    Returns:
        dict with ``team_titles`` / ``org_titles`` (de-duplicated unions)
        and ``has_scope`` (True iff at least one membership or grant
        resolved to a title).

    The downstream caller decides what to do with an empty result —
    typically: deny-all filter for query callers; empty list for
    ``get_allowed_*`` callers (which is the existing contract).
    """
    memberships = _membership_scoped_titles(username)
    grants = _grant_scoped_titles(username)

    def _dedup_union(left: list[str], right: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for v in list(left) + list(right):
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    team_titles = _dedup_union(memberships["team_titles"], grants["team_titles"])
    org_titles = _dedup_union(memberships["org_titles"], grants["org_titles"])

    return {
        "team_titles": team_titles,
        "org_titles": org_titles,
        "has_scope": bool(team_titles or org_titles),
    }


def _filter_from_privileged_scope(username: str):
    """derive a team_title filter from the union of memberships
    and active grants for a privileged user. Returns one of:

    * ``_DENY_ALL_FILTER`` — privileged user with zero memberships AND
      zero active grants (or both resolved to no teams). Sees no data.
    * a term / terms filter on team_title — privileged user with one or
      more memberships / grants; query is scoped to the de-duplicated
      union of their teams.

    Renamed (and re-scoped) ``_filter_from_grants``:
    the same shape and short-circuit logic, but the inner resolution
    now consults memberships too. Expired grants are filtered out by
    ``DataGrantService.list_for_user``.
    """
    resolved = _privileged_user_scope(username)
    titles = resolved["team_titles"]
    if not titles:
        # Either no memberships and no grants, or both resolved to no
        # teams (e.g. an org with no teams yet, a membership doc with
        # an esid that no longer exists).
        return _DENY_ALL_FILTER
    if len(titles) == 1:
        return {"term": {"team_title": titles[0]}}
    return {"terms": {"team_title": titles}}


# alias kept for compatibility with existing callers and tests
# that import ``_filter_from_grants`` directly. New code should use
# ``_filter_from_privileged_scope`` which makes the composition
# explicit in its name.
_filter_from_grants = _filter_from_privileged_scope


# (2026-05-28) — _legacy_es_security_titles + _LEGACY_*_ROLE_RE
# regex constants retired. They were the resolver's union source for
# hybrid users (legacy session["roles"] team_<title>/org_<title> strings
# whose new-authz docs hadn't been backfilled — 6-user
# cohort). The backfill (bin/python/tools/backfill_legacy_authz_users.py)
# closes that cohort on prod before this commit merges to main.
# extract_org_ids_from_roles / extract_team_ids_from_roles still parse
# the same string format via _LEGACY_ORG_ROLE_RE / _LEGACY_TEAM_ROLE_RE,
# kept below for those public-API helpers (~25 callers).
_LEGACY_ORG_ROLE_RE = re.compile(r"^org_(.+)$")
_LEGACY_TEAM_ROLE_RE = re.compile(r"^team_(.+)$")


def _workspace_esids_from_scope_titles(
    org_titles: tuple[str, ...] | list[str], team_titles: tuple[str, ...] | list[str]
) -> set[str]:
    """follow-up: derive workspace_esids by walking org/team docs up to their parent workspace.

    Why this exists: ``UserScope.workspace_esids`` is populated ONLY by
    ``_workspace_scoped_titles`` (which reads
    ``de_authz_workspace_membership``). That misses two important cases
    that surfaced in the 2026-05-18 smoke test:

    1. **Symptom 1** — a user with a ``de_authz_user_workspace_role``
       assignment but no membership doc. Their org/team-tier
       memberships still resolve correctly, but the workspace ESID is
       never derived, so filter intersects asset docs with
       ``set()`` and returns nothing.
    2. **Symptom 2** — a privileged user (superuser / system_admin)
       with a team-scoped or org-scoped data grant. ``_grant_scoped_titles``
       returns the granted titles, but it does NOT also surface the
       workspace_esid of those teams/orgs back to the resolver.

    The fix: after the resolver has produced its team/org titles, walk
    those titles up to the parent workspace via
    ``de_asset_organization.workspace_esid`` (backfill
    guarantees this is set) and ``de_asset_teams.organization_esid``
    (multi-org list). The result is the **inferred** workspace set —
    union it with ``UserScope.workspace_esids`` and you get the
    complete view of "every workspace this user has resolvable access
    to via any scope source."

    The helper is module-level rather than a UserScope method so it can
    be called from ``get_allowed_workspaces`` without invalidating the
    frozen-dataclass guarantee. Returns an empty set on ES outage or
    empty inputs.
    """
    if not org_titles and not team_titles:
        return set()

    es = get_elasticsearch_client()
    if es is None:
        return set()

    workspace_esids: set[str] = set()

    # Org → workspace: one ES round trip resolves every relevant org's
    # workspace_esid. terms-on-keyword over organization_title.
    org_titles_list = list(org_titles)
    if org_titles_list:
        try:
            resp = es.search(
                index="de_asset_organization",
                body={
                    "query": {"terms": {"organization_title": org_titles_list}},
                    "_source": ["workspace_esid"],
                    "size": 10000,
                },
            )
            for hit in resp.get("hits", {}).get("hits", []):
                ws = hit.get("_source", {}).get("workspace_esid")
                if ws:
                    workspace_esids.add(ws)
        except Exception as e:
            logging.warning("_workspace_esids_from_scope_titles: org lookup failed: %s", e)

    # Team → org → workspace: teams carry organization_esid as a list-or-
    # string. Pull the team docs, collect every referenced org ESID, then
    # one more terms-on-_id query against de_asset_organization to get
    # the workspace_esid set. Two round trips total regardless of how
    # many teams the user has — same shape uses for workspace-
    # scoped grant resolution.
    team_titles_list = list(team_titles)
    if team_titles_list:
        try:
            team_resp = es.search(
                index="de_asset_teams",
                body={
                    "query": {"terms": {"team_title": team_titles_list}},
                    "_source": ["organization_esid"],
                    "size": 10000,
                },
            )
            org_esids: set[str] = set()
            for hit in team_resp.get("hits", {}).get("hits", []):
                raw = hit.get("_source", {}).get("organization_esid")
                if isinstance(raw, str) and raw:
                    org_esids.add(raw)
                elif isinstance(raw, list):
                    for v in raw:
                        if v:
                            org_esids.add(v)
            if org_esids:
                org_resp = es.search(
                    index="de_asset_organization",
                    body={
                        "query": {"ids": {"values": list(org_esids)}},
                        "_source": ["workspace_esid"],
                        "size": len(org_esids),
                    },
                )
                for hit in org_resp.get("hits", {}).get("hits", []):
                    ws = hit.get("_source", {}).get("workspace_esid")
                    if ws:
                        workspace_esids.add(ws)
        except Exception as e:
            logging.warning("_workspace_esids_from_scope_titles: team→org→workspace lookup failed: %s", e)

    return workspace_esids


def _workspace_esids_from_scope_esids(
    org_esids: frozenset[str] | set[str] | list[str] | tuple[str, ...],
    team_esids: frozenset[str] | set[str] | list[str] | tuple[str, ...],
) -> set[str]:
    """(2026-05-26): ESID-based replacement for
    ``_workspace_esids_from_scope_titles``. Joins on the unique
    ``_id`` field of ``de_asset_organization`` and ``de_asset_teams``
    instead of on the non-unique ``organization_title`` /
    ``team_title`` fields.

    The title-based helper has a load-bearing bug: two wizard signups
    both create a "General" team in their own workspaces. Joining on
    ``team_title=["General"]`` walks BOTH teams up to their respective
    workspaces and returns the union — a cross-tenant data leak
    surfaced once let the wizard-cohort reach
    ``get_allowed_workspaces`` at all.

    Same two-leg traversal as the title-based helper:

    1. Org → workspace via ``de_asset_organization.workspace_esid``.
    2. Team → org → workspace via ``de_asset_teams.organization_esid``
       (list-or-string) → ``de_asset_organization.workspace_esid``.

    Returns an empty set on ES outage or empty inputs (fail-soft,
    matches the title-based helper's contract). The title-based helper
    is retained for any consumer that still passes titles, but new
    code should always prefer this one.
    """
    if not org_esids and not team_esids:
        return set()

    es = get_elasticsearch_client()
    if es is None:
        return set()

    workspace_esids: set[str] = set()

    # Org → workspace: one ES round trip via ``ids`` (terms on _id is
    # not supported; ``ids`` is the canonical shape).
    org_esid_list = list(org_esids) if org_esids else []
    if org_esid_list:
        try:
            resp = es.search(
                index="de_asset_organization",
                body={
                    "query": {"ids": {"values": org_esid_list}},
                    "_source": ["workspace_esid"],
                    "size": len(org_esid_list),
                },
            )
            for hit in resp.get("hits", {}).get("hits", []):
                ws = hit.get("_source", {}).get("workspace_esid")
                if ws:
                    workspace_esids.add(ws)
        except Exception as e:
            logging.warning("_workspace_esids_from_scope_esids: org-by-id lookup failed: %s", e)

    # Team → org → workspace: same two round trips as the title-based
    # helper, but the first one is an ``ids`` query against
    # de_asset_teams. organization_esid may be a list (multi-org-per-
    # team) or string.
    #
    # also read the team's OWN ``workspace_esid`` and add it
    # directly (heal-on-read). The org leg dead-ends when a team's
    # ``organization_esid`` refs point at orgs that no longer exist (an
    # org-delete cascade gap left them dangling). When that happens the
    # graph walk never reaches the workspace and graph-walking surfaces
    # (Asset Explorer, Data Inventory) hide a workspace whose only team is
    # org-orphaned. The team doc carries the authoritative
    # ``workspace_esid`` (the boundary field, reliably keyword
    # ), so a team in the user's resolved scope surfaces its
    # workspace even when its org links are broken. This is a STRICT
    # superset of the org-walk result — it never widens beyond a workspace
    # the user already has a scoped team in — so it introduces no
    # cross-tenant leak.
    team_esid_list = list(team_esids) if team_esids else []
    if team_esid_list:
        try:
            team_resp = es.search(
                index="de_asset_teams",
                body={
                    "query": {"ids": {"values": team_esid_list}},
                    "_source": ["organization_esid", "workspace_esid"],
                    "size": len(team_esid_list),
                },
            )
            walked_org_esids: set[str] = set()
            for hit in team_resp.get("hits", {}).get("hits", []):
                src = hit.get("_source", {})
                # Heal-on-read direct leg: the team's own workspace_esid.
                direct_ws = src.get("workspace_esid")
                if direct_ws:
                    workspace_esids.add(direct_ws)
                raw = src.get("organization_esid")
                if isinstance(raw, str) and raw:
                    walked_org_esids.add(raw)
                elif isinstance(raw, list):
                    for v in raw:
                        if v:
                            walked_org_esids.add(v)
            if walked_org_esids:
                org_resp = es.search(
                    index="de_asset_organization",
                    body={
                        "query": {"ids": {"values": list(walked_org_esids)}},
                        "_source": ["workspace_esid"],
                        "size": len(walked_org_esids),
                    },
                )
                for hit in org_resp.get("hits", {}).get("hits", []):
                    ws = hit.get("_source", {}).get("workspace_esid")
                    if ws:
                        workspace_esids.add(ws)
        except Exception as e:
            logging.warning("_workspace_esids_from_scope_esids: team→org→workspace lookup failed: %s", e)

    return workspace_esids


def resolve_user_scope(username: str, user_roles: list[str], config: dict) -> "UserScope":
    """Phase A: return the user's effective data scope as a single
    structured ``UserScope`` value.

    This is the **single source of truth** for "what data can this user
    see?" — every consumer helper (``should_apply_filter``,
    ``get_allowed_organizations``, ``get_allowed_teams_for_org_users``,
    ``get_filter_for_current_user``) reads from this resolver. earlier
    each helper walked its own fall-through; surfaced the
    divergence by trying to patch each chain to add workspace-tier
    fall-through and getting it wrong four times in a row.

    **Source order (additive within tier — UNION, not first-wins):**

    * For **privileged** users (system-admin / unrestricted-role /
      asset_admin): UNION of memberships
      (``de_authz_user_team_role`` + ``de_authz_user_org_role``) +
      data grants (``de_authz_data_grant``) + workspace memberships
      (``de_authz_workspace_membership``). This is the contract
      extended with the workspace tier called for.
    * For **non-privileged** users: ``de_authz_user_team_role`` and
      ``de_authz_user_org_role`` are the sole scope source.
      (2026-05-28) retired the legacy ES Security regex union that
      used to layer ``team_<title>`` / ``org_<title>`` strings from
      ``session["roles"]`` on top; every user is on memberships now.

    **Returns:** a ``UserScope`` with team_titles / org_titles /
    workspace_esids / allowed_indices set. Phase B (2026-05-15)
    populates ``allowed_indices`` as the ``{team_esid}__{source}``
    frozenset for Layer-1 index-name enforcement. ``scope_source``
    is ``'workspace_membership'`` / ``'data_grant'`` /
    ``'membership'`` / ``'none'`` — the
    name of the first source that contributed.

    **``scope_source='none'`` is the privacy signal.** A user with no
    scope from any source gets an empty ``UserScope``; consumers map
    that to the deny-all sentinel (data filter) or empty lists (allow
    lists). The dataclass itself is **never None** — emptiness is
    expressed by empty tuples, not by a missing object.

    Memoized per request via ``_memoize_on_g``.
    """
    cache_key = ("user_scope", username, tuple(user_roles or ()))
    return _memoize_on_g(cache_key, lambda: _resolve_user_scope_uncached(username, user_roles, config))


def _resolve_user_scope_uncached(username: str, user_roles: list[str], config: dict) -> "UserScope":
    """Uncached body of ``resolve_user_scope``. Exposed for tests that
    want to bypass the per-request memoization."""
    privileged = _user_is_privileged(user_roles, username, config)

    team_titles: list[str] = []
    org_titles: list[str] = []
    workspace_esids: list[str] = []
    # (2026-05-26): track ESIDs alongside titles. The
    # workspace walk in get_allowed_workspaces now joins on these
    # unique ``_id`` values instead of the non-unique titles, closing
    # the cross-workspace leak (two wizard signups both create a
    # "General" team — joining on team_title pulled BOTH teams up to
    # their respective workspaces).
    team_esids: list[str] = []
    org_esids: list[str] = []
    sources: list[str] = []

    if privileged:
        # (2026-05-15): privileged users get memberships UNION
        # active grants. extends this with the workspace tier
        # so a workspace_admin's data scope reaches
        # every team in their workspace's orgs without needing an
        # explicit per-team grant.
        memberships = _membership_scoped_titles(username)
        grants = _grant_scoped_titles(username)
        workspace = _workspace_scoped_titles(username)

        team_titles = _dedup_union(memberships["team_titles"], grants["team_titles"], workspace["team_titles"])
        org_titles = _dedup_union(memberships["org_titles"], grants["org_titles"], workspace["org_titles"])
        # union the ESID half of each source's return dict.
        # ``_workspace_scoped_titles`` exposes ``org_esids`` (no
        # ``team_esids`` — it expands orgs→teams via title which is
        # safe at the data-layer filter but doesn't seed the workspace
        # walk; the org_esids alone re-derive the workspace).
        team_esids = _dedup_union(memberships.get("team_esids") or [], grants.get("team_esids") or [])
        org_esids = _dedup_union(
            memberships.get("org_esids") or [],
            grants.get("org_esids") or [],
            workspace.get("org_esids") or [],
        )
        if workspace.get("has_workspace"):
            try:
                from web.services.workspace_membership_service import WorkspaceMembershipService
            except ImportError:
                from asset_management.web.services.workspace_membership_service import (
                    WorkspaceMembershipService,
                )
            workspace_esids = list(WorkspaceMembershipService.get_workspaces_for_user(username))

        # follow-up: a user with a de_authz_user_workspace_role
        # assignment but no membership doc must still resolve to that
        # workspace. user-create writes the role; membership
        # docs are written by the wizard's onboarding path, which the
        # admin-created user never runs. Union the two so the resolver
        # treats role assignment as a first-class scope source. This
        # mirrors the membership-grant union pattern above; absent this,
        # ``org_user`` (smoke-test 2026-05-18) had no resolvable scope
        # at any tier and saw zero assets.
        role_workspaces = _workspace_role_assignment_esids(username)
        if role_workspaces:
            seen_ws: set[str] = set(workspace_esids)
            for ws in role_workspaces:
                if ws not in seen_ws:
                    workspace_esids.append(ws)
                    seen_ws.add(ws)

        # Source naming follows resolution order so the privacy audit
        # log can tell at a glance WHICH source contributed. Workspace
        # is the forward-looking primary; membership stays first when
        # both contributed because that's the path the legacy tests
        # pin behavior against.
        if memberships["team_titles"] or memberships["org_titles"]:
            sources.append("membership")
        if grants.get("has_grants"):
            sources.append("data_grant")
        if workspace.get("has_workspace"):
            sources.append("workspace_membership")
        if role_workspaces and not workspace.get("has_workspace"):
            # Distinct source name so the audit log distinguishes
            # "scope came from a workspace_admin role assignment" from
            # "scope came from explicit membership." Only append when
            # the membership leg did NOT already contribute — avoids
            # double-counting when finally writes both indices.
            sources.append("workspace_role_assignment")
    else:
        # (2026-05-26): the non-privileged branch was historically
        # the legacy-ES-Security-only path: it walked ``session["roles"]``
        # for ``team_<title>`` / ``org_<title>`` regex matches and stopped
        # there. That branch silently broke for the new-authz cohort
        # (users created via Edit Roles / wizard / org invite, whose
        # role docs live in ``de_authz_user_team_role`` /
        # ``de_authz_user_org_role`` but whose ES Security
        # ``session["roles"]`` is empty). Six prod beta-tester users were
        # blocked from dashboards entirely.
        #
        # Fix: when the new-permissions cutover is in effect, read the
        # same new-authz indexes the auto-routing
        # ``extract_*_from_roles`` helpers consult. Layer legacy on top
        # so the legacy and new-authz cohorts compose cleanly during the
        # extended cutover window (a hybrid user with both sources
        # contributes from both — same union semantics the privileged
        # branch uses).
        #
        # Workspace membership and workspace-role assignments are
        # additionally consulted so a non-privileged user invited at the
        # workspace tier resolves to their workspace's orgs / teams
        # (mirrors the privileged branch's workspace handling — same
        # tier surface, same resolution path). Without this,
        # 6 broken users would still have empty workspace_esids and the
        # workspace gate downstream would drop their dashboards.
        new_authz_teams: list[str] = []
        new_authz_orgs: list[str] = []
        new_authz_workspaces: list[str] = []
        # (2026-05-26): ESID accumulators for the non-privileged
        # branch — same shape as the privileged branch above.
        new_authz_team_esids: list[str] = []
        new_authz_org_esids: list[str] = []
        new_authz_source: str | None = None
        if username:
            # (2026-05-28): the ``_use_new_permissions`` gate
            # that used to wrap this block was retired with the
            # ``USE_NEW_PERMISSIONS`` flag. New-authz reads are the only
            # path now; the legacy regex still contributes via
            # ``_legacy_es_security_titles`` at the union below
            #.
            #
            # NOTE: route through the explicit-username helper rather
            # than the session-reading ``_team_titles_from_memberships``
            # / ``_org_titles_from_memberships``. The resolver is called
            # from non-request contexts too (CLI tooling, release-plan
            # tests via _resolve_user_scope_uncached); the session-reading
            # helpers raise RuntimeError outside a Flask request context.
            # ``_membership_scoped_titles`` already encapsulates this
            # pattern correctly — use it.
            memberships = _membership_scoped_titles(username)
            # ``_membership_scoped_titles`` already expands org
            # memberships to their teams internally (see its own
            # org_titles → get_team_titles_by_organization call). Do
            # NOT re-expand here; mirrors the privileged-branch usage
            # at line 862 above.
            new_authz_teams = list(memberships["team_titles"])
            new_authz_orgs = list(memberships["org_titles"])
            new_authz_team_esids = list(memberships.get("team_esids") or [])
            new_authz_org_esids = list(memberships.get("org_esids") or [])
            workspace = _workspace_scoped_titles(username)
            if workspace.get("has_workspace"):
                new_authz_teams.extend(workspace["team_titles"])
                new_authz_orgs.extend(workspace["org_titles"])
                new_authz_org_esids.extend(workspace.get("org_esids") or [])
                try:
                    from web.services.workspace_membership_service import WorkspaceMembershipService
                except ImportError:
                    from asset_management.web.services.workspace_membership_service import (
                        WorkspaceMembershipService,
                    )
                new_authz_workspaces = list(WorkspaceMembershipService.get_workspaces_for_user(username))
            # Role-assignment-only workspaces:
            # a non-privileged user with a workspace_member assignment
            # but no membership doc still belongs to that workspace.
            role_workspaces = _workspace_role_assignment_esids(username)
            seen_ws: set[str] = set(new_authz_workspaces)
            for ws in role_workspaces:
                if ws not in seen_ws:
                    new_authz_workspaces.append(ws)
                    seen_ws.add(ws)
            if new_authz_teams or new_authz_orgs or new_authz_workspaces:
                if workspace.get("has_workspace"):
                    new_authz_source = "workspace_membership"
                elif role_workspaces and not (new_authz_teams or new_authz_orgs):
                    new_authz_source = "workspace_role_assignment"
                else:
                    new_authz_source = "membership"

        # (2026-05-28) — Legacy ES-Security regex union retired.
        # The hybrid cohort was empty on staging (0 of 24 users)
        # and the prod backfill (bin/python/tools/backfill_legacy_authz_users.py)
        # is run before this branch merges to main. New-authz reads are
        # the sole source of scope on the non-privileged branch from
        # this commit onward.
        team_titles = list(new_authz_teams)
        org_titles = list(new_authz_orgs)
        workspace_esids = list(new_authz_workspaces)
        team_esids = list(new_authz_team_esids)
        org_esids = list(new_authz_org_esids)
        if new_authz_source:
            sources.append(new_authz_source)

    scope_source = sources[0] if sources else "none"
    # Phase B: compute the index-name allowlist for Layer-1
    # enforcement. Empty team_titles → empty allowlist → Layer-1 returns
    # the no-match sentinel for every wrapped es.search call. This is
    # the structural complement to the team_title body-filter Layer-2:
    # a leak now requires BOTH layers to fail.
    allowed_indices = _build_allowed_indices(team_titles)
    return UserScope(
        workspace_esids=tuple(workspace_esids),
        org_titles=tuple(org_titles),
        team_titles=tuple(team_titles),
        # ESIDs used by ``get_allowed_workspaces`` to perform
        # the workspace walk against the unique ``_id`` field instead
        # of the non-unique ``team_title`` / ``organization_title``
        # text fields.
        team_esids=frozenset(team_esids),
        org_esids=frozenset(org_esids),
        allowed_indices=allowed_indices,
        scope_source=scope_source,
    )


def _resolve_team_esids_from_titles(team_titles: list[str]) -> list[str]:
    """Phase B: resolve a list of ``team_title`` strings to their
    ``team_esid`` document IDs via a single ES round-trip against
    ``de_asset_teams``.

    The resolver carries titles through every source helper (memberships /
    grants / workspace / legacy) because the four consumer helpers and
    Layer-2 body filter all key on ``team_title``. Phase B's Layer-1 index
    allowlist needs ESIDs to build ``{team_esid}__{source}`` names — this
    helper bridges the two representations.

    Returns ``[]`` on empty input, ES outage, or no matches — the resolver
    treats an empty ESID list as "no index allowlist" and the caller's
    Layer-1 enforcement falls through to the empty-scope sentinel. The
    fail-soft mirrors the rest of the role_filter_service contract (every
    join helper here degrades to empty list on ES failure).

    Args:
        team_titles: List of canonical team_title strings (the resolver's
            ``team_titles`` union output). De-duplication is the caller's
            responsibility — this helper just joins.

    Returns:
        List of team_esid strings (ES ``_id`` values). Order is not
        guaranteed; the index-name builder doesn't care.
    """
    if not team_titles:
        return []
    es = get_elasticsearch_client()
    if es is None:
        logging.warning("_resolve_team_esids_from_titles: ES unavailable; returning empty ESID list")
        return []

    # query BOTH `team_title.keyword` AND raw `team_title` because
    # `de_asset_teams.team_title` is mapped two different ways across
    # clusters/age cohorts. Older indices have plain `keyword` (no `.keyword`
    # subfield) so the `.keyword` path returns 0; newer indices have
    # `text` + `.keyword` subfield, so the raw path matches the
    # analyzer instead of the keyword. ES refuses to convert keyword→text
    # in-place, so the only forward-compatible fix is to OR both shapes.
    titles_list = list(team_titles)
    query = {
        "bool": {
            "should": [
                {"terms": {"team_title.keyword": titles_list}},
                {"terms": {"team_title": titles_list}},
            ],
            "minimum_should_match": 1,
        }
    }
    try:
        resp = es.search(
            index="de_asset_teams",
            body={
                "query": query,
                "_source": False,
                "size": max(len(team_titles), 1),
            },
        )
    except Exception as e:
        # Fail-soft: log + return empty so Layer-1 denies rather than
        # silently falling back to a wider pattern. Layer-2 body filter
        # still gates on team_title — defense in depth.
        logging.error("_resolve_team_esids_from_titles: de_asset_teams join failed: %s", e)
        return []
    return [hit["_id"] for hit in resp.get("hits", {}).get("hits", []) if hit.get("_id")]


def _build_allowed_indices(team_titles: list[str]) -> frozenset[str]:
    """Phase B / compute the frozenset of concrete ES
    index names a user may read, given their resolved ``team_titles``.

    Emits TWO naming conventions in the same allowlist:

    1. ``{team_esid}__{source}`` — the ESID-keyed naming that
       Phase B was designed for. Matches ``build_data_index_name``.
    2. ``{sanitize_team_title(team_title)}_{source}`` — the legacy
       team-slug naming the importers still write to today (is
       paused; see project_paused.md). Single underscore
       separator, no esid.

    Both branches are emitted unconditionally so the allowlist is a
    superset across both naming schemes. ``scoped_index_pattern``
    intersects with the wildcard the caller passed, so emitting extra
    names that don't match the wildcard is a no-op. (2026-05-16)
    added the legacy branch after discovering on staging that the actual
    data indices were exclusively legacy-slug-named and every
    grant-bearing user got "No data" because Phase B's allowlist
    intersection returned a comma-list of nonexistent ESID-keyed names.

    Once every importer is on the scheme, drop the legacy
    branch (and this comment). Until then both names are valid targets.

    Returns an empty frozenset when ``team_titles`` is empty — that's
    the deny-everything signal at the Layer-1 boundary. The dataclass
    field is ``frozenset`` so UserScope stays hashable / immutable.
    """
    if not team_titles:
        return frozenset()
    # ESID-keyed names.
    esids = _resolve_team_esids_from_titles(team_titles)
    esid_names = {f"{esid}__{source}".lower() for esid in esids for source in KNOWN_SOURCES}
    # Legacy team-slug names. Computed from titles directly — no ES
    # round-trip, fail-soft on a bad title via sanitize_team_title's
    # "unknown" fallback. The "unknown_*" name would not match a real
    # index, so it can't accidentally widen scope.
    try:
        from lib.es_helpers import sanitize_team_title
    except ImportError:
        # Fallback if the import path differs in a test/CLI context;
        # bias toward an empty legacy set rather than crashing the
        # resolver, since ESID names will still cover the path.
        sanitize_team_title = None  # type: ignore[assignment]
    legacy_names: set[str] = set()
    if sanitize_team_title is not None:
        for title in team_titles:
            slug = sanitize_team_title(title)
            if not slug or slug == "unknown":
                continue
            for source in KNOWN_SOURCES:
                legacy_names.add(f"{slug}_{source}".lower())
    return frozenset(esid_names | legacy_names)


def _dedup_union(*lists: list[str]) -> list[str]:
    """Order-preserving de-duplicated union of N lists. Used by the
    resolver to merge titles from independent sources without disturbing
    the existing ordering callers rely on (the legacy tests pin
    list-order in a few places)."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for v in lst or []:
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out


def _memoize_on_g(key, compute):
    """Per-request memoization on Flask ``g``. The new-model lookup
    queries ES for memberships + title resolution; on a single request
    that hits N dashboard endpoints the lookup must run once, not N
    times. Falls through to direct compute when ``g`` is unavailable
    (e.g. unit tests without a request context)."""
    try:
        from flask import g
    except Exception:
        return compute()
    try:
        cache = getattr(g, "_role_filter_memo", None)
        if cache is None:
            cache = {}
            g._role_filter_memo = cache
        if key not in cache:
            cache[key] = compute()
        return cache[key]
    except RuntimeError:
        # Outside request context.
        return compute()


class RoleFilterService:
    """Service for resolving role-based Elasticsearch filters."""

    _config_cache = None
    _config_path = "/app/reporting_configs/role_org_mapping.yaml"

    @classmethod
    def load_config(cls):
        """
        Load role mapping configuration from YAML file.

        Returns:
            dict: Configuration dictionary with role_mappings and unrestricted_roles

        Raises:
            FileNotFoundError: If config file doesn't exist
            yaml.YAMLError: If config file is invalid
        """
        if cls._config_cache is None:
            if not os.path.exists(cls._config_path):
                logging.warning(f"Role mapping config not found: {cls._config_path}")
                # Return default config if file doesn't exist
                cls._config_cache = {
                    "role_mappings": {},
                    "unrestricted_roles": ["superuser"],
                }
            else:
                try:
                    with open(cls._config_path, "r") as f:
                        cls._config_cache = yaml.safe_load(f)
                    logging.info(f"Loaded role mapping config from {cls._config_path}")
                except yaml.YAMLError as e:
                    logging.error(f"Error parsing YAML config: {e}")
                    # Return default config on parse error
                    cls._config_cache = {
                        "role_mappings": {},
                        "unrestricted_roles": ["superuser"],
                    }

        return cls._config_cache

    @classmethod
    def reload_config(cls):
        """Force reload of configuration file (clears cache)."""
        cls._config_cache = None
        return cls.load_config()

    @classmethod
    def extract_org_ids_from_roles(cls, user_roles):
        """Pure-regex parse of legacy ``org_<title>`` strings from
        ``user_roles``.

        Roles follow the convention: ``org_<organization_title>`` (e.g.
        ``org_ACME`` -> returns ``"ACME"``).

        History: Phase 4 (cutover, 2026-05-07) added a flag-true
        branch that ignored ``user_roles`` and read
        ``de_authz_user_org_role`` directly. (2026-05-28)
        retired that branch — the resolver
        (``_resolve_user_scope_uncached``) already reads new-authz
        memberships AND unions the legacy regex; making this helper
        also do ES round-trips was duplicate work that broke ~25 call
        sites expecting pure-regex semantics over ``session["roles"]``.

        Returns:
            list: List of organization titles (the part after ``org_`` prefix).
        """
        org_ids = []
        for role in user_roles or []:
            match = _LEGACY_ORG_ROLE_RE.match(role)
            if match:
                org_ids.append(match.group(1))
        return org_ids

    @classmethod
    def _org_titles_from_memberships(cls):
        """Phase 4: resolve the session user's org membership
        in the new authz model to organization_title strings.

        Reads `de_authz_user_org_role` for the session username,
        collects org_esid values, then resolves each to
        organization_title via `de_asset_organization`.

        Returns the same shape `extract_org_ids_from_roles` produced
        on the legacy path: a list of organization titles. Memoized
        per-request on Flask `g`.
        """
        username = session.get("username", "")
        if not username:
            return []

        return _memoize_on_g(
            ("org_titles", username),
            lambda: cls._resolve_titles_from_user_role_index(
                index="de_authz_user_org_role",
                esid_field="org_esid",
                title_index="de_asset_organization",
                title_field="organization_title",
                username=username,
            ),
        )

    @classmethod
    def _team_titles_from_memberships(cls):
        """Phase 4: resolve the session user's team membership
        in the new authz model to team_title strings.

        Reads `de_authz_user_team_role` for the session username,
        collects team_esid values, then resolves each to team_title
        via `de_asset_teams`. Memoized per-request on Flask `g`.
        """
        username = session.get("username", "")
        if not username:
            return []

        return _memoize_on_g(
            ("team_titles", username),
            lambda: cls._resolve_titles_from_user_role_index(
                index="de_authz_user_team_role",
                esid_field="team_esid",
                title_index="de_asset_teams",
                title_field="team_title",
                username=username,
            ),
        )

    @staticmethod
    def _resolve_esids_and_titles_from_user_role_index(*, index, esid_field, title_index, title_field, username):
        """(2026-05-26): same shape as
        ``_resolve_titles_from_user_role_index`` but returns ``(esids,
        titles)`` so the resolver can stash ESIDs on ``UserScope``
        alongside titles. The two halves come from the same two ES
        round trips — separating them was waste; this helper just
        keeps both.

        Returns ``([], [])`` on any ES failure — deny-by-default,
        consistent with the title-only helper. ESIDs are the ES
        ``_id`` values of the asset docs (globally unique). Titles
        are the human-readable strings (NOT unique across workspaces;
        callers walking authz scope → workspace MUST use the ESID
        half).
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("RoleFilterService: ES unavailable; returning empty %s esids/titles", title_field)
            return [], []

        try:
            result = es.search(
                index=index,
                body={
                    "query": {"term": {"username": username}},
                    "_source": [esid_field],
                    "size": 1000,
                },
            )
        except Exception as e:
            logging.error("RoleFilterService: error querying %s for %s: %s", index, username, e)
            return [], []

        esids = sorted(
            {
                hit["_source"][esid_field]
                for hit in result.get("hits", {}).get("hits", [])
                if hit.get("_source", {}).get(esid_field)
            }
        )
        if not esids:
            return [], []

        try:
            title_result = es.search(
                index=title_index,
                body={
                    "query": {"ids": {"values": esids}},
                    "_source": [title_field],
                    "size": len(esids),
                },
            )
        except Exception as e:
            logging.error("RoleFilterService: error resolving %s titles: %s", title_field, e)
            return [], []

        titles = []
        found_esids = []
        for hit in title_result.get("hits", {}).get("hits", []):
            title = hit.get("_source", {}).get(title_field)
            if title:
                titles.append(title)
                found_esids.append(hit["_id"])
        return found_esids, titles

    @staticmethod
    def _resolve_titles_from_user_role_index(*, index, esid_field, title_index, title_field, username):
        """Shared body for `_org_titles_from_memberships` and
        `_team_titles_from_memberships`. Two ES queries: one to fetch
        scope ESIDs for the user from a role-assignment index, one to
        resolve those ESIDs to titles. Returns [] on any ES failure
        (deny-by-default — the caller surfaces "no scope" as "no
        access").
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("RoleFilterService: ES unavailable; returning empty %s titles", title_field)
            return []

        try:
            result = es.search(
                index=index,
                body={
                    "query": {"term": {"username": username}},
                    "_source": [esid_field],
                    "size": 1000,
                },
            )
        except Exception as e:
            logging.error("RoleFilterService: error querying %s for %s: %s", index, username, e)
            return []

        esids = sorted(
            {
                hit["_source"][esid_field]
                for hit in result.get("hits", {}).get("hits", [])
                if hit.get("_source", {}).get(esid_field)
            }
        )
        if not esids:
            return []

        try:
            title_result = es.search(
                index=title_index,
                body={
                    "query": {"ids": {"values": esids}},
                    "_source": [title_field],
                    "size": len(esids),
                },
            )
        except Exception as e:
            logging.error("RoleFilterService: error resolving %s titles: %s", title_field, e)
            return []

        titles = []
        for hit in title_result.get("hits", {}).get("hits", []):
            title = hit.get("_source", {}).get(title_field)
            if title:
                titles.append(title)
        return titles

    @classmethod
    def get_org_titles_by_ids(cls, org_ids):
        """
        Fetch organization titles from Elasticsearch by searching the organization_title field.

        Args:
            org_ids: List of organization titles extracted from role names (e.g., ["ACME", "Kavanah"])

        Returns:
            list: List of organization titles (strings) found in Elasticsearch
        """
        org_titles = []
        es = get_elasticsearch_client()

        if not es:
            logging.error("Failed to get Elasticsearch client for organization lookup")
            return org_titles

        for org_title in org_ids:
            try:
                # Search for organizations with matching organization_title field
                # Use 'match' query for text fields (works with comma-separated lists)
                result = es.search(
                    index="de_asset_organization",
                    body={
                        "query": {"match": {"organization_title": org_title}},
                        "size": 10,  # Should only be one, but allow for potential duplicates
                    },
                )

                hits = result.get("hits", {}).get("hits", [])
                if hits:
                    # Use the organization_title from the first matching document
                    found_title = hits[0]["_source"].get("organization_title")
                    if found_title:
                        org_titles.append(found_title)
                        logging.info(f"Found organization '{found_title}' for role org_{org_title}")
                    else:
                        logging.warning(f"Organization document found but missing organization_title field for: {org_title}")
                else:
                    logging.warning(f"No organization found with organization_title: {org_title}")
            except Exception as e:
                logging.error(f"Error searching for organization with title '{org_title}': {e}")

        return org_titles

    @classmethod
    def get_org_descriptions_by_ids(cls, org_ids):
        """
        Fetch organization descriptions from Elasticsearch for header display.
        Searches by organization_title, returns organization_description.
        Falls back to organization_title if organization_description is not set.

        Args:
            org_ids: List of organization titles extracted from role names (e.g., ["ACME"])

        Returns:
            list: List of organization descriptions (strings) found in Elasticsearch
        """
        org_descriptions = []
        es = get_elasticsearch_client()

        if not es:
            logging.error("Failed to get Elasticsearch client for organization lookup")
            return org_descriptions

        for org_title in org_ids:
            try:
                result = es.search(
                    index="de_asset_organization",
                    body={
                        "query": {"match": {"organization_title": org_title}},
                        "size": 1,
                    },
                )

                hits = result.get("hits", {}).get("hits", [])
                if hits:
                    source = hits[0]["_source"]
                    description = source.get("organization_description")
                    if description:
                        org_descriptions.append(description)
                        logging.info(f"Found org description '{description}' for org_{org_title}")
                    else:
                        # Fall back to organization_title if description not populated
                        fallback = source.get("organization_title", org_title)
                        org_descriptions.append(fallback)
                        logging.warning(f"No organization_description for '{org_title}', using title fallback")
                else:
                    logging.warning(f"No organization found with organization_title: {org_title}")
            except Exception as e:
                logging.error(f"Error fetching org description for '{org_title}': {e}")

        return org_descriptions

    @classmethod
    def extract_team_ids_from_roles(cls, user_roles):
        """Pure-regex parse of legacy ``team_<title>`` strings from
        ``user_roles``.

        Roles follow the convention: ``team_<team_title>`` (e.g.
        ``team_NORTH`` -> returns ``"NORTH"``).

        See ``extract_org_ids_from_roles`` for the rationale
        (same shape).

        Returns:
            list: List of team titles (the part after ``team_`` prefix).
        """
        team_ids = []
        for role in user_roles or []:
            match = _LEGACY_TEAM_ROLE_RE.match(role)
            if match:
                team_ids.append(match.group(1))
        return team_ids

    @classmethod
    def get_team_titles_by_ids(cls, team_ids):
        """
        Fetch team titles from Elasticsearch by searching the team_title field.

        Args:
            team_ids: List of team titles extracted from role names (e.g., ["NORTH", "ACME"])

        Returns:
            list: List of team titles (strings) found in Elasticsearch
        """
        team_titles = []
        es = get_elasticsearch_client()

        if not es:
            logging.error("Failed to get Elasticsearch client for team lookup")
            return team_titles

        for team_title in team_ids:
            try:
                # Search for teams with matching team_title field
                # Use 'match' query for text fields
                result = es.search(
                    index="de_asset_teams",
                    body={"query": {"match": {"team_title": team_title}}, "size": 10},
                )

                hits = result.get("hits", {}).get("hits", [])
                if hits:
                    # Use the team_title from the first matching document
                    found_title = hits[0]["_source"].get("team_title")
                    if found_title:
                        team_titles.append(found_title)
                        logging.info(f"Found team '{found_title}' for role team_{team_title}")
                    else:
                        logging.warning(f"Team document found but missing team_title field for: {team_title}")
                else:
                    logging.warning(f"No team found with team_title: {team_title}")
            except Exception as e:
                logging.error(f"Error searching for team with title '{team_title}': {e}")

        return team_titles

    @classmethod
    def get_team_titles_by_organization(cls, org_titles):
        """
        Fetch all team titles belonging to the given organization(s).

        Args:
            org_titles: List of organization titles (e.g., ["ACME", "Kavanah"])

        Returns:
            list: List of team title strings found in Elasticsearch, or empty list if none found
        """
        team_titles = []
        es = get_elasticsearch_client()

        if not es:
            logging.error("Failed to get Elasticsearch client for team lookup by organization")
            return team_titles

        try:
            # Build query to find teams matching any of the organization titles
            # Note: organization_title can be a string OR an array (for multi-org teams)
            query = {"bool": {"should": []}}

            for org_title in org_titles:
                # Use match query which handles both comma-separated strings
                # (e.g. "ACME, BETA, GAMMA") and array fields, unlike terms which
                # requires exact token matches and fails for multi-org teams
                query["bool"]["should"].append({"match": {"organization_title": org_title}})

            result = es.search(
                index="de_asset_teams",
                body={
                    "query": query,
                    "size": 1000,  # Support orgs with many teams
                    "_source": ["team_title", "organization_title"],
                },
            )

            hits = result.get("hits", {}).get("hits", [])
            for hit in hits:
                team_title = hit["_source"].get("team_title")
                if team_title:
                    team_titles.append(team_title)
                    logging.debug(f"Found team '{team_title}' for organization(s) {org_titles}")

            if team_titles:
                logging.info(f"Found {len(team_titles)} teams for organizations {org_titles}")
            else:
                logging.warning(f"No teams found for organizations: {org_titles}")

        except Exception as e:
            logging.error(f"Error searching for teams by organization '{org_titles}': {e}")

        return team_titles

    @classmethod
    def get_team_titles_by_organization_esids(cls, org_esids):
        """ESID-keyed sibling of ``get_team_titles_by_organization``.

        Expand "user has org-tier scope on these orgs" → "team titles under
        those orgs" by joining on the unique ``organization_esid`` (the org
        doc's ES ``_id``) instead of the non-unique ``organization_title``
        string. The title join (sibling above) matches any team whose
        ``organization_title`` array merely *contains* the requested title —
        so when two workspaces own a same-titled org (e.g. both pick
        "Kavanah" / "Acme"), an org-scoped user on workspace A's org pulls
        workspace B's same-titled teams into their ``team_titles``. The
        ESID join can't collide: ``organization_esid`` is the org's ``_id``,
        unique by construction.

        ``terms organization_esid`` is exact-token and handles the
        multi-org-per-team shape (``organization_esid`` may be a list) the
        same way the title ``match`` did — a team in-scope if ANY of its
        parent org ESIDs is requested.

        Args:
            org_esids: list of organization ESIDs (de_asset_organization _id).

        Returns:
            list: team_title strings (possibly empty). Fail-soft: ES down or
                  error → empty list, mirroring the title helper.
        """
        team_titles: list[str] = []
        if not org_esids:
            return team_titles

        es = get_elasticsearch_client()
        if not es:
            logging.error("Failed to get Elasticsearch client for team lookup by org ESIDs")
            return team_titles

        # ``de_asset_teams.organization_esid`` is mapped
        # ``text`` + ``.keyword`` subfield on staging/prod (and plain
        # ``keyword`` on older cohorts). A ``terms`` query on the bare
        # ``text`` field analyzes the esid and matches NOTHING (verified:
        # 0 hits), so the first cut of this helper silently returned no
        # teams for every grant/workspace/membership user — orgs survived
        # (different field) but teams vanished everywhere. OR both shapes,
        # the same forward-compatible pattern uses for
        # ``team_title`` (line 1217): ``.keyword`` matches the text-cohort,
        # the bare field matches the plain-keyword-cohort.
        esids_list = list(org_esids)
        try:
            result = es.search(
                index="de_asset_teams",
                body={
                    "query": {
                        "bool": {
                            "should": [
                                {"terms": {"organization_esid.keyword": esids_list}},
                                {"terms": {"organization_esid": esids_list}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                    "size": 1000,  # Support orgs with many teams
                    "_source": ["team_title", "organization_esid"],
                },
            )

            hits = result.get("hits", {}).get("hits", [])
            for hit in hits:
                team_title = hit["_source"].get("team_title")
                if team_title:
                    team_titles.append(team_title)

            if team_titles:
                logging.info(f"Found {len(team_titles)} teams for org ESIDs {org_esids}")
            else:
                logging.warning(f"No teams found for org ESIDs: {org_esids}")

        except Exception as e:
            logging.error(f"Error searching for teams by org ESIDs '{org_esids}': {e}")

        return team_titles

    @classmethod
    def get_allowed_teams(cls):
        """
        Get list of team titles the current user can access.

        privileged users (system-admin / unrestricted-role)
        return their grant-scoped team titles. A bare superuser with
        zero grants returns ``[]`` — an empty list, NOT None — so
        downstream callers like the asset tree (app.py:1501) know to
        render no teams instead of falling back to unrestricted.

         Phase A (2026-05-15): the privileged branch now reads
        the unified ``resolve_user_scope`` resolver, which unions
        memberships + grants + workspace memberships.
        Non-privileged behavior is unchanged — the legacy regex over
        ``session["roles"]`` is the source for users still on
        ``team_<title>`` strings.

        Returns:
            list: List of team title strings (possibly empty), or None
                  for non-privileged users who have no team_* roles
                  (org-level users go through ``get_allowed_teams_for_org_users``).
        """
        if not cls.should_apply_filter():
            return None

        config = cls.load_config()
        user_roles = session.get("roles", [])
        username = session.get("username", "")

        # privileged users derive their allowed teams from the
        # unified resolver. Same UNION semantics as the earlier
        # ``_privileged_user_scope`` (memberships ∪ grants) but extended
        # with the workspace tier.
        if _user_is_privileged(user_roles, username, config):
            scope = resolve_user_scope(username, user_roles, config)
            return list(scope.team_titles)

        team_ids = cls.extract_team_ids_from_roles(user_roles)
        if team_ids:
            team_titles = cls.get_team_titles_by_ids(team_ids)
            if team_titles:
                return team_titles

        # round 2 (2026-05-26): workspace-tier fallback. Same
        # pattern as get_allowed_organizations above — non-privileged
        # workspace-tier-only users have no team_* role docs but DO
        # have resolvable scope.team_titles from the workspace tier
        # (via _workspace_scoped_titles' org→teams expansion).
        # Without this fallback, the dashboard team-filter path returns
        # None → no filter → cluster-wide leak.
        scope = resolve_user_scope(username, user_roles, config)
        if not scope.is_empty:
            return list(scope.team_titles)
        return None

    @classmethod
    def get_allowed_teams_for_org_users(cls):
        """
        Get list of team titles accessible to organization-level users.

        For org users, this returns teams belonging to their organization(s).
        For team users, this returns their directly assigned teams.

        privileged users return their grant-scoped team titles
        here too — the dashboard-builder path (lines 3261-3263) uses
        this method as its primary scope source, so bare superusers
        with no grants must surface ``[]`` rather than None.

        Returns:
            list: List of team title strings (possibly empty for
                  privileged users without grants), or None for
                  non-privileged users with no resolvable scope.
        """
        if not cls.should_apply_filter():
            return None

        config = cls.load_config()
        user_roles = session.get("roles", [])
        username = session.get("username", "")

        # Phase A: privileged users derive their allowed teams
        # from the unified resolver (memberships ∪ grants ∪ workspace).
        if _user_is_privileged(user_roles, username, config):
            scope = resolve_user_scope(username, user_roles, config)
            return list(scope.team_titles)

        # Check for direct team roles first (most restrictive)
        team_ids = cls.extract_team_ids_from_roles(user_roles)
        if team_ids:
            # User has team_* roles - return those teams directly
            return cls.get_team_titles_by_ids(team_ids)

        # Check for organization roles - convert to team access
        org_ids = cls.extract_org_ids_from_roles(user_roles)
        if org_ids:
            org_titles = cls.get_org_titles_by_ids(org_ids)
            if org_titles:
                # Return all teams belonging to these organizations
                return cls.get_team_titles_by_organization(org_titles)

        # round 2 (2026-05-26): workspace-tier fallback. Mirrors
        # get_allowed_teams above so the dashboard-builder path (lines
        # 3261-3263) sees non-empty teams for the workspace-tier-only
        # cohort. Without this, dashboards rendered as workspace_member
        # show zero teams in the dashboard-builder's team picker.
        scope = resolve_user_scope(username, user_roles, config)
        if not scope.is_empty:
            return list(scope.team_titles)
        return None

    @classmethod
    def get_filter_for_current_user(cls):
        """
        Get Elasticsearch filter clause for current user based on their roles.

        Reads user roles from Flask session and resolves to org/team filters.

        Role Conventions:
            - org_<organization_title> maps to de_asset_organization
            - team_<team_title> maps to de_asset_teams

        Returns:
            dict: Elasticsearch filter clause (term or terms query), or None if
                  user has unrestricted access

        Examples:
            # User with single team role
            {"term": {"team_title": "NORTH"}}

            # User with single organization role
            {"term": {"organization_title": "ACME"}}

            # User with multiple organization roles
            {"terms": {"organization_title": ["Org1", "Org2"]}}
        """
        config = cls.load_config()

        # Get user roles from session
        user_roles = session.get("roles", [])

        if not user_roles:
            logging.warning("No roles found in session for filter resolution")
            return None

        # follow-up: system-admin in the new authz model is
        # checked first; legacy unrestricted_roles bypass remains as
        # the fallback for superuser.
        #
        # / F1: privileged users (system-admin OR
        # unrestricted-role) no longer get a blanket no-filter pass.
        # Instead, their query scope is derived from active
        # ``de_authz_data_grant`` records.
        #
        # (2026-05-15): composition extended — scope is the
        # UNION of standing memberships
        # (``de_authz_user_team_role`` + ``de_authz_user_org_role``)
        # and active grants. Zero of either → the
        # ``__NO_TEAMS_FOR_ORG__`` deny-all sentinel; otherwise a
        # team_title filter scoped to the union. This is what makes
        # an asset_admin with a single team membership see that team's
        # data without needing a grant, while a bare asset_admin /
        # superuser still sees nothing.
        username = session.get("username", "")
        # Phase A: collapsed the duplicated system-admin /
        # unrestricted-roles / asset_admin branches into one call.
        # ``_user_is_privileged`` covers all three (system-admin via the
        # ``de_authz_user_team_role:__all__:system_admin`` sentinel,
        # unrestricted_roles from YAML config, and the
        # ``_DATA_PRIVILEGED_ROLES`` constant for asset_admin). The
        # resolver returns a UserScope whose team_titles is the
        # memberships ∪ grants ∪ workspace UNION — empty means deny-all.
        if _user_is_privileged(user_roles, username, config):
            scope = resolve_user_scope(username, user_roles, config)
            if not scope.team_titles:
                logging.info(
                    "User %s is privileged with empty resolved scope; emitting deny-all sentinel",
                    username,
                )
                return _DENY_ALL_FILTER
            if len(scope.team_titles) == 1:
                scoped_filter = {"term": {"team_title": scope.team_titles[0]}}
            else:
                scoped_filter = {"terms": {"team_title": list(scope.team_titles)}}
            logging.info(
                "User %s is privileged; resolved scope filter: %s (source=%s)",
                username,
                scoped_filter,
                scope.scope_source,
            )
            return scoped_filter

        # Check for team roles first (most restrictive)
        team_ids = cls.extract_team_ids_from_roles(user_roles)
        if team_ids:
            team_titles = cls.get_team_titles_by_ids(team_ids)
            if team_titles:
                if len(team_titles) == 1:
                    filter_clause = {"term": {"team_title": team_titles[0]}}
                    logging.info(f"Created single team filter: {filter_clause}")
                    return filter_clause
                else:
                    filter_clause = {"terms": {"team_title": team_titles}}
                    logging.info(f"Created multi-team filter: {filter_clause}")
                    return filter_clause

        # Check for organization roles - NOW CONVERTS TO TEAM FILTER
        org_ids = cls.extract_org_ids_from_roles(user_roles)
        if org_ids:
            org_titles = cls.get_org_titles_by_ids(org_ids)
            if org_titles:
                # NEW: Look up teams belonging to these organizations
                team_titles = cls.get_team_titles_by_organization(org_titles)

                if team_titles:
                    # Create team-based filter instead of org-based filter
                    if len(team_titles) == 1:
                        filter_clause = {"term": {"team_title": team_titles[0]}}
                        logging.info(f"Created single team filter for org(s) {org_titles}: {filter_clause}")
                        return filter_clause
                    else:
                        filter_clause = {"terms": {"team_title": team_titles}}
                        logging.info(f"Created multi-team filter for org(s) {org_titles}: {filter_clause}")
                        return filter_clause
                else:
                    # No teams found for organization - show no data
                    logging.warning(f"No teams found for organizations {org_titles}, applying empty filter")
                    return {"term": {"team_title": "__NO_TEAMS_FOR_ORG__"}}

        # (2026-05-28): the extract_*_from_roles helpers no
        # longer parse legacy ``team_<title>`` / ``org_<title>`` strings
        # (the regex branch was retired with the USE_NEW_PERMISSIONS
        # flag). Fall through to the unified resolver, which still
        # consults the legacy regex via ``_legacy_es_security_titles``
        # at the resolver's union — same hybrid contract the
        # other ``get_allowed_*`` helpers use.
        scope = resolve_user_scope(username, user_roles, config)
        if scope.team_titles:
            titles = list(scope.team_titles)
            if len(titles) == 1:
                filter_clause = {"term": {"team_title": titles[0]}}
            else:
                filter_clause = {"terms": {"team_title": titles}}
            logging.info(f"Resolver-derived team filter (source={scope.scope_source}): {filter_clause}")
            return filter_clause

        logging.warning(f"No valid org or team roles found in: {user_roles}")
        return None

    # Fields that scope a query to a particular team/org. When a
    # scoped user passes a filter on one of these fields with a value
    # outside their allowed set, the value is dropped (or the whole filter
    # if no values remain).  Includes the bare and ``.keyword`` variants
    # because chart configs sometimes use one and sometimes the other.
    _SCOPE_DEFINING_TEAM_FIELDS = frozenset(
        {
            "team_title",
            "team_title.keyword",
        }
    )
    _SCOPE_DEFINING_ORG_FIELDS = frozenset(
        {
            "organization_title",
            "organization_title.keyword",
        }
    )

    @classmethod
    def scoped_index_pattern(cls, generic_pattern):
        """Phase B Layer-1 entry point. Rewrite a wildcard / comma-list
        ES index pattern down to the explicit set of indices the current user
        is allowed to read.

        Thin Flask-session wrapper over ``lib.es_helpers.scoped_index_pattern``:
        resolves the current user's ``UserScope.allowed_indices`` via the
        unified resolver, then delegates the intersection. Wrapping is the
        whole point — call sites pass their existing wildcard
        (``"*google_ads"``, ``"*echo_conversations,-unknown_echo_conversations"``)
        and the helper returns the same shape, scoped.

        Empty allowlist (bare privileged user, ES outage, or
        ``scope_source='none'``) returns the no-match wildcard sentinel —
        ES returns zero hits silently without ``ignore_unavailable=True``.
        That's the structural privacy guarantee: even if a future Layer-2
        regression lets a team_title filter slip, the query physically
        cannot reach another tenant's index.

        Returns the input unchanged when there is no current user (e.g.
        called from a CLI/import context with no Flask session). The
        Layer-1 guarantee is for authenticated request paths; CLI tooling
        runs as the import service account with full data access.
        """
        try:
            user_roles = session.get("roles", []) or []
            username = session.get("username", "")
        except RuntimeError:
            # Outside Flask request context — pass through. The Layer-1
            # gate is request-scoped; CLI / background-job callers do not
            # cross a tenant boundary.
            return generic_pattern

        if not username:
            # No authenticated user. Pass through rather than denying —
            # this branch covers anonymous error-page rendering and a
            # handful of healthcheck paths that hit the same routes.
            # Callers that need a hard deny should check
            # session["username"] before calling.
            return generic_pattern

        config = cls.load_config()
        scope = resolve_user_scope(username, user_roles, config)
        return _scoped_index_pattern_impl(generic_pattern, scope.allowed_indices)

    @classmethod
    def workspace_scoped_index_pattern(cls, generic_pattern, workspace_esid):
        """rewrite ``generic_pattern`` down to the indices of ONE
        chosen workspace — for the Data Inventory page's workspace switcher.

        ``scoped_index_pattern`` narrows to the user's WHOLE allowed set
        (the union across every workspace a superuser/owner can reach).
        Data Inventory lets the user pick a single workspace to inspect;
        this method narrows further to just that workspace's team indices.

        Authorization is by construction, two layers deep:

        1. The chosen ``workspace_esid`` MUST be in
           ``get_allowed_workspaces()`` — a forged / out-of-scope value
           returns the no-match sentinel (zero hits), never another
           tenant's data.
        2. Even for an in-scope workspace, the result is intersected with
           the user's own ``scoped_index_pattern`` output, so the chosen
           workspace can never widen scope beyond what the user already
           holds.

        Returns the no-match sentinel (``_EMPTY_SCOPE_SENTINEL`` shape via
        the impl) when the workspace is out of scope, unknown, or empty.
        """
        if not workspace_esid:
            return cls.scoped_index_pattern(generic_pattern)

        # (item 1): gate on the SAME narrowed set the switcher lists —
        # data-grant + primary workspace only, not the broad union from
        # get_allowed_workspaces(). This always returns a concrete list, so a
        # superuser can only inspect a workspace they actually hold a grant on
        # (or their primary), never an arbitrary one passed in the param.
        try:
            from web.services.workspace_membership_service import WorkspaceMembershipService
        except ImportError:
            from asset_management.web.services.workspace_membership_service import (
                WorkspaceMembershipService,
            )
        username = session.get("username", "")
        allowed_workspaces = WorkspaceMembershipService.data_grant_and_primary_workspaces(username)
        if workspace_esid not in set(allowed_workspaces):
            # Out-of-scope / forged workspace → deny via the empty-scope
            # sentinel (no foreign tenant's indices, ever).
            return _scoped_index_pattern_impl(generic_pattern, frozenset())

        # Resolve the chosen workspace's team titles, build their index
        # names with the same dual-scheme helper the resolver uses, and
        # intersect with the data-view wildcard.
        try:
            from web.services.invite_service import InviteService
        except ImportError:
            from asset_management.web.services.invite_service import InviteService

        teams = InviteService.list_workspace_teams(workspace_esid)
        team_titles = [t["team_title"] for t in teams if t.get("team_title")]
        ws_indices = _build_allowed_indices(team_titles)

        # Layer 2: never exceed the user's own scope. Intersect the
        # workspace's indices with the user's full allowed set.
        user_roles = session.get("roles", []) or []
        config = cls.load_config()
        user_scope = resolve_user_scope(username, user_roles, config)
        effective = frozenset(ws_indices) & frozenset(user_scope.allowed_indices)
        return _scoped_index_pattern_impl(generic_pattern, effective)

    @classmethod
    def intersect_body_filters(cls, body, allowed_teams):
        """Mutate ``body['filters']`` so any scope-defining filter values
        outside ``allowed_teams`` are dropped.

        For each filter in ``body['filters']`` whose ``field`` is a
        scope-defining team field, the ``value`` is intersected with
        ``allowed_teams`` (handling both single-value ``op: "eq"`` /
        ``"match_phrase"`` shapes and list-value ``op: "in"`` /
        ``"match_phrase_any"`` shapes).  Filters whose intersection is empty
        are removed entirely.  Filters on ``organization_title`` are
        dropped outright for scoped users — the allow-only filter appended
        by ``append_scope_filter`` already pins scope, and an org filter
        could only re-expand scope via partner-org lookup.

        Filters on non-scope-defining fields (date, status, content fields)
        pass through unchanged.

        Idempotent. Safe to call when ``allowed_teams`` is ``None`` (admin
        path) — returns without modification.

        Args:
            body: Dict with optional ``filters`` list of
                  ``{field, op, value}`` clauses.  Mutated in place.
            allowed_teams: List of team_title strings the caller may
                           legitimately query, or ``None`` for admins.
        """
        if allowed_teams is None:
            return
        if not isinstance(body, dict):
            return
        filters = body.get("filters")
        if not isinstance(filters, list):
            return

        allowed_set = set(allowed_teams)
        kept = []

        for f in filters:
            if not isinstance(f, dict):
                continue
            field = f.get("field", "")

            # Org filters are dropped outright for scoped users. See
            # rationale: ``organization_title`` could otherwise re-expand scope
            # via partner-org lookup.
            if field in cls._SCOPE_DEFINING_ORG_FIELDS:
                continue

            if field not in cls._SCOPE_DEFINING_TEAM_FIELDS:
                kept.append(f)
                continue

            # Scope-defining team filter: intersect value(s) against allowed.
            value = f.get("value")
            if isinstance(value, list):
                intersected = [v for v in value if v in allowed_set]
                if not intersected:
                    continue  # whole filter dropped
                f = dict(f)
                f["value"] = intersected
                kept.append(f)
            else:
                if value in allowed_set:
                    kept.append(f)
                # else: drop the whole filter

        body["filters"] = kept

    @classmethod
    def append_scope_filter(cls, body, allowed_teams):
        """Append an allow-only ``team_title`` filter to ``body['filters']``.

        Belt-and-suspenders to ``intersect_body_filters``: even a request
        with no scope-defining filter at all gets pinned to the user's
        allowed team set, so ES can never return a row outside that set.

        No-op when ``allowed_teams`` is ``None`` (admin path).  Idempotent
        — calling twice produces the same result as once because the
        appended clause is structurally identical.  Empty ``allowed_teams``
        appends a filter that returns no hits (rather than silently
        skipping), so a scoped user with no resolvable teams sees nothing.

        Args:
            body: Dict with optional ``filters`` list.  Mutated in place.
            allowed_teams: List of team_title strings, or ``None``.
        """
        if allowed_teams is None:
            return
        if not isinstance(body, dict):
            return

        filters = body.get("filters")
        if not isinstance(filters, list):
            filters = []
            body["filters"] = filters

        # Mirror _org_team_filters' choice of ``team_title`` (text field) over
        # ``team_title.keyword`` — match_phrase on the text field is what every
        # other DT/team-scoped query uses, so the allow-only clause matches the
        # established index-mapping assumption.
        clause = {
            "field": "team_title",
            "op": "match_phrase_any",
            "value": list(allowed_teams),
        }
        # Idempotent: don't append if an identical clause is already present.
        for existing in filters:
            if (
                isinstance(existing, dict)
                and existing.get("field") == clause["field"]
                and existing.get("op") == clause["op"]
                and existing.get("value") == clause["value"]
            ):
                return

        filters.append(clause)

    @classmethod
    def should_apply_filter(cls):
        """
        Check if current user should have filters applied.

        Returns:
            bool: True if filter should be applied; False only for users
                  whose ES queries should run completely unscoped.

         (2026-05-14): Pre-F1, this returned False for any
        privileged user (system-admin OR legacy unrestricted_roles),
        meaning superusers got blanket no-filter access. F1 changes
        this: privileged users now route through grant-scoped filtering
        and always get filtered queries — either to their granted
        scopes or to the deny-all sentinel if they hold no grants.
        Returning True for those users is what tells callers like
        ``app.py:1501`` (asset tree) and ``get_allowed_*`` (line 616
        et al.) to *actually consult* the grant-scoped methods instead
        of falling through to no-filter behavior.

        The single remaining False path is the earlier catch-all:
        a user with no org/team roles at all who is also NOT privileged.
        That's a misconfigured account, not a privileged one; the
        legacy logic returned False here and we preserve that to avoid
        regressing on misconfiguration edge cases.
        """
        config = cls.load_config()
        user_roles = session.get("roles", [])
        username = session.get("username", "")

        if _user_is_privileged(user_roles, username, config):
            # Privileged users always have a filter applied: either
            # scoped to their grants, or deny-all. The grant lookup
            # itself happens in get_filter_for_current_user /
            # get_allowed_*.
            return True

        # Non-privileged: filter applies iff they have at least one
        # org/team role to resolve.
        org_ids = cls.extract_org_ids_from_roles(user_roles)
        team_ids = cls.extract_team_ids_from_roles(user_roles)
        if org_ids or team_ids:
            return True

        # (2026-05-26): workspace-tier-only cohort. The legacy
        # check above only consults ``de_authz_user_org_role`` and
        # ``de_authz_user_team_role``. A non-privileged user whose
        # only scope source is the workspace tier
        # (``de_authz_workspace_membership`` or
        # ``de_authz_user_workspace_role`` with role_esid=workspace_member)
        # has empty org/team role docs and falls through to ``False``,
        # which made every ``get_allowed_*`` helper short-circuit to
        # ``None`` and the filter dropdowns leaked the cluster-wide
        # list. Surfaced by the attack-surface harness 2026-05-26 as
        # ``test-grant`` (a workspace_member of workspace-b) seeing
        # ``ControlOrg`` (an org in control-workspace) in
        # ``/api/dashboards/filter-options``.
        #
        # The fix: consult the unified resolver. It already reads
        # workspace tier and is memoized on Flask ``g`` so
        # the extra call is shared with the get_allowed_* helpers'
        # subsequent resolver reads. The legacy False path
        # (LEGACY-REGRESSION-001 kibana_admin / no-roles user) is
        # preserved because their UserScope still resolves to
        # ``is_empty=True`` — they have nothing in the new-authz
        # indexes either.
        scope = resolve_user_scope(username, user_roles, config)
        return not scope.is_empty

    @classmethod
    def current_user_has_any_scope(cls):
        """Phase B.4: True iff the current Flask-session user has
        resolvable scope from ANY source (memberships, grants, workspace
        membership, or legacy team_*/org_* roles).

        Endpoints that enumerate tenant-scoped data (Asset Explorer's
        list views, the tree structure, the filter dropdowns) call this
        to gate a deny-all return path. Returning ``False`` means "the
        user resolves to UserScope.is_empty" — they should see zero
        orgs / zero teams, not the full unfiltered list.

        **Why this is a separate concern from** ``should_apply_filter``:
        ``should_apply_filter`` carries a legacy semantic where ``False``
        means "no filter to apply, pass through" for genuinely-unscoped
        users like ``kibana_admin``. Other callers (the asset tree
        rendering, ``get_filter_for_current_user``'s fallback) depend
        on that semantic. Conflating the two would break the
        ``LEGACY-REGRESSION-001`` test contract — a ``kibana_admin``
        user MUST still return ``False`` from ``should_apply_filter``
        even though they correctly resolve to ``scope.is_empty``.

        The clean separation: ``should_apply_filter`` answers "do
        callers need to consult the allowed-list helpers?"
        ``current_user_has_any_scope`` answers "is there ANY scope at
        all, or do we deny everything?"

        Returns ``True`` outside a Flask request context — the gate is
        per-request; CLI / background-job callers don't cross a tenant
        boundary and should not be denied.
        """
        try:
            user_roles = session.get("roles", []) or []
            username = session.get("username", "")
        except RuntimeError:
            return True
        if not username:
            return True
        config = cls.load_config()
        scope = resolve_user_scope(username, user_roles, config)
        return not scope.is_empty

    @classmethod
    def current_user_is_privileged(cls):
        """True iff the current Flask-session user is on the
        privileged-resolver path — system_admin, an ``unrestricted_roles``
        member (``superuser``), a ``_DATA_PRIVILEGED_ROLES`` member
        (``asset_admin``), or a privileged workspace role
        (``workspace_admin`` / ``workspace_owner``).

        This is the "scope-restricted vs admin" discriminator the
        ``get_allowed_*`` *values* cannot supply: a bare privileged user
        with no resolvable grants returns ``[]`` from
        ``get_allowed_teams_for_org_users`` / ``get_allowed_workspaces``,
        IDENTICAL to a genuinely-empty scoped user. Callers that must
        treat "admin with empty grants" as see-all (rather than deny-all)
        — e.g. the Data Connectors team dropdown, which collapses to a
        "No teams found" empty state if its team list is narrowed to
        ``[]`` — gate on this predicate instead of on the empty value.

        Note the deliberate asymmetry with the per-tenant *data* filter:
        the asset-list / picker surfaces narrow a privileged-but-grantless
        user to ``[]``, because there an empty result is a
        correct deny-all. This predicate is for the inverse case — a
        page-render surface where ``[]`` would wrongly hide structure from
        an admin who simply has no grants resolved (incl. a transient
        ES-unavailable path). ``workspace_admin`` / ``workspace_owner``
        count as privileged here, which is safe for the Data Connectors
        caller: ``list_teams`` has already workspace-narrowed their list,
        so skipping the team-title narrowing leaves them with exactly
        their workspace's teams (AC: workspace owner sees all teams in
        their workspace).

        Returns ``True`` outside a Flask request context — mirrors
        ``current_user_has_any_scope``; CLI / background-job callers don't
        cross a tenant boundary.
        """
        try:
            user_roles = session.get("roles", []) or []
            username = session.get("username", "")
        except RuntimeError:
            return True
        config = cls.load_config()
        return _user_is_privileged(user_roles, username, config)

    @classmethod
    def get_allowed_organizations(cls):
        """
        Get list of organization titles the current user can access.

        privileged users (system-admin / unrestricted-role)
        return their grant-scoped org titles. A bare superuser with
        zero grants returns ``[]`` so the asset tree at app.py:1501
        renders no orgs instead of falling back to all-orgs visibility.

        Returns:
            list: List of organization title strings (possibly empty
                  for privileged users without grants), or None for
                  non-privileged users with no org roles.
        """
        if not cls.should_apply_filter():
            return None

        config = cls.load_config()
        user_roles = session.get("roles", [])
        username = session.get("username", "")

        # Phase A: privileged users derive allowed orgs from
        # the unified resolver (memberships ∪ grants ∪ workspace).
        if _user_is_privileged(user_roles, username, config):
            scope = resolve_user_scope(username, user_roles, config)
            return list(scope.org_titles)

        org_ids = cls.extract_org_ids_from_roles(user_roles)
        if org_ids:
            org_titles = cls.get_org_titles_by_ids(org_ids)
            if org_titles:
                return org_titles

        # round 2 (2026-05-26): workspace-tier fallback. The
        # legacy role-doc lookup above misses non-privileged users
        # whose only scope source is workspace tier (membership or
        # workspace_member role assignment). Pre-fix this branch
        # returned None → filter dropdowns leaked cluster-wide.
        # ``should_apply_filter`` already returned True (
        # round 1); now mirror the workspace-tier read here so the
        # downstream filter has something concrete to narrow with.
        scope = resolve_user_scope(username, user_roles, config)
        if not scope.is_empty:
            return list(scope.org_titles)
        return None

    @classmethod
    def get_allowed_workspaces(cls):
        """list of workspace_esid strings the current user can access.

        Asset Explorer's per-tenant isolation reads this for every
        data-source asset list view (Chatwoot, Echo, FB, GA, Google Ads,
        Instagram, YouTube) plus teams. Each asset doc carries a
        ``workspace_esid`` keyword; ``list_assets``
        filters ``hits`` to the intersection of this allow-list and the
        doc's field.

        The set is the UNION of two legs:

        1. ``UserScope.workspace_esids`` — populated by
           ``_workspace_scoped_titles`` from ``de_authz_workspace_membership``.
        2. **Inferred** workspaces — walked up from
           ``UserScope.org_titles`` and ``UserScope.team_titles`` via
           ``de_asset_organization.workspace_esid``
           and ``de_asset_teams.organization_esid → org.workspace_esid``.
           This covers users who have role assignments or data grants
           at the org/team tier without a corresponding workspace
           membership doc — without the inferred leg, a workspace_admin
           with no membership row or a superuser with a team-scoped
           grant would see zero assets (the smoke-test bug surfaced
           2026-05-18).

        Returns:
            list: List of workspace_esid strings (possibly empty for
                  privileged users with no workspace scope of any
                  kind), or ``None`` for non-privileged users with no
                  scope to resolve — same convention as
                  ``get_allowed_organizations``. ``None`` means "no
                  workspace filter to apply at this layer"; ``[]``
                  means "filter and admit nothing" (deny-all).
        """
        if not cls.should_apply_filter():
            return None

        config = cls.load_config()
        user_roles = session.get("roles", [])
        username = session.get("username", "")

        scope = resolve_user_scope(username, user_roles, config)
        direct = set(scope.workspace_esids)
        # (2026-05-26): walk authz scope → workspace using ESIDs
        # (globally unique) instead of titles (collide across
        # workspaces when two wizard signups both name an org / team
        # the same thing — e.g. both wizards create a "General" team).
        # The title-based helper is retained as a deprecated fallback
        # for any legacy caller that still passes titles only.
        inferred = _workspace_esids_from_scope_esids(scope.org_esids, scope.team_esids)
        # Legacy fallback: a user whose only scope came through the
        # pre-cutover ``team_<title>`` / ``org_<title>`` ES-Security
        # path has no ESIDs on UserScope. Fall through to the title
        # helper so they aren't denied. The legacy cohort's titles are
        # unique by historical data shape (kavanah-media's teams).
        if not scope.team_esids and not scope.org_esids and (scope.team_titles or scope.org_titles):
            inferred |= _workspace_esids_from_scope_titles(scope.org_titles, scope.team_titles)
        return list(direct | inferred)

    @classmethod
    def filter_hits_by_workspace(cls, hits, *, workspace_field="workspace_esid"):
        """narrow a list of ES hit envelopes to the current
        user's allowed workspaces, enforcing the tenant boundary by
        ``workspace_esid`` rather than by (collision-prone) title.

        Every team-list / asset-list surface that previously filtered
        ONLY by team/org *title* leaked across workspaces: titles are not
        unique (two wizard signups both create a "General" / "Kavanah
        Team"), so a user scoped to a title in workspace A matched the
        same-titled doc in workspace B. This helper is the second,
        authoritative layer — apply it AFTER the existing title filter so
        a same-titled foreign-workspace doc is dropped.

        Contract mirrors ``get_allowed_workspaces``:
          * ``None``  → no workspace filter (admin override / privileged
            union resolved to no narrowing) — return ``hits`` unchanged.
          * ``[]``    → deny-all — return ``[]``.
          * a list    → keep only hits whose ``workspace_field`` value is
            in the allowed set. **Fail-closed**: a hit with no
            ``workspace_esid`` value is dropped (a stale / un-backfilled
            row must not widen scope through a missing field — same
            posture as the picker + asset-list filter).

        ``hits`` are ES hit envelopes (``{"_id", "_source": {...}}``);
        the workspace value is read from ``_source``.
        """
        allowed_workspaces = cls.get_allowed_workspaces()
        if allowed_workspaces is None:
            return hits
        allowed_set = set(allowed_workspaces)
        if not allowed_set:
            return []
        return [
            h
            for h in hits
            if (h.get("_source", {}) or {}).get(workspace_field)
            and (h.get("_source", {}) or {}).get(workspace_field) in allowed_set
        ]

    @classmethod
    def has_multiple_organizations(cls):
        """
        Check if current user has access to multiple organizations.

        Returns:
            bool: True if user has 2+ org_* roles, False otherwise
        """
        user_roles = session.get("roles", [])
        org_ids = cls.extract_org_ids_from_roles(user_roles)
        return len(org_ids) >= 2

    @classmethod
    def get_partner_organizations(cls):
        """
        Get list of partner organizations for users with partnerships.

        For multi-role users: Returns all their org roles
        For single-role users: Queries de_asset_teams to find all orgs that appear
                              in teams with the primary org

        Returns:
            list: List of {'title': str, 'is_primary': bool} dicts
                  Returns None if no partnerships
        """
        # Check session cache first
        cached_partner_orgs = session.get("cached_partner_orgs")
        if cached_partner_orgs is not None:
            logging.debug(f"Using cached partner orgs: {cached_partner_orgs}")
            return cached_partner_orgs if cached_partner_orgs else None

        user_roles = session.get("roles", [])
        org_ids = cls.extract_org_ids_from_roles(user_roles)

        if len(org_ids) == 0:
            session["cached_partner_orgs"] = []
            return None

        # Get org titles
        org_titles = cls.get_org_titles_by_ids(org_ids)

        if len(org_ids) >= 2:
            # Multi-org user: return all orgs
            partner_orgs = []
            for idx, title in enumerate(org_titles):
                partner_orgs.append({"title": title, "is_primary": (idx == 0)})
            logging.debug(f"Partner orgs for multi-org user: {partner_orgs}")
            session["cached_partner_orgs"] = partner_orgs
            return partner_orgs

        elif len(org_ids) == 1:
            # Single-org user: query teams to find all partner orgs
            if org_titles and len(org_titles) > 0:
                primary_org = org_titles[0]
                all_orgs = cls.get_partner_orgs_from_teams(primary_org)

                # Always include at least the primary org
                partner_orgs = [{"title": primary_org, "is_primary": True}]

                # Add any partner orgs found (excluding primary since it's already added)
                if all_orgs:
                    for org_title in all_orgs:
                        if org_title != primary_org:
                            partner_orgs.append({"title": org_title, "is_primary": False})

                logging.info(
                    f"Partner orgs for single-org user: {partner_orgs} (from {len(all_orgs) if all_orgs else 0} total orgs found in teams)"
                )
                session["cached_partner_orgs"] = partner_orgs
                return partner_orgs

        session["cached_partner_orgs"] = []
        return None

    @classmethod
    def get_partner_orgs_from_teams(cls, primary_org):
        """
        Query de_asset_teams to find all organizations that appear in teams
        with the primary org.

        Args:
            primary_org: Primary organization name (e.g., "ACME")

        Returns:
            list: List of unique organization titles from all teams that include
                  the primary org (including the primary org itself)
        """
        es = get_elasticsearch_client()
        if not es:
            logging.error("Cannot connect to Elasticsearch for partner org lookup")
            return []

        try:
            # Query de_asset_teams for teams that include the primary org.
            # organization_title may be stored as a comma-separated string
            # (e.g. "ACME, BETA, GAMMA") so use match query on the text field.
            query = {"match": {"organization_title": primary_org}}

            result = es.search(
                index="de_asset_teams",
                body={
                    "query": query,
                    "size": 1000,
                    "_source": ["organization_title", "team_title"],
                },
                ignore_unavailable=True,
            )

            # Extract all unique org titles from the teams.
            # Handle both array and comma-separated string formats.
            all_orgs = set()
            teams_found = 0
            for hit in result.get("hits", {}).get("hits", []):
                teams_found += 1
                org_title = hit.get("_source", {}).get("organization_title")
                team_title = hit.get("_source", {}).get("team_title", "unknown")

                if isinstance(org_title, list):
                    for org in org_title:
                        all_orgs.add(org.strip())
                elif isinstance(org_title, str) and "," in org_title:
                    # Comma-separated multi-org string
                    for org in org_title.split(","):
                        all_orgs.add(org.strip())
                elif isinstance(org_title, str):
                    all_orgs.add(org_title.strip())

            org_list = sorted(list(all_orgs))
            logging.info(
                f"Found {teams_found} teams for org '{primary_org}', extracted {len(org_list)} unique orgs: {org_list}"
            )
            return org_list

        except Exception as e:
            logging.error(f"Error querying teams for partner orgs: {e}")
            return []

    @classmethod
    def has_partnership_documents(cls, org_name):
        """
        Check if partnership documents exist in Elasticsearch for the given organization.
        Partnership documents are those where organization_title is an array with multiple values.

        Args:
            org_name: Organization name to check (e.g., "ACME")

        Returns:
            bool: True if partnership documents exist, False otherwise
        """
        import time

        global _partnership_cache

        # Check cache first
        now = time.time()
        with _partnership_cache_lock:
            cached = _partnership_cache.get(org_name)
        if cached is not None:
            has_partnerships, cached_time = cached
            if (now - cached_time) < PARTNERSHIP_CACHE_TTL:
                logging.debug(f"Partnership check cache hit for {org_name}: {has_partnerships}")
                return has_partnerships

        # Query Elasticsearch
        es = get_elasticsearch_client()
        if not es:
            logging.error("Cannot connect to Elasticsearch for partnership check")
            return False

        # List of indices to check
        index_patterns = [
            "*fb_insights",  # Facebook Ads
            "*google_ads",  # Google Ads
            "*echo_conversations",  # Echo
            "*ga_metrics",  # Google Analytics
            "*youtube_analytics",  # YouTube
        ]

        has_partnerships = False

        try:
            for index_pattern in index_patterns:
                # Query: Get documents where organization_title contains the org
                # Use match query to handle comma-separated strings and array fields
                query = {"bool": {"must": [{"match": {"organization_title": org_name}}]}}

                result = es.search(
                    index=index_pattern,
                    body={
                        "query": query,
                        "size": 100,  # Check first 100 docs per index
                        "_source": ["organization_title"],  # Only fetch this field
                    },
                    ignore_unavailable=True,  # Skip if index doesn't exist
                )

                # Check if any results have organization_title as an array with length > 1
                for hit in result.get("hits", {}).get("hits", []):
                    org_title = hit.get("_source", {}).get("organization_title")
                    if isinstance(org_title, list) and len(org_title) > 1 and org_name in org_title:
                        has_partnerships = True
                        logging.info(f"Partnership documents found for {org_name} in {index_pattern}")
                        break

                if has_partnerships:
                    break  # No need to check other indices

        except Exception as e:
            logging.error(f"Error checking for partnership documents for {org_name}: {e}")
            # On error, assume no partnerships (fail gracefully)
            has_partnerships = False

        # Cache the result
        with _partnership_cache_lock:
            _partnership_cache[org_name] = (has_partnerships, now)
        logging.debug(f"Partnership check for {org_name}: {has_partnerships} (cached)")

        return has_partnerships

    @classmethod
    def get_filter_visibility(cls):
        """
        Determine which dashboard filters to show based on user roles.

        Role-based rules:
            - team_* roles (single team): hide ALL filters (single-team user sees no dropdowns)
            - team_* roles (multiple teams): show team filter only (let user switch between teams)
            - org_* roles (single org): show only team filter (hide org dropdown, it's pre-selected)
            - org_* roles (multiple orgs): show org filter, partnership filter, and team filter
            - Unrestricted roles (superuser, or system-admin in the new
              authz model): show both org and team

        Returns:
            dict: {
                'show_org_filter': bool,
                'show_partnership_filter': bool,
                'show_team_filter': bool
            }
        """
        user_roles = session.get("roles", [])

        # Default: show both filters
        visibility = {
            "show_org_filter": True,
            "show_partnership_filter": False,
            "show_team_filter": True,
        }

        # belt-and-suspenders preflight — when the user has NO
        # legacy ``team_*`` / ``org_*`` role bundle in their session, we
        # can safely consult ``resolve_user_scope`` without disturbing
        # the legacy branches below (they would have produced an empty
        # team_ids / org_ids result anyway). If the resolver returns a
        # non-empty scope from the new-authz path AND that scope is at
        # most one org AND at most one team, hide both filters. This
        # catches the wizard-signup case where memberships exist on the
        # new authz indices but the session.roles list lacks
        # ``team_<title>`` / ``org_<title>`` strings, so the legacy
        # branches never fire and the fall-through's zero-zero
        # branch may have already hidden everything (which is the
        # baseline we're reinforcing) — OR the resolver returns a 1+1
        # scope but the legacy branches don't, in which case this
        # preflight is the load-bearing visibility rule.
        #
        # Gated on the legacy-role absence so a legacy single-org
        # ``org_<title>`` user keeps their earlier behavior
        # (show_team_filter=True, partnership probe via the legacy
        # branch) and the test_role_filter_service fixture's ES mock
        # budget isn't disturbed by an additional resolver-side query.
        _NEW_AUTHZ_SOURCES = frozenset({"membership", "data_grant", "workspace_membership", "workspace_role_assignment"})
        _has_legacy_role = any((role.startswith("team_") or role.startswith("org_")) for role in (user_roles or []))
        if not _has_legacy_role:
            try:
                config = cls.load_config()
                username = session.get("username", "")
                short_scope = resolve_user_scope(username, user_roles, config)
                if not short_scope.is_empty and short_scope.scope_source in _NEW_AUTHZ_SOURCES:
                    short_org_count = len(short_scope.org_titles)
                    short_team_count = len(short_scope.team_titles)
                    if short_org_count <= 1 and short_team_count <= 1:
                        visibility["show_org_filter"] = False
                        visibility["show_team_filter"] = False
                        visibility["show_partnership_filter"] = False
                        logging.debug(
                            "short-circuit hide both filters "
                            f"(scope_source={short_scope.scope_source}, "
                            f"orgs={short_org_count}, teams={short_team_count})"
                        )
                        return visibility
            except Exception as e:  # noqa: BLE001 — never block /dashboard render
                logging.warning(f"get_filter_visibility short-circuit failed: {e}")

        # Check for team roles (team_*)
        team_ids = cls.extract_team_ids_from_roles(user_roles)
        if team_ids:
            visibility["show_org_filter"] = False
            visibility["show_partnership_filter"] = False
            if len(team_ids) == 1:
                # Single-team user: hide all filters
                visibility["show_team_filter"] = False
                logging.debug(f"Single-team user detected, hiding all filters: {user_roles}")
            else:
                # Multi-team user: show team dropdown so they can switch
                visibility["show_team_filter"] = True
                logging.debug(f"Multi-team user detected with {len(team_ids)} teams, showing team filter: {user_roles}")
            return visibility

        # Check for org roles (org_*)
        org_ids = cls.extract_org_ids_from_roles(user_roles)
        if org_ids:
            if len(org_ids) >= 2:
                # Multi-org user: show org dropdown so they can switch
                visibility["show_org_filter"] = True
                visibility["show_partnership_filter"] = True
                logging.debug(f"Multi-org user detected with {len(org_ids)} orgs, showing org filter: {org_ids}")
            else:
                # Single-org user: hide org filter (pre-selected)
                visibility["show_org_filter"] = False

                # Check for partnerships
                org_name = cls.get_org_titles_by_ids([org_ids[0]])
                if org_name and len(org_name) > 0:
                    primary_org = org_name[0]
                    has_partnerships = cls.has_partnership_documents(primary_org)
                    if has_partnerships:
                        visibility["show_partnership_filter"] = True
                        logging.info(f"Single-org user {primary_org} has partnership documents, showing filter")
                    else:
                        logging.debug(f"Single-org user {primary_org} has no partnership documents")

                logging.debug(f"Single-org user detected, hiding org filter: {user_roles}")

            logging.debug(f"Filter visibility for roles {user_roles}: {visibility}")
            return visibility

        # fall-through: the user has no legacy ``team_*`` or
        # ``org_*`` role strings (e.g. a wizard signup post
        # Item 3 — roles=[]). Their scope is resolvable via
        # workspace memberships / role assignments / data grants.
        # Mirror the legacy rules using the count of scopes the
        # resolver returns:
        #
        # * 0 orgs + 0 teams + any number of workspaces -> hide both
        #   filters (an individual / single-workspace user with
        #   nothing to switch between).
        # * 1 org + 0..1 teams -> hide org filter (pre-selected),
        #   show team filter only if multi-team.
        # * 2+ orgs -> show org filter.
        # * Privileged / unscoped readers (resolver returns "no
        #   filter") -> keep the show-both default; same posture as
        # earlier superusers.
        try:
            config = cls.load_config()
            username = session.get("username", "")
            scope = resolve_user_scope(username, user_roles, config)
        except Exception as e:  # noqa: BLE001 — defensive: never block /dashboard render
            logging.warning(f"get_filter_visibility scope resolve failed: {e}")
            logging.debug(f"Filter visibility for roles {user_roles}: {visibility}")
            return visibility

        org_count = len(scope.org_titles)
        team_count = len(scope.team_titles)

        if org_count >= 2:
            visibility["show_org_filter"] = True
            visibility["show_team_filter"] = True
            logging.debug(f"multi-org workspace user ({org_count} orgs); showing both filters")
        elif org_count == 1:
            visibility["show_org_filter"] = False
            if team_count >= 2:
                visibility["show_team_filter"] = True
            else:
                visibility["show_team_filter"] = False
            logging.debug(
                f"single-org workspace user ({team_count} teams); "
                f"hiding org filter, team={visibility['show_team_filter']}"
            )
        else:
            # Zero orgs AND zero teams: individual / single-workspace
            # user with no horizontal scope to switch between. Hide
            # both filters.
            visibility["show_org_filter"] = False
            visibility["show_team_filter"] = False
            logging.debug(
                f"zero-org / zero-team user " f"(workspaces={len(scope.workspace_esids)}); hiding both filters"
            )

        logging.debug(f"Filter visibility for roles {user_roles}: {visibility}")
        return visibility

    @classmethod
    def audit_log_filter_application(cls, username, filter_clause, context="unknown"):
        """
        Log filter application for audit trail.

        Args:
            username: Username of the user
            filter_clause: The filter clause applied (or None)
            context: Context where filter was applied (e.g., "visualization_creation")
        """
        if filter_clause:
            logging.info(f"AUDIT: user={username} context={context} " f"filter_applied=True filter={filter_clause}")
        else:
            logging.info(f"AUDIT: user={username} context={context} " f"filter_applied=False reason=unrestricted_role")
