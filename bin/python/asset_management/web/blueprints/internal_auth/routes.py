"""
Internal auth-backend routes for nginx auth_request.

GET /api/internal/kibana-gate is the auth backend that nginx's
auth_request module calls on every /kibana request. nginx forwards the
user's cookies in the subrequest, this route reads the DEAM session, and
the response status code is the allow/deny signal:

  204 No Content   -> nginx proxies the original request to kib01:5601
  401 Unauthorized -> nginx error_page redirects to /assets/login?next=/kibana
  403 Forbidden    -> nginx error_page renders /forbidden (DEAM 403 page)

F1 originally extended this gate to also require an
unexpired ``session['elevated_until']`` before returning 204, with a
companion ``POST /admin/elevate`` route to set the flag. After
discussing the UX cost (every Kibana entry becomes a form submission
for Kavanah-internal staff) a teammate decided 2026-05-14 to drop the gate
elevation requirement for now and revisit it under a separate ticket
once the team has agreed on a longer-term model (re-enable elevation
once a UI ships? segregate customer data into a separate ES instance?
accept the current frictionless model and rely on Layer A grants
alone for the privacy boundary?).

What stayed :
- Layer A is fully live — ``RoleFilterService`` denies bare superusers
  at the dashboard query layer via ``de_authz_data_grant`` lookups.
  The privacy boundary for DEAM dashboards is enforced; only the
  Kibana surface is frictionless.
- The ``POST /admin/elevate`` route and ``DataGrantService.has_active_grant``
  remain dormant in code as building blocks for the future decision.
  They cost nothing to keep and re-enabling the gate's elevation check
  is a one-line change.

Fail-closed contract on the gate: any unhandled exception MUST surface
as a 5xx so nginx auth_request denies access. We deliberately do NOT
catch-and-degrade — a broad except returning 204 would silently turn an
outage into allow-by-default.
"""

import logging
import time

from flask import jsonify, render_template, request, session

from . import internal_auth_bp

# the gate hands nginx a per-user Kibana API key on the 204. Same
# dual-path import the analytics blueprint uses (the app is importable both
# as ``web`` and as ``asset_management.web``).
try:
    from web.services.kibana_gate_service import HEADER_NAME as _KGATE_HEADER
    from web.services.kibana_gate_service import KibanaGateService
except ImportError:  # pragma: no cover — alternate import root
    from asset_management.web.services.kibana_gate_service import HEADER_NAME as _KGATE_HEADER
    from asset_management.web.services.kibana_gate_service import KibanaGateService


@internal_auth_bp.route("/api/internal/kibana-gate", methods=["GET"])
def kibana_gate():
    """nginx auth_request backend for /kibana.

     contract (restored 2026-05-14 reconsideration):
    204 for authenticated superuser; 401 for anonymous; 403 for
    authenticated non-superuser. No additional elevation check — the
     elevation requirement was dropped pending a team decision
    on the longer-term Kibana-access model.

    No body on any path — nginx's auth_request module discards the body
    and only consumes the status code (and,, one response
    header).

    on 204 the gate ALSO returns ``X-Kibana-Authorization: ApiKey
    <key>`` — a per-user Elasticsearch API key minted via ``run_as`` for the
    session user (see ``services/kibana_gate_service.py``). nginx copies it
    into the upstream ``Authorization`` header so Kibana authenticates the
    request itself and never shows its native login form. Three invariants:

      * The authz decisions above run FIRST and unchanged — a 401/403 never
        carries the header and never triggers a mint. The fail-closed
        contract on those checks is untouched (KIBANA-004 still pins it).
      * The mint is best-effort by design: ``authorization_header_for``
        never raises. When the feature is off (env unset) or a mint fails,
        the response is the bare 204 it always was and Kibana falls back to
        its own login — a failure can withhold convenience, not grant access.
      * This route never writes to ``session``: auth_request discards the
        subrequest's Set-Cookie, so nothing written here would survive.
    """
    if not session.get("authenticated"):
        return "", 401
    # verify_auth skips /api/internal/* by design, so
    # the idle/absolute timers and the deleted-user check
    # it enforces everywhere else were never applied here. Reuse its
    # decision. 401, not 403: an expired or revoked session is not an
    # authenticated one, and nginx's 401 handler is the login redirect.
    # Relative import resolves to the same package root the app runs
    # under (``web`` or ``asset_management.web``); it is late so this
    # blueprint stays cheap to import and free of an import cycle.
    from ... import auth as auth_module

    denial = auth_module.session_denial_reason()
    if denial is not None:
        logging.info("kibana-gate: 401 user=%r reason=%s", session.get("username"), denial)
        return "", 401
    # verify_auth also skips this surface (, same as
    # session_denial_reason above), so the role-refresh check it runs on
    # every ordinary request never reaches this route either. Without this,
    # a superuser demoted while their Kibana tab stayed open would keep the
    # 204 (and a fresh gate key) for up to PERMANENT_SESSION_LIFETIME
    # instead of losing Kibana access within the same 30 s bound every
    # other surface gets. Read-only fail-soft: a truthy username re-checks
    # ES via the same cached _user_account_record session_denial_reason
    # just paid for; an empty one is a no-op (the 401 above already covers
    # "no session").
    _gate_username = session.get("username") or ""
    if _gate_username:
        auth_module.refresh_session_roles(_gate_username)
    if "superuser" not in session.get("roles", []):
        return "", 403

    headers = {}
    if KibanaGateService.enabled():
        auth_value = KibanaGateService.authorization_header_for(
            session.get("username"),
            ip_address=request.remote_addr or "",
        )
        if auth_value:
            headers[_KGATE_HEADER] = auth_value
        else:
            # The service already error-logs + audits the cause (rate-limited
            # per user); this fires on every one of a page load's dozens of
            # requests, so keep it at debug.
            logging.debug(
                "kibana-gate: no API key for user=%s — Kibana will show its own login",
                session.get("username"),
            )
    return "", 204, headers


@internal_auth_bp.route("/admin/elevate", methods=["POST"])
def admin_elevate():
    """/ F1: elevate the current session for Kibana access.

    Required JSON body: ``{"scope_type": "org"|"team", "scope_esid": "<esid>", "reason": "..."}``.

    Flow:
      1. Reject if not authenticated (the verify_auth handler in
         auth.py already enforces this for everything outside
         ``/api/internal/*``, but check again as defense-in-depth).
      2. Reject if not a superuser — Layer B (elevation) is
         superuser-only; non-superusers don't need to elevate because
         their grants come from their role memberships at signup.
      3. Reject if the user does not hold a persistent
         ``de_authz_data_grant`` for the requested scope. This refuses
         the self-grant-by-elevation bypass: you cannot elevate to view
         a tenant you do not already hold a Layer A grant for.
      4. Set ``session['elevated_until'] = time.time() +
         config.ELEVATION_LIFETIME``.
      5. Audit-log ``permission.elevation.start`` with target_user=self,
         detail=scope+reason.

    CSRF is auto-enforced by ``check_csrf_for_state_change`` in
    auth.py:115; the route does not need to validate the token
    explicitly.
    """
    # Defense-in-depth: verify_auth normally gates this, but
    # /api/internal/* is exempt from verify_auth (because the gate
    # endpoint must do its own session check), and /admin/elevate
    # lives in the same blueprint. Re-check authentication here.
    if not session.get("authenticated"):
        return jsonify({"error": "unauthenticated"}), 401
    if "superuser" not in session.get("roles", []):
        return jsonify({"error": "elevation requires superuser"}), 403

    body = request.get_json(silent=True) or {}
    scope_type = body.get("scope_type")
    scope_esid = body.get("scope_esid")
    reason = body.get("reason", "").strip()

    if scope_type not in ("org", "team"):
        return jsonify({"error": "scope_type must be 'org' or 'team'"}), 400
    if not scope_esid:
        return jsonify({"error": "scope_esid is required"}), 400
    if not reason:
        return jsonify({"error": "reason is required (audit trail)"}), 400

    # Late import keeps the blueprint import cheap and avoids a circular
    # dep with config / audit log if those change.
    try:
        from web.config import config
        from web.services.audit_log_service import AuditLogService
        from web.services.data_grant_service import DataGrantService
    except ImportError:
        from asset_management.web.config import config
        from asset_management.web.services.audit_log_service import AuditLogService
        from asset_management.web.services.data_grant_service import DataGrantService

    username = session.get("username", "")
    if not DataGrantService.has_active_grant(username=username, scope_type=scope_type, scope_esid=scope_esid):
        # No grant = no elevation. This is the load-bearing rule that
        # makes /admin/elevate auditable: every elevation traces back to
        # a pre-existing Layer A grant.
        return jsonify({"error": "no active grant for that scope"}), 403

    lifetime = config.ELEVATION_LIFETIME
    elevated_until = time.time() + lifetime
    session["elevated_until"] = elevated_until
    session["elevated_scope_type"] = scope_type
    session["elevated_scope_esid"] = scope_esid

    AuditLogService.log_event(
        username,
        "permission.elevation.start",
        True,
        target_user=username,
        detail=f"scope={scope_type}:{scope_esid}, reason={reason}, expires_at={int(elevated_until)}",
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
    )

    return jsonify(
        {
            "elevated_until": int(elevated_until),
            "scope_type": scope_type,
            "scope_esid": scope_esid,
            "lifetime_seconds": lifetime,
        }
    )


@internal_auth_bp.route("/forbidden", methods=["GET"])
def forbidden_page():
    """Render the DEAM "not authorized" page used by nginx error_page
    handlers.

    The route name is intentionally generic — any future nginx-gated
    surface that wants the same "you're logged in but lack the role"
    UX can redirect here too. The `next` query param surfaces in the
    message so a user landing here from /kibana sees "you need superuser
    to view Kibana" rather than a context-free "Access Denied".

    Returns 200, NOT 403. Flask's @app.errorhandler(403) at
    app.py:7073 catches returned-status 403 responses for authenticated
    users and redirects them to /dashboard via _redirect_to_allowed_page
    — which would defeat the purpose of this page. The status code on
    the *response* is unrelated to the access decision (nginx already
    made that); the page itself is just an explanation render.
    """
    next_path = request.args.get("next", "")
    if next_path == "/kibana":
        message = (
            "Your account needs the 'superuser' role to view Kibana. "
            "Contact an administrator if you believe this is in error."
        )
    else:
        message = (
            "Your account does not have permission to view this page. "
            "Contact an administrator if you believe this is in error."
        )
    return render_template(
        "error.html",
        error_code=403,
        error_title="Access Denied",
        error_message=message,
    )
