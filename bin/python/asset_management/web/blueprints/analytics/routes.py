"""
Analytics Blueprint Routes
Public-facing routes for Kavanah Analytics (kavanahanalytics.com):
  /signup, /login, /dashboard, /logout

/login is the single sign-in page for both surfaces (asset
manager and dashboards). The legacy /dashboards/login URL — and direct
hits to /assets/login when the asset-manager prefix is in play — issue
permanent redirects to /login (308 for POST so form bodies survive,
301 for GET) so existing bookmarks keep working.

Marketing pages (/landing, /pricing, /features/<slug>) are served as
static files from the the private docs submodule — see
the private docs
"""

import os
import time
import logging
from datetime import datetime, timezone

from flask import (
    abort,
    current_app,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from asset_management.web.login_gate_redirect import gate_redirect_after_auth
from asset_management.web.public_url import public_base_url
from . import analytics_bp

try:
    from web.config import config
    from web import auth as auth_module
    from web.services.audit_log_service import AuditLogService
    from web.services.kibana_gate_service import KibanaGateService
    from web.services.mfa_service import MFAService
    from web.services.user_provisioning_service import UserProvisioningService
    from web.services.dashboard_service import DashboardService
    from web.services.dashboard_checklist_service import DashboardChecklistService
    from web.services.role_filter_service import RoleFilterService
    from web.services import time_preset_service
    from web.services.registration_service import RegistrationService
    from web.services.invite_service import InviteService, NOTE_MAX_LENGTH
    from web.services.workspace_membership_service import WorkspaceMembershipService
    from web.services.wizard_analytics_service import (
        ACTION_COMPLETED,
        ACTION_ENTERED,
        ACTION_ERRORED,
        ACTION_SKIPPED,
        WizardAnalyticsService,
    )
    from web.services.wizard_progress_service import WizardProgressService
except ImportError:
    from asset_management.web.config import config
    from asset_management.web import auth as auth_module
    from asset_management.web.services.audit_log_service import AuditLogService
    from asset_management.web.services.kibana_gate_service import KibanaGateService
    from asset_management.web.services.mfa_service import MFAService
    from asset_management.web.services.user_provisioning_service import UserProvisioningService
    from asset_management.web.services.dashboard_service import DashboardService
    from asset_management.web.services.dashboard_checklist_service import (
        DashboardChecklistService,
    )
    from asset_management.web.services.role_filter_service import RoleFilterService
    from asset_management.web.services import time_preset_service
    from asset_management.web.services.registration_service import RegistrationService
    from asset_management.web.services.invite_service import InviteService, NOTE_MAX_LENGTH
    from asset_management.web.services.workspace_membership_service import (
        WorkspaceMembershipService,
    )
    from asset_management.web.services.wizard_analytics_service import (
        ACTION_COMPLETED,
        ACTION_ENTERED,
        ACTION_ERRORED,
        ACTION_SKIPPED,
        WizardAnalyticsService,
    )
    from asset_management.web.services.wizard_progress_service import (
        WizardProgressService,
    )

USE_PREFIX = os.environ.get("FLASK_USE_PREFIX", "true").lower() == "true"


def _wizard_progress_for(current_step: str, username: str | None = None):
    """Resolve the wizard progress snapshot for a render call.

    Reads the active wizard_session if a username is provided so the
    "~X min left" estimate adapts to the user's elapsed-time pace. Any
    ES blip falls back to the static-median estimate (the service
    treats a None session as "no pace history available"). This helper
    centralises the lookup so every render site is a one-line call —
    a future swap to population medians from the analytics
    events lives here and only here.
    """
    if not username:
        return WizardProgressService.get_progress(current_step)
    try:
        # Lazy import — WizardSessionService is already imported in the
        # routes that need it for analytics; we avoid making it a
        # top-level import in this module so a Flask import cycle stays
        # contained.
        try:
            from web.services.wizard_session_service import WizardSessionService as _WSS
        except ImportError:
            from asset_management.web.services.wizard_session_service import (
                WizardSessionService as _WSS,
            )
        active = _WSS.get_active(username, "onboarding")
    except Exception as e:  # noqa: BLE001 — never block the render on an ES blip
        logging.warning("wizard_progress get_active failed: %s", e)
        active = None
    return WizardProgressService.get_progress(current_step, wizard_session=active)


def _redirect_if_wizard_done(username: str):
    """follow-up — block /onboarding/step-N re-entry after the
    wizard has finished.

    Called at the top of every ``onboarding_stepN_page`` GET. If the
    user's wizard_session is in a terminal status (``completed`` or
    ``skipped``), returns a 302 to /dashboard with a one-time flash
    explaining what happened. Otherwise returns None and the caller
    continues rendering.

    Why both completed AND skipped: a user who clicked "Skip — connect
    later" on step-6 IS done with the wizard (they explicitly opted
    out). Re-entering /onboarding/step-N by typing it in the URL bar
    or following a stale tab link should land them on /dashboard with
    the same UX as a completed user. The reactivation path
    that previously let skipped users re-enter via the dashboard
    checklist's "Connect a data source" CTA is replaced by the
     retarget of that row to /assets/data-connectors/ — the
    post-wizard connector-management surface.

    Fail-soft: WizardSessionService.get_status returns None on ES
    outage, which falls through to "allow access" so a transient blip
    doesn't strand a mid-wizard user.
    """
    if not username:
        return None
    try:
        try:
            from web.services.wizard_session_service import (
                WizardSessionService as _WSS,
                STATUS_COMPLETED,
                STATUS_SKIPPED,
            )
        except ImportError:
            from asset_management.web.services.wizard_session_service import (  # type: ignore[no-redef]
                WizardSessionService as _WSS,
                STATUS_COMPLETED,
                STATUS_SKIPPED,
            )
        status = _WSS.get_status(username, "onboarding")
    except Exception as e:  # noqa: BLE001 — top-level boundary; never block on the guard
        logging.warning("wizard-done guard failed for %r: %s", username, e)
        return None
    if status in (STATUS_COMPLETED, STATUS_SKIPPED):
        # Silent 302 to /dashboard. A flash() call would push the
        # "you're already set up" message into the Flask session, but
        # dashboards.html doesn't render get_flashed_messages today.
        # Adding the flash surface would be a separate template-side
        # change; this guard ships with the redirect-only behaviour
        # and a follow-up ticket can add the explanation banner if
        # users land here confused. The blocked-status query param is
        # passed so the dashboard handler / template can decide later
        # to surface the message without a global flash refactor.
        return redirect(url_for("analytics.dashboard_page", wizard_done=status))
    return None


def _safe_redirect_url(url: str | None, default: str = "/") -> str:
    """Validate a redirect URL to prevent open redirect attacks.

    Only relative paths that start with '/' are allowed.  Absolute URLs,
    protocol-relative URLs (``//evil.com``), and non-path values are
    rejected and replaced with *default*.

    also reject URLs containing backslash or any ASCII
    control character. Older browsers (and some embedded WebViews) normalize
    backslash in a Location header to forward slash, turning ``/\\evil.com``
    into a protocol-relative redirect. ASCII control chars can be similarly
    abused (some renderers strip them, again collapsing ``/<TAB>/evil.com``
    to ``//evil.com``). Werkzeug already rejects CR/LF at the header
    boundary, but other control chars slip through — defense in depth.
    """
    if not url or not isinstance(url, str):
        return default
    if not url.startswith("/") or url.startswith("//"):
        return default
    if "\\" in url:
        return default
    # Reject any ASCII control character (0x00-0x1F, 0x7F).
    if any(c < " " or c == "\x7f" for c in url):
        return default
    # reject non-landable API paths. This guard only ever protected
    # against OPEN redirects; a same-origin ``next=/dashboard-builder/api/...``
    # (built by any past or future producer, e.g. the earlier AJAX-expiry
    # branch) landed the user on a bare API/POST URL after login. No DEAM
    # page lives under an ``api`` path segment, so any ``/api/`` component
    # marks a non-page target — fall back to the default landing.
    path = url.split("?", 1)[0].split("#", 1)[0]
    if "/api/" in path or path.rstrip("/").endswith("/api"):
        return default
    return url


def _mfa_redirect(endpoint: str, **kwargs):
    """Build a redirect to an MFA route at the bare /mfa/* path.

    MFA pages are canonical at bare /mfa/* (matching the bare
    /login surface). url_for under PrefixMiddleware would re-prepend
    /assets, so strip it back off before redirecting.
    """
    path = url_for(endpoint, **kwargs)
    if path.startswith("/assets"):
        path = path[len("/assets") :]
    return redirect(path)


# Default CA bundle mounted into the container (same path the Elasticsearch
# client verifies against in lib.es_helpers.get_elasticsearch_client).
_KIBANA_CA_CERT_PATH = "/app/data/certs/es01_http_ca.crt"


def _kibana_tls_verify():
    """Resolve the ``verify`` argument for the Kibana login request.

    Secure by default: this call carries the user's plaintext password, so TLS
    verification must not be silently disabled (was hardcoded ``verify=False``).

    Resolution order:
      1. ``KIBANA_CA_CERT`` env var, if set and the file exists -> verify against it.
      2. The standard container CA bundle, if present -> verify against it.
      3. Otherwise -> ``True`` (verify against the system trust store; works for
         the public dev-reporting host's CA-issued cert).

    Escape hatch: ``KIBANA_TLS_INSECURE=1`` restores the old skip-verification
    behavior for an environment that genuinely needs it (e.g. an internal IP
    with a self-signed cert and no mounted CA). It is logged loudly so the
    insecure posture is never silent.
    """
    if os.environ.get("KIBANA_TLS_INSECURE") == "1":
        logging.warning("KIBANA_TLS_INSECURE=1 — Kibana login TLS verification is DISABLED")
        return False
    ca_path = os.environ.get("KIBANA_CA_CERT") or _KIBANA_CA_CERT_PATH
    if ca_path and os.path.exists(ca_path):
        return ca_path
    return True


def _create_kibana_session(username: str, password: str) -> str | None:
    """Create a Kibana session by calling Kibana's login API.

    Returns the sid cookie value on success, None on failure.
    """
    import requests

    from asset_management.web.config import DEFAULT_KIBANA_URL

    kibana_url = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)
    login_url = f"{kibana_url}/internal/security/login"

    try:
        response = requests.post(
            login_url,
            json={
                "providerType": "basic",
                "providerName": "basic",
                "currentURL": f"{kibana_url}/",
                "params": {"username": username, "password": password},
            },
            headers={
                "kbn-xsrf": "true",
                "Content-Type": "application/json",
                "x-elastic-internal-product": "kibana",
            },
            verify=_kibana_tls_verify(),
            allow_redirects=False,
            timeout=10,
        )

        if response.status_code == 200:
            sid_cookie = response.cookies.get("sid")
            if sid_cookie:
                logging.info(f"AUDIT: user={username} action=kibana_session_create success=True")
                return sid_cookie
            else:
                logging.warning(f"Kibana login succeeded but no sid cookie returned for {username}")
        else:
            logging.warning(f"Kibana login failed for {username}: {response.status_code}")

    except Exception as e:
        logging.error(f"Error creating Kibana session for {username}: {e}")

    return None


# ============================================
# AUTH ROUTES
# ============================================


def _clear_partial_sessions_on_login_view():
    """Drop fpc_pending / mfa_pending state when a user intentionally
    navigates back to the login page — they're asking to start over
    (typically to sign in as someone else).."""
    if session.get("fpc_pending") or session.get("mfa_pending"):
        session.clear()


def _canonical_login_url(path: str = "/login") -> str:
    """Build a bare-host login URL with the current request's query string.

    every flavour of sign-in (``/login``, ``/assets/login``,
    ``/dashboards/login``) is canonicalised to bare ``/login``. We can't
    use ``url_for`` here because under the ``PrefixMiddleware`` it would
    re-prepend ``/assets``, defeating the point.
    """
    qs = request.query_string.decode("ascii", errors="replace")
    return f"{path}?{qs}" if qs else path


def _redirect_to_canonical_login_if_prefixed(method_preserving: bool):
    """If the request reached us via the ``/assets`` prefix, bounce to bare
    ``/login`` so the URL the user sees is the single canonical entry. Returns
    a Flask response when a redirect is issued, otherwise ``None``.

    GETs use 301 (permanent); POSTs use 308 so the form body survives the
    browser hop without being silently re-issued as GET.
    """
    if request.script_root == "/assets":
        status = 308 if method_preserving else 301
        return redirect(_canonical_login_url("/login"), code=status)
    return None


@analytics_bp.route("/login", methods=["GET"])
def login_page():
    """Display login form."""
    # a direct hit to /assets/login bounces to the canonical /login
    # so the URL bar reflects the single sign-in surface.
    if resp := _redirect_to_canonical_login_if_prefixed(method_preserving=False):
        return resp

    # If there's a partial sign-in flow (fpc / mfa) lingering in the session,
    # clear it so the login form is actually usable.
    _clear_partial_sessions_on_login_view()

    # If already authenticated, redirect to requested page
    if "authenticated" in session and session.get("authenticated"):
        # run the login gate even for already-authenticated
        # sessions. Pre-fix, this branch redirected to /dashboard
        # without consulting the gate; a wizard-incomplete user who
        # hit /login (e.g. clicking a CTA from the verify-email tab,
        # or hitting /login directly while mid-wizard) bypassed the
        # gate entirely and landed on /dashboard with no workspace
        # scope — full cross-tenant dashboard leak. Run the gate
        # first; only fall through to the next-url redirect when the
        # gate returns NONE.
        username = session.get("username", "")
        if username:
            try:
                from asset_management.web.services.login_gate_service import (
                    LoginGate,
                    LoginGateService,
                )

                _gate = LoginGateService.evaluate_for_login(username, session.get("roles") or [])
                if _gate.gate == LoginGate.VERIFY_EMAIL:
                    session["wizard_pending"] = True
                    return redirect(url_for("analytics.onboarding_verify_required_page"))
                if _gate.gate == LoginGate.WIZARD_INCOMPLETE:
                    session["wizard_pending"] = True
                    try:
                        from asset_management.web.services.wizard_resume_service import (
                            WizardResumeService,
                        )

                        resume_state = WizardResumeService.get_resume_state(username)
                        target = (resume_state.resume_url if resume_state else None) or url_for(
                            "analytics.onboarding_step4_page"
                        )
                    except Exception as e:  # noqa: BLE001 — fail-soft
                        logging.warning(
                            "already-auth login gate resume lookup failed for %r: %s",
                            username,
                            e,
                        )
                        target = url_for("analytics.onboarding_step4_page")
                    return redirect(target)
                # Gate said NONE — clear any leftover flag from a prior
                # wizard-incomplete session in this same browser.
                session.pop("wizard_pending", None)
            except Exception as e:  # noqa: BLE001 — never block login on gate failure
                logging.warning(
                    "already-auth login gate evaluate failed for %r: %s",
                    username,
                    e,
                )

        next_url = _safe_redirect_url(request.args.get("next"), "/dashboard")
        allowed_without_prefix = [
            "/login",
            "/logout",
            "/dashboard",
            "/dashboards/login",
            "/api/dashboards/",
        ]
        needs_prefix = (
            USE_PREFIX
            and not next_url.startswith("/assets")
            and not any(next_url.startswith(path) for path in allowed_without_prefix)
        )
        if needs_prefix:
            next_url = "/assets" + next_url
        return redirect(next_url)

    csrf_token = auth_module.refresh_csrf_token()

    error_message = request.args.get("error")
    success_message = request.args.get("message")
    next_url = _safe_redirect_url(request.args.get("next"), "")

    response = make_response(
        render_template(
            "login.html",
            csrf_token=csrf_token,
            error_message=error_message,
            success_message=success_message,
            next_url=next_url,
            registration_enabled=config.REGISTRATION_ENABLED,
            # TE accounts are admin-created, so the wizard's
            # signup page (the only other place these buttons render) is
            # never visited — the sign-in surface is the only way an
            # existing user can reach SSO. Fail-closed: an unconfigured
            # client hides the button, matching the route's own 404.
            microsoft_sso_enabled=config.MICROSOFT_SSO_ENABLED,
            google_sso_enabled=config.GOOGLE_SSO_ENABLED,
        )
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@analytics_bp.route("/login", methods=["POST"])
def login_post():
    """Handle login form submission."""
    # form posts that land on /assets/login bounce to bare /login
    # before any auth work runs. 308 preserves method + body.
    if resp := _redirect_to_canonical_login_if_prefixed(method_preserving=True):
        return resp

    from lib.es_auth import authenticate_user

    # Validate CSRF token
    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        session_tok = session.get("csrf_token", "")
        logging.warning(
            f"AUDIT: CSRF validation failed from {request.remote_addr} — "
            f"session_has_csrf={bool(session_tok)}, "
            f"session_authenticated={session.get('authenticated')}, "
            f"form_token_present={bool(csrf_token)}, "
            f"session_tok_prefix={session_tok[:8] if session_tok else 'NONE'}, "
            f"form_tok_prefix={csrf_token[:8] if csrf_token else 'NONE'}, "
            f"session_keys={list(session.keys())}"
        )
        return redirect(url_for("analytics.login_page"))

    # Extract credentials from form
    credential = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not credential or not password:
        return redirect(url_for("analytics.login_page", error="Username and password are required."))

    # follow-up: accept email as a credential. If the user
    # typed something that looks like an email (single '@', non-empty
    # local part + dotted domain), resolve it to the ES Security
    # username via the same enumeration the signup duplicate-check
    # uses. A miss falls through to the existing "Invalid username or
    # password" error — login is NOT the place to leak account
    # existence (signup is, by design; login is the security-sensitive
    # surface for this exact decision).
    username = credential
    if "@" in credential:
        from asset_management.web.services.onboarding_signup_service import (
            OnboardingSignupService,
        )

        match = OnboardingSignupService.find_user_by_email(credential.lower())
        if match is None:
            # Don't disclose at login-time; mirror the same "invalid"
            # path the wrong-password branch takes.
            auth_module.record_failed_attempt(credential)
            AuditLogService.log_event(
                credential,
                "login",
                False,
                auth_method="form_login",
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent", ""),
                failure_reason="email_no_match",
            )
            return redirect(url_for("analytics.login_page", error="Invalid username or password."))
        username = match["username"]

    # Check rate limiting (reuses existing logic)
    if not auth_module.check_rate_limit(username):
        logging.warning(f"AUDIT: user={username} action=rate_limit_exceeded ip={request.remote_addr}")
        abort(429, description="Too many failed attempts. Please try again later.")

    # Validate against Elasticsearch Security API
    auth_result = authenticate_user(username, password)

    if not auth_result["success"]:
        auth_module.record_failed_attempt(username)
        logging.warning(f"AUDIT: user={username} action=login_form_submit success=False ip={request.remote_addr}")
        AuditLogService.log_event(
            username,
            "login",
            False,
            auth_method="form_login",
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            failure_reason="invalid_credentials",
        )
        return redirect(url_for("analytics.login_page", error="Invalid username or password."))

    # Authentication successful - allow login for all users
    # Role-based authorization will be enforced at the route level by verify_auth()

    # Clear failed attempts on successful auth. through the
    # helper, not a `del` on the username dict — the shared LoginLockoutService
    # store also counts failures per IP, and only the helper clears both (the
    # Basic-Auth path in auth.py has done it this way).
    auth_module.clear_failed_attempts(username)

    # Check if MFA is required for this user
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
        # create Kibana SSO session now so we never store the plaintext
        # password in the Flask session.  Only the opaque sid cookie is kept.
        session["mfa_pending_kibana_sid"] = _create_kibana_session(username, password)
        session["mfa_pending_next"] = _safe_redirect_url(request.args.get("next"), "/dashboard")
        session["mfa_pending_login_type"] = "form_login"
        logging.info(
            f"AUDIT: user={username} action=login_form_submit mfa=required method={mfa_status['method']} ip={request.remote_addr}"
        )
        AuditLogService.log_event(
            username,
            "mfa_required",
            True,
            auth_method="form_login",
            mfa_method=mfa_status["method"],
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
        )
        if not mfa_status["setup_complete"]:
            return _mfa_redirect("mfa_setup_page")
        return _mfa_redirect("mfa_challenge_page")

    # Check if user must change their password before accessing the app
    user_metadata = UserProvisioningService.get_user_metadata(username)
    if user_metadata and user_metadata.get("force_password_change"):
        session.clear()
        session["fpc_pending"] = True
        session["fpc_username"] = username
        session["fpc_roles"] = auth_result["roles"]
        session["fpc_full_name"] = auth_result.get("full_name", username)
        session["fpc_email"] = auth_result.get("email", "")
        session["fpc_timestamp"] = time.time()
        logging.info(
            f"AUDIT: user={username} action=login_form_submit force_password_change=required ip={request.remote_addr}"
        )
        AuditLogService.log_event(
            username,
            "force_password_change",
            True,
            auth_method="form_login",
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
        )
        return redirect(url_for("change_password_page"))

    # Create session (same structure as existing auth methods)
    session.pop("logged_out", None)
    session["authenticated"] = True
    session["username"] = username
    session["roles"] = auth_result["roles"]
    session["is_admin"] = "superuser" in auth_result["roles"] or config.REQUIRED_ROLE in auth_result["roles"]
    session["full_name"] = auth_result.get("full_name", username)
    session["email"] = auth_result.get("email", "")
    session["auth_timestamp"] = time.time()
    session["last_activity"] = time.time()
    session["auth_method"] = "form_login"
    session.permanent = True

    logging.info(f"AUDIT: user={username} action=login_form_submit success=True ip={request.remote_addr}")
    AuditLogService.log_event(
        username,
        "login",
        True,
        auth_method="form_login",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    # follow-up (2026-05-20) — hard block at login for new
    # wizard signups who haven't verified email AND completed the
    # 6-step wizard. Existing users (no ``metadata.onboarding_required``
    # flag) and superuser/asset_admin bypass automatically. See
    # LoginGateService for the full evaluation order + fail-open
    # posture on ES outages.
    #
    # this moved into a shared post-auth step so the MFA
    # completion and the SSO existing-user sign-in run it too. It used
    # to live inline here, which is why it only ever ran for users
    # WITHOUT MFA (the MFA branch returns well above this line).
    if _gate_redirect := gate_redirect_after_auth(
        username,
        auth_result.get("roles") or [],
        auth_method="form_login",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    ):
        return _gate_redirect

    # Create Kibana session for embedded dashboard SSO
    kibana_sid_cookie = _create_kibana_session(username, password)

    # Redirect to originally requested page or dashboard
    requested_next = request.args.get("next")
    next_url = _safe_redirect_url(requested_next, "/dashboard")
    # auto-resume an in-flight onboarding wizard. Only fires
    # when the user has NOT explicitly asked for a non-dashboard
    # destination via ``?next=`` (a user deep-linking to /profile mid-
    # wizard still wants /profile, not the wizard). The dashboard banner
    # is the fallback resume affordance for the MFA / FPC paths and for
    # users who navigate away from the auto-redirect.
    if not requested_next or next_url == "/dashboard":
        try:
            from asset_management.web.services.wizard_resume_service import (
                WizardResumeService,
            )

            resume_state = WizardResumeService.get_resume_state(username)
            if resume_state and resume_state.auto_redirect_url:
                next_url = resume_state.auto_redirect_url
                AuditLogService.log_event(
                    username,
                    "login_wizard_resume",
                    True,
                    ip_address=request.remote_addr or "",
                    user_agent=request.headers.get("User-Agent", ""),
                    detail=(
                        f"analytics_event=login.wizard.resume "
                        f"step={resume_state.current_step} "
                        f"age_days={resume_state.age_days}"
                    ),
                )
        except Exception as e:  # ES outage / service import failure
            # Resume is a UX shortcut; a failure here falls back to
            # /dashboard which still shows the banner if the ES read
            # recovers by then. Never block login on a resume lookup.
            logging.warning("login resume lookup failed for %r: %s", username, e)

    # allow dashboards-surface paths to survive without /assets
    # prefixing so users logging in from /dashboards/* land back there.
    allowed_without_prefix = [
        "/dashboard",
        "/dashboards/",
        "/api/dashboards/",
        "/profile",
        "/signup",
        "/verify-email",
        # onboarding paths must survive without the /assets
        # prefix so the login auto-resume lands on the wizard, not on
        # /assets/onboarding/step-N which is a different route tree.
        "/onboarding/",
    ]
    needs_prefix = (
        USE_PREFIX
        and not next_url.startswith("/assets")
        and not any(next_url.startswith(path) for path in allowed_without_prefix)
    )
    if needs_prefix:
        next_url = "/assets" + next_url

    # Signal cross-tab login broadcast
    separator = "&" if "?" in next_url else "?"
    next_url = f"{next_url}{separator}just_logged_in=1"

    response = make_response(redirect(next_url))

    # Set Kibana session cookie if obtained
    if kibana_sid_cookie:
        response.set_cookie(
            "sid",
            kibana_sid_cookie,
            secure=True,
            httponly=True,
            samesite="Lax",
            path="/kibana",
        )

    return response


@analytics_bp.route("/dashboards/login", methods=["GET", "POST"])
def dashboards_login_redirect():
    """Permanent redirect from the legacy /dashboards/login URL to /login.

     collapsed the asset-manager and dashboards login flows into a
    single ``/login`` page. This handler keeps the old URL working for
    bookmarks and external links: GET → 301, POST → 308 (preserves the
    form body so a stale form action doesn't lose credentials in flight).
    """
    status = 308 if request.method == "POST" else 301
    return redirect(_canonical_login_url("/login"), code=status)


@analytics_bp.route("/logout")
def logout():
    """Clear session and redirect to login."""
    username = session.get("username")
    auth_method = session.get("auth_method")

    # kill this user's Kibana gate API key (if one was minted) so a
    # signed-out session cannot keep driving Kibana through a stale key.
    # Fail-soft by contract — logout must complete even if ES is unreachable;
    # the key's own expiration (KIBANA_GATE_KEY_TTL) is the backstop.
    if username:
        try:
            KibanaGateService.revoke_for(username, ip_address=request.remote_addr or "")
        except Exception as _kgate_err:  # pragma: no cover — revoke_for already swallows
            logging.warning("logout: kibana gate revoke failed for %s: %s", username, _kgate_err)

    # Preserve CSRF token so other open tabs can still submit the login form.
    csrf_token = session.get("csrf_token")
    session.clear()
    if csrf_token:
        session["csrf_token"] = csrf_token
    # Prevent nginx SSO from immediately re-authenticating
    session["logged_out"] = True

    if username:
        logging.info(f"AUDIT: user={username} action=logout method={auth_method}")
        AuditLogService.log_event(
            username,
            "logout",
            True,
            auth_method=auth_method or "",
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
        )

    # Preserve the return URL from referrer so the user lands back on the
    # surface they signed out from once they re-authenticate.
    from urllib.parse import urlparse

    next_url = _safe_redirect_url(request.args.get("next"), "/")
    if request.referrer:
        parsed = urlparse(request.referrer)
        if parsed.path and parsed.path not in ["/logout", "/login"]:
            next_url = _safe_redirect_url(parsed.path, "/")

    # every logout funnels through the canonical bare /login URL.
    # The asset-manager surface lives behind the /assets prefix, so url_for
    # would prepend it — bypass url_for here and build the bare path so the
    # user always sees /login in the URL bar.
    #
    # Skip the next= param when it would just be the post-login default
    # (/dashboard). login_post falls back to /dashboard on its own, so a user
    # signing out from /dashboard sees a clean /login?message=… instead of
    # the visually noisy /login?message=…&next=%2Fdashboard.
    from urllib.parse import urlencode

    login_params = {"message": "You have been signed out successfully."}
    if next_url and next_url != "/dashboard":
        login_params["next"] = next_url
    login_url = f"/login?{urlencode(login_params)}"

    # Return a brief HTML page that broadcasts logout via localStorage,
    # then redirects to the login page.  A plain 302 redirect can't run
    # JavaScript, so this intermediate page ensures other tabs are notified.
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <script>localStorage.setItem('logout',Date.now().toString());
    window.location.replace({login_url!r});</script>
    </head><body></body></html>"""
    response = make_response(html)
    response.headers["Cache-Control"] = "no-store"

    # Clean up legacy session cookie at /assets path
    cookie_name = current_app.config.get("SESSION_COOKIE_NAME", "session")
    response.delete_cookie(cookie_name, path="/assets")

    return response


# ============================================
# DASHBOARD ROUTE (auth required)
# ============================================


@analytics_bp.route("/dashboard")
def dashboard_page():
    """Display dashboard page with ECharts dashboards."""
    try:
        # Get user roles and admin status for visibility filtering
        user_roles = session.get("roles", [])
        is_admin = "superuser" in user_roles or config.REQUIRED_ROLE in user_roles

        # Fetch visible ECharts dashboards
        initial_dashboards = DashboardService.get_visible_echarts_dashboards(user_roles, is_admin)

        # Build ECharts viewer base URL with correct /assets/ prefix.
        echarts_viewer_base = url_for("dashboard_builder.index")
        if USE_PREFIX and not echarts_viewer_base.startswith("/assets"):
            echarts_viewer_base = "/assets" + echarts_viewer_base
        echarts_viewer_base = echarts_viewer_base + "view/"

        # the checklist's invite / add-client modals fetch these
        # APIs. This page is served at BARE /dashboard (PrefixMiddleware
        # whitelist), so SCRIPT_NAME is empty and url_for() would emit a bare
        # /api/invites — which nginx has no location block for (unlike
        # /api/dashboards/), so it 404s and the modal shows "Network error".
        # Force the /assets/ prefix (which nginx DOES proxy) the same way
        # echarts_viewer_base above does, so the fetch hits the proxied mount
        # regardless of which URL the page was reached through.
        def _assets_api(endpoint: str) -> str:
            u = url_for(endpoint)
            if USE_PREFIX and not u.startswith("/assets"):
                u = "/assets" + u
            return u

        invite_api_base = _assets_api("analytics.post_invite")
        invite_pending_api = _assets_api("analytics.get_invite_pending")
        invite_teams_api = _assets_api("analytics.get_invite_teams")
        client_api_base = _assets_api("analytics.post_client")

        # Get role-based filter visibility
        filter_visibility = RoleFilterService.get_filter_visibility()

        # Get allowed organizations and teams for auto-filtering
        allowed_orgs = RoleFilterService.get_allowed_organizations()
        allowed_teams = RoleFilterService.get_allowed_teams()

        # For org users, convert to team-based access
        if allowed_orgs and not allowed_teams:
            allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
            if allowed_teams:
                logging.info(f"Org user - converted {len(allowed_orgs)} org(s) to {len(allowed_teams)} team(s)")

        auto_filter_required = allowed_orgs is not None or allowed_teams is not None

        # Get partner organizations for multi-org users
        partner_orgs = RoleFilterService.get_partner_organizations()
        if partner_orgs:
            logging.info(f"Partner orgs for user: {partner_orgs}")
        else:
            logging.info(f"No partner orgs found for user, allowed_orgs={allowed_orgs}")

        # Audit log auto-filter application
        if allowed_orgs or allowed_teams:
            RoleFilterService.get_filter_for_current_user()
            username = session.get("username", "unknown")
            logging.info(f"AUDIT: user={username} action=dashboard_auto_filter orgs={allowed_orgs} teams={allowed_teams}")

        # Server-rendered "today" (UTC) so the custom-range date picker caps
        # the upper bound on the same reference the server uses for date math
        # (e.g. `now` / `now-90d/d`). Client-local "today" can drift a day in
        # extreme time zones; using the server value keeps the two in sync.
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # per-team time-range presets. Dashboards are shared across
        # many teams, so the per-team control is the Team filter (#team-filter);
        # the client rebuilds the time-range dropdown from the team selected
        # there. Build a title→presets map over the viewer's allowed teams
        # (get_allowed_teams returns titles, or None for an unrestricted viewer
        # who can pick any team). ``team_presets_by_title`` holds only teams with
        # a config; a selected team not in the map (or "All Teams") falls back to
        # ``default_time_presets``. Fail-soft to {} on any ES error.
        _preset_team_titles = allowed_teams
        if _preset_team_titles is None:
            # Unrestricted viewer: the Team filter can list any team, so map
            # every configured team by reading the field directly.
            _preset_team_titles = time_preset_service.all_configured_team_titles()
        team_presets_by_title = time_preset_service.presets_by_team_title(_preset_team_titles)
        default_time_presets = time_preset_service.default_presets()

        # surface the "Connect your first source" checklist when
        # the user skipped step-4 of the onboarding wizard. The skipped
        # session is the load-bearing signal; clicking the checklist CTA
        # routes back to /onboarding/step-6 which reactivates the same doc.
        from asset_management.web.services.wizard_session_service import (
            WizardSessionService,
        )

        username = session.get("username", "")

        # follow-up (2026-05-20) — "Continue setup" banner
        # "Your dashboard is empty" first-source recovery
        # block both removed from the dashboard template. The login gate
        # now hard-blocks incomplete-wizard users from /dashboard via
        # LoginGateService → WIZARD_INCOMPLETE, so neither surface could
        # ever render in practice. WizardResumeService stays in use by
        # the login auto-redirect path (login_post +
        # /onboarding/verify-required/check), but the dashboard render
        # no longer consults it.

        # persistent yellow checklist card (right rail). The
        # discovery surface for ongoing setup. Org-scoped progress via
        # WorkspaceMembershipService; per-user dismissibility once
        # progress >= 80% AND remaining > 1.
        checklist_state = DashboardChecklistService.get_state(username) if username else None

        # workspace-scoped menu order for the user-facing
        # sidebar. Replaces the prior hardcoded ``platformDefinitions``
        # array in templates/dashboards.html. Resolved per request so
        # workspace-admin reorders show up on the next page load. An
        # empty list (no workspace, ES outage) lets the template fall
        # back to alphabetical order over the menus implied by the
        # ``dashboard.menu`` strings — the existing earlier path
        # for non-platform menu names.
        workspace_menus: list[dict] = []
        # per-workspace opt-in for the legacy data-driven menu
        # hiding (updatePlatformVisibility in dashboards.html). Default
        # False: flag-visible dashboards stay in the menu even with zero
        # data, so the in-dashboard "pull data" control is reachable. Only
        # the legacy Kavanah Media workspace is seeded True to preserve its
        # historical hide-empty-dashboards behavior. Fail-soft to False
        # (ES outage / no workspace / missing field) — the safe default is
        # "show", which can never strand a user behind a hidden dashboard.
        hide_empty_dashboards = False
        if username:
            try:
                from asset_management.web.services.workspace_membership_service import (
                    WorkspaceMembershipService,
                )
                from asset_management.web.services.workspace_menu_service import (
                    WorkspaceMenuService,
                )

                # (2026-05-26): primary-then-resolver workspace
                # resolution. ``get_primary_workspace`` returned None for a
                # team-grant-only user (no workspace doc), so those users
                # fell through to ``workspace_menus=[]`` and saw the
                # alphabetical fallback instead of the workspace owner's
                # configured ``menu_position`` order. Their workspace is
                # still resolvable — ``get_allowed_workspaces`` walks
                # team_esids/org_esids → workspace via the ESID
                # join. Prefer the primary lookup (preserves the working
                # owner / member case exactly) and only fall back to the
                # resolver walk when primary yields nothing.
                #
                # (2026-06-02): repointed
                # ``get_primary_workspace`` onto the workspace ROLE index,
                # taking ``[0]`` of the user's role docs. A superuser /
                # grant-holder with workspace-role docs on OTHER workspaces
                # (none on the workspace whose menus they actually view,
                # none ``is_primary``) now gets a wrong-but-non-None
                # workspace whose ``list_menus`` is empty — so the
                # None-gated fallback was skipped and the alpha
                # order returned. Fire the resolver fallback ALSO when the
                # primary workspace yields no menus, and pick the first
                # resolved workspace that actually has menus. The
                # single-workspace constraint means the resolver list is
                # usually 0 or 1 element for this cohort.
                # resolve the SAME workspace the Dashboard Builder
                # curates (is_primary role -> workspace data-grant ->
                # positional primary). Previously this used
                # get_primary_workspace directly, which returns an arbitrary
                # positional [0] role workspace; for a superuser whose [0]
                # role differs from their data-grant workspace, the read-only
                # sidebar rendered a DIFFERENT workspace's menu order than the
                # Builder edited, so reorders never appeared here.
                ws_esid = WorkspaceMembershipService.resolve_curation_workspace(username)
                if ws_esid:
                    workspace_menus = WorkspaceMenuService.list_menus(ws_esid)
                if not workspace_menus:
                    for candidate in RoleFilterService.get_allowed_workspaces() or []:
                        if candidate == ws_esid:
                            continue
                        candidate_menus = WorkspaceMenuService.list_menus(candidate)
                        if candidate_menus:
                            ws_esid = candidate
                            workspace_menus = candidate_menus
                            break

                # scope the sidebar's dashboards to the SAME workspace
                # its menu order resolves to. get_visible_echarts_dashboards
                # returns the dashboards of every workspace the user can reach
                #, so cross-workspace dashboards whose
                # ``menu`` titles have no row in ``ws_esid`` leaked into the
                # alphabetical orphan tail — e.g. workspace-a's "Google
                # Analytics" appearing under a kavanah-media sidebar. Narrow to
                # ws_esid here so the read-only view matches the (workspace-
                # scoped) Builder. Legacy dashboards with no ``workspace_esid``
                # stamp pass through (can't be attributed to a workspace yet);
                # this only filters OUT dashboards stamped to a different one.
                if ws_esid:
                    initial_dashboards = [
                        d for d in initial_dashboards if not d.get("workspace_esid") or d.get("workspace_esid") == ws_esid
                    ]

                if ws_esid:
                    # read the per-workspace opt-in off the
                    # resolved workspace. Cheap (one es.get on a doc we've
                    # already located the id for); fail-soft to the default
                    # False inside the service.
                    hide_empty_dashboards = WorkspaceMembershipService.hide_empty_dashboards_for_workspace(ws_esid)
            except Exception as e:  # ES outage / bootstrap race — fall back to alpha order.
                logging.warning(f"workspace_menus load failed for {username}: {e}")
                workspace_menus = []

        # resolve the active workspace name so the docs-capture
        # sanitization gate has a demo-workspace anchor for this page. It is
        # NOT rendered as visible text (the on-screen label was removed for
        # layout reasons); the template emits it into a hidden
        # ``data-docs-workspace`` attribute on #dashboard-toolbar that the
        # capture reads. Fail-soft: any resolution error just omits the
        # attribute, the page is unaffected, and the capture gate fail-closes
        # (no marker => nothing publishes), which is the safe outcome. Reuses
        # the Builder's workspace resolver for consistency.
        dashboard_workspace = ""
        try:
            try:
                from web.blueprints.dashboard_builder.routes import (
                    _selectable_workspaces_for_user,
                    _default_workspace_esid,
                )
            except ImportError:
                from asset_management.web.blueprints.dashboard_builder.routes import (
                    _selectable_workspaces_for_user,
                    _default_workspace_esid,
                )

            _ws = _selectable_workspaces_for_user()
            _ws_default = _default_workspace_esid(_ws)
            dashboard_workspace = next(
                (w.get("title", "") for w in _ws if w.get("esid") == _ws_default),
                (_ws[0].get("title", "") if _ws else ""),
            )
        except Exception as e:  # noqa: BLE001 — never block the render
            logging.warning(f"workspace anchor load failed: {e}")

        response = make_response(
            render_template(
                "dashboards.html",
                dashboard_workspace=dashboard_workspace,
                initial_dashboards=initial_dashboards,
                workspace_menus=workspace_menus,
                hide_empty_dashboards=hide_empty_dashboards,
                filter_visibility=filter_visibility,
                auto_filter_orgs=allowed_orgs,
                auto_filter_teams=allowed_teams,
                auto_filter_required=auto_filter_required,
                user_roles=user_roles,
                partner_orgs=partner_orgs,
                echarts_viewer_base=echarts_viewer_base,
                today_utc=today_utc,
                team_presets_by_title=team_presets_by_title,
                default_time_presets=default_time_presets,
                checklist=checklist_state,
                # shared with _invite_modal.html so the JS
                # 280-char cap and the server-side NOTE_MAX_LENGTH stay
                # in lock-step. Sourced from the service constant so a
                # future bump lives in one place.
                invite_note_max=NOTE_MAX_LENGTH,
                # /assets-prefixed API URLs for the invite +
                # add-client modals (this page is served at bare /dashboard,
                # where url_for alone yields an unproxied bare /api/... path).
                invite_api_base=invite_api_base,
                invite_pending_api=invite_pending_api,
                invite_teams_api=invite_teams_api,
                client_api_base=client_api_base,
            )
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        return response
    except Exception as e:
        logging.error(f"Failed to load dashboards page: {e}")
        return (
            render_template(
                "error.html",
                error_code=500,
                error_title="Failed to Load Dashboards",
                error_message="Something went wrong loading this page. Try again in a moment; if it keeps happening, contact support.",
            ),
            500,
        )


# ============================================
# DASHBOARD CHECKLIST CARD
# ============================================


def _emit_checklist_event(event_name: str, audit_action: str, extra: dict | None = None) -> None:
    """AC3 — emit one canonical ``checklist.{event}`` envelope.

    Fired on every successful checklist interaction (dismiss / undismiss
    / skip / unskip / collapse / expand). Reads the user's wizard_session
    by id REGARDLESS of status so the cohort still anchors to the
    onboarding week even though the wizard has terminated by the time
    the user touches the checklist. Fail-soft: any error in the emitter
    logs a warning and is swallowed (never blocks the checklist route).
    """
    username = session.get("username", "")
    if not username:
        return
    try:
        ctx = WizardAnalyticsService.build_context_for_user(username=username)
        WizardAnalyticsService.emit_event(
            event_name=event_name,
            audit_action=audit_action,
            context=ctx,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra=extra,
        )
    except Exception as e:  # noqa: BLE001 — emitter must never block the checklist
        logging.warning("checklist emit %r raised for %r: %s", event_name, username, e)


@analytics_bp.route("/dashboard/checklist/dismiss", methods=["POST"])
def dashboard_checklist_dismiss():
    """Persist the user's dismissal of the dashboard checklist card.

    Requires an authenticated session (verify_auth runs first) and the
    standard CSRF token (NOT added to _CSRF_EXEMPT_ENDPOINTS so the
    global hook enforces it). Returns 400 with ``below_threshold`` when
    the user is under 80% complete — defense in depth so a forged POST
    can't dismiss the card before the user finished setup.
    """
    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"success": False, "error": "not_authenticated"}), 401

    result = DashboardChecklistService.dismiss(username)
    if not result.get("success"):
        error = result.get("error") or "dismiss_failed"
        status = 400 if error in ("below_threshold", "no_workspace") else 500
        return jsonify(result), status
    _emit_checklist_event("checklist.dismissed", "checklist_dismissed")
    return jsonify(result), 200


@analytics_bp.route("/dashboard/checklist/undismiss", methods=["POST"])
def dashboard_checklist_undismiss():
    """Restore the checklist card after a prior dismissal.

    Idempotent. Same auth + CSRF gates as the dismiss surface.
    """
    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"success": False, "error": "not_authenticated"}), 401

    result = DashboardChecklistService.undismiss(username)
    if not result.get("success"):
        return jsonify(result), 500
    _emit_checklist_event("checklist.undismissed", "checklist_undismissed")
    return jsonify(result), 200


# ============================================
# DASHBOARD CHECKLIST PER-ITEM SKIP
# ============================================


def _checklist_item_id_from_request() -> str:
    """Extract ``item_id`` from a skip/unskip POST.

    Accepts either a JSON body (``{"item_id": "..."}``) or a form-
    encoded body (``item_id=...``) so the inline template JS can post
    a plain JSON body while form-encoded probes also reach the route.
    """
    payload = request.get_json(silent=True) or {}
    item_id = payload.get("item_id") if isinstance(payload, dict) else ""
    if not item_id:
        item_id = request.form.get("item_id", "") or request.args.get("item_id", "")
    return (item_id or "").strip()


@analytics_bp.route("/dashboard/checklist/skip", methods=["POST"])
def dashboard_checklist_skip():
    """Persist a per-item skip row.

    Body: ``{"item_id": "<id>"}``. Auth + CSRF gates apply (NOT added
    to ``_CSRF_EXEMPT_ENDPOINTS``). Returns 400 for an unknown item id
    so a typo'd POST surfaces in the JSON rather than landing a stray
    row.
    """
    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"success": False, "error": "not_authenticated"}), 401

    item_id = _checklist_item_id_from_request()
    result = DashboardChecklistService.skip(username, item_id)
    if not result.get("success"):
        error = result.get("error") or "skip_failed"
        status = 400 if error in ("unknown_item", "item_id_required", "no_workspace") else 500
        return jsonify(result), status
    _emit_checklist_event("checklist.item.skipped", "checklist_item_skipped", extra={"item_id": item_id})
    return jsonify(result), 200


@analytics_bp.route("/dashboard/checklist/unskip", methods=["POST"])
def dashboard_checklist_unskip():
    """Re-open a previously skipped checklist item.

    Body: ``{"item_id": "<id>"}``. Idempotent — re-opening an item the
    user hasn't skipped returns 200 (no row to delete is success per
    the service contract).
    """
    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"success": False, "error": "not_authenticated"}), 401

    item_id = _checklist_item_id_from_request()
    result = DashboardChecklistService.unskip(username, item_id)
    if not result.get("success"):
        error = result.get("error") or "unskip_failed"
        status = 400 if error in ("unknown_item", "item_id_required", "no_workspace") else 500
        return jsonify(result), status
    _emit_checklist_event("checklist.item.unskipped", "checklist_item_unskipped", extra={"item_id": item_id})
    return jsonify(result), 200


# ============================================
# DASHBOARD CHECKLIST PER-ITEM CLICK
# ============================================


@analytics_bp.route("/dashboard/checklist/track-click", methods=["POST"])
def dashboard_checklist_track_click():
    """Fire a canonical ``checklist.item.clicked`` event before navigation.

    The dashboard checklist rows are anchor tags; clicking one navigates
    immediately (either to ``/onboarding/step-6`` for resume rows or to
    the in-page invite/add-client modal sentinels). The browser-side JS
    fires a ``navigator.sendBeacon`` to this route before navigation so
    the marketing funnel can answer "click-through rate per checklist
    row" — distinct from the downstream ``wizard.stepN.entered`` signal
    (which conflates every entry path) and the "saw the row" impression
    signal (a future follow-up; today we have nothing between render
    and click).

    Body: ``{"item_id": "<id>", "csrf_token": "<token>"}``. Validated
    against :meth:`DashboardChecklistService._all_known_item_ids` so a
    forged POST with an unknown id is rejected (400) rather than
    landing a spurious row in the funnel.

    CSRF: this route is in ``_CSRF_EXEMPT_ENDPOINTS`` because
    ``navigator.sendBeacon`` cannot set custom headers (so the global
    ``X-CSRF-Token`` gate would block every beacon). We validate the
    JSON-body ``csrf_token`` in-route so beacon and fetch-keepalive
    callers both pass through the same gate.

    The endpoint deliberately has NO ES write beyond the audit log:
    click-throughs are an analytics-only signal; persisting them in a
    per-(user,item) doc would duplicate the audit log without adding
    query value.
    """
    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"success": False, "error": "not_authenticated"}), 401

    payload = request.get_json(silent=True) or {}
    body_csrf = ""
    if isinstance(payload, dict):
        body_csrf = str(payload.get("csrf_token", "") or "")
    # Header / form-field paths still work for non-beacon callers.
    header_csrf = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf_token", "")
    csrf_token = body_csrf or header_csrf
    if not auth_module.validate_csrf_token(csrf_token):
        return jsonify({"success": False, "error": "csrf_invalid"}), 403

    item_id = _checklist_item_id_from_request()
    if not item_id:
        return jsonify({"success": False, "error": "item_id_required"}), 400
    if item_id not in DashboardChecklistService._all_known_item_ids():
        return jsonify({"success": False, "error": "unknown_item"}), 400

    _emit_checklist_event(
        "checklist.item.clicked",
        "checklist_item_clicked",
        extra={"item_id": item_id},
    )
    return jsonify({"success": True, "error": None}), 200


# ============================================
# DASHBOARD CHECKLIST COLLAPSE / EXPAND
# ============================================


@analytics_bp.route("/dashboard/checklist/collapse", methods=["POST"])
def dashboard_checklist_collapse():
    """Persist an explicit "I want the card collapsed" choice.

    Same auth + CSRF gates as the dismiss surface. No threshold guard:
    a user can collapse the card at any progress level. The persisted
    choice survives page refreshes because the next ``get_state`` call
    reads the field and renders the slim header variant.
    """
    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"success": False, "error": "not_authenticated"}), 401

    result = DashboardChecklistService.collapse(username)
    if not result.get("success"):
        error = result.get("error") or "collapse_failed"
        status = 400 if error in ("no_workspace", "username_required", "invalid_state") else 500
        return jsonify(result), status
    _emit_checklist_event("checklist.collapsed", "checklist_collapsed")
    return jsonify(result), 200


@analytics_bp.route("/dashboard/checklist/expand", methods=["POST"])
def dashboard_checklist_expand():
    """Persist an explicit "I want the card expanded" choice.

    Mirrors ``dashboard_checklist_collapse`` — same auth + CSRF gates,
    same threshold-free contract. Once written, the next ``get_state``
    sees ``collapse_state="expanded"`` and overrides the auto-rule
    (which would otherwise collapse the card at >=80% progress). This
    is what makes the AC's "Expand state survives page refresh" bullet
    land — the user choice is durable until they change it.
    """
    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"success": False, "error": "not_authenticated"}), 401

    result = DashboardChecklistService.expand(username)
    if not result.get("success"):
        error = result.get("error") or "expand_failed"
        status = 400 if error in ("no_workspace", "username_required", "invalid_state") else 500
        return jsonify(result), status
    _emit_checklist_event("checklist.expanded", "checklist_expanded")
    return jsonify(result), 200


# landing page for checklist items whose real surface lives
# in a future epic (schedule_weekly_summary, set_conversion_goals). The
# AC requires every checklist item route to a real-or-stub destination
# (never 404). When the real surface ships (separate ticket per item)
# the corresponding ChecklistItem.action_url moves to point at it and
# the entry here becomes unreachable. Whitelisted in
# check_route_authorization so users without asset_admin (the
# checklist card itself renders for every authenticated user) don't
# 403 on click-through.
_CHECKLIST_COMING_SOON_TITLES = {
    "schedule_weekly_summary": ("Schedule weekly summary", "Coming soon — weekly summary scheduling."),
    "set_conversion_goals": ("Set conversion goals", "Coming soon — conversion goal targets."),
    # conditional-item stubs. Their visibility predicates are
    # gated False today (new-device tracking + trial-end timestamps
    # ship in separate epics) so these entries are defensive — they
    # surface a friendly page if the row is ever forced visible before
    # the real destination lands.
    "enable_2fa": ("Enable two-factor authentication", "Coming soon — 2FA enrollment from the dashboard."),
    "connect_billing": ("Connect billing", "Coming soon — billing setup from the dashboard."),
}


@analytics_bp.route("/onboarding/checklist/coming-soon")
def checklist_coming_soon():
    """Friendly placeholder for checklist items whose real destination
    ships in a separate epic.

    Read the item id from ``?item=<id>``; falls back to a generic
    title if the id is unknown so a typo'd query string still renders
    a 200 instead of bouncing to an error page. The page is a dead-end
    by design — its only affordance is "Back to dashboard".
    """
    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))
    item_id = request.args.get("item", "")
    title, body = _CHECKLIST_COMING_SOON_TITLES.get(
        item_id,
        ("Coming soon", "This setup step is being built — check back shortly."),
    )
    return render_template(
        "checklist_coming_soon.html",
        item_title=title,
        item_body=body,
    )


# ============================================
# INVITE TEAMMATES MODAL
# ============================================
#
# Sender-side surface for the in-app invite modal. Consumes the
# `?invite=open` signal and the `invite_teammate` checklist row.
#
# Auth: legacy `_require_admin` backstop on both routes. The proper gate
# is `Permission.USER_INVITE` once wires resolver enforcement —
# swap the `auth_module._require_admin()` call below for the new
# permission check at that point. The enum value already exists
# (permissions_enum.Permission.USER_INVITE); only the call-site changes.
#
# CSRF: NOT in `_CSRF_EXEMPT_ENDPOINTS`. The global before-request hook
# validates the token on every POST.
#
# Audit events fire on both `opened` (GET pending — opportunistic) and
# `sent` (POST success). Literal `analytics_event=invite.modal.*`
# substrings so marketing's Kibana funnel slice catches the event by
# string match, same convention established for the wizard.


def _require_invite_permission() -> bool:
    """Permission gate for the invite routes.

    Until wires ``Permission.USER_INVITE`` enforcement, gate on
    the legacy admin check. Returns True iff the user is allowed; the
    caller jsonifies a 403 on False so the modal can render an inline
    "permission denied" error rather than a redirect.

    this proves a scoped admin role ANYWHERE, so it is only a
    pre-gate. ``post_invite`` re-checks USER_INVITE in the resolved target
    workspace and caps the grantable role (``InviteService.inviter_grant_refusal``).

    Source-pin: when lands, replace the body with a call to the
    permission resolver:
        from web.services.permission_resolver import PermissionResolver
        return PermissionResolver.user_has_permission(
            username, Permission.USER_INVITE, workspace_esid
        )
    """
    roles = session.get("roles", []) or []
    if "asset_admin" in roles or "superuser" in roles:
        return True
    # Mirror _require_admin's scoped-admin allowance so workspace
    # owners / admins can invite without holding cluster-admin roles.
    username = session.get("username", "")
    if not username:
        return False
    try:
        from asset_management.web.app import _user_has_scoped_admin_role
    except ImportError:
        try:
            from web.app import _user_has_scoped_admin_role  # type: ignore
        except ImportError:
            return False
    return _user_has_scoped_admin_role(username)


def _invite_read_refused(username: str, workspace_esid: str) -> bool:
    """True when the invite modal's read side (pending lookup, team
    picker) must refuse: the caller is not a platform admin (superuser /
    asset_admin, unchanged) and does not hold USER_INVITE in the workspace the
    request resolved to. ``_require_invite_permission`` proves a scoped admin
    role ANYWHERE; this binds the read to the workspace it reads, as
    ``post_invite`` does for the send.
    """
    session_roles = session.get("roles", []) or []
    if "asset_admin" in session_roles or "superuser" in session_roles:
        return False
    if InviteService.inviter_may_invite_in(username, workspace_esid):
        return False
    logging.warning(
        "AUDIT: action=invite_read_refused inviter=%s workspace=%s endpoint=%s",
        username,
        workspace_esid,
        request.endpoint,
    )
    return True


@analytics_bp.route("/api/invites", methods=["POST"])
def post_invite():
    """Send (or resend) an invite from the in-app modal.

    Body: ``{"email": "...", "role_esid": "...", "note": "..." (optional)}``.

    Inline-state response shapes (the modal JS dispatches on
    ``status``):

    * 200 ``{"status": "sent", "email": "...", "analytics_event": "invite.modal.sent"}``
    * 200 ``{"status": "resent", "email": "...", "existing_created_at": "..."}``
    * 409 ``{"status": "already_member"}``
    * 400 ``{"status": "invalid", "reason": "bad_email|bad_role|note_too_long|missing_session"}``
    * 500 ``{"status": "send_failed", "error": "..."}`` — doc IS written;
      retry hits the resend path (no duplicate doc).
    * 500 ``{"status": "es_unavailable", "error": "..."}`` — no doc
      written; safe to retry.

    Audit event ``invite.modal.sent`` fires only on a sent/resent
    branch (the AC names success specifically). Failed sends / invalid
    inputs do not fire so the funnel isn't polluted with form-errors.
    """
    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"status": "invalid", "reason": "not_authenticated"}), 401

    if not _require_invite_permission():
        return jsonify({"status": "invalid", "reason": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"status": "invalid", "reason": "bad_body"}), 400
    email = payload.get("email") or ""
    role_esid = payload.get("role_esid") or ""
    note = payload.get("note") or ""
    # optional team scope picked by the inviter. Empty string
    # (or absent) means "any team / let the receiver decide"; the
    # service validates cross-workspace forgeries before persisting.
    team_esid_raw = payload.get("team_esid") or ""
    team_esid = team_esid_raw.strip() if isinstance(team_esid_raw, str) else ""

    # resolve the inviter's WORKING workspace, not the arbitrary
    # positional get_primary_workspace([0]). For a multi-workspace
    # superuser/agency-admin with no is_primary flag, [0] was an arbitrary
    # created_at pick, so an invite could land in the wrong workspace.
    # resolve_curation_workspace (is_primary -> workspace-grant -> org-walk
    # -> positional) is the same non-arbitrary context the Builder/creator
    # paths use; single-workspace behavior is unchanged.
    workspace_esid = WorkspaceMembershipService.resolve_curation_workspace(username)
    if not workspace_esid:
        # The modal should never have surfaced for a user without a
        # workspace (the checklist gate requires one), but defend in
        # depth: a forged POST without a workspace can't write an
        # orphan invite doc.
        return jsonify({"status": "invalid", "reason": "no_workspace"}), 400

    # ``_require_invite_permission`` proves a scoped admin role
    # ANYWHERE; the invite lands in the workspace resolved above. For a
    # tenant-side inviter, re-check USER_INVITE in THAT workspace and refuse
    # a role at or above their own there (an admin inviting an owner was a
    # privilege escalation). Platform admins keep their existing behaviour;
    # the role allow-list still applies to them inside create_or_resend.
    session_roles = session.get("roles", []) or []
    if "asset_admin" not in session_roles and "superuser" not in session_roles:
        refusal = InviteService.inviter_grant_refusal(username, workspace_esid, role_esid)
        if refusal == "bad_role":
            return jsonify({"status": "invalid", "reason": "bad_role"}), 400
        if refusal:
            logging.warning(
                "AUDIT: action=invite_refused reason=%s inviter=%s workspace=%s",
                refusal,
                username,
                workspace_esid,
            )
            return jsonify({"status": "invalid", "reason": refusal}), 403

    result = InviteService.create_or_resend(
        inviter_username=username,
        workspace_esid=workspace_esid,
        email=email,
        role_esid=role_esid,
        note=note,
        secret_key=current_app.config.get("SECRET_KEY", ""),
        team_esid=team_esid or None,
    )
    status = result.get("status")

    if status in ("sent", "resent"):
        # fire the success audit event. Literal
        # `analytics_event=invite.modal.sent` substring so marketing's
        # Kibana funnel slice catches the event by string match. role=
        # is the second slice the AC names so we can answer "what role
        # do owners default-grant invitees?" without a join.
        AuditLogService.log_event(
            username,
            "invite_modal_sent",
            True,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            target_user=result.get("email", ""),
            detail=(f"analytics_event=invite.modal.sent role={role_esid} status={status}"),
        )
        # Echo the analytics event back to the client so the modal can
        # also emit a client-side beacon if marketing wires one up.
        result["analytics_event"] = "invite.modal.sent"
        if status == "resent":
            existing = result.pop("existing", None)
            if isinstance(existing, dict):
                result["existing_created_at"] = existing.get("created_at")
        return jsonify(result), 200

    if status == "already_member":
        return jsonify(result), 409
    if status == "invalid":
        return jsonify(result), 400
    if status == "send_failed":
        return jsonify(result), 500
    if status == "es_unavailable":
        return jsonify(result), 500
    return jsonify(result), 500


@analytics_bp.route("/api/invites/pending", methods=["GET"])
def get_invite_pending():
    """Look up the pending invite for ``?email=...``.

    Returns the existing pending invite's ``created_at`` + ``role_esid``
    so the modal can render an informed "Resend?" prompt. Also fires
    the ``invite.modal.opened`` audit event with ``source=`` so the
    funnel can be sliced by entry point.

    ``source`` is one of ``wizard|checklist|header|direct``. Defaults to
    ``direct`` when the modal opens without a known producer; the
     wizard URL passes ``source=wizard``, the checklist row
    passes ``source=checklist``.

    Response shapes:
    * 200 ``{"status": "pending", "created_at": "...", "role_esid": "..."}``
    * 200 ``{"status": "none"}`` — no pending invite for this email
    * 400 ``{"status": "invalid"}`` — missing/blank email
    """
    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"status": "invalid", "reason": "not_authenticated"}), 401

    if not _require_invite_permission():
        return jsonify({"status": "invalid", "reason": "forbidden"}), 403

    email = (request.args.get("email") or "").strip()
    source = (request.args.get("source") or "direct").strip()
    # Constrain ``source`` to the documented enum so a typo'd query
    # string doesn't pollute the analytics slice with one-off values.
    if source not in ("wizard", "checklist", "header", "direct"):
        source = "direct"

    # fire the opened audit event. We do this unconditionally
    # (regardless of whether the email lookup turns up a pending row)
    # because the AC's funnel intent is "did the modal open?", not "was
    # there a pending invite?". The literal
    # `analytics_event=invite.modal.opened` substring is what the
    # marketing Kibana query keys off.
    AuditLogService.log_event(
        username,
        "invite_modal_opened",
        True,
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"analytics_event=invite.modal.opened source={source}",
    )

    if not email:
        return jsonify({"status": "invalid", "reason": "missing_email"}), 400

    # working workspace, not positional [0] (see post_invite).
    workspace_esid = WorkspaceMembershipService.resolve_curation_workspace(username)
    if not workspace_esid:
        return jsonify({"status": "invalid", "reason": "no_workspace"}), 400
    if _invite_read_refused(username, workspace_esid):
        return jsonify({"status": "invalid", "reason": "forbidden"}), 403

    pending = InviteService.get_pending(workspace_esid, email)
    if pending is None:
        return jsonify({"status": "none"}), 200
    return (
        jsonify(
            {
                "status": "pending",
                "created_at": pending.get("created_at"),
                "last_sent_at": pending.get("last_sent_at"),
                "role_esid": pending.get("role_esid"),
            }
        ),
        200,
    )


@analytics_bp.route("/api/invites/teams", methods=["GET"])
def get_invite_teams():
    """List the inviter's workspace teams for the modal picker.

    Response shape:

    * 200 ``{"status": "ok", "teams": [{"team_esid": "...", "team_title": "..."}, ...]}``
    * 200 ``{"status": "ok", "teams": []}`` — workspace has no teams;
      the modal collapses the picker and the invitee falls into the
      auto-create-General branch on consume.
    * 401 ``{"status": "invalid", "reason": "not_authenticated"}``
    * 403 ``{"status": "invalid", "reason": "forbidden"}`` — caller lacks
      USER_INVITE.
    * 400 ``{"status": "invalid", "reason": "no_workspace"}``
    """
    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"status": "invalid", "reason": "not_authenticated"}), 401

    if not _require_invite_permission():
        return jsonify({"status": "invalid", "reason": "forbidden"}), 403

    # working workspace, not positional [0] (see post_invite).
    workspace_esid = WorkspaceMembershipService.resolve_curation_workspace(username)
    if not workspace_esid:
        return jsonify({"status": "invalid", "reason": "no_workspace"}), 400
    if _invite_read_refused(username, workspace_esid):
        return jsonify({"status": "invalid", "reason": "forbidden"}), 403

    teams = InviteService.list_workspace_teams(workspace_esid)
    return jsonify({"status": "ok", "teams": teams}), 200


_CLIENT_ADMIN_WORKSPACE_ROLES = frozenset({"workspace_owner", "workspace_admin"})


@analytics_bp.route("/api/clients", methods=["POST"])
def post_client():
    """Add a new client org to the caller's agency workspace.

    Body: ``{"client_title": "...", "currency": "USD"}``.

    Response shapes (the modal JS dispatches on ``status``):

    * 200 ``{"status": "created", "org_esid": "...", "team_esid": "...",
            "client_title": "...", "analytics_event": "add_client.modal.created"}``
    * 200 ``{"status": "exists", "org_esid": "...", "team_esid": "...",
            "client_title": "..."}`` — idempotent re-POST.
    * 400 ``{"status": "invalid", "reason": "missing_title|title_too_long|bad_currency|no_workspace"}``
    * 403 ``{"status": "invalid", "reason": "not_agency"}``
    * 403 ``{"status": "invalid", "reason": "not_workspace_admin"}``
    * 500 ``{"status": "es_unavailable", "error": "..."}``

    Gates:

    * Session-authenticated (no anonymous POSTs).
    * Caller must be on an agency workspace
      (``WorkspaceMembershipService.is_agency_for_user``) — the same
      signal the checklist's ``_should_show_add_client`` predicate uses,
      so the modal can only fire for users who'd legitimately see the
      row.
    * Global CSRF middleware checks ``X-CSRF-Token`` header or
      ``csrf_token`` form field. The modal JS sends the header.

    Audit event ``add_client.modal.created`` fires only on a fresh
    create (not on the idempotent ``exists`` branch) so the funnel
    counts new clients, not refresh-storms.
    """
    from flask import jsonify

    try:
        from web.services.onboarding_service import OnboardingService  # type: ignore[no-redef]
    except ImportError:
        from asset_management.web.services.onboarding_service import OnboardingService

    username = session.get("username", "")
    if not username:
        return jsonify({"status": "invalid", "reason": "not_authenticated"}), 401

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"status": "invalid", "reason": "bad_body"}), 400
    client_title = (payload.get("client_title") or "").strip()
    currency = (payload.get("currency") or "").strip().upper()

    if not client_title:
        return jsonify({"status": "invalid", "reason": "missing_title"}), 400
    if len(client_title) > 120:
        return jsonify({"status": "invalid", "reason": "title_too_long"}), 400
    # Lenient currency: 3 letters or empty (defaults applied server-side).
    # Matches the wizard's "lenient" posture for the More-options block
    # — power-user surface, not a blocking gate.
    if currency and (len(currency) != 3 or not currency.isalpha()):
        return jsonify({"status": "invalid", "reason": "bad_currency"}), 400

    # working workspace, not positional [0] (see post_invite).
    # An agency admin owning several workspaces adds the client to their
    # ACTUAL agency workspace, not an arbitrary created_at-first one.
    workspace_esid = WorkspaceMembershipService.resolve_curation_workspace(username)
    if not workspace_esid:
        return jsonify({"status": "invalid", "reason": "no_workspace"}), 400

    # Same gate as the checklist row visibility — only agency-mode
    # workspace owners can add clients. A non-agency user hitting this
    # route directly (forged POST or stale browser state) gets 403.
    try:
        is_agency = WorkspaceMembershipService.is_agency_for_user(username)
    except Exception as e:  # noqa: BLE001 — defensive boundary
        logging.warning("post_client is_agency check failed for %s: %s", username, e)
        is_agency = False
    if not is_agency:
        return jsonify({"status": "invalid", "reason": "not_agency"}), 403

    # the agency flag says what the workspace IS, not what the caller
    # may do in it — any member (a Viewer) passed, and the new client org is
    # stamped with the caller as its owner. Require workspace_owner /
    # workspace_admin on the workspace the client is added to. Superusers keep
    # their path (no per-workspace role doc, as in _require_workspace_admin).
    if "superuser" not in (session.get("roles", []) or []):
        caller_role = WorkspaceMembershipService.get_role(username, workspace_esid)
        if caller_role not in _CLIENT_ADMIN_WORKSPACE_ROLES:
            logging.warning(
                "AUDIT: action=add_client_refused reason=not_workspace_admin user=%s workspace=%s",
                username,
                workspace_esid,
            )
            return jsonify({"status": "invalid", "reason": "not_workspace_admin"}), 403

    provision = OnboardingService.add_client_to_workspace(
        username,
        workspace_esid=workspace_esid,
        client_title=client_title,
    )
    if provision.errors:
        if "elasticsearch unavailable" in provision.errors:
            return jsonify({"status": "es_unavailable", "error": ";".join(provision.errors)}), 500
        return jsonify({"status": "invalid", "reason": ";".join(provision.errors)}), 400

    created = bool(provision.org_created)
    status = "created" if created else "exists"
    if created:
        # Literal `analytics_event=add_client.modal.created` substring
        # so the marketing Kibana funnel slice catches the event by
        # string match. currency= is the second slice for "what
        # currencies do agencies most often onboard clients in?".
        AuditLogService.log_event(
            username,
            "add_client_modal_created",
            True,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            detail=(f"analytics_event=add_client.modal.created " f"currency={currency or 'unset'} workspace={workspace_esid}"),
        )

    body = {
        "status": status,
        "org_esid": provision.org_esid,
        "team_esid": provision.team_esid,
        "client_title": client_title,
    }
    if created:
        body["analytics_event"] = "add_client.modal.created"
    return jsonify(body), 200


# ============================================
# REGISTRATION / SIGNUP ROUTES (public)
# ============================================


@analytics_bp.route("/signup")
def signup_page():
    """Display self-service registration form."""
    if not config.REGISTRATION_ENABLED:
        abort(404)
    csrf_token = auth_module.refresh_csrf_token()
    return render_template(
        "register.html",
        csrf_token=csrf_token,
        hcaptcha_site_key=config.HCAPTCHA_SITE_KEY,
        errors=[],
        form_data={},
    )


@analytics_bp.route("/signup", methods=["POST"])
def signup_post():
    """Handle signup form submission."""
    import requests as http_requests

    if not config.REGISTRATION_ENABLED:
        abort(404)

    # CSRF validation
    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return redirect(url_for("analytics.signup_page"))

    # Rate limiting (5 attempts per IP per hour)
    ip = request.remote_addr
    if not RegistrationService.check_rate_limit(ip):
        logging.warning("AUDIT: action=registration_rate_limited ip=%s", ip)
        abort(429, description="Too many registration attempts. Please try again later.")

    RegistrationService.record_attempt(ip)

    # Extract form data
    username = request.form.get("username", "").strip().lower()
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    form_data = {"username": username, "full_name": full_name, "email": email}

    # hCaptcha verification (skip if not configured)
    if config.HCAPTCHA_SITE_KEY and config.HCAPTCHA_SECRET_KEY:
        captcha_response = request.form.get("h-captcha-response", "")
        if not captcha_response:
            return render_template(
                "register.html",
                csrf_token=auth_module.refresh_csrf_token(),
                hcaptcha_site_key=config.HCAPTCHA_SITE_KEY,
                errors=["Please complete the captcha"],
                form_data=form_data,
            )
        try:
            verify_resp = http_requests.post(
                "https://api.hcaptcha.com/siteverify",
                data={
                    "secret": config.HCAPTCHA_SECRET_KEY,
                    "response": captcha_response,
                },
                timeout=10,
            )
            if not verify_resp.json().get("success"):
                return render_template(
                    "register.html",
                    csrf_token=auth_module.refresh_csrf_token(),
                    hcaptcha_site_key=config.HCAPTCHA_SITE_KEY,
                    errors=["Captcha verification failed. Please try again."],
                    form_data=form_data,
                )
        except Exception:
            # Reject registration when captcha verification is unreachable
            logging.warning("hCaptcha verification request failed, rejecting registration")
            return render_template(
                "register.html",
                csrf_token=auth_module.refresh_csrf_token(),
                hcaptcha_site_key=config.HCAPTCHA_SITE_KEY,
                errors=["Captcha verification service unavailable. Please try again later."],
                form_data=form_data,
            )

    # Validate all fields
    validation = RegistrationService.validate_registration(username, email, password, confirm_password, full_name=full_name)
    if not validation["valid"]:
        return render_template(
            "register.html",
            csrf_token=auth_module.refresh_csrf_token(),
            hcaptcha_site_key=config.HCAPTCHA_SITE_KEY,
            errors=validation["errors"],
            form_data=form_data,
        )

    # Create pending registration
    from flask import current_app

    result = RegistrationService.create_pending_registration(
        username=username,
        full_name=full_name,
        email=email,
        password=password,
        ip_address=ip,
        user_agent=request.headers.get("User-Agent", ""),
    )
    if not result["success"]:
        return render_template(
            "register.html",
            csrf_token=auth_module.refresh_csrf_token(),
            hcaptcha_site_key=config.HCAPTCHA_SITE_KEY,
            errors=["Registration failed. Please try again later."],
            form_data=form_data,
        )

    # Generate verification token and send email
    token = RegistrationService.generate_verification_token(result["registration_id"], current_app.secret_key)
    # never build an emailed link from the request host (X-Forwarded-Host is client-influenced).
    verify_url = public_base_url() + url_for("analytics.verify_email", token=token)
    email_result = RegistrationService.send_verification_email(email, username, full_name, verify_url)
    if not email_result["success"]:
        logging.error(
            "Registration created but verification email failed: %s",
            email_result.get("error"),
        )

    # Audit log
    AuditLogService.log_event(
        username,
        "registration_submitted",
        True,
        ip_address=ip,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"email={email}",
    )

    return redirect(url_for("analytics.signup_success_page", email=email))


@analytics_bp.route("/signup/success")
def signup_success_page():
    """Show post-registration confirmation page."""
    email = request.args.get("email", "")
    return render_template("registration_success.html", email=email)


@analytics_bp.route("/verify-email/<token>")
def verify_email(token):
    """Handle email verification link click."""
    from flask import current_app

    token_result = RegistrationService.verify_token(token, current_app.secret_key)
    if not token_result["valid"]:
        return render_template(
            "verify_email.html",
            success=False,
            error=token_result["error"],
        )

    confirm_result = RegistrationService.confirm_email_verification(token_result["registration_id"])
    if not confirm_result["success"] and confirm_result.get("error") != "Email already verified":
        return render_template(
            "verify_email.html",
            success=False,
            error=confirm_result.get("error", "Verification failed"),
        )

    # Audit log
    reg = RegistrationService.get_registration_by_id(token_result["registration_id"])
    if reg:
        AuditLogService.log_event(
            reg.get("username", ""),
            "email_verified",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
        )

    return render_template("verify_email.html", success=True, error=None)


# ============================================
# ONBOARDING WIZARD STEP 1
# ============================================
# Unconditional retired the new-onboarding feature flag
#. Distinct from /signup which
# routes through email verification + admin approval; the wizard flow
# is one-shot — user picks a password, account is created, session is
# started, redirect to step 2 in the same screen.


def _redirect_if_already_authenticated_for_signup(return_to: str | None = None):
    """follow-up (2026-05-20) — block sign-up while another
    user is already authenticated in this browser session.

    Bug a teammate flagged: with the wizard signup flow auto-logging in
    on submit, a new sign-up POST silently overwrote the existing
    session, so a user with tab A on /dashboard as ``alice`` who
    typed into a tab B sign-up form would be suddenly logged in as
    the brand-new account on BOTH tabs. The session cookie is
    browser-wide, not per-tab.

    Returns a Flask redirect Response if the current session is
    authenticated (any user), else None.

    Only used by the SSO-start GET handlers (Google + Microsoft) now —
    those are navigation-only entrypoints with no form data to discard,
    so the hard 302→/logout shape is fine.

    ``return_to`` is the post-logout landing path. It
    defaults to the signup page, which is correct for the wizard
    entry points this guard was written for. The login page's
    "Sign in with Microsoft" button passes /login instead: the
    session-overwrite protection is identical, but bouncing a
    login-initiated flow onto the *signup* form is the wrong
    destination — the user asked to sign in, so they must land back
    on the sign-in surface. Callers pass a url_for() result, never
    user input, so this is not an open-redirect vector.

    The /onboarding/start form handlers no longer call this: both the
    GET and POST handlers render the signup form with an inline
    "already signed in as X" warning (and the POST refuses to
    provision over the existing session). Both avoid the original
    silent-logout that destroyed the other tab's session and dropped
    form input on the two-tab session-conflict scenario (2026-05-26).
    """
    if session.get("authenticated"):
        return redirect(
            url_for(
                "analytics.logout",
                next=return_to or url_for("analytics.onboarding_start_page"),
            )
        )
    return None


def _redirect_wizard_pending_off_signup():
    """Issue 9 — redirect a wizard-pending session that lands
    on GET /onboarding/start back to its own resume URL.

    A wizard-pending session is NOT trying to create a second account;
    they're trying to continue their OWN wizard but landed on the
    signup URL. Send them to the resume URL (the same target
    verify_auth's gate uses) instead of showing them a signup form.
    Falls back to step-4 on ES outage so a transient blip doesn't
    strand the user.

    Returns a Flask redirect Response for a wizard-pending session,
    else None.

    2026-05-26 — the old full-page "Already signed in" blocker this
    helper used to render was removed. A completed/regular
    authenticated user who lands here now just sees the signup form
    with an inline "already signed in as X" warning (added by the GET
    handler), matching the POST handler's behaviour — no separate
    blocker page, no Return-to-dashboard escape, no superuser special-
    casing (the inline warning is harmless to a superuser demoing the
    form, and the POST still refuses to provision over their session).
    """
    if not session.get("authenticated"):
        return None
    if not session.get("wizard_pending"):
        return None
    username = session.get("username") or ""
    target = None
    if username:
        try:
            from asset_management.web.services.wizard_resume_service import (
                WizardResumeService,
            )

            resume_state = WizardResumeService.get_resume_state(username)
            if resume_state and resume_state.resume_url:
                target = resume_state.resume_url
        except Exception as e:  # noqa: BLE001 — fail-soft
            logging.warning(
                "Issue 9 wizard_pending resume lookup failed: %s",
                e,
            )
    if not target:
        target = url_for("analytics.onboarding_step4_page")
    return redirect(target)


def _purge_orphan_pending_connections(username: str) -> int:
    """Issue 2 — drop ``status=pending_token`` connections.

    Called from the step-6 Skip handler. A user who reached step-7
    (OAuth callback wrote a ``de_connection`` row with
    ``status=pending_token``) and then back-buttoned + skipped on
    step-6 leaves the pending row behind. The dashboard's
    ``connect_first_source`` checklist predicate counts ANY connection
    matching the user's principal as "done" — so the orphan flags the
    item as complete even though the user explicitly opted out.

    Scope: principals matching the username AND the user's primary
    workspace_esid. Skips ``status`` other than pending_token so a
    genuinely active connection from a prior completion isn't
    accidentally purged. Fail-soft: any ES error returns 0 silently;
    the Skip flow must never block on a cleanup failure.

    Returns the count of rows purged (useful for the audit detail
    line). The connections index is bootstrapped lazily, so an
    index-missing error is a no-op (no rows existed to purge).
    """
    if not username:
        return 0
    try:
        from lib.es_helpers import get_elasticsearch_client
        from asset_management.web.services.connection_service import (
            INDEX_CONNECTION,
            STATUS_PENDING_TOKEN,
        )
        from asset_management.web.services.workspace_membership_service import (
            WorkspaceMembershipService,
        )
    except ImportError as e:
        logging.warning("Issue 2 orphan-connection-purge imports failed: %s", e)
        return 0
    es = get_elasticsearch_client()
    if es is None:
        return 0
    principals: list[str] = [username]
    try:
        workspace_esid = WorkspaceMembershipService.get_primary_workspace(username)
    except Exception as e:  # noqa: BLE001
        logging.warning("Issue 2 workspace lookup failed for %r: %s", username, e)
        workspace_esid = None
    if workspace_esid:
        principals.append(workspace_esid)
    try:
        resp = es.delete_by_query(
            index=INDEX_CONNECTION,
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"terms": {"principal": principals}},
                            {"term": {"status": STATUS_PENDING_TOKEN}},
                        ]
                    }
                }
            },
            refresh=True,
        )
        deleted = int(resp.get("deleted", 0))
        if deleted:
            logging.info(
                "Issue 2 purged %d orphan pending_token connection(s) for %r",
                deleted,
                username,
            )
        return deleted
    except Exception as e:  # noqa: BLE001 — fail-soft
        err = str(e).lower()
        # Index-missing is the lazy-bootstrap case: no rows to purge.
        if "index_not_found" in err or "no such index" in err:
            return 0
        logging.warning(
            "Issue 2 orphan-connection-purge failed for %r: %s",
            username,
            e,
        )
        return 0


def _compute_locked_connector_ids(popular_tiles, all_tiles, limit: int) -> frozenset[str]:
    """compute which selectable tiles are locked behind the
    upgrade tooltip.

    Walks the popular section first, then the all-connectors section
    (same order the template renders), and counts selectable tiles
    (not placeholder, not coming_soon — those have their own UX paths
    independent of the source limit). The first ``limit`` selectable
    tiles are enabled; every later selectable tile is locked.

    ``limit < 0`` is the paid-tier sentinel: no tile is locked. The
    return is a ``frozenset`` so the template's ``in`` check is O(1)
    per tile and the value can't be mutated downstream.
    """
    if limit < 0:
        return frozenset()
    locked: list[str] = []
    selectable_seen = 0
    for tile in tuple(popular_tiles) + tuple(all_tiles):
        # Placeholder + coming-soon tiles are gated by their own flags
        # (notify-me banner); they don't consume a source-limit slot
        # and they don't get the upgrade tooltip.
        if tile.placeholder or tile.coming_soon:
            continue
        if selectable_seen >= limit:
            locked.append(tile.connector_id)
        selectable_seen += 1
    return frozenset(locked)


# Invited-flow role display map. Covers the role
# esids the invite modal in /dashboard's checklist creates today; any
# unmapped esid falls back to a titlecased version of the esid itself
# so the banner copy still makes sense without a hard-coded entry.
_INVITE_ROLE_DISPLAY: dict[str, str] = {
    # Keys match the role_esid values the invite modal actually sends
    # (_invite_modal.html: editor / viewer / org_admin / workspace_admin) plus
    # the legacy role names still used elsewhere. Without the modal's own
    # values here, an "Editor" invite fell through to the fallback below and
    # rendered "a Editor".
    "editor": "an Editor",
    "viewer": "a Viewer",
    "org_admin": "an Org Admin",
    "asset_admin": "an Admin",
    "kibana_user": "a Viewer",
    "team_admin": "a Team Admin",
    "workspace_admin": "a Workspace Admin",
    "workspace_owner": "a Workspace Owner",
}


def _fallback_role_display(role_esid: str) -> str:
    """Article-correct fallback for a role_esid not in _INVITE_ROLE_DISPLAY.

    Picks "an" before a vowel-initial word, "a" otherwise, so an unmapped
    role never renders "a Editor" / "a Org Admin".
    """
    label = role_esid.replace("_", " ").title()
    article = "an" if label[:1].lower() in "aeiou" else "a"
    return f"{article} {label}"


def _resolve_pending_invite_banner(invite: dict | None) -> dict | None:
    """Resolve a ``pending_invite`` payload into banner-display fields.

    The session payload (``InviteTokenService.build_session_invite``)
    carries esids only — org_esid, team_esid, role_esid — plus the
    inviter username. The step-1 banner ("Sarah Reyes invited you to
    Acme Corp.") needs human-readable titles, so this helper does the
    two ES lookups and humanizes the role.

    Fail-soft on every leg: an ES outage or missing doc resolves the
    affected field to an empty string rather than raising, so the banner
    never blocks the signup form. Returns ``None`` only when the input
    is missing or empty — never on a lookup failure.
    """
    if not invite or not isinstance(invite, dict):
        return None

    org_esid = (invite.get("org_esid") or "").strip()
    team_esid = (invite.get("team_esid") or "").strip()
    role_esid = (invite.get("role_esid") or "").strip()
    inviter_display = (invite.get("inviter") or "").strip()

    org_title = ""
    team_title = ""
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is not None:
            if org_esid:
                try:
                    doc = es.get(index="de_asset_organization", id=org_esid)
                    src = doc.get("_source") or {}
                    # Asset docs key titles by ``{asset_type}_title``,
                    # not the generic ``title`` field — verified on
                    # dev-reporting 2026-05-19.
                    org_title = (src.get("organization_title") or src.get("title") or "").strip()
                except Exception:
                    org_title = ""
            if team_esid:
                try:
                    doc = es.get(index="de_asset_teams", id=team_esid)
                    src = doc.get("_source") or {}
                    team_title = (src.get("team_title") or src.get("title") or "").strip()
                except Exception:
                    team_title = ""
    except Exception as e:  # noqa: BLE001 — fail-soft for the banner
        logging.warning("invite banner ES lookup failed: %s", e)

    role_display = ""
    if role_esid:
        role_display = _INVITE_ROLE_DISPLAY.get(role_esid) or _fallback_role_display(role_esid)

    return {
        "inviter_display": inviter_display,
        "org_title": org_title,
        "team_title": team_title,
        "role_display": role_display,
    }


@analytics_bp.route("/onboarding/verify-required")
def onboarding_verify_required_page():
    """follow-up (2026-05-20) — verify-your-email holding page.

    Landed on by ``LoginGateService.VERIFY_EMAIL``: a new user (one
    whose ``metadata.onboarding_required`` is True) signed in successfully
    but has not yet clicked the verification link in their inbox. The
    page renders the destination email + a "refresh" CTA that hits
    ``/onboarding/verify-required/check`` (NOT /login — the user is
    already authenticated, so /login GET would short-circuit to
    /dashboard and bypass the gate). Signing out is the explicit
    escape valve.
    """
    return render_template(
        "onboarding_verify_required.html",
        user_email=session.get("email", "") or "",
    )


@analytics_bp.route("/onboarding/verify-required/check")
def onboarding_verify_required_check():
    """Recheck the login gate without logging the user out.

    The verify-required landing page's "I've verified — refresh" CTA
    hits this route. We re-evaluate :class:`LoginGateService` against
    the still-authenticated user and redirect on the outcome:

    * NONE → /dashboard (the user completed the wizard and verified
      while sitting on the holding page — unlikely but possible).
    * VERIFY_EMAIL → back to /onboarding/verify-required (no change,
      they hadn't actually clicked the link yet).
    * WIZARD_INCOMPLETE → wizard resume URL (the user verified email
      and now needs to finish the wizard).
    """
    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    from asset_management.web.services.login_gate_service import (
        LoginGate,
        LoginGateService,
    )

    _gate = LoginGateService.evaluate_for_login(username, session.get("roles") or [])
    if _gate.gate == LoginGate.NONE:
        return redirect(url_for("analytics.dashboard_page"))
    if _gate.gate == LoginGate.WIZARD_INCOMPLETE:
        try:
            from asset_management.web.services.wizard_resume_service import (
                WizardResumeService,
            )

            resume_state = WizardResumeService.get_resume_state(username)
            target = (resume_state.resume_url if resume_state else None) or url_for("analytics.onboarding_step4_page")
        except Exception as e:  # noqa: BLE001 — fail-soft
            logging.warning("verify-required/check resume lookup failed for %r: %s", username, e)
            target = url_for("analytics.onboarding_step4_page")
        return redirect(target)
    # VERIFY_EMAIL — still not verified. Stay on the holding page.
    return redirect(url_for("analytics.onboarding_verify_required_page"))


@analytics_bp.route("/onboarding/start")
def onboarding_start_page():
    """Wizard step 1: display the signup form."""
    # 2026-08-06 open-registration incident: with self-signup closed, the
    # signup form is only reachable with an invite (?invite=<jwt> being
    # presented now, or one already decoded into the session). Everyone
    # else goes to the sign-in page. Wizard-pending sessions fall through
    # to the resume redirect below, which routes them back into their own
    # in-progress wizard rather than the (blocked) blank form.
    if (
        not config.SELF_SIGNUP_ENABLED
        and not request.args.get("invite")
        and not session.get("pending_invite")
        and not request.args.get("signup")  # personal signup link
        and not session.get("signup_grant")
        and not session.get("wizard_pending")
    ):
        logging.info(
            "AUDIT: action=signup_page_refused reason=signup_disabled ip=%s",
            request.remote_addr,
        )
        return redirect(url_for("analytics.login_page"))
    # Issue 9 — a wizard-pending session lands back on its own
    # resume URL, not the signup form (they're continuing their own
    # wizard, not making a second account).
    if _resp := _redirect_wizard_pending_off_signup():
        return _resp
    csrf_token = auth_module.refresh_csrf_token()

    # (2026-05-26) — a completed/regular authenticated user who
    # lands on the signup GET sees the form with an inline "already
    # signed in as X" warning, matching the POST handler. The old
    # full-page blocker page was removed: it replaced the form
    # entirely and read as if the visitor (not the other tab) was at
    # fault. The inline warning is enough — and if they actually submit,
    # the POST refuses to provision over the existing session.
    errors: list[str] = []
    if session.get("authenticated"):
        signed_in_as = session.get("full_name") or session.get("username") or "another account"
        errors.append(
            f"This browser is already signed in as {signed_in_as} in "
            "another tab. Sign out of that account first, then come "
            "back to create a new one."
        )

    # decode ?invite=<jwt> and stash the resulting payload on
    # the Flask session cookie so it survives the user clicking through
    # the SSO buttons / refreshing the form. The session row that owns
    # ``state['invite']`` doesn't exist yet (anonymous landing); the
    # POST handler promotes it once a user_id exists.
    #
    # an EXPIRED token is no longer surfaced as a generic
    # error — we redirect to ``/onboarding/invite-expired`` (passing the
    # token in the session so the screen handler can decode the claims
    # and trigger the inviter-notify path). Invalid / malformed tokens
    # still fall through to the generic-error branch since they have
    # no signed claims we can trust.
    invite_param = request.args.get("invite") or ""
    if invite_param:
        from asset_management.web.services.invite_token_service import (
            InviteTokenService,
        )

        decoded = InviteTokenService.decode_token(invite_param, current_app.secret_key)
        if decoded.get("valid"):
            session["pending_invite"] = InviteTokenService.build_session_invite(decoded["payload"])
        elif decoded.get("expired"):
            # Stash the raw token so the expiry-screen handler can
            # decode-claims-without-exp-check via
            # ``InviteTokenService.decode_expired_claims`` and identify
            # the inviter. Cleared by the expiry-screen handler after
            # the notify-and-render pass.
            session["expired_invite_token"] = invite_param
            session.pop("pending_invite", None)
            return redirect(url_for("analytics.onboarding_invite_expired_page"))
        else:
            session.pop("pending_invite", None)
            errors.append(decoded.get("error", "Invite link is invalid."))

    # ?signup=<jwt> is a personal signup link — one named person
    # may create their OWN workspace while public signup is closed. The
    # grant record is checked here and again, atomically, when the account
    # is created. A signup link and an invite are different doors: holding
    # one drops the other, so the wizard never mixes join and create.
    signup_param = request.args.get("signup") or ""
    if signup_param:
        from asset_management.web.services.signup_grant_service import SignupGrantService

        checked = SignupGrantService.check(signup_param, current_app.secret_key)
        if checked.get("valid"):
            session["signup_grant"] = checked["grant"]
            session.pop("pending_invite", None)
        else:
            session.pop("signup_grant", None)
            errors.append(checked.get("error") or "This signup link is not valid.")

    grant = session.get("signup_grant") or {}
    return render_template(
        "onboarding_start.html",
        csrf_token=csrf_token,
        errors=errors,
        form_data={"email": grant.get("email", "")} if grant else {},
        google_sso_enabled=config.GOOGLE_SSO_ENABLED,
        microsoft_sso_enabled=config.MICROSOFT_SSO_ENABLED,
        invite_banner=_resolve_pending_invite_banner(session.get("pending_invite")),
        signup_grant_email=grant.get("email", ""),
    )


@analytics_bp.route("/onboarding/invite-expired")
def onboarding_invite_expired_page():
    """landing screen for an expired invite token.

    Reached only via ``onboarding_start_page``'s expired-branch redirect
    (which stashes the raw token on ``session['expired_invite_token']``).
    A direct hit with no stashed token falls through to a generic copy
    so the page never 404s — the AC says 'Invitee always has a path
    forward, never a 404.'

    Side effects (only when claims decode cleanly):
      1. ``InviteService.mark_expired`` flips the matching pending doc
         to ``status="expired"`` (idempotent, soft-fails).
      2. Debounce gate — if ``expiry_notified_at`` is already stamped,
         the notify-email is skipped. Otherwise we stamp it BEFORE the
         send so a concurrent second landing on the same token won't
         double-notify.
      3. ``InviteService.notify_inviter_of_expiry`` emails the inviter.

    Renders ``onboarding_invite_expired.html`` with two CTAs:
      * ``inviter_notified`` flag drives a banner copy variant
        ("We've let {inviter} know" vs "Couldn't reach the inviter").
      * ``/onboarding/start`` link — the 'Sign up as new account' path;
        strictly clears ``pending_invite`` / ``expired_invite_token``
        from the session so the user lands fresh per the AC.
    """

    token = session.pop("expired_invite_token", "") or ""

    inviter_display = ""
    inviter_notified = False
    invitee_email = ""

    if token:
        from asset_management.web.services.invite_token_service import (
            InviteTokenService,
        )

        claims = InviteTokenService.decode_expired_claims(token, current_app.secret_key)
        if claims is not None:
            invitee_email = claims.get("invitee_email", "") or ""
            inviter_username = claims.get("inviter", "") or ""
            # the JWT's ``org_esid`` slot carries
            # workspace_esid for the invite flow. will
            # fix the slot name; until then, every consumer translates.
            workspace_esid = claims.get("org_esid", "") or ""

            if workspace_esid and invitee_email:
                # 1. Flip the doc to status=expired. Idempotent on
                # already-flipped docs; soft-fails on missing rows.
                InviteService.mark_expired(workspace_esid, invitee_email)

                # 2. Debounce: check whether the inviter has already
                # been notified about THIS expiry. We read the doc
                # AFTER mark_expired so a missing row short-circuits
                # cleanly to "no debounce row".
                doc = InviteService.get_invite_doc(workspace_esid, invitee_email)
                already_notified = bool(isinstance(doc, dict) and doc.get("expiry_notified_at"))

                if not already_notified and inviter_username:
                    # 3. Stamp the debounce field BEFORE the send so a
                    # concurrent landing won't fire a second email.
                    InviteService.stamp_expiry_notified(workspace_esid, invitee_email)
                    send_result = InviteService.notify_inviter_of_expiry(
                        inviter_username=inviter_username,
                        invitee_email=invitee_email,
                        workspace_esid=workspace_esid,
                    )
                    inviter_notified = bool(send_result.get("success"))
                else:
                    # Either already-notified (don't spam) or no
                    # inviter on the JWT (legacy / malformed token).
                    # Surface "already notified" to the user so the
                    # copy doesn't promise a fresh email was just sent.
                    inviter_notified = already_notified

            inviter_display = inviter_username or ""

            # Audit event for the marketing funnel. Literal
            # ``analytics_event=invite.expiry.landing`` substring so a
            # Kibana query can slice expired-link encounters per
            # workspace without joining indices.
            try:
                AuditLogService.log_event(
                    inviter_username or "anonymous",
                    "invite_expiry_landing",
                    True,
                    ip_address=request.remote_addr or "",
                    user_agent=request.headers.get("User-Agent", ""),
                    target_user=invitee_email,
                    detail=(
                        f"analytics_event=invite.expiry.landing "
                        f"workspace={workspace_esid} "
                        f"notified={'yes' if inviter_notified else 'no'}"
                    ),
                )
            except Exception as e:  # noqa: BLE001 — top-level boundary
                logging.warning(
                    "onboarding_invite_expired_page: audit-log write failed: %s",
                    e,
                )

    # Clear any stale pending_invite that lived alongside the expired
    # token — the user is taking the 'sign up as new account' path
    # or requesting a resend; either way the expired payload is dead.
    session.pop("pending_invite", None)

    return render_template(
        "onboarding_invite_expired.html",
        inviter_display=inviter_display,
        inviter_notified=inviter_notified,
        invitee_email=invitee_email,
    )


@analytics_bp.route("/onboarding/invite-team-picker", methods=["GET", "POST"])
def onboarding_invite_team_picker_page():
    """team picker for invited users whose invite has no team.

    Reached via ``_consume_invite_and_handoff`` when the invite carries
    no ``team_esid`` and the workspace already has ≥1 team. The picker
    state (teams + invite scope) is read off
    ``wizard_session.state['invite_team_picker']``.

    GET renders the picker.

    POST validates the chosen ``team_esid`` against the workspace (a
    forged team_esid that isn't in the picker's team list falls back to
    auto-create "General"), writes it onto the invite payload, and
    re-enters the consume flow.

    Direct hits without a stashed picker payload redirect to
    ``/dashboard`` rather than 404 — the AC's "never blocked" contract.
    """

    username = session.get("username", "") or ""
    if not username:
        return redirect(url_for("analytics.login_page"))

    try:
        from asset_management.web.services.wizard_session_service import (
            WizardSessionService,
        )
    except Exception as e:  # noqa: BLE001 — import-time blip
        logging.error("picker import failed: %s", e)
        return redirect("/dashboard")

    active = WizardSessionService.get_active(username, "onboarding") or {}
    state = dict(active.get("state") or {})
    picker = dict(state.get("invite_team_picker") or {})

    teams = picker.get("teams") or []
    workspace_esid = picker.get("workspace_esid") or ""
    invite_role_esid = picker.get("role_esid") or ""

    invite_payload = dict(state.get("invite") or {})

    # Direct deep-link with no stashed picker state → fall through to
    # /dashboard rather than dead-end. The AC's "Invited user is never
    # blocked at this branch."
    if not workspace_esid or not invite_payload:
        return redirect("/dashboard")

    if request.method == "POST":
        csrf_token = request.form.get("csrf_token", "")
        if not auth_module.validate_csrf_token(csrf_token):
            return redirect(url_for("analytics.onboarding_invite_team_picker_page"))

        chosen = (request.form.get("team_esid") or "").strip()
        create_general = (request.form.get("create_general") or "").strip() == "1"

        # Validate the chosen esid against the workspace's actual team
        # list (defense-in-depth: a tampered hidden input cannot grant
        # membership in a stranger's team). Three cases:
        #   * chosen is in the stashed teams ⇒ honor it
        #   * chosen is empty (the "Start in General" radio) OR the
        #     form's create_general flag is set ⇒ ensure_general_team
        #   * chosen is non-empty but NOT in the stashed list
        #     (forged hidden input) ⇒ fall back to ensure_general_team
        #     rather than honor the forged value
        valid_team_esids = {t.get("team_esid") for t in teams if t.get("team_esid")}
        resolved_team_esid: str | None = None
        if chosen and chosen in valid_team_esids:
            resolved_team_esid = chosen
        else:
            if chosen and chosen not in valid_team_esids:
                logging.warning(
                    "picker: rejected team_esid %r not in stashed teams; " "falling back to General. workspace=%s",
                    chosen,
                    workspace_esid,
                )
            try:
                resolved_team_esid = InviteService.ensure_general_team(workspace_esid)
            except Exception as e:  # noqa: BLE001 — fall-open
                logging.warning("picker General-create failed: %s", e)
                resolved_team_esid = None

        # Stamp the chosen team onto the invite payload + clear the
        # picker stash so a refresh of /dashboard doesn't re-show the
        # picker. Both writes go through a single WizardSessionService
        # save to keep the wizard_session row coherent.
        invite_payload["team_esid"] = resolved_team_esid or ""
        state["invite"] = invite_payload
        state.pop("invite_team_picker", None)
        try:
            WizardSessionService.save(
                user_id=username,
                wizard_type="onboarding",
                current_step="step-6",
                state=state,
            )
        except Exception as e:  # noqa: BLE001 — non-load-bearing for handoff
            logging.warning("picker save failed for %r: %s", username, e)

        AuditLogService.log_event(
            username,
            "onboarding_invite_team_chosen",
            True,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"analytics_event=invite.team_picker.chosen workspace={workspace_esid} "
                f"team_esid={resolved_team_esid or ''} "
                f"create_general={'yes' if create_general else 'no'}"
            ),
        )

        # Re-enter the consume flow with a hydrated invite payload. The
        # second pass sees team_esid set and falls through to
        # provision_invited without re-showing this picker.
        return _consume_invite_and_handoff(username, invite_payload)

    return render_template(
        "onboarding_invite_team_picker.html",
        csrf_token=auth_module.refresh_csrf_token(),
        teams=teams,
        invite_role_esid=invite_role_esid,
        progress=_wizard_progress_for("invite-team-picker", username),
    )


@analytics_bp.route("/onboarding/resume/restart", methods=["POST"])
def onboarding_resume_restart():
    """abandon the user's active onboarding wizard and start fresh.

    Reached from the "Continue setup" banner's Restart button (only
    rendered when the active session is older than
    ``WizardResumeService.STALE_AGE_DAYS``). Flips the active
    wizard_session doc to ``abandoned`` so the next render of /dashboard
    doesn't re-show the banner, then redirects to ``/onboarding/start``
    so the user lands on a fresh wizard.

    The provision_* helpers in OnboardingService key on deterministic doc
    IDs (workspace slug, org/team titles), so even if the user re-creates
    a workspace with the same name, they'll resolve back to the SAME ES
    document via the 409-swallow path. Restart is "clean wizard state",
    not "clean asset state" — the latter is a Settings-level destructive
    operation, not a UX shortcut.
    """
    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return redirect("/dashboard")

    username = session.get("username", "") or ""
    if not username:
        return redirect(url_for("analytics.login_page"))

    try:
        from asset_management.web.services.wizard_resume_service import (
            WizardResumeService,
        )

        # Capture the age before we abandon, so the audit event records
        # the staleness that triggered the restart (useful for measuring
        # "how stale was a session when the user gave up on it?").
        prior = WizardResumeService.get_resume_state(username)
        WizardResumeService.restart(username)
    except Exception as e:  # noqa: BLE001 — fall-open
        # Restart is a UX shortcut, not a security-load-bearing action;
        # a failed abandon still lets the user start fresh because the
        # next step-2 save will overwrite the active doc anyway.
        logging.warning("restart failed for %r: %s", username, e)
        prior = None

    age_days = prior.age_days if prior else -1
    current_step = prior.current_step if prior else ""
    AuditLogService.log_event(
        username,
        "onboarding_resume_restart",
        True,
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
        detail=(f"analytics_event=onboarding.resume.restart " f"prior_step={current_step} age_days={age_days}"),
    )

    return redirect(url_for("analytics.onboarding_start_page"))


def _claim_grant_after_sso(grant: dict, result: dict, *, auth_method: str) -> None:
    """an SSO sign-up just used a personal signup link. Claim the
    grant (email-bound, so a second attempt with this email is a sign-in, not
    another account) and carry it into the wizard for step-5's sponsorship."""
    from asset_management.web.services.signup_grant_service import SignupGrantService

    session.pop("signup_grant", None)
    username = result.get("username") or ""
    if not SignupGrantService.claim(grant.get("jti", ""), email=result.get("email") or "", username=username):
        logging.warning("AUDIT: action=signup_grant_claim_lost jti=%s user=%s", grant.get("jti", ""), username)
        return
    try:
        from asset_management.web.services.wizard_session_service import WizardSessionService

        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="mfa-setup",
            state={"signup_grant": grant},
        )
    except Exception as e:  # noqa: BLE001 — never let a stash failure break sign-up
        logging.warning("wizard_session save failed for %r: %s", username, e)
    AuditLogService.log_event(
        username,
        "signup_grant_used",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"jti={grant.get('jti', '')} sponsored={bool(grant.get('sponsored'))} via={auth_method}",
    )


@analytics_bp.route("/onboarding/start", methods=["POST"])
def onboarding_start_post():
    """Wizard step 1: provision the user and log them in."""
    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return redirect(url_for("analytics.onboarding_start_page"))

    # 2026-08-06 open-registration incident: a POST is a real provisioning
    # attempt — refuse it outright when self-signup is closed and no invite
    # is pending. (The GET already hides the form; this covers direct POSTs.)
    # a personal signup link (session['signup_grant']) opens the
    # same door for exactly its own email; the email check and the atomic
    # claim below enforce that.
    signup_grant = session.get("signup_grant") or None
    if not config.SELF_SIGNUP_ENABLED and not session.get("pending_invite") and not signup_grant:
        logging.warning(
            "AUDIT: action=signup_post_refused reason=signup_disabled ip=%s",
            request.remote_addr,
        )
        return redirect(url_for("analytics.login_page"))

    username = request.form.get("username", "").strip().lower()
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")
    # browser-detected IANA zone from a hidden field on the signup
    # form. Validated (and silently dropped if forged/invalid) when stamped
    # onto the new user's metadata in signup_step1.
    detected_time_zone = request.form.get("detected_time_zone", "").strip()
    form_data = {"username": username, "full_name": full_name, "email": email}
    signup_grant_email = (signup_grant or {}).get("email", "")

    if signup_grant and email != signup_grant_email:
        return render_template(
            "onboarding_start.html",
            csrf_token=auth_module.refresh_csrf_token(),
            errors=[
                f"This signup link is for {signup_grant_email}. Create the account with that address, "
                "or ask for a link for the address you want to use."
            ],
            form_data={**form_data, "email": signup_grant_email},
            google_sso_enabled=config.GOOGLE_SSO_ENABLED,
            microsoft_sso_enabled=config.MICROSOFT_SSO_ENABLED,
            invite_banner=None,
            signup_grant_email=signup_grant_email,
        )

    # follow-up (2026-05-20) — refuse to auto-login a new user
    # over an existing authenticated session.
    #
    # 2026-05-26: the failure mode the user hit is a two-tab one. Tab A
    # is the signup form; tab B is signed in to an existing account.
    # The session cookie is browser-wide, so when tab A submits, the
    # server sees the authenticated cookie. Originally the POST silently
    # 302'd to /logout (destroying tab B's session + dropping tab A's
    # form input); a first fix rendered the full-page blocker on tab A,
    # but that replaced the signup form the user was looking at and read
    # as if THEY were the one already signed in. The right UX: keep the
    # signup form ON tab A (with their typed values intact) and surface
    # an inline error explaining the conflicting session — the user
    # never loses their work, and tab B is untouched. The superuser GET
    # bypass does NOT apply here (a POST is a real provisioning attempt,
    # not a form demo), so completed + superuser sessions both land here.
    if session.get("authenticated"):
        signed_in_as = session.get("full_name") or session.get("username") or "another account"
        return render_template(
            "onboarding_start.html",
            csrf_token=auth_module.refresh_csrf_token(),
            errors=[
                f"This browser is already signed in as {signed_in_as} in "
                "another tab. Sign out of that account first, then come "
                "back to finish creating this one."
            ],
            form_data=form_data,
            google_sso_enabled=config.GOOGLE_SSO_ENABLED,
            microsoft_sso_enabled=config.MICROSOFT_SSO_ENABLED,
            invite_banner=_resolve_pending_invite_banner(session.get("pending_invite")),
            signup_grant_email=signup_grant_email,
        )

    # short-circuit on a duplicate email BEFORE the auto-join
    # prompt fires. An existing-account user who hits an auto-join
    # domain match would otherwise be sent to "Join {Org}?" instead of
    # being told they already have an account; per the ticket design
    # brief we choose the informative path over the silent one.
    if email:
        from asset_management.web.services.onboarding_signup_service import (
            OnboardingSignupService as _PreCheckSignup,
        )

        existing = _PreCheckSignup.find_user_by_email(email)
        if existing is not None:
            return render_template(
                "onboarding_start.html",
                csrf_token=auth_module.refresh_csrf_token(),
                errors=["You already have an account — sign in instead."],
                form_data=form_data,
                existing_username=existing.get("username", ""),
                google_sso_enabled=config.GOOGLE_SSO_ENABLED,
                microsoft_sso_enabled=config.MICROSOFT_SSO_ENABLED,
                invite_banner=_resolve_pending_invite_banner(session.get("pending_invite")),
                signup_grant_email=signup_grant_email,
            )

    # Issue 1 (2026-05-25) — duplicate-USERNAME short-circuit,
    # parallel to the duplicate-email branch above. Pre-fix the
    # username collision check only ran inside ``signup_step1`` AFTER
    # validate_signup ran the 5 password rules — so a user trying to
    # re-create their existing account would see password-rule errors
    # ("Password must contain at least one ...") before being told the
    # username was already taken. a teammate flagged the order as
    # confusing. Run the username check at the route's pre-validation
    # tier so the duplicate error fires first regardless of password
    # shape. Same conservative posture as the email branch: any ES
    # lookup error falls through to the legacy path so a transient
    # blip never blocks a legitimate signup.
    if username:
        try:
            from lib.es_helpers import get_elasticsearch_client
            from asset_management.web.services.user_provisioning_service import (
                UserProvisioningService,
            )

            _username_es = get_elasticsearch_client()
            if _username_es is not None and UserProvisioningService._username_exists(_username_es, username):
                suggestion = UserProvisioningService.suggest_unique_username(username)
                return render_template(
                    "onboarding_start.html",
                    csrf_token=auth_module.refresh_csrf_token(),
                    errors=[f"Username '{username}' is already taken. " f"Try '{suggestion}' instead."],
                    form_data=form_data,
                    google_sso_enabled=config.GOOGLE_SSO_ENABLED,
                    microsoft_sso_enabled=config.MICROSOFT_SSO_ENABLED,
                    invite_banner=_resolve_pending_invite_banner(session.get("pending_invite")),
                    signup_grant_email=signup_grant_email,
                )
        except Exception as e:  # noqa: BLE001 — fail-soft, fall through to legacy path
            logging.warning(
                "Issue 1 username pre-check failed for %r: %s",
                username,
                e,
            )

    # domain-match auto-join prompt. Surface a "Join {OrgName}?"
    # affordance when the signup email's domain has been explicitly listed
    # on an existing org's ``auto_join_email_domains``. Precedence:
    #
    # 1. ``session['pending_invite']`` — an invite token already
    #      names the target org/team/role, so domain-match must never
    #      override it. The user is here because someone invited them.
    #   2. ``session['auto_join_declined']`` — the user already saw the
    #      prompt and clicked "Create my own", so we skip the matcher to
    #      avoid an infinite redirect loop and respect their choice.
    #   3. ``AutoJoinDomainService.find_match(email)`` — exact-match on the
    #      domain, free-mail domains skipped. On hit: stash the candidate
    #      + form payload on the session and redirect to the prompt
    #      before signup_step1 runs (we don't want to half-create an ES
    #      Security user only to immediately bounce away from this page).
    if not session.get("pending_invite") and not signup_grant and not session.get("auto_join_declined") and email:
        from asset_management.web.services.auto_join_domain_service import (
            AutoJoinDomainService,
        )

        match = AutoJoinDomainService.find_match(email)
        if match:
            session["auto_join_candidate"] = {
                "org_esid": match["org_esid"],
                "org_title": match["org_title"],
                "domain": match["domain"],
                # Round-trip the form so the prompt's Accept branch can
                # complete signup_step1 without re-prompting for password.
                # The Flask session cookie is server-signed-and-encrypted;
                # storing the password here for the lifetime of one click
                # is acceptable. The candidate is cleared on either branch.
                "form": {
                    "username": username,
                    "full_name": full_name,
                    "email": email,
                    "password": password,
                    "confirm_password": confirm_password,
                    "detected_time_zone": detected_time_zone,
                },
            }
            return redirect(url_for("analytics.onboarding_join_prompt_page"))

    # No domain match (or precedence kicked in) — clear the one-shot
    # "declined" flag so a future signup attempt re-evaluates cleanly.
    session.pop("auto_join_declined", None)

    from asset_management.web.services.onboarding_signup_service import (
        OnboardingSignupService,
    )

    # claim the grant BEFORE the account exists, so a forwarded
    # link or a double submit can never create two accounts; a failed
    # signup below releases the claim so a password-rule error does not burn
    # the link.
    grant_claimed = False
    if signup_grant:
        from asset_management.web.services.signup_grant_service import SignupGrantService

        grant_claimed = SignupGrantService.claim(signup_grant.get("jti", ""), email=email, username=username)
        if not grant_claimed:
            session.pop("signup_grant", None)
            logging.warning(
                "AUDIT: action=signup_grant_claim_refused jti=%s ip=%s",
                signup_grant.get("jti", ""),
                request.remote_addr,
            )
            return render_template(
                "onboarding_start.html",
                csrf_token=auth_module.refresh_csrf_token(),
                errors=["This signup link has already been used or is no longer valid. Sign in, or ask for a new link."],
                form_data=form_data,
                google_sso_enabled=config.GOOGLE_SSO_ENABLED,
                microsoft_sso_enabled=config.MICROSOFT_SSO_ENABLED,
                invite_banner=None,
                signup_grant_email="",
            )

    result = OnboardingSignupService.signup_step1(
        username=username,
        email=email,
        password=password,
        confirm_password=confirm_password,
        full_name=full_name,
        detected_time_zone=detected_time_zone,
    )

    if not result.get("success") and grant_claimed:
        SignupGrantService.release(signup_grant.get("jti", ""), username=username)

    if not result.get("success"):
        return render_template(
            "onboarding_start.html",
            csrf_token=auth_module.refresh_csrf_token(),
            errors=result.get("errors", ["Could not create account."]),
            form_data=form_data,
            # pass existing_username through unconditionally;
            # the template's existing_username-AND-message conjunction
            # is the single source of truth for surfacing the
            # Sign-in-instead link. A non-collision failure has no
            # existing_username key on the result and defaults to "".
            existing_username=result.get("existing_username", ""),
            google_sso_enabled=config.GOOGLE_SSO_ENABLED,
            microsoft_sso_enabled=config.MICROSOFT_SSO_ENABLED,
            invite_banner=_resolve_pending_invite_banner(session.get("pending_invite")),
            signup_grant_email=signup_grant_email,
        )

    # If the partial-rollback path tripped (password set, but
    # force_password_change couldn't be cleared), bounce through the
    # change-password flow rather than auto-logging in.
    if result.get("force_password_change"):
        AuditLogService.log_event(
            result["username"],
            "onboarding_signup_partial",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail="force_password_change flag could not be cleared",
        )
        return redirect(url_for("analytics.login_page", message="Please sign in to continue."))

    # Auto-login: build the same session shape login_post() does. The
    # acceptance criterion is "land user on step 2 within one screen",
    # so we skip the password re-prompt the legacy /signup flow does.
    session.pop("logged_out", None)
    session["authenticated"] = True
    session["username"] = result["username"]
    session["roles"] = result.get("roles", [])
    session["is_admin"] = "superuser" in result.get("roles", []) or config.REQUIRED_ROLE in result.get("roles", [])
    session["full_name"] = result.get("full_name") or result["username"]
    session["email"] = result.get("email", "")
    session["auth_timestamp"] = time.time()
    session["last_activity"] = time.time()
    session["auth_method"] = "onboarding_signup"
    # follow-up — set wizard_pending so verify_auth confines this
    # brand-new session to the onboarding wizard whitelist. Cleared at
    # step-8 finish and at step-6 skip; verify_auth's hard-redirect to
    # the resume URL stops the user reaching /dashboard / asset-manager /
    # dashboard-builder until one of those completion paths fires.
    session["wizard_pending"] = True
    session.permanent = True

    AuditLogService.log_event(
        result["username"],
        "login",
        True,
        auth_method="onboarding_signup",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    # promote the pre-auth invite stash into wizard_session.
    # The GET decoded ?invite=<jwt> into ``session['pending_invite']``
    # because no wizard_session row exists pre-signup. Now that we have
    # a user_id we save the wizard session with the invite payload on
    # ``state['invite']`` and clear the cookie. reads this back
    # to skip steps 2-3 and apply the role from the invite; ``consumed``
    # stays False until that ticket flips it on first arrival at step 2.
    # carry the claimed grant into the wizard so step-5 can apply
    # a sponsored grant's comp to the workspace it creates. No invite state
    # is written, so the wizard runs the normal new-owner path.
    claimed_grant = session.pop("signup_grant", None) if grant_claimed else None
    if claimed_grant:
        try:
            from asset_management.web.services.wizard_session_service import (
                WizardSessionService,
            )

            WizardSessionService.save(
                user_id=result["username"],
                wizard_type="onboarding",
                current_step="verify-email",
                state={"signup_grant": claimed_grant},
            )
        except Exception as e:  # noqa: BLE001 — never let a stash failure break signup
            logging.warning("wizard_session save failed for '%s': %s", result["username"], e)
        AuditLogService.log_event(
            result["username"],
            "signup_grant_used",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"jti={claimed_grant.get('jti', '')} sponsored={bool(claimed_grant.get('sponsored'))} via=password",
        )

    pending_invite = session.pop("pending_invite", None)
    if pending_invite:
        try:
            from asset_management.web.services.wizard_session_service import (
                WizardSessionService,
            )

            WizardSessionService.save(
                user_id=result["username"],
                wizard_type="onboarding",
                current_step="step-4",
                state={"invite": pending_invite},
            )
        except Exception as e:  # noqa: BLE001 — never let a stash failure break signup
            logging.warning(
                "wizard_session save failed for '%s': %s",
                result["username"],
                e,
            )

    # dispatch the verification email on a background thread so
    # the wizard POST returns in <100ms regardless of SMTP latency. The
    # user lands on step 2 while the message is in flight; the
    # verification flag is only consulted by the final handoff (future
    # step 7), so steps 2–6 are intentionally unblocked.
    try:
        from asset_management.web.services.onboarding_email_verification_service import (
            OnboardingEmailVerificationService,
        )

        OnboardingEmailVerificationService.dispatch_verification_email(
            username=result["username"],
            email=result.get("email", ""),
            full_name=result.get("full_name", ""),
            secret_key=current_app.secret_key,
            app_base_url=public_base_url(),
        )
    except Exception as e:  # noqa: BLE001 — never let SMTP wiring break signup
        logging.warning(
            "dispatch_verification_email failed for '%s': %s",
            result["username"],
            e,
        )

    # Phase 1 (2026-05-20): redirect to the new verify-email
    # step (logical step 2 of 7) instead of straight to step-2
    # (Account type, now logical step 3). The verify-email step
    # hard-blocks forward progress until the user clicks the inbox link.
    return redirect(url_for("analytics.onboarding_verify_email_step_page"))


# ============================================
# ONBOARDING DOMAIN-MATCH AUTO-JOIN PROMPT
# ============================================
# When a signup email's domain has been listed on an org's
# ``auto_join_email_domains`` allowlist, the start POST stashes the
# match + form payload in ``session['auto_join_candidate']`` and
# redirects here. The user picks Accept (join the matched org) or
# Create-my-own (fall back to the normal org_owner flow). The prompt
# is the only place the matched org's existence is surfaced, and only
# to a user whose own email is at that domain — see the
# DECISIONS.md entry for the privacy reasoning.


@analytics_bp.route("/onboarding/join-prompt")
def onboarding_join_prompt_page():
    """Wizard step 1.5 — render the 'Join {OrgName} on Kavanah?' prompt."""
    candidate = session.get("auto_join_candidate")
    if not candidate:
        return redirect(url_for("analytics.onboarding_start_page"))
    csrf_token = auth_module.refresh_csrf_token()
    return render_template(
        "onboarding_join_prompt.html",
        csrf_token=csrf_token,
        org_title=candidate.get("org_title", ""),
        domain=candidate.get("domain", ""),
    )


@analytics_bp.route("/onboarding/join-prompt", methods=["POST"])
def onboarding_join_prompt_post():
    """Accept or decline the domain-match auto-join offer.

    Two branches:

    * ``action=join`` — run ``signup_step1`` with the stashed form data,
      log the user in (same session shape as the regular signup), then
      park the join on the wizard session. The org membership
      + Viewer role are written only after the email is verified, by
      ``OnboardingService.apply_pending_domain_join`` from the verify
      handlers. Audit-logged as ``onboarding_domain_join``. Redirects to
      the verify-email step.
    * ``action=create_own`` — set ``session['auto_join_declined']`` so
      the next ``/onboarding/start`` POST skips the matcher, and bounce
      back to the signup form. Audit-logged as ``onboarding_domain_join_declined``.
    """

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return redirect(url_for("analytics.onboarding_join_prompt_page"))

    candidate = session.get("auto_join_candidate")
    if not candidate:
        # The candidate expired or the user navigated here directly.
        # Bounce to step 1 — no harm done, no signup attempted.
        return redirect(url_for("analytics.onboarding_start_page"))

    action = request.form.get("action", "")

    if action == "create_own":
        # The user explicitly declined the join offer. Mark the session
        # so the next start POST skips the matcher (prevents an infinite
        # prompt loop), audit-log the decline (so org owners can later
        # see how many domain matches were rejected if a reporting UI
        # ever ships), and bounce back to the signup form with the
        # already-typed form data prefilled.
        org_esid = candidate.get("org_esid", "")
        form = candidate.get("form") or {}
        session["auto_join_declined"] = True
        session.pop("auto_join_candidate", None)
        AuditLogService.log_event(
            form.get("username", "") or "(anonymous)",
            "onboarding_domain_join_declined",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"matched_org_esid={org_esid}",
            matched_org_esid=org_esid,
        )
        return render_template(
            "onboarding_start.html",
            csrf_token=auth_module.refresh_csrf_token(),
            errors=[],
            form_data={
                "username": form.get("username", ""),
                "full_name": form.get("full_name", ""),
                "email": form.get("email", ""),
            },
            google_sso_enabled=config.GOOGLE_SSO_ENABLED,
            microsoft_sso_enabled=config.MICROSOFT_SSO_ENABLED,
            invite_banner=_resolve_pending_invite_banner(session.get("pending_invite")),
        )

    if action != "join":
        # Unknown action — re-render the prompt rather than guessing.
        return redirect(url_for("analytics.onboarding_join_prompt_page"))

    # ── Accept branch ────────────────────────────────────────────────
    form = candidate.get("form") or {}
    org_esid = candidate.get("org_esid", "")
    org_title = candidate.get("org_title", "")

    from asset_management.web.services.onboarding_signup_service import (
        OnboardingSignupService,
    )

    result = OnboardingSignupService.signup_step1(
        username=form.get("username", ""),
        email=form.get("email", ""),
        password=form.get("password", ""),
        confirm_password=form.get("confirm_password", ""),
        full_name=form.get("full_name", ""),
        detected_time_zone=form.get("detected_time_zone", ""),
    )

    if not result.get("success"):
        # signup_step1 failed (username collision, weak password, etc.).
        # Drop the candidate and re-render the prompt's underlying form
        # with the surfaced errors so the user can fix and retry.
        session.pop("auto_join_candidate", None)
        return render_template(
            "onboarding_start.html",
            csrf_token=auth_module.refresh_csrf_token(),
            errors=result.get("errors", ["Could not create account."]),
            form_data={
                "username": form.get("username", ""),
                "full_name": form.get("full_name", ""),
                "email": form.get("email", ""),
            },
            google_sso_enabled=config.GOOGLE_SSO_ENABLED,
            microsoft_sso_enabled=config.MICROSOFT_SSO_ENABLED,
            invite_banner=_resolve_pending_invite_banner(session.get("pending_invite")),
        )

    # Mirror onboarding_start_post's auto-login shape so the user lands
    # on step 2 as a logged-in session. Kept identical (not extracted)
    # because the two flows will diverge once additional -adjacent
    # steps land — premature extraction would couple them.
    session.pop("logged_out", None)
    session["authenticated"] = True
    session["username"] = result["username"]
    session["roles"] = result.get("roles", [])
    session["is_admin"] = "superuser" in result.get("roles", []) or config.REQUIRED_ROLE in result.get("roles", [])
    session["full_name"] = result.get("full_name") or result["username"]
    session["email"] = result.get("email", "")
    session["auth_timestamp"] = time.time()
    session["last_activity"] = time.time()
    session["auth_method"] = "onboarding_signup"
    session.permanent = True

    AuditLogService.log_event(
        result["username"],
        "login",
        True,
        auth_method="onboarding_signup",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    # do NOT write the org role here. The match came from the
    # email the user TYPED, which is unverified, so provisioning now let
    # anyone who typed ``x@victim-domain`` into an opted-in org's tenant.
    # Park the join on the wizard session; the email-verification handlers
    # apply it (``OnboardingService.apply_pending_domain_join``) once the
    # inbox link is clicked, after re-matching the VERIFIED address. The
    # verify-email step this redirects to already hard-blocks the wizard
    # until then. Best-effort: a failed stash does not roll back signup.
    try:
        from asset_management.web.services.onboarding_service import (
            OnboardingService,
        )

        if not OnboardingService.record_pending_domain_join(
            result["username"],
            org_esid=org_esid,
            org_title=org_title,
            domain=candidate.get("domain", ""),
            email=result.get("email", ""),
        ):
            logging.warning(
                "record_pending_domain_join failed for '%s' org=%s",
                result["username"],
                org_esid,
            )
    except Exception as e:  # noqa: BLE001 — never let the stash break signup
        logging.warning(
            "record_pending_domain_join exception for '%s' org=%s: %s",
            result["username"],
            org_esid,
            e,
        )

    AuditLogService.log_event(
        result["username"],
        "onboarding_domain_join",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"matched_org_esid={org_esid} matched_org_title={org_title!r} pending_verification=1",
        matched_org_esid=org_esid,
    )

    # fire the verification email on the same daemon-thread
    # path as the regular signup. Kept here rather than extracted so the
    # join branch isn't a special case for verification semantics.
    try:
        from asset_management.web.services.onboarding_email_verification_service import (
            OnboardingEmailVerificationService,
        )

        OnboardingEmailVerificationService.dispatch_verification_email(
            username=result["username"],
            email=result.get("email", ""),
            full_name=result.get("full_name", ""),
            secret_key=current_app.secret_key,
            app_base_url=public_base_url(),
        )
    except Exception as e:  # noqa: BLE001 — never let SMTP wiring break signup
        logging.warning(
            "dispatch_verification_email failed for '%s': %s",
            result["username"],
            e,
        )

    # Clear the candidate and any stale "declined" marker before the
    # user lands on step 2 — both are step-1-scoped state.
    session.pop("auto_join_candidate", None)
    session.pop("auto_join_declined", None)

    # Phase 1 (2026-05-20): redirect to verify-email step
    # instead of step-2 (Account type). Hard-block on email
    # verification before the wizard proceeds.
    return redirect(url_for("analytics.onboarding_verify_email_step_page"))


def _apply_pending_domain_join(username: str) -> None:
    """apply a domain-match join parked by the join prompt.

    Called from the email-verification handlers. The service re-checks that
    the email is verified and still matches the same org before writing the
    org role, so calling this for a user with nothing pending, or not yet
    verified, is a no-op. Best-effort: never breaks the verify page.
    """
    if not username:
        return
    try:
        from asset_management.web.services.onboarding_service import (
            OnboardingService,
        )

        applied = OnboardingService.apply_pending_domain_join(username)
    except Exception as e:  # noqa: BLE001 — the verify page must still render
        logging.warning("apply_pending_domain_join exception for %r: %s", username, e)
        return
    if applied is None:
        return
    if not applied.success:
        logging.warning(
            "apply_pending_domain_join soft-failed for %r org=%s: %s",
            username,
            applied.org_esid,
            applied.errors,
        )
        return
    AuditLogService.log_event(
        username,
        "onboarding_domain_join_applied",
        True,
        ip_address=request.remote_addr if request else None,
        user_agent=request.headers.get("User-Agent", "") if request else "",
        detail=f"matched_org_esid={applied.org_esid} applied_after=email_verified",
        matched_org_esid=applied.org_esid,
    )


def _consume_invite_and_handoff(username: str, invite: dict):
    """consume the invite + route the invited user.

    Single source of truth for the invited-variant skip-and-provision
    path. Called from step-2 GET (auto-advance, no choice screen) and
    step-2 POST (when the user POSTs ``account_type=invited``). The
    explicit step-3/4 invited bare-redirects also defer to this so a
    user who deep-links step-3 with an unconsumed invite still gets
    provisioned + routed correctly.

    Behaviour:

    1. if the invite carries no ``team_esid``, branch on the
       workspace's team inventory: zero teams ⇒ auto-create a "General"
       team and proceed with that team_esid (the invitee lands as a
       team-tier member of General without seeing an extra screen);
       one-or-more teams ⇒ short-circuit to ``/onboarding/invite-team-picker``
       so the invitee picks a team or chooses "Create General". The
       picker page POSTs back into this helper with a chosen
       ``team_esid``; the AC's "never blocked" contract is preserved
       on both branches.
    2. Calls ``OnboardingService.provision_invited`` with the invite's
       scope (workspace / org / team) and ``role_esid`` applied at all
       three tiers — earlier invites only carry workspace scope
       today, so passing the role at the workspace tier is what honours
       the AC's "Role from invite token is honored" literally.
    3. Stamps ``account_type=invited`` on the user profile so a
       subsequent /dashboard or Settings visit sees the correct branch.
    4. Flips ``consumed=True`` on ``wizard_session.state['invite']``
       (one-time use; the back-button replay no longer re-offers the
       join tile) and calls ``InviteService.accept_invite`` to mark the
       ``de_authz_invite`` row ``status="accepted"``.
    5. Decides the handoff destination per the AC's "1 + (0 or 3)
       screens" rule: if the invite scoped to a team AND that team has
       at least one configured connector, hand off straight to
       ``/dashboard``; otherwise advance the wizard to step-4 so the
       invited user picks a connector before landing on the dashboard.
       The team-connector inventory is read via
       ``connector_status_service.get_team_connectors``; an outage
       there falls open to the show-4-6 branch (better to over-show
       the picker than to land an empty dashboard).

    Returns a Flask response (a redirect). Caller just returns it.
    """
    workspace_esid = invite.get("org_esid") or None
    team_esid = invite.get("team_esid") or None
    role_esid = invite.get("role_esid") or ""
    invitee_email = invite.get("invitee_email") or ""

    # missing-team edge. If the invite carries no team_esid
    # and the workspace has existing teams, send the invitee to the
    # picker so they choose (or auto-create General). The picker
    # re-enters this helper after stamping the invite with a chosen
    # team_esid. With zero teams in the workspace we auto-create
    # "General" inline — no extra screen, AC literal "never blocked".
    #
    # The picker path stashes the chosen team back into
    # ``wizard_session.state['invite']['team_esid']`` and then redirects
    # to step-2, which calls this helper again — the second pass sees
    # team_esid present and falls through to provision_invited.
    if workspace_esid and not team_esid:
        try:
            teams = InviteService.list_workspace_teams(workspace_esid)
        except Exception as e:  # noqa: BLE001 — fall-open if ES blips
            logging.warning("list_workspace_teams failed for %s: %s", workspace_esid, e)
            teams = []
        if teams:
            # Stash the invite payload onto the wizard_session state so
            # the picker route reads it back without a JWT round-trip,
            # then redirect. The picker will set team_esid and re-enter
            # the consume flow.
            try:
                from asset_management.web.services.wizard_session_service import (
                    WizardSessionService,
                )

                active = WizardSessionService.get_active(username, "onboarding") or {}
                state = dict(active.get("state") or {})
                existing_invite = dict(state.get("invite") or {})
                # Persist the picker payload (teams list + invite scope)
                # under a stable key so the picker page renders without
                # repeating the workspace-teams lookup.
                state["invite_team_picker"] = {
                    "teams": teams,
                    "workspace_esid": workspace_esid,
                    "role_esid": role_esid,
                    "invitee_email": invitee_email,
                }
                # Make sure the invite stays in state so the picker can
                # re-consume after the user submits.
                if existing_invite:
                    state["invite"] = existing_invite
                WizardSessionService.save(
                    user_id=username,
                    wizard_type="onboarding",
                    current_step="invite-team-picker",
                    state=state,
                )
            except Exception as e:  # noqa: BLE001 — stash is best-effort
                logging.warning("picker stash failed for %r: %s", username, e)
            AuditLogService.log_event(
                username,
                "onboarding_invite_team_picker",
                True,
                ip_address=request.remote_addr if request else None,
                user_agent=request.headers.get("User-Agent", "") if request else "",
                detail=(f"analytics_event=invite.team_picker.shown workspace={workspace_esid} " f"team_count={len(teams)}"),
            )
            return redirect(url_for("analytics.onboarding_invite_team_picker_page"))
        # No existing teams — auto-create "General" inline so the
        # invitee lands with a team-tier membership without an extra
        # screen. A None return from ensure_general_team falls open to
        # the (workspace-only) provision path: the AC's "never blocked"
        # contract — the invitee advances regardless.
        try:
            auto_team = InviteService.ensure_general_team(workspace_esid)
        except Exception as e:  # noqa: BLE001 — fall-open
            logging.warning("ensure_general_team failed for %s: %s", workspace_esid, e)
            auto_team = None
        if auto_team:
            team_esid = auto_team
            AuditLogService.log_event(
                username,
                "onboarding_invite_team_general",
                True,
                ip_address=request.remote_addr if request else None,
                user_agent=request.headers.get("User-Agent", "") if request else "",
                detail=(
                    f"analytics_event=invite.team_picker.auto_general workspace={workspace_esid} " f"team_esid={auto_team}"
                ),
            )

    try:
        from asset_management.web.services.onboarding_service import (
            OnboardingService,
        )
        from asset_management.web.services.user_profile_service import (
            ACCOUNT_TYPE_INVITED,
            UserProfileService,
        )
        from asset_management.web.services.wizard_session_service import (
            WizardSessionService,
        )
    except Exception as e:  # noqa: BLE001 — never let an import blip break the wizard
        logging.error("service import failed: %s", e)
        return redirect("/dashboard")

    # is_primary_workspace: True iff the invited user holds no
    # workspace memberships yet (first-time signup via invite). The
    # WorkspaceMembershipService has the canonical check; an error or
    # absence is treated as "primary" (the safer direction — most
    # invitees ARE first-time users).
    is_primary = True
    try:
        existing = WorkspaceMembershipService.get_workspaces_for_user(username)
        if existing:
            is_primary = False
    except Exception:  # noqa: BLE001 — defensive read, not load-bearing
        pass

    provision = OnboardingService.provision_invited(
        username,
        workspace_esid=workspace_esid,
        team_esid=team_esid,
        role_esid=role_esid,
        workspace_role_esid=role_esid,
        is_primary_workspace=is_primary,
    )
    if provision.errors:
        logging.warning(
            "provision_invited returned errors for %r: %s",
            username,
            provision.errors,
        )

    try:
        UserProfileService.set_account_type(username, ACCOUNT_TYPE_INVITED)
    except Exception as e:  # noqa: BLE001 — profile write is non-load-bearing for handoff
        logging.warning("set_account_type(invited) failed for %r: %s", username, e)

    try:
        active = WizardSessionService.get_active(username, "onboarding") or {}
        state = dict(active.get("state") or {})
        existing_invite = dict(state.get("invite") or {})
        existing_invite["consumed"] = True
        existing_invite["consumed_at"] = datetime.now(timezone.utc).isoformat()
        state["invite"] = existing_invite
        state["account_type"] = ACCOUNT_TYPE_INVITED
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-6",
            state=state,
        )
    except Exception as e:  # noqa: BLE001 — wizard_session write is non-load-bearing
        logging.warning("wizard_session consume-flip failed for %r: %s", username, e)

    if workspace_esid and invitee_email:
        accept_result = InviteService.accept_invite(workspace_esid, invitee_email)
        if accept_result.get("status") != "accepted":
            logging.info(
                "accept_invite non-accepted for ws=%s email=%s: %s",
                workspace_esid,
                invitee_email,
                accept_result,
            )

    AuditLogService.log_event(
        username,
        "onboarding_invited_consumed",
        bool(not provision.errors),
        ip_address=request.remote_addr if request else None,
        user_agent=request.headers.get("User-Agent", "") if request else "",
        detail=(f"workspace_esid={workspace_esid or ''} " f"team_esid={team_esid or ''} role_esid={role_esid}"),
    )

    # AC: skip steps 4-6 only if the invite's team already has a
    # configured connector. wired the team_esid path (sender
    # picker + receiver missing-team branch above), so this is now a
    # live decision — every consume reaches here with a non-None
    # team_esid unless ensure_general_team failed on a 0-team workspace.
    if team_esid:
        try:
            from asset_management.web.services.connector_status_service import (
                get_team_connectors,
            )

            payload = get_team_connectors(team_esid) or {}
            connectors = payload.get("connectors") or []
            has_any_configured = any(c.get("configured") for c in connectors)
            if has_any_configured:
                WizardSessionService.complete(username, "onboarding")
                # follow-up — clear the session-level wizard gate
                # in lockstep with the ES-level status flip so verify_auth
                # lets this user reach /dashboard without another bounce.
                session.pop("wizard_pending", None)
                return redirect("/dashboard")
        except Exception as e:  # noqa: BLE001 — fall-open to show-4-6 on lookup error
            logging.warning(
                "team-connector check failed for team=%s: %s",
                team_esid,
                e,
            )

    return redirect(url_for("analytics.onboarding_step6_page"))


def _step2_invite_from_session(username: str) -> dict | None:
    """Return the unconsumed invite stashed, or None.

    The wizard_session row owns the invite payload from
    ``/onboarding/start`` (POST) onward; ``consumed`` flips to True the
    first time step 2 commits an ``invited`` selection so a back-button
    + replay doesn't re-offer the join tile. owns the matching
    auto-skip-to-step-3 behaviour; only needs the read.
    """
    if not username:
        return None
    try:
        from asset_management.web.services.wizard_session_service import (
            WizardSessionService,
        )

        active = WizardSessionService.get_active(username, "onboarding")
    except Exception as e:  # noqa: BLE001 — never let a session read break the wizard
        logging.warning("wizard_session read failed for %r: %s", username, e)
        return None
    if not active:
        return None
    invite = (active.get("state") or {}).get("invite")
    if not isinstance(invite, dict):
        return None
    if invite.get("consumed"):
        return None
    return invite


@analytics_bp.route("/onboarding/verify-email-step")
def onboarding_verify_email_step_page():
    """Wizard logical step 2 — verify your email.

    Inserted between the sign-up POST handoff and the existing step-2
    (Account type). User must click the verification link in their
    inbox before they can advance.

    Hard-block contract:
      * GET: renders the verify-email card with the user's email,
        a 'Resend' affordance, and a 'I've verified — continue' button.
      * The Continue button is server-validated on POST: if
        ``is_email_verified`` returns False, the user is bounced back
        to this same step with an "email not yet verified" banner.
        No client-side trust.

    Forward navigation only happens via the POST handler when
    ``is_email_verified`` returns True. Browser back, refresh, and
    deep-link bookmarks all land on this page if the email isn't
    verified yet — there is no skip path.

    Persists ``wizard_session.current_step="verify-email"`` on first
    arrival so resume lands the user back here on next login.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    csrf_token = auth_module.refresh_csrf_token()
    user_email = session.get("email", "") or ""

    # Persist current_step on first hit so resume on next login lands
    # the user back here (not on /dashboard, not on step-2). Best-
    # effort; a save failure does not block the render.
    try:
        from asset_management.web.services.wizard_session_service import (
            WizardSessionService,
        )

        active = WizardSessionService.get_active(username, "onboarding") or {}
        state = dict(active.get("state") or {})
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="verify-email",
            state=state,
        )
    except Exception as e:  # noqa: BLE001 — non-blocking
        logging.warning("Phase 1 verify-email-step save failed for %r: %s", username, e)

    # Surface a banner if the previous Continue attempt failed.
    not_yet_verified = request.args.get("not_yet_verified") == "1"

    # Item 4: the Continue button is disabled on initial render
    # until email_verified flips True. The status-poll endpoint below
    # flips it client-side without a page reload. Reading the live flag
    # here covers the "user clicked the link in another tab before
    # landing on this page" case (initial render already shows
    # Continue enabled, no poll needed).
    from asset_management.web.services.onboarding_email_verification_service import (
        OnboardingEmailVerificationService,
    )

    try:
        email_verified = OnboardingEmailVerificationService.is_email_verified(username)
    except Exception as e:  # noqa: BLE001 — fail-closed for the gate
        logging.warning("Item 4 is_email_verified raise for %r: %s", username, e)
        email_verified = False

    return render_template(
        "onboarding_verify_email_step.html",
        csrf_token=csrf_token,
        user_email=user_email,
        not_yet_verified=not_yet_verified,
        email_verified=email_verified,
        progress=_wizard_progress_for("verify-email", username),
    )


@analytics_bp.route("/onboarding/verify-email-step/status")
def onboarding_verify_email_step_status():
    """Item 4 — JSON status probe for the Continue-button poll.

    The step-2 template renders the Continue button disabled by
    default; client-side JS polls this endpoint every ~5 seconds and
    enables the button when the response flips to
    ``{"verified": true}``. Server-side POST still re-checks the
    flag — this endpoint only removes the friction of forcing the
    user to click a "I've verified, recheck" button.

    Returns ``{"verified": bool}`` with HTTP 200 on every path that
    has a session, including ES-outage / lookup-failure paths
    (fail-closed so the button stays disabled rather than enabling
    on a transient blip). Returns 401 with no body on missing
    session.
    """

    from flask import jsonify

    username = session.get("username", "")
    if not username:
        return jsonify({"verified": False}), 401

    from asset_management.web.services.onboarding_email_verification_service import (
        OnboardingEmailVerificationService,
    )

    try:
        verified = OnboardingEmailVerificationService.is_email_verified(username)
    except Exception as e:  # noqa: BLE001 — fail-closed
        logging.warning(
            "Item 4 status-poll is_email_verified raise for %r: %s",
            username,
            e,
        )
        verified = False

    return jsonify({"verified": bool(verified)})


@analytics_bp.route("/onboarding/verify-email-step", methods=["POST"])
def onboarding_verify_email_step_post():
    """Continue handoff from the verify-email step.

    Server-validates that the user actually clicked the inbox link
    before advancing — there is no client-side trust. Two outcomes:

    * Verified → 302 to /onboarding/step-4 (Account type, logical
      step 3 of 7).
    * Not yet verified → 302 back to this page with
      ``?not_yet_verified=1`` so the template surfaces a banner.

    The actual verification happens on the
    ``/onboarding/verify-email/<token>`` link the SES email points
    at; that route flips ``metadata.email_verified=True``. This
    handler ONLY reads that flag.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        abort(403)

    from asset_management.web.services.onboarding_email_verification_service import (
        OnboardingEmailVerificationService,
    )

    try:
        verified = OnboardingEmailVerificationService.is_email_verified(username)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        # Fail closed on the wizard step: if we can't confirm, keep
        # them on this step. The login gate's fail-open policy
        # applies to /login, not to forward wizard progression.
        logging.warning("Phase 1 is_email_verified raise for %r: %s", username, e)
        return redirect(url_for("analytics.onboarding_verify_email_step_page", not_yet_verified=1))

    if not verified:
        return redirect(url_for("analytics.onboarding_verify_email_step_page", not_yet_verified=1))

    # a domain-match join parked at the join prompt is owed now
    # that the email is verified. Normally the inbox link already applied
    # it; this catches a transient failure there. Idempotent.
    _apply_pending_domain_join(username)

    # Verified — advance to mfa-setup-step (Two-factor, logical step 3
    # of 8). Phase 2 (2026-05-20) inserted MFA setup between
    # verify-email and step-2; prior to Phase 2 this jumped straight
    # to /onboarding/step-4.
    return redirect(url_for("analytics.onboarding_mfa_setup_step_page"))


@analytics_bp.route("/onboarding/verify-email-step/resend", methods=["POST"])
def onboarding_verify_email_step_resend():
    """Resend the verification email.

    Re-fires the same async send that onboarding_start_post triggers
    on sign-up, so a user whose first email got lost (delayed SES,
    typo on the address that needs profile fix) can re-request
    without going back to sign-up.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        abort(403)

    from asset_management.web.services.onboarding_email_verification_service import (
        OnboardingEmailVerificationService,
    )

    user_email = session.get("email", "") or ""
    full_name = session.get("full_name", "") or username

    if user_email:
        # Same fire-and-forget pattern as the signup hop — the
        # service spawns a background thread for SMTP.
        try:
            OnboardingEmailVerificationService.dispatch_verification_email(
                username=username,
                email=user_email,
                full_name=full_name,
                secret_key=current_app.secret_key,
            )
        except Exception as e:  # noqa: BLE001 — never block on email
            logging.warning("Phase 1 resend failed for %r: %s", username, e)

    return redirect(url_for("analytics.onboarding_verify_email_step_page", resent=1))


@analytics_bp.route("/onboarding/verify-email-step/change-email", methods=["POST"])
def onboarding_verify_email_step_change_email():
    """Change the user's email AND resend verification to the new address.

     follow-up (2026-05-21). The original verify-email step
    assumed the user typed their email correctly on signup; the only
    recovery for a typo was "resend to the same address" or "contact
    support". This handler closes that gap:

      1. Validate the new email (non-empty, contains @).
      2. Call OnboardingEmailVerificationService.update_email which
         updates the ES Security user's email field AND clears
         metadata.email_verified so the Continue button stays
         disabled until the new address gets confirmed.
      3. Update ``session['email']`` so the rest of the wizard +
         the resend affordance both use the new value.
      4. Dispatch a fresh verification email to the new address.
      5. Redirect back to /onboarding/verify-email-step with a
         ?changed=1 flag so the template can surface a success
         banner.

    Same fail-soft pattern as the resend route: an SMTP blip doesn't
    block; the new email is already on the ES user record so the
    user can hit Resend manually if the dispatched email never arrives.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        abort(403)

    new_email = (request.form.get("new_email") or "").strip()
    if not new_email or "@" not in new_email:
        return redirect(
            url_for(
                "analytics.onboarding_verify_email_step_page",
                change_email_error="invalid",
            )
        )

    from asset_management.web.services.onboarding_email_verification_service import (
        OnboardingEmailVerificationService,
    )

    result = OnboardingEmailVerificationService.update_email(username, new_email)
    if not result.get("success"):
        logging.warning(
            "onboarding_email_change update_email failed for %r: %s",
            username,
            result.get("error"),
        )
        return redirect(
            url_for(
                "analytics.onboarding_verify_email_step_page",
                change_email_error="server",
            )
        )

    if result.get("unchanged"):
        # User submitted the same email they already had. No-op +
        # surface the resent banner so they get feedback that
        # something happened.
        return redirect(url_for("analytics.onboarding_verify_email_step_page", resent=1))

    # Update the session so other surfaces (resend, profile page) see
    # the new email immediately. Audit log on the service-side
    # already records the change.
    session["email"] = new_email

    full_name = session.get("full_name", "") or username
    try:
        OnboardingEmailVerificationService.dispatch_verification_email(
            username=username,
            email=new_email,
            full_name=full_name,
            secret_key=current_app.secret_key,
        )
    except Exception as e:  # noqa: BLE001 — never block on email
        logging.warning(
            "onboarding_email_change verification dispatch failed " "for %r: %s",
            username,
            e,
        )

    return redirect(url_for("analytics.onboarding_verify_email_step_page", changed=1))


# follow-up (2026-05-21): the /onboarding/verify-email-step/back
# POST handler that lived here was removed along with the Back button
# in the template. The escape hatch was added follow-up so
# a user who entered a typo'd email could roll back the half-provisioned
# account; in practice it was confusing (modal asked users to confirm
# deleting their own account mid-signup) and the wrong-email recovery
# path is now "contact support / clear cookies / use the resend button
# with a corrected address". If the back-out flow is needed again,
# resurrect it from git history at parent SHA 6c5d0fcd110 — the
# implementation called WizardResumeService.restart +
# es.security.delete_user + cascade_purge_authz_for_user + session.clear.


@analytics_bp.route("/onboarding/mfa-setup-step")
def onboarding_mfa_setup_step_page():
    """Wizard logical step 3 — set up two-factor authentication
    .

    Inserted between verify-email-step (logical step 2) and step-2
    (Account type, logical step 4). User must configure at least
    one confirmed second factor (TOTP via authenticator app OR
    yubikey) before they can advance.

    Two GET render modes:
      * ``?method=`` absent or invalid → method picker (TOTP / yubikey).
      * ``?method=totp`` → renders a TOTP QR code + a 6-digit
        confirmation input.
      * ``?method=yubikey`` → renders the yubikey OTP input.

    Forward navigation (POST with action=continue) only succeeds when
    ``MFAService.has_any_factor`` returns True. Failure modes (no
    factor yet, ES outage) bounce back to the picker with a banner.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    csrf_token = auth_module.refresh_csrf_token()
    method = (request.args.get("method") or "").strip().lower()

    # Persist current_step on first hit so resume lands the
    # user back here.
    try:
        from asset_management.web.services.wizard_session_service import (
            WizardSessionService,
        )

        active = WizardSessionService.get_active(username, "onboarding") or {}
        state = dict(active.get("state") or {})
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="mfa-setup",
            state=state,
        )
    except Exception as e:  # noqa: BLE001 — non-blocking
        logging.warning("Phase 2 mfa-setup-step save failed for %r: %s", username, e)

    from asset_management.web.services.mfa_service import MFAService

    # follow-up (2026-05-21): the wizard MFA-setup step is for
    # NEW users only. The prior ``already_configured`` auto-advance
    # used MFAService.has_any_factor as the signal; in practice that
    # signal fired on stale MFA records left behind by a previously-
    # deleted user with the same username (the cascade-purge missed
    # the MFA index). Waving a new user through on that signal would
    # let them inherit someone else's factor. Always show the picker;
    # if the user truly does already have a factor for some legitimate
    # reason (admin pre-provisioned, etc.) picking again just
    # overwrites the doc — same end state, no security hazard.

    error = request.args.get("error", "")
    not_yet_configured = request.args.get("not_yet_configured") == "1"

    # TOTP path: generate or surface the existing secret.
    totp_payload = None
    if method == "totp":
        try:
            totp_payload = MFAService.generate_totp_secret(username)
        except Exception as e:  # noqa: BLE001 — render-time fallback
            logging.error("Phase 2 generate_totp_secret failed for %r: %s", username, e)
            return redirect(url_for("analytics.onboarding_mfa_setup_step_page", error="totp_setup_failed"))

    return render_template(
        "onboarding_mfa_setup_step.html",
        csrf_token=csrf_token,
        method=method,
        totp_payload=totp_payload,
        not_yet_configured=not_yet_configured,
        error=error,
        progress=_wizard_progress_for("mfa-setup", username),
    )


def _wizard_show_recovery_codes_or_continue(username: str):
    """Tier 1 — after the wizard's first MFA enrollment, generate the
    user's recovery codes (only if they have none yet) and route them through
    the shared one-time display screen at bare ``/mfa/recovery-codes``. Its
    Continue handler reads ``_recovery_codes_return_to == "wizard"`` and advances
    to step 4. If the user already has codes (re-enrollment) or generation fails,
    advance to step 4 directly — never block enrollment on this convenience.

    The display screen lives outside the blueprint at the canonical bare
    ``/mfa/*`` path (in the PrefixMiddleware allow-list), so we redirect to the
    literal path rather than url_for (which would prepend ``/assets``).
    """
    from asset_management.web.services.mfa_service import MFAService

    try:
        if MFAService.count_recovery_codes(username) > 0:
            return redirect(url_for("analytics.onboarding_step4_page"))
        result = MFAService.generate_recovery_codes(username)
    except Exception as e:  # noqa: BLE001 — top-level boundary; never block the wizard
        logging.warning("wizard recovery-code generation raised for %r: %s", username, e)
        return redirect(url_for("analytics.onboarding_step4_page"))

    if not result.get("success"):
        logging.error("wizard recovery-code generation failed for %r: %s", username, result.get("error"))
        return redirect(url_for("analytics.onboarding_step4_page"))

    try:
        AuditLogService.log_event(
            username,
            "recovery_codes_generated",
            True,
            target_user=username,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
        )
    except Exception:  # noqa: BLE001 — audit is best-effort
        pass

    session["_recovery_codes_plaintext"] = result["plaintext"]
    session["_recovery_codes_method"] = "totp"  # unused on the wizard return path
    session["_recovery_codes_return_to"] = "wizard"
    return redirect("/mfa/recovery-codes")


@analytics_bp.route("/onboarding/mfa-setup-step", methods=["POST"])
def onboarding_mfa_setup_step_post():
    """Method-choose / TOTP-confirm / yubikey-register / continue
    handoff for the MFA setup step.

    Action discriminator (form field ``action``):
      * ``choose_totp`` → POST sets MFA method, GET redirects to
        ?method=totp.
      * ``choose_yubikey`` → POST sets MFA method, GET redirects to
        ?method=yubikey.
      * ``confirm_totp`` → validates the 6-digit code via
        MFAService.verify_and_confirm_totp; on success, advances to
        step-2. Failure surfaces an error banner.
      * ``register_yubikey`` → validates the yubikey OTP via
        MFAService.register_yubikey; on success, advances to step-2.
      * ``continue`` → server-checks has_any_factor; advances to
        step-2 if True, bounces back if False.

    Any other action (or missing) → method picker.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        abort(403)

    from asset_management.web.services.mfa_service import MFAService

    action = (request.form.get("action") or "").strip().lower()

    if action == "choose_totp":
        try:
            MFAService.set_mfa_method(username, "totp")
        except Exception as e:  # noqa: BLE001 — fail-soft
            logging.warning("Phase 2 set_mfa_method(totp) failed for %r: %s", username, e)
        return redirect(url_for("analytics.onboarding_mfa_setup_step_page", method="totp"))

    if action == "choose_yubikey":
        try:
            MFAService.set_mfa_method(username, "yubikey")
        except Exception as e:  # noqa: BLE001 — fail-soft
            logging.warning("Phase 2 set_mfa_method(yubikey) failed for %r: %s", username, e)
        return redirect(url_for("analytics.onboarding_mfa_setup_step_page", method="yubikey"))

    if action == "confirm_totp":
        code = (request.form.get("code") or "").strip()
        try:
            ok = MFAService.verify_and_confirm_totp(username, code)
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.warning("Phase 2 verify_and_confirm_totp raised for %r: %s", username, e)
            ok = False
        if not ok:
            return redirect(
                url_for(
                    "analytics.onboarding_mfa_setup_step_page",
                    method="totp",
                    error="invalid_code",
                )
            )
        # Tier 1: show one-time recovery codes before advancing to
        # step 4 (wizard enrollment is the new user's first factor — they must
        # see their codes here, the same as the login/profile enrollment paths).
        return _wizard_show_recovery_codes_or_continue(username)

    if action == "register_yubikey":
        otp = (request.form.get("otp") or "").strip()
        try:
            result = MFAService.register_yubikey(
                username=username,
                otp=otp,
                yubico_client_id=config.YUBICO_CLIENT_ID,
                yubico_secret_key=config.YUBICO_SECRET_KEY,
                allow_insecure_dev_mode=config.MFA_ALLOW_INSECURE_DEV,
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.warning("Phase 2 register_yubikey raised for %r: %s", username, e)
            result = {"success": False}
        if not result.get("success"):
            return redirect(
                url_for(
                    "analytics.onboarding_mfa_setup_step_page",
                    method="yubikey",
                    error="yubikey_register_failed",
                )
            )
        # Tier 1: show one-time recovery codes before advancing.
        return _wizard_show_recovery_codes_or_continue(username)

    # action=continue — server-validates the user has at least one
    # confirmed factor. Forward progression is server-gated; no client
    # trust.
    try:
        has_factor = MFAService.has_any_factor(username)
    except Exception as e:  # noqa: BLE001 — fail-closed (login-side gates fail open; wizard-side fail closed)
        logging.warning("Phase 2 has_any_factor raised for %r: %s", username, e)
        has_factor = False

    if not has_factor:
        return redirect(url_for("analytics.onboarding_mfa_setup_step_page", not_yet_configured=1))

    return redirect(url_for("analytics.onboarding_step4_page"))


# ─────────────────────────────────────────────────────────────────────────────
# follow-up (2026-05-21) — URL path and UI step number now
# match. The original documented an Option B mapping table to
# paper over the drift introduced (inserting verify-email +
# mfa-setup ahead of the existing step-N URLs without renaming them).
# a teammate flagged the drift as a real-world papercut on staging — the
# URL bar saying "step-5" while the eyebrow said "Step 7 of 8" was
# user-confusing AND code-review-confusing. This follow-up flipped to
# Option A (rename the routes) so the two namespaces agree:
#
#   URL path                          UI label         Screen
#   ───────────────────────────────   ──────────────   ────────────────────
#   /onboarding/start                 (pre-wizard)     Welcome
#   /onboarding/verify-email-step     Step 2 of 8      Verify your email
#   /onboarding/mfa-setup-step        Step 3 of 8      Set up two-factor
#   /onboarding/step-4                Step 4 of 8      Account type
#   /onboarding/step-5                Step 5 of 8      Workspace
#   /onboarding/step-6                Step 6 of 8      Connect a source
#   /onboarding/step-7                Step 7 of 8      Review permissions
#   /onboarding/step-8                Step 8 of 8      Pick your accounts
#   /onboarding/step7/building        (post-wizard)    Backfill animation
#
# Three constants must stay in lockstep with this table:
#   * ``wizard_progress_service._STEP_ORDER`` — the canonical sequence
#     used to compute step-number-of-N for the progress rail.
#   * ``wizard_resume_service._STEP_ROUTE_NAMES`` — maps the slug
#     stamped on ``wizard_session.current_step`` to the endpoint that
#     renders that step. Also carries a back-compat shim for legacy
#     "step-2" / "step-3" slugs written before the rename.
#   * ``auth._CSRF_EXEMPT_ENDPOINTS`` + ``verify_auth`` pre-auth list —
#     every wizard route is named explicitly; adding a step requires
#     two new entries here.
#
# Adding a new wizard step is a four-file change: route decl + fn in
# this file, plus those three constants. carries a release-
# plan test that asserts agreement between
# the documented mapping and the runtime _STEP_ORDER constant so any
# future reorder that forgets one of the four sites fails CI.
# ─────────────────────────────────────────────────────────────────────────────


# under paid plans, step 4 only exposes the self-serve org
# path. "Just me" (individual) is hidden and "Agency / multi-client"
# (agency) is rendered disabled with a contact-sales prompt; neither is
# selectable in the UI. This allowlist is the server-side boundary that
# enforces the same constraint against a forged/stale form POST — it is
# a strict subset of VALID_ACCOUNT_TYPES, which stays unchanged so the
# Settings page and existing-user records can still carry the other
# types. ``invited`` is handled by its own wizard_session gate below and
# is intentionally NOT in this self-serve set.
_STEP4_SELF_SERVE_ACCOUNT_TYPES = frozenset({"org_owner"})


@analytics_bp.route("/onboarding/step-4")
def onboarding_step4_page():
    """Wizard step 2 — account-type picker.

    Tile-grid UI with four entry points: ``individual``, ``org_owner``,
    ``agency``, and (conditionally) ``invited``. The invited tile only
    renders when a invite payload is sitting on the user's
    wizard_session — otherwise an arbitrary signup could pick "Joining
    a team" with no invite to anchor it.

    Default selection follows the spec: ``invited`` when an invite is
    present, else ``org_owner`` (the most common paid case).
    """
    username = session.get("username", "")
    # follow-up — block re-entry after the wizard is done.
    _done_redirect = _redirect_if_wizard_done(username)
    if _done_redirect is not None:
        return _done_redirect
    invite = _step2_invite_from_session(username)
    # invited variant auto-advances the step-2 choice screen.
    # An invited user has no decision to make on this page (the invite
    # already named their scope), so showing the tile picker just adds
    # a screen to the "at most 1 + (0 or 3)" budget for no UX benefit.
    # Provision + route immediately; the user lands on step-4 (or
    # /dashboard if the team-connector branch fires) on the same hop.
    if invite:
        return _consume_invite_and_handoff(username, invite)

    # Restore the user's previous choice when they back-nav into this
    # step. POST persists to BOTH UserProfileService and
    # wizard_session.state["account_type"]; the session row is the
    # cheaper read (no ES security.get_user call) so we try it first
    # and fall back to the profile metadata for the resume case where
    # the session row got pruned.
    default = "org_owner"
    try:
        from asset_management.web.services.user_profile_service import (
            UserProfileService,
        )
        from asset_management.web.services.wizard_session_service import (
            WizardSessionService,
        )

        _active = WizardSessionService.get_active(username, "onboarding") or {}
        _state = _active.get("state") or {}
        _prior = (_state.get("account_type") or "").strip()
        if not _prior:
            _prior = (UserProfileService.get_account_type(username) or "").strip()
        # only restore a prior choice that is still selectable
        # on this step. A stored individual/agency (e.g. set from Settings
        # or a earlier wizard run) must not become the hidden-input
        # default, or clicking Continue would POST a now-rejected value.
        if _prior in _STEP4_SELF_SERVE_ACCOUNT_TYPES:
            default = _prior
    except Exception as e:  # noqa: BLE001 — never block render
        logging.warning("step-4 prior-selection lookup failed for %r: %s", username, e)

    csrf_token = auth_module.refresh_csrf_token()

    # canonical funnel envelope. The pre-existing
    # ``onboarding_step4_account_type`` POST event records the chosen
    # account_type; the ``wizard.step4.entered`` event below is the
    # entry-counterpart so a marketing funnel can count step-2 reach
    # against step-2 finish. context.account_type may be empty here —
    # account_type is the OUTPUT of this step.
    try:
        from asset_management.web.services.wizard_session_service import (
            WizardSessionService,
        )

        active = WizardSessionService.get_active(username, "onboarding")
        ctx = WizardAnalyticsService.build_context(
            username=username,
            wizard_session=active,
        )
        WizardAnalyticsService.emit(
            step="step-4",
            action=ACTION_ENTERED,
            context=ctx,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
        )
    except Exception as e:  # noqa: BLE001 — emitter must never block render
        logging.warning("step-2 entered emit failed for %r: %s", username, e)

    return render_template(
        "onboarding_step4_account_type.html",
        csrf_token=csrf_token,
        username=username,
        full_name=session.get("full_name", ""),
        invite=invite,
        selected=default,
        error=None,
        progress=_wizard_progress_for("step-4", username),
    )


@analytics_bp.route("/onboarding/step-4", methods=["POST"])
def onboarding_step4_post():
    """Persist the chosen ``account_type`` and advance the wizard.

    Writes to two surfaces:

    * ``UserProfileService.set_account_type`` — the metadata
      field that downstream branching (steps 3-6, Settings, audit log)
      reads from. Validated against ``VALID_ACCOUNT_TYPES`` before the
      ES call.
    * ``wizard_session.state['account_type']`` — same value, kept on
      the wizard row so a refresh / back-button replay sees the choice
      without re-reading the user record.

    The ``invited`` selection is only honoured when a current
    wizard_session invite exists; otherwise the route falls through to
    the form re-render with an explanatory error rather than silently
    downgrading. ``current_step`` advances to ``step-3`` so a wizard
    resumer lands on the workspace step (owns that route, so
    for now the redirect is to ``/dashboard`` as a soft handoff).
    """
    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return redirect(url_for("analytics.onboarding_step4_page"))

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    # follow-up — block re-entry POSTs after the wizard is done.
    _done_redirect = _redirect_if_wizard_done(username)
    if _done_redirect is not None:
        return _done_redirect

    from asset_management.web.services.user_profile_service import (
        UserProfileService,
        VALID_ACCOUNT_TYPES,
    )

    account_type = (request.form.get("account_type") or "").strip()
    invite = _step2_invite_from_session(username)

    def _rerender(error_msg: str, selected: str | None = None, status: int = 400):
        return (
            render_template(
                "onboarding_step4_account_type.html",
                csrf_token=auth_module.refresh_csrf_token(),
                username=username,
                full_name=session.get("full_name", ""),
                invite=invite,
                selected=selected or ("invited" if invite else "org_owner"),
                error=error_msg,
                progress=_wizard_progress_for("step-4", username),
            ),
            status,
        )

    # build the canonical context once at the top of the POST
    # so every error/success branch reuses it. account_type defaults to
    # the submitted form value so an ``errored`` event is still
    # attributable to the choice the user tried to make.
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    _wa_active = WizardSessionService.get_active(username, "onboarding")
    _wa_ctx = WizardAnalyticsService.build_context(
        username=username,
        wizard_session=_wa_active,
        account_type=account_type or "",
    )

    if account_type not in VALID_ACCOUNT_TYPES:
        WizardAnalyticsService.emit(
            step="step-4",
            action=ACTION_ERRORED,
            context=_wa_ctx,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"error_code": "invalid_account_type"},
        )
        return _rerender("Pick an option to continue.")

    # reject the non-self-serve types (individual / agency) at
    # the POST boundary. The template hides "Just me" and disables
    # "Agency / multi-client", but a forged or stale form could still
    # submit them; this is where "cannot be selected to advance" is
    # actually enforced. ``invited`` is a valid type with its own
    # wizard_session gate immediately below, so it is exempt here.
    if account_type != "invited" and account_type not in _STEP4_SELF_SERVE_ACCOUNT_TYPES:
        WizardAnalyticsService.emit(
            step="step-4",
            action=ACTION_ERRORED,
            context=_wa_ctx,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"error_code": "account_type_not_self_serve"},
        )
        if account_type == "agency":
            return _rerender(
                "Agency / multi-client setup isn't self-serve yet. " "Contact sales@kavanahanalytics.com to get started."
            )
        return _rerender("Pick an option to continue.")

    if account_type == "invited" and not invite:
        # The invited tile is gated client-side, but the POST is the
        # boundary that has to enforce it — a user without an invite
        # selecting "invited" would otherwise leave the wizard without
        # an org/team to join.
        WizardAnalyticsService.emit(
            step="step-4",
            action=ACTION_ERRORED,
            context=_wa_ctx,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"error_code": "invited_without_invite"},
        )
        return _rerender(
            'An invite is required to choose "Joining a team". '
            "Pick a different option or use the invite link you were sent."
        )

    # invited variant: defer to the consume helper for the
    # provision + accept + route hop. The helper writes account_type +
    # the wizard_session consumed flag itself, so we don't fall through
    # to the regular ``set_account_type`` / wizard_session.save below.
    if account_type == "invited":
        # The invited variant completes step-2 implicitly — the invite
        # already named the account_type and the consume helper performs
        # provision + handoff. Fire ``completed`` here so the funnel
        # still sees a single canonical step-2 finish event for invited
        # users.
        WizardAnalyticsService.emit(
            step="step-4",
            action=ACTION_COMPLETED,
            context=_wa_ctx,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"variant": "invited"},
        )
        return _consume_invite_and_handoff(username, invite)

    profile_result = UserProfileService.set_account_type(username, account_type)
    if not profile_result.get("success"):
        WizardAnalyticsService.emit(
            step="step-4",
            action=ACTION_ERRORED,
            context=_wa_ctx,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"error_code": "set_account_type_failed"},
        )
        return _rerender(
            profile_result.get("error") or "Could not save your selection. Please try again.",
            selected=account_type,
            status=500,
        )

    try:
        active = WizardSessionService.get_active(username, "onboarding") or {}
        state = dict(active.get("state") or {})
        state["account_type"] = account_type
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-5",
            state=state,
        )
    except Exception as e:  # noqa: BLE001 — never let a session write break the wizard
        logging.warning("wizard_session save failed for %r: %s", username, e)

    AuditLogService.log_event(
        username,
        "onboarding_step4_account_type",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"account_type={account_type}",
    )

    # canonical step-2 completion. Re-uses the context built
    # at the top of the POST but with the now-saved account_type, so
    # the funnel event matches what was actually persisted.
    WizardAnalyticsService.emit(
        step="step-4",
        action=ACTION_COMPLETED,
        context=WizardAnalyticsService.build_context(
            username=username,
            wizard_session=WizardSessionService.get_active(username, "onboarding"),
            account_type=account_type,
        ),
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
    )

    return redirect(url_for("analytics.onboarding_step5_page"))


def _step5_workspace_option_lists() -> dict:
    """Build the TZ + currency <select> option lists for step-5.

     follow-up: replace the free-text wizard inputs with
    dropdowns so a user can't fat-finger a non-IANA zone or a typo'd
    currency code. The lenient ``WorkspaceSettingsService.sanitize``
    contract stays unchanged — it still falls back to UTC/USD on any
    unknown input so a JSON client or a forged form post degrades
    safely. The <select> just makes invalid input unreachable via the
    UI.

    The TZ list comes from ``zoneinfo.available_timezones`` so it tracks
    the host's tzdata. The currency list is the curated short-list on
    ``WorkspaceSettingsService.CURRENCY_OPTIONS`` (high-volume codes the
    dashboard's ad-platform exporters return). The downstream sanitize
    rule still accepts any 3-letter alpha string, so an API client can
    submit any code; the picker stays opinionated.
    """
    try:
        from zoneinfo import available_timezones
    except ImportError:  # pragma: no cover — Python 3.9+ ships zoneinfo
        tz_options = ["UTC"]
    else:
        zones = available_timezones()
        filtered = [
            z for z in zones if "/" in z and not z.startswith(("SystemV/", "US/", "Canada/", "Brazil/", "Chile/", "Mexico/"))
        ]
        filtered.sort()
        tz_options = ["UTC", *filtered]

    try:
        from asset_management.web.services.workspace_settings_service import (
            CURRENCY_OPTIONS,
        )
    except ImportError:
        from web.services.workspace_settings_service import (  # type: ignore[no-redef]
            CURRENCY_OPTIONS,
        )

    return {
        "timezone_options": tz_options,
        "currency_options": list(CURRENCY_OPTIONS),
    }


# entry points whose step-5 form exposes an editable team-name
# field. Agency is deliberately excluded: its team is named after the
# first client org (the team belongs to the client org, not the agency
# workspace), so naming it in step 5 would muddy the "client = org"
# terminology. Invited users never see step 5 at all.
_STEP5_TEAM_NAME_ENTRY_POINTS = ("individual", "org_owner")


def _step5_team_name_default(account_type: str, *, org_title_default: str) -> str:
    """Default team name shown in the step-5 team-name field.

    Prefills the field with a sensible starting value (the field is
    required, so this is the value the user sees and can keep or edit):

    * ``org_owner`` → ``"Team 1"`` (renamed from the historical "General"
      org_owner default per a teammate 2026-06-22).
    * ``individual`` → the org-name default (the individual team is named
      after the org by convention).

    ``org_title_default`` is the org-name value the route already computed
    for the org-name input, so the individual default tracks whatever the
    user has typed / the email-local fallback without recomputing it here.
    Any other (non-team-field) account type returns an empty string; the
    caller gates rendering on ``_STEP5_TEAM_NAME_ENTRY_POINTS`` anyway.
    """
    from asset_management.web.services.user_profile_service import (
        ACCOUNT_TYPE_ORG_OWNER,
    )

    if account_type == ACCOUNT_TYPE_ORG_OWNER:
        return "Team 1"
    return org_title_default


# ============================================
# ONBOARDING STEP 3 — WORKSPACE
# ============================================
# Per-entry-point workspace-naming step. The branching service
# maps the step-2 ``account_type`` to title template, copy strings, and
# whether the agency client field renders. ``invited`` users skip step 3
# entirely (their org/team scope was set by the invite) and bounce to
# step-4 instead.
#
# carves out one branch from the "no provision until
# commit" rule: the **agency** branch calls ``provision_agency`` on the
# step-3 POST so the multi-workspace flag, the first client workspace,
# and the Owner role are stamped before the user lands on step-4. The
# ticket's acceptance ("First client workspace exists at end of step 3")
# is unsatisfiable any other way. Non-agency branches keep the
# defer — their org/team identity is still wizard-intent until the
# downstream commit step.


@analytics_bp.route("/onboarding/step-5")
def onboarding_step5_page():
    """Wizard step 3 — workspace creation screen.

    Reads ``account_type`` from wizard_session (falling back to
    ``UserProfileService.get_account_type``), feeds it through
    ``OnboardingBranchingService.for_account_type`` for copy / template,
    and renders the workspace-naming form. ``invited`` users never see
    this step — they bounce to step-4 because their org/team scope was
    set by the invite.

    Pre-filled values come from prior wizard_session state when one
    exists (refresh resumes); otherwise they come from the branching
    service's templates rendered against the user's email local-part.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    # follow-up — block re-entry after the wizard is done.
    _done_redirect = _redirect_if_wizard_done(username)
    if _done_redirect is not None:
        return _done_redirect

    from asset_management.web.services.onboarding_branching_service import (
        OnboardingBranchingService,
    )
    from asset_management.web.services.user_profile_service import (
        ACCOUNT_TYPE_INVITED,
        UserProfileService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    active = WizardSessionService.get_active(username, "onboarding") or {}
    state = dict(active.get("state") or {})
    account_type = state.get("account_type") or UserProfileService.get_account_type(username)

    # Invited users skip step-3 entirely — the invite already scoped them
    # to an org/team, so naming a workspace would create a parallel
    # surface they never asked for. advances them to step-4 so
    # the connector picker still runs (provisioning happens at step-2;
    # a user who deep-links step-3 has already been provisioned).
    if account_type == ACCOUNT_TYPE_INVITED:
        return redirect(url_for("analytics.onboarding_step6_page"))

    # fire ``wizard.step5.entered`` only for users who actually
    # see the workspace-naming screen. Invited users get redirected
    # without rendering, so emitting here would over-count the funnel.
    WizardAnalyticsService.emit(
        step="step-5",
        action=ACTION_ENTERED,
        context=WizardAnalyticsService.build_context(
            username=username,
            wizard_session=active or None,
            account_type=account_type,
        ),
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
    )

    defaults = OnboardingBranchingService.for_account_type(account_type)

    email_local = (username.split("@", 1)[0] if "@" in (username or "") else username) or ""
    # the input now collects the ORG name (not the workspace
    # title). The template variable name stays ``workspace_title`` for
    # backward-compat with form field names; the value is semantically
    # the org-name default. Pre-fill priority:
    #   1. ``state.org_title`` — the canonical org-name key written by
    # earlier POST handlers.
    #   2. Branching service default from email_local.
    #
    # follow-up (2026-05-21): the prior fallback to
    # ``state.workspace_title`` was removed. That key holds the
    # SYSTEM-derived "{org}-workspace" string (with slug-walker
    # suffixes like "-2", "-6" applied when collisions happen). When
    # state.org_title was empty for any reason, the field would
    # prefill with "joshua-mann2-workspace-6" and the user would
    # submit that as if it were their org name, leaking the slug
    # walker into organization_title on the doc. The fallback was
    # marked "best-effort, cosmetic, self-corrects on next POST" —
    # but a teammate hit a case where the cosmetic miss compounded
    # through the rename guard's banner. Strict path now: org_title
    # OR the email_local default. earlier sessions whose state
    # only carries workspace_title fall back to the email_local
    # default (one screen of typing for the user; far better than
    # surfacing the system-derived form as if it were their input).
    workspace_title = state.get("org_title") or defaults.step3_workspace_title(email_local=email_local)
    agency_client_title = state.get("agency_client_title") or ""

    # team-name field. Shown for individual + org_owner only.
    # Pre-fill priority: the user's prior typed value (wizard resume /
    # back-button) → the per-entry-point default that reproduces today's
    # default (org name for individual, "Team 1" for org_owner).
    show_team_name_field = account_type in _STEP5_TEAM_NAME_ENTRY_POINTS
    team_title = state.get("team_title") or _step5_team_name_default(account_type, org_title_default=workspace_title)

    # render persisted More-options values when a prior POST
    # stamped them (refresh resumes the typed value). When no prior
    # value exists, render empty strings for tz/currency so the
    # browser-side Intl prefill can populate them; fiscal-year-start
    # falls back to January (1) so the <select> has a sensible default.
    workspace_timezone = state.get("workspace_timezone", "")
    workspace_currency = state.get("workspace_currency", "")
    workspace_fiscal_year_start_month = state.get("workspace_fiscal_year_start_month", 1)

    csrf_token = auth_module.refresh_csrf_token()
    return render_template(
        "onboarding_step5_workspace.html",
        csrf_token=csrf_token,
        username=username,
        step3_title=defaults.step3_title,
        step3_subtitle=defaults.step3_subtitle,
        step3_label=defaults.step3_label,
        workspace_title=workspace_title,
        agency_client_title=agency_client_title,
        show_agency_client_field=defaults.step3_show_agency_client_field,
        team_title=team_title,
        show_team_name_field=show_team_name_field,
        workspace_timezone=workspace_timezone,
        workspace_currency=workspace_currency,
        workspace_fiscal_year_start_month=workspace_fiscal_year_start_month,
        error=None,
        rename_confirm_prior=None,
        progress=_wizard_progress_for("step-5", username),
        **_step5_workspace_option_lists(),
    )


def _apply_grant_sponsorship(state: dict, username: str) -> None:
    """a sponsored personal signup link comps the workspace its owner
    just created at step-5 (and every org under it), once. Fail-soft like the
    provision it follows: a comp failure is logged and audited for a superuser
    to redo from the billing control, and the wizard continues. Mutates
    ``state`` (the caller saves it)."""
    _grant = state.get("signup_grant") or {}
    if not (_grant.get("sponsored") and state.get("workspace_esid") and not state.get("signup_grant_sponsorship_applied")):
        return
    try:
        from asset_management.web.services.signup_grant_service import SignupGrantService

        _sponsor = SignupGrantService.apply_sponsorship(
            state["workspace_esid"], actor=_grant.get("created_by") or "signup_grant"
        )
    except Exception as e:  # noqa: BLE001 — see docstring
        logging.warning("sponsorship raised for %r: %s", username, e)
        _sponsor = {"ok": False, "orgs_ok": [], "orgs_failed": []}
    if _sponsor.get("ok"):
        state["signup_grant_sponsorship_applied"] = True
    AuditLogService.log_event(
        username,
        "signup_grant_sponsorship",
        bool(_sponsor.get("ok")),
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"jti={_grant.get('jti', '')} workspace={state['workspace_esid']} "
            f"orgs_ok={len(_sponsor.get('orgs_ok') or [])} orgs_failed={','.join(_sponsor.get('orgs_failed') or []) or '-'}"
        ),
    )


@analytics_bp.route("/onboarding/step-5", methods=["POST"])
def onboarding_step5_post():
    """Persist the workspace name(s) and advance to step-4.

    Writes to ``wizard_session.state``:

    * ``workspace_title`` — required, trimmed, length-capped.
    * ``agency_client_title`` — only when the entry point is agency
      (the form omits the field on other branches; the POST treats
      anything else as an ignored value).

    Agency branch: calls
    ``OnboardingService.provision_agency`` to create the multi-workspace
    org + first client workspace + Owner role. ``agency_client_title``
    is required on this branch (the empty fallback that allowed
    is rejected so the acceptance "first client workspace exists at end
    of step 3" actually holds). The provision call is idempotent on
    title (a refresh after a flaky commit re-finds the same org/team
    rather than creating a duplicate).

    Non-agency branches: the actual ES writes
    (org / team / memberships) happen at the wizard's commit point
    downstream so that abandoning the wizard at step-4 doesn't leave
    orphan orgs. ``current_step`` advances to ``step-4`` on all
    non-invited branches.

    Per the scope cut, the "More options" disclosure inputs
    (tz / currency / fiscal-year) are persisted to wizard_session.state
     but are not threaded into the agency provision call —
    that mapping happens at the wizard's commit step when the asset doc
    is finalised.
    """
    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return redirect(url_for("analytics.onboarding_step5_page"))

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    # follow-up — block re-entry POSTs after the wizard is done.
    _done_redirect = _redirect_if_wizard_done(username)
    if _done_redirect is not None:
        return _done_redirect

    from asset_management.web.services.onboarding_branching_service import (
        OnboardingBranchingService,
    )
    from asset_management.web.services.user_profile_service import (
        ACCOUNT_TYPE_AGENCY,
        ACCOUNT_TYPE_INDIVIDUAL,
        ACCOUNT_TYPE_INVITED,
        ACCOUNT_TYPE_ORG_OWNER,
        UserProfileService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    active = WizardSessionService.get_active(username, "onboarding") or {}
    state = dict(active.get("state") or {})
    account_type = state.get("account_type") or UserProfileService.get_account_type(username)

    if account_type == ACCOUNT_TYPE_INVITED:
        # Defence-in-depth: the GET sends invited users to step-4, but a
        # forged POST shouldn't be able to stuff a workspace title onto
        # the wizard_session for an invited user. forwards to
        # step-4 (the next live screen on the invited variant) rather
        # than dropping them on /dashboard pre-connector.
        return redirect(url_for("analytics.onboarding_step6_page"))

    defaults = OnboardingBranchingService.for_account_type(account_type)

    # the visible step-3 input is now the ORG name (not the
    # workspace title). The form field name stays "workspace_title" for
    # wizard_session backward-compat, but the value is semantically the
    # user's org/agency name. The actual workspace title is derived
    # below via OnboardingDefaults.derive_workspace_title.
    org_title_input = (request.form.get("workspace_title") or "").strip()
    # follow-up (2026-05-21) — defensive strip: if the form
    # input arrives with a trailing "-workspace" or "-workspace-N"
    # suffix, that means the GET prefill leaked the SYSTEM-derived
    # workspace title (with optional slug-walker suffix) into the
    # field. Strip back to the user's likely intended org name so a
    # polluted state from a pre-fix session doesn't compound into the
    # ES doc on rename. The "-workspace" literal is the canonical
    # WORKSPACE_TITLE_SUFFIX (onboarding_branching_service.py); a
    # trailing "-N" slug-walker suffix comes from
    # WorkspaceSlugService.resolve_unique_slug. Both get peeled here.
    import re as _re_2

    _ws_suffix_re = _re_2.compile(r"-workspace(?:-\d+)?$", _re_2.IGNORECASE)
    _cleaned = _ws_suffix_re.sub("", org_title_input)
    if _cleaned != org_title_input:
        logging.warning(
            "step-5 stripped workspace-suffix from org_title_input "
            "for %r: %r -> %r (likely leaked from a pre-fix state.workspace_title prefill)",
            username,
            org_title_input,
            _cleaned,
        )
        org_title_input = _cleaned
    agency_client_title = (request.form.get("agency_client_title") or "").strip()
    # raw user-typed team name (individual + org_owner only;
    # the form omits the field on agency/invited branches so this is an
    # empty string there). Length-capped below alongside the org name;
    # the effective title (with the per-entry-point default applied) is
    # computed after the account_type / org-name defaults are known.
    team_title_input = (request.form.get("team_title") or "").strip()

    # More-options raw form values (sanitised below before
    # persistence). Captured up here so a re-render after a title-validation
    # failure preserves the user's typed timezone/currency/fiscal-year
    # rather than blowing them away.
    raw_timezone = request.form.get("timezone", "")
    raw_currency = request.form.get("currency", "")
    raw_fiscal_year_start_month = request.form.get("fiscal_year_start_month", "")

    # every error branch in step-3 POST fires the canonical
    # ``wizard.step5.errored`` event with a short error_code slice the
    # marketing funnel can group on. Built once at the top so each
    # ``_rerender`` call only has to name the code.
    _wa_ctx = WizardAnalyticsService.build_context(
        username=username,
        wizard_session=active or None,
        account_type=account_type,
    )

    def _emit_errored(error_code: str) -> None:
        WizardAnalyticsService.emit(
            step="step-5",
            action=ACTION_ERRORED,
            context=_wa_ctx,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"error_code": error_code},
        )

    # derive the workspace title from the org-name input.
    # The user never sees this string (Layer-1 scope hides workspaces
    # from non-superusers and admin User Management is the only
    # surface that renders it). Empty input is rejected below; an
    # empty derive result here would short-circuit to "" which the
    # downstream provision_* methods would treat as None (fallback to
    # legacy default).
    workspace_title = defaults.derive_workspace_title(org_title_input)

    # whether the team-name field renders (individual +
    # org_owner). Computed once so every re-render path preserves it.
    show_team_name_field = account_type in _STEP5_TEAM_NAME_ENTRY_POINTS

    def _team_title_for_render() -> str:
        """Team value to re-show on an error re-render.

        Preserve the user's typed team name verbatim so a title-validation
        failure doesn't wipe their team-name edit; fall back to the
        per-entry-point default when they hadn't typed anything yet.
        """
        return team_title_input or _step5_team_name_default(account_type, org_title_default=org_title_input or workspace_title)

    def _rerender(error_msg: str, status: int = 400):
        # Recompute the render context from the canonical sources rather
        # than the form post — keeps the error path honest and prevents a
        # bad form value from changing the title/subtitle copy.
        # the template renders the user-typed org name in the
        # input field, NOT the derived workspace title. Re-show the
        # typed value verbatim on error so the user can fix it without
        # losing their work. Falls back to the email-local default when
        # the user submitted an empty field.
        email_local = (username.split("@", 1)[0] if "@" in username else username) or ""
        fallback_workspace = org_title_input or defaults.step3_workspace_title(
            email_local=email_local,
        )
        # Preserve the typed More-options values verbatim on re-render;
        # the user shouldn't lose their tz/currency edit when fixing an
        # unrelated title validation error.
        try:
            month_int = int(raw_fiscal_year_start_month) if raw_fiscal_year_start_month else 1
        except ValueError:
            month_int = 1
        return (
            render_template(
                "onboarding_step5_workspace.html",
                csrf_token=auth_module.refresh_csrf_token(),
                username=username,
                step3_title=defaults.step3_title,
                step3_subtitle=defaults.step3_subtitle,
                step3_label=defaults.step3_label,
                workspace_title=fallback_workspace,
                agency_client_title=agency_client_title,
                show_agency_client_field=defaults.step3_show_agency_client_field,
                team_title=_team_title_for_render(),
                show_team_name_field=show_team_name_field,
                workspace_timezone=raw_timezone,
                workspace_currency=raw_currency,
                workspace_fiscal_year_start_month=month_int,
                error=error_msg,
                rename_confirm_prior=None,
                progress=_wizard_progress_for("step-5", username),
                **_step5_workspace_option_lists(),
            ),
            status,
        )

    def _rerender_rename_confirm(
        *,
        prior_org_title: str,
        typed_title: str,
        agency_client_title: str,
        raw_timezone: str,
        raw_currency: str,
        raw_fiscal_year_start_month: str,
        defaults,
        username: str,
    ):
        # Issue A — re-render with the rename-confirm banner.
        # The input keeps the user's typed-new value (so a 2nd submit
        # with the same value carries the confirm_rename flag), but the
        # banner copy quotes the prior value so the user can decide to
        # revert. ``rename_confirm_prior`` is the template flag that
        # swaps the inline message above the input AND injects the
        # hidden ``confirm_rename=1`` form field on next submit.
        try:
            month_int = int(raw_fiscal_year_start_month) if raw_fiscal_year_start_month else 1
        except ValueError:
            month_int = 1
        return (
            render_template(
                "onboarding_step5_workspace.html",
                csrf_token=auth_module.refresh_csrf_token(),
                username=username,
                step3_title=defaults.step3_title,
                step3_subtitle=defaults.step3_subtitle,
                step3_label=defaults.step3_label,
                workspace_title=typed_title,
                agency_client_title=agency_client_title,
                show_agency_client_field=defaults.step3_show_agency_client_field,
                team_title=_team_title_for_render(),
                show_team_name_field=show_team_name_field,
                workspace_timezone=raw_timezone,
                workspace_currency=raw_currency,
                workspace_fiscal_year_start_month=month_int,
                error=None,
                rename_confirm_prior=prior_org_title,
                progress=_wizard_progress_for("step-5", username),
                **_step5_workspace_option_lists(),
            ),
            409,
        )

    # validate the user-typed org-name input. The derived
    # workspace_title carries a fixed "-workspace" suffix, so checking
    # the derived length would be off-by-10 against the visible input.
    # Error codes keep their earlier names so analytics dashboards
    # tracking funnel error rates don't break.
    if not org_title_input:
        _emit_errored("workspace_title_empty")
        return _rerender(f"{defaults.step3_label} can't be empty.")
    if len(org_title_input) > 120:
        _emit_errored("workspace_title_too_long")
        return _rerender(f"{defaults.step3_label} must be 120 characters or fewer.")

    # validate the team-name input (individual + org_owner).
    # The field is REQUIRED (a teammate 2026-06-22): a blank or whitespace-only
    # team name is rejected with an inline error, same contract as the
    # org-name field above. ``team_title_input`` is already ``.strip()``-ed,
    # so a spaces-only value arrives here as "" and trips the empty branch.
    # This deviates from the original AC#4 (which specified a
    # default-fallback); the field still PREFILLS with the per-entry-point
    # default ("Team 1" / org name) so the common case needs no typing,
    # but a user who clears it must type a real name to continue.
    if show_team_name_field:
        if not team_title_input:
            _emit_errored("team_title_empty")
            return _rerender("Team name can't be empty.")
        if len(team_title_input) > 120:
            _emit_errored("team_title_too_long")
            return _rerender("Team name must be 120 characters or fewer.")

    # The effective team title threaded into the provision / rename calls.
    # Now that the field is required, this is just the validated user input
    # for the team-field entry points; it stays "" for agency/invited
    # (which never render the field and never pass it downstream).
    effective_team_title = team_title_input if show_team_name_field else ""

    # Issue A — rename guard. The user already provisioned a
    # workspace on a prior step-3 POST (state.workspace_esid stamped at
    # line 4075-4077 below). Returning to step-3 via Back from step-4/5
    # and submitting a DIFFERENT org name would otherwise hit
    # provision_individual a second time with a different title, which
    # creates a SECOND workspace doc (different slug → different doc id)
    # and abandons the first. The natural-key idempotency only protects
    # SAME-name re-submits.
    #
    # Behaviour:
    #   1st submit with new name (no confirm token) → re-render with a
    #     non-error banner telling the user their original workspace
    #     already exists and asking them to confirm the rename.
    #   2nd submit with same new name + confirm_rename=1 → call
    #     OnboardingService.rename_provisioned_workspace, which updates
    #     workspace_title + organization_title (+ team_title for the
    #     individual variant) in place. The slug is intentionally
    #     stable — it's the URL identifier and renaming the doc id
    #     would require delete+recreate plus reindexing every cross-
    #     reference (Slack/Linear pattern: ``acme`` stays ``acme`` even
    #     when you rename the workspace to "Acme Inc 2026"). The provision
    #     branch is skipped (the docs already exist; renaming + provisioning
    #     would be redundant).
    prior_workspace_esid = (state.get("workspace_esid") or "").strip()
    prior_org_esid = (state.get("org_esid") or "").strip()
    prior_team_esid = (state.get("team_esid") or "").strip()
    prior_org_title = (state.get("org_title") or "").strip()
    # the team title last persisted to wizard_session, used by
    # the rename guard below to detect a team-name-only edit (org name
    # unchanged but the team renamed via Back). May be hydrated from ES
    # in the block when state was cleared between POSTs.
    prior_team_title = (state.get("team_title") or "").strip()

    # Defect C — when the wizard_session state was cleared between
    # step-5 POSTs (manual reset during testing; explicit cascade-purge;
    # an importer restart that lost in-memory caches), the four prior_*
    # fields above are empty even though the user has a workspace +
    # org + team in ES from a previous run. The rename guard
    # below relies on prior_workspace_esid + prior_org_title to detect a
    # rename intent — when both are empty, the guard silently passes and
    # the provision branch creates a SECOND workspace. test-account on
    # dev-reporting has 5 workspaces from one user's history; this is the
    # path that produced them.
    #
    # Fix: when wizard_session state is missing, check ES for an existing
    # primary workspace membership; if one exists, hydrate the four prior_*
    # fields from ES so the rename-guard below can do its job.
    if not prior_workspace_esid:
        try:
            from asset_management.web.services.workspace_membership_service import (
                WorkspaceMembershipService,
            )
        except ImportError:
            from web.services.workspace_membership_service import (  # type: ignore[no-redef]
                WorkspaceMembershipService,
            )
        es_existing = WorkspaceMembershipService.get_primary_workspace(username)
        if es_existing:
            try:
                from lib.es_helpers import get_elasticsearch_client as _get_es_6
            except ImportError:
                _get_es_6 = None  # type: ignore[assignment]
            try:
                from asset_management.web.services.permissions_index_service import (
                    WORKSPACE_INDEX as _KAN749_WS_INDEX,
                )
                from asset_management.web.services.onboarding_service import (
                    ORG_INDEX as _KAN749_ORG_INDEX,
                    TEAM_INDEX as _KAN749_TEAM_INDEX,
                )
            except ImportError:
                _KAN749_WS_INDEX = "de_asset_workspace"  # type: ignore[assignment]
                _KAN749_ORG_INDEX = "de_asset_organization"  # type: ignore[assignment]
                _KAN749_TEAM_INDEX = "de_asset_teams"  # type: ignore[assignment]
            _es_client = _get_es_6() if _get_es_6 else None
            if _es_client is not None:
                try:
                    _ws_doc = _es_client.get(index=_KAN749_WS_INDEX, id=es_existing)
                    _ws_src = _ws_doc.get("_source", {}) or {}
                    prior_workspace_esid = es_existing
                    state["workspace_esid"] = prior_workspace_esid
                    _org_resp = _es_client.search(
                        index=_KAN749_ORG_INDEX,
                        body={
                            "query": {"term": {"workspace_esid": prior_workspace_esid}},
                            "size": 1,
                        },
                    )
                    _org_hits = (_org_resp.get("hits") or {}).get("hits") or []
                    if _org_hits:
                        prior_org_esid = _org_hits[0]["_id"]
                        prior_org_title = (_org_hits[0]["_source"].get("organization_title") or "").strip()
                        state["org_esid"] = prior_org_esid
                        state["org_title"] = prior_org_title
                        _team_resp = _es_client.search(
                            index=_KAN749_TEAM_INDEX,
                            body={
                                # bare organization_esid (explicit keyword post-reindex).
                                "query": {"term": {"organization_esid": prior_org_esid}},
                                "size": 1,
                            },
                        )
                        _team_hits = (_team_resp.get("hits") or {}).get("hits") or []
                        if _team_hits:
                            prior_team_esid = _team_hits[0]["_id"]
                            state["team_esid"] = prior_team_esid
                            # hydrate the prior team title too so the
                            # rename guard can detect a team-name-only edit.
                            prior_team_title = (_team_hits[0]["_source"].get("team_title") or "").strip()
                            state["team_title"] = prior_team_title
                    logging.info(
                        "step-5 hydrated prior state from ES for %r: "
                        "workspace=%s org=%s team=%s prior_org_title=%r",
                        username,
                        prior_workspace_esid,
                        prior_org_esid,
                        prior_team_esid,
                        prior_org_title,
                    )
                except Exception as e:  # noqa: BLE001 — fail-soft; the rename guard simply won't fire
                    logging.warning(
                        "step-5 hydrate from ES failed for %r: %s",
                        username,
                        e,
                    )

    confirm_rename = request.form.get("confirm_rename") == "1"
    rename_performed = False
    # the team name is also editable now (individual +
    # org_owner). A back-button edit that changes ONLY the team name
    # (org name unchanged) must still rename the team in place rather
    # than fall through to the provision branch, where _upsert_team's
    # title-keyed lookup would miss the renamed team and create a
    # duplicate. So the rename guard fires on an org-name change OR a
    # team-name change. The org-name-change arm is the original
    # path; the team-only arm is new. ``prior_team_title`` is only
    # populated for the team-field entry points (it's hydrated from the
    # team doc / state), so this can't fire on agency/invited.
    org_title_changed = bool(prior_org_title) and org_title_input != prior_org_title
    team_title_changed = (
        show_team_name_field
        and bool(prior_team_title)
        and bool(effective_team_title)
        and effective_team_title != prior_team_title
    )
    if prior_workspace_esid and (org_title_changed or team_title_changed):
        # Issue 8 — the "Submitting again will rename it
        # everywhere it appears" warning is calibrated for a POST-
        # onboarding rename, where the workspace title shows up in
        # filter dropdowns, user management, and team labels that
        # other users see. While the user is still mid-wizard there
        # IS no "everywhere" yet — no other users, no dashboards
        # reading the title, no team labels rendered. A back-button
        # + edit during onboarding is just "I changed my mind",
        # not a destructive rename.
        #
        # Issue 11 (2026-05-25) — pre-fix this branch read
        # session['wizard_pending'], but that flag can be inadvertently
        # cleared (e.g. a stray /login navigation runs the already-auth
        # gate and pops the flag). A developer observed the banner still
        # firing on a mid-wizard test session whose ES
        # ``status`` was still ``active``. Switch the check to ES
        # status directly so a session-flag drift can't surface the
        # banner over a still-incomplete wizard. Fail-soft: an ES
        # outage falls back to the legacy session-flag check so a
        # transient blip doesn't reverse the suppression.
        wizard_still_pending = bool(session.get("wizard_pending"))
        try:
            from asset_management.web.services.wizard_session_service import (
                WizardSessionService as _WSS_RG,
                STATUS_ACTIVE as _STATUS_ACTIVE_RG,
            )

            _es_status = _WSS_RG.get_status(username, "onboarding")
            if _es_status == _STATUS_ACTIVE_RG:
                wizard_still_pending = True
        except Exception as e:  # noqa: BLE001 — fail-soft
            logging.warning(
                "Issue 11 rename-guard ES status lookup failed for %r: %s",
                username,
                e,
            )
        if not confirm_rename and not wizard_still_pending:
            _emit_errored("workspace_rename_unconfirmed")
            return _rerender_rename_confirm(
                prior_org_title=prior_org_title,
                typed_title=org_title_input,
                agency_client_title=agency_client_title,
                raw_timezone=raw_timezone,
                raw_currency=raw_currency,
                raw_fiscal_year_start_month=raw_fiscal_year_start_month,
                defaults=defaults,
                username=username,
            )
        # Confirmed rename. Call the rename helper with the typed-new
        # titles; the slug stays the original (it's the doc id, immutable).
        # ``rename_team`` is now True for BOTH individual and
        # org_owner (both expose an editable team name in step 5, so a
        # back-button edit must propagate to the team doc). Agency still
        # skips the team rename (its team belongs to a separate client
        # org and isn't named in step 5).
        try:
            from asset_management.web.services.onboarding_service import (
                OnboardingService as _RenameOnboardingService,
            )
        except ImportError:
            from web.services.onboarding_service import (  # type: ignore[no-redef]
                OnboardingService as _RenameOnboardingService,
            )
        rename_team_flag = account_type in (ACCOUNT_TYPE_INDIVIDUAL, ACCOUNT_TYPE_ORG_OWNER)

        # (AC#6) — slug-collision pre-check on the rename path.
        # The org already exists (prior_org_esid) with a team; renaming
        # the team to a title whose sanitized alias slug collides with
        # another team in this org must surface a user-visible error and
        # keep the user on step 5, NOT silently fail-soft into a missing
        # team. ``exclude_team_esid`` lets a no-op same-slug re-submit of
        # the team's own row pass. Fresh-provision needs no such check:
        # a brand-new org has no sibling team to collide with.
        if rename_team_flag and prior_team_esid and team_title_changed:
            try:
                from lib.es_helpers import (
                    check_team_slug_unique as _check_slug,
                    TeamSlugCollisionError as _SlugCollision,
                    get_elasticsearch_client as _get_es_2,
                )
            except ImportError:
                _check_slug = None  # type: ignore[assignment]
                _SlugCollision = Exception  # type: ignore[assignment,misc]
                _get_es_2 = None  # type: ignore[assignment]
            if _check_slug is not None and _get_es_2 is not None:
                _es_2 = _get_es_2()
                if _es_2 is not None:
                    try:
                        _check_slug(
                            _es_2,
                            team_title=effective_team_title,
                            org_esids=[prior_org_esid],
                            exclude_team_esid=prior_team_esid,
                        )
                    except _SlugCollision:
                        _emit_errored("team_title_slug_collision")
                        return _rerender(
                            "That team name conflicts with an existing team in " "your organization — pick another."
                        )

        rename_result = _RenameOnboardingService.rename_provisioned_workspace(
            username,
            workspace_esid=prior_workspace_esid,
            org_esid=prior_org_esid,
            team_esid=prior_team_esid or None,
            new_workspace_title=workspace_title,
            new_org_title=org_title_input,
            rename_team=rename_team_flag,
            new_team_title=effective_team_title or None,
        )
        if not rename_result.success:
            # ES update failure on rename. Log + fall back to the
            # keep-original behaviour so the user isn't blocked: their
            # state is still consistent (the docs are unchanged), they
            # advance to step-4 with the prior name. A follow-up ticket
            # can decide whether to surface a louder error.
            logging.warning(
                "rename failed for %r workspace=%s org=%s: %s — falling back to keep-original",
                username,
                prior_workspace_esid,
                prior_org_esid,
                rename_result.errors,
            )
            org_title_input = prior_org_title
            workspace_title = defaults.derive_workspace_title(org_title_input)
        else:
            rename_performed = True
            logging.info(
                "step-3 rename succeeded for %r: prior=%r new=%r (rename_team=%s)",
                username,
                prior_org_title,
                org_title_input,
                rename_team_flag,
            )

    # resolve a unique slug for the workspace title and stash it
    # alongside the title on wizard_session. ``resolve_unique_slug`` walks
    # ``slug``, ``slug-2``, ``slug-3`` … until it finds a free value, so
    # this branch never raises on a duplicate — the user just sees their
    # workspace land on the next-free variant. The wizard's commit step
    # (downstream ticket) re-runs the check before the actual ES write.
    try:
        from asset_management.web.services.workspace_slug_service import (
            WorkspaceSlugService,
        )
    except ImportError:
        from web.services.workspace_slug_service import WorkspaceSlugService  # type: ignore[no-redef]

    # follow-up: on a confirmed rename, the slug stays the
    # original (workspace_esid IS the slug; renaming the title doesn't
    # change the doc id). Re-resolving from the new title would either
    # return the same slug (if free) or worse, a brand-new slug that
    # doesn't match the actual doc — pointing the wizard session at a
    # workspace that doesn't exist. Reuse the prior slug to keep state
    # aligned with what's on disk.
    if rename_performed:
        workspace_slug = state.get("workspace_slug") or prior_workspace_esid
    else:
        workspace_slug = WorkspaceSlugService.resolve_unique_slug(
            workspace_title,
            exclude_username=username,
        )

    # store BOTH titles on the wizard session:
    # * workspace_title — the system-derived "{org}-workspace" string,
    #   consumed by WorkspaceSlugService._slug_in_sessions which
    #   slugifies it to detect cross-tab slug races. Keeping this stored
    #   under the existing key preserves that collision-detection
    #   contract without touching the slug service.
    # * org_title — the user-typed org name; what the GET re-fills the
    #   input with on wizard resume.
    state["workspace_title"] = workspace_title
    state["org_title"] = org_title_input
    state["workspace_slug"] = workspace_slug
    # persist the effective team title so a wizard resume /
    # back-button re-fills the team-name field and the rename guard above
    # can detect a subsequent team-name-only edit. Only for the team-field
    # entry points; agency/invited never write this.
    if show_team_name_field and effective_team_title:
        state["team_title"] = effective_team_title
    if defaults.step3_show_agency_client_field:
        # agency_client_title is REQUIRED on the agency branch.
        # The contract allowed it to be optional ("add the first
        # client post-wizard"), but acceptance is "First client
        # workspace exists at end of step 3" — so the wizard must collect
        # the name before the POST can advance.
        if not agency_client_title:
            _emit_errored("agency_client_title_empty")
            return _rerender("Name your first client to continue.")
        if len(agency_client_title) > 120:
            _emit_errored("agency_client_title_too_long")
            return _rerender("Client name must be 120 characters or fewer.")
        state["agency_client_title"] = agency_client_title

    # persist the More-options triple onto wizard_session.state.
    # Sanitisation is lenient — an unknown value falls back to defaults
    # rather than blocking the wizard. The downstream commit step reads
    # these to populate the asset doc.
    try:
        from asset_management.web.services.workspace_settings_service import (
            STATE_KEY_CURRENCY,
            STATE_KEY_FISCAL_YEAR_START,
            STATE_KEY_TIMEZONE,
            WorkspaceSettingsService,
        )
    except ImportError:
        from web.services.workspace_settings_service import (  # type: ignore[no-redef]
            STATE_KEY_CURRENCY,
            STATE_KEY_FISCAL_YEAR_START,
            STATE_KEY_TIMEZONE,
            WorkspaceSettingsService,
        )
    workspace_settings = WorkspaceSettingsService.sanitize(
        timezone=raw_timezone,
        currency=raw_currency,
        fiscal_year_start_month=raw_fiscal_year_start_month,
    )
    state[STATE_KEY_TIMEZONE] = workspace_settings.timezone
    state[STATE_KEY_CURRENCY] = workspace_settings.currency
    state[STATE_KEY_FISCAL_YEAR_START] = workspace_settings.fiscal_year_start_month

    # (agency) + (individual / org_owner): provision the
    # workspace + org + team + workspace_owner role on step-3 POST. All
    # three calls are idempotent on the natural keys (workspace slug /
    # org title / team title / username:scope_esid:role_esid), so a
    # wizard re-render after a transient ES blip doesn't double-create.
    #
    # The invited variant is handled by the early-return above
    # (provision_invited fires at signup time from a different code
    # path, not step-3); a brand-new account_type / future variant
    # falls through to ``no_provision`` so the wizard doesn't crash on
    # an unknown value.
    provision_detail = "no_provision"
    provision_kind: str | None = None
    # when a confirmed rename arrives the rename branch above
    # has already updated the workspace + org (+ team) doc titles in
    # place. Skip the provision branch entirely: the docs already exist
    # at the prior slug, and provision_* would either re-find them
    # (no-op) or, worse, slug-resolve the renamed title to a new slug
    # (under the earlier derive_workspace_title) and create a
    # second workspace. Bypassing the branch is the safer invariant.
    if rename_performed:
        provision_detail = "rename_performed"
    elif account_type in (ACCOUNT_TYPE_AGENCY, ACCOUNT_TYPE_INDIVIDUAL, ACCOUNT_TYPE_ORG_OWNER):
        try:
            from asset_management.web.services.onboarding_service import (
                OnboardingService,
            )
        except ImportError:
            from web.services.onboarding_service import OnboardingService  # type: ignore[no-redef]
        # acquire a short per-user cluster-wide lock around the
        # provision_* call. Two rapid clicks on the step-5 Continue
        # button would otherwise race two OnboardingService calls past
        # each other: the slug walker in WorkspaceSlugService runs
        # ``_slug_in_workspaces(exclude_username=username)`` which only
        # treats an existing workspace as "yours" when the membership
        # doc is already written. The first request's _upsert_workspace
        # writes the workspace doc under refresh="wait_for", but the
        # _upsert_workspace_membership row that follows is on a separate
        # ES round-trip — if the second request's slug walker runs in
        # that window it sees "slug taken, not yours" and bumps to
        # ``foo-workspace-2``, creating the orphan second workspace
        # reported on prod 2026-05-25.
        #
        # The lock collapses that window: the second request waits for
        # acquire to fail (lock held), then re-reads wizard_session
        # state — by then the first request has saved workspace_esid +
        # org_esid + team_esid and we surface those without re-running
        # provision_*. This mirrors the step-8 Popen guard
        # (ImportProgressService.find_active_run_id) — same shape, same
        # ImportLockService primitive, different scope.
        #
        # The "connector" name uses a ``wizard_step5_provision:`` prefix
        # so the lock doc is visually distinguishable from real importer
        # locks when an operator greps ``de_import_locks``. The 30-min
        # TTL inherited from ImportLockService is wider than necessary
        # but the release path below clears the lock on every exit
        # branch, so a crashed worker is the only way a stale lock
        # survives — and the next double-click within 30min hits the
        # short-circuit branch (which is correct: the prior request
        # presumably DID write the workspace).
        try:
            from lib.import_lock_service import ImportLockService as _Step5Lock
        except ImportError:
            _Step5Lock = None  # type: ignore[assignment]
        _step5_lock_key = f"wizard_step5_provision:{username}"
        _step5_lock_id = _Step5Lock.acquire(_step5_lock_key) if _Step5Lock is not None else None
        # distinguish "lock contended" from "ES outage". Both
        # paths return None from acquire,
        # but the skip-provision branch is only safe when someone
        # POSITIVELY holds the lock. If acquire failed because ES is
        # unreachable, fall through to provision_* — the natural-key
        # idempotency in _upsert_workspace's op_type=create is the
        # load-bearing dedup gate in the ES-outage case.
        _step5_lock_contended = False
        if _Step5Lock is not None and _step5_lock_id is None:
            try:
                _step5_lock_contended = bool(_Step5Lock.is_held(_step5_lock_key))
            except Exception:  # noqa: BLE001 — soft-fail; treat as not-contended
                _step5_lock_contended = False
        if _step5_lock_contended:
            # lock contended (concurrent step-5 POST in flight,
            # almost always a double-click). Re-read wizard_session state
            # to surface the IDs the lock-holder is about to write (or
            # already wrote). The wizard advances to step-6 regardless;
            # blocking the user here would punish the SECOND click for
            # the FIRST click's work.
            try:
                _refreshed = WizardSessionService.get_active(username, "onboarding") or {}
                _refreshed_state = dict(_refreshed.get("state") or {})
                _existing_ws = (_refreshed_state.get("workspace_esid") or "").strip()
                _existing_org = (_refreshed_state.get("org_esid") or "").strip()
                _existing_team = (_refreshed_state.get("team_esid") or "").strip()
            except Exception:  # noqa: BLE001 — fail-soft, fall through to natural-key dedup
                _existing_ws = _existing_org = _existing_team = ""
            if _existing_ws:
                state["workspace_esid"] = _existing_ws
            if _existing_org:
                state["org_esid"] = _existing_org
            if _existing_team:
                state["team_esid"] = _existing_team
            logging.info(
                "step-5 provision lock contended for %r: surfacing "
                "existing workspace_esid=%r org_esid=%r team_esid=%r and "
                "skipping duplicate provision_* call.",
                username,
                _existing_ws,
                _existing_org,
                _existing_team,
            )
            provision_detail = (
                f"lock_contended workspace_esid={_existing_ws or '(none)'} "
                f"org_esid={_existing_org or '(none)'} team_esid={_existing_team or '(none)'}"
            )
        else:
            try:
                if account_type == ACCOUNT_TYPE_AGENCY:
                    provision_kind = "provision_agency"
                    # for agency, the workspace title is the
                    # derived "{agency}-workspace" string. provision_agency's
                    # ``agency_title`` kwarg drives the workspace.title field
                    # (the workspace IS the agency container under F7), so
                    # we pass the derived string here. The first-client org
                    # name remains the user's separate input.
                    provision_result = OnboardingService.provision_agency(
                        username,
                        agency_title=workspace_title,
                        first_client_title=agency_client_title,
                    )
                elif account_type == ACCOUNT_TYPE_INDIVIDUAL:
                    provision_kind = "provision_individual"
                    # workspace_title is the derived "-workspace"
                    # string (invisible to non-superusers); org_title is the
                    # user's typed name (visible in filter dropdowns).
                    provision_result = OnboardingService.provision_individual(
                        username,
                        workspace_title=workspace_title,
                        org_title=org_title_input,
                        team_title=effective_team_title or None,
                    )
                else:
                    provision_kind = "provision_org_owner"
                    provision_result = OnboardingService.provision_org_owner(
                        username,
                        email=session.get("email"),
                        workspace_title=workspace_title,
                        org_title=org_title_input,
                        team_title=effective_team_title or None,
                    )
                if provision_result.success:
                    # follow-up: stamp the resolved ESIDs onto
                    # wizard_session.state so a later step-3 re-entry that
                    # confirms a rename has the workspace/org/team doc ids
                    # to target. Pre-stamp the wizard only logged these in
                    # the audit trail; without them on state the rename
                    # branch couldn't locate the docs to update.
                    if provision_result.workspace_esid:
                        state["workspace_esid"] = provision_result.workspace_esid
                    if provision_result.org_esid:
                        state["org_esid"] = provision_result.org_esid
                    if provision_result.team_esid:
                        state["team_esid"] = provision_result.team_esid
                    provision_detail = (
                        f"{provision_kind}=ok "
                        f"workspace_esid={provision_result.workspace_esid} "
                        f"org_esid={provision_result.org_esid} "
                        f"team_esid={provision_result.team_esid} "
                        f"workspace_created={provision_result.workspace_created} "
                        f"org_created={provision_result.org_created} "
                        f"team_created={provision_result.team_created}"
                    )
                else:
                    # Fail-soft: log the error but still let the wizard advance.
                    # Blocking step-3 on a transient ES blip would strand a
                    # user who just typed valid names. The follow-up
                    # release-plan test asserts the happy path; alerting
                    # on the soft-fail counter is a follow-up ticket.
                    logging.warning(
                        "%s failed for %r: %s",
                        provision_kind,
                        username,
                        provision_result.errors,
                    )
                    provision_detail = f"{provision_kind}=errors={','.join(provision_result.errors)}"
            except Exception as e:  # noqa: BLE001 — same fail-soft policy as wizard_session.save
                logging.warning(
                    "%s raised for %r: %s",
                    provision_kind or "provision_unknown",
                    username,
                    e,
                )
                provision_detail = f"{provision_kind or 'provision_unknown'}=exception={type(e).__name__}"
            finally:
                # always release the per-user step-5 lock, even
                # on the provision-failed branch. Leaving the lock held
                # would block a legitimate retry by the same user for up
                # to 30 minutes (the ImportLockService TTL).
                if _Step5Lock is not None and _step5_lock_id is not None:
                    try:
                        _Step5Lock.release(_step5_lock_key, _step5_lock_id)
                    except Exception:  # noqa: BLE001 — soft-fail; lock expires on its own
                        pass

    # a sponsored personal signup link comps the new workspace once.
    _apply_grant_sponsorship(state, username)

    try:
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-6",
            state=state,
        )
    except Exception as e:  # noqa: BLE001 — never let a session write break the wizard
        logging.warning("wizard_session save failed for %r: %s", username, e)

    AuditLogService.log_event(
        username,
        "onboarding_step5_workspace",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"account_type={account_type} title_len={len(workspace_title)} "
            f"slug={workspace_slug} tz={workspace_settings.timezone} "
            f"ccy={workspace_settings.currency} "
            f"fy_start={workspace_settings.fiscal_year_start_month} "
            f"rename_performed={rename_performed} "
            f"{provision_detail}"
        ),
    )

    # canonical step-3 completion. Re-read the wizard session
    # so cohort + connector reflect the just-saved state (especially the
    # advance to step-4 in current_step), and so a future emit-site
    # invariant test sees the same wizard_session_id across steps.
    WizardAnalyticsService.emit(
        step="step-5",
        action=ACTION_COMPLETED,
        context=WizardAnalyticsService.build_context(
            username=username,
            wizard_session=WizardSessionService.get_active(username, "onboarding"),
            account_type=account_type,
        ),
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
    )

    # step-3 hands off to the connector tile-grid screen.
    return redirect(url_for("analytics.onboarding_step6_page"))


@analytics_bp.route("/onboarding/step-5/slug-check")
def onboarding_step5_slug_check():
    """JSON probe for inline workspace-slug availability.

    The step-3 form fires this on every keystroke (debounced
    client-side) to surface a duplicate-slug warning before the user
    hits Continue. The endpoint is intentionally fail-soft — an ES
    outage returns ``available=True`` so a transient issue never
    blocks the wizard; the wizard's commit step is the load-bearing
    dedup gate.

    Response shape (always 200 on the happy path):

    .. code-block:: json

        {
            "slug": "acme-corp",
            "available": false,
            "reason": "taken",
            "suggestions": ["acme-corp-2", "acme-corp-3", "acme-corp-team"]
        }

    ``reason`` is one of
    ``ok | empty | too_short | reserved | taken | check_failed | unauthenticated``.
    ``suggestions`` is non-empty only when ``available`` is False.
    """

    username = session.get("username", "")
    if not username:
        # Defense-in-depth: verify_auth has already gated us on a real
        # session; a missing ``username`` here means a corrupted session
        # edge case. Return structured JSON so the inline check can
        # render an "your session expired" message instead of trying to
        # parse a 302 to /login.
        return {"slug": "", "available": False, "reason": "unauthenticated", "suggestions": []}, 401

    title_or_slug = (request.args.get("title") or request.args.get("slug") or "").strip()

    try:
        from asset_management.web.services.workspace_slug_service import (
            WorkspaceSlugService,
        )
    except ImportError:
        from web.services.workspace_slug_service import WorkspaceSlugService  # type: ignore[no-redef]

    # the step-3 input is now the org name; the workspace
    # slug is derived from "{org}-workspace". The availability check
    # must run against the DERIVED title so the JS warning matches
    # what the POST handler will actually resolve. We import the
    # branching service here (instead of at module top) to avoid
    # circular imports between routes and the wizard service tree.
    try:
        from asset_management.web.services.onboarding_branching_service import (
            OnboardingDefaults,
        )
    except ImportError:
        from web.services.onboarding_branching_service import OnboardingDefaults  # type: ignore[no-redef]

    title_for_check = OnboardingDefaults.derive_workspace_title(title_or_slug) or title_or_slug

    result = WorkspaceSlugService.check_availability(
        title_for_check,
        exclude_username=username,
        include_suggestions=True,
    )

    return {
        "slug": result.slug,
        "available": result.available,
        "reason": result.reason,
        "suggestions": result.suggestions,
    }, 200


@analytics_bp.route("/onboarding/step-6")
def onboarding_step6_page():
    """Wizard step 4 — connector tile grid with search.

    Renders the Funnel.io-style grid of connector tiles. The default
    selection comes from ``OnboardingBranchingService`` (Meta Ads for
    most flows; None for invited users so no tile is preselected),
    overridden by any prior ``selected_connector_id`` on
    wizard_session.state — refreshing the screen resumes the user's
    pick, not the branching default.

    Invited users see step-4 too — their workspace scope is
    set by the invite but they still pick their own first connector.
    The "skip 4-6 when target team already has a connector" branch
    fires at step-2's consume helper before the user ever sees this
    page.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    # follow-up — block re-entry after the wizard is done.
    # Note: this fires BEFORE the reactivate path below.
    # earlier, a skipped user clicking "Connect a data source" on
    # the dashboard checklist landed here and reactivated their
    # session. That path is replaced by the checklist's retarget to
    # /assets/data-connectors/ (also part of this same
    # follow-up), so the reactivate-from-skipped branch below is now
    # unreachable through normal navigation — kept as defensive code
    # for any in-flight skipped sessions written pre-deploy.
    _done_redirect = _redirect_if_wizard_done(username)
    if _done_redirect is not None:
        return _done_redirect

    from asset_management.web.services.connector_catalog_service import (
        ConnectorCatalogService,
    )
    from asset_management.web.services.onboarding_branching_service import (
        OnboardingBranchingService,
    )
    from asset_management.web.services.user_profile_service import (
        UserProfileService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    active = WizardSessionService.get_active(username, "onboarding") or {}
    state = dict(active.get("state") or {})

    # re-entry from the dashboard checklist. A previously-skipped
    # session is reactivated (status flips back to active) and its prior
    # state blob is rehydrated so the user lands at their last picked tile,
    # not the branching default.
    if not active:
        skipped = WizardSessionService.get_skipped(username, "onboarding") or {}
        if skipped:
            state = dict(skipped.get("state") or {})
            WizardSessionService.reactivate(username, "onboarding")

    account_type = state.get("account_type") or UserProfileService.get_account_type(username)

    # invited users DO see step-4 now. Their org/team is
    # set by the invite, but the conditional steps 4-6 still apply
    # (per the AC: "only shown if the target team has zero connectors
    # yet"). Today's earlier invites carry workspace_esid only,
    # no team_esid, so every invited user falls into the show-4-6
    # branch wires team selection on the sender side.
    # The OnboardingBranchingService gives invited users a no-tile
    # default which the priority logic below respects.

    defaults = OnboardingBranchingService.for_account_type(account_type)
    selectable_ids = ConnectorCatalogService.selectable_ids()

    # Selection priority: prior wizard_session value > branching default.
    # The persisted value is filtered through ``selectable_ids`` so a
    # stale wizard_session that names a now-coming-soon tile falls back
    # to the branching default instead of rendering nothing-selected.
    persisted_selection = state.get("selected_connector_id") or ""
    if persisted_selection in selectable_ids:
        selected_connector_id = persisted_selection
    elif defaults.step4_default_connector in selectable_ids:
        selected_connector_id = defaults.step4_default_connector
    else:
        selected_connector_id = ""

    selected_tile = ConnectorCatalogService.get(selected_connector_id)
    selected_connector_name = selected_tile.name if selected_tile else ""

    # the wizard's single-source goal supersedes
    # free-tier lock on this surface. Every wired tile is selectable
    # regardless of tier; additional sources are added post-onboarding
    # via /assets/data-connectors/. The SourceLimitService
    # helper + _compute_locked_connector_ids stay in the module for
    # a possible future caller on /assets/data-connectors/, but step-4
    # no longer instantiates them.

    csrf_token = auth_module.refresh_csrf_token()

    # fire ``wizard.step6.entered`` once per page render. The
    # selection priority logic above can resolve a non-empty
    # ``selected_connector_id`` from prior wizard_session state, so
    # the canonical event already carries the resumed connector
    # without a separate read.
    WizardAnalyticsService.emit(
        step="step-6",
        action=ACTION_ENTERED,
        context=WizardAnalyticsService.build_context(
            username=username,
            wizard_session=active or None,
            account_type=account_type,
            connector_override=selected_connector_id,
        ),
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
    )

    # the step-6 dispatch helpers redirect back here with
    # ?error=<code> instead of silently bouncing to /dashboard when a
    # hand-off can't proceed (unresolved primary team, missing
    # mc_data_connectors, failed tokenless finalize). Codes resolve
    # through a fixed map — an unknown code renders no banner and the
    # raw param is never echoed into the page.
    step6_error = _STEP6_ERROR_MESSAGES.get((request.args.get("error") or "").strip())

    return render_template(
        "onboarding_step6_connector.html",
        csrf_token=csrf_token,
        username=username,
        popular_tiles=ConnectorCatalogService.popular_tiles(),
        all_tiles=ConnectorCatalogService.all_section_tiles(),
        selected_connector_id=selected_connector_id,
        selected_connector_name=selected_connector_name,
        error=step6_error,
        progress=_wizard_progress_for("step-6", username),
    )


@analytics_bp.route("/onboarding/step-6", methods=["POST"])
def onboarding_step6_post():
    """Persist the selected connector and hand off to step-5.

    Accepts a single ``selected_connector_id`` form field; validates it
    against ``ConnectorCatalogService.selectable_ids()`` so a forged
    POST can't stuff a coming-soon, placeholder, or unknown id onto
    wizard_session.

    A ``skip=1`` form field is the "Skip — connect later" exit link
    from the prototype. On
    skip we stamp ``selected_connector_id=None``, mark the wizard_session
    ``skipped`` (NOT completed — re-entry from the dashboard
    checklist resumes the same doc at step-4), and redirect to
    ``/dashboard`` where the empty-state surfaces a "Connect your first
    source" checklist item.

     (2026-05-26) removed two branches that used to live here:

    * The ``notify_me=1`` branch — the wizard's
      catalog has no remaining ``placeholder=True`` tiles,
      and the "let users register interest in a coming-soon tile"
      affordance contradicts intent that any wired tile be
      selectable. ``coming_soon`` tiles still render visually distinct
      but are non-interactive in the wizard.
    * The ``source_limit`` / ``locked_connector_ids`` refusal
       — the wizard's single-source goal means free-tier vs.
      paid-tier doesn't gate which tile the user picks; additional
      sources are added post-onboarding via /assets/data-connectors/
      . The helpers stay in this module for a possible future
      caller on the data-connectors page.

    Selecting a connector advances ``current_step`` to step-5 (when
    the connector has a scope manifest), tokenless-finalises inline
    (Echo / Google Ads / GA4), or soft-redirects to /dashboard.
    """
    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        return redirect(url_for("analytics.onboarding_step6_page"))

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    # follow-up — block re-entry POSTs after the wizard is done.
    _done_redirect = _redirect_if_wizard_done(username)
    if _done_redirect is not None:
        return _done_redirect

    from asset_management.web.services.connector_catalog_service import (
        ConnectorCatalogService,
    )
    from asset_management.web.services.user_profile_service import (
        UserProfileService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    active = WizardSessionService.get_active(username, "onboarding") or {}
    state = dict(active.get("state") or {})
    account_type = state.get("account_type") or UserProfileService.get_account_type(username)

    # invited users post connector selections too. The
    # bare-redirect that lived here when steps 4-6 weren't part of the
    # invited path is gone; today's invites carry workspace_esid only
    # so the conditional steps 4-6 always fire for invited users.

    # context shared across every step-4 POST branch. Re-built
    # whenever the connector_override changes (skip stamps None, select
    # stamps the new id) but the base context is the same everywhere.
    def _wa_ctx_for(connector: str = "") -> "object":
        return WizardAnalyticsService.build_context(
            username=username,
            wizard_session=active or None,
            account_type=account_type,
            connector_override=connector,
        )

    skip_requested = request.form.get("skip") == "1"

    if skip_requested:
        state["selected_connector_id"] = None
        try:
            WizardSessionService.save(
                user_id=username,
                wizard_type="onboarding",
                current_step="step-6",
                state=state,
            )
            # mark skipped (not completed). The dashboard's
            # "Connect your first source" checklist looks for exactly
            # this terminal status to surface the re-entry CTA.
            WizardSessionService.skip(username, "onboarding")
            # follow-up — Skip is a terminal exit from the
            # wizard, so the session-level wizard_pending flag must
            # clear in lockstep. Without this pop the redirect to
            # /dashboard below loops: verify_auth re-bounces to the
            # resume URL which now points at /onboarding/start (no
            # active wizard_session); /onboarding/start renders the
            # signed-in blocker because the user IS authenticated.
            # The user reports landing on the "Can't sign in to this
            # account" blocker after clicking Skip — this missing pop
            # is the cause.
            session.pop("wizard_pending", None)
            # Issue 2 (2026-05-25) — purge any orphan
            # ``de_connection`` rows the user accumulated from a
            # prior wizard attempt that reached step-7 (OAuth callback
            # writes a ``pending_token`` row) but then back-buttoned
            # and Skipped on step-6. Without this purge, the
            # ``connect_first_source`` checklist predicate's
            # ``_has_any_connection`` finds the orphan and marks the
            # row done — even though the user just explicitly opted
            # out of connecting a source. Limit the purge to
            # ``status=pending_token`` so an active connection from a
            # genuine prior completion is left alone (a user who
            # finished + skipped a re-entry shouldn't lose live data).
            _purge_orphan_pending_connections(username)
        except Exception as e:  # noqa: BLE001 — never let a session write break the wizard
            logging.warning("step-4 skip session-save failed for %r: %s", username, e)
        AuditLogService.log_event(
            username,
            "onboarding_step6_connector",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"account_type={account_type} action=skip",
        )
        # canonical skip event. connector="" because the user
        # explicitly DIDN'T pick one; the funnel needs to distinguish
        # "skipped step 4" from "completed step 4 with no connector".
        WizardAnalyticsService.emit(
            step="step-6",
            action=ACTION_SKIPPED,
            context=_wa_ctx_for(connector=""),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
        )
        return redirect("/dashboard")

    # (2026-05-26) — notify_me POST branch removed.
    # closed the last placeholder=True tile so the affordance had no
    # surface; also rips out the corresponding UI from the
    # template. A POST that still carries notify_me=1 (stale tab, etc.)
    # falls through to the standard selected_connector_id validation
    # below — an unknown / empty id surfaces the inline "Pick a source
    # to continue." error rather than silently no-op'ing.

    selected_connector_id = (request.form.get("selected_connector_id") or "").strip()
    selectable_ids = ConnectorCatalogService.selectable_ids()

    if selected_connector_id not in selectable_ids:
        # Re-render with an inline error rather than 500 — keeps the
        # wizard recoverable for a user whose browser somehow submitted
        # an empty / stale value.
        from asset_management.web.services.onboarding_branching_service import (
            OnboardingBranchingService,
        )

        defaults = OnboardingBranchingService.for_account_type(account_type)
        fallback_id = defaults.step4_default_connector if defaults.step4_default_connector in selectable_ids else ""
        fallback_tile = ConnectorCatalogService.get(fallback_id)
        # unknown / unselectable id is an ``errored`` event,
        # not a silently-dropped POST. The funnel can count UX bugs
        # (empty / stale selections) separately from honest skips.
        WizardAnalyticsService.emit(
            step="step-6",
            action=ACTION_ERRORED,
            context=_wa_ctx_for(connector=selected_connector_id),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"error_code": "unknown_or_unselectable_connector"},
        )
        return (
            render_template(
                "onboarding_step6_connector.html",
                csrf_token=auth_module.refresh_csrf_token(),
                username=username,
                popular_tiles=ConnectorCatalogService.popular_tiles(),
                all_tiles=ConnectorCatalogService.all_section_tiles(),
                selected_connector_id=fallback_id,
                selected_connector_name=fallback_tile.name if fallback_tile else "",
                error="Pick a source to continue.",
                progress=_wizard_progress_for("step-6", username),
            ),
            400,
        )

    # (2026-05-26) — locked-id server-side gate
    # removed. Free-tier vs. paid-tier no longer governs which tile is
    # selectable in the wizard; the single-source rule means
    # every wired tile is reachable here regardless of plan, and the
    # post-onboarding /assets/data-connectors/ surface owns the
    # multi-source story for both tiers. The audit event
    # ``onboarding_step6_source_limit_blocked`` is now unreachable
    # from this surface (kept as a known string for log retention).

    state["selected_connector_id"] = selected_connector_id
    try:
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-7",
            state=state,
        )
    except Exception as e:  # noqa: BLE001 — same fail-soft policy as the earlier steps
        logging.warning("step-4 session-save failed for %r: %s", username, e)

    AuditLogService.log_event(
        username,
        "onboarding_step6_connector",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(f"account_type={account_type} action=select " f"connector_id={selected_connector_id}"),
    )

    # canonical step-4 completion. The connector field is
    # now-populated (the just-saved selection), so this is the FIRST
    # canonical step event that carries a non-empty connector. The
    # marketing funnel pivots on this field to slice by tile.
    WizardAnalyticsService.emit(
        step="step-6",
        action=ACTION_COMPLETED,
        context=_wa_ctx_for(connector=selected_connector_id),
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
    )

    # when the selected connector has a step-5 scope manifest
    # (today: meta_ads), hand off to the pre-redirect permissions
    # review screen.
    from asset_management.web.services.connector_scope_manifest_service import (
        ConnectorScopeManifestService,
    )

    if selected_connector_id in ConnectorScopeManifestService.supported_connector_ids():
        return redirect(url_for("analytics.onboarding_step7_page"))

    # connectors that don't need per-tenant OAuth (today: Echo)
    # skip step-5 entirely. The cluster-managed token is in deployment
    # config, the only "asset" is the user's primary team_esid (the
    # Echo importer reads it off connection.principal anyway), and step-6
    # would render an empty / no-choice screen. Finalise inline: write
    # the tokenless connection + asset, materialise the first-look
    # dashboard, kick off the on-demand backfill, then hand off to the
    # step-7 building screen. This is symmetric with what step-6 finish
    # does for meta_ads — same dispatch, just without the OAuth + asset
    # selection ceremony in between.
    if selected_connector_id in _NO_OAUTH_CONNECTOR_IDS:
        # (2026-05-28) — closes the empty-dashboard outcome for
        # Google Ads + GA4. A brand-new onboarding user's primary team
        # has no ``google_ads_customer_esid`` / ``team_ga4_property_esids``
        # configured yet, so the tokenless finalize below would resolve
        # zero provider assets and write a connection the importer can't
        # back. Detect the gap and hand the user off to the Data
        # Connectors modal's pick-or-create wizard (the same surface
        # v2 built); the modal POSTs to the onboarding-finalize
        # endpoints which configure the team and then run the tokenless
        # finalize for real. Symmetric with YouTube fix.
        if selected_connector_id in _ONBOARDING_NEEDS_TEAM_CONFIG_CONNECTOR_IDS:
            redirect_response = _maybe_redirect_tokenless_to_data_connectors(
                username=username,
                selected_connector_id=selected_connector_id,
            )
            if redirect_response is not None:
                return redirect_response
        return _finalize_tokenless_connector(
            username=username,
            selected_connector_id=selected_connector_id,
            account_type=account_type,
            state=state,
        )

    # (2026-05-28) — closes the YouTube step-4 dead-end.
    # YouTube's real setup flow (OAuth popup + channel multiselect) lives
    # on /data-connectors/. Rather than duplicate ~400 LOC of ytWizard JS
    # into onboarding chrome (Option B/C), redirect the user to the
    # existing modal pre-targeted at their primary team. The modal page's
    # JS reads ``open=youtube&from_onboarding=1`` and auto-opens
    # ytWizard; on submit success it POSTs to the
    # ``onboarding-finalize`` endpoint and follows the step-7 redirect.
    # wizard-signup workspace-tier bridge grants
    # mc_data_connectors mid-wizard for owner/admin signups; account
    # types outside the bridge are caught by the
    # _wizard_can_reach_data_connectors pre-flight inside the helper,
    # which surfaces an explicit step-6 error instead of the
    # destination's 403.
    #
    # first-look pack materialise + on-demand backfill run at
    # the END of the modal flow: the onboarding-finalize endpoint (and the
    # regular /data-connectors/ configure) call the shared tokenless-finalize
    # core with the connection configure_youtube writes. Nothing to
    # kick off here — at this point no channel is linked yet.
    if selected_connector_id == "youtube":
        return _redirect_youtube_to_data_connectors(username=username)

    # Selections without a manifest AND without a tokenless finalize
    # path soft-redirect to /dashboard the same way did while
    # step-5 was unbuilt — the wizard never dead-ends and the source is
    # still saved.
    return redirect("/dashboard")


# ============================================
# ONBOARDING STEP 5 — PERMISSIONS REVIEW
# ============================================
# Pre-redirect OAuth scope summary. The user lands here after picking
# a working connector on step 4 and reads a plain-language list of
# every scope we'll request before clicking Continue. Borrows the
# pattern from Funnel.io / Stripe Connect: surface the ask first,
# redirect second, so a wary user can opt out without ever touching
# the provider's consent screen.
#
# Continue now hands off to the wizard-aware Meta OAuth roundtrip
# (onboarding_step7_start_oauth → callback →
# step-8 asset selection → token persistence). The roundtrip
# lives in this same blueprint, not in fb_oauth_server.py — the
# asset-management OAuth server stores tokens per FB-app-user and is
# intentionally separate from the per-onboarding-user OAuth flow. The
# /dashboard soft-redirect that lived here shipped is
# gone.


@analytics_bp.route("/onboarding/step-7")
def onboarding_step7_page():
    """Wizard step 5 — pre-redirect OAuth scope summary.

    Renders the plain-language permissions board for the connector
    the user picked on step 4. The selection is read back from
    ``wizard_session.state.selected_connector_id`` so a refresh or
    a deep-link is recoverable.

    Guards (each one redirects back to step-4 instead of rendering
    an empty / wrong screen):

    * No active wizard_session → /onboarding/step-6 reactivates a
      skipped one or starts fresh.
    * Active session but no ``selected_connector_id`` → /onboarding/step-6.
    * Connector id is unknown OR has no scope manifest → /onboarding/step-6.

    On a successful render, fires an ``onboarding_step7_entered``
    audit event with ``detail`` containing the prototype's
    ``analytics_event=wizard.step7.entered`` literal so a marketing
    funnel slice can find the step.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    # follow-up — block re-entry after the wizard is done.
    _done_redirect = _redirect_if_wizard_done(username)
    if _done_redirect is not None:
        return _done_redirect

    from asset_management.web.services.connector_catalog_service import (
        ConnectorCatalogService,
    )
    from asset_management.web.services.connector_scope_manifest_service import (
        ConnectorScopeManifestService,
    )
    from asset_management.web.services.onboarding_oauth_error_service import (
        OnboardingOAuthErrorService,
    )
    from asset_management.web.services.provider_identity_service import (
        ProviderIdentityService,
    )
    from asset_management.web.services.scope_reconciliation_service import (
        ReconciliationOutcome,
        ScopeReconciliationService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    active = WizardSessionService.get_active(username, "onboarding") or {}
    state = dict(active.get("state") or {})
    selected_connector_id = (state.get("selected_connector_id") or "").strip()

    if not selected_connector_id:
        return redirect(url_for("analytics.onboarding_step6_page"))

    manifest = ConnectorScopeManifestService.get(selected_connector_id)
    if manifest is None:
        # Stale wizard_session or a connector without a manifest —
        # bounce back to step-4 so the user re-picks rather than
        # staring at a blank permissions list.
        return redirect(url_for("analytics.onboarding_step6_page"))

    tile = ConnectorCatalogService.get(selected_connector_id)
    connector_name = tile.name if tile else selected_connector_id

    # "Logged in as … — Switch account?" surface. Step-5
    # runs before any OAuth, so we don't have a provider-side token
    # to query a real `me` endpoint with. The service falls back to
    # the DEAM session's email + full_name and builds a per-provider
    # logout-then-return URL so the user can force a provider-side
    # account picker on the next OAuth.
    session_full_name = session.get("full_name", "") or ""
    # the identity-surface "Switch account" link now points
    # at the wizard's re-OAuth route with ?reauth=1, NOT at the
    # provider's logout endpoint. Facebook's logout.php?next= stopped
    # honoring non-facebook.com destinations years ago and silently
    # dropped users on facebook.com home page still logged in. The
    # reauth path uses Meta's auth_type=reauthenticate OAuth parameter
    # to force the account picker without provider-side logout.
    # Host-header injection blocked at nginx (fixed server_name +
    # nginx also pins X-Forwarded-Host = $host, so ProxyFix (x_host=1) cannot be steered by a client header.
    # X-Forwarded-Host override). The Switch account URL needs to be
    # absolute because the identity-surface anchor renders verbatim.
    # nosemgrep: flask-url-for-external-true - host header is fixed by nginx
    return_to_url = url_for(
        "analytics.onboarding_step7_start_oauth",
        reauth="1",
        _external=True,
    )
    identity_surface = ProviderIdentityService.resolve(
        connector_id=selected_connector_id,
        session_username=username,
        session_full_name=session_full_name,
        return_to_url=return_to_url,
    )

    # recover gracefully from an OAuth failure. The Meta
    # callback redirects
    # back here with /onboarding/step-7?error=<code> on provider-side
    # cancel / exchange failure. The wizard_session selection survives
    # because it was persisted on step-4 and the cancel/error path
    # only pops oauth_state, never selected_connector_id.
    error_code = (request.args.get("error") or "").strip()
    oauth_error = OnboardingOAuthErrorService.resolve(error_code)
    if oauth_error is not None:
        AuditLogService.log_event(
            username,
            "onboarding_step7_errored",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"analytics_event=wizard.step7.errored "
                f"connector_id={selected_connector_id} "
                f"error_code={oauth_error.code} "
                f"raw_error={error_code}"
            ),
        )

    # partial-scope reconciliation. After the OAuth callback
    # stamps ``state.granted_scopes`` onto the wizard_session, the
    # step-5 GET re-renders with one of three outcomes:
    #
    # FULL — every requested scope granted; advances
    #                 the user to step-6 (the asset selection table).
    #   PARTIAL     — at least one granted, at least one missing;
    #                 render per-missing-scope explainer + "Re-grant
    #                 access" anchor at /start-oauth?scopes=<missing>.
    #   NONE_USABLE — zero usable scopes granted; render the
    #                 docs-handoff card (AC: "skip step 6 if no usable
    #                 scopes were granted; route to docs").
    #
    # When ``state.granted_scopes`` is absent (the user hasn't hit
    # OAuth yet), reconciliation is ``None`` and step-5 renders the
    # pre-redirect summary unchanged. The reconciler returns
    # ``None`` for an unknown connector_id, but the manifest gate above
    # already bounced that case back to step-4.
    granted_scopes_raw = state.get("granted_scopes")
    reconciliation = None
    if granted_scopes_raw is not None:
        reconciliation = ScopeReconciliationService.reconcile(
            connector_id=selected_connector_id,
            granted_scopes=granted_scopes_raw,
        )

    # on a FULL reconciliation, advance to step-6 (the
    # asset selection table). The PARTIAL / NONE_USABLE branches stay
    # on step-5 so the user can re-grant or hand off to docs. Stamping
    # current_step=step-6 lets a refresh / OAuth-aware-resume land
    # the user on step-6 directly.
    if reconciliation is not None and reconciliation.outcome == ReconciliationOutcome.FULL:
        state["current_step"] = "step-8"
        try:
            WizardSessionService.save(
                user_id=username,
                wizard_type="onboarding",
                current_step="step-8",
                state=state,
            )
        except Exception as e:  # noqa: BLE001 — same fail-soft policy
            logging.warning("step-5 advance-to-step-6 save failed for %r: %s", username, e)
        AuditLogService.log_event(
            username,
            "onboarding_step7_reconciled",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"analytics_event=wizard.step7.reconciled "
                f"connector_id={selected_connector_id} "
                f"outcome=full "
                f"missing_count=0"
            ),
        )
        return redirect(url_for("analytics.onboarding_step8_page"))

    # Re-grant URL: only the missing scopes go on the next OAuth (the
    # AC's "re-redirect with the missing scopes only" requirement).
    # ``None`` outside the PARTIAL bucket — the template only renders
    # the re-grant button when the bucket calls for it.
    regrant_url = None
    if reconciliation is not None and reconciliation.outcome == ReconciliationOutcome.PARTIAL:
        missing_scope_str = ScopeReconciliationService.re_grant_scope_string(reconciliation.missing_keys)
        regrant_url = url_for(
            "analytics.onboarding_step7_start_oauth",
            scopes=missing_scope_str,
        )

    # Docs target for the NONE_USABLE bucket. Stable, public marketing
    # URL — keeps the wizard out of the broken state where a user who
    # denied everything dead-ends on step-5.
    docs_url = "https://kavanahanalytics.com/docs/connectors/meta-ads"

    # Audit-log slice the marketing funnel keys off. Fires only when a
    # reconciliation actually happened (granted_scopes present); a
    # plain pre-OAuth render is the existing onboarding_step7_entered
    # event below.
    if reconciliation is not None:
        AuditLogService.log_event(
            username,
            "onboarding_step7_reconciled",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"analytics_event=wizard.step7.reconciled "
                f"connector_id={selected_connector_id} "
                f"outcome={reconciliation.outcome.value} "
                f"missing_count={len(reconciliation.missing_keys)}"
            ),
        )

    AuditLogService.log_event(
        username,
        "onboarding_step7_entered",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(f"analytics_event=wizard.step7.entered " f"connector_id={selected_connector_id}"),
    )

    # canonical envelope event alongside the pre-existing rich
    # ``wizard.step7.entered`` above. Both fire on every step-5 render;
    # the rich one carries connector_id only, the envelope adds the
    # five-field schema (user_id, account_type, connector, cohort,
    # wizard_session_id) so the funnel can slice without joining ES.
    WizardAnalyticsService.emit(
        step="step-7",
        action=ACTION_ENTERED,
        context=WizardAnalyticsService.build_context(
            username=username,
            wizard_session=active or None,
            connector_override=selected_connector_id,
        ),
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
        extra={"oauth_error": oauth_error or ""},
    )

    # Try-again target — same page minus the ?error= query string so a
    # second failure doesn't compound onto the URL.
    retry_url = url_for("analytics.onboarding_step7_page")

    csrf_token = auth_module.refresh_csrf_token()
    return render_template(
        "onboarding_step7_permissions.html",
        csrf_token=csrf_token,
        username=username,
        connector_id=selected_connector_id,
        connector_name=connector_name,
        scope_groups=manifest.groups,
        never_does=ConnectorScopeManifestService.never_does(selected_connector_id),
        identity_surface=identity_surface,
        oauth_error=oauth_error,
        retry_url=retry_url,
        reconciliation=reconciliation,
        regrant_url=regrant_url,
        docs_url=docs_url,
        progress=_wizard_progress_for("step-7", username),
    )


@analytics_bp.route("/onboarding/step-7", methods=["POST"])
def onboarding_step7_post():
    """Continue / Cancel handoff for the step-5 scope review.

    Two ``action`` values:

    * ``continue`` — user agreed to the scopes. Stamp
      ``state.current_step=step-8`` so a refresh/resume lands the
      user on the next step, fire an ``onboarding_step7_continue``
      audit event, and redirect to the wizard-aware Meta OAuth
      roundtrip. The
      callback returns the user here with ``granted_scopes`` stamped
      on the wizard_session; on a FULL reconciliation the GET handler
      advances to step-8.
    * ``cancel`` — user backed out. No wizard_session mutation;
      redirect to /onboarding/step-6 so they can pick a different
      connector. The cancel path is the recoverability AC: "Cancel
      returns to step 4 without state loss."

    Any other ``action`` (including missing) is treated as cancel —
    safer default since this route never mutates state on cancel.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        abort(403)

    # follow-up — block re-entry POSTs after the wizard is done.
    _done_redirect = _redirect_if_wizard_done(username)
    if _done_redirect is not None:
        return _done_redirect

    action = (request.form.get("action") or "").strip().lower()

    if action != "continue":
        # Cancel (or unknown) — no state mutation, route back to
        # step-4. Fire the cancel audit event so the funnel slice
        # can count drop-offs at the permissions screen.
        AuditLogService.log_event(
            username,
            "onboarding_step7_cancel",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"action={action or '(none)'}",
        )
        # Cancel on the permissions screen is a deliberate
        # back-out; treat as a step-5 skip so the funnel can count
        # users who saw the scope summary and abandoned without OAuth.
        from asset_management.web.services.wizard_session_service import (
            WizardSessionService as _CancelWSS,
        )

        _cancel_active = _CancelWSS.get_active(username, "onboarding")
        WizardAnalyticsService.emit(
            step="step-7",
            action=ACTION_SKIPPED,
            context=WizardAnalyticsService.build_context(
                username=username,
                wizard_session=_cancel_active,
            ),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"reason": "cancel"},
        )
        return redirect(url_for("analytics.onboarding_step6_page"))

    from asset_management.web.services.connector_scope_manifest_service import (
        ConnectorScopeManifestService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    active = WizardSessionService.get_active(username, "onboarding") or {}
    state = dict(active.get("state") or {})
    selected_connector_id = (state.get("selected_connector_id") or "").strip()

    # Same allowlist guard as the GET — a forged POST with a stale or
    # unsupported connector id can't slip past the manifest check
    # into the "advance to step-6" path.
    if not selected_connector_id or not ConnectorScopeManifestService.get(selected_connector_id):
        return redirect(url_for("analytics.onboarding_step6_page"))

    state["current_step"] = "step-8"
    try:
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-8",
            state=state,
        )
    except Exception as e:  # noqa: BLE001 — same fail-soft policy as the earlier steps
        logging.warning("step-5 session-save failed for %r: %s", username, e)

    AuditLogService.log_event(
        username,
        "onboarding_step7_continue",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"connector_id={selected_connector_id}",
    )

    # canonical step-5 completion fires on Continue (user
    # agreed to the scopes and is about to enter the OAuth roundtrip).
    # The actual OAuth callback's success/failure is separate funnel
    # event-space: ``wizard.step7.oauth_callback_ok`` already exists for
    # the rich event, and reaching step-6 in the funnel is the
    # downstream signal that OAuth succeeded.
    WizardAnalyticsService.emit(
        step="step-7",
        action=ACTION_COMPLETED,
        context=WizardAnalyticsService.build_context(
            username=username,
            wizard_session=active or None,
            connector_override=selected_connector_id,
        ),
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
    )

    # Continue now hands off to the wizard-aware OAuth
    # roundtrip (/onboarding/step-7/start-oauth) instead of
    # soft-redirecting to /dashboard. The OAuth callback stamps
    # ``state.granted_scopes`` and bounces the user back to step-5,
    # where the reconciler decides FULL / PARTIAL / NONE_USABLE.
    return redirect(url_for("analytics.onboarding_step7_start_oauth"))


# ============================================
# ONBOARDING STEP 5 OAUTH ROUNDTRIP
# ============================================
# Wizard-aware Meta OAuth handoff. Separate from the asset-management
# fb_oauth_server.py flow on :8083 — that flow stores tokens per FB-app-
# user; the wizard's flow only reads granted_scopes off /debug_token and
# discards the access token. is scoped to scope reconciliation;
# token persistence is a follow-up ticket.
#
# Both routes live under analytics_bp so they share the same
# url_for("analytics.…") naming as the rest of the wizard. Pre-auth
# allowlist entries in auth.py mirror the google_start /
# google_callback pattern.


@analytics_bp.route("/onboarding/step-7/start-oauth")
def onboarding_step7_start_oauth():
    """Initiate the wizard's Meta OAuth roundtrip.

    Reads the active wizard_session, picks up the user's selected
    connector, mints a state token, stores it on wizard_session.state
    (NOT just the Flask session — a multi-tab user shouldn't be able
    to replay state across tabs, and a refresh of the OAuth-initiator
    tab needs to survive), and redirects to the Meta authorize URL.

    ``?scopes=<comma-list>`` narrows the request to a subset of the
    manifest's scopes — the AC's "re-redirect with the missing scopes
    only" path on a partial-scope re-grant. When the query param is
    absent or empty, the wizard requests the full manifest set
    (first-time OAuth from step-5 Continue).

    Failures fall back to the step-5 GET with a synthetic
    ``?error=server_error`` so the existing banner surfaces a
    coherent retry path instead of a 500.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    from asset_management.web.services.connector_scope_manifest_service import (
        ConnectorScopeManifestService,
    )
    from asset_management.web.services.wizard_oauth_service import (
        WizardOAuthService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    active = WizardSessionService.get_active(username, "onboarding") or {}
    state_blob = dict(active.get("state") or {})
    selected_connector_id = (state_blob.get("selected_connector_id") or "").strip()
    if not selected_connector_id or not ConnectorScopeManifestService.get(selected_connector_id):
        return redirect(url_for("analytics.onboarding_step6_page"))

    # Re-grant path narrows the scope list to ?scopes=… (comma-joined,
    # already lower-cased by ScopeReconciliationService.re_grant_scope_string).
    # Validate against the manifest's full scope set so a forged query
    # string can't widen the ask beyond what was originally summarised
    # on step-5.
    raw_scope_param = (request.args.get("scopes") or "").strip()
    allowed_scopes = set(WizardOAuthService.default_scopes_for(selected_connector_id))
    if raw_scope_param:
        requested_subset = {s.strip().lower() for s in raw_scope_param.split(",") if s.strip()}
        # Intersect: drop anything not in the original manifest. If
        # the intersection is empty (forged query string with no valid
        # keys), fall back to the full set so the user still gets a
        # working OAuth.
        scopes = tuple(sorted(requested_subset & allowed_scopes)) or tuple(sorted(allowed_scopes))
    else:
        scopes = tuple(sorted(allowed_scopes))

    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None:
        AuditLogService.log_event(
            username,
            "onboarding_step7_oauth_start_failed",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"analytics_event=wizard.step7.oauth_start_failed " f"connector_id={selected_connector_id} " f"reason=no_app"
            ),
        )
        # Bounce to step-5 with a server_error banner so the user
        # sees a coherent retry path instead of a 500.
        return redirect(url_for("analytics.onboarding_step7_page", error="server_error"))

    oauth_state = WizardOAuthService.generate_state_token()
    state_blob["oauth_state"] = oauth_state
    state_blob["oauth_app_esid"] = meta_app.esid
    # Persist the scope subset so the callback can audit-log "user
    # was asked for X, granted Y" without a re-derivation.
    state_blob["oauth_requested_scopes"] = list(scopes)
    try:
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-7",
            state=state_blob,
        )
    except Exception as e:  # noqa: BLE001 — fail-soft policy mirrors earlier steps
        logging.warning("step-5 oauth-state save failed for %r: %s", username, e)
        return redirect(url_for("analytics.onboarding_step7_page", error="server_error"))

    # OAuth 2.0 redirect_uri MUST be absolute per RFC 6749 §3.1.2. Host-header injection blocked at
    # nginx; Meta only accepts pre-registered redirect_uri values so off-host steering can't complete.
    # nosemgrep: flask-url-for-external-true - OAuth redirect_uri must be absolute; host fixed by nginx
    redirect_uri = url_for("analytics.onboarding_step7_oauth_callback", _external=True)
    # when the user clicked "Switch account" on step-5/step-6,
    # the calling link passes ?reauth=1. Threaded through to
    # build_authorize_url which appends auth_type=reauthenticate +
    # auth_nonce. Without this Meta would silently honor the existing
    # FB session and send the user back through OAuth as the SAME
    # account they wanted to switch away from.
    reauth_flag = (request.args.get("reauth") or "").strip() == "1"
    authorize_url = WizardOAuthService.build_authorize_url(
        client_id=meta_app.client_id,
        redirect_uri=redirect_uri,
        state=oauth_state,
        scopes=scopes,
        reauthenticate=reauth_flag,
    )

    AuditLogService.log_event(
        username,
        "onboarding_step7_oauth_started",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event=wizard.step7.oauth_started "
            f"connector_id={selected_connector_id} "
            f"requested_scopes={','.join(scopes)} "
            f"reauth={int(reauth_flag)}"
        ),
    )
    return redirect(authorize_url)


# ─────────────────────────────────────────────────────────────────────────
# reauthorize-callback helper.
#
# Re-uses the wizard's Meta-side OAuth callback URL (it's already
# pre-registered with the Meta app) but takes a different post-exchange
# branch: read the existing connection by esid, preserve its provider
# (facebook OR instagram — both share the Meta app), upsert against
# the existing natural key so the token refreshes in place, redirect
# to /connections/<esid>.
#
# State is keyed by connection_esid in flask.session (not on
# de_wizard_session), so the wizard's analytics index stays clean.
# ─────────────────────────────────────────────────────────────────────────


def _handle_reauth_callback(connection_esid: str):
    """process the OAuth callback for a Meta connection
    reauthorise initiated from /connections/<esid>/start-reauth.

    On the happy path:

    1. Validates the state token against ``session[oauth_reauth_state:<esid>]``.
    2. Exchanges the code via ``WizardOAuthService.exchange_code_for_granted_scopes``.
    3. Reads the existing ``de_connection`` row to determine its
       ``provider`` (facebook or instagram — both share the Meta app)
       and ``provider_account_id``. The connection's natural key is
       preserved, so ``upsert_connection`` refreshes the token in
       place rather than creating a duplicate.
    4. Redirects to ``/connections/<esid>`` with a one-shot
       ``?reauth=ok`` banner.

    Failure modes (state mismatch, exchange failure, missing
    connection, principal mismatch) all consume the state token (so a
    leaked callback URL can't be replayed) and redirect to the same
    detail page with ``?reauth=error&code=<reason>`` — the template
    surfaces the banner without an error class that exposes implementation
    details.
    """
    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    from asset_management.web.services.connection_service import (
        ConnectionService,
        PROVIDER_FACEBOOK,
        PROVIDER_INSTAGRAM,
    )
    from asset_management.web.services.wizard_oauth_service import (
        WizardOAuthService,
    )

    state_expected = session.pop(f"oauth_reauth_state:{connection_esid}", "")
    app_esid_expected = session.pop(f"oauth_reauth_app_esid:{connection_esid}", "")

    # Build a one-stop redirect helper for the failure path. Detail
    # page reads ?reauth=… and ?code=… to render the banner.
    def _back_with_error(code: str):
        AuditLogService.log_event(
            username,
            "connection_reauth_callback_error",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(f"analytics_event=connection.reauth.callback_error " f"connection_esid={connection_esid} reason={code}"),
        )
        return redirect(_connection_detail_url(connection_esid, reauth="error", code=code))

    provider_error = (request.args.get("error") or "").strip()
    if provider_error:
        return _back_with_error(provider_error or "provider_error")

    if not state_expected or not app_esid_expected:
        # Replay or hand-typed URL — no state on this Flask session.
        return _back_with_error("missing_state")

    source = ConnectionService.get_connection(connection_esid)
    if not source:
        return _back_with_error("connection_not_found")
    # Tier 1 — mirror start_reauthorize's widened gate. A principal-only
    # check here would let a qualified admin start a re-auth and then fail at the
    # callback, which is a worse dead end than never offering the button.
    from asset_management.web.blueprints.connections.routes import (
        _viewer_can_reauthorize_connection,
    )

    if not _viewer_can_reauthorize_connection(source, username, connection_esid):
        return _back_with_error("connection_not_found")

    provider = (source.get("provider") or "").strip()
    if provider not in (PROVIDER_FACEBOOK, PROVIDER_INSTAGRAM):
        # Defence in depth — start-reauth already gated on this set.
        return _back_with_error("provider_not_supported")

    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None or meta_app.esid != app_esid_expected:
        return _back_with_error("app_mismatch")

    code = (request.args.get("code") or "").strip()
    state_received = (request.args.get("state") or "").strip()
    # nosemgrep: flask-url-for-external-true - OAuth redirect_uri must be absolute; host fixed by nginx
    redirect_uri = url_for("analytics.onboarding_step7_oauth_callback", _external=True)

    result = WizardOAuthService.exchange_code_for_granted_scopes(
        code=code,
        state_received=state_received,
        state_expected=state_expected,
        client_id=meta_app.client_id,
        client_secret=meta_app.client_secret,
        redirect_uri=redirect_uri,
    )

    if not result.success:
        return _back_with_error(result.code or "exchange_failed")

    # Tier 2 — COVERAGE CHECK, before anything is committed.
    #
    # A structurally valid token can reach nothing. Meta's consent dialog has a
    # separate asset-SELECTION step, so a user can approve every scope while
    # ticking zero pages; the token comes back valid and returns empty lists
    # forever, and nothing in the OAuth response says so. That is the concrete
    # mechanism behind "it says connected but isn't", and it is the default
    # outcome when someone rushes the dialog.
    #
    # So: probe the new token against the connection's own assets FIRST. Full
    # coverage commits. Partial coverage is STAGED and requires explicit
    # acknowledgment — because silently replacing a working token with one that
    # reaches less is the failure this whole ticket exists to prevent.
    coverage = _reauth_coverage(connection_esid, result.access_token)

    if _reauth_decision(coverage) == "commit":
        committed = ConnectionService.adopt_reauth_token(
            connection_esid,
            actor=username,
            oauth_token=result.access_token,
            token_expires_at=result.token_expires_at,
            data_access_expires_at=result.data_access_expires_at,
            scopes=list(result.granted_scopes),
            coverage=coverage,
        )
        if not committed:
            logging.warning("reauth adopt failed for %s", connection_esid)
            return _back_with_error("persist_failed")
        outcome, redirect_kwargs = "ok", {"reauth": "ok"}
    else:
        staged = ConnectionService.stage_reauth_token(
            connection_esid,
            actor=username,
            oauth_token=result.access_token,
            token_expires_at=result.token_expires_at,
            data_access_expires_at=result.data_access_expires_at,
            scopes=list(result.granted_scopes),
            coverage=coverage,
        )
        if not staged:
            logging.warning("reauth stage failed for %s", connection_esid)
            return _back_with_error("persist_failed")
        outcome, redirect_kwargs = "partial", {"reauth": "partial"}

    AuditLogService.log_event(
        username,
        "connection_reauth_callback_ok",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event=connection.reauth.callback_ok "
            f"connection_esid={connection_esid} "
            f"provider={provider} "
            f"outcome={outcome} "
            f"coverage={coverage['covered']}/{coverage['total']} "
            f"granted_scopes={','.join(result.granted_scopes)}"
        ),
    )
    return redirect(_connection_detail_url(connection_esid, **redirect_kwargs))


def _connection_detail_url(connection_esid: str, **query) -> str:
    """Connection detail page URL, canonical under the ``/assets`` prefix.

    the shared OAuth callback is served on the BARE ``/onboarding``
    path (the only form registered with Meta), so ``url_for`` here builds
    ``/connections/<esid>`` without ``/assets``. nginx happens to route that
    , but the page then renders every ``url_for`` link without the
    prefix too — its "Back to Data Connectors" pointed at bare
    ``/data-connectors/``, which nginx 404s. Send the browser to the same URL
    Data Connectors itself deep-links to.
    """
    path = url_for("connections.connection_detail", connection_esid=connection_esid, **query)
    if path.startswith("/connections/"):
        path = "/assets" + path
    return path


def _reauth_decision(coverage: dict) -> str:
    """Commit the new token, or stage it pending acknowledgment?

    Extracted as a pure function because it is the load-bearing decision of the
    whole ticket and was previously an inline conditional no test could reach —
    a mutation that made the callback commit unconditionally passed the entire
    suite.

    ``commit`` only when coverage was genuinely established AND nothing failed:

    * ``error``  -> stage. We could not check, which is not the same as fine.
    * ``total==0`` -> commit. There were no assets to check; a connection with no
      linked assets must still be re-authorisable.
    * ``fully_covered`` -> commit.
    * anything else (partial) -> stage, and make a human look at the list.
    """
    if coverage.get("error"):
        return "stage"
    if coverage.get("total", 0) == 0:
        return "commit"
    return "commit" if coverage.get("fully_covered") else "stage"


# DEAM's de_connection_asset vocabulary -> the prober's. Verified against the
# live index 2026-08-27: fb_page (87), fb_ad_account (32), ig_account (6).
_ASSET_TYPE_TO_PROBE = {
    "fb_page": "page",
    "fb_ad_account": "ad_account",
    "ig_account": "ig",
}


def _reauth_coverage(connection_esid: str, access_token: str) -> dict:
    """Probe every asset linked to a connection with a candidate token.

    Reuses prober — the scheduled sweep and this coverage check are
    the same call with a different token, so the two can never disagree about
    what "reachable" means.

    Returns the ``summarize()`` shape plus an ``error`` flag.

    Fails CLOSED for real: an internal failure sets ``error=True``, and the
    caller stages rather than commits. An earlier version returned
    ``total=0, fully_covered=False`` and called that "closed" — but ``total==0``
    is the *no assets to check* case, which commits. A NameError in this function
    would therefore have committed every re-auth with no coverage check at all,
    silently. Distinguishing "nothing to check" from "could not check" is the
    whole point.
    """
    try:
        from asset_management.web.services.connection_service import ConnectionService
        from lib.credential_prober import probe_assets, summarize, TokenProbe

        assets = []
        for hit in ConnectionService.list_assets_for_connection(connection_esid):
            src = hit.get("_source") or {}
            probe_type = _ASSET_TYPE_TO_PROBE.get(src.get("asset_type") or "")
            if not probe_type:
                continue  # not a Meta asset we know how to touch
            assets.append(
                {
                    "asset_type": probe_type,
                    "asset_id": str(src.get("provider_asset_id") or ""),
                    "asset_name": src.get("label") or "",
                }
            )
        probes = probe_assets(access_token, assets)
        out = summarize(TokenProbe(reachable=True, is_valid=True), probes)
        out["error"] = False
        return out
    except Exception as exc:  # noqa: BLE001
        logging.warning("coverage probe failed for %s: %s", connection_esid, exc)
        return {
            "token": {},
            "assets": [],
            "covered": 0,
            "total": 0,
            "fully_covered": False,
            # NOT the same as "no assets" — the caller must stage, not commit.
            "error": True,
        }


def _try_wizard_token_adoption(
    *,
    placeholder_esid: str,
    provider: str,
    actor: str,
    provider_account_id: str,
    access_token: str,
    token_expires_at: str | None,
    scopes: list[str],
    data_access_expires_at: str | None = None,
) -> str | None:
    """Phase B — is this wizard OAuth completion really a RECONNECT
    of a connection we already have?

    The wizard is the re-auth path users actually take (nine
    silent attempts), and for a SECOND person the same-principal
    merge cannot fire — so earlier every such completion minted a
    fresh row while the team references stayed on the dead one.

    Adoption commits if and only if ALL of:

      * the merge will NOT handle it (no same-(actor, provider,
        account) row — that merge stays the more specific match);
      * the actor could press Reauthorize on the candidate — the SAME
        authority predicate as the connection page
        (``_viewer_can_reauthorize_connection``: principal / explicit
        superuser / scoped admin / System-2 owner / workspace-scoped
        ``Permission.CONNECTION_REAUTHORIZE``). This is the workspace
        boundary the guarantee moved to — cross-tenant adoption
        is structurally impossible;
      * the new token FULLY covers the candidate's ALREADY-BOUND assets
        (``_reauth_coverage`` → the shared prober). We only ever match
        against bound assets — token enumeration never claims anything
        (Meta rule 5: access is not ownership; the hard rule
        stands);
      * exactly ONE candidate matches. Zero, partial-only, ambiguous, or
        probe-error outcomes all fall back to today's behavior (a new
        row) with the reason audit-logged — never a guess, never a
        silently weakened working token, never a blocked wizard.

    Returns the adopted canonical esid, or None to fall back.
    """
    from asset_management.web.services.connection_service import ConnectionService

    # merge domain: the actor already has a row for this exact
    # account — set_token_for_pending_connection will merge onto it.
    if ConnectionService.find_by_natural_key(actor, provider, provider_account_id):
        return None

    # One authority definition for "may re-authorize this connection" —
    # imported from the connections blueprint rather than re-derived here,
    # so the wizard and the Reauthorize button can never disagree.
    from asset_management.web.blueprints.connections.routes import (
        _viewer_can_reauthorize_connection,
    )

    matches: list[tuple[str, dict]] = []
    skipped: list[str] = []
    for hit in ConnectionService.list_adoption_candidates(provider, exclude_esid=placeholder_esid):
        cand_esid = str(hit.get("_id") or "")
        cand_src = hit.get("_source") or {}
        if not _viewer_can_reauthorize_connection(cand_src, actor, cand_esid):
            continue
        coverage = _reauth_coverage(cand_esid, access_token)
        if coverage.get("error"):
            skipped.append(f"{cand_esid}:probe_error")
            continue
        if not coverage.get("total"):
            continue  # no bound assets — no basis to call it a reconnect
        if coverage.get("fully_covered"):
            matches.append((cand_esid, coverage))
        else:
            skipped.append(f"{cand_esid}:partial({coverage.get('covered')}/{coverage.get('total')})")

    if len(matches) != 1:
        if matches or skipped:
            reason = "ambiguous" if len(matches) > 1 else "no_full_coverage"
            AuditLogService.log_event(
                actor,
                "wizard_token_adoption_skipped",
                True,
                detail=(
                    f"analytics_event=wizard.token_adoption_skipped reason={reason} "
                    f"full=[{','.join(e for e, _ in matches)}] other=[{','.join(skipped)}] "
                    f"placeholder={placeholder_esid} provider={provider}"
                ),
            )
        return None

    cand_esid, coverage = matches[0]
    adopted = ConnectionService.adopt_pending_into(
        placeholder_esid,
        cand_esid,
        actor=actor,
        oauth_token=access_token,
        token_expires_at=token_expires_at,
        data_access_expires_at=data_access_expires_at,
        scopes=scopes,
        coverage={k: coverage.get(k) for k in ("covered", "total", "fully_covered")},
    )
    if adopted:
        AuditLogService.log_event(
            actor,
            "wizard_token_adopted",
            True,
            detail=(
                f"analytics_event=wizard.token_adopted connection_esid={adopted} "
                f"placeholder={placeholder_esid} provider={provider} "
                f"coverage={coverage.get('covered')}/{coverage.get('total')}"
            ),
        )
    return adopted


def _popup_close_response() -> tuple[str, int, dict[str, str]]:
    """Return a minimal HTML page that closes the popup. The
    wizard's underlying tab keeps polling /oauth-status — that's
    the source of truth for the wizard's state machine.

    Module-level because both the data-connectors callback
    branch and the orphaned-popup fallback in
    ``onboarding_step7_oauth_callback`` need it.
    """
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>OAuth complete</title></head><body>"
        "<script>try { window.close(); } catch(e) {}</script>"
        "<p>You may close this window.</p></body></html>"
    )
    return body, 200, {"Content-Type": "text/html; charset=utf-8"}


def _handle_data_connectors_callback(connection_esid: str):
    """Path A — process the OAuth callback for the migrated
    data-connectors FB wizard.

    Mirrors :func:`_handle_reauth_callback` structurally — Flask-session
    keyed state + app_esid, single-use token consumption, audit-logged
    failure modes — with two differences:

    1. The connection row already exists (created at oauth-start time
       with ``status=pending_token`` and a placeholder
       ``provider_account_id``). Successful callback flips the row to
       ``active`` IN PLACE via
       ``ConnectionService.set_token_for_pending_connection`` so the
       frontend's poll-by-esid loop sees ``has_token=true`` on the
       same esid it opened the popup with.
    2. There is no detail page to redirect to. The callback returns a
       minimal HTML page that closes the popup window; the wizard JS
       in the underlying tab already polls
       ``/api/wizard/fb/oauth-status/<esid>`` so the popup-close is
       sufficient.
    """
    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    from asset_management.web.services.connection_service import (
        ConnectionService,
    )
    from asset_management.web.services.wizard_oauth_service import (
        WizardOAuthService,
    )

    state_expected = session.pop(f"oauth_dc_state:{connection_esid}", "")
    app_esid_expected = session.pop(f"oauth_dc_app_esid:{connection_esid}", "")

    def _close_with_error(code: str):
        AuditLogService.log_event(
            username,
            "data_connectors_oauth_callback_error",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"analytics_event=data_connectors.oauth.callback_error " f"connection_esid={connection_esid} reason={code}"
            ),
        )
        return _popup_close_response()

    provider_error = (request.args.get("error") or "").strip()
    if provider_error:
        return _close_with_error(provider_error or "provider_error")

    if not state_expected or not app_esid_expected:
        return _close_with_error("missing_state")

    source = ConnectionService.get_connection(connection_esid)
    if not source:
        return _close_with_error("connection_not_found")
    if (source.get("principal") or "") != username:
        # State was stashed under our session but the connection's
        # principal doesn't match — treat as not-found (don't leak
        # existence by distinguishing the two cases).
        return _close_with_error("connection_not_found")

    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None or meta_app.esid != app_esid_expected:
        return _close_with_error("app_mismatch")

    code = (request.args.get("code") or "").strip()
    state_received = (request.args.get("state") or "").strip()
    # Path A — must match the authorize-side redirect_uri
    # byte-for-byte. The authorize call (data_connectors.routes
    # api_wizard_fb_oauth_start) strips the /assets/ prefix so the
    # URL matches the form registered in the Meta App Dashboard.
    # Apply the same strip here — the callback normally arrives at
    # bare /onboarding/... but defending against a /assets/... entry
    # path keeps the two sides identical regardless.
    # nosemgrep: flask-url-for-external-true - OAuth redirect_uri must be absolute; host fixed by nginx
    redirect_uri = url_for("analytics.onboarding_step7_oauth_callback", _external=True)
    redirect_uri = redirect_uri.replace("/assets/onboarding/", "/onboarding/", 1)

    result = WizardOAuthService.exchange_code_for_granted_scopes(
        code=code,
        state_received=state_received,
        state_expected=state_expected,
        client_id=meta_app.client_id,
        client_secret=meta_app.client_secret,
        redirect_uri=redirect_uri,
    )

    if not result.success:
        return _close_with_error(result.code or "exchange_failed")

    # Phase B — before minting a new row, check whether this
    # completion is really a RECONNECT of an existing connection the actor
    # is authorized on (second-person wizard re-run: the merge
    # below can't fire because both principal AND Meta user id differ, so
    # pre-1424 the token landed on a phantom new row while every team
    # reference stayed on the dead one). Best-effort: any failure in the
    # adoption machinery falls back to the plain flip — never a lost token,
    # never a blocked wizard.
    canonical_esid = None
    try:
        canonical_esid = _try_wizard_token_adoption(
            placeholder_esid=connection_esid,
            provider=source.get("provider") or "",
            actor=username,
            provider_account_id=result.provider_account_id or username,
            access_token=result.access_token,
            token_expires_at=result.token_expires_at,
            data_access_expires_at=result.data_access_expires_at,
            scopes=list(result.granted_scopes),
        )
    except Exception as e:  # noqa: BLE001 — adoption is an optimization; the flip below is the safe floor
        logging.warning("wizard adoption check failed for %s: %s", connection_esid, e)

    # Flip the pending row to active. provider_account_id moves from the
    # wizard-pending-... placeholder to the real Meta /me user_id. —
    # when the principal ALREADY has a connection for that real account, the
    # token lands on that existing row instead (no duplicate); the placeholder
    # becomes a superseded tombstone and the returned canonical esid differs
    # from connection_esid. The frontend's poll follows the tombstone redirect
    # and adopts the canonical esid.
    if not canonical_esid:
        canonical_esid = ConnectionService.set_token_for_pending_connection(
            connection_esid,
            provider_account_id=result.provider_account_id or username,
            oauth_token=result.access_token,
            token_expires_at=result.token_expires_at,
            data_access_expires_at=result.data_access_expires_at,
            scopes=list(result.granted_scopes),
        )
    if not canonical_esid:
        logging.warning(
            "Path A: callback persist failed for connection=%s",
            connection_esid,
        )
        return _close_with_error("persist_failed")

    AuditLogService.log_event(
        username,
        "data_connectors_oauth_callback_ok",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event=data_connectors.oauth.callback_ok "
            f"connection_esid={connection_esid} "
            f"canonical_esid={canonical_esid} "
            f"granted_scopes={','.join(result.granted_scopes)}"
        ),
    )
    return _popup_close_response()


@analytics_bp.route("/onboarding/step-7/oauth-callback")
def onboarding_step7_oauth_callback():
    """Receive Meta's OAuth callback.

    Validates ``state`` against the wizard_session blob (NOT the Flask
    session), exchanges the code for a token, calls /debug_token to
    read granted scopes, stamps ``state.granted_scopes`` onto the
    wizard_session, and redirects back to /onboarding/step-7 where the
    reconciler decides FULL / PARTIAL / NONE_USABLE.

     follow-up — persists the token to ``de_connection`` via
    ``ConnectionService.upsert_connection`` on the happy path so the
    pending_token row written at step-6 finish
    flips to ``status=active`` once the token lands. Upsert is
    natural-key safe: a previous wizard run that wrote a pending_token
    row with the same (principal, provider, provider_account_id) is
    refreshed in place rather than duplicated.

    Failure modes (state mismatch, token exchange failed, debug_token
    failed, Meta returned ``error=…``) all redirect to step-5 with the
    existing ``?error=…`` banner code so the recovery UX
    stays consistent across error types.
    """
    # Path A — branch on Flask-session markers BEFORE
    # the onboarding feature-flag gate. The reauth and data-connectors
    # callbacks share this URL (it's the only Meta-pre-registered
    # redirect URI) but are independent of the onboarding feature flag.
    # If we 404'd them here on a flag-off environment, an existing
    # connection's reauth flow would be silently broken whenever the
    # onboarding flag was toggled off.
    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    reauth_connection_esid = session.pop("oauth_reauth_active_esid", "")
    if reauth_connection_esid:
        return _handle_reauth_callback(reauth_connection_esid)

    dc_connection_esid = session.pop("oauth_dc_active_esid", "")
    if dc_connection_esid:
        return _handle_data_connectors_callback(dc_connection_esid)

    from asset_management.web.services.wizard_oauth_service import (
        WizardOAuthService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    # orphaned popup callback. The only flow that legitimately
    # reaches this point is the full-page onboarding OAuth roundtrip, and
    # /onboarding/step-7/start-oauth cannot have redirected to Meta without
    # an active wizard_session (it stashes oauth_state on it first). So a
    # callback arriving with no reauth/data-connectors marker AND no active
    # onboarding session is a stale data-connectors popup whose one-shot
    # session markers were consumed by an earlier callback hit (e.g. the
    # user cancelled at Meta, then clicked "Re-open Facebook popup" on a
    # earlier authorize URL). Redirecting would render the full app
    # inside the popup (step-7 page → wizard-done guard →
    # /dashboard?wizard_done=completed); self-close instead. False-positive
    # risk (ES blip returning None for a genuinely mid-wizard user) is
    # fail-soft: the user sees "You may close this window" and retries.
    active = WizardSessionService.get_active(username, "onboarding")
    if not active:
        AuditLogService.log_event(
            username,
            "onboarding_step7_oauth_callback_orphan",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=("analytics_event=wizard.step7.oauth_callback_orphan " "reason=no_active_wizard_session"),
        )
        return _popup_close_response()

    # Provider-side cancel / error arrives as ?error=…; let the
    # OAuth-error service map the code onto a bucket via
    # the existing step-5 ?error= handler. No state save needed —
    # the wizard_session selection survives because step-4 already
    # persisted selected_connector_id and we don't touch it here.
    provider_error = (request.args.get("error") or "").strip()
    if provider_error:
        AuditLogService.log_event(
            username,
            "onboarding_step7_oauth_callback_error",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(f"analytics_event=wizard.step7.oauth_callback_error " f"provider_error={provider_error}"),
        )
        # canonical step-5 errored event for the OAuth-failure
        # branch. Sits alongside the pre-existing rich
        # ``wizard.step7.oauth_callback_error`` event so the funnel can
        # count step-5 errors via one consistent grep without unioning
        # multiple error-name slices.
        WizardAnalyticsService.emit(
            step="step-7",
            action=ACTION_ERRORED,
            context=WizardAnalyticsService.build_context(
                username=username,
                wizard_session=active,
            ),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"error_code": "oauth_callback_error", "provider_error": provider_error},
        )
        return redirect(url_for("analytics.onboarding_step7_page", error=provider_error))

    state_blob = dict(active.get("state") or {})
    state_expected = (state_blob.get("oauth_state") or "").strip()
    app_esid = (state_blob.get("oauth_app_esid") or "").strip()

    # If the wizard_session has no oauth_state, the callback is a
    # replay (or a hand-typed URL); bounce to step-5 with a generic
    # server_error banner.
    if not state_expected or not app_esid:
        AuditLogService.log_event(
            username,
            "onboarding_step7_oauth_callback_error",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=("analytics_event=wizard.step7.oauth_callback_error " "provider_error=missing_session_state"),
        )
        return redirect(url_for("analytics.onboarding_step7_page", error="server_error"))

    # Re-fetch the Meta app credentials by esid so a concurrent edit
    # to de_asset_fb_apps after /start-oauth doesn't drop us onto a
    # different app's secret.
    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None or meta_app.esid != app_esid:
        return redirect(url_for("analytics.onboarding_step7_page", error="server_error"))

    code = (request.args.get("code") or "").strip()
    state_received = (request.args.get("state") or "").strip()
    # OAuth 2.0 redirect_uri MUST be absolute (RFC 6749 §3.1.2) AND match the value sent at authorize.
    # Same nginx + pre-registered-redirect-URI defenses as the authorize call above.
    # nosemgrep: flask-url-for-external-true - OAuth redirect_uri must be absolute; host fixed by nginx
    redirect_uri = url_for("analytics.onboarding_step7_oauth_callback", _external=True)

    result = WizardOAuthService.exchange_code_for_granted_scopes(
        code=code,
        state_received=state_received,
        state_expected=state_expected,
        client_id=meta_app.client_id,
        client_secret=meta_app.client_secret,
        redirect_uri=redirect_uri,
    )

    # Consume the state token on first use (regardless of outcome) so
    # a leaked callback URL can't be replayed.
    state_blob.pop("oauth_state", None)
    state_blob.pop("oauth_app_esid", None)

    if not result.success:
        # Map our internal exchange-failure codes onto the
        # error vocabulary (server_error for everything except
        # state_mismatch, which has its own bucket via the alias
        # table) so the banner copy reads coherently.
        ui_error = "server_error"
        if result.code == "state_mismatch":
            ui_error = "access_denied"
        AuditLogService.log_event(
            username,
            "onboarding_step7_oauth_callback_error",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(f"analytics_event=wizard.step7.oauth_callback_error " f"exchange_code={result.code}"),
        )
        # Persist the state-token-cleanup even on the failure path so
        # a retried callback doesn't trip the state-mismatch branch.
        try:
            WizardSessionService.save(
                user_id=username,
                wizard_type="onboarding",
                current_step="step-7",
                state=state_blob,
            )
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "step-5 oauth-state cleanup save failed for %r: %s",
                username,
                e,
            )
        return redirect(url_for("analytics.onboarding_step7_page", error=ui_error))

    # Happy path: stamp granted_scopes onto the wizard_session and
    # bounce back to step-5 for reconciliation.
    state_blob["granted_scopes"] = list(result.granted_scopes)
    # follow-up — stamp the provider_account_id (Meta /me id)
    # onto wizard_session so the step-6 finish handler can write the
    # de_connection row keyed by (principal, provider, provider_account_id)
    # without a second debug_token round-trip. Falls back to username on
    # the unlikely case Meta returned an empty user_id.
    state_blob["provider_account_id"] = result.provider_account_id or username
    try:
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-7",
            state=state_blob,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning(
            "step-5 oauth-state granted-save failed for %r: %s",
            username,
            e,
        )

    # follow-up — persist (or refresh) the token to de_connection
    # so any pending_token row written by a prior step-6 finish flips to
    # active. The natural key is (principal, provider, provider_account_id);
    # upsert_connection preserves a prior org_shared decision so this
    # call doesn't reset the user's earlier sharing choice.
    try:
        from asset_management.web.services.connection_service import (
            ConnectionService,
            PROVIDER_FACEBOOK,
        )

        connection_result = ConnectionService.upsert_connection(
            principal=username,
            provider=PROVIDER_FACEBOOK,
            provider_account_id=result.provider_account_id or username,
            oauth_token=result.access_token,
            label=f"meta_ads — {username}",
            token_expires_at=result.token_expires_at,
            data_access_expires_at=result.data_access_expires_at,
            scopes=list(result.granted_scopes),
        )
        if not connection_result.get("success"):
            logging.warning(
                "step-5 callback connection-persist failed for %r: %s",
                username,
                connection_result.get("error"),
            )
    except Exception as e:  # noqa: BLE001 — fail-soft; reconciliation still proceeds
        logging.warning(
            "step-5 callback connection-persist raised for %r: %s",
            username,
            e,
        )

    AuditLogService.log_event(
        username,
        "onboarding_step7_oauth_callback_ok",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(f"analytics_event=wizard.step7.oauth_callback_ok " f"granted_scopes={','.join(result.granted_scopes)}"),
    )
    return redirect(url_for("analytics.onboarding_step7_page"))


# ============================================
# ONBOARDING STEP 6 — ASSET SELECTION TABLE
# ============================================
# After the user reviews scopes on step 5 and completes the Meta
# OAuth roundtrip surfaces the multi-select table of ad
# accounts / pages / properties under their login. Selections are
# persisted to wizard_session.state.selected_assets so a refresh or
# OAuth-aware-resume sees the same rows checked.
#
# Scope-honest scaffold: discards the access token (token
# persistence is a deferred follow-up), so the WizardAssetCatalogService
# returns an empty list today. The route handles the empty catalog by
# rendering a "couldn't find any assets" empty-state hint with a
# refresh affordance. The Finish POST tolerates an empty selection
# (mirrors the prototype's "Skip — set up later" escape hatch) so a
# user landing on the empty state doesn't dead-end.


def _bridge_meta_assets(
    *,
    username: str,
    team_esid: str,
    connection_esid: str,
    accepted_ids: list,
    friendly_names: dict,
    workspace_esid: str | None = None,
) -> dict:
    """Defect A — bridge wizard step-8 selections to the FB importer.

    returns an outcome dict::

        {"wrote_any": bool, "written_accounts": [...],
         "shared_accounts": [{"fb_ad_account_id", "ad_account_esid",
                              "owner_team_esid"}],
         "probe_denied":  [{"fb_ad_account_id", "detail"}]}

    Previously this returned ``None`` whether it wrote every selected account or
    none of them, so the finisher's ``fb_bridge_ok`` flag only ever meant "did not
    raise" — a run that wired nothing was reported to the user as a success while
    ``ds_facebook`` stayed unwritten and the connector card stayed unconfigured
    (the prod failure behind nine silent retries on 2026-08-03).

    ``shared_accounts`` is INFORMATIONAL: accounts whose asset doc registers a
    different team. They ARE linked — sharing one Ad Account across teams is the
    expected Meta topology — and are only barred from becoming the legacy scalar
    ``fb_ad_account_esid``. Do not treat this list as a failure.

    ``probe_denied`` IS a failure reason: Meta positively refused ``ads_read`` for
    that account, so a link would only produce a silently-failing daily import.

    Writes ONE ``de_asset_fb_ad_accounts`` doc per accepted ad-account id
    (ownerless Phase 3 — attribution authority is the team
    doc's ``fb_ad_account_links``, never the account doc), then updates the
    user's team doc with the three account-wide fields the importer reads:

    * ``fb_import_mode = "account_wide"`` — the mode flag
      ``get_insights.py`` branches on.
    * ``fb_ad_account_esid`` — the asset doc id (the FIRST accepted id).
      This legacy scalar is a fallback for teams with no links; the
      authority is ``fb_ad_account_links`` below, which carries EVERY
      accepted account and all of which import.
    * ``connection_esid`` — the de_connection row whose
      ``oauth_token_enc`` the account-wide credential resolver decrypts.
    * ``ds_facebook = "true"`` — the existing on/off switch the
      ``get_fb_teams`` query filters on.
    * ``ds_instagram = "true"`` — the parallel switch the Instagram
      importer filters on. Only set when the connection's
      granted scopes include ``instagram_basic``; a partial-grant
      outcome that omitted the IG scopes is honoured (no IG-side
      walk attempted). ``instagram_basic`` is the minimum prereq —
      ``instagram_manage_insights`` is also needed for the metrics
      endpoint, but a token that has the former without the latter is
      a Meta-side anomaly we don't need to defend against here.

    Fail-soft: per-write exceptions are logged at WARNING; the wizard
    still redirects to the building screen. A failed write here means
    the importer simply won't see this team at run time — same outcome
    as the earlier status quo, so we're never worse off.
    """
    # every early exit reports "wired nothing" rather than a bare None,
    # so the finisher can never mistake one for a success (see the docstring).
    _nothing_wired: dict = {
        "wrote_any": False,
        "written_accounts": [],
        "shared_accounts": [],
        "probe_denied": [],
    }
    if not accepted_ids:
        return dict(_nothing_wired)

    try:
        from lib.es_helpers import get_elasticsearch_client as _get_es_6
    except Exception as e:  # noqa: BLE001
        logging.warning("bridge: get_elasticsearch_client import failed: %s", e)
        return dict(_nothing_wired)
    es = _get_es_6()
    if es is None:
        logging.warning("bridge: ES client unavailable for user %r", username)
        return dict(_nothing_wired)

    try:
        from import_scripts.facebook.lib.fb_helpers import (
            account_wide_owner_ok,
            strip_ad_account_prefix,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("bridge: fb_helpers import failed: %s", e)
        return dict(_nothing_wired)

    now_iso = datetime.now(timezone.utc).isoformat()
    first_asset_esid: str | None = None
    # every accepted-and-written account becomes an effective-dated
    # link on the team doc (the scalar pair below stays the importer contract
    # until Phase 2 and is derived from the first written account, unchanged).
    written_accounts: list = []
    # accounts whose asset doc registers a DIFFERENT team. Informational
    # only: they are linked normally (shared ad accounts are the expected topology)
    # and merely barred from becoming the legacy scalar back-link.
    shared_accounts: list = []
    # Accounts Meta positively refused ads_read on — a genuine
    # failure reason, since a link would only produce a failing daily import.
    probe_denied: list = []

    # Phase 3 — resolve the connection's token ONCE for the per-account
    # ads_read probe below. Fail-open: a missing/undecryptable token skips the
    # probe (the daily importer will surface a real credential problem anyway);
    # the probe must never block a legitimate link on a transient failure.
    probe_token = None
    try:
        from lib.encryption import decrypt as _decrypt

        _conn_src = (es.get(index="de_connection", id=connection_esid).get("_source")) or {}
        _token_enc = _conn_src.get("oauth_token_enc")
        if _token_enc:
            probe_token = _decrypt(_token_enc)
    except Exception as e:  # noqa: BLE001 — boundary; probe is advisory
        logging.warning(
            "bridge: could not resolve token for ads_read probe " "(connection=%s); links proceed unprobed: %s",
            connection_esid,
            e,
        )

    for provider_asset_id in accepted_ids:
        bare = strip_ad_account_prefix(str(provider_asset_id))
        title = (friendly_names or {}).get(provider_asset_id) or f"act_{bare}"
        # Phase 3 — the wizard NO LONGER writes team ownership onto
        # ad-account docs. Ownership authority is the team's effective-dated
        # fb_ad_account_links; the doc's
        # historical team_esid (where present) is informational only and is
        # retired in Phase 5. This is the root-cause fix for the
        # cross-claim: a re-auth can no longer capture sibling teams' accounts
        # because it writes nothing another team's import depends on.
        doc = {
            "fb_ad_account_id": bare,
            "fb_ad_account_title": title,
            "fb_ad_account_description": "",
            "created_by_wizard": True,
            "wizard_principal": username,
            "wizard_connection_esid": connection_esid,
            "@timestamp": now_iso,
        }
        # Phase 3 — ads_read probe (cross-BM case, one team): refuse
        # to link an account Meta POSITIVELY says this token cannot read —
        # the link would only produce a silently-failing daily import. Probe
        # errors other than a positive denial fail open.
        if probe_token:
            try:
                from asset_management.web.services.meta_graph_client import (
                    can_read_ad_account_insights,
                )

                allowed, probe_detail = can_read_ad_account_insights(bare, probe_token)
            except Exception as e:  # noqa: BLE001 — boundary; probe is advisory
                allowed, probe_detail = True, f"unverified: probe raised: {e}"
            if not allowed:
                logging.warning(
                    "bridge: NOT linking ad account %s for team %s — "
                    "the connection's token lacks ads_read on it (%s). "
                    "Cross-BM accounts need the authing login granted access "
                    "in Business Manager first.",
                    bare,
                    team_esid,
                    probe_detail,
                )
                # surface this to the finisher too (see shared_accounts).
                probe_denied.append({"fb_ad_account_id": bare, "detail": probe_detail or ""})
                continue
            if probe_detail:
                logging.info(
                    "bridge: ads_read probe inconclusive for account %s (%s); proceeding.",
                    bare,
                    probe_detail,
                )
        # stamp the team's workspace_esid so the ad-account doc is
        # visible to workspace-scoped operators. Sibling bridges (IG,
        # page) already stamp it; this bridge was the straggler, so
        # wizard-created ad-accounts were dropped fail-closed by the Asset
        # Explorer count + connector picker for non-superusers.
        if workspace_esid:
            doc["workspace_esid"] = workspace_esid
        # find-or-update on the natural key (fb_ad_account_id,
        # workspace_esid) instead of insert-fresh. Pre-fix every re-link wrote
        # a new doc with a random _id, accumulating duplicates that corrupted
        # any fb_ad_account_id -> esid resolution.
        # Mirrors the page bridge's cross-call dedup.
        existing_esid = _find_existing_ad_account(es, bare, workspace_esid)
        # a non-wizard (legacy-chain) doc for the same ad account is
        # deliberately NOT collapsed onto: the two docs carry the two token
        # paths (legacy fb_app_user vs wizard connection) and the daily
        # importer's page-filtered walk keys off the legacy doc's fb_app_esid,
        # so merging would double-emit the account in mixed-scope runs (see
        # the private docs 2026-07-12). The twin is sanctioned but must
        # never be silent — log it so tools/audit_fb_ad_account_twins.py runs
        # have a creation-time trail.
        legacy_twins = _find_legacy_ad_account_twins(es, bare, workspace_esid)
        if legacy_twins:
            logging.warning(
                "wizard link for ad account %s coexists with %d "
                "non-wizard asset doc(s) %s — twin by design (two token paths); "
                "not collapsing. Audit: tools/audit_fb_ad_account_twins.py",
                bare,
                len(legacy_twins),
                legacy_twins,
            )
        # the guard used to REFUSE the whole account here when
        # an existing wizard doc carried a different team's ``team_esid``. That was
        # a domain error: an Ad Account is a billing/execution container, not a
        # team-owned asset. Per the Meta model (a teammate's rules 6/7/9/10) a Business
        # Portfolio holds many Ad Accounts, each Ad belongs to exactly one Ad
        # Account AND exactly one Facebook Page, and Ads for the SAME Page can run
        # from DIFFERENT Ad Accounts. So two teams legitimately sharing one Ad
        # Account is the expected topology, not an anomaly — and prod confirms it
        #.
        #
        # The read side already reached this conclusion: Phase 2 made the
        # team's ``fb_ad_account_links`` the attribution authority and skips the
        # doc check entirely for link-based teams
        # (get_insights.py:884-886). Phase 3 made these docs ownerless on write —
        # note ``doc`` above carries no ``team_esid``, so the update below never
        # overwrote a foreign owner anyway. This guard was the last place still
        # treating the doc's ``team_esid`` as ownership, and refusing the write
        # blocked a legitimate share: the wizard could not configure Facebook at
        # all (prod, 2026-08-03 — nine retries, no data ever imported).
        #
        # What genuinely protects is narrower: the LEGACY SCALAR
        # ``fb_ad_account_esid`` back-link, which the importer still consults for
        # teams with no links. A sibling's re-auth must not make a foreign-owned
        # doc this team's scalar. So the account is linked normally, but is barred
        # from becoming ``first_asset_esid``.
        #
        # The one real uniqueness constraint in the domain is rule 1 — a Facebook
        # Page belongs to exactly ONE team — which lives on the page bridge, not
        # here. It is currently unenforced at write time.
        foreign_owned = False
        if existing_esid:
            try:
                existing_src = (
                    es.get(
                        index="de_asset_fb_ad_accounts",
                        id=existing_esid,
                    ).get("_source")
                    or {}
                )
            except Exception as e:  # noqa: BLE001 — boundary; fail-open to write path
                existing_src = {}
                logging.warning(
                    "bridge: could not read existing ad-account doc %s " "for ownership check (proceeding): %s",
                    existing_esid,
                    e,
                )
            if not account_wide_owner_ok(existing_src, team_esid):
                foreign_owned = True
                logging.info(
                    "ad account %s (esid=%s) carries team_esid=%s, not "
                    "team %s — linking anyway (shared ad accounts are the norm; the "
                    "team's fb_ad_account_links is the attribution authority per "
                    "). Barred from becoming the legacy scalar back-link.",
                    bare,
                    existing_esid,
                    existing_src.get("team_esid"),
                    team_esid,
                )
                shared_accounts.append(
                    {
                        "fb_ad_account_id": bare,
                        "ad_account_esid": existing_esid,
                        "owner_team_esid": existing_src.get("team_esid") or "",
                    }
                )
        try:
            if existing_esid:
                es.update(
                    index="de_asset_fb_ad_accounts",
                    id=existing_esid,
                    doc=doc,
                    refresh="wait_for",
                )
                esid = existing_esid
                logging.info(
                    "bridge: reused de_asset_fb_ad_accounts esid=%s "
                    "for user=%r team=%s ad_account=%s (re-link, no duplicate)",
                    esid,
                    username,
                    team_esid,
                    bare,
                )
            else:
                response = es.index(
                    index="de_asset_fb_ad_accounts",
                    document=doc,
                    refresh="wait_for",
                )
                esid = response.get("_id")
                logging.info(
                    "bridge: wrote de_asset_fb_ad_accounts esid=%s " "for user=%r team=%s ad_account=%s",
                    esid,
                    username,
                    team_esid,
                    bare,
                )
            if esid:
                written_accounts.append({"ad_account_esid": esid, "fb_ad_account_id": bare})
                # a foreign-owned doc still becomes a LINK (shared ad
                # accounts are legitimate) but must not become the legacy scalar
                # fb_ad_account_esid: that is the back-link the importer trusts for
                # teams WITHOUT links, and letting a sibling's re-auth claim it is
                # the corruption (one team claimed a sibling team's primary org, 2026-07-14).
                # This is the narrow, real part of the old guard.
                if first_asset_esid is None and not foreign_owned:
                    first_asset_esid = esid
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "bridge: de_asset_fb_ad_accounts write failed for " "user=%r ad_account=%s: %s",
                username,
                bare,
                e,
            )

    # gate on "did any account get written", NOT on the legacy scalar.
    # A team whose every selected account is shared (foreign-owned doc) writes real
    # links but deliberately no scalar, and that team IS configured: the importer's
    # link path is the authority and never reads the scalar.
    # Keying the bail-out on first_asset_esid would have kept exactly the users this
    # ticket is about broken.
    if not written_accounts:
        logging.warning(
            "bridge: no asset docs were written for user=%r team=%s; "
            "team flag NOT flipped (shared_accounts=%d probe_denied=%d)",
            username,
            team_esid,
            len(shared_accounts),
            len(probe_denied),
        )
        # Report the outcome instead of returning a bare None. The finisher's
        # fb_bridge_ok flag only ever meant "did not raise", so a run that wired
        # nothing was reported to the user as a SUCCESS while ds_facebook was never
        # written and the card stayed unconfigured. The caller can now say WHY.
        return {
            "wrote_any": False,
            "written_accounts": [],
            "shared_accounts": shared_accounts,
            "probe_denied": probe_denied,
        }

    # derive ds_instagram from the connection's granted scopes.
    # The Instagram importer (import_scripts/instagram/get_instagram_insights.py)
    # filters teams by ``ds_instagram == "true"``; without this flag the IG
    # step silently skips wizard-created teams even when the user has granted
    # the IG scopes. Reads the connection doc directly so the bridge
    # function signature stays stable (existing 3 release-plan
    # tests don't need updating). Fail-soft: any error reading scopes
    # falls back to "don't set ds_instagram" — the importer skip is the
    # same outcome as the earlier status quo.
    team_flag_doc = {
        "fb_import_mode": "account_wide",
        "connection_esid": connection_esid,
        "ds_facebook": "true",
    }
    # only write the legacy scalar when we have an eligible account.
    # Every selected account being shared (foreign-owned doc) leaves it None, and
    # writing None would clobber a previously-valid scalar with null. Omitting the
    # key leaves any existing value intact, which is the safer merge: the importer's
    # link path is the authority for this team either way.
    if first_asset_esid is not None:
        team_flag_doc["fb_ad_account_esid"] = first_asset_esid
    # Phase 1 — dual-write the effective-dated link list alongside the
    # scalars: close open links to now-unselected accounts, refresh/append links
    # for the selected ones. Fail-soft: if the team doc can't be read, the links
    # field is omitted from this update (scalars still land; the list is healed
    # by the next successful pass) rather than clobbered with a partial merge.
    try:
        team_resp = es.get(index="de_asset_teams", id=team_esid)
        existing_links = (team_resp.get("_source") or {}).get("fb_ad_account_links") or []
        team_flag_doc["fb_ad_account_links"] = _merge_team_ad_account_links(
            existing_links,
            written_accounts,
            connection_esid,
            username,
            datetime.now(timezone.utc).date().isoformat(),
            now_iso,
        )
    except Exception as e:  # noqa: BLE001 — boundary; scalar write must not be blocked
        logging.warning(
            "bridge: could not read team %s to merge fb_ad_account_links "
            "(scalars still written; links unchanged this pass): %s",
            team_esid,
            e,
        )
    try:
        conn_resp = es.get(index="de_connection", id=connection_esid)
        conn_scopes = (conn_resp.get("_source") or {}).get("scopes") or []
        if "instagram_basic" in conn_scopes:
            team_flag_doc["ds_instagram"] = "true"
            logging.info(
                "connection %s carries instagram_basic; setting ds_instagram=true on team %s",
                connection_esid,
                team_esid,
            )
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning(
            "could not read scopes on connection %s; skipping " "ds_instagram flag (Facebook side unaffected): %s",
            connection_esid,
            e,
        )

    try:
        es.update(
            index="de_asset_teams",
            id=team_esid,
            doc=team_flag_doc,
            refresh="wait_for",
        )
        logging.info(
            "bridge: flipped team %s to fb_import_mode=account_wide "
            "fb_ad_account_esid=%s connection_esid=%s ds_instagram=%s",
            team_esid,
            first_asset_esid,
            connection_esid,
            "true" if team_flag_doc.get("ds_instagram") == "true" else "false",
        )
    except Exception as e:  # noqa: BLE001
        logging.warning(
            "bridge: team flag update failed for team=%s: %s " "(asset doc landed but importer won't see it)",
            team_esid,
            e,
        )

    # partial outcomes matter too: some accounts may have been written
    # while others were refused, so the caller reports both.
    return {
        "wrote_any": True,
        "written_accounts": written_accounts,
        "shared_accounts": shared_accounts,
        "probe_denied": probe_denied,
    }


def _bridge_instagram_assets(
    *,
    username: str,
    team_esid: str,
    connection_esid: str,
    accepted_ids: list,
    workspace_esid: str | None = None,
    selected_pages: list | None = None,
) -> dict:
    """bridge wizard step-7 selections to the Instagram importer.

    Returns a outcome dict —
    ``{"ig_accounts_linked": int, "pages_walked": int,
    "pages_without_ig": [page names]}`` — so the Data Connectors wizard
    can tell the user WHICH selected pages yielded no Instagram account
    instead of silently returning a short list (a teammate's "pulled some but
    not all", one team, 2026-08-04). Every early exit returns the zero
    outcome; callers that predate the report (onboarding step 8) ignore
    the return value.

    Sibling to :func:`_bridge_meta_assets`. Discovers Instagram
    Business accounts linked to Facebook Pages under the user's accepted
    ad accounts, writes one ``de_asset_ig_accounts`` doc per discovered
    IG account, and stamps the new esids onto the team's
    ``team_instagram_profile_esids`` back-link.

    The Instagram importer
    (``import_scripts/instagram/get_instagram_insights.py``) is a
    two-stage filter:

    1. Stage 1 selects teams with ``ds_instagram="true"`` —
       made wizard teams selectable here.
    2. Stage 2 walks ``team_instagram_profile_esids`` and resolves
       each entry against ``de_asset_ig_accounts``.

    Wizard-provisioned teams used to fail stage 2 silently because no
    one wrote the IG-side asset chain. This bridge closes that gap.

    Gates on the connection's granted scopes — if ``instagram_basic``
    is missing (partial-grant outcome), short-circuits without any
    Graph calls or writes. Same gate uses to decide
    ``ds_instagram``; the two flags stay in lock-step.

    Fail-soft: per-Page Graph errors and per-doc write errors log at
    WARNING; the bridge continues processing remaining Pages. A
    complete failure (zero IG accounts discovered) leaves
    ``team_instagram_profile_esids`` unset — the importer's existing
    empty-profile-esids check drops the team cleanly with no error.

    ``TokenExpiredError`` from ``list_pages_on_ad_account`` propagates
    (mirrors posture); the route handler's outer try/except
    around this call demotes it to a wizard warning rather than a
    redirect.
    """
    # discovery outcome reported back to the Data Connectors
    # wizard. Every exit path returns it (a copy, so callers can't alias
    # the working lists).
    outcome: dict = {"ig_accounts_linked": 0, "pages_walked": 0, "pages_without_ig": []}

    if not accepted_ids:
        return dict(outcome)

    try:
        from lib.es_helpers import get_elasticsearch_client as _get_es_7
        from asset_management.web.services import meta_graph_client
        from asset_management.web.services.connection_service import ConnectionService
    except Exception as e:  # noqa: BLE001 — top-level import boundary
        logging.warning("bridge: import failed: %s", e)
        return dict(outcome)

    es = _get_es_7()
    if es is None:
        logging.warning("bridge: ES client unavailable for user %r", username)
        return dict(outcome)

    # Gate: read connection scopes; short-circuit unless instagram_basic
    # was granted. Mirrors the gate on _bridge_meta_assets
    # so a partial-grant user (FB scopes only) gets the same outcome
    # everywhere — ds_instagram unset and zero IG asset writes.
    try:
        conn_resp = es.get(index="de_connection", id=connection_esid)
        conn_scopes = (conn_resp.get("_source") or {}).get("scopes") or []
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning(
            "bridge: could not read scopes on connection %s; " "skipping IG bridge (Facebook side unaffected): %s",
            connection_esid,
            e,
        )
        return dict(outcome)

    if "instagram_basic" not in conn_scopes:
        logging.info(
            "bridge: connection %s lacks instagram_basic; skipping IG provisioning for team %s",
            connection_esid,
            team_esid,
        )
        return dict(outcome)

    # Resolve the OAuth token once — used for every Graph call below.
    access_token = ConnectionService.get_decrypted_token(connection_esid)
    if not access_token:
        logging.warning(
            "bridge: no decrypted token for connection %s; cannot walk Graph",
            connection_esid,
        )
        return dict(outcome)

    now_iso = datetime.now(timezone.utc).isoformat()
    discovered_esids: list[str] = []
    # Track seen IG account ids so a Page linked to the same IG under two
    # ad accounts doesn't produce duplicate asset rows. The importer reads
    # ESIDs from the team back-link, so deduping at write-time keeps
    # the back-link list clean.
    seen_ig_ids: set[str] = set()

    # iterate the Pages the user SELECTED, not the Pages
    # reachable through the accepted ad accounts.
    #
    # This replaces a walk over ``/act_<id>/promote_pages`` (the pages available
    # for PROMOTION through an ad account), which was the wrong input set in both
    # directions at once:
    #
    # TOO WIDE: an Ad Account is a billing container that teams
    # legitimately share (Meta rules 6/7/10), so its promotable pages belong to
    # SEVERAL teams. Before the wizard refused shared ad accounts, which
    # kept this narrow BY ACCIDENT; allowing them (correct) widened it, and a
    # re-auth then claimed sibling teams' profiles. Observed on prod 2026-08-04:
    # one operator's re-auth of team A claimed team B's IG profile — one selected
    # Page, two IG profiles.
    #
    # TOO NARROW: a page the user DID select is skipped entirely when
    # it is not promotable on the accepted ad account, so its Instagram profile is
    # never looked up. Observed the same day on one team: 5 pages selected, only the
    # 2 present in promote_pages got an IG lookup. The Meta admin confirmed one
    # skipped page's Instagram IS linked, and there was no log line for it at all —
    # because no request was ever made.
    #
    # An Instagram Profile attaches to exactly one Page, and a Page belongs to
    # exactly one Team (rules 9 -> 1), so the selected pages ARE the correct and
    # complete input. a teammate (who administers the Meta permissions) confirmed
    # ad-account access and Page access are granted SEPARATELY — the operational
    # reason an ad account can never stand in for a page list — and that teams do
    # select pages, so there is no ad-account fallback worth preserving.
    walk_pages = []
    for p in selected_pages or []:
        pid = str((p or {}).get("fb_page_id") or "").strip()
        if pid:
            walk_pages.append({"id": pid, "name": (p or {}).get("fb_page_name") or pid})

    if walk_pages:
        logging.info(
            "resolving Instagram profiles from %d selected page(s) for team %s",
            len(walk_pages),
            team_esid,
        )
    else:
        # ONBOARDING FALLBACK — deliberately retained, and NOT the Data Connectors
        # path. The onboarding step-8 flow (routes.py ~10864) has no page-selection
        # step at all: its sibling page bridge (_bridge_fb_pages) likewise
        # discovers pages from the accepted ad accounts. So `selected_pages=None`
        # cannot mean "claim nothing" without silently breaking IG provisioning
        # for every onboarding user.
        #
        # The Data Connectors wizard ALWAYS passes selected_pages (see
        # connector_wizard_service), so it never reaches this branch — which is
        # where over-claim missed pages both lived.
        # This fallback keeps the old, wider behavior only for the caller that has
        # no better input, and says so in the log rather than pretending the two
        # paths are equivalent.
        logging.info(
            "no page selection supplied for team %s — falling back to the pages "
            "promotable through the accepted ad account(s). This is the onboarding path; "
            "the Data Connectors wizard always supplies a selection. The fallback can reach "
            "pages owned by OTHER teams when an ad account is shared, so profiles it finds "
            "are attributed by page linkage, never claimed outright.",
            team_esid,
        )
        for ad_account_id in accepted_ids:
            try:
                for page in meta_graph_client.list_pages_on_ad_account(access_token, str(ad_account_id)):
                    if page.get("id"):
                        walk_pages.append({"id": page["id"], "name": page.get("name") or page["id"]})
            except meta_graph_client.TokenExpiredError:
                # Re-raise so the caller's outer try/except can surface re-OAuth —
                # same posture as list_ad_accounts.
                raise
            except Exception as e:  # noqa: BLE001 — per-account fail-soft
                logging.warning(
                    "bridge: list_pages_on_ad_account(%s) raised: %s",
                    ad_account_id,
                    e,
                )
                continue

    if not walk_pages:
        logging.info(
            "bridge: no pages to walk for team %s — no Instagram profiles claimed.",
            team_esid,
        )
        return dict(outcome)

    # pages whose IG lookup produced nothing, so the caller can tell
    # the user WHICH pages yielded no Instagram rather than silently returning a
    # short list. Being in the Business Portfolio is not the same as being
    # connected to a Page: the Graph edge we use
    # (/<page_id>?fields=instagram_business_account) returns only the account
    # linked to that page, so an existing-but-unlinked account resolves to None.
    # Aliased into the outcome so the wizard can render the same report.
    outcome["pages_walked"] = len(walk_pages)
    pages_without_ig: list[str] = outcome["pages_without_ig"]

    for page in walk_pages:
        page_id = page.get("id")
        page_name = page.get("name") or page_id
        if not page_id:
            continue
        try:
            ig_account = meta_graph_client.get_instagram_business_account(access_token, page_id)
        except meta_graph_client.TokenExpiredError:
            raise
        except Exception as e:  # noqa: BLE001 — per-page fail-soft
            logging.warning(
                "bridge: get_instagram_business_account(page=%s) raised: %s",
                page_id,
                e,
            )
            pages_without_ig.append(str(page_name))
            continue

        if not ig_account:
            # was a bare `continue` with the comment "silently skip
            # (common)". It IS common, but silence made it undiagnosable: a user
            # who selected five pages and got two Instagram accounts had no way
            # to tell which three yielded nothing, or why. a teammate hit exactly this
            # on one team (2026-08-04) and reported it as "pulled some but not all".
            #
            # Word it as NOT CONNECTED, not "does not exist" — the accounts may
            # well exist in the Business Portfolio. The Graph edge here
            # (/<page_id>?fields=instagram_business_account) returns only the
            # account linked to THAT page, so an existing-but-unlinked account
            # resolves to None. Telling the user it does not exist would send
            # them looking for something they can plainly see in Business Suite.
            logging.info(
                "no Instagram account is connected to page %s (%s) — skipping. "
                "The page may have no Instagram linked, or its Instagram account may exist "
                "in the Business Portfolio without being connected to this page (portfolio "
                "membership is not page linkage).",
                page_id,
                page_name,
            )
            pages_without_ig.append(str(page_name))
            continue

        ig_id = ig_account.get("id")
        if not ig_id or ig_id in seen_ig_ids:
            continue
        seen_ig_ids.add(ig_id)

        ig_username = ig_account.get("username") or ""
        ig_name = ig_account.get("name") or ig_username or ig_id
        # point 4 — the doc deliberately carries NO team_esid. It was
        # a write-only stamp that actively misled ownership resolution: on a
        # re-link the es.update below re-applied it wholesale, so a
        # re-auth by team B overwrote team A's stamp on team A's own doc (both
        # contested docs named the WRONG team because of this).
        # Ownership authority is linked_page_id → de_asset_teams.
        # team_facebook_page_id (Meta rules 9 → 1); the importer resolves via
        # team_instagram_profile_esids. Phase 3 made ad-account docs
        # ownerless for the same reason. The workspace-delete cascade,
        # the field's only reader, now purges IG docs via workspace_esid
        # (permissions_index_service step 3.5). Old docs keep their stale
        # stamp; nothing reads it for ownership.
        doc = {
            "ig_account_id": ig_id,
            "ig_account_username": ig_username,
            "ig_account_title": ig_name,
            "linked_page_id": page_id,
            "linked_page_title": page_name,
            "created_by_wizard": True,
            "wizard_principal": username,
            "wizard_connection_esid": connection_esid,
            "@timestamp": now_iso,
        }
        if workspace_esid:
            doc["workspace_esid"] = workspace_esid
        # find-or-update on (ig_account_id, workspace_esid)
        # instead of insert-fresh, so a wizard re-link reuses the existing
        # IG asset doc rather than orphaning it. Mirrors the ad-account /
        # page bridges' cross-call dedup.
        existing_ig_esid = _find_existing_ig_account(es, ig_id, workspace_esid)
        try:
            if existing_ig_esid:
                es.update(
                    index="de_asset_ig_accounts",
                    id=existing_ig_esid,
                    doc=doc,
                    refresh="wait_for",
                )
                esid = existing_ig_esid
                logging.info(
                    "bridge: reused de_asset_ig_accounts esid=%s for user=%r "
                    "team=%s ig=@%s (page=%s, re-link, no duplicate)",
                    esid,
                    username,
                    team_esid,
                    ig_username,
                    page_id,
                )
            else:
                resp = es.index(
                    index="de_asset_ig_accounts",
                    document=doc,
                    refresh="wait_for",
                )
                esid = resp.get("_id")
                logging.info(
                    "bridge: wrote de_asset_ig_accounts esid=%s for user=%r team=%s ig=@%s (page=%s)",
                    esid,
                    username,
                    team_esid,
                    ig_username,
                    page_id,
                )
            if esid:
                discovered_esids.append(esid)
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "bridge: de_asset_ig_accounts write failed for user=%r ig=%s: %s",
                username,
                ig_id,
                e,
            )

    # always state the discovery RESULT, not just the successes, so
    # "some but not all" is answerable from the log without re-running anything.
    if pages_without_ig:
        logging.info(
            "%d of %d selected page(s) for team %s yielded no Instagram account: %s. "
            "To import Instagram data for a page, connect an Instagram Business or Creator "
            "account to that page in Meta Business Suite — an account that only exists in the "
            "Business Portfolio is not connected to any page.",
            len(pages_without_ig),
            len(walk_pages),
            team_esid,
            ", ".join(pages_without_ig),
        )

    outcome["ig_accounts_linked"] = len(discovered_esids)

    if not discovered_esids:
        logging.info(
            "bridge: no Instagram Business accounts discovered for user=%r team=%s; " "team back-link not stamped",
            username,
            team_esid,
        )
        return dict(outcome)

    # Partial-doc update on de_asset_teams — mirrors M3 pin
    # so other team fields (team_title, organization_esid,
    # fb_import_mode) aren't clobbered.
    #
    # (2026-08-03) — ds_instagram is written HERE, in the same
    # partial doc as the back-link, not left to write in
    # _bridge_meta_assets. The importer needs BOTH fields
    # (get_instagram_insights.py stage 1 filters ds_instagram, stage 2
    # walks the esids), but the two writes used to live in separate
    # fail-soft functions with independent early returns — so 749 could
    # skip the flag (its scope read failed, or it bailed at "no ad-account
    # docs written") while this bridge still stamped the esids. Six prod
    # teams landed with esids and no ds_instagram key at all, silently
    # importing nothing. Writing both here makes the flag a consequence of
    # the back-link rather than a parallel write that can diverge; setting
    # it again when 749 already did is idempotent. This is es.update with a
    # partial doc (merge semantics), so naming ds_instagram cannot clobber
    # fb_import_mode / connection_esid / ds_facebook. Mirrors the shape
    # connector_wizard_service._bridge_ig_for_data_connectors_wizard
    # already uses.
    try:
        es.update(
            index="de_asset_teams",
            id=team_esid,
            doc={
                "team_instagram_profile_esids": discovered_esids,
                "ds_instagram": "true",
            },
            refresh="wait_for",
        )
        logging.info(
            "bridge: stamped team_instagram_profile_esids=%s and ds_instagram=true on team %s",
            discovered_esids,
            team_esid,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning(
            "bridge: team back-link update failed for team=%s: %s "
            "(IG asset docs landed but importer won't see them)",
            team_esid,
            e,
        )

    return dict(outcome)


def _bridge_fb_pages(
    *,
    username: str,
    team_esid: str,
    connection_esid: str,
    accepted_ids: list,
    workspace_esid: str | None = None,
    selected_pages: list | None = None,
) -> None:
    """bridge wizard step-7 selections to the Facebook Page importer.

    Sibling to :func:`_bridge_instagram_assets` (the IG side) and
    :func:`_bridge_meta_assets` (the FB ad-account side). Discovers
    Facebook Pages under the user's accepted ad accounts, writes one
    ``de_asset_fb_pages`` doc per Page, and stamps the discovered IDs
    onto the team's ``team_facebook_page_id`` + ``team_facebook_page_esids``
    back-links so the FB Page importer's stage-1 filter matches the
    wizard team.

    The FB Page importer
    (``import_scripts/facebook/get_page_insights.py``) reads:

    1. ``team_facebook_page_id`` on the team doc (raw Meta page IDs).
    2. ``de_asset_fb_pages`` rows keyed by ``fb_page_id`` for page details.

    Wizard-provisioned teams used to fail this lookup silently — no
    asset chain meant zero pages matched and the team was dropped. This
    bridge closes that gap; the companion half (importer auth resolver
    in ``get_page_insights.py``) lets the importer authenticate pages
    via the wizard's ``de_connection`` token instead of the legacy
    ``de_asset_fb_app_users`` chain.

    Gates on ``pages_read_engagement`` in the connection's granted
    scopes — partial-grant (no pages permission) short-circuits before
    any Graph call or write, mirroring ``instagram_basic`` gate.

    Dedup: skips any Page whose ``fb_page_id`` already has a doc in
    ``de_asset_fb_pages`` scoped to the same ``workspace_esid``. Prevents
    duplicate rows when the same user re-runs the wizard or has
    overlapping ad accounts that list the same Page.

    Fail-soft: per-account Graph errors and per-doc write errors log at
    WARNING and continue. Zero pages discovered leaves both team
    back-links unset — the importer's stage-1 filter drops the team
    cleanly with no error. ``TokenExpiredError`` from
    ``list_pages_on_ad_account`` propagates so the caller's wizard
    redirect can surface re-OAuth, same posture as.
    ``selected_pages`` is the EXPLICIT page choice from the Data
    Connectors wizard: a list of ``{"fb_page_id": ..., "fb_page_name": ...}``
    dicts. When present, the bridge links EXACTLY those pages (writes their
    de_asset_fb_pages docs + team back-links) instead of walking the ad
    account's pages. This fixes pre-fix the wizard discarded the
    user's selection and discovered pages by ad-account walk, which returned
    nothing when the chosen Page wasn't reachable via that ad account. The
    ad-account-walk discovery remains the default when no explicit selection is
    passed (the onboarding flow + the account_wide cohort). ``fb_import_mode``
    is left untouched — the page importer runs off ``ds_facebook`` + page
    ESIDs independently of the mode, so the team can import both ads (account
    wide) and these pages.
    """
    # Explicit-selection path can run without accepted_ids; the
    # discovery path still needs ad accounts to walk.
    selected_pages = selected_pages or []
    if not accepted_ids and not selected_pages:
        return

    try:
        from lib.es_helpers import get_elasticsearch_client as _get_es_9
        from asset_management.web.services import meta_graph_client
        from asset_management.web.services.connection_service import ConnectionService
    except Exception as e:  # noqa: BLE001 — top-level import boundary
        logging.warning("bridge: import failed: %s", e)
        return

    es = _get_es_9()
    if es is None:
        logging.warning("bridge: ES client unavailable for user %r", username)
        return

    # Gate: short-circuit unless pages_read_engagement was granted.
    # Same pattern as instagram_basic gate.
    try:
        conn_resp = es.get(index="de_connection", id=connection_esid)
        conn_scopes = (conn_resp.get("_source") or {}).get("scopes") or []
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning(
            "bridge: could not read scopes on connection %s; "
            "skipping FB Page bridge (Facebook ads side unaffected): %s",
            connection_esid,
            e,
        )
        return

    if "pages_read_engagement" not in conn_scopes:
        logging.info(
            "bridge: connection %s lacks pages_read_engagement; " "skipping FB Page provisioning for team %s",
            connection_esid,
            team_esid,
        )
        return

    access_token = ConnectionService.get_decrypted_token(connection_esid)
    if not access_token:
        logging.warning(
            "bridge: no decrypted token for connection %s; cannot walk Graph",
            connection_esid,
        )
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    discovered_esids: list[str] = []
    discovered_page_ids: list[str] = []
    # Track seen page ids so the same Page under two ad accounts doesn't
    # produce duplicate rows within this single bridge call.
    seen_page_ids: set[str] = set()

    # build the candidate (page_id, page_name, page_slug) list. If the
    # wizard passed an explicit ``selected_pages`` choice, link EXACTLY those (no
    # Graph walk — the user already picked from a list the wizard fetched).
    # Otherwise fall back to discovering pages by walking each ad account.
    # ``page_slug`` is the team-form chip's display field (fb_page_slug); the
    # wizard's /me/accounts listing carries it as ``username``, the ad-account
    # walk (promote_pages) does not, so the discovery path falls back to name.
    candidates: list[tuple[str, str, str]] = []
    if selected_pages:
        for p in selected_pages:
            pid = str(p.get("fb_page_id") or "").strip()
            if not pid:
                continue
            name = p.get("fb_page_name") or pid
            slug = p.get("fb_page_slug") or name
            candidates.append((pid, name, slug))
        logging.info(
            "bridge:  explicit page selection — linking %d chosen page(s) for team %s",
            len(candidates),
            team_esid,
        )
    else:
        for ad_account_id in accepted_ids:
            try:
                pages = meta_graph_client.list_pages_on_ad_account(access_token, str(ad_account_id))
            except meta_graph_client.TokenExpiredError:
                # Re-raise so the caller's outer try/except can surface
                # re-OAuth — same posture as.
                raise
            except Exception as e:  # noqa: BLE001 — per-account fail-soft
                logging.warning(
                    "bridge: list_pages_on_ad_account(%s) raised: %s",
                    ad_account_id,
                    e,
                )
                continue
            for page in pages:
                pid = page.get("id")
                if not pid:
                    continue
                nm = page.get("name") or str(pid)
                # promote_pages returns id+name only — slug falls back to name.
                candidates.append((str(pid), nm, page.get("username") or nm))

    if not selected_pages and candidates:
        # the ad-account walk lists every Page the account promotes,
        # and one ad account serves Pages of different teams (Meta rules 6 and
        # 10), so discovery must not claim a Page another workspace's team
        # already owns. Skip those (decline and report) rather than refusing
        # the whole finish: a shared ad account is normal. An
        # explicit selection is refused upstream in fb_finish_data_connectors_wizard.
        from asset_management.web.services.connector_wizard_service import (
            WizardError,
            pages_linked_in_other_workspaces,
        )

        try:
            foreign = pages_linked_in_other_workspaces([c[0] for c in candidates], team_esid, workspace_esid)
        except WizardError:
            foreign = {c[0] for c in candidates}
            logging.warning(
                "bridge: page-ownership lookup failed; linking no discovered pages for team %s", team_esid
            )
        if foreign:
            logging.warning(
                "bridge: skipped %d discovered page(s) owned by another workspace for team %s: %s",
                len(foreign),
                team_esid,
                sorted(foreign),
            )
            candidates = [c for c in candidates if c[0] not in foreign]

    for page_id_str, page_name, page_slug in candidates:
        if page_id_str in seen_page_ids:
            continue
        seen_page_ids.add(page_id_str)

        # Cross-call dedup: skip if a de_asset_fb_pages row for this
        # (fb_page_id, workspace_esid) already exists (e.g. wizard
        # re-run for the same team, or a previous wizard signup in
        # the same workspace). Cross-workspace duplicates are
        # allowed — ESID-keyed indices isolate by team
        # so the same fb_page_id in two workspaces won't collide
        # at import time.
        existing_esid = _find_existing_page(es, page_id_str, workspace_esid)
        if existing_esid:
            discovered_esids.append(existing_esid)
            discovered_page_ids.append(page_id_str)
            # re-stamp the wizard link on the EXISTING doc. The
            # dedup above prevents a duplicate ROW, but the whole point of a
            # re-auth is to migrate that existing page onto the new
            # connection: the importer picks wizard-vs-legacy auth per page
            # off wizard_connection_esid (get_page_insights.py), so an
            # already-onboarded legacy page that we merely reuse-without-
            # updating stays on the legacy app forever. Partial-doc update
            # (mirrors the de_asset_fb_ad_accounts / de_asset_teams updates
            # in this module) so we set only the three wizard fields and
            # leave fb_page_slug / fb_account_esid / workspace_esid intact.
            try:
                es.update(
                    index="de_asset_fb_pages",
                    id=existing_esid,
                    doc={
                        "wizard_connection_esid": connection_esid,
                        "created_by_wizard": True,
                        "wizard_principal": username,
                    },
                    refresh="wait_for",
                )
                logging.info(
                    "bridge: re-stamped wizard_connection_esid=%s on existing "
                    "de_asset_fb_pages esid=%s for fb_page_id=%s in workspace=%s",
                    connection_esid,
                    existing_esid,
                    page_id_str,
                    workspace_esid,
                )
            except Exception as e:  # noqa: BLE001 — fail-soft, mirror per-doc write
                logging.warning(
                    "bridge: re-stamp failed for existing esid=%s fb_page_id=%s: %s",
                    existing_esid,
                    page_id_str,
                    e,
                )
            continue

        doc = {
            "fb_page_id": page_id_str,
            "fb_page_name": page_name,
            # fb_page_slug is the team-form chip's display field and
            # the facebook_pages asset's title_field; without it the chip + the
            # Asset Explorer row render blank. Falls back to name when the
            # source had no slug.
            "fb_page_slug": page_slug,
            "team_esid": team_esid,
            "created_by_wizard": True,
            "wizard_principal": username,
            "wizard_connection_esid": connection_esid,
            "@timestamp": now_iso,
        }
        if workspace_esid:
            doc["workspace_esid"] = workspace_esid
        try:
            resp = es.index(
                index="de_asset_fb_pages",
                document=doc,
                refresh="wait_for",
            )
            esid = resp.get("_id")
            if esid:
                discovered_esids.append(esid)
                discovered_page_ids.append(page_id_str)
            logging.info(
                "bridge: wrote de_asset_fb_pages esid=%s for user=%r team=%s fb_page_id=%s (%s)",
                esid,
                username,
                team_esid,
                page_id_str,
                page_name,
            )
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "bridge: de_asset_fb_pages write failed for user=%r fb_page_id=%s: %s",
                username,
                page_id_str,
                e,
            )

    if not discovered_esids:
        logging.info(
            "bridge: no Facebook Pages discovered for user=%r team=%s; " "team back-links not stamped",
            username,
            team_esid,
        )
        return

    # Partial-doc update on de_asset_teams. Mirror
    # partial-doc semantics so other team fields (team_title,
    # organization_esid, ds_facebook, fb_import_mode, ds_instagram,
    # team_instagram_profile_esids) aren't clobbered. We stamp BOTH
    # team_facebook_page_id (raw Meta IDs — what the importer's stage-1
    # filter reads) AND team_facebook_page_esids (ESID-keyed back-link
    # the connector card + asset editor read).
    try:
        es.update(
            index="de_asset_teams",
            id=team_esid,
            doc={
                "team_facebook_page_id": discovered_page_ids,
                "team_facebook_page_esids": discovered_esids,
            },
            refresh="wait_for",
        )
        logging.info(
            "bridge: stamped team_facebook_page_id=%s + team_facebook_page_esids=%s on team %s",
            discovered_page_ids,
            discovered_esids,
            team_esid,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning(
            "bridge: team back-link update failed for team=%s: %s "
            "(FB Page asset docs landed but importer won't see them)",
            team_esid,
            e,
        )


def _merge_team_ad_account_links(
    existing_links: list,
    written_accounts: list,
    connection_esid: str,
    username: str,
    today_iso: str,
    now_iso: str,
) -> list:
    """Phase 1 — merge a wizard link event into a team's
    effective-dated ``fb_ad_account_links`` list. Pure (no ES) so the
    release-plan tests exercise the close/append rules directly.

    ``written_accounts`` is the bridge's accepted-and-written set:
    ``[{"ad_account_esid": ..., "fb_ad_account_id": ...}, ...]`` — only
    accounts that passed the ownership guard and landed as asset
    docs, i.e. explicit user selections, never mere enumeration.

    Rules (design: the private docs):
    * OPEN link (no ``end_date``) to an account NOT in this selection →
      CLOSED (``end_date`` = today). History is never deleted.
    * OPEN link to a selected account → kept, but its ``connection_esid``
      is refreshed to the authing connection (a re-auth may rotate the
      credential; the token must always match the account).
    * Selected account with no open link → new open link appended with
      ``start_date`` = today, ``source`` = "wizard".
    * ENDED links are never touched — they are the historical record.
    """
    selected = {w["ad_account_esid"]: w for w in written_accounts if w.get("ad_account_esid")}
    merged: list = []
    open_covered: set = set()

    for link in existing_links or []:
        link = dict(link)
        is_open = not link.get("end_date")
        esid = link.get("ad_account_esid")
        if is_open and esid in selected:
            if link.get("connection_esid") != connection_esid:
                link["connection_esid"] = connection_esid
                link["updated_by"] = username
                link["updated_at"] = now_iso
            open_covered.add(esid)
        elif is_open:
            link["end_date"] = today_iso
            link["updated_by"] = username
            link["updated_at"] = now_iso
        merged.append(link)

    for esid, written in selected.items():
        if esid in open_covered:
            continue
        merged.append(
            {
                "ad_account_esid": esid,
                "fb_ad_account_id": written.get("fb_ad_account_id"),
                "connection_esid": connection_esid,
                "start_date": today_iso,
                "source": "wizard",
                "updated_by": username,
                "updated_at": now_iso,
            }
        )
    return merged


def _find_existing_ad_account(es, fb_ad_account_id: str, workspace_esid: str | None) -> str | None:
    """Return the esid of an existing wizard-created ``de_asset_fb_ad_accounts``
    row matching ``(fb_ad_account_id, workspace_esid)`` or ``None`` if no
    match. Used by the bridge to reuse an existing doc on re-link
    instead of inserting a fresh one with a random ``_id``.

    pre-fix the bridge did a plain ``es.index`` on every pass, so
    re-linking the same ad account accumulated duplicate asset docs (each a
    different esid, all the same ``fb_ad_account_id``). Mirrors
    :func:`_find_existing_page` (the page bridge's cross-call dedup).

    Scoped to ``created_by_wizard == true`` so a wizard re-link never collapses
    onto a hand-created / importer-created / legacy ad-account doc — same scope
    as the ``tools/dedupe_wizard_fb_ad_accounts.py`` cleanup. When multiple
    wizard dupes already exist (pre-fix backlog), the latest ``@timestamp``
    survivor is chosen — the same keep-rule the dedupe tool applies — so the
    write-time guard and the cleanup tool agree on which doc wins.

    Cross-workspace duplicates are allowed (two orgs may each link the same ad
    account); per-team ESID isolation keeps them from colliding at
    import time.

    NOTE on the field name: ``fb_ad_account_id`` is mapped as a PURE keyword
    (``update_v1``/``update_v4``), so it has NO ``.keyword`` sub-field — a
    ``term`` on ``fb_ad_account_id.keyword`` matches nothing and the dedup
    silently degrades to insert-fresh (caught by staging smoke 2026-06-25; the
    same prod/staging mapping-divergence class as). Query the
    BARE field — and since the reindex ALL de_asset_* string fields are
    explicit keyword, so ``workspace_esid`` (and the sibling helpers' page/IG
    ids) are bare too; their old ``.keyword`` subfields no longer exist.
    """
    must_clauses: list = [
        {"term": {"fb_ad_account_id": fb_ad_account_id}},
        {"term": {"created_by_wizard": True}},
    ]
    if workspace_esid:
        must_clauses.append({"term": {"workspace_esid": workspace_esid}})
    try:
        resp = es.search(
            index="de_asset_fb_ad_accounts",
            size=1,
            body={
                "query": {"bool": {"must": must_clauses}},
                "sort": [{"@timestamp": {"order": "desc", "missing": "_last"}}],
                "_source": False,
            },
            ignore_unavailable=True,
        )
    except Exception as e:  # noqa: BLE001 — boundary, fail-soft
        logging.warning(
            "bridge: dedup lookup for fb_ad_account_id=%s workspace=%s failed: %s",
            fb_ad_account_id,
            workspace_esid,
            e,
        )
        return None
    hits = resp.get("hits", {}).get("hits", []) or []
    return hits[0].get("_id") if hits else None


def _find_legacy_ad_account_twins(es, fb_ad_account_id: str, workspace_esid: str | None) -> list[str]:
    """Return esids of NON-wizard ``de_asset_fb_ad_accounts`` docs for the same
    ``fb_ad_account_id`` in the connecting workspace (or with no workspace
    stamp at all — legacy docs created after the last backfill run
    carry none).

    the bridge deliberately keeps a wizard doc SEPARATE
    from a legacy-chain doc for the same Meta ad account (two token paths;
    merging would put the account in both the page-filtered walk and the
    account-wide entry set of one daily run). This helper only makes the twin
    VISIBLE at creation time; it never selects a reuse target — that stays
    ``_find_existing_ad_account``'s wizard-scoped job.

    Docs whose ``workspace_esid`` is set and differs from the connecting
    team's are excluded: cross-workspace duplicates are allowed by design
     and are not twins. Fail-soft: any lookup error returns ``[]``
    (the bridge write must never be blocked by an advisory query).
    """
    try:
        resp = es.search(
            index="de_asset_fb_ad_accounts",
            size=25,
            body={
                "query": {
                    "bool": {
                        # bare keyword field (no.keyword sub-field).
                        "must": [{"term": {"fb_ad_account_id": fb_ad_account_id}}],
                        "must_not": [{"term": {"created_by_wizard": True}}],
                    }
                },
                "_source": ["workspace_esid"],
            },
            ignore_unavailable=True,
        )
    except Exception as e:  # noqa: BLE001 — boundary, fail-soft (advisory only)
        logging.warning(
            "legacy-twin lookup for fb_ad_account_id=%s failed (advisory only): %s",
            fb_ad_account_id,
            e,
        )
        return []
    twins: list[str] = []
    for hit in resp.get("hits", {}).get("hits", []) or []:
        doc_ws = (hit.get("_source") or {}).get("workspace_esid") or ""
        if workspace_esid and doc_ws and doc_ws != workspace_esid:
            continue  # another workspace's link — allowed, not a twin
        twins.append(hit.get("_id"))
    return twins


def _find_existing_ig_account(es, ig_account_id: str, workspace_esid: str | None) -> str | None:
    """Return the esid of an existing wizard-created ``de_asset_ig_accounts``
    row matching ``(ig_account_id, workspace_esid)`` or ``None`` if no match.

    sibling of :func:`_find_existing_ad_account` for the
    IG bridge. Pre-fix the bridge ``es.index``-ed a fresh
    ``de_asset_ig_accounts`` doc on every re-link; the team back-link was
    re-stamped to only the latest esids, so prior docs were orphaned and
    accumulated. Reusing the existing doc on re-link keeps the index clean.
    Same ``created_by_wizard``-scoped, latest-``@timestamp`` keep-rule.
    """
    must_clauses: list = [
        # bare fields (explicit keyword post-reindex).
        {"term": {"ig_account_id": ig_account_id}},
        {"term": {"created_by_wizard": True}},
    ]
    if workspace_esid:
        must_clauses.append({"term": {"workspace_esid": workspace_esid}})
    try:
        resp = es.search(
            index="de_asset_ig_accounts",
            size=1,
            body={
                "query": {"bool": {"must": must_clauses}},
                "sort": [{"@timestamp": {"order": "desc", "missing": "_last"}}],
                "_source": False,
            },
            ignore_unavailable=True,
        )
    except Exception as e:  # noqa: BLE001 — boundary, fail-soft
        logging.warning(
            "bridge: IG dedup lookup for ig_account_id=%s workspace=%s failed: %s",
            ig_account_id,
            workspace_esid,
            e,
        )
        return None
    hits = resp.get("hits", {}).get("hits", []) or []
    return hits[0].get("_id") if hits else None


def _find_existing_page(es, fb_page_id: str, workspace_esid: str | None) -> str | None:
    """Return the esid of an existing ``de_asset_fb_pages`` row matching
    ``(fb_page_id, workspace_esid)`` or ``None`` if no match. Used by
    the bridge to skip duplicate writes on wizard re-run.
    """
    # bare fields (explicit keyword post-reindex).
    must_clauses: list = [{"term": {"fb_page_id": fb_page_id}}]
    if workspace_esid:
        must_clauses.append({"term": {"workspace_esid": workspace_esid}})
    try:
        resp = es.search(
            index="de_asset_fb_pages",
            size=1,
            body={"query": {"bool": {"must": must_clauses}}, "_source": False},
            ignore_unavailable=True,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning(
            "bridge: dedup lookup for fb_page_id=%s workspace=%s failed: %s",
            fb_page_id,
            workspace_esid,
            e,
        )
        return None
    hits = resp.get("hits", {}).get("hits", []) or []
    return hits[0].get("_id") if hits else None


def _resolve_primary_team_esid(username: str) -> str | None:
    """Resolve the team the wizard should scope this user's connector to.

    this used to query ``de_authz_team_membership`` for an
    ``is_primary=True`` row. That index has not been written since
     Phase 3b retired membership docs:
    ``OnboardingService._upsert_team_membership`` is a deliberate no-op
    (asserted by ``the release-plan tests``).
    So for every account created after P3b the lookup returned ``None``
    unconditionally — not an indexing race, a permanently empty index.
    Step-6 read that as "no team" and dead-ended Google Ads, GA4 and
    YouTube; Meta was unaffected only because its OAuth path never
    resolves a team. Retrying could never have helped.

    The replacement follows the ladder that and
     already standardised on:

      1. ``WorkspaceMembershipService.resolve_curation_workspace`` for
         the workspace (is_primary role -> workspace data grant ->
         ORG-OWNER org-walk -> positional fallback).
      2. The user's OWN ``de_authz_user_team_role`` rows, narrowed to
         teams under that workspace via ``de_asset_teams``.

    The role index is the reliable anchor: Defect B makes every
    wizard provision path write a ROLE_OWNER team-role row for the team
    it just created (``onboarding_service.py`` :281, :383, :600).

    Ambiguity fails CLOSED. An agency owner accumulates one ROLE_OWNER
    team row per client (``add_client_to_workspace``), so >1 candidate
    is reachable outside the wizard. Rather than sort and pick — the
    alphabetical-accident failure is open on at workspace tier
    — this logs a WARN naming the candidates and returns ``None``, which
    the callers surface as an explicit step-6 banner. Step-6 itself runs
    immediately after provisioning, before any client team exists, so
    the wizard path resolves a single candidate.
    """
    if not username:
        return None
    try:
        from lib.es_helpers import get_elasticsearch_client
        from asset_management.web.services.permissions_index_service import (
            USER_TEAM_ROLE_INDEX,
        )
        from asset_management.web.services.workspace_membership_service import (
            WorkspaceMembershipService,
        )
    except Exception as e:  # noqa: BLE001 — import failure must not 500 the wizard
        logging.warning("primary-team resolver imports failed for %r: %s", username, e)
        return None

    es = get_elasticsearch_client()
    if es is None:
        return None

    try:
        workspace_esid = WorkspaceMembershipService.resolve_curation_workspace(username)
    except Exception as e:  # noqa: BLE001 — see docstring: no-answer beats a wrong answer
        logging.warning("curation-workspace lookup failed for %r: %s", username, e)
        return None
    if not workspace_esid:
        logging.warning(
            "no curation workspace for %r; cannot resolve a wizard team",
            username,
        )
        return None

    try:
        resp = es.search(
            index=USER_TEAM_ROLE_INDEX,
            body={
                "size": 100,
                "query": {"bool": {"filter": [{"term": {"username": username}}]}},
            },
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("team-role lookup failed for %r: %s", username, e)
        return None

    candidates = []
    for hit in (resp.get("hits") or {}).get("hits") or []:
        team_esid = (hit.get("_source") or {}).get("team_esid")
        if team_esid and team_esid not in candidates:
            candidates.append(team_esid)
    if not candidates:
        logging.warning(
            "%r holds no de_authz_user_team_role rows; cannot resolve a wizard team",
            username,
        )
        return None

    # Narrow to teams that actually live under the resolved workspace.
    # reindexed ``de_asset_teams.workspace_esid`` to keyword, so
    # the bare field is the correct term target here.
    scoped = _filter_team_esids_to_workspace(es, candidates, workspace_esid)
    if not scoped:
        logging.warning(
            "none of %r's teams %r are under workspace %r; cannot resolve a wizard team",
            username,
            candidates,
            workspace_esid,
        )
        return None
    if len(scoped) > 1:
        # See docstring: fail closed rather than pick. tracks the
        # equivalent ambiguity at workspace tier.
        logging.warning(
            "ambiguous wizard team for %r under workspace %r: %r — refusing to guess",
            username,
            workspace_esid,
            sorted(scoped),
        )
        return None
    return scoped[0]


def _filter_team_esids_to_workspace(es, team_esids: list[str], workspace_esid: str) -> list[str]:
    """keep only ``team_esids`` whose team doc sits under
    ``workspace_esid``. Returns ``[]`` on any lookup failure so the
    caller fails closed rather than scoping a connector to a team in
    another tenant.
    """
    from asset_management.web.services.connector_wizard_service import TEAMS_INDEX

    try:
        resp = es.search(
            index=TEAMS_INDEX,
            body={
                "size": len(team_esids),
                "query": {
                    "bool": {
                        "filter": [
                            {"terms": {"_id": team_esids}},
                            {"term": {"workspace_esid": workspace_esid}},
                        ]
                    }
                },
            },
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("team/workspace narrowing failed for %r: %s", workspace_esid, e)
        return []
    return [hit["_id"] for hit in (resp.get("hits") or {}).get("hits") or [] if hit.get("_id")]


def _apply_tokenless_connector_for_team(
    *,
    username: str,
    selected_connector_id: str,
    account_type: str,
    team_esid: str,
    audit_event_prefix: str,
    audit_event_namespace: str,
    existing_connection_esid: str | None = None,
) -> dict:
    """connector-write core, factored out of step-6 finish.

    Writes the tokenless connection + asset rows, materialises the
    first-look pack, and kicks off the on-demand backfill — for a
    *caller-supplied* ``team_esid``. No wizard_session reads or writes,
    no Flask redirects.

    Two callers:

    * onboarding step-4 ``_finalize_tokenless_connector`` (wraps with
      primary-team resolution + wizard_session bookkeeping +
      Flask redirect — keeps behaviour);
    * data-connectors API ``api_wizard_google_ads_configure`` /
      ``api_wizard_ga4_configure``. Each
      updates the team doc first (asset link + ds_* flag) then
      calls this core to write de_connection + de_connection_asset
      rows. No Flask redirect — returns the dict for a JSON envelope.

    ``audit_event_prefix`` + ``audit_event_namespace`` namespace the
    audit-log events so the two call sites are distinguishable in
    the audit index. The onboarding wrapper passes
    ``"onboarding_step6"`` / ``"wizard.step6"`` to preserve the
    canonical ``onboarding_step6_tokenless_finalize`` event; the
    data-connectors API passes ``"data_connectors"`` /
    ``"data_connectors"``.

    Returns a dict shaped:

      ``{"success": bool, "connection_esid": str|None,
         "provider_asset_ids": list[str], "asset_count": int,
         "first_look_status": str, "first_look_id": str|None,
         "kickoff_ok": bool, "error": str|None, "error_kind": str|None}``

    ``error_kind`` is one of ``unmapped_connector`` / ``no_team`` /
    ``no_provider_asset`` / ``connection_write_failed``; the API caller
    maps it to a user-facing message.

    ``existing_connection_esid`` supports connectors whose
    connection was already written by the caller (YouTube: configure_youtube's
     real-principal de_connection + de_connection_asset rows +
    team-doc stamp). When provided, the tokenless connection write is
    skipped entirely and the first-look materialise + on-demand kickoff
    run against the caller's connection. YouTube REQUIRES it — it is a
    per-user-OAuth connector, so minting a tokenless row here would
    duplicate the connection under a second natural key; without it the
    finalize degrades to the earlier no-kickoff behaviour.
    """
    from asset_management.web.services.connection_service import (
        ASSET_TYPE_GA4_PROPERTY,
        ASSET_TYPE_GOOGLE_ADS_CUSTOMER,
        ASSET_TYPE_YOUTUBE_CHANNEL,
        ConnectionService,
        PROVIDER_GA4,
        PROVIDER_GOOGLE_ADS,
        PROVIDER_YOUTUBE,
    )
    from asset_management.web.services.onboarding_branching_service import (
        OnboardingBranchingService,
    )

    result: dict = {
        "success": False,
        "connection_esid": None,
        "provider_asset_ids": [],
        "asset_count": 0,
        "first_look_status": "skipped",
        "first_look_id": None,
        "kickoff_ok": False,
        "error": None,
        "error_kind": None,
    }

    connector_to_provider = {
        "google_ads": (PROVIDER_GOOGLE_ADS, ASSET_TYPE_GOOGLE_ADS_CUSTOMER),
        "google_analytics_4": (PROVIDER_GA4, ASSET_TYPE_GA4_PROPERTY),
        "youtube": (PROVIDER_YOUTUBE, ASSET_TYPE_YOUTUBE_CHANNEL),
    }
    mapping = connector_to_provider.get(selected_connector_id)
    if mapping is None:
        logging.warning(
            "_apply_tokenless_connector_for_team called for unmapped connector %r",
            selected_connector_id,
        )
        result["error"] = f"Unsupported connector: {selected_connector_id}"
        result["error_kind"] = "unmapped_connector"
        return result
    provider, asset_type = mapping

    if not team_esid:
        AuditLogService.log_event(
            username,
            f"{audit_event_prefix}_tokenless_finalize_no_team",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"analytics_event={audit_event_namespace}.tokenless_finalize_no_team " f"connector_id={selected_connector_id}"
            ),
        )
        result["error"] = "No team selected."
        result["error_kind"] = "no_team"
        return result

    if selected_connector_id == "google_ads":
        google_ads_customer_id = _resolve_team_to_google_ads_customer_id(team_esid)
        if not google_ads_customer_id:
            logging.warning(
                "tokenless apply skipped for %r: team %r has no google_ads_customer_esid configured",
                username,
                team_esid,
            )
            AuditLogService.log_event(
                username,
                f"{audit_event_prefix}_tokenless_finalize_no_provider_asset",
                False,
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent", ""),
                detail=(
                    f"analytics_event={audit_event_namespace}.tokenless_finalize_no_provider_asset "
                    f"connector_id={selected_connector_id} "
                    f"team_esid={team_esid}"
                ),
            )
            result["error"] = (
                "This team has no Google Ads customer configured. "
                "Add a Google Ads Customer asset and link it on the team page first."
            )
            result["error_kind"] = "no_provider_asset"
            return result
        provider_account_id = google_ads_customer_id
        provider_asset_ids = [google_ads_customer_id]
    elif selected_connector_id == "google_analytics_4":
        ga_property_ids = _resolve_team_to_ga_property_ids(team_esid)
        if not ga_property_ids:
            logging.warning(
                "tokenless apply skipped for %r: team %r has no team_ga4_property_esids configured",
                username,
                team_esid,
            )
            AuditLogService.log_event(
                username,
                f"{audit_event_prefix}_tokenless_finalize_no_provider_asset",
                False,
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent", ""),
                detail=(
                    f"analytics_event={audit_event_namespace}.tokenless_finalize_no_provider_asset "
                    f"connector_id={selected_connector_id} "
                    f"team_esid={team_esid}"
                ),
            )
            result["error"] = (
                "This team has no GA4 properties configured. " "Link at least one GA Property on the team page first."
            )
            result["error_kind"] = "no_provider_asset"
            return result
        provider_account_id = team_esid
        provider_asset_ids = ga_property_ids
    elif selected_connector_id == "youtube":
        # the team's linked channels (written by configure_youtube
        # just before this runs on both YouTube call sites).
        youtube_channel_ids = _resolve_team_to_youtube_channel_ids(team_esid)
        if not youtube_channel_ids:
            logging.warning(
                "tokenless apply skipped for %r: team %r has no YouTube channels linked",
                username,
                team_esid,
            )
            AuditLogService.log_event(
                username,
                f"{audit_event_prefix}_tokenless_finalize_no_provider_asset",
                False,
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent", ""),
                detail=(
                    f"analytics_event={audit_event_namespace}.tokenless_finalize_no_provider_asset "
                    f"connector_id={selected_connector_id} "
                    f"team_esid={team_esid}"
                ),
            )
            result["error"] = "This team has no YouTube channels linked. " "Connect a channel via the YouTube wizard first."
            result["error_kind"] = "no_provider_asset"
            return result
        provider_account_id = team_esid
        provider_asset_ids = youtube_channel_ids
    else:
        # Default for future team-scoped tokenless connectors.
        provider_account_id = team_esid
        provider_asset_ids = [team_esid]

    defaults = OnboardingBranchingService.for_account_type(account_type)
    org_shared = defaults.step6_org_shared_default

    connection_esid: str | None = None
    if existing_connection_esid:
        # the caller already wrote the connection (YouTube:
        # configure_youtube's write covered the de_connection +
        # de_connection_asset rows + youtube_connection_esid team stamp).
        # Minting a tokenless row here would duplicate it under a second
        # natural key, so skip straight to first-look + kickoff.
        connection_esid = existing_connection_esid
    elif selected_connector_id == "youtube":
        # YouTube is per-user OAuth, never tokenless. Reaching
        # here without a connection means configure_youtube's fail-soft
        # connection write failed; leave connection_esid None so first-look
        # + kickoff skip and the caller's fail-soft posture applies
        # (earlier behaviour: link landed, cron catches up).
        logging.warning(
            "tokenless apply: no existing YouTube connection for team %r; " "skipping first-look + kickoff",
            team_esid,
        )
    else:
        try:
            connection_result = ConnectionService.create_tokenless_connection(
                principal=username,
                provider=provider,
                provider_account_id=provider_account_id,
                label=f"{selected_connector_id} — {username}",
                org_shared=bool(org_shared),
            )
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning("create_tokenless_connection raised for %r: %s", username, e)
            connection_result = {"success": False, "error": "Could not create the connection."}

        if connection_result.get("success"):
            connection_esid = connection_result.get("esid")
            for asset_id in provider_asset_ids:
                try:
                    ConnectionService.upsert_connection_asset(
                        connection_esid=connection_esid,
                        asset_type=asset_type,
                        provider_asset_id=asset_id,
                        label=asset_id,
                    )
                except Exception as e:  # noqa: BLE001
                    logging.warning(
                        "upsert_connection_asset raised for %r asset=%r: %s",
                        username,
                        asset_id,
                        e,
                    )
            # stamp connection_esid on the team doc so the GA4 /
            # Google Ads connector card resolves a Manage surface (the field
            # connector_status_service._resolve_connection_esid_fallback reads).
            # The tokenless de_connection_asset rows carry no legacy_asset_esid
            # backlink, so the primary find_connection_esid_by_legacy_assets
            # lookup can't resolve these cards — the team-doc stamp is what
            # lights them up, mirroring the FB bridge + YouTube.
            # Native ES partial-update (doc= merge), NOT a full-doc replace.
            # KAN-NNNN — use provider-specific fields (google_ads_connection_esid,
            # ga4_connection_esid, youtube_connection_esid) instead of a shared
            # connection_esid to prevent one provider's stamp from overwriting
            # another's when a team has multiple wizard-cohort connectors.
            try:
                from lib.es_helpers import get_elasticsearch_client

                es_team = get_elasticsearch_client()
                if es_team is not None and connection_esid:
                    # Map connector_id to the provider-specific field name
                    field_map = {
                        "google_ads": "google_ads_connection_esid",
                        "google_analytics_4": "ga4_connection_esid",
                    }
                    field_name = field_map.get(selected_connector_id, "connection_esid")
                    es_team.update(
                        index="de_asset_teams",
                        id=team_esid,
                        doc={field_name: connection_esid},
                        refresh="wait_for",
                    )
            except Exception as e:  # noqa: BLE001 — connection + assets already written; stamp is last
                logging.warning(
                    "team-doc connection_esid stamp failed for team=%s connector=%s: %s",
                    team_esid,
                    selected_connector_id,
                    e,
                )
        else:
            logging.warning(
                "create_tokenless_connection failed for %r: %s",
                username,
                connection_result.get("error"),
            )

    first_look_status = "skipped"
    first_look_dashboard_id: str | None = None
    if connection_esid:
        primary_workspace_esid: str | None = None
        try:
            from asset_management.web.services.workspace_membership_service import (
                WorkspaceMembershipService,
            )

            primary_workspace_esid = WorkspaceMembershipService.get_primary_workspace(username)
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "tokenless apply primary-workspace lookup raised for %r: %s",
                username,
                e,
            )

        try:
            from asset_management.web.services.template_service import (
                TemplateService,
            )

            landed_ids = TemplateService.materialize_pack_for_user(
                connector_id=selected_connector_id,
                team_esid=team_esid,
                source_account_ids=list(provider_asset_ids),
                workspace_esid=primary_workspace_esid,
            )
            if landed_ids:
                first_look_status = "installed"
                first_look_dashboard_id = landed_ids[0]
            else:
                first_look_status = "no_pack"
        except Exception as e:  # noqa: BLE001
            logging.warning("tokenless apply pack install raised for %r: %s", username, e)
            first_look_status = "exception"

    asset_count = len(provider_asset_ids)
    AuditLogService.log_event(
        username,
        f"{audit_event_prefix}_tokenless_finalize",
        connection_esid is not None,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event={audit_event_namespace}.tokenless_finalize "
            f"connector_id={selected_connector_id} "
            f"connection_esid={connection_esid or '(none)'} "
            f"team_esid={team_esid} "
            f"asset_count={asset_count} "
            f"first_look={first_look_status} "
            f"first_look_id={first_look_dashboard_id or '(none)'}"
        ),
    )

    kickoff_ok = False
    if connection_esid and first_look_status in ("installed", "created", "exists"):
        kickoff_result = _kickoff_on_demand_backfill(
            username=username,
            connection_esid=connection_esid,
            connector_id=selected_connector_id,
            asset_count=asset_count,
            # team-scoped importers (youtube) dispatch --team_ids
            # off this; the --connection_esid connectors ignore it.
            scope_team_esid=team_esid,
        )
        kickoff_ok = bool(kickoff_result.get("ok"))

    if connection_esid is None:
        result["error"] = "Connection write failed; please retry."
        result["error_kind"] = "connection_write_failed"

    result["success"] = connection_esid is not None
    result["connection_esid"] = connection_esid
    result["provider_asset_ids"] = list(provider_asset_ids)
    result["asset_count"] = asset_count
    result["first_look_status"] = first_look_status
    result["first_look_id"] = first_look_dashboard_id
    result["kickoff_ok"] = kickoff_ok
    return result


def _finalize_tokenless_connector(
    *,
    username: str,
    selected_connector_id: str,
    account_type: str,
    state: dict,
):
    """Finalise a no-OAuth connector inline at step-4.

    Mirrors the post-selection portion of step-6 finish (write
    connection + asset, materialise first-look, kick off importer,
    redirect to step-7 building screen) without the OAuth + asset
    selection ceremony that would be empty for a cluster-managed
    connector.

    the fail-soft /dashboard fallbacks are gone. A failed
    connection write, an unresolved primary team, or a zero-asset
    resolution now bounces back to step-6 with an explicit banner so
    the user knows setup did NOT complete (the old posture landed them
    on a blank dashboard with no message). A SUCCESSFUL connection
    always proceeds to the step-7 building screen, even when the
    on-demand kickoff failed — the connection + first-look exist and
    the daily cron backfills overnight, so the completion interlude is
    honest; its poll ceiling lands the user on /dashboard regardless.

    Returns a Flask redirect Response. Caller is the step-4 POST
    handler; the wizard_session is already saved to step-5 by the
    caller, so this helper only mutates state via the downstream
    completes (org_shared, selected_assets, current_step=step-7).

    the connection-write core was extracted to
    ``_apply_tokenless_connector_for_team`` so the data-connectors
    page can reuse it with a user-picked team_esid. This wrapper
    keeps the onboarding semantics intact: primary-team resolution,
    wizard_session bookkeeping, redirects.
    """
    from asset_management.web.services.onboarding_branching_service import (
        OnboardingBranchingService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    primary_team_esid = _resolve_primary_team_esid(username)

    if not primary_team_esid:
        AuditLogService.log_event(
            username,
            "onboarding_step6_tokenless_finalize_no_team",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(f"analytics_event=wizard.step6.tokenless_finalize_no_team " f"connector_id={selected_connector_id}"),
        )
        logging.warning(
            "tokenless finalise skipped for %r: no primary team membership",
            username,
        )
        return _step6_error_redirect(
            username=username,
            connector_id=selected_connector_id,
            error_code="no_team",
        )

    apply_result = _apply_tokenless_connector_for_team(
        username=username,
        selected_connector_id=selected_connector_id,
        account_type=account_type,
        team_esid=primary_team_esid,
        audit_event_prefix="onboarding_step6",
        audit_event_namespace="wizard.step6",
    )

    # every apply failure surfaces an explicit step-6 banner.
    # The zero-asset / unmapped cases skip the wizard-session bookkeeping
    # below on purpose: nothing was set up, so the session must not
    # advance past the user's retry surface.
    _apply_error_codes = {
        "unmapped_connector": "unsupported",
        "no_provider_asset": "not_configured",
        "no_team": "no_team",
    }
    error_kind = apply_result.get("error_kind")
    if error_kind in _apply_error_codes:
        return _step6_error_redirect(
            username=username,
            connector_id=selected_connector_id,
            error_code=_apply_error_codes[error_kind],
        )
    if not apply_result.get("success"):
        return _step6_error_redirect(
            username=username,
            connector_id=selected_connector_id,
            error_code="connect_failed",
        )

    # Wizard-session bookkeeping — runs whether or not the connection
    # write succeeded, mirroring the legacy fail-soft contract.
    defaults = OnboardingBranchingService.for_account_type(account_type)
    org_shared = defaults.step6_org_shared_default
    state["selected_assets"] = list(apply_result.get("provider_asset_ids") or [])
    state["friendly_names"] = {}
    state["org_shared"] = bool(org_shared)
    state["current_step"] = "step-7"
    try:
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-7",
            state=state,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("wizard-session save failed for %r: %s", username, e)

    # a successful connection always gets the step-7 building
    # interlude, kickoff or not. When the kickoff failed, the progress
    # doc is either stamped failed (the building pill renders the error)
    # or absent (the poll ceiling fires and lands on /dashboard) — both
    # are better than the old silent immediate /dashboard bounce, and the
    # connection + first-look are real either way.
    return redirect(
        url_for(
            "analytics.onboarding_step7_building_page",
            connector_id=selected_connector_id,
            asset_count=apply_result.get("asset_count", 0),
            next="/dashboard",
        )
    )


# fixed error banners for the step-6 re-entry redirects.
# The step-6 dispatch helpers used to redirect("/dashboard") with no
# user-visible message on every failure ("the wizard skipped me onto a
# blank dashboard"). They now bounce back to the step-6 grid with
# ?error=<code>; the GET handler maps the code through this dict so the
# raw query param is never echoed into the page.
_STEP6_ERROR_MESSAGES: dict[str, str] = {
    # this used to say "brand-new accounts can take a few
    # seconds to provision, please click Continue to try again". Under
    # the real cause (a retired index, never an indexing race) retrying
    # could not succeed, so the copy sent users into a loop. The banner
    # now points at the surface that can actually complete the setup.
    "no_team": (
        "We couldn't work out which team to connect this source to. "
        "Connect it from the Data Connectors page, or contact support "
        "if this keeps happening."
    ),
    "no_access": (
        "Your account doesn't have access to the connection setup page "
        "this source needs. Ask your workspace owner to connect it from "
        "the Data Connectors page, or pick a different source."
    ),
    "not_configured": (
        "This source isn't configured for your team yet, so the connection "
        "couldn't be completed. Connect it from the Data Connectors page, "
        "or contact support if this keeps happening."
    ),
    "connect_failed": ("We couldn't save your connection — nothing was set up. Please " "click Continue to try again."),
    "unsupported": ("This source can't be connected through the wizard yet. Pick a " "different source to continue."),
}


def _step6_error_redirect(*, username: str, connector_id: str, error_code: str):
    """bounce back to the step-6 grid with an explicit banner.

    Single replacement for the four fail-soft ``redirect("/dashboard")``
    escape hatches in the step-6 dispatch helpers. Emits the canonical
    funnel ``errored`` event (the failure-specific audit event stays at
    the call site), then lands the user back on step-6 where
    ``?error=<code>`` renders the matching banner and the persisted tile
    selection survives for a retry.
    """
    try:
        WizardAnalyticsService.emit(
            step="step-6",
            action=ACTION_ERRORED,
            context=WizardAnalyticsService.build_context(
                username=username,
                wizard_session=None,
                connector_override=connector_id,
            ),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"error_code": error_code},
        )
    except Exception as e:  # noqa: BLE001 — the emit must never mask the error surface
        logging.warning("step-6 errored emit failed for %r: %s", username, e)
    return redirect(url_for("analytics.onboarding_step6_page", error=error_code))


def _wizard_can_reach_data_connectors(username: str) -> bool:
    """pre-flight the ``mc_data_connectors`` gate before the
    step-6 hand-off redirects to ``/data-connectors/``.

    The destination's ``_require_data_connectors()`` aborts 403, which the
    app-level 403 handler rewrites into a redirect — from the wizard
    user's seat that is indistinguishable from "the wizard skipped me".
    Checking the same merged-permissions resolution here lets step-6
    surface an explicit "ask your workspace owner" error instead. The
     workspace-tier bridge covers wizard-signup owners/admins;
    account types outside it (e.g. invited team members) are exactly who
    this check catches. Fail-closed: a resolution error here would also
    fail at the destination gate, so denying with a visible message is
    strictly better than redirecting into the 403 black hole.
    """
    try:
        from asset_management.web.services.role_permissions_service import (
            RolePermissionsService,
        )

        perms = RolePermissionsService.get_merged_permissions(
            session.get("roles", []),
            username=username,
        )
        return bool(perms.get("mc_data_connectors"))
    except Exception as e:  # noqa: BLE001 — see docstring: deny-with-message beats the 403 black hole
        logging.warning("mc_data_connectors pre-check failed for %r: %s", username, e)
        return False


def _data_connectors_onboarding_url(*, connector_id: str, team_esid: str) -> str:
    """the ``/assets/data-connectors/`` hand-off URL used by the
    step-6 YouTube and Google Ads / GA4 redirects.

    Both call sites used to build this with
    ``url_for('data_connectors.index')``, on the stated assumption that
    ``PrefixMiddleware`` would prepend ``SCRIPT_NAME=/assets``
    automatically. It does not from here. The middleware only sets
    ``SCRIPT_NAME`` when the INCOMING path starts with ``/assets``, and
    these redirects are issued from ``POST /onboarding/step-6`` — a bare
    top-level route with its own nginx block. So ``url_for`` emitted a
    bare ``/data-connectors/...``, which nginx has no location block for
    and answers with a 404 before the request ever reaches Flask. The
    page is served only under the ``/assets`` mount.

    Neither hand-off can ever have worked on a prefixed deployment; the
    dead primary-team lookup (the main defect) failed earlier in
    the same request and masked it.

    The literal ``/assets`` mount matches the working precedent in
    ``dashboard_checklist_service``, whose two
    ``/assets/data-connectors/`` action URLs are built the same way.
    """
    from urllib.parse import quote as _quote

    return (
        f"/assets{url_for('data_connectors.index')}"
        f"?open={_quote(connector_id, safe='')}&from_onboarding=1"
        f"&team={_quote(team_esid, safe='')}"
    )


def _redirect_youtube_to_data_connectors(*, username: str):
    """hand the onboarding YouTube path off to the Data
    Connectors modal.

    Resolves the user's primary team and redirects to
    ``/data-connectors/?open=youtube&from_onboarding=1&team=<esid>``.
    The modal page's JS reads the params, auto-selects the team,
    auto-opens ytWizard, and (on submit success) POSTs to
    ``api_wizard_youtube_onboarding_finalize`` which writes the
    wizard_session step-7 bookkeeping and returns the step-7 redirect.

    the no-team soft-skip to /dashboard is gone. The team
    lookup resolves via the ladder (the old membership-index
    query returned ``None`` for every earlier account, which is why
    this path was dead), and if it still can't resolve, the user lands
    back on step-6 with an explicit banner instead of a silent blank
    dashboard. The redirect is
    also pre-flighted against ``mc_data_connectors`` so an account type
    outside the bridge gets an actionable message rather than
    the destination's 403-rewritten-to-302 black hole.
    """
    primary_team_esid = _resolve_primary_team_esid(username)

    if not primary_team_esid:
        AuditLogService.log_event(
            username,
            "onboarding_step6_youtube_redirect_no_team",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail="analytics_event=wizard.step6.youtube_redirect_no_team connector_id=youtube",
        )
        return _step6_error_redirect(
            username=username,
            connector_id="youtube",
            error_code="no_team",
        )

    if not _wizard_can_reach_data_connectors(username):
        AuditLogService.log_event(
            username,
            "onboarding_step6_data_connectors_denied",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=("analytics_event=wizard.step6.data_connectors_denied " "connector_id=youtube"),
        )
        return _step6_error_redirect(
            username=username,
            connector_id="youtube",
            error_code="no_access",
        )

    AuditLogService.log_event(
        username,
        "onboarding_step6_youtube_redirect",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=("analytics_event=wizard.step6.youtube_redirect " "connector_id=youtube " f"team_esid={primary_team_esid}"),
    )

    return redirect(
        _data_connectors_onboarding_url(
            connector_id="youtube",
            team_esid=primary_team_esid,
        )
    )


def _maybe_redirect_tokenless_to_data_connectors(
    *,
    username: str,
    selected_connector_id: str,
):
    """gap-detect + redirect for tokenless connectors whose
    finalize needs a per-team config row.

    Returns a Flask redirect Response when the user's primary team has
    NOT been configured for ``selected_connector_id`` yet — sending
    them to the Data Connectors modal's pick-or-create wizard for that
    connector (the v2 surface), with the onboarding-finalize
    URL flag. Returns ``None`` when the team is already configured (an
    operator pre-seeded the customer ID / property), so the caller's
    existing tokenless finalize runs unchanged — no regression.

    a missing primary team no longer returns ``None``. That
    fall-through sent an unresolved-team user into
    ``_finalize_tokenless_connector``, which resolved zero provider
    assets and silently bounced to /dashboard — precisely the outcome
     was written to prevent. The team lookup now resolves via the
     ladder, and an unresolvable team returns an explicit step-6
    error redirect (Response), never ``None``.
    """
    primary_team_esid = _resolve_primary_team_esid(username)

    if not primary_team_esid:
        AuditLogService.log_event(
            username,
            "onboarding_step6_tokenless_finalize_no_team",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(f"analytics_event=wizard.step6.tokenless_finalize_no_team " f"connector_id={selected_connector_id}"),
        )
        return _step6_error_redirect(
            username=username,
            connector_id=selected_connector_id,
            error_code="no_team",
        )

    if selected_connector_id == "google_ads":
        configured = bool(_resolve_team_to_google_ads_customer_id(primary_team_esid))
    elif selected_connector_id == "google_analytics_4":
        configured = bool(_resolve_team_to_ga_property_ids(primary_team_esid))
    else:
        # Defensive: only the connectors enumerated in
        # _ONBOARDING_NEEDS_TEAM_CONFIG_CONNECTOR_IDS should reach
        # here. Anything else falls through to the existing finalize.
        return None

    if configured:
        return None

    # same pre-flight as the YouTube hand-off: an account
    # type outside the bridge would 403 at the destination
    # (rewritten to a 302 by the app-level handler), which reads as a
    # silent skip. Surface the blocker on step-6 instead.
    if not _wizard_can_reach_data_connectors(username):
        AuditLogService.log_event(
            username,
            "onboarding_step6_data_connectors_denied",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(f"analytics_event=wizard.step6.data_connectors_denied " f"connector_id={selected_connector_id}"),
        )
        return _step6_error_redirect(
            username=username,
            connector_id=selected_connector_id,
            error_code="no_access",
        )

    AuditLogService.log_event(
        username,
        f"onboarding_step6_{selected_connector_id}_redirect",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event=wizard.step6.{selected_connector_id}_redirect "
            f"connector_id={selected_connector_id} "
            f"team_esid={primary_team_esid}"
        ),
    )

    return redirect(
        _data_connectors_onboarding_url(
            connector_id=selected_connector_id,
            team_esid=primary_team_esid,
        )
    )


# on-demand FB import kickoff invoked from step-6 finish.
# Two failure modes the helper handles fail-soft:
#   1. The progress doc write fails (ES down). We still spawn the
#      importer — the user's data is the primary outcome, the pill is
#      a UX nicety. Caller redirects directly to /dashboard rather
#      than the building screen (no doc → 404 every poll → ceiling
#      fires anyway, but cleaner to just skip the interlude).
#   2. Popen raises (binary missing, perms, etc). We stamp the
#      progress doc as failed so the pill can render the error, and
#      return ok=False so the caller falls back to /dashboard.
import subprocess as _subprocess  # noqa: E402  — used by _kickoff_on_demand_backfill

# follow-up REGISTRY-001 — connector → importer dispatch.
# Adding a future connector (google_ads, ga4, echo) is a one-line
# entry here + that connector's run_daily_imports.py honoring
# --connection_esid + the connector's template in
# first_look_templates.py. Today only meta_ads is wired end-to-end.
#
# Fields:
#   importer_source  — first positional arg to import.py (subdirectory
#                      under bin/python/import_scripts/)
#   importer_action  — second positional arg (script name without .py)
#   lock_connector   — connector key for the cluster-level FB-token lock
#                      (LOCK-001). Multiple registry entries can share
#                      the same lock when they share a provider/API
#                      surface (e.g. fb-page-insights and fb-ad-insights
#                      both use the FB Graph API and the same app token).
#   days_total       — backfill window in days. Hard-coded today to
#                      match the marketing "90d backfill" copy.
_CONNECTOR_IMPORTER_REGISTRY: dict[str, dict] = {
    "meta_ads": {
        "importer_source": "facebook",
        "importer_action": "run_daily_imports",
        "lock_connector": "facebook",
        # follow-up — on-demand backfill window dropped from 90
        # to 7 days. 90d × N accounts on first connect was hitting Meta
        # Marketing API rate limits during the wizard finish; the daily
        # cron extends the history opportunistically thereafter. The
        # "90-day backfill kicks off when you finish" marketing copy on
        # step-6 is out of date; updated in this commit.
        "days_total": 7,
    },
    # follow-up — Google Ads. LIVE
    # (2026-05-28): step-6 dispatches google_ads through the tokenless
    # finalize (pre-configured team) or the Data Connectors modal
    # hand-off (unconfigured team), both of which reach
    # _kickoff_on_demand_backfill via _apply_tokenless_connector_for_team.
    # (An earlier note here claimed the entry was inert because step-6
    # finish was unreachable for google_ads — superseded;.)
    "google_ads": {
        "importer_source": "google_ads",
        "importer_action": "run_daily_imports",
        "lock_connector": "google_ads",
        "days_total": 30,  # matches the importer's existing default window
    },
    # Echo. Inert today. wired the
    # registry + a tokenless step-4 dispatch; (2026-05-25)
    # removed the catalog tile + the tokenless path because the Echo
    # importer reads de_asset_echo_tenants (set by the legacy
    # per-tenant wizard on /assets/data-connectors/) and never the
    # de_connection rows the tokenless path wrote. Entry kept so a
    # future Kavanah-managed-Echo path can re-light this without
    # rewriting the dispatch shape; today nothing invokes it.
    "echo": {
        "importer_source": "echo",
        "importer_action": "run_daily_imports",
        "lock_connector": "echo",
        "days_total": 30,
    },
    # Google Analytics 4. Importer wired for
    # -connection_esid (modifications to
    # google_analytics/run_daily_imports.py + get_ga_metrics.py);
    # tokenless dispatch through _finalize_tokenless_connector below.
    "google_analytics_4": {
        "importer_source": "google_analytics",
        "importer_action": "run_daily_imports",
        "lock_connector": "google_analytics",
        "days_total": 30,
    },
    # YouTube. The importer is team-scoped, NOT connection-scoped:
    # run_daily_imports requires --team_ids and
    # resolves channels + per-account OAuth tokens from the team's linked
    # de_asset_youtube_channels / de_asset_youtube_accounts docs — it never
    # reads de_connection. The kickoff helper dispatches --team_ids for this
    # source (see the youtube branch in _kickoff_on_demand_backfill).
    # lock_connector is nominal: the YouTube importer takes no cluster lock
    # (same posture as report_schedule_service._LOCKED_IMPORT_SOURCES), so
    # is_held is always False and the LOCK-001 guard is a no-op.
    "youtube": {
        "importer_source": "youtube",
        "importer_action": "run_daily_imports",
        "lock_connector": "youtube",
        "days_total": 30,  # matches the importer's existing default window
    },
    # TikTok organic. Team-scoped like YouTube (--team_ids). The
    # Display API has no history, so the importer takes today's snapshot and
    # ignores the date window; days_total only feeds the shared window math.
    # lock_connector is nominal: the importer locks per connection, around the
    # token refresh only, so the global is_held probe never blocks a kickoff.
    "tiktok": {
        "importer_source": "tiktok",
        "importer_action": "run_daily_imports",
        "lock_connector": "tiktok",
        "days_total": 1,
    },
}


# YouTube run_daily_imports reports progress as 3 sequential steps
# (fetch / index / archive), not a per-day loop. Progress docs for
# youtube kickoffs are minted with this step count so days_fetched/days_total
# percent math is real (mirrors report_schedule_service._YOUTUBE_PROGRESS_STEPS).
_YOUTUBE_PROGRESS_STEPS = 3


# connector ids that bypass step-5 (no OAuth
# handshake) AND step-6 (no per-tenant asset selection). Step-4 POST
# finalises these inline: writes a tokenless connection + a single
# asset row, materialises the first-look dashboard, kicks off the
# on-demand backfill, and hands off to the step-7 building screen.
# Membership here implies no scope manifest is registered for the
# connector (the supported_connector_ids check on step-4 takes
# precedence and would route through step-5 otherwise).
#
# * "google_ads" — cluster-wide OAuth refresh token in
#   /app/reporting_configs/google-ads.yaml. Per-user scope is the
#   google_ads_customer_id configured on the principal's primary team
#   (de_asset_teams.google_ads_customer_esid → de_asset_google_ads_customers).
# * "google_analytics_4" — cluster-wide credentials in
#   /app/reporting_configs/google-analytics.yaml. Per-user scope is
#   the list of properties on the principal's primary team
#   (de_asset_teams.team_ga4_property_esids → de_asset_ga_properties).
#
# (2026-05-25) — "echo" removed. The tokenless path
# wrote de_connection + de_connection_asset rows that the Echo
# importer never reads (importer keys on de_asset_echo_tenants +
# team.ds_echo). Echo setup now lives only on /assets/data-connectors/
# via the legacy subdomain+api_key wizard. Echo tile dropped from the
# step-4 catalog (see connector_catalog_service.py).
_NO_OAUTH_CONNECTOR_IDS: frozenset[str] = frozenset({"google_ads", "google_analytics_4"})


# tokenless connectors whose finalize requires a per-team
# config row before it can resolve a non-empty provider_asset_ids set.
# A brand-new onboarding user's primary team is unconfigured by
# construction, so the step-4 dispatch redirects these to the Data
# Connectors modal's pick-or-create wizard first (mirrors for
# YouTube). If the team IS already configured (an operator pre-seeded
# it), the redirect short-circuits and we fall through to the regular
# tokenless finalize — no regression for pre-configured paths.
_ONBOARDING_NEEDS_TEAM_CONFIG_CONNECTOR_IDS: frozenset[str] = frozenset({"google_ads", "google_analytics_4"})


def _resolve_team_to_google_ads_customer_id(team_esid: str) -> str | None:
    """resolve a team_esid to its configured google_ads_customer_id.

    de_asset_teams may carry a ``google_ads_customer_esid`` field
    pointing into de_asset_google_ads_customers. The wizard reads
    this to populate the connection_asset row's provider_asset_id
    (the field the importer's per-customer filter keys off).

    Returns the customer id string ("123-456-7890" shape) on the
    happy path. Returns ``None`` when the team document is missing,
    has no google_ads_customer_esid configured, or the pointed-to
    customer doc is absent — the caller surfaces a no-team-like
    early-exit so the wizard doesn't dead-end.
    """
    if not team_esid:
        return None
    try:
        from lib.es_helpers import get_elasticsearch_client
    except Exception:
        return None

    es = get_elasticsearch_client()
    if es is None:
        return None

    try:
        team_doc = es.get(
            index="de_asset_teams",
            id=team_esid,
            ignore=[404],
        )
    except Exception:
        return None

    if not team_doc or not team_doc.get("found"):
        return None
    customer_esid = (team_doc.get("_source") or {}).get("google_ads_customer_esid") or ""
    customer_esid = str(customer_esid).strip()
    if not customer_esid:
        return None

    try:
        customer_doc = es.get(
            index="de_asset_google_ads_customers",
            id=customer_esid,
            ignore=[404],
        )
    except Exception:
        return None

    if not customer_doc or not customer_doc.get("found"):
        return None
    customer_id = (customer_doc.get("_source") or {}).get("google_ads_customer_id") or ""
    customer_id = str(customer_id).strip()
    return customer_id or None


def _resolve_team_to_ga_property_ids(team_esid: str) -> list[str]:
    """resolve a team_esid to its configured GA4 property_ids.

    de_asset_teams may carry a ``team_ga4_property_esids`` array
    (multi-select per asset_configs.py) pointing into
    de_asset_ga_properties. Each pointed-to doc has a bare-number
    ``ga_property_id`` field; the wizard writes one connection_asset
    per property with that bare number as provider_asset_id (matching
    the importer's doc field).

    Returns the list of bare numeric property_ids on the happy path,
    or an empty list when the team has no GA4 properties configured /
    the team doc is missing / the property docs are missing.
    """
    if not team_esid:
        return []
    try:
        from lib.es_helpers import get_elasticsearch_client
    except Exception:
        return []

    es = get_elasticsearch_client()
    if es is None:
        return []

    try:
        team_doc = es.get(
            index="de_asset_teams",
            id=team_esid,
            ignore=[404],
        )
    except Exception:
        return []

    if not team_doc or not team_doc.get("found"):
        return []
    property_esids_raw = (team_doc.get("_source") or {}).get("team_ga4_property_esids") or []
    if not isinstance(property_esids_raw, list):
        return []
    property_esids = [str(e).strip() for e in property_esids_raw if str(e).strip()]
    if not property_esids:
        return []

    property_ids: list[str] = []
    for esid in property_esids:
        try:
            prop_doc = es.get(
                index="de_asset_ga_properties",
                id=esid,
                ignore=[404],
            )
        except Exception:
            continue
        if not prop_doc or not prop_doc.get("found"):
            continue
        prop_id = (prop_doc.get("_source") or {}).get("ga_property_id") or ""
        prop_id = str(prop_id).strip()
        if prop_id:
            property_ids.append(prop_id)
    return property_ids


def _resolve_team_to_youtube_channel_ids(team_esid: str) -> list[str]:
    """resolve a team_esid to its linked YouTube channel_ids.

    Reads both team→channel linkage conventions (the same pair the
    importer's ``_channel_esids_for_teams`` reads): the multi-select
    ``team_youtube_channel_handle_esids`` list and the legacy scalar
    ``youtube_channel_esid``, each pointing into
    ``de_asset_youtube_channels``. Each pointed-to doc carries the bare
    ``youtube_channel_id`` (UC... shape) that becomes the first-look
    pack's placeholder substitution value — matching the
    ``youtube_channel_id`` field the importer stamps on every
    ``*_youtube_analytics`` doc.

    Returns the list of channel_ids on the happy path, or an empty list
    when the team has no channels linked / the team doc is missing /
    the channel docs are missing.
    """
    if not team_esid:
        return []
    try:
        from lib.es_helpers import get_elasticsearch_client
    except Exception:
        return []

    es = get_elasticsearch_client()
    if es is None:
        return []

    try:
        team_doc = es.get(
            index="de_asset_teams",
            id=team_esid,
            ignore=[404],
        )
    except Exception:
        return []

    if not team_doc or not team_doc.get("found"):
        return []
    team_src = team_doc.get("_source") or {}
    channel_esids_raw = team_src.get("team_youtube_channel_handle_esids") or []
    if isinstance(channel_esids_raw, str):
        channel_esids_raw = [channel_esids_raw] if channel_esids_raw else []
    elif not isinstance(channel_esids_raw, list):
        channel_esids_raw = []
    legacy_esid = str(team_src.get("youtube_channel_esid") or "").strip()
    channel_esids = [str(e).strip() for e in channel_esids_raw if str(e).strip()]
    if legacy_esid and legacy_esid not in channel_esids:
        channel_esids.append(legacy_esid)
    if not channel_esids:
        return []

    channel_ids: list[str] = []
    for esid in channel_esids:
        try:
            channel_doc = es.get(
                index="de_asset_youtube_channels",
                id=esid,
                ignore=[404],
            )
        except Exception:
            continue
        if not channel_doc or not channel_doc.get("found"):
            continue
        channel_id = (channel_doc.get("_source") or {}).get("youtube_channel_id") or ""
        channel_id = str(channel_id).strip()
        if channel_id:
            channel_ids.append(channel_id)
    return channel_ids


def _kickoff_meta_lock_scope(*, connection_esid, legacy_team_esid, scope_team_esid) -> str:
    """the per-connection lock scope key a Meta kickoff will
    contend on, or "" for the connector-global ``facebook`` key.

    Mirrors the importer's lock site (``run_daily_imports._lock_scope_for_run``
    → ``ImportLockService.meta_connection_scope_key``) so the web-layer gate
    and the importer's acquire agree on the key:

      * legacy (page_filtered) dispatch → "" — those runs take the global
        lock (shared master-config token);
      * team-scoped dispatch (``scope_team_esid``) → resolve the team cohort
        exactly as the importer will;
      * wizard-connection dispatch (bare ``connection_esid``) → the importer
        resolves connection → team → this same connection, so key on the
        connection esid directly (``scope_key_for_ids([esid])`` for byte
        parity). If the importer's resolution degrades to the global key
        (e.g. the team isn't account_wide), the probe here is merely
        optimistic — the importer's own acquire is the real gate and a loss
        resolves the progress doc, visibly.

    Fail-SAFE to "" (probe the global key — the conservative gate)."""
    if legacy_team_esid is not None:
        return ""
    try:
        from lib.import_lock_service import ImportLockService

        if scope_team_esid:
            from lib.es_helpers import get_elasticsearch_client

            return ImportLockService.meta_connection_scope_key(get_elasticsearch_client(), scope_team_esid)
        if connection_esid and not str(connection_esid).startswith("legacy_team:"):
            return ImportLockService.scope_key_for_ids([connection_esid])
    except Exception as exc:  # noqa: BLE001 — boundary; default to the global probe
        logging.warning("kickoff lock-scope resolve raised: %s — probing the global key", exc)
    return ""


def _kickoff_on_demand_backfill(
    *,
    username: str,
    connection_esid: str,
    connector_id: str,
    asset_count: int,
    start_date: str | None = None,
    end_date: str | None = None,
    legacy_team_esid: str | None = None,
    scope_team_esid: str | None = None,
    stages: list[str] | None = None,
    scope_filter: str | None = None,
    dedup_only: bool = False,
) -> dict:
    """Write the initial progress doc + spawn the importer subprocess.

    KAN selective dispatch (B) — ``stages`` is an optional subset of
    ``["ad", "page", "ig"]``. When provided, the importer is spawned with
    ``--stages=...`` and runs ONLY those Meta stages (e.g. an IG-only dashboard
    passes ``["ig"]``). When None/empty, no ``--stages`` flag is added and the
    importer runs all three (the existing wizard/cron default). Only meaningful
    for the meta_ads connector; ignored for others.

    The progress doc is written SYNCHRONOUSLY (before Popen) so the
    building screen's first poll always finds something — without this,
    a slow subprocess startup would have the first poll return 404
    (no_progress), the JS would treat it as "keep waiting" until the
    subprocess wrote the doc, and the perceived latency would push past
    the 15s ceiling on slow hosts.

    Connector dispatch is registry-driven (REGISTRY-001): only
    connectors in ``_CONNECTOR_IMPORTER_REGISTRY`` have a wired
    importer; others return ``no_importer_for_connector`` so the
    caller falls back to a direct /dashboard redirect.

    Cluster-level lock (LOCK-001): before spawning, the helper checks
    ``ImportLockService.is_held`` for the registry entry's
    ``lock_connector``. If a cron run (or another on-demand backfill)
    holds the lock, the helper returns ``fb_import_in_flight`` and
    the caller falls back — the in-flight import will pull the same
    data anyway.

     Slice B — ``start_date`` / ``end_date`` (YYYY-MM-DD) override
    the registry-default window when both are passed. Used by the
    dashboard viewer's Custom Range "pull new data" path. When omitted
    (existing wizard step-6 / step-4 finish callers), the helper falls
    back to the registry's ``days_total`` window unchanged.

    Returns ``{"ok": True, "run_id": "..."}`` on a clean kickoff, or
    ``{"ok": False, "reason": "..."}`` on a failure the caller should
    fall back from (direct redirect to /dashboard).
    """
    entry = _CONNECTOR_IMPORTER_REGISTRY.get(connector_id)
    if entry is None:
        # Connector has no importer hook wired. The first-look
        # dashboard still materialised, the rest of the
        # wizard still works, the cron will catch up eventually.
        return {"ok": False, "reason": "no_importer_for_connector"}

    # entitlement gate. An import-blocked user (lapsed, or a
    # canceled Stripe subscription; NOT past_due, which is a grace period)
    # cannot kick off a NEW data import. This is the per-user import entry
    # point — it covers the wizard step-4/step-8 finish kickoffs AND the
    # dashboard viewer's custom-range "pull new data" path — so the gate
    # lives here rather than at each caller. Trial / active / past_due /
    # comped users are unaffected. ``is_import_blocked`` FAILS OPEN: an
    # undetermined state (ES blip) does NOT block, so a transient lookup
    # failure never refuses a legitimate user's import — the page display
    # may show lapsed, but the import proceeds and the data is theirs (lapse
    # = block imports only, never hide data — confirmed 2026-06-04). The
    # 07:05 cron enforces the same gate per-team, so
    # scheduled imports honor the entitlement too.
    try:
        from asset_management.web.services.subscription_state_service import (
            SubscriptionStateService,
        )

        if SubscriptionStateService.is_import_blocked(username):
            AuditLogService.log_event(
                username,
                "billing_import_blocked_lapsed",
                False,
                ip_address=request.remote_addr if request else "",
                user_agent=request.headers.get("User-Agent", "") if request else "",
                detail=f"connector_id={connector_id} connection_esid={connection_esid}",
            )
            logging.info(
                "kickoff: subscription import-blocked for %r; refusing " "on-demand import for connection=%r.",
                username,
                connection_esid,
            )
            return {"ok": False, "reason": "subscription_lapsed"}
    except Exception as e:  # noqa: BLE001 — boundary; never 500 the finish path
        # A failure resolving subscription state must not crash the wizard
        # finish. Log and proceed — fail-open is the chosen posture.
        logging.warning("kickoff: subscription-state check failed for %r: %s", username, e)

    # LOCK-001 — bail if another importer holds a CONFLICTING lock; the
    # importer's own acquire would lose anyway, so bailing here is honest
    # and cheaper than spawning a doomed subprocess.
    #
    # for the Meta family the probe is per lock CLASS, matching
    # the importer's scoped acquire (facebook::<connection_esid> for wizard
    # connections, connector-global for legacy/unresolvable): ``is_blocked``
    # applies the same global↔scoped exclusion the acquire does, so an
    # UNRELATED connection's in-flight Meta import no longer skips this
    # kickoff (the onboarding silent-skip), while a held global
    # lock or this same connection's own lock still bails. This replaces the
    # skip-the-guard-entirely branch (account_wide runs are locked
    # again, just per-connection). Non-Meta connectors keep the historical
    # global is_held probe — their scoped acquire is the importer-side gate.
    try:
        from lib.import_lock_service import ImportLockService

        if entry["lock_connector"] == "facebook":
            probe_scope = _kickoff_meta_lock_scope(
                connection_esid=connection_esid,
                legacy_team_esid=legacy_team_esid,
                scope_team_esid=scope_team_esid,
            )
            blocked = ImportLockService.is_blocked("facebook", probe_scope)
        else:
            blocked = ImportLockService.is_held(entry["lock_connector"])
        if blocked:
            logging.info(
                "kickoff: %s import in flight; skipping on-demand backfill "
                "for connection=%r (cron will pull on schedule).",
                entry["lock_connector"],
                connection_esid,
            )
            return {"ok": False, "reason": "fb_import_in_flight"}
    except Exception as e:  # noqa: BLE001 — boundary
        # Lock service unavailable (ES down, import error). Proceed
        # without the lock check — the race is rare and the cron is
        # the safety net.
        logging.warning("kickoff: lock check raised: %s — proceeding", e)

    # Backfill window. Hard-coded per entry today; if a future
    # connector wants a different range, the registry can carry an
    # alternate ``days_total`` without changing this helper.
    #
    # Slice B — when both ``start_date`` and ``end_date`` are
    # passed in (Custom Range "pull new data" path), they override the
    # registry default. ``days_total`` for the progress doc is recomputed
    # from the override window so the building-screen poller's percent
    # math (days_fetched / days_total) stays meaningful.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(tz=timezone.utc)
    if start_date and end_date:
        try:
            sd = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            ed = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as e:
            logging.warning("kickoff: invalid range override %r..%r: %s", start_date, end_date, e)
            return {"ok": False, "reason": "invalid_range"}
        if sd > ed:
            return {"ok": False, "reason": "invalid_range"}
        days_total = max(1, (ed - sd).days + 1)
    else:
        # config-key override for fast staging QA. When
        # WIZARD_KICKOFF_DAYS is set (master-config fboauth.wizard_kickoff_days
        # or env var), it wins over the registry's per-connector default.
        # Prod typically leaves it unset so the registry (7 days for meta_ads,
        # 30 days for echo / google_ads / GA4) applies.
        from asset_management.web.config import config as _config_2

        kickoff_override = _config_2.WIZARD_KICKOFF_DAYS
        registry_default = int(entry["days_total"])
        if kickoff_override is not None:
            days_total = kickoff_override
            logging.info(
                "kickoff: WIZARD_KICKOFF_DAYS=%d overrides registry default %d for connector=%s",
                days_total,
                registry_default,
                connector_id,
            )
        else:
            days_total = registry_default
        end_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        start_date = (now - timedelta(days=days_total)).strftime("%Y-%m-%d")

    try:
        from lib.import_progress_service import ImportProgressService
    except Exception as e:  # noqa: BLE001
        logging.warning("kickoff: progress service import failed: %s", e)
        return {"ok": False, "reason": "progress_import_failed"}

    # Popen guard. If a previous click on this connection already
    # spawned an importer that's still pending/running (within the 60min
    # progress-service idempotency window), surface that run_id without
    # spawning a second subprocess. ``start_run`` is itself idempotent on
    # connection_esid, but without this probe we'd reuse the existing
    # run_id and STILL Popen a second importer, racing the first one
    # against the same Meta per-user rate-limit budget. The frontend's
    # disabled-on-first-click guard prevents the double POST in the
    # common case; this is the belt to that frontend's braces.
    existing_run_id = ImportProgressService.find_active_run_id(connection_esid)
    if existing_run_id is not None:
        logging.info(
            "kickoff: active run already in flight for connection=%r run_id=%r; " "skipping duplicate Popen.",
            connection_esid,
            existing_run_id,
        )
        return {"ok": True, "run_id": existing_run_id, "asset_count": asset_count, "reuse": True}

    # youtube progress is step-count-based (3), not day-based; the
    # import window above still spans days_total days.
    # tiktok reports the same 3 steps.
    progress_days_total = _YOUTUBE_PROGRESS_STEPS if entry["importer_source"] in ("youtube", "tiktok") else days_total
    run_id = ImportProgressService.start_run(
        username=username,
        connection_esid=connection_esid,
        connector_id=connector_id,
        days_total=progress_days_total,
    )
    if not run_id:
        # The importer would still run (and write its own start_run
        # doc — idempotency reuses any existing one), but the
        # building screen's first poll would 404. Skip the
        # interlude to avoid wasting the user's 15s ceiling.
        return {"ok": False, "reason": "progress_start_failed"}

    # follow-up — page-filtered (legacy) teams pass --team_ids
    # instead of --connection_esid. The importer's main() at
    # run_daily_imports.py supports both surfaces; the team_ids path
    # was the original cron surface and works with the existing
    # de_asset_fb_app_users / de_asset_fb_pages token chain (no
    # de_connection row required).
    # both the legacy (page_filtered) and account_wide dashboard
    # custom-range paths dispatch the importer scoped by --team_ids and
    # tracked by --progress_run_id (the run_id the web layer already minted).
    #
    # Why team_ids and NOT connection_esid for account_wide too: the importer
    # (run_daily_imports.main) resolves connection_esid back to a team via
    # connection->principal->primary-team, but that reverse walk returns None
    # for connections whose principal has no resolvable primary-team
    # membership (confirmed on staging: SPjfc...->operator principal->None). When it
    # returns None the importer disables progress writes AND leaves team_ids
    # unset, so the import ran UNFILTERED across every team (39 pages, ~25min)
    # and left a duplicate progress doc stuck at 0/7, never stamped terminal.
    # The kickoff already resolved the correct team (team->connection), so we
    # pass that team directly. account_wide token resolution still works:
    # get_insights._build_account_wide_entries reads connection_esid off the
    # team doc itself (scoped by team_ids), so no --connection_esid is needed.
    cmd = [
        "python3",
        "/app/python/import.py",
        entry["importer_source"],
        entry["importer_action"],
        f"--start_date={start_date}",
        f"--end_date={end_date}",
        f"--progress_run_id={run_id}",
    ]
    source = entry["importer_source"]

    if source == "facebook":
        # Unchanged FB dispatch: scope by --team_ids. scope_esid is
        # always set for the range-import caller (it resolves a team first);
        # the connection_esid fallback covers any non-range caller.
        scope_esid = legacy_team_esid or scope_team_esid
        if scope_esid:
            cmd.insert(4, f"--team_ids={scope_esid}")
        else:
            cmd.insert(4, f"--connection_esid={connection_esid}")
        # KAN selective dispatch (B) — Meta-only stage subset.
        if stages:
            valid = [s for s in stages if s in ("ad", "page", "ig")]
            if valid:
                cmd.append(f"--stages={','.join(valid)}")
        # dedup-only refresh: the importer skips all per-day stages
        # and runs ONLY the windowed dedup-reach pull. Mutually exclusive with
        # --stages in practice (the Refresh button never sets stages); if both
        # were passed, the importer's dedup_only branch wins (returns before the
        # stage dispatch).
        if dedup_only:
            cmd.append("--dedup_only=true")
    elif source in ("youtube", "tiktok"):
        # tiktok is team-scoped the same way (require_scope on
        # --team_ids); it resolves the team's TikTok connections itself.
        # the YouTube importer is team-scoped (
        # require_scope): it resolves channels + per-account OAuth tokens from
        # the team's linked assets and never reads de_connection, so
        # --connection_esid would be ignored AND the scope gate would bail.
        # Dispatch --team_ids; refuse an unscoped pull outright.
        if scope_team_esid:
            cmd.insert(4, f"--team_ids={scope_team_esid}")
        else:
            logging.warning(
                "kickoff: %s connector with no scope_team_esid; refusing unscoped on-demand pull.",
                source,
            )
            ImportProgressService.complete_run(run_id=run_id, status="failed", error="no scope resolved")
            return {"ok": False, "reason": "no_scope_resolved"}
    else:
        # GA4 / Google Ads. Two dispatch shapes:
        #   1. Real OAuth connection: --connection_esid. The importer resolves
        #      the provider-asset filter from the connection AND mints its own
        #      progress run — idempotent on connection_esid, so it converges on
        #      the SAME run_id the web layer just minted (start_run keys on
        #      connection_esid alone). The poller finds it. No scope_filter.
        #   2. Service-account team (synthetic legacy_team: key, no real conn):
        #      pass the provider-asset scope filter directly + --progress_run_id
        #      so the importer scopes to THIS team and writes to OUR run_id.
        if connection_esid and not connection_esid.startswith("legacy_team:"):
            cmd.insert(4, f"--connection_esid={connection_esid}")
            # --progress_run_id is harmless if the importer also mints its own
            # (same id via idempotency); keep it for the importers that read it.
        elif scope_filter:
            filter_flag = "--property_ids" if source == "google_analytics" else "--customer_ids"
            cmd.insert(4, f"{filter_flag}={scope_filter}")
        else:
            # Should not happen (route guards this), but never run unscoped.
            logging.warning(
                "kickoff: %s connector with neither connection nor scope_filter; " "refusing unscoped on-demand pull.",
                connector_id,
            )
            ImportProgressService.complete_run(run_id=run_id, status="failed", error="no scope resolved")
            return {"ok": False, "reason": "no_scope_resolved"}
    try:
        _subprocess.Popen(
            cmd,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001 — boundary
        logging.warning("kickoff: Popen failed: %s", e)
        # Mark the doc failed so the pill can render an error pill
        # instead of staying "syncing" indefinitely.
        try:
            ImportProgressService.complete_run(
                run_id=run_id,
                status="failed",
                error=str(e),
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("kickoff: complete_run(failed) raised: %s", exc)
        return {"ok": False, "reason": "popen_failed"}

    return {"ok": True, "run_id": run_id, "asset_count": asset_count}


def _render_step6(
    *,
    username: str,
    connector_id: str,
    connector_name: str,
    assets,
    selected_ids,
    friendly_names: dict,
    friendly_name_errors: dict,
    show_org_shared_toggle: bool = False,
    org_shared_checked: bool = False,
):
    """Shared step-6 render path used by both GET and the POST-with-
    validation-error path.

    Keeping it as a small helper instead of redirecting back on POST
    error means the user's submitted aliases survive — a redirect
    would either lose them (form fields are not preserved across a
    302) or require shoving them into the session, which conflicts
    with the "selection persists to wizard_session only on Finish"
    contract.

    agency users get an Advanced disclosure with an
    "org-shared" checkbox. ``show_org_shared_toggle`` gates the
    block (False hides it entirely so non-agency users never see it);
    ``org_shared_checked`` is the initial checkbox state (defaults to
    True for agency mode per OnboardingBranchingService).
    """
    csrf_token = auth_module.refresh_csrf_token()
    return render_template(
        "onboarding_step8_assets.html",
        csrf_token=csrf_token,
        username=username,
        connector_id=connector_id,
        connector_name=connector_name,
        assets=assets,
        selected_ids=selected_ids,
        friendly_names=friendly_names,
        friendly_name_errors=friendly_name_errors,
        show_org_shared_toggle=show_org_shared_toggle,
        org_shared_checked=org_shared_checked,
        progress=_wizard_progress_for("step-8", username),
    )


@analytics_bp.route("/onboarding/step-8")
def onboarding_step8_page():
    """Wizard step 6 — multi-select asset table.

    Reads wizard_session.state.selected_connector_id (set by step-4)
    and renders the asset selection table for that connector. The
    catalog is read from WizardAssetCatalogService — empty today,
    real list once the connection-persistence follow-up ships.

    Guards (each one redirects back to the appropriate earlier step
    rather than rendering a wrong screen):

    * No active wizard_session → /onboarding/step-6.
    * Active session but no selected_connector_id → /onboarding/step-6.
    * Connector lacks a scope manifest (only meta_ads today) →
      /onboarding/step-6 — same gate step-5 uses.

    On a successful render, fires an ``onboarding_step8_entered``
    audit event with detail containing the prototype's
    ``analytics_event=wizard.step8.entered`` literal so the marketing
    funnel slice can find the step.

    pre-populates the friendly-name inputs from the prior
    visit's ``state.friendly_names`` (``{provider_asset_id: alias}``)
    so a resumed session sees the same aliases. defaults to
    the top-3 most recently active rows when no prior selection
    exists.

    token-expiry-mid-wizard. If the catalog fetch raises
    ``TokenExpiredError`` (provider rejected the token with 401), fires
    an ``onboarding_step8_token_expired`` audit event and redirects to
    ``/onboarding/step-7?error=token_expired`` so the banner
    nudges the user to re-grant. The wizard_session is read-only on
    this path, so ``state.selected_assets`` and ``state.friendly_names``
    survive the re-OAuth round-trip.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    # follow-up — block re-entry after the wizard is done.
    _done_redirect = _redirect_if_wizard_done(username)
    if _done_redirect is not None:
        return _done_redirect

    from asset_management.web.services.connector_catalog_service import (
        ConnectorCatalogService,
    )
    from asset_management.web.services.connector_scope_manifest_service import (
        ConnectorScopeManifestService,
    )
    from asset_management.web.services.onboarding_branching_service import (
        OnboardingBranchingService,
    )
    from asset_management.web.services.user_profile_service import UserProfileService
    from asset_management.web.services.wizard_asset_catalog_service import (
        TokenExpiredError,
        WizardAssetCatalogService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    active = WizardSessionService.get_active(username, "onboarding") or {}
    state = dict(active.get("state") or {})
    selected_connector_id = (state.get("selected_connector_id") or "").strip()

    if not selected_connector_id:
        return redirect(url_for("analytics.onboarding_step6_page"))

    if ConnectorScopeManifestService.get(selected_connector_id) is None:
        # Same gate step-5 uses — a stale wizard_session naming a
        # connector without a manifest gets bounced back to step-4
        # rather than rendering an empty board.
        return redirect(url_for("analytics.onboarding_step6_page"))

    tile = ConnectorCatalogService.get(selected_connector_id)
    connector_name = tile.name if tile else selected_connector_id

    # if the OAuth token expired between the step-5 callback
    # and now (rare but possible — long-lived Meta tokens rotate, and a
    # user can take hours to pick accounts), the catalog fetch raises
    # TokenExpiredError. Bounce to step-5 with the
    # ?error=token_expired banner so the user re-grants without losing
    # the selections they've already persisted on prior visits — the
    # wizard_session's state.selected_assets / state.friendly_names
    # are never touched on this path, so they survive the re-OAuth
    # round-trip automatically. The 401 surface lives on
    # list_for_session today, but selectable_ids / default_selected_ids
    # both delegate to list_for_session internally, so any of the
    # three may raise — wrap all three calls in one guard rather than
    # threading the same try/except through each call site.
    try:
        assets = WizardAssetCatalogService.list_for_session(
            username=username,
            connector_id=selected_connector_id,
        )

        # Resume / refresh: a prior visit's selection wins over the
        # service's default_selected hint so a user who unchecked
        # everything doesn't see those rows re-checked on refresh. An
        # empty string in the wizard_session means "no prior visit" —
        # fall through to the defaults.
        prior_selection_raw = state.get("selected_assets")
        if isinstance(prior_selection_raw, list):
            prior_selection = frozenset(str(x) for x in prior_selection_raw)
            use_prior = True
        else:
            prior_selection = frozenset()
            use_prior = False

        default_selected = WizardAssetCatalogService.default_selected_ids(
            username=username,
            connector_id=selected_connector_id,
        )
        selected_ids = prior_selection if use_prior else default_selected

        # a stale wizard_session may still carry an asset id
        # that's since been disabled by Meta. Intersect with the current
        # selectable allowlist so the rendered "X of N selected" footer
        # excludes disabled rows and the row's visual state stays
        # consistent (no row both .selected and .disabled). The catalog's
        # selectable_ids is the source-of-truth: disabled rows are
        # excluded there.
        selectable = WizardAssetCatalogService.selectable_ids(
            username=username,
            connector_id=selected_connector_id,
        )
        selected_ids = frozenset(selected_ids) & selectable
    except TokenExpiredError:
        AuditLogService.log_event(
            username,
            "onboarding_step8_token_expired",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(f"analytics_event=wizard.step8.token_expired " f"connector_id={selected_connector_id}"),
        )
        # canonical step-6 errored event for the token-expired
        # branch. The user is bounced to step-5 to re-OAuth; the funnel
        # counts this as a step-6 error, not a step-5 retry.
        WizardAnalyticsService.emit(
            step="step-8",
            action=ACTION_ERRORED,
            context=WizardAnalyticsService.build_context(
                username=username,
                wizard_session=active or None,
                connector_override=selected_connector_id,
            ),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"error_code": "token_expired"},
        )
        return redirect(url_for("analytics.onboarding_step7_page", error="token_expired"))

    # prior friendly-name aliases survive refresh. A non-
    # dict shape (corrupted state, legacy session) falls back to
    # empty so the template renders with blank inputs.
    prior_friendly_raw = state.get("friendly_names")
    friendly_names = prior_friendly_raw if isinstance(prior_friendly_raw, dict) else {}

    # agency org-shared vs workspace-scoped toggle.
    # Branching defaults are deterministic from account_type, so
    # refresh resolves the same disclosure state the user saw before.
    # Prior visit's ``state.org_shared`` (a real bool — neither None
    # nor "missing") wins over the default so an agency owner who
    # un-shared and refreshed doesn't see the box re-checked.
    account_type = state.get("account_type") or UserProfileService.get_account_type(username)
    defaults = OnboardingBranchingService.for_account_type(account_type)
    prior_org_shared = state.get("org_shared")
    if isinstance(prior_org_shared, bool):
        org_shared_checked = prior_org_shared
    else:
        org_shared_checked = defaults.step6_org_shared_default

    AuditLogService.log_event(
        username,
        "onboarding_step8_entered",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event=wizard.step8.entered " f"connector_id={selected_connector_id} " f"asset_count={len(assets)}"
        ),
    )

    # canonical envelope event alongside the pre-existing rich
    # ``wizard.step8.entered`` above. asset_count is carried through as
    # extra-detail since funnel queries that slice by zero-assets vs
    # populated-list still want to read it without joining the rich
    # event.
    WizardAnalyticsService.emit(
        step="step-8",
        action=ACTION_ENTERED,
        context=WizardAnalyticsService.build_context(
            username=username,
            wizard_session=active or None,
            account_type=account_type,
            connector_override=selected_connector_id,
        ),
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
        extra={"asset_count": len(assets)},
    )

    # zero-accounts empty state. The funnel slice needs a
    # distinct event to count users who land on step 6 with no ad
    # accounts under their login (the "connected but Meta surfaced
    # nothing" path) separately from users who simply haven't reached
    # the table yet. Fires only when the catalog fetch succeeded AND
    # returned empty — the TokenExpiredError branch above redirects
    # before reaching here, so a zero count on this line is genuine
    # "Meta returned no accounts" rather than a request-level failure.
    if not assets:
        AuditLogService.log_event(
            username,
            "onboarding_step8_zero_accounts",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(f"analytics_event=wizard.step8.zero_accounts " f"connector_id={selected_connector_id}"),
        )

    return _render_step6(
        username=username,
        connector_id=selected_connector_id,
        connector_name=connector_name,
        assets=assets,
        selected_ids=selected_ids,
        friendly_names=friendly_names,
        friendly_name_errors={},
        show_org_shared_toggle=defaults.step6_show_advanced_org_shared_toggle,
        org_shared_checked=org_shared_checked,
    )


@analytics_bp.route("/onboarding/step-8", methods=["POST"])
def onboarding_step8_post():
    """Finish-setup / Skip handoff for step 6.

    Two ``action`` values:

    * ``finish`` — user clicked "Finish setup". Intersects the
      submitted ``selected_asset_id`` form values with the
      server-side allowlist (so a forged POST can't slip a
      disabled or unknown asset id past the UX gate), persists
      the cleaned list to ``wizard_session.state.selected_assets``,
      flips ``current_step`` to ``step-7`` (the "Building your
      first dashboard…" surface — a downstream ticket; until that
      ships the Finish path completes the wizard), and soft-
      redirects to /dashboard.
    * ``skip`` — user clicked "Skip — set up later". No selection
      mutation; calls ``WizardSessionService.complete`` so the
      dashboard's empty-state checklist takes over from
      here. Redirects to /dashboard.

    Any other ``action`` (including missing) is treated as skip —
    safer default since this route's only state mutation on skip
    is the terminal status flip.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    csrf_token = request.form.get("csrf_token", "")
    if not auth_module.validate_csrf_token(csrf_token):
        abort(403)

    # follow-up — block re-entry POSTs after the wizard is done.
    _done_redirect = _redirect_if_wizard_done(username)
    if _done_redirect is not None:
        return _done_redirect

    from asset_management.web.services.connector_scope_manifest_service import (
        ConnectorScopeManifestService,
    )
    from asset_management.web.services.onboarding_branching_service import (
        OnboardingBranchingService,
    )
    from asset_management.web.services.user_profile_service import UserProfileService
    from asset_management.web.services.wizard_asset_catalog_service import (
        WizardAssetCatalogService,
    )
    from asset_management.web.services.wizard_session_service import (
        WizardSessionService,
    )

    active = WizardSessionService.get_active(username, "onboarding") or {}
    state = dict(active.get("state") or {})
    selected_connector_id = (state.get("selected_connector_id") or "").strip()

    if not selected_connector_id or not ConnectorScopeManifestService.get(selected_connector_id):
        return redirect(url_for("analytics.onboarding_step6_page"))

    action = (request.form.get("action") or "").strip().lower()

    # resolve branching defaults so the POST shares the
    # same agency / non-agency dispatch as the GET. Non-agency users
    # never see the toggle; their persisted ``org_shared`` is the
    # branching default regardless of whatever the form carries (or
    # doesn't), so a forged POST naming ``org_shared=on`` from a
    # non-agency session can't elevate the connection to org-shared.
    account_type = state.get("account_type") or UserProfileService.get_account_type(username)
    defaults = OnboardingBranchingService.for_account_type(account_type)
    if defaults.step6_show_advanced_org_shared_toggle:
        org_shared = (request.form.get("org_shared") or "").strip().lower() == "on"
    else:
        org_shared = defaults.step6_org_shared_default

    if action != "finish":
        # Skip path — no selection persisted, wizard completes so
        # the dashboard's "Connect your first source" checklist
        # becomes the resume surface.
        try:
            WizardSessionService.complete(username, "onboarding")
            # follow-up — clear the session gate in lockstep
            # with the ES status flip. The skip-from-step-8 hands off
            # to /dashboard (line below); without the pop, verify_auth
            # would bounce the user right back into the wizard.
            session.pop("wizard_pending", None)
        except Exception as e:  # noqa: BLE001 — same fail-soft policy as earlier steps
            logging.warning("step-6 wizard-complete failed for %r: %s", username, e)

        AuditLogService.log_event(
            username,
            "onboarding_step8_skip",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"analytics_event=wizard.step8.skip " f"connector_id={selected_connector_id} " f"action={action or '(none)'}"
            ),
        )
        # canonical step-6 skip event.
        WizardAnalyticsService.emit(
            step="step-8",
            action=ACTION_SKIPPED,
            context=WizardAnalyticsService.build_context(
                username=username,
                wizard_session=active or None,
                account_type=account_type,
                connector_override=selected_connector_id,
            ),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"action": action or "(none)"},
        )
        return redirect(url_for("analytics.dashboard_page"))

    # Finish path — intersect the submitted ids with the server-side
    # allowlist. A forged POST naming a disabled or unknown id is
    # silently dropped (not 400) so the user still finishes setup;
    # the audit detail records both the submitted and the accepted
    # counts so a regression that drops everything is visible in the
    # log.
    submitted_ids = [s.strip() for s in request.form.getlist("selected_asset_id") if s.strip()]
    allowlist = WizardAssetCatalogService.selectable_ids(
        username=username,
        connector_id=selected_connector_id,
    )
    accepted_ids = sorted(set(submitted_ids) & allowlist)

    # friendly-name aliasing. Parse ``friendly_name__<id>``
    # form fields into ``{provider_asset_id: alias}``, then validate.
    # The allowlist for the friendly-name validator is the union of
    # accepted_ids (what the user ticked) AND the full asset catalog —
    # an alias on a row the user didn't tick this submit is still a
    # legitimate edit (they may un-tick + re-tick later), so we keep
    # those entries rather than dropping them. The catalog's
    # ``selectable_ids`` is the right allowlist: it excludes disabled
    # rows (no aliasing on a row that can't be imported anyway) and
    # forged ids.
    submitted_friendly: dict[str, str] = {}
    for key, value in request.form.items():
        if key.startswith("friendly_name__"):
            asset_id = key[len("friendly_name__") :]
            submitted_friendly[asset_id] = value or ""

    friendly_result = WizardAssetCatalogService.validate_friendly_names(
        submitted=submitted_friendly,
        selectable_ids=allowlist,
    )

    if not friendly_result.ok:
        # Conflict path — re-render step-6 inline with the error
        # messages keyed by provider_asset_id. Form values survive
        # because we re-render rather than redirect; the user fixes
        # the conflict and resubmits without losing their other edits.
        from asset_management.web.services.connector_catalog_service import (
            ConnectorCatalogService,
        )

        tile = ConnectorCatalogService.get(selected_connector_id)
        connector_name = tile.name if tile else selected_connector_id
        assets = WizardAssetCatalogService.list_for_session(
            username=username,
            connector_id=selected_connector_id,
        )
        AuditLogService.log_event(
            username,
            "onboarding_step8_friendly_name_conflict",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"analytics_event=wizard.step8.friendly_name_conflict "
                f"connector_id={selected_connector_id} "
                f"error_count={len(friendly_result.errors)}"
            ),
        )
        # canonical step-6 errored event for friendly-name
        # conflicts. The user stays on step-6 (the rerender path) so
        # this is the only step-6 event for the request; the funnel
        # rebuild reads this as "stayed on step-6, retry imminent".
        WizardAnalyticsService.emit(
            step="step-8",
            action=ACTION_ERRORED,
            context=WizardAnalyticsService.build_context(
                username=username,
                wizard_session=active or None,
                account_type=account_type,
                connector_override=selected_connector_id,
            ),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"error_code": "friendly_name_conflict", "error_count": len(friendly_result.errors)},
        )
        return _render_step6(
            username=username,
            connector_id=selected_connector_id,
            connector_name=connector_name,
            assets=assets,
            # On conflict, surface the user's most recent intent:
            # whatever they ticked stays ticked.
            selected_ids=frozenset(accepted_ids),
            # Reflect submitted aliases (un-cleaned) so the inputs
            # keep the user's actual typing — preserving casing,
            # leading/trailing spaces, etc. — until they fix the
            # conflict.
            friendly_names=submitted_friendly,
            friendly_name_errors=friendly_result.errors,
            # preserve the user's most recent org-shared
            # intent across the conflict re-render so they don't lose
            # an uncheck while fixing a friendly-name conflict.
            show_org_shared_toggle=defaults.step6_show_advanced_org_shared_toggle,
            org_shared_checked=org_shared,
        )

    # Happy path — both selection and friendly-names validate.
    state["selected_assets"] = accepted_ids
    state["friendly_names"] = friendly_result.cleaned
    # persist the org-shared decision alongside the asset
    # selection. The Sources page reads this value out of de_connection
    # (not wizard_session) once the connection-write below lands, but
    # we keep the wizard_session copy too so a resumed wizard session
    # can re-render the toggle's checked state without re-querying ES.
    state["org_shared"] = org_shared
    state["current_step"] = "step-7"
    try:
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-7",
            state=state,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("step-6 session-save failed for %r: %s", username, e)

    # write the de_connection + de_connection_asset rows so
    # the Sources page (below in this file) can list the connection and
    # toggle org_shared post-onboarding. The OAuth token itself is
    # written by the wizard-aware OAuth callback
    # (onboarding_step7_oauth_callback ➜ ConnectionService.upsert_connection,
    # follow-up), so the natural-key (principal, provider,
    # provider_account_id) row already exists and is refreshed in place
    # rather than recreated. When the callback ran before this step the
    # connection lands ``status=active``; on the cold path (callback
    # path failed but the user still progressed past reconciliation), it
    # lands ``status=pending_token`` and the operator can retry from the
    # Sources page. Provider mapping: meta_ads (the only wizard-shipped
    # connector today) writes a 'facebook' provider row + fb_ad_account
    # asset rows.
    #
    # Failure here MUST NOT block wizard completion — the user has
    # already finished setup from their perspective. We log a warning,
    # surface a follow-up retry affordance on the Sources page (when
    # selected_assets is set on wizard_session but no matching
    # de_connection row exists, the page renders a 'finish setup'
    # button — out of scope for this commit, open follow-up).
    from asset_management.web.services.connection_service import (
        ASSET_TYPE_FB_AD_ACCOUNT,
        ConnectionService,
        PROVIDER_FACEBOOK,
    )

    connector_to_provider = {"meta_ads": PROVIDER_FACEBOOK}
    connector_to_asset_type = {"meta_ads": ASSET_TYPE_FB_AD_ACCOUNT}
    provider = connector_to_provider.get(selected_connector_id)
    asset_type = connector_to_asset_type.get(selected_connector_id)
    connection_esid = None
    if provider and asset_type:
        try:
            # follow-up — provider_account_id is now stamped on
            # wizard_session.state by the OAuth callback (real Meta /me
            # user_id), so the natural key (principal, provider,
            # provider_account_id) matches the row the callback wrote
            # when it persisted the token. Fall back to username if a
            # session predates the callback's stamping (e.g. a wizard
            # run that completed step-5 before this code shipped).
            provider_account_id = state.get("provider_account_id") or username
            connection_result = ConnectionService.create_pending_connection(
                principal=username,
                provider=provider,
                provider_account_id=provider_account_id,
                label=f"{selected_connector_id} — {username}",
                scopes=list(state.get("granted_scopes") or []),
                org_shared=bool(org_shared),
            )
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "step-6 create_pending_connection failed for %r: %s",
                username,
                e,
            )
            connection_result = {"success": False, "error": "Could not create the connection."}

        if connection_result.get("success"):
            connection_esid = connection_result.get("esid")
            for asset_id in accepted_ids:
                try:
                    ConnectionService.upsert_connection_asset(
                        connection_esid=connection_esid,
                        asset_type=asset_type,
                        provider_asset_id=asset_id,
                        label=friendly_result.cleaned.get(asset_id) or asset_id,
                    )
                except Exception as e:  # noqa: BLE001
                    logging.warning(
                        "step-6 upsert_connection_asset failed for " "%r asset=%r: %s",
                        username,
                        asset_id,
                        e,
                    )
        else:
            logging.warning(
                "step-6 connection-write failed for %r: %s",
                username,
                connection_result.get("error"),
            )

    # slice 3 (2026-05-22) — Step 7: install the connector's
    # full template PACK into the user's workspace. Replaces the
    # legacy first-look service call which only installed
    # one dashboard per connector from a hardcoded Python registry.
    # The new path reads from the ES-backed ``de_dashboard_templates``
    # index so a teammate's curated Save-as-template work + the
    # migration job (also slice 3) are both visible at wizard-finish.
    #
    # Fail-soft like the connection write above: an install failure
    # logs a warning but does not block the redirect — the user has
    # already finished setup from their perspective, and an empty
    # /dashboard is still better than a 500. The legacy service
    # stays importable for now; slice 5 deletes it after this path
    # is green on staging.
    first_look_status = "skipped"
    first_look_dashboard_id: str | None = None  # kept so the audit-log line below keeps shape
    if accepted_ids and connection_esid:
        try:
            primary_team_esid = _resolve_primary_team_esid(username)
        except Exception as e:  # noqa: BLE001
            logging.warning("step-7 primary-team lookup raised for %r: %s", username, e)
            primary_team_esid = None

        # slice 3: the cloned dashboards need workspace_esid so
        # the tenant boundary applies. Resolve the user's
        # primary workspace; fail-soft on the lookup so a missing
        # WorkspaceMembershipService row doesn't block the wizard.
        primary_workspace_esid: str | None = None
        try:
            from asset_management.web.services.workspace_membership_service import (
                WorkspaceMembershipService,
            )

            primary_workspace_esid = WorkspaceMembershipService.get_primary_workspace(username)
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "step-7 primary-workspace lookup raised for %r: %s",
                username,
                e,
            )

        if primary_team_esid:
            try:
                from asset_management.web.services.template_service import (
                    TemplateService,
                )

                landed_ids = TemplateService.materialize_pack_for_user(
                    connector_id=selected_connector_id,
                    team_esid=primary_team_esid,
                    source_account_ids=list(accepted_ids),
                    workspace_esid=primary_workspace_esid,
                )
                if landed_ids:
                    first_look_status = "installed"
                    # Audit-log compat: keep the first landed id in
                    # the legacy variable so the existing log line
                    # downstream still has something to render.
                    first_look_dashboard_id = landed_ids[0]
                else:
                    first_look_status = "no_pack"
            except Exception as e:  # noqa: BLE001
                logging.warning(
                    "step-7 pack install raised for %r: %s",
                    username,
                    e,
                )
                first_look_status = "exception"

            # Defect A — wizard cohort FB asset-bridge. Before this
            # the wizard wrote de_connection + de_connection_asset only;
            # the FB importer reads from de_asset_fb_ad_accounts +
            # de_asset_teams. With no bridge between the two stores, the
            # importer never saw the wizard user's ad-account → 0 docs
            # written but run stamped status=succeeded. Gated to meta_ads
            # because the account-wide model is FB-specific; Echo / GA4 /
            # Google Ads use the existing tokenless or fb-app-chain paths.
            if selected_connector_id == "meta_ads":
                try:
                    _bridge_meta_assets(
                        username=username,
                        team_esid=primary_team_esid,
                        connection_esid=connection_esid,
                        accepted_ids=list(accepted_ids),
                        friendly_names=friendly_result.cleaned,
                    )
                except Exception as e:  # noqa: BLE001
                    logging.warning(
                        "bridge raised for user=%r team=%s: %s",
                        username,
                        primary_team_esid,
                        e,
                    )

                # Instagram asset chain. Sibling to the
                # bridge. Walks the user's accepted ad-accounts → Pages →
                # linked IG Business accounts, writes de_asset_ig_accounts
                # rows, and stamps team_instagram_profile_esids on the
                # team. Gated internally on instagram_basic in the
                # connection's scopes (same gate uses for
                # ds_instagram). Same fail-soft envelope as so
                # an IG-side failure doesn't block the wizard redirect.
                try:
                    _bridge_instagram_assets(
                        username=username,
                        team_esid=primary_team_esid,
                        connection_esid=connection_esid,
                        accepted_ids=list(accepted_ids),
                        workspace_esid=primary_workspace_esid,
                    )
                except Exception as e:  # noqa: BLE001
                    logging.warning(
                        "bridge raised for user=%r team=%s: %s",
                        username,
                        primary_team_esid,
                        e,
                    )

                # Facebook Page asset chain. Sibling to the
                # (FB ad-account) (IG account)
                # bridges. Walks the user's accepted ad-accounts →
                # Pages, writes de_asset_fb_pages rows, and stamps
                # team_facebook_page_id + team_facebook_page_esids on
                # the team. Gated internally on pages_read_engagement
                # in the connection's scopes — partial-grant
                # short-circuits silently. Same fail-soft envelope as
                # so a Page-side failure doesn't block
                # the wizard redirect.
                try:
                    _bridge_fb_pages(
                        username=username,
                        team_esid=primary_team_esid,
                        connection_esid=connection_esid,
                        accepted_ids=list(accepted_ids),
                        workspace_esid=primary_workspace_esid,
                    )
                except Exception as e:  # noqa: BLE001
                    logging.warning(
                        "bridge raised for user=%r team=%s: %s",
                        username,
                        primary_team_esid,
                        e,
                    )
        else:
            first_look_status = "no_team"

    # distinct funnel-slice event so the analytics dashboard
    # can count agency owners who left the connection org-shared vs
    # un-shared, and so the share decision is visible separately from
    # the finish event (non-agency users fire this too but always with
    # org_shared=false, which keeps the funnel row count = finish count
    # for the slice). ``toggle_shown`` distinguishes a user who
    # actually saw the disclosure from a non-agency user whose value
    # was server-default.
    AuditLogService.log_event(
        username,
        "onboarding_step8_org_shared_decision",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event=wizard.step8.org_shared_decision "
            f"connector_id={selected_connector_id} "
            f"account_type={account_type} "
            f"org_shared={'true' if org_shared else 'false'} "
            f"toggle_shown={'true' if defaults.step6_show_advanced_org_shared_toggle else 'false'}"
        ),
    )

    AuditLogService.log_event(
        username,
        "onboarding_step8_finish",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event=wizard.step8.finish "
            f"connector_id={selected_connector_id} "
            f"submitted_count={len(submitted_ids)} "
            f"accepted_count={len(accepted_ids)} "
            f"friendly_name_count={len(friendly_result.cleaned)} "
            f"org_shared={'true' if org_shared else 'false'} "
            f"connection_esid={connection_esid or '(none)'} "
            f"first_look={first_look_status} "
            f"first_look_id={first_look_dashboard_id or '(none)'}"
        ),
    )

    # follow-up — flip the ES wizard session to ``completed``
    # so verify_auth's wizard_pending gate stops bouncing this user
    # back into the wizard, and clear the session-level flag in lock-
    # step. Pre-fix the finish path emitted the analytics event but
    # never wrote the terminal status, so a refreshed session would
    # re-evaluate as WIZARD_INCOMPLETE on next login. Fail-soft: a
    # status-write blip should never block the redirect to /dashboard.
    try:
        WizardSessionService.complete(username, "onboarding")
        session.pop("wizard_pending", None)
    except Exception as e:  # noqa: BLE001
        logging.warning("step-8 finish wizard-complete failed for %r: %s", username, e)

    # canonical step-6 completion. Sits alongside the rich
    # ``wizard.step8.finish`` event above; the rich event carries the
    # detailed counts, the canonical event carries the five-field
    # schema so the funnel "finish rate per cohort" query doesn't
    # need to read the rich event.
    WizardAnalyticsService.emit(
        step="step-8",
        action=ACTION_COMPLETED,
        context=WizardAnalyticsService.build_context(
            username=username,
            wizard_session=WizardSessionService.get_active(username, "onboarding"),
            account_type=account_type,
            connector_override=selected_connector_id,
        ),
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
        extra={"accepted_count": len(accepted_ids)},
    )

    # on-demand FB import kickoff + redirect to building
    # screen.
    #
    # follow-up — dropped the ``first_look_status in (...)``
    # gate. Pre-fix the kickoff was suppressed whenever the first-look
    # dashboard template clone failed (most commonly: wizard user has
    # no primary team membership yet → ``first_look_status='no_team'``).
    # That conflated two independent surfaces: the dashboard-template
    # clone is a UI nicety; the import kickoff is the actual data
    # fetch. Gating the data fetch on the template clone meant users
    # with no team saw the "Data is syncing" pill spin forever against
    # an empty index. The right gate is just (connection_esid AND
    # accepted_ids) — if the user picked accounts and we have a
    # connection to fetch them with, kick off the import. The dashboard
    # may render empty until a team assignment lands, but the data is
    # in ES waiting either way.
    if connection_esid and accepted_ids:
        kickoff_result = _kickoff_on_demand_backfill(
            username=username,
            connection_esid=connection_esid,
            connector_id=selected_connector_id,
            asset_count=len(accepted_ids),
        )
        if kickoff_result.get("ok"):
            return redirect(
                url_for(
                    "analytics.onboarding_step7_building_page",
                    connector_id=selected_connector_id,
                    asset_count=len(accepted_ids),
                    next="/dashboard",
                )
            )
        # Kickoff failed (Popen raised, no team, etc). Fall through
        # to the direct /dashboard redirect — the user still gets to
        # their dashboard, just without the building-screen interlude.
        # The pill will not animate either, but the first-look
        # banner still renders and the cron will catch
        # up overnight.

    return redirect(url_for("analytics.dashboard_page"))


# ============================================
# BILLING SECTION
# ============================================
# (epic) turns the single /billing placeholder
# into a self-serve Billing *section* with three sub-pages — Payment,
# Plans, Current Plan — reachable by every authenticated role from a
# Billing button in the top nav. This ticket delivers navigation +
# routing + empty page scaffolds only; the real content arrives in
# (Payment) (Plans) (Current Plan).
#
# ``/billing`` is the section landing and redirects to the default
# sub-page (Current Plan). The three sub-pages share one template
# (``billing_section.html``) and differ only by an ``active`` marker
# that selects the highlighted tab + the scaffold body. The
# "Upgrade to add more sources" upgrade-pitch copy is folded into the
# Plans sub-page, where it belongs thematically; the old standalone
# ``billing.html`` is left unused (separable cleanup).
#
# All four routes are authenticated-only (an anonymous click bounces to
# /login first) but role-agnostic — the per-path skip in
# ``check_route_authorization`` lets dashboard-only users in, mirroring
# /profile. nginx already proxies /billing (exact) and /billing/ (prefix)
# to Flask, so no nginx change is needed.

# Default sub-page the bare /billing landing redirects to.
_BILLING_DEFAULT_SUBPAGE = "current"


def _billing_access_guard():
    """Shared gate for the Billing section.

    Returns a redirect/abort response the caller should return, or
    ``None`` when the current user may proceed. Two gates:

    * not signed in → redirect to /login (verify_auth already enforces a
      session upstream, but this keeps the route self-contained);
    * signed in but not superuser / workspace_owner → ``abort(403)``.
      For an authenticated page request the app-level @errorhandler(403)
      turns that into a friendly redirect to an allowed page, so a
      non-owner who deep-links to /billing/* lands somewhere they can see
      rather than on a raw 403.
    """
    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))
    if not auth_module.user_can_access_billing(username, session.get("roles", [])):
        abort(403, description="The Billing section is limited to workspace owners.")
    return None


def _billing_back_url():
    """Back-link target for the Billing pages (arrow + "Back"), mirroring the
    profile back-link. Uses a same-origin Referer captured on entry (stashed
    in the session so it survives tab-switches between the billing sub-pages),
    falling back to the dashboard. Referers that are themselves /billing URLs
    are ignored so switching tabs doesn't overwrite the original entry point.
    """
    referer = request.headers.get("Referer", "") or ""
    same_origin = referer.startswith(request.host_url) or (referer.startswith("/") and not referer.startswith("//"))
    # Capture the entry Referer once, excluding the billing sub-pages
    # themselves AND the sibling profile pages. Without the profile guard,
    # navigating /billing ↔ /profile made each page stash the other as its
    # back-target, so Back ping-ponged between them instead of returning to
    # the real entry point (dashboard / console).
    is_sibling = "/billing" in referer or "/profile" in referer
    if referer and same_origin and not is_sibling and not session.get("_billing_back_url"):
        session["_billing_back_url"] = referer
    return session.get("_billing_back_url") or "/dashboard"


@analytics_bp.route("/billing")
def billing_page():
    """Billing section landing — redirects to the default sub-page.

    Keeps the stable ``/billing`` URL (the step-4 "Upgrade"
    CTA and external links still resolve) and the audit signal that
    captured who entered Billing. The real destination is the Current
    Plan sub-page. Restricted to superuser + workspace_owner.
    """
    guard = _billing_access_guard()
    if guard is not None:
        return guard
    username = session.get("username", "")
    AuditLogService.log_event(
        username,
        "billing_page_view",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail="",
    )
    return redirect(url_for("analytics.billing_current_page"))


# Plan keys are the stable identifiers carried in the ``?plan=`` query
# param from the Plans page into the Payment page. ``business`` is the
# default when no (or an unrecognized) plan is supplied.
_BILLING_DEFAULT_PLAN = "business"

# the sponsored-license application (Google Form). Single source
# for every in-app nonprofit CTA; the marketing site's /pricing "Apply for a
# sponsored license" button points at the same form. Applications are
# reviewed manually; approval = the org gets comped (subscription_state's
# comped_active flag).
NONPROFIT_APPLY_FORM_URL = (
    "https://docs.google.com/forms/d/e/1FAIpQLSe1ji3nSay-WRyqijU0KZTDEt-YkvUDsJF6kDRC6tV31_Ncvg/viewform"
)

# Shared caps (epic): both plans cap at 6 connections, 1 ad
# account per connection (bumped 5→6 item 5a). The 6/1 limits
# are caps, not the differentiator between plans — price is. A "Contact us
# for more connections" CTA sits on both cards (add-on pricing for extra
# connections beyond 6 is item 5b, deferred).
_BILLING_CONNECTION_CAP = 6


def _caps_enabled_for_templates() -> bool:
    """single read of ``SubscriptionStateService.caps_enabled`` for
    the billing templates' cap copy (imported lazily like every other billing
    service reference in this module)."""
    from asset_management.web.services.subscription_state_service import SubscriptionStateService

    return SubscriptionStateService.caps_enabled()


# item 5b — self-serve add-on pricing: $10 per connection beyond the
# included cap (the displayed rate on the Plans CTA + the Current Plan add-on
# control). The discounted "mission" rate is NOT a displayed price — it's
# applied via the add-on-scoped Stripe coupon (KESH67WG, 50% off) when a
# mission org enters their code, and is deliberately NOT surfaced in the UI so
# for-profits never see it (decision 2026-06-09). So there is no mission price
# CONSTANT here — the rate lives in Stripe, driven by a user-entered code.
_BILLING_ADDON_CONNECTION_PRICE = 10

# (2026-06-16, user decision) — hard cap on self-serve add-on
# connections: an org may hold at most this many. The stepper offers
# included..included+this and the buy route refuses any TARGET above this, so
# the total can't exceed it regardless of client tampering. Reductions are
# unaffected. Beyond this the UI points at "contact us for volume pricing"
# raised from 4 to 100 so the self-serve range is effectively
# open; volume pricing is the sales motion past it.
_BILLING_ADDON_MAX = 100


def _billing_plans():
    """Plan catalog — one source of truth for the Plans comparison and the
    Payment summary.

    Prices come from config (``BILLING_BUSINESS_PRICE_USD`` /
    ``BILLING_NONPROFIT_PRICE_USD``) so the comparison card, the payment
    summary, and later the real Stripe amount never drift. Returns an
    ordered list of plan dicts keyed by ``key`` (the ``?plan=`` value).

    ``self_serve`` flags whether selecting the plan routes into the
    self-serve Payment flow. Nonprofit is **not** self-serve in this phase:
    automatic EIN verification is not yet built, so the nonprofit
    card surfaces the sales-contact eligibility route instead (per the
     AC's "or surfaces the sales-contact route").
    """
    business_price = config.BILLING_BUSINESS_PRICE_USD
    nonprofit_price = config.BILLING_NONPROFIT_PRICE_USD
    # v1 funnel: eligibility is proven through the sponsored-license
    # application form (the same Google Form the marketing site's /pricing
    # "Apply for a sponsored license" CTA uses; reviewed manually by Kavanah).
    # Replaces the old sales-mailto route that predated the form.
    apply_form_url = NONPROFIT_APPLY_FORM_URL
    # Display flags:
    #   show_name_eyebrow — Business drops the "Business" eyebrow label
    #     (it's the default/primary tile; the name is redundant there).
    #   show_price        — nonprofit hides the dollar amount and shows a
    #     "Contact us for non-profit pricing" line instead (price is by
    #     sales conversation, not self-serve).
    return [
        {
            "key": "business",
            # Display name only. Renamed For-profit -> Business 2026-06-05;
            # the internal key was also renamed forprofit -> business (the
            # key is not persisted, so this is safe).
            "name": "Business",
            "price_usd": business_price,
            "price_display": f"${business_price:,.2f}",
            "blurb": "For teams and companies running their own marketing.",
            "self_serve": True,
            "eligibility": None,
            "show_name_eyebrow": False,
            "show_price": True,
            "price_contact_text": "",
            "apply_url": "",
        },
        {
            "key": "nonprofit",
            "name": "Nonprofit",
            "price_usd": nonprofit_price,
            "price_display": f"${nonprofit_price:,.2f}",
            # item 2: dropped the "Discounted rate for verified
            # 501(c)(3) organizations." second sentence entirely; the tile
            # now shows only the "Contact us for Christian ministry pricing"
            # contact line. Empty blurb -> the template suppresses the <p>.
            "blurb": "",
            "self_serve": False,
            # eligibility is proven via the sponsored-license
            # application form (manual review by Kavanah), replacing the old
            # sales-mailto route. (automatic EIN verification) remains
            # unbuilt; the form + manual review IS the eligibility check now.
            "eligibility": "Apply for a sponsored license to confirm your ministry's eligibility.",
            "show_name_eyebrow": False,
            "show_price": False,
            # item 2 kept the "Christian ministry" framing;
            # swaps "contact us" for the application-form language.
            "price_contact_text": "Sponsored license for Christian ministries",
            "apply_url": apply_form_url,
        },
    ]


def _billing_plan(plan_key):
    """Resolve a ``?plan=`` key to its catalog entry, defaulting to the
    Business plan for a missing/unknown key."""
    plans = {p["key"]: p for p in _billing_plans()}
    return plans.get(plan_key, plans[_BILLING_DEFAULT_PLAN])


def _billing_plan_price_id(plan_key):
    """The Stripe Price id for a plan key — so coupon
    validation can resolve the plan's product and reject a coupon scoped to a
    different product. Nonprofit → its price; everything else → Business."""
    if plan_key == "nonprofit":
        return config.STRIPE_PRICE_NONPROFIT
    return config.STRIPE_PRICE_BUSINESS


_BILLING_COUNTRIES = None  # lazily-loaded, cached list of (code, name) pairs


def _billing_countries():
    """Sorted list of ``(iso2_code, name)`` pairs for the billing Country
    dropdown.

    Reads the existing ISO catalog
    (``asset_management/updates/iso_countries.json``) so the list never drifts
    from the rest of the app. The dropdown submits the 2-letter ISO code (e.g.
    ``US``) because Stripe's flexible billing REJECTS country names
    ("Country 'United States' is unknown … use a 2-character … code"). The
    catalog's ``country_iso3_code`` field actually holds the 2-letter code.
    Sorted by display name. Cached; empty list on any error so the template
    degrades rather than crashing checkout."""
    global _BILLING_COUNTRIES
    if _BILLING_COUNTRIES is not None:
        return _BILLING_COUNTRIES
    import json
    import os

    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "updates", "iso_countries.json"))
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        pairs = sorted(
            (
                ((row.get("country_iso3_code") or "").strip().upper(), (row.get("country_title") or "").strip())
                for row in data
                if row.get("country_iso3_code") and row.get("country_title")
            ),
            key=lambda p: p[1],
        )
    except Exception as exc:  # noqa: BLE001 — never let a missing catalog break checkout
        logging.warning("billing country list load failed (%s): %s", path, exc)
        pairs = []
    _BILLING_COUNTRIES = pairs
    return pairs


def _billing_payment_summary(plan_key=_BILLING_DEFAULT_PLAN):
    """Build the subscription-summary dict for the Payment page (,
    plan-aware as of).

    The summary reflects the plan chosen on the Plans page and carried in
    via ``?plan=`` (Business $49.95 or nonprofit $20). Prices come from
    the plan catalog (``_billing_plans``), the single source of truth.

     follow-up (v1 simplification): the Subtotal, Coupon, and Tax
    lines were removed from the page — a flat single-line-item price, no tax
    collected, no promo codes yet — so the summary is just the plan + Total
    (== unit price). Money values are pre-formatted strings.
    """
    plan = _billing_plan(plan_key)
    unit_price = plan["price_usd"]
    quantity = 1
    total = unit_price * quantity
    return {
        "plan_key": plan["key"],
        "plan_name": plan["name"],
        "cadence_caption": "Billed Monthly",
        "limits_caption": (
            f"{_BILLING_CONNECTION_CAP} data sources, 1 account/source"
            if _caps_enabled_for_templates()
            else "Unlimited data sources"
        ),
        "quantity": quantity,
        "unit_price": f"${unit_price:,.2f}",
        "total": f"${total:,.2f}",
    }


@analytics_bp.route("/billing/payment")
def billing_payment_page():
    """Billing → Payment sub-page.

    Renders the subscription summary + Stripe Elements card form. The
    publishable key reaches the template so Stripe.js can mount the
    Payment Element; an empty key degrades to a "payments not configured"
    notice. Card data is captured entirely by Stripe Elements — no
    PAN/CVV touches our server.
    """
    guard = _billing_access_guard()
    if guard is not None:
        return guard
    # honor the plan chosen on the Plans page (?plan=). Only
    # self-serve plans may reach checkout; a non-self-serve plan (nonprofit,
    # which is gated behind manual sales eligibility) falls
    # back to the default Business summary rather than offering a $20
    # self-serve checkout that bypasses verification.
    requested_plan = _billing_plan(request.args.get("plan", _BILLING_DEFAULT_PLAN))
    plan_key = requested_plan["key"] if requested_plan["self_serve"] else _BILLING_DEFAULT_PLAN
    # polish 2026-06-05 — if the subscription is already active (paid
    # OR comped), don't show the card form: paying again would create a
    # duplicate Stripe subscription / double charge. Show a notice + a link to
    # the Current Plan page instead. (The create-subscription POST enforces
    # the same guard server-side, so a stale tab can't bypass this.)
    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
        STATE_ACTIVE,
    )

    _pay_state = SubscriptionStateService.get_state(session.get("username", ""))
    already_active = _pay_state["state"] == STATE_ACTIVE
    # A PAID-active user can CHANGE their card (update form); a comped-active
    # user has no Stripe card, so they just see the already-active notice.
    # (2026-06-11): PAST_DUE also gets the UPDATE-CARD form — the
    # subscription is still live in Stripe's retry window and recovering it
    # means updating the default card so the retry succeeds. Previously
    # past_due fell through to the SUBSCRIBE form, which would have created a
    # DUPLICATE subscription alongside the still-live past_due one.
    from asset_management.web.services.subscription_state_service import STATE_PAST_DUE

    paid_active = (already_active and _pay_state.get("is_paid")) or _pay_state["state"] == STATE_PAST_DUE
    # Pre-fill the update-card billing fields from the Stripe Customer so a
    # paid user doesn't re-type their address on every card change (the card
    # itself is never pre-filled). Only fetched for the paid-active path that
    # renders the update form; fail-soft → None (form renders blank).
    update_prefill = _billing_update_prefill(session.get("username", "")) if paid_active else None
    # 2026-06-09 — if the user already has a paid subscription WITH a
    # coupon, pre-load this summary box with their actual discounted Total +
    # a discount line (so it matches the Current Plan page on refresh, not just
    # after clicking Apply). Fail-soft: no discount info just shows list price.
    summary = _billing_payment_summary(plan_key)
    # a paid-active subscriber whose active subscription is 100%-off
    # ($0 total) is charged nothing, so the "Update payment method" card form is
    # pointless — there's no upcoming charge to put a card behind. Treat them
    # like a comped user (show the no-payment-required notice instead of the
    # card form). Detected from the live subscription summary: a discount that
    # zeroes the total (subtotal > 0, total == 0).
    fully_discounted = False
    if paid_active:
        _sub = _billing_subscription_summary(session.get("username", ""))
        if _sub and _sub.get("has_discount"):
            sub_cents = _sub.get("amount_subtotal") or 0
            tot_cents = _sub.get("amount_total") or 0
            # Gate on sub_cents only — a 100%-off coupon makes tot_cents == 0
            # (falsy), which would skip the preload for the biggest discount
            #.
            if sub_cents:
                summary["total"] = f"${tot_cents / 100:,.2f}"
                summary["discount_amount"] = f"-${(sub_cents - tot_cents) / 100:,.2f}"
                fully_discounted = tot_cents == 0
    return render_template(
        "billing_section.html",
        username=session.get("username", ""),
        active="payment",
        summary=summary,
        stripe_enabled=config.STRIPE_ENABLED,
        stripe_publishable_key=config.STRIPE_PUBLISHABLE_KEY,
        back_url=_billing_back_url(),
        already_active=already_active,
        paid_active=paid_active,
        # True when the active sub is 100%-off ($0) — hide the
        # update-card form like a comped user (nothing will ever be charged).
        fully_discounted=fully_discounted,
        update_prefill=update_prefill,
        is_superuser="superuser" in session.get("roles", []),
        # 2026-06-08 — the template hides the price summary + card form
        # for a comped user (complimentary access, nothing to pay). Pass the
        # resolved state so ``state.is_comped`` is available on the Payment page.
        state=_pay_state,
        # (code, name) pairs for the billing Country dropdown; the
        # dropdown submits the ISO code (Stripe requires it).
        countries=_billing_countries(),
    )


# Server-side mirror of the client billing-field character filter
#. The Payment page strips out-of-class
# characters as the user types, but that is a UX layer only — a crafted /
# non-browser POST can still send anything. These patterns enforce the SAME
# allowed-character classes on the server so invalid input is rejected
# (400) before any Stripe call. Unicode-aware: À-ɏ covers the
# accented Latin letters common in names + place names. Empty values pass
# here (the required-field / Stripe checks own "missing").
import re as _billing_re  # noqa: E402 — localized to the billing helpers

_BILLING_FIELD_PATTERNS = {
    # name / city / state / country: letters, space, . ' -
    "name": _billing_re.compile(r"^[A-Za-zÀ-ɏ .'\-]*$"),
    # company: letters, digits, space, & . , ' - ( )
    "company": _billing_re.compile(r"^[A-Za-zÀ-ɏ0-9 &.,'()\-]*$"),
    # address: letters, digits, space, . , ' - / # &
    "address": _billing_re.compile(r"^[A-Za-zÀ-ɏ0-9 .,'/#&\-]*$"),
    # postal/ZIP: letters, digits, space, hyphen
    "postal": _billing_re.compile(r"^[A-Za-zÀ-ɏ0-9 \-]*$"),
    # coupon/promo code: letters, digits, hyphen, underscore (Stripe promo
    # codes are alphanumeric; no spaces). Mirrors the client-side filter.
    "coupon": _billing_re.compile(r"^[A-Za-z0-9_\-]*$"),
}


def _billing_field_invalid(value, kind):
    """Return True iff ``value`` contains a character outside the allowed
    class for ``kind`` (name/company/address/postal). Empty/whitespace
    passes — required-ness is enforced separately."""
    pattern = _BILLING_FIELD_PATTERNS.get(kind)
    if pattern is None:
        return False
    return not pattern.match(value or "")


def _apply_coupon_to_total(unit_price, verdict):
    """Compute the discounted monthly total (float USD) from a plan ``unit_price``
    and a valid ``validate_coupon`` verdict. Mirrors how Stripe applies the
    coupon: percentage first, else a fixed amount_off (cents → dollars). Never
    goes below $0. Returns ``(new_total, discount_label)``."""
    percent = verdict.get("percent_off") or 0
    amount_cents = verdict.get("amount_off") or 0
    if percent:
        new_total = unit_price * (1 - percent / 100.0)
        # "60% off" / "12.5% off" — drop the trailing .0 on whole percents.
        pct = int(percent) if float(percent).is_integer() else percent
        label = f"{pct}% off"
    elif amount_cents:
        new_total = unit_price - (amount_cents / 100.0)
        label = f"${amount_cents / 100:.2f} off"
    else:
        new_total = unit_price
        label = verdict.get("description") or "discount"
    if new_total < 0:
        new_total = 0.0
    return new_total, label


@analytics_bp.route("/billing/payment/apply-coupon", methods=["POST"])
def billing_apply_coupon():
    """Validate a coupon code and return the discounted total for the Payment
    page.

    The customer types a coupon code and clicks Apply; the browser POSTs the
    code + the chosen plan, and this endpoint validates against Stripe and
    computes the discounted monthly total SERVER-SIDE (the price is authoritative
    here, never trusted from the client). Returns the new total + a discount
    label ("60% off") so the page can update the Pay button and show the badge.

    Responses:
      * valid   → 200 ``{"status":"ok","total":"$0.00","discount_label":"100% off",
                          "raw_total":0.0}``
      * invalid → 200 ``{"status":"invalid","message":"Invalid code."}`` (a bad
                  code is a normal outcome the page shows inline, not an error)
      * live payments off / not configured → 200 ``{"status":"unavailable"}`` so
        the page can hide the field rather than erroring.

    CSRF is enforced by the global before-request hook (not CSRF-exempt).
    """
    from flask import jsonify

    guard = _billing_access_guard()
    if guard is not None:
        return guard

    payload = request.get_json(silent=True) or {}
    code = (payload.get("coupon") or "").strip()[:64]
    plan_key = _billing_plan(payload.get("plan") or _BILLING_DEFAULT_PLAN)["key"]

    if not code:
        return jsonify({"status": "invalid", "message": "Enter a coupon code."}), 200
    if _billing_field_invalid(code, "coupon"):
        return jsonify({"status": "invalid", "message": "Invalid code."}), 200

    if not config.STRIPE_LIVE_PAYMENTS or not config.STRIPE_SECRET_KEY:
        # Stub/unconfigured host can't validate against Stripe — tell the page so
        # it leaves the field inert rather than implying a bad code.
        return jsonify({"status": "unavailable"}), 200

    from asset_management.web.services.stripe_service import StripeService, StripeError

    try:
        verdict = StripeService.validate_coupon(
            secret_key=config.STRIPE_SECRET_KEY,
            code=code,
            # Pass the plan's price so a coupon scoped to a different product
            # (e.g. the add-on-only mission coupon) is rejected here, not
            # previewed as a discount it won't actually get.
            price_id=_billing_plan_price_id(plan_key),
        )
    except StripeError:
        # Transport blip — don't claim the code is bad; let the page proceed
        # without a preview (the code is still validated again at charge time).
        return jsonify({"status": "unavailable"}), 200

    if not verdict.get("valid"):
        AuditLogService.log_event(
            session.get("username", ""),
            "billing_coupon_apply_invalid",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"reason={verdict.get('reason', '')}",
        )
        # A real code scoped to a different product gets a clearer message than
        # a flat "Invalid code.".
        msg = "That code doesn't apply to this plan." if verdict.get("reason") == "wrong_product" else "Invalid code."
        return jsonify({"status": "invalid", "message": msg}), 200

    unit_price = _billing_plan(plan_key)["price_usd"]
    new_total, label = _apply_coupon_to_total(unit_price, verdict)
    discount_amount = round(unit_price - new_total, 2)
    return (
        jsonify(
            {
                "status": "ok",
                "total": f"${new_total:,.2f}",
                "raw_total": round(new_total, 2),
                "discount_label": label,
                # For the summary box's Discount line.
                "discount_amount": f"-${discount_amount:,.2f}",
                "unit_price": f"${unit_price:,.2f}",
            }
        ),
        200,
    )


@analytics_bp.route("/billing/payment/create-subscription", methods=["POST"])
def billing_create_subscription():
    """Create/activate the Stripe subscription for the Payment page.

    The card is tokenized client-side by Stripe Elements; the browser
    POSTs only the resulting ``payment_method_id`` (+ optional coupon)
    here — never raw card data.

    CSRF is enforced by the global ``check_csrf_for_state_change``
    before-request hook (the front-end sends ``X-CSRF-Token``); this
    endpoint is deliberately NOT in ``_CSRF_EXEMPT_ENDPOINTS``.

    Behaviour is gated by ``config.STRIPE_LIVE_PAYMENTS``:
      * false (default) → **stub mode**: validate input, log the attempt,
        and return ``{"status": "stub"}`` WITHOUT changing any plan
        state. Lets dev/staging exercise the page with a Stripe *test*
        publishable key before the real secret-key + webhook work lands.
      * true → the real Stripe path (Customer/Subscription) — a guarded
        follow-up; see the TODO below.
    """
    from flask import jsonify

    guard = _billing_access_guard()
    if guard is not None:
        return guard

    username = session.get("username", "")
    payload = request.get_json(silent=True) or {}
    payment_method_id = (payload.get("payment_method_id") or "").strip()
    # Billing details collected by our own form:
    # name + optional company + address. Passed to Stripe on the Customer.
    # Trim + bound each to defend against oversized input.
    billing_name = (payload.get("name") or "").strip()[:150]
    billing_company = (payload.get("company") or "").strip()[:150]
    _addr_in = payload.get("address") or {}
    billing_address = {
        "line1": (str(_addr_in.get("line1") or "")).strip()[:200],
        "city": (str(_addr_in.get("city") or "")).strip()[:100],
        "state": (str(_addr_in.get("state") or "")).strip()[:100],
        "postal_code": (str(_addr_in.get("postal_code") or "")).strip()[:10],
        "country": (str(_addr_in.get("country") or "")).strip()[:100],
    }
    # Optional promo/coupon code. Trimmed
    # + bounded; validated against Stripe BEFORE charging in the live path below.
    billing_coupon = (payload.get("coupon") or "").strip()[:64]

    # a payment method is normally required, but a 100%-off coupon
    # brings the order total to $0 — in that case there is nothing to charge and
    # we must accept a submission with NO payment_method_id. We can't decide that
    # here (the total depends on validating the coupon against Stripe), so defer
    # the "missing payment method" rejection until after coupon resolution below.
    # The frontend toggles the card off for a $0 total; this server-side gate is
    # the authoritative check (a crafted POST with no PM and no/partial coupon
    # must still be rejected with 400).

    # Server-side character-type validation — the
    # same allowed-character classes the Payment page filters client-side,
    # enforced here so a crafted POST can't smuggle invalid characters into the
    # Stripe Customer / billing details. Reject (don't silently sanitize) so a
    # malformed payment request fails loudly rather than charging against
    # mangled data.
    for _val, _kind in (
        (billing_name, "name"),
        (billing_company, "company"),
        (billing_address["line1"], "address"),
        (billing_address["city"], "name"),
        (billing_address["state"], "name"),
        (billing_address["postal_code"], "postal"),
        (billing_address["country"], "name"),
        (billing_coupon, "coupon"),
    ):
        if _billing_field_invalid(_val, _kind):
            return jsonify({"status": "error", "message": "Billing details contain invalid characters."}), 400

    # polish 2026-06-05 — refuse a new subscription when one is already
    # active (paid OR comped). The Payment page hides the form in this case,
    # but enforce it here too: a stale tab or a direct POST must not create a
    # duplicate Stripe subscription / double charge. Checked BEFORE any Stripe
    # call so nothing is charged.
    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
        STATE_ACTIVE,
        STATE_PAST_DUE,
    )

    # (2026-06-11): PAST_DUE also blocks — that subscription is still
    # live in Stripe's retry window; a new subscription on top of it would
    # double-charge the moment the retry succeeds. The Payment page shows the
    # update-card form for past_due, so this guard should never be hit from
    # the UI — it stops stale tabs / direct POSTs.
    if SubscriptionStateService.get_state(username)["state"] in (STATE_ACTIVE, STATE_PAST_DUE):
        AuditLogService.log_event(
            username,
            "billing_payment_blocked_active",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail="already active; refused duplicate subscription",
        )
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Your subscription is already active. Manage it from the Current Plan page.",
                }
            ),
            409,
        )

    AuditLogService.log_event(
        username,
        "billing_payment_attempt",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"live={config.STRIPE_LIVE_PAYMENTS}",
    )

    if not config.STRIPE_LIVE_PAYMENTS:
        # Stub mode: no real charge, no plan change. The front-end shows a
        # "test mode" confirmation so QA can verify the full client flow.
        # A stub host can't validate the coupon against Stripe (apply-coupon
        # returns "unavailable"), so the page never suppresses the card here —
        # require a payment method as before unless a coupon was entered
        # (the $0 path is exercised on the live Stripe path).
        if not payment_method_id and not billing_coupon:
            return jsonify({"status": "error", "message": "Missing payment method."}), 400
        return (
            jsonify(
                {
                    "status": "stub",
                    "message": "Payment captured in test mode. No charge was made and your plan is unchanged.",
                }
            ),
            200,
        )

    # follow-up — real Stripe path. Create the Customer +
    # Subscription on the Business price.
    # On success we stamp the live status immediately (the webhook is the
    # durable source of truth, but stamping here means the Current Plan page
    # reflects the purchase without waiting for the async event). A decline
    # / SDK error returns a clean error and changes NO plan state (
    # AC). A missing SDK degrades to 503 rather than 500.
    from asset_management.web.services.stripe_service import (
        StripeService,
        StripeError,
        StripeUnavailableError,
    )
    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
    )

    secret_key = config.STRIPE_SECRET_KEY
    price_id = config.STRIPE_PRICE_BUSINESS
    if not secret_key or not price_id:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Live payments are not fully configured. Please contact sales@kavanahanalytics.com.",
                }
            ),
            503,
        )

    # Stripe's Customer.create rejects a non-email string (``username`` is a
    # login name like "jane_doe", NOT an email — passing it 402'd every
    # real attempt). Use the session email only when it looks like an email;
    # otherwise omit it (Stripe allows a customer with no email).
    session_email = (session.get("email") or "").strip()
    customer_email = session_email if "@" in session_email else ""

    # Validate the coupon BEFORE charging.
    # The customer entered a promo/coupon code; resolve it to a Stripe coupon id
    # first so an invalid/expired code returns a coupon-SPECIFIC message and
    # charges nothing — the user can tell a bad coupon from a bad card. A
    # transport error during validation fails soft (proceed WITHOUT a coupon)
    # rather than blocking the purchase.
    resolved_coupon = ""
    resolved_promotion_code = ""
    # track whether the validated coupon brings the order total to $0
    # (a 100%-off discount). When it does, no payment method is required.
    zero_total = False
    if billing_coupon:
        try:
            verdict = StripeService.validate_coupon(secret_key=secret_key, code=billing_coupon, price_id=price_id)
        except StripeError:
            verdict = {"valid": False, "reason": "lookup_error"}
        if verdict.get("reason") == "lookup_error":
            logging.warning("billing coupon validation transport error for %s; proceeding without coupon", username)
        elif not verdict.get("valid"):
            AuditLogService.log_event(
                username,
                "billing_coupon_invalid",
                False,
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent", ""),
                detail=f"reason={verdict.get('reason', '')}",
            )
            return (
                jsonify(
                    {
                        "status": "error",
                        "field": "coupon",
                        "message": "That promo code isn't valid. Check the code or remove it to continue.",
                    }
                ),
                400,
            )
        else:
            resolved_coupon = verdict.get("coupon", "")
            # Prefer the promotion code so the promo's redemption caps / expiry
            # / eligibility are enforced by Stripe.
            resolved_promotion_code = verdict.get("promotion_code", "")
            # compute the discounted total SERVER-SIDE from the same
            # verdict the apply-coupon preview uses. A total of $0 (a 100%-off
            # coupon) means nothing will be charged, so a missing payment method
            # is acceptable below. The price is authoritative here — never the
            # client's claimed total. This route always charges the Business
            # price (price_id = STRIPE_PRICE_BUSINESS), so price the discount off
            # the matching Business plan.
            _unit_price = _billing_plan(_BILLING_DEFAULT_PLAN)["price_usd"]
            _new_total, _ = _apply_coupon_to_total(_unit_price, verdict)
            zero_total = round(_new_total, 2) <= 0.0
            # Already-added guard: if this user ALREADY
            # has a LIVE subscription carrying this exact discount, tell them
            # rather than creating a duplicate. On a true first purchase there is
            # no customer/sub yet, so this simply doesn't fire (skip on no match).
            #
            # gate on a NON-TERMINAL status. get_subscription_summary
            # returns the most-recent subscription regardless of status
            # (status="all", limit=1), so a CANCELED sub that once carried this
            # coupon would otherwise wrongly block re-subscribing with the same
            # code (subscribe → cancel → resubscribe). Only a live subscription
            # (active / trialing / past_due) counts as "already applied".
            _summary = _billing_subscription_summary(username) or {}
            _existing = _summary.get("discount_coupon") or ""
            _live_states = ("active", "trialing", "past_due")
            if _existing and _existing == verdict.get("coupon") and (_summary.get("status") or "").lower() in _live_states:
                return (
                    jsonify(
                        {"status": "error", "field": "coupon", "message": "This code is already applied to your subscription."}
                    ),
                    400,
                )

    # enforce the deferred payment-method requirement. A payment method
    # is required UNLESS a validated coupon brought the total to $0 — then there
    # is nothing to charge and Stripe can create the subscription with no PM.
    # This rejects a crafted POST that omits the PM without a 100%-off coupon
    # (AC #5: server-side rejects missing-payment when the total is > $0).
    if not payment_method_id and not zero_total:
        return jsonify({"status": "error", "message": "Missing payment method."}), 400

    # Option B (O2, flag-gated): when ORG_BILLING_PER_TEAM is on, buy a
    # TEAM license — one subscription per team under the org's single Stripe
    # customer (the buyer licenses their PRIMARY team). When off, the existing
    # single-subscription-per-payer path runs unchanged (safe fallback until the
    # O4 migration + cutover). Per-team success is stamped on the team
    # subscription doc by OrgBillingService; we still stamp the workspace status
    # below so the Current Plan page reflects active immediately.
    from asset_management.web.services.feature_flag_service import (
        FeatureFlagService,
        FLAG_ORG_BILLING_PER_TEAM,
    )
    from asset_management.web.services.org_billing_service import OrgBillingService
    from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

    _per_team = FeatureFlagService.is_enabled(FLAG_ORG_BILLING_PER_TEAM, username)
    _team_esid = None
    if _per_team:
        _ws_for_team = WorkspaceBillingService.resolve_workspace_esid(username)
        _team_esid = OrgBillingService.resolve_primary_team_esid(username)
        if not (_ws_for_team and _team_esid):
            # Can't resolve org+team → fall back to the legacy single-sub path
            # rather than block the purchase (fail-open, mirrors).
            logging.warning(
                "O2: per-team checkout flag on but unresolved org/team for %r (ws=%s team=%s); "
                "falling back to single-subscription path",
                username,
                _ws_for_team,
                _team_esid,
            )
            _per_team = False

    try:
        if _per_team:
            _team_res = OrgBillingService.create_team_subscription(
                workspace_esid=_ws_for_team,
                team_esid=_team_esid,
                price_id=price_id,
                secret_key=secret_key,
                payment_method_id=payment_method_id,
                coupon=resolved_coupon,
                promotion_code=resolved_promotion_code,
                owner_email=customer_email,
                owner_name=billing_name,
                owner_address=billing_address,
                company=billing_company,
            )
            result = {
                "customer_id": _team_res.get("customer_id", ""),
                "subscription_id": _team_res.get("subscription_id", ""),
                "status": _team_res.get("status", ""),
            }
        else:
            result = StripeService.create_subscription(
                secret_key=secret_key,
                price_id=price_id,
                payment_method_id=payment_method_id,
                customer_email=customer_email,
                customer_name=billing_name,
                customer_address=billing_address,
                company=billing_company,
                coupon=resolved_coupon,
                promotion_code=resolved_promotion_code,
            )
    except StripeUnavailableError:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Payments are temporarily unavailable. Please try again later.",
                }
            ),
            503,
        )
    except StripeError as exc:
        # Declined card / invalid coupon / any Stripe error → clean error,
        # NO plan change. The specific reason is captured in the audit log
        # (stripe_error=…), not surfaced to the user.
        AuditLogService.log_event(
            username,
            "billing_payment_failed",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"stripe_error={str(exc)[:200]}",
        )
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Your payment could not be processed. Please check your card details and try again.",
                }
            ),
            402,
        )

    # Stamp the live status so the Current Plan page reflects the purchase
    # immediately; the webhook reconciles/maintains it thereafter.
    # workspace-billing Phase 3c: the subscription attaches to the acting
    # user's WORKSPACE (every member inherits). Falls back to stamping the
    # user record if no workspace resolves (pre-migration safety) so the
    # purchaser at least reflects active.
    from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

    _ws_esid = WorkspaceBillingService.resolve_workspace_esid(username)
    if _ws_esid:
        WorkspaceBillingService.apply_stripe_status(
            _ws_esid, result.get("status", ""), customer_id=result.get("customer_id", "")
        )
    else:
        SubscriptionStateService.apply_stripe_status(
            username, result.get("status", ""), customer_id=result.get("customer_id", "")
        )
    AuditLogService.log_event(
        username,
        "billing_subscription_created",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"workspace={_ws_esid} customer={result.get('customer_id')} status={result.get('status')}",
    )
    return (
        jsonify({"status": "active", "message": "Payment successful. Your subscription is now active."}),
        200,
    )


# Stripe → our state mapping for the webhook lives in
# SubscriptionStateService (Phase 71). The webhook handler processes the
# subscription LIFECYCLE only (created/updated/deleted) — the set
# AC implies ("a successful payment activates the subscription"). A
# subscription.deleted event has no status on the object, so it maps to the
# terminal canceled state explicitly.
_STRIPE_WEBHOOK_SUBSCRIPTION_EVENTS = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)


def _maybe_send_billing_webhook_email(event_type, stripe_status, username, workspace_esid=None):
    """Dispatch the right billing email for a verified Stripe webhook event
    . Best-effort + async; never raises.

    Mapping (only meaningful transitions notify):
      * customer.subscription.created + active/trialing -> payment_succeeded
      * any event with status past_due                  -> payment_failed
      * customer.subscription.deleted                   -> subscription_canceled
    A routine ``customer.subscription.updated`` with status still active sends
    nothing (avoids spamming "payment received" on every metadata change).

    Target: when ``workspace_esid`` is given (the subscription is workspace-
    billed) the recipient is the workspace OWNER and the comp-suppression check
    reads the WORKSPACE state; otherwise the per-user path is used (pre-cutover
    subscriptions).
    """
    status = (stripe_status or "").strip().lower()
    if event_type == "customer.subscription.deleted":
        kind = "subscription_canceled"
    elif status == "past_due":
        kind = "payment_failed"
    elif event_type == "customer.subscription.created" and status in ("active", "trialing"):
        kind = "payment_succeeded"
    else:
        return  # nothing to notify for this transition

    try:
        from asset_management.web.services.subscription_state_service import SubscriptionStateService
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService
        from asset_management.web.services.billing_email_service import BillingEmailService

        # Suppress the "subscription was canceled" email when the cancellation
        # was caused by granting a comp — the comp path cancels the paid sub
        # immediately and sends its own "complimentary access" email, so the
        # scary canceled/imports-paused message would be wrong (now comped).
        # Read the WORKSPACE comp state when workspace-billed, else per-user.
        if kind == "subscription_canceled":
            if workspace_esid:
                comped = WorkspaceBillingService.get_workspace_state(workspace_esid).get("is_comped")
            else:
                comped = SubscriptionStateService.get_state(username).get("is_comped")
            if comped:
                logging.info("webhook: suppressing canceled email (comped) ws=%r user=%r", workspace_esid, username)
                return

        # Recipient: workspace owner (workspace-billed) or the user.
        if workspace_esid:
            email = WorkspaceBillingService.get_workspace_email(workspace_esid)
        else:
            email = SubscriptionStateService.get_user_email(username)
        if not email:
            return
        business = _billing_plan("business")
        # Next-billing date only meaningful for a payment_succeeded; the
        # workspace summary reads the workspace customer (resolved via the
        # owner) — keep it simple and only fetch for the owner/user path.
        summary = None
        if kind == "payment_succeeded":
            who = WorkspaceBillingService.find_workspace_owner(workspace_esid) if workspace_esid else username
            summary = _billing_subscription_summary(who) if who else None
        # Reflect the ACTUAL charged price (list minus any coupon) in the email,
        # not the static list price. The subscription
        # already carries the coupon at this point, so the summary's
        # amount_total is the real recurring charge.
        price_display = business.get("price_display", "")
        # Use the actual charged amount when we have a subscription summary —
        # gate on amount_subtotal, NOT amount_total (a 100%-off coupon makes
        # amount_total == 0, which is falsy; 2026-06-09).
        if summary and (summary.get("amount_subtotal") or 0):
            price_display = f"${(summary.get('amount_total') or 0) / 100:,.2f}"
        ctx = {
            "full_name": "",
            "plan_name": business.get("name", "Business"),
            "price_display": price_display,
            "date": (summary or {}).get("current_period_end", "")[:10] if summary else "",
        }
        BillingEmailService.dispatch(kind, email, ctx)
    except Exception as exc:  # noqa: BLE001 — a billing email must never break the webhook
        logging.warning("billing webhook email (%s) failed (ws=%r user=%r): %s", kind, workspace_esid, username, exc)


def _maybe_send_payment_receipt(invoice, ws_esid, username):
    """Send a monthly payment RECEIPT for a paid Stripe invoice (
    2026-06-09). Best-effort + async; never raises.

    ``invoice.amount_paid`` is the ACTUAL amount charged this period — already
    net of any promo/coupon. A $0.00 charge (e.g. a 100%-off coupon) sends NO
    receipt (per the requirement). Recipient is the workspace owner when
    workspace-billed, else the per-user email.
    """
    try:
        amount_paid = int((invoice or {}).get("amount_paid") or 0)
        if amount_paid <= 0:
            # $0.00 charge (full comp / 100%-off coupon) — no receipt.
            return
        from asset_management.web.services.subscription_state_service import SubscriptionStateService
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService
        from asset_management.web.services.billing_email_service import BillingEmailService

        if ws_esid:
            email = WorkspaceBillingService.get_workspace_email(ws_esid)
        else:
            email = SubscriptionStateService.get_user_email(username)
        if not email:
            return
        business = _billing_plan("business")
        # Next billing date from the invoice's period end, when present.
        period_end = ((invoice or {}).get("lines") or {}).get("data") or []
        next_date = ""
        if period_end:
            pe = (period_end[0].get("period") or {}).get("end")
            if pe:
                try:
                    from datetime import datetime, timezone

                    next_date = datetime.fromtimestamp(int(pe), tz=timezone.utc).isoformat()[:10]
                except Exception:  # noqa: BLE001
                    next_date = ""
        # a mid-cycle add-on purchase invoices with
        # billing_reason "subscription_update" (a normal renewal is
        # "subscription_cycle"); flag it so the receipt copy explains the
        # partial/prorated amount instead of presenting it as a full month.
        is_proration = (invoice or {}).get("billing_reason") == "subscription_update"
        ctx = {
            "full_name": "",
            "plan_name": business.get("name", "Business"),
            "price_display": f"${amount_paid / 100:,.2f}",
            "date": next_date,
            "is_proration": is_proration,
        }
        # on a PRORATED add-on receipt only, itemize the coupon so the
        # odd partial figure is legible ("$X, after your $Y discount"). The net
        # amount is already discount-aware; this just names the discount. Summed
        # from invoice.total_discount_amounts (minor units); omitted (no
        # breakdown line) when absent/zero so a no-coupon receipt is unchanged
        # and a renewal receipt is never touched. Fail-soft — a malformed field
        # just skips the breakdown, never breaks the receipt.
        if is_proration:
            try:
                disc_cents = sum(
                    int((d or {}).get("amount") or 0) for d in ((invoice or {}).get("total_discount_amounts") or [])
                )
                if disc_cents > 0:
                    ctx["discount_display"] = f"${disc_cents / 100:,.2f}"
            except Exception:  # noqa: BLE001 — breakdown is best-effort, never fatal
                pass
        BillingEmailService.dispatch("payment_receipt", email, ctx)
    except Exception as exc:  # noqa: BLE001 — a receipt email must never break the webhook
        logging.warning("payment receipt email failed (ws=%r user=%r): %s", ws_esid, username, exc)


@analytics_bp.route("/billing/stripe/webhook", methods=["POST"])
def billing_stripe_webhook():
    """Stripe webhook receiver.

    Anonymous + CSRF-exempt by necessity — Stripe POSTs server-to-server
    with no session and no CSRF token. Authenticity is established ENTIRELY
    by verifying the ``Stripe-Signature`` header against
    ``config.STRIPE_WEBHOOK_SECRET`` (the endpoint is in auth's anonymous
    allowlist + ``_CSRF_EXEMPT_ENDPOINTS``). A bad/missing signature → 400
    with no state change (fail closed). An empty webhook secret rejects
    every call.

    Scope: subscription lifecycle (created/updated/deleted). The
    subscription's ``status`` is applied to the mapped user via
    ``SubscriptionStateService.apply_stripe_status`` (deleted → canceled).
    Unknown event types are acknowledged (200) and ignored so Stripe
    doesn't retry. An event whose customer maps to no user is also acked.
    """
    from flask import jsonify

    from asset_management.web.services.stripe_service import (
        StripeService,
        StripeSignatureError,
        StripeUnavailableError,
    )
    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
        STATE_CANCELED,
    )

    payload = request.get_data()  # raw bytes — signature is over the exact body
    signature = request.headers.get("Stripe-Signature", "")
    try:
        event = StripeService.construct_webhook_event(
            payload=payload, signature=signature, secret=config.STRIPE_WEBHOOK_SECRET
        )
    except StripeSignatureError:
        # Unverifiable → reject. Never mutate state on an unsigned payload.
        return jsonify({"status": "error", "message": "invalid signature"}), 400
    except StripeUnavailableError:
        return jsonify({"status": "error", "message": "stripe unavailable"}), 503

    # Once the signature is verified, NEVER 500 back to Stripe — a 500 makes
    # Stripe retry the event for days. Any failure applying the event is
    # logged (with the traceback) and acked, so a transient/processing bug
    # surfaces in our logs without an infinite retry storm. (The signed
    # event is trustworthy; the work after it is best-effort + idempotent.)
    try:
        event_type = event.get("type", "")

        # invoice.paid — a successful recurring (or first) charge. Send a
        # monthly receipt with the amount actually paid.
        # Skipped for a $0.00 invoice inside the helper. Resolve the target the
        # same way as the lifecycle events (workspace owner, else per-user).
        if event_type in ("invoice.paid", "invoice.payment_succeeded"):
            from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

            invoice = ((event.get("data") or {}).get("object")) or {}
            inv_customer = invoice.get("customer") or ""
            ws = WorkspaceBillingService.find_workspace_by_stripe_customer(inv_customer)
            user = None if ws else SubscriptionStateService.find_username_by_stripe_customer(inv_customer)
            if ws or user:
                _maybe_send_payment_receipt(invoice, ws, user)
            return jsonify({"status": "ok", "type": event_type}), 200

        if event_type not in _STRIPE_WEBHOOK_SUBSCRIPTION_EVENTS:
            # Acknowledge + ignore everything outside the subscription lifecycle.
            return jsonify({"status": "ignored", "type": event_type}), 200

        subscription = ((event.get("data") or {}).get("object")) or {}
        customer_id = subscription.get("customer") or ""

        # workspace-billing Phase 3d: subscriptions attach to a
        # WORKSPACE, so map the customer -> workspace and apply there. Fall
        # back to the per-user mapping for any subscription created before the
        # workspace cutover (so in-flight per-user subs still reconcile).
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

        ws_esid = WorkspaceBillingService.find_workspace_by_stripe_customer(customer_id)
        username = None if ws_esid else SubscriptionStateService.find_username_by_stripe_customer(customer_id)
        if not ws_esid and not username:
            # No workspace OR user carries this customer id — ack so Stripe stops retrying.
            logging.info("webhook: no target for customer=%r (event=%s)", customer_id, event_type)
            return jsonify({"status": "no_target", "customer": customer_id}), 200

        # subscription.deleted carries no meaningful status → terminal canceled.
        if event_type == "customer.subscription.deleted":
            stripe_status = STATE_CANCELED
        else:
            stripe_status = subscription.get("status", "")

        # Option B (O3, flag-gated): a subscription is per-TEAM, so apply
        # the status to the specific TEAM it belongs to. Map by the subscription
        # id (the team-subscription doc stores it) or the team_esid we stamp in
        # the subscription metadata at create time. This runs ALONGSIDE the
        # workspace apply below (the org-level page still reflects status); it
        # just keeps the per-team doc current. Best-effort: never fail the
        # webhook. With the flag off, no team docs exist, so this no-ops.
        try:
            from asset_management.web.services.feature_flag_service import (
                FeatureFlagService,
                FLAG_ORG_BILLING_PER_TEAM,
            )

            if FeatureFlagService.is_enabled(FLAG_ORG_BILLING_PER_TEAM):
                from asset_management.web.services.org_billing_service import OrgBillingService

                sub_id = subscription.get("id") or ""
                team_esid = (subscription.get("metadata") or {}).get("team_esid") or (
                    OrgBillingService.find_team_by_subscription_id(sub_id) if sub_id else None
                )
                if team_esid:
                    OrgBillingService.set_team_subscription_status(team_esid, stripe_status)
        except Exception as team_exc:  # noqa: BLE001 — per-team update must never 500 the webhook
            logging.warning("O3 webhook: per-team status update failed (non-fatal): %s", team_exc)

        if ws_esid:
            result = WorkspaceBillingService.apply_stripe_status(ws_esid, stripe_status, customer_id=customer_id)
        else:
            result = SubscriptionStateService.apply_stripe_status(username, stripe_status, customer_id=customer_id)
        try:
            AuditLogService.log_event(
                username or f"workspace:{ws_esid}",
                "billing_webhook_status",
                bool(result.get("ok")),
                ip_address=request.remote_addr,
                user_agent=request.headers.get("User-Agent", ""),
                detail=f"event={event_type} status={stripe_status} workspace={ws_esid} ok={result.get('ok')}",
            )
        except Exception as audit_exc:  # noqa: BLE001 — auditing must never fail the webhook
            logging.warning("webhook: audit log failed (non-fatal): %s", audit_exc)

        # Notify the user of the billing change (best-effort, async — a mail
        # failure must never 500 the webhook). Only the meaningful transitions
        # send: a NEW subscription -> payment_succeeded; a past_due ->
        # payment_failed; a deletion -> subscription_canceled. Routine
        # subscription.updated events (status still active) send nothing so we
        # don't spam "payment received" on every metadata tweak.
        _maybe_send_billing_webhook_email(event_type, stripe_status, username, workspace_esid=ws_esid)
        return jsonify({"status": "ok", "applied": stripe_status}), 200
    except Exception as exc:  # noqa: BLE001 — boundary: ack + log, never 500 to Stripe
        logging.exception("webhook: processing failed after signature verify: %s", exc)
        return jsonify({"status": "received"}), 200


@analytics_bp.route("/billing/plans")
def billing_plans_page():
    """Billing → Plans sub-page.

    Presents both subscription tiers — Business ($49.95/mo) and
    Nonprofit ($20/mo) — each with its price, the shared 6-connection cap,
    the 1-account-per-connection rule, and a "Contact us for more
    connections" CTA. The Business card routes into the self-serve
    Payment flow carrying ``?plan=business``; the nonprofit card surfaces
    the sales-contact eligibility route (automatic EIN verification
    is not yet built).
    """
    guard = _billing_access_guard()
    if guard is not None:
        return guard
    return render_template(
        "billing_section.html",
        username=session.get("username", ""),
        active="plans",
        plans=_billing_plans(),
        connection_cap=_BILLING_CONNECTION_CAP,
        addon_connection_price=_BILLING_ADDON_CONNECTION_PRICE,
        caps_enabled=_caps_enabled_for_templates(),
        back_url=_billing_back_url(),
    )


# display copy for the subscription state machine. The state
# value comes from SubscriptionStateService; this maps it to the badge
# label/tone and the headline/sub copy the Current Plan card shows. Kept
# next to the route so the template stays dumb (it just prints these).
_BILLING_STATE_DISPLAY = {
    # Headlines reference the top-level billing entity. item 1 renames
    # it "organization" in user-facing copy (internal tier is still the
    # workspace). Badges stay short (Trial/Active/Expired/Past due/Canceled/
    # Access ended). The body <p> in the template carries the CONSEQUENCE +
    # next step only — it must NOT restate the headline (2026-06-09:
    # the canceled headline + body were the same sentence twice).
    "trial": {
        "badge": "Trial",
        "tone": "trial",
        "headline": "Your organization is on a free trial",
    },
    "active": {
        "badge": "Active",
        "tone": "active",
        "headline": "Your organization's subscription is active",
    },
    "lapsed": {
        # Display badge "Expired" (a trial that timed out) — the internal state
        # key stays "lapsed". Vocabulary split: trial timed out -> Expired,
        # user-canceled sub -> Canceled, revoked comp -> Access ended (
        # 2026-06-08). The "lapsed" tone class keeps the existing red styling.
        "badge": "Expired",
        "tone": "lapsed",
        "headline": "Your organization's trial has ended",
    },
    # Stripe scaffold — surfaced only once the webhook sets
    # ``stripe_status``. past_due = grace (warn, imports still allowed);
    # canceled = ended (imports blocked, data stays visible).
    "past_due": {
        "badge": "Past due",
        "tone": "warn",
        "headline": "Your organization's payment is past due",
    },
    "canceled": {
        "badge": "Canceled",
        "tone": "lapsed",
        "headline": "Your organization's subscription was canceled",
    },
    # follow-up — admin revoked a previously-granted comp.
    "comp_revoked": {
        "badge": "Access ended",
        "tone": "lapsed",
        "headline": "Your organization has lost complimentary access",
    },
}


def _billing_current_connections(username, effective_cap=None):
    """Build the display list of the user's connections + the ad accounts
    under each, for the Current Plan page.

    Delegates to ``SubscriptionStateService.connections_for_display`` — the
    SAME source the "N of M used" counts come from — so the list and the
    counts can never disagree. org-scoped (org → teams → connector
    cards) when the acting user owns an org, else the per-principal rollup
    (one entry per distinct real provider account, ``wizard-pending-*`` rows
    collapsed). Only ad-account-type assets, shown by name + provider id.
    Fail-soft: any error returns an empty list.

    the unit that the cap (``effective_cap``) and the page's
    "N of M used" counter both measure is the SOURCE — each FB ad account /
    page / IG account, each GA4 property, each Google Ads customer, each Echo
    tenant — NOT the provider row. So the active/inactive split is per-source:
    the FIRST ``effective_cap`` sources, in the existing flattened display
    order, are ACTIVE (within the paid limit, imported); every source beyond
    the cap is INACTIVE (over the limit, not imported). A provider that
    straddles the cap appears in BOTH groups, carrying only its active sources
    in the Active group and only its inactive sources in the Inactive group.
    This is a display-only label — it does NOT disable imports.

    Returns ``{"active": [<rows>], "inactive": [<rows>]}`` where each list holds
    the same connection-row shape as before (provider / label / status / count /
    groups / accounts), restricted to that group's sources. ``effective_cap=None``
    puts every source in ``active`` (no cap to enforce / older callers).
    """
    try:
        from asset_management.web.services.subscription_state_service import (
            SubscriptionStateService,
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("current-plan connections: import failed: %s", exc)
        return []

    # the active/inactive SOURCE partition (first ``cap``
    # sources in flattened display order active, the rest inactive) is owned by
    # ``SubscriptionStateService.partition_connections_by_cap`` so the badge here
    # and the importer billing pre-check read ONE rule and can't drift.
    # We precompute the per-position verdict over the SAME connection list +
    # flatten order this helper iterates, then consume it positionally below.
    connections = list(SubscriptionStateService.connections_for_display(username))
    _partition = SubscriptionStateService.partition_connections_by_cap(connections, effective_cap)
    _active_count = len(_partition["active"])
    seen = [0]  # boxed so the nested helper can mutate it

    def _is_active():
        """Consume the next source slot; True while still within the cap.

        Position-based — the first ``_active_count`` sources (in this helper's
        flatten order, which matches ``partition_connections_by_cap``'s) are
        active; the rest inactive. ``effective_cap=None`` puts every source in
        active (``_active_count`` == the total)."""
        active = seen[0] < _active_count
        seen[0] += 1
        return active

    active_out = []
    inactive_out = []

    for conn in connections:
        ad_accounts = conn.get("ad_accounts", []) or []
        provider = conn.get("provider", "") or ""
        status = conn.get("status", "") or ""
        label = conn.get("label") or ""

        # Meta splits into labelled sub-sections; every other
        # provider renders one flat list. ESIDs are NEVER shown — each row
        # uses the human-facing provider id / slug instead. each
        # source is routed to the active or inactive bucket by cap position.
        if provider == "facebook":
            # (label, [active accounts], [inactive accounts]) per sub-section,
            # in the existing Ad accounts / Pages / Instagram order.
            sub = {
                "Ad accounts": ([], []),
                "Pages": ([], []),
                "Instagram accounts": ([], []),
            }
            for acct in ad_accounts:
                kind = acct.get("kind") or ""
                if kind == "page":
                    seclabel, row = "Pages", {"name": _billing_acct_display(acct.get("slug"), acct.get("provider_id"))}
                elif kind == "ig_account":
                    seclabel, row = "Instagram accounts", {"name": acct.get("name") or ""}
                else:
                    seclabel, row = "Ad accounts", {"name": _billing_acct_display(acct.get("name"), acct.get("provider_id"))}
                sub[seclabel][0 if _is_active() else 1].append(row)

            a_groups, a_count = [], 0
            i_groups, i_count = [], 0
            for seclabel in ("Ad accounts", "Pages", "Instagram accounts"):
                act, inact = sub[seclabel]
                if act:
                    a_groups.append({"label": seclabel, "accounts": act})
                    a_count += len(act)
                if inact:
                    i_groups.append({"label": seclabel, "accounts": inact})
                    i_count += len(inact)

            for bucket, groups, count in (
                (active_out, a_groups, a_count),
                (inactive_out, i_groups, i_count),
            ):
                if count:
                    bucket.append(
                        {
                            "provider": provider,
                            "label": label or "Meta",
                            "status": status,
                            "count": count,
                            "groups": groups,
                            "accounts": [],
                        }
                    )
            continue

        a_accounts, i_accounts = [], []
        for acct in ad_accounts:
            name = acct.get("name") or ""
            if provider == "echo":
                # Echo: tenant title only, no id.
                display = name
            else:
                # Title + the human-facing provider id (Disciple.Tools surfaces
                # its site domain in provider_id). Never the ESID.
                display = _billing_acct_display(name, acct.get("provider_id"))
            (a_accounts if _is_active() else i_accounts).append({"name": display})

        for bucket, accounts in ((active_out, a_accounts), (inactive_out, i_accounts)):
            if accounts:
                bucket.append(
                    {
                        "provider": provider,
                        "label": label,
                        "status": status,
                        "count": len(accounts),
                        "groups": [],
                        "accounts": accounts,
                    }
                )

    return {"active": active_out, "inactive": inactive_out}


# providers whose sources ARE literal ad accounts (so they count
# toward the "Ad accounts" sub-row). Mirrors SubscriptionStateService's
# org-path ``accounts_used`` definition ({google_ads, ga4}); Facebook ad
# accounts are counted separately via their "Ad accounts" sub-section, since a
# Meta row also carries pages / IG accounts that are NOT ad accounts.
_BILLING_AD_ACCOUNT_PROVIDERS = frozenset({"google_ads", "ga4"})


def _billing_count_rendered_sources(connections):
    """count sources + ad accounts from the EXACT list the Current
    Plan page renders, so the "N of M used" meter, the card-header total, and
    the visible rows can never disagree.

    The reported bug (prod: meter "4 of 6" but only 2 rows shown; staging:
    "10" connections vs "11" ad accounts) came from the meter/header counting
    via ``SubscriptionStateService._usage`` while the list rendered via a
    SEPARATE ``connections_for_display`` pass — two independent rollups that
    can diverge, and that count incompatible units in the per-principal path
    (``connections_used`` = provider-account groups, ``accounts_used`` =
    ad accounts under them, which can exceed it). Deriving both counts from the
    rendered rows makes the three surfaces one source of truth by construction.

    ``connections`` is the ``{"active": [...], "inactive": [...]}`` structure
    from ``_billing_current_connections`` (or a bare list / falsy for older
    callers). Returns ``{"connections_used": int, "accounts_used": int}``:

      * ``connections_used`` = total sources across every rendered row (each
        row's ``count``), active + inactive — the unit the user counts in the
        list (decision 2026-06-19: the meter counts sources shown in the list).
      * ``accounts_used`` = the subset of those sources that are literal ad
        accounts: a Meta row's "Ad accounts" sub-section, plus every source
        under a Google Ads / GA4 row. A strict subset of ``connections_used``,
        so the "Included in connections above" sub-row copy is always true.
    """
    if not connections:
        return {"connections_used": 0, "accounts_used": 0}
    # Accept both the grouped {"active", "inactive"} shape and a bare list.
    if isinstance(connections, dict):
        rows = list(connections.get("active") or []) + list(connections.get("inactive") or [])
    else:
        rows = list(connections)

    connections_used = 0
    accounts_used = 0
    for row in rows:
        count = int(row.get("count") or 0)
        connections_used += count
        provider = row.get("provider") or ""
        if provider == "facebook":
            # Meta row: only the "Ad accounts" sub-section is ad accounts;
            # pages / IG accounts are organic sources, not ad accounts.
            for grp in row.get("groups") or []:
                if (grp.get("label") or "") == "Ad accounts":
                    accounts_used += len(grp.get("accounts") or [])
        elif provider in _BILLING_AD_ACCOUNT_PROVIDERS:
            accounts_used += count
    return {"connections_used": connections_used, "accounts_used": accounts_used}


def _billing_acct_display(name, provider_id):
    """Format a connector asset row for the Current Plan page:
    ``"Title (provider_id)"`` when both are present, else whichever is
    non-empty. NEVER includes the ESID — ``provider_id`` is the human-facing
    id (FB ad-account id, FB page id, DT site domain)."""
    name = (name or "").strip()
    provider_id = (provider_id or "").strip()
    if name and provider_id:
        return f"{name} ({provider_id})"
    return name or provider_id


def _billing_user_stripe_customer_id(username):
    """Resolve the Stripe ``stripe_customer_id`` for the acting user's billing
    target.

    Prefers the WORKSPACE billing record (a subscription now attaches to the
    workspace, so cancel / update-card / summary / prefill must read it there).
    Falls back to the user's own metadata when the workspace has no billing
    record yet (pre-Phase-4 migration) — same fallback posture as get_state.
    Returns "" when absent / on error.
    """
    try:
        from asset_management.web.services.subscription_state_service import (
            STRIPE_CUSTOMER_ID_KEY,
        )
        from asset_management.web.services.workspace_billing_service import (
            WorkspaceBillingService,
        )
        from lib.es_helpers import get_elasticsearch_client

        # Workspace record wins when present.
        ws_esid = WorkspaceBillingService.resolve_workspace_esid(username)
        if ws_esid and WorkspaceBillingService.has_billing_record(ws_esid):
            es = get_elasticsearch_client()
            if es is not None:
                doc = WorkspaceBillingService._get_billing_doc(es, ws_esid)
                cid = doc.get(STRIPE_CUSTOMER_ID_KEY) or ""
                if cid:
                    return cid

        es = get_elasticsearch_client()
        if es is None:
            return ""
        rec = dict(es.security.get_user(username=username)).get(username) or {}
        return (rec.get("metadata") or {}).get(STRIPE_CUSTOMER_ID_KEY) or ""
    except Exception as exc:  # noqa: BLE001
        logging.warning("stripe_customer_id lookup failed for %r: %s", username, exc)
        return ""


def _billing_subscription_summary(username):
    """Fetch the live Stripe subscription summary (next billing date +
    cancel-scheduled) for a paid user, or ``None``.

    Only meaningful for a real Stripe sub. Fail-soft: returns None on any
    missing config / customer / Stripe error so the page still renders.
    """
    if not config.STRIPE_LIVE_PAYMENTS or not config.STRIPE_SECRET_KEY:
        return None
    customer_id = _billing_user_stripe_customer_id(username)
    if not customer_id:
        return None
    try:
        from asset_management.web.services.stripe_service import StripeService, StripeError

        try:
            return StripeService.get_subscription_summary(secret_key=config.STRIPE_SECRET_KEY, customer_id=customer_id)
        except StripeError:
            return None
    except Exception as exc:  # noqa: BLE001 — never break the page on a Stripe hiccup
        logging.warning("subscription summary failed for %r: %s", username, exc)
        return None


def _billing_update_prefill(username):
    """Read the Customer's stored billing details to pre-fill the update-card
    form, so a paid user doesn't re-type name/address on
    every card change. Returns the form-shaped dict, or ``None`` when nothing
    to pre-fill. Fail-soft: any missing config / customer / Stripe error
    returns None so the form just renders blank instead of 500ing the page.
    """
    if not config.STRIPE_LIVE_PAYMENTS or not config.STRIPE_SECRET_KEY:
        return None
    customer_id = _billing_user_stripe_customer_id(username)
    if not customer_id:
        return None
    try:
        from asset_management.web.services.stripe_service import StripeService, StripeError

        try:
            return StripeService.get_customer_billing_details(secret_key=config.STRIPE_SECRET_KEY, customer_id=customer_id)
        except StripeError:
            return None
    except Exception as exc:  # noqa: BLE001 — never break the page on a Stripe hiccup
        logging.warning("billing prefill failed for %r: %s", username, exc)
        return None


@analytics_bp.route("/billing/current")
def billing_current_page():
    """Billing → Current Plan sub-page.

    Renders the signed-in user's subscription state (trial / active /
    lapsed), days remaining on trial, usage against the caps that apply to
    that state, and the user's connections + accounts. State comes from
    SubscriptionStateService (the single source the import gate and the
    superuser comp also read). Entry points link to Plans and
    Payment.
    """
    guard = _billing_access_guard()
    if guard is not None:
        return guard

    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
    )

    username = session.get("username", "")
    is_superuser = "superuser" in session.get("roles", [])
    state = SubscriptionStateService.get_state(username)
    # reconcile the stored add-on count against the live Stripe item
    # before rendering, so a metadata/Stripe drift can't mis-state "purchased"
    # or break the netting price (Stripe is source of truth; fail-soft).
    state = _billing_reconcile_addons(username, state)

    # a complimentary account (superuser, or an admin-granted comp)
    # is covered in FULL: every connection is comped, with $0 owed regardless of
    # how many there are. The included-6 cap (and the $10/source over-cap split)
    # is a PAID-plan concept and must not apply here — without this, connections
    # 7+ fell into the "over your plan limit" group and read as billable add-ons,
    # contradicting the comp. ``None`` tells _billing_current_connections to put
    # every source in the active group (no over-cap split), and the template
    # swaps the "N of 6 used" meter + "plan includes 6" note for comped copy.
    _is_complimentary = bool(is_superuser or state.get("is_comped"))

    # build the rendered connections list ONCE and derive the
    # "N of M used" / "Ad accounts" counts FROM it, so the usage meter, the
    # card-header total, and the visible rows are one source of truth and can
    # never disagree (the reported prod "4 of 6" vs. 2-rows-shown and staging
    # "10 connections vs 11 ad accounts" splits both came from the meter and
    # the list being counted by two independent passes). The active/inactive
    # split still keys off the effective cap (purchased add-ons), which the
    # count overlay leaves untouched — EXCEPT a complimentary account, which is
    # uncapped.
    _effective_cap = int(state.get("connection_cap", _BILLING_CONNECTION_CAP))
    # no source caps: every account splits uncapped, the same path
    # a complimentary account already took (no "Inactive — over your plan
    # limit" group, no "Not imported" badge).
    _caps_enabled = SubscriptionStateService.caps_enabled()
    _split_cap = None if (_is_complimentary or not _caps_enabled) else _effective_cap
    connections = _billing_current_connections(username, effective_cap=_split_cap)
    _counts = _billing_count_rendered_sources(connections)
    state["connections_used"] = _counts["connections_used"]
    state["accounts_used"] = _counts["accounts_used"]

    display = _BILLING_STATE_DISPLAY.get(state["state"], _BILLING_STATE_DISPLAY["lapsed"])
    # Complimentary access (superuser, or an admin-granted comp) resolves to
    # the "active" state, but "Your subscription is active" / "Active" is wrong
    # — they have no subscription. Show a complimentary headline + badge so the
    # heading matches the complimentary-access copy below.
    if is_superuser or state.get("is_comped"):
        display = {
            "badge": "Complimentary",
            "tone": "active",
            "headline": "You have complimentary access",
        }

    # The plan a user is on / would buy. v1 has no stored tier choice, so
    # active/lapsed surface the Business plan (the default, and the caps
    # a comp grants); trial surfaces the trial caption. Price comes from the
    # plan catalog (single source of truth shared with Plans/Payment).
    business = _billing_plan("business")

    # (2026-06-05): for a real PAID subscription, surface the next
    # billing date + whether a cancel is already scheduled. Comped-active has
    # no Stripe sub, so no bill date (the Cancel button still shows for active,
    # and for a comped user it clears the comp). Fail-soft → None.
    billing = None
    if state["state"] == "active" and state.get("is_paid"):
        billing = _billing_subscription_summary(username)
        # 2026-06-09 — surface the ACTUAL charged amount (list price
        # minus any coupon) so the Current Plan copy + next-bill line reflect
        # the discount, not the list price. Formatted strings for the template;
        # fail-soft (a summary without amounts just shows the list price).
        if billing:
            sub_cents = billing.get("amount_subtotal") or 0
            tot_cents = billing.get("amount_total") or 0
            # Gate on amount_subtotal (the list price is always > 0), NOT on
            # tot_cents — a 100%-off coupon makes tot_cents == 0 (falsy), which
            # would skip price_display and wrongly fall back to list price
            #.
            if sub_cents:
                billing["price_display"] = f"${tot_cents / 100:,.2f}"
                billing["list_price_display"] = f"${sub_cents / 100:,.2f}"
                # a 100%-off coupon zeroes the charge: the plan is FREE,
                # not "$0.00". Flag it so the template renders "free" wording
                # instead of a bare $0.00 (which reads as a glitch). list_price
                # still shows so the coupon's value is visible.
                billing["is_free"] = tot_cents == 0

    # Option B (O5, flag-gated) — the org's per-team subscriptions for
    # the "Team subscriptions" card. [] when the flag is off / no licensed
    # teams / any resolution error, so the card simply doesn't render and the
    # page is unchanged until cutover. Fail-soft: never break the page.
    team_subscriptions = []
    try:
        from asset_management.web.services.feature_flag_service import (
            FeatureFlagService,
            FLAG_ORG_BILLING_PER_TEAM,
        )

        if FeatureFlagService.is_enabled(FLAG_ORG_BILLING_PER_TEAM, username):
            from asset_management.web.services.org_billing_service import OrgBillingService
            from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

            _ws_o5 = WorkspaceBillingService.resolve_workspace_esid(username)
            if _ws_o5:
                team_subscriptions = OrgBillingService.list_team_subscriptions_display(_ws_o5)
    except Exception as exc:  # noqa: BLE001
        logging.warning("O5: team-subscriptions list failed for %r: %s", username, exc)

    # AI Assistant spend card. Renders ONLY when the resolved
    # billing workspace has the AI Assistant enabled (the superuser
    # workspace toggle); everyone else gets no card at all. spend_usd is
    # None on a usage-index error — the template blanks the figure (never
    # inf, never an error string). Fail-soft: never break the page.
    ai_spend = None
    try:
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService
        from asset_management.web.services.workspace_membership_service import WorkspaceMembershipService

        # AI-attribution resolver so the card's visibility test names the
        # same workspace get_ai_spend_readout sums over; the Stripe billing
        # resolver could pick a different one for a multi-workspace user
        # and gate the card on a tenant whose spend is not being shown.
        _ai_ws = WorkspaceBillingService.resolve_ai_workspace_esid(username)
        _is_su = "superuser" in (session.get("roles") or [])
        if _ai_ws and (_is_su or WorkspaceMembershipService.ai_assistant_enabled_for_workspace(_ai_ws)):
            ai_spend = WorkspaceBillingService.get_ai_spend_readout(username)
    except Exception as exc:  # noqa: BLE001
        logging.warning("AI spend readout failed for %r: %s", username, exc)

    return render_template(
        "billing_section.html",
        username=username,
        active="current",
        state=state,
        state_display=display,
        plan=business,
        connection_cap=_BILLING_CONNECTION_CAP,
        # the org's EFFECTIVE cap (included 6 + purchased add-ons),
        # in the SAME source unit as the "N of M used" counter. Drives the
        # active/inactive split in the connections card and the helper copy.
        effective_cap=_effective_cap,
        # a complimentary (superuser / comped) account is covered in
        # full: the template hides the "N of 6 used" cap framing + the over-cap
        # helper note and shows comped copy instead (no source is "over limit").
        is_complimentary=_is_complimentary,
        # {"active": [...rows...], "inactive": [...rows...]} — the first
        # effective_cap sources (flattened display order) are active. —
        # built above (once) so state.connections_used / accounts_used are
        # derived from THIS exact list.
        connections=connections,
        billing=billing,
        # the superuser comp control renders only for superusers
        # (workspace_owner-only billing users can reach the page but not comp).
        is_superuser=is_superuser,
        # item 5b self-serve add-ons (Phase D). The "Add connections"
        # control shows only for a real PAID-active org with Stripe live + an
        # add-on price configured (a comped/superuser/trial org has nothing to
        # add to). addon_connections = how many are already purchased (effective
        # cap minus the included cap); addon_price is the per-connection rate.
        # (2026-06-11): a cancel-SCHEDULED subscription also hides the
        # add-on section — buying prorated add-ons / applying add-on coupons on
        # a subscription that's ending makes no sense (restore it first; same
        # rule as the base "Have a coupon?" box and the Update-payment CTA).
        # (2026-06-11, user rule): also hidden while the org still has
        # FREE included slots — offering a $10 add-on to someone using 2 of 6
        # invites a pointless charge. Shows once all included connections are
        # used, OR when add-ons were already purchased (the org must always be
        # able to adjust/remove what it's paying for).
        # with no source caps there is nothing an add-on buys, so the
        # section only renders for an org that ALREADY owns add-ons (it must
        # always be able to remove what it is paying for). Restores the full
        # rule automatically if SOURCE_CAPS_ENABLED is flipped back.
        addon_self_serve=bool(
            state["state"] == "active"
            and state.get("is_paid")
            and not (billing and billing.get("cancel_at_period_end"))
            and (
                (_caps_enabled and int(state.get("connections_used") or 0) >= _BILLING_CONNECTION_CAP)
                or int(state.get("connection_cap", _BILLING_CONNECTION_CAP)) > _BILLING_CONNECTION_CAP
            )
            and config.STRIPE_LIVE_PAYMENTS
            and config.STRIPE_SECRET_KEY
            and config.STRIPE_PRICE_ADDON_CONNECTION
        ),
        caps_enabled=_caps_enabled,
        addon_connections=max(0, int(state.get("connection_cap", _BILLING_CONNECTION_CAP)) - _BILLING_CONNECTION_CAP),
        addon_included=_BILLING_CONNECTION_CAP,
        addon_price=_BILLING_ADDON_CONNECTION_PRICE,
        # Hard cap on self-serve add-ons: at the cap the
        # Add dropdown + Pay button hide (nothing more to buy).
        addon_max=_BILLING_ADDON_MAX,
        # (2026-06-12) — non-empty ISO date while the org's add-ons are
        # marked for a scheduled change at period end: the template swaps the
        # Remove button + hint for the scheduled note. addon_pending_qty is the
        # change's TARGET (0 = full removal, N = reduction) for the note copy.
        # Read from the surface get_state reads (workspace when ws-billed).
        addon_remove_at=_billing_addon_remove_at(username),
        addon_pending_qty=_billing_addon_pending_qty(username),
        # non-empty only when ORG_BILLING_PER_TEAM is on AND the
        # org has licensed teams; the template hides the card otherwise.
        team_subscriptions=team_subscriptions,
        # non-None only when the workspace has AI Assistant
        # enabled; the template hides the card otherwise.
        ai_spend=ai_spend,
        back_url=_billing_back_url(),
    )


@analytics_bp.route("/billing/comp", methods=["POST"])
def billing_set_comp():
    """Superuser comp override.

    Lets a superuser mark an existing user as having an **active
    subscription without payment** (migrating current users onto billing,
    or comping an account), or clear that grant. Reversible and auditable.

    Superuser-ONLY — the broader ``_billing_access_guard`` also admits
    workspace_owners, who must NOT be able to comp, so this endpoint gates
    on the ``superuser`` role directly. CSRF is enforced by the global
    ``check_csrf_for_state_change`` before-request hook (the endpoint is
    deliberately NOT in ``_CSRF_EXEMPT_ENDPOINTS``).

    Body (JSON): ``{"target": "<username>", "action": "set" | "clear"}``.
    The comp metadata write (``comped_active``/``comped_by``/``comped_at``)
    lives in ``SubscriptionStateService.set_comp`` / ``clear_comp``; this
    endpoint owns the authorization + audit.
    """
    from flask import jsonify

    actor = session.get("username", "")
    if not actor:
        return redirect(url_for("analytics.login_page"))
    if "superuser" not in session.get("roles", []):
        abort(403, description="Comping a subscription is limited to superusers.")

    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
    )

    payload = request.get_json(silent=True) or {}
    target = (payload.get("target") or "").strip()
    org_esid_arg = (payload.get("org_esid") or "").strip()
    workspace_esid_arg = (payload.get("workspace_esid") or "").strip()
    action = (payload.get("action") or "").strip()

    if not target and not org_esid_arg and not workspace_esid_arg:
        return jsonify({"status": "error", "message": "Missing target username, org_esid, or workspace_esid."}), 400
    if action not in ("set", "clear"):
        return jsonify({"status": "error", "message": "action must be 'set' or 'clear'."}), 400

    # workspace comp cascade. A workspace owner with NO org role
    # (prod case: cody) inherits the workspace's paid tier and gets capped,
    # and comping more orgs cannot fix them — comp must land on the WORKSPACE
    # billing record. Per a teammate's requirement (this ticket's approach (c)):
    # comp the workspace AND cascade an explicit org comp to every org under
    # it, so each org's record carries its own comped_by/comped_at and no org
    # holding a non-comped record is silently left outside the workspace comp.
    # get_state is deliberately untouched (every import gate reads it).
    #
    # Deliberately NO comp_granted/comp_revoked emails on this path: the
    # 2026-08-04 prod backfill precedent — N per-org emails for one
    # administrative workspace action is noise, not notification.
    #
    # Accepted trade-off (a teammate, 2026-08-04): an org created under a comped
    # workspace AFTER this runs does not inherit the comp — re-run the
    # workspace comp as orgs are added.
    if workspace_esid_arg:
        from asset_management.web.services.org_billing_service import OrgBillingService
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

        if action == "set":
            ws_result = WorkspaceBillingService.set_comp(workspace_esid_arg, actor)
        else:
            ws_result = WorkspaceBillingService.clear_comp(workspace_esid_arg)

        orgs = OrgBillingService.list_orgs_for_workspace(workspace_esid_arg)
        orgs_ok, orgs_failed = [], []
        for org in orgs:
            o_esid = (org.get("org_esid") or "").strip()
            if not o_esid:
                continue
            if action == "set":
                r = OrgBillingService.set_org_comp(o_esid, actor, workspace_esid=workspace_esid_arg)
            else:
                r = OrgBillingService.clear_org_comp(o_esid)
            (orgs_ok if r.get("ok") else orgs_failed).append(o_esid)

        AuditLogService.log_event(
            actor,
            f"billing_comp_workspace_{action}",
            bool(ws_result.get("ok")) and not orgs_failed,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=(
                f"workspace={workspace_esid_arg} ws_ok={ws_result.get('ok')} "
                f"orgs_ok={len(orgs_ok)} orgs_failed={len(orgs_failed)} "
                f"failed_esids={','.join(orgs_failed) or '-'}"
            ),
        )
        if not ws_result.get("ok"):
            return (
                jsonify({"status": "error", "message": "Workspace comp update failed. Please try again."}),
                502,
            )
        return (
            jsonify(
                {
                    "status": "ok",
                    "action": action,
                    "workspace_esid": workspace_esid_arg,
                    "orgs_comped": len(orgs_ok),
                    "orgs_failed": orgs_failed,
                }
            ),
            200,
        )

    # comp targets the ORG, not the workspace (the org is the billing
    # entity; a workspace can hold many orgs, e.g. kavanah-media holds 20, and
    # comping one must not comp the siblings). Two entry points resolve to the
    # SAME org operation below:
    # * org_esid (Workspace→Org picker) — comp THAT exact org; no
    #     user lookup.
    #   * target username/email — resolve that user's PRIMARY org and comp it
    #     (the original UX; a multi-org user lands on their primary only).
    # The org_esid path takes precedence when both are sent; ``target`` is then
    # used only to resolve a notification recipient + audit label.
    from asset_management.web.services.org_billing_service import OrgBillingService

    if org_esid_arg:
        # Org picker path — no user to validate; the org owner (if any) is the
        # email recipient.
        org_esid = org_esid_arg
        notify_user = OrgBillingService.find_org_owner(org_esid) or ""
        result = {"ok": True}  # provisional; replaced by the set/clear result below
    else:
        user_check = SubscriptionStateService.validate_comp_target(target)
        if not user_check.get("ok"):
            # carries NOT_FOUND_ERR / cannot_comp_superuser for the mapper below
            result = user_check
            org_esid = ""
            notify_user = ""
        else:
            org_esid = SubscriptionStateService.resolve_primary_org_esid(target)
            notify_user = target
            # A user who resolves to no org can't be comped; otherwise proceed.
            result = {"ok": False, "error": "no_org"} if not org_esid else {"ok": True}

    if org_esid and result.get("ok"):
        if action == "set":
            result = OrgBillingService.set_org_comp(org_esid, actor)
            if result.get("ok"):
                try:
                    from asset_management.web.services.billing_email_service import BillingEmailService

                    target_email = SubscriptionStateService.get_user_email(notify_user) if notify_user else ""
                    if target_email:
                        BillingEmailService.dispatch("comp_granted", target_email, {"full_name": ""})
                except Exception as mail_exc:  # noqa: BLE001 — email must never break the comp
                    logging.warning("comp_granted email failed for org=%r: %s", org_esid, mail_exc)
        else:  # clear
            result = OrgBillingService.clear_org_comp(org_esid)
            if result.get("ok") and result.get("was_comped"):
                try:
                    from asset_management.web.services.billing_email_service import BillingEmailService

                    target_email = SubscriptionStateService.get_user_email(notify_user) if notify_user else ""
                    if target_email:
                        BillingEmailService.dispatch("comp_revoked", target_email, {"full_name": ""})
                except Exception as mail_exc:  # noqa: BLE001 — email must never break the clear
                    logging.warning("comp_revoked email failed for org=%r: %s", org_esid, mail_exc)

    AuditLogService.log_event(
        actor,
        f"billing_comp_{action}",
        bool(result.get("ok")),
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"target={target or '-'} org_esid={org_esid_arg or '-'} ok={result.get('ok')} err={result.get('error') or ''}",
    )

    if not result.get("ok"):
        from asset_management.web.services.subscription_state_service import NOT_FOUND_ERR

        # A missing user surfaces as the NOT_FOUND_ERR sentinel (set/clear_comp
        # propagate it from _get_user) → clean 404 with a friendly message
        # instead of leaking the raw ES NotFoundError string.
        if result.get("error") == NOT_FOUND_ERR:
            return (
                jsonify({"status": "error", "message": f"No user found with the username or email “{target}”."}),
                404,
            )
        # Comping a superuser is rejected server-side — they already have
        # complimentary access.
        if result.get("error") == "cannot_comp_superuser":
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "That user is an administrator and already has complimentary access — no comp needed.",
                    }
                ),
                400,
            )
        # The user exists but doesn't resolve to any org we can bill (
        # org-billing) — can't comp an org we can't resolve.
        if result.get("error") == "no_org":
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": f"“{target}” doesn't resolve to any organization, so there's no org to comp.",
                    }
                ),
                400,
            )
        return (
            jsonify({"status": "error", "message": "Comp update failed. Please try again."}),
            502,
        )
    return jsonify({"status": "ok", "action": action, "target": target, "org_esid": org_esid_arg}), 200


@analytics_bp.route("/billing/cancel", methods=["POST"])
def billing_cancel_subscription():
    """Self-serve cancel of the signed-in user's subscription (
    2026-06-05).

    Same access as the rest of Billing (``_billing_access_guard`` —
    superuser / workspace_owner). CSRF via the global hook (NOT exempt).

    Behaviour by state:
      * **comped** (no real Stripe sub) → clears the comp
        (``clear_comp``); "cancel" for a comp means un-comp.
      * **paid** → schedules a Stripe cancel AT PERIOD END
        (``cancel_at_period_end=True``) so the user keeps access through the
        period they already paid for; the webhook flips them to canceled when
        the period ends.
      * anything else (trial/lapsed/canceled) → nothing to cancel → 409.
    """
    from flask import jsonify

    guard = _billing_access_guard()
    if guard is not None:
        return guard

    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
        STATE_ACTIVE,
    )

    username = session.get("username", "")
    state = SubscriptionStateService.get_state(username)

    # Comped → clearing the comp IS the cancel (no Stripe sub).
    # org-billing: clear the comp on the ORG (the billing entity get_state now
    # reads), falling back to the workspace then per-user comp for any record
    # written by the older paths. (In practice comped users have no Cancel button
    # — only an admin removes a comp via the comp control — so this branch is
    # defensive.)
    if state.get("is_comped"):
        from asset_management.web.services.org_billing_service import OrgBillingService
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

        _org = SubscriptionStateService.resolve_primary_org_esid(username)
        if _org and OrgBillingService.has_org_billing_record(_org):
            result = OrgBillingService.clear_org_comp(_org)
            _scope = f"org={_org}"
        else:
            _ws = WorkspaceBillingService.resolve_workspace_esid(username)
            result = WorkspaceBillingService.clear_comp(_ws) if _ws else SubscriptionStateService.clear_comp(username)
            _scope = f"workspace={_ws}"
        AuditLogService.log_event(
            username,
            "billing_cancel_comp",
            bool(result.get("ok")),
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"cancel = clear comp ({_scope})",
        )
        if not result.get("ok"):
            return jsonify({"status": "error", "message": "Could not cancel. Please try again."}), 502
        return jsonify({"status": "ok", "mode": "comp_cleared"}), 200

    # Not active (or not a paid sub) → nothing to cancel.
    if state["state"] != STATE_ACTIVE or not state.get("is_paid"):
        return jsonify({"status": "error", "message": "There is no active subscription to cancel."}), 409

    # Paid → schedule a Stripe cancel at period end.
    if not config.STRIPE_LIVE_PAYMENTS or not config.STRIPE_SECRET_KEY:
        return jsonify({"status": "error", "message": "Cancellation is not available right now."}), 503

    summary = _billing_subscription_summary(username)
    subscription_id = _billing_per_team_subscription_id(username) or (summary or {}).get("subscription_id")
    if not subscription_id:
        return jsonify({"status": "error", "message": "No active subscription found to cancel."}), 404

    from asset_management.web.services.stripe_service import StripeService, StripeError

    try:
        res = StripeService.cancel_subscription(
            secret_key=config.STRIPE_SECRET_KEY,
            subscription_id=subscription_id,
            at_period_end=True,
        )
    except StripeError as exc:
        AuditLogService.log_event(
            username,
            "billing_cancel_failed",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"sub={subscription_id} err={str(exc)[:200]}",
        )
        return jsonify({"status": "error", "message": "Could not cancel your subscription. Please try again."}), 502

    AuditLogService.log_event(
        username,
        "billing_cancel_scheduled",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"sub={subscription_id} cancel_at_period_end={res.get('cancel_at_period_end')}",
    )
    # Confirm the scheduled cancel by email (best-effort, async). The webhook
    # only sees a routine subscription.updated here, so the email is sent from
    # this route. The "subscription_canceled" email fires later, when the
    # period actually ends (subscription.deleted).
    try:
        from asset_management.web.services.billing_email_service import BillingEmailService

        email = SubscriptionStateService.get_user_email(username)
        if email:
            business = _billing_plan("business")
            BillingEmailService.dispatch(
                "cancel_scheduled",
                email,
                {
                    "full_name": "",
                    "plan_name": business.get("name", "Business"),
                    "date": (summary or {}).get("current_period_end", "")[:10],
                },
            )
    except Exception as mail_exc:  # noqa: BLE001 — email must never break the cancel
        logging.warning("cancel_scheduled email failed for %r: %s", username, mail_exc)
    return jsonify({"status": "ok", "mode": "at_period_end", "subscription_id": subscription_id}), 200


@analytics_bp.route("/billing/resume", methods=["POST"])
def billing_resume_subscription():
    """Undo a scheduled cancel — "uncancel".

    Same access as the rest of Billing; CSRF via the global hook. Only valid
    while the subscription is active with a cancel scheduled at period end;
    flips ``cancel_at_period_end`` back to False so it renews. After a full
    lapse there is nothing to resume (the user re-subscribes via Payment) →
    409.
    """
    from flask import jsonify

    guard = _billing_access_guard()
    if guard is not None:
        return guard

    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
        STATE_ACTIVE,
    )

    username = session.get("username", "")
    state = SubscriptionStateService.get_state(username)

    if state["state"] != STATE_ACTIVE or not state.get("is_paid"):
        return jsonify({"status": "error", "message": "There is no scheduled cancellation to resume."}), 409
    if not config.STRIPE_LIVE_PAYMENTS or not config.STRIPE_SECRET_KEY:
        return jsonify({"status": "error", "message": "Resume is not available right now."}), 503

    summary = _billing_subscription_summary(username)
    # O3 (flag-gated): target the member's TEAM subscription when
    # per-team billing is on (else the workspace sub). The cancel-scheduled
    # signal still reads the summary; the full per-team summary is O5.
    subscription_id = _billing_per_team_subscription_id(username) or (summary or {}).get("subscription_id")
    if not subscription_id:
        return jsonify({"status": "error", "message": "No subscription found to resume."}), 404
    if not (summary or {}).get("cancel_at_period_end"):
        # Active and NOT scheduled to cancel — nothing to undo.
        return jsonify({"status": "error", "message": "Your subscription is not scheduled to cancel."}), 409

    from asset_management.web.services.stripe_service import StripeService, StripeError

    try:
        StripeService.resume_subscription(secret_key=config.STRIPE_SECRET_KEY, subscription_id=subscription_id)
    except StripeError as exc:
        AuditLogService.log_event(
            username,
            "billing_resume_failed",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"sub={subscription_id} err={str(exc)[:200]}",
        )
        return jsonify({"status": "error", "message": "Could not resume your subscription. Please try again."}), 502

    AuditLogService.log_event(
        username,
        "billing_resume",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"sub={subscription_id}",
    )
    return jsonify({"status": "ok", "subscription_id": subscription_id}), 200


@analytics_bp.route("/billing/apply-coupon", methods=["POST"])
def billing_apply_coupon_to_subscription():
    """Apply a coupon to the signed-in user's EXISTING paid subscription
    .

    Self-serve: a paying user enters a coupon on their Current Plan page. We
    validate it against Stripe, then attach it to their live subscription
    (``Subscription.modify(coupon=...)``). Stripe applies the discount from the
    NEXT invoice onward — the current period is not refunded — so the response
    says so. Only meaningful for a PAID-active subscriber (not comped/superuser,
    who pay nothing). The coupon replaces any existing discount (Stripe holds at
    most one per subscription; coupons never stack).

    Responses mirror the checkout apply endpoint:
      * valid + applied → 200 ``{"status":"ok","discount_label":"60% off",
                                 "message": "...next bill..."}``
      * invalid code    → 200 ``{"status":"invalid","message":"Invalid code."}``
      * not a paying sub / live off → 409 / 503 with a clear message.
    CSRF via the global before-request hook.
    """
    from flask import jsonify

    guard = _billing_access_guard()
    if guard is not None:
        return guard

    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
        STATE_ACTIVE,
    )

    username = session.get("username", "")
    payload = request.get_json(silent=True) or {}
    code = (payload.get("coupon") or "").strip()[:64]
    # 2026-06-09 — the client re-posts confirm=True after the user
    # accepts the downgrade-confirmation prompt, bypassing that one check.
    confirmed = bool(payload.get("confirm"))

    if not code:
        return jsonify({"status": "invalid", "message": "Enter a coupon code."}), 200
    if _billing_field_invalid(code, "coupon"):
        return jsonify({"status": "invalid", "message": "Invalid code."}), 200

    state = SubscriptionStateService.get_state(username)
    # Only a real PAID-active subscriber can add a coupon to a subscription. A
    # comped/superuser pays nothing (no Stripe sub); a trial/lapsed user has no
    # live subscription to discount — they apply a coupon at checkout instead.
    if state["state"] != STATE_ACTIVE or not state.get("is_paid"):
        return (
            jsonify({"status": "error", "message": "A coupon can only be added to an active paid subscription."}),
            409,
        )
    if not config.STRIPE_LIVE_PAYMENTS or not config.STRIPE_SECRET_KEY:
        return jsonify({"status": "error", "message": "Coupons are not available right now."}), 503

    summary = _billing_subscription_summary(username)
    subscription_id = (summary or {}).get("subscription_id")
    if not subscription_id:
        return jsonify({"status": "error", "message": "No subscription found to apply the coupon to."}), 404

    from asset_management.web.services.stripe_service import StripeService, StripeError

    # Validate first so an invalid code returns a coupon-specific message and
    # leaves the subscription untouched. Pass the base-plan price so a coupon
    # scoped to a different product (e.g. the add-on-only mission coupon) is
    # rejected rather than attached to the plan subscription.
    try:
        verdict = StripeService.validate_coupon(
            secret_key=config.STRIPE_SECRET_KEY,
            code=code,
            price_id=_billing_plan_price_id(_BILLING_DEFAULT_PLAN),
        )
    except StripeError:
        return jsonify({"status": "error", "message": "Could not check that code. Please try again."}), 502
    if not verdict.get("valid"):
        AuditLogService.log_event(
            username,
            "billing_sub_coupon_invalid",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"reason={verdict.get('reason', '')}",
        )
        return jsonify({"status": "invalid", "message": "Invalid code."}), 200

    # Already-in-use guard: if the resolved coupon is
    # already the subscription's active discount, tell the user instead of
    # re-applying the same one (a no-op that would otherwise say "applied").
    if verdict.get("coupon") and verdict.get("coupon") == (summary or {}).get("discount_coupon"):
        return jsonify({"status": "in_use", "message": "This code is already in use."}), 200

    # Downgrade confirmation: if the subscription already
    # has a discount and the new code gives a SMALLER one (higher charged
    # total), ask the user to confirm before replacing it — so an accidental
    # code doesn't silently downgrade a better discount. They can still proceed
    # (confirm=True) or switch deliberately; we don't hard-block.
    if not confirmed and (summary or {}).get("has_discount"):
        list_price = _billing_plan(_BILLING_DEFAULT_PLAN)["price_usd"]
        new_total, _ = _apply_coupon_to_total(list_price, verdict)
        current_total = ((summary or {}).get("amount_total") or 0) / 100.0
        if new_total > current_total:
            return (
                jsonify(
                    {
                        "status": "confirm_downgrade",
                        "message": (
                            f"You currently pay ${current_total:,.2f}/month. This code changes it to "
                            f"${new_total:,.2f}/month — a smaller discount. Apply it anyway?"
                        ),
                    }
                ),
                200,
            )

    try:
        StripeService.apply_coupon_to_subscription(
            secret_key=config.STRIPE_SECRET_KEY,
            subscription_id=subscription_id,
            # Prefer the promotion code (enforces the promo's restrictions);
            # the bare coupon id remains a fallback.
            coupon=verdict.get("coupon", ""),
            promotion_code=verdict.get("promotion_code", ""),
        )
    except StripeError as exc:
        AuditLogService.log_event(
            username,
            "billing_sub_coupon_failed",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"sub={subscription_id} err={str(exc)[:200]}",
        )
        return jsonify({"status": "error", "message": "Could not apply that coupon. Please try again."}), 502

    AuditLogService.log_event(
        username,
        "billing_sub_coupon_applied",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"sub={subscription_id} coupon={verdict.get('coupon', '')}",
    )
    label = verdict.get("description") or "discount"
    next_date = (summary or {}).get("current_period_end", "")[:10]
    msg = f"Coupon applied ({label}) — it takes effect on your next bill"
    msg += f" on {next_date}." if next_date else "."
    # 2026-06-09 — return the new discounted price so the Current Plan
    # page updates the displayed amount in REAL TIME (no reload). Computed from
    # the plan list price + the validated coupon (mirrors what Stripe will
    # charge next period).
    list_price = _billing_plan(_BILLING_DEFAULT_PLAN)["price_usd"]
    new_total, _ = _apply_coupon_to_total(list_price, verdict)
    return (
        jsonify(
            {
                "status": "ok",
                "discount_label": label,
                "message": msg,
                "new_price": f"${new_total:,.2f}",
                "list_price": f"${list_price:,.2f}",
            }
        ),
        200,
    )


def _billing_addon_guard(username):
    """Shared gate for the self-serve add-on endpoints.

    Returns ``(subscription_id, None)`` when the caller may buy add-ons, else
    ``(None, (json, status))`` — an error response the route returns directly.
    Add-ons attach to a real PAID-active Stripe subscription only: a comped/
    superuser pays nothing, a trial/lapsed org has no live sub to extend.
    """
    from flask import jsonify

    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
        STATE_ACTIVE,
    )

    state = SubscriptionStateService.get_state(username)
    if state["state"] != STATE_ACTIVE or not state.get("is_paid"):
        return None, (jsonify({"status": "error", "message": "Add-on connections require an active paid subscription."}), 409)
    if not config.STRIPE_LIVE_PAYMENTS or not config.STRIPE_SECRET_KEY or not config.STRIPE_PRICE_ADDON_CONNECTION:
        return None, (jsonify({"status": "error", "message": "Add-on connections are not available right now."}), 503)
    summary = _billing_subscription_summary(username)
    subscription_id = (summary or {}).get("subscription_id")
    if not subscription_id:
        return None, (jsonify({"status": "error", "message": "No subscription found to add connections to."}), 404)
    return subscription_id, None


def _billing_addon_quantity(payload):
    """Parse + bound the requested add-on quantity (connections BEYOND the
    included cap). Returns ``(quantity, None)`` or ``(None, err_msg)``.
    Bounded to a sane ceiling so a typo can't order thousands."""
    raw = payload.get("quantity")
    try:
        qty = int(raw)
    except (TypeError, ValueError):
        return None, "Enter a whole number of connections."
    if qty < 0:
        return None, "Quantity can't be negative."
    # Hard cap: the TARGET total may not exceed the max.
    # qty here IS the resulting total (the client posts owned+delta), so this
    # one check bounds both a first buy and an increase, server-side — the
    # dropdown is convenience, this is the gate. 0 (full removal) is allowed.
    if qty > _BILLING_ADDON_MAX:
        return None, f"You can have at most {_BILLING_ADDON_MAX} extra connections — contact sales for more."
    return qty, None


def _billing_addon_ws_esid(username):
    """The user's billing WORKSPACE esid when they're workspace-billed, else
    None. Add-on stamps must land where get_state reads:
    a workspace-billed member's state comes from get_workspace_state, which
    shadows the per-user metadata — stamping the user there made a successful
    Stripe purchase look like nothing happened. Fail-soft None → per-user."""
    try:
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

        ws_esid = WorkspaceBillingService.resolve_workspace_esid(username)
        if ws_esid and WorkspaceBillingService.has_billing_record(ws_esid):
            return ws_esid
    except Exception as exc:  # noqa: BLE001 — resolution blip → per-user path
        logging.warning("_billing_addon_ws_esid failed for %r: %s", username, exc)
    return None


def _billing_addon_stamp(username, quantity):
    """Stamp the purchased add-on count where the user's get_state reads it
    (workspace doc when workspace-billed, else per-user metadata)."""
    from asset_management.web.services.subscription_state_service import SubscriptionStateService

    ws_esid = _billing_addon_ws_esid(username)
    if ws_esid:
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

        return WorkspaceBillingService.set_addon_connections(ws_esid, quantity)
    return SubscriptionStateService.set_addon_connections(username, quantity)


def _billing_reconcile_addons(username, state):
    """(2026-06-15) — STRIPE is the source of truth for how many add-on
    connections an org is actually paying for. The stored ``addon_connections``
    metadata can drift from the live Stripe item (a half-applied write, a
    webhook miss, or — how this surfaced — a manual stamp on a sub that never
    had the item). Drift mis-states "Currently purchased: N" and breaks the
    netting price. On render, read the live quantity and, on a CONFIRMED
    mismatch, re-stamp the metadata to match + clear any stale removal mark.

    Only runs for a PAID org with a configured add-on price + a resolvable
    subscription (comp/trial/lapsed have no add-on sub to reconcile). Fail-soft:
    if Stripe can't be read (returns None) the stored value is LEFT ALONE — a
    transient blip must never zero a real entitlement. Returns the (possibly
    refreshed) state dict so the caller renders the reconciled numbers."""
    try:
        if not (
            state.get("is_paid")
            and config.STRIPE_LIVE_PAYMENTS
            and config.STRIPE_SECRET_KEY
            and config.STRIPE_PRICE_ADDON_CONNECTION
        ):
            return state
        summary = _billing_subscription_summary(username)
        subscription_id = (summary or {}).get("subscription_id")
        if not subscription_id:
            return state

        from asset_management.web.services.stripe_service import StripeService

        live_qty = StripeService.get_addon_quantity(
            secret_key=config.STRIPE_SECRET_KEY,
            subscription_id=subscription_id,
            addon_price_id=config.STRIPE_PRICE_ADDON_CONNECTION,
        )
        if live_qty is None:  # couldn't read Stripe → don't touch stored state
            return state

        stored = _billing_addon_purchased_count(username)
        # A scheduled removal legitimately holds metadata ABOVE the live item
        # (paid capacity kept until period end) — don't "reconcile" that away.
        if _billing_addon_remove_at(username):
            return state
        if live_qty == stored:
            return state

        # Confirmed drift, no pending removal: trust Stripe, re-stamp, refetch.
        logging.warning(
            "add-on drift for %r: stored=%s live=%s — reconciling to live.",
            username,
            stored,
            live_qty,
        )
        _billing_addon_stamp(username, live_qty)
        from asset_management.web.services.subscription_state_service import SubscriptionStateService

        return SubscriptionStateService.get_state(username)
    except Exception as exc:  # noqa: BLE001 — reconciliation must never break the page
        logging.warning("_billing_reconcile_addons failed for %r: %s", username, exc)
        return state


def _billing_addon_remove_at(username):
    """The pending removal stamp visible to this user ('' when none) — read
    from the same surface get_state reads (workspace when workspace-billed)."""
    from asset_management.web.services.subscription_state_service import SubscriptionStateService

    ws_esid = _billing_addon_ws_esid(username)
    if ws_esid:
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

        return WorkspaceBillingService.get_addon_remove_at(ws_esid)
    return SubscriptionStateService.get_addon_remove_at(username)


def _billing_addon_mark_removal(username, remove_at, target_qty=0):
    """Mark the add-ons for a scheduled change (0 = removal, N = reduction)
    on the surface get_state reads."""
    from asset_management.web.services.subscription_state_service import SubscriptionStateService

    ws_esid = _billing_addon_ws_esid(username)
    if ws_esid:
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

        return WorkspaceBillingService.mark_addon_removal(ws_esid, remove_at, target_qty)
    return SubscriptionStateService.mark_addon_removal(username, remove_at, target_qty)


def _billing_addon_pending_qty(username):
    """The pending change's TARGET quantity ('' mark → 0) — same surface
    get_state reads."""
    from asset_management.web.services.subscription_state_service import SubscriptionStateService

    ws_esid = _billing_addon_ws_esid(username)
    if ws_esid:
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

        return WorkspaceBillingService.get_addon_pending_qty(ws_esid)
    return SubscriptionStateService.get_addon_pending_qty(username)


def _billing_addon_reduce(username, subscription_id, target, addon_coupon=""):
    """Handle a scheduled DECREASE: set the Stripe item
    to ``target`` (0 deletes it) with NO proration credit — the current
    period is already paid — and MARK the change for the period end, so the
    org keeps the capacity it paid for until then and the next bill carries
    only the target quantity. Money never flows back; increases (handled by
    the caller) charge immediately instead. Returns a Flask response."""
    from flask import jsonify

    from asset_management.web.services.stripe_service import StripeError, StripeService
    from asset_management.web.services.subscription_state_service import SubscriptionStateService

    owned = _billing_addon_purchased_count(username)
    if owned <= 0:
        return jsonify({"status": "invalid", "message": "You have no extra connections to remove."}), 200
    # An already-scheduled change is NOT a blocker (user decision 2026-06-12):
    # re-marking with a new target simply REPLACES the scheduled number — the
    # Stripe item moves to the new target and the stamp is overwritten below.

    summary = _billing_subscription_summary(username)
    remove_at = (summary or {}).get("current_period_end") or ""
    if not remove_at:
        return jsonify({"status": "error", "message": "Couldn't read your billing period. Please try again."}), 502

    try:
        StripeService.set_addon_connections(
            secret_key=config.STRIPE_SECRET_KEY,
            subscription_id=subscription_id,
            quantity=target,
            addon_price_id=config.STRIPE_PRICE_ADDON_CONNECTION,
            mission_coupon=addon_coupon,
            proration_behavior="none",  # no credit — the period is paid; capacity stays until remove_at
            # The Remove box has no coupon field — dropping 1 of 3 must not
            # strip an existing discount from the remaining 2.
            preserve_discounts=True,
        )
    except StripeError as exc:
        AuditLogService.log_event(
            username,
            "billing_addon_remove_failed",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"sub={subscription_id} target={target} err={str(exc)[:200]}",
        )
        return jsonify({"status": "error", "message": "Couldn't remove your connections. Please try again."}), 502

    stamp = _billing_addon_mark_removal(username, remove_at, target)
    if not stamp.get("ok"):
        # The Stripe item is already gone; without the mark the capacity
        # would vanish immediately instead of at period end — log loudly.
        logging.error(
            "addon reduction mark failed AFTER Stripe change for %r (sub=%s target=%s remove_at=%s): %s",
            username,
            subscription_id,
            target,
            remove_at,
            stamp.get("error"),
        )
    AuditLogService.log_event(
        username,
        "billing_addon_remove_scheduled",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"sub={subscription_id} owned={owned} target={target} remove_at={remove_at} marked={stamp.get('ok')}",
    )
    # Confirmation email: the removal generates NO
    # invoice (proration none), so the receipt email never announces it —
    # without this the next receipt is just silently smaller a month later.
    try:
        from asset_management.web.services.billing_email_service import BillingEmailService

        email = SubscriptionStateService.get_user_email(username)
        if email:
            BillingEmailService.dispatch(
                "addons_removal_scheduled",
                email,
                {"full_name": "", "count": owned - target, "date": remove_at[:10]},
            )
    except Exception as mail_exc:  # noqa: BLE001 — email must never break the removal
        logging.warning("addons_removal_scheduled email failed for %r: %s", username, mail_exc)
    return jsonify({"status": "ok", "quantity": target, "remove_at": remove_at}), 200


def _billing_addon_coupon(payload):
    """Validate an OPTIONAL user-entered coupon code for the add-on purchase
    (item 5b — the mission $5 rate is a code the user types, like any
    other coupon). Returns ``(promotion_code, None)`` on a valid code (or
    ``("", None)`` when no code was entered), or ``("", (json, status))`` an
    error response the route returns directly.

    The code is validated against the ADD-ON price, so a coupon scoped to a
    different product (e.g. a base-plan promo) is rejected here — and the
    add-on-scoped mission coupon is accepted. Discounts apply via the
    promotion_code (enforces the promo's own restrictions)."""
    from flask import jsonify

    code = (payload.get("coupon") or "").strip()[:64]
    if not code:
        return "", None  # no coupon entered — full add-on price
    if _billing_field_invalid(code, "coupon"):
        return "", (jsonify({"status": "invalid", "message": "Invalid code."}), 200)

    from asset_management.web.services.stripe_service import StripeService, StripeError

    try:
        verdict = StripeService.validate_coupon(
            secret_key=config.STRIPE_SECRET_KEY,
            code=code,
            price_id=config.STRIPE_PRICE_ADDON_CONNECTION,
        )
    except StripeError:
        return "", (jsonify({"status": "error", "message": "Could not check that code. Please try again."}), 502)
    if not verdict.get("valid"):
        msg = "That code doesn't apply to add-on connections." if verdict.get("reason") == "wrong_product" else "Invalid code."
        return "", (jsonify({"status": "invalid", "message": msg}), 200)
    # Prefer the promotion code (enforces redemption caps / expiry / eligibility);
    # fall back to the resolved coupon id.
    return (verdict.get("promotion_code") or verdict.get("coupon") or ""), None


def _billing_per_team_subscription_id(username):
    """The member's TEAM subscription id when per-team billing is ON (
    O3), else "" so the caller falls back to the workspace subscription. Lets
    cancel/resume target the right Stripe subscription under the org customer.
    Best-effort → "" on flag-off / no team / any error."""
    try:
        from asset_management.web.services.feature_flag_service import (
            FeatureFlagService,
            FLAG_ORG_BILLING_PER_TEAM,
        )

        if not FeatureFlagService.is_enabled(FLAG_ORG_BILLING_PER_TEAM, username):
            return ""
        from asset_management.web.services.org_billing_service import OrgBillingService

        team_esid = OrgBillingService.resolve_primary_team_esid(username)
        sub = OrgBillingService.get_team_subscription(team_esid) if team_esid else None
        return (sub or {}).get("stripe_subscription_id") or ""
    except Exception:  # noqa: BLE001
        return ""


def _billing_addon_purchased_count(username):
    """How many add-on connections the org has purchased (the stamped count =
    effective cap - included). 0 when none / on error. (the add-on
    coupon surface needs an existing add-on to discount.)"""
    try:
        from asset_management.web.services.subscription_state_service import SubscriptionStateService

        state = SubscriptionStateService.get_state(username)
        return max(0, int(state.get("connection_cap", _BILLING_CONNECTION_CAP)) - _BILLING_CONNECTION_CAP)
    except Exception:  # noqa: BLE001 — never break the route on a state blip
        return 0


def _billing_addon_has_discount(username, discount_id):
    """True iff the org's add-on item already carries ``discount_id`` (coupon or
    promotion_code) — the "already applied" guard. Best-effort: False
    on any error so a read blip doesn't wrongly block a legitimate apply."""
    if not discount_id:
        return False
    try:
        from asset_management.web.services.stripe_service import StripeService

        customer_id = _billing_user_stripe_customer_id(username)
        summary = _billing_subscription_summary(username)
        subscription_id = (summary or {}).get("subscription_id")
        if not subscription_id:
            return False
        ids = StripeService.get_addon_discount_ids(
            secret_key=config.STRIPE_SECRET_KEY,
            subscription_id=subscription_id,
            addon_price_id=config.STRIPE_PRICE_ADDON_CONNECTION,
        )
        return discount_id in ids
    except Exception:  # noqa: BLE001
        return False


@analytics_bp.route("/billing/addons/connections/preview", methods=["POST"])
def billing_addon_connections_preview():
    """Preview the immediate prorated charge for changing the add-on connection
    quantity, WITHOUT modifying the subscription.

    The Current Plan UI calls this as the user picks a quantity, to show
    "you'll be charged $X now". CSRF via the global before-request hook.
    Responses: ``{"status":"ok","amount_due":"$X.XX","quantity":N}`` or a
    409/503/404 from the shared guard / a 502 on a Stripe error.
    """
    from flask import jsonify

    guard = _billing_access_guard()
    if guard is not None:
        return guard
    username = session.get("username", "")
    payload = request.get_json(silent=True) or {}
    qty, qty_err = _billing_addon_quantity(payload)
    if qty_err:
        return jsonify({"status": "invalid", "message": qty_err}), 200

    subscription_id, err = _billing_addon_guard(username)
    if err is not None:
        return err

    # A DECREASE never charges (scheduled for the paid period end, no credit),
    # so there's nothing to preview. The Add box only ever previews ADDs (it
    # sends action=add); a reduce/no-tag-below-owned short-circuits. This keeps
    # the Add box from mis-reading an add-from-reduced-baseline as a reduction
    # (2026-06-15 — the "Could not price that change" the user hit).
    action = (payload.get("action") or "").strip().lower()
    if action != "add" and qty < _billing_addon_purchased_count(username):
        return jsonify({"status": "reduction", "quantity": qty}), 200

    customer_id = _billing_user_stripe_customer_id(username)

    # Optional user-entered add-on coupon so the preview reflects the discount.
    addon_coupon, coupon_err = _billing_addon_coupon(payload)
    if coupon_err is not None:
        return coupon_err

    from asset_management.web.services.stripe_service import StripeService, StripeError

    def _preview(quantity):
        return StripeService.preview_addon_proration(
            secret_key=config.STRIPE_SECRET_KEY,
            customer_id=customer_id,
            subscription_id=subscription_id,
            quantity=quantity,
            addon_price_id=config.STRIPE_PRICE_ADDON_CONNECTION,
            mission_coupon=addon_coupon,
            # Final coupon form: no purchase-time field, so the preview must
            # include an APPLIED discount, exactly like the buy will.
            preserve_discounts=not addon_coupon,
        )

    try:
        cents = int(_preview(qty).get("amount_due", 0) or 0)
        # Netting (2026-06-12): while a removal is scheduled the Stripe item is
        # below the capacity already PAID through period end, so the raw
        # preview would price re-buying it. The buy restores up to the paid
        # count for free and charges only beyond it — price the same way.
        owned = _billing_addon_purchased_count(username)
        if _billing_addon_remove_at(username) and owned > 0 and qty > owned:
            cents = max(0, cents - int(_preview(owned).get("amount_due", 0) or 0))
    except StripeError:
        return jsonify({"status": "error", "message": "Couldn't price that change. Please try again."}), 502
    return jsonify({"status": "ok", "amount_due": f"${cents / 100:,.2f}", "quantity": qty}), 200


@analytics_bp.route("/billing/addons/connections", methods=["POST"])
def billing_addon_connections():
    """Buy/adjust self-serve add-on data connections on the org's existing paid
    subscription. Adds/updates a $10/connection add-on item,
    prorated immediately (item 7), then stamps the purchased count so the
    entitlement gate grants the extra capacity (effective cap =
    CONNECTION_CAP + addon_connections).

    ``quantity`` is the number of connections BEYOND the included cap.
    ``quantity=0`` is the REMOVE flow (2026-06-12 rework): the Stripe item is
    deleted with NO proration credit (the period is already paid) and the
    capacity is MARKED for removal at the current period end — it keeps
    counting toward the cap until then and the next bill excludes it. An
    OPTIONAL ``coupon`` code (user-entered) discounts the add-on (validated
    against the add-on price, so a non-add-on code is rejected). CSRF via the
    global before-request hook.
    """
    from flask import jsonify

    guard = _billing_access_guard()
    if guard is not None:
        return guard
    username = session.get("username", "")
    payload = request.get_json(silent=True) or {}
    qty, qty_err = _billing_addon_quantity(payload)
    if qty_err:
        return jsonify({"status": "invalid", "message": qty_err}), 200

    subscription_id, err = _billing_addon_guard(username)
    if err is not None:
        return err

    # Optional user-entered add-on coupon (e.g. the mission $5 code). Invalid +
    # wrong-product are rejected inside the helper (validated vs the add-on
    # price).
    addon_coupon, coupon_err = _billing_addon_coupon(payload)
    if coupon_err is not None:
        return coupon_err

    # ADD vs REDUCE is disambiguated by an explicit action tag, NOT by
    # comparing the number to the current count (2026-06-15): while a removal
    # is scheduled the same target can mean either "reduce further" (Remove
    # box) or "add back from the reduced baseline" (Add box), so the client
    # says which. Back-compat: no tag + a target below raw-owned (or 0) is a
    # reduce, matching the pre-tag behavior for older clients.
    action = (payload.get("action") or "").strip().lower()
    is_reduce = action == "remove" or (not action and (qty == 0 or qty < _billing_addon_purchased_count(username)))
    if is_reduce:
        return _billing_addon_reduce(username, subscription_id, qty, addon_coupon or "")

    # Already-added guard: a no-op purchase that ONLY re-supplies a
    # code already on the add-on item — same quantity, same discount — is told
    # "already applied" rather than charging/re-applying. (A quantity change
    # with the same code is legitimate — the discount must carry to the new
    # quantity — so the guard only fires when nothing changes.)
    if (
        addon_coupon
        and qty == _billing_addon_purchased_count(username)
        and _billing_addon_has_discount(username, addon_coupon)
    ):
        return jsonify({"status": "in_use", "message": "This code is already applied to your add-ons."}), 200

    from asset_management.web.services.stripe_service import StripeService, StripeError
    from asset_management.web.services.subscription_state_service import SubscriptionStateService

    try:
        # Netting (2026-06-12): while a removal is scheduled, the org has
        # ALREADY PAID for ``owned`` connections through period end but the
        # Stripe item sits below that. Restore up to the paid count for FREE
        # (proration none), then charge prorated only for what's beyond it —
        # otherwise a mark-then-add would double-pay the marked connections.
        owned_paid = _billing_addon_purchased_count(username)
        if _billing_addon_remove_at(username) and 0 < owned_paid < qty:
            StripeService.set_addon_connections(
                secret_key=config.STRIPE_SECRET_KEY,
                subscription_id=subscription_id,
                quantity=owned_paid,
                addon_price_id=config.STRIPE_PRICE_ADDON_CONNECTION,
                proration_behavior="none",
                preserve_discounts=True,
            )
        StripeService.set_addon_connections(
            secret_key=config.STRIPE_SECRET_KEY,
            subscription_id=subscription_id,
            quantity=qty,
            addon_price_id=config.STRIPE_PRICE_ADDON_CONNECTION,
            mission_coupon=addon_coupon,
            # 2026-06-12 (final form): purchases carry no coupon field — the
            # "Have a coupon?" box is the single surface — so a no-coupon buy
            # must PRESERVE an applied discount, not clear it.
            preserve_discounts=not addon_coupon,
        )
    except StripeError as exc:
        AuditLogService.log_event(
            username,
            "billing_addon_connections_failed",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"sub={subscription_id} qty={qty} err={str(exc)[:200]}",
        )
        return jsonify({"status": "error", "message": "Couldn't update your connections. Please try again."}), 502

    # Persist the purchased count so the entitlement gate grants the capacity.
    # Stripe charged successfully above; if this stamp fails the user paid but
    # the gate wouldn't reflect it — log loudly so it can be reconciled.
    # Dual-path (2026-06-12): a workspace-billed member's get_state reads the
    # WORKSPACE doc, so the stamp must land there, not on the user.
    stamp = _billing_addon_stamp(username, qty)
    if not stamp.get("ok"):
        logging.error(
            "addon stamp failed AFTER Stripe charge for %r (sub=%s qty=%s): %s",
            username,
            subscription_id,
            qty,
            stamp.get("error"),
        )
    AuditLogService.log_event(
        username,
        "billing_addon_connections_set",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"sub={subscription_id} qty={qty} stamped={stamp.get('ok')}",
    )
    return jsonify({"status": "ok", "quantity": qty}), 200


@analytics_bp.route("/billing/addons/coupon", methods=["POST"])
def billing_addon_coupon():
    """Apply a coupon to ALREADY-PURCHASED add-on connections — the "Have a
    coupon for your add-ons?" surface on Current Plan.

    Unlike the add-on PURCHASE route this changes no quantity and charges
    nothing now: the discount attaches to the existing add-on item and takes
    effect on the NEXT bill (mirrors the base-plan "Have a coupon?"). Guards
    (per the request): an INVALID code → "Invalid code."; a WRONG-PRODUCT code
    (scoped to a different product) → a clear message; a code ALREADY applied to
    the add-on → "in use" (no-op, not a misleading "applied"). All reject before
    touching Stripe. Paid-active + an existing add-on required. CSRF via the
    global before-request hook.
    """
    from flask import jsonify

    guard = _billing_access_guard()
    if guard is not None:
        return guard
    username = session.get("username", "")
    payload = request.get_json(silent=True) or {}
    code = (payload.get("coupon") or "").strip()[:64]
    if not code:
        return jsonify({"status": "invalid", "message": "Enter a coupon code."}), 200

    subscription_id, err = _billing_addon_guard(username)
    if err is not None:
        return err

    # Must already HAVE add-ons to discount them.
    if _billing_addon_purchased_count(username) <= 0:
        return jsonify({"status": "error", "message": "You have no add-on connections to apply a coupon to."}), 409

    # Validate the code AGAINST THE ADD-ON PRODUCT — invalid + wrong-product
    # both reject here (the shared helper returns the error response).
    addon_coupon, coupon_err = _billing_addon_coupon(payload)
    if coupon_err is not None:
        return coupon_err
    if not addon_coupon:
        # Defensive: a blank/again-empty code shouldn't reach the apply.
        return jsonify({"status": "invalid", "message": "Enter a coupon code."}), 200

    # Already-applied guard: if the add-on item already carries this discount,
    # say so instead of re-applying (a no-op that would wrongly read "applied").
    if _billing_addon_has_discount(username, addon_coupon):
        return jsonify({"status": "in_use", "message": "This code is already applied to your add-ons."}), 200

    from asset_management.web.services.stripe_service import StripeService, StripeError

    # Worse-value confirmation (2026-06-15 — parity with the plan
    # "Have a coupon?" box): if the add-on item ALREADY has a discount and the
    # new code is a SMALLER one (higher add-on total), ask before replacing it
    # so an accidental code doesn't silently downgrade a better add-on
    # discount. The user can still proceed (confirm=True); we don't hard-block.
    if not bool(payload.get("confirm")):
        try:
            current = StripeService.get_addon_discount_value(
                secret_key=config.STRIPE_SECRET_KEY,
                subscription_id=subscription_id,
                addon_price_id=config.STRIPE_PRICE_ADDON_CONNECTION,
            )
        except StripeError:
            current = {}
        if current:
            try:
                new_verdict = StripeService.validate_coupon(
                    secret_key=config.STRIPE_SECRET_KEY,
                    code=code,
                    price_id=config.STRIPE_PRICE_ADDON_CONNECTION,
                )
            except StripeError:
                new_verdict = {}
            owned = _billing_addon_purchased_count(username)
            base = owned * _BILLING_ADDON_CONNECTION_PRICE
            new_total, _ = _apply_coupon_to_total(base, new_verdict)
            current_total, _ = _apply_coupon_to_total(base, current)
            if new_total > current_total:
                return (
                    jsonify(
                        {
                            "status": "confirm_downgrade",
                            "message": (
                                f"Your add-ons are currently ${current_total:,.2f}/month. This code changes "
                                f"them to ${new_total:,.2f}/month — a smaller discount. Apply it anyway?"
                            ),
                        }
                    ),
                    200,
                )

    # The resolved id is a promotion_code (promo_…) when available, else a
    # coupon id — pass it in the matching slot so the promo's restrictions hold.
    is_promo = addon_coupon.startswith("promo_")
    try:
        StripeService.apply_coupon_to_addon(
            secret_key=config.STRIPE_SECRET_KEY,
            subscription_id=subscription_id,
            addon_price_id=config.STRIPE_PRICE_ADDON_CONNECTION,
            promotion_code=addon_coupon if is_promo else "",
            coupon="" if is_promo else addon_coupon,
        )
    except StripeError as exc:
        AuditLogService.log_event(
            username,
            "billing_addon_coupon_failed",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"sub={subscription_id} err={str(exc)[:200]}",
        )
        return jsonify({"status": "error", "message": "Could not apply that coupon. Please try again."}), 502

    AuditLogService.log_event(
        username,
        "billing_addon_coupon_applied",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"sub={subscription_id} discount={addon_coupon}",
    )
    return jsonify({"status": "ok", "message": "Coupon applied to your add-ons — it takes effect on your next bill."}), 200


@analytics_bp.route("/billing/payment-method", methods=["POST"])
def billing_update_payment_method():
    """Change the card on an existing paid subscription.

    Used by the "change card details" form on the Payment page for a PAID-active
    user — attaches the new card and makes it the subscription's default so it's
    used on the NEXT billing period. Does NOT create a new subscription.

    Billing access + CSRF (via the global hook). The browser POSTs the Stripe
    ``payment_method_id`` (card captured by Elements) plus the same full
    billing details as the pay form (name/company/address); these refresh the
    Stripe Customer's stored billing details. Comped / not-active / no-sub →
    409 / 404 (nothing to update).
    """
    from flask import jsonify

    guard = _billing_access_guard()
    if guard is not None:
        return guard

    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
        STATE_ACTIVE,
    )

    username = session.get("username", "")
    payload = request.get_json(silent=True) or {}
    payment_method_id = (payload.get("payment_method_id") or "").strip()
    if not payment_method_id:
        return jsonify({"status": "error", "message": "Missing payment method."}), 400

    # Full billing details (same shape + bounds as create-subscription). Trim
    # and bound each, then enforce the SAME server-side character classes so a
    # crafted POST can't smuggle invalid characters into the Stripe Customer.
    billing_name = (payload.get("name") or "").strip()[:150]
    billing_company = (payload.get("company") or "").strip()[:150]
    _addr_in = payload.get("address") or {}
    billing_address = {
        "line1": (str(_addr_in.get("line1") or "")).strip()[:200],
        "city": (str(_addr_in.get("city") or "")).strip()[:100],
        "state": (str(_addr_in.get("state") or "")).strip()[:100],
        "postal_code": (str(_addr_in.get("postal_code") or "")).strip()[:10],
        "country": (str(_addr_in.get("country") or "")).strip()[:100],
    }
    for _val, _kind in (
        (billing_name, "name"),
        (billing_company, "company"),
        (billing_address["line1"], "address"),
        (billing_address["city"], "name"),
        (billing_address["state"], "name"),
        (billing_address["postal_code"], "postal"),
        (billing_address["country"], "name"),
    ):
        if _billing_field_invalid(_val, _kind):
            return jsonify({"status": "error", "message": "Billing details contain invalid characters."}), 400

    state = SubscriptionStateService.get_state(username)
    # Only a real PAID subscription has a card to change. A comp has no Stripe
    # sub; trial/lapsed/canceled have nothing to update.
    if state["state"] != STATE_ACTIVE or not state.get("is_paid"):
        return jsonify({"status": "error", "message": "There is no active subscription to update."}), 409
    if not config.STRIPE_LIVE_PAYMENTS or not config.STRIPE_SECRET_KEY:
        return jsonify({"status": "error", "message": "Card updates are not available right now."}), 503

    customer_id = _billing_user_stripe_customer_id(username)
    summary = _billing_subscription_summary(username)
    subscription_id = (summary or {}).get("subscription_id")
    if not customer_id or not subscription_id:
        return jsonify({"status": "error", "message": "No active subscription found to update."}), 404

    from asset_management.web.services.stripe_service import StripeService, StripeError

    try:
        StripeService.update_payment_method(
            secret_key=config.STRIPE_SECRET_KEY,
            customer_id=customer_id,
            subscription_id=subscription_id,
            payment_method_id=payment_method_id,
            customer_name=billing_name,
            customer_address=billing_address,
            company=billing_company,
        )
    except StripeError as exc:
        AuditLogService.log_event(
            username,
            "billing_card_update_failed",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=f"sub={subscription_id} err={str(exc)[:200]}",
        )
        return (
            jsonify({"status": "error", "message": "Your card could not be updated. Please check the details and try again."}),
            402,
        )

    AuditLogService.log_event(
        username,
        "billing_card_updated",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"sub={subscription_id}",
    )
    return jsonify({"status": "ok", "subscription_id": subscription_id}), 200


@analytics_bp.route("/billing/comp/search")
def billing_comp_search():
    """Superuser comp user-search.

    Backs the Current Plan comp autocomplete: given ``?q=``, returns up to 10
    matching users by username OR email substring. Superuser-ONLY (same gate
    as the comp endpoint). A query under 2 chars returns an empty list.
    """
    from flask import jsonify

    if not session.get("username"):
        return redirect(url_for("analytics.login_page"))
    if "superuser" not in session.get("roles", []):
        abort(403, description="Comp search is limited to superusers.")

    from asset_management.web.services.subscription_state_service import (
        SubscriptionStateService,
    )

    query = (request.args.get("q") or "").strip()
    return jsonify({"results": SubscriptionStateService.search_users(query)}), 200


@analytics_bp.route("/billing/comp/list")
def billing_comp_list():
    """Superuser roster of currently-comped accounts.

    Backs the "Currently comped" list on the Current Plan comp control so the
    superuser can see (and clear) who has been comped. Superuser-ONLY.

     org-billing: comp now targets the ORG (set/clear write
    ``de_billing_org``), so the roster reads the ORG records. the
    per-row clear posts the row's own ``org_esid`` (→ ``clear_org_comp``), so it
    works for an org with no owner and never routes through the owner's PRIMARY
    org; ``username`` is carried for display and as a fallback only. Returns
    ``{"results": [{username, email, org_esid, org_title, comped_by, comped_at,
    workspace_esid}]}`` for every org with ``comped_active=True``.
    """
    from flask import jsonify

    if not session.get("username"):
        return redirect(url_for("analytics.login_page"))
    if "superuser" not in session.get("roles", []):
        abort(403, description="The comp roster is limited to superusers.")

    from asset_management.web.services.org_billing_service import OrgBillingService

    return jsonify({"results": OrgBillingService.list_comped_orgs()}), 200


@analytics_bp.route("/billing/comp/workspaces")
def billing_comp_workspaces():
    """Superuser workspace-search for the org-comp picker.

    Backs the Workspace autocomplete in the comp control: given ``?q=``, returns
    up to 10 matching workspaces by title. Superuser-ONLY (same gate as the comp
    endpoint). A query under 2 chars returns an empty list.
    """
    from flask import jsonify

    if not session.get("username"):
        return redirect(url_for("analytics.login_page"))
    if "superuser" not in session.get("roles", []):
        abort(403, description="Workspace search is limited to superusers.")

    from asset_management.web.services.org_billing_service import OrgBillingService

    query = (request.args.get("q") or "").strip()
    return jsonify({"results": OrgBillingService.search_workspaces(query)}), 200


@analytics_bp.route("/billing/comp/orgs")
def billing_comp_orgs():
    """Superuser org-list for the org-comp picker.

    Backs the Org dropdown in the comp control: given ``?workspace_esid=``,
    returns every org under that workspace as ``{org_esid, org_title}``.
    Superuser-ONLY (same gate as the comp endpoint).
    """
    from flask import jsonify

    if not session.get("username"):
        return redirect(url_for("analytics.login_page"))
    if "superuser" not in session.get("roles", []):
        abort(403, description="Org listing is limited to superusers.")

    from asset_management.web.services.org_billing_service import OrgBillingService

    workspace_esid = (request.args.get("workspace_esid") or "").strip()
    return jsonify({"results": OrgBillingService.list_orgs_for_workspace(workspace_esid)}), 200


# ============================================
# ONBOARDING STEP 7 — BUILDING SCREEN + BACKFILL PROGRESS API
# ============================================
# After the step-6 finish handler spawns the on-demand FB importer and
# writes the initial ``de_import_progress`` doc, the user is redirected
# to ``/onboarding/step7/building`` — a "Building your dashboard…"
# spinner that polls the progress endpoint every second and either
# advances to the first-look dashboard on first-data OR after a 15s
# ceiling.
#
# The progress endpoint is read by both surfaces:
#  * the building screen's per-second poll (decides when to redirect),
#  * the dashboard sync pill's per-5s poll (renders the live %).
#
# Both routes were historically gated by the new-onboarding feature
# flag (retired, and dropped from these two routes even
# earlier — see the notes below). The progress
# endpoint scopes by ``session["username"]`` — never accepts a
# query-string username — so user A cannot fetch user B's
# backfill state.


# phase-weighted progress. The Meta pull runs ad insights →
# page insights → instagram. The day counter (days_fetched/days_total)
# only advances during the ad-insights loop, so a naive
# days_fetched/days_total saturates at 100% the moment that loop ends
# while the other two phases still run. Map each phase onto a band of the
# overall 0–100 bar so it keeps moving, and reserve 100 for the terminal
# success stamp.
#
# Bands (upper bound is the percent shown while that phase is the LAST one
# that has reported): ad insights fills 0→60 proportional to
# days_fetched/days_total; page insights holds at 85; instagram holds at
# 99. 100 is emitted only when the route's caller observes a terminal
# ``succeeded`` status. The bands are coarse on purpose — page/instagram
# don't report per-day progress, so we can't smoothly interpolate them;
# the goal is "the bar keeps moving and never lies about being done", not
# a precise ETA.
_BACKFILL_AD_BAND_TOP = 60
_BACKFILL_PHASE_FLOOR = {
    # phase value on the doc -> the percent the bar should show once that
    # phase is running. ad_insights is handled separately (proportional).
    "page_insights": 85,
    "instagram": 99,
    # every selected source is switched off for the scoped team(s);
    # the run does no fetching. Hold at 99 so the bar doesn't imply work.
    "all_sources_disabled": 99,
}
_BACKFILL_PHASE_LABELS = {
    "ad_insights": "Importing ad insights…",
    "page_insights": "Importing page insights…",
    "instagram": "Importing Instagram insights…",
    "all_sources_disabled": "All import sources are turned off for this team — nothing to import",
}


def _backfill_percent(*, days_total, days_fetched, phase, status):
    """overall import percent across all phases (0–100).

    ``status`` ``succeeded`` is the only path to 100. While the run is
    in-flight the value is capped below 100 so the bar never reads "done"
    before the data has actually landed. The page/instagram phases hold
    at their band floor (85 / 99) because they don't report per-day
    progress; the ad-insights phase fills 0→60 proportional to the day
    counter.
    """
    if status in ("succeeded", "completed"):
        return 100

    # Page / instagram phases: a fixed band floor (they don't tick days).
    floor = _BACKFILL_PHASE_FLOOR.get(phase)
    if floor is not None:
        return floor

    # Ad-insights phase (explicit "ad_insights" or the implicit pre-phase
    # state where phase is empty): proportional fill across the 0→60 band.
    if days_total > 0:
        frac = days_fetched / days_total
        if frac < 0:
            frac = 0.0
        if frac > 1:
            frac = 1.0
        return min(_BACKFILL_AD_BAND_TOP, round(_BACKFILL_AD_BAND_TOP * frac))
    return 0


def _backfill_phase_label(phase):
    """human-readable label for the active phase, or "" if none."""
    return _BACKFILL_PHASE_LABELS.get(phase, "")


@analytics_bp.route("/onboarding/api/backfill-progress")
def onboarding_backfill_progress():
    """Return the calling user's latest import-progress doc as JSON.

    Response shapes:

      * 200 ``{connector_id, connector_name, days_total, days_fetched,
              percent, status, started_at, updated_at}`` — a progress
              doc exists.
      * 404 ``{status: "no_progress"}`` — no doc exists for this user
              (the importer hasn't kicked off yet, or the user is on a
              dashboard without an active backfill).
      * 401 ``{status: "unauthenticated"}`` — anonymous request.

    The 404 / 200 split is the load-bearing contract for the building
    screen's poller: it treats 404 as "keep waiting" and 200 as
    "advance if days_fetched ≥ 1." A regression that returned 200 +
    nulls or 500 on no_progress would break the redirect logic.

     (reopen, 2026-06-01) — the new-onboarding feature-flag
    gate was removed for the same reason as the sibling cancel route
    (the flag itself was later retired everywhere):
    the dashboard custom-range "pull new data" banner polls this route,
    but on prod (flag off / cohort) the flag evaluated False
    for ``user_id=None`` so the route 404'd and the poller treated it as
    "no doc yet — keep waiting" forever. That's why the import ran with
    NO visible % even though the backend recorded ``days_fetched``
    correctly. The route is already scoped to ``session['username']``
    (never a query-string user), so the gate added no privacy — only the
    silent-banner bug.
    """
    username = session.get("username", "")
    if not username:
        return {"status": "unauthenticated"}, 401

    try:
        from lib.import_progress_service import ImportProgressService
    except Exception as e:  # noqa: BLE001
        logging.warning("backfill-progress: service import failed: %s", e)
        return {"status": "no_progress"}, 404

    doc = ImportProgressService.get_latest_for_user(username)
    if not doc:
        return {"status": "no_progress"}, 404

    days_total = int(doc.get("days_total") or 0)
    days_fetched = int(doc.get("days_fetched") or 0)
    raw_phase = doc.get("phase") or ""
    raw_status_for_pct = doc.get("status") or ""
    # phase-aware overall percent. The Meta pull runs three
    # sequential phases: ad insights (the per-date loop that bumps
    # days_fetched), then page insights, then instagram. Pre-fix the
    # percent was days_fetched/days_total, which saturated at 100% the
    # instant the ad loop finished — so the bar sat frozen at 100% while
    # page + instagram still ran for the full date range ("100% but still
    # spinning"). Map each phase onto a band of the overall bar so it keeps
    # advancing through every phase, and reserve 100% for the terminal
    # ``succeeded`` status only.
    percent = _backfill_percent(
        days_total=days_total,
        days_fetched=days_fetched,
        phase=raw_phase,
        status=raw_status_for_pct,
    )

    connector_id = doc.get("connector_id") or "unknown"
    connector_name = _connector_display_name(connector_id)

    # follow-up STALE-001 — surface ``stale`` to the pill
    # when a pending doc's importer has gone dark (container
    # restarted mid-import, kernel OOM, etc.). The doc in ES is
    # NOT mutated; only the response payload is interpreted. The
    # pill JS treats stale the same as failed (renders error pill).
    #
    # 10 minutes is wide enough that a real backfill doesn't
    # trigger (a 90d Meta Ads pull is well under 10 minutes for a
    # single ad account) but narrow enough that a stuck importer
    # surfaces inside one dashboard session.
    raw_status = doc.get("status") or "unknown"
    surfaced_status = raw_status
    if raw_status in ("pending", "cancelling"):
        from datetime import datetime, timedelta, timezone

        # a ``cancelling`` doc whose worker died before it could
        # stamp the terminal ``cancelled`` status (SIGKILL, container
        # restart, or a worker wedged in a Graph call past the new socket
        # timeout) would otherwise leave the banner spinning on
        # "Cancelling…" forever — exactly the staging symptom that opened
        # this ticket (run_id ec5c2cb0 sat at ``cancelling`` for a day).
        # Treat a stale ``cancelling`` doc as terminal ``cancelled`` so the
        # banner resolves; a stale ``pending`` doc stays ``stale`` (it was
        # never cancelled, just abandoned). The doc in ES is NOT mutated —
        # only the response payload is reinterpreted, matching the existing
        # STALE-001 convention.
        #
        # ``updated_at`` (not ``started_at``) anchors the cancelling case:
        # the cancel request bumps updated_at, so the window measures time
        # since the user asked to stop, not since the import began.
        anchor_field = "updated_at" if raw_status == "cancelling" else "started_at"
        anchor_str = doc.get(anchor_field) or doc.get("started_at") or ""
        try:
            anchor_at = datetime.fromisoformat(anchor_str)
        except (TypeError, ValueError):
            anchor_at = None
        if anchor_at is not None:
            age = datetime.now(tz=timezone.utc) - anchor_at
            if age > timedelta(minutes=10):
                surfaced_status = "cancelled" if raw_status == "cancelling" else "stale"

    # surface the failure reason to the banner. The importer
    # records a human-readable ``error`` on the progress doc when it fails
    # (e.g. the lock-loss message "another import is already in
    # progress; this run was skipped"), but the response never forwarded it,
    # so the banner degraded every failure to a bare "unknown error"
    # (dashboards.html falls back to that when ``progress.error`` is absent).
    # Pass the stored error through, and synthesize one for the STALE-001
    # reinterpretation (a ``pending`` doc whose importer went dark leaves no
    # stored error — a container restart mid-import is the common cause).
    surfaced_error = doc.get("error") or ""
    if surfaced_status == "stale" and not surfaced_error:
        surfaced_error = (
            "the import stopped responding (its process may have been " "interrupted, e.g. by a restart); please try again"
        )

    # Response shape pinned — the user's connection_esid and other
    # identifiers stay server-side. The pill/builder JS never needs
    # them; surfacing them would be incremental privacy leak surface.
    #
    # item 2 — run_id IS surfaced so the banner's Cancel
    # button has something to address the cancel endpoint with. The
    # run_id is a server-minted uuid scoped to this user's progress
    # doc; the cancel endpoint re-checks ownership before honouring
    # the request so the surface isn't a privilege-escalation vector.
    return {
        "run_id": doc.get("run_id") or "",
        "connector_id": connector_id,
        "connector_name": connector_name,
        "days_total": days_total,
        "days_fetched": days_fetched,
        "percent": int(percent),
        "status": surfaced_status,
        # follow-up 3 — pipeline stage (ad_insights implicit during
        # the per-date loop, then page_insights / instagram). Lets the pill
        # show "Fetching page insights…" instead of a static 100% while the
        # non-per-date post-loop stages run. Empty string when unset (older
        # docs / cron path) so the JS can treat absence as "no sub-phase".
        "phase": raw_phase,
        # human-readable phase label for the banner so the user
        # sees which phase is live (e.g. "Importing page insights…") while
        # the bar advances through the page/instagram bands. Empty string
        # when no sub-phase applies.
        "phase_label": _backfill_phase_label(raw_phase),
        # failure reason for the banner (empty unless the run
        # failed / went stale). Lets the banner show the real cause instead
        # of the "unknown error" fallback.
        "error": surfaced_error,
        "started_at": doc.get("started_at") or "",
        "updated_at": doc.get("updated_at") or "",
    }, 200


@analytics_bp.route("/onboarding/api/import/<run_id>/cancel", methods=["POST"])
def onboarding_import_cancel(run_id):
    """item 2 — flip an in-flight import's status to
    ``cancelling`` so the importer's per-iteration check bails out.

    Auth: caller must own the progress doc (same username). We fetch
    the doc BY run_id and confirm its ``username`` matches the session
    so an attacker who scraped a run_id can't cancel another user's
    backfill.

    Response shapes:

      * 200 ``{ok: true, status: "cancelling"}`` — accepted.
      * 200 ``{ok: false, reason: "already_terminal"}`` — the doc
              already reached succeeded/failed/cancelled; the importer
              already exited.
      * 401 ``{ok: false, reason: "unauthenticated"}`` — anonymous.
      * 404 ``{ok: false, reason: "no_progress"}`` — no doc for this
              run_id.
      * 403 ``{ok: false, reason: "wrong_user"}`` — the run_id
              points at a doc owned by a different user.

    Partial data already written stays in ES — the importer's loop
    breaks between (team, date) iterations, so any data fetched
    before the cancel is preserved. Matches user mental model:
    "stop running, keep what you've got."

     (reopen, 2026-06-01) — two bugs fixed here:

      1. This route used to ``abort(404)`` unless the new-onboarding
         feature flag (retired) evaluated True. That flag
         was evaluated with ``user_id=None``, which is
         False in ``mode='off'`` AND ``mode='cohort'`` — so on prod (flag off)
         EVERY cancel 404'd silently, while the ungated kickoff route
         (``/api/dashboards/meta/kickoff-range-import``) still started imports.
         The dashboard Cancel is no longer a wizard-only feature, so the gate is
         removed entirely. The import-progress service is independently
         fail-soft; an anonymous/abuse request is still rejected by the auth +
         ownership checks below.
      2. Ownership was checked against ``get_latest_for_user`` (latest doc by
         ``updated_at``), so a legitimate cancel of an older-but-running run
         was rejected ``wrong_user`` whenever the user had a newer doc. Now we
         fetch the doc BY run_id and compare its ``username``.
    """
    username = session.get("username", "")
    if not username:
        return {"ok": False, "reason": "unauthenticated"}, 401

    try:
        from lib.import_progress_service import ImportProgressService
    except Exception as e:  # noqa: BLE001
        logging.warning("cancel: service import failed: %s", e)
        return {"ok": False, "reason": "service_unavailable"}, 500

    # log the inbound cancel so a prod repro shows exactly which
    # run_id the route received and which branch it took (a teammate's WHAT-FIX-
    # NEEDS #1). The progress-banner symptom was indistinguishable between
    # "front-end never fired the POST" and "route rejected it" without this.
    logging.info("cancel: received run_id=%r user=%r", run_id, username)

    # Ownership check: fetch the doc BY run_id and confirm it belongs to
    # the caller. Keeps the privacy guarantee (can't cancel a foreign run)
    # without the latest-doc fragility.
    doc = ImportProgressService.get_run(run_id)
    if not doc:
        logging.info("cancel: no doc for run_id=%r", run_id)
        return {"ok": False, "reason": "no_progress"}, 404
    if (doc.get("username") or "") != username:
        logging.warning(
            "cancel: ownership mismatch run_id=%r doc_user=%r session_user=%r",
            run_id,
            doc.get("username"),
            username,
        )
        return {"ok": False, "reason": "wrong_user"}, 403

    accepted = ImportProgressService.cancel_run(run_id)
    if not accepted:
        # cancel_run returns False when the doc is already terminal
        # — the importer already exited (succeeded / failed / a prior
        # cancel acknowledged). Surface that so the banner stops
        # spinning the Cancel button.
        return {"ok": False, "reason": "already_terminal"}, 200

    AuditLogService.log_event(
        username,
        "import_cancel_requested",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"analytics_event=import.cancel.requested run_id={run_id}",
    )
    return {"ok": True, "status": "cancelling"}, 200


def _connector_display_name(connector_id: str) -> str:
    """Resolve a wizard connector_id to its human label for the pill.

    Read from the connector catalog so the label stays in sync with
    the step-4 tile grid. Falls back to the id verbatim when the
    catalog lookup raises (e.g. a connector that was removed between
    the progress write and the pill read) — degraded but readable.
    """
    try:
        from asset_management.web.services.connector_catalog_service import CONNECTORS

        for tile in CONNECTORS:
            if tile.connector_id == connector_id:
                return tile.name
    except Exception as e:  # noqa: BLE001
        logging.warning("connector_display_name lookup failed: %s", e)
    return connector_id


@analytics_bp.route("/onboarding/step7/building")
def onboarding_step7_building_page():
    """Render the post-finish "Building your dashboard…" screen.

    Query-string params (set by the step-6 finish redirect):
      * ``connector_id`` — for the human label in the headline copy.
      * ``asset_count`` — "Connecting <N> ad accounts" sub-line.
      * ``next`` — relative URL to redirect to once a poll returns
                   days_fetched ≥ 1 OR the 15s ceiling fires. Falls
                   back to ``/dashboard`` if missing or non-relative.

    The JS-side poll loop lives in the template. SSR pins the DOM
    hooks (``id="step7-building-poll"``, ``data-next``,
    ``data-poll-url``, ``data-ceiling-ms``) the JS reads.

    org-owner entry sees an inline "Invite teammates now or
    later?" prompt above the spinner. When the prompt renders, the JS
    poller is suppressed (the user owns the redirect via the two CTAs)
    and an ``onboarding_step7_invite_prompt_shown`` audit event fires.
    "Invite now" appends ``?invite=open`` to ``next_url`` so a future
    invite-modal ticket can consume the flag; "Later" routes to
    ``next_url`` as-is. Both choices land on ``/dashboard`` per the AC.
    """

    username = session.get("username", "")
    if not username:
        return redirect(url_for("analytics.login_page"))

    connector_id = (request.args.get("connector_id") or "").strip()
    asset_count_raw = (request.args.get("asset_count") or "").strip()
    try:
        asset_count = max(0, int(asset_count_raw)) if asset_count_raw else 0
    except ValueError:
        asset_count = 0

    next_url = _safe_redirect_url(request.args.get("next"), default="/dashboard")
    connector_name = _connector_display_name(connector_id) if connector_id else "your dashboard"

    show_invite_prompt, account_type = _resolve_step7_invite_prompt(username)
    invite_now_url = _append_invite_open_param(next_url) if show_invite_prompt else next_url
    invite_later_url = next_url

    # follow-up — deferred step-7 soft block. Email
    # verification fires async at signup; nothing forces the user to
    # click the link. Surfacing a non-blocking 'verify your email'
    # banner on step 7 finally consumes the ``is_email_verified``
    # reader (the read-side helper that was shipped but never wired into
    # the wizard). Soft block by design: the user can still click through
    # to /dashboard via the "I'll verify later" CTA or the existing
    # invite-prompt CTAs. Anti-spam-light, not a hard gate.
    show_email_verify_prompt = False
    try:
        from asset_management.web.services.onboarding_email_verification_service import (
            OnboardingEmailVerificationService,
        )

        show_email_verify_prompt = not OnboardingEmailVerificationService.is_email_verified(username)
    except Exception as e:  # noqa: BLE001 — never block the render on an ES blip
        logging.warning("step-7 email-verify lookup failed for %r: %s", username, e)
        show_email_verify_prompt = False  # fail-open so a transient blip doesn't surface a misleading nag

    if show_invite_prompt:
        # Fire once per render so marketing's funnel can count how many
        # org-owner sessions actually saw the prompt vs. dropped off
        # earlier in the wizard. Literal ``analytics_event`` string
        # matches the wizard.stepN.<event> convention /
        # so a single Kibana dashboard captures the whole funnel.
        AuditLogService.log_event(
            username,
            "onboarding_step7_invite_prompt_shown",
            True,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=("analytics_event=wizard.step7.invite_prompt_shown " f"account_type={account_type}"),
        )

    # canonical step-7 entered + completed. Step-7 is the
    # terminal building screen — the user has already finished the
    # wizard at step-6 (or the step-4 tokenless finalize). Both events
    # fire on the same render: ``entered`` for the funnel "saw the
    # building screen" slice; ``completed`` because step-7 is the end
    # of the wizard funnel from the user's perspective even though the
    # backfill is still running (the JS poller redirects to /dashboard
    # once the data lands; there's no further user-input boundary).
    try:
        from asset_management.web.services.wizard_session_service import (
            WizardSessionService,
        )

        _s7_active = WizardSessionService.get_active(username, "onboarding")
        _s7_ctx = WizardAnalyticsService.build_context(
            username=username,
            wizard_session=_s7_active,
            account_type=account_type,
            connector_override=connector_id,
        )
        WizardAnalyticsService.emit(
            step="step-7",
            action=ACTION_ENTERED,
            context=_s7_ctx,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"asset_count": asset_count},
        )
        WizardAnalyticsService.emit(
            step="step-7",
            action=ACTION_COMPLETED,
            context=_s7_ctx,
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
            extra={"asset_count": asset_count, "show_invite_prompt": show_invite_prompt},
        )
    except Exception as e:  # noqa: BLE001 — emitter must never block the building-screen render
        logging.warning("step-7 emit failed for %r: %s", username, e)

    return render_template(
        "onboarding_step7_building.html",
        username=username,
        connector_id=connector_id,
        connector_name=connector_name,
        asset_count=asset_count,
        next_url=next_url,
        show_invite_prompt=show_invite_prompt,
        invite_now_url=invite_now_url,
        invite_later_url=invite_later_url,
        show_email_verify_prompt=show_email_verify_prompt,
        user_email=session.get("email", ""),
    )


# kept module-scoped (not nested) so the unit test can
# patch them and so a future ticket that grows the variant matrix
# (e.g. agency entry-point too) only touches one function.
def _resolve_step7_invite_prompt(username: str) -> tuple[bool, str]:
    """Return ``(show_prompt, account_type)`` for the step-7 render.

    Reads ``UserProfileService.get_account_type`` — the canonical reader
    the wizard branching and the checklist variant
    already consume, so the three onboarding surfaces stay in lock-step.
    Fails CLOSED on exception by returning the default account type
    (org_owner) — a transient ES blip surfaces the prompt rather than
    silently hiding it from someone who would otherwise see it. Mirrors
    the safe-direction posture from ``_is_individual`` in
    ``dashboard_checklist_service.py``.
    """
    from asset_management.web.services.user_profile_service import (
        ACCOUNT_TYPE_ORG_OWNER,
        DEFAULT_ACCOUNT_TYPE,
        UserProfileService,
    )

    try:
        account_type = UserProfileService.get_account_type(username)
    except Exception as e:  # noqa: BLE001 — defensive boundary
        logging.warning(
            "step-7 account_type lookup raised for %r: %s",
            username,
            e,
        )
        account_type = DEFAULT_ACCOUNT_TYPE
    return account_type == ACCOUNT_TYPE_ORG_OWNER, account_type


def _append_invite_open_param(url: str) -> str:
    """Append ``invite=open`` to ``url`` preserving any existing query.

    Uses string-level concatenation rather than urllib.parse because
    ``url`` is always the output of ``_safe_redirect_url`` (relative,
    no fragment, well-formed) — the simpler form keeps the audit-trail
    in the template easy to inspect.
    """
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}invite=open"


# ============================================
# ONBOARDING EMAIL VERIFICATION
# ============================================
# Async email verification fires from the signup POST handler; this
# is the landing route for the link in that email. Public (the user
# is following a link from their inbox, not necessarily authenticated
# in this browser tab) and gated only by the JWT signature + expiry.
# Sets user.metadata.email_verified=true; future step-7 handoff will
# consult that flag for the soft block. No CSRF because GET-only and
# the JWT is the unforgeable token.


@analytics_bp.route("/onboarding/verify/<token>")
def onboarding_verify_email(token):
    """Land the user on a success/expired page and stamp the metadata flag.

    The user clicks the link in the verification email, we
    decode the JWT, look up the user, set ``metadata.email_verified=true``,
    and render a friendly confirmation page. Failures (expired,
    tampered, unknown user) surface a reason and a "request a new
    verification email" affordance.

    The route is intentionally public — a user might verify on a
    device that isn't logged in, and we don't want to force them
    through /login just to confirm an email they own.

    Verification happens on the GET (auto-verify on link click). A
    prefetch-safe "Confirm my email" button was tried (2026-05-29) to
    stop mail-scanner prefetch from consuming the link, but it was
    reverted — the extra click was worse UX than the prefetch problem.
    """

    from asset_management.web.services.onboarding_email_verification_service import (
        OnboardingEmailVerificationService,
    )

    decoded = OnboardingEmailVerificationService.verify_token(token, current_app.secret_key)
    if not decoded.get("valid"):
        return (
            render_template(
                "onboarding_verify_email.html",
                status="invalid",
                message=decoded.get("error", "Verification link is invalid."),
                username="",
            ),
            400,
        )

    username = decoded["username"]
    stamp = OnboardingEmailVerificationService.mark_email_verified(username)

    if not stamp.get("success"):
        AuditLogService.log_event(
            username,
            "onboarding_email_verify_failed",
            False,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", ""),
            detail=stamp.get("error", "unknown"),
        )
        return (
            render_template(
                "onboarding_verify_email.html",
                status="error",
                message=stamp.get("error", "Could not record your verification."),
                username=username,
            ),
            500,
        )

    AuditLogService.log_event(
        username,
        "onboarding_email_verified",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail="already_verified" if stamp.get("already_verified") else "first_verification",
    )

    # the email is verified now, so a domain-match join parked at
    # the join prompt can be applied (the service re-matches the verified
    # address to the same org first).
    _apply_pending_domain_join(username)

    # Always render the self-contained success page; never redirect.
    # Email clients open the verify link in a new tab, so the verifying
    # browser context is NOT the tab that holds the active wizard
    # session — the user still has that original tab open (that's where
    # they triggered the email). That tab (step 2) polls
    # /onboarding/verify-email-step/status every 5s, sees the flag flip
    # on page-load, and enables Continue without any explicit user
    # action. So this page only needs to say "you can close this tab and
    # go back". No "continue setup" link: it added nothing (the user
    # already has the wizard tab open) and, worse, routed a user who had
    # finished onboarding back onto step 2.
    return render_template(
        "onboarding_verify_email.html",
        status="already_verified" if stamp.get("already_verified") else "verified",
        message="",
        username=username,
    )


# ============================================
# ONBOARDING WIZARD STEP 1 — GOOGLE SSO
# ============================================
# Two GET routes: /onboarding/google/start kicks off the OAuth2 flow,
# /onboarding/google/callback handles Google's redirect-back. Both are
# gated by a configured GOOGLE_SSO client (the feature-flag half of
# the gate was retired). On
# success we either auto-login (existing email) and land on /dashboard,
# or provision a new user and land on /onboarding/step-4 — mirroring
# the email+password contract.


def _google_sso_available() -> bool:
    """True iff the Google OAuth client is configured.

    A misconfigured client hides the button (so a half-deployed
    environment doesn't show a button that 500s). The route handlers
    also return 404 when this is False, so the button + route stay in
    sync without a duplicated check. The wizard feature-flag half of
    this gate was retired in config is the only condition.
    """
    return config.GOOGLE_SSO_ENABLED


@analytics_bp.route("/onboarding/google/start")
def onboarding_google_start():
    """Kick off the Google SSO flow: generate state, store in session,
    redirect to Google's authorize endpoint.
    """
    if not _google_sso_available():
        abort(404)

    # Same ?origin=login contract as the Microsoft button:
    # marks a flow started from the sign-in page rather than the signup
    # wizard, and routes failures back there. Compared against a literal,
    # so an arbitrary ?origin= value cannot steer the redirect.
    origin_is_login = request.args.get("origin") == "login"

    # follow-up (2026-05-20) — refuse to launch SSO sign-up
    # while a session is already authenticated. See helper docstring.
    if _resp := _redirect_if_already_authenticated_for_signup(
        return_to=url_for("analytics.login_page") if origin_is_login else None
    ):
        return _resp

    from asset_management.web.services.google_sso_service import GoogleSsoService

    state = GoogleSsoService.generate_state_token()
    session["google_sso_state"] = state
    session["google_sso_state_ts"] = time.time()
    # Remembered across the Google round-trip: the callback has no query
    # params of ours, only Google's code/state.
    session["google_sso_origin"] = "login" if origin_is_login else "signup"

    authorize_url = GoogleSsoService.build_authorize_url(
        client_id=config.GOOGLE_SSO_CLIENT_ID,
        redirect_uri=config.GOOGLE_SSO_REDIRECT_URI,
        state=state,
    )
    logging.info("AUDIT: action=google_sso_start ip=%s", request.remote_addr)
    return redirect(authorize_url)


@analytics_bp.route("/onboarding/google/callback")
def onboarding_google_callback():
    """Handle Google's OAuth2 redirect-back.

    Validates state, exchanges the auth code for verified userinfo,
    then either signs in an existing user (email match → /dashboard)
    or provisions a fresh one (→ /onboarding/step-4). The state token
    is consumed on first use (popped from the session) regardless of
    outcome so a leaked callback URL is not replayable.
    """
    if not _google_sso_available():
        abort(404)

    from asset_management.web.services.google_sso_service import GoogleSsoService

    state_expected = session.pop("google_sso_state", None)
    session.pop("google_sso_state_ts", None)
    # Where failures land: back to the sign-in page when the flow started
    # there (same contract as the Microsoft callback), else the signup page.
    sso_origin = session.pop("google_sso_origin", "signup")
    failure_redirect = url_for("analytics.login_page") if sso_origin == "login" else url_for("analytics.onboarding_start_page")

    error_qp = request.args.get("error")
    if error_qp:
        logging.warning(
            "AUDIT: action=google_sso_callback_error google_error=%s ip=%s",
            error_qp,
            request.remote_addr,
        )
        return redirect(failure_redirect)

    code = request.args.get("code", "")
    state_received = request.args.get("state", "")

    exchange = GoogleSsoService.exchange_code_for_userinfo(
        code=code,
        state_received=state_received,
        state_expected=state_expected or "",
        client_id=config.GOOGLE_SSO_CLIENT_ID,
        client_secret=config.GOOGLE_SSO_CLIENT_SECRET,
        redirect_uri=config.GOOGLE_SSO_REDIRECT_URI,
    )
    if not exchange.get("success"):
        logging.warning(
            "AUDIT: action=google_sso_callback_failed code=%s ip=%s",
            exchange.get("code"),
            request.remote_addr,
        )
        return redirect(failure_redirect)

    # 2026-08-06 open-registration incident: existing users sign in as
    # before; provisioning a NEW account requires the self-signup flag or
    # a pending invite in the session (invited visitors keep working).
    # a personal signup link also allows a NEW account, but only
    # while its grant is pending and only for the email the provider
    # verified. An existing user signing in never touches the grant.
    signup_grant = session.get("signup_grant") or None
    grant_allows = False
    if signup_grant:
        from asset_management.web.services.signup_grant_service import SignupGrantService

        grant_allows = SignupGrantService.is_usable_for(signup_grant, exchange.get("email") or "")
    allow_provision = config.SELF_SIGNUP_ENABLED or bool(session.get("pending_invite")) or grant_allows
    result = GoogleSsoService.sign_in_or_provision(exchange, allow_provision=allow_provision)
    if result.get("success") and result.get("user_action") == "signup" and grant_allows:
        _claim_grant_after_sso(signup_grant, result, auth_method="google_sso")
    if not result.get("success"):
        logging.warning(
            "AUDIT: action=google_sso_provision_failed ip=%s errors=%s",
            request.remote_addr,
            result.get("errors"),
        )
        return redirect(failure_redirect)

    # Build the authenticated session shape used by both /login and
    # /onboarding/start so downstream code sees a uniform session.
    session.pop("logged_out", None)
    session["authenticated"] = True
    session["username"] = result["username"]
    session["roles"] = result.get("roles", [])
    session["is_admin"] = "superuser" in result.get("roles", []) or config.REQUIRED_ROLE in result.get("roles", [])
    session["full_name"] = result.get("full_name") or result["username"]
    session["email"] = result.get("email", "")
    session["auth_timestamp"] = time.time()
    session["last_activity"] = time.time()
    session["auth_method"] = "google_sso"
    session.permanent = True

    AuditLogService.log_event(
        result["username"],
        "login",
        True,
        auth_method="google_sso",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    # New signup → SKIP the verify-email step (Google validated the
    # email at OAuth handoff — enforced since the exchange
    # rejects userinfo whose email_verified is not true, so this path
    # only ever stamps verified addresses) but still must set up MFA.
    # Land on the
    # mfa-setup step (logical step 3 of 8 Phase 2);
    # Phase 1 stamps metadata.email_verified=True so the
    # LoginGateService treats this user as verified.
    #
    # Existing user → straight to the dashboard, matching the ticket
    # AC "Existing Google-SSO user signs in directly to dashboard
    # (skipping wizard)".
    if result.get("user_action") == "signup":
        try:
            from asset_management.web.services.onboarding_email_verification_service import (
                OnboardingEmailVerificationService,
            )

            OnboardingEmailVerificationService.mark_email_verified(result["username"])
        except Exception as e:  # noqa: BLE001 — fail-soft; gate will surface it
            logging.warning(
                "Phase 1: mark_email_verified failed for Google SSO signup %r: %s",
                result["username"],
                e,
            )
        return redirect(url_for("analytics.onboarding_mfa_setup_step_page"))

    # Existing user signing in through Google — a login entry point in its
    # own right, so it runs the gate like any other. The
    # signup branch above returns before this: it is already walking into the
    # wizard, and gating it would redirect a user away from the step they are on.
    if _gate_redirect := gate_redirect_after_auth(
        result["username"],
        result.get("roles") or [],
        auth_method="google_sso",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    ):
        return _gate_redirect
    return redirect(url_for("analytics.dashboard_page"))


# ============================================
# ONBOARDING WIZARD STEP 1 — MICROSOFT SSO
# ============================================
# Mirrors the Google SSO pair. Two GET routes:
# /onboarding/microsoft/start kicks off the OAuth2 flow,
# /onboarding/microsoft/callback handles Microsoft's redirect-back.
# Both are gated by a configured Microsoft SSO client (the feature-flag
# half of the gate was retired). On success we either
# auto-login (existing email) and
# land on /dashboard, or provision a new user and land on
# /onboarding/step-4 — matching the email+password contract from
# and the Google contract.


def _microsoft_sso_available() -> bool:
    """True iff the Microsoft SSO client is configured.

    A misconfigured client hides the button — client
    misconfigured → button hidden (so a half-deployed environment
    doesn't show a button that 500s). The route handlers also
    return 404 when this is False, so the button + route stay in
    sync without a duplicated check. The wizard feature-flag half of
    this gate was retired in config is the only condition.
    """
    return config.MICROSOFT_SSO_ENABLED


@analytics_bp.route("/onboarding/microsoft/start")
def onboarding_microsoft_start():
    """Kick off the Microsoft SSO flow: generate state, store in
    session, redirect to Microsoft's authorize endpoint.
    """
    if not _microsoft_sso_available():
        abort(404)

    # ?origin=login marks a flow started from the sign-in
    # page rather than the signup wizard. It controls two things: the
    # post-logout landing path in the already-authenticated guard
    # below, and the post-callback destination for a NEW account (see
    # the callback's signup branch). Compared against a literal, so an
    # arbitrary ?origin= value cannot steer either redirect.
    origin_is_login = request.args.get("origin") == "login"

    # follow-up (2026-05-20) — refuse to launch SSO sign-up
    # while a session is already authenticated. See helper docstring.
    if _resp := _redirect_if_already_authenticated_for_signup(
        return_to=url_for("analytics.login_page") if origin_is_login else None
    ):
        return _resp

    from asset_management.web.services.microsoft_sso_service import MicrosoftSsoService

    state = MicrosoftSsoService.generate_state_token()
    session["microsoft_sso_state"] = state
    session["microsoft_sso_state_ts"] = time.time()
    # Remembered across the Microsoft round-trip: the callback has no
    # query params of ours, only Microsoft's code/state.
    session["microsoft_sso_origin"] = "login" if origin_is_login else "signup"

    authorize_url = MicrosoftSsoService.build_authorize_url(
        client_id=config.MICROSOFT_SSO_CLIENT_ID,
        redirect_uri=config.MICROSOFT_SSO_REDIRECT_URI,
        state=state,
        tenant=config.MICROSOFT_SSO_TENANT,
    )
    logging.info("AUDIT: action=microsoft_sso_start ip=%s", request.remote_addr)
    return redirect(authorize_url)


@analytics_bp.route("/onboarding/microsoft/callback")
def onboarding_microsoft_callback():
    """Handle Microsoft's OAuth2 redirect-back.

    Validates state, exchanges the auth code for verified userinfo,
    then either signs in an existing user (email match →
    /dashboard) or provisions a fresh one (→ /onboarding/step-4).
    The state token is consumed on first use (popped from the
    session) regardless of outcome so a leaked callback URL is not
    replayable.
    """
    if not _microsoft_sso_available():
        abort(404)

    from asset_management.web.services.microsoft_sso_service import MicrosoftSsoService

    state_expected = session.pop("microsoft_sso_state", None)
    session.pop("microsoft_sso_state_ts", None)
    # where a FAILED handshake sends the user. A flow started
    # from the sign-in page must fall back to /login, not the signup
    # wizard — otherwise a user whose SSO attempt fails is silently
    # moved to a "create an account" form. Popped alongside the state so
    # a stale marker cannot leak into an unrelated later flow.
    failure_redirect = (
        url_for("analytics.login_page")
        if session.pop("microsoft_sso_origin", "signup") == "login"
        else url_for("analytics.onboarding_start_page")
    )

    error_qp = request.args.get("error")
    if error_qp:
        logging.warning(
            "AUDIT: action=microsoft_sso_callback_error microsoft_error=%s ip=%s",
            error_qp,
            request.remote_addr,
        )
        return redirect(failure_redirect)

    code = request.args.get("code", "")
    state_received = request.args.get("state", "")

    exchange = MicrosoftSsoService.exchange_code_for_userinfo(
        code=code,
        state_received=state_received,
        state_expected=state_expected or "",
        client_id=config.MICROSOFT_SSO_CLIENT_ID,
        client_secret=config.MICROSOFT_SSO_CLIENT_SECRET,
        redirect_uri=config.MICROSOFT_SSO_REDIRECT_URI,
        tenant=config.MICROSOFT_SSO_TENANT,
    )
    if not exchange.get("success"):
        logging.warning(
            "AUDIT: action=microsoft_sso_callback_failed code=%s ip=%s",
            exchange.get("code"),
            request.remote_addr,
        )
        return redirect(failure_redirect)

    # 2026-08-06: same self-signup gate as the Google callback.
    # a personal signup link also allows a NEW account, but only
    # while its grant is pending and only for the email the provider
    # verified. An existing user signing in never touches the grant.
    signup_grant = session.get("signup_grant") or None
    grant_allows = False
    if signup_grant:
        from asset_management.web.services.signup_grant_service import SignupGrantService

        grant_allows = SignupGrantService.is_usable_for(signup_grant, exchange.get("email") or "")
    allow_provision = config.SELF_SIGNUP_ENABLED or bool(session.get("pending_invite")) or grant_allows
    result = MicrosoftSsoService.sign_in_or_provision(exchange, allow_provision=allow_provision)
    if result.get("success") and result.get("user_action") == "signup" and grant_allows:
        _claim_grant_after_sso(signup_grant, result, auth_method="microsoft_sso")
    if not result.get("success"):
        logging.warning(
            "AUDIT: action=microsoft_sso_provision_failed ip=%s errors=%s",
            request.remote_addr,
            result.get("errors"),
        )
        return redirect(failure_redirect)

    # Build the authenticated session shape used by both /login and
    # /onboarding/start so downstream code sees a uniform session.
    session.pop("logged_out", None)
    session["authenticated"] = True
    session["username"] = result["username"]
    session["roles"] = result.get("roles", [])
    session["is_admin"] = "superuser" in result.get("roles", []) or config.REQUIRED_ROLE in result.get("roles", [])
    session["full_name"] = result.get("full_name") or result["username"]
    session["email"] = result.get("email", "")
    session["auth_timestamp"] = time.time()
    session["last_activity"] = time.time()
    session["auth_method"] = "microsoft_sso"
    session.permanent = True

    AuditLogService.log_event(
        result["username"],
        "login",
        True,
        auth_method="microsoft_sso",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    # New signup → SKIP the verify-email step (Microsoft validated the
    # email at OAuth handoff) but still must set up MFA. Land on the
    # mfa-setup step (logical step 3 of 8 Phase 2).
    # Same rationale as the Google SSO branch above. Existing user →
    # straight to the dashboard.
    if result.get("user_action") == "signup":
        try:
            from asset_management.web.services.onboarding_email_verification_service import (
                OnboardingEmailVerificationService,
            )

            OnboardingEmailVerificationService.mark_email_verified(result["username"])
        except Exception as e:  # noqa: BLE001 — fail-soft
            logging.warning(
                "Phase 1: mark_email_verified failed for Microsoft SSO signup %r: %s",
                result["username"],
                e,
            )
        return redirect(url_for("analytics.onboarding_mfa_setup_step_page"))

    # Existing user signing in through Microsoft — a login entry point in its
    # own right, so it runs the gate like any other. The
    # signup branch above returns before this: it is already walking into the
    # wizard, and gating it would redirect a user away from the step they are on.
    if _gate_redirect := gate_redirect_after_auth(
        result["username"],
        result.get("roles") or [],
        auth_method="microsoft_sso",
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    ):
        return _gate_redirect
    return redirect(url_for("analytics.dashboard_page"))
