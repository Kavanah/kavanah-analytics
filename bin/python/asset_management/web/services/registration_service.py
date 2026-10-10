"""
Registration Service
Handles self-service user registration, email verification,
admin approval/rejection, and pending registration management.
"""

import html
import json
import logging
import os
import re
import smtplib

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import jwt

INDEX = "de_pending_registrations"

INDEX_MAPPING = {
    "properties": {
        "@timestamp": {"type": "date"},
        "username": {"type": "keyword"},
        "full_name": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword"}},
        },
        "email": {"type": "keyword"},
        "encrypted_password": {
            "type": "keyword",
            "index": False,
            "doc_values": False,
        },
        "status": {"type": "keyword"},
        "ip_address": {"type": "ip"},
        "user_agent": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
        },
        "email_verified_at": {"type": "date"},
        "decided_at": {"type": "date"},
        "decided_by": {"type": "keyword"},
        "rejection_reason": {"type": "text"},
    }
}

# Statuses
STATUS_PENDING_VERIFICATION = "pending_verification"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# Shared SMTP config path (same as UserProvisioningService)
try:
    from web.services.smtp_settings import load_smtp_settings
except ImportError:
    from ..services.smtp_settings import load_smtp_settings

_PASSWORD_RESET_SETTINGS = "/app/python/password_reset/config/settings.json"

# In-memory rate limiting: IP -> [timestamp, ...]
_registration_attempts = defaultdict(list)

# Email validation regex (simple but effective)
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

# Username validation: letters, digits, underscores, hyphens, dots; 3-30 chars
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]{3,30}$")

# follow-up — server-side length ceilings mirroring the register
# template's maxlength attributes (a crafted POST bypasses the client).
_EMAIL_MAX_LENGTH = 254  # RFC-5321 total-address ceiling
_FULL_NAME_MAX_LENGTH = 100

VERIFICATION_TOKEN_EXPIRY_SECONDS = 86400  # 24 hours


def _get_smtp_settings():
    """Env-aware shared SMTP settings: PW_RESET_SMTP_* env
    credentials win over settings.json, which no longer stores them."""
    return load_smtp_settings("RegistrationService")


def _get_app_base_url():
    """Derive the application base URL from SMTP settings."""
    settings = _get_smtp_settings()
    app_url = settings.get("app_url", "").rstrip("/")
    if app_url:
        from urllib.parse import urlparse

        parsed = urlparse(app_url)
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


class RegistrationService:

    # ------------------------------------------------------------------
    # Index bootstrap
    # ------------------------------------------------------------------

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the pending registrations index if it does not exist."""
        from lib.es_helpers import (
            get_elasticsearch_client,
            put_regular_index_template,
            create_index_if_not_exists,
        )

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("RegistrationService: could not connect to ES")
            return

        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)

        try:
            es.indices.put_mapping(index=INDEX, body=INDEX_MAPPING)
        except Exception as e:
            logging.warning("RegistrationService: put_mapping update: %s", e)

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    @staticmethod
    def check_rate_limit(ip_address: str) -> bool:
        """Return True if the IP is within rate limits (5 attempts/hour)."""
        now = time.time()
        _registration_attempts[ip_address] = [t for t in _registration_attempts[ip_address] if now - t < 3600]
        return len(_registration_attempts[ip_address]) < 5

    @staticmethod
    def record_attempt(ip_address: str) -> None:
        """Record a registration attempt for rate limiting."""
        _registration_attempts[ip_address].append(time.time())

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_password(password: str) -> dict:
        """
        Validate password strength.
        Returns: {valid: bool, errors: list[str]}
        """
        errors = []
        if len(password) < 12:
            errors.append("Password must be at least 12 characters")
        if not re.search(r"[A-Z]", password):
            errors.append("Password must contain at least one uppercase letter")
        if not re.search(r"[a-z]", password):
            errors.append("Password must contain at least one lowercase letter")
        if not re.search(r"[0-9]", password):
            errors.append("Password must contain at least one number")
        if not re.search(r"[^A-Za-z0-9]", password):
            errors.append("Password must contain at least one special character")
        return {"valid": len(errors) == 0, "errors": errors}

    @staticmethod
    def validate_registration(username, email, password, confirm_password, full_name="") -> dict:
        """
        Validate all registration fields.
        Returns: {valid: bool, errors: list[str]}
        """
        errors = []

        # Username
        if not username:
            errors.append("Username is required")
        elif not _USERNAME_RE.match(username):
            errors.append(
                "Username must be 3-30 characters and contain only " "letters, digits, underscores, hyphens, or dots"
            )

        # Email
        if not email:
            errors.append("Email is required")
        elif not _EMAIL_RE.match(email):
            errors.append("Invalid email address")
        # follow-up — the format regex has no length bound; cap
        # separately (RFC-5321 ceiling; mirrors the template's maxlength).
        elif len(email) > _EMAIL_MAX_LENGTH:
            errors.append(f"Email address must be {_EMAIL_MAX_LENGTH} characters or fewer")

        # follow-up — full name is optional but bounded (mirrors
        # the template's maxlength="100").
        if full_name and len(full_name) > _FULL_NAME_MAX_LENGTH:
            errors.append(f"Full name must be {_FULL_NAME_MAX_LENGTH} characters or fewer")

        # Password
        if not password:
            errors.append("Password is required")
        else:
            pw_result = RegistrationService.validate_password(password)
            errors.extend(pw_result["errors"])

        # Confirm password
        if password and confirm_password != password:
            errors.append("Passwords do not match")

        # Uniqueness checks (only if basic validation passed)
        if not errors:
            uniqueness_errors = RegistrationService._check_username_uniqueness(username)
            errors.extend(uniqueness_errors)

            email_errors = RegistrationService._check_email_uniqueness(email)
            errors.extend(email_errors)

        return {"valid": len(errors) == 0, "errors": errors}

    @staticmethod
    def _check_username_uniqueness(username: str) -> list:
        """Check username is not taken in ES security or pending registrations."""
        from lib.es_helpers import get_elasticsearch_client

        errors = []
        es = get_elasticsearch_client()
        if es is None:
            errors.append("Unable to verify username availability")
            return errors

        # Check ES security users
        try:
            response = es.security.get_user(username=username)
            if dict(response).get(username):
                errors.append("Username is already taken")
                return errors
        except Exception:
            pass  # 404 means user doesn't exist — that's fine

        # Check pending registrations (not rejected)
        try:
            result = es.search(
                index=INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [{"term": {"username": username}}],
                            "must_not": [{"term": {"status": STATUS_REJECTED}}],
                        }
                    },
                    "size": 1,
                },
            )
            if result["hits"]["total"]["value"] > 0:
                errors.append("Username is already taken")
        except Exception:
            pass  # Index may not exist yet

        return errors

    @staticmethod
    def _check_email_uniqueness(email: str) -> list:
        """Check email is not already used in pending registrations."""
        from lib.es_helpers import get_elasticsearch_client

        errors = []
        es = get_elasticsearch_client()
        if es is None:
            return errors

        # Check pending registrations (not rejected)
        try:
            result = es.search(
                index=INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [{"term": {"email": email}}],
                            "must_not": [{"term": {"status": STATUS_REJECTED}}],
                        }
                    },
                    "size": 1,
                },
            )
            if result["hits"]["total"]["value"] > 0:
                errors.append("An account with this email is already pending")
        except Exception:
            pass

        return errors

    # ------------------------------------------------------------------
    # Password encryption (Fernet — needed because ES Security API
    # requires the plaintext password at user creation time)
    # ------------------------------------------------------------------

    @staticmethod
    def _encrypt_password(password: str) -> str:
        """Encrypt password using the project's Fernet key."""
        from lib.encryption import encrypt

        return encrypt(password)

    @staticmethod
    def _decrypt_password(encrypted: str) -> str:
        """Decrypt password for ES user creation at approval time."""
        from lib.encryption import decrypt

        return decrypt(encrypted)

    # ------------------------------------------------------------------
    # Registration CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def create_pending_registration(
        username: str,
        full_name: str,
        email: str,
        password: str,
        ip_address: str = "",
        user_agent: str = "",
    ) -> dict:
        """
        Store a new pending registration in Elasticsearch.
        Returns: {success: bool, registration_id: str, error: str}
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        encrypted_password = RegistrationService._encrypt_password(password)

        doc = {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "username": username,
            "full_name": full_name,
            "email": email,
            "encrypted_password": encrypted_password,
            "status": STATUS_PENDING_VERIFICATION,
            "ip_address": ip_address or None,
            "user_agent": user_agent,
        }

        try:
            result = es.index(index=INDEX, body=doc, refresh="wait_for")
            registration_id = result["_id"]
            logging.info(
                "AUDIT: action=registration_submitted username=%s email=%s ip=%s",
                username,
                email,
                ip_address,
            )
            return {"success": True, "registration_id": registration_id}
        except Exception as e:
            logging.error("RegistrationService: failed to create registration: %s", e)
            return {"success": False, "error": "Could not create the registration."}

    @staticmethod
    def get_registration_by_id(registration_id: str) -> dict | None:
        """Fetch a single registration by its ES document ID."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return None

        try:
            result = es.get(index=INDEX, id=registration_id)
            doc = result["_source"]
            doc["_id"] = result["_id"]
            return doc
        except Exception:
            return None

    @staticmethod
    def get_registrations(status: str = None, size: int = 100) -> list:
        """
        List registrations, optionally filtered by status.
        Returns newest first.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return []

        query = {"match_all": {}}
        if status:
            query = {"term": {"status": status}}

        try:
            result = es.search(
                index=INDEX,
                body={
                    "query": query,
                    "sort": [{"@timestamp": {"order": "desc"}}],
                    "size": size,
                    "_source": {"excludes": ["encrypted_password"]},
                },
            )
            registrations = []
            for hit in result["hits"]["hits"]:
                doc = hit["_source"]
                doc["_id"] = hit["_id"]
                registrations.append(doc)
            return registrations
        except Exception as e:
            logging.error("RegistrationService: failed to list registrations: %s", e)
            return []

    # ------------------------------------------------------------------
    # Email verification tokens (JWT)
    # ------------------------------------------------------------------

    @staticmethod
    def generate_verification_token(registration_id: str, secret_key: str) -> str:
        """
        Generate a JWT for email verification.
        Contains registration_id, purpose, and 24-hour expiry.
        """
        payload = {
            "sub": registration_id,
            "purpose": "email_verify",
            "exp": int(time.time()) + VERIFICATION_TOKEN_EXPIRY_SECONDS,
            "iat": int(time.time()),
        }
        return jwt.encode(payload, secret_key, algorithm="HS256")

    @staticmethod
    def verify_token(token: str, secret_key: str) -> dict:
        """
        Decode and validate an email verification JWT.
        Returns: {valid: bool, registration_id: str, error: str}
        """
        try:
            payload = jwt.decode(token, secret_key, algorithms=["HS256"])
            if payload.get("purpose") != "email_verify":
                return {"valid": False, "error": "Invalid token purpose"}
            return {"valid": True, "registration_id": payload["sub"]}
        except jwt.ExpiredSignatureError:
            return {"valid": False, "error": "Verification link has expired"}
        except jwt.InvalidTokenError:
            return {"valid": False, "error": "Invalid verification link"}

    # ------------------------------------------------------------------
    # Email verification
    # ------------------------------------------------------------------

    @staticmethod
    def confirm_email_verification(registration_id: str) -> dict:
        """
        Update registration status from pending_verification to pending_approval.
        Returns: {success: bool, error: str}
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        reg = RegistrationService.get_registration_by_id(registration_id)
        if reg is None:
            return {"success": False, "error": "Registration not found"}
        if reg["status"] != STATUS_PENDING_VERIFICATION:
            if reg["status"] == STATUS_PENDING_APPROVAL:
                return {"success": True, "error": "Email already verified"}
            return {"success": False, "error": f"Registration status is '{reg['status']}'"}

        try:
            es.update(
                index=INDEX,
                id=registration_id,
                body={
                    "doc": {
                        "status": STATUS_PENDING_APPROVAL,
                        "email_verified_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
                refresh="wait_for",
            )
            logging.info(
                "AUDIT: action=email_verified username=%s registration_id=%s",
                reg["username"],
                registration_id,
            )
            return {"success": True}
        except Exception as e:
            logging.error("RegistrationService: email verification failed: %s", e)
            return {"success": False, "error": "Could not verify the email address."}

    # ------------------------------------------------------------------
    # Admin approval / rejection
    # ------------------------------------------------------------------

    @staticmethod
    def approve_registration(registration_id: str, admin_username: str, roles: list) -> dict:
        """
        Approve a registration: create the ES security user and update status.
        Returns: {success: bool, error: str}
        """
        from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        reg = RegistrationService.get_registration_by_id(registration_id)
        if reg is None:
            return {"success": False, "error": "Registration not found"}
        if reg["status"] != STATUS_PENDING_APPROVAL:
            return {
                "success": False,
                "error": f"Cannot approve registration with status '{reg['status']}'",
            }

        # Decrypt the stored password
        try:
            password = RegistrationService._decrypt_password(reg["encrypted_password"])
        except Exception as e:
            logging.error(  # nosemgrep: python-logger-credential-disclosure
                "RegistrationService: password decryption failed: %s", e
            )
            return {"success": False, "error": "Failed to decrypt stored password"}

        # Create the ES security user
        try:
            get_es_admin_client(es).security.put_user(
                username=reg["username"],
                body={
                    "password": password,
                    "roles": roles,
                    "full_name": reg.get("full_name", ""),
                    "email": reg["email"],
                    "metadata": {"force_password_change": False},
                },
            )
        except Exception as e:
            logging.error(
                "RegistrationService: failed to create ES user '%s': %s",
                reg["username"],
                e,
            )
            return {"success": False, "error": "Failed to create user."}

        # Update registration status
        try:
            es.update(
                index=INDEX,
                id=registration_id,
                body={
                    "doc": {
                        "status": STATUS_APPROVED,
                        "decided_at": datetime.now(timezone.utc).isoformat(),
                        "decided_by": admin_username,
                        "encrypted_password": "",  # Clear stored password
                    }
                },
                refresh="wait_for",
            )
        except Exception as e:
            logging.warning("RegistrationService: user created but status update failed: %s", e)

        logging.info(
            "AUDIT: action=registration_approved username=%s approved_by=%s roles=%s",
            reg["username"],
            admin_username,
            roles,
        )

        # Send approval notification email
        RegistrationService._send_approval_email(reg["email"], reg["username"], reg.get("full_name", ""))

        return {"success": True}

    @staticmethod
    def reject_registration(registration_id: str, admin_username: str, reason: str = "") -> dict:
        """
        Reject a registration and update status.
        Returns: {success: bool, error: str}
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Elasticsearch unavailable"}

        reg = RegistrationService.get_registration_by_id(registration_id)
        if reg is None:
            return {"success": False, "error": "Registration not found"}
        if reg["status"] not in (STATUS_PENDING_VERIFICATION, STATUS_PENDING_APPROVAL):
            return {
                "success": False,
                "error": f"Cannot reject registration with status '{reg['status']}'",
            }

        try:
            es.update(
                index=INDEX,
                id=registration_id,
                body={
                    "doc": {
                        "status": STATUS_REJECTED,
                        "decided_at": datetime.now(timezone.utc).isoformat(),
                        "decided_by": admin_username,
                        "rejection_reason": reason,
                        "encrypted_password": "",  # Clear stored password
                    }
                },
                refresh="wait_for",
            )
            logging.info(
                "AUDIT: action=registration_rejected username=%s rejected_by=%s reason=%s",
                reg["username"],
                admin_username,
                reason,
            )
            return {"success": True}
        except Exception as e:
            logging.error("RegistrationService: rejection failed: %s", e)
            return {"success": False, "error": "Failed to reject registration."}

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def cleanup_expired(hours: int = 48) -> int:
        """
        Delete pending_verification registrations older than the given hours.
        Returns the number of deleted documents, or -1 on error.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return -1

        try:
            # proceed past docs the other gunicorn worker already deleted at startup (avoid 409).
            result = es.delete_by_query(
                index=INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"status": STATUS_PENDING_VERIFICATION}},
                                {"range": {"@timestamp": {"lt": f"now-{hours}h"}}},
                            ]
                        }
                    }
                },
                conflicts="proceed",
            )
            deleted = result.get("deleted", 0)
            if deleted:
                logging.info("RegistrationService: cleaned up %d expired registrations", deleted)
            return deleted
        except Exception as e:
            logging.error("RegistrationService: cleanup failed: %s", e)
            return -1

    # ------------------------------------------------------------------
    # Email helpers
    # ------------------------------------------------------------------

    @staticmethod
    def send_verification_email(email: str, username: str, full_name: str, verification_url: str) -> dict:
        """
        Send email verification link to the registering user.
        Returns: {success: bool, error: str}
        """
        settings = _get_smtp_settings()
        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {
                "success": False,
                "error": f"SMTP not configured (missing: {', '.join(missing)})",
            }

        greeting = f" {full_name}" if full_name else ""
        # (SVC-007): HTML-escaped variants for html_body; the plain
        # greeting/username stay raw for the text/plain part.
        greeting_html = f" {html.escape(full_name)}" if full_name else ""
        username_html = html.escape(username)
        subject = "Verify Your Kavanah Analytics Account"
        logo_url = f"{_get_app_base_url()}/static/kavanah-analytics-logo.svg"

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
        <p style="margin:0 0 12px;">Thank you for registering for a Kavanah Analytics account. Please verify your email address by clicking the button below:</p>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0;"><tr>
          <td style="border-radius:6px;background-color:#0972D3;">
            <a href="{verification_url}" style="display:inline-block;padding:14px 32px;color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;border-radius:6px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Verify Email Address</a>
          </td>
        </tr></table>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FAFAFA;border:1px solid #D5DBDB;border-radius:6px;margin:20px 0;">
          <tr><td style="padding:14px 18px;font-size:14px;color:#000716;">
            <p style="margin:0 0 6px;"><strong>Username:</strong> {username_html}</p>
            <p style="margin:0 0 6px;">This link will expire in 24 hours.</p>
            <p style="margin:0;">After verification, an administrator will review your registration before your account is activated.</p>
          </td></tr>
        </table>
        <p style="margin:16px 0 4px;font-size:13px;color:#5F6B7A;">If the button doesn't work, copy and paste this URL into your browser:</p>
        <p style="margin:0 0 16px;"><a href="{verification_url}" style="color:#0972D3;word-break:break-all;">{verification_url}</a></p>
        <p style="margin:0;font-size:13px;color:#5F6B7A;">If you did not create this account, you can safely ignore this email.</p>
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
            f"Verify Your Kavanah Analytics Account\n\n"
            f"Hello{greeting},\n\n"
            f"Thank you for registering. Please verify your email address by visiting:\n\n"
            f"{verification_url}\n\n"
            f"Username: {username}\n"
            f"This link will expire in 24 hours.\n\n"
            f"After verification, an administrator will review your registration "
            f"before your account is activated.\n\n"
            f"If you did not create this account, you can safely ignore this email."
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
                "RegistrationService: verification email sent to %s for user '%s'",
                email,
                username,
            )
            return {"success": True}
        except Exception as e:
            logging.error(
                "RegistrationService: failed to send verification email to %s: %s",
                email,
                e,
            )
            return {"success": False, "error": "Could not send the verification email."}

    @staticmethod
    def _send_approval_email(email: str, username: str, full_name: str) -> dict:
        """Send notification that the registration has been approved."""
        settings = _get_smtp_settings()
        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {"success": False, "error": "SMTP not configured"}

        base_url = _get_app_base_url()
        login_url = f"{base_url}/dashboards" if base_url else ""
        logo_url = f"{base_url}/static/kavanah-analytics-logo.svg" if base_url else ""
        greeting = f" {full_name}" if full_name else ""
        # (SVC-007): HTML-escaped variants for html_body; the plain
        # greeting/username stay raw for the text/plain part.
        greeting_html = f" {html.escape(full_name)}" if full_name else ""
        username_html = html.escape(username)
        subject = "Your Kavanah Analytics Account Has Been Approved"

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
        <h2 style="margin:0 0 16px;color:#000716;font-size:20px;font-weight:700;">Your account has been approved</h2>
        <p style="margin:0 0 12px;">Hello{greeting_html},</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F2F8F0;border:1px solid #037F0C;border-radius:6px;margin:16px 0;">
          <tr><td style="padding:14px 18px;font-size:14px;color:#037F0C;">
            Your Kavanah Analytics account has been approved. You can now log in using the username and password you created during registration.
          </td></tr>
        </table>
        <p style="margin:0 0 4px;"><strong>Username:</strong> {username_html}</p>
        {login_button_html}
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
            f"Your Kavanah Analytics Account Has Been Approved\n\n"
            f"Hello{greeting},\n\n"
            f"Your account has been approved! You can now log in.\n\n"
            f"Username: {username}\n"
            + (f"Log in at: {login_url}\n\n" if login_url else "\n")
            + "This is an automated message — please do not reply."
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
                "RegistrationService: approval email sent to %s for user '%s'",
                email,
                username,
            )
            return {"success": True}
        except Exception as e:
            logging.error(
                "RegistrationService: failed to send approval email to %s: %s",
                email,
                e,
            )
            return {"success": False, "error": "Could not send the approval email."}
