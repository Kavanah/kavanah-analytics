"""
Data Connectors Blueprint Routes

Team-centric view of which data connectors are configured for each team.
Read-only — all writes still go through the existing /asset/<type>/... CRUD
endpoints. See connector_status_service for the aggregation logic.
"""

import logging

from flask import abort, render_template, request, jsonify, session

from . import data_connectors_bp

try:
    from web.services.connector_catalog_service import ConnectorCatalogService
    from web.services.connector_status_service import (
        ConnectorDisconnectError,
        disconnect_connector,
        get_team_connectors,
        list_teams,
        remove_connector_account,
    )
    from web.services.connector_wizard_service import (
        WizardError,
        configure_bitly,
        configure_chatwoot,
        google_ads_campaign_rules,
        list_chatwoot_inboxes,
        preview_google_ads_campaign_rules,
        set_google_ads_campaign_rules,
        set_chatwoot_inboxes,
        configure_tiktok,
        tiktok_connection_status,
        tiktok_oauth_start,
        configure_disciple_tools,
        configure_echo,
        configure_ga4_property,
        configure_ga4_oauth,
        configure_google_ads_customer,
        configure_youtube,
        fb_app_selection_mode,
        fb_connection_status,
        fb_create_pending_connection,
        fb_finish_data_connectors_wizard,
        fb_link_pages_to_team,
        fb_list_ad_accounts_for_connection,
        fb_list_pages_for_connection,
        fb_list_pages_via_graph,
        fb_pick_or_create_account,
        ga4_account_status,
        ga4_list_properties,
        ga4_oauth_pick_or_create_account,
        google_ads_connection_status,
        google_ads_discovery_confirm,
        google_ads_discovery_list,
        google_ads_discovery_start,
        google_ads_oauth_start,
        google_ads_validate_oauth_customer,
        list_assets_for_picker,
        youtube_account_status,
        youtube_list_channels,
        youtube_oauth_pick_or_create_account,
    )
    from web.services.role_filter_service import RoleFilterService
    from web.services.role_permissions_service import RolePermissionsService
except ImportError:
    from ...services.connector_catalog_service import ConnectorCatalogService
    from ...services.connector_status_service import (
        ConnectorDisconnectError,
        disconnect_connector,
        get_team_connectors,
        list_teams,
        remove_connector_account,
    )
    from ...services.connector_wizard_service import (
        WizardError,
        configure_bitly,
        configure_chatwoot,
        google_ads_campaign_rules,
        list_chatwoot_inboxes,
        preview_google_ads_campaign_rules,
        set_google_ads_campaign_rules,
        set_chatwoot_inboxes,
        configure_tiktok,
        tiktok_connection_status,
        tiktok_oauth_start,
        configure_disciple_tools,
        configure_echo,
        configure_ga4_property,
        configure_ga4_oauth,
        configure_google_ads_customer,
        configure_youtube,
        fb_app_selection_mode,
        fb_connection_status,
        fb_create_pending_connection,
        fb_finish_data_connectors_wizard,
        fb_link_pages_to_team,
        fb_list_ad_accounts_for_connection,
        fb_list_pages_for_connection,
        fb_list_pages_via_graph,
        fb_pick_or_create_account,
        ga4_account_status,
        ga4_list_properties,
        ga4_oauth_pick_or_create_account,
        google_ads_connection_status,
        google_ads_discovery_confirm,
        google_ads_discovery_list,
        google_ads_discovery_start,
        google_ads_oauth_start,
        google_ads_validate_oauth_customer,
        list_assets_for_picker,
        youtube_account_status,
        youtube_list_channels,
        youtube_oauth_pick_or_create_account,
    )
    from ...services.role_filter_service import RoleFilterService
    from ...services.role_permissions_service import RolePermissionsService


# connector_id → in-page wizard key.
#   * meta_ads: 5-step FB OAuth modal (custom controller).
#   * echo: existing subdomain+api_key pick-or-create modal.
#   * chatwoot: account-id + API-token pick-or-create modal (mirrors echo).
#     Un-gated 2026-06-05 — the backend (configure_chatwoot + importer +
#     cron) already shipped; it was a mistake to leave the tile coming_soon.
# * google_ads: v2 (2026-05-26) — pick an existing
#     de_asset_google_ads_customers row, write the link + ds_*
#     flag + integration_status to the team doc, then run the
#     tokenless connection finalize. Replaces the v1 one-shot
#     POST that failed when the team wasn't pre-configured.
# * google_analytics_4: same v2 pattern. GA4 is multi-
#     asset, so the wizard appends to team_ga4_property_esids
#     (preserving any already linked); users add multiple
#     properties by reopening the wizard, or use the team-edit
#     page's multi-select picker for a bulk replace.
# Everything else renders disabled with a hover hint:
#   * Coming-soon catalog tiles: no integration anywhere.
# * youtube: custom OAuth + channel-multiselect flow
#     (Account → Authorize → Channels → Confirm), structurally like the
#     Facebook custom flow. Wireable for any mc_data_connectors user. (The
# original superuser gate was lifted 2026-05-27 at a teammate's
# request — see index + DECISIONS.md. OAuth verification is
#     still unfinished, so the ~100-grant cap + 7-day refresh-token expiry
#     on the unverified client is now an accepted broadly-applicable risk.)
# * disciple_tools: (2026-06-11) — form-only modal (DT site
#     domain + dashboard domain + Site-to-Site Link token). Live-verifies
#     the credential against the DT site, then writes the de_connection +
#     dt_site rows the pull importer reads.
_TILE_TO_WIZARD_KEY: dict[str, str] = {
    "meta_ads": "facebook",
    "echo": "echo",
    "chatwoot": "chatwoot",
    "google_ads": "google_ads",
    "google_analytics_4": "google_analytics_4",
    "youtube": "youtube",
    "disciple_tools": "disciple_tools",
    # Phase 3 — form-only modal (API token + tag filter). Live-
    # verifies the token against Bitly /v4/user, then writes the encrypted
    # de_connection + bitly_account rows the pull importer reads.
    "bitly": "bitly",
    # TikTok organic: Login Kit popup (oauth-start / oauth-status)
    # then configure links the authorized account to the team.
    "tiktok": "tiktok",
}


# catalog tile connector_id → connector_status_service status
# ``key``. The status payload (api_team_connectors) keys each connector by
# its status ``key``; the tile grid keys by catalog ``connector_id``. The two
# vocabularies agree for every connector except GA4, where the tile id is
# ``google_analytics_4`` but the status key is ``google_analytics`` (no _4
# suffix — see connector_status_service.CONNECTORS). The template inverts this
# map to overlay a Configured / Needs-attention pill on the matching tile when
# a team is selected. Tiles with no entry fall back to identity (tile id ==
# status key), which covers meta_ads / google_ads / echo and any future
# connector whose two ids match.
_TILE_TO_STATUS_KEY: dict[str, str] = {
    "google_analytics_4": "google_analytics",
}


# Asset types the wizard's "pick existing" dropdowns are allowed to enumerate.
# Whitelisted to avoid leaking arbitrary asset indices via the API.
# v2 (2026-05-26) added google_ads_customers + ga_properties so the
# new in-page wizards can list them.
_PICKER_ALLOWED_TYPES = frozenset(
    {
        "echo_tenants",
        "chatwoot_accounts",
        "facebook_accounts",
        # Phase 3 (2026-05-28) removed facebook_apps + facebook_app_users
        # from this picker allowlist. The Path A wizard no longer enumerates
        # them (the unified Meta app is the only one used, and the App-User
        # OAuth identity has been replaced by the de_connection model). The
        # underlying ASSET_CONFIGS for those types stay in place so the Asset
        # Explorer "Add" / "Edit" forms still work for superusers who need to
        # manage the legacy records that remain on prod.
        "facebook_pages",
        "google_ads_customers",
        "ga_properties",
        # the GA4 wizard's "use existing account" dropdown
        # (data_connectors.html requests wizard/options/ga4_accounts). This
        # was missing from the allowlist, so the route 400'd ("Asset type not
        # allowed") and the dropdown was always empty even though
        # de_asset_ga4_accounts held docs. Sibling to ga_properties /
        # youtube_accounts already here.
        "ga4_accounts",
        # the YouTube wizard's "pick existing account" dropdown.
        # youtube_channels stays out: channels are enumerated live from the
        # Google API (youtube_list_channels), never picked from a dropdown.
        "youtube_accounts",
        # Phase 4 — the team page's "Add ad-account link" dropdown
        # (Asset Explorer Ad accounts panel). Workspace-narrowed like every
        # other picker type; the link-add endpoint re-validates the esid.
        "facebook_ad_accounts",
    }
)


def _require_data_connectors() -> None:
    """Abort with 403 unless the current user has mc_data_connectors.

    wizard-signup users hold ``workspace_owner`` /
    ``workspace_admin`` as a System 2 grant rather than an ES Security
    role, so ``session["roles"]`` is ``[]`` and the legacy resolver
    path returns all-False. Passing ``username`` opts this route into
    the System 2 bridge in ``get_merged_permissions`` — workspace-tier
    grants get the minimum bundle required to reach this page.
    """
    perms = RolePermissionsService.get_merged_permissions(
        session.get("roles", []),
        username=session.get("username"),
    )
    if not perms.get("mc_data_connectors"):
        abort(403, description="This page requires the Data Connectors permission.")


def _effective_allowed_workspaces():
    """the workspace allow-list this page's team / asset SELECTION
    surfaces filter by (the team dropdown, the per-team wizard authz re-check,
    the pick-existing asset pickers).

    The bug: every surface threaded ``RoleFilterService.get_allowed_workspaces()``
    into ``list_teams`` / ``get_team_connectors`` / ``list_assets_for_picker``.
    For a SUPERUSER that resolver returns the broad grant+membership+inferred
    union (effectively ``None`` / every workspace), so the team dropdown listed
    every team in every workspace.

    Fix (user decision 2026-06-19, mirrors the Report Scheduler blueprint's
    helper of the same name): for a PRIVILEGED caller substitute the STRICT
    primary-workspace + explicit-grant set —
    ``WorkspaceMembershipService.primary_and_grant_workspaces`` (primary
    workspace + explicit workspace data-grants only). TIGHTER than the Data
    Inventory ``data_grant_and_primary_workspaces``, which also walks org/team
    scope up to its workspaces (Leg 3) and so drags in every workspace a
    superuser owns scattered teams in. A non-privileged caller keeps
    ``get_allowed_workspaces()`` unchanged (already bounded to their own scope;
    the ``get_allowed_teams_for_org_users`` title narrowing in ``index``
    still applies on top of it).

    Contract mirrors ``get_allowed_workspaces`` (``None``/``[]``/``[esids]``) so
    it drops into the existing callers — except a privileged caller now gets a
    concrete list (primary + grants), never ``None`` (bound to their workspace,
    not see-all). ``[]`` is deny-all. Fail-soft: any error degrades to the
    legacy ``get_allowed_workspaces()`` so a transient ES blip can't lock a
    superuser out of the page.
    """
    try:
        if not RoleFilterService.current_user_is_privileged():
            return RoleFilterService.get_allowed_workspaces()
    except Exception:  # noqa: BLE001 — unresolvable privilege check → treat as non-privileged
        return RoleFilterService.get_allowed_workspaces()
    try:
        try:
            from web.services.workspace_membership_service import WorkspaceMembershipService
        except ImportError:
            from ...services.workspace_membership_service import WorkspaceMembershipService

        # STRICT primary + explicit-grant set (NOT
        # data_grant_and_primary_workspaces, whose org/team-scope walk drags in
        # every workspace a superuser owns scattered teams in). See the
        # report_scheduler sibling helper for the full rationale.
        return WorkspaceMembershipService.primary_and_grant_workspaces(session.get("username", "") or "")
    except Exception as e:  # noqa: BLE001 — degrade to the legacy allow-list, never crash a selection surface
        logging.warning("data_connectors._effective_allowed_workspaces: %s", e)
        return RoleFilterService.get_allowed_workspaces()


# ── Blueprint error handlers ─────────────────────────────────────────────────


@data_connectors_bp.errorhandler(403)
def _handle_403(e):
    if request.path.startswith("/data-connectors/api/"):
        return jsonify({"success": False, "error": "Access denied. Data Connectors permission required."}), 403
    return e


# ── Page route ────────────────────────────────────────────────────────────────


@data_connectors_bp.route("/")
def index() -> str:
    _require_data_connectors()
    # scope the team dropdown to the caller's allowed workspaces.
    # Superuser / asset_admin (RoleFilterService.get_allowed_workspaces() is
    # None) see every team; scoped users see only teams in workspaces they
    # have membership / grant in. Mirrors the Asset Explorer
    # pattern.
    # narrow a PRIVILEGED caller from the broad get_allowed_workspaces
    # union (every workspace in the cluster) to their primary workspace + grants
    # via _effective_allowed_workspaces, so the team dropdown no longer lists
    # every workspace's teams. Non-privileged callers are unchanged (the
    # title narrowing below still applies on top).
    teams = list_teams(allowed_workspaces=_effective_allowed_workspaces())
    # workspace narrowing alone leaks every team in the workspace
    # to an org-/team-scoped user (the org owner sees ALL workspace teams,
    # not just their org's). Narrow further to the caller's allowed team
    # titles, mirroring the dashboard-visibility ``org-team-options`` picker.
    #
    # Gate on ``current_user_is_privileged()`` — the scope-restricted-vs-admin
    # discriminator — NOT on the allowed-teams value. A bare superuser /
    # asset_admin with no resolvable grants (incl. a transient ES-unavailable
    # path) returns ``[]`` from ``get_allowed_teams_for_org_users``, IDENTICAL
    # to a genuinely-empty scoped user — so a value-only gate cannot tell them
    # apart. Narrowing an admin to ``[]`` empties ``teams``, which collapses
    # this page to the "No teams found" empty state and drops the whole tile
    # grid (the regression that got the first attempt
    # reverted). Privileged users (superuser / asset_admin / workspace
    # owner+admin) skip narrowing: ``list_teams`` has ALREADY workspace-
    # narrowed their list, so a workspace owner still sees only their
    # workspace's teams (AC: workspace owner sees all teams in their
    # workspace), while superuser / asset_admin see everything. The call is
    # wrapped fail-safe: ``get_allowed_teams_for_org_users`` can raise /
    # redirect under a no-ES edge path and previously 500'd the render — on
    # error keep the workspace-narrowed list.
    if not RoleFilterService.current_user_is_privileged():
        try:
            allowed_teams = RoleFilterService.get_allowed_teams_for_org_users()
            teams = [t for t in teams if t.get("title") in set(allowed_teams or [])]
        except Exception:  # noqa: BLE001 — fail-safe: keep the workspace-narrowed list
            pass
    # tile grid mirroring onboarding step-6. Pass the catalog
    # so the template renders the same tiles users saw during signup
    # ("Add another data source" landing here follow-up).
    # ``wireable_connector_ids`` is the v1 click-enabled subset; tiles
    # outside it render disabled with an "available in onboarding
    # wizard" hint (see _TILE_TO_WIZARD_KEY for rationale).
    tiles = ConnectorCatalogService.all_tiles()
    # v2 (2026-05-26) — all wireable tiles are now modal-wizard
    # tiles (Meta Ads, Echo, Google Ads, GA4). The one-shot tokenless
    # path was replaced by the configure-then-finalize wizards that
    # write team-doc fields BEFORE running the tokenless finalize, so
    # a user clicking Google Ads / GA4 doesn't need to pre-configure
    # the team on a separate page.
    # YouTube wizard. The original superuser-only gate (which
    # popped ``youtube`` from the wireable set for non-superusers) was lifted
    # 2026-05-27 at the team's request: YouTube is now wireable for any
    # mc_data_connectors user, same as the other tiles. NOTE —
    # (Google OAuth verification) is still unfinished, so the unverified
    # client's ~100-grant cap + 7-day refresh-token expiry now applies to
    # every user who onboards a channel here, not just operators. See
    # DECISIONS.md 2026-05-27 for the accepted-risk rationale.
    tile_to_wizard_key = dict(_TILE_TO_WIZARD_KEY)
    wireable_connector_ids = list(tile_to_wizard_key.keys())
    # tile-id → status-key map drives the per-tile Configured /
    # Needs-attention pill overlay (the template inverts it to status-key →
    # tile-id at paint time). See _TILE_TO_STATUS_KEY for the GA4 mismatch.
    tile_to_status_key = dict(_TILE_TO_STATUS_KEY)
    return render_template(
        "data_connectors.html",
        teams=teams,
        tiles=tiles,
        wireable_connector_ids=wireable_connector_ids,
        tile_to_wizard_key=tile_to_wizard_key,
        tile_to_status_key=tile_to_status_key,
    )


# ── API routes ────────────────────────────────────────────────────────────────


@data_connectors_bp.route("/api/teams/<team_esid>/connectors")
def api_team_connectors(team_esid: str):
    """Return the connector status payload for one team."""
    _require_data_connectors()
    # re-check the requested team's workspace against the
    # caller's allowed set. A scoped user asking for another tenant's
    # team gets the same 404 as a missing team — don't leak existence.
    # for a privileged caller the allowed set is narrowed to their
    # primary workspace + grants, matching the dropdown the team was selected
    # from (a team in a foreign workspace 404s, as it now does in the list).
    payload = get_team_connectors(
        team_esid,
        allowed_workspaces=_effective_allowed_workspaces(),
    )
    if payload is None:
        return jsonify({"success": False, "error": "Team not found"}), 404
    _annotate_google_ads_can_manage(payload)
    return jsonify({"success": True, **payload})


def _annotate_google_ads_can_manage(payload: dict) -> None:
    """stamp ``can_manage`` on the Google Ads connector so the
    dialog can hide its "Manage connection" button for a viewer who can SEE the
    connection (via workspace scope) but isn't allowed onto the principal-gated
    connection-detail page — where the button would 404.

    Google Ads is the odd one out: its connected-state summary reads linked
    accounts straight from the status payload (workspace-scoped), so an in-scope
    admin already sees the accounts and can Remove/Reconnect (both
    workspace-gated). The only dead-end is Manage, which links to the
    connection-detail page gated by ``_viewer_can_see_connection``. Mirror that
    exact gate here so the button shows iff Manage would actually resolve. The
    other three connectors (YouTube/GA4/Meta) fetch a connection-assets endpoint
    that already returns ``admin_view`` for this case, so they don't need this.

    Fail-safe: on any resolution error, leave ``can_manage`` unset — the
    template treats a missing flag as "show Manage" (the earlier default),
    so a probe failure never hides a button the principal legitimately has.
    """
    connectors = payload.get("connectors") or []
    gads = next((c for c in connectors if c.get("key") == "google_ads"), None)
    if not gads:
        return
    connection_esid = gads.get("connection_esid")
    if not connection_esid:
        return  # not connected — no Manage button rendered anyway
    username = session.get("username") or ""
    try:
        from web.services.connection_service import ConnectionService
        from web.blueprints.connections.routes import _viewer_can_see_connection
    except ImportError:
        from ...services.connection_service import ConnectionService
        from ..connections.routes import _viewer_can_see_connection
    try:
        source = ConnectionService.get_connection(connection_esid)
        if source is not None:
            gads["can_manage"] = bool(_viewer_can_see_connection(source, viewer_username=username))
    except Exception as e:  # noqa: BLE001 — display-only enrichment; a probe failure must never break the payload
        logging.warning("can_manage probe failed for %s: %s", connection_esid, e)


@data_connectors_bp.route("/api/teams/<team_esid>/summary-sections", methods=["GET", "POST"])
def api_team_summary_sections(team_esid: str):
    """Tier 2 — the per-team "Report sections" panel.

    GET returns the Summary Report's card vocabulary (ordered keys + labels)
    plus the team's current overrides; POST replaces the overrides with the
    sanitized map from the body (``{"overrides": {unit_key: "show"|"hide"}}``
    — absent key = automatic). Explicit choice wins over the Tier-1
    auto-hide in both directions; the viewer picks it up on next render via
    the availability endpoint's ``units`` payload.

    Tenant-scoped like the disconnect route: a scoped caller naming
    another tenant's team gets the same 404 as a missing team.
    """
    _require_data_connectors()
    try:
        from web.services import summary_visibility_service as svs
    except ImportError:
        from ...services import summary_visibility_service as svs
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return jsonify({"success": False, "error": "Search backend unavailable"}), 503
    allowed = _effective_allowed_workspaces()

    if request.method == "GET":
        overrides = svs.get_overrides(es, team_esid, allowed)
        if overrides is None:
            return jsonify({"success": False, "error": "Team not found"}), 404
        return jsonify(
            {
                "success": True,
                "units": [{"key": k, "label": lbl} for k, lbl in svs.SUMMARY_UNITS],
                "overrides": overrides,
            }
        )

    body = request.get_json(silent=True) or {}
    try:
        stored = svs.set_overrides(es, team_esid, body.get("overrides"), allowed)
    except Exception:  # noqa: BLE001 — write failure only; scoping returns None below
        return jsonify({"success": False, "error": "Could not save report sections"}), 500
    if stored is None:
        return jsonify({"success": False, "error": "Team not found"}), 404
    return jsonify({"success": True, "overrides": stored})


@data_connectors_bp.route("/api/teams/<team_esid>/connectors/<connector_key>/disconnect", methods=["POST"])
def api_disconnect_connector(team_esid: str, connector_key: str):
    """remove a connector from a team.

    Turns off the connector's ``ds_*`` flag and clears the team's linked
    ``*_esid`` field(s), returning the card to "not configured". This is the
    self-service exit for a connector stuck in "Needs attention"
    (disconnected / needs-setup) — previously only a superuser hand-editing the
    team doc could clear it.

    The underlying ``de_connection`` OAuth credential is intentionally left in
    place (it may be shared; revoke separately on the Manage Connection page).

    Tenant-scoped: a scoped user disconnecting another tenant's team gets the
    same 404 as a missing team. Returns the freshly recomputed connector
    payload so the page repaints without a second fetch.
    """
    _require_data_connectors()
    try:
        payload = disconnect_connector(
            team_esid,
            connector_key,
            allowed_workspaces=_effective_allowed_workspaces(),  # privileged callers narrowed to primary ws + grants
        )
    except ConnectorDisconnectError as e:
        # "Team not found" is the deny / not-found collapse → 404; everything
        # else (unknown key, write failure) is a 400.
        msg = e.public_message
        status = 404 if msg == "Team not found" else 400
        return jsonify({"success": False, "error": msg}), status
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Could not disconnect connector",
            log_detail=f"api_disconnect_connector failed team={team_esid} key={connector_key}",
            exc=e,
        )
    return jsonify({"success": True, **payload})


@data_connectors_bp.route(
    "/api/teams/<team_esid>/connectors/<connector_key>/accounts/<account_esid>/remove",
    methods=["POST"],
)
def api_remove_connector_account(team_esid: str, connector_key: str, account_esid: str):
    """detach a SINGLE account from a team's connector.

    Unlike disconnect, this removes only
    ``account_esid`` from the connector's multi-value link and leaves the
    other accounts — and the connector itself — intact. The ``ds_*`` flag is
    turned off only when the last account is removed. The underlying
    ``de_connection`` credential is left in place (same policy as disconnect).

    Scoped to multi-account connectors (Google Ads in this phase); a
    single-value connector raises (use disconnect there). Tenant-scoped — a
    scoped user touching another tenant's team gets the same 404 as a missing
    team. Returns the freshly recomputed connector payload.
    """
    _require_data_connectors()
    try:
        payload = remove_connector_account(
            team_esid,
            connector_key,
            account_esid,
            allowed_workspaces=_effective_allowed_workspaces(),  # privileged callers narrowed to primary ws + grants
        )
    except ConnectorDisconnectError as e:
        # "Team not found" is the deny / not-found collapse → 404; everything
        # else (unknown connector, account not linked, write failure) is a 400.
        msg = e.public_message
        status = 404 if msg == "Team not found" else 400
        return jsonify({"success": False, "error": msg}), status
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Could not remove account",
            log_detail=f"api_remove_connector_account failed team={team_esid} key={connector_key} account={account_esid}",
            exc=e,
        )
    return jsonify({"success": True, **payload})


@data_connectors_bp.route("/api/teams/<team_esid>/ad-account-links/<action>", methods=["POST"])
def api_edit_team_ad_account_links(team_esid: str, action: str):
    """Phase 4 — admin edits to a team's effective-dated ad-account
    links (the panel on the Asset Explorer team page).

    ``action`` ∈ ``add`` / ``close`` / ``update`` / ``delete``:

    * ``add`` — ``{ad_account_esid, connection_esid?, start_date?, note?}``;
      appends a manual OPEN link (rejects a second open link to the same
      account — close it first).
    * ``close`` — ``{ad_account_esid, end_date?}``; sets ``end_date``
      (default today) on the open link. Closing is the only removal —
      history is never deleted.
    * ``update`` — ``{link_index, start_date?, end_date?, note?}``; edits the
      addressed link's dates/note (empty string clears; clearing end_date
      reopens).
    * ``delete`` — ``{link_index}``; removes an ERRONEOUS row entirely (admin
      correction — routine end-of-use is ``close``, which preserves the
      history the importer attributes from). Logged loudly server-side.

    All writes stamp ``source="manual"`` + who/when so hand edits stay
    distinguishable from wizard writes. Gated on the Data Connectors
    permission + the caller's workspace scope (cross-tenant collapses to
    404, same as the connector disconnect endpoints above). Warnings (e.g.
    >2 concurrently-open links) come back on the success envelope.
    """
    _require_data_connectors()
    try:
        from web.services.team_ad_account_link_service import (
            LinkEditError,
            edit_links,
        )
    except ImportError:
        from ...services.team_ad_account_link_service import (  # type: ignore[no-redef]
            LinkEditError,
            edit_links,
        )

    data, err = _wizard_payload_or_400()
    if err:
        return err
    try:
        result = edit_links(
            action,
            team_esid,
            data,
            username=session.get("username") or "unknown",
            allowed_workspaces=_effective_allowed_workspaces(),
        )
    except LinkEditError as e:
        msg = e.public_message
        status = 404 if msg == "Team not found" else 400
        return jsonify({"success": False, "error": msg}), status
    except Exception as e:  # noqa: BLE001 — boundary
        from lib.errors import safe_error_response

        return safe_error_response(
            "Could not save the link change",
            log_detail=f"api_edit_team_ad_account_links failed team={team_esid} action={action}",
            exc=e,
        )
    return jsonify({"success": True, **result})


# ── Wizard routes ─────────────────────────────────────────────────────────────


@data_connectors_bp.route("/api/wizard/options/<asset_type>")
def api_wizard_options(asset_type: str):
    """Return existing assets of a type for the wizard's pick-existing dropdown.

    Whitelisted to a fixed set of connector asset types (see
    _PICKER_ALLOWED_TYPES) so this can't be used to enumerate arbitrary
    indices via the API.

     (2026-05-27): narrow the option list to the operator's
    workspace scope. Pre-fix the picker enumerated EVERY asset of the
    type across the whole cluster — a workspace-admin in workspace-A
    could read every workspace-B Echo tenant / Google Ads customer /
    FB account title. The page-level ``_require_data_connectors`` gate is
    permission-only (mc_data_connectors), not tenant-scoped, so it never
    closed this. Two layers now apply, mirroring the Asset Explorer
    list view (``app.py`` ``list_assets``) and the sibling
     fixes:

    * **Layer 1 — deny-all floor.** A caller with no resolvable scope
      from any source sees an empty picker (not the cluster list). This
      is the same ``current_user_has_any_scope`` floor the Asset
      Explorer uses.
    * **Layer 2 — workspace narrowing.** ``get_allowed_workspaces()``
      (``None`` = superuser/admin-override, see everything; a list =
      narrow; ``[]`` = deny-all) is threaded into
      ``list_assets_for_picker``, which fail-closes on docs missing
      ``workspace_esid``.
    """
    _require_data_connectors()
    if asset_type not in _PICKER_ALLOWED_TYPES:
        return jsonify({"success": False, "error": "Asset type not allowed"}), 400

    # Layer 1: deny-all floor. A scoped operator with no
    # resolvable workspace/org/team scope from ANY source must not see
    # the cluster-wide list. current_user_has_any_scope() returns True
    # for superusers (admin override) so they fall through to the
    # get_allowed_workspaces() is None path below and see everything.
    if not RoleFilterService.current_user_has_any_scope():
        return jsonify({"success": True, "options": []})

    # Layer 2: workspace narrowing. None = no filter (superuser /
    # asset_admin); a concrete list narrows the picker; [] denies all.
    # a privileged caller is narrowed to their primary workspace +
    # grants (so the pick-existing dropdown stops enumerating every workspace's
    # assets), matching the team dropdown on the same page.
    allowed_workspaces = _effective_allowed_workspaces()

    try:
        options = list_assets_for_picker(asset_type, allowed_workspaces=allowed_workspaces)
    except WizardError as e:
        # WizardError carries operator-facing validation messages.
        return jsonify({"success": False, "error": e.public_message}), 400
    return jsonify({"success": True, "options": options})


def _wizard_payload_or_400():
    """Pull the JSON body from the request, returning ({}, response) on
    failure or (data, None) on success."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"success": False, "error": "Request body must be JSON"}), 400)
    return data, None


@data_connectors_bp.route("/api/wizard/echo/configure", methods=["POST"])
def api_wizard_echo_configure():
    """Configure Echo for a team. Body:
    {
        "team_esid": "...",
        "tenant_esid": "..." | null,
        "new_tenant": {...} | null   // pick exactly one
    }
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    try:
        result = configure_echo(
            team_esid=team_esid,
            tenant_esid=data.get("tenant_esid") or None,
            new_tenant=data.get("new_tenant") or None,
        )
    except WizardError as e:
        # WizardError carries operator-facing validation messages
        # raised by the wizard service. CodeQL flags these as
        # py/stack-trace-exposure but the contract is intentional.
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Echo wizard failed",
            log_detail="api_wizard_echo_configure failed",
            exc=e,
        )
    # starter dashboards for a connector added outside the wizard.
    result.update(_install_starter_pack_for_team(team_esid, "echo", [str(result.get("tenant_esid") or "")]))
    return jsonify({"success": True, **result})


def _install_starter_pack_for_team(team_esid: str, connector_id: str, source_account_ids: list[str]) -> dict:
    """install the connector's starter dashboards for a team whose
    connector was just configured on this page (Echo / Disciple.Tools; Meta
    runs the same call inside ``fb_finish_data_connectors_wizard`` and the
    Google connectors already do it through ``_run_tokenless_finalize``).

    Fail-soft and idempotent (see ``TemplateService.install_starter_pack``);
    returns the ``first_look_status`` / ``first_look_id`` pair for the JSON
    envelope so the wizard UI can say a dashboard was created. The team's
    workspace comes from the team doc — this page picks a concrete team, so
    the caller's primary workspace would be the wrong tenant boundary.
    """
    try:
        try:
            from web.services.template_service import TemplateService
        except ImportError:
            from ...services.template_service import TemplateService
        try:
            from web.services.connector_wizard_service import TEAMS_INDEX, AssetService
        except ImportError:
            from ...services.connector_wizard_service import TEAMS_INDEX, AssetService
        workspace_esid = None
        try:
            team_doc = AssetService.get_by_id(TEAMS_INDEX, team_esid) or {}
            workspace_esid = (team_doc.get("workspace_esid") or "").strip() or None
        except Exception as e:  # noqa: BLE001 — a missing workspace only degrades the menu grouping
            logging.warning("team workspace lookup failed for %s: %s", team_esid, e)
        out = TemplateService.install_starter_pack(
            connector_id=connector_id,
            team_esid=team_esid,
            source_account_ids=[x for x in (source_account_ids or []) if x],
            workspace_esid=workspace_esid,
            origin="data_connectors",
        )
        return {"first_look_status": out.get("first_look_status"), "first_look_id": out.get("first_look_id")}
    except Exception as e:  # noqa: BLE001 — the connector is configured; the pack is a bonus
        logging.warning("starter pack failed for connector=%s team=%s: %s", connector_id, team_esid, e)
        return {"first_look_status": "exception", "first_look_id": None}


def _team_live_google_ads_stamp(team_esid: str) -> str:
    """the team's google_ads_connection_esid when it is a live,
    tokened connection, else ""."""
    try:
        from lib.es_helpers import get_document_by_id

        try:
            from web.services.connection_service import ConnectionService
        except ImportError:
            from ...services.connection_service import ConnectionService
        stamp = str((get_document_by_id("de_asset_teams", team_esid) or {}).get("google_ads_connection_esid") or "")
        doc = ConnectionService.get_connection(stamp) if stamp else None
    except Exception as e:  # noqa: BLE001 — falls back to the earlier finalize, never blocks onboarding
        logging.warning("team stamp lookup failed for %s: %s", team_esid, e)
        return ""
    live = bool(doc and doc.get("provider") == "google_ads" and doc.get("status") == "active" and doc.get("oauth_token_enc"))
    return stamp if live else ""


def _google_ads_backing_connection(link_result: dict) -> str | None:
    """the live connection configure_google_ads_customer stamped on
    the team, or None. When one exists the finalize must reuse it: minting a
    tokenless placeholder under the clicking user re-pointed the team at a
    "Pending Token" connection. The customer's connection-asset row is written
    here because the finalize skips its own writes for an existing connection.
    """
    connection_esid = str((link_result or {}).get("connection_esid") or "").strip()
    customer_id = str((link_result or {}).get("customer_id") or "").strip()
    if not connection_esid:
        return None
    if customer_id:
        try:
            from web.services.connection_service import ASSET_TYPE_GOOGLE_ADS_CUSTOMER, ConnectionService
        except ImportError:
            from ...services.connection_service import ASSET_TYPE_GOOGLE_ADS_CUSTOMER, ConnectionService
        try:
            ConnectionService.upsert_connection_asset(
                connection_esid=connection_esid,
                asset_type=ASSET_TYPE_GOOGLE_ADS_CUSTOMER,
                provider_asset_id=customer_id,
                label=customer_id,
            )
        except Exception as e:  # noqa: BLE001 — the link and stamp already landed; the summary row is cosmetic
            logging.warning("upsert_connection_asset failed for %s/%s: %s", connection_esid, customer_id, e)
    return connection_esid


def _run_tokenless_finalize(team_esid: str, connector_id: str, existing_connection_esid: str | None = None):
    """v2 (2026-05-26) — shared post-link finalize.

    Called by the Google Ads / GA4 configure endpoints AFTER the team
    doc has been updated (asset link + ds_* flag + integration-status
    where applicable). Writes the de_connection + de_connection_asset
    rows, materialises the first-look pack, and kicks off the on-
    demand backfill — same core as the onboarding wizard's step-4
    tokenless finalize, namespaced under audit_event_prefix=
    "data_connectors" so the two call sites are distinguishable.

    the YouTube endpoints pass ``existing_connection_esid``
    (the real-principal connection configure_youtube just
    wrote) so the core skips its tokenless connection mint and runs
    only the first-look materialise + on-demand kickoff.

    Returns the apply-result dict from the analytics core (no Flask
    response wrap) so the caller can fold it into the wizard's JSON
    envelope.
    """
    username = session.get("username") or ""
    if not username:
        return None, (jsonify({"success": False, "error": "Not authenticated"}), 401)

    try:
        from web.services.user_profile_service import UserProfileService
    except ImportError:
        from ...services.user_profile_service import UserProfileService
    try:
        account_type = (UserProfileService.get_account_type(username) or "").strip() or "org_owner"
    except Exception:
        account_type = "org_owner"

    try:
        from web.blueprints.analytics.routes import _apply_tokenless_connector_for_team
    except ImportError:
        from ..analytics.routes import _apply_tokenless_connector_for_team

    apply_result = _apply_tokenless_connector_for_team(
        username=username,
        selected_connector_id=connector_id,
        account_type=account_type,
        team_esid=team_esid,
        audit_event_prefix="data_connectors",
        audit_event_namespace="data_connectors",
        existing_connection_esid=existing_connection_esid,
    )
    return apply_result, None


def _check_team_for_wizard(team_esid: str):
    """Run the standard team-authz re-check + 404 collapse. Returns
    ``None`` on success; a Flask response tuple on failure that the caller
    returns verbatim.

    Two layers, both collapsing a deny into a generic 404 so a caller can't
    probe team esids across tenants:

    1. workspace narrowing: ``get_team_connectors`` returns ``None``
       when the team's ``workspace_esid`` is outside the caller's effective
       workspace allow-list (privileged callers narrowed to primary ws +
       grants).
    2. team-admin scope: holding ``mc_data_connectors`` is not
       enough to link a data source to ANY team that merely happens to fall in
       an allowed workspace. The caller must also be able to administer that
       specific team. Privileged callers (``current_user_is_privileged``) pass;
       everyone else must have the team's title within
       ``get_allowed_teams_for_org_users()`` (the same title-scoped set the
       team dropdown is built from). ``None`` from that resolver means "no
       resolvable scope" → deny. We compare on the team TITLE because the
       payload already carries it (``team.title`` == ``team_title``, the exact
       string shape the resolver returns), so no extra esid→title lookup is
       needed.
    """
    if not team_esid:
        return jsonify({"success": False, "error": "team_esid is required"}), 400
    team_payload = get_team_connectors(
        team_esid,
        allowed_workspaces=_effective_allowed_workspaces(),  # privileged callers narrowed to primary ws + grants
    )
    if team_payload is None:
        return jsonify({"success": False, "error": "Team not found"}), 404

    # team-admin scope check (IDOR on team_esid). Privileged callers
    # short-circuit; everyone else must administer THIS team, not just share a
    # workspace with it. Fail-closed: an unresolvable scope denies.
    try:
        if not RoleFilterService.current_user_is_privileged():
            allowed_team_titles = RoleFilterService.get_allowed_teams_for_org_users()
            team_title = (team_payload.get("team") or {}).get("title")
            if not allowed_team_titles or team_title not in allowed_team_titles:
                # Collapse deny into the same 404 as the workspace gate so team
                # esids can't be enumerated by probing for a 403-vs-404 split.
                return jsonify({"success": False, "error": "Team not found"}), 404
    except Exception as e:  # noqa: BLE001 — an authz-resolution error must fail closed, never silently allow
        logging.warning("data_connectors._check_team_for_wizard authz: %s", e)
        return jsonify({"success": False, "error": "Team not found"}), 404

    return None


def _check_connection_for_wizard(connection_esid: str):
    """IDOR gate for wizard routes that take a ``connection_esid``
    (or ``account_esid`` resolving to a connection-backed asset) from request
    input and then read the connection's OAuth token / status.

    Returns ``None`` on success; a Flask ``(json, status)`` tuple on failure
    that the caller returns verbatim. A denied / missing connection collapses
    to the SAME generic 404 so a caller can't enumerate connection esids by
    probing a 403-vs-404 split.

    Reuses the connections blueprint's ``_viewer_can_see_connection`` gate
    (principal-identity OR allowed-workspace-with-org-sharing OR the
    None=see-all privileged bypass) — the same gate the connection-detail and
    ga4/youtube connection-assets endpoints already enforce. Several sibling
    wizard routes (``ga4/connection-assets``, ``fb/connection-assets``,
    ``youtube/connection-assets``) call ``_viewer_can_see_connection`` inline;
    this helper consolidates that for the routes that were missing it.
    """
    if not connection_esid:
        return jsonify({"success": False, "error": "connection_esid is required"}), 400
    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    try:
        from web.services.connection_service import ConnectionService
        from web.blueprints.connections.routes import _viewer_can_see_connection
    except ImportError:
        from ...services.connection_service import ConnectionService
        from ..connections.routes import _viewer_can_see_connection
    try:
        source = ConnectionService.get_connection(connection_esid)
        if not source or not _viewer_can_see_connection(source, viewer_username=username):
            return jsonify({"success": False, "error": "Connection not found"}), 404
    except Exception as e:  # noqa: BLE001 — an authz-resolution error must fail closed, never silently allow
        logging.warning("data_connectors._check_connection_for_wizard authz: %s", e)
        return jsonify({"success": False, "error": "Connection not found"}), 404
    return None


def _check_account_asset_for_wizard(asset_index: str, account_esid: str):
    """IDOR gate for wizard routes that take an ``account_esid``
    naming a connection-backed asset doc (``de_asset_youtube_accounts`` /
    ``de_asset_ga_properties``) from request input and then read its OAuth
    status / token-backed channel/property list.

    Returns ``None`` on success; a Flask ``(json, status)`` tuple on failure.
    Verifies the asset's ``workspace_esid`` is within the caller's effective
    workspace allow-list (``_effective_allowed_workspaces`` — privileged callers
    narrowed to primary ws + grants), mirroring the
    pick-existing asset picker's fail-closed boundary. A denied / missing asset
    collapses to the same generic 404 so esids can't be enumerated.

    Contract of the allow-list (matches ``get_allowed_workspaces``):
      * ``None``      → privileged see-all → allow
      * ``[]``        → deny-all
      * ``[esids]``   → allow iff the asset's workspace_esid is in the set
    A pick-new request (no ``account_esid``) is a no-op (returns ``None``); the
    create path stamps the caller's own workspace.
    """
    esid = (account_esid or "").strip()
    if not esid:
        return None  # pick-new — nothing client-supplied to validate
    if not (session.get("username") or ""):
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    try:
        from lib.es_helpers import get_document_by_id

        allowed = _effective_allowed_workspaces()
        if allowed is None:
            return None  # privileged see-all
        doc = get_document_by_id(asset_index, esid) or {}
        if not doc:
            return jsonify({"success": False, "error": "Account not found"}), 404
        # a wizard account (youtube_accounts / ga4_accounts) is minted
        # in step 1 BEFORE a team (and thus a workspace) is chosen, so it carries
        # NO workspace_esid until configure_* stamps it at the final step (the
        # timing gap). The Authorize step polls oauth-status DURING that
        # window; gating purely on `workspace_esid in allowed` then 404s on every
        # poll (None not in the set) and the wizard hangs on "waiting for OAuth" —
        # for EVERY caller, made even privileged callers workspace-
        # bounded here rather than see-all.
        #
        # A workspace-less doc is an unclaimed, in-flight (or abandoned/orphaned)
        # wizard account. Allow it ONLY to the user who created it — the account
        # is stamped wizard_created_by=username at mint time. A blanket
        # workspace-less allow would expose every unclaimed account's OAuth status
        # (title, auth_url slug, has_token) to any connectors user by esid, and
        # orphaned docs stay workspace-less indefinitely, so that window is not
        # transient. A doc that HAS a workspace outside the caller's set is still
        # gated as before — the real IDOR protection for claimed accounts.
        doc_ws = (str(doc.get("workspace_esid") or "").strip()) or None
        if doc_ws is None:
            creator = str(doc.get("wizard_created_by") or "").strip()
            caller = str(session.get("username") or "").strip()
            if not creator or creator != caller:
                return jsonify({"success": False, "error": "Account not found"}), 404
        elif doc_ws not in set(allowed):
            return jsonify({"success": False, "error": "Account not found"}), 404
    except Exception as e:  # noqa: BLE001 — an authz-resolution error must fail closed, never silently allow
        logging.warning("data_connectors._check_account_asset_for_wizard authz: %s", e)
        return jsonify({"success": False, "error": "Account not found"}), 404
    return None


@data_connectors_bp.route("/api/wizard/google-ads/configure", methods=["POST"])
def api_wizard_google_ads_configure():
    """v2 — Google Ads in-page configure-then-finalize wizard.

    Body: ``{"team_esid": "...", "customer_esid": "..."}``.

    1. Updates the team doc with ``google_ads_customer_esid`` +
       ``ds_google_ads="true"`` +
       ``team_google_ads_integration_status="Active"`` (so the
       team-edit page's gating checkbox is satisfied and the importer
       picks the team up).
    2. Runs the shared tokenless finalize to write the de_connection
       + de_connection_asset rows, install the first-look pack, and
       kick off the on-demand backfill.

    Replaces v1's one-shot tokenless endpoint, which required
    the team to be pre-configured on the team-edit page before the
    tile click could succeed.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err

    team_esid = (data.get("team_esid") or "").strip()
    customer_esid = (data.get("customer_esid") or "").strip() or None
    new_customer = data.get("new_customer") or None
    # Exactly one of pick-existing / create-new must be present.
    if bool(customer_esid) == bool(new_customer):
        return (
            jsonify({"success": False, "error": "Provide exactly one of customer_esid or new_customer"}),
            400,
        )
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err

    try:
        link_result = configure_google_ads_customer(
            team_esid=team_esid,
            customer_esid=customer_esid,
            new_customer=new_customer,
            # the connection the wizard just authorized, if any.
            prefer_connection_esid=(data.get("connection_esid") or "").strip() or None,
        )
    except WizardError as e:
        # a picked-existing customer with no OAuth token is not a hard
        # failure: the wizard must send the user through Authorize first. Surface
        # a typed needs_authorization signal (HTTP 200, success=False) carrying
        # the customer id/title so the frontend can open the OAuth step instead
        # of showing an error. Plain WizardError stays a 400.
        if getattr(e, "needs_authorization", False):
            return jsonify(
                {
                    "success": False,
                    "error_kind": "needs_authorization",
                    "error": e.public_message,
                    "customer_id": getattr(e, "customer_id", ""),
                    "customer_title": getattr(e, "customer_title", ""),
                }
            )
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Google Ads team update failed",
            log_detail="api_wizard_google_ads_configure team-doc update failed",
            exc=e,
        )
    customer_esid = link_result.get("customer_esid")

    apply_result, response_err = _run_tokenless_finalize(
        team_esid, "google_ads", existing_connection_esid=_google_ads_backing_connection(link_result)
    )
    if response_err is not None:
        return response_err
    if not apply_result.get("success"):
        return jsonify(
            {
                "success": False,
                "error": apply_result.get("error") or "Setup failed.",
                "error_kind": apply_result.get("error_kind"),
            }
        )
    return jsonify(
        {
            "success": True,
            "customer_esid": customer_esid,
            "connection_esid": apply_result.get("connection_esid"),
            "asset_count": apply_result.get("asset_count", 0),
            "first_look_status": apply_result.get("first_look_status"),
        }
    )


# ── Google Ads OAuth grant path ────────────────────────────────────
#
# The pre-existing tokenless google-ads/configure route (above) stays as the
# service-account path. These add the per-user OAuth-grant path: start mints a
# pending de_connection + popup URL, status polls for token arrival. After the
# token lands the frontend calls the existing /google-ads/configure to link the
# customer to the team (the link logic is identical regardless of auth method).


@data_connectors_bp.route("/api/wizard/google-ads/oauth-start", methods=["POST"])
def api_wizard_google_ads_oauth_start():
    """Step 1. Body: {customer_id}. Create a pending google_ads
    de_connection owned by the authenticated user and seed an OAuth state.
    Returns {connection_esid, auth_url_path, has_token, fboauth_hostname}.

    The frontend opens the popup as
    ``https://{fboauth_hostname}/google_ads/{auth_url_path}`` and polls
    oauth-status for has_token.

    SECURITY: principal is taken from session, NOT the request body.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    try:
        result = google_ads_oauth_start(
            customer_id=(data.get("customer_id") or "").strip(),
            username=username,
        )
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    return jsonify({"success": True, **result, "fboauth_hostname": _fboauth_hostname()})


@data_connectors_bp.route("/api/wizard/google-ads/oauth-status/<connection_esid>")
def api_wizard_google_ads_oauth_status(connection_esid: str):
    """Step 2 polling. Returns {connection_esid, has_token, status}."""
    _require_data_connectors()
    # existence / has_token / status oracle for any esid without
    # this; same gate as the tiktok / fb status routes.
    authz_err = _check_connection_for_wizard(connection_esid)
    if authz_err is not None:
        return authz_err
    return _wizard_call(google_ads_connection_status, connection_esid=connection_esid)


@data_connectors_bp.route("/api/wizard/google-ads/oauth-validate", methods=["POST"])
def api_wizard_google_ads_oauth_validate():
    """after the OAuth grant lands, verify the customer id is
    reachable with the connection's own token (before Confirm).

    Body: {connection_esid, customer_id}. Returns {success, ok} on a clean or
    fail-open probe; 400 with an OAuth-appropriate error on a definitive
    not-authorized/not-found.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    # pass the session username so the service can assert connection
    # ownership before probing with the connection's OAuth token (IDOR gate).
    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    return _wizard_call(
        google_ads_validate_oauth_customer,
        connection_esid=(data.get("connection_esid") or "").strip(),
        customer_id=(data.get("customer_id") or "").strip(),
        username=username,
    )


# ── TikTok OAuth grant ────────────────────────────────────────────
#
# Login Kit roundtrip: start mints (or reuses) the user's wizard-pending TikTok
# connection and seeds its OAuth state; the frontend opens
# https://{fboauth_hostname}/tiktok/{auth_url_path} and polls status until the
# fb_oauth-host callback has stored the token under the real open_id.


def _require_tiktok_enabled() -> None:
    """404 every TikTok wizard route while the connector is switched off.

    404 rather than 403: with the switch off the connector does not exist on
    this deployment, and the tile that would call these routes is not rendered.
    Checked before the permission gate so the answer is the same for everyone.
    """
    from lib.tiktok_constants import tiktok_enabled

    if not tiktok_enabled():
        abort(404)


@data_connectors_bp.route("/api/wizard/tiktok/oauth-start", methods=["POST"])
def api_wizard_tiktok_oauth_start():
    """Returns {connection_esid, auth_url_path, fboauth_hostname}.

    SECURITY: principal is taken from session, NOT the request body.
    """
    _require_tiktok_enabled()
    _require_data_connectors()
    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    try:
        result = tiktok_oauth_start(username=username)
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    return jsonify({"success": True, **result, "fboauth_hostname": _fboauth_hostname()})


@data_connectors_bp.route("/api/wizard/tiktok/oauth-status/<connection_esid>")
def api_wizard_tiktok_oauth_status(connection_esid: str):
    """Returns {connection_esid, status, has_token, label}."""
    _require_tiktok_enabled()
    _require_data_connectors()
    # connection_esid is a client-supplied path param.
    authz_err = _check_connection_for_wizard(connection_esid)
    if authz_err is not None:
        return authz_err
    return _wizard_call(tiktok_connection_status, connection_esid=connection_esid)


@data_connectors_bp.route("/api/wizard/tiktok/configure", methods=["POST"])
def api_wizard_tiktok_configure():
    """Body: {team_esid, connection_esid}. Links the authorized TikTok account to
    the team, installs the TikTok starter dashboards, and starts the first
    import. Returns {connection_esid, label, open_id, linked, first_look_*,
    import_started}.
    """
    _require_tiktok_enabled()
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    team_esid = (data.get("team_esid") or "").strip()
    connection_esid = (data.get("connection_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid) or _check_connection_for_wizard(connection_esid)
    if authz_err is not None:
        return authz_err
    try:
        result = configure_tiktok(team_esid=team_esid, connection_esid=connection_esid)
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "TikTok wizard failed",
            log_detail="api_wizard_tiktok_configure failed",
            exc=e,
        )

    result.update(_install_starter_pack_for_team(team_esid, "tiktok", [result.get("open_id") or ""]))

    # First snapshot now rather than at the next scheduled run. Fail-soft: the
    # account is linked either way; the reason is reported for the UI.
    try:
        try:
            from web.blueprints.analytics.routes import _kickoff_on_demand_backfill
        except ImportError:
            from ..analytics.routes import _kickoff_on_demand_backfill
        kickoff = _kickoff_on_demand_backfill(
            username=username,
            connection_esid=connection_esid,
            connector_id="tiktok",
            asset_count=1,
            scope_team_esid=team_esid,
        )
    except Exception as e:  # noqa: BLE001 — the link is saved; the import is a bonus
        logging.warning("tiktok kickoff failed for team=%s: %s", team_esid, e)
        kickoff = {"ok": False, "reason": "exception"}
    result["import_started"] = bool(kickoff.get("ok"))
    result["import_run_id"] = kickoff.get("run_id")
    if not kickoff.get("ok"):
        result["import_skipped_reason"] = kickoff.get("reason") or ""
    return jsonify({"success": True, **result})


# ── Google Ads "Discover from a Google account" mode ──────────────


@data_connectors_bp.route("/api/wizard/google-ads/discovery-start", methods=["POST"])
def api_wizard_google_ads_discovery_start():
    """Step 1. Mint a customer-id-less discovery connection + OAuth state.
    Returns {connection_esid, auth_url_path, has_token, fboauth_hostname}. The
    frontend opens the popup at https://{fboauth_hostname}/google_ads/{auth_url_path}.

    SECURITY: principal is the session username, never the request body."""
    _require_data_connectors()
    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    try:
        result = google_ads_discovery_start(username=username)
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    return jsonify({"success": True, **result, "fboauth_hostname": _fboauth_hostname()})


@data_connectors_bp.route("/api/wizard/google-ads/discovery-list/<connection_esid>")
def api_wizard_google_ads_discovery_list(connection_esid: str):
    """Step 2. List the customers the discovery token can add to a team.
    Returns {success, customers: [{id, name, manager}]}.

    SECURITY: ownership of the connection is verified against the session username
    (the connection holds an OAuth token)."""
    _require_data_connectors()
    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    return _wizard_call(google_ads_discovery_list, connection_esid=connection_esid, username=username)


@data_connectors_bp.route("/api/wizard/google-ads/discovery-confirm", methods=["POST"])
def api_wizard_google_ads_discovery_confirm():
    """Step 3. Link the chosen discovered customers to the team.
    Body: {connection_esid, team_esid, customers: [{id, name}]}. Returns
    {success, linked: [...], errors: [...]}.

    SECURITY: principal is the session username, never the request body."""
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    # the service already verifies the caller OWNS the discovery
    # connection, but the destination team_esid was unguarded: a
    # caller could link discovered customers to a team in another tenant. Gate
    # the team the same way every other configure route does.
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    return _wizard_call(
        google_ads_discovery_confirm,
        connection_esid=(data.get("connection_esid") or "").strip(),
        team_esid=team_esid,
        customers=(data.get("customers") or []),
        username=username,
    )


@data_connectors_bp.route("/api/wizard/ga4/configure", methods=["POST"])
def api_wizard_ga4_configure():
    """v2 — GA4 in-page configure-then-finalize wizard.

    Body: ``{"team_esid": "...", "property_esid": "..."}``.

    1. Appends ``property_esid`` to the team's
       ``team_ga4_property_esids`` array (preserves any properties
       already linked) and flips ``ds_google_analytics="true"``.
    2. Runs the shared tokenless finalize.

    Append-style rather than overwrite so a user adding a second
    property doesn't accidentally unlink the first. To unlink, the
    user uses the team-edit page's multi-select picker.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err

    team_esid = (data.get("team_esid") or "").strip()
    property_esid = (data.get("property_esid") or "").strip() or None
    new_property = data.get("new_property") or None
    if bool(property_esid) == bool(new_property):
        return (
            jsonify({"success": False, "error": "Provide exactly one of property_esid or new_property"}),
            400,
        )
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err

    try:
        link_result = configure_ga4_property(
            team_esid=team_esid,
            property_esid=property_esid,
            new_property=new_property,
        )
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "GA4 team update failed",
            log_detail="api_wizard_ga4_configure team-doc update failed",
            exc=e,
        )

    apply_result, response_err = _run_tokenless_finalize(team_esid, "google_analytics_4")
    if response_err is not None:
        return response_err
    if not apply_result.get("success"):
        return jsonify(
            {
                "success": False,
                "error": apply_result.get("error") or "Setup failed.",
                "error_kind": apply_result.get("error_kind"),
            }
        )
    return jsonify(
        {
            "success": True,
            "property_esid": link_result.get("property_esid") or property_esid,
            "property_count": link_result.get("property_count"),
            "connection_esid": apply_result.get("connection_esid"),
            "asset_count": apply_result.get("asset_count", 0),
            "first_look_status": apply_result.get("first_look_status"),
        }
    )


def _run_onboarding_tokenless_finalize_with_bookkeeping(
    *,
    team_esid: str,
    connector_id: str,
    existing_connection_esid: str | None = None,
):
    """onboarding-scoped post-link finalize.

    Wraps :func:`_run_tokenless_finalize` with the wizard_session
    step-7 bookkeeping + audit + funnel events the marketing funnel
    expects, then builds the step-7-building redirect URL the modal
    page will navigate to. Symmetric with
    ``api_wizard_youtube_onboarding_finalize`` body but generalised
    over connector_id so the Google Ads + GA4 endpoints share the
    bookkeeping path.

    Returns ``(apply_result, redirect_url)`` on the success path and
    ``(apply_result, None)`` if the apply core failed — the caller
    surfaces the apply_result error verbatim in the JSON envelope.
    Returns ``(None, response)`` if the authn / authz preconditions
    failed; the caller returns ``response`` verbatim.
    """
    apply_result, response_err = _run_tokenless_finalize(
        team_esid, connector_id, existing_connection_esid=existing_connection_esid
    )
    if response_err is not None:
        return None, response_err
    if not apply_result.get("success"):
        return apply_result, None

    username = session.get("username") or ""

    try:
        from web.services.audit_log_service import AuditLogService
        from web.services.onboarding_branching_service import OnboardingBranchingService
        from web.services.user_profile_service import UserProfileService
        from web.services.wizard_analytics_service import (
            ACTION_COMPLETED,
            WizardAnalyticsService,
        )
        from web.services.wizard_session_service import WizardSessionService
    except ImportError:
        from ...services.audit_log_service import AuditLogService
        from ...services.onboarding_branching_service import OnboardingBranchingService
        from ...services.user_profile_service import UserProfileService
        from ...services.wizard_analytics_service import (
            ACTION_COMPLETED,
            WizardAnalyticsService,
        )
        from ...services.wizard_session_service import WizardSessionService

    try:
        account_type = (UserProfileService.get_account_type(username) or "").strip() or "org_owner"
    except Exception:
        account_type = "org_owner"

    asset_count = apply_result.get("asset_count", 0)

    AuditLogService.log_event(
        username,
        f"onboarding_step6_{connector_id}_finalize",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event=wizard.step6.{connector_id}_finalize "
            f"connector_id={connector_id} "
            f"team_esid={team_esid} "
            f"asset_count={asset_count} "
            f"first_look={apply_result.get('first_look_status') or 'unknown'}"
        ),
    )

    try:
        active = WizardSessionService.get_active(username, "onboarding")
        state = dict((active or {}).get("state") or {})
        defaults = OnboardingBranchingService.for_account_type(account_type)
        state["selected_connector_id"] = connector_id
        state["selected_assets"] = list(apply_result.get("provider_asset_ids") or [])
        state["friendly_names"] = {}
        state["org_shared"] = bool(defaults.step6_org_shared_default)
        state["current_step"] = "step-7"
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-7",
            state=state,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("wizard-session save failed for %r: %s", username, e)
        active = None

    try:
        WizardAnalyticsService.emit(
            step="step-6",
            action=ACTION_COMPLETED,
            context=WizardAnalyticsService.build_context(
                username=username,
                wizard_session=active or None,
                account_type=account_type,
                connector_override=connector_id,
            ),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("wizard-analytics emit failed for %r: %s", username, e)

    try:
        from flask import url_for as _url_for

        redirect_url = _url_for(
            "analytics.onboarding_step7_building_page",
            connector_id=connector_id,
            asset_count=asset_count,
            next="/dashboard",
        )
    except Exception:  # noqa: BLE001
        redirect_url = f"/onboarding/step7/building?connector_id={connector_id}" f"&asset_count={asset_count}&next=/dashboard"
    return apply_result, redirect_url


@data_connectors_bp.route("/api/wizard/google-ads/onboarding-finalize", methods=["POST"])
def api_wizard_google_ads_onboarding_finalize():
    """onboarding-scoped Google Ads configure-then-finalize.

    Mirrors :func:`api_wizard_google_ads_configure` but folds in the
    wizard_session step-7 bookkeeping + funnel events the onboarding
    funnel expects, and returns a ``redirect`` URL the modal page
    navigates to. The JS only POSTs here when the page was opened with
    ``?from_onboarding=1``; the regular configure endpoint stays
    unchanged so the Data Connectors path keeps its no-redirect,
    no-session behaviour.

    Body: ``{"team_esid": "...", "customer_esid": "..."}`` or
    ``{"team_esid": "...", "new_customer": {...}}``.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err

    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    team_esid = (data.get("team_esid") or "").strip()
    customer_esid = (data.get("customer_esid") or "").strip() or None
    new_customer = data.get("new_customer") or None
    # discover mode already linked its customers via
    # google-ads/discovery-confirm; this call exists only for the step-7
    # bookkeeping + redirect the onboarding funnel expects. Skip the
    # single-customer configure entirely.
    discovered = bool(data.get("discovered"))
    if not discovered and bool(customer_esid) == bool(new_customer):
        return (
            jsonify({"success": False, "error": "Provide exactly one of customer_esid or new_customer"}),
            400,
        )
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err

    try:
        link_result = (
            {}
            if discovered
            else configure_google_ads_customer(
                team_esid=team_esid,
                customer_esid=customer_esid,
                new_customer=new_customer,
                prefer_connection_esid=(data.get("connection_esid") or "").strip() or None,
            )
        )
    except WizardError as e:
        # same needs_authorization handling as the in-page configure
        # route. (The onboarding Google Ads flow is type-only/OAuth-first, so a
        # tokenless picked-existing should not normally reach here, but keep the
        # two call sites' contract identical.)
        if getattr(e, "needs_authorization", False):
            return jsonify(
                {
                    "success": False,
                    "error_kind": "needs_authorization",
                    "error": e.public_message,
                    "customer_id": getattr(e, "customer_id", ""),
                    "customer_title": getattr(e, "customer_title", ""),
                }
            )
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Google Ads team update failed",
            log_detail="api_wizard_google_ads_onboarding_finalize team-doc update failed",
            exc=e,
        )
    customer_esid = link_result.get("customer_esid")

    # discover mode already linked (and stamped) via discovery-confirm;
    # reuse the team's live stamp so the finalize doesn't mint a placeholder over it.
    if discovered:
        link_result = {"connection_esid": _team_live_google_ads_stamp(team_esid)}
    apply_result, redirect_or_resp = _run_onboarding_tokenless_finalize_with_bookkeeping(
        team_esid=team_esid,
        connector_id="google_ads",
        existing_connection_esid=_google_ads_backing_connection(link_result),
    )
    if apply_result is None:
        # authn precondition response, surface verbatim
        return redirect_or_resp
    if not apply_result.get("success"):
        return jsonify(
            {
                "success": False,
                "error": apply_result.get("error") or "Setup failed.",
                "error_kind": apply_result.get("error_kind"),
            }
        )
    return jsonify(
        {
            "success": True,
            "customer_esid": customer_esid,
            "connection_esid": apply_result.get("connection_esid"),
            "asset_count": apply_result.get("asset_count", 0),
            "first_look_status": apply_result.get("first_look_status"),
            "redirect": redirect_or_resp,
        }
    )


@data_connectors_bp.route("/api/wizard/ga4/onboarding-finalize", methods=["POST"])
def api_wizard_ga4_onboarding_finalize():
    """onboarding-scoped GA4 configure-then-finalize.

    Mirrors :func:`api_wizard_ga4_configure` but folds in the
    wizard_session step-7 bookkeeping + funnel events. See the Google
    Ads sibling for the rationale.

    Body: ``{"team_esid": "...", "property_esid": "..."}`` or
    ``{"team_esid": "...", "new_property": {...}}``.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err

    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    team_esid = (data.get("team_esid") or "").strip()
    property_esid = (data.get("property_esid") or "").strip() or None
    new_property = data.get("new_property") or None
    if bool(property_esid) == bool(new_property):
        return (
            jsonify({"success": False, "error": "Provide exactly one of property_esid or new_property"}),
            400,
        )
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err

    try:
        link_result = configure_ga4_property(
            team_esid=team_esid,
            property_esid=property_esid,
            new_property=new_property,
        )
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "GA4 team update failed",
            log_detail="api_wizard_ga4_onboarding_finalize team-doc update failed",
            exc=e,
        )

    apply_result, redirect_or_resp = _run_onboarding_tokenless_finalize_with_bookkeeping(
        team_esid=team_esid,
        connector_id="google_analytics_4",
    )
    if apply_result is None:
        return redirect_or_resp
    if not apply_result.get("success"):
        return jsonify(
            {
                "success": False,
                "error": apply_result.get("error") or "Setup failed.",
                "error_kind": apply_result.get("error_kind"),
            }
        )
    return jsonify(
        {
            "success": True,
            "property_esid": link_result.get("property_esid") or property_esid,
            "property_count": link_result.get("property_count"),
            "connection_esid": apply_result.get("connection_esid"),
            "asset_count": apply_result.get("asset_count", 0),
            "first_look_status": apply_result.get("first_look_status"),
            "redirect": redirect_or_resp,
        }
    )


@data_connectors_bp.route("/api/wizard/chatwoot/configure", methods=["POST"])
def api_wizard_chatwoot_configure():
    """Configure Chatwoot for a team. Body shape mirrors the Echo wizard."""
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    try:
        result = configure_chatwoot(
            team_esid=team_esid,
            account_esid=data.get("account_esid") or None,
            new_account=data.get("new_account") or None,
        )
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Chatwoot wizard failed",
            log_detail="api_wizard_chatwoot_configure failed",
            exc=e,
        )
    return jsonify({"success": True, **result})


@data_connectors_bp.route("/api/wizard/chatwoot/inboxes", methods=["GET"])
def api_wizard_chatwoot_inboxes():
    """the team's Chatwoot inboxes for the inbox picker, read live
    from the Chatwoot server with the account's stored token."""
    _require_data_connectors()
    team_esid = (request.args.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    try:
        result = list_chatwoot_inboxes(team_esid)
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Could not load Chatwoot inboxes",
            log_detail="api_wizard_chatwoot_inboxes failed",
            exc=e,
        )
    return jsonify({"success": True, **result})


@data_connectors_bp.route("/api/wizard/chatwoot/inboxes", methods=["POST"])
def api_wizard_chatwoot_inboxes_save():
    """save which Chatwoot inboxes belong to the team.
    Body: ``{"team_esid": "...", "inbox_ids": ["14", "18"]}``."""
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    try:
        result = set_chatwoot_inboxes(team_esid, data.get("inbox_ids"))
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Could not save Chatwoot inboxes",
            log_detail="api_wizard_chatwoot_inboxes_save failed",
            exc=e,
        )
    return jsonify({"success": True, **result})


def _gads_rules_response(fn, *args, what):
    """shared WizardError / unexpected-error handling for the
    Google Ads campaign-rules endpoints."""
    try:
        result = fn(*args)
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            f"Could not {what} Google Ads campaign rules", log_detail=f"gads campaign rules {what}", exc=e
        )
    return jsonify({"success": True, **result})


@data_connectors_bp.route("/api/wizard/google-ads/campaign-rules", methods=["GET"])
def api_wizard_google_ads_campaign_rules():
    """the team's campaign rules plus, per Google Ads account it
    shares, which team each imported campaign goes to."""
    _require_data_connectors()
    team_esid = (request.args.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    return _gads_rules_response(google_ads_campaign_rules, team_esid, what="load")


@data_connectors_bp.route("/api/wizard/google-ads/campaign-rules/preview", methods=["POST"])
def api_wizard_google_ads_campaign_rules_preview():
    """preview unsaved rules. Body: {team_esid, labels, prefixes}."""
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    return _gads_rules_response(
        preview_google_ads_campaign_rules, team_esid, data.get("labels"), data.get("prefixes"), what="preview"
    )


@data_connectors_bp.route("/api/wizard/google-ads/campaign-rules", methods=["POST"])
def api_wizard_google_ads_campaign_rules_save():
    """save the team's campaign rules. Body: {team_esid, labels, prefixes}."""
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    return _gads_rules_response(
        set_google_ads_campaign_rules, team_esid, data.get("labels"), data.get("prefixes"), what="save"
    )


@data_connectors_bp.route("/api/wizard/disciple-tools/configure", methods=["POST"])
def api_wizard_disciple_tools_configure():
    """configure a Disciple.Tools site for a team.

    Body: ``{"team_esid": "...", "dt_domain": "...", "our_domain": "...",
    "token": "..."}``. Live-verifies the Site-to-Site Link credential against
    the DT site before anything is written; on success the de_connection +
    dt_site rows the pull importer reads are upserted and the team's
    ``team_disciple_tools_instance`` is stamped (drives the Configured pill).

    SECURITY: the connection principal is the session username, never the
    request body.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    try:
        result = configure_disciple_tools(
            team_esid=team_esid,
            dt_domain=data.get("dt_domain", ""),
            our_domain=data.get("our_domain", ""),
            token=data.get("token", ""),
            username=username,
        )
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Disciple.Tools wizard failed",
            log_detail="api_wizard_disciple_tools_configure failed",
            exc=e,
        )
    # starter dashboards for a connector added outside the wizard.
    result.update(_install_starter_pack_for_team(team_esid, "disciple_tools", []))
    return jsonify({"success": True, **result})


@data_connectors_bp.route("/api/wizard/bitly/configure", methods=["POST"])
def api_wizard_bitly_configure():
    """Phase 3 — configure the Bitly connector for a team.

    Body: ``{"team_esid": "...", "api_token": "...", "tags": "a, b, c"}``
    (``tags`` may also be a JSON list). Live-verifies the token against
    Bitly's ``/v4/user`` before anything is written; on success the encrypted
    de_connection + bitly_account rows the pull importer reads are upserted
    and the team's ``ds_bitly`` + ``bitly_connection_esid`` are stamped.
    An empty ``api_token`` keeps the team's saved token (dots placeholder —
    the DT keep-if-unchanged pattern).

    SECURITY: the connection principal is the session username, never the
    request body. The token is never logged.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    try:
        result = configure_bitly(
            team_esid=team_esid,
            api_token=data.get("api_token", ""),
            tags=data.get("tags", ""),
            username=username,
        )
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Bitly wizard failed",
            log_detail="api_wizard_bitly_configure failed",
            exc=e,
        )
    return jsonify({"success": True, **result})


# ── YouTube wizard routes ──────────────────────────────────────────
#
# Custom 4-step flow (Account → Authorize → Channels → Confirm). Gated by the
# shared _require_data_connectors page permission, same as every other wizard
# endpoint. (The original superuser gate was lifted 2026-05-27 at
# the team's request — see the index comment + DECISIONS.md. The OAuth
# verification risk — ~100-grant cap, 7-day refresh-token expiry on the
# unverified client — now applies to any mc_data_connectors user.)


@data_connectors_bp.route("/api/wizard/youtube/oauth-start", methods=["POST"])
def api_wizard_youtube_oauth_start():
    """Step 1. Body: {account_esid?, new_account?}. Pick or create a YouTube
    account and return its OAuth status:
    {account_esid, auth_url_path, account_title, has_token, fboauth_hostname}.

    The frontend builds the popup URL as
    ``https://{fboauth_hostname}/youtube/{auth_url_path}`` (same shape as the
    FB app-user OAuth popup) and polls oauth-status for has_token.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    # IDOR gate: a client-supplied account_esid resolves to a
    # de_asset_youtube_accounts doc whose status/token we expose; verify it's in
    # the caller's workspace scope. Pick-new (no account_esid) is a no-op.
    authz_err = _check_account_asset_for_wizard("de_asset_youtube_accounts", data.get("account_esid") or "")
    if authz_err is not None:
        return authz_err
    try:
        result = youtube_oauth_pick_or_create_account(
            account_esid=data.get("account_esid") or None,
            new_account=data.get("new_account") or None,
            allowed_workspaces=_effective_allowed_workspaces(),  # reuse only own-workspace docs
            # stamp the creator so the oauth-status IDOR gate can allow
            # this user (and only this user) to poll their workspace-less account.
            username=session.get("username") or "",
        )
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        return jsonify({"success": False, "error": e.public_message}), 400
    return jsonify({"success": True, **result, "fboauth_hostname": _fboauth_hostname()})


@data_connectors_bp.route("/api/wizard/youtube/oauth-status/<account_esid>")
def api_wizard_youtube_oauth_status(account_esid: str):
    """Step 2 polling. Returns {account_esid, auth_url_path, account_title,
    has_token}.
    """
    _require_data_connectors()
    # IDOR gate: account_esid is a client-supplied path param.
    authz_err = _check_account_asset_for_wizard("de_asset_youtube_accounts", account_esid)
    if authz_err is not None:
        return authz_err
    return _wizard_call(youtube_account_status, account_esid=account_esid)


@data_connectors_bp.route("/api/wizard/youtube/list-channels", methods=["POST"])
def api_wizard_youtube_list_channels():
    """Step 3. Body: {account_esid}. Calls the YouTube Data API v3
    (channels.list(mine=True)) and returns {channels: [{id, title, handle}]}.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    # IDOR gate: account_esid (request body) backs the YouTube token
    # used to call channels.list; verify workspace scope first.
    authz_err = _check_account_asset_for_wizard("de_asset_youtube_accounts", data.get("account_esid") or "")
    if authz_err is not None:
        return authz_err
    try:
        channels = youtube_list_channels(account_esid=data.get("account_esid", ""))
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "YouTube list-channels failed",
            log_detail="api_wizard_youtube_list_channels failed",
            exc=e,
        )
    return jsonify({"success": True, "channels": channels})


@data_connectors_bp.route("/api/wizard/youtube/configure", methods=["POST"])
def api_wizard_youtube_configure():
    """Step 4. Body: {team_esid, account_esid, channels: [{id, title, handle}]}.
    Final link step — creates channel asset rows and flags the team
    ds_youtube=true with team_youtube_channel_handle_esids appended.

    after a successful link, runs the shared post-link finalize
    (first-look pack materialise + on-demand backfill kickoff) against the
     connection configure_youtube just wrote — the same core the
    Google Ads / GA4 configure endpoints run. Fail-soft: the channel/team
    link is the primary outcome and has already landed; a finalize failure
    is reported in the envelope (first_look_status / kickoff_ok) but never
    fails the wizard — the nightly cron remains the safety net.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    # the account carries the OAuth credential configure_youtube
    # decrypts and mirrors onto a new de_connection under the CALLER; gate it
    # exactly as the sibling oauth-status / list-channels routes do.
    # Deny collapses to the same generic 404.
    authz_err = _check_account_asset_for_wizard("de_asset_youtube_accounts", data.get("account_esid") or "")
    if authz_err is not None:
        return authz_err
    try:
        result = configure_youtube(
            team_esid=team_esid,
            account_esid=data.get("account_esid", ""),
            channels=data.get("channels") or [],
            # thread the real username so configure_youtube can
            # write a real-principal de_connection for the Manage surface.
            username=session.get("username") or "",
        )
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "YouTube configure failed",
            log_detail="api_wizard_youtube_configure failed",
            exc=e,
        )

    apply_result = _run_youtube_setup_kickoff(
        team_esid=team_esid,
        connection_esid=result.get("connection_esid"),
    )
    return jsonify(
        {
            "success": True,
            **result,
            "first_look_status": apply_result.get("first_look_status"),
            "kickoff_ok": bool(apply_result.get("kickoff_ok")),
        }
    )


def _run_youtube_setup_kickoff(*, team_esid: str, connection_esid: str | None) -> dict:
    """fail-soft wrapper around the shared post-link finalize for
    the two YouTube call sites (regular configure + onboarding-finalize).

    configure_youtube has already landed the channel/team link AND
    the real-principal de_connection when this runs; the finalize only adds
    the first-look pack + on-demand import kickoff. Any failure here —
    missing connection, authn edge, core exception — must not fail the
    wizard, so this never raises and always returns a dict (empty-shaped
    on failure) the caller can fold into its envelope / audit detail.
    """
    fallback = {"first_look_status": "skipped", "first_look_id": None, "kickoff_ok": False}
    try:
        apply_result, response_err = _run_tokenless_finalize(
            team_esid,
            "youtube",
            existing_connection_esid=(connection_esid or "").strip() or None,
        )
    except Exception as e:  # noqa: BLE001 — boundary; the link already landed
        logging.warning("youtube setup kickoff raised for team=%s: %s", team_esid, e)
        return fallback
    if response_err is not None or not isinstance(apply_result, dict):
        return fallback
    if not apply_result.get("kickoff_ok"):
        logging.warning(
            "youtube setup kickoff did not dispatch for team=%s: first_look=%s error_kind=%s",
            team_esid,
            apply_result.get("first_look_status"),
            apply_result.get("error_kind"),
        )
    return apply_result


@data_connectors_bp.route("/api/wizard/youtube/onboarding-finalize", methods=["POST"])
def api_wizard_youtube_onboarding_finalize():
    """onboarding-scoped YouTube finalize.

    Calls the same ``configure_youtube`` service the regular wizard does,
    then writes the onboarding wizard_session step-7 bookkeeping +
    audit / analytics events the marketing funnel expects (mirroring
    ``_finalize_tokenless_connector``), and returns a redirect URL the
    modal page navigates to. The JS only POSTs here when the page was
    opened with ``from_onboarding=1``; the regular ``configure``
    endpoint stays unchanged so the Data Connectors path keeps its
    no-redirect, no-session behaviour.

    Body: ``{team_esid, account_esid, channels: [{id, title, handle}]}``.

    after the link lands, runs the shared post-link finalize
    (first-look pack materialise + on-demand backfill kickoff) against
    the connection configure_youtube just wrote, so the user
    lands on a populated dashboard the same way Google Ads / GA4 do
    (closing the "nightly cron populates data" gap). Fail-soft:
    a finalize failure keeps the earlier behaviour — the step-7
    building screen still renders and the cron catches up.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err

    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    team_esid = data.get("team_esid", "")
    account_esid = data.get("account_esid", "")
    channels = data.get("channels") or []

    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    # same account gate as api_wizard_youtube_configure.
    authz_err = _check_account_asset_for_wizard("de_asset_youtube_accounts", account_esid)
    if authz_err is not None:
        return authz_err

    try:
        result = configure_youtube(
            team_esid=team_esid,
            account_esid=account_esid,
            channels=channels,
            # real-principal de_connection for the Manage surface.
            username=username,
        )
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "YouTube onboarding-finalize failed",
            log_detail="api_wizard_youtube_onboarding_finalize failed",
            exc=e,
        )

    selected_channel_ids = [c.get("id") for c in channels if c.get("id")]
    asset_count = len(selected_channel_ids)

    # first-look materialise + on-demand backfill kickoff, reusing
    # the connection configure_youtube just wrote. Runs BEFORE the audit event
    # so the event records the real first-look id + kickoff outcome instead of
    # the historical first_look=skipped / first_look_id=(none) placeholders.
    apply_result = _run_youtube_setup_kickoff(
        team_esid=team_esid,
        connection_esid=result.get("connection_esid"),
    )

    try:
        from web.services.audit_log_service import AuditLogService
    except ImportError:
        from ...services.audit_log_service import AuditLogService

    AuditLogService.log_event(
        username,
        "onboarding_step6_youtube_finalize",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            "analytics_event=wizard.step6.youtube_finalize "
            f"connector_id=youtube "
            f"team_esid={team_esid} "
            f"asset_count={asset_count} "
            f"first_look={apply_result.get('first_look_status') or 'skipped'} "
            f"first_look_id={apply_result.get('first_look_id') or '(none)'} "
            f"kickoff_ok={bool(apply_result.get('kickoff_ok'))}"
        ),
    )

    # Wizard-session step-7 bookkeeping (mirrors _finalize_tokenless_connector).
    try:
        from web.services.wizard_session_service import WizardSessionService
        from web.services.onboarding_branching_service import OnboardingBranchingService
        from web.services.user_profile_service import UserProfileService
    except ImportError:
        from ...services.wizard_session_service import WizardSessionService
        from ...services.onboarding_branching_service import OnboardingBranchingService
        from ...services.user_profile_service import UserProfileService

    try:
        account_type = (UserProfileService.get_account_type(username) or "").strip() or "org_owner"
    except Exception:
        account_type = "org_owner"

    try:
        active = WizardSessionService.get_active(username, "onboarding")
        state = dict((active or {}).get("state") or {})
        defaults = OnboardingBranchingService.for_account_type(account_type)
        state["selected_connector_id"] = "youtube"
        state["selected_assets"] = list(selected_channel_ids)
        state["friendly_names"] = {}
        state["org_shared"] = bool(defaults.step6_org_shared_default)
        state["current_step"] = "step-7"
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-7",
            state=state,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("wizard-session save failed for %r: %s", username, e)

    # WizardAnalyticsService funnel event — matches the step-4 POST
    # canonical-completed shape so the YouTube funnel slices the same as
    # every other tile.
    try:
        from web.services.wizard_analytics_service import (
            ACTION_COMPLETED,
            WizardAnalyticsService,
        )
    except ImportError:
        from ...services.wizard_analytics_service import (
            ACTION_COMPLETED,
            WizardAnalyticsService,
        )

    try:
        WizardAnalyticsService.emit(
            step="step-6",
            action=ACTION_COMPLETED,
            context=WizardAnalyticsService.build_context(
                username=username,
                wizard_session=active or None,
                account_type=account_type,
                connector_override="youtube",
            ),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("wizard-analytics emit failed for %r: %s", username, e)

    # Step-7 building page; ``_finalize_tokenless_connector``
    # builds the same URL via ``url_for("analytics.onboarding_step7_building_page", …)``.
    # The /onboarding/ path is reachable directly at nginx (no /assets/
    # prefix needed there).
    try:
        from flask import url_for as _url_for

        redirect_url = _url_for(
            "analytics.onboarding_step7_building_page",
            connector_id="youtube",
            asset_count=asset_count,
            next="/dashboard",
        )
    except Exception:  # noqa: BLE001 — fall back if url_for raises (no app context, etc.)
        redirect_url = f"/onboarding/step7/building?connector_id=youtube" f"&asset_count={asset_count}&next=/dashboard"
    return jsonify({"success": True, "redirect": redirect_url, **result})


# ── GA4 OAuth wizard routes ────────────────────────────────────────
#
# Custom 4-step flow (Account → Authorize → Properties → Confirm), mirroring the
# YouTube wizard. Gated by the shared _require_data_connectors page permission.
# The pre-existing tokenless ga4/configure + ga4/onboarding-finalize routes stay
# in place (service-account fallback path); these add the OAuth-grant path.


@data_connectors_bp.route("/api/wizard/ga4/oauth-start", methods=["POST"])
def api_wizard_ga4_oauth_start():
    """Step 1. Body: {account_esid?, new_account?}. Pick or create a GA4 account
    and return its OAuth status:
    {account_esid, auth_url_path, account_title, has_token, fboauth_hostname}.

    The frontend builds the popup URL as
    ``https://{fboauth_hostname}/ga4/{auth_url_path}`` and polls oauth-status
    for has_token.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    # IDOR gate: a client-supplied account_esid resolves to a
    # de_asset_ga4_accounts doc whose status/token we expose; verify workspace
    # scope. Pick-new (no account_esid) is a no-op.
    authz_err = _check_account_asset_for_wizard("de_asset_ga4_accounts", data.get("account_esid") or "")
    if authz_err is not None:
        return authz_err
    try:
        result = ga4_oauth_pick_or_create_account(
            account_esid=data.get("account_esid") or None,
            new_account=data.get("new_account") or None,
            allowed_workspaces=_effective_allowed_workspaces(),  # reuse only own-workspace docs
            # stamp the creator for the oauth-status gate (see YouTube).
            username=session.get("username") or "",
        )
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    return jsonify({"success": True, **result, "fboauth_hostname": _fboauth_hostname()})


@data_connectors_bp.route("/api/wizard/ga4/oauth-status/<account_esid>")
def api_wizard_ga4_oauth_status(account_esid: str):
    """Step 2 polling. Returns {account_esid, auth_url_path, account_title,
    has_token}.
    """
    _require_data_connectors()
    # IDOR gate (defense-in-depth, parity with youtube/oauth-status):
    # account_esid is a client-supplied path param.
    authz_err = _check_account_asset_for_wizard("de_asset_ga4_accounts", account_esid)
    if authz_err is not None:
        return authz_err
    return _wizard_call(ga4_account_status, account_esid=account_esid)


@data_connectors_bp.route("/api/wizard/ga4/list-properties", methods=["POST"])
def api_wizard_ga4_list_properties():
    """Step 3. Body: {account_esid}. Calls the GA4 Admin API
    (list_account_summaries) and returns {properties: [{id, title}]}.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    # IDOR gate: account_esid (request body) backs the GA4 token used
    # to call the Admin API; verify workspace scope first.
    authz_err = _check_account_asset_for_wizard("de_asset_ga4_accounts", data.get("account_esid") or "")
    if authz_err is not None:
        return authz_err
    try:
        properties = ga4_list_properties(account_esid=data.get("account_esid", ""))
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "GA4 list-properties failed",
            log_detail="api_wizard_ga4_list_properties failed",
            exc=e,
        )
    return jsonify({"success": True, "properties": properties})


@data_connectors_bp.route("/api/wizard/ga4/oauth-configure", methods=["POST"])
def api_wizard_ga4_oauth_configure():
    """Step 4. Body: {team_esid, account_esid, properties: [{id, title}]}.
    Final link step (OAuth path) — creates property asset rows, flags the team
    ds_google_analytics=true with team_ga4_property_esids appended, and writes a
    real-principal de_connection for the Manage surface.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    # the account carries the OAuth credential; gate it like the
    # sibling GA4 oauth-status / list-properties routes. Deny → 404.
    authz_err = _check_account_asset_for_wizard("de_asset_ga4_accounts", data.get("account_esid") or "")
    if authz_err is not None:
        return authz_err
    try:
        result = configure_ga4_oauth(
            team_esid=team_esid,
            account_esid=data.get("account_esid", ""),
            properties=data.get("properties") or [],
            username=session.get("username") or "",
        )
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "GA4 OAuth configure failed",
            log_detail="api_wizard_ga4_oauth_configure failed",
            exc=e,
        )
    return jsonify({"success": True, **result})


@data_connectors_bp.route("/api/wizard/ga4/oauth-onboarding-finalize", methods=["POST"])
def api_wizard_ga4_oauth_onboarding_finalize():
    """onboarding-scoped GA4 OAuth finalize.

    Calls the same ``configure_ga4_oauth`` service the regular wizard does, then
    writes the onboarding wizard_session step-7 bookkeeping + funnel events and
    returns a redirect URL the modal page navigates to. The JS only POSTs here
    when the page was opened with ``from_onboarding=1``. Mirrors
    :func:`api_wizard_youtube_onboarding_finalize`.

    Body: ``{team_esid, account_esid, properties: [{id, title}]}``.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err

    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    team_esid = data.get("team_esid", "")
    account_esid = data.get("account_esid", "")
    properties = data.get("properties") or []

    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    # same account gate as api_wizard_ga4_oauth_configure.
    authz_err = _check_account_asset_for_wizard("de_asset_ga4_accounts", account_esid)
    if authz_err is not None:
        return authz_err

    try:
        result = configure_ga4_oauth(
            team_esid=team_esid,
            account_esid=account_esid,
            properties=properties,
            username=username,
        )
    except WizardError as e:
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "GA4 OAuth onboarding-finalize failed",
            log_detail="api_wizard_ga4_oauth_onboarding_finalize failed",
            exc=e,
        )

    asset_count = len([p for p in properties if p.get("id")])

    try:
        from web.services.audit_log_service import AuditLogService
    except ImportError:
        from ...services.audit_log_service import AuditLogService

    AuditLogService.log_event(
        username,
        "onboarding_step6_ga4_oauth_finalize",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            "analytics_event=wizard.step6.ga4_oauth_finalize "
            f"connector_id=google_analytics_4 "
            f"team_esid={team_esid} "
            f"asset_count={asset_count} "
            f"first_look=skipped "
            f"first_look_id=(none)"
        ),
    )

    # Wizard-session step-7 bookkeeping (mirrors the YouTube finalize).
    try:
        from web.services.wizard_session_service import WizardSessionService
        from web.services.onboarding_branching_service import OnboardingBranchingService
        from web.services.user_profile_service import UserProfileService
    except ImportError:
        from ...services.wizard_session_service import WizardSessionService
        from ...services.onboarding_branching_service import OnboardingBranchingService
        from ...services.user_profile_service import UserProfileService

    try:
        account_type = (UserProfileService.get_account_type(username) or "").strip() or "org_owner"
    except Exception:
        account_type = "org_owner"

    try:
        active = WizardSessionService.get_active(username, "onboarding")
        state = dict((active or {}).get("state") or {})
        defaults = OnboardingBranchingService.for_account_type(account_type)
        state["selected_connector_id"] = "google_analytics_4"
        state["selected_assets"] = [p.get("id") for p in properties if p.get("id")]
        state["friendly_names"] = {}
        state["org_shared"] = bool(defaults.step6_org_shared_default)
        state["current_step"] = "step-7"
        WizardSessionService.save(
            user_id=username,
            wizard_type="onboarding",
            current_step="step-7",
            state=state,
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("wizard-session save failed for %r: %s", username, e)

    try:
        from web.services.wizard_analytics_service import (
            ACTION_COMPLETED,
            WizardAnalyticsService,
        )
    except ImportError:
        from ...services.wizard_analytics_service import (
            ACTION_COMPLETED,
            WizardAnalyticsService,
        )

    try:
        WizardAnalyticsService.emit(
            step="step-6",
            action=ACTION_COMPLETED,
            context=WizardAnalyticsService.build_context(
                username=username,
                wizard_session=active or None,
                account_type=account_type,
                connector_override="google_analytics_4",
            ),
            ip_address=request.remote_addr or "",
            user_agent=request.headers.get("User-Agent", ""),
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("wizard-analytics emit failed for %r: %s", username, e)

    try:
        from flask import url_for as _url_for

        redirect_url = _url_for(
            "analytics.onboarding_step7_building_page",
            connector_id="google_analytics_4",
            asset_count=asset_count,
            next="/dashboard",
        )
    except Exception:  # noqa: BLE001
        redirect_url = (
            f"/onboarding/step7/building?connector_id=google_analytics_4" f"&asset_count={asset_count}&next=/dashboard"
        )
    return jsonify({"success": True, "redirect": redirect_url, **result})


@data_connectors_bp.route("/api/wizard/ga4/connection-assets/<connection_esid>")
def api_wizard_ga4_connection_assets(connection_esid: str):
    """return the saved properties + status on a GA4 de_connection.

    Drives the GA4 card's connected-state summary. Mirrors
    :func:`api_wizard_youtube_connection_assets`: reads the ``ga4_property``
    ``de_connection_asset`` rows, with a team-legacy fallback when the
    connection has no asset rows yet. Authorisation reuses the connections
    blueprint's ``_viewer_can_see_connection`` gate (denied/missing → 404).

    Returns::

        {"success": True, "status": "active", "token_expires_at": "...",
         "properties": [{"provider_id": "123456789", "label": "..."}]}
    """
    _require_data_connectors()

    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    try:
        from web.services.connection_service import (
            ConnectionService,
            ASSET_TYPE_GA4_PROPERTY,
        )
        from web.blueprints.connections.routes import (
            _viewer_can_see_connection,
            _viewer_can_admin_connection,
        )
    except ImportError:
        from ...services.connection_service import (
            ConnectionService,
            ASSET_TYPE_GA4_PROPERTY,
        )
        from ..connections.routes import (
            _viewer_can_see_connection,
            _viewer_can_admin_connection,
        )

    source = ConnectionService.get_connection(connection_esid)
    if not source:
        return jsonify({"success": False, "error": "Connection not found"}), 404
    if not _viewer_can_see_connection(source, viewer_username=username):
        # in-scope admin restricted view (identity + unlink), no
        # properties (property listing needs the owner's token). See the YouTube
        # endpoint for the full rationale.
        if _viewer_can_admin_connection(source, viewer_username=username, connection_esid=connection_esid):
            return jsonify(
                {
                    "success": True,
                    "admin_view": True,
                    "status": source.get("status") or "",
                    "token_expires_at": source.get("token_expires_at"),
                    "account": (
                        str(source.get("label") or "").strip() or str(source.get("provider_account_id") or "").strip()
                    ),
                    "properties": [],
                }
            )
        return jsonify({"success": False, "error": "Connection not found"}), 404

    properties: list[dict] = []
    for hit in ConnectionService.list_assets_for_connection(connection_esid):
        s = hit.get("_source") or {}
        if (s.get("asset_type") or "") != ASSET_TYPE_GA4_PROPERTY:
            continue
        provider_id = str(s.get("provider_asset_id") or "").strip()
        if not provider_id:
            continue
        properties.append({"provider_id": provider_id, "label": str(s.get("label") or "").strip()})

    # Legacy fallback — same posture as the YouTube endpoint: only fill an empty
    # list, never override real connection_asset rows.
    team_esid = (request.args.get("team_esid") or "").strip()
    # team_esid is a query arg: resolve its scope once and use it for
    # the legacy fallback AND the esid stamp below (the stamp used to run for any
    # team, an esid oracle for another workspace's properties).
    team_in_scope = bool(team_esid) and (
        get_team_connectors(
            team_esid,
            allowed_workspaces=_effective_allowed_workspaces(),  # privileged callers narrowed to primary ws + grants
        )
        is not None
    )
    if team_in_scope and not properties:
        properties = _ga4_legacy_properties_for_team(team_esid)

    # stamp the de_asset ESID onto each property so the dialog's
    # per-property Remove button can target it via the endpoint, which
    # operates on the team's team_ga4_property_esids list. The connection_asset
    # rows above carry only the provider_asset_id (the GA4 property id), not the
    # ESID; resolve it from the team's linked properties by matching property id.
    # The legacy path already carries esid (see _ga4_legacy_properties_for_team).
    if team_in_scope:
        _stamp_esids_by_provider_id(
            properties,
            team_esid,
            link_field="team_ga4_property_esids",
            asset_index="de_asset_ga_properties",
            provider_id_field="ga_property_id",
        )

    return jsonify(
        {
            "success": True,
            "status": source.get("status") or "",
            "token_expires_at": source.get("token_expires_at"),
            # surface the connected Google account so the summary can
            # name it (mirrors the YouTube + Meta cards). Prefer the
            # human-readable label, fall back to the provider account id.
            "account": (str(source.get("label") or "").strip() or str(source.get("provider_account_id") or "").strip()),
            "properties": properties,
        }
    )


def _stamp_esids_by_provider_id(
    rows: list[dict],
    team_esid: str,
    link_field: str,
    asset_index: str,
    provider_id_field: str,
) -> None:
    """fill in each row's ``esid`` (the de_asset document id) from the
    team's linked assets, matching on the human provider id.

    The connection-asset rows that drive the YouTube/GA4 summary carry only the
    provider id (channel id / property id), not the de_asset ESID the per-account
    remove endpoint operates on. Build a ``{provider_id: esid}`` map
    from the team's link list (``link_field`` → ``asset_index`` docs) and stamp
    ``esid`` onto every row that matches. Rows already carrying an ``esid`` (the
    legacy/grouped paths) are left as-is; rows with no team-linked match get no
    ``esid`` and therefore no Remove button — correct, since they aren't in the
    team's link list. Best effort: any ES error leaves rows unstamped.
    """
    if not rows or not team_esid:
        return
    try:
        from lib.es_helpers import get_document_by_id
    except ImportError:  # pragma: no cover - import boundary
        return
    try:
        team = get_document_by_id("de_asset_teams", team_esid) or {}
    except Exception:
        return
    esids = team.get(link_field) or []
    if isinstance(esids, str):
        esids = [esids] if esids else []

    by_provider: dict[str, str] = {}
    for esid in esids:
        if not esid:
            continue
        try:
            doc = get_document_by_id(asset_index, esid) or {}
        except Exception:
            continue
        pid = str(doc.get(provider_id_field) or "").strip()
        if pid:
            by_provider[pid] = esid

    for row in rows:
        if row.get("esid"):
            continue
        match = by_provider.get(str(row.get("provider_id") or "").strip())
        if match:
            row["esid"] = match


def _ga4_legacy_properties_for_team(team_esid: str) -> list[dict]:
    """derive linked GA4 properties from a team's LEGACY assets, for
    teams whose de_connection has no de_connection_asset rows yet. Mirrors
    :func:`_youtube_legacy_channels_for_team`. The team points at its properties
    via ``team_ga4_property_esids`` → de_asset_ga_properties rows.
    """
    try:
        from lib.es_helpers import get_document_by_id
    except ImportError:
        return []

    team_doc = get_document_by_id("de_asset_teams", team_esid) or {}
    esids = team_doc.get("team_ga4_property_esids") or []
    if isinstance(esids, str):
        esids = [esids] if esids else []
    out: list[dict] = []
    for esid in esids:
        prop = get_document_by_id("de_asset_ga_properties", esid) or {}
        pid = str(prop.get("ga_property_id") or "").strip()
        if not pid:
            continue
        # de_asset ESID for the per-property Remove button.
        out.append({"esid": esid, "provider_id": pid, "label": str(prop.get("ga_property_title") or pid).strip()})
    return out


# ── Facebook wizard routes ───────────────────────────────────────────────────


def _wizard_call(fn, **kwargs):
    """Tiny helper: call a wizard service function and translate exceptions
    into the standard JSON envelope."""
    try:
        return jsonify({"success": True, **fn(**kwargs)})
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "FB wizard call failed",
            log_detail=f"_wizard_call({fn.__name__}) failed",
            exc=e,
        )


@data_connectors_bp.route("/api/wizard/fb/account", methods=["POST"])
def api_wizard_fb_account():
    """Step 1. Body: {account_esid?, new_account?}. Returns {fb_account_esid}."""
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err
    # a picked account_esid is a de_asset_fb_accounts doc that the
    # legacy configure step links; scope it like the other wizard accounts.
    authz_err = _check_account_asset_for_wizard("de_asset_fb_accounts", data.get("account_esid") or "")
    if authz_err is not None:
        return authz_err
    try:
        esid = fb_pick_or_create_account(
            account_esid=data.get("account_esid") or None,
            new_account=data.get("new_account") or None,
            allowed_workspaces=_effective_allowed_workspaces(),  # reuse only own-workspace docs
        )
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        return jsonify({"success": False, "error": e.public_message}), 400
    return jsonify({"success": True, "fb_account_esid": esid})


@data_connectors_bp.route("/api/wizard/fb/app-selection-mode", methods=["GET"])
def api_wizard_fb_app_selection_mode():
    """Tell the frontend whether to show or skip the App step.

    workspaces with no existing fb_apps skip the App step and
    use the environment's unified Meta app (``WIZARD_META_APP_ID``).
    Legacy workspaces that own one or more fb_apps keep the picker.

    Returns ``{"success": True, "mode": "skip", "fb_app_esid": "..."}``
    or ``{"success": True, "mode": "pick"}``. The same Layer-1
    (``current_user_has_any_scope``) + Layer-2
    (``get_allowed_workspaces``) scoping as ``api_wizard_options``
    applies so this surface can't be used to side-channel a different
    workspace's app inventory.
    """
    _require_data_connectors()

    # Same deny-all floor as the wizard's existing-app picker — a
    # scoped operator with no resolvable workspace falls through to
    # "pick" (legacy path), not "skip" (which would auto-bind their
    # unrelated workspace to the unified app).
    if not RoleFilterService.current_user_has_any_scope():
        return jsonify({"success": True, "mode": "pick"})

    allowed_workspaces = _effective_allowed_workspaces()  # privileged callers narrowed to primary ws + grants
    return jsonify({"success": True, **fb_app_selection_mode(allowed_workspaces)})


# ── Path A: de_connection-backed FB wizard endpoints ─────────────────
#
# Phase 3 (2026-05-28) removed the legacy /api/wizard/fb/app +
# /api/wizard/fb/app-user + /api/wizard/fb/app-user/<esid>/status routes that
# drove the pre-Path-A 5-step wizard. The Path A wizard never calls them, and
# Phase 2 deployed atomically so there's no in-flight frontend session that
# would touch them. The dual-mode /list-pages and /configure endpoints
# (below) still accept legacy bodies for prod-fallback safety per the plan's
# Risk Register R8 (the existing Kavanah Media de_asset_fb_app_users records
# on prod remain functional).


@data_connectors_bp.route("/api/wizard/fb/oauth-start", methods=["POST"])
def api_wizard_fb_oauth_start():
    """Path A — start the de_connection-backed OAuth roundtrip.

    Body: ``{}`` (no required fields — the username comes from the
    session). Mints a pending_token de_connection row, builds the Meta
    authorize URL, stashes state on flask.session keyed by
    connection_esid (same pattern as the reauth flow), and
    returns ``{oauth_url, connection_esid}``. The frontend opens
    ``oauth_url`` in a popup; the shared callback at
    ``analytics.onboarding_step7_oauth_callback`` detects the
    ``oauth_dc_active_esid`` marker and upserts the real token onto the
    same de_connection row.

    The Meta-app redirect URI is unchanged
    (``/onboarding/step-7/oauth-callback``) — the callback branches on
    a Flask-session marker, so the data-connectors flow reuses the
    pre-registered URI rather than adding a fourth one.
    """
    _require_data_connectors()

    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    try:
        from web.services.wizard_oauth_service import WizardOAuthService
    except ImportError:
        from ...services.wizard_oauth_service import WizardOAuthService

    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None:
        return jsonify({"success": False, "error": "Meta app not configured"}), 503

    try:
        pending = fb_create_pending_connection(username=username)
    except WizardError as e:
        # WizardError carries operator-facing validation messages.
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "FB oauth-start failed",
            log_detail="api_wizard_fb_oauth_start pending-connection write failed",
            exc=e,
        )

    connection_esid = pending["connection_esid"]
    scopes = pending["scopes"]

    # Mint state token and stash on flask.session keyed by connection_esid
    # so a multi-tab user opening two wizards doesn't clobber each other's
    # state. Mirrors reauth_state keys; the callback pops by the
    # key it reads from session["oauth_dc_active_esid"].
    import secrets

    oauth_state = secrets.token_urlsafe(32)
    session[f"oauth_dc_state:{connection_esid}"] = oauth_state
    session[f"oauth_dc_app_esid:{connection_esid}"] = meta_app.esid
    session["oauth_dc_active_esid"] = connection_esid

    from flask import url_for

    # Path A — the OAuth callback URL must match the form
    # registered in the Meta App Dashboard, which is the BARE
    # ``https://<host>/onboarding/step-7/oauth-callback`` (no /assets/
    # prefix). Onboarding's request flows in via bare /onboarding/...
    # so its url_for builds the bare form; ours flows in via
    # /assets/data-connectors/... so SCRIPT_NAME=/assets and url_for
    # would prepend the prefix. Same canonicalisation as
    # _mfa_redirect — build the URL then strip /assets back off.
    # nosemgrep: flask-url-for-external-true - OAuth redirect_uri must be absolute; host fixed by nginx
    redirect_uri = url_for("analytics.onboarding_step7_oauth_callback", _external=True)
    redirect_uri = redirect_uri.replace("/assets/onboarding/", "/onboarding/", 1)
    authorize_url = WizardOAuthService.build_authorize_url(
        client_id=meta_app.client_id,
        redirect_uri=redirect_uri,
        state=oauth_state,
        scopes=scopes,
        reauthenticate=False,
    )
    return jsonify(
        {
            "success": True,
            "oauth_url": authorize_url,
            "connection_esid": connection_esid,
            "scopes": list(scopes),
        }
    )


@data_connectors_bp.route("/api/wizard/fb/oauth-status/<connection_esid>")
def api_wizard_fb_oauth_status(connection_esid: str):
    """Path A — poll a de_connection for callback completion.

    Returns ``{connection_esid, status, has_token, token_expires_at}``.
    The frontend polls this until ``has_token=true``, then advances to
    the Accounts step.
    """
    _require_data_connectors()
    # IDOR gate: connection_esid is a client-supplied path param; a
    # caller could poll another principal's connection status without it.
    authz_err = _check_connection_for_wizard(connection_esid)
    if authz_err is not None:
        return authz_err
    return _wizard_call(fb_connection_status, connection_esid=connection_esid)


@data_connectors_bp.route("/api/wizard/fb/list-ad-accounts", methods=["POST"])
def api_wizard_fb_list_ad_accounts():
    """Path A Phase 1 addendum — Accounts-step pick-from-list.

    Body: ``{"connection_esid": "..."}``. Calls Meta Graph
    ``/me/adaccounts`` with the connection's decrypted token and
    returns ``{accounts: [{id, name, account_status}]}`` sorted by
    name. Mirrors :func:`fb_list_pages_for_connection`'s shape.

    The frontend single-selects one account and passes its ``id``
    (``act_NNN``) to the Confirm step as ``fb_account_provider_id``.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err

    connection_esid = (data.get("connection_esid") or "").strip()
    if not connection_esid:
        return jsonify({"success": False, "error": "connection_esid is required"}), 400

    # IDOR gate: connection_esid comes from the request body and the
    # call reads the connection's decrypted token to hit Meta Graph. Verify the
    # caller owns / can see the connection first, mirroring fb/connection-assets.
    authz_err = _check_connection_for_wizard(connection_esid)
    if authz_err is not None:
        return authz_err

    try:
        accounts = fb_list_ad_accounts_for_connection(connection_esid=connection_esid)
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        # the 400 goes to the browser and used to leave NO server-side
        # trace, so a wizard that stalled here was undiagnosable after the fact
        # (nine failed prod attempts, 2026-08-03, with nothing in the log).
        logging.warning(
            "wizard/fb/list-ad-accounts: WizardError for connection=%s user=%s: %s",
            connection_esid,
            session.get("username"),
            e,
        )
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "FB list-ad-accounts failed",
            log_detail="api_wizard_fb_list_ad_accounts failed",
            exc=e,
        )
    # an EMPTY list is the quietest failure in the wizard: the step
    # renders with nothing to select, advanceFromAccounts hard-blocks, Confirm is
    # unreachable, and the finisher (the only writer of ds_facebook + the team
    # links) never runs. Indistinguishable in the log from a successful call.
    if not accounts:
        logging.warning(
            "wizard/fb/list-ad-accounts: /me/adaccounts returned ZERO "
            "accounts for connection=%s user=%s — the Accounts step will render "
            "empty and the wizard cannot advance to Confirm.",
            connection_esid,
            session.get("username"),
        )

    # with a team context, annotate each account with intent signals:
    # linked_to_team (this team's OPEN fb_ad_account_links → the wizard
    # pre-checks exactly these) and used_by_teams (other teams' open links →
    # rendered as an informational label, never a block). team_esid is optional
    # so the endpoint stays backward-compatible; it is authz-checked because it
    # arrives from the request body and drives a cross-team read.
    team_esid = (data.get("team_esid") or "").strip()
    if team_esid:
        authz_err = _check_team_for_wizard(team_esid)
        if authz_err is not None:
            return authz_err
        try:
            from web.services.connector_wizard_service import annotate_ad_accounts_with_team_links
        except ImportError:
            from ...services.connector_wizard_service import (  # type: ignore[no-redef]
                annotate_ad_accounts_with_team_links,
            )
        accounts = annotate_ad_accounts_with_team_links(accounts, team_esid)

    return jsonify({"success": True, "accounts": accounts})


def _fb_legacy_assets_for_team(team_esid: str) -> dict:
    """derive accounts + page ids from a team's LEGACY linked
    assets, for wizard-cohort teams that have no de_connection_asset rows.

    The team doc points at its FB/IG assets three ways:
      * ``fb_ad_account_esid`` → a ``de_asset_fb_ad_accounts`` doc
        (fb_ad_account_id + fb_ad_account_title). That single account is
        the active-import one (the importer reads one per team), so it's
        flagged is_active_import=True.
      * ``team_facebook_page_esids`` → ``de_asset_fb_pages`` docs
        (fb_page_id each).
      * ``team_instagram_profile_esids`` → ``de_asset_ig_accounts`` docs
        .

    every returned row carries its de_asset ``esid`` so the
    summary dialog can render a per-account "Remove from this team" button
    (the remove endpoint operates on the esid). Pages are returned both as
    bare ``page_ids`` (the legacy count contract, unchanged) AND as ``pages``
    row dicts (provider_id + esid + name) for the removable rendering.

    Returns ``{"accounts": [...], "page_ids": [...], "pages": [...],
    "ig_accounts": [...]}`` — a superset of the de_connection_asset path so
    the caller can swap it in. Best effort: missing docs / ES errors yield
    empty lists.
    """
    try:
        from lib.es_helpers import get_document_by_id
    except ImportError:  # pragma: no cover - import boundary
        return {"accounts": [], "page_ids": [], "pages": [], "ig_accounts": []}

    team = None
    try:
        team = get_document_by_id("de_asset_teams", team_esid)
    except Exception:
        team = None
    if not team:
        return {"accounts": [], "page_ids": [], "pages": [], "ig_accounts": []}

    accounts: list[dict] = []
    ad_esid = (team.get("fb_ad_account_esid") or "").strip()
    if ad_esid:
        try:
            ad = get_document_by_id("de_asset_fb_ad_accounts", ad_esid)
        except Exception:
            ad = None
        if ad:
            bare = str(ad.get("fb_ad_account_id") or "").strip()
            if bare:
                provider_id = bare if bare.startswith("act_") else f"act_{bare}"
                accounts.append(
                    {
                        # the de_asset ESID the per-account Remove
                        # endpoint targets (here it's the team's scalar field).
                        "esid": ad_esid,
                        "provider_id": provider_id,
                        "name": str(ad.get("fb_ad_account_title") or "").strip(),
                        "is_active_import": True,
                    }
                )

    page_ids: list[str] = []
    pages: list[dict] = []
    page_esids = team.get("team_facebook_page_esids") or []
    if isinstance(page_esids, str):
        page_esids = [page_esids] if page_esids else []
    for pe in page_esids:
        if not pe:
            continue
        try:
            pg = get_document_by_id("de_asset_fb_pages", pe)
        except Exception:
            pg = None
        if not pg:
            continue
        pid = str(pg.get("fb_page_id") or "").strip()
        if pid:
            page_ids.append(pid)
            pages.append(
                {
                    "esid": pe,
                    "provider_id": pid,
                    "name": str(pg.get("fb_page_name") or "").strip(),
                }
            )

    ig_accounts: list[dict] = []
    ig_esids = team.get("team_instagram_profile_esids") or []
    if isinstance(ig_esids, str):
        ig_esids = [ig_esids] if ig_esids else []
    for ie in ig_esids:
        if not ie:
            continue
        try:
            ig = get_document_by_id("de_asset_ig_accounts", ie)
        except Exception:
            ig = None
        if not ig:
            continue
        ig_accounts.append(
            {
                "esid": ie,
                "provider_id": str(ig.get("ig_account_id") or ie).strip(),
                "name": str(ig.get("ig_account_title") or "").strip(),
            }
        )

    return {"accounts": accounts, "page_ids": page_ids, "pages": pages, "ig_accounts": ig_accounts}


def _annotate_ig_importability(ig_accounts: list[dict], team_esid: str) -> list[dict]:
    """mark IG rows that LOOK connected but will never import.

    Discovery ≠ importability: the wizard finds an IG through its page's
    ``instagram_business_account`` edge (page access alone allows this), but
    a credential that cannot read the IG itself (not in the Business
    portfolio / not assigned to the system user) imports nothing — silently,
    forever (one team's Instagram-account case). Joins the team's DURABLE
    credential-health rows — the sweep's per-asset probes — so this
    costs zero live Meta calls. only probes from credentials the IG
    importer actually tries for THAT account count (``ig_import_ladder``:
    the IG doc's ``wizard_connection_esid`` first, then the portfolio-matched
    legacy app users), and any one success is importable because the importer
    stops at the first working token:

      * ``importable: true``  — the last probe read this IG successfully;
      * ``importable: false`` — the probe failed; ``import_note`` says why:
        ``"page_failed"`` when the parent page itself failed (the IG failure
        is a consequence, per the prober's caused_by), else
        ``"not_reachable"`` (the IG is outside the portfolio / unassigned);
      * ``importable: null``  — never probed (fresh link before the next
        sweep); the UI stays neutral rather than claiming either way.

    Best-effort: any failure leaves rows unannotated, never blocks the
    summary.
    """
    if not ig_accounts or not team_esid:
        return ig_accounts
    try:
        from lib import credential_health as ch
        from lib.es_helpers import get_document_by_id

        # judge each IG by the credentials the IMPORTER tries for it,
        # not by whichever probe happened to be first. The team's health rows
        # hold both families; a wizard probe that reads the IG fine says nothing
        # when the importer authenticates that IG through a system-user token
        # that cannot (the one-team shape above, which first-wins painted green).
        rows = ch.health_for_fb_team(team_esid)
        for ig in ig_accounts:
            ig_doc = get_document_by_id("de_asset_ig_accounts", str(ig.get("esid") or "")) or {}
            ladder = ch.ig_import_ladder(ig_doc, rows)
            importable, note = ch.ig_importability(str(ig.get("provider_id") or ""), ladder)
            ig["importable"] = importable
            if importable is False:
                ig["import_note"] = note
    except Exception as e:  # noqa: BLE001 — annotation must never break the summary
        logging.warning("IG importability annotation failed for team %s: %s", team_esid, e)
    return ig_accounts


def _youtube_legacy_channels_for_team(team_esid: str) -> list[dict]:
    """derive linked channels from a team's LEGACY assets, for
    teams whose de_connection has no de_connection_asset rows yet.

    The Phase-77 writer creates the de_connection_asset rows on a fresh
    configure run, so this fallback only fires for a team whose
    connection_esid resolved (real-principal row exists) but whose channel
    asset rows weren't written (e.g. a partial/interrupted finish). The
    team points at its channels via ``team_youtube_channel_handle_esids`` →
    ``de_asset_youtube_channels`` docs (youtube_channel_id + _title/_handle).

    Returns ``[{"provider_id": <channel id>, "label": <title>}]`` — the same
    shape as the de_connection_asset path so the caller can swap it in.
    Best effort: missing docs / ES errors yield an empty list.
    """
    try:
        from lib.es_helpers import get_document_by_id
    except ImportError:  # pragma: no cover - import boundary
        return []

    try:
        team = get_document_by_id("de_asset_teams", team_esid)
    except Exception:
        team = None
    if not team:
        return []

    channel_esids = team.get("team_youtube_channel_handle_esids") or []
    if isinstance(channel_esids, str):
        channel_esids = [channel_esids] if channel_esids else []

    channels: list[dict] = []
    for ce in channel_esids:
        if not ce:
            continue
        try:
            ch = get_document_by_id("de_asset_youtube_channels", ce)
        except Exception:
            ch = None
        if not ch:
            continue
        cid = str(ch.get("youtube_channel_id") or "").strip()
        if not cid:
            continue
        channels.append(
            {
                # the de_asset ESID, so the dialog's per-channel Remove
                # button can target this channel via the endpoint.
                "esid": ce,
                "provider_id": cid,
                # surface the human-facing @handle so the summary's
                # secondary line can show it instead of the opaque UC… id.
                "handle": str(ch.get("youtube_channel_handle") or "").strip(),
                "label": str(ch.get("youtube_channel_title") or ch.get("youtube_channel_handle") or cid).strip(),
            }
        )
    return channels


def _youtube_accounts_grouped_for_team(team_esid: str) -> list[dict]:
    """the team's linked YouTube channels GROUPED by their parent
    account, so the connected-state dialog can show ALL accounts (a team may
    connect more than one), not just the one resolved from the scalar
    connection_esid.

    Walks ``team_youtube_channel_handle_esids`` → ``de_asset_youtube_channels``
    (each carries ``youtube_account_esid``), resolves each account's display
    name (title → email → esid; never blank), and groups the channels under it.
    Channels with no parent account fall under a synthetic "unassigned" group
    keyed by ``""`` so they are never silently dropped.

    Returns
    ``[{"account_esid", "account_name", "channels": [{"provider_id","label"}]}]``
    ordered by account_name. Best effort: missing docs / ES errors yield [].
    """
    try:
        from lib.es_helpers import get_document_by_id
    except ImportError:  # pragma: no cover - import boundary
        return []

    try:
        team = get_document_by_id("de_asset_teams", team_esid)
    except Exception:
        team = None
    if not team:
        return []

    channel_esids = team.get("team_youtube_channel_handle_esids") or []
    if isinstance(channel_esids, str):
        channel_esids = [channel_esids] if channel_esids else []

    # account_esid -> {"account_esid", "account_name", "channels": [...]}
    groups: dict[str, dict] = {}
    name_cache: dict[str, str] = {}

    def _account_name(account_esid: str) -> str:
        if account_esid in name_cache:
            return name_cache[account_esid]
        name = account_esid  # esid fallback (last resort)
        if account_esid:
            try:
                acct = get_document_by_id("de_asset_youtube_accounts", account_esid) or {}
            except Exception:
                acct = {}
            # title -> email -> esid (never blank)
            name = (
                str(acct.get("youtube_account_title") or "").strip()
                or str(acct.get("youtube_account_email") or "").strip()
                or account_esid
            )
            # one Google login can front many Brand Account
            # identities, and each needs its own doc (one doc = one token = one
            # identity). Title and email are IDENTICAL across those
            # docs, so this heading rendered the same string twice and the user
            # could not tell which group served which channel. Suffix the owning
            # identity's channels, exactly as the picker does.
            try:
                from web.services.connector_wizard_service import _youtube_picker_label
            except ImportError:
                from ...services.connector_wizard_service import _youtube_picker_label
            name = _youtube_picker_label(name, acct)
        name_cache[account_esid] = name
        return name

    for ce in channel_esids:
        if not ce:
            continue
        try:
            ch = get_document_by_id("de_asset_youtube_channels", ce)
        except Exception:
            ch = None
        if not ch:
            continue
        cid = str(ch.get("youtube_channel_id") or "").strip()
        if not cid:
            continue
        account_esid = str(ch.get("youtube_account_esid") or "").strip()
        grp = groups.get(account_esid)
        if grp is None:
            grp = {
                "account_esid": account_esid,
                "account_name": _account_name(account_esid) if account_esid else "Unassigned",
                "channels": [],
            }
            groups[account_esid] = grp
        grp["channels"].append(
            {
                # de_asset ESID for the per-channel Remove button.
                "esid": ce,
                "provider_id": cid,
                # see _youtube_legacy_channels_for_team.
                "handle": str(ch.get("youtube_channel_handle") or "").strip(),
                "label": str(ch.get("youtube_channel_title") or ch.get("youtube_channel_handle") or cid).strip(),
            }
        )

    return sorted(groups.values(), key=lambda g: (g["account_name"] or "").lower())


@data_connectors_bp.route("/api/wizard/youtube/connection-assets/<connection_esid>")
def api_wizard_youtube_connection_assets(connection_esid: str):
    """return the saved channels + status on a YouTube de_connection.

    Drives the YouTube card's connected-state **summary** (shown when a team
    already has a resolved ``connection_esid`` instead of re-running OAuth).
    Mirrors :func:`api_wizard_fb_connection_assets`: reads the
    ``youtube_channel`` ``de_connection_asset`` rows (written by the Phase-77
    configure path), with a team-legacy fallback when the connection has no
    asset rows yet.

    Returns::

        {"success": True,
         "status": "active",
         "token_expires_at": "...",
         "channels": [{"provider_id": "UC...", "label": "..."}]}

    Authorisation reuses the connections blueprint's
    ``_viewer_can_see_connection`` gate, collapsing a denied / missing
    connection into a 404 (same enumeration defence as the detail page) so a
    viewer who isn't the principal sees the inaccessible-degrade path.
    """
    _require_data_connectors()

    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    try:
        from web.services.connection_service import (
            ConnectionService,
            ASSET_TYPE_YOUTUBE_CHANNEL,
        )
        from web.blueprints.connections.routes import (
            _viewer_can_see_connection,
            _viewer_can_admin_connection,
        )
    except ImportError:
        from ...services.connection_service import (
            ConnectionService,
            ASSET_TYPE_YOUTUBE_CHANNEL,
        )
        from ..connections.routes import (
            _viewer_can_see_connection,
            _viewer_can_admin_connection,
        )

    source = ConnectionService.get_connection(connection_esid)
    if not source:
        return jsonify({"success": False, "error": "Connection not found"}), 404
    if not _viewer_can_see_connection(source, viewer_username=username):
        # the viewer isn't the principal, but an in-scope
        # workspace/org admin may still SEE the connected account identity and
        # UNLINK it from the team (channel editing stays principal-only — it
        # needs the owner's token). Return a restricted admin view: identity +
        # status, no channels. A true outsider still collapses to 404 (the
        # enumeration defence), which the frontend reads as Reconnect-only.
        if _viewer_can_admin_connection(source, viewer_username=username, connection_esid=connection_esid):
            return jsonify(
                {
                    "success": True,
                    "admin_view": True,
                    "status": source.get("status") or "",
                    "token_expires_at": source.get("token_expires_at"),
                    "account": (
                        str(source.get("label") or "").strip() or str(source.get("provider_account_id") or "").strip()
                    ),
                    "accounts": [],
                    "channels": [],
                }
            )
        return jsonify({"success": False, "error": "Connection not found"}), 404

    channels: list[dict] = []
    for hit in ConnectionService.list_assets_for_connection(connection_esid):
        s = hit.get("_source") or {}
        if (s.get("asset_type") or "") != ASSET_TYPE_YOUTUBE_CHANNEL:
            continue
        provider_id = str(s.get("provider_asset_id") or "").strip()
        if not provider_id:
            continue
        channels.append({"provider_id": provider_id, "label": str(s.get("label") or "").strip()})

    # Legacy fallback — same posture as the FB endpoint: only fill an empty
    # list, never override real connection_asset rows.
    team_esid = (request.args.get("team_esid") or "").strip()
    team_visible = False
    if team_esid:
        team_payload = get_team_connectors(
            team_esid,
            allowed_workspaces=_effective_allowed_workspaces(),  # privileged callers narrowed to primary ws + grants
        )
        team_visible = team_payload is not None
        if team_visible and not channels:
            channels = _youtube_legacy_channels_for_team(team_esid)

    # stamp the de_asset ESID onto the flat connection-asset channels
    # (the grouped `accounts` and legacy fallback already carry it) so the
    # dialog's per-channel Remove button can target it. Matches the channel's
    # provider id back to a team-linked de_asset_youtube_channels row.
    if team_esid and team_visible:
        _stamp_esids_by_provider_id(
            channels,
            team_esid,
            link_field="team_youtube_channel_handle_esids",
            asset_index="de_asset_youtube_channels",
            provider_id_field="youtube_channel_id",
        )

    # a team can connect MORE THAN ONE YouTube account, but the
    # team stamps a single scalar connection_esid (overwritten per add), so the
    # connection above resolves only one account. Group the team's full linked
    # channel set by parent account so the dialog shows ALL accounts, each
    # named. Authoritative source = the team's linked channels (what the
    # importer pulls), not one connection's asset rows. Requires team_esid +
    # the viewer being allowed to see the team (same gate as the fallback).
    accounts_grouped: list[dict] = []
    if team_esid and team_visible:
        accounts_grouped = _youtube_accounts_grouped_for_team(team_esid)

    return jsonify(
        {
            "success": True,
            "status": source.get("status") or "",
            "token_expires_at": source.get("token_expires_at"),
            # surface the connected Google account so the summary can
            # name it (like the Meta card names the ad-account). Prefer the
            # human-readable label, fall back to the provider account id (the
            # account email the YouTube de_connection is keyed on).
            "account": (str(source.get("label") or "").strip() or str(source.get("provider_account_id") or "").strip()),
            # all of the team's accounts grouped + named. Additive:
            # the flat `channels` (this connection's) stays for back-compat; the
            # frontend prefers `accounts` when present.
            "accounts": accounts_grouped,
            "channels": channels,
        }
    )


@data_connectors_bp.route("/api/wizard/fb/connection-assets/<connection_esid>")
def api_wizard_fb_connection_assets(connection_esid: str):
    """return the saved accounts + pages on a de_connection.

    Drives two surfaces in the data-connectors FB wizard:

    * the connected-state **summary** shown when a team already has a
      resolved ``connection_esid`` (instead of re-running OAuth), and
    * **pre-loading** the Accounts / Pages step selections when the user
      clicks "Edit accounts & pages".

    Reads ``de_connection_asset`` rows via
    ``ConnectionService.list_assets_for_connection`` and splits them by
    ``asset_type``. The fb_ad_account rows carry the friendly name
    (``label``) and the ``metadata.is_active_import`` flag stamped at
    finish time (see ``_upsert_connection_assets``).

    Returns::

        {"success": True,
         "status": "active",
         "token_expires_at": "...",
         "accounts": [{"provider_id": "act_NNN", "name": "...",
                       "is_active_import": true}],
         "page_ids": ["123", ...]}

    Authorisation reuses the connections blueprint's
    ``_viewer_can_see_connection`` gate (principal-or-workspace-scope),
    collapsing a denied / missing connection into the same 404 as the
    detail page so this can't enumerate connection esids across tenants.
    """
    _require_data_connectors()

    username = session.get("username") or ""
    if not username:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    try:
        from web.services.connection_service import (
            ConnectionService,
            ASSET_TYPE_FB_AD_ACCOUNT,
            ASSET_TYPE_FB_PAGE,
        )
        from web.blueprints.connections.routes import (
            _viewer_can_see_connection,
            _viewer_can_admin_connection,
        )
    except ImportError:
        from ...services.connection_service import (
            ConnectionService,
            ASSET_TYPE_FB_AD_ACCOUNT,
            ASSET_TYPE_FB_PAGE,
        )
        from ..connections.routes import (
            _viewer_can_see_connection,
            _viewer_can_admin_connection,
        )

    source = ConnectionService.get_connection(connection_esid)
    if not source:
        return jsonify({"success": False, "error": "Connection not found"}), 404
    if not _viewer_can_see_connection(source, viewer_username=username):
        # in-scope admin restricted view (identity + unlink), no
        # accounts/pages (editing the linked assets stays principal-only). See
        # the YouTube endpoint for the full rationale.
        if _viewer_can_admin_connection(source, viewer_username=username, connection_esid=connection_esid):
            return jsonify(
                {
                    "success": True,
                    "admin_view": True,
                    "status": source.get("status") or "",
                    "token_expires_at": source.get("token_expires_at"),
                    "account": (
                        str(source.get("label") or "").strip() or str(source.get("provider_account_id") or "").strip()
                    ),
                    "team_accounts": [],
                    "accounts": [],
                    "page_ids": [],
                    "pages": [],
                    "ig_accounts": [],
                }
            )
        return jsonify({"success": False, "error": "Connection not found"}), 404

    accounts: list[dict] = []
    page_ids: list[str] = []
    for hit in ConnectionService.list_assets_for_connection(connection_esid):
        s = hit.get("_source") or {}
        asset_type = s.get("asset_type") or ""
        provider_id = str(s.get("provider_asset_id") or "").strip()
        if not provider_id:
            continue
        if asset_type == ASSET_TYPE_FB_AD_ACCOUNT:
            meta = s.get("metadata") or {}
            accounts.append(
                {
                    "provider_id": provider_id,
                    "name": str(s.get("label") or "").strip(),
                    "is_active_import": bool(meta.get("is_active_import")),
                }
            )
        elif asset_type == ASSET_TYPE_FB_PAGE:
            page_ids.append(provider_id)

    # teams wired before the helper wrote
    # de_connection_asset rows (the existing wizard cohort, e.g. Kavanah
    # Media) have a real de_connection but zero connection_asset rows, so
    # the lists above come back empty and the summary / Edit pre-load
    # would show nothing. When ``team_esid`` is supplied (the frontend
    # passes the selected team) and the viewer can see it, derive the
    # accounts + page ids from the team's legacy linked assets
    # (de_asset_fb_ad_accounts / de_asset_fb_pages) so the connected
    # surface is populated without a data backfill. Only fills a list
    # that came back empty — never overrides real connection_asset rows.
    # the connected SUMMARY is a TEAM view (matching every other
    # connector: Google Ads / YouTube / GA4 show the team's linked assets and
    # Remove unlinks from the team — list and action over the same store). The
    # ``accounts`` list above is the CONNECTION's saved ad accounts, which is
    # the right source for PRE-LOADING the Edit step's multi-select (you pick
    # which saved accounts to link), but the WRONG source for the summary's
    # removable rows. So ``accounts``/``page_ids`` stay connection-sourced for
    # Edit, and the summary renders the team-linked rows below:
    #   * team_accounts — the team's linked ad account(s) (fb_ad_account_esid),
    #   * pages         — the team's linked FB pages,
    #   * ig_accounts   — the team's linked IG accounts.
    # All three carry the de_asset esid the remove endpoint targets, so Remove
    # unlinks and the row vanishes on the next summary fetch.
    pages: list[dict] = []
    ig_accounts: list[dict] = []
    team_accounts: list[dict] = []
    team_esid = (request.args.get("team_esid") or "").strip()
    if team_esid:
        team_payload = get_team_connectors(
            team_esid,
            allowed_workspaces=_effective_allowed_workspaces(),  # privileged callers narrowed to primary ws + grants
        )
        if team_payload is not None:
            legacy = _fb_legacy_assets_for_team(team_esid)
            # fill the Edit-preload lists when the connection has no
            # de_connection_asset rows yet (wizard cohort); never override real
            # connection rows.
            if not accounts:
                accounts = legacy["accounts"]
            if not page_ids:
                page_ids = legacy["page_ids"]
            # the summary's removable rows always come from the
            # team's link lists, esid-stamped (the remove endpoint targets the
            # esid on the team doc, not a connection-asset provider id).
            team_accounts = legacy["accounts"]
            pages = legacy["pages"]
            # IG rows carry importable/import_note so the summary
            # can say when a page-edge-discovered IG will never import.
            ig_accounts = _annotate_ig_importability(legacy["ig_accounts"], team_esid)

    # Surface as active whichever account is flagged; if none is flagged
    # (legacy single-account writes predate the flag), the frontend
    # treats the first as active.
    return jsonify(
        {
            "success": True,
            "status": source.get("status") or "",
            "token_expires_at": source.get("token_expires_at"),
            # surface the connected Meta account identity so the
            # summary can name it, matching YouTube/GA4 (the audit found Meta
            # alone omitted this). Prefer the friendly label, else the provider
            # account id the de_connection is keyed on.
            "account": (str(source.get("label") or "").strip() or str(source.get("provider_account_id") or "").strip()),
            # team_accounts drives the SUMMARY (removable, team view);
            # accounts/page_ids drive the EDIT step's preload (connection view).
            "team_accounts": team_accounts,
            "accounts": accounts,
            "page_ids": page_ids,
            "pages": pages,
            "ig_accounts": ig_accounts,
        }
    )


# ── Legacy + Path A dual-mode endpoints ──────────────────────────────────────


@data_connectors_bp.route("/api/wizard/fb/list-pages", methods=["POST"])
def api_wizard_fb_list_pages():
    """List Facebook pages for the wizard's Pages step.

    Dual-mode for the Path A landing:

    * Legacy mode — body: ``{"app_user_esid": "..."}``. Reads the token
      off the fb_app_user doc and calls /me/accounts. Still used by the
      live wizard frontend until Phase 2 migrates it.
    * Path A mode — body: ``{"connection_esid": "..."}``. Reads the
      token off the de_connection doc and calls /me/accounts. Used by
      the migrated wizard once Phase 2 lands.

    Exactly one of the two keys must be present.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err

    app_user_esid = (data.get("app_user_esid") or "").strip() or None
    connection_esid = (data.get("connection_esid") or "").strip() or None
    if bool(app_user_esid) == bool(connection_esid):
        return (
            jsonify({"success": False, "error": "Provide exactly one of app_user_esid or connection_esid"}),
            400,
        )

    # IDOR gate on the Path A (connection_esid) branch: it reads the
    # connection's decrypted token to call /me/accounts. The legacy
    # app_user_esid branch reads a different (fb_app_user) doc and is out of this
    # finding's scope.
    if connection_esid:
        authz_err = _check_connection_for_wizard(connection_esid)
        if authz_err is not None:
            return authz_err
    else:
        # the legacy branch reads the fb_app_user doc's stored Meta
        # token; the doc must be in the caller's workspace scope.
        authz_err = _check_account_asset_for_wizard("de_asset_fb_app_users", app_user_esid or "")
        if authz_err is not None:
            return authz_err

    try:
        if connection_esid:
            pages = fb_list_pages_for_connection(connection_esid=connection_esid)
        else:
            pages = fb_list_pages_via_graph(app_user_esid=app_user_esid)
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        # see the list-ad-accounts sibling: the 400 left no server-side
        # trace, making a stall at this step undiagnosable after the fact.
        logging.warning(
            "wizard/fb/list-pages: WizardError for connection=%s " "app_user=%s user=%s: %s",
            connection_esid,
            app_user_esid,
            session.get("username"),
            e,
        )
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "FB list-pages failed",
            log_detail="api_wizard_fb_list_pages failed",
            exc=e,
        )
    # zero pages is the leading suspect for the nine-retry prod stall:
    # pages_show_list was granted, but a Business-Manager-managed Page the authing
    # login does not personally administer does NOT appear in /me/accounts. The
    # Pages step then renders empty and advanceFromPages hard-blocks, so Confirm —
    # and the finisher — are never reached. Log it explicitly.
    if not pages:
        logging.warning(
            "wizard/fb/list-pages: /me/accounts returned ZERO pages for "
            "connection=%s app_user=%s user=%s — the Pages step will render empty "
            "and the wizard cannot advance to Confirm. A BM-managed Page the "
            "authing login does not administer is not returned by /me/accounts.",
            connection_esid,
            app_user_esid,
            session.get("username"),
        )
    return jsonify({"success": True, "pages": pages})


@data_connectors_bp.route("/api/wizard/fb/configure", methods=["POST"])
def api_wizard_fb_configure():
    """Final FB-wizard configure step. Dual-mode Path A.

    * Legacy mode — body: ``{team_esid, fb_account_esid, pages: [...]}``.
      Calls fb_link_pages_to_team which provisions
      de_asset_fb_pages + team back-links via the legacy app-user
      token + IG bridge.
    * Path A mode — body:
      ``{team_esid, connection_esid,
         accounts: [{provider_id, name}, ...],
         selected_page_ids: [...]}``. Calls
      fb_finish_data_connectors_wizard which invokes the onboarding
       bridges keyed off connection_esid +
      kicks the wizard-style importer.

      the Accounts step is now multi-select with per-account
      names, so ``accounts`` is the list-of-dicts shape. The legacy
      single ``fb_account_provider_id`` string is still accepted (the
      finisher's ``_normalize_accounts_arg`` collapses either shape) for
      the dual-mode prod-fallback path (Risk Register R8).

    The presence of ``connection_esid`` selects Path A; otherwise the
    legacy mode runs.
    """
    _require_data_connectors()
    data, err = _wizard_payload_or_400()
    if err:
        return err

    connection_esid = (data.get("connection_esid") or "").strip() or None
    team_esid = (data.get("team_esid") or "").strip()
    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err
    # Path A binds the connection's decrypted token to this team
    # (fb_finish_data_connectors_wizard runs the bridges off
    # it and kicks the importer), so the caller must be allowed to SEE that
    # connection, exactly as fb/oauth-status and fb/list-ad-accounts require
    #. Deny collapses to the same generic 404. Legacy mode (no
    # connection_esid) is unchanged.
    if connection_esid:
        authz_err = _check_connection_for_wizard(connection_esid)
        if authz_err is not None:
            return authz_err
    else:
        # the legacy branch links the fb_accounts doc and existing
        # fb_pages docs named in the body; both must be in the caller's
        # workspace scope, like every other wizard account esid.
        authz_err = _check_account_asset_for_wizard("de_asset_fb_accounts", data.get("fb_account_esid") or "")
        if authz_err is not None:
            return authz_err
        for page in data.get("pages") or []:
            if isinstance(page, dict) and page.get("esid"):
                authz_err = _check_account_asset_for_wizard("de_asset_fb_pages", str(page["esid"]))
                if authz_err is not None:
                    return authz_err

    # follow-up — mirror the account-name input's maxlength="120"
    # server-side (a crafted POST bypasses the client; the name lands on
    # de_asset_fb_ad_accounts docs unbounded otherwise).
    for account in data.get("accounts") or []:
        if isinstance(account, dict) and len(str(account.get("name") or "")) > 120:
            return (
                jsonify({"success": False, "error": "Account name must be 120 characters or fewer."}),
                400,
            )

    try:
        if connection_esid:
            username = session.get("username") or ""
            if not username:
                return jsonify({"success": False, "error": "Not authenticated"}), 401
            result = fb_finish_data_connectors_wizard(
                username=username,
                team_esid=team_esid,
                connection_esid=connection_esid,
                accounts=list(data.get("accounts") or []) or None,
                fb_account_provider_id=data.get("fb_account_provider_id") or None,
                selected_page_ids=list(data.get("selected_page_ids") or []),
                # explicit page selection with names, so the page
                # bridge links exactly what the user chose.
                selected_pages=list(data.get("selected_pages") or []) or None,
            )
        else:
            result = fb_link_pages_to_team(
                team_esid=team_esid,
                fb_account_esid=data.get("fb_account_esid", ""),
                pages=data.get("pages") or [],
            )
    except WizardError as e:
        # see api_wizard_echo_configure for the WizardError rationale.
        return jsonify({"success": False, "error": e.public_message}), 400
    except Exception as e:
        from lib.errors import safe_error_response

        return safe_error_response(
            "FB configure failed",
            log_detail="api_wizard_fb_configure failed",
            exc=e,
        )
    return jsonify({"success": True, **result})


def _fboauth_hostname() -> str:
    """Read FBOAUTH_HOSTNAME from app config; empty string if unset."""
    try:
        from web.config import config as app_config
    except ImportError:
        from ...config import config as app_config
    return app_config.FBOAUTH_HOSTNAME or ""
