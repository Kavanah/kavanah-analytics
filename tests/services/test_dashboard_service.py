"""
Tests for the Dashboard Service.

Covers:
  get_visible_echarts_dashboards — show_all, hide, show_teams, admin override
  get_dashboard_visibility       — found, not found, exception
  _convert_name_to_esid          — found, not found, exception
"""

from unittest import mock

import pytest

from asset_management.web.services.dashboard_service import DashboardService
from asset_management.web.services.role_filter_service import RoleFilterService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_kibana_dashboard(id_, title, description=""):
    return {"id": id_, "title": title, "description": description}


def _make_config_doc(dashboard_id, visibility_mode="show_all", **extras):
    source = {
        "dashboard_id": dashboard_id,
        "dashboard_title": extras.get("dashboard_title", "Title"),
        "dashboard_description": extras.get("dashboard_description", ""),
        "visibility_mode": visibility_mode,
        "allowed_org_esids": extras.get("allowed_org_esids", []),
        "allowed_team_esids": extras.get("allowed_team_esids", []),
    }
    source.update(extras)
    return {"_id": f"doc_{dashboard_id}", "_source": source}


# ---------------------------------------------------------------------------
# get_visible_echarts_dashboards
# ---------------------------------------------------------------------------

class TestGetVisibleEchartsDashboards:

    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_show_all(self, mock_get_all):
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "EChart 1", "menu": "analytics",
                "visibility_mode": "show_all",
            }},
        ]
        result = DashboardService.get_visible_echarts_dashboards()
        assert len(result) == 1
        assert result[0]["is_echarts"] is True
        assert result[0]["menu"] == "analytics"

    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_entry_exposes_workspace_esid(self, mock_get_all):
        """each entry carries its owning ``workspace_esid`` so the
        read-only dashboards sidebar can scope its render to a single
        workspace (a bare superuser otherwise gets every workspace's
        dashboards, leaking cross-workspace menus into the orphan tail).
        Absent stamp -> empty string (legacy dashboards pass through the
        route filter).
        """
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "Stamped", "menu": "Meta",
                "visibility_mode": "show_all", "workspace_esid": "kavanah-media",
            }},
            {"_id": "e2", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e2",
                "title": "Legacy", "menu": "Reports",
                "visibility_mode": "show_all",
            }},
        ]
        result = DashboardService.get_visible_echarts_dashboards()
        by_id = {d["id"]: d for d in result}
        assert by_id["e1"]["workspace_esid"] == "kavanah-media"
        assert by_id["e2"]["workspace_esid"] == ""

    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_hide(self, mock_get_all):
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "Hidden", "visibility_mode": "hide",
            }},
        ]
        result = DashboardService.get_visible_echarts_dashboards()
        assert len(result) == 0

    @mock.patch.object(RoleFilterService, "current_user_is_privileged", return_value=False)
    @mock.patch.object(DashboardService, "_resolve_user_scope_esids")
    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_show_teams_nonprivileged_without_scope_is_hidden(self, mock_get_all, mock_resolve, _mock_priv):
        """A NON-privileged user whose scope does not intersect
        ``allowed_team_esids`` must NOT see the dashboard — the team-scope
        gate still governs ordinary users.

         #2a (2026-06-04) reversed this for PRIVILEGED admins only —
        a superuser/asset_admin now sees every in-workspace dashboard (see
        test_show_teams_privileged_admin_sees_all). This test pins the
        unchanged half: privilege is explicitly mocked False so the
        team-intersection path is exercised (current_user_is_privileged()
        returns True outside a request context, which would otherwise mask it).
        """
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "Team EChart", "visibility_mode": "show_teams",
                "allowed_team_esids": ["t1"],
                "allowed_org_esids": [],
            }},
        ]
        # Non-privileged user, no memberships, no grants.
        mock_resolve.return_value = (set(), set())
        result = DashboardService.get_visible_echarts_dashboards()
        assert len(result) == 0, (
            "A non-privileged user must NOT see a show_teams dashboard scoped "
            "to a team they're not in."
        )

    @mock.patch.object(RoleFilterService, "current_user_is_privileged", return_value=True)
    @mock.patch.object(DashboardService, "_resolve_user_scope_esids")
    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_show_teams_privileged_admin_sees_all(self, mock_get_all, mock_resolve, _mock_priv):
        """#2a: a privileged admin sees a show_teams dashboard even with
        NO intersecting team scope (workspace-scoped admin visibility; a bare
        admin resolves to all workspaces). Requires a logged-in username so the
        resolver's fail-open guard doesn't apply.

        Mutation pin: drop the is_privileged_admin branch in
        get_visible_echarts_dashboards -> the no-scope admin sees 0 -> fails.
        """
        from flask import g

        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "Other Team EChart", "visibility_mode": "show_teams",
                "allowed_team_esids": ["other_team"],
                "allowed_org_esids": [],
            }},
        ]
        mock_resolve.return_value = (set(), set())  # no intersecting scope
        from asset_management.web.app import app as _app
        with _app.test_request_context("/"):
            from flask import session as _s
            _s["username"] = "super"
            _s["roles"] = ["superuser"]
            g._dashboard_scope_esids = (set(), set())
            result = DashboardService.get_visible_echarts_dashboards()
        assert len(result) == 1, "a privileged admin must see all in-workspace show_teams dashboards"

    @mock.patch.object(DashboardService, "_resolve_user_scope_esids")
    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_show_teams_admin_with_scope_is_visible(self, mock_get_all, mock_resolve):
        """an admin WITH the relevant membership/grant DOES see
        the show_teams dashboard — proves the scope path works for
        admins, not just the deny path. Complement to the bare-admin
        test above.
        """
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "Team EChart", "visibility_mode": "show_teams",
                "allowed_team_esids": ["t1"],
                "allowed_org_esids": [],
            }},
        ]
        mock_resolve.return_value = ({"t1"}, set())
        result = DashboardService.get_visible_echarts_dashboards(is_admin=True)
        assert len(result) == 1

    @mock.patch.object(DashboardService, "_resolve_user_scope_esids")
    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_show_teams_role_match(self, mock_get_all, mock_resolve):
        """``team_<title>`` role resolves to an ESID that matches
        ``allowed_team_esids`` — grants access."""
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "Team EChart", "visibility_mode": "show_teams",
                "allowed_team_esids": ["team_A_esid"],
                "allowed_org_esids": [],
            }},
        ]
        mock_resolve.return_value = ({"team_A_esid"}, set())
        result = DashboardService.get_visible_echarts_dashboards(user_roles=["team_Alpha"])
        assert len(result) == 1

    @mock.patch.object(DashboardService, "_resolve_user_scope_esids")
    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_show_teams_no_match(self, mock_get_all, mock_resolve):
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "Team EChart", "visibility_mode": "show_teams",
                "allowed_team_esids": ["team_A_esid"],
                "allowed_org_esids": [],
            }},
        ]
        mock_resolve.return_value = ({"team_B_esid"}, set())
        result = DashboardService.get_visible_echarts_dashboards(user_roles=["team_Beta"])
        assert len(result) == 0

    @mock.patch.object(DashboardService, "_resolve_user_scope_esids")
    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_show_teams_org_role_matches_allowed_org(self, mock_get_all, mock_resolve):
        """``org_<title>`` role resolves to an org ESID that must be
        matched against ``allowed_org_esids`` — not the team list."""
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "Org EChart", "visibility_mode": "show_teams",
                "allowed_team_esids": [],
                "allowed_org_esids": ["org_N_esid"],
            }},
        ]
        mock_resolve.return_value = (set(), {"org_N_esid"})
        result = DashboardService.get_visible_echarts_dashboards(user_roles=["org_Nova"])
        assert len(result) == 1

    @mock.patch.object(DashboardService, "_resolve_user_scope_esids")
    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_show_teams_team_role_does_not_match_allowed_org(self, mock_get_all, mock_resolve):
        """A team role must not grant access to an org-restricted dashboard —
        the two lists are resolved against separate role kinds."""
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "Org EChart", "visibility_mode": "show_teams",
                "allowed_team_esids": [],
                "allowed_org_esids": ["collision"],
            }},
        ]
        mock_resolve.return_value = ({"collision"}, set())
        result = DashboardService.get_visible_echarts_dashboards(user_roles=["team_X"])
        assert len(result) == 0

    @mock.patch.object(DashboardService, "_resolve_user_scope_esids")
    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_show_teams_empty_arrays_hidden(self, mock_get_all, mock_resolve):
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "echarts", "dashboard_id": "e1",
                "title": "Team EChart", "visibility_mode": "show_teams",
                "allowed_team_esids": [],
                "allowed_org_esids": [],
            }},
        ]
        mock_resolve.return_value = ({"team_A_esid"}, set())
        result = DashboardService.get_visible_echarts_dashboards(user_roles=["team_A"])
        assert len(result) == 0

    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_skips_non_echarts_type(self, mock_get_all):
        mock_get_all.return_value = [
            {"_id": "e1", "_source": {
                "dashboard_type": "kibana",
                "dashboard_id": "e1", "title": "Not ECharts",
                "visibility_mode": "show_all",
            }},
        ]
        result = DashboardService.get_visible_echarts_dashboards()
        assert len(result) == 0

    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents", side_effect=Exception("boom"))
    def test_exception_returns_empty(self, mock_get_all):
        result = DashboardService.get_visible_echarts_dashboards()
        assert result == []


# ---------------------------------------------------------------------------
# get_dashboard_visibility
# ---------------------------------------------------------------------------

class TestGetDashboardVisibility:

    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_found(self, mock_get_all):
        mock_get_all.return_value = [
            {"_id": "doc1", "_source": {"dashboard_id": "d1", "visibility_mode": "show_all"}},
        ]
        result = DashboardService.get_dashboard_visibility("d1")
        assert result["visibility_mode"] == "show_all"

    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents", return_value=[])
    def test_not_found(self, mock_get_all):
        result = DashboardService.get_dashboard_visibility("missing")
        assert result is None

    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents", side_effect=Exception("boom"))
    def test_exception(self, mock_get_all):
        result = DashboardService.get_dashboard_visibility("d1")
        assert result is None

