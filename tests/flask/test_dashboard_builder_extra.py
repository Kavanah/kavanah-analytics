"""Extra tests for fb_region_map, fb_country_map, echo_map, and leaflet_map export."""
import os
import sys
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.blueprints.dashboard_builder.routes import (
    _org_team_filters,
    _query_fb_region_map,
    _query_fb_country_map,
    _query_echo_map,
    _query_echo_country_map,
    _query_google_ads_installs_geo_map,
    _load_dt_team_country_map,
)

BUILDER_PREFIX = "/dashboard-builder"


# ===========================================================================
# _org_team_filters — org/team filter clause generation
# ===========================================================================


class TestOrgTeamFilters:
    """Verify _org_team_filters skips org clause when teams are provided.

    Disciple Tools (and potentially other data views) don't have an
    organization_title field. Adding it as an AND clause returns 0 hits.
    This regression has occurred multiple times — these tests guard against it.
    """

    def test_org_only_produces_org_clause(self):
        clauses = _org_team_filters("VOGZ", [])
        assert len(clauses) == 1
        assert "match_phrase" in clauses[0]
        assert clauses[0]["match_phrase"]["organization_title"] == "VOGZ"

    def test_team_only_produces_team_clause(self):
        clauses = _org_team_filters("", ["FLKVT"])
        assert len(clauses) == 1
        assert "match_phrase" in clauses[0]
        assert clauses[0]["match_phrase"]["team_title"] == "FLKVT"

    def test_org_and_single_team_skips_org(self):
        """When a team is provided, org filter must be omitted."""
        clauses = _org_team_filters("VOGZ", ["FLKVT"])
        assert len(clauses) == 1
        clause = clauses[0]
        assert "match_phrase" in clause
        assert "team_title" in clause["match_phrase"]
        # org clause must NOT be present
        assert not any("organization_title" in str(c) for c in clauses), (
            "org filter must be skipped when teams are provided — "
            "Disciple Tools has no organization_title field"
        )

    def test_org_and_multiple_teams_skips_org(self):
        """Multiple teams produce a bool/should — org must still be omitted."""
        clauses = _org_team_filters("VOGZ", ["FLKVT", "NFCY", "TeamC"])
        assert len(clauses) == 1
        clause = clauses[0]
        assert "bool" in clause
        should = clause["bool"]["should"]
        assert len(should) == 3
        assert clause["bool"]["minimum_should_match"] == 1
        # Verify all should clauses are team filters
        for s in should:
            assert "match_phrase" in s
            assert "team_title" in s["match_phrase"]
        # org clause must NOT be present
        assert not any("organization_title" in str(c) for c in clauses)

    def test_string_team_is_normalized(self):
        """A single team string (not list) should be handled correctly."""
        clauses = _org_team_filters("VOGZ", "FLKVT")
        assert len(clauses) == 1
        assert clauses[0]["match_phrase"]["team_title"] == "FLKVT"
        assert not any("organization_title" in str(c) for c in clauses)

    def test_empty_string_team_treated_as_no_teams(self):
        """An empty string team should fall through to org filter."""
        clauses = _org_team_filters("VOGZ", "")
        assert len(clauses) == 1
        assert clauses[0]["match_phrase"]["organization_title"] == "VOGZ"

    def test_no_org_no_teams_returns_empty(self):
        clauses = _org_team_filters("", [])
        assert clauses == []


# ===========================================================================
# _query_fb_region_map — aggregates on location_regions.name
# ===========================================================================

class TestQueryFbRegionMap:
    def _make_resp(self, buckets):
        resp = mock.MagicMock()
        resp.body = {"aggregations": {"regions": {"buckets": buckets}}}
        return resp

    def _empty_resp(self):
        resp = mock.MagicMock()
        resp.body = {"aggregations": {"regions": {"buckets": []}}}
        return resp

    def test_returns_empty_on_exception_on_both_fields(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_region_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_returns_empty_when_both_field_variants_have_no_buckets(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = [self._empty_resp(), self._empty_resp()]
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_region_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_returns_points_with_coords_from_sample_hit(self):
        mock_es = mock.MagicMock()
        bucket = {
            "key": "California", "doc_count": 10, "stat": {"value": 200},
            "sample": {"hits": {"hits": [{"_source": {"location_regions": [
                {"name": "California", "coordinates": {"lat": 36.78, "lon": -119.42}, "country_code": "US"}
            ]}}]}},
        }
        mock_es.search.return_value = self._make_resp([bucket])
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_region_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "California"
        assert result[0]["value"] == 200
        assert result[0]["lat"] == 36.78
        assert result[0]["lon"] == -119.42

    def test_returns_point_with_null_coords_when_sample_missing_match(self):
        """Bucket name doesn't appear in the sample hit's location_regions array → coords are None."""
        mock_es = mock.MagicMock()
        bucket = {
            "key": "Texas", "doc_count": 5, "stat": {"value": 50},
            "sample": {"hits": {"hits": [{"_source": {"location_regions": [
                {"name": "California", "coordinates": {"lat": 36.78, "lon": -119.42}},
            ]}}]}},
        }
        mock_es.search.return_value = self._make_resp([bucket])
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_region_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "Texas"
        assert result[0]["value"] == 50
        assert result[0]["lat"] is None
        assert result[0]["lon"] is None

    def test_skips_bucket_with_empty_name(self):
        mock_es = mock.MagicMock()
        bucket = {"key": "", "doc_count": 10, "stat": {"value": 100}, "sample": {"hits": {"hits": []}}}
        mock_es.search.return_value = self._make_resp([bucket])
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_region_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_falls_back_to_doc_count_when_stat_value_is_zero(self):
        """ES sum of a non-existent field yields 0 → we fall back to doc_count."""
        mock_es = mock.MagicMock()
        bucket = {
            "key": "Uttar Pradesh", "doc_count": 7, "stat": {"value": 0},
            "sample": {"hits": {"hits": [{"_source": {"location_regions": [
                {"name": "Uttar Pradesh", "coordinates": {"lat": 26.8, "lon": 80.9}}
            ]}}]}},
        }
        mock_es.search.return_value = self._make_resp([bucket])
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_region_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["value"] == 7

    def test_falls_through_to_plain_field_when_keyword_subfield_empty(self):
        """First call (.keyword) returns no buckets; second call (.name) returns data."""
        mock_es = mock.MagicMock()
        bucket = {
            "key": "Ile-de-France", "doc_count": 3, "stat": {"value": 42},
            "sample": {"hits": {"hits": [{"_source": {"location_regions": [
                {"name": "Ile-de-France", "coordinates": {"lat": 48.85, "lon": 2.35}}
            ]}}]}},
        }
        mock_es.search.side_effect = [self._empty_resp(), self._make_resp([bucket])]
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_region_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "Ile-de-France"
        assert result[0]["value"] == 42


# ===========================================================================
# _query_fb_country_map (lines 840-914)
# ===========================================================================

class TestQueryFbCountryMap:
    def test_returns_points_from_iso2_primary(self):
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "countries": {
                    "buckets": [{
                        "key": "US", "doc_count": 100, "stat": {"value": 500},
                    }]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_country_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "US"
        assert result[0]["iso2"] == "US"
        # lat/lon come from the canonical _COUNTRY_CENTROIDS table
        assert result[0]["lat"] == 37.09

    def test_iso2_not_in_centroids_returns_none_coords(self):
        """Primary path appends point with lat/lon=None when iso2 missing from centroids dict."""
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "countries": {
                    "buckets": [{"key": "ZZ", "doc_count": 10, "stat": {"value": 50}}]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_country_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["iso2"] == "ZZ"
        assert result[0]["lat"] is None
        assert result[0]["lon"] is None

    def test_falls_back_to_team_country_iso2_when_primary_empty(self):
        """When location_countries has no data, ISO2 entries from team_country fill in."""
        mock_es = mock.MagicMock()
        empty_resp = mock.MagicMock()
        empty_resp.body = {"aggregations": {"countries": {"buckets": []}}}
        fallback_resp = mock.MagicMock()
        fallback_resp.body = {
            "aggregations": {
                "countries": {
                    "buckets": [{"key": "TR", "doc_count": 50, "stat": {"value": 200}}]
                }
            }
        }
        mock_es.search.side_effect = [empty_resp, empty_resp, fallback_resp]
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_country_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "TR"
        assert result[0]["iso2"] == "TR"

    def test_skips_full_country_names_in_team_country(self):
        """Junk team_country values like "United States" or "Cairo" are skipped."""
        mock_es = mock.MagicMock()
        empty_resp = mock.MagicMock()
        empty_resp.body = {"aggregations": {"countries": {"buckets": []}}}
        fallback_resp = mock.MagicMock()
        fallback_resp.body = {
            "aggregations": {
                "countries": {
                    "buckets": [
                        {"key": "United States", "doc_count": 50, "stat": {"value": 200}},
                        {"key": "Cairo", "doc_count": 30, "stat": {"value": 100}},
                    ]
                }
            }
        }
        mock_es.search.side_effect = [empty_resp, empty_resp, fallback_resp]
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_country_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_unions_primary_and_fallback(self):
        """Primary location_countries plus team_country merge by ISO2; primary wins on conflict."""
        mock_es = mock.MagicMock()
        primary_resp = mock.MagicMock()
        primary_resp.body = {
            "aggregations": {
                "countries": {
                    "buckets": [{"key": "EG", "doc_count": 100, "stat": {"value": 1000}}]
                }
            }
        }
        fallback_resp = mock.MagicMock()
        fallback_resp.body = {
            "aggregations": {
                "countries": {
                    "buckets": [
                        {"key": "EG", "doc_count": 50, "stat": {"value": 99}},  # ignored — primary wins
                        {"key": "TR", "doc_count": 20, "stat": {"value": 200}},
                    ]
                }
            }
        }
        mock_es.search.side_effect = [primary_resp, fallback_resp]
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_country_map(mock_es, stat_cfg, "", [], "", "")
        iso2s = sorted(p["iso2"] for p in result)
        assert iso2s == ["EG", "TR"]
        eg = next(p for p in result if p["iso2"] == "EG")
        assert eg["value"] == 1000  # primary value, not fallback's 99

    def test_fallback_skips_unknown_country(self):
        mock_es = mock.MagicMock()
        resp1 = mock.MagicMock()
        resp1.body = {"aggregations": {"countries": {"buckets": []}}}
        resp2 = mock.MagicMock()
        resp2.body = {
            "aggregations": {
                "countries": {
                    "buckets": [
                        {"key": "unknown", "doc_count": 5, "stat": {"value": 0}},
                        {"key": "", "doc_count": 3, "stat": {"value": 0}},
                    ]
                }
            }
        }
        mock_es.search.side_effect = [resp1, resp2]
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_country_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_returns_empty_on_primary_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_country_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_two_char_key_sets_iso2(self):
        mock_es = mock.MagicMock()
        resp1 = mock.MagicMock()
        resp1.body = {"aggregations": {"countries": {"buckets": []}}}
        resp2 = mock.MagicMock()
        resp2.body = {
            "aggregations": {
                "countries": {
                    "buckets": [{"key": "TR", "doc_count": 20, "stat": {"value": 100}}]
                }
            }
        }
        mock_es.search.side_effect = [resp1, resp2]
        stat_cfg = {"field": "insight_reach", "type": "exists"}
        result = _query_fb_country_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["iso2"] == "TR"


# ===========================================================================
# _query_echo_map (lines 1309-1348)
# ===========================================================================

class TestQueryEchoMap:
    def test_returns_empty_on_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        stat_cfg = {"field": "incomingMessagesCount", "type": "exists"}
        result = _query_echo_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_returns_country_centroid_points(self):
        """Aggregates by contact_country_iso2 and returns centroid lat/lon."""
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
        result = _query_echo_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "TR"
        assert result[0]["value"] == 200
        assert result[0]["lat"] is not None
        assert result[0]["lon"] is not None

    def test_skips_iso2_without_centroid(self):
        """ISO2 codes not in _COUNTRY_CENTROIDS are skipped."""
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [{"key": "XX", "doc_count": 5}]
                }
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {"field": "incomingMessagesCount", "type": "exists"}
        result = _query_echo_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_handles_no_field_in_stat_cfg(self):
        """No field → uses doc_count as value."""
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {"buckets": [{"key": "EG", "doc_count": 10}]}
            }
        }
        mock_es.search.return_value = resp
        stat_cfg = {}  # no field
        result = _query_echo_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["value"] == 10


# ===========================================================================
# Export with leaflet_map chart type (lines 2411-2878)
# ===========================================================================

class TestExportLeafletMap:
    def _make_leaflet_dashboard(self, data_view="team_fb_insights"):
        return {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_type": "leaflet_map",
                    "title": "Engagement Map",
                    "map_statistics": [{
                        "data_view": data_view,
                        "field": "insight_reach",
                        "aggregation": "sum",
                        "label": "Reach",
                        "key": "fb_reach",
                    }],
                }]
            }
        }

    def _country_resp(self, country="US", value=1000):
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": country, "total": {"value": value}, "doc_count": 100}
                    ]
                }
            }
        }
        return resp

    def test_export_leaflet_map_countries_csv(self, auth_client):
        """Covers the leaflet_map block including _ISO2_NAMES dict (lines 2411-2878)."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._country_resp()

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=self._make_leaflet_dashboard(),
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes._query_fb_map",
                    return_value=[],
                ):
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes._query_fb_region_map",
                        return_value=[],
                    ):
                        resp = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv"},
                        )

        assert resp.status_code == 200
        assert b"Engagement Map - Countries" in resp.data

    def test_export_us_iso2_resolved_to_united_states(self, auth_client):
        """Verifies _country_display resolves ISO2 'US' -> 'United States'."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._country_resp("US", 500)

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=self._make_leaflet_dashboard(),
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes._query_fb_map",
                    return_value=[],
                ):
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes._query_fb_region_map",
                        return_value=[],
                    ):
                        resp = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv"},
                        )

        assert b"United States" in resp.data

    def test_export_leaflet_map_skips_empty_map_stats(self, auth_client):
        """map_statistics=[] -> leaflet_map block hits continue; bar chart fills the sheet."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [
                    {"chart_type": "leaflet_map", "title": "Map", "map_statistics": []},
                    {"chart_type": "bar", "title": "Bar", "data_view": "idx",
                     "x_field": "date", "y_field": "reach"},
                ]
            }
        }
        mock_es = mock.MagicMock()
        resp_es = mock.MagicMock()
        resp_es.body = {}
        mock_es.search.return_value = resp_es

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
                            "xAxis": {"data": ["A"]},
                            "series": [{"name": "S", "data": [1]}]
                        },
                    ):
                        resp = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv"},
                        )

        assert resp.status_code == 200

    def test_export_leaflet_map_skips_stat_without_data_view(self, auth_client):
        """Stat with no data_view is skipped -> no countries -> 400."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_type": "leaflet_map",
                    "title": "Map",
                    "map_statistics": [
                        {"data_view": "", "field": "reach", "label": "Reach"},
                    ],
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
                resp = auth_client.post(
                    f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                    json={"format": "csv"},
                )

        assert resp.status_code == 400

    def test_export_leaflet_map_with_city_and_region_data(self, auth_client):
        """Covers city_rows and region_rows inclusion (single-stat path, lines 2751-2769)."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._country_resp("GB", 200)

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=self._make_leaflet_dashboard("team_fb_insights"),
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes._query_fb_map",
                    return_value=[{"name": "London", "lat": 51.5, "lon": -0.1, "value": 100}],
                ):
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes._query_fb_region_map",
                        return_value=[{"name": "England", "lat": 52.0, "lon": -1.0, "value": 100}],
                    ):
                        resp = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv"},
                        )

        assert resp.status_code == 200
        assert b"London" in resp.data
        assert b"England" in resp.data

    def test_export_leaflet_map_charts_as_dict(self, auth_client):
        """Covers line 2360: charts = list(charts.values()) when charts is a dict."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": {
                    "chart0": {
                        "chart_type": "leaflet_map",
                        "title": "Map",
                        "map_statistics": [{
                            "data_view": "team_fb_insights",
                            "field": "insight_reach",
                            "aggregation": "sum",
                            "label": "Reach",
                        }],
                    }
                }
            }
        }
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._country_resp("FR", 300)

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=dashboard,
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes._query_fb_map",
                    return_value=[],
                ):
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes._query_fb_region_map",
                        return_value=[],
                    ):
                        resp = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv"},
                        )

        assert resp.status_code == 200
        assert b"France" in resp.data

    def test_export_leaflet_map_with_time_range_resolves_date_math(self, auth_client):
        """Covers lines 2381-2400: the inner _resolve_date_math is called when time_from set."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._country_resp("DE", 150)

        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes.get_document_by_id",
            return_value=self._make_leaflet_dashboard(),
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes.get_elasticsearch_client",
                return_value=mock_es,
            ):
                with mock.patch(
                    "asset_management.web.blueprints.dashboard_builder.routes._query_fb_map",
                    return_value=[],
                ):
                    with mock.patch(
                        "asset_management.web.blueprints.dashboard_builder.routes._query_fb_region_map",
                        return_value=[],
                    ):
                        resp = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv", "time_from": "now-30d/d", "time_to": "now"},
                        )

        assert resp.status_code == 200
        assert b"Germany" in resp.data

    def test_export_leaflet_map_stat_key_filter(self, auth_client):
        """stat_keys filter: stat not in stat_keys is skipped -> no countries -> 400."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_type": "leaflet_map",
                    "title": "Map",
                    "map_statistics": [{
                        "data_view": "team_fb_insights",
                        "field": "insight_reach",
                        "label": "Reach",
                        "key": "fb_reach",
                    }],
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
                resp = auth_client.post(
                    f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                    json={"format": "csv", "stat_keys": ["other_key"]},
                )

        assert resp.status_code == 400


# ===========================================================================
# read-only Dashboards "Export" button: "All Teams" export
# ===========================================================================
class TestKan943AllTeamsExport:
    """The Dashboards Export button collapsed an "All Teams" export to
    ``AUTO_FILTER_TEAMS[0]`` — a single team whose value often did not match the
    data's ``team_title`` — producing a blank or one-team report. The fix leaves
    ``team_filter`` empty for all-teams; ``export_dashboard`` routes every query
    through ``scoped_index_pattern`` (Layer-1 tenant isolation), so an empty
    filter means "all the caller's teams", never cluster-wide, never collapsed
    to one team.
    """

    _R = "asset_management.web.blueprints.dashboard_builder.routes"
    _SENTINEL = "SCOPED__only_my_team_indices__*"

    def _table_dashboard(self, data_view="team_fb_insights_window"):
        return {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_id": "c1",
                    "chart_type": "table",
                    "title": "Ad Sets (Delivery)",
                    "data_view": data_view,
                    "x_field": "adset_name",
                    "y_fields": ["reach"],
                }]
            },
        }

    def _post_export(self, auth_client, body):
        mock_es = mock.MagicMock()
        es_resp = mock.MagicMock()
        es_resp.body = {}
        mock_es.search.return_value = es_resp
        build_query = mock.MagicMock(return_value={"size": 0})
        with mock.patch(f"{self._R}.get_document_by_id", return_value=self._table_dashboard()), \
                mock.patch(f"{self._R}.get_elasticsearch_client", return_value=mock_es), \
                mock.patch(f"{self._R}._enrich_y_field_types"), \
                mock.patch(f"{self._R}.build_query", build_query), \
                mock.patch(f"{self._R}.transform", return_value={"chart_type": "table"}), \
                mock.patch(f"{self._R}._chart_data_to_table", return_value=(["Ad Set"], [["a"], ["b"], ["c"]])), \
                mock.patch(f"{self._R}.RoleFilterService.scoped_index_pattern", return_value=self._SENTINEL):
            resp = auth_client.post(f"{BUILDER_PREFIX}/api/dashboards/dash-1/export", json=body)
        return resp, mock_es, build_query

    def test_all_teams_export_is_index_scoped_with_no_team_narrowing(self, auth_client):
        """Empty team_filter -> query hits only the caller's scoped indices and
        injects NO team_title clause (all of the caller's teams, not one).

        Mutation: have the route default an empty team_filter to a single team
        -> the no-team-narrowing assert fails; bypass scoped_index_pattern ->
        the scoped-index assert fails.
        """
        resp, mock_es, build_query = self._post_export(auth_client, {"format": "csv"})
        assert resp.status_code == 200
        assert mock_es.search.call_args.kwargs["index"] == self._SENTINEL
        qc = build_query.call_args.args[0]
        team_clauses = [f for f in (qc.get("filters") or []) if f.get("field") == "team_title"]
        assert team_clauses == [], (
            f"an all-teams export must not narrow by team_title; got {team_clauses}"
        )

    def test_specific_team_export_adds_team_title_narrowing(self, auth_client):
        """A specific selected team still narrows by team_title — proving the
        all-teams absence is meaningful. made the export team filter
        multi-select: a bare-string team_filter (back-compat) normalizes to a
        one-element match_phrase_any list instead of a scalar match_phrase.
        """
        resp, mock_es, build_query = self._post_export(
            auth_client, {"format": "csv", "team_filter": "Alpha"}
        )
        assert resp.status_code == 200
        qc = build_query.call_args.args[0]
        team_clauses = [f for f in (qc.get("filters") or []) if f.get("field") == "team_title"]
        assert team_clauses == [{"field": "team_title", "op": "match_phrase_any", "value": ["Alpha"]}], (
            f"a specific-team export must narrow by team_title; got {team_clauses}"
        )

    def test_export_tabular_js_does_not_collapse_all_teams(self):
        """The dashboards Export JS must not substitute AUTO_FILTER_TEAMS[0] for
        an empty (all-teams) team filter — the front-end half of the fix.

        Mutation: restore `teamValue = AUTO_FILTER_TEAMS[0]` -> this fails.
        """
        tpl_path = os.path.join(
            _REPO_ROOT, "bin", "python", "asset_management", "web", "templates", "dashboards.html"
        )
        with open(tpl_path, encoding="utf-8") as fh:
            tpl = fh.read()
        assert "teamValue = AUTO_FILTER_TEAMS[0]" not in tpl, (
            "dashboards.html must not collapse an all-teams export to "
            "AUTO_FILTER_TEAMS[0] (sends one non-matching team -> blank report)."
        )
        assert "" in tpl, "expected the fix comment in dashboards.html."
