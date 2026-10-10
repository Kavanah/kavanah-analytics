"""
Tests for remaining uncovered lines in app.py.

Covers:
  - _handle_logo_upload helper (direct invocation with mock file uploads)
  - _delete_logo_file helper (direct invocation)
  - first_role_display template filter
  - inject_user context processor
  - logout() route edge cases
  - create_kibana_session() (direct invocation with mocked requests.post)
  - Asset add/edit edge cases (ga_accounts, ga_properties auto-generation, create failure)
  - Bulk delete with dashboard Kibana cleanup
  - get_dashboard_info exception path
  - resolve_data_view_index (direct invocation)
  - _get_platform_index_map (direct invocation with caching)
  - _get_dashboard_index_map (direct invocation with caching)
  - _get_team_titles_for_org (direct invocation)
  - get_platform_availability route edge cases
  - get_data_pulled_as_of route edge cases
  - get_dashboard_filter_options route edge cases (partnership filters via ES aggs)
"""

import io
import os
import time
from unittest import mock

import pytest

# bulk-delete tests here target the company-tier (teams) route that now
# enforces a per-object scope check; neutralize it (gate covered by
# the release-plan tests).
pytestmark = pytest.mark.usefixtures("allow_company_tier_and_options_scope")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ITEM_ID = "test-item-123"


def _seed_session(client, **overrides):
    """Seed an authenticated session with optional overrides."""
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["username"] = "testuser"
        sess["roles"] = ["asset_admin"]
        sess["is_admin"] = True
        sess["full_name"] = "Test User"
        sess["email"] = "test@example.com"
        sess["auth_timestamp"] = time.time()
        sess["csrf_token"] = "test-csrf"
        sess.update(overrides)


# ===========================================================================
# _handle_logo_upload  (lines 132-154)
# ===========================================================================


class TestHandleLogoUploadDirect:
    """Direct invocation of _handle_logo_upload with real Flask request context."""

    def test_remove_flag_returns_none(self, app):
        from asset_management.web.app import _handle_logo_upload

        with app.test_request_context(
            method="POST",
            data={"logo_remove": "1"},
            content_type="multipart/form-data",
        ):
            result, error = _handle_logo_upload("logo")
        assert result is None
        assert error is None

    def test_no_file_returns_false(self, app):
        from asset_management.web.app import _handle_logo_upload

        with app.test_request_context(
            method="POST",
            data={},
            content_type="multipart/form-data",
        ):
            result, error = _handle_logo_upload("logo")
        assert result is False
        assert error is None

    def test_empty_filename_returns_false(self, app):
        from asset_management.web.app import _handle_logo_upload

        with app.test_request_context(
            method="POST",
            data={"logo_file": (io.BytesIO(b""), "")},
            content_type="multipart/form-data",
        ):
            result, error = _handle_logo_upload("logo")
        assert result is False
        assert error is None

    def test_disallowed_extension_returns_error(self, app):
        from asset_management.web.app import _handle_logo_upload

        with app.test_request_context(
            method="POST",
            data={"logo_file": (io.BytesIO(b"GIF89a"), "logo.gif")},
            content_type="multipart/form-data",
        ):
            result, error = _handle_logo_upload("logo")
        assert result is False
        assert "JPG or PNG" in error

    def test_file_too_large_returns_error(self, app):
        from asset_management.web.app import _handle_logo_upload

        large_data = b"x" * (3 * 1024 * 1024)  # 3 MB
        with app.test_request_context(
            method="POST",
            data={"logo_file": (io.BytesIO(large_data), "big.png")},
            content_type="multipart/form-data",
        ):
            result, error = _handle_logo_upload("logo")
        assert result is False
        assert "2 MB" in error

    @mock.patch("asset_management.web.app.os.makedirs")
    @mock.patch("builtins.open", mock.mock_open())
    def test_valid_upload_saves_file(self, mock_makedirs, app):
        from asset_management.web.app import _handle_logo_upload

        png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        with app.test_request_context(
            method="POST",
            data={"logo_file": (io.BytesIO(png_data), "logo.png")},
            content_type="multipart/form-data",
        ):
            result, error = _handle_logo_upload("logo")
        assert error is None
        assert result.startswith("uploads/logos/logo_")
        assert result.endswith(".png")
        mock_makedirs.assert_called_once()

    @mock.patch("asset_management.web.app.os.makedirs")
    @mock.patch("builtins.open", mock.mock_open())
    def test_valid_jpg_upload(self, mock_makedirs, app):
        from asset_management.web.app import _handle_logo_upload

        jpg_data = b"\xff\xd8\xff" + b"\x00" * 50
        with app.test_request_context(
            method="POST",
            data={"logo_file": (io.BytesIO(jpg_data), "photo.jpg")},
            content_type="multipart/form-data",
        ):
            result, error = _handle_logo_upload("logo")
        assert error is None
        assert result.endswith(".jpg")

    @mock.patch("asset_management.web.app.os.makedirs")
    @mock.patch("builtins.open", mock.mock_open())
    def test_valid_jpeg_upload(self, mock_makedirs, app):
        from asset_management.web.app import _handle_logo_upload

        data = b"\xff\xd8\xff" + b"\x00" * 50
        with app.test_request_context(
            method="POST",
            data={"logo_file": (io.BytesIO(data), "photo.jpeg")},
            content_type="multipart/form-data",
        ):
            result, error = _handle_logo_upload("logo")
        assert error is None
        assert result.endswith(".jpeg")


# ===========================================================================
# _delete_logo_file  (lines 157-165)
# ===========================================================================


class TestDeleteLogoFileDirect:
    """Direct invocation of _delete_logo_file."""

    def test_none_path_is_noop(self):
        from asset_management.web.app import _delete_logo_file

        # Should not raise
        _delete_logo_file(None)

    def test_empty_path_is_noop(self):
        from asset_management.web.app import _delete_logo_file

        _delete_logo_file("")

    @mock.patch("asset_management.web.app.os.remove")
    def test_existing_file_is_deleted(self, mock_remove):
        from asset_management.web.app import _delete_logo_file

        _delete_logo_file("uploads/logos/old.jpg")
        mock_remove.assert_called_once()
        call_path = mock_remove.call_args[0][0]
        assert "uploads" in call_path and "old.jpg" in call_path

    @mock.patch("asset_management.web.app.os.remove", side_effect=OSError("not found"))
    def test_missing_file_silently_ignored(self, mock_remove):
        from asset_management.web.app import _delete_logo_file

        # Should not raise
        _delete_logo_file("uploads/logos/missing.png")


# ===========================================================================
# first_role_display template filter  (lines 273-337)
# ===========================================================================


class TestFirstRoleDisplay:
    """Tests for the first_role_display template filter."""

    def test_empty_roles_returns_empty(self, app):
        from asset_management.web.app import first_role_display

        assert first_role_display([]) == ""
        assert first_role_display(None) == ""

    def test_superuser_returns_superuser(self, app):
        from asset_management.web.app import first_role_display

        assert first_role_display(["superuser", "org_abc"]) == "superuser"

    @mock.patch("services.role_filter_service.RoleFilterService.get_org_descriptions_by_ids", return_value=["Acme Corp"])
    def test_org_role_resolves_description(self, mock_desc, app):
        from asset_management.web.app import first_role_display

        result = first_role_display(["org_abc123"])
        assert result == "Acme Corp"
        mock_desc.assert_called_once_with(["abc123"])

    @mock.patch("services.role_filter_service.RoleFilterService.get_org_descriptions_by_ids", return_value=[])
    def test_org_role_fallback_to_id(self, mock_desc, app):
        from asset_management.web.app import first_role_display

        result = first_role_display(["org_xyz"])
        assert result == "xyz"

    @mock.patch("services.role_filter_service.RoleFilterService.get_org_descriptions_by_ids", side_effect=Exception("ES down"))
    def test_org_role_exception_fallback(self, mock_desc, app):
        from asset_management.web.app import first_role_display

        result = first_role_display(["org_fail"])
        assert result == "fail"

    @mock.patch("services.role_filter_service.RoleFilterService.get_team_titles_by_ids", return_value=["Alpha Team"])
    def test_team_role_resolves_title(self, mock_titles, app):
        from asset_management.web.app import first_role_display

        result = first_role_display(["team_t1"])
        assert result == "Alpha Team"
        mock_titles.assert_called_once_with(["t1"])

    @mock.patch("services.role_filter_service.RoleFilterService.get_team_titles_by_ids", return_value=[])
    def test_team_role_fallback_to_id(self, mock_titles, app):
        from asset_management.web.app import first_role_display

        result = first_role_display(["team_xyz"])
        assert result == "xyz"

    @mock.patch("services.role_filter_service.RoleFilterService.get_team_titles_by_ids", side_effect=Exception("boom"))
    def test_team_role_exception_fallback(self, mock_titles, app):
        from asset_management.web.app import first_role_display

        result = first_role_display(["team_broken"])
        assert result == "broken"

    def test_no_org_or_team_role_returns_first(self, app):
        from asset_management.web.app import first_role_display

        assert first_role_display(["asset_admin"]) == "asset_admin"

    @mock.patch("services.role_filter_service.RoleFilterService.get_org_descriptions_by_ids", return_value=["Org One"])
    def test_org_prioritized_over_team(self, mock_desc, app):
        from asset_management.web.app import first_role_display

        result = first_role_display(["team_t1", "org_o1"])
        assert result == "Org One"


# ===========================================================================
# inject_user context processor  (lines 262-270)
# ===========================================================================


class TestInjectUser:
    """Tests for the inject_user context processor."""

    def test_authenticated_returns_current_user(self, auth_client):
        """When authenticated, current_user is populated."""
        resp = auth_client.get("/")
        assert resp.status_code == 200

    def test_unauthenticated_returns_none_user(self, app):
        """When not authenticated, current_user is None."""
        with app.test_request_context("/"):
            from asset_management.web.app import inject_user
            from flask import session as flask_session

            flask_session.clear()
            ctx = inject_user()
            assert ctx["current_user"] is None
            # session_lifetime was removed from inject_user — the
            # JS countdown that consumed it is gone.
            assert "session_lifetime" not in ctx


# ===========================================================================
# logout() edge cases  (lines 420-428)
# ===========================================================================


class TestLogoutEdgeCases:
    """Tests for logout route edge cases."""

    @mock.patch("asset_management.web.blueprints.analytics.routes.AuditLogService.log_event")
    def test_logout_with_referrer_preserves_path(self, mock_audit, auth_client):
        """When referrer is set, logout extracts path for next_url."""
        resp = auth_client.get(
            "/logout",
            headers={"Referer": "http://localhost/dashboards/some-page"},
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "window.location.replace" in body
        assert "dashboards" in body

    @mock.patch("asset_management.web.blueprints.analytics.routes.AuditLogService.log_event")
    def test_logout_referrer_login_is_ignored(self, mock_audit, auth_client):
        """Referrer pointing to /login or /logout should be skipped."""
        resp = auth_client.get(
            "/logout",
            headers={"Referer": "http://localhost/login"},
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "window.location.replace" in body
        assert "login_page" in body or "/login" in body

    @mock.patch("asset_management.web.blueprints.analytics.routes.AuditLogService.log_event")
    def test_logout_with_next_param(self, mock_audit, auth_client):
        """explicit next= is preserved on the canonical /login URL."""
        resp = auth_client.get("/logout?next=/dashboards/custom")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "window.location.replace" in body
        assert "/login?" in body
        assert "next=%2Fdashboards%2Fcustom" in body

    @mock.patch("asset_management.web.blueprints.analytics.routes.AuditLogService.log_event")
    def test_logout_dashboards_referrer_preserved_as_next_on_canonical_login(self, mock_audit, auth_client):
        """a /dashboards/* referrer becomes the next= so the user
        lands back on the dashboards surface after re-auth — no longer a
        separate /dashboards/login page."""
        resp = auth_client.get(
            "/logout",
            headers={"Referer": "http://localhost/dashboards/analytics"},
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "window.location.replace" in body
        assert "/login?" in body
        assert "/dashboards/login" not in body
        assert "next=%2Fdashboards%2Fanalytics" in body

    @mock.patch("asset_management.web.blueprints.analytics.routes.AuditLogService.log_event")
    def test_logout_no_csrf_token(self, mock_audit, app):
        """Logout without a CSRF token in session does not error."""
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            sess["auth_timestamp"] = time.time()
            # No csrf_token set
        resp = c.get("/logout")
        assert resp.status_code == 200
        assert "window.location.replace" in resp.get_data(as_text=True)

    @mock.patch("asset_management.web.blueprints.analytics.routes.AuditLogService.log_event")
    def test_logout_without_session_does_not_audit(self, mock_audit, client):
        """A logout request with no active session must not write an audit event.

        Reproduces a bug where expired/empty sessions produced audit entries
        with username='unknown' and auth_method='unknown'.
        """
        resp = client.get("/logout")
        assert resp.status_code == 200
        mock_audit.assert_not_called()

    @mock.patch("asset_management.web.blueprints.analytics.routes.AuditLogService.log_event")
    def test_logout_with_session_audits_real_username(self, mock_audit, auth_client):
        """Authenticated logout writes an audit event with the real username."""
        resp = auth_client.get("/logout")
        assert resp.status_code == 200
        mock_audit.assert_called_once()
        args, _ = mock_audit.call_args
        assert args[0] != "unknown"
        assert args[1] == "logout"


# ===========================================================================
# create_kibana_session  (lines 527-567)
# ===========================================================================


class TestCreateKibanaSession:
    """Tests for create_kibana_session (direct invocation)."""

    @mock.patch("requests.post")
    def test_successful_login_returns_sid(self, mock_post, app):
        from asset_management.web.app import create_kibana_session

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.cookies.get.return_value = "sid-cookie-value"
        mock_post.return_value = mock_response

        result = create_kibana_session("admin", "password123")
        assert result == "sid-cookie-value"
        mock_post.assert_called_once()

    @mock.patch("requests.post")
    def test_successful_login_no_sid_cookie(self, mock_post, app):
        from asset_management.web.app import create_kibana_session

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.cookies.get.return_value = None
        mock_post.return_value = mock_response

        result = create_kibana_session("admin", "password123")
        assert result is None

    @mock.patch("requests.post")
    def test_failed_login_returns_none(self, mock_post, app):
        from asset_management.web.app import create_kibana_session

        mock_response = mock.MagicMock()
        mock_response.status_code = 401
        mock_post.return_value = mock_response

        result = create_kibana_session("admin", "wrong")
        assert result is None

    @mock.patch("requests.post", side_effect=ConnectionError("cannot connect"))
    def test_exception_returns_none(self, mock_post, app):
        from asset_management.web.app import create_kibana_session

        result = create_kibana_session("admin", "password123")
        assert result is None

    @mock.patch("requests.post")
    def test_custom_kibana_url_from_env(self, mock_post, app):
        from asset_management.web.app import create_kibana_session

        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.cookies.get.return_value = "sid-123"
        mock_post.return_value = mock_response

        with mock.patch.dict(os.environ, {"KIBANA_URL": "https://custom-kibana.local"}):
            create_kibana_session("user", "pass")

        call_url = mock_post.call_args[0][0]
        assert "custom-kibana.local" in call_url


# ===========================================================================
# Asset add edge cases: ga_accounts, ga_properties auto-generation (lines 970-984)
# ===========================================================================


class TestAssetAddGaAutoGeneration:
    """Tests for auto-generating resource_name fields on asset creation."""

    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "new-id"))
    @mock.patch("asset_management.web.app.FormService.process_form_data")
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_ga_accounts_auto_generates_resource_name(self, mock_val, mock_proc, mock_create, auth_client):
        mock_proc.return_value = {"ga_account_id": "12345"}
        resp = auth_client.post("/asset/ga_accounts/add", data={"ga_account_id": "12345"})
        assert resp.status_code == 302
        created_data = mock_create.call_args[0][1]
        assert created_data["ga_account_resource_name"] == "accounts/12345"

    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "new-id"))
    @mock.patch("asset_management.web.app.FormService.process_form_data")
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_ga_accounts_does_not_overwrite_existing_resource_name(self, mock_val, mock_proc, mock_create, auth_client):
        mock_proc.return_value = {"ga_account_id": "12345", "ga_account_resource_name": "accounts/99"}
        resp = auth_client.post("/asset/ga_accounts/add", data={"ga_account_id": "12345"})
        assert resp.status_code == 302
        created_data = mock_create.call_args[0][1]
        assert created_data["ga_account_resource_name"] == "accounts/99"

    @mock.patch("lib.es_helpers.create_ga_metrics_index_for_team")
    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "new-id"))
    @mock.patch("asset_management.web.app.FormService.process_form_data")
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_ga_properties_auto_generates_resource_name_and_creates_index(
        self, mock_val, mock_proc, mock_create, mock_ga_index, auth_client
    ):
        mock_proc.return_value = {"ga_property_id": "67890", "team_esid": "team-abc"}
        resp = auth_client.post("/asset/ga_properties/add", data={"ga_property_id": "67890"})
        assert resp.status_code == 302
        created_data = mock_create.call_args[0][1]
        assert created_data["ga_property_resource_name"] == "properties/67890"
        mock_ga_index.assert_called_once_with("team-abc")

    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(False, "ES conflict", None))
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "T"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_create_failure_returns_500_error(self, mock_val, mock_proc, mock_create, auth_client):
        resp = auth_client.post("/asset/teams/add", data={"team_title": "T"})
        assert resp.status_code == 500
        assert b"Failed to Add Asset" in resp.data or b"Could not save" in resp.data


# ===========================================================================
# Asset edit edge cases: ga_accounts/properties auto-generation (lines 1107-1127)
# ===========================================================================


class TestAssetEditGaAutoGeneration:
    """Tests for auto-generating resource_name fields on edit and GA index creation."""

    @mock.patch("asset_management.web.app._data_source_asset_in_user_workspace", return_value=True)
    @mock.patch("asset_management.web.app.AssetService.update", return_value=True)
    @mock.patch("asset_management.web.app.FormService.process_form_data")
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch("asset_management.web.app.AssetService.get_by_id")
    def test_edit_ga_accounts_auto_generates_resource_name(
        self, mock_get, mock_val, mock_proc, mock_update, mock_ws_gate, auth_client
    ):
        mock_get.return_value = {"ga_account_id": "111"}
        mock_proc.return_value = {"ga_account_id": "111"}
        resp = auth_client.post(f"/asset/ga_accounts/edit/{ITEM_ID}", data={"ga_account_id": "111"})
        assert resp.status_code == 302
        updated_doc = mock_update.call_args[0][2]
        assert updated_doc["ga_account_resource_name"] == "accounts/111"

    @mock.patch("asset_management.web.app._data_source_asset_in_user_workspace", return_value=True)
    @mock.patch("lib.es_helpers.create_ga_metrics_index_for_team")
    @mock.patch("asset_management.web.app.AssetService.update", return_value=True)
    @mock.patch("asset_management.web.app.FormService.process_form_data")
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch("asset_management.web.app.AssetService.get_by_id")
    def test_edit_ga_properties_creates_index_on_team_change(
        self, mock_get, mock_val, mock_proc, mock_update, mock_ga_index, mock_ws_gate, auth_client
    ):
        mock_get.return_value = {"ga_property_id": "222", "team_esid": "old-team"}
        mock_proc.return_value = {"ga_property_id": "222", "team_esid": "new-team"}
        resp = auth_client.post(f"/asset/ga_properties/edit/{ITEM_ID}", data={"ga_property_id": "222"})
        assert resp.status_code == 302
        mock_ga_index.assert_called_once_with("new-team")

    @mock.patch("asset_management.web.app._data_source_asset_in_user_workspace", return_value=True)
    @mock.patch("lib.es_helpers.create_ga_metrics_index_for_team")
    @mock.patch("asset_management.web.app.AssetService.update", return_value=True)
    @mock.patch("asset_management.web.app.FormService.process_form_data")
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch("asset_management.web.app.AssetService.get_by_id")
    def test_edit_ga_properties_no_index_when_team_unchanged(
        self, mock_get, mock_val, mock_proc, mock_update, mock_ga, mock_ws_gate, auth_client
    ):
        mock_get.return_value = {"ga_property_id": "333", "team_esid": "same-team"}
        mock_proc.return_value = {"ga_property_id": "333", "team_esid": "same-team"}
        resp = auth_client.post(f"/asset/ga_properties/edit/{ITEM_ID}", data={})
        assert resp.status_code == 302
        mock_ga.assert_not_called()


# ===========================================================================
# Bulk delete with dashboard Kibana cleanup (lines 1198-1203)
# ===========================================================================


class TestBulkDeleteEdgeCases:
    """Tests for bulk_delete_assets edge cases."""

    @mock.patch("asset_management.web.app.AssetService.delete_only", return_value=False)
    def test_bulk_delete_failure_tracked(self, mock_delete, auth_client):
        """When AssetService.delete_only returns False, it counts as a failure."""
        resp = auth_client.post(
            "/asset/teams/bulk-delete",
            json={"item_ids": ["id1"]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["failed_count"] == 1

    @mock.patch("asset_management.web.app.AssetService.delete_only", side_effect=Exception("ES error"))
    def test_bulk_delete_exception_per_item(self, mock_delete, auth_client):
        """Exceptions per item are caught and recorded."""
        resp = auth_client.post(
            "/asset/teams/bulk-delete",
            json={"item_ids": ["bad-id"]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["failed_count"] == 1
        assert len(data["failures"]) == 1
        # exception text stays in the server log, never the response.
        assert data["failures"][0]["error"] == "Delete failed"

    @mock.patch("asset_management.web.app.AssetService.delete_only", return_value=True)
    def test_bulk_delete_success_count(self, mock_delete, auth_client):
        resp = auth_client.post(
            "/asset/teams/bulk-delete",
            json={"item_ids": ["id1", "id2"]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["deleted_count"] == 2

    def test_bulk_delete_no_items_returns_400(self, auth_client):
        resp = auth_client.post(
            "/asset/teams/bulk-delete",
            json={"item_ids": []},
        )
        assert resp.status_code == 400

    def test_bulk_delete_invalid_type_returns_404(self, auth_client):
        resp = auth_client.post(
            "/asset/nonexistent/bulk-delete",
            json={"item_ids": ["id1"]},
        )
        assert resp.status_code == 404


# ===========================================================================
# get_dashboard_info exception path (lines 1519-1523)
# ===========================================================================


class TestDashboardInfoException:
    """Test exception handling in get_dashboard_info."""

    @mock.patch("asset_management.web.app.get_dashboard_url", side_effect=RuntimeError("Kibana unreachable"))
    def test_exception_returns_500(self, mock_url, auth_client):
        from asset_management.web.app import DASHBOARD_CONFIGS

        # Pick a real dashboard key if available, otherwise skip
        if not DASHBOARD_CONFIGS:
            pytest.skip("No dashboards configured")
        key = next(iter(DASHBOARD_CONFIGS))
        resp = auth_client.get(f"/api/dashboards/{key}")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False


# ===========================================================================
# resolve_data_view_index (lines 2233-2270)
# ===========================================================================


class TestResolveDataViewIndex:
    """Direct invocation of resolve_data_view_index."""

    def test_unknown_platform_returns_none(self, app):
        from asset_management.web.app import resolve_data_view_index

        assert resolve_data_view_index("NonExistentPlatform") is None

    @mock.patch.dict(os.environ, {"ES01_PASSWORD": ""}, clear=False)
    def test_no_es_password_returns_none(self, app):
        from asset_management.web.app import resolve_data_view_index

        # Temporarily clear the password
        old = os.environ.pop("ES01_PASSWORD", None)
        try:
            result = resolve_data_view_index("Echo")
        finally:
            if old is not None:
                os.environ["ES01_PASSWORD"] = old
        assert result is None

    @mock.patch("requests.get")
    def test_kibana_api_failure_returns_none(self, mock_get, app):
        from asset_management.web.app import resolve_data_view_index

        mock_get.return_value = mock.MagicMock(status_code=500)
        result = resolve_data_view_index("Echo")
        assert result is None

    @mock.patch("requests.get")
    def test_data_view_found(self, mock_get, app):
        from asset_management.web.app import resolve_data_view_index

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data_view": [
                {"name": "Echo Conversations data view", "title": "echo_conversations*"},
                {"name": "Other", "title": "other*"},
            ]
        }
        mock_get.return_value = mock_resp

        result = resolve_data_view_index("Echo")
        assert result == "echo_conversations*"

    @mock.patch("requests.get")
    def test_data_view_not_found_returns_none(self, mock_get, app):
        from asset_management.web.app import resolve_data_view_index

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data_view": [{"name": "Other", "title": "other*"}]}
        mock_get.return_value = mock_resp

        result = resolve_data_view_index("Echo")
        assert result is None

    @mock.patch("requests.get")
    def test_list_platform_resolves_multiple(self, mock_get, app):
        from asset_management.web.app import resolve_data_view_index

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data_view": [
                {"name": "Facebook data view", "title": "fb_ads*"},
                {"name": "Facebook Page Insights data view", "title": "fb_page*"},
                {"name": "Instagram Data View", "title": "ig*"},
            ]
        }
        mock_get.return_value = mock_resp

        result = resolve_data_view_index("Meta")
        assert "fb_ads*" in result
        assert "fb_page*" in result
        assert "ig*" in result
        # comma separated
        assert "," in result


# ===========================================================================
# _get_platform_index_map (lines 2283-2305)
# ===========================================================================


class TestGetPlatformIndexMap:
    """Tests for _get_platform_index_map with caching."""

    @mock.patch("asset_management.web.app.resolve_data_view_index")
    def test_builds_map_from_all_platforms(self, mock_resolve, app):
        import asset_management.web.app as app_mod

        # Clear cache
        app_mod._platform_index_cache = None
        app_mod._platform_index_cache_time = 0

        mock_resolve.side_effect = lambda p: f"idx_{p}" if p == "Echo" else None

        result = app_mod._get_platform_index_map()
        assert "Echo" in result
        assert result["Echo"] == "idx_Echo"

    @mock.patch("asset_management.web.app.resolve_data_view_index")
    def test_cache_hit_does_not_re_resolve(self, mock_resolve, app):
        import asset_management.web.app as app_mod

        app_mod._platform_index_cache = {"Echo": "cached*"}
        app_mod._platform_index_cache_time = time.time()

        result = app_mod._get_platform_index_map()
        assert result == {"Echo": "cached*"}
        mock_resolve.assert_not_called()

        # Cleanup
        app_mod._platform_index_cache = None
        app_mod._platform_index_cache_time = 0


# ===========================================================================
# _get_dashboard_index_map (lines 2308-2353)
# ===========================================================================


class TestGetDashboardIndexMap:
    """Tests for _get_dashboard_index_map with caching."""

    @mock.patch("lib.es_helpers.get_all_documents")
    def test_builds_map_from_echarts_dashboards(self, mock_get_all, app):
        import asset_management.web.app as app_mod

        app_mod._dashboard_index_cache = None
        app_mod._dashboard_index_cache_time = 0

        mock_get_all.return_value = [
            {
                "_source": {
                    "dashboard_id": "dash-1",
                    "echarts_config": {
                        "charts": [
                            {"data_view": "fb_ads*"},
                            {"data_view": "ig*"},
                        ]
                    },
                }
            },
            {
                "_source": {
                    "dashboard_id": "dash-2",
                    "visibility_mode": "hide",
                    "echarts_config": {"charts": [{"data_view": "hidden*"}]},
                }
            },
        ]

        result = app_mod._get_dashboard_index_map()
        assert "dash-1" in result
        assert sorted(result["dash-1"]) == ["fb_ads*", "ig*"]
        assert "dash-2" not in result

        # Cleanup
        app_mod._dashboard_index_cache = None
        app_mod._dashboard_index_cache_time = 0

    @mock.patch("lib.es_helpers.get_all_documents")
    def test_cache_hit_skips_es(self, mock_get_all, app):
        import asset_management.web.app as app_mod

        app_mod._dashboard_index_cache = {"cached": ["idx*"]}
        app_mod._dashboard_index_cache_time = time.time()

        result = app_mod._get_dashboard_index_map()
        assert result == {"cached": ["idx*"]}
        mock_get_all.assert_not_called()

        app_mod._dashboard_index_cache = None
        app_mod._dashboard_index_cache_time = 0

    @mock.patch("lib.es_helpers.get_all_documents", side_effect=Exception("ES down"))
    def test_exception_returns_empty_dict(self, mock_get_all, app):
        import asset_management.web.app as app_mod

        app_mod._dashboard_index_cache = None
        app_mod._dashboard_index_cache_time = 0

        result = app_mod._get_dashboard_index_map()
        assert result == {}


# ===========================================================================
# _get_team_titles_for_org (lines 2356-2410)
# ===========================================================================


class TestGetTeamTitlesForOrg:
    """Tests for _get_team_titles_for_org helper."""

    def _make_es(self, hits):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": h} for h in hits]}
        }
        return mock_es

    def test_all_filter_returns_all_teams(self, app):
        from asset_management.web.app import _get_team_titles_for_org

        es = self._make_es([
            {"team_title": "Team A", "organization_title": ["Org1"]},
            {"team_title": "Team B", "organization_title": ["Org1", "Org2"]},
        ])
        result = _get_team_titles_for_org(es, "Org1", "all")
        assert "Team A" in result
        assert "Team B" in result

    def test_primary_filter_single_org_only(self, app):
        from asset_management.web.app import _get_team_titles_for_org

        es = self._make_es([
            {"team_title": "Single", "organization_title": ["Org1"]},
            {"team_title": "Multi", "organization_title": ["Org1", "Org2"]},
        ])
        result = _get_team_titles_for_org(es, "Org1", "primary")
        assert result == ["Single"]

    def test_partnerships_filter_multi_org_only(self, app):
        from asset_management.web.app import _get_team_titles_for_org

        es = self._make_es([
            {"team_title": "Single", "organization_title": ["Org1"]},
            {"team_title": "Multi", "organization_title": ["Org1", "Org2"]},
        ])
        result = _get_team_titles_for_org(es, "Org1", "partnerships")
        assert result == ["Multi"]

    def test_comma_separated_string_is_multi(self, app):
        from asset_management.web.app import _get_team_titles_for_org

        es = self._make_es([
            {"team_title": "CSV Team", "organization_title": "Org1, Org2"},
        ])
        result = _get_team_titles_for_org(es, "Org1", "partnerships")
        assert result == ["CSV Team"]

    def test_single_string_is_single_org(self, app):
        from asset_management.web.app import _get_team_titles_for_org

        es = self._make_es([
            {"team_title": "Str Team", "organization_title": "Org1"},
        ])
        result = _get_team_titles_for_org(es, "Org1", "primary")
        assert result == ["Str Team"]

    def test_empty_filter_type_returns_all(self, app):
        from asset_management.web.app import _get_team_titles_for_org

        es = self._make_es([
            {"team_title": "Any", "organization_title": "Org1"},
        ])
        result = _get_team_titles_for_org(es, "Org1", "")
        assert result == ["Any"]

    def test_missing_team_title_skipped(self, app):
        from asset_management.web.app import _get_team_titles_for_org

        es = self._make_es([
            {"organization_title": "Org1"},
        ])
        result = _get_team_titles_for_org(es, "Org1", "all")
        assert result == []

    def test_exception_returns_empty_list(self, app):
        from asset_management.web.app import _get_team_titles_for_org

        es = mock.MagicMock()
        es.search.side_effect = Exception("boom")
        result = _get_team_titles_for_org(es, "Org1", "all")
        assert result == []

    def test_non_list_non_string_org_title_treated_as_single(self, app):
        from asset_management.web.app import _get_team_titles_for_org

        es = self._make_es([
            {"team_title": "Weird", "organization_title": 123},
        ])
        # org_title is neither list nor str => is_multi = False
        result = _get_team_titles_for_org(es, "Org1", "primary")
        assert result == ["Weird"]


# ===========================================================================
# get_platform_availability route edge cases (lines 2413-2535)
# ===========================================================================


class TestPlatformAvailabilityEdgeCases:
    """Edge case tests for the platform-availability route."""

    @mock.patch("asset_management.web.app._get_dashboard_index_map", return_value={})
    @mock.patch("asset_management.web.app._get_platform_index_map", return_value={})
    @mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None)
    def test_es_unavailable_returns_500(self, mock_es, mock_pim, mock_dim, auth_client):
        resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch("asset_management.web.app._get_dashboard_index_map", return_value={})
    @mock.patch("asset_management.web.app._get_platform_index_map", return_value={})
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_team_filter_used_directly(self, mock_es_client, mock_pim, mock_dim, auth_client):
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {"responses": []}
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/platform-availability?team=TeamX")
        assert resp.status_code == 200

    @mock.patch("asset_management.web.app._get_dashboard_index_map", return_value={"dash-1": ["fb*"]})
    @mock.patch("asset_management.web.app._get_platform_index_map", return_value={"Meta": "fb*"})
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_dashboard_level_availability(self, mock_es_client, mock_pim, mock_dim, auth_client):
        """Verifies per-dashboard availability is included in response."""
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {
            "responses": [
                {"hits": {"total": {"value": 5}}},  # platform: Meta
                {"hits": {"total": {"value": 3}}},  # individual: fb*
            ]
        }
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["platforms"]["Meta"]["has_data"] is True
        assert data["dashboards"]["dash-1"]["has_data"] is True

    @mock.patch("asset_management.web.app._get_dashboard_index_map", return_value={})
    @mock.patch("asset_management.web.app._get_platform_index_map")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_exception_returns_500(self, mock_es_client, mock_pim, mock_dim, auth_client):
        mock_es_client.side_effect = Exception("unexpected")
        resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch("asset_management.web.app._get_dashboard_index_map", return_value={})
    @mock.patch("asset_management.web.app._get_platform_index_map", return_value={"Meta": "fb*"})
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_error_in_msearch_response_treated_as_zero(self, mock_es_client, mock_pim, mock_dim, auth_client):
        """When an msearch response has 'error', doc count should be 0."""
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {
            "responses": [
                {"error": {"type": "index_not_found_exception"}},
            ]
        }
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["platforms"]["Meta"]["has_data"] is False


# ===========================================================================
# get_data_pulled_as_of edge cases (lines 2538-2569)
# ===========================================================================


class TestDataPulledAsOfEdgeCases:
    """Edge case tests for the data-pulled-as-of route."""

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value=None)
    def test_resolved_none_returns_null_timestamp(self, mock_resolve, auth_client):
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["timestamp"] is None

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="echo*")
    @mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None)
    def test_es_unavailable_returns_null_timestamp(self, mock_es, mock_resolve, auth_client):
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["timestamp"] is None

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="echo*")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_exception_returns_null_timestamp(self, mock_es_client, mock_resolve, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("timeout")
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["timestamp"] is None

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="echo*")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_successful_returns_timestamp(self, mock_es_client, mock_resolve, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {"max_ts": {"value_as_string": "2026-04-01T12:00:00Z"}}
        }
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["timestamp"] == "2026-04-01T12:00:00Z"
        assert data["platform"] == "Echo"


# ===========================================================================
# get_dashboard_filter_options edge cases (lines 2034-2072)
# Partnership/primary filter via ES aggregations
# ===========================================================================


class TestFilterOptionsPlatformAggEdgeCases:
    """Tests for the filter-options route when using platform-based ES aggregations."""

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb*")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_single_org_primary_filter_uses_script(self, mock_es_client, mock_resolve, auth_client):
        """Single org with filter_type=primary uses script query to filter single-org docs."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {
                "orgs": {"buckets": [{"key": "OrgA", "doc_count": 10}]},
                "teams": {"team_names": {"buckets": [{"key": "Team1", "doc_count": 5}]}},
            }
        }
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/filter-options?platform=Meta&orgs=OrgA&filter_type=primary")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["platform"] == "Meta"

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb*")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_single_org_partnerships_filter_uses_script(self, mock_es_client, mock_resolve, auth_client):
        """Single org with filter_type=partnerships uses script query for multi-org docs."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {
                "orgs": {"buckets": [{"key": "OrgA", "doc_count": 10}]},
                "teams": {"team_names": {"buckets": [{"key": "Partner Team", "doc_count": 3}]}},
            }
        }
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/filter-options?platform=Meta&orgs=OrgA&filter_type=partnerships")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb*")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_multiple_orgs_uses_terms_filter(self, mock_es_client, mock_resolve, auth_client):
        """Multiple orgs use terms filter instead of script."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {
                "orgs": {"buckets": [{"key": "OrgA", "doc_count": 10}, {"key": "OrgB", "doc_count": 5}]},
                "teams": {"team_names": {"buckets": [{"key": "T1", "doc_count": 1}]}},
            }
        }
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/filter-options?platform=Meta&orgs=OrgA,OrgB")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb*")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_both_suffixes_fail_returns_empty(self, mock_es_client, mock_resolve, auth_client):
        """When both '' and '.keyword' suffixes fail, returns empty lists."""
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("mapping error")
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/filter-options?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["organizations"] == []
        assert data["teams"] == []

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb*")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_single_org_default_filter_type(self, mock_es_client, mock_resolve, auth_client):
        """Single org with unrecognized filter_type defaults to term match."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {
                "orgs": {"buckets": [{"key": "OrgX", "doc_count": 10}]},
                "teams": {"team_names": {"buckets": [{"key": "T", "doc_count": 2}]}},
            }
        }
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/filter-options?platform=Meta&orgs=OrgX&filter_type=custom_unknown")
        assert resp.status_code == 200

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
    def test_asset_fallback_comma_separated_org(self, _has_scope, _no_filter, mock_get_all, auth_client):
        """Teams with comma-separated organization_title are handled.

        should_apply_filter mocked off so the legacy
        pass-through branch fires after the deny-all gate.
        Phase B.2 also requires resolve_user_scope to return a
        non-empty scope (otherwise _scope_filter_options_to_user
        short-circuits to deny-all and the test never reaches the
        comma-split path).
        """
        mock_get_all.side_effect = [
            [{"_source": {"organization_title": "OrgA"}}],
            [{"_source": {"team_title": "CSVTeam", "organization_title": "OrgA, OrgB"}}],
        ]
        # Phase B.2: bypass the deny-all gate by mocking the
        # resolver to return a non-empty scope. The test exercises
        # the comma-split fallback path that runs *after* the gate.
        from asset_management.web.services.role_filter_service import UserScope

        with mock.patch(
            "asset_management.web.services.role_filter_service.resolve_user_scope",
            return_value=UserScope(
                org_titles=("OrgA",),
                team_titles=("CSVTeam",),
                scope_source="es_security_legacy",
            ),
        ):
            resp = auth_client.get("/api/dashboards/filter-options?filter_type=partnerships")
        assert resp.status_code == 200
        data = resp.get_json()
        team_titles = [t["title"] for t in data["teams"]]
        assert "CSVTeam" in team_titles

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="echo*")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
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
    def test_no_org_filter_uses_simple_teams_agg(self, _has_scope, _no_filter, mock_es_client, mock_resolve, auth_client):
        """When no org filter, teams agg is simple (not nested under filter).

        ``should_apply_filter`` is mocked to False so the
        scope-filter doesn't strip Team1 out of the result. The
        scope-filter behavior itself is covered -EXPLORER-001.

         Phase B.2: resolve_user_scope is mocked to return a
        non-empty scope so the deny-all gate doesn't short-circuit
        before the agg-flatten code under test runs.
        """
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {
                "orgs": {"buckets": [{"key": "Org1", "doc_count": 10}]},
                "teams": {"buckets": [{"key": "Team1", "doc_count": 5}]},
            }
        }
        mock_es_client.return_value = mock_es

        from asset_management.web.services.role_filter_service import UserScope

        with mock.patch(
            "asset_management.web.services.role_filter_service.resolve_user_scope",
            return_value=UserScope(
                org_titles=("Org1",),
                team_titles=("Team1",),
                scope_source="es_security_legacy",
            ),
        ):
            resp = auth_client.get("/api/dashboards/filter-options?platform=Echo")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["teams"][0]["title"] == "Team1"

    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb*")
    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_unknown_buckets_filtered_out(self, mock_es_client, mock_resolve, auth_client):
        """Buckets with key='unknown' (case-insensitive) are excluded."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {
                "orgs": {"buckets": [
                    {"key": "OrgA", "doc_count": 10},
                    {"key": "unknown", "doc_count": 2},
                ]},
                "teams": {"buckets": [
                    {"key": "Unknown", "doc_count": 1},
                    {"key": "Good Team", "doc_count": 5},
                ]},
            }
        }
        mock_es_client.return_value = mock_es

        resp = auth_client.get("/api/dashboards/filter-options?platform=Meta")
        assert resp.status_code == 200
        data = resp.get_json()
        org_titles = [o["title"] for o in data["organizations"]]
        assert "unknown" not in org_titles
        team_titles = [t["title"] for t in data["teams"]]
        assert "Unknown" not in team_titles
