"""
Google Analytics JSON to Elasticsearch Indexer

Reads JSON files created by get_ga_metrics.py and indexes them to Elasticsearch.
Creates a unified ga_metrics index for all teams with team differentiation via team_title field.
"""

import os
import json
import sys
import logging
import lib.logging
from lib.es_helpers import (
    build_team_lookup_cache,
    create_index_if_not_exists,
    ensure_data_index,
    get_elasticsearch_client,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_for_index_type

SOURCE_SLUG = "ga_metrics"


def create_ga_metrics_index(client, index_name):
    """
    Create or ensure unified GA metrics index exists with proper mapping.
    Uses centralized schema from schemas/ directory.

    Args:
        client: Elasticsearch client
        index_name: Name of the index to create (unified: ga_metrics)
    """
    lib.logging.define("ga-json-to-elastic")

    # Load schema from JSON file (replaces inline schema definition)
    mapping = get_schema_for_index_type("ga_metrics")

    create_index_if_not_exists(client, index_name, mapping)
    logging.info(f"Unified index {index_name} ensured to exist or created successfully")


def send_to_elasticsearch(client, index_name, data, doc_id):
    """
    Send a single document to Elasticsearch.

    Args:
        client: Elasticsearch client
        index_name: Target index name
        data: Document to index
        doc_id: Unique document ID (for deduplication)

    Returns:
        bool: True if indexed successfully, False on error
    """
    lib.logging.define("ga-json-to-elastic")
    try:
        # Use doc_id for idempotent indexing (prevents duplicates)
        client.index(index=index_name, body=data, id=doc_id)
        return True
    except Exception as e:
        logging.error(f"Failed to send document to {index_name}: {e}")
        return False


def read_json_files(directory):
    """
    Generator that yields individual documents from JSON files.

    Each file may contain multiple newline-delimited JSON documents.

    Args:
        directory: Base directory to walk

    Yields:
        dict: Individual JSON document
    """
    lib.logging.define("ga-json-to-elastic")

    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "r") as file:
                        # Handle newline-delimited JSON (one doc per line)
                        for line in file:
                            line = line.strip()
                            if line:
                                yield json.loads(line)
                except json.JSONDecodeError as e:
                    logging.error(f"Failed to parse JSON from {filepath}: {e}")
                except Exception as e:
                    logging.error(f"Error reading {filepath}: {e}")


def main(params):
    """
    Main entry point from import.py.

    Reads JSON files from /app/data/google_analytics/ga_metrics/ and indexes to Elasticsearch.

    Args:
        params: Dict of command-line parameters (unused)
    """
    lib.logging.define("ga-json-to-elastic")

    directory = "/app/data/google_analytics/ga_metrics/"

    # Log start
    logging.info(f"Reading JSON files from {directory}")
    logging.info("Using ESID-keyed indices (pattern: {team_esid}__ga_metrics)")

    # Get Elasticsearch client
    es_client = get_elasticsearch_client()

    # Track created indices to avoid recreating (keyed by team_esid)
    indices_created = {}

    # Get schema once for reuse
    schema = get_schema_for_index_type("ga_metrics")

    # one-shot team lookup so we can resolve team_esid (and drop
    # orphan docs) without a per-doc ES round-trip.
    # Phase B: narrow scan to GA-enabled teams (ESID-first; avoids the
    # wasted all-teams scan + cross-workspace collision WARNING).
    team_cache = build_team_lookup_cache(es_client, team_filter={"ds_google_analytics": "true"})

    doc_count = 0
    error_count = 0
    skipped_orphans = 0

    for data in read_json_files(directory):
        # Set kibana_date if missing
        if "kibana_date" not in data or not data["kibana_date"]:
            # Convert GA4 date format (YYYYMMDD) to YYYY-MM-DD
            ga_date = data.get("date", "")
            if len(ga_date) == 8:
                data["kibana_date"] = f"{ga_date[:4]}-{ga_date[4:6]}-{ga_date[6:]}"
            else:
                # Fallback: use current date
                from datetime import datetime

                data["kibana_date"] = datetime.now().strftime("%Y-%m-%d")

        # Get pre-computed document ID (for deduplication)
        doc_id = data.get("_doc_id")
        if not doc_id:
            logging.warning(f"Document missing _doc_id field, generating one")
            import hashlib

            # fallback _id mirrors get_ga_metrics.save_property_json —
            # report_type + every key on the doc that is a GA4 dimension (the
            # producer already wrote only this report_type's dimensions). Sorted
            # for determinism so the same row always hashes the same.
            report_type = data.get("report_type", "pages")
            _dim_candidates = (
                "pageTitle",
                "pagePath",
                "unifiedScreenClass",
                "platform",
                "date",
                "sessionSource",
                "sessionMedium",
                "firstUserSource",
                "firstUserMedium",
                "country",
                "city",
                "deviceCategory",
                "newVsReturning",
                "audienceName",
                "eventName",
                "linkUrl",
            )
            dim_parts = "-".join(f"{k}={data.get(k, '')}" for k in _dim_candidates if k in data)
            doc_id_string = f"{data.get('property_id')}-{report_type}-{dim_parts}"
            doc_id = hashlib.md5(  # nosemgrep: insecure-hash-algorithm-md5 - non-cryptographic doc ID fingerprint
                doc_id_string.encode()
            ).hexdigest()
            data["_doc_id"] = doc_id

        # Remove _doc_id before indexing (it's only for deduplication)
        doc_data = {k: v for k, v in data.items() if k != "_doc_id"}

        # resolve team_esid and build the ESID-keyed index name.
        # Docs whose team can't be resolved are dropped (no unknown_* index).
        team_esid = resolve_team_esid(
            team_cache,
            team_esid=data.get("team_esid"),
            team_title=data.get("team_title"),
        )
        if not team_esid:
            skipped_orphans += 1
            if skipped_orphans <= 5:
                logging.warning(
                    "ga_metrics: dropping doc with unresolved team — " "team_esid=%r team_title=%r",
                    data.get("team_esid"),
                    data.get("team_title"),
                )
            continue

        # Create index on first encounter
        if team_esid not in indices_created:
            index_name = ensure_data_index(es_client, team_esid, SOURCE_SLUG, schema)
            indices_created[team_esid] = index_name
            logging.info(f"Created team index: {index_name}")
        else:
            index_name = indices_created[team_esid]

        # Send to team-specific index
        if send_to_elasticsearch(es_client, index_name, doc_data, doc_id):
            doc_count += 1
        else:
            error_count += 1

        if (doc_count + error_count) % 100 == 0:
            logging.info(f"Processed {doc_count + error_count} documents so far ({error_count} errors)...")

    # Log summary
    logging.info(f"Completed sending data to {len(indices_created)} team-specific indices")
    logging.info(f"Total documents indexed: {doc_count}")
    if error_count:
        logging.error(f"Failed to index {error_count} document(s) — see errors above")
    if skipped_orphans:
        logging.warning(
            "ga_metrics: total %d doc(s) dropped because team could not be resolved "
            "(team_esid missing AND team_title sanitization did not match any team in "
            "de_asset_teams)",
            skipped_orphans,
        )


if __name__ == "__main__":
    # For standalone testing
    main({})
