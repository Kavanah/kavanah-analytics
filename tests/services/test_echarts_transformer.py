"""
Tests for the ECharts Transformer service.

Covers:
  _safe_agg_key    — dot/hyphen replacement (mirror of es_query_builder)
  _get_agg_value   — standard metric, filter (doc_count), top_hits, missing
  _format_value    — float rounding, int passthrough
  transform        — metric, table, pie, bar, line chart types
"""

import pytest

from asset_management.web.services.echarts_transformer import (
    _format_value,
    _get_agg_value,
    _safe_agg_key,
    transform,
)


# ---------------------------------------------------------------------------
# _safe_agg_key
# ---------------------------------------------------------------------------

class TestSafeAggKey:

    def test_replaces_dots(self):
        assert _safe_agg_key("stats.impressions") == "stats__impressions"

    def test_replaces_hyphens(self):
        assert _safe_agg_key("ad-group") == "ad_group"

    def test_plain_name_unchanged(self):
        assert _safe_agg_key("impressions") == "impressions"


# ---------------------------------------------------------------------------
# _get_agg_value
# ---------------------------------------------------------------------------

class TestGetAggValue:

    def test_standard_metric_returns_value(self):
        bucket = {"metric_value": {"value": 42.5}}
        assert _get_agg_value(bucket) == 42.5

    def test_missing_agg_returns_zero(self):
        assert _get_agg_value({}) == 0

    def test_named_agg(self):
        bucket = {"metric_clicks": {"value": 100}}
        assert _get_agg_value(bucket, "metric_clicks") == 100

    def test_filter_agg_returns_doc_count(self):
        bucket = {"metric_value": {"doc_count": 25}}
        assert _get_agg_value(bucket) == 25

    def test_filter_agg_zero_doc_count(self):
        bucket = {"metric_value": {"doc_count": 0}}
        assert _get_agg_value(bucket) == 0

    def test_top_hits_returns_first_source_value(self):
        bucket = {
            "metric_value": {
                "hits": {
                    "hits": [{"_source": {"team_title": "Alpha Team"}}]
                }
            }
        }
        assert _get_agg_value(bucket) == "Alpha Team"

    def test_top_hits_empty_hits_returns_empty_string(self):
        bucket = {"metric_value": {"hits": {"hits": []}}}
        assert _get_agg_value(bucket) == ""

    def test_top_hits_empty_source_returns_empty_string(self):
        bucket = {"metric_value": {"hits": {"hits": [{"_source": {}}]}}}
        assert _get_agg_value(bucket) == ""

    def test_null_value_returns_zero(self):
        bucket = {"metric_value": {"value": None}}
        assert _get_agg_value(bucket) == 0

    def test_null_doc_count_returns_zero(self):
        bucket = {"metric_value": {"doc_count": None}}
        assert _get_agg_value(bucket) == 0


# ---------------------------------------------------------------------------
# _format_value
# ---------------------------------------------------------------------------

class TestFormatValue:

    def test_rounds_float(self):
        assert _format_value(3.14159) == 3.14

    def test_int_passthrough(self):
        assert _format_value(42) == 42

    def test_string_passthrough(self):
        assert _format_value("hello") == "hello"

    def test_zero_float(self):
        assert _format_value(0.0) == 0.0

    def test_negative_float(self):
        assert _format_value(-1.999) == -2.0


# ---------------------------------------------------------------------------
# transform — metric chart
# ---------------------------------------------------------------------------

class TestTransformMetric:

    def test_metric_simple(self):
        es_resp = {"aggregations": {"metric_impressions": {"value": 1234}}}
        config = {"chart_type": "metric", "y_fields": ["impressions"], "aggregation": "sum"}
        result = transform(es_resp, config)
        assert result["chart_type"] == "metric"
        assert result["value"] == 1234
        assert result["label"] == "impressions"
        assert result["aggregation"] == "sum"

    def test_metric_with_breakdown(self):
        es_resp = {
            "aggregations": {
                "breakdown_buckets": {
                    "buckets": [
                        {"key": "Facebook", "doc_count": 100},
                        {"key": "Google", "doc_count": 50},
                    ]
                }
            }
        }
        config = {"chart_type": "metric", "y_fields": ["impressions"], "breakdown": "platform", "aggregation": "count"}
        result = transform(es_resp, config)
        assert result["chart_type"] == "metric"
        assert len(result["breakdowns"]) == 2
        assert result["breakdowns"][0] == {"label": "Facebook", "value": 100}

    def test_metric_no_y_field(self):
        es_resp = {"aggregations": {}}
        config = {"chart_type": "metric", "aggregation": "count"}
        result = transform(es_resp, config)
        assert result["chart_type"] == "metric"
        assert result["value"] == 0

    def test_metric_legacy_y_field(self):
        es_resp = {"aggregations": {"metric_clicks": {"value": 99}}}
        config = {"chart_type": "metric", "y_field": "clicks", "aggregation": "sum"}
        result = transform(es_resp, config)
        assert result["value"] == 99

    def test_metric_float_value_rounded(self):
        es_resp = {"aggregations": {"metric_cost": {"value": 123.456789}}}
        config = {"chart_type": "metric", "y_fields": ["cost"], "aggregation": "avg"}
        result = transform(es_resp, config)
        assert result["value"] == 123.46

    def test_metric_dotted_field(self):
        es_resp = {"aggregations": {"metric_stats__impressions": {"value": 500}}}
        config = {"chart_type": "metric", "y_fields": ["stats.impressions"], "aggregation": "sum"}
        result = transform(es_resp, config)
        assert result["value"] == 500


# ---------------------------------------------------------------------------
# transform — table chart
# ---------------------------------------------------------------------------

class TestTransformTable:

    def test_table_basic(self):
        es_resp = {
            "aggregations": {
                "table_buckets": {
                    "buckets": [
                        {"key": {"campaign": "Camp A"}, "metric_impressions": {"value": 100}},
                        {"key": {"campaign": "Camp B"}, "metric_impressions": {"value": 200}},
                    ]
                }
            }
        }
        config = {
            "chart_type": "table",
            "x_field": "campaign",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = transform(es_resp, config)
        assert result["chart_type"] == "table"
        assert result["columns"] == ["campaign", "impressions"]
        assert result["rows"] == [["Camp A", 100], ["Camp B", 200]]

    def test_table_with_x_field_2(self):
        es_resp = {
            "aggregations": {
                "table_buckets": {
                    "buckets": [
                        {"key": {"campaign": "A", "ad_group": "G1"}, "metric_clicks": {"value": 10}},
                    ]
                }
            }
        }
        config = {
            "chart_type": "table",
            "x_field": "campaign",
            "x_field_2": "ad_group",
            "y_fields": ["clicks"],
            "aggregation": "sum",
        }
        result = transform(es_resp, config)
        assert result["columns"] == ["campaign", "ad_group", "clicks"]
        assert result["rows"] == [["A", "G1", 10]]

    def test_table_with_table_columns_and_decimals(self):
        es_resp = {
            "aggregations": {
                "table_buckets": {
                    "buckets": [
                        {"key": {"team": "Alpha"}, "metric_cost": {"value": 1.23456}, "metric_clicks": {"value": 5}},
                    ]
                }
            }
        }
        config = {
            "chart_type": "table",
            "x_field": "team",
            "table_columns": [
                {"field": "cost", "aggregation": "sum", "decimals": 3},
                {"field": "clicks", "aggregation": "sum", "decimals": 0},
            ],
        }
        result = transform(es_resp, config)
        assert result["rows"][0] == ["Alpha", 1.235, 5]

    def test_table_auto_promoted_text_column(self):
        es_resp = {
            "aggregations": {
                "table_buckets": {
                    "buckets": [
                        {"key": {"team_title": "Alpha"}, "metric_impressions": {"value": 100}},
                    ]
                }
            }
        }
        config = {
            "chart_type": "table",
            "y_fields": ["impressions"],
            "table_columns": [
                {"field": "team_title", "aggregation": "text"},
                {"field": "impressions", "aggregation": "sum"},
            ],
        }
        result = transform(es_resp, config)
        assert "team_title" in result["columns"]
        assert "impressions" in result["columns"]

    def test_table_keyword_suffix_stripped_for_display(self):
        es_resp = {
            "aggregations": {
                "table_buckets": {
                    "buckets": [
                        {"key": {"team_title.keyword": "Alpha"}, "metric_impressions": {"value": 100}},
                    ]
                }
            }
        }
        config = {
            "chart_type": "table",
            "x_field": "team_title.keyword",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = transform(es_resp, config)
        assert result["columns"][0] == "team_title"

    def test_table_empty_buckets(self):
        es_resp = {"aggregations": {"table_buckets": {"buckets": []}}}
        config = {"chart_type": "table", "x_field": "campaign", "y_fields": ["impressions"], "aggregation": "sum"}
        result = transform(es_resp, config)
        assert result["rows"] == []

    def test_table_no_x_field_derives_from_first_bucket_key(self):
        es_resp = {
            "aggregations": {
                "table_buckets": {
                    "buckets": [
                        {"key": {"team_title": "Alpha"}, "metric_impressions": {"value": 100}},
                    ]
                }
            }
        }
        config = {
            "chart_type": "table",
            "y_fields": ["team_title", "impressions"],
            "aggregation": "sum",
        }
        result = transform(es_resp, config)
        assert result["columns"][0] == "team_title"


# ---------------------------------------------------------------------------
# transform — pie chart
# ---------------------------------------------------------------------------

class TestTransformPie:

    def test_pie_basic(self):
        es_resp = {
            "aggregations": {
                "x_buckets": {
                    "buckets": [
                        {"key": "Facebook", "metric_impressions": {"value": 300}},
                        {"key": "Google", "metric_impressions": {"value": 200}},
                    ]
                }
            }
        }
        config = {
            "chart_type": "pie",
            "x_field": "platform",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = transform(es_resp, config)
        assert "series" in result
        assert result["series"][0]["type"] == "pie"
        data = result["series"][0]["data"]
        assert len(data) == 2
        assert data[0] == {"name": "Facebook", "value": 300}

    def test_pie_legend_data(self):
        es_resp = {
            "aggregations": {
                "x_buckets": {
                    "buckets": [
                        {"key": "A", "metric_v": {"value": 1}},
                        {"key": "B", "metric_v": {"value": 2}},
                    ]
                }
            }
        }
        config = {"chart_type": "pie", "x_field": "x", "y_fields": ["v"], "aggregation": "sum"}
        result = transform(es_resp, config)
        assert result["legend"]["data"] == ["A", "B"]

    def test_pie_empty_buckets(self):
        es_resp = {"aggregations": {"x_buckets": {"buckets": []}}}
        config = {"chart_type": "pie", "x_field": "x", "y_fields": ["v"], "aggregation": "sum"}
        result = transform(es_resp, config)
        assert result["series"][0]["data"] == []


# ---------------------------------------------------------------------------
# transform — bar / line chart
# ---------------------------------------------------------------------------

class TestTransformBarLine:

    def _make_es_resp(self, buckets):
        return {"aggregations": {"x_buckets": {"buckets": buckets}}}

    def test_bar_basic(self):
        buckets = [
            {"key": "Camp A", "metric_impressions": {"value": 100}},
            {"key": "Camp B", "metric_impressions": {"value": 200}},
        ]
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = transform(self._make_es_resp(buckets), config)
        assert result["xAxis"]["data"] == ["Camp A", "Camp B"]
        assert len(result["series"]) == 1
        assert result["series"][0]["type"] == "bar"
        assert result["series"][0]["data"] == [100, 200]

    def test_line_basic(self):
        buckets = [
            {"key_as_string": "2024-01-01", "key": 1704067200000, "metric_impressions": {"value": 50}},
        ]
        config = {
            "chart_type": "line",
            "x_field": "date_start",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = transform(self._make_es_resp(buckets), config)
        assert result["series"][0]["type"] == "line"
        assert result["xAxis"]["data"] == ["2024-01-01"]

    def test_bar_multiple_y_fields(self):
        buckets = [
            {"key": "A", "metric_impressions": {"value": 100}, "metric_clicks": {"value": 10}},
        ]
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_fields": ["impressions", "clicks"],
            "aggregation": "sum",
        }
        result = transform(self._make_es_resp(buckets), config)
        assert len(result["series"]) == 2
        assert result["series"][0]["name"] == "impressions"
        assert result["series"][1]["name"] == "clicks"

    def test_bar_with_y_field_labels(self):
        buckets = [{"key": "A", "metric_impressions": {"value": 100}}]
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_fields": ["impressions"],
            "y_field_labels": {"impressions": "Total Impressions"},
            "aggregation": "sum",
        }
        result = transform(self._make_es_resp(buckets), config)
        assert result["series"][0]["name"] == "Total Impressions"
        assert result["legend"]["data"] == ["Total Impressions"]

    def test_bar_with_breakdown(self):
        buckets = [
            {
                "key": "Camp A",
                "breakdown_buckets": {
                    "buckets": [
                        {"key": "Facebook", "metric_impressions": {"value": 100}},
                        {"key": "Google", "metric_impressions": {"value": 50}},
                    ]
                },
            },
            {
                "key": "Camp B",
                "breakdown_buckets": {
                    "buckets": [
                        {"key": "Facebook", "metric_impressions": {"value": 200}},
                    ]
                },
            },
        ]
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_fields": ["impressions"],
            "breakdown": "platform",
            "aggregation": "sum",
        }
        result = transform(self._make_es_resp(buckets), config)
        assert len(result["series"]) == 2
        fb_series = next(s for s in result["series"] if s["name"] == "Facebook")
        assert fb_series["data"] == [100, 200]
        google_series = next(s for s in result["series"] if s["name"] == "Google")
        assert google_series["data"] == [50, 0]  # missing in Camp B → 0

    def test_bar_empty_buckets(self):
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = transform(self._make_es_resp([]), config)
        assert result["xAxis"]["data"] == []
        # Series entry exists but has empty data
        assert len(result["series"]) == 1
        assert result["series"][0]["data"] == []

    def test_bar_float_values_rounded(self):
        buckets = [{"key": "A", "metric_cost": {"value": 1.23456}}]
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_fields": ["cost"],
            "aggregation": "avg",
        }
        result = transform(self._make_es_resp(buckets), config)
        assert result["series"][0]["data"] == [1.23]


# ---------------------------------------------------------------------------
# transform — edge cases
# ---------------------------------------------------------------------------

class TestTransformEdgeCases:

    def test_empty_aggregations(self):
        es_resp = {"aggregations": {}}
        config = {"chart_type": "bar", "x_field": "x", "y_fields": ["y"], "aggregation": "sum"}
        result = transform(es_resp, config)
        assert result["xAxis"]["data"] == []

    def test_no_aggregations_key(self):
        es_resp = {}
        config = {"chart_type": "metric", "y_fields": ["impressions"], "aggregation": "sum"}
        result = transform(es_resp, config)
        assert result["chart_type"] == "metric"
        assert result["value"] == 0

    def test_default_chart_type_is_bar(self):
        es_resp = {"aggregations": {"x_buckets": {"buckets": []}}}
        config = {"x_field": "x", "y_fields": ["y"], "aggregation": "sum"}
        result = transform(es_resp, config)
        assert "xAxis" in result

    def test_legacy_y_field_string_normalized(self):
        es_resp = {"aggregations": {"metric_clicks": {"value": 42}}}
        config = {"chart_type": "metric", "y_field": "clicks", "aggregation": "sum"}
        result = transform(es_resp, config)
        assert result["value"] == 42
