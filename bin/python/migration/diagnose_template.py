#!/usr/bin/env python3
"""Diagnose which templates would match our migration index names"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.es_helpers import get_elasticsearch_client
import json


def check_template_match(es, index_name):
    """Check which templates would match an index name"""
    try:
        # Simulate index creation to see which templates would match
        result = es.indices.simulate_index_template(name=index_name)
        return result
    except Exception as e:
        print(f"Error checking template for {index_name}: {e}")
        return None


def main():
    es = get_elasticsearch_client()
    if not es:
        print("Failed to connect to Elasticsearch")
        return 1

    # Get the problematic template
    try:
        templates = es.indices.get_index_template(name="default_replicas_0_for_single_node")
        print("=== Template Configuration ===")
        print(json.dumps(templates, indent=2))
        print()
    except Exception as e:
        print(f"Could not fetch template: {e}")

    # Test various index name patterns
    test_names = [
        "example_ga_metrics",
        "migration-temp-example-20260205",
        "temp-migration-example-20260205",
        "migration_temp_20260205",
        "temp_migration_20260205",
        "xtemp20260205",
        "reindex_temp",
    ]

    print("=== Testing Index Name Patterns ===")
    for name in test_names:
        result = check_template_match(es, name)
        if result and "overlapping" in result:
            templates_matched = [t["name"] for t in result["overlapping"]]
            print(f"{name:40} -> Matches: {templates_matched}")
        elif result and "template" in result:
            print(f"{name:40} -> Would use template")
        else:
            print(f"{name:40} -> No template match")


if __name__ == "__main__":
    main()
