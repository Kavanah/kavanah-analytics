"""
Tests for registration routes.

Covers:
  GET  /register                          — page render, disabled state
  POST /api/auth/register                 — validation, rate limiting, success
  GET  /verify-email/<token>              — valid, expired, invalid tokens
  GET  /registration-success              — page render
  GET  /api/admin/registrations           — auth required, listing
  POST /api/admin/registrations/<id>/approve — auth required, success
  POST /api/admin/registrations/<id>/reject  — auth required, success
"""

import os
import time
from unittest import mock

import pytest


# Enable registration for tests
os.environ["REGISTRATION_ENABLED"] = "true"


from asset_management.web.services.registration_service import (
    RegistrationService,
    _registration_attempts,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_registration_rate_limits():
    """Clear registration rate limits before each test."""
    _registration_attempts.clear()
    yield
    _registration_attempts.clear()


# ---------------------------------------------------------------------------
# GET /register
# ---------------------------------------------------------------------------

class TestRegisterPage:

    def test_returns_200(self, client):
        response = client.get("/signup")
        assert response.status_code == 200
        assert b"Create Account" in response.data

    def test_contains_form_fields(self, client):
        response = client.get("/signup")
        html = response.data.decode()
        assert 'name="username"' in html
        assert 'name="email"' in html
        assert 'name="password"' in html
        assert 'name="confirm_password"' in html
        assert 'name="csrf_token"' in html

    def test_disabled_returns_404(self, client):
        with mock.patch(
            "asset_management.web.config.Config.REGISTRATION_ENABLED",
            new_callable=mock.PropertyMock,
            return_value=False,
        ):
            response = client.get("/signup")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /registration-success
# ---------------------------------------------------------------------------

class TestRegistrationSuccessPage:

    def test_returns_200(self, client):
        response = client.get("/signup/success?email=test@example.com")
        assert response.status_code == 200
        assert b"test@example.com" in response.data


# ---------------------------------------------------------------------------
# POST /api/auth/register
# ---------------------------------------------------------------------------

class TestApiRegister:

    def _get_csrf_token(self, client):
        """Get a CSRF token by loading the register page."""
        response = client.get("/signup")
        with client.session_transaction() as sess:
            return sess.get("csrf_token", "test-csrf-token")

    def test_missing_fields_shows_errors(self, client):
        csrf = self._get_csrf_token(client)
        response = client.post("/signup", data={
            "csrf_token": csrf,
            "username": "",
            "email": "",
            "password": "",
            "confirm_password": "",
        })
        assert response.status_code == 200
        assert b"required" in response.data.lower()

    def test_weak_password_shows_errors(self, client):
        csrf = self._get_csrf_token(client)
        response = client.post("/signup", data={
            "csrf_token": csrf,
            "username": "newuser",
            "email": "new@example.com",
            "password": "weak",
            "confirm_password": "weak",
        })
        assert response.status_code == 200
        assert b"12 characters" in response.data

    def test_password_mismatch_shows_error(self, client):
        csrf = self._get_csrf_token(client)
        with mock.patch.object(RegistrationService, "_check_username_uniqueness", return_value=[]), \
             mock.patch.object(RegistrationService, "_check_email_uniqueness", return_value=[]):
            response = client.post("/signup", data={
                "csrf_token": csrf,
                "username": "newuser",
                "email": "new@example.com",
                "password": "StrongPass1!xx",
                "confirm_password": "DifferentPass1!",
            })
        assert response.status_code == 200
        assert b"do not match" in response.data

    @mock.patch.object(RegistrationService, "send_verification_email", return_value={"success": True})
    @mock.patch.object(RegistrationService, "create_pending_registration", return_value={"success": True, "registration_id": "reg-123"})
    @mock.patch.object(RegistrationService, "validate_registration", return_value={"valid": True, "errors": []})
    @mock.patch("asset_management.web.services.audit_log_service.AuditLogService.log_event")
    def test_success_redirects_to_success_page(self, mock_audit, mock_validate, mock_create, mock_email, client):
        csrf = self._get_csrf_token(client)
        response = client.post("/signup", data={
            "csrf_token": csrf,
            "username": "newuser",
            "full_name": "New User",
            "email": "new@example.com",
            "password": "StrongPass1!xx",
            "confirm_password": "StrongPass1!xx",
        }, follow_redirects=False)
        assert response.status_code == 302
        assert "/signup/success" in response.headers["Location"]

    def test_rate_limit_returns_429(self, client):
        csrf = self._get_csrf_token(client)
        # Exhaust rate limit
        for _ in range(5):
            RegistrationService.record_attempt("127.0.0.1")
        response = client.post("/signup", data={
            "csrf_token": csrf,
            "username": "newuser",
            "email": "new@example.com",
            "password": "StrongPass1!xx",
            "confirm_password": "StrongPass1!xx",
        })
        assert response.status_code == 429

    def test_disabled_returns_404(self, client):
        with mock.patch(
            "asset_management.web.config.Config.REGISTRATION_ENABLED",
            new_callable=mock.PropertyMock,
            return_value=False,
        ):
            response = client.post("/signup", data={"csrf_token": "x"})
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /verify-email/<token>
# ---------------------------------------------------------------------------

class TestVerifyEmail:

    def test_valid_token(self, client, app):
        with mock.patch.object(
            RegistrationService, "confirm_email_verification",
            return_value={"success": True},
        ), mock.patch.object(
            RegistrationService, "get_registration_by_id",
            return_value={"username": "testuser"},
        ), mock.patch(
            "asset_management.web.services.audit_log_service.AuditLogService.log_event"
        ):
            token = RegistrationService.generate_verification_token(
                "reg-123", app.secret_key
            )
            response = client.get(f"/verify-email/{token}")
        assert response.status_code == 200
        assert b"verified successfully" in response.data

    def test_expired_token(self, client, app):
        import jwt as pyjwt
        payload = {
            "sub": "reg-123",
            "purpose": "email_verify",
            "exp": int(time.time()) - 10,
            "iat": int(time.time()) - 100,
        }
        token = pyjwt.encode(payload, app.secret_key, algorithm="HS256")
        response = client.get(f"/verify-email/{token}")
        assert response.status_code == 200
        assert b"expired" in response.data

    def test_invalid_token(self, client):
        response = client.get("/verify-email/invalid-token")
        assert response.status_code == 200
        assert b"failed" in response.data.lower()


# ---------------------------------------------------------------------------
# GET /api/admin/registrations
# ---------------------------------------------------------------------------

class TestListRegistrations:

    def test_requires_auth(self, client):
        response = client.get("/api/admin/registrations")
        # an unauthenticated /api/ call is an AJAX/API caller → 401 + JSON
        # (was 302 -> /login). 403 still tolerated for a role-gated authed caller.
        assert response.status_code in (401, 403)

    @mock.patch.object(RegistrationService, "get_registrations", return_value=[])
    def test_admin_can_list(self, mock_get, auth_client):
        response = auth_client.get("/api/admin/registrations")
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True

    @mock.patch.object(RegistrationService, "get_registrations", return_value=[])
    def test_filters_by_status(self, mock_get, auth_client):
        auth_client.get("/api/admin/registrations?status=pending_approval")
        mock_get.assert_called_once_with(status="pending_approval")


# ---------------------------------------------------------------------------
# POST /api/admin/registrations/<id>/approve
# ---------------------------------------------------------------------------

class TestApproveRegistration:

    def test_requires_auth(self, client):
        response = client.post("/api/admin/registrations/reg-123/approve")
        # unauthenticated /api/ POST → 401 + JSON (was 302).
        assert response.status_code in (401, 403)

    @mock.patch("asset_management.web.services.audit_log_service.AuditLogService.log_event")
    @mock.patch.object(RegistrationService, "approve_registration", return_value={"success": True})
    def test_admin_can_approve(self, mock_approve, mock_audit, auth_client):
        response = auth_client.post(
            "/api/admin/registrations/reg-123/approve",
            json={"roles": ["viewer"]},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        mock_approve.assert_called_once_with("reg-123", "testuser", ["viewer"])

    @mock.patch.object(RegistrationService, "approve_registration", return_value={"success": False, "error": "Not found"})
    def test_approve_failure(self, mock_approve, auth_client):
        response = auth_client.post(
            "/api/admin/registrations/reg-999/approve",
            json={"roles": []},
        )
        data = response.get_json()
        assert data["success"] is False

    def test_org_user_cannot_approve(self, org_user_client):
        response = org_user_client.post(
            "/api/admin/registrations/reg-123/approve",
            json={"roles": []},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/admin/registrations/<id>/reject
# ---------------------------------------------------------------------------

class TestRejectRegistration:

    def test_requires_auth(self, client):
        response = client.post("/api/admin/registrations/reg-123/reject")
        # unauthenticated /api/ POST → 401 + JSON (was 302).
        assert response.status_code in (401, 403)

    @mock.patch("asset_management.web.services.audit_log_service.AuditLogService.log_event")
    @mock.patch.object(RegistrationService, "reject_registration", return_value={"success": True})
    def test_admin_can_reject(self, mock_reject, mock_audit, auth_client):
        response = auth_client.post(
            "/api/admin/registrations/reg-123/reject",
            json={"reason": "Spam"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        mock_reject.assert_called_once_with("reg-123", "testuser", "Spam")
