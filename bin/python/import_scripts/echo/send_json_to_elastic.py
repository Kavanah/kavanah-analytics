import os
import re
import json
import logging
from datetime import datetime
import lib.logging  # Import your logging library
from lib.es_helpers import (
    build_team_lookup_cache,
    ensure_data_index,
    get_elasticsearch_client,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_for_index_type
from elasticsearch.helpers import bulk

BULK_CHUNK_SIZE = 500
SOURCE_SLUG = "echo_conversations"

# Echo's API delivers ``created_at`` as e.g. "April 27, 2026 05:23:46 UTC".
# kibana_date must match every other platform's ``yyyy-MM-dd`` so date-based
# aggregations and the cleanup tooling work uniformly.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ECHO_FREE_TEXT_FORMAT = "%B %d, %Y %H:%M:%S UTC"


def _to_iso_date(raw):
    """Return YYYY-MM-DD if ``raw`` is parseable, else None.

    Accepts strings already in ISO form (idempotent), strings in Echo's
    free-text English-month form, and ISO 8601 timestamps. Returns None for
    any other input — caller decides whether to skip the doc.
    """
    if not raw or not isinstance(raw, str):
        return None
    if _ISO_DATE_RE.match(raw):
        return raw
    try:
        return datetime.strptime(raw, _ECHO_FREE_TEXT_FORMAT).strftime("%Y-%m-%d")
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def read_json_files_recursively(directory):
    """Recursively yield JSON data from files in the directory."""
    lib.logging.define("echo-json-to-elastic")
    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "r") as file:
                        yield json.load(file)
                except json.JSONDecodeError as e:
                    logging.error(f"Error decoding JSON from file {filepath}: {e}")


def generate_bulk_actions(documents, indices_created, es, schema, current_date_formatted, team_cache):
    """Yield bulk upsert actions, creating ESID-keyed indices on first encounter.

    Echo docs carry only ``team_title`` (no ``team_esid``), so the
    team is resolved by sanitized-title lookup against ``de_asset_teams``.
    The index name is now ``{team_esid}__echo_conversations``. Docs whose team
    can't be resolved — including the former ``unknown_echo_conversations``
    bucket — are dropped with a warning rather than routed to an opaque index.
    """
    skipped_orphans = 0
    for data in documents:
        if "expiration" not in data or not data["expiration"]:
            data["expiration"] = current_date_formatted

        # kibana_date must be ISO yyyy-MM-dd. Parse from created_at
        # (Echo's free-text English-month format), with a fallback to createdAt
        # (ISO 8601 from the GraphQL endpoint). If neither parses, log and skip
        # the doc — don't crash the import on a malformed source date.
        existing = data.get("kibana_date")
        iso_date = _to_iso_date(existing) if existing else None
        if iso_date is None:
            iso_date = _to_iso_date(data.get("created_at")) or _to_iso_date(data.get("createdAt"))
        if iso_date is None:
            logging.warning(
                "Echo doc id=%s team_title=%r dropped: "
                "could not derive ISO kibana_date from "
                "kibana_date=%r, created_at=%r, createdAt=%r",
                data.get("id"),
                data.get("team_title"),
                existing,
                data.get("created_at"),
                data.get("createdAt"),
            )
            continue
        data["kibana_date"] = iso_date

        team_esid = resolve_team_esid(team_cache, team_title=data.get("team_title"))
        if not team_esid:
            skipped_orphans += 1
            if skipped_orphans <= 5:
                logging.warning(
                    "echo_conversations: dropping doc id=%s with unresolved team — " "team_title=%r",
                    data.get("id"),
                    data.get("team_title"),
                )
            continue

        if team_esid not in indices_created:
            index_name = ensure_data_index(es, team_esid, SOURCE_SLUG, schema)
            indices_created[team_esid] = index_name
            logging.info(f"Created Echo index: {index_name}")
            # ensure_data_index only CREATES; an index that predates
            # the fb_page_id schema field never learns it and
            # the first doc would dynamic-map it text+keyword — a mixed mapping
            # across the DV pattern that breaks the per-page
            # composite. Adding a NEW field via put_mapping is a safe merge on
            # every index, old or new; once per index per run.
            try:
                es.indices.put_mapping(index=index_name, properties={"fb_page_id": {"type": "keyword"}})
            except Exception as exc:  # noqa: BLE001 — mapping merge is best-effort; docs still land
                logging.warning("fb_page_id put_mapping failed for %s: %s", index_name, exc)
        else:
            index_name = indices_created[team_esid]

        doc_id = data.get("id")
        if isinstance(doc_id, list):
            doc_id = doc_id[0]

        yield {
            "_op_type": "update",
            "_index": index_name,
            "_id": doc_id,
            "doc": data,
            "doc_as_upsert": True,
        }

    if skipped_orphans:
        logging.warning(
            "echo_conversations: total %d doc(s) dropped because team_title did not "
            "resolve to a team in de_asset_teams (former unknown_echo_conversations bucket)",
            skipped_orphans,
        )


def main(params):
    lib.logging.define("echo-json-to-elastic")

    base_directory = "/app/data/echo/conversations"

    logging.info(f"Starting import process for Echo conversations in {base_directory}")
    logging.info("Using ESID-keyed indices (pattern: {team_esid}__echo_conversations)")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return

    indices_created = {}
    schema = get_schema_for_index_type("echo_conversations")
    current_date_formatted = datetime.utcnow().strftime("%B %d, %Y %H:%M:%S UTC")
    team_cache = build_team_lookup_cache(es)

    documents = read_json_files_recursively(base_directory)
    actions = generate_bulk_actions(documents, indices_created, es, schema, current_date_formatted, team_cache)

    success_count, errors = bulk(es, actions, chunk_size=BULK_CHUNK_SIZE, raise_on_error=False)

    # Re-set prefix since es_helpers calls may have changed it
    lib.logging.define("echo-json-to-elastic")

    if errors:
        logging.error(f"Bulk indexing completed with {len(errors)} errors:")
        for err in errors[:10]:
            logging.error(f"  {err}")

    logging.info(
        f"Completed importing {success_count} Echo conversations to "
        f"{len(indices_created)} team-specific indices. Errors: {len(errors)}"
    )
