"""
Tests for advanced dashboard API routes.

Routes covered:
  GET  /api/dashboards/<dashboard_key>                    get_dashboard_info
  GET  /api/dashboards/data-pulled-as-of                  get_data_pulled_as_of

All external service calls are mocked.
"""

from unittest import mock

import pytest


# ===========================================================================
# GET /api/dashboards/<dashboard_key>  -- get_dashboard_info
# ===========================================================================


class TestGetDashboardInfo:
    """Tests for the get_dashboard_info route."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/dashboards/fb_ads_overview")
        assert resp.status_code == 401

    def test_returns_dashboard_info(self, auth_client):
        resp = auth_client.get("/api/dashboards/fb_ads_overview")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["dashboard_key"] == "fb_ads_overview"
        assert "title" in data
        assert "url" in data

    def test_not_found_for_invalid_key(self, auth_client):
        resp = auth_client.get("/api/dashboards/nonexistent_dashboard")
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["success"] is False

    def test_disabled_dashboard_returns_403(self, auth_client):
        with mock.patch.dict(
            "asset_management.web.app.DASHBOARD_CONFIGS",
            {"disabled_dash": {"title": "Disabled", "enabled": False}},
        ):
            resp = auth_client.get("/api/dashboards/disabled_dash")
        assert resp.status_code == 403
        assert resp.get_json()["success"] is False


# ===========================================================================
# GET /api/dashboards/data-pulled-as-of
# ===========================================================================


class TestDataPulledAsOf:
    """Tests for the get_data_pulled_as_of route."""

    def test_unknown_platform_returns_null(self, auth_client):
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=NonExistent")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["timestamp"] is None

    @mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None)
    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb_insights*")
    def test_es_unavailable_returns_null(self, mock_resolve, mock_es, auth_client):
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Meta")
        data = resp.get_json()
        assert data["success"] is True
        assert data["timestamp"] is None

    @mock.patch("lib.es_helpers.get_elasticsearch_client")
    @mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb_insights*")
    def test_returns_timestamp(self, mock_resolve, mock_es_func, auth_client):
        mock_es = mock.MagicMock()
        mock_es_func.return_value = mock_es
        mock_es.search.return_value = {
            "aggregations": {"max_ts": {"value_as_string": "2025-01-01T12:00:00Z"}}
        }
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Meta")
        data = resp.get_json()
        assert data["success"] is True
        assert data["timestamp"] == "2025-01-01T12:00:00Z"

    def test_no_platform_param_returns_null(self, auth_client):
        resp = auth_client.get("/api/dashboards/data-pulled-as-of")
        data = resp.get_json()
        assert data["success"] is True
        assert data["timestamp"] is None
