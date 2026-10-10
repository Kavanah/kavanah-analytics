"""
Tests for the Registration Service.

Covers:
  validate_password          — all strength requirements
  validate_registration      — field validation, uniqueness checks
  rate limiting              — check_rate_limit, record_attempt
  JWT tokens                 — generate/verify, expiry, invalid
  create_pending_registration — success, ES unavailable
  confirm_email_verification — status transitions
  approve_registration       — success, wrong status, ES failure
  reject_registration        — success, wrong status
  cleanup_expired            — delete old pending_verification records
  password encryption        — encrypt/decrypt roundtrip
"""

import time
from unittest import mock

import jwt
import pytest

from asset_management.web.services.registration_service import (
    RegistrationService,
    _registration_attempts,
    STATUS_PENDING_VERIFICATION,
    STATUS_PENDING_APPROVAL,
    STATUS_APPROVED,
    STATUS_REJECTED,
)


# ---------------------------------------------------------------------------
# validate_password
# ---------------------------------------------------------------------------

class TestValidatePassword:

    def test_valid_password(self):
        result = RegistrationService.validate_password("StrongPass1!")
        assert result["valid"] is True
        assert result["errors"] == []

    def test_too_short(self):
        result = RegistrationService.validate_password("Short1!aA")
        assert result["valid"] is False
        assert any("12 characters" in e for e in result["errors"])

    def test_missing_uppercase(self):
        result = RegistrationService.validate_password("lowercase123!!")
        assert result["valid"] is False
        assert any("uppercase" in e for e in result["errors"])

    def test_missing_lowercase(self):
        result = RegistrationService.validate_password("UPPERCASE123!!")
        assert result["valid"] is False
        assert any("lowercase" in e for e in result["errors"])

    def test_missing_digit(self):
        result = RegistrationService.validate_password("NoDigitsHere!!")
        assert result["valid"] is False
        assert any("number" in e for e in result["errors"])

    def test_missing_special(self):
        result = RegistrationService.validate_password("NoSpecial12345")
        assert result["valid"] is False
        assert any("special" in e for e in result["errors"])

    def test_multiple_failures(self):
        result = RegistrationService.validate_password("short")
        assert result["valid"] is False
        assert len(result["errors"]) >= 3


# ---------------------------------------------------------------------------
# validate_registration
# ---------------------------------------------------------------------------

class TestValidateRegistration:

    @mock.patch("asset_management.web.services.registration_service.RegistrationService._check_email_uniqueness", return_value=[])
    @mock.patch("asset_management.web.services.registration_service.RegistrationService._check_username_uniqueness", return_value=[])
    def test_valid_registration(self, mock_user, mock_email):
        result = RegistrationService.validate_registration(
            "testuser", "test@example.com", "StrongPass1!xx", "StrongPass1!xx"
        )
        assert result["valid"] is True

    def test_empty_username(self):
        result = RegistrationService.validate_registration(
            "", "test@example.com", "StrongPass1!xx", "StrongPass1!xx"
        )
        assert result["valid"] is False
        assert any("Username is required" in e for e in result["errors"])

    def test_invalid_username_chars(self):
        result = RegistrationService.validate_registration(
            "user name!", "test@example.com", "StrongPass1!xx", "StrongPass1!xx"
        )
        assert result["valid"] is False
        assert any("3-30 characters" in e for e in result["errors"])

    def test_empty_email(self):
        result = RegistrationService.validate_registration(
            "testuser", "", "StrongPass1!xx", "StrongPass1!xx"
        )
        assert result["valid"] is False
        assert any("Email is required" in e for e in result["errors"])

    def test_invalid_email(self):
        result = RegistrationService.validate_registration(
            "testuser", "notanemail", "StrongPass1!xx", "StrongPass1!xx"
        )
        assert result["valid"] is False
        assert any("Invalid email" in e for e in result["errors"])

    def test_password_mismatch(self):
        result = RegistrationService.validate_registration(
            "testuser", "test@example.com", "StrongPass1!xx", "DifferentPass1!"
        )
        assert result["valid"] is False
        assert any("do not match" in e for e in result["errors"])

    def test_empty_password(self):
        result = RegistrationService.validate_registration(
            "testuser", "test@example.com", "", ""
        )
        assert result["valid"] is False
        assert any("Password is required" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:

    def setup_method(self):
        _registration_attempts.clear()

    def test_allows_under_limit(self):
        assert RegistrationService.check_rate_limit("1.2.3.4") is True

    def test_blocks_after_five_attempts(self):
        for _ in range(5):
            RegistrationService.record_attempt("1.2.3.4")
        assert RegistrationService.check_rate_limit("1.2.3.4") is False

    def test_different_ips_independent(self):
        for _ in range(5):
            RegistrationService.record_attempt("1.2.3.4")
        assert RegistrationService.check_rate_limit("5.6.7.8") is True

    def test_old_attempts_expire(self):
        # Inject timestamps older than 1 hour
        _registration_attempts["1.2.3.4"] = [time.time() - 3700] * 5
        assert RegistrationService.check_rate_limit("1.2.3.4") is True

    def teardown_method(self):
        _registration_attempts.clear()


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

class TestVerificationTokens:

    SECRET = "test-secret-key"

    def test_generate_and_verify(self):
        token = RegistrationService.generate_verification_token("reg-123", self.SECRET)
        result = RegistrationService.verify_token(token, self.SECRET)
        assert result["valid"] is True
        assert result["registration_id"] == "reg-123"

    def test_expired_token(self):
        payload = {
            "sub": "reg-123",
            "purpose": "email_verify",
            "exp": int(time.time()) - 10,
            "iat": int(time.time()) - 100,
        }
        token = jwt.encode(payload, self.SECRET, algorithm="HS256")
        result = RegistrationService.verify_token(token, self.SECRET)
        assert result["valid"] is False
        assert "expired" in result["error"].lower()

    def test_invalid_token(self):
        result = RegistrationService.verify_token("garbage.token.here", self.SECRET)
        assert result["valid"] is False

    def test_wrong_purpose(self):
        payload = {
            "sub": "reg-123",
            "purpose": "password_reset",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        }
        token = jwt.encode(payload, self.SECRET, algorithm="HS256")
        result = RegistrationService.verify_token(token, self.SECRET)
        assert result["valid"] is False
        assert "purpose" in result["error"].lower()

    def test_wrong_secret(self):
        token = RegistrationService.generate_verification_token("reg-123", self.SECRET)
        result = RegistrationService.verify_token(token, "wrong-secret")
        assert result["valid"] is False


# ---------------------------------------------------------------------------
# create_pending_registration
# ---------------------------------------------------------------------------

class TestCreatePendingRegistration:

    @mock.patch("lib.encryption.encrypt", return_value="encrypted-pw")
    def test_success(self, mock_encrypt):
        mock_es = mock.MagicMock()
        mock_es.index.return_value = {"_id": "reg-abc123"}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.create_pending_registration(
                "testuser", "Test User", "test@example.com", "StrongPass1!",
                ip_address="1.2.3.4", user_agent="TestBrowser",
            )
        assert result["success"] is True
        assert result["registration_id"] == "reg-abc123"
        mock_es.index.assert_called_once()
        call_kwargs = mock_es.index.call_args[1]
        assert call_kwargs["body"]["username"] == "testuser"
        assert call_kwargs["body"]["status"] == STATUS_PENDING_VERIFICATION

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = RegistrationService.create_pending_registration(
                "testuser", "Test User", "test@example.com", "StrongPass1!",
            )
        assert result["success"] is False
        assert "unavailable" in result["error"].lower()


# ---------------------------------------------------------------------------
# confirm_email_verification
# ---------------------------------------------------------------------------

class TestConfirmEmailVerification:

    def test_success(self):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {
            "_id": "reg-123",
            "_source": {
                "username": "testuser",
                "status": STATUS_PENDING_VERIFICATION,
            },
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.confirm_email_verification("reg-123")
        assert result["success"] is True
        mock_es.update.assert_called_once()

    def test_already_verified(self):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {
            "_id": "reg-123",
            "_source": {
                "username": "testuser",
                "status": STATUS_PENDING_APPROVAL,
            },
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.confirm_email_verification("reg-123")
        assert result["success"] is True

    def test_not_found(self):
        mock_es = mock.MagicMock()
        mock_es.get.side_effect = Exception("not found")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.confirm_email_verification("reg-999")
        assert result["success"] is False

    def test_wrong_status(self):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {
            "_id": "reg-123",
            "_source": {"username": "testuser", "status": STATUS_REJECTED},
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.confirm_email_verification("reg-123")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# approve_registration
# ---------------------------------------------------------------------------

class TestApproveRegistration:

    @mock.patch("lib.encryption.decrypt", return_value="PlainPass1!")
    def test_success(self, mock_decrypt):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {
            "_id": "reg-123",
            "_source": {
                "username": "testuser",
                "email": "test@example.com",
                "full_name": "Test User",
                "encrypted_password": "encrypted-pw",
                "status": STATUS_PENDING_APPROVAL,
            },
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch.object(RegistrationService, "_send_approval_email", return_value={"success": True}):
            result = RegistrationService.approve_registration(
                "reg-123", "admin", ["viewer"]
            )
        assert result["success"] is True
        mock_es.security.put_user.assert_called_once()
        put_kwargs = mock_es.security.put_user.call_args[1]
        assert put_kwargs["body"]["password"] == "PlainPass1!"
        assert put_kwargs["body"]["roles"] == ["viewer"]

    def test_wrong_status(self):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {
            "_id": "reg-123",
            "_source": {"status": STATUS_PENDING_VERIFICATION, "username": "test"},
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.approve_registration("reg-123", "admin", [])
        assert result["success"] is False
        assert "Cannot approve" in result["error"]

    def test_not_found(self):
        mock_es = mock.MagicMock()
        mock_es.get.side_effect = Exception("not found")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.approve_registration("reg-999", "admin", [])
        assert result["success"] is False


# ---------------------------------------------------------------------------
# reject_registration
# ---------------------------------------------------------------------------

class TestRejectRegistration:

    def test_success(self):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {
            "_id": "reg-123",
            "_source": {"username": "testuser", "status": STATUS_PENDING_APPROVAL},
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.reject_registration(
                "reg-123", "admin", "Spam registration"
            )
        assert result["success"] is True
        update_body = mock_es.update.call_args[1]["body"]["doc"]
        assert update_body["status"] == STATUS_REJECTED
        assert update_body["rejection_reason"] == "Spam registration"

    def test_wrong_status(self):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {
            "_id": "reg-123",
            "_source": {"username": "testuser", "status": STATUS_APPROVED},
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.reject_registration("reg-123", "admin")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# cleanup_expired
# ---------------------------------------------------------------------------

class TestCleanupExpired:

    def test_success(self):
        mock_es = mock.MagicMock()
        mock_es.delete_by_query.return_value = {"deleted": 3}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.cleanup_expired(hours=48)
        assert result == 3

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = RegistrationService.cleanup_expired()
        assert result == -1

    def test_es_error(self):
        mock_es = mock.MagicMock()
        mock_es.delete_by_query.side_effect = Exception("error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.cleanup_expired()
        assert result == -1


# ---------------------------------------------------------------------------
# Password encryption roundtrip
# ---------------------------------------------------------------------------

class TestPasswordEncryption:

    @mock.patch("lib.encryption.decrypt", return_value="MyPassword1!")
    @mock.patch("lib.encryption.encrypt", return_value="encrypted-data")
    def test_encrypt_decrypt_roundtrip(self, mock_enc, mock_dec):
        encrypted = RegistrationService._encrypt_password("MyPassword1!")
        assert encrypted == "encrypted-data"
        decrypted = RegistrationService._decrypt_password(encrypted)
        assert decrypted == "MyPassword1!"


# ---------------------------------------------------------------------------
# get_registrations
# ---------------------------------------------------------------------------

class TestGetRegistrations:

    def test_returns_list(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_id": "reg-1",
                        "_source": {
                            "username": "testuser",
                            "status": STATUS_PENDING_APPROVAL,
                        },
                    }
                ],
            }
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = RegistrationService.get_registrations(status="pending_approval")
        assert len(result) == 1
        assert result[0]["_id"] == "reg-1"

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = RegistrationService.get_registrations()
        assert result == []
