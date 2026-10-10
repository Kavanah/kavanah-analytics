"""
Backfill missing or incorrect organization_title fields in data indices.

This migration script updates organization_title fields in all documents across
data indices to match their team's current organization assignments. It handles
both single-org teams (string values) and multi-org teams (array values).

Indices affected:
- fb_insights: Facebook Ads data
- echo_conversations: Echo messaging data
- ga_metrics: Google Analytics data
- ig_insights: Instagram insights data

Features:
- Pre-loads all team→organization mappings for fast lookups
- Preserves arrays for multi-org teams (native Elasticsearch support)
- Idempotent: skips documents that already have correct values
- Dry-run mode for testing
- Comprehensive validation and statistics

Execution:
    python3 /app/python/import.py migration backfill_organization_title [--dry-run=true] [--index=fb_insights]
    python3 /app/python/import_scripts/migration/backfill_organization_title.py
"""

import logging
import lib.logging
from lib.es_helpers import get_elasticsearch_client
from elasticsearch import helpers


def load_team_organizations(es):
    """
    Load all teams and build team_title → organization_title mapping.

    Args:
        es: Elasticsearch client

    Returns:
        dict: {team_title: organization_title_value} or None on error
              Values can be strings, arrays, or 'Unknown'
    """
    lib.logging.define("backfill-org-title-load")

    logging.info("Loading team organization mappings from de_asset_teams...")

    team_org_map = {}
    stats = {"total_teams": 0, "with_orgs": 0, "without_orgs": 0, "multi_org_teams": 0}

    try:
        # Fetch all teams (size=10000 should be sufficient)
        response = es.search(index="de_asset_teams", body={"query": {"match_all": {}}, "size": 10000})

        teams = response.get("hits", {}).get("hits", [])
        stats["total_teams"] = len(teams)

        for hit in teams:
            team_source = hit["_source"]
            team_title = team_source.get("team_title")

            # Skip teams without titles (shouldn't happen)
            if not team_title:
                logging.warning(f"Team {hit['_id']} missing team_title, skipping")
                continue

            # Extract organization_title value
            org_title = team_source.get("organization_title")

            # Handle different value types
            if org_title:
                # Could be string, array, or other
                if isinstance(org_title, list):
                    if len(org_title) > 0:
                        # Non-empty array → preserve as-is
                        team_org_map[team_title] = org_title
                        stats["with_orgs"] += 1
                        if len(org_title) > 1:
                            stats["multi_org_teams"] += 1
                    else:
                        # Empty array → treat as no org
                        team_org_map[team_title] = "Unknown"
                        stats["without_orgs"] += 1
                elif isinstance(org_title, str) and org_title.strip():
                    # Non-empty string
                    team_org_map[team_title] = org_title
                    stats["with_orgs"] += 1
                else:
                    # Empty string or other falsy value
                    team_org_map[team_title] = "Unknown"
                    stats["without_orgs"] += 1
            else:
                # Field missing or None
                team_org_map[team_title] = "Unknown"
                stats["without_orgs"] += 1

        logging.info(f"Loaded {stats['total_teams']} teams:")
        logging.info(f"  - With organizations: {stats['with_orgs']}")
        logging.info(f"  - Multi-org teams: {stats['multi_org_teams']}")
        logging.info(f"  - Without organizations: {stats['without_orgs']}")

        return team_org_map

    except Exception as e:
        logging.error(f"Failed to load team organizations: {e}")
        return None


def process_index(es, index_name, team_org_map, dry_run=False):
    """
    Process a single index to backfill organization_title field.

    Args:
        es: Elasticsearch client
        index_name: Name of index to process
        team_org_map: Dictionary mapping team_title → organization_title
        dry_run: If True, only count documents without making changes

    Returns:
        dict: Statistics about the operation
    """
    lib.logging.define(f"backfill-org-title-{index_name}")

    stats = {
        "index": index_name,
        "total_docs": 0,
        "updated": 0,
        "no_team_title": 0,
        "team_not_found": 0,
        "already_correct": 0,
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

    if stats["total_docs"] == 0:
        logging.info(f"Index {index_name} is empty, skipping")
        return stats

    # Query: ALL documents (user wants to update everything)
    query = {"match_all": {}}

    try:
        # Scroll through all documents
        response = es.search(index=index_name, body={"query": query}, scroll="5m", size=1000)

        scroll_id = response["_scroll_id"]
        actions = []

        while True:
            hits = response["hits"]["hits"]
            if not hits:
                break

            for doc in hits:
                doc_id = doc["_id"]
                source = doc["_source"]

                # Extract team_title from document
                team_title = source.get("team_title")

                # Edge Case 1: No team_title in document
                if not team_title or team_title == "unknown":
                    stats["no_team_title"] += 1
                    # Set to Unknown and continue
                    new_org_title = "Unknown"
                else:
                    # Look up organization_title from team map
                    new_org_title = team_org_map.get(team_title)

                    # Edge Case 2: Team not found in map
                    if new_org_title is None:
                        stats["team_not_found"] += 1
                        new_org_title = "Unknown"
                        logging.debug(f"Team '{team_title}' not found in team map")

                # Check if value already matches (optimization)
                current_org_title = source.get("organization_title")
                if current_org_title == new_org_title:
                    stats["already_correct"] += 1
                    continue  # Skip update

                # Build bulk update action
                if not dry_run:
                    action = {
                        "_op_type": "update",
                        "_index": index_name,
                        "_id": doc_id,
                        "doc": {"organization_title": new_org_title},
                    }
                    actions.append(action)

            # Execute bulk update when batch size reached
            if len(actions) >= 1000:
                try:
                    success, failed = helpers.bulk(es, actions, raise_on_error=False, stats_only=True)
                    stats["updated"] += success
                    stats["errors"] += failed
                    if failed > 0:
                        logging.warning(f"Bulk update: {success} succeeded, {failed} failed")
                    else:
                        logging.info(f"Bulk update: {success} documents updated")
                    actions = []
                except Exception as e:
                    logging.error(f"Bulk update failed: {e}")
                    stats["errors"] += len(actions)
                    stats["error_details"].append(str(e))
                    actions = []

            # Get next batch
            response = es.scroll(scroll_id=scroll_id, scroll="5m")

        # Process remaining actions
        if actions and not dry_run:
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

        if dry_run:
            # Calculate how many would be updated
            would_update = stats["total_docs"] - stats["already_correct"]
            logging.info(f"[DRY RUN] Would update {would_update} documents")
            logging.info(f"  - No team_title: {stats['no_team_title']}")
            logging.info(f"  - Team not found: {stats['team_not_found']}")
            logging.info(f"  - Already correct: {stats['already_correct']}")

    except Exception as e:
        logging.error(f"Error processing {index_name}: {e}")
        stats["errors"] += 1
        stats["error_details"].append(str(e))

    return stats


def validate_migration(es, index_name):
    """
    Validate migration results for an index.

    Args:
        es: Elasticsearch client
        index_name: Name of index to validate

    Returns:
        dict: Validation statistics
    """
    lib.logging.define("backfill-org-title-validate")

    # Force refresh for accurate counts
    try:
        es.indices.refresh(index=index_name)
    except Exception as e:
        logging.warning(f"Could not refresh {index_name}: {e}")

    results = {
        "index": index_name,
        "total_docs": 0,
        "with_org_title": 0,
        "missing_org_title": 0,
        "unknown_org_title": 0,
        "array_org_title": 0,
        "string_org_title": 0,
    }

    # Total document count
    results["total_docs"] = es.count(index=index_name)["count"]

    # Count documents missing organization_title field
    missing_query = {"bool": {"must_not": {"exists": {"field": "organization_title"}}}}
    results["missing_org_title"] = es.count(index=index_name, body={"query": missing_query})["count"]

    # Count documents with organization_title = 'Unknown'
    unknown_query = {"term": {"organization_title": "Unknown"}}
    results["unknown_org_title"] = es.count(index=index_name, body={"query": unknown_query})["count"]

    # Calculate documents with valid org_title
    results["with_org_title"] = results["total_docs"] - results["missing_org_title"]

    # Sample documents to check array vs string types
    try:
        sample = es.search(
            index=index_name,
            body={"query": {"exists": {"field": "organization_title"}}, "size": 100},
        )

        for hit in sample["hits"]["hits"]:
            org_value = hit["_source"].get("organization_title")
            if isinstance(org_value, list):
                results["array_org_title"] += 1
            elif isinstance(org_value, str):
                results["string_org_title"] += 1
    except Exception as e:
        logging.warning(f"Could not sample documents: {e}")

    return results


def sample_documents(es, index_name, sample_size=5):
    """
    Retrieve sample documents for manual inspection.

    Args:
        es: Elasticsearch client
        index_name: Name of index to sample
        sample_size: Number of samples to retrieve

    Returns:
        dict: Sample documents categorized by org_title type
    """
    lib.logging.define("backfill-org-title-sample")

    samples = {"with_array_org": [], "with_string_org": [], "with_unknown_org": []}

    try:
        # Sample documents with organization_title
        docs = es.search(
            index=index_name,
            body={
                "query": {"exists": {"field": "organization_title"}},
                "size": sample_size * 3,  # Get extra to find all types
            },
        )

        for hit in docs["hits"]["hits"]:
            source = hit["_source"]
            org_value = source.get("organization_title")

            sample_doc = {
                "doc_id": hit["_id"],
                "team_title": source.get("team_title"),
                "organization_title": org_value,
                "org_type": type(org_value).__name__,
            }

            if isinstance(org_value, list):
                if len(samples["with_array_org"]) < sample_size:
                    samples["with_array_org"].append(sample_doc)
            elif org_value == "Unknown":
                if len(samples["with_unknown_org"]) < sample_size:
                    samples["with_unknown_org"].append(sample_doc)
            else:
                if len(samples["with_string_org"]) < sample_size:
                    samples["with_string_org"].append(sample_doc)

    except Exception as e:
        logging.error(f"Error sampling documents: {e}")

    return samples


def main(params):
    """
    Entry point for organization title backfill migration.

    Args:
        params: Dictionary of parameters from import.py
                - dry-run: If "true", only preview changes
                - index: Optional specific index to process

    Returns:
        str: 'success' if migration succeeded, 'abort' otherwise
    """
    lib.logging.define("backfill-org-title")

    dry_run = params.get("dry-run", "").lower() == "true"
    target_index = params.get("index")  # Optional: specific index

    # Target indices
    indices = ["fb_insights", "echo_conversations", "ga_metrics", "ig_insights"]

    # Filter to specific index if requested
    if target_index:
        if target_index in indices:
            indices = [target_index]
            logging.info(f"Processing single index: {target_index}")
        else:
            logging.error(f"Unknown index: {target_index}")
            logging.error(f"Valid indices: {', '.join(indices)}")
            return "abort"

    if dry_run:
        logging.info("=" * 70)
        logging.info("DRY RUN MODE - No changes will be made")
        logging.info("=" * 70)

    logging.info("Starting organization_title backfill migration...")
    logging.info("This migration updates ALL documents in data indices")
    logging.info("to match team organization assignments")
    logging.info("")

    # Connect to Elasticsearch
    es = get_elasticsearch_client()
    if not es:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    # Step 1: Load team organization mappings
    logging.info("=" * 70)
    logging.info("STEP 1: Loading team organization mappings")
    logging.info("=" * 70)

    team_org_map = load_team_organizations(es)
    if team_org_map is None:
        logging.error("Failed to load team organizations")
        return "abort"

    logging.info(f"Successfully loaded {len(team_org_map)} team mappings")
    logging.info("")

    # Step 2: Process each index
    results = []

    for index_name in indices:
        logging.info("=" * 70)
        logging.info(f"STEP 2: Processing {index_name} index")
        logging.info("=" * 70)

        stats = process_index(es, index_name, team_org_map, dry_run=dry_run)
        results.append(stats)

        logging.info("")

    # Step 3: Print summary
    logging.info("=" * 70)
    logging.info("MIGRATION SUMMARY")
    logging.info("=" * 70)

    for stats in results:
        logging.info(f"\n{stats['index']}:")
        logging.info(f"  Total documents: {stats['total_docs']}")
        logging.info(f"  Updated: {stats['updated']}")
        logging.info(f"  Already correct: {stats['already_correct']}")
        logging.info(f"  No team_title: {stats['no_team_title']}")
        logging.info(f"  Team not found: {stats['team_not_found']}")
        logging.info(f"  Errors: {stats['errors']}")

        if stats["error_details"]:
            logging.warning(f"  Error samples: {stats['error_details'][:3]}")

    # Step 4: Validation (only if not dry run)
    if not dry_run:
        logging.info("")
        logging.info("=" * 70)
        logging.info("STEP 3: VALIDATION")
        logging.info("=" * 70)

        all_valid = True

        for stats in results:
            validation = validate_migration(es, stats["index"])

            logging.info(f"\n{validation['index']}:")
            logging.info(f"  Total documents: {validation['total_docs']}")
            logging.info(f"  With organization_title: {validation['with_org_title']}")
            logging.info(f"  Missing organization_title: {validation['missing_org_title']}")
            logging.info(f"  Set to 'Unknown': {validation['unknown_org_title']}")
            logging.info(f"  Array values: {validation['array_org_title']}")
            logging.info(f"  String values: {validation['string_org_title']}")

            if validation["missing_org_title"] > 0:
                logging.warning(f"  WARNING: {validation['missing_org_title']} docs missing organization_title")
                all_valid = False

            # Sample documents
            samples = sample_documents(es, stats["index"], sample_size=3)

            if samples["with_array_org"]:
                logging.info(f"\n  Sample multi-org documents:")
                for sample in samples["with_array_org"][:2]:
                    logging.info(f"    - Team: {sample['team_title']}, Orgs: {sample['organization_title']}")

            if samples["with_unknown_org"]:
                logging.info(f"\n  Sample 'Unknown' documents:")
                for sample in samples["with_unknown_org"][:2]:
                    logging.info(f"    - Team: {sample['team_title']}, Org: {sample['organization_title']}")

        if all_valid:
            logging.info("\nSUCCESS: All indices validated successfully")
        else:
            logging.warning("\nWARNING: Some indices have validation warnings")

    logging.info("")
    logging.info("=" * 70)
    logging.info("Migration completed")
    logging.info("=" * 70)

    return "success"


if __name__ == "__main__":
    # Allow direct execution
    main({})
