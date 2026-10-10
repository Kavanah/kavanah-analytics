"""
Google Ads App Campaign JSON to Elasticsearch Indexer

Reads JSON files created by get_campaign_data.py and indexes them to Elasticsearch.
Creates ESID-keyed indices following the pattern: {team_esid}__google_ads

Usage:
    python3 /app/python/import.py google_ads send_json_to_elastic
"""

import os
import json
import sys
import logging
import hashlib
import lib.logging
from lib.es_helpers import (
    build_data_index_name,
    build_team_lookup_cache,
    create_index_if_not_exists,
    get_elasticsearch_client,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_for_index_type

SOURCE_SLUG = "google_ads"


def create_google_ads_index(client, index_name):
    """
    Create or ensure Google Ads campaign index exists with proper mapping.

    Args:
        client: Elasticsearch client
        index_name: Name of the index to create
    """
    lib.logging.define("google-ads-json-to-elastic")

    # mapping loaded from schemas/unified_schema_google_ads.json (single
    # source of truth). It is the mappings *body* (``{"properties": {...}}``) —
    # ``create_index_if_not_exists`` adds the outer ``{"mappings": ...}`` wrapper.
    # do NOT wrap it again here (``{"mappings": {"mappings":...}}`` raised
    # a swallowed mapper_parsing_exception and fell back to dynamic mapping).
    mapping = get_schema_for_index_type(SOURCE_SLUG)
    if not create_index_if_not_exists(client, index_name, mapping):
        raise RuntimeError(
            f"failed to create {index_name} with its schema mapping; refusing to write — "
            "an index write now would dynamic-map the index."
        )
    logging.info(f"Index {index_name} ensured to exist or created successfully")


def send_to_elasticsearch(client, index_name, data, doc_id):
    """
    Send a single document to Elasticsearch.

    Args:
        client: Elasticsearch client
        index_name: Target index name
        data: Document to index
        doc_id: Unique document ID (for deduplication)
    """
    lib.logging.define("google-ads-json-to-elastic")
    try:
        # Use doc_id for idempotent indexing (prevents duplicates)
        client.index(index=index_name, body=data, id=doc_id)
    except Exception as e:
        logging.error(f"Failed to send document to {index_name}: {e}")


def read_json_files(directory):
    """
    Generator that yields JSON documents from files.

    Args:
        directory: Base directory to walk

    Yields:
        dict: Individual JSON document
    """
    lib.logging.define("google-ads-json-to-elastic")

    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "r") as file:
                        yield json.load(file)
                except json.JSONDecodeError as e:
                    logging.error(f"Failed to parse JSON from {filepath}: {e}")
                except Exception as e:
                    logging.error(f"Error reading {filepath}: {e}")


def main(params):
    """
    Main entry point from import.py.

    Reads JSON files from /app/data/google_ads/app_campaigns/ and indexes to Elasticsearch.

    Args:
        params: Dict of command-line parameters (unused)
    """
    lib.logging.define("google-ads-json-to-elastic")

    directory = "/app/data/google_ads/app_campaigns/"

    # Log start
    logging.info(f"Reading JSON files from {directory}")

    # Get Elasticsearch client
    es_client = get_elasticsearch_client()

    # one-shot team lookup so we resolve team_esid (and drop orphan
    # docs) without a per-doc ES round-trip.
    # narrow the scan to Google-Ads-enabled teams. Google Ads docs
    # carry a pre-resolved team_esid and resolve via by_esid (never the title
    # fallback), so an all-teams scan was wasted work that also fired the
    # cross-workspace title-collision WARNING for unrelated teams (e.g. the
    # 'test' slug). ds_google_ads is a keyword field with value "true".
    team_cache = build_team_lookup_cache(es_client, team_filter={"ds_google_ads": "true"})

    doc_count = 0
    skipped_orphans = 0
    index_count = {}

    for data in read_json_files(directory):
        # Set kibana_date field (use date field value)
        if "kibana_date" not in data or not data["kibana_date"]:
            data["kibana_date"] = data.get("date", "")

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
                    "google_ads: dropping doc with unresolved team — " "team_esid=%r team_title=%r",
                    data.get("team_esid"),
                    data.get("team_title"),
                )
            continue

        index_name = build_data_index_name(team_esid, SOURCE_SLUG)

        # Track index usage
        if index_name not in index_count:
            index_count[index_name] = 0

        # Ensure index exists with proper mapping (only once per index)
        if index_count[index_name] == 0:
            create_google_ads_index(es_client, index_name)

        # Generate unique document ID for deduplication.
        # Geo documents (record_type='geo') use criterion_id instead of ad_id.
        if data.get("record_type") == "geo":
            doc_id_string = (
                f"{data.get('google_ads_customer_id')}-"
                f"{data.get('campaign_id')}-geo-"
                f"{data.get('geo_criterion_id')}-"
                f"{data.get('date')}"
            )
        elif data.get("record_type") == "campaign_metadata":
            doc_id_string = f"{data.get('google_ads_customer_id')}-" f"{data.get('campaign_id')}-metadata"
        else:
            # campaign-grain perf docs: one doc per (customer, campaign,
            # date), no ad_id (the importer pulls FROM campaign now, not
            # ad_group_ad). Idempotent: a re-pull of the same campaign/day
            # overwrites the same ES doc.
            doc_id_string = f"{data.get('google_ads_customer_id')}-" f"{data.get('campaign_id')}-" f"{data.get('date')}"
        doc_id = hashlib.md5(  # nosemgrep: insecure-hash-algorithm-md5 - non-cryptographic doc ID fingerprint
            doc_id_string.encode()
        ).hexdigest()

        # Send to Elasticsearch
        send_to_elasticsearch(es_client, index_name, data, doc_id)

        doc_count += 1
        index_count[index_name] += 1

        if doc_count % 100 == 0:
            logging.info(f"Indexed {doc_count} documents so far...")

    # Log summary
    logging.info(f"Completed sending data to Elasticsearch")
    logging.info(f"Total documents indexed: {doc_count}")
    for index_name, count in index_count.items():
        logging.info(f"  {index_name}: {count} documents")
    if skipped_orphans:
        logging.warning(
            "google_ads: total %d doc(s) dropped because team could not be resolved "
            "(team_esid missing AND team_title sanitization did not match any team in "
            "de_asset_teams)",
            skipped_orphans,
        )


if __name__ == "__main__":
    # For standalone testing
    main({})
