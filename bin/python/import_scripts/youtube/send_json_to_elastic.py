"""
YouTube Analytics JSON to Elasticsearch Indexer

Reads JSON files created by get_youtube_analytics.py and indexes them to Elasticsearch.
Creates ESID-keyed indices following the pattern: {team_esid}__youtube_analytics

Usage:
    python3 /app/python/import.py youtube send_json_to_elastic
"""

import os
import json
import sys
import logging
import hashlib
from datetime import datetime

sys.path.insert(0, "/app/python")
import lib.logging as custom_logging
from lib.es_helpers import (
    build_data_index_name,
    build_team_lookup_cache,
    create_index_if_not_exists,
    get_elasticsearch_client,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_for_index_type

SOURCE_SLUG = "youtube_analytics"


def create_youtube_analytics_index(client, index_name):
    """
    Create or ensure YouTube Analytics index exists with proper mapping.

    Args:
        client: Elasticsearch client
        index_name: Name of the index to create
    """
    # mapping loaded from schemas/unified_schema_youtube_analytics.json
    # (single source of truth) — the same file the per-source mapping
    # template uses, so an auto-created index and an explicit create can never
    # disagree.
    mapping = get_schema_for_index_type(SOURCE_SLUG)
    if not create_index_if_not_exists(client, index_name, mapping):
        raise RuntimeError(
            f"failed to create {index_name} with its schema mapping; refusing to write — "
            "an index write now would dynamic-map the index."
        )
    logging.info(f"✓ Index {index_name} ensured to exist")


def send_to_elasticsearch(client, index_name, data, doc_id):
    """
    Send a single document to Elasticsearch.

    Args:
        client: Elasticsearch client
        index_name: Target index name
        data: Document to index
        doc_id: Unique document ID

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Add kibana_date field (Kibana-friendly timestamp)
        if "date" in data and not data.get("kibana_date"):
            data["kibana_date"] = data["date"]

        # Index document with explicit ID to enable deduplication
        client.index(index=index_name, id=doc_id, document=data)
        return True
    except Exception as e:
        logging.error(f"Failed to index document {doc_id}: {e}")
        return False


def generate_doc_id(data):
    """
    Generate unique document ID based on channel, date.

    Args:
        data: Document data

    Returns:
        str: MD5 hash as document ID
    """
    # Video retention docs include video_id — overwrites on re-import (retention is cumulative)
    video_id = data.get("video_id")
    # Country breakdown docs include country in the key to avoid colliding with channel totals
    country = data.get("country")
    if video_id:
        unique_key = f"{data.get('youtube_channel_id')}_{video_id}_{data.get('team_esid')}_retention"
    elif country:
        unique_key = f"{data.get('youtube_channel_id')}_{data.get('date')}_{country}"
    else:
        unique_key = f"{data.get('youtube_channel_id')}_{data.get('date')}"
    return hashlib.md5(  # nosemgrep: insecure-hash-algorithm-md5 - non-cryptographic doc ID fingerprint
        unique_key.encode()
    ).hexdigest()


def read_json_files(base_path="/app/data/youtube_analytics/channel_metrics"):
    """
    Generator that yields JSON file data from all channel/team directories.

    Args:
        base_path: Base directory containing channel metrics

    Yields:
        tuple: (file_path, data_dict)
    """
    if not os.path.exists(base_path):
        logging.warning(f"Channel metrics directory not found: {base_path}")
        return

    for root, dirs, files in os.walk(base_path):
        for file in files:
            if file.endswith(".json"):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r") as f:
                        data = json.load(f)
                        yield (file_path, data)
                except Exception as e:
                    logging.error(f"Failed to read {file_path}: {e}")


def main(params):
    """
    Main entry point from import.py.

    Args:
        params: Dict with command-line parameters
    """
    custom_logging.define("youtube-analytics-json-to-elastic")

    logging.info("Starting YouTube Analytics JSON to Elasticsearch indexing")

    # Get Elasticsearch client
    es_client = get_elasticsearch_client()

    # Track statistics
    total_files = 0
    indexed_count = 0
    skipped_count = 0
    error_count = 0
    skipped_orphans = 0
    indices_created = {}

    # one-shot team lookup to resolve team_esid (and drop orphans)
    # without a per-doc ES round-trip.
    # Phase B: narrow scan to YouTube-enabled teams (ESID-first; avoids
    # the wasted all-teams scan + cross-workspace collision WARNING).
    team_cache = build_team_lookup_cache(es_client, team_filter={"ds_youtube": "true"})

    # Process all JSON files
    for file_path, data in read_json_files():
        total_files += 1

        # ESID-keyed index name; drop docs with unresolvable team.
        team_esid = resolve_team_esid(
            team_cache,
            team_esid=data.get("team_esid"),
            team_title=data.get("team_title"),
        )
        if not team_esid:
            skipped_orphans += 1
            if skipped_orphans <= 5:
                logging.warning(
                    "youtube_analytics: dropping doc with unresolved team — " "team_esid=%r team_title=%r",
                    data.get("team_esid"),
                    data.get("team_title"),
                )
            continue

        index_name = build_data_index_name(team_esid, SOURCE_SLUG)

        # Ensure index exists (create if needed)
        if team_esid not in indices_created:
            create_youtube_analytics_index(es_client, index_name)
            indices_created[team_esid] = index_name

        # Generate unique document ID
        doc_id = generate_doc_id(data)

        # Index document
        if send_to_elasticsearch(es_client, index_name, data, doc_id):
            indexed_count += 1
            logging.debug(f"Indexed {file_path} → {index_name}")
        else:
            error_count += 1

        # Progress logging every 100 records
        if total_files % 100 == 0:
            logging.info(f"Progress: {indexed_count} indexed, {error_count} errors")

    # Summary — re-set prefix since es_helpers calls may have changed it
    custom_logging.define("youtube-analytics-json-to-elastic")
    logging.info(f"\n{'='*60}")
    logging.info(f"YouTube Analytics Indexing Complete")
    logging.info(f"{'='*60}")
    logging.info(f"Total JSON files processed: {total_files}")
    logging.info(f"Successfully indexed: {indexed_count}")
    logging.info(f"Errors: {error_count}")
    logging.info(f"Indices created/updated: {len(indices_created)}")
    if indices_created:
        logging.info(f"Index names: {', '.join(sorted(indices_created.values()))}")
    if skipped_orphans:
        logging.warning(
            f"Dropped {skipped_orphans} doc(s) with unresolvable team "
            f"(team_esid missing AND team_title did not match de_asset_teams)"
        )
    logging.info(f"{'='*60}")


if __name__ == "__main__":
    main({})
