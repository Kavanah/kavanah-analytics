#!/usr/bin/env python3
"""
One-time migration: YAML property mappings → ES assets

Migrates property_team_mapping from google-analytics.yaml to Elasticsearch assets
in de_asset_ga_properties index.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from google_analytics.lib.ga_config import GAConfig
from google_analytics.lib.assets import get_ga_properties
from lib.es_helpers import get_elasticsearch_client
from datetime import datetime
import hashlib
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate_yaml_to_assets():
    """Migrate property_team_mapping from YAML to ES assets."""

    print("\n" + "=" * 70)
    print("Google Analytics YAML → ES Assets Migration")
    print("=" * 70)

    # Load config
    try:
        config = GAConfig()
        logger.info("Loaded configuration from YAML")
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        return

    # Get ES client
    try:
        es_client = get_elasticsearch_client()
        logger.info("Connected to Elasticsearch")
    except Exception as e:
        logger.error(f"Failed to connect to Elasticsearch: {e}")
        return

    # Check if there are mappings to migrate
    if not config.property_team_mapping:
        print("\nNo property mappings found in YAML configuration.")
        print("Nothing to migrate.")
        return

    print(f"\nFound {len(config.property_team_mapping)} property mappings in YAML\n")

    created_count = 0
    updated_count = 0
    error_count = 0

    for property_id, team_data in config.property_team_mapping.items():
        print(f"Processing property {property_id}...")

        # Check if property already exists in ES
        existing = get_ga_properties(filters={"ga_property_id": property_id})

        try:
            if existing:
                # Update team assignment
                doc_id = existing[0]["_id"]
                es_client.update(
                    index="de_asset_ga_properties",
                    id=doc_id,
                    body={
                        "doc": {
                            "team_esid": team_data.get("team_esid"),
                            "team_title": team_data.get("team_title"),
                        }
                    },
                )
                print(f"  ✓ Updated property {property_id} → team {team_data.get('team_esid')}")
                updated_count += 1
            else:
                # Create new property asset
                doc_id = hashlib.md5(  # nosemgrep: insecure-hash-algorithm-md5 - non-cryptographic doc ID fingerprint
                    f"ga_property_{property_id}".encode()
                ).hexdigest()
                doc = {
                    "ga_property_id": property_id,
                    "ga_property_resource_name": f"properties/{property_id}",
                    "ga_property_title": f"Property {property_id}",
                    "ga_property_display_name": f"Property {property_id}",
                    "ga_account_id": None,
                    "ga_account_esid": None,
                    "ga_account_title": None,
                    "team_esid": team_data.get("team_esid"),
                    "team_title": team_data.get("team_title"),
                }
                es_client.index(index="de_asset_ga_properties", id=doc_id, document=doc)
                print(f"  ✓ Created property {property_id} → team {team_data.get('team_esid')}")
                created_count += 1

        except Exception as e:
            logger.error(f"Failed to migrate property {property_id}: {e}")
            print(f"  ✗ Failed: {e}")
            error_count += 1

    # Summary
    print("\n" + "=" * 70)
    print("Migration Summary")
    print("=" * 70)
    print(f"Properties created: {created_count}")
    print(f"Properties updated: {updated_count}")
    print(f"Errors: {error_count}")
    print(f"Total processed: {len(config.property_team_mapping)}")
    print("=" * 70)

    if error_count == 0:
        print("\n✓ Migration completed successfully!")
        print("\nNext steps:")
        print("  1. Verify mappings:")
        print("     python3 /app/python/asset-management.py")
        print("     → Navigate to: Google Ads Assets → Google Analytics Properties")
        print("  2. Test imports:")
        print("     python3 /app/python/import.py google_analytics run_daily_imports --days_to_pull=1")
        print("  3. Check logs for 'Found team mapping in ES assets'")
        print("  4. After validation, remove property_team_mapping from YAML config")
        print("     (Keep YAML as fallback during transition period)")
    else:
        print(f"\n⚠ Migration completed with {error_count} errors")
        print("  Review errors above and retry migration for failed properties")

    print()


if __name__ == "__main__":
    migrate_yaml_to_assets()
