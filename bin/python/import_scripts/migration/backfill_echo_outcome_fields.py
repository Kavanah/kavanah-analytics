"""
Backfill outcome category counter fields in Echo conversation documents.

This migration script adds the following fields to all Echo conversation documents
based on their existing 'outcome' value:
- spiritual_conversation
- multiplier
- no_response
- language_barrier
- Other

Exactly one field is set to 1, the rest are set to 0 per document.

Indices affected:
- All indices matching pattern: *_echo_conversations

Execution:
    python3 /app/python/import.py migration backfill_echo_outcome_fields [--dry-run=true]
"""

import logging
import lib.logging
from lib.es_helpers import get_elasticsearch_client
from elasticsearch import helpers
from import_scripts.echo.outcome_flags import compute_outcome_flags


def calculate_outcome_fields(outcome_value):
    """
    Calculate the 5 outcome category counter fields based on outcome value.

    delegates to the shared, case-insensitive
    ``compute_outcome_flags`` (the importer uses the same function), so a
    re-run of this migration re-stamps docs whose flags were computed with
    the old case-sensitive compare ("Language barrier" vs Echo's actual
    "Language Barrier").

    Args:
        outcome_value: The value of the 'outcome' field from the document

    Returns:
        dict: Dictionary with the 5 counter fields (exactly one is 1, rest are 0)
    """
    return compute_outcome_flags(outcome_value)


def process_echo_index(es, index_name, dry_run=False):
    """
    Process a single Echo conversations index to backfill outcome fields.

    Args:
        es: Elasticsearch client
        index_name: Name of the index to process
        dry_run: If True, only count documents without making changes

    Returns:
        dict: Statistics about the operation
    """
    lib.logging.define("backfill-echo-outcome")

    stats = {
        "index": index_name,
        "total_docs": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "error_details": [],
    }

    # Count total documents in this index
    try:
        stats["total_docs"] = es.count(index=index_name)["count"]
        logging.info(f"Total documents in {index_name}: {stats['total_docs']}")
    except Exception as e:
        logging.error(f"Failed to count documents in {index_name}: {e}")
        return stats

    if stats["total_docs"] == 0:
        logging.info(f"Index {index_name} is empty, skipping")
        return stats

    # Query all documents (no filter - update all documents)
    query = {"match_all": {}}

    try:
        # Use scroll API for large datasets
        response = es.search(index=index_name, body={"query": query}, scroll="5m", size=1000)

        scroll_id = response["_scroll_id"]

        if dry_run:
            logging.info(f"[DRY RUN] Would update {stats['total_docs']} documents")
            return stats

        actions = []

        while True:
            hits = response["hits"]["hits"]
            if not hits:
                break

            for doc in hits:
                doc_id = doc["_id"]
                source = doc["_source"]

                # Get outcome value (empty string if missing)
                outcome = source.get("outcome", "")

                # Calculate the 5 outcome fields
                outcome_fields = calculate_outcome_fields(outcome)

                # Create update action
                action = {
                    "_op_type": "update",
                    "_index": index_name,
                    "_id": doc_id,
                    "doc": outcome_fields,
                }
                actions.append(action)

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
    Validate that all documents in index have the outcome fields.

    Args:
        es: Elasticsearch client
        index_name: Name of index to validate

    Returns:
        dict: Validation results
    """
    lib.logging.define("backfill-echo-outcome-validate")

    # Force refresh for accurate counts
    try:
        es.indices.refresh(index=index_name)
    except Exception as e:
        logging.warning(f"Could not refresh {index_name}: {e}")

    # Count total documents
    total = es.count(index=index_name)["count"]

    # Count documents missing any of the 5 outcome fields
    query = {
        "bool": {
            "should": [
                {"bool": {"must_not": {"exists": {"field": "spiritual_conversation"}}}},
                {"bool": {"must_not": {"exists": {"field": "multiplier"}}}},
                {"bool": {"must_not": {"exists": {"field": "no_response"}}}},
                {"bool": {"must_not": {"exists": {"field": "language_barrier"}}}},
                {"bool": {"must_not": {"exists": {"field": "Other"}}}},
            ],
            "minimum_should_match": 1,
        }
    }

    try:
        missing = es.count(index=index_name, body={"query": query})["count"]
    except Exception as e:
        logging.error(f"Validation query failed for {index_name}: {e}")
        missing = -1

    # Count documents with all outcome fields
    with_fields = total - missing if missing >= 0 else -1

    return {
        "index": index_name,
        "total": total,
        "with_fields": with_fields,
        "missing_fields": missing,
        "success": missing == 0,
    }


def main(params):
    """
    Entry point for backfill outcome fields migration script.

    Args:
        params: Dictionary of parameters from import.py
                - dry-run: If "true", only preview changes

    Returns:
        str: 'success' if migration succeeded, 'abort' otherwise
    """
    lib.logging.define("backfill-echo-outcome")

    dry_run = params.get("dry-run", "").lower() == "true"

    if dry_run:
        logging.info("=" * 70)
        logging.info("DRY RUN MODE - No changes will be made")
        logging.info("=" * 70)

    logging.info("Starting outcome fields backfill migration...")
    logging.info("This migration adds outcome category counter fields to all")
    logging.info("Echo conversation documents across all team-specific indices")
    logging.info("")

    # Connect to Elasticsearch
    es = get_elasticsearch_client()
    if not es:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    # Find all Echo conversation indices
    try:
        all_indices = es.indices.get(index="*_echo_conversations")
        echo_indices = sorted(all_indices.keys())
        logging.info(f"Found {len(echo_indices)} Echo conversation indices:")
        for idx in echo_indices:
            logging.info(f"  - {idx}")
        logging.info("")
    except Exception as e:
        logging.error(f"Failed to query Echo conversation indices: {e}")
        return "abort"

    if not echo_indices:
        logging.warning("No Echo conversation indices found matching pattern *_echo_conversations")
        return "success"

    # Process each index
    results = []

    for index_name in echo_indices:
        logging.info("=" * 70)
        logging.info(f"Processing {index_name}...")
        logging.info("=" * 70)
        stats = process_echo_index(es, index_name, dry_run=dry_run)
        results.append(stats)
        logging.info("")

    # Print summary
    logging.info("=" * 70)
    logging.info("MIGRATION SUMMARY")
    logging.info("=" * 70)

    total_docs = 0
    total_updated = 0
    total_errors = 0

    for stats in results:
        total_docs += stats["total_docs"]
        total_updated += stats["updated"]
        total_errors += stats["errors"]

        logging.info(f"\n{stats['index']}:")
        logging.info(f"  Total documents: {stats['total_docs']}")
        logging.info(f"  Updated: {stats['updated']}")
        logging.info(f"  Skipped: {stats['skipped']}")
        logging.info(f"  Errors: {stats['errors']}")

        if stats["error_details"]:
            logging.warning(f"  Error details: {stats['error_details'][:5]}")  # First 5 errors

    logging.info("")
    logging.info(f"TOTALS ACROSS ALL INDICES:")
    logging.info(f"  Indices processed: {len(results)}")
    logging.info(f"  Total documents: {total_docs}")
    logging.info(f"  Total updated: {total_updated}")
    logging.info(f"  Total errors: {total_errors}")

    # Validation (only if not dry run)
    if not dry_run:
        logging.info("")
        logging.info("=" * 70)
        logging.info("VALIDATION")
        logging.info("=" * 70)

        all_success = True
        for stats in results:
            if stats["total_docs"] == 0:
                continue

            validation = validate_migration(es, stats["index"])
            logging.info(f"\n{validation['index']}:")
            logging.info(f"  Total documents: {validation['total']}")
            logging.info(f"  With all outcome fields: {validation['with_fields']}")
            logging.info(f"  Missing outcome fields: {validation['missing_fields']}")

            if validation["success"]:
                logging.info(f"  ✓ SUCCESS: All documents have outcome fields")
            else:
                logging.warning(f"  ⚠ WARNING: {validation['missing_fields']} documents still missing outcome fields")
                all_success = False

        if all_success:
            logging.info("")
            logging.info("✓ All validations passed!")

    logging.info("")
    logging.info("=" * 70)
    logging.info("Migration completed")
    logging.info("=" * 70)

    return "success"


if __name__ == "__main__":
    # Allow direct execution
    main({})
