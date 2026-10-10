"""
HTTP Basic Auth fallback must enforce MFA (WEB-012).

Before the fix, the Basic Auth branch at the end of ``verify_auth`` promoted
a bare password check straight to a fully authenticated session —
``session['authenticated']=True`` with roles and ``is_admin`` — without ever
consulting ``MFAService.is_mfa_required``. Every other credential path
(form login, nginx SSO) routes through an ``mfa_pending``
session first, so a leaked asset_admin/superuser password plus one
``Authorization: Basic`` header was a complete MFA bypass.

After the fix the branch mirrors the nginx SSO branch: when MFA is required
it establishes an ``mfa_pending`` session (never ``authenticated``) and
redirects to /mfa/setup (not enrolled) or /mfa/challenge (enrolled).

Mutation pins:
  M1. Remove the ``is_mfa_required`` call from the Basic Auth branch ->
      ``test_mfa_required_gets_pending_session_not_full_session`` and both
      redirect tests fail.
  M2. Set ``session['authenticated']`` before the MFA gate ->
      ``test_mfa_required_gets_pending_session_not_full_session`` fails.
"""

import base64
from unittest import mock

import pytest

# DoD gate — release-plan coverage for the cycle.
pytestmark = []

_MFA_SVC = "asset_management.web.services.mfa_service.MFAService.is_mfa_required"


def _basic_header(user="testuser", password="testpass"):
    raw = f"{user}:{password}"
    encoded = base64.b64encode(raw.encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _auth_result(roles=None):
    return {
        "success": True,
        "username": "testuser",
        "roles": roles or ["asset_admin"],
        "full_name": "Test User",
        "email": "test@example.com",
    }


class TestBasicAuthMfaGate:

    def test_mfa_required_enrolled_redirects_to_challenge(self, client):
        with mock.patch("lib.es_auth.authenticate_user", return_value=_auth_result()), \
             mock.patch(_MFA_SVC, return_value={"required": True, "method": "totp", "setup_complete": True}):
            resp = client.get("/", headers=_basic_header())
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/mfa/challenge")

    def test_mfa_required_unenrolled_redirects_to_setup(self, client):
        with mock.patch("lib.es_auth.authenticate_user", return_value=_auth_result()), \
             mock.patch(_MFA_SVC, return_value={"required": True, "method": None, "setup_complete": False}):
            resp = client.get("/", headers=_basic_header())
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/mfa/setup")

    def test_mfa_required_gets_pending_session_not_full_session(self, client):
        # M1/M2 pin: the session left behind must be mfa_pending, never
        # authenticated — a pending session grants no route access.
        with mock.patch("lib.es_auth.authenticate_user", return_value=_auth_result(["superuser"])), \
             mock.patch(_MFA_SVC, return_value={"required": True, "method": "totp", "setup_complete": True}):
            client.get("/", headers=_basic_header())
        with client.session_transaction() as sess:
            assert sess.get("mfa_pending") is True
            assert sess.get("mfa_pending_username") == "testuser"
            assert sess.get("mfa_pending_roles") == ["superuser"]
            assert sess.get("mfa_pending_login_type") == "basic_auth"
            assert "authenticated" not in sess
            assert "is_admin" not in sess

    def test_mfa_not_required_keeps_existing_behavior(self, client):
        # Bypass/exempt users (and ENFORCE_MFA-off deployments) still get the
        # earlier full session from Basic Auth.
        with mock.patch("lib.es_auth.authenticate_user", return_value=_auth_result()), \
             mock.patch(_MFA_SVC, return_value={"required": False, "method": None, "setup_complete": True}):
            resp = client.get("/", headers=_basic_header())
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            assert "/mfa" not in resp.headers.get("Location", "")
            assert "/login" not in resp.headers.get("Location", "").lower()
        with client.session_transaction() as sess:
            assert sess.get("authenticated") is True

    def test_mfa_gate_runs_after_role_check(self, client):
        # A user without asset_admin/superuser still 403s — the MFA gate must
        # not turn an unauthorized account into an MFA-enrollment session.
        with mock.patch("lib.es_auth.authenticate_user", return_value=_auth_result(["some_other_role"])), \
             mock.patch(_MFA_SVC, return_value={"required": True, "method": None, "setup_complete": False}):
            resp = client.get("/", headers=_basic_header())
        assert resp.status_code == 403
