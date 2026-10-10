"""
Additional tests for dashboard builder map-related and utility functions.

Covers functions NOT already tested in test_dashboard_builder.py or
test_dashboard_builder_advanced.py:

  - _persist_geocode_cache / _persist_rev_geocode_cache
  - _load_rev_geocode_cache
  - _load_dt_grid_names
  - _geocode_dt_grid
  - _dt_map_query_body
  - _load_dt_team_country_map
  - _query_dt_team_country_map
  - _query_dt_country_map
  - _query_dt_map
  - _query_team_title_country_map
  - _enrich_y_field_types
  - _export_grouped_table
  - _run_table_multi_dv_query (unit-level, additional scenarios)
  - Export route edge cases (xlsx, trend_line, chart_visibility, grouped_table)
"""

from unittest import mock

import pytest

from asset_management.web.blueprints.dashboard_builder.routes import (
    _dt_map_query_body,
    _enrich_y_field_types,
)

_R = "asset_management.web.blueprints.dashboard_builder.routes"


# ===========================================================================
# _persist_geocode_cache
# ===========================================================================


class TestPersistGeocodeCache:

    # persist goes through _atomic_json_dump (temp + os.replace)
    # instead of a bare truncate-and-write, so concurrent writers can't
    # corrupt the cache file. Pin the call and the serialisation contract.
    @mock.patch(f"{_R}._atomic_json_dump")
    def test_writes_serialisable_data(self, mock_dump):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        orig = dict(mod._geocode_cache)
        mod._geocode_cache.clear()
        mod._geocode_cache["London"] = (51.5, -0.1, "UK")
        mod._geocode_cache["Nowhere"] = None
        mod._persist_geocode_cache()
        mock_dump.assert_called_once()
        path, payload = mock_dump.call_args.args
        assert path == mod._geocode_cache_path
        # Tuples become lists; None entries survive as None.
        assert payload == {"London": [51.5, -0.1, "UK"], "Nowhere": None}
        mod._geocode_cache.clear()
        mod._geocode_cache.update(orig)

    @mock.patch("builtins.open", side_effect=PermissionError("denied"))
    def test_exception_is_swallowed(self, mock_open):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        # Should not raise
        mod._persist_geocode_cache()


# ===========================================================================
# _persist_rev_geocode_cache
# ===========================================================================


class TestPersistRevGeocodeCache:

    # same atomic-dump pin as TestPersistGeocodeCache.
    @mock.patch(f"{_R}._atomic_json_dump")
    def test_writes_data(self, mock_dump):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        orig = dict(mod._rev_geocode_cache)
        mod._rev_geocode_cache.clear()
        mod._rev_geocode_cache["30.0,31.2"] = "Cairo Governorate"
        mod._persist_rev_geocode_cache()
        mock_dump.assert_called_once()
        path, payload = mock_dump.call_args.args
        assert path == mod._rev_geocode_cache_path
        assert payload == {"30.0,31.2": "Cairo Governorate"}
        mod._rev_geocode_cache.clear()
        mod._rev_geocode_cache.update(orig)

    @mock.patch("builtins.open", side_effect=IOError("disk full"))
    def test_exception_is_swallowed(self, mock_open):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._persist_rev_geocode_cache()


# ===========================================================================
# _load_rev_geocode_cache
# ===========================================================================


class TestLoadRevGeocodeCache:

    @mock.patch("builtins.open", mock.mock_open(read_data='{"30.0,31.2": "Cairo"}'))
    @mock.patch("os.path.exists", return_value=True)
    def test_loads_from_disk(self, mock_exists):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._rev_geocode_cache.clear()
        mod._rev_geocode_cache_loaded = False
        mod._load_rev_geocode_cache()
        assert "30.0,31.2" in mod._rev_geocode_cache
        assert mod._rev_geocode_cache["30.0,31.2"] == "Cairo"
        mod._rev_geocode_cache_loaded = False

    @mock.patch("os.path.exists", return_value=False)
    def test_no_file_sets_loaded_flag(self, mock_exists):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._rev_geocode_cache_loaded = False
        mod._load_rev_geocode_cache()
        assert mod._rev_geocode_cache_loaded is True
        mod._rev_geocode_cache_loaded = False

    def test_idempotent_when_already_loaded(self):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._rev_geocode_cache_loaded = True
        mod._load_rev_geocode_cache()  # Should not do file I/O
        mod._rev_geocode_cache_loaded = False

    @mock.patch("builtins.open", side_effect=Exception("corrupt"))
    @mock.patch("os.path.exists", return_value=True)
    def test_corrupt_file_is_handled(self, mock_exists, mock_open):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._rev_geocode_cache_loaded = False
        mod._load_rev_geocode_cache()  # Should not raise
        assert mod._rev_geocode_cache_loaded is True
        mod._rev_geocode_cache_loaded = False


# ===========================================================================
# _load_dt_grid_names
# ===========================================================================


class TestLoadDtGridNames:

    @mock.patch("builtins.open", mock.mock_open(read_data='{"100012345": "Nairobi"}'))
    @mock.patch("os.path.exists", return_value=True)
    def test_loads_from_disk_cache(self, mock_exists):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._dt_grid_names = None
        result = mod._load_dt_grid_names()
        assert result["100012345"] == "Nairobi"
        mod._dt_grid_names = None

    def test_returns_cached_value(self):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._dt_grid_names = {"999": "TestCity"}
        result = mod._load_dt_grid_names()
        assert result["999"] == "TestCity"
        mod._dt_grid_names = None

    @mock.patch(f"{_R}.http_requests.get")
    @mock.patch("os.path.exists", return_value=False)
    def test_downloads_from_github(self, mock_exists, mock_get):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._dt_grid_names = None
        mock_resp = mock.MagicMock()
        mock_resp.text = "  - Nairobi (100012345)\n  - Kampala (100099999)\n"
        mock_get.return_value = mock_resp
        with mock.patch("builtins.open", mock.mock_open()):
            result = mod._load_dt_grid_names()
        assert result.get("100012345") == "Nairobi"
        assert result.get("100099999") == "Kampala"
        mod._dt_grid_names = None

    @mock.patch(f"{_R}.http_requests.get", side_effect=Exception("timeout"))
    @mock.patch("os.path.exists", return_value=False)
    def test_github_failure_returns_empty(self, mock_exists, mock_get):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._dt_grid_names = None
        result = mod._load_dt_grid_names()
        assert result == {}
        mod._dt_grid_names = None


# ===========================================================================
# _geocode_dt_grid
# ===========================================================================


class TestGeocodeDtGrid:

    @mock.patch(f"{_R}._geocode_address", return_value=(1.3, 36.8))
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={"555": "Nairobi"})
    def test_known_grid_id(self, mock_names, mock_geo):
        from asset_management.web.blueprints.dashboard_builder.routes import _geocode_dt_grid
        result = _geocode_dt_grid("555")
        assert result == (1.3, 36.8)
        mock_geo.assert_called_once_with("Nairobi")

    @mock.patch(f"{_R}._load_dt_grid_names", return_value={})
    def test_unknown_grid_id(self, mock_names):
        from asset_management.web.blueprints.dashboard_builder.routes import _geocode_dt_grid
        result = _geocode_dt_grid("99999")
        assert result is None

    @mock.patch(f"{_R}._geocode_address", return_value=None)
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={"123": "Unknown Place"})
    def test_geocode_fails(self, mock_names, mock_geo):
        from asset_management.web.blueprints.dashboard_builder.routes import _geocode_dt_grid
        result = _geocode_dt_grid("123")
        assert result is None


# ===========================================================================
# _dt_map_query_body
# ===========================================================================


class TestDtMapQueryBody:

    def test_empty_must(self):
        body = _dt_map_query_body([])
        assert body["query"] == {"match_all": {}}
        assert "by_address" in body["aggs"]
        assert "by_grid" in body["aggs"]
        assert body["size"] == 0

    def test_with_must_clauses(self):
        must = [{"term": {"status": "active"}}]
        body = _dt_map_query_body(must)
        assert body["query"]["bool"]["must"] == must

    def test_address_only_excludes_grid(self):
        body = _dt_map_query_body([], address_only=True)
        assert "by_address" in body["aggs"]
        assert "by_grid" not in body["aggs"]

    def test_extra_aggs_merged(self):
        extra = {"total_count": {"value_count": {"field": "_id"}}}
        body = _dt_map_query_body([], extra_aggs=extra)
        assert "total_count" in body["aggs"]
        assert "by_address" in body["aggs"]

    def test_address_only_with_extra_aggs(self):
        extra = {"my_metric": {"sum": {"field": "amount"}}}
        body = _dt_map_query_body([], extra_aggs=extra, address_only=True)
        assert "by_grid" not in body["aggs"]
        assert "my_metric" in body["aggs"]


# ===========================================================================
# _load_dt_team_country_map
# ===========================================================================


class TestLoadDtTeamCountryMap:

    def _make_es_mock(self, hits):
        es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {"hits": {"hits": hits}}
        es.search.return_value = resp
        return es

    def test_basic_mapping(self):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

        es = self._make_es_mock([
            {"_source": {"team_title": "Team Alpha", "team_country_text": "US, GB"}},
            {"_source": {"team_title": "Team Beta", "team_country_text": "EG"}},
        ])
        result = mod._load_dt_team_country_map(es)
        assert result["Team Alpha"] == "US"
        assert result["Team Beta"] == "EG"
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

    def test_list_country(self):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

        es = self._make_es_mock([
            {"_source": {"team_title": "Team List", "team_country_text": ["TR", "US"]}},
        ])
        result = mod._load_dt_team_country_map(es)
        assert result["Team List"] == "TR"
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

    def test_missing_title_skipped(self):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

        es = self._make_es_mock([
            {"_source": {"team_country_text": "US"}},  # no team_title
        ])
        result = mod._load_dt_team_country_map(es)
        assert result == {}
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

    def test_missing_country_skipped(self):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

        es = self._make_es_mock([
            {"_source": {"team_title": "No Country"}},
        ])
        result = mod._load_dt_team_country_map(es)
        assert "No Country" not in result
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

    def test_cache_ttl_returns_cached(self):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        import time as t
        mod._team_country_map_cache = {"Cached Team": "JP"}
        mod._team_country_map_cache_ts = t.time()  # just now

        es = mock.MagicMock()
        result = mod._load_dt_team_country_map(es)
        assert result == {"Cached Team": "JP"}
        es.search.assert_not_called()
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

    def test_es_failure_returns_empty(self):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

        es = mock.MagicMock()
        es.search.side_effect = Exception("connection refused")
        result = mod._load_dt_team_country_map(es)
        assert result == {}
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

    def test_team_country_fallback_field(self):
        """Uses team_country when team_country_text is absent."""
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0

        es = self._make_es_mock([
            {"_source": {"team_title": "Fallback", "team_country": "DE"}},
        ])
        result = mod._load_dt_team_country_map(es)
        assert result["Fallback"] == "DE"
        mod._team_country_map_cache = {}
        mod._team_country_map_cache_ts = 0.0


# ===========================================================================
# _query_dt_team_country_map
# ===========================================================================


class TestQueryDtTeamCountryMap:

    def _make_es_mock(self, team_buckets):
        # the dual-field team_title agg queries BOTH team_title.keyword
        # and bare team_title and merges. A real cluster returns buckets under
        # exactly one mapping; mirror that (buckets for .keyword, empty for bare)
        # so the merge isn't a double-count.
        es = mock.MagicMock()

        def _search(index=None, body=None, ignore_unavailable=False):
            field = body["aggs"]["by_team"]["terms"]["field"]
            buckets = team_buckets if field == "team_title.keyword" else []
            resp = mock.MagicMock()
            resp.body = {"aggregations": {"by_team": {"buckets": buckets}}}
            return resp

        es.search.side_effect = _search
        return es

    @mock.patch(f"{_R}._load_dt_team_country_map")
    @mock.patch(f"{_R}._dt_date_filter", return_value=None)
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    def test_basic_aggregation(self, mock_otf, mock_sf, mock_df, mock_tcm):
        from asset_management.web.blueprints.dashboard_builder.routes import _query_dt_team_country_map
        mock_tcm.return_value = {"Team A": "US", "Team B": "EG"}
        es = self._make_es_mock([
            {"key": "Team A", "doc_count": 10},
            {"key": "Team B", "doc_count": 20},
        ])
        result = _query_dt_team_country_map(es, {}, "", [], "", "")
        countries = {r["name"]: r["value"] for r in result}
        assert countries["US"] == 10
        assert countries["EG"] == 20

    @mock.patch(f"{_R}._load_dt_team_country_map", return_value={})
    @mock.patch(f"{_R}._dt_date_filter", return_value=None)
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    def test_no_team_map_returns_empty(self, mock_otf, mock_sf, mock_df, mock_tcm):
        from asset_management.web.blueprints.dashboard_builder.routes import _query_dt_team_country_map
        es = mock.MagicMock()
        result = _query_dt_team_country_map(es, {}, "", [], "", "")
        assert result == []

    @mock.patch(f"{_R}._load_dt_team_country_map")
    @mock.patch(f"{_R}._dt_date_filter", return_value=None)
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    def test_unmapped_team_excluded(self, mock_otf, mock_sf, mock_df, mock_tcm):
        from asset_management.web.blueprints.dashboard_builder.routes import _query_dt_team_country_map
        mock_tcm.return_value = {"Team A": "US"}
        es = self._make_es_mock([
            {"key": "Team A", "doc_count": 5},
            {"key": "Team Unknown", "doc_count": 15},
        ])
        result = _query_dt_team_country_map(es, {}, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "US"

    @mock.patch(f"{_R}._load_dt_team_country_map")
    @mock.patch(f"{_R}._dt_date_filter", return_value=None)
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    def test_keyword_fallback_on_exception(self, mock_otf, mock_sf, mock_df, mock_tcm):
        """When first search (team_title.keyword) fails, falls back to team_title."""
        from asset_management.web.blueprints.dashboard_builder.routes import _query_dt_team_country_map
        mock_tcm.return_value = {"Team A": "US"}
        es = mock.MagicMock()

        # First call raises, second succeeds
        fallback_resp = mock.MagicMock()
        fallback_resp.body = {
            "aggregations": {"by_team": {"buckets": [{"key": "Team A", "doc_count": 7}]}}
        }
        es.search.side_effect = [Exception("mapping error"), fallback_resp]
        result = _query_dt_team_country_map(es, {}, "", [], "", "")
        assert len(result) == 1
        assert result[0]["value"] == 7

    @mock.patch(f"{_R}._load_dt_team_country_map")
    @mock.patch(f"{_R}._dt_date_filter", return_value=None)
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    def test_both_searches_fail(self, mock_otf, mock_sf, mock_df, mock_tcm):
        from asset_management.web.blueprints.dashboard_builder.routes import _query_dt_team_country_map
        mock_tcm.return_value = {"Team A": "US"}
        es = mock.MagicMock()
        es.search.side_effect = Exception("total failure")
        result = _query_dt_team_country_map(es, {}, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_team_title_country_map
# ===========================================================================


class TestQueryTeamTitleCountryMap:

    @mock.patch(f"{_R}._load_dt_team_country_map")
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    @mock.patch(f"{_R}._build_date_filter", return_value=None)
    def test_basic_aggregation(self, mock_df, mock_otf, mock_sf, mock_tcm):
        from asset_management.web.blueprints.dashboard_builder.routes import _query_team_title_country_map
        mock_tcm.return_value = {"Team A": "US", "Team B": "EG"}

        # dual-field agg — buckets under.keyword, empty under bare,
        # so the merge across both fields doesn't double-count.
        es = mock.MagicMock()

        def _search(index=None, body=None, ignore_unavailable=False):
            field = body["aggs"]["by_team"]["terms"]["field"]
            buckets = (
                [
                    {"key": "Team A", "doc_count": 5, "total": {"value": 100}},
                    {"key": "Team B", "doc_count": 3, "total": {"value": 200}},
                ]
                if field == "team_title.keyword"
                else []
            )
            resp = mock.MagicMock()
            resp.body = {"aggregations": {"by_team": {"buckets": buckets}}}
            return resp

        es.search.side_effect = _search

        result = _query_team_title_country_map(es, "youtube_analytics", {"field": "views"}, "", [], "", "")
        countries = {r["name"]: r["value"] for r in result}
        assert countries["US"] == 100
        assert countries["EG"] == 200

    @mock.patch(f"{_R}._load_dt_team_country_map", return_value={})
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    @mock.patch(f"{_R}._build_date_filter", return_value=None)
    def test_empty_team_map(self, mock_df, mock_otf, mock_sf, mock_tcm):
        from asset_management.web.blueprints.dashboard_builder.routes import _query_team_title_country_map
        es = mock.MagicMock()
        result = _query_team_title_country_map(es, "yt*", {}, "", [], "", "")
        assert result == []


# ===========================================================================
# _enrich_y_field_types
# ===========================================================================


class TestEnrichYFieldTypes:

    def test_adds_missing_types(self):
        es = mock.MagicMock()
        es.indices.get_mapping.return_value = {
            "idx": {"mappings": {"properties": {"reach": {"type": "long"}}}}
        }
        config = {"data_view": "idx", "y_field": "reach"}
        _enrich_y_field_types(es, config)
        assert config["y_field_types"]["reach"] == "numeric"

    def test_preserves_existing_types(self):
        es = mock.MagicMock()
        es.indices.get_mapping.return_value = {
            "idx": {"mappings": {"properties": {"reach": {"type": "long"}}}}
        }
        config = {"data_view": "idx", "y_field": "reach", "y_field_types": {"reach": "custom"}}
        _enrich_y_field_types(es, config)
        assert config["y_field_types"]["reach"] == "custom"

    def test_no_field_types_returns(self):
        es = mock.MagicMock()
        es.indices.get_mapping.side_effect = Exception("no index")
        config = {"data_view": "missing", "y_field": "x"}
        _enrich_y_field_types(es, config)
        # Should not crash, no y_field_types key added
        assert config.get("y_field_types") is None

    def test_multiple_y_fields(self):
        es = mock.MagicMock()
        es.indices.get_mapping.return_value = {
            "idx": {
                "mappings": {
                    "properties": {
                        "reach": {"type": "long"},
                        "clicks": {"type": "integer"},
                    }
                }
            }
        }
        config = {"data_view": "idx", "y_fields": ["reach", "clicks"]}
        _enrich_y_field_types(es, config)
        assert config["y_field_types"]["reach"] == "numeric"
        assert config["y_field_types"]["clicks"] == "numeric"


# ===========================================================================
# _export_grouped_table
# ===========================================================================


class TestExportGroupedTable:

    @mock.patch(f"{_R}.transform")
    @mock.patch(f"{_R}.build_query", return_value={"size": 0})
    @mock.patch(f"{_R}._enrich_y_field_types")
    def test_basic_export(self, mock_enrich, mock_build, mock_transform):
        from asset_management.web.blueprints.dashboard_builder.routes import _export_grouped_table

        mock_transform.return_value = {"value": 42}
        es = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {}
        es.search.return_value = es_resp

        config = {
            "groups": [
                {
                    "label": "Group A",
                    "columns": [
                        {
                            "label": "Metric 1",
                            "aggregation": "sum",
                            "data_sources": [{"data_view": "fb_insights", "field": "reach"}],
                        }
                    ],
                }
            ]
        }
        result = _export_grouped_table(es, config)
        assert result["columns"] == ["Group", "Column", "Value"]
        assert len(result["rows"]) == 1
        assert result["rows"][0][0] == "Group A"
        assert result["rows"][0][1] == "Metric 1"
        assert result["rows"][0][2] == 42

    def test_empty_groups(self):
        from asset_management.web.blueprints.dashboard_builder.routes import _export_grouped_table
        es = mock.MagicMock()
        result = _export_grouped_table(es, {"groups": []})
        assert result["rows"] == []
        assert result["columns"] == []

    @mock.patch(f"{_R}.transform")
    @mock.patch(f"{_R}.build_query", return_value={"size": 0})
    @mock.patch(f"{_R}._enrich_y_field_types")
    def test_multiple_data_sources_summed(self, mock_enrich, mock_build, mock_transform):
        from asset_management.web.blueprints.dashboard_builder.routes import _export_grouped_table

        mock_transform.side_effect = [{"value": 10}, {"value": 30}]
        es = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {}
        es.search.return_value = es_resp

        config = {
            "groups": [
                {
                    "label": "G1",
                    "columns": [
                        {
                            "label": "Total",
                            "data_sources": [
                                {"data_view": "fb_insights", "field": "reach"},
                                {"data_view": "google_ads", "field": "clicks"},
                            ],
                        }
                    ],
                }
            ]
        }
        result = _export_grouped_table(es, config)
        assert result["rows"][0][2] == 40

    @mock.patch(f"{_R}.transform", side_effect=Exception("query failed"))
    @mock.patch(f"{_R}.build_query", return_value={"size": 0})
    @mock.patch(f"{_R}._enrich_y_field_types")
    def test_source_error_graceful(self, mock_enrich, mock_build, mock_transform):
        from asset_management.web.blueprints.dashboard_builder.routes import _export_grouped_table
        es = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {}
        es.search.return_value = es_resp

        config = {
            "groups": [
                {
                    "label": "G1",
                    "columns": [
                        {
                            "label": "Bad",
                            "data_sources": [{"data_view": "fb", "field": "x"}],
                        }
                    ],
                }
            ]
        }
        result = _export_grouped_table(es, config)
        # Should still produce a row with value 0
        assert result["rows"][0][2] == 0


# ===========================================================================
# _query_dt_map
# ===========================================================================


class TestQueryDtMap:

    @mock.patch(f"{_R}._geocode_address")
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={"111": "Nairobi"})
    @mock.patch(f"{_R}._dt_date_filter", return_value=None)
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    def test_grid_and_address_points(self, mock_otf, mock_sf, mock_df, mock_names, mock_geo):
        from asset_management.web.blueprints.dashboard_builder.routes import _query_dt_map

        mock_geo.side_effect = lambda addr, *a: (1.3, 36.8) if addr == "Nairobi" else (30.0, 31.2)

        es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_grid": {"buckets": [{"key": "111", "doc_count": 5}]},
                "by_address": {"buckets": [{"key": "Cairo, Egypt", "doc_count": 3}]},
            }
        }
        es.search.return_value = resp

        result = _query_dt_map(es, {}, "", [], "", "")
        assert len(result) == 2
        names = [p["name"] for p in result]
        assert "Nairobi" in names
        assert "Cairo, Egypt" in names

    @mock.patch(f"{_R}._geocode_address", return_value=None)
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={})
    @mock.patch(f"{_R}._dt_date_filter", return_value=None)
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    def test_no_geocoded_results(self, mock_otf, mock_sf, mock_df, mock_names, mock_geo):
        from asset_management.web.blueprints.dashboard_builder.routes import _query_dt_map

        es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {"aggregations": {"by_grid": {"buckets": []}, "by_address": {"buckets": []}}}
        es.search.return_value = resp

        result = _query_dt_map(es, {}, "", [], "", "")
        assert result == []

    @mock.patch(f"{_R}._geocode_address")
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={"111": "Same Place"})
    @mock.patch(f"{_R}._dt_date_filter", return_value=None)
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    def test_deduplicates_coordinates(self, mock_otf, mock_sf, mock_df, mock_names, mock_geo):
        from asset_management.web.blueprints.dashboard_builder.routes import _query_dt_map

        # Both grid and address resolve to same coords
        mock_geo.return_value = (1.0, 2.0)
        es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_grid": {"buckets": [{"key": "111", "doc_count": 5}]},
                "by_address": {"buckets": [{"key": "Same Place", "doc_count": 3}]},
            }
        }
        es.search.return_value = resp

        result = _query_dt_map(es, {}, "", [], "", "")
        # Should deduplicate to 1 point
        assert len(result) == 1


# ===========================================================================
# _query_dt_country_map
# ===========================================================================


class TestQueryDtCountryMap:

    @mock.patch("lib.geocoding.country_from_coords", return_value="KE")
    @mock.patch(f"{_R}._geocode_address")
    @mock.patch(f"{_R}._load_geocode_cache")
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={"111": "Nairobi"})
    @mock.patch(f"{_R}._dt_date_filter", return_value=None)
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    def test_aggregates_by_country(self, mock_otf, mock_sf, mock_df, mock_names, mock_load, mock_geo, mock_country):
        import asset_management.web.blueprints.dashboard_builder.routes as mod
        from asset_management.web.blueprints.dashboard_builder.routes import _query_dt_country_map

        mod._geocode_cache["Nairobi"] = (1.3, 36.8, "Kenya")

        es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_grid": {"buckets": [{"key": "111", "doc_count": 10}]},
                "by_address": {"buckets": []},
            }
        }
        es.search.return_value = resp

        result = _query_dt_country_map(es, {}, "", [], "", "")
        # Result is keyed by ISO2 (resolved via country_from_coords), not country name
        countries = {r["name"]: r["value"] for r in result}
        assert countries.get("KE") == 10

    @mock.patch(f"{_R}._dt_date_filter", return_value=None)
    @mock.patch(f"{_R}._build_stat_filter", return_value=None)
    @mock.patch(f"{_R}._org_team_filters", return_value=[])
    def test_es_failure_returns_empty(self, mock_otf, mock_sf, mock_df):
        from asset_management.web.blueprints.dashboard_builder.routes import _query_dt_country_map
        es = mock.MagicMock()
        es.search.side_effect = Exception("timeout")
        result = _query_dt_country_map(es, {}, "", [], "", "")
        assert result == []


# ===========================================================================
# Export route edge cases (xlsx, trend_line, chart_visibility, grouped_table)
# ===========================================================================


class TestExportEdgeCases:

    @mock.patch(f"{_R}.transform")
    @mock.patch(f"{_R}.build_query", return_value={"size": 0})
    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_xlsx_export_success(self, mock_get, mock_es, mock_build, mock_transform, auth_client):
        import openpyxl  # required dependency (in requirements.txt)

        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [
                    {
                        "chart_id": "c1",
                        "chart_type": "bar",
                        "data_view": "fb_insights",
                        "x_field": "kibana_date",
                        "y_field": "reach",
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
        mock_transform.return_value = {
            "xAxis": {"data": ["Jan"]},
            "series": [{"name": "Reach", "data": [100]}],
        }

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={"format": "xlsx", "title": "My Report"},
        )
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.content_type

    @mock.patch(f"{_R}.transform")
    @mock.patch(f"{_R}.build_query", return_value={"size": 0})
    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_xlsx_export_without_openpyxl(self, mock_get, mock_es, mock_build, mock_transform, auth_client):
        """When openpyxl is not installed, xlsx export returns 500 with helpful message."""
        import builtins
        import sys

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "openpyxl":
                raise ImportError("mocked: no openpyxl")
            return real_import(name, *args, **kwargs)

        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [
                    {
                        "chart_id": "c1",
                        "chart_type": "bar",
                        "data_view": "fb_insights",
                        "x_field": "kibana_date",
                        "y_field": "reach",
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
        mock_transform.return_value = {
            "xAxis": {"data": ["Jan"]},
            "series": [{"name": "Reach", "data": [100]}],
        }

        # If a previous test already imported openpyxl, it's cached in
        # sys.modules and our __import__ mock never fires. Evict the cached
        # module(s) so the import statement under test actually re-runs.
        cached_openpyxl_keys = [k for k in list(sys.modules) if k == "openpyxl" or k.startswith("openpyxl.")]
        cached_openpyxl_mods = {k: sys.modules.pop(k) for k in cached_openpyxl_keys}

        try:
            with mock.patch("builtins.__import__", side_effect=mock_import):
                resp = auth_client.post(
                    "/dashboard-builder/api/dashboards/d1/export",
                    json={"format": "xlsx"},
                )
        finally:
            # Restore the cached modules so subsequent tests aren't affected.
            sys.modules.update(cached_openpyxl_mods)

        assert resp.status_code == 500
        data = resp.get_json()
        # The route has an explicit `except ImportError` handler that returns
        # this hard-coded message — left it alone (it's not a leaked
        # exception text path; it's a static install-instruction message).
        assert "openpyxl" in data["error"]

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_trend_line_export(self, mock_get, mock_es, auth_client):
        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [
                    {
                        "chart_id": "tl1",
                        "chart_type": "trend_line",
                        "sources": [
                            {"label": "Reach", "data_view": "fb_insights", "field": "reach", "date_field": "kibana_date"},
                        ],
                        "date_interval": "month",
                    }
                ]
            },
        }
        es_mock = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {
            "aggregations": {
                "over_time": {
                    "buckets": [
                        {"key_as_string": "2026-01", "metric": {"value": 100}},
                        {"key_as_string": "2026-02", "metric": {"value": 200}},
                    ]
                }
            }
        }
        es_mock.search.return_value = es_resp
        es_mock.indices.get_mapping.return_value = {}
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={"format": "csv"},
        )
        assert resp.status_code == 200
        assert b"2026-01" in resp.data
        assert b"Reach" in resp.data

    @mock.patch(f"{_R}.transform")
    @mock.patch(f"{_R}.build_query", return_value={"size": 0})
    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_chart_visibility_filters_series(self, mock_get, mock_es, mock_build, mock_transform, auth_client):
        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [
                    {
                        "chart_id": "c1",
                        "chart_type": "bar",
                        "data_view": "fb_insights",
                        "x_field": "month",
                        "y_field": "reach",
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
        mock_transform.return_value = {
            "xAxis": {"data": ["Jan"]},
            "series": [
                {"name": "Reach", "data": [100]},
                {"name": "Clicks", "data": [50]},
            ],
        }

        # Only "Reach" is visible
        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={
                "format": "csv",
                "chart_visibility": {"c1": {"Reach": True, "Clicks": False}},
            },
        )
        assert resp.status_code == 200
        assert b"Reach" in resp.data
        # Clicks should be filtered out
        assert b"Clicks" not in resp.data

    @mock.patch(f"{_R}.transform")
    @mock.patch(f"{_R}.build_query", return_value={"size": 0})
    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_chart_visibility_all_unchecked_skips(self, mock_get, mock_es, mock_build, mock_transform, auth_client):
        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [
                    {
                        "chart_id": "c1",
                        "chart_type": "bar",
                        "data_view": "fb",
                        "x_field": "x",
                        "y_field": "y",
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
        mock_transform.return_value = {
            "xAxis": {"data": ["Jan"]},
            "series": [{"name": "S1", "data": [10]}],
        }

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={
                "format": "csv",
                "chart_visibility": {"c1": {"S1": False}},
            },
        )
        # All series unchecked, chart skipped, no exportable data
        assert resp.status_code == 400
        data = resp.get_json()
        assert "No exportable" in data["error"]

    @mock.patch(f"{_R}._export_grouped_table")
    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_grouped_table_export(self, mock_get, mock_es, mock_export_gt, auth_client):
        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [
                    {
                        "chart_id": "gt1",
                        "chart_type": "grouped_table",
                        "data_view": "fb_insights",
                        "groups": [
                            {
                                "label": "G1",
                                "columns": [{"label": "Col1", "data_sources": [{"data_view": "fb", "field": "x"}]}],
                            }
                        ],
                    }
                ]
            },
        }
        es_mock = mock.MagicMock()
        es_mock.indices.get_mapping.return_value = {}
        mock_es.return_value = es_mock
        mock_export_gt.return_value = {
            "chart_type": "table",
            "columns": ["Group", "Column", "Value"],
            "rows": [["G1", "Col1", 42]],
        }

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={"format": "csv"},
        )
        assert resp.status_code == 200
        assert b"G1" in resp.data
        assert b"42" in resp.data

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_export_with_org_team_filters(self, mock_get, mock_es, auth_client):
        """Export with org and team filters passes them through."""
        mock_get.return_value = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [
                    {
                        "chart_id": "tl1",
                        "chart_type": "trend_line",
                        "sources": [
                            {"label": "Src", "data_view": "fb_insights", "field": "reach", "date_field": "kibana_date"},
                        ],
                    }
                ]
            },
        }
        es_mock = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {
            "aggregations": {
                "over_time": {
                    "buckets": [{"key_as_string": "2026-01", "metric": {"value": 50}}]
                }
            }
        }
        es_mock.search.return_value = es_resp
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards/d1/export",
            json={
                "format": "csv",
                "org_filter": "Acme Corp",
                "team_filter": "Team Alpha",
            },
        )
        assert resp.status_code == 200
        # The ES search should have been called with filter clauses
        call_args = es_mock.search.call_args
        body = call_args.kwargs.get("body") or call_args[1].get("body", {})
        filters = body.get("query", {}).get("bool", {}).get("filter", [])
        filter_str = str(filters)
        assert "organization_title" in filter_str or "team_title" in filter_str
