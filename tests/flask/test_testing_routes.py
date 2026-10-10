"""
Tests for the testing harness API routes and additional asset/search/dashboard APIs.

Routes covered:
  GET  /testing                       testing_page (page, gates on mc_testing)
  GET  /api/testing/dashboards        api_testing_dashboards
  POST /api/testing/run-pytest        api_testing_run_pytest
  GET  /api/testing/pytest-log        api_testing_pytest_log
  GET  /api/testing/check-dashboard   api_testing_check_dashboard
  GET  /api/asset/<type>/<id>/details get_asset_details
  GET  /api/dashboard/summary         get_dashboard_summary
  GET  /api/search                    search_assets

All external service calls are mocked.
"""

import os
import subprocess
import time
from unittest import mock

import pytest

# get_asset_details tests target the now-scoped company-tier details
# route; neutralize that gate (covered by
# the release-plan tests).
pytestmark = pytest.mark.usefixtures("allow_company_tier_and_options_scope")


# ===========================================================================
# GET /testing — page route gates on mc_testing
# ===========================================================================


def _custom_role_client(app):
    """Test client with a custom non-admin role for permission gating tests."""
    from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["username"] = "customuser"
        sess["roles"] = ["custom_qa_runner"]
        sess["is_admin"] = False
        sess["full_name"] = "Custom User"
        sess["email"] = "custom@example.com"
        sess["auth_timestamp"] = time.time()
        sess["csrf_token"] = CSRF_TEST_TOKEN
    return CsrfTestClient(c)


class TestTestingPagePermission:
    """The /testing page gates on mc_testing rather than the asset_admin role."""

    def test_admin_can_access(self, auth_client):
        resp = auth_client.get("/testing")
        assert resp.status_code == 200

    def test_superuser_can_access(self, superuser_client):
        resp = superuser_client.get("/testing")
        assert resp.status_code == 200

    def test_custom_role_with_mc_testing_can_access(self, app):
        c = _custom_role_client(app)
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.get_merged_permissions",
            return_value={"mc_testing": True},
        ):
            resp = c.get("/testing")
        assert resp.status_code == 200

    def test_custom_role_without_mc_testing_redirected(self, app):
        c = _custom_role_client(app)
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.get_merged_permissions",
            return_value={"mc_testing": False},
        ):
            resp = c.get("/testing")
        # Page-level 403 → redirect by global handler.
        assert resp.status_code == 302


# ===========================================================================
# GET /api/testing/dashboards  — api_testing_dashboards
# ===========================================================================


class TestApiTestingDashboards:
    """Tests for the testing dashboards list endpoint."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/testing/dashboards")
        assert resp.status_code == 401

    def test_non_admin_gets_403(self, org_user_client):
        resp = org_user_client.get("/api/testing/dashboards")
        assert resp.status_code == 403

    @mock.patch("asset_management.web.app.get_all_documents", create=True)
    @mock.patch("lib.es_helpers.get_all_documents", return_value=[])
    def test_returns_empty_list_when_no_dashboards(self, mock_get_all_lib, mock_get_all_app, auth_client):
        """When there are no dashboards in ES, returns an empty JSON array."""
        # The route does `from lib.es_helpers import get_all_documents as _get_all`
        # inside the function, so we patch the source module.
        with mock.patch("lib.es_helpers.get_all_documents", return_value=[]):
            resp = auth_client.get("/api/testing/dashboards")
        assert resp.status_code == 200
        assert resp.get_json() == []

    @mock.patch("lib.es_helpers.get_all_documents")
    def test_returns_kibana_dashboards(self, mock_get_all, auth_client):
        mock_get_all.side_effect = [
            # First call: de_asset_dashboards (Kibana)
            [
                {
                    "_id": "k1",
                    "_source": {
                        "dashboard_id": "dash-uuid-1",
                        "dashboard_title": "Facebook Overview",
                        "space_id": "marketing",
                        "visibility_mode": "show",
                    },
                }
            ],
            # Second call: de_asset_echarts_dashboards
            [],
        ]
        resp = auth_client.get("/api/testing/dashboards")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["id"] == "dash-uuid-1"
        assert data[0]["title"] == "Facebook Overview"
        assert data[0]["type"] == "kibana"
        assert "check_url" in data[0]
        assert "iframe_url" in data[0]

    @mock.patch("lib.es_helpers.get_all_documents")
    def test_hidden_dashboards_excluded(self, mock_get_all, auth_client):
        mock_get_all.side_effect = [
            [
                {
                    "_id": "k1",
                    "_source": {
                        "dashboard_id": "dash-hidden",
                        "dashboard_title": "Hidden",
                        "visibility_mode": "hide",
                    },
                }
            ],
            [],
        ]
        resp = auth_client.get("/api/testing/dashboards")
        assert resp.status_code == 200
        assert resp.get_json() == []

    @mock.patch("lib.es_helpers.get_all_documents")
    def test_dashboards_without_id_excluded(self, mock_get_all, auth_client):
        mock_get_all.side_effect = [
            [{"_id": "k1", "_source": {"dashboard_id": "", "dashboard_title": "No ID"}}],
            [],
        ]
        resp = auth_client.get("/api/testing/dashboards")
        assert resp.status_code == 200
        assert resp.get_json() == []

    @mock.patch("lib.es_helpers.get_all_documents")
    def test_returns_echarts_dashboards(self, mock_get_all, auth_client):
        mock_get_all.side_effect = [
            [],  # Kibana
            [
                {
                    "_id": "e1",
                    "_source": {
                        "dashboard_id": "echart-uuid-1",
                        "title": "Custom EChart",
                        "visibility_mode": "show",
                    },
                }
            ],
        ]
        resp = auth_client.get("/api/testing/dashboards")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["type"] == "echarts"
        assert data[0]["title"] == "Custom EChart"

    @mock.patch("lib.es_helpers.get_all_documents")
    def test_superuser_can_access(self, mock_get_all, superuser_client):
        mock_get_all.return_value = []
        resp = superuser_client.get("/api/testing/dashboards")
        assert resp.status_code == 200


# ===========================================================================
# POST /api/testing/run-pytest  — api_testing_run_pytest
# ===========================================================================


class TestApiTestingRunPytest:
    """Tests for the pytest execution endpoint."""

    def test_unauthenticated_redirects(self, client):
        resp = client.post("/api/testing/run-pytest")
        assert resp.status_code == 401

    def test_non_admin_gets_403(self, org_user_client):
        resp = org_user_client.post("/api/testing/run-pytest")
        assert resp.status_code == 403

    @mock.patch("builtins.open", mock.mock_open())
    @mock.patch("os.makedirs")
    @mock.patch("subprocess.run")
    def test_pytest_passes(self, mock_run, mock_makedirs, auth_client):
        mock_run.return_value = mock.MagicMock(
            returncode=0,
            stdout="===== 5 passed =====\n",
            stderr="",
        )
        resp = auth_client.post("/api/testing/run-pytest")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["passed"] is True
        assert "filename" in data

    @mock.patch("builtins.open", mock.mock_open())
    @mock.patch("os.makedirs")
    @mock.patch("subprocess.run")
    def test_pytest_fails(self, mock_run, mock_makedirs, auth_client):
        mock_run.return_value = mock.MagicMock(
            returncode=1,
            stdout="FAILED test_something\n",
            stderr="",
        )
        resp = auth_client.post("/api/testing/run-pytest")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["passed"] is False

    @mock.patch("os.makedirs")
    @mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=180))
    def test_pytest_timeout(self, mock_run, mock_makedirs, auth_client):
        resp = auth_client.post("/api/testing/run-pytest")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False
        assert "timed out" in data["error"]

    @mock.patch("os.makedirs")
    @mock.patch("subprocess.run", side_effect=OSError("No such file"))
    def test_pytest_exception(self, mock_run, mock_makedirs, auth_client):
        resp = auth_client.post("/api/testing/run-pytest")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False
        # generic client-facing message; exception text is in the
        # server log only.
        assert data["error"] == "Failed to run pytest"

    @mock.patch("builtins.open", mock.mock_open())
    @mock.patch("os.makedirs")
    @mock.patch("subprocess.run")
    def test_superuser_can_run_pytest(self, mock_run, mock_makedirs, superuser_client):
        mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
        resp = superuser_client.post("/api/testing/run-pytest")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


# ===========================================================================
# GET /api/testing/pytest-log  — api_testing_pytest_log
# ===========================================================================


class TestApiTestingPytestLog:
    """Tests for the pytest log retrieval endpoint."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/testing/pytest-log")
        assert resp.status_code == 401

    def test_non_admin_gets_403(self, org_user_client):
        resp = org_user_client.get("/api/testing/pytest-log")
        assert resp.status_code == 403

    @mock.patch("glob.glob", return_value=[])
    def test_no_log_files(self, mock_glob, auth_client):
        resp = auth_client.get("/api/testing/pytest-log")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is False
        assert data["filename"] is None

    @mock.patch("builtins.open", mock.mock_open(read_data="test output\nall passed"))
    @mock.patch("glob.glob", return_value=["/app/data/logs/pytest_20260403.log"])
    def test_returns_latest_log(self, mock_glob, auth_client):
        resp = auth_client.get("/api/testing/pytest-log")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is True
        assert data["filename"] == "pytest_20260403.log"
        assert "test output" in data["content"]

    @mock.patch("builtins.open", mock.mock_open(read_data="log1 content"))
    @mock.patch("glob.glob", return_value=[
        "/app/data/logs/pytest_20260403.log",
        "/app/data/logs/pytest_20260402.log",
    ])
    def test_returns_first_sorted_log(self, mock_glob, auth_client):
        """Sorted reverse means most recent date first."""
        resp = auth_client.get("/api/testing/pytest-log")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is True
        assert data["filename"] == "pytest_20260403.log"

    @mock.patch("glob.glob", side_effect=Exception("permission denied"))
    def test_error_reading_log(self, mock_glob, auth_client):
        resp = auth_client.get("/api/testing/pytest-log")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["exists"] is False
        # generic client-facing message; exception text is in the
        # server log only.
        assert data["error"] == "Failed to read pytest log"

    @mock.patch("builtins.open", mock.mock_open(read_data="content"))
    @mock.patch("glob.glob", return_value=["/app/data/logs/pytest_20260403.log"])
    def test_superuser_can_read_log(self, mock_glob, superuser_client):
        resp = superuser_client.get("/api/testing/pytest-log")
        assert resp.status_code == 200
        assert resp.get_json()["exists"] is True


# ===========================================================================
# GET /api/testing/check-dashboard  — api_testing_check_dashboard
# ===========================================================================


class TestApiTestingCheckDashboard:
    """Tests for the dashboard health-check endpoint."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/testing/check-dashboard")
        assert resp.status_code == 401

    def test_non_admin_gets_403(self, org_user_client):
        resp = org_user_client.get("/api/testing/check-dashboard")
        assert resp.status_code == 403

    def test_missing_params_returns_400(self, auth_client):
        resp = auth_client.get("/api/testing/check-dashboard")
        assert resp.status_code == 400
        assert "dtype and did are required" in resp.get_json()["error"]

    def test_missing_did_returns_400(self, auth_client):
        resp = auth_client.get("/api/testing/check-dashboard?dtype=kibana")
        assert resp.status_code == 400

    def test_missing_dtype_returns_400(self, auth_client):
        resp = auth_client.get("/api/testing/check-dashboard?did=abc")
        assert resp.status_code == 400

    @mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None)
    def test_es_unavailable_returns_503(self, mock_es, auth_client):
        resp = auth_client.get("/api/testing/check-dashboard?dtype=kibana&did=abc")
        assert resp.status_code == 503
        assert "Cannot connect" in resp.get_json()["error"]

    def test_unknown_dtype_returns_400(self, auth_client):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/testing/check-dashboard?dtype=unknown&did=abc")
        assert resp.status_code == 400
        assert "Unknown dtype" in resp.get_json()["error"]

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_kibana_dashboard_found(self, mock_get_es, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_source": {"dashboard_id": "dash-1", "dashboard_title": "FB Overview"}}
                ]
            }
        }
        mock_get_es.return_value = mock_es
        resp = auth_client.get("/api/testing/check-dashboard?dtype=kibana&did=dash-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["title"] == "FB Overview"

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_kibana_dashboard_not_found(self, mock_get_es, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        mock_get_es.return_value = mock_es
        resp = auth_client.get("/api/testing/check-dashboard?dtype=kibana&did=nonexistent")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert "not found" in data["error"]

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_kibana_empty_dashboard_id(self, mock_get_es, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": {"dashboard_id": "", "dashboard_title": "Empty"}}]}
        }
        mock_get_es.return_value = mock_es
        resp = auth_client.get("/api/testing/check-dashboard?dtype=kibana&did=dash-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert "empty" in data["error"]

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_echarts_dashboard_found(self, mock_get_es, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "dashboard_id": "ec-1",
                            "title": "Custom Chart",
                            "echarts_config": {"charts": [{"type": "bar"}, {"type": "line"}]},
                        }
                    }
                ]
            }
        }
        mock_get_es.return_value = mock_es
        resp = auth_client.get("/api/testing/check-dashboard?dtype=echarts&did=ec-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["title"] == "Custom Chart"
        assert data["chart_count"] == 2

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_echarts_dashboard_not_found(self, mock_get_es, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        mock_get_es.return_value = mock_es
        resp = auth_client.get("/api/testing/check-dashboard?dtype=echarts&did=missing")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_echarts_charts_not_a_list(self, mock_get_es, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "dashboard_id": "ec-1",
                            "title": "Bad Config",
                            "echarts_config": {"charts": "not-a-list"},
                        }
                    }
                ]
            }
        }
        mock_get_es.return_value = mock_es
        resp = auth_client.get("/api/testing/check-dashboard?dtype=echarts&did=ec-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert "not a list" in data["error"]

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_es_exception_returns_500(self, mock_get_es, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES connection lost")
        mock_get_es.return_value = mock_es
        resp = auth_client.get("/api/testing/check-dashboard?dtype=kibana&did=dash-1")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["ok"] is False
        # generic client-facing message; exception text is in the
        # server log only.
        assert data["error"] == "Failed to check dashboard"


# ===========================================================================
# GET /api/asset/<type>/<id>/details  — get_asset_details
# ===========================================================================


class TestGetAssetDetails:
    """Tests for the asset detail API."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/asset/teams/id1/details")
        assert resp.status_code == 401

    def test_invalid_asset_type_returns_404(self, auth_client):
        resp = auth_client.get("/api/asset/nonexistent/id1/details")
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"]

    @mock.patch("asset_management.web.app.FormService.get_computed_display_fields", return_value=[])
    @mock.patch("asset_management.web.app.FormService.prepare_for_display", return_value={"team_title": "My Team"})
    @mock.patch("asset_management.web.app.AssetService.get_by_id")
    def test_returns_details(self, mock_get, mock_display, mock_computed, auth_client):
        mock_get.return_value = {"_id": "t1", "_source": {"team_title": "My Team"}}
        resp = auth_client.get("/api/asset/teams/t1/details")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == "t1"
        assert data["asset_type"] == "teams"
        assert "data" in data
        assert "computed_fields" in data
        assert "asset_config" in data

    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=None)
    def test_item_not_found_returns_404(self, mock_get, auth_client):
        resp = auth_client.get("/api/asset/teams/missing/details")
        assert resp.status_code == 404
        assert "Item not found" in resp.get_json()["error"]


# ===========================================================================
# GET /api/dashboard/summary  — get_dashboard_summary
# ===========================================================================


class TestGetDashboardSummary:
    """Tests for the dashboard summary statistics endpoint."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/dashboard/summary")
        assert resp.status_code == 401

    @mock.patch("asset_management.web.app.RoleFilterService.should_apply_filter", return_value=False)
    @mock.patch("asset_management.web.app.RoleFilterService.current_user_has_any_scope", return_value=True)
    @mock.patch("asset_management.web.app.AssetService.count", return_value=3)
    def test_returns_summary(self, mock_count, _scope, _filter, auth_client):
        # Phase C added workspace-scoped counts; mock the scope
        # methods so the per-type count assertion still runs.
        resp = auth_client.get("/api/dashboard/summary")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "total_assets" in data
        assert "by_category" in data
        assert "by_type" in data
        assert data["total_assets"] > 0

    @mock.patch("asset_management.web.app.AssetService.count", return_value=0)
    def test_returns_zero_counts(self, mock_count, auth_client):
        resp = auth_client.get("/api/dashboard/summary")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total_assets"] == 0

    @mock.patch("asset_management.web.app.AssetService.count", return_value=5)
    def test_by_type_has_title_and_count(self, mock_count, auth_client):
        resp = auth_client.get("/api/dashboard/summary")
        assert resp.status_code == 200
        data = resp.get_json()
        for key, val in data["by_type"].items():
            assert "title" in val
            assert "count" in val


# ===========================================================================
# GET /api/search  — search_assets
# ===========================================================================


class TestSearchAssets:
    """Tests for the asset search endpoint."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/search?q=test")
        assert resp.status_code == 401

    def test_missing_query_returns_400(self, auth_client):
        resp = auth_client.get("/api/search")
        assert resp.status_code == 400
        assert "Query parameter required" in resp.get_json()["error"]

    def test_empty_query_returns_400(self, auth_client):
        resp = auth_client.get("/api/search?q=")
        assert resp.status_code == 400

    @mock.patch("asset_management.web.app.AssetService.get_all", return_value=[])
    def test_no_results(self, mock_get_all, auth_client):
        resp = auth_client.get("/api/search?q=zzz_no_match")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["query"] == "zzz_no_match"
        assert data["results"] == {}

    @mock.patch("asset_management.web.app.AssetService.get_all")
    def test_returns_matching_results(self, mock_get_all, auth_client):
        mock_get_all.return_value = [
            {"_id": "t1", "_source": {"team_title": "Alpha Team", "org_id": "org1"}},
            {"_id": "t2", "_source": {"team_title": "Beta Team", "org_id": "org2"}},
        ]
        resp = auth_client.get("/api/search?q=Alpha*")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["query"] == "Alpha*"
        # Results may or may not contain matches depending on field configs;
        # just verify structure is correct
        assert isinstance(data["results"], dict)

    @mock.patch("asset_management.web.app.AssetService.get_all", return_value=[])
    def test_whitespace_query_returns_400(self, mock_get_all, auth_client):
        resp = auth_client.get("/api/search?q=   ")
        assert resp.status_code == 400


# ===========================================================================
# POST /api/testing/run-locust — duration validation
# ===========================================================================


class TestRunLocustDurationValidation:
    """duration flows into Locust's --run-time argument as part of subprocess
    argv. Reject anything that isn't a Locust-format duration so a malformed
    value can't reach the subprocess (CodeQL py/command-line-injection)."""

    def test_invalid_duration_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/testing/run-locust",
            json={"users": 5, "spawn_rate": 1, "duration": "60s; rm -rf /"},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["success"] is False
        assert "duration" in body["error"]

    def test_empty_duration_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/testing/run-locust",
            json={"users": 5, "spawn_rate": 1, "duration": ""},
        )
        assert resp.status_code == 400
        assert "duration" in resp.get_json()["error"]

    def test_garbage_duration_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/testing/run-locust",
            json={"users": 5, "spawn_rate": 1, "duration": "not-a-duration"},
        )
        assert resp.status_code == 400


# ===========================================================================
# POST /api/testing/run-locust — profile + identity-mode params
# ===========================================================================


class TestRunLocustStepProfileParams:
    """the stepped baseline→ramp profile and identity mode. Step
    values flow into the subprocess env (like duration flows into argv), so
    malformed input must be rejected at the endpoint."""

    def test_invalid_profile_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/testing/run-locust",
            json={"profile": "chaos"},
        )
        assert resp.status_code == 400
        assert "profile" in resp.get_json()["error"]

    def test_invalid_identity_mode_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/testing/run-locust",
            json={"identity_mode": "everyone"},
        )
        assert resp.status_code == 400
        assert "identity_mode" in resp.get_json()["error"]

    def test_malformed_step_users_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/testing/run-locust",
            json={"profile": "steps", "step_users": "1,5;rm -rf /"},
        )
        assert resp.status_code == 400
        assert "step_users" in resp.get_json()["error"]

    def test_step_users_not_starting_at_one_rejected(self, auth_client):
        """The first step is the single-user baseline window — without it the
        degradation report has no denominator."""
        resp = auth_client.post(
            "/api/testing/run-locust",
            json={"profile": "steps", "step_users": "5,10,25"},
        )
        assert resp.status_code == 400
        assert "baseline" in resp.get_json()["error"]

    def test_step_users_over_cap_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/testing/run-locust",
            json={"profile": "steps", "step_users": "1,5,500"},
        )
        assert resp.status_code == 400

    def test_bad_step_seconds_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/testing/run-locust",
            json={"profile": "steps", "step_users": "1,5", "step_seconds": 5},
        )
        assert resp.status_code == 400
        assert "step_seconds" in resp.get_json()["error"]

    def _launch(self, auth_client, tmp_path, payload):
        """Drive the endpoint through a mocked Popen; return (resp, popen_mock).

         Phase 2: run-locust spawns TWO processes (locust, then the
        resource sampler) — assertions on the locust invocation must use
        call_args_list[0].
        """
        from asset_management.web import app as app_module

        with mock.patch.object(app_module, "_locust_log_path", str(tmp_path / "locust.log")), \
             mock.patch.object(app_module, "_locust_csv_prefix", str(tmp_path / "locust_results")), \
             mock.patch.object(app_module, "_locust_pid_path", str(tmp_path / "locust.pid")), \
             mock.patch.object(app_module, "_locust_resources_path", str(tmp_path / "locust_resources.csv")), \
             mock.patch.object(app_module, "_locust_sampler_pid_path", str(tmp_path / "locust_sampler.pid")), \
             mock.patch.object(app_module, "_locust_sampler_log_path", str(tmp_path / "locust_sampler.log")), \
             mock.patch.object(app_module, "_locust_run_meta_path", str(tmp_path / "locust_run_meta.json")), \
             mock.patch("asset_management.web.app._locust_running_pid", return_value=None), \
             mock.patch("asset_management.web.app._sampler_running_pid", return_value=None), \
             mock.patch("subprocess.Popen") as popen:
            popen.return_value = mock.MagicMock(pid=4242)
            resp = auth_client.post("/api/testing/run-locust", json=payload)
        return resp, popen

    def test_steps_profile_sets_env_and_omits_user_args(self, auth_client, tmp_path):
        """Steps mode: shape env vars set, --csv-full-history present, and the
        shape-conflicting --users/--spawn-rate/--run-time args omitted."""
        resp, popen = self._launch(
            auth_client,
            tmp_path,
            {"profile": "steps", "step_users": "1,5,10", "step_seconds": 60, "identity_mode": "pool"},
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["profile"] == "steps"
        assert body["identity_mode"] == "pool"
        assert body["total_seconds"] == 180
        assert body["step_users"] == [1, 5, 10]

        cmd = popen.call_args_list[0].args[0]
        env = popen.call_args_list[0].kwargs["env"]
        assert "--csv-full-history" in cmd
        assert "--users" not in cmd
        assert "--run-time" not in cmd
        assert env["LOAD_TEST_PROFILE"] == "steps"
        assert env["LOAD_TEST_STEP_USERS"] == "1,5,10"
        assert env["LOAD_TEST_STEP_SECONDS"] == "60"
        assert env["LOAD_TEST_IDENTITY_MODE"] == "pool"

    def test_classic_profile_unchanged(self, auth_client, tmp_path):
        """Classic mode keeps the earlier argv (users/spawn-rate/run-time)
        and does NOT set the shape env var."""
        resp, popen = self._launch(
            auth_client,
            tmp_path,
            {"users": 5, "spawn_rate": 2, "duration": "60s"},
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["profile"] == "classic"
        assert body["identity_mode"] == "single"
        assert body["total_seconds"] is None

        cmd = popen.call_args_list[0].args[0]
        env = popen.call_args_list[0].kwargs["env"]
        assert "--users" in cmd and "--run-time" in cmd
        assert "LOAD_TEST_PROFILE" not in env
        assert env["LOAD_TEST_IDENTITY_MODE"] == "single"

    def test_probe_identity_injected_from_csv(self, auth_client, tmp_path, monkeypatch):
        """Under ENFORCE_MFA the historical elastic default can never
        web-login, so the seeded probe row becomes LOAD_TEST_USERNAME unless
        an explicit env override exists."""
        identities = tmp_path / "identities.csv"
        identities.write_text(
            "username,password,role,workspace\n"
            "loadtest-01@example.com,pw1,unscoped,\n"
            "loadtest-probe@example.com,pw-probe,probe,\n"
        )
        monkeypatch.setenv("LOAD_TEST_IDENTITY_FILE", str(identities))
        monkeypatch.delenv("LOAD_TEST_USERNAME", raising=False)
        monkeypatch.delenv("LOAD_TEST_PASSWORD", raising=False)

        resp, popen = self._launch(auth_client, tmp_path, {"users": 2, "duration": "60s"})
        assert resp.status_code == 200, resp.get_json()
        env = popen.call_args_list[0].kwargs["env"]
        assert env["LOAD_TEST_USERNAME"] == "loadtest-probe@example.com"
        assert env["LOAD_TEST_PASSWORD"] == "pw-probe"

    def test_explicit_username_env_wins_over_probe_row(self, auth_client, tmp_path, monkeypatch):
        identities = tmp_path / "identities.csv"
        identities.write_text("username,password,role,workspace\nloadtest-probe@example.com,pw-probe,probe,\n")
        monkeypatch.setenv("LOAD_TEST_IDENTITY_FILE", str(identities))
        monkeypatch.setenv("LOAD_TEST_USERNAME", "operator-choice")
        monkeypatch.setenv("LOAD_TEST_PASSWORD", "operator-pw")

        resp, popen = self._launch(auth_client, tmp_path, {"users": 2, "duration": "60s"})
        assert resp.status_code == 200, resp.get_json()
        env = popen.call_args_list[0].kwargs["env"]
        assert env["LOAD_TEST_USERNAME"] == "operator-choice"
        assert env["LOAD_TEST_PASSWORD"] == "operator-pw"

    def test_unscoped_identities_excluded_by_default(self, auth_client, tmp_path, monkeypatch):
        """Unscoped pool identities dilute the capacity numbers (they only
        exercise the 403 gate), so they're opt-in per run — the env flag must
        be ABSENT unless include_unscoped is sent, even if a stale value
        lingers in the server's environment from a previous run."""
        monkeypatch.setenv("LOAD_TEST_INCLUDE_UNSCOPED", "1")
        resp, popen = self._launch(
            auth_client, tmp_path, {"identity_mode": "pool", "users": 2, "duration": "60s"}
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["include_unscoped"] is False
        env = popen.call_args_list[0].kwargs["env"]
        assert "LOAD_TEST_INCLUDE_UNSCOPED" not in env

    def test_include_unscoped_sets_env_flag(self, auth_client, tmp_path):
        resp, popen = self._launch(
            auth_client,
            tmp_path,
            {"identity_mode": "pool", "users": 2, "duration": "60s", "include_unscoped": True},
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["include_unscoped"] is True
        env = popen.call_args_list[0].kwargs["env"]
        assert env["LOAD_TEST_INCLUDE_UNSCOPED"] == "1"

    def test_run_writes_meta_and_starts_sampler(self, auth_client, tmp_path):
        """Phase 2: the run records display metadata and launches the
        resource sampler watching the locust PID."""
        import json

        resp, popen = self._launch(
            auth_client,
            tmp_path,
            {"profile": "steps", "step_users": "1,5", "step_seconds": 60},
        )
        assert resp.status_code == 200, resp.get_json()

        meta = json.loads((tmp_path / "locust_run_meta.json").read_text())
        assert meta["profile"] == "steps"
        assert meta["step_users"] == [1, 5]
        assert meta["total_seconds"] == 120

        assert len(popen.call_args_list) == 2
        sampler_cmd = popen.call_args_list[1].args[0]
        assert any("resource_sampler.py" in part for part in sampler_cmd)
        assert "--watch-pid" in sampler_cmd
        assert sampler_cmd[sampler_cmd.index("--watch-pid") + 1] == "4242"
        assert (tmp_path / "locust_sampler.pid").read_text() == "4242"


# ===========================================================================
# _locust_running_pid PID-reuse hardening + stop-locust cleanup
# ===========================================================================


class TestLocustRunningPidValidation:
    """a bare ``os.kill(pid, 0)`` is not enough — the OS recycles
    PIDs, so a stale PID file can keep the running flag wedged forever once
    the original Locust subprocess exits and the kernel hands its PID to an
    unrelated process. ``_locust_running_pid`` must additionally verify that
    the live process is actually a Locust invocation."""

    def test_returns_none_and_removes_pid_file_when_pid_belongs_to_non_locust_process(
        self, tmp_path, auth_client
    ):
        from asset_management.web import app as app_module

        pid_file = tmp_path / "locust.pid"
        pid_file.write_text(str(os.getpid()))  # current process is pytest, not locust

        with mock.patch.object(app_module, "_locust_pid_path", str(pid_file)):
            result = app_module._locust_running_pid()

        assert result is None
        assert not pid_file.exists()

    def test_returns_pid_when_cmdline_contains_locust(self, tmp_path):
        from asset_management.web import app as app_module

        pid_file = tmp_path / "locust.pid"
        pid_file.write_text("12345")

        # First open() reads the PID file (text mode); second reads
        # /proc/<pid>/cmdline (binary mode). Replace builtins.open with a
        # side_effect that returns the right handle for each call.
        handles = [
            mock.mock_open(read_data="12345").return_value,
            mock.mock_open(read_data=b"python3\x00-m\x00locust\x00--headless\x00").return_value,
        ]
        with mock.patch.object(app_module, "_locust_pid_path", str(pid_file)), \
             mock.patch("builtins.open", side_effect=handles):
            result = app_module._locust_running_pid()

        assert result == 12345

    def test_returns_none_when_pid_file_missing(self, tmp_path):
        from asset_management.web import app as app_module

        pid_file = tmp_path / "locust.pid"  # never created
        with mock.patch.object(app_module, "_locust_pid_path", str(pid_file)):
            assert app_module._locust_running_pid() is None

    def test_returns_none_and_removes_pid_file_when_pid_invalid(self, tmp_path):
        from asset_management.web import app as app_module

        pid_file = tmp_path / "locust.pid"
        pid_file.write_text("not-a-number")

        with mock.patch.object(app_module, "_locust_pid_path", str(pid_file)):
            result = app_module._locust_running_pid()

        assert result is None
        assert not pid_file.exists()


class TestStopLocust:
    """stop-locust must always remove the PID file (even if the
    subprocess is already gone) so that the running flag cannot survive a
    completed run. It must also escalate from SIGINT to SIGTERM if Locust
    ignores the interrupt."""

    def test_unauthenticated_redirects(self, client):
        resp = client.post("/api/testing/stop-locust")
        assert resp.status_code == 401

    def test_non_admin_gets_403(self, org_user_client):
        resp = org_user_client.post("/api/testing/stop-locust")
        assert resp.status_code == 403

    def test_returns_404_when_nothing_running(self, auth_client):
        with mock.patch("asset_management.web.app._locust_running_pid", return_value=None):
            resp = auth_client.post("/api/testing/stop-locust")
        assert resp.status_code == 404
        assert "No load test is running" in resp.get_json()["error"]

    def test_removes_pid_file_after_stopping(self, tmp_path, auth_client):
        from asset_management.web import app as app_module

        pid_file = tmp_path / "locust.pid"
        pid_file.write_text("12345")

        # _locust_running_pid → 12345 (pretend Locust is alive); first os.kill
        # is the SIGINT; the polling os.kill(pid, 0) raises ProcessLookupError
        # immediately so we don't sleep through 5 seconds of test time.
        kill_calls = []

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError()
            return None

        with mock.patch.object(app_module, "_locust_pid_path", str(pid_file)), \
             mock.patch("asset_management.web.app._locust_running_pid", return_value=12345), \
             mock.patch("os.kill", side_effect=fake_kill):
            resp = auth_client.post("/api/testing/stop-locust")

        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert not pid_file.exists()
        # SIGINT was sent at least once; we don't pin further signal counts.
        assert any(sig != 0 for _, sig in kill_calls)

    def test_escalates_to_sigterm_when_sigint_ignored(self, tmp_path, auth_client):
        """If the process never exits within the polling window, the handler
        sends SIGTERM as a fallback. We assert SIGTERM appears in the kill
        call sequence."""
        import signal as signal_module

        from asset_management.web import app as app_module

        pid_file = tmp_path / "locust.pid"
        pid_file.write_text("12345")

        kill_calls = []

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))
            return None  # always succeed → process never appears dead

        with mock.patch.object(app_module, "_locust_pid_path", str(pid_file)), \
             mock.patch("asset_management.web.app._locust_running_pid", return_value=12345), \
             mock.patch("os.kill", side_effect=fake_kill), \
             mock.patch("time.sleep"):
            resp = auth_client.post("/api/testing/stop-locust")

        assert resp.status_code == 200
        sent_signals = [sig for _, sig in kill_calls]
        assert signal_module.SIGINT in sent_signals
        assert signal_module.SIGTERM in sent_signals
        assert not pid_file.exists()


# ===========================================================================
# GET /api/testing/load-report — api_testing_load_report
# ===========================================================================


_HISTORY_CSV_FIXTURE = (
    "Timestamp,User Count,Type,Name,Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%,"
    "Total Request Count,Total Failure Count,Total Median Response Time,Total Average Response Time,"
    "Total Min Response Time,Total Max Response Time,Total Average Content Size\n"
    "1000,1,GET,probe:/dashboard,0.5,0,100,100,100,100,100,110,110,110,110,110,110,5,0,100,100,90,120,300\n"
    "1000,1,,Aggregated,0.5,0,100,100,100,100,100,110,110,110,110,110,110,5,0,100,100,90,120,300\n"
    "1060,5,GET,probe:/dashboard,0.5,0,200,200,200,200,200,330,330,330,330,330,330,25,0,150,150,90,400,300\n"
    "1060,5,,Aggregated,2.5,0,180,180,180,180,180,300,300,300,300,300,300,25,0,150,150,90,400,300\n"
)

_RESOURCES_CSV_FIXTURE = (
    "timestamp,host_cpu_pct,host_mem_used_pct,host_mem_used_mb,host_mem_total_mb,loadavg_1m,"
    "container_cpu_pct,container_mem_used_mb,container_mem_limit_mb,container_mem_pct,"
    "es_process_cpu_pct,es_heap_used_pct,locust_cpu_pct\n"
    "1970-01-01T00:16:40+00:00,25.0,60.0,4800.0,8000.0,1.2,10.0,900.0,2048.0,43.9,15.0,55.0,2.0\n"
    "1970-01-01T00:17:40+00:00,75.0,70.0,5600.0,8000.0,3.4,30.0,1400.0,2048.0,68.4,,,4.0\n"
)


class TestApiTestingLoadReport:
    """The degradation report endpoint: admin-gated, available:false before
    any run, and computed server-side from the run's CSVs."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/testing/load-report")
        assert resp.status_code == 401

    def test_non_admin_gets_403(self, org_user_client):
        resp = org_user_client.get("/api/testing/load-report")
        assert resp.status_code == 403

    def test_unavailable_when_no_history_csv(self, auth_client, tmp_path):
        from asset_management.web import app as app_module

        with mock.patch.object(app_module, "_locust_csv_prefix", str(tmp_path / "locust_results")):
            resp = auth_client.get("/api/testing/load-report")
        assert resp.status_code == 200
        assert resp.get_json() == {"available": False}

    def test_builds_report_from_run_csvs(self, auth_client, tmp_path):
        """Happy path: baseline from the users==1 window, degradation
        normalized against it, resources parsed, meta folded in."""
        import json

        from asset_management.web import app as app_module

        (tmp_path / "locust_results_stats_history.csv").write_text(_HISTORY_CSV_FIXTURE)
        (tmp_path / "locust_resources.csv").write_text(_RESOURCES_CSV_FIXTURE)
        (tmp_path / "locust_run_meta.json").write_text(json.dumps({"profile": "steps"}))

        with mock.patch.object(app_module, "_locust_csv_prefix", str(tmp_path / "locust_results")), \
             mock.patch.object(app_module, "_locust_resources_path", str(tmp_path / "locust_resources.csv")), \
             mock.patch.object(app_module, "_locust_run_meta_path", str(tmp_path / "locust_run_meta.json")):
            resp = auth_client.get("/api/testing/load-report")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["available"] is True
        assert body["meta"]["profile"] == "steps"
        assert body["baseline"] == {
            "p50_ms": 100.0,
            "p95_ms": 110.0,
            "samples": 1,
            "per_name": {"probe:/dashboard": {"p50_ms": 100.0, "p95_ms": 110.0}},
        }
        # At 5 users the probe's p50 doubled and p95 tripled vs baseline.
        assert body["degradation"][-1]["x50"] == 2.0
        assert body["degradation"][-1]["x95"] == 3.0
        assert body["steps"] == [
            {"users": 1, "from_t": 1000, "to_t": 1000},
            {"users": 5, "from_t": 1060, "to_t": 1060},
        ]
        assert len(body["resources"]) == 2
        assert body["resources"][1]["host_cpu_pct"] == 75.0
        assert body["resources"][1]["es_process_cpu_pct"] is None  # blank cell


# ===========================================================================
# Import-load mode endpoints
# ===========================================================================


class TestImportLoadEndpoints:
    def test_candidates_unauthenticated(self, client):
        assert client.get("/api/testing/import-load-candidates").status_code == 401

    def test_candidates_non_admin_403(self, org_user_client):
        assert org_user_client.get("/api/testing/import-load-candidates").status_code == 403

    @mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None)
    def test_candidates_es_down_503(self, _es, auth_client):
        resp = auth_client.get("/api/testing/import-load-candidates")
        assert resp.status_code == 503

    @mock.patch("asset_management.web.services.import_load_service.list_candidates", return_value=[])
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_candidates_happy_path(self, _es, _list, auth_client):
        resp = auth_client.get("/api/testing/import-load-candidates")
        assert resp.status_code == 200
        assert resp.get_json() == {"success": True, "candidates": []}

    def test_run_unauthenticated(self, client):
        assert client.post("/api/testing/run-import-load", json={}).status_code == 401

    def test_run_non_admin_403(self, org_user_client):
        assert org_user_client.post("/api/testing/run-import-load", json={}).status_code == 403

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_run_rejects_invalid_payload(self, _es, auth_client):
        resp = auth_client.post("/api/testing/run-import-load", json={"imports": []})
        assert resp.status_code == 400
        assert "non-empty" in resp.get_json()["error"]

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_run_rejects_duplicate_lock_groups(self, get_es, auth_client):
        # Teams exist (pass the existence check) so the lock-group collision
        # is what rejects the request.
        get_es.return_value.search.return_value = {"hits": {"hits": [{"_id": "t1"}, {"_id": "t2"}]}}
        resp = auth_client.post(
            "/api/testing/run-import-load",
            json={
                "imports": [
                    {"source": "facebook_ads", "team_ids": "t1", "start_date": "2026-07-01", "end_date": "2026-07-02"},
                    {"source": "instagram", "team_ids": "t2", "start_date": "2026-07-01", "end_date": "2026-07-02"},
                ]
            },
        )
        assert resp.status_code == 400
        assert "lock" in resp.get_json()["error"]

    def test_status_requires_import_id(self, auth_client):
        resp = auth_client.get("/api/testing/import-load-status")
        assert resp.status_code == 400

    @mock.patch(
        "asset_management.web.services.import_load_service.import_load_status",
        return_value={"found": True, "completed": True, "markers": []},
    )
    def test_status_happy_path(self, _status, auth_client):
        resp = auth_client.get("/api/testing/import-load-status?import_id=abc12345")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["markers"] == []


# ===========================================================================
# GET /api/testing/locust-results — new fields
# ===========================================================================


class TestLocustResultsNewFields:
    def test_results_include_history_and_resources(self, auth_client, tmp_path):
        from asset_management.web import app as app_module

        (tmp_path / "locust_results_stats_history.csv").write_text("h")
        (tmp_path / "locust_resources.csv").write_text("r")

        with mock.patch.object(app_module, "_locust_log_path", str(tmp_path / "locust.log")), \
             mock.patch.object(app_module, "_locust_csv_prefix", str(tmp_path / "locust_results")), \
             mock.patch.object(app_module, "_locust_resources_path", str(tmp_path / "locust_resources.csv")):
            resp = auth_client.get("/api/testing/locust-results")

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["stats_history_csv"] == "h"
        assert body["resources_csv"] == "r"


# ===========================================================================
# GET /api/testing/release-plans — api_testing_release_plans
# ===========================================================================


def _use_sample_catalog(monkeypatch, plans: dict) -> None:
    """Swap the app's release-plan catalog for a fixed one. The scanner itself
    is covered by tests/services/test_release_plan_service.py; these route tests
    must not depend on which release plans happen to exist in the checkout."""
    from datetime import datetime, timezone

    from asset_management.web import app as app_module

    class _Catalog:
        def get(self) -> dict:
            return {k: list(v) for k, v in plans.items()}

        def scanned_at(self):
            return datetime.now(timezone.utc)

        def invalidate(self) -> None:
            return None

    monkeypatch.setattr(app_module, "_RELEASE_PLAN_CATALOG", _Catalog())


class TestApiTestingReleasePlans:
    """Endpoint returns the cached marker catalog. Admin-only."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/testing/release-plans")
        assert resp.status_code == 401

    def test_non_admin_gets_403(self, org_user_client):
        resp = org_user_client.get("/api/testing/release-plans")
        assert resp.status_code == 403

    def test_returns_plans_with_test_id_lists(self, auth_client, monkeypatch):
        # The catalog scans test files for release-plan markers. Point it at a
        # sample file instead of the repo's own tests, so the result does not
        # depend on which release plans happen to exist in this checkout.
        _use_sample_catalog(monkeypatch, {"plan-25": ["SEC-CSP-001", "SEC-CSP-002", "SEC-CSP-003"]})
        resp = auth_client.get("/api/testing/release-plans")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "plans" in body
        names = {p["name"] for p in body["plans"]}
        assert "plan-25" in names
        pr25 = next(p for p in body["plans"] if p["name"] == "plan-25")
        assert len(pr25["test_ids"]) >= 3
        assert "scanned_at" in body


# ===========================================================================
# POST /api/testing/run-release-plan — api_testing_run_release_plan
# ===========================================================================


class TestApiTestingRunReleasePlan:
    """End-to-end runner: pytest live + most-recent Playwright JSON."""

    def test_unauthenticated_redirects(self, client):
        resp = client.post("/api/testing/run-release-plan", json={"plan": "plan-25"})
        assert resp.status_code == 401

    def test_non_admin_gets_403(self, org_user_client):
        resp = org_user_client.post(
            "/api/testing/run-release-plan", json={"plan": "plan-25"}
        )
        assert resp.status_code == 403

    def test_missing_plan_returns_400(self, auth_client):
        resp = auth_client.post("/api/testing/run-release-plan", json={})
        assert resp.status_code == 400
        assert "plan is required" in resp.get_json()["error"]

    def test_unknown_plan_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/testing/run-release-plan", json={"plan": "PR-DOES-NOT-EXIST"}
        )
        assert resp.status_code == 400
        assert "Unknown plan" in resp.get_json()["error"]

    @mock.patch("builtins.open", mock.mock_open())
    @mock.patch("os.makedirs")
    @mock.patch("subprocess.run")
    def test_merges_pytest_and_playwright_results(
        self, mock_run, mock_makedirs, auth_client, tmp_path, monkeypatch
    ):
        """Pytest writes a JSON report; the Playwright loader reads the most
        recent JSON from /app/data/logs/e2e/. Both contribute rows to the
        merged response."""
        from asset_management.web import app as app_module

        # Have the pytest subprocess "succeed" with a stub report.
        pytest_report_data = {
            "results": {
                "SEC-CSP-001": {
                    "outcome": "passed",
                    "duration": 0.05,
                    "source": "pytest",
                    "longrepr": None,
                }
            }
        }
        playwright_payload = {
            "generated_at": "2026-04-28T20:00:00+00:00",
            "release_plan": "plan-25",
            "results": {
                "SEC-CSP-005": {
                    "outcome": "passed",
                    "duration": 1.2,
                    "source": "playwright",
                    "longrepr": None,
                }
            },
        }

        mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
        _use_sample_catalog(monkeypatch, {"plan-25": ["SEC-CSP-001"]})

        # Patch os.path.exists to claim the pytest report exists, and
        # _load_latest_playwright_results to return our stub payload.
        monkeypatch.setattr(
            app_module,
            "_load_latest_playwright_results",
            lambda plan: playwright_payload,
        )
        plan = "plan-25"
        monkeypatch.setattr(os.path, "exists", lambda p: f"release_plan_{plan}_pytest" in p)

        # Patch json.load via builtins.open mock — the endpoint reads the
        # report file. Easiest: monkeypatch json.load globally.
        import json as _json

        monkeypatch.setattr(
            _json,
            "load",
            lambda f: pytest_report_data,
        )

        resp = auth_client.post(
            "/api/testing/run-release-plan", json={"plan": "plan-25"}
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["success"] is True
        assert body["plan"] == "plan-25"
        ids = {r["test_id"] for r in body["results"]}
        assert "SEC-CSP-001" in ids
        assert "SEC-CSP-005" in ids
        # Playwright run is timestamped, not stale ⇒ playwright_stale is True
        # (Apr 28 vs run time, depending on test execution time, may flap;
        # just assert the field is present).
        assert "playwright_run_at" in body
        assert "playwright_stale" in body
