"""Tests for _query_team_title_country_map, _query_dt_team_country_map, _query_dt_map."""
import os
import sys
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.blueprints.dashboard_builder.routes import (
    _query_team_title_country_map,
    _query_dt_team_country_map,
    _query_dt_map,
)


def _es_team_title(buckets):
    """mock ES for the dual-field team_title agg.

    ``_terms_agg_by_team_title`` queries BOTH ``team_title.keyword`` and bare
    ``team_title`` and merges. A real cluster returns buckets under exactly one
    mapping; mirror that (buckets for ``.keyword``, empty for bare) so the merge
    isn't a double-count. (The merge itself is covered by
    the release-plan tests)
    """
    mock_es = mock.MagicMock()

    def _search(index=None, body=None, ignore_unavailable=False):
        field = body["aggs"]["by_team"]["terms"]["field"]
        out = buckets if field == "team_title.keyword" else []
        resp = mock.MagicMock()
        resp.body = {"aggregations": {"by_team": {"buckets": out}}}
        return resp

    mock_es.search.side_effect = _search
    return mock_es


# ===========================================================================
# _query_team_title_country_map (lines 1524-1566)
# ===========================================================================

class TestQueryTeamTitleCountryMap:
    def test_returns_empty_when_team_map_empty(self):
        mock_es = mock.MagicMock()
        stat_cfg = {"field": "views", "type": "range", "gte": 0}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={},
        ):
            result = _query_team_title_country_map(mock_es, "idx", stat_cfg, "", [], "", "")
        assert result == []

    def test_returns_country_points(self):
        # dual-field agg — buckets under.keyword, empty under bare
        # (a team lives in one mapping) so the merge doesn't double-count.
        mock_es = _es_team_title([{"key": "NFCY", "doc_count": 50, "total": {"value": 200}}])
        stat_cfg = {"field": "views", "type": "range", "gte": 0}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={"NFCY": "TR"},
        ):
            result = _query_team_title_country_map(mock_es, "idx", stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "TR"
        assert result[0]["value"] == 200

    def test_returns_empty_on_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        stat_cfg = {"field": "views", "type": "range", "gte": 0}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={"NFCY": "TR"},
        ):
            result = _query_team_title_country_map(mock_es, "idx", stat_cfg, "", [], "", "")
        assert result == []

    def test_skips_team_not_in_map(self):
        mock_es = _es_team_title([{"key": "UNKNOWN_TEAM", "doc_count": 10, "total": {"value": 50}}])
        stat_cfg = {"field": "views", "type": "range", "gte": 0}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={"NFCY": "TR"},
        ):
            result = _query_team_title_country_map(mock_es, "idx", stat_cfg, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_dt_team_country_map (lines 1576-1628)
# ===========================================================================

class TestQueryDtTeamCountryMap:
    def test_returns_empty_when_team_map_empty(self):
        mock_es = mock.MagicMock()
        stat_cfg = {"type": "exists", "field": "contact_count"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={},
        ):
            result = _query_dt_team_country_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_returns_country_points(self):
        # dual-field agg — buckets under.keyword, empty under bare.
        mock_es = _es_team_title([{"key": "NFCY", "doc_count": 120}])
        stat_cfg = {"type": "exists", "field": "contact_count"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={"NFCY": "TR"},
        ):
            result = _query_dt_team_country_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "TR"
        assert result[0]["value"] == 120

    def test_falls_back_to_plain_team_title(self):
        """First search raises exception -> falls back to plain team_title field."""
        mock_es = mock.MagicMock()
        resp2 = mock.MagicMock()
        resp2.body = {
            "aggregations": {
                "by_team": {
                    "buckets": [{"key": "VOGZ", "doc_count": 80}]
                }
            }
        }
        mock_es.search.side_effect = [Exception("keyword field error"), None]
        mock_es.search.return_value = resp2
        # First call raises, second returns data
        mock_es.search.side_effect = [Exception("keyword error"), resp2]
        stat_cfg = {"type": "exists", "field": "contact_count"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={"VOGZ": "MX"},
        ):
            result = _query_dt_team_country_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "MX"

    def test_returns_empty_when_both_searches_fail(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        stat_cfg = {"type": "exists", "field": "contact_count"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_team_country_map",
            return_value={"NFCY": "TR"},
        ):
            result = _query_dt_team_country_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_dt_map (lines 1640-1698)
# ===========================================================================

class TestQueryDtMap:
    def _make_resp(self, grid_buckets=None, address_buckets=None):
        resp = mock.MagicMock()
        resp.body = {
            "aggregations": {
                "by_grid": {"buckets": grid_buckets or []},
                "by_address": {"buckets": address_buckets or []},
            }
        }
        return resp

    def test_returns_geocoded_address_points(self):
        """Address in by_address -> _geocode_address -> coords -> point."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._make_resp(
            address_buckets=[{"key": "Istanbul, Turkey", "doc_count": 30}]
        )
        stat_cfg = {"type": "exists", "field": "contact_count"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._geocode_address",
            return_value=(41.01, 28.97),
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes._load_dt_grid_names",
                return_value={},
            ):
                result = _query_dt_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "Istanbul, Turkey"

    def test_skips_address_without_geocode(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._make_resp(
            address_buckets=[{"key": "Unknown", "doc_count": 5}]
        )
        stat_cfg = {"type": "exists", "field": "contact_count"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._geocode_address",
            return_value=None,
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes._load_dt_grid_names",
                return_value={},
            ):
                result = _query_dt_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_returns_grid_points_via_geocode_address(self):
        """Grid ID -> name via _load_dt_grid_names -> coords via _geocode_address."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._make_resp(
            grid_buckets=[{"key": "12345", "doc_count": 50}]
        )
        stat_cfg = {"type": "exists", "field": "contact_count"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_grid_names",
            return_value={"12345": "Ankara"},
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes._geocode_address",
                return_value=(39.9, 32.8),
            ):
                result = _query_dt_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
        assert result[0]["lat"] == 39.9

    def test_skips_grid_key_not_in_names(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._make_resp(
            grid_buckets=[{"key": "99999", "doc_count": 10}]
        )
        stat_cfg = {"type": "exists", "field": "contact_count"}
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._load_dt_grid_names",
            return_value={},  # 99999 not in map
        ):
            result = _query_dt_map(mock_es, stat_cfg, "", [], "", "")
        assert result == []

    def test_deduplicates_same_coords(self):
        """Two addresses resolve to same rounded coords -> only one point."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._make_resp(
            address_buckets=[
                {"key": "Address A", "doc_count": 20},
                {"key": "Address B", "doc_count": 15},
            ]
        )
        stat_cfg = {"type": "exists", "field": "contact_count"}
        # Both return identical coords
        with mock.patch(
            "asset_management.web.blueprints.dashboard_builder.routes._geocode_address",
            return_value=(41.01, 28.97),
        ):
            with mock.patch(
                "asset_management.web.blueprints.dashboard_builder.routes._load_dt_grid_names",
                return_value={},
            ):
                result = _query_dt_map(mock_es, stat_cfg, "", [], "", "")
        assert len(result) == 1
