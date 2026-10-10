"""Invite Token Service.

Encodes and decodes the ``?invite=<jwt>`` query parameter that lands an
invitee on ``/onboarding/start``. The token names the org / team the
invitee is being added to and the role they should receive on first
arrival at step 2 (consumes the stashed payload to skip the
``account_type`` and workspace-creation steps).

Token contract — same shape as ``OnboardingEmailVerificationService``
 so the verify path and codebase stay coherent:

* HS256 JWT, signed with the Flask ``SECRET_KEY``.
* ``purpose="onboarding_invite"`` claim — distinct from
  ``onboarding_email_verify`` so a verification link cannot unlock an
  invite slot and vice-versa.
* 7-day default expiry (TTL from the ticket scope).
* ``sub`` carries the invitee's email; ``org_esid`` / ``team_esid`` /
  ``role_esid`` / ``inviter`` describe the target. ``jti`` is a random
  identifier the wizard stamps onto ``wizard_session.state['invite']``
  alongside a ``consumed`` flag so a downstream ticket can
  enforce one-time-use without a new ES index.

Decode contract: returns ``{"valid": True, "payload": {...}}`` on
success or ``{"valid": False, "error": <str>}`` on failure. Failure
strings are user-facing (the route uses them in a flash) so they stay
generic — never echo the offending token back to the user.

Out of scope for these belong to sibling tickets:

* Issuing tokens (an invite-send endpoint / UI). only decodes.
* Step-2/3 skip + role honour — this service merely stashes
  the payload; the wizard variant ticket reads it back.

 added ``decode_expired_claims`` — a relaxed-validation helper
that surfaces the claims of an already-expired token (signature still
verified) so the landing-handler can notify the inviter + flip the
matching ``de_authz_invite`` doc to ``status="expired"``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt

# 7-day expiry — ticket default. Long enough that a teammate forwarded
# an invite from email at the weekend can still accept on Monday;
# short enough that a leaked URL self-rots before the next sprint.
INVITE_TOKEN_EXPIRY_SECONDS = 7 * 24 * 60 * 60

# Distinct purpose claim — see module docstring. A
# verification link must not satisfy ``decode_token`` here.
TOKEN_PURPOSE = "onboarding_invite"


class InviteTokenService:
    """HS256 JWT codec for the ``?invite=…`` query parameter."""

    @staticmethod
    def generate_token(
        *,
        invitee_email: str,
        org_esid: str | None,
        team_esid: str | None,
        role_esid: str,
        inviter: str,
        secret_key: str,
        expires_in_seconds: int = INVITE_TOKEN_EXPIRY_SECONDS,
    ) -> str:
        """Issue an invite JWT.

        At least one of ``org_esid`` / ``team_esid`` must be present —
        an invite that names neither has nowhere to add the user.
        Returns the encoded token; raises ``ValueError`` on a payload
        that the decode side would refuse.
        """
        if not invitee_email or "@" not in invitee_email:
            raise ValueError("invitee_email must be a valid email address")
        if not org_esid and not team_esid:
            raise ValueError("at least one of org_esid / team_esid is required")
        if not role_esid:
            raise ValueError("role_esid is required")
        if not inviter:
            raise ValueError("inviter is required")

        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": invitee_email,
            "purpose": TOKEN_PURPOSE,
            "org_esid": org_esid,
            "team_esid": team_esid,
            "role_esid": role_esid,
            "inviter": inviter,
            "iat": now,
            "exp": now + expires_in_seconds,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(payload, secret_key, algorithm="HS256")

    @staticmethod
    def decode_token(token: str, secret_key: str) -> dict:
        """Decode and validate an invite token.

        Returns ``{"valid": True, "payload": <claims>}`` on success or
        ``{"valid": False, "error": <reason>, "expired": <bool>}`` on
        failure. ``expired`` is broken out so the caller can route an
        expired token to the notify-inviter screen instead of
        the generic "invite is invalid" message.
        """
        if not token:
            return {"valid": False, "error": "Missing invite token.", "expired": False}

        try:
            claims = jwt.decode(token, secret_key, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return {"valid": False, "error": "Invite link has expired.", "expired": True}
        except jwt.InvalidTokenError:
            return {"valid": False, "error": "Invite link is invalid.", "expired": False}

        if claims.get("purpose") != TOKEN_PURPOSE:
            return {"valid": False, "error": "Invite link is invalid.", "expired": False}

        invitee_email = claims.get("sub")
        if not isinstance(invitee_email, str) or "@" not in invitee_email:
            return {"valid": False, "error": "Invite link is invalid.", "expired": False}

        org_esid = claims.get("org_esid")
        team_esid = claims.get("team_esid")
        if not org_esid and not team_esid:
            return {"valid": False, "error": "Invite link is invalid.", "expired": False}

        role_esid = claims.get("role_esid")
        if not isinstance(role_esid, str) or not role_esid:
            return {"valid": False, "error": "Invite link is invalid.", "expired": False}

        return {
            "valid": True,
            "payload": {
                "invitee_email": invitee_email,
                "org_esid": org_esid,
                "team_esid": team_esid,
                "role_esid": role_esid,
                "inviter": claims.get("inviter") or "",
                "jti": claims.get("jti") or "",
                "exp": claims.get("exp"),
            },
        }

    @staticmethod
    def decode_expired_claims(token: str, secret_key: str) -> dict | None:
        """Decode an EXPIRED invite JWT, returning the original claims.

         needs the invitee email + inviter + workspace from an
        already-expired token so the landing handler can (a) flip the
        matching ``de_authz_invite`` doc to ``status="expired"`` and
        (b) email the inviter a "Resend invite" CTA. ``decode_token``
        raises on ``exp`` so it can't surface those claims; this helper
        skips ONLY the ``exp`` check while keeping signature verification
        (otherwise a forged "expired" token would let any attacker spam
        an arbitrary inviter via the auto-notify path).

        Returns the same payload shape as ``decode_token`` on success,
        or ``None`` if the signature is bad / claims are malformed /
        purpose mismatches. Distinct from ``decode_token`` so callers
        opt-in to relaxed validation; never call this for a token that
        will be used to provision membership.
        """
        if not token:
            return None
        try:
            claims = jwt.decode(
                token,
                secret_key,
                algorithms=["HS256"],
                options={"verify_exp": False},
            )
        except jwt.InvalidTokenError:
            return None

        if claims.get("purpose") != TOKEN_PURPOSE:
            return None

        invitee_email = claims.get("sub")
        if not isinstance(invitee_email, str) or "@" not in invitee_email:
            return None

        org_esid = claims.get("org_esid")
        team_esid = claims.get("team_esid")
        if not org_esid and not team_esid:
            return None

        role_esid = claims.get("role_esid")
        if not isinstance(role_esid, str) or not role_esid:
            return None

        return {
            "invitee_email": invitee_email,
            "org_esid": org_esid,
            "team_esid": team_esid,
            "role_esid": role_esid,
            "inviter": claims.get("inviter") or "",
            "jti": claims.get("jti") or "",
            "exp": claims.get("exp"),
        }

    @staticmethod
    def build_session_invite(payload: dict) -> dict:
        """Wrap a decoded payload for storage on Flask ``session`` or
        ``wizard_session.state``.

        Adds the ``consumed=False`` flag will flip when the
        invitee first arrives at step 2. Kept as a dedicated helper so
        the field shape is defined in exactly one place (the route and
        the consume site both call this).
        """
        return {
            "invitee_email": payload.get("invitee_email", ""),
            "org_esid": payload.get("org_esid"),
            "team_esid": payload.get("team_esid"),
            "role_esid": payload.get("role_esid"),
            "inviter": payload.get("inviter", ""),
            "jti": payload.get("jti", ""),
            "exp": payload.get("exp"),
            "consumed": False,
            "consumed_at": None,
        }
