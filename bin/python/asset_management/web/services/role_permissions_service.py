"""
Role Permissions Service
Manages per-role visibility settings for menus and navigation links.

Stores permission documents in the ``de_role_permissions`` Elasticsearch
index.  Each document is keyed by role name and contains boolean flags
for every controllable UI element (sidebar items, header links, etc.).

The ``get_merged_permissions()`` helper OR-merges permissions across all
of a user's roles so templates can do simple ``{% if user_perms.mc_* %}``
checks.
"""

import logging
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")
from lib.es_helpers import (
    get_elasticsearch_client,
    get_es_admin_client,
    put_regular_index_template,
    create_index_if_not_exists,
)

INDEX = "de_role_permissions"

# surface the System 2 seeded roles (workspace/org/team-tier
# admin bundles defined in code by PermissionsSeederService) on the
# Roles tab as read-only entries. Keeping the esid list local avoids a
# circular import via permissions_seeder_service (which itself imports
# from permissions_enum). The list mirrors
# permissions_seeder_service.DEFAULT_ROLES exactly; a drift check at
# get_roles_for_listing() time would couple the two modules harder
# without catching anything real (the seeder is the only writer of
# de_authz_role:is_system=True docs).
_SYSTEM2_ROLE_ESIDS: tuple[str, ...] = (
    "workspace_owner",
    "workspace_admin",
    "workspace_member",
    "owner",
    "org_admin",
    "team_admin",
    "editor",
    "viewer",
)

# de_authz_role.applies_to → de_authz_user_<tier>_role index for
# counting user assignments. Kept here rather than importing from
# permissions_index_service to avoid the import cycle described above.
_SYSTEM2_USER_ROLE_INDEX = {
    "workspace": "de_authz_user_workspace_role",
    "org": "de_authz_user_org_role",
    "team": "de_authz_user_team_role",
}

# Pinned delete-reason for System 2 rows. ``can_delete=False`` because
# the bundle is code-defined; the UI surfaces this string when the
# Delete button would otherwise appear.
_SYSTEM2_DELETE_REASON = "Built-in role, defined in code"

# Kibana application privileges attached to every custom ES Security role
# created via ``create_role``. Lets a user with the role read dashboards and
# Discover in the default Kibana space. Inlined from
# RoleManagementService._put_role on retirement.
_KIBANA_PRIVILEGES = [
    {
        "application": "kibana-.kibana",
        "privileges": ["feature_dashboard.read", "feature_discover.read"],
        "resources": ["space:default"],
    }
]

INDEX_MAPPING = {
    "properties": {
        "@timestamp": {"type": "date"},
        "role_name": {"type": "keyword"},
        "role_type": {"type": "keyword"},
        "is_locked": {"type": "boolean"},
        # Management Console sidebar
        "mc_asset_explorer": {"type": "boolean"},
        "mc_data_connectors": {"type": "boolean"},
        "mc_data_explorer": {"type": "boolean"},
        "mc_import_logs": {"type": "boolean"},
        "mc_import_status_reports": {"type": "boolean"},
        "mc_import_data": {"type": "boolean"},
        "mc_data_inventory": {"type": "boolean"},
        "mc_restore_backup": {"type": "boolean"},
        "mc_user_management": {"type": "boolean"},
        # Field Catalog picker page — owner-only (WORKSPACE_DELETE),
        # granted via the bridge; role docs rarely set it directly.
        "mc_field_catalog": {"type": "boolean"},
        "mc_grants_management": {"type": "boolean"},
        "mc_dashboard_builder": {"type": "boolean"},
        # Canvas AI Assistant capability — gate 3 of the
        # three-gate model. Carried by the dedicated ``ai_assistant``
        # system role (opt-in grant, never a deny); superuser-only in
        # the Roles surfaces (see _ELASTIC_ONLY_FIELDS).
        "mc_ai_assistant": {"type": "boolean"},
        "mc_testing": {"type": "boolean"},
        # Header navigation links
        "hdr_dashboards_link": {"type": "boolean"},
        "hdr_elastic_link": {"type": "boolean"},
        # Dashboard page header links
        "db_management_console": {"type": "boolean"},
        "db_data_explorer": {"type": "boolean"},
        "db_elastic": {"type": "boolean"},
        # Capability flags
        "publish_reports": {"type": "boolean"},
        # Optional human-readable description of the role for the Roles tab UI.
        "description": {"type": "text"},
        # Metadata
        "updated_by": {"type": "keyword"},
        "updated_at": {"type": "date"},
    }
}

# Every boolean permission field name.
_PERM_FIELDS = [
    "mc_asset_explorer",
    "mc_data_connectors",
    "mc_data_explorer",
    "mc_import_logs",
    "mc_import_status_reports",
    "mc_import_data",
    "mc_data_inventory",
    "mc_restore_backup",
    "mc_user_management",
    "mc_field_catalog",
    "mc_grants_management",
    "mc_dashboard_builder",
    "mc_ai_assistant",
    "mc_testing",
    "hdr_dashboards_link",
    "hdr_elastic_link",
    "db_management_console",
    "db_data_explorer",
    "db_elastic",
    "publish_reports",
]

# Fields that only superuser can have set to True.
#
# mc_ai_assistant joins the set (user decision 2026-07-28,
# defense-in-depth on a spend-metered capability): update_permissions
# forces it False on every non-superuser role and create_role forces it
# False unconditionally, so the ONLY document that ever carries it True
# is the code-seeded (and is_locked) ``ai_assistant`` role. Runtime
# grants flow through get_merged_permissions, not these docs.
_ELASTIC_ONLY_FIELDS = {"hdr_elastic_link", "db_elastic", "mc_ai_assistant"}

# Fields that require an admin-level role (superuser, asset_admin).
_MC_FIELDS = [f for f in _PERM_FIELDS if f.startswith("mc_")]

# Roles that are considered "admin" for the purpose of mc_* assignment.
_ADMIN_ROLES = {"superuser", "asset_admin"}

# System roles that cannot be deleted.
_PROTECTED_ROLES = {
    "superuser",
    "asset_admin",
    "editor",
    "elastic",
    "report_publisher",
    "ai_assistant",
}

# canonical role → human-friendly display-name map. The single
# source for role labels in the Create User / Edit Roles pickers and the
# role badges (via the ``role_display`` Jinja filter and the serialized
# JS copy in user_management.html). Only entries whose title-cased
# fallback is wrong or ambiguous need to be listed — everything else goes
# through role_display_name()'s fallback. Deliberately NOT merged with
# _INVITE_ROLE_DISPLAY (article-prefixed banner prose), _FRIENDLY_ROLE_NAMES
# (membership chips), or the first_role_display filter (org/team titles) —
# those carry different shapes for different surfaces (user decision on
# ).
ROLE_DISPLAY_NAMES = {
    "data_explorer": "Data Explorer",
    "report_publisher": "Report Publisher",
    "ai_assistant": "AI Assistant",
}


def role_display_name(role: str) -> str:
    """Human-friendly label for an ES Security role name.

    Unmapped roles fall back to a title-cased, underscore-stripped
    rendering (``grant_writer`` → ``Grant Writer``) so newly created
    custom roles never regress to snake_case in the UI.
    """
    if not role:
        return ""
    return ROLE_DISPLAY_NAMES.get(role) or role.replace("_", " ").title()


# ── Default permission sets for seeding ──────────────────────────────────────

# Documentation-only fields that admins cannot edit via the Roles tab —
# the only fields where seed_defaults() may upsert on each startup
# without risking clobbering admin-toggled values. All other fields stay
# create-only (admins toggle them via /api/role-permissions PATCH).
_UPSERTABLE_SEED_FIELDS = ("description",)

# seeded roles that must ALSO exist as ES Security roles so the
# Edit Roles modal (which writes ``user.roles`` via es.security.put_user)
# can grant them. superuser / asset_admin / report_publisher predate the
# app and already exist on every cluster; nothing else in the repo creates
# ES roles outside the Roles-tab create path, so new code-seeded grantable
# roles must be listed here — seed_defaults() creates them if missing.
_SEED_ES_SECURITY_ROLES = ("ai_assistant",)

_DEFAULTS = {
    "superuser": {
        "role_type": "system",
        "is_locked": True,
        "mc_asset_explorer": True,
        "mc_data_connectors": True,
        "mc_data_explorer": True,
        "mc_import_logs": True,
        "mc_import_status_reports": True,
        "mc_import_data": True,
        "mc_data_inventory": True,
        "mc_restore_backup": True,
        "mc_user_management": True,
        "mc_grants_management": True,
        "mc_dashboard_builder": True,
        "mc_ai_assistant": True,
        "mc_testing": True,
        "hdr_dashboards_link": True,
        "hdr_elastic_link": True,
        "db_management_console": True,
        "db_data_explorer": True,
        "db_elastic": True,
        "publish_reports": True,
    },
    "asset_admin": {
        "role_type": "system",
        "is_locked": False,
        # (Layer 2, defaults-only): Asset Explorer + User
        # Management are workspace-tier-only surfaces. New asset_admin
        # seeds no longer grant these two menus, matching the System-2
        # tier model. NOTE: seed_defaults() is op_type=create, so this
        # flip affects ONLY first seeds / new clusters — existing prod
        # asset_admin docs are NOT migrated (no one-shot ES update was
        # run; that was deliberately deferred as a separate scope
        # decision). superuser keeps both menus.
        "mc_asset_explorer": False,
        "mc_data_connectors": False,
        "mc_data_explorer": True,
        "mc_import_logs": True,
        "mc_import_status_reports": True,
        "mc_import_data": True,
        "mc_data_inventory": True,
        "mc_restore_backup": True,
        "mc_user_management": False,
        "mc_grants_management": False,
        "mc_dashboard_builder": True,
        # excluded from asset_admin (spend-metered; must be
        # granted via the ai_assistant role) — mirrors the runtime map.
        "mc_ai_assistant": False,
        "mc_testing": True,
        "hdr_dashboards_link": True,
        "hdr_elastic_link": False,
        "db_management_console": True,
        "db_data_explorer": True,
        "db_elastic": False,
        "publish_reports": True,
    },
    # Dedicated opt-in role carrying exactly one capability —
    # mc_ai_assistant (gate 3 of the three-gate AI Assistant model). A
    # workspace admin/owner grants it per user through the Edit Roles
    # modal; workspace enablement alone grants nothing (fail-closed
    # opt-in, never a deny role — see the decision log).
    # is_locked so the Roles-tab permissions editor cannot mutate the
    # doc; the capability is defined here in code only.
    "ai_assistant": {
        "role_type": "system",
        "is_locked": True,
        "description": (
            "Grants access to the Canvas AI Assistant (a paid, usage-metered "
            "feature). The user's workspace must also have AI Assistant "
            "enabled by a Kavanah administrator, and the user needs Dashboard "
            "Builder access — this role adds no other permissions."
        ),
        "mc_asset_explorer": False,
        "mc_data_connectors": False,
        "mc_data_explorer": False,
        "mc_import_logs": False,
        "mc_import_status_reports": False,
        "mc_import_data": False,
        "mc_data_inventory": False,
        "mc_restore_backup": False,
        "mc_user_management": False,
        "mc_grants_management": False,
        "mc_dashboard_builder": False,
        "mc_ai_assistant": True,
        "mc_testing": False,
        "hdr_dashboards_link": False,
        "hdr_elastic_link": False,
        "db_management_console": False,
        "db_data_explorer": False,
        "db_elastic": False,
        "publish_reports": False,
    },
    # Dedicated role that grants only the publish_reports capability.
    # Members can mint / regenerate / revoke public report links but have no
    # other admin permissions. Pair with mc_dashboard_builder via a second
    # role assignment if the user also needs to edit dashboards.
    "report_publisher": {
        "role_type": "system",
        "is_locked": False,
        "description": (
            "Allows the user to share Impact Reports via public URLs. "
            "Members can publish, regenerate, and revoke shareable report "
            "links from the Publish dialog. They cannot edit dashboards "
            "or access any other admin tooling — pair with another role "
            "(such as asset_admin) if they also need to build reports."
        ),
        "mc_asset_explorer": False,
        "mc_data_connectors": False,
        "mc_data_explorer": False,
        "mc_import_logs": False,
        "mc_import_status_reports": False,
        "mc_import_data": False,
        "mc_data_inventory": False,
        "mc_restore_backup": False,
        "mc_user_management": False,
        "mc_grants_management": False,
        "mc_dashboard_builder": False,
        "mc_ai_assistant": False,
        "mc_testing": False,
        "hdr_dashboards_link": True,
        "hdr_elastic_link": False,
        "db_management_console": False,
        "db_data_explorer": False,
        "db_elastic": False,
        "publish_reports": True,
    },
}

# ── In-memory permission cache ───────────────────────────────────────────────

_perm_cache: dict[frozenset, tuple[dict, float]] = {}
_PERM_CACHE_TTL = 120  # seconds


class RolePermissionsService:
    """Service for managing per-role UI visibility settings."""

    # ─────────────────────────────────────────────────────────────────────────
    # Index bootstrap
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the role permissions index if it does not exist."""
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("RolePermissionsService: could not connect to ES")
            return
        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)
        try:
            es.indices.put_mapping(index=INDEX, body=INDEX_MAPPING)
        except Exception as e:
            logging.warning("RolePermissionsService: put_mapping update: %s", e)

    # ─────────────────────────────────────────────────────────────────────────
    # Seed defaults
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def seed_defaults() -> None:
        """Create default permission documents for system roles if missing,
        and upsert documentation-only fields on existing docs.

        For new roles (e.g. ``report_publisher`` arriving with),
        the full default permission doc is written with ``op_type=create``.

        For roles whose doc already exists, ``description`` is the only
        field that gets refreshed on each startup. Admin-mutable fields
        (``mc_*``, ``hdr_*``, ``db_*``, ``publish_reports``, etc.) are
        deliberately *not* upserted — that would clobber admin-toggled
        values every restart.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("RolePermissionsService: could not connect to ES for seeding")
            return

        for role_name, defaults in _DEFAULTS.items():
            try:
                es.index(
                    index=INDEX,
                    id=role_name,
                    body={
                        "role_name": role_name,
                        **defaults,
                        "updated_by": "system",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "@timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    op_type="create",  # skip if doc already exists
                )
                logging.info("RolePermissionsService: seeded defaults for '%s'", role_name)
            except Exception as e:
                if "version_conflict_engine_exception" in str(e).lower():
                    # Document already exists — refresh upsertable doc-only
                    # fields (currently just ``description``) so prod picks up
                    # text edits without a manual backfill.
                    upsert = {k: defaults[k] for k in _UPSERTABLE_SEED_FIELDS if k in defaults}
                    if upsert:
                        try:
                            es.update(index=INDEX, id=role_name, doc=upsert)
                            logging.info(
                                "RolePermissionsService: refreshed %s for '%s'",
                                ",".join(upsert.keys()),
                                role_name,
                            )
                        except Exception as upd_err:
                            logging.error(
                                "RolePermissionsService: failed to refresh upsertable fields for '%s': %s",
                                role_name,
                                upd_err,
                            )
                else:
                    logging.error("RolePermissionsService: error seeding '%s': %s", role_name, e)

        # make sure code-seeded grantable roles exist in ES
        # Security. A permission doc alone is not grantable — the Edit
        # Roles modal writes ``user.roles``, which ES rejects silently
        # for roles that don't exist as security roles. Marker roles
        # only: no cluster/index privileges (the capability rides the
        # merged-permissions map, not ES privileges).
        for role_name in _SEED_ES_SECURITY_ROLES:
            try:
                existing = es.security.get_role(name=role_name)
                if existing and role_name in existing:
                    continue
            except Exception:
                pass  # not found — create below
            try:
                get_es_admin_client(es).security.put_role(name=role_name, body={"cluster": [], "indices": []})
                logging.info("RolePermissionsService: created ES security role '%s'", role_name)
            except Exception as e:
                logging.error(
                    "RolePermissionsService: failed to create ES security role '%s': %s",
                    role_name,
                    e,
                )

    # ─────────────────────────────────────────────────────────────────────────
    # Read helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _classify_role(role_name: str) -> str:
        """Return the role type: 'system', 'org', or 'team'."""
        if role_name in _PROTECTED_ROLES or role_name in _DEFAULTS:
            return "system"
        if role_name.startswith("org_"):
            return "org"
        if role_name.startswith("team_"):
            return "team"
        return "system"

    @staticmethod
    def _default_permissions(role_name: str) -> dict:
        """Return sensible default permissions for a role with no stored doc."""
        if role_name in _DEFAULTS:
            return {**_DEFAULTS[role_name], "role_name": role_name}

        role_type = RolePermissionsService._classify_role(role_name)
        # org_* and team_* roles get dashboard-only defaults
        result = {"role_name": role_name, "role_type": role_type, "is_locked": False}
        for field in _PERM_FIELDS:
            result[field] = False
        # Give them the dashboards link so they can navigate back
        result["hdr_dashboards_link"] = True
        return result

    @staticmethod
    def get_permissions(role_name: str) -> dict:
        """Fetch the permission settings for a single role.

        Returns stored permissions or sensible defaults if no doc exists.
        Permission fields added to `_PERM_FIELDS` after a doc was last written
        are backfilled from `_DEFAULTS` (for system roles) or False (for custom
        roles), so adding a new permission field doesn't require a data
        migration.
        """
        es = get_elasticsearch_client()
        if es is None:
            return RolePermissionsService._default_permissions(role_name)
        try:
            result = es.get(index=INDEX, id=role_name)
            doc = dict(result["_source"])
        except Exception:
            return RolePermissionsService._default_permissions(role_name)

        role_defaults = _DEFAULTS.get(role_name, {})
        for field in _PERM_FIELDS:
            if field not in doc:
                doc[field] = bool(role_defaults.get(field, False))
        return doc

    @staticmethod
    def get_all_permissions() -> dict[str, dict]:
        """Fetch all stored permission documents, keyed by role name."""
        es = get_elasticsearch_client()
        if es is None:
            return {}
        try:
            result = es.search(index=INDEX, body={"query": {"match_all": {}}}, size=1000)
            return {hit["_id"]: hit["_source"] for hit in result.get("hits", {}).get("hits", [])}
        except Exception as e:
            logging.error("RolePermissionsService: get_all_permissions error: %s", e)
            return {}

    # ─────────────────────────────────────────────────────────────────────────
    # System 2 read-only catalog
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _fetch_system2_role_doc(es, role_esid: str) -> dict | None:
        """Return the seeded ``de_authz_role`` doc for ``role_esid``, or
        None if absent. Treat any read failure as absent so a degraded
        ES connection doesn't break the Roles tab for System 1.
        """
        try:
            result = es.get(index="de_authz_role", id=role_esid)
            return dict(result["_source"])
        except Exception:
            return None

    @staticmethod
    def _fetch_system2_user_count(es, role_esid: str, applies_to: str) -> int:
        """Count DISTINCT users holding a System 2 role.

        Reads ``de_authz_user_<tier>_role`` with a ``term: {role_esid}``
        filter and runs a ``cardinality`` agg on ``username``. :
        the prior implementation counted rows, which double-counts a
        user holding the same role across multiple scopes (e.g. a
        workspace_owner across 4 workspaces was counted as 4 users).

        Returns 0 if the tier is unknown or the search call fails.
        """
        tier_index = _SYSTEM2_USER_ROLE_INDEX.get(applies_to)
        if not tier_index:
            return 0
        try:
            result = es.search(
                index=tier_index,
                body={
                    "size": 0,
                    "query": {"term": {"role_esid": role_esid}},
                    "aggs": {"users": {"cardinality": {"field": "username"}}},
                },
            )
            return int(result.get("aggregations", {}).get("users", {}).get("value", 0))
        except Exception:
            return 0

    @staticmethod
    def _build_system2_listings(es) -> list[dict]:
        """Build the System 2 read-only rows for the Roles tab.

        Each row carries the same shape as System 1 entries plus the
        pinned ``can_delete=False`` / ``delete_reason`` and a forced
        ``is_locked=True``. Missing role docs (the seeder didn't run on
        this cluster yet) are skipped silently — the System 2 tab simply
        won't have that row until the next boot.
        """
        rows: list[dict] = []
        for esid in _SYSTEM2_ROLE_ESIDS:
            doc = RolePermissionsService._fetch_system2_role_doc(es, esid)
            if not doc:
                continue
            applies_to = doc.get("applies_to") or ""
            if applies_to not in _SYSTEM2_USER_ROLE_INDEX:
                continue  # unknown tier — skip rather than mis-classify
            user_count = RolePermissionsService._fetch_system2_user_count(es, esid, applies_to)
            rows.append(
                {
                    "role_name": esid,
                    "role_type": applies_to,
                    "is_locked": True,
                    "user_count": user_count,
                    "can_delete": False,
                    "delete_reason": _SYSTEM2_DELETE_REASON,
                    "description": doc.get("description") or "",
                    # marker so the template renders the
                    # ``Permissions`` button as a read-only viewer
                    # rather than disabling it. System 1 rows omit
                    # the field; the JS reads ``!!r.system2``.
                    "system2": True,
                }
            )
        return rows

    @staticmethod
    def get_system2_permissions(role_esid: str) -> dict | None:
        """Return the read-only permission bundle for a System 2 role.

        Shape: ``{role_name, role_type, is_locked: True, description,
        permissions: [{key, resource, action, description}, ...]}``.
        Frontend distinguishes this from the System 1 ``mc_*`` shape by
        the presence of the ``permissions`` array.

        Returns ``None`` if the role doc is missing — caller falls back
        to the System 1 path.
        """
        es = get_elasticsearch_client()
        if es is None:
            return None
        doc = RolePermissionsService._fetch_system2_role_doc(es, role_esid)
        if not doc:
            return None

        # Pull the role's permission links and resolve each to its
        # metadata doc. Both indices are seeded by PermissionsSeederService
        # — a missing permission doc is a seeder gap, not a runtime
        # error, so we surface the key alone in that case.
        permission_keys: list[str] = []
        try:
            link_result = es.search(
                index="de_authz_role_permission",
                body={"query": {"term": {"role_esid": role_esid}}, "size": 100},
            )
            for hit in link_result.get("hits", {}).get("hits", []):
                key = hit.get("_source", {}).get("permission_key")
                if key:
                    permission_keys.append(key)
        except Exception as e:
            logging.error("RolePermissionsService: error fetching System 2 perms for '%s': %s", role_esid, e)

        permissions: list[dict] = []
        for key in sorted(permission_keys):
            try:
                pdoc = es.get(index="de_authz_permission", id=key)
                meta = dict(pdoc["_source"])
            except Exception:
                meta = {"key": key}
            permissions.append(
                {
                    "key": key,
                    "resource": meta.get("resource") or "",
                    "action": meta.get("action") or "",
                    "description": meta.get("description") or "",
                }
            )

        return {
            "role_name": role_esid,
            "role_type": doc.get("applies_to") or "",
            "is_locked": True,
            "description": doc.get("description") or "",
            "permissions": permissions,
        }

    @staticmethod
    def is_system2_role(role_name: str) -> bool:
        """True if ``role_name`` matches a seeded System 2 role esid."""
        return role_name in _SYSTEM2_ROLE_ESIDS

    @staticmethod
    def get_merged_permissions(roles: list[str], username: str | None = None) -> dict:
        """Return effective UI-flag permissions for a user's roles.

         (Phase 5 cleanup) replaced the per-role lookup against
        ``de_role_permissions`` with a deterministic three-case map:

        * ``superuser`` or ``asset_admin`` → all flags True
        * ``data_explorer`` (and not admin) → only the two flags
          required to reach Data Explorer (``db_data_explorer`` +
          ``hdr_dashboards_link``); everything else False
        * Anything else (org_*, team_*, custom roles) → all flags
          False (scoped users don't see admin sidebar items), except
          ``db_data_explorer``, which every user holds

        The legacy ES-backed lookup let admins toggle 15 boolean
        flags per custom role in the Roles tab. In practice the only
        non-admin role configured was ``data_explorer``, which had
        exactly the carve-out hardcoded above. retired the
        per-role configuration in favor of this fixed map; the
        Roles tab still renders for admins but its checkboxes no
        longer affect runtime behavior. is the source of
        truth for admin/scope decisions.

         added an OPT-IN System 2 bridge for wizard-signup
        users. Wizard accounts are created with no ES Security roles
        (``DEFAULT_SIGNUP_ROLES = []``), so their ``session["roles"]``
        is empty even though they hold ``workspace_owner`` /
        ``workspace_admin`` grants in the System 2 store
        (``de_authz_user_workspace_role``). When a caller passes a
        non-empty ``username``, we consult :class:`PermissionResolver`
        for the workspace-tier ``WORKSPACE_MANAGE`` permission and, if
        present, grant the workspace-tier management surface
        (``mc_data_connectors`` + ``mc_asset_explorer`` +
        ``mc_dashboard_builder`` + ``db_management_console`` +
        ``hdr_dashboards_link``; ``mc_user_management`` only on
        ``WORKSPACE_DELETE``). Callers that do NOT pass ``username``
        get the legacy zero-ES behaviour unchanged.

         (2026-06-04): the scoped-ES-role grants
        (``data_explorer`` / ``report_publisher``) and the System-2
        bridge are ADDITIVE. They used to ``return`` early, so a user
        holding ``report_publisher`` (an ES-Security role) AND an
        org/workspace ``owner`` grant in System 2 had their whole
        Management Console hidden — the ``report_publisher`` branch
        short-circuited before the ``WORKSPACE_MANAGE`` bridge could
        run. Now every non-admin grant ORs into a single ``scoped``
        dict and the bridge runs unconditionally on top. ``superuser`` /
        ``asset_admin`` still return ``all_true`` early (nothing for
        the bridge to add).
        """
        all_false = {field: False for field in _PERM_FIELDS}
        all_true = {field: True for field in _PERM_FIELDS}

        role_set = set(roles or [])

        if "superuser" in role_set:
            return all_true

        if "asset_admin" in role_set:
            # asset_admin sees every admin surface EXCEPT
            # Data Grants — cross-tenant data-access exceptions are a
            # Kavanah-staff (superuser) decision, not a per-tenant
            # admin one. The grants endpoints in app.py enforce the
            # same boundary via _require_superuser; this gate keeps
            # the sidebar link consistent so asset_admins don't see
            # a link that 403s when clicked.
            #
            # (user decision 2026-07-28): mc_ai_assistant is
            # also excluded from the all-True carve-out. The AI
            # Assistant is spend-metered — an asset_admin must be
            # granted the ai_assistant role explicitly like everyone
            # else, so the carve-out can't silently bill a workspace.
            # (is retiring asset_admin from the pickers; the
            # exclusion keeps the runtime map fail-closed meanwhile.)
            #
            # the Elastic link (hdr_elastic_link / db_elastic)
            # is excluded too — the /kibana gate admits only
            # superuser, so an asset_admin saw a link that 403'd. Driven
            # by _ELASTIC_ONLY_FIELDS so the runtime map and the stored-doc
            # rule in update_permissions/create_role share one list.
            merged = {
                **all_true,
                "mc_grants_management": False,
                **{field: False for field in _ELASTIC_ONLY_FIELDS},
            }
            if "ai_assistant" in role_set:
                merged["mc_ai_assistant"] = True
            return merged

        # the scoped-ES-role grants (data_explorer, report_publisher)
        # and the System-2 workspace bridge below are ADDITIVE, not mutually
        # exclusive. A user can hold an ES-Security report_publisher role AND
        # be an org/workspace owner in the System-2 store. The branches used
        # to ``return scoped`` early, which short-circuited the bridge — so
        # granting report_publisher to an org owner silently hid their entire
        # Management Console (mc_data_connectors etc. forced False). Accumulate
        # into one dict and OR each grant in; the bridge runs unconditionally
        # afterwards. (The superuser / asset_admin branches above still return
        # all_true early — there is nothing for the bridge to add on top.)
        scoped = dict(all_false)

        # (a teammate, 2026-10-27): Data Explorer / Ask Kavanah is open to
        # every signed-in user. The flag grants the page, not data: nlq_service
        # scopes every query to the caller's teams (filter,
        # index scope) and the routes refuse a caller with no scope before any
        # model spend.
        scoped["db_data_explorer"] = True

        if "data_explorer" in role_set:
            scoped["db_data_explorer"] = True
            scoped["hdr_dashboards_link"] = True

        # report_publisher is a non-admin scope that grants only
        # the publish_reports capability (and the dashboards link so the
        # user can navigate to a report and click Publish). Admin roles
        # checked above already imply publish_reports.
        if "report_publisher" in role_set:
            scoped["publish_reports"] = True
            scoped["hdr_dashboards_link"] = True

        # the ai_assistant role grants exactly one capability —
        # the Canvas AI Assistant. Additive like the grants above; the
        # other two gates (workspace enablement + mc_dashboard_builder)
        # are enforced at the canvas surface, not here.
        if "ai_assistant" in role_set:
            scoped["mc_ai_assistant"] = True

        # opt-in System 2 bridge for wizard users. See the
        # docstring above for the policy. Lazily imported to avoid an
        # import cycle (PermissionResolver pulls in services that
        # transitively depend on RolePermissionsService).
        if username:
            try:
                from asset_management.web.services.permission_resolver import (
                    PermissionResolver,
                )
                from asset_management.web.services.permissions_enum import Permission

                if PermissionResolver.has_permission(username, Permission.WORKSPACE_MANAGE):
                    # follow-up (2026-06-02): the original
                    # bridge granted ONLY mc_data_connectors, so a
                    # workspace_owner/admin whose scope lives only in role
                    # docs (no ES-Security role) could reach Data Connectors
                    # but NOT Asset Explorer / Dashboard Builder / User
                    # Management — they saw a near-empty console. Grant the
                    # full workspace-tier management surface here.
                    #
                    # User Management is OWNER-ONLY: gate it on WORKSPACE_DELETE
                    # (owner's list(Permission) has it; admin's explicit bundle
                    # does not). Admin keeps the user-management *capability*
                    # (USER_INVITE/REMOVE/ROLE_ASSIGNMENT_MANAGE) for API use;
                    # it just doesn't get the page.
                    scoped["mc_data_connectors"] = True
                    scoped["mc_asset_explorer"] = True
                    scoped["mc_dashboard_builder"] = True
                    # Data Inventory is a workspace-tier read-only
                    # data-coverage page. Bridged on WORKSPACE_MANAGE so both
                    # workspace_owner and workspace_admin reach it, consistent
                    # with the other workspace-tier management surfaces above.
                    scoped["mc_data_inventory"] = True
                    scoped["db_management_console"] = True
                    scoped["hdr_dashboards_link"] = True
                    if PermissionResolver.has_permission(username, Permission.WORKSPACE_DELETE):
                        scoped["mc_user_management"] = True
                        # the Field Catalog picker is a per-tenant
                        # data-governance surface — owner-only, same primitive.
                        scoped["mc_field_catalog"] = True
            except Exception as e:  # noqa: BLE001 — defensive boundary
                # ES outage / import surprise — deny by default rather
                # than fail the request. The legitimate-deny path below
                # is the same outcome a no-grant user would get.
                logging.warning(
                    "RolePermissionsService.get_merged_permissions:  bridge for %s failed: %s",
                    username,
                    e,
                )

        return scoped

    # ─────────────────────────────────────────────────────────────────────────
    # Update
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def update_permissions(role_name: str, settings: dict, updated_by: str) -> dict:
        """Update the permission settings for a role.

        Enforces business rules:
        - Superuser (is_locked) cannot be modified.
        - Elastic links are forced false for non-superuser roles.
        - mc_* fields require db_management_console to be True.

        Returns ``{"success": True}`` or ``{"success": False, "error": "..."}``.
        """
        # Check if locked
        current = RolePermissionsService.get_permissions(role_name)
        if current.get("is_locked"):
            return {"success": False, "error": "Superuser permissions cannot be modified"}

        role_type = RolePermissionsService._classify_role(role_name)
        mc_allowed = bool(settings.get("db_management_console", False))

        # Build the update doc from submitted settings
        doc = {
            "role_name": role_name,
            "role_type": role_type,
            "is_locked": False,
            "updated_by": updated_by,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "@timestamp": datetime.now(timezone.utc).isoformat(),
        }
        for field in _PERM_FIELDS:
            value = bool(settings.get(field, False))

            # Elastic links are superuser-only
            if field in _ELASTIC_ONLY_FIELDS and role_name != "superuser":
                value = False

            # mc_* fields require the Management Console dashboard link
            if field in _MC_FIELDS and not mc_allowed:
                value = False

            doc[field] = value

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        try:
            es.index(index=INDEX, id=role_name, body=doc)
            # Invalidate cache
            _perm_cache.clear()
            logging.info(
                "RolePermissionsService: updated permissions for '%s' by '%s'",
                role_name,
                updated_by,
            )
            return {"success": True}
        except Exception as e:
            logging.error("RolePermissionsService: update error for '%s': %s", role_name, e)
            return {"success": False, "error": "Failed to update role permissions."}

    # ─────────────────────────────────────────────────────────────────────────
    # Roles listing for the Roles tab
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_roles_for_listing() -> list[dict]:
        """Return all roles with metadata for the Roles tab.

        Each entry contains: role_name, role_type, is_locked, user_count,
        can_delete, delete_reason.
        """
        es = get_elasticsearch_client()
        if es is None:
            return []

        # Get all ES security roles (non-system)
        from asset_management.web.services.role_management_service import (
            RoleManagementService,
            _SYSTEM_ROLE_NAMES as RM_SYSTEM_NAMES,
            _SYSTEM_ROLE_PREFIXES as RM_SYSTEM_PREFIXES,
        )

        custom_roles = RoleManagementService.get_all_role_names()
        all_role_names = custom_roles | set(_DEFAULTS.keys())

        # Get user-role assignments
        user_role_counts: dict[str, int] = {}
        try:
            response = es.security.get_user()
            es_users = dict(response)
            for uname, udata in es_users.items():
                if udata.get("metadata", {}).get("_reserved") or uname.startswith("."):
                    continue
                for role in udata.get("roles", []):
                    user_role_counts[role] = user_role_counts.get(role, 0) + 1
        except Exception as e:
            logging.error("RolePermissionsService: error fetching users: %s", e)

        # Include roles that are assigned to users but may not exist as
        # formal ES security role definitions (legacy / orphan roles).
        # Exclude ES built-in system roles so they don't clutter the list.
        for role in user_role_counts:
            if role not in RM_SYSTEM_NAMES and not any(role.startswith(p) for p in RM_SYSTEM_PREFIXES):
                all_role_names.add(role)

        # Get stored permissions
        all_perms = RolePermissionsService.get_all_permissions()

        roles = []
        for role_name in sorted(all_role_names):
            role_type = RolePermissionsService._classify_role(role_name)
            perms = all_perms.get(role_name, {})
            is_locked = perms.get("is_locked", role_name == "superuser")
            user_count = user_role_counts.get(role_name, 0)
            can_delete, delete_reason = RolePermissionsService.can_delete_role(role_name, user_count=user_count)
            # surface ``description`` so the Roles tab can show
            # admins what each system role does. Falls back to the seed
            # value for system roles whose stored doc predates the
            # description field (covers the prod-rollout window before
            # seed_defaults' upsert backfills it).
            description = perms.get("description") or _DEFAULTS.get(role_name, {}).get("description") or ""

            roles.append(
                {
                    "role_name": role_name,
                    "role_type": role_type,
                    "is_locked": is_locked,
                    "user_count": user_count,
                    "can_delete": can_delete,
                    "delete_reason": delete_reason,
                    "description": description,
                }
            )

        # append the seeded System 2 (workspace/org/team-tier)
        # admin roles as read-only entries. They live in ``de_authz_role``
        # not ES Security, so they're invisible to the System 1 walk
        # above. Append rather than merge — role esids like ``viewer`` /
        # ``editor`` are filtered out of the System 1 collection by
        # RoleManagementService._SYSTEM_ROLE_NAMES so there is no
        # name collision in practice.
        roles.extend(RolePermissionsService._build_system2_listings(es))

        return roles

    # ─────────────────────────────────────────────────────────────────────────
    # Delete
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def can_delete_role(role_name: str, user_count: int | None = None) -> tuple[bool, str]:
        """Check whether a role can be deleted.

        Returns (can_delete, reason).
        """
        # System roles are protected
        if role_name in _PROTECTED_ROLES:
            return False, "System role cannot be deleted"

        role_type = RolePermissionsService._classify_role(role_name)
        if role_type == "system":
            return False, "System role cannot be deleted"

        # Check user assignments
        if user_count is None:
            es = get_elasticsearch_client()
            if es is None:
                return False, "Could not verify user assignments"
            user_count = 0
            try:
                response = es.security.get_user()
                for uname, udata in response.items():
                    if udata.get("metadata", {}).get("_reserved"):
                        continue
                    if role_name in udata.get("roles", []):
                        user_count += 1
            except Exception:
                return False, "Could not verify user assignments"

        if user_count > 0:
            return False, f"Role is assigned to {user_count} user(s)"

        return True, ""

    @staticmethod
    def delete_role(role_name: str) -> dict:
        """Delete a role from ES Security API and its permission document.

        Returns ``{"success": True}`` or ``{"success": False, "error": "..."}``.
        """
        can_delete, reason = RolePermissionsService.can_delete_role(role_name)
        if not can_delete:
            return {"success": False, "error": reason}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        # Delete the ES security role
        try:
            get_es_admin_client(es).security.delete_role(name=role_name)
            logging.info("RolePermissionsService: deleted ES role '%s'", role_name)
        except Exception as e:
            logging.error("RolePermissionsService: error deleting ES role '%s': %s", role_name, e)
            return {"success": False, "error": "Failed to delete role."}

        # Delete permission doc (best effort)
        try:
            es.delete(index=INDEX, id=role_name)
        except Exception:
            pass  # Permission doc may not exist

        _perm_cache.clear()
        return {"success": True}

    # ─────────────────────────────────────────────────────────────────────────
    # Create
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def create_role(role_name: str, role_type: str, settings: dict, created_by: str) -> dict:
        """Create a new custom role with permissions.

        Validates naming convention by type:
          - ``system``: any name; must not collide with protected/built-in names.
          - ``org``: must start with ``org_`` and have a suffix.
          - ``team``: must start with ``team_`` and have a suffix.

        Creates the ES security role (with Kibana dashboard privileges) and
        writes the permissions document. Returns
        ``{"success": True, "role_name": name}`` on success.
        """
        from asset_management.web.services.role_management_service import (
            _SYSTEM_ROLE_NAMES,
            _SYSTEM_ROLE_PREFIXES,
        )

        name = (role_name or "").strip()
        rtype = (role_type or "").strip()

        if not name:
            return {"success": False, "error": "Role name is required"}
        # Cap role name length — propagates to ES security role + every user
        # token; unbounded names bloat both. 64 chars matches the Elasticsearch
        # role-name convention used elsewhere (see app.py:api_create_user).
        if len(name) > 64:
            return {"success": False, "error": "Role name must be 64 characters or fewer"}
        if rtype not in ("system", "org", "team"):
            return {"success": False, "error": "Role type must be system, org, or team"}

        # Name charset: letters, digits, underscore, hyphen
        import re

        if not re.match(r"^[A-Za-z0-9_\-]+$", name):
            return {"success": False, "error": "Role name may only contain letters, digits, underscore, or hyphen"}

        # Type-specific naming rules
        if rtype == "org":
            if not name.startswith("org_") or len(name) <= len("org_"):
                return {"success": False, "error": "Org roles must start with 'org_' followed by a name"}
        elif rtype == "team":
            if not name.startswith("team_") or len(name) <= len("team_"):
                return {"success": False, "error": "Team roles must start with 'team_' followed by a name"}
        else:  # system
            if name.startswith("org_") or name.startswith("team_"):
                return {"success": False, "error": "System roles may not use org_ or team_ prefix"}
            if name in _SYSTEM_ROLE_NAMES:
                return {"success": False, "error": f"'{name}' is a reserved built-in role name"}
            if any(name.startswith(p) for p in _SYSTEM_ROLE_PREFIXES):
                return {"success": False, "error": f"'{name}' uses a reserved built-in role prefix"}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        # Reject if role already exists (ES security role or permissions doc)
        try:
            existing = es.security.get_role(name=name)
            if existing and name in existing:
                return {"success": False, "error": f"Role '{name}' already exists"}
        except Exception:
            pass  # not found — good
        try:
            es.get(index=INDEX, id=name)
            return {"success": False, "error": f"Role '{name}' already has a permissions entry"}
        except Exception:
            pass  # not found — good

        # Create the ES security role with Kibana dashboard privileges.
        # Existence already checked above (line ~661); a failure here is a
        # real ES error worth surfacing to the caller.
        try:
            get_es_admin_client(es).security.put_role(
                name=name,
                body={"cluster": [], "indices": [], "applications": _KIBANA_PRIVILEGES},
            )
        except Exception as e:
            logging.error("RolePermissionsService: failed to create ES role '%s': %s", name, e)
            return {"success": False, "error": "Could not create ES security role."}

        # Build the permissions doc
        mc_allowed = bool(settings.get("db_management_console", False))
        doc = {
            "role_name": name,
            "role_type": rtype,
            "is_locked": False,
            "updated_by": created_by,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "@timestamp": datetime.now(timezone.utc).isoformat(),
        }
        for field in _PERM_FIELDS:
            value = bool(settings.get(field, False))
            # Elastic-only links stay superuser-only
            if field in _ELASTIC_ONLY_FIELDS:
                value = False
            # mc_* fields require the Management Console dashboard link
            if field in _MC_FIELDS and not mc_allowed:
                value = False
            doc[field] = value

        try:
            es.index(index=INDEX, id=name, body=doc)
        except Exception as e:
            # Roll back ES security role so we don't leave orphans
            try:
                get_es_admin_client(es).security.delete_role(name=name)
            except Exception:
                pass
            logging.error("RolePermissionsService: create_role index error '%s': %s", name, e)
            return {"success": False, "error": "Failed to write permissions."}

        _perm_cache.clear()
        logging.info(
            "RolePermissionsService: created role '%s' (type=%s) by '%s'",
            name,
            rtype,
            created_by,
        )
        return {"success": True, "role_name": name, "role_type": rtype}
