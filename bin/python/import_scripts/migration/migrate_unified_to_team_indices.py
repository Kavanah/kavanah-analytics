#!/usr/bin/env python3
"""
Migration Script: Move data from unified indices to team-specific indices

This script migrates data from unified indices (fb_insights, ig_insights, ga_metrics,
echo_conversations) to team-specific indices (e.g., kavanah_media_fb_insights).

Usage:
    python3 migrate_unified_to_team_indices.py [data_source]

    data_source options: facebook, instagram, google_analytics, echo, all

Examples:
    python3 migrate_unified_to_team_indices.py facebook
    python3 migrate_unified_to_team_indices.py all
"""

import sys
import os
import logging
from collections import defaultdict

# Add parent directory to path to import lib modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import (
    create_index_if_not_exists,
    get_elasticsearch_client,
    sanitize_team_title,
)
from migration.schema_loader import get_schema_for_index_type

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# the local sanitize_team_title copy was retired — this script now
# uses the canonical implementation in lib.es_helpers (byte-identical logic).


def migrate_facebook():
    """Migrate Facebook data from fb_insights to team-specific indices."""
    logging.info("=" * 70)
    logging.info("MIGRATING FACEBOOK ADS DATA")
    logging.info("=" * 70)

    es = get_elasticsearch_client()
    unified_index = "fb_insights"

    # Check if unified index exists
    if not es.indices.exists(index=unified_index):
        logging.warning(f"Unified index '{unified_index}' does not exist. Skipping.")
        return

    # Get document count
    count_result = es.count(index=unified_index)
    total_docs = count_result["count"]
    logging.info(f"Found {total_docs} documents in {unified_index}")

    if total_docs == 0:
        logging.info("No documents to migrate. Skipping.")
        return

    # Get schema
    schema = get_schema_for_index_type("fb_insights")

    # Group documents by team
    team_docs = defaultdict(list)

    # Scroll through all documents
    logging.info("Grouping documents by team...")
    scroll_result = es.search(index=unified_index, scroll="5m", size=1000, body={"query": {"match_all": {}}})

    scroll_id = scroll_result["_scroll_id"]
    hits = scroll_result["hits"]["hits"]
    processed = 0

    while hits:
        for hit in hits:
            doc = hit["_source"]
            team_title = doc.get("team_title", "unknown")
            sanitized = sanitize_team_title(team_title)
            team_docs[sanitized].append(doc)
            processed += 1

        if processed % 1000 == 0:
            logging.info(f"  Processed {processed}/{total_docs} documents...")

        scroll_result = es.scroll(scroll_id=scroll_id, scroll="5m")
        scroll_id = scroll_result["_scroll_id"]
        hits = scroll_result["hits"]["hits"]

    # Clear scroll
    es.clear_scroll(scroll_id=scroll_id)

    logging.info(f"Grouped into {len(team_docs)} teams")

    # Create team-specific indices and reindex
    for team_sanitized, docs in team_docs.items():
        team_index = f"{team_sanitized}_fb_insights"
        logging.info(f"Creating index: {team_index} ({len(docs)} documents)")

        # Create index using helper function
        if not es.indices.exists(index=team_index):
            # refuse rather than let the bulk below auto-create the
            # index under dynamic mapping (the drift).
            if not create_index_if_not_exists(es, team_index, schema):
                raise RuntimeError(
                    f"failed to create {team_index} with its schema mapping — refusing to bulk-write into it"
                )

        # Bulk index documents
        from elasticsearch.helpers import bulk

        actions = [{"_index": team_index, "_source": doc} for doc in docs]

        success, failed = bulk(es, actions)
        logging.info(f"  Indexed {success} documents to {team_index}")
        if failed:
            logging.error(f"  Failed to index {failed} documents")

    logging.info(f"✓ Facebook migration complete: {processed} documents migrated")
    return processed


def migrate_instagram():
    """Migrate Instagram data from ig_insights to team-specific indices."""
    logging.info("=" * 70)
    logging.info("MIGRATING INSTAGRAM DATA")
    logging.info("=" * 70)

    es = get_elasticsearch_client()
    unified_index = "ig_insights"

    if not es.indices.exists(index=unified_index):
        logging.warning(f"Unified index '{unified_index}' does not exist. Skipping.")
        return

    count_result = es.count(index=unified_index)
    total_docs = count_result["count"]
    logging.info(f"Found {total_docs} documents in {unified_index}")

    if total_docs == 0:
        logging.info("No documents to migrate. Skipping.")
        return

    schema = get_schema_for_index_type("ig_insights")
    team_docs = defaultdict(list)

    logging.info("Grouping documents by team...")
    scroll_result = es.search(index=unified_index, scroll="5m", size=1000, body={"query": {"match_all": {}}})

    scroll_id = scroll_result["_scroll_id"]
    hits = scroll_result["hits"]["hits"]
    processed = 0

    while hits:
        for hit in hits:
            doc = hit["_source"]
            team_title = doc.get("team_title", "unknown")
            sanitized = sanitize_team_title(team_title)
            team_docs[sanitized].append(doc)
            processed += 1

        if processed % 1000 == 0:
            logging.info(f"  Processed {processed}/{total_docs} documents...")

        scroll_result = es.scroll(scroll_id=scroll_id, scroll="5m")
        scroll_id = scroll_result["_scroll_id"]
        hits = scroll_result["hits"]["hits"]

    es.clear_scroll(scroll_id=scroll_id)

    logging.info(f"Grouped into {len(team_docs)} teams")

    for team_sanitized, docs in team_docs.items():
        team_index = f"{team_sanitized}_ig_insights"
        logging.info(f"Creating index: {team_index} ({len(docs)} documents)")

        if not es.indices.exists(index=team_index):
            # refuse rather than let the bulk below auto-create the
            # index under dynamic mapping (the drift).
            if not create_index_if_not_exists(es, team_index, schema):
                raise RuntimeError(
                    f"failed to create {team_index} with its schema mapping — refusing to bulk-write into it"
                )

        from elasticsearch.helpers import bulk

        actions = [{"_index": team_index, "_source": doc} for doc in docs]

        success, failed = bulk(es, actions)
        logging.info(f"  Indexed {success} documents to {team_index}")
        if failed:
            logging.error(f"  Failed to index {failed} documents")

    logging.info(f"✓ Instagram migration complete: {processed} documents migrated")
    return processed


def migrate_google_analytics():
    """Migrate Google Analytics data from ga_metrics to team-specific indices."""
    logging.info("=" * 70)
    logging.info("MIGRATING GOOGLE ANALYTICS DATA")
    logging.info("=" * 70)

    es = get_elasticsearch_client()
    unified_index = "ga_metrics"

    if not es.indices.exists(index=unified_index):
        logging.warning(f"Unified index '{unified_index}' does not exist. Skipping.")
        return

    count_result = es.count(index=unified_index)
    total_docs = count_result["count"]
    logging.info(f"Found {total_docs} documents in {unified_index}")

    if total_docs == 0:
        logging.info("No documents to migrate. Skipping.")
        return

    schema = get_schema_for_index_type("ga_metrics")
    team_docs = defaultdict(list)

    logging.info("Grouping documents by team...")
    scroll_result = es.search(index=unified_index, scroll="5m", size=1000, body={"query": {"match_all": {}}})

    scroll_id = scroll_result["_scroll_id"]
    hits = scroll_result["hits"]["hits"]
    processed = 0

    while hits:
        for hit in hits:
            doc = hit["_source"]
            doc_id = hit.get("_id")  # Preserve document ID for deduplication
            team_title = doc.get("team_title", "unknown")
            sanitized = sanitize_team_title(team_title)
            team_docs[sanitized].append((doc_id, doc))
            processed += 1

        if processed % 1000 == 0:
            logging.info(f"  Processed {processed}/{total_docs} documents...")

        scroll_result = es.scroll(scroll_id=scroll_id, scroll="5m")
        scroll_id = scroll_result["_scroll_id"]
        hits = scroll_result["hits"]["hits"]

    es.clear_scroll(scroll_id=scroll_id)

    logging.info(f"Grouped into {len(team_docs)} teams")

    for team_sanitized, doc_tuples in team_docs.items():
        team_index = f"{team_sanitized}_ga_metrics"
        logging.info(f"Creating index: {team_index} ({len(doc_tuples)} documents)")

        if not es.indices.exists(index=team_index):
            # refuse rather than let the bulk below auto-create the
            # index under dynamic mapping (the drift).
            if not create_index_if_not_exists(es, team_index, schema):
                raise RuntimeError(
                    f"failed to create {team_index} with its schema mapping — refusing to bulk-write into it"
                )

        from elasticsearch.helpers import bulk

        actions = [
            {
                "_index": team_index,
                "_id": doc_id,  # Use original doc_id for deduplication
                "_source": doc,
            }
            for doc_id, doc in doc_tuples
        ]

        success, failed = bulk(es, actions)
        logging.info(f"  Indexed {success} documents to {team_index}")
        if failed:
            logging.error(f"  Failed to index {failed} documents")

    logging.info(f"✓ Google Analytics migration complete: {processed} documents migrated")
    return processed


def migrate_echo():
    """Migrate Echo data from echo_conversations to team-specific indices."""
    logging.info("=" * 70)
    logging.info("MIGRATING ECHO CONVERSATIONS DATA")
    logging.info("=" * 70)

    es = get_elasticsearch_client()
    unified_index = "echo_conversations"

    if not es.indices.exists(index=unified_index):
        logging.warning(f"Unified index '{unified_index}' does not exist. Skipping.")
        return

    count_result = es.count(index=unified_index)
    total_docs = count_result["count"]
    logging.info(f"Found {total_docs} documents in {unified_index}")

    if total_docs == 0:
        logging.info("No documents to migrate. Skipping.")
        return

    schema = get_schema_for_index_type("echo_conversations")
    team_docs = defaultdict(list)

    logging.info("Grouping documents by team...")
    scroll_result = es.search(index=unified_index, scroll="5m", size=1000, body={"query": {"match_all": {}}})

    scroll_id = scroll_result["_scroll_id"]
    hits = scroll_result["hits"]["hits"]
    processed = 0

    while hits:
        for hit in hits:
            doc = hit["_source"]
            doc_id = hit.get("_id")  # Preserve document ID
            team_title = doc.get("team_title", "unknown")
            sanitized = sanitize_team_title(team_title)
            team_docs[sanitized].append((doc_id, doc))
            processed += 1

        if processed % 1000 == 0:
            logging.info(f"  Processed {processed}/{total_docs} documents...")

        scroll_result = es.scroll(scroll_id=scroll_id, scroll="5m")
        scroll_id = scroll_result["_scroll_id"]
        hits = scroll_result["hits"]["hits"]

    es.clear_scroll(scroll_id=scroll_id)

    logging.info(f"Grouped into {len(team_docs)} teams")

    for team_sanitized, doc_tuples in team_docs.items():
        team_index = f"{team_sanitized}_echo_conversations"
        logging.info(f"Creating index: {team_index} ({len(doc_tuples)} documents)")

        if not es.indices.exists(index=team_index):
            # refuse rather than let the bulk below auto-create the
            # index under dynamic mapping (the drift).
            if not create_index_if_not_exists(es, team_index, schema):
                raise RuntimeError(
                    f"failed to create {team_index} with its schema mapping — refusing to bulk-write into it"
                )

        from elasticsearch.helpers import bulk

        actions = [
            {
                "_index": team_index,
                "_id": doc_id,  # Use original doc_id for deduplication
                "_source": doc,
            }
            for doc_id, doc in doc_tuples
        ]

        success, failed = bulk(es, actions)
        logging.info(f"  Indexed {success} documents to {team_index}")
        if failed:
            logging.error(f"  Failed to index {failed} documents")

    logging.info(f"✓ Echo migration complete: {processed} documents migrated")
    return processed


def verify_migration(data_source):
    """Verify that migration was successful."""
    logging.info("=" * 70)
    logging.info("VERIFYING MIGRATION")
    logging.info("=" * 70)

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
        return

    unified_index, team_pattern = index_mappings[data_source]

    # Count documents in unified index
    if es.indices.exists(index=unified_index):
        unified_count = es.count(index=unified_index)["count"]
        logging.info(f"Unified index '{unified_index}': {unified_count} documents")
    else:
        unified_count = 0
        logging.info(f"Unified index '{unified_index}': does not exist")

    # Count documents in team-specific indices
    team_indices = es.indices.get(index=team_pattern)
    total_team_docs = 0

    logging.info(f"\nTeam-specific indices (pattern: {team_pattern}):")
    for idx in sorted(team_indices.keys()):
        if idx != unified_index:  # Exclude unified index if it matches pattern
            count = es.count(index=idx)["count"]
            total_team_docs += count
            logging.info(f"  {idx}: {count} documents")

    logging.info(f"\nTotal documents in team indices: {total_team_docs}")

    if unified_count == total_team_docs:
        logging.info("✓ Migration verified: Document counts match!")
    else:
        logging.warning(f"⚠ Document count mismatch: unified={unified_count}, team={total_team_docs}")


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    data_source = sys.argv[1].lower()

    if data_source not in ["facebook", "instagram", "google_analytics", "echo", "all"]:
        print(f"Error: Invalid data source '{data_source}'")
        print(__doc__)
        sys.exit(1)

    logging.info("=" * 70)
    logging.info("UNIFIED → TEAM-SPECIFIC INDEX MIGRATION")
    logging.info("=" * 70)
    logging.info(f"Data source: {data_source}")
    logging.info("")

    total_migrated = 0

    if data_source == "all" or data_source == "facebook":
        count = migrate_facebook()
        if count:
            total_migrated += count
            verify_migration("facebook")

    if data_source == "all" or data_source == "instagram":
        count = migrate_instagram()
        if count:
            total_migrated += count
            verify_migration("instagram")

    if data_source == "all" or data_source == "google_analytics":
        count = migrate_google_analytics()
        if count:
            total_migrated += count
            verify_migration("google_analytics")

    if data_source == "all" or data_source == "echo":
        count = migrate_echo()
        if count:
            total_migrated += count
            verify_migration("echo")

    logging.info("=" * 70)
    logging.info(f"MIGRATION COMPLETE: {total_migrated} total documents migrated")
    logging.info("=" * 70)
    logging.info("")
    logging.info("Next steps:")
    logging.info("1. Verify data in team-specific indices")
    logging.info("2. Test imports with new team-specific logic")
    logging.info("3. Delete unified indices when confident:")
    logging.info("   curl -X DELETE 'http://es01:9200/fb_insights'")
    logging.info("   curl -X DELETE 'http://es01:9200/ig_insights'")
    logging.info("   curl -X DELETE 'http://es01:9200/ga_metrics'")
    logging.info("   curl -X DELETE 'http://es01:9200/echo_conversations'")


if __name__ == "__main__":
    main()
