"""
Data Explorer Blueprint Routes
Provides the conversational AI assistant ("Ask Kavanah") for querying ES data
using natural language via the Claude API with tool use.
"""

import logging
from datetime import datetime, timezone

from flask import abort, render_template, request, jsonify, session, send_file

from . import data_explorer_bp

try:
    from web.config import config
    from web.services.nlq_service import (
        chat as nlq_chat,
        get_available_data_views,
        model_choices_distinct,
        structured_query as nlq_structured_query,
        resolve_memory_tenant_keys,
    )
    from web.services.nlq_usage_service import (
        log_query,
        log_feedback,
        get_feature_spend_today,
        get_usage_stats,
        ensure_usage_index,
    )
    from web.services.report_export_service import export_excel, export_pdf
    from web.services.nlq_memory_service import (
        ensure_memory_index,
        update_memory_rating,
        get_suggested_queries,
        get_memory_count,
    )
except ImportError:
    from ...config import config
    from ...services.nlq_service import (
        chat as nlq_chat,
        get_available_data_views,
        model_choices_distinct,
        structured_query as nlq_structured_query,
        resolve_memory_tenant_keys,
    )
    from ...services.nlq_usage_service import (
        log_query,
        log_feedback,
        get_feature_spend_today,
        get_usage_stats,
        ensure_usage_index,
    )
    from ...services.report_export_service import export_excel, export_pdf
    from ...services.nlq_memory_service import (
        ensure_memory_index,
        update_memory_rating,
        get_suggested_queries,
        get_memory_count,
    )


def _require_data_explorer() -> None:
    """Abort with 403 if the user lacks either mc_data_explorer or db_data_explorer.

    mc_data_explorer grants access from the Management Console (admin use case:
    validate imports). db_data_explorer grants access from the dashboards side
    (user use case: ask questions about data). Either one is enough.
    """
    try:
        from web.services.role_permissions_service import RolePermissionsService
    except ImportError:
        from ...services.role_permissions_service import RolePermissionsService
    perms = RolePermissionsService.get_merged_permissions(session.get("roles", []))
    if not perms.get("mc_data_explorer") and not perms.get("db_data_explorer"):
        abort(403, description="This page requires the Data Explorer permission.")


def _ai_call_refusal(username: str) -> tuple[str, int] | None:
    """why an Ask Kavanah call must not reach the model, or None.

    Every user holds Data Explorer, so two checks run before any spend:
    a caller with no team scope would only get the deny-all filter's empty
    answer, and each user has a daily cost cap. Superusers (Kavanah staff)
    are exempt from the cap (a teammate, 2026-10-27). The spend lookup fails
    closed (``get_feature_spend_today`` returns inf on ES errors).
    """
    try:
        from web.services.role_filter_service import RoleFilterService
    except ImportError:
        from ...services.role_filter_service import RoleFilterService
    if not RoleFilterService.current_user_has_any_scope():
        return "You don't have access to any teams yet, so there is no data to ask about.", 403
    if "superuser" in session.get("roles", []):
        return None
    cap = config.NLQ_USER_DAILY_COST_CAP_USD
    if cap > 0 and get_feature_spend_today("nlq", user=username) >= cap:
        return "You've reached today's Ask Kavanah limit. It resets at midnight UTC.", 429
    return None


# ── Blueprint error handlers (return JSON for API routes) ──────────────────────


@data_explorer_bp.errorhandler(403)
def _handle_403(e):
    if request.path.startswith("/data-explorer/api/"):
        return jsonify({"success": False, "error": "Access denied. Data Explorer permission required."}), 403
    return e


@data_explorer_bp.errorhandler(503)
def _handle_503(e):
    if request.path.startswith("/data-explorer/api/"):
        return jsonify({"success": False, "error": str(e.description or "Service unavailable")}), 503
    return e


# ── Page route ────────────────────────────────────────────────────────────────


@data_explorer_bp.route("/")
def index() -> str:
    _require_data_explorer()
    if not config.NLQ_ENABLED:
        abort(503, description="Data Explorer is not enabled. Configure the NLQ section in master-config.yaml.")

    # Get user's allowed orgs from roles for default selection
    user_orgs = []
    try:
        from web.services.role_filter_service import RoleFilterService

        allowed = RoleFilterService.get_allowed_organizations()
        if allowed:
            user_orgs = allowed
    except Exception:
        pass

    # hide the haiku/sonnet selector when both choices resolve to one model.
    return render_template(
        "data_explorer.html",
        nlq_model=config.NLQ_MODEL,
        nlq_model_choice=model_choices_distinct(),
        user_orgs=user_orgs,
    )


# ── API routes ────────────────────────────────────────────────────────────────


@data_explorer_bp.route("/api/chat", methods=["POST"])
def api_chat():
    """Run a conversational turn with the Kavanah AI assistant.

    Expects JSON body: {"messages": [{"role": "user", "content": "..."}]}
    Messages follow the Anthropic format (role: user/assistant).
    """
    _require_data_explorer()
    if not config.NLQ_ENABLED:
        return jsonify({"success": False, "error": "NLQ feature is not enabled"}), 503

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Request body must be JSON"}), 400

    messages = data.get("messages", [])
    if not messages:
        return jsonify({"success": False, "error": "Messages are required"}), 400

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    username = session.get("username", "unknown")
    refusal = _ai_call_refusal(username)
    if refusal:
        return jsonify({"success": False, "error": refusal[0]}), refusal[1]

    # Get the latest user message for logging
    user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            user_message = msg["content"]
            break

    # Run the chat
    result = nlq_chat(messages, today)

    # Log usage
    usage = result.get("usage", {})
    if usage:
        try:
            log_query(
                user=username,
                query_text=user_message,
                data_view="conversation",
                usage=usage,
                success=result.get("success", False),
                error=result.get("error"),
            )
        except Exception as e:
            logging.error(f"Failed to log NLQ usage: {e}")

    if result.get("success"):
        return jsonify(result)
    else:
        status_code = 502 if "API" in result.get("error", "") else 400
        return jsonify(result), status_code


@data_explorer_bp.route("/api/ask", methods=["POST"])
def api_ask():
    """Run a structured Ask Kavanah query.

    Expects JSON body: {
        "query": "natural language question",
        "indices": ["index1", "index2"],  // optional
        "date_range": "Q1 2025",          // optional
        "comparison": "vs same period LY" // optional
    }

    Returns structured result with narrative, stats, chart, table, and ES query.
    """
    _require_data_explorer()
    if not config.NLQ_ENABLED:
        return jsonify({"success": False, "error": "NLQ feature is not enabled"}), 503

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Request body must be JSON"}), 400

    query = data.get("query", "").strip()
    if not query:
        return jsonify({"success": False, "error": "Query is required"}), 400

    indices = data.get("indices") or None
    organizations = data.get("organizations") or None
    teams = data.get("teams") or None
    date_range = data.get("date_range") or None
    comparison = data.get("comparison") or None
    model_choice = data.get("model", "haiku")
    if model_choice not in ("haiku", "sonnet"):
        model_choice = "haiku"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    username = session.get("username", "unknown")
    refusal = _ai_call_refusal(username)
    if refusal:
        return jsonify({"success": False, "error": refusal[0]}), refusal[1]

    result = nlq_structured_query(
        query=query,
        indices=indices,
        organizations=organizations,
        teams=teams,
        date_range=date_range,
        comparison=comparison,
        today=today,
        model_choice=model_choice,
    )

    # Log usage
    usage = result.get("usage", {})
    if usage:
        try:
            log_query(
                user=username,
                query_text=query,
                data_view="structured",
                usage=usage,
                success=result.get("success", False),
                error=result.get("error"),
            )
        except Exception as e:
            logging.error(f"Failed to log NLQ usage: {e}")

    if result.get("success"):
        return jsonify(result)
    else:
        status_code = 502 if "API" in result.get("error", "") else 400
        return jsonify(result), status_code


@data_explorer_bp.route("/api/data-views")
def api_data_views():
    """Return available data views for reference."""
    _require_data_explorer()
    views = get_available_data_views()
    if not views:
        logging.warning("NLQ: no data views found from Kibana or ES")
    return jsonify({"success": True, "data_views": views})


@data_explorer_bp.route("/api/feedback", methods=["POST"])
def api_feedback():
    """Record user feedback (thumbs up/down) for a query result.

    Expects JSON body: {"query": "...", "rating": 1 or -1}
    """
    _require_data_explorer()
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Request body must be JSON"}), 400

    query_text = data.get("query", "")
    rating = data.get("rating", 0)
    if rating not in (1, -1):
        return jsonify({"success": False, "error": "Rating must be 1 or -1"}), 400

    username = session.get("username", "unknown")
    try:
        log_feedback(user=username, query_text=query_text, rating=rating)
        # Also update the memory store rating so it affects few-shot selection.
        # scope the update to the caller's tenant so a user can't rate
        # (and thereby surface/suppress) another tenant's memory.
        update_memory_rating(query_text, rating, tenant_keys=resolve_memory_tenant_keys())
    except Exception as e:
        logging.error(f"Failed to log feedback: {e}")
        return jsonify({"success": False, "error": "Failed to save feedback"}), 500

    return jsonify({"success": True})


@data_explorer_bp.route("/api/export/excel", methods=["POST"])
def api_export_excel():
    """Export saved report sections as an Excel workbook.

    Expects JSON body: {
        "report_name": "Q1 Analysis",
        "sections": [
            {"query": "...", "narrative": "...", "stats": [...], "table": {...}}
        ]
    }
    """
    _require_data_explorer()
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Request body must be JSON"}), 400

    report_name = data.get("report_name", "Report")
    sections = data.get("sections", [])
    if not sections:
        return jsonify({"success": False, "error": "No sections to export"}), 400

    try:
        buf = export_excel(report_name, sections)
        filename = report_name.replace(" ", "_") + ".xlsx"
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Excel export failed",
            log_detail="api_export_excel: export_excel raised",
            exc=e,
        )


@data_explorer_bp.route("/api/export/pdf", methods=["POST"])
def api_export_pdf():
    """Export saved report sections as a PDF document.

    Expects JSON body: same as /api/export/excel
    """
    _require_data_explorer()
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Request body must be JSON"}), 400

    report_name = data.get("report_name", "Report")
    sections = data.get("sections", [])
    if not sections:
        return jsonify({"success": False, "error": "No sections to export"}), 400

    try:
        buf = export_pdf(report_name, sections)
        filename = report_name.replace(" ", "_") + ".pdf"
        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except ImportError:
        return jsonify({"success": False, "error": "PDF library (fpdf2) not installed on server"}), 503
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "PDF export failed",
            log_detail="api_export_pdf: export_pdf raised",
            exc=e,
        )


@data_explorer_bp.route("/api/usage")
def api_usage():
    """Return NLQ usage statistics.

    superusers see the platform-wide figures; everyone else sees
    only their own, so no user learns another tenant's usernames or spend.
    """
    _require_data_explorer()
    is_superuser = "superuser" in session.get("roles", [])
    stats = get_usage_stats(user=None if is_superuser else session.get("username", ""))
    if "error" in stats:
        return jsonify({"success": False, "error": stats["error"]}), 503
    return jsonify({"success": True, "stats": stats})


@data_explorer_bp.route("/api/suggestions")
def api_suggestions():
    """Return suggested query chips from the memory store."""
    _require_data_explorer()
    # suggestions are drawn only from the caller's tenant's memories.
    suggestions = get_suggested_queries(n=6, tenant_keys=resolve_memory_tenant_keys())
    return jsonify({"success": True, "suggestions": suggestions})


@data_explorer_bp.route("/api/memory-count")
def api_memory_count():
    """Return the total number of learned queries in the memory store."""
    _require_data_explorer()
    # the badge counts only the caller's tenant's learned queries.
    count = get_memory_count(tenant_keys=resolve_memory_tenant_keys())
    return jsonify({"success": True, "count": count})


# ── Initialization ────────────────────────────────────────────────────────────

try:
    ensure_usage_index()
except Exception as e:
    logging.warning(f"Could not ensure NLQ usage index exists: {e}")

try:
    ensure_memory_index()
except Exception as e:
    logging.warning(f"Could not ensure NLQ memory index exists: {e}")
