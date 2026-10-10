"""
Index Chatwoot conversation JSON files into Elasticsearch.

Uses upsert by conversation_id — each run reflects the current state of
open/pending conversations.  ESID-keyed indices: {team_esid}__chatwoot_conversations.

Usage:
    python3 /app/python/import.py chatwoot send_conversations_to_elastic
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
    put_regular_index_template,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_cached

DATA_DIR = "/app/data/chatwoot/conversations"
SOURCE_SLUG = "chatwoot_conversations"

# mapping loaded from schemas/unified_schema_chatwoot_conversations.json
# (single source of truth). The same file feeds the per-source mapping
# template, so the per-team template, the explicit create, and the auto-create
# template can never disagree. ``{"properties": {...}}`` body — the es_helpers
# wrappers add the outer ``{"mappings": ...}``. Both consumers below
# (put_regular_index_template + create_index_if_not_exists) use this.
INDEX_MAPPING = get_schema_cached(SOURCE_SLUG)


def read_json_files(directory):
    for root, _dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "r") as f:
                        yield filepath, json.load(f)
                except json.JSONDecodeError as e:
                    logging.error(f"Error decoding {filepath}: {e}")


def make_doc_id(data):
    account_esid = data.get("chatwoot_account_esid", "unknown")
    conv_id = data.get("conversation_id", "unknown")
    return f"{account_esid}_{conv_id}"


def main(params):
    lib.logging.define("chatwoot-conversations-to-elastic")

    es = get_elasticsearch_client()
    if es is None:
        # a hard failure; a bare return exited 0.
        raise RuntimeError("Failed to connect to Elasticsearch.")

    indices_created = set()
    count = 0
    failed = 0
    skipped_orphans = 0

    # one-shot team lookup to resolve team_esid (and drop orphans).
    # Phase B: narrow scan to Chatwoot-enabled teams (ESID-first;
    # avoids the wasted all-teams scan + cross-workspace collision WARNING).
    team_cache = build_team_lookup_cache(es, team_filter={"ds_chatwoot": "true"})

    for filepath, data in read_json_files(DATA_DIR):
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
                    "chatwoot_conversations: dropping doc with unresolved team — " "team_esid=%r team_title=%r",
                    data.get("team_esid"),
                    data.get("team_title"),
                )
            continue

        index_name = build_data_index_name(team_esid, SOURCE_SLUG)
        doc_id = make_doc_id(data)

        if index_name not in indices_created:
            # Use exact index name (no wildcard) to avoid priority conflicts
            # with existing ES system templates.
            put_regular_index_template(es, index_name, INDEX_MAPPING)
            if not create_index_if_not_exists(es, index_name, INDEX_MAPPING):
                raise RuntimeError(
                    f"failed to create {index_name} with its schema mapping; refusing to write — "
                    "an index write now would dynamic-map the index."
                )
            logging.info(f"Index '{index_name}' ensured.")
            indices_created.add(index_name)

        try:
            es.index(index=index_name, id=doc_id, body=data)
            count += 1
        except Exception as e:  # noqa: BLE001 — counted; the run raises below
            logging.error(f"Failed to index {doc_id}: {e}")
            failed += 1

    # Re-set prefix since es_helpers calls may have changed it
    lib.logging.define("chatwoot-conversations-to-elastic")
    logging.info(f"Indexed {count} conversation document(s) to chatwoot_conversations indices.")
    if skipped_orphans:
        logging.warning(
            "chatwoot_conversations: total %d doc(s) dropped because team could not be "
            "resolved (team_esid missing AND team_title did not match de_asset_teams)",
            skipped_orphans,
        )
    if failed:
        # a doc that never reached ES used to leave the run at rc 0.
        raise RuntimeError(f"chatwoot_conversations: {failed} document(s) failed to index; see the log.")
