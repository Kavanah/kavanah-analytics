"""
Public Report Blueprint —

Serves published Impact Reports at ``/public/report/<token>`` without requiring
login. Data queries are enforced to the dashboard's stored org/team scope; the
client cannot override it.

This blueprint bypasses ``verify_auth`` / CSRF — see ``asset_management.web.auth``
for the path-based exemption. Every handler must therefore treat every request
as fully untrusted and rely only on server-side state.
"""

import hashlib
import logging
from flask import current_app, jsonify, make_response, redirect, render_template, request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException

from . import public_report_bp

from lib.es_helpers import scoped_index_pattern, team_data_index_names

try:
    from lib.es_helpers import get_elasticsearch_client
    from web.services.es_query_builder import build_query
    from web.services.echarts_transformer import transform
    from web.services.login_lockout_service import LoginLockoutService
    from web.services.role_filter_service import set_public_publish_allowed_indices
except ImportError:
    from lib.es_helpers import get_elasticsearch_client
    from services.es_query_builder import build_query
    from services.echarts_transformer import transform
    from services.login_lockout_service import LoginLockoutService
    from services.role_filter_service import set_public_publish_allowed_indices

# Use a relative import for the dashboard_builder helpers so pytest
# mock-patches applied to the canonical module path actually reach the
# functions we call here. sys.path has *two* entries that can reach the
# dashboard_builder package (``bin/python`` and ``bin/python/.../web``),
# which yields two distinct module instances if imported via absolute paths.
from ..dashboard_builder.routes import (  # noqa: E402
    _QUERY_BATCH_MAX_ITEMS,
    _allowed_data_views_for_publish,
    _check_publish_passcode,
    _es_doc_to_dashboard_state,
    _forced_publish_filters,
    _lookup_dashboard_by_token,
    _publish_scope_team_esids,
    _strip_org_team_filters,
)
from ..dashboard_builder import canvas_theme  # noqa: E402
from ..dashboard_builder import routes as _db_routes  # noqa: E402


# ── optional viewer passcode ───────────────────────────────────────
#
# A publish entry may carry ``publish_passcode_hash`` (scrypt, set in the
# publish dialog). When it does, every public surface for that token — the
# viewer page AND each ``/public/api/*`` data route — requires proof that the
# viewer entered the passcode. The proof is a signed, expiring cookie scoped to
# ``/public/`` whose payload names the token fingerprint and a digest of the
# hash in force, so changing or removing the passcode invalidates every
# outstanding unlock, and one report's unlock never opens another. The cookie
# never contains the token or the passcode.
#
# Guessing is throttled through the shared lockout store, keyed by
# the token fingerprint (as the "username") and the requesting address —
# the same thresholds and audit events as the login form.

_UNLOCK_COOKIE_SALT = "public-report-unlock"
_UNLOCK_TTL_SECONDS = 12 * 60 * 60
_UNLOCK_LOCKOUT_SURFACE = "public_report_passcode"


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:32]


def _hash_digest(passcode_hash: str) -> str:
    return hashlib.sha256((passcode_hash or "").encode("utf-8")).hexdigest()[:16]


def _unlock_cookie_name(token: str) -> str:
    return f"pr_unlock_{_token_fingerprint(token)[:16]}"


def _unlock_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt=_UNLOCK_COOKIE_SALT)


def _lockout_key(token: str) -> str:
    return f"pubrpt:{_token_fingerprint(token)}"


def _passcode_satisfied(token: str, source: dict) -> bool:
    """True when the entry has no passcode, or the request carries a valid unlock."""
    stored = (source or {}).get("publish_passcode_hash")
    if not stored:
        return True
    raw = request.cookies.get(_unlock_cookie_name(token))
    if not raw:
        return False
    try:
        payload = _unlock_serializer().loads(raw, max_age=_UNLOCK_TTL_SECONDS)
    except BadSignature:  # covers SignatureExpired too
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("f") == _token_fingerprint(token) and payload.get("h") == _hash_digest(stored)


def _passcode_required_response():
    """JSON 401 for the data routes — the viewer page handles the redirect."""
    return jsonify({"success": False, "error": "Passcode required", "passcode_required": True}), 401


def _map_detail(source: dict) -> str:
    """the publish entry's map-detail choice, read off the token-scoped
    source view (``publish_map_detail`` is in ``_PUBLISH_ENTRY_FIELDS`` so
    ``_scoped_source_view`` flattens it like the passcode hash). Legacy entries
    without the field read as ``city``."""
    return _db_routes._entry_map_detail(source)


def _empty_map_payload(map_detail: str) -> dict:
    return {"success": True, "cities": [], "regions": [], "countries": [], "map_detail": map_detail}


# ── fail closed when the publish scope is gone ─────────────────────
#
# Every public query is scoped by ``_forced_publish_filters``. When the
# dashboard's allowed team/org ESIDs no longer resolve (team deleted, scope
# cleared after publishing), that list is empty — and a query without it runs
# over the chart's wildcard data view for EVERY tenant. A link with no scope
# serves nothing: the viewer gets the not-found answer, never data.


def _scope_missing_response(token: str):
    """JSON 404 for a data route whose publish scope no longer resolves."""
    # Same truncated-token audit pattern as the data_view rejections below.
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
    logging.warning("AUDIT: public report refused — publish scope resolves to nothing token_prefix=%s", token[:8])
    return jsonify({"success": False, "error": "Invalid or revoked token"}), 404


def _pin_publish_scope(source: dict) -> frozenset:
    """resolve the publish to its teams' ESIDs and pin this request.

    Returns the ``{team_esid}__{source}`` allowlist (empty → the caller
    refuses). Pinning makes ``RoleFilterService.scoped_index_pattern`` — used
    by the shared map helpers — intersect with the publisher's own indices
    instead of passing an anonymous request through to the wildcard.
    """
    allowed = team_data_index_names(_publish_scope_team_esids(source))
    if allowed:
        set_public_publish_allowed_indices(allowed)
    return allowed


# person-level location below country. The Disciple.Tools contact
# place ("City, Country") and location grid id. A public query that names one
# of these is refused unless the publisher chose city-level map detail
# otherwise a canvas binding or a hand-built query on a
# ``country`` / ``hidden`` link would hand out what the map withholds.
_PUBLIC_SUB_COUNTRY_FIELDS = ("meta.contact_place", "meta.location_grid")


def _references_sub_country_field(value) -> bool:
    """True when any string inside ``value`` names a sub-country person field."""
    if isinstance(value, str):
        return any(value == f or value.startswith(f + ".") for f in _PUBLIC_SUB_COUNTRY_FIELDS)
    if isinstance(value, dict):
        return any(_references_sub_country_field(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_references_sub_country_field(v) for v in value)
    return False


def _render_passcode_page(token: str, source: dict, *, error: str | None = None, status: int = 401):
    resp = make_response(
        render_template(
            "public_report/passcode.html",
            token=token,
            title=source.get("title", "Impact Report"),
            error=error,
        ),
        status,
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@public_report_bp.route("/report/<token>/unlock", methods=["POST"])
def unlock_public_report(token: str):
    """Check the viewer's passcode; on success set the unlock cookie and return to the report."""
    _, source = _lookup_dashboard_by_token(token)
    if not source:
        return render_template("public_report/not_found.html"), 404
    if not source.get("publish_passcode_hash"):
        return redirect(f"/public/report/{token}", code=303)

    key = _lockout_key(token)
    ip = request.remote_addr
    if not LoginLockoutService.is_allowed(key, ip):
        return _render_passcode_page(token, source, error="Too many attempts. Please try again later.", status=429)

    passcode = (request.form.get("passcode") or "").strip()
    if not passcode or not _check_publish_passcode(source, passcode):
        LoginLockoutService.record_failure(key, ip, surface=_UNLOCK_LOCKOUT_SURFACE)
        return _render_passcode_page(token, source, error="That passcode is not correct.")

    LoginLockoutService.clear(key, ip)
    proof = _unlock_serializer().dumps({"f": _token_fingerprint(token), "h": _hash_digest(source["publish_passcode_hash"])})
    resp = redirect(f"/public/report/{token}", code=303)
    resp.set_cookie(
        _unlock_cookie_name(token),
        proof,
        max_age=_UNLOCK_TTL_SECONDS,
        path="/public/",
        secure=bool(current_app.config.get("SESSION_COOKIE_SECURE", True)),
        httponly=True,
        samesite="Lax",
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _publish_org_team_scope(source: dict) -> tuple[str, list[str]]:
    """Translate the dashboard's forced publish filters to (org, teams).

    The auth'd map endpoints take ``org`` (str) and ``teams`` (list) and
    pass them through ``_org_team_filters`` to build the ES filter. The
    publish path stores titles as snapshots (or falls back to the
    allowed_*_esids resolution); reuse ``_forced_publish_filters`` so the
    public map queries are scoped exactly the same way as the public
    report queries on ``/public/api/query``.
    """
    specs = _forced_publish_filters(source)
    org = ""
    teams: list[str] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        field = spec.get("field")
        value = spec.get("value")
        op = spec.get("op")
        if field == "team_title":
            if op == "match_phrase_any" and isinstance(value, list):
                teams.extend(v for v in value if v)
            elif value:
                teams.append(value)
        elif field == "organization_title":
            if op == "match_phrase_any" and isinstance(value, list) and value:
                # The auth'd map endpoints take a single org string; pick
                # the first when the publish snapshot allows multiple.
                # team scope (when present) is what actually constrains.
                org = value[0]
            elif value:
                org = value
    return org, teams


@public_report_bp.route("/report/<token>", methods=["GET"])
def view_public_report(token: str):
    """Render the public viewer for a published Impact Report or canvas page."""
    doc_id, source = _lookup_dashboard_by_token(token)
    if not source:
        return render_template("public_report/not_found.html"), 404
    # passcode-protected link without a valid unlock → ask for it.
    if not _passcode_satisfied(token, source):
        return _render_passcode_page(token, source)

    # canvas pages render through their own public viewer — the
    # shared canvas_view.js renderer in public-token mode. Same publish
    # entry drives both types, so the time-selector opt-in and the
    # token contract are identical.
    template = "public_report/canvas_viewer.html" if source.get("dashboard_type") == "canvas" else "public_report/viewer.html"
    return render_template(
        template,
        dashboard_id=doc_id,
        token=token,
        title=source.get("title", "Impact Report"),
        # publisher opts in to showing the time-range dropdown.
        # Default is False (hidden) so shared links ship with a fixed window.
        display_time_selector=bool(source.get("display_time_selector")),
        # theme tokens for the canvas viewer (viewer.html ignores it).
        canvas_theme_tokens=canvas_theme.tokens_json(),
    )


@public_report_bp.route("/api/dashboards/<token>", methods=["GET"])
def load_public_dashboard(token: str):
    """Return the dashboard config for a published token.

    Shape matches the authenticated ``/dashboard-builder/api/dashboards/<id>``
    response so the viewer page can reuse the same chart-loading code — minus
    any sensitive fields.
    """
    doc_id, source = _lookup_dashboard_by_token(token)
    if not source:
        return jsonify({"success": False, "error": "Invalid or revoked token"}), 404
    if not _passcode_satisfied(token, source):
        return _passcode_required_response()

    state = _es_doc_to_dashboard_state(doc_id, source)
    # Strip fields that are internal to the authenticated builder — the public
    # viewer has no concept of visibility modes or org/team selection.
    for key in ("visibility_mode", "allowed_org_esids", "allowed_team_esids"):
        state.pop(key, None)
    return jsonify(state)


@public_report_bp.route("/api/query", methods=["POST"])
def run_public_query():
    """Execute an ES query for a published Impact Report.

    Request body (JSON): the standard chart-query config plus a ``token`` field.

    Security invariants enforced here:
      1. Token must match a published dashboard.
      2. ``data_view`` must be one the dashboard legitimately queries
         (whitelist = union of stored chart data_views + platform template views).
      3. Any client-supplied ``organization_title`` / ``team_title`` filter is
         stripped before we run the query; we then inject the dashboard's stored
         org/team titles as forced ``match_phrase`` filters.
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        token = (body.pop("token", "") or "").strip()
        if not token:
            return jsonify({"success": False, "error": "Missing token"}), 400

        _, source = _lookup_dashboard_by_token(token)
        if not source:
            return jsonify({"success": False, "error": "Invalid or revoked token"}), 404
        if not _passcode_satisfied(token, source):
            return _passcode_required_response()

        config = body
        data_view = (config.get("data_view") or "").strip()
        if not data_view:
            return jsonify({"success": False, "error": "Missing data_view"}), 400

        allowed_dvs = _allowed_data_views_for_publish(source)
        if data_view not in allowed_dvs:
            # `token` is a publish token for a shareable public URL — not an
            # auth credential. Only the first 8 characters are logged, matching
            # the truncation pattern used elsewhere in audit logs.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.warning(
                "AUDIT: public query rejected — data_view=%s not in whitelist token_prefix=%s",
                data_view,
                token[:8],
            )
            return jsonify({"success": False, "error": "data_view not allowed for this report"}), 403

        if _map_detail(source) != "city" and _references_sub_country_field(config):
            return jsonify({"success": False, "error": "Field not available on this report"}), 403

        config = _strip_org_team_filters(config)
        forced = _forced_publish_filters(source)
        allowed = _pin_publish_scope(source)
        if not forced or not allowed:
            return _scope_missing_response(token)
        config = dict(config)
        config["filters"] = list(config.get("filters") or []) + forced

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        query_body = build_query(config)
        es_response = es.search(index=scoped_index_pattern(data_view, allowed), body=query_body)
        es_dict = es_response.body if hasattr(es_response, "body") else dict(es_response)
        return jsonify(transform(es_dict, config))
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to run public report query",
            log_detail="Error running public report query",
            exc=e,
        )


@public_report_bp.route("/api/query_batch", methods=["POST"])
def run_public_query_batch():
    """execute many chart configs in ONE request for a published page.

    Public mirror of ``/dashboard-builder/api/query_batch`` — a
    published canvas page resolves every live binding in a single batch,
    exactly as the authenticated canvas viewer does.

    Request:  ``{"token": <publish token>, "queries": [<chart config>, ...]}``
    Response: ``{"results": [...]}`` — aligned with the request array by index.

    Security invariants mirror ``run_public_query``, applied PER ITEM:
      1. One token check per batch (the scope is constant across items).
      2. Each item's ``data_view`` must be in the publish whitelist — for a
         canvas page that is the union of the live bindings' data views.
      3. Client-supplied org/team filters are stripped per item; the publish
         entry's forced scope filters are appended per item.
    Per-item ``dashboard_filters`` pass
    through to ``build_query`` — they can only AND-narrow within the forced
    scope, never widen it. A failed item yields a per-item error entry; it
    never sinks the batch.
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        token = (body.pop("token", "") or "").strip()
        if not token:
            return jsonify({"success": False, "error": "Missing token"}), 400

        _, source = _lookup_dashboard_by_token(token)
        if not source:
            return jsonify({"success": False, "error": "Invalid or revoked token"}), 404
        if not _passcode_satisfied(token, source):
            return _passcode_required_response()

        queries = body.get("queries")
        if not isinstance(queries, list) or not queries:
            return jsonify({"success": False, "error": "No queries provided"}), 400
        if len(queries) > _QUERY_BATCH_MAX_ITEMS:
            return (
                jsonify({"success": False, "error": f"Too many queries (max {_QUERY_BATCH_MAX_ITEMS})"}),
                400,
            )

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        allowed_dvs = _allowed_data_views_for_publish(source)
        forced = _forced_publish_filters(source)
        allowed = _pin_publish_scope(source)
        if not forced or not allowed:
            return _scope_missing_response(token)
        city_detail = _map_detail(source) == "city"

        results = []
        for item_config in queries:
            if not isinstance(item_config, dict) or not item_config:
                results.append({"success": False, "error": "Invalid query config"})
                continue
            config = dict(item_config)
            dashboard_filters = config.pop("dashboard_filters", None) or []

            data_view = (config.get("data_view") or "").strip()
            if not data_view or data_view not in allowed_dvs:
                # Same truncated-token audit pattern as run_public_query.
                # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                logging.warning(
                    "AUDIT: public batch query rejected — data_view=%s not in whitelist token_prefix=%s",
                    data_view,
                    token[:8],
                )
                results.append({"success": False, "error": "data_view not allowed for this report"})
                continue

            if not city_detail and (_references_sub_country_field(config) or _references_sub_country_field(dashboard_filters)):
                results.append({"success": False, "error": "Field not available on this report"})
                continue

            config = _strip_org_team_filters(config)
            config = dict(config)
            config["filters"] = list(config.get("filters") or []) + forced

            try:
                query_body = build_query(config, dashboard_filters=dashboard_filters)
                es_response = es.search(index=scoped_index_pattern(data_view, allowed), body=query_body)
                es_dict = es_response.body if hasattr(es_response, "body") else dict(es_response)
                results.append(transform(es_dict, config))
            except Exception as e:  # per-item boundary: one bad binding must not sink the batch
                logging.warning(f"public query_batch item failed ({data_view}): {e}")
                results.append({"success": False, "error": "Query failed"})

        return jsonify({"results": results})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to run public batch query",
            log_detail="Error running public batch query",
            exc=e,
        )


# follow-up: public mirrors of /dashboard-builder/api/map-data
# and /api/map-data/cities. Pre-664 these endpoints lived only on the
# authenticated blueprint, so a published Impact Report that included a
# map chart (or a leaflet_map dashboard, which is now publish-ineligible
# but was once) couldn't render geo data in the public viewer — every
# map fetch 404'd at the proxy. The public versions force the
# dashboard's publish-time (team, org) scope so client-supplied org/team
# values are ignored (mirrors the scope-escape posture of
# ``/public/api/query``).


@public_report_bp.route("/api/map-data", methods=["GET"])
def public_map_data():
    """Geo aggregates for a published Impact Report's map chart.

    Same dispatch shape as the authenticated ``/dashboard-builder/api/map-data``
    — but the (org, teams) scope comes from the dashboard's publish
    snapshot, NEVER from query params. The ``statistic`` whitelist is
    ``_MAP_STATISTICS``, the same surface area the authenticated viewer
    sees.
    """
    try:
        token = (request.args.get("token") or "").strip()
        if not token:
            return jsonify({"success": False, "error": "Missing token"}), 400

        _, source = _lookup_dashboard_by_token(token)
        if not source:
            return jsonify({"success": False, "error": "Invalid or revoked token"}), 404
        if not _passcode_satisfied(token, source):
            return _passcode_required_response()

        stat_key = request.args.get("statistic", "reach")
        stat_cfg = _db_routes._MAP_STATISTICS.get(stat_key)
        if not stat_cfg:
            return jsonify({"success": False, "error": f"Unknown statistic: {stat_key}"}), 400

        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")

        # Force the publish-time scope; any client-supplied org/team is
        # dropped to prevent scope-escape via the public URL.
        org, teams = _publish_org_team_scope(source)
        if (not org and not teams) or not _pin_publish_scope(source):
            return _scope_missing_response(token)

        # the publisher's map-detail choice is enforced HERE, not in
        # the viewer script — a ``hidden`` link returns no geography at all and
        # a ``country`` link never runs the city / region aggregations, so the
        # geo_point coordinates never leave Elasticsearch for those links.
        map_detail = _map_detail(source)
        if map_detail == "hidden":
            return jsonify(_empty_map_payload(map_detail))
        sub_country = map_detail == "city"

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        data_source = stat_cfg["data_source"]
        cities: list = []
        regions: list = []
        countries: list = []

        if data_source == "fb_insights":
            if sub_country:
                regions = _db_routes._query_fb_region_map(es, stat_cfg, org, teams, date_from, date_to)
            countries = _db_routes._query_fb_country_map(es, stat_cfg, org, teams, date_from, date_to)
        elif data_source == "echo":
            if sub_country:
                cities = _db_routes._query_echo_map(es, stat_cfg, org, teams, date_from, date_to)
            countries = _db_routes._query_echo_country_map(es, stat_cfg, org, teams, date_from, date_to)
        elif data_source == "dt":
            if sub_country:
                cities = _db_routes._query_dt_map(es, stat_cfg, org, teams, date_from, date_to)
            countries = _db_routes._query_dt_country_map(es, stat_cfg, org, teams, date_from, date_to)
        elif data_source == "google_ads":
            if stat_cfg.get("field") == "app_installs":
                countries = _db_routes._query_google_ads_installs_geo_map(es, stat_cfg, org, teams, date_from, date_to)
            else:
                countries = _db_routes._query_team_country_map_generic(
                    es, "*google_ads", stat_cfg, org, teams, date_from, date_to
                )
        elif data_source == "fb_page_insights":
            countries = _db_routes._query_team_country_map_generic(
                es, "*fb_page_insights", stat_cfg, org, teams, date_from, date_to
            )
        elif data_source == "ig_insights":
            countries = _db_routes._query_team_country_map_generic(
                es, "*ig_insights", stat_cfg, org, teams, date_from, date_to
            )
        elif data_source == "youtube":
            countries = _db_routes._query_youtube_country_map(es, stat_cfg, org, teams, date_from, date_to)
            if not countries:
                countries = _db_routes._query_team_title_country_map(
                    es, "*youtube_analytics", stat_cfg, org, teams, date_from, date_to
                )
        elif data_source == "ga_metrics":
            countries = _db_routes._query_ga4_country_map(es, stat_cfg, org, teams, date_from, date_to)
        else:
            return jsonify({"success": False, "error": f"Unknown data source: {data_source}"}), 400

        return jsonify(
            {
                "success": True,
                "cities": cities,
                "regions": regions,
                "countries": countries,
                "map_detail": map_detail,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to fetch public map data",
            log_detail="Error in public_map_data",
            exc=e,
        )


@public_report_bp.route("/api/map-data/cities", methods=["GET"])
def public_map_city_data():
    """Cache-only city markers for a published Impact Report's map chart.

    Mirrors the authenticated ``/dashboard-builder/api/map-data/cities``
    . Same forced-scope discipline as the
    sibling ``public_map_data`` — client-supplied org/team are ignored.
    """
    try:
        token = (request.args.get("token") or "").strip()
        if not token:
            return jsonify({"success": False, "error": "Missing token"}), 400

        _, source = _lookup_dashboard_by_token(token)
        if not source:
            return jsonify({"success": False, "error": "Invalid or revoked token"}), 404
        if not _passcode_satisfied(token, source):
            return _passcode_required_response()

        stat_key = request.args.get("statistic", "reach")
        stat_cfg = _db_routes._MAP_STATISTICS.get(stat_key)
        if not stat_cfg:
            return jsonify({"success": False, "error": f"Unknown statistic: {stat_key}"}), 400

        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")
        org, teams = _publish_org_team_scope(source)
        if (not org and not teams) or not _pin_publish_scope(source):
            return _scope_missing_response(token)

        # city markers exist only for a ``city`` publish. Answer the
        # empty shape the viewer already understands, before touching ES.
        map_detail = _map_detail(source)
        if map_detail != "city":
            return jsonify(
                {
                    "success": True,
                    "cities": [],
                    "total_cities": 0,
                    "resolved_cities": 0,
                    "map_detail": map_detail,
                }
            )

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        # Auth'd cities endpoint short-circuits for non-fb data sources to
        # an empty payload so the frontend can call it unconditionally —
        # match that.
        if stat_cfg["data_source"] != "fb_insights":
            return jsonify(
                {
                    "success": True,
                    "cities": [],
                    "total_cities": 0,
                    "resolved_cities": 0,
                    "map_detail": map_detail,
                }
            )

        cities, total_cities, resolved_cities = _db_routes._query_fb_map_cached_only(
            es, stat_cfg, org, teams, date_from, date_to
        )
        return jsonify(
            {
                "success": True,
                "cities": cities,
                "total_cities": total_cities,
                "resolved_cities": resolved_cities,
                "map_detail": map_detail,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to fetch public map city data",
            log_detail="Error in public_map_city_data",
            exc=e,
        )
