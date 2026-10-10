"""
Flask Authentication Middleware
Handles authentication and authorization for the Asset Web Manager.
"""

import base64
import secrets
import time
import logging
from datetime import datetime, timedelta
from flask import request, session, abort
from collections import defaultdict
from urllib.parse import quote

# Rate limiting storage (in-memory)
# the lockout store is LoginLockoutService (shared ES index, username
# AND IP thresholds). ``failed_attempts`` is kept as a name because tests and
# the degraded-mode path use it — it IS the service's in-process fallback dict,
# the same object, so seeding or clearing it here still means what it did.
try:
    from web.services.login_lockout_service import LoginLockoutService
except ImportError:
    from .services.login_lockout_service import LoginLockoutService

failed_attempts = LoginLockoutService.local_username_failures

# Documented attack-surface personas usable via the X-Test-Persona header-auth
# path (see the TEST_AUTH_SECRET branch in verify_auth and
# the private docs). Only these persona
# keys can be assumed; an unknown X-Test-Persona value is ignored. Roles mirror
# the personas doc — only `superuser` carries an admin role; the rest
# authenticate as plain users for scope/authz testing. Usernames are built at
# lookup time by lib.test_personas: ``test-<slug>@<TEST_PERSONA_DOMAIN>``,
# the same helper the seed tool uses, so login and seeding agree and the tree
# carries no deployment hostname.
_TEST_PERSONA_ROLES = {
    "superuser": ["superuser"],
    "workspace_admin": [],
    "workspace_member": [],
    "workspace_grant": [],
    "org_user": [],
    "org_grant": [],
    "team_user": [],
    "team_grant": [],
    "grant_user": [],
    "bare": [],
}


def _test_persona(persona_key):
    """Resolve an ``X-Test-Persona`` value to ``{"username", "roles"}``, or None."""
    roles = _TEST_PERSONA_ROLES.get(persona_key)
    if roles is None:
        return None
    from lib.test_personas import persona_username

    return {"username": persona_username(persona_key), "roles": list(roles)}


# follow-up — per-username ES Security account-state cache. Used
# by verify_auth (and session_denial_reason) to force-logout a
# session whose backing user has been deleted (via User Management OR
# direct Kibana / ES delete), suspended, or,,
# had its credentials changed after the session authenticated. Since
# the same record also carries the account's current roles, so
# a live session's session["roles"] / session["is_admin"] can be brought
# in line with ES Security (see refresh_session_roles) without a second
# round-trip. The cache holds {username: (state, credential_changed_epoch:
# float | None, roles: list[str] | None, checked_at: float)} with state
# one of _USER_STATES. TTL is short so a stale-cache window doesn't let a
# deleted or suspended user keep making requests; long enough that we
# don't hit ES on every request.
_USER_EXISTS_CACHE: dict[str, tuple[str, float | None, list[str] | None, float]] = {}
_USER_EXISTS_CACHE_TTL_SECONDS = 30.0
#: ``active`` — exists and enabled; ``disabled`` — exists, ``enabled=False``
#: (suspended); ``deleted`` — no such ES Security user.
_USER_STATES = ("active", "disabled", "deleted")


def _parse_credential_changed_at(username: str, record: dict) -> float | None:
    """epoch seconds for ``record``'s ``credential_changed_at``
    metadata, or ``None`` when the key is absent or unparsable.

    Fail-soft and quiet on the common case (most users have never had a
     writer touch them, so the key is simply absent): only an
    unparsable non-empty value logs, since that indicates a real data
    problem rather than "this ticket hasn't stamped this user yet".
    """
    raw = (record.get("metadata") or {}).get("credential_changed_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError) as e:
        logging.warning("could not parse credential_changed_at for %r: %r (%s)", username, raw, e)
        return None


def _user_account_record(username: str) -> tuple[str, float | None, list[str] | None]:
    """what ES Security currently
    says about ``username``: ``(state, credential_changed_epoch, roles)``
    where state is one of ``"active"``, ``"disabled"`` or ``"deleted"``; the
    second element is the epoch of the account's ``credential_changed_at``
    metadata stamp, or ``None`` if it was never stamped or fails to parse;
    and the third element is the account's current ES Security roles as a
    plain ``list[str]``, or ``None`` when they cannot be resolved (deleted /
    ES-unreachable / any exception path) — ``None`` means "unknown", never
    "no roles", so callers must not treat it as an empty list.

    One ``es.security.get_user(username)`` call answers the
    existence question, the suspension question, the
    credential-change question, and the account's live roles —
    the record carries ``enabled``, ``metadata`` and ``roles`` — so adding
    the fourth check costs no extra round-trip. Cached with a 30-second TTL
    so the typical authenticated request is one in-process dict lookup.
    ``_user_still_exists``, ``_user_is_disabled``, ``_credential_changed_at``
    and ``_account_roles`` are the views callers use; ``_user_account_state``
    is a thin view for the state alone.

    Fail-soft: any unexpected exception (ES outage, connectivity blip,
    parse failure) returns ``("active", None, None)``. The alternative —
    failing closed on a transient blip — would log every authenticated user
    out of staging every time the ES container hiccups, which is worse than
    a brief window where a freshly-deleted, freshly-suspended, or
    freshly-credential-changed user can still make requests. A 404 IS a
    definite answer and is cached (with ``roles=None`` — a deleted user has
    no roles to report).

    Catches every surface that writes the account, not just ours:
      * /api/user-management/delete-user, suspend-user, bulk-action,
        update-roles
      * Kibana / direct ES ``DELETE _security/user/<username>`` and
        ``PUT _security/user/<username>/_disable``
      * reset_password / admin_set_password / the standalone password-reset
        app, which stamp ``credential_changed_at``

    The 30s TTL means the worst case is a deleted, suspended, or
    credential-changed user can make up to ~30s of requests before their
    session is killed — unless the write went through our own endpoints,
    which flush the entry (``_invalidate_user_exists_cache``). The cache is
    per process and DEAM runs two gunicorn workers, so that flush only
    reaches the worker that served the admin's request; the other keeps
    its entry to the TTL. The guarantee is "denied within 30s", not "the
    very next request". The same bound applies to role refresh —
    see ``refresh_session_roles``.
    """
    if not username:
        return "deleted", None, None
    now = time.time()
    cached = _USER_EXISTS_CACHE.get(username)
    if cached is not None:
        state, changed_at, roles, checked_at = cached
        if now - checked_at < _USER_EXISTS_CACHE_TTL_SECONDS:
            return state, changed_at, roles
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            # ES unreachable — fail-soft (don't log everyone out on a blip).
            return "active", None, None
        record = dict(es.security.get_user(username=username)).get(username)
        if not record:
            state = "deleted"
            changed_at = None
            roles = None
        else:
            state = "disabled" if record.get("enabled", True) is False else "active"
            changed_at = _parse_credential_changed_at(username, record)
            roles = [str(r) for r in record["roles"]] if isinstance(record.get("roles"), list) else None
    except Exception as e:  # noqa: BLE001 — top-level boundary, fail-soft
        # 404 / not_found / any other error path — fail-soft. The
        # cache is NOT populated so the next request retries the ES
        # call rather than holding a misleading answer for 30s.
        err = str(e).lower()
        if "not_found" in err or "404" in err:
            _USER_EXISTS_CACHE[username] = ("deleted", None, None, now)
            return "deleted", None, None
        logging.warning("_user_account_record raised for %r: %s", username, e)
        return "active", None, None
    _USER_EXISTS_CACHE[username] = (state, changed_at, roles, now)
    return state, changed_at, roles


def _user_account_state(username: str) -> str:
    """what ES Security currently says about
    ``username``: ``"active"``, ``"disabled"`` or ``"deleted"``. A thin
    view over ``_user_account_record``; see it for the cache and
    fail-soft contract."""
    return _user_account_record(username)[0]


def _credential_changed_at(username: str) -> float | None:
    """epoch of ``username``'s last ``credential_changed_at``
    stamp, or ``None`` if it was never stamped or fails to parse. A view
    over ``_user_account_record``; see it for the cache and fail-soft
    contract."""
    return _user_account_record(username)[1]


def _account_roles(username: str) -> list[str] | None:
    """``username``'s current ES Security roles, or ``None``
    when they cannot be resolved (deleted / ES-unreachable / any
    exception path). A view over ``_user_account_record``; see it for the
    cache and fail-soft contract. ``None`` means "unknown" — callers must
    never treat it as "no roles"."""
    return _user_account_record(username)[2]


def _user_still_exists(username: str) -> bool:
    """follow-up — does ``username`` still exist as an ES
    Security user? A view over ``_user_account_state``; see it for the
    cache and fail-soft contract."""
    return _user_account_state(username) != "deleted"


def _user_is_disabled(username: str) -> bool:
    """is ``username`` suspended (``enabled=False``) in ES
    Security? A view over ``_user_account_state``; a deleted user is
    reported by ``_user_still_exists`` and answers False here."""
    return _user_account_state(username) == "disabled"


def _resolve_es_roles(username: str) -> list[str] | None:
    """roles for ``username`` straight from ES Security, or
    ``None`` when they cannot be resolved.

    Used by the reverse-proxy header-SSO branch in ``verify_auth`` so the
    session's roles come from the role store, never from the
    ``X-Remote-Roles`` request header (which the client controls).

    Fail-closed, unlike ``_user_still_exists``: that helper guards
    *continuation* of an already-authenticated session, where failing on an
    ES blip would log every active user out. This one *grants* a brand-new
    session — granting with unverifiable roles is an authentication
    decision made blind, so any lookup failure returns ``None`` and the
    caller denies.
    """
    if not username:
        return None
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return None
        record = dict(es.security.get_user(username=username)).get(username)
        if not record:
            return None
        return [str(r) for r in record.get("roles", [])]
    except Exception as e:  # noqa: BLE001 — top-level boundary, fail-closed
        logging.warning("_resolve_es_roles failed for %r: %s", username, e)
        return None


def _invalidate_user_exists_cache(username: str | None = None) -> None:
    """follow-up — purge an entry from the account-state cache.

    Called from the User Management delete endpoint right after the
    ES Security delete succeeds, and from
    ``UserManagementService.suspend_user`` / ``reinstate_user`` right
    after the enabled flag flips, so the affected user's next request
    to THIS process re-checks ES instead of waiting up to TTL seconds
    for the cached state to expire. Other gunicorn workers keep their
    own entries until the TTL (see ``_user_account_state``).

    Direct Kibana / ES writes bypass this call (no application-side
    hook), so they rely on the natural TTL expiry — still bounded by
    _USER_EXISTS_CACHE_TTL_SECONDS.

    With ``username=None`` clears the whole cache (used by tests).
    """
    if username is None:
        _USER_EXISTS_CACHE.clear()
        return
    _USER_EXISTS_CACHE.pop(username, None)


def refresh_session_roles(username: str) -> bool:
    """bring session["roles"] / session["is_admin"] in line with
    ES Security. Returns True when the session was rewritten.

    Before this, an admin role change (``/api/user-management/update-roles``)
    only reached a live session at its next login — ``session["roles"]`` was
    written once at login and never revisited, so a promoted or demoted user
    kept their OLD privileges (in either direction) in every tab until they
    signed out. This closes that gap by re-checking the account's current
    roles against the session on every request that reaches ``verify_auth``
    or the Kibana gate route, riding the SAME cached ``_user_account_record``
    the checks already pay for — no extra ES
    round-trip, and the same 30s bound: "reflected within 30s", not "the
    very next request" (a stale worker's cache entry, or one not yet
    refreshed by ``UserManagementService.update_user_roles``'s cache flush).

    Deliberately conservative: a session with no ``roles`` key at all, or
    whose roles cannot currently be resolved (``_account_roles`` returns
    ``None`` — ES unreachable, or the account was deleted, which the
     check ahead of this in ``verify_auth`` already handles), is left
    untouched rather than guessed at. Flask only re-signs the session
    cookie when the session is modified, so a no-op call (the common case —
    most requests see no role change) costs nothing beyond the dict/cache
    lookup ``_account_roles`` already does.
    """
    if "roles" not in session:
        return False
    es_roles = _account_roles(username)
    if es_roles is None:
        return False
    old_roles = session.get("roles") or []
    if sorted(set(es_roles)) == sorted(set(old_roles)):
        return False
    from .config import config

    logging.info("role refresh: user=%r roles %s -> %s", username, old_roles, es_roles)
    session["roles"] = list(es_roles)
    session["is_admin"] = "superuser" in es_roles or config.REQUIRED_ROLE in es_roles
    return True


def user_is_workspace_owner(username: str, roles: list | None = None) -> bool:
    """True iff the user is a **superuser** or holds the **workspace_owner**
    grant — NOT org-tier admins, NOT workspace_admin.

    ``superuser`` is an ES Security role carried on the session.
    ``workspace_owner`` is held as a System 2 grant
    (``de_authz_user_workspace_role``), NOT an ES Security role, so
    wizard-signup owners have ``session["roles"] == []`` and must be
    detected via the grant index.

    Canonical home for every owner-only gate: Billing (, via the
    ``user_can_access_billing`` alias) and Echo import kickoff (—
    Echo pulls are tenant-wide, so kickoff is owner-only).

    Best-effort; an ES outage / missing index returns False (fail-closed).
    """
    if "superuser" in (roles or []):
        return True
    if not username:
        return False
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return False
    try:
        resp = es.search(
            index="de_authz_user_workspace_role",
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"username": username}},
                            {"term": {"role_esid": "workspace_owner"}},
                        ]
                    }
                },
                "size": 1,
                "_source": False,
            },
        )
    except Exception as e:  # noqa: BLE001 — best-effort gate, fail-closed
        err = str(e).lower()
        if "index_not_found" in err or "404" in err:
            return False
        logging.warning("user_is_workspace_owner lookup failed for %r: %s", username, e)
        return False
    return bool(resp.get("hits", {}).get("hits", []))


def _user_is_org_owner(username: str) -> bool:
    """True iff the user holds the org-tier **owner** grant
    (``de_authz_user_org_role``, role_esid ``owner`` — the seeded
    ROLE_OWNER, applies_to "org"; written by the signup wizard's
    three-tier fan-out and the Edit Roles UI).

    Org-tier ONLY — the team-tier index also carries ``owner`` docs and
    must not be consulted here (a team owner is not an org owner).

    Best-effort; an ES outage / missing index returns False (fail-closed).
    """
    if not username:
        return False
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return False
    try:
        resp = es.search(
            index="de_authz_user_org_role",
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"username": username}},
                            {"term": {"role_esid": "owner"}},
                        ]
                    }
                },
                "size": 1,
                "_source": False,
            },
        )
    except Exception as e:  # noqa: BLE001 — best-effort gate, fail-closed
        err = str(e).lower()
        if "index_not_found" in err or "404" in err:
            return False
        logging.warning("_user_is_org_owner lookup failed for %r: %s", username, e)
        return False
    return bool(resp.get("hits", {}).get("hits", []))


def user_can_access_billing(username: str, roles: list | None = None) -> bool:
    """(follow-up): True iff the user may see + reach the Billing
    section. Kept as a named alias so the nav-button gate (app.py context
    processor) and the route gate (analytics billing routes) keep reading
    as Billing intent.

    billing is org-scoped, so the org-tier ``owner`` grant also
    qualifies — Billing = workspace owner OR org owner. Echo import
    kickoff deliberately stays on the stricter
    ``user_is_workspace_owner`` and is NOT widened by this."""
    if user_is_workspace_owner(username, roles):
        return True
    return _user_is_org_owner(username)


def init_auth(app):
    """
    Initialize authentication system with Flask app.

    Args:
        app: Flask application instance
    """
    # Configure Flask session settings for security
    app.config["SESSION_COOKIE_SECURE"] = True  # HTTPS only
    app.config["SESSION_COOKIE_HTTPONLY"] = True  # XSS protection
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # CSRF protection
    app.config["SESSION_COOKIE_PATH"] = "/"  # Cookie valid for all paths (both /assets/ and /dashboards)

    # Session lifetime from config. The Flask cookie must outlive whichever
    # of (idle, absolute) is longer — otherwise the cookie expires before
    # the server-side check fires and the user is logged out for the wrong
    # reason. SESSION_LIFETIME is the idle timeout; SESSION_MAX_LIFETIME is
    # the absolute cap (mirrors Kibana's xpack.security.session.lifespan).
    from .config import config

    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=max(config.SESSION_LIFETIME, config.SESSION_MAX_LIFETIME))

    logging.info("Authentication system initialized")


def generate_csrf_token():
    """Generate CSRF token and store in session."""
    if "csrf_token" not in session:
        import secrets

        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def refresh_csrf_token():
    """Return existing CSRF token or generate one if absent.

    We no longer force-rotate on every GET /login because rotation means
    whichever tab loads the login page last invalidates all other tabs'
    forms. SameSite=Lax on the session cookie already mitigates login-CSRF,
    so a stable per-session token is sufficient.
    """
    if "csrf_token" not in session:
        import secrets

        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def validate_csrf_token(token):
    """Validate CSRF token from form submission.

    Uses a constant-time comparison (secrets.compare_digest) rather than ``==``
    so the per-request match cannot be probed via a timing side-channel — the
    same posture password_reset already uses. Returns a plain bool; an empty
    submitted token or absent session token is always rejected.
    """
    import secrets

    expected = session.get("csrf_token")
    if not token or not expected:
        return False
    return secrets.compare_digest(str(expected), str(token))


# Endpoints that are exempt from global CSRF enforcement because they
# either handle CSRF validation themselves (login/register forms) or
# are inherently safe (GET-only, static, OAuth callbacks).
_CSRF_EXEMPT_ENDPOINTS = frozenset(
    {
        "static",
        "serve_static",
        "analytics.login_page",
        "analytics.login_post",  # validates csrf_token from form
        "analytics.dashboards_login_redirect",  # 301/308 to /login, no body processed
        "analytics.logout",
        "analytics.signup_page",
        "analytics.signup_post",  # validates csrf_token from form
        "analytics.signup_success_page",
        "analytics.verify_email",
        "analytics.onboarding_start_page",
        "analytics.onboarding_start_post",  # validates csrf_token from form
        "analytics.onboarding_invite_expired_page",  # anonymous GET landing
        "analytics.onboarding_invite_team_picker_page",  # validates csrf_token on POST
        "analytics.onboarding_resume_restart",  # validates csrf_token from form
        "analytics.onboarding_join_prompt_page",  # GET-only, candidate stashed in session
        "analytics.onboarding_join_prompt_post",  # validates csrf_token from form
        # Phase 1 (2026-05-20) — verify-email-step is logical
        # step 2 of 8. POST handlers validate csrf_token in-route.
        "analytics.onboarding_verify_email_step_page",
        "analytics.onboarding_verify_email_step_post",
        "analytics.onboarding_verify_email_step_resend",
        # follow-up (2026-05-21) — change-email lets a user
        # who typo'd their address on signup fix it inline. POST
        # validates csrf_token in-route.
        "analytics.onboarding_verify_email_step_change_email",
        # Item 4 (2026-05-20) — GET-only JSON status probe for
        # the Continue-button JS poll. Session-authenticated; no CSRF
        # surface to gate.
        "analytics.onboarding_verify_email_step_status",
        # Phase 2 (2026-05-20) — mfa-setup-step is logical
        # step 3 of 8. POST validates csrf_token in-route + dispatches
        # on action (choose_totp / choose_yubikey / confirm_totp /
        # register_yubikey / continue).
        "analytics.onboarding_mfa_setup_step_page",
        "analytics.onboarding_mfa_setup_step_post",
        "analytics.onboarding_step4_page",
        "analytics.onboarding_step4_post",  # validates csrf_token from form
        "analytics.onboarding_step5_page",
        "analytics.onboarding_step5_post",  # validates csrf_token from form
        "analytics.onboarding_step5_slug_check",  # GET-only JSON probe, session-authenticated
        "analytics.onboarding_step6_page",
        "analytics.onboarding_step6_post",  # validates csrf_token from form
        "analytics.onboarding_step7_page",
        "analytics.onboarding_step7_post",  # validates csrf_token from form
        "analytics.onboarding_step7_start_oauth",  # GET-only, state stored on wizard_session
        "analytics.onboarding_step7_oauth_callback",  # GET-only, state validated from wizard_session
        "analytics.onboarding_step8_page",
        "analytics.onboarding_step8_post",  # validates csrf_token from form
        # beacon endpoint. The browser-side sendBeacon
        # API can't set custom headers, so the global X-CSRF-Token /
        # form-csrf gate would block every navigator.sendBeacon call.
        # Exempt at the global hook; validate the JSON-body csrf_token
        # in-route so beacon and fetch-keepalive callers both reach
        # the same gate.
        "analytics.dashboard_checklist_track_click",
        "analytics.onboarding_google_start",  # GET-only, state stored in session
        "analytics.onboarding_google_callback",  # GET-only, state validated
        "analytics.onboarding_microsoft_start",  # GET-only, state stored in session
        "analytics.onboarding_microsoft_callback",  # GET-only, state validated
        "analytics.onboarding_verify_email",  # GET-only, JWT is the unforgeable token
        "change_password_page",
        "change_password_post",  # validates csrf_token from form
        "mfa_challenge_page",
        "mfa_challenge_post",  # validates csrf_token from form
        # Tier 1 recovery-code challenge + one-time display (pre-auth,
        # mfa_pending session). Each POST validates csrf_token from the form.
        "mfa_challenge_recovery_page",
        "mfa_challenge_recovery_post",
        "mfa_recovery_codes_page",
        "mfa_recovery_codes_continue",
        # Tier 2 — email recovery (pre-auth, mfa_pending session or
        # token-only). The POST validates csrf_token from the form; the GET
        # consume links carry an unforgeable signed token instead of CSRF.
        "mfa_challenge_email_page",
        "mfa_challenge_email_post",
        "mfa_recovery_email_consume",
        "mfa_setup_page",
        "mfa_setup_totp_generate",
        "mfa_setup_totp_confirm",  # validates csrf_token from form
        "mfa_setup_yubikey_register",  # validates csrf_token from form
        # follow-up: Stripe posts the webhook server-to-server with
        # no session/CSRF token; authenticity is the Stripe-Signature HMAC,
        # verified in-handler against STRIPE_WEBHOOK_SECRET.
        "analytics.billing_stripe_webhook",
    }
)


def check_csrf_for_state_change():
    """Before-request handler that enforces CSRF on state-changing methods.

    Accepts the token from:
      - ``X-CSRF-Token`` request header  (JavaScript ``fetch`` calls)
      - ``csrf_token`` form field        (traditional form POSTs)

    Returns ``None`` to continue, or a 403 JSON response on failure.
    """
    # Only enforce on state-changing HTTP methods
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None

    # Skip static assets
    if request.path.startswith("/static/"):
        return None

    # Skip public (unauthenticated) report routes —
    if request.path.startswith("/public/"):
        return None

    # Skip endpoints that manage their own CSRF validation
    endpoint = request.endpoint or ""
    if endpoint in _CSRF_EXEMPT_ENDPOINTS:
        return None

    # Accept token from header (AJAX) or form field (HTML form)
    token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")

    if not validate_csrf_token(token):
        logging.warning(
            f"AUDIT: CSRF rejected method={request.method} path={request.path} "
            f"endpoint={endpoint} ip={request.remote_addr} "
            f"header_present={bool(request.headers.get('X-CSRF-Token'))} "
            f"form_present={bool(request.form.get('csrf_token'))}"
        )
        from flask import jsonify as _jsonify

        return _jsonify({"error": "CSRF validation failed"}), 403

    return None


def _wants_json_response() -> bool:
    """True iff the current request is an AJAX/API call that
    expects JSON rather than a full HTML page.

    The session-expiry handlers (idle/absolute timeout, missing-auth)
    historically returned a ``302 -> /login`` for *every* request. For a
    full-page navigation that is correct — the browser follows it and the
    login form renders. But ``fetch`` transparently follows the 302, so an
    AJAX caller's ``response.json()`` then tries to parse the login HTML and
    throws a SyntaxError — surfacing a cryptic error instead of a clean
    redirect. For those callers we return ``401`` + a JSON body the
    client-side fetch guard (session-timeout.js) acts on by navigating to
    the login page itself.

    A request is treated as JSON-expecting when ANY of:
      * the path is under a known API prefix
        (``/api/``, ``/dashboard-builder/api/``);
      * ``X-Requested-With: XMLHttpRequest`` is present (classic AJAX);
      * an explicit ``X-CSRF-Token`` header is present (our fetch wrapper
        sets it on state-changing fetches — a strong signal of a fetch
        caller rather than a top-level navigation);
      * ``Accept`` prefers ``application/json`` over ``text/html``.
    """
    path = request.path or ""
    if path.startswith("/api/") or path.startswith("/dashboard-builder/api/"):
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    if request.headers.get("X-CSRF-Token"):
        return True
    accept = request.headers.get("Accept", "")
    # Prefer JSON only when it is explicitly accepted AND html is not the
    # primary type — a normal navigation sends ``text/html,...`` first.
    if "application/json" in accept and "text/html" not in accept:
        return True
    return False


# endpoints the dashboard polls automatically on a wall-clock
# interval, with no user interaction. These must NOT refresh the session's
# idle timer (last_activity), or an open-but-idle tab never idles out. Auth
# is still fully enforced on them — they just don't count as "activity".
# Matched as path prefixes (covers query strings). Fail-closed: anything not
# listed still counts as activity, so interactive requests never regress.
_BACKGROUND_POLL_PATHS = (
    "/api/maintenance-alert",
    "/onboarding/api/backfill-progress",
)

# minimum seconds between presence-heartbeat writes per user, so we
# don't upsert de_user_presence on every authenticated request. The presence
# window (SESSION_LIFETIME) is far larger than this, so a 60s heartbeat is
# always fresh enough to keep an active user inside the window.
_HEARTBEAT_INTERVAL_S = 60


def _is_background_poll() -> bool:
    """True iff the current request is an automatic background
    poll that should not extend the idle session window. A client can also
    opt in explicitly with an ``X-Background-Poll`` header (so future
    pollers can self-declare without editing this allowlist)."""
    if request.headers.get("X-Background-Poll"):
        return True
    path = request.path or ""
    return any(path.startswith(p) for p in _BACKGROUND_POLL_PATHS)


def _session_expired_response(login_target: str):
    """build the right "session expired / not authenticated"
    response for the caller: a ``401`` + JSON for AJAX/API callers (so the
    client fetch guard can redirect cleanly), or a ``302`` to ``login_target``
    for a full-page navigation (unchanged legacy behavior).

    For an AJAX/API caller the ``login_target``'s ``next=`` was built from
    the *failed request's* path — which is an API/POST endpoint (e.g.
    ``/dashboard-builder/api/dashboards/<id>/publish``), NOT a page a user
    can land on. Honoring that ``next=`` after login would navigate the user
    straight to a bare API URL. So for the JSON branch we drop the API
    ``next=`` and send the user to a safe landing page instead. The full-page
    branch keeps its ``next=`` (there the path IS a real page the user asked
    for).
    """
    from flask import redirect, jsonify

    if _wants_json_response():
        # The fetch guard navigates the top window to login_url. The expired
        # request was a fetch to an API path, so any next= here points at an
        # un-landable endpoint — strip it to a safe default.
        safe_login_url = "/login?next=%2F"  # land on home after re-login
        resp = jsonify({"error": "session_expired", "login_url": safe_login_url})
        return resp, 401
    return redirect(login_target)


def _session_ages(now: float) -> tuple[float, float]:
    """timers for the current session: ``(idle_age, absolute_age)``
    in seconds. ``last_activity`` falls back to ``auth_timestamp`` for
    sessions written. Caller guarantees ``auth_timestamp``.
    """
    last_activity = session.get("last_activity", session["auth_timestamp"])
    return now - last_activity, now - session["auth_timestamp"]


def _session_timer_fired(idle_age: float, absolute_age: float) -> str | None:
    """Which timer has expired: ``"idle"`` (>= SESSION_LIFETIME),
    ``"absolute"`` (>= SESSION_MAX_LIFETIME), or ``None`` while both are
    inside their windows. Single home for the decision — ``verify_auth``
    and ``session_denial_reason`` both call it.
    """
    from .config import config

    if idle_age >= config.SESSION_LIFETIME:
        return "idle"
    if absolute_age >= config.SESSION_MAX_LIFETIME:
        return "absolute"
    return None


def session_denial_reason(now: float | None = None) -> str | None:
    """is the current session still one ``verify_auth`` would
    accept? ``None`` when it is; otherwise a short reason.

    ``verify_auth`` skips ``/api/internal/*`` by design, so the
    Kibana gate used to re-derive only two of its checks (authenticated,
    superuser) and missed the idle/absolute timers and the
    deleted-user revocation: an idle-expired or de-provisioned superuser
    kept opening Kibana until the signed cookie itself aged out (up to
    ``PERMANENT_SESSION_LIFETIME``). Surfaces that ``verify_auth`` does not
    cover call this instead of copying its logic.

    Reasons: ``unauthenticated``, ``no_timestamp`` (a session without
    ``auth_timestamp`` is not one login wrote — deny), ``idle``,
    ``absolute``, ``user_deleted``, ``user_disabled`` (the
    account was suspended after login — until then a suspended user
    kept every DEAM surface until the timers fired), and
    ``credential_changed`` (the account's password/credential
    was changed by an admin reset, admin set-password, or self-service
    change AFTER this session authenticated — a stale ``auth_timestamp``
    is the whole signal, since resetting a password because an account
    may be compromised must not leave the old session alive). Read-only:
    never clears or writes the session (nginx auth_request discards the
    subrequest's Set-Cookie anyway). Inherits the account-state cache's
    fail-soft 30 s window, the same trade-off every other route accepts.
    """
    if not session.get("authenticated"):
        return "unauthenticated"
    if not session.get("auth_timestamp"):
        return "no_timestamp"
    fired = _session_timer_fired(*_session_ages(time.time() if now is None else now))
    if fired is not None:
        return fired
    username = session.get("username", "")
    if username and not _user_still_exists(username):
        return "user_deleted"
    if username and _user_is_disabled(username):
        return "user_disabled"
    if username:
        changed_at = _credential_changed_at(username)
        if changed_at is not None and float(session["auth_timestamp"]) < changed_at:
            return "credential_changed"
    return None


def verify_auth():
    """
    Before-request handler that validates authentication.
    Called on every request before route handlers.

    Returns:
        None if authentication successful
        Flask abort response if authentication fails (triggers WWW-Authenticate)
    """
    # Skip static files
    if request.path.startswith("/static/"):
        return None

    # Skip public (unauthenticated) report routes —.
    # Each handler in the public_report blueprint validates its own token.
    if request.path.startswith("/public/"):
        return None

    # nginx auth_request backend. The /api/internal/* surface is
    # the auth backend for the /kibana gate — verify_auth must NOT
    # intercept it, because the gate route itself is what decides
    # 204 / 401 / 403 based on session contents. If verify_auth ran first
    # an anonymous request would 302 to /login, and nginx auth_request
    # would see a 302 (not a 401) and deny in a way nginx error_page
    # cannot recover with a login redirect. The route does its own
    # session check.
    if request.path.startswith("/api/internal/"):
        return None

    # Import here to avoid circular imports
    from .config import config

    # Check for a forced password change session (user must change password before accessing the app)
    if session.get("fpc_pending"):
        # Enforce a 15-minute timeout on the partial session
        if time.time() - session.get("fpc_timestamp", 0) > 900:
            session.clear()
            from flask import redirect, url_for

            return redirect(url_for("analytics.login_page", error="Session expired. Please log in again."))

        # Allow only the change-password endpoints, logout, and login pages
        # (so a user stuck in an fpc session can navigate back to sign in as
        # someone else). The login_page handler clears any fpc_pending state.
        fpc_whitelist = {
            "change_password_page",
            "change_password_post",
            "analytics.logout",
            "analytics.login_page",
            "analytics.login_post",
            "analytics.dashboards_login_redirect",
            "static",
            "serve_static",
        }
        if request.endpoint not in fpc_whitelist:
            from flask import redirect

            # /change-password is part of the sign-in surface
            # and lives at the bare path (matching /login, /mfa/*).
            return redirect("/change-password")
        return None

    # Check for a pending MFA session (user passed password but hasn't done MFA yet)
    if session.get("mfa_pending"):
        # Enforce a 5-minute timeout on the partial session
        if time.time() - session.get("mfa_pending_timestamp", 0) > 300:
            session.clear()
            from flask import redirect, url_for

            return redirect(url_for("analytics.login_page", error="MFA session expired. Please log in again."))

        # Allow MFA-related endpoints, logout, and login pages through; redirect
        # everything else. Login pages are whitelisted so a user in the middle
        # of an MFA flow can navigate back to sign in as someone else; the
        # login_page handler clears any mfa_pending state.
        mfa_whitelist = {
            "mfa_challenge_page",
            "mfa_challenge_post",
            # recovery-code challenge + one-time display are part of
            # the pre-auth MFA surface — a user mid-challenge must reach them.
            "mfa_challenge_recovery_page",
            "mfa_challenge_recovery_post",
            "mfa_recovery_codes_page",
            "mfa_recovery_codes_continue",
            # Tier 2 — send a recovery link to email while mid-challenge.
            "mfa_challenge_email_page",
            "mfa_challenge_email_post",
            # Tier 2 — the recovery-link + verify-link are clicked in the
            # SAME browser that's still mid-challenge (mfa_pending is live), so
            # they must pass this gate. The consume route clears mfa_pending and
            # establishes a fresh authed session; without this entry the gate
            # bounces the click to /mfa/challenge before the route ever runs.
            "mfa_recovery_email_consume",
            "profile_mfa_recovery_email_verify",
            # external report-recipient confirmation link (signed token; clicked from an inbox).
            "report_scheduler.report_recipient_verify",
            "mfa_setup_page",
            "mfa_setup_choose",
            "mfa_setup_choose_post",
            "mfa_setup_totp_generate",
            "mfa_setup_totp_confirm",
            "mfa_setup_yubikey_register",
            "analytics.logout",
            "analytics.login_page",
            "analytics.login_post",
            "analytics.dashboards_login_redirect",
            "static",
            "serve_static",
        }
        if request.endpoint not in mfa_whitelist:
            from flask import redirect

            # MFA pages are canonical at bare /mfa/* (not
            # /assets/mfa/*). The route is already in the PrefixMiddleware
            # allow-list so the bare path resolves end-to-end.
            return redirect("/mfa/challenge")
        return None

    # Check if already authenticated in session.
    # idle timeout + absolute cap (mirrors Kibana's idleTimeout +
    # lifespan model). A session is valid only if both timers are within
    # their windows. last_activity is refreshed on every authenticated
    # request; auth_timestamp is fixed at login. Sessions written before
    # may not have last_activity — fall back to auth_timestamp so
    # in-flight sessions don't crash on the upgrade.
    if "authenticated" in session and session.get("auth_timestamp"):
        now = time.time()
        idle_age, absolute_age = _session_ages(now)
        # the timer decision lives in _session_timer_fired so the
        # Kibana gate (session_denial_reason) cannot drift from it again.
        timer_fired = _session_timer_fired(idle_age, absolute_age)
        if timer_fired is None:
            # follow-up — does the user still exist in ES
            # Security? If they were deleted via /api/user-management/
            # delete-user OR via Kibana / direct ES, force-logout
            # within the TTL window. 30s cached so this is a dict
            # lookup on the typical request.
            session_username = session.get("username", "")
            if session_username and not _user_still_exists(session_username):
                logging.info(
                    "force-logout: session user=%r no longer exists in ES Security",
                    session_username,
                )
                session.clear()
                from flask import redirect, url_for

                return redirect(
                    url_for(
                        "analytics.login_page",
                        error="Your account has been removed. Please sign in again or contact your administrator.",
                    )
                )
            # and is the user still ENABLED? Suspending set
            # enabled=False in ES Security, which only ever blocked the
            # next login; the live session sailed on until the
            # timers fired. Same cached lookup as the existence check
            # above (the record carries the flag), same force-logout
            # shape, so a suspended user's session ends here within the
            # 30 s cache window.
            if session_username and _user_is_disabled(session_username):
                logging.info(
                    "force-logout: session user=%r is suspended (enabled=false) in ES Security",
                    session_username,
                )
                session.clear()
                from flask import redirect, url_for

                return redirect(
                    url_for(
                        "analytics.login_page",
                        error="Your account has been suspended. Please contact your administrator.",
                    )
                )
            # was the account's credential changed (admin
            # reset, admin set-password, or self-service change) AFTER
            # THIS session authenticated? Same cached lookup as the two
            # checks above (the record now also carries
            # ``credential_changed_at``); a session whose auth_timestamp
            # predates the stamp is the one an admin reset a password to
            # get rid of, so it ends here within the 30 s cache window.
            # Strict less-than: a session that authenticated in the SAME
            # instant as the stamp (e.g. the acting session's own
            # self-service change, which re-stamps auth_timestamp) is not
            # denied.
            if session_username:
                changed_at = _credential_changed_at(session_username)
                if changed_at is not None and float(session["auth_timestamp"]) < changed_at:
                    logging.info(
                        "force-logout: session user=%r auth_timestamp=%s predates credential_changed_at=%s",
                        session_username,
                        session["auth_timestamp"],
                        changed_at,
                    )
                    session.clear()
                    from flask import redirect, url_for

                    return redirect(
                        url_for(
                            "analytics.login_page",
                            error="Your password was changed. Please sign in again.",
                        )
                    )
            # bring session["roles"] / session["is_admin"] in
            # line with ES Security. Ranked last, after the deleted /
            # disabled / credential-changed force-logout checks above: a
            # session that should be thrown out entirely never reaches a
            # mere refresh. Same cached _user_account_record the three
            # checks above already paid for this request; see
            # refresh_session_roles for the no-op / rewrite contract.
            if session_username:
                refresh_session_roles(session_username)
            # follow-up — per-request wizard-pending gate.
            # Signup auto-login and login (when LoginGate returns
            # WIZARD_INCOMPLETE / VERIFY_EMAIL) set ``wizard_pending``
            # on the session. Until step-8 finish or step-6 skip clears
            # it, every request to a non-wizard endpoint is bounced
            # back to the resume URL so the user cannot reach
            # /dashboard, /assets/, dashboard-builder, etc. mid-wizard.
            #
            # The whitelist allows every onboarding step (and the
            # auxiliary screens — verify-required, invite-expired,
            # invite-team-picker), logout, the canonical /login
            # surface, static assets, the SSO callbacks the wizard
            # bounces through, and the CSRF/auth probes. Anything else
            # 302s to the wizard's resume URL.
            # Issue 11 (2026-05-25) — the wizard-pending gate
            # used to read ``session['wizard_pending']`` exclusively.
            # That session flag turned out to be fragile — multiple
            # code paths could inadvertently clear or fail to set it,
            # producing either a redirect loop (flag stale-True) or a
            # tenant leak (flag stale-False). The gate is now driven by
            # ES ``de_wizard_session.status`` as the authority.
            #
            # (2026-05-26) — earlier this only consulted ES when
            # the session flag was True, so a wrongly-CLEARED flag
            # (stale-False) let a mid-wizard user reach /dashboard. That
            # was the reported leak. The gate now ALWAYS consults ES for
            # an authenticated non-admin user, with the result memoized
            # on flask.g so the rest of the request reuses it. The
            # session flag is now only a hint that drives the self-heal
            # log line; it is NOT load-bearing for the block decision.
            # The behaviour is:
            #
            #   * ES status ``active``            → gate fires.
            #   * ES status terminal / no doc     → no gate (and clear
            #     any stale flag so the self-heal log is accurate).
            #   * ES unreachable / lookup error   → FAIL OPEN (do not
            #     strand the whole product on a transient blip); the
            #     login-time LoginGate already fail-opens the same way.
            #
            # Admins (superuser / asset_admin) are never wizard-cohort,
            # so we skip the ES read for them entirely (cost + they have
            # no wizard session to be confined to).
            _flag_says_pending = bool(session.get("wizard_pending"))
            _es_says_active = False
            username = session.get("username", "")
            _is_admin = bool(session.get("is_admin")) or any(
                r in ("superuser", "asset_admin") for r in (session.get("roles") or [])
            )
            # Cost control: once we have CONFIRMED (via an ES read) that
            # this session's user is past the wizard, stamp a sticky
            # session marker so onboarded users pay the ES read at most
            # once per session instead of on every request. The marker
            # is only set on a confirmed-terminal ES read — never
            # inferred from the fragile wizard_pending flag — so it
            # cannot itself reintroduce the stale-False leak.
            _wizard_cleared = bool(session.get("wizard_cleared"))
            if username and not _is_admin and not _wizard_cleared:
                try:
                    from flask import g as _g
                    from .services.wizard_session_service import (
                        WizardSessionService as _WSS,
                        STATUS_ACTIVE,
                        STATUS_COMPLETED,
                        STATUS_SKIPPED,
                    )

                    # Memoize per request — other consumers may read this
                    # again in the same request.
                    if hasattr(_g, "_wizard_status_cache"):
                        es_status = _g._wizard_status_cache
                    else:
                        es_status = _WSS.get_status(username, "onboarding")
                        _g._wizard_status_cache = es_status

                    if es_status == STATUS_ACTIVE:
                        _es_says_active = True
                    elif es_status in (STATUS_COMPLETED, STATUS_SKIPPED):
                        # Confirmed past the wizard. Stamp the sticky
                        # marker so future requests skip the ES read, and
                        # self-heal any stale-True wizard_pending flag.
                        session["wizard_cleared"] = True
                        if _flag_says_pending:
                            logging.info(
                                "wizard_pending self-heal: user=%r ES status=%s, clearing flag",
                                username,
                                es_status,
                            )
                            session.pop("wizard_pending", None)
                    # es_status is None → no wizard_session doc at all
                    # (grandfathered / non-wizard-cohort user). Treat as
                    # cleared so we don't read ES every request for the
                    # entire existing-user population.
                    elif es_status is None:
                        session["wizard_cleared"] = True
                except Exception as e:  # noqa: BLE001 — fail-soft / fail-open
                    # FAIL OPEN: never strand the product on an ES blip.
                    # Do NOT stamp wizard_cleared here — a transient error
                    # must not permanently mark an active-wizard user as
                    # done; the next request re-reads.
                    logging.warning(
                        "wizard status gate lookup failed: %s — fail open",
                        e,
                    )

            if _es_says_active:
                endpoint = request.endpoint or ""
                # Allow the wizard's own routes + the universal
                # log-out / re-auth path so a pending user can always
                # escape via sign-out and the wizard can use SSO
                # callbacks. Endpoints starting with these prefixes
                # are permitted; everything else 302s to the resume URL.
                wizard_pending_allow_prefixes = (
                    "analytics.onboarding_",
                    "analytics.oauth_",
                    "static",
                )
                wizard_pending_allow_endpoints = {
                    "analytics.logout",
                    "analytics.login_page",
                    "analytics.login_post",
                    "analytics.dashboards_login_redirect",
                    # the email-verify GET link the inbox
                    # message points at lives outside the wizard
                    # prefix on a few legacy aliases; allow it here
                    # so a click in the email tab doesn't bounce.
                    "analytics.onboarding_verify_email",
                    "analytics.onboarding_verify_required_page",
                    "analytics.onboarding_verify_required_check",
                    # The Meta / Google / Microsoft OAuth callbacks
                    # used by step-7 and the SSO buttons on step-1.
                    "analytics.onboarding_meta_callback",
                    "analytics.onboarding_google_callback",
                    "analytics.onboarding_microsoft_callback",
                    "analytics.onboarding_google_start",
                    "analytics.onboarding_microsoft_start",
                    "mfa_setup_choose",
                    "mfa_setup_choose_post",
                    "mfa_setup_totp_generate",
                    "mfa_setup_totp_confirm",
                    "mfa_setup_yubikey_register",
                    # the one-time recovery-code display shown right
                    # after MFA enrollment completes inside the wizard.
                    "mfa_recovery_codes_page",
                    "mfa_recovery_codes_continue",
                    "serve_static",
                    # CSRF token probes the wizard JS uses.
                    "analytics.csrf_token_endpoint",
                }
                permitted = (
                    not endpoint
                    or endpoint in wizard_pending_allow_endpoints
                    or any(endpoint.startswith(p) for p in wizard_pending_allow_prefixes)
                )
                if not permitted:
                    from flask import redirect, url_for

                    # Resolve the resume URL once — fail-soft to a
                    # known wizard entry point so a transient ES blip
                    # doesn't strand a pending user with no escape.
                    target = None
                    try:
                        from .services.wizard_resume_service import WizardResumeService

                        username = session.get("username", "")
                        if username:
                            resume_state = WizardResumeService.get_resume_state(username)
                            if resume_state and resume_state.resume_url:
                                target = resume_state.resume_url
                    except Exception as e:  # noqa: BLE001 — fail-soft
                        logging.warning(
                            "wizard_pending gate resume lookup failed: %s",
                            e,
                        )
                    if not target:
                        target = url_for("analytics.onboarding_step4_page")
                    return redirect(target)

            # Valid session — refresh idle timer and continue.
            #
            # do NOT refresh last_activity for automatic background
            # polls. The dashboard fires these on a wall-clock interval with
            # no user action (maintenance-alert banner check, onboarding
            # backfill-progress). If they bumped last_activity, the server
            # idle window would never elapse for an open-but-idle tab — the
            # session would stay alive forever and the client idle timer's
            # redirect to /login would bounce straight back (already-authed),
            # which is the "dashboard reloads but never logs out" symptom.
            # Only genuinely user-driven requests extend the idle window.
            # Fail-closed: anything NOT in this allowlist still counts as
            # activity (the earlier behavior), so no interactive request
            # regresses — we only stop the known background beacons.
            if not _is_background_poll():
                session["last_activity"] = now
                # record a throttled activity heartbeat to ES so the
                # User Management "active now" count reflects real per-request
                # activity (the cookie-only last_activity above is invisible to
                # any query). Reuse the same genuine-user-activity gate as the
                # idle-timer refresh — a background poll is not presence either.
                # Throttle to at most once per HEARTBEAT_INTERVAL_S per user via
                # session["last_heartbeat"], but always write the FIRST time
                # (marker unset) so a mid-session user is recorded within one
                # request after deploy rather than missing for a whole window.
                # Fail-soft: a heartbeat error must never break the request.
                _hb_username = session.get("username", "")
                if _hb_username:
                    _last_hb = session.get("last_heartbeat")
                    if _last_hb is None or (now - _last_hb) >= _HEARTBEAT_INTERVAL_S:
                        try:
                            from .services.presence_service import PresenceService

                            PresenceService.record_heartbeat(_hb_username)
                            session["last_heartbeat"] = now
                        except Exception as _hb_err:  # noqa: BLE001 — fail-soft
                            logging.warning(
                                "presence heartbeat failed for %r: %s",
                                _hb_username,
                                _hb_err,
                            )
            return check_route_authorization()
        else:
            # Session expired — redirect to login page
            logging.info(
                f"DEBUG: Session expired for user={session.get('username')} "
                f"timer={timer_fired} idle_age={int(idle_age)}s "
                f"absolute_age={int(absolute_age)}s "
                f"idle_limit={config.SESSION_LIFETIME}s "
                f"absolute_limit={config.SESSION_MAX_LIFETIME}s "
                f"endpoint={request.endpoint} method={request.method} "
                f"session_keys_before_clear={list(session.keys())}"
            )
            session.clear()
            from flask import redirect, url_for

            # every expired-session redirect goes to the canonical
            # bare /login. POST login (login_post) needs an explicit redirect
            # because session.clear() above wiped its csrf_token; GET login
            # pages will fall through and render a fresh form.
            #
            # AJAX/API callers get a 401 + JSON instead of a 302 so
            # their fetch never tries to parse the login HTML as JSON. The
            # client fetch guard (session-timeout.js) reads login_url and
            # navigates the whole tab. Full-page navigations keep the 302.
            if request.endpoint == "analytics.login_post":
                return _session_expired_response("/login?error=Your+session+expired.+Please+log+in+again.")
            if request.endpoint not in [
                "analytics.login_page",
                "analytics.dashboards_login_redirect",
                "static",
                "serve_static",
            ]:
                # For index page (tree explorer), redirect to show dashboard
                if request.path == "/" or request.path == "":
                    return _session_expired_response(f"/login?next={quote('/?show=dashboard', safe='/')}")
                # For other pages, preserve the requested path for post-login redirect.
                # URL-encode full_path so an attacker can't inject
                # extra query params (e.g. /foo&forced=1) via the request URL.
                # The login page validates next= via _safe_redirect_url before
                # using it, so this is defense-in-depth.
                full_path = request.environ.get("SCRIPT_NAME", "") + request.path
                return _session_expired_response(f"/login?next={quote(full_path, safe='/')}")

    # Reverse-proxy header SSO (X-Remote-User). :
    # TRUST_NGINX_HEADERS alone is NOT proof the request came through nginx —
    # the deploy exported it unconditionally while docker-compose published
    # gunicorn's port to the host, so anyone reaching :8084 could forge a
    # superuser session. The proxy hop is now proven independently of the
    # flag, twice, before the header carries any authority:
    #   1. request.remote_addr — the TCP peer, which unlike a header cannot
    #      be forged — must be in the TRUSTED_PROXY_IPS allowlist, and
    #   2. the proxy must present PROXY_AUTH_SECRET in X-Proxy-Auth,
    #      compared constant-time.
    # Both default empty, so the branch is inert unless a deployment
    # deliberately configures the whole chain (off-by-absence — no
    # current deployment does: the live SSO is Entra OAuth).
    # X-Remote-Roles is ignored entirely; roles come from ES Security.
    remote_user = request.headers.get("X-Remote-User")

    sso_roles: list[str] | None = None
    if remote_user and config.TRUST_NGINX_HEADERS and not session.get("logged_out"):
        proxy_secret = config.PROXY_AUTH_SECRET
        hop_proven = (
            request.remote_addr in config.TRUSTED_PROXY_IPS
            and bool(proxy_secret)
            and secrets.compare_digest(str(proxy_secret), str(request.headers.get("X-Proxy-Auth", "")))
        )
        sso_roles = _resolve_es_roles(remote_user) if hop_proven else None
        if sso_roles is None:
            # Fall through with no authority: the request proceeds exactly
            # like any unauthenticated one (login redirect / Basic-Auth
            # fallback below), so a forged header gets the same response as
            # no header at all.
            reason = "unverified_proxy_hop" if not hop_proven else "roles_unresolvable"
            logging.warning(
                f"AUDIT: user={remote_user} action=nginx_sso_login success=False " f"reason={reason} ip={request.remote_addr}"
            )

    if sso_roles is not None:
        roles = sso_roles

        # enforce MFA even on the SSO path. The nginx layer has
        # only validated password / upstream SSO — it has NOT validated a
        # second factor. If MFA is required, create an mfa_pending session
        # instead of a full one so the user is routed through enrollment
        # or challenge.
        from .services.mfa_service import MFAService

        mfa_status = MFAService.is_mfa_required(remote_user)
        if mfa_status["required"]:
            session.clear()
            session["mfa_pending"] = True
            session["mfa_pending_username"] = remote_user
            session["mfa_pending_method"] = mfa_status["method"]
            session["mfa_pending_timestamp"] = time.time()
            session["mfa_pending_roles"] = roles
            session["mfa_pending_full_name"] = remote_user
            session["mfa_pending_email"] = ""
            session["mfa_pending_next"] = "/"
            session["mfa_pending_login_type"] = "nginx_sso"
            logging.info(
                f"AUDIT: user={remote_user} action=nginx_sso_login mfa=required "
                f"method={mfa_status['method']} setup_complete={mfa_status['setup_complete']} "
                f"ip={request.remote_addr}"
            )
            from flask import redirect

            # route to the canonical bare /mfa/* path.
            mfa_path = "/mfa/setup" if not mfa_status["setup_complete"] else "/mfa/challenge"
            return redirect(mfa_path)

        # Create Flask session from nginx-validated user
        session["authenticated"] = True
        session["username"] = remote_user
        session["roles"] = roles
        session["is_admin"] = "superuser" in roles or config.REQUIRED_ROLE in roles
        session["full_name"] = remote_user  # Could be enhanced to fetch from ES
        session["email"] = ""
        session["auth_timestamp"] = time.time()
        session["last_activity"] = time.time()
        session["auth_method"] = "nginx_sso"
        session.permanent = True

        logging.info(f"AUDIT: user={remote_user} action=nginx_sso_login success=True roles={roles} ip={request.remote_addr}")
        try:
            from .services.audit_log_service import AuditLogService

            AuditLogService.log_event(
                remote_user,
                "login",
                True,
                auth_method="nginx_sso",
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent", ""),
            )
        except Exception:
            pass  # Don't break auth flow if audit logging fails

        # Check route authorization after authentication
        return check_route_authorization()

    # ── Test-persona header auth (staging verification only) ──────────────────
    # Lets a verification agent authenticate as a documented attack-surface
    # persona by sending two headers, skipping the login/MFA/cookie dance so
    # ProofShot can record admin-gated pages hands-off.
    #
    # SAFE BY ABSENCE, not by environment guess: the branch is inert
    # unless ``config.TEST_AUTH_SECRET`` is non-empty AND the request carries a
    # matching ``X-Test-Auth-Token``. Prod never sets the secret, so both a
    # normal prod request and a forged one (with guessed headers) fall straight
    # through. The token is compared constant-time. MFA is intentionally skipped
    # on this path — that is the whole point, and why it is gated on a secret
    # that lives only in staging's .env.
    test_secret = config.TEST_AUTH_SECRET
    test_token = request.headers.get("X-Test-Auth-Token", "")
    if test_secret and test_token and secrets.compare_digest(str(test_secret), str(test_token)):
        persona_key = request.headers.get("X-Test-Persona", "")
        persona = _test_persona(persona_key)
        if persona and not session.get("logged_out"):
            roles = list(persona["roles"])
            now = time.time()
            session.clear()
            session["authenticated"] = True
            session["username"] = persona["username"]
            session["roles"] = roles
            session["is_admin"] = "superuser" in roles or config.REQUIRED_ROLE in roles
            session["full_name"] = persona["username"]
            session["email"] = persona["username"]
            session["auth_timestamp"] = now
            session["last_activity"] = now
            session["auth_method"] = "test_persona_header"
            session.permanent = True
            logging.warning(
                f"AUDIT: user={persona['username']} action=test_persona_header_login "
                f"success=True roles={roles} ip={request.remote_addr} "
                "(TEST_AUTH_SECRET path — staging only)"
            )
            return check_route_authorization()

    # Get Authorization header for fallback HTTP Basic Auth
    auth_header = request.headers.get("Authorization")

    if not auth_header:
        # If no auth header, redirect to login form
        # Skip auth check for login endpoints and static files
        if request.endpoint in [
            "analytics.login_page",
            "analytics.login_post",
            "analytics.dashboards_login_redirect",
            "analytics.logout",
            "static",
            "serve_static",
            "mfa_challenge_page",
            "mfa_challenge_post",
            "mfa_challenge_recovery_page",
            "mfa_challenge_recovery_post",
            "mfa_recovery_codes_page",
            "mfa_recovery_codes_continue",
            # Tier 2 — email recovery. The consume + verify links are
            # clicked from an inbox with no session, so they must be reachable
            # with no auth header (the signed token is the proof).
            "mfa_challenge_email_page",
            "mfa_challenge_email_post",
            "mfa_recovery_email_consume",
            "profile_mfa_recovery_email_verify",
            # external report-recipient confirmation link (signed token; clicked from an inbox).
            "report_scheduler.report_recipient_verify",
            "mfa_setup_page",
            "mfa_setup_choose",
            "mfa_setup_choose_post",
            "mfa_setup_totp_generate",
            "mfa_setup_totp_confirm",
            "mfa_setup_yubikey_register",
            "analytics.signup_page",
            "analytics.signup_post",
            "analytics.verify_email",
            "analytics.signup_success_page",
            "analytics.onboarding_start_page",
            "analytics.onboarding_start_post",
            "analytics.onboarding_invite_expired_page",
            "analytics.onboarding_join_prompt_page",
            "analytics.onboarding_join_prompt_post",
            "analytics.onboarding_google_start",
            "analytics.onboarding_google_callback",
            "analytics.onboarding_microsoft_start",
            "analytics.onboarding_microsoft_callback",
            "analytics.onboarding_verify_email",
            "analytics.billing_stripe_webhook",  # follow-up: Stripe-signature-verified, no session
        ]:
            return None

        logging.warning(f"Missing authentication from {request.remote_addr}, redirecting to login")

        # every "needs auth" redirect goes to the canonical bare
        # /login URL. The original path (including any /assets prefix from
        # SCRIPT_NAME) is preserved on next= so the user lands back where
        # they tried to go after re-auth.
        # URL-encode full_path (defense-in-depth, see comment above).
        # AJAX/API callers get a 401 + JSON (see _session_expired_response)
        # so a fetch against an expired session redirects cleanly instead of
        # parse-erroring on the login HTML.
        full_path = request.environ.get("SCRIPT_NAME", "") + request.path
        return _session_expired_response(f"/login?next={quote(full_path, safe='/')}")

    # Extract username and password from Authorization header
    try:
        username, password = extract_credentials_from_basic_auth(auth_header)
    except Exception as e:
        logging.error(f"Failed to extract credentials from auth header: {e}")
        from flask import Response

        response = Response(
            "Invalid authentication format",
            401,
            {"WWW-Authenticate": 'Basic realm="Digital Engagement Dashboard - Asset Management"'},
        )
        abort(response)

    # Check rate limiting
    if not check_rate_limit(username):
        logging.warning(f"AUDIT: user={username} action=rate_limit_exceeded ip={request.remote_addr}")
        abort(429, description="Too many failed attempts. Please try again later.")

    # Validate against Elasticsearch
    from lib.es_auth import authenticate_user

    auth_result = authenticate_user(username, password)

    if not auth_result["success"]:
        record_failed_attempt(username)
        logging.warning(f"AUDIT: user={username} action=login success=False ip={request.remote_addr}")
        from flask import Response

        response = Response(
            "Invalid credentials. Please try again.",
            401,
            {"WWW-Authenticate": 'Basic realm="Digital Engagement Dashboard - Asset Management"'},
        )
        abort(response)

    # Check for required role (allow superuser or asset_admin)
    if config.REQUIRED_ROLE not in auth_result["roles"] and "superuser" not in auth_result["roles"]:
        logging.warning(
            f"AUDIT: user={username} action=authorization required_role={config.REQUIRED_ROLE} success=False roles={auth_result['roles']} ip={request.remote_addr}"
        )
        abort(
            403,
            description=f"Access denied. Your account needs the '{config.REQUIRED_ROLE}' or 'superuser' role.",
        )

    # Clear failed attempts on successful auth
    clear_failed_attempts(username)

    # enforce MFA on the Basic Auth fallback (parity with
    # the nginx SSO branch above). authenticate_user has only validated the
    # password — never a second factor — so without this gate a leaked
    # asset_admin/superuser password alone yields a full admin session from
    # a single Authorization: Basic header, never touching /mfa. Routing
    # through mfa_pending also picks up the force_password_change gate in
    # _complete_mfa_login, which this branch previously skipped.
    from .services.mfa_service import MFAService

    mfa_status = MFAService.is_mfa_required(username)
    if mfa_status["required"]:
        session.clear()
        session["mfa_pending"] = True
        session["mfa_pending_username"] = username
        session["mfa_pending_method"] = mfa_status["method"]
        session["mfa_pending_timestamp"] = time.time()
        session["mfa_pending_roles"] = auth_result["roles"]
        session["mfa_pending_full_name"] = auth_result.get("full_name", username)
        session["mfa_pending_email"] = auth_result.get("email", "")
        session["mfa_pending_next"] = "/"
        session["mfa_pending_login_type"] = "basic_auth"
        logging.info(
            f"AUDIT: user={username} action=login mfa=required "
            f"method={mfa_status['method']} setup_complete={mfa_status['setup_complete']} "
            f"ip={request.remote_addr}"
        )
        from flask import redirect

        # route to the canonical bare /mfa/* path.
        mfa_path = "/mfa/setup" if not mfa_status["setup_complete"] else "/mfa/challenge"
        return redirect(mfa_path)

    # Store in session
    session["authenticated"] = True
    session["username"] = username
    session["roles"] = auth_result["roles"]
    session["is_admin"] = "superuser" in auth_result["roles"] or config.REQUIRED_ROLE in auth_result["roles"]
    session["full_name"] = auth_result.get("full_name", username)
    session["email"] = auth_result.get("email", "")
    session["auth_timestamp"] = time.time()
    session["last_activity"] = time.time()
    session.permanent = True

    logging.info(f"AUDIT: user={username} action=login success=True ip={request.remote_addr}")

    # Check route authorization after authentication
    return check_route_authorization()


def get_current_user() -> dict:
    """
    Get current authenticated user from session.

    Returns:
        dict: User information or empty dict if not authenticated
    """
    if "authenticated" in session:
        return {
            "username": session.get("username"),
            "roles": session.get("roles", []),
            "is_admin": session.get("is_admin", False),
            "full_name": session.get("full_name"),
            "email": session.get("email", ""),
        }
    return {}


def extract_credentials_from_basic_auth(auth_header: str) -> tuple:
    """
    Extract username and password from HTTP Basic Auth header.

    Args:
        auth_header: Authorization header value (e.g., "Basic base64string")

    Returns:
        tuple: (username, password)

    Raises:
        ValueError: If header format is invalid
    """
    # Check if it's Basic auth
    if not auth_header.startswith("Basic "):
        raise ValueError("Not a Basic Auth header")

    # Extract the base64 encoded part
    encoded_credentials = auth_header[6:]  # Remove "Basic " prefix

    # Decode base64
    try:
        decoded_bytes = base64.b64decode(encoded_credentials)
        decoded_str = decoded_bytes.decode("utf-8")
    except Exception as e:
        raise ValueError(f"Failed to decode credentials: {e}")

    # Split on first colon (username:password)
    if ":" not in decoded_str:
        raise ValueError("Invalid credentials format")

    # Return both username and password
    parts = decoded_str.split(":", 1)
    if len(parts) != 2:
        raise ValueError("Invalid credentials format")

    return parts[0], parts[1]  # username, password


def _client_ip() -> str | None:
    """``request.remote_addr`` when inside a request, else None (CLI / tests)."""
    try:
        return request.remote_addr
    except RuntimeError:
        return None


def check_rate_limit(username: str) -> bool:
    """Is this username — and the requesting address — still under the lockout
    thresholds? (shared across workers and restarts, IP-aware.)

    Returns:
        bool: True if the attempt may proceed, False if locked out.
    """
    return LoginLockoutService.is_allowed(username, _client_ip())


def record_failed_attempt(username: str, *, surface: str = "login"):
    """Record a failed authentication attempt against the username and the
    requesting address."""
    LoginLockoutService.record_failure(username, _client_ip(), surface=surface)


def clear_failed_attempts(username: str) -> None:
    """Forget a username's failures after a successful login."""
    LoginLockoutService.clear(username, _client_ip())


def check_route_authorization():
    """
    Check if authenticated user has required role for current route.

    This enforces role-based access control AFTER authentication.
    Some routes (dashboards, logout) are accessible to all authenticated users.
    Other routes (asset management, logs, etc.) require asset_admin role.

    Returns:
        None if authorized, Flask abort(403) if not authorized
    """
    from flask import abort, session, request
    from .config import config

    # Skip authorization check for whitelisted endpoints (accessible to all authenticated users)
    whitelisted_endpoints = [
        "analytics.login_page",
        "analytics.login_post",
        "analytics.dashboards_login_redirect",
        "analytics.logout",
        "analytics.dashboard_page",
        "static",
        "serve_static",
        "mfa_challenge_page",
        "mfa_challenge_post",
        "mfa_challenge_recovery_page",
        "mfa_challenge_recovery_post",
        "mfa_recovery_codes_page",
        "mfa_recovery_codes_continue",
        # Tier 2 — email recovery routes accessible to any authenticated
        # role (the verify link may be clicked from a logged-in session) and to
        # the pre-auth challenge surface.
        "mfa_challenge_email_page",
        "mfa_challenge_email_post",
        "mfa_recovery_email_consume",
        "profile_mfa_recovery_email_verify",
        "mfa_setup_page",
        "mfa_setup_totp_generate",
        "mfa_setup_totp_confirm",
        "mfa_setup_yubikey_register",
        "analytics.signup_page",
        "analytics.signup_post",
        "analytics.verify_email",
        "analytics.signup_success_page",
        # Issue 10 — the signup GET/POST must be reachable by any
        # authenticated role so the cross-tab blocker (Issue 5) actually
        # renders for a non-admin user. Pre-fix only superuser/asset_admin
        # could reach the route; non-admin authenticated sessions were
        # 302'd to /dashboard by the role gate, so the blocker check
        # never even ran. Adding the endpoints here lets the blocker
        # render for the cohort it was actually designed for; the GET
        # superuser bypass at the route layer handles the admin path.
        "analytics.onboarding_start_page",
        "analytics.onboarding_start_post",
        # wizard steps 2-6 run with the bare ``kibana_user`` role
        # OnboardingSignupService assigns at step 1 — they must be reachable
        # before step 3's OnboardingService.provision_* elevates the user.
        "analytics.onboarding_step4_page",
        "analytics.onboarding_step4_post",
        # workspace creation screen. Same pre-elevation rationale —
        # the route persists workspace_title to wizard_session and never
        # calls provision_*, so the user still runs on bare ``kibana_user``.
        "analytics.onboarding_step5_page",
        "analytics.onboarding_step5_post",
        # inline slug-availability JSON probe consumed by step-3's
        # client script. Same pre-elevation rationale as the page route.
        "analytics.onboarding_step5_slug_check",
        # connector tile-grid screen. Selection persists to
        # wizard_session and never calls provision_*, so the user is
        # still on the bare ``kibana_user`` role assigned at signup.
        "analytics.onboarding_step6_page",
        "analytics.onboarding_step6_post",
        # pre-redirect OAuth scope summary. Same pre-elevation
        # rationale — the route reads wizard_session and stamps
        # current_step=step-6 but never calls provision_*, so the
        # user is still on the bare ``kibana_user`` role.
        "analytics.onboarding_step7_page",
        "analytics.onboarding_step7_post",
        # wizard-aware Meta OAuth roundtrip on step-5. Same
        # pre-elevation rationale — both routes read/write
        # wizard_session.state.{oauth_state, granted_scopes} and never
        # call provision_*, so the bare ``kibana_user`` role assigned
        # at signup is sufficient.
        "analytics.onboarding_step7_start_oauth",
        "analytics.onboarding_step7_oauth_callback",
        # asset selection table on step-6. Same pre-elevation
        # rationale — reads wizard_session.state.{selected_connector_id,
        # selected_assets} and never calls provision_*, so the bare
        # ``kibana_user`` role assigned at signup is sufficient.
        "analytics.onboarding_step8_page",
        "analytics.onboarding_step8_post",
        # team picker for invited users whose invite has no team_esid.
        # Reached from _consume_invite_and_handoff BEFORE provision_invited has
        # fired, so the user is still on the bare ``kibana_user`` role assigned
        # at signup — same pre-elevation rationale as the wizard step routes.
        "analytics.onboarding_invite_team_picker_page",
        # "Continue setup" banner's Restart button. Reached from
        # /dashboard by a user whose wizard_session is older than 30 days;
        # depending on which step they stalled on, they may still be on
        # the bare ``kibana_user`` (stalled at step-2/3) so the whitelist
        # entry mirrors the wizard step routes' pre-elevation rationale.
        "analytics.onboarding_resume_restart",
        # billing-upgrade landing page reached from the step-4
        # "Upgrade to add more sources" CTA. Same pre-elevation
        # rationale — a free-tier user clicks Upgrade before any
        # provision_* call has run, so the bare ``kibana_user`` must
        # be able to reach the page.
        "analytics.billing_page",
        # post-step-6 "Building your dashboard…" screen and
        # its JSON poller. The user has been elevated by step-3 by
        # this point (so they could have route_authz on any of the
        # elevated routes), but adding to the whitelist here makes
        # the wizard's full route surface explicit in one place —
        # easier to audit + matches how every other step-N route
        # is registered.
        "analytics.onboarding_step7_building_page",
        "analytics.onboarding_backfill_progress",
        # follow-up (2026-05-20) — verify-email holding page +
        # recheck route. The login gate lands a user on
        # /onboarding/verify-required when their email isn't verified;
        # both this page and its "I've verified — refresh" recheck
        # endpoint must be reachable on the bare ``kibana_user`` role
        # assigned at signup. Same pre-elevation rationale as the wizard
        # step routes — the user has signed up but never run provision_*.
        "analytics.onboarding_verify_required_page",
        "analytics.onboarding_verify_required_check",
        # Phase 1 (2026-05-20) — verify-email-step is logical
        # step 2 of the 8-step wizard. Same pre-elevation rationale
        # as the other wizard step routes: the user has signed up
        # but hasn't yet run any provision_* call, so the bare
        # ``kibana_user`` role assigned at signup must reach the GET
        # + the POST handlers (continue check, resend email, and
        # follow-up: change-email for typo recovery).
        "analytics.onboarding_verify_email_step_page",
        "analytics.onboarding_verify_email_step_post",
        "analytics.onboarding_verify_email_step_resend",
        "analytics.onboarding_verify_email_step_change_email",
        # Item 4 (2026-05-20) — JSON status probe consumed by
        # the Continue-button JS poll. Same pre-elevation rationale
        # as the other step-2 routes.
        "analytics.onboarding_verify_email_step_status",
        # Item 1 + Item 3 (2026-05-20) — the public verify
        # link landing now redirects in-session users back to the
        # wizard step 2. Combined with Item 3 (signup is roleless),
        # a user clicking their own verify link inside an active
        # session has roles=[] and would otherwise be bounced by
        # the role gate before the redirect could run. JWT is the
        # unforgeable token; auth is enforced on the redirect target.
        "analytics.onboarding_verify_email",
        # Phase 2 (2026-05-20) — mfa-setup-step is logical
        # step 3 of 8. The POST handles method-pick, TOTP confirm,
        # yubikey register, and the continue gate (has_any_factor).
        # Same pre-elevation rationale.
        "analytics.onboarding_mfa_setup_step_page",
        "analytics.onboarding_mfa_setup_step_post",
        # friendly "coming soon" landing for checklist items
        # whose real surface lives in a future epic (weekly summary,
        # conversion goals). The /dashboard checklist card itself
        # renders for every authenticated user — gating the click-
        # through on asset_admin would surprise dashboard-only users
        # with a 403 from a CTA they can see.
        "analytics.checklist_coming_soon",
        # per-item skip / unskip surfaces. The checklist card
        # renders for every authenticated user, so the skip and unskip
        # CTAs on each row must reach the route from a dashboard-only
        # session (which lacks asset_admin). Auth + CSRF still apply
        # via verify_auth and the global CSRF hook.
        "analytics.dashboard_checklist_skip",
        "analytics.dashboard_checklist_unskip",
        # collapse / expand surfaces. Same rationale as the
        # skip/unskip pair — the checklist card renders for every
        # authenticated user, so the header-bar Expand toggle (and the
        # full-card Collapse affordance) must reach the route without
        # asset_admin. Auth + CSRF still apply.
        "analytics.dashboard_checklist_collapse",
        "analytics.dashboard_checklist_expand",
        # beacon endpoint for per-item click-through
        # tracking. Same rationale: the checklist renders for every
        # authenticated user, so the beacon must reach the route
        # without asset_admin. CSRF is enforced in-route (the global
        # hook is exempted for sendBeacon compatibility).
        "analytics.dashboard_checklist_track_click",
        # the dashboard custom-range "pull new data" banner is a
        # dashboard-viewer feature, not a wizard/admin feature. Its kickoff
        # (/api/dashboards/...) is already allowlisted below, but the
        # progress poll and the Cancel POST were not — so a dashboard-only
        # (non-asset_admin) user who STARTED an import couldn't see its
        # progress or CANCEL it (the role gate 302'd them before the
        # handler ran). Any user who can start an import must be able to
        # cancel it; the cancel route still enforces per-run ownership
        # in-handler (by run_id -> username), and CSRF still applies.
        "analytics.onboarding_backfill_progress",
        "analytics.onboarding_import_cancel",
        # Tier 1 — the connection detail page and its re-auth
        # surfaces must be reachable by workspace OWNERS, who hold their role
        # as a System 2 grant with EMPTY session["roles"] (same shape as
        # Billing /): the generic asset_admin check below would 302
        # them to /dashboard before the in-handler gates ever ran. Each
        # handler enforces its own fail-closed gate — connection_detail via
        # _viewer_can_see_connection (roles-aware None handling + owner
        # grant), start_reauthorize / resolve_reauth_coverage via
        # _viewer_can_reauthorize_connection (principal / explicit superuser
        # / scoped admin / workspace-owner grant; the ambiguous None sentinel
        # always denies). connections.disconnect_connection is deliberately
        # NOT exempted — it stays principal-only behind the asset_admin gate.
        "connections.connection_detail",
        "connections.start_reauthorize",
        "connections.resolve_reauth_coverage",
    ]
    if request.endpoint in whitelisted_endpoints:
        return None

    # Skip for API dashboard endpoints (accessible to all authenticated users)
    if request.path.startswith("/api/dashboards/"):
        return None

    # Skip for feedback endpoints (accessible to all authenticated users)
    if request.path.startswith("/api/feedback/"):
        return None

    # Skip for docs nav-filter endpoint. Regular users need this
    # to learn which documentation sections to hide in the sidebar.
    if request.path == "/api/documentation-permissions":
        return None

    # Skip for ECharts dashboard viewer and its required API endpoints.
    # These are used by org/team users who don't have asset_admin role.
    if (
        request.path.startswith("/dashboard-builder/view/")
        or request.path.startswith("/dashboard-builder/api/dashboards/")
        or request.path.startswith("/dashboard-builder/api/data-views")
        or request.path == "/dashboard-builder/api/query"
        # the Summary Report's batched variant of /api/query.
        # Same audience (any dashboard viewer), same per-item scope guards
        # in-route.
        or request.path == "/dashboard-builder/api/query_batch"
        or request.path == "/dashboard-builder/api/map-data"
        or request.path.startswith("/dashboard-builder/api/map-data/")
        or request.path == "/dashboard-builder/api/trend-line-data"
    ):
        return None

    # Self-service profile — every authenticated user can
    # manage their own name/email/password/MFA, regardless of role.
    if request.path == "/profile" or request.path.startswith("/profile/"):
        return None

    # Billing section — skip the role-based route-authz check so
    # the billing route HANDLERS run their own gate. The section is
    # restricted to superuser + workspace_owner via
    # ``user_can_access_billing`` (enforced in-handler, see the analytics
    # billing routes); a workspace_owner holds the role as a System 2 grant
    # with an empty ``session["roles"]``, so the generic asset_admin check
    # here would wrongly bounce them before the handler's gate could run.
    # verify_auth still enforces a logged-in session upstream. Supersedes the
    # narrower ``analytics.billing_page`` endpoint allowlist entry.
    if request.path == "/billing" or request.path.startswith("/billing/"):
        return None

    # Self-service account-type flip. Lives at
    # /settings/account-type instead of /profile/account-type because the
    # POST predates the Profile-card UI by several KAN cycles and external
    # references already exist. Same auth posture as /profile: any
    # authenticated user can flip their own account_type cohort label.
    if request.path == "/settings/account-type":
        return None

    # For all other routes, check if user has required role.
    # Phase 3: compute the old-system allow/deny decision into
    # one variable, run the shadow comparison once at the junction,
    # then act on the original decision unchanged.
    if "authenticated" in session and session.get("authenticated"):
        user_roles = session.get("roles", [])
        is_super_admin = "superuser" in user_roles

        try:
            from web.services.role_permissions_service import RolePermissionsService
        except ImportError:
            from .services.role_permissions_service import RolePermissionsService

        old_allow = False
        if config.REQUIRED_ROLE in user_roles or is_super_admin:
            old_allow = True
        elif request.path.startswith("/data-explorer") or request.path.startswith("/data-explorer/"):
            # Data Explorer is available to dashboard-side users via the
            # db_data_explorer permission. The blueprint performs its
            # own finer-grained permission check.
            perms = RolePermissionsService.get_merged_permissions(user_roles)
            if perms.get("db_data_explorer") or perms.get("mc_data_explorer"):
                old_allow = True
        else:
            # MC sidebar destinations whose route handler now
            # gates on an mc_* perm. Without this special case the
            # asset_admin check above would 403 a custom role before
            # its own mc_* gate runs, making the Role Settings
            # checkbox a no-op. Each entry is a path predicate paired
            # with the perm that grants access; the route handler
            # still runs the same check downstream.
            _mc_path_perms = (
                (lambda p: p == "/dashboard-builder/" or p.startswith("/dashboard-builder/"), "mc_dashboard_builder"),
                (lambda p: p == "/restore-backup/" or p.startswith("/restore-backup/"), "mc_restore_backup"),
                (lambda p: p == "/user-management", "mc_user_management"),
                (lambda p: p == "/testing", "mc_testing"),
            )
            for matches, perm_name in _mc_path_perms:
                if matches(request.path):
                    perms = RolePermissionsService.get_merged_permissions(user_roles)
                    if perms.get(perm_name):
                        old_allow = True
                    break

        # Phase 4 cutover (USE_NEW_PERMISSIONS flag retired in
        # 05-28): admin status is sourced from
        # ``session['roles']``, not the new-authz indexes. The new
        # authz model has no per-user concept of asset_admin /
        # superuser — those are ES Security session-side role names,
        # not new-authz role assignments. The 2026-05-06 flip attempt
        # confirmed: lookup by name returned empty for every user
        # including admins because the backfill skipped admins by
        # design.
        if old_allow:
            return None

        # route-authorization carve-out for scoped admins.
        # Without this, a user holding only workspace_admin / org_admin
        # 403s here before the route's own _require_admin or _require_mc
        # gate (which know about the new authz model) ever runs. Logging
        # the carve-out matches the _require_admin C1 policy — the same
        # follow-up audit ticket will examine which paths actually
        # depend on scoped-admin elevation.
        username_for_check = session.get("username", "")
        if username_for_check:
            try:
                from web.services.permission_resolver import PermissionResolver, SIDEBAR_ITEM_PERMISSIONS
            except ImportError:
                from asset_management.web.services.permission_resolver import (
                    PermissionResolver,
                    SIDEBAR_ITEM_PERMISSIONS,
                )
            held = PermissionResolver.effective_permissions_anywhere(username_for_check)
            # The migrated mc_*/hdr_*/db_* keys all map through SIDEBAR_ITEM_PERMISSIONS.
            # If the user holds any of the underlying primitives, treat
            # them as authorized at the route level — the page route
            # itself still runs its specific gate.
            any_migrated_perm_held = any(any(p in held for p in needed) for needed in SIDEBAR_ITEM_PERMISSIONS.values())
            if any_migrated_perm_held:
                logging.warning(
                    "scope-violation candidate: user=%s scoped_admin_path=%s",
                    username_for_check,
                    request.path,
                )
                return None

        username = session.get("username", "unknown")
        logging.warning(
            f"AUDIT: user={username} action=route_authorization path={request.path} required_role={config.REQUIRED_ROLE} success=False roles={user_roles} ip={request.remote_addr}"
        )
        abort(
            403,
            description=f"Access denied. This page requires the '{config.REQUIRED_ROLE}' role. You can access the <a href='/dashboard'>dashboard page</a>.",
        )

    return None
