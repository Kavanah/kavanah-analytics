"""
Seed/update script: ensure a "Trends" trend_line dashboard exists in
de_asset_echarts_dashboards under the Reports menu group.

Idempotent — if a trend_line dashboard already exists it is updated in place;
otherwise a new one is created with a fixed, stable UUID.

Run on the server after git pull:
    sudo docker exec imports_container python3 \
      /app/python/asset_management/web/blueprints/dashboard_builder/update_trends_dashboard.py
"""

import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")

try:
    from lib.es_helpers import get_elasticsearch_client, dual_term
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    from lib.es_helpers import get_elasticsearch_client, dual_term

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"

# Fixed stable ID so this script is always idempotent
TRENDS_DASHBOARD_ID = "trends-reach-dashboard-0001"

TREND_SOURCES = [
    # ── Reach ──────────────────────────────────────────────────────────────
    {
        "label": "Meta",
        "category": "Reach",
        "default_visible": True,
        "data_view": "*fb_insights",
        "field": "insight_reach",
        "color": "#5470c6",
    },
    {
        # page_impressions_unique → page_total_media_view_unique
        # (Meta v23 deprecated the former; the latter carries the same
        # "unique viewers" semantics).
        "label": "Organic Facebook",
        "category": "Reach",
        "default_visible": False,
        "data_view": "*fb_page_insights",
        "field": "page_total_media_view_unique",
        "color": "#91cc75",
    },
    {
        "label": "Organic Instagram",
        "category": "Reach",
        "default_visible": False,
        "data_view": "*ig_insights",
        "field": "ig_insight_reach",
        "color": "#fac858",
    },
    {
        "label": "Organic Web",
        "category": "Reach",
        "default_visible": False,
        "data_view": "*ga_metrics",
        "field": "totalUsers",
        "color": "#9a60b4",
    },
    # ── Engagement ─────────────────────────────────────────────────────────
    {
        "label": "15s Video Views",
        "category": "Engagement",
        "default_visible": False,
        "data_view": "*fb_page_insights",
        "field": "page_video_views_15s_total",
        "color": "#ee6666",
    },
    {
        "label": "View Time",
        "category": "Engagement",
        "default_visible": False,
        "data_view": "*fb_page_insights",
        "field": "page_video_avg_sec_watched",
        "color": "#fc8452",
    },
    {
        "label": "Facebook Follows",
        "category": "Engagement",
        "default_visible": False,
        "data_view": "*fb_page_insights",
        "field": "page_follows",
        "color": "#73c0de",
    },
    {
        "label": "Instagram Follows",
        "category": "Engagement",
        "default_visible": False,
        "data_view": "*ig_insights",
        "field": "ig_user_followers_count",
        "color": "#3ba272",
    },
    {
        "label": "YouTube Subs",
        "category": "Engagement",
        "default_visible": False,
        "data_view": "*youtube_analytics",
        "field": "subscribers_gained",
        "color": "#ea7ccc",
    },
    {
        "label": "App Installs",
        "category": "Engagement",
        "default_visible": False,
        "data_view": "*google_ads",
        "field": "app_installs",
        "color": "#4dc9f6",
    },
    # ── Actions ────────────────────────────────────────────────────────────
    {
        "label": "FB Reactions",
        "category": "Actions",
        "default_visible": False,
        "data_view": "*fb_page_insights",
        "field": "page_actions_post_reactions_total",
        "color": "#5470c6",
    },
    {
        "label": "FB Shares",
        "category": "Actions",
        "default_visible": False,
        "data_view": "*fb_page_insights",
        "field": "page_posts_shares_total",
        "color": "#91cc75",
    },
    {
        "label": "IG Reactions",
        "category": "Actions",
        "default_visible": False,
        "data_view": "*ig_insights",
        "field": "ig_media_total_likes",
        "color": "#fac858",
    },
    {
        "label": "IG Shares",
        "category": "Actions",
        "default_visible": False,
        "data_view": "*ig_insights",
        "field": "ig_media_total_shares",
        "color": "#ee6666",
    },
    {
        "label": "YT Likes",
        "category": "Actions",
        "default_visible": False,
        "data_view": "*youtube_analytics",
        "field": "likes",
        "color": "#fc8452",
    },
    {
        "label": "YT Shares",
        "category": "Actions",
        "default_visible": False,
        "data_view": "*youtube_analytics",
        "field": "shares",
        "color": "#73c0de",
    },
    # ── Interaction ────────────────────────────────────────────────────────
    {
        "label": "Messaging Contacts",
        "category": "Interaction",
        "default_visible": False,
        "data_view": "*echo_conversations",
        "field": None,
        "color": "#3ba272",
    },
    {
        "label": "Messages Received",
        "category": "Interaction",
        "default_visible": False,
        "data_view": "*echo_conversations",
        "field": "incomingMessagesCount",
        "color": "#ea7ccc",
    },
    {
        "label": "Messages Sent",
        "category": "Interaction",
        "default_visible": False,
        "data_view": "*echo_conversations",
        "field": "outboundMessagesCount",
        "color": "#4dc9f6",
    },
    {
        "label": "Spiritual Convos",
        "category": "Interaction",
        "default_visible": False,
        "data_view": "*echo_conversations,-unknown_echo_conversations",
        "field": "spiritual_conversation",
        "color": "#5470c6",
    },
    {
        "label": "Comments",
        "category": "Interaction",
        "default_visible": False,
        "data_view": "*youtube_analytics",
        "field": "comments",
        "color": "#91cc75",
    },
    # ── Journey ────────────────────────────────────────────────────────────
    {
        "label": "New Contacts",
        "category": "Journey",
        "default_visible": False,
        "data_view": "disciple-tools",
        "field": None,
        "date_field": "date_created_ms",
        "date_field_filter": "date_created_ms",
        "color": "#fac858",
    },
    {
        "label": "1st Meetings",
        "category": "Journey",
        "default_visible": False,
        "data_view": "disciple-tools",
        "field": None,
        "query_filter": {"term": {"meta.seeker_path.keyword": "met"}},
        "date_field": "meta.first_contact_date_ms",
        "date_field_filter": "meta.first_contact_date_ms",
        "color": "#ee6666",
    },
    {
        "label": "Baptisms",
        "category": "Journey",
        "default_visible": False,
        "data_view": "disciple-tools",
        "field": None,
        "query_filter": {"exists": {"field": "meta.baptism_date"}},
        "date_field": "meta.baptism_date_ms",
        "date_field_filter": "meta.baptism_date_ms",
        "color": "#fc8452",
    },
    {
        "label": "New Groups",
        "category": "Journey",
        "default_visible": False,
        "data_view": "disciple-tools",
        "field": None,
        # bare keyword (unified disciple_tools schema);.keyword matched nothing there.
        "query_filter": {"term": {"post_type": "groups"}},
        "date_field": "meta.start_date_ms",
        "date_field_filter": "meta.start_date_ms",
        "color": "#73c0de",
    },
    {
        "label": "New Churches",
        "category": "Journey",
        "default_visible": False,
        "data_view": "disciple-tools",
        "field": None,
        "query_filter": {"exists": {"field": "meta.church_start_date"}},
        "date_field": "meta.church_start_date_ms",
        "date_field_filter": "meta.church_start_date_ms",
        "color": "#3ba272",
    },
]

TRENDS_CHART = {
    "chart_id": "trends-reach-chart-0001",
    "chart_type": "trend_line",
    "title": "How many people saw our content?",
    "sources": TREND_SOURCES,
    "date_interval": "month",
    "width": "100%",
    "height": "600px",
}


def _build_doc(now):
    return {
        "dashboard_id": TRENDS_DASHBOARD_ID,
        "dashboard_type": "echarts",
        "title": "Trends",
        "menu": "Overview",
        "menu_position": 1,
        "visibility_mode": "show_all",
        "allowed_org_esids": [],
        "allowed_team_esids": [],
        "updated_at": now,
        "echarts_config": {
            "global_date_range": None,
            "charts": [TRENDS_CHART],
        },
    }


def main():
    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: Cannot connect to Elasticsearch")
        sys.exit(1)

    now = datetime.now(tz=timezone.utc).isoformat()

    # Check whether our fixed-ID document already exists
    try:
        existing = es.get(index=DASHBOARDS_INDEX, id=TRENDS_DASHBOARD_ID, ignore=[404])
        found = existing.get("found", False)
    except Exception:
        found = False

    doc = _build_doc(now)
    if not found:
        doc["created_at"] = now

    try:
        es.index(
            index=DASHBOARDS_INDEX,
            id=TRENDS_DASHBOARD_ID,
            document=doc,
            refresh="wait_for",
        )
        action = "Created" if not found else "Updated"
        print(f"{action} Trends dashboard (id={TRENDS_DASHBOARD_ID})")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Delete any other "Trends" echarts dashboards that are not our fixed-ID one
    try:
        result = es.search(
            index=DASHBOARDS_INDEX,
            body={
                "query": {
                    "bool": {
                        "must": [{"match": {"title": "Trends"}}],
                        # dual-term so the canonical dashboard is
                        # excluded regardless of dashboard_id mapping shape. The
                        # _id clause below is the true safeguard (the doc is
                        # indexed with id=TRENDS_DASHBOARD_ID, so _id always
                        # matches), but the dashboard_id clause was silently dead
                        # on bare-keyword prod — normalize it so the exclusion
                        # doesn't rely solely on _id.
                        "must_not": [
                            dual_term("dashboard_id", TRENDS_DASHBOARD_ID),
                            {"term": {"_id": TRENDS_DASHBOARD_ID}},
                        ],
                    }
                },
                "size": 10,
            },
        )
        for hit in result.get("hits", {}).get("hits", []):
            old_title = hit["_source"].get("title", "")
            es.delete(index=DASHBOARDS_INDEX, id=hit["_id"], refresh="wait_for")
            print(f'Deleted duplicate: "{old_title}" (id={hit["_id"]})')
    except Exception as e:
        print(f"Warning: could not delete duplicates: {e}")


if __name__ == "__main__":
    main()
