"""
Tests for MFAService in services/mfa_service.py.

All Elasticsearch and external API calls are mocked.
"""

import sys
import os
import pytest
from unittest import mock

# Path setup
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.services.mfa_service import MFAService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_es(get_return=None, search_return=None, raises=None):
    """Return a mock ES client."""
    es = mock.MagicMock()
    if raises:
        es.get.side_effect = raises
        es.search.side_effect = raises
    else:
        if get_return is not None:
            es.get.return_value = get_return
        if search_return is not None:
            es.search.return_value = search_return
    return es


MFA_RECORD = {
    "username": "alice",
    "mfa_enabled": True,
    "mfa_method": "totp",
    "totp_secret_enc": "enc:secret",
    "totp_confirmed": True,
    "yubikey_public_id": None,
    "yubikey_confirmed": False,
}

YUBIKEY_RECORD = {
    "username": "bob",
    "mfa_enabled": True,
    "mfa_method": "yubikey",
    "totp_secret_enc": None,
    "totp_confirmed": False,
    "yubikey_public_id": "ccccccxxxxxx",
    "yubikey_confirmed": True,
}


# ===========================================================================
# ensure_index_exists
# ===========================================================================

class TestEnsureIndexExists:

    def test_creates_index_when_missing(self):
        es = mock.MagicMock()
        es.indices.exists.return_value = False
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            MFAService.ensure_index_exists()
        es.indices.create.assert_called_once()

    def test_skips_creation_when_index_already_exists(self):
        es = mock.MagicMock()
        es.indices.exists.return_value = True
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            MFAService.ensure_index_exists()
        es.indices.create.assert_not_called()

    def test_handles_no_es_connection(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            MFAService.ensure_index_exists()  # Should not raise

    def test_ignores_already_exists_exception(self):
        es = mock.MagicMock()
        es.indices.exists.return_value = False
        es.indices.create.side_effect = Exception("resource_already_exists_exception foo")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            MFAService.ensure_index_exists()  # Should not raise

    def test_logs_unexpected_exception(self):
        es = mock.MagicMock()
        es.indices.exists.return_value = False
        es.indices.create.side_effect = Exception("unexpected disk error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            MFAService.ensure_index_exists()  # Should not raise


# ===========================================================================
# get_mfa_record
# ===========================================================================

class TestGetMfaRecord:

    def test_returns_record_when_found(self):
        es = _make_es(get_return={"_source": MFA_RECORD})
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.get_mfa_record("alice")
        assert result["username"] == "alice"

    def test_returns_none_when_not_found(self):
        es = _make_es(raises=Exception("not found"))
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.get_mfa_record("nobody")
        assert result is None

    def test_returns_none_when_no_es(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.get_mfa_record("alice")
        assert result is None


# ===========================================================================
# is_mfa_required
# ===========================================================================

_ENFORCE_MFA_PATH = "asset_management.web.config.Config.ENFORCE_MFA"


class TestIsMfaRequired:
    """earlier opt-in behavior — tested with enforce_mfa=false."""

    def test_returns_not_required_when_no_record(self):
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=None):
            result = MFAService.is_mfa_required("alice")
        assert result["required"] is False

    def test_returns_not_required_when_mfa_disabled(self):
        record = {**MFA_RECORD, "mfa_enabled": False}
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=record):
            result = MFAService.is_mfa_required("alice")
        assert result["required"] is False

    def test_returns_required_totp_setup_complete(self):
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=MFA_RECORD):
            result = MFAService.is_mfa_required("alice")
        assert result["required"] is True
        assert result["method"] == "totp"
        assert result["setup_complete"] is True

    def test_returns_required_totp_setup_incomplete(self):
        record = {**MFA_RECORD, "totp_confirmed": False}
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=record):
            result = MFAService.is_mfa_required("alice")
        assert result["setup_complete"] is False

    def test_returns_required_yubikey_setup_complete(self):
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=YUBIKEY_RECORD):
            result = MFAService.is_mfa_required("bob")
        assert result["method"] == "yubikey"
        assert result["setup_complete"] is True

    def test_returns_setup_incomplete_for_unknown_method(self):
        record = {**MFA_RECORD, "mfa_method": "sms"}
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=record):
            result = MFAService.is_mfa_required("alice")
        assert result["setup_complete"] is False


# ===========================================================================
# list_all_users_with_mfa
# ===========================================================================

class TestListAllUsersWithMfa:

    def _make_es_with_users(self, users_dict, mfa_hits=None):
        es = mock.MagicMock()
        es.security.get_user.return_value = users_dict
        es.search.return_value = {
            "hits": {
                "hits": mfa_hits or []
            }
        }
        return es

    def test_returns_empty_list_when_no_es(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.list_all_users_with_mfa()
        assert result == []

    def test_returns_empty_list_when_es_user_fetch_fails(self):
        es = mock.MagicMock()
        es.security.get_user.side_effect = Exception("network error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.list_all_users_with_mfa()
        assert result == []

    def test_skips_reserved_users(self):
        users = {
            ".kibana_system": {"metadata": {"_reserved": True}, "roles": [], "full_name": ""},
            "alice": {"metadata": {}, "roles": ["asset_admin"], "full_name": "Alice"},
        }
        es = self._make_es_with_users(users)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.list_all_users_with_mfa()
        usernames = [u["username"] for u in result]
        assert "alice" in usernames
        assert ".kibana_system" not in usernames

    def test_joins_mfa_records(self):
        users = {
            "alice": {"metadata": {}, "roles": [], "full_name": "Alice", "email": "a@b.com"},
        }
        mfa_hits = [{
            "_id": "alice",
            "_source": {"username": "alice", "mfa_enabled": True, "mfa_method": "totp",
                        "totp_confirmed": True, "last_used": "2024-01-01"},
        }]
        es = self._make_es_with_users(users, mfa_hits)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.list_all_users_with_mfa()
        assert result[0]["mfa_enabled"] is True
        assert result[0]["setup_complete"] is True

    def test_handles_missing_mfa_index_gracefully(self):
        users = {
            "alice": {"metadata": {}, "roles": [], "full_name": "Alice", "email": ""},
        }
        es = mock.MagicMock()
        es.security.get_user.return_value = users
        es.search.side_effect = Exception("index not found")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.list_all_users_with_mfa()
        assert result[0]["mfa_enabled"] is False

    def test_results_sorted_by_username(self):
        users = {
            "zoe": {"metadata": {}, "roles": [], "full_name": "Zoe", "email": ""},
            "alice": {"metadata": {}, "roles": [], "full_name": "Alice", "email": ""},
        }
        es = self._make_es_with_users(users)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.list_all_users_with_mfa()
        assert result[0]["username"] == "alice"
        assert result[1]["username"] == "zoe"


# ===========================================================================
# admin_enable_mfa
# ===========================================================================

class TestAdminEnableMfa:

    def test_returns_success_true_on_ok(self):
        es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.admin_enable_mfa("alice", enabled_by="admin")
        assert result["success"] is True

    def test_writes_record_with_null_method(self):
        es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            MFAService.admin_enable_mfa("alice", enabled_by="admin")
        doc = es.index.call_args[1]["document"]
        assert doc["mfa_enabled"] is True
        assert doc["mfa_method"] is None

    def test_returns_error_when_no_es(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.admin_enable_mfa("alice", enabled_by="admin")
        assert result["success"] is False

    def test_returns_error_on_es_exception(self):
        es = mock.MagicMock()
        es.index.side_effect = Exception("write failed")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.admin_enable_mfa("alice", enabled_by="admin")
        assert result["success"] is False
        assert "error" in result


class TestSetMfaMethod:

    def test_persists_chosen_method(self):
        es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.set_mfa_method("alice", "totp")
        assert result["success"] is True
        body = es.update.call_args[1]["body"]
        assert body["doc"]["mfa_method"] == "totp"

    def test_rejects_unknown_method(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock.MagicMock()):
            result = MFAService.set_mfa_method("alice", "sms")
        assert result["success"] is False

    def test_returns_error_when_no_es(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.set_mfa_method("alice", "totp")
        assert result["success"] is False


# ===========================================================================
# admin_disable_mfa
# ===========================================================================

class TestAdminDisableMfa:

    def test_returns_success_true_on_ok(self):
        es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.admin_disable_mfa("alice")
        assert result["success"] is True

    def test_returns_error_when_no_es(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.admin_disable_mfa("alice")
        assert result["success"] is False

    def test_returns_error_on_es_exception(self):
        es = mock.MagicMock()
        es.delete.side_effect = Exception("delete failed")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.admin_disable_mfa("alice")
        assert result["success"] is False


# ===========================================================================
# set_mfa_bypass
# ===========================================================================

class TestSetMfaBypass:

    def test_enable_persists_reason_and_actor(self):
        es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.set_mfa_bypass(
                "alice", enabled=True, set_by="admin", reason="lost yubikey"
            )
        assert result["success"] is True
        body = es.update.call_args[1]["body"]
        assert body["doc_as_upsert"] is True
        assert body["doc"]["bypass_mfa"] is True
        assert body["doc"]["bypass_reason"] == "lost yubikey"
        assert body["doc"]["bypass_set_by"] == "admin"
        assert body["doc"]["bypass_set_at"]  # timestamp written

    def test_disable_clears_bypass_metadata(self):
        es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.set_mfa_bypass(
                "alice", enabled=False, set_by="admin", reason=""
            )
        assert result["success"] is True
        body = es.update.call_args[1]["body"]
        assert body["doc"]["bypass_mfa"] is False
        assert body["doc"]["bypass_reason"] is None
        assert body["doc"]["bypass_set_by"] is None

    def test_rejects_non_boolean_enabled(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock.MagicMock()):
            result = MFAService.set_mfa_bypass("alice", enabled="yes", set_by="admin")
        assert result["success"] is False

    def test_returns_error_when_no_es(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.set_mfa_bypass("alice", enabled=True, set_by="admin", reason="r")
        assert result["success"] is False


# ===========================================================================
# validate_totp_code
# ===========================================================================

class TestValidateTotpCode:

    def test_returns_false_when_no_record(self):
        with mock.patch.object(MFAService, "get_mfa_record", return_value=None):
            assert MFAService.validate_totp_code("alice", "123456") is False

    def test_returns_false_when_no_secret(self):
        with mock.patch.object(MFAService, "get_mfa_record",
                               return_value={"totp_secret_enc": None}):
            assert MFAService.validate_totp_code("alice", "123456") is False

    def test_returns_true_for_valid_code(self):
        with mock.patch.object(MFAService, "get_mfa_record",
                               return_value={"totp_secret_enc": "enc:secret"}), \
             mock.patch("lib.encryption.decrypt", return_value="JBSWY3DP"), \
             mock.patch("pyotp.TOTP") as mock_totp_cls:
            mock_totp_cls.return_value.verify.return_value = True
            result = MFAService.validate_totp_code("alice", "123456")
        assert result is True

    def test_returns_false_for_invalid_code(self):
        with mock.patch.object(MFAService, "get_mfa_record",
                               return_value={"totp_secret_enc": "enc:secret"}), \
             mock.patch("lib.encryption.decrypt", return_value="JBSWY3DP"), \
             mock.patch("pyotp.TOTP") as mock_totp_cls:
            mock_totp_cls.return_value.verify.return_value = False
            result = MFAService.validate_totp_code("alice", "000000")
        assert result is False

    def test_returns_false_on_decrypt_exception(self):
        with mock.patch.object(MFAService, "get_mfa_record",
                               return_value={"totp_secret_enc": "enc:secret"}), \
             mock.patch("lib.encryption.decrypt", side_effect=Exception("bad key")):
            result = MFAService.validate_totp_code("alice", "123456")
        assert result is False


# ===========================================================================
# verify_and_confirm_totp
# ===========================================================================

class TestVerifyAndConfirmTotp:

    def test_returns_false_when_no_record(self):
        with mock.patch.object(MFAService, "get_mfa_record", return_value=None):
            assert MFAService.verify_and_confirm_totp("alice", "123456") is False

    def test_returns_false_when_no_secret(self):
        with mock.patch.object(MFAService, "get_mfa_record",
                               return_value={"totp_secret_enc": None}):
            assert MFAService.verify_and_confirm_totp("alice", "123456") is False

    def test_returns_false_for_invalid_code(self):
        with mock.patch.object(MFAService, "get_mfa_record",
                               return_value={"totp_secret_enc": "enc:secret"}), \
             mock.patch("lib.encryption.decrypt", return_value="JBSWY3DP"), \
             mock.patch("pyotp.TOTP") as mock_totp_cls:
            mock_totp_cls.return_value.verify.return_value = False
            result = MFAService.verify_and_confirm_totp("alice", "000000")
        assert result is False

    def test_returns_true_and_updates_es_on_valid_code(self):
        es = mock.MagicMock()
        with mock.patch.object(MFAService, "get_mfa_record",
                               return_value={"totp_secret_enc": "enc:secret"}), \
             mock.patch("lib.encryption.decrypt", return_value="JBSWY3DP"), \
             mock.patch("pyotp.TOTP") as mock_totp_cls, \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            mock_totp_cls.return_value.verify.return_value = True
            result = MFAService.verify_and_confirm_totp("alice", "123456")
        assert result is True
        es.update.assert_called_once()


# ===========================================================================
# extract_yubikey_public_id
# ===========================================================================

class TestExtractYubikeyPublicId:

    def test_extracts_first_12_chars_from_valid_otp(self):
        otp = "c" * 12 + "x" * 32  # 44 chars total
        result = MFAService.extract_yubikey_public_id(otp)
        assert result == "c" * 12

    def test_returns_none_for_wrong_length(self):
        assert MFAService.extract_yubikey_public_id("short") is None
        assert MFAService.extract_yubikey_public_id("x" * 45) is None

    def test_strips_whitespace_before_checking_length(self):
        otp = "  " + "c" * 12 + "x" * 32 + "  "
        result = MFAService.extract_yubikey_public_id(otp)
        assert result == "c" * 12


# ===========================================================================
# validate_yubikey_otp
# ===========================================================================

class TestValidateYubikeyOtp:

    def test_returns_false_for_invalid_otp_length(self):
        result = MFAService.validate_yubikey_otp("short", "cccccc", "", "")
        assert result is False

    def test_returns_false_when_public_id_mismatch(self):
        otp = "a" * 12 + "x" * 32
        result = MFAService.validate_yubikey_otp(otp, "ccccccxxxxxx", "", "")
        assert result is False

    def test_fails_closed_in_dev_mode_by_default(self):
        # (SVC-005): no client id and no explicit dev-mode flag must
        # fail closed rather than accept on the 12-char prefix match alone.
        otp = "ccccccxxxxxx" + "y" * 32
        assert MFAService.validate_yubikey_otp(otp, "ccccccxxxxxx", "", "") is False

    def test_returns_true_in_dev_mode_when_explicitly_allowed(self):
        otp = "ccccccxxxxxx" + "y" * 32
        result = MFAService.validate_yubikey_otp(
            otp, "ccccccxxxxxx", "", "", allow_insecure_dev_mode=True
        )
        assert result is True

    def test_calls_yubico_api_when_client_id_set(self):
        otp = "ccccccxxxxxx" + "y" * 32
        with mock.patch.object(MFAService, "_call_yubico_api",
                               return_value={"status": "OK"}) as mock_api:
            result = MFAService.validate_yubikey_otp(otp, "ccccccxxxxxx", "client-123", "secret")
        mock_api.assert_called_once()
        assert result is True

    def test_returns_false_when_yubico_api_fails(self):
        otp = "ccccccxxxxxx" + "y" * 32
        with mock.patch.object(MFAService, "_call_yubico_api",
                               return_value={"status": "REPLAYED_OTP"}):
            result = MFAService.validate_yubikey_otp(otp, "ccccccxxxxxx", "client-123", "secret")
        assert result is False


# ===========================================================================
# register_yubikey
# ===========================================================================

class TestRegisterYubikey:

    def test_returns_error_for_invalid_otp(self):
        result = MFAService.register_yubikey("bob", "short", "", "")
        assert result["success"] is False
        assert "Invalid OTP" in result["error"]

    def test_registration_fails_closed_in_dev_mode_by_default(self):
        # (SVC-005): no client id + no explicit dev-mode flag = reject
        # before any ES write.
        otp = "ccccccxxxxxx" + "y" * 32
        result = MFAService.register_yubikey("bob", otp, "", "")
        assert result["success"] is False

    def test_registers_without_api_call_when_dev_mode_allowed(self):
        otp = "ccccccxxxxxx" + "y" * 32
        es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.register_yubikey(
                "bob", otp, "", "", allow_insecure_dev_mode=True
            )
        assert result["success"] is True
        es.update.assert_called_once()

    def test_calls_yubico_api_when_client_id_set(self):
        otp = "ccccccxxxxxx" + "y" * 32
        es = mock.MagicMock()
        with mock.patch.object(MFAService, "_call_yubico_api",
                               return_value={"status": "OK"}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.register_yubikey("bob", otp, "client-123", "secret")
        assert result["success"] is True

    def test_returns_error_when_yubico_rejects(self):
        otp = "ccccccxxxxxx" + "y" * 32
        with mock.patch.object(MFAService, "_call_yubico_api",
                               return_value={"status": "BAD_OTP"}):
            result = MFAService.register_yubikey("bob", otp, "client-123", "secret")
        assert result["success"] is False

    def test_returns_error_when_no_es(self):
        # dev-mode allowed so the OTP check passes and we exercise the ES path.
        otp = "ccccccxxxxxx" + "y" * 32
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.register_yubikey("bob", otp, "", "", allow_insecure_dev_mode=True)
        assert result["success"] is False

    def test_returns_error_on_es_exception(self):
        otp = "ccccccxxxxxx" + "y" * 32
        es = mock.MagicMock()
        es.update.side_effect = Exception("write failed")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            result = MFAService.register_yubikey("bob", otp, "", "", allow_insecure_dev_mode=True)
        assert result["success"] is False


# ===========================================================================
# _call_yubico_api
# ===========================================================================

class TestCallYubicoApi:

    def test_parses_ok_response(self):
        # (SVC-004): a status=OK is only trusted after authentication.
        # With no shared secret the response is authenticated by nonce/otp echo,
        # so the mock must echo the nonce/otp the service actually sent.
        def _get(url, params=None, timeout=None):
            response = mock.MagicMock()
            response.text = f"status=OK\nnonce={params['nonce']}\notp={params['otp']}\n"
            return response

        with mock.patch("requests.get", side_effect=_get):
            result = MFAService._call_yubico_api("c" * 44, "client-id", "")
        assert result["status"] == "OK"

    def test_returns_error_on_network_failure(self):
        with mock.patch("requests.get", side_effect=Exception("timeout")):
            result = MFAService._call_yubico_api("otp", "client-id", "")
        assert result["status"] == "ERROR"

    def test_builds_hmac_signature_when_secret_key_provided(self):
        response = mock.MagicMock()
        response.text = "status=OK\n"
        import base64
        secret_key = base64.b64encode(b"test-secret").decode()
        with mock.patch("requests.get", return_value=response) as mock_get:
            MFAService._call_yubico_api("otp", "client-id", secret_key)
        call_kwargs = mock_get.call_args[1]
        assert "h" in call_kwargs.get("params", {})


# ===========================================================================
# update_last_used
# ===========================================================================

class TestUpdateLastUsed:

    def test_updates_es_timestamp(self):
        es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            MFAService.update_last_used("alice")
        es.update.assert_called_once()

    def test_handles_no_es_gracefully(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            MFAService.update_last_used("alice")  # Should not raise

    def test_handles_es_exception_gracefully(self):
        es = mock.MagicMock()
        es.update.side_effect = Exception("write failed")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            MFAService.update_last_used("alice")  # Should not raise
