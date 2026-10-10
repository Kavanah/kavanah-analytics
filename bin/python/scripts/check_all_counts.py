#!/usr/bin/env python3
"""Check doc counts across all Kavanah indices.

the kavanah_team_* reverse aliases are retired; patterns match the
ESID-keyed concrete indices directly."""

import sys

sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client

es = get_elasticsearch_client()
if es is None:
    print("ERROR: Cannot connect to ES")
    sys.exit(1)

# disciple-tools / disciple-tools-staging removed: the legacy alias
# and the push-staging index are retired; demo DT lives in the team-keyed
# index below.
indices = [
    "*__disciple_tools",
    "*__fb_insights",
    "*__fb_page_insights",
    "*__google_ads",
    "*__ga_metrics",
    "*__echo_conversations",
    "*__ig_insights",
    "*__youtube_analytics",
    "*__chatwoot_insights",
    "*__chatwoot_conversations",
]

for idx in indices:
    try:
        resp = es.count(index=idx)
        rd = resp.body if hasattr(resp, "body") else dict(resp)
        total = rd["count"]
        print(f"  {idx:45s} {total:>6d} docs")
    except Exception as e:
        err = str(e)
        if "index_not_found" in err:
            print(f"  {idx:45s}  (not found)")
        else:
            print(f"  {idx:45s}  ERROR: {err[:80]}")
