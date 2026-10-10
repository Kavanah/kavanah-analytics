#!/usr/bin/env python3
"""
Cleanup Script: Delete unified indices after migration to team-specific indices

This script deletes the old unified indices after confirming that data has been
successfully migrated to team-specific indices.

⚠️  WARNING: This operation is DESTRUCTIVE and cannot be undone!

Usage:
    python3 cleanup_unified_indices.py [data_source] [--force]

    data_source options: facebook, instagram, google_analytics, echo, all
    --force: Skip confirmation prompts (use with caution!)

Examples:
    python3 cleanup_unified_indices.py facebook
    python3 cleanup_unified_indices.py all --force
"""

import sys
import os
import logging

# Add parent directory to path to import lib modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def verify_before_delete(es, unified_index, team_pattern):
    """Verify that team-specific indices exist before deleting unified index.

    Args:
        es: Elasticsearch client
        unified_index: Unified index name to delete
        team_pattern: Pattern for team-specific indices

    Returns:
        tuple: (bool: safe_to_delete, int: unified_count, int: team_count)
    """
    # Check if unified index exists
    if not es.indices.exists(index=unified_index):
        logging.info(f"Unified index '{unified_index}' does not exist. Nothing to delete.")
        return False, 0, 0

    # Count documents in unified index
    unified_count = es.count(index=unified_index)["count"]

    # Check if any team-specific indices exist
    try:
        team_indices = es.indices.get(index=team_pattern)
        # Filter out the unified index itself if it matches the pattern
        team_indices = {k: v for k, v in team_indices.items() if k != unified_index}
    except Exception:
        team_indices = {}

    if not team_indices:
        logging.error(f"No team-specific indices found matching pattern '{team_pattern}'")
        logging.error(f"Cannot delete unified index '{unified_index}' - no migration target exists!")
        return False, unified_count, 0

    # Count total documents in team indices
    total_team_docs = 0
    for idx in team_indices.keys():
        count = es.count(index=idx)["count"]
        total_team_docs += count

    logging.info(f"Unified index '{unified_index}': {unified_count} documents")
    logging.info(f"Team-specific indices: {len(team_indices)} indices with {total_team_docs} total documents")

    if unified_count == 0:
        logging.info("✓ Unified index is empty. Safe to delete.")
        return True, unified_count, total_team_docs

    if total_team_docs >= unified_count:
        logging.info("✓ Team indices have equal or more documents. Safe to delete.")
        return True, unified_count, total_team_docs

    logging.warning("⚠ Team indices have FEWER documents than unified index!")
    logging.warning(f"  Unified: {unified_count}, Team: {total_team_docs}")
    logging.warning("  This may indicate incomplete migration.")

    return False, unified_count, total_team_docs


def delete_index(es, index_name, force=False):
    """Delete an Elasticsearch index with confirmation.

    Args:
        es: Elasticsearch client
        index_name: Index to delete
        force: Skip confirmation if True

    Returns:
        bool: True if deleted, False otherwise
    """
    if not es.indices.exists(index=index_name):
        logging.info(f"Index '{index_name}' does not exist. Skipping.")
        return False

    if not force:
        response = input(f"\n⚠️  DELETE index '{index_name}'? This cannot be undone! (yes/no): ")
        if response.lower() != "yes":
            logging.info(f"Deletion cancelled for '{index_name}'")
            return False

    logging.info(f"Deleting index '{index_name}'...")
    try:
        es.indices.delete(index=index_name)
        logging.info(f"✓ Successfully deleted '{index_name}'")
        return True
    except Exception as e:
        logging.error(f"Failed to delete '{index_name}': {e}")
        return False


def cleanup_data_source(data_source, force=False):
    """Cleanup unified index for a specific data source.

    Args:
        data_source: Data source name (facebook, instagram, etc.)
        force: Skip confirmation prompts

    Returns:
        bool: True if successful
    """
    es = get_elasticsearch_client()

    # Map data source to unified and team index patterns
    index_mappings = {
        "facebook": ("fb_insights", "*_fb_insights"),
        "instagram": ("ig_insights", "*_ig_insights"),
        "google_analytics": ("ga_metrics", "*_ga_metrics"),
        "echo": ("echo_conversations", "*_echo_conversations"),
    }

    if data_source not in index_mappings:
        logging.error(f"Unknown data source: {data_source}")
        return False

    unified_index, team_pattern = index_mappings[data_source]

    logging.info("=" * 70)
    logging.info(f"CLEANUP: {data_source.upper()}")
    logging.info("=" * 70)

    # Verify before deleting
    safe, unified_count, team_count = verify_before_delete(es, unified_index, team_pattern)

    if not safe and not force:
        logging.error(f"❌ Skipping deletion of '{unified_index}' - verification failed")
        logging.error("   Use --force to override (not recommended)")
        return False

    if not safe and force:
        logging.warning(f"⚠️  Force flag set - deleting '{unified_index}' despite verification failure")

    # Delete the unified index
    return delete_index(es, unified_index, force=force)


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    args = sys.argv[1:]
    force = "--force" in args

    # Remove --force from args to get data source
    data_sources = [arg for arg in args if arg != "--force"]

    if not data_sources:
        print("Error: No data source specified")
        print(__doc__)
        sys.exit(1)

    data_source = data_sources[0].lower()

    if data_source not in ["facebook", "instagram", "google_analytics", "echo", "all"]:
        print(f"Error: Invalid data source '{data_source}'")
        print(__doc__)
        sys.exit(1)

    logging.info("=" * 70)
    logging.info("UNIFIED INDEX CLEANUP")
    logging.info("=" * 70)
    logging.info(f"Data source: {data_source}")
    if force:
        logging.warning("⚠️  FORCE MODE ENABLED - Skipping confirmations")
    logging.info("")

    success_count = 0
    total_count = 0

    sources = ["facebook", "instagram", "google_analytics", "echo"] if data_source == "all" else [data_source]

    for source in sources:
        total_count += 1
        if cleanup_data_source(source, force=force):
            success_count += 1
        logging.info("")

    logging.info("=" * 70)
    logging.info(f"CLEANUP COMPLETE: {success_count}/{total_count} indices deleted")
    logging.info("=" * 70)

    if success_count < total_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
