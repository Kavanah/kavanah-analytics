#!/usr/bin/env python3
"""
Create a higher-priority template for migration indices to override data stream requirement.

This template matches migration index patterns with priority 200 (higher than the default
template's priority of 1) and does NOT include a data_stream block, allowing regular
index creation.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.es_helpers import get_elasticsearch_client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_migration_template(es_client):
    """Create high-priority template for migration indices."""

    template_name = "migration_indices_override"

    template_body = {
        # Match our unique migration index patterns
        # Using xmig_ prefix to avoid conflicts with existing indexes
        "index_patterns": ["xmig_temp_*", "xmig_final_*"],
        "priority": 200,  # Higher than default_replicas_0_for_single_node (priority 1)
        "template": {"settings": {"number_of_shards": 1, "number_of_replicas": 0}},
        # NOTE: No data_stream block - this allows regular index creation
        "_meta": {"description": "Override template for migration/reindex indices - prevents data stream creation"},
    }

    try:
        # Check if template exists
        try:
            existing = es_client.indices.get_index_template(name=template_name)
            logger.info(f"Template {template_name} already exists, updating...")
        except:
            logger.info(f"Creating new template {template_name}...")

        # Create or update the template
        es_client.indices.put_index_template(name=template_name, body=template_body)
        logger.info(f"✓ Template {template_name} created/updated successfully")

        return True
    except Exception as e:
        logger.error(f"Failed to create template: {e}")
        return False


def main():
    """Main function."""

    print("\n" + "=" * 70)
    print("Migration Indices Template Setup")
    print("=" * 70 + "\n")

    try:
        es_client = get_elasticsearch_client()
        logger.info("Connected to Elasticsearch")

        # Create the override template
        if create_migration_template(es_client):
            print("\n✓ Migration indices template created successfully!")
            print("\nThis template:")
            print("  - Matches patterns: xmig_temp_*, xmig_final_*")
            print("  - Priority: 200 (overrides default template)")
            print("  - Allows regular index creation (no data streams)")
            print("\nNow you can run the migration:")
            print("  python3 /app/python/migration/migrate_ga_pageTitle_to_keyword.py")
        else:
            print("\n✗ Failed to create template")
            print("See error messages above")

        print("=" * 70 + "\n")

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    main()
