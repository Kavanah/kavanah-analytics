"""MFA Recovery Email Service.

Sends the two MFA email-recovery messages — the recovery-address verification
link and the time-limited recovery-login link. Follows the established
transactional-email pattern (``BillingEmailService`` / ``invite_service``):
SMTP settings from the shared ``password_reset/config/settings.json``, the same
branded light-only table shell, a pure ``render`` plus a synchronous best-effort
``send`` that never raises.

Send is synchronous (not the billing async dispatch) because both call sites —
the challenge-screen "send recovery link" POST and the account-settings "verify
email" POST — want the inline result to reflect what actually happened (the
invite-modal convention), and recovery sends are rare.
"""

from __future__ import annotations

import json
import logging
import smtplib

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, "/app/python")

try:
    from web.services.smtp_settings import load_smtp_settings
except ImportError:
    from ..services.smtp_settings import load_smtp_settings

_PASSWORD_RESET_SETTINGS = "/app/python/password_reset/config/settings.json"


def _get_smtp_settings() -> dict:
    """Env-aware shared SMTP settings: PW_RESET_SMTP_* env
    credentials win over settings.json, which no longer stores them."""
    return load_smtp_settings("MfaRecoveryEmailService")


def _get_app_base_url(settings: dict | None = None) -> str:
    """Derive ``scheme://host`` from the SMTP settings' ``app_url``."""
    settings = settings if settings is not None else _get_smtp_settings()
    app_url = (settings.get("app_url") or "").rstrip("/")
    if not app_url:
        return ""
    from urllib.parse import urlparse

    parsed = urlparse(app_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _shell(subject: str, heading: str, body_html: str, settings: dict) -> str:
    """Branded, email-client-safe table shell (matches the billing/onboarding
    emails)."""
    logo_url = f"{_get_app_base_url(settings)}/static/kavanah-analytics-logo.svg"
    return f"""<!DOCTYPE html>
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
          <td style="vertical-align:middle;padding-left:10px;color:#ffffff;font-size:17px;font-weight:700;">Kavanah Analytics</td>
        </tr></table>
      </td></tr>
      <tr><td style="padding:32px 28px;background-color:#ffffff;color:#000716;font-size:15px;line-height:1.6;">
        <h2 style="margin:0 0 16px;color:#000716;font-size:20px;font-weight:700;">{heading}</h2>
        {body_html}
      </td></tr>
      <tr><td style="padding:16px 28px;background-color:#FAFAFA;border-top:1px solid #e9ebed;border-radius:0 0 8px 8px;font-size:12px;color:#5f6b7a;">
        Kavanah Analytics · This is an automated security message. If you did not request it, you can ignore this email.
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def _button(url: str, label: str) -> str:
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0;"><tr>'
        f'<td style="border-radius:4px;background-color:#0972d3;">'
        f'<a href="{url}" style="display:inline-block;padding:12px 24px;color:#ffffff;'
        f'text-decoration:none;font-weight:700;font-size:15px;">{label}</a></td></tr></table>'
    )


class MfaRecoveryEmailService:
    """Transactional MFA-recovery emails."""

    @staticmethod
    def render(kind: str, ctx: dict, settings: dict) -> dict:
        """Return ``{"subject", "html", "text"}`` for an email ``kind``.

        ``kind`` ∈ {recovery_email_verify, recovery_login}. Unknown kinds raise
        ValueError (a programming error, not runtime input).
        """
        link = ctx.get("link", "")
        if kind == "recovery_email_verify":
            subject = "Verify your recovery email — Kavanah Analytics"
            heading = "Confirm your recovery email"
            html = (
                "<p>You added this address as a two-factor recovery email for your "
                "Kavanah Analytics account. Confirm it so it can be used to regain "
                "access if you lose your authenticator.</p>"
                f"{_button(link, 'Verify recovery email')}"
                '<p style="font-size:13px;color:#5f6b7a;">If you didn\'t request this, '
                "you can safely ignore this email.</p>"
            )
            text = (
                "Confirm your recovery email for Kavanah Analytics two-factor "
                f"authentication:\n\n{link}\n\n"
                "If you didn't request this, ignore this email."
            )
        elif kind == "recovery_login":
            subject = "Your two-factor recovery link — Kavanah Analytics"
            heading = "Recover access to your account"
            html = (
                "<p>You asked to recover access to your Kavanah Analytics account "
                "because you can't use your authenticator. This link disables "
                "two-factor authentication and signs you in so you can set it up "
                "again.</p>"
                f"{_button(link, 'Recover my account')}"
                '<p style="font-size:13px;color:#5f6b7a;">This link expires in '
                "15 minutes and can only be used once. If you didn't request it, "
                "ignore this email — your account is unchanged.</p>"
            )
            text = (
                "Recover access to your Kavanah Analytics account (this disables "
                "two-factor authentication and signs you in):\n\n"
                f"{link}\n\n"
                "This link expires in 15 minutes and can be used once. If you "
                "didn't request it, ignore this email."
            )
        else:
            raise ValueError(f"unknown MFA recovery email kind: {kind}")

        return {"subject": subject, "html": _shell(subject, heading, html, settings), "text": text}

    @staticmethod
    def send(kind: str, to_email: str, ctx: dict) -> dict:
        """Render + send synchronously. Best-effort: returns
        ``{"success": bool, "error": str}`` and never raises."""
        if not to_email:
            return {"success": False, "error": "no recipient"}
        settings = _get_smtp_settings()
        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {"success": False, "error": f"SMTP not configured (missing: {', '.join(missing)})"}

        try:
            rendered = MfaRecoveryEmailService.render(kind, ctx, settings)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        msg = MIMEMultipart("alternative")
        msg["Subject"] = rendered["subject"]
        msg["From"] = settings["from_email"]
        msg["To"] = to_email
        msg.attach(MIMEText(rendered["text"], "plain"))
        msg.attach(MIMEText(rendered["html"], "html"))

        try:
            if settings.get("smtp_use_tls", True):
                server = smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS)
                server.starttls(context=smtp_ssl_context())
            else:
                server = smtplib.SMTP_SSL(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context()
                )
            server.login(settings["smtp_username"], settings["smtp_password"])
            server.sendmail(settings["from_email"], to_email, msg.as_string())
            server.quit()
            logging.info("MfaRecoveryEmailService: %s email sent to %s", kind, to_email)
            return {"success": True}
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("MfaRecoveryEmailService: failed to send %s to %s: %s", kind, to_email, e)
            return {"success": False, "error": str(e)}
