"""
Report Scheduler Blueprint Routes

The first user-facing surface for the report scheduler: a Schedules list page
backed by ReportScheduleService. Per-card actions (Run now / Pause / Resume /
Delete) call the JSON API here; the create/edit form (P4b) and history +
snapshot View (P4c) land in later phases.

Access is gated on ``mc_dashboard_builder`` (decision 2026-06-09): report
schedules reference dashboards, so the dashboard-builder audience is the right
one, and reusing an already-granted permission avoids the create-only
seed_defaults trap a new flag would hit. Every list/mutator call passes the
caller's UserScope so a user only sees / acts on schedules for orgs/teams they
can access (the service enforces it).
"""

import logging
import re
from datetime import datetime

from flask import Response, abort, jsonify, render_template, request, session

from lib.fb_app_attribution import finding_detail, legacy_app_findings_for_esids

from . import report_scheduler_bp


def _timezone_options() -> list:
    """IANA timezone names for the form's dropdown. Mirrors the wizard step-5
    list (analytics/routes._step5_workspace_option_lists): zoneinfo names with a
    "/" minus the legacy SystemV/US/Canada/Brazil/Chile/Mexico aliases, sorted,
    UTC first."""
    try:
        from zoneinfo import available_timezones
    except ImportError:  # pragma: no cover — py3.9+ ships zoneinfo
        return ["UTC"]
    zones = available_timezones()
    filtered = [
        z for z in zones if "/" in z and not z.startswith(("SystemV/", "US/", "Canada/", "Brazil/", "Chile/", "Mexico/"))
    ]
    filtered.sort()
    return ["UTC", *filtered]


try:
    from web import auth as auth_module
    from web.services.report_schedule_service import (
        ReportScheduleService,
        RUN_PHASE_IMPORTING,
        clamp_daily_days,
        clamp_snapshot_keep,
    )
    from web.services.role_filter_service import RoleFilterService, resolve_user_scope
    from web.services import connector_status_service
    from web.services.connector_catalog_service import ConnectorCatalogService
    from web.services.user_profile_service import UserProfileService
except ImportError:
    from ... import auth as auth_module
    from ...services.report_schedule_service import (
        ReportScheduleService,
        RUN_PHASE_IMPORTING,
        clamp_daily_days,
        clamp_snapshot_keep,
    )
    from ...services.role_filter_service import RoleFilterService, resolve_user_scope
    from ...services import connector_status_service
    from ...services.connector_catalog_service import ConnectorCatalogService
    from ...services.user_profile_service import UserProfileService


# connector_id → connector_status_service status key. Mirrors data_connectors
# _TILE_TO_STATUS_KEY: only GA4 differs (catalog google_analytics_4 ↔ status
# google_analytics); all others are identity.
_TILE_TO_STATUS_KEY = {"google_analytics_4": "google_analytics"}


def _status_key(connector_id: str) -> str:
    return _TILE_TO_STATUS_KEY.get(connector_id, connector_id)


# The ECharts dashboards store (doc _id == dashboard_id). Reused for the
# connector-filtered dashboard picker. Mirrors report_schedule_service.
_INDEX_ECHARTS_DASHBOARDS = "de_asset_echarts_dashboards"


def _resolve_dashboard_connector(data_views) -> str | None:
    """Map a dashboard's chart data_views to its connector_id, or None.

    Mirrors app._resolve_dashboard_connector for the Meta/GA4/Google
    Ads families so this blueprint doesn't import the app module — but is
    DELIBERATELY WIDER: this resolver backs the wizard's dashboard PICKER, not
    the import-kickoff path. #2 adds Echo / YouTube / Chatwoot / Disciple
    Tools so their dashboard templates surface in the picker (app.py's copy stays
    narrow on purpose — left youtube None there because import-kickoff has
    no youtube dashboard pack; the picker has no such constraint).

    Match order (most specific family first):
      * Meta family — fb_insights / fb_page_insights / ig_insights → meta_ads
      * GA4         — ga_metrics (covers *_ga_metrics and *_ga_metrics_window)
      * Google Ads  — google_ads
      * YouTube     — youtube_analytics  (data-view *_youtube_analytics)
      * Echo        — echo_conversations (data-view *_echo_conversations)
      * Chatwoot    — chatwoot            (*_chatwoot_conversations / *_chatwoot_insights)
      * Disciple Tools — disciple_tools   (data-view *_disciple_tools)
      * TikTok — tiktok_organic (data-view *_tiktok_organic)

    The substrings are taken verbatim from deployment/create-data-views.sh so a
    data-view rename there must update both. google_ads is checked before the
    bare-substring families and youtube_analytics is its own token, so none of
    the new connectors false-match the Meta/GA4/Ads suffixes.
    """
    dvs = [dv for dv in (data_views or []) if isinstance(dv, str)]
    if any(("fb_insights" in dv) or ("fb_page_insights" in dv) or ("ig_insights" in dv) for dv in dvs):
        return "meta_ads"
    if any("ga_metrics" in dv for dv in dvs):
        return "google_analytics_4"
    if any("google_ads" in dv for dv in dvs):
        return "google_ads"
    if any("youtube_analytics" in dv for dv in dvs):
        return "youtube"
    if any("echo_conversations" in dv for dv in dvs):
        return "echo"
    if any("chatwoot" in dv for dv in dvs):
        return "chatwoot"
    if any("disciple_tools" in dv for dv in dvs):
        return "disciple_tools"
    if any("tiktok_organic" in dv for dv in dvs):
        return "tiktok"
    return None


def _dashboard_data_views(source: dict) -> set:
    """Collect the data_views a dashboard doc binds (charts + table columns +
    sources). Mirrors app._dashboard_data_views' extraction."""
    dvs = set()
    for chart in (source.get("echarts_config") or {}).get("charts", []) or []:
        if chart.get("data_view"):
            dvs.add(chart["data_view"])
        for col in chart.get("table_columns", []) or []:
            if col.get("data_view"):
                dvs.add(col["data_view"])
        for s in chart.get("sources", []) or []:
            if s.get("data_view"):
                dvs.add(s["data_view"])
    return dvs


def _user_is_report_scheduler_audience() -> bool:
    """v2 audience: superusers, workspace owners, org owners.

    = RoleFilterService.current_user_is_privileged() (superuser / asset_admin /
    workspace_admin / workspace_owner) OR the user has an org-tier role
    (resolve_user_scope.org_esids non-empty — the proxy for "org owner", since
    there's no distinct org_owner role in the codebase). Decision 2026-06-11.
    """
    try:
        if RoleFilterService.current_user_is_privileged():
            return True
    except Exception:  # noqa: BLE001 — the gate must never 500
        pass
    try:
        scope = _get_user_scope()
        return bool(getattr(scope, "org_esids", None))
    except Exception:  # noqa: BLE001
        return False


def _require_report_scheduler() -> None:
    """Abort 403 unless the user is in the v2 Report Scheduler audience
    (superuser / workspace owner / org owner) — v2 gate (was
    mc_dashboard_builder in v1 P4a)."""
    if not _user_is_report_scheduler_audience():
        abort(403, description="The Report Scheduler is available to organization owners and admins.")


def _get_user_scope():
    """Resolve the current user's UserScope for org/team filtering."""
    username = session.get("username", "")
    user_roles = session.get("roles", []) or []
    config = RoleFilterService.load_config()
    return resolve_user_scope(username, user_roles, config)


def _effective_allowed_workspaces():
    """the workspace allow-list the page's SELECTION surfaces filter
    by (the org/team tree, connector tiles, the dashboard picker, the card's
    connector-active count). NOT the read-visibility scope (that's
    _visibility_scope) and NOT the save-authorize scope (_effective_scope).

    The bug: every selection surface here threaded
    ``RoleFilterService.get_allowed_workspaces()`` straight into
    ``connector_status_service.list_teams`` / ``get_team_connectors`` /
    ``workspaces_for_teams``. For a SUPERUSER that resolver returns the broad
    grant+membership+inferred union (effectively ``None`` / every workspace in
    the cluster), so the wizard tree listed every org and team in every
    workspace — exactly what already narrowed for the
    saved-schedules READ path, but never for the picker.

    Fix (user decision 2026-06-19): for a PRIVILEGED caller, substitute the
    STRICT primary-workspace + explicit-grant set —
    ``WorkspaceMembershipService.primary_and_grant_workspaces`` (primary
    workspace + explicit workspace data-grants only). This is TIGHTER than the
    schedules-list / Data Inventory ``data_grant_and_primary_workspaces``, which
    also walks the user's org/team scope up to its workspaces (Leg 3): that walk
    drags in every workspace a superuser owns scattered teams in (one superuser
    resolved to 4 of 7 on staging, 2026-06-19), defeating the narrowing. For a
    non-privileged (already scope-restricted) caller, keep ``get_allowed_workspaces()``
    unchanged: it is already bounded to their own scope, and the narrow set is
    a subset of it — substituting could only NARROW, never widen, but the
    org-tier ``_narrow_tree_to_primary_org`` leg already handles their case, so
    we leave their path untouched to avoid a behavioral change for org owners.

    Contract mirrors ``get_allowed_workspaces`` so it drops into the same
    callers unchanged:

      * privileged → a concrete list (primary + grants). Never ``None`` — a
        privileged user is bound to their workspace here, not see-all. ``[]``
        (no resolvable primary/grant) is deny-all, the same fail-closed posture
         took on the read path (a superuser with no home workspace sees
        nothing rather than everything).
      * non-privileged → ``get_allowed_workspaces()`` verbatim
        (``None``/``[]``/``[esids]``).

    Fail-soft: any error resolving the narrow set falls back to
    ``get_allowed_workspaces`` (the earlier behavior) so a transient ES
    blip degrades to the old see-all rather than locking a superuser out.
    """
    try:
        if not RoleFilterService.current_user_is_privileged():
            return RoleFilterService.get_allowed_workspaces()
    except Exception:  # noqa: BLE001 — treat an unresolvable privilege check as non-privileged
        return RoleFilterService.get_allowed_workspaces()
    try:
        try:
            from web.services.workspace_membership_service import WorkspaceMembershipService
        except ImportError:
            from ...services.workspace_membership_service import WorkspaceMembershipService

        username = session.get("username", "") or ""
        # the STRICT primary + explicit-grant set (NOT
        # data_grant_and_primary_workspaces, whose org/team-scope walk drags in
        # every workspace a superuser owns scattered teams in — one superuser
        # resolved to 4 of 7 on staging). primary_and_grant_workspaces drops that
        # walk so a superuser is bounded to their primary workspace + real grants.
        return WorkspaceMembershipService.primary_and_grant_workspaces(username)
    except Exception as e:  # noqa: BLE001 — degrade to the legacy allow-list, never crash a selection surface
        logging.warning("report_scheduler._effective_allowed_workspaces: %s", e)
        return RoleFilterService.get_allowed_workspaces()


def _effective_scope():
    """The scope to enforce on a WRITE (create/update/pause/delete/run-now).

    Named ``_effective_scope`` for historical continuity (tests patch
    this symbol). split read from write: this is the WRITE/authorize
    scope; reads use the narrower :func:`_visibility_scope`.

     v2 fix: the org/team TREE the user picked from is workspace-scoped
    (get_allowed_workspaces via connector_status_service.list_teams), but
    UserScope.{org,team}_esids is the org/team-tier resolver — and a PRIVILEGED
    user (superuser / asset_admin / workspace_admin / workspace_owner) typically
    resolves to EMPTY *_esids. With the empty UserScope, create wrongly 403s
    ("not authorized…"). Privileged users already passed the page gate and the
    tree trusts them workspace-wide, so authorize them by returning None — the
    service treats None as "skip the guard" for writes (same trust model the rest
    of this blueprint uses).

     #4: the SAME tree-vs-authz mismatch bit ORG-TIER users (org owners),
    who are not privileged so they kept their narrow resolved UserScope. The tree
    offers them every team in their allowed workspace(s) (get_allowed_workspaces
    walks org/team scope UP to the workspace, then list_teams returns ALL of that
    workspace's teams — including sibling-org teams the owner isn't a direct
    member of), but create_schedule's _scope_allows_any only admits a team in
    their own team_esids/org_esids → "not authorized for the requested org/team
    scope" on save. Fix (user decision 2026-06-15: widen authz to the workspace,
    not narrow the tree): widen the org-tier user's UserScope so its team_esids
    carry exactly the teams the tree offered them. The tree and the save check
    then agree.

    this WIDENED scope is for the SAVE/authorize path ONLY. Reusing it
    for the list/visibility path leaked schedules from sibling-org teams that
    merely share the owner's workspace (the tree's team universe is workspace-wide
    and includes orgs the owner doesn't own). Reads now use the NARROW
    _visibility_scope() so an org owner only SEES schedules for teams they are
    actually scoped to (user decision 2026-06-17). The two scopes are deliberately
    different: an org owner may still SAVE a schedule for any team the tree offered
    (authorize=widened), but that schedule is only visible to a user actually
    scoped to its team (read=narrow).
    """
    try:
        if RoleFilterService.current_user_is_privileged():
            return None
    except Exception:  # noqa: BLE001 — fall back to the resolved scope
        pass
    return _workspace_widened_scope(_get_user_scope())


def _visibility_scope():
    """The scope to enforce on a READ (list / get / history / snapshot / the
    visibility leg of a mutate gate).

     (final, user decision 2026-06-17): a schedule is visible to its
    CREATOR (see _visibility_creator / list_schedules' created_by leg) and to
    anyone whose scope intersects the schedule's org/teams — where "scope" mirrors
    the SAME workspace boundary the rest of the management console enforces, NOT a
    global see-all.

    The boundary is ``WorkspaceMembershipService.data_grant_and_primary_workspaces``
    : workspace-scope data grants + the user's primary workspace. This is
    the exact narrow set the Data Inventory switcher and dashboard-builder use —
    deliberately tighter than ``RoleFilterService.get_allowed_workspaces`` (whose
    grant+membership+inferred union balloons a broadly-granted superuser up to
    every workspace in the cluster, which is what re-leaked schedules across
    workspaces). A superuser is therefore bounded to their primary workspace +
    explicit workspace grants here, exactly as they are on the dashboard builder.

    The read team set is the UNION of:
      * the resolved UserScope's own ``team_esids`` (direct team memberships);
      * teams UNDER the user's own ``org_esids`` (_org_expanded_scope) — an org
        owner resolves org-tier with empty team_esids, but schedules store their
        scope in team_esids with empty org_esids, so org→own-teams projection is
        what lets an owner match a schedule for their org;
      * every team IN the user's primary/grant workspace(s)
        (_primary_workspace_scope via data_grant_and_primary_workspaces →
        list_teams) — so a workspace owner/superuser sees their workspace's
        schedules, and ONLY their workspace's.

    NEVER returns None (None = the service's "no filter → every schedule in every
    workspace", the original leak). A caller with no resolvable scope sees only
    schedules they created. Distinct from _effective_scope() (the SAVE/authorize
    scope), which still widens to the whole tree and may be None for privileged
    writers.
    """
    scope = _org_expanded_scope(_get_user_scope())
    return _primary_workspace_scope(scope)


def _primary_workspace_scope(scope):
    """Union ``scope``'s team_esids with every team in the caller's
    primary/grant workspace(s) — the SAME narrow set the Data Inventory switcher
    and dashboard builder use (WorkspaceMembershipService.data_grant_and_primary_
    workspaces), NOT the broad get_allowed_workspaces union.

    This is what keeps a superuser bounded to their primary workspace (+ explicit
    workspace grants) instead of every workspace in the cluster. Fail-soft: empty
    workspace set → scope unchanged; ES error → scope unchanged (never crash a
    read)."""
    if scope is None:
        scope = _get_user_scope()
    try:
        try:
            from web.services.workspace_membership_service import WorkspaceMembershipService
        except ImportError:
            from ...services.workspace_membership_service import WorkspaceMembershipService

        username = session.get("username", "") or ""
        ws = WorkspaceMembershipService.data_grant_and_primary_workspaces(username)
        if not ws:
            return scope
        ws_team_esids = {t["esid"] for t in connector_status_service.list_teams(ws) if t.get("esid")}
        if not ws_team_esids:
            return scope
        import dataclasses

        widened = frozenset(getattr(scope, "team_esids", frozenset()) or frozenset()) | ws_team_esids
        return dataclasses.replace(scope, team_esids=widened)
    except Exception as e:  # noqa: BLE001 — workspace leg is best-effort, never fatal
        logging.warning("report_scheduler._primary_workspace_scope: %s", e)
        return scope


def _visibility_creator() -> str:
    """The username whose OWN schedules are always visible to them regardless of
    scope intersection. A schedule a user created is theirs
    to see even if its teams aren't in their resolved org/team scope (e.g. they
    saved it for a workspace-tree team they're not a direct member of)."""
    return session.get("username", "") or ""


def _org_expanded_scope(scope):
    """Return ``scope`` with its team_esids widened to include every team UNDER
    the user's own org_esids (connector_status_service.list_teams_for_org) — the
    read-side counterpart to _workspace_widened_scope, but bounded to the user's
    OWN org(s) rather than the whole workspace.

    An org owner resolves to org-tier scope (org_esids set, team_esids empty);
    report schedules store their scope in team_esids with org_esids empty, so the
    org-match leg can't see them. Expanding org→own-teams lets the team-match leg
    fire for the owner's own org's teams without admitting sibling-org teams.

    Fail-soft: no org_esids → returned unchanged (a team-tier user keeps their
    resolved team_esids); ES error → unchanged (never crash a read).
    """
    if scope is None:
        return None
    org_esids = list(getattr(scope, "org_esids", frozenset()) or frozenset())
    if not org_esids:
        return scope
    try:
        own_team_esids: set = set()
        for org_esid in org_esids:
            own_team_esids |= {t["esid"] for t in connector_status_service.list_teams_for_org(org_esid) if t.get("esid")}
        if not own_team_esids:
            return scope
        import dataclasses

        widened = frozenset(getattr(scope, "team_esids", frozenset()) or frozenset()) | own_team_esids
        return dataclasses.replace(scope, team_esids=widened)
    except Exception as e:  # noqa: BLE001 — expansion is best-effort, never fatal
        logging.warning("report_scheduler._org_expanded_scope: %s", e)
        return scope


def _workspace_widened_scope(scope):
    """Return ``scope`` widened so its team_esids include every team the org/team
    tree offers this user — i.e. all teams in their allowed workspace(s), the
    same set api_org_team_tree builds.

    Org-tier users (org owners) are scope-restricted, so get_allowed_workspaces
    returns their workspace allow-list (not None); list_teams over it is exactly
    the tree's team universe. We union those esids onto the resolved scope so the
    service's scope-intersection checks accept any team the wizard let them pick.

    Fail-soft: if the workspace allow-list can't be resolved (ES blip / None =
    see-all admin path, already handled by the privileged branch), the original
    scope is returned unchanged so we never WIDEN past the workspace nor crash a
    read. A user whose tree is empty stays exactly as resolved.
    """
    if scope is None:
        return None
    try:
        allowed_ws = RoleFilterService.get_allowed_workspaces()
        # None → no workspace filter (see-all); the privileged branch already
        # returns None for those, so here a None means "don't widen".
        if allowed_ws is None:
            return scope
        team_esids = {t["esid"] for t in connector_status_service.list_teams(allowed_ws) if t.get("esid")}
        if not team_esids:
            return scope
        import dataclasses

        widened = frozenset(getattr(scope, "team_esids", frozenset()) or frozenset()) | team_esids
        return dataclasses.replace(scope, team_esids=widened)
    except Exception as e:  # noqa: BLE001 — widening is best-effort, never fatal
        logging.warning("report_scheduler._workspace_widened_scope: %s", e)
        return scope


def _narrow_tree_to_primary_org(teams: list[dict]) -> list[dict]:
    """drop teams an org owner only owns SECONDARILY from the
    org/team tree. Keep a team only when its PRIMARY org (``team['org_esid']`` =
    ``organization_esid[0]``, from ``connector_status_service.list_teams``) is one
    of the caller's own ``UserScope.org_esids``.

    Privileged callers (superuser / asset_admin / workspace_admin / owner) and any
    caller whose scope resolves to no org_esids see the full ``teams`` list
    unchanged — privileged users are trusted workspace-wide and a team-tier user
    has no org to narrow by. The narrowing fires only for org owners (the org-tier
    audience whose ``org_esids`` is non-empty).

    Fail-soft: any error resolving the scope leaves ``teams`` unchanged — a read
    must never 500, and falling open here only WIDENS what's shown (no authz
    bypass: the save path independently re-checks scope).
    """
    try:
        if _current_user_is_privileged():
            return teams
        scope = _get_user_scope()
        own_orgs = frozenset(getattr(scope, "org_esids", frozenset()) or frozenset())
        if not own_orgs:
            return teams
        return [t for t in teams if t.get("org_esid") in own_orgs]
    except Exception as e:  # noqa: BLE001 — narrowing is best-effort, never fatal
        logging.warning("report_scheduler._narrow_tree_to_primary_org: %s", e)
        return teams


def _echo_tenant_uniqueness_gate(connector_id, team_esids, *, exclude_schedule_id: str = ""):
    """at most one Echo report per (workspace, Echo tenant) pair.

    Supersedes the ``_echo_owner_gate`` workspace-owner block: org owners
    ARE now allowed to schedule Echo (an org owner may own a DIFFERENT Echo tenant
    than the rest of the workspace), gated only by per-tenant uniqueness. Echo's
    API is still tenant-scoped (every import is a full-tenant pull), so two
    schedules pointing at the SAME tenant in the same workspace is the conflict to
    prevent — regardless of who created the existing one.

    Returns a 409 JSON response to return from the route, or None when the
    connector isn't echo / the selected teams resolve to no Echo tenant / no other
    schedule already covers any of their tenants. ``exclude_schedule_id`` skips the
    schedule being edited so a no-op edit doesn't self-conflict."""
    if (connector_id or "").strip() != "echo":
        return None
    conflict = ReportScheduleService.echo_tenant_conflict(team_esids or [], exclude_schedule_id=exclude_schedule_id)
    if conflict is None:
        return None
    tenant = conflict.get("tenant_title") or conflict.get("tenant_esid") or "this tenant"
    return (
        jsonify(
            {
                "ok": False,
                "error": (
                    f"An Echo report already exists for tenant {tenant} in this workspace. "
                    "There can only be one Echo report per tenant."
                ),
            }
        ),
        409,
    )


def _current_user_is_privileged() -> bool:
    """current_user_is_privileged with the same never-500 guard the audience gate
    uses (#1: drives the mutate-vs-read-only ownership check)."""
    try:
        return bool(RoleFilterService.current_user_is_privileged())
    except Exception:  # noqa: BLE001 — fail closed (treat as non-privileged)
        return False


def _can_mutate(schedule: dict) -> bool:
    """Whether the current session user may Edit / Pause / Delete / Run-now this
    schedule. Thin request-context wrapper over the pure
    ReportScheduleService.can_mutate predicate."""
    return ReportScheduleService.can_mutate(
        schedule,
        username=session.get("username", ""),
        is_privileged=_current_user_is_privileged(),
    )


def _mutate_gate(schedule_id: str, *, schedule: dict | None = None):
    """Shared guard for the mutator routes: load the schedule (unless the caller
    already has it), enforce both visibility (scope) and ownership (can_mutate).
    Returns ``(schedule, None)`` when the caller may mutate, or
    ``(None, (json, status))`` to return.

    A schedule the caller can't even SEE is 404 (don't leak existence, matching
    api_get); one they can see but don't OWN is 403 read-only. This is the
    server-side enforcement behind the card's hidden buttons — a crafted request
    to a workspace-owned schedule is refused here, not just hidden in the UI.

    Connector-specific gates that carry their OWN message (the echo
    workspace-owner gate) run BEFORE this in the route, so their specific 403
    surfaces rather than the generic ownership one — echo is stricter than
    creator-ownership (it needs a workspace owner, not just the creator).
    """
    if schedule is None:
        schedule = ReportScheduleService.get_schedule(schedule_id)
    if not schedule:
        return None, (jsonify({"ok": False, "error": "schedule not found"}), 404)
    # the visibility leg uses the NARROW read scope (resolved org/team,
    # NOT the wider save-authorize scope) OR the creator leg. A schedule the caller
    # can neither see under their own scope nor created is 404 (don't leak
    # existence). You can't mutate what you can't see.
    scope = _visibility_scope()
    if scope is not None and not ReportScheduleService._scope_or_creator_allows(
        scope, schedule, created_by=_visibility_creator()
    ):
        return None, (jsonify({"ok": False, "error": "schedule not found"}), 404)
    if not _can_mutate(schedule):
        return None, (
            jsonify({"ok": False, "error": "This schedule is workspace-owned; you can view its history but not change it."}),
            403,
        )
    return schedule, None


# the create/update SELECTION gate. ``_effective_scope`` is None for
# every privileged caller, and every self-signup customer is a workspace owner,
# i.e. privileged. The service skips its scope check on None, so a workspace
# owner could post another tenant's team_esids / dashboard_ids, then run or email
# the schedule and read that tenant's data. These helpers bind the posted
# selection to the caller's own teams and readable dashboards before anything is
# saved. Platform admins (superuser / asset_admin) keep the unchecked behaviour.
_SELECTION_DENIED = "You can only schedule reports for your own teams and dashboards."


def _is_platform_admin() -> bool:
    """superuser / asset_admin by session role — the same test as
    app._require_cluster_admin (not importable here: the app imports this
    blueprint)."""
    roles = session.get("roles", []) or []
    return "superuser" in roles or "asset_admin" in roles


def _selection_allow_list():
    """The concrete ``(team_esids, org_esids)`` a create/update selection may
    name, or None for a platform admin (unchecked).

    Non-privileged callers get ``_effective_scope`` unchanged (the #4
    workspace-widened scope, i.e. the tree's team universe). For a privileged
    tenant caller (workspace owner / admin) ``_effective_scope()`` is None, so
    bind them to the same universe the org/team tree offers them: the resolved
    scope plus every team in ``_effective_allowed_workspaces`` (
    primary workspace + explicit grants), with those teams' primary orgs. A None
    workspace set (unresolvable) admits nothing rather than everything.
    """
    if _is_platform_admin():
        return None
    scope = _effective_scope()
    if scope is not None:
        return (
            frozenset(getattr(scope, "team_esids", frozenset()) or frozenset()),
            frozenset(getattr(scope, "org_esids", frozenset()) or frozenset()),
        )
    own = _get_user_scope()
    allowed_ws = _effective_allowed_workspaces()
    tree = connector_status_service.list_teams(allowed_ws) if allowed_ws is not None else []
    teams = set(getattr(own, "team_esids", frozenset()) or frozenset())
    teams |= {t["esid"] for t in tree if t.get("esid")}
    orgs = set(getattr(own, "org_esids", frozenset()) or frozenset())
    orgs |= {t["org_esid"] for t in tree if t.get("org_esid")}
    return frozenset(teams), frozenset(orgs)


def _dashboard_selectable(dashboard_id: str) -> bool:
    """True iff the caller may put ``dashboard_id`` on a schedule: a built-in
    ``report:*`` type, or a de_asset_echarts_dashboards doc (loaded exactly as
    the runner loads it) that passes the Dashboard Builder's read gate — the
    same gate behind the email dialog's dashboard list. Missing doc → False."""
    try:
        from web.services.report_types_service import is_report_type
    except ImportError:
        from ...services.report_types_service import is_report_type
    from ..dashboard_builder.routes import _user_may_read_dashboard

    if is_report_type(dashboard_id):
        return True
    es = _es_client()
    if es is None:
        return False
    source = ReportScheduleService._load_dashboard_source(es, dashboard_id)
    return bool(source) and bool(_user_may_read_dashboard(source))


def _selection_gate(*, team_esids: list, org_ids: list, dashboard_ids: list):
    """refuse (403) a create/update whose selection reaches outside
    the caller's tenant. ALL-match: every team must be one of the caller's
    teams, every org (checked only when no team is selected — a team selection
    is what scopes the data) one of the caller's orgs, and every dashboard one
    the caller may read. Returns None when the selection is allowed."""
    allow = _selection_allow_list()
    if allow is None:
        return None
    allowed_teams, allowed_orgs = allow
    foreign_teams = [t for t in team_esids if t not in allowed_teams]
    foreign_orgs = [o for o in org_ids if o not in allowed_orgs]
    foreign_dashboards = [d for d in dashboard_ids if not _dashboard_selectable(d)]
    if foreign_teams or foreign_orgs or foreign_dashboards:
        logging.warning(
            "report schedule selection refused: user=%s teams=%s orgs=%s dashboards=%s",
            session.get("username", ""),
            foreign_teams,
            foreign_orgs,
            foreign_dashboards,
        )
        return jsonify({"ok": False, "error": _SELECTION_DENIED}), 403
    return None


# connector-active team count for the card Scope label.
#
# A schedule's Scope label used to read the raw selected-team count ("33 teams")
# even when only a handful of those teams actually have an active connector of
# the report's type. We resolve, per schedule, HOW MANY of its selected teams
# have the schedule's connector CONFIGURED, so the card can render "2 of 33
# teams". The set-with-an-active-connector resolution lives here (server-side),
# mirroring api_connector_tiles: status key via _status_key, configured-ness via
# connector_status_service.get_team_connectors. The label is then rendered from
# the count the API returns rather than recomputed in JS.


def _schedule_team_esids_for_count(schedule: dict) -> list[str]:
    """The teams a schedule scopes, as the card resolves them — v2 ``team_esids``
    if present, else the v1 single ``team_id`` (when it isn't the all-teams
    placeholder). Mirrors the JS in report_scheduler.html so the server count
    matches the M the card shows."""
    team_esids = [t for t in (schedule.get("team_esids") or []) if t]
    if team_esids:
        return team_esids
    team_id = (schedule.get("team_id") or "").strip()
    if team_id and team_id != "__all__":
        return [team_id]
    return []


def _annotate_active_team_counts(schedules: list[dict]) -> None:
    """Stamp ``active_team_count`` on each schedule that scopes specific teams and
    carries a connector_id: how many of its selected teams have that connector
    CONFIGURED (an active connector of the report's type).

    Schedules with no connector_id, or no specific teams (all-teams scope), get
    no annotation — the card keeps the plain "All teams" / "1 team" / "N teams"
    wording for them.

    Workspace-scoped like api_connector_tiles (a team outside the caller's
    workspace scope contributes nothing). get_team_connectors results are cached
    per team across the whole list, since multi-team schedules overlap heavily.
    Fail-soft: any error leaves a schedule un-annotated (card falls back to the
    raw count) rather than 500-ing the list.
    """
    try:
        allowed_ws = _effective_allowed_workspaces()  # narrow privileged callers to primary workspace + grants
    except Exception:  # noqa: BLE001 — never break the list over a scope read
        return

    # team_esid → set of CONFIGURED status keys for that team (cached per call).
    team_configured: dict[str, set] = {}

    def _configured_keys(team_esid: str) -> set:
        if team_esid in team_configured:
            return team_configured[team_esid]
        keys: set = set()
        try:
            payload = connector_status_service.get_team_connectors(team_esid, allowed_ws)
            for conn in (payload or {}).get("connectors") or []:
                if conn.get("configured"):
                    keys.add(conn.get("key"))
        except Exception:  # noqa: BLE001 — a single bad team doesn't sink the list
            keys = set()
        team_configured[team_esid] = keys
        return keys

    for s in schedules:
        connector_id = (s.get("connector_id") or "").strip()
        teams = _schedule_team_esids_for_count(s)
        if not connector_id or not teams:
            continue  # all-teams / dashboard-only schedule — no "N of M" wording
        skey = _status_key(connector_id)
        s["active_team_count"] = sum(1 for t in teams if skey in _configured_keys(t))


# ── Page route ────────────────────────────────────────────────────────────────


@report_scheduler_bp.route("/")
def index() -> str:
    _require_report_scheduler()
    return render_template("report_scheduler.html", timezone_options=_timezone_options())


# ── JSON API ────────────────────────────────────────────────────────────────


@report_scheduler_bp.route("/api/list")
def api_list():
    """Return the caller's schedules (scope-filtered).

     #1: each schedule carries ``can_edit`` — whether THIS caller may
    mutate it (owns it, or is privileged). The card UI renders read-only +
    "Workspace owned" when false; the mutator routes re-check it server-side so
    the flag is a UI hint, not the security boundary.
    """
    _require_report_scheduler()
    scope = _visibility_scope()  # resolved org/team scope (no admin all-workspaces inflation)
    username = session.get("username", "")
    # creator leg: a user always sees schedules they created, even ones
    # scoped to teams outside their resolved scope.
    schedules = ReportScheduleService.list_schedules(scope=scope, created_by=username)
    is_privileged = _current_user_is_privileged()
    for s in schedules:
        s["can_edit"] = ReportScheduleService.can_mutate(s, username=username, is_privileged=is_privileged)
        # split the two kinds client-side; default earlier rows to
        # "import" so they render in the Scheduled imports section as before.
        s["schedule_type"] = s.get("schedule_type") or "import"
        # surface the most recent run's classified import errors so the
        # card can render an "Errors" button ONLY when there were errors. The
        # summary is computed from last_run_state.import_errors, which
        # list_schedules back-filled from the (terminal) import progress doc.
        err = ReportScheduleService.summarize_import_errors(s.get("last_run_state") or {})
        s["import_error_count"] = err["count"]
        s["import_errors"] = err["errors"]
        s["import_info"] = err["info"]
    # stamp the connector-active team count so the card Scope label can
    # read "2 of 33 teams" instead of the raw selected count.
    _annotate_active_team_counts(schedules)
    return jsonify({"ok": True, "schedules": schedules})


# ── v2: org/team tree + connector tiles (Phase B) ────────────────────────────

# Cap the teams a single connector-tiles request may probe (each is an ES read).
_MAX_TILE_TEAMS = 200


@report_scheduler_bp.route("/api/org-team-tree")
def api_org_team_tree():
    """Visible orgs grouping their teams, for the page's multi-select tree.

    {orgs: [{org_title, teams: [{esid, title}]}]} sorted by org then team.
    Workspace-scoped via get_allowed_workspaces (None=see-all / []=none /
    [esids]=filter). Teams whose org_title is empty group under "(Unassigned)".
    a team still on its own legacy Meta app additionally carries
    ``legacy_meta_app: {path, detail}`` (absent on clean teams) — the tree
    renders it as an amber badge so migration progress is visible in-page.

    primary-org narrowing for org owners: a non-privileged org-tier
    caller (an org owner) sees a team ONLY when its PRIMARY org
    (``organization_esid[0]``, surfaced as ``list_teams``' ``org_esid``) is one of
    the caller's own ``UserScope.org_esids``. A team that lists the owner's org
    only as a SECONDARY org — or carries no primary org at all — is dropped. This
    matches the read-side ``_org_expanded_scope`` (which walks
    ``list_teams_for_org`` on the same ``organization_esid[0]`` rule), so the
    narrowed tree stays a subset of the workspace-widened save scope and
    create/edit still authorize. Privileged / see-all callers (superuser,
    asset_admin, workspace_admin / owner) keep the full workspace tree.
    """
    _require_report_scheduler()
    allowed_ws = _effective_allowed_workspaces()  # narrow privileged callers to primary workspace + grants
    teams = _narrow_tree_to_primary_org(connector_status_service.list_teams(allowed_ws))

    # per-team legacy-Meta-app warning for the tree's amber badge.
    # Bounded ES cost for the whole tree (one teams read + one pages read + one
    # apps read, regardless of team count) and fail-soft to {} — a missing
    # badge must never break the tree. Only affected teams carry the field.
    legacy = legacy_app_findings_for_esids([t["esid"] for t in teams])

    groups: dict = {}
    for t in teams:
        org = (t.get("org_title") or "").strip() or "(Unassigned)"
        entry = {"esid": t["esid"], "title": t["title"]}
        finding = legacy.get(t["esid"])
        if finding:
            entry["legacy_meta_app"] = {"path": finding["path"], "detail": finding_detail(finding)}
        groups.setdefault(org, []).append(entry)
    orgs = [
        {"org_title": org, "teams": sorted(tms, key=lambda x: x["title"].lower())}
        for org, tms in sorted(groups.items(), key=lambda kv: kv[0].lower())
    ]
    return jsonify({"ok": True, "orgs": orgs})


@report_scheduler_bp.route("/api/connector-tiles", methods=["POST"])
def api_connector_tiles():
    """Given selected team_esids, return the connector tiles to show — the union
    of connectors CONFIGURED on ≥1 selected team. Each tile carries team_count
    (how many of the selected teams have it configured). Scope-enforced: a team
    outside the caller's workspace scope contributes nothing.
    """
    _require_report_scheduler()
    body = request.get_json(silent=True) or {}
    team_esids = [t for t in (body.get("team_esids") or []) if t][:_MAX_TILE_TEAMS]
    if not team_esids:
        return jsonify({"ok": True, "tiles": []})

    allowed_ws = _effective_allowed_workspaces()  # narrow privileged callers to primary workspace + grants
    # status_key → count of selected teams that have it configured.
    counts: dict = {}
    for team_esid in team_esids:
        payload = connector_status_service.get_team_connectors(team_esid, allowed_ws)
        if not payload:
            continue  # out of scope / not found → contributes nothing
        for conn in payload.get("connectors") or []:
            if conn.get("configured"):
                counts[conn.get("key")] = counts.get(conn.get("key"), 0) + 1

    # Map the configured status keys back to catalog tiles for display.
    tiles = []
    for tile in ConnectorCatalogService.all_tiles():
        skey = _status_key(tile.connector_id)
        if skey in counts:
            icon = f"/static/dashboard_builder/icons/{tile.icon_file}" if getattr(tile, "icon_file", "") else ""
            tiles.append(
                {
                    "connector_id": tile.connector_id,
                    "name": tile.name,
                    "icon": icon,
                    "glyph": getattr(tile, "logo_glyph", ""),
                    "team_count": counts[skey],
                }
            )
    return jsonify({"ok": True, "tiles": tiles})


def _connector_filtered_teams(connector_id: str, candidate_esids: list) -> list[dict]:
    """the SUBSET of ``candidate_esids`` whose team has ``connector_id``
    CONFIGURED, as ``[{"esid", "title", "org_title"}]`` (sorted by title).

    Single source of truth for the schedule dialog's Scope listbox in BOTH the
    create and edit flows. Resolves configured-ness through the same primitives
    api_connector_tiles / _annotate_active_team_counts use — get_team_connectors
    keyed by _status_key (so the GA4 catalog id google_analytics_4 maps to the
    google_analytics status key) — so the dialog list and the tile count cannot
    drift. Scope-enforced: a team outside the caller's workspace scope (or one not
    found) contributes nothing. Order of the input is preserved as a title sort,
    matching the page tree.
    """
    skey = _status_key(connector_id)
    allowed_ws = _effective_allowed_workspaces()  # narrow privileged callers to primary workspace + grants
    teams: list[dict] = []
    seen: set = set()
    for esid in candidate_esids:
        if not esid or esid in seen:
            continue
        seen.add(esid)
        payload = connector_status_service.get_team_connectors(esid, allowed_ws)
        if not payload:
            continue  # out of scope / not found → contributes nothing
        configured = any(c.get("configured") and c.get("key") == skey for c in payload.get("connectors") or [])
        if not configured:
            continue
        team = payload.get("team") or {}
        teams.append(
            {
                "esid": esid,
                "title": team.get("title") or esid,
                "org_title": (team.get("org_title") or "").strip(),
            }
        )
    teams.sort(key=lambda t: t["title"].lower())
    return teams


@report_scheduler_bp.route("/api/connector-teams", methods=["POST"])
def api_connector_teams():
    """Given a connector_id and a candidate set of team_esids, return ONLY the
    teams that have that connector CONFIGURED — for the schedule dialog's Scope
    listbox.

    Create flow passes the page-tree selection; edit flow passes the schedule's
    saved team_esids. Both narrow to connector-valid teams via the SAME path
    (_connector_filtered_teams) so neither dialog can show a team the connector
    tile count excluded. {teams: [{esid, title, org_title}]}.

    ``include_all`` widens the candidate set on EDIT so the "Show
    unselected teams" toggle can surface teams NOT already in the schedule's
    scope. When set, the candidate set becomes the whole team universe the page
    tree would offer this caller (``_effective_allowed_workspaces`` → list_teams,
    narrowed to the caller's primary org for org owners exactly as
    api_org_team_tree does) UNIONed with the posted team_esids (so the currently-
    selected teams are always included even if scope resolution drops one). The
    result is still passed through ``_connector_filtered_teams`` unchanged, so
    the "only connector-valid teams are returned" contract still holds — extra
    teams without a valid connector of this type never appear. Off (default) →
    the earlier behavior: candidates are exactly the posted team_esids.
    """
    _require_report_scheduler()
    body = request.get_json(silent=True) or {}
    connector_id = (body.get("connector_id") or "").strip()
    team_esids = [t for t in (body.get("team_esids") or []) if t]
    include_all = bool(body.get("include_all"))
    if not connector_id:
        return jsonify({"ok": True, "teams": []})

    if include_all:
        # The team universe the page tree offers this caller, narrowed for org
        # owners the same way api_org_team_tree narrows it. Union the posted
        # team_esids first so a currently-selected team survives even if it falls
        # outside the resolved workspace set (e.g. a schedule saved for a team the
        # caller can no longer resolve).
        allowed_ws = _effective_allowed_workspaces()
        tree_teams = _narrow_tree_to_primary_org(connector_status_service.list_teams(allowed_ws))
        candidates: list = list(dict.fromkeys([*team_esids, *(t["esid"] for t in tree_teams if t.get("esid"))]))
    else:
        candidates = team_esids

    candidates = candidates[:_MAX_TILE_TEAMS]
    if not candidates:
        return jsonify({"ok": True, "teams": []})
    return jsonify({"ok": True, "teams": _connector_filtered_teams(connector_id, candidates)})


# ── v2 wizard support: connector-filtered dashboards + profile tz (Phase C) ──


def _es_client():
    """ES client via the shared helper. lib.es_helpers resolves in every run
    context the service already imports it from (top-level under /app/python)."""
    from lib.es_helpers import get_elasticsearch_client

    return get_elasticsearch_client()


@report_scheduler_bp.route("/api/connector-dashboards")
def api_connector_dashboards():
    """Dashboards whose resolved connector == ?connector_id (id + title),
    scoped to the workspace(s) of the wizard's selected ?team_esids.

    Backs the wizard's Dashboard step. An empty list is a valid result (the
    connector has no associated dashboard — the wizard then offers an
    import-only save). #2: Echo / YouTube / Chatwoot / Disciple Tools now
    resolve (see _resolve_dashboard_connector), so their templates surface here.

    workspace scope (security). The picker used to scan EVERY echarts
    dashboard globally and filter only by connector type, so a standard title
    that exists once per workspace (e.g. "Campaign Performance" in four
    workspaces) appeared 2–4 times AND a user could pick a dashboard_id that
    belongs to a workspace their schedule isn't scoped to (a cross-workspace
    data-exposure risk). We now bound the scan to the workspace(s) the selected
    teams live in (connector_status_service.workspaces_for_teams), which also
    applies the caller's read scope (get_allowed_workspaces) so a crafted
    team_esids pointing at a foreign workspace resolves to nothing. Within a
    single workspace titles are unique, so the duplicates disappear and
    foreign-workspace dashboards can no longer be picked. Dedup by title alone
    would be the WRONG fix — it would merge distinct dashboards from different
    workspaces into one row.

     #3: the title/id dedup is kept as a secondary safety net (true
    duplicate docs, or legacy docs keyed only by _id, within the scoped set).
    """
    _require_report_scheduler()
    connector_id = (request.args.get("connector_id") or "").strip()
    if not connector_id:
        return jsonify({"ok": True, "dashboards": []})

    # resolve the in-scope workspace(s) from the selected teams. With
    # no teams selected, or none in the caller's scope, the picker shows nothing
    # rather than falling back to the global scan (refuse, don't leak).
    team_esids = [t for t in request.args.getlist("team_esids") if t]
    allowed_ws = _effective_allowed_workspaces()  # narrow privileged callers to primary workspace + grants
    scope_workspaces = connector_status_service.workspaces_for_teams(team_esids, allowed_ws)
    if not scope_workspaces:
        return jsonify({"ok": True, "dashboards": []})

    es = _es_client()
    if es is None:
        return jsonify({"ok": True, "dashboards": []})
    try:
        res = es.search(
            index=_INDEX_ECHARTS_DASHBOARDS,
            body={"query": {"match_all": {}}, "size": 500},
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:
        logging.warning("report_scheduler.api_connector_dashboards: %s", e)
        return jsonify({"ok": True, "dashboards": []})

    seen: set = set()
    out = []
    for hit in res.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        # workspace gate: a dashboard outside the selected teams'
        # workspace(s) is excluded. A doc with no workspace_esid stamp cannot be
        # attributed to a workspace, so it is excluded too (fail closed for a
        # security fix — the v1 path's list_dashboards lets legacy docs through,
        # but here a foreign pick is the exact risk we're closing).
        if (src.get("workspace_esid") or "") not in scope_workspaces:
            continue
        # follow-up — exclude HIDDEN dashboards. ``visibility_mode ==
        # "hide"`` is a viewer-side setting (DashboardService.get_visible_echarts_
        # dashboards hides it on the user-facing /dashboards page), so a scheduled
        # report — which a normal viewer consumes — must not target one. This also
        # removes the remaining same-workspace same-title duplicates the picker
        # showed: where two docs share a title (e.g. "Key Performance Indicators")
        # one is typically a private ``hide`` draft alongside the published
        # ``show_all`` copy, and only the published one belongs in the picker.
        if (src.get("visibility_mode") or "show_all") == "hide":
            continue
        if _resolve_dashboard_connector(_dashboard_data_views(src)) != connector_id:
            continue
        did = src.get("dashboard_id") or hit["_id"]
        title = src.get("title") or src.get("dashboard_title") or "(untitled)"
        # Collapse on dashboard_id when present, else on the displayed title, so
        # duplicate docs within the scoped set show as a single picker entry.
        dedupe_key = ("id", did) if src.get("dashboard_id") else ("title", title.strip().lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append({"id": did, "title": title})
    out.sort(key=lambda d: d["title"].lower())
    return jsonify({"ok": True, "dashboards": out})


@report_scheduler_bp.route("/api/profile-timezone")
def api_profile_timezone():
    """The caller's profile timezone for the wizard's tz default:
    {time_zone, is_default}. is_default → the wizard may use the browser zone."""
    _require_report_scheduler()
    username = session.get("username", "")
    try:
        tz = UserProfileService.get_time_zone_detailed(username)
    except Exception:  # noqa: BLE001 — non-critical default
        tz = {"time_zone": "UTC", "is_default": True}
    return jsonify({"ok": True, "time_zone": tz.get("time_zone", "UTC"), "is_default": bool(tz.get("is_default"))})


def _split_recipients(raw) -> list:
    """Accept a list or a comma/newline/semicolon-separated string of emails →
    a clean list of non-empty trimmed addresses."""
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[,;\n]+", str(raw or ""))
    return [e.strip() for e in items if e and e.strip()]


# follow-up — server-side caps for the user-typed schedule fields.
# MUST mirror the HTML ``maxlength`` attributes in report_scheduler.html (both
# the import rsm-* and email esm-* dialogs) so client and server agree:
# maxlength stops typing/paste in the UI, not a hand-crafted API body — the
# same class of gap closed for the Asset Explorer forms. Oversize is
# REJECTED with a message (FormService's style), never silently truncated.
_NAME_MAXLEN = 200
_DESCRIPTION_MAXLEN = 1000
_SUBJECT_TEMPLATE_MAXLEN = 200
_RECIPIENT_MAXLEN = 254  # per address — RFC 5321 forward-path limit
# follow-up — cap on dashboards per email schedule. Each one is a
# separate precompute (charts executed + link resolved) inside one cron run, so
# an unbounded list is a self-inflicted slow run, not just a long email.
_MAX_DASHBOARDS_PER_SCHEDULE = 10


def _dedup_keep_order(values: list[str]) -> list[str]:
    """De-duplicate preserving first-seen order (the user's check order is the
    order links appear in the email)."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _assemble_from_body(body: dict) -> dict:
    """Map the flat form body into the structured (schedule, delivery, ...) dicts
    the service expects. Returns {error} if a required field is missing.

    v2: the wizard posts connector_id +
    team_esids — previously dropped here, so a wizard-created schedule stored
    NO connector/team list and the Phase D import dispatch never fired for it.
    When team_esids are present they ARE the scope: the v1 picker placeholders
    (org_id="" / team_id="__all__") are blanked so create_schedule back-fills
    the v1 single fields from the first selected team, and a dashboard becomes
    optional (the wizard's "No report (import only)" option).
    """
    name = (body.get("name") or "").strip()
    org_id = (body.get("org_id") or "").strip()
    team_id = (body.get("team_id") or "").strip()
    connector_id = (body.get("connector_id") or "").strip()
    team_esids = [str(t).strip() for t in (body.get("team_esids") or []) if str(t).strip()]
    dashboard_id = (body.get("dashboard_id") or "").strip()
    # follow-up — MULTI-dashboard email schedules. ``dashboard_ids`` is
    # the v2 canonical list; the v1 scalar ``dashboard_id`` is retained and
    # back-filled with the first entry so pre-v2 readers keep working (same
    # dual-write shape as team_id/team_esids). A v1 client that sends only the
    # scalar is normalized up into the list.
    dashboard_ids = _dedup_keep_order([str(d).strip() for d in (body.get("dashboard_ids") or []) if str(d).strip()])
    if not dashboard_ids and dashboard_id:
        dashboard_ids = [dashboard_id]
    if len(dashboard_ids) > _MAX_DASHBOARDS_PER_SCHEDULE:
        return {"error": f"Choose at most {_MAX_DASHBOARDS_PER_SCHEDULE} dashboards."}
    dashboard_id = dashboard_ids[0] if dashboard_ids else ""
    frequency = (body.get("frequency") or "").strip()
    time_str = (body.get("time") or "").strip()
    schedule_type = (body.get("schedule_type") or "import").strip()
    is_email = schedule_type == "email"

    if team_esids:
        org_id, team_id = "", ""

    if not name:
        return {"error": "A report name is required."}
    if len(name) > _NAME_MAXLEN:
        return {"error": f"Report name must be {_NAME_MAXLEN} characters or fewer."}
    description = (body.get("description") or "").strip()
    if len(description) > _DESCRIPTION_MAXLEN:
        return {"error": f"Description must be {_DESCRIPTION_MAXLEN} characters or fewer."}
    # an EMAIL schedule has no connector/team scope; it is owned by its
    # creator (who always sees their own schedules) and just emails a dashboard
    # they can read. So it requires a dashboard + recipients, NOT an org/team.
    # (FLAG: scope is creator-only for now; deriving the dashboard's workspace
    # for workspace-owner visibility is a possible follow-up.)
    if is_email:
        if not dashboard_ids:
            return {"error": "Choose at least one dashboard to email."}
        email_in = body.get("email") or {}
        if not _split_recipients(email_in.get("recipients")):
            return {"error": "Enter at least one recipient."}
    else:
        if not org_id and not team_id and not team_esids:
            return {"error": "Select an organization or team."}
        if not dashboard_id and not connector_id:
            return {"error": "Select a dashboard to report on."}
    if not frequency:
        return {"error": "Choose how often the report runs."}
    if not time_str:
        return {"error": "Choose a run time."}

    schedule = {
        "frequency": frequency,
        "time": time_str,
        "timezone": (body.get("timezone") or "UTC").strip(),
        "date_range_mode": (body.get("date_range_mode") or "rolling_7d").strip(),
    }
    # DOW (weekly/biweekly) / DOM (monthly/quarterly) only when relevant + numeric.
    if frequency in ("weekly", "biweekly") and body.get("day_of_week") is not None:
        try:
            schedule["day_of_week"] = int(body["day_of_week"])
        except (TypeError, ValueError):
            return {"error": "Pick a day of the week."}
    if frequency in ("monthly", "quarterly") and body.get("day_of_month") is not None:
        try:
            schedule["day_of_month"] = int(body["day_of_month"])
        except (TypeError, ValueError):
            return {"error": "Pick a day of the month."}
    # daily-only "days to pull": the previous N complete days ending
    # yesterday. Clamped server-side to 1..7 (don't trust the client); missing /
    # unparseable → 1, the earlier previous-day behavior. Only stored for
    # daily — the other frequencies keep their frequency-derived windows.
    if frequency == "daily":
        schedule["days_to_pull"] = clamp_daily_days(body.get("days_to_pull"))

    email_in = body.get("email") or {}
    subject_template = (email_in.get("subject_template") or "").strip()
    if len(subject_template) > _SUBJECT_TEMPLATE_MAXLEN:
        return {"error": f"Subject template must be {_SUBJECT_TEMPLATE_MAXLEN} characters or fewer."}
    recipients = _split_recipients(email_in.get("recipients"))
    if any(len(r) > _RECIPIENT_MAXLEN for r in recipients):
        return {"error": f"Each recipient address must be {_RECIPIENT_MAXLEN} characters or fewer."}
    delivery = {
        "save_snapshot": bool(body.get("save_snapshot", True)),
        # how many snapshots to retain (clamped 1..100, default 7).
        # Stored regardless of save_snapshot so toggling the box back on keeps the
        # last-entered value; pruning only runs when a snapshot is actually saved.
        "snapshot_keep": clamp_snapshot_keep(body.get("snapshot_keep")),
        "email": {
            "enabled": bool(email_in.get("enabled")),
            "recipients": recipients,
            "format": (email_in.get("format") or "summary_link").strip(),
            "subject_template": subject_template,
        },
    }
    # an email schedule is creator-scoped: leave org/team empty rather
    # than the import default of "__all__" (which would read as org-wide and could
    # surface in a workspace owner's view). An import schedule keeps the legacy
    # "__all__" org-wide default / v2 first-team back-fill.
    if is_email:
        resolved_team_id = ""
    elif team_esids:
        resolved_team_id = team_id
    else:
        resolved_team_id = team_id or "__all__"
    return {
        "name": name,
        "description": description,
        "org_id": "" if is_email else org_id,
        "team_id": resolved_team_id,
        "connector_id": "" if is_email else connector_id,
        # email schedules MAY carry an explicit team selection (the
        # dialog's optional narrowing; validated all-in-creator-scope by the
        # service). Previously dropped here, which forced every email schedule
        # to the all-my-teams clamp.
        "team_esids": team_esids,
        "dashboard_id": dashboard_id,
        "dashboard_ids": dashboard_ids,
        "schedule_type": schedule_type,
        "schedule": schedule,
        "delivery": delivery,
        "query": {},
    }


@report_scheduler_bp.route("/api/<schedule_id>")
def api_get(schedule_id: str):
    """Return one schedule for edit prefill (scope-checked via list intersection)."""
    _require_report_scheduler()
    scope = _visibility_scope()  # narrow read scope
    schedule = ReportScheduleService.get_schedule(schedule_id)
    if not schedule:
        return jsonify({"ok": False, "error": "schedule not found"}), 404
    # Reuse the service's scope predicate: a schedule the caller can't see is 404
    # (don't leak existence). scope intersection OR creator, matching
    # exactly what list_schedules shows them.
    if scope is not None and not ReportScheduleService._scope_or_creator_allows(
        scope, schedule, created_by=_visibility_creator()
    ):
        return jsonify({"ok": False, "error": "schedule not found"}), 404
    return jsonify({"ok": True, "schedule": schedule})


def _recipient_status_after_save(assembled: dict) -> dict:
    """after a schedule is saved, run the recipient policy for the
    author: members pass, external addresses get a verification mail (with a
    24 h resend cooldown) and an audit row, and the author is told which
    addresses delivery will skip until confirmed. Best-effort: a policy error
    never fails the save (the DELIVERY-time check is the enforcement)."""
    email_cfg = (assembled.get("delivery") or {}).get("email") or {}
    recipients = list(email_cfg.get("recipients") or [])
    if not recipients:
        return {"members": [], "verified": [], "unverified": [], "warning": ""}
    try:
        try:
            from web.services.report_recipient_service import ReportRecipientService
        except ImportError:
            from ...services.report_recipient_service import ReportRecipientService
        return ReportRecipientService.on_schedule_saved(recipients, session.get("username", ""))
    except Exception as e:  # noqa: BLE001 — save-time hook is advisory
        logging.warning("report_scheduler: recipient status failed: %s", e)
        return {"members": [], "verified": [], "unverified": [], "warning": ""}


@report_scheduler_bp.route("/report-recipient/verify/<token>", methods=["GET"])
def report_recipient_verify(token: str):
    """public confirmation link for an external report recipient.
    Reached from an inbox with no session (listed in auth's exempt endpoints);
    the signed token is the proof. Renders a minimal template (Jinja
    autoescape) — never string-built HTML."""
    try:
        from web.services.report_recipient_service import ReportRecipientService
    except ImportError:
        from ...services.report_recipient_service import ReportRecipientService
    res = ReportRecipientService.verify(token)
    if res.get("ok"):
        title = "Address confirmed"
        body = f"{res['email']} will now receive the scheduled reports it was added to."
        code = 200
    else:
        title = "Could not confirm"
        body = res.get("error") or "This confirmation link is invalid."
        code = 400
    return render_template("report_recipient_verify.html", title=title, body=body), code


@report_scheduler_bp.route("/api/schedules", methods=["POST"])
def api_create():
    """Create a schedule from the form body. principal = session user (NOT body)."""
    _require_report_scheduler()
    scope = _effective_scope()  # widened save scope (the tree's team universe)
    body = request.get_json(silent=True) or {}
    assembled = _assemble_from_body(body)
    if assembled.get("error"):
        return jsonify({"ok": False, "error": assembled["error"]}), 400
    # bind the selection to the caller's tenant before anything else
    # reads it (the Echo gate below would name a foreign tenant in its 409). The
    # v1 single team/org count when no team_esids were posted.
    v1_team = assembled["team_id"] if assembled["team_id"] != "__all__" else ""
    sel_teams = assembled["team_esids"] or ([v1_team] if v1_team else [])
    denied = _selection_gate(
        team_esids=sel_teams,
        org_ids=[] if sel_teams or not assembled["org_id"] else [assembled["org_id"]],
        dashboard_ids=assembled["dashboard_ids"],
    )
    if denied is not None:
        return denied
    # Echo per-tenant uniqueness (replaces the owner block):
    # reject creating a second Echo report for a tenant that already has one in
    # the same workspace. Runs after assembly so it sees the resolved team_esids.
    gate = _echo_tenant_uniqueness_gate(assembled["connector_id"], assembled["team_esids"])
    if gate is not None:
        return gate
    result = ReportScheduleService.create_schedule(
        principal=session.get("username", ""),
        org_id=assembled["org_id"],
        team_id=assembled["team_id"],
        name=assembled["name"],
        description=assembled["description"],
        dashboard_id=assembled["dashboard_id"],
        dashboard_ids=assembled["dashboard_ids"],
        connector_id=assembled["connector_id"],
        team_esids=assembled["team_esids"],
        query=assembled["query"],
        delivery=assembled["delivery"],
        schedule=assembled["schedule"],
        schedule_type=assembled["schedule_type"],
        scope=scope,
    )
    if not result.get("success"):
        return jsonify({"ok": False, "error": result.get("error")}), 400
    # classify the recipients, mail verification links to external
    # addresses, audit each external, and tell the author who will be skipped.
    rcpt = _recipient_status_after_save(assembled)
    return jsonify(
        {"ok": True, "schedule_id": result.get("esid"), "recipient_status": rcpt, "warning": rcpt.get("warning", "")}
    )


@report_scheduler_bp.route("/api/<schedule_id>", methods=["PUT"])
def api_update(schedule_id: str):
    """Update an existing schedule from the form body (scope + ownership)."""
    _require_report_scheduler()
    body = request.get_json(silent=True) or {}
    existing = ReportScheduleService.get_schedule(schedule_id) or {}
    # #1 — ownership gate: only the creator (or a privileged user) may
    # edit; a workspace-owned schedule is read-only to an org owner.
    existing, denied = _mutate_gate(schedule_id, schedule=existing or None)
    if denied is not None:
        return denied
    scope = _effective_scope()  # write path; _mutate_gate already enforced narrow visibility
    assembled = _assemble_from_body(body)
    if assembled.get("error"):
        return jsonify({"ok": False, "error": assembled["error"]}), 400
    # _mutate_gate authorizes the EXISTING schedule; the NEW selection
    # this PUT writes (team_esids, and dashboard_ids, which are always patched)
    # must be the caller's own too. v1 org_id/team_id are never patched on edit.
    denied = _selection_gate(team_esids=assembled["team_esids"], org_ids=[], dashboard_ids=assembled["dashboard_ids"])
    if denied is not None:
        return denied
    # Echo per-tenant uniqueness on edit. The effective connector/teams
    # are the body's when it carries them, else the stored values (a v1/stale edit
    # doesn't resend connector_id/team_esids and must not wipe them — same rule the
    # patch below uses). Exclude THIS schedule so a no-op save never self-conflicts.
    eff_connector = assembled["connector_id"] or existing.get("connector_id") or ""
    eff_team_esids = assembled["team_esids"] or ReportScheduleService._schedule_team_esids(existing)
    gate = _echo_tenant_uniqueness_gate(eff_connector, eff_team_esids, exclude_schedule_id=schedule_id)
    if gate is not None:
        return gate
    fields = {
        "name": assembled["name"],
        "description": assembled["description"],
        "dashboard_id": assembled["dashboard_id"],
        # follow-up — always patched alongside the scalar so the two
        # can never disagree. _assemble_from_body normalizes a v1 scalar-only
        # body up into the list, so a stale client still writes a coherent pair.
        "dashboard_ids": assembled["dashboard_ids"],
        "query": assembled["query"],
        "delivery": assembled["delivery"],
        "schedule": assembled["schedule"],
    }
    # v2 fields patch only when the body carries them — a v1 edit (or a stale
    # client that doesn't send them) must not wipe a schedule's connector/teams.
    if assembled["connector_id"]:
        fields["connector_id"] = assembled["connector_id"]
    # EMAIL edits always patch team_esids, INCLUDING an empty list:
    # unchecking every team in the dialog means "back to all my teams" (the
    # creator clamp), and the don't-wipe rule below would make that
    # clear impossible. The rule remains for import edits (v1/stale clients).
    # team_id rides along because _schedule_team_esids falls back to the v1
    # single team_id when team_esids is empty — a stale back-filled team_id
    # would otherwise pin a cleared schedule to one team instead of the clamp.
    if assembled["schedule_type"] == "email":
        fields["team_esids"] = assembled["team_esids"]
        fields["team_id"] = assembled["team_esids"][0] if assembled["team_esids"] else ""
    elif assembled["team_esids"]:
        fields["team_esids"] = assembled["team_esids"]
    result = ReportScheduleService.update_schedule(schedule_id, fields=fields, scope=scope)
    if not result.get("success"):
        return jsonify({"ok": False, "error": result.get("error")}), 400
    # prune immediately on save (not only on the next run) so lowering
    # "Number to keep" deletes the now-excess older snapshots right away. Only
    # when snapshots are being saved (save_snapshot on); best-effort, never fails
    # the save. clamp_snapshot_keep matches the run path's bound enforcement.
    if assembled["delivery"].get("save_snapshot", True):
        try:
            keep = clamp_snapshot_keep(assembled["delivery"].get("snapshot_keep"))
            ReportScheduleService.prune_snapshots(_es_client(), schedule_id, keep=keep)
        except Exception as e:  # noqa: BLE001 — pruning never fails the save
            logging.warning("prune-on-save failed for %s: %s", schedule_id, e)
    # follow-up — backfill history records honor snapshot_keep on their
    # OWN count, independent of save_snapshot (a backfill writes a history record
    # even with snapshots off), so lowering the value trims excess backfills too.
    try:
        keep = clamp_snapshot_keep(assembled["delivery"].get("snapshot_keep"))
        ReportScheduleService.prune_snapshots(_es_client(), schedule_id, keep=keep, record_type="backfill")
    except Exception as e:  # noqa: BLE001 — pruning never fails the save
        logging.warning("backfill prune-on-save failed for %s: %s", schedule_id, e)
    rcpt = _recipient_status_after_save(assembled)
    return jsonify({"ok": True, "schedule_id": schedule_id, "recipient_status": rcpt, "warning": rcpt.get("warning", "")})


@report_scheduler_bp.route("/api/<schedule_id>/status", methods=["POST"])
def api_set_status(schedule_id: str):
    """Pause or resume a schedule (scope + ownership)."""
    _require_report_scheduler()
    # #1 — ownership gate (Pause/Resume is a mutation).
    _schedule, denied = _mutate_gate(schedule_id)
    if denied is not None:
        return denied
    scope = _effective_scope()  # write path; _mutate_gate already enforced narrow visibility
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in ("active", "paused"):
        return jsonify({"ok": False, "error": "status must be 'active' or 'paused'"}), 400
    result = ReportScheduleService.set_status(schedule_id, status, scope=scope)
    if not result.get("success"):
        return jsonify({"ok": False, "error": result.get("error")}), 400
    return jsonify({"ok": True, "schedule_id": schedule_id, "status": status})


@report_scheduler_bp.route("/api/<schedule_id>/run-now", methods=["POST"])
def api_run_now(schedule_id: str):
    """Trigger an immediate run (scope + ownership)."""
    _require_report_scheduler()
    schedule = ReportScheduleService.get_schedule(schedule_id) or {}
    # the echo workspace-owner gate is GONE: org owners may
    # Run-now an Echo schedule they own. The per-tenant uniqueness enforced at
    # create/edit (and the run_due batch dedup) keeps only one Echo report per
    # tenant, so a Run-now can't introduce an overlapping tenant pull. The
    # ownership _mutate_gate below still applies (creator/privileged only).
    # #1 — ownership gate: Run-now generates a snapshot + dispatches an
    # import, so it's a mutation (read-only schedules exclude Run now).
    schedule, denied = _mutate_gate(schedule_id, schedule=schedule)
    if denied is not None:
        return denied
    scope = _effective_scope()  # write path; _mutate_gate already enforced narrow visibility
    result = ReportScheduleService.run_now(schedule_id, scope=scope)
    if not result.get("success"):
        return jsonify({"ok": False, "error": result.get("error")}), 400
    return jsonify(
        {
            "ok": True,
            "snapshot_id": result.get("snapshot_id"),
            "import_dispatched": bool(result.get("import_dispatched")),
            "connector_id": result.get("connector_id") or "",
        }
    )


@report_scheduler_bp.route("/api/<schedule_id>/backfill", methods=["POST"])
def api_backfill(schedule_id: str):
    """start a one-time historical backfill for this schedule.

    Backfills the user-selected {from,to} date range (inclusive span ≤ 365 days) of
    the schedule's connector data for its teams, dispatched through the SAME
    scoped-import path a scheduled run uses, so the importer's per-source billing
    gate (``billing_entitlement`` — lapsed owners blocked, over-cap sources skipped)
    applies unchanged. Import-only (no snapshot); progress + Cancel reuse the
    run-state bar.

    Meta (``meta_ads``) is now supported: the service runs it in
    rate-limit bail-out mode (a Meta throttle stops the run gracefully, keeping the
    data written so far) so day-by-day backfills accumulate the full history without
    an approved FB app. ``overwrite`` (Meta only; other connectors have no force
    path) controls whether existing days are re-written or skipped (default skip).

    Gated like Run-now via the ownership ``_mutate_gate``. Range validation runs here
    AND in the service (defence in depth).
    """
    _require_report_scheduler()
    schedule = ReportScheduleService.get_schedule(schedule_id) or {}
    schedule, denied = _mutate_gate(schedule_id, schedule=schedule)
    if denied is not None:
        return denied

    connector_id = (schedule.get("connector_id") or "").strip()
    if not connector_id:
        return jsonify({"ok": False, "error": "This schedule has no connector to backfill."}), 400
    # Meta backfill is now supported (smart backfill: rate-limit bail-out
    # keeps partial progress, so day-by-day backfills can be strung together without
    # an approved FB app). No connector is rejected here anymore.

    data = request.get_json(silent=True) or {}
    # the dialog now sends a from/to range (all connectors) + an overwrite
    # flag (Meta only; other connectors have no force path and ignore it). The full
    # range validation lives in the service (defence-in-depth); the route does the
    # cheap shape checks so a bad request fails fast with a clear message.
    date_range = data.get("date_range") or {}
    frm = str((date_range or {}).get("from") or "").strip()
    to = str((date_range or {}).get("to") or "").strip()
    if not frm or not to:
        return jsonify({"ok": False, "error": "Select a from and to date to backfill."}), 400
    try:
        d_from = datetime.strptime(frm, "%Y-%m-%d").date()
        d_to = datetime.strptime(to, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"ok": False, "error": "Dates must be in YYYY-MM-DD format."}), 400
    if d_from > d_to:
        return jsonify({"ok": False, "error": "The from date must be on or before the to date."}), 400
    span_days = (d_to - d_from).days + 1
    if span_days > ReportScheduleService.BACKFILL_MAX_DAYS:
        return (
            jsonify(
                {"ok": False, "error": f"The date range must be {ReportScheduleService.BACKFILL_MAX_DAYS} days or fewer."}
            ),
            400,
        )
    # Overwrite is meaningful only for Meta (the sole connector with a force-refetch
    # path); ignore it for others so the checkbox can never imply an effect it lacks.
    overwrite = bool(data.get("overwrite")) if connector_id == "meta_ads" else False

    # NO billing block here. Backfill is never refused for an
    # import-blocked plan: the backfill dispatches like a normal run, and the
    # importer's per-source gate (billing_entitlement.source_billing_block) skips
    # only the sources billing says to skip — exactly the parity a scheduled run
    # has. Blocking the whole backfill upfront would over-block (a partially-paid
    # workspace's covered sources would be denied too).
    scope = _effective_scope()  # write path; _mutate_gate already enforced narrow visibility
    requested_by = session.get("username", "") or ""
    result = ReportScheduleService.backfill_now(
        schedule_id,
        {"from": frm, "to": to},
        overwrite=overwrite,
        requested_by=requested_by,
        scope=scope,
    )
    if not result.get("success"):
        return jsonify({"ok": False, "error": result.get("error")}), 400
    return jsonify(
        {
            "ok": True,
            "schedule_id": schedule_id,
            "progress_run_id": result.get("progress_run_id") or "",
            "import_dispatched": bool(result.get("import_dispatched")),
            "connector_id": result.get("connector_id") or "",
            "window": result.get("window") or {},
            "overwrite": bool(result.get("overwrite")),
        }
    )


@report_scheduler_bp.route("/api/<schedule_id>/cancel", methods=["POST"])
def api_cancel_run(schedule_id: str):
    """cancel a schedule's in-flight run and kill its import subtree.

    Belt-and-suspenders (service ReportScheduleService.cancel_run): persists a
    cancel flag the importer's checkpoints poll AND ``os.killpg``s the
    persisted process group so a mid-API-batch importer dies even before its next
    checkpoint. A kill that can't be CONFIRMED dead comes back ``kill_confirmed:
    false`` — surfaced to the client, not hidden behind a clean "cancelled".

    Gated like Run-now via the ownership ``_mutate_gate``. dropped the
     echo workspace-owner gate: an org owner may cancel an Echo run they
    own. An echo cancel still reaps the SHARED tenant-wide pull (``shared_echo_pull``
    below documents that to the UI), but per-tenant uniqueness means an owner's
    Echo schedule is the only one on its tenant, so cancelling it is theirs to do.
    """
    _require_report_scheduler()
    schedule = ReportScheduleService.get_schedule(schedule_id) or {}
    # #1 — ownership gate (cancel mutates the run state + kills processes).
    schedule, denied = _mutate_gate(schedule_id, schedule=schedule)
    if denied is not None:
        return denied
    scope = _effective_scope()  # write path; _mutate_gate already enforced narrow visibility
    is_echo = (schedule.get("connector_id") or "") == "echo"
    result = ReportScheduleService.cancel_run(schedule_id, scope=scope)
    if not result.get("success"):
        return jsonify({"ok": False, "error": result.get("error")}), 400
    return jsonify(
        {
            "ok": True,
            "schedule_id": schedule_id,
            # kill_confirmed: the group was verified dead. kill_reached: we at least
            # SIGKILL'd it (it's going down) AND cleaned up its lock/progress doc — a
            # reached-but-not-yet-reaped kill is NOT degraded. The client shows the
            # degraded "will stop at its next checkpoint" note ONLY when NOT reached
            # (killpg couldn't signal it at all). Driven off the persisted run_state
            # on reload too, so the note survives navigation.
            "kill_confirmed": bool(result.get("kill_confirmed")),
            "kill_reached": bool(result.get("kill_reached")),
            "killpg_supported": bool(result.get("killpg_supported")),
            # echo's pull is tenant-wide and shared across every due echo
            # schedule; flag it so the UI can document that cancel stopped them all.
            "shared_echo_pull": is_echo,
        }
    )


@report_scheduler_bp.route("/api/<schedule_id>/import-status")
def api_import_status(schedule_id: str):
    """Live status of the background import for this schedule's last Run-now.

    Backs the Run-now feedback poll: the snapshot returns synchronously but the
    connector import is a fire-and-forget subprocess.

    Two signal tiers:

    * PER-RUN progress. When the last run persisted a
      ``progress_run_id`` (progress-capable connectors, FB today), return the live
      ``de_import_progress`` doc: ``progress: {status, phase, percent,
      days_fetched, days_total}``. This is a true per-run signal — it resolves on
      the run's own terminal status, not a shared lock — and drives the bar's
      real percentage + stage message. ``running`` is derived from it
      (status in pending/running) for back-compat with the existing poll.
    * the cluster import lock (per-CONNECTOR, not per-run, so a
      concurrent daily-cron import of the same connector reads as running too).
      Used when there's no progress doc. ``trackable=False`` means the connector's
      importer takes no lock (youtube) or has no scoped import — the client shows a
      one-shot "started in the background" instead of polling.
    """
    _require_report_scheduler()
    scope = _visibility_scope()  # resolved org/team scope
    schedule = ReportScheduleService.get_schedule(schedule_id)
    if not schedule:
        return jsonify({"ok": False, "error": "schedule not found"}), 404
    if scope is not None and not ReportScheduleService._scope_or_creator_allows(
        scope, schedule, created_by=_visibility_creator()
    ):
        return jsonify({"ok": False, "error": "schedule not found"}), 404

    # prefer the per-run progress doc when this run wired one up.
    run_state = schedule.get("last_run_state") or {}
    progress_run_id = run_state.get("progress_run_id") or ""

    # finalize a DEFERRED snapshot build inline, right here on the poll.
    # leaves a run_now in phase=importing until _sweep_deferred_builds runs at
    # the next cron sweep — so a schedule whose import already finished sticks on
    # "Importing fresh data…" for hours. The building screen polls this endpoint every
    # ~3s; when the polled schedule is genuinely deferred (phase=importing + a terminal
    # progress_run_id), build the snapshot now (idempotent, race-safe with the cron sweep;
    # the helper re-reads fresh state and no-ops if the run already moved on). Cheap guard
    # first so a normal in-flight poll never touches the build path.
    if progress_run_id and run_state.get("phase") == RUN_PHASE_IMPORTING:
        ReportScheduleService.finalize_deferred_build_if_ready(_es_client(), schedule_id)

    if progress_run_id:
        progress = ReportScheduleService.import_progress(progress_run_id)
        if progress is not None:
            terminal = progress.get("status") in ("succeeded", "failed", "cancelled")
            return jsonify(
                {
                    "ok": True,
                    "trackable": True,
                    "running": not terminal,
                    "progress": progress,
                }
            )
        # Doc not found yet (importer hasn't written it) — fall through to the
        # lock so the bar still shows "running" during the brief startup window.

    lock_key = ReportScheduleService.import_lock_connector(schedule.get("connector_id") or "")
    if not lock_key:
        return jsonify({"ok": True, "trackable": False, "running": False})
    from lib.import_lock_service import ImportLockService

    return jsonify({"ok": True, "trackable": True, "running": bool(ImportLockService.is_held(lock_key))})


@report_scheduler_bp.route("/api/<schedule_id>", methods=["DELETE"])
def api_delete(schedule_id: str):
    """Delete a schedule (scope + ownership)."""
    _require_report_scheduler()
    # #1 — ownership gate: only the creator (or a privileged user) deletes.
    _schedule, denied = _mutate_gate(schedule_id)
    if denied is not None:
        return denied
    scope = _effective_scope()  # write path; _mutate_gate already enforced narrow visibility
    result = ReportScheduleService.delete_schedule(schedule_id, scope=scope)
    if not result.get("success"):
        return jsonify({"ok": False, "error": result.get("error")}), 400
    return jsonify({"ok": True})


# ── Snapshots: history / view / export (P4c) ─────────────────────────────────


def _scoped_snapshot_or_none(snapshot_id: str, scope):
    """Fetch a snapshot and apply the scope gate. Returns the doc, or None if it
    doesn't exist OR the caller can't see it (caller maps None → 404; we don't
    distinguish, so existence isn't leaked across tenants)."""
    snap = ReportScheduleService.get_snapshot(snapshot_id)
    if not snap:
        return None
    if scope is None:
        return snap
    if ReportScheduleService._scope_allows(scope, snap.get("org_id", ""), snap.get("team_id", "")):
        return snap
    # email-schedule snapshots are creator-scoped (empty org/team),
    # so the org/team intersection above can never match them. Fall back to
    # the parent schedule's visibility predicate (scope ∩ schedule OR creator)
    # — the same gate the schedule list/get/mutate routes apply, so a snapshot
    # is visible exactly when its schedule is.
    schedule = ReportScheduleService.get_schedule(snap.get("schedule_id") or "")
    if schedule and ReportScheduleService._scope_or_creator_allows(scope, schedule, created_by=_visibility_creator()):
        return snap
    return None


def _snapshot_sections(snap: dict) -> list:
    """Map a snapshot's summary + tables into the {query, narrative, table}
    section shape report_export_service consumes (mirrors the P3 email adapter)."""
    data = snap.get("data") or {}
    sections = []
    summary = data.get("summary_metrics") or {}
    if summary:
        sections.append(
            {
                "query": "Summary",
                "narrative": "",
                "table": {"headers": ["Metric", "Value"], "rows": [[k, v] for k, v in summary.items()]},
            }
        )
    for tbl in data.get("tables") or []:
        result = tbl.get("result") or {}
        sections.append(
            {
                "query": tbl.get("title") or tbl.get("chart_id") or "Table",
                "narrative": "",
                "table": {"headers": result.get("columns") or [], "rows": result.get("rows") or []},
            }
        )
    return sections


@report_scheduler_bp.route("/api/<schedule_id>/snapshots")
def api_list_snapshots(schedule_id: str):
    """List a schedule's snapshots (history), scope-filtered, optional date window."""
    _require_report_scheduler()
    scope = _visibility_scope()  # narrow read scope
    snapshots = ReportScheduleService.list_snapshots(
        scope=scope,
        schedule_id=schedule_id,
        date_from=(request.args.get("date_from") or "").strip() or None,
        date_to=(request.args.get("date_to") or "").strip() or None,
    )
    # Trim the heavy ``data`` blob from the list response — history rows only
    # need metadata; the View fetches the full doc. #7: also surface a
    # per-run error summary (count + the chart/table errors + precompute-skipped
    # reason) so the History modal can badge + expand failures without fetching
    # the full snapshot.
    rows = []
    for s in snapshots:
        data = s.get("data") or {}
        # a backfill record is a one-time historical-pull RUN record
        # (record_type="backfill"), not a viewable report: it carries no ``data``
        # blob, so skip the error/chart summary and surface only its backfill
        # metadata for the History modal's labeled row.
        if s.get("record_type") == "backfill":
            rows.append(
                {
                    "snapshot_id": s.get("snapshot_id"),
                    "schedule_id": s.get("schedule_id"),
                    "generated_at": s.get("generated_at"),
                    "date_range": s.get("date_range") or {},
                    "record_type": "backfill",
                    "backfill": s.get("backfill") or {},
                }
            )
            continue
        err = ReportScheduleService.summarize_snapshot_errors(data)
        rows.append(
            {
                "snapshot_id": s.get("snapshot_id"),
                "schedule_id": s.get("schedule_id"),
                "generated_at": s.get("generated_at"),
                "date_range": s.get("date_range") or data.get("date_range") or {},
                "size_bytes": s.get("size_bytes"),
                "chart_count": len(data.get("charts") or []),
                "table_count": len(data.get("tables") or []),
                "error_count": err["count"],
                "errors": err["errors"],
                "precompute_skipped": err["skipped"],
                # benign note (e.g. import-only schedule, no dashboard);
                # rendered as a grey info line, not a red error.
                "info_note": err["info"],
            }
        )
    return jsonify({"ok": True, "snapshots": rows})


@report_scheduler_bp.route("/api/snapshots/<snapshot_id>")
def api_get_snapshot(snapshot_id: str):
    """Full snapshot doc (for the View renderer), scope-gated (404 otherwise)."""
    _require_report_scheduler()
    snap = _scoped_snapshot_or_none(snapshot_id, _visibility_scope())  # narrow read scope
    if not snap:
        return jsonify({"ok": False, "error": "snapshot not found"}), 404
    return jsonify({"ok": True, "snapshot": snap})


def _snapshot_delete_gate(snap: dict):
    """authorize deleting a single snapshot. Returns
    ``(None, (json, status))`` to refuse, or ``(snap, None)`` to proceed.

    A snapshot has no ``created_by`` of its own — ownership lives on its parent
    schedule — so the delete is gated by whether the caller could MUTATE that
    schedule (creator or privileged), the same rule that already governs
    Edit/Pause/Delete/Run-now (``_mutate_gate``). This is intentionally STRICTER
    than the read/visibility gate the View/CSV/Excel actions use: a read-only
    viewer (an org owner who can see a workspace-owned schedule's history but not
    change it) must NOT be able to delete its snapshots (decision 2026-06-22).

    History outlives its schedule (delete_schedule retains snapshots), so a
    snapshot may be ORPHANED. With no parent schedule there's no creator to
    check, so an orphan is privileged-only: a non-privileged caller can't delete
    a run whose ownership can no longer be established.
    """
    schedule_id = (snap or {}).get("schedule_id") or ""
    schedule = ReportScheduleService.get_schedule(schedule_id) if schedule_id else None
    if schedule:
        # Reuse the exact schedule mutate gate: visibility (narrow read scope or
        # creator) THEN ownership (creator/privileged). A crafted DELETE against a
        # workspace-owned schedule's snapshot is refused here, not just hidden.
        _sched, denied = _mutate_gate(schedule_id, schedule=schedule)
        if denied is not None:
            return None, denied
        return snap, None
    # Orphaned snapshot (parent schedule already deleted): privileged-only.
    if not _current_user_is_privileged():
        return None, (
            jsonify({"ok": False, "error": "This snapshot is workspace-owned; you can view it but not delete it."}),
            403,
        )
    return snap, None


@report_scheduler_bp.route("/api/snapshots/<snapshot_id>", methods=["DELETE"])
def api_delete_snapshot(snapshot_id: str):
    """delete a single snapshot/run from a schedule's history.

    Two-gate flow: the caller must be able to SEE the snapshot (narrow read
    scope, 404 otherwise so existence isn't leaked) AND be able to MUTATE its
    parent schedule (_snapshot_delete_gate; 403 for a read-only viewer). On
    success the snapshot doc — the sole store of the run's precompute and the
    source its CSV/Excel exports are generated from — is hard-deleted, so the run
    no longer appears in /snapshots and is no longer viewable/exportable.
    """
    _require_report_scheduler()
    snap = _scoped_snapshot_or_none(snapshot_id, _visibility_scope())  # narrow read scope
    if not snap:
        return jsonify({"ok": False, "error": "snapshot not found"}), 404
    _snap, denied = _snapshot_delete_gate(snap)
    if denied is not None:
        return denied
    result = ReportScheduleService.delete_snapshot(snapshot_id)
    if not result.get("success"):
        return jsonify({"ok": False, "error": result.get("error")}), 400
    return jsonify({"ok": True, "snapshot_id": snapshot_id})


@report_scheduler_bp.route("/snapshots/<snapshot_id>")
def snapshot_view(snapshot_id: str):
    """Render the snapshot View page (the JS fetches the JSON + draws charts)."""
    _require_report_scheduler()
    snap = _scoped_snapshot_or_none(snapshot_id, _visibility_scope())  # narrow read scope
    if not snap:
        abort(404)
    return render_template("report_scheduler_snapshot.html", snapshot_id=snapshot_id)


@report_scheduler_bp.route("/api/snapshots/<snapshot_id>/export")
def api_export_snapshot(snapshot_id: str):
    """Download a snapshot as csv or xlsx (?format=csv|xlsx). Scope-gated."""
    _require_report_scheduler()
    snap = _scoped_snapshot_or_none(snapshot_id, _visibility_scope())  # narrow read scope
    if not snap:
        abort(404)
    fmt = (request.args.get("format") or "csv").strip().lower()
    sections = _snapshot_sections(snap)

    exp = _import_first(
        "asset_management.web.services.report_export_service",
        "web.services.report_export_service",
        "services.report_export_service",
    )
    schedule_id = snap.get("schedule_id") or "report"
    dr = snap.get("date_range") or {}
    title = f"Report {schedule_id} {dr.get('from', '')}_{dr.get('to', '')}".strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", title) or "report"

    if fmt == "xlsx":
        buf = exp.export_excel(title, sections)
        return Response(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{safe}.xlsx"'},
        )
    # default csv
    buf = exp.export_csv(title, sections)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'},
    )


def _import_first(*candidates):
    """Three-root resolver for the export service (container / app / pytest).
    Same pattern as report_schedule_service._import_first — needed because the
    export module's home differs by run context."""
    import importlib

    last_err = None
    for name in candidates:
        try:
            return importlib.import_module(name)
        except ImportError as e:
            last_err = e
    raise last_err
