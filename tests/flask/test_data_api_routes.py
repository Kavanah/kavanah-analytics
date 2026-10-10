"""
Tests for data-related API routes.

Routes covered:
  GET  /api/import/data-source-status    get_data_source_status
  GET  /api/import/data-view-teams       get_data_view_teams
  GET  /api/import/field-values          get_field_values
  GET  /api/search                       search_assets
  GET  /api/dashboard/summary            get_dashboard_summary
  GET  /api/asset/<type>/<id>/details    get_asset_details

All Elasticsearch and service calls are mocked.
"""

from unittest import mock

import pytest

# validate_index_access is real now. These tests cover
# orthogonal route logic under a session with no ES backend, so grant the gate.
pytestmark = pytest.mark.usefixtures("allow_index_scope")


# ---------------------------------------------------------------------------
# Helpers — canned ES responses
# ---------------------------------------------------------------------------


def _es_search_response(total=0, aggs=None, hits=None):
    """Build a minimal ES search response dict."""
    resp = {
        "hits": {
            "total": {"value": total},
            "hits": hits or [],
        },
        "aggregations": aggs or {},
    }
    return resp


def _mock_es_client(search_return=None, search_side_effect=None):
    """Return a MagicMock posing as an Elasticsearch client."""
    es = mock.MagicMock()
    if search_side_effect:
        es.search.side_effect = search_side_effect
    elif search_return is not None:
        es.search.return_value = search_return
    return es


# ===========================================================================
# GET /api/import/data-source-status
# ===========================================================================


class TestDataSourceStatus:
    """Tests for the data-source-status API."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/import/data-source-status?data_view=test_index")
        assert resp.status_code == 401

    def test_missing_data_view_returns_400(self, auth_client):
        resp = auth_client.get("/api/import/data-source-status")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "data_view is required"

    def test_empty_data_view_returns_400(self, auth_client):
        resp = auth_client.get("/api/import/data-source-status?data_view=")
        assert resp.status_code == 400

    @mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None)
    def test_es_unavailable_returns_503(self, mock_get_es, auth_client):
        resp = auth_client.get("/api/import/data-source-status?data_view=test_index")
        assert resp.status_code == 503
        assert "unavailable" in resp.get_json()["error"].lower()

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_basic_response_structure(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                total=42,
                aggs={
                    "max_timestamp": {"value_as_string": "2025-06-15T00:00:00Z"},
                    "by_day": {"buckets": []},
                },
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-source-status?data_view=test_index")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["document_count"] == 42
        assert data["last_pulled"] == "2025-06-15T00:00:00Z"
        assert isinstance(data["monthly_summary"], list)

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_team_filter_applied(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                total=10,
                aggs={"max_timestamp": {"value_as_string": None}, "by_day": {"buckets": []}},
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-source-status?data_view=test_index&team=TeamA")
        assert resp.status_code == 200
        # Verify ES was called with a query containing team filter
        call_body = es.search.call_args[1]["body"]
        assert call_body["query"] != {"match_all": {}}

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_date_range_filter(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                total=5,
                aggs={"max_timestamp": {"value_as_string": None}, "by_day": {"buckets": []}},
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-source-status?data_view=test_index&from_date=2025-01-01&to_date=2025-03-31")
        assert resp.status_code == 200
        data = resp.get_json()
        # Should have gap-filled monthly summary for Jan-Mar
        assert data["success"] is True
        months = [m["month"] for m in data["monthly_summary"]]
        assert 1 in months
        assert 2 in months
        assert 3 in months

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_monthly_summary_aggregation(self, mock_get_es, auth_client):
        buckets = [
            {"key_as_string": "2025-02-01", "doc_count": 10},
            {"key_as_string": "2025-02-15", "doc_count": 5},
            {"key_as_string": "2025-03-10", "doc_count": 3},
        ]
        es = _mock_es_client(
            _es_search_response(
                total=18,
                aggs={"max_timestamp": {"value_as_string": None}, "by_day": {"buckets": buckets}},
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-source-status?data_view=test_index")
        assert resp.status_code == 200
        data = resp.get_json()
        summary = data["monthly_summary"]
        assert len(summary) == 2  # Feb and Mar
        feb = [m for m in summary if m["month"] == 2][0]
        assert feb["dates_pulled"] == 2
        assert feb["year"] == 2025

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_disciple_tools_skips_by_day_agg(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                total=100,
                aggs={"max_timestamp": {"value_as_string": "2025-06-01T00:00:00Z"}},
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-source-status?data_view=disciple-tools-contacts")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["monthly_summary"] == []

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_es_exception_returns_500(self, mock_get_es, auth_client):
        es = _mock_es_client(search_side_effect=Exception("ES error"))
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-source-status?data_view=test_index")
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False


# ===========================================================================
# GET /api/import/data-view-teams
# ===========================================================================


class TestDataViewTeams:
    """Tests for the data-view-teams API."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/import/data-view-teams?data_view=test_index")
        assert resp.status_code == 401

    def test_missing_data_view_returns_400(self, auth_client):
        resp = auth_client.get("/api/import/data-view-teams")
        assert resp.status_code == 400

    @mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None)
    def test_es_unavailable_returns_503(self, mock_get_es, auth_client):
        resp = auth_client.get("/api/import/data-view-teams?data_view=test_index")
        assert resp.status_code == 503

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_returns_teams_from_keyword_subfield(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                aggs={"teams": {"buckets": [{"key": "TeamA"}, {"key": "TeamB"}]}},
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-view-teams?data_view=test_index")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["teams_with_data"] == ["TeamA", "TeamB"]

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_falls_back_to_base_field(self, mock_get_es, auth_client):
        es = mock.MagicMock()
        # First call (keyword subfield) returns empty
        # Second call (base field) returns teams
        es.search.side_effect = [
            _es_search_response(aggs={"teams": {"buckets": []}}),
            _es_search_response(aggs={"teams": {"buckets": [{"key": "FallbackTeam"}]}}),
        ]
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-view-teams?data_view=test_index")
        assert resp.status_code == 200
        assert "FallbackTeam" in resp.get_json()["teams_with_data"]

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_fallback_field_error_returns_empty(self, mock_get_es, auth_client):
        es = mock.MagicMock()
        es.search.side_effect = [
            _es_search_response(aggs={"teams": {"buckets": []}}),
            Exception("fielddata not available"),
        ]
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-view-teams?data_view=test_index")
        assert resp.status_code == 200
        assert resp.get_json()["teams_with_data"] == []

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_es_exception_returns_500(self, mock_get_es, auth_client):
        es = _mock_es_client(search_side_effect=Exception("Connection error"))
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-view-teams?data_view=test_index")
        assert resp.status_code == 500

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_returns_sorted_teams(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                aggs={"teams": {"buckets": [{"key": "Zebra"}, {"key": "Alpha"}]}},
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/data-view-teams?data_view=test_index")
        teams = resp.get_json()["teams_with_data"]
        assert teams == ["Alpha", "Zebra"]


# ===========================================================================
# GET /api/import/field-values
# ===========================================================================


class TestFieldValues:
    """Tests for the field-values API."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/import/field-values?data_view=x&date=2025-01-01&field=impressions")
        assert resp.status_code == 401

    def test_missing_data_view_returns_400(self, auth_client):
        resp = auth_client.get("/api/import/field-values?date=2025-01-01&field=impressions")
        assert resp.status_code == 400

    def test_missing_date_returns_400(self, auth_client):
        resp = auth_client.get("/api/import/field-values?data_view=x&field=impressions")
        assert resp.status_code == 400

    def test_missing_field_returns_400(self, auth_client):
        resp = auth_client.get("/api/import/field-values?data_view=x&date=2025-01-01")
        assert resp.status_code == 400

    @mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None)
    def test_es_unavailable_returns_503(self, mock_get_es, auth_client):
        resp = auth_client.get("/api/import/field-values?data_view=x&date=2025-01-01&field=impressions")
        assert resp.status_code == 503

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_returns_field_values(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                aggs={
                    "by_team": {
                        "buckets": [
                            {
                                "key": "TeamA",
                                "field_sample": {
                                    "hits": {"hits": [{"_source": {"impressions": 1234}}]},
                                },
                            },
                        ],
                    },
                },
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/field-values?data_view=test_index&date=2025-01-15&field=impressions")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["values"]) == 1
        assert data["values"][0]["team_title"] == "TeamA"
        assert data["values"][0]["value"] == 1234

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_dotted_field_path(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                aggs={
                    "by_team": {
                        "buckets": [
                            {
                                "key": "TeamA",
                                "field_sample": {
                                    "hits": {"hits": [{"_source": {"metrics": {"clicks": 99}}}]},
                                },
                            },
                        ],
                    },
                },
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/field-values?data_view=test_index&date=2025-01-15&field=metrics.clicks")
        assert resp.status_code == 200
        assert resp.get_json()["values"][0]["value"] == 99

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_falls_back_to_base_team_field(self, mock_get_es, auth_client):
        es = mock.MagicMock()
        # First call (keyword subfield) returns empty
        # Second call (base field) returns values
        es.search.side_effect = [
            _es_search_response(aggs={"by_team": {"buckets": []}}),
            _es_search_response(
                aggs={
                    "by_team": {
                        "buckets": [
                            {
                                "key": "FallbackTeam",
                                "field_sample": {
                                    "hits": {"hits": [{"_source": {"impressions": 10}}]},
                                },
                            },
                        ],
                    },
                },
            ),
        ]
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/field-values?data_view=test_index&date=2025-01-15&field=impressions")
        assert resp.status_code == 200
        assert resp.get_json()["values"][0]["team_title"] == "FallbackTeam"

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_team_filter_applied(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                aggs={"by_team": {"buckets": []}},
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/field-values?data_view=test_index&date=2025-01-15&field=impressions&team=TeamA")
        assert resp.status_code == 200
        # Verify team filter was passed
        call_body = es.search.call_args[1]["body"]
        filters = call_body["query"]["bool"]["filter"]
        team_filters = [f for f in filters if "match_phrase" in f]
        assert len(team_filters) == 1

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_disciple_tools_skips_date_filter(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                aggs={"by_team": {"buckets": []}},
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/field-values?data_view=disciple-tools-contacts&date=2025-01-15&field=status")
        assert resp.status_code == 200
        call_body = es.search.call_args[1]["body"]
        filters = call_body["query"]["bool"]["filter"]
        range_filters = [f for f in filters if "range" in f]
        assert len(range_filters) == 0

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_es_exception_returns_500(self, mock_get_es, auth_client):
        es = _mock_es_client(search_side_effect=Exception("timeout"))
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/field-values?data_view=test_index&date=2025-01-15&field=impressions")
        assert resp.status_code == 500

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    def test_values_sorted_by_team_title(self, mock_get_es, auth_client):
        es = _mock_es_client(
            _es_search_response(
                aggs={
                    "by_team": {
                        "buckets": [
                            {"key": "Zebra", "field_sample": {"hits": {"hits": [{"_source": {"f": 1}}]}}},
                            {"key": "Alpha", "field_sample": {"hits": {"hits": [{"_source": {"f": 2}}]}}},
                        ],
                    },
                },
            )
        )
        mock_get_es.return_value = es
        resp = auth_client.get("/api/import/field-values?data_view=test_index&date=2025-01-15&field=f")
        values = resp.get_json()["values"]
        assert values[0]["team_title"] == "Alpha"
        assert values[1]["team_title"] == "Zebra"


# ===========================================================================
# GET /api/search
# ===========================================================================


class TestSearchAssets:
    """Tests for the global search API."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/search?q=test")
        assert resp.status_code == 401

    def test_missing_query_returns_400(self, auth_client):
        resp = auth_client.get("/api/search")
        assert resp.status_code == 400
        assert "required" in resp.get_json()["error"].lower()

    def test_empty_query_returns_400(self, auth_client):
        resp = auth_client.get("/api/search?q=")
        assert resp.status_code == 400

    @mock.patch("asset_management.web.app.AssetService.get_all", return_value=[])
    def test_no_results(self, mock_get_all, auth_client):
        resp = auth_client.get("/api/search?q=nonexistent")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["query"] == "nonexistent"
        assert data["results"] == {}

    @mock.patch("asset_management.web.app.RoleFilterService.should_apply_filter", return_value=False)
    @mock.patch("asset_management.web.app.RoleFilterService.current_user_has_any_scope", return_value=True)
    @mock.patch("asset_management.web.app.AssetService.get_all")
    def test_wildcard_match(self, mock_get_all, _scope, _filter, auth_client):
        # Phase C added per-asset workspace scoping to /api/search;
        # mock the scope methods so the test runs the no-filter path
        # (the unit under test is wildcard matching, not the scope filter).
        mock_get_all.return_value = [
            {"_id": "t1", "_source": {"team_title": "Alpha Team"}},
            {"_id": "t2", "_source": {"team_title": "Beta Team"}},
        ]
        resp = auth_client.get("/api/search?q=Alpha*")
        assert resp.status_code == 200
        data = resp.get_json()
        # Should find Alpha Team via wildcard match
        has_match = False
        for asset_key, asset_data in data["results"].items():
            for m in asset_data["matches"]:
                if "Alpha" in m["title"]:
                    has_match = True
        assert has_match

    @mock.patch("asset_management.web.app.RoleFilterService.should_apply_filter", return_value=False)
    @mock.patch("asset_management.web.app.RoleFilterService.current_user_has_any_scope", return_value=True)
    @mock.patch("asset_management.web.app.AssetService.get_all")
    def test_case_insensitive_search(self, mock_get_all, _scope, _filter, auth_client):
        mock_get_all.return_value = [
            {"_id": "t1", "_source": {"team_title": "UPPERCASE Team"}},
        ]
        resp = auth_client.get("/api/search?q=uppercase*")
        assert resp.status_code == 200
        data = resp.get_json()
        found = any(m["title"] == "UPPERCASE Team" for results in data["results"].values() for m in results["matches"])
        assert found

    @mock.patch("asset_management.web.app.AssetService.get_all", return_value=[])
    def test_response_includes_query(self, mock_get_all, auth_client):
        resp = auth_client.get("/api/search?q=test*query")
        data = resp.get_json()
        assert data["query"] == "test*query"


# ===========================================================================
# GET /api/dashboard/summary
# ===========================================================================


class TestDashboardSummary:
    """Tests for the dashboard summary statistics API."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/dashboard/summary")
        assert resp.status_code == 401

    @mock.patch("asset_management.web.app.AssetService.count", return_value=0)
    def test_empty_counts(self, mock_count, auth_client):
        resp = auth_client.get("/api/dashboard/summary")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total_assets"] == 0
        assert isinstance(data["by_category"], dict)
        assert isinstance(data["by_type"], dict)

    @mock.patch("asset_management.web.app.RoleFilterService.should_apply_filter", return_value=False)
    @mock.patch("asset_management.web.app.RoleFilterService.current_user_has_any_scope", return_value=True)
    @mock.patch("asset_management.web.app.AssetService.count", return_value=5)
    def test_counts_all_asset_types(self, mock_count, _scope, _filter, auth_client):
        # Phase C added workspace-scoped counts for the 13 data-source
        # asset types. Mock the scope methods to hit the no-filter path so
        # this test still validates the per-type count behavior.
        resp = auth_client.get("/api/dashboard/summary")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total_assets"] > 0
        # Each asset type should have count=5
        for at_key, at_data in data["by_type"].items():
            assert at_data["count"] == 5
            assert "title" in at_data

    @mock.patch("asset_management.web.app.AssetService.count", return_value=3)
    def test_category_totals(self, mock_count, auth_client):
        resp = auth_client.get("/api/dashboard/summary")
        data = resp.get_json()
        # total should be 3 * number of asset types
        total_from_categories = sum(c["count"] for c in data["by_category"].values())
        assert total_from_categories == data["total_assets"]


# ===========================================================================
# GET /api/asset/<type>/<id>/details
# ===========================================================================


class TestAssetDetails:
    """Tests for the single-asset detail API."""

    @pytest.fixture(autouse=True)
    def _allow_company_tier_scope(self):
        """the details route now enforces a company-tier per-object
        scope check for teams/organizations. These tests exercise the response
        SHAPE (not the gate), so grant access; the gate itself is covered by
        the release-plan tests"""
        with mock.patch(
            "asset_management.web.app._company_tier_asset_in_user_scope",
            return_value=True,
        ):
            yield

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/asset/teams/abc123/details")
        assert resp.status_code == 401

    def test_invalid_asset_type_returns_404(self, auth_client):
        resp = auth_client.get("/api/asset/nonexistent/abc123/details")
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"].lower()

    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=None)
    def test_missing_item_returns_404(self, mock_get, auth_client):
        resp = auth_client.get("/api/asset/teams/missing_id/details")
        assert resp.status_code == 404
        assert "not found" in resp.get_json()["error"].lower()

    @mock.patch("asset_management.web.app.FormService.get_computed_display_fields", return_value={})
    @mock.patch("asset_management.web.app.FormService.prepare_for_display", return_value={"team_title": "Demo"})
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "Demo"})
    def test_returns_asset_data(self, mock_get, mock_prep, mock_comp, auth_client):
        resp = auth_client.get("/api/asset/teams/abc123/details")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == "abc123"
        assert data["asset_type"] == "teams"
        assert data["data"]["team_title"] == "Demo"
        assert "asset_config" in data
        assert "computed_fields" in data

    @mock.patch("asset_management.web.app.FormService.get_computed_display_fields", return_value={"link": "http://x"})
    @mock.patch("asset_management.web.app.FormService.prepare_for_display", return_value={"team_title": "T"})
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "T"})
    def test_includes_computed_fields(self, mock_get, mock_prep, mock_comp, auth_client):
        resp = auth_client.get("/api/asset/teams/abc123/details")
        data = resp.get_json()
        assert data["computed_fields"] == {"link": "http://x"}

    @mock.patch("asset_management.web.app.FormService.get_computed_display_fields", return_value={})
    @mock.patch("asset_management.web.app.FormService.prepare_for_display", return_value={"team_title": "T"})
    @mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "T"})
    def test_asset_config_includes_title_and_fields(self, mock_get, mock_prep, mock_comp, auth_client):
        resp = auth_client.get("/api/asset/teams/abc123/details")
        data = resp.get_json()
        assert "title" in data["asset_config"]
        assert "title_field" in data["asset_config"]
        assert "fields" in data["asset_config"]
