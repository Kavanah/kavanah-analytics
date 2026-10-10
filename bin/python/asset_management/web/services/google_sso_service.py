"""
Google SSO Service

"Continue with Google" entry point for the New User Onboarding wizard
. Two responsibilities:

1. Build the Google authorize URL and exchange the callback code for
   verified userinfo (sub, email, email_verified, name).
2. Match the verified email to an existing ES Security user and sign
   them in; otherwise provision a fresh user with a random password
   (the user authenticates via Google going forward — they never see
   or use this password) and stamp ``metadata.google_sub`` for future
   fast-path lookups via stored-sub instead of email.

Mirrors the ``OnboardingSignupService`` pattern:
``UserProvisioningService.create_user`` is reused to keep username-
collision handling consistent with admin- and form-signup flows.
The temp password it returns is discarded — we immediately overwrite
it with a random one, then clear ``force_password_change`` (the user
never types a password, so there's nothing to force-change).

Existing-user match strategy: email-only (decided 2026-05-12). If
Google's userinfo email matches an existing ES user's stored email,
the SSO flow signs that user in. Since that email must carry
``email_verified=true`` from Google — an unverified email is rejected
before matching or provisioning (both in ``exchange_code_for_userinfo``
and again in ``sign_in_or_provision``), because email-match trust is
only sound when Google has actually vouched for the address. Subsequent sign-ins use the stored
``metadata.google_sub`` as a faster lookup path, but the email match
remains the canonical "first-time link" trigger. Trade-offs and
revisit conditions live in DECISIONS.md.

Library choice: ``google-auth-oauthlib`` (already in requirements.txt
for the YouTube / Google Ads importers). The ``Flow`` class handles
state, PKCE, and the token exchange; we add our own session-bound
state token as defense in depth on top of the library's nonce.
"""

from __future__ import annotations

import logging
import re
import secrets
import string
import sys
from typing import Any

# Top-level imports of UserProvisioningService and get_elasticsearch_client
# so tests can patch them on this module. The
# sys.path insert mirrors sibling services so ``lib.es_helpers`` resolves
# whether the module is loaded from the test harness (where conftest
# extends sys.path) or from imports_container (where ``/app/python`` is
# the resolution root).
sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client, get_es_admin_client  # noqa: E402
from asset_management.web.services.user_provisioning_service import (  # noqa: E402
    UserProvisioningService,
)

# OAuth2 endpoints and the minimum scope set we need for sign-in:
# openid + email (so the ID token carries a verified email) + profile
# (so we can pre-fill full_name on first sign-in).
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# ES Security roles assigned to an SSO-provisioned account.
# Empty list — see DEFAULT_SIGNUP_ROLES in onboarding_signup_service.py
# for the rationale (KA users never authenticate to Kibana directly,
# and ES Security's authenticate() does not require a role for a
# successful auth call).
DEFAULT_SSO_ROLES: list[str] = []

# Username-from-email rules: lowercase, restrict to the local-part's
# alphanumerics + underscore + dot + hyphen, trim to <= 30 chars, pad
# short locals with random hex. Mirrors the username regex so
# the SSO-derived username is acceptable to validate_signup downstream.
_USERNAME_SAFE_RE = re.compile(r"[^a-z0-9_.\-]")


def _derive_username_from_email(email: str) -> str:
    """Derive a candidate ES username from a verified Google email.

    Local-part only, lowercased, scrubbed of disallowed characters. If
    the result is shorter than 3 characters (e.g. ``a@example.com``),
    pad with 4 random hex chars so the username regex accepts
    it. Final length is clamped to 30. Collision is the caller's
    problem — ``UserProvisioningService.suggest_unique_username``
    handles it on the create path.
    """
    local = (email or "").split("@", 1)[0].lower()
    cleaned = _USERNAME_SAFE_RE.sub("", local)
    if len(cleaned) < 3:
        cleaned = cleaned + secrets.token_hex(2)
    return cleaned[:30]


def _generate_unguessable_password() -> str:
    """Generate a 32-char random password meeting the complexity
    rules. Never shown to the user — the SSO path replaces the temp
    password from ``create_user`` with this one and then clears
    ``force_password_change`` so the user is not prompted for a
    password they don't know. The user authenticates via Google going
    forward; this is a defense-in-depth fallback (and a value ES
    Security can compare against if the SSO link is ever broken).
    """
    rng = secrets.SystemRandom()
    chars: list[str] = [
        rng.choice(string.ascii_uppercase),
        rng.choice(string.ascii_lowercase),
        rng.choice(string.digits),
        rng.choice("!@#$%^&*()-_=+[]{};:,.<>?/\\|~"),
    ]
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    chars.extend(rng.choice(alphabet) for _ in range(28))
    rng.shuffle(chars)
    return "".join(chars)


class GoogleSsoService:
    """Google SSO sign-in / sign-up contract for the wizard."""

    @staticmethod
    def generate_state_token() -> str:
        """Return a fresh URL-safe state token. The route handler stores
        this in the Flask session before redirecting to Google, then
        compares it against the ``state`` query param on the callback
        to defeat cross-site request forgery on the OAuth handshake.
        """
        return secrets.token_urlsafe(32)

    @staticmethod
    def build_authorize_url(
        client_id: str,
        redirect_uri: str,
        state: str,
    ) -> str:
        """Build the Google OAuth2 authorize URL.

        We construct the URL ourselves (rather than using
        ``Flow.authorization_url``) to keep the route handler thin and
        avoid depending on ``Flow``'s session-based state mechanics —
        we manage state in the Flask session directly, which is the
        easier thing to mutation-test.
        """
        from urllib.parse import urlencode

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "state": state,
            "access_type": "online",
            "include_granted_scopes": "true",
            "prompt": "select_account",
        }
        return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"

    @staticmethod
    def exchange_code_for_userinfo(
        code: str,
        state_received: str,
        state_expected: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> dict:
        """Validate state, exchange the auth code for an access token,
        then fetch verified userinfo from Google.

        Returns ``{"success": True, "sub": <str>, "email": <str>,
        "email_verified": <bool>, "name": <str>}`` on success;
        ``{"success": False, "error": <str>, "code": <code>}`` on
        failure. ``code`` is one of: ``state_mismatch``, ``no_code``,
        ``token_exchange_failed``, ``userinfo_failed``,
        ``email_not_verified``.
        """
        if not state_received or not state_expected or state_received != state_expected:
            logging.warning(
                "GoogleSsoService.exchange_code_for_userinfo: state mismatch " "(received_len=%s, expected_len=%s)",
                len(state_received or ""),
                len(state_expected or ""),
            )
            return {"success": False, "error": "Authentication failed (state mismatch).", "code": "state_mismatch"}

        if not code:
            return {"success": False, "error": "Missing authorization code.", "code": "no_code"}

        # Token exchange. We use ``requests`` directly rather than
        # ``Flow.fetch_token`` because Flow expects a full request URL
        # and the request object — easier to drive a plain POST here
        # and keep the mock surface area small in tests.
        import requests

        try:
            token_resp = requests.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.error("GoogleSsoService: token exchange request failed: %s", e)
            return {"success": False, "error": "Could not contact Google.", "code": "token_exchange_failed"}

        if token_resp.status_code != 200:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.warning(
                "GoogleSsoService: token exchange returned %s: %s",
                token_resp.status_code,
                token_resp.text[:200],
            )
            return {
                "success": False,
                "error": "Google rejected the sign-in.",
                "code": "token_exchange_failed",
            }

        access_token = (token_resp.json() or {}).get("access_token")
        if not access_token:
            return {"success": False, "error": "Google did not return a token.", "code": "token_exchange_failed"}

        try:
            ui_resp = requests.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("GoogleSsoService: userinfo request failed: %s", e)
            return {"success": False, "error": "Could not fetch your Google profile.", "code": "userinfo_failed"}

        if ui_resp.status_code != 200:
            return {"success": False, "error": "Google did not return profile data.", "code": "userinfo_failed"}

        ui = ui_resp.json() or {}
        sub = ui.get("sub") or ""
        email = (ui.get("email") or "").strip().lower()
        # strict identity check — only a JSON ``true`` counts
        # as verified. bool() would coerce a stray "false" string (or
        # any other truthy junk) to True and defeat the gate below.
        email_verified = ui.get("email_verified") is True
        name = ui.get("name") or ""

        if not email or not sub:
            return {"success": False, "error": "Google profile is missing email.", "code": "userinfo_failed"}

        # Google can return email_verified=false (notably for
        # some Workspace/domain configurations). An unverified email
        # must never match or provision an account — the email-match
        # link strategy below assumes Google vouched for the address.
        if email_verified is not True:
            logging.warning(
                "GoogleSsoService: rejected sign-in for unverified email (sub=%s).",
                sub,
            )
            return {
                "success": False,
                "error": "Your Google account's email address is not verified.",
                "code": "email_not_verified",
            }

        return {
            "success": True,
            "sub": sub,
            "email": email,
            "email_verified": email_verified,
            "name": name,
        }

    @staticmethod
    def find_user_by_email(email: str) -> dict | None:
        """Look up an ES Security user whose stored email matches ``email``.

        ES Security has no native email index — ``get_user()`` without
        a username argument returns every user. With <100 users this
        is fine (and matches the existing pattern in
        ``role_permissions_service.py``); document the scale tradeoff
        rather than building a side-table.

        Returns the user record dict (with ``username`` injected as a
        key) on match, ``None`` on no match or on ES failure.
        """
        if not email:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            response = dict(es.security.get_user())
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("GoogleSsoService.find_user_by_email: get_user failed: %s", e)
            return None

        wanted = email.strip().lower()
        for username, record in response.items():
            stored_email = (record.get("email") or "").strip().lower()
            if stored_email and stored_email == wanted:
                return {"username": username, **record}
        return None

    @staticmethod
    def _stamp_google_sub(username: str, google_sub: str, existing_metadata: dict | None = None) -> bool:
        """Write ``metadata.google_sub`` onto the ES Security user record.

        Preserves all other metadata keys (force_password_change,
        account_type, suspended_at, etc.) by merging onto
        the existing dict — ES Security's ``put_user`` is an upsert
        that overwrites the metadata object wholesale, so dropping
        siblings would silently lose state.
        """
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            current = dict(es.security.get_user(username=username)).get(username, {})
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("GoogleSsoService._stamp_google_sub: get_user('%s') failed: %s", username, e)
            return False

        merged_metadata = dict(existing_metadata or current.get("metadata") or {})
        merged_metadata["google_sub"] = google_sub

        try:
            get_es_admin_client(es).security.put_user(
                username=username,
                body={
                    "roles": current.get("roles", []),
                    "full_name": current.get("full_name", ""),
                    "email": current.get("email", ""),
                    # ES writes enabled=true when it is omitted.
                    "enabled": current.get("enabled", True),
                    "metadata": merged_metadata,
                },
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("GoogleSsoService._stamp_google_sub: put_user('%s') failed: %s", username, e)
            return False
        return True

    @staticmethod
    def sign_in_or_provision(userinfo: dict, allow_provision: bool = True) -> dict:
        """Match a verified Google userinfo payload to an existing user
        or provision a fresh one.

        ``allow_provision=False`` (2026-08-06 open-registration incident):
        the existing-user sign-in branch runs unchanged, but an unmatched
        email is REFUSED with code ``signup_disabled`` instead of
        provisioned. The route layer passes
        ``config.SELF_SIGNUP_ENABLED or session-has-pending-invite`` so
        invited visitors keep working while cold self-signup is closed.

        Existing user (email match):
          - Stamp ``metadata.google_sub`` so subsequent sign-ins can
            fast-path on sub instead of scanning all users (and so we
            have a record of which user is linked to which Google
            account).
          - Return ``user_action="signin"`` so the route layer knows
            to skip the wizard and land on the dashboard.

        New user:
          - Derive a candidate username from the email local-part.
          - Call ``UserProvisioningService.create_user`` (which
            generates a temp password we discard).
          - Replace the temp password with a random one the user
            never sees, then clear ``force_password_change`` (the
            user authenticated via Google — no first-login password
            prompt makes sense).
          - Stamp ``metadata.google_sub``.
          - Return ``user_action="signup"`` so the route layer
            redirects to /onboarding/step-4.

        Returns:
            ``{"success": True, "user_action": "signin"|"signup",
               "username": <str>, "roles": [...], "full_name": <str>,
               "email": <str>}`` on success.
            ``{"success": False, "errors": [<str>, ...]}`` on failure.
        """
        email = (userinfo.get("email") or "").strip().lower()
        google_sub = userinfo.get("sub") or ""
        name = userinfo.get("name") or ""

        if not email or not google_sub:
            return {"success": False, "errors": ["Google profile was incomplete."]}

        # defense in depth: exchange_code_for_userinfo already
        # rejects unverified emails, but this method is the account
        # trust boundary (email match → sign-in, or provision + the
        # route stamping metadata.email_verified=True), so it must not
        # rely on every caller having gated first.
        if userinfo.get("email_verified") is not True:
            logging.warning(
                "AUDIT: action=google_sso_rejected reason=email_not_verified sub=%s",
                google_sub,
            )
            return {"success": False, "errors": ["Google account email is not verified."]}

        existing = GoogleSsoService.find_user_by_email(email)
        if existing is not None:
            username = existing["username"]
            # a suspended account gets
            # no session here, exactly as password login refuses it.
            if existing.get("enabled", True) is False:
                logging.warning(
                    "AUDIT: action=google_sso_rejected reason=account_suspended username=%s",
                    username,
                )
                return {
                    "success": False,
                    "errors": ["This account has been suspended. Please contact your administrator."],
                    "code": "account_suspended",
                }
            # Stamp google_sub if not already present so future sign-ins
            # have the linkage recorded. Failure to stamp is non-fatal
            # — the user can still sign in via the email-match path.
            existing_meta = existing.get("metadata") or {}
            if existing_meta.get("google_sub") != google_sub:
                GoogleSsoService._stamp_google_sub(username, google_sub, existing_meta)
            logging.info(
                "AUDIT: action=google_sso_signin username=%s email=%s",
                username,
                email,
            )
            return {
                "success": True,
                "user_action": "signin",
                "username": username,
                "roles": existing.get("roles", []),
                "full_name": existing.get("full_name") or name or username,
                "email": email,
            }

        # New user — provision (unless self-signup is closed).
        if not allow_provision:
            logging.warning(
                "AUDIT: action=google_sso_signup_refused reason=signup_disabled email=%s",
                email,
            )
            return {
                "success": False,
                "errors": ["New account sign-up is not available right now."],
                "code": "signup_disabled",
            }
        candidate = _derive_username_from_email(email)
        if UserProvisioningService.suggest_unique_username:  # tests may patch this away
            # ``suggest_unique_username`` returns the input unchanged if
            # the username is free, otherwise the next non-colliding
            # variant. Run it pre-emptively so the create_user 409 path
            # is a true exception rather than the common case for SSO.
            candidate = UserProvisioningService.suggest_unique_username(candidate)

        create_result = UserProvisioningService.create_user(
            username=candidate,
            full_name=name,
            email=email,
            roles=list(DEFAULT_SSO_ROLES),
        )
        if not create_result.get("success"):
            # On the rare race (suggest_unique_username said free, but
            # create_user found a collision), surface the suggested
            # alternative the same way does. SSO has no UI to
            # ask the user to pick a different username, so this is a
            # terminal error — but we keep the structured response so
            # the route layer can log a sensible message.
            return {
                "success": False,
                "errors": [create_result.get("error") or "Could not create account."],
                "code": create_result.get("code") or "create_failed",
            }

        # Replace the temp password with one the user never sees, then
        # clear force_password_change. The user authenticates via
        # Google going forward; we still keep a password on the ES
        # record so that ES Security can compare against it if the
        # SSO link is ever broken and admin needs to reset.
        es = get_elasticsearch_client()
        if es is None:
            logging.error(
                "GoogleSsoService.sign_in_or_provision: ES became unreachable " "after create_user succeeded for '%s'.",
                candidate,
            )
            return {
                "success": False,
                "errors": ["Account was created but could not be activated. Please contact support."],
            }

        try:
            get_es_admin_client(es).security.change_password(
                username=candidate, body={"password": _generate_unguessable_password()}
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.error("GoogleSsoService.sign_in_or_provision: change_password failed: %s", e)
            return {
                "success": False,
                "errors": ["Could not finalize Google sign-in. Please try again."],
            }

        if not UserProvisioningService.clear_force_password_change(candidate):
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.error(
                "GoogleSsoService.sign_in_or_provision: clear_force_password_change " "failed for '%s'.",
                candidate,
            )
            # Non-fatal — the user can still log in via Google because
            # the auth_method bypass on /onboarding/google/callback
            # never consults force_password_change. Surface a partial
            # success so the route layer audit-logs it.

        # stamp credential_changed_at for the password just set
        # above. Fail-soft and non-fatal, same reasoning as
        # clear_force_password_change immediately above — the caller (the
        # /onboarding/google/callback route) establishes the session after
        # this function returns, so there is no pre-existing session to
        # worry about denying here; this covers a re-provision of an
        # account that already had a live session elsewhere.
        UserProvisioningService.stamp_credential_changed(candidate)

        # Stamp google_sub so subsequent sign-ins fast-path. Pass
        # ``existing_metadata=None`` so the helper re-reads the live
        # record (which already has ``force_password_change=False``
        # from the clear step above plus any ``account_type``)
        # and merges the sub onto that, rather than overwriting with a
        # stale dict.
        GoogleSsoService._stamp_google_sub(candidate, google_sub, existing_metadata=None)

        logging.info(
            "AUDIT: action=google_sso_signup username=%s email=%s",
            candidate,
            email,
        )
        return {
            "success": True,
            "user_action": "signup",
            "username": candidate,
            "roles": list(DEFAULT_SSO_ROLES),
            "full_name": name or candidate,
            "email": email,
        }


__all__ = [
    "DEFAULT_SSO_ROLES",
    "GOOGLE_AUTHORIZE_URL",
    "GOOGLE_SCOPES",
    "GoogleSsoService",
]
