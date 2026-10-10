"""
Tests for status, index, keep-alive, and debug routes.

Routes covered:
  GET  /status-reports        status_reports
  GET  /                      index
  GET  /debug/expire-session  debug_expire_session
  GET  /static/<filename>     serve_static
  GET  /dashboards/login      dashboards_login_page
  POST /dashboards/login      dashboards_login_post
  GET  /api/options/<index>   get_options
  GET  /api/options/<index>/filtered  get_filtered_options
  GET  /api/tree/structure    get_tree_structure
  GET  /api/tree/assets/<type>  get_asset_list
  GET  /api/asset/<type>/<id>/details  get_asset_details
  GET  /api/dashboard/summary  get_dashboard_summary
  GET  /api/search             search
  GET  /testing                testing_page
  POST /api/assets/import      import_assets_csv

Note: /landing, /pricing, /features/<slug> moved to the static marketing
site (see the private docs) — no longer tested here.

All external service calls are mocked.
"""

import io
import time
from unittest import mock

import pytest

# get_options / get_filtered_options tests target the now-scoped
# /api/options routes; neutralize those gates (covered by
# the release-plan tests).
pytestmark = pytest.mark.usefixtures("allow_company_tier_and_options_scope")


# ===========================================================================
# GET /  — index
# ===========================================================================


class TestIndexPage:
    """Tests for the main index/dashboard page."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_returns_200_for_authenticated(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200

    def test_returns_200_for_superuser(self, superuser_client):
        resp = superuser_client.get("/")
        assert resp.status_code == 200

    def test_org_user_redirected(self, org_user_client):
        """Org users without asset_admin role are redirected from the index page."""
        resp = org_user_client.get("/")
        assert resp.status_code == 302


# ===========================================================================
# GET /api/documentation-permissions  — api_documentation_permissions
# ===========================================================================


class TestDocumentationPermissions:
    """Tests for the docs nav-filter permissions endpoint."""

    def test_unauthenticated_returns_401_or_redirect(self, client):
        resp = client.get("/api/documentation-permissions")
        assert resp.status_code in (302, 401)

    def test_asset_admin_sees_first_three_sections(self, auth_client):
        resp = auth_client.get("/api/documentation-permissions")
        assert resp.status_code == 200
        sections = resp.get_json()["sections"]
        assert sections["guide-dashboards"] is True
        assert sections["guide-data-explorer"] is True
        assert sections["guide-management-console"] is True
        assert sections["self-hosted"] is False

    def test_superuser_sees_all_sections(self, superuser_client):
        resp = superuser_client.get("/api/documentation-permissions")
        assert resp.status_code == 200
        sections = resp.get_json()["sections"]
        assert sections["guide-dashboards"] is True
        assert sections["guide-data-explorer"] is True
        assert sections["guide-management-console"] is True
        assert sections["self-hosted"] is True

    def test_regular_org_user_sees_dashboards_only(self, org_user_client):
        """Regular (non-admin) users only see the Dashboards section."""
        resp = org_user_client.get("/api/documentation-permissions")
        assert resp.status_code == 200
        sections = resp.get_json()["sections"]
        assert sections["guide-dashboards"] is True
        assert sections["guide-data-explorer"] is False
        assert sections["guide-management-console"] is False
        assert sections["self-hosted"] is False


# ===========================================================================
# GET /debug/expire-session  — debug_expire_session
# ===========================================================================


class TestDebugExpireSession:
    """Tests for the debug session expiry endpoint."""

    def test_unauthenticated_returns_error(self, client):
        # No active session — should respond with an error or redirect
        resp = client.get("/debug/expire-session")
        # Either 302 redirect to login, or 400 "No active session"
        assert resp.status_code in (302, 400, 404)

    def test_returns_404_in_production(self, auth_client, app):
        """debug endpoint is gated behind debug/development mode."""
        app.debug = False
        resp = auth_client.get("/debug/expire-session")
        assert resp.status_code == 404
        app.debug = False

    def test_authenticated_expires_session(self, auth_client, app):
        app.debug = True
        resp = auth_client.get("/debug/expire-session")
        assert resp.status_code == 200
        assert b"Session marked as expired" in resp.data
        app.debug = False

    def test_response_contains_navigation_link(self, auth_client, app):
        app.debug = True
        resp = auth_client.get("/debug/expire-session")
        assert resp.status_code == 200
        assert b"Go to asset explorer" in resp.data
        app.debug = False

    def test_superuser_can_expire_session(self, superuser_client, app):
        app.debug = True
        resp = superuser_client.get("/debug/expire-session")
        assert resp.status_code == 200
        assert b"Session marked as expired" in resp.data
        app.debug = False


# ===========================================================================
# GET /status-reports  — status_reports
# ===========================================================================


class TestStatusReports:
    """Tests for the status reports page."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/status-reports")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    @mock.patch("asset_management.web.app.LogService.get_import_summary", return_value=[])
    @mock.patch("asset_management.web.app.LogService.read_log_file", return_value="some log content")
    @mock.patch(
        "asset_management.web.app.LogService.get_most_recent_log_errors",
        return_value={"filename": "import_20250101.log", "errors": []},
    )
    def test_returns_200_with_log(self, mock_errors, mock_read, mock_summary, auth_client):
        resp = auth_client.get("/status-reports")
        assert resp.status_code == 200
        mock_errors.assert_called_once()

    @mock.patch(
        "asset_management.web.app.LogService.get_most_recent_log_errors",
        return_value={"filename": None, "errors": []},
    )
    def test_returns_200_without_log(self, mock_errors, auth_client):
        resp = auth_client.get("/status-reports")
        assert resp.status_code == 200

    @mock.patch(
        "asset_management.web.app.LogService.get_most_recent_log_errors",
        side_effect=Exception("ES down"),
    )
    def test_returns_500_on_error(self, mock_errors, auth_client):
        resp = auth_client.get("/status-reports")
        assert resp.status_code == 500

    @mock.patch(
        "asset_management.web.app.LogService.get_most_recent_log_errors",
        return_value={"filename": "log.log", "errors": ["ERROR: something"]},
    )
    @mock.patch("asset_management.web.app.LogService.read_log_file", side_effect=Exception("read fail"))
    def test_returns_200_when_log_read_fails(self, mock_read, mock_errors, auth_client):
        resp = auth_client.get("/status-reports")
        assert resp.status_code == 200

    @mock.patch("asset_management.web.app.LogService.get_import_summary_for_date", return_value=[{"name": "Facebook Ads", "records": 10, "errors": 0, "ran": True}])
    @mock.patch(
        "asset_management.web.app.LogService.get_most_recent_log_errors",
        return_value={"filename": "imports_20250101.log", "date": "2025-01-01", "errors": ["ERROR: test"], "has_errors": True},
    )
    def test_passes_summary_to_template(self, mock_errors, mock_summary, auth_client):
        resp = auth_client.get("/status-reports")
        assert resp.status_code == 200
        mock_summary.assert_called_once_with("2025-01-01", "imports_20250101.log")


# ===========================================================================
# GET /dashboards/login — legacy redirect to /login
# ===========================================================================


class TestDashboardsLoginRedirect:
    """/dashboards/login is a permanent redirect to /login.

    The behaviour previously covered here (form rendering, query-param
    handling, error/message flash) now lives entirely on /login. These tests
    pin the redirect itself."""

    def test_unauthenticated_get_returns_301_to_login(self, client):
        with mock.patch("asset_management.web.app.verify_auth", return_value=None):
            resp = client.get("/dashboards/login")
        assert resp.status_code == 301
        assert resp.headers["Location"].startswith("/login")

    def test_authenticated_get_returns_301_to_login(self, auth_client):
        resp = auth_client.get("/dashboards/login")
        assert resp.status_code == 301
        assert resp.headers["Location"].startswith("/login")

    def test_query_string_preserved(self, client):
        with mock.patch("asset_management.web.app.verify_auth", return_value=None):
            resp = client.get("/dashboards/login?next=/dashboards/custom&error=Bad+credentials")
        assert resp.status_code == 301
        loc = resp.headers["Location"]
        assert "next=" in loc
        assert "error=" in loc


# ===========================================================================
# GET /api/options/<index_name>  — get_options
# ===========================================================================


class TestGetOptions:
    """Tests for the options API endpoint."""

    @mock.patch("asset_management.web.app.AssetService.get_all")
    def test_returns_options(self, mock_get_all, auth_client):
        mock_get_all.return_value = [
            {"_id": "id1", "_source": {"title": "Org A"}},
            {"_id": "id2", "_source": {"title": "Org B"}},
        ]
        resp = auth_client.get("/api/options/de_asset_organization")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2
        assert data[0]["id"] == "id1"
        assert data[0]["source"]["title"] == "Org A"

    @mock.patch("asset_management.web.app.AssetService.get_all", return_value=[])
    def test_returns_empty_list(self, mock_get_all, auth_client):
        resp = auth_client.get("/api/options/de_asset_organization")
        assert resp.status_code == 200
        assert resp.get_json() == []


# ===========================================================================
# GET /api/options/<index_name>/filtered  — get_filtered_options
# ===========================================================================


class TestGetFilteredOptions:
    """Tests for the filtered options API endpoint."""

    @mock.patch("asset_management.web.app.AssetService.get_all")
    def test_returns_filtered_options(self, mock_get_all, auth_client):
        mock_get_all.return_value = [
            {"_id": "id1", "_source": {"team_title": "Team A"}},
        ]
        resp = auth_client.get("/api/options/de_asset_teams/filtered?filter_field=organization_esid&filter_value=org1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1

    @mock.patch("asset_management.web.app.AssetService.get_all")
    def test_no_filter_params(self, mock_get_all, auth_client):
        mock_get_all.return_value = []
        resp = auth_client.get("/api/options/de_asset_teams/filtered")
        assert resp.status_code == 200
        # Should call get_all with query=None
        mock_get_all.assert_called_once_with("de_asset_teams", None)


# ===========================================================================
# GET /api/tree/structure  — get_tree_structure
# ===========================================================================


class TestGetTreeStructure:
    """Tests for the tree structure API endpoint."""

    @mock.patch("asset_management.web.app.AssetService.count", return_value=5)
    def test_returns_structure(self, mock_count, auth_client):
        resp = auth_client.get("/api/tree/structure")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "categories" in data
        assert isinstance(data["categories"], list)

    @mock.patch("asset_management.web.app.AssetService.count", return_value=0)
    def test_returns_structure_with_zero_counts(self, mock_count, auth_client):
        resp = auth_client.get("/api/tree/structure")
        assert resp.status_code == 200
        data = resp.get_json()
        for cat in data["categories"]:
            for at in cat["asset_types"]:
                assert at["count"] == 0


# ===========================================================================
# GET /api/tree/assets/<asset_type>  — get_asset_list
# ===========================================================================


class TestGetAssetList:
    """Tests for the tree asset list API endpoint."""

    @mock.patch("asset_management.web.app.AssetService.get_all")
    @mock.patch(
        "asset_management.web.app.RoleFilterService.get_allowed_teams_for_org_users",
        return_value=["Team A", "Team B"],
    )
    @mock.patch(
        # Phase B.4: deny-all gate runs BEFORE the narrowing
        # branch in get_asset_list. Without this stub the resolver tries
        # to reach ES (unavailable in this test), returns is_empty, and
        # the route returns []. Stub True so the test's subject (the
        # narrowing-then-shape behavior) runs.
        "asset_management.web.app.RoleFilterService.current_user_has_any_scope",
        return_value=True,
    )
    @mock.patch(
        "asset_management.web.app.RoleFilterService.should_apply_filter",
        return_value=True,
    )
    def test_returns_asset_list(self, _apply_filter, _has_scope, _mock_allowed, mock_get_all, auth_client):
        """(2026-05-15): asset_admin is now scope-filtered on
        ``teams`` and ``organizations``. To exercise the happy-path
        list-returning behavior, the test must mock the allowed-teams
        set to include the two test teams. Without the mock, the bare
        asset_admin's empty membership/grant set would filter them all
        out (which is the correct behavior, but tested elsewhere in
        EXPLORER-001).
        """
        mock_get_all.return_value = [
            {"_id": "id1", "_source": {"team_title": "Team A"}},
            {"_id": "id2", "_source": {"team_title": "Team B"}},
        ]
        resp = auth_client.get("/api/tree/assets/teams")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["asset_type"] == "teams"
        assert len(data["items"]) == 2

    def test_returns_404_for_invalid_type(self, auth_client):
        resp = auth_client.get("/api/tree/assets/nonexistent")
        assert resp.status_code == 404

    @mock.patch("asset_management.web.app.AssetService.get_all", return_value=[])
    def test_returns_empty_list(self, mock_get_all, auth_client):
        resp = auth_client.get("/api/tree/assets/teams")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["items"] == []

    @mock.patch("asset_management.web.app.AssetService.get_all")
    @mock.patch(
        "asset_management.web.app.RoleFilterService.get_allowed_teams_for_org_users",
        return_value=["Alpha", "Zebra"],
    )
    @mock.patch(
        # Phase B.4: deny-all gate runs BEFORE the narrowing
        # branch in get_asset_list. Stub True so the test's subject (the
        # sort ordering after narrowing) runs.
        "asset_management.web.app.RoleFilterService.current_user_has_any_scope",
        return_value=True,
    )
    @mock.patch(
        "asset_management.web.app.RoleFilterService.should_apply_filter",
        return_value=True,
    )
    def test_items_sorted_alphabetically(self, _apply_filter, _has_scope, _mock_allowed, mock_get_all, auth_client):
        """same scope-filter caveat as test_returns_asset_list —
        the mock includes both teams in the allowed set so the sort
        ordering can be observed.
        """
        mock_get_all.return_value = [
            {"_id": "id2", "_source": {"team_title": "Zebra"}},
            {"_id": "id1", "_source": {"team_title": "Alpha"}},
        ]
        resp = auth_client.get("/api/tree/assets/teams")
        assert resp.status_code == 200
        items = resp.get_json()["items"]
        assert items[0]["title"] == "Alpha"
        assert items[1]["title"] == "Zebra"


# ===========================================================================
# POST /api/assets/import  — import_assets_csv
# ===========================================================================


class TestImportAssetsCsv:
    """Tests for the CSV asset import endpoint."""

    def test_unauthenticated_redirects(self, client):
        resp = client.post("/api/assets/import")
        assert resp.status_code == 401

    def test_missing_asset_type(self, auth_client):
        data = {"file": (io.BytesIO(b"col1,col2\na,b"), "test.csv")}
        resp = auth_client.post("/api/assets/import", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "No asset_type specified"

    def test_invalid_asset_type(self, auth_client):
        data = {"asset_type": "nonexistent", "file": (io.BytesIO(b"col1,col2\na,b"), "test.csv")}
        resp = auth_client.post("/api/assets/import", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "Invalid asset_type" in resp.get_json()["error"]

    def test_no_file_uploaded(self, auth_client):
        resp = auth_client.post("/api/assets/import", data={"asset_type": "teams"}, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "No file uploaded" in resp.get_json()["error"]

    def test_empty_filename(self, auth_client):
        data = {"asset_type": "teams", "file": (io.BytesIO(b"col1\na"), "")}
        resp = auth_client.post("/api/assets/import", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_non_csv_file(self, auth_client):
        data = {"asset_type": "teams", "file": (io.BytesIO(b"data"), "test.xlsx")}
        resp = auth_client.post("/api/assets/import", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "CSV" in resp.get_json()["error"]

    @mock.patch("asset_management.web.app.AssetImportService.import_from_csv")
    def test_successful_import(self, mock_import, auth_client):
        mock_import.return_value = {"success": True, "imported_count": 2, "skipped_count": 0, "error_count": 0}
        # Must use a valid asset_type from AssetImportService.ASSET_IMPORT_CONFIGS
        with mock.patch.dict("asset_management.web.app.AssetImportService.ASSET_IMPORT_CONFIGS", {"teams": {}}):
            data = {"asset_type": "teams", "file": (io.BytesIO(b"team_title\nTeam A\nTeam B"), "teams.csv")}
            resp = auth_client.post("/api/assets/import", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @mock.patch("asset_management.web.app.AssetImportService.import_from_csv", side_effect=Exception("import error"))
    def test_import_exception(self, mock_import, auth_client):
        with mock.patch.dict("asset_management.web.app.AssetImportService.ASSET_IMPORT_CONFIGS", {"teams": {}}):
            data = {"asset_type": "teams", "file": (io.BytesIO(b"team_title\nTeam A"), "teams.csv")}
            resp = auth_client.post("/api/assets/import", data=data, content_type="multipart/form-data")
        assert resp.status_code == 500


# ===========================================================================
# GET /testing  — testing_page
# ===========================================================================


class TestTestingPage:
    """Tests for the testing harness page."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/testing")
        assert resp.status_code == 302

    def test_non_admin_redirected(self, org_user_client):
        resp = org_user_client.get("/testing")
        assert resp.status_code == 302

    def test_admin_returns_200(self, auth_client):
        resp = auth_client.get("/testing")
        assert resp.status_code == 200

    def test_superuser_returns_200(self, superuser_client):
        resp = superuser_client.get("/testing")
        assert resp.status_code == 200


# ===========================================================================
# Context processor and template filter
# ===========================================================================


class TestContextProcessor:
    """Tests for inject_user and first_role_display."""

    def test_index_page_renders_with_context_variables(self, auth_client):
        # was test_index_page_has_session_lifetime; session_lifetime
        # was removed from inject_user. The route still must render 200.
        resp = auth_client.get("/")
        assert resp.status_code == 200

    def test_unauthenticated_context(self, client):
        # /dashboards/login → /login. Follow the chain so we still
        # exercise the unauthenticated-context render path that this test
        # was originally about.
        with mock.patch("asset_management.web.app.verify_auth", return_value=None):
            resp = client.get("/dashboards/login", follow_redirects=True)
        assert resp.status_code == 200
