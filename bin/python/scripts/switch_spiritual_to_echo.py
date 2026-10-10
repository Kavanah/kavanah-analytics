#!/usr/bin/env python3
"""Switch Spiritual Conversations from DT to Echo in dashboard configs.

Updates:
1. Trends dashboard: Spiritual Convos source → Echo data view
2. Maps dashboard: spiritual_conversations map_statistic → Echo data view

Run inside imports_container:
    python3 /app/python/scripts/switch_spiritual_to_echo.py
"""

import sys
import json

sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"
TRENDS_ID = "trends-reach-dashboard-0001"
MAPS_ID = "92bfe8c2-e4e5-46fc-8c84-a8d2eec258d0"


def update_trends(es: object) -> None:
    """Update Spiritual Convos trend source from DT to Echo."""
    r = es.get(index=DASHBOARDS_INDEX, id=TRENDS_ID)
    rd = r.body if hasattr(r, "body") else dict(r)
    source = rd["_source"]
    charts = source.get("echarts_config", {}).get("charts", [])

    changed = False
    for chart in charts:
        if chart.get("chart_type") != "trend_line":
            continue
        for src in chart.get("sources", []):
            if src.get("label") == "Spiritual Convos" and src.get("data_view") == "disciple-tools":
                old = json.dumps(src)
                src["data_view"] = "*echo_conversations,-unknown_echo_conversations"
                src["field"] = "spiritual_conversation"
                # Remove DT-specific date field config if present
                src.pop("date_field_filter", None)
                src.pop("trends_date_field", None)
                print(f"  Trends: updated source")
                print(f"    was:  {old}")
                print(f"    now:  {json.dumps(src)}")
                changed = True

    if changed:
        es.update(
            index=DASHBOARDS_INDEX,
            id=TRENDS_ID,
            body={"doc": {"echarts_config": source["echarts_config"]}},
            refresh="wait_for",
        )
        print("  Trends dashboard updated in ES.")
    else:
        print("  Trends: no matching source found (already updated?)")


def update_maps(es: object) -> None:
    """Update spiritual_conversations map stat from DT to Echo."""
    r = es.get(index=DASHBOARDS_INDEX, id=MAPS_ID)
    rd = r.body if hasattr(r, "body") else dict(r)
    source = rd["_source"]
    charts = source.get("echarts_config", {}).get("charts", [])

    changed = False
    for chart in charts:
        if chart.get("chart_type") != "leaflet_map":
            continue
        for stat in chart.get("map_statistics", []):
            if stat.get("key") == "spiritual_conversations" and stat.get("data_view") == "disciple-tools":
                old = json.dumps(stat)
                stat["data_view"] = "*echo_conversations,-unknown_echo_conversations"
                stat["field"] = "spiritual_conversation"
                stat["key"] = "spiritual_convos_echo"
                stat["label"] = "Spiritual Conversations"
                print(f"  Maps: updated map_statistic")
                print(f"    was:  {old}")
                print(f"    now:  {json.dumps(stat)}")
                changed = True

    if changed:
        es.update(
            index=DASHBOARDS_INDEX,
            id=MAPS_ID,
            body={"doc": {"echarts_config": source["echarts_config"]}},
            refresh="wait_for",
        )
        print("  Maps dashboard updated in ES.")
    else:
        print("  Maps: no matching stat found (already updated?)")


def main() -> None:
    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: Cannot connect to ES")
        sys.exit(1)

    print("=== Switching Spiritual Conversations to Echo ===\n")
    print("[Trends Dashboard]")
    update_trends(es)
    print("\n[Maps Dashboard]")
    update_maps(es)
    print("\nDone.")


if __name__ == "__main__":
    main()
