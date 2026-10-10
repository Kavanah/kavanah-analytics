"""
Wizard OAuth Service

Step-5 of the New User Onboarding wizard's Meta OAuth roundtrip. The
service owns three responsibilities:

1. Build the Meta authorize URL with a wizard-bound ``state`` token
   and a caller-supplied ``scope`` list (so the re-grant path can
   request *only* the missing scopes — the AC's "re-redirect with
   the missing scopes only" requirement).

2. Exchange the callback ``code`` for an access token, then call
   Meta's ``/debug_token`` endpoint to read the *actual* granted
   scopes (the ``data.scopes`` array). whole point is
   surfacing partial-scope outcomes, and the granted-scope list is
   the load-bearing input to that surface.

3. Discard the access token. is scoped to scope reconciliation
   only; token persistence (so step-6 can call Marketing API) is a
   downstream ticket. Keeping the token in-memory and dropping it on
   exit is the deliberate scope boundary — a future ticket adds the
   encrypted-storage path without changing this service's signature.

Meta app credentials (client_id + client_secret) come from
``de_asset_fb_apps``. the wizard picks the row whose
``fb_app_id`` matches the explicit ``WIZARD_META_APP_ID`` config
(master-config ``fboauth.wizard_meta_app_id`` or env var). earlier
the picker was ``match_all + _doc:asc`` which silently routed through
whichever doc Lucene happened to put first in insertion order — a
stale app from earlier testing could shadow the canonical one. A
long-term Kavanah-owned shared app is still the right answer and that
remains tracked as a separate follow-up. The asset-management Pages/Ads flow
in ``fb_oauth_server.py`` is intentionally untouched — its callback
writes a per-user token doc keyed by ``fb_app_user_auth_state``,
which the wizard doesn't have. The two flows share the Meta app
record but nothing else.

This module imports ``lib.es_helpers`` + ``lib.encryption`` +
``lib.fb_constants`` so the service runs identically under
``imports_container`` and the test harness. Network I/O is performed
via ``requests``; the test harness mocks ``requests.get`` and
``requests.post`` at the service-module import path.
"""

from __future__ import annotations

import logging
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

sys.path.insert(0, "/app/python")

import requests  # noqa: E402 — top-level so tests can patch via the module path

from lib.encryption import decrypt  # noqa: E402
from lib.es_helpers import get_elasticsearch_client  # noqa: E402
from lib.fb_constants import FB_GRAPH_URL, FB_OAUTH_DIALOG_URL  # noqa: E402

# imported at module level so tests can patch
# ``wizard_oauth_service.config.WIZARD_META_APP_ID`` via mock.patch on
# the attribute path. The config module is leaf in the import graph
# (only depends on stdlib + yaml) so there's no circular-import risk
# from binding it here.
from asset_management.web.config import config  # noqa: E402

logger = logging.getLogger(__name__)


# Connector → list of provider scope keys that the wizard requests by
# default. Today only ``meta_ads`` ships; this map mirrors the
# ConnectorScopeManifestService catalog so a future connector adds
# itself in two places (the user-facing manifest + the provider-side
# scope list) which is intentional — the manifest is the *display*
# contract, this map is the *wire* contract, and conflating them would
# couple copy edits to provider permissions.
#
# Issue 1 (2026-05-21): pre-fix this map carried
# ``read_insights`` — a scope key that does not exist in Meta's Login
# permissions catalogue today. Meta's OAuth dialog responded with
# "Invalid Scopes: read_insights" and refused to render. Removed.
#
# (2026-05-21): added ``pages_read_engagement`` +
# ``instagram_basic`` + ``instagram_manage_insights`` to match the
# App Review submission's 6-scope claim. Per the app-review
# session: the consent screen the reviewer sees during the hands-on
# test must match what the submission requests; a 3-scope consent
# screen against a 6-scope submission is a direct rejection signal.
# Until Meta formally approves, only app-tester accounts (devs) get
# the full consent dialog — non-tester users can't reach the wizard
# yet, so there's no production impact from requesting gated scopes.
#
# 2026-05-28: re-added ``read_insights`` (reversing the Issue 1
# removal above). removed it because Meta's OAuth dialog
# rejected it with "Invalid Scopes: read_insights". That rejection no
# longer applies — the Meta-side permission was fixed and the active
# integration now authorises against a system user token that carries
# ``read_insights``, so the consent dialog renders it correctly. Kept
# in lockstep with the display manifest (see
# ``connector_scope_manifest_service``) per the sync invariant.
_DEFAULT_SCOPES_BY_CONNECTOR: dict[str, tuple[str, ...]] = {
    "meta_ads": (
        "ads_read",
        "business_management",
        "pages_show_list",
        "pages_read_engagement",
        "instagram_basic",
        "instagram_manage_insights",
        "read_insights",
    ),
}


# Error codes the route handler maps onto the OnboardingOAuthErrorService
# bucket vocabulary. Kept as string constants so callsites don't fall
# back to string literals (which a typo would silently miss).
EXCHANGE_OK = "ok"
EXCHANGE_NO_CODE = "no_code"
EXCHANGE_STATE_MISMATCH = "state_mismatch"
EXCHANGE_NO_APP = "no_app"
EXCHANGE_TOKEN_FAILED = "token_exchange_failed"
EXCHANGE_DEBUG_TOKEN_FAILED = "debug_token_failed"


@dataclass(frozen=True)
class MetaAppCredentials:
    """One row from ``de_asset_fb_apps`` reduced to the OAuth-relevant
    fields. ``client_secret`` is already decrypted — the caller must
    not log it. ``esid`` is the document id so the callback can find
    its way back to the same app record without re-scanning the index.
    """

    esid: str
    client_id: str
    client_secret: str


def _epoch_to_iso(raw) -> str | None:
    """debug_token timestamps are unix epochs; the connection schema stores
    ISO-8601. A missing / zero / malformed value stays None so the index field
    is null (Meta reports 0 for clocks that do not apply)."""
    if isinstance(raw, (int, float)) and raw > 0:
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat()
        except Exception:  # noqa: BLE001 — out-of-range epoch
            return None
    return None


@dataclass(frozen=True)
class ExchangeResult:
    """Outcome of the ``/oauth-callback`` token exchange + debug_token
    call. The route reads ``success`` for the happy path and ``code``
    for the failure surface (which it maps onto the existing
    ``OnboardingOAuthErrorService`` bucket vocabulary via the same
    ``?error=`` query string the route already knows how to read).

    ``granted_scopes`` is the normalised set from
    ``debug_token.data.scopes`` — the load-bearing field
    scope reconciliation. Empty on any failure path AND on the case
    where Meta returned an empty ``data.scopes`` array (zero scopes
    granted — the NONE_USABLE bucket on the step-5 GET).

     follow-up — ``access_token``, ``provider_account_id``,
    ``token_expires_at`` carry the load-bearing fields the wizard-aware
    connection-persistence handoff needs. ``access_token`` is the
    plaintext token (callers must hand it straight to
    ``ConnectionService.upsert_connection`` which encrypts at rest —
    NEVER log the field). The three are empty on any failure path and
    on the legacy ``discard-token`` posture (preserved for
    callers that don't want the token).
    """

    success: bool
    code: str
    granted_scopes: tuple[str, ...] = ()
    # follow-up — present on the happy path; empty on failure.
    access_token: str = ""
    provider_account_id: str = ""
    token_expires_at: str | None = None
    # debug_token ``data_access_expires_at`` as ISO-8601: when Meta's
    # 90-day data-access window on this token closes. None when Meta omits it
    # or reports 0 (system-user tokens carry no window).
    data_access_expires_at: str | None = None


class WizardOAuthService:
    """OAuth roundtrip for the step-5 Meta handoff."""

    # ── Authorize-URL side ──────────────────────────────────────────────

    @staticmethod
    def generate_state_token() -> str:
        """Return a fresh URL-safe state token.

        The route handler stores this on the wizard_session blob
        before redirecting to Meta, then compares it against the
        callback's ``state`` query param. State is wizard-session-
        bound (not just Flask-session-bound) so a multi-tab user
        can't replay another tab's state, and so an in-flight OAuth
        roundtrip survives a refresh of the originating tab.
        """
        return secrets.token_urlsafe(32)

    @staticmethod
    def default_scopes_for(connector_id: str) -> tuple[str, ...]:
        """Provider-side scope keys the wizard requests for a fresh
        OAuth on ``connector_id``. Mirrors the manifest catalog. An
        unknown connector returns an empty tuple — the route gates on
        the manifest first, so this branch should never be hit in
        prod, but it's the safer default than KeyError.
        """
        return _DEFAULT_SCOPES_BY_CONNECTOR.get(connector_id, ())

    @staticmethod
    def build_authorize_url(
        client_id: str,
        redirect_uri: str,
        state: str,
        scopes: Iterable[str],
        *,
        reauthenticate: bool = False,
    ) -> str:
        """Build the Meta OAuth dialog URL.

        ``scopes`` is the comma-joined Meta scope list; the AC's
        re-grant path passes ``missing_scopes_only`` here so the
        second consent screen asks for less, not the full original
        set. ``state`` defeats CSRF on the callback. The URL is
        constructed manually (no facebook-business SDK helper) to
        keep the test mock surface trivial.

        ``reauthenticate=True`` appends Meta's documented
        ``auth_type=reauthenticate`` plus an ``auth_nonce``
        (single-use random token) to force the FB account picker
        even when the user already has an active FB session in the
        same browser. Used by the "Switch account" affordances on
        step-5 and step-6 — they cannot rely on facebook.com/logout.php
        because Meta stopped honoring its ``?next=`` parameter for
        non-facebook.com destinations years ago (the legacy step-5
        anchor silently dropped users on facebook.com home, still
        logged in). The reauthenticate path is the only documented
        way to force account-picker behaviour from a third-party app.
        """
        from urllib.parse import urlencode

        scope_str = ",".join(s.strip() for s in scopes if s and s.strip())
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope_str,
            "response_type": "code",
        }
        if reauthenticate:
            params["auth_type"] = "reauthenticate"
            # auth_nonce is a single-use token Meta requires alongside
            # auth_type=reauthenticate. We use the same generator as
            # ``generate_state_token`` (urlsafe base64, ≥32 chars) so
            # the entropy budget matches.
            params["auth_nonce"] = secrets.token_urlsafe(32)
        return f"{FB_OAUTH_DIALOG_URL}?{urlencode(params)}"

    # ── Meta app lookup ─────────────────────────────────────────────────

    @staticmethod
    def get_meta_app() -> MetaAppCredentials | None:
        """Return the wizard's Meta app credentials, or ``None`` if the
        configured app cannot be resolved.

        looks up ``de_asset_fb_apps`` by the explicit
        ``WIZARD_META_APP_ID`` config key (master-config
        ``fboauth.wizard_meta_app_id`` or env var). The prior implementation
        ran ``match_all + sort:[{_doc:asc}] + size:1``, which returned
        whichever doc Lucene happened to put first in insertion order — a
        stale app from earlier testing could silently shadow the intended
        canonical one. Pinning the lookup to a per-environment config key
        makes the picker deterministic and operator-auditable; multi-app
        clusters (dev, staging, prod) each route through the right app
        without sharing rows.

        Returns ``None`` when (a) the config key is unset, (b) ES is
        unreachable, (c) no ``de_asset_fb_apps`` row matches the
        configured ``fb_app_id``, or (d) the matching row is missing its
        client_id / client_secret. Every miss path is logged at WARN with
        the configured ID so operations can diagnose without a deploy
        (per the ticket's audit AC).

        ``client_secret`` is decrypted in-place via the project's
        ``lib.encryption`` helper. The caller must not log or persist
        the decrypted value.
        """
        configured_app_id = (getattr(config, "WIZARD_META_APP_ID", "") or "").strip()
        if not configured_app_id:
            logger.warning(
                "WizardOAuthService.get_meta_app: WIZARD_META_APP_ID is not "
                "configured — set fboauth.wizard_meta_app_id in master-config "
                "or the WIZARD_META_APP_ID env var"
            )
            return None

        es = get_elasticsearch_client()
        if es is None:
            logger.warning(
                "WizardOAuthService.get_meta_app: ES unreachable (configured app_id=%s)",
                configured_app_id,
            )
            return None
        try:
            # term: fb_app_id pins the lookup to the exact app the
            # operator configured. size=1 is belt-and-braces — there
            # should only ever be one doc per fb_app_id, but if
            # duplicate rows accreted from earlier testing we still
            # return one deterministic result (lowest _id by natural
            # sort).
            result = es.search(
                index="de_asset_fb_apps",
                body={
                    "size": 1,
                    "query": {"term": {"fb_app_id": configured_app_id}},
                },
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logger.error(
                "WizardOAuthService.get_meta_app: ES search failed for app_id=%s: %s",
                configured_app_id,
                e,
            )
            return None

        hits = (result.get("hits") or {}).get("hits") or []
        if not hits:
            logger.warning(
                "WizardOAuthService.get_meta_app: no de_asset_fb_apps row "
                "matches WIZARD_META_APP_ID=%s — register the app via the "
                "Meta Apps asset surface or update the config key",
                configured_app_id,
            )
            return None

        doc = hits[0]
        source = doc.get("_source") or {}
        client_id = (source.get("fb_app_id") or "").strip()
        encrypted_secret = source.get("fb_app_secret") or ""
        if not client_id or not encrypted_secret:
            logger.warning(
                "WizardOAuthService.get_meta_app: doc %r (configured app_id=%s) "
                "missing client_id or secret — wizard OAuth cannot start",
                doc.get("_id"),
                configured_app_id,
            )
            return None

        try:
            client_secret = decrypt(encrypted_secret)
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logger.error(
                "WizardOAuthService.get_meta_app: decrypt failed for doc %r " "(configured app_id=%s): %s",
                doc.get("_id"),
                configured_app_id,
                e,
            )
            return None

        return MetaAppCredentials(
            esid=doc.get("_id") or "",
            client_id=client_id,
            client_secret=client_secret,
        )

    # ── Callback side ───────────────────────────────────────────────────

    @staticmethod
    def exchange_code_for_granted_scopes(
        code: str,
        state_received: str,
        state_expected: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> ExchangeResult:
        """Validate state, exchange the auth code for a Meta access
        token, then call ``/debug_token`` to read the granted scopes.

        Returns an ``ExchangeResult`` with ``success=True`` and the
        normalised ``granted_scopes`` tuple on the happy path. The
        access token is NOT returned — discards it. Future
        tickets that need the token will add a separate method (or a
        flag on this one) so the discard default stays explicit.

        Failure modes (``success=False``):

        * ``no_code`` — Meta didn't send a code (rare; user closed
          the tab mid-redirect).
        * ``state_mismatch`` — the ``state`` we minted on the
          authorize-URL leg doesn't match what came back. Defends
          against CSRF on the callback.
        * ``token_exchange_failed`` — token endpoint returned non-200
          or a body without ``access_token``.
        * ``debug_token_failed`` — ``/debug_token`` returned non-200
          or a body without ``data.scopes``. Treated as a hard
          failure rather than "empty grant" because the user-side
          UX of "you granted nothing" is wrong when in fact we
          couldn't *read* what they granted.
        """
        if not code:
            return ExchangeResult(success=False, code=EXCHANGE_NO_CODE)

        # Constant-time-ish equality not strictly required (the token
        # is URL-safe random of 256 bits and a timing-side-channel on
        # a one-shot redirect is implausible), but using ``secrets``
        # keeps it cheap.
        if not state_received or not state_expected:
            return ExchangeResult(success=False, code=EXCHANGE_STATE_MISMATCH)
        if not secrets.compare_digest(state_received, state_expected):
            return ExchangeResult(success=False, code=EXCHANGE_STATE_MISMATCH)

        # Step 1: code → access_token
        token_url = f"{FB_GRAPH_URL}/oauth/access_token"
        try:
            token_resp = requests.get(
                token_url,
                params={
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "client_secret": client_secret,
                    "code": code,
                },
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            # nosemgrep: python-logger-credential-disclosure - logs the requests exception (network error), not the OAuth token; "token" in the format string is the endpoint name
            logger.error("WizardOAuthService.exchange: token GET failed: %s", e)
            return ExchangeResult(success=False, code=EXCHANGE_TOKEN_FAILED)

        if token_resp.status_code != 200:
            # nosemgrep: python-logger-credential-disclosure - logs the HTTP status code (integer), not the response body or token
            logger.warning(
                "WizardOAuthService.exchange: token endpoint returned %s",
                token_resp.status_code,
            )
            return ExchangeResult(success=False, code=EXCHANGE_TOKEN_FAILED)

        try:
            token_body = token_resp.json() or {}
        except Exception as e:  # noqa: BLE001
            # nosemgrep: python-logger-credential-disclosure - logs the JSON-decode exception (parser error), not the response body
            logger.error("WizardOAuthService.exchange: token body not JSON: %s", e)
            return ExchangeResult(success=False, code=EXCHANGE_TOKEN_FAILED)

        access_token = token_body.get("access_token") or ""
        if not access_token:
            return ExchangeResult(success=False, code=EXCHANGE_TOKEN_FAILED)

        # Step 2: access_token → /debug_token → granted scopes.
        # App access token = {app_id}|{app_secret}; same construction
        # fb_oauth_server uses on its callback.
        debug_url = "https://graph.facebook.com/debug_token"
        app_token = f"{client_id}|{client_secret}"
        try:
            debug_resp = requests.get(
                debug_url,
                params={
                    "input_token": access_token,
                    "access_token": app_token,
                },
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            # nosemgrep: python-logger-credential-disclosure - logs the requests exception (network error), not the access_token/app_token; "debug_token" is the FB endpoint name
            logger.error("WizardOAuthService.exchange: debug_token GET failed: %s", e)
            return ExchangeResult(success=False, code=EXCHANGE_DEBUG_TOKEN_FAILED)

        if debug_resp.status_code != 200:
            # nosemgrep: python-logger-credential-disclosure - logs the HTTP status code (integer), not the response body
            logger.warning(
                "WizardOAuthService.exchange: debug_token returned %s",
                debug_resp.status_code,
            )
            return ExchangeResult(success=False, code=EXCHANGE_DEBUG_TOKEN_FAILED)

        try:
            debug_body = debug_resp.json() or {}
        except Exception as e:  # noqa: BLE001
            # nosemgrep: python-logger-credential-disclosure - logs the JSON-decode exception (parser error), not the response body
            logger.error("WizardOAuthService.exchange: debug_token body not JSON: %s", e)
            return ExchangeResult(success=False, code=EXCHANGE_DEBUG_TOKEN_FAILED)

        data = debug_body.get("data") or {}
        raw_scopes = data.get("scopes")
        if not isinstance(raw_scopes, list):
            # ``data.scopes`` missing or wrong type — treat as a debug-token
            # failure rather than as "zero granted" so the error UX is
            # honest about what went wrong.
            logger.warning("WizardOAuthService.exchange: debug_token missing data.scopes")
            return ExchangeResult(success=False, code=EXCHANGE_DEBUG_TOKEN_FAILED)

        # Normalise: lower-case, strip, drop empties, de-dup, sort.
        normalised: set[str] = set()
        for raw in raw_scopes:
            if not isinstance(raw, str):
                continue
            cleaned = raw.strip().lower()
            if cleaned:
                normalised.add(cleaned)

        # follow-up — surface the Meta /me user_id + the
        # token's debug-token-reported expires_at to the caller. The
        # user_id is the load-bearing natural-key disambiguator for
        # ``de_connection`` (a single principal can hold multiple
        # connections to the same provider under different Meta accounts).
        # expires_at on debug_token is a unix timestamp; we convert to
        # ISO-8601 for consistency with the rest of the connection
        # schema. A missing/zero value (long-lived tokens without an
        # expiry surface as 0) stays None so the index field is null.
        provider_account_id = str(data.get("user_id") or "").strip()
        token_expires_at = _epoch_to_iso(data.get("expires_at"))
        # the clock the credential-health probe counts down to.
        # Until this was read at callback time, every wizard row had no
        # data_access_expires_at and the probe had nothing to warn about
        # (prod 2026-09-07: 15 rows, all null, ~2026-11-02 cliff).
        data_access_expires_at = _epoch_to_iso(data.get("data_access_expires_at"))

        return ExchangeResult(
            success=True,
            code=EXCHANGE_OK,
            granted_scopes=tuple(sorted(normalised)),
            access_token=access_token,
            provider_account_id=provider_account_id,
            token_expires_at=token_expires_at,
            data_access_expires_at=data_access_expires_at,
        )
