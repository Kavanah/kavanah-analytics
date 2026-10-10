"""
One-time fix: clean up the 4 orphaned ESID references found on 2026-04-14.

- 2 deleted GA Properties referenced by teams via team_ga4_property_esids (array)
- 2 deleted Regions referenced by teams via team_region_esid (array)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.es_helpers import get_elasticsearch_client

ORPHANS = [
    {
        "child_index": "de_asset_teams",
        "esid_field": "team_ga4_property_esids",
        "title_field": "team_ga4_properties",
        "orphan_esids": ["-kxTw5sBIm2AdJKuZd6A", "7UxSw5sBIm2AdJKuvd55"],
        "is_array": True,
    },
    {
        "child_index": "de_asset_teams",
        "esid_field": "team_region_esid",
        "title_field": "team_region",
        "orphan_esids": ["N6YIO5UBJb5rGJQSiBrg", "OKYIO5UBJb5rGJQS4Roq"],
        "is_array": True,
    },
]

ARRAY_CLEANUP_SCRIPT = """
    if (ctx._source.containsKey(params.esid_field) && ctx._source[params.esid_field] instanceof List) {
        for (int i = ctx._source[params.esid_field].size() - 1; i >= 0; i--) {
            if (params.doc_ids.contains(ctx._source[params.esid_field].get(i))) {
                ctx._source[params.esid_field].remove(i);
                if (ctx._source.containsKey(params.title_field)
                        && ctx._source[params.title_field] instanceof List
                        && i < ctx._source[params.title_field].size()) {
                    ctx._source[params.title_field].remove(i);
                }
            }
        }
    }
"""


def main():
    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: Cannot connect to Elasticsearch.")
        sys.exit(1)

    for orphan in ORPHANS:
        print(f"\nCleaning {orphan['child_index']}.{orphan['esid_field']}...")
        print(f"  Removing ESIDs: {orphan['orphan_esids']}")

        body = {
            "query": {"terms": {orphan["esid_field"] + ".keyword": orphan["orphan_esids"]}},
            "script": {
                "source": ARRAY_CLEANUP_SCRIPT,
                "lang": "painless",
                "params": {
                    "esid_field": orphan["esid_field"],
                    "title_field": orphan["title_field"],
                    "doc_ids": orphan["orphan_esids"],
                },
            },
        }

        try:
            response = es.update_by_query(
                index=orphan["child_index"],
                body=body,
                conflicts="proceed",
                refresh=True,
            )
            updated = response.get("updated", 0)
            print(f"  Updated {updated} document(s)")
        except Exception as e:
            print(f"  ERROR: {e}")

    print("\nDone. Re-running check to verify...")


if __name__ == "__main__":
    main()
