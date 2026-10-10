"""MFA Recovery Token Service.

HS256 JWT codec for the two email-based MFA-recovery tokens. Same shape and
conventions as ``InviteTokenService`` and
``OnboardingEmailVerificationService`` so the auth-flow token surface
stays coherent — signed with the Flask ``SECRET_KEY``, distinct ``purpose``
claims so one token kind can never satisfy the other's decode.

Two token kinds:

* ``mfa_recovery_email_verify`` — proves the user controls a recovery email
  address. Clicking it flips ``recovery_email_verified=True``. No MFA effect.
  Longer-lived (24h) like the onboarding verify link.

* ``mfa_recovery_login`` — the time-limited single-use link that, when clicked,
  disables MFA and logs the user in. 15-minute expiry (ticket scope). Carries a
  random ``jti`` that ``MFAService.stage_recovery_login_jti`` persists on the MFA
  record at send time; ``MFAService.consume_recovery_login`` matches + clears it
  so the JWT's statelessness doesn't break single-use.

Decode contract mirrors the siblings: ``{"valid": True, ...}`` on success or
``{"valid": False, "error": <user-facing str>}`` on failure — never echo the
token back.
"""

from __future__ import annotations

import time
import uuid

import jwt

# Recovery-address verification link — 24h, same as the onboarding verify token.
VERIFY_EMAIL_EXPIRY_SECONDS = 86400
VERIFY_EMAIL_PURPOSE = "mfa_recovery_email_verify"

# The recovery-login link — 15 minutes, single-use (ticket scope).
RECOVERY_LOGIN_EXPIRY_SECONDS = 15 * 60
RECOVERY_LOGIN_PURPOSE = "mfa_recovery_login"


class MfaRecoveryTokenService:
    """HS256 JWT codec for the two MFA email-recovery tokens."""

    # -- recovery-email verification ---------------------------------------

    @staticmethod
    def generate_verify_email_token(username: str, email: str, secret_key: str) -> str:
        """Issue a recovery-email verification token (binds username + address)."""
        if not username:
            raise ValueError("username is required")
        if not email or "@" not in email:
            raise ValueError("email must be a valid email address")
        now = int(time.time())
        payload = {
            "sub": username,
            "email": email.strip().lower(),
            "purpose": VERIFY_EMAIL_PURPOSE,
            "iat": now,
            "exp": now + VERIFY_EMAIL_EXPIRY_SECONDS,
        }
        return jwt.encode(payload, secret_key, algorithm="HS256")

    @staticmethod
    def verify_email_token(token: str, secret_key: str) -> dict:
        """Decode a recovery-email verification token.

        Returns ``{"valid": True, "username": str, "email": str}`` or
        ``{"valid": False, "error": str}``.
        """
        if not token:
            return {"valid": False, "error": "Verification link is invalid."}
        try:
            claims = jwt.decode(token, secret_key, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return {"valid": False, "error": "Verification link has expired."}
        except jwt.InvalidTokenError:
            return {"valid": False, "error": "Verification link is invalid."}

        if claims.get("purpose") != VERIFY_EMAIL_PURPOSE:
            return {"valid": False, "error": "Verification link is invalid."}
        username = claims.get("sub")
        email = claims.get("email")
        if not isinstance(username, str) or not username:
            return {"valid": False, "error": "Verification link is invalid."}
        if not isinstance(email, str) or "@" not in email:
            return {"valid": False, "error": "Verification link is invalid."}
        return {"valid": True, "username": username, "email": email}

    # -- recovery-login link -----------------------------------------------

    @staticmethod
    def generate_recovery_login_token(username: str, secret_key: str) -> dict:
        """Issue a 15-minute recovery-login token. Returns ``{"token", "jti"}`` —
        the caller persists the jti on the MFA record (single-use), then mails
        the token."""
        if not username:
            raise ValueError("username is required")
        now = int(time.time())
        jti = uuid.uuid4().hex
        payload = {
            "sub": username,
            "purpose": RECOVERY_LOGIN_PURPOSE,
            "jti": jti,
            "iat": now,
            "exp": now + RECOVERY_LOGIN_EXPIRY_SECONDS,
        }
        token = jwt.encode(payload, secret_key, algorithm="HS256")
        return {"token": token, "jti": jti}

    @staticmethod
    def verify_recovery_login_token(token: str, secret_key: str) -> dict:
        """Decode a recovery-login token.

        Returns ``{"valid": True, "username": str, "jti": str}`` or
        ``{"valid": False, "error": str}``. The caller still has to match the
        ``jti`` against the stored one for single-use enforcement.
        """
        if not token:
            return {"valid": False, "error": "Recovery link is invalid."}
        try:
            claims = jwt.decode(token, secret_key, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return {"valid": False, "error": "Recovery link has expired."}
        except jwt.InvalidTokenError:
            return {"valid": False, "error": "Recovery link is invalid."}

        if claims.get("purpose") != RECOVERY_LOGIN_PURPOSE:
            return {"valid": False, "error": "Recovery link is invalid."}
        username = claims.get("sub")
        jti = claims.get("jti")
        if not isinstance(username, str) or not username:
            return {"valid": False, "error": "Recovery link is invalid."}
        if not isinstance(jti, str) or not jti:
            return {"valid": False, "error": "Recovery link is invalid."}
        return {"valid": True, "username": username, "jti": jti}
