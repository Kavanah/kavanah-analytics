#!/usr/bin/env python3
"""
Create Elasticsearch indices for Google Analytics asset management.

Creates:
- de_asset_ga_accounts: GA4 Account-level assets
- de_asset_ga_properties: GA4 Property-level assets with team relationships
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from lib.es_helpers import get_elasticsearch_client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_ga_accounts_index(es_client):
    """Create de_asset_ga_accounts index with proper mapping."""

    index_name = "de_asset_ga_accounts"

    # Settings to prevent data stream creation and ensure regular index
    # NOTE: No @timestamp field - asset indices are entity data, not time-series
    settings = {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {
            "properties": {
                "ga_account_id": {"type": "keyword"},
                "ga_account_resource_name": {"type": "keyword"},
                "ga_account_title": {"type": "text"},
                "ga_account_description": {"type": "text"},
            }
        },
    }

    # Check if index already exists
    if es_client.indices.exists(index=index_name):
        logger.warning(f"Index {index_name} already exists. Skipping creation.")
        return False

    try:
        # Create the index with explicit settings to override template
        es_client.indices.create(index=index_name, body=settings)
        logger.info(f"✓ Created index: {index_name}")
        return True
    except Exception as e:
        # If template forces data stream, we need to work around it
        if "creates data streams only" in str(e):
            logger.error(f"Template conflict detected for {index_name}")
            logger.error("The Elasticsearch template 'default_replicas_0_for_single_node' is forcing data stream creation")
            logger.error("Solution: Asset indices should be regular indices, not data streams")
            logger.error("Workaround: Create index without @timestamp field or modify template")
            raise
        else:
            raise


def create_ga_properties_index(es_client):
    """Create de_asset_ga_properties index with proper mapping."""

    index_name = "de_asset_ga_properties"

    # Settings to prevent data stream creation and ensure regular index
    # NOTE: No @timestamp field - asset indices are entity data, not time-series
    settings = {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
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
            }
        },
    }

    # Check if index already exists
    if es_client.indices.exists(index=index_name):
        logger.warning(f"Index {index_name} already exists. Skipping creation.")
        return False

    try:
        # Create the index with explicit settings to override template
        es_client.indices.create(index=index_name, body=settings)
        logger.info(f"✓ Created index: {index_name}")
        return True
    except Exception as e:
        # If template forces data stream, we need to work around it
        if "creates data streams only" in str(e):
            logger.error(f"Template conflict detected for {index_name}")
            logger.error("The Elasticsearch template 'default_replicas_0_for_single_node' is forcing data stream creation")
            logger.error("Solution: Asset indices should be regular indices, not data streams")
            logger.error("Workaround: Create index without @timestamp field or modify template")
            raise
        else:
            raise


def main():
    """Main function to create all GA asset indices."""

    logger.info("Starting Google Analytics asset indices creation...")

    try:
        # Get Elasticsearch client
        es_client = get_elasticsearch_client()
        logger.info("Connected to Elasticsearch")

        # Create indices
        accounts_created = create_ga_accounts_index(es_client)
        properties_created = create_ga_properties_index(es_client)

        # Summary
        print("\n" + "=" * 60)
        print("Google Analytics Asset Indices Setup Complete")
        print("=" * 60)
        print(f"de_asset_ga_accounts: {'Created' if accounts_created else 'Already exists'}")
        print(f"de_asset_ga_properties: {'Created' if properties_created else 'Already exists'}")
        print("\nRelationships:")
        print("  de_asset_ga_properties.ga_account_esid → de_asset_ga_accounts._id")
        print("  de_asset_ga_properties.team_esid → de_asset_teams._id")
        print("  de_asset_teams.ga_property_esid → de_asset_ga_properties._id")
        print("\nNext steps:")
        print("  1. Run export_properties.py to auto-populate assets")
        print("  2. Use asset-management.py to assign properties to teams")
        print("  3. Run migrate_yaml_to_assets.py for existing YAML mappings")
        print("=" * 60 + "\n")

    except Exception as e:
        logger.error(f"Error creating indices: {e}")
        raise


if __name__ == "__main__":
    main()
