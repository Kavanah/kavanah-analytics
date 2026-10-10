"""
Tests for change-password, create-user, delete-user routes in app.py.

Covers:
  GET  /change-password                         — forced password change page
  POST /change-password                         — process forced password change
  POST /api/user-management/create-user         — admin: create new ES user
  POST /api/user-management/delete-user         — admin: delete ES user
  POST /mfa/challenge (force_password edge)     — MFA success triggers fpc flow
  GET  /mfa/setup    (unknown method fallback)  — mfa_setup_page no-method path
  POST /mfa/setup/yubikey (success path)        — yubikey setup complete
"""

import time
import pytest
from unittest import mock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_fpc_pending(client, username="newuser"):
    """Inject a forced-password-change session."""
    with client.session_transaction() as sess:
        sess["fpc_pending"] = True
        sess["fpc_username"] = username
        sess["fpc_roles"] = ["asset_admin"]
        sess["fpc_full_name"] = "New User"
        sess["fpc_email"] = "new@example.com"
        sess["fpc_timestamp"] = time.time()
        sess["csrf_token"] = "test-csrf-token"


def _set_mfa_pending(client, method="totp", username="mfauser"):
    with client.session_transaction() as sess:
        sess["mfa_pending"] = True
        sess["mfa_pending_method"] = method
        sess["mfa_pending_username"] = username
        sess["mfa_pending_timestamp"] = time.time()
        sess["mfa_pending_roles"] = ["asset_admin"]
        sess["mfa_pending_full_name"] = "MFA User"
        sess["mfa_pending_email"] = "mfa@example.com"
        sess["mfa_pending_kibana_sid"] = "sid-value"
        sess["mfa_pending_next"] = "/"
        sess["mfa_pending_login_type"] = "form_login"
        sess["csrf_token"] = "test-csrf-token"


# ===========================================================================
# GET /change-password
# ===========================================================================

class TestChangePasswordPage:

    def test_redirects_to_login_when_no_fpc_pending(self, client):
        resp = client.get("/change-password")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_shows_change_password_form_when_fpc_pending(self, client):
        _set_fpc_pending(client)
        resp = client.get("/change-password")
        assert resp.status_code == 200

    def test_shows_error_from_query_param(self, client):
        _set_fpc_pending(client)
        resp = client.get("/change-password?error=Invalid+password")
        assert resp.status_code == 200


# ===========================================================================
# POST /change-password
# ===========================================================================

class TestChangePasswordPost:

    def _post(self, client, **kwargs):
        data = {
            "csrf_token": "test-csrf-token",
            "current_password": "TempPass1!",
            "new_password": "NewPass123!",
            "confirm_password": "NewPass123!",
        }
        data.update(kwargs)
        return client.post("/change-password", data=data)

    def test_redirects_to_login_when_no_fpc_pending(self, client):
        resp = self._post(client)
        assert resp.status_code == 401
        assert "login" in (resp.get_json() or {}).get("login_url", "").lower()

    def test_rejects_invalid_csrf(self, client):
        _set_fpc_pending(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=False):
            resp = self._post(client, csrf_token="bad-token")
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]

    def test_returns_error_when_current_password_empty(self, client):
        _set_fpc_pending(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True):
            resp = self._post(client, current_password="")
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]

    def test_returns_error_when_current_password_wrong(self, client):
        _set_fpc_pending(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": False}):
            resp = self._post(client, current_password="WrongPass!")
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]

    def test_returns_error_when_new_password_too_short(self, client):
        _set_fpc_pending(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}):
            resp = self._post(client, new_password="short", confirm_password="short")
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]

    def test_returns_error_when_new_password_too_long(self, client):
        # the setup flow now bounds the upper length server-side
        # (matching profile_change_password), so an over-128-char password is
        # rejected before reaching es.security.change_password.
        _set_fpc_pending(client)
        long_pw = "A" * 129 + "1!"
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}):
            resp = self._post(client, new_password=long_pw, confirm_password=long_pw)
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]
        assert "too+long" in resp.headers["Location"] or "too%20long" in resp.headers["Location"]

    def test_returns_error_when_new_matches_current(self, client):
        _set_fpc_pending(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}):
            resp = self._post(
                client,
                current_password="TempPass1!",
                new_password="TempPass1!",
                confirm_password="TempPass1!",
            )
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]

    def test_returns_error_when_passwords_dont_match(self, client):
        _set_fpc_pending(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}):
            resp = self._post(
                client,
                new_password="NewPass123!",
                confirm_password="DifferentPass!",
            )
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]

    def test_returns_error_when_es_unavailable(self, client):
        _set_fpc_pending(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = self._post(client)
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]

    def test_returns_error_when_es_change_fails(self, client):
        _set_fpc_pending(client)
        mock_es = mock.MagicMock()
        mock_es.security.change_password.side_effect = Exception("ES error")
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = self._post(client)
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]

    def test_successful_change_promotes_session(self, client):
        _set_fpc_pending(client)
        mock_es = mock.MagicMock()
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.UserProvisioningService.clear_force_password_change"), \
             mock.patch("asset_management.web.app.create_kibana_session", return_value=None):
            resp = self._post(client)
        assert resp.status_code == 302
        # After force-password-change, all users land on /dashboard.
        assert resp.headers["Location"].rstrip("/").endswith("/dashboard")

    def test_successful_change_sets_kibana_cookie(self, client):
        _set_fpc_pending(client)
        mock_es = mock.MagicMock()
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.UserProvisioningService.clear_force_password_change"), \
             mock.patch("asset_management.web.app.create_kibana_session", return_value="sid-value"):
            resp = self._post(client)
        assert resp.status_code == 302
        assert "sid" in resp.headers.get("Set-Cookie", "")


# ===========================================================================
# POST /api/user-management/create-user
# ===========================================================================

class TestApiCreateUser:

    def test_returns_400_when_username_missing(self, auth_client):
        resp = auth_client.post(
            "/api/user-management/create-user",
            json={"full_name": "Alice", "email": "a@example.com", "roles": ["asset_admin"]},
        )
        assert resp.status_code == 400
        assert "Username" in resp.get_json()["error"]

    def test_returns_400_when_email_missing(self, auth_client):
        resp = auth_client.post(
            "/api/user-management/create-user",
            json={"username": "alice", "full_name": "Alice", "roles": ["asset_admin"]},
        )
        assert resp.status_code == 400
        assert "Email" in resp.get_json()["error"]

    def test_returns_400_when_roles_empty(self, auth_client):
        # v3 follow-up: empty memberships is the rejection now —
        # at least one org/team scope is required.
        resp = auth_client.post(
            "/api/user-management/create-user",
            json={"username": "alice", "email": "a@example.com",
                  "system_roles": [], "memberships": []},
        )
        assert resp.status_code == 400
        assert "membership" in resp.get_json()["error"].lower()

    def test_returns_500_when_create_user_fails(self, auth_client):
        with mock.patch(
            "asset_management.web.app.UserProvisioningService.create_user",
            return_value={"success": False, "error": "User already exists"},
        ), mock.patch("asset_management.web.app._provision_new_authz_assignments"):
            resp = auth_client.post(
                "/api/user-management/create-user",
                json={
                    "username": "alice",
                    "full_name": "Alice",
                    "email": "a@example.com",
                    # asset_admin is now superuser-only; auth_client
                    # is asset_admin (non-superuser). Use an ordinary role.
                    "system_roles": ["report_publisher"],
                    "memberships": ["org_test"],
                },
            )
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False

    def test_successful_create_returns_email_status(self, auth_client):
        with mock.patch(
            "asset_management.web.app.UserProvisioningService.create_user",
            return_value={"success": True, "temp_password": "Temp123!"},
        ), mock.patch(
            "asset_management.web.app.UserProvisioningService.send_welcome_email",
            return_value={"success": True},
        ), mock.patch("asset_management.web.app._provision_new_authz_assignments"):
            resp = auth_client.post(
                "/api/user-management/create-user",
                json={
                    "username": "alice",
                    "full_name": "Alice",
                    "email": "alice@example.com",
                    # asset_admin is now superuser-only; use an
                    # ordinary grantable role for this happy-path assertion.
                    "system_roles": ["report_publisher"],
                    "memberships": ["org_test"],
                },
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["email_sent"] is True

    def test_returns_403_for_org_user(self, org_user_client):
        resp = org_user_client.post(
            "/api/user-management/create-user",
            json={"username": "x", "email": "x@x.com", "roles": ["r"]},
        )
        assert resp.status_code == 403

    def test_unauthenticated_redirects(self, client):
        resp = client.post(
            "/api/user-management/create-user",
            json={"username": "x", "email": "x@x.com", "roles": ["r"]},
        )
        assert resp.status_code == 401


# ===========================================================================
# POST /api/user-management/delete-user
# ===========================================================================

class TestApiDeleteUser:

    def test_returns_400_when_username_missing(self, auth_client):
        resp = auth_client.post("/api/user-management/delete-user", json={})
        assert resp.status_code == 400

    def test_returns_400_when_deleting_self(self, auth_client):
        # auth_client is logged in as "testuser"
        resp = auth_client.post(
            "/api/user-management/delete-user", json={"username": "testuser"}
        )
        assert resp.status_code == 400
        assert "own account" in resp.get_json()["error"]

    def test_returns_400_when_deleting_elastic_superuser(self, auth_client):
        resp = auth_client.post(
            "/api/user-management/delete-user", json={"username": "elastic"}
        )
        assert resp.status_code == 400
        assert "elastic" in resp.get_json()["error"]

    def test_returns_500_when_es_unavailable(self, auth_client):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = auth_client.post(
                "/api/user-management/delete-user", json={"username": "alice"}
            )
        assert resp.status_code == 500

    def test_successful_delete_returns_success(self, auth_client):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.post(
                "/api/user-management/delete-user", json={"username": "alice"}
            )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_es.security.delete_user.assert_called_once_with(username="alice")

    def test_returns_500_on_es_error(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.security.delete_user.side_effect = Exception("ES error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.post(
                "/api/user-management/delete-user", json={"username": "alice"}
            )
        assert resp.status_code == 500

    def test_returns_403_for_org_user(self, org_user_client):
        resp = org_user_client.post(
            "/api/user-management/delete-user", json={"username": "alice"}
        )
        assert resp.status_code == 403


# ===========================================================================
# MFA challenge — force_password_change after successful MFA
# ===========================================================================

class TestMfaChallengeForcePasswordChange:

    def test_redirects_to_change_password_when_fpc_required(self, client):
        _set_mfa_pending(client, method="totp")
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("asset_management.web.app.MFAService.validate_totp_code", return_value=True), \
             mock.patch(
                 "asset_management.web.app.UserProvisioningService.get_user_metadata",
                 return_value={"force_password_change": True},
             ):
            resp = client.post(
                "/mfa/challenge",
                data={"csrf_token": "test-csrf-token", "code": "123456"},
            )
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"]

    def test_sets_kibana_cookie_on_mfa_success(self, client):
        """When MFA passes and no fpc required, kibana cookie is set.

        The Kibana SID is now created at password-auth time and stored
        in the session as mfa_pending_kibana_sid (no plaintext password stored).
        """
        _set_mfa_pending(client, method="totp")
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("asset_management.web.app.MFAService.validate_totp_code", return_value=True), \
             mock.patch("asset_management.web.app.MFAService.update_last_used"), \
             mock.patch(
                 "asset_management.web.app.UserProvisioningService.get_user_metadata",
                 return_value=None,
             ):
            resp = client.post(
                "/mfa/challenge",
                data={"csrf_token": "test-csrf-token", "code": "123456"},
            )
        assert resp.status_code == 302
        assert "sid" in resp.headers.get("Set-Cookie", "")


# ===========================================================================
# GET /mfa/setup — fallback when method is unknown
# ===========================================================================

class TestMfaSetupPageFallback:

    def test_redirects_to_picker_when_method_unset(self, client):
        """users with MFA enabled but no chosen method land on the picker."""
        with client.session_transaction() as sess:
            sess["mfa_pending"] = True
            sess["mfa_pending_method"] = None
            sess["mfa_pending_timestamp"] = time.time()
        resp = client.get("/mfa/setup")
        assert resp.status_code == 302
        assert "/mfa/setup/choose" in resp.headers["Location"]


# ===========================================================================
# POST /mfa/setup/yubikey — success path (logging + redirect)
# ===========================================================================

class TestMfaSetupYubikeySuccess:

    def test_completes_login_on_successful_registration(self, client):
        """successful YubiKey registration completes login directly."""
        _set_mfa_pending(client, method="yubikey")
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch(
                 "asset_management.web.app.MFAService.register_yubikey",
                 return_value={"success": True},
             ), \
             mock.patch(
                 "asset_management.web.app.UserProvisioningService.get_user_metadata",
                 return_value=None,
             ), \
             mock.patch("asset_management.web.app.MFAService.update_last_used"):
            resp = client.post(
                "/mfa/setup/yubikey",
                data={"csrf_token": "test-csrf-token", "otp": "ccccccxxxxxx"},
            )
        assert resp.status_code == 302
        assert "mfa" not in resp.headers["Location"].lower()
