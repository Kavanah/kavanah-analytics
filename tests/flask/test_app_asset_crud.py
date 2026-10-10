"""
Tests for asset CRUD edge cases and login side-paths in app.py.

Covers:
  create_kibana_session                   — Kibana SSO helper
  login_post (force_password_change)      — fpc branch after successful login
  add_asset  (ga_properties, ga_accounts, organizations, teams, fb app users)
  edit_asset (ga_properties, ga_accounts, fb app users, field deletions)
  delete_asset (dashboards — also deletes from Kibana)
  bulk_delete_assets (dashboards — also deletes from Kibana; per-item errors)
"""

import time
import pytest
from unittest import mock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_csrf(client, token="test-csrf-token"):
    with client.session_transaction() as sess:
        sess["csrf_token"] = token
    return token


def _auth_result(roles=None):
    return {
        "success": True,
        "roles": roles or ["asset_admin"],
        "full_name": "Test User",
        "email": "test@example.com",
    }


# ===========================================================================
# create_kibana_session
# ===========================================================================

class TestCreateKibanaSession:

    def _call(self, app, status=200, sid_cookie=None, exc=None):
        """Call create_kibana_session directly via the app module."""
        from asset_management.web.app import create_kibana_session
        import requests

        mock_response = mock.MagicMock()
        mock_response.status_code = status
        mock_response.cookies.get.return_value = sid_cookie

        if exc:
            with mock.patch("requests.post", side_effect=exc):
                return create_kibana_session("alice", "secret")
        with mock.patch("requests.post", return_value=mock_response):
            return create_kibana_session("alice", "secret")

    def test_returns_sid_cookie_on_success(self, app):
        result = self._call(app, status=200, sid_cookie="my-sid-value")
        assert result == "my-sid-value"

    def test_returns_none_when_no_sid_cookie(self, app):
        result = self._call(app, status=200, sid_cookie=None)
        assert result is None

    def test_returns_none_on_non_200_status(self, app):
        result = self._call(app, status=401)
        assert result is None

    def test_returns_none_on_request_exception(self, app):
        import requests
        result = self._call(app, exc=requests.exceptions.ConnectionError("refused"))
        assert result is None


# ===========================================================================
# login_post — force_password_change branch
# ===========================================================================

class TestLoginPostForcedPasswordChange:

    def test_redirects_to_change_password_when_fpc_required(self, client):
        _seed_csrf(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value=_auth_result()), \
             mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required",
                        return_value={"required": False}), \
             mock.patch(
                 "asset_management.web.blueprints.analytics.routes.UserProvisioningService.get_user_metadata",
                 return_value={"force_password_change": True},
             ):
            resp = client.post(
                "/login",
                data={"csrf_token": "test-csrf-token", "username": "alice", "password": "Pass1!"},
            )
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]


# ===========================================================================
# add_asset — edge cases
# ===========================================================================

class TestAddAssetEdgeCases:

    def _post_add(self, auth_client, asset_type, form_data):
        return auth_client.post(
            f"/asset/{asset_type}/add",
            data=form_data,
            follow_redirects=False,
        )

    def test_ga_account_auto_generates_resource_name(self, auth_client):
        # dropped patch on RoleManagementService.create_org_role.
        with mock.patch("asset_management.web.app.FormService.validate_required_fields",
                        return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.process_form_data",
                        return_value={"ga_account_id": "123456789"}), \
             mock.patch("asset_management.web.app.AssetService.create_with_error",
                        return_value=(True, None, "new-id")):
            resp = self._post_add(auth_client, "ga_accounts",
                                  {"ga_account_id": "123456789"})
        assert resp.status_code == 302

    def test_ga_property_auto_generates_resource_name_and_creates_index(self, auth_client):
        with mock.patch("asset_management.web.app.FormService.validate_required_fields",
                        return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.process_form_data",
                        return_value={"ga_property_id": "999", "team_esid": "team-abc"}), \
             mock.patch("asset_management.web.app.AssetService.create_with_error",
                        return_value=(True, None, "new-id")), \
             mock.patch("lib.es_helpers.create_ga_metrics_index_for_team") as mock_create_idx:
            resp = self._post_add(auth_client, "ga_properties",
                                  {"ga_property_id": "999", "team_esid": "team-abc"})
        assert resp.status_code == 302
        mock_create_idx.assert_called_once_with("team-abc")

    # test_organization_add_creates_es_role +
    # test_team_add_creates_es_role deleted with the legacy auto-create
    # call sites in /asset/<type>/add.

    def test_facebook_app_user_oauth_redirect(self, auth_client):
        saved_item = {"fb_app_esid": "app-123", "fb_app_user_auth_url": "oauth/start"}
        with mock.patch("asset_management.web.app.FormService.validate_required_fields",
                        return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.process_form_data",
                        return_value={"fb_app_esid": "app-123"}), \
             mock.patch("asset_management.web.app.AssetService.create_with_error",
                        return_value=(True, None, "new-id")), \
             mock.patch("asset_management.web.app.AssetService.get_by_id",
                        return_value=saved_item), \
             mock.patch("asset_management.web.app._data_source_asset_in_user_workspace",
                        return_value=True):  # reference gate: the FB app is in scope
            resp = self._post_add(
                auth_client, "facebook_app_users",
                {"fb_app_esid": "app-123", "oauth_redirect": "1"},
            )
        assert resp.status_code == 302
        assert "oauth" in resp.headers["Location"].lower() or "app-123" in resp.headers["Location"]

    def test_facebook_app_user_edit_redirect_without_oauth(self, auth_client):
        """After adding FB App User without oauth_redirect, go to the edit page
        so the OAuth URL banner (driven by computed_display) is immediately
        visible.
        """
        with mock.patch("asset_management.web.app.FormService.validate_required_fields",
                        return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.process_form_data",
                        return_value={}), \
             mock.patch("asset_management.web.app.AssetService.create_with_error",
                        return_value=(True, None, "new-id")):
            resp = self._post_add(auth_client, "facebook_app_users", {})
        assert resp.status_code == 302
        assert "/edit/new-id" in resp.headers["Location"]


# ===========================================================================
# edit_asset — edge cases
# ===========================================================================

class TestEditAssetEdgeCases:

    def _post_edit(self, auth_client, asset_type, item_id, form_data):
        return auth_client.post(
            f"/asset/{asset_type}/edit/{item_id}",
            data=form_data,
            follow_redirects=False,
        )

    def test_ga_property_creates_index_when_team_changes(self, auth_client):
        existing = {"ga_property_id": "999", "team_esid": "old-team"}
        with mock.patch("asset_management.web.app.AssetService.get_by_id",
                        return_value=existing), \
             mock.patch("asset_management.web.app._data_source_asset_in_user_workspace",
                        return_value=True), \
             mock.patch("asset_management.web.app.FormService.validate_required_fields",
                        return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.prepare_for_edit",
                        return_value=existing), \
             mock.patch("asset_management.web.app.FormService.get_select_options",
                        return_value={}), \
             mock.patch("asset_management.web.app.FormService.process_form_data",
                        return_value={"ga_property_id": "999", "team_esid": "new-team"}), \
             mock.patch("asset_management.web.app.AssetService.update",
                        return_value=True), \
             mock.patch("lib.es_helpers.create_ga_metrics_index_for_team") as mock_idx:
            resp = self._post_edit(auth_client, "ga_properties", "prop-id",
                                   {"ga_property_id": "999", "team_esid": "new-team"})
        assert resp.status_code == 302
        mock_idx.assert_called_once_with("new-team")

    def test_ga_account_auto_generates_resource_name_on_edit(self, auth_client):
        existing = {"ga_account_id": "456"}
        with mock.patch("asset_management.web.app.AssetService.get_by_id",
                        return_value=existing), \
             mock.patch("asset_management.web.app._data_source_asset_in_user_workspace",
                        return_value=True), \
             mock.patch("asset_management.web.app.FormService.validate_required_fields",
                        return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.prepare_for_edit",
                        return_value=existing), \
             mock.patch("asset_management.web.app.FormService.get_select_options",
                        return_value={}), \
             mock.patch("asset_management.web.app.FormService.process_form_data",
                        return_value={"ga_account_id": "456"}), \
             mock.patch("asset_management.web.app.AssetService.update",
                        return_value=True):
            resp = self._post_edit(auth_client, "ga_accounts", "acct-id",
                                   {"ga_account_id": "456"})
        assert resp.status_code == 302

    def test_facebook_app_user_oauth_redirect_on_edit(self, auth_client):
        existing = {"fb_app_esid": "app-abc", "fb_app_user_auth_url": "oauth/path"}
        with mock.patch("asset_management.web.app.AssetService.get_by_id",
                        return_value=existing), \
             mock.patch("asset_management.web.app._data_source_asset_in_user_workspace",
                        return_value=True), \
             mock.patch("asset_management.web.app.FormService.validate_required_fields",
                        return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.prepare_for_edit",
                        return_value=existing), \
             mock.patch("asset_management.web.app.FormService.get_select_options",
                        return_value={}), \
             mock.patch("asset_management.web.app.FormService.process_form_data",
                        return_value={"fb_app_esid": "app-abc", "fb_app_user_auth_url": "oauth/path"}), \
             mock.patch("asset_management.web.app.AssetService.update",
                        return_value=True):
            resp = self._post_edit(
                auth_client, "facebook_app_users", "fb-id",
                {"fb_app_esid": "app-abc", "oauth_redirect": "1"},
            )
        assert resp.status_code == 302
        # Should redirect to Facebook OAuth URL, not the list page
        assert "app-abc" in resp.headers["Location"] or "oauth" in resp.headers["Location"].lower()

    @pytest.fixture(autouse=True)
    def _allow_company_tier_scope(self):
        # edit_asset now enforces a company-tier per-object scope check
        # for teams/organizations. This test verifies field-deletion behavior,
        # not the gate (covered /test_company_tier_idor.py).
        with mock.patch(
            "asset_management.web.app._company_tier_asset_in_user_scope", return_value=True
        ):
            yield

    def test_field_deletion_removes_key(self, auth_client):
        existing = {"team_title": "Alpha", "some_field": "value_to_delete"}
        with mock.patch("asset_management.web.app.AssetService.get_by_id",
                        return_value=existing), \
             mock.patch("asset_management.web.app.FormService.validate_required_fields",
                        return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.prepare_for_edit",
                        return_value=existing), \
             mock.patch("asset_management.web.app.FormService.get_select_options",
                        return_value={}), \
             mock.patch("asset_management.web.app.FormService.process_form_data",
                        return_value={"team_title": "Alpha", "__delete__some_field": True}), \
             mock.patch("asset_management.web.app.AssetService.update",
                        return_value=True) as mock_update:
            resp = self._post_edit(auth_client, "teams", "team-id",
                                   {"team_title": "Alpha", "__delete__some_field": "1"})
        assert resp.status_code == 302
        # The updated document passed to AssetService.update should NOT contain some_field
        updated_doc = mock_update.call_args[0][2]
        assert "some_field" not in updated_doc


# ===========================================================================
# delete_asset — Kibana dashboard deletion
# ===========================================================================

class TestDeleteAssetKibanaIntegration:

    @pytest.fixture(autouse=True)
    def _allow_company_tier_scope(self):
        # delete_asset now runs a company-tier scope check (which loads
        # the doc) for teams before deleting. Allow it + stub the load so this
        # Kibana-integration test exercises the delete path it targets.
        with mock.patch(
            "asset_management.web.app._company_tier_asset_in_user_scope", return_value=True
        ), mock.patch(
            "asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "Alpha"}
        ):
            yield

    def test_delete_non_dashboard_skips_kibana(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.delete", return_value=True) as mock_del, \
             mock.patch("asset_management.web.app.KibanaService.delete_dashboard") as mock_kib:
            resp = auth_client.post("/asset/teams/delete/team-id")
        assert resp.status_code == 200
        mock_kib.assert_not_called()


# ===========================================================================
# bulk_delete_assets — Kibana + per-item error handling
# ===========================================================================

class TestBulkDeleteAssetsEdgeCases:

    @pytest.fixture(autouse=True)
    def _allow_company_tier_scope(self):
        # bulk_delete now runs a per-item company-tier scope check
        # (which loads each doc) for teams. Allow it + stub the load so this
        # per-item-error test reaches delete_only for both ids.
        with mock.patch(
            "asset_management.web.app._company_tier_asset_in_user_scope", return_value=True
        ), mock.patch(
            "asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "Alpha"}
        ):
            yield

    def test_bulk_delete_records_per_item_exception(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.delete_only",
                        side_effect=Exception("ES write error")):
            resp = auth_client.post(
                "/asset/teams/bulk-delete",
                json={"item_ids": ["t1", "t2"]},
            )
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["failed_count"] == 2
        assert len(data["failures"]) == 2
        # exception text stays in the server log, never the response.
        assert data["failures"][0]["error"] == "Delete failed"

