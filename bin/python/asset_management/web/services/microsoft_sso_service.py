"""
Microsoft SSO Service

"Continue with Microsoft" entry point for the New User Onboarding
wizard, the second SSO option alongside Google
SSO. Two responsibilities, mirroring ``GoogleSsoService``:

1. Build the Microsoft Identity Platform authorize URL and exchange
   the callback code for OIDC userinfo (sub, email, name) plus the
   id_token claims (tid, oid) that establish how far the email claim
   can be trusted.
2. Match the payload to an existing ES Security user and sign them
   in; otherwise provision a fresh user with a random password
   (the user authenticates via Microsoft going forward — they never
   see or use this password) and stamp ``metadata.microsoft_sub``
   for sub-based linkage on subsequent sign-ins.

Mirrors the ``GoogleSsoService`` shape so the two providers
behave identically from the route layer's perspective. The Microsoft
path differs only at the OAuth wire-protocol layer:

  * Authorize endpoint uses ``login.microsoftonline.com`` with a
    configurable tenant (``common`` accepts both work/school and
    personal accounts; locked-down deployments can pin to a
    specific Entra tenant ID).
  * Userinfo is fetched from Microsoft Graph's OIDC userinfo
    endpoint (``https://graph.microsoft.com/oidc/userinfo``) which
    returns the standard OIDC ``sub`` / ``email`` / ``name`` fields,
    so the rest of the matching + provisioning code is identical.
  * Scopes are the OIDC ``openid email profile`` triple (Microsoft
    does not require ``User.Read`` to hit the OIDC userinfo
    endpoint).

Existing-user match strategy (, superseding the 2026-05-12
email-only decision — see DECISIONS.md): ``metadata.microsoft_sub``
first, then email. Microsoft's ``email``/``upn`` claims are NOT
verified attributes — an administrator of any Entra tenant can set
them to an arbitrary address, and with tenant ``common`` any tenant
can sign in (the nOAuth cross-tenant account-takeover pattern). The
``sub`` claim, by contrast, is issued by Microsoft per (user, app)
and is not attacker-settable, so a stored-sub match is always safe.
Email match / provisioning is only allowed when the id_token marks
the email claim trustworthy: the deployment pinned its own tenant,
the account is a consumer (MSA) account whose email Microsoft
itself verifies, or the tenant asserts ``xms_edov`` (email domain
owner verified).

Library choice: ``requests`` directly. ``msal`` is the official
Microsoft Python library but it's confidential-client heavy and
opinionated about session storage; for a thin server-side OAuth2
code-flow handshake the direct ``requests`` calls keep the mock
surface small (same rationale as vs ``google-auth-oauthlib``).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import string
import sys
from typing import Any

# Top-level imports of UserProvisioningService and get_elasticsearch_client
# so tests can patch them on this module (
# pattern). The sys.path insert mirrors sibling services so
# ``lib.es_helpers`` resolves whether the module is loaded from the
# test harness (where conftest extends sys.path) or from
# imports_container (where ``/app/python`` is the resolution root).
sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client, get_es_admin_client  # noqa: E402
from asset_management.web.services.user_provisioning_service import (  # noqa: E402
    UserProvisioningService,
)

# Microsoft Identity Platform OAuth2 + OIDC endpoints. ``{tenant}`` is
# substituted at authorize-URL-build time so a single service can serve
# multi-tenant (``common``) or single-tenant (an Entra tenant GUID or
# verified domain) deployments without code changes. Userinfo lives at
# graph.microsoft.com under the OIDC path and returns the standard
# OIDC claim set — preferred over ``/me`` because we only need OIDC
# fields and ``/me`` would pull a broader payload we'd have to ignore.
MICROSOFT_AUTHORIZE_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MICROSOFT_USERINFO_URL = "https://graph.microsoft.com/oidc/userinfo"

# the well-known tenant ID Microsoft stamps as ``tid`` on
# consumer (personal MSA) accounts. MSA emails are verified by
# Microsoft itself (they are the account's login identifier), so this
# tenant is exempt from the nOAuth email-distrust rule below.
MICROSOFT_MSA_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"

# The multi-tenant endpoint aliases accepted by
# login.microsoftonline.com in place of a concrete tenant. When
# MICROSOFT_SSO_TENANT is one of these, ANY Entra tenant's users can
# complete the code flow, so the email/upn claims (settable by that
# tenant's admin) must not be trusted for account matching.
_MULTI_TENANT_ALIASES = frozenset({"common", "organizations", "consumers"})

_TENANT_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# OIDC scopes — ``openid`` issues the ID token, ``email`` adds the
# verified email claim, ``profile`` adds name. We deliberately do NOT
# request ``offline_access`` (no refresh tokens — this is a one-shot
# sign-in, the user re-auths via Microsoft each time their session
# expires) or any Graph-specific permissions.
MICROSOFT_SCOPES = [
    "openid",
    "email",
    "profile",
]

# ES Security roles assigned to an SSO-provisioned account.
# Empty list — see DEFAULT_SIGNUP_ROLES in onboarding_signup_service.py
# for the rationale (KA users never authenticate to Kibana directly,
# and ES Security's authenticate() does not require a role for a
# successful auth call).
DEFAULT_SSO_ROLES: list[str] = []

# Username-from-email rules: lowercase, restrict to the local-part's
# alphanumerics + underscore + dot + hyphen, trim to <= 30 chars, pad
# short locals with random hex. Mirrors the regex.
_USERNAME_SAFE_RE = re.compile(r"[^a-z0-9_.\-]")


def _derive_username_from_email(email: str) -> str:
    """Derive a candidate ES username from a verified Microsoft email.

    Local-part only, lowercased, scrubbed of disallowed characters.
    If the result is shorter than 3 characters, pad with 4 random hex
    chars so the username regex accepts it. Final length is
    clamped to 30. Collision is the caller's problem —
    ``UserProvisioningService.suggest_unique_username`` handles it on
    the create path.
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
    password they don't know. The user authenticates via Microsoft
    going forward; this is a defense-in-depth fallback (and a value
    ES Security can compare against if the SSO link is ever broken).
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


def _decode_id_token_claims(id_token: str) -> dict | None:
    """Decode the id_token's payload segment and return its claims,
    or ``None`` on any parse failure.

    The signature is deliberately NOT verified: the token arrives in
    the same TLS response body as the access token, straight from
    login.microsoftonline.com's token endpoint in a confidential-client
    code flow — there is no untrusted hop where a forged token could
    have been substituted. Signature verification would require the
    per-tenant JWKS fetch + rotation handling for zero additional
    trust on this channel. If the token is ever accepted from a
    browser-supplied source (implicit/hybrid flow), that assumption
    breaks and full validation becomes mandatory.
    """
    try:
        payload_b64 = id_token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001 — any malformed token means "no claims"
        return None
    return dict(claims) if isinstance(claims, dict) else None


def _email_claim_trusted(tid: str, tenant: str, claims: dict) -> bool:
    """(nOAuth): is the payload's email/upn claim safe to use
    for account matching or provisioning?

    Microsoft's ``email``/``upn`` are tenant-admin-settable, so they
    are only trustworthy when one of these holds:

    * the deployment pinned its own tenant (``MICROSOFT_SSO_TENANT``
      is not a multi-tenant alias) — the token endpoint is then
      tenant-specific, so the claim comes from the org that owns this
      deployment, which is inside the trust boundary;
    * ``tid`` is the consumer (MSA) tenant — Microsoft verifies MSA
      emails itself;
    * the id_token carries ``xms_edov`` (email domain owner verified)
      as JSON ``true`` — Microsoft's own opt-in nOAuth mitigation
      claim, configured on the app registration.

    Strict ``is True`` on ``xms_edov`` for the same reason as the
     ``email_verified`` parse: string junk must not coerce.
    """
    tenant_normalized = (tenant or "common").strip().lower()
    if tenant_normalized not in _MULTI_TENANT_ALIASES:
        return True
    if tid.lower() == MICROSOFT_MSA_TENANT_ID:
        return True
    return claims.get("xms_edov") is True


class MicrosoftSsoService:
    """Microsoft SSO sign-in / sign-up contract for the wizard."""

    @staticmethod
    def generate_state_token() -> str:
        """Return a fresh URL-safe state token. The route handler stores
        this in the Flask session before redirecting to Microsoft, then
        compares it against the ``state`` query param on the callback
        to defeat cross-site request forgery on the OAuth handshake.
        """
        return secrets.token_urlsafe(32)

    @staticmethod
    def build_authorize_url(
        client_id: str,
        redirect_uri: str,
        state: str,
        tenant: str = "common",
    ) -> str:
        """Build the Microsoft Identity Platform authorize URL.

        ``tenant`` defaults to ``common`` (accepts both AAD and MSA
        accounts). Single-tenant deployments pass an Entra tenant GUID
        or verified domain. We construct the URL ourselves rather than
        using ``msal`` for the same reason avoids
        ``google-auth-oauthlib``'s ``Flow``: the route handler stays
        thin and the test mock surface is small.
        """
        from urllib.parse import urlencode

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(MICROSOFT_SCOPES),
            "state": state,
            "response_mode": "query",
            "prompt": "select_account",
        }
        authorize_url = MICROSOFT_AUTHORIZE_URL_TEMPLATE.format(tenant=tenant or "common")
        return f"{authorize_url}?{urlencode(params)}"

    @staticmethod
    def exchange_code_for_userinfo(
        code: str,
        state_received: str,
        state_expected: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        tenant: str = "common",
    ) -> dict:
        """Validate state, exchange the auth code for tokens, then
        fetch OIDC userinfo from Microsoft Graph and cross-check it
        against the id_token claims.

        Returns ``{"success": True, "sub": <str>, "email": <str>,
        "name": <str>, "tid": <str>, "oid": <str>,
        "email_trusted": <bool>}`` on success;
        ``{"success": False, "error": <str>, "code": <code>}`` on
        failure. ``code`` is one of: ``state_mismatch``, ``no_code``,
        ``token_exchange_failed``, ``userinfo_failed``,
        ``id_token_invalid``, ``tenant_mismatch``.

        ``email_trusted`` is the nOAuth gate — see
        ``_email_claim_trusted``. It is returned rather than enforced
        here because a sub-linked user may still sign in with an
        untrusted email claim; ``sign_in_or_provision`` is the trust
        boundary that decides.
        """
        if not state_received or not state_expected or state_received != state_expected:
            logging.warning(
                "MicrosoftSsoService.exchange_code_for_userinfo: state mismatch " "(received_len=%s, expected_len=%s)",
                len(state_received or ""),
                len(state_expected or ""),
            )
            return {"success": False, "error": "Authentication failed (state mismatch).", "code": "state_mismatch"}

        if not code:
            return {"success": False, "error": "Missing authorization code.", "code": "no_code"}

        # Token exchange. We use ``requests`` directly rather than
        # ``msal.ConfidentialClientApplication.acquire_token_by_authorization_code``
        # for the same reason avoids ``Flow.fetch_token`` — it
        # is easier to drive a plain POST here and keep the mock
        # surface area small in tests.
        import requests

        token_url = MICROSOFT_TOKEN_URL_TEMPLATE.format(tenant=tenant or "common")
        try:
            token_resp = requests.post(
                token_url,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "scope": " ".join(MICROSOFT_SCOPES),
                },
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.error("MicrosoftSsoService: token exchange request failed: %s", e)
            return {"success": False, "error": "Could not contact Microsoft.", "code": "token_exchange_failed"}

        if token_resp.status_code != 200:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.warning(
                "MicrosoftSsoService: token exchange returned %s: %s",
                token_resp.status_code,
                token_resp.text[:200],
            )
            return {
                "success": False,
                "error": "Microsoft rejected the sign-in.",
                "code": "token_exchange_failed",
            }

        token_payload = token_resp.json() or {}
        access_token = token_payload.get("access_token")
        if not access_token:
            return {"success": False, "error": "Microsoft did not return a token.", "code": "token_exchange_failed"}

        # the id_token (always present — we request the
        # ``openid`` scope) carries the claims userinfo does not:
        # ``tid`` (which tenant vouches for this account) and
        # optionally ``xms_edov``. Without them the email claim's
        # provenance is unknowable, so a missing/undecodable id_token
        # is a terminal failure, not a degrade.
        id_claims = _decode_id_token_claims(token_payload.get("id_token") or "")
        if id_claims is None or not id_claims.get("tid"):
            logging.warning("MicrosoftSsoService: token response id_token missing or undecodable.")
            return {
                "success": False,
                "error": "Microsoft did not return a valid identity token.",
                "code": "id_token_invalid",
            }
        tid = str(id_claims.get("tid") or "")
        oid = str(id_claims.get("oid") or "")

        # Defense in depth for pinned-tenant deployments: the token
        # endpoint is already tenant-specific (codes minted by other
        # tenants fail the exchange above), but if the configured
        # tenant is a GUID we can also assert the issued tid matches.
        tenant_normalized = (tenant or "common").strip().lower()
        if _TENANT_GUID_RE.match(tenant_normalized) and tid.lower() != tenant_normalized:
            logging.warning(
                "MicrosoftSsoService: id_token tid=%s does not match the configured tenant.",
                tid,
            )
            return {
                "success": False,
                "error": "Microsoft account belongs to the wrong organization.",
                "code": "tenant_mismatch",
            }

        try:
            ui_resp = requests.get(
                MICROSOFT_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("MicrosoftSsoService: userinfo request failed: %s", e)
            return {"success": False, "error": "Could not fetch your Microsoft profile.", "code": "userinfo_failed"}

        if ui_resp.status_code != 200:
            return {"success": False, "error": "Microsoft did not return profile data.", "code": "userinfo_failed"}

        ui = ui_resp.json() or {}
        # Microsoft Graph OIDC userinfo returns ``sub`` (stable
        # per-app subject), ``email``, and ``name`` (display name).
        # neither ``email`` nor the ``upn`` fallback is a
        # verified attribute — both are settable by the issuing
        # tenant's admin; ``email_trusted`` below records whether the
        # issuing tenant is inside our trust boundary. The ``upn``
        # fallback stays so single-tenant work accounts without a
        # published email still match.
        sub = ui.get("sub") or ""
        email = (ui.get("email") or ui.get("upn") or "").strip().lower()
        name = ui.get("name") or ""

        if not email or not sub:
            return {"success": False, "error": "Microsoft profile is missing email.", "code": "userinfo_failed"}

        # The userinfo response and the id_token describe the same
        # authentication — a subject mismatch means one of them is not
        # about this user, so nothing in the payload can be relied on.
        id_sub = str(id_claims.get("sub") or "")
        if id_sub and id_sub != sub:
            logging.warning("MicrosoftSsoService: userinfo sub does not match id_token sub.")
            return {
                "success": False,
                "error": "Microsoft returned inconsistent identity data.",
                "code": "id_token_invalid",
            }

        return {
            "success": True,
            "sub": sub,
            "email": email,
            "name": name,
            "tid": tid,
            "oid": oid,
            "email_trusted": _email_claim_trusted(tid, tenant, id_claims),
        }

    @staticmethod
    def find_user_by_email(email: str) -> dict | None:
        """Look up an ES Security user whose stored email matches ``email``.

        ES Security has no native email index — ``get_user()`` without
        a username argument returns every user. With <100 users this
        is fine (and matches the existing pattern in
        ``role_permissions_service.py`` + ``google_sso_service.py``);
        document the scale tradeoff rather than building a side-table.

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
            logging.error("MicrosoftSsoService.find_user_by_email: get_user failed: %s", e)
            return None

        wanted = email.strip().lower()
        for username, record in response.items():
            stored_email = (record.get("email") or "").strip().lower()
            if stored_email and stored_email == wanted:
                return {"username": username, **record}
        return None

    @staticmethod
    def find_user_by_microsoft_sub(microsoft_sub: str) -> dict | None:
        """Look up an ES Security user whose ``metadata.microsoft_sub``
        matches ``microsoft_sub``.

        This is the safe linkage path: ``sub`` is issued by Microsoft
        per (user, app) and cannot be set by a hostile tenant admin,
        unlike the email/upn claims. Same scan-all-users approach (and
        scale tradeoff) as ``find_user_by_email``.

        Returns the user record dict (with ``username`` injected as a
        key) on match, ``None`` on no match or on ES failure.
        """
        if not microsoft_sub:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            response = dict(es.security.get_user())
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("MicrosoftSsoService.find_user_by_microsoft_sub: get_user failed: %s", e)
            return None

        for username, record in response.items():
            metadata = record.get("metadata") or {}
            if metadata.get("microsoft_sub") == microsoft_sub:
                return {"username": username, **record}
        return None

    @staticmethod
    def _stamp_microsoft_sub(username: str, microsoft_sub: str, existing_metadata: dict | None = None) -> bool:
        """Write ``metadata.microsoft_sub`` onto the ES Security user record.

        Preserves all other metadata keys (force_password_change,
        account_type, google_sub,
        suspended_at, etc.) by merging onto the existing dict — ES
        Security's ``put_user`` is an upsert that overwrites the
        metadata object wholesale, so dropping siblings would
        silently lose state.
        """
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            current = dict(es.security.get_user(username=username)).get(username, {})
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error(
                "MicrosoftSsoService._stamp_microsoft_sub: get_user('%s') failed: %s",
                username,
                e,
            )
            return False

        merged_metadata = dict(existing_metadata or current.get("metadata") or {})
        merged_metadata["microsoft_sub"] = microsoft_sub

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
            logging.error(
                "MicrosoftSsoService._stamp_microsoft_sub: put_user('%s') failed: %s",
                username,
                e,
            )
            return False
        return True

    @staticmethod
    def _refuse_suspended(username: str) -> dict:
        """a suspended account gets no
        session through either existing-user branch, exactly as password
        login refuses it."""
        logging.warning(
            "AUDIT: action=microsoft_sso_rejected reason=account_suspended username=%s",
            username,
        )
        return {
            "success": False,
            "errors": ["This account has been suspended. Please contact your administrator."],
            "code": "account_suspended",
        }

    @staticmethod
    def sign_in_or_provision(userinfo: dict, allow_provision: bool = True) -> dict:
        """Match a Microsoft userinfo payload to an existing user or
        provision a fresh one. This is the account trust boundary
        .

        ``allow_provision=False`` (2026-08-06 open-registration incident):
        both existing-user branches (sub match, email match) run
        unchanged, but an unmatched account is REFUSED with code
        ``signup_disabled`` instead of provisioned — mirrors
        GoogleSsoService.sign_in_or_provision.

        Match order:
          1. ``metadata.microsoft_sub == sub`` — always safe, works
             even when the email claim is untrusted (sub is
             Microsoft-issued, not tenant-settable).
          2. Email match / provisioning — only when the exchange
             marked the payload ``email_trusted=True``; an untrusted
             email must never match (account takeover) or provision
             (the created record would poison future email matching
             with an address the attacker chose).

        Existing user (sub or email match):
          - Stamp ``metadata.microsoft_sub`` on an email match so
            subsequent sign-ins take the sub path.
          - Return ``user_action="signin"`` so the route layer knows
            to skip the wizard and land on the dashboard.

        New user:
          - Derive a candidate username from the email local-part.
          - Call ``UserProvisioningService.create_user`` (which
            generates a temp password we discard).
          - Replace the temp password with a random one the user
            never sees, then clear ``force_password_change``.
          - Stamp ``metadata.microsoft_sub``.
          - Return ``user_action="signup"`` so the route layer
            redirects to /onboarding/step-4.

        Returns:
            ``{"success": True, "user_action": "signin"|"signup",
               "username": <str>, "roles": [...], "full_name": <str>,
               "email": <str>}`` on success.
            ``{"success": False, "errors": [<str>, ...]}`` on failure.
        """
        email = (userinfo.get("email") or "").strip().lower()
        microsoft_sub = userinfo.get("sub") or ""
        name = userinfo.get("name") or ""

        if not email or not microsoft_sub:
            return {"success": False, "errors": ["Microsoft profile was incomplete."]}

        # sub-first linkage. A user who has signed in via
        # Microsoft before carries metadata.microsoft_sub; matching on
        # it needs no email trust at all, so already-linked users keep
        # working even from tenants outside the trust boundary.
        linked = MicrosoftSsoService.find_user_by_microsoft_sub(microsoft_sub)
        if linked is not None:
            username = linked["username"]
            if linked.get("enabled", True) is False:
                return MicrosoftSsoService._refuse_suspended(username)
            logging.info(
                "AUDIT: action=microsoft_sso_signin username=%s match=sub",
                username,
            )
            return {
                "success": True,
                "user_action": "signin",
                "username": username,
                "roles": linked.get("roles", []),
                "full_name": linked.get("full_name") or name or username,
                # The stored email, NOT the claim — on this path the
                # claim may be untrusted and must not enter the session.
                "email": (linked.get("email") or "").strip().lower(),
            }

        # (nOAuth): beyond this point the email claim IS the
        # identity, so an untrusted claim is a terminal rejection.
        # Strict ``is True`` — a missing or junk flag must fail closed.
        if userinfo.get("email_trusted") is not True:
            logging.warning(
                "AUDIT: action=microsoft_sso_rejected reason=email_not_trusted sub=%s tid=%s",
                microsoft_sub,
                userinfo.get("tid"),
            )
            return {
                "success": False,
                "errors": ["Microsoft could not verify that this email address belongs to your account."],
            }

        existing = MicrosoftSsoService.find_user_by_email(email)
        if existing is not None:
            username = existing["username"]
            if existing.get("enabled", True) is False:
                return MicrosoftSsoService._refuse_suspended(username)
            # Stamp microsoft_sub if not already present so future
            # sign-ins have the linkage recorded. Failure to stamp is
            # non-fatal — the user can still sign in via the
            # email-match path.
            existing_meta = existing.get("metadata") or {}
            if existing_meta.get("microsoft_sub") != microsoft_sub:
                MicrosoftSsoService._stamp_microsoft_sub(username, microsoft_sub, existing_meta)
            logging.info(
                "AUDIT: action=microsoft_sso_signin username=%s email=%s",
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
                "AUDIT: action=microsoft_sso_signup_refused reason=signup_disabled email=%s",
                email,
            )
            return {
                "success": False,
                "errors": ["New account sign-up is not available right now."],
                "code": "signup_disabled",
            }
        candidate = _derive_username_from_email(email)
        if UserProvisioningService.suggest_unique_username:  # tests may patch this away
            candidate = UserProvisioningService.suggest_unique_username(candidate)

        create_result = UserProvisioningService.create_user(
            username=candidate,
            full_name=name,
            email=email,
            roles=list(DEFAULT_SSO_ROLES),
        )
        if not create_result.get("success"):
            return {
                "success": False,
                "errors": [create_result.get("error") or "Could not create account."],
                "code": create_result.get("code") or "create_failed",
            }

        # Replace the temp password with one the user never sees, then
        # clear force_password_change. The user authenticates via
        # Microsoft going forward; we still keep a password on the ES
        # record so that ES Security can compare against it if the
        # SSO link is ever broken and admin needs to reset.
        es = get_elasticsearch_client()
        if es is None:
            logging.error(
                "MicrosoftSsoService.sign_in_or_provision: ES became unreachable " "after create_user succeeded for '%s'.",
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
            logging.error("MicrosoftSsoService.sign_in_or_provision: change_password failed: %s", e)
            return {
                "success": False,
                "errors": ["Could not finalize Microsoft sign-in. Please try again."],
            }

        if not UserProvisioningService.clear_force_password_change(candidate):
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.error(
                "MicrosoftSsoService.sign_in_or_provision: clear_force_password_change " "failed for '%s'.",
                candidate,
            )
            # Non-fatal — the user can still log in via Microsoft
            # because the auth_method bypass on
            # /onboarding/microsoft/callback never consults
            # force_password_change.

        # stamp credential_changed_at for the password just set
        # above. Fail-soft and non-fatal, same reasoning as
        # clear_force_password_change immediately above — the caller (the
        # /onboarding/microsoft/callback route) establishes the session
        # after this function returns, so there is no pre-existing session
        # to worry about denying here; this covers a re-provision of an
        # account that already had a live session elsewhere.
        UserProvisioningService.stamp_credential_changed(candidate)

        MicrosoftSsoService._stamp_microsoft_sub(candidate, microsoft_sub, existing_metadata=None)

        logging.info(
            "AUDIT: action=microsoft_sso_signup username=%s email=%s",
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
    "MICROSOFT_AUTHORIZE_URL_TEMPLATE",
    "MICROSOFT_MSA_TENANT_ID",
    "MICROSOFT_SCOPES",
    "MICROSOFT_TOKEN_URL_TEMPLATE",
    "MICROSOFT_USERINFO_URL",
    "MicrosoftSsoService",
]
