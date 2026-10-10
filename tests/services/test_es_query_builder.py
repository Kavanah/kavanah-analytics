"""
Tests for the ES Query Builder service.

Covers:
  _safe_agg_key       — dot/hyphen replacement
  _terms_field         — text → .keyword promotion
  _build_filters       — every OPERATOR_MAP entry + date_range
  _metric_agg          — every aggregation type + numeric-on-text fallback
  build_query          — metric, table, line, bar, pie chart types
"""

import pytest

from asset_management.web.services.es_query_builder import (
    METRIC_AGG_MAP,
    OPERATOR_MAP,
    _build_filters,
    _metric_agg,
    _safe_agg_key,
    _terms_field,
    build_query,
)

# every chart query excludes the retired "no data" marker
# docs; _build_filters appends this clause unconditionally, so exact-shape
# assertions below expect it as the trailing filter.
NO_MARKERS = {"bool": {"must_not": {"term": {"no_data": True}}}}


# ---------------------------------------------------------------------------
# _safe_agg_key
# ---------------------------------------------------------------------------

class TestSafeAggKey:

    def test_replaces_dots(self):
        assert _safe_agg_key("stats.impressions") == "stats__impressions"

    def test_replaces_hyphens(self):
        assert _safe_agg_key("ad-group") == "ad_group"

    def test_replaces_both(self):
        assert _safe_agg_key("team.fb-ads.clicks") == "team__fb_ads__clicks"

    def test_plain_name_unchanged(self):
        assert _safe_agg_key("impressions") == "impressions"


# ---------------------------------------------------------------------------
# _terms_field
# ---------------------------------------------------------------------------

class TestTermsField:

    def test_text_field_gets_keyword_suffix(self):
        assert _terms_field("team_title", "text") == "team_title.keyword"

    def test_text_field_already_keyword_unchanged(self):
        assert _terms_field("team_title.keyword", "text") == "team_title.keyword"

    def test_keyword_field_unchanged(self):
        assert _terms_field("team_title", "keyword") == "team_title"

    def test_no_type_unchanged(self):
        assert _terms_field("impressions") == "impressions"

    def test_none_type_unchanged(self):
        assert _terms_field("impressions", None) == "impressions"

    def test_date_field_unchanged(self):
        assert _terms_field("date_start", "date") == "date_start"


# ---------------------------------------------------------------------------
# _build_filters — OPERATOR_MAP entries
# ---------------------------------------------------------------------------

class TestBuildFilters:

    def test_eq_operator(self):
        config = {"filters": [{"field": "status", "op": "eq", "value": "active"}]}
        result = _build_filters(config)
        assert result == [{"term": {"status": "active"}}, NO_MARKERS]

    def test_neq_operator(self):
        config = {"filters": [{"field": "status", "op": "neq", "value": "deleted"}]}
        result = _build_filters(config)
        assert result == [{"bool": {"must_not": {"term": {"status": "deleted"}}}}, NO_MARKERS]

    def test_gt_operator(self):
        config = {"filters": [{"field": "clicks", "op": "gt", "value": 100}]}
        result = _build_filters(config)
        assert result == [{"range": {"clicks": {"gt": 100}}}, NO_MARKERS]

    def test_gte_operator(self):
        config = {"filters": [{"field": "clicks", "op": "gte", "value": 50}]}
        result = _build_filters(config)
        assert result == [{"range": {"clicks": {"gte": 50}}}, NO_MARKERS]

    def test_lt_operator(self):
        config = {"filters": [{"field": "cost", "op": "lt", "value": 10.5}]}
        result = _build_filters(config)
        assert result == [{"range": {"cost": {"lt": 10.5}}}, NO_MARKERS]

    def test_lte_operator(self):
        config = {"filters": [{"field": "cost", "op": "lte", "value": 20}]}
        result = _build_filters(config)
        assert result == [{"range": {"cost": {"lte": 20}}}, NO_MARKERS]

    def test_between_operator(self):
        config = {"filters": [{"field": "cost", "op": "between", "value": [5, 50]}]}
        result = _build_filters(config)
        assert result == [{"range": {"cost": {"gte": 5, "lte": 50}}}, NO_MARKERS]

    def test_in_operator(self):
        config = {"filters": [{"field": "status", "op": "in", "value": ["a", "b"]}]}
        result = _build_filters(config)
        assert result == [{"terms": {"status": ["a", "b"]}}, NO_MARKERS]

    def test_match_phrase_operator(self):
        config = {"filters": [{"field": "name", "op": "match_phrase", "value": "hello world"}]}
        result = _build_filters(config)
        assert result == [{"match_phrase": {"name": "hello world"}}, NO_MARKERS]

    def test_default_op_is_eq(self):
        config = {"filters": [{"field": "status", "value": "ok"}]}
        result = _build_filters(config)
        assert result == [{"term": {"status": "ok"}}, NO_MARKERS]

    def test_unknown_operator_skipped(self):
        config = {"filters": [{"field": "x", "op": "unknown", "value": 1}]}
        result = _build_filters(config)
        assert result == [NO_MARKERS]

    def test_multiple_filters(self):
        config = {"filters": [
            {"field": "status", "op": "eq", "value": "active"},
            {"field": "clicks", "op": "gt", "value": 10},
        ]}
        result = _build_filters(config)
        assert len(result) == 3

    def test_empty_filters(self):
        assert _build_filters({}) == [NO_MARKERS]
        assert _build_filters({"filters": []}) == [NO_MARKERS]


# ---------------------------------------------------------------------------
# .keyword fallback — dashboard filters target ``team_esid.keyword`` /
# ``organization_title.keyword`` etc., but the importer schemas on IG, FB
# Page, GA, and Chatwoot map those fields as bare ``keyword`` with no
# sub-field. The query layer expands ``.keyword``-suffixed filter fields
# to a ``bool/should`` over both shapes so the query resolves regardless
# of which mapping the index actually has.
# ---------------------------------------------------------------------------


class TestKeywordFallback:

    def test_eq_with_keyword_suffix_expands_to_bool_should(self):
        config = {
            "filters": [
                {"field": "team_esid.keyword", "op": "eq", "value": "TaZXW54Br1aoHc3Me_09"},
            ]
        }
        result = _build_filters(config)
        assert result == [
            {
                "bool": {
                    "should": [
                        {"term": {"team_esid.keyword": "TaZXW54Br1aoHc3Me_09"}},
                        {"term": {"team_esid": "TaZXW54Br1aoHc3Me_09"}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            NO_MARKERS,
        ]

    def test_eq_without_keyword_suffix_is_unchanged(self):
        """Bare fields are trusted as-is — no expansion. Backwards-compat."""
        config = {"filters": [{"field": "team_esid", "op": "eq", "value": "abc"}]}
        result = _build_filters(config)
        assert result == [{"term": {"team_esid": "abc"}}, NO_MARKERS]

    def test_in_with_keyword_suffix_expands_to_bool_should(self):
        config = {
            "filters": [
                {"field": "team_title.keyword", "op": "in", "value": ["Alpha", "Beta"]}
            ]
        }
        result = _build_filters(config)
        assert result == [
            {
                "bool": {
                    "should": [
                        {"terms": {"team_title.keyword": ["Alpha", "Beta"]}},
                        {"terms": {"team_title": ["Alpha", "Beta"]}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            NO_MARKERS,
        ]

    def test_neq_with_keyword_suffix_wraps_must_not_over_variants(self):
        """Negation must reject docs matching EITHER variant."""
        config = {"filters": [{"field": "status.keyword", "op": "neq", "value": "deleted"}]}
        result = _build_filters(config)
        assert result == [
            {
                "bool": {
                    "must_not": {
                        "bool": {
                            "should": [
                                {"term": {"status.keyword": "deleted"}},
                                {"term": {"status": "deleted"}},
                            ],
                            "minimum_should_match": 1,
                        }
                    }
                }
            },
            NO_MARKERS,
        ]

    def test_match_phrase_with_keyword_suffix_expands(self):
        config = {
            "filters": [
                {"field": "team_title.keyword", "op": "match_phrase", "value": "Kavanah Media"}
            ]
        }
        result = _build_filters(config)
        assert result == [
            {
                "bool": {
                    "should": [
                        {"match_phrase": {"team_title.keyword": "Kavanah Media"}},
                        {"match_phrase": {"team_title": "Kavanah Media"}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            NO_MARKERS,
        ]

    def test_dashboard_filter_keyword_suffix_expands(self):
        """The topbar filter-dropdown path — dashboard_filters with .keyword."""
        result = _build_filters(
            {},
            dashboard_filters=[
                {
                    "field": "organization_title.keyword",
                    "data_view": "*_ig_insights",
                    "selected_values": ["Kavanah", "Demo"],
                }
            ],
            active_data_view="*_ig_insights",
        )
        assert result == [
            {
                "bool": {
                    "should": [
                        {"terms": {"organization_title.keyword": ["Kavanah", "Demo"]}},
                        {"terms": {"organization_title": ["Kavanah", "Demo"]}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            NO_MARKERS,
        ]

    def test_dashboard_filter_without_keyword_suffix_unchanged(self):
        """Bare-field dashboard filter — no expansion."""
        result = _build_filters(
            {},
            dashboard_filters=[
                {
                    "field": "team_title",
                    "data_view": "*_ig_insights",
                    "selected_values": ["Alpha"],
                }
            ],
            active_data_view="*_ig_insights",
        )
        assert result == [{"terms": {"team_title": ["Alpha"]}}, NO_MARKERS]

    def test_range_ops_with_keyword_suffix_dont_expand(self):
        """Range ops are typically on numeric/date fields. Front-end shouldn't
        generate .keyword-suffixed range filters; if it does, leave the
        clause shape unchanged (range on a non-existent field returns 0
        hits silently, same as term)."""
        config = {"filters": [{"field": "score.keyword", "op": "gt", "value": 100}]}
        result = _build_filters(config)
        assert result == [{"range": {"score.keyword": {"gt": 100}}}, NO_MARKERS]


# ---------------------------------------------------------------------------
# _build_filters — date_range
# ---------------------------------------------------------------------------

class TestBuildFiltersDateRange:

    def test_date_range_from_and_to(self):
        config = {"date_range": {"field": "date_start", "from": "2024-01-01", "to": "2024-12-31"}}
        result = _build_filters(config)
        # ISO bounds carry an explicit format so ES doesn't try to
        # parse them using the field's stored format (echo's kibana_date).
        assert result == [
            {"range": {"date_start": {"gte": "2024-01-01", "lte": "2024-12-31", "format": "yyyy-MM-dd"}}},
            NO_MARKERS,
        ]

    def test_date_range_from_only(self):
        config = {"date_range": {"field": "date_start", "from": "2024-01-01"}}
        result = _build_filters(config)
        assert result == [{"range": {"date_start": {"gte": "2024-01-01", "format": "yyyy-MM-dd"}}}, NO_MARKERS]

    def test_date_range_to_only(self):
        config = {"date_range": {"field": "date_start", "to": "2024-12-31"}}
        result = _build_filters(config)
        assert result == [{"range": {"date_start": {"lte": "2024-12-31", "format": "yyyy-MM-dd"}}}, NO_MARKERS]

    def test_date_range_no_field_ignored(self):
        config = {"date_range": {"from": "2024-01-01", "to": "2024-12-31"}}
        result = _build_filters(config)
        assert result == [NO_MARKERS]

    def test_date_range_no_from_no_to_ignored(self):
        config = {"date_range": {"field": "date_start"}}
        result = _build_filters(config)
        assert result == [NO_MARKERS]

    def test_filters_combined_with_date_range(self):
        config = {
            "filters": [{"field": "status", "op": "eq", "value": "active"}],
            "date_range": {"field": "date_start", "from": "2024-01-01", "to": "2024-12-31"},
        }
        result = _build_filters(config)
        assert len(result) == 3
        assert result[0] == {"term": {"status": "active"}}
        assert "range" in result[1]


# ---------------------------------------------------------------------------
# _metric_agg
# ---------------------------------------------------------------------------

class TestMetricAgg:

    @pytest.mark.parametrize("agg_name,es_type", [
        ("sum", "sum"),
        ("avg", "avg"),
        ("min", "min"),
        ("max", "max"),
        ("count", "value_count"),
    ])
    def test_standard_aggregation_types(self, agg_name, es_type):
        result = _metric_agg("impressions", agg_name)
        assert result == {es_type: {"field": "impressions"}}

    def test_cardinality_carries_precision_threshold(self):
        # the unique-contacts metric must match Echo's Clients KPI
        # exactly, so cardinality is pinned to the 40,000 precision ceiling
        # (default 3,000 is only approximate) rather than the bare agg shape.
        result = _metric_agg("client_id", "cardinality")
        assert result == {"cardinality": {"field": "client_id", "precision_threshold": 40000}}

    def test_text_aggregation_uses_top_hits(self):
        result = _metric_agg("team_title", "text")
        assert "top_hits" in result
        assert result["top_hits"]["size"] == 1
        assert result["top_hits"]["_source"]["includes"] == ["team_title"]

    def test_text_aggregation_strips_keyword_suffix(self):
        result = _metric_agg("team_title.keyword", "text")
        assert result["top_hits"]["_source"]["includes"] == ["team_title"]

    def test_date_aggregation_uses_top_hits(self):
        result = _metric_agg("date_start", "date")
        assert "top_hits" in result

    def test_equals_aggregation_with_value(self):
        result = _metric_agg("status", "equals", equals_value="active")
        assert result == {"filter": {"term": {"status": "active"}}}

    def test_exists_aggregation(self):
        result = _metric_agg("email", "exists")
        assert result == {"filter": {"exists": {"field": "email"}}}

    def test_query_date_with_range(self):
        dr = {"field": "date_start", "from": "2024-01-01", "to": "2024-12-31"}
        result = _metric_agg("date_start", "query date", date_range=dr)
        # ISO bounds carry an explicit format on the range clause.
        assert result == {
            "filter": {
                "range": {"date_start": {"gte": "2024-01-01", "lte": "2024-12-31", "format": "yyyy-MM-dd"}}
            }
        }

    def test_query_date_uses_date_range_field_over_y_field(self):
        dr = {"field": "date_start.keyword", "from": "2024-01-01"}
        result = _metric_agg("date_start", "query date", date_range=dr)
        assert "date_start.keyword" in result["filter"]["range"]

    def test_query_date_no_range_falls_back_to_exists(self):
        result = _metric_agg("date_start", "query date")
        assert result == {"filter": {"exists": {"field": "date_start"}}}

    def test_query_date_empty_range_falls_back_to_exists(self):
        result = _metric_agg("date_start", "query date", date_range={"field": "date_start"})
        assert result == {"filter": {"exists": {"field": "date_start"}}}

    def test_numeric_agg_on_text_field_falls_back_to_count(self):
        result = _metric_agg("team_title", "sum", field_type="text")
        assert result == {"value_count": {"field": "team_title"}}

    def test_numeric_agg_on_keyword_field_falls_back_to_count(self):
        result = _metric_agg("status", "avg", field_type="keyword")
        assert result == {"value_count": {"field": "status"}}

    def test_numeric_agg_on_date_field_falls_back_to_count(self):
        result = _metric_agg("date_start", "max", field_type="date")
        assert result == {"value_count": {"field": "date_start"}}

    def test_count_on_text_field_not_double_converted(self):
        result = _metric_agg("team_title", "count", field_type="text")
        assert result == {"value_count": {"field": "team_title"}}

    def test_unknown_aggregation_defaults_to_sum(self):
        result = _metric_agg("impressions", "unknown_agg")
        assert result == {"sum": {"field": "impressions"}}


# ---------------------------------------------------------------------------
# build_query — metric chart
# ---------------------------------------------------------------------------

class TestBuildQueryMetric:

    def test_metric_with_y_field(self):
        config = {"chart_type": "metric", "y_fields": ["impressions"], "aggregation": "sum"}
        result = build_query(config)
        assert result["size"] == 0
        assert "metric_impressions" in result["aggs"]
        assert result["query"] == {"bool": {"filter": [NO_MARKERS]}}

    def test_metric_with_breakdown(self):
        config = {"chart_type": "metric", "y_fields": ["impressions"], "breakdown": "campaign_name", "aggregation": "sum"}
        result = build_query(config)
        assert "breakdown_buckets" in result["aggs"]
        assert result["aggs"]["breakdown_buckets"]["terms"]["field"] == "campaign_name"

    def test_metric_no_y_field(self):
        config = {"chart_type": "metric", "aggregation": "count"}
        result = build_query(config)
        assert result["aggs"] == {}

    def test_metric_with_filters(self):
        config = {
            "chart_type": "metric",
            "y_fields": ["clicks"],
            "aggregation": "sum",
            "filters": [{"field": "status", "op": "eq", "value": "active"}],
        }
        result = build_query(config)
        assert result["query"]["bool"]["filter"] == [{"term": {"status": "active"}}, NO_MARKERS]

    def test_metric_legacy_y_field_string(self):
        config = {"chart_type": "metric", "y_field": "impressions", "aggregation": "sum"}
        result = build_query(config)
        assert "metric_impressions" in result["aggs"]

    def test_metric_with_equals_value(self):
        config = {
            "chart_type": "metric",
            "y_fields": ["status"],
            "aggregation": "equals",
            "equals_value": "active",
        }
        result = build_query(config)
        agg = result["aggs"]["metric_status"]
        assert agg == {"filter": {"term": {"status": "active"}}}

    def test_metric_dotted_field_name(self):
        config = {"chart_type": "metric", "y_fields": ["stats.impressions"], "aggregation": "sum"}
        result = build_query(config)
        assert "metric_stats__impressions" in result["aggs"]


# ---------------------------------------------------------------------------
# build_query — table chart
# ---------------------------------------------------------------------------

class TestBuildQueryTable:

    def test_table_with_x_field(self):
        config = {
            "chart_type": "table",
            "x_field": "campaign_name",
            "y_fields": ["impressions", "clicks"],
            "aggregation": "sum",
        }
        result = build_query(config)
        assert result["size"] == 0
        composite = result["aggs"]["table_buckets"]["composite"]
        assert composite["sources"] == [{"campaign_name": {"terms": {"field": "campaign_name"}}}]
        inner = result["aggs"]["table_buckets"]["aggs"]
        assert "metric_impressions" in inner
        assert "metric_clicks" in inner

    def test_table_with_x_field_2(self):
        config = {
            "chart_type": "table",
            "x_field": "campaign",
            "x_field_2": "ad_group",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = build_query(config)
        sources = result["aggs"]["table_buckets"]["composite"]["sources"]
        assert len(sources) == 2
        # the sub-row source keeps docs that lack the field (they
        # group under the "(none)" bucket) instead of silently dropping rows.
        assert sources[1] == {"ad_group": {"terms": {"field": "ad_group", "missing_bucket": True}}}

    def test_table_no_x_field_auto_promotes_text_column(self):
        config = {
            "chart_type": "table",
            "y_fields": ["impressions"],
            "aggregation": "sum",
            "table_columns": [
                {"field": "team_title", "aggregation": "text"},
                {"field": "impressions", "aggregation": "sum"},
            ],
        }
        result = build_query(config)
        sources = result["aggs"]["table_buckets"]["composite"]["sources"]
        # text column auto-promoted → keyword
        assert "team_title" in str(sources[0])
        # auto-promoted column removed from metric_aggs
        inner = result["aggs"]["table_buckets"]["aggs"]
        assert "metric_team_title" not in inner
        assert "metric_impressions" in inner

    def test_table_no_x_field_no_text_column_returns_match_none(self):
        config = {
            "chart_type": "table",
            "y_fields": ["impressions"],
            "aggregation": "sum",
            "table_columns": [
                {"field": "impressions", "aggregation": "sum"},
            ],
        }
        result = build_query(config)
        assert result == {"size": 0, "query": {"match_none": {}}}

    def test_table_with_table_columns(self):
        config = {
            "chart_type": "table",
            "x_field": "team_title",
            "table_columns": [
                {"field": "impressions", "aggregation": "sum"},
                {"field": "clicks", "aggregation": "avg"},
            ],
            "aggregation": "sum",
        }
        result = build_query(config)
        inner = result["aggs"]["table_buckets"]["aggs"]
        assert "metric_impressions" in inner
        assert "metric_clicks" in inner

    def test_table_x_field_text_type_gets_keyword(self):
        config = {
            "chart_type": "table",
            "x_field": "team_title",
            "x_field_type": "text",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = build_query(config)
        sources = result["aggs"]["table_buckets"]["composite"]["sources"]
        assert sources[0] == {"team_title.keyword": {"terms": {"field": "team_title.keyword"}}}

    # Row field date-format support. When x_field_type=date, the
    # composite source uses date_histogram with calendar_interval=day and
    # the chart's date_format (default yyyy-MM-dd) so the row labels are
    # calendar buckets rather than a raw terms agg over every distinct
    # epoch timestamp value.

    def test_table_x_field_date_type_uses_date_histogram(self):
        config = {
            "chart_type": "table",
            "x_field": "kibana_date",
            "x_field_type": "date",
            "y_fields": ["ig_insight_reach"],
            "aggregation": "sum",
        }
        result = build_query(config)
        sources = result["aggs"]["table_buckets"]["composite"]["sources"]
        assert sources[0] == {
            "kibana_date": {
                "date_histogram": {
                    "field": "kibana_date",
                    "calendar_interval": "day",
                    "format": "yyyy-MM-dd",
                }
            }
        }

    def test_table_x_field_date_honors_date_format(self):
        config = {
            "chart_type": "table",
            "x_field": "kibana_date",
            "x_field_type": "date",
            "date_format": "MMM dd, yyyy",
            "y_fields": ["ig_insight_reach"],
            "aggregation": "sum",
        }
        result = build_query(config)
        sources = result["aggs"]["table_buckets"]["composite"]["sources"]
        assert sources[0]["kibana_date"]["date_histogram"]["format"] == "MMM dd, yyyy"

    def test_table_x_field_date_honors_calendar_interval(self):
        config = {
            "chart_type": "table",
            "x_field": "kibana_date",
            "x_field_type": "date",
            "calendar_interval": "week",
            "y_fields": ["ig_insight_reach"],
            "aggregation": "sum",
        }
        result = build_query(config)
        sources = result["aggs"]["table_buckets"]["composite"]["sources"]
        assert sources[0]["kibana_date"]["date_histogram"]["calendar_interval"] == "week"

    def test_table_x_field_keyword_type_still_uses_terms(self):
        """Negative pin — only x_field_type=date switches to date_histogram.
        keyword-typed Row fields keep the existing terms agg behavior."""
        config = {
            "chart_type": "table",
            "x_field": "team_title.keyword",
            "x_field_type": "keyword",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = build_query(config)
        sources = result["aggs"]["table_buckets"]["composite"]["sources"]
        assert "terms" in sources[0]["team_title.keyword"]
        assert "date_histogram" not in sources[0]["team_title.keyword"]


# ---------------------------------------------------------------------------
# build_query — line chart
# ---------------------------------------------------------------------------

class TestBuildQueryLine:

    def test_line_date_x_axis_uses_date_histogram(self):
        config = {
            "chart_type": "line",
            "x_field": "date_start",
            "x_field_type": "date",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = build_query(config)
        assert "date_histogram" in result["aggs"]["x_buckets"]
        assert result["aggs"]["x_buckets"]["date_histogram"]["field"] == "date_start"

    def test_line_date_keyword_heuristic(self):
        config = {
            "chart_type": "line",
            "x_field": "date_month",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = build_query(config)
        assert "date_histogram" in result["aggs"]["x_buckets"]

    def test_line_keyword_x_axis_uses_terms(self):
        config = {
            "chart_type": "line",
            "x_field": "campaign_name",
            "x_field_type": "keyword",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = build_query(config)
        assert "terms" in result["aggs"]["x_buckets"]

    def test_line_multiple_y_fields(self):
        config = {
            "chart_type": "line",
            "x_field": "date_start",
            "x_field_type": "date",
            "y_fields": ["impressions", "clicks"],
            "aggregation": "sum",
        }
        result = build_query(config)
        inner = result["aggs"]["x_buckets"]["aggs"]
        assert "metric_impressions" in inner
        assert "metric_clicks" in inner


# ---------------------------------------------------------------------------
# build_query — bar / pie
# ---------------------------------------------------------------------------

class TestBuildQueryBarPie:

    def test_bar_uses_terms(self):
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = build_query(config)
        assert "terms" in result["aggs"]["x_buckets"]
        assert result["aggs"]["x_buckets"]["terms"]["field"] == "campaign"

    def test_pie_uses_terms(self):
        config = {
            "chart_type": "pie",
            "x_field": "platform",
            "y_fields": ["impressions"],
            "aggregation": "sum",
        }
        result = build_query(config)
        assert "terms" in result["aggs"]["x_buckets"]

    def test_bar_with_breakdown(self):
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_fields": ["impressions"],
            "aggregation": "sum",
            "breakdown": "platform",
        }
        result = build_query(config)
        inner = result["aggs"]["x_buckets"]["aggs"]
        assert "breakdown_buckets" in inner
        assert inner["breakdown_buckets"]["terms"]["field"] == "platform"
        # Breakdown uses primary y_field for metric
        assert "metric_impressions" in inner["breakdown_buckets"]["aggs"]

    def test_bar_terms_size_configurable(self):
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_fields": ["impressions"],
            "aggregation": "sum",
            "terms_size": 50,
        }
        result = build_query(config)
        assert result["aggs"]["x_buckets"]["terms"]["size"] == 50

    def test_default_chart_type_is_bar(self):
        config = {"x_field": "campaign", "y_fields": ["impressions"], "aggregation": "sum"}
        result = build_query(config)
        assert "terms" in result["aggs"]["x_buckets"]

    def test_bar_multiple_y_fields(self):
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_fields": ["impressions", "clicks", "cost"],
            "aggregation": "sum",
        }
        result = build_query(config)
        inner = result["aggs"]["x_buckets"]["aggs"]
        assert "metric_impressions" in inner
        assert "metric_clicks" in inner
        assert "metric_cost" in inner


# ---------------------------------------------------------------------------
# build_query — edge cases
# ---------------------------------------------------------------------------

class TestBuildQueryEdgeCases:

    def test_y_field_types_forwarded_to_metric_agg(self):
        config = {
            "chart_type": "metric",
            "y_fields": ["team_title"],
            "aggregation": "sum",
            "y_field_types": {"team_title": "text"},
        }
        result = build_query(config)
        # sum on text → falls back to value_count
        assert result["aggs"]["metric_team_title"] == {"value_count": {"field": "team_title"}}

    def test_empty_config_returns_valid_query(self):
        result = build_query({})
        assert result["size"] == 0
        assert "aggs" in result

    def test_no_x_field_bar(self):
        config = {"chart_type": "bar", "y_fields": ["impressions"], "aggregation": "sum"}
        result = build_query(config)
        # x_field is None — terms agg field will be None but query is structurally valid
        assert "x_buckets" in result["aggs"]

    def test_legacy_y_field_normalized_to_y_fields(self):
        config = {
            "chart_type": "bar",
            "x_field": "campaign",
            "y_field": "impressions",
            "aggregation": "sum",
        }
        result = build_query(config)
        assert "metric_impressions" in result["aggs"]["x_buckets"]["aggs"]
