"""
Tests for the self-service profile routes.

Covers:
  GET  /profile                              — render profile page
  POST /profile/update-name                  — change full_name
  POST /profile/update-email                 — change email
  POST /profile/change-password              — self-service password change
  POST /profile/mfa/totp/setup               — start TOTP enrollment
  POST /profile/mfa/totp/confirm             — verify TOTP and complete
  POST /profile/mfa/yubikey/setup            — start YubiKey enrollment
  POST /profile/mfa/yubikey/register         — register YubiKey OTP
  POST /profile/mfa/disable                  — disable MFA
"""

from unittest import mock


_ENFORCE_MFA_PATH = "asset_management.web.config.Config.ENFORCE_MFA"


# ===========================================================================
# Auth gating — non-admin users CAN reach /profile
# ===========================================================================

class TestProfileAuthGating:

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/profile")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_org_user_can_reach_profile(self, org_user_client):
        """every authenticated user can manage their own profile."""
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None):
            resp = org_user_client.get("/profile")
        assert resp.status_code == 200

    def test_admin_can_reach_profile(self, auth_client):
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None):
            resp = auth_client.get("/profile")
        assert resp.status_code == 200


# ===========================================================================
# GET /profile — page rendering
# ===========================================================================

class TestProfilePage:

    def test_renders_with_no_mfa_record(self, auth_client):
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None):
            resp = auth_client.get("/profile")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "My Profile" in body
        assert "Test User" in body
        assert "test@example.com" in body
        assert "Not set up" in body

    def test_renders_back_link_with_referer_target(self, auth_client):
        """Back link href is the Referer the user came from (server-captured)."""
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None):
            resp = auth_client.get(
                "/profile",
                headers={"Referer": "http://localhost/assets/dashboards"},
            )
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert 'id="profile-back-link"' in body
        assert 'href="http://localhost/assets/dashboards"' in body

    def test_renders_back_link_with_admin_default_when_no_referer(self, auth_client):
        """No Referer + user has mc_* perms → fall back to /assets/."""
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None), \
             mock.patch(
                 "asset_management.web.app.RolePermissionsService.get_merged_permissions",
                 return_value={"mc_user_management": True},
             ):
            resp = auth_client.get("/profile")
        body = resp.data.decode("utf-8")
        assert 'href="/assets/"' in body

    def test_renders_back_link_with_dashboard_default_for_team_user(self, org_user_client):
        """No Referer + user has no mc_* perms → fall back to /dashboard."""
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None), \
             mock.patch(
                 "asset_management.web.app.RolePermissionsService.get_merged_permissions",
                 return_value={"db_management_console": False},
             ):
            resp = org_user_client.get("/profile")
        body = resp.data.decode("utf-8")
        assert 'href="/dashboard"' in body

    def test_back_link_ignores_self_referrer(self, auth_client):
        """A Referer pointing at /profile (e.g. form-submit redirect) doesn't
        overwrite the previously-captured entry point."""
        # Seed the session as if the user originally came from /assets/foo
        with auth_client.session_transaction() as sess:
            sess["_profile_back_url"] = "http://localhost/assets/foo"
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None):
            resp = auth_client.get(
                "/profile",
                headers={"Referer": "http://localhost/profile"},
            )
        body = resp.data.decode("utf-8")
        assert 'href="http://localhost/assets/foo"' in body

    def test_back_link_rejects_external_referrer(self, auth_client):
        """A Referer pointing at evil.com is ignored — same-origin only."""
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None), \
             mock.patch(
                 "asset_management.web.app.RolePermissionsService.get_merged_permissions",
                 return_value={"mc_user_management": True},
             ):
            resp = auth_client.get(
                "/profile",
                headers={"Referer": "https://evil.com/page"},
            )
        body = resp.data.decode("utf-8")
        assert "evil.com" not in body
        assert 'href="/assets/"' in body  # falls back to admin default

    def test_save_buttons_render_disabled_by_default(self, auth_client):
        """All three Save/Change buttons start disabled — the JS validator
        re-enables them once the form has valid + changed input."""
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None):
            resp = auth_client.get("/profile")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        for btn_id in ("profile-name-submit", "profile-email-submit", "profile-pw-submit"):
            # The button tag for each form has id=<btn_id> and the disabled attribute
            idx = body.find(f'id="{btn_id}"')
            assert idx >= 0, f"missing button {btn_id}"
            # search a small window after the id for "disabled"
            assert "disabled" in body[idx:idx + 200], f"{btn_id} should render disabled"

    def test_renders_with_pending_mfa(self, auth_client):
        with mock.patch(
            "asset_management.web.app.MFAService.get_mfa_record",
            return_value={"mfa_enabled": True, "mfa_method": None},
        ):
            resp = auth_client.get("/profile")
        assert resp.status_code == 200
        assert "Pending" in resp.data.decode("utf-8")

    def test_renders_with_active_totp(self, auth_client):
        with mock.patch(
            "asset_management.web.app.MFAService.get_mfa_record",
            return_value={"mfa_enabled": True, "mfa_method": "totp", "totp_confirmed": True},
        ):
            resp = auth_client.get("/profile")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Active" in body
        assert "Authenticator app" in body

    def test_shows_success_banner_from_query(self, auth_client):
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None):
            resp = auth_client.get("/profile?success=password")
        assert resp.status_code == 200
        assert "Password changed successfully" in resp.data.decode("utf-8")

    def test_hides_disable_button_when_enforce_mfa(self, auth_client):
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record",
                        return_value={"mfa_enabled": True, "mfa_method": "totp", "totp_confirmed": True}), \
             mock.patch(_ENFORCE_MFA_PATH, True):
            resp = auth_client.get("/profile")
        assert resp.status_code == 200
        assert "cannot be disabled" in resp.data.decode("utf-8")


# ===========================================================================
# POST /profile/update-name
# ===========================================================================

class TestProfileUpdateName:

    def test_rejects_empty_name(self, auth_client):
        resp = auth_client.post("/profile/update-name", data={"full_name": ""})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_too_long_name(self, auth_client):
        resp = auth_client.post("/profile/update-name", data={"full_name": "x" * 201})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_returns_error_when_es_unavailable(self, auth_client):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = auth_client.post("/profile/update-name", data={"full_name": "New Name"})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_returns_error_when_user_not_found(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.return_value = {}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.post("/profile/update-name", data={"full_name": "New Name"})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_successful_update_writes_to_es_and_session(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.return_value = {
            "testuser": {"roles": ["asset_admin"], "full_name": "Old", "email": "test@example.com", "metadata": {}}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = auth_client.post("/profile/update-name", data={"full_name": "Brand New Name"})
        assert resp.status_code == 302
        assert "success=name" in resp.headers["Location"]
        mock_es.security.put_user.assert_called_once()
        kwargs = mock_es.security.put_user.call_args.kwargs
        assert kwargs["full_name"] == "Brand New Name"
        assert kwargs["email"] == "test@example.com"
        assert kwargs["roles"] == ["asset_admin"]


# ===========================================================================
# POST /profile/update-email
# ===========================================================================

class TestProfileUpdateEmail:

    def test_rejects_empty_email(self, auth_client):
        resp = auth_client.post("/profile/update-email", data={"email": ""})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_invalid_email(self, auth_client):
        resp = auth_client.post("/profile/update-email", data={"email": "not-an-email"})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_email_with_whitespace(self, auth_client):
        resp = auth_client.post("/profile/update-email", data={"email": "bad email@example.com"})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_email_without_tld(self, auth_client):
        resp = auth_client.post("/profile/update-email", data={"email": "user@localhost"})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_oversize_email(self, auth_client):
        # 256 chars total — exceeds the 254 limit
        long_email = "a" * 245 + "@example.com"
        assert len(long_email) > 254
        resp = auth_client.post("/profile/update-email", data={"email": long_email})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_successful_update(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.return_value = {
            "testuser": {"roles": ["asset_admin"], "full_name": "Test", "email": "old@example.com", "metadata": {}}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = auth_client.post("/profile/update-email", data={"email": "new@example.com"})
        assert resp.status_code == 302
        assert "success=email" in resp.headers["Location"]
        kwargs = mock_es.security.put_user.call_args.kwargs
        assert kwargs["email"] == "new@example.com"


# ===========================================================================
# POST /profile/change-password
# ===========================================================================

class TestProfileChangePassword:

    def _post(self, client, **kwargs):
        data = {
            "current_password": "OldPass1!",
            "new_password": "NewPass123!",
            "confirm_password": "NewPass123!",
        }
        data.update(kwargs)
        return client.post("/profile/change-password", data=data)

    def test_rejects_empty_current_password(self, auth_client):
        resp = self._post(auth_client, current_password="")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_short_new_password(self, auth_client):
        resp = self._post(auth_client, new_password="short", confirm_password="short")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_oversize_new_password(self, auth_client):
        oversize = "A" * 129
        resp = self._post(auth_client, new_password=oversize, confirm_password=oversize)
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_when_new_matches_current(self, auth_client):
        resp = self._post(auth_client, current_password="Same123!",
                          new_password="Same123!", confirm_password="Same123!")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_when_confirmation_mismatched(self, auth_client):
        resp = self._post(auth_client, confirm_password="Different123!")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_wrong_current_password(self, auth_client):
        with mock.patch("lib.es_auth.authenticate_user", return_value={"success": False}), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = self._post(auth_client, current_password="WrongPass!")
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_returns_error_when_es_unavailable(self, auth_client):
        with mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = self._post(auth_client)
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_returns_error_when_es_change_fails(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.security.change_password.side_effect = Exception("ES error")
        with mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = self._post(auth_client)
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_successful_change_redirects_with_success(self, auth_client):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"), \
             mock.patch("asset_management.web.app.create_kibana_session", return_value=None):
            resp = self._post(auth_client)
        assert resp.status_code == 302
        assert "success=password" in resp.headers["Location"]
        mock_es.security.change_password.assert_called_once_with(
            username="testuser", body={"password": "NewPass123!"}
        )

    def test_successful_change_sets_kibana_cookie(self, auth_client):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"), \
             mock.patch("asset_management.web.app.create_kibana_session", return_value="sid-value"):
            resp = self._post(auth_client)
        assert resp.status_code == 302
        assert "sid" in resp.headers.get("Set-Cookie", "")


# ===========================================================================
# POST /profile/mfa/totp/setup + /profile/mfa/totp/confirm
# ===========================================================================

class TestProfileMfaTotp:

    def test_setup_renders_qr_and_stages_secret_in_session(self, auth_client):
        """Setup must NOT write to ES — only stage in session. So Cancel is a no-op."""
        with mock.patch(
            "asset_management.web.app.MFAService.build_totp_setup_payload",
            return_value={"secret_enc": "encrypted-blob",
                          "qr_code_b64": "ZmFrZS1xcg==",
                          "totp_secret": "ABC123"},
        ):
            resp = auth_client.post("/profile/mfa/totp/setup")
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Set up authenticator app" in body
        assert "ABC123" in body
        with auth_client.session_transaction() as sess:
            assert sess.get("_totp_setup_secret_enc") == "encrypted-blob"

    def test_setup_renders_confirm_button_disabled(self, auth_client):
        """Confirm button starts disabled — JS enables on a valid 6-digit code."""
        with mock.patch(
            "asset_management.web.app.MFAService.build_totp_setup_payload",
            return_value={"secret_enc": "x", "qr_code_b64": "x", "totp_secret": "ABC"},
        ):
            resp = auth_client.post("/profile/mfa/totp/setup")
        body = resp.data.decode("utf-8")
        idx = body.find('id="profile-totp-confirm-btn"')
        assert idx >= 0
        assert "disabled" in body[idx:idx + 200]

    def test_confirm_redirects_to_login_when_session_secret_missing(self, auth_client):
        """If user posts confirm without staging (e.g., session expired), error out."""
        resp = auth_client.post("/profile/mfa/totp/confirm", data={"code": "123456"})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_confirm_writes_to_es_only_after_valid_code(self, auth_client):
        """Confirmation is the moment the MFA record is committed."""
        with auth_client.session_transaction() as sess:
            sess["_totp_setup_secret_enc"] = "encrypted-blob"
        commit_mock = mock.MagicMock(return_value={"success": True})
        with mock.patch(
            "asset_management.web.app.MFAService.verify_totp_code_against_secret",
            return_value=True,
        ), mock.patch(
            "asset_management.web.app.MFAService.commit_self_mfa_setup", commit_mock,
        ), mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = auth_client.post("/profile/mfa/totp/confirm", data={"code": "123456"})
        assert resp.status_code == 302
        assert "success=mfa" in resp.headers["Location"]
        commit_mock.assert_called_once_with("testuser", "totp", totp_secret_enc="encrypted-blob")
        with auth_client.session_transaction() as sess:
            assert "_totp_setup_secret_enc" not in sess

    def test_confirm_re_renders_with_same_secret_on_invalid_code(self, auth_client):
        """Invalid code re-renders the form WITHOUT regenerating the secret —
        the user's authenticator app still holds the original entry."""
        with auth_client.session_transaction() as sess:
            sess["_totp_setup_secret_enc"] = "encrypted-blob"
        rebuild = mock.MagicMock(return_value={
            "secret_enc": "encrypted-blob",
            "qr_code_b64": "ZmFrZS1xcg==",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        })
        with mock.patch(
            "asset_management.web.app.MFAService.verify_totp_code_against_secret",
            return_value=False,
        ), mock.patch(
            "asset_management.web.app.MFAService.build_totp_setup_payload", rebuild,
        ):
            resp = auth_client.post("/profile/mfa/totp/confirm", data={"code": "000000"})
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "Invalid code" in body
        assert "JBSWY3DPEHPK3PXP" in body  # same secret echoed back
        # build_totp_setup_payload was called with the staged secret, not without
        rebuild.assert_called_once_with("testuser", existing_secret_enc="encrypted-blob")
        with auth_client.session_transaction() as sess:
            # Secret is still staged — user can retry without re-scanning
            assert sess.get("_totp_setup_secret_enc") == "encrypted-blob"

    def test_profile_get_clears_stale_setup_secret(self, auth_client):
        """Landing back on /profile drops any in-flight TOTP staging."""
        with auth_client.session_transaction() as sess:
            sess["_totp_setup_secret_enc"] = "stale-blob"
        with mock.patch("asset_management.web.app.MFAService.get_mfa_record", return_value=None):
            resp = auth_client.get("/profile")
        assert resp.status_code == 200
        with auth_client.session_transaction() as sess:
            assert "_totp_setup_secret_enc" not in sess


# ===========================================================================
# POST /profile/mfa/yubikey/setup + /profile/mfa/yubikey/register
# ===========================================================================

class TestProfileMfaYubikey:

    def test_setup_renders_otp_form_without_es_write(self, auth_client):
        """Setup just renders the form — no MFAService method writes called."""
        resp = auth_client.post("/profile/mfa/yubikey/setup")
        assert resp.status_code == 200
        assert "Register your YubiKey" in resp.data.decode("utf-8")

    def test_setup_renders_register_button_disabled(self, auth_client):
        """Register button starts disabled — JS enables on a 44-char modhex OTP."""
        resp = auth_client.post("/profile/mfa/yubikey/setup")
        body = resp.data.decode("utf-8")
        idx = body.find('id="profile-yubikey-register-btn"')
        assert idx >= 0
        assert "disabled" in body[idx:idx + 200]

    def test_register_writes_to_es_only_after_valid_otp(self, auth_client):
        commit_mock = mock.MagicMock(return_value={"success": True})
        with mock.patch(
            "asset_management.web.app.MFAService.validate_yubikey_otp_for_registration",
            return_value={"success": True, "public_id": "ccccccfedcba", "error": None},
        ), mock.patch(
            "asset_management.web.app.MFAService.commit_self_mfa_setup", commit_mock,
        ), mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = auth_client.post("/profile/mfa/yubikey/register", data={"otp": "c" * 44})
        assert resp.status_code == 302
        assert "success=mfa" in resp.headers["Location"]
        commit_mock.assert_called_once_with("testuser", "yubikey", yubikey_public_id="ccccccfedcba")

    def test_register_re_renders_on_failure(self, auth_client):
        with mock.patch(
            "asset_management.web.app.MFAService.validate_yubikey_otp_for_registration",
            return_value={"success": False, "public_id": None, "error": "Invalid OTP"},
        ):
            resp = auth_client.post("/profile/mfa/yubikey/register", data={"otp": "bad"})
        assert resp.status_code == 200
        assert "Invalid OTP" in resp.data.decode("utf-8")

    def test_register_handles_commit_failure(self, auth_client):
        with mock.patch(
            "asset_management.web.app.MFAService.validate_yubikey_otp_for_registration",
            return_value={"success": True, "public_id": "ccccccfedcba", "error": None},
        ), mock.patch(
            "asset_management.web.app.MFAService.commit_self_mfa_setup",
            return_value={"success": False, "error": "ES error"},
        ):
            resp = auth_client.post("/profile/mfa/yubikey/register", data={"otp": "c" * 44})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]


# ===========================================================================
# POST /profile/mfa/disable
# ===========================================================================

class TestProfileMfaDisable:

    def test_blocked_when_enforce_mfa_on(self, auth_client):
        with mock.patch(_ENFORCE_MFA_PATH, True):
            resp = auth_client.post("/profile/mfa/disable", data={"current_password": "x"})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_empty_password(self, auth_client):
        with mock.patch(_ENFORCE_MFA_PATH, False):
            resp = auth_client.post("/profile/mfa/disable", data={"current_password": ""})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_rejects_wrong_password(self, auth_client):
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": False}), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = auth_client.post("/profile/mfa/disable", data={"current_password": "wrong"})
        assert resp.status_code == 302
        assert "error=" in resp.headers["Location"]

    def test_successful_disable(self, auth_client):
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("asset_management.web.app.MFAService.admin_disable_mfa",
                        return_value={"success": True}), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = auth_client.post("/profile/mfa/disable", data={"current_password": "right"})
        assert resp.status_code == 302
        assert "success=mfa-disabled" in resp.headers["Location"]
