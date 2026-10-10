"""
Diagnostic script: Check for orphaned ESID references across all asset indices.

For each parent-child relationship defined in ASSET_CONFIGS, this script:
1. Collects all unique ESID values stored in the child index
2. Checks which of those ESIDs no longer exist in the parent index
3. Reports orphaned references with document details

Usage (inside imports_container):
    python3 /app/python/migration/check_orphaned_references.py
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.es_helpers import get_elasticsearch_client
from asset_management.web.asset_configs import ASSET_CONFIGS


def _build_reverse_map():
    """Build reverse relationship map from ASSET_CONFIGS (self-contained)."""
    reverse_map = {}
    for _asset_type, config in ASSET_CONFIGS.items():
        child_index = config["index"]
        for field in config.get("fields", []):
            if not field.get("store_esid"):
                continue
            source_index = field["source"]
            entry = {
                "child_index": child_index,
                "esid_field": field["esid_field"],
                "title_field": field["name"],
                "is_array": field.get("type") == "multi_select",
            }
            reverse_map.setdefault(source_index, []).append(entry)
    return reverse_map


REVERSE_MAP = _build_reverse_map()


def get_title_field_for_index(index_name):
    """Get the title_field for an asset type by its index name."""
    for config in ASSET_CONFIGS.values():
        if config["index"] == index_name:
            return config.get("title_field")
    return None


def collect_esid_values(es, child_index, esid_field, is_array):
    """Collect all unique ESID values from a child index's field."""
    # Try .keyword sub-field first (ESID fields are often mapped as text)
    for field_variant in [f"{esid_field}.keyword", esid_field]:
        body = {
            "size": 0,
            "aggs": {
                "unique_esids": {
                    "terms": {
                        "field": field_variant,
                        "size": 10000,
                    }
                }
            },
        }
        try:
            result = es.search(index=child_index, body=body)
            buckets = result["aggregations"]["unique_esids"]["buckets"]
            return {b["key"]: b["doc_count"] for b in buckets}
        except Exception:
            continue
    print(f"  WARN: could not aggregate {child_index}.{esid_field}")
    return {}


def check_esids_exist(es, parent_index, esid_values):
    """Check which ESIDs actually exist in the parent index. Returns set of missing ones."""
    if not esid_values:
        return set()

    missing = set()
    # Use mget for efficient bulk lookup
    try:
        docs = [{"_index": parent_index, "_id": esid} for esid in esid_values]
        result = es.mget(body={"docs": docs})
        for doc in result["docs"]:
            if not doc.get("found", False):
                missing.add(doc["_id"])
    except Exception as e:
        print(f"  ERROR checking existence in {parent_index}: {e}")
    return missing


def get_orphan_details(es, child_index, esid_field, orphan_esid, title_field_child):
    """Get details about documents holding an orphaned reference."""
    # Try .keyword sub-field first for exact match on text fields
    for field_variant in [f"{esid_field}.keyword", esid_field]:
        body = {
            "query": {"term": {field_variant: orphan_esid}},
            "size": 10,
            "_source": [title_field_child, esid_field] if title_field_child else [esid_field],
        }
        try:
            result = es.search(index=child_index, body=body)
            hits = result["hits"]["hits"]
            if hits:
                return [
                    {"_id": h["_id"], "title": h["_source"].get(title_field_child, "N/A") if title_field_child else "N/A"}
                    for h in hits
                ]
        except Exception:
            continue
    return []


def main():
    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: Cannot connect to Elasticsearch.")
        sys.exit(1)

    print("=" * 70)
    print("ORPHANED ESID REFERENCE CHECK")
    print("=" * 70)

    total_orphans = 0
    total_affected_docs = 0
    orphan_report = []

    for parent_index, entries in sorted(REVERSE_MAP.items()):
        for entry in entries:
            child_index = entry["child_index"]
            esid_field = entry["esid_field"]
            is_array = entry["is_array"]

            # Get all unique ESID values in the child field
            esid_counts = collect_esid_values(es, child_index, esid_field, is_array)
            if not esid_counts:
                continue

            # Check which ones are orphaned
            missing = check_esids_exist(es, parent_index, list(esid_counts.keys()))
            if not missing:
                continue

            # Get the child's title field for reporting
            child_title_field = get_title_field_for_index(child_index)

            for orphan_esid in sorted(missing):
                doc_count = esid_counts[orphan_esid]
                total_orphans += 1
                total_affected_docs += doc_count

                details = get_orphan_details(es, child_index, esid_field, orphan_esid, child_title_field)

                orphan_report.append(
                    {
                        "parent_index": parent_index,
                        "child_index": child_index,
                        "esid_field": esid_field,
                        "orphan_esid": orphan_esid,
                        "affected_doc_count": doc_count,
                        "sample_docs": details,
                    }
                )

    # Print report
    print()
    if not orphan_report:
        print("NO ORPHANED REFERENCES FOUND. All ESID references are valid.")
    else:
        print(f"FOUND {total_orphans} ORPHANED REFERENCE(S) affecting {total_affected_docs} document(s):")
        print()
        for item in orphan_report:
            print(f"  Parent: {item['parent_index']}")
            print(f"  Child:  {item['child_index']}.{item['esid_field']}")
            print(f"  Missing ESID: {item['orphan_esid']}")
            print(f"  Affected docs: {item['affected_doc_count']}")
            if item["sample_docs"]:
                print(f"  Sample affected documents:")
                for doc in item["sample_docs"][:5]:
                    print(f"    - {doc['_id']} (title: {doc['title']})")
            print()

    print("=" * 70)
    print(f"Summary: {total_orphans} orphan(s), {total_affected_docs} affected document(s)")
    print("=" * 70)


if __name__ == "__main__":
    main()
