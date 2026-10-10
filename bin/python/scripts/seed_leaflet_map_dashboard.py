"""
seed_leaflet_map_dashboard.py
One-time script to create the initial leaflet_map dashboard document
in the de_asset_echarts_dashboards index.

Run from inside the imports_container:
  python3 /app/python/scripts/seed_leaflet_map_dashboard.py
"""

import sys
import os
import uuid
from datetime import datetime, timezone

# Allow imports from /app/python
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.es_helpers import get_elasticsearch_client

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"

LEAFLET_MAP_DOC = {
    "dashboard_id": str(uuid.uuid4()),
    "dashboard_type": "leaflet_map",
    "title": "Geographic Map",
    "created_at": datetime.now(timezone.utc).isoformat(),
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "visibility_mode": "show_all",
    "allowed_org_esids": [],
    "allowed_team_esids": [],
    "map_config": {
        "default_statistic": "reach",
        "statistics": [
            {
                "key": "reach",
                "label": "Reach",
                "data_source": "fb_insights",
                "field": "insight_reach",
                "type": "range",
                "gte": 1,
            },
            {
                "key": "page_visits",
                "label": "Page Visits",
                "data_source": "fb_insights",
                "field": "insight_actions_page_engagement",
                "type": "range",
                "gte": 1,
            },
            {
                "key": "video_views",
                "label": "Video Views",
                "data_source": "fb_insights",
                "field": "insight_actions_video_view_total",
                "type": "range",
                "gte": 1,
            },
            {
                "key": "messages",
                "label": "Messages Received",
                "data_source": "echo",
                "field": "incomingMessagesCount",
                "type": "range",
                "gte": 1,
            },
            {
                "key": "baptisms",
                "label": "Baptisms",
                "data_source": "dt",
                "field": "Meta.milestones",
                "type": "phrase",
                "value": "milestone_baptized",
            },
            {
                "key": "dt_contacts",
                "label": "DT Contacts",
                "data_source": "dt",
                "field": None,
            },
            {"key": "dt_contacts", "label": "DT Contacts", "data_source": "dt", "field": None},
            {
                "key": "salvation",
                "label": "Salvation Decisions",
                "data_source": "dt",
                "field": "Meta.faith_status",
                "type": "phrase",
                "value": "believer",
            },
        ],
    },
}


def main():
    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: Could not connect to Elasticsearch.")
        sys.exit(1)

    doc_id = LEAFLET_MAP_DOC["dashboard_id"]

    # Check if a leaflet_map dashboard already exists
    try:
        result = es.search(
            index=DASHBOARDS_INDEX,
            body={"query": {"term": {"dashboard_type": "leaflet_map"}}, "size": 1},
        )
        hits = result.get("hits", {}).get("hits", [])
        if hits:
            existing = hits[0]["_source"]
            print(f"A leaflet_map dashboard already exists: '{existing.get('title')}' (id={existing.get('dashboard_id')})")
            print("Nothing to do. Delete it first if you want to reseed.")
            return
    except Exception as e:
        print(f"Warning: could not check for existing document: {e}")

    # Index the document
    resp = es.index(index=DASHBOARDS_INDEX, id=doc_id, document=LEAFLET_MAP_DOC)
    print(f"Created leaflet_map dashboard document:")
    print(f"  Index:  {DASHBOARDS_INDEX}")
    print(f"  Doc ID: {doc_id}")
    print(f"  Title:  {LEAFLET_MAP_DOC['title']}")
    print(f"  Result: {resp.get('result', resp)}")


if __name__ == "__main__":
    main()
