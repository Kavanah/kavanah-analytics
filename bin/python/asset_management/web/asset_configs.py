"""
Asset type configurations for the Web Asset Manager.
Each asset type defines its Elasticsearch index, display settings, and field definitions.
"""

# the per-team time-range preset catalog lives in one place
# (time_preset_service) so the team form, the server union resolver, and the
# release-plan test all agree on the canonical values/labels. ``form_options``
# is the editable subset ("All time" excluded — always offered to viewers).
try:
    from web.services.time_preset_service import (
        TEAM_FIELD as _TIME_PRESETS_FIELD,
        form_options as _time_preset_form_options,
    )
except ImportError:  # tooling / test contexts that import via a different root
    from asset_management.web.services.time_preset_service import (
        TEAM_FIELD as _TIME_PRESETS_FIELD,
        form_options as _time_preset_form_options,
    )

# reusable validation patterns for the few user-typed fields whose
# format is unambiguous (URL, email, numeric / known-format IDs). Applied via a
# field's ``pattern`` (enforced client-side as HTML ``pattern`` AND server-side
# in FormService.validate_required_fields). Deliberately LENIENT — name / title /
# description fields get a length cap only and NO pattern, because they
# legitimately contain accents, apostrophes, spaces and other characters a
# strict regex would wrongly reject. Patterns here accept every known-valid form
# of the value rather than a single canonical shape, so they never reject data
# the importers already accept.
_PATTERN_URL = r"https?://.+"  # any http(s) URL; browser type=url also validates
_PATTERN_EMAIL = r"[^@\s]+@[^@\s]+\.[^@\s]+"
_PATTERN_NUMERIC_ID = r"\d+"  # purely-numeric platform IDs
_PATTERN_GOOGLE_ADS_ID = r"\d{3}-?\d{3}-?\d{4}"  # 123-456-7890 or 1234567890
_PATTERN_YT_HANDLE = r"@?[A-Za-z0-9._-]+"  # @handle or bare handle

# (Item 2b): optional superuser-only Workspace selector for data-source
# assets (echo tenants, FB/GA/Google-Ads/IG/YouTube/Chatwoot accounts, etc.).
#
# These assets carry their own load-bearing ``workspace_esid`` (read directly by
# the picker, Asset Explorer list, RoleFilterService boundary, and
# never resolved through the linked team). Before this field
# existed there was NO UI control to set it: a superuser creating a data-source
# asset always got it stamped to their own primary workspace, so
# placing one in another workspace required an out-of-band ES re-stamp (hit
# during the tripwire setup).
#
# ``required=False`` (unlike the orgs/teams workspace field, which is required):
# absence is the common case and falls back to the existing
# creator-primary stamp, so adding this field is purely additive — no existing
# create flow gains a new validation failure. When a superuser DOES pick a
# workspace, ``process_form_data`` writes it to ``data["workspace_esid"]`` and
# the pre-block's ``"workspace_esid" not in data`` guard preserves it.
#
# The tier assets (teams/organizations) keep their own ``required=True`` version
# inline — for them the workspace is mandatory, not an optional override.
_SUPERUSER_WORKSPACE_FIELD = {
    "name": "workspace_title",
    "label": "Workspace",
    "type": "select",
    "source": "de_asset_workspace",
    "source_field": "workspace_title",
    "store_esid": True,
    "esid_field": "workspace_esid",
    "required": False,
    "superuser_only": True,
    "help_text": "Optional: target workspace. Defaults to your primary workspace.",
}

ASSET_CONFIGS = {
    # ============================================
    # COMPANY ASSETS
    # ============================================
    "teams": {
        "index": "de_asset_teams",
        "title": "Teams",
        "title_field": "team_title",
        "description": "Manage organization teams",
        "category": "company",
        "fields": [
            {
                "name": "team_title",
                "label": "Title",
                "type": "text",
                "required": True,
            },
            {"name": "team_name", "label": "Name", "type": "text"},
            {
                # superuser-only workspace target picker.
                # Mirrors the orgs config; same gating + semantics.
                # Field naming follows the codebase convention:
                # ``name`` is the title field, ``esid_field`` is the
                # load-bearing ESID. The auto-tagger at app.py:1246
                # respects an explicit workspace_esid in data.
                #
                # required=True so the form rejects blank
                # workspace submission, which used to produce orphan
                # teams (invisible to scope resolver, freely
                # duplicatable). add_asset GET handler pre-fills the
                # default value (creator primary, falling back to first
                # visible workspace) so the typical case is one click.
                "name": "workspace_title",
                "label": "Workspace",
                "type": "select",
                "source": "de_asset_workspace",
                "source_field": "workspace_title",
                "store_esid": True,
                "esid_field": "workspace_esid",
                "required": True,
                "superuser_only": True,
                "help_text": "Target workspace for the new team. Required: defaults to your primary workspace.",
            },
            {
                # follow-up: required=True so teams cannot be
                # created/edited without at least one organization.
                # The 'team must have an org' invariant matches the
                # tier mental model (workspace → org → team) and
                # eliminates orphan teams, which were the bug class
                # that let same-titled teams duplicate (the slug
                # check at es_helpers.py:703-704 short-circuits for
                # orphan teams). Individual users get a workspace
                # only; orgs+teams are an org/agency-tier construct.
                "name": "organization_title",
                "label": "Organization",
                "type": "multi_select",
                "source": "de_asset_organization",
                "source_field": "organization_title",
                "store_esid": True,
                "esid_field": "organization_esid",
                "required": True,
                # non-agency workspace users have a fixed set of
                # orgs (their workspace membership grants org access).
                # form.html renders this as read-only chips for that
                # persona — they can't reassign teams across orgs they
                # don't belong to. Superusers and agency workspaces get
                # the full multi-select.
                "readonly_unless_superuser_or_agency": True,
                # resolve the org name in the teams listbox via
                # organization_esid → org doc (live lookup), exactly like the
                # edit form's prepare_for_edit does. Without a display_format
                # block, enrich_for_list_display skips this field and the
                # listbox falls back to the raw stored organization_title —
                # which wizard-provisioned teams never store (_upsert_team
                # writes organization_esid only), so the row rendered blank
                # while the edit form showed the correct name. Resolving by
                # ESID also keeps the listbox from going stale on org rename.
                "display_format": {
                    "field": "organization_title",
                },
            },
            {
                "name": "team_city",
                "label": "City",
                "type": "text",
                "required": False,
            },
            {
                "name": "team_country_text",
                "label": "Countries",
                "type": "multi_select",
                "source": "de_asset_countries",
                "source_field": "country_iso3_code",
                "store_esid": True,
                "esid_field": "team_country_esids",
                "required": False,
                "display_format": {
                    "field": "country_title",
                    "tooltip_fields": [
                        {"field": "country_iso3_code", "label": "ISO Code"},
                    ],
                },
            },
            {
                "name": "team_region",
                "label": "Region",
                "type": "multi_select",
                "source": "de_asset_regions",
                "source_field": "region_title",
                "store_esid": True,
                "esid_field": "team_region_esid",
                "required": False,
            },
            {
                "name": "team_language",
                "label": "Language",
                "type": "list",
                "help_text": "Enter one language per line",
            },
            # (a teammate): a team can have more than one URL. The
            # repeatable ``list`` widget renders the input + "Add" button and
            # persists the entries into the keyword-list field ``team_urls``.
            # ``primary_scalar_field`` keeps the legacy scalar ``team_url`` in
            # sync with the first URL so the ~6 importers that stamp
            # ``team_site_url = team.get("team_url")`` (FB / IG / YouTube)
            # keep emitting a single representative URL — no downstream change.
            # Mirrors the scalar→list approach used for Google Ads
            # customers (update_v19). The edit form seeds ``team_urls`` from the
            # scalar for any team predating the migration (see
            # FormService.prepare_for_edit).
            {
                "name": "team_urls",
                "label": "URLs",
                "type": "list",
                "primary_scalar_field": "team_url",
                "pattern": _PATTERN_URL,  # each entry must be an http(s) URL
                "help_text": "Enter one URL per line",
                "required": False,
            },
            # per-team time-range presets. Ticked options are the
            # ONLY presets this team's dashboard viewers see in the page-level
            # time-range dropdown (the viewer offers the UNION across all teams
            # they can see — see time_preset_service.resolve_time_presets).
            # Leaving every box unchecked means "no custom config" → the viewer
            # falls back to the full default catalog. Stored as a keyword list
            # of preset VALUES in ``team_time_presets``. "All time" and "Custom
            # range" are always offered and are intentionally not toggles here.
            {
                "name": "_time_presets_divider",
                "label": "Time Range Presets",
                "type": "section_divider",
            },
            {
                "name": _TIME_PRESETS_FIELD,
                "label": "Available time ranges",
                "type": "checkbox_group",
                "options": _time_preset_form_options(),
                "required": False,
                "help_text": (
                    "Choose which time-range presets this team's dashboard "
                    "viewers can pick. Leave all unchecked to show the full "
                    "default list. All time and Custom range are always available."
                ),
            },
            # --- Data Connectors section divider ---
            # connectors are managed on the dedicated Data
            # Connectors page; on the team form they are read-only. Each
            # ``data_source_checkbox`` carries ``readonly`` so form.html
            # skips the checkbox entirely, and each connector sub-field
            # carries ``readonly`` (render as chips, populated-only, edit
            # mode) + ``preserve_on_empty`` (so the now-unsubmitted input
            # is NOT marked for deletion on team save — it would otherwise
            # wipe the team's connectors on every edit).
            {
                "name": "_data_sources_divider",
                "label": "Data Connectors",
                "type": "section_divider",
                "readonly_data_connectors": True,
            },
            # Disciple Tools — a connector like the rest, so it
            # lives inside the read-only Data Connectors section (was a
            # plain editable URL field above the divider). Shown read-only
            # when set, skipped when blank.
            {
                "name": "team_disciple_tools_instance",
                "label": "Disciple Tools Instance",
                "type": "url",
                "required": False,
                "readonly": True,
                "preserve_on_empty": True,
            },
            # Bitly — Phase 3: connection-level connector (no
            # de_asset_* row). The team's ``bitly_connection_esid`` pointer is
            # deliberately NOT a form field — fields absent from this config
            # are untouched by team-form saves (same treatment as
            # connection_esid / google_ads_connection_esid), so it needs no
            # preserve_on_empty machinery. Only the read-only ds_ flag renders.
            {
                "name": "ds_bitly",
                "label": "Bitly",
                "type": "data_source_checkbox",
                "condition_affects": True,
                "readonly": True,
                # see ds_chatwoot note below.
                "preserve_on_empty": True,
            },
            # TikTok — connection-level connector like Bitly. The
            # team's ``team_tiktok_connection_esids`` list is deliberately NOT a
            # form field (absent fields are untouched by team-form saves); only
            # the read-only ds_ flag renders.
            {
                "name": "ds_tiktok",
                "label": "TikTok",
                "type": "data_source_checkbox",
                "condition_affects": True,
                "readonly": True,
                # see ds_chatwoot note below.
                "preserve_on_empty": True,
            },
            # Chatwoot
            {
                "name": "ds_chatwoot",
                "label": "Chatwoot",
                "type": "data_source_checkbox",
                "condition_affects": True,
                "readonly": True,
                # read-only ds_* checkboxes are not
                # submitted by the team form, so an edit would otherwise
                # delete the flag (process_form_data marks unsubmitted
                # optional fields for deletion). Preserve the stored value
                # like the connector esid/title fields below already do.
                "preserve_on_empty": True,
            },
            {
                "name": "chatwoot_account_title",
                "label": "Chatwoot Account",
                "type": "select",
                "source": "de_asset_chatwoot_accounts",
                "source_field": "chatwoot_account_title",
                "store_esid": True,
                "esid_field": "chatwoot_account_esid",
                "required": False,
                "readonly": True,
                "preserve_on_empty": True,
                "condition": {
                    "field": "ds_chatwoot",
                    "value": "true",
                },
            },
            # Echo
            {
                "name": "ds_echo",
                "label": "Echo",
                "type": "data_source_checkbox",
                "condition_affects": True,
                "readonly": True,
                # see ds_chatwoot note above.
                "preserve_on_empty": True,
            },
            {
                "name": "echo_tenant_title",
                "label": "Echo Tenant",
                "type": "select",
                "source": "de_asset_echo_tenants",
                "source_field": "echo_tenant_title",
                "store_esid": True,
                "esid_field": "echo_tenant_esid",
                "required": False,
                "readonly": True,
                "preserve_on_empty": True,
                "condition": {
                    "field": "ds_echo",
                    "value": "true",
                },
            },
            # Facebook
            {
                "name": "ds_facebook",
                "label": "Facebook",
                "type": "data_source_checkbox",
                "condition_affects": True,
                "readonly": True,
                # see ds_chatwoot note above.
                "preserve_on_empty": True,
            },
            {
                "name": "team_facebook_page_id",
                "label": "Facebook Pages",
                "type": "multi_select",
                "source": "de_asset_fb_pages",
                "source_field": "fb_page_id",
                "store_esid": True,
                "esid_field": "team_facebook_page_esids",
                "required": False,
                "readonly": True,
                "preserve_on_empty": True,
                "display_format": {
                    "field": "fb_page_slug",
                    "tooltip_fields": [
                        {"field": "fb_page_name", "label": "Page Name"},
                        {"field": "fb_account_title", "label": "Meta Business Portfolio"},
                    ],
                },
                "condition": {
                    "field": "ds_facebook",
                    "value": "true",
                },
            },
            # Google Analytics
            {
                "name": "ds_google_analytics",
                "label": "Google Analytics",
                "type": "data_source_checkbox",
                "condition_affects": True,
                "readonly": True,
                # see ds_chatwoot note above.
                "preserve_on_empty": True,
            },
            {
                "name": "team_ga4_properties",
                "label": "GA4 Properties",
                "type": "multi_select",
                "source": "de_asset_ga_properties",
                "source_field": "ga_property_id",
                "store_esid": True,
                "esid_field": "team_ga4_property_esids",
                "required": False,
                "readonly": True,
                "preserve_on_empty": True,
                "display_format": {
                    "field": "ga_property_title",
                    "tooltip_fields": [
                        {"field": "ga_property_display_name", "label": "Display Name"},
                        {"field": "ga_property_id", "label": "Property ID"},
                    ],
                },
                "condition": {
                    "field": "ds_google_analytics",
                    "value": "true",
                },
            },
            # Google Ads
            {
                "name": "ds_google_ads",
                "label": "Google Ads",
                "type": "data_source_checkbox",
                "condition_affects": True,
                "readonly": True,
                # see ds_chatwoot note above.
                "preserve_on_empty": True,
            },
            {
                "name": "team_google_ads_integration_status",
                "label": "Google Ads Integration",
                "type": "select",
                "options": ["Active", "Disabled"],
                "required": False,
                "condition_affects": True,
                "readonly": True,
                "preserve_on_empty": True,
                "condition": {
                    "field": "ds_google_ads",
                    "value": "true",
                },
            },
            {
                # Phase 1 — a team can hold MULTIPLE Google Ads
                # customers. Multi-select on the list field
                # team_google_ads_customer_esids (mirrors the GA4 Properties
                # entry above). The scalar google_ads_customer_esid is still
                # written by configure_google_ads_customer (= primary) during
                # the transition for legacy readers, but the team form now
                # reads/writes the list.
                "name": "team_google_ads_customers",
                "label": "Google Ads Customers",
                "type": "multi_select",
                "source": "de_asset_google_ads_customers",
                "source_field": "google_ads_customer_title",
                "store_esid": True,
                "esid_field": "team_google_ads_customer_esids",
                "required": False,
                "readonly": True,
                "preserve_on_empty": True,
                "condition": {
                    "field": "team_google_ads_integration_status",
                    "value": "Active",
                },
            },
            # Instagram
            {
                "name": "ds_instagram",
                "label": "Instagram",
                "type": "data_source_checkbox",
                "condition_affects": True,
                "readonly": True,
                # see ds_chatwoot note above.
                "preserve_on_empty": True,
            },
            {
                "name": "ig_account_id",
                "label": "Instagram Accounts",
                "type": "multi_select",
                "source": "de_asset_ig_accounts",
                "source_field": "ig_account_id",
                "store_esid": True,
                "esid_field": "team_instagram_profile_esids",
                "required": False,
                "readonly": True,
                "preserve_on_empty": True,
                "display_format": {
                    "field": "ig_account_title",
                    "tooltip_fields": [
                        {"field": "ig_account_id", "label": "Account ID"},
                    ],
                },
                "condition": {
                    "field": "ds_instagram",
                    "value": "true",
                },
            },
            # YouTube
            {
                "name": "ds_youtube",
                "label": "YouTube",
                "type": "data_source_checkbox",
                "condition_affects": True,
                "readonly": True,
                # see ds_chatwoot note above.
                "preserve_on_empty": True,
            },
            {
                "name": "team_youtube_channel_handle",
                "label": "YouTube Channels",
                "type": "multi_select",
                "source": "de_asset_youtube_channels",
                "source_field": "youtube_channel_handle",
                "store_esid": True,
                "esid_field": "team_youtube_channel_handle_esids",
                "required": False,
                "readonly": True,
                "preserve_on_empty": True,
                "display_format": {
                    "field": "youtube_channel_title",
                    "tooltip_fields": [
                        {"field": "youtube_channel_handle", "label": "Handle"},
                        {"field": "youtube_channel_id", "label": "Channel ID"},
                    ],
                },
                "condition": {
                    "field": "ds_youtube",
                    "value": "true",
                },
            },
            # the team's linked TikTok accounts, by name. The esid list
            # is the SAME team_tiktok_connection_esids the importer reads: each
            # de_asset_tiktok_accounts doc id is its de_connection esid, so the
            # chips resolve with no second list. Read-only + preserve_on_empty
            #: linking happens in Data Connectors, and a team-form save
            # must never touch the list.
            {
                "name": "team_tiktok_account",
                "label": "TikTok Accounts",
                "type": "multi_select",
                "source": "de_asset_tiktok_accounts",
                "source_field": "tiktok_account_title",
                "store_esid": True,
                "esid_field": "team_tiktok_connection_esids",
                "required": False,
                "readonly": True,
                "preserve_on_empty": True,
                "display_format": {
                    "field": "tiktok_account_title",
                    "tooltip_fields": [
                        {"field": "tiktok_display_name", "label": "Name"},
                        {"field": "tiktok_connection_status", "label": "Connection"},
                    ],
                },
                "condition": {
                    "field": "ds_tiktok",
                    "value": "true",
                },
            },
        ],
    },
    "workspaces": {
        # tenant-boundary entity. Gated to system_admin only via
        # ``_workspaces_visible_to`` in app.py — never appears in the
        # Asset Explorer tree for non-admins. Visible-set is further
        # narrowed by ``WorkspaceVisibilityService`` to (member_of) ∪
        # (created_by), so even a system_admin sees only their own
        # workspaces. Cross-tenant enumeration is forbidden by design.
        #
        # ``workspace_slug`` is intentionally NOT a form field — it's
        # derived server-side via WorkspaceSlugService.slugify on create,
        # immutable thereafter (renaming a slug would orphan child orgs'
        # aliases). The slug surfaces in the list view via
        # ``title_field``-driven rendering; the form only collects the
        # human-facing title and settings.
        "index": "de_asset_workspace",
        "title": "Workspaces",
        "title_field": "workspace_title",
        "description": "Tenant-boundary workspaces (admins only)",
        "category": "company",
        "fields": [
            {
                "name": "workspace_title",
                "label": "Workspace Title",
                "type": "text",
                "required": True,
            },
            {
                # required + no_empty_option so the select
                # renders exactly two states (false / true) rather than
                # three (None / false / true). is_agency is a binary
                # flag; a sentinel "-- Select --" option lets the user
                # save an empty string, which the create path then
                # coerces to false implicitly. Make the two valid
                # values the only choices.
                "name": "is_agency",
                "label": "Agency workspace (manages multiple client orgs)",
                "type": "select",
                "options": ["false", "true"],
                "required": True,
                "no_empty_option": True,
                "help_text": "Set to true if this workspace operates as an agency. Controls whether the Organizations menu is visible to non-admin members.",
            },
            {
                # superuser-only AI Assistant enablement (gate 1 of
                # the three-gate model). Follows the is_agency two-state
                # select shape but is deliberately NOT required — the field
                # is stripped from non-superuser forms/POSTs,
                # so a required flag would fail validation for the
                # non-superuser system_admins who can also edit workspaces.
                # Absent coerces to False in the add/edit handlers.
                "name": "ai_assistant_enabled",
                "label": "AI Assistant enabled (superuser only)",
                "type": "select",
                "options": ["false", "true"],
                "required": False,
                "no_empty_option": True,
                "superuser_only": True,
                "help_text": "Enable the Canvas AI Assistant for this workspace. Users still need the ai_assistant role to use it (spend-metered feature).",
            },
            {
                "name": "timezone",
                "label": "Timezone",
                "type": "text",
                "required": False,
                "help_text": "IANA timezone (e.g. America/New_York). Defaults to UTC if blank or invalid.",
            },
            {
                "name": "currency",
                "label": "Currency",
                "type": "text",
                "required": False,
                "help_text": "Three-letter ISO 4217 currency code (e.g. USD). Defaults to USD if blank or invalid.",
                "max_length": 3,
            },
            {
                "name": "fiscal_year_start_month",
                "label": "Fiscal Year Start Month (1-12)",
                "type": "text",
                "required": False,
                "help_text": "Integer 1 (January) through 12 (December). Defaults to 1 if blank or invalid.",
            },
        ],
    },
    "organizations": {
        "index": "de_asset_organization",
        "title": "Organizations",
        "title_field": "organization_title",
        "description": "Manage organizations",
        "category": "company",
        "fields": [
            {
                "name": "organization_title",
                "label": "Organization Title",
                "type": "text",
                "required": True,
            },
            {
                # superuser-only workspace target picker. Lets a
                # superuser seed cross-workspace orgs without first
                # rotating their own primary workspace. Hidden for
                # non-superusers; the auto-tagger at app.py:1224-1249
                # handles the common case (asset lands in creator's
                # primary workspace).
                #
                # Field naming follows the codebase convention:
                # ``name`` is the title field, ``esid_field`` derives
                # to workspace_esid. process_form_data writes BOTH:
                #   data["workspace_title"] = title (display)
                #   data["workspace_esid"]  = doc id (load-bearing)
                # The auto-tagger's ``"workspace_esid" not in data``
                # guard at app.py:1246 then naturally yields to the
                # explicit value.
                #
                # required=True so the form rejects blank
                # workspace submission. add_asset GET handler pre-fills
                # the default (creator primary, falling back to first
                # visible workspace).
                "name": "workspace_title",
                "label": "Workspace",
                "type": "select",
                "source": "de_asset_workspace",
                "source_field": "workspace_title",
                "store_esid": True,
                "esid_field": "workspace_esid",
                "required": True,
                "superuser_only": True,
                "help_text": "Target workspace for the new org. Required: defaults to your primary workspace.",
            },
            {
                "name": "organization_description",
                "label": "Description",
                "type": "text",
            },
            {
                "name": "organization_logo",
                "label": "Organization Logo",
                "type": "logo",
            },
            {
                "name": "auto_join_email_domains",
                "label": "Auto-join email domains",
                "type": "list",
                "help_text": (
                    "One domain per line (e.g. acme.com). Anyone signing up "
                    "with an email at one of these domains will be shown a "
                    "prompt to join this org. Free-mail domains (gmail.com, "
                    "outlook.com, etc.) are ignored even if listed."
                ),
            },
            # Publish to Global.Church — the three settings the publish
            # dialog (/publish-global-church/<org_esid>) needs. The dialog
            # writes the agreement acceptance and the last-release receipt
            # onto the same document itself; those fields are not edited
            # here and survive an edit through the {**existing, **data}
            # merge on save.
            {
                "name": "gc_section",
                "label": "Publish to Global.Church",
                "type": "section_divider",
            },
            {
                "name": "gc_gateway_url",
                "label": "Global.Church gateway URL",
                "type": "url",
                "pattern": r"https://.*",
                "superuser_only": True,
                "help_text": (
                    "https origin of the receiving gateway. The host must be on the GC_GATEWAY_HOSTS allowlist "
                    "(default api.global.church) or the publish is refused."
                ),
            },
            {
                "name": "gc_partner_api_key",
                "label": "Global.Church partner API key",
                "type": "password",
                "encrypted": True,
                "superuser_only": True,
                "help_text": "Issued by Global.Church to this organisation. Encrypted when saved.",
                "help_text_edit": "Encrypted value is shown. To replace it, enter the new key.",
            },
            {
                "name": "gc_org_uri",
                "label": "Global.Church directory URI",
                "type": "url",
                "pattern": r"https://data\.global\.church/org/[a-z0-9][a-z0-9-]*",
                "help_text": "The organisation's directory identifier, of the form https://data.global.church/org/<slug>.",
            },
        ],
    },
    # ============================================
    # FACEBOOK ASSETS
    # ============================================
    # "Facebook Account" is really Meta's Business Portfolio (the
    # entity that owns pages, IG profiles and ad accounts). The old label
    # collided with the app/login credential concept; renamed for clarity at
    # the team's request. Index + field names are unchanged (data-compatible) —
    # this is presentation only. The IG-accounts config already used this
    # wording, so the phrasing matches it.
    "facebook_accounts": {
        "index": "de_asset_fb_accounts",
        "title": "Meta Business Portfolios",
        "title_field": "fb_account_title",
        "description": "Manage Meta Business Portfolios",
        "category": "facebook",
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "fb_account_title",
                "label": "Portfolio Name",
                "type": "text",
                "required": True,
            },
            {
                "name": "fb_account_id",
                "label": "Portfolio ID",
                "type": "text",
                "required": True,
                "pattern": _PATTERN_NUMERIC_ID,
                "inputmode": "numeric",
            },
            {"name": "fb_account_description", "label": "Description", "type": "text"},
        ],
    },
    "facebook_apps": {
        "index": "de_asset_fb_apps",
        "title": "Facebook Apps",
        "title_field": "fb_app_title",
        "description": "Manage Facebook application credentials",
        "category": "facebook",
        # the oauth_redirect_uri computed-display is retired: the
        # Path A wizard uses the unified Meta app (WIZARD_META_APP_ID) and
        # nobody registers callbacks through Asset Explorer any more (FB Apps
        # are a superuser-only surface). The callback URL itself still lives
        # in the Meta App Dashboard: https://{fboauth_hostname}/auth_callback.
        "fields": [
            {
                "name": "fb_app_title",
                "label": "App Title",
                "type": "text",
                "required": True,
            },
            {
                "name": "fb_app_id",
                "label": "App ID",
                "type": "text",
                "required": True,
                "pattern": _PATTERN_NUMERIC_ID,
                "inputmode": "numeric",
            },
            {
                "name": "fb_app_secret",
                "label": "App Secret",
                "type": "password",
                "required": True,
                "encrypted": True,
                "help_text": "The App Secret will be encrypted when Create is clicked",
                "help_text_edit": "Encrypted value is shown. To update, enter an unencrypted value.",
            },
            {
                "name": "fb_app_token",
                "label": "App Token",
                "type": "password",
                "required": True,
                "encrypted": True,
                "help_text": "Enter <App ID>|<App Secret> (e.g. 123456|abcdef). The value will be encrypted when Create is clicked.",
                "help_text_edit": "Encrypted value is shown. To enter a new value, enter an unencrypted <App ID>|<App Secret> (e.g. 123456|abcdef). The value will be encrypted when this page is closed.",
            },
            {"name": "fb_app_description", "label": "Description", "type": "text"},
            {
                "name": "fb_account_title",
                "label": "Meta Business Portfolio",
                "type": "select",
                "source": "de_asset_fb_accounts",
                "source_field": "fb_account_title",
                "store_esid": True,
                "esid_field": "fb_account_esid",
                "required": False,
                "help_text": "Meta Business Portfolio this app belongs to",
            },
        ],
    },
    # "Meta Ad Accounts", not "Facebook Ad Accounts": an ad account
    # serves BOTH Facebook and Instagram ads (IG insights flow through the same
    # account), so the Facebook-only name was misleading. Meta's own UI calls
    # these "ad accounts" under a Business Portfolio. Facebook Pages and
    # Instagram Accounts keep their product brands — only the Portfolio and
    # ad-account layers are Meta-branded. Index/field names unchanged.
    "facebook_ad_accounts": {
        "index": "de_asset_fb_ad_accounts",
        "title": "Meta Ad Accounts",
        "title_field": "fb_ad_account_title",
        "description": "Manage Meta advertising accounts (Facebook and Instagram ads)",
        "category": "facebook",
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "fb_ad_account_title",
                "label": "Ad Account Title",
                "type": "text",
                "required": True,
            },
            {
                "name": "fb_ad_account_id",
                "label": "Ad Account ID",
                "type": "text",
                "required": True,
                # numeric, but accept the optional act_ prefix the
                # help text invites (stripped for storage downstream).
                "pattern": r"(act_)?\d+",
                "help_text": "Enter numeric ID (act_ prefix will be removed for storage)",
            },
            {
                "name": "fb_app_title",
                "label": "Facebook App",
                "type": "select",
                "source": "de_asset_fb_apps",
                "source_field": "fb_app_title",
                "store_esid": True,
                "esid_field": "fb_app_esid",
                "required": False,
            },
            {
                "name": "fb_app_user_title",
                "label": "Facebook App User",
                "type": "select",
                "source": "de_asset_fb_app_users",
                "source_field": "fb_app_user_title",
                "store_esid": True,
                "esid_field": "fb_app_user_esid",
                "required": False,
            },
            {
                "name": "fb_ad_account_payment_card_type",
                "label": "Payment Card Type",
                "type": "select",
                "options": ["Visa", "Mastercard", "Amex", "Discover", "Other"],
                "required": False,
            },
            {
                "name": "fb_ad_account_payment_card_last4",
                "label": "Card Last 4",
                "type": "text",
                "required": False,
                "max_length": 4,
                "pattern": r"\d{4}",
                "inputmode": "numeric",
                "help_text": "Last 4 digits of the credit card on file (numbers only)",
            },
        ],
    },
    "facebook_app_users": {
        "index": "de_asset_fb_app_users",
        "title": "Facebook App Users",
        "title_field": "fb_app_user_title",
        "description": "Manage Facebook OAuth user tokens",
        "category": "facebook",
        "auto_generate": {
            "fb_app_user_auth_url": {"length": 15, "unique": True},
            "fb_app_user_auth_state": {"length": 25, "unique": False},
        },
        "computed_display": {
            "oauth_url": {
                "label": "OAuth URL",
                "template": "https://{fboauth_hostname}/{fb_app_esid}/{fb_app_user_auth_url}",
            }
        },
        "fields": [
            {
                "name": "fb_app_user_title",
                "label": "User Title",
                "type": "text",
                "required": True,
            },
            {
                "name": "fb_app_title",
                "label": "Facebook App",
                "type": "select",
                "source": "de_asset_fb_apps",
                "source_field": "fb_app_title",
                "store_esid": True,
                "esid_field": "fb_app_esid",
                "required": False,
            },
            {
                "name": "fb_app_user_role",
                "label": "User Role",
                "type": "select",
                "options": ["Ads", "Pages"],
                "required": False,
            },
            {
                "name": "fb_app_user_token",
                "label": "User Access Token",
                "type": "password",
                "required": False,
                "encrypted": True,
                "help_text": "Leave blank and click Authorize via OAuth to generate a token automatically.",
                "help_text_edit": "Encrypted value shown. To update, click the Authorize via OAuth button.",
            },
        ],
    },
    "facebook_pages": {
        "index": "de_asset_fb_pages",
        "title": "Facebook Pages",
        "title_field": "fb_page_slug",
        "description": "Manage Facebook page references",
        "category": "facebook",
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "fb_page_slug",
                "label": "Page Slug (Username)",
                "type": "text",
                "required": True,
            },
            {
                "name": "fb_page_id",
                "label": "Page ID",
                "type": "text",
                "required": True,
                "pattern": _PATTERN_NUMERIC_ID,
                "inputmode": "numeric",
            },
            {
                "name": "fb_page_name",
                "label": "Page Name",
                "type": "text",
                "required": True,
            },
            {
                "name": "fb_account_title",
                "label": "Meta Business Portfolio",
                "type": "select",
                "source": "de_asset_fb_accounts",
                "source_field": "fb_account_title",
                "store_esid": True,
                "esid_field": "fb_account_esid",
                "required": False,
                "help_text": "Meta Business Portfolio that owns this page",
            },
        ],
    },
    # ============================================
    # ECHO ASSETS
    # ============================================
    "echo_tenants": {
        "index": "de_asset_echo_tenants",
        "title": "Echo Tenants",
        "title_field": "echo_tenant_title",
        "description": "Manage Echo messaging tenants",
        "category": "echo",
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "echo_tenant_title",
                "label": "Tenant Title",
                "type": "text",
                "required": True,
            },
            {
                "name": "echo_tenant_api_key",
                "label": "API Key",
                "type": "password",
                "required": True,
                "encrypted": True,
            },
            {
                "name": "echo_tenant_subdomain",
                "label": "Subdomain",
                "type": "text",
                "required": True,
            },
            {"name": "echo_tenant_description", "label": "Description", "type": "text"},
        ],
    },
    # ============================================
    # GOOGLE ANALYTICS ASSETS
    # ============================================
    "ga_accounts": {
        "index": "de_asset_ga_accounts",
        # distinguish the legacy service-account GA type from the GA4
        # OAuth type (ga4_accounts), which also carries the title "Google
        # Analytics Accounts". Both render under the same category, so an
        # identical title produced two indistinguishable rows in Asset Explorer.
        "title": "Google Analytics Accounts (Legacy)",
        "title_field": "ga_account_title",
        "description": "Manage legacy (service-account) Google Analytics connections",
        "category": "google_analytics",
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "ga_account_title",
                "label": "Account Title",
                "type": "text",
                "required": True,
            },
            {
                "name": "ga_account_id",
                "label": "Account ID",
                "type": "text",
                "required": True,
                "pattern": _PATTERN_NUMERIC_ID,
                "inputmode": "numeric",
            },
            {
                "name": "ga_account_resource_name",
                "label": "Resource Name",
                "type": "text",
                "required": False,
                "help_text": "Auto-generated as accounts/{account_id} if not provided",
            },
            {
                "name": "ga_account_description",
                "label": "Description",
                "type": "text",
                "required": False,
            },
        ],
    },
    "ga_properties": {
        "index": "de_asset_ga_properties",
        "title": "Google Analytics Properties",
        "title_field": "ga_property_title",
        "description": "Manage Google Analytics properties (team assignment is done via Teams)",
        "category": "google_analytics",
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "ga_property_title",
                "label": "Property Title",
                "type": "text",
                "required": True,
            },
            {
                "name": "ga_property_id",
                "label": "Property ID",
                "type": "text",
                "required": True,
                "pattern": _PATTERN_NUMERIC_ID,
                "inputmode": "numeric",
            },
            {
                "name": "ga_property_display_name",
                "label": "Display Name",
                "type": "text",
                "required": True,
            },
            {
                "name": "ga_property_resource_name",
                "label": "Resource Name",
                "type": "text",
                "required": False,
                "help_text": "Auto-generated as properties/{property_id} if not provided",
            },
            {
                "name": "ga_account_title",
                "label": "GA Account",
                "type": "select",
                "required": False,
                "source": "de_asset_ga_accounts",
                "source_field": "ga_account_title",
                "store_esid": True,
                "esid_field": "ga_account_esid",
            },
        ],
    },
    # ============================================
    # GOOGLE ADS ASSETS
    # ============================================
    "google_ads_customers": {
        "index": "de_asset_google_ads_customers",
        "title": "Google Ads Customers",
        "title_field": "google_ads_customer_title",
        "description": "Manage Google Ads customer accounts",
        "category": "google_ads",
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "google_ads_customer_title",
                "label": "Customer Title",
                "type": "text",
                "required": True,
                "help_text": "Display name for this Google Ads customer account",
            },
            {
                "name": "google_ads_customer_id",
                "label": "Customer ID",
                "type": "text",
                "required": True,
                "pattern": _PATTERN_GOOGLE_ADS_ID,  # 123-456-7890 or 1234567890
                "help_text": "Google Ads Customer ID (format: 123-456-7890)",
            },
        ],
    },
    # ============================================
    # INSTAGRAM ASSETS
    # ============================================
    "instagram_accounts": {
        "index": "de_asset_ig_accounts",
        "title": "Instagram Accounts",
        "title_field": "ig_account_title",
        "description": "Manage Instagram Business Account references",
        "category": "instagram",
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "ig_account_title",
                "label": "Account Title",
                "type": "text",
                "required": True,
            },
            {
                "name": "ig_account_id",
                "label": "Instagram Account ID",
                "type": "text",
                "required": True,
                "pattern": _PATTERN_NUMERIC_ID,
                "inputmode": "numeric",
                "help_text": "Numeric Instagram Business Account ID",
            },
            {
                "name": "fb_page_id",
                "label": "Facebook Page",
                "type": "select",
                "source": "de_asset_fb_pages",
                "source_field": "fb_page_id",
                "store_esid": True,
                "esid_field": "fb_page_esid",
                "required": True,
                "help_text": "Facebook Page this Instagram account is linked to",
            },
            {
                "name": "fb_account_title",
                "label": "Meta Business Portfolio",
                "type": "select",
                "source": "de_asset_fb_accounts",
                "source_field": "fb_account_title",
                "store_esid": True,
                "esid_field": "fb_account_esid",
                "required": False,
                "help_text": "Meta Business Portfolio this account belongs to",
            },
            {
                "name": "ig_account_notes",
                "label": "Notes",
                "type": "text",
                "required": False,
            },
        ],
    },
    # ============================================
    # TIKTOK ASSETS
    # ============================================
    # A read-only projection of the TikTok de_connection rows (see
    # services/tiktok_account_projection.py). ``code_managed`` makes the
    # Explorer refuse Add / Edit / Delete for this type: an account exists only
    # through TikTok's login on the Data Connectors page, and every field is
    # recomputed from de_connection, so an edit here would be overwritten and a
    # delete would orphan a live token.
    "tiktok_accounts": {
        "index": "de_asset_tiktok_accounts",
        "title": "TikTok Accounts",
        "title_field": "tiktok_account_title",
        "description": "TikTok accounts connected in Data Connectors (read-only)",
        "category": "tiktok",
        "no_add": True,
        "code_managed": True,
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {"name": "tiktok_account_title", "label": "Account", "type": "text", "required": False, "readonly": True},
            {"name": "tiktok_display_name", "label": "Display Name", "type": "text", "required": False, "readonly": True},
            {"name": "tiktok_open_id", "label": "TikTok Open ID", "type": "text", "required": False, "readonly": True},
            {"name": "tiktok_connection_status", "label": "Connection", "type": "text", "required": False, "readonly": True},
            {"name": "tiktok_authorized_by", "label": "Authorized By", "type": "text", "required": False, "readonly": True},
            {
                "name": "tiktok_last_snapshot_date",
                "label": "Last Snapshot",
                "type": "text",
                "required": False,
                "readonly": True,
            },
            {
                "name": "tiktok_refresh_expires_at",
                "label": "Reconnect Before",
                "type": "text",
                "required": False,
                "readonly": True,
                "help_text": "TikTok's authorization lasts a year; reconnect in Data Connectors before this date.",
            },
        ],
    },
    # ============================================
    # YOUTUBE ASSETS
    # ============================================
    "youtube_accounts": {
        "index": "de_asset_youtube_accounts",
        "title": "YouTube Accounts",
        "title_field": "youtube_account_title",
        "description": "Manage YouTube/Google account OAuth credentials",
        "category": "youtube",
        "auto_generate": {
            "youtube_account_auth_url": {"length": 15, "unique": True},
            "youtube_account_auth_state": {"length": 25, "unique": False},
        },
        "computed_display": {
            "oauth_url": {
                "label": "OAuth URL",
                "template": "https://{fboauth_hostname}/youtube/{youtube_account_auth_url}",
            }
        },
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "youtube_account_title",
                "label": "Account Title",
                "type": "text",
                "required": True,
                "help_text": "Label for this Google account (e.g. owner email or org name)",
            },
            {
                "name": "youtube_account_email",
                "label": "Google Account Email",
                "type": "text",
                "required": True,
                "pattern": _PATTERN_EMAIL,
                "inputmode": "email",
                "help_text": "Email of the Google account. Used as login_hint during OAuth.",
            },
            # the only field that distinguishes same-login account
            # docs. One Google login can front many Brand Account identities,
            # each needing its own doc (one doc = one token = one identity,
            # ), but Title and Email are IDENTICAL across them — so this
            # list showed indistinguishable rows. Stamped by the OAuth callback
            # from the YouTube API.
            #
            # readonly: it is API-derived truth about who authorized, never
            # operator input; hand-editing it would make the dedup and ownership
            # audits lie. preserve_on_empty is REQUIRED with readonly — a
            # readonly field is not submitted by the form, and without the flag
            # process_form_data marks unsubmitted optional fields for deletion,
            # so every unrelated edit would silently wipe the stamp (the
            # regression on the team ds_* checkboxes).
            {
                "name": "youtube_account_owned_channel_titles",
                "label": "Authorized For (channels owned)",
                "type": "text",
                "required": False,
                "readonly": True,
                "preserve_on_empty": True,
                "help_text": (
                    "Channels the authorized Google identity OWNS, recorded at OAuth time. "
                    "This is what distinguishes two accounts that share one Google login. "
                    "Note it is not necessarily the same as the channels LINKED to this "
                    "account — a linked channel the identity does not own cannot import."
                ),
            },
        ],
    },
    "youtube_channels": {
        "index": "de_asset_youtube_channels",
        "title": "YouTube Channels",
        "title_field": "youtube_channel_handle",
        "description": "Manage YouTube channel assets",
        "category": "youtube",
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "youtube_channel_title",
                "label": "Channel Name",
                "type": "text",
                "required": True,
                "help_text": "Display name of the YouTube channel",
            },
            {
                "name": "youtube_channel_id",
                "label": "Channel ID",
                "type": "text",
                "required": True,
                # channel IDs are [A-Za-z0-9_-] (canonically UC…);
                # lenient char-class rather than forcing the UC prefix.
                "pattern": r"[A-Za-z0-9_-]+",
                "help_text": "YouTube Channel ID (format: UCxxxxxxxxxxxxxx)",
            },
            {
                "name": "youtube_channel_handle",
                "label": "Channel Handle",
                "type": "text",
                "required": False,
                "pattern": _PATTERN_YT_HANDLE,  # @handle or bare handle
                "help_text": "YouTube @handle (e.g., @username)",
            },
            {
                "name": "youtube_account_title",
                "label": "YouTube Account",
                "type": "select",
                "source": "de_asset_youtube_accounts",
                "source_field": "youtube_account_title",
                "store_esid": True,
                "esid_field": "youtube_account_esid",
                "required": False,
                "help_text": "The Google account that owns this channel",
            },
        ],
    },
    # ============================================
    # GA4 OAUTH ACCOUNTS
    # ============================================
    # Mirrors youtube_accounts: the GA4 OAuth-grant carrier. One doc per
    # Google account a user authorizes; the fb_oauth_server /ga4/* callback
    # writes the encrypted refresh token onto ``ga4_account_token``. GA4
    # properties (de_asset_ga_properties) remain the child asset, enumerated
    # live from the granted account and linked to a team by the wizard.
    "ga4_accounts": {
        "index": "de_asset_ga4_accounts",
        "title": "Google Analytics Accounts",
        "title_field": "ga4_account_title",
        "description": "Manage Google Analytics (GA4) account OAuth credentials",
        "category": "google_analytics",
        "auto_generate": {
            "ga4_account_auth_url": {"length": 15, "unique": True},
            "ga4_account_auth_state": {"length": 25, "unique": False},
        },
        "computed_display": {
            "oauth_url": {
                "label": "OAuth URL",
                "template": "https://{fboauth_hostname}/ga4/{ga4_account_auth_url}",
            }
        },
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "ga4_account_title",
                "label": "Account Title",
                "type": "text",
                "required": True,
                "help_text": "Label for this Google account (e.g. owner email or org name)",
            },
            {
                "name": "ga4_account_email",
                "label": "Google Account Email",
                "type": "text",
                "required": True,
                "pattern": _PATTERN_EMAIL,
                "inputmode": "email",
                "help_text": "Email of the Google account. Used as login_hint during OAuth.",
            },
        ],
    },
    # ============================================
    # CHATWOOT ASSETS
    # ============================================
    "chatwoot_accounts": {
        "index": "de_asset_chatwoot_accounts",
        "title": "Chatwoot Accounts",
        "title_field": "chatwoot_account_title",
        "description": "Manage Chatwoot customer support account connections",
        "category": "chatwoot",
        "fields": [
            _SUPERUSER_WORKSPACE_FIELD,
            {
                "name": "chatwoot_account_title",
                "label": "Account Title",
                "type": "text",
                "required": True,
                "help_text": "Display name for this Chatwoot account",
            },
            {
                # vetted server-side (lib.chatwoot_base_url) on add
                # and edit: public HTTPS only, and moving an existing account to
                # a different server requires re-entering the API token.
                "name": "chatwoot_base_url",
                "label": "Chatwoot Server URL",
                "type": "url",
                "required": False,
                "help_text": (
                    "Leave blank for Chatwoot Cloud (app.chatwoot.com). For a self-hosted "
                    "Chatwoot, enter your server's address, e.g. https://chat.example.org"
                ),
            },
            {
                "name": "chatwoot_account_id",
                "label": "Account ID",
                "type": "text",
                "required": True,
                "pattern": _PATTERN_NUMERIC_ID,
                "inputmode": "numeric",
                "help_text": "Numeric Chatwoot account ID (found in your Chatwoot URL)",
            },
            {
                "name": "chatwoot_api_token",
                "label": "API Access Token",
                "type": "password",
                "required": True,
                "encrypted": True,
                "help_text": "API Access Token from Chatwoot Profile Settings → Access Token",
                "help_text_edit": "Encrypted value is shown. To update, enter a new unencrypted token.",
            },
            {
                "name": "chatwoot_account_description",
                "label": "Description",
                "type": "text",
                "required": False,
            },
        ],
    },
}

# Asset categories for dashboard organization
ASSET_CATEGORIES = {
    "company": {
        "title": "Organization Assets",
        # workspaces ordered first — they sit above orgs in the
        # tenant hierarchy and only system_admins see them; the runtime
        # gate in ``_workspaces_visible_to`` keeps the entry hidden for
        # non-admins so this ordering doesn't surface as a discoverable
        # tier to other roles.
        "assets": ["workspaces", "teams", "organizations"],
    },
    "chatwoot": {"title": "Chatwoot Assets", "assets": ["chatwoot_accounts"]},
    "echo": {"title": "Echo Assets", "assets": ["echo_tenants"]},
    "facebook": {
        "title": "Facebook Assets",
        "assets": [
            "facebook_accounts",
            "facebook_apps",
            "facebook_ad_accounts",
            "facebook_app_users",
            "facebook_pages",
        ],
    },
    "google_analytics": {
        "title": "Google Analytics Assets",
        "assets": ["ga_accounts", "ga_properties", "ga4_accounts"],
    },
    "google_ads": {"title": "Google Ads Assets", "assets": ["google_ads_customers"]},
    "instagram": {"title": "Instagram Assets", "assets": ["instagram_accounts"]},
    # read-only (tiktok_accounts is code_managed).
    "tiktok": {"title": "TikTok Assets", "assets": ["tiktok_accounts"]},
    "youtube": {
        "title": "YouTube Assets",
        "assets": ["youtube_accounts", "youtube_channels"],
    },
}
