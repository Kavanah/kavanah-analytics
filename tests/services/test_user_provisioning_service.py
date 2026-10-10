"""
Tests for UserProvisioningService — admin password operations.

Focused on the Phase 3 work: admin_set_password lets an admin set
an explicit password without sending email, for service / demo accounts.
"""

import logging
from unittest import mock

import pytest

from asset_management.web.services.user_provisioning_service import UserProvisioningService


# ---------------------------------------------------------------------------
# validate_admin_password_complexity
# ---------------------------------------------------------------------------


class TestValidateAdminPasswordComplexity:
    """Complexity rules: >=12 chars, upper, lower, digit, special."""

    def test_returns_none_for_valid_password(self):
        assert UserProvisioningService.validate_admin_password_complexity("Abcdef1!ghij") is None

    def test_rejects_short_password(self):
        err = UserProvisioningService.validate_admin_password_complexity("Ab1!xyz")
        assert err and "12 characters" in err

    def test_rejects_missing_uppercase(self):
        err = UserProvisioningService.validate_admin_password_complexity("abcdef1!ghij")
        assert err and "uppercase" in err

    def test_rejects_missing_lowercase(self):
        err = UserProvisioningService.validate_admin_password_complexity("ABCDEF1!GHIJ")
        assert err and "lowercase" in err

    def test_rejects_missing_digit(self):
        err = UserProvisioningService.validate_admin_password_complexity("Abcdefg!hijk")
        assert err and "digit" in err

    def test_rejects_missing_special(self):
        err = UserProvisioningService.validate_admin_password_complexity("Abcdef1ghijk")
        assert err and "special" in err

    def test_rejects_non_string(self):
        err = UserProvisioningService.validate_admin_password_complexity(None)
        assert err and "string" in err


# ---------------------------------------------------------------------------
# admin_set_password
# ---------------------------------------------------------------------------


class TestAdminSetPassword:
    """admin_set_password() side-effects, error handling, audit logging."""

    def test_returns_error_when_username_blank(self):
        result = UserProvisioningService.admin_set_password(
            username="", new_password="Abcdef1!ghij", set_by="admin", reason="test"
        )
        assert result == {"success": False, "error": "Username is required"}

    def test_returns_error_when_set_by_blank(self):
        result = UserProvisioningService.admin_set_password(
            username="alice", new_password="Abcdef1!ghij", set_by="", reason="test"
        )
        assert result == {"success": False, "error": "set_by is required"}

    def test_returns_error_when_reason_blank(self):
        result = UserProvisioningService.admin_set_password(
            username="alice", new_password="Abcdef1!ghij", set_by="admin", reason="   "
        )
        assert result == {"success": False, "error": "Audit reason is required"}

    def test_returns_complexity_error_for_weak_password(self):
        result = UserProvisioningService.admin_set_password(
            username="alice", new_password="weak", set_by="admin", reason="test"
        )
        assert result["success"] is False
        assert "12 characters" in result["error"]

    def test_returns_error_when_es_unavailable(self):
        with mock.patch(
            "asset_management.web.services.user_provisioning_service.get_elasticsearch_client",
            return_value=None,
            create=True,
        ), mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = UserProvisioningService.admin_set_password(
                username="alice", new_password="Abcdef1!ghij", set_by="admin", reason="test"
            )
        assert result == {"success": False, "error": "Elasticsearch unavailable"}

    def test_returns_error_when_user_not_found(self):
        es = mock.MagicMock()
        es.security.get_user.return_value = {}  # username key absent
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserProvisioningService.admin_set_password(
                username="ghost", new_password="Abcdef1!ghij", set_by="admin", reason="test"
            )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_refuses_built_in_user(self):
        es = mock.MagicMock()
        es.security.get_user.return_value = {
            "elastic": {"metadata": {"_reserved": True}, "roles": ["superuser"]}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserProvisioningService.admin_set_password(
                username="elastic", new_password="Abcdef1!ghij", set_by="admin", reason="test"
            )
        assert result["success"] is False
        assert "built-in" in result["error"]
        es.security.change_password.assert_not_called()

    def test_calls_change_password_with_explicit_value(self):
        es = mock.MagicMock()
        es.security.get_user.return_value = {
            "alice": {"metadata": {}, "roles": ["team_Kavanah Team"], "email": "alice@example.com"}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserProvisioningService.admin_set_password(
                username="alice", new_password="Abcdef1!ghij", set_by="admin", reason="demo seed"
            )
        assert result == {"success": True}
        es.security.change_password.assert_called_once_with(
            username="alice", body={"password": "Abcdef1!ghij"}
        )

    def test_clears_force_password_change_atomically(self):
        """admin_set_password clears force_password_change in the same call.

        Service / demo accounts need a stable known password — re-prompting on
        next login defeats the purpose. The flag is cleared atomically so the
        admin doesn't have to remember a second step.
        """
        es = mock.MagicMock()
        es.security.get_user.return_value = {
            "alice": {
                "metadata": {"force_password_change": True, "other": "preserved"},
                "roles": ["team_X"],
                "full_name": "Alice",
                "email": "alice@example.com",
            }
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserProvisioningService.admin_set_password(
                username="alice", new_password="Abcdef1!ghij", set_by="admin", reason="seed"
            )
        assert result == {"success": True}
        # change_password was called.
        es.security.change_password.assert_called_once()
        # put_user was called once with force_password_change=False, preserving
        # other metadata, roles, full_name, and email.
        es.security.put_user.assert_called_once()
        kwargs = es.security.put_user.call_args.kwargs
        assert kwargs["username"] == "alice"
        assert kwargs["roles"] == ["team_X"]
        assert kwargs["full_name"] == "Alice"
        assert kwargs["email"] == "alice@example.com"
        assert kwargs["metadata"]["force_password_change"] is False
        assert kwargs["metadata"]["other"] == "preserved"

    def test_returns_error_when_clearing_force_flag_fails(self):
        """If change_password succeeds but put_user fails, surface the error.

        The user CAN log in with the new password, but will be re-prompted to
        change it. The admin needs to know so they can retry or fix manually.
        """
        es = mock.MagicMock()
        es.security.get_user.return_value = {
            "alice": {"metadata": {"force_password_change": True}, "roles": [], "email": ""}
        }
        es.security.put_user.side_effect = RuntimeError("metadata write failed")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserProvisioningService.admin_set_password(
                username="alice", new_password="Abcdef1!ghij", set_by="admin", reason="seed"
            )
        assert result["success"] is False
        assert "clear force_password_change" in result["error"]
        # change_password was still called — the partial success is real.
        es.security.change_password.assert_called_once()

    def test_writes_audit_line_with_set_by_and_reason(self, caplog):
        es = mock.MagicMock()
        es.security.get_user.return_value = {
            "alice": {"metadata": {}, "roles": [], "email": ""}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es), \
                caplog.at_level(logging.INFO):
            UserProvisioningService.admin_set_password(
                username="alice", new_password="Abcdef1!ghij", set_by="admin99", reason="kavanah_demo seed"
            )
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "AUDIT:" in joined
        assert "action=admin_set_password" in joined
        assert "username=alice" in joined
        assert "set_by=admin99" in joined
        assert "reason=kavanah_demo seed" in joined

    def test_audit_line_does_not_contain_password(self, caplog):
        es = mock.MagicMock()
        es.security.get_user.return_value = {
            "alice": {"metadata": {}, "roles": [], "email": ""}
        }
        secret = "ZZqwerty99!@#"
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es), \
                caplog.at_level(logging.INFO):
            UserProvisioningService.admin_set_password(
                username="alice", new_password=secret, set_by="admin", reason="seed"
            )
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert secret not in joined

    def test_returns_error_when_change_password_fails(self):
        es = mock.MagicMock()
        es.security.get_user.return_value = {
            "alice": {"metadata": {}, "roles": [], "email": ""}
        }
        es.security.change_password.side_effect = RuntimeError("ES boom")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserProvisioningService.admin_set_password(
                username="alice", new_password="Abcdef1!ghij", set_by="admin", reason="seed"
            )
        assert result["success"] is False
        # ES exception text stays in the server log, never the response.
        assert result["error"] == "Could not set the password."
