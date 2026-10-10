#!/usr/bin/env python3
"""Quick check: do the new audience geo fields exist in Kavanah indices?"""

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


def check(index, label, query, source_fields):
    try:
        r = es.search(index=index, body={"size": 3, "_source": source_fields, "query": query})
        rd = r.body if hasattr(r, "body") else dict(r)
        total = rd["hits"]["total"]["value"]
        print(f"\n=== {label} ===")
        print(f"  Matching docs: {total}")
        for h in rd["hits"]["hits"][:3]:
            print(f"  {json.dumps(h['_source'], indent=None)}")
    except Exception as e:
        print(f"\n=== {label} ===")
        print(f"  ERROR: {e}")


# 1. FB insights — location_countries
check(
    "*__fb_insights",
    "FB insights: location_countries",
    {"exists": {"field": "location_countries"}},
    ["location_countries"],
)

# 2. Echo — contact_country_iso2
check(
    "*__echo_conversations",
    "Echo: contact_country_iso2",
    {"exists": {"field": "contact_country_iso2"}},
    ["contact_country_iso2", "country"],
)

# 3. Google Ads — geo records
check(
    "*__google_ads",
    "Google Ads: geo records",
    {"term": {"record_type": "geo"}},
    ["record_type", "geo_country_name", "geo_country_code"],
)

# 4. YouTube — country_breakdown
check(
    "*__youtube_analytics",
    "YouTube: country_breakdown",
    {"term": {"record_type": "country_breakdown"}},
    ["record_type", "country"],
)

# 5. Also check total counts per index
print("\n=== Index doc counts ===")
for idx in [
    "*__fb_insights",
    "*__echo_conversations",
    "*__google_ads",
    "*__youtube_analytics",
    "*__ga_metrics",
]:
    try:
        c = es.count(index=idx)
        cd = c.body if hasattr(c, "body") else dict(c)
        print(f"  {idx}: {cd['count']} docs")
    except Exception as e:
        print(f"  {idx}: ERROR {e}")
