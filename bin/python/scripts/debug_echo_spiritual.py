#!/usr/bin/env python3
"""Debug: replicate full _query_echo_country_map for spiritual_convos_echo stat."""

import sys
import os
import json
import logging

sys.path.insert(0, "/app/python")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logging.basicConfig(level=logging.WARNING)

from lib.es_helpers import get_elasticsearch_client

es = get_elasticsearch_client()

# Replicate exactly what the route does for spiritual_convos_echo
stat_cfg = {
    "data_source": "echo",
    "field": "spiritual_conversation",
    "type": "range",
    "gte": 1,
}

# Build stat filter
stat_filter = {"range": {"spiritual_conversation": {"gte": 1}}}

# No org/team/date filters for this test (same as selecting "All" in the UI)
must = [stat_filter]
query = {"bool": {"must": must}}

# No cardinality_field, so we go to the non-cardinality path
total_agg = {"sum": {"field": "spiritual_conversation"}}

print("=== Replicating _query_echo_country_map (non-cardinality path) ===")
print(f"Query: {json.dumps(query)}")
print()

for field in ("contact_country_iso2.keyword", "contact_country_iso2", "team_country.keyword", "team_country"):
    is_iso2_field = field.startswith("contact_country_iso2")
    sub_aggs = {}
    if not is_iso2_field:
        sub_aggs["iso3_hit"] = {
            "top_hits": {
                "size": 1,
                "_source": {"includes": ["team_country_iso3_code"]},
            }
        }
    sub_aggs["total"] = total_agg

    agg_body = {
        "size": 0,
        "query": query,
        "aggs": {
            "by_country": {
                "terms": {"field": field, "size": 300},
                "aggs": sub_aggs,
            }
        },
    }

    try:
        resp = es.search(index="*echo_conversations,-unknown_echo_conversations", body=agg_body)
        rd = resp.body if hasattr(resp, "body") else dict(resp)
        buckets = rd.get("aggregations", {}).get("by_country", {}).get("buckets", [])
        print(f"Field: {field}")
        print(f"  Buckets: {len(buckets)}")
        if buckets:
            for b in buckets[:15]:
                val = b.get("total", {}).get("value") or b["doc_count"]
                print(f"    {b['key']}: count={b['doc_count']}, value={val}")
            print(f"  >>> Would return {len(buckets)} points")
            print(f"  >>> This is the field the map API uses — returns here, stops iterating")
            break
        else:
            print(f"  No buckets — trying next field...")
    except Exception as e:
        print(f"Field: {field}")
        print(f"  ERROR: {e}")
        print(f"  Trying next field...")
