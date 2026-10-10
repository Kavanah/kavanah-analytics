"""
Tests for dashboard builder blueprint routes.

Routes covered:
  GET  /builder/                                    index (HTML)
  GET  /builder/view/<dashboard_id>                 view_dashboard (HTML)
  GET  /builder/debug                               debug_view (HTML)
  GET  /builder/api/data-views                      list_data_views
  GET  /builder/api/data-views/<id>/fields           get_fields
  GET  /builder/api/dashboards                      list_dashboards
  POST /builder/api/dashboards                      save_dashboard
  GET  /builder/api/dashboards/<id>                 load_dashboard
  DELETE /builder/api/dashboards/<id>               delete_dashboard
  POST /builder/api/query                           run_query
  POST /builder/api/trend-line-data                 get_trend_line_data

Helper functions (unit tests):
  _simplify_es_type
  _flatten_mapping
  _resolve_date_math

All external service calls are mocked.
"""

from unittest import mock

import pytest

# Helper function imports (pure logic, no request context)
from asset_management.web.blueprints.dashboard_builder.routes import (
    _simplify_es_type,
    _flatten_mapping,
    _resolve_date_math,
)

# validate_index_access is real now. These tests cover
# orthogonal route logic under a session with no ES backend, so grant the gate.
pytestmark = pytest.mark.usefixtures("allow_index_scope")

# Common mock target prefix
_R = "asset_management.web.blueprints.dashboard_builder.routes"
# the chart-render chokepoint now routes through the dedup helper,
# which calls build_query in its own module. With the dedup flag OFF (default)
# it's still exactly one build_query call, but at this namespace, not _R's. The
# route resolves the helper via the ``services.`` import path (the ``web.``
# variant has no top-level ``web`` package in the test env), so patch there.
_DR = "services.dedup_routing"


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_ES_HITS = [
    {
        "_id": "dash-1",
        "_source": {
            "dashboard_id": "dash-1",
            "dashboard_type": "echarts",
            "title": "Campaign Overview",
            "updated_at": "2026-03-15T12:00:00Z",
        },
    },
    {
        "_id": "dash-2",
        "_source": {
            "dashboard_id": "dash-2",
            "dashboard_type": "echarts",
            "title": "Google Ads Report",
            "updated_at": "2026-03-10T08:00:00Z",
        },
    },
]

SAMPLE_KIBANA_DASHBOARD = {
    "_id": "kib-1",
    "_source": {
        "dashboard_id": "kib-1",
        "dashboard_type": "kibana",
        "title": "Kibana Only",
        "updated_at": "2026-03-01T00:00:00Z",
    },
}

SAMPLE_ECHARTS_SOURCE = {
    "dashboard_id": "dash-1",
    "dashboard_type": "echarts",
    "title": "Campaign Overview",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-03-15T12:00:00Z",
    "visibility_mode": "show_all",
    "allowed_org_esids": [],
    "allowed_team_esids": [],
    "echarts_config": {
        "global_date_range": {"field": "kibana_date", "from": "now-30d/d", "to": "now"},
        "charts": [
            {
                "chart_id": "c1",
                "title": "Reach Over Time",
                "chart_type": "bar",
                "data_view": "fb_insights",
                "x_field": "kibana_date",
                "y_field": "insight_reach",
            }
        ],
    },
}

SAMPLE_LEAFLET_SOURCE = {
    "dashboard_id": "map-1",
    "dashboard_type": "leaflet_map",
    "title": "Map Dashboard",
    "map_config": {"zoom": 3},
}


# ===========================================================================
# Auth: unauthenticated access
# ===========================================================================


class TestBuilderAuthRequired:
    """All builder routes require authentication."""

    @pytest.mark.parametrize(
        "url,method",
        [
            ("/dashboard-builder/", "GET"),
            ("/dashboard-builder/view/some-id", "GET"),
            ("/dashboard-builder/debug", "GET"),
            ("/dashboard-builder/api/data-views", "GET"),
            ("/dashboard-builder/api/data-views/my-index/fields", "GET"),
            ("/dashboard-builder/api/dashboards", "GET"),
            ("/dashboard-builder/api/dashboards", "POST"),
            ("/dashboard-builder/api/dashboards/dash-1", "GET"),
            ("/dashboard-builder/api/dashboards/dash-1", "DELETE"),
            ("/dashboard-builder/api/query", "POST"),
            ("/dashboard-builder/api/trend-line-data", "POST"),
        ],
    )
    def test_unauthenticated_is_bounced_to_login(self, client, url, method):
        # full-page navigations (the builder/view/debug HTML pages) keep
        # the 302 -> /login; API calls under /dashboard-builder/api/ are
        # classified as AJAX/API callers and get 401 + JSON {login_url} so a
        # fetch redirects the tab cleanly instead of parsing the login HTML.
        if method == "GET":
            resp = client.get(url)
        elif method == "POST":
            resp = client.post(url, json={})
        elif method == "DELETE":
            resp = client.delete(url)
        if "/api/" in url:
            assert resp.status_code == 401
            assert "login" in (resp.get_json() or {}).get("login_url", "").lower()
        else:
            assert resp.status_code == 302
            assert "login" in (resp.headers.get("Location") or "").lower()


# ===========================================================================
# Page routes
# ===========================================================================


class TestIndexPage:
    """GET /builder/ — renders the builder index page."""

    def test_returns_200(self, auth_client):
        resp = auth_client.get("/dashboard-builder/")
        assert resp.status_code == 200

    def test_returns_html(self, auth_client):
        resp = auth_client.get("/dashboard-builder/")
        assert b"<!DOCTYPE html>" in resp.data or b"<html" in resp.data

    def test_custom_role_with_mc_dashboard_builder_can_access(self, app):
        """page gates on mc_dashboard_builder so custom roles
        with the perm can reach the builder."""
        import time as _time
        from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient

        c = app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "builder"
            sess["roles"] = ["custom_dashboard_builder"]
            sess["is_admin"] = False
            sess["full_name"] = "Builder"
            sess["email"] = "b@example.com"
            sess["auth_timestamp"] = _time.time()
            sess["csrf_token"] = CSRF_TEST_TOKEN
        client = CsrfTestClient(c)
        # The blueprint imports RolePermissionsService via `from
        # services.role_permissions_service ...` while auth.py imports it
        # via `from .services.role_permissions_service ...` — those resolve
        # to two distinct class objects in sys.modules. Patch both so the
        # before_request authorizer and the route handler both see the
        # mocked perms.
        with (
            mock.patch(
                f"{_R}.RolePermissionsService.get_merged_permissions",
                return_value={"mc_dashboard_builder": True},
            ),
            mock.patch(
                "asset_management.web.services.role_permissions_service.RolePermissionsService.get_merged_permissions",
                return_value={"mc_dashboard_builder": True},
            ),
        ):
            resp = client.get("/dashboard-builder/")
        assert resp.status_code == 200

    def test_custom_role_without_mc_dashboard_builder_redirected(self, app):
        import time as _time
        from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient

        c = app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "viewer"
            sess["roles"] = ["custom_dashboard_viewer"]
            sess["is_admin"] = False
            sess["full_name"] = "Viewer"
            sess["email"] = "v@example.com"
            sess["auth_timestamp"] = _time.time()
            sess["csrf_token"] = CSRF_TEST_TOKEN
        client = CsrfTestClient(c)
        with (
            mock.patch(
                f"{_R}.RolePermissionsService.get_merged_permissions",
                return_value={"mc_dashboard_builder": False},
            ),
            mock.patch(
                "asset_management.web.services.role_permissions_service.RolePermissionsService.get_merged_permissions",
                return_value={"mc_dashboard_builder": False},
            ),
        ):
            resp = client.get("/dashboard-builder/")
        # Page-level 403 → redirect via global handler.
        assert resp.status_code == 302


class TestViewDashboardPage:
    """GET /builder/view/<dashboard_id> — renders the view page."""

    @mock.patch(f"{_R}.get_document_by_id")
    def test_returns_200(self, mock_get, auth_client):
        # view page now checks dashboard visibility before rendering.
        mock_get.return_value = {"dashboard_type": "echarts", "visibility_mode": "show_all"}
        resp = auth_client.get("/dashboard-builder/view/test-dash-id")
        assert resp.status_code == 200

    @mock.patch(f"{_R}.get_document_by_id")
    def test_returns_html(self, mock_get, auth_client):
        mock_get.return_value = {"dashboard_type": "echarts", "visibility_mode": "show_all"}
        resp = auth_client.get("/dashboard-builder/view/test-dash-id")
        assert b"<!DOCTYPE html>" in resp.data or b"<html" in resp.data

    @mock.patch(f"{_R}.get_document_by_id")
    def test_dashboard_id_in_context(self, mock_get, auth_client):
        mock_get.return_value = {"dashboard_type": "echarts", "visibility_mode": "show_all"}
        resp = auth_client.get("/dashboard-builder/view/my-special-id")
        assert resp.status_code == 200


class TestDebugPage:
    """GET /builder/debug — renders the debug page (superuser-only)."""

    def test_returns_200(self, superuser_client):
        resp = superuser_client.get("/dashboard-builder/debug")
        assert resp.status_code == 200


# ===========================================================================
# Data Views API
# ===========================================================================


class TestListDataViews:
    """GET /builder/api/data-views"""

    @mock.patch(f"{_R}._fetch_kibana_data_views")
    def test_returns_kibana_data_views(self, mock_fetch, auth_client):
        mock_fetch.return_value = [
            {"id": "fb_insights", "label": "Facebook Insights"},
            {"id": "google_ads", "label": "Google Ads"},
        ]
        resp = auth_client.get("/dashboard-builder/api/data-views")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["data_views"]) == 2
        assert data["data_views"][0]["id"] == "fb_insights"

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}._fetch_kibana_data_views")
    def test_fallback_to_es_indices(self, mock_fetch, mock_es, auth_client):
        mock_fetch.return_value = []
        es_mock = mock.MagicMock()
        es_mock.indices.get.return_value = {
            "fb_insights": {},
            "google_ads": {},
            ".kibana": {},  # system index, should be excluded
        }
        mock_es.return_value = es_mock
        resp = auth_client.get("/dashboard-builder/api/data-views")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        ids = [dv["id"] for dv in data["data_views"]]
        assert "fb_insights" in ids
        assert "google_ads" in ids
        assert ".kibana" not in ids

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}._fetch_kibana_data_views")
    def test_es_fallback_failure_returns_empty(self, mock_fetch, mock_es, auth_client):
        mock_fetch.return_value = []
        mock_es.return_value = None
        resp = auth_client.get("/dashboard-builder/api/data-views")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["data_views"] == []

    @mock.patch(f"{_R}._fetch_kibana_data_views")
    def test_space_param_passed(self, mock_fetch, auth_client):
        mock_fetch.return_value = []
        auth_client.get("/dashboard-builder/api/data-views?space=default")
        mock_fetch.assert_called_once_with("default")

    @mock.patch(f"{_R}._fetch_kibana_data_views")
    def test_default_space(self, mock_fetch, auth_client):
        mock_fetch.return_value = []
        auth_client.get("/dashboard-builder/api/data-views")
        mock_fetch.assert_called_once_with("default")


class TestGetFields:
    """GET /builder/api/data-views/<data_view_id>/fields"""

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_returns_fields(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_mock.indices.get_mapping.return_value = {
            "fb_insights": {
                "mappings": {
                    "properties": {
                        "kibana_date": {"type": "date"},
                        "insight_reach": {"type": "long"},
                        "campaign_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    }
                }
            }
        }
        mock_es.return_value = es_mock
        resp = auth_client.get("/dashboard-builder/api/data-views/fb_insights/fields")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        names = [f["name"] for f in data["fields"]]
        assert "kibana_date" in names
        assert "insight_reach" in names
        assert "campaign_name" in names
        assert "campaign_name.keyword" in names

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_es_unavailable(self, mock_es, auth_client):
        mock_es.return_value = None
        resp = auth_client.get("/dashboard-builder/api/data-views/fb_insights/fields")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_mapping_error(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_mock.indices.get_mapping.side_effect = Exception("index not found")
        mock_es.return_value = es_mock
        resp = auth_client.get("/dashboard-builder/api/data-views/missing_index/fields")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_wildcard_pattern_merges(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_mock.indices.get_mapping.return_value = {
            "fb_insights_2025": {
                "mappings": {
                    "properties": {
                        "date": {"type": "date"},
                    }
                }
            },
            "fb_insights_2026": {
                "mappings": {
                    "properties": {
                        "date": {"type": "date"},
                        "extra_field": {"type": "keyword"},
                    }
                }
            },
        }
        mock_es.return_value = es_mock
        resp = auth_client.get("/dashboard-builder/api/data-views/fb_insights_*/fields")
        assert resp.status_code == 200
        data = resp.get_json()
        names = [f["name"] for f in data["fields"]]
        assert "date" in names
        assert "extra_field" in names

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_fields_sorted_by_name(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_mock.indices.get_mapping.return_value = {
            "idx": {
                "mappings": {
                    "properties": {
                        "zebra": {"type": "keyword"},
                        "alpha": {"type": "keyword"},
                        "middle": {"type": "keyword"},
                    }
                }
            }
        }
        mock_es.return_value = es_mock
        resp = auth_client.get("/dashboard-builder/api/data-views/idx/fields")
        data = resp.get_json()
        names = [f["name"] for f in data["fields"]]
        assert names == sorted(names)


# ===========================================================================
# Dashboard CRUD API
# ===========================================================================


class TestListDashboards:
    """GET /builder/api/dashboards"""

    @mock.patch(f"{_R}.get_all_documents")
    def test_returns_echarts_dashboards(self, mock_get_all, auth_client):
        mock_get_all.return_value = SAMPLE_ES_HITS
        resp = auth_client.get("/dashboard-builder/api/dashboards")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["dashboards"]) == 2
        assert data["dashboards"][0]["title"] == "Campaign Overview"

    @mock.patch(f"{_R}.get_all_documents")
    def test_filters_non_echarts(self, mock_get_all, auth_client):
        mock_get_all.return_value = SAMPLE_ES_HITS + [SAMPLE_KIBANA_DASHBOARD]
        resp = auth_client.get("/dashboard-builder/api/dashboards")
        data = resp.get_json()
        assert len(data["dashboards"]) == 2  # kibana dashboard excluded

    @mock.patch(f"{_R}.get_all_documents")
    def test_includes_leaflet_map(self, mock_get_all, auth_client):
        map_hit = {
            "_id": "map-1",
            "_source": {
                "dashboard_id": "map-1",
                "dashboard_type": "leaflet_map",
                "title": "Map",
                "updated_at": "2026-04-01T00:00:00Z",
            },
        }
        mock_get_all.return_value = [map_hit]
        resp = auth_client.get("/dashboard-builder/api/dashboards")
        data = resp.get_json()
        assert len(data["dashboards"]) == 1
        assert data["dashboards"][0]["title"] == "Map"

    @mock.patch(f"{_R}.get_all_documents")
    def test_empty_list(self, mock_get_all, auth_client):
        mock_get_all.return_value = []
        resp = auth_client.get("/dashboard-builder/api/dashboards")
        data = resp.get_json()
        assert data["success"] is True
        assert data["dashboards"] == []

    @mock.patch(f"{_R}.get_all_documents")
    def test_sorted_by_updated_at_desc(self, mock_get_all, auth_client):
        mock_get_all.return_value = SAMPLE_ES_HITS
        resp = auth_client.get("/dashboard-builder/api/dashboards")
        data = resp.get_json()
        dates = [d["updated_at"] for d in data["dashboards"]]
        assert dates == sorted(dates, reverse=True)

    @mock.patch(f"{_R}.get_all_documents")
    def test_es_error_returns_500(self, mock_get_all, auth_client):
        mock_get_all.side_effect = Exception("connection refused")
        resp = auth_client.get("/dashboard-builder/api/dashboards")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False


class TestSaveDashboard:
    """POST /builder/api/dashboards"""

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_create_new_dashboard(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_mock.get.side_effect = Exception("not found")  # no existing doc
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards",
            json={
                "title": "New Dashboard",
                "charts": [{"chart_id": "c1", "chart_type": "bar"}],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "id" in data
        es_mock.index.assert_called_once()

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_create_with_explicit_id(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_mock.get.side_effect = Exception("not found")
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards",
            json={"id": "my-custom-id", "title": "Custom"},
        )
        data = resp.get_json()
        assert data["id"] == "my-custom-id"

    # overwriting an existing dashboard now passes through the
    # per-dashboard mutate-scope gate (folds in the follow-up). This
    # test exercises create_at/menu preservation mechanics, not authz — treat
    # the test admin as a workspace admin so the gate passes. (The mutate gate
    # itself is covered by test_save_scope.py / the tests.)
    # the admin bypass is per-workspace; these fixtures carry no
    # workspace_esid, so treat the admin as admin of the dashboard's workspace.
    @mock.patch(f"{_R}._current_user_admins_dashboard_workspace", new=lambda _source: True)
    @mock.patch(f"{_R}._current_user_is_workspace_admin", return_value=True)
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_update_existing_preserves_created_at(self, mock_es, _mock_ws_admin, auth_client):
        es_mock = mock.MagicMock()
        es_mock.get.return_value = {
            "_source": {
                "created_at": "2025-01-01T00:00:00Z",
                "menu": "My Menu",
            }
        }
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards",
            json={"id": "existing-id", "title": "Updated"},
        )
        assert resp.status_code == 200
        call_kwargs = es_mock.index.call_args
        doc = call_kwargs.kwargs.get("document") or call_kwargs[1].get("document")
        assert doc["created_at"] == "2025-01-01T00:00:00Z"
        assert doc["menu"] == "My Menu"

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_charts_as_dict_converted_to_list(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_mock.get.side_effect = Exception("not found")
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards",
            json={
                "title": "Dict Charts",
                "charts": {"c1": {"chart_type": "bar"}, "c2": {"chart_type": "line"}},
            },
        )
        assert resp.status_code == 200
        call_kwargs = es_mock.index.call_args
        doc = call_kwargs.kwargs.get("document") or call_kwargs[1].get("document")
        assert isinstance(doc["echarts_config"]["charts"], list)
        assert len(doc["echarts_config"]["charts"]) == 2

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_no_body_returns_error(self, mock_es, auth_client):
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.post(
            "/dashboard-builder/api/dashboards",
            data="",
            content_type="application/json",
        )
        # Empty body causes JSON parse error caught by outer except → 500
        assert resp.status_code in (400, 500)
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_es_unavailable(self, mock_es, auth_client):
        mock_es.return_value = None
        resp = auth_client.post(
            "/dashboard-builder/api/dashboards",
            json={"title": "Test"},
        )
        assert resp.status_code == 503

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_es_index_error(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_mock.get.side_effect = Exception("not found")
        es_mock.index.side_effect = Exception("index write failed")
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards",
            json={"title": "Test"},
        )
        assert resp.status_code == 500

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_default_title(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_mock.get.side_effect = Exception("not found")
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards",
            json={"charts": []},
        )
        assert resp.status_code == 200
        call_kwargs = es_mock.index.call_args
        doc = call_kwargs.kwargs.get("document") or call_kwargs[1].get("document")
        assert doc["title"] == "Untitled Dashboard"


class TestLoadDashboard:
    """GET /builder/api/dashboards/<id>"""

    @mock.patch(f"{_R}.get_document_by_id")
    def test_load_echarts_dashboard(self, mock_get, auth_client):
        mock_get.return_value = SAMPLE_ECHARTS_SOURCE
        resp = auth_client.get("/dashboard-builder/api/dashboards/dash-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == "dash-1"
        assert data["title"] == "Campaign Overview"
        assert data["dashboard_type"] == "echarts"
        assert "charts" in data

    @mock.patch(f"{_R}.get_document_by_id")
    def test_load_leaflet_map(self, mock_get, auth_client):
        mock_get.return_value = SAMPLE_LEAFLET_SOURCE
        resp = auth_client.get("/dashboard-builder/api/dashboards/map-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["dashboard_type"] == "leaflet_map"
        assert data["map_config"]["zoom"] == 3

    @mock.patch(f"{_R}.get_document_by_id")
    def test_not_found(self, mock_get, auth_client):
        mock_get.return_value = None
        resp = auth_client.get("/dashboard-builder/api/dashboards/nonexistent")
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch(f"{_R}.get_document_by_id")
    def test_non_echarts_returns_400(self, mock_get, auth_client):
        mock_get.return_value = {
            "dashboard_type": "kibana",
            "title": "Kibana Dashboard",
        }
        resp = auth_client.get("/dashboard-builder/api/dashboards/kib-1")
        assert resp.status_code == 400

    @mock.patch(f"{_R}.get_document_by_id")
    def test_es_error_returns_500(self, mock_get, auth_client):
        mock_get.side_effect = Exception("connection error")
        resp = auth_client.get("/dashboard-builder/api/dashboards/dash-1")
        assert resp.status_code == 500


class TestDeleteDashboard:
    """DELETE /builder/api/dashboards/<id>"""

    # the delete-mechanics tests below exercise the 200/400/500/leaflet
    # paths, not the per-dashboard mutate-scope gate (owned by
    # test_delete_scope.py). The SAMPLE_* fixtures are show_all, which
    # the new gate denies for a non-workspace-admin; treat the test admin as a
    # workspace admin so the gate passes and these tests stay focused.
    # the admin bypass is per-workspace; these fixtures carry no
    # workspace_esid, so treat the admin as admin of the dashboard's workspace.
    @mock.patch(f"{_R}._current_user_admins_dashboard_workspace", new=lambda _source: True)
    @mock.patch(f"{_R}._current_user_is_workspace_admin", return_value=True)
    @mock.patch(f"{_R}.delete_document_by_id")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_delete_success(self, mock_get, mock_delete, _mock_ws_admin, auth_client):
        mock_get.return_value = SAMPLE_ECHARTS_SOURCE
        mock_delete.return_value = True
        resp = auth_client.delete("/dashboard-builder/api/dashboards/dash-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "deleted"

    @mock.patch(f"{_R}.get_document_by_id")
    def test_delete_not_found(self, mock_get, auth_client):
        mock_get.return_value = None
        resp = auth_client.delete("/dashboard-builder/api/dashboards/nonexistent")
        assert resp.status_code == 404

    # the admin bypass is per-workspace; these fixtures carry no
    # workspace_esid, so treat the admin as admin of the dashboard's workspace.
    @mock.patch(f"{_R}._current_user_admins_dashboard_workspace", new=lambda _source: True)
    @mock.patch(f"{_R}._current_user_is_workspace_admin", return_value=True)
    @mock.patch(f"{_R}.get_document_by_id")
    def test_delete_non_echarts_returns_400(self, mock_get, _mock_ws_admin, auth_client):
        mock_get.return_value = {
            "dashboard_type": "kibana",
            "title": "Kibana Dashboard",
        }
        resp = auth_client.delete("/dashboard-builder/api/dashboards/kib-1")
        assert resp.status_code == 400

    # the admin bypass is per-workspace; these fixtures carry no
    # workspace_esid, so treat the admin as admin of the dashboard's workspace.
    @mock.patch(f"{_R}._current_user_admins_dashboard_workspace", new=lambda _source: True)
    @mock.patch(f"{_R}._current_user_is_workspace_admin", return_value=True)
    @mock.patch(f"{_R}.delete_document_by_id")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_delete_failure_returns_500(self, mock_get, mock_delete, _mock_ws_admin, auth_client):
        mock_get.return_value = SAMPLE_ECHARTS_SOURCE
        mock_delete.return_value = False
        resp = auth_client.delete("/dashboard-builder/api/dashboards/dash-1")
        assert resp.status_code == 500

    @mock.patch(f"{_R}.get_document_by_id")
    def test_delete_es_error(self, mock_get, auth_client):
        mock_get.side_effect = Exception("connection error")
        resp = auth_client.delete("/dashboard-builder/api/dashboards/dash-1")
        assert resp.status_code == 500

    # the admin bypass is per-workspace; these fixtures carry no
    # workspace_esid, so treat the admin as admin of the dashboard's workspace.
    @mock.patch(f"{_R}._current_user_admins_dashboard_workspace", new=lambda _source: True)
    @mock.patch(f"{_R}._current_user_is_workspace_admin", return_value=True)
    @mock.patch(f"{_R}.delete_document_by_id")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_delete_leaflet_map_succeeds(self, mock_get, mock_delete, _mock_ws_admin, auth_client):
        mock_get.return_value = SAMPLE_LEAFLET_SOURCE
        mock_delete.return_value = True
        resp = auth_client.delete("/dashboard-builder/api/dashboards/map-1")
        assert resp.status_code == 200


# ===========================================================================
# Query API
# ===========================================================================


class TestRunQuery:
    """POST /builder/api/query"""

    @mock.patch(f"{_R}.transform")
    @mock.patch(f"{_DR}.build_query")
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_basic_query(self, mock_es, mock_build, mock_transform, auth_client):
        es_mock = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {"aggregations": {"x_field": {"buckets": []}}}
        es_mock.search.return_value = es_resp
        es_mock.indices.get_mapping.return_value = {}
        mock_es.return_value = es_mock
        mock_build.return_value = {"size": 0, "aggs": {}}
        mock_transform.return_value = {
            "xAxis": {"data": ["Jan", "Feb"]},
            "series": [{"data": [10, 20]}],
        }

        resp = auth_client.post(
            "/dashboard-builder/api/query",
            json={
                "data_view": "fb_insights",
                "chart_type": "bar",
                "x_field": "kibana_date",
                "y_field": "insight_reach",
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "xAxis" in data
        mock_build.assert_called_once()
        mock_transform.assert_called_once()

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_no_config_returns_error(self, mock_es, auth_client):
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.post(
            "/dashboard-builder/api/query",
            data="",
            content_type="application/json",
        )
        # Empty body causes JSON parse error caught by outer except → 500
        assert resp.status_code in (400, 500)
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_es_unavailable(self, mock_es, auth_client):
        mock_es.return_value = None
        resp = auth_client.post(
            "/dashboard-builder/api/query",
            json={"data_view": "fb_insights"},
        )
        assert resp.status_code == 503

    @mock.patch(f"{_DR}.build_query")
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_query_exception_returns_500(self, mock_es, mock_build, auth_client):
        es_mock = mock.MagicMock()
        es_mock.indices.get_mapping.return_value = {}
        mock_es.return_value = es_mock
        mock_build.side_effect = Exception("bad query")

        resp = auth_client.post(
            "/dashboard-builder/api/query",
            json={"data_view": "fb_insights", "chart_type": "bar"},
        )
        assert resp.status_code == 500

    @mock.patch(f"{_R}.transform")
    @mock.patch(f"{_DR}.build_query")
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_enriches_y_field_types(self, mock_es, mock_build, mock_transform, auth_client):
        es_mock = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {"aggregations": {}}
        es_mock.search.return_value = es_resp
        es_mock.indices.get_mapping.return_value = {
            "fb_insights": {
                "mappings": {
                    "properties": {
                        "insight_reach": {"type": "long"},
                    }
                }
            }
        }
        mock_es.return_value = es_mock
        mock_build.return_value = {"size": 0}
        mock_transform.return_value = {}

        auth_client.post(
            "/dashboard-builder/api/query",
            json={
                "data_view": "fb_insights",
                "chart_type": "bar",
                "y_field": "insight_reach",
            },
        )
        # build_query should receive config with y_field_types populated
        config_arg = mock_build.call_args[0][0]
        assert "y_field_types" in config_arg

    @mock.patch(f"{_R}.transform")
    @mock.patch(f"{_DR}.build_query")
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_es_response_without_body_attr(self, mock_es, mock_build, mock_transform, auth_client):
        """ES client may return a plain dict (older client versions)."""
        es_mock = mock.MagicMock()
        es_resp = {"aggregations": {}}
        # Remove .body attribute by using a plain dict
        es_mock.search.return_value = es_resp
        es_mock.indices.get_mapping.return_value = {}
        mock_es.return_value = es_mock
        mock_build.return_value = {"size": 0}
        mock_transform.return_value = {"series": []}

        resp = auth_client.post(
            "/dashboard-builder/api/query",
            json={"data_view": "fb_insights", "chart_type": "bar"},
        )
        assert resp.status_code == 200


class TestTrendLineData:
    """POST /builder/api/trend-line-data"""

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_basic_trend(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {
            "aggregations": {
                "over_time": {
                    "buckets": [
                        {"key_as_string": "2026-01", "doc_count": 100},
                        {"key_as_string": "2026-02", "doc_count": 150},
                    ]
                }
            }
        }
        es_mock.search.return_value = es_resp
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/trend-line-data",
            json={
                "sources": [{"label": "Reach", "data_view": "fb_insights", "color": "#ff0000"}],
                "date_interval": "month",
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "x_data" in data
        assert "series" in data
        assert len(data["series"]) == 1
        assert data["series"][0]["name"] == "Reach"
        assert data["x_data"] == ["2026-01", "2026-02"]

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_no_body_returns_error(self, mock_es, auth_client):
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.post(
            "/dashboard-builder/api/trend-line-data",
            data="",
            content_type="application/json",
        )
        # Empty body causes JSON parse error caught by outer except → 500
        assert resp.status_code in (400, 500)

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_es_unavailable(self, mock_es, auth_client):
        mock_es.return_value = None
        resp = auth_client.post(
            "/dashboard-builder/api/trend-line-data",
            json={"sources": []},
        )
        assert resp.status_code == 503

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_source_with_field_uses_sum(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {
            "aggregations": {
                "over_time": {
                    "buckets": [
                        {
                            "key_as_string": "2026-01",
                            "metric": {"value": 500},
                        }
                    ]
                }
            }
        }
        es_mock.search.return_value = es_resp
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/trend-line-data",
            json={
                "sources": [{"label": "Reach", "data_view": "fb_insights", "field": "insight_reach"}],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["series"][0]["data"] == [500]

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_empty_data_view_skipped(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/trend-line-data",
            json={
                "sources": [{"label": "Empty", "data_view": "", "color": "#000"}],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["series"][0]["data"] == []

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_multiple_sources(self, mock_es, auth_client):
        es_mock = mock.MagicMock()

        def search_side_effect(**kwargs):
            resp = mock.MagicMock()
            resp.body = {
                "aggregations": {
                    "over_time": {
                        "buckets": [
                            {"key_as_string": "2026-01", "doc_count": 10},
                        ]
                    }
                }
            }
            return resp

        es_mock.search.side_effect = search_side_effect
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/trend-line-data",
            json={
                "sources": [
                    {"label": "Source A", "data_view": "idx_a"},
                    {"label": "Source B", "data_view": "idx_b"},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["series"]) == 2

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_source_failure_graceful(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_mock.search.side_effect = Exception("search failed")
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/trend-line-data",
            json={
                "sources": [{"label": "Bad Source", "data_view": "missing_index"}],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["series"][0]["data"] == []

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_default_visible_and_color(self, mock_es, auth_client):
        es_mock = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {"aggregations": {"over_time": {"buckets": []}}}
        es_mock.search.return_value = es_resp
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/trend-line-data",
            json={
                "sources": [
                    {
                        "label": "Test",
                        "data_view": "idx",
                        "color": "#abc123",
                        "default_visible": False,
                    }
                ],
            },
        )
        data = resp.get_json()
        assert data["series"][0]["color"] == "#abc123"
        assert data["series"][0]["default_visible"] is False


# ===========================================================================
# Helper functions (pure unit tests — no Flask client needed)
# ===========================================================================


class TestSimplifyEsType:
    """_simplify_es_type maps ES types to simplified categories."""

    def test_date(self):
        assert _simplify_es_type("date") == "date"

    def test_text(self):
        assert _simplify_es_type("text") == "text"

    def test_keyword(self):
        assert _simplify_es_type("keyword") == "keyword"

    def test_constant_keyword(self):
        assert _simplify_es_type("constant_keyword") == "keyword"

    def test_wildcard(self):
        assert _simplify_es_type("wildcard") == "keyword"

    @pytest.mark.parametrize(
        "es_type",
        ["long", "integer", "short", "byte", "float", "double", "half_float", "scaled_float", "unsigned_long"],
    )
    def test_numeric_types(self, es_type):
        assert _simplify_es_type(es_type) == "numeric"

    def test_unknown_defaults_to_keyword(self):
        assert _simplify_es_type("geo_point") == "keyword"

    def test_empty_string_defaults_to_keyword(self):
        assert _simplify_es_type("") == "keyword"


class TestFlattenMapping:
    """_flatten_mapping flattens nested ES mapping properties."""

    def test_flat_properties(self):
        props = {
            "name": {"type": "keyword"},
            "count": {"type": "long"},
        }
        fields = _flatten_mapping(props)
        assert len(fields) == 2
        names = {f["name"] for f in fields}
        assert names == {"name", "count"}

    def test_nested_properties(self):
        props = {
            "user": {
                "properties": {
                    "name": {"type": "keyword"},
                    "age": {"type": "integer"},
                }
            }
        }
        fields = _flatten_mapping(props)
        names = {f["name"] for f in fields}
        assert "user.name" in names
        assert "user.age" in names

    def test_multi_fields(self):
        props = {
            "title": {
                "type": "text",
                "fields": {
                    "keyword": {"type": "keyword"},
                },
            }
        }
        fields = _flatten_mapping(props)
        names = {f["name"] for f in fields}
        assert "title" in names
        assert "title.keyword" in names

    def test_prefix(self):
        props = {"field": {"type": "keyword"}}
        fields = _flatten_mapping(props, prefix="parent")
        assert fields[0]["name"] == "parent.field"

    def test_empty_mapping(self):
        assert _flatten_mapping({}) == []

    def test_deeply_nested(self):
        props = {"a": {"properties": {"b": {"properties": {"c": {"type": "keyword"}}}}}}
        fields = _flatten_mapping(props)
        assert fields[0]["name"] == "a.b.c"
        assert fields[0]["type"] == "keyword"

    def test_type_simplification(self):
        props = {
            "date_field": {"type": "date"},
            "num_field": {"type": "float"},
        }
        fields = _flatten_mapping(props)
        type_map = {f["name"]: f["type"] for f in fields}
        assert type_map["date_field"] == "date"
        assert type_map["num_field"] == "numeric"

    def test_no_type_key_only_properties(self):
        """A mapping node with only 'properties' and no 'type' should still recurse."""
        props = {"container": {"properties": {"inner": {"type": "keyword"}}}}
        fields = _flatten_mapping(props)
        assert len(fields) == 1
        assert fields[0]["name"] == "container.inner"


class TestResolveDateMath:
    """_resolve_date_math resolves ES date math expressions."""

    def test_now(self):
        result = _resolve_date_math("now")
        assert result is not None
        # Should be a YYYY-MM-DD string
        assert len(result) == 10
        assert result[4] == "-"

    def test_now_minus_days(self):
        result = _resolve_date_math("now-30d/d")
        assert result is not None
        assert len(result) == 10

    def test_now_minus_months(self):
        result = _resolve_date_math("now-6m/m")
        # 'm' lowercase maps to months (30-day approximation)
        assert result is not None

    def test_now_minus_years(self):
        result = _resolve_date_math("now-1y/y")
        assert result is not None

    def test_plain_date_passthrough(self):
        assert _resolve_date_math("2026-01-15") == "2026-01-15"

    def test_empty_string(self):
        assert _resolve_date_math("") == ""

    def test_none(self):
        assert _resolve_date_math(None) is None

    def test_unix_ts_mode(self):
        result = _resolve_date_math("now-30d/d", unix_ts=True)
        assert result.isdigit()

    def test_unix_ts_plain_date(self):
        result = _resolve_date_math("2026-01-01", unix_ts=True)
        assert result.isdigit()

    def test_upper_bound_now(self):
        normal = _resolve_date_math("now", upper_bound=False)
        upper = _resolve_date_math("now", upper_bound=True)
        # upper_bound adds one day so lte includes current day
        assert upper >= normal

    def test_unrecognized_expression_passthrough(self):
        assert _resolve_date_math("some-random-text") == "some-random-text"


# ===========================================================================
# _fetch_kibana_data_views (private helper, tested via mock)
# ===========================================================================


class TestFetchKibanaDataViews:
    """_fetch_kibana_data_views calls the Kibana API."""

    @mock.patch(f"{_R}.http_requests.get")
    def test_successful_fetch(self, mock_get, auth_client):
        from asset_management.web.blueprints.dashboard_builder.routes import _fetch_kibana_data_views

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data_view": [
                {"title": "fb_insights", "name": "Facebook Insights"},
                {"title": "google_ads", "name": "Google Ads"},
            ]
        }
        mock_get.return_value = mock_resp

        views = _fetch_kibana_data_views("default")
        assert len(views) == 2
        assert views[0]["id"] in ("fb_insights", "google_ads")

    @mock.patch(f"{_R}.http_requests.get")
    def test_non_200_returns_empty(self, mock_get):
        from asset_management.web.blueprints.dashboard_builder.routes import _fetch_kibana_data_views

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp

        assert _fetch_kibana_data_views() == []

    @mock.patch(f"{_R}.http_requests.get")
    def test_exception_returns_empty(self, mock_get):
        from asset_management.web.blueprints.dashboard_builder.routes import _fetch_kibana_data_views

        mock_get.side_effect = Exception("connection refused")
        assert _fetch_kibana_data_views() == []

    @mock.patch.dict("os.environ", {"ES01_PASSWORD": ""})
    def test_no_password_returns_empty(self):
        from asset_management.web.blueprints.dashboard_builder.routes import _fetch_kibana_data_views

        assert _fetch_kibana_data_views() == []

    @mock.patch(f"{_R}.http_requests.get")
    def test_default_space_url(self, mock_get):
        from asset_management.web.blueprints.dashboard_builder.routes import _fetch_kibana_data_views

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data_view": []}
        mock_get.return_value = mock_resp

        _fetch_kibana_data_views("default")
        call_url = mock_get.call_args[0][0]
        assert "/s/" not in call_url

    @mock.patch(f"{_R}.http_requests.get")
    def test_sorted_by_label(self, mock_get):
        from asset_management.web.blueprints.dashboard_builder.routes import _fetch_kibana_data_views

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data_view": [
                {"title": "z_index", "name": "Zebra"},
                {"title": "a_index", "name": "Alpha"},
            ]
        }
        mock_get.return_value = mock_resp

        views = _fetch_kibana_data_views()
        assert views[0]["label"] == "Alpha"
        assert views[1]["label"] == "Zebra"

    @mock.patch(f"{_R}.http_requests.get")
    def test_label_falls_back_to_title(self, mock_get):
        from asset_management.web.blueprints.dashboard_builder.routes import _fetch_kibana_data_views

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data_view": [
                {"title": "my_index"},  # no 'name' key
            ]
        }
        mock_get.return_value = mock_resp

        views = _fetch_kibana_data_views()
        assert views[0]["label"] == "my_index"
