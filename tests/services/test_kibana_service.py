"""
Tests for the Kibana Service.

Covers:
  get_dashboards_in_space  — default space, custom space, empty, auth, exceptions
  get_dashboard_panels     — panel extraction, custom space, missing password, errors
  resolve_data_view_id     — title resolution, fallback on failure, missing password
  get_full_dashboard_with_visualizations — by-value panels, by-reference panels, errors
  delete_dashboard         — success (200/204), HTTP error, connection error, missing password
"""

import json
from unittest import mock

import pytest
import requests

from asset_management.web.services.kibana_service import KibanaService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, json_data=None, text=""):
    """Create a mock requests.Response."""
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    resp.ok = 200 <= status_code < 300
    return resp


def _find_response(dashboards):
    """Kibana saved_objects/_find response."""
    return {
        "saved_objects": [
            {
                "id": d["id"],
                "type": "dashboard",
                "attributes": {
                    "title": d.get("title", "Untitled"),
                    "description": d.get("description", ""),
                },
                "references": [],
            }
            for d in dashboards
        ]
    }


def _dashboard_object(dashboard_id, title, panels, references=None):
    """Kibana saved_objects/dashboard/{id} response body."""
    return {
        "id": dashboard_id,
        "type": "dashboard",
        "attributes": {
            "title": title,
            "panelsJSON": json.dumps(panels),
        },
        "references": references or [],
    }


# ---------------------------------------------------------------------------
# get_dashboards_in_space
# ---------------------------------------------------------------------------

class TestGetDashboardsInSpace:

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_default_space_url_has_no_space_prefix(self, mock_get):
        payload = _find_response([{"id": "d1", "title": "Alpha"}])
        mock_get.return_value = _mock_response(json_data=payload)

        result = KibanaService.get_dashboards_in_space("default")

        assert len(result) == 1
        assert result[0]["id"] == "d1"
        assert result[0]["title"] == "Alpha"
        url = mock_get.call_args[0][0]
        assert "/s/default/" not in url
        assert "type=dashboard" in url

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_custom_space_url_has_space_prefix(self, mock_get):
        payload = _find_response([{"id": "d2", "title": "Beta"}])
        mock_get.return_value = _mock_response(json_data=payload)

        result = KibanaService.get_dashboards_in_space("test-space")

        url = mock_get.call_args[0][0]
        assert "/s/test-space/api/" in url
        assert len(result) == 1

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_dashboards_sorted_alphabetically(self, mock_get):
        payload = _find_response([
            {"id": "d1", "title": "Zebra"},
            {"id": "d2", "title": "Alpha"},
            {"id": "d3", "title": "middle"},
        ])
        mock_get.return_value = _mock_response(json_data=payload)

        result = KibanaService.get_dashboards_in_space()

        titles = [d["title"] for d in result]
        assert titles == ["Alpha", "middle", "Zebra"]

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_empty_saved_objects(self, mock_get):
        mock_get.return_value = _mock_response(json_data=_find_response([]))

        result = KibanaService.get_dashboards_in_space()

        assert result == []

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_non_200_returns_empty_list(self, mock_get):
        mock_get.return_value = _mock_response(status_code=500, text="Internal error")

        result = KibanaService.get_dashboards_in_space()

        assert result == []

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_connection_error_returns_empty_list(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("refused")

        result = KibanaService.get_dashboards_in_space()

        assert result == []

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_generic_exception_returns_empty_list(self, mock_get):
        mock_get.side_effect = Exception("boom")

        result = KibanaService.get_dashboards_in_space()

        assert result == []

    @mock.patch.dict("os.environ", {"ES01_PASSWORD": ""}, clear=False)
    def test_missing_password_returns_empty_list(self):
        with mock.patch.dict("os.environ", {"ES01_PASSWORD": ""}):
            result = KibanaService.get_dashboards_in_space()
        assert result == []

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_uses_tls_verify_and_auth(self, mock_get):
        mock_get.return_value = _mock_response(json_data=_find_response([]))

        KibanaService.get_dashboards_in_space()

        kwargs = mock_get.call_args[1]
        # TLS verification is no longer disabled (was verify=False).
        assert kwargs["verify"] is not False
        assert kwargs["auth"] == ("elastic", mock.ANY)
        assert kwargs["headers"]["kbn-xsrf"] == "true"


# ---------------------------------------------------------------------------
# get_dashboard_panels
# ---------------------------------------------------------------------------

class TestGetDashboardPanels:

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_returns_dict_with_panel_info(self, mock_get):
        panels = [
            {"panelIndex": "p1", "type": "lens"},
            {"panelIndex": "p2", "type": "visualization"},
        ]
        payload = _dashboard_object("dash-1", "Test", panels)
        mock_get.return_value = _mock_response(json_data=payload)

        result = KibanaService.get_dashboard_panels("dash-1")

        assert isinstance(result, dict)
        assert result["panel_count"] == 2
        assert result["first_panel_id"] == "p1"
        assert len(result["panels"]) == 2

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_empty_panels(self, mock_get):
        payload = _dashboard_object("dash-2", "Empty", [])
        mock_get.return_value = _mock_response(json_data=payload)

        result = KibanaService.get_dashboard_panels("dash-2")

        assert result["panel_count"] == 0
        assert result["first_panel_id"] is None

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_custom_space_in_url(self, mock_get):
        payload = _dashboard_object("dash-3", "X", [])
        mock_get.return_value = _mock_response(json_data=payload)

        KibanaService.get_dashboard_panels("dash-3", space_id="marketing")

        url = mock_get.call_args[0][0]
        assert "/s/marketing/" in url
        assert "dash-3" in url

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_default_space_no_prefix(self, mock_get):
        payload = _dashboard_object("dash-4", "X", [])
        mock_get.return_value = _mock_response(json_data=payload)

        KibanaService.get_dashboard_panels("dash-4", space_id="default")

        url = mock_get.call_args[0][0]
        assert "/s/default/" not in url

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_non_200_returns_none(self, mock_get):
        mock_get.return_value = _mock_response(status_code=404, text="Not found")

        result = KibanaService.get_dashboard_panels("missing")

        assert result is None

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_exception_returns_none(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("timeout")

        result = KibanaService.get_dashboard_panels("dash-1")

        assert result is None

    @mock.patch.dict("os.environ", {"ES01_PASSWORD": ""}, clear=False)
    def test_missing_password_returns_none(self):
        with mock.patch.dict("os.environ", {"ES01_PASSWORD": ""}):
            result = KibanaService.get_dashboard_panels("dash-1")
        assert result is None


# ---------------------------------------------------------------------------
# resolve_data_view_id
# ---------------------------------------------------------------------------

class TestResolveDataViewId:

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_returns_title_on_success(self, mock_get):
        payload = {"data_view": {"title": "facebook_ad_insights_*"}}
        mock_get.return_value = _mock_response(json_data=payload)

        result = KibanaService.resolve_data_view_id("dv-123")

        assert result == "facebook_ad_insights_*"

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_returns_original_id_on_missing_title(self, mock_get):
        payload = {"data_view": {}}
        mock_get.return_value = _mock_response(json_data=payload)

        result = KibanaService.resolve_data_view_id("dv-456")

        assert result == "dv-456"

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_returns_original_id_on_non_200(self, mock_get):
        mock_get.return_value = _mock_response(status_code=404)

        result = KibanaService.resolve_data_view_id("dv-missing")

        assert result == "dv-missing"

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_returns_original_id_on_exception(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("down")

        result = KibanaService.resolve_data_view_id("dv-err")

        assert result == "dv-err"

    @mock.patch.dict("os.environ", {"ES01_PASSWORD": ""}, clear=False)
    def test_missing_password_returns_original_id(self):
        with mock.patch.dict("os.environ", {"ES01_PASSWORD": ""}):
            result = KibanaService.resolve_data_view_id("dv-no-pw")
        assert result == "dv-no-pw"

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_custom_space_in_url(self, mock_get):
        payload = {"data_view": {"title": "google_ads_*"}}
        mock_get.return_value = _mock_response(json_data=payload)

        KibanaService.resolve_data_view_id("dv-789", space_id="custom")

        url = mock_get.call_args[0][0]
        assert "/s/custom/" in url
        assert "dv-789" in url

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_default_space_no_prefix(self, mock_get):
        payload = {"data_view": {"title": "idx_*"}}
        mock_get.return_value = _mock_response(json_data=payload)

        KibanaService.resolve_data_view_id("dv-x", space_id="default")

        url = mock_get.call_args[0][0]
        assert "/s/default/" not in url


# ---------------------------------------------------------------------------
# get_full_dashboard_with_visualizations
# ---------------------------------------------------------------------------

class TestGetFullDashboardWithVisualizations:

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_by_value_panels_no_extra_fetch(self, mock_get):
        """By-value panels (inline Lens config) should not trigger extra GET calls."""
        panels = [
            {
                "type": "lens",
                "title": "Inline Chart",
                "panelIndex": "p1",
                "embeddableConfig": {
                    "attributes": {
                        "visualizationType": "lnsXY",
                        "title": "Inline Chart",
                    }
                },
            }
        ]
        dash_body = _dashboard_object("d1", "My Dashboard", panels)
        mock_get.return_value = _mock_response(json_data=dash_body)

        result = KibanaService.get_full_dashboard_with_visualizations("d1")

        assert result is not None
        assert result["title"] == "My Dashboard"
        assert len(result["panels"]) == 1
        assert result["panels"][0]["title"] == "Inline Chart"
        # Only one GET for the dashboard itself (no viz fetch needed)
        assert mock_get.call_count == 1

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_by_reference_panels_fetch_viz(self, mock_get):
        """By-reference panels trigger a second GET to fetch the visualization."""
        panels = [
            {"type": "lens", "panelIndex": "p1", "panelRefName": "panel_0"},
        ]
        references = [
            {"name": "panel_0", "type": "lens", "id": "viz-abc"},
        ]
        dash_body = _dashboard_object("d2", "Ref Dashboard", panels, references)
        viz_body = {
            "id": "viz-abc",
            "attributes": {"title": "Fetched Viz", "visualizationType": "lnsPie"},
        }

        mock_get.side_effect = [
            _mock_response(json_data=dash_body),
            _mock_response(json_data=viz_body),
        ]

        result = KibanaService.get_full_dashboard_with_visualizations("d2")

        assert result is not None
        assert result["title"] == "Ref Dashboard"
        assert len(result["panels"]) == 1
        assert result["panels"][0]["title"] == "Fetched Viz"
        assert mock_get.call_count == 2

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_empty_panels(self, mock_get):
        dash_body = _dashboard_object("d3", "Empty", [])
        mock_get.return_value = _mock_response(json_data=dash_body)

        result = KibanaService.get_full_dashboard_with_visualizations("d3")

        assert result is not None
        assert result["title"] == "Empty"
        assert result["panels"] == []

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_non_200_dashboard_fetch_returns_none(self, mock_get):
        mock_get.return_value = _mock_response(status_code=404, text="Not found")

        result = KibanaService.get_full_dashboard_with_visualizations("d-gone")

        assert result is None

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_exception_returns_none(self, mock_get):
        mock_get.side_effect = Exception("kaboom")

        result = KibanaService.get_full_dashboard_with_visualizations("d1")

        assert result is None

    @mock.patch.dict("os.environ", {"ES01_PASSWORD": ""}, clear=False)
    def test_missing_password_returns_none(self):
        with mock.patch.dict("os.environ", {"ES01_PASSWORD": ""}):
            result = KibanaService.get_full_dashboard_with_visualizations("d1")
        assert result is None

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_custom_space_in_url(self, mock_get):
        dash_body = _dashboard_object("d4", "Space Test", [])
        mock_get.return_value = _mock_response(json_data=dash_body)

        KibanaService.get_full_dashboard_with_visualizations("d4", space_id="marketing")

        url = mock_get.call_args[0][0]
        assert "/s/marketing/" in url

    @mock.patch("asset_management.web.services.kibana_service.requests.get")
    def test_viz_fetch_failure_skips_panel(self, mock_get):
        """If a by-reference viz fetch returns non-200, the panel is skipped."""
        panels = [
            {"type": "lens", "panelIndex": "p1", "panelRefName": "panel_0"},
        ]
        references = [
            {"name": "panel_0", "type": "lens", "id": "viz-missing"},
        ]
        dash_body = _dashboard_object("d5", "Partial", panels, references)

        mock_get.side_effect = [
            _mock_response(json_data=dash_body),
            _mock_response(status_code=404, text="Not found"),
        ]

        result = KibanaService.get_full_dashboard_with_visualizations("d5")

        assert result is not None
        assert result["panels"] == []


# ---------------------------------------------------------------------------
# delete_dashboard
# ---------------------------------------------------------------------------

class TestDeleteDashboard:

    @mock.patch("asset_management.web.services.kibana_service.requests.delete")
    def test_success_200(self, mock_delete):
        mock_delete.return_value = _mock_response(status_code=200)

        result = KibanaService.delete_dashboard("dash-1")

        assert result is True
        url = mock_delete.call_args[0][0]
        assert "dash-1" in url
        assert "/s/" not in url

    @mock.patch("asset_management.web.services.kibana_service.requests.delete")
    def test_success_204(self, mock_delete):
        mock_delete.return_value = _mock_response(status_code=204)

        result = KibanaService.delete_dashboard("dash-1")

        assert result is True

    @mock.patch("asset_management.web.services.kibana_service.requests.delete")
    def test_custom_space_in_url(self, mock_delete):
        mock_delete.return_value = _mock_response(status_code=200)

        KibanaService.delete_dashboard("dash-2", space_id="marketing")

        url = mock_delete.call_args[0][0]
        assert "/s/marketing/" in url

    @mock.patch("asset_management.web.services.kibana_service.requests.delete")
    def test_default_space_no_prefix(self, mock_delete):
        mock_delete.return_value = _mock_response(status_code=200)

        KibanaService.delete_dashboard("dash-3", space_id="default")

        url = mock_delete.call_args[0][0]
        assert "/s/default/" not in url

    @mock.patch("asset_management.web.services.kibana_service.requests.delete")
    def test_non_200_returns_false(self, mock_delete):
        mock_delete.return_value = _mock_response(status_code=404, text="Not found")

        result = KibanaService.delete_dashboard("dash-missing")

        assert result is False

    @mock.patch("asset_management.web.services.kibana_service.requests.delete")
    def test_connection_error_returns_false(self, mock_delete):
        mock_delete.side_effect = requests.exceptions.ConnectionError("refused")

        result = KibanaService.delete_dashboard("dash-1")

        assert result is False

    @mock.patch("asset_management.web.services.kibana_service.requests.delete")
    def test_generic_exception_returns_false(self, mock_delete):
        mock_delete.side_effect = Exception("unexpected")

        result = KibanaService.delete_dashboard("dash-1")

        assert result is False

    @mock.patch.dict("os.environ", {"ES01_PASSWORD": ""}, clear=False)
    def test_missing_password_returns_false(self):
        with mock.patch.dict("os.environ", {"ES01_PASSWORD": ""}):
            result = KibanaService.delete_dashboard("dash-1")
        assert result is False

    @mock.patch("asset_management.web.services.kibana_service.requests.delete")
    def test_uses_tls_verify_and_kbn_xsrf(self, mock_delete):
        mock_delete.return_value = _mock_response(status_code=200)

        KibanaService.delete_dashboard("dash-1")

        kwargs = mock_delete.call_args[1]
        # TLS verification is no longer disabled (was verify=False).
        assert kwargs["verify"] is not False
        assert kwargs["headers"]["kbn-xsrf"] == "true"
        assert kwargs["auth"] == ("elastic", mock.ANY)
