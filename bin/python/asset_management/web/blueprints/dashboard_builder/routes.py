"""
Dashboard Builder Blueprint Routes
Provides API endpoints and page rendering for the visual dashboard builder.
"""

import json
import os
import re
import secrets
import threading
import time
import uuid
import logging
import requests as http_requests
from datetime import datetime, timedelta, timezone
from flask import abort, g, make_response, render_template, request, jsonify, session
from werkzeug.exceptions import HTTPException

from . import dashboard_bp
from . import canvas_theme
from .canvas_seed import (
    SEEDABLE_CHART_TYPES,
    build_canvas_from_proposal,
    convert_dashboard_to_canvas,
    icon_library_has,
    list_icon_library,
    visible_canva_sections,
)

try:
    from lib.es_helpers import (
        get_elasticsearch_client,
        get_all_documents,
        get_document_by_id,
        edit_document,
        delete_document_by_id,
        merge_index_mapping_properties,
    )
    from web.services.es_query_builder import (
        build_query,
        _build_filters,
        breakdown_exclusion_clause,
        _date_range_clause,
        _iso_to_epoch_ms,
        _safe_agg_key,
        table_metric_agg_keys,
        RATIO_FIELDS,
        is_ratio_field,
        chart_dedup_matches,
        dashboard_dedup_matches,
        inflated_reach_warning,
        _is_window_data_view as _is_window_dv_registry,
        _dedup_routing_enabled,
    )
    from web.services.echarts_transformer import transform, _get_agg_value
    from web.services.index_pattern_service import (
        get_index_pattern,
        get_allowed_team_prefixes,
        validate_index_access,
    )
    from web.services.role_permissions_service import RolePermissionsService
    from web.services.role_filter_service import RoleFilterService
    from web.services.dashboard_service import DashboardService
    from web.services.dashboard_team_labels import apply_duplicate_team_labels, owner_team_esid
    from web.services.export_capability_service import (
        IMPACT_REPORT_CHART_TYPES,
        dashboard_export_formats,
        tabular_unsupported_reason,
    )
    from web.config import config
except ImportError:
    from lib.es_helpers import (
        get_elasticsearch_client,
        get_all_documents,
        get_document_by_id,
        edit_document,
        delete_document_by_id,
        merge_index_mapping_properties,
    )
    from services.es_query_builder import (
        build_query,
        _build_filters,
        breakdown_exclusion_clause,
        _date_range_clause,
        _iso_to_epoch_ms,
        _safe_agg_key,
        table_metric_agg_keys,
        RATIO_FIELDS,
        is_ratio_field,
        chart_dedup_matches,
        dashboard_dedup_matches,
        inflated_reach_warning,
        _is_window_data_view as _is_window_dv_registry,
        _dedup_routing_enabled,
    )
    from services.echarts_transformer import transform, _get_agg_value
    from services.index_pattern_service import (
        get_index_pattern,
        get_allowed_team_prefixes,
        validate_index_access,
    )
    from services.role_permissions_service import RolePermissionsService
    from services.role_filter_service import RoleFilterService
    from services.dashboard_service import DashboardService
    from services.dashboard_team_labels import apply_duplicate_team_labels, owner_team_esid
    from services.export_capability_service import (
        IMPACT_REPORT_CHART_TYPES,
        dashboard_export_formats,
        tabular_unsupported_reason,
    )
    from config import config

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"


def _require_index_access(data_view):
    """Abort with 403 if the current user may not access *data_view*."""
    if not validate_index_access(data_view):
        abort(403, description="Access denied: index outside your organization scope.")


# ──────────────────────────────────────────────
# Dashboard authorization
# ──────────────────────────────────────────────
#
# Two orthogonal checks govern dashboard-builder endpoints:
#
#   * Read  — respect the dashboard's ``visibility_mode`` + ``allowed_*_esids``
#             so users cannot read configs for dashboards they aren't allowed
#             to see (including ``hide``).  Admins bypass.
#   * Write — require the ``mc_dashboard_builder`` permission (or an admin
#             role), so regular dashboard viewers cannot create, edit,
#             delete, or publish dashboards.
#
# Both helpers raise 403 (not 404) — 404 would leak dashboard existence.


def _current_user_roles():
    return session.get("roles", []) or []


def _current_user_is_ui_admin():
    """UI-admin bypass for management-console actions: creating /
    editing / deleting dashboards, publishing reports, opening admin
    pages. Returns True for ``superuser`` or ``asset_admin``
    (``config.REQUIRED_ROLE``).

     (2026-05-15): renamed from ``_current_user_is_admin`` to
    make the intent unambiguous. The old name was used in two
    incompatible places — as a *UI* admin gate on write paths AND as
    a *data* admin bypass on the dashboard read path. The data bypass
    is the leak this ticket closes. Read-path checks now go through
    visibility_mode + ``_current_user_scope_esids`` for ALL users;
    only the write/publish gates retain the admin bypass via this
    helper.
    """
    roles = _current_user_roles()
    return "superuser" in roles or config.REQUIRED_ROLE in roles


def _current_user_can_build_dashboards():
    """True if the user's merged permissions include ``mc_dashboard_builder``.

    UI admins (``superuser`` / ``asset_admin``) always return True via
    ``_current_user_is_ui_admin``. Non-admins must hold a role whose
    merged permissions grant ``mc_dashboard_builder``.

    scoped admins (workspace_admin / org_admin etc.) hold
    REPORT_EDIT via the new authz model and should be able to build
    dashboards too. OR-with-OLD-path semantics consistent with the
    rest of the migration.
    """
    if _current_user_is_ui_admin():
        return True
    perms = RolePermissionsService.get_merged_permissions(_current_user_roles())
    if perms.get("mc_dashboard_builder"):
        return True
    from flask import session as _session
    from asset_management.web.services.permission_resolver import (
        PermissionResolver,
        SIDEBAR_ITEM_PERMISSIONS,
    )

    needed = SIDEBAR_ITEM_PERMISSIONS.get("mc_dashboard_builder", ())
    if not needed:
        return False
    held = PermissionResolver.effective_permissions_anywhere(
        _session.get("username", ""),
        is_super_admin="superuser" in _session.get("roles", []),
    )
    return any(p in held for p in needed)


def _current_user_can_publish_reports():
    """True if the user may publish / unpublish / regenerate report links.

    UI admins always return True. Otherwise the user must hold a role
    whose merged permissions grant ``publish_reports`` (e.g. the
    dedicated ``report_publisher`` system role).

    Decoupled from ``mc_dashboard_builder`` so an org admin can grant
    "share this report publicly" without also granting "edit any dashboard."
    """
    if _current_user_is_ui_admin():
        return True
    perms = RolePermissionsService.get_merged_permissions(_current_user_roles())
    return bool(perms.get("publish_reports"))


def _current_user_scope_esids():
    """Return ``(team_esids, org_esids)`` for the current request.

    Roles in the session are ``team_<title>`` / ``org_<title>`` strings —
    the dashboard stores scope as ES document IDs. Resolve once and cache on
    ``flask.g`` so ``list_dashboards`` (which calls ``_user_may_read_dashboard``
    per dashboard) doesn't re-hit Elasticsearch for every row.
    """
    cached = getattr(g, "_dashboard_scope_esids", None)
    if cached is not None:
        return cached
    resolved = DashboardService._resolve_user_scope_esids(_current_user_roles())
    g._dashboard_scope_esids = resolved
    return resolved


def _label_viewer_team_esids() -> set[str] | None:
    """the teams whose names a duplicate-title label may show this
    viewer: None (any team) for a privileged user, who already sees every
    team's dashboards in the workspace (the #2a bypass below), else the
    viewer's own team scope."""
    from asset_management.web.services.role_filter_service import RoleFilterService

    try:
        if (session.get("username", "") or "") and RoleFilterService.current_user_is_privileged():
            return None
    except Exception:  # noqa: BLE001 — fail closed to the viewer's own teams
        pass
    return set(_current_user_scope_esids()[0])


def _user_may_read_dashboard(source):
    """Return True if the current user is allowed to read *source* INSIDE
    the Dashboard Builder.

     (2026-05-15): the UI-admin bypass on ``show_teams`` was
    removed; ALL users — including ``superuser`` and ``asset_admin`` —
    resolve through ``visibility_mode`` + ``_current_user_scope_esids``.
    The scope helper returns memberships UNION grant-derived ESIDs.
    A privileged user with the relevant membership/active grant sees
    the dashboard; a bare privileged user only sees ``show_all``
    dashboards.

     (2026-05-18): ``hide`` is reframed as a **viewer-side**
    setting — it hides the dashboard on the user-facing /dashboards
    page (``DashboardService.get_visible_echarts_dashboards``), but
    NOT inside the Dashboard Builder. Editors with
    ``mc_dashboard_builder`` always see hidden dashboards so they
    can un-hide them; without this, a dashboard marked hide is
    permanently unreachable through the UI. The whole Builder
    blueprint is page-gated by ``mc_dashboard_builder``, so this
    helper's "hide bypass" is effectively the same gate the user
    already passed to load the page. Public viewer (publish token)
    lives in the ``public_report`` blueprint and does NOT call this
    helper.

     #1 / (2026-05-19): workspace gate layered on top of
    the visibility_mode check, mirroring the same gate
    ``DashboardService.get_visible_echarts_dashboards`` applies to the
    user-facing /dashboard page. Without this, the Builder's
    ``/api/dashboards`` list AND the direct-open ``/api/dashboards/<id>``
    endpoint both leak cross-workspace dashboards. The gate is
    additive — a dashboard whose workspace_esid is NOT in the user's
    allowed set is hidden even if visibility_mode==show_all. Legacy
    dashboards without a workspace_esid stamp pass through during the
    migration window; the backfill (tools/backfill_dashboard_workspace_esid.py)
    stamps them so the legacy branch is empty post-migration.
    ``get_allowed_workspaces()`` is None only for a role-less non-privileged
    account or when the lookup fails; a privileged user gets the workspaces
    they reach through membership, role or grant, so the gate
    applies to them too, matching the user-facing helper's contract.
    """
    visibility_mode = (source or {}).get("visibility_mode", "show_all")

    # regression fix: the creator can ALWAYS read their own dashboard,
    # regardless of team scope or workspace gate. Under teams-only visibility a
    # creator with no team scope (a bare superuser/admin) would otherwise fail
    # the show_teams intersection check and never see what they just created.
    # Scope still governs every other viewer; this only widens the creator's
    # own view of their own doc.
    created_by = (source or {}).get("created_by") or ""
    if created_by and created_by == (session.get("username", "") or ""):
        return True

    # #1 / workspace gate. Applied before the
    # visibility_mode branches so a show_all dashboard from another
    # workspace doesn't leak.
    try:
        from asset_management.web.services.role_filter_service import (
            RoleFilterService,
        )

        allowed_ws = RoleFilterService.get_allowed_workspaces()
    except Exception:
        allowed_ws = None  # ES outage / bootstrap race — fail open (legacy contract)
    if allowed_ws is not None:
        dashboard_workspace_esid = (source or {}).get("workspace_esid") or ""
        if dashboard_workspace_esid and dashboard_workspace_esid not in set(allowed_ws):
            return False

    # #2a: workspace-scoped admin visibility. A privileged admin
    # (superuser / asset_admin / workspace admin) sees every dashboard within
    # the workspaces they can resolve — the workspace gate above already
    # narrowed to those (a privileged user resolves to the workspaces they reach
    # through membership, role or grant, not to all of them). Without
    # this, the teams-only redesign left an admin with
    # no team scope unable to see dashboards other users created in their own
    # workspace (the cross-visibility bug). Org/team-tier users are NOT
    # privileged here, so they remain governed by the team-intersection below —
    # tenant isolation across orgs within a shared workspace is preserved.
    # Require a real logged-in username: ``current_user_is_privileged()``
    # returns True OUTSIDE a request context (its CLI contract), so gating on a
    # username avoids a fail-open when this gate is reached without a session.
    try:
        if (session.get("username", "") or "") and RoleFilterService.current_user_is_privileged():
            return True
    except Exception:  # noqa: BLE001 — fail closed to the scope checks below
        pass

    if visibility_mode == "hide":
        # editors always see hidden dashboards in the
        # Builder context. Non-editors who somehow reach this
        # helper still get the deny (defense-in-depth — the page
        # gate at ``index()`` would normally have already 403'd).
        return _current_user_can_build_dashboards()
    if visibility_mode == "show_all":
        return True
    if visibility_mode == "show_teams":
        allowed_org_esids = source.get("allowed_org_esids", []) or []
        allowed_team_esids = source.get("allowed_team_esids", []) or []
        if not allowed_org_esids and not allowed_team_esids:
            return False
        user_team_esids, user_org_esids = _current_user_scope_esids()
        return not user_team_esids.isdisjoint(allowed_team_esids) or not user_org_esids.isdisjoint(allowed_org_esids)
    # Unknown visibility_mode: fail closed.
    return False


def _abort_json(status, message):
    """Abort with a JSON ``{"success": False, "error": ...}`` body.

    Matches the shape the builder API uses in its happy paths, so the
    frontend can parse errors uniformly.  Flask's ``abort(int)`` normally
    produces HTML, which breaks JSON callers.
    """
    abort(make_response(jsonify({"success": False, "error": message}), status))


def _load_dashboard_for_read(dashboard_id):
    """Fetch a dashboard doc and enforce read authorization.

    Returns the ``_source`` dict on success.  Aborts with 404 if the doc
    does not exist, or 403 if the current user may not read it.  403 is
    deliberately returned even when the caller would otherwise not know
    the dashboard exists — we don't want to leak existence.
    """
    source = get_document_by_id(DASHBOARDS_INDEX, dashboard_id)
    if source is None:
        _abort_json(404, "Dashboard not found")
    if not _user_may_read_dashboard(source):
        _abort_json(403, "You do not have access to this dashboard.")
    return source


def _require_builder_write():
    """Abort with 403 unless the user can create/edit/delete dashboards.

    Write paths (POST/PATCH/DELETE on ``/api/dashboards``) require
    ``mc_dashboard_builder`` — an admin role is also sufficient.
    Visibility per-dashboard is checked separately when the write
    targets an existing dashboard.
    """
    if not _current_user_can_build_dashboards():
        _abort_json(403, "Dashboard builder permission is required.")


# the three-gate AI Assistant model. A user gets the AI
# Assistant only when ALL of these hold (plus the global kill switch):
#   1. workspace_enabled — a superuser turned AI Assistant on for the
#      user's billing workspace (de_asset_workspace.ai_assistant_enabled)
#   2. dashboard_builder — the user can reach the canvas at all
#   3. ai_role — the user holds the opt-in ``ai_assistant`` role
#      (mc_ai_assistant via get_merged_permissions; superusers carry it)
# Reasons are machine-readable so an admin can tell WHICH gate is closed
# from the 403 body or the log line without reading code.


def _canvas_ai_gate():
    """Evaluate the AI Assistant gates for the current session.

    Returns ``(ok, reason, status, message)``. ``reason`` is one of
    ``feature_disabled`` / ``no_dashboard_builder`` / ``no_ai_role`` /
    ``no_workspace`` / ``workspace_disabled`` ("" when open). Every closed
    gate is logged with the username so support can diagnose "I don't have
    AI Assistant" without a code dive. Fail-closed throughout: unresolved
    workspace state denies (spend-metered feature).
    """
    username = session.get("username", "") or ""
    if not config.CANVAS_AI_ENABLED:
        return False, "feature_disabled", 503, "Canvas AI chat is not enabled"

    # Gate 1 — workspace enablement (checked first: it's the tenant-level
    # switch, and the message tells the user who can fix it).
    try:
        from web.services.workspace_billing_service import WorkspaceBillingService
        from web.services.workspace_membership_service import WorkspaceMembershipService
    except ImportError:
        from ...services.workspace_billing_service import WorkspaceBillingService
        from ...services.workspace_membership_service import WorkspaceMembershipService

    workspace_esid = None
    try:
        workspace_esid = WorkspaceBillingService.resolve_ai_workspace_esid(username)
    except Exception:
        logging.exception("Canvas AI gate: workspace resolution failed for %s", username)
    if not workspace_esid:
        logging.info("Canvas AI gate closed for %s: no_workspace", username)
        return (
            False,
            "no_workspace",
            403,
            "No workspace could be resolved for your account, so the AI Assistant is unavailable.",
        )
    # A superuser skips the ENABLEMENT half of gate 1 but still had to
    # resolve a workspace above — spend must stay attributed to a real
    # tenant. Bypassing resolution too would meter their turns to nothing
    # and blank the readout, which is worse than being blocked on a
    # spend-metered feature. Gates 2 and 3 are already all-True for them
    # via get_merged_permissions, so this makes superuser access uniform.
    if "superuser" in (session.get("roles") or []):
        return True, "", 200, ""
    if not WorkspaceMembershipService.ai_assistant_enabled_for_workspace(workspace_esid):
        logging.info("Canvas AI gate closed for %s: workspace_disabled (workspace=%s)", username, workspace_esid)
        return (
            False,
            "workspace_disabled",
            403,
            "The AI Assistant is not enabled for your workspace. A Kavanah administrator can enable it.",
        )

    # Gate 2 — canvas access.
    if not _current_user_can_build_dashboards():
        logging.info("Canvas AI gate closed for %s: no_dashboard_builder", username)
        return False, "no_dashboard_builder", 403, "Dashboard builder permission is required."

    # Gate 3 — the opt-in ai_assistant role.
    perms = RolePermissionsService.get_merged_permissions(_current_user_roles())
    if not perms.get("mc_ai_assistant"):
        logging.info("Canvas AI gate closed for %s: no_ai_role", username)
        return (
            False,
            "no_ai_role",
            403,
            "You don't have the AI Assistant role. Your workspace admin can grant it from User Management.",
        )

    return True, "", 200, ""


def _canvas_ai_spend_payload():
    """Format the workspace-spend readout for the AI panel.

    Returns ``{"label": "...", "title": "..."}`` or ``None`` (hide the
    readout: no resolvable workspace, or the usage index errored — the
    footrow renders blank rather than an error string or inf).
    """
    try:
        from web.services.workspace_billing_service import WorkspaceBillingService
    except ImportError:
        from ...services.workspace_billing_service import WorkspaceBillingService

    try:
        readout = WorkspaceBillingService.get_ai_spend_readout(session.get("username", "") or "")
    except Exception:
        logging.exception("Canvas AI: spend readout failed")
        return None
    if not readout or readout.get("spend_usd") is None:
        return None
    since_day = (readout.get("cycle_start") or "")[:10]
    window = "current billing period" if readout.get("source") == "stripe" else "this calendar month"
    spend = readout["spend_usd"]
    # Sub-cent spend must read as movement, not as zero — a handful of
    # Haiku turns costs fractions of a cent and "$0.00" after real usage
    # looks like a broken counter (it was, once: the staging
    # text-mapping bug was reported as exactly "does that number go up").
    amount = "< $0.01" if 0 < spend < 0.005 else f"${spend:,.2f}"
    return {
        "label": f"Workspace AI spend: {amount}",
        "title": f"Estimated workspace-wide AI cost for the {window} (since {since_day}).",
    }


def _enforce_visibility_scope(visibility_mode):
    """reject ``show_all`` from a non-workspace-admin caller.

    ``show_all`` is workspace-wide visibility — it publishes a dashboard
    above the scope of any org-tier / team-tier user. The dropdown UI
    hides the option for those users (``index`` passes
    ``is_workspace_admin`` to the template), but the UI fix alone is a
    privilege-escalation gap: a user can craft a POST/PATCH with
    ``visibility_mode=show_all`` directly. This is the server-side
    defense-in-depth layer, mirroring the Layer-1 scope enforcement
    pattern from the write path validates scope regardless of
    what the client sends.

    Workspace admins / owners (and superusers, via the bypass baked into
    ``_current_user_is_workspace_admin``) are unaffected. Anyone else
    sending ``show_all`` gets a 403; every other ``visibility_mode`` value
    (``show_teams`` / ``hide``) is left untouched here — its own
    allow-list scoping is enforced by ``_user_may_read_dashboard`` on the
    read path.
    """
    if visibility_mode == "show_all" and not _current_user_is_workspace_admin():
        _abort_json(403, "Only workspace admins can set workspace-wide (Show All) visibility.")


def _normalize_write_visibility_scope(data):
    """collapse the write-side visibility model to teams-only.

    The Builder no longer offers ``show_all`` (workspace-wide) or ``hide`` as
    selectable modes, and the Organizations checkbox list is gone. Visibility
    now always hinges on the **teams** allow-list:

      * Every saved dashboard is written ``visibility_mode = "show_teams"``.
      * ``allowed_org_esids`` is always ``[]`` (org tier dropped from the editor;
        the read path's org-intersection branch still exists for legacy docs but
        receives no new org scope from a write).
      * ``allowed_team_esids`` is the set of ticked teams. An empty list means the
        dashboard is visible to no one — this replaces the old ``hide`` mode.

    This is the server-side enforcement (defense-in-depth, mirroring /
    ): a crafted POST/PATCH carrying ``show_all`` / ``hide`` / org scope is
    normalized away regardless of what the client sends.

    Scope constraint: a non-workspace-admin may only grant
    teams within their **own** team scope (memberships ∪ grants). A request that
    names a team outside that scope is a privilege-escalation attempt and gets a
    403 — a legitimate UI only ever renders the caller's own teams. Workspace
    admins / owners + superusers may grant any team (the picker shows their whole
    workspace) — bounded, for everyone but a superuser, to the
    dashboard's own workspace by ``_require_visibility_teams_in_workspace``,
    which every caller runs once the target workspace is known.

    Returns ``(visibility_mode, allowed_org_esids, allowed_team_esids)`` ready to
    stamp on the ES doc. Aborts 403 on an out-of-scope team.
    """
    # Dedupe + coerce to a clean list of non-empty strings, order-stable.
    raw_teams = data.get("allowed_team_esids") or []
    seen: set[str] = set()
    requested_team_esids: list[str] = []
    for t in raw_teams:
        if isinstance(t, str) and t and t not in seen:
            seen.add(t)
            requested_team_esids.append(t)

    if not _current_user_is_workspace_admin():
        user_team_esids, _user_org_esids = _current_user_scope_esids()
        out_of_scope = [t for t in requested_team_esids if t not in user_team_esids]
        if out_of_scope:
            _abort_json(403, "You can only share a dashboard with teams you belong to.")

    return "show_teams", [], requested_team_esids


_TEAMS_INDEX = "de_asset_teams"


def _require_visibility_teams_in_workspace(team_esids: list[str], workspace_esid: str) -> None:
    """a visibility write may name only teams of the dashboard's own
    workspace.

    ``_normalize_write_visibility_scope`` lets any workspace admin name any team
    — and ``_current_user_is_workspace_admin`` answers for the caller's PRIMARY
    workspace only, so every self-signup owner passed it. The narrowing
    in ``save_dashboard`` dropped a team only when it could prove the team lived
    elsewhere, keeping unknown / unstamped teams and every team on a lookup
    error. A non-superuser now gets a 403 unless every named team's
    ``de_asset_teams.workspace_esid`` equals ``workspace_esid`` (the dashboard's
    stamp, or the workspace a new dashboard is being saved into). Unknown team,
    team with no workspace, unstamped dashboard → 403; lookup failure → 503.
    The Builder's team picker already lists only teams of the selected
    workspace, so the UI never sends anything else.

    Superusers are unchanged: they keep the narrowing in the save path.
    """
    if not team_esids or "superuser" in (session.get("roles") or []):
        return
    refusal = "You can only share a dashboard with teams in the dashboard's own workspace."
    if not workspace_esid:
        _abort_json(403, refusal)
    es = get_elasticsearch_client()
    if es is None:
        _abort_json(503, "Could not verify the teams this dashboard is shared with. Try again.")
    try:
        resp = es.search(
            index=_TEAMS_INDEX,
            body={
                "query": {"terms": {"_id": list(team_esids)}},
                "_source": ["workspace_esid"],
                "size": len(team_esids),
            },
        )
    except Exception:  # noqa: BLE001 — authorization lookup, fail closed
        logging.warning("team workspace lookup failed for workspace %s", workspace_esid)
        _abort_json(503, "Could not verify the teams this dashboard is shared with. Try again.")
    team_workspace = {
        hit.get("_id"): (hit.get("_source") or {}).get("workspace_esid") or ""
        for hit in (resp.get("hits") or {}).get("hits") or []
    }
    if any(team_workspace.get(t) != workspace_esid for t in team_esids):
        _abort_json(403, refusal)


def _current_user_admins_dashboard_workspace(source) -> bool:
    """True iff the caller is a superuser or holds workspace_admin /
    workspace_owner on the workspace ``source`` is stamped with.

    An unstamped (legacy) dashboard belongs to no provable workspace, so no
    workspace role covers it. Fails closed on any lookup error.
    """
    if "superuser" in (session.get("roles") or []):
        return True
    workspace_esid = (source or {}).get("workspace_esid") or ""
    username = session.get("username", "") or ""
    if not workspace_esid or not username:
        return False
    try:
        from asset_management.web.services.workspace_membership_service import (
            WorkspaceMembershipService,
        )

        return WorkspaceMembershipService.get_role(username, workspace_esid) in _WORKSPACE_ADMIN_ROLES
    except Exception:  # noqa: BLE001 — authorization check, fail closed
        logging.warning("workspace role lookup failed for %s on %s", username, workspace_esid)
        return False


def _require_dashboard_mutate_scope(source, allow_empty_scope=False):
    """gate a destructive/owning mutation on per-dashboard scope.

    ``_load_dashboard_for_read`` (via ``_user_may_read_dashboard``) is a
    READ gate — it lets any user, including an org-tier ``org_admin`` /
    ``owner`` with no workspace role, *read* a ``show_all`` workspace-shared
    dashboard. Read access is NOT mutate access. ``delete_dashboard`` was
    gated only by ``_require_builder_write`` (any ``REPORT_EDIT`` holder)
    plus the read gate, so an org-only user could delete ANY dashboard by
    ID — including a ``show_all`` dashboard belonging to sibling orgs in the
    same agency workspace, rippling the delete across the whole workspace.

    Model "b" (confirmed by a teammate 2026-06-02): org/team users may *read*
    workspace-shared dashboards but may only *mutate* ones scoped to their
    own org/teams. "Block show_all only" (a teammate 2026-06-04): the gate targets
    exactly the ticket's leak without regressing the two adjacent contracts.

      * Workspace admins / owners + superusers (``_current_user_is_workspace_admin``)
        keep full mutate rights.
      * ``show_all`` (workspace-wide visibility) is denied for everyone else —
        it is owned above the org/team tier; this is the leak closes.
      * ``hide`` is permitted — requires any Builder editor to be able
        to delete hidden dashboards, which would otherwise be permanently
        unreachable for cleanup. The page is already gated by
        ``mc_dashboard_builder``; ``_require_builder_write`` re-checks it.
      * ``show_teams`` requires the dashboard's allowed-org/team scope to
        intersect the caller's own scope, mirroring the ``show_teams`` branch
        of ``_user_may_read_dashboard``.

    ``asset_admin`` is NOT bypassed here: dashboard data access
    derives from scope, not the UI-admin flag, and delete is a data mutation.

    ``allow_empty_scope``: when True, a ``show_teams`` dashboard with
    NO allowed teams/orgs (private-to-creator, the teams-only replacement for
    ``hide``) is permitted instead of denied. Deletion must never be gated on
    visibility scope — an empty-scope dashboard is shared with no team, so
    deleting it cannot ripple across a workspace (the leak this gate
    closes), and a caller who reached here has already passed the read gate
    (creator / privileged). Set True only on the delete path; the save and
    properties paths leave it False so an unticked-teams Save is still blocked
    upstream by the normal scope checks.

    the admin bypass is per-workspace. ``_current_user_is_workspace_admin``
    answers for the caller's PRIMARY workspace only, so every workspace owner
    (every self-signup user) used to pass for any dashboard id. A non-superuser
    now bypasses only when they hold workspace_admin / workspace_owner on the
    dashboard's own ``workspace_esid``; otherwise they fall through to the
    creator and team-scope rules like any other user.

    Aborts 403 on denial; returns ``None`` when the mutation is permitted.
    """
    if _current_user_is_workspace_admin() and _current_user_admins_dashboard_workspace(source):
        return

    # regression fix: the creator can always mutate their own dashboard,
    # mirroring the creator short-circuit on the READ gate
    # (``_user_may_read_dashboard``). Without this, a creator whose team scope
    # doesn't intersect the dashboard they JUST created (a bare superuser/admin,
    # or an org user whose resolved team scope differs from the seeded teams)
    # could see their dashboard but get 403 trying to save/delete it — they'd be
    # locked out of editing their own work.
    created_by = (source or {}).get("created_by") or ""
    if created_by and created_by == (session.get("username", "") or ""):
        return

    visibility_mode = (source or {}).get("visibility_mode", "show_all")
    if visibility_mode == "show_all":
        # Workspace-wide visibility — above any org/team user's authority.
        _abort_json(403, "You do not have permission to modify this dashboard.")
    if visibility_mode == "hide":
        # hidden dashboards stay deletable by Builder editors so
        # they don't become permanently unreachable.
        return

    # show_teams: SUPERSET rule. A delete or
    # config-overwrite affects EVERY team the dashboard is shared with, so a
    # non-admin non-creator may mutate it only when they own ALL of its scope —
    # every allowed team AND org must be within the caller's own scope. The
    # prior intersection rule let an org user in team X delete a dashboard
    # shared with [X, Y], removing it for team Y they don't control. An empty
    # allowed-scope (private-to-creator) is reachable only by the creator/admin
    # branches above, so reaching here with empty scope is a deny.
    allowed_org_esids = source.get("allowed_org_esids", []) or []
    allowed_team_esids = source.get("allowed_team_esids", []) or []
    if not allowed_org_esids and not allowed_team_esids:
        # empty-scope (private-to-creator) is deletable. Deletion is
        # never gated on visibility scope; only the save/properties paths keep
        # the deny (allow_empty_scope=False).
        if allow_empty_scope:
            return
        _abort_json(403, "You do not have permission to modify this dashboard.")
    user_team_esids, user_org_esids = _current_user_scope_esids()
    teams_owned = set(allowed_team_esids).issubset(user_team_esids)
    orgs_owned = set(allowed_org_esids).issubset(user_org_esids)
    if not (teams_owned and orgs_owned):
        _abort_json(
            403,
            "You can only modify a dashboard that is shared only with teams you belong to.",
        )


def _require_publish_reports():
    """Abort with 403 unless the user holds the publish_reports capability.

    Used by the publish/unpublish/regenerate endpoints. Replaces the prior
    coupling to ``mc_dashboard_builder`` so a user with the dedicated
    ``report_publisher`` role can mint share links without being a full
    dashboard editor.
    """
    if not _current_user_can_publish_reports():
        _abort_json(403, "Publish-reports permission is required.")


# ──────────────────────────────────────────────
# Page
# ──────────────────────────────────────────────


@dashboard_bp.route("/")
def index():
    """Render the Dashboard Builder page.

    gate the MC sidebar destination on ``mc_dashboard_builder``.
    The viewer (``/view/<id>``) stays loose by design — see ticket non-goals.
    """
    if not _current_user_can_build_dashboards():
        abort(403, description="This page requires the Dashboard Builder permission.")
    # Server-rendered "today" (UTC) so the custom-range date picker caps the
    # upper bound on the same reference the server uses for date math
    # (e.g. ``now`` / ``now-90d/d``). Mirrors the pattern used in
    # analytics/routes.py for the dashboards page.
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # surface workspace-admin status so the left-panel
    # "+ Add Menu" button + menu drag-handles render only for users
    # who can actually exercise the write endpoints. The write-route
    # gate is still server-enforced — this flag only governs UI
    # disclosure (matches the existing IS_SUPERUSER pattern in
    # user_management.html).
    is_workspace_admin = _current_user_is_workspace_admin()
    # #1 + #3 — Save As Template + Choose Template Edit/Delete
    # are platform-curation surfaces, restricted to superusers (template
    # docs are global; workspace_admins shouldn't be able to mint or
    # destroy templates that affect every workspace).
    is_superuser = "superuser" in (session.get("roles") or [])
    # follow-up (smoke 2026-05-25) — apply the same single-of-each
    # rule the /dashboards page uses to the Builder's own topbar org+team
    # dropdowns. The Builder filters are independent of the page's filters
    # but the rationale is identical: an individual wizard user with one
    # org + one team has nothing to switch between.
    from asset_management.web.services.role_filter_service import RoleFilterService

    try:
        filter_visibility = RoleFilterService.get_filter_visibility()
    except Exception:  # noqa: BLE001 — never block /dashboard-builder render
        filter_visibility = {"show_org_filter": True, "show_team_filter": True}
    # visibility-dropdown defaults. Workspace admins keep the
    # full dropdown (incl. "Show All") and the legacy show_all default;
    # org/team-tier users lose "Show All" and default to "Show Teams"
    # with their own scope pre-selected. Server-side enforcement
    # (_enforce_visibility_scope) is the load-bearing gate — this only
    # governs UI disclosure + the new-dashboard seed.
    visibility_defaults = _builder_visibility_defaults(is_workspace_admin)
    # workspace selector context. A multi-workspace user (or bare
    # superuser → all workspaces) gets a topbar selector; a single-workspace
    # user gets a read-only label; zero → hidden. The selected workspace drives
    # the org/team dropdowns and the new-dashboard create-stamp, so a superuser
    # can no longer accidentally write a dashboard into the wrong workspace.
    try:
        builder_workspaces = _selectable_workspaces_for_user()
    except Exception:  # noqa: BLE001 — never block the render
        builder_workspaces = []
    builder_default_workspace = _default_workspace_esid(builder_workspaces)
    return render_template(
        "dashboard_builder/index.html",
        today_utc=today_utc,
        is_workspace_admin=is_workspace_admin,
        is_superuser=is_superuser,
        filter_visibility=filter_visibility,
        visibility_defaults=visibility_defaults,
        builder_workspaces=builder_workspaces,
        builder_default_workspace=builder_default_workspace,
    )


@dashboard_bp.route("/view/<dashboard_id>")
def view_dashboard(dashboard_id):
    """Read-only viewer for a saved dashboard (embedded in dashboards.html iframe).

     follow-up: canvas pages render through their own read-only
    viewer template (same iframe contract — it posts
    ``echarts-content-ready`` when painted). Not superuser-gated: viewing is
    read-only and rides the same ``_load_dashboard_for_read`` gate as any
    dashboard; only the EDITOR page keeps the superuser gate.
    """
    source = _load_dashboard_for_read(dashboard_id)
    if source.get("dashboard_type") == "canvas":
        return render_template(
            "dashboard_builder/canvas_view.html",
            dashboard_id=dashboard_id,
            canvas_theme_tokens=canvas_theme.tokens_json(),
        )
    return render_template("dashboard_builder/view.html", dashboard_id=dashboard_id)


@dashboard_bp.route("/debug")
def debug_view():
    """Cross-dashboard stat comparison table for testing/debugging.

    superuser-only, 404 for everyone else (a developer tool, not a
    tenant page — and 404 does not advertise that it exists). The app has no
    dev/prod switch the route could key on (``DEPLOY_ENV`` is read only by
    secret validation), so the role gate is the guard in every environment.
    """
    if "superuser" not in (session.get("roles") or []):
        abort(404)
    return render_template("dashboard_builder/debug.html")


@dashboard_bp.route("/canvas-prototype")
def canvas_prototype():
    """Canvas Builder spike — THROWAWAY prototype, superuser-only.

    Proves the riskiest slice of the Canvas Builder epic: a live
    data field (bound through the real ``/api/query`` metric path, so scope
    enforcement applies) and a text box, both free-form positioned on an
    absolute-positioned HTML/CSS page, in edit and published-view modes.
    Nothing persists — the canvas document lives in JS memory only. See
    the private docs (2026-07-07) for the architecture this
    validates; the production editor lands +.

    Superuser-only: this is a spike surface, not a product page. It exposes
    no data a superuser couldn't already reach via the builder.
    """
    if "superuser" not in (session.get("roles") or []):
        abort(403, description="Canvas prototype is superuser-only.")
    return render_template("dashboard_builder/canvas_prototype.html")


@dashboard_bp.route("/canvas")
def canvas_editor():
    """Canvas Builder editor: page setup,
    background, rulers/guides/grid, and save/load/rename/delete of
    ``dashboard_type: "canvas"`` documents.

     dropped the interim superuser gate: the editor is now
    open to everyone who can use the Dashboard Builder. The generic
    ``/dashboard-builder/`` prefix rule in auth.py enforces
    ``mc_dashboard_builder`` upstream for non-superusers, and all persistence
    runs through the existing ``/api/dashboards`` routes with their own
    write/mutate-scope gates — so this route needs no gate of its own,
    exactly like the builder index.

    The editor reads ``?id=<dashboard_id>`` client-side to open an existing
    document; without it, it starts a blank page.

    ``canvas_ai_enabled`` shows/hides the AI chat
    panel — it is the full three-gate verdict (workspace
    enabled + dashboard builder + ai_assistant role), not just the global
    flag. The /api/ai/chat endpoint re-checks the same gates server-side,
    so the template switch is presentation only.

    ``canvas_ai_spend`` is the current-cycle workspace AI spend
    for the panel footrow — visible to every gated-in user (decision
    2026-07-28: the workspace total is a shared budget, not a secret).
    None (readout hidden / blank) when the workspace can't be resolved or
    the usage index errored — never an error string, never inf.
    """
    ai_ok, _ai_reason, _ai_status, _ai_message = _canvas_ai_gate()
    canvas_ai_spend = _canvas_ai_spend_payload() if ai_ok else None
    return render_template(
        "dashboard_builder/canvas.html",
        canvas_ai_enabled=ai_ok,
        canvas_ai_spend=canvas_ai_spend,
        canvas_theme_tokens=canvas_theme.tokens_json(),
    )


# ── seed a canvas page from an existing report ──────────────────
# Conversion logic lives in canvas_seed.py (pure, unit-testable); these
# routes only add authz + transport. Relationship model: COPY-on-seed
# (the private docs 2026-07-09) — the converted config is returned
# unsaved and persists as a NEW canvas doc via the normal save route.


@dashboard_bp.route("/api/canvas-seed-sources", methods=["GET"])
def canvas_seed_sources():
    """List dashboards the current user may read that can seed a canvas page.

    A dashboard is eligible when it is an ``echarts`` doc with at least one
    chart of a seedable type (grouped_table / report / google_report). Same
    read gate as ``list_dashboards`` (``_user_may_read_dashboard``).
    """
    try:
        hits = get_all_documents(DASHBOARDS_INDEX)
        sources = []
        for hit in hits:
            source = hit.get("_source", {})
            if source.get("dashboard_type") != "echarts":
                continue
            charts = (source.get("echarts_config") or {}).get("charts") or []
            if isinstance(charts, dict):
                charts = list(charts.values())
            chart_types = {c.get("chart_type") for c in charts if isinstance(c, dict)}
            seedable = sorted(chart_types & SEEDABLE_CHART_TYPES)
            if not seedable:
                continue
            if not _user_may_read_dashboard(source):
                continue
            sources.append(
                {
                    "id": source.get("dashboard_id", hit.get("_id", "")),
                    "title": source.get("title", "Untitled"),
                    "chart_types": seedable,
                    "updated_at": (source.get("updated_at", "") or "")[:10],
                }
            )
        sources.sort(key=lambda d: d["updated_at"], reverse=True)
        return jsonify({"success": True, "sources": sources})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error listing seedable reports",
            log_detail="canvas_seed_sources failed",
            exc=e,
        )


@dashboard_bp.route("/api/canvas-seed/<dashboard_id>", methods=["GET"])
def canvas_seed(dashboard_id):
    """Convert an existing report dashboard into an unsaved canvas_config.

    Read authz mirrors ``load_dashboard`` (``_load_dashboard_for_read`` —
    404 unknown, 403 out-of-scope). The response carries the converted
    config plus a ``skipped`` list of elements that degraded to placeholders,
    so the editor can surface what was not converted.

    ``?theme=<key>`` seeds the page in a named built-in theme —
    the editor passes the page's current theme so a canvas created from the
    picker seeds report content in that same look. Unknown keys fall back
    to classic inside ``get_theme`` (never an error).
    """
    try:
        source = _load_dashboard_for_read(dashboard_id)
        if source.get("dashboard_type", "echarts") != "echarts":
            return (
                jsonify({"success": False, "error": "Only report dashboards can seed a canvas page."}),
                400,
            )
        # seed-time (option 2) — optional repeated ?team=<title>
        # params scope the seed: canva Summary sections with no data for
        # those teams (and no force-show override) are not seeded, so the
        # copy is born without sections the team does not use. No teams =>
        # seed everything (unchanged behavior). Client-supplied titles are
        # intersected with the caller's allowed set so a
        # scoped user cannot probe another tenant's data presence.
        visible_sections = None
        team_titles = [t for t in request.args.getlist("team") if t]
        if team_titles:
            try:
                from web.services.role_filter_service import RoleFilterService as _RFS
            except ImportError:
                from ...services.role_filter_service import RoleFilterService as _RFS
            allowed_teams = _RFS.get_allowed_teams_for_org_users()
            if allowed_teams is not None:
                team_titles = [t for t in team_titles if t in allowed_teams]
            from lib.es_helpers import get_elasticsearch_client

            es = get_elasticsearch_client()
            if es is not None and team_titles:
                charts = (source.get("echarts_config") or {}).get("charts") or []
                canva_chart = next((c for c in charts if isinstance(c, dict) and c.get("chart_type") == "canva"), None)
                if canva_chart is not None:
                    visible_sections = visible_canva_sections(es, team_titles, canva_chart)
        canvas_config, skipped = convert_dashboard_to_canvas(
            source, theme_ref=request.args.get("theme"), visible_sections=visible_sections
        )
        # COPY-on-seed provenance stamp — persisted by save_dashboard's
        # canvas branch, never read back for syncing.
        canvas_config["seeded_from_dashboard_id"] = source.get("dashboard_id", dashboard_id)
        return jsonify(
            {
                "success": True,
                "title": source.get("title", "Untitled"),
                "canvas_config": canvas_config,
                "skipped": skipped,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error seeding canvas page",
            log_detail=f"canvas_seed failed for dashboard_id '{dashboard_id}'",
            exc=e,
        )


@dashboard_bp.route("/api/canvas-icons", methods=["GET"])
def canvas_icons():
    """List the icon library (the existing SVG set under
    static/dashboard_builder/icons — the only icon store,).
    Icon objects reference these by bare filename (``icon.name``)."""
    return jsonify({"success": True, "icons": list_icon_library()})


@dashboard_bp.route("/api/canvas-themes", methods=["GET"])
def canvas_themes():
    """list the built-in theme library for the theme picker
    (the ``/api/canvas-icons`` pattern: a curated, code-defined set
    referenced by key). Returns key + name + preview swatch per theme;
    the full token sets ride the template injection, not this endpoint."""
    return jsonify({"success": True, "themes": canvas_theme.theme_summaries()})


# ── Canvas AI chat ────────────────────────────────────────────────
# Once-per-worker latch for the usage-index bootstrap (mapping put is
# idempotent; no need to re-run it on every chat turn).
_canvas_ai_usage_index_ready = False

# field catalog for the create-dashboard capability. Built from
# the SAME sources the builder UI binds against: _connected_data_views (the
# picker's filtered view list) + _data_view_fields (the picker's mapping
# merge). The bin/python/catalog/*.json files the ticket references cover
# only fb_ads today AND are themselves what GENERATES those ES mappings
# (catalog_loader/catalog_to_schema), so validating against the live
# mappings validates against the catalog transitively while also covering
# every connected non-FB data view. Cached ~10 min per worker — the catalog
# changes on deploys/reindexes, not per request. Only cached for
# unrestricted sessions: if the index-isolation hook ever returns scoped
# prefixes again, those requests bypass the shared cache.
_CANVAS_AI_CATALOG_TTL_SECONDS = 600
_canvas_ai_catalog_cache: dict = {"at": 0.0, "catalog": None}
_canvas_ai_catalog_lock = threading.Lock()


def _canvas_ai_field_catalog() -> dict | None:
    """``{"data_views": [{id, label, fields: [{name, type}]}]}`` for the AI
    proposal prompt + validator, or None when nothing is enumerable."""
    scoped = get_allowed_team_prefixes() is not None
    now = time.time()
    if not scoped:
        with _canvas_ai_catalog_lock:
            cached = _canvas_ai_catalog_cache["catalog"]
            if cached is not None and now - _canvas_ai_catalog_cache["at"] < _CANVAS_AI_CATALOG_TTL_SECONDS:
                return cached

    es = get_elasticsearch_client()
    if es is None:
        return None
    data_views = []
    for dv in _connected_data_views():
        try:
            fields = _data_view_fields(es, dv["id"])
        except Exception:
            # A single broken pattern (e.g. no matching index yet) must not
            # take the whole catalog down — the view is just not offered.
            logging.warning("Canvas AI catalog: skipping data view %s (fields unavailable)", dv["id"])
            continue
        if fields:
            data_views.append({"id": dv["id"], "label": dv.get("label") or dv["id"], "fields": fields})
    if not data_views:
        return None
    catalog = {"data_views": data_views}
    if not scoped:
        with _canvas_ai_catalog_lock:
            _canvas_ai_catalog_cache["at"] = now
            _canvas_ai_catalog_cache["catalog"] = catalog
    return catalog


# Endpoint for the canvas-editor AI assistant (spike, plan
# the private docs). shipped chat Q&A +
# attachments; added capability 1 — create-dashboard-from-
# attachment/description, returned as an UNSAVED validated proposal;
# adds capability 2 — alter the page open in the editor, returned
# as an UNSAVED merged config plus a changes diff the user must explicitly
# apply (the table-tile capability remains a follow-up ticket). Model tier
# is auto-routed server-side (no client-picked model —
# the only client knob is the opt-in complex_mode flag). auth.py's
# /dashboard-builder/ prefix rule enforces mc_dashboard_builder upstream;
# _require_builder_write re-checks in-route (this endpoint spends money, so
# it takes the write-tier gate, not the viewer exemption list).

# raw size cap on the client-sent dashboard-context JSON. The
# in-editor DOC can legitimately carry ~6MB of image data URIs (they are
# stripped before the model sees it); anything past this is not a real page.
_MAX_AI_DASHBOARD_CONTEXT_CHARS = 8_000_000


@dashboard_bp.route("/api/ai/chat", methods=["POST"])
def canvas_ai_chat():
    """Run one canvas AI chat turn.

    Accepts multipart/form-data (``message``, optional ``conversation_id``,
    optional ``complex_mode``, optional ``attachments`` files, optional
    ``dashboard`` JSON — the page open in the editor) or plain
    JSON with the same scalar fields. Attachments are validated and inlined as
    base64 content blocks server-side (Bedrock has no Files API); the
    conversation — including attachment bytes — is held server-side and
    referenced by ``conversation_id``, never round-tripped through the
    browser. Every turn is usage-logged under feature="canvas_ai" and
    refused with 429 once the daily budget caps are hit.

    the request is refused unless ALL three AI Assistant gates
    are open (workspace enabled + dashboard builder + ai_assistant role).
    This server-side check is the real gate — the template only hides the
    panel — and the refusal body carries a machine-readable ``reason`` so
    a hand-crafted request from a gated-out user gets a diagnosable 403.
    """
    _ai_ok, _ai_reason, _ai_status, _ai_message = _canvas_ai_gate()
    if not _ai_ok:
        return jsonify({"success": False, "error": _ai_message, "reason": _ai_reason}), _ai_status

    try:
        from web.services import canvas_ai_service
        from web.services.nlq_usage_service import ensure_usage_index, log_query
    except ImportError:
        from ...services import canvas_ai_service
        from ...services.nlq_usage_service import ensure_usage_index, log_query

    if request.content_type and request.content_type.startswith("multipart/"):
        message = (request.form.get("message") or "").strip()
        conversation_id = (request.form.get("conversation_id") or "").strip()
        complex_mode = (request.form.get("complex_mode") or "").lower() in ("1", "true", "on")
        uploads = request.files.getlist("attachments")
        dashboard_raw = request.form.get("dashboard") or ""
    else:
        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        conversation_id = (data.get("conversation_id") or "").strip()
        complex_mode = bool(data.get("complex_mode"))
        uploads = []
        dashboard_raw = data.get("dashboard") or ""

    if not message and not uploads:
        return jsonify({"success": False, "error": "Message is required"}), 400

    # the editor sends the OPEN page with each turn:
    # {"dashboard_id": <id or null>, "title": ..., "canvas_config": {...}}.
    # It powers the modify capability; absent means a context-less turn (the
    # modify tool is withdrawn), malformed is a client bug worth a 400.
    dashboard_config = None
    dashboard_id = None
    if dashboard_raw:
        if isinstance(dashboard_raw, str):
            if len(dashboard_raw) > _MAX_AI_DASHBOARD_CONTEXT_CHARS:
                return jsonify({"success": False, "error": "The page is too large for the AI to read."}), 400
            try:
                dashboard_raw = json.loads(dashboard_raw)
            except ValueError:
                return jsonify({"success": False, "error": "Invalid dashboard context."}), 400
        if not isinstance(dashboard_raw, dict) or not isinstance(dashboard_raw.get("canvas_config"), dict):
            return jsonify({"success": False, "error": "Invalid dashboard context."}), 400
        dashboard_config = dashboard_raw["canvas_config"]
        dashboard_id = (dashboard_raw.get("dashboard_id") or "").strip() or None

    # a live (actively published) page gets a warning
    # the client must confirm before applying AI changes. Soft lookup: the
    # doc may be unsaved (no id) and a lookup failure must not break chat.
    published_warning = False
    if dashboard_id:
        try:
            src = get_document_by_id(DASHBOARDS_INDEX, dashboard_id)
            if src is not None and _any_active_entry(src) is not None:
                published_warning = True
        except Exception:
            logging.warning("Canvas AI: publish-state lookup failed for dashboard '%s'", dashboard_id)

    username = session.get("username", "unknown")

    ok, budget_message = canvas_ai_service.check_budget(username)
    if not ok:
        return jsonify({"success": False, "error": budget_message}), 429

    attachment_blocks = []
    attachment_names = []
    import base64 as _b64

    for f in uploads:
        raw = f.read()
        filename = f.filename or "file"
        media_type = (f.mimetype or "").lower().split(";")[0].strip()
        # Browsers (esp. Windows/Excel installs) report .csv/.txt with vendor
        # MIME types — the extension is the reliable signal for the text
        # formats. Mirrors the client-side fallback in canvas_editor.js.
        if media_type not in ("text/plain", "text/csv") and re.search(r"\.(csv|txt)$", filename, re.IGNORECASE):
            media_type = "text/csv" if filename.lower().endswith(".csv") else "text/plain"
        try:
            if media_type in ("text/plain", "text/csv"):
                payload = raw.decode("utf-8", errors="replace")
            else:
                payload = _b64.b64encode(raw).decode("ascii")
            block = canvas_ai_service.build_attachment_block(filename, media_type, payload, len(raw))
        except canvas_ai_service.AttachmentError as e:
            return jsonify({"success": False, "error": e.public_message}), 400
        attachment_blocks.append(block)
        attachment_names.append(filename)

    # the field catalog powers the create-dashboard capability.
    # Build failure degrades the turn to Q&A-only (the service switches to
    # the no-catalog system prompt) rather than failing the chat.
    catalog = None
    try:
        catalog = _canvas_ai_field_catalog()
    except Exception:
        logging.exception("Canvas AI: field catalog build failed — create capability off this turn")

    result = canvas_ai_service.chat(
        username=username,
        message_text=message,
        attachments=attachment_blocks,
        conversation_id=conversation_id or None,
        complex_mode=complex_mode,
        catalog=catalog,
        dashboard_config=dashboard_config,
        icon_check=icon_library_has,
    )

    # convert a validated proposal into an UNSAVED canvas_config.
    # The AI path never writes ES: the config only persists if the user later
    # saves it through the normal save route. _validate_canvas_payload is the
    # same defense-in-depth gate that route applies, run here so a config the
    # editor could never save is refused before the user ever previews it.
    if result.get("success") and result.get("proposal"):
        proposal = result.pop("proposal")
        issues = result.pop("issues", [])
        # the open page's selected theme (page.theme in the
        # dashboard context the editor sends every turn) restyles the
        # generated config server-side; the model never sees or invents
        # colors. No context / no theme falls back to the classic fixed look.
        theme_ref = None
        if isinstance(dashboard_config, dict):
            page = dashboard_config.get("page")
            if isinstance(page, dict) and isinstance(page.get("theme"), str):
                theme_ref = page["theme"]
        try:
            canvas_config = build_canvas_from_proposal(proposal, theme_ref)
            err = _validate_canvas_payload({"canvas_config": canvas_config})
            if err:
                raise ValueError(f"generated canvas_config failed validation: {err}")
            tile_count = sum(len(s.get("tiles") or []) for s in proposal.get("sections") or [])
            result["proposal"] = {
                "title": proposal.get("title") or "AI Dashboard",
                "summary": proposal.get("summary") or "",
                "canvas_config": canvas_config,
                "section_count": len(proposal.get("sections") or []),
                "tile_count": tile_count,
                "issues": issues,
            }
        except Exception:
            logging.exception("Canvas AI: proposal conversion failed")
            result["issues"] = issues
            result["reply"] = (
                (result.get("reply") or "")
                + "\n\nI drafted a dashboard but it couldn't be converted into a canvas page — please try again."
            ).strip()
    # a merged modification takes the same defense-in-depth gate
    # the save route applies (_validate_canvas_payload), run here so a config
    # the editor could never save is refused before the user ever previews
    # it. Nothing persists from this path: applying is a client-side preview
    # and saving still goes through the normal user-confirmed save route.
    elif result.get("success") and result.get("modification"):
        modification = result.pop("modification")
        issues = result.pop("issues", [])
        try:
            err = _validate_canvas_payload({"canvas_config": modification.get("canvas_config")})
            if err:
                raise ValueError(f"merged canvas_config failed validation: {err}")
            modification["issues"] = issues
            modification["published_warning"] = published_warning
            result["modification"] = modification
        except Exception:
            logging.exception("Canvas AI: modification validation failed")
            result["issues"] = issues
            result["reply"] = (
                (result.get("reply") or "")
                + "\n\nI drafted a change but it couldn't be validated against the page — please try again."
            ).strip()
    # A tool turn whose every tile failed validation has no proposal but
    # still carries top-level ``issues`` — left in the response so the chat
    # panel renders the flagged fields under the reply (never a silent drop).

    usage = result.get("usage", {})
    if usage:
        try:
            global _canvas_ai_usage_index_ready
            if not _canvas_ai_usage_index_ready:
                ensure_usage_index()
                _canvas_ai_usage_index_ready = True
            log_query(
                user=username,
                query_text=message[:500],
                data_view="canvas_chat",
                usage=usage,
                success=result.get("success", False),
                error=result.get("error"),
                feature=canvas_ai_service.FEATURE,
                # workspace attribution resolves inside log_query
                # (resolve_billing_workspace) — best-effort, never blocking.
            )
        except Exception:
            logging.exception("Canvas AI: failed to log usage")

    if result.get("success"):
        result["attachments"] = attachment_names
        # refresh the panel's workspace-spend readout with the
        # turn just logged. None (key absent) leaves the footrow unchanged.
        spend = _canvas_ai_spend_payload()
        if spend:
            result["workspace_spend"] = spend
        return jsonify(result)
    return jsonify(result), 502 if "AI" in (result.get("error") or "") else 400


# ──────────────────────────────────────────────
# Data Views
# ──────────────────────────────────────────────


# Authoritative display labels for data views, keyed by index pattern (title).
# Used when Kibana data views lack a 'name' field — e.g. when created before
# the name field was added, or when create-data-views.sh skipped an update.
_DATA_VIEW_LABELS: dict[str, str] = {
    "*_echo_conversations": "Echo Conversations",
    "*_fb_insights": "Facebook Ad Insights",
    "*_fb_page_insights": "Facebook Page Insights",
    "*_ga_metrics": "Google Analytics",
    "*_ig_insights": "Instagram Insights",
    "*_google_ads": "Google Ads",
    "*_youtube_analytics": "YouTube Analytics",
    "*_tiktok_organic": "TikTok Organic",
    "*_chatwoot_conversations": "Chatwoot Conversations",
    "*_chatwoot_insights": "Chatwoot Insights",
    # legacy hyphen alias kept only until the alias + its data view
    # are deleted on prod; the by-team entry is the live one.
    "disciple-tools": "Disciple Tools",
    "*_disciple_tools": "Disciple Tools (by team)",
}


def _fetch_kibana_data_views(space_id="default"):
    """
    Fetch data views (index patterns) from a Kibana space.
    Returns a list of dicts with 'id' (ES index pattern/title) and 'label' (display name).
    Falls back to an empty list on any error.
    """
    # validate space_id before interpolating into a Kibana URL
    # so a path-traversal value can't redirect the request (CodeQL py/partial-ssrf).
    try:
        from web.services.kibana_service import _validated_space_id, kibana_auth, kibana_tls_verify
    except ImportError:
        from ...services.kibana_service import _validated_space_id, kibana_auth, kibana_tls_verify
    space_id = _validated_space_id(space_id)
    from asset_management.web.config import DEFAULT_KIBANA_URL

    kibana_url = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)

    if space_id and space_id != "default":
        url = f"{kibana_url}/s/{space_id}/api/data_views"
    else:
        url = f"{kibana_url}/api/data_views"

    # scoped service account / API key, falling back to elastic.
    headers = {"kbn-xsrf": "true", "Content-Type": "application/json"}
    auth, configured = kibana_auth(headers)
    if not configured:
        return []

    try:
        resp = http_requests.get(
            url,
            headers=headers,
            auth=auth,
            verify=kibana_tls_verify(),
            timeout=10,
        )
        if resp.status_code != 200:
            logging.warning(f"Kibana data views API returned {resp.status_code} for space {space_id}")
            return []
        raw = resp.json()
        views = []
        for dv in raw.get("data_view", []):
            # title = ES index pattern, name = human label (may be absent)
            es_pattern = dv.get("title", "")
            label = dv.get("name") or _DATA_VIEW_LABELS.get(es_pattern, "") or es_pattern
            if es_pattern:
                views.append({"id": es_pattern, "label": label})
        views.sort(key=lambda v: v["label"].lower())
        return views
    except Exception as e:
        logging.error(f"Error fetching Kibana data views: {e}")
        return []


def _without_switched_off_sources(data_views: list[dict]) -> list[dict]:
    """Drop the data views of connectors that are switched off.

    Today that is TikTok: while ``TIKTOK_ENABLED`` is off its data view is not a
    choice. create-data-views.sh may already have made it (it is git-defined and
    harmless to keep), but offering it would put an empty TikTok source in front
    of every builder, Import Data and NLQ user. Suffix match, so a concrete
    ``{team}__tiktok_organic`` index (the ES fallback) goes too, not only the
    ``*_tiktok_organic`` pattern.
    """
    from lib.tiktok_constants import SOURCE_SLUG as TIKTOK_SOURCE_SLUG, tiktok_enabled

    if tiktok_enabled():
        return data_views
    return [dv for dv in data_views if not dv["id"].endswith("_" + TIKTOK_SOURCE_SLUG)]


def _connected_data_views(space_id: str = "default") -> list[dict]:
    """The connected data views a builder user may bind to, fully filtered.

    Extracted from ``list_data_views`` so the AI field catalog is
    built from EXACTLY the set the picker offers — hidden/system views,
    dedup-window views, and index-isolation all apply identically.
    """
    data_views = _fetch_kibana_data_views(space_id)

    if not data_views:
        # Fallback: list non-system ES indices
        try:
            es = get_elasticsearch_client()
            if es:
                existing_ids = {dv["id"] for dv in data_views}
                indices = es.indices.get(index="*")
                for name in sorted(indices.keys()):
                    if not name.startswith(".") and name not in existing_ids:
                        data_views.append({"id": name, "label": name.replace("_", " ").title()})
        except Exception as e:
            logging.error(f"ES fallback index list failed: {e}")

    # Hide system/internal data views that aren't useful in the UI.
    # follow-up: de_dashboard_templates is a config index (dashboard
    # template docs), not an analytics source — it has no team_title and must
    # not be a selectable data view. Hide it here so the dropdown drops it even
    # if the Kibana data-view object still exists on a not-yet-reprovisioned env.
    _HIDDEN_PREFIXES = (".", "logs-", "metrics-", "de_asset_")
    _HIDDEN_EXACT = {"logs", "metrics", "de_dashboard_templates"}
    data_views = [
        dv for dv in data_views if dv["id"] not in _HIDDEN_EXACT and not any(dv["id"].startswith(p) for p in _HIDDEN_PREFIXES)
    ]

    # when dedup routing is on, hide the window/dedup data views
    # (e.g. *_fb_insights_window) from the builder. Users build on the single
    # canonical view (*_fb_insights) and routing fetches the dedup fields from
    # the window index transparently; the window view should no longer be a
    # selectable choice. Registry-driven (_is_window_data_view), so it covers
    # every current and future window suffix, not just FB. Flag OFF leaves the
    # window view visible — a flag-off deploy still has charts that legitimately
    # pin it, and removing it from the picker would orphan them.
    if _dedup_routing_enabled():
        data_views = [dv for dv in data_views if not _is_window_dv_registry(dv["id"])]

    data_views = _without_switched_off_sources(data_views)

    # Index-level isolation hook (no-op since org_admin was retired —
    # kept so a future scoped role can plug back in).
    prefixes = get_allowed_team_prefixes()
    if prefixes is not None:
        data_views = [dv for dv in data_views if validate_index_access(dv["id"])]

    return data_views


@dashboard_bp.route("/api/data-views")
def list_data_views():
    """Return data views from the Kibana Team Analytics space."""
    return jsonify({"success": True, "data_views": _connected_data_views(request.args.get("space", "default"))})


@dashboard_bp.route("/api/ga4-properties")
def list_ga4_properties():
    """GA4 properties for a dashboard's workspace, for the property-first
    selector on GA4 dashboards.

    A GA4 dedup figure (activeUsers/totalUsers) is only valid within ONE property
    : a chart spanning 2+ properties shows the labeled upper bound. The
    selector lets the user pick a single property so the dedup field collapses to
    one grain and shows a real number. This endpoint feeds that dropdown.

    Enumerated from ``de_asset_ga_properties`` (the ASSET index — NOT a
    property_id agg on the metrics index, which carries ~40 orphan single-doc
    property_ids that must never appear in the picker). Scoped to the
    ``?workspace_esid=`` of the dashboard; absent → empty (the selector has no
    workspace to enumerate, so it offers nothing rather than every tenant's
    properties). Returns ``{ga_property_id, name}`` — ``ga_property_id`` is the
    value the frontend injects as a ``property_id`` dashboard_filter (the metrics
    indices key on ``property_id`` with this same numeric id; confirmed bare
    keyword on staging).
    """
    workspace_esid = (request.args.get("workspace_esid") or "").strip()
    if not workspace_esid:
        return jsonify({"success": True, "properties": []})

    # fail-closed workspace gate. A scoped user must not enumerate
    # another tenant's properties by passing its esid. get_allowed_workspaces()
    # is None only for a role-less account or a failed lookup (privileged users
    # get their resolvable list); everyone is restricted to their
    # resolvable workspaces (mirrors the dashboard-list gate).
    try:
        allowed_ws = RoleFilterService.get_allowed_workspaces()
    except Exception:
        allowed_ws = None  # ES outage / bootstrap race — fail open (legacy contract)
    if allowed_ws is not None and workspace_esid not in set(allowed_ws):
        return jsonify({"success": True, "properties": []})

    try:
        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503
        # workspace_esid is now bare `keyword` in de_asset_ga_properties
        # after the asset-schema reindex (prod cutover 2026-07-07). A term
        # on the old `.keyword` sub-field matches NOTHING (the subfield no longer
        # exists) — the inverse-trap that hid this selector on every
        # workspace. Query the bare field. (Verified prod Kibana Dev Tools:
        # term workspace_esid.keyword "kavanah-media" -> 0 hits;
        # term workspace_esid "kavanah-media" -> 15.) Do NOT add a `.keyword`
        # fallback — the field is unambiguously bare keyword now, and a dual-shape
        # query would only mask the next such regression.
        resp = es.search(
            index="de_asset_ga_properties",
            body={
                "size": 500,
                "query": {"term": {"workspace_esid": workspace_esid}},
                "_source": ["ga_property_id", "ga_property_display_name", "ga_property_title"],
            },
            ignore_unavailable=True,
        )
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
        hits = (resp_dict.get("hits") or {}).get("hits") or []
        seen = set()
        properties = []
        for h in hits:
            src = h.get("_source") or {}
            pid = src.get("ga_property_id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            name = src.get("ga_property_display_name") or src.get("ga_property_title") or str(pid)
            properties.append({"ga_property_id": str(pid), "name": name})
        properties.sort(key=lambda p: p["name"].lower())
        return jsonify({"success": True, "properties": properties})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error listing GA4 properties",
            log_detail="list_ga4_properties failed",
            exc=e,
        )


def _simplify_es_type(es_type):
    if es_type == "date":
        return "date"
    if es_type == "text":
        return "text"
    if es_type in ("keyword", "constant_keyword", "wildcard"):
        return "keyword"
    if es_type in (
        "long",
        "integer",
        "short",
        "byte",
        "float",
        "double",
        "half_float",
        "scaled_float",
        "unsigned_long",
    ):
        return "numeric"
    return "keyword"


def _flatten_mapping(props, prefix=""):
    """Recursively flatten ES mapping properties into [{name, type}] list.

    Traverses both 'properties' (sub-objects) and 'fields' (ES multi-fields such
    as .keyword sub-fields) so that aggregatable keyword variants are visible in
    the field picker alongside their parent text fields.
    """
    fields = []
    for fname, fmeta in props.items():
        full_name = f"{prefix}.{fname}" if prefix else fname
        ftype = fmeta.get("type", "")
        # a never-populated dynamic object (schema `object` with no
        # sub-fields written yet, e.g. adset_promoted_object on most indices)
        # reports `"type": "object"`; _simplify_es_type's fallback labeled it
        # "(keyword)" and offering it in the picker silently yields zero
        # buckets. Hide the bare container — populated objects carry
        # `properties` (and no `type`) and keep flattening to their real
        # sub-fields below.
        if ftype and ftype != "object":
            fields.append({"name": full_name, "type": _simplify_es_type(ftype)})
        if "properties" in fmeta:
            fields.extend(_flatten_mapping(fmeta["properties"], full_name))
        # Also expose ES multi-fields (e.g. field.keyword, field.raw)
        if "fields" in fmeta:
            fields.extend(_flatten_mapping(fmeta["fields"], full_name))
    return fields


def _data_view_fields(es, data_view_id: str, stats: dict | None = None) -> list[dict]:
    """``[{name, type}]`` for a data view — the mapping-merge + dedup-field
    core of ``get_fields``, extracted so the AI field catalog and
    the picker endpoint can never disagree about what fields exist.

    ``stats`` (optional) is filled with ``index_count`` — how many
    concrete indices the scoped pattern matched — so the picker route can tell
    "no data imported yet" (0 indices) apart from "index exists but is bare".
    """
    # Layer-1 scope the mapping read: a foreign data_view resolves to
    # the no-match sentinel and yields no fields instead of another tenant's schema.
    #
    # a brand-new team's scoped pattern narrows to a concrete index
    # (``<team>__disciple_tools``) that does not exist until the first import
    # writes it. Without ``ignore_unavailable`` ES answers 404 and the picker
    # surfaced a 500 "Error fetching fields" to every new user who opened the
    # builder before their first import. An absent index is simply "no fields yet".
    from elasticsearch import NotFoundError

    scoped = RoleFilterService.scoped_index_pattern(data_view_id)
    try:
        mapping = es.indices.get_mapping(index=scoped, ignore_unavailable=True, allow_no_indices=True)
    except NotFoundError:
        mapping = {}
    mapping = dict(mapping or {})
    if stats is not None:
        stats["index_count"] = len(mapping)

    # deep-union properties across all matching indices. The
    # previous shallow first-index-wins merge donated the WHOLE subtree of
    # a dynamic object (e.g. adset_promoted_object) from whichever index
    # ES listed first; index order is not stable, so sub-fields like
    # adset_promoted_object.page_id flickered in and out of the picker per
    # request. A sub-field is now offered iff ANY matching index has it.
    merged_props = merge_index_mapping_properties(mapping)

    fields = _flatten_mapping(merged_props)

    # surface de-duplicated fields on the ONE daily data view so
    # users select e.g. insight_reach without ever choosing a window data view.
    # Some dedup fields (full_view_reach, recall metrics) live ONLY in the
    # window index mapping, so they're absent from the daily mapping above;
    # add any registry dedup field for this data view's source that isn't
    # already present. Numeric (all dedup metrics are numeric). Registry-driven
    # — no window-index mapping lookup needed.
    try:
        from web.services.dedup_routing import dedup_fields_for_data_view, window_dimension_fields_for_data_view
    except ImportError:
        from services.dedup_routing import dedup_fields_for_data_view, window_dimension_fields_for_data_view
    existing_names = {f["name"] for f in fields}
    for dedup_name in dedup_fields_for_data_view(data_view_id):
        if dedup_name not in existing_names:
            fields.append({"name": dedup_name, "type": "numeric"})
            existing_names.add(dedup_name)

    # surface window-only grain DIMENSIONS (demographics age/gender)
    # the same registry-driven way: they exist only on window docs, but a
    # group-by on them is served wholly from the window index by the routed
    # read path, so the picker (and the AI catalog built from this same list)
    # must offer them on the canonical daily view.
    for dim_name in window_dimension_fields_for_data_view(data_view_id):
        if dim_name not in existing_names:
            fields.append({"name": dim_name, "type": "keyword"})
            existing_names.add(dim_name)

    fields.sort(key=lambda f: f["name"])
    return fields


@dashboard_bp.route("/api/data-views/<path:data_view_id>/fields")
def get_fields(data_view_id):
    """Return field names and simplified types from an ES index mapping.
    Supports wildcard index patterns — merges properties from all matching indices.
    """
    _require_index_access(data_view_id)
    try:
        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        stats: dict = {}
        fields = _data_view_fields(es, data_view_id, stats)
        # annotate fields the editor should treat as
        # asset-backed so the value-picker can switch to label/value
        # pairs from the asset index. Lookup is keyed on the requested
        # ``data_view_id`` (the wildcard pattern as the user wrote it),
        # not on each indices' real name. Pure additive — existing
        # callers that don't know about ``asset_type`` ignore it.
        for f in fields:
            asset_type = _asset_type_for(data_view_id, f["name"])
            if asset_type:
                f["asset_type"] = asset_type
            # annotate registered ratio fields so the editor can
            # lock the aggregation dropdown to "ratio" and surface a
            # "Computed as: sum(num) / sum(denom)" helper. Backend ignores
            # the user's stored aggregation for these fields anyway, but
            # the frontend lock keeps the contract visible to chart authors.
            if is_ratio_field(f["name"]):
                spec = RATIO_FIELDS[f["name"]]
                f["is_ratio"] = True
                f["ratio_numerator"] = spec["numerator"]
                f["ratio_denominator"] = spec["denominator"]
        payload: dict = {"success": True, "fields": fields}
        if stats.get("index_count", 1) == 0:
            # nothing has been imported for this source yet (no index
            # behind the scoped pattern). A success with an explicit reason lets
            # the editor say so instead of showing a generic error.
            payload["no_data_yet"] = True
            payload["message"] = (
                "No data has been imported for this source yet. Fields appear after the "
                "first import runs — check Import Logs for its schedule."
            )
        return jsonify(payload)
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error fetching fields",
            log_detail=f"get_fields: failed for data_view_id '{data_view_id}'",
            exc=e,
        )


@dashboard_bp.route("/api/data-views/<path:data_view_id>/latest-window")
def get_latest_window(data_view_id):
    """(66.2) — return the MOST RECENTLY PULLED window for a window data view.

    Path: ``/dashboard-builder/api/data-views/<data_view_id>/latest-window``

    The Ad Sets table reads ``*_fb_insights_window`` docs, each representing one
    pulled ``[insight_window_start, insight_window_end]`` range (not a scannable
    daily series). The viewer (66.3) calls this on dashboard open to auto-set the
    date picker to the latest pulled window, so the time filter always matches a
    window that has data. When no window doc exists, the viewer shows the
    "enter a date range" prompt (66.4).

    Returns ``{success: true, window: {start, end, label}}`` for the doc with the
    greatest ``@timestamp`` (most recently pulled), or ``{success: true,
    window: null}`` when the (scoped) data view has no docs. Scope is applied via
    RoleFilterService.scoped_index_pattern — a user only ever sees their own
    team's windows.
    """
    _require_index_access(data_view_id)
    try:
        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        # Top hit by @timestamp desc = the most recently pulled doc; its window
        # bounds are the range to open on. size=1, _source-limited to the two
        # window fields. ignore_unavailable so a never-pulled team (no index)
        # returns cleanly rather than 404.
        body = {
            "size": 1,
            "sort": [{"@timestamp": {"order": "desc"}}],
            "_source": ["insight_window_start", "insight_window_end", "@timestamp"],
            "query": {"exists": {"field": "insight_window_start"}},
        }
        result = es.search(index=RoleFilterService.scoped_index_pattern(data_view_id), body=body, ignore_unavailable=True)
        hits = (result.get("hits") or {}).get("hits") or []
        if not hits:
            return jsonify({"success": True, "window": None})

        src = hits[0].get("_source") or {}
        start = _normalize_week_value(src.get("insight_window_start"))
        end = _normalize_week_value(src.get("insight_window_end"))
        if not start or not end:
            return jsonify({"success": True, "window": None})

        # (#3) — also return when this window was pulled (@timestamp of
        # the most-recent doc) so the viewer can show "Data pulled as of: <ts> —
        # window <label>". The generic data-pulled endpoint reads a different
        # (scope-rewritten) index pattern and came back blank for the windowed
        # Ad Sets dashboard; sourcing the pulled-at time from the SAME scoped
        # window lookup the viewer already calls on open keeps the two in sync.
        return jsonify(
            {
                "success": True,
                "window": {
                    "start": start,
                    "end": end,
                    "label": _format_week_label(start, end),
                    "pulled_at": src.get("@timestamp"),
                },
            }
        )
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error fetching latest window",
            log_detail=f"get_latest_window: failed for data_view_id '{data_view_id}'",
            exc=e,
        )


@dashboard_bp.route("/api/data-views/<path:data_view_id>/window-exists")
def window_exists(data_view_id):
    """does an EXACT-bounds dedup window already exist for a range?

    Refresh's snap-to-window check (decision 2026-06-29): the read path already
    auto-snaps to the NEAREST pulled window, so Refresh only needs to RE-PULL
    when the user's exact range isn't already on file. This endpoint answers
    "is there a window doc whose [insight_window_start, insight_window_end]
    EXACTLY equals [?from, ?to] (optionally for ?property_id)?" so the button can
    skip a redundant GA4/FB pull and just reload (snap) instead.

    EXACT match only (not "covers"): a wider window would report a figure for a
    different period than the user asked for. Scope via scoped_index_pattern so a
    user only ever sees their own team's windows. ``?property_id`` narrows to one
    GA4 property (the selector's grain) — without it, any team window matches.

    Returns ``{success, exists: bool, pulled_at: <ts|null>}``.
    """
    _require_index_access(data_view_id)
    try:
        date_from = (request.args.get("from") or "").strip()
        date_to = (request.args.get("to") or "").strip()
        property_id = (request.args.get("property_id") or "").strip()
        if not date_from or not date_to:
            return jsonify({"success": False, "error": "from and to are required"}), 400

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        # EXACT-bounds match on the window fields. property_id is a bare keyword
        # on the metrics indices, so a plain term is correct
        # here (unlike workspace_esid which is text-mapped).
        filters = [
            {"term": {"insight_window_start": date_from}},
            {"term": {"insight_window_end": date_to}},
        ]
        if property_id:
            filters.append({"term": {"property_id": property_id}})

        body = {
            "size": 1,
            "sort": [{"@timestamp": {"order": "desc"}}],
            "_source": ["@timestamp"],
            "query": {"bool": {"filter": filters}},
        }
        result = es.search(
            index=RoleFilterService.scoped_index_pattern(data_view_id),
            body=body,
            ignore_unavailable=True,
        )
        hits = (result.get("hits") or {}).get("hits") or []
        if not hits:
            return jsonify({"success": True, "exists": False, "pulled_at": None})
        return jsonify({"success": True, "exists": True, "pulled_at": (hits[0].get("_source") or {}).get("@timestamp")})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error checking window existence",
            log_detail=f"window_exists: failed for data_view_id '{data_view_id}'",
            exc=e,
        )


def _format_week_label(start: str, end: str) -> str:
    """Format a (start, end) pair as "Apr 26, 2026 - May 2, 2026".

    Both inputs are ISO YYYY-MM-DD strings. Returns the raw inputs joined
    by " - " if parsing fails so a malformed date doesn't break the
    response — callers see the raw dates and can investigate.
    """
    from datetime import datetime

    try:
        s = datetime.strptime(start, "%Y-%m-%d").strftime("%b %d, %Y")
        e = datetime.strptime(end, "%Y-%m-%d").strftime("%b %d, %Y")
        return f"{s} - {e}"
    except (ValueError, TypeError):
        return f"{start} - {end}"


def _normalize_week_value(value) -> str:
    """Normalise a composite-agg date bucket key to ISO yyyy-MM-dd.

    Bucket keys for ES ``date`` fields come back as epoch_millis (int)
    when the agg source has no ``format`` set. We can't always set
    ``format`` on the source (some ES versions reject it on terms
    composite sources, returning 500), so the safe path is to accept
    either shape here:

      - int / numeric string  → assume epoch_millis, convert to ISO.
      - "yyyy-MM-dd" string   → pass through unchanged.

    Returns "" for None / unparseable input so the caller can skip
    the bucket cleanly.
    """
    if value is None:
        return ""
    # Already a date string?
    if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-":
        return value
    try:
        millis = int(value)
    except (ValueError, TypeError):
        return ""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# distinct-values endpoint backing the dashboard-filter value
# drop-downs. In-process cache keyed by (data_view, field, asset_type,
# team_filter_joined, org_filter). 60-second TTL is short enough that
# newly-imported data shows up promptly without hammering ES on every
# drop-down open. Cap is 200 server-side regardless of the requested
# ``size`` so a tampered request can't ask for the full cardinality of
# a high-card field.
#
# Cache value shape: list[{label, value}]. Keyword filters get
# label === value; asset-backed filters get the friendly title as
# label and the underlying ID as value.
_FIELD_VALUES_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_FIELD_VALUES_TTL_SECONDS = 60
_FIELD_VALUES_HARD_CAP = 200

# the windowed FB index (`*_fb_insights_window`) stores, per ad set,
# one doc per real publisher_platform PLUS a combined cross-platform dedup doc
# stamped publisher_platform="all". That "all" doc is an INTERNAL aggregation
# helper (it carries FB's deduplicated reach but, by design, NO additive metrics
# — spend/impressions/clicks were stripped so an all-platforms SUM over the real
# per-platform docs isn't double-counted). It must NEVER appear as a selectable
# value in the Platform filter dropdown: a user picking it filters the table to
# that lone doc, so reach shows but every additive column reads 0. "All
# platforms" is expressed by selecting NOTHING (empty filter) — which sums the
# per-platform docs and max-reads the combined doc for reach. Hide the sentinel.
_WINDOW_COMBINED_PLATFORM = "all"
_WINDOW_PLATFORM_FIELD = "publisher_platform"


def _is_window_data_view(data_view_id: str) -> bool:
    """True for the *_fb_insights_window data view (combined-platform sentinel lives here)."""
    return "fb_insights_window" in (data_view_id or "")


# asset-backed value source for dashboard filters.
#
# Some chart-data fields (e.g. ``ad_account_id`` on ``*fb_insights``) carry
# raw IDs that match an entry in an asset index. Users want the filter
# drop-down to display friendly titles ("Example Agency 4") rather than raw
# IDs ("1234567890123456") while the filter clause still goes on the
# underlying ID column.
#
# Two hardcoded maps drive the feature:
#
#   _ASSET_BACKED_FIELDS — (data_view, field) → asset_type
#       The editor uses this to flip the value-picker into asset-backed
#       mode when the user picks one of these fields. The /fields
#       endpoint annotates each matching field item with ``asset_type``
#       so the JS doesn't need a parallel hardcoded copy.
#
#   _ASSET_TYPE_REGISTRY — asset_type → {index, label_field, value_field}
#       The values endpoint reads `_source[label_field]` and
#       `_source[value_field]` to build {label, value} pairs.
#
# Adding a new asset type requires entries in BOTH maps.
_ASSET_BACKED_FIELDS: dict[tuple[str, str], str] = {
    # Kibana's registered data-view title is ``*_fb_insights`` (with the
    # leading underscore) — verified against the live /api/data_views
    # response. Keeping the canonical title here, not the wildcard the
    # ES query happens to accept.
    ("*_fb_insights", "ad_account_id"): "fb_ad_accounts",
}

_ASSET_TYPE_REGISTRY: dict[str, dict[str, str]] = {
    "fb_ad_accounts": {
        "index": "de_asset_fb_ad_accounts",
        "label_field": "fb_ad_account_title",
        "value_field": "fb_ad_account_id",
    },
}

_ASSET_VALUES_CACHE: dict[tuple[str, tuple[str, ...]], tuple[float, list[dict[str, str]]]] = {}


def _asset_type_for(data_view: str, field: str) -> str | None:
    """Return the asset_type for a (data_view, field) pair, or None.

    Pure dict lookup; no ES round-trip.
    """
    return _ASSET_BACKED_FIELDS.get((data_view, field))


def _enrich_values_with_asset_titles(es, asset_type, raw_ids):
    """Map raw IDs to ``[{label, value}]`` pairs using the registered asset
    index. IDs without a matching asset doc fall back to ``label === value``
    so they don't disappear from the popover.
    """
    registry = _ASSET_TYPE_REGISTRY.get(asset_type)
    if not registry or not raw_ids:
        return [{"label": v, "value": v} for v in raw_ids]
    try:
        body = {
            "size": len(raw_ids),
            "_source": [registry["label_field"], registry["value_field"]],
            "query": {"terms": {registry["value_field"]: list(raw_ids)}},
        }
        resp = es.search(index=registry["index"], body=body)
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
        hits = resp_dict.get("hits", {}).get("hits", [])
        id_to_title = {}
        for hit in hits:
            src = hit.get("_source", {}) or {}
            v = src.get(registry["value_field"])
            label = src.get(registry["label_field"])
            if v is not None and label is not None:
                id_to_title[str(v)] = str(label)
        return [{"label": id_to_title.get(v, v), "value": v} for v in raw_ids]
    except Exception:
        # Asset enrichment is best-effort — if the asset index lookup
        # fails for any reason, fall back to raw labels so the popover
        # still works (just shows IDs).
        return [{"label": v, "value": v} for v in raw_ids]


def _agg_search_with_keyword_fallback(es, data_view_id, body, field):
    """Run a terms-agg search; on a ``Fielddata is disabled`` error, retry
    once with ``<field>.keyword`` and return that response instead.

    Background: earlier ``*_fb_insights[_weekly]`` indices
    on prod had no ``publisher_platform`` field. When first
    breakdown-doc landed, ES dynamic-mapping defaulted those indices'
    ``publisher_platform`` to ``text`` with a ``.keyword`` subfield —
    not ``keyword`` direct as the schema declares. Terms aggregations
    against the bare ``text`` field error with ``Fielddata is disabled``.

    The schema fix is to reindex every affected index. Until
    that ships, this fallback keeps the popover endpoint usable on mixed
    -mapping wildcard data views by transparently retrying against the
    multi-field's ``.keyword`` subfield. No-op on indices already
    mapped as ``keyword`` direct (no retry, no extra round-trip on the
    happy path) and no-op on fields the caller already qualified with
    ``.keyword`` (avoids ``foo.keyword.keyword`` infinite suffixing).
    """
    try:
        return es.search(
            index=RoleFilterService.scoped_index_pattern(data_view_id),
            body=body,
            ignore_unavailable=True,
        )
    except Exception as exc:
        msg = str(exc)
        if "Fielddata is disabled" not in msg:
            raise
        if field.endswith(".keyword"):
            raise
        fallback_field = f"{field}.keyword"
        fallback_terms = {**body["aggs"]["values"]["terms"], "field": fallback_field}
        fallback_body = {**body, "aggs": {"values": {"terms": fallback_terms}}}
        return es.search(
            index=RoleFilterService.scoped_index_pattern(data_view_id),
            body=fallback_body,
            ignore_unavailable=True,
        )


@dashboard_bp.route("/api/data-views/<path:data_view_id>/field/<path:field>/values")
def get_field_values(data_view_id, field):
    """Return the top-N distinct alphabetical ``[{label, value}]`` pairs for
    *field* in *data_view_id*.

    Used by the dashboard-level filter value drop-downs.

    Optional query params:
      - ``asset_type``  — when set + in registry, enrich each raw ID with
        its friendly title from the corresponding asset index. Unmatched
        IDs keep ``label === value``.
      - ``team_filter`` — multi. Narrows the terms-agg via a ``team_title``
        match clause so the popover lists only values that exist in the
        currently-filtered chart data. Mirrors the filtering the chart
        queries already apply.
      - ``org_filter``  — single. Same idea on ``organization_title``.

    Returns ``{success, values: [{label, value}], cached}``. The cache
    key includes the optional params so changing the topbar filter
    invalidates cleanly.
    """
    _require_index_access(data_view_id)
    try:
        asset_type = (request.args.get("asset_type") or "").strip() or None
        team_filters = [t for t in request.args.getlist("team_filter") if t]
        org_filter = (request.args.get("org_filter") or "").strip() or None
        # optional viewer time range (date math or ISO).
        date_from = (request.args.get("from") or "").strip() or None
        date_to = (request.args.get("to") or "").strip() or None

        cache_key = (
            data_view_id,
            field,
            asset_type or "",
            ",".join(sorted(team_filters)),
            org_filter or "",
            date_from or "",
            date_to or "",
        )
        now = time.monotonic()
        cached = _FIELD_VALUES_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _FIELD_VALUES_TTL_SECONDS:
            return jsonify({"success": True, "values": cached[1], "cached": True})

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        # Pull more than the hard cap from ES so we can sort alphabetically
        # locally without losing values that ES would have dropped from a
        # smaller term-count-ordered terms agg. ES caps terms.size at
        # search.max_buckets; the modest 1000 here stays well under that.
        # Phase 24: narrow the agg by topbar team/org filters so
        # the popover lists only values present in the same data the
        # charts are showing.
        narrowing_clauses = []
        if team_filters:
            if len(team_filters) == 1:
                narrowing_clauses.append({"match_phrase": {"team_title": team_filters[0]}})
            else:
                narrowing_clauses.append(
                    {
                        "bool": {
                            "should": [{"match_phrase": {"team_title": t}} for t in team_filters],
                            "minimum_should_match": 1,
                        }
                    }
                )
        if org_filter and not team_filters:
            # earlier: when team filters are present they're the
            # tighter scope; organization_title may not exist on every
            # data view (e.g. Disciple Tools), so adding it as an AND
            # would zero-out matches that the team filter alone would
            # have hit. Mirrors view.html's _withExtraFilters logic.
            narrowing_clauses.append({"match_phrase": {"organization_title": org_filter}})

        # narrow the value list to the viewer's time range so
        # the popover matches what the charts show. Guarded on the DV's
        # mapping actually carrying a date-typed kibana_date: Disciple
        # Tools has none, and a range on a missing field would zero the
        # list instead of degrading to the unfiltered behavior.
        if date_from or date_to:
            range_clause = _date_range_clause(date_from, date_to)
            if range_clause and _get_field_types(es, data_view_id).get("kibana_date") == "date":
                narrowing_clauses.append({"range": {"kibana_date": range_clause}})

        query_clause = {"bool": {"filter": narrowing_clauses}} if narrowing_clauses else {"match_all": {}}

        body = {
            "size": 0,
            "query": query_clause,
            "aggs": {
                "values": {
                    "terms": {
                        "field": field,
                        "size": 1000,
                        "order": {"_count": "desc"},
                    }
                }
            },
        }
        try:
            resp = _agg_search_with_keyword_fallback(es, data_view_id, body, field)
        except Exception as es_err:
            from lib.errors import safe_error_response

            return safe_error_response(
                "Error fetching field values",
                log_detail=f"get_field_values: ES query failed for {data_view_id}/{field}",
                exc=es_err,
                body={"values": []},
            )

        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
        buckets = resp_dict.get("aggregations", {}).get("values", {}).get("buckets", [])
        # Cast to str so non-string keys (numeric, bool) survive JSON. Skip empty.
        raw_values = [str(b.get("key")) for b in buckets if b.get("key") not in (None, "")]
        # never offer the combined-platform sentinel ("all") as a
        # selectable Platform value on the windowed index: picking it filters the
        # table to the additive-stripped combined doc (reach only, every other
        # column 0). "All platforms" = select nothing. Scoped to the exact
        # (window DV, publisher_platform) pair so no other field/index is touched.
        if _is_window_data_view(data_view_id) and field == _WINDOW_PLATFORM_FIELD:
            raw_values = [v for v in raw_values if v != _WINDOW_COMBINED_PLATFORM]
        # Alphabetical, case-insensitive, dedup-after-cast.
        unique_sorted = sorted(set(raw_values), key=lambda v: v.lower())
        capped = unique_sorted[:_FIELD_VALUES_HARD_CAP]

        # Phase 24: when an asset_type is supplied AND in the
        # registry, enrich each ID with the friendly title from the
        # asset index and return ``[{label, value}]``. Unmatched IDs
        # keep ``label === value`` so they don't disappear (e.g. legacy
        # ``act_*``-prefixed values that don't have asset docs).
        #
        # Without asset_type, return ``[str]`` to preserve the
        # endpoint contract (existing tests + JS path expect bare
        # strings). The JS synthesizes label === value upstream when it
        # needs the uniform shape.
        if asset_type:
            payload = _enrich_values_with_asset_titles(es, asset_type, capped)
            payload.sort(key=lambda p: p["label"].lower())
        else:
            payload = capped

        _FIELD_VALUES_CACHE[cache_key] = (now, payload)
        return jsonify({"success": True, "values": payload, "cached": False})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error fetching field values",
            log_detail=f"get_field_values: failed for {data_view_id}/{field}",
            exc=e,
            body={"values": []},
        )


@dashboard_bp.route("/api/asset-types/<asset_type>/values")
def get_asset_type_values(asset_type):
    """Return the top-N {label, value} pairs for an asset-backed filter.

    the dashboard-filter editor calls this when a user picks a
    field flagged as asset-backed (e.g. ad_account_id on *fb_insights).
    The ``label`` is what the popover and strip button display; the
    ``value`` is what travels in the saved selection and the ES filter
    clause. Same 200-cap + 60s cache pattern as
    ``/api/data-views/<dv>/field/<field>/values``.

    Auth: only the global ``before_request`` authentication check applies.
    The viewer also calls this so non-admin users can see asset titles
    for filters the dashboard owner configured. The ``asset_type``
    is whitelisted by the registry so a tampered request can't read
    arbitrary indices. the values are narrowed to the caller's
    workspaces — an ad-account list names another tenant's accounts.
    """
    registry = _ASSET_TYPE_REGISTRY.get(asset_type)
    if not registry:
        return jsonify({"success": False, "error": "Unknown asset_type"}), 404

    # the asset index holds every tenant's docs, so the list is
    # narrowed to the caller's workspaces — the same workspace_esid allow-list
    # the Asset Explorer applies to these types. ``get_allowed_workspaces`` is
    # None only for a user with no resolvable scope (``should_apply_filter``
    # False — privileged users always get a list, possibly empty), so None,
    # an empty list, or a lookup error all return no values.
    try:
        allowed_workspaces = (
            RoleFilterService.get_allowed_workspaces() if RoleFilterService.current_user_has_any_scope() else None
        )
    except Exception:  # noqa: BLE001 — tenant boundary, fail closed
        logging.warning("get_asset_type_values scope lookup failed for %s", asset_type)
        allowed_workspaces = None
    if not allowed_workspaces:
        return jsonify({"success": True, "values": [], "cached": False})
    workspace_esids = sorted(set(allowed_workspaces))

    try:
        # Keyed by the caller's workspace set: a process-wide entry per
        # asset_type served one tenant's list to every other tenant.
        cache_key = (asset_type, tuple(workspace_esids))
        now = time.monotonic()
        cached = _ASSET_VALUES_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _FIELD_VALUES_TTL_SECONDS:
            return jsonify({"success": True, "values": cached[1], "cached": True})

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        index = registry["index"]
        label_field = registry["label_field"]
        value_field = registry["value_field"]

        # Pull more than the hard cap so the local sort + dedup doesn't
        # drop entries ES would have shown under a smaller terms agg.
        # Asset indices are small in practice (tens of docs), so this
        # is a one-shot search rather than a paginated agg.
        body = {
            "size": 1000,
            "_source": [label_field, value_field],
            "query": {"bool": {"filter": [{"terms": {"workspace_esid": workspace_esids}}]}},
        }
        try:
            resp = es.search(index=index, body=body)
        except Exception as es_err:
            from lib.errors import safe_error_response

            return safe_error_response(
                "Error fetching asset values",
                log_detail=f"get_asset_type_values: ES query failed for {asset_type}",
                exc=es_err,
                body={"values": []},
            )

        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
        hits = resp_dict.get("hits", {}).get("hits", [])
        # Build {label, value} pairs, dropping docs missing either field.
        pairs = []
        seen_values: set[str] = set()
        for hit in hits:
            src = hit.get("_source", {}) or {}
            label = src.get(label_field)
            value = src.get(value_field)
            if label is None or value is None or value == "":
                continue
            value_str = str(value)
            if value_str in seen_values:
                continue
            seen_values.add(value_str)
            pairs.append({"label": str(label), "value": value_str})

        # Alphabetical case-insensitive by label (matches the
        # /values endpoint's sort semantics so the UI looks consistent).
        pairs.sort(key=lambda p: p["label"].lower())
        capped = pairs[:_FIELD_VALUES_HARD_CAP]

        _ASSET_VALUES_CACHE[cache_key] = (now, capped)
        return jsonify({"success": True, "values": capped, "cached": False})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error fetching asset values",
            log_detail=f"get_asset_type_values: failed for {asset_type}",
            exc=e,
            body={"values": []},
        )


# ──────────────────────────────────────────────
# Leaflet Map Data
# ──────────────────────────────────────────────

# Statistics config keyed by stat key → data_source, field, filter type
_MAP_STATISTICS = {
    "app_installs": {
        "data_source": "fb_insights",
        "field": "insight_actions_mobile_app_install",
        "type": "range",
        "gte": 1,
    },
    "google_ads_app_installs": {
        "data_source": "google_ads",
        "field": "app_installs",
        "type": "range",
        "gte": 1,
    },
    # dt_date_field: field used for time-range filtering on DT stats.
    # Now uses _ms (epoch millis) fields which are native ES date type.
    "baptisms": {
        "data_source": "dt",
        "field": "meta.baptism_date",
        "type": "exists",
        "dt_date_field": "date_created_ms",
    },
    "dt_contacts": {
        "data_source": "dt",
        "field": None,
        "dt_date_field": "meta.first_contact_date_ms",
    },
    "echo_multiplier": {
        "data_source": "echo",
        "field": "multiplier",
        "type": "range",
        "gte": 1,
        "min_country_value": 2,
    },
    "messages": {
        "data_source": "echo",
        "field": "incomingMessagesCount",
        "type": "range",
        "gte": 1,
    },
    "multiplier_contacts": {
        "data_source": "dt",
        "field": "meta.baptism_generation",
        "type": "exists",
        "dt_date_field": "date_created_ms",
    },
    "page_visits": {
        "data_source": "fb_insights",
        "field": "insight_actions_page_engagement",
        "type": "range",
        "gte": 1,
    },
    "reach": {
        "data_source": "fb_insights",
        "field": "insight_reach",
        "type": "range",
        "gte": 1,
    },
    "salvation": {
        "data_source": "dt",
        "field": "meta.faith_status.keyword",
        "type": "term",
        "value": "believer",
        "address_only": True,
        "dt_date_field": "date_created_ms",
    },
    "spiritual_conversations": {
        "data_source": "dt",
        "field": "meta.quick_button_contact_established",
        "type": "exists",
        "dt_date_field": "date_created_ms",
    },
    "video_views": {
        "data_source": "fb_insights",
        "field": "insight_actions_video_view_total",
        "type": "range",
        "gte": 1,
    },
    # New statistics matching the Summary dashboard hierarchy
    "messaging_contacts": {
        "data_source": "echo",
        "field": None,
        "cardinality_field": "client_id",
    },
    "messages_sent": {
        "data_source": "echo",
        "field": "outboundMessagesCount",
        "type": "range",
        "gte": 1,
    },
    "spiritual_convos_echo": {
        "data_source": "echo",
        "field": "spiritual_conversation",
        "type": "range",
        "gte": 1,
    },
    "first_meetings": {
        "data_source": "dt",
        "field": "meta.seeker_path.keyword",
        "type": "term",
        "value": "met",
        "dt_date_field": "meta.first_contact_date_ms",
    },
    "new_groups": {
        "data_source": "dt",
        # unified schema maps post_type as bare keyword;.keyword
        # counted 0 on every schema-mapped *_disciple_tools index.
        "field": "post_type",
        "type": "term",
        "value": "groups",
        "dt_date_field": "meta.start_date_ms",
    },
    "new_churches": {
        "data_source": "dt",
        "field": "meta.church_start_date",
        "type": "exists",
        "dt_date_field": "meta.church_start_date_ms",
    },
    # Organic reach
    # page_impressions_unique started silently 500-ing on Meta v23
    # (2026-05). Replacement is page_total_media_view_unique (Meta's
    # rename for the same "unique viewers" metric). Both fields exist on
    # docs going forward; this site reads the new one.
    "organic_facebook": {
        "data_source": "fb_page_insights",
        "field": "page_total_media_view_unique",
        "type": "range",
        "gte": 1,
    },
    "organic_instagram": {
        "data_source": "ig_insights",
        "field": "ig_insight_reach",
        "type": "range",
        "gte": 1,
    },
    "organic_web": {
        "data_source": "ga_metrics",
        "field": "totalUsers",
        "type": "range",
        "gte": 1,
    },
    # Video engagement
    "videos_15s": {
        "data_source": "fb_page_insights",
        "field": "page_video_views_15s_total",
        "type": "range",
        "gte": 1,
    },
    "view_time": {
        "data_source": "fb_page_insights",
        "field": "page_video_avg_sec_watched",
        "type": "range",
        "gte": 1,
    },
    # Follows / subscriptions
    "follows_facebook": {
        "data_source": "fb_page_insights",
        "field": "page_follows",
        "type": "range",
        "gte": 1,
    },
    "follows_instagram": {
        "data_source": "ig_insights",
        "field": "ig_user_followers_count",
        "type": "range",
        "gte": 1,
    },
    "follows_youtube": {
        "data_source": "youtube",
        "field": "subscribers_gained",
        "type": "range",
        "gte": 1,
    },
    # Reactions / shares
    "fb_reactions": {
        "data_source": "fb_page_insights",
        "field": "page_actions_post_reactions_total",
        "type": "range",
        "gte": 1,
    },
    "fb_shares": {
        "data_source": "fb_page_insights",
        "field": "page_posts_shares_total",
        "type": "range",
        "gte": 1,
    },
    "ig_reactions": {
        "data_source": "ig_insights",
        "field": "ig_media_total_likes",
        "type": "range",
        "gte": 1,
    },
    "ig_shares": {
        "data_source": "ig_insights",
        "field": "ig_media_total_shares",
        "type": "range",
        "gte": 1,
    },
    "yt_reactions": {
        "data_source": "youtube",
        "field": "likes",
        "type": "range",
        "gte": 1,
    },
    "yt_shares": {
        "data_source": "youtube",
        "field": "shares",
        "type": "range",
        "gte": 1,
    },
    "comments": {
        "data_source": "youtube",
        "field": "comments",
        "type": "range",
        "gte": 1,
    },
}


def _resolve_date_math(expr, upper_bound=False, unix_ts=False):
    """Convert an ES date math expression (e.g. 'now-30d/d', 'now') to a plain
    date string (or Unix epoch string) suitable for lexicographic comparison
    against keyword fields.  Returns the original string unchanged if it does
    not look like date math.

    unix_ts=True  → returns str(epoch_seconds) for fields like meta.church_start_date
    unix_ts=False → returns 'yyyy-MM-dd' for fields like date_created / meta.first_contact_date
    """
    if not expr:
        return expr
    now = datetime.utcnow()
    if expr.lower() == "now":
        # Upper bound: add one day so 'lte' includes all records from the current day
        d = now + timedelta(days=1) if upper_bound else now
    else:
        m = re.match(r"now-(\d+)([dMy])(?:/[dMy])?$", expr, re.IGNORECASE)
        if not m:
            # Plain ISO date string (e.g. '2026-01-01') — convert to unix epoch when needed
            if unix_ts:
                try:
                    d = datetime.strptime(expr, "%Y-%m-%d")
                    if upper_bound:
                        d += timedelta(days=1)
                    return str(int(d.replace(tzinfo=timezone.utc).timestamp()))
                except (ValueError, TypeError):
                    pass
            return expr  # Already a plain value — pass through unchanged
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit == "d":
            d = now - timedelta(days=n)
        elif unit == "m":
            d = now - timedelta(days=n * 30)
        elif unit == "y":
            d = now - timedelta(days=n * 365)
        else:
            return expr
    return str(int(d.timestamp())) if unix_ts else d.strftime("%Y-%m-%d")


def _dt_date_filter(date_from, date_to, stat_cfg):
    """Build a date filter clause for Disciple Tools map queries.

    Uses the dt_date_field from the stat config. Fields ending in _ms are
    ES date type (epoch_millis format) — ES resolves date math natively.
    Legacy keyword fields still use string comparison via _resolve_date_math.
    """
    dt_field = stat_cfg.get("dt_date_field", "date_created_ms")
    if dt_field.endswith("_ms"):
        # ES date type — pass date math and ISO dates directly
        return _build_date_filter(date_from or None, date_to or None, dt_field)
    is_unix = stat_cfg.get("dt_date_unix", False)
    resolved_from = _resolve_date_math(date_from, upper_bound=False, unix_ts=is_unix) if date_from else None
    resolved_to = _resolve_date_math(date_to, upper_bound=True, unix_ts=is_unix) if date_to else None
    return _build_date_filter(resolved_from, resolved_to, dt_field)


def _build_date_filter(date_from, date_to, field):
    """Return an ES range clause for the given date field, or None if no dates provided."""
    if not date_from and not date_to:
        return None
    rng = {}
    if date_from:
        rng["gte"] = date_from
    if date_to:
        rng["lte"] = date_to
    return {"range": {field: rng}}


def _build_stat_filter(stat_cfg):
    """Return an ES filter clause for the statistic field, or None for count-all."""
    if not stat_cfg.get("field"):
        return None
    if stat_cfg.get("type") == "exists":
        return {"exists": {"field": stat_cfg["field"]}}
    if stat_cfg.get("type") == "phrase":
        return {"match_phrase": {stat_cfg["field"]: stat_cfg["value"]}}
    if stat_cfg.get("type") == "term":
        return {"term": {stat_cfg["field"]: stat_cfg["value"]}}
    return {"range": {stat_cfg["field"]: {"gte": stat_cfg["gte"]}}}


def _org_team_filters(org, teams):
    """Return ES filter clauses for org and team scoping.

    teams may be a single string or a list of team names.
    A single team produces a match_phrase clause; multiple teams produce a
    bool-should (OR) clause so any matching team passes the filter.

    When teams are provided, the org filter is skipped because the team filter
    already scopes the data and some data views (e.g. Disciple Tools) don't
    have an organization_title field — adding it as an AND clause would return
    0 hits.
    """
    clauses = []
    if isinstance(teams, str):
        teams = [teams] if teams else []
    if org and not teams:
        clauses.append({"match_phrase": {"organization_title": org}})
    if len(teams) == 1:
        clauses.append({"match_phrase": {"team_title": teams[0]}})
    elif len(teams) > 1:
        clauses.append(
            {
                "bool": {
                    "should": [{"match_phrase": {"team_title": t}} for t in teams],
                    "minimum_should_match": 1,
                }
            }
        )
    return clauses


@dashboard_bp.route("/api/map-data", methods=["GET"])
def get_map_data():
    """Return geographic data points for the Leaflet map dashboard.

    Query params:
        statistic  — key from _MAP_STATISTICS (default: 'reach')
        org        — organization_title filter (optional)
        team       — team_title filter; may be repeated for OR filtering (optional)
        date_from  — ISO date string e.g. '2024-01-01' (optional)
        date_to    — ISO date string e.g. '2024-12-31' (optional)

    Returns:
        { success: true, cities: [...], regions: [...], countries: [...] }

    For ``fb_insights`` the cities array is intentionally empty here — FB
    cities load via ``/api/map-data/cities`` so that geocoding latency
    never blocks the country render. Other data sources keep
    their inline cities.
    """
    try:
        stat_key = request.args.get("statistic", "reach")
        org = request.args.get("org", "")
        teams = request.args.getlist("team")
        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")

        # scoped users must not be able to probe map data for
        # teams they have no role for. Mirrors the guard on
        # /api/dashboards/platform-availability — client-supplied
        # ``team`` / ``org`` values outside the session's allowed set are
        # silently dropped, and the request is evaluated against the
        # user's own scope. Admins / unrestricted users have
        # ``allowed_teams is None`` and keep the existing behaviour
        # (free choice of team via the dropdown).
        allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
        if allowed_teams is not None:
            if teams:
                teams = [t for t in teams if t in allowed_teams]
            if not teams:
                teams = list(allowed_teams)
            # Once teams are locked to the allowed set, ``org`` would only
            # serve to re-expand scope via ``_get_team_titles_for_org`` —
            # drop it.
            org = ""

        stat_cfg = _MAP_STATISTICS.get(stat_key)
        if not stat_cfg:
            return (
                jsonify({"success": False, "error": f"Unknown statistic: {stat_key}"}),
                400,
            )

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        data_source = stat_cfg["data_source"]
        cities = []
        regions = []
        countries = []

        if data_source == "fb_insights":
            # FB cities load via /api/map-data/cities; the country
            # render must not block on geocoding.
            regions = _query_fb_region_map(es, stat_cfg, org, teams, date_from, date_to)
            countries = _query_fb_country_map(es, stat_cfg, org, teams, date_from, date_to)
        elif data_source == "echo":
            cities = _query_echo_map(es, stat_cfg, org, teams, date_from, date_to)
            countries = _query_echo_country_map(es, stat_cfg, org, teams, date_from, date_to)
        elif data_source == "dt":
            cities = _query_dt_map(es, stat_cfg, org, teams, date_from, date_to)
            countries = _query_dt_country_map(es, stat_cfg, org, teams, date_from, date_to)
        elif data_source == "google_ads":
            if stat_cfg.get("field") == "app_installs":
                countries = _query_google_ads_installs_geo_map(es, stat_cfg, org, teams, date_from, date_to)
            else:
                countries = _query_team_country_map_generic(es, "*google_ads", stat_cfg, org, teams, date_from, date_to)
        elif data_source == "fb_page_insights":
            countries = _query_team_country_map_generic(es, "*fb_page_insights", stat_cfg, org, teams, date_from, date_to)
        elif data_source == "ig_insights":
            countries = _query_team_country_map_generic(es, "*ig_insights", stat_cfg, org, teams, date_from, date_to)
        elif data_source == "youtube":
            countries = _query_youtube_country_map(es, stat_cfg, org, teams, date_from, date_to)
            if not countries:
                # country_breakdown records lack per-country data for this metric (e.g. likes,
                # shares, comments are not returned by the YouTube Analytics API per country).
                # Fall back to team_title → country lookup (from de_asset_teams). This is more
                # reliable than team_country since the field mapping is inconsistent across
                # youtube_analytics indices (some use keyword, others use text+keyword).
                countries = _query_team_title_country_map(es, "*youtube_analytics", stat_cfg, org, teams, date_from, date_to)
        elif data_source == "ga_metrics":
            countries = _query_ga4_country_map(es, stat_cfg, org, teams, date_from, date_to)
        else:
            return (
                jsonify({"success": False, "error": f"Unknown data source: {data_source}"}),
                400,
            )

        return jsonify(
            {
                "success": True,
                "cities": cities,
                "regions": regions,
                "countries": countries,
            }
        )

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error fetching map data",
            log_detail="get_map_data failed",
            exc=e,
        )


@dashboard_bp.route("/api/map-data/cities", methods=["GET"])
def get_map_city_data():
    """Return city markers for the Leaflet map dashboard.

    Cache-only: cities are resolved exclusively from the geocode cache
    populated by the import / prewarm path and the ``location_cities``
    source field. No live Nominatim calls are made on this path, so a
    cold cache or a slow geocoder cannot stall the response. Whatever
    is resolved comes back; everything else is silently omitted.

    Currently only ``fb_insights`` returns cities here — other data
    sources keep their inline cities on ``/api/map-data``. The endpoint
    short-circuits to an empty payload for them so the frontend can
    call it unconditionally.

    Query params: same as ``/api/map-data``.

    Returns:
        { success: true, cities: [...], total_cities: N, resolved_cities: M }
    """
    try:
        stat_key = request.args.get("statistic", "reach")
        org = request.args.get("org", "")
        teams = request.args.getlist("team")
        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")

        # same scope guard as ``get_map_data``. The two endpoints
        # share the ``_org_team_filters(org, teams)`` helper, so they need
        # symmetric guards or a hand-crafted request to the cities
        # sub-route would still leak.
        allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
        if allowed_teams is not None:
            if teams:
                teams = [t for t in teams if t in allowed_teams]
            if not teams:
                teams = list(allowed_teams)
            org = ""

        stat_cfg = _MAP_STATISTICS.get(stat_key)
        if not stat_cfg:
            return (
                jsonify({"success": False, "error": f"Unknown statistic: {stat_key}"}),
                400,
            )

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        if stat_cfg["data_source"] != "fb_insights":
            return jsonify(
                {
                    "success": True,
                    "cities": [],
                    "total_cities": 0,
                    "resolved_cities": 0,
                }
            )

        cities, total_cities, resolved_cities = _query_fb_map_cached_only(es, stat_cfg, org, teams, date_from, date_to)
        return jsonify(
            {
                "success": True,
                "cities": cities,
                "total_cities": total_cities,
                "resolved_cities": resolved_cities,
            }
        )

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error fetching map city data",
            log_detail="get_map_city_data failed",
            exc=e,
        )


def _query_google_ads_country_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate Google Ads geographic performance (geo documents) by country.

    Queries *_google_ads indices for documents where record_type='geo', which are
    produced by the geographic_view GAQL query in get_campaign_data.py.  Each such
    document represents one campaign × country × day, with app_installs already
    computed and geo_country_name / geo_country_code resolved from geo_target_constant.

    Returns a list of country-level points compatible with the Leaflet choropleth renderer.
    """
    # record_type may be auto-mapped as text — use match instead of term for robustness
    filters = [{"match": {"record_type": "geo"}}]
    date_filter = _build_date_filter(date_from, date_to, "date")
    if date_filter:
        filters.append(date_filter)
    filters.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        filters.append(stat_filter)

    query = {"bool": {"filter": filters}}

    # Try .keyword sub-fields first (auto-mapped text), then plain fields (explicit keyword)
    for name_field in ("geo_country_name.keyword", "geo_country_name"):
        agg_body = {
            "size": 0,
            "query": query,
            "aggs": {
                "by_country": {
                    "terms": {"field": name_field, "size": 300},
                    "aggs": {
                        "total": {"sum": {"field": stat_cfg["field"]}},
                        "iso2_hit": {
                            "top_hits": {
                                "size": 1,
                                "_source": {"includes": ["geo_country_code"]},
                            }
                        },
                    },
                }
            },
        }

        try:
            resp = es.search(
                index=RoleFilterService.scoped_index_pattern("*google_ads"),
                body=agg_body,
                ignore_unavailable=True,
            )
            resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
            buckets = resp_dict.get("aggregations", {}).get("by_country", {}).get("buckets", [])
            if not buckets:
                continue
            points = []
            for bucket in buckets:
                country_name = bucket["key"]
                if not country_name:
                    continue
                value = bucket.get("total", {}).get("value") or 0
                if not value:
                    continue
                iso2_hits = bucket.get("iso2_hit", {}).get("hits", {}).get("hits", [])
                iso2 = None
                if iso2_hits:
                    raw = iso2_hits[0].get("_source", {}).get("geo_country_code", "")
                    if raw:
                        iso2 = raw.upper()
                points.append(
                    {
                        "name": country_name,
                        "iso2": iso2,
                        "lat": None,
                        "lon": None,
                        "value": value,
                    }
                )
            if points:
                return points
        except Exception as e:
            logging.warning(f"google_ads country map query ({name_field}) failed: {e}")

    return []


def _query_fb_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate Facebook insights by city — returns geo points with stat value.

    Uses adset_targeting.geo_locations.cities.name as the primary city source
    (authoritative list from Facebook targeting), with country codes from the
    same field.  Coordinates come from the geocoded location_cities array when
    available; cities missing from location_cities are geocoded on-the-fly.
    """
    filters = []
    date_filter = _build_date_filter(date_from, date_to, "kibana_date")
    if date_filter:
        filters.append(date_filter)
    filters.extend(_org_team_filters(org, teams))

    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        filters.append(stat_filter)

    query = {"bool": {"filter": filters}} if filters else {"match_all": {}}

    stat_field = stat_cfg.get("field") or "insight_reach"

    # Aggregate by the targeting city name — this has ALL cities including those
    # where location_cities geocoding failed at import time.
    agg_body = {
        "size": 0,
        "query": query,
        "aggs": {
            "cities": {
                "terms": {
                    "field": "adset_targeting.geo_locations.cities.name.keyword",
                    "size": 500,
                },
                "aggs": {
                    "sample": {
                        "top_hits": {
                            "size": 3,
                            "_source": [
                                "location_cities",
                                "adset_targeting.geo_locations.cities",
                            ],
                        }
                    },
                    "stat": {"sum": {"field": stat_field}},
                },
            },
        },
    }

    resp = es.search(
        index=RoleFilterService.scoped_index_pattern("*fb_insights"),
        body=agg_body,
        ignore_unavailable=True,
    )
    resp_dict = resp.body if hasattr(resp, "body") else dict(resp)

    from lib.geocoding import country_from_coords, geocode_city

    points = []
    for bucket in resp_dict.get("aggregations", {}).get("cities", {}).get("buckets", []):
        city_name = bucket["key"]
        hits = bucket.get("sample", {}).get("hits", {}).get("hits", [])
        if not hits:
            continue

        # Find the country and region FB targeted this city in. country
        # filters the Nominatim search; region disambiguates homonyms (e.g.
        # Katra in Jammu vs Bihar) and matches the cache key the FB import
        # wrote at ingest time.
        country = ""
        region = ""
        for hit in hits:
            targeting_cities = hit.get("_source", {}).get("adset_targeting", {}).get("geo_locations", {}).get("cities", [])
            for tc in targeting_cities:
                if tc.get("name") == city_name:
                    if not country and tc.get("country"):
                        country = tc["country"]
                    if not region and tc.get("region"):
                        region = tc["region"]
            if country and region:
                break

        # Prefer the shared geocode cache. Try the region-qualified key
        # first (disambiguates homonyms like Katra in Jammu vs Bihar); fall
        # back to the no-region key when FB's region string doesn't match
        # Nominatim's hierarchy (e.g. "Punjab region", "Ankara Province"
        # which Nominatim doesn't recognise).
        lat, lon = None, None
        coords = geocode_city(city_name, region, country) if country else None
        if not coords and region and country:
            coords = geocode_city(city_name, "", country)
        if coords:
            lat, lon = coords["lat"], coords["lon"]

        # Fallback to whatever location_cities stored at import time.
        if lat is None:
            for hit in hits:
                city_list = hit.get("_source", {}).get("location_cities", [])
                city_obj = next((c for c in city_list if c.get("name") == city_name), None)
                if city_obj:
                    c = city_obj.get("coordinates") or {}
                    if c.get("lat") is not None and c.get("lon") is not None:
                        lat, lon = c["lat"], c["lon"]
                        break

        # Drop the point if still no coords — better a missing dot than one
        # parked at the country centroid, which looks like real data but isn't.
        if lat is None or lon is None:
            continue

        value = bucket["stat"]["value"] or bucket["doc_count"]
        points.append(
            {
                "name": city_name,
                "value": value,
                "country": country or country_from_coords(lat, lon),
                "lat": lat,
                "lon": lon,
            }
        )
    return points


def _query_fb_map_cached_only(es, stat_cfg, org, teams, date_from, date_to):
    """Cache-only counterpart to ``_query_fb_map`` used by the progressive
    map render.

    Same ES aggregation as ``_query_fb_map`` but coords are resolved only
    from the geocode cache + ``location_cities`` source field — no live
    Nominatim calls on the request path. Cities that can't be resolved
    are silently omitted; the caller gets a count of total vs resolved
    buckets so the UI can surface "N of M cities shown".

    Returns ``(points, total_cities, resolved_cities)``.
    """
    from lib.geocoding import (
        country_from_coords,
        lookup_cached_city,
        reload_cache_if_changed,
    )

    # Pick up entries the prewarm/import wrote since this worker started.
    # The cache is per-Python-process and gunicorn workers can otherwise
    # silently drop cities purely because they're holding a stale snapshot.
    reload_cache_if_changed()

    filters = []
    date_filter = _build_date_filter(date_from, date_to, "kibana_date")
    if date_filter:
        filters.append(date_filter)
    filters.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        filters.append(stat_filter)

    query = {"bool": {"filter": filters}} if filters else {"match_all": {}}
    stat_field = stat_cfg.get("field") or "insight_reach"

    agg_body = {
        "size": 0,
        "query": query,
        "aggs": {
            "cities": {
                "terms": {
                    "field": "adset_targeting.geo_locations.cities.name.keyword",
                    "size": 500,
                },
                "aggs": {
                    "sample": {
                        "top_hits": {
                            "size": 3,
                            "_source": [
                                "location_cities",
                                "adset_targeting.geo_locations.cities",
                            ],
                        }
                    },
                    "stat": {"sum": {"field": stat_field}},
                },
            },
        },
    }

    resp = es.search(
        index=RoleFilterService.scoped_index_pattern("*fb_insights"),
        body=agg_body,
        ignore_unavailable=True,
    )
    resp_dict = resp.body if hasattr(resp, "body") else dict(resp)

    points = []
    buckets = resp_dict.get("aggregations", {}).get("cities", {}).get("buckets", [])
    total_cities = 0
    for bucket in buckets:
        city_name = bucket["key"]
        hits = bucket.get("sample", {}).get("hits", {}).get("hits", [])
        if not hits:
            continue
        total_cities += 1

        country = ""
        region = ""
        for hit in hits:
            targeting_cities = hit.get("_source", {}).get("adset_targeting", {}).get("geo_locations", {}).get("cities", [])
            for tc in targeting_cities:
                if tc.get("name") == city_name:
                    if not country and tc.get("country"):
                        country = tc["country"]
                    if not region and tc.get("region"):
                        region = tc["region"]
            if country and region:
                break

        # Cache-only resolution. Try region-qualified key first
        # (disambiguates homonyms like Katra in Jammu vs Bihar) then the
        # no-region key — same precedence the cache writer uses.
        lat, lon = None, None
        coords = lookup_cached_city(city_name, region, country) if country else None
        if not coords and region and country:
            coords = lookup_cached_city(city_name, "", country)
        if coords:
            lat, lon = coords["lat"], coords["lon"]

        # Fallback to whatever location_cities stored at import time.
        if lat is None:
            for hit in hits:
                city_list = hit.get("_source", {}).get("location_cities", [])
                city_obj = next((c for c in city_list if c.get("name") == city_name), None)
                if city_obj:
                    c = city_obj.get("coordinates") or {}
                    if c.get("lat") is not None and c.get("lon") is not None:
                        lat, lon = c["lat"], c["lon"]
                        break

        if lat is None or lon is None:
            continue

        value = bucket["stat"]["value"] or bucket["doc_count"]
        points.append(
            {
                "name": city_name,
                "value": value,
                "country": country or country_from_coords(lat, lon),
                "lat": lat,
                "lon": lon,
            }
        )
    return points, total_cities, len(points)


def _query_fb_region_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate Facebook insights by location_regions.name (populated at import time).

    Regions come from adset_targeting.geo_locations.regions and are geocoded into
    location_regions during FB import (see geocode_all_targeting_levels in
    lib/geocoding.py).  The front-end resolves each region name to an admin1
    polygon via admin1.geojson; unmatched names fall back to a circle marker
    at the lat/lon returned here.
    """
    filters = []
    date_filter = _build_date_filter(date_from, date_to, "kibana_date")
    if date_filter:
        filters.append(date_filter)
    filters.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        filters.append(stat_filter)

    query = {"bool": {"filter": filters}} if filters else {"match_all": {}}
    stat_field = stat_cfg.get("field") or "insight_reach"

    # Try .keyword sub-field first (auto-mapped text), then plain field (explicit keyword).
    for name_field in ("location_regions.name.keyword", "location_regions.name"):
        try:
            resp = es.search(
                index=RoleFilterService.scoped_index_pattern("*fb_insights"),
                body={
                    "size": 0,
                    "query": query,
                    "aggs": {
                        "regions": {
                            "terms": {"field": name_field, "size": 500},
                            "aggs": {
                                "stat": {"sum": {"field": stat_field}},
                                "sample": {"top_hits": {"size": 1, "_source": ["location_regions"]}},
                            },
                        },
                    },
                },
                ignore_unavailable=True,
            )
            resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
            buckets = resp_dict.get("aggregations", {}).get("regions", {}).get("buckets", [])
            if not buckets:
                continue
            points = []
            for bucket in buckets:
                name = bucket["key"]
                if not name:
                    continue
                value = bucket["stat"]["value"] or bucket["doc_count"]
                lat = None
                lon = None
                hits = bucket.get("sample", {}).get("hits", {}).get("hits", [])
                if hits:
                    regions_arr = hits[0].get("_source", {}).get("location_regions") or []
                    match = next((r for r in regions_arr if r.get("name") == name), None)
                    if match:
                        coords = match.get("coordinates") or {}
                        lat = coords.get("lat")
                        lon = coords.get("lon")
                points.append({"name": name, "lat": lat, "lon": lon, "value": value})
            if points:
                return points
        except Exception as e:
            logging.warning(f"fb_insights region map query ({name_field}) failed: {e}")
            continue

    return []


def _query_fb_country_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate Facebook insights by country, unioning two sources.

    location_countries.iso2 (populated by import-time geocoding) covers
    campaigns that targeted at country level. team_country (set on every
    FB record from the team config) fills in countries that only have
    city-level targeting but no explicit country in the campaign — e.g.
    Turkey, Lebanon, Bangladesh in the current data.

    Both queries run; results are unioned, keyed by ISO2.  When the same
    country appears in both, location_countries wins (it's keyed off ad
    targeting, not the team's country tag, and is closer to FB's own
    audience reporting).
    """
    filters = []
    date_filter = _build_date_filter(date_from, date_to, "kibana_date")
    if date_filter:
        filters.append(date_filter)
    filters.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        filters.append(stat_filter)

    query = {"bool": {"filter": filters}} if filters else {"match_all": {}}
    stat_field = stat_cfg.get("field") or "insight_reach"

    from lib.geocoding import _COUNTRY_CENTROIDS

    # Primary aggregation: location_countries.iso2 — try .keyword sub-field
    # first (auto-mapped text), then plain field (explicit keyword mapping).
    primary_buckets = []
    for iso2_field in ("location_countries.iso2.keyword", "location_countries.iso2"):
        try:
            resp = es.search(
                index=RoleFilterService.scoped_index_pattern("*fb_insights"),
                body={
                    "size": 0,
                    "query": query,
                    "aggs": {
                        "countries": {
                            "terms": {"field": iso2_field, "size": 300},
                            "aggs": {"stat": {"sum": {"field": stat_field}}},
                        },
                    },
                },
                ignore_unavailable=True,
            )
            resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
            primary_buckets = resp_dict.get("aggregations", {}).get("countries", {}).get("buckets", [])
            if primary_buckets:
                break
        except Exception:
            continue

    points_by_iso2 = {}
    for bucket in primary_buckets:
        iso2 = (bucket.get("key") or "").upper()
        if not iso2 or iso2.lower() == "unknown":
            continue
        value = bucket["stat"]["value"] or bucket["doc_count"]
        centroid = _COUNTRY_CENTROIDS.get(iso2, {})
        points_by_iso2[iso2] = {
            "name": iso2,
            "iso2": iso2,
            "lat": centroid.get("lat"),
            "lon": centroid.get("lon"),
            "value": value,
        }

    # Supplement with team_country: catches countries with city-only
    # targeting (TR, LB, BD, …) that never populated location_countries.
    try:
        resp = es.search(
            index=RoleFilterService.scoped_index_pattern("*fb_insights"),
            body={
                "size": 0,
                "query": query,
                "aggs": {
                    "countries": {
                        "terms": {"field": "team_country", "size": 300},
                        "aggs": {"stat": {"sum": {"field": stat_field}}},
                    },
                },
            },
            ignore_unavailable=True,
        )
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
        for bucket in resp_dict.get("aggregations", {}).get("countries", {}).get("buckets", []):
            name = bucket.get("key") or ""
            if not name or name.lower() == "unknown":
                continue
            # Only ISO2-shaped entries are reliable; full names like "United
            # States" or stray values like "Cairo" are skipped to avoid
            # double-counting a country that's already keyed by ISO2 above.
            if len(name) != 2:
                continue
            iso2 = name.upper()
            if iso2 in points_by_iso2:
                continue
            value = bucket["stat"]["value"] or bucket["doc_count"]
            centroid = _COUNTRY_CENTROIDS.get(iso2, {})
            points_by_iso2[iso2] = {
                "name": iso2,
                "iso2": iso2,
                "lat": centroid.get("lat"),
                "lon": centroid.get("lon"),
                "value": value,
            }
    except Exception:
        pass

    return list(points_by_iso2.values())


def _echo_date_filter(date_from, date_to):
    """Build a kibana_date range filter for echo_conversations.

    earlier this had to convert ISO bounds to epoch-milliseconds because
    Echo's kibana_date was stored with format ``MMMM dd, yyyy HH:mm:ss 'UTC'``
    and ES rejected bare ISO strings against it. earlier every
    ``*_echo_conversations`` index is reindexed with ``kibana_date`` as
    ``yyyy-MM-dd`` (matching every other platform), so the standard
    ``_build_date_filter`` works directly.

    Kept as a thin wrapper instead of inlining the call sites so callers
    keep their semantic intent (``_echo_date_filter`` documents *why* this
    field needs a range clause), and so a future Echo-specific filter
    addition has an obvious home.
    """
    return _build_date_filter(date_from, date_to, "kibana_date")


def _query_echo_country_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate Echo conversations by contact_country_iso2 — returns country-level points.

    Prefers the contact_country_iso2 field (derived from the contact's own
    country) over team_country (the team's home country).  Falls back to
    team_country when contact_country_iso2 data is not yet available.
    """
    must = []
    date_filter = _echo_date_filter(date_from, date_to)
    if date_filter:
        must.append(date_filter)
    must.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        must.append(stat_filter)

    query = {"bool": {"must": must}} if must else {"match_all": {}}
    cardinality_field = stat_cfg.get("cardinality_field")

    if cardinality_field:
        # Try aggregating by contact_country_iso2 directly with cardinality sub-agg.
        for country_field in ("contact_country_iso2.keyword", "contact_country_iso2"):
            try:
                resp = es.search(
                    index=RoleFilterService.scoped_index_pattern("*echo_conversations,-unknown_echo_conversations"),
                    body={
                        "size": 0,
                        "query": query,
                        "aggs": {
                            "by_country": {
                                "terms": {"field": country_field, "size": 300},
                                "aggs": {"total": {"cardinality": {"field": cardinality_field}}},
                            }
                        },
                    },
                    ignore_unavailable=True,
                )
                rd = resp.body if hasattr(resp, "body") else dict(resp)
                buckets = rd.get("aggregations", {}).get("by_country", {}).get("buckets", [])
                if buckets:
                    points = []
                    for bucket in buckets:
                        iso2 = bucket["key"].upper()
                        value = bucket.get("total", {}).get("value", 0)
                        if value and iso2 and iso2.lower() not in ("unknown", ""):
                            points.append({"name": iso2, "iso2": iso2, "lat": None, "lon": None, "value": value})
                    if points:
                        return points
            except Exception as e:
                logging.warning(f"echo cardinality country map query ({country_field}) failed: {e}")

        # Fallback: aggregate by team_title, map team → country via asset lookup
        team_country_map = _load_dt_team_country_map(es)
        if not team_country_map:
            return []
        resp_dict = None
        for team_field in ("team_title.keyword", "team_title"):
            try:
                resp = es.search(
                    index=RoleFilterService.scoped_index_pattern("*echo_conversations,-unknown_echo_conversations"),
                    body={
                        "size": 0,
                        "query": query,
                        "aggs": {
                            "by_team": {
                                "terms": {"field": team_field, "size": 200},
                                "aggs": {"total": {"cardinality": {"field": cardinality_field}}},
                            }
                        },
                    },
                    ignore_unavailable=True,
                )
                rd = resp.body if hasattr(resp, "body") else dict(resp)
                if rd.get("aggregations", {}).get("by_team", {}).get("buckets"):
                    resp_dict = rd
                    break
            except Exception as e:
                logging.warning(f"echo cardinality team map query ({team_field}) failed: {e}")
        if not resp_dict:
            return []
        country_totals = {}
        for bucket in resp_dict.get("aggregations", {}).get("by_team", {}).get("buckets", []):
            team = bucket["key"]
            value = bucket.get("total", {}).get("value", 0)
            country = team_country_map.get(team)
            if country and value:
                country_totals[country] = country_totals.get(country, 0) + value
        return [{"name": c, "iso2": c, "lat": None, "lon": None, "value": v} for c, v in country_totals.items() if v > 0]

    if stat_cfg.get("field"):
        total_agg = {"sum": {"field": stat_cfg["field"]}}
    else:
        total_agg = None

    # Try contact_country_iso2 first (actual contact location), fall back to team_country
    for field in ("contact_country_iso2.keyword", "contact_country_iso2", "team_country.keyword", "team_country"):
        is_iso2_field = field.startswith("contact_country_iso2")
        sub_aggs = {}
        if not is_iso2_field:
            sub_aggs["iso3_hit"] = {
                "top_hits": {
                    "size": 1,
                    "_source": {"includes": ["team_country_iso3_code"]},
                }
            }
        if total_agg:
            sub_aggs["total"] = total_agg
        agg_body = {
            "size": 0,
            "query": query,
            "aggs": {
                "by_country": {
                    "terms": {"field": field, "size": 300},
                    "aggs": sub_aggs if sub_aggs else None,
                }
            },
        }
        if not sub_aggs:
            agg_body["aggs"]["by_country"].pop("aggs", None)
        try:
            resp = es.search(
                index=RoleFilterService.scoped_index_pattern("*echo_conversations,-unknown_echo_conversations"),
                body=agg_body,
                ignore_unavailable=True,
            )
            resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
            buckets = resp_dict.get("aggregations", {}).get("by_country", {}).get("buckets", [])
            if buckets:
                points = []
                for bucket in buckets:
                    country_key = bucket["key"]
                    if not country_key or country_key.lower() in ("unknown", ""):
                        continue
                    value = bucket.get("total", {}).get("value") or bucket["doc_count"]
                    min_val = stat_cfg.get("min_country_value")
                    if min_val and value < min_val:
                        continue
                    if is_iso2_field:
                        iso2 = country_key.upper()
                        iso3 = None
                        name = iso2
                    else:
                        iso3_hits = bucket.get("iso3_hit", {}).get("hits", {}).get("hits", [])
                        iso2 = None
                        iso3 = None
                        name = country_key
                        if iso3_hits:
                            raw = iso3_hits[0].get("_source", {}).get("team_country_iso3_code", "")
                            if raw and raw.lower() not in ("unknown", ""):
                                raw = raw.upper()
                                if len(raw) == 2:
                                    iso2 = raw
                                else:
                                    iso3 = raw
                    points.append(
                        {
                            "name": name,
                            "iso3": iso3,
                            "iso2": iso2,
                            "lat": None,
                            "lon": None,
                            "value": value,
                        }
                    )
                return points
        except Exception as e:
            logging.warning(f"echo country ({field}) query failed: {e}")

    return []


def _query_google_ads_installs_geo_map(es, stat_cfg, org, teams, date_from, date_to):
    """Query Google Ads geo documents for per-country install breakdown.

    Geo documents (record_type='geo') store the geographic_view breakdown from
    the Google Ads API — each document represents installs in a specific country
    for a specific campaign. This gives the real install locations rather than
    the team's home country.
    """
    # Use exists on geo_country_name rather than term on record_type — works
    # regardless of whether record_type was auto-mapped as text or keyword.
    must = [{"exists": {"field": "geo_country_name"}}]
    date_filter = _build_date_filter(date_from, date_to, "kibana_date")
    if date_filter:
        must.append(date_filter)
    must.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        must.append(stat_filter)

    query = {"bool": {"must": must}}

    # Try keyword sub-fields first (auto-mapped text), then plain fields (explicit keyword mapping)
    for name_field, code_field in [
        ("geo_country_name.keyword", "geo_country_code.keyword"),
        ("geo_country_name", "geo_country_code"),
    ]:
        agg_body = {
            "size": 0,
            "query": query,
            "aggs": {
                "by_country": {
                    "terms": {"field": name_field, "size": 300},
                    "aggs": {
                        "total": {"sum": {"field": stat_cfg["field"]}},
                        "country_code": {"terms": {"field": code_field, "size": 1}},
                    },
                }
            },
        }

        try:
            resp = es.search(
                index=RoleFilterService.scoped_index_pattern("*google_ads"),
                body=agg_body,
                ignore_unavailable=True,
            )
            resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
            buckets = resp_dict.get("aggregations", {}).get("by_country", {}).get("buckets", [])
            if not buckets:
                continue
            points = []
            for bucket in buckets:
                country_name = bucket["key"]
                if not country_name or country_name.lower() in ("unknown", ""):
                    continue
                value = bucket.get("total", {}).get("value") or 0
                if not value:
                    continue
                code_buckets = bucket.get("country_code", {}).get("buckets", [])
                iso2 = code_buckets[0]["key"].upper() if code_buckets else None
                points.append(
                    {
                        "name": country_name,
                        "iso2": iso2,
                        "lat": None,
                        "lon": None,
                        "value": value,
                    }
                )
            if points:
                return points
        except Exception as e:
            logging.warning(f"google_ads geo map query ({name_field}) failed: {e}")

    return []


def _query_team_country_map_generic(es, index_pattern, stat_cfg, org, teams, date_from, date_to):
    """Generic country-level map query for indices that store location in team_country.

    Used for fb_page_insights, ig_insights, youtube_analytics, and ga_metrics which
    have team_country (team location) but no per-city geographic breakdown.
    """
    must = []
    date_filter = _build_date_filter(date_from, date_to, "kibana_date")
    if date_filter:
        must.append(date_filter)
    must.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        must.append(stat_filter)

    query = {"bool": {"must": must}} if must else {"match_all": {}}
    total_agg = {"sum": {"field": stat_cfg["field"]}} if stat_cfg.get("field") else {"value_count": {"field": "_id"}}

    # Try team_country.keyword first (text field with keyword sub-field).
    # Fall back to plain team_country only if .keyword returns nothing — merging
    # both would double-count countries stored under text+keyword mappings.
    for field in ("team_country.keyword", "team_country"):
        agg_body = {
            "size": 0,
            "query": query,
            "aggs": {
                "by_country": {
                    "terms": {"field": field, "size": 300},
                    "aggs": {"total": total_agg},
                },
            },
        }
        try:
            resp = es.search(
                index=RoleFilterService.scoped_index_pattern(index_pattern),
                body=agg_body,
                ignore_unavailable=True,
            )
            resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
            buckets = resp_dict.get("aggregations", {}).get("by_country", {}).get("buckets", [])
            points = []
            for bucket in buckets:
                country_name = bucket["key"]
                if not country_name or country_name.lower() in ("unknown", ""):
                    continue
                value = bucket.get("total", {}).get("value") or bucket["doc_count"]
                iso2 = country_name.upper() if len(country_name) == 2 else None
                points.append(
                    {
                        "name": country_name,
                        "iso2": iso2,
                        "lat": None,
                        "lon": None,
                        "value": value,
                    }
                )
            if points:
                return points
        except Exception as e:
            logging.warning(f"{index_pattern} country ({field}) query failed: {e}")

    return []


def _query_ga4_country_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate GA4 metrics by the native country dimension from the GA4 API.

    GA4 data includes a 'country' keyword field (visitor country) pulled directly
    from the Analytics Data API — more accurate than team_country for web traffic.
    """
    must = []
    date_filter = _build_date_filter(date_from, date_to, "kibana_date")
    if date_filter:
        must.append(date_filter)
    must.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        must.append(stat_filter)

    query = {"bool": {"must": must}} if must else {"match_all": {}}
    total_agg = {"sum": {"field": stat_cfg["field"]}} if stat_cfg.get("field") else {"value_count": {"field": "_id"}}

    agg_body = {
        "size": 0,
        "query": query,
        "aggs": {
            "by_country": {
                "terms": {"field": "country", "size": 300},
                "aggs": {"total": total_agg},
            },
        },
    }

    try:
        resp = es.search(
            index=RoleFilterService.scoped_index_pattern("*ga_metrics"),
            body=agg_body,
            ignore_unavailable=True,
        )
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
        buckets = resp_dict.get("aggregations", {}).get("by_country", {}).get("buckets", [])
        points = []
        for bucket in buckets:
            country_name = bucket["key"]
            if not country_name or country_name.lower() in ("unknown", "(not set)", ""):
                continue
            value = bucket.get("total", {}).get("value") or bucket["doc_count"]
            if not value:
                continue
            points.append(
                {
                    "name": country_name,
                    "iso2": None,
                    "lat": None,
                    "lon": None,
                    "value": value,
                }
            )
        return points
    except Exception as e:
        logging.warning(f"ga4 country map query failed: {e}")
        return []


def _query_youtube_country_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate YouTube analytics by the native country dimension.

    Queries country_breakdown documents (record_type='country_breakdown') produced by
    fetch_channel_analytics_by_country() in get_youtube_analytics.py. These are stored
    alongside the channel-level daily totals so existing charts are not inflated.
    """
    # record_type may be auto-mapped as text — use match instead of term for robustness
    must = [{"match": {"record_type": "country_breakdown"}}]
    date_filter = _build_date_filter(date_from, date_to, "kibana_date")
    if date_filter:
        must.append(date_filter)
    must.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        must.append(stat_filter)

    total_agg = {"sum": {"field": stat_cfg["field"]}} if stat_cfg.get("field") else {"value_count": {"field": "_id"}}

    # Try country.keyword first (auto-mapped text), then plain country (explicit keyword)
    for country_field in ("country.keyword", "country"):
        agg_body = {
            "size": 0,
            "query": {"bool": {"must": must}},
            "aggs": {
                "by_country": {
                    "terms": {"field": country_field, "size": 300},
                    "aggs": {"total": total_agg},
                },
            },
        }

        try:
            resp = es.search(
                index=RoleFilterService.scoped_index_pattern("*youtube_analytics"),
                body=agg_body,
                ignore_unavailable=True,
            )
            resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
            buckets = resp_dict.get("aggregations", {}).get("by_country", {}).get("buckets", [])
            if not buckets:
                continue
            points = []
            for bucket in buckets:
                country_code = bucket["key"]
                if not country_code or country_code.lower() in ("unknown", ""):
                    continue
                value = bucket.get("total", {}).get("value") or bucket["doc_count"]
                if not value:
                    continue
                # YouTube returns ISO2 country codes directly
                points.append(
                    {
                        "name": country_code,
                        "iso2": country_code.upper(),
                        "lat": None,
                        "lon": None,
                        "value": value,
                    }
                )
            if points:
                return points
        except Exception as e:
            logging.warning(f"youtube country map query ({country_field}) failed: {e}")

    return []


def _query_echo_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate Echo conversations by contact_country_iso2 for geographic display.

    Echo documents do not have city-level contact addresses, so this returns
    country-centroid points (one per ISO2 code) using _COUNTRY_CENTROIDS.
    """
    from lib.geocoding import _COUNTRY_CENTROIDS

    must = []
    date_filter = _echo_date_filter(date_from, date_to)
    if date_filter:
        must.append(date_filter)
    must.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        must.append(stat_filter)

    query = {"bool": {"must": must}} if must else {"match_all": {}}
    total_agg = {"sum": {"field": stat_cfg["field"]}} if stat_cfg.get("field") else None

    for field in ("contact_country_iso2.keyword", "contact_country_iso2"):
        sub_aggs = {}
        if total_agg:
            sub_aggs["total"] = total_agg
        agg_body = {
            "size": 0,
            "query": query,
            "aggs": {
                "by_country": {
                    "terms": {"field": field, "size": 300},
                }
            },
        }
        if sub_aggs:
            agg_body["aggs"]["by_country"]["aggs"] = sub_aggs

        try:
            resp = es.search(
                index=RoleFilterService.scoped_index_pattern("*echo_conversations,-unknown_echo_conversations"),
                body=agg_body,
                ignore_unavailable=True,
            )
            resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
            buckets = resp_dict.get("aggregations", {}).get("by_country", {}).get("buckets", [])
            if buckets:
                points = []
                for bucket in buckets:
                    iso2 = bucket["key"].upper()
                    if not iso2 or iso2.lower() in ("unknown", ""):
                        continue
                    centroid = _COUNTRY_CENTROIDS.get(iso2)
                    if not centroid:
                        continue
                    value = bucket.get("total", {}).get("value") or bucket["doc_count"]
                    points.append({"name": iso2, "lat": centroid["lat"], "lon": centroid["lon"], "value": value})
                return points
        except Exception as e:
            logging.warning(f"echo map query ({field}) failed: {e}")

    return []


# ── DT location grid lookup ───────────────────────────────────────────────────
# Maps DT grid IDs (e.g. '100352413') to place names (e.g. 'Karşıyaka').
# Downloaded once from GitHub and cached to disk + in memory.
_dt_grid_names = None  # {grid_id_str: place_name_str}
_dt_grid_names_lock = threading.Lock()


def _atomic_json_dump(path, obj):
    """Write *obj* as JSON to *path* via temp-file + os.replace.

    cache persists used a bare truncate-and-write; two gunicorn
    workers (and, with gthread, two threads) dumping concurrently could
    interleave and corrupt the file. os.replace is atomic, so readers only
    ever see a complete old or complete new file.

    The os.replace is wrapped in a short bounded retry: on Windows it raises
    PermissionError (WinError 5) when the destination is momentarily held open
    by another process — antivirus / a file-sync client scanning the file we
    just wrote, or a concurrent reader. POSIX rename is atomic and never hits
    this, so the retry is a no-op on the prod (Ubuntu) path and only smooths the
    transient lock on Windows dev/test machines. On the final attempt the error
    propagates (we don't silently swallow a genuine permission failure), and the
    temp file is cleaned up so a failed replace leaves no residue.
    """
    import json

    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(obj, f)

    last_err = None
    for attempt in range(5):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError as e:  # Windows: destination transiently locked
            last_err = e
            time.sleep(0.05 * (attempt + 1))
    # Exhausted retries — clean up the temp file so no residue is left, then
    # surface the real error rather than masking it.
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    raise last_err


def _load_dt_grid_names():
    """Return a dict mapping DT location grid IDs to place names.
    Downloads the hierarchy.txt from GitHub on first call; caches to /tmp afterward.
    """
    global _dt_grid_names
    if _dt_grid_names is not None:
        return _dt_grid_names
    import json, os, re

    # build into a local dict and publish once complete. The
    # pre-thread code assigned the empty dict to the global before
    # populating it, so a concurrent reader could see (and keep) a
    # partially-filled map. The lock also collapses concurrent first-call
    # downloads into one.
    with _dt_grid_names_lock:
        if _dt_grid_names is not None:
            return _dt_grid_names

        cache_path = "/tmp/dt_grid_names.json"
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    _dt_grid_names = json.load(f)
                return _dt_grid_names
            except Exception:
                pass
        names = {}
        try:
            resp = http_requests.get(
                "https://raw.githubusercontent.com/DiscipleTools/location-grid-project/master/v1/hierarchy.txt",
                timeout=60,
            )
            pattern = re.compile(r"[-\s]*(.*?) \((\d+)\)")
            for line in resp.text.split("\n"):
                m = pattern.search(line)
                if m:
                    names[m.group(2)] = m.group(1).strip()
            _atomic_json_dump(cache_path, names)
            logging.info(f"DT grid names loaded: {len(names)} entries")
        except Exception as e:
            logging.warning(f"DT grid name lookup load failed: {e}")
        _dt_grid_names = names
        return _dt_grid_names


def _geocode_dt_grid(grid_id):
    """Return (lat, lon) for a DT location grid ID, or None."""
    name = _load_dt_grid_names().get(str(grid_id))
    return _geocode_address(name) if name else None


def _dt_map_query_body(must, extra_aggs=None, address_only=False):
    aggs = {"by_address": {"terms": {"field": "meta.contact_place.keyword", "size": 500}}}
    if not address_only:
        aggs["by_grid"] = {"terms": {"field": "meta.location_grid.keyword", "size": 500}}
    if extra_aggs:
        aggs.update(extra_aggs)
    return {
        "size": 0,
        "query": {"bool": {"must": must}} if must else {"match_all": {}},
        "aggs": aggs,
    }


def _query_dt_country_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate Disciple Tools contacts by country.

    Uses meta.location_grid (primary, better coverage) and meta.contact_place
    (fallback) to resolve country names via Nominatim.
    Called after _query_dt_map so the geocode cache is already warm.

    date_created is stored as text (yyyy-MM-dd HH:mm:ss) so we resolve date math
    to plain date strings and compare against date_created.keyword lexicographically.
    """
    must = []
    must.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        must.append(stat_filter)
    date_filter = _dt_date_filter(date_from, date_to, stat_cfg)
    if date_filter:
        must.append(date_filter)

    address_only = stat_cfg.get("address_only", False)
    try:
        # team-keyed indices ({team_esid}__disciple_tools) replace the
        # retired cross-tenant `disciple-tools` alias. Tenant scoping stays at
        # query level via _org_team_filters, same model the alias used.
        resp = es.search(
            index="*_disciple_tools",
            body=_dt_map_query_body(must, address_only=address_only),
        )
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
    except Exception as e:
        logging.warning(f"dt country map query failed: {e}")
        return []

    from lib.geocoding import country_from_coords

    country_totals = {}  # iso2 -> total_value
    new_count = [0]

    def _add_country(lookup_key, count):
        _load_geocode_cache()
        cached = _geocode_cache.get(lookup_key)
        if not cached or len(cached) < 2:
            return
        lat, lon = cached[0], cached[1]
        if lat is None or lon is None:
            return
        iso2 = country_from_coords(lat, lon)
        if iso2:
            country_totals[iso2] = country_totals.get(iso2, 0) + count

    if not address_only:
        # Primary: location_grid — country map is called after _query_dt_map so
        # most entries should already be in the geocode cache.
        grid_names = _load_dt_grid_names()
        for bucket in resp_dict.get("aggregations", {}).get("by_grid", {}).get("buckets", []):
            gid, count = bucket["key"], bucket["doc_count"]
            name = grid_names.get(gid)
            if name:
                _geocode_address(name, new_count)
                _add_country(name, count)

    # Address-based (primary when address_only=True, fallback otherwise)
    for bucket in resp_dict.get("aggregations", {}).get("by_address", {}).get("buckets", []):
        _geocode_address(bucket["key"], new_count)
        _add_country(bucket["key"], bucket["doc_count"])

    return [{"name": iso2, "iso2": iso2, "lat": None, "lon": None, "value": total} for iso2, total in country_totals.items()]


def _load_dt_team_country_map(es):
    """Return {team_title: primary_iso2} from de_asset_teams.

    team_country_text may be a string ('TR, US, GB'), a list (['EG']), or None.
    We take the first entry as the primary country.

    Result is cached in-process for _TEAM_COUNTRY_MAP_CACHE_TTL seconds to avoid
    redundant de_asset_teams queries across successive map stat API calls.
    """
    global _team_country_map_cache, _team_country_map_cache_ts
    now = time.time()
    if _team_country_map_cache and now - _team_country_map_cache_ts < _TEAM_COUNTRY_MAP_CACHE_TTL:
        return _team_country_map_cache
    # double-checked under the lock so concurrent gthread requests
    # don't stampede de_asset_teams on an expired cache.
    with _team_country_map_lock:
        if _team_country_map_cache and time.time() - _team_country_map_cache_ts < _TEAM_COUNTRY_MAP_CACHE_TTL:
            return _team_country_map_cache
        return _load_dt_team_country_map_locked(es)


def _load_dt_team_country_map_locked(es):
    """Body of _load_dt_team_country_map; caller holds _team_country_map_lock."""
    global _team_country_map_cache, _team_country_map_cache_ts
    now = time.time()
    try:
        resp = es.search(
            index="de_asset_teams",
            body={
                "size": 200,
                "_source": ["team_title", "team_country_text", "team_country"],
                "query": {"match_all": {}},
            },
        )
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
        result = {}
        for hit in resp_dict.get("hits", {}).get("hits", []):
            s = hit.get("_source", {})
            title = s.get("team_title")
            if not title:
                continue
            country_raw = s.get("team_country_text") or s.get("team_country")
            if not country_raw:
                continue
            if isinstance(country_raw, list):
                countries = [c.strip() for c in country_raw if c.strip()]
            else:
                countries = [c.strip() for c in str(country_raw).split(",") if c.strip()]
            if countries:
                result[title] = countries[0]
        _team_country_map_cache = result
        _team_country_map_cache_ts = now
        return result
    except Exception as e:
        logging.warning(f"Failed to load DT team country map: {e}")
        return {}


def _terms_agg_by_team_title(es, index, query, *, metric_agg=None, size=200):
    """group by team_title across BOTH mappings, merged.

    ``team_title`` is mapped two ways across data indices: bare ``keyword`` (the
    current unified schema — most ``*_insights`` / ``*_disciple_tools`` indices)
    and legacy ``text`` + ``.keyword`` subfield. A ``terms`` agg on
    ``team_title.keyword`` silently returns ZERO buckets for the bare-keyword
    indices (no such subfield) — it does NOT raise, so an except-only fallback
    never fires. That dropped whole teams (e.g. one team) from map/country rollups.

    Run the agg on BOTH fields and merge by team. A team lives in exactly one
    mapping, so summing the two bucket sets never double-counts. Each side is
    isolated in its own try/except: a ``terms`` agg on a pure-text field without
    fielddata raises a 400, which must not sink the other side.

    Returns ``{team_title: {"doc_count": int, "value": float|None}}`` where
    ``value`` is the ``metric_agg`` result (None when no metric_agg requested).
    """
    merged: dict = {}
    for field in ("team_title.keyword", "team_title"):
        aggs = {"by_team": {"terms": {"field": field, "size": size}}}
        if metric_agg:
            aggs["by_team"]["aggs"] = {"total": metric_agg}
        try:
            resp = es.search(
                index=index,
                body={"size": 0, "query": query, "aggs": aggs},
                ignore_unavailable=True,
            )
        except Exception as e:  # noqa: BLE001 — text-field-without-fielddata 400s here; the other field still counts
            logging.debug("team_title agg on %r failed (expected for one mapping): %s", field, e)
            continue
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
        for b in resp_dict.get("aggregations", {}).get("by_team", {}).get("buckets", []):
            team = b["key"]
            entry = merged.setdefault(team, {"doc_count": 0, "value": None})
            entry["doc_count"] += b["doc_count"]
            if metric_agg:
                v = b.get("total", {}).get("value")
                if v is not None:
                    entry["value"] = (entry["value"] or 0) + v
    return merged


def _query_team_title_country_map(es, index_pattern, stat_cfg, org, teams, date_from, date_to):
    """Aggregate any index by team_title → primary country from de_asset_teams, summing the metric field.

    Used as a robust fallback for indices where team_country mapping is inconsistent
    across shards/indices (e.g. youtube_analytics). Groups by team_title.keyword,
    sums the metric per team, then maps team → country via de_asset_teams.
    """
    must = []
    date_filter = _build_date_filter(date_from, date_to, "kibana_date")
    if date_filter:
        must.append(date_filter)
    must.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        must.append(stat_filter)

    team_country_map = _load_dt_team_country_map(es)
    if not team_country_map:
        return []

    total_agg = {"sum": {"field": stat_cfg["field"]}} if stat_cfg.get("field") else {"value_count": {"field": "_id"}}
    query = {"bool": {"must": must}} if must else {"match_all": {}}
    # dual-field team_title agg (bare keyword + legacy.keyword), merged.
    # A bare team_title.keyword agg silently drops bare-keyword-mapped teams.
    by_team = _terms_agg_by_team_title(
        es,
        RoleFilterService.scoped_index_pattern(index_pattern),
        query,
        metric_agg=total_agg,
    )

    country_totals = {}
    for team, agg in by_team.items():
        value = agg["value"] if agg["value"] is not None else agg["doc_count"]
        country = team_country_map.get(team)
        if country and value:
            country_totals[country] = country_totals.get(country, 0) + value

    return [{"name": c, "iso2": c, "lat": None, "lon": None, "value": v} for c, v in country_totals.items() if v > 0]


def _query_dt_team_country_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate DT contacts by team_title → primary country from de_asset_teams.

    Used as the country-level map for DT statistics. Unlike the geocoded approach
    (_query_dt_country_map), this covers ALL matching contacts — including those
    without a geocoded address — so the map total matches the Summary value.
    """
    must = []
    must.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        must.append(stat_filter)
    date_filter = _dt_date_filter(date_from, date_to, stat_cfg)
    if date_filter:
        must.append(date_filter)

    team_country_map = _load_dt_team_country_map(es)
    if not team_country_map:
        return []

    query = {"bool": {"must": must}} if must else {"match_all": {}}
    # team-keyed indices replace the retired `disciple-tools` alias.
    # dual-field team_title agg, merged. The previous except-only
    # fallback to bare team_title never fired — a .keyword agg on a bare-keyword
    # index returns 0 buckets rather than raising — so bare-keyword teams were
    # silently dropped from the DT country map.
    by_team = _terms_agg_by_team_title(es, "*_disciple_tools", query)

    country_totals = {}
    for team, agg in by_team.items():
        count = agg["doc_count"]
        country = team_country_map.get(team)
        if country:
            country_totals[country] = country_totals.get(country, 0) + count

    return [{"name": c, "iso2": c, "lat": None, "lon": None, "value": v} for c, v in country_totals.items() if v > 0]


def _query_dt_map(es, stat_cfg, org, teams, date_from, date_to):
    """Aggregate Disciple Tools contacts by location for geographic display.

    Uses meta.location_grid (primary, ~195 contacts) and meta.contact_place
    (fallback, ~12 contacts) to place points on the map.

    date_created is stored as text (yyyy-MM-dd HH:mm:ss) so we resolve date math
    to plain date strings and compare against date_created.keyword lexicographically.
    """
    must = []
    must.extend(_org_team_filters(org, teams))
    stat_filter = _build_stat_filter(stat_cfg)
    if stat_filter:
        must.append(stat_filter)
    date_filter = _dt_date_filter(date_from, date_to, stat_cfg)
    if date_filter:
        must.append(date_filter)

    address_only = stat_cfg.get("address_only", False)
    # team-keyed indices replace the retired `disciple-tools` alias.
    resp = es.search(index="*_disciple_tools", body=_dt_map_query_body(must, address_only=address_only))
    resp_dict = resp.body if hasattr(resp, "body") else dict(resp)

    points = []
    seen_coord_keys = set()
    new_count = [0]  # mutable counter shared across geocode calls in this request

    if not address_only:
        # Primary: location_grid
        grid_names = _load_dt_grid_names()
        for bucket in resp_dict.get("aggregations", {}).get("by_grid", {}).get("buckets", []):
            name = grid_names.get(bucket["key"])
            if not name:
                continue
            coords = _geocode_address(name, new_count)
            if not coords:
                continue
            ck = (round(coords[0], 2), round(coords[1], 2))
            if ck in seen_coord_keys:
                continue
            seen_coord_keys.add(ck)
            points.append(
                {
                    "name": name,
                    "lat": coords[0],
                    "lon": coords[1],
                    "value": bucket["doc_count"],
                }
            )

    # contact_place (primary when address_only=True, fallback otherwise)
    for bucket in resp_dict.get("aggregations", {}).get("by_address", {}).get("buckets", []):
        coords = _geocode_address(bucket["key"], new_count)
        if not coords:
            continue
        ck = (round(coords[0], 2), round(coords[1], 2))
        if ck in seen_coord_keys:
            continue
        seen_coord_keys.add(ck)
        points.append(
            {
                "name": bucket["key"],
                "lat": coords[0],
                "lon": coords[1],
                "value": bucket["doc_count"],
            }
        )

    return points


# ── Team country map cache (in-memory, TTL-based) ────────────────────────────
# _load_dt_team_country_map is called once per map API request for echo/dt/youtube stats.
# Cache the result for 5 minutes to avoid redundant de_asset_teams queries across requests.
_team_country_map_cache: dict = {}
_team_country_map_cache_ts: float = 0.0
_TEAM_COUNTRY_MAP_CACHE_TTL = 300  # seconds
_team_country_map_lock = threading.Lock()


# ── Geocode cache (in-memory + disk-backed) ───────────────────────────────────
_geocode_cache = {}  # address -> (lat, lon, country) or None
_geocode_cache_path = "/tmp/dt_geocode_cache.json"
_geocode_cache_loaded = False
_geocode_cache_lock = threading.Lock()
_nominatim_last_call = 0.0  # epoch seconds of last Nominatim request
# serializes the rate-limit wait + HTTP call. Holding a lock across
# I/O is deliberate here — Nominatim's policy is 1 req/sec process-wide, and
# serializing the calls is exactly what enforces it once handlers run in
# threads.
_nominatim_lock = threading.Lock()

# No live Nominatim calls from the web app.  The geocode cache is
# pre-warmed by the promote_staging cron job so all DT addresses are
# resolved before a user ever loads the map.
_MAX_NEW_GEOCODES = 0


def _load_geocode_cache():
    """Load persisted geocode results from disk into the in-memory cache."""
    global _geocode_cache_loaded
    if _geocode_cache_loaded:
        return
    import json, os

    # the loaded-flag used to be set BEFORE the disk read, so a
    # concurrent thread could see loaded=True, read an empty cache, and
    # poison entries with None. Load under the lock, flag last.
    with _geocode_cache_lock:
        if _geocode_cache_loaded:
            return
        if os.path.exists(_geocode_cache_path):
            try:
                with open(_geocode_cache_path) as f:
                    data = json.load(f)
                _geocode_cache.update(data)
            except Exception:
                pass
        _geocode_cache_loaded = True


def _persist_geocode_cache():
    """Save in-memory geocode results to disk."""
    try:
        # snapshot under the lock (dict iteration during a
        # concurrent insert raises RuntimeError), write atomically.
        with _geocode_cache_lock:
            serialisable = {k: list(v) if v else None for k, v in _geocode_cache.items()}
        _atomic_json_dump(_geocode_cache_path, serialisable)
    except Exception:
        pass


def _geocode_address(address, _new_count=None):
    """Geocode a text address string using Nominatim (OpenStreetMap).
    Returns (lat, lon) tuple or None if not found.
    Results are cached in memory as (lat, lon, country_name).

    _new_count: mutable [int] counter shared by a single request.  If provided
    and the address is not already cached, the counter is incremented and the
    call is skipped once it exceeds _MAX_NEW_GEOCODES to prevent worker timeouts.
    """
    global _nominatim_last_call
    _load_geocode_cache()
    if not address:
        return None
    if address in _geocode_cache:
        cached = _geocode_cache[address]
        return (cached[0], cached[1]) if cached else None
    # Skip if this request has already hit the per-request cap
    if _new_count is not None:
        if _new_count[0] >= _MAX_NEW_GEOCODES:
            return None
        _new_count[0] += 1
    try:
        with _nominatim_lock:
            elapsed = time.time() - _nominatim_last_call
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)
            _nominatim_last_call = time.time()
            resp = http_requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": address, "format": "json", "limit": 1, "addressdetails": "1"},
                headers={"User-Agent": "DEAM-Dashboard/1.0"},
                timeout=5,
            )
        results = resp.json()
        if results:
            lat = float(results[0]["lat"])
            lon = float(results[0]["lon"])
            country = results[0].get("address", {}).get("country", "")
            _geocode_cache[address] = (lat, lon, country)
            _persist_geocode_cache()
            return (lat, lon)
    except Exception as e:
        logging.warning(f'Geocode failed for "{address}": {e}')
    _geocode_cache[address] = None
    return None


# ── Reverse-geocode cache (lat/lon → admin1) ───────────────────────────────────
_rev_geocode_cache = {}  # "lat,lon" -> admin1_name or None
_rev_geocode_cache_path = "/tmp/fb_revgeocode_cache.json"
_rev_geocode_cache_loaded = False
_rev_geocode_cache_lock = threading.Lock()


def _load_rev_geocode_cache():
    global _rev_geocode_cache_loaded
    if _rev_geocode_cache_loaded:
        return
    import json, os

    # same load-under-lock, flag-last fix as _load_geocode_cache.
    with _rev_geocode_cache_lock:
        if _rev_geocode_cache_loaded:
            return
        if os.path.exists(_rev_geocode_cache_path):
            try:
                with open(_rev_geocode_cache_path) as f:
                    _rev_geocode_cache.update(json.load(f))
            except Exception:
                pass
        _rev_geocode_cache_loaded = True


def _persist_rev_geocode_cache():
    try:
        with _rev_geocode_cache_lock:
            snapshot = dict(_rev_geocode_cache)
        _atomic_json_dump(_rev_geocode_cache_path, snapshot)
    except Exception:
        pass


def _reverse_geocode_admin1(lat, lon, _new_count=None):
    """Reverse-geocode (lat, lon) to an admin1 name (state/province/governorate).

    Uses Nominatim /reverse endpoint.  Shares the same rate-limit state as
    _geocode_address to respect the 1 req/sec policy.
    Results are cached at /tmp/fb_revgeocode_cache.json.
    """
    global _nominatim_last_call
    _load_rev_geocode_cache()
    cache_key = f"{round(lat, 3)},{round(lon, 3)}"
    if cache_key in _rev_geocode_cache:
        return _rev_geocode_cache[cache_key]
    if _new_count is not None:
        if _new_count[0] >= _MAX_NEW_GEOCODES:
            return None
        _new_count[0] += 1
    try:
        with _nominatim_lock:
            elapsed = time.time() - _nominatim_last_call
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)
            _nominatim_last_call = time.time()
            resp = http_requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "addressdetails": "1"},
                headers={"User-Agent": "DEAM-Dashboard/1.0", "Accept-Language": "en"},
                timeout=5,
            )
        result = resp.json()
        addr = result.get("address", {})
        admin1 = addr.get("state") or addr.get("province") or addr.get("region") or addr.get("county") or ""
        admin1 = admin1.strip() or None
        _rev_geocode_cache[cache_key] = admin1
        _persist_rev_geocode_cache()
        return admin1
    except Exception as e:
        logging.warning(f"Reverse geocode failed for ({lat},{lon}): {e}")
    _rev_geocode_cache[cache_key] = None
    return None


# ──────────────────────────────────────────────
# Query / Chart Data
# ──────────────────────────────────────────────


def _get_field_types(es, data_view):
    """Return a dict of {field_name: simplified_type} for the given index/pattern.

     Phase C — when the SAME field maps ``text`` in one index and bare
    ``keyword`` in another (the reindex left a mixed fb_insights zoo:
    reindexed teams are bare keyword, legacy teams text+.keyword), prefer the
    ``keyword`` mapping. ES returns get_mapping indices in ARBITRARY order, so
    first-writer-wins made x_field_type a per-request coin flip: a "text" win
    grouped tables on ``<field>.keyword`` — unmapped on every reindexed index —
    and their rows silently vanished (one team dropping off the Reach Test table).
    Preferring keyword is deterministic and correct for every reindexed index
    and for prod (uniform bare keyword since the cutover); the few
    legacy text-mapped staging indices are being reindexed to match.
    """
    try:
        # Layer-1 scope the mapping read (see _data_view_fields).
        mapping = es.indices.get_mapping(index=RoleFilterService.scoped_index_pattern(data_view))
        merged = {}
        for idx_data in mapping.values():
            for k, v in idx_data.get("mappings", {}).get("properties", {}).items():
                if k not in merged:
                    merged[k] = v
                elif merged[k].get("type") == "text" and v.get("type") == "keyword":
                    merged[k] = v
        return {f["name"]: f["type"] for f in _flatten_mapping(merged)}
    except Exception:
        return {}


# Default columns for the posts_table when the chart config carries none
# (e.g. a freshly-added panel). field → header label. Order = display order.
_POSTS_TABLE_DEFAULT_COLUMNS = [
    # Date column ships as an aggregation="date" column with a readable
    # format so renderTable formats it (MMM DD, YYYY -> "May 29, 2026")
    # instead of showing the raw ISO "2026-05-29T11:01:12+0000". The user
    # can change the format in the per-column editor.
    {"field": "post_created_time", "label": "Date", "aggregation": "date", "date_format": "MMM DD, YYYY"},
    {"field": "post_type", "label": "Type"},
    {"field": "post_message", "label": "Post"},
    {"field": "post_reactions_total", "label": "Reactions"},
    {"field": "post_comments", "label": "Comments"},
    {"field": "post_shares", "label": "Shares"},
    # views / viewers replace the impressions and engaged-user
    # columns: Meta retired those metrics (Graph v25.0) and new post docs no
    # longer carry them.
    {"field": "post_media_view", "label": "Views"},
    {"field": "post_total_media_view_unique", "label": "Viewers"},
]


def _run_posts_table_query(es, config, *, dashboard_filters=None):
    """Raw-hits table for the "Recent FB Page Posts" panel (posts_table).

    Unlike the aggregation-based ``table`` path, this returns one row per
    post document: a plain ``es.search`` sorted by ``post_created_time``
    desc, projecting the configured columns. Demonstrates
    ``pages_read_engagement`` data (post text/type + per-post engagement).

    Scope safety: ``run_query`` already applied ``intersect_body_filters``
    + ``append_scope_filter`` to ``config`` before dispatching here, so
    reusing ``build_query``'s query clause carries the org/team scope
    filter and the dashboard custom filter — a scoped user can't read
    another team's posts. ``scoped_index_pattern`` adds the Layer-1
    index allowlist on top.

    Returns the same ``{chart_type:"table", columns, rows}`` shape the
    front-end ``renderTable`` already consumes (chart_type echoed as
    "table" so the existing renderer dispatch picks it up).
    """
    data_view = config.get("data_view", "")
    _require_index_access(data_view)

    table_columns = config.get("table_columns") or _POSTS_TABLE_DEFAULT_COLUMNS
    fields = [c["field"] for c in table_columns if c.get("field")]
    if not fields:
        fields = [c["field"] for c in _POSTS_TABLE_DEFAULT_COLUMNS]
        table_columns = _POSTS_TABLE_DEFAULT_COLUMNS

    # num_values caps the row count; default 25, hard-capped so a config
    # can't ask ES for an unbounded hit set.
    try:
        size = int(config.get("num_values") or 25)
    except (TypeError, ValueError):
        size = 25
    size = max(1, min(size, 200))

    # Reuse build_query's filter translation (scope + custom + dashboard
    # filters) via a no-op metric shape, then run our own raw-hits search.
    query_config = {**config, "chart_type": "metric", "y_fields": [], "breakdown": None}
    built = build_query(query_config, dashboard_filters=dashboard_filters)
    query_clause = built.get("query", {"match_all": {}})

    body = {
        "size": size,
        "_source": fields,
        "query": query_clause,
        "sort": [{"post_created_time": {"order": "desc"}}],
    }

    try:
        resp = es.search(
            index=RoleFilterService.scoped_index_pattern(data_view),
            body=body,
            ignore_unavailable=True,
        )
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
    except Exception:
        logging.exception("posts_table query failed")
        return jsonify(
            {
                "chart_type": "table",
                "columns": [c.get("label") or c["field"] for c in table_columns],
                "rows": [],
                "_warnings": ["Query failed — see server logs for detail"],
            }
        )

    hits = resp_dict.get("hits", {}).get("hits", [])
    columns = [c.get("label") or c["field"] for c in table_columns]
    rows = []
    for hit in hits:
        src = hit.get("_source", {}) or {}
        rows.append([src.get(f, "") for f in fields])

    return jsonify({"chart_type": "table", "columns": columns, "rows": rows})


def _table_has_cross_dv_calc(table_columns, default_dv):
    """True when any calculation column's formula references a field
    bound to a data view other than the chart default.

    per-field DV bindings live in the sparse
    ``col.formula_field_views`` map ({field_name: data_view}); a field
    absent from the map falls back to the chart-default DV. A table
    with such a column must take the multi-DV path — the single-query
    transformer can only aggregate one index pattern.
    """
    for col in table_columns or []:
        if col.get("aggregation") != "calculation":
            continue
        for dv in (col.get("formula_field_views") or {}).values():
            if dv and dv != default_dv:
                return True
    return False


def _subrow_key_value(key_dict, field):
    """the sub-row (x_field_2) value in a composite bucket key.

    The composite source may have been renamed by ``_terms_field`` (
    strips a redundant ``.keyword``; the text auto-suffix adds one), so the raw
    config name alone can miss — mirroring the x_field lookup, try the stored
    name, its base, and the ``.keyword`` variant.
    """
    base = field[: -len(".keyword")] if field.endswith(".keyword") else field
    for cand in (field, base, f"{base}.keyword"):
        if cand in key_dict:
            # missing_bucket stores a null key for docs lacking the
            # field; label it "(none)" so it stays distinct from the None used
            # for x-keys observed only on other data views (blank sub-row cell).
            return "(none)" if key_dict[cand] is None else key_dict[cand]
    return None


def _run_table_multi_dv_query(es, config, *, dashboard_filters=None):
    """Table query where columns span multiple data views.

    Groups columns by their effective data_view, runs a composite aggregation
    per group, then merges rows by x_key.  Returns the same
    {chart_type, columns, rows} shape as the normal transformer output.

    ``dashboard_filters`` is passed through to each per-DV
    ``build_query`` call so dashboard-level filters apply only to columns
    whose DV matches the filter's DV (decision 6).
    """
    default_dv = config.get("data_view", "")
    x_field_raw = config.get("x_field")
    table_columns = config.get("table_columns") or []

    # Validate index access for all referenced data views
    for col in table_columns:
        dv = col.get("data_view") or default_dv
        if dv:
            _require_index_access(dv)

    # Phase 18 — calculation columns don't have a single
    # backing data_view. each formula field may carry its
    # own data view via the sparse ``col.formula_field_views`` map
    # ({field_name: data_view}); fields absent from the map fall back
    # to the chart-default DV, so earlier formulas behave exactly
    # as before. Group field-bound columns by DV; park calc columns
    # separately and resolve which DV each formula field queries.
    dv_to_cols = {}
    calc_cols = []
    for col in table_columns:
        if col.get("aggregation") == "calculation":
            calc_cols.append(col)
            continue
        if not col.get("field"):
            continue
        dv = col.get("data_view") or default_dv
        dv_to_cols.setdefault(dv, []).append(col)

    # per-DV formula-field sets: {dv: {field_name,...}}.
    # Every referenced DV gets index-access validation and a query
    # group, even when no field-bound column lives there.
    calc_fields_by_dv = {}
    for col in calc_cols:
        views = col.get("formula_field_views") or {}
        for fname in col.get("formula_fields") or []:
            fdv = views.get(fname) or default_dv
            if fdv:
                calc_fields_by_dv.setdefault(fdv, set()).add(fname)
    for fdv in calc_fields_by_dv:
        _require_index_access(fdv)
        if fdv not in dv_to_cols:
            dv_to_cols[fdv] = []

    # Ordered field names for output (preserves column order). Calc
    # columns appear in this list via their synthesized __calc_<ts>
    # ``field`` so the renderer keeps them in the right slot.
    ordered_fields = [col["field"] for col in table_columns if col.get("field")]

    # per-column value-store identity. A table can carry two columns
    # on the SAME field but different aggregations (client_id count AND unique);
    # storing values by field name alone collapsed them (both read the first).
    # store_key(col) is the field name normally (unchanged) and a disambiguated
    # "<field>#<aggregation>" token when the field carries multiple aggregations.
    # ordered_store_keys is parallel to ordered_fields — output reads by it, the
    # header still shows the field name. Calc columns keep their synthesized
    # __calc_<ts> field as their store key (already unique).
    _dup_agg_keys = table_metric_agg_keys(table_columns)

    def _store_key(col):
        field = col.get("field")
        agg = col.get("aggregation")
        if agg in ("equals", "calculation") or not field:
            return field
        # Disambiguated only when the helper suffixed this field (2+ aggs).
        if _dup_agg_keys.get((field, agg), "").endswith(f"__agg_{_safe_agg_key(str(agg))}"):
            return f"{field}#{agg}"
        return field

    ordered_store_keys = [_store_key(col) for col in table_columns if col.get("field")]

    # the sub-row (x_field_2) belongs to the CHART-DEFAULT data view
    # (the builder populates the sub-row picker from config.data_view's field
    # list). Columns bound to OTHER data views keep row grain: their sub-queries
    # must not carry the sub-row composite source (a foreign field yields zero
    # buckets and blanks the whole column), and their values render on the FIRST
    # sub-row of each row group only. Without an x_field there is no row key to
    # merge sub-rows under, so the sub-row is dropped from every group.
    x_field_2_raw = config.get("x_field_2")
    x2_active = bool(x_field_2_raw and x_field_raw)

    x_keys_ordered = []  # row-level x keys in first-seen order
    x_keys_seen = set()
    sub_keys_ordered = []  # (x, x2) keys from the default-DV group, bucket order
    sub_keys_seen = set()
    # Row keys are tuples: {(x,): {field: value}} for row-grain (other-DV)
    # values; {(x, x2): {field: value}} for the default DV when a sub-row is
    # active (without one, every group keys at (x,)).
    results = {}
    warnings = []

    # Phase 18 — pre-parse calc-column formulas once. Trees
    # are reused across every bucket and every DV. parse_formula
    # returns ok=False for bad formulas; we drop those silently here
    # because the editor surfaced the error at type time.
    from asset_management.web.services.dashboard_formula import parse_formula, evaluate as _eval_formula

    calc_trees = {}
    for col in calc_cols:
        parsed = parse_formula(col.get("formula") or "")
        if parsed.get("ok"):
            calc_trees[col["field"]] = parsed["tree"]

    # formula-field values collected across every DV's
    # buckets: {row_key: {(field_name, dv): value}}. Keyed by (field, dv)
    # so the same field name resolved to different DVs by different
    # calc columns can't collide. Formulas are evaluated after ALL DV
    # queries have merged (see below), joined per row key.
    calc_values = {}

    # row-key filtering. Rows merge as the UNION of every
    # sub-query's x_keys, so a dashboard filter on the group-by field
    # that reaches only its own DV's sub-query (decision 6) is undone by
    # the other DVs re-contributing the excluded keys. A filter whose
    # field (modulo .keyword drift) IS the table's x_field and whose DV
    # is one this table references is therefore rebound to each
    # sub-query's own DV. Non-group-by filters keep per-DV scoping: they
    # constrain a DV's metric values, not which rows exist.
    x_base = x_field_raw or ""
    if x_base.endswith(".keyword"):
        x_base = x_base[: -len(".keyword")]
    table_dvs = set(dv_to_cols)
    if default_dv:
        table_dvs.add(default_dv)

    def _rowkey_filters_for(dv):
        if not dashboard_filters or not x_base:
            return dashboard_filters
        rebound = []
        for df in dashboard_filters:
            f_base = df.get("field") or "" if isinstance(df, dict) else ""
            if f_base.endswith(".keyword"):
                f_base = f_base[: -len(".keyword")]
            if f_base == x_base and df.get("data_view") in table_dvs:
                rebound.append({**df, "data_view": dv})
            else:
                rebound.append(df)
        return rebound

    # Phase C — route each per-DV sub-query through the dedup helper
    # instead of an inline es.search. run_routed_query is a strict superset of
    # the old inline path: flag OFF, or a DV group with no dedup field (echo,
    # google_ads, ...), runs exactly ONE query identical to before. A group WITH
    # a dedup field (fb/ig reach) gets the window snap +
    # grain routing + stitch — previously bypassed on EVERY multi-DV table, so
    # a reach column here silently summed the raw daily index (the one-team
    # 1,138,146-vs-729,167 symptom). The search_fn closure owns scoping so the
    # daily and dedup queries are scoped identically (mirrors
    # _execute_chart_query's _scoped_search).
    try:
        from web.services.dedup_routing import run_routed_query
        from web.services.es_query_builder import cross_dv_subrow_field, cross_dv_subrow_id_to_name, _build_filters
    except ImportError:
        from services.dedup_routing import run_routed_query
        from services.es_query_builder import cross_dv_subrow_field, cross_dv_subrow_id_to_name, _build_filters

    def _scoped_search(view, body):
        resp = es.search(
            index=RoleFilterService.scoped_index_pattern(view),
            body=body,
            ignore_unavailable=True,
        )
        return resp.body if hasattr(resp, "body") else dict(resp)

    # lazy {page_id: page_name} map for translating an id-valued
    # sub-row group (echo fb_page_id) onto a page_NAME sub-row. Built ONCE and
    # only when an echo group actually needs it. Primary source: the default DV
    # (fb_insights) docs in the SAME query scope — so the names it yields ARE the
    # labels the fb sub-row shows and the echo rows can't drift onto a phantom
    # label. Fallback: the DEAM de_asset_fb_pages inventory, for pages that have
    # conversations but no fb ads in range (else they'd land on the raw id).
    _id_name_map_cache = {}

    def _page_id_to_name_map(map_id_field, map_name_field):
        cache_key = (map_id_field, map_name_field)
        if cache_key in _id_name_map_cache:
            return _id_name_map_cache[cache_key]
        mapping = {}
        # Fallback layer first, so the query-scoped fb names OVERWRITE it (live
        # label always wins over the inventory — the precedence).
        try:
            for doc in get_all_documents("de_asset_fb_pages"):
                src = doc.get("_source", doc)
                pid, pname = src.get("fb_page_id"), src.get("fb_page_name")
                if pid and pname:
                    mapping[str(pid)] = pname
        except Exception:
            logging.exception("de_asset_fb_pages inventory read failed for id→name map")
        try:
            body = {
                "size": 0,
                "query": {
                    "bool": {
                        "filter": _build_filters(config, dashboard_filters=dashboard_filters, active_data_view=default_dv)
                    }
                },
                "aggs": {
                    "ids": {
                        "terms": {"field": map_id_field, "size": 1000},
                        "aggs": {"name": {"terms": {"field": map_name_field, "size": 1}}},
                    }
                },
            }
            resp = _scoped_search(default_dv, body)
            for b in resp.get("aggregations", {}).get("ids", {}).get("buckets", []):
                name_buckets = b.get("name", {}).get("buckets", [])
                if name_buckets:
                    mapping[str(b["key"])] = name_buckets[0]["key"]
        except Exception:
            logging.exception("fb_insights id→name map query failed")
        _id_name_map_cache[cache_key] = mapping
        return mapping

    # Phase C — dedup metadata collected across DV groups so the merged
    # table option carries the same renderer contract as the single-DV transform
    # (dedup_window / dedup_empty / dedup_empty_cells). In practice at most one
    # group (the fb/ig one) produces them; first-wins keeps the merge simple.
    dedup_window = None
    dedup_empty_col = None
    row_dedup_reasons = {}  # {x_key: {field: reason}} from stitched buckets

    # whole-scope totals for the grand-totals row, accumulated per
    # DV from each sub-query's ``table_totals`` bucket (build_query emits it
    # when the totals box is on). Keyed by the column store key so the final
    # totals list aligns with ordered_store_keys exactly like the rows do.
    totals_by_store_key = {}
    calc_totals = {}  # {(formula_field, dv): whole-scope sum}

    for dv, cols in dv_to_cols.items():
        # Re-derive x_field_type for this specific data view so build_query
        # adds .keyword when team_title is text in this index (e.g. *google_ads).
        dv_field_types = _get_field_types(es, dv)
        dv_x_field_type = dv_field_types.get(x_field_raw, config.get("x_field_type", "")) if x_field_raw else ""

        # plug a filtered copy of each calc column into this
        # DV's sub_config so build_query issues sum sub-aggs only for
        # the formula fields that resolve to THIS data view. Calc
        # columns themselves are skipped by build_query's metric_aggs
        # builder, so they don't pollute the per-bucket result keys.
        sub_cols = list(cols)
        if calc_fields_by_dv.get(dv):
            for calc_col in calc_cols:
                views = calc_col.get("formula_field_views") or {}
                fnames = [fname for fname in (calc_col.get("formula_fields") or []) if (views.get(fname) or default_dv) == dv]
                if fnames:
                    sub_cols = sub_cols + [{**calc_col, "formula_fields": fnames}]

        sub_config = {
            **config,
            "data_view": dv,
            "table_columns": sub_cols,
            "x_field_type": dv_x_field_type,
        }
        # only the chart-default DV's group groups by the sub-row.
        # unless this DV declares an equivalent sub-row field (echo
        # fb_page_id ≡ fb_insights page_id): then it groups by its OWN field
        # and its buckets merge into the same (x, sub-row) keys, splitting its
        # metrics per sub-row instead of parking them on the first row.
        dv_subrow_field = None
        # {id: name} map applied to this DV's sub-row bucket keys
        # when it groups by an id but the sub-row is a page NAME. None otherwise.
        dv_subrow_id_name_map = None
        if x2_active:
            if dv == default_dv:
                dv_subrow_field = x_field_2_raw
            else:
                dv_subrow_field = cross_dv_subrow_field(default_dv, x_field_2_raw, dv)
                translate = cross_dv_subrow_id_to_name(default_dv, x_field_2_raw, dv)
                if dv_subrow_field and translate:
                    _other_id_field, _map_id_field, _map_name_field = translate
                    dv_subrow_id_name_map = _page_id_to_name_map(_map_id_field, _map_name_field)
        dv_has_subrow = bool(dv_subrow_field)
        if not dv_has_subrow:
            sub_config.pop("x_field_2", None)
        elif dv != default_dv:
            sub_config["x_field_2"] = dv_subrow_field
        try:
            resp_dict = run_routed_query(sub_config, _scoped_search, dashboard_filters=_rowkey_filters_for(dv))
        except Exception:
            # keep the exception detail in the server log (logging.exception
            # captures the traceback); the warning shown to the dashboard builder UI
            # is generic. Operators see the cause via /logs.
            logging.exception(f"Multi-DV table: query failed for {dv}")
            warnings.append(f'Query failed for data view "{dv}" — see server logs for detail')
            continue

        # Phase C — lift the routed response's dedup metadata (set by
        # stitch_table_responses on a dedup-field group; absent otherwise).
        if resp_dict.get("dedup_window") and dedup_window is None:
            dedup_window = resp_dict["dedup_window"]
        if resp_dict.get("dedup_empty") and dedup_empty_col is None:
            dedup_empty_col = resp_dict["dedup_empty"]

        # lift this DV's whole-scope totals. An agg key absent from
        # the bucket (non-additive window metric, text column) stays out of the
        # map and renders as a blank totals cell.
        totals_bucket = ((resp_dict.get("aggregations", {}).get("table_totals") or {}).get("buckets") or {}).get("all")
        if isinstance(totals_bucket, dict):
            for col in cols:
                field = col["field"]
                agg_key = _dup_agg_keys.get((field, col.get("aggregation")), f"metric_{_safe_agg_key(field)}")
                if agg_key in totals_bucket:
                    v = _get_agg_value(totals_bucket, agg_key)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        totals_by_store_key[_store_key(col)] = v
            for fname in calc_fields_by_dv.get(dv) or ():
                tkey = f"metric_{_safe_agg_key(fname)}"
                if tkey in totals_bucket:
                    v = _get_agg_value(totals_bucket, tkey)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        calc_totals[(fname, dv)] = v

        buckets = resp_dict.get("aggregations", {}).get("table_buckets", {}).get("buckets", [])
        logging.info(f"Multi-DV table: {dv} returned {len(buckets)} buckets")
        if not buckets:
            warnings.append(
                f'No buckets returned for data view "{dv}" — check that x_field "{x_field_raw}" exists in this index'
            )

        for bucket in buckets:
            key_dict = bucket.get("key", {})
            if x_field_raw:
                x_key = str(key_dict.get(x_field_raw, key_dict.get(x_field_raw + ".keyword", "")))
            else:
                x_key = str(list(key_dict.values())[0]) if key_dict else ""

            if x_key not in x_keys_seen:
                x_keys_ordered.append(x_key)
                x_keys_seen.add(x_key)
            # the default-DV group keys per (x, sub-row); every other
            # group stays row-grain at (x,) — except a equivalent-field
            # group, which keys per (x, sub-row) under its OWN field name.
            if dv_has_subrow:
                sub_val = _subrow_key_value(key_dict, dv_subrow_field)
                # translate an id-valued sub-row key (echo fb_page_id)
                # to the page NAME the fb sub-row is keyed by. An id with no name
                # (page has conversations but no fb ad and isn't in inventory)
                # keeps its raw id so it still appears as its own row rather than
                # collapsing into "(none)". "(none)" (missing_bucket) is left as-is.
                if dv_subrow_id_name_map and isinstance(sub_val, str) and sub_val != "(none)":
                    sub_val = dv_subrow_id_name_map.get(sub_val, sub_val)
                row_key = (x_key, sub_val)
                if row_key not in sub_keys_seen:
                    sub_keys_ordered.append(row_key)
                    sub_keys_seen.add(row_key)
            else:
                row_key = (x_key,)
            if row_key not in results:
                results[row_key] = {}

            # Phase C — per-cell dedup reasons ({field: reason}) from the
            # stitched bucket (explained-empty / cross-account rows). Keyed by
            # row key so they can be projected onto the MERGED row order at the end.
            cell_reasons = bucket.get("dedup_empty")
            if isinstance(cell_reasons, dict) and cell_reasons:
                row_dedup_reasons.setdefault(row_key, {}).update(cell_reasons)

            # Field-bound columns: lift each metric_<field> value.
            # read under the same disambiguated agg key build_query
            # emitted, and store under the per-column identity so two columns on
            # one field (different aggregations) don't overwrite each other.
            for col in cols:
                field = col["field"]
                agg_key = _dup_agg_keys.get((field, col.get("aggregation")), f"metric_{_safe_agg_key(field)}")
                results[row_key][_store_key(col)] = _get_agg_value(bucket, agg_key)

            # collect this DV's formula-field values. Only
            # the fields resolved to this DV carry sub-aggs in this
            # query, so lift exactly those.
            for fname in calc_fields_by_dv.get(dv) or ():
                v = _get_agg_value(bucket, f"metric_{_safe_agg_key(fname)}")
                if isinstance(v, (int, float)):
                    calc_values.setdefault(row_key, {})[(fname, dv)] = v

    # final row keys. With a sub-row active, the default DV's
    # (x, x2) keys come first in bucket order; x keys observed ONLY on other
    # DVs append as a single (x, None) row so union-of-rows semantics survive.
    # Without a sub-row this is exactly the earlier x_key order.
    if x2_active:
        final_keys = list(sub_keys_ordered)
        xs_with_sub = {k[0] for k in final_keys}
        final_keys += [(x, None) for x in x_keys_ordered if x not in xs_with_sub]
    else:
        final_keys = [(x,) for x in x_keys_ordered]

    # Per final row: merge the default-DV (sub-row) values with the row-grain
    # (other-DV) values — the latter attach to the FIRST row of each x group
    # only, so a team-level metric never repeats down its sub-rows. The same
    # first-row rule applies to the calc inputs and the dedup cell reasons.
    values_by_key = []
    calc_inputs_by_key = []
    reasons_by_key = []
    _seen_group = set()
    for k in final_keys:
        x_level = (k[0],)
        vals, cvals, reasons = {}, {}, {}
        if k[0] not in _seen_group:
            _seen_group.add(k[0])
            vals.update(results.get(x_level, {}))
            cvals.update(calc_values.get(x_level, {}))
            reasons.update(row_dedup_reasons.get(x_level, {}))
        if len(k) == 2:
            vals.update(results.get(k, {}))
            cvals.update(calc_values.get(k, {}))
            reasons.update(row_dedup_reasons.get(k, {}))
        values_by_key.append(vals)
        calc_inputs_by_key.append(cvals)
        reasons_by_key.append(reasons)

    # evaluate calc columns once every DV query has merged,
    # so a formula can mix fields from different data views joined on
    # the row key. A field whose DV had no bucket for this row is simply
    # absent from the values map; evaluate() returns None and the
    # renderer shows "—" (agreed missing-side behavior).
    if calc_cols:
        for vals, per_key in zip(values_by_key, calc_inputs_by_key):
            for col in calc_cols:
                tree = calc_trees.get(col["field"])
                if tree is None:
                    vals[col["field"]] = None
                    continue
                views = col.get("formula_field_views") or {}
                values = {}
                for fname in col.get("formula_fields") or []:
                    v = per_key.get((fname, views.get(fname) or default_dv))
                    if v is not None:
                        values[fname] = v
                vals[col["field"]] = _eval_formula(tree, values)

    x_display = (x_field_raw[:-8] if x_field_raw and x_field_raw.endswith(".keyword") else x_field_raw) or "key"
    label_cols = [x_display]
    if x2_active:
        # emit the sub-row column so the response shape matches the
        # renderer's label-column count (charts.js counts 2 when x_field_2 is
        # set; omitting the column shifted every metric one slot).
        label_cols.append(x_field_2_raw[:-8] if x_field_2_raw.endswith(".keyword") else x_field_2_raw)
    # the output column identities must be the per-column store keys,
    # not the bare field names: two columns on one field (count + unique) share a
    # field, so emitting field names here collided them in the renderer's
    # positional label/format map (both headers took the last column's label —
    # "Conversations" showed as "Unique Conversations"). ordered_store_keys is
    # field-name-identical for single-aggregation fields, so existing tables are
    # unchanged; only a duplicated field gets a distinct "<field>#<agg>" identity.
    columns = label_cols + ordered_store_keys
    rows = []
    for k, vals in zip(final_keys, values_by_key):
        labels = [k[0]]
        if x2_active:
            labels.append("" if len(k) < 2 or k[1] is None else str(k[1]))
        rows.append(labels + [vals.get(f) for f in ordered_store_keys])
    table_option = {"chart_type": "table", "columns": columns, "rows": rows, "_warnings": warnings}

    # server-computed grand totals aligned with ``columns`` (None =
    # blank cell), same shape the single-DV transform emits. Calc columns
    # evaluate their formula over the whole-scope sums, mixing DVs exactly like
    # the per-row evaluation above.
    if config.get("include_totals_row") is True:
        for col in calc_cols:
            tree = calc_trees.get(col["field"])
            if tree is None:
                continue
            views = col.get("formula_field_views") or {}
            values = {}
            for fname in col.get("formula_fields") or []:
                v = calc_totals.get((fname, views.get(fname) or default_dv))
                if v is not None:
                    values[fname] = v
            totals_by_store_key[_store_key(col)] = _eval_formula(tree, values)
        table_option["totals"] = [None] * len(label_cols) + [totals_by_store_key.get(f) for f in ordered_store_keys]

    # Phase C — carry the routed dedup metadata on the merged option in
    # the SAME shape the single-DV transform emits (echarts_transformer), so
    # renderTable paints the snapped-window caption, explained-empty cells, and
    # cross-account markers identically for multi-DV tables. dedup_empty_cells is
    # parallel to `rows`, each entry {output-col-index: reason}; col 0 is the
    # x/label column, so field f sits at 1 + its ordered_fields position.
    if dedup_window:
        table_option["dedup_window"] = dedup_window
    if dedup_empty_col:
        table_option["dedup_empty"] = dedup_empty_col
    if row_dedup_reasons:
        col_index_by_field = {f: len(label_cols) + j for j, f in enumerate(ordered_fields)}
        dedup_empty_cells = []
        for reasons in reasons_by_key:
            dedup_empty_cells.append({col_index_by_field[f]: r for f, r in reasons.items() if f in col_index_by_field})
        if any(dedup_empty_cells):
            table_option["dedup_empty_cells"] = dedup_empty_cells

    # Phase C — feed the warning detector a synthetic routed-response
    # dict so it can CLEAR the reach triangle when routing genuinely deduplicated
    # the field everywhere (keep it warned if the column or
    # ANY row fell back). Before this, the call passed no es_dict, so a multi-DV
    # reach column kept the daily-sum triangle even when fully deduplicated.
    synthetic_es = {
        "aggregations": {
            "table_buckets": {"buckets": [{"dedup_empty": reasons} if reasons else {} for reasons in reasons_by_key]}
        },
    }
    if dedup_window:
        synthetic_es["dedup_window"] = dedup_window
    if dedup_empty_col:
        synthetic_es["dedup_empty"] = dedup_empty_col
    _attach_reach_warning(table_option, config, synthetic_es)
    return jsonify(table_option)


@dashboard_bp.route("/api/query", methods=["POST"])
def run_query():
    """Execute an ES query for a chart config and return ECharts-formatted data."""
    try:
        config = request.get_json(force=True)
        if not config:
            return jsonify({"success": False, "error": "No config provided"}), 400

        # dashboard-level filters travel as a sibling array on the
        # request body. Pulled out before any RoleFilterService calls so they
        # can't be confused with per-chart filter scope-bypass attempts.
        dashboard_filters = config.pop("dashboard_filters", None) or []

        _require_index_access(config.get("data_view", ""))

        # scoped users (team_*/org_* roles) must not be able to
        # query other teams' data via hand-crafted ``filters`` clauses in
        # the request body. ``intersect_body_filters`` strips
        # team_title / organization_title filter values that fall outside
        # the user's allowed set; ``append_scope_filter`` adds an
        # allow-only clause so even unfiltered queries are pinned to the
        # allowed team set. Admins (allowed_teams is None) keep their
        # existing free-choice behaviour.
        allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
        RoleFilterService.intersect_body_filters(config, allowed_teams)
        RoleFilterService.append_scope_filter(config, allowed_teams)

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        # Posts table — raw document hits (one row per post), NOT an
        # aggregation. The generic "table" path below buckets/sums numeric
        # metrics and can't carry post text/type as row content, so posts
        # get their own raw-hits query. Scope guards already applied above.
        if config.get("chart_type") == "posts_table":
            return _run_posts_table_query(es, config, dashboard_filters=dashboard_filters)

        # Route to multi-data-view handler when any column has its own
        # data_view, or a calculation column's formula
        # references a field from a non-default data view.
        if config.get("chart_type") == "table":
            default_dv = config.get("data_view", "")
            table_cols = config.get("table_columns") or []
            if any(col.get("data_view") for col in table_cols if col.get("field")) or _table_has_cross_dv_calc(
                table_cols, default_dv
            ):
                return _run_table_multi_dv_query(es, config, dashboard_filters=dashboard_filters)

        echarts_option = _execute_chart_query(es, config, dashboard_filters=dashboard_filters)
        return jsonify(echarts_option)
    except Exception as e:
        if isinstance(e, HTTPException) and e.code == 403:
            raise  # Re-raise 403 from _require_index_access
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error running dashboard query",
            log_detail="run_query failed",
            exc=e,
        )


def _attach_reach_warning(option, config, es_dict=None):
    """attach the inflated-reach warning to an ECharts option.

    Sets ``option["reach_warning"]`` to ``{fields, message}`` when the chart is
    bound to a FB reach field showing the un-deduplicated daily sum; leaves the
    option untouched otherwise. Safe on any transform output (metric / table /
    bar-line-pie) since they are all plain dicts. Best-effort: a detection error
    must never break a query response.

    the warning must track what dedup ROUTING actually returned, not
    just the config. ``inflated_reach_warning(config)`` is the config-level
    candidate set (reach field on a non-window view). When ``es_dict`` is the
    routed response, a field is CLEARED only if routing genuinely deduplicated it
    **everywhere it appears** — every aggregated row got a dedup value. A field is
    KEPT warned if it fell back at column level (``no_window`` / ``not_built``)
    OR at ANY row (per-row ``dedup_empty``).

    The row check is the multi-team fix: a wildcard ``*_fb_insights`` table
    scoped across many teams can have a top-level ``dedup_window`` set merely
    because ONE team has window docs, while every other team's row fell back to a
    daily sum (per-row ``dedup_empty``: ``no_data``). Suppressing on the
    top-level signal alone hid the over-count on the un-deduplicated teams — so we
    only clear a field when NO row reports it as fallen-back. Non-table / flag-off
    responses carry no dedup metadata, so nothing is cleared (every config-warned
    field remains) — exactly the daily-sum case.
    """
    if not isinstance(option, dict):
        return option
    try:
        warning = inflated_reach_warning(config)
        if not warning:
            return option
        warned = list(warning["fields"])

        if isinstance(es_dict, dict):
            dedup_present = bool(es_dict.get("dedup_window")) or ("dedup_empty" in es_dict)
            if dedup_present:
                # A field fell back if the whole column fell back (top-level
                # dedup_empty.fields) OR any aggregated row reports it in its
                # per-row dedup_empty map. Only clear fields that fell back NOWHERE.
                fell_back = set((es_dict.get("dedup_empty") or {}).get("fields") or [])
                buckets = (es_dict.get("aggregations") or {}).get("table_buckets") or {}
                for row in buckets.get("buckets") or []:
                    row_empty = row.get("dedup_empty") or {}
                    if isinstance(row_empty, dict):
                        fell_back.update(row_empty.keys())
                # Keep a field iff routing did NOT deduplicate it everywhere.
                warned = [f for f in warned if f in fell_back]

        if warned == warning["fields"]:
            option["reach_warning"] = warning  # unchanged (flag-off / non-table / any fallback)
        elif warned:
            # Reduced set — rebuild via the centralized builder so copy + field
            # list stay single-sourced. Feed a synthetic daily-view config.
            option["reach_warning"] = inflated_reach_warning({"y_fields": warned, "data_view": "*_fb_insights"})
        # else: every warned field was genuinely deduplicated everywhere → no triangle.
    except Exception:
        logging.exception("reach-warning detection failed")
    return option


def _execute_chart_query(es, config, *, dashboard_filters=None, field_types_cache=None):
    """Shared body of ``/api/query`` and ``/api/query_batch``:
    field-type enrichment, query build, Layer-1-scoped search, ECharts
    transform. Returns the ECharts option dict.

    Scope guards (``validate_index_access`` / ``intersect_body_filters`` /
    ``append_scope_filter``) must already be applied by the caller; the
    Layer-1 index rewrite (``scoped_index_pattern``) happens here so no
    caller can reach ES with an unscoped pattern.

    ``field_types_cache``: optional ``{data_view: field_types}`` dict the
    batch endpoint shares across items, so N metrics over the same data
    view cost one ``get_mapping`` instead of N.
    """
    data_view = config.get("data_view", "")
    if field_types_cache is not None and data_view in field_types_cache:
        field_types = field_types_cache[data_view]
    else:
        # Enrich y_field_types from ES mapping so the query builder can fall back
        # to count for text/keyword fields regardless of what the frontend stored.
        field_types = _get_field_types(es, data_view)
        if field_types_cache is not None:
            field_types_cache[data_view] = field_types
    if field_types:
        existing = config.get("y_field_types") or {}
        y_fields = config.get("y_fields") or ([config["y_field"]] if config.get("y_field") else [])
        for yf in y_fields:
            if yf not in existing and yf in field_types:
                existing[yf] = field_types[yf]
        config["y_field_types"] = existing

        # Fix x_field: strip .keyword suffix when the base field is already
        # keyword type.  Mixed mappings across indices (e.g. youtube_analytics)
        # can cause some to have text+keyword and others plain keyword; using
        # the bare keyword field works for both.
        x_field = config.get("x_field", "")
        if x_field and x_field.endswith(".keyword"):
            base = x_field[:-8]
            if field_types.get(base) == "keyword":
                config["x_field"] = base

    # route through the dedup helper. When the flag is OFF or the
    # chart has no dedup field this is exactly one scoped query (unchanged); when
    # ON and the chart reads a dedup field, the helper runs the daily query + a
    # dedup-index query (sequential) and stitches. The search_fn closure owns
    # scoping so BOTH queries are scoped identically (RoleFilterService).
    try:
        from web.services.dedup_routing import run_routed_query
    except ImportError:
        from services.dedup_routing import run_routed_query

    def _scoped_search(data_view, body):
        resp = es.search(
            index=RoleFilterService.scoped_index_pattern(data_view),
            body=body,
            ignore_unavailable=True,
        )
        return resp.body if hasattr(resp, "body") else dict(resp)

    es_dict = run_routed_query(config, _scoped_search, dashboard_filters=dashboard_filters)

    option = transform(es_dict, config)
    _attach_reach_warning(option, config, es_dict)
    return option


# hard cap on batch size. The Summary Report sends 29 metrics; the
# cap bounds worst-case request cost without constraining legitimate pages.
_QUERY_BATCH_MAX_ITEMS = 50


@dashboard_bp.route("/api/query_batch", methods=["POST"])
def run_query_batch():
    """execute many chart configs in ONE request.

    The Summary Report used to fire one ``/api/query`` per metric (29 HTTP
    round-trips), each independently re-resolving the user's authz scope
    (``resolve_user_scope`` is memoized per request only) and re-fetching
    index mappings. Batching amortizes both: scope resolves once per batch
    via the request-level ``g`` memo, and ``field_types_cache`` shares one
    ``get_mapping`` per distinct data view.

    Scope safety mirrors ``/api/query`` exactly, applied PER ITEM —
    body-filter stripping + allow-only scope clause, and the
    Layer-1 index rewrite inside ``_execute_chart_query``. A denied or
    failed item yields a per-item error entry; it never sinks the batch
    and never escalates to a whole-request 403.

    Request:  ``{"queries": [<chart config>, ...]}``
    Response: ``{"results": [<echarts option | {"success": false, ...}>, ...]}``
    — results align with the request array by index.

    ``posts_table`` and multi-data-view ``table`` configs keep their
    dedicated single-query endpoints (their responses are bespoke Flask
    Responses, not ECharts options).
    """
    try:
        payload = request.get_json(force=True)
        queries = (payload or {}).get("queries")
        if not isinstance(queries, list) or not queries:
            return jsonify({"success": False, "error": "No queries provided"}), 400
        if len(queries) > _QUERY_BATCH_MAX_ITEMS:
            return (
                jsonify({"success": False, "error": f"Too many queries (max {_QUERY_BATCH_MAX_ITEMS})"}),
                400,
            )

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        # Resolved once; the session is constant across the batch, so this
        # matches what /api/query would resolve for every individual item.
        allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()

        field_types_cache = {}
        results = []
        for item_config in queries:
            if not isinstance(item_config, dict) or not item_config:
                results.append({"success": False, "error": "Invalid query config"})
                continue

            dashboard_filters = item_config.pop("dashboard_filters", None) or []

            if not validate_index_access(item_config.get("data_view", "")):
                results.append({"success": False, "error": "Access denied: index outside your organization scope."})
                continue

            chart_type = item_config.get("chart_type")
            table_cols = item_config.get("table_columns") or []
            if chart_type == "posts_table" or (
                chart_type == "table"
                and (
                    any(col.get("data_view") for col in table_cols if col.get("field"))
                    # cross-DV calc formulas need the multi-DV
                    # fan-out, which batch doesn't support either.
                    or _table_has_cross_dv_calc(table_cols, item_config.get("data_view", ""))
                )
            ):
                results.append({"success": False, "error": "chart_type not supported in batch"})
                continue

            try:
                RoleFilterService.intersect_body_filters(item_config, allowed_teams)
                RoleFilterService.append_scope_filter(item_config, allowed_teams)
                results.append(
                    _execute_chart_query(
                        es,
                        item_config,
                        dashboard_filters=dashboard_filters,
                        field_types_cache=field_types_cache,
                    )
                )
            except Exception as e:  # per-item boundary: one bad metric must not sink the batch
                logging.warning(f"query_batch item failed ({item_config.get('data_view', '')}): {e}")
                results.append({"success": False, "error": "Query failed"})

        return jsonify({"results": results})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error running dashboard batch query",
            log_detail="run_query_batch failed",
            exc=e,
        )


# Fields the Profile card reads from the IG insights index. Mirrors the
# order shown in the rendered card. ``ig_user_*`` are point-in-time
# attributes (followers/media counts move daily), so the card shows the
# single most-recent doc within the user's scope rather than aggregating.
_PROFILE_IG_FIELDS = [
    "ig_user_profile_picture_url",
    "ig_user_name",
    "ig_user_username",
    "ig_user_biography",
    "ig_user_followers_count",
    "ig_user_media_count",
    "ig_user_website",
]


@dashboard_bp.route("/api/profile", methods=["POST"])
def get_profile_data():
    """Return the latest in-scope Instagram profile doc for a Profile card.

    The Profile card is a fixed-field display (no x/y bindings): it shows
    the most-recent ``*_ig_insights`` document within the requesting
    user's allowed team set, projecting only the profile attributes the
    card renders.

    Scope safety mirrors ``/api/query`` exactly — ``_require_index_access``
    plus ``intersect_body_filters`` + ``append_scope_filter`` so a scoped
    user can never read another team's profile via a hand-crafted body, and
    so an unfiltered request is still pinned to the allowed team set. The
    actual filter translation is delegated to ``build_query`` (we reuse its
    ``query`` clause) so the Profile card honors org/team scoping and the
    dashboard custom filter identically to every other chart.
    """
    try:
        config = request.get_json(force=True)
        if not config:
            return jsonify({"error": "No config provided"}), 400

        dashboard_filters = config.pop("dashboard_filters", None) or []

        data_view = config.get("data_view", "")
        _require_index_access(data_view)

        # Same scope guards as /api/query: strip out-of-scope
        # team/org filter values, then pin to the allowed set.
        allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
        RoleFilterService.intersect_body_filters(config, allowed_teams)
        RoleFilterService.append_scope_filter(config, allowed_teams)

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"error": "Elasticsearch unavailable"}), 503

        # Reuse build_query's filter translation (scope + custom filter +
        # dashboard filters) by borrowing its query clause from a no-op
        # metric shape, then run our own size-1 sorted projection.
        query_config = {**config, "chart_type": "metric", "y_fields": [], "breakdown": None}
        built = build_query(query_config, dashboard_filters=dashboard_filters)
        query_clause = built.get("query", {"match_all": {}})

        body = {
            "size": 1,
            "_source": _PROFILE_IG_FIELDS,
            "query": query_clause,
            "sort": [{"@timestamp": {"order": "desc"}}],
        }

        resp = es.search(
            index=RoleFilterService.scoped_index_pattern(data_view),
            body=body,
            ignore_unavailable=True,
        )
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
        hits = resp_dict.get("hits", {}).get("hits", [])
        if not hits:
            return jsonify({"profile": None})

        src = hits[0].get("_source", {}) or {}
        profile = {field: src.get(field) for field in _PROFILE_IG_FIELDS}
        return jsonify({"profile": profile})
    except Exception as e:
        if isinstance(e, HTTPException) and e.code == 403:
            raise  # Re-raise 403 from _require_index_access
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error loading profile data",
            log_detail="get_profile_data failed",
            exc=e,
        )


# Hosts the profile-image proxy is allowed to fetch from. Instagram /
# Facebook serve avatars from signed *.cdninstagram.com / *.fbcdn.net
# URLs that the browser refuses to hot-link (referrer/CORS policy), so
# the card routes them through this same-origin proxy. The allowlist is
# an SSRF guard — the proxy will only fetch from these suffixes, never
# an arbitrary URL a caller hands it.
_PROFILE_IMAGE_HOST_SUFFIXES = (
    ".cdninstagram.com",
    ".fbcdn.net",
)
_PROFILE_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB cap — avatars are tiny

# Strict raster-image content-type allowlist. SVG is deliberately
# EXCLUDED: image/svg+xml is an active document (can carry <script>) and,
# served same-origin, would be a stored-XSS vector. The value we echo
# back to the browser is taken from THIS map (normalized), never from the
# upstream header verbatim.
_PROFILE_IMAGE_ALLOWED_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}


@dashboard_bp.route("/api/profile-image")
def get_profile_image():
    """Same-origin proxy for an Instagram profile picture.

    Instagram's ``profile_picture_url`` is a signed CDN link the browser
    won't hot-link. The Profile card points its ``<img>`` here with the
    CDN URL as ``?url=``; the server fetches it and streams it back from
    our own origin so it loads reliably.

    Security:
      * SSRF — the URL must be https and its host must end in one of
        ``_PROFILE_IMAGE_HOST_SUFFIXES``. Redirects are NOT followed
        (``allow_redirects=False``); a 3xx is rejected, so a CDN URL
        can't bounce the fetch to an internal/metadata host past the
        host check.
      * Content-type XSS — only raster types in
        ``_PROFILE_IMAGE_ALLOWED_TYPES`` are served (SVG rejected), the
        echoed Content-Type is normalized from that map (not the upstream
        header), and ``X-Content-Type-Options: nosniff`` +
        ``Content-Disposition: inline`` + a locked-down CSP prevent the
        bytes from being treated as an active document.
      * Size — hard 5 MB cap so an oversized upstream can't balloon memory.
    """
    from urllib.parse import urlparse, urlunparse

    # nosemgrep: python.django.security.injection.ssrf.ssrf-injection-requests.ssrf-injection-requests, python.flask.security.injection.ssrf-requests.ssrf-requests
    raw_url = request.args.get("url", "")  # SSRF: scheme+host allowlisted below before the fetch (see the requests.get note)
    parsed = urlparse(raw_url)

    if parsed.scheme != "https":
        return jsonify({"error": "Only https image URLs are allowed"}), 400

    # urlparse and requests/urllib3 disagree about where the host
    # is when the URL carries a backslash or userinfo:
    # ``https://10.0.0.5:9200\@a.fbcdn.net/x`` parses as host a.fbcdn.net
    # here but connects to 10.0.0.5:9200. Signed CDN avatar URLs never carry
    # userinfo, a port, a backslash or whitespace, so refuse them outright.
    if (
        "\\" in raw_url
        or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in raw_url)
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        return jsonify({"error": "Image URL not allowed"}), 400
    try:
        port = parsed.port
    except ValueError:
        return jsonify({"error": "Image URL not allowed"}), 400
    if port not in (None, 443):
        return jsonify({"error": "Image URL not allowed"}), 400

    host = (parsed.hostname or "").lower()
    if not any(host == s.lstrip(".") or host.endswith(s) for s in _PROFILE_IMAGE_HOST_SUFFIXES):
        return jsonify({"error": "Image host not allowed"}), 400

    # Fetch a URL rebuilt from the validated parts, never the caller's string,
    # so the host requests connects to is the host checked above.
    fetch_url = urlunparse(("https", host, parsed.path, parsed.params, parsed.query, ""))

    try:
        # allow_redirects=False — a redirect can point anywhere (incl.
        # 169.254.169.254 / RFC1918), bypassing the host allowlist above.
        # SSRF is mitigated as the semgrep ssrf rules prescribe: the URL
        # scheme is pinned to https and the host validated against the
        # _PROFILE_IMAGE_HOST_SUFFIXES allowlist BEFORE this call.
        # Regression-pinned -PROFILE-IMAGE-PROXY-001. The two ssrf
        # rules anchor differently (django at the request.args source line,
        # flask at this sink), so the suppression is on BOTH lines.
        # nosemgrep: python.flask.security.injection.ssrf-requests.ssrf-requests, python.django.security.injection.ssrf.ssrf-injection-requests.ssrf-injection-requests
        upstream = http_requests.get(fetch_url, timeout=10, stream=True, allow_redirects=False)
    except Exception:
        # Best-effort proxy: a dead/expired CDN URL becomes a 502 so the
        # card's <img> onerror can fall back. No detail leaked to client.
        logging.warning("profile-image proxy: upstream fetch failed", exc_info=True)
        return jsonify({"error": "Could not fetch image"}), 502

    # Reject redirects outright rather than chasing them to a re-validated
    # target — IG avatar URLs resolve directly, so there's no legitimate
    # 3xx to follow.
    if upstream.is_redirect or 300 <= upstream.status_code < 400:
        upstream.close()
        return jsonify({"error": "Redirects are not allowed"}), 502
    if upstream.status_code != 200:
        upstream.close()
        return jsonify({"error": "Could not fetch image"}), 502

    # Normalize against the strict allowlist — strip any ``; charset=…``
    # parameter, lowercase, and reject anything not a known raster type.
    raw_ct = upstream.headers.get("Content-Type", "")
    base_ct = raw_ct.split(";", 1)[0].strip().lower()
    safe_ct = _PROFILE_IMAGE_ALLOWED_TYPES.get(base_ct)
    if not safe_ct:
        upstream.close()
        return jsonify({"error": "Upstream did not return a supported image"}), 502

    # Read with a hard size cap so a misbehaving/oversized upstream can't
    # balloon memory.
    chunks = []
    total = 0
    for chunk in upstream.iter_content(chunk_size=8192):
        if not chunk:
            continue
        total += len(chunk)
        if total > _PROFILE_IMAGE_MAX_BYTES:
            upstream.close()
            return jsonify({"error": "Image too large"}), 502
        chunks.append(chunk)

    resp = make_response(b"".join(chunks))
    # Echo the NORMALIZED type, never the upstream string.
    resp.headers["Content-Type"] = safe_ct
    # Defense in depth against MIME confusion / active-content rendering:
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Disposition"] = 'inline; filename="avatar"'
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    # Cache in the browser for an hour — the signed CDN URL rotates daily
    # via re-import anyway, so a short cache is safe and cuts round-trips.
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@dashboard_bp.route("/api/trend-line-data", methods=["POST"])
def get_trend_line_data():
    """Return multi-source date histogram data for a trend_line chart.

    Accepts a JSON body with:
        sources      — list of {label, data_view, field, color, date_field}
        filters      — optional list of {field, op, value} (org/team filters)
        date_range   — optional {field, from, to}
        date_interval — 'month' (default) | 'week' | 'day' | 'year'
    Returns:
        {x_data: ['2025-01', ...], series: [{name, data, color}, ...]}
    """
    try:
        body = request.get_json(force=True)
        if not body:
            return jsonify({"error": "No config provided"}), 400

        # dashboard-level filters from the top-of-dashboard strip.
        # Each source has its own data_view; per-source merge happens below
        # using ``active_data_view=source['data_view']``.
        dashboard_filters = body.pop("dashboard_filters", None) or []

        # Validate index access for all sources
        for src in body.get("sources", []):
            dv = src.get("data_view", "")
            if dv:
                _require_index_access(dv)

        # same body-filter scope-bypass pattern as /api/query.
        # Intersect any scope-defining filters in body['filters'] against
        # the user's allowed teams, then append an allow-only clause so
        # unfiltered queries are also pinned. Admins (allowed_teams is
        # None) are unaffected.
        allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
        RoleFilterService.intersect_body_filters(body, allowed_teams)
        RoleFilterService.append_scope_filter(body, allowed_teams)

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"error": "Elasticsearch unavailable"}), 503

        sources = body.get("sources", [])
        interval = body.get("date_interval", "month")
        # base_filters is now derived per-source inside the loop
        # so dashboard filters can be merged against the source's own DV.

        # Extended bounds: if a date range is supplied, force ES to emit buckets for
        # every interval within that range (even if no docs exist), so the x-axis
        # spans the full selected time range.
        # extended_bounds has no ``format`` slot; ES parses ``min`` /
        # ``max`` against the field's stored format. Echo's kibana_date is
        # ``MMMM dd, yyyy HH:mm:ss 'UTC'`` so a bare ``yyyy-MM-dd`` is rejected.
        # Convert ISO bounds to epoch_millis (accepted on every date field
        # regardless of stored format); date math like ``now-3M/d`` passes
        # through and ES handles it natively.
        dr = body.get("date_range") or {}
        extended_bounds = None
        if dr.get("from") and dr.get("to"):
            extended_bounds = {
                "min": _iso_to_epoch_ms(dr["from"]),
                "max": _iso_to_epoch_ms(dr["to"], upper_bound=True),
            }

        all_dates = set()
        results = {}

        for source in sources:
            label = source.get("label", "")
            data_view = source.get("data_view", "")
            field = source.get("field") or None
            date_field = source.get("date_field", "kibana_date")
            query_filter = source.get("query_filter")

            if not data_view:
                results[label] = {}
                continue

            is_ms_date = date_field.endswith("_ms")
            hist = {
                "field": date_field,
                "calendar_interval": interval,
                "format": "yyyy-MM",
                "min_doc_count": 0,
            }
            if extended_bounds:
                hist["extended_bounds"] = extended_bounds

            cardinality_field = source.get("cardinality_field") or None
            aggs = {"over_time": {"date_histogram": hist}}
            if field and is_ratio_field(field):
                # registered ratio fields emit sum(num)/sum(denom)
                # per time bucket. date_histogram is a multi-bucket parent,
                # so bucket_script works natively here without the filters
                # wrapper that the KPI path needs. The renderer reads
                # bucket["metric"].value transparently.
                spec = RATIO_FIELDS[field]
                aggs["over_time"]["aggs"] = {
                    "metric_num": {"sum": {"field": spec["numerator"]}},
                    "metric_denom": {"sum": {"field": spec["denominator"]}},
                    "metric": {
                        "bucket_script": {
                            "buckets_path": {"num": "metric_num", "denom": "metric_denom"},
                            "script": "params.denom > 0 ? params.num / params.denom : null",
                        }
                    },
                }
            elif field:
                aggs["over_time"]["aggs"] = {"metric": {"sum": {"field": field}}}
            elif cardinality_field:
                aggs["over_time"]["aggs"] = {"metric": {"cardinality": {"field": cardinality_field}}}

            date_field_filter = source.get("date_field_filter")
            if date_field_filter and dr.get("from") and dr.get("to"):
                # Source uses its own date field (e.g. DT's date_created_ms) instead of the
                # global kibana_date. Rebuild filters without the global date range clause,
                # then add a per-source range on the correct field.
                # route through _date_range_clause so ISO bounds carry
                # ``format: yyyy-MM-dd`` and survive against custom-format date
                # fields like Echo's kibana_date.
                # dashboard filters merge against this source's DV.
                non_date_filters = _build_filters(
                    {
                        "filters": body.get("filters", []),
                        # carry the breakdown-records opt-in so this
                        # custom-date-field trend path honors the tile's checkbox
                        # instead of always excluding breakdown record_types.
                        "include_breakdown_records": body.get("include_breakdown_records"),
                    },
                    dashboard_filters=dashboard_filters,
                    active_data_view=data_view,
                )
                range_clause = _date_range_clause(dr.get("from"), dr.get("to"))
                if range_clause:
                    source_filters = non_date_filters + [{"range": {date_field_filter: range_clause}}]
                else:
                    source_filters = non_date_filters
            else:
                # rebuild base filters per source so dashboard filters
                # apply only to sources whose DV matches the filter's DV.
                source_filters = _build_filters(
                    body,
                    dashboard_filters=dashboard_filters,
                    active_data_view=data_view,
                )
            if query_filter:
                source_filters.append(query_filter)

            query_body = {
                "size": 0,
                "query": ({"bool": {"filter": source_filters}} if source_filters else {"match_all": {}}),
                "aggs": aggs,
            }

            try:
                resp = es.search(
                    index=RoleFilterService.scoped_index_pattern(data_view),
                    body=query_body,
                    ignore_unavailable=True,
                )
                resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
                buckets = resp_dict.get("aggregations", {}).get("over_time", {}).get("buckets", [])
                source_data = {}
                for bucket in buckets:
                    key = bucket.get("key_as_string", "")
                    if is_ms_date and (not key or not key[:4].isdigit()):
                        # Fallback: if _ms field is still mapped as long (pre-template),
                        # key_as_string is garbage — convert epoch key to yyyy-MM.
                        epoch_ms = bucket.get("key")
                        if epoch_ms is None:
                            continue
                        key = datetime.utcfromtimestamp(epoch_ms / 1000).strftime("%Y-%m")
                    if not key:
                        continue
                    all_dates.add(key)
                    if field or cardinality_field:
                        value = (bucket.get("metric") or {}).get("value") or 0
                    else:
                        value = bucket.get("doc_count", 0)
                    source_data[key] = round(value) if value else 0
                results[label] = source_data
            except Exception as e:
                logging.warning(f'Trend line source "{label}" ({data_view}) failed: {e}')
                results[label] = {}

        x_data = sorted(all_dates)
        series = []
        for source in sources:
            label = source.get("label", "")
            series.append(
                {
                    "name": label,
                    "data": [results.get(label, {}).get(d, 0) for d in x_data],
                    "color": source.get("color", "#5470c6"),
                    "category": source.get("category", ""),
                    "default_visible": source.get("default_visible", True),
                }
            )

        return jsonify({"x_data": x_data, "series": series})

    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error getting trend line data",
            log_detail="get_trend_line_data failed",
            exc=e,
            body={},
        )


# ──────────────────────────────────────────────
# Saved Dashboards — stored in de_asset_dashboards
# ──────────────────────────────────────────────
#
# ES document shape:
# {
#   "dashboard_id":     str (UUID, also used as the ES doc _id)
#   "dashboard_type":   "echarts"
#   "title":            str
#   "created_at":       ISO-8601 str
#   "updated_at":       ISO-8601 str
#   "visibility_mode":  "show_all" | "show_teams" | "hide"
#   "allowed_org_esids":  []
#   "allowed_team_esids": []
#   "echarts_config": {
#     "global_date_range": { field, from, to } | null
# "filters": [ { id, title, data_view, field, field_type, selected_values: [...] } ]
#     "charts": [ { chart_id, title, chart_type, data_view, x_field, ... }, ... ]
#   }
# }
#
# Kibana dashboards in the same index do NOT have echarts_config, so we
# filter with exists: {field: echarts_config} to return only ECharts dashboards.
# ──────────────────────────────────────────────


def _es_doc_to_dashboard_state(doc_id, source):
    """Convert a de_asset_dashboards _source dict to the DASHBOARD_STATE shape the JS expects."""
    dashboard_type = source.get("dashboard_type", "echarts")

    if dashboard_type == "leaflet_map":
        return {
            "id": source.get("dashboard_id", doc_id),
            "title": source.get("title", "Untitled"),
            "dashboard_type": "leaflet_map",
            "menu": source.get("menu", "Reports"),
            "visibility_mode": source.get("visibility_mode", "hide"),
            "allowed_org_esids": source.get("allowed_org_esids", []),
            "allowed_team_esids": source.get("allowed_team_esids", []),
            "map_config": source.get("map_config", {}),
            "global_date_range": None,
            "charts": {},
        }

    if dashboard_type == "canvas":
        # canvas documents mirror the leaflet_map
        # branch — config rides in its own sibling key; there are no charts.
        cfg = source.get("canvas_config", {}) or {}
        return {
            "id": source.get("dashboard_id", doc_id),
            "title": source.get("title", "Untitled"),
            "dashboard_type": "canvas",
            "menu": source.get("menu", "Reports"),
            "visibility_mode": source.get("visibility_mode", "hide"),
            "allowed_org_esids": source.get("allowed_org_esids", []),
            "allowed_team_esids": source.get("allowed_team_esids", []),
            "workspace_esid": source.get("workspace_esid", ""),
            "canvas_config": cfg,
            "global_date_range": cfg.get("global_date_range"),
            "filters": cfg.get("filters") or [],
            "time_field_choices": cfg.get("time_field_choices") or [],
            "charts": {},
        }

    cfg = source.get("echarts_config", {})
    charts_list = cfg.get("charts", [])
    return {
        "id": source.get("dashboard_id", doc_id),
        "title": source.get("title", "Untitled"),
        "dashboard_type": "echarts",
        "menu": source.get("menu", "Reports"),
        "visibility_mode": source.get("visibility_mode", "hide"),
        "allowed_org_esids": source.get("allowed_org_esids", []),
        "allowed_team_esids": source.get("allowed_team_esids", []),
        # the dashboard's workspace, so the viewer's GA4 property
        # selector can enumerate that workspace's properties (/api/ga4-properties).
        # Already the tenant key on the read gate; exposing it here is consistent
        # with allowed_*_esids above. Empty on legacy/unstamped dashboards (the
        # selector then stays hidden — no workspace to enumerate).
        "workspace_esid": source.get("workspace_esid", ""),
        "global_date_range": cfg.get("global_date_range"),
        # dashboard-level filters (top-of-dashboard value pickers).
        # Distinct from per-chart filters under each chart's ``filters`` key.
        "filters": cfg.get("filters") or [],
        # author-curated date-basis choices ([{field, label}], first =
        # default). The viewer renders a "Date basis" selector from these so a
        # user can re-scope the global time range on a different date field
        # (e.g. Echo Created vs Closed). Empty/absent -> no selector.
        "time_field_choices": cfg.get("time_field_choices") or [],
        "charts": charts_list,  # Return as array to preserve ES insertion order exactly
        # first-look marker fields. The viewer reads these to
        # toggle the "Data is still syncing" banner on dashboards that
        # were materialised from a per-connector template at the end
        # of onboarding. Absent on legacy / hand-built dashboards.
        "first_look": bool(source.get("first_look", False)),
        "first_look_connector": source.get("first_look_connector"),
        "first_look_created_at": source.get("first_look_created_at"),
    }


@dashboard_bp.route("/api/dashboards", methods=["GET"])
def list_dashboards():
    """Return list of saved ECharts dashboards from de_asset_dashboards.

    Filters results to dashboards the current user is allowed to see
    (admin bypass; otherwise respects ``visibility_mode`` +
    ``allowed_*_esids``) —.

    when ``?team=<title>`` (and optionally ``?org=<title>``) is
    present, the per-row ``published`` flag answers 'is this dashboard
    published for THAT team?' — so the green pill follows the toolbar
    team filter and a dashboard published only for team B does not light
    up for team A's view. Without the param the row's flag still answers
    'is any entry active?' (the pre-664 view, preserved for callers that
    haven't been updated).
    """
    try:
        scope_key: tuple[str, str] | None = None
        team_param = request.args.get("team")
        org_param = request.args.get("org")
        if team_param is not None or org_param is not None:
            scope_key = _publish_scope_key(team_param, org_param)

        # the Builder topbar passes the selected workspace so the
        # explorer shows only THAT workspace's dashboards. This is a UI scoping
        # filter layered ON TOP of the read gate (which may admit more, e.g. a
        # privileged admin sees every workspace). Without it, a superuser who
        # can read all workspaces sees every dashboard in every workspace's
        # menu regardless of which workspace they're working in. Legacy
        # dashboards with no workspace_esid stamp pass through (can't be
        # attributed to a workspace; hiding them would strand them).
        selected_workspace = (request.args.get("workspace_esid") or "").strip()

        # Use match_all and filter in Python — avoids dependency on echarts_config
        # being indexed (index may have dynamic mapping disabled).
        hits = get_all_documents(DASHBOARDS_INDEX)
        dashboards = []
        # (workspace, owning team) per row, for the duplicate-title labels.
        label_meta: dict[str, tuple[str, str | None]] = {}
        for hit in hits:
            source = hit.get("_source", {})
            # "canvas" joins the listable types so canvas documents
            # appear in the Builder explorer. (Since the end-user
            # /dashboards sidebar and the public /public/report/<token> viewer
            # consume canvas pages too.)
            if source.get("dashboard_type") not in ("echarts", "leaflet_map", "canvas"):
                continue
            if not _user_may_read_dashboard(source):
                continue
            # scope the listing to the selected workspace (UI filter).
            if selected_workspace:
                doc_ws = source.get("workspace_esid") or ""
                if doc_ws and doc_ws != selected_workspace:
                    continue
            doc_id = hit.get("_id", "")

            if scope_key is None:
                active_entry = _any_active_entry(source)
            else:
                entry = _find_publish_entry(source, scope_key)
                active_entry = entry if _entry_is_active(entry or {}) else None

            published_active = active_entry is not None
            # follow-up: ``shared`` answers "is this dashboard visible to
            # anyone besides its creator?" Under teams-only a ``show_teams``
            # dashboard with no allowed teams/orgs is private-to-creator (the
            # replacement for the old ``hide``); the explorer strikes it through
            # so an editor can tell it isn't shared. Legacy ``show_all`` is
            # always shared; legacy ``hide`` is not. Computed server-side so the
            # client needn't receive the raw scope ESIDs.
            _vis = source.get("visibility_mode", "hide")
            if _vis == "show_all":
                _shared = True
            elif _vis == "show_teams":
                _shared = bool(source.get("allowed_team_esids") or source.get("allowed_org_esids"))
            else:  # hide / unknown
                _shared = False
            row_id = source.get("dashboard_id", doc_id)
            label_meta[row_id] = (source.get("workspace_esid") or "", owner_team_esid(source))
            dashboards.append(
                {
                    "id": row_id,
                    "title": source.get("title", "Untitled"),
                    # the explorer routes canvas rows to the canvas
                    # editor page instead of loading them into the grid builder.
                    "dashboard_type": source.get("dashboard_type", "echarts"),
                    "updated_at": (source.get("updated_at", "") or "")[:10],  # YYYY-MM-DD
                    "menu": source.get("menu", ""),
                    "menu_position": source.get("menu_position"),
                    "visibility_mode": _vis,
                    "shared": _shared,
                    "published": published_active,
                    "published_expires_at": (active_entry or {}).get("publish_expires_at") if published_active else None,
                }
            )
        dashboards.sort(key=lambda d: d["updated_at"], reverse=True)
        # name the owning team on same-titled rows (one starter-pack
        # copy per team). A non-privileged viewer only ever sees their own
        # teams' names; a privileged one already sees every team's copy.
        apply_duplicate_team_labels(dashboards, label_meta, _label_viewer_team_esids())
        return jsonify({"success": True, "dashboards": dashboards})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error listing dashboards",
            log_detail="list_dashboards failed",
            exc=e,
        )


# Caps for save_dashboard payload — match the HTML maxlength / min / max
# attributes on the dashboard-builder inputs. Defense-in-depth: any caller
# bypassing the form (scripted POST, replay attack, etc.) is held to the
# same limits.
_MAX_DASHBOARD_TITLE = 200
_MAX_LABEL_LEN = 200

# follow-up — server-side ceilings for the remaining builder
# write surfaces, mirroring each input's maxlength (properties PATCH,
# menu create/rename, template freeze/update, export title). The main
# dashboard save was already covered by _validate_dashboard_payload.
_MAX_MENU_TITLE = 200
_MAX_EXPORT_TITLE = 100
_MAX_TEMPLATE_NAME = 200
_MAX_TEMPLATE_DESCRIPTION = 500
_MAX_TEMPLATE_ID = 80
_MAX_PACK_ID = 40
_MAX_TEMPLATE_MENU_TITLE = 60
# The builder modal enforces slug shape client-side (pattern attr,
# [a-z0-9_]); the server-side belt additionally tolerates hyphens so a
# legacy registry id can't be locked out of the Edit Template flow.
_TEMPLATE_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")


def _validate_template_metadata(
    *,
    template_id: str,
    pack_id: str,
    name: str,
    description: str,
    menu_title: str,
) -> str | None:
    """Length + slug-shape caps shared by the freeze and update-template
    routes. Returns an error string, or None when everything fits."""
    if len(template_id) > _MAX_TEMPLATE_ID:
        return f"template_id must be {_MAX_TEMPLATE_ID} characters or fewer."
    if template_id and not _TEMPLATE_SLUG_RE.fullmatch(template_id):
        return "template_id may only contain lowercase letters, digits, and underscores."
    if len(pack_id) > _MAX_PACK_ID:
        return f"pack_id must be {_MAX_PACK_ID} characters or fewer."
    if pack_id and not _TEMPLATE_SLUG_RE.fullmatch(pack_id):
        return "pack_id may only contain lowercase letters, digits, and underscores."
    if len(name) > _MAX_TEMPLATE_NAME:
        return f"name must be {_MAX_TEMPLATE_NAME} characters or fewer."
    if len(description) > _MAX_TEMPLATE_DESCRIPTION:
        return f"description must be {_MAX_TEMPLATE_DESCRIPTION} characters or fewer."
    if len(menu_title) > _MAX_TEMPLATE_MENU_TITLE:
        return f"menu_title must be {_MAX_TEMPLATE_MENU_TITLE} characters or fewer."
    return None


_MAX_CHARTS_PER_DASHBOARD = 100
_NUMERIC_BOUNDS = {
    "max_columns": (1, 10),
    "max_rows": (1, 100),
    "num_values_to_show": (1, 1000),
}

# chart_id and y_fields are rendered into the builder / viewer
# DOM (element ids, data-* attributes). The client now escapes them, and the
# server refuses shapes no legitimate minter produces: chart_id from the
# builder ("c" + Date.now()), template slugs, or UUIDs; y_fields are ES field
# names. Prod scan 2026-10-07: 0 of 213 chart_ids / 419 y_fields fail these.
_CHART_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_Y_FIELD_FORBIDDEN_RE = re.compile(r"['\"\\<>\x00-\x1f]")


def _validate_chart_identifiers(chart: dict, prefix: str) -> str | None:
    """Reject a chart_id / y_fields entry that could not come from the builder.
    Returns an error string, or None when the chart's identifiers are clean."""
    chart_id = chart.get("chart_id")
    if chart_id is not None and not (isinstance(chart_id, str) and _CHART_ID_RE.fullmatch(chart_id)):
        return f"{prefix}.chart_id may only contain letters, digits, '_' and '-' (max 64)."
    y_fields = chart.get("y_fields")
    candidates = list(y_fields) if isinstance(y_fields, list) else []
    if chart.get("y_field") is not None:
        candidates.append(chart.get("y_field"))
    for yf in candidates:
        if isinstance(yf, str) and _Y_FIELD_FORBIDDEN_RE.search(yf):
            return f"{prefix}.y_fields contains a field name with a forbidden character."
    return None


# server-side caps for canvas documents (dashboard_type "canvas",
# epic). DOM rendering tops out at hundreds of objects per page
#, so the object cap sits well under that while leaving
# report pages room. Page dimensions are raw CSS px at 100% zoom.
_MAX_CANVAS_OBJECTS = 200
_CANVAS_PAGE_MIN_PX = 200
_CANVAS_PAGE_MAX_PX = 10000
_CANVAS_ORIENTATIONS = ("landscape", "portrait")
_CANVAS_BACKGROUND_TYPES = ("color", "image")
# A page background image is stored as a data: URI inside canvas_config —
# ~1MB of base64 ≈ 750KB of image, plenty for a page backdrop. Object-level
# image storage is decided separately.
_MAX_CANVAS_BG_VALUE = 1_000_000
# Text objects hold paragraph-length content; the generic 200-char string
# cap is for labels/ids, not body copy.
_MAX_CANVAS_TEXT_LEN = 5000
_MAX_CANVAS_GUIDES = 50
# rich text: text.rich is a structured block/run model (never
# HTML). Blocks are p / ul / ol; runs carry bold/italic/underline plus
# color/font TOKEN KEYS and a size step — the client renderer drops any
# key/size outside its cvTextTokens() set, so the server bounds shape and
# volume here (defense-in-depth, same posture as the rest of this file).
_MAX_CANVAS_RICH_BLOCKS = 200
_CANVAS_RICH_BLOCK_TYPES = ("p", "ul", "ol")
_CANVAS_TEXT_ALIGNS = ("left", "center", "right")
# shapes + images. Shape objects carry a closed kind set (the
# renderer only knows these five). Image objects store their pixels as a
# data: URI inside canvas_config (the private docs 2026-07-13 — same
# posture as the page background): one image shares the background's
# per-value ceiling, and the whole document carries a total budget so a
# 200-object page can't accrete an ES doc measured in tens of MB.
_CANVAS_SHAPE_KINDS = ("rect", "ellipse", "line", "arrow", "callout")
# live chart objects (world map / trend small-multiples); the
# renderers (canvas_charts.js) only know these kinds, and a trend chart's
# series list is bounded so a crafted doc can't fan out unbounded queries.
_CANVAS_CHART_KINDS = ("geo", "trend", "bar")  # adds bar
_MAX_CANVAS_CHART_SERIES = 10
# per-binding row filters ({field, value} term-equality pairs).
# Bounded so a crafted save can't fan a single data field out into an
# unbounded ES filter clause list. The AI schema caps tighter (5).
_MAX_CANVAS_BINDING_FILTERS = 10
# calculated fields: obj.calc = {formula, variables}. The formula
# is the calculation-column grammar, validated by the SAME
# engine (services.dashboard_formula.parse_formula — no second formula
# system); each variable is a earlier metric binding. The variable
# count is bounded like binding filters so a crafted save can't fan a single
# object out into an unbounded query_batch.
_MAX_CANVAS_CALC_VARIABLES = 10
_CANVAS_CALC_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# table objects carry a verbatim legacy table chart config under
# ``table``. Only the aggregation Table is canvas-eligible (posts_table's
# public query path is bespoke); the column cap bounds the per-render ES agg
# fan-out the same way the chart series cap does. The legacy builder has no
# cap, so this sits well above any real dashboard's column count.
_CANVAS_TABLE_CHART_TYPES = ("table",)
_MAX_CANVAS_TABLE_COLUMNS = 40
_MAX_CANVAS_IMAGE_VALUE = 1_000_000
_MAX_CANVAS_TOTAL_IMAGE_CHARS = 6_000_000
_CANVAS_IMAGE_SRC_RE = re.compile(r"^data:image/(png|jpeg|gif|webp|svg\+xml)[;,]")
# SVG active content. Rendering is <img>-only on every surface (editor,
# preview, iframe viewer, public viewer), which never executes scripts —
# this server-side rejection is the second, independent layer.
_CANVAS_SVG_FORBIDDEN_RE = re.compile(r"<\s*script|javascript:|<\s*foreignobject|\son[a-z]+\s*=", re.IGNORECASE)


def _validate_canvas_image_src(src: object, obj_index: int) -> str | None:
    """bound an image object's ``image.src`` data URI.

    Must be a data:image/... URI of an allowed format, under the per-image
    ceiling; SVG payloads are additionally decoded and rejected when they
    carry active content (script tags, event-handler attributes,
    foreignObject, javascript: URLs).
    """
    prefix = f"objects[{obj_index}].image.src"
    if not isinstance(src, str) or not src:
        return f"{prefix} must be a data:image/... URI."
    m = _CANVAS_IMAGE_SRC_RE.match(src)
    if not m:
        return f"{prefix} must be a data:image/png|jpeg|gif|webp|svg+xml URI."
    if len(src) > _MAX_CANVAS_IMAGE_VALUE:
        return f"{prefix} is too large (max ~750KB per image)."
    if m.group(1) == "svg+xml":
        return _validate_canvas_svg_payload(src, prefix)
    return None


def _validate_canvas_svg_payload(src: str, prefix: str) -> str | None:
    """Decode an SVG data URI (base64 or URL-encoded) and reject active content."""
    import base64
    from urllib.parse import unquote

    header, _, payload = src.partition(",")
    try:
        if ";base64" in header:
            text = base64.b64decode(payload, validate=False).decode("utf-8", "replace")
        else:
            text = unquote(payload)
    except Exception:
        return f"{prefix} is not a decodable SVG data URI."
    if _CANVAS_SVG_FORBIDDEN_RE.search(text):
        return f"{prefix} SVG contains active content (scripts/event handlers) — not allowed."
    return None


def _validate_canvas_binding_filters(bfilters: object, prefix: str) -> str | None:
    """shape + cap for a metric binding's row-filter list.

    Shared by data-field bindings and calculated-field variable
    bindings — both carry the same ``[{field, value}, ...]`` shape.
    """
    if not isinstance(bfilters, list):
        return f"{prefix} must be a list."
    if len(bfilters) > _MAX_CANVAS_BINDING_FILTERS:
        return f"{prefix} must have at most {_MAX_CANVAS_BINDING_FILTERS} entries."
    for fi, bf in enumerate(bfilters):
        if not isinstance(bf, dict) or not isinstance(bf.get("field"), str) or not bf.get("field"):
            return f"{prefix}[{fi}] must be an object with a 'field'."
    return None


def _validate_canvas_time_override(ov: object, prefix: str) -> str | None:
    """shape check for a binding's time-range override.

    Presence of the key means the field queries its own range and ignores
    the page's time-range filter (``bindingQuery`` in both canvas JS files,
    resolved by the shared ``cvResolveTimeOverride``). ``preset`` is pinned
    to the TIME_PRESETS catalog (the toolbar's option set) plus ``custom``;
    ``custom`` — and the pre-preset ``{from, to}`` shape — carries fixed
    date strings. The strings fall under the generic label cap; this pins
    the shape so a crafted save can't persist a structure the client would
    misread.
    """
    from asset_management.web.services.time_preset_service import TIME_PRESETS

    if not isinstance(ov, dict):
        return f"{prefix} must be an object."
    preset = ov.get("preset")
    if preset is not None:
        valid = {p["value"] for p in TIME_PRESETS} | {"custom"}
        if not isinstance(preset, str) or preset not in valid:
            return f"{prefix}.preset must be one of the time-range presets or 'custom'."
    for key in ("from", "to"):
        v = ov.get(key)
        if v is not None and not isinstance(v, str):
            return f"{prefix}.{key} must be a string."
    if (preset is None or preset == "custom") and not ov.get("from") and not ov.get("to"):
        return f"{prefix} must set 'from' and/or 'to'."
    return None


def _validate_canvas_calc_field(obj: dict, obj_index: int) -> str | None:
    """shape checks for a calculated-field object's ``calc``.

    The formula is validated by the SAME engine as the Dashboard Builder's
    calculation columns (``services.dashboard_formula.parse_formula`` — the
    authoritative gate; the browser-side parser is UX only). An empty
    formula is a not-yet-configured field (the + Calc skeleton, same posture
    as a data field with ``binding: null``) and passes; a non-empty formula
    must parse and every referenced name must be defined in
    ``calc.variables``, each variable a earlier metric binding.
    """
    from asset_management.web.services.dashboard_formula import parse_formula

    prefix = f"objects[{obj_index}].calc"
    calc = obj.get("calc")
    if calc is None:
        return None
    if not isinstance(calc, dict):
        return f"{prefix} must be an object."

    formula = calc.get("formula", "")
    if formula is not None and not isinstance(formula, str):
        return f"{prefix}.formula must be a string."

    variables = calc.get("variables")
    if variables is not None and not isinstance(variables, dict):
        return f"{prefix}.variables must be an object."
    variables = variables if isinstance(variables, dict) else {}
    if len(variables) > _MAX_CANVAS_CALC_VARIABLES:
        return f"{prefix}.variables must have at most {_MAX_CANVAS_CALC_VARIABLES} entries."
    for vname, vbind in variables.items():
        if not isinstance(vname, str) or not _CANVAS_CALC_VAR_NAME_RE.match(vname):
            return f"{prefix}.variables names must be identifiers (letters, digits, underscores)."
        if (
            not isinstance(vbind, dict)
            or not isinstance(vbind.get("data_view"), str)
            or not vbind.get("data_view")
            or not isinstance(vbind.get("field"), str)
            or not vbind.get("field")
        ):
            return f"{prefix}.variables['{vname}'] must be an object with a data_view and a field."
        if vbind.get("filters") is not None:
            err = _validate_canvas_binding_filters(vbind["filters"], f"{prefix}.variables['{vname}'].filters")
            if err:
                return err

    if isinstance(formula, str) and formula.strip():
        parsed = parse_formula(formula)
        if not parsed.get("ok"):
            return f"{prefix}.formula is invalid: {parsed.get('error')}"
        missing = [f for f in parsed["fields"] if f not in variables]
        if missing:
            return f"{prefix}.formula references undefined variable(s): {', '.join(missing)}."
    return None


def _validate_canvas_style_tokens(style: object, obj_index: int) -> str | None:
    """shape + bounds for an object's token-capable ``style`` fields.

    Color fields may hold a raw CSS color OR a theme-token key — both are
    short strings, so one bound covers both (same posture as the rich-text
    run color/font keys). The client resolvers pass unknown keys through
    raw, so nothing here needs to check membership — only shape and volume.

    Font-size fields must stay NUMBERS (user decision 2026-07-17): the live
    index maps ``style.font_size`` as ``long`` (dynamic mapping from years
    of numeric docs), so a string scale key would pass this gate and then
    400 at ES index time. Colors-only tokens for v1; size tokens wait on a
    canvas_config ``dynamic: false`` reindex migration (see DECISIONS.md).
    """
    if style is None:
        return None
    if not isinstance(style, dict):
        return f"objects[{obj_index}].style must be an object."
    prefix = f"objects[{obj_index}].style"
    for field in canvas_theme.COLOR_STYLE_FIELDS:
        value = style.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > canvas_theme.MAX_TOKEN_KEY_LEN):
            return f"{prefix}.{field} must be a color value or theme token key (short string)."
    for field in canvas_theme.FONT_SIZE_STYLE_FIELDS:
        value = style.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{prefix}.{field} must be a number (theme size token keys are not supported yet)."
    return None


def _validate_canvas_rich_text(rich: object, obj_index: int) -> str | None:
    """shape + caps for a text object's ``text.rich`` model.

    The total run text shares the ``_MAX_CANVAS_TEXT_LEN`` budget with the
    plain mirror; individual runs are exempt from the generic 200-char label
    cap because they ARE body copy (the caller carves ``rich`` out of the
    generic walk for that reason).
    """
    prefix = f"objects[{obj_index}].text.rich"
    if not isinstance(rich, list):
        return f"{prefix} must be a list."
    if len(rich) > _MAX_CANVAS_RICH_BLOCKS:
        return f"{prefix} has too many blocks (max {_MAX_CANVAS_RICH_BLOCKS})."
    total = 0
    for bi, block in enumerate(rich):
        if not isinstance(block, dict):
            return f"{prefix}[{bi}] must be an object."
        btype = block.get("type", "p")
        if btype not in _CANVAS_RICH_BLOCK_TYPES:
            return f"{prefix}[{bi}].type must be one of: {', '.join(_CANVAS_RICH_BLOCK_TYPES)}."
        align = block.get("align")
        if align is not None and align not in _CANVAS_TEXT_ALIGNS:
            return f"{prefix}[{bi}].align must be left, center, or right."
        run_lists = [block.get("runs") or []] if btype == "p" else (block.get("items") or [])
        if not isinstance(run_lists, list):
            return f"{prefix}[{bi}].items must be a list."
        for runs in run_lists:
            if not isinstance(runs, list):
                return f"{prefix}[{bi}] run lists must be lists."
            for run in runs:
                if not isinstance(run, dict):
                    return f"{prefix}[{bi}] runs must be objects."
                text = run.get("t", "")
                if not isinstance(text, str):
                    return f"{prefix}[{bi}] run text (t) must be a string."
                total += len(text)
                for key in ("color", "font"):
                    value = run.get(key)
                    if value is not None and (not isinstance(value, str) or len(value) > 40):
                        return f"{prefix}[{bi}] run {key} must be a short token key."
    if total > _MAX_CANVAS_TEXT_LEN:
        return f"{prefix} exceeds {_MAX_CANVAS_TEXT_LEN} characters of text."
    return None


def _validate_canvas_payload(data: dict) -> str | None:
    """caps + shape checks for a ``dashboard_type: "canvas"`` save.

    Mirrors what the canvas editor UI enforces client-side so a crafted or
    scripted POST is held to the same limits (defense-in-depth, same posture
    as the charts branch of ``_validate_dashboard_payload``). The background
    ``value`` is exempted from the generic string cap because an image
    background is a data: URI with its own (much larger) ceiling.
    """
    cfg = data.get("canvas_config")
    if not isinstance(cfg, dict):
        return "canvas_config must be an object."

    page = cfg.get("page")
    if not isinstance(page, dict):
        return "canvas_config.page must be an object."

    for dim in ("width", "height"):
        try:
            v = int(page.get(dim))
        except (TypeError, ValueError):
            return f"canvas_config.page.{dim} must be a number."
        if v < _CANVAS_PAGE_MIN_PX or v > _CANVAS_PAGE_MAX_PX:
            return f"canvas_config.page.{dim} must be between {_CANVAS_PAGE_MIN_PX} and {_CANVAS_PAGE_MAX_PX}."

    orientation = page.get("orientation", "landscape")
    if orientation not in _CANVAS_ORIENTATIONS:
        return "canvas_config.page.orientation must be 'landscape' or 'portrait'."

    # page-level theme reference: a short token key. Unknown keys
    # are ALLOWED (the client falls back to the classic theme, and
    # library will grow the set) — only shape and length are pinned here.
    theme_ref = page.get("theme")
    if theme_ref is not None and (
        not isinstance(theme_ref, str) or not theme_ref or len(theme_ref) > canvas_theme.MAX_TOKEN_KEY_LEN
    ):
        return "canvas_config.page.theme must be a short theme key string."

    background = page.get("background") or {}
    if not isinstance(background, dict):
        return "canvas_config.page.background must be an object."
    bg_type = background.get("type", "color")
    if bg_type not in _CANVAS_BACKGROUND_TYPES:
        return "canvas_config.page.background.type must be 'color' or 'image'."
    bg_value = background.get("value", "")
    if not isinstance(bg_value, str):
        return "canvas_config.page.background.value must be a string."
    if bg_type == "image":
        if bg_value and not bg_value.startswith("data:image/"):
            return "canvas_config.page.background.value must be a data:image/... URI for image backgrounds."
        if len(bg_value) > _MAX_CANVAS_BG_VALUE:
            return "Background image is too large (max ~750KB)."
    elif len(bg_value) > _MAX_LABEL_LEN:
        return f"canvas_config.page.background.value exceeds {_MAX_LABEL_LEN} characters."

    guides = page.get("guides") or {}
    if not isinstance(guides, dict):
        return "canvas_config.page.guides must be an object."
    for axis in ("h", "v"):
        positions = guides.get(axis) or []
        if not isinstance(positions, list):
            return f"canvas_config.page.guides.{axis} must be a list."
        if len(positions) > _MAX_CANVAS_GUIDES:
            return f"Too many guides (max {_MAX_CANVAS_GUIDES} per axis)."
        if not all(isinstance(p, (int, float)) and not isinstance(p, bool) for p in positions):
            return f"canvas_config.page.guides.{axis} must contain only numbers."

    objects = cfg.get("objects", [])
    if not isinstance(objects, list):
        return "canvas_config.objects must be a list."
    if len(objects) > _MAX_CANVAS_OBJECTS:
        return f"Too many canvas objects (max {_MAX_CANVAS_OBJECTS})."

    # Generic string cap over everything else, with page.background.value
    # (data URI), image.src (data URI), and text.content (body copy) carved
    # out to their own caps. Shallow copies only — the config isn't mutated.
    # the total image budget spans every image object PLUS the page
    # background so the document as a whole stays bounded.
    total_image_chars = len(bg_value) if bg_type == "image" else 0
    for i, obj in enumerate(objects):
        if not isinstance(obj, dict):
            continue
        # icon objects must reference an existing library file by
        # bare filename — icon.name is joined into a static URL client-side,
        # so a crafted path segment must never persist.
        if obj.get("object_type") == "icon":
            icon_name = (obj.get("icon") or {}).get("name") if isinstance(obj.get("icon"), dict) else None
            if not icon_library_has(icon_name):
                return f"objects[{i}].icon.name must be an existing icon-library file."
        # shape objects carry a closed kind set — the renderers
        # (editor and viewer) only know these five.
        if obj.get("object_type") == "shape":
            kind = (obj.get("shape") or {}).get("kind") if isinstance(obj.get("shape"), dict) else None
            if kind not in _CANVAS_SHAPE_KINDS:
                return f"objects[{i}].shape.kind must be one of: {', '.join(_CANVAS_SHAPE_KINDS)}."
        # token-capable style fields — color slots must be short
        # strings (raw color or theme-token key), font sizes numbers or
        # scale keys.
        err = _validate_canvas_style_tokens(obj.get("style"), i)
        if err:
            return err
        # data-field binding row filters — a bounded list of
        # {field, value} objects (the editor UI and AI emit a handful; the
        # value strings fall under the generic label cap below).
        obj_binding = obj.get("binding")
        if isinstance(obj_binding, dict) and obj_binding.get("filters") is not None:
            err = _validate_canvas_binding_filters(obj_binding["filters"], f"objects[{i}].binding.filters")
            if err:
                return err
        # per-field time-range override — {from, to} strings.
        if isinstance(obj_binding, dict) and obj_binding.get("time_override") is not None:
            err = _validate_canvas_time_override(obj_binding["time_override"], f"objects[{i}].binding.time_override")
            if err:
                return err
        # calculated fields — formula (validated by the shared
        # dashboard_formula engine) over named variable bindings.
        if obj.get("object_type") == "calc_field":
            err = _validate_canvas_calc_field(obj, i)
            if err:
                return err
        # chart objects — closed kind set, bounded series fan-out.
        if obj.get("object_type") == "chart":
            chart_payload = obj.get("chart") if isinstance(obj.get("chart"), dict) else {}
            if chart_payload.get("kind") not in _CANVAS_CHART_KINDS:
                return f"objects[{i}].chart.kind must be one of: {', '.join(_CANVAS_CHART_KINDS)}."
            series = chart_payload.get("series", [])
            if series is not None and not isinstance(series, list):
                return f"objects[{i}].chart.series must be a list."
            if isinstance(series, list) and len(series) > _MAX_CANVAS_CHART_SERIES:
                return f"objects[{i}].chart.series must have at most {_MAX_CANVAS_CHART_SERIES} series."
        # table objects — the payload is a legacy table chart
        # config rendered by charts.js renderTable. Shape-check the dict,
        # pin the chart_type to the closed set, and bound the column list;
        # every string inside falls under the generic label cap below.
        if obj.get("object_type") == "table":
            table_payload = obj.get("table")
            if not isinstance(table_payload, dict):
                return f"objects[{i}].table must be an object (a table chart config)."
            if table_payload.get("chart_type") not in _CANVAS_TABLE_CHART_TYPES:
                return f"objects[{i}].table.chart_type must be one of: {', '.join(_CANVAS_TABLE_CHART_TYPES)}."
            table_cols = table_payload.get("table_columns")
            if table_cols is not None and not isinstance(table_cols, list):
                return f"objects[{i}].table.table_columns must be a list."
            if isinstance(table_cols, list):
                if len(table_cols) > _MAX_CANVAS_TABLE_COLUMNS:
                    return f"objects[{i}].table.table_columns must have at most {_MAX_CANVAS_TABLE_COLUMNS} columns."
                for ci, col in enumerate(table_cols):
                    if not isinstance(col, dict):
                        return f"objects[{i}].table.table_columns[{ci}] must be an object."
        # image objects — allowed data URI formats, per-image cap,
        # SVG active-content rejection, and the running total budget.
        obj_image = obj.get("image")
        if obj.get("object_type") == "image":
            src = obj_image.get("src") if isinstance(obj_image, dict) else None
            err = _validate_canvas_image_src(src, i)
            if err:
                return err
            total_image_chars += len(src)
        text_payload = obj.get("text")
        obj_rest = {k: v for k, v in obj.items() if k not in ("text", "image")}
        if isinstance(obj_image, dict):
            # image.src has its own (much larger) ceiling above — keep the
            # generic label cap over the image dict's other keys.
            obj_rest["image"] = {k: v for k, v in obj_image.items() if k != "src"}
        if isinstance(text_payload, dict):
            content = text_payload.get("content")
            if isinstance(content, str) and len(content) > _MAX_CANVAS_TEXT_LEN:
                return f"objects[{i}].text.content exceeds {_MAX_CANVAS_TEXT_LEN} characters."
            # rich runs are body copy — validated by their own
            # shape/volume checks, carved out of the generic label cap.
            rich = text_payload.get("rich")
            if rich is not None:
                err = _validate_canvas_rich_text(rich, i)
                if err:
                    return err
            obj_rest["text"] = {k: v for k, v in text_payload.items() if k not in ("content", "rich")}
        err = _walk_strings_for_cap(obj_rest, _MAX_LABEL_LEN, prefix=f"canvas_config.objects[{i}]")
        if err:
            return err

    if total_image_chars > _MAX_CANVAS_TOTAL_IMAGE_CHARS:
        return "The page's images total too much data (max ~4.5MB across all images and the background)."

    page_rest = {k: v for k, v in page.items() if k != "background"}
    background_rest = {k: v for k, v in background.items() if k != "value"}
    cfg_rest = {k: v for k, v in cfg.items() if k not in ("page", "objects")}
    for node, prefix in (
        (page_rest, "canvas_config.page"),
        (background_rest, "canvas_config.page.background"),
        (cfg_rest, "canvas_config"),
    ):
        err = _walk_strings_for_cap(node, _MAX_LABEL_LEN, prefix=prefix)
        if err:
            return err

    return None


def _validate_dashboard_payload(data: dict) -> str | None:
    """Return an error string if the dashboard payload violates a cap, else None."""
    title = data.get("title")
    if isinstance(title, str) and len(title) > _MAX_DASHBOARD_TITLE:
        return f"Dashboard title must be {_MAX_DASHBOARD_TITLE} characters or fewer."

    # canvas documents carry canvas_config instead of charts —
    # validate that shape and skip the chart caps (a canvas save has none).
    if (data.get("dashboard_type") or "echarts") == "canvas":
        return _validate_canvas_payload(data)

    charts_raw = data.get("charts", [])
    if isinstance(charts_raw, dict):
        charts_list = list(charts_raw.values())
    elif isinstance(charts_raw, list):
        charts_list = charts_raw
    else:
        return "Charts payload must be a list or dict."

    if len(charts_list) > _MAX_CHARTS_PER_DASHBOARD:
        return f"Too many charts (max {_MAX_CHARTS_PER_DASHBOARD})."

    for i, chart in enumerate(charts_list):
        if not isinstance(chart, dict):
            continue
        # String length cap — applied to every string anywhere in the chart
        # config (titles, labels, table headers, org names, etc.). Preferable
        # to enumerating every field name since the config schema evolves.
        err = _walk_strings_for_cap(chart, _MAX_LABEL_LEN, prefix=f"chart[{i}]")
        if err:
            return err
        err = _validate_chart_identifiers(chart, prefix=f"chart[{i}]")
        if err:
            return err
        # Numeric bounds for the fields the dashboard-builder UI restricts.
        for nfield, (lo, hi) in _NUMERIC_BOUNDS.items():
            v = chart.get(nfield)
            if v is None or v == "":
                continue
            try:
                vi = int(v)
            except (TypeError, ValueError):
                return f"chart[{i}].{nfield} must be a number."
            if vi < lo or vi > hi:
                return f"chart[{i}].{nfield} must be between {lo} and {hi}."

    return None


def _walk_strings_for_cap(node, max_len: int, prefix: str = "") -> str | None:
    """Recursively cap every string in a nested dict/list. Returns error or None."""
    if isinstance(node, dict):
        for k, v in node.items():
            err = _walk_strings_for_cap(v, max_len, f"{prefix}.{k}" if prefix else str(k))
            if err:
                return err
    elif isinstance(node, list):
        for i, item in enumerate(node):
            err = _walk_strings_for_cap(item, max_len, f"{prefix}[{i}]")
            if err:
                return err
    elif isinstance(node, str):
        if len(node) > max_len:
            return f"{prefix} exceeds {max_len} characters."
    return None


@dashboard_bp.route("/api/dashboards", methods=["POST"])
def save_dashboard():
    """Save (create or overwrite) an ECharts dashboard in de_asset_dashboards."""
    try:
        _require_builder_write()

        # ``silent=True`` so a malformed/empty body returns ``None`` instead
        # of raising Flask's default 400 HTML — callers of this JSON API
        # rely on ``{"success": False, "error": ...}`` shape for all errors.
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        # Server-side caps — enforced regardless of what the client sends.
        # Mirrors the maxlength="200" / min=1 max={10,100,1000} attributes on
        # the dashboard-builder inputs so a tampered or scripted POST can't
        # bypass them.
        validation_err = _validate_dashboard_payload(data)
        if validation_err:
            return jsonify({"success": False, "error": validation_err}), 400

        # this route persists echarts (default) and canvas
        # dashboards. leaflet_map docs are seeded by scripts, never through
        # this endpoint — reject any other type rather than silently coercing
        # it to echarts.
        payload_type = data.get("dashboard_type") or "echarts"
        if payload_type not in ("echarts", "canvas"):
            return jsonify({"success": False, "error": f"Unsupported dashboard_type '{payload_type}'."}), 400

        # teams-only visibility. Collapse the write to
        # ``show_teams`` + caller-scoped ``allowed_team_esids`` (no ``show_all``,
        # no ``hide``, no org scope), enforcing the own-teams constraint
        # server-side. Supersedes the ``_enforce_visibility_scope``
        # show_all reject — that path can no longer be reached from the UI and
        # is normalized away here regardless of crafted input.
        (
            write_visibility_mode,
            write_allowed_org_esids,
            write_allowed_team_esids,
        ) = _normalize_write_visibility_scope(data)
        visibility_in_payload = any(k in data for k in ("visibility_mode", "allowed_org_esids", "allowed_team_esids"))

        dashboard_id = data.get("id") or str(uuid.uuid4())
        now = datetime.now(tz=timezone.utc).isoformat()

        # charts may arrive as a list (serialised) or a dict (in-memory)
        charts_raw = data.get("charts", [])
        if isinstance(charts_raw, dict):
            charts_list = list(charts_raw.values())
        else:
            charts_list = charts_raw

        es = get_elasticsearch_client()
        if es is None:
            return (
                jsonify({"success": False, "error": "Elasticsearch unavailable"}),
                503,
            )

        # Look up existing fields to preserve on update
        created_at = now
        # regression fix: stamp the creator so they can always read
        # their own dashboard back. Under teams-only visibility a creator with
        # no team scope (a bare superuser/admin, or any dashboard that saved
        # with zero teams) would otherwise fail the show_teams scope check and
        # never see what they just created. Default to the current user on
        # create; preserve the original creator on overwrite.
        created_by = session.get("username", "") or ""
        existing_menu: str | None = None
        # #1 / workspace_esid is the tenant boundary for
        # dashboards. On update we preserve the existing value (a
        # workspace migration would move it via a separate tool, not via
        # a dashboard save); on create we stamp the creator's primary
        # workspace_esid so visibility queries can intersect against it.
        existing_workspace_esid: str | None = None
        preserved_publish: dict = {}
        try:
            existing = es.get(index=DASHBOARDS_INDEX, id=dashboard_id)
            existing_source = existing["_source"]
            # a save can only overwrite a doc of the SAME type — a
            # canvas payload aimed at an existing echarts id (or vice versa)
            # would silently convert the dashboard and orphan its config.
            existing_type = existing_source.get("dashboard_type", "echarts")
            if existing_type != payload_type:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": f"Dashboard {dashboard_id} is type '{existing_type}', not '{payload_type}'.",
                        }
                    ),
                    400,
                )
            # (folds in the overwrite follow-up): overwriting an
            # existing dashboard is a mutation, so gate it with the same
            # per-dashboard mutate-scope check as delete — not the looser
            # read gate. A non-workspace-admin may overwrite only a dashboard
            # whose team scope intersects their own; a legacy ``show_all`` doc
            # (or one scoped to another org/team) is denied. Workspace admins /
            # superusers keep full overwrite. Prevents a builder on team A from
            # rewriting a team-B-scoped or workspace-shared dashboard.
            # the read gate first — an overwrite by id must not reach
            # a dashboard the caller cannot even open (another workspace's).
            if not _user_may_read_dashboard(existing_source):
                _abort_json(403, "You do not have access to this dashboard.")
            _require_dashboard_mutate_scope(existing_source)
            created_at = existing_source.get("created_at", now)
            created_by = existing_source.get("created_by") or created_by  # preserve original creator
            existing_menu = existing_source.get("menu") or None
            existing_workspace_esid = existing_source.get("workspace_esid") or None
            # Publish state is owned by the dedicated publish endpoints; preserve it
            # across generic dashboard saves so a user editing the dashboard does not
            # silently unpublish it.
            # also preserve the per-(dashboard, scope) ``publishes`` list
            # AND the top-level ``publish_tokens_lookup`` mirror array used by
            # the token-lookup query.
            for key in (
                "publish_token",
                "publish_enabled",
                "published_at",
                "publish_expires_at",
                "published_org_title",
                "published_team_title",
                "display_time_selector",
                "publishes",
                "publish_tokens_lookup",
            ):
                if key in existing_source:
                    preserved_publish[key] = existing_source[key]
            # visibility is owned by the Properties dialog (PATCH
            # /properties). The Canvas Editor's content save omits the
            # visibility keys entirely; normalizing that omission to
            # ``show_teams`` + ``[]`` ("visible to no one") silently
            # un-shared the report from every team on each save — the same
            # class of bug fixed for publish state. When the payload
            # carries no visibility key at all, preserve the existing scope.
            # (The echarts Builder always sends the keys explicitly, so its
            # saves — including an explicit empty team list — are unchanged.)
            if not visibility_in_payload:
                write_visibility_mode = existing_source.get("visibility_mode", write_visibility_mode)
                write_allowed_org_esids = existing_source.get("allowed_org_esids") or []
                write_allowed_team_esids = existing_source.get("allowed_team_esids") or []
        except HTTPException:
            raise
        except Exception:
            pass  # new document

        # ``menu`` flows from the request payload at create/duplicate
        # time so the Create New Dashboard dialog's menu picker is honored.
        # On update, existing menu is preserved unless the caller explicitly
        # passes a new ``menu`` (which is also the new behavior of the
        # Properties PATCH endpoint). For brand-new dashboards without a
        # ``menu`` in the payload, fall back to the workspace's first menu
        # (or the legacy "Reports" default if the workspace has none).
        payload_menu = data.get("menu")
        if isinstance(payload_menu, str) and payload_menu.strip():
            menu = payload_menu.strip()
        elif existing_menu:
            menu = existing_menu
        else:
            menu = _default_menu_for_current_workspace()

        # #1 / resolve the dashboard's workspace_esid.
        # Update path: preserve the existing value. Create path: stamp the
        # creator's primary workspace_esid. Falls back to the singleton
        # workspace via resolve_default_workspace_esid for pre-onboarding
        # users (transition window — same fallback shape uses for
        # asset writes). Empty string means "no workspace stamp" — the
        # backfill tool fills these in lazily.
        if existing_workspace_esid:
            dashboard_workspace_esid = existing_workspace_esid
        else:
            dashboard_workspace_esid = ""

            # honor the workspace the user explicitly selected in the
            # Builder topbar — the load-bearing fix for the wrong-workspace
            # stamp. Validate it is a workspace the caller can resolve
            # (``get_allowed_workspaces`` — None = privileged/all) so a crafted
            # payload can't stamp into a foreign workspace. When valid, this
            # wins over the arbitrary primary-workspace guess below.
            requested_ws = (data.get("workspace_esid") or "").strip()
            if requested_ws:
                try:
                    allowed_ws_for_stamp = RoleFilterService.get_allowed_workspaces()
                except Exception:  # noqa: BLE001
                    allowed_ws_for_stamp = []
                if allowed_ws_for_stamp is None or requested_ws in set(allowed_ws_for_stamp or []):
                    dashboard_workspace_esid = requested_ws

            if not dashboard_workspace_esid:
                try:
                    from lib.es_helpers import resolve_creator_workspace_esid

                    creator_username = session.get("username", "") or ""
                    dashboard_workspace_esid = resolve_creator_workspace_esid(es, creator_username) or ""
                except Exception as e:
                    logging.warning(
                        "workspace_esid resolution failed for save_dashboard: %s",
                        e,
                    )
                    dashboard_workspace_esid = ""

            # Defect B: an org-only creator has no workspace membership,
            # so resolve_creator_workspace_esid falls back to the singleton
            # workspace. But the Builder read path filters by
            # ``get_allowed_workspaces()`` — which INFERS the org's parent
            # workspace (org_esid → org.workspace_esid) — so the singleton stamp
            # doesn't match the creator's resolvable scope and the dashboard they
            # just created is filtered out of their own view. Reconcile the stamp
            # to a workspace the creator can actually read back: if the resolved
            # stamp isn't in their allowed-workspaces set, use the inferred one.
            try:
                allowed_ws = RoleFilterService.get_allowed_workspaces()
            except Exception:  # noqa: BLE001 — never block a save on this reconcile
                allowed_ws = None
            if allowed_ws:  # non-empty list → the user has a resolvable scope
                if dashboard_workspace_esid not in set(allowed_ws):
                    # Stamp the first inferred workspace so read-back succeeds.
                    # Sorted for deterministic choice when a user spans several.
                    dashboard_workspace_esid = sorted(allowed_ws)[0]

        # a team list the caller sent must name only teams of the
        # workspace the dashboard lives in (or is being created in). A scope
        # preserved from the stored doc is
        # not a new grant and is left alone.
        if visibility_in_payload:
            _require_visibility_teams_in_workspace(write_allowed_team_esids, dashboard_workspace_esid)

        # defense-in-depth — drop allowed_team_esids that belong to a
        # DIFFERENT workspace than the one stamped. The topbar workspace
        # selector + the workspace-scoped team picker already prevent a
        # cross-workspace team mix; this catches a crafted payload or stale
        # Select-All. Conservative: only drop a team we can POSITIVELY confirm
        # lives in another workspace. If the lookup returns nothing (ES flaky,
        # un-stamped teams), we keep the teams untouched — the read
        # gate is the real boundary, so a genuinely-foreign team is unreadable
        # regardless and persisting it does no harm. a non-superuser's
        # sent list was already refused above unless every team is in this
        # workspace, so this narrowing now only acts for a superuser or on a
        # preserved (not re-sent) scope.
        if write_allowed_team_esids and dashboard_workspace_esid:
            try:
                _es = get_elasticsearch_client()
                _resp = _es.search(
                    index="de_asset_teams",
                    body={
                        "query": {"terms": {"_id": list(write_allowed_team_esids)}},
                        "_source": ["workspace_esid"],
                        "size": len(write_allowed_team_esids),
                    },
                )
                _hits = _resp.get("hits", {}).get("hits", [])
                # Map team_esid -> its workspace_esid (only for teams ES knows).
                _team_ws = {}
                for _h in _hits:
                    _team_ws[_h.get("_id")] = (_h.get("_source") or {}).get("workspace_esid") or ""
                # Drop ONLY teams positively known to be in another workspace.
                write_allowed_team_esids = [
                    t
                    for t in write_allowed_team_esids
                    if _team_ws.get(t, dashboard_workspace_esid) == dashboard_workspace_esid
                ]
            except Exception:  # noqa: BLE001 — never block a save on this narrowing
                logging.warning("workspace team-narrowing failed for %s", dashboard_id)

        # defense-in-depth — never persist a date-basis choice whose
        # field is blank. The viewer parent drops such a choice as invalid (a
        # blank field can't re-scope anything), so a saved blank silently
        # collapses the Date-basis dropdown. The builder editor now adopts the
        # displayed field, but a stale/crafted payload could still carry one.
        clean_time_field_choices = [
            c for c in (data.get("time_field_choices") or []) if isinstance(c, dict) and (c.get("field") or "").strip()
        ]

        es_doc = {
            "dashboard_id": dashboard_id,
            "dashboard_type": payload_type,
            "title": data.get("title", "Untitled Dashboard"),
            "menu": menu,
            "workspace_esid": dashboard_workspace_esid,
            "created_at": created_at,
            "created_by": created_by,
            "updated_at": now,
            "visibility_mode": write_visibility_mode,
            "allowed_org_esids": write_allowed_org_esids,
            "allowed_team_esids": write_allowed_team_esids,
            **preserved_publish,
        }
        if payload_type == "canvas":
            # canvas_config (schema v1) is the sibling of
            # echarts_config / map_config — page settings + free-form objects,
            # plus the same date/filter keys echarts_config carries so the
            # dashboard-filter plumbing transfers when wires it up.
            cfg = data.get("canvas_config") or {}
            es_doc["canvas_config"] = {
                "schema_version": int(cfg.get("schema_version") or 1),
                "page": cfg.get("page") or {},
                "objects": cfg.get("objects") or [],
                "global_date_range": cfg.get("global_date_range"),
                "filters": cfg.get("filters") or [],
                "time_field_choices": cfg.get("time_field_choices") or [],
                # COPY-on-seed provenance — which report this canvas
                # was seeded from. Never read back for syncing (DECISIONS.md
                # 2026-07-09); None for blank-canvas documents.
                "seeded_from_dashboard_id": cfg.get("seeded_from_dashboard_id"),
            }
        else:
            es_doc["echarts_config"] = {
                "global_date_range": data.get("global_date_range"),
                # dashboard-level filters (top-of-dashboard value pickers).
                # Persisted alongside ``charts`` so the viewer-side strip can hydrate
                # in one round-trip with the rest of the dashboard config.
                "filters": data.get("filters") or [],
                # author-curated viewer date-basis choices
                # ([{field, label}], first = default). Drives the viewer's
                # "Date basis" selector. Empty/absent -> no selector.
                # blank-field choices stripped above so a silently
                # un-picked field can't collapse the viewer dropdown.
                "time_field_choices": clean_time_field_choices,
                "charts": charts_list,
            }

        try:
            es.index(
                index=DASHBOARDS_INDEX,
                id=dashboard_id,
                document=es_doc,
                refresh="wait_for",
            )
        except Exception as es_err:
            from lib.errors import safe_error_response

            return safe_error_response(
                "Elasticsearch error saving dashboard",
                log_detail=f"ES index error saving dashboard {dashboard_id}",
                exc=es_err,
            )

        return jsonify({"success": True, "id": dashboard_id, "status": "saved"})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error saving dashboard",
            log_detail="save_dashboard failed",
            exc=e,
        )


@dashboard_bp.route("/api/dashboards/<dashboard_id>", methods=["GET"])
def load_dashboard(dashboard_id):
    """Load a saved ECharts dashboard config by its dashboard_id."""
    try:
        source = _load_dashboard_for_read(dashboard_id)

        if source.get("dashboard_type") not in ("echarts", "leaflet_map", "canvas"):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Not an ECharts, Leaflet map, or canvas dashboard",
                    }
                ),
                400,
            )

        state = _es_doc_to_dashboard_state(dashboard_id, source)
        return jsonify(state)
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error loading dashboard",
            log_detail=f"load_dashboard: failed for dashboard_id '{dashboard_id}'",
            exc=e,
        )


@dashboard_bp.route("/api/dashboards/<dashboard_id>/dedup-info", methods=["GET"])
def dashboard_dedup_info(dashboard_id):
    """Report which of a dashboard's charts read a de-duplicated (window) metric.

    . The frontend uses this to decide whether to render the
    Refresh button (Phase 02) and which charts it covers — instead of
    string-matching window-index names in JS (the ``*ga`` substring trap flagged
    ). A chart is a dedup chart when it reads a registered
    non-additive ``(source, field)`` pair FROM that field's window index.

    Response shape::

        {
          "has_dedup": bool,        # true iff at least one LIVE dedup chart
          "charts": [               # one entry per (chart × dedup field) hit
            {"chart_id", "source", "field", "status"}, ...
          ]
        }

    ``has_dedup`` keys on ``status == "live"`` only — a dashboard whose only
    dedup charts are ``planned`` (no window importer yet) reports ``false`` so
    the Refresh button stays hidden until the importer ships.
    """
    try:
        source = _load_dashboard_for_read(dashboard_id)

        # walk the whole doc (echarts charts OR canvas objects) so
        # canvas pages with windowed reads get the Refresh button too.
        hits = []
        for chart_id, match in dashboard_dedup_matches(source):
            hits.append(
                {
                    "chart_id": chart_id,
                    "source": match["source"],
                    "field": match["field"],
                    "status": match["status"],
                }
            )

        has_dedup = any(h["status"] == "live" for h in hits)
        return jsonify({"has_dedup": has_dedup, "charts": hits})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error loading dedup info",
            log_detail=f"dashboard_dedup_info: failed for dashboard_id '{dashboard_id}'",
            exc=e,
        )


@dashboard_bp.route("/api/dashboards/<dashboard_id>/export", methods=["POST"])
def export_dashboard(dashboard_id):
    """Export a dashboard's chart data as Excel (.xlsx) or CSV (.csv).

    Request body (JSON):
        format      — 'xlsx' or 'csv'
        title       — optional report title string
        time_from   — date/date-math string (e.g. 'now-30d/d' or ISO date), empty = all time
        time_to     — date/date-math string or 'now', empty = all time
        org_filter  — organization_title value (empty = no filter)
        team_filter — team_title value (empty = no filter)
    """
    import io
    import csv as csv_module
    import json as json_module

    try:
        body = request.get_json(force=True) or {}
        fmt = (body.get("format") or "xlsx").lower()
        title = (body.get("title") or "").strip()
        time_from = (body.get("time_from") or "").strip()
        time_to = (body.get("time_to") or "").strip()
        org_filter = (body.get("org_filter") or "").strip()
        # team_filter is now a LIST of team_titles (multi-select). Accept
        # both a list (new client) and a bare string (back-compat / other callers);
        # normalize to a de-duped list. Empty list = All Teams (no team narrowing).
        _raw_team = body.get("team_filter")
        if isinstance(_raw_team, list):
            team_filters = [str(t).strip() for t in _raw_team if str(t).strip()]
        else:
            _s = (_raw_team or "").strip()
            team_filters = [_s] if _s else []
        # Optional: restrict map export to specific statistic keys (e.g. ['dt_contacts'])
        stat_keys = body.get("stat_keys") or []  # empty list = all statistics
        # Chart visibility: { chartId: { seriesName: true/false } } — from frontend legend state
        chart_visibility = body.get("chart_visibility") or {}

        if fmt not in ("xlsx", "csv"):
            return (
                jsonify({"error": f"Unsupported format: {fmt}. Use xlsx or csv."}),
                400,
            )

        # follow-up — mirror the export dialog's maxlength="100".
        if len(title) > _MAX_EXPORT_TITLE:
            return (
                jsonify({"error": f"Report title must be {_MAX_EXPORT_TITLE} characters or fewer."}),
                400,
            )

        # Load dashboard config (with visibility check)
        source = _load_dashboard_for_read(dashboard_id)
        # capability gate, replacing the inline echarts-only check.
        # PDF never reaches this endpoint (it's browser-print of the viewer
        # iframe); xlsx/csv are only allowed when the capability resolver says
        # the dashboard can actually produce tabular sheets, with a message
        # naming the type-specific reason otherwise.
        if fmt not in dashboard_export_formats(source):
            return (
                jsonify({"error": tabular_unsupported_reason(source)}),
                400,
            )

        cfg = source.get("echarts_config", {})
        charts = cfg.get("charts", [])
        if isinstance(charts, dict):
            charts = list(charts.values())

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"error": "Elasticsearch unavailable"}), 503

        # Build extra filters from org/team selection
        extra_filters = []
        if org_filter:
            extra_filters.append(
                {
                    "field": "organization_title",
                    "op": "match_phrase",
                    "value": org_filter,
                }
            )
        if team_filters:
            # OR across the selected teams. match_phrase_any (a
            # should-of-match_phrase) is mapping-safe on text-mapped team_title
            # (unlike a terms clause), matching the read path (view.html).
            extra_filters.append({"field": "team_title", "op": "match_phrase_any", "value": team_filters})

        # Helper: resolve ES date math to a plain YYYY-MM-DD string (for DT text fields)
        def _resolve_date_math(expr, upper_bound=False):
            if not expr:
                return expr
            now = datetime.utcnow()
            if re.match(r"^now$", expr, re.I):
                d = now
                if upper_bound:
                    d = d + timedelta(days=1)
                return d.strftime("%Y-%m-%d")
            m = re.match(r"^now-(\d+)([dMy])(?:/[dMy])?$", expr, re.I)
            if not m:
                return expr
            n, unit = int(m.group(1)), m.group(2).lower()
            if unit == "d":
                d = now - timedelta(days=n)
            elif unit == "m":
                # Approximate months
                d = now - timedelta(days=n * 30)
            else:
                d = now - timedelta(days=n * 365)
            return d.strftime("%Y-%m-%d")

        # Collect tabular sheets from each chart
        sheets = []  # list of {title, columns, rows}

        for chart in charts:
            chart_type = chart.get("chart_type", "")
            chart_title = chart.get("title") or "Chart"

            # leaflet_map: aggregate each statistic by team_country and build a wide table
            if chart_type == "leaflet_map":
                map_stats = chart.get("map_statistics", [])
                if not map_stats:
                    continue
                # Filter to selected statistics if caller specified them
                if stat_keys:
                    map_stats = [s for s in map_stats if s.get("key") in stat_keys]
                if not map_stats:
                    continue

                # Build date filter from export time range
                map_date_filter = None
                if time_from or time_to:
                    range_clause = {}
                    if time_from:
                        range_clause["gte"] = time_from
                    if time_to:
                        range_clause["lte"] = time_to
                    map_date_filter = {"range": {"kibana_date": range_clause}}

                # Build org/team filters
                map_must = []
                if org_filter:
                    map_must.append({"match_phrase": {"organization_title": org_filter}})
                if team_filters:
                    # OR across the selected teams (was a single match_phrase).
                    map_must.append(
                        {
                            "bool": {
                                "should": [{"match_phrase": {"team_title": t}} for t in team_filters],
                                "minimum_should_match": 1,
                            }
                        }
                    )

                all_countries = set()
                stat_results = {}  # label -> {country_code: value}

                for stat in map_stats:
                    data_view = stat.get("data_view", "")
                    field = stat.get("field", "")
                    aggregation = stat.get("aggregation", "sum")
                    label = stat.get("label", stat.get("key", ""))

                    if not data_view:
                        continue

                    # Disciple Tools: reuse the existing geocode-based country query.
                    # match BOTH spellings — legacy hyphen alias (old saved
                    # configs) and team-keyed `*_disciple_tools` (underscore).
                    if "disciple-tools" in data_view or "disciple_tools" in data_view:
                        stat_cfg = {
                            "field": field,
                            "aggregation": aggregation,
                            "label": label,
                            "key": stat.get("key", ""),
                        }
                        # Pass through any extra stat-level config keys (address_only, filter fields)
                        for k in (
                            "address_only",
                            "filter_field",
                            "filter_value",
                            "dt_date_field",
                        ):
                            if k in stat:
                                stat_cfg[k] = stat[k]
                        dt_teams = list(team_filters)  # selected teams (may be several)
                        dt_date_from = _resolve_date_math(time_from) if time_from else ""
                        dt_date_to = _resolve_date_math(time_to, upper_bound=True) if time_to else ""
                        try:
                            points = _query_dt_country_map(
                                es,
                                stat_cfg,
                                org_filter,
                                dt_teams,
                                dt_date_from,
                                dt_date_to,
                            )
                            country_values = {}
                            for pt in points:
                                country = pt.get("name", "")
                                if country:
                                    country_values[country] = pt.get("value", 0)
                                    all_countries.add(country)
                            stat_results[label] = country_values
                        except Exception as exc:
                            logging.warning(f'Export map DT stat "{label}" failed: {exc}')
                            stat_results[label] = {}
                        continue

                    must = list(map_must)
                    if map_date_filter:
                        must.append(map_date_filter)

                    total_agg = (
                        {"sum": {"field": field}} if field and aggregation != "count" else {"value_count": {"field": "_id"}}
                    )

                    country_values = {}
                    for tc_field in ("team_country.keyword", "team_country"):
                        agg_body = {
                            "size": 0,
                            "query": ({"bool": {"must": must}} if must else {"match_all": {}}),
                            "aggs": {
                                "by_country": {
                                    "terms": {"field": tc_field, "size": 300},
                                    "aggs": {"total": total_agg},
                                }
                            },
                        }
                        try:
                            resp = es.search(
                                index=RoleFilterService.scoped_index_pattern(data_view),
                                body=agg_body,
                                ignore_unavailable=True,
                            )
                            resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
                            buckets = resp_dict.get("aggregations", {}).get("by_country", {}).get("buckets", [])
                            for bucket in buckets:
                                country = bucket["key"]
                                if not country or country.lower() in ("unknown", ""):
                                    continue
                                value = bucket.get("total", {}).get("value") or bucket["doc_count"]
                                country_values[country] = round(value) if value else 0
                                all_countries.add(country)
                            if country_values:
                                break  # .keyword returned data; no need for fallback
                        except Exception as exc:
                            logging.warning(f'Export map stat "{label}" ({data_view}/{tc_field}) failed: {exc}')

                    stat_results[label] = country_values

                if not all_countries:
                    continue

                # ISO 3166-1 alpha-2 → full country name
                _ISO2_NAMES = {
                    "AF": "Afghanistan",
                    "AL": "Albania",
                    "DZ": "Algeria",
                    "AO": "Angola",
                    "AR": "Argentina",
                    "AM": "Armenia",
                    "AU": "Australia",
                    "AT": "Austria",
                    "AZ": "Azerbaijan",
                    "BD": "Bangladesh",
                    "BY": "Belarus",
                    "BE": "Belgium",
                    "BJ": "Benin",
                    "BO": "Bolivia",
                    "BA": "Bosnia and Herzegovina",
                    "BW": "Botswana",
                    "BR": "Brazil",
                    "BG": "Bulgaria",
                    "BF": "Burkina Faso",
                    "BI": "Burundi",
                    "KH": "Cambodia",
                    "CM": "Cameroon",
                    "CA": "Canada",
                    "CF": "Central African Republic",
                    "TD": "Chad",
                    "CL": "Chile",
                    "CN": "China",
                    "CO": "Colombia",
                    "CD": "Congo (DRC)",
                    "CG": "Congo (Rep.)",
                    "CR": "Costa Rica",
                    "CI": "Côte d'Ivoire",
                    "HR": "Croatia",
                    "CU": "Cuba",
                    "CY": "Cyprus",
                    "CZ": "Czech Republic",
                    "DK": "Denmark",
                    "DJ": "Djibouti",
                    "DO": "Dominican Republic",
                    "EC": "Ecuador",
                    "EG": "Egypt",
                    "SV": "El Salvador",
                    "GQ": "Equatorial Guinea",
                    "ER": "Eritrea",
                    "ET": "Ethiopia",
                    "FI": "Finland",
                    "FR": "France",
                    "GA": "Gabon",
                    "GM": "Gambia",
                    "GE": "Georgia",
                    "DE": "Germany",
                    "GH": "Ghana",
                    "GR": "Greece",
                    "GT": "Guatemala",
                    "GN": "Guinea",
                    "GW": "Guinea-Bissau",
                    "HT": "Haiti",
                    "HN": "Honduras",
                    "HU": "Hungary",
                    "IN": "India",
                    "ID": "Indonesia",
                    "IR": "Iran",
                    "IQ": "Iraq",
                    "IE": "Ireland",
                    "IL": "Israel",
                    "IT": "Italy",
                    "JM": "Jamaica",
                    "JP": "Japan",
                    "JO": "Jordan",
                    "KZ": "Kazakhstan",
                    "KE": "Kenya",
                    "KW": "Kuwait",
                    "KG": "Kyrgyzstan",
                    "LA": "Laos",
                    "LB": "Lebanon",
                    "LR": "Liberia",
                    "LY": "Libya",
                    "LK": "Sri Lanka",
                    "MG": "Madagascar",
                    "MW": "Malawi",
                    "MY": "Malaysia",
                    "ML": "Mali",
                    "MR": "Mauritania",
                    "MX": "Mexico",
                    "MD": "Moldova",
                    "MN": "Mongolia",
                    "MA": "Morocco",
                    "MZ": "Mozambique",
                    "MM": "Myanmar",
                    "NA": "Namibia",
                    "NP": "Nepal",
                    "NL": "Netherlands",
                    "NZ": "New Zealand",
                    "NI": "Nicaragua",
                    "NE": "Niger",
                    "NG": "Nigeria",
                    "NO": "Norway",
                    "OM": "Oman",
                    "PK": "Pakistan",
                    "PA": "Panama",
                    "PG": "Papua New Guinea",
                    "PY": "Paraguay",
                    "PE": "Peru",
                    "PH": "Philippines",
                    "PL": "Poland",
                    "PT": "Portugal",
                    "QA": "Qatar",
                    "RO": "Romania",
                    "RU": "Russia",
                    "RW": "Rwanda",
                    "SA": "Saudi Arabia",
                    "SN": "Senegal",
                    "SL": "Sierra Leone",
                    "SO": "Somalia",
                    "ZA": "South Africa",
                    "SS": "South Sudan",
                    "ES": "Spain",
                    "SD": "Sudan",
                    "SE": "Sweden",
                    "CH": "Switzerland",
                    "SY": "Syria",
                    "TW": "Taiwan",
                    "TJ": "Tajikistan",
                    "TZ": "Tanzania",
                    "TH": "Thailand",
                    "TL": "Timor-Leste",
                    "TG": "Togo",
                    "TN": "Tunisia",
                    "TR": "Turkey",
                    "TM": "Turkmenistan",
                    "UG": "Uganda",
                    "UA": "Ukraine",
                    "AE": "United Arab Emirates",
                    "GB": "United Kingdom",
                    "US": "United States",
                    "UY": "Uruguay",
                    "UZ": "Uzbekistan",
                    "VE": "Venezuela",
                    "VN": "Vietnam",
                    "YE": "Yemen",
                    "ZM": "Zambia",
                    "ZW": "Zimbabwe",
                }

                def _country_display(code):
                    """Return full name if code is ISO2, otherwise return as-is."""
                    if isinstance(code, str) and len(code) == 2:
                        return _ISO2_NAMES.get(code.upper(), code)
                    return code

                stat_labels = [s.get("label", s.get("key", "")) for s in map_stats if s.get("data_view")]
                map_columns = ["Country"] + stat_labels
                map_rows = [
                    [_country_display(country)] + [stat_results.get(lbl, {}).get(country, 0) for lbl in stat_labels]
                    for country in sorted(all_countries)
                ]
                sheets.append(
                    {
                        "title": chart_title + " - Countries",
                        "columns": map_columns,
                        "rows": map_rows,
                    }
                )

                # ── Cities and Regions ───────────────────────────────────────
                # Collect city-level points per stat; regions only from FB sources.
                # Use long format: [Location, Lat, Lon, Statistic, Value]
                # For single-stat exports simplify to [Location, Lat, Lon, Value].
                map_teams = list(team_filters)  # selected teams (may be several)
                map_date_from = _resolve_date_math(time_from) if time_from else time_from
                map_date_to = _resolve_date_math(time_to, upper_bound=True) if time_to else time_to
                single_stat = len(stat_labels) == 1

                city_rows = []  # accumulate across stats
                region_rows = []

                for stat in map_stats:
                    data_view = stat.get("data_view", "")
                    field = stat.get("field", "")
                    aggregation = stat.get("aggregation", "sum")
                    label = stat.get("label", stat.get("key", ""))
                    if not data_view:
                        continue

                    # type='exists' makes _build_stat_filter emit an exists clause (safe
                    # default when we have no gte/value from _MAP_STATISTICS config).
                    stat_cfg_geo = {
                        "field": field,
                        "aggregation": aggregation,
                        "type": "exists" if field else None,
                    }
                    for k in (
                        "address_only",
                        "filter_field",
                        "filter_value",
                        "dt_date_field",
                    ):
                        if k in stat:
                            stat_cfg_geo[k] = stat[k]

                    try:
                        if "fb_insights" in data_view:
                            city_pts = _query_fb_map(
                                es,
                                stat_cfg_geo,
                                org_filter,
                                map_teams,
                                map_date_from,
                                map_date_to,
                            )
                            region_pts = _query_fb_region_map(
                                es,
                                stat_cfg_geo,
                                org_filter,
                                map_teams,
                                map_date_from,
                                map_date_to,
                            )
                            if single_stat:
                                city_rows += [
                                    [
                                        p["name"],
                                        p.get("lat", ""),
                                        p.get("lon", ""),
                                        p["value"],
                                    ]
                                    for p in city_pts
                                ]
                                region_rows += [
                                    [
                                        p["name"],
                                        p.get("lat", ""),
                                        p.get("lon", ""),
                                        p["value"],
                                    ]
                                    for p in region_pts
                                ]
                            else:
                                city_rows += [
                                    [
                                        p["name"],
                                        p.get("lat", ""),
                                        p.get("lon", ""),
                                        label,
                                        p["value"],
                                    ]
                                    for p in city_pts
                                ]
                                region_rows += [
                                    [
                                        p["name"],
                                        p.get("lat", ""),
                                        p.get("lon", ""),
                                        label,
                                        p["value"],
                                    ]
                                    for p in region_pts
                                ]
                        elif "echo_conversations" in data_view:
                            city_pts = _query_echo_map(
                                es,
                                stat_cfg_geo,
                                org_filter,
                                map_teams,
                                map_date_from,
                                map_date_to,
                            )
                            if single_stat:
                                city_rows += [
                                    [
                                        p["name"],
                                        p.get("lat", ""),
                                        p.get("lon", ""),
                                        p["value"],
                                    ]
                                    for p in city_pts
                                ]
                            else:
                                city_rows += [
                                    [
                                        p["name"],
                                        p.get("lat", ""),
                                        p.get("lon", ""),
                                        label,
                                        p["value"],
                                    ]
                                    for p in city_pts
                                ]
                        # match BOTH DT spellings (hyphen = legacy saved
                        # configs, underscore = team-keyed data view).
                        elif "disciple-tools" in data_view or "disciple_tools" in data_view:
                            city_pts = _query_dt_map(
                                es,
                                stat_cfg_geo,
                                org_filter,
                                map_teams,
                                map_date_from,
                                map_date_to,
                            )
                            if single_stat:
                                city_rows += [
                                    [
                                        p["name"],
                                        p.get("lat", ""),
                                        p.get("lon", ""),
                                        p["value"],
                                    ]
                                    for p in city_pts
                                ]
                            else:
                                city_rows += [
                                    [
                                        p["name"],
                                        p.get("lat", ""),
                                        p.get("lon", ""),
                                        label,
                                        p["value"],
                                    ]
                                    for p in city_pts
                                ]
                    except Exception as exc:
                        logging.warning(f'Export map city/region for stat "{label}" failed: {exc}')

                if single_stat:
                    city_cols = ["City", "Lat", "Lon", stat_labels[0]]
                    region_cols = ["Region", "Lat", "Lon", stat_labels[0]]
                else:
                    city_cols = ["City", "Lat", "Lon", "Statistic", "Value"]
                    region_cols = ["Region", "Lat", "Lon", "Statistic", "Value"]

                if city_rows:
                    sheets.append(
                        {
                            "title": chart_title + " - Cities",
                            "columns": city_cols,
                            "rows": city_rows,
                        }
                    )
                if region_rows:
                    sheets.append(
                        {
                            "title": chart_title + " - Regions",
                            "columns": region_cols,
                            "rows": region_rows,
                        }
                    )

                continue

            # trend_line: run date-histogram per source and build date × series table
            if chart_type == "trend_line":
                sources = chart.get("sources", [])
                interval = chart.get("date_interval", "month")
                if not sources:
                    continue
                # Filter to checked sources only.
                # vis = {} → chart never interacted with → export all sources
                # vis has entries, some true → export only checked sources
                # vis has entries, all false → user unchecked everything → skip chart
                vis = chart_visibility.get(chart.get("chart_id", ""), {})
                if vis:
                    if not any(vis.values()):
                        continue  # all unchecked — skip this chart
                    sources = [s for s in sources if vis.get(s.get("label", ""), False)]
                if not sources:
                    continue

                # Build base ES filters (same logic as get_trend_line_data)
                tl_body = {
                    "filters": [{"field": f["field"], "op": f["op"], "value": f["value"]} for f in extra_filters],
                    "date_range": None,
                }
                if time_from or time_to:
                    tl_body["date_range"] = {
                        "field": "kibana_date",
                        "from": time_from or None,
                        "to": time_to or None,
                    }
                dr = tl_body["date_range"] or {}
                base_filters = _build_filters(tl_body)
                extended_bounds = None
                if dr.get("from") and dr.get("to"):
                    extended_bounds = {"min": dr["from"], "max": dr["to"]}

                all_dates = set()
                tl_results = {}

                for source in sources:
                    label = source.get("label", "")
                    data_view = source.get("data_view", "")
                    field = source.get("field") or None
                    date_field = source.get("date_field", "kibana_date")
                    query_filter = source.get("query_filter")
                    if not data_view:
                        tl_results[label] = {}
                        continue

                    hist = {
                        "field": date_field,
                        "calendar_interval": interval,
                        "format": "yyyy-MM",
                        "min_doc_count": 0,
                    }
                    if extended_bounds:
                        hist["extended_bounds"] = extended_bounds
                    aggs = {"over_time": {"date_histogram": hist}}
                    if field and is_ratio_field(field):
                        # registered ratio fields, same shape as
                        # get_trend_line_data above. date_histogram is a
                        # multi-bucket parent so bucket_script works natively.
                        spec = RATIO_FIELDS[field]
                        aggs["over_time"]["aggs"] = {
                            "metric_num": {"sum": {"field": spec["numerator"]}},
                            "metric_denom": {"sum": {"field": spec["denominator"]}},
                            "metric": {
                                "bucket_script": {
                                    "buckets_path": {"num": "metric_num", "denom": "metric_denom"},
                                    "script": "params.denom > 0 ? params.num / params.denom : null",
                                }
                            },
                        }
                    elif field:
                        aggs["over_time"]["aggs"] = {"metric": {"sum": {"field": field}}}

                    source_filters = list(base_filters)
                    if query_filter:
                        source_filters.append(query_filter)
                    # breakdown-record exclusion is per-source (the
                    # breakdown types differ by data_view), but base_filters is
                    # built once outside this loop. Append the source-specific
                    # must_not here so google_ads/youtube trend-export lines sum
                    # perf docs only, honoring the tile's opt-in checkbox.
                    breakdown_clause = breakdown_exclusion_clause(
                        data_view,
                        include_breakdown_records=chart.get("include_breakdown_records"),
                        chart_filters=extra_filters,
                    )
                    if breakdown_clause:
                        source_filters.append(breakdown_clause)

                    query_body = {
                        "size": 0,
                        "query": ({"bool": {"filter": source_filters}} if source_filters else {"match_all": {}}),
                        "aggs": aggs,
                    }
                    try:
                        resp = es.search(
                            index=RoleFilterService.scoped_index_pattern(data_view),
                            body=query_body,
                            ignore_unavailable=True,
                        )
                        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
                        buckets = resp_dict.get("aggregations", {}).get("over_time", {}).get("buckets", [])
                        source_data = {}
                        for bucket in buckets:
                            key = bucket.get("key_as_string", "")
                            if not key:
                                continue
                            all_dates.add(key)
                            if field:
                                value = (bucket.get("metric") or {}).get("value") or 0
                            else:
                                value = bucket.get("doc_count", 0)
                            source_data[key] = round(value) if value else 0
                        tl_results[label] = source_data
                    except Exception as exc:
                        logging.warning(f'Export trend_line source "{label}" ({data_view}) failed: {exc}')
                        tl_results[label] = {}

                x_data = sorted(all_dates)
                if x_data:
                    tl_columns = ["Date"] + [s.get("label", "") for s in sources]
                    tl_rows = [[d] + [tl_results.get(s.get("label", ""), {}).get(d, 0) for s in sources] for d in x_data]
                    sheets.append({"title": chart_title, "columns": tl_columns, "rows": tl_rows})
                continue

            # Build query config with injected org/team filters and time range
            query_config = dict(chart)
            existing_filters = list(query_config.get("filters") or [])
            query_config["filters"] = existing_filters + extra_filters

            # Inject time range (mirrors _withExtraFilters logic in charts.js)
            if not query_config.get("date_range") and (time_from or time_to):
                data_view = query_config.get("data_view", "")
                if data_view:
                    # match BOTH DT spellings (hyphen = legacy saved
                    # configs, underscore = team-keyed data view).
                    is_dt = "disciple-tools" in data_view or "disciple_tools" in data_view
                    if is_dt:
                        # Team-keyed *_disciple_tools maps date_created as a bare
                        # keyword (no .keyword subfield) and carries date_created_ms
                        # (real date type); the legacy dynamic-mapped alias was text
                        # and needed the subfield.
                        default_dt_field = "date_created.keyword" if "disciple-tools" in data_view else "date_created_ms"
                        dt_field = query_config.get("dt_date_field", default_dt_field)
                        query_config["date_range"] = {
                            "field": dt_field,
                            "from": _resolve_date_math(time_from) or None,
                            "to": _resolve_date_math(time_to, upper_bound=True) or None,
                        }
                    else:
                        x_field_type = query_config.get("x_field_type", "")
                        x_field = query_config.get("x_field", "")
                        date_field = x_field if (x_field_type == "date" and x_field) else "kibana_date"
                        query_config["date_range"] = {
                            "field": date_field,
                            "from": time_from or None,
                            "to": time_to or None,
                        }

            try:
                if chart_type == "grouped_table":
                    # grouped_table uses a complex multi-cell query; export a simplified version
                    result_data = _export_grouped_table(es, query_config)
                elif chart_type == "table":
                    # May be multi-data-view (per-column DV, or
                    # cross-DV calc formula fields)
                    table_cols = query_config.get("table_columns") or []
                    if any(col.get("data_view") for col in table_cols if col.get("field")) or _table_has_cross_dv_calc(
                        table_cols, query_config.get("data_view", "")
                    ):
                        resp = _run_table_multi_dv_query(es, query_config)
                        result_data = json_module.loads(resp.get_data(as_text=True))
                    else:
                        _enrich_y_field_types(es, query_config)
                        qbody = build_query(query_config)
                        es_resp = es.search(
                            index=RoleFilterService.scoped_index_pattern(query_config["data_view"]),
                            body=qbody,
                            ignore_unavailable=True,
                        )
                        es_dict = es_resp.body if hasattr(es_resp, "body") else dict(es_resp)
                        result_data = transform(es_dict, query_config)
                else:
                    _enrich_y_field_types(es, query_config)
                    qbody = build_query(query_config)
                    es_resp = es.search(
                        index=RoleFilterService.scoped_index_pattern(query_config["data_view"]),
                        body=qbody,
                        ignore_unavailable=True,
                    )
                    es_dict = es_resp.body if hasattr(es_resp, "body") else dict(es_resp)
                    result_data = transform(es_dict, query_config)
            except Exception as exc:
                logging.warning(f'Export: query failed for chart "{chart_title}": {exc}')
                continue

            # Filter hidden items using frontend legend/checkbox state.
            # vis = {} → chart never interacted with → export all data
            # vis has entries, some true → export only checked items
            # vis has entries, all false → user unchecked everything → skip chart
            vis = chart_visibility.get(chart.get("chart_id", ""), {})
            if vis:
                if not any(vis.values()):
                    continue  # all unchecked — skip this chart
                if isinstance(result_data, dict):
                    series = result_data.get("series")
                    if series:
                        result_data = dict(result_data)
                        if series[0].get("type") == "pie":
                            filtered = dict(series[0])
                            filtered["data"] = [d for d in series[0].get("data", []) if vis.get(d.get("name", ""), False)]
                            result_data["series"] = [filtered]
                        else:
                            result_data["series"] = [s for s in series if vis.get(s.get("name", ""), False)]

            columns, rows = _chart_data_to_table(result_data, query_config)
            if columns:
                sheets.append({"title": chart_title, "columns": columns, "rows": rows})

        if not sheets:
            return (
                jsonify({"error": "No exportable chart data was found in this dashboard."}),
                400,
            )

        safe_name = re.sub(r"[^\w\s\-]", "", title or "dashboard").strip().replace(" ", "_") or "dashboard"

        # (DBUILD-003): titles, column names and row values come from
        # ES aggregation bucket keys — third-party-influenced strings (campaign
        # names, team/org titles, country labels). Neutralize formula triggers
        # with the shared csv_safe before ANY csv.writer / openpyxl write;
        # non-strings pass through so numerics stay typed in XLSX.
        from asset_management.web.services.report_export_service import csv_safe

        # ── CSV ──────────────────────────────────────────────────────
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv_module.writer(buf)
            if title:
                writer.writerow([csv_safe(title)])
                writer.writerow([])
            for idx, sheet in enumerate(sheets):
                if idx > 0:
                    writer.writerow([])
                    writer.writerow([])
                writer.writerow([csv_safe(sheet["title"])])
                writer.writerow([csv_safe(c) for c in sheet["columns"]])
                for row in sheet["rows"]:
                    writer.writerow([csv_safe(v) for v in row])
            from flask import Response as FlaskResponse

            return FlaskResponse(
                buf.getvalue().encode("utf-8-sig"),  # BOM for Excel compatibility
                mimetype="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{safe_name}.csv"'},
            )

        # ── XLSX ─────────────────────────────────────────────────────
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
        except ImportError:
            return (
                jsonify({"error": "Excel export requires openpyxl. Run: pip install openpyxl"}),
                500,
            )

        wb = openpyxl.Workbook()
        wb.remove(wb.active)  # remove the default blank sheet

        header_fill = PatternFill(start_color="2C3E50", end_color="2C3E50", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)

        for sheet in sheets:
            ws_title = sheet["title"][:31]  # Excel limit: 31 chars
            ws = wb.create_sheet(title=ws_title)
            row_offset = 1

            if title:
                cell = ws.cell(row=row_offset, column=1, value=csv_safe(title))
                cell.font = Font(bold=True, size=12)
                row_offset += 2

            # Header row
            for ci, col_name in enumerate(sheet["columns"], 1):
                cell = ws.cell(row=row_offset, column=ci, value=csv_safe(col_name))
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")

            # Data rows
            for ri, row in enumerate(sheet["rows"], row_offset + 1):
                for ci, val in enumerate(row, 1):
                    ws.cell(row=ri, column=ci, value=csv_safe(val))

            # Auto-size columns (capped at 50)
            for col_cells in ws.columns:
                max_len = max((len(str(c.value or "")) for c in col_cells), default=10)
                ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 50)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        from flask import Response as FlaskResponse

        return FlaskResponse(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.xlsx"'},
        )

    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error exporting dashboard",
            log_detail=f"export_dashboard: failed for dashboard_id '{dashboard_id}'",
            exc=e,
            body={},
        )


def _enrich_y_field_types(es, query_config):
    """Add ES field type metadata to query_config (same as in run_query)."""
    field_types = _get_field_types(es, query_config.get("data_view", ""))
    if not field_types:
        return
    existing = query_config.get("y_field_types") or {}
    y_fields = query_config.get("y_fields") or ([query_config["y_field"]] if query_config.get("y_field") else [])
    for yf in y_fields:
        if yf not in existing and yf in field_types:
            existing[yf] = field_types[yf]
    query_config["y_field_types"] = existing


def _export_grouped_table(es, config):
    """Export a grouped_table chart as a flat Group / Column / Value table.

    grouped_table charts have a groups → columns → data_sources nested structure
    where each column may query a different data_view and field.  We run a single
    metric aggregation per data_source and sum the results when a column has
    multiple sources.
    """
    groups = config.get("groups") or []
    if not groups:
        return {"chart_type": "table", "columns": [], "rows": []}

    # Carry the time range and filters from the parent export config
    filters = config.get("filters", [])
    date_range = config.get("date_range")

    rows = []
    for group in groups:
        group_label = group.get("label", "")
        for col in group.get("columns", []):
            col_label = col.get("label", "")
            aggregation = col.get("aggregation", "sum")
            total_value = 0

            for ds in col.get("data_sources", []):
                data_view = ds.get("data_view", "") or col.get("data_view", "")
                field = ds.get("field", "") or col.get("field", "")
                if not data_view or not field:
                    continue

                metric_config = {
                    "chart_type": "metric",
                    "data_view": data_view,
                    "y_field": field,
                    "aggregation": aggregation,
                    "filters": filters,
                }
                if date_range:
                    metric_config["date_range"] = date_range

                try:
                    _enrich_y_field_types(es, metric_config)
                    qbody = build_query(metric_config)
                    es_resp = es.search(
                        index=RoleFilterService.scoped_index_pattern(data_view),
                        body=qbody,
                        ignore_unavailable=True,
                    )
                    es_dict = es_resp.body if hasattr(es_resp, "body") else dict(es_resp)
                    result = transform(es_dict, metric_config)
                    total_value += result.get("value") or 0
                except Exception as exc:
                    logging.warning(f'Export grouped_table cell "{group_label}/{col_label}" failed: {exc}')

            rows.append(
                [
                    group_label,
                    col_label,
                    (round(total_value) if isinstance(total_value, float) else total_value),
                ]
            )

    return {
        "chart_type": "table",
        "columns": ["Group", "Column", "Value"],
        "rows": rows,
    }


def _chart_data_to_table(result, config):
    """Convert transform() output to (columns, rows) lists suitable for export.

    Handles: table, metric, pie, bar, line.
    Returns ([], []) when no data is available.
    """
    chart_type = result.get("chart_type") or config.get("chart_type", "")

    # ── Table ───────────────────────────────────────────────────────
    if chart_type == "table":
        return result.get("columns", []), result.get("rows", [])

    # ── Metric ──────────────────────────────────────────────────────
    if chart_type == "metric":
        if result.get("breakdowns"):
            cols = ["Category", "Value"]
            rows = [[b["label"], b["value"]] for b in result["breakdowns"]]
        else:
            cols = ["Metric", "Value"]
            rows = [[result.get("label", ""), result.get("value", "")]]
        return cols, rows

    # ── Pie ─────────────────────────────────────────────────────────
    series = result.get("series", [])
    if series and series[0].get("type") == "pie":
        data = series[0].get("data", [])
        cols = ["Category", "Value"]
        rows = [[d.get("name", ""), d.get("value", "")] for d in data]
        return cols, rows

    # ── Bar / Line ──────────────────────────────────────────────────
    if series:
        x_data = result.get("xAxis", {}).get("data", [])
        x_field = config.get("x_field", "Category")
        x_label = x_field.replace(".keyword", "") if x_field else "Category"
        cols = [x_label] + [s.get("name", f"Series {i + 1}") for i, s in enumerate(series)]
        rows = []
        for i, x_val in enumerate(x_data):
            row = [x_val] + [s["data"][i] if i < len(s.get("data", [])) else "" for s in series]
            rows.append(row)
        return cols, rows

    return [], []


@dashboard_bp.route("/api/dashboards/org-team-options", methods=["GET"])
def get_org_team_options():
    """Return organizations and teams for the properties panel.

    scoped users (team_*/org_* roles) only see their allowed
    teams + the orgs containing those teams.

     (2026-05-15): privileged users (``superuser`` /
    ``asset_admin``) now also resolve through
    ``get_allowed_teams_for_org_users`` — their result is the union
    of memberships and active data grants, possibly empty. The
    ``allowed_teams is None`` branch below is reached only by
    non-privileged misconfigured accounts (no team/org roles, no
    privilege). The existing scope-filter handles bare privileged
    users (empty list, not None) correctly: it filters teams and
    orgs to empty arrays, closing the leak Surface 3 of.
    """
    try:
        org_hits = get_all_documents("de_asset_organization")
        team_hits = get_all_documents("de_asset_teams")

        orgs = []
        for hit in org_hits:
            source = hit.get("_source", {})
            orgs.append(
                {
                    "esid": hit.get("_id", ""),
                    "title": source.get("organization_title", ""),
                }
            )
        orgs.sort(key=lambda o: o["title"])

        teams = []
        for hit in team_hits:
            source = hit.get("_source", {})
            teams.append(
                {
                    "esid": hit.get("_id", ""),
                    "title": source.get("team_title", ""),
                    # carry workspace_esid so the scope filter
                    # below can enforce the tenant boundary by workspace,
                    # not just by (collision-prone) team title.
                    "workspace_esid": source.get("workspace_esid", ""),
                }
            )
        teams.sort(key=lambda t: t["title"])

        # filter to scope for non-admin users. Admin path
        # (allowed_teams is None) returns the full list as before.
        allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()

        # tenant-boundary filter. The title filter below
        # (``t["title"] in allowed_set``) was the ONLY scope gate, and team
        # titles are NOT unique across workspaces. A user scoped to a team
        # titled e.g. "Kavanah Team" in workspace A would match a
        # same-titled team in workspace B and see it in the picker — and
        # selecting it surfaces B's per-workspace ``{team_esid}__{source}``
        # indices (confirmed cross-tenant leak, fb_app_review 2026-05-29).
        # Layer this workspace-scope narrowing UNDER the title filter,
        # mirroring the data-connectors picker + Asset Explorer:
        #   None → no filter (superuser / asset_admin admin-override)
        #   []   → deny-all
        #   list → keep only teams whose workspace_esid is in the set;
        #          fail-closed on a team with no workspace_esid (a stale
        #          row must not widen scope through a missing field).
        allowed_workspaces = RoleFilterService.get_allowed_workspaces()
        if allowed_workspaces is not None:
            allowed_ws_set = set(allowed_workspaces)
            teams = [t for t in teams if t.get("workspace_esid") and t["workspace_esid"] in allowed_ws_set]

        # the Builder passes the active workspace (topbar selector) so
        # the properties-panel team picker + Select All scope to ONE workspace
        # — a superuser no longer mixes teams across workspaces. Applied on top
        # of the role/workspace scope above; fail-closed on a team with no
        # workspace_esid (mirrors the filter). Org narrowing follows.
        selected_workspace = (request.args.get("workspace_esid") or "").strip()
        if selected_workspace:
            teams = [t for t in teams if t.get("workspace_esid") == selected_workspace]
            # Narrow orgs to the selected workspace too, so the (legacy) org
            # checkbox callers don't surface foreign-workspace orgs.
            try:
                ws_org_esids = {
                    h.get("_id", "")
                    for h in org_hits
                    if (h.get("_source", {}) or {}).get("workspace_esid") == selected_workspace
                }
                orgs = [o for o in orgs if o["esid"] in ws_org_esids]
            except Exception:  # noqa: BLE001 — never widen
                pass

        if allowed_teams is not None:
            allowed_set = set(allowed_teams)
            teams = [t for t in teams if t["title"] in allowed_set]
            # follow-up (2026-06-02): orgs appear in the visibility
            # picker ONLY when the user actually holds an org/workspace role.
            # ``get_allowed_organizations()`` is empty for a team-only user
            # (e.g. a team admin) and non-empty for an org/workspace-role
            # holder. The previous "parent orgs of my allowed teams" fallback
            # surfaced orgs to a team-only user — a team admin's authority is
            # the team, not the org — so it is removed. A team-only user now
            # sees teams only; an org/workspace-role user still sees their orgs.
            allowed_org_titles = set(RoleFilterService.get_allowed_organizations() or [])
            orgs = [o for o in orgs if o["title"] in allowed_org_titles]

        # drop the internal workspace_esid helper field before
        # returning so the API response shape stays {esid, title} as the
        # properties-panel JS expects. The field was only needed for the
        # tenant-boundary filter above.
        teams = [{"esid": t["esid"], "title": t["title"]} for t in teams]

        return jsonify({"success": True, "organizations": orgs, "teams": teams})
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error fetching org/team options",
            log_detail="get_org_team_options failed",
            exc=e,
        )


@dashboard_bp.route("/api/dashboards/<dashboard_id>/properties", methods=["PATCH"])
def update_dashboard_properties(dashboard_id):
    """Update dashboard properties (title, menu, visibility_mode) without touching charts."""
    try:
        _require_builder_write()

        data = request.get_json(force=True)
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        source = _load_dashboard_for_read(dashboard_id)

        # (folds in the mutate follow-up): a properties edit is a
        # mutation of an existing dashboard, so gate it with the per-dashboard
        # mutate-scope check before applying any change. A non-workspace-admin
        # may edit only a dashboard whose team scope intersects their own.
        _require_dashboard_mutate_scope(source)

        # ``menu`` is editable from the Dashboard Properties panel so
        # an existing dashboard can be moved between workspace-scoped menus
        # without a full charts-and-all save round-trip.
        allowed_fields = {"title", "visibility_mode", "allowed_org_esids", "allowed_team_esids", "menu"}
        updates = {k: v for k, v in data.items() if k in allowed_fields}
        if not updates:
            return jsonify({"success": False, "error": "No valid fields to update"}), 400

        # follow-up — the create/save path caps the title via
        # _validate_dashboard_payload; this PATCH could rename past the cap
        # with a crafted request. Mirror the same ceilings.
        patch_title = updates.get("title")
        if isinstance(patch_title, str) and len(patch_title) > _MAX_DASHBOARD_TITLE:
            return (
                jsonify({"success": False, "error": f"Dashboard title must be {_MAX_DASHBOARD_TITLE} characters or fewer."}),
                400,
            )
        patch_menu = updates.get("menu")
        if isinstance(patch_menu, str) and len(patch_menu) > _MAX_MENU_TITLE:
            return (
                jsonify({"success": False, "error": f"Menu title must be {_MAX_MENU_TITLE} characters or fewer."}),
                400,
            )

        # when this PATCH MOVES the dashboard to a different menu,
        # recompute menu_position to append it at the END of the target menu.
        # Without this the dashboard keeps its old position integer, which can
        # strand it mid-list (or collide) in the target group. The position is
        # computed server-side from the target menu's current docs — the
        # client cannot set an arbitrary position via this PATCH (menu_position
        # is deliberately NOT in allowed_fields). No-op when the menu is
        # unchanged or absent from the payload.
        new_menu = updates.get("menu")
        if isinstance(new_menu, str) and new_menu.strip() and new_menu.strip() != (source.get("menu") or ""):
            from asset_management.web.services.workspace_menu_service import (
                WorkspaceMenuService,
            )

            target_workspace = source.get("workspace_esid") or ""
            if target_workspace:
                updates["menu_position"] = WorkspaceMenuService.next_dashboard_position(target_workspace, new_menu.strip())

        # teams-only visibility. When the PATCH touches any visibility
        # field, normalize the whole trio to the teams-only model (show_teams +
        # caller-scoped teams + no org scope), replacing the show_all
        # reject. A title-only / menu-only edit leaves visibility untouched.
        if updates.keys() & {"visibility_mode", "allowed_org_esids", "allowed_team_esids"}:
            (
                vis_mode,
                vis_org_esids,
                vis_team_esids,
            ) = _normalize_write_visibility_scope(data)
            # only teams of the dashboard's own workspace.
            _require_visibility_teams_in_workspace(vis_team_esids, source.get("workspace_esid") or "")
            updates["visibility_mode"] = vis_mode
            updates["allowed_org_esids"] = vis_org_esids
            updates["allowed_team_esids"] = vis_team_esids

        updates["updated_at"] = datetime.now(tz=timezone.utc).isoformat()

        es.update(
            index=DASHBOARDS_INDEX,
            id=dashboard_id,
            doc=updates,
            refresh="wait_for",
        )

        return jsonify({"success": True, "id": dashboard_id, "updated": list(updates.keys())})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error updating dashboard properties",
            log_detail=f"update_dashboard_properties: failed for dashboard_id '{dashboard_id}'",
            exc=e,
        )


@dashboard_bp.route("/api/dashboards/<dashboard_id>", methods=["DELETE"])
def delete_dashboard(dashboard_id):
    """Delete a saved ECharts dashboard."""
    try:
        _require_builder_write()

        # Verify it's an ECharts dashboard before deleting
        source = _load_dashboard_for_read(dashboard_id)

        # read access (above) is not delete access. Enforce the
        # per-dashboard mutate-scope gate so an org-tier user cannot delete a
        # workspace-shared (show_all) dashboard or one scoped to another org.
        # but an empty-scope (private-to-creator, no teams ticked)
        # dashboard is still deletable — deletion is never gated on visibility.
        _require_dashboard_mutate_scope(source, allow_empty_scope=True)

        if source.get("dashboard_type") not in ("echarts", "leaflet_map", "canvas"):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Not an ECharts, Leaflet map, or canvas dashboard",
                    }
                ),
                400,
            )

        success = delete_document_by_id(DASHBOARDS_INDEX, dashboard_id)
        if not success:
            return jsonify({"success": False, "error": "Delete failed"}), 500

        # Clean up any ESID references in child assets
        try:
            from asset_management.web.services.reference_cleanup_service import (
                ReferenceCleanupService,
            )

            ReferenceCleanupService.cleanup_references(DASHBOARDS_INDEX, dashboard_id)
        except Exception as cleanup_err:
            logging.error(f"Reference cleanup failed for dashboard {dashboard_id}: {cleanup_err}")

        return jsonify({"success": True, "status": "deleted"})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error deleting dashboard",
            log_detail=f"delete_dashboard: failed for dashboard_id '{dashboard_id}'",
            exc=e,
        )


# ══════════════════════════════════════════════════════════════
# Publish — Share Impact Reports via unguessable URLs
# Updated by gated capability, configurable expiry, scope snapshot
# ══════════════════════════════════════════════════════════════
#
# Fields on the dashboard record:
#   publish_token          — URL-safe secret; absent when unpublished
#   publish_enabled        — bool mirror of token presence, kept for clarity
#   published_at           — ISO-8601 timestamp of first publish / last regenerate
# publish_expires_at — ISO-8601 expiry; null = never expires
# published_org_title — org title snapshot at publish time
# published_team_title — team title snapshot at publish time
#
# Lookup by token uses a term query on publish_token.keyword and rejects
# expired tokens (publish_expires_at <= now).
#
# The ``publish_reports`` permission gates publish/unpublish/regenerate so an
# org admin can grant "share publicly" without granting "edit dashboards".

# UI default; server accepts any positive int or null (never-expires).
# Cap at 10 years to keep ES date arithmetic sane.
# default raised 30 → 90 days, and ``never_expires`` now needs an
# explicit ``confirm_never_expires`` alongside it — a link that never expires
# is a deliberate choice, not a checkbox slip.
_PUBLISH_DEFAULT_EXPIRY_DAYS = 90
_PUBLISH_MAX_EXPIRY_DAYS = 365 * 10

# optional viewer passcode on a publish entry. Stored as a werkzeug
# scrypt hash (``scrypt:32768:8:1$<salt>$<hash>``) — never the passcode.
# Bounds are generous: this is a shared secret typed by a supporter, not a
# personal password.
_PUBLISH_PASSCODE_MIN_LEN = 4
_PUBLISH_PASSCODE_MAX_LEN = 64
# how much geography a published map may show. ``city`` is the
# pre-1513 behaviour (city markers + admin-1 regions + countries); ``country``
# keeps only the country choropleth; ``hidden`` returns no geo data at all.
# City-level points on a shared link are a targeting map for anyone who can
# identify the team (threat model §5.4), so a NEW publish defaults to
# ``country`` — the publisher opts back into city detail per link. Entries
# written before 1513 carry no field and keep rendering as ``city``.
_PUBLISH_MAP_DETAIL_CHOICES = ("city", "country", "hidden")
_PUBLISH_DEFAULT_MAP_DETAIL = "country"
_PUBLISH_LEGACY_MAP_DETAIL = "city"

# IMPACT_REPORT_CHART_TYPES is now imported from
# export_capability_service (see the module imports at the top of this file),
# collapsing the copy that lived here. The name stays in this
# module's namespace for its uses below.

# Whitelist of index patterns each Impact Report chart type legitimately queries.
# Keep in sync with _RPT_PLATFORM in static/dashboard_builder/charts.js.
_IMPACT_REPORT_DATA_VIEWS: dict[str, frozenset[str]] = {
    "report": frozenset({"*fb_insights", "*echo_conversations"}),
    "google_report": frozenset({"*google_ads"}),
    "combined_report": frozenset({"*fb_insights", "*echo_conversations", "*google_ads"}),
}


def _is_publish_eligible(source: dict) -> tuple[bool, str]:
    """Return (ok, reason) — is this dashboard publishable rules?

    Rules:
      - dashboard_type == "echarts"
      - every chart is an Impact Report (``report`` / ``google_report`` / ``combined_report``)
      - at least one allowed_org_esid or allowed_team_esid (so there is a scope to enforce)

    the prior ``menu == "Reports"`` requirement is dropped.
    "Reports" is no longer a privileged menu — workspace admins now
    configure their own menu list per workspace, and any menu may
    contain publishable Impact Report dashboards. Visibility-mode +
    allowed_*_esids continue to gate publish.

    canvas pages join the publishable types. The chart-type
    check has no canvas analog (live elements are metric bindings, not
    charts — every public query is already pinned to the binding
    data-view whitelist + forced scope), so a canvas page is eligible
    when it has content and a scope to enforce.
    """
    if source.get("dashboard_type") == "canvas":
        if not ((source.get("canvas_config") or {}).get("objects") or []):
            return False, "Canvas page has no content to publish."
        if not (source.get("allowed_org_esids") or source.get("allowed_team_esids")):
            return False, "Assign the dashboard to an organization or team before publishing."
        return True, ""

    if source.get("dashboard_type") != "echarts":
        return False, "Only ECharts dashboards can be published."

    charts = (source.get("echarts_config") or {}).get("charts") or []
    if isinstance(charts, dict):
        charts = list(charts.values())
    if not charts:
        return False, "Dashboard has no charts to publish."
    for chart in charts:
        if chart.get("chart_type") not in IMPACT_REPORT_CHART_TYPES:
            return False, "Dashboard must contain only Impact Report charts."

    if not (source.get("allowed_org_esids") or source.get("allowed_team_esids")):
        return False, "Assign the dashboard to an organization or team before publishing."

    return True, ""


def _resolve_org_titles(esids: list) -> list[str]:
    """Resolve organization ES doc IDs to their ``organization_title`` values."""
    titles: list[str] = []
    if not esids:
        return titles
    for esid in esids:
        if not esid:
            continue
        doc = get_document_by_id("de_asset_organization", esid)
        if doc:
            title = doc.get("organization_title")
            if title:
                titles.append(title)
    return titles


def _resolve_team_titles(esids: list) -> list[str]:
    """Resolve team ES doc IDs to their ``team_title`` values."""
    titles: list[str] = []
    if not esids:
        return titles
    for esid in esids:
        if not esid:
            continue
        doc = get_document_by_id("de_asset_teams", esid)
        if doc:
            title = doc.get("team_title")
            if title:
                titles.append(title)
    return titles


def _forced_publish_filters(source: dict) -> list[dict]:
    """Return filter specs forced onto every public query for this dashboard.

    Each returned item is in the ``{field, op, value}`` shape that
    ``es_query_builder._build_filters`` accepts — callers can append them
    directly to ``config['filters']``.

    When teams are present the org filter is omitted — data views like Disciple
    Tools don't have ``organization_title``, and the team filter is the tighter
    scope anyway. This mirrors the behavior of ``_org_team_filters()``.

    when the dashboard was published with a specific publisher-selected
    org or team (``published_team_title`` / ``published_org_title``), that snapshot
    takes precedence over the dashboard's general ``allowed_*_esids`` scope. The
    snapshot is what the publisher chose at publish time, so the public viewer
    sees exactly the scope the publisher intended — independent of any later
    membership changes for the publisher.
    """
    team_titles = _resolve_team_titles(source.get("allowed_team_esids") or [])
    org_titles = _resolve_org_titles(source.get("allowed_org_esids") or [])

    pub_team = (source.get("published_team_title") or "").strip()
    pub_org = (source.get("published_org_title") or "").strip()
    # the publisher snapshot may only RESTRICT within the dashboard's
    # own allowed scope. Intersecting here (in addition to the publish-time
    # validation) closes the window for any entry stamped with a foreign title
    # BEFORE the write-path fix landed — without it, a pre-fix malicious entry
    # would keep serving another tenant's data via the public link. An
    # out-of-scope snapshot title falls through to the dashboard's general
    # allowed_*_esids scope (never wider than the dashboard itself).
    if pub_team and pub_team in set(team_titles):
        return [{"field": "team_title", "op": "match_phrase", "value": pub_team}]
    if pub_org and pub_org in set(org_titles):
        return [{"field": "organization_title", "op": "match_phrase", "value": pub_org}]

    specs: list[dict] = []
    if team_titles:
        if len(team_titles) == 1:
            specs.append({"field": "team_title", "op": "match_phrase", "value": team_titles[0]})
        else:
            specs.append({"field": "team_title", "op": "match_phrase_any", "value": team_titles})
    elif org_titles:
        if len(org_titles) == 1:
            specs.append({"field": "organization_title", "op": "match_phrase", "value": org_titles[0]})
        else:
            specs.append({"field": "organization_title", "op": "match_phrase_any", "value": org_titles})
    return specs


def _canvas_binding_data_views(source: dict) -> set[str]:
    """the data views a canvas page's live bindings legitimately query.

    Simple bindings contribute ``binding.data_view``; computed (calculated)
    bindings contribute their numerator + denominator views. Mirrors
    ``bindingDataViews()`` in canvas_view.js. Dashboard-level filters add no
    views of their own — a filter only ever merges into a query whose
    ``data_view`` it matches.
    """
    allowed: set[str] = set()

    def _add(binding) -> None:
        dv = (binding or {}).get("data_view") if isinstance(binding, dict) else None
        if dv:
            allowed.add(dv)

    for obj in (source.get("canvas_config") or {}).get("objects") or []:
        if not isinstance(obj, dict):
            continue
        # table objects query their config's data_view through
        # /public/api/query (and per-column data_view overrides on the
        # authenticated surface); whitelist both so a published canvas
        # table resolves.
        if obj.get("object_type") == "table":
            table_payload = obj.get("table")
            if isinstance(table_payload, dict):
                _add(table_payload)
                for col in table_payload.get("table_columns") or []:
                    _add(col if isinstance(col, dict) else None)
            continue
        # calculated fields query one metric per variable
        # binding; whitelist every variable's data view so a published
        # calc field resolves. Mirrors bindingDataViews() in canvas_view.js.
        if obj.get("object_type") == "calc_field":
            calc = obj.get("calc")
            variables = calc.get("variables") if isinstance(calc, dict) else None
            if isinstance(variables, dict):
                for vbind in variables.values():
                    _add(vbind)
            continue
        if obj.get("object_type") != "data_field":
            continue
        binding = obj.get("binding")
        if not isinstance(binding, dict):
            continue
        if binding.get("computed"):
            _add(binding.get("numerator"))
            _add(binding.get("denominator"))
        else:
            _add(binding)
    return allowed


def _allowed_data_views_for_publish(source: dict) -> set[str]:
    """Return the set of index patterns a public query may target for this dashboard.

    Union of each chart's stored ``data_view`` + the platform template data_views
    for the chart's type. Canvas pages have no charts — their
    whitelist is the union of the live bindings' data views.
    """
    if source.get("dashboard_type") == "canvas":
        return _canvas_binding_data_views(source)

    allowed: set[str] = set()
    charts = (source.get("echarts_config") or {}).get("charts") or []
    if isinstance(charts, dict):
        charts = list(charts.values())
    for chart in charts:
        dv = chart.get("data_view")
        if dv:
            allowed.add(dv)
        platform_dvs = _IMPACT_REPORT_DATA_VIEWS.get(chart.get("chart_type") or "")
        if platform_dvs:
            allowed.update(platform_dvs)
    return allowed


def _strip_org_team_filters(config: dict) -> dict:
    """Return *config* with any ``organization_title`` / ``team_title`` filter removed.

    Public query handler forces these server-side; client-supplied values are
    dropped to prevent scope-escape.
    """
    if not isinstance(config, dict):
        return config
    filters = config.get("filters")
    if not isinstance(filters, list):
        return config
    kept = [f for f in filters if not (isinstance(f, dict) and f.get("field") in ("organization_title", "team_title"))]
    if len(kept) != len(filters):
        config = dict(config)
        config["filters"] = kept
    return config


def _is_publish_expired(source: dict) -> bool:
    """Return True if this dashboard's publish_expires_at is in the past.

    Null / missing ``publish_expires_at`` means "never expires" — the
    publisher explicitly opted out of expiry via the dialog checkbox.
    """
    raw = source.get("publish_expires_at")
    if not raw:
        return False
    try:
        # Accept both naive UTC and aware ISO strings; "Z" suffix → +00:00.
        s = raw.replace("Z", "+00:00") if isinstance(raw, str) else raw
        expires_at = datetime.fromisoformat(s) if isinstance(s, str) else None
    except Exception:
        return False
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(tz=timezone.utc) >= expires_at


# per-(dashboard, scope) publish entries.
#
# Pre-664 a dashboard carried a single publish slot via the flat fields
# (publish_token / publish_enabled / published_at / publish_expires_at /
# published_org_title / published_team_title / display_time_selector). That
# made publish-state per-dashboard-global: publishing for team B revoked
# team A's URL, and the per-row Published pill lit up for every team
# filter regardless of which team minted the link.
#
# adds a ``publishes`` list of entries with the same per-entry
# shape, keyed by the publisher's selected (team_title, org_title) scope.
# The legacy flat fields are still read on the no-regression path so
# existing prod docs keep working until the next publish writes them into
# the list. The publish endpoints take the scope from the payload (the
# already-shipped ``published_org_title`` / ``published_team_title`` keys
# ) so a dialog change in the toolbar's team filter routes
# straight to the right entry.

_PUBLISH_ENTRY_FIELDS = (
    "publish_token",
    "publish_enabled",
    "published_at",
    "publish_expires_at",
    "published_org_title",
    "published_team_title",
    "display_time_selector",
    # optional viewer passcode (hash only). Carried by
    # ``_scoped_source_view`` so the public gate can read it per entry.
    "publish_passcode_hash",
    # city / country / hidden — read per entry by the public map
    # endpoints through the same ``_scoped_source_view`` flattening.
    "publish_map_detail",
)


def _publish_scope_key(team_title, org_title) -> tuple[str, str]:
    """Normalize a (team, org) pair to the tuple used as the entries map key.

    Trims whitespace and coerces None/missing to empty string. team comes
    first because team is the tighter scope (a publish keyed by team
    ignores the org field even if both are set, mirroring
    ``_forced_publish_filters``).
    """
    t = (team_title or "").strip() if isinstance(team_title, (str, bytes)) else ""
    o = (org_title or "").strip() if isinstance(org_title, (str, bytes)) else ""
    return (t, o)


def _legacy_entry_from_flat_fields(source: dict) -> dict | None:
    """Return a synthetic publish entry built from the legacy flat fields, or None.

    A dashboard published earlier stored its single publish slot at the
    top of the doc. Treating it as a one-element ``publishes`` list lets
    every helper iterate entries uniformly without a separate code path.
    Returns None when no legacy publish exists (no token).
    """
    if not source.get("publish_token"):
        return None
    return {key: source.get(key) for key in _PUBLISH_ENTRY_FIELDS}


def _iter_publish_entries(source: dict):
    """Yield every publish entry on the source — both the ``publishes`` list and
    the legacy flat-field entry (if it has a token AND no matching list entry).

    The legacy fallback lets ``_lookup_dashboard_by_token`` and the list
    endpoint keep returning a hit for not-yet-migrated docs without forcing
    a backfill. Once a doc is re-published, the new entry lands in
    ``publishes[]`` and supersedes the legacy snapshot via scope key.
    """
    entries = source.get("publishes") or []
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                yield entry
    legacy = _legacy_entry_from_flat_fields(source)
    if legacy is not None:
        legacy_key = _publish_scope_key(
            legacy.get("published_team_title"),
            legacy.get("published_org_title"),
        )
        # Hide the legacy entry only when the publishes[] already carries
        # an entry for the same scope — otherwise we'd lose the unmigrated
        # publish on a doc that has been partially written.
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            if (
                _publish_scope_key(
                    entry.get("published_team_title"),
                    entry.get("published_org_title"),
                )
                == legacy_key
            ):
                return
        yield legacy


def _entry_is_active(entry: dict) -> bool:
    """True if this entry has an enabled, unexpired token."""
    if not isinstance(entry, dict):
        return False
    if not (entry.get("publish_enabled") and entry.get("publish_token")):
        return False
    return not _is_publish_expired(entry)


def _find_publish_entry(source: dict, scope_key: tuple[str, str]) -> dict | None:
    """Return the entry whose (team, org) matches ``scope_key``, or None.

    Considers both ``publishes[]`` and the legacy flat fields. Exists-but-
    expired entries are returned (callers decide whether expiry matters);
    use ``_entry_is_active`` when you specifically need 'active & unexpired'.
    """
    for entry in _iter_publish_entries(source):
        if (
            _publish_scope_key(
                entry.get("published_team_title"),
                entry.get("published_org_title"),
            )
            == scope_key
        ):
            return entry
    return None


def _any_active_entry(source: dict) -> dict | None:
    """Return any active+unexpired entry on the source, or None."""
    for entry in _iter_publish_entries(source):
        if _entry_is_active(entry):
            return entry
    return None


def _tokens_lookup_array(publishes: list[dict]) -> list[str]:
    """Mirror every entry's ``publish_token`` into a top-level string array.

    Prod's dynamic mapping treats nested-object string fields like
    ``publishes.publish_token`` as analyzed text WITHOUT a ``.keyword``
    subfield, so a term query against the per-entry token never matches.
    Top-level string fields, however, get the standard dynamic template
    (text + keyword) — so this flat array carries a usable lookup
    surface for ``_lookup_dashboard_by_token``. Kept in sync on every
    publish/unpublish write so a doc's lookup list always reflects its
    publishes[].
    """
    out: list[str] = []
    for entry in publishes or []:
        tok = entry.get("publish_token") if isinstance(entry, dict) else None
        if tok:
            out.append(tok)
    return out


def _build_publishes_list_for_write(source: dict, scope_key: tuple[str, str], entry: dict | None) -> list[dict]:
    """Compute the ``publishes`` list to write back to ES.

    Starts from the doc's existing ``publishes`` (or migrates the legacy
    flat-field entry into a single-element list when ``publishes`` is
    absent), then either replaces the entry at ``scope_key`` with
    ``entry`` (mint / regenerate / update path) or removes it
    (``entry is None``; unpublish path).
    """
    existing = source.get("publishes") or []
    out: list[dict] = []
    if isinstance(existing, list) and existing:
        out = [dict(e) for e in existing if isinstance(e, dict)]
    else:
        legacy = _legacy_entry_from_flat_fields(source)
        if legacy is not None:
            out = [legacy]

    # Drop any existing entry at this scope key — we either replace it
    # below or are removing it (unpublish).
    out = [
        e
        for e in out
        if _publish_scope_key(
            e.get("published_team_title"),
            e.get("published_org_title"),
        )
        != scope_key
    ]
    # purge expired sibling entries while we hold the pen. Expiry
    # was only ever enforced at lookup time, so a dead token (and its
    # passcode hash) otherwise sat on the doc forever with
    # ``publish_enabled: true`` — the 2026-09-09 prod audit found exactly
    # that. An expired entry is unreachable, so dropping it changes nothing
    # the viewer can see; a re-publish of that scope mints a fresh token.
    out = [e for e in out if not _is_publish_expired(e)]
    if entry is not None:
        out.append(entry)
    return out


def _scoped_source_view(source: dict, entry: dict) -> dict:
    """Return a copy of ``source`` with ``entry``'s fields flattened on top.

    The public viewer (``_forced_publish_filters``,
    ``_publish_state_response``) reads the flat field names from a single
    dict. Per-entry lookup hands them a source where the entry's
    ``publish_token`` / ``published_team_title`` / etc. shadow whatever the
    top-level doc carries, so the existing helpers keep working without
    each one growing a scope parameter.
    """
    out = dict(source)
    for key in _PUBLISH_ENTRY_FIELDS:
        if key in entry:
            out[key] = entry[key]
    return out


def _lookup_dashboard_by_token(token: str) -> tuple[str | None, dict | None]:
    """Return (doc_id, source) for the dashboard with this publish_token, or (None, None).

    a published dashboard whose ``publish_expires_at`` is in the past
    is treated as if the token had been revoked — the public route 404s.
    Filtering happens in Python so we don't need to push date math into ES
    (and so the existing test fixtures keep working).

    tokens now live per-(dashboard, scope) inside ``publishes[]``.
    The lookup matches either the new array path or the legacy flat
    ``publish_token`` and returns a source view with the matching entry's
    snapshot flattened on top — so downstream helpers
    (``_forced_publish_filters``, ``_allowed_data_views_for_publish``) keep
    reading ``published_team_title`` / ``published_org_title`` etc. from a
    single dict without a scope parameter. Each entry's expiry is checked
    independently; an expired entry on the same dashboard does not hide a
    sibling entry's still-active token.
    """
    if not token:
        return None, None
    es = get_elasticsearch_client()
    if es is None:
        return None, None
    try:
        # Match against either:
        #   - the legacy top-level ``publish_token.keyword`` (pre-664 docs)
        #   - the new top-level ``publish_tokens_lookup.keyword`` mirror
        # array
        # Avoid querying ``publishes.publish_token.keyword`` directly:
        # prod's dynamic mapping doesn't keywordize nested-object string
        # fields, so that path silently never matches. Active-entry
        # filtering is done in Python below.
        resp = es.search(
            index=DASHBOARDS_INDEX,
            body={
                "size": 1,
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"publish_token.keyword": token}},
                            {"term": {"publish_tokens_lookup.keyword": token}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
            },
        )
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
        hits = (resp_dict.get("hits") or {}).get("hits") or []
        if not hits:
            return None, None
        hit = hits[0]
        source = hit.get("_source") or {}
        doc_id = hit.get("_id")

        # Walk every entry on the doc (publishes[] + the legacy flat
        # fields) and return the first whose token matches AND that is
        # still active. This is what isolates per-entry expiry — without
        # it an expired team-A entry would mask an active team-B entry
        # that happened to share a doc.
        for entry in _iter_publish_entries(source):
            if entry.get("publish_token") != token:
                continue
            if not _entry_is_active(entry):
                return None, None
            return doc_id, _scoped_source_view(source, entry)
        return None, None
    except Exception as e:
        logging.error(f"Error looking up dashboard by publish token: {e}")
        return None, None


def _publish_state_response(dashboard_id: str, source: dict, scope_key: tuple[str, str] | None = None) -> dict:
    """Shape the JSON response for the publish-state endpoints.

    Includes ``eligible`` so the UI can gate the Publish button without
    guessing at the rules client-side.

     additions:
      - ``can_publish`` reflects whether the *current user* may mint or
        revoke a token. The frontend hides the Publish button when False.
      - ``expires_at`` and ``expired`` expose the configurable expiry so
        the dialog can render "Expires on …" and the public route's 404
        on expired tokens is observable to the publisher.
      - ``published_org_title`` / ``published_team_title`` are the publisher-
        selected scope snapshot used by the public query route.

    a publish lives per-(dashboard, scope). Pass ``scope_key`` so
    the response reflects ONE entry — the one for the team currently
    selected in the toolbar. When ``scope_key`` is None the response
    answers 'is ANY entry active for this dashboard' (used by the list
    endpoint's no-team-filter fall-back).
    """
    if scope_key is None:
        # No specific scope requested — report on any active entry.
        entry = _any_active_entry(source) or {}
    else:
        entry = _find_publish_entry(source, scope_key) or {}

    token = entry.get("publish_token")
    enabled = bool(entry.get("publish_enabled") and token)
    expired = _is_publish_expired(entry) if token else False
    effective_published = enabled and not expired
    eligible, reason = _is_publish_eligible(source)
    return {
        "success": True,
        "id": dashboard_id,
        "published": effective_published,
        "token": token if effective_published else None,
        "path": f"/public/report/{token}" if effective_published else None,
        "published_at": entry.get("published_at"),
        "expires_at": entry.get("publish_expires_at"),
        "expired": expired and enabled,
        "published_org_title": entry.get("published_org_title") or None,
        "published_team_title": entry.get("published_team_title") or None,
        "display_time_selector": bool(entry.get("display_time_selector")),
        # the dialog shows "passcode protected" — never the hash.
        "passcode_protected": bool(entry.get("publish_passcode_hash")),
        # what the public map may show for this link.
        "map_detail": _entry_map_detail(entry),
        "map_detail_default": _PUBLISH_DEFAULT_MAP_DETAIL,
        "eligible": eligible,
        "eligibility_reason": reason if not eligible else None,
        "can_publish": _current_user_can_publish_reports(),
    }


def _coerce_expires_at(payload: dict) -> tuple[str | None, str | None]:
    """Translate publish-dialog input into an ISO ``publish_expires_at`` value.

    Returns ``(expires_at_iso_or_none, error_message_or_none)``.

    Accepted shapes (all optional — defaults to 90 days):
      - ``{"never_expires": true, "confirm_never_expires": true}`` → returns
        ``(None, None)`` (no expiry). Without the confirmation the request
        is rejected — a never-expiring public link must be a deliberate,
        acknowledged choice.
      - ``{"expires_in_days": 14}`` → returns now + 14 days, ISO 8601.
      - ``{}`` or no payload → returns now + 90 days.

    Rejects negative days and absurdly large values to keep the stored
    timestamp inside ES's date range.
    """
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("never_expires") is True:
        if payload.get("confirm_never_expires") is not True:
            return None, "A link that never expires must be confirmed (confirm_never_expires)."
        return None, None

    raw = payload.get("expires_in_days", _PUBLISH_DEFAULT_EXPIRY_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None, "expires_in_days must be a whole number."
    if days < 1:
        return None, "expires_in_days must be at least 1."
    if days > _PUBLISH_MAX_EXPIRY_DAYS:
        return None, f"expires_in_days must be {_PUBLISH_MAX_EXPIRY_DAYS} or fewer."

    expiry = datetime.now(tz=timezone.utc) + timedelta(days=days)
    return expiry.isoformat(), None


def _hash_publish_passcode(passcode: str) -> str:
    """scrypt-hash a viewer passcode for storage on a publish entry."""
    from werkzeug.security import generate_password_hash

    return generate_password_hash(passcode, method="scrypt")


def _check_publish_passcode(entry: dict, passcode: str) -> bool:
    """True when ``passcode`` matches the entry's stored hash. An entry with no
    hash never matches — callers gate on ``publish_passcode_hash`` first."""
    from werkzeug.security import check_password_hash

    stored = (entry or {}).get("publish_passcode_hash")
    if not stored or not isinstance(passcode, str):
        return False
    try:
        return check_password_hash(stored, passcode)
    except ValueError:
        # Unparseable hash on the doc — treat as no match, never as open.
        return False


def _coerce_passcode(payload: dict, existing_entry: dict | None) -> tuple[str | None, str | None]:
    """Resolve the passcode hash to write for a publish entry.

    Returns ``(hash_or_none, error_or_none)``:
      - ``{"clear_passcode": true}`` → ``(None, None)`` — passcode removed.
      - ``{"passcode": "…"}`` → a fresh scrypt hash of the (stripped) value;
        4–64 characters, else an error.
      - neither key → the existing entry's hash is preserved (or None).
    The plaintext never leaves this function.
    """
    if not isinstance(payload, dict):
        payload = {}
    if payload.get("clear_passcode") is True:
        return None, None
    if "passcode" in payload:
        raw = payload.get("passcode")
        if not isinstance(raw, str):
            return None, "passcode must be a string."
        passcode = raw.strip()
        if not passcode:
            # Blank input in the dialog means "leave it as it is".
            return (existing_entry or {}).get("publish_passcode_hash"), None
        if len(passcode) < _PUBLISH_PASSCODE_MIN_LEN:
            return None, f"passcode must be at least {_PUBLISH_PASSCODE_MIN_LEN} characters."
        if len(passcode) > _PUBLISH_PASSCODE_MAX_LEN:
            return None, f"passcode must be {_PUBLISH_PASSCODE_MAX_LEN} characters or fewer."
        return _hash_publish_passcode(passcode), None
    return (existing_entry or {}).get("publish_passcode_hash"), None


def _entry_map_detail(entry: dict | None) -> str:
    """Normalised ``map_detail`` for a publish entry.

    A missing or unrecognised stored value reads as ``city`` — the behaviour
    every link published before 1513 had — so the switch never silently
    changes an existing link; only a publisher's explicit choice does.
    """
    value = (entry or {}).get("publish_map_detail")
    if isinstance(value, str) and value in _PUBLISH_MAP_DETAIL_CHOICES:
        return value
    return _PUBLISH_LEGACY_MAP_DETAIL


def _coerce_map_detail(payload: dict, existing_entry: dict | None) -> tuple[str, str | None]:
    """Resolve the ``publish_map_detail`` value to write.

    Returns ``(value, error_or_none)``:
      - ``{"map_detail": "city" | "country" | "hidden"}`` → that value;
        anything else is an error (never coerced to a default — a typo in a
        client must not silently widen or narrow what a link shows).
      - key absent → the existing entry's value when there is one, else
        ``_PUBLISH_DEFAULT_MAP_DETAIL`` for a brand-new link.
    """
    if not isinstance(payload, dict):
        payload = {}
    if "map_detail" in payload:
        raw = payload.get("map_detail")
        if not isinstance(raw, str) or raw.strip().lower() not in _PUBLISH_MAP_DETAIL_CHOICES:
            return _PUBLISH_DEFAULT_MAP_DETAIL, "map_detail must be one of: " + ", ".join(_PUBLISH_MAP_DETAIL_CHOICES) + "."
        return raw.strip().lower(), None
    if existing_entry and existing_entry.get("publish_token"):
        return _entry_map_detail(existing_entry), None
    return _PUBLISH_DEFAULT_MAP_DETAIL, None


def _coerce_display_time_selector(payload: dict) -> bool | None:
    """Return the publisher's choice for the public viewer time-selector visibility.

    the publish dialog carries a checkbox ("Display time selector").
    Returns ``True``/``False`` when the payload includes the key, else ``None``
    so callers can preserve the existing stored value (regenerate path).
    """
    if not isinstance(payload, dict) or "display_time_selector" not in payload:
        return None
    return bool(payload.get("display_time_selector"))


def _publisher_scope_snapshot(payload: dict) -> dict:
    """Pull the publisher's currently-selected org/team out of the publish payload.

    the public report is scoped to the org/team selected by the
    publisher at publish time, regardless of who later loads it. The frontend
    sends the titles directly (matching what the user sees in the dropdowns)
    so the server doesn't need to re-resolve ESIDs.

    Empty / missing fields fall through to the dashboard's general
    ``allowed_*_esids`` scope in ``_forced_publish_filters``.
    """
    if not isinstance(payload, dict):
        return {}
    snapshot: dict = {}
    org_title = (payload.get("published_org_title") or "").strip()
    team_title = (payload.get("published_team_title") or "").strip()
    if org_title:
        snapshot["published_org_title"] = org_title
    if team_title:
        snapshot["published_team_title"] = team_title
    return snapshot


def _validate_publisher_scope_snapshot(source: dict, snapshot: dict) -> str | None:
    """confirm a publisher-supplied scope snapshot stays WITHIN the
    dashboard's own ``allowed_team_esids`` / ``allowed_org_esids``.

    The publish/regenerate routes let the publisher send free-form
    ``published_team_title`` / ``published_org_title`` strings that are stamped
    verbatim into the publish entry and later become the SOLE tenant filter the
    public viewer runs (``_forced_publish_filters`` → ``team_title``
    match_phrase). Without this check a publisher could stamp ANOTHER tenant's
    team/org title and mint a public link that serves that tenant's data — a
    cross-tenant read (the dashboard's chart data-views are shared platform
    wildcard indices whose only tenant boundary is the team/org title).

    The snapshot may only RESTRICT to a team/org the dashboard is already scoped
    to (the same set the public viewer would otherwise infer from
    ``allowed_*_esids``). We validate in title-space because that's the shape
    both the snapshot and the data filter use, and it's exactly the set
    ``_forced_publish_filters`` resolves the esids to.

    Returns ``None`` when the snapshot is valid (or empty — the unscoped slot
    falls through to the dashboard's general scope), else a user-facing error
    string the caller turns into a 400.
    """
    team_title = (snapshot.get("published_team_title") or "").strip()
    org_title = (snapshot.get("published_org_title") or "").strip()
    if not team_title and not org_title:
        return None  # unscoped slot — inherits allowed_*_esids at view time

    allowed_team_titles = set(_resolve_team_titles(source.get("allowed_team_esids") or []))
    allowed_org_titles = set(_resolve_org_titles(source.get("allowed_org_esids") or []))

    if team_title and team_title not in allowed_team_titles:
        return "Selected team is outside this dashboard's allowed scope."
    if org_title and org_title not in allowed_org_titles:
        return "Selected organization is outside this dashboard's allowed scope."
    return None


def _scope_key_from_request() -> tuple[str, str] | None:
    """Pull (team, org) scope out of the publish-endpoint request.

    GET/DELETE carry the scope on the query string (``?team=...&org=...``);
    POST/regenerate carry it in the JSON body (``published_team_title`` /
    ``published_org_title``, the keys already shipped).

    Returns ``None`` only for the GET no-arg case where the caller is
    explicitly asking 'any entry active?' — every write endpoint resolves
    to a concrete (team, org) pair (empty strings included, which keys
    the unscoped publish slot).
    """
    team = request.args.get("team")
    org = request.args.get("org")
    if team is not None or org is not None:
        return _publish_scope_key(team, org)
    if request.method in ("POST", "DELETE"):
        payload = request.get_json(force=True, silent=True) or {}
        if isinstance(payload, dict):
            team_p = payload.get("published_team_title")
            org_p = payload.get("published_org_title")
            if team_p is not None or org_p is not None:
                return _publish_scope_key(team_p, org_p)
            # No scope hints anywhere → treat as the unscoped slot ("","")
            return _publish_scope_key("", "")
    return None


@dashboard_bp.route("/api/dashboards/<dashboard_id>/publish", methods=["GET"])
def get_publish_state(dashboard_id: str):
    """Return current publish state for a dashboard, scoped to ?team= / ?org=.

    the dialog passes the current team-filter selection so the
    response reflects 'is THIS team's view of the dashboard published?'.
    With no params the response answers 'is ANY entry active?' which
    preserves the pre-664 single-publish view for callers that haven't
    been updated.
    """
    try:
        source = _load_dashboard_for_read(dashboard_id)
        scope_key = _scope_key_from_request()
        return jsonify(_publish_state_response(dashboard_id, source, scope_key))
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error fetching publish state",
            log_detail=f"get_publish_state: failed for dashboard_id '{dashboard_id}'",
            exc=e,
        )


@dashboard_bp.route("/api/dashboards/<dashboard_id>/publish", methods=["POST"])
def publish_dashboard(dashboard_id: str):
    """Publish the dashboard for the publisher's selected (team, org) scope.

    each (dashboard, scope) pair has its own publish entry in
    ``publishes[]``. Publishing for team B leaves team A's entry intact
    so the two URLs coexist. Idempotent within a scope — re-POSTing with
    the same scope and no expiry/scope changes returns the same token.

     contract preserved:
    - ``publish_reports`` gate
    - ``expires_in_days`` / ``never_expires`` choose link lifetime
    - ``published_org_title`` / ``published_team_title`` define the scope
      (these now also key the entry; an empty pair keys the unscoped slot)
    - expired-token POST cleanly issues a new token for that scope
    """
    try:
        _require_publish_reports()
        source = _load_dashboard_for_read(dashboard_id)

        ok, reason = _is_publish_eligible(source)
        if not ok:
            return jsonify({"success": False, "error": reason}), 400

        payload = request.get_json(force=True, silent=True) or {}
        expires_at, expiry_err = _coerce_expires_at(payload)
        if expiry_err:
            return jsonify({"success": False, "error": expiry_err}), 400
        scope_snapshot = _publisher_scope_snapshot(payload)
        # the publisher-supplied team/org title must be within this
        # dashboard's own allowed scope — it becomes the public viewer's sole
        # tenant filter, so a free-form foreign title would leak another
        # tenant's data through the public link.
        scope_err = _validate_publisher_scope_snapshot(source, scope_snapshot)
        if scope_err:
            return jsonify({"success": False, "error": scope_err}), 403
        time_selector_choice = _coerce_display_time_selector(payload)

        scope_key = _publish_scope_key(
            scope_snapshot.get("published_team_title"),
            scope_snapshot.get("published_org_title"),
        )
        existing_entry = _find_publish_entry(source, scope_key)
        existing_unexpired = _entry_is_active(existing_entry or {})
        # optional viewer passcode — set, clear, or (default) keep.
        # An expired entry's passcode is not inherited by the fresh token.
        passcode_hash, passcode_err = _coerce_passcode(payload, existing_entry if existing_unexpired else None)
        if passcode_err:
            return jsonify({"success": False, "error": passcode_err}), 400
        # map detail — explicit choice, else keep the live entry's,
        # else the safe default for a fresh link.
        map_detail, map_detail_err = _coerce_map_detail(payload, existing_entry if existing_unexpired else None)
        if map_detail_err:
            return jsonify({"success": False, "error": map_detail_err}), 400

        # Idempotent within this scope: no expiry/scope/visibility/passcode/
        # map-detail change → return the existing entry's URL, do not touch ES.
        no_changes_requested = (
            "expires_in_days" not in payload
            and "never_expires" not in payload
            and "passcode" not in payload
            and "clear_passcode" not in payload
            and "map_detail" not in payload
            and not scope_snapshot
            and time_selector_choice is None
        )
        if existing_unexpired and no_changes_requested:
            return jsonify(_publish_state_response(dashboard_id, source, scope_key))

        # Reuse the existing token for this scope when the caller is only
        # updating expiry/scope (rotation belongs to /publish/regenerate).
        token = existing_entry.get("publish_token") if existing_unexpired and existing_entry else secrets.token_urlsafe(32)
        now = datetime.now(tz=timezone.utc).isoformat()
        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        new_entry = {
            "publish_token": token,
            "publish_enabled": True,
            "published_at": now,
            "publish_expires_at": expires_at,
            # Always write the snapshot keys (even blank) so a re-publish
            # of the unscoped slot can never inherit stale scope.
            "published_org_title": scope_snapshot.get("published_org_title"),
            "published_team_title": scope_snapshot.get("published_team_title"),
            # default off so new publishes ship with the fixed
            # window unless the publisher explicitly opts in.
            "display_time_selector": bool(time_selector_choice) if time_selector_choice is not None else False,
            # hash only; None when the link is open.
            "publish_passcode_hash": passcode_hash,
            # city / country / hidden.
            "publish_map_detail": map_detail,
        }

        new_publishes = _build_publishes_list_for_write(source, scope_key, new_entry)
        update_doc = {
            "publishes": new_publishes,
            # follow-up: mirror tokens into a top-level string
            # array so the lookup query has a .keyword surface to term
            # against (prod's dynamic mapping doesn't keywordize the
            # per-entry publishes.publish_token field).
            "publish_tokens_lookup": _tokens_lookup_array(new_publishes),
            "updated_at": now,
        }
        # Migration step: clear the legacy flat fields once we own the
        # entry list, so future writes don't have to keep the two
        # representations in sync. Re-readers (token lookup, list
        # endpoint) walk ``publishes[]`` from now on.
        if source.get("publish_token") is not None or source.get("publish_enabled") is not None:
            for key in _PUBLISH_ENTRY_FIELDS:
                update_doc[key] = None

        es.update(
            index=DASHBOARDS_INDEX,
            id=dashboard_id,
            doc=update_doc,
            refresh="wait_for",
        )

        source.update(update_doc)
        return jsonify(_publish_state_response(dashboard_id, source, scope_key))
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error publishing dashboard",
            log_detail=f"publish_dashboard: failed for dashboard_id '{dashboard_id}'",
            exc=e,
        )


@dashboard_bp.route("/api/dashboards/<dashboard_id>/publish/regenerate", methods=["POST"])
def regenerate_publish_token(dashboard_id: str):
    """Rotate this scope's publish token — invalidates only this scope's URL.

    regenerate is per-(dashboard, scope). Rotating team A's token
    leaves team B's URL valid.

     contract preserved:
    - ``expires_in_days`` / ``never_expires`` may extend or shorten the
      lifetime when rotating
    - ``published_org_title`` / ``published_team_title`` define the scope
    - omitting any of these preserves the prior entry's value
    """
    try:
        _require_publish_reports()
        source = _load_dashboard_for_read(dashboard_id)

        ok, reason = _is_publish_eligible(source)
        if not ok:
            return jsonify({"success": False, "error": reason}), 400

        payload = request.get_json(force=True, silent=True) or {}

        scope_snapshot = _publisher_scope_snapshot(payload)
        scope_key = _publish_scope_key(
            scope_snapshot.get("published_team_title"),
            scope_snapshot.get("published_org_title"),
        )
        existing_entry = _find_publish_entry(source, scope_key) or {}

        if "expires_in_days" in payload or "never_expires" in payload:
            expires_at, expiry_err = _coerce_expires_at(payload)
            if expiry_err:
                return jsonify({"success": False, "error": expiry_err}), 400
        else:
            expires_at = existing_entry.get("publish_expires_at")

        org_title = scope_snapshot.get("published_org_title", existing_entry.get("published_org_title"))
        team_title = scope_snapshot.get("published_team_title", existing_entry.get("published_team_title"))
        # validate the EFFECTIVE scope (incoming snapshot OR preserved
        # prior entry) against the dashboard's allowed_*_esids before re-minting
        # a token — same cross-tenant guard as publish_dashboard, and it also
        # catches a stale out-of-scope title left by a pre-fix publish.
        scope_err = _validate_publisher_scope_snapshot(
            source,
            {"published_team_title": team_title, "published_org_title": org_title},
        )
        if scope_err:
            return jsonify({"success": False, "error": scope_err}), 403
        # preserve prior visibility choice when the caller omits it.
        time_selector_choice = _coerce_display_time_selector(payload)
        if time_selector_choice is None:
            time_selector_choice = bool(existing_entry.get("display_time_selector"))
        # rotating the token keeps the passcode unless the caller
        # sets a new one or clears it.
        passcode_hash, passcode_err = _coerce_passcode(payload, existing_entry)
        if passcode_err:
            return jsonify({"success": False, "error": passcode_err}), 400
        # rotating the token keeps the map-detail choice unless the
        # caller sends a new one.
        map_detail, map_detail_err = _coerce_map_detail(payload, existing_entry)
        if map_detail_err:
            return jsonify({"success": False, "error": map_detail_err}), 400

        token = secrets.token_urlsafe(32)
        now = datetime.now(tz=timezone.utc).isoformat()
        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        new_entry = {
            "publish_token": token,
            "publish_enabled": True,
            "published_at": now,
            "publish_expires_at": expires_at,
            "published_org_title": org_title,
            "published_team_title": team_title,
            "display_time_selector": bool(time_selector_choice),
            "publish_passcode_hash": passcode_hash,
            "publish_map_detail": map_detail,
        }
        new_publishes = _build_publishes_list_for_write(source, scope_key, new_entry)
        update_doc = {
            "publishes": new_publishes,
            # follow-up: mirror tokens into a top-level string
            # array so the lookup query has a .keyword surface to term
            # against (prod's dynamic mapping doesn't keywordize the
            # per-entry publishes.publish_token field).
            "publish_tokens_lookup": _tokens_lookup_array(new_publishes),
            "updated_at": now,
        }
        # Same migration step as publish_dashboard — once we own the entry
        # list, retire the legacy flat fields on the doc.
        if source.get("publish_token") is not None or source.get("publish_enabled") is not None:
            for key in _PUBLISH_ENTRY_FIELDS:
                update_doc[key] = None

        es.update(
            index=DASHBOARDS_INDEX,
            id=dashboard_id,
            doc=update_doc,
            refresh="wait_for",
        )

        source.update(update_doc)
        return jsonify(_publish_state_response(dashboard_id, source, scope_key))
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error regenerating publish token",
            log_detail=f"regenerate_publish_token: failed for dashboard_id '{dashboard_id}'",
            exc=e,
        )


@dashboard_bp.route("/api/dashboards/<dashboard_id>/publish", methods=["DELETE"])
def unpublish_dashboard(dashboard_id: str):
    """Revoke the publish token for this (dashboard, scope) — public URL 404s.

    unpublishing team A removes only A's entry. Team B's URL
    stays valid. The DELETE payload's ``published_team_title`` /
    ``published_org_title`` (or query params on ``?team=&org=``) identify
    which entry to drop.
    """
    try:
        _require_publish_reports()
        source = _load_dashboard_for_read(dashboard_id)

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"success": False, "error": "Elasticsearch unavailable"}), 503

        scope_key = _scope_key_from_request() or _publish_scope_key("", "")

        new_publishes = _build_publishes_list_for_write(source, scope_key, None)
        now = datetime.now(tz=timezone.utc).isoformat()
        update_doc = {
            "publishes": new_publishes,
            "publish_tokens_lookup": _tokens_lookup_array(new_publishes),
            "updated_at": now,
        }
        # If the legacy flat fields point at the scope being dropped, also
        # clear them so a stale snapshot can't resurrect the URL on the
        # next read. (Other scopes' entries are preserved inside
        # publishes[] via _build_publishes_list_for_write.)
        legacy_key = _publish_scope_key(
            source.get("published_team_title"),
            source.get("published_org_title"),
        )
        if source.get("publish_token") is not None and legacy_key == scope_key:
            for key in _PUBLISH_ENTRY_FIELDS:
                update_doc[key] = None

        es.update(
            index=DASHBOARDS_INDEX,
            id=dashboard_id,
            doc=update_doc,
            refresh="wait_for",
        )

        return jsonify({"success": True, "id": dashboard_id, "published": False})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error unpublishing dashboard",
            log_detail=f"unpublish_dashboard: failed for dashboard_id '{dashboard_id}'",
            exc=e,
        )


# ══════════════════════════════════════════════════════════════
# Workspace menus —
# ══════════════════════════════════════════════════════════════
#
# Per-workspace configurable Dashboard Builder menus. Reads gated by
# ``mc_dashboard_builder`` (anyone who can open the builder can see the
# menu list). Writes gated by ``workspace_admin`` or ``workspace_owner``
# on the current workspace (System 2 roles seeded).
#
# user decision (in plan): no global-superuser bypass on
# writes — a support engineer who needs to edit a tenant's menus must
# hold workspace_admin on that workspace via the System 2 membership
# data, the same posture other workspace-scoped actions take.

_WORKSPACE_ADMIN_ROLES = frozenset({"workspace_admin", "workspace_owner"})


def _current_workspace_esid_or_abort():
    """Resolve the current user's primary workspace, or 403 if none."""
    from asset_management.web.services.workspace_membership_service import (
        WorkspaceMembershipService,
    )

    username = session.get("username", "") or ""
    if not username:
        _abort_json(403, "Not authenticated.")
    workspace_esid = WorkspaceMembershipService.get_primary_workspace(username)
    if not workspace_esid:
        _abort_json(403, "No workspace membership.")
    return username, workspace_esid


def _menu_write_workspace_or_abort():
    """resolve the workspace a menu WRITE should target.

    The Builder topbar workspace selector lets a multi-workspace
    user — typically a superuser — act in a workspace that is NOT their
    primary one. The dashboard save path already honors the selected
    ``workspace_esid`` (see ``save_dashboard``); the menu write endpoints
    did not, so they resolved only ``get_primary_workspace`` and 403'd for
    superusers (who have no workspace-role doc earlier) — the menu
    drag-reorder silently lost the new order.

    Resolution mirrors the stamp validation in ``save_dashboard``:

    * Take the client-supplied ``workspace_esid`` (JSON body, then query
      string — DELETE has no body).
    * Accept it only if it is a workspace the caller can resolve
      (``get_allowed_workspaces`` — ``None`` means privileged/all, i.e. a
      superuser, so any workspace is accepted). A crafted payload can't
      target a foreign workspace.
    * Fall back to the primary workspace when no valid selection is
      supplied (the single-workspace-admin case is unchanged).

    Aborts 403 when nothing resolves. Returns ``(username, workspace_esid)``.
    """
    from asset_management.web.services.workspace_membership_service import (
        WorkspaceMembershipService,
    )

    username = session.get("username", "") or ""
    if not username:
        _abort_json(403, "Not authenticated.")

    data = request.get_json(silent=True) or {}
    requested_ws = (data.get("workspace_esid") or request.args.get("workspace_esid") or "").strip()
    if requested_ws:
        try:
            allowed_ws = RoleFilterService.get_allowed_workspaces()
        except Exception:  # noqa: BLE001 — degrade to the primary-workspace fallback
            allowed_ws = []
        if allowed_ws is None or requested_ws in set(allowed_ws or []):
            return username, requested_ws

    workspace_esid = WorkspaceMembershipService.get_primary_workspace(username)
    if not workspace_esid:
        _abort_json(403, "No workspace membership.")
    return username, workspace_esid


def _menus_for_current_user():
    """resolve the current user's workspace MENUS for a READ.

    The menu-list read path (GET ``/api/menus``) used
    :func:`_current_workspace_esid_or_abort`, which resolves only
    ``get_primary_workspace`` and 403s when it returns None.
    repointed ``get_primary_workspace`` onto the workspace ROLE index, so
    an org-admin / team-grant / grant-holder user (scope outside
    ``de_authz_user_workspace_role``) now resolves None — the Builder's
    ``/api/menus`` 403'd and its left panel fell back to alphabetical
    order, even though the dashboards page (which got the
    resolver fallback) rendered correctly.

    Mirror the dashboards-page resolution (analytics/routes.py): prefer
    the primary workspace, then fall back to the
    ``get_allowed_workspaces`` walk when the primary yields no menus,
    picking the first resolved workspace that actually has menus. Returns
    ``[]`` (never raises/aborts) when nothing resolves — the picker then
    falls through to its synthetic ``"Reports"`` default, the same
    degrade as a brand-new workspace.

    This is READ-ONLY. The menu WRITE endpoints keep
    :func:`_current_workspace_esid_or_abort` +
    :func:`_require_workspace_admin`, so write authz is unchanged.
    """
    from asset_management.web.services.workspace_membership_service import (
        WorkspaceMembershipService,
    )
    from asset_management.web.services.workspace_menu_service import (
        WorkspaceMenuService,
    )

    username = session.get("username", "") or ""
    if not username:
        return []

    # honor the Builder topbar workspace selector first, so a
    # superuser's menu READ matches what the menu WRITE endpoints target
    # (see _menu_write_workspace_or_abort). Without this, a superuser
    # acting in a non-primary workspace would see one workspace's menus
    # but reorder/add against the selected one — the dragged menu_esids
    # wouldn't match and the order would be lost. Same allow-list
    # validation as the write path (None = privileged/all).
    selected_ws = (request.args.get("workspace_esid") or "").strip()
    if selected_ws:
        try:
            allowed_ws = RoleFilterService.get_allowed_workspaces()
        except Exception:  # noqa: BLE001 — fall through to the primary resolution
            allowed_ws = []
        if allowed_ws is None or selected_ws in set(allowed_ws or []):
            return WorkspaceMenuService.list_menus(selected_ws)

    ws_esid = WorkspaceMembershipService.get_primary_workspace(username)
    menus = WorkspaceMenuService.list_menus(ws_esid) if ws_esid else []
    if not menus:
        for candidate in RoleFilterService.get_allowed_workspaces() or []:
            if candidate == ws_esid:
                continue
            candidate_menus = WorkspaceMenuService.list_menus(candidate)
            if candidate_menus:
                menus = candidate_menus
                break
    return menus


def _default_menu_for_current_workspace() -> str:
    """Pick a sensible default menu title for a brand-new dashboard.

    Used by ``save_dashboard`` when the create-time payload omits a
    ``menu``. Returns the first (position 0) menu in the user's
    primary workspace, falling back to the legacy ``"Reports"`` default
    if the user has no workspace, the workspace has no menus, or the
    WorkspaceMenuService is unavailable. ``"Reports"`` is also the last
    seeded default, so this fallback only kicks in for the rare case
    where the seed itself failed.
    """
    try:
        from asset_management.web.services.workspace_membership_service import (
            WorkspaceMembershipService,
        )
        from asset_management.web.services.workspace_menu_service import (
            WorkspaceMenuService,
        )

        username = session.get("username", "") or ""
        if not username:
            return "Reports"
        workspace_esid = WorkspaceMembershipService.get_primary_workspace(username)
        if not workspace_esid:
            return "Reports"
        menus = WorkspaceMenuService.list_menus(workspace_esid)
        if menus:
            return menus[0]["title"]
    except Exception:
        pass
    return "Reports"


def _require_workspace_admin(username: str, workspace_esid: str) -> None:
    """Abort with 403 unless the user holds workspace_admin/owner on this workspace.

    superusers bypass the membership requirement — they curate
    menus across workspaces via the Builder topbar selector and have no
    per-workspace membership doc. This matches the disclosure short-circuit
    in :func:`_current_user_is_workspace_admin` (which renders the drag
    handles + "+ Add Menu" for superusers) and the Save-as-template authz
    (:func:`_freeze_template_authz_or_abort`). It deliberately reverses
     original no-superuser-bypass posture (user-approved
    2026-06-08): without it, the very handles the UI shows superusers POST
    to an endpoint that 403s them. Non-superusers still require
    workspace_admin/owner on the resolved workspace.
    """
    if "superuser" in (session.get("roles") or []):
        return

    from asset_management.web.services.workspace_membership_service import (
        WorkspaceMembershipService,
    )

    role = WorkspaceMembershipService.get_role(username, workspace_esid)
    if role not in _WORKSPACE_ADMIN_ROLES:
        _abort_json(403, "Workspace admin permission is required.")


def _current_user_is_workspace_admin() -> bool:
    """Boolean form of :func:`_require_workspace_admin` for UI flags.

    Used by the builder index route to render-time-gate the "+ Add Menu"
    button, the "Save as template" button,
    and menu drag-handles. Returns ``False`` for any of: not logged in,
    no workspace membership, role not in :data:`_WORKSPACE_ADMIN_ROLES`.
    Server-side write gates still apply independently — this only
    governs disclosure.

     slice 2 follow-up (2026-05-21): superusers short-circuit
    to True even without a workspace membership. The Save-as-template
    route's authz function (:func:`_freeze_template_authz_or_abort`)
    already had this bypass; without it here the button was hidden
    from superusers even though the route would accept their POSTs.
    Same applies to the "+ Add Menu" surface — superusers
    curating menus shouldn't depend on having a workspace assignment.
    """
    try:
        username = session.get("username", "") or ""
        if not username:
            return False
        # Superuser bypass — platform admins can curate without a workspace
        # membership. Mirrors _freeze_template_authz_or_abort + the
        # existing IS_SUPERUSER pattern in _current_user_can_build_dashboards.
        if "superuser" in (session.get("roles") or []):
            return True

        from asset_management.web.services.workspace_membership_service import (
            WorkspaceMembershipService,
        )

        workspace_esid = WorkspaceMembershipService.get_primary_workspace(username)
        if not workspace_esid:
            return False
        role = WorkspaceMembershipService.get_role(username, workspace_esid)
        return role in _WORKSPACE_ADMIN_ROLES
    except Exception:
        return False


def _builder_visibility_defaults(is_workspace_admin: bool) -> dict:
    """Compute the new-dashboard visibility defaults for the Dashboard
    Builder, shaped for the template / ``builder.js``.

    Returns a dict::

        {
            "show_all_allowed": bool, # always False
            "default_mode": str, # always "show_teams"
            "default_org_esids": list, # always [] (org tier dropped)
            "default_team_esids": list, # pre-checked teams for the editor
        }

    visibility is now teams-only — ``show_all`` and ``hide`` are no
    longer writable. Every new dashboard defaults to ``show_teams``; the
    "Show All" option is gone for everyone (``show_all_allowed`` stays in the
    shape for back-compat with the template/JS but is always ``False``).

    * **Workspace admins / owners (and superusers)** default to ``show_teams``
      with **no** team pre-selected — they pick teams explicitly (the Select
      All button ticks their whole workspace at once).
    * **Org-tier / team-tier users** default to ``show_teams`` with their own
      teams pre-checked — their team ESIDs (the most specific tier). Org ESIDs
      are no longer pre-selected (the org checkbox list is removed); the
      pre-selected team ESIDs match the identifiers the ``org-team-options``
      picker renders, so the checkboxes line up.

    Fail-soft: any resolution error degrades to ``show_teams`` with no
    pre-selection (never blocks the /dashboard-builder render).
    """
    if is_workspace_admin:
        # Workspace admins pick teams explicitly (Select All covers the whole
        # workspace). No pre-selection — a blank teams list means "no one yet".
        return {
            "show_all_allowed": False,
            "default_mode": "show_teams",
            "default_org_esids": [],
            "default_team_esids": [],
        }

    default_team_esids: list[str] = []
    try:
        from asset_management.web.services.role_filter_service import (
            RoleFilterService,
            resolve_user_scope,
        )

        username = session.get("username", "") or ""
        user_roles = session.get("roles", []) or []
        scope = resolve_user_scope(username, user_roles, RoleFilterService.load_config())
        # Pre-select the editor's own teams (the most specific tier). Sorted
        # for deterministic render order (frozensets are unordered).
        if scope.team_esids:
            default_team_esids = sorted(scope.team_esids)
    except Exception as e:  # noqa: BLE001 — never block /dashboard-builder render
        logging.warning("visibility-defaults resolution failed: %s", e)

    return {
        "show_all_allowed": False,
        "default_mode": "show_teams",
        "default_org_esids": [],
        "default_team_esids": default_team_esids,
    }


@dashboard_bp.route("/api/menus", methods=["GET"])
def list_workspace_menus():
    """Return the current workspace's menus in display order.

    Read-gated by ``mc_dashboard_builder`` — anyone who can open the
    builder can see the menu list (the Create / Properties pickers
    need it).

    resolve via :func:`_menus_for_current_user`, which prefers
    the primary workspace and falls back to the ``get_allowed_workspaces``
    walk so an org-admin / team-grant user (no workspace-role doc, None
    primary earlier) still receives their workspace's configured
    menu order instead of a 403 that collapses the Builder panel to
    alphabetical order.
    """
    try:
        _require_builder_write()
        menus = _menus_for_current_user()
        return jsonify({"success": True, "menus": menus})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error listing workspace menus",
            log_detail="list_workspace_menus failed",
            exc=e,
        )


@dashboard_bp.route("/api/menus", methods=["POST"])
def add_workspace_menu():
    """Append a new menu at the end of the workspace's list.

    Body: ``{"title": "..."}``. Title is trimmed, unique within the
    workspace (case-insensitive). Returns the new menu row.
    """
    try:
        _require_builder_write()
        username, workspace_esid = _menu_write_workspace_or_abort()
        _require_workspace_admin(username, workspace_esid)

        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"success": False, "error": "Title is required."}), 400
        # follow-up — mirror the dialog's maxlength server-side.
        if len(title) > _MAX_MENU_TITLE:
            return (
                jsonify({"success": False, "error": f"Menu title must be {_MAX_MENU_TITLE} characters or fewer."}),
                400,
            )

        from asset_management.web.services.workspace_menu_service import (
            DuplicateMenuTitleError,
            WorkspaceMenuError,
            WorkspaceMenuService,
        )

        try:
            row = WorkspaceMenuService.add_menu(workspace_esid, title)
        except DuplicateMenuTitleError as e:
            return jsonify({"success": False, "error": e.public_message}), 409
        except WorkspaceMenuError as e:
            return jsonify({"success": False, "error": e.public_message}), 503

        return jsonify({"success": True, "menu": row})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error adding workspace menu",
            log_detail="add_workspace_menu failed",
            exc=e,
        )


@dashboard_bp.route("/api/menus/<menu_esid>", methods=["PATCH"])
def rename_workspace_menu(menu_esid):
    """Rename a menu and cascade the title change to every dashboard
    in the workspace that referenced the old title.

    Body: ``{"title": "..."}``.
    """
    try:
        _require_builder_write()
        username, workspace_esid = _menu_write_workspace_or_abort()
        _require_workspace_admin(username, workspace_esid)

        data = request.get_json(silent=True) or {}
        new_title = (data.get("title") or "").strip()
        if not new_title:
            return jsonify({"success": False, "error": "Title is required."}), 400
        # follow-up — mirror the dialog's maxlength server-side.
        if len(new_title) > _MAX_MENU_TITLE:
            return (
                jsonify({"success": False, "error": f"Menu title must be {_MAX_MENU_TITLE} characters or fewer."}),
                400,
            )

        from asset_management.web.services.workspace_menu_service import (
            DuplicateMenuTitleError,
            MenuNotFoundError,
            WorkspaceMenuError,
            WorkspaceMenuService,
        )

        try:
            row = WorkspaceMenuService.rename_menu(workspace_esid, menu_esid, new_title)
        except MenuNotFoundError:
            return jsonify({"success": False, "error": "Menu not found."}), 404
        except DuplicateMenuTitleError as e:
            return jsonify({"success": False, "error": e.public_message}), 409
        except WorkspaceMenuError as e:
            return jsonify({"success": False, "error": e.public_message}), 503

        return jsonify({"success": True, "menu": row})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error renaming workspace menu",
            log_detail=f"rename_workspace_menu failed for menu_esid '{menu_esid}'",
            exc=e,
        )


@dashboard_bp.route("/api/menus/<menu_esid>", methods=["DELETE"])
def delete_workspace_menu(menu_esid):
    """Delete a menu.

    Refuses with 409 (``dashboards_in_menu`` in the body) if any
    dashboard still references the menu's title. The workspace admin
    must move or delete those dashboards first — no silent reassignment.
    """
    try:
        _require_builder_write()
        username, workspace_esid = _menu_write_workspace_or_abort()
        _require_workspace_admin(username, workspace_esid)

        from asset_management.web.services.workspace_menu_service import (
            MenuNotEmptyError,
            MenuNotFoundError,
            WorkspaceMenuError,
            WorkspaceMenuService,
        )

        try:
            WorkspaceMenuService.delete_menu(workspace_esid, menu_esid)
        except MenuNotFoundError:
            return jsonify({"success": False, "error": "Menu not found."}), 404
        except MenuNotEmptyError as e:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": e.public_message,
                        "dashboards_in_menu": e.dashboards_in_menu,
                    }
                ),
                409,
            )
        except WorkspaceMenuError as e:
            return jsonify({"success": False, "error": e.public_message}), 503

        return jsonify({"success": True})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error deleting workspace menu",
            log_detail=f"delete_workspace_menu failed for menu_esid '{menu_esid}'",
            exc=e,
        )


@dashboard_bp.route("/api/menus/reorder", methods=["POST"])
def reorder_workspace_menus():
    """Persist a new menu order.

    Body (preferred): ``{"ordered_titles": ["Overview", "Meta",...]}``
    — the on-screen menu order by TITLE. A workspace whose menus exist only
    implicitly (derived from dashboards' ``menu`` strings, never created via
    "+ Add Menu") has no ``de_asset_workspace_menu`` rows and no menu_esids, so
    the esid-keyed path below collected an empty list and silently no-op'd —
    the drag reverted on reload. The title path materializes a backing row for
    any title that lacks one, then writes positions 0..len-1.

    Body (legacy / back-compat): ``{"ordered_esids": ["esid1", ...]}``. Positions
    rewritten 0..len-1; unknown esids 400 the request.
    """
    try:
        _require_builder_write()
        username, workspace_esid = _menu_write_workspace_or_abort()
        _require_workspace_admin(username, workspace_esid)

        data = request.get_json(silent=True) or {}
        ordered_titles = data.get("ordered_titles")
        ordered_esids = data.get("ordered_esids")

        from asset_management.web.services.workspace_menu_service import (
            MenuNotFoundError,
            WorkspaceMenuError,
            WorkspaceMenuService,
        )

        try:
            if ordered_titles is not None:
                if not isinstance(ordered_titles, list):
                    return jsonify({"success": False, "error": "ordered_titles must be a list."}), 400
                WorkspaceMenuService.reorder_menus_by_title(workspace_esid, ordered_titles)
            else:
                ordered = ordered_esids or []
                if not isinstance(ordered, list):
                    return jsonify({"success": False, "error": "ordered_esids must be a list."}), 400
                WorkspaceMenuService.reorder_menus(workspace_esid, ordered)
        except MenuNotFoundError as e:
            return jsonify({"success": False, "error": f"Unknown menu esid(s): {e.public_message}"}), 400
        except WorkspaceMenuError as e:
            return jsonify({"success": False, "error": e.public_message}), 503

        return jsonify({"success": True})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error reordering workspace menus",
            log_detail="reorder_workspace_menus failed",
            exc=e,
        )


@dashboard_bp.route("/api/dashboards/reorder", methods=["POST"])
def reorder_menu_dashboards():
    """persist a new order for the dashboards under one menu.

    Body: ``{"menu": "<menu title>", "ordered_dashboard_ids": ["id1", ...]}``
    (plus the usual ``workspace_esid`` topbar stamp). Positions are
    rewritten 0..n-1 in the supplied order. Any id that does not belong to
    the resolved ``(workspace, menu)`` group 400s the request and nothing is
    written.

    Authz mirrors the menu-reorder endpoint exactly: builder-write +
    workspace-admin (superuser bypass) on the topbar-resolved workspace, so a
    non-admin never gets a 200 here and a crafted payload cannot target a
    foreign workspace.
    """
    try:
        _require_builder_write()
        username, workspace_esid = _menu_write_workspace_or_abort()
        _require_workspace_admin(username, workspace_esid)

        data = request.get_json(silent=True) or {}
        menu_title = (data.get("menu") or "").strip()
        ordered = data.get("ordered_dashboard_ids") or []
        if not menu_title:
            return jsonify({"success": False, "error": "menu is required."}), 400
        if not isinstance(ordered, list):
            return (
                jsonify({"success": False, "error": "ordered_dashboard_ids must be a list."}),
                400,
            )

        from asset_management.web.services.workspace_menu_service import (
            MenuNotFoundError,
            WorkspaceMenuError,
            WorkspaceMenuService,
        )

        try:
            WorkspaceMenuService.reorder_dashboards(workspace_esid, menu_title, ordered)
        except MenuNotFoundError as e:
            return jsonify({"success": False, "error": f"Unknown dashboard id(s): {e.public_message}"}), 400
        except WorkspaceMenuError as e:
            return jsonify({"success": False, "error": e.public_message}), 503

        return jsonify({"success": True})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error reordering dashboards",
            log_detail="reorder_menu_dashboards failed",
            exc=e,
        )


# ─── Save-as-template (freeze) ─────────────────────────────────────


def _freeze_template_authz_or_abort() -> str:
    """#1 — gate Save-as-template behind superuser only.

    earlier this also accepted workspace
    admin/owner. Tightened because template docs are global — a
    workspace_admin freezing a template silently affects every
    workspace's Choose Template dialog, which is a curation surface
    that belongs to platform operators only.

    Returns the resolved ``username`` so the caller can stamp audit
    fields without re-reading the session.
    """
    username = session.get("username", "") or ""
    if not username:
        _abort_json(403, "Not authenticated.")

    if "superuser" not in (session.get("roles") or []):
        _abort_json(403, "Superuser permission is required.")

    return username


@dashboard_bp.route("/api/templates/freeze", methods=["POST"])
def freeze_dashboard_as_template():
    """slice 2 — promote a live dashboard into the
    ``de_dashboard_templates`` registry.

    CSRF is enforced globally by the app's ``before_request`` hook
    (``check_csrf_for_state_change`` — accepts ``X-CSRF-Token`` header
    or ``csrf_token`` form field). No per-route check needed.

    Authz: workspace admin / workspace owner / superuser.

    Body (JSON):

        {
          "dashboard_id":        "<existing dashboard id>",
          "template_id":         "<stable slug, unique in the registry>",
          "pack_id":             "meta" | "echo" | ...,
          "connector_id":        "meta_ads" | ...,
          "menu_title":          "Meta",
          "name":                "Display name",
          "description":         "...",
          "placeholder_fields":  ["ad_account_id", ...],
          "menu_position":       0,
          "required_sources":    ["meta_ads"]   // optional, defaults to [connector_id]
        }

    Returns ``{ok: true, template_id, name}`` on success.
    """
    try:
        _freeze_template_authz_or_abort()
        _require_builder_write()

        data = request.get_json(silent=True) or {}
        dashboard_id = (data.get("dashboard_id") or "").strip()
        template_id = (data.get("template_id") or "").strip()
        pack_id = (data.get("pack_id") or "").strip()
        connector_id = (data.get("connector_id") or "").strip()
        menu_title = (data.get("menu_title") or "").strip()
        name = (data.get("name") or "").strip()

        missing = [
            key
            for key, val in (
                ("dashboard_id", dashboard_id),
                ("template_id", template_id),
                ("pack_id", pack_id),
                ("connector_id", connector_id),
                ("menu_title", menu_title),
                ("name", name),
            )
            if not val
        ]
        if missing:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"Missing required field(s): {', '.join(missing)}",
                    }
                ),
                400,
            )

        # follow-up — length + slug caps mirroring the modal's
        # maxlength/pattern attributes (a crafted POST bypasses the client).
        meta_err = _validate_template_metadata(
            template_id=template_id,
            pack_id=pack_id,
            name=name,
            description=(data.get("description") or "").strip(),
            menu_title=menu_title,
        )
        if meta_err:
            return jsonify({"ok": False, "error": meta_err}), 400

        placeholder_fields = data.get("placeholder_fields") or []
        if not isinstance(placeholder_fields, list):
            return jsonify({"ok": False, "error": "placeholder_fields must be a list."}), 400
        placeholder_fields = [str(f).strip() for f in placeholder_fields if str(f).strip()]
        # slice 2 follow-up (2026-05-22 re-apply): a template can
        # have at most one asset-bound placeholder field. The wizard's
        # pack installer threads a single ``source_account_ids`` list
        # through to every sentinelized slot — if a template carries
        # sentinels on two different fields (e.g. both ad_account_id
        # AND campaign_name), the consumer's ad-account ids would also
        # land in campaign_name and the cloned dashboard would render
        # empty. The modal's checkbox-mutex UI prevents this at the
        # client; this is the server-side belt.
        if len(placeholder_fields) > 1:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            "Only one placeholder field is allowed per template. "
                            "The wizard's installer threads a single asset-id list "
                            "through to every sentinelized slot; multiple fields "
                            "would receive the wrong kind of value. "
                            f"Got {len(placeholder_fields)}: {placeholder_fields!r}."
                        ),
                    }
                ),
                400,
            )

        required_sources = data.get("required_sources")
        if required_sources is not None:
            if not isinstance(required_sources, list):
                return jsonify({"ok": False, "error": "required_sources must be a list."}), 400
            required_sources = [str(s).strip() for s in required_sources if str(s).strip()]

        menu_position = data.get("menu_position", 0)
        try:
            menu_position = int(menu_position)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "menu_position must be an integer."}), 400

        description = (data.get("description") or "").strip()

        # Source-dashboard read-authz: a workspace admin must still be
        # able to READ the source dashboard. visibility +
        # workspace boundary apply here. Reuse the existing
        # ``_load_dashboard_for_read`` so the same 404-vs-403 semantics
        # apply (deliberately don't leak existence to a user with no
        # access).
        _load_dashboard_for_read(dashboard_id)

        from asset_management.web.services.template_service import (
            TemplateFreezeError,
            TemplateService,
        )

        try:
            new_template_id = TemplateService.freeze_dashboard_as_template(
                dashboard_id=dashboard_id,
                template_id=template_id,
                pack_id=pack_id,
                connector_id=connector_id,
                menu_title=menu_title,
                placeholder_fields=placeholder_fields,
                name=name,
                description=description,
                menu_position=menu_position,
                required_sources=required_sources,
            )
        except TemplateFreezeError as e:
            return jsonify({"ok": False, "error": e.public_message}), 400

        return jsonify({"ok": True, "template_id": new_template_id, "name": name})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error freezing dashboard as template",
            log_detail="freeze_dashboard_as_template failed",
            exc=e,
        )


# ─── #3 — Single-template GET + DELETE (superuser-only) ─────────────


@dashboard_bp.route("/api/templates/<template_id>", methods=["GET"])
def get_template_by_id(template_id):
    """#3 — return a single template doc including its
    ``echarts_config`` so the builder can hydrate "Edit template" mode.

    Authz: superuser only (same gate as freeze + delete — templates
    are global, so loading their internals into the builder for
    rewrite is a platform-curation action).

    Returns ``{ok: true, template: {...}}`` or 404 if missing.
    """
    try:
        _freeze_template_authz_or_abort()
        _require_builder_write()

        from asset_management.web.services.template_service import (
            TemplateService,
        )

        template = TemplateService.get_template((template_id or "").strip())
        if template is None:
            return jsonify({"ok": False, "error": "Template not found."}), 404
        return jsonify({"ok": True, "template": template})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error loading template",
            log_detail=f"get_template_by_id failed for {template_id!r}",
            exc=e,
        )


@dashboard_bp.route("/api/templates/<template_id>", methods=["PUT"])
def update_template_by_id(template_id):
    """#3 — overwrite a template's ``echarts_config`` + metadata
    in place. Used by the Edit Template flow: the Builder hydrates the
    template into the canvas directly (without writing a dashboard
    doc), the curator edits, then this route writes back to the same
    ``template_id`` so the registry entry is updated (not duplicated).

    Authz: superuser only (shared gate). Requires the template to
    already exist — use POST ``/api/templates/freeze`` to create new
    templates from live dashboards.

    Body (JSON): same shape as the freeze route but with
    ``echarts_config`` instead of ``dashboard_id`` — the canvas state
    is supplied directly because superusers don't have a primary team
    membership so they can't install-then-freeze.
    """
    try:
        _freeze_template_authz_or_abort()
        _require_builder_write()

        template_id = (template_id or "").strip()
        if not template_id:
            return jsonify({"ok": False, "error": "template_id is required."}), 400

        data = request.get_json(silent=True) or {}
        echarts_config = data.get("echarts_config")
        pack_id = (data.get("pack_id") or "").strip()
        connector_id = (data.get("connector_id") or "").strip()
        menu_title = (data.get("menu_title") or "").strip()
        name = (data.get("name") or "").strip()

        if echarts_config is None or not isinstance(echarts_config, dict):
            return (
                jsonify({"ok": False, "error": "echarts_config (object) is required."}),
                400,
            )

        # follow-up — this route accepts a RAW canvas config (unlike
        # freeze, which copies from an already-capped dashboard doc), so walk
        # its strings with the same cap the dashboard save enforces, and apply
        # the shared metadata ceilings.
        walk_err = _walk_strings_for_cap(echarts_config, _MAX_LABEL_LEN, prefix="echarts_config")
        if walk_err:
            return jsonify({"ok": False, "error": walk_err}), 400
        tpl_charts = echarts_config.get("charts")
        tpl_charts = list(tpl_charts.values()) if isinstance(tpl_charts, dict) else (tpl_charts or [])
        for i, chart in enumerate(tpl_charts if isinstance(tpl_charts, list) else []):
            if isinstance(chart, dict):
                ident_err = _validate_chart_identifiers(chart, prefix=f"echarts_config.charts[{i}]")
                if ident_err:
                    return jsonify({"ok": False, "error": ident_err}), 400
        meta_err = _validate_template_metadata(
            template_id=template_id,
            pack_id=pack_id,
            name=name,
            description=(data.get("description") or "").strip(),
            menu_title=menu_title,
        )
        if meta_err:
            return jsonify({"ok": False, "error": meta_err}), 400

        missing = [
            key
            for key, val in (
                ("pack_id", pack_id),
                ("connector_id", connector_id),
                ("menu_title", menu_title),
                ("name", name),
            )
            if not val
        ]
        if missing:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"Missing required field(s): {', '.join(missing)}",
                    }
                ),
                400,
            )

        placeholder_fields = data.get("placeholder_fields") or []
        if not isinstance(placeholder_fields, list):
            return jsonify({"ok": False, "error": "placeholder_fields must be a list."}), 400
        placeholder_fields = [str(f).strip() for f in placeholder_fields if str(f).strip()]
        if len(placeholder_fields) > 1:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            "Only one placeholder field is allowed per template. "
                            f"Got {len(placeholder_fields)}: {placeholder_fields!r}."
                        ),
                    }
                ),
                400,
            )

        required_sources = data.get("required_sources")
        if required_sources is not None:
            if not isinstance(required_sources, list):
                return jsonify({"ok": False, "error": "required_sources must be a list."}), 400
            required_sources = [str(s).strip() for s in required_sources if str(s).strip()]

        menu_position = data.get("menu_position", 0)
        try:
            menu_position = int(menu_position)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "menu_position must be an integer."}), 400

        description = (data.get("description") or "").strip()

        from asset_management.web.services.template_service import (
            TemplateFreezeError,
            TemplateService,
        )

        existing = TemplateService.get_template(template_id)
        if existing is None:
            return jsonify({"ok": False, "error": "Template not found."}), 404

        try:
            updated_id = TemplateService.update_template(
                template_id=template_id,
                echarts_config=echarts_config,
                pack_id=pack_id,
                connector_id=connector_id,
                menu_title=menu_title,
                placeholder_fields=placeholder_fields,
                name=name,
                description=description,
                menu_position=menu_position,
                required_sources=required_sources,
            )
        except TemplateFreezeError as e:
            return jsonify({"ok": False, "error": e.public_message}), 400

        return jsonify({"ok": True, "template_id": updated_id, "name": name})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error updating template",
            log_detail=f"update_template_by_id failed for {template_id!r}",
            exc=e,
        )


@dashboard_bp.route("/api/templates/<template_id>", methods=["DELETE"])
def delete_template_by_id(template_id):
    """#3 — remove a template from ``de_dashboard_templates``.

    Authz: superuser only. Dashboards previously instantiated from
    this template stay alive in ``de_asset_echarts_dashboards`` — only
    the registry entry is removed, so the user-facing sidebar isn't
    affected for existing clones. The wizard's pack auto-install path
    silently skips the missing template on the next run (per
    ``materialize_pack_for_user`` fail-soft loop).
    """
    try:
        _freeze_template_authz_or_abort()
        _require_builder_write()

        template_id = (template_id or "").strip()
        if not template_id:
            return jsonify({"ok": False, "error": "template_id is required."}), 400

        from asset_management.web.services.template_service import (
            TEMPLATES_INDEX,
            TemplateService,
        )
        from lib.es_helpers import get_elasticsearch_client

        # 404 vs 200-with-noop: existence check first so the client can
        # surface "already gone" distinct from "deleted now". Reuses
        # the same lookup the picker + freeze paths consult.
        existing = TemplateService.get_template(template_id)
        if existing is None:
            return jsonify({"ok": False, "error": "Template not found."}), 404

        es = get_elasticsearch_client()
        if es is None:
            return jsonify({"ok": False, "error": "Elasticsearch unavailable."}), 503

        try:
            es.delete(
                index=TEMPLATES_INDEX,
                id=template_id,
                refresh="wait_for",
            )
        except Exception as e:  # noqa: BLE001 — boundary
            from elasticsearch import NotFoundError

            if isinstance(e, NotFoundError):
                # Race: another superuser deleted between the
                # existence check and the delete call. Surface as
                # the same 404 the existence check would have.
                return jsonify({"ok": False, "error": "Template not found."}), 404
            raise

        return jsonify({"ok": True, "template_id": template_id})
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error deleting template",
            log_detail=f"delete_template_by_id failed for {template_id!r}",
            exc=e,
        )


# ─── Workspace selector (superuser / multi-workspace) ──────────────


def _selectable_workspaces_for_user() -> list[dict]:
    """the workspaces the current user may act in, as
    ``[{"esid", "title"}]`` sorted by title.

    Source = the user's resolvable workspaces (``get_allowed_workspaces()``):
      * a concrete list  → those workspaces
      * ``None`` (bare privileged / superuser) → ALL workspaces
      * ``[]``           → none

    The Builder topbar renders a workspace selector when this returns >1 entry
    (a superuser or multi-workspace user); with exactly 1 it renders a
    read-only label; with 0 the selector is hidden. The selected workspace
    drives the org/team dropdowns and the new-dashboard create-stamp.

    Fail-soft: returns ``[]`` on any error.
    """
    try:
        ws_hits = get_all_documents("de_asset_workspace")
    except Exception:  # noqa: BLE001 — never block the Builder render
        return []

    titles = {}
    for hit in ws_hits:
        esid = hit.get("_id", "")
        if esid:
            titles[esid] = (hit.get("_source", {}) or {}).get("workspace_title", "") or esid

    try:
        allowed = RoleFilterService.get_allowed_workspaces()
    except Exception:  # noqa: BLE001
        allowed = []

    if allowed is None:
        # Bare privileged / superuser — every workspace is selectable.
        esids = list(titles.keys())
    else:
        esids = [w for w in allowed if w in titles]

    out = [{"esid": e, "title": titles[e]} for e in esids]
    out.sort(key=lambda w: w["title"].lower())
    return out


def _default_workspace_esid(selectable: list[dict]) -> str:
    """the workspace the Builder should select on load.

    Preference order (first that is in the selectable set wins):
      1. A genuinely-PRIMARY workspace role (``de_authz_user_workspace_role``
         with ``is_primary == True``). NOTE: ``get_primary_workspace`` returns
         ``workspaces[0]`` even when nothing is flagged primary — that
         arbitrary first is exactly the wrong-default bug, so we check the
         ``is_primary`` flag ourselves and ignore the positional fallback.
      2. A workspace the user holds a DATA GRANT into
         (``de_authz_data_grant`` ``scope_type == "workspace"``) — for a
         superuser whose working context is a grant (e.g. an operator granted into
         kavanah-media), this is their intended workspace.
      3. The first selectable (already title-sorted).

    Empty string when the user has no selectable workspace.
    """
    if not selectable:
        return ""
    selectable_esids = {w["esid"] for w in selectable}
    username = session.get("username", "") or ""

    def _search_ids(index, query, field):
        try:
            es = get_elasticsearch_client()
            if es is None:
                return []
            resp = es.search(index=index, body={"query": query, "_source": [field], "size": 50})
            out = []
            for h in resp.get("hits", {}).get("hits", []):
                v = (h.get("_source") or {}).get(field)
                if v:
                    out.append(v)
            return out
        except Exception:  # noqa: BLE001 — degrade to the positional default
            return []

    # 1. A genuinely-primary workspace role.
    if username:
        primary_ids = _search_ids(
            "de_authz_user_workspace_role",
            {"bool": {"filter": [{"term": {"username": username}}, {"term": {"is_primary": True}}]}},
            "workspace_esid",
        )
        for ws in primary_ids:
            if ws in selectable_esids:
                return ws

        # 2. A workspace-scope data grant. The grant doc stores the workspace
        # in ``scope_esid`` (with ``scope_type == "workspace"``), NOT in a
        # ``workspace_esid`` field — reading the wrong field is why this
        # defaulted past an operator's kavanah-media grant to the first
        # alphabetical workspace. ``scope_type`` and ``scope_esid`` are both
        # plain ``keyword`` (verified mapping), so query them bare — NOT with a
        # ``.keyword`` sub-field, which doesn't exist here and matches nothing.
        grant_ids = _search_ids(
            "de_authz_data_grant",
            {"bool": {"filter": [{"term": {"username": username}}, {"term": {"scope_type": "workspace"}}]}},
            "scope_esid",
        )
        for ws in grant_ids:
            if ws in selectable_esids:
                return ws

    # 3. First selectable (title-sorted).
    return selectable[0]["esid"]


@dashboard_bp.route("/api/builder/workspaces", methods=["GET"])
def list_builder_workspaces():
    """workspaces the current Builder user may act in + the default
    selection. Backs the topbar workspace selector."""
    try:
        _require_builder_write()
        workspaces = _selectable_workspaces_for_user()
        return jsonify(
            {
                "success": True,
                "workspaces": workspaces,
                "default_esid": _default_workspace_esid(workspaces),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error listing builder workspaces",
            log_detail="list_builder_workspaces failed",
            exc=e,
        )


# ─── Templates picker (list + install) ─────────────────────────────


def _pickable_teams_for_user() -> list[dict]:
    """the teams the current user may install a template into.

    Returns ``[{"esid", "title"}]`` narrowed to the caller's workspace scope
    via the same tenant-boundary filter the properties-panel team picker uses
    (``get_allowed_workspaces`` + ``filter_hits_by_workspace``). For a
    privileged user this is every team in their workspace(s); for a privileged
    user with no resolvable workspace scope (``get_allowed_workspaces() is
    None``) it's all teams. Used only on the team-less-privileged install path
    so a superuser can choose which team's connected accounts to bind to.

    Fail-soft: returns ``[]`` on any error (the caller treats an empty list as
    "no team to offer" and the UI shows an appropriate message).
    """
    try:
        team_hits = get_all_documents("de_asset_teams")
    except Exception:  # noqa: BLE001 — never raise out of the install path
        return []

    teams = []
    for hit in team_hits:
        source = hit.get("_source", {})
        org_field = source.get("organization_esid")
        if isinstance(org_field, list):
            org_esids = [o for o in org_field if o]
        elif org_field:
            org_esids = [org_field]
        else:
            org_esids = []
        teams.append(
            {
                "esid": hit.get("_id", ""),
                "title": source.get("team_title", ""),
                "workspace_esid": source.get("workspace_esid", ""),
                "organization_esids": org_esids,
            }
        )

    # Tenant-boundary filter, mirroring get_org_team_options:
    #   None → no filter (privileged with no narrowing) → all teams
    #   []   → deny-all
    #   list → keep teams whose workspace_esid is in the set (fail-closed
    #          on a team missing the field).
    try:
        allowed_workspaces = RoleFilterService.get_allowed_workspaces()
    except Exception:  # noqa: BLE001
        allowed_workspaces = []
    if allowed_workspaces is not None:
        allowed_ws_set = set(allowed_workspaces)
        teams = [t for t in teams if t.get("workspace_esid") and t["workspace_esid"] in allowed_ws_set]

    # narrow to the user's ORG(s) when they have an org scope.
    # The workspace filter alone shows an org owner EVERY team in their
    # workspace (e.g. all of kavanah-media), not just their org's teams.
    # An org-tier user resolves a non-empty org_esids; intersect the team's
    # organization_esid with it so they only pick from their own org's teams.
    # A user with no org scope (a bare superuser / asset_admin who isn't
    # org-scoped) resolves empty org_esids → no narrowing → all-workspace
    # behavior is preserved.
    try:
        from asset_management.web.services.role_filter_service import (
            resolve_user_scope,
        )

        user_org_esids = set(
            resolve_user_scope(
                session.get("username", "") or "",
                session.get("roles", []) or [],
                RoleFilterService.load_config(),
            ).org_esids
        )
    except Exception:  # noqa: BLE001 — fail open to the workspace-scoped list
        user_org_esids = set()
    if user_org_esids:
        teams = [t for t in teams if user_org_esids.intersection(t.get("organization_esids") or [])]

    teams.sort(key=lambda t: t["title"])
    return [{"esid": t["esid"], "title": t["title"]} for t in teams]


def _resolve_primary_team_for_user(username: str) -> str | None:
    """Look up the user's primary team_esid from authz team memberships.

    Mirror of ``analytics.routes._resolve_primary_team_esid`` — duplicated
    here to avoid a cross-blueprint import. The shape is small (one ES
    search), so the duplication cost is lower than the coupling cost of
    importing across blueprints.
    """
    try:
        from asset_management.web.services.permissions_index_service import (
            TEAM_MEMBERSHIP_INDEX,
        )
        from lib.es_helpers import get_elasticsearch_client
    except Exception:
        return None

    es = get_elasticsearch_client()
    if es is None:
        return None
    try:
        resp = es.search(
            index=TEAM_MEMBERSHIP_INDEX,
            body={
                "size": 1,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"username": username}},
                            {"term": {"is_primary": True}},
                        ]
                    }
                },
            },
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception:
        return None

    hits = (resp.get("hits") or {}).get("hits") or []
    if not hits:
        return None
    return hits[0].get("_source", {}).get("team_esid")


def _dedupe_dashboard_title(es, base_title: str, workspace_esid: str | None) -> str:
    """#3 smoke followup (2026-05-25 #3): pick a non-colliding
    dashboard title in the workspace.

    Returns ``base_title`` unchanged when no dashboard in the same
    workspace owns it. Otherwise returns the lowest available
    ``f"{base_title} [N]"`` suffix (N starting at 2). Used by the
    install route so two templates whose default name matches an
    existing dashboard don't both land with the same title in the
    sidebar.

    Workspace-scoped lookup matches the visibility boundary the user
    sees on /dashboards — a collision against a dashboard in another
    workspace is invisible to them and shouldn't trigger a suffix.

    Fails open: any ES error returns ``base_title`` (the
    pre-followup behavior). A title collision is a UX paper-cut, not
    a correctness issue worth blocking install on.

     #3 smoke followup retry (2026-05-25 #4): the original
    implementation used ``prefix`` against ``title.keyword`` and
    filtered ``workspace_esid`` via ``term``. Two mapping-drift
    landmines: (a) older clusters lack ``title.keyword`` because the
    index was dynamic-mapped before ES added implicit ``.keyword``
    subfields, and (b) ``workspace_esid`` may be ``text`` (not
    ``keyword``) so ``term`` returns no hits. Switched to a
    bare-field query + Python-side filtering so the dedupe works
    regardless of the index's mapping shape. The pre-fix query
    returned zero candidates on staging — the user's smoke caught
    two ``Meta Spend`` entries side-by-side in the sidebar.
    """
    if not es or not base_title:
        return base_title or ""
    try:
        # Read ALL dashboards in the index (size=10000 ceiling — typical
        # workspace has dozens, cluster-wide has hundreds), filter to
        # the user's workspace in Python. The mapping-agnostic shape
        # trades a slightly bigger response for resilience against
        # index-mapping drift.
        body: dict = {
            "size": 10000,
            "_source": ["title", "workspace_esid"],
            "query": {"match_all": {}},
        }
        resp = es.search(
            index=DASHBOARDS_INDEX,
            body=body,
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:  # noqa: BLE001 — boundary; fail open
        logging.warning("_dedupe_dashboard_title ES lookup failed: %s", e)
        return base_title
    hits = (resp.get("hits") or {}).get("hits") or []
    existing_titles: set[str] = set()
    for hit in hits:
        source = hit.get("_source") or {}
        doc_workspace = source.get("workspace_esid") or ""
        # Filter to the user's workspace. Empty workspace_esid on a
        # doc means "legacy / unscoped" — skip it (matches the
        # pre-followup behavior of not deduping against legacy docs).
        if workspace_esid and doc_workspace != workspace_esid:
            continue
        title = source.get("title")
        if title:
            existing_titles.add(title)
    if base_title not in existing_titles:
        logging.info(
            "_dedupe_dashboard_title: no collision for %r in workspace=%r " "(%d existing titles checked)",
            base_title,
            workspace_esid,
            len(existing_titles),
        )
        return base_title
    # Walk suffixes starting at [2] until we find one nobody owns.
    # In practice N is tiny; cap at 1000 as a defensive ceiling.
    for n in range(2, 1001):
        candidate = f"{base_title} [{n}]"
        if candidate not in existing_titles:
            logging.info(
                "_dedupe_dashboard_title: collision on %r in workspace=%r — " "suffixed to %r",
                base_title,
                workspace_esid,
                candidate,
            )
            return candidate
    # Pathological case (1000 dashboards with the same base name) —
    # fall back to the base title; a UX paper-cut is better than
    # an infinite loop.
    return base_title


def _resolve_legacy_team_asset_ids(connector_id: str, team_esid: str, placeholder_field: str | None) -> list[str]:
    """resolve a team's connected asset ids from the LEGACY
    asset-management representation (the team doc's ``ds_*`` flag +
    ``*_esid*`` field → ``de_asset_*`` index), not ``de_connection``.

    A team whose data source was wired the old way (before the
    de_connection OAuth model) carries its assets directly on the team
    doc — never in ``de_connection`` — so the install gate's
    de_connection-only lookup falsely reports "no connected accounts."
    This reuses the canonical connector map and asset resolver from
    ``connector_status_service`` (the same join the Data Connectors
    page renders) so the two stay in sync.

    ``connector_id`` keys ``CONNECTORS_BY_KEY`` directly — a template's
    ``required_sources`` already store connector_ids ("meta_ads",
    "google_ads", "echo"), so no provider bridge is needed here (that
    bridge is only for the ``de_connection.provider`` query).

    ``placeholder_field`` picks the correct asset KIND when a connector
    has two chains. ``_substitute_assets`` is field-blind — it writes
    ids wholesale wherever the sentinel appears — so for meta_ads a
    template frozen on ``ad_account_id`` MUST get ``fb_ad_account_id``
    values (the fallback chain), not ``fb_page_id`` (the primary).
    Substituting the wrong kind would render an empty dashboard silently
    — worse than the current false 400. We pick the chain whose
    id-field matches ``placeholder_field``; default to the primary chain.

    Fail-soft: any missing config / ES error returns ``[]`` so the
    caller falls back to the de_connection path.
    """
    if not connector_id or not team_esid:
        return []
    try:
        from asset_management.web.services.connector_status_service import (
            CONNECTORS_BY_KEY,
            _resolve_linked_assets,
        )
    except Exception:  # noqa: BLE001 — boundary; fall back to de_connection
        return []

    connector = CONNECTORS_BY_KEY.get(connector_id)
    if not connector or not connector.get("team_flag") or not connector.get("asset_index"):
        # Unknown connector, or one with no asset chain (e.g. disciple_tools).
        return []

    try:
        team_source = get_document_by_id("de_asset_teams", team_esid)
    except Exception:  # noqa: BLE001 — boundary
        team_source = None
    if not team_source:
        return []

    # The legacy data-source must actually be enabled on the team.
    flag_value = team_source.get(connector["team_flag"])
    if str(flag_value).lower() not in ("true", "1", "yes"):
        return []

    # Echo (and any connector whose template substitutes the
    # team_esid itself) keys its data on team_esid, not a per-account id.
    # Its template carries ``placeholder_field == "team_esid"`` and the
    # CONNECTORS config has no asset_id_field, so the asset-doc resolution
    # below would yield nothing and falsely report "no connected accounts"
    # even though the connector is enabled. The substituted value IS the
    # selected team's esid. Resolve it directly when the connector is on.
    if placeholder_field == "team_esid":
        return [team_esid]

    # Chain selection: prefer the chain whose id-field matches the
    # template's placeholder_field. meta_ads' fallback (ad_account)
    # id-field ``fb_ad_account_id`` ends with the ``ad_account_id``
    # placeholder; its primary (page) id-field ``fb_page_id`` does not.
    primary = (
        connector.get("team_esid_field"),
        connector.get("asset_index"),
        connector.get("asset_title_field"),
        connector.get("asset_id_field"),
    )
    fallback = (
        connector.get("fallback_team_esid_field"),
        connector.get("fallback_asset_index"),
        connector.get("fallback_asset_title_field"),
        connector.get("fallback_asset_id_field"),
    )

    def _matches(id_field: str | None) -> bool:
        return bool(placeholder_field and id_field and id_field.endswith(placeholder_field))

    if fallback[0] and fallback[1] and _matches(fallback[3]):
        chain = fallback
    else:
        chain = primary

    team_esid_field, asset_index, title_field, id_field = chain
    if not team_esid_field or not asset_index:
        return []

    assets = _resolve_linked_assets(
        team_source.get(team_esid_field),
        asset_index,
        title_field or "",
        id_field=id_field,
    )

    # Phase 1 — same-index fallback. Google Ads moved its team link
    # from the scalar google_ads_customer_esid to the list
    # team_google_ads_customer_esids, declaring the scalar as
    # fallback_team_esid_field over the SAME asset_index. A pre-migration team
    # carries only the scalar, so when the primary (list) field resolves nothing
    # AND the fallback team-field points at the same index, resolve from it too —
    # mirrors connector_status_service's empty-primary fallback. (The cross-index
    # fallback above, used by meta_ads' ad_account chain, is unaffected: it's
    # selected up front by the placeholder match, not here.)
    fb_field = connector.get("fallback_team_esid_field")
    if not assets and fb_field and fb_field != team_esid_field and not connector.get("fallback_asset_index"):
        assets = _resolve_linked_assets(
            team_source.get(fb_field),
            asset_index,
            title_field or "",
            id_field=id_field,
        )
    return [a.get("provider_id") for a in assets if a.get("provider_id")]


def _resolve_provider_asset_ids(
    username: str,
    provider: str,
    *,
    connector_id: str | None = None,
    team_esid: str | None = None,
    placeholder_field: str | None = None,
) -> list[str]:
    """For the install path, gather the connected asset ids under
    ``provider`` (e.g. ``meta_ads`` → list of ad_account_ids).

    resolves from two representations, in priority order:

    1. **Legacy (team-scoped).** When ``team_esid`` + ``connector_id``
       are supplied and the team carries the connector the old way
       (``ds_*`` flag + asset index, no ``de_connection`` row), use that
       team's asset ids. This is the source of truth for the install —
       binding a template to the SELECTED team, not the caller's
       de_connection grants. Using the team-scoped set (rather than
       unioning it with the username's connections) keeps the
       tenant boundary intact: a superuser installing into team B must
       not have their own connection's assets bleed into team B's
       dashboard.
    2. **de_connection (username-scoped) fallback.** Reads
       ``de_connection`` rows for the user, filters to those whose
       ``provider`` matches, and lists every ``de_connection_asset``
       under each. Used when there is no legacy team set (the team is the
       user's own, wired via OAuth, or no team_esid is in play).

    Returns the asset ``provider_asset_id`` values — what
    ``TemplateService.instantiate`` expects for placeholder
    substitution.

    Empty list is the caller's signal to refuse the install with an
    "unconnected" 4xx — the substitution would otherwise produce
    ``filter value=[]`` which matches cluster-wide (the cross-tenant
    leak guard closed; ``TemplateService.instantiate``
    raises ``TemplateInstantiationError`` if it ever sees that
    combination, but better to fail at the route layer with a clearer
    message).
    """

    def _dedupe(asset_ids: list[str]) -> list[str]:
        # Preserve order — two connections against the same ad_account_id
        # would otherwise yield duplicate entries in the substituted filter.
        seen: set[str] = set()
        out: list[str] = []
        for a in asset_ids:
            if a and a not in seen:
                seen.add(a)
                out.append(a)
        return out

    # legacy team-scoped resolution wins when it yields anything.
    legacy_ids = _resolve_legacy_team_asset_ids(connector_id or "", team_esid or "", placeholder_field)
    if legacy_ids:
        return _dedupe(legacy_ids)

    if not username or not provider:
        return []
    from asset_management.web.services.connection_service import (
        ConnectionService,
    )

    connections = ConnectionService.list_connections_for_principal(username)
    matching_connection_esids = [
        hit.get("_id") for hit in connections if (hit.get("_source") or {}).get("provider") == provider and hit.get("_id")
    ]

    asset_ids: list[str] = []
    for connection_esid in matching_connection_esids:
        asset_rows = ConnectionService.list_assets_for_connection(connection_esid)
        for hit in asset_rows:
            source = hit.get("_source") or {}
            asset_id = source.get("provider_asset_id")
            if asset_id:
                asset_ids.append(asset_id)
    return _dedupe(asset_ids)


# map a team's ``ds_*`` data-source flag to the provider string the
# template picker's availability check compares against. The values mirror the
# bridged ``required_sources`` taxonomy: meta_ads -> "facebook" (via
# TemplateService._CONNECTOR_TO_PROVIDER) and GA's connector_id is
# "google_analytics_4" (NOT the connector_status_service key "google_analytics"),
# so this map is keyed on the ds_* flag and emits the template-facing provider
# directly to avoid that drift.
_DS_FLAG_TO_PROVIDER: dict[str, str] = {
    "ds_facebook": "facebook",
    "ds_google_analytics": "google_analytics_4",
    "ds_google_ads": "google_ads",
    "ds_echo": "echo",
    "ds_youtube": "youtube",
    "ds_chatwoot": "chatwoot",
}


def _scope_connected_providers(username: str) -> set[str]:
    """providers the user can build templates against, unioned
    from two sources:

    1. The principal's own ``de_connection`` rows (the original,
       username-keyed signal).
    2. Every team in the user's scope (``_pickable_teams_for_user``):
       each enabled ``ds_*`` flag on the team doc maps to a provider.

    The picker's availability was source (1) only, so an org-tier user
    (org owner with no team membership and no de_connection of their own)
    saw every template locked even though their org's teams carry working
    connectors. Unioning the in-scope teams' enabled connectors fixes the
    org-tier gap; the install gate still enforces the specific team.

    Fail-soft: any error contributes nothing (the de_connection leg alone
    is the prior behavior).
    """
    providers: set[str] = set()
    if not username:
        return providers

    # Leg 1 — the principal's de_connection providers.
    try:
        from asset_management.web.services.connection_service import (
            ConnectionService,
        )

        for hit in ConnectionService.list_connections_for_principal(username) or []:
            provider = (hit.get("_source") or {}).get("provider")
            if provider:
                providers.add(provider)
    except Exception:  # noqa: BLE001 — boundary; fall through to leg 2
        pass

    # Leg 2 — connectors enabled across the teams in the user's scope.
    try:
        for team in _pickable_teams_for_user():
            team_doc = get_document_by_id("de_asset_teams", team.get("esid")) or {}
            for flag, provider in _DS_FLAG_TO_PROVIDER.items():
                if str(team_doc.get(flag)).lower() in ("true", "1", "yes"):
                    providers.add(provider)
    except Exception:  # noqa: BLE001 — boundary; leg 1 still applies
        pass

    return providers


@dashboard_bp.route("/api/templates", methods=["GET"])
def list_templates():
    """Builder Templates picker list.

    Returns every ``de_dashboard_templates`` doc annotated with
    ``is_available`` (True iff every entry in ``required_sources``
    matches a ``provider`` on one of the user's ``de_connection``
    docs). The Builder modal renders unavailable entries greyed out
    with a "Connect X to unlock" CTA.

    Authz: same gate as the rest of the Builder
    (``_require_builder_write`` — ``mc_dashboard_builder`` or an
    admin role). Unauthenticated callers get 403 via the gate; the
    page itself is also gated, so an anonymous direct-hit is
    blocked at the same layer.

    Returns ``{ok: true, templates: [...]}``.
    """
    try:
        _require_builder_write()
        username = session.get("username", "") or ""
        from asset_management.web.services.template_service import (
            TemplateService,
        )

        # resolve connected providers from the user's own
        # de_connection rows UNIONED with the connectors enabled across the
        # teams in their scope. Without the team leg an org-tier user (org
        # owner, no team membership, no personal de_connection) saw every
        # template locked. Used for BOTH the registry annotation
        # (list_templates_for_user) and the hardcoded-entry signal below so
        # the two stay consistent.
        scope_providers = _scope_connected_providers(username)

        templates = TemplateService.list_templates_for_user(username, extra_providers=scope_providers)

        # surface the user's connected OAuth providers in the
        # response so the JS can compute ``is_available`` for the
        # hardcoded Choose Template entries (Facebook Digital Impact
        # Report / Google Digital Impact Report / Combined / Summary).
        # Those rows live in builder.js (not in the registry) because
        # they're coupled to legacy chart_type rendering paths, so the
        # server can't annotate them via list_templates_for_user the
        # same way it does registry entries. Returning the raw provider
        # set lets the JS apply the same _CONNECTOR_TO_PROVIDER bridge
        # used by list_templates_for_user, without duplicating the
        # source-of-truth set on the client. this is the same
        # scope-aware set the registry annotation uses.
        connected_providers: list[str] = sorted(scope_providers)

        # Strip the full echarts_config from the response — the picker
        # only needs metadata, and echarts_config can be tens of KB
        # per template (every chart's full config). Drops payload by
        # ~90% on a 5-template registry.
        def _summary(t):
            return {
                "template_id": t.get("template_id"),
                "name": t.get("name"),
                "description": t.get("description"),
                "pack_id": t.get("pack_id"),
                "connector_id": t.get("connector_id"),
                "menu_title": t.get("menu_title"),
                "required_sources": t.get("required_sources") or [],
                "placeholder_field": t.get("placeholder_field"),
                "is_available": t.get("is_available", True),
            }

        return jsonify(
            {
                "ok": True,
                "templates": [_summary(t) for t in templates],
                "connected_providers": connected_providers,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error listing templates",
            log_detail="list_templates failed",
            exc=e,
        )


@dashboard_bp.route("/api/templates/<template_id>/install", methods=["POST"])
def install_template(template_id):
    """Builder Templates picker install action.

    Clones a ``de_dashboard_templates`` entry into the user's primary
    workspace + team via ``TemplateService.instantiate``. Idempotent
    on ``(team_esid, template_id)`` — a re-click returns the existing
    dashboard's id rather than writing a duplicate.

    Authz: ``_require_builder_write`` (same gate as the rest of the
    Builder write surface). CSRF is enforced globally by the app's
    ``before_request`` hook (accepts ``X-CSRF-Token`` header or
    ``csrf_token`` form field).

    First-cut team resolution: the user's primary team. Multi-team
    users with multiple connections per template's ``required_sources``
    provider get all of that team's matching asset ids substituted
    into the placeholder. A multi-team picker is out of scope here —
    follow-up ticket when the need surfaces.

    Returns ``{ok: true, dashboard_id, title}`` on success.
    Returns 400 with a user-facing error message if the install
    can't proceed (template not found, no primary team, no
    connected asset ids, etc.).
    """
    try:
        _require_builder_write()
        username = session.get("username", "") or ""
        if not username:
            return jsonify({"ok": False, "error": "Not authenticated."}), 403

        # optional ``team_esid`` lets a team-less privileged user pick
        # which team to install the template into. ``silent=True`` so a missing
        # / non-JSON body is a no-op (the normal install path sends no body).
        data = request.get_json(silent=True) or {}

        # the install flow now prompts for a name + menu (the same
        # New-Dashboard modal), so the dashboard lands where the USER chose
        # rather than in the template author's baked-in menu_title (which could
        # be a menu the user's workspace doesn't have → "I can't find it").
        # Both are optional: a body without them falls back to the template's
        # name (deduped) and menu_title, preserving programmatic callers.
        requested_title = (data.get("title") or "").strip() if isinstance(data, dict) else ""
        requested_menu = (data.get("menu") or "").strip() if isinstance(data, dict) else ""

        from asset_management.web.services.template_service import (
            TemplateInstantiationError,
            TemplateNotFoundError,
            TemplateService,
        )

        template = TemplateService.get_template(template_id)
        if template is None:
            return jsonify({"ok": False, "error": f"Template {template_id!r} not found."}), 404

        # Resolve curation workspace + primary team.
        #
        # use ``resolve_curation_workspace`` (resolver:
        # is_primary role → workspace DATA GRANT → positional fallback), NOT the
        # bare positional ``get_primary_workspace`` (``workspaces[0]``). For an
        # org owner whose ``[0]`` workspace differs from the data-grant
        # workspace where their teams' data actually lives, the positional pick
        # landed the installed dashboard in the WRONG workspace (e.g. an empty
        # ``workspace-a`` instead of ``kavanah-media``), so every chart scoped to
        # that workspace returned zero rows despite the data existing. This is
        # the same builder/read-only split fixed for the Builder topbar
        # and read-only sidebar; the template-install path was never migrated.
        try:
            from asset_management.web.services.workspace_membership_service import (
                WorkspaceMembershipService,
            )

            workspace_esid = WorkspaceMembershipService.resolve_curation_workspace(username)
        except Exception:
            workspace_esid = None

        team_esid = _resolve_primary_team_for_user(username)

        # (defect 2): honor an explicitly-picked team even when the
        # caller HAS a primary team. A privileged user (superuser / asset_admin
        # / workspace admin) installs against the team they selected in the UI,
        # not whatever their own primary-team membership happens to be — the
        # primary team may carry no connector for this template, which is the
        # exact false-"no connected accounts" the user reported. The team-less
        # branch below already honors data["team_esid"]; this extends
        # the same honor-the-pick logic to the has-primary-team case. Validated
        # against _pickable_teams_for_user() so a caller can only target a team
        # inside their own workspace scope (no cross-tenant install).
        requested_team = (data.get("team_esid") or "").strip() if isinstance(data, dict) else ""
        if team_esid and requested_team and requested_team != team_esid:
            is_privileged = False
            try:
                is_privileged = RoleFilterService.current_user_is_privileged()
            except Exception:  # noqa: BLE001 — fail closed: keep the primary team
                is_privileged = False
            if is_privileged and requested_team in {t["esid"] for t in _pickable_teams_for_user()}:
                team_esid = requested_team
                # Re-resolve the workspace from the picked team so the install
                # stamps the right tenant (the primary-team workspace would be wrong).
                try:
                    _td = get_document_by_id("de_asset_teams", team_esid)
                    if _td and _td.get("workspace_esid"):
                        workspace_esid = _td.get("workspace_esid")
                except Exception:  # noqa: BLE001 — boundary; keep prior workspace
                    pass

        # a template only NEEDS a team when it carries an asset
        # placeholder (the team supplies the account ids substituted into it).
        # ``placeholder_field`` is set iff the template's echarts_config holds
        # the ASSET_PLACEHOLDER sentinel (verified across the seeded registry).
        # A placeholder-less template is just a plain dashboard: an org /
        # workspace owner with no team membership can install it and then pick
        # which teams see it in the properties panel, exactly like a hand-built
        # dashboard. So the team-mandatory gate below only applies when the
        # template actually needs one — otherwise we fall through with
        # team_esid possibly None and instantiate lands it as show_all.
        needs_team = bool(template.get("placeholder_field"))

        # a placeholder template no longer hard-blocks on a team.
        # When no team is resolved yet, honor an explicit (in-scope) pick;
        # else AUTO-PICK when the user has exactly one team in scope (the common
        # single-team case → the dashboard lands team-scoped and its charts
        # populate once a source connects); else install TEAMLESS — instantiate
        # lands it ``show_all`` and ``_substitute_assets`` writes a match-nothing
        # sentinel into the placeholder so the data-bound charts render blank
        # (never cluster-wide) until the user sets a team + connects a source.
        # No error toast, no dead-end picker. (Was the need_team
        # prompt + onboarding 400.)
        if needs_team and not team_esid:
            requested_team = (data.get("team_esid") or "").strip() if isinstance(data, dict) else ""
            pickable = _pickable_teams_for_user()
            pickable_esids = {t["esid"] for t in pickable}

            chosen = None
            if requested_team and requested_team in pickable_esids:
                chosen = requested_team
            elif len(pickable) == 1:
                # Auto-pick the sole in-scope team — no prompt for the common case.
                chosen = pickable[0]["esid"]
            # else: zero or multiple teams in scope → install teamless (show_all).

            if chosen:
                team_esid = chosen
                if not workspace_esid:
                    try:
                        _td = get_document_by_id("de_asset_teams", team_esid)
                        workspace_esid = (_td or {}).get("workspace_esid") or workspace_esid
                    except Exception:  # noqa: BLE001 — boundary; keep prior workspace
                        pass

        # Resolve the asset ids the placeholder substitution needs.
        # Templates with ``required_sources`` always carry exactly one
        # provider today; if the schema grows to multi-provider templates
        # we'll need a different shape here.
        #
        # #2 follow-up (smoke 2026-05-25): ``required_sources``
        # stores connector_ids (e.g. ``"meta_ads"``) but
        # ``de_connection.provider`` stores OAuth provider names
        # (e.g. ``"facebook"``). The list_templates_for_user gate
        # bridges through ``_CONNECTOR_TO_PROVIDER``; the install path
        # needs the same translation or the asset-id lookup against
        # ``de_connection`` returns zero hits for every Meta template
        # and the user sees "No connected meta_ads accounts" despite
        # having a working facebook connection. Reuse the canonical map
        # so the two sides stay in sync.
        # only resolve/require accounts when the template actually
        # has an asset placeholder. ``required_sources`` is set on plain
        # templates too (e.g. meta_kpi carries ``["meta_ads"]`` but no
        # placeholder), so gating on it alone would 400 a no-placeholder
        # template for a team with no connected accounts. ``needs_team``
        # (placeholder_field present) is the authoritative "this template
        # substitutes asset ids" signal.
        required_sources = template.get("required_sources") or []
        if needs_team and required_sources and team_esid:
            primary_connector = required_sources[0]
            primary_provider = TemplateService._CONNECTOR_TO_PROVIDER.get(primary_connector, primary_connector)
            # resolve against the SELECTED team (legacy connectors
            # live on the team doc, not de_connection) and pass the template's
            # placeholder_field so the right asset KIND is collected.
            source_account_ids = _resolve_provider_asset_ids(
                username,
                primary_provider,
                connector_id=primary_connector,
                team_esid=team_esid,
                placeholder_field=template.get("placeholder_field"),
            )
            # no connected accounts is NO LONGER a hard error. The
            # template installs and ``_substitute_assets`` writes a match-nothing
            # sentinel into the placeholder, so its data-bound charts render
            # blank until the team connects a source (then re-install / re-scope
            # repopulates them). (Was a 400 "Connect a data source first".)
        else:
            # No placeholder, no required_sources, or teamless install — no
            # asset substitution targets resolved. instantiate() substitutes
            # the match-nothing sentinel (placeholder templates) or no-ops
            # (plain templates) cleanly on an empty list.
            source_account_ids = []

        # Resolve org_esid via the team doc so the cloned dashboard's
        # allowed_org_esids stamp matches the team's org (preserves the
        # boundary). Fail-soft — if the team has no org_esid
        # we still install; the cloned dashboard renders without an
        # org-scope visibility entry, which matches show_teams +
        # allowed_team_esids only.
        org_esid: str | None = None
        try:
            team_doc = get_document_by_id("de_asset_teams", team_esid)
            if team_doc:
                org_field = team_doc.get("organization_esid")
                if isinstance(org_field, list):
                    org_esid = org_field[0] if org_field else None
                else:
                    org_esid = org_field
        except Exception:
            org_esid = None

        # #3 smoke followup (2026-05-25 #3): dedupe the title
        # against existing dashboards in the same workspace. Only
        # applies on the FIRST install of this template (idempotent
        # re-hits return the existing dashboard via
        # _find_existing_instantiation inside instantiate). Look up
        # collisions BEFORE the instantiate call so we can hand the
        # final title in via title_override.
        from lib.es_helpers import get_elasticsearch_client

        es_client = get_elasticsearch_client()
        # dedupe the USER-chosen title when supplied, else the
        # template's default name.
        base_title = requested_title or template.get("name") or "Untitled"
        deduped_title = _dedupe_dashboard_title(es_client, base_title, workspace_esid)

        # #3 smoke followup #5 (2026-05-25): check idempotency
        # BEFORE calling instantiate so the response can distinguish
        # "fresh install" from "re-using existing dashboard." Without
        # this distinction the toast lies — it says "Dashboard X
        # created" even when no new doc was written.
        was_existing = False
        if es_client is not None:
            try:
                prior_id = TemplateService._find_existing_instantiation(es_client, team_esid, template_id)
                was_existing = prior_id is not None
            except Exception:
                was_existing = False

        # scope the installed dashboard's visibility EXACTLY like the
        # New button (teams-only): a non-workspace-admin creator (org
        # owner / team user) lands ``show_teams`` scoped to their OWN teams, so
        # the dashboard is visible to their org only — never the workspace-wide
        # ``show_all`` that would surface it in sibling orgs of the same agency
        # workspace (and that they couldn't even mutate,). A
        # workspace admin gets ``show_teams`` with no pre-selected teams (they
        # re-scope in properties); a bare privileged creator with no teams falls
        # through to the legacy ``show_all`` so it isn't hidden from everyone.
        _is_ws_admin = _current_user_is_workspace_admin()
        _vis_defaults = _builder_visibility_defaults(_is_ws_admin)
        _default_team_esids = _vis_defaults.get("default_team_esids") or []
        if _default_team_esids:
            visibility_override = {
                "visibility_mode": "show_teams",
                "allowed_team_esids": _default_team_esids,
                "allowed_org_esids": [],
            }
        elif _is_ws_admin:
            # Workspace admin with no pre-selected team: show_all keeps it
            # visible+editable to them, re-scoped in properties (their authority
            # spans the workspace, so this is not a cross-org leak).
            visibility_override = {"visibility_mode": "show_all", "allowed_team_esids": [], "allowed_org_esids": []}
        else:
            # Non-admin with no resolvable team scope (rare): leave the override
            # off so instantiate's fallback applies (show_teams+team_esid, else
            # show_all) — the creator short-circuit still lets them mutate it.
            visibility_override = None

        # best-effort: when the resolver returned no workspace but a
        # team WAS resolved (picked / auto-picked), derive the workspace from
        # that team's doc — its workspace is authoritative. resolve_curation_workspace
        # now has an org-owner leg too, so an empty workspace is rare.
        #
        # NOTE: we do NOT refuse on a still-empty workspace. A teamless install
        # with no resolvable workspace is a SUPPORTED flow (it
        # lands show_all and is re-scoped later), and several existing tests +
        # real onboarding paths rely on it. An earlier hard refusal here broke
        # those (over-reach caught by CI). instantiate() handles an empty
        # workspace_esid the same as before this change.
        if not workspace_esid and team_esid:
            try:
                _td = get_document_by_id("de_asset_teams", team_esid)
                workspace_esid = (_td or {}).get("workspace_esid") or workspace_esid
            except Exception:  # noqa: BLE001 — boundary; keep prior (possibly empty) workspace
                pass

        try:
            dashboard_id = TemplateService.instantiate(
                template_id=template_id,
                team_esid=team_esid,
                source_account_ids=source_account_ids,
                workspace_esid=workspace_esid,
                org_esid=org_esid,
                created_by=username,
                visibility_override=visibility_override,
                # honor the user-chosen title. When they supplied one
                # we always pass it (deduped); otherwise keep the prior behavior
                # of overriding only when the template name collided.
                title_override=(deduped_title if (requested_title or deduped_title != base_title) else None),
                menu_override=requested_menu or None,
            )
        except TemplateNotFoundError as e:
            return jsonify({"ok": False, "error": e.public_message}), 404
        except TemplateInstantiationError as e:
            return jsonify({"ok": False, "error": e.public_message}), 400

        # Read back the actual title — instantiate is idempotent on
        # (team_esid, template_id), so a re-hit returns the EXISTING
        # dashboard's title, which may differ from the deduped guess
        # we computed above. The response title should reflect what
        # the user will see in the sidebar.
        actual_title = deduped_title
        try:
            installed_doc = get_document_by_id(DASHBOARDS_INDEX, dashboard_id)
            if installed_doc and installed_doc.get("title"):
                actual_title = installed_doc["title"]
        except Exception:
            pass

        return jsonify(
            {
                "ok": True,
                "dashboard_id": dashboard_id,
                "title": actual_title,
                # #3 followup #5: ``created`` is False when the
                # template was already installed for this (team, template)
                # pair — the route just returned the existing dashboard's
                # id. The client uses this to choose the right toast
                # ("created" vs. "opened existing").
                "created": not was_existing,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Error installing template",
            log_detail="install_template failed",
            exc=e,
        )
