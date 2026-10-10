"""
Bitly Link Clicks JSON to Elasticsearch Indexer

Reads JSON files created by get_link_clicks.py and indexes them to
Elasticsearch. Creates ESID-keyed indices following the pattern:
{team_esid}__bitly_clicks

Usage:
    python3 /app/python/import.py bitly send_json_to_elastic
"""

import json
import logging
import os

import lib.logging
from lib.es_helpers import (
    build_data_index_name,
    build_team_lookup_cache,
    create_index_if_not_exists,
    get_elasticsearch_client,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_for_index_type

SOURCE_SLUG = "bitly_clicks"

DATA_DIR = "/app/data/bitly/link_clicks/"


def create_bitly_clicks_index(client, index_name):
    """
    Create or ensure the Bitly clicks index exists with proper mapping.

    Args:
        client: Elasticsearch client
        index_name: Name of the index to create
    """
    lib.logging.define("bitly-json-to-elastic")

    # Mapping loaded from schemas/unified_schema_bitly_clicks.json (single
    # source of truth pattern). It is the mappings *body*
    # (``{"properties": {...}}``) — ``create_index_if_not_exists`` adds the
    # outer ``{"mappings":...}`` wrapper (do NOT wrap it again).
    mapping = get_schema_for_index_type(SOURCE_SLUG)
    if not create_index_if_not_exists(client, index_name, mapping):
        raise RuntimeError(
            f"failed to create {index_name} with its schema mapping; refusing to write — "
            "an index write now would dynamic-map the index."
        )
    logging.info(f"Index {index_name} ensured to exist or created successfully")


def build_doc_id(link_id, date):
    """Deterministic ES _id for a (link, day) doc: ``{link_id}_{date}`` with
    the bitlink id's slash sanitized (``bit.ly/abc`` → ``bit.ly_abc``), so a
    re-run of the same window upserts instead of duplicating."""
    sanitized = str(link_id or "").replace("/", "_")
    return f"{sanitized}_{date}"


def send_to_elasticsearch(client, index_name, data, doc_id):
    """
    Send a single document to Elasticsearch.

    Args:
        client: Elasticsearch client
        index_name: Target index name
        data: Document to index
        doc_id: Unique document ID (for deduplication)
    """
    lib.logging.define("bitly-json-to-elastic")
    try:
        # Use doc_id for idempotent indexing (prevents duplicates)
        client.index(index=index_name, body=data, id=doc_id)
    except Exception as e:
        logging.error(f"Failed to send document to {index_name}: {e}")


def read_json_files(directory):
    """
    Generator that yields JSON documents from files.

    get_link_clicks.py writes ONE file per run containing a LIST of docs, so a
    list payload is unrolled; a single-dict payload (hand-crafted repair files)
    also works.

    Args:
        directory: Base directory to walk

    Yields:
        dict: Individual JSON document
    """
    lib.logging.define("bitly-json-to-elastic")

    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "r") as file:
                        payload = json.load(file)
                    if isinstance(payload, list):
                        for doc in payload:
                            yield doc
                    else:
                        yield payload
                except json.JSONDecodeError as e:
                    logging.error(f"Failed to parse JSON from {filepath}: {e}")
                except Exception as e:
                    logging.error(f"Error reading {filepath}: {e}")


def main(params):
    """
    Main entry point from import.py.

    Reads JSON files from /app/data/bitly/link_clicks/ and indexes to
    Elasticsearch.

    Args:
        params: Dict of command-line parameters (unused)
    """
    lib.logging.define("bitly-json-to-elastic")

    logging.info(f"Reading JSON files from {DATA_DIR}")

    es_client = get_elasticsearch_client()

    # one-shot team lookup so we resolve team_esid (and drop
    # orphan docs) without a per-doc ES round-trip. Bitly docs carry a
    # pre-resolved team_esid from the config, so resolution is by_esid.
    team_cache = build_team_lookup_cache(es_client)

    doc_count = 0
    skipped_orphans = 0
    index_count = {}

    for data in read_json_files(DATA_DIR):
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
                    "bitly: dropping doc with unresolved team — team_esid=%r team_title=%r",
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
            create_bitly_clicks_index(es_client, index_name)

        doc_id = build_doc_id(data.get("link_id"), data.get("date"))

        send_to_elasticsearch(es_client, index_name, data, doc_id)

        doc_count += 1
        index_count[index_name] += 1

        if doc_count % 100 == 0:
            logging.info(f"Indexed {doc_count} documents so far...")

    logging.info("Completed sending data to Elasticsearch")
    logging.info(f"Total documents indexed: {doc_count}")
    for index_name, count in index_count.items():
        logging.info(f"  {index_name}: {count} documents")
    if skipped_orphans:
        logging.warning(
            "bitly: total %d doc(s) dropped because team could not be resolved "
            "(team_esid missing AND team_title did not match any team in de_asset_teams)",
            skipped_orphans,
        )


if __name__ == "__main__":
    # For standalone testing
    main({})
