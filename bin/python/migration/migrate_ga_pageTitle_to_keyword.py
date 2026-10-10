#!/usr/bin/env python3
"""
Migration script: Change pageTitle field from text to keyword in *ga_metrics indexes

This script:
1. Finds all indexes matching *ga_metrics pattern
2. Creates temporary indexes with updated schema (pageTitle as keyword)
3. Reindexes data from old to new indexes
4. Deletes old indexes and renames new ones

Usage:
    python migration/migrate_ga_pageTitle_to_keyword.py [--dry-run]

Options:
    --dry-run    Show what would be done without making changes
"""

import sys
import os
import argparse
import logging
from datetime import datetime

# Add parent directory to path to import lib modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.es_helpers import get_elasticsearch_client
from migration.schema_loader import get_schema_for_index_type

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_ga_metrics_indexes(es):
    """
    Get list of all indexes matching *ga_metrics pattern.

    Returns:
        list: Index names
    """
    try:
        response = es.cat.indices(index="*ga_metrics", format="json")
        indexes = [idx["index"] for idx in response]
        logger.info(f"Found {len(indexes)} ga_metrics indexes")
        return indexes
    except Exception as e:
        logger.error(f"Error fetching ga_metrics indexes: {e}")
        return []


def check_current_mapping(es, index_name):
    """
    Check the current mapping for pageTitle field.
    Handles both regular indexes and aliases.

    Returns:
        str: Current type ('text' or 'keyword') or None if field doesn't exist
    """
    try:
        mapping = es.indices.get_mapping(index=index_name)

        # The response might have the actual index name (not the alias)
        # So we need to get the first key from the response
        if mapping:
            actual_index_name = list(mapping.keys())[0]
            properties = mapping[actual_index_name]["mappings"]["properties"]

            if "pageTitle" in properties:
                return properties["pageTitle"].get("type", "unknown")
            else:
                logger.warning(f"Index {index_name} has no pageTitle field")
                return None
        else:
            logger.error(f"No mapping found for {index_name}")
            return None
    except Exception as e:
        logger.error(f"Error checking mapping for {index_name}: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return None


def get_document_count(es, index_name):
    """Get document count for an index."""
    try:
        count = es.count(index=index_name)
        return count["count"]
    except Exception as e:
        logger.error(f"Error counting documents in {index_name}: {e}")
        return 0


def create_temp_index(es, source_index, temp_index):
    """
    Create temporary index with updated schema.

    Args:
        es: Elasticsearch client
        source_index: Original index name
        temp_index: Temporary index name

    Returns:
        bool: True if successful
    """
    try:
        # Load updated schema (with pageTitle as keyword)
        mapping = get_schema_for_index_type("ga_metrics")

        # Create index with updated mapping
        es.indices.create(
            index=temp_index,
            body={"settings": {"number_of_replicas": 0}, "mappings": mapping},
        )
        logger.info(f"Created temporary index: {temp_index}")
        return True
    except Exception as e:
        logger.error(f"Error creating temp index {temp_index}: {e}")
        return False


def reindex_data(es, source_index, dest_index):
    """
    Reindex data from source to destination index.

    Args:
        es: Elasticsearch client
        source_index: Source index name
        dest_index: Destination index name

    Returns:
        bool: True if successful
    """
    try:
        logger.info(f"Reindexing {source_index} -> {dest_index}...")

        response = es.reindex(
            body={"source": {"index": source_index}, "dest": {"index": dest_index}},
            wait_for_completion=True,
            refresh=True,
        )

        if response.get("failures"):
            logger.error(f"Reindex had failures: {response['failures']}")
            return False

        logger.info(f"Reindexed {response['total']} documents")
        return True
    except Exception as e:
        logger.error(f"Error reindexing {source_index} to {dest_index}: {e}")
        return False


def verify_reindex(es, source_index, dest_index):
    """
    Verify that reindex was successful by comparing document counts.

    Returns:
        bool: True if counts match
    """
    source_count = get_document_count(es, source_index)
    dest_count = get_document_count(es, dest_index)

    if source_count == dest_count:
        logger.info(f"Verification passed: {source_count} documents in both indexes")
        return True
    else:
        logger.error(f"Verification failed: {source_index} has {source_count} docs, " f"{dest_index} has {dest_count} docs")
        return False


def swap_indexes(es, old_index, temp_index):
    """
    Replace old index with new one:
    1. Create final index with correct name
    2. Reindex from temp to final
    3. Delete old and temp indexes

    Args:
        es: Elasticsearch client
        old_index: Original index to replace
        temp_index: Temporary index with migrated data

    Returns:
        bool: True if successful
    """
    try:
        # Use unique prefix to avoid conflicts with existing indexes
        import random

        random_suffix = "".join([str(random.randint(0, 9)) for _ in range(8)])
        final_index = f"xmig_final_{random_suffix}"

        # Create final index with correct mapping
        logger.info(f"Creating final index: {final_index}")
        mapping = get_schema_for_index_type("ga_metrics")
        es.indices.create(
            index=final_index,
            body={"settings": {"number_of_replicas": 0}, "mappings": mapping},
        )

        # Reindex from temp to final
        logger.info(f"Reindexing {temp_index} -> {final_index}")
        es.reindex(
            body={"source": {"index": temp_index}, "dest": {"index": final_index}},
            wait_for_completion=True,
            refresh=True,
        )

        # Verify final index
        if not verify_reindex(es, temp_index, final_index):
            logger.error("Final reindex verification failed")
            return False

        # Delete old index
        logger.info(f"Deleting old index: {old_index}")
        es.indices.delete(index=old_index)

        # Delete temp index
        logger.info(f"Deleting temp index: {temp_index}")
        es.indices.delete(index=temp_index)

        # Create alias from final to original name
        logger.info(f"Creating alias {old_index} -> {final_index}")
        es.indices.put_alias(index=final_index, name=old_index)

        logger.info(f"Migration complete: queries to '{old_index}' will use '{final_index}'")
        return True
    except Exception as e:
        logger.error(f"Error swapping indexes: {e}")
        return False


def migrate_index(es, index_name, dry_run=False):
    """
    Migrate a single index.

    Args:
        es: Elasticsearch client
        index_name: Index to migrate
        dry_run: If True, only show what would be done

    Returns:
        bool: True if successful (or would be successful in dry-run mode)
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"Processing index: {index_name}")
    logger.info(f"{'='*60}")

    # Check current mapping
    current_type = check_current_mapping(es, index_name)
    if current_type is None:
        logger.warning(f"Skipping {index_name} - no pageTitle field")
        return False

    if current_type == "keyword":
        logger.info(f"Index {index_name} already has pageTitle as keyword - skipping")
        return True

    doc_count = get_document_count(es, index_name)
    logger.info(f"Current pageTitle type: {current_type}")
    logger.info(f"Document count: {doc_count}")

    if dry_run:
        logger.info("[DRY RUN] Would create temp index and reindex data")
        return True

    # Create temporary index name
    # Use unique prefix to avoid conflicts with existing indexes
    import random

    random_suffix = "".join([str(random.randint(0, 9)) for _ in range(8)])
    temp_index = f"xmig_temp_{random_suffix}"

    # Step 1: Create temporary index with new schema
    if not create_temp_index(es, index_name, temp_index):
        return False

    # Step 2: Reindex data
    if not reindex_data(es, index_name, temp_index):
        # Clean up temp index on failure
        try:
            es.indices.delete(index=temp_index)
        except:
            pass
        return False

    # Step 3: Verify reindex
    if not verify_reindex(es, index_name, temp_index):
        logger.error("Reindex verification failed - keeping both indexes for manual inspection")
        return False

    # Step 4: Swap indexes
    if not swap_indexes(es, index_name, temp_index):
        return False

    logger.info(f"✓ Successfully migrated {index_name}")
    return True


def create_migration_template(es):
    """
    Create a higher-priority template for migration indices.
    This allows regular index creation instead of forcing data streams.
    """
    template_name = "migration_indices_override"

    template_body = {
        # Match our unique migration index patterns
        # Using xmig_ prefix to avoid conflicts with existing indexes
        "index_patterns": ["xmig_temp_*", "xmig_final_*"],
        "priority": 200,  # Higher than default_replicas_0_for_single_node (priority 1)
        "template": {"settings": {"number_of_shards": 1, "number_of_replicas": 0}},
        # NOTE: No data_stream block - this allows regular index creation
        "_meta": {"description": "Override template for migration indices - prevents data stream creation"},
    }

    try:
        es.indices.put_index_template(name=template_name, body=template_body)
        logger.info(f"✓ Created high-priority template '{template_name}' to override data stream requirement")
        return True
    except Exception as e:
        logger.error(f"Failed to create template: {e}")
        return False


def main():
    """Main migration function."""
    parser = argparse.ArgumentParser(description="Migrate pageTitle field from text to keyword in *ga_metrics indexes")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN MODE - No changes will be made ===\n")

    # Connect to Elasticsearch
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Failed to connect to Elasticsearch")
        return 1

    # Create high-priority template to override data stream requirement
    logger.info("Setting up migration template...")
    if not create_migration_template(es):
        logger.error("Failed to create migration template - migration may fail")
        if not args.dry_run:
            logger.error("Aborting migration")
            return 1

    # Get all ga_metrics indexes
    indexes = get_ga_metrics_indexes(es)
    if not indexes:
        logger.info("No ga_metrics indexes found")
        return 0

    # Migrate each index
    success_count = 0
    failure_count = 0
    skipped_count = 0

    for index_name in indexes:
        try:
            result = migrate_index(es, index_name, dry_run=args.dry_run)
            if result:
                # Check if it was actually migrated or just already correct
                current_type = check_current_mapping(es, index_name)
                if current_type == "keyword":
                    success_count += 1
                else:
                    skipped_count += 1
            else:
                failure_count += 1
        except Exception as e:
            logger.error(f"Unexpected error migrating {index_name}: {e}")
            failure_count += 1

    # Summary
    logger.info(f"\n{'='*60}")
    logger.info("Migration Summary")
    logger.info(f"{'='*60}")
    logger.info(f"Total indexes: {len(indexes)}")
    logger.info(f"Successfully migrated: {success_count}")
    logger.info(f"Already correct (skipped): {skipped_count}")
    logger.info(f"Failed: {failure_count}")

    if args.dry_run:
        logger.info("\nThis was a dry run. Run without --dry-run to apply changes.")

    return 0 if failure_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
