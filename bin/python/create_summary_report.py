#!/usr/bin/env python3
"""
Create a 'Summary Report' grouped_table dashboard in the ECharts Dashboard Builder.

This saves the dashboard to the de_asset_echarts_dashboards Elasticsearch index,
making it available in the Dashboard Builder UI.

The fields match the existing Kibana "Overview Summary" dashboard exactly.

Run ON THE SERVER inside imports_container:
    sudo docker exec imports_container python3 /app/python/create_summary_report.py
"""

import json
import uuid
import sys
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────
# Elasticsearch connection
# ─────────────────────────────────────────────────────────
try:
    sys.path.insert(0, "/app/python")
    from lib.es_helpers import get_elasticsearch_client
except ImportError:
    # Fallback for local dev — tries env-based connection
    sys.path.insert(0, "bin/python")
    from lib.es_helpers import get_elasticsearch_client

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"

# prod maps `title` as a BARE keyword (no.keyword subfield), so a
# title.keyword-only term matched nothing and the update-not-duplicate check
# silently degraded to create-a-duplicate. Match either spelling (the
# recurring.keyword trap).
EXISTING_REPORT_QUERY = {
    "bool": {
        "should": [
            {"term": {"title": "Summary Report"}},
            {"term": {"title.keyword": "Summary Report"}},
        ],
        "minimum_should_match": 1,
    }
}

# ─────────────────────────────────────────────────────────
# Data view names (ES index patterns used by the builder)
# ─────────────────────────────────────────────────────────
# every pattern must survive the Layer-1 endswith
# intersection against real index names — the old '*fb_page' / '*youtube' /
# '*ga' shortcuts matched no index (indices end in _fb_page_insights /
# _youtube_analytics / _ga_metrics), rewrote to the empty-scope sentinel, and
# the bound columns read 0. Keep in lockstep with canvas_seed.py
# _CANVA_SEED_METRICS (the canonical patterns).
DV = {
    "fb_insights": "*fb_insights",
    "fb_page": "*_fb_page_insights",
    "echo": "*echo_conversations",
    # team-keyed pattern (the cross-tenant alias is retired).
    # DT field paths below follow the pull schema
    # (unified_schema_disciple_tools.json): top-level fields (post_type) are
    # BARE keyword — no .keyword subfield exists; meta.* fields are
    # text+keyword, so their .keyword paths are correct. Re-seeding prod is
    # safe again now that the code matches the hand-corrected prod config.
    "dt": "*_disciple_tools",
    "youtube": "*_youtube_analytics",
    "ga": "*_ga_metrics",
    "google_ads": "*google_ads",
    "ig_insights": "*ig_insights",
    # monthly per-team rollups of conversations older than 24
    # months (retention_sweep.py). A second source on the additive Echo
    # columns so the all-time period keeps its history after the raw docs go.
    "rollups": "*_conversation_rollups",
}


def col(label, data_view, field, aggregation="sum", extra_sources=None, **kwargs):
    """Build a grouped_table column definition.

    ``extra_sources``: additional ``{"data_view", "field"}`` dicts
    for a multi-source column — the renderer queries each source and combines
    them under the one ``aggregation``.
    """
    entry = {
        "label": label,
        "data_sources": [{"data_view": data_view, "field": field}, *(extra_sources or [])],
        "aggregation": aggregation,
    }
    entry.update(kwargs)
    return entry


def count_col(label, data_view, field, equals_value, **kwargs):
    """Build a column that counts docs where field == value."""
    return col(label, data_view, field, aggregation="equals", equals_value=equals_value, **kwargs)


def exists_col(label, data_view, field, **kwargs):
    """Build a column that counts docs where field exists."""
    return col(label, data_view, field, aggregation="exists", **kwargs)


# ─────────────────────────────────────────────────────────
# Dashboard definition
# ─────────────────────────────────────────────────────────

dashboard_id = str(uuid.uuid4())
now = datetime.now(tz=timezone.utc).isoformat()

chart_config = {
    "chart_id": "c_summary_report",
    "title": "Summary Report",
    "position": {"col": 0, "row": 0, "w": 12, "h": 8},
    "chart_type": "grouped_table",
    "width": None,
    "height": None,
    "table_title": "Summary Report",
    "title_bg_color": "#1565C0",
    "group_header_bg_color": "#5a9cc5",
    "col_header_bg_color": "#2c6da0",
    "header_text_color": "#ffffff",
    "group_text_color": "#ffffff",
    "group_font_size": "13",
    "group_bold": True,
    "col_text_color": "#ffffff",
    "col_font_size": "12",
    "col_bold": True,
    "groups": [
        # ── Section 1: How many people saw our content? (Blue) ──
        {
            "label": "How many people saw our content?",
            "columns": [
                col("Meta Reach", DV["fb_insights"], "insight_reach"),
                col("Google Reach", DV["google_ads"], "impressions"),
                # page_impressions_unique → page_total_media_view_unique
                # (Meta v23 deprecation, see get_page_insights.py:413).
                col("Facebook Reach", DV["fb_page"], "page_total_media_view_unique"),
                col("Instagram Reach", DV["ig_insights"], "reach"),
                col("YouTube Views", DV["youtube"], "views"),
                col("Web Users", DV["ga"], "totalUsers"),
            ],
        },
        # ── Section 2: How did they engage? (Green) ──
        {
            "label": "How did they engage with our content?",
            "columns": [
                # lifetime-to-date snapshot over recent videos (no
                # daily page-level 15s metric exists in the Graph API): max
                # reads the latest snapshot; the old sum multiplied lifetime
                # values across days. Will NOT reconcile with Business Suite's
                # internal daily figure — that number is not API-exposed.
                col(
                    "15s Video Views (lifetime, recent videos)", DV["fb_page"], "page_video_views_15s_total", aggregation="max"
                ),
                # (decision 2026-08-20): View Time is a combined
                # cross-platform total in minutes. adds the second
                # source: page_video_view_time_daily_min is the importer's
                # lifetime-DELTA daily total (already minutes; safely summable,
                # unlike page_video_avg_sec_watched, which is a lifetime
                # AVERAGE and must never be summed into this column).
                col(
                    "View Time (Minutes)",
                    DV["youtube"],
                    "estimated_minutes_watched",
                    extra_sources=[{"data_view": DV["fb_page"], "field": "page_video_view_time_daily_min"}],
                ),
                col("Facebook Follows", DV["fb_page"], "page_follows", aggregation="max"),
                col("Instagram Follows", DV["ig_insights"], "follows"),
                col("YouTube Subscribers", DV["youtube"], "subscribers_gained"),
                col("App Installs", DV["google_ads"], "app_installs"),
                col("Facebook Reactions", DV["fb_page"], "page_actions_post_reactions_total"),
                # page_post_engagements is ALL engagements and
                # over-reports; page_posts_shares_total is the shares count
                # every other surface reports (same swap made in the
                # canva registries).
                col("Facebook Shares", DV["fb_page"], "page_posts_shares_total"),
                col("Instagram Reactions", DV["ig_insights"], "likes"),
                col("Instagram Shares", DV["ig_insights"], "shares"),
                col("YouTube Reactions", DV["youtube"], "likes"),
                col("YouTube Shares", DV["youtube"], "shares"),
            ],
        },
        # ── Section 3: How did they interact? (Orange) ──
        {
            "label": "How did they interact with us?",
            "columns": [
                # cardinality is not additive across sources, so this
                # column stays raw-only and counts distinct clients over the
                # last 24 months at most (documented loss; DECISIONS 2026-09-10).
                col("Messaging Contacts", DV["echo"], "client_id", aggregation="cardinality"),
                col(
                    "Messages Received",
                    DV["echo"],
                    "incomingMessagesCount",
                    extra_sources=[{"data_view": DV["rollups"], "field": "incomingMessagesCount"}],
                ),
                col(
                    "Messages Sent",
                    DV["echo"],
                    "outboundMessagesCount",
                    extra_sources=[{"data_view": DV["rollups"], "field": "outboundMessagesCount"}],
                ),
                # integer flag (0/1) — "true" made ES reject the query.
                # sum of the 0/1 flag (== the count of 1s) instead of
                # equals "1", so the monthly rollup — which stores the month's
                # sum in the same field — can be the column's second source.
                col(
                    "Spiritual Conversations",
                    DV["echo"],
                    "spiritual_conversation",
                    extra_sources=[{"data_view": DV["rollups"], "field": "spiritual_conversation"}],
                ),
                # the Page Insights comment metric is retired at Meta; count ad comments.
                col("Comments (ads)", DV["fb_insights"], "insight_actions_comment"),
            ],
        },
        # ── Section 4: Jesus journey (Purple) ──
        # post_type is a BARE keyword on the pull schema — querying
        # post_type.keyword silently returned 0 on team-keyed indices. The
        # meta.* paths keep .keyword (text+keyword on the pull schema AND on
        # the dynamic-mapped demo index).
        {
            "label": "Where are they on their Jesus journey?",
            "columns": [
                count_col("New Contacts", DV["dt"], "post_type", "contacts"),
                count_col("1st Meetings", DV["dt"], "meta.seeker_path.keyword", "met"),
                count_col("Baptisms", DV["dt"], "meta.milestones.keyword", "milestone_baptized"),
                count_col("New Groups", DV["dt"], "post_type", "groups"),
                exists_col("New Churches", DV["dt"], "meta.church_start_date"),
            ],
        },
    ],
}

es_doc = {
    "dashboard_id": dashboard_id,
    "dashboard_type": "echarts",
    "title": "Summary Report",
    "menu": "Reports",
    "created_at": now,
    "updated_at": now,
    "visibility_mode": "show_all",
    "allowed_org_esids": [],
    "allowed_team_esids": [],
    "echarts_config": {
        "global_date_range": None,
        "charts": [chart_config],
    },
}


def main():
    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: Could not connect to Elasticsearch")
        sys.exit(1)

    # Check for existing "Summary Report" and update instead of duplicating
    try:
        result = es.search(
            index=DASHBOARDS_INDEX,
            body={
                "size": 1,
                "query": EXISTING_REPORT_QUERY,
            },
        )
        hits = result.get("hits", result.body.get("hits", {})) if hasattr(result, "body") else result.get("hits", {})
        if isinstance(hits, dict):
            hit_list = hits.get("hits", [])
        else:
            hit_list = []

        if hit_list:
            existing_id = hit_list[0]["_id"]
            print(f"Found existing 'Summary Report' (id={existing_id}), updating...")
            es_doc["dashboard_id"] = existing_id
            es_doc["created_at"] = hit_list[0]["_source"].get("created_at", now)
            es.index(index=DASHBOARDS_INDEX, id=existing_id, document=es_doc, refresh="wait_for")
            print(f"Updated dashboard: {existing_id}")
            return
    except Exception as e:
        print(f"Warning checking for existing dashboard: {e}")

    # Create new
    es.index(index=DASHBOARDS_INDEX, id=dashboard_id, document=es_doc, refresh="wait_for")
    print(f"Created 'Summary Report' dashboard: {dashboard_id}")
    print(f"Groups: {len(chart_config['groups'])}")
    total_cols = sum(len(g["columns"]) for g in chart_config["groups"])
    print(f"Total columns: {total_cols}")
    print("\nOpen Dashboard Builder in the asset manager to view it.")


if __name__ == "__main__":
    main()
