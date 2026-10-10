#!/usr/bin/env python3
"""
Step 4 — Index Parity Comparison
Compares actual ES index mappings against the expected schema JSON files.
Reports any missing, extra, or type-mismatched fields.

Usage: python3 /app/python/compare_index_parity.py
"""

import os
import sys
import json
import ssl
from elasticsearch import Elasticsearch


def get_es():
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ca = "/app/data/certs/es01_http_ca.crt"
    if not os.path.exists(ca):
        print(f"[ERROR] CA cert not found at {ca}")
        sys.exit(1)
    ctx.load_verify_locations(cafile=ca)
    es = Elasticsearch(
        [f"https://{os.environ['ES01_INTERNAL_IP']}:9200"],
        basic_auth=("elastic", os.environ["ES01_PASSWORD"]),
        ssl_context=ctx,
        ssl_show_warn=False,
        verify_certs=True,
    )
    if not es.ping():
        print("[ERROR] Cannot connect to Elasticsearch")
        sys.exit(1)
    return es


SCHEMAS_DIR = "/app/python/schemas"


def load_schema(filename):
    with open(os.path.join(SCHEMAS_DIR, filename)) as f:
        data = json.load(f)
    if "mappings" in data and "properties" not in data:
        return data["mappings"]
    return data


PARITY_CHECKS = {
    "test_fb_insights": "unified_schema_fb_insights.json",
    "test_ga_metrics": "unified_schema_ga_metrics.json",
    "test_echo_conversations": "unified_schema_echo_conversations.json",
    "test_ig_insights": "unified_schema_ig_insights.json",
    "test_fb_page_insights": "unified_schema_fb_page_insights.json",
    "test_youtube_analytics": "unified_schema_youtube_analytics.json",
}


def flatten_mapping(properties, prefix=""):
    """Flatten nested ES mapping properties into dot-notation field:type pairs."""
    fields = {}
    for field, config in properties.items():
        full_name = f"{prefix}{field}" if not prefix else f"{prefix}.{field}"
        if "type" in config:
            fields[full_name] = config["type"]
        if "properties" in config:
            fields.update(flatten_mapping(config["properties"], full_name))
    return fields


def compare(index_name, schema_file, es):
    schema_path = os.path.join(SCHEMAS_DIR, schema_file)

    print(f"\n{'─' * 60}")
    print(f"Index:  {index_name}")
    print(f"Schema: {schema_file}")

    # Load expected schema
    if not os.path.exists(schema_path):
        print(f"  [FAIL] Schema file not found: {schema_path}")
        return False

    expected_schema = load_schema(schema_file)
    expected_fields = flatten_mapping(expected_schema.get("properties", {}))

    # Get actual mapping from ES
    if not es.indices.exists(index=index_name):
        print(f"  [FAIL] Index does not exist in Elasticsearch")
        return False

    mapping_resp = es.indices.get_mapping(index=index_name)
    # ES 9.x client returns a typed response object; convert to dict
    mapping_body = mapping_resp.body if hasattr(mapping_resp, "body") else dict(mapping_resp)
    actual_props = mapping_body[index_name]["mappings"].get("properties", {})
    actual_fields = flatten_mapping(actual_props)

    # Compare
    expected_set = set(expected_fields.keys())
    actual_set = set(actual_fields.keys())

    missing = expected_set - actual_set
    extra = actual_set - expected_set
    type_mismatches = {f for f in expected_set & actual_set if expected_fields[f] != actual_fields[f]}

    passed = not missing and not type_mismatches
    status = "[PASS]" if passed else "[FAIL]"
    print(f"  Status:          {status}")
    print(f"  Expected fields: {len(expected_set)}")
    print(f"  Actual fields:   {len(actual_set)}")

    if missing:
        print(f"  Missing fields ({len(missing)}):")
        for f in sorted(missing):
            print(f"    - {f} (expected: {expected_fields[f]})")

    if extra:
        print(f"  Extra fields ({len(extra)}) [dynamic docs may add these]:")
        for f in sorted(extra)[:10]:
            print(f"    + {f}: {actual_fields[f]}")
        if len(extra) > 10:
            print(f"    ... and {len(extra)-10} more")

    if type_mismatches:
        print(f"  Type mismatches ({len(type_mismatches)}):")
        for f in sorted(type_mismatches):
            print(f"    ~ {f}: expected={expected_fields[f]}, actual={actual_fields[f]}")

    if passed:
        print(f"  All {len(expected_set)} expected fields present with correct types")

    return passed


def main():
    print("=" * 60)
    print("Index Parity Comparison")
    print("=" * 60)

    es = get_es()
    print(f"\n[OK] Connected to Elasticsearch\n")

    results = {}
    for index_name, schema_file in PARITY_CHECKS.items():
        results[index_name] = compare(index_name, schema_file, es)

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    all_passed = True
    for index_name, passed in results.items():
        mark = "[PASS]" if passed else "[FAIL]"
        print(f"  {mark}  {index_name}")
        if not passed:
            all_passed = False

    print(f"\n{'=' * 60}")
    if all_passed:
        print("All indices match their schema definitions.")
        print("Index parity with production schemas: CONFIRMED")
    else:
        print("Some indices have parity issues — review output above.")
    print("=" * 60)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
