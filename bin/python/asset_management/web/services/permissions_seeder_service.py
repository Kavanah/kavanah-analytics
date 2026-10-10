"""
Permissions Seeder Service

Idempotent seeders for the new authz model. Writes the closed
``Permission`` enum into ``de_authz_permission`` and the five default
system roles into ``de_authz_role`` + ``de_authz_role_permission``.
Phase 1 (Build, no behavior change) — nothing reads from these
documents during this phase.

Idempotency rules:

* Permission docs use PUT semantics — the enum + metadata are canonical
  so every boot rewrites ``resource``/``action``/``description`` from
  ``PERMISSION_METADATA``.
* Role docs use PUT semantics — ``name``/``description``/``applies_to``/
  ``is_system`` are canonical. ``created_at`` is preserved across re-seeds
  (read existing doc first); ``updated_at`` is bumped on every boot.
* Role-permission link docs use ``op_type=create`` — the ticket allows
  admins to add permissions to system roles via UI ("UI prevents
  removing baseline permissions, but new permissions can be added").
  Baseline links missing for any reason are restored; admin extras are
  not overwritten.
"""

import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client

from asset_management.web.services.permissions_enum import (
    PERMISSION_METADATA,
    Permission,
)
from asset_management.web.services.permissions_index_service import (
    PERMISSION_INDEX,
    ROLE_INDEX,
    ROLE_PERMISSION_INDEX,
)

# Stable role ESIDs for the default system roles. Other tables
# (de_authz_role_permission, de_authz_user_team_role,
# de_authz_user_org_role) reference these strings, so once seeded they
# must not change.
ROLE_OWNER = "owner"
ROLE_ORG_ADMIN = "org_admin"
# org-tier read-only role. Mirrors ROLE_VIEWER (team tier)
# at the org tier so an admin can grant org-scoped read-only access
# through the UI. Without this, _AUTHZ_TIER_BY_PREFIX["org_"]'s
# default_role of "viewer" failed _validate_role_for_tier because
# viewer applies_to "team", not "org".
ROLE_ORG_VIEWER = "org_viewer"
ROLE_TEAM_ADMIN = "team_admin"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
# / F8: workspace-tier roles. Backfill (tools/backfill_workspace_memberships.py)
# promotes existing users' highest org role into one of these on the
# Kavanah singleton. Mapping: org_owner → workspace_owner,
# org_admin → workspace_admin, everything else → workspace_member.
ROLE_WORKSPACE_OWNER = "workspace_owner"
ROLE_WORKSPACE_ADMIN = "workspace_admin"
# workspace_viewer is the workspace viewer-tier role. It was renamed
# from "workspace_member" (a teammate's 2026-05-31 note: "Membership in a workspace
# is not the same as being a viewer of all orgs and teams" — this ROLE grants
# read access to every org and team in the workspace). The legacy
# workspace_member name + its alias were dropped in P3b once the gated
# migration (tools/retire_workspace_membership.py) rewrote existing prod role
# docs. Run that migration BEFORE deploying this code to any cluster that
# still holds workspace_member docs.
ROLE_WORKSPACE_VIEWER = "workspace_viewer"

# Owner deliberately uses ``list(Permission)`` so the bundle tracks the
# enum — when later phases add a permission, Owner picks it up on the
# next boot. The other four roles enumerate explicit baselines so their
# bundles stay stable as the enum grows.
DEFAULT_ROLES: dict[str, dict] = {
    ROLE_OWNER: {
        "name": "Owner",
        "description": "All permissions on the organization and all its teams.",
        "applies_to": "org",
        "permissions": list(Permission),
    },
    ROLE_ORG_ADMIN: {
        "name": "Org Admin",
        "description": "Manage organization settings, members, and team rosters.",
        "applies_to": "org",
        "permissions": [
            Permission.ORG_MANAGE,
            Permission.TEAM_MANAGE,
            Permission.USER_INVITE,
            Permission.USER_REMOVE,
            Permission.ROLE_ASSIGNMENT_MANAGE,
            Permission.REPORT_VIEW,
            Permission.REPORT_EDIT,
            # tenant data view is the load-bearing permission
            # for RoleFilterService. Non-superusers carry it via their
            # role assignment; the seeder run at boot adds it
            # idempotently (op_type=create + 409-as-success) so existing
            # users automatically gain it on the next deploy.
            Permission.DB_VIEW_TENANT_DATA,
        ],
    },
    # org-tier read-only role. The seeded catalog had viewer
    # at the team tier but no equivalent at org tier, leaving
    # _AUTHZ_TIER_BY_PREFIX["org_"]'s default_role="viewer" pointed at
    # a role that fails _validate_role_for_tier. This role mirrors
    # ROLE_VIEWER's permission set, scoped to org instead of team.
    ROLE_ORG_VIEWER: {
        "name": "Org Viewer",
        "description": "Read-only access to organization resources.",
        "applies_to": "org",
        "permissions": [
            Permission.REPORT_VIEW,
            Permission.DB_VIEW_TENANT_DATA,
        ],
    },
    ROLE_TEAM_ADMIN: {
        "name": "Team Admin",
        "description": "Manage team settings and members.",
        "applies_to": "team",
        "permissions": [
            Permission.TEAM_MANAGE,
            Permission.USER_INVITE,
            Permission.USER_REMOVE,
            Permission.ROLE_ASSIGNMENT_MANAGE,
            Permission.REPORT_VIEW,
            Permission.REPORT_EDIT,
            Permission.DB_VIEW_TENANT_DATA,
        ],
    },
    ROLE_EDITOR: {
        "name": "Editor",
        "description": "Create and edit team resources.",
        "applies_to": "team",
        "permissions": [
            Permission.REPORT_VIEW,
            Permission.REPORT_EDIT,
            Permission.DB_VIEW_TENANT_DATA,
        ],
    },
    ROLE_VIEWER: {
        "name": "Viewer",
        "description": "Read-only access to team resources.",
        "applies_to": "team",
        "permissions": [
            Permission.REPORT_VIEW,
            Permission.DB_VIEW_TENANT_DATA,
        ],
    },
    # ─────────────────────────────────────────────────────────────────────
    # / F8: workspace-tier roles.
    #
    # workspace_owner uses ``list(Permission)`` like ROLE_OWNER so the
    # bundle tracks the enum — new permissions added in later phases
    # land on workspace_owner automatically. workspace_admin enumerates
    # explicitly so it can shed permissions independently as the enum
    # grows (today: all of workspace_owner minus a future
    # WORKSPACE_DELETE if/when that primitive is introduced).
    # workspace_member is the privacy-floor role for users who can use
    # dashboards but not change anything.
    # ─────────────────────────────────────────────────────────────────────
    ROLE_WORKSPACE_OWNER: {
        "name": "Workspace Owner",
        "description": "All permissions on the workspace and every org/team within.",
        "applies_to": "workspace",
        "permissions": list(Permission),
    },
    ROLE_WORKSPACE_ADMIN: {
        "name": "Workspace Admin",
        "description": "Manage workspace settings + orgs/teams within. Cannot delete the workspace itself.",
        "applies_to": "workspace",
        "permissions": [
            Permission.WORKSPACE_MANAGE,
            Permission.ORG_MANAGE,
            Permission.TEAM_MANAGE,
            Permission.USER_INVITE,
            Permission.USER_REMOVE,
            Permission.ROLE_ASSIGNMENT_MANAGE,
            Permission.REPORT_VIEW,
            Permission.REPORT_EDIT,
            Permission.DB_VIEW_TENANT_DATA,
            # admins may re-authorize connections in their
            # workspace (owner gets it automatically via list(Permission)).
            Permission.CONNECTION_REAUTHORIZE,
        ],
    },
    # (P3b): workspace_member was dropped here once the gated
    # migration rewrote existing role docs to workspace_viewer. Only the
    # canonical viewer role remains.
    ROLE_WORKSPACE_VIEWER: {
        "name": "Workspace Viewer",
        "description": "Read-only access to dashboards and reports for every org and team within the workspace. No CRUD.",
        "applies_to": "workspace",
        "permissions": [
            Permission.REPORT_VIEW,
            Permission.DB_VIEW_TENANT_DATA,
        ],
    },
}


class PermissionsSeederService:
    """Idempotent seeders for the authz tables."""

    @staticmethod
    def seed_permissions() -> None:
        """Write one document per ``Permission`` enum value to
        ``de_authz_permission``. PUT semantics — the enum + metadata
        are the source of truth, so every boot rewrites the doc.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("PermissionsSeederService: could not connect to ES; " "permissions not seeded")
            return

        for permission in Permission:
            meta = PERMISSION_METADATA[permission]
            try:
                es.index(
                    index=PERMISSION_INDEX,
                    id=permission.value,
                    body={
                        "key": permission.value,
                        "resource": meta["resource"],
                        "action": meta["action"],
                        "description": meta["description"],
                    },
                )
            except Exception as e:
                logging.error(
                    "PermissionsSeederService: error seeding permission %s: %s",
                    permission.value,
                    e,
                )

    @staticmethod
    def seed_default_roles() -> None:
        """Write the five default system roles + their baseline
        permission links.

        Role docs (``de_authz_role``) use PUT — canonical metadata is
        rewritten on every boot, with ``created_at`` preserved across
        re-seeds. Role-permission links (``de_authz_role_permission``)
        use ``op_type=create`` so admin-added permissions on system
        roles are not overwritten.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("PermissionsSeederService: could not connect to ES; " "default roles not seeded")
            return

        now_iso = datetime.now(timezone.utc).isoformat()

        for role_esid, spec in DEFAULT_ROLES.items():
            role_doc = {
                "name": spec["name"],
                "description": spec["description"],
                "applies_to": spec["applies_to"],
                "is_system": True,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            # Preserve created_at if the role was seeded on an earlier boot.
            try:
                existing = es.get(index=ROLE_INDEX, id=role_esid)
                existing_created_at = existing.get("_source", {}).get("created_at")
                if existing_created_at:
                    role_doc["created_at"] = existing_created_at
            except Exception:
                pass  # not found — first seed

            try:
                es.index(index=ROLE_INDEX, id=role_esid, body=role_doc)
            except Exception as e:
                logging.error(
                    "PermissionsSeederService: error seeding role %s: %s",
                    role_esid,
                    e,
                )
                continue

            for permission in spec["permissions"]:
                link_id = f"{role_esid}:{permission.value}"
                try:
                    es.index(
                        index=ROLE_PERMISSION_INDEX,
                        id=link_id,
                        body={
                            "role_esid": role_esid,
                            "permission_key": permission.value,
                        },
                        op_type="create",
                    )
                except Exception as e:
                    if "version_conflict_engine_exception" in str(e).lower():
                        continue  # link already exists — desired
                    logging.error(
                        "PermissionsSeederService: error seeding link %s: %s",
                        link_id,
                        e,
                    )
