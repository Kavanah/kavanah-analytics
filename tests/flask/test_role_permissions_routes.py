"""
Tests for role permissions Flask routes.

Covers:
- GET /api/user-management/roles — list all roles with metadata
- GET /api/user-management/role-permissions/<role_name> — get settings
- POST /api/user-management/role-permissions/<role_name> — update settings
- DELETE /api/user-management/delete-role/<role_name> — delete a role
"""

from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOCK_ROLES = [
    {
        "role_name": "superuser",
        "role_type": "system",
        "is_locked": True,
        "user_count": 1,
        "can_delete": False,
        "delete_reason": "System role cannot be deleted",
    },
    {
        "role_name": "org_VOGZ",
        "role_type": "org",
        "is_locked": False,
        "user_count": 2,
        "can_delete": False,
        "delete_reason": "Role is assigned to 2 user(s)",
    },
    {
        "role_name": "team_NFCY",
        "role_type": "team",
        "is_locked": False,
        "user_count": 0,
        "can_delete": True,
        "delete_reason": "",
    },
]

_MOCK_PERMS = {
    "role_name": "org_VOGZ",
    "role_type": "org",
    "is_locked": False,
    "mc_asset_explorer": False,
    "mc_user_management": False,
    "hdr_dashboards_link": True,
}


# ---------------------------------------------------------------------------
# GET /api/user-management/roles
# ---------------------------------------------------------------------------

class TestListRoles:
    def test_admin_sees_only_non_system_roles(self, auth_client):
        """asset_admin should not see system roles (superuser-only)."""
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.get_roles_for_listing",
            return_value=_MOCK_ROLES,
        ):
            resp = auth_client.get("/api/user-management/roles")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "roles" in data
        # System roles (superuser) are filtered out for non-superusers
        assert len(data["roles"]) == 2
        role_names = {r["role_name"] for r in data["roles"]}
        assert "superuser" not in role_names

    def test_superuser_sees_all_roles(self, superuser_client):
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.get_roles_for_listing",
            return_value=_MOCK_ROLES,
        ):
            resp = superuser_client.get("/api/user-management/roles")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["roles"]) == 3

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = org_user_client.get("/api/user-management/roles")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/user-management/role-permissions/<role_name>
# ---------------------------------------------------------------------------

class TestGetRolePermissions:
    def test_returns_permissions(self, auth_client):
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.get_permissions",
            return_value=_MOCK_PERMS,
        ):
            resp = auth_client.get("/api/user-management/role-permissions/org_VOGZ")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["role_name"] == "org_VOGZ"
        assert data["hdr_dashboards_link"] is True

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = org_user_client.get("/api/user-management/role-permissions/org_VOGZ")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/user-management/role-permissions/<role_name>
# ---------------------------------------------------------------------------

class TestUpdateRolePermissions:
    # role writes are superuser-only (roles are cluster-wide), so
    # the success-path cases run as a superuser rather than an asset_admin.
    def test_saves_settings(self, superuser_client):
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.update_permissions",
            return_value={"success": True},
        ) as mock_update, mock.patch(
            "asset_management.web.app.AuditLogService.log_event",
        ):
            resp = superuser_client.post(
                "/api/user-management/role-permissions/org_VOGZ",
                json={"mc_asset_explorer": True, "hdr_dashboards_link": True},
            )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_update.assert_called_once()

    def test_returns_error_for_superuser_role(self, superuser_client):
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.update_permissions",
            return_value={"success": False, "error": "Superuser permissions cannot be modified"},
        ):
            resp = superuser_client.post(
                "/api/user-management/role-permissions/superuser",
                json={"mc_testing": False},
            )
        data = resp.get_json()
        assert data["success"] is False
        assert "cannot be modified" in data["error"]


# ---------------------------------------------------------------------------
# DELETE /api/user-management/delete-role/<role_name>
# ---------------------------------------------------------------------------

class TestDeleteRole:
    # superuser-only, like create-role — see TestUpdateRolePermissions.
    def test_deletes_successfully(self, superuser_client):
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.delete_role",
            return_value={"success": True},
        ) as mock_delete, mock.patch(
            "asset_management.web.app.AuditLogService.log_event",
        ):
            resp = superuser_client.delete("/api/user-management/delete-role/team_NFCY")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_delete.assert_called_once_with("team_NFCY")

    def test_returns_error_for_assigned_role(self, superuser_client):
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.delete_role",
            return_value={"success": False, "error": "Role is assigned to 2 user(s)"},
        ):
            resp = superuser_client.delete("/api/user-management/delete-role/org_VOGZ")
        data = resp.get_json()
        assert data["success"] is False
        assert "assigned" in data["error"]

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = org_user_client.delete("/api/user-management/delete-role/team_NFCY")
        assert resp.status_code == 403
