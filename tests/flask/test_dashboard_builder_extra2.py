"""Additional tests for cardinality, google ads installs, multi-stat leaflet, time range."""
import os
import sys
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.blueprints.dashboard_builder.routes import (
    _query_echo_country_map,
    _query_google_ads_installs_geo_map,
    _load_dt_team_country_map,
)

BUILDER_PREFIX = "/dashboard-builder"


# ===========================================================================
# _query_echo_country_map cardinality_field branch (lines 966-1000)
# ===========================================================================

class TestQueryEchoCountryMapCardinality:
    def test_cardinality_contact_iso2_path(self):
        """Primary cardinality path: aggregate by contact_country_iso2."""
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "TR", "doc_count": 100, "total": {"value": 500}},
                    ]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"cardinality_field": "contact_phone"}
        result = _query_echo_country_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "TR"
        assert result[0]["iso2"] == "TR"
        assert result[0]["value"] == 500

    def test_cardinality_fallback_to_team_mapping(self):
        """Fallback cardinality path: team -> country mapping when iso2 data absent."""
        mock_es = mock.MagicMock()
        empty_resp = mock.MagicMock()
        empty_resp.body = {"aggregations": {"by_country": {"buckets": []}}}
        team_resp = mock.MagicMock()
        team_resp.body = {
            "aggregations": {
                "by_team": {
                    "buckets": [
                        {"key": "NFCY", "total": {"value": 500}},
                    ]
                }
            }
        }
        # First two calls (contact_country_iso2) return empty, third returns team data
        mock_es.search.side_effect = [empty_resp, empty_resp, team_resp]
        stat_cfg = {"cardinality_field": "contact_phone"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={"NFCY": "TR"},
        ):
            result = _query_echo_country_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "TR"
        assert result[0]["value"] == 500

    def test_cardinality_returns_empty_when_team_map_empty(self):
        mock_es = mock.MagicMock()
        empty_resp = mock.MagicMock()
        empty_resp.body = {"aggregations": {"by_country": {"buckets": []}}}
        mock_es.search.side_effect = [empty_resp, empty_resp]
        stat_cfg = {"cardinality_field": "contact_phone"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={},
        ):
            result = _query_echo_country_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_cardinality_returns_empty_when_no_buckets(self):
        mock_es = mock.MagicMock()
        empty_resp = mock.MagicMock()
        empty_resp.body = {"aggregations": {"by_country": {"buckets": []}}}
        team_resp = mock.MagicMock()
        team_resp.body = {"aggregations": {"by_team": {"buckets": []}}}
        mock_es.search.side_effect = [empty_resp, empty_resp, team_resp, team_resp]
        stat_cfg = {"cardinality_field": "contact_phone"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={"NFCY": "TR"},
        ):
            result = _query_echo_country_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_echo_country_min_value_filter(self):
        """min_country_value filter skips low-value buckets."""
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "IN", "doc_count": 3, "total": {"value": 2}},
                    ]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"field": "incomingMessagesCount", "type": "exists", "min_country_value": 5}
        result = _query_echo_country_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_echo_country_iso2_primary_path(self):
        """contact_country_iso2 bucket key is used directly as iso2."""
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "TR", "doc_count": 50, "total": {"value": 200}},
                    ]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"field": "incomingMessagesCount", "type": "exists"}
        result = _query_echo_country_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["iso2"] == "TR"
        assert result[0]["name"] == "TR"
        assert result[0]["value"] == 200

    def test_echo_country_with_date_filter(self):
        """date_filter appended when date_from provided."""
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "IN", "doc_count": 100, "total": {"value": 500}},
                    ]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"field": "incomingMessagesCount", "type": "exists"}
        result = _query_echo_country_map(mock_es, stat_cfg, "", [], "2024-01-01", "2024-12-31")
        assert len(result) == 1


# ===========================================================================
# _query_google_ads_installs_geo_map (lines 1081-1131)
# ===========================================================================

class TestQueryGoogleAdsInstallsGeoMap:
    def test_returns_country_points(self):
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "United States",
                        "doc_count": 100,
                        "total": {"value": 500},
                        "country_code": {"buckets": [{"key": "US"}]},
                    }]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"field": "app_installs", "type": "range", "gte": 0}
        result = _query_google_ads_installs_geo_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "United States"
        assert result[0]["iso2"] == "US"

    def test_skips_unknown_country(self):
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "unknown", "doc_count": 5, "total": {"value": 10},
                         "country_code": {"buckets": []}},
                    ]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"field": "app_installs", "type": "range", "gte": 0}
        result = _query_google_ads_installs_geo_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_skips_zero_value(self):
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "Brazil", "doc_count": 10, "total": {"value": 0},
                         "country_code": {"buckets": [{"key": "BR"}]}},
                    ]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"field": "app_installs", "type": "range", "gte": 0}
        result = _query_google_ads_installs_geo_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_returns_none_iso2_when_no_code_bucket(self):
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "Germany", "doc_count": 20, "total": {"value": 100},
                         "country_code": {"buckets": []}},
                    ]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"field": "app_installs", "type": "range", "gte": 0}
        result = _query_google_ads_installs_geo_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["iso2"] is None

    def test_returns_empty_on_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        stat_cfg = {"field": "app_installs", "type": "range", "gte": 0}
        result = _query_google_ads_installs_geo_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []


# ===========================================================================
# _load_dt_team_country_map (lines 1481-1514)
# ===========================================================================

class TestLoadDtTeamCountryMap:
    def setup_method(self):
        import asset_management.web.blueprints.dashboard_builder.routes as r
        r._team_country_map_cache = None
        r._team_country_map_cache_ts = 0

    def test_returns_team_country_map(self):
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "hits": {
                "hits": [
                    {"_source": {"team_title": "NFCY", "team_country_text": "TR"}},
                    {"_source": {"team_title": "VOGZ", "team_country_text": "MX"}},
                ]
            }
        }
        mock_es.search.return_value = resp
        result = _load_dt_team_country_map(mock_es)
        assert result["NFCY"] == "TR"
        assert result["VOGZ"] == "MX"

    def test_uses_team_country_list(self):
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "hits": {
                "hits": [
                    {"_source": {"team_title": "TEAM1", "team_country_text": ["EG", "SA"]}},
                ]
            }
        }
        mock_es.search.return_value = resp
        result = _load_dt_team_country_map(mock_es)
        assert result["TEAM1"] == "EG"

    def test_skips_missing_team_title(self):
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "hits": {
                "hits": [
                    {"_source": {"team_country_text": "US"}},
                ]
            }
        }
        mock_es.search.return_value = resp
        result = _load_dt_team_country_map(mock_es)
        assert result == {}

    def test_returns_empty_on_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        result = _load_dt_team_country_map(mock_es)
        assert result == {}


# ===========================================================================
# Export multi-stat leaflet_map (lines 2771-2852: multi-stat city/region rows)
# ===========================================================================

class TestExportLeafletMapMultiStat:
    def _make_multi_stat_dashboard(self):
        return {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_type": "leaflet_map",
                    "title": "Multi-Stat Map",
                    "map_statistics": [
                        {"data_view": "team_fb_insights", "field": "insight_reach",
                         "aggregation": "sum", "label": "Reach", "key": "fb_reach"},
                        {"data_view": "team_fb_insights", "field": "page_impressions",
                         "aggregation": "sum", "label": "Impressions", "key": "fb_impressions"},
                    ],
                }]
            }
        }

    def test_export_multi_stat_with_city_data(self, auth_client):
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [{"key": "US", "total": {"value": 1000}, "doc_count": 100}]
                }
            }
        }
        mock_es.search.return_value = resp

        city_pts = [{"name": "New York", "lat": 40.7, "lon": -74.0, "value": 500}]
        region_pts = [{"name": "New York State", "lat": 42.0, "lon": -74.5, "value": 500}]

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=self._make_multi_stat_dashboard(),
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes._query_fb_map",
                    return_value=city_pts,
                ):
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes._query_fb_region_map",
                        return_value=region_pts,
                    ):
                        resp_http = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv"},
                        )

        assert resp_http.status_code == 200
        assert b"New York" in resp_http.data

    def test_export_dt_data_view_in_leaflet(self, auth_client):
        """disciple-tools data_view calls _query_dt_country_map (lines 2451-2488)."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_type": "leaflet_map",
                    "title": "DT Map",
                    "map_statistics": [{
                        "data_view": "disciple-tools-default",
                        "field": "contact_count",
                        "aggregation": "count",
                        "label": "Contacts",
                        "key": "dt_contacts",
                    }],
                }]
            }
        }
        mock_es = mock.MagicMock()
        dt_points = [{"name": "Turkey", "iso2": None, "lat": None, "lon": None, "value": 200}]

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=dashboard,
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes._query_dt_country_map",
                    return_value=dt_points,
                ):
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes._query_dt_map",
                        return_value=[],
                    ):
                        resp_http = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv"},
                        )

        assert resp_http.status_code == 200
        assert b"Turkey" in resp_http.data


# ===========================================================================
# Export time range injection and table chart (lines 2983-3046)
# ===========================================================================

class TestExportWithTimeRange:
    def test_bar_chart_with_time_from_injects_date_range(self, auth_client):
        """Lines 2983-3001: time_from injected into query_config.date_range."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_type": "bar", "title": "Bar Chart",
                    "data_view": "team_fb_*",
                    "x_field": "kibana_date", "x_field_type": "date",
                    "y_field": "insight_reach",
                }]
            }
        }
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {}
        mock_es.search.return_value = resp

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=dashboard,
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes.build_query",
                    return_value={"size": 0},
                ) as mock_build:
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes.transform",
                        return_value={
                            "chart_type": "bar",
                            "xAxis": {"data": ["2024-01"]},
                            "series": [{"name": "Reach", "data": [100]}],
                        },
                    ):
                        auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv", "time_from": "now-30d/d", "time_to": "now"},
                        )

        assert mock_build.called
        call_cfg = mock_build.call_args[0][0]
        assert "date_range" in call_cfg

    def test_dt_data_view_with_time_from_uses_resolved_date(self, auth_client):
        """Lines 2986-2992: disciple-tools data_view uses resolved date strings."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_type": "bar", "title": "DT Bar",
                    "data_view": "disciple-tools-default",
                    "x_field": "date_created.keyword",
                    "y_field": "contact_count",
                }]
            }
        }
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {}
        mock_es.search.return_value = resp

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=dashboard,
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes.build_query",
                    return_value={"size": 0},
                ) as mock_build:
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes.transform",
                        return_value={
                            "chart_type": "bar",
                            "xAxis": {"data": ["2024-01"]},
                            "series": [{"name": "Count", "data": [50]}],
                        },
                    ):
                        auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv", "time_from": "now-30d/d", "time_to": "now"},
                        )

        assert mock_build.called

    def test_export_table_chart_multi_dv(self, auth_client):
        """Lines 3009-3012: table with per-column data_view uses _run_table_multi_dv_query."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_type": "table", "title": "Table",
                    "data_view": "team_fb_*",
                    "x_field": "team_title",
                    "table_columns": [
                        {"field": "insight_reach", "data_view": "team_fb_*"},
                    ],
                }]
            }
        }
        mock_es = mock.MagicMock()
        table_resp = mock.MagicMock()
        table_resp.get_data = mock.MagicMock(
            return_value='{"chart_type":"table","columns":["Team","Reach"],'
                         '"rows":[["NFCY",100]],"_warnings":[]}'
        )

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=dashboard,
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes._run_table_multi_dv_query",
                    return_value=table_resp,
                ):
                    resp_http = auth_client.post(
                        f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                        json={"format": "csv"},
                    )

        assert resp_http.status_code == 200
        assert b"NFCY" in resp_http.data

    def test_export_chart_visibility_all_false_skips_chart(self, auth_client):
        """Lines 3035-3036: all series unchecked -> chart is skipped -> 400 if no other charts."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_id": "chart1",
                    "chart_type": "bar", "title": "Bar",
                    "data_view": "team_fb_*",
                    "x_field": "date",
                    "y_field": "reach",
                }]
            }
        }
        mock_es = mock.MagicMock()

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=dashboard,
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes.build_query",
                    return_value={"size": 0},
                ):
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes.transform",
                        return_value={
                            "chart_type": "bar",
                            "xAxis": {"data": ["Jan"]},
                            "series": [{"name": "Reach", "data": [100]}],
                        },
                    ):
                        resp_http = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={
                                "format": "csv",
                                "chart_visibility": {
                                    "chart1": {"Reach": False}
                                },
                            },
                        )

        # All series unchecked -> chart skipped -> no sheets -> 400
        assert resp_http.status_code == 400

    def test_export_chart_visibility_filters_series(self, auth_client):
        """Lines 3037-3046: partial visibility filters series."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_id": "chart1",
                    "chart_type": "bar", "title": "Bar",
                    "data_view": "team_fb_*",
                    "x_field": "date",
                    "y_field": "reach",
                }]
            }
        }
        mock_es = mock.MagicMock()

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=dashboard,
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes.build_query",
                    return_value={"size": 0},
                ):
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes.transform",
                        return_value={
                            "chart_type": "bar",
                            "xAxis": {"data": ["Jan"]},
                            "series": [
                                {"name": "Reach", "data": [100]},
                                {"name": "Impressions", "data": [200]},
                            ],
                        },
                    ):
                        resp_http = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={
                                "format": "csv",
                                "chart_visibility": {
                                    "chart1": {"Reach": True, "Impressions": False}
                                },
                            },
                        )

        assert resp_http.status_code == 200
        assert b"Reach" in resp_http.data
