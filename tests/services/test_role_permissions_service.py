"""Tests for RolePermissionsService."""
import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.services.role_permissions_service import (
    RolePermissionsService,
    _DEFAULTS,
    _PERM_FIELDS,
    _PROTECTED_ROLES,
    _perm_cache,
    INDEX,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the permission cache before and after each test."""
    _perm_cache.clear()
    yield
    _perm_cache.clear()


@pytest.fixture
def mock_es():
    return mock.MagicMock()


# ---------------------------------------------------------------------------
# ensure_index_exists
# ---------------------------------------------------------------------------

class TestEnsureIndexExists:
    def test_creates_index_and_template(self, mock_es):
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ), mock.patch(
            "asset_management.web.services.role_permissions_service.put_regular_index_template"
        ) as mock_put_template, mock.patch(
            "asset_management.web.services.role_permissions_service.create_index_if_not_exists"
        ) as mock_create:
            RolePermissionsService.ensure_index_exists()

        mock_put_template.assert_called_once()
        mock_create.assert_called_once()
        mock_es.indices.put_mapping.assert_called_once()

    def test_handles_es_unavailable(self):
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=None,
        ):
            # Should not raise
            RolePermissionsService.ensure_index_exists()


# ---------------------------------------------------------------------------
# seed_defaults
# ---------------------------------------------------------------------------

class TestSeedDefaults:
    def test_creates_docs_for_system_roles(self, mock_es):
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            RolePermissionsService.seed_defaults()

        assert mock_es.index.call_count == len(_DEFAULTS)
        created_roles = {
            call.kwargs["id"] for call in mock_es.index.call_args_list
        }
        assert created_roles == set(_DEFAULTS.keys())
        for call in mock_es.index.call_args_list:
            assert call.kwargs.get("op_type") == "create"

    def test_idempotent_skips_existing(self, mock_es):
        """If a doc already exists, the version_conflict is silently ignored."""
        mock_es.index.side_effect = Exception("version_conflict_engine_exception")
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            # Should not raise
            RolePermissionsService.seed_defaults()

    def test_handles_es_unavailable(self):
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=None,
        ):
            RolePermissionsService.seed_defaults()


# ---------------------------------------------------------------------------
# get_permissions
# ---------------------------------------------------------------------------

class TestGetPermissions:
    def test_returns_stored_doc(self, mock_es):
        mock_es.get.return_value = {
            "_source": {
                "role_name": "org_VOGZ",
                "mc_asset_explorer": True,
                "mc_data_explorer": False,
            }
        }
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.get_permissions("org_VOGZ")

        assert result["role_name"] == "org_VOGZ"
        assert result["mc_asset_explorer"] is True

    def test_returns_defaults_for_unknown_role(self, mock_es):
        mock_es.get.side_effect = Exception("not found")
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.get_permissions("org_NewOrg")

        assert result["role_name"] == "org_NewOrg"
        assert result["role_type"] == "org"
        # org roles default to all False except hdr_dashboards_link
        assert result["mc_asset_explorer"] is False
        assert result["hdr_dashboards_link"] is True

    def test_returns_system_defaults_for_known_role(self, mock_es):
        mock_es.get.side_effect = Exception("not found")
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.get_permissions("superuser")

        assert result["role_name"] == "superuser"
        assert result["mc_asset_explorer"] is True
        assert result["hdr_elastic_link"] is True

    def test_backfills_missing_field_for_system_role_from_defaults(self, mock_es):
        """A stored doc that predates a new permission field should still
        return the default value for that field (from _DEFAULTS)."""
        mock_es.get.return_value = {
            "_source": {
                "role_name": "superuser",
                "is_locked": True,
                # mc_data_connectors deliberately absent — simulates a doc
                # written before the field existed.
                "mc_asset_explorer": True,
            }
        }
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.get_permissions("superuser")

        # Backfilled from _DEFAULTS["superuser"]
        assert result["mc_data_connectors"] is True
        # Untouched from the stored doc
        assert result["mc_asset_explorer"] is True

    def test_backfills_missing_field_for_custom_role_as_false(self, mock_es):
        """A custom role's stored doc missing a field should default to False
        (custom roles aren't in _DEFAULTS)."""
        mock_es.get.return_value = {
            "_source": {
                "role_name": "org_VOGZ",
                # mc_data_connectors deliberately absent
                "mc_asset_explorer": True,
            }
        }
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.get_permissions("org_VOGZ")

        assert result["mc_data_connectors"] is False
        assert result["mc_asset_explorer"] is True


# ---------------------------------------------------------------------------
# get_merged_permissions
# ---------------------------------------------------------------------------

class TestGetMergedPermissions:
    """(Phase 5 cleanup) replaced the per-role ES lookup with a
    deterministic three-case map: admin -> all True, data_explorer ->
    Data Explorer carve-out, anything else -> all False. The legacy
    OR-merge + ES cache are gone."""

    def test_admin_role_returns_all_true_except_grants_management(self):
        """asset_admin sees every admin surface EXCEPT Data
        Grants — cross-tenant data-access exceptions are a Kavanah-staff
        (superuser) decision. The /api/admin/grants endpoints enforce
        the same boundary via _require_superuser; this carve-out keeps
        the sidebar/modal/route gates consistent.

         (2026-07-28): mc_ai_assistant joins the exclusion — the
        AI Assistant is spend-metered, so asset_admin needs the explicit
        ai_assistant role like everyone else.

        the Elastic link (hdr_elastic_link / db_elastic) is
        excluded too — the /kibana gate admits only superuser."""
        result = RolePermissionsService.get_merged_permissions(["asset_admin"])
        assert result["mc_grants_management"] is False, "asset_admin must NOT manage grants"
        assert result["mc_ai_assistant"] is False, "asset_admin must NOT inherit the AI Assistant"
        assert result["hdr_elastic_link"] is False, "asset_admin must NOT see the Elastic link"
        assert result["db_elastic"] is False, "asset_admin must NOT see the Elastic link"
        for field in _PERM_FIELDS:
            if field in ("mc_grants_management", "mc_ai_assistant", "hdr_elastic_link", "db_elastic"):
                continue
            assert result[field] is True, f"asset_admin must have {field}=True"

    def test_superuser_returns_all_true(self):
        result = RolePermissionsService.get_merged_permissions(["superuser"])
        for field in _PERM_FIELDS:
            assert result[field] is True

    def test_data_explorer_role_only_grants_data_explorer_carveout(self):
        result = RolePermissionsService.get_merged_permissions(["data_explorer"])
        assert result["db_data_explorer"] is True
        assert result["hdr_dashboards_link"] is True
        # Every other flag must be False.
        for field in _PERM_FIELDS:
            if field in ("db_data_explorer", "hdr_dashboards_link"):
                continue
            assert result[field] is False, f"data_explorer must NOT grant {field}"

    def test_scoped_role_returns_all_false(self):
        """org_*/team_*/custom non-admin roles get nothing."""
        result = RolePermissionsService.get_merged_permissions(["org_VOGZ", "team_NFCY"])
        for field in _PERM_FIELDS:
            assert result[field] is False

    def test_admin_overrides_data_explorer(self):
        """A user with both asset_admin and data_explorer gets the
        asset_admin treatment — all True except
        mc_grants_management carve-out spend-metered
        mc_ai_assistant exclusion."""
        result = RolePermissionsService.get_merged_permissions(["data_explorer", "asset_admin"])
        assert result["mc_grants_management"] is False
        assert result["mc_ai_assistant"] is False
        assert result["hdr_elastic_link"] is False
        assert result["db_elastic"] is False
        for field in _PERM_FIELDS:
            if field in ("mc_grants_management", "mc_ai_assistant", "hdr_elastic_link", "db_elastic"):
                continue
            assert result[field] is True

    def test_empty_roles_returns_all_false(self):
        result = RolePermissionsService.get_merged_permissions([])
        for field in _PERM_FIELDS:
            assert result[field] is False

    def test_does_not_query_es(self, mock_es):
        """the deterministic map means ES is never consulted."""
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            RolePermissionsService.get_merged_permissions(["asset_admin"])
            RolePermissionsService.get_merged_permissions(["data_explorer"])
            RolePermissionsService.get_merged_permissions(["org_X"])

        assert mock_es.get.call_count == 0, (
            "get_merged_permissions must not query ES earlier — the "
            "deterministic map replaces the per-role ES lookup"
        )


# ---------------------------------------------------------------------------
# update_permissions
# ---------------------------------------------------------------------------

class TestUpdatePermissions:
    def test_rejects_locked_superuser(self, mock_es):
        mock_es.get.return_value = {"_source": {"is_locked": True, "role_name": "superuser"}}
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.update_permissions(
                "superuser", {"mc_testing": False}, "admin"
            )

        assert result["success"] is False
        assert "cannot be modified" in result["error"]

    def test_forces_elastic_false_for_non_superuser(self, mock_es):
        mock_es.get.side_effect = Exception("not found")
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.update_permissions(
                "asset_admin",
                {
                    "hdr_elastic_link": True,
                    "db_elastic": True,
                    "mc_testing": True,
                    "db_management_console": True,
                },
                "admin",
            )

        assert result["success"] is True
        saved_doc = mock_es.index.call_args.kwargs["body"]
        assert saved_doc["hdr_elastic_link"] is False
        assert saved_doc["db_elastic"] is False
        assert saved_doc["mc_testing"] is True

    def test_forces_mc_false_when_db_management_console_unchecked(self, mock_es):
        """mc_* fields require db_management_console; gated regardless of role type."""
        mock_es.get.side_effect = Exception("not found")
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.update_permissions(
                "asset_admin",
                {
                    "mc_asset_explorer": True,
                    "mc_testing": True,
                    "hdr_dashboards_link": True,
                    "db_management_console": False,
                },
                "admin",
            )

        assert result["success"] is True
        saved_doc = mock_es.index.call_args.kwargs["body"]
        assert saved_doc["mc_asset_explorer"] is False
        assert saved_doc["mc_testing"] is False
        assert saved_doc["hdr_dashboards_link"] is True

    def test_allows_mc_for_org_role_when_db_management_console_checked(self, mock_es):
        """Org roles can have mc_* when db_management_console is True."""
        mock_es.get.side_effect = Exception("not found")
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.update_permissions(
                "org_VOGZ",
                {
                    "mc_asset_explorer": True,
                    "db_management_console": True,
                },
                "admin",
            )

        assert result["success"] is True
        saved_doc = mock_es.index.call_args.kwargs["body"]
        assert saved_doc["mc_asset_explorer"] is True
        assert saved_doc["db_management_console"] is True

    def test_clears_cache_on_update(self, mock_es):
        _perm_cache[frozenset(["test"])] = ({"mc_testing": True}, 999999999)
        mock_es.get.side_effect = Exception("not found")
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            RolePermissionsService.update_permissions("asset_admin", {}, "admin")

        assert len(_perm_cache) == 0


# ---------------------------------------------------------------------------
# can_delete_role
# ---------------------------------------------------------------------------

class TestCanDeleteRole:
    def test_blocks_system_roles(self):
        for role in _PROTECTED_ROLES:
            can, reason = RolePermissionsService.can_delete_role(role)
            assert can is False
            assert "System role" in reason

    def test_blocks_assigned_roles(self):
        can, reason = RolePermissionsService.can_delete_role("org_VOGZ", user_count=3)
        assert can is False
        assert "3 user(s)" in reason

    def test_allows_unassigned_org_team_roles(self):
        can, reason = RolePermissionsService.can_delete_role("org_VOGZ", user_count=0)
        assert can is True
        assert reason == ""

        can, reason = RolePermissionsService.can_delete_role("team_NFCY", user_count=0)
        assert can is True


# ---------------------------------------------------------------------------
# delete_role
# ---------------------------------------------------------------------------

class TestDeleteRole:
    def test_deletes_es_role_and_permissions(self, mock_es):
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            # No users assigned
            mock_es.security.get_user.return_value = {}
            result = RolePermissionsService.delete_role("org_VOGZ")

        assert result["success"] is True
        mock_es.security.delete_role.assert_called_once_with(name="org_VOGZ")
        mock_es.delete.assert_called_once_with(index=INDEX, id="org_VOGZ")

    def test_rejects_system_role(self, mock_es):
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.delete_role("superuser")

        assert result["success"] is False
        assert "System role" in result["error"]
        mock_es.security.delete_role.assert_not_called()

    def test_rejects_assigned_role(self, mock_es):
        mock_es.security.get_user.return_value = {
            "jsmith": {"roles": ["org_VOGZ"], "metadata": {}}
        }
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=mock_es,
        ):
            result = RolePermissionsService.delete_role("org_VOGZ")

        assert result["success"] is False
        assert "1 user(s)" in result["error"]


# ---------------------------------------------------------------------------
# _classify_role
# ---------------------------------------------------------------------------

class TestClassifyRole:
    def test_system_roles(self):
        assert RolePermissionsService._classify_role("superuser") == "system"
        assert RolePermissionsService._classify_role("asset_admin") == "system"
        assert RolePermissionsService._classify_role("viewer") == "system"

    def test_org_roles(self):
        assert RolePermissionsService._classify_role("org_VOGZ") == "org"
        assert RolePermissionsService._classify_role("org_Kavanah") == "org"

    def test_team_roles(self):
        assert RolePermissionsService._classify_role("team_NFCY") == "team"
        assert RolePermissionsService._classify_role("team_FLKVT") == "team"


# ---------------------------------------------------------------------------
# create_role
# ---------------------------------------------------------------------------

class TestCreateRole:
    """create_role validates naming, creates the ES security role, and writes perms."""

    def _mock_es_no_conflicts(self):
        """Return a mock ES client where the role and permissions doc don't exist."""
        m = mock.MagicMock()
        m.security.get_role.return_value = {}
        m.get.side_effect = Exception("not found")
        return m

    def test_creates_system_role(self):
        es = self._mock_es_no_conflicts()
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ), mock.patch(
            "asset_management.web.services.role_management_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="auditor",
                role_type="system",
                settings={"mc_asset_explorer": True, "hdr_dashboards_link": True},
                created_by="admin",
            )
        assert result["success"] is True
        assert result["role_name"] == "auditor"
        es.security.put_role.assert_called_once()
        es.index.assert_called_once()

    def test_create_role_uses_kibana_dashboard_privileges(self):
        """Inlined _put_role: put_role body carries the kibana-.kibana app + dashboard/Discover read."""
        es = self._mock_es_no_conflicts()
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ):
            RolePermissionsService.create_role(
                role_name="auditor",
                role_type="system",
                settings={},
                created_by="admin",
            )

        body = es.security.put_role.call_args.kwargs["body"]
        assert body["applications"][0]["application"] == "kibana-.kibana"
        assert "feature_dashboard.read" in body["applications"][0]["privileges"]
        assert "feature_discover.read" in body["applications"][0]["privileges"]
        assert body["applications"][0]["resources"] == ["space:default"]

    def test_create_role_surfaces_es_put_role_error(self):
        """Inlined _put_role: if es.security.put_role raises, create_role returns success=False."""
        es = self._mock_es_no_conflicts()
        es.security.put_role.side_effect = Exception("Permission denied")
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="auditor",
                role_type="system",
                settings={},
                created_by="admin",
            )
        assert result["success"] is False
        # a constant message; the ES exception text stays in the log.
        assert result["error"] == "Could not create ES security role."
        assert "Permission denied" not in result["error"]
        # No permissions doc should have been written if the ES role create failed.
        es.index.assert_not_called()

    def test_creates_org_role_with_prefix(self):
        es = self._mock_es_no_conflicts()
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ), mock.patch(
            "asset_management.web.services.role_management_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="org_Acme",
                role_type="org",
                settings={"hdr_dashboards_link": True},
                created_by="admin",
            )
        assert result["success"] is True

    def test_rejects_org_role_without_prefix(self):
        es = self._mock_es_no_conflicts()
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="Acme",
                role_type="org",
                settings={},
                created_by="admin",
            )
        assert result["success"] is False
        assert "org_" in result["error"]

    def test_rejects_team_role_without_prefix(self):
        es = self._mock_es_no_conflicts()
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="NFCY",
                role_type="team",
                settings={},
                created_by="admin",
            )
        assert result["success"] is False
        assert "team_" in result["error"]

    def test_rejects_system_role_with_org_prefix(self):
        es = self._mock_es_no_conflicts()
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="org_thing",
                role_type="system",
                settings={},
                created_by="admin",
            )
        assert result["success"] is False

    def test_rejects_reserved_built_in_name(self):
        es = self._mock_es_no_conflicts()
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="kibana_admin",
                role_type="system",
                settings={},
                created_by="admin",
            )
        assert result["success"] is False
        assert "reserved" in result["error"].lower()

    def test_rejects_existing_role(self):
        es = mock.MagicMock()
        es.security.get_role.return_value = {"auditor": {}}
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="auditor",
                role_type="system",
                settings={},
                created_by="admin",
            )
        assert result["success"] is False
        assert "already exists" in result["error"]

    def test_rejects_empty_name(self):
        result = RolePermissionsService.create_role(
            role_name="",
            role_type="system",
            settings={},
            created_by="admin",
        )
        assert result["success"] is False

    def test_rejects_invalid_type(self):
        result = RolePermissionsService.create_role(
            role_name="foo",
            role_type="bogus",
            settings={},
            created_by="admin",
        )
        assert result["success"] is False

    def test_rejects_invalid_chars(self):
        es = self._mock_es_no_conflicts()
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="bad name!",
                role_type="system",
                settings={},
                created_by="admin",
            )
        assert result["success"] is False

    def test_mc_fields_require_db_management_console(self):
        """mc_* fields should be forced False when db_management_console is False."""
        es = self._mock_es_no_conflicts()
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ), mock.patch(
            "asset_management.web.services.role_management_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="auditor",
                role_type="system",
                settings={
                    "mc_asset_explorer": True,
                    "hdr_dashboards_link": True,
                    "db_management_console": False,
                },
                created_by="admin",
            )
        assert result["success"] is True
        written_doc = es.index.call_args[1]["body"]
        assert written_doc["mc_asset_explorer"] is False
        assert written_doc["hdr_dashboards_link"] is True

    def test_mc_fields_allowed_for_org_when_db_management_console_checked(self):
        """Org/team roles can have mc_* when db_management_console is True."""
        es = self._mock_es_no_conflicts()
        with mock.patch(
            "asset_management.web.services.role_permissions_service.get_elasticsearch_client",
            return_value=es,
        ), mock.patch(
            "asset_management.web.services.role_management_service.get_elasticsearch_client",
            return_value=es,
        ):
            result = RolePermissionsService.create_role(
                role_name="org_Acme",
                role_type="org",
                settings={
                    "mc_asset_explorer": True,
                    "db_management_console": True,
                },
                created_by="admin",
            )
        assert result["success"] is True
        written_doc = es.index.call_args[1]["body"]
        assert written_doc["mc_asset_explorer"] is True
        assert written_doc["db_management_console"] is True
