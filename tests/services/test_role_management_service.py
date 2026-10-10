"""Tests for RoleManagementService."""
import os
import sys
import logging
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.services.role_management_service import (
    RoleManagementService,
    _SYSTEM_ROLE_NAMES,
    _SYSTEM_ROLE_PREFIXES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_es():
    return mock.MagicMock()


# ---------------------------------------------------------------------------
# get_all_role_names
# ---------------------------------------------------------------------------

class TestGetAllRoleNames:
    def test_returns_non_system_roles(self, mock_es):
        mock_es.security.get_role.return_value = {
            "org_VOGZ": {},
            "team_NFCY": {},
            "kibana_system": {},
            "superuser": {},
            "elastic": {},
        }
        with mock.patch(
            "asset_management.web.services.role_management_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RoleManagementService.get_all_role_names()

        assert "org_VOGZ" in result
        assert "team_NFCY" in result
        assert "kibana_system" not in result
        assert "superuser" not in result
        assert "elastic" not in result

    def test_filters_system_prefix_roles(self, mock_es):
        mock_es.security.get_role.return_value = {
            "kibana_admin": {},
            "logstash_writer": {},
            "beats_agent": {},
            "apm_reader": {},
            "custom_role": {},
        }
        with mock.patch(
            "asset_management.web.services.role_management_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RoleManagementService.get_all_role_names()

        assert "custom_role" in result
        assert "kibana_admin" not in result
        assert "logstash_writer" not in result

    def test_filters_km_infra_platform_roles(self, mock_es):
        # km-infra creates SIEM/security roles directly on the
        # cluster (km_siem_security_reader). They must be excluded from
        # our role listing, the same way kibana_/monitoring_/fleet_ are.
        mock_es.security.get_role.return_value = {
            "km_siem_security_reader": {},
            "km_monitoring_writer": {},
            "org_VOGZ": {},
        }
        with mock.patch(
            "asset_management.web.services.role_management_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RoleManagementService.get_all_role_names()

        assert "org_VOGZ" in result
        assert "km_siem_security_reader" not in result
        assert "km_monitoring_writer" not in result

    def test_returns_empty_set_when_es_none(self):
        with mock.patch(
            "asset_management.web.services.role_management_service.get_elasticsearch_client",
            return_value=None,
        ):
            result = RoleManagementService.get_all_role_names()

        assert result == set()

    def test_returns_empty_set_on_exception(self, mock_es):
        mock_es.security.get_role.side_effect = Exception("ES error")
        with mock.patch(
            "asset_management.web.services.role_management_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RoleManagementService.get_all_role_names()

        assert result == set()

    def test_returns_empty_set_when_no_custom_roles(self, mock_es):
        mock_es.security.get_role.return_value = {
            "superuser": {},
            "kibana_system": {},
        }
        with mock.patch(
            "asset_management.web.services.role_management_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RoleManagementService.get_all_role_names()

        assert result == set()


# TestCreateOrgRole / TestPutRole / TestSyncAllRoles /
# TestGetRoleSyncStatus deleted with the legacy ES Security role system.
# _put_role inlined into RolePermissionsService.create_role —
# test_creates_system_role + test_create_role_uses_kibana_privileges in
# test_role_permissions_service.py cover the put_role body shape.

