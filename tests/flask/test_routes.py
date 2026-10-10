"""
Tests for the remaining Flask routes in app.py.

Covers:
  Logs:
    GET  /logs
    GET  /logs/view/<filename>
    POST /logs/delete/<filename>
    GET  /logs/download/<filename>

  Status Reports:
    GET  /status-reports

  Dashboards:
    GET  /dashboards
    GET  /api/dashboards/<dashboard_key>
    GET  /api/dashboards/space/<space_id>

  Import Data:
    GET  /import-data
    POST /api/import/run
    GET  /api/import/status/<import_id>
    POST /api/import/cancel/<import_id>
    POST /api/import/delete-log
    GET  /api/import/log-exists
    GET  /api/import/log-content

  Asset Options & Tree:
    GET  /api/options/<index_name>
    GET  /api/options/<index_name>/filtered
    GET  /api/tree/structure
    GET  /api/tree/assets/<asset_type>

  Asset Details & Search:
    GET  /api/asset/<asset_type>/<item_id>/details
    GET  /api/dashboard/summary
    GET  /api/search

  Backup:
    POST /api/backup/create
    GET  /api/backup/list
    POST /api/backup/restore

All Elasticsearch and service calls are mocked.
"""

import json
import os
import pytest
from unittest import mock

from asset_management.web.services.import_service import ImportService
from lib.errors import InvalidRequestError, RequestedItemNotFoundError  # what the services raise

# get_options / get_asset_details tests target the now-scoped
# /api/options + company-tier details routes; neutralize those gates (covered by
# the release-plan tests).
pytestmark = pytest.mark.usefixtures("allow_company_tier_and_options_scope")


# ---------------------------------------------------------------------------
# Shared mock data
# ---------------------------------------------------------------------------

VALID_ASSET_TYPE = "teams"
ITEM_ID = "abc123"

MOCK_HIT = {
    "_id": ITEM_ID,
    "_source": {"team_title": "Test Team"},
}


# ===========================================================================
# LOGS ROUTES
# ===========================================================================

class TestListLogs:

    def test_returns_200_with_logs(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.get_recent_logs",
                        return_value=[{"filename": "import_2024-01-15.log", "size": 1024}]):
            resp = auth_client.get("/logs")
        assert resp.status_code == 200

    def test_returns_500_on_service_error(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.get_recent_logs",
                        side_effect=Exception("disk error")):
            resp = auth_client.get("/logs")
        assert resp.status_code == 500

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


class TestViewLog:

    def test_returns_200_for_existing_file(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.read_log_file",
                        return_value="line1\nline2\nline3"), \
             mock.patch("asset_management.web.app.LogService.get_line_class",
                        return_value="log-info"):
            resp = auth_client.get("/logs/view/import_2024-01-15.log")
        assert resp.status_code == 200

    def test_returns_404_for_missing_file(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.read_log_file",
                        side_effect=FileNotFoundError("not found")):
            resp = auth_client.get("/logs/view/missing.log")
        assert resp.status_code == 404

    def test_returns_500_on_read_error(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.read_log_file",
                        side_effect=Exception("io error")):
            resp = auth_client.get("/logs/view/bad.log")
        assert resp.status_code == 500


class TestDeleteLog:

    def test_returns_success_true(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.delete_log_file"):
            resp = auth_client.post("/logs/delete/import_2024-01-15.log")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_returns_500_on_error(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.delete_log_file",
                        side_effect=Exception("permission denied")):
            resp = auth_client.post("/logs/delete/bad.log")
        assert resp.status_code == 500


class TestDownloadLog:

    def test_returns_file_for_existing_log(self, auth_client, tmp_path):
        log_file = tmp_path / "import_2024-01-15.log"
        log_file.write_text("log content")
        with mock.patch("asset_management.web.app.LogService.get_log_filepath",
                        return_value=str(log_file)):
            resp = auth_client.get("/logs/download/import_2024-01-15.log")
        assert resp.status_code == 200

    def test_returns_404_for_missing_file(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.get_log_filepath",
                        side_effect=FileNotFoundError("not found")):
            resp = auth_client.get("/logs/download/missing.log")
        assert resp.status_code == 404

    def test_returns_500_on_error(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.get_log_filepath",
                        side_effect=Exception("io error")):
            resp = auth_client.get("/logs/download/bad.log")
        assert resp.status_code == 500


# ===========================================================================
# STATUS REPORTS
# ===========================================================================

class TestStatusReports:

    def test_returns_200(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.get_most_recent_log_errors",
                        return_value={"errors": [], "warnings": []}):
            resp = auth_client.get("/status-reports")
        assert resp.status_code == 200

    def test_returns_500_on_error(self, auth_client):
        with mock.patch("asset_management.web.app.LogService.get_most_recent_log_errors",
                        side_effect=Exception("read error")):
            resp = auth_client.get("/status-reports")
        assert resp.status_code == 500


# ===========================================================================
# DASHBOARDS ROUTES
# ===========================================================================

class TestDashboardsPage:

    def _mock_dashboard_services(self):
        return [
            mock.patch("asset_management.web.blueprints.analytics.routes.DashboardService.get_visible_echarts_dashboards",
                       return_value=[]),
            mock.patch("asset_management.web.blueprints.analytics.routes.RoleFilterService.get_filter_visibility",
                       return_value={}),
            mock.patch("asset_management.web.blueprints.analytics.routes.RoleFilterService.get_allowed_organizations",
                       return_value=None),
            mock.patch("asset_management.web.blueprints.analytics.routes.RoleFilterService.get_allowed_teams",
                       return_value=None),
            mock.patch("asset_management.web.blueprints.analytics.routes.RoleFilterService.get_partner_organizations",
                       return_value=None),
        ]

    def test_returns_200_for_authenticated_user(self, auth_client):
        patches = self._mock_dashboard_services()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            resp = auth_client.get("/dashboard")
        assert resp.status_code == 200

    def test_returns_200_for_org_user(self, org_user_client):
        patches = self._mock_dashboard_services()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            resp = org_user_client.get("/dashboard")
        assert resp.status_code == 200

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 302

    def test_org_user_with_org_filter_gets_teams(self, org_user_client):
        """Org users with no direct teams get teams via org."""
        patches = [
            mock.patch("asset_management.web.blueprints.analytics.routes.DashboardService.get_visible_echarts_dashboards", return_value=[]),
            mock.patch("asset_management.web.blueprints.analytics.routes.RoleFilterService.get_filter_visibility", return_value={}),
            mock.patch("asset_management.web.blueprints.analytics.routes.RoleFilterService.get_allowed_organizations", return_value=["Acme"]),
            mock.patch("asset_management.web.blueprints.analytics.routes.RoleFilterService.get_allowed_teams", return_value=None),
            mock.patch("asset_management.web.blueprints.analytics.routes.RoleFilterService.get_allowed_teams_for_org_users", return_value=["Alpha"]),
            mock.patch("asset_management.web.blueprints.analytics.routes.RoleFilterService.get_partner_organizations", return_value=None),
            mock.patch("asset_management.web.blueprints.analytics.routes.RoleFilterService.get_filter_for_current_user", return_value={}),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            resp = org_user_client.get("/dashboard")
        assert resp.status_code == 200

    def test_returns_500_on_service_error(self, auth_client):
        with mock.patch("asset_management.web.blueprints.analytics.routes.DashboardService.get_visible_echarts_dashboards",
                        side_effect=Exception("ES down")):
            resp = auth_client.get("/dashboard")
        assert resp.status_code == 500


class TestGetDashboardInfo:

    def test_returns_dashboard_for_valid_key(self, auth_client):
        resp = auth_client.get("/api/dashboards/fb_ads_overview")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["dashboard_key"] == "fb_ads_overview"

    def test_returns_404_for_unknown_key(self, auth_client):
        resp = auth_client.get("/api/dashboards/nonexistent_key")
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/dashboards/fb_ads_overview")
        assert resp.status_code == 401


class TestGetDashboardsInSpace:

    def test_returns_dashboards_list(self, superuser_client):
        with mock.patch("asset_management.web.app.KibanaService.get_dashboards_in_space",
                        return_value=[{"id": "d1", "title": "My Dashboard"}]):
            resp = superuser_client.get("/api/dashboards/space/default")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert data["space_id"] == "default"

    def test_returns_500_on_kibana_error(self, superuser_client):
        with mock.patch("asset_management.web.app.KibanaService.get_dashboards_in_space",
                        side_effect=Exception("Kibana unreachable")):
            resp = superuser_client.get("/api/dashboards/space/default")
        assert resp.status_code == 500


# ===========================================================================
# IMPORT DATA ROUTES
# ===========================================================================

class TestImportDataPage:

    def test_returns_200(self, auth_client):
        resp = auth_client.get("/import-data")
        assert resp.status_code == 200

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/import-data")
        assert resp.status_code == 302


class TestRunImports:

    def _valid_import(self):
        return [{"source": "facebook_ads", "days": 7}]

    def test_returns_400_when_no_body(self, auth_client):
        # send empty JSON object — route checks data.get("imports", []) == []
        resp = auth_client.post("/api/import/run", json={})
        assert resp.status_code == 400

    def test_returns_400_when_imports_empty(self, auth_client):
        resp = auth_client.post("/api/import/run", json={"imports": []})
        assert resp.status_code == 400

    def test_returns_400_for_invalid_source(self, auth_client):
        resp = auth_client.post("/api/import/run",
                                json={"imports": [{"source": "invalid_source", "days": 7}]})
        assert resp.status_code == 400

    def test_returns_400_when_neither_days_nor_dates(self, auth_client):
        resp = auth_client.post("/api/import/run",
                                json={"imports": [{"source": "facebook_ads"}]})
        assert resp.status_code == 400

    def test_returns_400_when_both_days_and_dates(self, auth_client):
        resp = auth_client.post("/api/import/run", json={"imports": [{
            "source": "facebook_ads",
            "days": 7,
            "start_date": "2024-01-01",
            "end_date": "2024-01-07",
        }]})
        assert resp.status_code == 400

    def test_returns_400_when_days_out_of_range(self, auth_client):
        resp = auth_client.post("/api/import/run",
                                json={"imports": [{"source": "facebook_ads", "days": 400}]})
        assert resp.status_code == 400

    def test_returns_400_when_start_date_after_end_date(self, auth_client):
        resp = auth_client.post("/api/import/run", json={"imports": [{
            "source": "facebook_ads",
            "start_date": "2024-01-10",
            "end_date": "2024-01-01",
        }]})
        assert resp.status_code == 400

    def test_returns_400_for_invalid_date_format(self, auth_client):
        resp = auth_client.post("/api/import/run", json={"imports": [{
            "source": "facebook_ads",
            "start_date": "not-a-date",
            "end_date": "2024-01-07",
        }]})
        assert resp.status_code == 400

    def test_returns_400_for_future_end_date(self, auth_client):
        resp = auth_client.post("/api/import/run", json={"imports": [{
            "source": "facebook_ads",
            "start_date": "2024-01-01",
            "end_date": "2099-12-31",
        }]})
        assert resp.status_code == 400

    def test_returns_400_for_date_range_over_365_days(self, auth_client):
        resp = auth_client.post("/api/import/run", json={"imports": [{
            "source": "facebook_ads",
            "start_date": "2022-01-01",
            "end_date": "2023-12-31",
        }]})
        assert resp.status_code == 400

    def test_successful_run_with_days(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.run_imports",
                        return_value={"import_id": "imp-1", "started": True, "message": "ok"}):
            resp = auth_client.post("/api/import/run",
                                    json={"imports": [{"source": "facebook_ads", "days": 7}]})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_run_with_delete_log_flag(self, auth_client):
        # echo is workspace-owner-gated now (auth_client is only
        # asset_admin), so the delete_log flag is exercised with a non-echo
        # source; the echo gate is pinned in test_echo_import_dedup_gate.
        with mock.patch("asset_management.web.app.ImportService.delete_manual_log",
                        return_value={"success": True}), \
             mock.patch("asset_management.web.app.ImportService.run_imports",
                        return_value={"import_id": "imp-1", "started": True, "message": "ok"}):
            resp = auth_client.post("/api/import/run", json={
                "imports": [{"source": "facebook_ads", "days": 3}],
                "delete_log": True,
            })
        assert resp.status_code == 200

    def test_run_with_delete_data_flag(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.delete_recent_data",
                        return_value={"deleted": 5}), \
             mock.patch("asset_management.web.app.ImportService.run_imports",
                        return_value={"import_id": "imp-2", "started": True, "message": "ok"}):
            resp = auth_client.post("/api/import/run", json={
                "imports": [{"source": "google_ads", "days": 1}],
                "delete_data": True,
            })
        assert resp.status_code == 200


class TestGetImportStatus:

    def test_returns_status_dict(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.get_import_status",
                        return_value={"found": True, "status": "running", "progress": 50}):
            resp = auth_client.get("/api/import/status/imp-123")
        assert resp.status_code == 200
        assert resp.get_json()["found"] is True

    def test_returns_500_on_error(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.get_import_status",
                        side_effect=Exception("not found")):
            resp = auth_client.get("/api/import/status/bad-id")
        assert resp.status_code == 500


class TestCancelImport:

    def test_cancels_running_import(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.cancel_import",
                        return_value={"success": True, "cancelled": 2}):
            resp = auth_client.post("/api/import/cancel/imp-123")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_returns_500_on_error(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.cancel_import",
                        side_effect=Exception("process error")):
            resp = auth_client.post("/api/import/cancel/bad-id")
        assert resp.status_code == 500


class TestDeleteTodaysLog:

    def test_returns_success(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.delete_manual_log",
                        return_value={"success": True, "deleted": True}):
            resp = auth_client.post("/api/import/delete-log")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_returns_500_on_error(self, auth_client):
        with mock.patch("asset_management.web.app.ImportService.delete_manual_log",
                        side_effect=Exception("permission error")):
            resp = auth_client.post("/api/import/delete-log")
        assert resp.status_code == 500


class TestCheckTodaysLog:

    def test_returns_exists_true_when_log_present(self, auth_client):
        with mock.patch("asset_management.web.app.os.path.exists", return_value=True):
            resp = auth_client.get("/api/import/log-exists")
        data = resp.get_json()
        assert data["exists"] is True
        assert data["filename"] == "imports_manual.log"

    def test_returns_exists_false_when_no_log(self, auth_client):
        with mock.patch("asset_management.web.app.os.path.exists", return_value=False):
            resp = auth_client.get("/api/import/log-exists")
        assert resp.get_json()["exists"] is False


class TestGetLogContent:

    def test_returns_exists_false_when_file_missing(self, auth_client, tmp_path):
        nonexistent = str(tmp_path / "no_such_file.log")
        with mock.patch.object(ImportService, "MANUAL_LOG_FILE", nonexistent):
            resp = auth_client.get("/api/import/log-content")
        data = resp.get_json()
        assert data["exists"] is False

    def test_returns_content_from_offset_zero(self, auth_client, tmp_path):
        log_file = tmp_path / "import.log"
        log_file.write_text("INFO: starting import\nINFO: done")
        with mock.patch.object(ImportService, "MANUAL_LOG_FILE", str(log_file)):
            resp = auth_client.get("/api/import/log-content?offset=0")
        data = resp.get_json()
        assert data["exists"] is True
        assert "starting import" in data["content"]

    def test_handles_offset_beyond_file_size(self, auth_client, tmp_path):
        log_file = tmp_path / "import.log"
        log_file.write_text("short")
        with mock.patch.object(ImportService, "MANUAL_LOG_FILE", str(log_file)):
            resp = auth_client.get("/api/import/log-content?offset=99999")
        data = resp.get_json()
        assert data["exists"] is True


# ===========================================================================
# ASSET OPTIONS
# ===========================================================================

class TestGetOptions:

    def test_returns_list_of_options(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_all",
                        return_value=[MOCK_HIT]):
            resp = auth_client.get("/api/options/de_asset_teams")
        data = resp.get_json()
        assert resp.status_code == 200
        assert len(data) == 1
        assert data[0]["id"] == ITEM_ID

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/options/de_asset_teams")
        assert resp.status_code == 401


class TestGetFilteredOptions:

    def test_returns_filtered_list(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_all",
                        return_value=[MOCK_HIT]):
            resp = auth_client.get(
                "/api/options/de_asset_teams/filtered?filter_field=organization_esid&filter_value=org1"
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_returns_all_when_no_filter(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_all",
                        return_value=[MOCK_HIT]):
            resp = auth_client.get("/api/options/de_asset_teams/filtered")
        assert resp.status_code == 200


# ===========================================================================
# TREE EXPLORER API
# ===========================================================================

class TestGetTreeStructure:

    def test_returns_structure_with_categories(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.count", return_value=5):
            resp = auth_client.get("/api/tree/structure")
        data = resp.get_json()
        assert resp.status_code == 200
        assert "categories" in data
        assert len(data["categories"]) > 0

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/tree/structure")
        assert resp.status_code == 401


class TestGetAssetList:

    def test_returns_items_for_valid_type(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_all",
                        return_value=[MOCK_HIT]):
            resp = auth_client.get(f"/api/tree/assets/{VALID_ASSET_TYPE}")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["asset_type"] == VALID_ASSET_TYPE

    def test_returns_404_for_invalid_type(self, auth_client):
        resp = auth_client.get("/api/tree/assets/nonexistent_type")
        assert resp.status_code == 404


# ===========================================================================
# ASSET DETAILS & SEARCH
# ===========================================================================

class TestGetAssetDetails:

    def test_returns_item_details_for_valid_id(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_by_id",
                        return_value={"team_title": "Test Team"}), \
             mock.patch("asset_management.web.app.FormService.prepare_for_display",
                        return_value={"team_title": "Test Team", "_id": ITEM_ID}), \
             mock.patch("asset_management.web.app.FormService.get_computed_display_fields",
                        return_value={}):
            resp = auth_client.get(f"/api/asset/{VALID_ASSET_TYPE}/{ITEM_ID}/details")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == ITEM_ID

    def test_returns_404_for_unknown_asset_type(self, auth_client):
        resp = auth_client.get("/api/asset/nonexistent/abc123/details")
        assert resp.status_code == 404

    def test_returns_404_when_item_not_found(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=None):
            resp = auth_client.get(f"/api/asset/{VALID_ASSET_TYPE}/missing-id/details")
        assert resp.status_code == 404


class TestGetDashboardSummary:

    def test_returns_totals(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.count", return_value=3):
            resp = auth_client.get("/api/dashboard/summary")
        data = resp.get_json()
        assert resp.status_code == 200
        assert "total_assets" in data
        assert "by_category" in data
        assert "by_type" in data


class TestSearchAssets:

    def test_returns_400_when_query_missing(self, auth_client):
        resp = auth_client.get("/api/search")
        assert resp.status_code == 400

    def test_returns_empty_results_for_no_matches(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_all", return_value=[]):
            resp = auth_client.get("/api/search?q=nonexistent")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["query"] == "nonexistent"
        assert data["results"] == {}

    def test_finds_matching_asset(self, auth_client):
        # Phase C added per-asset workspace scoping to /api/search;
        # the test environment has no ES, so the scope-resolution methods
        # return empty by default. Mock current_user_has_any_scope=True +
        # should_apply_filter=False so the route runs the earlier
        # no-filter behavior on the supplied hits (the unit under test
        # is wildcard matching, not the scope filter).
        hit = {"_id": ITEM_ID, "_source": {"team_title": "Alpha Team"}}
        with mock.patch("asset_management.web.app.AssetService.get_all", return_value=[hit]), mock.patch(
            "asset_management.web.app.RoleFilterService.current_user_has_any_scope",
            return_value=True,
        ), mock.patch(
            "asset_management.web.app.RoleFilterService.should_apply_filter",
            return_value=False,
        ):
            resp = auth_client.get("/api/search?q=Alpha*")
        data = resp.get_json()
        assert resp.status_code == 200
        # teams should be in results since "Alpha Team" matches "Alpha*"
        assert "teams" in data["results"]

    def test_wildcard_match_is_case_insensitive(self, auth_client):
        hit = {"_id": ITEM_ID, "_source": {"team_title": "Alpha Team"}}
        with mock.patch("asset_management.web.app.AssetService.get_all", return_value=[hit]), mock.patch(
            "asset_management.web.app.RoleFilterService.current_user_has_any_scope",
            return_value=True,
        ), mock.patch(
            "asset_management.web.app.RoleFilterService.should_apply_filter",
            return_value=False,
        ):
            resp = auth_client.get("/api/search?q=alpha*")
        data = resp.get_json()
        # case-insensitive match should still work
        assert "teams" in data["results"]


# ===========================================================================
# BACKUP ROUTES
# ===========================================================================

class TestBackupCreate:

    def test_creates_backup_for_admin(self, auth_client):
        result = {"filename": "asset-backup-20240115-120000.tar.gz", "pruned": []}
        with mock.patch("asset_management.web.app.BackupService.create_backup",
                        return_value=result):
            resp = auth_client.post("/api/backup/create")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True
        assert data["filename"] == "asset-backup-20240115-120000.tar.gz"

    def test_creates_backup_for_superuser(self, superuser_client):
        result = {"filename": "asset-backup-20240115-120000.tar.gz", "pruned": []}
        with mock.patch("asset_management.web.app.BackupService.create_backup",
                        return_value=result):
            resp = superuser_client.post("/api/backup/create")
        assert resp.status_code == 200

    def test_returns_403_for_org_user(self, org_user_client):
        resp = org_user_client.post("/api/backup/create")
        assert resp.status_code == 403

    def test_returns_500_on_backup_error(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.create_backup",
                        side_effect=Exception("disk full")):
            resp = auth_client.post("/api/backup/create")
        assert resp.status_code == 500

    def test_unauthenticated_redirects(self, client):
        resp = client.post("/api/backup/create")
        assert resp.status_code == 401


class TestBackupList:

    def test_lists_backups_for_admin(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.list_backups",
                        return_value=[{"filename": "backup.json.gz", "size": 1024}]):
            resp = auth_client.get("/api/backup/list")
        data = resp.get_json()
        assert resp.status_code == 200
        assert len(data["backups"]) == 1

    def test_returns_403_for_org_user(self, org_user_client):
        resp = org_user_client.get("/api/backup/list")
        assert resp.status_code == 403

    def test_returns_500_on_error(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.list_backups",
                        side_effect=Exception("io error")):
            resp = auth_client.get("/api/backup/list")
        assert resp.status_code == 500


class TestBackupRestore:

    def test_restores_backup_for_admin(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.restore_backup",
                        return_value={"restored": 42, "indices": ["de_asset_teams"]}):
            resp = auth_client.post("/api/backup/restore",
                                    json={"filename": "backup_2024-01-15.json.gz"})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True

    def test_returns_400_when_filename_missing(self, auth_client):
        resp = auth_client.post("/api/backup/restore", json={})
        assert resp.status_code == 400

    def test_returns_403_for_org_user(self, org_user_client):
        resp = org_user_client.post("/api/backup/restore",
                                    json={"filename": "backup.json.gz"})
        assert resp.status_code == 403

    def test_returns_400_on_value_error(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.restore_backup",
                        side_effect=InvalidRequestError("invalid file")):
            resp = auth_client.post("/api/backup/restore",
                                    json={"filename": "bad.json.gz"})
        assert resp.status_code == 400

    def test_returns_400_on_file_not_found(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.restore_backup",
                        side_effect=RequestedItemNotFoundError("missing")):
            resp = auth_client.post("/api/backup/restore",
                                    json={"filename": "missing.json.gz"})
        assert resp.status_code == 400

    def test_returns_500_on_unexpected_error(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.restore_backup",
                        side_effect=Exception("db error")):
            resp = auth_client.post("/api/backup/restore",
                                    json={"filename": "backup.json.gz"})
        assert resp.status_code == 500
