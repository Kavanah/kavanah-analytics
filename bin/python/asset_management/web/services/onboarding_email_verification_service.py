"""Onboarding Email Verification Service.

Async, non-blocking email verification for the new-user onboarding
wizard. Distinct from ``RegistrationService`` which gates
account creation behind verification + admin approval: this flow runs
in parallel with wizard steps 2–6 and only the final handoff (step 7,
not yet built) checks the flag.

Storage contract: verification state lives on the ES Security user
``metadata`` dict as the ``email_verified`` boolean (mirrors the
 ``account_type`` precedent — single source of truth on the
user record, no new index). Stamp helper merges onto existing metadata
so sibling keys are preserved.

Token contract: HS256 JWT signed with the Flask SECRET_KEY, 24-hour
expiry, ``purpose="onboarding_email_verify"`` claim. Subject is the
username (not a registration_id — the legacy /signup flow uses the
pending-registration doc id; the wizard creates the ES user up-front
so username is the natural key).

Send contract: SMTP via the shared ``/app/python/password_reset/config/
settings.json`` settings file. Send runs on a background daemon thread
so the wizard POST returns in <100ms even if SMTP is slow — the AC says
"User can complete steps 2–6 without waiting on verification."

Out of scope (deferred to follow-up tickets):
  - Wizard step-7 soft-block UI (step 7 doesn't exist in code yet).
  - Post-wizard checklist resend affordance (same — no checklist yet).
  - Re-verification when a user changes their email (no email-change UI).
"""

from __future__ import annotations

import html
import json
import logging
import os
import smtplib

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context
import sys
import threading
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import jwt

# Tests patch ``get_elasticsearch_client`` on this module (/
# pattern). Top-level import + sys.path
# insert mirrors sibling services so resolution works in both the
# imports_container (where /app/python is the root) and pytest (where
# conftest extends sys.path).
sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client, get_es_admin_client  # noqa: E402

try:
    from web.services.smtp_settings import load_smtp_settings
except ImportError:
    from ..services.smtp_settings import load_smtp_settings

_PASSWORD_RESET_SETTINGS = "/app/python/password_reset/config/settings.json"

# 24-hour expiry, same as the legacy /signup verification token. Long
# enough for users on mobile to switch apps and tap the link; short
# enough that a stolen token loses value quickly.
VERIFICATION_TOKEN_EXPIRY_SECONDS = 86400

# JWT ``purpose`` claim — distinct from the legacy /signup
# ``email_verify`` value so a legacy token can't unlock a wizard
# account (different storage layer, different audit shape).
TOKEN_PURPOSE = "onboarding_email_verify"


def _get_smtp_settings() -> dict:
    """Env-aware shared SMTP settings: PW_RESET_SMTP_* env
    credentials win over settings.json, which no longer stores them."""
    return load_smtp_settings("OnboardingEmailVerificationService")


def _get_app_base_url(settings: dict | None = None) -> str:
    """Derive the application base URL (scheme://host) from SMTP settings.

    The settings file stores ``app_url`` as the password-reset app's
    own base (e.g. ``https://host/reset-password``) — strip the path
    so the wizard verify URL builds cleanly off scheme+host.
    """
    settings = settings if settings is not None else _get_smtp_settings()
    app_url = (settings.get("app_url") or "").rstrip("/")
    if not app_url:
        return ""
    from urllib.parse import urlparse

    parsed = urlparse(app_url)
    return f"{parsed.scheme}://{parsed.netloc}"


class OnboardingEmailVerificationService:
    """Async email verification for the onboarding wizard."""

    # ------------------------------------------------------------------
    # Token generation / verification
    # ------------------------------------------------------------------

    @staticmethod
    def generate_verification_token(username: str, secret_key: str) -> str:
        """Generate a 24-hour HS256 JWT for ``username``.

        Encodes ``sub=username``, ``purpose=onboarding_email_verify``,
        and ``exp`` 24h in the future. Distinct purpose from the
        legacy /signup token so the verify route can refuse mis-typed
        tokens cleanly.
        """
        now = int(time.time())
        payload = {
            "sub": username,
            "purpose": TOKEN_PURPOSE,
            "exp": now + VERIFICATION_TOKEN_EXPIRY_SECONDS,
            "iat": now,
        }
        return jwt.encode(payload, secret_key, algorithm="HS256")

    @staticmethod
    def verify_token(token: str, secret_key: str) -> dict:
        """Decode and validate an onboarding verification token.

        Returns ``{"valid": True, "username": <str>}`` on success or
        ``{"valid": False, "error": <reason>}`` on failure. Failure
        reasons are user-facing so they go in the verify route's
        flash message.
        """
        try:
            payload = jwt.decode(token, secret_key, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return {"valid": False, "error": "Verification link has expired."}
        except jwt.InvalidTokenError:
            return {"valid": False, "error": "Verification link is invalid."}

        if payload.get("purpose") != TOKEN_PURPOSE:
            return {"valid": False, "error": "Verification link is invalid."}

        username = payload.get("sub")
        if not isinstance(username, str) or not username:
            return {"valid": False, "error": "Verification link is invalid."}
        return {"valid": True, "username": username}

    # ------------------------------------------------------------------
    # Metadata stamp
    # ------------------------------------------------------------------

    @staticmethod
    def mark_email_verified(username: str) -> dict:
        """Set ``metadata.email_verified=true`` on the ES Security user.

        Preserves all other metadata keys (force_password_change,
        google_sub, microsoft_sub, account_type, …) by merging onto
        the existing dict — ES Security's ``put_user`` is an upsert
        that overwrites metadata wholesale.

        Returns ``{"success": bool, "already_verified": bool, "error": str}``.
        ``already_verified=True`` is not a failure — the verify route
        renders a friendly "already verified" page on that branch.
        """
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        try:
            current = dict(es.security.get_user(username=username)).get(username)
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error(
                "OnboardingEmailVerificationService.mark_email_verified: " "get_user('%s') failed: %s",
                username,
                e,
            )
            return {"success": False, "error": "User lookup failed"}

        if not current:
            return {"success": False, "error": "User not found"}

        metadata = dict(current.get("metadata") or {})
        if metadata.get("email_verified") is True:
            return {"success": True, "already_verified": True}

        metadata["email_verified"] = True
        metadata["email_verified_at"] = datetime.now(timezone.utc).isoformat()

        try:
            get_es_admin_client(es).security.put_user(
                username=username,
                body={
                    "roles": current.get("roles", []),
                    "full_name": current.get("full_name", ""),
                    "email": current.get("email", ""),
                    # ES writes enabled=true when it is omitted.
                    "enabled": current.get("enabled", True),
                    "metadata": metadata,
                },
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error(
                "OnboardingEmailVerificationService.mark_email_verified: " "put_user('%s') failed: %s",
                username,
                e,
            )
            return {"success": False, "error": "Could not persist verification"}

        logging.info(
            "AUDIT: action=onboarding_email_verified username=%s",
            username,
        )
        return {"success": True, "already_verified": False}

    @staticmethod
    def update_email(username: str, new_email: str) -> dict:
        """follow-up — change the user's email on their ES
        Security record AND reset ``metadata.email_verified`` to False.

        Used by the wizard verify-email step when the user realises
        they typo'd their email on signup. The flow is: user clicks
        "Change email" on /onboarding/verify-email-step, types a new
        address, submits → this method updates the ES Security user's
        email field + clears the email_verified flag → the caller
        dispatches a fresh verification email to the new address.

        The Continue button stays disabled (email_verified=False
        again) until the user clicks the link in the new inbox, at
        which point the existing /verify route flips the flag back
        to True via ``mark_email_verified`` and the polling JS
        re-enables Continue.

        Validation:
        - ``new_email`` must be a non-empty string containing ``@``.
          Anything more strict (RFC compliance, MX lookup) is too
          tight for a wizard step; the verification-email send is
          the load-bearing "is this address real?" gate.
        - If the new email is the same as the current one (case-
          insensitive trim), no-op with ``unchanged=True`` so the
          route can skip the dispatch.

        Returns ``{"success": bool, "unchanged": bool, "error": str}``.
        """
        cleaned = (new_email or "").strip()
        if not cleaned or "@" not in cleaned:
            return {"success": False, "error": "Enter a valid email address."}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        try:
            current = dict(es.security.get_user(username=username)).get(username)
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error(
                "OnboardingEmailVerificationService.update_email: " "get_user('%s') failed: %s",
                username,
                e,
            )
            return {"success": False, "error": "User lookup failed"}

        if not current:
            return {"success": False, "error": "User not found"}

        existing_email = (current.get("email") or "").strip()
        if existing_email.lower() == cleaned.lower():
            return {"success": True, "unchanged": True}

        # refuse to move this user's email onto an address already
        # owned by a DIFFERENT account. ES Security has no email index, so we
        # enumerate get_user() and compare case-insensitively (same approach
        # as OnboardingSignupService.find_user_by_email), skipping this user's
        # own record. Generic message — this surface is reachable mid-wizard
        # by the account owner, who shouldn't be able to probe other accounts.
        # Fail-open on the enumeration error: if get_user() raises here the
        # outer try/except already covers the put_user, and a transient blip
        # shouldn't trap the user on the verify step.
        try:
            all_users = dict(es.security.get_user())
            wanted = cleaned.lower()
            for other_username, other_record in all_users.items():
                if other_username.strip().lower() == username.strip().lower():
                    continue
                other_email = (other_record.get("email") or "").strip().lower()
                if other_email and other_email == wanted:
                    return {"success": False, "error": "That email address is already in use."}
        except Exception as e:  # noqa: BLE001 — fail-open enumeration
            logging.error(
                "OnboardingEmailVerificationService.update_email: " "duplicate-email enumeration failed for '%s': %s",
                username,
                e,
            )

        metadata = dict(current.get("metadata") or {})
        # Reset email_verified — the new address hasn't been confirmed
        # yet. The verify route's mark_email_verified will flip this
        # back to True when the user clicks the link sent to the new
        # inbox below.
        metadata.pop("email_verified", None)
        metadata.pop("email_verified_at", None)
        # Track that the email was changed during onboarding so a
        # later audit can answer "who changed what when".
        metadata["email_changed_at"] = datetime.now(timezone.utc).isoformat()
        metadata["email_changed_from"] = existing_email

        try:
            get_es_admin_client(es).security.put_user(
                username=username,
                body={
                    "roles": current.get("roles", []),
                    "full_name": current.get("full_name", ""),
                    "email": cleaned,
                    # ES writes enabled=true when it is omitted.
                    "enabled": current.get("enabled", True),
                    "metadata": metadata,
                },
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error(
                "OnboardingEmailVerificationService.update_email: " "put_user('%s') failed: %s",
                username,
                e,
            )
            return {"success": False, "error": "Could not update email"}

        logging.info(
            "AUDIT: action=onboarding_email_changed username=%s from=%s to=%s",
            username,
            existing_email,
            cleaned,
        )
        return {"success": True, "unchanged": False}

    @staticmethod
    def is_email_verified(username: str) -> bool:
        """Read-side helper: True iff ``metadata.email_verified`` is set.

        Used by the future step-7 handoff soft-block. Returns False on
        any error path so the gate fails closed — an unverified user
        with a transient ES blip should not bypass the check.
        """
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            current = dict(es.security.get_user(username=username)).get(username)
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.warning(
                "OnboardingEmailVerificationService.is_email_verified: " "get_user('%s') failed: %s",
                username,
                e,
            )
            return False
        if not current:
            return False
        return bool((current.get("metadata") or {}).get("email_verified"))

    # ------------------------------------------------------------------
    # SMTP send
    # ------------------------------------------------------------------

    @staticmethod
    def send_verification_email(
        email: str,
        username: str,
        full_name: str,
        verification_url: str,
    ) -> dict:
        """Send the verification email synchronously.

        Returns ``{"success": bool, "error": str}``. The async wrapper
        (``dispatch_verification_email``) calls this on a daemon
        thread so the wizard POST never blocks on SMTP.
        """
        settings = _get_smtp_settings()
        required = [
            "smtp_host",
            "smtp_port",
            "smtp_username",
            "smtp_password",
            "from_email",
        ]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {
                "success": False,
                "error": f"SMTP not configured (missing: {', '.join(missing)})",
            }

        greeting = f" {full_name}" if full_name else ""
        # (SVC-006): HTML-escaped variants for the html_body (the plain
        # greeting/username stay raw for the text/plain part).
        greeting_html = f" {html.escape(full_name)}" if full_name else ""
        username_html = html.escape(username)
        subject = "Verify your email — Kavanah Analytics"
        logo_url = f"{_get_app_base_url(settings)}/static/kavanah-analytics-logo.svg"

        html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background-color:#F1F3F3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#000716;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F1F3F3;">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:8px;border:1px solid #e9ebed;">
      <tr><td style="background-color:#232F3E;padding:16px 24px;border-radius:8px 8px 0 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="vertical-align:middle;width:32px;"><img src="{logo_url}" width="28" height="28" alt="" style="display:block;border:0;"></td>
          <td style="vertical-align:middle;padding-left:10px;color:#ffffff;font-size:17px;font-weight:700;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Kavanah Analytics</td>
        </tr></table>
      </td></tr>
      <tr><td style="padding:32px 28px;background-color:#ffffff;color:#000716;font-size:15px;line-height:1.6;">
        <h2 style="margin:0 0 16px;color:#000716;font-size:20px;font-weight:700;">Verify your email address</h2>
        <p style="margin:0 0 12px;">Hello{greeting_html},</p>
        <p style="margin:0 0 12px;">Thanks for signing up. We need to verify your email before you can finish setting up your account. Tap the button below within the next 24 hours to confirm we have the right address.</p>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0;"><tr>
          <td style="border-radius:6px;background-color:#0972D3;">
            <a href="{verification_url}" style="display:inline-block;padding:14px 32px;color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;border-radius:6px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Verify email</a>
          </td>
        </tr></table>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FAFAFA;border:1px solid #D5DBDB;border-radius:6px;margin:20px 0;">
          <tr><td style="padding:14px 18px;font-size:14px;color:#000716;">
            <p style="margin:0 0 6px;"><strong>Username:</strong> {username_html}</p>
            <p style="margin:0;">This link expires in 24 hours.</p>
          </td></tr>
        </table>
        <p style="margin:16px 0 4px;font-size:13px;color:#5F6B7A;">If the button doesn't work, copy and paste this URL into your browser:</p>
        <p style="margin:0 0 16px;"><a href="{verification_url}" style="color:#0972D3;word-break:break-all;">{verification_url}</a></p>
        <p style="margin:0;font-size:13px;color:#5F6B7A;">If you didn't create this account, you can safely ignore this email.</p>
      </td></tr>
      <tr><td style="padding:16px 28px 24px;border-top:1px solid #e9ebed;background-color:#ffffff;color:#5F6B7A;font-size:12px;line-height:1.5;border-radius:0 0 8px 8px;">
        This is an automated message — please do not reply.
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

        text_body = (
            f"Verify your email — Kavanah Analytics\n\n"
            f"Hello{greeting},\n\n"
            f"Thanks for signing up. We need to verify your email before you can finish "
            f"setting up your account. Visit the link below within the next 24 hours to "
            f"confirm we have the right address:\n\n"
            f"{verification_url}\n\n"
            f"Username: {username}\n"
            f"This link expires in 24 hours.\n\n"
            f"If you didn't create this account, you can safely ignore this email."
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings["from_email"]
        msg["To"] = email
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        try:
            if settings.get("smtp_use_tls", True):
                server = smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS)
                server.starttls(context=smtp_ssl_context())
            else:
                server = smtplib.SMTP_SSL(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context()
                )
            server.login(settings["smtp_username"], settings["smtp_password"])
            server.sendmail(settings["from_email"], email, msg.as_string())
            server.quit()
            logging.info(
                "OnboardingEmailVerificationService: verification email sent to %s for '%s'",
                email,
                username,
            )
            return {"success": True}
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error(
                "OnboardingEmailVerificationService: failed to send to %s: %s",
                email,
                e,
            )
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Async dispatch
    # ------------------------------------------------------------------

    @staticmethod
    def build_verification_url(token: str, app_base_url: str | None = None) -> str:
        """Build the absolute verification URL for the email body.

        Prefers an explicit ``app_base_url`` from the caller (the route
        has a live request and can use ``url_for(..., _external=True)``);
        falls back to the SMTP-settings ``app_url`` host when called
        from a background thread that has no request context.
        """
        base = (app_base_url or "").rstrip("/")
        if not base:
            base = _get_app_base_url()
        return f"{base}/onboarding/verify/{token}"

    @staticmethod
    def dispatch_verification_email(
        username: str,
        email: str,
        full_name: str,
        secret_key: str,
        *,
        app_base_url: str | None = None,
        _send_fn: Any = None,
    ) -> threading.Thread:
        """Generate a token and send the email on a daemon thread.

        Returns the started ``Thread`` so tests can ``.join()`` it.
        Production callers (the signup POST handler) ignore the
        return value — the AC requires "User can complete steps 2–6
        without waiting on verification."

        ``_send_fn`` is a seam for tests to swap in a recording stub
        without monkey-patching SMTP. Production passes ``None`` and
        the real ``send_verification_email`` is used.
        """
        token = OnboardingEmailVerificationService.generate_verification_token(username, secret_key)
        verification_url = OnboardingEmailVerificationService.build_verification_url(token, app_base_url=app_base_url)
        sender = _send_fn or OnboardingEmailVerificationService.send_verification_email

        def _runner() -> None:
            try:
                sender(
                    email=email,
                    username=username,
                    full_name=full_name,
                    verification_url=verification_url,
                )
            except Exception as e:  # noqa: BLE001 — daemon thread, last-line guard
                logging.error(
                    "OnboardingEmailVerificationService.dispatch: send failed for '%s': %s",
                    username,
                    e,
                )

        thread = threading.Thread(
            target=_runner,
            name=f"onboarding-verify-email-{username}",
            daemon=True,
        )
        thread.start()
        return thread
