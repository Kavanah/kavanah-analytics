"""New-signup staff notification.

Emails staff when a new account is created through the onboarding wizard:
the email+password POST and the Google / Microsoft SSO callbacks' signup
branch. Before this, nothing told anyone a signup had happened.

Recipients come from ``config.SIGNUP_NOTIFY_EMAIL`` (master-config.yaml
``notifications.signup_email``, overridden by the ``SIGNUP_NOTIFY_EMAIL``
env var), comma-separated. Empty means off.

Send contract: same shared SMTP settings as the verification email, on a
daemon thread so the signup request never waits on SMTP and a send failure
never reaches the user. Failures are logged at ERROR.
"""

from __future__ import annotations

import logging
import smtplib
import sys
import threading
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context

sys.path.insert(0, "/app/python")

try:
    from web.services.smtp_settings import load_smtp_settings
except ImportError:
    from ..services.smtp_settings import load_smtp_settings

# Human labels for the auth_method values the routes already use.
VIA_LABELS = {
    "password": "Email and password",
    "google_sso": "Google",
    "microsoft_sso": "Microsoft",
}

_SUBJECT_NAME_MAX = 80


def _one_line(value: str) -> str:
    """Collapse CR/LF so a user-typed name can't add mail headers."""
    return " ".join((value or "").splitlines()).strip()


class SignupNotificationService:
    """Builds and sends the staff email for a new account."""

    @staticmethod
    def recipients() -> list[str]:
        """The configured recipient addresses; empty when the notice is off."""
        from asset_management.web.config import config

        raw = config.SIGNUP_NOTIFY_EMAIL or ""
        return [addr.strip() for addr in raw.split(",") if addr.strip()]

    @staticmethod
    def build_message(
        *,
        username: str,
        full_name: str,
        email: str,
        via: str,
        invited: bool,
        from_email: str,
        recipients: list[str],
        now: datetime | None = None,
    ) -> MIMEText:
        """Plain-text notice: who signed up, how, and when (UTC)."""
        when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
        name = _one_line(full_name) or _one_line(username)
        subject = f"New Kavanah Analytics signup: {name[:_SUBJECT_NAME_MAX]}"
        body = (
            "A new account was just created on Kavanah Analytics.\n\n"
            f"Name: {_one_line(full_name) or '(not given)'}\n"
            f"Username: {_one_line(username)}\n"
            f"Email: {_one_line(email) or '(not given)'}\n"
            f"Signed up with: {VIA_LABELS.get(via, via)}\n"
            f"Invite or signup link used: {'Yes' if invited else 'No'}\n"
            f"Time: {when}\n\n"
            "They are now in the setup wizard. A person who stops after this step "
            "leaves an account with no workspace.\n"
        )
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = ", ".join(recipients)
        return msg

    @staticmethod
    def send_signup_notification(
        *,
        username: str,
        full_name: str,
        email: str,
        via: str,
        invited: bool,
        recipients: list[str],
    ) -> dict:
        """Send the notice synchronously. Returns ``{"success": bool, "error": str}``."""
        settings = load_smtp_settings("SignupNotificationService")
        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {"success": False, "error": f"SMTP not configured (missing: {', '.join(missing)})"}

        msg = SignupNotificationService.build_message(
            username=username,
            full_name=full_name,
            email=email,
            via=via,
            invited=invited,
            from_email=settings["from_email"],
            recipients=recipients,
        )
        try:
            if settings.get("smtp_use_tls", True):
                server = smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS)
                server.starttls(context=smtp_ssl_context())
            else:
                server = smtplib.SMTP_SSL(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context()
                )
            server.login(settings["smtp_username"], settings["smtp_password"])
            server.sendmail(settings["from_email"], recipients, msg.as_string())
            server.quit()
            logging.info("SignupNotificationService: signup notice sent for '%s'", username)
            return {"success": True}
        except Exception as e:  # noqa: BLE001 — top-level boundary
            return {"success": False, "error": str(e)}

    @staticmethod
    def dispatch_signup_notification(
        *,
        username: str,
        full_name: str,
        email: str,
        via: str,
        invited: bool,
        _send_fn: Any = None,
    ) -> threading.Thread | None:
        """Send the notice on a daemon thread. Returns ``None`` when no
        recipient is configured, else the started thread (tests join it).

        ``_send_fn`` is a test seam, as in the verification dispatcher.
        """
        recipients = SignupNotificationService.recipients()
        if not recipients:
            return None
        sender = _send_fn or SignupNotificationService.send_signup_notification

        def _runner() -> None:
            try:
                result = sender(
                    username=username,
                    full_name=full_name,
                    email=email,
                    via=via,
                    invited=invited,
                    recipients=recipients,
                )
            except Exception as e:  # noqa: BLE001 — daemon thread, last-line guard
                result = {"success": False, "error": str(e)}
            if not result.get("success"):
                logging.error(
                    "SignupNotificationService: signup notice for '%s' not sent: %s",
                    username,
                    result.get("error"),
                )

        thread = threading.Thread(target=_runner, name=f"signup-notice-{username}", daemon=True)
        thread.start()
        return thread
