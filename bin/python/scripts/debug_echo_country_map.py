#!/usr/bin/env python3
"""Debug: replicate _query_echo_country_map logic for spiritual_convos_echo."""

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

# The stat config for spiritual_convos_echo
stat_cfg = {
    "data_source": "echo",
    "field": "spiritual_conversation",
    "type": "range",
    "gte": 1,
}

# Step 1: Check field mapping
print("=== Field mapping for contact_country_iso2 ===")
try:
    mapping = es.indices.get_mapping(index="kavanah_team_echo_conversations")
    md = mapping.body if hasattr(mapping, "body") else dict(mapping)
    props = md["kavanah_team_echo_conversations"]["mappings"].get("properties", {})
    cc = props.get("contact_country_iso2")
    print(f"  contact_country_iso2: {json.dumps(cc, indent=2)}")
    sc = props.get("spiritual_conversation")
    print(f"  spiritual_conversation: {json.dumps(sc, indent=2)}")
except Exception as e:
    print(f"  ERROR: {e}")

# Step 2: Check if contact_country_iso2 has data
print("\n=== contact_country_iso2 value distribution ===")
for field in ("contact_country_iso2.keyword", "contact_country_iso2"):
    try:
        resp = es.search(
            index="kavanah_team_echo_conversations",
            body={
                "size": 0,
                "aggs": {"by_country": {"terms": {"field": field, "size": 300}}},
            },
        )
        rd = resp.body if hasattr(resp, "body") else dict(resp)
        buckets = rd.get("aggregations", {}).get("by_country", {}).get("buckets", [])
        print(f"  {field}: {len(buckets)} buckets")
        for b in buckets[:10]:
            print(f"    {b['key']}: {b['doc_count']}")
        if buckets:
            break
    except Exception as e:
        print(f"  {field}: ERROR {e}")

# Step 3: Replicate the actual query with spiritual_conversation filter
print("\n=== Echo country map with spiritual_conversation >= 1 ===")
query = {
    "bool": {
        "must": [
            {"range": {"spiritual_conversation": {"gte": 1}}},
        ]
    }
}

for field in ("contact_country_iso2.keyword", "contact_country_iso2", "team_country.keyword", "team_country"):
    try:
        resp = es.search(
            index="*echo_conversations,-unknown_echo_conversations",
            body={
                "size": 0,
                "query": query,
                "aggs": {
                    "by_country": {
                        "terms": {"field": field, "size": 300},
                        "aggs": {"total": {"sum": {"field": "spiritual_conversation"}}},
                    }
                },
            },
        )
        rd = resp.body if hasattr(resp, "body") else dict(resp)
        buckets = rd.get("aggregations", {}).get("by_country", {}).get("buckets", [])
        print(f"  {field}: {len(buckets)} buckets")
        for b in buckets[:10]:
            print(f"    {b['key']}: count={b['doc_count']}, total={b['total']['value']}")
        if buckets:
            print(f"  ^ This is what the map would use")
            break
    except Exception as e:
        print(f"  {field}: ERROR {e}")

# Step 4: Check how many docs have BOTH contact_country_iso2 and spiritual_conversation
print("\n=== Docs with BOTH contact_country_iso2 AND spiritual_conversation >= 1 ===")
try:
    resp = es.count(
        index="kavanah_team_echo_conversations",
        body={
            "query": {
                "bool": {
                    "must": [
                        {"exists": {"field": "contact_country_iso2"}},
                        {"range": {"spiritual_conversation": {"gte": 1}}},
                    ]
                }
            }
        },
    )
    rd = resp.body if hasattr(resp, "body") else dict(resp)
    print(f"  Count: {rd['count']}")
except Exception as e:
    print(f"  ERROR: {e}")

# Step 5: Check total docs with contact_country_iso2 vs without
print("\n=== Coverage: contact_country_iso2 ===")
try:
    total = es.count(index="kavanah_team_echo_conversations")
    td = total.body if hasattr(total, "body") else dict(total)
    with_field = es.count(
        index="kavanah_team_echo_conversations",
        body={"query": {"exists": {"field": "contact_country_iso2"}}},
    )
    wd = with_field.body if hasattr(with_field, "body") else dict(with_field)
    print(f"  Total docs: {td['count']}")
    print(f"  With contact_country_iso2: {wd['count']}")
    print(f"  Without: {td['count'] - wd['count']}")
except Exception as e:
    print(f"  ERROR: {e}")
