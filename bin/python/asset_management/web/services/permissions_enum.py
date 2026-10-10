"""
Permission Enum

Closed, type-checked source of truth for atomic permissions in the new
authz model. The seeder writes one document per enum value to the
``de_authz_permission`` index; nothing else consumes the enum during
Phase 1.

Phase 2+ (backfill, shadow, cutover) will add resolver wiring and call
sites; do not introduce that wiring here.
"""

from enum import StrEnum
from typing import Final


class Permission(StrEnum):
    """Atomic permissions in the new authz model.

    Naming: ``<resource>.<action>``. Adding a new permission means
    adding the enum member and a metadata entry below; the import-time
    check enforces that pairing.
    """

    REPORT_VIEW = "report.view"
    REPORT_EDIT = "report.edit"
    USER_INVITE = "user.invite"
    USER_REMOVE = "user.remove"
    TEAM_MANAGE = "team.manage"
    ORG_MANAGE = "org.manage"
    # / F8: workspace-tier management (settings, branding,
    # workspace-level invites). Distinct from ORG_MANAGE so a future
    # workspace_admin role can manage workspace settings without
    # implicitly gaining org-level CRUD.
    WORKSPACE_MANAGE = "workspace.manage"
    # follow-up (2026-06-02): the destructive workspace-delete verb,
    # long noted as "future" in WORKSPACE_MANAGE's metadata. It is the ONLY
    # permission workspace_owner holds that workspace_admin does not — owner's
    # bundle is list(Permission) so it auto-includes this; admin's explicit
    # bundle omits it. Used to gate owner-only surfaces (User Management).
    WORKSPACE_DELETE = "workspace.delete"
    ROLE_ASSIGNMENT_MANAGE = "role_assignment.manage"
    DB_VIEW_TENANT_DATA = "db.view_tenant_data"
    # re-authorize a data connection (commit a fresh OAuth token
    # onto an existing de_connection row). Granted to workspace_owner
    # (automatic via list(Permission)) + workspace_admin (explicit bundle).
    # Always checked WORKSPACE-SCOPED — re-auth overwrites a live
    # credential, so "holds it anywhere" is never sufficient.
    CONNECTION_REAUTHORIZE = "connection.reauthorize"


PERMISSION_METADATA: Final[dict[Permission, dict[str, str]]] = {
    Permission.REPORT_VIEW: {
        "resource": "report",
        "action": "view",
        "description": "View reports and dashboards within scope.",
    },
    Permission.REPORT_EDIT: {
        "resource": "report",
        "action": "edit",
        "description": "Create and edit reports and dashboards within scope.",
    },
    Permission.USER_INVITE: {
        "resource": "user",
        "action": "invite",
        "description": "Invite a user to a team or organization within scope.",
    },
    Permission.USER_REMOVE: {
        "resource": "user",
        "action": "remove",
        "description": "Remove a user from a team or organization within scope.",
    },
    Permission.TEAM_MANAGE: {
        "resource": "team",
        "action": "manage",
        "description": "Edit team settings and metadata within scope.",
    },
    Permission.ORG_MANAGE: {
        "resource": "org",
        "action": "manage",
        "description": "Edit organization settings and metadata within scope.",
    },
    Permission.WORKSPACE_MANAGE: {
        "resource": "workspace",
        "action": "manage",
        # / F8: granted to workspace_owner + workspace_admin.
        # Covers settings, branding, slug rename, and workspace-level
        # member invites. Workspace delete is intentionally NOT covered
        # by this permission — a future WORKSPACE_DELETE primitive can
        # carry that destructive verb without re-granting it to admins.
        "description": "Edit workspace settings, branding, and workspace-level members.",
    },
    Permission.WORKSPACE_DELETE: {
        "resource": "workspace",
        "action": "delete",
        # follow-up: owner-only. Distinguishes workspace_owner
        # (list(Permission) → has it) from workspace_admin (explicit bundle →
        # lacks it). Gates owner-only surfaces such as User Management.
        "description": "Delete the workspace itself. Owner-only destructive verb.",
    },
    Permission.ROLE_ASSIGNMENT_MANAGE: {
        "resource": "role_assignment",
        "action": "manage",
        "description": "Assign and unassign roles to users within scope.",
    },
    Permission.DB_VIEW_TENANT_DATA: {
        "resource": "db",
        "action": "view_tenant_data",
        # / F1: load-bearing privacy permission. Granted to system
        # roles (Owner, Org Admin, Team Admin, Editor, Viewer) so existing
        # users keep their scoped data access. NOT granted to ``superuser``
        # or ``asset_admin`` — their data access comes from explicit
        # ``de_authz_data_grant`` records instead. This is what makes the
        # privacy boundary enforceable: a bare superuser holds no scope.
        "description": "View tenant business data (rows, dashboards). Required for any data query through RoleFilterService.",
    },
    Permission.CONNECTION_REAUTHORIZE: {
        "resource": "connection",
        "action": "reauthorize",
        # replaces the bespoke role plumbing in the
        # Tier 1 gate. KM staff reach it via the superuser short-circuit —
        # never via per-workspace admin rows.
        "description": "Re-authorize a data connection in the workspace (commit a fresh OAuth token onto an existing connection).",
    },
}

# Fail loudly at import time if the enum and metadata drift apart —
# easier to debug than a silent partial seed.
_missing_metadata = set(Permission) - PERMISSION_METADATA.keys()
if _missing_metadata:
    raise RuntimeError("Permission enum members without metadata: " f"{sorted(p.value for p in _missing_metadata)}")
_extra_metadata = PERMISSION_METADATA.keys() - set(Permission)
if _extra_metadata:
    raise RuntimeError(
        "PERMISSION_METADATA has keys that are not Permission members: " f"{sorted(str(p) for p in _extra_metadata)}"
    )

ALL_PERMISSIONS: Final[frozenset[Permission]] = frozenset(Permission)
