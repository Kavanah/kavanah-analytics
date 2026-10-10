"""
Seed the Chat Woot ECharts dashboard into de_asset_echarts_dashboards.

Creates a "Chat Analytics" table dashboard under the "Chat Woot" menu group
showing per-team conversation metrics from the chatwoot_insights index.

Safe to re-run — uses a stable document ID.

Usage:
  python3 /app/python/import.py chatwoot seed_chatwoot_dashboard
"""

import logging
from datetime import datetime, timezone

import lib.logging
from lib.es_helpers import get_elasticsearch_client

DASHBOARD_INDEX = "de_asset_echarts_dashboards"
DASHBOARD_ID = "chatwoot-chat-analytics"


def main(params):
    lib.logging.define("chatwoot-seed-dashboard")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return

    now = datetime.now(timezone.utc).isoformat()

    doc = {
        "dashboard_id": DASHBOARD_ID,
        "dashboard_type": "echarts",
        "title": "Chat Analytics",
        "menu": "Chat Woot",
        "visibility_mode": "show_all",
        "allowed_org_esids": [],
        "allowed_team_esids": [],
        "created_at": now,
        "updated_at": now,
        "echarts_config": {
            "global_date_range": None,
            "charts": [
                {
                    "chart_id": "c1",
                    "chart_type": "table",
                    "title": "Chat Analytics",
                    # Wildcard data view spanning every team's per-team index
                    # ({team_esid}__chatwoot_insights). A bare "chatwoot_insights"
                    # is a literal index name that matches nothing (no such index
                    # or alias exists), so the chart — and any report-scheduler
                    # snapshot built from it — returned zero rows for every team.
                    # Matches the Kibana data view in deployment/create-data-views.sh.
                    "data_view": "*_chatwoot_insights",
                    # The bare ``team_title``: the unified schema maps it as
                    # ``keyword`` with no ``.keyword`` sub-field, on every
                    # per-team index. grouped on ``.keyword`` while
                    # some indices were still ``text``-mapped; the
                    # mapping sweep rebuilt those, after which ``.keyword``
                    # existed on no index and the table silently rendered zero
                    # rows for every team. A terms agg on a missing
                    # field does not error — it returns no buckets.
                    "x_field": "team_title",
                    "x_label": "Name of team",
                    "table_columns": [
                        {
                            "field": "conversations_count",
                            "label": "Conversation count",
                            "aggregation": "sum",
                            "decimals": 0,
                        },
                        {
                            "field": "incoming_messages_count",
                            "label": "Incoming messages",
                            "aggregation": "sum",
                            "decimals": 0,
                        },
                        {
                            "field": "outgoing_messages_count",
                            "label": "Outgoing messages",
                            "aggregation": "sum",
                            "decimals": 0,
                        },
                        {
                            "field": "resolutions_count",
                            "label": "Resolutions",
                            "aggregation": "sum",
                            "decimals": 0,
                        },
                        {
                            "field": "avg_first_response_time",
                            "label": "Avg first response (sec)",
                            "aggregation": "avg",
                            "decimals": 1,
                        },
                        {
                            "field": "avg_resolution_time",
                            "label": "Avg resolution (sec)",
                            "aggregation": "avg",
                            "decimals": 1,
                        },
                    ],
                    "display_toggles": {},
                    "filters": [],
                    "position": {"col": 1, "row": 1, "w": 12, "h": 8},
                }
            ],
        },
    }

    try:
        es.index(index=DASHBOARD_INDEX, id=DASHBOARD_ID, body=doc)
        logging.info(f"Dashboard '{DASHBOARD_ID}' saved to '{DASHBOARD_INDEX}'.")
    except Exception as e:
        logging.error(f"Failed to save dashboard: {e}")
