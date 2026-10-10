#!/usr/bin/env python3
"""
Fix Google Analytics asset indices creation issue.

The default_replicas_0_for_single_node template is interfering with asset index creation.
This script checks existing asset indices and creates GA asset indices using the same method.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from lib.es_helpers import get_elasticsearch_client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def check_existing_assets(es_client):
    """Check how existing asset indices were created."""
    try:
        # Get list of existing asset indices
        indices = es_client.cat.indices(index="de_asset_*", format="json", h="index,docs.count")

        print("\n" + "=" * 70)
        print("Existing Asset Indices")
        print("=" * 70)
        for idx in indices:
            print(f"  {idx['index']}: {idx['docs.count']} documents")
        print("=" * 70 + "\n")

        # Check if they're data streams or regular indices
        if indices:
            sample_index = indices[0]["index"]
            mapping = es_client.indices.get_mapping(index=sample_index)
            logger.info(f"Sample mapping from {sample_index}:")
            logger.info(f"  Has @timestamp: {'@timestamp' in str(mapping)}")

        return indices
    except Exception as e:
        logger.error(f"Error checking existing indices: {e}")
        return []


def create_index_directly(es_client, index_name, mappings):
    """Create index using the same method as other asset indices."""

    # Check if already exists
    if es_client.indices.exists(index=index_name):
        logger.info(f"✓ Index {index_name} already exists")
        return True

    # Try creating without explicit settings (let ES use defaults)
    body = {"mappings": {"properties": mappings}}

    try:
        es_client.indices.create(index=index_name, body=body)
        logger.info(f"✓ Created index: {index_name}")
        return True
    except Exception as e:
        if "creates data streams only" in str(e):
            logger.warning(f"Template conflict for {index_name}")
            logger.warning("Attempting workaround: creating with index.mode=standard")

            # Try with explicit mode to override template
            body_with_settings = {
                "settings": {"index": {"mode": "standard"}},
                "mappings": {"properties": mappings},
            }

            try:
                es_client.indices.create(index=index_name, body=body_with_settings)
                logger.info(f"✓ Created index with workaround: {index_name}")
                return True
            except Exception as e2:
                logger.error(f"✗ Failed to create {index_name}: {e2}")
                return False
        else:
            logger.error(f"✗ Failed to create {index_name}: {e}")
            return False


def main():
    """Main function."""

    print("\n" + "=" * 70)
    print("Google Analytics Asset Indices Setup")
    print("=" * 70 + "\n")

    try:
        es_client = get_elasticsearch_client()
        logger.info("Connected to Elasticsearch")

        # Check existing asset indices
        check_existing_assets(es_client)

        # Define GA account mappings
        ga_accounts_mappings = {
            "ga_account_id": {"type": "keyword"},
            "ga_account_resource_name": {"type": "keyword"},
            "ga_account_title": {"type": "text"},
            "ga_account_description": {"type": "text"},
            "@timestamp": {"type": "date"},
        }

        # Define GA properties mappings
        ga_properties_mappings = {
            "ga_property_id": {"type": "keyword"},
            "ga_property_resource_name": {"type": "keyword"},
            "ga_property_title": {"type": "text"},
            "ga_property_display_name": {"type": "text"},
            "ga_account_id": {"type": "keyword"},
            "ga_account_esid": {"type": "keyword"},
            "ga_account_title": {"type": "text"},
            "team_esid": {"type": "keyword"},
            "team_title": {"type": "keyword"},
            "@timestamp": {"type": "date"},
        }

        print("Creating GA asset indices...\n")

        # Create indices
        accounts_ok = create_index_directly(es_client, "de_asset_ga_accounts", ga_accounts_mappings)
        properties_ok = create_index_directly(es_client, "de_asset_ga_properties", ga_properties_mappings)

        # Summary
        print("\n" + "=" * 70)
        print("Setup Summary")
        print("=" * 70)
        print(f"de_asset_ga_accounts: {'✓ Ready' if accounts_ok else '✗ Failed'}")
        print(f"de_asset_ga_properties: {'✓ Ready' if properties_ok else '✗ Failed'}")

        if accounts_ok and properties_ok:
            print("\n✓ All indices created successfully!")
            print("\nNext steps:")
            print("  1. Run: python3 /app/python/import.py google_analytics export_properties")
            print("  2. Run: python3 /app/python/import_scripts/google_analytics/migrate_yaml_to_assets.py")
        else:
            print("\n⚠ Some indices failed to create")
            print("Manual workaround: Create indices via Kibana Dev Tools Console")

        print("=" * 70 + "\n")

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    main()
