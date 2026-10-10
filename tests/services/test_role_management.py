"""
Tests for the Role Management Service.

Covers:
  get_all_role_names   — filters system roles, ES unavailable, exception
  create_org_role      — creates role, already exists, ES unavailable
  create_team_role     — creates role
  _put_role            — role creation logic, ES error
  sync_all_roles       — creates from orgs and teams, handles errors
  get_role_sync_status — identifies missing roles
"""

from unittest import mock

import pytest

from asset_management.web.services.role_management_service import RoleManagementService


class TestGetAllRoleNames:

    def test_returns_non_system_roles(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_role.return_value = {
            "org_VOGZ": {}, "team_NFCY": {}, "custom_role": {},
            "elastic": {}, "superuser": {}, "kibana_system": {},
            ".internal_role": {},
        }
        with mock.patch("asset_management.web.services.role_management_service.get_elasticsearch_client", return_value=mock_es):
            result = RoleManagementService.get_all_role_names()
        assert "org_VOGZ" in result
        assert "team_NFCY" in result
        assert "custom_role" in result
        assert "elastic" not in result
        assert "superuser" not in result
        assert ".internal_role" not in result

    def test_es_unavailable(self):
        with mock.patch("asset_management.web.services.role_management_service.get_elasticsearch_client", return_value=None):
            result = RoleManagementService.get_all_role_names()
        assert result == set()

    def test_exception(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_role.side_effect = Exception("error")
        with mock.patch("asset_management.web.services.role_management_service.get_elasticsearch_client", return_value=mock_es):
            result = RoleManagementService.get_all_role_names()
        assert result == set()


# TestCreateRoles deleted with create_org_role/create_team_role.

# TestSyncAllRoles deleted with sync_all_roles.

# TestGetRoleSyncStatus deleted with get_role_sync_status.
