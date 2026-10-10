"""
Tests for advanced asset route paths including form submission, logo upload,
CSV import, and FB App User OAuth URL building.

Routes covered:
  POST /asset/<type>/add       — form submission with validation, logo upload
  POST /asset/<type>/edit/<id> — edit form submission with logo, field deletion
  POST /asset/<type>/delete/<id>  — delete asset (dashboard special case)
  POST /asset/<type>/bulk-delete  — bulk delete
  Helper: _handle_logo_upload
  Helper: _build_fb_app_user_oauth_url
  Helper: _delete_logo_file

All Elasticsearch calls are mocked; no real ES instance is required.
"""

import io
import os
from unittest import mock

import pytest

# edit/delete/bulk-delete tests here target the company-tier (teams/
# organizations) routes that now enforce a per-object scope check; neutralize it
# (covered by the release-plan tests).
pytestmark = pytest.mark.usefixtures("allow_company_tier_and_options_scope")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ITEM_ID = "item-abc-123"


# ===========================================================================
# POST /asset/<type>/add — form submission
# ===========================================================================


class TestAddAssetPost:
    """Tests for POST add_asset (form submission path)."""

    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "new-id"))
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "New Team"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_add_team_success_redirects(self, mock_validate, mock_process, mock_create, auth_client):
        resp = auth_client.post(
            "/asset/teams/add",
            data={"team_title": "New Team"},
        )
        assert resp.status_code == 302
        assert "/asset/teams" in resp.headers["Location"]

    @mock.patch("asset_management.web.app.FormService.get_select_options", return_value={})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(False, "Title is required"))
    def test_add_validation_failure_returns_400(self, mock_validate, mock_opts, auth_client):
        resp = auth_client.post(
            "/asset/teams/add",
            data={"team_title": ""},
        )
        assert resp.status_code == 400

    def test_add_invalid_type_returns_404(self, auth_client):
        resp = auth_client.post("/asset/nonexistent/add", data={"title": "x"})
        assert resp.status_code == 404

    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(False, "ES write error", None))
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "T"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_add_create_failure_returns_500(self, mock_v, mock_p, mock_c, auth_client):
        resp = auth_client.post("/asset/teams/add", data={"team_title": "T"})
        assert resp.status_code == 500

    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"fb_app_esid": "app1", "fb_app_user_auth_url": "auth"})
    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "new-fb-id"))
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"fb_field": "val"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_add_fb_app_user_redirects_to_edit(self, mock_v, mock_p, mock_c, mock_get, auth_client):
        # facebook_app_users has a computed_display block (OAuth URL) — after
        # add, the user lands on the edit page so the OAuth URL banner is
        # immediately visible.
        resp = auth_client.post(
            "/asset/facebook_app_users/add",
            data={"fb_field": "val"},
        )
        assert resp.status_code == 302
        assert "/edit/new-fb-id" in resp.headers["Location"]

    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"fb_app_esid": "app1", "fb_app_user_auth_url": "auth"})
    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "new-fb-id"))
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"fb_field": "val"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_add_fb_app_user_with_oauth_redirect(self, mock_v, mock_p, mock_c, mock_get, auth_client):
        resp = auth_client.post(
            "/asset/facebook_app_users/add",
            data={"fb_field": "val", "oauth_redirect": "1"},
        )
        assert resp.status_code == 302
        # Should redirect to OAuth URL, not the list page
        assert "app1" in resp.headers["Location"] or "auth" in resp.headers["Location"]

    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "new-id"))
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "T"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_add_from_tree_redirects_to_table_view(self, mock_v, mock_p, mock_c, auth_client):
        # from_tree=true now redirects to /asset/<asset_type>
        # (the table view), not back to /index?refresh=true&select_*.
        resp = auth_client.post("/asset/teams/add?from_tree=true", data={"team_title": "T"})
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/asset/teams"

    # test_add_org_creates_role + test_add_team_creates_role
    # deleted with the legacy ES-Security auto-create call sites.

    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "ga-id"))
    @mock.patch(
        "asset_management.web.app.FormService.process_form_data",
        return_value={"ga_account_id": "12345"},
    )
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_add_ga_account_auto_generates_resource_name(self, mock_v, mock_p, mock_c, auth_client):
        resp = auth_client.post("/asset/ga_accounts/add", data={"ga_account_id": "12345"})
        assert resp.status_code == 302
        # The data passed to create should have ga_account_resource_name auto-set
        call_data = mock_c.call_args[0][1]
        assert call_data.get("ga_account_resource_name") == "accounts/12345"


# ===========================================================================
# POST /asset/<type>/edit/<id>  — edit form submission
# ===========================================================================


class TestEditAssetPost:
    """Tests for POST edit_asset (form submission path)."""

    @mock.patch("asset_management.web.app.AssetService.update", return_value=True)
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "Updated"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "Old"})
    def test_edit_success_redirects(self, mock_get, mock_v, mock_p, mock_update, auth_client):
        resp = auth_client.post(
            f"/asset/teams/edit/{ITEM_ID}",
            data={"team_title": "Updated"},
        )
        assert resp.status_code == 302
        assert "/asset/teams" in resp.headers["Location"]

    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=None)
    def test_edit_nonexistent_item_returns_404(self, mock_get, auth_client):
        resp = auth_client.post(f"/asset/teams/edit/{ITEM_ID}", data={"team_title": "X"})
        assert resp.status_code == 404

    def test_edit_invalid_type_returns_404(self, auth_client):
        resp = auth_client.post(f"/asset/nonexistent/edit/{ITEM_ID}", data={})
        assert resp.status_code == 404

    @mock.patch("asset_management.web.app.FormService.get_select_options", return_value={})
    @mock.patch("asset_management.web.app.FormService.prepare_for_edit", return_value={"team_title": "Old"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(False, "Title required"))
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "Old"})
    def test_edit_validation_failure_returns_400(self, mock_get, mock_v, mock_prep, mock_opts, auth_client):
        resp = auth_client.post(f"/asset/teams/edit/{ITEM_ID}", data={"team_title": ""})
        assert resp.status_code == 400

    @mock.patch("asset_management.web.app.AssetService.update", return_value=False)
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "X"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "Old"})
    def test_edit_update_failure_returns_500(self, mock_get, mock_v, mock_p, mock_update, auth_client):
        resp = auth_client.post(f"/asset/teams/edit/{ITEM_ID}", data={"team_title": "X"})
        assert resp.status_code == 500

    @mock.patch("asset_management.web.app.AssetService.update", return_value=True)
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "U"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "Old"})
    def test_edit_from_tree_redirects_with_refresh(self, mock_get, mock_v, mock_p, mock_update, auth_client):
        resp = auth_client.post(f"/asset/teams/edit/{ITEM_ID}?from_tree=true", data={"team_title": "U"})
        assert resp.status_code == 302
        assert "refresh=true" in resp.headers["Location"]

    @mock.patch("asset_management.web.app.AssetService.update", return_value=True)
    @mock.patch(
        "asset_management.web.app.FormService.process_form_data",
        return_value={"fb_field": "v", "fb_app_esid": "app1", "fb_app_user_auth_url": "auth"},
    )
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch(
        "asset_management.web.app.AssetService.get_by_id",
        return_value={"fb_field": "old", "fb_app_esid": "app1", "fb_app_user_auth_url": "auth"},
    )
    @mock.patch("asset_management.web.app._data_source_asset_in_user_workspace", return_value=True)
    def test_edit_fb_app_user_with_oauth_redirect(self, mock_ws_gate, mock_get, mock_v, mock_p, mock_update, auth_client):
        resp = auth_client.post(
            f"/asset/facebook_app_users/edit/{ITEM_ID}",
            data={"fb_field": "v", "oauth_redirect": "1"},
        )
        assert resp.status_code == 302

    @mock.patch("asset_management.web.app.AssetService.update", return_value=True)
    @mock.patch(
        "asset_management.web.app.FormService.process_form_data",
        return_value={"__delete__old_field": True, "team_title": "T"},
    )
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "Old", "old_field": "val"})
    def test_edit_handles_field_deletion(self, mock_get, mock_v, mock_p, mock_update, auth_client):
        resp = auth_client.post(f"/asset/teams/edit/{ITEM_ID}", data={"team_title": "T"})
        assert resp.status_code == 302
        # Verify that old_field was removed from the updated document
        update_args = mock_update.call_args
        updated_doc = update_args[0][2]
        assert "old_field" not in updated_doc
        assert "__delete__old_field" not in updated_doc


# ===========================================================================
# POST /asset/<type>/delete/<id>  — delete asset
# ===========================================================================


class TestDeleteAsset:
    """Tests for delete_asset including dashboard special case."""

    @mock.patch("asset_management.web.app.AssetService.delete", return_value=True)
    def test_delete_success(self, mock_delete, auth_client):
        resp = auth_client.post(f"/asset/teams/delete/{ITEM_ID}")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @mock.patch("asset_management.web.app.AssetService.delete", return_value=False)
    def test_delete_failure(self, mock_delete, auth_client):
        resp = auth_client.post(f"/asset/teams/delete/{ITEM_ID}")
        assert resp.status_code == 500

    def test_delete_invalid_type(self, auth_client):
        resp = auth_client.post(f"/asset/nonexistent/delete/{ITEM_ID}")
        assert resp.status_code == 404

    @mock.patch("asset_management.web.app.AssetService.delete", return_value=True)
    @mock.patch("asset_management.web.app.KibanaService.delete_dashboard")
    @mock.patch(
        "asset_management.web.app.AssetService.get_by_id",
        return_value={"dashboard_id": "kib-id", "space_id": "default"},
    )
    @mock.patch("asset_management.web.app._data_source_asset_in_user_workspace", return_value=True)
    def test_delete_dashboard_also_deletes_from_kibana(self, mock_ws_gate, mock_get, mock_kib_del, mock_del, auth_client):
        """Dashboards type triggers Kibana deletion when present in ASSET_CONFIGS."""
        # 'dashboards' must be injected into ASSET_CONFIGS for this test
        dash_config = {"index": "de_asset_dashboards", "title": "Dashboards", "title_field": "dashboard_title"}
        with mock.patch.dict("asset_management.web.app.ASSET_CONFIGS", {"dashboards": dash_config}):
            resp = auth_client.post(f"/asset/dashboards/delete/{ITEM_ID}")
        assert resp.status_code == 200
        mock_kib_del.assert_called_once_with("kib-id", "default")


# ===========================================================================
# POST /asset/<type>/bulk-delete  — bulk delete
# ===========================================================================


class TestBulkDeleteAssets:
    """Tests for bulk delete endpoint."""

    @mock.patch("asset_management.web.app.AssetService.delete_only", return_value=True)
    def test_bulk_delete_all_success(self, mock_delete, auth_client):
        resp = auth_client.post(
            "/asset/teams/bulk-delete",
            json={"item_ids": ["id1", "id2"]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["deleted_count"] == 2
        assert data["failed_count"] == 0

    def test_bulk_delete_no_items(self, auth_client):
        resp = auth_client.post("/asset/teams/bulk-delete", json={"item_ids": []})
        assert resp.status_code == 400

    def test_bulk_delete_invalid_type(self, auth_client):
        resp = auth_client.post("/asset/nonexistent/bulk-delete", json={"item_ids": ["id1"]})
        assert resp.status_code == 404

    @mock.patch("asset_management.web.app.AssetService.delete_only", side_effect=[True, False])
    def test_bulk_delete_partial_failure(self, mock_delete, auth_client):
        resp = auth_client.post(
            "/asset/teams/bulk-delete",
            json={"item_ids": ["id1", "id2"]},
        )
        data = resp.get_json()
        assert data["deleted_count"] == 1
        assert data["failed_count"] == 1

    @mock.patch("asset_management.web.app.AssetService.delete_only", side_effect=Exception("boom"))
    def test_bulk_delete_exception_handling(self, mock_delete, auth_client):
        resp = auth_client.post(
            "/asset/teams/bulk-delete",
            json={"item_ids": ["id1"]},
        )
        data = resp.get_json()
        assert data["failed_count"] == 1
        # exception text stays in the server log, never the response.
        assert data["failures"][0]["error"] == "Delete failed"


# ===========================================================================
# Logo upload handling
# ===========================================================================


# (F7 / co-required PR) gates /asset/organizations/* on
# workspace.is_agency=true. The auth_client fixture is an asset_admin
# without an agency workspace, which now 404s the org routes. Mock the
# gate at class level so every TestLogoUpload case exercises the
# earlier behaviour. Per-class decorator is cleaner than per-method
# patches because all 6 tests need the same treatment.
@mock.patch(
    "asset_management.web.services.workspace_membership_service.WorkspaceMembershipService.is_agency_for_user",
    return_value=True,
)
class TestLogoUpload:
    """Tests for _handle_logo_upload and _delete_logo_file helpers."""

    @mock.patch("asset_management.web.app.FormService.get_select_options", return_value={})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(False, "Required"))
    def test_add_org_validation_returns_400(self, mock_v, mock_opts, _agency, auth_client):
        """Validation failure on organizations returns 400 before logo processing."""
        resp = auth_client.post("/asset/organizations/add", data={"organization_title": ""})
        assert resp.status_code == 400

    @mock.patch("asset_management.web.app._handle_logo_upload", return_value=(False, "Logo must be a JPG or PNG file."))
    @mock.patch("asset_management.web.app.FormService.get_select_options", return_value={})
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"organization_title": "X"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_add_org_logo_error_returns_400(self, mock_v, mock_p, mock_opts, mock_logo, _agency, auth_client):
        resp = auth_client.post("/asset/organizations/add", data={"organization_title": "X"})
        assert resp.status_code == 400

    @mock.patch("asset_management.web.app._handle_logo_upload", return_value=("uploads/logos/logo_abc.jpg", None))
    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "org-id"))
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"organization_title": "X"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_add_org_with_logo_saves_path(self, mock_v, mock_p, mock_c, mock_logo, _agency, auth_client):
        # dropped @mock.patch on RoleManagementService.create_org_role.
        resp = auth_client.post("/asset/organizations/add", data={"organization_title": "X"})
        assert resp.status_code == 302
        call_data = mock_c.call_args[0][1]
        assert call_data.get("organization_logo") == "uploads/logos/logo_abc.jpg"

    @mock.patch("asset_management.web.app._handle_logo_upload", return_value=(None, None))
    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "org-id"))
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"organization_title": "X"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_add_org_logo_removal(self, mock_v, mock_p, mock_c, mock_logo, _agency, auth_client):
        """Logo removal request (return None) sets logo path to empty string."""
        # dropped @mock.patch on RoleManagementService.create_org_role.
        resp = auth_client.post("/asset/organizations/add", data={"organization_title": "X"})
        assert resp.status_code == 302
        call_data = mock_c.call_args[0][1]
        assert call_data.get("organization_logo") == ""

    @mock.patch("asset_management.web.app._delete_logo_file")
    @mock.patch("asset_management.web.app._handle_logo_upload", return_value=("uploads/logos/new.jpg", None))
    @mock.patch("asset_management.web.app.AssetService.update", return_value=True)
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"organization_title": "X"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch(
        "asset_management.web.app.AssetService.get_by_id",
        return_value={"organization_title": "X", "organization_logo": "uploads/logos/old.jpg"},
    )
    def test_edit_org_deletes_old_logo(self, mock_get, mock_v, mock_p, mock_update, mock_logo, mock_del, _agency, auth_client):
        resp = auth_client.post(f"/asset/organizations/edit/{ITEM_ID}", data={"organization_title": "X"})
        assert resp.status_code == 302
        mock_del.assert_called_once_with("uploads/logos/old.jpg")

    @mock.patch("asset_management.web.app._handle_logo_upload", return_value=(False, "Logo file exceeds the 2 MB size limit."))
    @mock.patch("asset_management.web.app.FormService.get_select_options", return_value={})
    @mock.patch("asset_management.web.app.FormService.prepare_for_edit", return_value={"organization_title": "X"})
    @mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"organization_title": "X"})
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"organization_title": "X"})
    def test_edit_org_logo_error_returns_400(self, mock_get, mock_v, mock_p, mock_prep, mock_opts, mock_logo, _agency, auth_client):
        resp = auth_client.post(f"/asset/organizations/edit/{ITEM_ID}", data={"organization_title": "X"})
        assert resp.status_code == 400


# ===========================================================================
# _build_fb_app_user_oauth_url
# ===========================================================================


class TestBuildFbAppUserOauthUrl:
    """Tests for the _build_fb_app_user_oauth_url helper."""

    def test_builds_correct_url(self):
        from asset_management.web.app import _build_fb_app_user_oauth_url

        item = {"fb_app_esid": "app123", "fb_app_user_auth_url": "auth/callback"}
        with mock.patch("asset_management.web.config.config") as mock_config:
            mock_config.FBOAUTH_HOSTNAME = "oauth.example.com"
            url = _build_fb_app_user_oauth_url(item)
        assert url == "https://oauth.example.com/app123/auth/callback"

    def test_uses_localhost_when_no_hostname(self):
        from asset_management.web.app import _build_fb_app_user_oauth_url

        item = {"fb_app_esid": "app1", "fb_app_user_auth_url": "auth"}
        with mock.patch("asset_management.web.config.config") as mock_config:
            mock_config.FBOAUTH_HOSTNAME = None
            url = _build_fb_app_user_oauth_url(item)
        assert url == "https://localhost/app1/auth"

    def test_handles_empty_fields(self):
        from asset_management.web.app import _build_fb_app_user_oauth_url

        item = {}
        with mock.patch("asset_management.web.config.config") as mock_config:
            mock_config.FBOAUTH_HOSTNAME = "host.com"
            url = _build_fb_app_user_oauth_url(item)
        assert url == "https://host.com//"


# ===========================================================================
# GET /asset/<type>/add  — form rendering (GET)
# ===========================================================================


class TestAddAssetGet:
    """Tests for GET add_asset (form rendering)."""

    @mock.patch("asset_management.web.app.FormService.get_select_options", return_value={})
    def test_renders_form(self, mock_opts, auth_client):
        resp = auth_client.get("/asset/teams/add")
        assert resp.status_code == 200

    def test_invalid_type_returns_404(self, auth_client):
        resp = auth_client.get("/asset/nonexistent/add")
        assert resp.status_code == 404


# ===========================================================================
# GET /asset/<type>/edit/<id>  — form rendering (GET)
# ===========================================================================


class TestEditAssetGet:
    """Tests for GET edit_asset (form rendering)."""

    @mock.patch("asset_management.web.app.FormService.get_select_options", return_value={})
    @mock.patch("asset_management.web.app.FormService.prepare_for_edit", return_value={"team_title": "T"})
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "T"})
    def test_renders_edit_form(self, mock_get, mock_prep, mock_opts, auth_client):
        resp = auth_client.get(f"/asset/teams/edit/{ITEM_ID}")
        assert resp.status_code == 200

    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=None)
    def test_nonexistent_item_returns_404(self, mock_get, auth_client):
        resp = auth_client.get(f"/asset/teams/edit/{ITEM_ID}")
        assert resp.status_code == 404


# ===========================================================================
# GA auto-generate resource_name on edit
# ===========================================================================


class TestGaAutoResourceName:
    """Tests for auto-generating GA resource_name fields."""

    @mock.patch("asset_management.web.app.AssetService.update", return_value=True)
    @mock.patch(
        "asset_management.web.app.FormService.process_form_data",
        return_value={"ga_property_id": "99999"},
    )
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"ga_property_id": "99999"})
    @mock.patch("asset_management.web.app._data_source_asset_in_user_workspace", return_value=True)
    def test_edit_ga_property_auto_generates_resource_name(
        self, mock_ws_gate, mock_get, mock_v, mock_p, mock_update, auth_client
    ):
        resp = auth_client.post(f"/asset/ga_properties/edit/{ITEM_ID}", data={"ga_property_id": "99999"})
        assert resp.status_code == 302
        updated_doc = mock_update.call_args[0][2]
        assert updated_doc.get("ga_property_resource_name") == "properties/99999"

    @mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "gp-id"))
    @mock.patch(
        "asset_management.web.app.FormService.process_form_data",
        return_value={"ga_property_id": "77777"},
    )
    @mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None))
    def test_add_ga_property_auto_generates_resource_name(self, mock_v, mock_p, mock_c, auth_client):
        resp = auth_client.post("/asset/ga_properties/add", data={"ga_property_id": "77777"})
        assert resp.status_code == 302
        call_data = mock_c.call_args[0][1]
        assert call_data.get("ga_property_resource_name") == "properties/77777"
