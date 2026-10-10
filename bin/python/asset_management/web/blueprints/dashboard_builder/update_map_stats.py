"""
Migration: add new map statistics to the Maps dashboard in ES.

Idempotent — skips any stat already present.

Run on the server:
    sudo docker exec imports_container python3 \
      /app/python/asset_management/web/blueprints/dashboard_builder/update_map_stats.py
"""

import sys
import os

sys.path.insert(0, "/app/python")

try:
    from lib.es_helpers import get_elasticsearch_client
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    from lib.es_helpers import get_elasticsearch_client

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"
DASHBOARD_TITLE = "Maps"

# Stats to add, each with an 'insert_after' key indicating which existing stat
# they should follow (None = append at end).
NEW_STATS = [
    {
        "stat": {
            "key": "echo_multiplier",
            "label": "Echo multiplier",
            "data_view": "*echo_conversations",
            "field": "multiplier",
            "aggregation": "sum",
        },
        "insert_after": "dt_contacts",
    },
    {
        "stat": {
            "key": "google_ads_app_installs",
            "label": "Google Ads app installs",
            "data_view": "*google_ads",
            "field": "app_installs",
            "aggregation": "sum",
        },
        "insert_after": "echo_multiplier",
    },
]


def main():
    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: Cannot connect to Elasticsearch")
        sys.exit(1)

    # Find the Maps dashboard
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
    if not hits:
        print("ERROR: Maps dashboard not found in Elasticsearch")
        sys.exit(1)

    doc_id = hits[0]["_id"]
    source = hits[0]["_source"]
    charts = source.get("echarts_config", {}).get("charts", [])
    if not charts:
        print("ERROR: No charts found in dashboard config")
        sys.exit(1)

    map_stats = charts[0].get("map_statistics", [])
    changed = False

    for entry in NEW_STATS:
        new_stat = entry["stat"]
        insert_after = entry["insert_after"]
        key = new_stat["key"]

        if any(s.get("key") == key for s in map_stats):
            print(f"{key} already present — skipping")
            continue

        insert_idx = next(
            (i + 1 for i, s in enumerate(map_stats) if s.get("key") == insert_after),
            len(map_stats),
        )
        map_stats.insert(insert_idx, new_stat)
        print(f"Added {key} at position {insert_idx}")
        changed = True

    if changed:
        charts[0]["map_statistics"] = map_stats
        es.update(
            index=DASHBOARDS_INDEX,
            id=doc_id,
            body={"doc": {"echarts_config": source["echarts_config"]}},
            refresh="wait_for",
        )
        print(f"Dashboard {doc_id} updated successfully")
    else:
        print("No changes needed")


if __name__ == "__main__":
    main()
