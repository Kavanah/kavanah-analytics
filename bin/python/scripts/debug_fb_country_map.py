#!/usr/bin/env python3
"""Debug: replicate _query_fb_country_map logic to see what the map API returns."""

import sys
import os
import json

sys.path.insert(0, "/app/python")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.es_helpers import get_elasticsearch_client

es = get_elasticsearch_client()
if es is None:
    print("ERROR: Cannot connect to ES")
    sys.exit(1)

# Step 1: Check field mapping for location_countries
print("=== Field mapping for location_countries ===")
try:
    mapping = es.indices.get_mapping(index="kavanah_team_fb_insights")
    md = mapping.body if hasattr(mapping, "body") else dict(mapping)
    props = md["kavanah_team_fb_insights"]["mappings"].get("properties", {})
    lc = props.get("location_countries")
    print(f"  location_countries mapping: {json.dumps(lc, indent=2)}")
except Exception as e:
    print(f"  ERROR: {e}")

# Step 2: Check if location_countries.iso2 is a usable keyword field
print("\n=== Aggregation: location_countries.iso2 ===")
try:
    resp = es.search(
        index="kavanah_team_fb_insights",
        body={
            "size": 0,
            "aggs": {
                "countries": {
                    "terms": {"field": "location_countries.iso2", "size": 300},
                    "aggs": {
                        "centroid": {"geo_centroid": {"field": "location_countries.coordinates"}},
                        "stat": {"sum": {"field": "insight_reach"}},
                    },
                },
            },
        },
    )
    rd = resp.body if hasattr(resp, "body") else dict(resp)
    buckets = rd.get("aggregations", {}).get("countries", {}).get("buckets", [])
    print(f"  Buckets: {len(buckets)}")
    for b in buckets[:5]:
        loc = b.get("centroid", {}).get("location", {})
        print(f"    {b['key']}: count={b['doc_count']}, reach={b['stat']['value']}, centroid={loc}")
except Exception as e:
    print(f"  ERROR: {e}")

# Step 3: Check the fallback — team_country
print("\n=== Fallback: team_country ===")
try:
    resp = es.search(
        index="kavanah_team_fb_insights",
        body={
            "size": 0,
            "aggs": {
                "countries": {
                    "terms": {"field": "team_country", "size": 300},
                    "aggs": {"stat": {"sum": {"field": "insight_reach"}}},
                },
            },
        },
    )
    rd = resp.body if hasattr(resp, "body") else dict(resp)
    buckets = rd.get("aggregations", {}).get("countries", {}).get("buckets", [])
    print(f"  Buckets: {len(buckets)}")
    for b in buckets[:5]:
        print(f"    {b['key']}: count={b['doc_count']}, reach={b['stat']['value']}")
except Exception as e:
    print(f"  ERROR: {e}")

# Step 4: Check a raw doc to see the actual stored structure
print("\n=== Raw doc sample (location_countries) ===")
try:
    resp = es.search(
        index="kavanah_team_fb_insights",
        body={"size": 1, "_source": ["location_countries"], "query": {"exists": {"field": "location_countries"}}},
    )
    rd = resp.body if hasattr(resp, "body") else dict(resp)
    for h in rd["hits"]["hits"]:
        print(f"  {json.dumps(h['_source'], indent=2)}")
except Exception as e:
    print(f"  ERROR: {e}")
