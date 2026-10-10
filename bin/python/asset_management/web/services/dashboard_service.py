"""
Dashboard visibility management service.

Handles dashboard visibility filtering based on de_asset_dashboards configuration
and user role-based access control.
"""

import logging
import os
import sys

from flask import session

# Add parent directory to path to import lib
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from lib.es_helpers import (
    dual_terms,
    get_all_documents,
    get_document_by_id,
    get_elasticsearch_client,
)

# per-dashboard export capability (three-root resolver, same
# reason as the other cross-service imports in this package).
try:
    from asset_management.web.services.export_capability_service import dashboard_export_formats
except ImportError:
    try:
        from web.services.export_capability_service import dashboard_export_formats
    except ImportError:
        from services.export_capability_service import dashboard_export_formats


class DashboardService:
    """Service for managing dashboard visibility and configuration."""

    INDEX_NAME = "de_asset_dashboards"

    @classmethod
    def _resolve_user_scope_esids(cls, user_roles):
        """Resolve the current user's team / org scope into ESIDs.

        Reads ``de_authz_user_team_role`` / ``de_authz_user_org_role``
        for ``session['username']`` and returns the membership ESIDs
        directly. ``user_roles`` is retained on the signature for
        backwards compatibility with existing callers + test mocks but
        is no longer consulted — the legacy ``team_<title>`` /
        ``org_<title>`` regex path was retired after the
        observation cycle on Phase 1 (fail-closed default) ran
        clean.

        the result is the UNION of memberships and active
        ``de_authz_data_grant`` scopes. This keeps the dashboard
        read-authz path consistent with the privileged-user contract:
        an asset_admin / superuser with a grant-only access path can
        still read a ``show_teams`` dashboard scoped to a granted
        team/org. Bare privileged users with neither return empty sets
        and cannot read any non-``show_all`` dashboard.

         follow-up (2026-05-15): an org-scoped grant or
        membership is also expanded to all team_esids belonging to
        that org. Without this, a superuser with an org-ACME grant
        could not read a dashboard scoped to ``allowed_team_esids =
        [ACME-NORTH_esid]`` because the org grant only landed in
        ``org_esids`` and the dashboard only declared team scope.
        Mirrors the title-level expansion ``_grant_scoped_titles`` does
        in role_filter_service.py via ``get_team_titles_by_organization``.

        Returns:
            (team_esids, org_esids) as sets. Empty sets on any failure
            (deny-by-default — the caller treats "no scope" as "no access").
        """
        del user_roles  # retired legacy regex path; param kept for caller compat.
        team_esids, org_esids = cls._resolve_scope_from_new_authz()

        grant_team_esids, grant_org_esids = cls._resolve_scope_from_grants()
        team_esids = team_esids | grant_team_esids
        org_esids = org_esids | grant_org_esids

        # expand org_esids → team_esids so a dashboard that only
        # declares allowed_team_esids is still readable by a user whose
        # scope is org-level.
        if org_esids:
            team_esids = team_esids | cls._expand_org_esids_to_team_esids(org_esids)

        return team_esids, org_esids

    @classmethod
    def _expand_org_esids_to_team_esids(cls, org_esids):
        """return the set of team_esids belonging to any of the
        given org_esids. Looks up ``de_asset_teams`` by the
        ``organization_esid`` field (the stored team→org foreign key).

        ``organization_esid`` drifts between bare ``keyword`` (prod)
        and ``text`` + ``.keyword`` across environments. The old
        ``.keyword``-only ``terms`` query silently returned zero team_esids on
        bare-keyword prod, collapsing the org's dashboard set to empty.
        ``dual_terms`` matches on either shape.

        Returns an empty set on ES failure or empty input.
        """
        if not org_esids:
            return set()
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("DashboardService: ES unavailable; cannot expand org→team_esids")
            return set()
        try:
            result = es.search(
                index="de_asset_teams",
                body={
                    "query": dual_terms("organization_esid", list(org_esids)),
                    "_source": [],
                    "size": 1000,
                },
            )
        except Exception as e:
            logging.error("DashboardService: error expanding org_esids %s: %s", org_esids, e)
            return set()
        return {hit["_id"] for hit in result.get("hits", {}).get("hits", []) if hit.get("_id")}

    @classmethod
    def _expand_workspace_esids_to_org_esids(cls, workspace_esids):
        """return the set of org_esids belonging to any of the
        given workspace_esids. Looks up ``de_asset_organization`` by the
        ``workspace_esid`` field (the F5 backfilled foreign key).

        Same fail-soft contract as ``_expand_org_esids_to_team_esids``:
        ES outage or empty input returns an empty set, and the caller
        treats that as no expansion.
        """
        if not workspace_esids:
            return set()
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("DashboardService: ES unavailable; cannot expand workspace→org_esids")
            return set()
        try:
            result = es.search(
                index="de_asset_organization",
                body={
                    "query": {"terms": {"workspace_esid": list(workspace_esids)}},
                    "_source": [],
                    "size": 10000,
                },
            )
        except Exception as e:
            logging.error("DashboardService: error expanding workspace_esids %s: %s", workspace_esids, e)
            return set()
        return {hit["_id"] for hit in result.get("hits", {}).get("hits", []) if hit.get("_id")}

    @classmethod
    def _resolve_scope_from_grants(cls):
        """pull team/org ESIDs from the session user's active
        ``de_authz_data_grant`` docs. Returns empty sets when the user
        has no grants or when ES is unavailable — the caller unions
        the result with memberships, so an empty grant set is a no-op.

        Expired grants are filtered out by ``DataGrantService.list_for_user``.
        """
        team_esids: set = set()
        org_esids: set = set()

        username = session.get("username", "") if session else ""
        if not username:
            return team_esids, org_esids

        try:
            from .data_grant_service import (
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

        try:
            grants = DataGrantService.list_for_user(username)
        except Exception as e:
            logging.error("DashboardService: error listing grants for %s: %s", username, e)
            return team_esids, org_esids

        # (Issue 1): collect workspace grants and resolve them to
        # org_esids on this pass so the caller's existing org→team expansion
        # picks them up. Without this branch, a workspace grant contributes
        # zero esids and show_teams dashboards stay hidden from the menu.
        workspace_grant_esids: set = set()
        for grant in grants or []:
            scope_type = grant.get("scope_type")
            esid = grant.get("scope_esid")
            if not esid:
                continue
            if scope_type == SCOPE_TYPE_TEAM:
                team_esids.add(esid)
            elif scope_type == SCOPE_TYPE_ORG:
                org_esids.add(esid)
            elif scope_type == SCOPE_TYPE_WORKSPACE:
                workspace_grant_esids.add(esid)

        if workspace_grant_esids:
            org_esids = org_esids | cls._expand_workspace_esids_to_org_esids(workspace_grant_esids)

        return team_esids, org_esids

    @classmethod
    def _resolve_scope_from_new_authz(cls):
        """pull team/org membership ESIDs from the new-authz
        indices, keyed by ``session['username']``. Mirrors the read
        pattern in ``RoleFilterService._resolve_titles_from_user_role_index``
        but returns ESIDs directly without the title-resolution step
        (dashboards already store scope as ESIDs)."""
        team_esids: set = set()
        org_esids: set = set()

        username = session.get("username", "") if session else ""
        if not username:
            return team_esids, org_esids

        es = get_elasticsearch_client()
        if es is None:
            logging.warning(
                "DashboardService: ES unavailable; returning empty scope for %s",
                username,
            )
            return team_esids, org_esids

        for index, esid_field, target_set in (
            ("de_authz_user_team_role", "team_esid", team_esids),
            ("de_authz_user_org_role", "org_esid", org_esids),
        ):
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
                logging.error(
                    "DashboardService: error querying %s for %s: %s",
                    index,
                    username,
                    e,
                )
                continue

            for hit in result.get("hits", {}).get("hits", []):
                esid = (hit.get("_source") or {}).get(esid_field)
                if esid:
                    target_set.add(esid)

        return team_esids, org_esids

    @classmethod
    def get_visible_echarts_dashboards(cls, user_roles=None, is_admin=False):
        """
        Return ECharts dashboards from de_asset_echarts_dashboards that are
        visible to the current user, including the 'menu' field.

         (2026-05-15): the ``is_admin`` short-circuit branches
        have been removed. earlier, asset_admin/superuser saw every
        dashboard regardless of visibility_mode + allowed_*_esids —
        which mirrored the same data-access leak the dashboard
        listing endpoint had. The menu in the sidebar reflected that
        leak: admins saw entries for dashboards they shouldn't be
        seeing. Now ALL users resolve through ``visibility_mode`` +
        ``_resolve_user_scope_esids`` (which itself includes
        memberships UNION grant-derived ESIDs). Bare
        admins see only ``show_all`` dashboards in the menu;
        ``show_teams`` dashboards only appear when scope intersects.

        ``is_admin`` is retained on the signature for backward
        compatibility with one external caller in
        ``analytics/routes.py:510`` but is no longer consulted here.
        """
        del is_admin  # parameter retained for signature compat only.
        if user_roles is None:
            user_roles = []
        try:
            docs = get_all_documents("de_asset_echarts_dashboards", {"match_all": {}})
            # Resolve once — roles are constant for the request.
            user_team_esids, user_org_esids = cls._resolve_user_scope_esids(user_roles)

            # #1 / workspace boundary on dashboard
            # visibility. Pre-fix: any visibility_mode='show_all' dashboard
            # was visible to every user regardless of which workspace
            # owned it. A workspace-B user saw kavanah-media dashboards
            # in their sidebar. The fix layers a workspace check on top
            # of the visibility_mode + allowed_*_esids check.
            #
            # ``allowed_workspace_esids`` is the set of workspaces the
            # current user can access:
            # * ``None`` — no workspace filter: only a role-less
            # non-privileged account or a failed lookup (:
            #   privileged users always get a list).
            # * empty set — user has no workspace scope; see no
            #   workspace-stamped dashboards (legacy unstamped dashboards
            #   still pass through, see the per-doc check below).
            # * non-empty set — narrow to dashboards whose
            #   ``workspace_esid`` is in this set.
            from asset_management.web.services.role_filter_service import (
                RoleFilterService,
            )

            allowed_ws = RoleFilterService.get_allowed_workspaces()
            allowed_workspace_esids: set | None = None if allowed_ws is None else set(allowed_ws)

            # regression fix: a creator always sees their own dashboard
            # in the sidebar (except when they deliberately hid it), regardless
            # of team scope or workspace gate. Without this, a creator with no
            # team scope (a bare superuser/admin) never sees the teams-only
            # dashboard they just created. Guard the session read so a
            # non-request caller (unit tests, background jobs) degrades to "no
            # creator" rather than raising out of the resolver.
            try:
                current_username = session.get("username", "") or ""
            except Exception:  # noqa: BLE001 — outside a request context
                current_username = ""

            # #2a: workspace-scoped admin visibility. A privileged admin
            # sees every dashboard within the workspaces they can resolve (the
            # workspace gate below already narrows to those; a privileged user
            # resolves to the workspaces they reach through membership, role or
            # grant, not all of them). Mirrors the same rule
            # in the Builder's _user_may_read_dashboard so the sidebar and the
            # Builder agree. Resolved once — privilege is request-constant.
            # Require a real logged-in username: current_user_is_privileged()
            # returns True OUTSIDE a request context (its CLI contract), so
            # gating on a username avoids a fail-open when this resolver is
            # called without a session (and keeps the non-privileged unit tests
            # honest — they assert scope behavior with no session).
            try:
                is_privileged_admin = bool(current_username) and RoleFilterService.current_user_is_privileged()
            except Exception:  # noqa: BLE001
                is_privileged_admin = False

            from asset_management.web.services.dashboard_team_labels import (
                apply_duplicate_team_labels,
                owner_team_esid,
            )

            visible = []
            # (workspace, owning team) per entry, for the duplicate-title labels.
            label_meta: dict[str, tuple[str, str | None]] = {}
            for doc in docs:
                source = doc.get("_source", {})
                # follow-up: canvas pages join the end-user
                # dashboards sidebar — they render through the same
                # /dashboard-builder/view/<id> iframe (the route serves the
                # read-only canvas viewer for them), so visibility settings
                # made in the Builder actually take effect somewhere.
                if source.get("dashboard_type", "echarts") not in ("echarts", "canvas"):
                    continue
                dashboard_id = source.get("dashboard_id") or doc.get("_id", "")
                title = source.get("title", "Untitled")
                menu = source.get("menu", "") or ""
                menu_position = source.get("menu_position")
                visibility_mode = source.get("visibility_mode", "show_all")
                dashboard_workspace_esid = source.get("workspace_esid") or ""
                is_creator = bool(current_username) and source.get("created_by") == current_username
                label_meta[dashboard_id] = (dashboard_workspace_esid, owner_team_esid(source))

                # #1: workspace gate. Applied BEFORE the
                # visibility_mode check so a show_all dashboard from
                # another workspace doesn't leak. Legacy dashboards
                # without a workspace_esid stamp pass through — the
                # backfill tool (tools/backfill_dashboard_workspace_esid.py)
                # stamps them so this branch becomes deny-only once
                # backfill is applied. The creator bypasses this gate for
                # their own dashboard.
                if (
                    not is_creator
                    and allowed_workspace_esids is not None
                    and dashboard_workspace_esid
                    and dashboard_workspace_esid not in allowed_workspace_esids
                ):
                    continue

                # surface "is this dashboard's report URL live?"
                # so the dashboards-page sidebar can show a small icon next
                # to each row. ``published`` is True only when an unexpired
                # token exists; expired tokens read as unpublished.
                from asset_management.web.blueprints.dashboard_builder.routes import _is_publish_expired

                published_active = bool(
                    source.get("publish_enabled") and source.get("publish_token") and not _is_publish_expired(source)
                )

                entry = {
                    "id": dashboard_id,
                    "title": title,
                    "menu": menu,
                    "menu_position": menu_position,
                    # canvas entries keep is_echarts=True — for the
                    # sidebar it means "renders via the viewer iframe", which
                    # holds for both types; dashboard_type disambiguates.
                    "is_echarts": True,
                    "dashboard_type": source.get("dashboard_type", "echarts"),
                    # supported export formats (capability resolver:
                    # canvas → pdf-only; echarts → derived from chart
                    # membership). The dashboards-page export modal gates its
                    # format radios on this instead of offering all three
                    # unconditionally.
                    "export_formats": list(dashboard_export_formats(source)),
                    "published": published_active,
                    # expose the owning workspace so the read-only
                    # dashboards sidebar can scope its render to the same
                    # single workspace its menu order resolves to (a user who
                    # reaches several workspaces sees all their dashboards here,
                    # which otherwise leak cross-workspace menus into the sidebar's
                    # orphan tail). Harmless to other callers.
                    "workspace_esid": dashboard_workspace_esid,
                }

                if visibility_mode == "hide":
                    # ``hide`` is a deliberate choice — respect it even for the
                    # creator (they can un-hide it from the Builder).
                    continue
                elif visibility_mode == "show_all":
                    visible.append(entry)
                elif visibility_mode == "show_teams":
                    # the creator always sees their own show_teams
                    # dashboard, even with no intersecting team scope.
                    if is_creator:
                        visible.append(entry)
                        continue
                    # #2a: a privileged admin sees every show_teams
                    # dashboard in a workspace they can resolve (the workspace
                    # gate above already enforced the boundary). Org/team users
                    # fall through to the team-intersection check.
                    if is_privileged_admin:
                        visible.append(entry)
                        continue
                    allowed_org_esids = source.get("allowed_org_esids", []) or []
                    allowed_team_esids = source.get("allowed_team_esids", []) or []
                    if not allowed_org_esids and not allowed_team_esids:
                        continue
                    user_has_access = not user_team_esids.isdisjoint(allowed_team_esids) or not user_org_esids.isdisjoint(
                        allowed_org_esids
                    )
                    if user_has_access:
                        visible.append(entry)
            # name the owning team on same-titled entries. Grouping is
            # per workspace, so the route's later single-workspace narrowing
            # cannot split a group. A non-privileged viewer only ever
            # sees their own teams' names.
            apply_duplicate_team_labels(visible, label_meta, None if is_privileged_admin else set(user_team_esids))
            return visible
        except Exception as e:
            logging.error(f"Error fetching visible ECharts dashboards: {e}")
            return []

    @staticmethod
    def get_dashboard_visibility(dashboard_id):
        """
        Get visibility configuration for a specific dashboard.

        Args:
            dashboard_id: Kibana dashboard ID

        Returns:
            Dictionary with visibility config or None if not found
        """
        try:
            query = {"query": {"term": {"dashboard_id.keyword": dashboard_id}}}

            results = get_all_documents(DashboardService.INDEX_NAME, query)

            if results and len(results) > 0:
                return results[0]["_source"]

            return None

        except Exception as e:
            logging.error(f"Error getting dashboard visibility for {dashboard_id}: {e}")
            return None
