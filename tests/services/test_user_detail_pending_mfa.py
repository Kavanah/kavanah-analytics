"""
Regression test.

When an admin pre-enables MFA for a user (via the "Enable 2FA" button on the
user-management page) but the user has not yet logged in to pick TOTP or
YubiKey, the MFA record stores ``mfa_method=None``. The user-detail endpoint
used to forward that ``None`` straight to JSON, and the sidebar JS then
crashed on ``null.toUpperCase()`` when rendering the 2FA badge — surfacing as
a generic "Error" in the sidebar.

The fix coerces ``method`` to an empty string and exposes ``setup_complete``
so the sidebar can render a "Pending" badge instead.
"""

import os
import sys
from unittest import mock

# Path setup mirrors tests/services/test_mfa_service.py
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.services.user_management_service import UserManagementService


def _make_es(security_user, mfa_record=None, audit_hits=None):
    """Mock ES with the three calls get_user_detail makes."""
    es = mock.MagicMock()
    es.security.get_user.return_value = {"alice": security_user}

    def _search(index=None, body=None, size=None, **kwargs):
        if index == "de_asset_user_mfa":
            hits = [{"_source": mfa_record}] if mfa_record else []
            return {"hits": {"hits": hits}}
        if index == "de_audit_log":
            return {"hits": {"hits": audit_hits or []}}
        return {"hits": {"hits": []}}

    es.search.side_effect = _search
    return es


_BASE_USER = {
    "username": "alice",
    "full_name": "Alice",
    "email": "a@b.com",
    "roles": ["asset_admin"],
    "enabled": True,
    "metadata": {},
}


class TestGetUserDetailMfaPending:

    def test_method_is_empty_string_when_admin_pre_enabled_mfa(self):
        """mfa_method=None must be coerced to '' so the JS toUpperCase guard never fires on null."""
        record = {
            "username": "alice",
            "mfa_enabled": True,
            "mfa_method": None,
            "totp_confirmed": False,
            "yubikey_confirmed": False,
            "last_used": None,
        }
        es = _make_es(_BASE_USER, mfa_record=record)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserManagementService.get_user_detail("alice")

        assert result["success"] is True
        assert result["mfa"]["enabled"] is True
        assert result["mfa"]["method"] == ""
        assert result["mfa"]["method"] is not None
        assert result["mfa"]["setup_complete"] is False
        assert result["mfa"]["last_used"] == ""

    def test_setup_complete_true_for_confirmed_totp(self):
        record = {
            "username": "alice",
            "mfa_enabled": True,
            "mfa_method": "totp",
            "totp_confirmed": True,
            "yubikey_confirmed": False,
            "last_used": "2026-04-28T00:00:00Z",
        }
        es = _make_es(_BASE_USER, mfa_record=record)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserManagementService.get_user_detail("alice")

        assert result["mfa"]["method"] == "totp"
        assert result["mfa"]["setup_complete"] is True
        assert result["mfa"]["last_used"] == "2026-04-28T00:00:00Z"

    def test_setup_complete_true_for_confirmed_yubikey(self):
        record = {
            "username": "alice",
            "mfa_enabled": True,
            "mfa_method": "yubikey",
            "totp_confirmed": False,
            "yubikey_confirmed": True,
        }
        es = _make_es(_BASE_USER, mfa_record=record)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserManagementService.get_user_detail("alice")

        assert result["mfa"]["method"] == "yubikey"
        assert result["mfa"]["setup_complete"] is True

    def test_setup_complete_false_when_method_picked_but_not_confirmed(self):
        record = {
            "username": "alice",
            "mfa_enabled": True,
            "mfa_method": "totp",
            "totp_confirmed": False,
            "yubikey_confirmed": False,
        }
        es = _make_es(_BASE_USER, mfa_record=record)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserManagementService.get_user_detail("alice")

        assert result["mfa"]["method"] == "totp"
        assert result["mfa"]["setup_complete"] is False

    def test_no_mfa_record_returns_disabled_state(self):
        """Brand-new user with no MFA record at all (the original 'never logged in' scenario)."""
        es = _make_es(_BASE_USER, mfa_record=None)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserManagementService.get_user_detail("alice")

        assert result["mfa"]["enabled"] is False
        assert result["mfa"]["method"] == ""
        assert result["mfa"]["setup_complete"] is False
        assert result["mfa"]["bypass_mfa"] is False
        assert result["login_history"] == []

    def test_bypass_flag_is_exposed(self):
        record = {
            "username": "alice",
            "mfa_enabled": True,
            "mfa_method": None,
            "bypass_mfa": True,
        }
        es = _make_es(_BASE_USER, mfa_record=record)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = UserManagementService.get_user_detail("alice")

        assert result["mfa"]["bypass_mfa"] is True
        assert result["mfa"]["method"] == ""
