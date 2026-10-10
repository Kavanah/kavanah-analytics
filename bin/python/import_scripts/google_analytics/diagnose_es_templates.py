#!/usr/bin/env python3
"""
Diagnose Elasticsearch template and index configuration.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from lib.es_helpers import get_elasticsearch_client
import json


def main():
    es_client = get_elasticsearch_client()

    print("\n" + "=" * 70)
    print("Elasticsearch Configuration Diagnosis")
    print("=" * 70 + "\n")

    # Check existing asset indices
    print("1. EXISTING ASSET INDICES")
    print("-" * 70)
    try:
        indices = es_client.cat.indices(index="de_asset_*", format="json", h="index,docs.count")
        for idx in indices:
            print(f"  {idx['index']}: {idx.get('docs.count', 0)} docs")

            # Check if it's a data stream backing index
            index_name = idx["index"]
            if index_name.startswith(".ds-"):
                print(f"    └─ DATA STREAM BACKING INDEX")
            else:
                print(f"    └─ REGULAR INDEX")
    except Exception as e:
        print(f"  Error: {e}")

    # Check data streams
    print("\n2. EXISTING DATA STREAMS")
    print("-" * 70)
    try:
        streams = es_client.indices.get_data_stream(name="de_asset_*")
        if streams.get("data_streams"):
            for stream in streams["data_streams"]:
                print(f"  {stream['name']}")
        else:
            print("  No data streams found matching de_asset_*")
    except Exception as e:
        print(f"  No data streams found or error: {e}")

    # Check templates
    print("\n3. INDEX TEMPLATES")
    print("-" * 70)
    try:
        templates = es_client.indices.get_index_template()
        for template in templates.get("index_templates", []):
            name = template["name"]
            patterns = template["index_template"].get("index_patterns", [])
            has_data_stream = "data_stream" in template["index_template"]

            # Check if template matches de_asset_*
            matches_assets = any("de_asset" in p or "*" in p for p in patterns)

            if matches_assets or has_data_stream:
                print(f"\n  Template: {name}")
                print(f"    Patterns: {patterns}")
                print(f"    Data Stream: {has_data_stream}")
                print(f"    Priority: {template['index_template'].get('priority', 0)}")

                if has_data_stream:
                    print(f"    ⚠ This template FORCES data stream creation")
    except Exception as e:
        print(f"  Error: {e}")

    # Solution
    print("\n" + "=" * 70)
    print("SOLUTION")
    print("=" * 70)
    print(
        """
The template 'default_replicas_0_for_single_node' is forcing ALL indices
to be data streams. Asset indices should be regular indices.

RECOMMENDED FIX: Modify the template to exclude asset indices

Run this in Kibana Dev Tools Console:

GET _index_template/default_replicas_0_for_single_node

Then update it to exclude de_asset_* pattern, or lower its priority.

ALTERNATIVE: Create indices via Kibana Console without using Python API:

PUT /de_asset_ga_accounts
{
  "settings": {
    "number_of_shards": 1,
    "number_of_replicas": 0
  },
  "mappings": {
    "properties": {
      "ga_account_id": {"type": "keyword"},
      "ga_account_resource_name": {"type": "keyword"},
      "ga_account_title": {"type": "text"},
      "ga_account_description": {"type": "text"},
      "@timestamp": {"type": "date"}
    }
  }
}

PUT /de_asset_ga_properties
{
  "settings": {
    "number_of_shards": 1,
    "number_of_replicas": 0
  },
  "mappings": {
    "properties": {
      "ga_property_id": {"type": "keyword"},
      "ga_property_resource_name": {"type": "keyword"},
      "ga_property_title": {"type": "text"},
      "ga_property_display_name": {"type": "text"},
      "ga_account_id": {"type": "keyword"},
      "ga_account_esid": {"type": "keyword"},
      "ga_account_title": {"type": "text"},
      "team_esid": {"type": "keyword"},
      "team_title": {"type": "keyword"},
      "@timestamp": {"type": "date"}
    }
  }
}
"""
    )
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
