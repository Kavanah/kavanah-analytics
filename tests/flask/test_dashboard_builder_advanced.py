"""
Advanced tests for dashboard builder blueprint routes.

Covers areas NOT in test_dashboard_builder.py:
  - _build_date_filter, _dt_date_filter, _build_stat_filter
  - _org_team_filters
  - _echo_date_filter
  - _get_field_types
  - _geocode_address, _reverse_geocode_admin1, _load_geocode_cache
  - _chart_data_to_table, _es_doc_to_dashboard_state
  - _run_table_multi_dv_query (route-level via run_query)
  - GET  /dashboard-builder/api/map-data
  - POST /dashboard-builder/api/dashboards/<id>/export
"""

from unittest import mock

import pytest

from asset_management.web.blueprints.dashboard_builder.routes import (
    _build_date_filter,
    _build_stat_filter,
    _dt_date_filter,
    _echo_date_filter,
    _org_team_filters,
    _chart_data_to_table,
    _es_doc_to_dashboard_state,
    _MAP_STATISTICS,
)

# validate_index_access is real now. These tests cover
# orthogonal route logic under a session with no ES backend, so grant the gate.
pytestmark = pytest.mark.usefixtures("allow_index_scope")

_R = "asset_management.web.blueprints.dashboard_builder.routes"


# ===========================================================================
# _build_date_filter
# ===========================================================================


class TestBuildDateFilter:

    def test_both_bounds(self):
        result = _build_date_filter("2026-01-01", "2026-06-30", "kibana_date")
        assert result == {"range": {"kibana_date": {"gte": "2026-01-01", "lte": "2026-06-30"}}}

    def test_only_from(self):
        result = _build_date_filter("2026-01-01", None, "kibana_date")
        assert result == {"range": {"kibana_date": {"gte": "2026-01-01"}}}

    def test_only_to(self):
        result = _build_date_filter(None, "2026-12-31", "kibana_date")
        assert result == {"range": {"kibana_date": {"lte": "2026-12-31"}}}

    def test_no_dates_returns_none(self):
        assert _build_date_filter(None, None, "kibana_date") is None

    def test_empty_strings_returns_none(self):
        assert _build_date_filter("", "", "kibana_date") is None

    def test_custom_field(self):
        result = _build_date_filter("2025-01-01", "2025-12-31", "date_created.keyword")
        assert "date_created.keyword" in result["range"]


# ===========================================================================
# _build_stat_filter
# ===========================================================================


class TestBuildStatFilter:

    def test_no_field_returns_none(self):
        assert _build_stat_filter({"field": None}) is None
        assert _build_stat_filter({}) is None

    def test_exists_type(self):
        cfg = {"field": "meta.baptism_date", "type": "exists"}
        assert _build_stat_filter(cfg) == {"exists": {"field": "meta.baptism_date"}}

    def test_term_type(self):
        cfg = {"field": "meta.faith_status.keyword", "type": "term", "value": "believer"}
        assert _build_stat_filter(cfg) == {"term": {"meta.faith_status.keyword": "believer"}}

    def test_phrase_type(self):
        cfg = {"field": "some_field", "type": "phrase", "value": "hello world"}
        assert _build_stat_filter(cfg) == {"match_phrase": {"some_field": "hello world"}}

    def test_range_type(self):
        cfg = {"field": "insight_reach", "type": "range", "gte": 1}
        assert _build_stat_filter(cfg) == {"range": {"insight_reach": {"gte": 1}}}

    def test_default_is_range(self):
        """When type is missing but field is present, falls back to range."""
        cfg = {"field": "some_field", "gte": 5}
        assert _build_stat_filter(cfg) == {"range": {"some_field": {"gte": 5}}}

    def test_all_map_statistics_have_expected_shape(self):
        """Every entry in _MAP_STATISTICS should produce either None or a dict filter."""
        for key, cfg in _MAP_STATISTICS.items():
            result = _build_stat_filter(cfg)
            assert result is None or isinstance(result, dict), f"Bad filter for {key}"


# ===========================================================================
# _dt_date_filter
# ===========================================================================


class TestDtDateFilter:

    def test_uses_dt_date_field(self):
        cfg = {"dt_date_field": "meta.first_contact_date.keyword"}
        result = _dt_date_filter("2026-01-01", "2026-06-30", cfg)
        assert result is not None
        assert "meta.first_contact_date.keyword" in result["range"]

    def test_defaults_to_date_created(self):
        cfg = {}
        result = _dt_date_filter("2026-01-01", "2026-06-30", cfg)
        assert "date_created_ms" in result["range"]

    def test_no_dates_returns_none(self):
        cfg = {"dt_date_field": "date_created.keyword"}
        assert _dt_date_filter("", "", cfg) is None

    def test_unix_ts_mode(self):
        cfg = {"dt_date_field": "meta.start_date.keyword", "dt_date_unix": True}
        result = _dt_date_filter("now-30d/d", "now", cfg)
        assert result is not None
        rng = result["range"]["meta.start_date.keyword"]
        # Both values should be numeric epoch strings
        assert rng["gte"].isdigit()
        assert rng["lte"].isdigit()


# ===========================================================================
# _echo_date_filter
# ===========================================================================


class TestEchoDateFilter:
    # Echo's kibana_date is now mapped as yyyy-MM-dd (matching every
    # other platform), so _echo_date_filter is a thin wrapper over
    # _build_date_filter — it emits a plain ISO range clause, not the
    # epoch-millis workaround that the legacy custom-format mapping required.

    def test_returns_iso_range_clause(self):
        result = _echo_date_filter("2026-01-01", "2026-01-31")
        assert result == {"range": {"kibana_date": {"gte": "2026-01-01", "lte": "2026-01-31"}}}

    def test_no_dates_returns_none(self):
        assert _echo_date_filter("", "") is None
        assert _echo_date_filter(None, None) is None

    def test_only_from(self):
        result = _echo_date_filter("2026-03-01", "")
        rng = result["range"]["kibana_date"]
        assert "gte" in rng
        assert "lte" not in rng

    def test_only_to(self):
        result = _echo_date_filter("", "2026-03-01")
        rng = result["range"]["kibana_date"]
        assert "lte" in rng
        assert "gte" not in rng


# ===========================================================================
# _org_team_filters
# ===========================================================================


class TestOrgTeamFilters:

    def test_no_org_no_team(self):
        assert _org_team_filters("", []) == []

    def test_org_only(self):
        clauses = _org_team_filters("Acme Corp", [])
        assert len(clauses) == 1
        assert clauses[0] == {"match_phrase": {"organization_title": "Acme Corp"}}

    def test_single_team_string(self):
        clauses = _org_team_filters("", "Team Alpha")
        assert len(clauses) == 1
        assert clauses[0] == {"match_phrase": {"team_title": "Team Alpha"}}

    def test_single_team_list(self):
        clauses = _org_team_filters("", ["Team Alpha"])
        assert len(clauses) == 1
        assert clauses[0] == {"match_phrase": {"team_title": "Team Alpha"}}

    def test_multiple_teams(self):
        clauses = _org_team_filters("", ["Team A", "Team B", "Team C"])
        assert len(clauses) == 1
        should_clause = clauses[0]["bool"]["should"]
        assert len(should_clause) == 3
        assert clauses[0]["bool"]["minimum_should_match"] == 1

    def test_org_and_teams(self):
        # When teams are provided, the org filter is skipped because the team
        # filter already scopes the data (some data views lack organization_title).
        clauses = _org_team_filters("Org", ["T1", "T2"])
        assert len(clauses) == 1
        assert "bool" in clauses[0]

    def test_empty_string_team(self):
        clauses = _org_team_filters("", "")
        assert clauses == []

    def test_empty_list_team(self):
        clauses = _org_team_filters("", [])
        assert clauses == []


# ===========================================================================
# _es_doc_to_dashboard_state
# ===========================================================================


class TestEsDocToDashboardState:

    def test_echarts_conversion(self):
        source = {
            "dashboard_id": "d1",
            "dashboard_type": "echarts",
            "title": "My Dashboard",
            "echarts_config": {
                "global_date_range": {"field": "kibana_date", "from": "now-30d/d", "to": "now"},
                "charts": [{"chart_id": "c1", "chart_type": "bar"}],
            },
        }
        state = _es_doc_to_dashboard_state("d1", source)
        assert state["id"] == "d1"
        assert state["title"] == "My Dashboard"
        assert state["dashboard_type"] == "echarts"
        assert state["global_date_range"]["field"] == "kibana_date"
        assert len(state["charts"]) == 1

    def test_leaflet_map_conversion(self):
        source = {
            "dashboard_id": "m1",
            "dashboard_type": "leaflet_map",
            "title": "World Map",
            "map_config": {"zoom": 5, "center": [0, 0]},
        }
        state = _es_doc_to_dashboard_state("m1", source)
        assert state["dashboard_type"] == "leaflet_map"
        assert state["map_config"]["zoom"] == 5
        assert state["global_date_range"] is None
        assert state["charts"] == {}

    def test_missing_echarts_config(self):
        source = {"dashboard_id": "d2", "dashboard_type": "echarts", "title": "Empty"}
        state = _es_doc_to_dashboard_state("d2", source)
        assert state["charts"] == []
        assert state["global_date_range"] is None

    def test_doc_id_fallback(self):
        source = {"dashboard_type": "echarts", "title": "No ID"}
        state = _es_doc_to_dashboard_state("fallback-id", source)
        assert state["id"] == "fallback-id"


# ===========================================================================
# _chart_data_to_table
# ===========================================================================


class TestChartDataToTable:

    def test_table_passthrough(self):
        result = {"chart_type": "table", "columns": ["A", "B"], "rows": [[1, 2]]}
        cols, rows = _chart_data_to_table(result, {})
        assert cols == ["A", "B"]
        assert rows == [[1, 2]]

    def test_metric_simple(self):
        result = {"chart_type": "metric", "label": "Total", "value": 42}
        cols, rows = _chart_data_to_table(result, {"chart_type": "metric"})
        assert cols == ["Metric", "Value"]
        assert rows == [["Total", 42]]

    def test_metric_with_breakdowns(self):
        result = {
            "chart_type": "metric",
            "breakdowns": [
                {"label": "A", "value": 10},
                {"label": "B", "value": 20},
            ],
        }
        cols, rows = _chart_data_to_table(result, {"chart_type": "metric"})
        assert cols == ["Category", "Value"]
        assert len(rows) == 2

    def test_pie_chart(self):
        result = {
            "series": [
                {
                    "type": "pie",
                    "data": [
                        {"name": "Slice A", "value": 30},
                        {"name": "Slice B", "value": 70},
                    ],
                }
            ]
        }
        cols, rows = _chart_data_to_table(result, {"chart_type": "pie"})
        assert cols == ["Category", "Value"]
        assert len(rows) == 2

    def test_bar_chart(self):
        result = {
            "xAxis": {"data": ["Jan", "Feb", "Mar"]},
            "series": [
                {"name": "Reach", "data": [10, 20, 30]},
                {"name": "Clicks", "data": [5, 10, 15]},
            ],
        }
        cols, rows = _chart_data_to_table(result, {"chart_type": "bar", "x_field": "month.keyword"})
        assert cols == ["month", "Reach", "Clicks"]
        assert len(rows) == 3
        assert rows[0] == ["Jan", 10, 5]

    def test_empty_series(self):
        result = {"series": []}
        cols, rows = _chart_data_to_table(result, {"chart_type": "bar"})
        assert cols == []
        assert rows == []

    def test_no_data(self):
        cols, rows = _chart_data_to_table({}, {})
        assert cols == []
        assert rows == []


# ===========================================================================
# _get_field_types
# ===========================================================================


class TestGetFieldTypes:

    def test_returns_flattened_types(self):
        from asset_management.web.blueprints.dashboard_builder.routes import _get_field_types

        es_mock = mock.MagicMock()
        es_mock.indices.get_mapping.return_value = {
            "my_index": {
                "mappings": {
                    "properties": {
                        "kibana_date": {"type": "date"},
                        "insight_reach": {"type": "long"},
                        "campaign": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                    }
                }
            }
        }
        result = _get_field_types(es_mock, "my_index")
        assert result["kibana_date"] == "date"
        assert result["insight_reach"] == "numeric"
        assert result["campaign"] == "text"
        assert result["campaign.keyword"] == "keyword"

    def test_exception_returns_empty(self):
        from asset_management.web.blueprints.dashboard_builder.routes import _get_field_types

        es_mock = mock.MagicMock()
        es_mock.indices.get_mapping.side_effect = Exception("not found")
        assert _get_field_types(es_mock, "missing") == {}

    def test_merges_multiple_indices(self):
        from asset_management.web.blueprints.dashboard_builder.routes import _get_field_types

        es_mock = mock.MagicMock()
        es_mock.indices.get_mapping.return_value = {
            "idx_2025": {"mappings": {"properties": {"date": {"type": "date"}}}},
            "idx_2026": {"mappings": {"properties": {"date": {"type": "date"}, "extra": {"type": "keyword"}}}},
        }
        result = _get_field_types(es_mock, "idx_*")
        assert "date" in result
        assert "extra" in result


# ===========================================================================
# _geocode_address
# ===========================================================================


class TestGeocodeAddress:

    @mock.patch(f"{_R}._persist_geocode_cache")
    @mock.patch(f"{_R}.http_requests.get")
    @mock.patch(f"{_R}._load_geocode_cache")
    def test_successful_geocode(self, mock_load, mock_get, mock_persist):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        # Clear the cache for this test
        mod._geocode_cache.clear()

        mock_resp = mock.MagicMock()
        mock_resp.json.return_value = [{"lat": "40.7128", "lon": "-74.0060", "address": {"country": "United States"}}]
        mock_get.return_value = mock_resp

        result = mod._geocode_address("New York")
        assert result == (40.7128, -74.006)
        assert "New York" in mod._geocode_cache
        mock_persist.assert_called_once()

    @mock.patch(f"{_R}._load_geocode_cache")
    def test_cached_result(self, mock_load):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._geocode_cache["Cached City"] = (10.0, 20.0, "Some Country")
        result = mod._geocode_address("Cached City")
        assert result == (10.0, 20.0)

    @mock.patch(f"{_R}._load_geocode_cache")
    def test_cached_none_returns_none(self, mock_load):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._geocode_cache["Bad Place"] = None
        result = mod._geocode_address("Bad Place")
        assert result is None

    @mock.patch(f"{_R}._load_geocode_cache")
    def test_empty_address_returns_none(self, mock_load):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        assert mod._geocode_address("") is None
        assert mod._geocode_address(None) is None

    @mock.patch(f"{_R}.http_requests.get")
    @mock.patch(f"{_R}._load_geocode_cache")
    def test_max_new_geocodes_cap(self, mock_load, mock_get):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._geocode_cache.clear()
        counter = [mod._MAX_NEW_GEOCODES]  # already at cap
        result = mod._geocode_address("Somewhere New", counter)
        assert result is None
        mock_get.assert_not_called()

    @mock.patch(f"{_R}.http_requests.get")
    @mock.patch(f"{_R}._load_geocode_cache")
    def test_api_error_caches_none(self, mock_load, mock_get):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._geocode_cache.clear()
        mock_get.side_effect = Exception("timeout")
        result = mod._geocode_address("Error City")
        assert result is None
        assert mod._geocode_cache["Error City"] is None


# ===========================================================================
# _reverse_geocode_admin1
# ===========================================================================


class TestReverseGeocodeAdmin1:

    @mock.patch(f"{_R}._persist_rev_geocode_cache")
    @mock.patch(f"{_R}.http_requests.get")
    @mock.patch(f"{_R}._load_rev_geocode_cache")
    def test_successful_reverse(self, mock_load, mock_get, mock_persist):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._rev_geocode_cache.clear()
        mock_resp = mock.MagicMock()
        mock_resp.json.return_value = {"address": {"state": "California", "country": "United States"}}
        mock_get.return_value = mock_resp

        result = mod._reverse_geocode_admin1(34.0522, -118.2437)
        assert result == "California"

    @mock.patch(f"{_R}._load_rev_geocode_cache")
    def test_cached_result(self, mock_load):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._rev_geocode_cache["34.052,-118.244"] = "California"
        result = mod._reverse_geocode_admin1(34.052, -118.244)
        assert result == "California"

    @mock.patch(f"{_R}.http_requests.get")
    @mock.patch(f"{_R}._load_rev_geocode_cache")
    def test_max_geocodes_cap(self, mock_load, mock_get):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._rev_geocode_cache.clear()
        counter = [mod._MAX_NEW_GEOCODES]
        result = mod._reverse_geocode_admin1(0.0, 0.0, counter)
        assert result is None

    @mock.patch(f"{_R}.http_requests.get")
    @mock.patch(f"{_R}._load_rev_geocode_cache")
    def test_api_failure_caches_none(self, mock_load, mock_get):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._rev_geocode_cache.clear()
        mock_get.side_effect = Exception("network error")
        result = mod._reverse_geocode_admin1(10.0, 20.0)
        assert result is None
        cache_key = "10.0,20.0"
        assert mod._rev_geocode_cache[cache_key] is None


# ===========================================================================
# _load_geocode_cache
# ===========================================================================


class TestLoadGeocodeCache:

    @mock.patch("builtins.open", mock.mock_open(read_data='{"Paris": [48.8566, 2.3522, "France"]}'))
    @mock.patch("os.path.exists", return_value=True)
    def test_loads_from_disk(self, mock_exists):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._geocode_cache.clear()
        mod._geocode_cache_loaded = False
        mod._load_geocode_cache()
        assert "Paris" in mod._geocode_cache
        # Reset for other tests
        mod._geocode_cache_loaded = False

    @mock.patch("os.path.exists", return_value=False)
    def test_no_file_sets_loaded_flag(self, mock_exists):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._geocode_cache_loaded = False
        mod._load_geocode_cache()
        assert mod._geocode_cache_loaded is True
        mod._geocode_cache_loaded = False

    def test_idempotent_when_already_loaded(self):
        import asset_management.web.blueprints.dashboard_builder.routes as mod

        mod._geocode_cache_loaded = True
        # Should not attempt file I/O; just return
        mod._load_geocode_cache()
        mod._geocode_cache_loaded = False


# ===========================================================================
# GET /dashboard-builder/api/map-data
# ===========================================================================


class TestMapDataRoute:

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_unknown_statistic(self, mock_es, auth_client):
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=nonexistent")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_es_unavailable(self, mock_es, auth_client):
        mock_es.return_value = None
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=reach")
        assert resp.status_code == 503

    @mock.patch(f"{_R}._query_fb_country_map", return_value=[{"name": "Egypt", "iso2": "EG", "value": 100}])
    @mock.patch(f"{_R}._query_fb_region_map", return_value=[])
    @mock.patch(f"{_R}._query_fb_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_fb_insights_returns_country_render_only(self, mock_es, mock_fb, mock_region, mock_country, auth_client):
        """/api/map-data is the country-render endpoint and no
        longer carries FB cities. _query_fb_map (live geocoding) must not
        be invoked from this path. The legacy `points` alias is gone."""
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=reach")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["cities"] == []
        assert data["countries"] == [{"name": "Egypt", "iso2": "EG", "value": 100}]
        assert "points" not in data
        mock_fb.assert_not_called()

    @mock.patch(
        f"{_R}._query_echo_country_map", return_value=[{"name": "India", "iso2": None, "lat": None, "lon": None, "value": 50}]
    )
    @mock.patch(f"{_R}._query_echo_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_echo_data_source(self, mock_es, mock_echo, mock_echo_country, auth_client):
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=messages")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["countries"]) == 1

    @mock.patch(f"{_R}._query_dt_team_country_map", return_value=[])
    @mock.patch(f"{_R}._query_dt_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_dt_data_source(self, mock_es, mock_dt, mock_dt_country, auth_client):
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=baptisms")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    @mock.patch(f"{_R}._query_google_ads_installs_geo_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_google_ads_app_installs(self, mock_es, mock_gads, auth_client):
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=google_ads_app_installs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    @mock.patch(f"{_R}._query_team_country_map_generic", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_fb_page_insights_data_source(self, mock_es, mock_generic, auth_client):
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=organic_facebook")
        assert resp.status_code == 200

    @mock.patch(f"{_R}._query_ga4_country_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_ga_metrics_data_source(self, mock_es, mock_ga4, auth_client):
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=organic_web")
        assert resp.status_code == 200

    @mock.patch(f"{_R}._query_team_title_country_map", return_value=[])
    @mock.patch(f"{_R}._query_youtube_country_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_youtube_data_source_fallback(self, mock_es, mock_yt, mock_tt, auth_client):
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=yt_reactions")
        assert resp.status_code == 200
        # Should have called fallback
        mock_tt.assert_called_once()

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/dashboard-builder/api/map-data?statistic=reach")
        assert resp.status_code == 401


# ===========================================================================
# Multi-data-view table query (via POST /api/query)
# ===========================================================================


class TestMultiDvTableQuery:

    @mock.patch(f"{_R}._get_agg_value", return_value=42)
    @mock.patch(f"{_R}._safe_agg_key", return_value="metric_insight_reach")
    @mock.patch(f"{_R}.build_query")
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_multi_dv_table_route(self, mock_es, mock_build, mock_agg_key, mock_agg_val, auth_client):
        es_mock = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {
            "aggregations": {
                "table_buckets": {
                    "buckets": [
                        {
                            "key": {"team_title.keyword": "Team A"},
                            "doc_count": 10,
                            "metric_insight_reach": {"value": 42},
                        }
                    ]
                }
            }
        }
        es_mock.search.return_value = es_resp
        es_mock.indices.get_mapping.return_value = {}
        mock_es.return_value = es_mock
        mock_build.return_value = {"size": 0}

        resp = auth_client.post(
            "/dashboard-builder/api/query",
            json={
                "chart_type": "table",
                "data_view": "fb_insights",
                "x_field": "team_title.keyword",
                "table_columns": [
                    {"field": "insight_reach", "data_view": "fb_insights"},
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "columns" in data
        assert "rows" in data


# ===========================================================================
# POST /dashboard-builder/api/dashboards/<id>/export
# ===========================================================================


class TestExportDashboard:

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_not_found(self, mock_get, mock_es, auth_client):
        mock_get.return_value = None
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/no-dash/export",
            json={"format": "csv"},
        )
        assert resp.status_code == 404

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_non_echarts_rejects(self, mock_get, mock_es, auth_client):
        mock_get.return_value = {"dashboard_type": "kibana", "title": "K"}
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/kib-1/export",
            json={"format": "csv"},
        )
        assert resp.status_code == 400

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_unsupported_format(self, mock_get, mock_es, auth_client):
        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {"charts": []},
        }
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={"format": "pdf"},
        )
        assert resp.status_code == 400

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_es_unavailable(self, mock_get, mock_es, auth_client):
        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {"charts": [{"chart_id": "c1", "chart_type": "bar", "data_view": "fb"}]},
        }
        mock_es.return_value = None
        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={"format": "csv"},
        )
        assert resp.status_code == 503

    @mock.patch(f"{_R}.transform")
    @mock.patch(f"{_R}.build_query")
    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_csv_export_success(self, mock_get, mock_es, mock_build, mock_transform, auth_client):
        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [
                    {
                        "chart_id": "c1",
                        "chart_type": "bar",
                        "data_view": "fb_insights",
                        "x_field": "kibana_date",
                        "y_field": "insight_reach",
                    }
                ]
            },
        }
        es_mock = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {"aggregations": {}}
        es_mock.search.return_value = es_resp
        es_mock.indices.get_mapping.return_value = {}
        mock_es.return_value = es_mock
        mock_build.return_value = {"size": 0}
        mock_transform.return_value = {
            "xAxis": {"data": ["Jan"]},
            "series": [{"name": "Reach", "data": [100]}],
        }

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={"format": "csv", "title": "Test Report"},
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.content_type
        assert b"Test Report" in resp.data

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_no_exportable_data(self, mock_get, mock_es, auth_client):
        # a dashboard with no tabular charts is now refused by the
        # capability gate up front, with a message naming the reason (instead
        # of the late generic "No exportable chart data" 400).
        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {"charts": []},
        }
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={"format": "csv"},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "PDF" in data["error"]

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_no_exportable_data_tabular_chart_empty(self, mock_get, mock_es, auth_client):
        # the generic no-data 400 is still reachable when the
        # dashboard IS tabular-capable but its charts yield no sheets (here:
        # a table chart whose query errors out and is skipped).
        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {"charts": [{"chart_type": "table", "title": "T"}]},
        }
        mock_es.return_value = mock.MagicMock()
        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={"format": "csv"},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "No exportable" in data["error"]

    def test_unauthenticated_redirects(self, client):
        resp = client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={"format": "csv"},
        )
        assert resp.status_code == 401
