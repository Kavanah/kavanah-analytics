"""
Tests for the User Provisioning Service.

Covers:
  _get_smtp_settings              — file loading, missing file, error
  generate_temp_password           — length, character requirements
  create_user                      — success, ES unavailable, ES error
  send_welcome_email               — TLS, SSL, missing SMTP keys, failure, app_url
  get_user_metadata                — success, ES unavailable, ES error
  clear_force_password_change      — success, ES unavailable, ES error
"""

import json
import string
from unittest import mock

import pytest

from asset_management.web.services.user_provisioning_service import (
    UserProvisioningService,
    _get_smtp_settings,
)


# ---------------------------------------------------------------------------
# _get_smtp_settings
# ---------------------------------------------------------------------------

class TestGetSmtpSettings:

    def test_returns_settings_from_file(self):
        settings = {"smtp_host": "mail.example.com", "smtp_port": "587"}
        with mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(settings))):
            result = _get_smtp_settings()
        assert result == settings

    def test_returns_empty_dict_on_file_not_found(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            result = _get_smtp_settings()
        assert result == {}

    def test_returns_empty_dict_on_generic_error(self):
        with mock.patch("builtins.open", side_effect=Exception("disk error")):
            result = _get_smtp_settings()
        assert result == {}


# ---------------------------------------------------------------------------
# generate_temp_password
# ---------------------------------------------------------------------------

class TestGenerateTempPassword:

    def test_length_is_16(self):
        pw = UserProvisioningService.generate_temp_password()
        assert len(pw) == 16

    def test_contains_uppercase(self):
        pw = UserProvisioningService.generate_temp_password()
        assert any(c in string.ascii_uppercase for c in pw)

    def test_contains_lowercase(self):
        pw = UserProvisioningService.generate_temp_password()
        assert any(c in string.ascii_lowercase for c in pw)

    def test_contains_digit(self):
        pw = UserProvisioningService.generate_temp_password()
        assert any(c in string.digits for c in pw)

    def test_only_alphanumeric(self):
        pw = UserProvisioningService.generate_temp_password()
        assert all(c in string.ascii_letters + string.digits for c in pw)

    def test_unique_per_call(self):
        passwords = {UserProvisioningService.generate_temp_password() for _ in range(10)}
        assert len(passwords) == 10


# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------

class TestCreateUser:

    def test_success(self):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.create_user("testuser", "Test User", "test@example.com", ["role1"])
        assert result["success"] is True
        assert len(result["temp_password"]) == 16
        mock_es.security.put_user.assert_called_once()
        call_kwargs = mock_es.security.put_user.call_args
        assert call_kwargs[1]["username"] == "testuser"

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = UserProvisioningService.create_user("testuser", "Test User", "test@example.com", ["role1"])
        assert result["success"] is False
        assert "unavailable" in result["error"].lower()

    def test_es_exception(self):
        mock_es = mock.MagicMock()
        mock_es.security.put_user.side_effect = Exception("conflict")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.create_user("testuser", "Test User", "test@example.com", ["role1"])
        assert result["success"] is False
        # ES exception text stays in the server log, never the response.
        assert result["error"] == "Could not create user."


# ---------------------------------------------------------------------------
# send_welcome_email
# ---------------------------------------------------------------------------

_SMTP_SETTINGS = {
    "smtp_host": "smtp.example.com",
    "smtp_port": "587",
    "smtp_username": "user",
    "smtp_password": "pass",
    "from_email": "noreply@example.com",
    "smtp_use_tls": True,
    "app_url": "https://example.com/reset-password",
}


class TestSendWelcomeEmail:

    def _patch_smtp(self, settings=None):
        settings = settings or _SMTP_SETTINGS
        return mock.patch(
            "asset_management.web.services.user_provisioning_service._get_smtp_settings",
            return_value=settings,
        )

    def test_success_with_tls(self):
        mock_server = mock.MagicMock()
        with self._patch_smtp(), \
             mock.patch("asset_management.web.services.user_provisioning_service.smtplib.SMTP", return_value=mock_server):
            result = UserProvisioningService.send_welcome_email(
                "test@example.com", "testuser", "temppass123", "Test User",
            )
        assert result["success"] is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once()
        mock_server.sendmail.assert_called_once()
        mock_server.quit.assert_called_once()

    def test_success_with_ssl(self):
        settings = {**_SMTP_SETTINGS, "smtp_use_tls": False}
        mock_server = mock.MagicMock()
        with self._patch_smtp(settings), \
             mock.patch("asset_management.web.services.user_provisioning_service.smtplib.SMTP_SSL", return_value=mock_server):
            result = UserProvisioningService.send_welcome_email(
                "test@example.com", "testuser", "temppass123",
            )
        assert result["success"] is True
        mock_server.login.assert_called_once()

    def test_missing_smtp_keys(self):
        settings = {"smtp_host": "mail.example.com"}  # missing required keys
        with self._patch_smtp(settings):
            result = UserProvisioningService.send_welcome_email(
                "test@example.com", "testuser", "temppass123",
            )
        assert result["success"] is False
        assert "SMTP not configured" in result["error"]

    def test_smtp_send_failure(self):
        mock_server = mock.MagicMock()
        mock_server.sendmail.side_effect = Exception("SMTP error")
        with self._patch_smtp(), \
             mock.patch("asset_management.web.services.user_provisioning_service.smtplib.SMTP", return_value=mock_server):
            result = UserProvisioningService.send_welcome_email(
                "test@example.com", "testuser", "temppass123",
            )
        assert result["success"] is False
        # SMTP exception text stays in the server log, never the response.
        assert result["error"] == "Could not send the welcome email."

    def test_without_app_url(self):
        settings = {**_SMTP_SETTINGS, "app_url": ""}
        mock_server = mock.MagicMock()
        with self._patch_smtp(settings), \
             mock.patch("asset_management.web.services.user_provisioning_service.smtplib.SMTP", return_value=mock_server):
            result = UserProvisioningService.send_welcome_email(
                "test@example.com", "testuser", "temppass123",
            )
        assert result["success"] is True

    def test_email_contains_username_and_password(self):
        mock_server = mock.MagicMock()
        with self._patch_smtp(), \
             mock.patch("asset_management.web.services.user_provisioning_service.smtplib.SMTP", return_value=mock_server):
            UserProvisioningService.send_welcome_email(
                "test@example.com", "myuser", "mypass123",
            )
        sent_msg = mock_server.sendmail.call_args[0][2]
        assert "myuser" in sent_msg
        assert "mypass123" in sent_msg

    def test_with_full_name(self):
        mock_server = mock.MagicMock()
        with self._patch_smtp(), \
             mock.patch("asset_management.web.services.user_provisioning_service.smtplib.SMTP", return_value=mock_server):
            UserProvisioningService.send_welcome_email(
                "test@example.com", "testuser", "pass", "John Doe",
            )
        sent_msg = mock_server.sendmail.call_args[0][2]
        assert "John Doe" in sent_msg


# ---------------------------------------------------------------------------
# get_user_metadata
# ---------------------------------------------------------------------------

class TestGetUserMetadata:

    def test_success(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.return_value = {
            "testuser": {"metadata": {"force_password_change": True}, "roles": ["admin"]},
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.get_user_metadata("testuser")
        assert result == {"force_password_change": True}

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = UserProvisioningService.get_user_metadata("testuser")
        assert result is None

    def test_es_exception(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.side_effect = Exception("not found")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.get_user_metadata("testuser")
        assert result is None


# ---------------------------------------------------------------------------
# clear_force_password_change
# ---------------------------------------------------------------------------

class TestClearForcePasswordChange:

    def test_success(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.return_value = {
            "testuser": {
                "metadata": {"force_password_change": True, "other_key": "value"},
                "roles": ["role1"],
                "full_name": "Test User",
                "email": "test@example.com",
            },
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.clear_force_password_change("testuser")
        assert result is True
        # Verify put_user called with metadata where force_password_change is False
        put_call = mock_es.security.put_user.call_args
        assert put_call[1]["metadata"]["force_password_change"] is False
        assert put_call[1]["metadata"]["other_key"] == "value"

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = UserProvisioningService.clear_force_password_change("testuser")
        assert result is False

    def test_es_exception(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.side_effect = Exception("error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.clear_force_password_change("testuser")
        assert result is False


# ---------------------------------------------------------------------------
# reset_password
# ---------------------------------------------------------------------------

class TestResetPassword:

    def _mock_es_with_user(self, user_data=None):
        mock_es = mock.MagicMock()
        data = user_data if user_data is not None else {
            "metadata": {},
            "roles": ["viewer"],
            "full_name": "Alice",
            "email": "alice@example.com",
        }
        mock_es.security.get_user.return_value = {"alice": data}
        return mock_es

    def test_success_returns_new_temp_password(self):
        mock_es = self._mock_es_with_user()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.reset_password("alice")
        assert result["success"] is True
        assert len(result["temp_password"]) == 16
        assert result["email"] == "alice@example.com"
        assert result["full_name"] == "Alice"

    def test_changes_password_in_elasticsearch(self):
        mock_es = self._mock_es_with_user()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.reset_password("alice")
        mock_es.security.change_password.assert_called_once()
        call = mock_es.security.change_password.call_args
        assert call[1]["username"] == "alice"
        assert call[1]["body"]["password"] == result["temp_password"]

    def test_sets_force_password_change_flag(self):
        mock_es = self._mock_es_with_user()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            UserProvisioningService.reset_password("alice")
        put_call = mock_es.security.put_user.call_args
        assert put_call[1]["metadata"]["force_password_change"] is True

    def test_preserves_other_metadata(self):
        mock_es = self._mock_es_with_user({
            "metadata": {"custom": "value", "force_password_change": False},
            "roles": ["viewer"],
            "full_name": "",
            "email": "",
        })
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            UserProvisioningService.reset_password("alice")
        put_call = mock_es.security.put_user.call_args
        assert put_call[1]["metadata"]["custom"] == "value"
        assert put_call[1]["metadata"]["force_password_change"] is True

    def test_rejects_reserved_user(self):
        mock_es = self._mock_es_with_user({"metadata": {"_reserved": True}})
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.reset_password("alice")
        assert result["success"] is False
        assert "built-in" in result["error"].lower()
        mock_es.security.change_password.assert_not_called()

    def test_rejects_unknown_user(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.return_value = {}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.reset_password("alice")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = UserProvisioningService.reset_password("alice")
        assert result["success"] is False

    def test_change_password_failure_surfaces_error(self):
        mock_es = self._mock_es_with_user()
        mock_es.security.change_password.side_effect = Exception("ES error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = UserProvisioningService.reset_password("alice")
        assert result["success"] is False
        # ES exception text stays in the server log, never the response.
        assert result["error"] == "Could not reset the password."


# ---------------------------------------------------------------------------
# send_password_reset_email
# ---------------------------------------------------------------------------

class TestSendPasswordResetEmail:

    def _patch_smtp(self, settings=None):
        settings = settings or _SMTP_SETTINGS
        return mock.patch(
            "asset_management.web.services.user_provisioning_service._get_smtp_settings",
            return_value=settings,
        )

    def test_success_with_tls(self):
        mock_server = mock.MagicMock()
        with self._patch_smtp(), \
             mock.patch("asset_management.web.services.user_provisioning_service.smtplib.SMTP", return_value=mock_server):
            result = UserProvisioningService.send_password_reset_email(
                "test@example.com", "testuser", "temppass123", "Test User",
            )
        assert result["success"] is True
        mock_server.sendmail.assert_called_once()

    def test_email_contains_username_and_password(self):
        mock_server = mock.MagicMock()
        with self._patch_smtp(), \
             mock.patch("asset_management.web.services.user_provisioning_service.smtplib.SMTP", return_value=mock_server):
            UserProvisioningService.send_password_reset_email(
                "test@example.com", "myuser", "mypass123",
            )
        sent_msg = mock_server.sendmail.call_args[0][2]
        assert "myuser" in sent_msg
        assert "mypass123" in sent_msg

    def test_subject_signals_reset(self):
        """Distinguish the reset email from the welcome email in the inbox."""
        mock_server = mock.MagicMock()
        with self._patch_smtp(), \
             mock.patch("asset_management.web.services.user_provisioning_service.smtplib.SMTP", return_value=mock_server):
            UserProvisioningService.send_password_reset_email(
                "test@example.com", "testuser", "pass",
            )
        sent_msg = mock_server.sendmail.call_args[0][2]
        assert "Password Has Been Reset" in sent_msg

    def test_missing_smtp_keys(self):
        settings = {"smtp_host": "mail.example.com"}
        with self._patch_smtp(settings):
            result = UserProvisioningService.send_password_reset_email(
                "test@example.com", "testuser", "pass",
            )
        assert result["success"] is False

    def test_smtp_send_failure(self):
        mock_server = mock.MagicMock()
        mock_server.sendmail.side_effect = Exception("boom")
        with self._patch_smtp(), \
             mock.patch("asset_management.web.services.user_provisioning_service.smtplib.SMTP", return_value=mock_server):
            result = UserProvisioningService.send_password_reset_email(
                "test@example.com", "testuser", "pass",
            )
        assert result["success"] is False
        # SMTP exception text stays in the server log, never the response.
        assert result["error"] == "Could not send the password-reset email."
