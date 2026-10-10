"""
Backfill missing @timestamp fields in historical data.

This migration script adds @timestamp fields to documents that are missing them,
using data date fields (insight_date_start for Facebook, created_at for Echo)
instead of ingestion time for historical accuracy.

Indices affected:
- fb_insights: Uses insight_date_start or kibana_date
- echo_conversations: Uses created_at or kibana_date

Execution:
    python3 /app/python/import.py migration backfill_timestamp [--dry-run=true]
    python3 /app/python/import_scripts/migration/backfill_timestamp.py
"""

import logging
import lib.logging
from datetime import datetime, timezone
from lib.es_helpers import get_elasticsearch_client
from elasticsearch import helpers


def parse_date_to_timestamp(date_string, date_format):
    """
    Parse date string to ISO 8601 timestamp.

    Args:
        date_string: Date string to parse
        date_format: Format identifier ('fb_date' or 'echo_date')

    Returns:
        ISO 8601 timestamp string or None if parsing fails
    """
    try:
        if date_format == "fb_date":
            # Format: "yyyy-MM-dd" -> "2024-01-15"
            dt = datetime.strptime(date_string, "%Y-%m-%d")
            # Set to start of day in UTC
            dt = dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

        elif date_format == "echo_date":
            # Format: "MMMM dd, yyyy HH:mm:ss 'UTC'" -> "January 15, 2024 14:30:00 UTC"
            dt = datetime.strptime(date_string, "%B %d, %Y %H:%M:%S UTC")
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            return None

        # Return ISO 8601 format
        return dt.isoformat()

    except (ValueError, TypeError) as e:
        logging.warning(f"Failed to parse date '{date_string}' with format '{date_format}': {e}")
        return None


def process_fb_insights(es, dry_run=False):
    """
    Process fb_insights index to backfill missing @timestamp fields.

    Args:
        es: Elasticsearch client
        dry_run: If True, only count documents without making changes

    Returns:
        dict: Statistics about the operation
    """
    lib.logging.define("backfill-timestamp-fb")

    index_name = "fb_insights"
    stats = {
        "index": index_name,
        "total_docs": 0,
        "missing_timestamp": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "error_details": [],
    }

    # Count total documents
    try:
        stats["total_docs"] = es.count(index=index_name)["count"]
        logging.info(f"Total documents in {index_name}: {stats['total_docs']}")
    except Exception as e:
        logging.error(f"Failed to count documents in {index_name}: {e}")
        return stats

    # Query for documents missing @timestamp
    query = {"bool": {"must_not": {"exists": {"field": "@timestamp"}}}}

    try:
        # Use scroll API for large datasets
        response = es.search(index=index_name, body={"query": query}, scroll="5m", size=1000)

        scroll_id = response["_scroll_id"]
        stats["missing_timestamp"] = response["hits"]["total"]["value"]
        logging.info(f"Documents missing @timestamp: {stats['missing_timestamp']}")

        if dry_run:
            logging.info(f"[DRY RUN] Would update {stats['missing_timestamp']} documents")
            return stats

        actions = []

        while True:
            hits = response["hits"]["hits"]
            if not hits:
                break

            for doc in hits:
                doc_id = doc["_id"]
                source = doc["_source"]

                # Try primary field: insight_date_start
                date_value = source.get("insight_date_start")
                timestamp = None

                if date_value:
                    timestamp = parse_date_to_timestamp(date_value, "fb_date")

                # Fallback to kibana_date if primary fails
                if not timestamp:
                    date_value = source.get("kibana_date")
                    if date_value:
                        timestamp = parse_date_to_timestamp(date_value, "fb_date")

                if timestamp:
                    # Create update action
                    action = {
                        "_op_type": "update",
                        "_index": index_name,
                        "_id": doc_id,
                        "doc": {"@timestamp": timestamp},
                    }
                    actions.append(action)
                else:
                    # No valid date field found
                    stats["skipped"] += 1
                    logging.warning(f"Skipping document {doc_id}: no valid date field")

            # Bulk update when batch reaches 1000 or at end
            if len(actions) >= 1000:
                try:
                    success, failed = helpers.bulk(es, actions, raise_on_error=False, stats_only=True)
                    stats["updated"] += success
                    stats["errors"] += failed
                    logging.info(f"Bulk update: {success} succeeded, {failed} failed")
                    actions = []
                except Exception as e:
                    logging.error(f"Bulk update failed: {e}")
                    stats["errors"] += len(actions)
                    stats["error_details"].append(str(e))
                    actions = []

            # Get next batch
            response = es.scroll(scroll_id=scroll_id, scroll="5m")

        # Process remaining actions
        if actions:
            try:
                success, failed = helpers.bulk(es, actions, raise_on_error=False, stats_only=True)
                stats["updated"] += success
                stats["errors"] += failed
                logging.info(f"Final bulk update: {success} succeeded, {failed} failed")
            except Exception as e:
                logging.error(f"Final bulk update failed: {e}")
                stats["errors"] += len(actions)
                stats["error_details"].append(str(e))

        # Clear scroll
        try:
            es.clear_scroll(scroll_id=scroll_id)
        except:
            pass

    except Exception as e:
        logging.error(f"Error processing {index_name}: {e}")
        stats["errors"] += 1
        stats["error_details"].append(str(e))

    return stats


def process_echo_conversations(es, dry_run=False):
    """
    Process echo_conversations index to backfill missing @timestamp fields.

    Args:
        es: Elasticsearch client
        dry_run: If True, only count documents without making changes

    Returns:
        dict: Statistics about the operation
    """
    lib.logging.define("backfill-timestamp-echo")

    index_name = "echo_conversations"
    stats = {
        "index": index_name,
        "total_docs": 0,
        "missing_timestamp": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "error_details": [],
    }

    # Count total documents
    try:
        stats["total_docs"] = es.count(index=index_name)["count"]
        logging.info(f"Total documents in {index_name}: {stats['total_docs']}")
    except Exception as e:
        logging.error(f"Failed to count documents in {index_name}: {e}")
        return stats

    # Query for documents missing @timestamp
    query = {"bool": {"must_not": {"exists": {"field": "@timestamp"}}}}

    try:
        # Use scroll API for large datasets
        response = es.search(index=index_name, body={"query": query}, scroll="5m", size=1000)

        scroll_id = response["_scroll_id"]
        stats["missing_timestamp"] = response["hits"]["total"]["value"]
        logging.info(f"Documents missing @timestamp: {stats['missing_timestamp']}")

        if dry_run:
            logging.info(f"[DRY RUN] Would update {stats['missing_timestamp']} documents")
            return stats

        actions = []

        while True:
            hits = response["hits"]["hits"]
            if not hits:
                break

            for doc in hits:
                doc_id = doc["_id"]
                source = doc["_source"]

                # Try primary field: created_at
                date_value = source.get("created_at")
                timestamp = None

                if date_value:
                    timestamp = parse_date_to_timestamp(date_value, "echo_date")

                # Fallback to kibana_date if primary fails
                if not timestamp:
                    date_value = source.get("kibana_date")
                    if date_value:
                        timestamp = parse_date_to_timestamp(date_value, "echo_date")

                if timestamp:
                    # Create update action
                    action = {
                        "_op_type": "update",
                        "_index": index_name,
                        "_id": doc_id,
                        "doc": {"@timestamp": timestamp},
                    }
                    actions.append(action)
                else:
                    # No valid date field found
                    stats["skipped"] += 1
                    logging.warning(f"Skipping document {doc_id}: no valid date field")

            # Bulk update when batch reaches 1000 or at end
            if len(actions) >= 1000:
                try:
                    success, failed = helpers.bulk(es, actions, raise_on_error=False, stats_only=True)
                    stats["updated"] += success
                    stats["errors"] += failed
                    logging.info(f"Bulk update: {success} succeeded, {failed} failed")
                    actions = []
                except Exception as e:
                    logging.error(f"Bulk update failed: {e}")
                    stats["errors"] += len(actions)
                    stats["error_details"].append(str(e))
                    actions = []

            # Get next batch
            response = es.scroll(scroll_id=scroll_id, scroll="5m")

        # Process remaining actions
        if actions:
            try:
                success, failed = helpers.bulk(es, actions, raise_on_error=False, stats_only=True)
                stats["updated"] += success
                stats["errors"] += failed
                logging.info(f"Final bulk update: {success} succeeded, {failed} failed")
            except Exception as e:
                logging.error(f"Final bulk update failed: {e}")
                stats["errors"] += len(actions)
                stats["error_details"].append(str(e))

        # Clear scroll
        try:
            es.clear_scroll(scroll_id=scroll_id)
        except:
            pass

    except Exception as e:
        logging.error(f"Error processing {index_name}: {e}")
        stats["errors"] += 1
        stats["error_details"].append(str(e))

    return stats


def validate_migration(es, index_name):
    """
    Validate that all documents now have @timestamp field.

    Args:
        es: Elasticsearch client
        index_name: Name of index to validate

    Returns:
        dict: Validation results
    """
    lib.logging.define("backfill-timestamp-validate")

    # Force refresh for accurate counts
    try:
        es.indices.refresh(index=index_name)
    except Exception as e:
        logging.warning(f"Could not refresh {index_name}: {e}")

    # Count total documents
    total = es.count(index=index_name)["count"]

    # Count documents still missing @timestamp
    query = {"bool": {"must_not": {"exists": {"field": "@timestamp"}}}}
    missing = es.count(index=index_name, body={"query": query})["count"]

    # Count documents with @timestamp
    with_timestamp = total - missing

    return {
        "index": index_name,
        "total": total,
        "with_timestamp": with_timestamp,
        "missing_timestamp": missing,
        "success": missing == 0,
    }


def main(params):
    """
    Entry point for backfill timestamp migration script.

    Args:
        params: Dictionary of parameters from import.py
                - dry-run: If "true", only preview changes

    Returns:
        str: 'success' if migration succeeded, 'abort' otherwise
    """
    lib.logging.define("backfill-timestamp")

    dry_run = params.get("dry-run", "").lower() == "true"

    if dry_run:
        logging.info("=" * 70)
        logging.info("DRY RUN MODE - No changes will be made")
        logging.info("=" * 70)

    logging.info("Starting @timestamp backfill migration...")
    logging.info("This migration adds @timestamp fields to historical data")
    logging.info("using data date fields for historical accuracy")
    logging.info("")

    # Connect to Elasticsearch
    es = get_elasticsearch_client()
    if not es:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    # Process both indices
    results = []

    logging.info("=" * 70)
    logging.info("Processing fb_insights index...")
    logging.info("=" * 70)
    fb_stats = process_fb_insights(es, dry_run=dry_run)
    results.append(fb_stats)

    logging.info("")
    logging.info("=" * 70)
    logging.info("Processing echo_conversations index...")
    logging.info("=" * 70)
    echo_stats = process_echo_conversations(es, dry_run=dry_run)
    results.append(echo_stats)

    # Print summary
    logging.info("")
    logging.info("=" * 70)
    logging.info("MIGRATION SUMMARY")
    logging.info("=" * 70)

    for stats in results:
        logging.info(f"\n{stats['index']}:")
        logging.info(f"  Total documents: {stats['total_docs']}")
        logging.info(f"  Missing @timestamp before: {stats['missing_timestamp']}")
        logging.info(f"  Updated: {stats['updated']}")
        logging.info(f"  Skipped (no date field): {stats['skipped']}")
        logging.info(f"  Errors: {stats['errors']}")

        if stats["error_details"]:
            logging.warning(f"  Error details: {stats['error_details'][:5]}")  # First 5 errors

    # Validation (only if not dry run)
    if not dry_run:
        logging.info("")
        logging.info("=" * 70)
        logging.info("VALIDATION")
        logging.info("=" * 70)

        all_success = True
        for stats in results:
            validation = validate_migration(es, stats["index"])
            logging.info(f"\n{validation['index']}:")
            logging.info(f"  Total documents: {validation['total']}")
            logging.info(f"  With @timestamp: {validation['with_timestamp']}")
            logging.info(f"  Missing @timestamp: {validation['missing_timestamp']}")

            if validation["success"]:
                logging.info(f"  SUCCESS: All documents have @timestamp")
            else:
                logging.warning(f"  WARNING: {validation['missing_timestamp']} documents still missing @timestamp")
                all_success = False

    logging.info("")
    logging.info("=" * 70)
    logging.info("Migration completed")
    logging.info("=" * 70)

    return "success"


if __name__ == "__main__":
    # Allow direct execution
    main({})
