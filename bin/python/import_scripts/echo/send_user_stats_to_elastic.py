import os
import json
import logging
import re

import lib.logging
from lib.es_helpers import get_elasticsearch_client, create_index_if_not_exists, put_regular_index_template
from migration.schema_loader import get_schema_for_index_type
from elasticsearch.helpers import bulk

BULK_CHUNK_SIZE = 500


def get_index_name(data):
    """Return the per-tenant ES index name: {subdomain}_echo_user_stats."""
    subdomain = data.get("echo_tenant_subdomain", "unknown")
    sanitized = re.sub(r"[^a-z0-9_]", "_", subdomain.lower())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_") or "unknown"
    return f"{sanitized}_echo_user_stats"


def read_json_files_recursively(directory):
    """Recursively yield JSON data from files in the directory."""
    lib.logging.define("echo-user-stats-to-elastic")
    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "r") as f:
                        yield json.load(f)
                except json.JSONDecodeError as e:
                    logging.error(f"Error decoding JSON from file {filepath}: {e}")


def generate_bulk_actions(documents, indices_created, es, schema):
    """Yield bulk upsert actions, creating per-tenant indices on first encounter."""
    for data in documents:
        index_name = get_index_name(data)
        if index_name not in indices_created:
            # get_schema_for_index_type returns the mappings body itself
            # ({"properties": ...}); there is no "mappings" key. The old
            # schema.get("mappings", {}) handed ES an EMPTY mapping, so every
            # echo user-stats index got its field types from the *_echo_user_stats
            # template (if installed) or from dynamic mapping — and the
            # raced-create verification had zero fields to compare, so it always
            # blessed the race winner. Pass the schema verbatim, as the other
            # importers do.
            put_regular_index_template(es, index_name, schema)
            if not create_index_if_not_exists(es, index_name, schema):
                raise RuntimeError(
                    f"failed to create {index_name} with its schema mapping; refusing to write — "
                    "an index write now would dynamic-map the index."
                )
            indices_created.add(index_name)
            logging.info(f"Ensured index {index_name} exists.")

        doc_id = data.get("id")
        yield {
            "_op_type": "update",
            "_index": index_name,
            "_id": doc_id,
            "doc": data,
            "doc_as_upsert": True,
        }


def main(params):
    lib.logging.define("echo-user-stats-to-elastic")

    base_directory = "/app/data/echo/user_stats"
    logging.info(f"Starting Echo user stats import from {base_directory}")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return

    schema = get_schema_for_index_type("echo_user_stats")

    indices_created = set()
    documents = read_json_files_recursively(base_directory)
    actions = generate_bulk_actions(documents, indices_created, es, schema)

    success_count, errors = bulk(es, actions, chunk_size=BULK_CHUNK_SIZE, raise_on_error=False)

    if errors:
        logging.error(f"Bulk indexing completed with {len(errors)} errors:")
        for err in errors[:10]:
            logging.error(f"  {err}")

    logging.info(f"Completed importing {success_count} Echo user stat documents. Errors: {len(errors)}")
