"""
Permissions Index Service

Idempotent bootstrap for the new authz indices. Phase 1 ships these
indices alongside the existing ``de_role_permissions`` UI-flag system;
nothing reads from them during this phase. The resolver, seeders, and
backfill land in subsequent phases.

Indices created (most prefixed ``de_authz_`` to avoid collision with the
existing ``de_role_permissions`` UI-flag index; ``de_asset_workspace``
is the tenant-boundary asset doc added / F4 and is
bootstrapped here rather than left to dynamic mapping so its
``workspace_slug`` stays a keyword):

* ``de_authz_permission``           — atomic permission definitions
* ``de_authz_role``                 — role definitions (bundles of permissions)
* ``de_authz_role_permission``      — role-to-permission link
* ``de_authz_team_membership``      — user belongs to team
* ``de_authz_org_membership``       — user belongs to org
* ``de_authz_workspace_membership`` — user belongs to workspace (F8)
* ``de_authz_user_team_role``       — scoped role assignment (team)
* ``de_authz_user_org_role``        — scoped role assignment (org)
* ``de_authz_user_workspace_role``  — scoped role assignment (workspace, F8)
* ``de_asset_workspace``            — tenant-boundary entity (F4)
* ``de_asset_workspace_menu`` — workspace-scoped Dashboard Builder menus

Doc-ID conventions (replace UNIQUE constraints; PUT semantics make
duplicate inserts no-ops):

* ``de_authz_permission``      id = ``<key>``                          (e.g. ``report.view``)
* ``de_authz_role``            id = random ESID                         (role names are not guaranteed unique forever)
* ``de_authz_role_permission`` id = ``<role_esid>:<permission_key>``
* ``de_authz_team_membership``      id = ``<username>:<team_esid>``
* ``de_authz_org_membership``       id = ``<username>:<org_esid>``
* ``de_authz_workspace_membership`` id = ``<username>:<workspace_esid>``
* ``de_authz_user_team_role``       id = ``<username>:<team_esid>:<role_esid>``
* ``de_authz_user_org_role``        id = ``<username>:<org_esid>:<role_esid>``
* ``de_authz_user_workspace_role``  id = ``<username>:<workspace_esid>:<role_esid>``
* ``de_asset_workspace``            id = ``<workspace_slug>``            (slug is the natural key; deterministic from title via WorkspaceSlugService.slugify)
"""

import logging
import sys

sys.path.insert(0, "/app/python")
from lib.es_helpers import (
    create_index_if_not_exists,
    dual_term,
    dual_terms,
    get_elasticsearch_client,
    put_regular_index_template,
)

PERMISSION_INDEX = "de_authz_permission"
ROLE_INDEX = "de_authz_role"
ROLE_PERMISSION_INDEX = "de_authz_role_permission"
TEAM_MEMBERSHIP_INDEX = "de_authz_team_membership"
ORG_MEMBERSHIP_INDEX = "de_authz_org_membership"
USER_TEAM_ROLE_INDEX = "de_authz_user_team_role"
USER_ORG_ROLE_INDEX = "de_authz_user_org_role"
# Phase 2: per-user backfill diff record. Phase 5 cleanup will
# drop this index along with the backfill script itself.
BACKFILL_DIFF_INDEX = "de_authz_backfill_diff"
# / F1: persistent data-access grants. One doc per (user, scope).
# RoleFilterService consults this index for superusers; the deterministic
# doc id makes grant create/revoke idempotent under the _safe_create
# pattern from onboarding_service.
DATA_GRANT_INDEX = "de_authz_data_grant"
# / F4: workspace tenant-boundary entity. New top-level asset
# index above org. Bootstrapped here rather than via dynamic mapping so
# workspace_slug stays a keyword (not auto-detected text) and the
# uniqueness check in WorkspaceSlugService (F6) can rely on exact-term
# lookups.
WORKSPACE_INDEX = "de_asset_workspace"
# / F5: org doc gains workspace_esid (link to parent workspace)
# and organization_slug (per-workspace uniqueness). Both MUST be keyword
# so the F5 backfill verification (workspace_esid existence query) and
# the rescoped slug-uniqueness check (organization_slug per workspace_esid)
# work on exact-term lookups. The index existed pre-F5 via dynamic
# mapping; put_mapping additively adds the two new keyword fields without
# rebuilding the existing docs.
ORGANIZATION_INDEX = "de_asset_organization"
# / F8: workspace tier membership + role-assignment indices.
# Mirrors the (org_membership, user_org_role) pair from same
# deterministic doc id convention so duplicate writes are no-ops via
# op_type=create + 409-as-success. WorkspaceMembershipService is the
# canonical read surface; RoleFilterService consumes it for the
# workspace-priority data filter.
WORKSPACE_MEMBERSHIP_INDEX = "de_authz_workspace_membership"
USER_WORKSPACE_ROLE_INDEX = "de_authz_user_workspace_role"
# workspace-scoped Dashboard Builder menus. One doc per
# (workspace_esid, menu_title); ordering controlled by menu_position.
# WorkspaceMenuService bootstraps a workspace's nine default menus on
# first read so existing workspaces don't need a migration step.
WORKSPACE_MENU_INDEX = "de_asset_workspace_menu"
# per-workspace field-catalog selections (the DEAM field picker).
# One doc per (workspace_esid, catalog source), deterministic id
# "<workspace_esid>__<source>"; sparse overrides only — a workspace with
# no doc gets the catalog's default (requested: true) field set.
FIELD_CATALOG_INDEX = "de_asset_field_catalog"

ALL_INDICES: tuple[str, ...] = (
    PERMISSION_INDEX,
    ROLE_INDEX,
    ROLE_PERMISSION_INDEX,
    TEAM_MEMBERSHIP_INDEX,
    ORG_MEMBERSHIP_INDEX,
    USER_TEAM_ROLE_INDEX,
    USER_ORG_ROLE_INDEX,
    BACKFILL_DIFF_INDEX,
    DATA_GRANT_INDEX,
    WORKSPACE_INDEX,
    ORGANIZATION_INDEX,
    WORKSPACE_MEMBERSHIP_INDEX,
    USER_WORKSPACE_ROLE_INDEX,
    WORKSPACE_MENU_INDEX,
    FIELD_CATALOG_INDEX,
)

PERMISSION_MAPPING = {
    "properties": {
        "key": {"type": "keyword"},
        "resource": {"type": "keyword"},
        "action": {"type": "keyword"},
        "description": {"type": "text"},
    }
}

ROLE_MAPPING = {
    "properties": {
        "name": {"type": "keyword"},
        "description": {"type": "text"},
        # 'team', 'org', or 'either'
        "applies_to": {"type": "keyword"},
        "is_system": {"type": "boolean"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    }
}

ROLE_PERMISSION_MAPPING = {
    "properties": {
        "role_esid": {"type": "keyword"},
        "permission_key": {"type": "keyword"},
    }
}

TEAM_MEMBERSHIP_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "team_esid": {"type": "keyword"},
        "is_primary": {"type": "boolean"},
        "created_at": {"type": "date"},
    }
}

ORG_MEMBERSHIP_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "org_esid": {"type": "keyword"},
        "is_primary": {"type": "boolean"},
        "created_at": {"type": "date"},
    }
}

USER_TEAM_ROLE_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "team_esid": {"type": "keyword"},
        "role_esid": {"type": "keyword"},
        "created_at": {"type": "date"},
    }
}

USER_ORG_ROLE_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "org_esid": {"type": "keyword"},
        "role_esid": {"type": "keyword"},
        "created_at": {"type": "date"},
    }
}

# Phase 2: per-user backfill diff record. Doc id is the
# username (PUT semantics — latest backfill run is authoritative).
# ``flagged_roles`` surfaces custom-named roles that need manual
# review before any cutover. Phase 5 cleanup drops this index.
BACKFILL_DIFF_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "is_super_admin": {"type": "boolean"},
        "old_es_roles": {"type": "keyword"},
        "skipped_reason": {"type": "keyword"},
        "team_memberships_written": {
            "type": "nested",
            "properties": {
                "team_esid": {"type": "keyword"},
                "team_title": {"type": "keyword"},
                "is_primary": {"type": "boolean"},
            },
        },
        "org_memberships_written": {
            "type": "nested",
            "properties": {
                "org_esid": {"type": "keyword"},
                "org_title": {"type": "keyword"},
                "is_primary": {"type": "boolean"},
            },
        },
        "team_role_assignments_written": {
            "type": "nested",
            "properties": {
                "team_esid": {"type": "keyword"},
                "role_esid": {"type": "keyword"},
            },
        },
        "org_role_assignments_written": {
            "type": "nested",
            "properties": {
                "org_esid": {"type": "keyword"},
                "role_esid": {"type": "keyword"},
            },
        },
        "flagged_roles": {
            "type": "nested",
            "properties": {
                "role_name": {"type": "keyword"},
                "reason": {"type": "text"},
            },
        },
        "legacy_roles_kept": {"type": "keyword"},
        "old_effective_summary": {"type": "text"},
        "new_effective_summary": {"type": "text"},
        "delta_summary": {"type": "text"},
        "ran_in_dry_run_mode": {"type": "boolean"},
        "@timestamp": {"type": "date"},
    }
}

# / F1: persistent data-access grants. Doc id is deterministic
# ``<username>:<scope_type>:<scope_esid>`` so DataGrantService.create can
# use op_type=create + 409-as-success for idempotency.
DATA_GRANT_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "scope_type": {"type": "keyword"},  # "org" | "team"
        "scope_esid": {"type": "keyword"},
        "granted_by": {"type": "keyword"},
        "granted_at": {"type": "date"},
        # Nullable: durable grants (agency provisioning) leave
        # this null. Support / debug grants populate it with a future
        # timestamp. RoleFilterService treats grants with expires_at <
        # now as if they don't exist.
        "expires_at": {"type": "date"},
        "reason": {"type": "text"},
    }
}

# / F4: workspace tenant-boundary doc.
#
# workspace_slug is the load-bearing field — globally unique across all
# workspaces, used as the URL segment and as the prefix of every data-
# index alias once F5/ land. It MUST be keyword so the slug-
# uniqueness probe in WorkspaceSlugService (F6) can run exact-term
# lookups; auto-detected text would silently miss against the analyser.
#
# is_agency replaces de_asset_organization.is_multi_workspace (deprecated
# by F5 step 9). Settings (timezone, currency, fiscal_year_start_month)
# move here from org-level — they were always tenant-level conceptually,
# the org-level home was a holding pattern wizard work.
WORKSPACE_MAPPING = {
    "properties": {
        # workspace_title needs both text (full-text search via
        # the asset explorer search box) and keyword (exact-match terms /
        # sort / aggregations from the role-filter, scope-resolution,
        # and migration-tooling paths). Without the keyword subfield,
        # ``GET de_asset_workspace/_search { sort: [workspace_title.keyword] }``
        # fails. ``ignore_above: 256`` keeps the keyword index bounded
        # against a pathological long title without losing the searchable
        # text view.
        "workspace_title": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
        },
        "workspace_slug": {"type": "keyword"},
        "is_agency": {"type": "boolean"},
        # per-workspace AI Assistant enablement — gate 1 of the
        # three-gate model (workspace enabled AND mc_dashboard_builder AND
        # the user's ai_assistant role). Superuser-only editable via the
        # Asset Explorer workspace form; absent reads as False (fail-closed).
        "ai_assistant_enabled": {"type": "boolean"},
        "is_multi_workspace_user_supported": {"type": "boolean"},
        "timezone": {"type": "keyword"},
        "currency": {"type": "keyword"},
        "fiscal_year_start_month": {"type": "integer"},
        "created_at": {"type": "date"},
        "created_by": {"type": "keyword"},
    }
}

# / F5: additive mapping update for de_asset_organization.
#
# Elasticsearch's put_mapping is additive — declaring only the two new
# keyword fields adds them to the existing index without touching the
# other dynamic-mapped fields (organization_title, organization_logo,
# is_multi_workspace, etc.). The existing docs continue to read; F5's
# backfill script populates the two new fields on every existing doc.
#
# Deliberately ONLY declaring the new fields. Listing the full org schema
# here would couple F5 to every legacy field and conflict with whatever
# dynamic-mapped types already exist on prod (potentially incompatible
# with explicit redeclaration). The backfill verifies coverage by
# querying `must_not: exists: workspace_esid` after --apply.
ORGANIZATION_MAPPING = {
    "properties": {
        "workspace_esid": {"type": "keyword"},
        "organization_slug": {"type": "keyword"},
    }
}

# / F8: workspace membership + workspace role mappings mirror
# the org-tier shapes for consistency. is_primary identifies the default
# landing workspace for users who belong to N>1 (Phase-1 default is one
# workspace per user; the multi-workspace switcher UI flips on later).
WORKSPACE_MEMBERSHIP_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "workspace_esid": {"type": "keyword"},
        "is_primary": {"type": "boolean"},
        "created_at": {"type": "date"},
    }
}

USER_WORKSPACE_ROLE_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "workspace_esid": {"type": "keyword"},
        "role_esid": {"type": "keyword"},
        # is_primary migrated onto the role doc from the (being
        # retired) membership doc. Identifies the user's default landing
        # workspace; WorkspaceMembershipService.get_primary_workspace sorts
        # on it. Explicit boolean mapping so the sort is reliable.
        "is_primary": {"type": "boolean"},
        "created_at": {"type": "date"},
    }
}

# workspace-scoped Dashboard Builder menus. menu_title is the
# natural-key the existing ``de_asset_echarts_dashboards.menu`` field
# already references, so dashboards remain readable even if the
# WorkspaceMenuService row is somehow absent. menu_position is a
# 0-based integer; ordering ties broken by menu_title for determinism.
WORKSPACE_MENU_MAPPING = {
    "properties": {
        "menu_esid": {"type": "keyword"},
        "workspace_esid": {"type": "keyword"},
        "menu_title": {"type": "keyword"},
        "menu_position": {"type": "integer"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    }
}

# per-workspace field-picker selections. ``fields`` holds the sparse
# per-field override objects ({field_id: {enabled, changed_at, changed_by}});
# it is deliberately unindexed (enabled: false) — lookups are by _id /
# workspace_esid+source only, and field_ids contain dots that would otherwise
# explode into nested object mappings.
FIELD_CATALOG_MAPPING = {
    "properties": {
        "workspace_esid": {"type": "keyword"},
        "source": {"type": "keyword"},
        "fields": {"type": "object", "enabled": False},
        # per-team source toggles share this index: team docs
        # (id "team__<team_esid>__<source>") carry team_esid/scope, and both
        # doc shapes carry a source_enabled override object (never queried).
        "team_esid": {"type": "keyword"},
        "scope": {"type": "keyword"},
        "source_enabled": {"type": "object", "enabled": False},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
        "updated_by": {"type": "keyword"},
    }
}

# new-fields-only additive mapping for de_asset_workspace,
# following the ORGANIZATION_MAPPING precedent. The full
# WORKSPACE_MAPPING put_mapping in ensure_indices_exist FAILS atomically
# on clusters whose index predates the explicit mapping (staging/prod
# dynamic-mapped fiscal_year_start_month as keyword; the declaration says
# integer — a type change put_mapping cannot make), so a new field buried
# in the full declaration never lands. Pushing it alone always succeeds.
WORKSPACE_ADDITIVE_MAPPING = {
    "properties": {
        "ai_assistant_enabled": {"type": "boolean"},
    }
}

INDEX_MAPPINGS: tuple[tuple[str, dict], ...] = (
    (PERMISSION_INDEX, PERMISSION_MAPPING),
    (ROLE_INDEX, ROLE_MAPPING),
    (ROLE_PERMISSION_INDEX, ROLE_PERMISSION_MAPPING),
    (TEAM_MEMBERSHIP_INDEX, TEAM_MEMBERSHIP_MAPPING),
    (ORG_MEMBERSHIP_INDEX, ORG_MEMBERSHIP_MAPPING),
    (USER_TEAM_ROLE_INDEX, USER_TEAM_ROLE_MAPPING),
    (USER_ORG_ROLE_INDEX, USER_ORG_ROLE_MAPPING),
    (BACKFILL_DIFF_INDEX, BACKFILL_DIFF_MAPPING),
    (DATA_GRANT_INDEX, DATA_GRANT_MAPPING),
    (WORKSPACE_INDEX, WORKSPACE_MAPPING),
    (ORGANIZATION_INDEX, ORGANIZATION_MAPPING),
    (WORKSPACE_MEMBERSHIP_INDEX, WORKSPACE_MEMBERSHIP_MAPPING),
    (USER_WORKSPACE_ROLE_INDEX, USER_WORKSPACE_ROLE_MAPPING),
    (WORKSPACE_MENU_INDEX, WORKSPACE_MENU_MAPPING),
    (FIELD_CATALOG_INDEX, FIELD_CATALOG_MAPPING),
)


# explicit text+keyword mappings for the title field on every
# ``de_asset_*`` index. Before this, the title fields were dynamic-mapped
# at first-write time — some clusters got the .keyword subfield, others
# didn't (depending on which dynamic_templates were active). On staging
# 2026-05-19 the gap surfaced as a failed
# ``GET de_asset_teams/_search { sort: [team_title.keyword] }`` despite
# six months of stable production code that queries `team_title.keyword`.
#
# Each entry pairs the asset index with its title field name. The
# bootstrap applies the same shape (text + keyword/ignore_above=256) to
# every entry. ES additive-mapping semantics mean an existing index
# gains the .keyword subfield without a reindex; existing docs get the
# subfield populated lazily on next update.
#
# The constant is sourced from ASSET_CONFIGS to keep the audit
# discoverable: a future asset added there gets a title.keyword for
# free, but tests pin the cross-reference so a missed wiring would
# surface in CI rather than at scope-resolver-runtime.
#
# Out of scope per the ticket: other text fields (description, notes,
# slug-text variants). Slug fields are already mapped as keyword
# elsewhere; non-title text fields don't have
# active exact-match consumers.
# Per-asset title fields. The first 15 entries pair each de_asset_*
# index with the title field declared in asset_configs.ASSET_CONFIGS.
# Two indices use a non-``*_title`` natural key in asset_configs but
# also carry a separate display-title field that the codebase queries
# with .keyword:
#
# * de_asset_fb_pages: title_field is fb_page_slug (the URL slug,
#   already keyword-shaped); the fb_page_name field is the display
#   name. Neither one is the typical ``*_title`` form; we map both
#   to keep parity with the consumer patterns (search by slug for
#   uniqueness checks; sort by name for the asset list).
# * de_asset_youtube_channels: title_field is youtube_channel_handle
#   (the @handle); youtube_channel_title is the display name and is
#   the field that QUICKSTART.md documents as the canonical
#   ``.keyword`` consumer for groupings.
ASSET_TITLE_MAPPINGS: tuple[tuple[str, str], ...] = (
    ("de_asset_teams", "team_title"),
    ("de_asset_organization", "organization_title"),
    ("de_asset_workspace", "workspace_title"),
    ("de_asset_fb_accounts", "fb_account_title"),
    ("de_asset_fb_apps", "fb_app_title"),
    ("de_asset_fb_ad_accounts", "fb_ad_account_title"),
    ("de_asset_fb_app_users", "fb_app_user_title"),
    ("de_asset_fb_pages", "fb_page_name"),
    ("de_asset_echo_tenants", "echo_tenant_title"),
    ("de_asset_ga_accounts", "ga_account_title"),
    ("de_asset_ga_properties", "ga_property_title"),
    ("de_asset_ga4_accounts", "ga4_account_title"),
    ("de_asset_google_ads_customers", "google_ads_customer_title"),
    ("de_asset_ig_accounts", "ig_account_title"),
    ("de_asset_youtube_accounts", "youtube_account_title"),
    ("de_asset_youtube_channels", "youtube_channel_title"),
    ("de_asset_chatwoot_accounts", "chatwoot_account_title"),
    # read-only TikTok account projection.
    ("de_asset_tiktok_accounts", "tiktok_account_title"),
)


# explicit ``keyword`` mapping for ``workspace_esid`` on the two
# asset indices ES inferred as ``text`` (analyzed) at first write —
# ``de_asset_teams`` and ``de_asset_echarts_dashboards``. Every other index
# that carries ``workspace_esid`` (ORGANIZATION_INDEX, the authz indices,
# de_asset_workspace_menu, the checklist indices) got an explicit keyword
# mapping; these two slipped through because no code path declared the field
# before the first write.
#
# A ``text`` field tokenizes a hyphenated value (``test-account-workspace`` ->
# ``["test", "account", "workspace"]``), so a bare ``term:{workspace_esid: ...}``
# silently returns zero hits. BugB worked around that by querying
# ``workspace_esid.keyword`` everywhere; fixes the mapping so the bare
# field works again and reverts the workaround.
#
# Like the title mappings, these two indices are NOT in ALL_INDICES —
# they're created on first write rather than by an explicit bootstrap — so the
# mapping is applied via ``apply_workspace_esid_keyword_mappings`` (additive
# put_mapping on an existing index) and pre-flighted on first write. The
# additive put_mapping CANNOT flip an existing ``text`` field to ``keyword``;
# the one-shot ``tools/reindex_workspace_esid_keyword.py`` does that for
# already-text-mapped clusters. This declaration guarantees a fresh cluster (and
# the post-reindex index) is created correct.
WORKSPACE_ESID_KEYWORD_INDICES: tuple[str, ...] = (
    "de_asset_teams",
    "de_asset_echarts_dashboards",
)

WORKSPACE_ESID_KEYWORD_MAPPING = {
    "properties": {
        "workspace_esid": {"type": "keyword"},
    }
}


def _title_mapping_body(field_name: str) -> dict:
    """Build the additive put_mapping body for one title field.

    Shape matches the documented convention at WORKSPACE_MAPPING for
    workspace_title: text (full-text search) + keyword subfield
    (exact-match terms / sort / aggregations) with ignore_above=256 so
    a pathological long title doesn't bloat the keyword index.
    """
    return {
        "properties": {
            field_name: {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
            }
        }
    }


# Elasticsearch refuses an in-place field type change, so a startup
# put_mapping that re-declares a field the live index already maps differently
# fails on EVERY boot and logged a WARNING nothing at boot can act on. Seen on
# staging 2026-09-04: de_asset_teams.team_title and
# de_asset_organization.organization_title live as bare ``keyword`` (the
# declaration says text+keyword), de_asset_workspace.fiscal_year_start_month
# was dynamic-mapped ``keyword`` (declared integer). The readers already
# handle both title shapes (``es_helpers._sortable_title_field``),
# so the bootstrap leaves those fields alone, still pushes every other
# (additive) field, and notes the legacy type ONCE per process at INFO with
# the migration that would actually change it. A put_mapping that fails for
# any other reason still logs at WARNING — that is a real problem.
_LEGACY_TYPE_NOTED: set[tuple[str, str]] = set()

_REINDEX_HINT = (
    "reindex into a fresh index created from the declaration "
    "(tools.reindex_workspace_esid_keyword is the one-shot template)"
)


def _live_property_types(es, index_name: str) -> dict[str, str]:
    """``{field: type}`` for the live top-level properties of ``index_name``.

    ``{}`` when the mapping cannot be read (index missing, connection error,
    a test double without ``get_mapping``) — callers then fall back to the
    plain put_mapping, which is the earlier behaviour.
    """
    try:
        live = dict(es.indices.get_mapping(index=index_name))
        props = next(iter(live.values())).get("mappings", {}).get("properties", {}) or {}
    except Exception:  # noqa: BLE001 — best-effort probe; the caller's put_mapping still runs
        return {}
    return {field: spec["type"] for field, spec in props.items() if isinstance(spec, dict) and spec.get("type")}


def _legacy_type_conflicts(declared_properties: dict, live_types: dict[str, str]) -> dict[str, tuple[str, str]]:
    """``{field: (declared_type, live_type)}`` for every declared field whose
    live type differs. Fields absent from the live mapping are additive (not
    a conflict); declared fields without a ``type`` (object containers) are
    left to ES."""
    out: dict[str, tuple[str, str]] = {}
    for field, spec in declared_properties.items():
        want = spec.get("type") if isinstance(spec, dict) else None
        got = live_types.get(field)
        if want and got and want != got:
            out[field] = (want, got)
    return out


def _note_legacy_type(index_name: str, field: str, declared: str, live: str) -> None:
    """INFO, once per (index, field) per process — not WARNING: nothing at
    boot can act on it, and repeating it every start trains operators to
    ignore the WARNING lines that DO matter (template sync,
     install failures)."""
    key = (index_name, field)
    if key in _LEGACY_TYPE_NOTED:
        return
    _LEGACY_TYPE_NOTED.add(key)
    logging.info(
        "%s.%s is mapped %s on this cluster; the declaration says %s. "
        "ES cannot change a field type in place, so the startup put_mapping leaves it alone. "
        "To change it: %s",
        index_name,
        field,
        live,
        declared,
        _REINDEX_HINT,
    )


def _additive_mapping_body(es, index_name: str, mapping: dict) -> dict | None:
    """``mapping`` minus the fields the live index already maps with another
    type (each noted once at INFO), so the additive fields still land instead
    of the whole put failing atomically. ``None`` when nothing is left."""
    declared = mapping.get("properties", {})
    conflicts = _legacy_type_conflicts(declared, _live_property_types(es, index_name))
    if not conflicts:
        return mapping
    for field, (want, got) in sorted(conflicts.items()):
        _note_legacy_type(index_name, field, want, got)
    properties = {f: spec for f, spec in declared.items() if f not in conflicts}
    if not properties:
        return None
    return {**mapping, "properties": properties}


class PermissionsIndexService:
    """Idempotent bootstrap for the authz indices."""

    @staticmethod
    def ensure_indices_exist() -> None:
        """Create every authz index if it does not exist.

        Safe to call repeatedly. ``put_regular_index_template`` is
        invoked first so the index is created as a regular index even
        if a lower-priority data-stream template would otherwise match.
        ``put_mapping`` is then called to apply mapping additions to
        already-existing indices on subsequent runs.

        also pushes title-field text+keyword mappings to the
        ``de_asset_*`` indices. Those indices are normally auto-created
        on first write by ``AssetService.create_with_error`` (no
        explicit bootstrap), but the title fields need the explicit
        keyword subfield for downstream sort / terms / aggregation
        consumers. ``put_mapping`` is additive and idempotent — a re-run
        on a cluster that already has the keyword subfield is a no-op.
        Indices that don't yet exist (no asset of that type ever
        created) are skipped silently — the dynamic mapping at first
        write will lose the keyword subfield, but the next bootstrap
        run after the first write will fix it.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("PermissionsIndexService: could not connect to ES; " "authz indices not bootstrapped")
            return

        for index_name, mapping in INDEX_MAPPINGS:
            put_regular_index_template(es, index_name, mapping, priority=500)
            create_index_if_not_exists(es, index_name, mapping)
            # leave out fields the live index maps with another
            # type (noted once at INFO) so the additive ones still land.
            body = _additive_mapping_body(es, index_name, mapping)
            if body is None:
                continue
            try:
                es.indices.put_mapping(index=index_name, body=body)
            except Exception as e:
                logging.warning(
                    "PermissionsIndexService: put_mapping update for %s: %s",
                    index_name,
                    e,
                )

        # the full WORKSPACE_MAPPING put_mapping above fails
        # atomically on clusters with the legacy fiscal_year_start_month
        # keyword mapping — push the new field alone so it always lands.
        try:
            es.indices.put_mapping(index=WORKSPACE_INDEX, body=WORKSPACE_ADDITIVE_MAPPING)
        except Exception as e:
            logging.warning(
                "PermissionsIndexService:  additive put_mapping for %s: %s",
                WORKSPACE_INDEX,
                e,
            )

        PermissionsIndexService.apply_asset_title_mappings(es)
        PermissionsIndexService.apply_workspace_esid_keyword_mappings(es)

    @staticmethod
    def ensure_asset_title_mapping(es, index_name: str) -> None:
        """pre-flight the title-field mapping for ``index_name``
        before a first write. No-op for non-asset indices and for asset
        indices that already exist (put_mapping is idempotent but the
        exists-check skips the network call for the hot path).

        Called from ``AssetService.create_with_error`` right before the
        ``es.index`` write so the very first asset of a new type never
        races through dynamic-mapping without the keyword subfield.
        """
        title_field = next(
            (f for (idx, f) in ASSET_TITLE_MAPPINGS if idx == index_name),
            None,
        )
        if title_field is None:
            return  # not a de_asset_* index we recognize
        try:
            if es.indices.exists(index=index_name):
                return  # already exists — the bootstrap path has handled it
        except Exception:
            return  # exists-check failed; let the create proceed
        # The index does NOT exist yet. Create it now with the explicit
        # mapping so ES doesn't auto-create with dynamic mapping that
        # may omit the keyword subfield. Use create_index_if_not_exists
        # because a parallel request might create it between the exists
        # check and our create.
        #
        # for the two indices whose ``workspace_esid`` must be
        # keyword (de_asset_teams, de_asset_echarts_dashboards), fold that
        # field into the same create body so a fresh index is born correct
        # in one shot — avoids a second create racing the title pre-flight.
        body = _title_mapping_body(title_field)
        if index_name in WORKSPACE_ESID_KEYWORD_INDICES:
            body["properties"]["workspace_esid"] = {"type": "keyword"}
        try:
            create_index_if_not_exists(es, index_name, body)
        except Exception as e:
            logging.warning(
                "ensure_asset_title_mapping for %s failed: %s",
                index_name,
                e,
            )

    @staticmethod
    def apply_asset_title_mappings(es) -> dict[str, str]:
        """push text+keyword mapping to every de_asset_* title
        field. Returns ``{index: outcome}`` where outcome is one of:

        * ``"applied"`` — put_mapping succeeded (already-present-keyword
          subfield is itself a no-op put — ES does not error)
        * ``"index_missing"`` — index doesn't exist yet; deferred
        * ``"legacy_type: <type>"`` — the live field is not
          ``text`` (bare keyword on staging); put_mapping cannot change it,
          readers handle both shapes, noted once at INFO and skipped
        * ``"error: <text>"`` — write failed for another reason

        Best-effort: per-index failures are logged at WARNING and do
        not abort the loop. Exported so the migration tooling and the
        release-plan test can exercise the same code path without
        invoking the full authz-bootstrap.
        """
        outcomes: dict[str, str] = {}
        for index_name, title_field in ASSET_TITLE_MAPPINGS:
            try:
                exists = es.indices.exists(index=index_name)
            except Exception as e:
                outcomes[index_name] = f"error: exists-check failed: {e}"
                logging.warning("%s exists-check failed: %s", index_name, e)
                continue
            if not exists:
                outcomes[index_name] = "index_missing"
                continue
            # a title already mapped bare ``keyword`` (staging)
            # cannot become text+keyword by put_mapping; readers handle both
            # shapes, so note it once and move on.
            live_type = _live_property_types(es, index_name).get(title_field)
            if live_type and live_type != "text":
                _note_legacy_type(index_name, title_field, "text", live_type)
                outcomes[index_name] = f"legacy_type: {live_type}"
                continue
            try:
                es.indices.put_mapping(
                    index=index_name,
                    body=_title_mapping_body(title_field),
                )
                outcomes[index_name] = "applied"
            except Exception as e:
                outcomes[index_name] = f"error: {e}"
                logging.warning(
                    "put_mapping for %s.%s failed: %s",
                    index_name,
                    title_field,
                    e,
                )
        return outcomes

    @staticmethod
    def apply_workspace_esid_keyword_mappings(es) -> dict[str, str]:
        """push the explicit ``workspace_esid: keyword`` mapping to
        ``de_asset_teams`` and ``de_asset_echarts_dashboards``. Returns
        ``{index: outcome}`` where outcome is one of:

        * ``"applied"`` — put_mapping succeeded (a no-op put against an
          already-keyword field does not error)
        * ``"index_missing"`` — index doesn't exist yet; deferred
        * ``"legacy_type: text"`` — the field is still ``text``
          (pre-reindex). ES would reject the type change, so the put is
          skipped and the state noted once at INFO.
          ``tools/reindex_workspace_esid_keyword.py`` is what flips an
          existing text mapping; this method only guarantees a fresh (or
          post-reindex) index is keyword-correct and is a no-op thereafter.
        * ``"error: <text>"`` — write failed for another reason (logged at
          WARNING — a real problem).

        Best-effort and idempotent — mirrors ``apply_asset_title_mappings``.
        Exported so the reindex tool and the release-plan test can exercise
        the same code path without the full authz bootstrap.
        """
        outcomes: dict[str, str] = {}
        for index_name in WORKSPACE_ESID_KEYWORD_INDICES:
            try:
                exists = es.indices.exists(index=index_name)
            except Exception as e:
                outcomes[index_name] = f"error: exists-check failed: {e}"
                logging.warning("%s exists-check failed: %s", index_name, e)
                continue
            if not exists:
                outcomes[index_name] = "index_missing"
                continue
            # same treatment — a still-text workspace_esid is a
            # known state with its own reindex tool, not a boot-time WARNING.
            live_type = _live_property_types(es, index_name).get("workspace_esid")
            if live_type and live_type != "keyword":
                _note_legacy_type(index_name, "workspace_esid", "keyword", live_type)
                outcomes[index_name] = f"legacy_type: {live_type}"
                continue
            try:
                es.indices.put_mapping(
                    index=index_name,
                    body=WORKSPACE_ESID_KEYWORD_MAPPING,
                )
                outcomes[index_name] = "applied"
            except Exception as e:
                outcomes[index_name] = f"error: {e}"
                logging.warning(
                    "put_mapping for %s.workspace_esid failed "
                    "(expected on a still-text-mapped cluster — run "
                    "tools.reindex_workspace_esid_keyword): %s",
                    index_name,
                    e,
                )
        return outcomes


# cascade-delete helper. Called from BOTH api_delete_user
# (legacy single-delete route) AND UserManagementService.bulk_action
# (the UI path Users-page deletes actually take). Centralising here
# means the seven-index list lives next to the constants, and both
# callers can't drift out of sync.
AUTHZ_USERNAME_KEYED_INDICES: tuple[str, ...] = (
    WORKSPACE_MEMBERSHIP_INDEX,
    ORG_MEMBERSHIP_INDEX,
    TEAM_MEMBERSHIP_INDEX,
    USER_WORKSPACE_ROLE_INDEX,
    USER_ORG_ROLE_INDEX,
    USER_TEAM_ROLE_INDEX,
    DATA_GRANT_INDEX,
    # follow-up (2026-05-21): the MFA index keys docs by
    # username (doc id == username). Without it in the purge list, a
    # user delete via User Management (or the now-removed verify-email
    # back-out) left the MFA record orphaned. Next signup with the
    # same username would match the stale record and see
    # ``has_any_factor=True``, surfacing "You've already set up
    # two-factor" on the wizard MFA-setup step. The cascade now
    # delete_by_query against the MFA index too. a teammate hit this on
    # repeated test-account signups on staging.
    "de_asset_user_mfa",
    # (2026-05-25): the dashboard checklist indices key docs
    # by ``username:workspace_esid``. Without them in the purge list, a
    # user delete via User Management leaves the prior session's skip
    # + dismiss rows orphaned. A re-created user with the same
    # username inherits them; if enough skips landed pre-delete that
    # the re-created user's checklist computes 100% complete via the
    # skip-completion semantic, get_state returns
    # is_complete=True and the entire checklist surface hides — same
    # blank-checklist symptom a teammate hit on staging 2026-05-25 during
    # verification. Same shape as (MFA) and
    # (wizard_session) cascade gaps; same fix pattern.
    "de_dashboard_checklist_dismiss",
    "de_dashboard_checklist_skip",
)


def cascade_purge_authz_for_user(es, username: str) -> dict:
    """Fan out a delete_by_query for ``username`` against each
    username-keyed authz index. Best-effort: per-index failures are
    logged at WARNING and do not abort the cascade. Returns
    ``{index: deleted_count}`` for each index that ran.

    ``index_not_found`` / 404 is treated as a no-op (workspace indices
    may not exist in test/local envs that haven't booted the F4 seeder).
    ``refresh=True`` so the Users page re-renders consistently on the
    immediate redirect.
    """
    counts: dict[str, int] = {}
    for index in AUTHZ_USERNAME_KEYED_INDICES:
        try:
            resp = es.delete_by_query(
                index=index,
                body={"query": {"term": {"username": username}}},
                refresh=True,
                conflicts="proceed",
            )
            counts[index] = int(resp.get("deleted", 0))
        except Exception as e:
            err_text = str(e).lower()
            if "index_not_found" in err_text or "not_found" in err_text or "404" in err_text:
                counts[index] = 0
                continue
            logging.warning(
                "cascade delete_by_query failed for %s on %s: %s",
                username,
                index,
                e,
            )
            counts[index] = 0

    # also purge the user's wizard session doc. Sibling of the
    # workspace-delete cascade — without this, deleting and re-creating
    # a user with the same username resumes the wizard from stale state
    # (same failure mode as MFA cascade). Doc id is
    # ``<username>:onboarding`` per WizardSessionService._doc_id; 404 is
    # fine — the user may never have opened the wizard.
    try:
        es.delete(
            index="de_wizard_session",
            id=f"{username}:onboarding",
            refresh=True,
        )
        counts["de_wizard_session"] = 1
    except Exception as e:
        err_text = str(e).lower()
        if "not_found" in err_text or "404" in err_text:
            counts["de_wizard_session"] = 0
        else:
            logging.warning(
                "wizard-session purge failed for %s: %s",
                username,
                e,
            )
            counts["de_wizard_session"] = 0

    return counts


# scope-keyed authz cascade for the team/org ASSET delete path.
# The third axis of the existing cascade family — purges by
# username purges by workspace; this purges by the deleted
# scope itself. Without it, role/membership/grant docs naming a deleted
# team or org live forever, and their stale titles flow into the
# UserScope resolver's grant + membership legs (badge counts,
# get_allowed_teams_* consumers).
_SCOPE_CASCADE_LEGS = {
    "team": (TEAM_MEMBERSHIP_INDEX, USER_TEAM_ROLE_INDEX, "team_esid"),
    "org": (ORG_MEMBERSHIP_INDEX, USER_ORG_ROLE_INDEX, "org_esid"),
}


def cascade_purge_authz_for_scope(es, scope_type: str, scope_esid: str) -> dict:
    """Purge membership, role, and grant docs naming a deleted team/org.

    ``scope_type`` is ``"team"`` or ``"org"`` (matching the
    de_authz_data_grant scope_type vocabulary). Best-effort, mirroring
    cascade_purge_authz_for_user: per-index failures are logged at
    WARNING and do not abort; ``index_not_found`` is a 0-count no-op.
    Returns ``{index: deleted_count}``.
    """
    counts: dict[str, int] = {}
    legs = _SCOPE_CASCADE_LEGS.get(scope_type)
    if not legs or not scope_esid:
        return counts
    membership_index, role_index, esid_field = legs

    # Authz indices are F4-seeded with keyword mappings — bare term is
    # correct here (same as the cascades); dual_term is
    # only needed on the mapping-drifted de_asset_* indices.
    queries = (
        (membership_index, {"term": {esid_field: scope_esid}}),
        (role_index, {"term": {esid_field: scope_esid}}),
        (
            DATA_GRANT_INDEX,
            {
                "bool": {
                    "filter": [
                        {"term": {"scope_type": scope_type}},
                        {"term": {"scope_esid": scope_esid}},
                    ]
                }
            },
        ),
    )
    for index, query in queries:
        try:
            resp = es.delete_by_query(
                index=index,
                body={"query": query},
                refresh=True,
                conflicts="proceed",
            )
            counts[index] = int(resp.get("deleted", 0))
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                counts[index] = 0
                continue
            logging.warning(
                "scope cascade delete_by_query failed for %s %s on %s: %s",
                scope_type,
                scope_esid,
                index,
                e,
            )
            counts[index] = 0
    return counts


# cascade purge for the empty-workspace delete path on User
# Management. Scope deliberately narrow — the membership-empty gate is
# the safety boundary. The child teams' data (`{team_esid}__*` indices,
# legacy `{slug}_{source}` indices, raw files) is not deleted in the
# request: records a purge request per child team (7-day grace)
# and jobs/team_data_purge.py deletes the data afterwards only if the team
# doc is still gone.
#
# Match shape:
#   * de_asset_workspace            — doc id == workspace_esid
#   * de_asset_organization         — term:workspace_esid
#   * de_asset_teams                — term:workspace_esid
#   * de_authz_workspace_membership — term:workspace_esid
#   * de_authz_user_workspace_role  — term:workspace_esid
#   * de_authz_user_org_role        — terms:org_esid (child orgs)
#   * de_authz_user_team_role       — terms:team_esid (child teams)
#   * de_authz_org_membership       — terms:org_esid
#   * de_authz_team_membership      — terms:team_esid
#   * de_authz_data_grant           — workspace-scope grant OR child org/team scope


def count_workspace_memberships(es, workspace_esid: str) -> int:
    """Return the live member count for ``workspace_esid``.

    Used by both the empty-check endpoint AND as the in-route safety
    gate on the delete endpoint — a single source of truth so the UI
    enable-state and the server reject can't drift.

     (P3a): "member" == "holds a workspace role". Counts DISTINCT
    usernames with a ``de_authz_user_workspace_role`` doc for the
    workspace (cardinality agg) rather than membership rows, so a user
    with multiple role docs still counts once — preserving the accurate
    member count the delete-gate UI shows.

    Returns ``0`` when the index does not exist (pre-F4 envs).
    Returns ``-1`` on unexpected ES failure so the caller can fail-closed
    rather than silently treat the workspace as empty.
    """
    if not workspace_esid:
        return -1
    try:
        resp = es.search(
            index=USER_WORKSPACE_ROLE_INDEX,
            body={
                "size": 0,
                "query": {"term": {"workspace_esid": workspace_esid}},
                "aggs": {"members": {"cardinality": {"field": "username"}}},
            },
        )
        return int(resp.get("aggregations", {}).get("members", {}).get("value", 0))
    except Exception as e:
        err = str(e).lower()
        if "index_not_found" in err or "404" in err:
            return 0
        logging.warning(
            "workspace member count failed for %s: %s",
            workspace_esid,
            e,
        )
        return -1


def count_org_members(es, workspace_esid: str) -> int:
    """Distinct member count for an ORGANIZATION.

    "Organization" == the top-level paying entity the code calls a
    ``workspace`` (decision 2026-06-09). A member can hold a role at ANY of
    the three tiers under that org, so a correct count must UNION distinct
    usernames across:

      * ``de_authz_user_workspace_role`` — term on ``workspace_esid``;
      * ``de_authz_user_org_role``       — terms on the org's CHILD org esids;
      * ``de_authz_user_team_role``      — terms on the org's CHILD team esids.

    A user holding roles at more than one tier still counts ONCE (set union).
    Used by the 5-user hard cap (block-new-only): the grant seam compares this
    count against ``ORG_USER_CAP`` before writing a new role doc.

    Returns the distinct member count, ``0`` when the role indices don't exist
    yet (pre-F4 envs), or ``-1`` on unexpected ES failure so the caller can
    fail-CLOSED (refuse the add rather than silently allow an over-cap grant).
    """
    if not workspace_esid:
        return -1

    # Enumerate child org + team esids of this workspace. Mirrors
    # cascade_purge_empty_workspace's enumeration. reindexed
    # ``de_asset_teams.workspace_esid`` to keyword, so the bare field now
    # works (the ``.keyword`` workaround has been reverted);
    # ORGANIZATION_INDEX has always been keyword-mapped.
    child_org_esids: list[str] = []
    child_team_esids: list[str] = []
    for index, field, sink in (
        (ORGANIZATION_INDEX, "workspace_esid", child_org_esids),
        ("de_asset_teams", "workspace_esid", child_team_esids),
    ):
        try:
            resp = es.search(
                index=index,
                body={"query": {"term": {field: workspace_esid}}, "_source": False, "size": 10000},
            )
            for hit in resp.get("hits", {}).get("hits", []):
                sink.append(hit["_id"])
        except Exception as e:  # noqa: BLE001
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                continue
            logging.warning("child enumeration failed on %s for %s: %s", index, workspace_esid, e)
            return -1

    # Collect distinct usernames across the three role tiers. Each tier query
    # pulls the matching role docs' usernames into one set so a user with roles
    # at multiple tiers counts once.
    members: set[str] = set()
    tier_queries = [
        (USER_WORKSPACE_ROLE_INDEX, {"term": {"workspace_esid": workspace_esid}}),
    ]
    if child_org_esids:
        tier_queries.append((USER_ORG_ROLE_INDEX, {"terms": {"org_esid": child_org_esids}}))
    if child_team_esids:
        tier_queries.append((USER_TEAM_ROLE_INDEX, {"terms": {"team_esid": child_team_esids}}))

    for index, query in tier_queries:
        try:
            resp = es.search(
                index=index,
                body={"query": query, "_source": ["username"], "size": 10000},
            )
            for hit in resp.get("hits", {}).get("hits", []):
                uname = (hit.get("_source") or {}).get("username")
                if uname:
                    members.add(uname)
        except Exception as e:  # noqa: BLE001
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                continue
            logging.warning("member count failed on %s for %s: %s", index, workspace_esid, e)
            return -1

    return len(members)


# item 8 — hard cap on members per organization (top entity). The
# grant seam refuses a NEW member once an org is at this many distinct members.
# Block-new-only: existing over-cap orgs keep their members (this gate only
# fires when adding would push a not-yet-over org past the cap). Superuser and
# fully-comped orgs are exempt (checked at the call site).
ORG_USER_CAP = 5


def cascade_purge_empty_workspace(es, workspace_esid: str, requested_by: str = "unknown") -> dict:
    """Purge an empty workspace + its orgs/teams + authz rows.

    Caller MUST verify ``count_workspace_memberships(...) == 0`` before
    invoking — this function does NOT re-check (separation of concerns
    keeps the count + delete each idempotent and individually testable).

    the deleted child teams' data is not touched here; each gets a
    purge request (7-day grace) written as ``requested_by``, counted under
    ``de_team_purge_requests``.

    Returns ``{index: deleted_count}`` for every index touched.
    Best-effort: index_not_found is a 0-count no-op; other per-index
    failures are logged and recorded as 0 but do not abort the cascade.
    """
    counts: dict[str, int] = {}

    # Step 1: enumerate child org + team esids BEFORE deleting the asset
    # docs, so the role/membership purge in step 3 has the id set to
    # match on. Also enumerate former-member usernames so step 5 can
    # purge their wizard session docs — without this, a
    # re-signup with the same username resumes from stale form state.
    child_org_esids: list[str] = []
    child_team_esids: list[str] = []
    # each child team's title, read before its doc is purged, so
    # its purge request can name the legacy ``{slug}_{source}`` indices.
    child_team_titles: dict[str, str] = {}
    # ``de_asset_teams.workspace_esid`` was reindexed from ``text``
    # to ``keyword``, so the bare ``term:{workspace_esid: ...}`` now matches
    # hyphenated slugs and the ``.keyword`` workaround is gone.
    # ORGANIZATION_INDEX has always been keyword-mapped.
    for index, field, sink in (
        (ORGANIZATION_INDEX, "workspace_esid", child_org_esids),
        ("de_asset_teams", "workspace_esid", child_team_esids),
    ):
        try:
            resp = es.search(
                index=index,
                body={
                    "query": {"term": {field: workspace_esid}},
                    "_source": ["team_title"] if index == "de_asset_teams" else False,
                    "size": 10000,
                },
            )
            for hit in resp.get("hits", {}).get("hits", []):
                sink.append(hit["_id"])
                if index == "de_asset_teams":
                    child_team_titles[hit["_id"]] = (hit.get("_source") or {}).get("team_title") or ""
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                continue
            logging.warning(
                "child enumeration failed on %s for workspace %s: %s",
                index,
                workspace_esid,
                e,
            )

    # enumerate former-member usernames BEFORE step 2 deletes
    # the workspace_membership rows. Empty result is normal — the empty-
    # workspace gate means there should be no live members anyway, but a
    # workspace can legitimately have a stale membership for a since-
    # deleted user (the user-delete cascade purges the membership row).
    # We still want to clear any wizard sessions that survived under the
    # workspace's former members.
    former_member_usernames: list[str] = []
    try:
        resp = es.search(
            index=WORKSPACE_MEMBERSHIP_INDEX,
            body={
                "query": {"term": {"workspace_esid": workspace_esid}},
                "_source": ["username"],
                "size": 10000,
            },
        )
        for hit in resp.get("hits", {}).get("hits", []):
            uname = (hit.get("_source") or {}).get("username")
            if uname:
                former_member_usernames.append(uname)
    except Exception as e:
        err = str(e).lower()
        if "index_not_found" not in err and "404" not in err:
            logging.warning(
                "member-username enumeration failed for workspace %s: %s",
                workspace_esid,
                e,
            )

    # Step 2: workspace-scoped purges (single workspace_esid term).
    # ``de_asset_teams`` and ``de_asset_echarts_dashboards`` were
    # reindexed to a keyword ``workspace_esid``, so every index here uses the
    # bare field now (the per-index ``.keyword`` selection is gone).
    # (BugB) — Extended: added de_asset_echarts_dashboards +
    # de_asset_workspace_menu so a deleted workspace doesn't leave
    # orphan dashboards (the original cascade deliberately
    # skipped these; the assumption that an empty workspace had no
    # such docs turned out to be wrong because a user can hand-build
    # dashboards in the Dashboard Builder that survive the membership
    # purge).
    for index, field in (
        (WORKSPACE_INDEX, "workspace_esid"),
        (ORGANIZATION_INDEX, "workspace_esid"),
        ("de_asset_teams", "workspace_esid"),
        (WORKSPACE_MEMBERSHIP_INDEX, "workspace_esid"),
        (USER_WORKSPACE_ROLE_INDEX, "workspace_esid"),
        ("de_asset_echarts_dashboards", "workspace_esid"),
        ("de_asset_workspace_menu", "workspace_esid"),
    ):
        try:
            if index == WORKSPACE_INDEX:
                # Single-doc delete by id — workspace_esid IS the doc id.
                try:
                    es.delete(index=index, id=workspace_esid, refresh=True)
                    counts[index] = 1
                except Exception as e:
                    err = str(e).lower()
                    if "not_found" in err or "404" in err:
                        counts[index] = 0
                    else:
                        raise
            else:
                resp = es.delete_by_query(
                    index=index,
                    body={"query": {"term": {field: workspace_esid}}},
                    refresh=True,
                    conflicts="proceed",
                )
                counts[index] = int(resp.get("deleted", 0))
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                counts[index] = 0
                continue
            logging.warning(
                "workspace purge failed for %s on %s: %s",
                workspace_esid,
                index,
                e,
            )
            counts[index] = 0

    # the child teams' docs are gone (step 2); their data indices
    # and raw files are not. Record a purge request per team — the 7-day
    # grace and the still-deleted re-check live in jobs/team_data_purge.py.
    if child_team_esids and counts.get("de_asset_teams", 0) > 0:
        try:
            from asset_management.web.services.team_purge_service import record_team_purge_tombstones

            counts["de_team_purge_requests"] = record_team_purge_tombstones(
                es,
                [
                    {"esid": esid, "team_title": child_team_titles.get(esid, ""), "workspace_esid": workspace_esid}
                    for esid in child_team_esids
                ],
                requested_by=requested_by,
                reason="workspace_delete",
            )
        except Exception as e:  # noqa: BLE001 — best-effort, like every leg of this cascade
            logging.warning("purge requests failed for workspace %s: %s", workspace_esid, e)
            counts["de_team_purge_requests"] = 0

    # Step 3: child-keyed purges (terms over the enumerated org/team
    # esids). Empty enumeration => no-op for that index.
    for index, field, ids in (
        (ORG_MEMBERSHIP_INDEX, "org_esid", child_org_esids),
        (USER_ORG_ROLE_INDEX, "org_esid", child_org_esids),
        (TEAM_MEMBERSHIP_INDEX, "team_esid", child_team_esids),
        (USER_TEAM_ROLE_INDEX, "team_esid", child_team_esids),
    ):
        if not ids:
            counts[index] = 0
            continue
        try:
            resp = es.delete_by_query(
                index=index,
                body={"query": {"terms": {field: ids}}},
                refresh=True,
                conflicts="proceed",
            )
            counts[index] = int(resp.get("deleted", 0))
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                counts[index] = 0
                continue
            logging.warning(
                "child-id purge failed for workspace %s on %s: %s",
                workspace_esid,
                index,
                e,
            )
            counts[index] = 0

    # Step 3.5: purge wizard-written platform-asset rows whose
    # team_esid points at one of this workspace's now-deleted teams.
    # earlier, _bridge_meta_assets / _bridge_instagram_assets
    # / _bridge_fb_pages wrote into these indices on wizard finish
    # but the workspace-delete cascade only purged the authz layer, leaving
    # orphan rows pointing at deleted team_esids (8 such ig_account
    # orphans on staging 2026-05-24).
    #
    # Scope deliberately narrow:
    #   • Only indices the WIZARD writes to. Cluster-wide legacy assets
    #     (de_asset_fb_apps, de_asset_fb_app_users, GA, YouTube, Google
    #     Ads, Echo, Chatwoot) are out of scope — they don't carry
    #     team_esid and would no-op anyway; widening the list just grows
    #     the surface for future bugs.
    #   • Team-keyed for the FB indices. de_asset_fb_pages conditionally
    #     also carries workspace_esid (when set at wizard time) but
    #     de_asset_fb_ad_accounts does NOT, so team_esid remains their
    #     join key.
    # • de_asset_ig_accounts — point 4 made new wizard IG docs
    #     OWNERLESS (no team_esid; the field was a write-only stamp that
    # actively misled ownership resolution, and
    #     for the ad-account precedent). The purge therefore keys the IG
    # index on team_esid (legacy cohort, earlier docs) OR
    #     workspace_esid (new cohort). A team belongs to exactly one
    #     workspace, so every IG doc stamped with this workspace belongs to
    #     one of its (now-deleted) teams. Team legs stay FIRST in the
    #     should — bare-keyword-behavior fakes and diagnostics read the
    #     first bare leg.
    # • Mapping shape — team_esid drifts between bare keyword
    #     (prod) and text+.keyword across environments. The old .keyword-only
    # terms query silently matched NOTHING
    #     on bare-keyword prod, so this purge deleted zero and left orphaned
    #     wizard assets behind. dual_terms matches on either shape.
    if child_team_esids:
        ig_query = {
            "bool": {
                "should": [
                    *dual_terms("team_esid", child_team_esids)["bool"]["should"],
                    *dual_term("workspace_esid", workspace_esid)["bool"]["should"],
                ],
                "minimum_should_match": 1,
            }
        }
        for index, query in (
            ("de_asset_ig_accounts", ig_query),
            ("de_asset_fb_ad_accounts", dual_terms("team_esid", child_team_esids)),
            ("de_asset_fb_pages", dual_terms("team_esid", child_team_esids)),
        ):
            try:
                resp = es.delete_by_query(
                    index=index,
                    body={"query": query},
                    refresh=True,
                    conflicts="proceed",
                )
                counts[index] = int(resp.get("deleted", 0))
            except Exception as e:
                err = str(e).lower()
                if "index_not_found" in err or "404" in err:
                    counts[index] = 0
                    continue
                logging.warning(
                    "wizard-asset purge failed for workspace %s on %s: %s",
                    workspace_esid,
                    index,
                    e,
                )
                counts[index] = 0
    else:
        for index in (
            "de_asset_ig_accounts",
            "de_asset_fb_ad_accounts",
            "de_asset_fb_pages",
        ):
            counts[index] = 0

    # Step 3.6: purge de_connection + de_connection_asset rows
    # owned by users who, AFTER step 2's workspace_membership purge, are
    # now in ZERO workspaces. The de_connection index has no
    # workspace_esid field — its scope is `principal` (username). A
    # naive principal-keyed purge would delete OAuth tokens belonging to
    # users who are still active in other workspaces (cross-workspace
    # footgun). The "now in zero workspaces" guard avoids that — a user
    # whose only workspace is being deleted has no remaining workspace
    # to use those tokens from, and the connection token is a security
    # hygiene concern (per the ticket).
    orphaned_principals: list[str] = []
    for username in former_member_usernames:
        try:
            resp = es.count(
                index=WORKSPACE_MEMBERSHIP_INDEX,
                body={"query": {"term": {"username": username}}},
            )
            remaining = int(resp.get("count", 0))
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                remaining = 0
            else:
                logging.warning(
                    "workspace-membership count failed for %s: %s",
                    username,
                    e,
                )
                # Treat lookup failure as "user still has memberships"
                # to be safe — don't risk deleting a token we can't
                # verify is orphaned.
                continue
        if remaining == 0:
            orphaned_principals.append(username)

    if orphaned_principals:
        for index in ("de_connection", "de_connection_asset"):
            try:
                resp = es.delete_by_query(
                    index=index,
                    body={"query": {"terms": {"principal": orphaned_principals}}},
                    refresh=True,
                    conflicts="proceed",
                )
                counts[index] = int(resp.get("deleted", 0))
            except Exception as e:
                err = str(e).lower()
                if "index_not_found" in err or "404" in err:
                    counts[index] = 0
                    continue
                logging.warning(
                    "connection purge failed for workspace %s on %s: %s",
                    workspace_esid,
                    index,
                    e,
                )
                counts[index] = 0
    else:
        counts["de_connection"] = 0
        counts["de_connection_asset"] = 0

    # Step 4: data grants. DataGrantService.scope_type is "org" | "team"
    # today; a future "workspace" scope is anticipated by the schema.
    # Match all three to be forward-compatible.
    grant_clauses: list[dict] = [
        {
            "bool": {
                "must": [
                    {"term": {"scope_type": "workspace"}},
                    {"term": {"scope_esid": workspace_esid}},
                ]
            }
        }
    ]
    if child_org_esids:
        grant_clauses.append(
            {
                "bool": {
                    "must": [
                        {"term": {"scope_type": "org"}},
                        {"terms": {"scope_esid": child_org_esids}},
                    ]
                }
            }
        )
    if child_team_esids:
        grant_clauses.append(
            {
                "bool": {
                    "must": [
                        {"term": {"scope_type": "team"}},
                        {"terms": {"scope_esid": child_team_esids}},
                    ]
                }
            }
        )
    try:
        resp = es.delete_by_query(
            index=DATA_GRANT_INDEX,
            body={"query": {"bool": {"should": grant_clauses, "minimum_should_match": 1}}},
            refresh=True,
            conflicts="proceed",
        )
        counts[DATA_GRANT_INDEX] = int(resp.get("deleted", 0))
    except Exception as e:
        err = str(e).lower()
        if "index_not_found" in err or "404" in err:
            counts[DATA_GRANT_INDEX] = 0
        else:
            logging.warning(
                "data-grant purge failed for workspace %s: %s",
                workspace_esid,
                e,
            )
            counts[DATA_GRANT_INDEX] = 0

    # Step 5: purge wizard sessions for every former member.
    # Doc id is ``<username>:onboarding`` per WizardSessionService._doc_id.
    # 404 is fine — a member may never have opened the wizard.
    wizard_session_deletes = 0
    for username in former_member_usernames:
        try:
            es.delete(
                index="de_wizard_session",
                id=f"{username}:onboarding",
                refresh=True,
            )
            wizard_session_deletes += 1
        except Exception as e:
            err = str(e).lower()
            if "not_found" in err or "404" in err:
                continue
            logging.warning(
                "wizard-session purge failed for %s: %s",
                username,
                e,
            )
    counts["de_wizard_session"] = wizard_session_deletes

    return counts
