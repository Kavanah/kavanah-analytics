"""
Connector Status Service

Aggregates a team-centric view of which data connectors are configured for a
given team, joining across the existing ``de_asset_*`` indices that already
hold the underlying records. This service is read-only and side-effect-free —
all writes still go through the existing ``/asset/<type>/...`` endpoints.

Used by the Data Connectors page (Management Console).
"""

import logging
import sys
from typing import Any

sys.path.insert(0, "/app/python")
from lib.errors import UserFacingError  # noqa: E402
from lib.es_helpers import (  # noqa: E402
    get_all_documents,
    get_document_by_id,
    get_elasticsearch_client,
    search_index,
)


# ── Connector definitions ─────────────────────────────────────────────────────
#
# Each connector definition maps a team's ``ds_*`` flag and ``*_esid*`` field(s)
# onto the source index that holds the linked asset. Order matters — this is
# the order the cards render in.

CONNECTORS: list[dict[str, Any]] = [
    # (2026-05-26) — Meta Ads is one connector covering both
    # Facebook Ads and Instagram. earlier the rollup carried two
    # separate cards (``facebook`` + ``instagram``) each with its own
    # "Set up" button, even though both share a single Meta OAuth grant
    # (the seven-scope set incl. ``instagram_basic``). The merged
    # card's state (made Instagram additive — FB-only is valid):
    #   * not_configured: ds_facebook is off (whether or not ds_instagram is set)
    #   * needs_setup:    ds_facebook is on but FB page link doesn't resolve,
    #     OR ds_instagram is on but its link doesn't resolve (dangling)
    #   * configured:     ds_facebook on with resolved FB pages (Instagram, if
    #     present, is reported additively via ig_configured — its absence is
    #     NOT a problem)
    # ``linked_assets`` stays anchored on the FB Pages chain (the
    # canonical user-facing asset). ``ig_linked_assets`` carries the IG
    # accounts in parallel so the UI can render the Meta card with a
    # secondary "and X Instagram accounts" line without a second card.
    # The legacy ``facebook`` / ``instagram`` keys were retired with
    # this merge — tests that pinned ``key == "facebook"`` move to
    # ``key == "meta_ads"`` and assert on the merged state.
    {
        "key": "meta_ads",
        "label": "Meta",
        "team_flag": "ds_facebook",
        "team_esid_field": "team_facebook_page_esids",
        # the DENORMALIZED page-id twin the FB page importer actually
        # matches teams on (get_page_insights.py: team_facebook_page_id is the
        # source of truth, esids "can be empty/stale"). Disconnect
        # cleared only the esid field, leaving this populated → the importer kept
        # matching a "disconnected" team. Clear it alongside the esid. Resolved
        # element-wise on single-account removal via asset_id_field (fb_page_id).
        "team_denorm_field": "team_facebook_page_id",
        "asset_index": "de_asset_fb_pages",
        "asset_title_field": "fb_page_name",
        "asset_type": "facebook_pages",
        # real provider ids for the Current Plan billing rollup.
        # FB pages surface their slug + numeric page id (not the ESID).
        "asset_id_field": "fb_page_id",
        "asset_slug_field": "fb_page_slug",
        "asset_kind": "page",
        "auth": "oauth",
        "oauth_manage_asset_type": "facebook_app_users",
        # wizard-cohort teams run in account_wide mode and
        # carry ``fb_ad_account_esid`` instead of ``team_facebook_page_esids``.
        # When the primary field resolves nothing, fall back to the
        # ad-account shape so the card reflects the team's actual wired
        # asset instead of showing "needs attention" misleadingly.
        "fallback_team_esid_field": "fb_ad_account_esid",
        "fallback_asset_index": "de_asset_fb_ad_accounts",
        "fallback_asset_title_field": "fb_ad_account_title",
        # FB ad accounts surface title + the human-facing
        # ad-account id (not the ESID) on the Current Plan rollup.
        "fallback_asset_id_field": "fb_ad_account_id",
        "fallback_asset_kind": "ad_account",
        # when the fallback resolves the card, the action
        # buttons must route to the ad-account asset shape (not the
        # primary's facebook_pages slug, which 404s on a fb_ad_account
        # ESID). oauth_manage is dropped because wizard-cohort teams
        # hold their token on de_connection, not de_asset_fb_app_users.
        "fallback_asset_type": "facebook_ad_accounts",
        # IG side of the merged card, used to compute the
        # union state and to expose IG asset titles on the card without
        # rendering a second connector row.
        "ig_team_flag": "ds_instagram",
        "ig_team_esid_field": "team_instagram_profile_esids",
        "ig_asset_index": "de_asset_ig_accounts",
        "ig_asset_title_field": "ig_account_title",
    },
    {
        "key": "google_analytics",
        "label": "Google Analytics 4",
        "team_flag": "ds_google_analytics",
        "team_esid_field": "team_ga4_property_esids",
        # the DENORMALIZED property-id twin the GA4 importer matches on
        # (assets.py: bare_id in team_ga4_properties OR esid in the esid list —
        # the OR means the stale denorm field alone keeps a disconnected team
        # matching). Cleared alongside the esid; element-wise on single-account
        # removal via asset_id_field (ga_property_id).
        "team_denorm_field": "team_ga4_properties",
        "asset_index": "de_asset_ga_properties",
        "asset_title_field": "ga_property_title",
        # the GA4 property id (e.g. "396371821"). The dashboard
        # template placeholder_field is "property_id" and ga_property_id
        # ends with it, so the install resolver substitutes this value.
        "asset_id_field": "ga_property_id",
        "asset_type": "ga_properties",
        "auth": "service_account",
        "oauth_manage_asset_type": "ga_properties",
    },
    {
        "key": "google_ads",
        "label": "Google Ads",
        "team_flag": "ds_google_ads",
        # Phase 1 — a team can hold MULTIPLE customers via the list
        # field. Resolve linked assets from it; keep the legacy scalar as the
        # fallback field so disconnect clears BOTH during the transition window
        # and so pre-migration teams (list not yet seeded) still resolve.
        "team_esid_field": "team_google_ads_customer_esids",
        "fallback_team_esid_field": "google_ads_customer_esid",
        "asset_index": "de_asset_google_ads_customers",
        "asset_title_field": "google_ads_customer_title",
        # the Google Ads customer id (e.g. "646-706-0525"); the
        # template placeholder_field is "google_ads_customer_id" (exact match).
        "asset_id_field": "google_ads_customer_id",
        "asset_type": "google_ads_customers",
        # the fallback resolves the SAME asset shape as the
        # primary (the scalar points at a de_asset_google_ads_customers row too),
        # so the fallback_asset_* keys mirror the primary. _connector_status's
        # fallback block reads these with bracket access; omitting them (as the
        # original entry did) raised KeyError 'fallback_asset_index' and
        # 500'd the connector-status endpoint whenever the list field was empty.
        "fallback_asset_index": "de_asset_google_ads_customers",
        "fallback_asset_title_field": "google_ads_customer_title",
        "fallback_asset_id_field": "google_ads_customer_id",
        "fallback_asset_type": "google_ads_customers",
        "auth": "service_account",
        "oauth_manage_asset_type": "google_ads_customers",
    },
    {
        "key": "youtube",
        "label": "YouTube",
        "team_flag": "ds_youtube",
        "team_esid_field": "team_youtube_channel_handle_esids",
        "asset_index": "de_asset_youtube_channels",
        "asset_title_field": "youtube_channel_handle",
        "asset_type": "youtube_channels",
        "auth": "oauth",
        "oauth_manage_asset_type": "youtube_accounts",
    },
    {
        "key": "echo",
        "label": "Echo Messaging",
        "team_flag": "ds_echo",
        "team_esid_field": "echo_tenant_esid",
        "asset_index": "de_asset_echo_tenants",
        "asset_title_field": "echo_tenant_title",
        "asset_type": "echo_tenants",
        "auth": "api_key",
    },
    {
        "key": "chatwoot",
        "label": "Chatwoot",
        "team_flag": "ds_chatwoot",
        "team_esid_field": "chatwoot_account_esid",
        "asset_index": "de_asset_chatwoot_accounts",
        "asset_title_field": "chatwoot_account_title",
        "asset_type": "chatwoot_accounts",
        "auth": "api_key",
    },
    {
        "key": "disciple_tools",
        "label": "Disciple.Tools",
        "team_flag": None,  # Driven by team_disciple_tools_instance URL field
        "team_url_field": "team_disciple_tools_instance",
        "asset_index": None,
        "asset_title_field": None,
        "asset_type": None,
        "auth": "url",
    },
    # Phase 3 — Bitly. No de_asset_* index: the team's pointer
    # (bitly_connection_esid) targets de_connection directly, so status is
    # special-cased in _connector_status (flag + live-connection check) and
    # asset_index stays None. team_esid_field is still declared so
    # disconnect_connector's generic patch clears the pointer alongside the
    # flag.
    {
        "key": "bitly",
        "label": "Bitly",
        "team_flag": "ds_bitly",
        "team_esid_field": "bitly_connection_esid",
        "asset_index": None,
        "asset_title_field": None,
        "asset_type": None,
        "auth": "api_key",
    },
    # TikTok organic. Like Bitly there is no de_asset_* index: the
    # team lists its de_connection esids directly (one TikTok account per
    # connection), so status is special-cased in _connector_status. A genuine
    # list, so per-account removal applies (_MULTI_ACCOUNT_CONNECTORS).
    {
        "key": "tiktok",
        "label": "TikTok",
        "team_flag": "ds_tiktok",
        "team_esid_field": "team_tiktok_connection_esids",
        "asset_index": None,
        "asset_title_field": None,
        "asset_type": None,
        "auth": "oauth",
    },
]

TEAMS_INDEX = "de_asset_teams"
ORGS_INDEX = "de_asset_organization"


def _to_display_string(value: Any) -> str:
    """Coerce an ES field that may be a string, list, or None to a single
    display string. Multi-select fields like ``organization_title`` are
    stored as lists; plain text fields as strings.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v is not None and v != "")
    return str(value)


def _org_title_map(org_esids: list[str] | None = None) -> dict[str, str]:
    """build an ``organization_esid -> organization_title`` map from
    ``de_asset_organization``.

    Why this exists: wizard-provisioned teams store ``organization_esid`` but NOT
    ``organization_title`` (``_upsert_team`` writes the esid only — same gap
     fixed for the teams listbox). Reading the team's stored
    ``organization_title`` therefore yields "" for those teams, which the Report
    Scheduler tree rendered as "(Unassigned)" and the Data Connectors header
    rendered as a blank org name — even though the team HAS an org.

    Resolving via this map keeps the org name correct for wizard teams and
    survives org renames (the team's stored title would go stale). Fail-soft: an
    ES error yields an empty map, and callers fall back to the team's stored
    ``organization_title`` (earlier behaviour).

    scope the read to ``org_esids`` (the primary orgs actually
    referenced by the caller's teams) via an ``ids`` query on the org docs'
    ``_id``, instead of a full unscoped ``get_all_documents`` of every tenant's
    org docs on every ``list_teams`` call (this is on the hot Data Connectors /
    Report Scheduler paths). Behaviour by argument:

    * ``org_esids == []`` → empty map WITHOUT an ES read. No team carried a
      primary org, so there is nothing to resolve.
    * ``org_esids == [<esids>]`` → ``ids`` query fetching only those org docs.
    * ``org_esids is None`` → legacy full read, for callers that don't supply the
      referenced set (e.g. ``_resolve_org_title`` single-team callers don't reach
      here; this preserves any direct/test caller of the old no-arg form).
    """
    if org_esids is not None and not org_esids:
        return {}
    query = None
    if org_esids:
        # Org docs are keyed by _id (the esid); query by document id so the read
        # is mapping-agnostic — no dependency on an organization_esid _source
        # field or its text/keyword shape. dict.fromkeys dedups while preserving
        # order.
        query = {"ids": {"values": list(dict.fromkeys(org_esids))}}
    try:
        docs = get_all_documents(ORGS_INDEX, query=query) or []
    except Exception as e:  # noqa: BLE001 — fail-soft, callers degrade gracefully
        logging.warning("connector_status_service._org_title_map: %s", e)
        return {}
    out: dict[str, str] = {}
    for d in docs:
        esid = d.get("_id") or ""
        title = _to_display_string((d.get("_source") or {}).get("organization_title"))
        if esid and title:
            out[esid] = title
    return out


def _resolve_org_title(source: dict[str, Any], org_map: dict[str, str] | None = None) -> str:
    """the display org title for a team ``source``.

    Prefers the team's stored ``organization_title`` (legacy / explicitly-set
    teams). Falls back to resolving the team's PRIMARY org esid
    (``organization_esid[0]``) through ``org_map`` for wizard-provisioned teams
    that carry the esid only. ``org_map`` is optional so single-team callers can
    omit the batch read and pass ``None`` (then a one-doc lookup is used).
    """
    stored = _to_display_string(source.get("organization_title"))
    if stored:
        return stored
    primary = _primary_org_esid(source)
    if not primary:
        return ""
    if org_map is not None:
        return org_map.get(primary, "")
    # Single-team path: one targeted lookup rather than scanning every org.
    try:
        org = get_document_by_id(ORGS_INDEX, primary)
    except Exception as e:  # noqa: BLE001 — fail-soft
        logging.warning("connector_status_service._resolve_org_title(%r): %s", primary, e)
        return ""
    return _to_display_string((org or {}).get("organization_title"))


def list_teams(allowed_workspaces: list[str] | None = None) -> list[dict[str, Any]]:
    """Return all teams as ``[{"esid": ..., "title": ..., "org_title": ...}]``.

    Sorted by team title. Returns an empty list if ES is unavailable.

    tenant-scoping contract:

    * ``allowed_workspaces is None`` → no workspace filter (legacy
      behaviour; superuser / asset_admin who see everything).
    * ``allowed_workspaces == []`` → deny-all; returns ``[]`` without
      hitting ES. A scoped user with no workspace grant has no
      visibility into any team.
    * ``allowed_workspaces == [<esids>]`` → server-side filter via
      ``terms`` on ``workspace_esid``. reindexed
      ``de_asset_teams.workspace_esid`` to keyword, so the bare field
      matches hyphenated slugs again (the ``.keyword`` workaround
      has been reverted).
    """
    if allowed_workspaces is not None and not allowed_workspaces:
        return []

    query = None
    if allowed_workspaces:
        query = {"terms": {"workspace_esid": list(allowed_workspaces)}}

    try:
        docs = get_all_documents(TEAMS_INDEX, query=query) or []
    except Exception as e:
        logging.warning("connector_status_service.list_teams: %s", e)
        return []

    # resolve org titles via an org read so wizard teams (which store
    # organization_esid but not organization_title) show their real org name
    # instead of "(Unassigned)".
    # scope that read to the primary orgs actually referenced by the
    # returned teams, so a scoped caller no longer triggers a full unscoped read
    # of every tenant's org docs on this hot path. Only teams that LACK a stored
    # organization_title need the map (those with one resolve from it directly in
    # _resolve_org_title), so the read is narrowed to exactly the wizard-team gap
    # the map exists to close.
    referenced_org_esids = [
        primary
        for d in docs
        if not _to_display_string((src := d.get("_source") or {}).get("organization_title"))
        and (primary := _primary_org_esid(src))
    ]
    org_map = _org_title_map(referenced_org_esids)

    teams = []
    for d in docs:
        # get_all_documents returns ES hit envelopes ({"_id": ..., "_source": ...}).
        source = d.get("_source") or {}
        esid = d.get("_id") or ""
        title = _to_display_string(source.get("team_title")) or esid
        org_title = _resolve_org_title(source, org_map)
        # expose the team's PRIMARY org esid (organization_esid[0]) so
        # the org/team tree can drop teams an org owner only owns secondarily. ""
        # when the array is empty (such a team shows to no org owner; the
        # superuser/see-all tree never consults this field).
        teams.append(
            {
                "esid": esid,
                "title": title,
                "org_title": org_title,
                "org_esid": _primary_org_esid(source),
            }
        )
    teams.sort(key=lambda t: t["title"].lower())
    return teams


def _primary_org_esid(source: dict[str, Any]) -> str:
    """a team's PRIMARY org is the first entry of its
    ``organization_esid`` list (array-positional, DECISIONS.md 2026-06-16 — the
    same definition ``SubscriptionStateService.resolve_primary_org_esid`` step 3
    uses). ``organization_esid`` is a multi-select list, but legacy / scalar docs
    store a bare string; treat that as its own primary. Returns "" when unset.
    """
    org_field = source.get("organization_esid")
    if isinstance(org_field, list):
        return str(org_field[0]) if org_field else ""
    return str(org_field) if org_field else ""


def primary_org_for_team(team_esid: str) -> str:
    """the PRIMARY org esid of ``team_esid`` (``organization_esid[0]``),
    or "" when the team can't be resolved.

    Used by the org-scoped billing rollup to attribute a SHARED asset to exactly
    one org: an asset's ``owning_team_esid`` resolves through here to the single
    org that should be billed for it. Fail-soft: a missing team / ES error returns
    "" (the caller then leaves the source unattributed-by-owner and keeps its
    earlier behaviour rather than dropping it from every org).
    """
    if not team_esid:
        return ""
    try:
        source = get_document_by_id(TEAMS_INDEX, team_esid)
    except Exception as e:  # noqa: BLE001 — fail-soft
        logging.warning("connector_status_service.primary_org_for_team(%r): %s", team_esid, e)
        return ""
    if not source:
        return ""
    return _primary_org_esid(source)


def list_teams_for_org(org_esid: str) -> list[dict[str, Any]]:
    """return every team whose PRIMARY org is ``org_esid`` as
    ``[{"esid", "title", "org_title"}]``.

    Used by the org-scoped billing rollup (``SubscriptionStateService``):
    the Current Plan page lists an org owner's connectors by walking the
    org's teams, NOT by querying ``de_connection.principal`` (which is
    keyed on whoever authored the OAuth grant, not on the org).

    ``de_asset_teams.organization_esid`` is mapped ``text`` + ``.keyword``
    (verified on staging 2026-06-16: a bare ``term`` on ``organization_esid``
    returns 0; the ``.keyword`` subfield matches). Query the keyword subfield
    so a hyphen-/space-containing esid matches exactly. Same dual-mapping trap
    as the ``team_title`` note in ``role_filter_service``.

    ``organization_esid`` is a multi-select list, so the ``term``
    query matches a team that carries ``org_esid`` in ANY position, including a
    SECONDARY org. The Current Plan rollup must attribute a team to its PRIMARY
    org only (``organization_esid[0]`` — the array-positional definition in
    ``resolve_primary_org_esid`` step 3). An array-positional ``[0]`` check
    can't be expressed as a single ES ``term`` query, so the ``term`` narrows
    server-side (still a fast index hit) and a post-query filter on the fetched
    docs keeps only the teams whose primary org is ``org_esid``. A team that
    lists the org as a secondary org (e.g. team A / team B with the org in a non-first
    position) is excluded, so its connections no longer leak into that org's
    Current Plan / usage count.

    Fail-soft: empty input or ES error returns ``[]``.
    """
    if not org_esid:
        return []
    # organization_esid is a multi-select list field; the term narrows to teams
    # that carry the esid ANYWHERE. The primary-org filter below then keeps
    # only those whose FIRST org is this one. bare field —
    # explicit keyword post-reindex, the .keyword subfield is gone.
    query = {"term": {"organization_esid": org_esid}}
    try:
        docs = get_all_documents(TEAMS_INDEX, query=query) or []
    except Exception as e:
        logging.warning("connector_status_service.list_teams_for_org(%r): %s", org_esid, e)
        return []

    # every returned team shares this org, so resolve its title once
    # (from the org doc) and reuse it. Wizard teams store no organization_title,
    # so reading the stored field alone would render the org name blank.
    org_title_resolved = _to_display_string((get_document_by_id(ORGS_INDEX, org_esid) or {}).get("organization_title"))

    teams = []
    for d in docs:
        source = d.get("_source") or {}
        # primary-org gate: drop teams that carry org_esid only as a
        # secondary org (it isn't organization_esid[0]).
        if _primary_org_esid(source) != org_esid:
            continue
        esid = d.get("_id") or ""
        title = _to_display_string(source.get("team_title")) or esid
        org_title = _to_display_string(source.get("organization_title")) or org_title_resolved
        teams.append({"esid": esid, "title": title, "org_title": org_title})
    teams.sort(key=lambda t: t["title"].lower())
    return teams


def workspaces_for_teams(
    team_esids: list[str],
    allowed_workspaces: list[str] | None = None,
) -> set[str]:
    """resolve the set of ``workspace_esid`` the given teams belong
    to, dropping any team the caller is not scoped to.

    Used by the Report Scheduler's connector-dashboard picker to bound the
    dashboard scan to exactly the workspace(s) the wizard's selected teams live
    in (so same-title dashboards in OTHER workspaces are excluded), AND to
    enforce the caller's read scope (a crafted ``team_esids`` pointing at a
    foreign workspace contributes nothing).

    Tenant-scoping mirrors :func:`get_team_connectors`:

    * ``allowed_workspaces is None`` → no scope check (superuser / asset_admin);
      every selected team's workspace counts.
    * ``allowed_workspaces == []`` → deny-all; returns ``set()`` without reading
      ES.
    * otherwise → a team whose ``workspace_esid`` is not in the allowed set is
      silently skipped (same collapse-to-not-found pattern as the connector
      payload, so the picker can't enumerate cross-tenant teams).

    Fail-soft: an unresolvable / missing team is dropped; an ES error on one
    team lookup drops that team and continues.
    """
    if allowed_workspaces is not None and not allowed_workspaces:
        return set()

    allowed_set = set(allowed_workspaces) if allowed_workspaces is not None else None
    workspaces: set[str] = set()
    for team_esid in team_esids:
        if not team_esid:
            continue
        try:
            source = get_document_by_id(TEAMS_INDEX, team_esid)
        except Exception as e:  # noqa: BLE001 — one bad lookup must not crash the picker
            logging.warning("connector_status_service.workspaces_for_teams(%r): %s", team_esid, e)
            continue
        if not source:
            continue
        team_ws = source.get("workspace_esid")
        if not team_ws:
            continue
        if allowed_set is not None and team_ws not in allowed_set:
            continue  # out of the caller's scope → contributes nothing
        workspaces.add(team_ws)
    return workspaces


def _resolve_linked_assets(
    esid_field_value: Any,
    asset_index: str,
    title_field: str,
    id_field: str | None = None,
    slug_field: str | None = None,
    kind: str | None = None,
) -> list[dict[str, str]]:
    """Look up linked assets given an ESID or list of ESIDs.

    Missing / unresolvable ESIDs are dropped silently — they show up in the UI
    as "Configured but no linked asset" via the count check upstream.

    when ``id_field`` / ``slug_field`` are supplied, the resolved
    asset dict ALSO carries the human-facing provider id (e.g. the FB
    ad-account id ``fb_ad_account_id`` or page id ``fb_page_id``) and slug
    (``fb_page_slug``), so the Current Plan billing rollup can show real IDs
    instead of the meaningless ESID. ``kind`` tags the asset ("page" /
    "ad_account") so the Meta card's mixed assets can be split into labelled
    sub-sections. These keys are additive — existing consumers read only
    ``esid`` / ``title`` and are untouched.

    when the asset doc carries an ``owning_team_esid`` (the team that
    created/owns it), it is carried onto the resolved row so the org-scoped
    billing rollup can attribute a SHARED asset (one esid linked by teams across
    multiple orgs) to exactly one org — its owning team's primary org — instead
    of counting it under every org that happens to link it. Uniform field name
    across connectors, so no per-connector config; absent on un-backfilled assets
    (the rollup then keeps its earlier behaviour for that source).
    """
    if not esid_field_value:
        logging.info("_resolve_linked_assets: empty esid_field_value for index=%s", asset_index)
        return []

    esids = esid_field_value if isinstance(esid_field_value, list) else [esid_field_value]
    assets: list[dict[str, str]] = []
    logging.debug(
        "_resolve_linked_assets: resolving %d esids from index=%s field=%s",
        len(esids),
        asset_index,
        title_field,
    )

    for esid in esids:
        if not esid:
            continue
        try:
            source = get_document_by_id(asset_index, esid)
            logging.debug("_resolve_linked_assets: fetched %s/%s found=%s", asset_index, esid, bool(source))
        except Exception as e:
            logging.warning(
                "connector_status_service: lookup failed for %s/%s: %s",
                asset_index,
                esid,
                e,
            )
            continue
        if not source:
            logging.warning("connector_status_service: document not found for %s/%s", asset_index, esid)
            continue
        title = _to_display_string(source.get(title_field)) or esid
        asset: dict[str, str] = {"esid": esid, "title": title}
        if id_field:
            asset["provider_id"] = _to_display_string(source.get(id_field))
        if slug_field:
            asset["slug"] = _to_display_string(source.get(slug_field))
        if kind:
            asset["kind"] = kind
        # carry the asset's owning team (when stamped) so the
        # org-scoped billing rollup can attribute a shared asset to one org only.
        owning_team = _to_display_string(source.get("owning_team_esid"))
        if owning_team:
            asset["owning_team_esid"] = owning_team
        assets.append(asset)
        logging.debug("connector_status_service: resolved %s/%s title=%r", asset_index, esid, title)

    logging.debug("_resolve_linked_assets: returning %d assets", len(assets))
    return assets


def _resolve_fb_token_health(linked_pages: list[dict[str, str]]) -> dict[str, Any] | None:
    """Walk team → fb_pages → fb_account → fb_apps → fb_app_users and return
    a token-health payload for the FB card.

    Returns ``{"app_user_esid", "app_user_title", "token_expires_at",
    "expires_in_days", "status"}`` or ``None`` if the chain can't be resolved.

    ``status`` is one of: ``"healthy"`` (>14 days), ``"expiring_soon"``
    (<=14 days), ``"expired"`` (<=0 days), ``"unknown"`` (no expiry recorded),
    ``"missing"`` (no token).
    """
    if not linked_pages:
        return None

    # Take the first linked page as a starting point — they all share an account.
    first_page = get_document_by_id("de_asset_fb_pages", linked_pages[0]["esid"])
    if not first_page:
        return None
    fb_account_esid = first_page.get("fb_account_esid")
    if not fb_account_esid:
        return None

    # Find apps for this account
    es = get_elasticsearch_client()
    if es is None:
        return None
    try:
        apps_resp = es.search(
            index="de_asset_fb_apps",
            body={
                "query": {"term": {"fb_account_esid": fb_account_esid}},
                "size": 50,
            },
        )
        app_esids = [hit["_id"] for hit in apps_resp.get("hits", {}).get("hits", [])]
    except Exception as e:
        logging.warning("connector_status_service: fb_apps lookup failed: %s", e)
        return None
    if not app_esids:
        return None

    # Find app users for any of these apps. Pick the one with the latest
    # token expiry (most recently authorized).
    try:
        users_resp = es.search(
            index="de_asset_fb_app_users",
            body={
                "query": {"terms": {"fb_app_esid": app_esids}},
                "size": 100,
            },
        )
        user_hits = users_resp.get("hits", {}).get("hits", [])
    except Exception as e:
        logging.warning("connector_status_service: fb_app_users lookup failed: %s", e)
        return None

    candidates = []
    for hit in user_hits:
        src = hit.get("_source") or {}
        if not src.get("fb_app_user_token"):
            continue
        candidates.append(
            {
                "esid": hit["_id"],
                "title": src.get("fb_app_user_title") or hit["_id"],
                "expires_at": src.get("fb_app_user_token_exp"),
                "fb_app_esid": src.get("fb_app_esid") or "",
                "auth_url_path": src.get("fb_app_user_auth_url") or "",
            }
        )

    if not candidates:
        return {"status": "missing"}

    # Pick the candidate with the latest expiry (treat None as 0 = oldest).
    candidates.sort(key=lambda c: c.get("expires_at") or 0, reverse=True)
    best = candidates[0]
    expires_at = best.get("expires_at")

    if expires_at is None or expires_at == 0:
        status = "unknown"
        expires_in_days = None
    else:
        from datetime import datetime, timezone

        try:
            now_ts = datetime.now(timezone.utc).timestamp()
            expires_in_days = int((int(expires_at) - now_ts) // 86400)
        except (TypeError, ValueError):
            expires_in_days = None
            status = "unknown"
        else:
            if expires_in_days <= 0:
                status = "expired"
            elif expires_in_days <= 14:
                status = "expiring_soon"
            else:
                status = "healthy"

    # Build the OAuth URL for the "Refresh token" button. Empty string when
    # any piece is missing (the UI hides the button in that case).
    fboauth_hostname = _get_fboauth_hostname()
    oauth_url = ""
    if fboauth_hostname and best.get("fb_app_esid") and best.get("auth_url_path"):
        oauth_url = f"https://{fboauth_hostname}/{best['fb_app_esid']}/{best['auth_url_path']}"

    return {
        "app_user_esid": best["esid"],
        "app_user_title": best["title"],
        "token_expires_at": expires_at,
        "expires_in_days": expires_in_days,
        "status": status,
        "oauth_refresh_url": oauth_url,
    }


def _get_fboauth_hostname() -> str:
    """Read FBOAUTH_HOSTNAME from app config, returning '' if unset."""
    try:
        try:
            from web.config import config as app_config
        except ImportError:
            from ..config import config as app_config
        return app_config.FBOAUTH_HOSTNAME or ""
    except Exception:
        return ""


# map a connector card ``key`` to the de_connection ``provider`` it
# should resolve. All identity except GA4, whose card key is ``google_analytics``
# but whose connection provider is ``ga4`` (PROVIDER_GA4). Used to guard the
# team-doc ``connection_esid`` fallback so a team with two OAuth providers
# connected doesn't cross-resolve (the YouTube card accepting the GA4 connection
# id, or vice-versa). Cards not in the map use their key as-is.
_KEY_TO_CONNECTION_PROVIDER = {
    "meta_ads": "facebook",
    "google_analytics": "ga4",
    "youtube": "youtube",
    "google_ads": "google_ads",
}

# provider → the provider-specific team-doc connection stamp the
# wizard writes. reads it as the tie-breaker when a team's linked
# assets resolve to more than one live connection.
_PROVIDER_CONNECTION_STAMP_FIELD = {
    "google_ads": "google_ads_connection_esid",
    "ga4": "ga4_connection_esid",
    "youtube": "youtube_connection_esid",
}


def _provider_connection_stamp(team_source: dict[str, Any], key: str | None) -> str | None:
    """the team's provider-specific connection stamp for card
    ``key`` (e.g. ``ga4_connection_esid`` for ``google_analytics``), or None
    when the provider has no such field or the team never stamped it."""
    provider = _KEY_TO_CONNECTION_PROVIDER.get(key, key) if key else None
    field_name = _PROVIDER_CONNECTION_STAMP_FIELD.get(provider) if provider else None
    if not field_name:
        return None
    return str(team_source.get(field_name) or "").strip() or None


def _resolve_connection_esid_fallback(
    team_source: dict[str, Any],
    resolved_legacy_index: str | None,
    linked_assets: list[dict[str, str]],
    key: str | None = None,
) -> str | None:
    """resolve a card's de_connection when the
    ``de_connection_asset`` legacy backlink is missing.

    ``key`` is the connector card key. When provided, the team-doc
    ``connection_esid`` fallback (path 1 below) is accepted only if the resolved
    connection's ``provider`` matches the card. The team doc carries a SINGLE
    ``connection_esid`` that every provider's wizard stamps, so a team with both
    (say) YouTube and GA4 connected would otherwise have one card resolve the
    other's connection. The provider guard rejects the mismatch and falls
    through to path 2 (the per-asset ``wizard_connection_esid`` stamp), which is
    inherently provider-correct because it reads the card's OWN asset docs.

    ``find_connection_esid_by_legacy_assets`` only matches connections
    whose ``de_connection_asset`` rows were stamped with the linked
    asset's ``legacy_asset_esid`` — a backlink the finisher
    writes. Teams wired before that helper landed (the existing
    staging / prod wizard cohort, e.g. Kavanah Media) have a working
    ``de_connection`` (the importer uses it) but no backlink row, so
    the primary lookup returns None and the Manage button + connected
    summary never appear.

    Two ordered fallbacks, each validated against
    ``ConnectionService.get_connection`` so a stale id never deep-links
    to a deleted row:

    1. ``team_source["connection_esid"]`` — the field the bridge
       writes onto the team doc and the importer's account-wide
       credential resolver reads. This is the authoritative pointer for
       wizard-cohort teams.
    2. ``wizard_connection_esid`` stamped on each linked asset doc
       (``de_asset_fb_pages`` / ``de_asset_fb_ad_accounts``). Covers a
       team whose ``connection_esid`` field is absent but whose asset
       docs carry the stamp.

    Returns the first existing connection esid, or None.
    """
    try:
        from .connection_service import ConnectionService
    except Exception:  # pragma: no cover - import boundary
        return None

    def _exists(esid: str | None) -> str | None:
        # only accept a LIVE connection (exists AND not revoked).
        # A revoked/deleted connection must not drive the Manage surface;
        # is_connection_live encapsulates that rule (shared with the
        # primary find_connection_esid_by_legacy_assets path).
        if not esid:
            return None
        try:
            return esid if ConnectionService.is_connection_live(esid) else None
        except Exception:
            return None

    def _provider_matches(esid: str) -> bool:
        # accept the team-doc connection_esid only when its provider
        # matches this card. No key passed (legacy callers) -> no guard, prior
        # behavior. A card key with no mapping uses the key verbatim as the
        # expected provider.
        if not key:
            return True
        expected = _KEY_TO_CONNECTION_PROVIDER.get(key, key)
        try:
            conn = ConnectionService.get_connection(esid)
        except Exception:
            return False
        return bool(conn) and conn.get("provider") == expected

    # 1. provider-specific team-doc field (google_ads_connection_esid,
    # ga4_connection_esid, youtube_connection_esid). Prevents one provider's
    # stamp from overwriting another when multiple wizard-cohort connectors
    # are configured on the same team. Fallback to the legacy connection_esid
    # for backward compat with existing teams that predate this fix.
    team_doc_esid = _provider_connection_stamp(team_source, key)
    # Legacy fallback for teams created before provider-specific fields
    if not team_doc_esid:
        team_doc_esid = (team_source.get("connection_esid") or "").strip() or None
    hit = _exists(team_doc_esid)
    if hit and _provider_matches(hit):
        return hit

    # 2. wizard_connection_esid on the linked asset docs
    if resolved_legacy_index:
        for a in linked_assets:
            esid = a.get("esid")
            if not esid:
                continue
            try:
                doc = get_document_by_id(resolved_legacy_index, esid)
            except Exception:
                continue
            if not doc:
                continue
            hit = _exists((doc.get("wizard_connection_esid") or "").strip() or None)
            if hit:
                return hit
    return None


def _dt_site_has_token(team_esid: str) -> bool:
    """follow-up — does this team have a saved, decryptable DT Site
    Link token?

    Drives the wizard's "saved token on file" display: the token field is
    shown filled with dots (the MFA-secret pattern) so a saved credential
    doesn't read as a blank / lost field — WITHOUT shipping the ciphertext to
    the client. Resolves the team's ``dt_site`` connection_asset to its parent
    ``de_connection`` and checks for a stored ``oauth_token_enc``. Fail-soft:
    any lookup error returns False (the wizard renders an empty field, the
    pre-existing behavior).
    """
    if not team_esid:
        return False
    try:
        from web.services.connection_service import ASSET_TYPE_DT_SITE, ConnectionService
    except ImportError:
        from .connection_service import ASSET_TYPE_DT_SITE, ConnectionService
    try:
        for hit in ConnectionService.list_connection_assets_by_type(ASSET_TYPE_DT_SITE):
            src = hit.get("_source") or {}
            if (src.get("provider_asset_id") or "") != team_esid:
                continue
            conn = ConnectionService.get_connection(src.get("connection_esid") or "") or {}
            if conn.get("oauth_token_enc"):
                return True
    except Exception as e:  # noqa: BLE001 — display-only; never break the status payload
        logging.warning("connector_status: DT token check failed for %s: %s", team_esid, e)
    return False


def _google_ads_customer_team_map(customer_esids: list[str]) -> dict[str, list[dict[str, str]]]:
    """for each Google Ads customer ESID, the Active+enabled teams
    that link it.

    Mirrors the importer's resolution query
    (``get_team_by_google_ads_customer_esid``) so the UI flags exactly the
    condition the importer warns about: a single customer assigned to more than
    one Active, ``ds_google_ads``-enabled team — only ONE of which receives
    data. Matches EITHER the multi-account list field
    (``team_google_ads_customer_esids``) OR the legacy scalar
    (``google_ads_customer_esid``), the same transition-window pair the importer
    accepts.

    Batched: one ES query for all of a team's customers (a ``should`` over the
    full ESID set), not one query per customer, so the connectors endpoint stays
    fast. Returns ``{customer_esid: [{"esid", "title"}, ...]}`` — teams ordered
    by ``team_esid`` for stability. Best effort: an ES error yields ``{}`` so the
    card never breaks over a display-only annotation.
    """
    wanted = [e for e in (customer_esids or []) if e]
    if not wanted:
        return {}

    try:
        results = search_index(
            index_name=TEAMS_INDEX,
            query={
                "bool": {
                    "must": [
                        # bare fields — explicit keyword post-reindex,
                        # the .keyword subfields are gone (a term on them
                        # silently matches nothing).
                        {"term": {"team_google_ads_integration_status": "Active"}},
                        {"term": {"ds_google_ads": "true"}},
                    ],
                    "should": [
                        # the list field has been bare keyword since
                        # update_v19; brought the legacy scalar to the
                        # same shape, so both legs query bare now.
                        {"terms": {"team_google_ads_customer_esids": wanted}},
                        {"terms": {"google_ads_customer_esid": wanted}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            size=200,
        )
    except Exception as e:  # noqa: BLE001 — display-only; never break the status payload
        logging.warning("multi-team lookup failed: %s", e)
        return {}

    hits = results.get("hits", {}).get("hits", [])
    hits = sorted(hits, key=lambda h: h.get("_id", ""))

    wanted_set = set(wanted)
    out: dict[str, list[dict[str, str]]] = {e: [] for e in wanted}
    for h in hits:
        src = h.get("_source", {}) or {}
        team_entry = {
            "esid": h.get("_id", ""),
            "title": _to_display_string(src.get("team_title")) or h.get("_id", ""),
            # for workspace-scoped naming and the split-by-rules state.
            "workspace_esid": str(src.get("workspace_esid") or ""),
            "has_rules": bool(src.get("team_google_ads_campaign_labels") or src.get("team_google_ads_campaign_prefixes")),
        }
        # A team can link the customer via the list, the scalar, or both — record
        # it under each wanted ESID it actually references (no double-listing).
        linked = set(src.get("team_google_ads_customer_esids") or [])
        scalar = src.get("google_ads_customer_esid")
        if scalar:
            linked.add(scalar)
        for esid in linked & wanted_set:
            out[esid].append(team_entry)
    return out


def _bitly_connection_info(connection_esid: str) -> tuple[bool, list[str]]:
    """Phase 3 — ``(has_saved_token, tags)`` for a Bitly connection.

    Drives the wizard's prefill: the token field shows dots when a saved token
    exists (the DT/MFA-secret pattern, ciphertext never shipped) and the tags
    field prefills with the stored filter. Fail-soft: any lookup error returns
    ``(False, [])`` — the wizard renders empty fields.
    """
    if not connection_esid:
        return False, []
    try:
        from .connection_service import ASSET_TYPE_BITLY_ACCOUNT, ConnectionService

        conn = ConnectionService.get_connection(connection_esid) or {}
        has_token = bool(conn.get("oauth_token_enc"))
        tags: list[str] = []
        for hit in ConnectionService.list_assets_for_connection(connection_esid):
            src = hit.get("_source") or {}
            if (src.get("asset_type") or "") != ASSET_TYPE_BITLY_ACCOUNT:
                continue
            tags = [str(t) for t in ((src.get("metadata") or {}).get("tags") or []) if str(t).strip()]
            break
        return has_token, tags
    except Exception as e:  # noqa: BLE001 — display-only; never break the status payload
        logging.warning("connector_status: bitly connection info failed for %s: %s", connection_esid, e)
        return False, []


def _tiktok_status(team_source: dict[str, Any], connector: dict[str, Any]) -> dict[str, Any]:
    """the TikTok card (see the tiktok branch in _connector_status)."""
    enabled = str(team_source.get(connector["team_flag"])).lower() in ("true", "1", "yes")
    raw = team_source.get(connector["team_esid_field"]) or []
    esids = [str(e).strip() for e in ([raw] if isinstance(raw, str) else raw) if str(e).strip()]
    linked: list[dict[str, Any]] = []
    first_live = None
    first_existing = None
    if enabled:
        from .connection_service import PROVIDER_TIKTOK, STATUS_ACTIVE, STATUS_REVOKED, ConnectionService

        for esid in dict.fromkeys(esids):
            try:
                conn = ConnectionService.get_connection(esid) or {}
            except Exception as e:  # noqa: BLE001 — display-only; never break the status payload
                logging.warning("connector_status: tiktok connection lookup failed for %s: %s", esid, e)
                conn = {}
            # Only an active row imports: expired (TikTok refused the refresh)
            # and revoked both need a reconnect, so neither counts as live.
            is_tiktok = conn.get("provider") == PROVIDER_TIKTOK
            live = is_tiktok and (conn.get("status") or "") == STATUS_ACTIVE
            linked.append(
                {
                    "esid": esid,
                    "title": conn.get("label") or esid,
                    "provider_id": conn.get("provider_account_id") or "",
                    "status": conn.get("status") or "missing",
                    "live": live,
                }
            )
            if live and first_live is None:
                first_live = esid
            if is_tiktok and (conn.get("status") or "") != STATUS_REVOKED and first_existing is None:
                first_existing = esid
    if not enabled:
        state = "not_configured"
    elif not linked:
        state = "needs_setup"
    elif first_live:
        state = "configured"
    else:
        state = "disconnected"
    return {
        "key": connector["key"],
        "label": connector["label"],
        "auth": connector["auth"],
        "configured": state == "configured",
        "state": state,
        "linked_assets": linked,
        "asset_type": None,
        "oauth_manage_asset_type": None,
        # The Manage link: a live connection, else an expired one (still
        # re-authorizable), never a revoked or deleted row.
        "connection_esid": first_live or first_existing,
    }


def _connector_status(team_source: dict[str, Any], connector: dict[str, Any], team_esid: str = "") -> dict[str, Any]:
    """Compute the status payload for a single connector for one team."""
    key = connector["key"]
    label = connector["label"]
    auth = connector["auth"]

    # Phase 3 — Bitly is special: the team's pointer targets
    # de_connection directly (no de_asset_* row to resolve), so the generic
    # linked-asset path below cannot compute it. State semantics mirror the
    # generic model:
    #   not_configured: ds_bitly off
    #   needs_setup:    flag on but no bitly_connection_esid stamped
    #   disconnected:   pointer set but the connection is revoked/deleted
    #   configured:     pointer resolves to a LIVE de_connection
    if key == "bitly":
        flag_value = team_source.get(connector["team_flag"])
        enabled = str(flag_value).lower() in ("true", "1", "yes")
        pointer = str(team_source.get(connector["team_esid_field"]) or "").strip()
        live = False
        if enabled and pointer:
            try:
                from .connection_service import ConnectionService

                live = ConnectionService.is_connection_live(pointer)
            except Exception as e:  # noqa: BLE001 — display-only; never break the status payload
                logging.warning("connector_status: bitly liveness check failed for %s: %s", pointer, e)
        if not enabled:
            state = "not_configured"
        elif not pointer:
            state = "needs_setup"
        elif live:
            state = "configured"
        else:
            state = "disconnected"
        has_saved_token, tags = _bitly_connection_info(pointer) if pointer else (False, [])
        return {
            "key": key,
            "label": label,
            "auth": auth,
            "configured": state == "configured",
            "state": state,
            "linked_assets": [],
            "asset_type": None,
            "oauth_manage_asset_type": None,
            # Live pointer → the Manage button deep-links to /connections/<esid>.
            "connection_esid": pointer if live else None,
            # Wizard prefill (dots for a saved token, stored tag filter).
            "has_saved_token": has_saved_token,
            "tags": tags,
        }

    # TikTok: the team lists de_connection esids directly.
    #   not_configured: ds_tiktok off
    #   needs_setup:    flag on but no connection linked
    #   configured:     at least one linked connection is live
    #   disconnected:   connections linked but none is active (expired, revoked
    #                   or deleted — each needs a reconnect)
    # linked_assets lists every linked connection (live or not) so the wizard
    # can offer Remove on a dead one; ``live`` marks which still import.
    if key == "tiktok":
        return _tiktok_status(team_source, connector)

    # Disciple.Tools is special — driven by a URL field, not a ds_* flag.
    if key == "disciple_tools":
        url = _to_display_string(team_source.get(connector["team_url_field"])).strip()
        configured = bool(url)
        return {
            "key": key,
            "label": label,
            "auth": auth,
            "configured": configured,
            "state": "configured" if configured else "not_configured",
            "linked_assets": [],
            "instance_url": url,
            # follow-up — whether a saved Site Link token is on file,
            # so the wizard can show the token field as filled (dots, the
            # MFA-secret pattern) without ever shipping the ciphertext. Only
            # looked up when configured.
            "has_saved_token": _dt_site_has_token(team_esid) if configured else False,
            "asset_type": None,
            "oauth_manage_asset_type": None,
        }

    flag_value = team_source.get(connector["team_flag"])
    enabled = str(flag_value).lower() in ("true", "1", "yes")

    linked_assets: list[dict[str, str]] = []
    resolved_via_fallback = False
    if enabled:
        esid_field_value = team_source.get(connector["team_esid_field"])
        logging.debug(
            "connector_status: resolving %s assets esid_field=%s value=%r",
            key,
            connector["team_esid_field"],
            esid_field_value,
        )
        linked_assets = _resolve_linked_assets(
            esid_field_value,
            connector["asset_index"],
            connector["asset_title_field"],
            id_field=connector.get("asset_id_field"),
            slug_field=connector.get("asset_slug_field"),
            kind=connector.get("asset_kind"),
        )
        logging.debug("connector_status: resolved %d assets for %s", len(linked_assets), key)
        # when a connector defines a fallback asset shape (e.g.
        # FB Ads' account_wide mode) and the primary field yielded
        # nothing, try the fallback. Keeps the card "configured" for
        # wizard-cohort teams whose wired asset is fb_ad_account, not
        # fb_page.
        if not linked_assets and connector.get("fallback_team_esid_field"):
            linked_assets = _resolve_linked_assets(
                team_source.get(connector["fallback_team_esid_field"]),
                connector["fallback_asset_index"],
                connector["fallback_asset_title_field"],
                id_field=connector.get("fallback_asset_id_field"),
                kind=connector.get("fallback_asset_kind"),
            )
            if linked_assets:
                resolved_via_fallback = True

    # State semantics (the single ``misconfigured`` blob was split
    # into two honest states so the card can convey the cause and offer the
    # right remediation):
    #   not_configured: ds_* flag is off
    #   needs_setup:    flag is on but no ESID links resolve (dangling /
    #                   never-finished link — "finish linking an asset")
    #   disconnected:   flag is on and a link resolves, but the underlying
    #                   de_connection is revoked/deleted (set below; the
    #                   connector can't pull data until reconnected)
    #   configured:     flag is on and ≥1 link resolves with a live connection
    if not enabled:
        state = "not_configured"
    elif not linked_assets:
        state = "needs_setup"
    else:
        state = "configured"

    # when the fallback path resolved the card, route action
    # buttons to the fallback's asset shape (otherwise "View in Asset
    # Explorer" 404s on the primary's slug) and drop oauth_manage
    # (wizard-cohort teams hold their token on de_connection, not
    # de_asset_fb_app_users — the legacy list view is empty for them).
    if resolved_via_fallback and connector.get("fallback_asset_type"):
        payload_asset_type = connector["fallback_asset_type"]
        payload_oauth_manage_asset_type: str | None = None
        resolved_legacy_index = connector.get("fallback_asset_index")
    else:
        payload_asset_type = connector["asset_type"]
        payload_oauth_manage_asset_type = connector.get("oauth_manage_asset_type")
        resolved_legacy_index = connector.get("asset_index")

    # stamp a ``connection_esid`` on the card when the
    # team's linked assets resolve back to a ``de_connection_asset``
    # row. The "Manage connection" button
    # on the Data Connectors card deep-links to ``/connections/<esid>``
    # using this id. Cards without a resolvable connection
    # (legacy-only teams, or providers that don't write
    # de_connection_asset rows) emit ``connection_esid: None`` and
    # the template hides the Manage button.
    connection_esid: str | None = None
    if state == "configured" and resolved_legacy_index and linked_assets:
        try:
            from .connection_service import ConnectionService

            # when the linked assets span several live connections,
            # resolve to the one the team's latest wizard run stamped.
            connection_esid = ConnectionService.find_connection_esid_by_legacy_assets(
                resolved_legacy_index,
                [a["esid"] for a in linked_assets if a.get("esid")],
                prefer=_provider_connection_stamp(team_source, key),
            )
            # teams wired BEFORE the helper
            # started writing de_connection_asset rows (e.g. Kavanah
            # Media on staging) have no legacy_asset_esid backlink, so the
            # lookup above returns None even though a real de_connection
            # with the OAuth token exists. The bridge ALSO stamps
            # ``connection_esid`` straight onto the team doc (the field
            # the importer's account-wide credential resolver reads), and
            # each wizard-written asset doc carries ``wizard_connection_esid``.
            # Fall back to those so the Manage / connected-summary surface
            # resolves for the existing cohort without a backfill. Only
            # accept a connection that still exists (get_connection != None)
            # so we never deep-link to a deleted row.
            if not connection_esid:
                connection_esid = _resolve_connection_esid_fallback(
                    team_source,
                    resolved_legacy_index,
                    linked_assets,
                    key=key,
                )
        except Exception as e:
            logging.warning(
                "connector_status_service: connection_esid resolution failed for %s: %s",
                key,
                e,
            )

    # Meta Ads is a merged FB+IG card. The primary
    # ds_facebook branch above resolved FB state; here we layer IG on top.
    # made IG additive rather than gating. The card's pill semantics:
    #   not_configured: ds_facebook off (whether IG is on or not)
    #   needs_setup:    FB on but FB link unresolved, OR ds_instagram on but
    #                   its link doesn't resolve (a real dangling IG link)
    #   configured:     FB OK. IG absent (ds_instagram off) is FINE — FB-only
    #                   is a valid configuration, reported via ig_configured.
    ig_linked_assets: list[dict[str, str]] = []
    ig_enabled = False
    if key == "meta_ads":
        ig_flag_value = team_source.get(connector.get("ig_team_flag", ""))
        ig_enabled = str(ig_flag_value).lower() in ("true", "1", "yes")
        if ig_enabled:
            ig_linked_assets = _resolve_linked_assets(
                team_source.get(connector.get("ig_team_esid_field", "")),
                connector.get("ig_asset_index", ""),
                connector.get("ig_asset_title_field", ""),
                kind="ig_account",  # tag so the billing rollup can label IG distinctly
            )
        # FB-only is a fully valid configuration, NOT a problem.
        # Many teams run Facebook with no Instagram. The bridge writes
        # ``ds_instagram=false`` for some; the account_wide wizard cohort
        # writes ``ds_instagram=true`` with NO ``team_instagram_profile_esids``
        # at all (IG profiles aren't part of the ad-account flow). Both are
        # legitimate FB-configured states. The earlier code downgraded all
        # of them to "Needs attention" — a false alarm with no remediation.
        # Instagram is now purely additive: ``ig_configured`` + the card's
        # "+ Y Instagram accounts" line carry the IG side without gating FB.
        #
        # The one case that IS a real problem: IG ESIDs are PRESENT on the team
        # doc but none of them resolve to a doc — a genuine dangling link, the
        # same shape as the FB-side dangling case → ``needs_setup``. An ABSENT
        # / empty IG ESID field is "IG never wired", which is fine and stays
        # ``configured``. (follow-up 2026-06-15: the original branch
        # keyed on ``not ig_linked_assets`` alone, which wrongly downgraded the
        # account_wide cohort — ds_instagram=true, no IG ESIDs — to needs_setup.)
        ig_esids_present = bool(team_source.get(connector.get("ig_team_esid_field", "")))
        if state == "configured" and ig_enabled and ig_esids_present and not ig_linked_assets:
            state = "needs_setup"

    # downgrade a configured card to "disconnected" when
    # its connection has been revoked or deleted. The flag + linked asset still
    # resolve (so state computed "configured"), but without a LIVE de_connection
    # the connector can't pull data. gives this its own state (was the
    # generic "misconfigured") so the card can say "Disconnected — reconnect to
    # resume, or remove this connector" instead of an unexplained amber pill.
    #
    # Scoped to YouTube + GA4 + Google Ads only: these are wizard-era
    # connectors where every configured team SHOULD have a de_connection, so
    # connection_esid=None unambiguously means revoked/deleted. meta_ads is
    # excluded — it has a pre-wizard legacy-asset cohort that never wrote a
    # de_connection and would be false-downgraded. api_key (echo/chatwoot)
    # and url (disciple_tools) connectors never have a connection at all.
    if state == "configured" and key in ("youtube", "google_analytics", "google_ads") and connection_esid is None:
        state = "disconnected"

    # Meta's version of the downgrade above. meta_ads is excluded
    # from the blanket rule because its pre-wizard legacy cohort (no team-doc
    # ``connection_esid`` at all) never had a de_connection and would be
    # false-downgraded. But a team whose ``connection_esid`` field IS set
    # demonstrably once had a wizard connection; when that pointer no longer
    # resolves to a LIVE doc (deleted or revoked), the green pill was masking
    # a broken connection and the dialog dropped every viewer — including the
    # admin view — into the first-time setup wizard. Downgrade that
    # SET-BUT-DANGLING case to "disconnected"; leave the never-set legacy
    # cohort as-is. A pointer that resolves live but belongs to ANOTHER
    # provider (the shared legacy field can be stamped by any wizard)
    # says nothing about Meta and stays configured. Known residual: a dangling
    # pointer stamped by a earlier non-Meta wizard is indistinguishable
    # once its doc is deleted and downgrades too — accepted; the card offers
    # Reconnect either way.
    if state == "configured" and key == "meta_ads" and connection_esid is None:
        _meta_ptr = str(team_source.get("connection_esid") or "").strip()
        if _meta_ptr:
            try:
                from .connection_service import ConnectionService

                if not ConnectionService.is_connection_live(_meta_ptr):
                    state = "disconnected"
            except Exception as e:  # noqa: BLE001 — display-only; never break the status payload
                logging.warning("dangling-pointer check failed for %s: %s", _meta_ptr, e)

    # a Google Ads / GA4 connection that resolves but carries NO token
    # is un-importable (their service accounts were retired,
    # so there is no fallback). The wizard's tokenless "configure" path could land
    # such a row as active, making the card read "connected" while every import
    # fails with missing_credentials. Treat tokenless (pending_token or an
    # active-but-tokenless legacy row) as disconnected so the card prompts a real
    # OAuth authorization. YouTube is excluded — its token is always obtained at
    # oauth-start before configure, so it never lands tokenless.
    if state == "configured" and key in ("google_analytics", "google_ads") and connection_esid is not None:
        try:
            from .connection_service import ConnectionService

            _conn = ConnectionService.get_connection(connection_esid) or {}
            if not (_conn.get("oauth_token_enc") or ""):
                state = "disconnected"
        except Exception as e:  # noqa: BLE001 — display-only; never break the status payload
            logging.warning("tokenless-connection check failed for %s: %s", connection_esid, e)

    # flag a Google Ads customer that's assigned to MORE THAN ONE
    # Active+enabled team. The importer deterministically picks one
    # team and the others silently receive no data; surface that here so the
    # operator can see it and remove the customer from the extra team(s) via
    # per-account removal. Display-only, additive, Google-Ads-only —
    # each asset gains ``also_on_teams: [{esid, title}]`` listing the OTHER teams
    # (empty/absent when the customer is on exactly this team). Skipped unless the
    # card has resolved assets, to avoid a needless query for unconfigured cards.
    # once any of the sharing teams has campaign rules the account is
    # split per campaign instead (``split_by_campaign_rules``), and another
    # workspace's team is never named or identified (title/esid None → "another
    # team"), the posture.
    if key in _MULTI_ACCOUNT_CONNECTORS and linked_assets:
        team_map = _google_ads_customer_team_map([a.get("esid", "") for a in linked_assets])
        own_ws = str(team_source.get("workspace_esid") or "")
        own_rules = bool(
            team_source.get("team_google_ads_campaign_labels") or team_source.get("team_google_ads_campaign_prefixes")
        )
        for a in linked_assets:
            others = [t for t in team_map.get(a.get("esid", ""), []) if t.get("esid") != team_esid]
            if others:
                a["also_on_teams"] = [
                    {
                        "esid": t["esid"] if own_ws and t.get("workspace_esid") == own_ws else None,
                        "title": t["title"] if own_ws and t.get("workspace_esid") == own_ws else None,
                        "has_rules": t.get("has_rules", False),
                    }
                    for t in others
                ]
                a["split_by_campaign_rules"] = own_rules or any(t.get("has_rules") for t in others)

    payload: dict[str, Any] = {
        "key": key,
        "label": label,
        "auth": auth,
        "configured": state == "configured",
        "state": state,
        "linked_assets": linked_assets,
        "asset_type": payload_asset_type,
        "oauth_manage_asset_type": payload_oauth_manage_asset_type,
        # None for cards that don't resolve to a de_connection
        # (legacy assets, api_key connectors). The template gates the
        # Manage button on truthiness.
        "connection_esid": connection_esid,
    }

    # expose the IG side on the merged Meta Ads card so the
    # UI can render "X Facebook Pages + Y Instagram accounts" without a
    # second connector row. Empty list on non-Meta cards.
    if key == "meta_ads":
        payload["ig_linked_assets"] = ig_linked_assets
        payload["ig_configured"] = ig_enabled and bool(ig_linked_assets)

    # Token-health rollup for FB (now the Meta merged card). Cheap when
    # configured (one ES query for apps, one for users); skipped when
    # not configured.
    if key == "meta_ads" and state == "configured":
        try:
            payload["token_health"] = _resolve_fb_token_health(linked_assets)
        except Exception as e:
            # nosemgrep: python-logger-credential-disclosure - logs the exception type from FB token-health probing, not a credential. The string "token_health" is a payload-key name.
            logging.warning("token_health resolution failed: %s", e)
            payload["token_health"] = None

    return payload


def get_team_connectors(
    team_esid: str,
    allowed_workspaces: list[str] | None = None,
) -> dict[str, Any] | None:
    """Return the team's connector status payload, or None if the team is not found.

    tenant-scoping contract:

    * ``allowed_workspaces is None`` → no scope check (legacy; superuser
      / asset_admin).
    * ``allowed_workspaces == []`` → deny-all; returns ``None`` without
      reading ES.
    * Otherwise → returns ``None`` when the looked-up team's
      ``workspace_esid`` is not in the allowed set. Mirrors the Asset
      Explorer's ``_load_dashboard_for_read`` deny pattern: collapse
      cross-tenant access into the same shape as "team not found" so
      the API can't be used to enumerate team existence across
      tenants.

    Shape::

        {
            "team": {"esid": ..., "title": ..., "org_title": ...},
            "connectors": [
                {"key": "meta_ads", "label": "Meta Ads", "auth": "oauth",
                 "configured": True, "state": "configured",
                 "linked_assets": [{"esid": ..., "title": ...}, ...],
                 "ig_linked_assets": [{"esid": ..., "title": ...}, ...],
                 "ig_configured": True,
                 "asset_type": "facebook_pages",
                 "oauth_manage_asset_type": "facebook_app_users",
                 "connection_esid": "abc123"},
                ...
            ],
        }

    ``connection_esid`` is the de_connection doc id when
    the team's linked legacy assets resolve back via
    ``de_connection_asset.legacy_asset_esid``. None for legacy-only
    teams and for connectors that don't write de_connection rows
    (api_key connectors, disciple_tools).
    """
    if not team_esid:
        return None

    if allowed_workspaces is not None and not allowed_workspaces:
        return None

    try:
        source = get_document_by_id(TEAMS_INDEX, team_esid)
    except Exception as e:
        logging.warning("connector_status_service.get_team_connectors lookup: %s", e)
        return None
    if not source:
        return None

    if allowed_workspaces is not None:
        team_ws = source.get("workspace_esid")
        if not team_ws or team_ws not in allowed_workspaces:
            return None

    team_payload = {
        "esid": team_esid,
        "title": _to_display_string(source.get("team_title")) or team_esid,
        # resolve the org name from organization_esid for wizard teams
        # (which store no organization_title), so the Data Connectors header shows
        # the real org beside the selected team instead of a blank.
        "org_title": _resolve_org_title(source),
    }

    connectors = [_connector_status(source, c, team_esid=team_esid) for c in CONNECTORS]
    return {"team": team_payload, "connectors": connectors}


# connector key → its definition, for the disconnect path.
CONNECTORS_BY_KEY: dict[str, dict[str, Any]] = {c["key"]: c for c in CONNECTORS}


class ConnectorDisconnectError(UserFacingError):
    """Raised when a connector cannot be disconnected (unknown key, bad write)."""


# connectors whose team→account link is a genuine multi-value list,
# so a single account can be detached without disconnecting the whole connector.
# Single-value connectors (echo, chatwoot, disciple_tools) store their lone
# account under the same ``team_esid_field`` key as a scalar, so they cannot be
# distinguished from a multi-account list by config shape alone — hence an
# explicit allowlist.
#
# extended to YouTube (team_youtube_channel_handle_esids) and GA4
# (team_ga4_property_esids); both are clean list fields with a ds_* flag and no
# scalar fallback, so the generic remove_connector_account logic applies as-is.
#
# extended to Meta (meta_ads). Meta does NOT fit the single-list
# shape: it's a merged FB+IG card with THREE removal targets across TWO flags —
# the FB pages list (team_facebook_page_esids, ds_facebook), the IG accounts
# list (team_instagram_profile_esids, ds_instagram), and the single ad-account
# scalar (fb_ad_account_esid, ds_facebook). remove_connector_account routes
# meta_ads through a dedicated branch (_remove_meta_account) that searches all
# three, removes from whichever holds the account, and flips ds_facebook only
# when BOTH FB-side targets are empty / ds_instagram only when the IG list is.
# tiktok: team_tiktok_connection_esids is a plain list with a ds_*
# flag and no scalar fallback, so the shared list path applies as-is.
_MULTI_ACCOUNT_CONNECTORS = {"google_ads", "youtube", "google_analytics", "meta_ads", "tiktok"}


def _audit_reference_strips(
    path: str,
    team_esid: str,
    team_source: dict[str, Any],
    connector_key: str,
    removed: list[tuple[str, str]],
) -> None:
    """audit-log each (field, esid) reference the Data Connectors
    page's disconnect / per-account remove stripped from a team doc. Called
    after the team write succeeds. Fail-soft — audit emission never fails
    the user's action.
    """
    if not removed:
        return
    try:
        try:
            from .audit_log_service import AuditLogService
        except ImportError:  # pragma: no cover - import boundary
            from asset_management.web.services.audit_log_service import AuditLogService  # type: ignore

        for field, esid in removed:
            AuditLogService.log_reference_removed(
                path=path,
                child_index=TEAMS_INDEX,
                child_id=team_esid,
                child_title=str(team_source.get("team_title") or ""),
                field=field,
                removed_esid=esid,
                connector=connector_key,
            )
    except Exception as e:  # noqa: BLE001 — attribution only; the removal already landed
        logging.warning("_audit_reference_strips: audit emission failed: %s", e)


def disconnect_connector(
    team_esid: str,
    connector_key: str,
    allowed_workspaces: list[str] | None = None,
) -> dict[str, Any]:
    """sever a team's pointer to a connector so the card returns to
    "not configured", clearing the dangling/revoked "Needs attention" state.

    Turns off the connector's ``ds_*`` flag and clears the team's ``*_esid``
    field(s). For Meta this includes the FB fallback ad-account field and the
    Instagram flag + field; for Disciple.Tools it clears the instance URL field.

    **Scope of the write is the team document only.** The underlying
    ``de_connection`` (OAuth credential) is left untouched — it may be shared
    with other teams/cards and the grant stays valid for re-linking. Revoking
    the credential is a separate action on the Manage Connection page. (User
    decision, 2026-06-15.)

    This is the team→asset removal the user explicitly requested, so it does
    not route through ``ReferenceCleanupService`` (that service scrubs team
    back-references when an *asset* is deleted — the opposite direction). The
    asset docs themselves are intentionally left in place; only the team's
    pointer and flag are cleared.

    Tenant-scoping mirrors ``get_team_connectors``:
      * ``allowed_workspaces is None`` → no scope check (superuser / asset_admin).
      * ``allowed_workspaces == []`` → deny-all; raises (collapsed to not-found
        by the caller).
      * otherwise → the team's ``workspace_esid`` must be in the set.

    Returns the freshly recomputed connector payload (same shape as
    ``get_team_connectors``) so the caller can repaint the card without a
    second round-trip. Raises ``ConnectorDisconnectError`` on an unknown key,
    a missing/out-of-scope team, or a failed write.
    """
    connector = CONNECTORS_BY_KEY.get(connector_key)
    if connector is None:
        raise ConnectorDisconnectError(f"Unknown connector: {connector_key}")

    if allowed_workspaces is not None and not allowed_workspaces:
        raise ConnectorDisconnectError("Team not found")

    try:
        source = get_document_by_id(TEAMS_INDEX, team_esid)
    except Exception as e:
        logging.warning("disconnect_connector lookup failed for %s: %s", team_esid, e)
        raise ConnectorDisconnectError("Team not found") from e
    if not source:
        raise ConnectorDisconnectError("Team not found")

    if allowed_workspaces is not None:
        team_ws = source.get("workspace_esid")
        if not team_ws or team_ws not in allowed_workspaces:
            # Collapse cross-tenant into not-found, same as get_team_connectors.
            raise ConnectorDisconnectError("Team not found")

    # Build the patch: turn off every flag this connector owns and clear every
    # esid/url field it points at. Only fields the connector actually defines
    # are touched, so unrelated team data is never disturbed.
    updated = dict(source)
    flag_fields = [connector.get("team_flag"), connector.get("ig_team_flag")]
    esid_fields = [
        connector.get("team_esid_field"),
        connector.get("fallback_team_esid_field"),
        connector.get("ig_team_esid_field"),
        connector.get("team_url_field"),
        # the denormalized natural-id twin the importer matches on
        # (team_facebook_page_id / team_ga4_properties). Clearing only the esid
        # left this populated, and the GA4/FB importers match on it directly, so
        # a "disconnected" team kept importing. A whole-connector disconnect
        # clears everything, so blanking the denorm field wholesale is correct
        # here (the per-element case is remove_connector_account, below).
        connector.get("team_denorm_field"),
    ]
    # collect the references being cleared so each one lands in the
    # audit log once the write succeeds.
    stripped: list[tuple[str, str]] = []
    for f in flag_fields:
        if f:
            updated[f] = "false"
    for f in esid_fields:
        if not f:
            continue
        # Mirror the stored shape: list fields → [], scalar/url fields → "".
        existing = source.get(f)
        if isinstance(existing, list):
            stripped.extend((f, str(v)) for v in existing if v)
        elif existing and str(existing).strip():
            stripped.append((f, str(existing).strip()))
        updated[f] = [] if isinstance(existing, list) else ""

    try:
        from .asset_service import AssetService
    except ImportError:  # pragma: no cover - import boundary
        from asset_management.web.services.asset_service import AssetService  # type: ignore

    if not AssetService.update(TEAMS_INDEX, team_esid, updated):
        raise ConnectorDisconnectError("Could not update team")

    _audit_reference_strips("disconnect", team_esid, source, connector_key, stripped)

    logging.info(
        "disconnect_connector: team=%s connector=%s flags_off=%s fields_cleared=%s",
        team_esid,
        connector_key,
        [f for f in flag_fields if f],
        [f for f in esid_fields if f],
    )

    # Recompute and return the fresh payload so the UI repaints from truth.
    return get_team_connectors(team_esid, allowed_workspaces=allowed_workspaces) or {
        "team": {"esid": team_esid, "title": team_esid, "org_title": ""},
        "connectors": [],
    }


def _strip_denorm_element(
    connector: dict[str, Any],
    updated: dict[str, Any],
    source: dict[str, Any],
    account_esid: str,
) -> None:
    """on single-account removal, also drop the removed account's
    entry from the connector's denormalized natural-id field (in place on
    ``updated``).

    The denorm field (``team_denorm_field``, e.g. ``team_ga4_properties`` /
    ``team_facebook_page_id``) stores the asset's NATURAL id, while removal is
    keyed by the asset ESID — so we resolve esid → natural id by fetching the
    asset doc and reading ``asset_id_field``. Only the matching element is
    removed; other accounts' denorm entries are preserved (mirrors the esid-list
    surgery). Shape-preserving: list → filtered list, scalar → "" on match.

    No-op when the connector has no denorm field, when the asset can't be
    resolved, or when the natural id isn't present in the denorm field. Failure
    to resolve degrades to leaving the denorm field alone rather than blanking
    it — the esid was already removed, so the importer's esid leg no longer
    matches; the denorm leg is defense-in-depth and must not corrupt other
    accounts' data on a lookup hiccup.
    """
    denorm_field = connector.get("team_denorm_field")
    asset_id_field = connector.get("asset_id_field")
    asset_index = connector.get("asset_index")
    if not denorm_field or not asset_id_field or not asset_index:
        return

    try:
        asset = get_document_by_id(asset_index, account_esid)
    except Exception as e:  # noqa: BLE001 — resolution is best-effort defense-in-depth
        logging.warning("denorm resolve failed for %s/%s: %s", asset_index, account_esid, e)
        return
    if not asset:
        return
    natural_id = asset.get(asset_id_field)
    if natural_id is None or natural_id == "":
        return
    natural_id = str(natural_id)

    existing = source.get(denorm_field)
    if isinstance(existing, list):
        updated[denorm_field] = [v for v in existing if str(v) != natural_id]
    elif existing is not None and str(existing) == natural_id:
        updated[denorm_field] = ""


def _remove_meta_account(
    connector: dict[str, Any],
    source: dict[str, Any],
    account_esid: str,
) -> tuple[dict[str, Any], str]:
    """build the team-doc patch that detaches ONE Meta asset.

    Meta spans three team-doc targets across two flags:
      * FB pages list   — ``team_facebook_page_esids``     (flag ``ds_facebook``)
      * IG accounts list — ``team_instagram_profile_esids`` (flag ``ds_instagram``)
      * ad-account scalar — ``fb_ad_account_esid``           (flag ``ds_facebook``)

    The single ``account_esid`` names exactly one of those, so we search all
    three and remove from whichever holds it. Flag bookkeeping is per-side:
    ``ds_facebook`` turns off only when BOTH FB-side targets (pages list +
    ad-account scalar) are empty; ``ds_instagram`` turns off only when the IG
    list is empty. The underlying ``de_connection`` credential is left in place
    (same policy as).

    Returns ``(updated_team_doc, removed_from)`` where ``removed_from`` is one of
    ``"fb_pages" | "ig_accounts" | "ad_account"`` (for the log line). Raises
    ``ConnectorDisconnectError`` when the account is linked to none of them.
    """
    page_field = connector.get("team_esid_field")  # team_facebook_page_esids
    ig_field = connector.get("ig_team_esid_field")  # team_instagram_profile_esids
    ad_field = connector.get("fallback_team_esid_field")  # fb_ad_account_esid (scalar)
    fb_flag = connector.get("team_flag")  # ds_facebook
    ig_flag = connector.get("ig_team_flag")  # ds_instagram

    def _as_list(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value] if value else []
        return list(value or [])

    pages = _as_list(source.get(page_field))
    ig_accounts = _as_list(source.get(ig_field))
    ad_scalar = (source.get(ad_field) or "").strip() if ad_field else ""

    updated = dict(source)
    removed_from = ""

    if account_esid in pages:
        pages = [e for e in pages if e != account_esid]
        updated[page_field] = pages
        removed_from = "fb_pages"
        # the FB page importer matches on team_facebook_page_id
        # (denorm), not the esid, so the removed page's denorm entry must go too.
        # Only the FB-pages target has a denorm twin; IG / ad-account do not.
        #   * pages remain → strip only the removed page's matching denorm id.
        #   * last FB-side link gone (pages empty AND no ad-account scalar) →
        #     full FB teardown, so blank the whole denorm field (same reasoning
        #     as the shared list path: element-wise can't catch orphan denorm
        #     ids no surviving esid backs, and one leftover keeps the importer
        #     matching a disconnected team).
        denorm_field = connector.get("team_denorm_field")
        if denorm_field and not pages and not ad_scalar:
            existing = source.get(denorm_field)
            updated[denorm_field] = [] if isinstance(existing, list) else ""
        else:
            _strip_denorm_element(connector, updated, source, account_esid)
    elif account_esid in ig_accounts:
        ig_accounts = [e for e in ig_accounts if e != account_esid]
        updated[ig_field] = ig_accounts
        removed_from = "ig_accounts"
    elif ad_field and account_esid == ad_scalar:
        updated[ad_field] = ""
        ad_scalar = ""
        removed_from = "ad_account"
    else:
        raise ConnectorDisconnectError("Account is not linked to this team")

    # Per-side flag bookkeeping. ds_facebook spans BOTH FB-side targets, so it
    # only goes off once the pages list and the ad-account scalar are both empty.
    if fb_flag and not pages and not ad_scalar:
        updated[fb_flag] = "false"
    if ig_flag and not ig_accounts:
        updated[ig_flag] = "false"

    return updated, removed_from


def remove_connector_account(
    team_esid: str,
    connector_key: str,
    account_esid: str,
    allowed_workspaces: list[str] | None = None,
) -> dict[str, Any]:
    """detach a SINGLE account from a team without disconnecting the
    whole connector.

    Sibling of ``disconnect_connector``: same team-document-only scope, same
    tenant guard, same recompute-and-return contract. The difference is that
    this removes one ``account_esid`` from the connector's multi-value list
    field (and the legacy scalar fallback if it points at the same account)
    instead of clearing everything, and it only turns the ``ds_*`` flag off
    when the LAST account is removed (zero accounts left = effectively
    disconnected). The underlying ``de_connection`` credential is left in place
    (same policy as).

    Scoped to genuinely MULTI-ACCOUNT connectors (see
    ``_MULTI_ACCOUNT_CONNECTORS`` — Google Ads, YouTube, GA4, and Meta).
    ``meta_ads`` is special-cased to ``_remove_meta_account`` (three targets
    across two flags); the others share the single-list path below. Single-value
    connectors (Echo, Chatwoot, DT) store their lone account under the same
    ``team_esid_field`` key as a scalar, so the config alone can't tell them
    apart from a multi-account list — an explicit allowlist is the honest gate.
    For those, "remove account" is just ``disconnect_connector``, so this
    raises rather than silently clearing the scalar.

    Tenant-scoping and error collapse mirror ``disconnect_connector``.
    Returns the freshly recomputed connector payload. Raises
    ``ConnectorDisconnectError`` on an unknown/unsupported connector, a
    missing/out-of-scope team, an account not linked to the team, or a failed
    write.
    """
    connector = CONNECTORS_BY_KEY.get(connector_key)
    if connector is None:
        raise ConnectorDisconnectError(f"Unknown connector: {connector_key}")

    if connector_key not in _MULTI_ACCOUNT_CONNECTORS:
        # Single-value connector (or one not yet opted into per-account removal):
        # there is no individual account to detach — use disconnect instead.
        raise ConnectorDisconnectError(f"Connector {connector_key} does not support per-account removal")

    list_field = connector.get("team_esid_field")
    if not list_field:
        raise ConnectorDisconnectError(f"Connector {connector_key} has no account link field")

    if not account_esid:
        raise ConnectorDisconnectError("account_esid is required")

    if allowed_workspaces is not None and not allowed_workspaces:
        raise ConnectorDisconnectError("Team not found")

    try:
        source = get_document_by_id(TEAMS_INDEX, team_esid)
    except Exception as e:
        logging.warning("remove_connector_account lookup failed for %s: %s", team_esid, e)
        raise ConnectorDisconnectError("Team not found") from e
    if not source:
        raise ConnectorDisconnectError("Team not found")

    if allowed_workspaces is not None:
        team_ws = source.get("workspace_esid")
        if not team_ws or team_ws not in allowed_workspaces:
            # Collapse cross-tenant into not-found, same as disconnect_connector.
            raise ConnectorDisconnectError("Team not found")

    # Meta is a merged FB+IG card with three removal targets across
    # two flags; it does not fit the single-list path below. Route it to its own
    # multi-target patch builder, then share the write/recompute tail.
    if connector_key == "meta_ads":
        updated, removed_from = _remove_meta_account(connector, source, account_esid)

        try:
            from .asset_service import AssetService
        except ImportError:  # pragma: no cover - import boundary
            from asset_management.web.services.asset_service import AssetService  # type: ignore

        if not AssetService.update(TEAMS_INDEX, team_esid, updated):
            raise ConnectorDisconnectError("Could not update team")

        # name the actual team-doc field the account left.
        _meta_field = {
            "fb_pages": connector.get("team_esid_field"),
            "ig_accounts": connector.get("ig_team_esid_field"),
            "ad_account": connector.get("fallback_team_esid_field"),
        }.get(removed_from) or removed_from
        _audit_reference_strips("account_remove", team_esid, source, connector_key, [(_meta_field, account_esid)])

        logging.info(
            "remove_connector_account: team=%s connector=meta_ads removed=%s target=%s ds_facebook=%s ds_instagram=%s",
            team_esid,
            account_esid,
            removed_from,
            updated.get(connector.get("team_flag", "")),
            updated.get(connector.get("ig_team_flag", "")),
        )
        return get_team_connectors(team_esid, allowed_workspaces=allowed_workspaces) or {
            "team": {"esid": team_esid, "title": team_esid, "org_title": ""},
            "connectors": [],
        }

    # The list field is the source of truth for which accounts are linked. A
    # earlier team may only have the legacy scalar; fold it into the
    # working set so removal works during the transition window too.
    current = source.get(list_field) or []
    if isinstance(current, str):
        current = [current] if current else []
    current = list(current)

    fallback_field = connector.get("fallback_team_esid_field")
    legacy_scalar = (source.get(fallback_field) or "").strip() if fallback_field else ""
    if legacy_scalar and legacy_scalar not in current:
        current.append(legacy_scalar)

    if account_esid not in current:
        raise ConnectorDisconnectError("Account is not linked to this team")

    remaining = [e for e in current if e != account_esid]

    updated = dict(source)
    updated[list_field] = remaining
    # Drop the legacy scalar whenever it named the removed account, so the two
    # fields can't disagree (the scalar is the importer's pre-list fallback).
    if fallback_field and legacy_scalar == account_esid:
        updated[fallback_field] = ""
    # Zero accounts left → the connector is effectively disconnected; turn the
    # flag off so the card returns to "not configured".
    flag_field = connector.get("team_flag")
    if flag_field and not remaining:
        updated[flag_field] = "false"
    # clear the denormalized natural-id field the importer matches on
    # (GA4: team_ga4_properties). No-op for connectors without a
    # team_denorm_field (google_ads, youtube).
    #   * accounts remain → strip ONLY the removed account's matching id
    #     (resolve esid → ga_property_id via the asset doc), preserving the rest.
    #   * LAST account removed (list now empty, flag off) → this is a full
    #     teardown, same end-state as disconnect_connector, so blank the whole
    #     denorm field. Element-wise strip can't catch denorm ids that no
    #     surviving esid backs (e.g. historical orphans, or a denorm/esid count
    #     mismatch), and leaving even one lets the importer keep matching a
    # "disconnected" team — the exact vector.
    denorm_field = connector.get("team_denorm_field")
    if denorm_field and not remaining:
        existing = source.get(denorm_field)
        updated[denorm_field] = [] if isinstance(existing, list) else ""
    else:
        _strip_denorm_element(connector, updated, source, account_esid)

    try:
        from .asset_service import AssetService
    except ImportError:  # pragma: no cover - import boundary
        from asset_management.web.services.asset_service import AssetService  # type: ignore

    if not AssetService.update(TEAMS_INDEX, team_esid, updated):
        raise ConnectorDisconnectError("Could not update team")

    _audit_reference_strips("account_remove", team_esid, source, connector_key, [(list_field, account_esid)])

    logging.info(
        "remove_connector_account: team=%s connector=%s removed=%s remaining=%d flag_off=%s",
        team_esid,
        connector_key,
        account_esid,
        len(remaining),
        bool(flag_field and not remaining),
    )

    return get_team_connectors(team_esid, allowed_workspaces=allowed_workspaces) or {
        "team": {"esid": team_esid, "title": team_esid, "org_title": ""},
        "connectors": [],
    }
