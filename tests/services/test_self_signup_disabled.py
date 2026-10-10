"""Tests for the SELF_SIGNUP_ENABLED gate (2026-08-06 open-registration
incident).

Production was discovered live with open registration: any Google or
Microsoft account (or the email+password wizard) could self-provision a
KA account, and a completed wizard grants ``ai_assistant`` (Anthropic
API spend). The fix threads ``allow_provision`` through both SSO
services' ``sign_in_or_provision`` and gates the wizard routes; the
route layer computes ``allow_provision`` as
``config.SELF_SIGNUP_ENABLED or session-has-pending-invite``.

Covered here (service layer — the trust boundary):
  1. Google: unmatched email + ``allow_provision=False`` → refused with
     ``code="signup_disabled"`` and ``create_user`` never called.
  2. Google: EXISTING user still signs in with ``allow_provision=False``
     (the incident fix must not lock out real users).
  3. Microsoft: same refusal contract.
  4. Default is permissive at the service layer (``allow_provision=True``)
     so the flag's default-off lives in config/route, not hidden here.
  5. ``config.SELF_SIGNUP_ENABLED`` parses env/master-config truthiness
     and defaults False.
"""

from __future__ import annotations

from unittest import mock

import pytest

from asset_management.web.services.google_sso_service import GoogleSsoService
from asset_management.web.services.microsoft_sso_service import MicrosoftSsoService


_GOOGLE_USERINFO = {
    "email": "stranger@example.com",
    "email_verified": True,
    "sub": "google-sub-123",
    "name": "Total Stranger",
}


def _microsoft_userinfo():
    return {
        "email": "stranger@example.com",
        "sub": "ms-sub-123",
        "name": "Total Stranger",
        # past the sub-match, the email claim is only used
        # when the exchange marked it trusted.
        "email_trusted": True,
    }


def test_google_unmatched_email_refused_when_provision_disallowed():
    with (
        mock.patch.object(GoogleSsoService, "find_user_by_email", return_value=None),
        mock.patch(
            "asset_management.web.services.user_provisioning_service.UserProvisioningService.create_user"
        ) as create_user,
    ):
        result = GoogleSsoService.sign_in_or_provision(_GOOGLE_USERINFO, allow_provision=False)

    assert result["success"] is False
    assert result.get("code") == "signup_disabled"
    create_user.assert_not_called()


def test_google_existing_user_still_signs_in_when_provision_disallowed():
    existing = {
        "username": "stranger_existing",
        "email": "stranger@example.com",
        "roles": ["report_publisher"],
        "full_name": "Existing Person",
        "metadata": {"google_sub": "google-sub-123"},
    }
    with mock.patch.object(GoogleSsoService, "find_user_by_email", return_value=existing):
        result = GoogleSsoService.sign_in_or_provision(_GOOGLE_USERINFO, allow_provision=False)

    assert result["success"] is True
    assert result["user_action"] == "signin"
    assert result["username"] == "stranger_existing"


def test_microsoft_unmatched_account_refused_when_provision_disallowed():
    with (
        mock.patch.object(MicrosoftSsoService, "find_user_by_microsoft_sub", return_value=None),
        mock.patch.object(MicrosoftSsoService, "find_user_by_email", return_value=None),
        mock.patch(
            "asset_management.web.services.user_provisioning_service.UserProvisioningService.create_user"
        ) as create_user,
    ):
        result = MicrosoftSsoService.sign_in_or_provision(_microsoft_userinfo(), allow_provision=False)

    assert result["success"] is False
    assert result.get("code") == "signup_disabled"
    create_user.assert_not_called()


def test_service_layer_default_remains_permissive():
    """The default-off posture lives in config + route layer. The service
    default stays True so existing callers/tests are unaffected; the
    route MUST pass the flag explicitly (asserted by the route reading
    ``config.SELF_SIGNUP_ENABLED`` — covered by SIGNUP-GATE-005/006)."""
    import inspect

    sig_g = inspect.signature(GoogleSsoService.sign_in_or_provision)
    sig_m = inspect.signature(MicrosoftSsoService.sign_in_or_provision)
    assert sig_g.parameters["allow_provision"].default is True
    assert sig_m.parameters["allow_provision"].default is True


def test_config_flag_defaults_false(monkeypatch):
    from asset_management.web import config as config_module

    monkeypatch.delenv("SELF_SIGNUP_ENABLED", raising=False)
    cfg = config_module.Config()
    cfg._config = {}
    assert cfg.SELF_SIGNUP_ENABLED is False


def test_config_flag_parses_truthy(monkeypatch):
    from asset_management.web import config as config_module

    cfg = config_module.Config()
    cfg._config = {}
    monkeypatch.setenv("SELF_SIGNUP_ENABLED", "true")
    assert cfg.SELF_SIGNUP_ENABLED is True
    monkeypatch.setenv("SELF_SIGNUP_ENABLED", "false")
    assert cfg.SELF_SIGNUP_ENABLED is False
    # master-config bool (already-parsed YAML) is honored too
    monkeypatch.delenv("SELF_SIGNUP_ENABLED", raising=False)
    cfg._config = {"SELF_SIGNUP_ENABLED": True}
    assert cfg.SELF_SIGNUP_ENABLED is True
