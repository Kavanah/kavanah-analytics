"""
User Provisioning Service
Handles new user creation, temporary password generation, welcome emails,
and forced password change flag management.
"""

import html
import json
import logging
import os
import secrets
import smtplib

from datetime import datetime, timezone

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context
import string
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Special characters accepted in admin-set passwords. Excludes characters
# that mangle in shells / URLs without a clear security benefit.
_ADMIN_PASSWORD_SPECIALS = "!@#$%^&*()-_=+[]{};:,.<>?/\\|~"

# Path to password-reset app's SMTP config (both apps share the same container)
try:
    from web.services.smtp_settings import load_smtp_settings
except ImportError:
    from ..services.smtp_settings import load_smtp_settings

_PASSWORD_RESET_SETTINGS = "/app/python/password_reset/config/settings.json"


def _get_smtp_settings():
    """Env-aware shared SMTP settings: PW_RESET_SMTP_* env
    credentials win over settings.json, which no longer stores them."""
    return load_smtp_settings("UserProvisioningService")


class UserProvisioningService:

    @staticmethod
    def generate_temp_password():
        """
        Generate a secure 16-character random temporary password.
        Guarantees at least one uppercase letter, one lowercase letter,
        and one digit; remaining characters are drawn from the full set.
        """
        upper = secrets.choice(string.ascii_uppercase)
        lower = secrets.choice(string.ascii_lowercase)
        digit = secrets.choice(string.digits)
        alphabet = string.ascii_letters + string.digits
        rest = [secrets.choice(alphabet) for _ in range(13)]
        parts = list(upper + lower + digit) + rest
        secrets.SystemRandom().shuffle(parts)
        return "".join(parts)

    @staticmethod
    def _username_exists(es, username):
        """Return True iff an Elasticsearch security user with this exact name exists.

        Uses the ES security get_user API. ES returns 404 (NotFoundError) for
        unknown users, which we map to False. Any other error is logged and
        re-raised so the caller can surface it instead of silently treating
        the lookup as "no collision" — that would re-introduce the overwrite
        bug this guard exists to prevent.
        """
        from elasticsearch.exceptions import NotFoundError

        try:
            response = es.security.get_user(username=username)
        except NotFoundError:
            return False
        return bool(dict(response).get(username))

    @staticmethod
    def suggest_unique_username(base, max_attempts=99):
        """Suggest a non-colliding variant of ``base``.

        If ``base`` already ends in digits, increment the trailing number;
        otherwise append "2" and increment from there (jsmith → jsmith2 →
        jsmith3, jsmith9 → jsmith10). Returns the first variant that is not
        already a registered ES user. Falls back to base + a 4-digit random
        suffix after ``max_attempts`` collisions.

        Returns the input unchanged if ES is unreachable — the caller will
        re-check at write time and surface any collision as a hard failure.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return base

        # Trailing-digit split without a lazy regex (``^(.*?)(\d+)$``
        # is quadratic on long digit-heavy names).
        stem = base.rstrip("0123456789")
        num_str = base[len(stem) :]
        if num_str:
            counter = int(num_str) + 1
        else:
            stem, counter = base, 2

        for _ in range(max_attempts):
            candidate = f"{stem}{counter}"
            if not UserProvisioningService._username_exists(es, candidate):
                return candidate
            counter += 1

        return f"{base}{secrets.randbelow(9000) + 1000}"

    @staticmethod
    def find_user_by_email(email, exclude_username=None):
        """Return the first ES Security user whose stored email matches ``email``.

        shared duplicate-email detector for the admin Create-User /
        update-email / self-service-profile / wizard-change-email surfaces
        (the anonymous signup path has its own copy in OnboardingSignupService,
        kept separate so the wizard service doesn't import the provisioning
        service). ES Security has no email index, so we enumerate ``get_user()``
        and compare case-insensitively, mirroring the lookup.

        ``exclude_username`` is the record to ignore — an *update* surface must
        not flag the user's own current email as a collision. Compared after
        lowercase normalization.

        Returns ``{"username": <str>, ...record}`` on a match, else ``None``.
        Fails open (returns ``None``) when ES is unreachable or ``get_user``
        raises, so a transient ES blip never blocks a legitimate write — the
        username-collision guard in ``create_user`` is the hard backstop.
        """
        from lib.es_helpers import get_elasticsearch_client

        if not email:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            response = dict(es.security.get_user())
        except Exception as e:  # noqa: BLE001 — top-level boundary, fail-open
            logging.error("UserProvisioningService.find_user_by_email: get_user failed: %s", e)
            return None

        wanted = email.strip().lower()
        skip = (exclude_username or "").strip().lower()
        for username, record in response.items():
            if skip and username.strip().lower() == skip:
                continue
            stored_email = (record.get("email") or "").strip().lower()
            if stored_email and stored_email == wanted:
                return {"username": username, **record}
        return None

    @staticmethod
    def email_in_use(email, exclude_username=None):
        """Convenience boolean wrapper around ``find_user_by_email``."""
        return UserProvisioningService.find_user_by_email(email, exclude_username) is not None

    @staticmethod
    def create_user(username, full_name, email, roles):
        """
        Create a new Elasticsearch user with a temporary password and
        the force_password_change metadata flag set to True.

        Refuses to create a user whose name already exists — the ES
        security ``put_user`` API is an upsert keyed on username, so without
        this guard a duplicate name would silently overwrite the existing
        record. On collision, returns ``code="username_taken"`` and a
        ``suggested_username`` the caller can offer the admin.

        Args:
            username (str): Elasticsearch username (must be unique)
            full_name (str): User's display name
            email (str): User's email address (may be empty for superuser-created test accounts)
            roles (list[str]): List of ES role names to assign

        Returns:
            dict: {success: bool, temp_password: str, error: str,
                   code: str (only on failure), suggested_username: str (only on collision)}
        """
        from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        if UserProvisioningService._username_exists(es, username):
            suggestion = UserProvisioningService.suggest_unique_username(username)
            logging.info(
                "AUDIT: action=create_user_rejected reason=username_taken username=%s suggestion=%s",
                username,
                suggestion,
            )
            return {
                "success": False,
                "code": "username_taken",
                "error": f"Username '{username}' is already taken. Try '{suggestion}' instead.",
                "suggested_username": suggestion,
            }

        # refuse to create a second account on an email already
        # owned by another user. ES Security keys put_user on username, so a
        # duplicate email is otherwise silently accepted (two accounts, same
        # inbox — ambiguous for password-reset and the sign-in flow).
        # Skipped for emailless superuser-created test accounts (empty email).
        if email:
            existing_email_owner = UserProvisioningService.find_user_by_email(email)
            if existing_email_owner is not None:
                owner = existing_email_owner.get("username", "")
                logging.info(
                    "AUDIT: action=create_user_rejected reason=email_taken username=%s email=%s owner=%s",
                    username,
                    email,
                    owner,
                )
                return {
                    "success": False,
                    "code": "email_taken",
                    "error": f"Email '{email}' is already in use by user '{owner}'.",
                    "existing_username": owner,
                }

        temp_password = UserProvisioningService.generate_temp_password()

        try:
            get_es_admin_client(es).security.put_user(
                username=username,
                body={
                    "password": temp_password,
                    "roles": roles,
                    "full_name": full_name,
                    "email": email,
                    "metadata": {"force_password_change": True},
                },
            )
            logging.info(
                "AUDIT: action=create_user username=%s roles=%s email=%s",
                username,
                roles,
                email,
            )
            return {"success": True, "temp_password": temp_password}
        except Exception as e:
            logging.error("UserProvisioningService: failed to create user '%s': %s", username, e)
            # ES's own text stays in the log; its username
            # validation failure is the one case the admin can fix, so name it.
            if "validation failed" in str(e).lower() and "username" in str(e).lower():
                return {
                    "success": False,
                    "error": "That username is not valid. Use printable characters with no leading or trailing spaces.",
                }
            return {"success": False, "error": "Could not create user."}

    @staticmethod
    def send_welcome_email(email, username, temp_password, full_name=""):
        """
        Send a welcome email containing the username and temporary password.
        Reads SMTP settings from the password-reset app's config file.

        Args:
            email (str): Recipient email address
            username (str): Elasticsearch username
            temp_password (str): The generated temporary password
            full_name (str): Optional display name for the greeting

        Returns:
            dict: {success: bool, error: str}
        """
        settings = _get_smtp_settings()

        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {
                "success": False,
                "error": f"SMTP not configured (missing: {', '.join(missing)}). "
                "Configure SMTP via the password-reset admin panel.",
            }

        app_url = settings.get("app_url", "").rstrip("/") or ""
        # Derive login and dashboards URLs from the host in app_url.
        # app_url is the password-reset app's own base (e.g. https://host/reset-password);
        # we extract just scheme+host so we don't break the reset-token links that app builds.
        if app_url:
            from urllib.parse import urlparse

            _parsed = urlparse(app_url)
            _host = f"{_parsed.scheme}://{_parsed.netloc}"
            login_url = _host + "/dashboards/login"
            dashboards_url = _host + "/dashboard"
            logo_url = _host + "/static/kavanah-analytics-logo.svg"
        else:
            login_url = ""
            dashboards_url = ""
            logo_url = ""
        greeting = f" {full_name}" if full_name else ""
        # (SVC-012): HTML-escaped variants for html_body; the plain
        # greeting/username stay raw for the text/plain part.
        greeting_html = f" {html.escape(full_name)}" if full_name else ""
        username_html = html.escape(username)

        subject = "Your Kavanah Analytics Account Has Been Created"

        login_button_html = (
            (
                f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0;"><tr>'
                f'<td style="border-radius:6px;background-color:#0972D3;">'
                f'<a href="{login_url}" style="display:inline-block;padding:14px 32px;color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;border-radius:6px;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">Log In Now</a>'
                f"</td></tr></table>"
            )
            if login_url
            else ""
        )

        dashboards_html = (
            (
                f'<p style="margin:8px 0 0;font-size:13px;color:#5F6B7A;">After logging in, your dashboards are at: '
                f'<a href="{dashboards_url}" style="color:#0972D3;">{dashboards_url}</a></p>'
            )
            if dashboards_url
            else ""
        )

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
        <h2 style="margin:0 0 16px;color:#000716;font-size:20px;font-weight:700;">Your account is ready</h2>
        <p style="margin:0 0 12px;">Hello{greeting_html},</p>
        <p style="margin:0 0 12px;">An account has been created for you on Kavanah Analytics. Here are your login credentials:</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FAFAFA;border:1px solid #D5DBDB;border-radius:6px;margin:16px 0;">
          <tr><td style="padding:14px 18px;font-family:'Courier New',Consolas,monospace;font-size:14px;color:#000716;">
            <p style="margin:4px 0;"><strong style="color:#000716;">Username:</strong> {username_html}</p>
            <p style="margin:4px 0;"><strong style="color:#000716;">Temporary Password:</strong> {temp_password}</p>
          </td></tr>
        </table>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FFF8E1;border-left:4px solid #F59E0B;border-radius:0 6px 6px 0;margin:20px 0;">
          <tr><td style="padding:12px 16px;font-size:14px;color:#6D4C00;">
            <strong>&#9888; Action required:</strong> You must set a new password before you can access the application. You will be prompted to do this immediately after your first login.
          </td></tr>
        </table>
        {login_button_html}
        {dashboards_html}
      </td></tr>
      <tr><td style="padding:16px 28px 24px;border-top:1px solid #e9ebed;background-color:#ffffff;color:#5F6B7A;font-size:12px;line-height:1.5;border-radius:0 0 8px 8px;">
        <p style="margin:0 0 4px;">If you did not expect this email, please contact your administrator.</p>
        <p style="margin:0;">This is an automated message — please do not reply.</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

        text_body = (
            f"Welcome to Kavanah Analytics\n\n"
            f"Hello{greeting},\n\n"
            f"An account has been created for you.\n\n"
            f"Username:           {username}\n"
            f"Temporary Password: {temp_password}\n\n"
            f"IMPORTANT: You must change your password on first login.\n"
            f"You will be prompted to set a new password before accessing the application.\n\n"
            + (f"Log in at: {login_url}\n\n" if login_url else "")
            + (f"Your dashboards: {dashboards_url}\n\n" if dashboards_url else "")
            + "If you did not expect this email, please contact your administrator."
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
                "UserProvisioningService: welcome email sent to %s for user '%s'",
                email,
                username,
            )
            return {"success": True}
        except Exception as e:
            logging.error(
                "UserProvisioningService: failed to send welcome email to %s: %s",
                email,
                e,
            )
            return {"success": False, "error": "Could not send the welcome email."}

    @staticmethod
    def credential_changed_stamp():
        """
        the current time as the ISO-8601 UTC string written to
        every writer's ``credential_changed_at`` metadata key, so the format
        lives in exactly one place. Parsed back by
        ``web.auth._parse_credential_changed_at``.

        Returns:
            str: e.g. "2026-09-25T14:03:11.482113+00:00"
        """
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _flush_credential_change_cache(username):
        """
        flush THIS process's account-state cache entry for
        ``username`` via UserManagementService._flush_account_state_cache,
        so the worker that made the write re-checks ES on its very next
        request instead of riding the 30s TTL. The other gunicorn worker
        (if any) still relies on the TTL — that's the ticket's stated bound.

        Fail-soft: never raises. A failure here only means this process's
        own cache entry goes stale for up to 30s longer; it never blocks
        the credential write that triggered it.
        """
        try:
            try:
                from web.services.user_management_service import UserManagementService
            except ImportError:  # pragma: no cover — alternate import root
                from asset_management.web.services.user_management_service import UserManagementService

            UserManagementService._flush_account_state_cache(username)
        except Exception as e:  # noqa: BLE001 — fail-soft, never block the credential write
            logging.warning("UserProvisioningService: failed to flush account-state cache for '%s': %s", username, e)

    @staticmethod
    def _revoke_kibana_gate_for_credential_change(username):
        """
        revoke ``username``'s live Kibana gate API key (if any)
        via UserManagementService._revoke_kibana_gate_key(trigger=
        "credential_changed"), reusing the same lazy-imported, fail-soft
        helper suspend path uses.

        Called only from reset_password and admin_set_password: those are
        the two writers where the acting admin is not the account owner, so
        the old key must not be trusted to survive the change. Self-service
        password change, onboarding and SSO provisioning do NOT call this —
        the actor is changing their own credential and their own key should
        keep working.

        Fail-soft: never raises (matches _revoke_kibana_gate_key itself).
        """
        try:
            try:
                from web.services.user_management_service import UserManagementService
            except ImportError:  # pragma: no cover — alternate import root
                from asset_management.web.services.user_management_service import UserManagementService

            UserManagementService._revoke_kibana_gate_key(username, trigger="credential_changed")
        except Exception as e:  # noqa: BLE001 — fail-soft, never block the credential write
            logging.warning("UserProvisioningService: Kibana gate revoke failed for '%s': %s", username, e)

    @staticmethod
    def reset_password(username):
        """
        Admin-triggered password reset.

        Generates a new 16-char temp password, replaces the user's ES password,
        and sets force_password_change=True so the existing first-login flow
        picks up on next login.

        also stamps metadata.credential_changed_at and revokes the
        user's Kibana gate key (trigger="credential_changed"), so any DEAM
        session that authenticated before this reset is denied — and the
        nginx-fronted Kibana gate stops trusting the old key — within the
        30-second account-state cache TTL (see web.auth.session_denial_reason /
        verify_auth). Both are fail-soft: a failure here never blocks the
        password reset itself, it only means the old session(s) survive
        slightly longer than the ticket's bound.

        Returns:
            dict: {success: bool, temp_password: str, email: str, full_name: str, error: str}
        """
        from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        try:
            response = es.security.get_user(username=username)
            user_data = dict(response).get(username)
            if not user_data:
                return {"success": False, "error": f"User '{username}' not found"}
            if user_data.get("metadata", {}).get("_reserved"):
                return {"success": False, "error": f"Cannot reset built-in user '{username}'"}
        except Exception as e:
            logging.error("UserProvisioningService: could not fetch user '%s' for reset: %s", username, e)
            return {"success": False, "error": "Could not fetch user."}

        temp_password = UserProvisioningService.generate_temp_password()

        try:
            get_es_admin_client(es).security.change_password(username=username, body={"password": temp_password})
        except Exception as e:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.error("UserProvisioningService: change_password failed for '%s': %s", username, e)
            return {"success": False, "error": "Could not reset the password."}

        metadata = dict(user_data.get("metadata", {}))
        metadata["force_password_change"] = True
        # stamp so web.auth denies any session that predates this reset.
        metadata["credential_changed_at"] = UserProvisioningService.credential_changed_stamp()
        try:
            # enabled is carried because ES writes enabled=true when it is
            # omitted: resetting a suspended user's password must not reinstate them.
            get_es_admin_client(es).security.put_user(
                username=username,
                roles=user_data.get("roles", []),
                full_name=user_data.get("full_name", ""),
                email=user_data.get("email", ""),
                enabled=user_data.get("enabled", True),
                metadata=metadata,
            )
        except Exception as e:
            # Password was changed but we failed to set the flag. Log loudly so
            # an admin knows the user will land in the app without the forced
            # change prompt (but with a known-good temp password).
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.error(
                "UserProvisioningService: password reset for '%s' succeeded but setting " "force_password_change failed: %s",
                username,
                e,
            )
            return {"success": False, "error": "Password changed but could not set force_password_change flag."}

        UserProvisioningService._flush_credential_change_cache(username)
        UserProvisioningService._revoke_kibana_gate_for_credential_change(username)

        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logging.info("AUDIT: action=reset_password username=%s", username)
        return {
            "success": True,
            "temp_password": temp_password,
            "email": user_data.get("email", "") or "",
            "full_name": user_data.get("full_name", "") or "",
        }

    @staticmethod
    def send_password_reset_email(email, username, temp_password, full_name=""):
        """
        Send a password-reset email containing the new temporary password.
        Mirrors send_welcome_email's formatting so users get a consistent look.

        Returns:
            dict: {success: bool, error: str}
        """
        settings = _get_smtp_settings()

        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {
                "success": False,
                "error": f"SMTP not configured (missing: {', '.join(missing)}). "
                "Configure SMTP via the password-reset admin panel.",
            }

        app_url = settings.get("app_url", "").rstrip("/") or ""
        if app_url:
            from urllib.parse import urlparse

            _parsed = urlparse(app_url)
            _host = f"{_parsed.scheme}://{_parsed.netloc}"
            login_url = _host + "/dashboards/login"
            dashboards_url = _host + "/dashboard"
            logo_url = _host + "/static/kavanah-analytics-logo.svg"
        else:
            login_url = ""
            dashboards_url = ""
            logo_url = ""
        greeting = f" {full_name}" if full_name else ""
        # (SVC-012): HTML-escaped variants for html_body; the plain
        # greeting/username stay raw for the text/plain part.
        greeting_html = f" {html.escape(full_name)}" if full_name else ""
        username_html = html.escape(username)

        subject = "Your Kavanah Analytics Password Has Been Reset"

        login_button_html = (
            (
                f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0;"><tr>'
                f'<td style="border-radius:6px;background-color:#0972D3;">'
                f'<a href="{login_url}" style="display:inline-block;padding:14px 32px;color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;border-radius:6px;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">Log In Now</a>'
                f"</td></tr></table>"
            )
            if login_url
            else ""
        )

        dashboards_html = (
            (
                f'<p style="margin:8px 0 0;font-size:13px;color:#5F6B7A;">After logging in, your dashboards are at: '
                f'<a href="{dashboards_url}" style="color:#0972D3;">{dashboards_url}</a></p>'
            )
            if dashboards_url
            else ""
        )

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
        <h2 style="margin:0 0 16px;color:#000716;font-size:20px;font-weight:700;">Your password has been reset</h2>
        <p style="margin:0 0 12px;">Hello{greeting_html},</p>
        <p style="margin:0 0 12px;">An administrator has reset your Kavanah Analytics password. Use the temporary password below to sign in:</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FAFAFA;border:1px solid #D5DBDB;border-radius:6px;margin:16px 0;">
          <tr><td style="padding:14px 18px;font-family:'Courier New',Consolas,monospace;font-size:14px;color:#000716;">
            <p style="margin:4px 0;"><strong style="color:#000716;">Username:</strong> {username_html}</p>
            <p style="margin:4px 0;"><strong style="color:#000716;">Temporary Password:</strong> {temp_password}</p>
          </td></tr>
        </table>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FFF8E1;border-left:4px solid #F59E0B;border-radius:0 6px 6px 0;margin:20px 0;">
          <tr><td style="padding:12px 16px;font-size:14px;color:#6D4C00;">
            <strong>&#9888; Action required:</strong> You will be prompted to set a new password immediately after signing in. If you did not request this reset, contact your administrator right away.
          </td></tr>
        </table>
        {login_button_html}
        {dashboards_html}
      </td></tr>
      <tr><td style="padding:16px 28px 24px;border-top:1px solid #e9ebed;background-color:#ffffff;color:#5F6B7A;font-size:12px;line-height:1.5;border-radius:0 0 8px 8px;">
        <p style="margin:0 0 4px;">If you did not expect this email, please contact your administrator.</p>
        <p style="margin:0;">This is an automated message — please do not reply.</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

        text_body = (
            f"Your Kavanah Analytics Password Has Been Reset\n\n"
            f"Hello{greeting},\n\n"
            f"An administrator has reset your password. Use the temporary password below to sign in:\n\n"
            f"Username:           {username}\n"
            f"Temporary Password: {temp_password}\n\n"
            f"IMPORTANT: You will be prompted to set a new password immediately after signing in.\n"
            f"If you did not request this reset, contact your administrator right away.\n\n"
            + (f"Log in at: {login_url}\n\n" if login_url else "")
            + (f"Your dashboards: {dashboards_url}\n\n" if dashboards_url else "")
            + "This is an automated message -- please do not reply."
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
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.info(
                "UserProvisioningService: password-reset email sent to %s for user '%s'",
                email,
                username,
            )
            return {"success": True}
        except Exception as e:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.error(
                "UserProvisioningService: failed to send password-reset email to %s: %s",
                email,
                e,
            )
            return {"success": False, "error": "Could not send the password-reset email."}

    @staticmethod
    def get_user_metadata(username):
        """
        Fetch the metadata dict for an Elasticsearch user.

        Returns:
            dict: metadata fields, or None if the user could not be fetched
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return None

        try:
            response = es.security.get_user(username=username)
            user_data = dict(response).get(username, {})
            return user_data.get("metadata", {})
        except Exception as e:
            logging.error(
                "UserProvisioningService: could not fetch metadata for '%s': %s",
                username,
                e,
            )
            return None

    @staticmethod
    def validate_admin_password_complexity(password):
        """Return None if password meets complexity rules, else error string.

        Rules: length >= 12, at least one uppercase, one lowercase, one digit,
        and one character from _ADMIN_PASSWORD_SPECIALS. Used by admin_set_password
        to gate manually-chosen credentials for service/demo accounts.
        """
        if not isinstance(password, str):
            return "Password must be a string"
        if len(password) < 12:
            return "Password must be at least 12 characters"
        if not any(c.isupper() for c in password):
            return "Password must contain at least one uppercase letter"
        if not any(c.islower() for c in password):
            return "Password must contain at least one lowercase letter"
        if not any(c.isdigit() for c in password):
            return "Password must contain at least one digit"
        if not any(c in _ADMIN_PASSWORD_SPECIALS for c in password):
            return f"Password must contain at least one special character ({_ADMIN_PASSWORD_SPECIALS})"
        return None

    @staticmethod
    def admin_set_password(username, new_password, set_by, reason):
        """Admin-only: set a user's password to an explicit value, no email sent.

        Designed for service/demo accounts where the admin needs a known
        credential and emailing a temporary password defeats the purpose. The
        caller is responsible for record-keeping outside this service.

        Does NOT toggle force_password_change; admins generally want the chosen
        password to stand. Call clear_force_password_change(username) separately
        if the account was previously flagged.

        also stamps metadata.credential_changed_at and revokes the
        user's Kibana gate key (trigger="credential_changed") so any DEAM
        session or Kibana key issued before this change stops working within
        the 30-second account-state cache TTL. Fail-soft: never blocks the
        password change itself.

        Args:
            username (str): Target user.
            new_password (str): Explicit new password (must pass complexity).
            set_by (str): Admin username, recorded in audit log.
            reason (str): Free-text justification, recorded in audit log.

        Returns:
            dict: {success: bool, error: str}
        """
        from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

        if not username or not username.strip():
            return {"success": False, "error": "Username is required"}
        if not set_by or not set_by.strip():
            return {"success": False, "error": "set_by is required"}
        if not reason or not reason.strip():
            return {"success": False, "error": "Audit reason is required"}

        complexity_error = UserProvisioningService.validate_admin_password_complexity(new_password)
        if complexity_error:
            return {"success": False, "error": complexity_error}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        try:
            response = es.security.get_user(username=username)
            user_data = dict(response).get(username)
            if not user_data:
                return {"success": False, "error": f"User '{username}' not found"}
            if user_data.get("metadata", {}).get("_reserved"):
                return {"success": False, "error": f"Cannot set password for built-in user '{username}'"}
        except Exception as e:
            logging.error(  # nosemgrep: python-logger-credential-disclosure
                "UserProvisioningService.admin_set_password: could not fetch user '%s': %s",
                username,
                e,
            )
            return {"success": False, "error": "Could not fetch user."}

        try:
            get_es_admin_client(es).security.change_password(username=username, body={"password": new_password})
        except Exception as e:
            logging.error(  # nosemgrep: python-logger-credential-disclosure
                "UserProvisioningService.admin_set_password: change_password failed for '%s': %s",
                username,
                e,
            )
            return {"success": False, "error": "Could not set the password."}

        # Atomically clear force_password_change so the chosen password stands
        # on next login (matches admin intent for service / demo accounts).
        # ES merges metadata on PUT — set False rather than pop the key.
        metadata = dict(user_data.get("metadata", {}))
        metadata["force_password_change"] = False
        # stamp so web.auth denies any session that predates this change.
        metadata["credential_changed_at"] = UserProvisioningService.credential_changed_stamp()
        try:
            # enabled carried for the same reason as reset_password: never reinstate.
            get_es_admin_client(es).security.put_user(
                username=username,
                roles=user_data.get("roles", []),
                full_name=user_data.get("full_name", ""),
                email=user_data.get("email", ""),
                enabled=user_data.get("enabled", True),
                metadata=metadata,
            )
        except Exception as e:
            # Password changed but force-flag still set. The user CAN log in
            # with the chosen password, but will be re-prompted to change it.
            # Surface the error so the admin knows to retry / fix manually.
            logging.error(  # nosemgrep: python-logger-credential-disclosure
                "UserProvisioningService.admin_set_password: change_password succeeded but "
                "clearing force_password_change failed for '%s': %s",
                username,
                e,
            )
            return {
                "success": False,
                "error": "Password changed but could not clear force_password_change flag.",
            }

        UserProvisioningService._flush_credential_change_cache(username)
        UserProvisioningService._revoke_kibana_gate_for_credential_change(username)

        logging.info(  # nosemgrep: python-logger-credential-disclosure
            "AUDIT: action=admin_set_password username=%s set_by=%s reason=%s",
            username,
            set_by,
            reason.strip(),
        )
        return {"success": True}

    @staticmethod
    def clear_force_password_change(username):
        """
        Clear the force_password_change flag from a user's ES metadata.
        All other metadata and user attributes are preserved.

        Returns:
            bool: True if successful, False otherwise
        """
        from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

        es = get_elasticsearch_client()
        if es is None:
            return False

        try:
            response = es.security.get_user(username=username)
            user_data = dict(response).get(username, {})
            metadata = dict(user_data.get("metadata", {}))
            # ES 9.x merges metadata on PUT — keys can't be removed, only overwritten.
            # Set to False instead of popping the key.
            metadata["force_password_change"] = False

            # PUT without a password field preserves the existing password, but
            # NOT enabled — ES writes enabled=true when it is omitted, so carry it.
            get_es_admin_client(es).security.put_user(
                username=username,
                roles=user_data.get("roles", []),
                full_name=user_data.get("full_name", ""),
                email=user_data.get("email", ""),
                enabled=user_data.get("enabled", True),
                metadata=metadata,
            )
            logging.info(  # nosemgrep: python-logger-credential-disclosure
                "AUDIT: action=clear_force_password_change username=%s", username
            )
            return True
        except Exception as e:
            logging.error(  # nosemgrep: python-logger-credential-disclosure
                "UserProvisioningService: failed to clear force_password_change for '%s': %s",
                username,
                e,
            )
            return False

    @staticmethod
    def stamp_credential_changed(username):
        """
        stamp metadata.credential_changed_at = now (UTC) on
        ``username``'s ES Security record, so ``web.auth.session_denial_reason``
        / ``verify_auth`` deny any DEAM session that authenticated before this
        moment, within the 30-second account-state cache TTL.

        For callers that already do their own get -> merge -> put after a
        password write (reset_password, admin_set_password, onboarding
        signup's own metadata put), fold ``credential_changed_stamp()``
        directly into that existing put instead of calling this — a second
        round trip is unnecessary. This helper is for writers that do NOT
        already have an open put: the forced-password-change route
        (app.change_password_post) and SSO provisioning
        (microsoft_sso_service / google_sso_service), called right after
        their own successful change_password, before any session is
        established or promoted.

        Does NOT revoke the Kibana gate key: that only belongs on the two
        writers where the actor is not the account owner (reset_password,
        admin_set_password) — self-service, onboarding and SSO provisioning
        are the user acting on their own account, so their own key should
        keep working.

        Fail-soft: never raises. A failure here means the affected session(s)
        survive slightly longer than the ticket's bound; it must never block
        the credential write that triggered it.

        Returns:
            bool: True on success, False otherwise (logged as a warning).
        """
        from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("UserProvisioningService.stamp_credential_changed: ES unavailable for '%s'", username)
            return False

        try:
            response = es.security.get_user(username=username)
            user_data = dict(response).get(username)
            if not user_data:
                logging.warning("UserProvisioningService.stamp_credential_changed: user '%s' not found", username)
                return False

            metadata = dict(user_data.get("metadata", {}))
            metadata["credential_changed_at"] = UserProvisioningService.credential_changed_stamp()

            get_es_admin_client(es).security.put_user(
                username=username,
                roles=user_data.get("roles", []),
                full_name=user_data.get("full_name", ""),
                email=user_data.get("email", ""),
                enabled=user_data.get("enabled", True),
                metadata=metadata,
            )
        except Exception as e:  # noqa: BLE001 — fail-soft, must not block the credential write
            logging.warning("UserProvisioningService.stamp_credential_changed: failed for '%s': %s", username, e)
            return False

        UserProvisioningService._flush_credential_change_cache(username)

        logging.info("AUDIT: action=credential_changed_stamp username=%s", username)
        return True
