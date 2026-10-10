import json
import logging
import os

import lib.logging
from lib.es_helpers import (
    build_data_index_name,
    build_team_lookup_cache,
    create_index_if_not_exists,
    ensure_mapping_fields,
    get_elasticsearch_client,
    put_regular_index_template,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_cached

DATA_DIR = "/app/data/chatwoot/reports"
SOURCE_SLUG = "chatwoot_insights"

# mapping loaded from schemas/unified_schema_chatwoot_insights.json
# (single source of truth). The same file feeds the per-source mapping
# template, so the per-team template, the explicit create, and the auto-create
# template can never disagree. ``{"properties": {...}}`` body — the es_helpers
# wrappers add the outer ``{"mappings": ...}``. Both consumers below
# (put_regular_index_template + create_index_if_not_exists) use this.
INDEX_MAPPING = get_schema_cached(SOURCE_SLUG)


def read_json_files(directory, dates=None):
    """Yield (filepath, data) for JSON files under directory.

    If dates is provided (a set of YYYY-MM-DD strings), only files whose
    filename matches one of those dates are yielded.
    """
    for root, _dirs, files in os.walk(directory):
        for filename in files:
            if not filename.endswith(".json"):
                continue
            if dates is not None:
                date_stem = os.path.splitext(filename)[0]
                if date_stem not in dates:
                    continue
            filepath = os.path.join(root, filename)
            try:
                with open(filepath, "r") as f:
                    yield filepath, json.load(f)
            except json.JSONDecodeError as e:
                logging.error(f"Error decoding JSON from {filepath}: {e}")


def make_doc_id(data):
    """Generate a stable document ID from account ESID (+ inbox id for a
    per-inbox row) + date."""
    account_esid = data.get("chatwoot_account_esid", "unknown")
    date = data.get("date", "unknown")
    inbox_id = data.get("inbox_id")
    if inbox_id is not None:
        return f"{account_esid}_{inbox_id}_{date}"
    return f"{account_esid}_{date}"


def _dates_in_range(start_date_str, end_date_str):
    from datetime import datetime, timedelta

    start = datetime.strptime(start_date_str, "%Y-%m-%d")
    end = datetime.strptime(end_date_str, "%Y-%m-%d")
    current = start
    while current <= end:
        yield current.strftime("%Y-%m-%d")
        current += timedelta(days=1)


def main(params):
    lib.logging.define("chatwoot-json-to-elastic")

    # Build a filter set of dates so we only index files written this run.
    # Without date params, fall back to indexing everything (backfill/manual use).
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    specific_dates = params.get("dates")  # comma-separated list
    single_date = params.get("date")

    if start_date and end_date:
        dates = set(_dates_in_range(start_date, end_date))
    elif specific_dates and specific_dates is not True:
        dates = {d.strip() for d in str(specific_dates).split(",") if d.strip()}
    elif single_date:
        dates = {single_date}
    else:
        dates = None  # no filter — index all files

    if dates:
        logging.info(f"Filtering to {len(dates)} date(s): {sorted(dates)}")
    else:
        logging.info(f"No date filter — indexing all files in {DATA_DIR}")

    logging.info(f"Starting Chatwoot report import from {DATA_DIR}")

    es = get_elasticsearch_client()
    if es is None:
        # a hard failure; a bare return exited 0.
        raise RuntimeError("Failed to connect to Elasticsearch.")

    indices_created = set()
    # contact checkbox counts are named after Chatwoot's attribute keys
    # (contact_attr_<key>), so they cannot be listed in the schema up front; each
    # new one is mapped integer, add-only, before the first doc that carries it.
    attr_fields_mapped = set()
    count = 0
    failed = 0
    skipped_orphans = 0

    # one-shot team lookup to resolve team_esid (and drop orphans).
    # Phase B: narrow scan to Chatwoot-enabled teams (ESID-first;
    # avoids the wasted all-teams scan + cross-workspace collision WARNING).
    team_cache = build_team_lookup_cache(es, team_filter={"ds_chatwoot": "true"})

    for filepath, data in read_json_files(DATA_DIR, dates=dates):
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
                    "chatwoot_insights: dropping doc with unresolved team — " "team_esid=%r team_title=%r",
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
            # indices created before the inbox fields existed get them
            # add-only, so they map as the schema says rather than dynamically.
            ensure_mapping_fields(es, index_name, INDEX_MAPPING)
            logging.info(f"Index '{index_name}' ensured.")
            indices_created.add(index_name)

        new_attr_fields = {k for k in data if k.startswith("contact_attr_") and (index_name, k) not in attr_fields_mapped}
        if new_attr_fields:
            ensure_mapping_fields(es, index_name, {"properties": {k: {"type": "integer"} for k in new_attr_fields}})
            attr_fields_mapped.update((index_name, k) for k in new_attr_fields)

        try:
            es.index(index=index_name, id=doc_id, body=data)
            logging.info(f"Indexed {doc_id} into {index_name}")
            count += 1
        except Exception as e:  # noqa: BLE001 — counted; the run raises below
            logging.error(f"Failed to index document {doc_id}: {e}")
            failed += 1

    logging.info(f"Completed indexing {count} Chatwoot report document(s).")
    if skipped_orphans:
        logging.warning(
            "chatwoot_insights: total %d doc(s) dropped because team could not be "
            "resolved (team_esid missing AND team_title did not match de_asset_teams)",
            skipped_orphans,
        )
    if failed:
        # a doc that never reached ES used to leave the run at rc 0.
        raise RuntimeError(f"chatwoot_insights: {failed} document(s) failed to index; see the log.")
