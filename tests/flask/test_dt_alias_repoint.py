"""DT readers repointed from the retired cross-tenant `disciple-tools`
alias to the team-keyed `*_disciple_tools` pattern.

Pins:
  1. The three DT map query functions search `*_disciple_tools`, never the
     legacy hyphen alias (which 404s once the alias is deleted on prod).
  2. Map/export routing matches BOTH spellings — a team-keyed data view
     (underscore) routes to the DT query path, and legacy hyphen saved
     configs keep working.
  3. Export time-range injection picks the right default date field per
     spelling: `date_created_ms` (real date type) for team-keyed indices,
     `date_created.keyword` for legacy dynamic-mapped alias configs.
"""
import os
import sys
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.blueprints.dashboard_builder.routes import (
    _query_dt_map,
    _query_dt_country_map,
    _query_dt_team_country_map,
)

BUILDER_PREFIX = "/dashboard-builder"

_ROUTES = "asset_management.web.blueprints.dashboard_builder.routes"


def _empty_dt_resp():
    resp = mock.MagicMock()
    resp.body = {"aggregations": {"by_grid": {"buckets": []}, "by_address": {"buckets": []}}}
    return resp


# ===========================================================================
# 1. Map query functions hit the team-keyed pattern, not the retired alias
# ===========================================================================

class TestDtMapQueriesUseTeamKeyedIndices:
    def test_query_dt_map_searches_team_keyed_pattern(self):
        """_query_dt_map must search *_disciple_tools (alias is retired)."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _empty_dt_resp()

        with mock.patch(f"{_ROUTES}._load_dt_grid_names", return_value={}):
            _query_dt_map(mock_es, {"field": "", "aggregation": "count"}, "", [], "", "")

        assert mock_es.search.called
        _, kwargs = mock_es.search.call_args
        assert kwargs["index"] == "*_disciple_tools"

    def test_query_dt_country_map_searches_team_keyed_pattern(self):
        """_query_dt_country_map must search *_disciple_tools."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _empty_dt_resp()

        with mock.patch(f"{_ROUTES}._load_dt_grid_names", return_value={}):
            _query_dt_country_map(mock_es, {"field": "", "aggregation": "count"}, "", [], "", "")

        assert mock_es.search.called
        _, kwargs = mock_es.search.call_args
        assert kwargs["index"] == "*_disciple_tools"

    def test_query_dt_team_country_map_searches_team_keyed_pattern(self):
        """_query_dt_team_country_map must search *_disciple_tools."""
        mock_es = mock.MagicMock()
        resp = mock.MagicMock()
        resp.body = {"aggregations": {"by_team": {"buckets": []}}}
        mock_es.search.return_value = resp

        with mock.patch(f"{_ROUTES}._load_dt_team_country_map", return_value={"FLKVT": "TR"}):
            _query_dt_team_country_map(mock_es, {"field": "", "aggregation": "count"}, "", [], "", "")

        assert mock_es.search.called
        _, kwargs = mock_es.search.call_args
        assert kwargs["index"] == "*_disciple_tools"


# ===========================================================================
# 2. Export map routing matches the team-keyed (underscore) spelling
# ===========================================================================

class TestDtRoutingMatchesTeamKeyedSpelling:
    def test_export_leaflet_routes_team_keyed_dv_to_dt_query(self, auth_client):
        """A `*_disciple_tools` map stat must route to _query_dt_country_map
        (the earlier hyphen-only check silently skipped team-keyed views)."""
        dashboard = {
            "dashboard_type": "echarts",
            "echarts_config": {
                "charts": [{
                    "chart_type": "leaflet_map",
                    "title": "DT Map",
                    "map_statistics": [{
                        "data_view": "*_disciple_tools",
                        "field": "",
                        "aggregation": "count",
                        "label": "Contacts",
                        "key": "dt_contacts",
                    }],
                }]
            }
        }
        mock_es = mock.MagicMock()
        dt_points = [{"name": "Turkey", "iso2": None, "lat": None, "lon": None, "value": 200}]

        with mock.patch(f"{_ROUTES}.get_document_by_id", return_value=dashboard):
            with mock.patch(f"{_ROUTES}.get_elasticsearch_client", return_value=mock_es):
                with mock.patch(f"{_ROUTES}._query_dt_country_map", return_value=dt_points) as mock_dt:
                    with mock.patch(f"{_ROUTES}._query_dt_map", return_value=[]):
                        resp_http = auth_client.post(
                            f"{BUILDER_PREFIX}/api/dashboards/dash-1/export",
                            json={"format": "csv"},
                        )

        assert resp_http.status_code == 200
        assert mock_dt.called
        assert b"Turkey" in resp_http.data


# ===========================================================================
# 3. Export time-range default date field per DT spelling
# ===========================================================================

def _export_bar_chart(auth_client, data_view):
    """Drive the export endpoint with a bar chart on the given data view and
    return the query_config build_query received."""
    dashboard = {
        "dashboard_type": "echarts",
        "echarts_config": {
            "charts": [{
                "chart_type": "bar", "title": "DT Bar",
                "data_view": data_view,
                "x_field": "date_created",
                "y_field": "contact_count",
            }]
        }
    }
    mock_es = mock.MagicMock()
    resp = mock.MagicMock()
    resp.body = {}
    mock_es.search.return_value = resp

    with mock.patch(f"{_ROUTES}.get_document_by_id", return_value=dashboard):
        with mock.patch(f"{_ROUTES}.get_elasticsearch_client", return_value=mock_es):
            with mock.patch(f"{_ROUTES}.build_query", return_value={"size": 0}) as mock_build:
                with mock.patch(
                    f"{_ROUTES}.transform",
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
    return mock_build.call_args[0][0]


class TestDtExportTimeRangeDateField:
    def test_team_keyed_dv_defaults_to_date_created_ms(self, auth_client):
        """Team-keyed indices map date_created as bare keyword (no .keyword
        subfield) — the export time range must use date_created_ms (date type),
        or the range filter silently matches nothing."""
        cfg = _export_bar_chart(auth_client, "*_disciple_tools")
        assert cfg["date_range"]["field"] == "date_created_ms"

    def test_legacy_hyphen_dv_keeps_date_created_keyword(self, auth_client):
        """Legacy hyphen saved configs (dynamic-mapped text) keep the .keyword
        subfield default until the alias is fully retired."""
        cfg = _export_bar_chart(auth_client, "disciple-tools-default")
        assert cfg["date_range"]["field"] == "date_created.keyword"
