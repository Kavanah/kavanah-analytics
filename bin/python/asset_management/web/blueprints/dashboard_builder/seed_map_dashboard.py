"""
Seed script: create the "Geographic Map" ECharts dashboard in de_asset_echarts_dashboards.

Idempotent — skips creation if a dashboard with title "Geographic Map" already exists.

Run on the server:
    sudo docker exec imports_container python3 \
      /app/python/asset_management/web/blueprints/dashboard_builder/seed_map_dashboard.py
"""

import sys
import os
import uuid
from datetime import datetime, timezone

# Allow running from anywhere inside the container
sys.path.insert(0, "/app/python")

try:
    from lib.es_helpers import get_elasticsearch_client
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    from lib.es_helpers import get_elasticsearch_client

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"
DASHBOARD_TITLE = "Maps"

MAP_STATISTICS = [
    {
        "key": "app_installs",
        "label": "App installs",
        "data_view": "*fb_insights",
        "field": "insight_actions_mobile_app_install",
        "aggregation": "sum",
    },
    {
        "key": "baptisms",
        "label": "Baptisms",
        "data_view": "disciple-tools",
        "field": "meta.baptism_date",
        "aggregation": "count",
    },
    {
        "key": "dt_contacts",
        "label": "New Contacts",
        "data_view": "disciple-tools",
        "field": "",
        "aggregation": "count",
    },
    {
        "key": "echo_multiplier",
        "label": "Echo multiplier",
        "data_view": "*echo_conversations",
        "field": "multiplier",
        "aggregation": "sum",
    },
    {
        "key": "first_meetings",
        "label": "1st Meetings",
        "data_view": "disciple-tools",
        "field": "meta.seeker_path.keyword",
        "aggregation": "count",
    },
    {
        "key": "google_ads_app_installs",
        "label": "Installs",
        "data_view": "*google_ads",
        "field": "app_installs",
        "aggregation": "sum",
    },
    {
        "key": "messages",
        "label": "Messages Received",
        "data_view": "*echo_conversations",
        "field": "incomingMessagesCount",
        "aggregation": "sum",
    },
    {
        "key": "messaging_contacts",
        "label": "Messaging Contacts",
        "data_view": "*echo_conversations",
        "field": "",
        "aggregation": "count",
    },
    {
        "key": "messages_sent",
        "label": "Messages Sent",
        "data_view": "*echo_conversations",
        "field": "outboundMessagesCount",
        "aggregation": "sum",
    },
    {
        "key": "multiplier_contacts",
        "label": "Multiplier contacts",
        "data_view": "disciple-tools",
        "field": "meta.baptism_generation",
        "aggregation": "count",
    },
    {
        "key": "new_churches",
        "label": "New Churches",
        "data_view": "disciple-tools",
        "field": "meta.church_start_date",
        "aggregation": "count",
    },
    {
        "key": "new_groups",
        "label": "New Groups",
        "data_view": "disciple-tools",
        "field": "post_type",  # bare keyword in the unified schema
        "aggregation": "count",
    },
    {
        "key": "page_visits",
        "label": "Page visits",
        "data_view": "*fb_insights",
        "field": "insight_actions_page_engagement",
        "aggregation": "sum",
    },
    {
        "key": "reach",
        "label": "Meta",
        "data_view": "*fb_insights",
        "field": "insight_reach",
        "aggregation": "sum",
    },
    {
        "key": "salvation",
        "label": "Salvation decisions",
        "data_view": "disciple-tools",
        "field": "meta.faith_status",
        "aggregation": "count",
    },
    {
        "key": "spiritual_conversations",
        "label": "Spiritual Conversations",
        "data_view": "disciple-tools",
        "field": "meta.quick_button_contact_established",
        "aggregation": "count",
    },
    {
        "key": "video_views",
        "label": "Video views",
        "data_view": "*fb_insights",
        "field": "insight_actions_video_view_total",
        "aggregation": "sum",
    },
    # Organic reach
    # (2026-05-28): swapped page_impressions_unique →
    # page_total_media_view_unique. Meta started silently 500-ing the
    # deprecated field on v23; the new field carries the same "unique
    # viewers" semantics. See + importer comments at
    # get_page_insights.py:413 for full context.
    {
        "key": "organic_facebook",
        "label": "Organic Facebook",
        "data_view": "*fb_page_insights",
        "field": "page_total_media_view_unique",
        "aggregation": "sum",
    },
    {
        "key": "organic_instagram",
        "label": "Organic Instagram",
        "data_view": "*ig_insights",
        "field": "ig_insight_reach",
        "aggregation": "sum",
    },
    {
        "key": "organic_web",
        "label": "Organic Web",
        "data_view": "*ga_metrics",
        "field": "totalUsers",
        "aggregation": "sum",
    },
    # Video engagement
    {
        "key": "videos_15s",
        "label": "15s Video Views",
        "data_view": "*fb_page_insights",
        "field": "page_video_views_15s_total",
        "aggregation": "sum",
    },
    {
        "key": "view_time",
        "label": "Video View Time",
        "data_view": "*fb_page_insights",
        "field": "page_video_avg_sec_watched",
        "aggregation": "sum",
    },
    # Follows / subscriptions
    {
        "key": "follows_facebook",
        "label": "Facebook Follows",
        "data_view": "*fb_page_insights",
        "field": "page_follows",
        "aggregation": "sum",
    },
    {
        "key": "follows_instagram",
        "label": "Instagram Follows",
        "data_view": "*ig_insights",
        "field": "ig_user_followers_count",
        "aggregation": "sum",
    },
    {
        "key": "follows_youtube",
        "label": "YouTube Subscribers",
        "data_view": "*youtube_analytics",
        "field": "subscribers_gained",
        "aggregation": "sum",
    },
    # Reactions / shares
    {
        "key": "fb_reactions",
        "label": "FB Reactions",
        "data_view": "*fb_page_insights",
        "field": "page_actions_post_reactions_total",
        "aggregation": "sum",
    },
    {
        "key": "fb_shares",
        "label": "FB Shares",
        "data_view": "*fb_page_insights",
        "field": "page_posts_shares_total",
        "aggregation": "sum",
    },
    {
        "key": "ig_reactions",
        "label": "IG Reactions",
        "data_view": "*ig_insights",
        "field": "ig_media_total_likes",
        "aggregation": "sum",
    },
    {
        "key": "ig_shares",
        "label": "IG Shares",
        "data_view": "*ig_insights",
        "field": "ig_media_total_shares",
        "aggregation": "sum",
    },
    {
        "key": "yt_reactions",
        "label": "YT Likes",
        "data_view": "*youtube_analytics",
        "field": "likes",
        "aggregation": "sum",
    },
    {
        "key": "yt_shares",
        "label": "YT Shares",
        "data_view": "*youtube_analytics",
        "field": "shares",
        "aggregation": "sum",
    },
    {
        "key": "comments",
        "label": "Comments",
        "data_view": "*youtube_analytics",
        "field": "comments",
        "aggregation": "sum",
    },
]


def main():
    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: Cannot connect to Elasticsearch")
        sys.exit(1)

    # Check if a Geographic Map dashboard already exists
    try:
        result = es.search(
            index=DASHBOARDS_INDEX,
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"dashboard_type": "echarts"}},
                            {"match": {"title": DASHBOARD_TITLE}},
                        ]
                    }
                },
                "size": 1,
            },
        )
        hits = result.get("hits", {}).get("hits", [])
        if hits:
            existing_id = hits[0].get("_source", {}).get("dashboard_id") or hits[0].get("_id")
            doc_id = hits[0].get("_id")
            # Patch the default statistic and statistics list on the existing document
            existing_source = hits[0].get("_source", {})
            existing_config = existing_source.get("echarts_config", {})
            charts = existing_config.get("charts", [])
            for chart in charts:
                if chart.get("chart_type") == "leaflet_map":
                    chart["map_statistic"] = "reach"
                    chart["map_statistics"] = MAP_STATISTICS
            es.index(
                index=DASHBOARDS_INDEX,
                id=doc_id,
                document=existing_source,
                refresh="wait_for",
            )
            print(f"Updated existing dashboard — dashboard_id: {existing_id}")
            return
    except Exception as e:
        print(f"Warning: search failed ({e}), proceeding with creation")

    # Create the dashboard document
    new_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc).isoformat()

    doc = {
        "dashboard_id": new_id,
        "dashboard_type": "echarts",
        "title": DASHBOARD_TITLE,
        "menu": "Overview",
        "visibility_mode": "show_all",
        "allowed_org_esids": [],
        "allowed_team_esids": [],
        "created_at": now,
        "updated_at": now,
        "echarts_config": {
            "global_date_range": None,
            "charts": [
                {
                    "chart_id": "map-main",
                    "chart_type": "leaflet_map",
                    "title": "Geographic Map",
                    "map_statistic": "reach",
                    "map_statistics": MAP_STATISTICS,
                    "width": None,
                    "height": "600px",
                }
            ],
        },
    }

    try:
        es.index(
            index=DASHBOARDS_INDEX,
            id=new_id,
            document=doc,
            refresh="wait_for",
        )
        print(f"Created — dashboard_id: {new_id}")
    except Exception as e:
        print(f"ERROR: Failed to create dashboard: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
