"""
Tests for dashboard routes and dashboard API routes.

Routes covered:
  GET  /dashboard                              dashboard_page (HTML)
  GET  /api/dashboards/space/<space_id>        get_dashboards_in_space
  GET  /api/dashboards/filter-options           get_dashboard_filter_options
  GET  /api/dashboards/platform-availability    get_platform_availability
  GET  /api/dashboards/data-pulled-as-of        get_data_pulled_as_of

All external service calls are mocked.
"""

from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_DASHBOARDS = [
    {"id": "dash-1", "title": "Facebook Overview", "space_id": "default"},
    {"id": "dash-2", "title": "Google Ads Overview", "space_id": "default"},
]

SAMPLE_ECHARTS_DASHBOARDS = [
    {"id": "ec-1", "title": "Custom Chart", "is_echarts": True, "menu": "Custom"},
]

SAMPLE_PANEL_INFO = {
    "panel_count": 3,
    "first_panel_id": "panel-abc",
}

SAMPLE_SINGLE_PANEL_INFO = {
    "panel_count": 1,
    "first_panel_id": "panel-only",
}


# ===========================================================================
# GET /dashboard  — dashboard_page
# ===========================================================================


class TestDashboardsPage:
    """Tests for the GET /dashboard HTML route."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code in (302, 401)

    @mock.patch("asset_management.web.app.RoleFilterService.get_partner_organizations", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_teams", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_organizations", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_filter_visibility", return_value={})
    @mock.patch("asset_management.web.app.DashboardService.get_visible_echarts_dashboards")
    def test_returns_200_for_admin(
        self, mock_echarts, mock_filter_vis,
        mock_allowed_orgs, mock_allowed_teams, mock_partner_orgs,
        auth_client,
    ):
        mock_echarts.return_value = SAMPLE_ECHARTS_DASHBOARDS
        resp = auth_client.get("/dashboard")
        assert resp.status_code == 200
        mock_echarts.assert_called_once()

    @mock.patch("asset_management.web.app.RoleFilterService.get_partner_organizations", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_teams", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_organizations", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_filter_visibility", return_value={})
    @mock.patch("asset_management.web.app.DashboardService.get_visible_echarts_dashboards")
    def test_renders_workspace_anchor_for_docs_capture(
        self, mock_echarts, mock_filter_vis,
        mock_allowed_orgs, mock_allowed_teams, mock_partner_orgs,
        auth_client,
    ):
        """/dashboard emits the active workspace name into a hidden
        ``data-docs-workspace`` attribute on #dashboard-toolbar so the
        docs-capture sanitization gate has a demo-workspace anchor to assert
        on this page. There is deliberately NO visible on-screen label (it was
        removed for layout reasons); the anchor is attribute-only. Reuses the
        Builder's workspace resolver, mocked here.

        Mutation pin: set ``dashboard_workspace = ""`` in ``dashboard_page``
        (or drop the template ``{% if dashboard_workspace %}`` guard so the
        attribute is always emitted empty) and the assertions below fail.
        """
        import importlib

        mock_echarts.return_value = SAMPLE_ECHARTS_DASHBOARDS
        # Patch the resolver on the exact module dashboard_page imports from
        # (``web.blueprints.dashboard_builder.routes``), so the in-function
        # ``from ... import`` picks up the mock. Falls back to the
        # ``asset_management``-rooted name if that's the loaded alias.
        try:
            db_routes = importlib.import_module(
                "web.blueprints.dashboard_builder.routes"
            )
        except ModuleNotFoundError:  # pragma: no cover - path-setup dependent
            db_routes = importlib.import_module(
                "asset_management.web.blueprints.dashboard_builder.routes"
            )
        with mock.patch.object(
            db_routes,
            "_selectable_workspaces_for_user",
            return_value=[{"esid": "kavanah-demo", "title": "Kavanah Demo"}],
        ), mock.patch.object(
            db_routes, "_default_workspace_esid", return_value="kavanah-demo"
        ):
            resp = auth_client.get("/dashboard")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # Attribute-only anchor: the workspace name must ride on
        # #dashboard-toolbar's data-docs-workspace, NOT as visible copy.
        assert 'data-docs-workspace="Kavanah Demo"' in html
        # Guard against the visible label creeping back in (the whole point of
        # the revert was to keep it off-screen for layout reasons).
        assert "Workspace:" not in html

    @mock.patch("asset_management.web.app.RoleFilterService.get_partner_organizations", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_teams", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_organizations", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_filter_visibility", return_value={})
    @mock.patch("asset_management.web.app.DashboardService.get_visible_echarts_dashboards")
    def test_returns_200_for_org_user(
        self, mock_echarts, mock_filter_vis,
        mock_allowed_orgs, mock_allowed_teams, mock_partner_orgs,
        org_user_client,
    ):
        mock_echarts.return_value = []
        resp = org_user_client.get("/dashboard")
        assert resp.status_code == 200

    @mock.patch("asset_management.web.app.RoleFilterService.get_partner_organizations", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_teams", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_organizations", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_filter_visibility", return_value={})
    @mock.patch("asset_management.web.app.DashboardService.get_visible_echarts_dashboards")
    def test_empty_dashboards(
        self, mock_echarts, mock_filter_vis,
        mock_allowed_orgs, mock_allowed_teams, mock_partner_orgs,
        auth_client,
    ):
        mock_echarts.return_value = []
        resp = auth_client.get("/dashboard")
        assert resp.status_code == 200

    @mock.patch("asset_management.web.app.DashboardService.get_visible_echarts_dashboards")
    def test_service_error_returns_500(self, mock_echarts, auth_client):
        mock_echarts.side_effect = RuntimeError("ES down")
        resp = auth_client.get("/dashboard")
        assert resp.status_code == 500

    @mock.patch("asset_management.web.app.render_template", return_value="<html>OK</html>")
    @mock.patch("asset_management.web.app.RoleFilterService.get_partner_organizations", return_value=["OrgA", "OrgB"])
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_teams", return_value=None)
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_organizations", return_value=["OrgA"])
    @mock.patch("asset_management.web.app.RoleFilterService.get_filter_visibility", return_value={})
    @mock.patch("asset_management.web.app.DashboardService.get_visible_echarts_dashboards", return_value=[])
    @mock.patch("asset_management.web.app.RoleFilterService.get_allowed_teams_for_org_users", return_value=["Team1"])
    @mock.patch("asset_management.web.app.RoleFilterService.get_filter_for_current_user", return_value={"match_all": {}})
    def test_admin_with_auto_filter_triggers_team_lookup(
        self, mock_filter_clause, mock_teams_for_org, mock_echarts,
        mock_filter_vis, mock_allowed_orgs, mock_allowed_teams, mock_partner_orgs,
        mock_render, auth_client,
    ):
        """When allowed_orgs is set but allowed_teams is None, org-to-team lookup fires."""
        resp = auth_client.get("/dashboard")
        assert resp.status_code == 200
        mock_teams_for_org.assert_called_once()
        mock_filter_clause.assert_called_once()


# ===========================================================================
# GET /api/dashboards/space/<space_id>  — get_dashboards_in_space
# ===========================================================================


class TestGetDashboardsInSpace:
    """Tests for the GET /api/dashboards/space/<space_id> API route."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/dashboards/space/default")
        assert resp.status_code in (302, 401)

    @mock.patch("asset_management.web.app.KibanaService.get_dashboards_in_space")
    def test_returns_dashboards_json(self, mock_get, superuser_client):
        mock_get.return_value = SAMPLE_DASHBOARDS
        resp = superuser_client.get("/api/dashboards/space/default")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["space_id"] == "default"
        assert len(data["dashboards"]) == 2
        mock_get.assert_called_once_with("default")

    @mock.patch("asset_management.web.app.KibanaService.get_dashboards_in_space")
    def test_empty_space_returns_empty_list(self, mock_get, superuser_client):
        mock_get.return_value = []
        resp = superuser_client.get("/api/dashboards/space/empty-space")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["dashboards"] == []

    @mock.patch("asset_management.web.app.KibanaService.get_dashboards_in_space")
    def test_service_error_returns_500(self, mock_get, superuser_client):
        mock_get.side_effect = RuntimeError("Kibana unreachable")
        resp = superuser_client.get("/api/dashboards/space/default")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False
        # generic client-facing message; exception text is in the
        # server log only.
        assert data["error"] == "Failed to fetch dashboards for space"


# ===========================================================================
# GET /api/dashboards/filter-options  — get_dashboard_filter_options
# ===========================================================================


class TestFilterOptions:
    """Tests for the GET /api/dashboards/filter-options route.

     Phase B.2 — every test in this class that exercises the
    authenticated branch needs ``resolve_user_scope`` to return a
    non-empty scope, otherwise ``_scope_filter_options_to_user``
    short-circuits to deny-all (returning ``([],[])``) before the
    behaviour under test runs. The class-level fixture wires that
    in once so individual tests stay focused on the post-gate
    aggregation logic they're written to pin.
    """

    @pytest.fixture(autouse=True)
    def _non_empty_scope(self):
        from asset_management.web.services.role_filter_service import UserScope

        with mock.patch(
            "asset_management.web.services.role_filter_service.resolve_user_scope",
            return_value=UserScope(
                org_titles=("OrgA", "OrgB", "Org1", "OrgX"),
                team_titles=("Team1", "CSVTeam", "T1", "T", "Partner Team"),
                scope_source="es_security_legacy",
            ),
        ):
            yield

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/dashboards/filter-options")
        assert resp.status_code in (302, 401)

    @mock.patch("asset_management.web.app.AssetService.get_all")
    @mock.patch(
        "asset_management.web.app.RoleFilterService.should_apply_filter",
        return_value=False,
    )
    @mock.patch(
        # Phase B.4: deny-all gate runs BEFORE should_apply_filter
        # in _scope_filter_options_to_user. Stub True so this test's
        # subject (filter-options narrowing) isn't pre-empted.
        "asset_management.web.app.RoleFilterService.current_user_has_any_scope",
        return_value=True,
    )
    def test_no_platform_returns_asset_based_results(self, _has_scope, _no_filter, mock_get_all, auth_client):
        """Without a platform param, falls through to asset index lookup.

        ``should_apply_filter`` is mocked to False so this test
        continues to exercise the unrestricted-asset_admin behavior — the
        scope-filter pin lives -EXPLORER-001 (release-plan).
        """
        mock_get_all.side_effect = [
            # First call: organizations
            [{"_source": {"organization_title": "OrgA", "organization_logo": "", "organization_description": ""}}],
            # Second call: teams
            [{"_source": {"team_title": "Team1", "organization_title": "OrgA"}}],
        ]
        resp = auth_client.get("/api/dashboards/filter-options")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["organizations"]) == 1
        assert data["organizations"][0]["title"] == "OrgA"
        assert len(data["teams"]) == 1

    @mock.patch("asset_management.web.app.AssetService.get_all")
    @mock.patch(
        "asset_management.web.app.RoleFilterService.should_apply_filter",
        return_value=False,
    )
    @mock.patch(
        "asset_management.web.app.RoleFilterService.current_user_has_any_scope",
        return_value=True,
    )
    def test_team_carries_esid_additively(self, _has_scope, _no_filter, mock_get_all, auth_client):
        # each team must carry its ESID (the doc _id) alongside the
        # title so the import-run path can send a collision-safe team filter.
        # Title stays present for the title-keyed dashboard endpoints.
        mock_get_all.side_effect = [
            [{"_source": {"organization_title": "OrgA"}}],
            [{"_id": "team-esid-1", "_source": {"team_title": "Team1", "organization_title": "OrgA"}}],
        ]
        resp = auth_client.get("/api/dashboards/filter-options")
        assert resp.status_code == 200
        teams = resp.get_json()["teams"]
        assert len(teams) == 1
        assert teams[0]["title"] == "Team1"
        assert teams[0]["esid"] == "team-esid-1"

    @mock.patch("asset_management.web.app.AssetService.get_all")
    def test_unknown_platform_falls_back_to_assets(self, mock_get_all, auth_client):
        """A platform not in PLATFORM_DATA_VIEW_MAP falls through to asset lookup."""
        mock_get_all.side_effect = [
            [{"_source": {"organization_title": "OrgB"}}],
            [{"_source": {"team_title": "Team2", "organization_title": "OrgB"}}],
        ]
        resp = auth_client.get("/api/dashboards/filter-options?platform=Unknown")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    @mock.patch("asset_management.web.app.resolve_data_view_index")
    @mock.patch("asset_management.web.app.AssetService.get_all")
    def test_known_platform_no_index_falls_back(self, mock_get_all, mock_resolve, auth_client):
        """Known platform but resolve returns None => falls back to asset indices."""
        mock_resolve.return_value = None
        mock_get_all.side_effect = [
            [{"_source": {"organization_title": "OrgC"}}],
            [{"_source": {"team_title": "Team3", "organization_title": "OrgC"}}],
        ]
        resp = auth_client.get("/api/dashboards/filter-options?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    @mock.patch("asset_management.web.app.AssetService.get_all")
    def test_org_filter_filters_teams(self, mock_get_all, auth_client):
        """When org param is provided, teams query is filtered."""
        mock_get_all.side_effect = [
            [{"_source": {"organization_title": "OrgA"}}],
            [{"_source": {"team_title": "FilteredTeam", "organization_title": "OrgA"}}],
        ]
        resp = auth_client.get("/api/dashboards/filter-options?org=OrgA")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        # Second call should have received a query argument
        call_args = mock_get_all.call_args_list[1]
        assert call_args[0][0] == "de_asset_teams"
        assert "query" in call_args[1]

    @mock.patch("asset_management.web.app.AssetService.get_all")
    def test_service_error_returns_500(self, mock_get_all, auth_client):
        mock_get_all.side_effect = RuntimeError("ES down")
        resp = auth_client.get("/api/dashboards/filter-options")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch("asset_management.web.app.resolve_data_view_index")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_known_platform_with_es_query(self, mock_es_client, mock_resolve, auth_client):
        """Known platform with resolved index uses ES aggregation."""
        mock_resolve.return_value = "fb_ads_*"
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {
                "orgs": {"buckets": [{"key": "OrgX", "doc_count": 10}]},
                "teams": {"buckets": [{"key": "TeamX", "doc_count": 5}]},
            }
        }
        mock_es_client.return_value = mock_es
        resp = auth_client.get("/api/dashboards/filter-options?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["platform"] == "Echo"

    @mock.patch("asset_management.web.app.AssetService.get_all")
    @mock.patch(
        "asset_management.web.app.RoleFilterService.should_apply_filter",
        return_value=False,
    )
    @mock.patch(
        # Phase B.4: deny-all gate runs BEFORE should_apply_filter
        # in _scope_filter_options_to_user. Stub True so this test's
        # subject (filter-options narrowing) isn't pre-empted.
        "asset_management.web.app.RoleFilterService.current_user_has_any_scope",
        return_value=True,
    )
    def test_filter_type_primary(self, _has_scope, _no_filter, mock_get_all, auth_client):
        """filter_type=primary keeps only single-org teams.

        see ``test_no_platform_returns_asset_based_results`` —
        same scope-filter mock for the same reason.
        """
        mock_get_all.side_effect = [
            [{"_source": {"organization_title": "OrgA"}}],
            [
                {"_source": {"team_title": "SingleOrg", "organization_title": ["OrgA"]}},
                {"_source": {"team_title": "MultiOrg", "organization_title": ["OrgA", "OrgB"]}},
            ],
        ]
        resp = auth_client.get("/api/dashboards/filter-options?org=OrgA&filter_type=primary")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        team_titles = [t["title"] for t in data["teams"]]
        assert "SingleOrg" in team_titles
        assert "MultiOrg" not in team_titles

    @mock.patch("asset_management.web.app.AssetService.get_all")
    @mock.patch(
        "asset_management.web.app.RoleFilterService.should_apply_filter",
        return_value=False,
    )
    @mock.patch(
        # Phase B.4: deny-all gate runs BEFORE should_apply_filter
        # in _scope_filter_options_to_user. Stub True so this test's
        # subject (filter-options narrowing) isn't pre-empted.
        "asset_management.web.app.RoleFilterService.current_user_has_any_scope",
        return_value=True,
    )
    def test_filter_type_partnerships(self, _has_scope, _no_filter, mock_get_all, auth_client):
        """filter_type=partnerships keeps only multi-org teams.

        see ``test_no_platform_returns_asset_based_results``.
        """
        mock_get_all.side_effect = [
            [{"_source": {"organization_title": "OrgA"}}],
            [
                {"_source": {"team_title": "SingleOrg", "organization_title": ["OrgA"]}},
                {"_source": {"team_title": "MultiOrg", "organization_title": ["OrgA", "OrgB"]}},
            ],
        ]
        resp = auth_client.get("/api/dashboards/filter-options?org=OrgA&filter_type=partnerships")
        assert resp.status_code == 200
        data = resp.get_json()
        team_titles = [t["title"] for t in data["teams"]]
        assert "MultiOrg" in team_titles
        assert "SingleOrg" not in team_titles

    @mock.patch("asset_management.web.app.resolve_data_view_index")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_org_filter_uses_match_not_terms(self, mock_es_client, mock_resolve, auth_client):
        """Ensure the org filter aggregation uses match query, not terms.

        The terms query fails for multi-org teams stored as comma-separated
        strings or text-analyzed fields. This regression has occurred multiple
        times — this test catches it at the query level.
        """
        mock_resolve.return_value = "fb_ads_*"
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {
                "orgs": {"buckets": [{"key": "VOGZ", "doc_count": 10}]},
                "teams": {
                    "doc_count": 5,
                    "team_names": {"buckets": [{"key": "FLKVT", "doc_count": 5}]},
                },
            }
        }
        mock_es_client.return_value = mock_es
        resp = auth_client.get("/api/dashboards/filter-options?platform=Echo&org=VOGZ")
        assert resp.status_code == 200

        # Inspect the ES query that was sent
        call_kwargs = mock_es.search.call_args_list[0]
        search_body = call_kwargs[1].get("body") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1]["body"]
        aggs = search_body["aggs"]
        team_filter = aggs["teams"]["filter"]

        # Must use match, not terms/term
        filter_str = str(team_filter)
        assert "match" in filter_str, (
            f"Expected 'match' query for multi-org support, got: {team_filter}"
        )
        assert "'terms'" not in filter_str, (
            "terms query breaks multi-org teams — use match instead"
        )
        assert "'term'" not in filter_str or "'terms'" not in filter_str, (
            "term/terms query breaks multi-org teams — use match instead"
        )

    @mock.patch("asset_management.web.app.resolve_data_view_index")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_multi_org_filter_uses_bool_should_match(self, mock_es_client, mock_resolve, auth_client):
        """Multiple orgs should produce bool/should with match clauses."""
        mock_resolve.return_value = "fb_ads_*"
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {
                "orgs": {"buckets": [
                    {"key": "VOGZ", "doc_count": 10},
                    {"key": "Kavanah", "doc_count": 5},
                ]},
                "teams": {
                    "doc_count": 8,
                    "team_names": {"buckets": [
                        {"key": "FLKVT", "doc_count": 5},
                        {"key": "NFCY", "doc_count": 3},
                    ]},
                },
            }
        }
        mock_es_client.return_value = mock_es
        resp = auth_client.get("/api/dashboards/filter-options?platform=Echo&orgs=VOGZ,Kavanah")
        assert resp.status_code == 200

        search_body = mock_es.search.call_args_list[0][1]["body"]
        team_filter = search_body["aggs"]["teams"]["filter"]

        # Should be bool/should with match clauses
        assert "bool" in team_filter, f"Expected bool query for multi-org, got: {team_filter}"
        should = team_filter["bool"]["should"]
        assert len(should) == 2
        for clause in should:
            assert "match" in clause, f"Expected match clause, got: {clause}"


# ===========================================================================
# GET /api/dashboards/platform-availability  — get_platform_availability
# ===========================================================================


class TestPlatformAvailability:
    """Tests for the GET /api/dashboards/platform-availability route."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/dashboards/platform-availability")
        assert resp.status_code in (302, 401)

    @mock.patch("asset_management.web.app._get_dashboard_index_map", return_value={})
    @mock.patch("asset_management.web.app._get_platform_index_map", return_value={})
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_no_es_returns_500(self, mock_es_client, mock_pmap, mock_dmap, auth_client):
        mock_es_client.return_value = None
        resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch("asset_management.web.app._get_dashboard_index_map", return_value={})
    @mock.patch("asset_management.web.app._get_platform_index_map")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_returns_platform_data(self, mock_es_client, mock_pmap, mock_dmap, auth_client):
        mock_pmap.return_value = {"Meta": "fb_ads_*"}
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {
            "responses": [
                {"hits": {"total": {"value": 42}}}
            ]
        }
        mock_es_client.return_value = mock_es
        resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["platforms"]["Meta"]["has_data"] is True

    @mock.patch("asset_management.web.app._get_dashboard_index_map", return_value={})
    @mock.patch("asset_management.web.app._get_platform_index_map")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_zero_docs_means_no_data(self, mock_es_client, mock_pmap, mock_dmap, auth_client):
        mock_pmap.return_value = {"Echo": "echo_*"}
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {
            "responses": [
                {"hits": {"total": {"value": 0}}}
            ]
        }
        mock_es_client.return_value = mock_es
        resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["platforms"]["Echo"]["has_data"] is False

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_service_error_returns_500(self, mock_es_client, auth_client):
        mock_es_client.side_effect = RuntimeError("connection refused")
        resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch("asset_management.web.app._get_dashboard_index_map", return_value={})
    @mock.patch("asset_management.web.app._get_platform_index_map")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_with_org_filter(self, mock_es_client, mock_pmap, mock_dmap, auth_client):
        mock_pmap.return_value = {"Meta": "fb_ads_*"}
        mock_es = mock.MagicMock()
        # Mock the team title lookup + msearch
        mock_es.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }
        mock_es.msearch.return_value = {
            "responses": [
                {"hits": {"total": {"value": 10}}}
            ]
        }
        mock_es_client.return_value = mock_es
        resp = auth_client.get("/api/dashboards/platform-availability?org=TestOrg")
        assert resp.status_code == 200

    @mock.patch("asset_management.web.app._get_dashboard_index_map", return_value={})
    @mock.patch("asset_management.web.app._get_platform_index_map")
    @mock.patch("asset_management.web.app._get_team_titles_for_org")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    @mock.patch(
        "asset_management.web.app.RoleFilterService.should_apply_filter",
        return_value=False,
    )
    def test_should_union_team_titles_across_multiple_orgs(
        self,
        _no_filter,
        mock_es_client,
        mock_get_teams,
        mock_pmap,
        mock_dmap,
        auth_client,
    ):
        """with ``?org=A&org=B``, the endpoint must resolve teams for
        EVERY org and union the result, not just the first one. Otherwise
        multi-org users see availability scoped to one arbitrary org's teams.

        scope-filter mocked off — see related tests above.
        """
        mock_pmap.return_value = {"Meta": "fb_ads_*"}
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {"responses": [{"hits": {"total": {"value": 1}}}]}
        mock_es_client.return_value = mock_es

        # OrgA has team A1; OrgB has A1 (overlap, must dedupe) and B1.
        mock_get_teams.side_effect = lambda es, org, ft: {
            "OrgA": ["A1"],
            "OrgB": ["A1", "B1"],
        }[org]

        resp = auth_client.get(
            "/api/dashboards/platform-availability?org=OrgA&org=OrgB"
        )
        assert resp.status_code == 200

        # Both orgs' team lookups must run.
        called_orgs = [call.args[1] for call in mock_get_teams.call_args_list]
        assert called_orgs == ["OrgA", "OrgB"]

        # The resulting team filter sent to ES must be the unioned, deduped
        # set across both orgs (A1 appears once, plus B1).
        all_msearch_bodies = mock_es.msearch.call_args_list
        assert all_msearch_bodies, "msearch should have been invoked"
        # The team filter is built into every msearch query body. Stringify
        # the calls and assert both unique team titles appear.
        rendered = repr(all_msearch_bodies)
        assert rendered.count("'A1'") >= 1
        assert "'B1'" in rendered


# ===========================================================================
# GET /api/dashboards/data-pulled-as-of  — get_data_pulled_as_of
# ===========================================================================


class TestDataPulledAsOf:
    """Tests for the GET /api/dashboards/data-pulled-as-of route."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/dashboards/data-pulled-as-of")
        assert resp.status_code in (302, 401)

    def test_unknown_platform_returns_null_timestamp(self, auth_client):
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=NotReal")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["timestamp"] is None

    def test_no_platform_returns_null_timestamp(self, auth_client):
        resp = auth_client.get("/api/dashboards/data-pulled-as-of")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["timestamp"] is None

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    @mock.patch("asset_management.web.app.resolve_data_view_index")
    def test_no_index_pattern_returns_null(self, mock_resolve, mock_es_client, auth_client):
        mock_resolve.return_value = None
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["timestamp"] is None

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    @mock.patch("asset_management.web.app.resolve_data_view_index")
    def test_no_es_client_returns_null(self, mock_resolve, mock_es_client, auth_client):
        mock_resolve.return_value = "echo_*"
        mock_es_client.return_value = None
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["timestamp"] is None

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    @mock.patch("asset_management.web.app.resolve_data_view_index")
    def test_returns_timestamp(self, mock_resolve, mock_es_client, auth_client):
        mock_resolve.return_value = "echo_*"
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {
                "max_ts": {"value_as_string": "2026-04-03T10:00:00.000Z"}
            }
        }
        mock_es_client.return_value = mock_es
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["timestamp"] == "2026-04-03T10:00:00.000Z"
        assert data["platform"] == "Echo"

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    @mock.patch("asset_management.web.app.resolve_data_view_index")
    def test_es_error_returns_null_timestamp(self, mock_resolve, mock_es_client, auth_client):
        """ES errors are caught and return null timestamp (not 500)."""
        mock_resolve.return_value = "echo_*"
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = RuntimeError("timeout")
        mock_es_client.return_value = mock_es
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["timestamp"] is None
