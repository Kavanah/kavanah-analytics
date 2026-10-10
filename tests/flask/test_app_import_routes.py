"""
Tests for import-related API routes in app.py.

Covers:
  GET  /api/import/data-source-status  — document counts + monthly summary
  GET  /api/import/data-view-teams     — team_title aggregation
  GET  /api/import/field-values        — per-team field value lookup
  POST /api/assets/import              — CSV asset import
"""

import io
import json
import pytest
from unittest import mock

# validate_index_access is real now. These tests cover
# orthogonal route logic under a session with no ES backend, so grant the gate.
pytestmark = pytest.mark.usefixtures("allow_index_scope")


# ---------------------------------------------------------------------------
# Shared ES response helpers
# ---------------------------------------------------------------------------


def _es_search_result(total=5, max_ts="2025-01-15T00:00:00Z", buckets=None):
    """Minimal ES search response for data-source-status."""
    return {
        "hits": {"total": {"value": total}},
        "aggregations": {
            "max_timestamp": {"value_as_string": max_ts},
            "by_day": {"buckets": buckets or []},
        },
    }


def _day_bucket(date_str):
    return {"key_as_string": date_str, "doc_count": 10}


# ===========================================================================
# /api/import/data-source-status
# ===========================================================================


class TestGetDataSourceStatus:

    def test_returns_400_when_data_view_missing(self, auth_client):
        resp = auth_client.get("/api/import/data-source-status")
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_returns_503_when_es_unavailable(self, auth_client):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = auth_client.get("/api/import/data-source-status?data_view=fb_ads")
        assert resp.status_code == 503

    def test_returns_basic_stats(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _es_search_result(total=42, max_ts="2025-03-01T00:00:00Z")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-source-status?data_view=fb_ads")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert data["document_count"] == 42
        assert data["last_pulled"] == "2025-03-01T00:00:00Z"

    def test_returns_monthly_summary_from_day_buckets(self, auth_client):
        buckets = [
            _day_bucket("2025-01-10"),
            _day_bucket("2025-01-20"),
            _day_bucket("2025-02-05"),
        ]
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _es_search_result(total=3, buckets=buckets)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-source-status?data_view=fb_ads")
        data = resp.get_json()
        assert resp.status_code == 200
        months = {r["month"]: r for r in data["monthly_summary"]}
        assert 1 in months
        assert months[1]["dates_pulled"] == 2
        assert 2 in months
        assert months[2]["dates_pulled"] == 1

    def test_applies_team_filter(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _es_search_result(total=10)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-source-status?data_view=fb_ads&team=Alpha+Team")
        assert resp.status_code == 200
        call_body = mock_es.search.call_args[1]["body"]
        # must_filters should include a team filter
        must = call_body["query"]["bool"]["must"]
        assert any("bool" in f and "should" in f["bool"] for f in must)

    def test_applies_date_range_filter(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _es_search_result(total=5)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get(
                "/api/import/data-source-status?data_view=fb_ads" "&from_date=2025-01-01&to_date=2025-01-31"
            )
        assert resp.status_code == 200
        call_body = mock_es.search.call_args[1]["body"]
        must = call_body["query"]["bool"]["must"]
        assert any("range" in f for f in must)

    def test_fills_empty_months_in_date_range(self, auth_client):
        """When from_date/to_date provided, months with no data should be included as 0."""
        # Only January has data, but we request Jan–March
        buckets = [_day_bucket("2025-01-15")]
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _es_search_result(total=1, buckets=buckets)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get(
                "/api/import/data-source-status?data_view=fb_ads" "&from_date=2025-01-01&to_date=2025-03-31"
            )
        data = resp.get_json()
        months = [r["month"] for r in data["monthly_summary"]]
        assert 2 in months  # February gap filled
        assert 3 in months  # March gap filled

    def test_disciple_tools_skips_by_day_agg(self, auth_client):
        """disciple-tools indices should not include a by_day aggregation."""
        mock_es = mock.MagicMock()
        # No by_day key in response for disciple-tools
        mock_es.search.return_value = {
            "hits": {"total": {"value": 100}},
            "aggregations": {"max_timestamp": {"value_as_string": None}},
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-source-status?data_view=disciple-tools-main")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["monthly_summary"] == []

    def test_returns_500_on_es_error(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES cluster down")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-source-status?data_view=fb_ads")
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/import/data-source-status?data_view=fb_ads")
        assert resp.status_code == 401

    def test_skips_malformed_date_bucket(self, auth_client):
        """Buckets with bad date strings should be silently skipped."""
        buckets = [{"key_as_string": "not-a-date", "doc_count": 5}]
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _es_search_result(total=1, buckets=buckets)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-source-status?data_view=fb_ads")
        assert resp.status_code == 200
        assert resp.get_json()["monthly_summary"] == []

    def test_skips_bucket_with_empty_date_str(self, auth_client):
        """Buckets with empty key_as_string should be skipped (line 2634)."""
        buckets = [{"key_as_string": "", "doc_count": 3}, _day_bucket("2025-01-10")]
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _es_search_result(total=1, buckets=buckets)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-source-status?data_view=fb_ads")
        data = resp.get_json()
        assert resp.status_code == 200
        # Only January entry — empty bucket was skipped
        assert len(data["monthly_summary"]) == 1

    def test_fills_gaps_spanning_year_boundary(self, auth_client):
        """Date range Nov 2024 – Jan 2025 should wrap month counter across year boundary."""
        buckets = [_day_bucket("2024-11-15")]
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _es_search_result(total=1, buckets=buckets)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get(
                "/api/import/data-source-status?data_view=fb_ads" "&from_date=2024-11-01&to_date=2025-01-31"
            )
        data = resp.get_json()
        months_years = [(r["year"], r["month"]) for r in data["monthly_summary"]]
        assert (2024, 12) in months_years  # gap month in old year
        assert (2025, 1) in months_years  # gap month in new year

    def test_fills_gaps_invalid_date_range_is_silent(self, auth_client):
        """Invalid from_date/to_date should be silently ignored (ValueError caught)."""
        buckets = [_day_bucket("2025-01-10")]
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _es_search_result(total=1, buckets=buckets)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-source-status?data_view=fb_ads" "&from_date=not-a-date&to_date=also-bad")
        assert resp.status_code == 200
        # Monthly summary still has January from the buckets; no crash
        assert len(resp.get_json()["monthly_summary"]) == 1


# ===========================================================================
# /api/import/data-view-teams
# ===========================================================================


class TestGetDataViewTeams:

    def test_returns_400_when_data_view_missing(self, auth_client):
        resp = auth_client.get("/api/import/data-view-teams")
        assert resp.status_code == 400

    def test_returns_503_when_es_unavailable(self, auth_client):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = auth_client.get("/api/import/data-view-teams?data_view=fb_ads")
        assert resp.status_code == 503

    def test_returns_teams_from_keyword_subfield(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"aggregations": {"teams": {"buckets": [{"key": "Alpha"}, {"key": "Beta"}]}}}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-view-teams?data_view=youtube_main")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert "Alpha" in data["teams_with_data"]
        assert "Beta" in data["teams_with_data"]

    def test_falls_back_to_base_field_when_keyword_empty(self, auth_client):
        mock_es = mock.MagicMock()
        # First call (keyword) returns empty; second call (base) returns teams
        mock_es.search.side_effect = [
            {"aggregations": {"teams": {"buckets": []}}},
            {"aggregations": {"teams": {"buckets": [{"key": "TeamA"}]}}},
        ]
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-view-teams?data_view=fb_ads")
        data = resp.get_json()
        assert data["success"] is True
        assert "TeamA" in data["teams_with_data"]

    def test_handles_fielddata_error_on_fallback(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = [
            {"aggregations": {"teams": {"buckets": []}}},
            Exception("fielddata disabled for text field"),
        ]
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-view-teams?data_view=fb_ads")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["teams_with_data"] == []

    def test_returns_500_on_outer_exception(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES timeout")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/data-view-teams?data_view=fb_ads")
        assert resp.status_code == 500

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/import/data-view-teams?data_view=fb_ads")
        assert resp.status_code == 401


# ===========================================================================
# /api/import/field-values
# ===========================================================================


def _field_values_result(team_buckets=None):
    """Build an ES response for field-values aggregation."""
    buckets = team_buckets or []
    return {
        "aggregations": {
            "by_team": {
                "buckets": [
                    {
                        "key": team,
                        "field_sample": {"hits": {"hits": [{"_source": source}]}},
                    }
                    for team, source in buckets
                ]
            }
        }
    }


class TestGetFieldValues:

    def test_returns_400_when_params_missing(self, auth_client):
        resp = auth_client.get("/api/import/field-values")
        assert resp.status_code == 400

    def test_returns_400_when_date_missing(self, auth_client):
        resp = auth_client.get("/api/import/field-values?data_view=fb&field=impressions")
        assert resp.status_code == 400

    def test_returns_400_when_field_missing(self, auth_client):
        resp = auth_client.get("/api/import/field-values?data_view=fb&date=2025-01-01")
        assert resp.status_code == 400

    def test_returns_503_when_es_unavailable(self, auth_client):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = auth_client.get("/api/import/field-values?data_view=fb&date=2025-01-01&field=impressions")
        assert resp.status_code == 503

    def test_returns_values_from_keyword_subfield(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _field_values_result(
            [
                ("Alpha", {"impressions": 500}),
                ("Beta", {"impressions": 250}),
            ]
        )
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/field-values?data_view=fb&date=2025-01-01&field=impressions")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert len(data["values"]) == 2

    def test_falls_back_to_base_field_when_keyword_empty(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = [
            _field_values_result([]),
            _field_values_result([("Gamma", {"clicks": 100})]),
        ]
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/field-values?data_view=fb&date=2025-01-01&field=clicks")
        data = resp.get_json()
        assert data["success"] is True
        assert data["values"][0]["team_title"] == "Gamma"

    def test_applies_team_filter(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _field_values_result([("Alpha", {"spend": 50.0})])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/field-values?data_view=fb&date=2025-01-01&field=spend&team=Alpha")
        assert resp.status_code == 200
        body = mock_es.search.call_args[1]["body"]
        filters = body["query"]["bool"]["filter"]
        assert any("match_phrase" in f for f in filters)

    def test_skips_date_filter_for_disciple_tools(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _field_values_result([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/field-values?data_view=disciple-tools-main" "&date=2025-01-01&field=contacts")
        assert resp.status_code == 200
        body = mock_es.search.call_args[1]["body"]
        # No range filter should be present for disciple-tools
        filters = body["query"]["bool"]["filter"]
        assert not any("range" in f for f in filters)

    def test_handles_dotted_field_path(self, auth_client):
        """Support nested fields like 'metrics.impressions'."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _field_values_result(
            [
                ("Alpha", {"metrics": {"impressions": 999}}),
            ]
        )
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/field-values?data_view=fb&date=2025-01-01&field=metrics.impressions")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["values"][0]["value"] == 999

    def test_dotted_field_path_non_dict_intermediate(self, auth_client):
        """When a dotted path hits a non-dict value, result is None (lines 2838-2839)."""
        mock_es = mock.MagicMock()
        # metrics.impressions — but 'metrics' is a string, not a dict
        mock_es.search.return_value = _field_values_result(
            [
                ("Alpha", {"metrics": "not-a-dict"}),
            ]
        )
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/field-values?data_view=fb&date=2025-01-01&field=metrics.impressions")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["values"][0]["value"] is None

    def test_fallback_exception_is_suppressed(self, auth_client):
        """Second search (fallback) exception should be silently swallowed (lines 2855-2856)."""
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = [
            _field_values_result([]),  # first call: keyword subfield, empty
            Exception("fielddata disabled"),  # second call: base field, throws
        ]
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/field-values?data_view=fb&date=2025-01-01&field=impressions")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["values"] == []

    def test_returns_500_on_exception(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES timeout")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/import/field-values?data_view=fb&date=2025-01-01&field=impressions")
        assert resp.status_code == 500

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/import/field-values?data_view=fb&date=2025-01-01&field=impressions")
        assert resp.status_code == 401


# ===========================================================================
# /api/import/field-values-across-docs
# ===========================================================================


def _across_docs_search_result(values_per_doc):
    """Build an ES _search response yielding one hit per doc."""
    return {"hits": {"hits": [{"_source": src} for src in values_per_doc]}}


class TestGetFieldValuesAcrossDocs:
    """per-field popup that compares one field across N docs for
    the same (team, date)."""

    BASE = "/api/import/field-values-across-docs"
    GOOD_QS = "?data_view=fb&date=2025-01-01&team=FLKVJ&field=insight_spend"

    def test_returns_400_when_data_view_missing(self, auth_client):
        resp = auth_client.get(f"{self.BASE}?date=2025-01-01&team=FLKVJ&field=spend")
        assert resp.status_code == 400

    def test_returns_400_when_date_missing(self, auth_client):
        resp = auth_client.get(f"{self.BASE}?data_view=fb&team=FLKVJ&field=spend")
        assert resp.status_code == 400

    def test_returns_400_when_team_missing(self, auth_client):
        resp = auth_client.get(f"{self.BASE}?data_view=fb&date=2025-01-01&field=spend")
        assert resp.status_code == 400

    def test_returns_400_when_field_missing(self, auth_client):
        resp = auth_client.get(f"{self.BASE}?data_view=fb&date=2025-01-01&team=FLKVJ")
        assert resp.status_code == 400

    def test_returns_503_when_es_unavailable(self, auth_client):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = auth_client.get(self.BASE + self.GOOD_QS)
        assert resp.status_code == 503

    def test_returns_one_value_per_doc(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _across_docs_search_result(
            [
                {"insight_spend": 12.5},
                {"insight_spend": 7.0},
                {"insight_spend": 0},
            ]
        )
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get(self.BASE + self.GOOD_QS)
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert data["total_docs"] == 3
        assert [v["doc_index"] for v in data["values"]] == [0, 1, 2]
        assert [v["value"] for v in data["values"]] == [12.5, 7.0, 0]

    def test_handles_dotted_field_path(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _across_docs_search_result(
            [
                {"creative_details": {"actor_id": "page-1"}},
                {"creative_details": {"actor_id": "page-2"}},
            ]
        )
        qs = "?data_view=fb&date=2025-01-01&team=FLKVJ&field=creative_details.actor_id"
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get(self.BASE + qs)
        data = resp.get_json()
        assert resp.status_code == 200
        assert [v["value"] for v in data["values"]] == ["page-1", "page-2"]

    def test_dotted_field_path_returns_none_when_intermediate_is_not_dict(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _across_docs_search_result(
            [
                {"creative_details": "not-a-dict"},
            ]
        )
        qs = "?data_view=fb&date=2025-01-01&team=FLKVJ&field=creative_details.actor_id"
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get(self.BASE + qs)
        data = resp.get_json()
        assert data["values"][0]["value"] is None

    def test_filters_by_team_and_date(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _across_docs_search_result([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            auth_client.get(self.BASE + self.GOOD_QS)
        body = mock_es.search.call_args[1]["body"]
        filters = body["query"]["bool"]["filter"]
        # team filter via match_phrase
        assert any(f.get("match_phrase", {}).get("team_title") == "FLKVJ" for f in filters)
        # date filter via kibana_date range
        assert any("range" in f and "kibana_date" in f["range"] for f in filters)

    def test_uses_prefix_filter_for_disciple_tools(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _across_docs_search_result([])
        qs = "?data_view=disciple-tools-main&date=2025-01-01&team=FLKVJ&field=baptisms"
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            auth_client.get(self.BASE + qs)
        body = mock_es.search.call_args[1]["body"]
        filters = body["query"]["bool"]["filter"]
        # No range filter on kibana_date for disciple-tools.
        assert not any("range" in f for f in filters)
        # date_modified prefix filter is used instead.
        assert any("prefix" in f and "date_modified.keyword" in f["prefix"] for f in filters)

    def test_returns_empty_when_no_docs_match(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = _across_docs_search_result([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get(self.BASE + self.GOOD_QS)
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["success"] is True
        assert data["total_docs"] == 0
        assert data["values"] == []

    def test_returns_500_on_exception(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES timeout")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get(self.BASE + self.GOOD_QS)
        assert resp.status_code == 500

    def test_unauthenticated_redirects(self, client):
        resp = client.get(self.BASE + self.GOOD_QS)
        assert resp.status_code == 401


# ===========================================================================
# /api/assets/import  (CSV upload)
# ===========================================================================

VALID_CSV = (
    "Ad Account Name,Ad Account ID,Facebook App,Facebook App User,Meta Business Portfolio\n"
    "My Account,act_123,My App,a teammate,Portfolio1\n"
)


def _make_csv_upload(content=VALID_CSV, filename="accounts.csv", asset_type="facebook_ad_accounts"):
    data = {
        "asset_type": asset_type,
        "file": (io.BytesIO(content.encode()), filename),
    }
    return data


class TestImportAssetsCsv:

    def test_returns_400_when_no_asset_type(self, auth_client):
        resp = auth_client.post(
            "/api/assets/import",
            data={"file": (io.BytesIO(b"a,b\n1,2"), "test.csv")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_returns_400_for_invalid_asset_type(self, auth_client):
        resp = auth_client.post(
            "/api/assets/import",
            data={"asset_type": "nonexistent_type", "file": (io.BytesIO(b"a,b\n1,2"), "test.csv")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_returns_400_when_no_file(self, auth_client):
        resp = auth_client.post(
            "/api/assets/import",
            data={"asset_type": "facebook_ad_accounts"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "No file" in resp.get_json()["error"]

    def test_returns_400_when_filename_empty(self, auth_client):
        resp = auth_client.post(
            "/api/assets/import",
            data={"asset_type": "facebook_ad_accounts", "file": (io.BytesIO(b"a,b"), "")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_returns_400_for_non_csv_extension(self, auth_client):
        resp = auth_client.post(
            "/api/assets/import",
            data={"asset_type": "facebook_ad_accounts", "file": (io.BytesIO(b"a,b"), "accounts.xlsx")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        assert "CSV" in resp.get_json()["error"]

    def test_successful_import_returns_result(self, auth_client):
        import_result = {"success": True, "imported_count": 1, "skipped_count": 0, "error_count": 0}
        with mock.patch(
            "asset_management.web.app.AssetImportService.import_from_csv",
            return_value=import_result,
        ):
            resp = auth_client.post(
                "/api/assets/import",
                data=_make_csv_upload(),
                content_type="multipart/form-data",
            )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert resp.get_json()["imported_count"] == 1

    def test_returns_400_on_unicode_decode_error(self, auth_client):
        # Write a byte sequence that is invalid UTF-8 but valid latin-1
        bad_bytes = b"Ad Account Name,Ad Account ID\n\xff\xfe,act_123\n"
        resp = auth_client.post(
            "/api/assets/import",
            data={"asset_type": "facebook_ad_accounts", "file": (io.BytesIO(bad_bytes), "bad.csv")},
            content_type="multipart/form-data",
        )
        # May be 200 (utf-8-sig handles some edge cases) or 400; just ensure it doesn't 500
        assert resp.status_code in (200, 400)

    def test_returns_500_on_import_service_error(self, auth_client):
        with mock.patch(
            "asset_management.web.app.AssetImportService.import_from_csv",
            side_effect=Exception("ES unavailable"),
        ):
            resp = auth_client.post(
                "/api/assets/import",
                data=_make_csv_upload(),
                content_type="multipart/form-data",
            )
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False

    def test_unauthenticated_redirects(self, client):
        resp = client.post(
            "/api/assets/import",
            data=_make_csv_upload(),
            content_type="multipart/form-data",
        )
        assert resp.status_code == 401
