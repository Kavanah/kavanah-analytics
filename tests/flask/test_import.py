"""
Tests for import API routes and dashboard API routes.

Import routes covered:
  POST /api/import/run                   run_imports (validation + ImportService)
  GET  /api/import/status/<import_id>    get_import_status
  POST /api/import/cancel/<import_id>    cancel_import
  POST /api/import/delete-log            delete_manual_log
  GET  /api/import/log-exists            check_todays_log (uses MANUAL_LOG_FILE)
  GET  /api/import/log-content           get_log_content

Dashboard API routes covered:
  GET  /api/dashboards/space/<space_id>  get_dashboards_in_space
  GET  /dashboard                        dashboard_page (HTML)

All Elasticsearch and external service calls are mocked.
"""

import io
import pytest
from unittest import mock

from asset_management.web.services.import_service import ImportService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_import_payload(source="facebook_ads", days=7):
    """Return a minimal valid payload for /api/import/run."""
    return {
        "imports": [{"source": source, "days": days}]
    }


# ---------------------------------------------------------------------------
# POST /api/import/run  — input validation (no ImportService mock needed)
# ---------------------------------------------------------------------------

class TestRunImportsValidation:
    """These tests exercise the in-route validation before ImportService is called."""

    def test_no_body_returns_400(self, auth_client):
        # {} is falsy in Python, so the route's `if not data` guard returns 400.
        resp = auth_client.post("/api/import/run", json={})
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_empty_imports_list_returns_400(self, auth_client):
        resp = auth_client.post("/api/import/run", json={"imports": []})
        assert resp.status_code == 400

    def test_invalid_source_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "not_a_real_source", "days": 7}]},
        )
        assert resp.status_code == 400
        assert "Invalid source" in resp.get_json()["error"]

    def test_missing_days_and_dates_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "facebook_ads"}]},
        )
        assert resp.status_code == 400
        assert "Must provide" in resp.get_json()["error"]

    def test_both_days_and_dates_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "facebook_ads", "days": 7, "start_date": "2024-01-01", "end_date": "2024-01-07"}]},
        )
        assert resp.status_code == 400
        assert "Provide only one of" in resp.get_json()["error"]

    def test_days_zero_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "facebook_ads", "days": 0}]},
        )
        assert resp.status_code == 400

    def test_days_over_365_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "facebook_ads", "days": 366}]},
        )
        assert resp.status_code == 400

    def test_start_date_after_end_date_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "facebook_ads", "start_date": "2024-01-10", "end_date": "2024-01-01"}]},
        )
        assert resp.status_code == 400
        assert "Start date must be" in resp.get_json()["error"]

    def test_invalid_date_format_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "facebook_ads", "start_date": "not-a-date", "end_date": "also-bad"}]},
        )
        assert resp.status_code == 400
        assert "Invalid date format" in resp.get_json()["error"]

    # team_ids flows into a subprocess argv via _build_import_command.
    # Validate that obviously-malicious values are rejected at the route layer.

    def test_team_ids_with_shell_metachars_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "facebook_ads", "days": 7, "team_ids": "; rm -rf /"}]},
        )
        assert resp.status_code == 400
        assert "team_ids contains invalid characters" in resp.get_json()["error"]

    def test_team_ids_non_string_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "facebook_ads", "days": 7, "team_ids": ["a", "b"]}]},
        )
        assert resp.status_code == 400
        assert "team_ids must be a string" in resp.get_json()["error"]

    def test_team_ids_too_long_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "facebook_ads", "days": 7, "team_ids": "a" * 1025}]},
        )
        assert resp.status_code == 400
        assert "team_ids too long" in resp.get_json()["error"]

    def test_team_ids_normal_value_accepted(self, auth_client):
        # Normal comma-separated team list should pass validation. We don't
        # mock ImportService here, so the actual import will fail later (which
        # is fine — we're just checking the validation gate.)
        resp = auth_client.post(
            "/api/import/run",
            json={
                "imports": [
                    {"source": "facebook_ads", "days": 7, "team_ids": "Team A, Team B, team-c_42"}
                ]
            },
        )
        # Should NOT be the team_ids 400 — either it succeeds or fails further down.
        if resp.status_code == 400:
            assert "team_ids" not in resp.get_json().get("error", "")

    def test_date_range_over_365_days_returns_400(self, auth_client):
        resp = auth_client.post(
            "/api/import/run",
            json={"imports": [{"source": "facebook_ads", "start_date": "2022-01-01", "end_date": "2023-12-31"}]},
        )
        assert resp.status_code == 400
        assert "365 days" in resp.get_json()["error"]

    def test_unauthenticated_returns_redirect(self, client):
        resp = client.post("/api/import/run", json=_valid_import_payload())
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/import/run  — ImportService delegation
# ---------------------------------------------------------------------------

class TestRunImportsService:

    def test_successful_import_returns_import_id(self, auth_client):
        mock_result = {
            "import_id": "test-import-123",
            "started": True,
            "message": "Import started",
        }
        with mock.patch("asset_management.web.app.ImportService.run_imports", return_value=mock_result):
            resp = auth_client.post("/api/import/run", json=_valid_import_payload())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["import_id"] == "test-import-123"

    def test_delete_log_flag_calls_service(self, auth_client):
        mock_result = {"import_id": "x", "started": True, "message": "ok"}
        with mock.patch("asset_management.web.app.ImportService.run_imports", return_value=mock_result), \
             mock.patch("asset_management.web.app.ImportService.delete_manual_log", return_value={"success": True}) as mock_del:
            auth_client.post(
                "/api/import/run",
                json={**_valid_import_payload(), "delete_log": True},
            )
        mock_del.assert_called_once()

    def test_import_service_exception_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.run_imports", side_effect=RuntimeError("boom")):
            resp = auth_client.post("/api/import/run", json=_valid_import_payload())
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False


# ---------------------------------------------------------------------------
# GET /api/import/status/<import_id>
# ---------------------------------------------------------------------------

class TestGetImportStatus:

    def test_returns_status_from_service(self, auth_client):
        mock_status = {"found": True, "status": "running", "progress": 50}
        with mock.patch("asset_management.web.app.ImportService.get_import_status", return_value=mock_status):
            resp = auth_client.get("/api/import/status/test-123")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"

    def test_service_exception_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.get_import_status", side_effect=Exception("err")):
            resp = auth_client.get("/api/import/status/test-123")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/import/cancel/<import_id>
# ---------------------------------------------------------------------------

class TestCancelImport:

    def test_successful_cancel(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.cancel_import", return_value={"success": True}):
            resp = auth_client.post("/api/import/cancel/test-123")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_service_exception_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.cancel_import", side_effect=Exception("err")):
            resp = auth_client.post("/api/import/cancel/test-123")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# POST /api/import/delete-log
# ---------------------------------------------------------------------------

class TestDeleteTodaysLog:

    def test_successful_delete(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.delete_manual_log", return_value={"success": True}):
            resp = auth_client.post("/api/import/delete-log")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_service_exception_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.delete_manual_log", side_effect=Exception("err")):
            resp = auth_client.post("/api/import/delete-log")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/import/log-exists
# ---------------------------------------------------------------------------

class TestCheckTodaysLog:

    def test_log_exists(self, auth_client):
        with mock.patch("asset_management.web.app.os.path.exists", return_value=True):
            resp = auth_client.get("/api/import/log-exists")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is True
        assert data["filename"] == "imports_manual.log"

    def test_log_does_not_exist(self, auth_client):
        with mock.patch("asset_management.web.app.os.path.exists", return_value=False):
            resp = auth_client.get("/api/import/log-exists")
        assert resp.status_code == 200
        assert resp.get_json()["exists"] is False


# ---------------------------------------------------------------------------
# GET /api/import/log-content
# ---------------------------------------------------------------------------

class TestGetLogContent:

    def test_returns_empty_when_log_not_found(self, auth_client):
        with mock.patch.object(ImportService, "MANUAL_LOG_FILE", "/tmp/no-such-file.log"):
            resp = auth_client.get("/api/import/log-content")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is False
        assert data["content"] == ""

    def test_returns_content_when_log_exists(self, auth_client, tmp_path):
        log_file = tmp_path / "imports_manual.log"
        log_file.write_text("2024-01-01 INFO: Import started\n")
        with mock.patch.object(ImportService, "MANUAL_LOG_FILE", str(log_file)):
            resp = auth_client.get("/api/import/log-content")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is True
        assert "Import started" in data["content"]


# ---------------------------------------------------------------------------
# GET /api/dashboards/space/<space_id>
# ---------------------------------------------------------------------------

class TestGetDashboardsInSpace:

    def test_returns_dashboards_for_space(self, superuser_client):
        mock_dashboards = [{"id": "d1", "title": "Dashboard 1"}]
        with mock.patch("asset_management.web.app.KibanaService.get_dashboards_in_space", return_value=mock_dashboards):
            resp = superuser_client.get("/api/dashboards/space/default")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["space_id"] == "default"
        assert len(data["dashboards"]) == 1

    def test_service_exception_returns_500(self, superuser_client):
        with mock.patch("asset_management.web.app.KibanaService.get_dashboards_in_space", side_effect=Exception("Kibana down")):
            resp = superuser_client.get("/api/dashboards/space/default")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /dashboards  — HTML page
# ---------------------------------------------------------------------------

class TestDashboardsPage:

    def _mock_dashboard_services(self):
        """Return a context manager that mocks all services the dashboards page needs."""
        return mock.patch.multiple(
            "asset_management.web.app",
            **{
                "DashboardService.get_visible_echarts_dashboards": mock.DEFAULT,
                "RoleFilterService.get_filter_visibility": mock.DEFAULT,
                "RoleFilterService.get_allowed_organizations": mock.DEFAULT,
                "RoleFilterService.get_allowed_teams": mock.DEFAULT,
            },
        )

    def test_admin_gets_200(self, auth_client):
        with mock.patch("asset_management.web.app.DashboardService.get_visible_echarts_dashboards", return_value=[]), \
             mock.patch("asset_management.web.app.RoleFilterService.get_filter_visibility", return_value={}), \
             mock.patch("asset_management.web.app.RoleFilterService.get_allowed_organizations", return_value=None), \
             mock.patch("asset_management.web.app.RoleFilterService.get_allowed_teams", return_value=None):
            resp = auth_client.get("/dashboard")
        assert resp.status_code == 200

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_org_user_gets_200(self, org_user_client):
        with mock.patch("asset_management.web.app.DashboardService.get_visible_echarts_dashboards", return_value=[]), \
             mock.patch("asset_management.web.app.RoleFilterService.get_filter_visibility", return_value={}), \
             mock.patch("asset_management.web.app.RoleFilterService.get_allowed_organizations", return_value=["TestOrg"]), \
             mock.patch("asset_management.web.app.RoleFilterService.get_allowed_teams", return_value=None), \
             mock.patch("asset_management.web.app.RoleFilterService.get_allowed_teams_for_org_users", return_value=["TeamA"]):
            resp = org_user_client.get("/dashboard")
        assert resp.status_code == 200
