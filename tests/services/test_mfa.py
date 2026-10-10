"""
Tests for the MFA Service.

Covers:
  ensure_index_exists          — creates index, already exists, ES unavailable
  get_mfa_record               — found, not found, ES unavailable
  is_mfa_required              — no record, totp enabled, yubikey enabled, unknown method
  list_all_users_with_mfa      — merges ES users with MFA records
  admin_enable_mfa             — success, ES unavailable, ES error
  admin_disable_mfa            — success, ES unavailable, ES error
  generate_totp_secret         — generates QR code and stores encrypted secret
  verify_and_confirm_totp      — success, invalid code, no record
  validate_totp_code           — success, invalid code, no record
  extract_yubikey_public_id    — valid OTP, invalid length
  register_yubikey             — success, invalid OTP, API failure, ES error
  validate_yubikey_otp         — match, mismatch, dev mode, API failure
  _call_yubico_api             — success, with/without HMAC, API error
  update_last_used             — success, ES unavailable
"""

import base64
from unittest import mock

import pytest

from asset_management.web.services.mfa_service import MFAService, INDEX


# ---------------------------------------------------------------------------
# ensure_index_exists
# ---------------------------------------------------------------------------

class TestEnsureIndexExists:

    def test_creates_index_when_not_exists(self):
        mock_es = mock.MagicMock()
        mock_es.indices.exists.return_value = False
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            MFAService.ensure_index_exists()
        mock_es.indices.create.assert_called_once()

    def test_skips_when_index_exists(self):
        mock_es = mock.MagicMock()
        mock_es.indices.exists.return_value = True
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            MFAService.ensure_index_exists()
        mock_es.indices.create.assert_not_called()

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            MFAService.ensure_index_exists()  # should not raise

    def test_already_exists_exception_ignored(self):
        mock_es = mock.MagicMock()
        mock_es.indices.exists.return_value = False
        mock_es.indices.create.side_effect = Exception("resource_already_exists_exception")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            MFAService.ensure_index_exists()  # should not raise

    def test_other_exception_logged(self):
        mock_es = mock.MagicMock()
        mock_es.indices.exists.return_value = False
        mock_es.indices.create.side_effect = Exception("some other error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            MFAService.ensure_index_exists()  # should not raise


# ---------------------------------------------------------------------------
# get_mfa_record
# ---------------------------------------------------------------------------

class TestGetMfaRecord:

    def test_found(self):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {"_source": {"mfa_enabled": True, "mfa_method": "totp"}}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.get_mfa_record("testuser")
        assert result["mfa_enabled"] is True

    def test_not_found(self):
        mock_es = mock.MagicMock()
        mock_es.get.side_effect = Exception("not found")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.get_mfa_record("unknown")
        assert result is None

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.get_mfa_record("testuser")
        assert result is None


# ---------------------------------------------------------------------------
# is_mfa_required
# ---------------------------------------------------------------------------

_ENFORCE_MFA_PATH = "asset_management.web.config.Config.ENFORCE_MFA"


class TestIsMfaRequiredEnforceOff:
    """Behavior when enforce_mfa is off — opt-in mode (earlier)."""

    def test_no_record(self):
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=None):
            result = MFAService.is_mfa_required("testuser")
        assert result == {"required": False, "method": None, "setup_complete": False}

    def test_mfa_disabled(self):
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value={"mfa_enabled": False}):
            result = MFAService.is_mfa_required("testuser")
        assert result["required"] is False

    def test_totp_confirmed(self):
        record = {"mfa_enabled": True, "mfa_method": "totp", "totp_confirmed": True}
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=record):
            result = MFAService.is_mfa_required("testuser")
        assert result == {"required": True, "method": "totp", "setup_complete": True}

    def test_totp_not_confirmed(self):
        record = {"mfa_enabled": True, "mfa_method": "totp", "totp_confirmed": False}
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=record):
            result = MFAService.is_mfa_required("testuser")
        assert result["setup_complete"] is False

    def test_yubikey_confirmed(self):
        record = {"mfa_enabled": True, "mfa_method": "yubikey", "yubikey_confirmed": True}
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=record):
            result = MFAService.is_mfa_required("testuser")
        assert result == {"required": True, "method": "yubikey", "setup_complete": True}

    def test_unknown_method(self):
        record = {"mfa_enabled": True, "mfa_method": "sms"}
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=record):
            result = MFAService.is_mfa_required("testuser")
        assert result["setup_complete"] is False


class TestIsMfaRequiredEnforceOn:
    """enforce_mfa flips the default to 'required unless confirmed'."""

    def test_no_record_forces_enrollment(self):
        with mock.patch(_ENFORCE_MFA_PATH, True), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=None):
            result = MFAService.is_mfa_required("newuser")
        assert result == {"required": True, "method": None, "setup_complete": False}

    def test_mfa_disabled_forces_enrollment(self):
        with mock.patch(_ENFORCE_MFA_PATH, True), \
             mock.patch.object(MFAService, "get_mfa_record",
                               return_value={"mfa_enabled": False}):
            result = MFAService.is_mfa_required("newuser")
        assert result == {"required": True, "method": None, "setup_complete": False}

    def test_confirmed_totp_still_required_and_setup_complete(self):
        record = {"mfa_enabled": True, "mfa_method": "totp", "totp_confirmed": True}
        with mock.patch(_ENFORCE_MFA_PATH, True), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=record):
            result = MFAService.is_mfa_required("testuser")
        assert result == {"required": True, "method": "totp", "setup_complete": True}

    def test_elastic_user_always_blocked(self):
        """elastic is a service identity, not a web UI login."""
        with mock.patch(_ENFORCE_MFA_PATH, True):
            result = MFAService.is_mfa_required("elastic")
        assert result == {"required": True, "method": None, "setup_complete": False}

    def test_elastic_user_not_blocked_when_enforce_off(self):
        with mock.patch(_ENFORCE_MFA_PATH, False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=None):
            result = MFAService.is_mfa_required("elastic")
        assert result["required"] is False

    def test_bypass_flag_skips_enforcement(self):
        """Break-glass bypass returns required=false even when enforce is on."""
        record = {"mfa_enabled": False, "bypass_mfa": True}
        with mock.patch(_ENFORCE_MFA_PATH, True), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=record):
            result = MFAService.is_mfa_required("locked_out_user")
        assert result["required"] is False

    def test_bypass_flag_skips_enforcement_for_elastic_too(self):
        """Safety valve: if a superuser explicitly bypassed elastic, respect it.
        (Elastic block is rule 1 in is_mfa_required, but bypass rule is only rule 2.
        This test documents the current order of precedence.)"""
        record = {"bypass_mfa": True}
        with mock.patch(_ENFORCE_MFA_PATH, True), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=record):
            result = MFAService.is_mfa_required("elastic")
        # Elastic rule runs first, so elastic is still blocked even with bypass record
        assert result["required"] is True


# ---------------------------------------------------------------------------
# list_all_users_with_mfa
# ---------------------------------------------------------------------------

class TestListAllUsersWithMfa:

    def test_merges_users_and_mfa(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.return_value = {
            "admin": {"full_name": "Admin", "email": "a@e.com", "roles": ["superuser"], "metadata": {}},
            "john": {"full_name": "John", "email": "j@e.com", "roles": ["user"], "metadata": {}},
        }
        mock_es.search.return_value = {
            "hits": {"hits": [
                {"_id": "admin", "_source": {"username": "admin", "mfa_enabled": True, "mfa_method": "totp", "totp_confirmed": True}},
            ]},
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.list_all_users_with_mfa()
        assert len(result) == 2
        admin = next(u for u in result if u["username"] == "admin")
        assert admin["mfa_enabled"] is True
        assert admin["setup_complete"] is True
        john = next(u for u in result if u["username"] == "john")
        assert john["mfa_enabled"] is False

    def test_skips_reserved_users(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.return_value = {
            "admin": {"full_name": "Admin", "email": "", "roles": [], "metadata": {}},
            ".internal": {"full_name": "", "email": "", "roles": [], "metadata": {}},
            "reserved_user": {"full_name": "", "email": "", "roles": [], "metadata": {"_reserved": True}},
        }
        mock_es.search.return_value = {"hits": {"hits": []}}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.list_all_users_with_mfa()
        usernames = [u["username"] for u in result]
        assert ".internal" not in usernames
        assert "reserved_user" not in usernames
        assert "admin" in usernames

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.list_all_users_with_mfa()
        assert result == []

    def test_sorted_by_username(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.return_value = {
            "zoe": {"full_name": "Zoe", "email": "", "roles": [], "metadata": {}},
            "adam": {"full_name": "Adam", "email": "", "roles": [], "metadata": {}},
        }
        mock_es.search.return_value = {"hits": {"hits": []}}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.list_all_users_with_mfa()
        assert result[0]["username"] == "adam"
        assert result[1]["username"] == "zoe"


# ---------------------------------------------------------------------------
# admin_enable_mfa / admin_disable_mfa
# ---------------------------------------------------------------------------

class TestAdminEnableDisableMfa:

    def test_enable_success(self):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.admin_enable_mfa("testuser", "admin")
        assert result["success"] is True
        mock_es.index.assert_called_once()
        doc = mock_es.index.call_args[1]["document"]
        assert doc["mfa_enabled"] is True
        assert doc["mfa_method"] is None
        assert doc["enabled_by"] == "admin"

    def test_enable_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.admin_enable_mfa("testuser", "admin")
        assert result["success"] is False

    def test_enable_es_error(self):
        mock_es = mock.MagicMock()
        mock_es.index.side_effect = Exception("write error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.admin_enable_mfa("testuser", "admin")
        assert result["success"] is False

    def test_disable_success(self):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.admin_disable_mfa("testuser")
        assert result["success"] is True
        mock_es.delete.assert_called_once()

    def test_disable_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.admin_disable_mfa("testuser")
        assert result["success"] is False

    def test_disable_es_error(self):
        mock_es = mock.MagicMock()
        mock_es.delete.side_effect = Exception("delete error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.admin_disable_mfa("testuser")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# generate_totp_secret
# ---------------------------------------------------------------------------

class TestGenerateTotpSecret:

    def test_generates_qr_and_stores_secret(self):
        mock_es = mock.MagicMock()
        mock_pyotp = mock.MagicMock()
        mock_pyotp.random_base32.return_value = "JBSWY3DPEHPK3PXP"
        mock_totp_inst = mock.MagicMock()
        mock_totp_inst.provisioning_uri.return_value = "otpauth://totp/DEAM:testuser?secret=JBSWY3DPEHPK3PXP"
        mock_pyotp.totp.TOTP.return_value = mock_totp_inst
        mock_qrcode = mock.MagicMock()
        mock_qr_img = mock.MagicMock()
        mock_qr_img.save = mock.MagicMock()
        mock_qrcode.make.return_value = mock_qr_img

        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("lib.encryption.encrypt", return_value="encrypted_secret"), \
             mock.patch.dict("sys.modules", {"pyotp": mock_pyotp, "qrcode": mock_qrcode}):
            result = MFAService.generate_totp_secret("testuser")

        assert result["totp_secret"] == "JBSWY3DPEHPK3PXP"
        assert "provisioning_uri" in result
        assert "qr_code_b64" in result
        mock_es.update.assert_called_once()


# ---------------------------------------------------------------------------
# verify_and_confirm_totp
# ---------------------------------------------------------------------------

class TestVerifyAndConfirmTotp:

    def test_success(self):
        record = {"totp_secret_enc": "encrypted", "mfa_enabled": True}
        mock_es = mock.MagicMock()
        mock_totp = mock.MagicMock()
        mock_totp.verify.return_value = True
        mock_pyotp = mock.MagicMock()
        mock_pyotp.TOTP.return_value = mock_totp
        with mock.patch.object(MFAService, "get_mfa_record", return_value=record), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("lib.encryption.decrypt", return_value="SECRET"), \
             mock.patch.dict("sys.modules", {"pyotp": mock_pyotp}):
            result = MFAService.verify_and_confirm_totp("testuser", "123456")
        assert result is True
        mock_es.update.assert_called_once()

    def test_invalid_code(self):
        record = {"totp_secret_enc": "encrypted"}
        mock_totp = mock.MagicMock()
        mock_totp.verify.return_value = False
        mock_pyotp = mock.MagicMock()
        mock_pyotp.TOTP.return_value = mock_totp
        with mock.patch.object(MFAService, "get_mfa_record", return_value=record), \
             mock.patch("lib.encryption.decrypt", return_value="SECRET"), \
             mock.patch.dict("sys.modules", {"pyotp": mock_pyotp}):
            result = MFAService.verify_and_confirm_totp("testuser", "000000")
        assert result is False

    def test_no_record(self):
        with mock.patch.object(MFAService, "get_mfa_record", return_value=None), \
             mock.patch.dict("sys.modules", {"pyotp": mock.MagicMock()}):
            result = MFAService.verify_and_confirm_totp("testuser", "123456")
        assert result is False

    def test_decrypt_error(self):
        record = {"totp_secret_enc": "encrypted"}
        with mock.patch.object(MFAService, "get_mfa_record", return_value=record), \
             mock.patch("lib.encryption.decrypt", side_effect=Exception("decrypt failed")), \
             mock.patch.dict("sys.modules", {"pyotp": mock.MagicMock()}):
            result = MFAService.verify_and_confirm_totp("testuser", "123456")
        assert result is False


# ---------------------------------------------------------------------------
# validate_totp_code
# ---------------------------------------------------------------------------

class TestValidateTotpCode:

    def test_valid_code(self):
        record = {"totp_secret_enc": "encrypted"}
        mock_totp = mock.MagicMock()
        mock_totp.verify.return_value = True
        mock_pyotp = mock.MagicMock()
        mock_pyotp.TOTP.return_value = mock_totp
        with mock.patch.object(MFAService, "get_mfa_record", return_value=record), \
             mock.patch("lib.encryption.decrypt", return_value="SECRET"), \
             mock.patch.dict("sys.modules", {"pyotp": mock_pyotp}):
            result = MFAService.validate_totp_code("testuser", "123456")
        assert result is True

    def test_invalid_code(self):
        record = {"totp_secret_enc": "encrypted"}
        mock_totp = mock.MagicMock()
        mock_totp.verify.return_value = False
        mock_pyotp = mock.MagicMock()
        mock_pyotp.TOTP.return_value = mock_totp
        with mock.patch.object(MFAService, "get_mfa_record", return_value=record), \
             mock.patch("lib.encryption.decrypt", return_value="SECRET"), \
             mock.patch.dict("sys.modules", {"pyotp": mock_pyotp}):
            result = MFAService.validate_totp_code("testuser", "000000")
        assert result is False

    def test_no_record(self):
        with mock.patch.object(MFAService, "get_mfa_record", return_value=None), \
             mock.patch.dict("sys.modules", {"pyotp": mock.MagicMock()}):
            result = MFAService.validate_totp_code("testuser", "123456")
        assert result is False


# ---------------------------------------------------------------------------
# extract_yubikey_public_id
# ---------------------------------------------------------------------------

class TestExtractYubikeyPublicId:

    def test_valid_otp(self):
        otp = "c" * 44  # 44-char modhex
        assert MFAService.extract_yubikey_public_id(otp) == "cccccccccccc"

    def test_invalid_length_short(self):
        assert MFAService.extract_yubikey_public_id("short") is None

    def test_invalid_length_long(self):
        assert MFAService.extract_yubikey_public_id("c" * 45) is None

    def test_strips_whitespace(self):
        otp = " " + "c" * 44 + " "
        assert MFAService.extract_yubikey_public_id(otp) == "cccccccccccc"


# ---------------------------------------------------------------------------
# register_yubikey
# ---------------------------------------------------------------------------

class TestRegisterYubikey:

    def test_success_with_api_validation(self):
        otp = "c" * 44
        mock_es = mock.MagicMock()
        with mock.patch.object(MFAService, "_call_yubico_api", return_value={"status": "OK"}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.register_yubikey("testuser", otp, "client_id", "secret_key")
        assert result["success"] is True
        mock_es.update.assert_called_once()

    def test_invalid_otp_length(self):
        result = MFAService.register_yubikey("testuser", "short", "client_id", "secret_key")
        assert result["success"] is False
        assert "Invalid OTP" in result["error"]

    def test_api_validation_failure(self):
        otp = "c" * 44
        with mock.patch.object(MFAService, "_call_yubico_api", return_value={"status": "REPLAYED_OTP"}):
            result = MFAService.register_yubikey("testuser", otp, "client_id", "secret_key")
        assert result["success"] is False
        assert "REPLAYED_OTP" in result["error"]

    def test_fails_closed_in_dev_mode_by_default(self):
        # (SVC-005): no client id + no explicit dev-mode flag = reject.
        otp = "c" * 44
        result = MFAService.register_yubikey("testuser", otp, "", "")
        assert result["success"] is False

    def test_dev_mode_skips_api_when_allowed(self):
        otp = "c" * 44
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.register_yubikey("testuser", otp, "", "", allow_insecure_dev_mode=True)
        assert result["success"] is True

    def test_es_unavailable(self):
        otp = "c" * 44
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = MFAService.register_yubikey("testuser", otp, "", "", allow_insecure_dev_mode=True)
        assert result["success"] is False

    def test_es_update_error(self):
        otp = "c" * 44
        mock_es = mock.MagicMock()
        mock_es.update.side_effect = Exception("ES error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = MFAService.register_yubikey("testuser", otp, "", "", allow_insecure_dev_mode=True)
        assert result["success"] is False


# ---------------------------------------------------------------------------
# validate_yubikey_otp
# ---------------------------------------------------------------------------

class TestValidateYubikeyOtp:

    def test_match_with_api_ok(self):
        otp = "c" * 44
        with mock.patch.object(MFAService, "_call_yubico_api", return_value={"status": "OK"}):
            result = MFAService.validate_yubikey_otp(otp, "cccccccccccc", "client_id", "secret")
        assert result is True

    def test_public_id_mismatch(self):
        otp = "c" * 44
        result = MFAService.validate_yubikey_otp(otp, "dddddddddddd", "client_id", "secret")
        assert result is False

    def test_invalid_otp_length(self):
        result = MFAService.validate_yubikey_otp("short", "cccccccccccc", "client_id", "secret")
        assert result is False

    def test_fails_closed_in_dev_mode_by_default(self):
        # (SVC-005): no client id + no explicit dev-mode flag = reject.
        otp = "c" * 44
        assert MFAService.validate_yubikey_otp(otp, "cccccccccccc", "", "") is False

    def test_dev_mode_accepts_matching_id_when_allowed(self):
        otp = "c" * 44
        result = MFAService.validate_yubikey_otp(
            otp, "cccccccccccc", "", "", allow_insecure_dev_mode=True
        )
        assert result is True

    def test_api_failure(self):
        otp = "c" * 44
        with mock.patch.object(MFAService, "_call_yubico_api", return_value={"status": "BAD_OTP"}):
            result = MFAService.validate_yubikey_otp(otp, "cccccccccccc", "client_id", "secret")
        assert result is False


# ---------------------------------------------------------------------------
# _call_yubico_api
# ---------------------------------------------------------------------------

class TestCallYubicoApi:

    def test_success(self):
        # (SVC-004): status=OK is trusted only after authentication.
        # With no shared secret, authentication is the nonce/otp echo, so the
        # mock must echo what the service sent.
        def _get(url, params=None, timeout=None):
            resp = mock.MagicMock()
            resp.text = f"status=OK\nnonce={params['nonce']}\notp={params['otp']}\n"
            return resp

        with mock.patch("requests.get", side_effect=_get):
            result = MFAService._call_yubico_api("c" * 44, "client_id", "")
        assert result["status"] == "OK"

    def test_with_hmac_signature(self):
        # (SVC-004): with a shared secret, the response HMAC must verify.
        import hashlib
        import hmac as hmac_mod

        secret_key = base64.b64encode(b"testsecret").decode()

        def _get(url, params=None, timeout=None):
            body = {"status": "OK", "nonce": params["nonce"], "otp": params["otp"]}
            param_string = "&".join(f"{k}={v}" for k, v in sorted(body.items()))
            h = base64.b64encode(
                hmac_mod.new(base64.b64decode(secret_key), param_string.encode(), hashlib.sha1).digest()
            ).decode()
            resp = mock.MagicMock()
            resp.text = "\r\n".join(f"{k}={v}" for k, v in {**body, "h": h}.items())
            return resp

        with mock.patch("requests.get", side_effect=_get):
            result = MFAService._call_yubico_api("c" * 44, "client_id", secret_key)
        assert result["status"] == "OK"

    def test_api_network_error(self):
        with mock.patch("requests.get", side_effect=Exception("timeout")):
            result = MFAService._call_yubico_api("otp123", "client_id", "")
        assert result["status"] == "ERROR"


# ---------------------------------------------------------------------------
# update_last_used
# ---------------------------------------------------------------------------

class TestUpdateLastUsed:

    def test_success(self):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            MFAService.update_last_used("testuser")
        mock_es.update.assert_called_once()

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            MFAService.update_last_used("testuser")  # should not raise

    def test_es_exception(self):
        mock_es = mock.MagicMock()
        mock_es.update.side_effect = Exception("update error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            MFAService.update_last_used("testuser")  # should not raise
