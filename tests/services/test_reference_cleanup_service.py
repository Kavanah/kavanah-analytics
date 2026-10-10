"""
Tests for the Reference Cleanup Service.

Covers:
  _build_reverse_map     — correct inversion of ASSET_CONFIGS relationships
  get_dependents         — counting affected children, ES unavailable
  cleanup_references     — scalar field cleanup, array field cleanup, error handling
  cleanup_references_bulk — terms query, bulk array cleanup, empty input
"""

from unittest import mock

import pytest

from asset_management.web.services.reference_cleanup_service import (
    ReferenceCleanupService,
    _build_reverse_map,
    REVERSE_MAP,
)

# The conftest mock_es patches lib.es_helpers.get_elasticsearch_client, but
# reference_cleanup_service binds its own local reference at import time.
# We need to patch the reference where it's *used*, not where it's *defined*.
_RCS_ES_PATH = "asset_management.web.services.reference_cleanup_service.get_elasticsearch_client"


@pytest.fixture
def rcs_mock_es():
    """Yield a MagicMock Elasticsearch client, patched into reference_cleanup_service."""
    with mock.patch(_RCS_ES_PATH) as factory:
        client = mock.MagicMock()
        factory.return_value = client
        yield client


@pytest.fixture
def rcs_mock_es_none():
    """Simulate ES unavailable for reference_cleanup_service."""
    with mock.patch(_RCS_ES_PATH, return_value=None):
        yield


class TestBuildReverseMap:
    """Verify the reverse map is built correctly from ASSET_CONFIGS."""

    def test_reverse_map_is_not_empty(self):
        assert len(REVERSE_MAP) > 0

    def test_fb_accounts_has_expected_children(self):
        """Deleting a Facebook Account should affect fb_apps, fb_pages, ig_accounts."""
        entries = REVERSE_MAP.get("de_asset_fb_accounts", [])
        child_indices = {e["child_index"] for e in entries}
        assert "de_asset_fb_apps" in child_indices
        assert "de_asset_fb_pages" in child_indices
        assert "de_asset_ig_accounts" in child_indices

    def test_fb_accounts_children_are_scalar(self):
        """fb_account_esid is a select field, so is_array should be False."""
        entries = REVERSE_MAP.get("de_asset_fb_accounts", [])
        for entry in entries:
            if entry["esid_field"] == "fb_account_esid":
                assert entry["is_array"] is False

    def test_organization_has_teams_and_dashboards(self):
        """Deleting an Organization should affect teams and echarts_dashboards."""
        entries = REVERSE_MAP.get("de_asset_organization", [])
        child_indices = {e["child_index"] for e in entries}
        assert "de_asset_teams" in child_indices
        assert "de_asset_echarts_dashboards" in child_indices

    def test_organization_esid_is_array(self):
        """organization_esid on teams is multi_select, so is_array should be True."""
        entries = REVERSE_MAP.get("de_asset_organization", [])
        for entry in entries:
            if entry["child_index"] == "de_asset_teams":
                assert entry["is_array"] is True
                assert entry["esid_field"] == "organization_esid"
                assert entry["title_field"] == "organization_title"

    def test_teams_has_dashboards(self):
        """Deleting a Team should affect echarts_dashboards."""
        entries = REVERSE_MAP.get("de_asset_teams", [])
        child_indices = {e["child_index"] for e in entries}
        assert "de_asset_echarts_dashboards" in child_indices

    def test_cities_absent_after(self):
        """removed de_asset_cities — team_city is now a plain text field,
        so the reverse-map must NOT carry an entry pointing back at teams."""
        assert "de_asset_cities" not in REVERSE_MAP

    def test_fb_apps_has_ad_accounts_and_app_users(self):
        """Deleting a Facebook App should affect ad_accounts and app_users."""
        entries = REVERSE_MAP.get("de_asset_fb_apps", [])
        child_indices = {e["child_index"] for e in entries}
        assert "de_asset_fb_ad_accounts" in child_indices
        assert "de_asset_fb_app_users" in child_indices

    def test_asset_with_no_children_absent(self):
        """An asset type that nothing references should not appear in the map."""
        # Countries are only referenced, never a source that needs no cleanup
        # Actually countries IS a source — teams reference them.
        # Let's check that an index not in ASSET_CONFIGS source fields is absent.
        assert "de_asset_nonexistent" not in REVERSE_MAP

    def test_each_entry_has_required_keys(self):
        """Every entry in the reverse map must have the expected keys."""
        for source_index, entries in REVERSE_MAP.items():
            for entry in entries:
                assert "child_index" in entry
                assert "esid_field" in entry
                assert "title_field" in entry
                assert "is_array" in entry
                assert isinstance(entry["is_array"], bool)

    def test_gads_customer_cascades_to_both_scalar_and_array(self):
        """deleting a Google Ads customer must clear BOTH the array
        team_google_ads_customer_esids (from ASSET_CONFIGS store_esid) AND the
        legacy scalar google_ads_customer_esid (added explicitly — it has no
        store_esid config so it would otherwise dangle and trigger 404s in the
        shared importer billing pre-check).
        """
        entries = REVERSE_MAP.get("de_asset_google_ads_customers", [])
        by_field = {e["esid_field"]: e for e in entries}

        assert "team_google_ads_customer_esids" in by_field, "the array field must be cascaded"
        assert by_field["team_google_ads_customer_esids"]["is_array"] is True
        assert by_field["team_google_ads_customer_esids"]["child_index"] == "de_asset_teams"

        assert "google_ads_customer_esid" in by_field, (
            "the legacy SCALAR field must ALSO be cascaded — without it, "
            "a deleted customer leaves a dangling scalar ref that 404s on every importer"
        )
        scalar = by_field["google_ads_customer_esid"]
        assert scalar["is_array"] is False
        assert scalar["child_index"] == "de_asset_teams"

    def test_gads_customer_delete_clears_legacy_scalar(self, rcs_mock_es):
        """The scalar-clear path actually fires for the legacy field on delete."""
        rcs_mock_es.update_by_query.return_value = {"updated": 1}
        ReferenceCleanupService.cleanup_references("de_asset_google_ads_customers", "ga567")

        scalar_calls = [
            c
            for c in rcs_mock_es.update_by_query.call_args_list
            if c[1]["body"]["script"]["params"].get("esid_field") == "google_ads_customer_esid"
        ]
        assert scalar_calls, "expected an update_by_query that clears google_ads_customer_esid"
        body = scalar_calls[0][1]["body"]
        # cleanup now matches on both mapping shapes (dual_term).
        assert body["query"] == {
            "bool": {
                "should": [
                    {"term": {"google_ads_customer_esid": "ga567"}},
                    {"term": {"google_ads_customer_esid.keyword": "ga567"}},
                ],
                "minimum_should_match": 1,
            }
        }

    def test_workspace_has_workspace_menu_cascade(self):
        """workspace deletion cascades to workspace-menu rows.
        The cascade clears ``workspace_esid`` from every menu doc;
        WorkspaceMenuService.list_menus filters by workspace_esid term,
        so orphaned rows are no longer visible to that workspace.
        """
        entries = REVERSE_MAP.get("de_asset_workspace", [])
        child_indices = {e["child_index"] for e in entries}
        assert "de_asset_workspace_menu" in child_indices
        # And the entry is shaped as a scalar field clear (not an
        # array element-remove), since workspace_esid on the menu doc
        # is a single keyword.
        wm_entry = next(
            e for e in entries if e["child_index"] == "de_asset_workspace_menu"
        )
        assert wm_entry["esid_field"] == "workspace_esid"
        assert wm_entry["is_array"] is False


class TestGetDependents:

    def test_returns_counts(self, rcs_mock_es):
        rcs_mock_es.count.return_value = {"count": 3}
        result = ReferenceCleanupService.get_dependents("de_asset_fb_accounts", "doc123")
        assert len(result) > 0
        for child_index, count in result.items():
            assert count == 3

    def test_returns_empty_for_zero_counts(self, rcs_mock_es):
        rcs_mock_es.count.return_value = {"count": 0}
        result = ReferenceCleanupService.get_dependents("de_asset_fb_accounts", "doc123")
        assert result == {}

    def test_returns_empty_for_unknown_index(self, rcs_mock_es):
        result = ReferenceCleanupService.get_dependents("de_asset_nonexistent", "doc123")
        assert result == {}

    def test_es_unavailable(self, rcs_mock_es_none):
        result = ReferenceCleanupService.get_dependents("de_asset_fb_accounts", "doc123")
        assert result == {}

    def test_es_exception_handled(self, rcs_mock_es):
        rcs_mock_es.count.side_effect = Exception("connection timeout")
        result = ReferenceCleanupService.get_dependents("de_asset_fb_accounts", "doc123")
        assert result == {}


class TestCleanupReferencesScalar:
    """Test cleanup for scalar (select) ESID fields."""

    def test_scalar_cleanup_calls_update_by_query(self, rcs_mock_es):
        """Deleting a chatwoot account should clean up chatwoot_account_esid (scalar) on teams."""
        rcs_mock_es.update_by_query.return_value = {"updated": 2}
        result = ReferenceCleanupService.cleanup_references("de_asset_chatwoot_accounts", "cw123")

        assert "de_asset_teams" in result
        assert result["de_asset_teams"] == 2

        call_args = rcs_mock_es.update_by_query.call_args
        body = call_args[1]["body"]
        # dual_term matches bare +.keyword.
        assert body["query"] == {
            "bool": {
                "should": [
                    {"term": {"chatwoot_account_esid": "cw123"}},
                    {"term": {"chatwoot_account_esid.keyword": "cw123"}},
                ],
                "minimum_should_match": 1,
            }
        }
        assert "painless" in body["script"]["lang"]
        assert call_args[1]["index"] == "de_asset_teams"
        assert call_args[1]["conflicts"] == "proceed"

    def test_scalar_script_removes_both_fields(self, rcs_mock_es):
        """The Painless script params should reference both esid and title fields."""
        rcs_mock_es.update_by_query.return_value = {"updated": 1}
        ReferenceCleanupService.cleanup_references("de_asset_chatwoot_accounts", "cw123")

        call_args = rcs_mock_es.update_by_query.call_args
        params = call_args[1]["body"]["script"]["params"]
        assert params["esid_field"] == "chatwoot_account_esid"
        assert params["title_field"] == "chatwoot_account_title"


class TestCleanupReferencesArray:
    """Test cleanup for array (multi_select) ESID fields."""

    def test_array_cleanup_calls_update_by_query(self, rcs_mock_es):
        """Deleting an organization should clean up organization_esid (array) on teams."""
        rcs_mock_es.update_by_query.return_value = {"updated": 5}
        result = ReferenceCleanupService.cleanup_references("de_asset_organization", "org123")

        assert "de_asset_teams" in result
        assert result["de_asset_teams"] == 5

    def test_array_script_includes_doc_id(self, rcs_mock_es):
        """Array cleanup needs doc_id in script params for indexOf removal."""
        rcs_mock_es.update_by_query.return_value = {"updated": 1}
        ReferenceCleanupService.cleanup_references("de_asset_organization", "org123")

        # Find the call for de_asset_teams (organization_esid is array)
        for call in rcs_mock_es.update_by_query.call_args_list:
            if call[1]["index"] == "de_asset_teams":
                params = call[1]["body"]["script"]["params"]
                assert params["doc_id"] == "org123"
                assert params["esid_field"] == "organization_esid"
                assert params["title_field"] == "organization_title"
                return
        pytest.fail("Expected update_by_query call for de_asset_teams not found")


class TestCleanupReferencesNoOp:

    def test_unknown_index_returns_empty(self, rcs_mock_es):
        result = ReferenceCleanupService.cleanup_references("de_asset_nonexistent", "doc123")
        assert result == {}
        rcs_mock_es.update_by_query.assert_not_called()

    def test_zero_updates_not_in_result(self, rcs_mock_es):
        rcs_mock_es.update_by_query.return_value = {"updated": 0}
        result = ReferenceCleanupService.cleanup_references("de_asset_chatwoot_accounts", "cw123")
        assert result == {}


class TestCleanupReferencesErrorHandling:

    def test_es_unavailable(self, rcs_mock_es_none):
        result = ReferenceCleanupService.cleanup_references("de_asset_chatwoot_accounts", "cw123")
        assert result == {}

    def test_partial_failure_continues(self, rcs_mock_es):
        """If one child index fails, others should still be cleaned."""
        call_count = [0]
        entries = REVERSE_MAP.get("de_asset_fb_accounts", [])
        if len(entries) < 2:
            pytest.skip("Need at least 2 children for this test")

        def side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("first child fails")
            return {"updated": 1}

        rcs_mock_es.update_by_query.side_effect = side_effect
        result = ReferenceCleanupService.cleanup_references("de_asset_fb_accounts", "doc123")
        # At least one child should have been cleaned despite the failure
        assert len(result) >= 1
        # All children should have been attempted
        assert rcs_mock_es.update_by_query.call_count == len(entries)


class TestCleanupReferencesBulk:

    def test_bulk_uses_terms_query(self, rcs_mock_es):
        """Bulk cleanup should use a terms (plural) match, not a single term.
        the match is a dual_terms bool/should over both mapping
        shapes — each leg is a ``terms`` (not ``term``) clause."""
        rcs_mock_es.update_by_query.return_value = {"updated": 3}
        doc_ids = ["cw1", "cw2", "cw3"]
        result = ReferenceCleanupService.cleanup_references_bulk("de_asset_chatwoot_accounts", doc_ids)

        assert "de_asset_teams" in result
        call_args = rcs_mock_es.update_by_query.call_args
        body = call_args[1]["body"]
        legs = body["query"]["bool"]["should"]
        assert all("terms" in leg for leg in legs), "each dual leg must be a terms clause"
        assert {list(leg["terms"].keys())[0] for leg in legs} == {
            "chatwoot_account_esid",
            "chatwoot_account_esid.keyword",
        }
        for leg in legs:
            assert list(leg["terms"].values())[0] == doc_ids

    def test_bulk_array_includes_doc_ids_list(self, rcs_mock_es):
        """Bulk array cleanup should pass the full list of doc_ids in script params."""
        rcs_mock_es.update_by_query.return_value = {"updated": 2}
        doc_ids = ["org1", "org2"]
        ReferenceCleanupService.cleanup_references_bulk("de_asset_organization", doc_ids)

        # Find the call for de_asset_teams (array field)
        for call in rcs_mock_es.update_by_query.call_args_list:
            if call[1]["index"] == "de_asset_teams":
                params = call[1]["body"]["script"]["params"]
                assert params["doc_ids"] == doc_ids
                return
        pytest.fail("Expected update_by_query call for de_asset_teams not found")

    def test_empty_doc_ids_returns_empty(self, rcs_mock_es):
        result = ReferenceCleanupService.cleanup_references_bulk("de_asset_cities", [])
        assert result == {}
        rcs_mock_es.update_by_query.assert_not_called()

    def test_es_unavailable(self, rcs_mock_es_none):
        result = ReferenceCleanupService.cleanup_references_bulk("de_asset_cities", ["x"])
        assert result == {}

    def test_unknown_index_returns_empty(self, rcs_mock_es):
        result = ReferenceCleanupService.cleanup_references_bulk("de_asset_nonexistent", ["x"])
        assert result == {}
        rcs_mock_es.update_by_query.assert_not_called()


class TestAssetServiceDeleteIntegration:
    """Verify AssetService.delete() triggers reference cleanup."""

    def test_delete_calls_cleanup_on_success(self):
        with mock.patch(
            "asset_management.web.services.asset_service.delete_document_by_id",
            return_value=True,
        ), mock.patch(
            "asset_management.web.services.reference_cleanup_service.ReferenceCleanupService.cleanup_references",
            return_value={},
        ) as cleanup_mock:
            from asset_management.web.services.asset_service import AssetService

            AssetService.delete("de_asset_cities", "city123")
            cleanup_mock.assert_called_once_with("de_asset_cities", "city123")

    def test_delete_skips_cleanup_on_failure(self):
        with mock.patch(
            "asset_management.web.services.asset_service.delete_document_by_id",
            return_value=False,
        ), mock.patch(
            "asset_management.web.services.reference_cleanup_service.ReferenceCleanupService.cleanup_references",
        ) as cleanup_mock:
            from asset_management.web.services.asset_service import AssetService

            AssetService.delete("de_asset_cities", "city123")
            cleanup_mock.assert_not_called()

    def test_delete_succeeds_even_if_cleanup_fails(self):
        with mock.patch(
            "asset_management.web.services.asset_service.delete_document_by_id",
            return_value=True,
        ), mock.patch(
            "asset_management.web.services.reference_cleanup_service.ReferenceCleanupService.cleanup_references",
            side_effect=Exception("cleanup boom"),
        ):
            from asset_management.web.services.asset_service import AssetService

            result = AssetService.delete("de_asset_cities", "city123")
            assert result is True

    def test_delete_only_skips_cleanup(self):
        with mock.patch(
            "asset_management.web.services.asset_service.delete_document_by_id",
            return_value=True,
        ), mock.patch(
            "asset_management.web.services.reference_cleanup_service.ReferenceCleanupService.cleanup_references",
        ) as cleanup_mock:
            from asset_management.web.services.asset_service import AssetService

            AssetService.delete_only("de_asset_cities", "city123")
            cleanup_mock.assert_not_called()
