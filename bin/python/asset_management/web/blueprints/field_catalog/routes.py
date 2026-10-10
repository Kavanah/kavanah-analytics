"""Field Catalog Blueprint Routes.

The per-tenant field picker: a workspace/org OWNER enables/disables catalog
fields (fb_ads first) for their workspace; the FB importers then request the
union of (always-on base fields + enabled optional fields) for that tenant.

GO-FORWARD ONLY: enabling a field affects future imports only — history stays
blank before the enable date (stated on the page; decided 2026-06-17).

Entitlement: owner-only. The page/API gate on ``mc_field_catalog``, which the
 bridge grants on ``Permission.WORKSPACE_DELETE`` — the one permission
``workspace_owner`` holds that ``workspace_admin`` does not (superuser passes
via the all-true branch). Writes additionally pin the TARGET workspace: a
non-superuser may only edit their own primary workspace.
"""

import logging

from flask import abort, jsonify, render_template, request, session

from . import field_catalog_bp

try:
    from web.services.field_catalog_service import (
        SOURCE_TOGGLE_SOURCES,
        FieldCatalogService,
        FieldCatalogError,
        FieldNotToggleableError,
    )
    from web.services.connector_status_service import list_teams
    from web.services.role_filter_service import RoleFilterService
    from web.services.role_permissions_service import RolePermissionsService
    from web.services.workspace_membership_service import WorkspaceMembershipService
except ImportError:
    from ...services.field_catalog_service import (
        SOURCE_TOGGLE_SOURCES,
        FieldCatalogService,
        FieldCatalogError,
        FieldNotToggleableError,
    )
    from ...services.connector_status_service import list_teams
    from ...services.role_filter_service import RoleFilterService
    from ...services.role_permissions_service import RolePermissionsService
    from ...services.workspace_membership_service import WorkspaceMembershipService


_SOURCES = ("fb_ads",)  # FB ad insights only (PHASE-109+ generalizes)

# section metadata for the multi-source page. Adding a source =
# one _SOURCES entry + one label here + a catalog/<source>.json; the template
# renders the anchor nav and one collapsible section per registered source.
_SOURCE_LABELS = {
    "fb_ads": "Facebook Ad Insights",
}

# per-team import-source toggles. Independent of the picker _SOURCES:
# a toggleable source needs no catalog/<source>.json (it gates the whole
# importer run), so fb_pages / instagram appear here land.
_TOGGLE_SOURCE_LABELS = {
    "fb_ads": "Facebook Ad Insights",
    "fb_pages": "Facebook Page Insights",
    "instagram": "Instagram Insights",
}


def _require_field_catalog() -> None:
    """403 unless the caller holds mc_field_catalog (owner-only via the
     bridge; username kwarg REQUIRED for wizard-signup owners whose
    grants are System-2, not ES roles)."""
    perms = RolePermissionsService.get_merged_permissions(
        session.get("roles", []),
        username=session.get("username"),
    )
    if not perms.get("mc_field_catalog"):
        abort(403, description="This page requires workspace-owner access.")


def _effective_allowed_workspaces():
    """the workspace allow-list the team dropdown filters by, mirroring
    ``data_connectors._effective_allowed_workspaces``.

    A privileged caller (superuser / asset_admin) is narrowed from the broad
    see-everything grant to the STRICT primary-workspace + explicit-grant set
    (``WorkspaceMembershipService.primary_and_grant_workspaces``) — so a
    superuser sees the teams in THEIR primary workspace, not the first
    workspace in the cluster. A scoped caller keeps ``get_allowed_workspaces``.
    Contract: ``None`` = see-all, ``[]`` = deny-all, ``[esids]`` = filter.
    Fail-soft to the legacy allow-list on any error."""
    try:
        if not RoleFilterService.current_user_is_privileged():
            return RoleFilterService.get_allowed_workspaces()
    except Exception:  # noqa: BLE001 — unresolvable privilege check → treat as non-privileged
        return RoleFilterService.get_allowed_workspaces()
    try:
        return WorkspaceMembershipService.primary_and_grant_workspaces(session.get("username", "") or "")
    except Exception as e:  # noqa: BLE001 — degrade to the legacy allow-list, never lock a superuser out
        logging.warning("field_catalog._effective_allowed_workspaces: %s", e)
        return RoleFilterService.get_allowed_workspaces()


def _resolve_workspace() -> str:
    """The workspace this request manages.

    A superuser may target any workspace via ``?workspace=<esid>``; everyone
    else is pinned to their own primary workspace (Phase-1: one workspace per
    user), so a crafted ?workspace can never edit a foreign tenant."""
    username = session.get("username")
    requested = (request.args.get("workspace") or "").strip()
    if requested and "superuser" in session.get("roles", []):
        return requested
    workspace_esid = WorkspaceMembershipService.get_primary_workspace(username)
    if not workspace_esid:
        abort(404, description="No workspace resolved for this user.")
    return workspace_esid


def _check_source(source: str) -> None:
    if source not in _SOURCES:
        abort(404, description=f"Unknown catalog source '{source}'.")


# ── Page route ────────────────────────────────────────────────────────────────


@field_catalog_bp.route("/")
def index() -> str:
    _require_field_catalog()
    workspace_esid = _resolve_workspace()
    sources = [{"source": s, "label": _SOURCE_LABELS.get(s, s)} for s in _SOURCES]
    return render_template("field_catalog.html", workspace_esid=workspace_esid, sources=sources)


# ── API routes ────────────────────────────────────────────────────────────────


@field_catalog_bp.route("/api/<source>")
def api_fields(source: str):
    """The picker listing: catalog fields (metric + action_metric families)
    with this workspace's enabled/core/deprecated/window-only state."""
    _require_field_catalog()
    _check_source(source)
    workspace_esid = _resolve_workspace()
    return jsonify(
        {
            "workspace_esid": workspace_esid,
            "source": source,
            "fields": FieldCatalogService.picker_fields(workspace_esid, source),
        }
    )


@field_catalog_bp.route("/api/<source>/<field_id>", methods=["PUT"])
def api_toggle(source: str, field_id: str):
    """Persist one toggle for the caller's workspace. Owner-only; the service
    rejects core-disable, deprecated-enable, unmapped-enable, and non-picker
    fields server-side."""
    _require_field_catalog()
    _check_source(source)
    workspace_esid = _resolve_workspace()
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        abort(400, description="Body must include 'enabled' (boolean).")
    try:
        override = FieldCatalogService.set_field(
            workspace_esid,
            source,
            field_id,
            bool(body["enabled"]),
            session.get("username") or "unknown",
        )
    except FieldNotToggleableError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except FieldCatalogError as e:
        logging.warning("toggle failed for %s/%s on %s: %s", source, field_id, workspace_esid, e)
        return jsonify({"success": False, "error": "Could not save the change. Try again."}), 503
    return jsonify({"success": True, "field_id": field_id, "override": override})


# ── Team source toggles ─────────────────────────────────────────────


@field_catalog_bp.route("/api/team-sources")
def api_team_sources():
    """The team dropdown: every team the caller may edit (scoped by their
    effective workspace allow-list, exactly like Data Connectors), plus the
    toggleable-source registry. Per-team on/off state is fetched lazily per
    selected team via ``/api/team-sources/<team_esid>``."""
    _require_field_catalog()
    teams = [
        {"team_esid": t["esid"], "team_title": t.get("title") or t["esid"], "org_title": t.get("org_title")}
        for t in list_teams(allowed_workspaces=_effective_allowed_workspaces())
    ]
    return jsonify(
        {
            "sources": [{"source": s, "label": _TOGGLE_SOURCE_LABELS.get(s, s)} for s in SOURCE_TOGGLE_SOURCES],
            "teams": teams,
        }
    )


@field_catalog_bp.route("/api/team-sources/<team_esid>")
def api_team_source_states(team_esid: str):
    """Effective on/off state for one team across every toggleable source.
    The team must be inside the caller's accessible workspaces (else 404, so
    team existence never leaks)."""
    _require_field_catalog()
    allowed = _effective_allowed_workspaces()
    workspace_esid = FieldCatalogService._workspace_for_team(team_esid)
    if allowed is not None and workspace_esid not in allowed:
        abort(404, description="Unknown team.")
    return jsonify({"team_esid": team_esid, "states": FieldCatalogService.get_team_source_states(team_esid)})


@field_catalog_bp.route("/api/team-sources/<team_esid>/<source>", methods=["PUT"])
def api_team_source_toggle(team_esid: str, source: str):
    """Persist one team's source toggle. Body: {"enabled": bool}. The service
    rejects unknown sources and teams outside the caller's accessible
    workspaces."""
    _require_field_catalog()
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        abort(400, description="Body must include 'enabled' (boolean).")
    try:
        toggle = FieldCatalogService.set_source_enabled(
            team_esid,
            source,
            bool(body["enabled"]),
            session.get("username") or "unknown",
            allowed_workspaces=_effective_allowed_workspaces(),
        )
    except FieldCatalogError as e:
        logging.warning("source toggle failed for %s/%s: %s", source, team_esid, e)
        return jsonify({"success": False, "error": e.public_message}), 400
    return jsonify({"success": True, "source": source, "team_esid": team_esid, "override": toggle})
