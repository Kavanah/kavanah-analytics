"""
Tests for MFA challenge and setup Flask routes.

Routes covered:
  GET  /mfa/challenge              mfa_challenge_page
  POST /mfa/challenge              mfa_challenge_post
  GET  /mfa/setup                  mfa_setup_page
  GET  /mfa/setup/totp             mfa_setup_totp_generate
  POST /mfa/setup/totp/confirm     mfa_setup_totp_confirm
  POST /mfa/setup/yubikey          mfa_setup_yubikey_register
  POST /api/user-management/enable-mfa   api_enable_mfa
  POST /api/user-management/disable-mfa  api_disable_mfa

All MFA challenge/setup routes require mfa_pending=True in session.
Admin MFA enable/disable routes require asset_admin or superuser role.
"""

import time
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mfa_client(app, method="totp", username="testuser"):
    """Create a test client with mfa_pending session state."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["mfa_pending"] = True
        sess["mfa_pending_username"] = username
        sess["mfa_pending_method"] = method
        sess["mfa_pending_timestamp"] = time.time()
        sess["mfa_pending_roles"] = ["asset_admin"]
        sess["mfa_pending_full_name"] = "Test User"
        sess["mfa_pending_email"] = "test@example.com"
        sess["mfa_pending_kibana_sid"] = "test-kibana-sid"
        sess["csrf_token"] = "test-csrf-token"
    return c


# ===========================================================================
# GET /mfa/challenge
# ===========================================================================


class TestMfaChallengePageGet:

    def test_redirects_to_login_without_mfa_pending(self, client):
        resp = client.get("/mfa/challenge")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_renders_challenge_page_with_mfa_pending(self, app):
        c = _mfa_client(app)
        resp = c.get("/mfa/challenge")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "csrf" in html.lower() or "code" in html.lower()

    def test_renders_with_totp_method(self, app):
        c = _mfa_client(app, method="totp")
        resp = c.get("/mfa/challenge")
        assert resp.status_code == 200

    def test_renders_with_yubikey_method(self, app):
        c = _mfa_client(app, method="yubikey")
        resp = c.get("/mfa/challenge")
        assert resp.status_code == 200

    def test_displays_error_from_query_param(self, app):
        c = _mfa_client(app)
        resp = c.get("/mfa/challenge?error=Test+error+message")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Test error message" in html


# ===========================================================================
# Tier 2 — email-recovery consume reachable mid-challenge
# ===========================================================================


class TestEmailRecoveryConsumeWithMfaPending:
    """The recovery-link + verify-link are clicked in the SAME browser that is
    still mid-challenge (mfa_pending live). The mfa_pending gate must let them
    through — not bounce to /mfa/challenge before the route runs. Regression
    guard for the staging bug where clicking the link landed on the 2FA page."""

    def test_recovery_consume_not_bounced_by_mfa_pending(self, app):
        c = _mfa_client(app)
        # Bad token -> the route renders its result page (200). The point is it
        # is NOT a 302 to /mfa/challenge (which is what the gate did before).
        resp = c.get("/mfa/recovery/email/badtoken", follow_redirects=False)
        assert resp.status_code == 200
        if resp.status_code in (301, 302):  # defensive — must never bounce to challenge
            assert "/mfa/challenge" not in resp.headers.get("Location", "")

    def test_recovery_email_verify_not_bounced_by_mfa_pending(self, app):
        c = _mfa_client(app)
        resp = c.get("/profile/mfa/recovery-email/verify/badtoken", follow_redirects=False)
        assert resp.status_code == 200
        if resp.status_code in (301, 302):
            assert "/mfa/challenge" not in resp.headers.get("Location", "")


# ===========================================================================
# POST /mfa/challenge
# ===========================================================================


class TestMfaChallengePost:

    def test_redirects_to_login_without_mfa_pending(self, client):
        resp = client.post("/mfa/challenge", data={"code": "123456"})
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_invalid_csrf_returns_error(self, app):
        c = _mfa_client(app)
        resp = c.post(
            "/mfa/challenge",
            data={
                "code": "123456",
                "csrf_token": "wrong-token",
            },
        )
        # Should redirect back with error (invalid CSRF)
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_valid_totp_code_completes_login(self, app):
        c = _mfa_client(app, method="totp")
        with (
            mock.patch("asset_management.web.app.MFAService.validate_totp_code", return_value=True),
            mock.patch("asset_management.web.app.AuditLogService.log_event"),
            mock.patch("asset_management.web.app.UserProvisioningService.get_user_metadata", return_value=None),
            mock.patch("asset_management.web.app.MFAService.update_last_used"),
            mock.patch("asset_management.web.app.create_kibana_session", return_value=None),
        ):
            resp = c.post(
                "/mfa/challenge",
                data={
                    "code": "123456",
                    "csrf_token": "test-csrf-token",
                },
            )
        assert resp.status_code == 302
        # Should redirect to home, not back to MFA or login
        location = resp.headers["Location"]
        assert "mfa" not in location.lower()
        assert "login" not in location.lower()

    def test_valid_totp_sets_authenticated_session(self, app):
        c = _mfa_client(app, method="totp")
        with (
            mock.patch("asset_management.web.app.MFAService.validate_totp_code", return_value=True),
            mock.patch("asset_management.web.app.AuditLogService.log_event"),
            mock.patch("asset_management.web.app.UserProvisioningService.get_user_metadata", return_value=None),
            mock.patch("asset_management.web.app.MFAService.update_last_used"),
            mock.patch("asset_management.web.app.create_kibana_session", return_value=None),
        ):
            c.post(
                "/mfa/challenge",
                data={
                    "code": "123456",
                    "csrf_token": "test-csrf-token",
                },
            )
        with c.session_transaction() as sess:
            assert sess.get("authenticated") is True
            assert sess.get("username") == "testuser"
            assert "mfa_pending" not in sess

    def test_mfa_next_dashboard_not_prefixed_with_assets(self, app):
        """/dashboard is a top-level route reserved for non-admin
        users — it must NOT be rewritten to /assets/dashboard, which is
        reserved for the management console. Only fires when the /assets
        reverse-proxy prefix is active (USE_PREFIX=True in production)."""
        c = _mfa_client(app, method="totp")
        with c.session_transaction() as sess:
            sess["mfa_pending_next"] = "/dashboard"
        with (
            mock.patch("asset_management.web.app.USE_PREFIX", True),
            mock.patch("asset_management.web.app.MFAService.validate_totp_code", return_value=True),
            mock.patch("asset_management.web.app.AuditLogService.log_event"),
            mock.patch("asset_management.web.app.UserProvisioningService.get_user_metadata", return_value=None),
            mock.patch("asset_management.web.app.MFAService.update_last_used"),
        ):
            resp = c.post(
                "/mfa/challenge",
                data={
                    "code": "123456",
                    "csrf_token": "test-csrf-token",
                },
            )
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/dashboard"

    def test_invalid_totp_code_shows_error(self, app):
        c = _mfa_client(app, method="totp")
        with (
            mock.patch("asset_management.web.app.MFAService.validate_totp_code", return_value=False),
            mock.patch("asset_management.web.app.AuditLogService.log_event"),
            mock.patch("asset_management.web.auth.record_failed_attempt"),
        ):
            resp = c.post(
                "/mfa/challenge",
                data={
                    "code": "000000",
                    "csrf_token": "test-csrf-token",
                },
            )
        # Should re-render challenge page with error (200, not redirect)
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "invalid" in html.lower() or "error" in html.lower()

    def test_valid_yubikey_otp_completes_login(self, app):
        c = _mfa_client(app, method="yubikey")
        mock_record = {"yubikey_public_id": "cccccccccccc"}
        with (
            mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=mock_record),
            mock.patch("asset_management.web.app.MFAService.validate_yubikey_otp", return_value=True),
            mock.patch("asset_management.web.app.AuditLogService.log_event"),
            mock.patch("asset_management.web.app.UserProvisioningService.get_user_metadata", return_value=None),
            mock.patch("asset_management.web.app.MFAService.update_last_used"),
            mock.patch("asset_management.web.app.create_kibana_session", return_value=None),
        ):
            resp = c.post(
                "/mfa/challenge",
                data={
                    "code": "ccccccccccccabcdefghijklmnopqrst1234",
                    "csrf_token": "test-csrf-token",
                },
            )
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "login" not in location.lower()

    def test_invalid_yubikey_otp_shows_error(self, app):
        c = _mfa_client(app, method="yubikey")
        mock_record = {"yubikey_public_id": "cccccccccccc"}
        with (
            mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=mock_record),
            mock.patch("asset_management.web.app.MFAService.validate_yubikey_otp", return_value=False),
            mock.patch("asset_management.web.app.AuditLogService.log_event"),
            mock.patch("asset_management.web.auth.record_failed_attempt"),
        ):
            resp = c.post(
                "/mfa/challenge",
                data={
                    "code": "invalid-otp",
                    "csrf_token": "test-csrf-token",
                },
            )
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "invalid" in html.lower() or "error" in html.lower()

    def test_yubikey_no_record_fails(self, app):
        c = _mfa_client(app, method="yubikey")
        with (
            mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None),
            mock.patch("asset_management.web.app.AuditLogService.log_event"),
            mock.patch("asset_management.web.auth.record_failed_attempt"),
        ):
            resp = c.post(
                "/mfa/challenge",
                data={
                    "code": "some-otp",
                    "csrf_token": "test-csrf-token",
                },
            )
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "invalid" in html.lower() or "error" in html.lower()

    def test_forced_password_change_after_mfa(self, app):
        """If user has force_password_change, redirect to change-password after MFA."""
        c = _mfa_client(app, method="totp")
        with (
            mock.patch("asset_management.web.app.MFAService.validate_totp_code", return_value=True),
            mock.patch("asset_management.web.app.AuditLogService.log_event"),
            mock.patch(
                "asset_management.web.app.UserProvisioningService.get_user_metadata",
                return_value={"force_password_change": True},
            ),
        ):
            resp = c.post(
                "/mfa/challenge",
                data={
                    "code": "123456",
                    "csrf_token": "test-csrf-token",
                },
            )
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"].lower()


# ===========================================================================
# GET /mfa/setup
# ===========================================================================


class TestMfaSetupPage:

    def test_redirects_to_login_without_mfa_pending(self, client):
        resp = client.get("/mfa/setup")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_totp_method_redirects_to_totp_generate(self, app):
        c = _mfa_client(app, method="totp")
        resp = c.get("/mfa/setup")
        assert resp.status_code == 302
        assert "totp" in resp.headers["Location"].lower()

    def test_yubikey_method_renders_yubikey_setup(self, app):
        c = _mfa_client(app, method="yubikey")
        resp = c.get("/mfa/setup")
        assert resp.status_code == 200

    def test_unset_method_redirects_to_picker(self, app):
        """when the admin enables MFA without choosing a method,
        /mfa/setup routes the user to the method picker."""
        c = _mfa_client(app, method=None)
        resp = c.get("/mfa/setup")
        assert resp.status_code == 302
        assert "/mfa/setup/choose" in resp.headers["Location"]


# ===========================================================================
# GET /mfa/setup/choose
# ===========================================================================


class TestMfaSetupChooseGet:

    def test_redirects_to_login_without_mfa_pending(self, client):
        resp = client.get("/mfa/setup/choose")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_renders_picker_when_mfa_pending(self, app):
        c = _mfa_client(app, method=None)
        resp = c.get("/mfa/setup/choose")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Authenticator app" in html
        assert "YubiKey" in html


# ===========================================================================
# POST /mfa/setup/choose
# ===========================================================================


class TestMfaSetupChoosePost:

    def test_rejected_without_mfa_pending(self, client):
        """No mfa_pending session → CSRF guard fires (no session token to compare)
        and returns 403; either way the request is rejected."""
        resp = client.post("/mfa/setup/choose", data={"method": "totp", "csrf_token": "x"})
        assert resp.status_code in (302, 403)

    def test_invalid_csrf_rejected(self, app):
        c = _mfa_client(app, method=None)
        resp = c.post("/mfa/setup/choose", data={"method": "totp", "csrf_token": "wrong"})
        assert resp.status_code == 403

    def test_invalid_method_redirects_back_to_picker(self, app):
        c = _mfa_client(app, method=None)
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True):
            resp = c.post("/mfa/setup/choose", data={"method": "sms", "csrf_token": "test-csrf-token"})
        assert resp.status_code == 302
        assert "/mfa/setup/choose" in resp.headers["Location"]

    def test_valid_totp_choice_persists_and_redirects(self, app):
        c = _mfa_client(app, method=None)
        with (
            mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True),
            mock.patch("asset_management.web.app.MFAService.set_mfa_method", return_value={"success": True}) as m,
        ):
            resp = c.post("/mfa/setup/choose", data={"method": "totp", "csrf_token": "test-csrf-token"})
        assert resp.status_code == 302
        m.assert_called_once_with("testuser", "totp")
        with c.session_transaction() as sess:
            assert sess["mfa_pending_method"] == "totp"

    def test_valid_yubikey_choice_persists_and_redirects(self, app):
        c = _mfa_client(app, method=None)
        with (
            mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True),
            mock.patch("asset_management.web.app.MFAService.set_mfa_method", return_value={"success": True}),
        ):
            resp = c.post("/mfa/setup/choose", data={"method": "yubikey", "csrf_token": "test-csrf-token"})
        assert resp.status_code == 302
        with c.session_transaction() as sess:
            assert sess["mfa_pending_method"] == "yubikey"

    def test_set_method_failure_redirects_back_to_picker(self, app):
        c = _mfa_client(app, method=None)
        with (
            mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True),
            mock.patch("asset_management.web.app.MFAService.set_mfa_method", return_value={"success": False, "error": "boom"}),
        ):
            resp = c.post("/mfa/setup/choose", data={"method": "totp", "csrf_token": "test-csrf-token"})
        assert resp.status_code == 302
        assert "/mfa/setup/choose" in resp.headers["Location"]


# ===========================================================================
# GET /mfa/setup/totp
# ===========================================================================


class TestMfaSetupTotpGenerate:

    def test_redirects_to_login_without_mfa_pending(self, client):
        resp = client.get("/mfa/setup/totp")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_generates_totp_and_renders_qr(self, app):
        c = _mfa_client(app, method="totp")
        mock_result = {"qr_code_b64": "base64data==", "totp_secret": "JBSWY3DPEHPK3PXP"}
        with mock.patch("asset_management.web.app.MFAService.generate_totp_secret", return_value=mock_result):
            resp = c.get("/mfa/setup/totp")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "base64data" in html or "JBSWY3DPEHPK3PXP" in html


# ===========================================================================
# POST /mfa/setup/totp/confirm
# ===========================================================================


class TestMfaSetupTotpConfirm:

    def test_redirects_to_login_without_mfa_pending(self, client):
        resp = client.post("/mfa/setup/totp/confirm", data={"code": "123456"})
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_invalid_csrf_redirects_with_error(self, app):
        c = _mfa_client(app)
        resp = c.post(
            "/mfa/setup/totp/confirm",
            data={
                "code": "123456",
                "csrf_token": "wrong-token",
            },
        )
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_valid_code_completes_login_without_second_challenge(self, app):
        """a valid setup code completes login; no second MFA prompt."""
        c = _mfa_client(app, method="totp")
        with (
            mock.patch("asset_management.web.app.MFAService.verify_and_confirm_totp", return_value=True),
            mock.patch("asset_management.web.app.UserProvisioningService.get_user_metadata", return_value=None),
            mock.patch("asset_management.web.app.MFAService.update_last_used"),
        ):
            resp = c.post(
                "/mfa/setup/totp/confirm",
                data={
                    "code": "123456",
                    "csrf_token": "test-csrf-token",
                },
            )
        assert resp.status_code == 302
        location = resp.headers["Location"].lower()
        assert "mfa" not in location
        assert "login" not in location
        with c.session_transaction() as sess:
            assert sess.get("authenticated") is True
            assert sess.get("username") == "testuser"
            assert "mfa_pending" not in sess

    def test_invalid_code_re_renders_setup_with_error(self, app):
        c = _mfa_client(app, method="totp")
        mock_result = {"qr_code_b64": "base64data==", "totp_secret": "JBSWY3DPEHPK3PXP"}
        with (
            mock.patch("asset_management.web.app.MFAService.verify_and_confirm_totp", return_value=False),
            mock.patch("asset_management.web.app.MFAService.generate_totp_secret", return_value=mock_result),
        ):
            resp = c.post(
                "/mfa/setup/totp/confirm",
                data={
                    "code": "000000",
                    "csrf_token": "test-csrf-token",
                },
            )
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "invalid" in html.lower() or "error" in html.lower()


# ===========================================================================
# POST /mfa/setup/yubikey
# ===========================================================================


class TestMfaSetupYubikeyRegister:

    def test_redirects_to_login_without_mfa_pending(self, client):
        resp = client.post("/mfa/setup/yubikey", data={"otp": "some-otp"})
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_invalid_csrf_redirects_with_error(self, app):
        c = _mfa_client(app, method="yubikey")
        resp = c.post(
            "/mfa/setup/yubikey",
            data={
                "otp": "some-otp",
                "csrf_token": "wrong-token",
            },
        )
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_successful_registration_completes_login_without_second_challenge(self, app):
        """a successful YubiKey registration completes login; no second MFA prompt."""
        c = _mfa_client(app, method="yubikey")
        with (
            mock.patch("asset_management.web.app.MFAService.register_yubikey", return_value={"success": True}),
            mock.patch("asset_management.web.app.UserProvisioningService.get_user_metadata", return_value=None),
            mock.patch("asset_management.web.app.MFAService.update_last_used"),
        ):
            resp = c.post(
                "/mfa/setup/yubikey",
                data={
                    "otp": "ccccccccccccabcdefghijklmnopqrst1234",
                    "csrf_token": "test-csrf-token",
                },
            )
        assert resp.status_code == 302
        location = resp.headers["Location"].lower()
        assert "mfa" not in location
        assert "login" not in location
        with c.session_transaction() as sess:
            assert sess.get("authenticated") is True
            assert sess.get("username") == "testuser"
            assert "mfa_pending" not in sess

    def test_failed_registration_re_renders_with_error(self, app):
        c = _mfa_client(app, method="yubikey")
        with mock.patch(
            "asset_management.web.app.MFAService.register_yubikey", return_value={"success": False, "error": "Invalid OTP"}
        ):
            resp = c.post(
                "/mfa/setup/yubikey",
                data={
                    "otp": "invalid-otp",
                    "csrf_token": "test-csrf-token",
                },
            )
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Invalid OTP" in html


# ===========================================================================
# POST /api/user-management/enable-mfa (admin route)
# ===========================================================================


class TestAdminEnableMfa:

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.post("/api/user-management/enable-mfa", json={"username": "user1", "method": "totp"})
        assert resp.status_code == 401
        assert "login" in (resp.get_json() or {}).get("login_url", "").lower()

    def test_non_admin_returns_403(self, org_user_client):
        resp = org_user_client.post("/api/user-management/enable-mfa", json={"username": "user1", "method": "totp"})
        assert resp.status_code == 403

    def test_missing_username_returns_400(self, auth_client):
        resp = auth_client.post("/api/user-management/enable-mfa", json={"username": ""})
        assert resp.status_code == 400

    def test_valid_request_enables_mfa(self, auth_client):
        with mock.patch("asset_management.web.app.MFAService.admin_enable_mfa", return_value={"success": True}) as m:
            resp = auth_client.post("/api/user-management/enable-mfa", json={"username": "user1"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        m.assert_called_once_with("user1", enabled_by="testuser")

    def test_superuser_can_enable_mfa(self, superuser_client):
        with mock.patch("asset_management.web.app.MFAService.admin_enable_mfa", return_value={"success": True}):
            resp = superuser_client.post("/api/user-management/enable-mfa", json={"username": "user1"})
        assert resp.status_code == 200

    def test_method_in_request_body_is_ignored(self, auth_client):
        """Legacy clients that still send `method` must not break."""
        with mock.patch("asset_management.web.app.MFAService.admin_enable_mfa", return_value={"success": True}) as m:
            resp = auth_client.post("/api/user-management/enable-mfa", json={"username": "user1", "method": "totp"})
        assert resp.status_code == 200
        m.assert_called_once_with("user1", enabled_by="testuser")


# ===========================================================================
# POST /api/user-management/disable-mfa (admin route)
# ===========================================================================


class TestAdminDisableMfa:

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.post("/api/user-management/disable-mfa", json={"username": "user1"})
        assert resp.status_code == 401
        assert "login" in (resp.get_json() or {}).get("login_url", "").lower()

    def test_non_admin_returns_403(self, org_user_client):
        resp = org_user_client.post("/api/user-management/disable-mfa", json={"username": "user1"})
        assert resp.status_code == 403

    def test_asset_admin_returns_403(self, auth_client):
        """Disable 2FA requires superuser, not just asset_admin."""
        resp = auth_client.post("/api/user-management/disable-mfa", json={"username": "user1"})
        assert resp.status_code == 403

    def test_missing_username_returns_400(self, superuser_client):
        resp = superuser_client.post("/api/user-management/disable-mfa", json={"username": ""})
        assert resp.status_code == 400

    def test_superuser_can_disable_mfa(self, superuser_client):
        with mock.patch("asset_management.web.app.MFAService.admin_disable_mfa", return_value={"success": True}) as m:
            resp = superuser_client.post("/api/user-management/disable-mfa", json={"username": "user1"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        m.assert_called_once_with("user1")


# ===========================================================================
# POST /api/user-management/set-mfa-bypass
# ===========================================================================


class TestSetMfaBypass:

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.post("/api/user-management/set-mfa-bypass", json={"username": "user1", "enabled": True, "reason": "x"})
        assert resp.status_code == 401
        assert "login" in (resp.get_json() or {}).get("login_url", "").lower()

    def test_asset_admin_returns_403(self, auth_client):
        """Bypass requires superuser, not just asset_admin."""
        resp = auth_client.post(
            "/api/user-management/set-mfa-bypass", json={"username": "user1", "enabled": True, "reason": "x"}
        )
        assert resp.status_code == 403

    def test_missing_username_returns_400(self, superuser_client):
        resp = superuser_client.post(
            "/api/user-management/set-mfa-bypass", json={"username": "", "enabled": True, "reason": "x"}
        )
        assert resp.status_code == 400

    def test_non_boolean_enabled_returns_400(self, superuser_client):
        resp = superuser_client.post(
            "/api/user-management/set-mfa-bypass", json={"username": "alice", "enabled": "yes", "reason": "x"}
        )
        assert resp.status_code == 400

    def test_missing_reason_on_enable_returns_400(self, superuser_client):
        """Reason is mandatory when turning the bypass ON (audit discipline)."""
        resp = superuser_client.post(
            "/api/user-management/set-mfa-bypass", json={"username": "alice", "enabled": True, "reason": ""}
        )
        assert resp.status_code == 400

    def test_reason_not_required_when_disabling(self, superuser_client):
        with mock.patch("asset_management.web.app.MFAService.set_mfa_bypass", return_value={"success": True}):
            resp = superuser_client.post("/api/user-management/set-mfa-bypass", json={"username": "alice", "enabled": False})
        assert resp.status_code == 200

    def test_superuser_can_enable_bypass(self, superuser_client):
        with (
            mock.patch("asset_management.web.app.MFAService.set_mfa_bypass", return_value={"success": True}) as m,
            mock.patch("asset_management.web.app.AuditLogService.log_event"),
        ):
            resp = superuser_client.post(
                "/api/user-management/set-mfa-bypass",
                json={"username": "alice", "enabled": True, "reason": "lost yubikey"},
            )
        assert resp.status_code == 200
        m.assert_called_once_with(username="alice", enabled=True, set_by="superuser", reason="lost yubikey")

    def test_successful_enable_writes_audit_event(self, superuser_client):
        """Bypass actions must surface in the Login Activity tab."""
        with (
            mock.patch("asset_management.web.app.MFAService.set_mfa_bypass", return_value={"success": True}),
            mock.patch("asset_management.web.app.AuditLogService.log_event") as audit,
        ):
            superuser_client.post(
                "/api/user-management/set-mfa-bypass",
                json={"username": "alice", "enabled": True, "reason": "lost key"},
            )
        audit.assert_called_once()
        args, kwargs = audit.call_args
        assert args[0] == "superuser"
        assert args[1] == "mfa_bypass_enable"
        assert args[2] is True
        assert kwargs["target_user"] == "alice"
        assert kwargs["detail"] == "lost key"

    def test_successful_disable_writes_audit_event(self, superuser_client):
        with (
            mock.patch("asset_management.web.app.MFAService.set_mfa_bypass", return_value={"success": True}),
            mock.patch("asset_management.web.app.AuditLogService.log_event") as audit,
        ):
            superuser_client.post(
                "/api/user-management/set-mfa-bypass",
                json={"username": "alice", "enabled": False},
            )
        audit.assert_called_once()
        args, kwargs = audit.call_args
        assert args[1] == "mfa_bypass_disable"
        assert kwargs["detail"] == ""

    def test_failed_bypass_does_not_write_audit_event(self, superuser_client):
        """Only successful bypass operations are audit-logged."""
        with (
            mock.patch("asset_management.web.app.MFAService.set_mfa_bypass", return_value={"success": False, "error": "boom"}),
            mock.patch("asset_management.web.app.AuditLogService.log_event") as audit,
        ):
            superuser_client.post(
                "/api/user-management/set-mfa-bypass",
                json={"username": "alice", "enabled": True, "reason": "x"},
            )
        audit.assert_not_called()
