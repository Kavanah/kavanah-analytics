"""map raw DT posts and index into per-team ES indices.

Action: ``python3 import.py disciple_tools send_json_to_elastic``

Reads the raw files written by ``fetch.py``, maps each DT post to the dashboard
document shape (``mapping.py``), and bulk-upserts (by DT ID) into
``{team_esid}__disciple_tools``. After indexing it
folds in the existing ``organization_title`` enrichment and the geocache prewarm
against each per-team index — the same post-processing the legacy
``promote_staging.py`` ran against the shared alias.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone

import lib.logging
from elasticsearch.helpers import bulk

from import_scripts.disciple_tools import mapping
from import_scripts.disciple_tools.prewarm_geocache import prewarm
from import_scripts.migration.backfill_organization_title import (
    load_team_organizations,
    process_index,
)
from lib.es_helpers import (
    build_team_lookup_cache,
    ensure_data_index,
    get_elasticsearch_client,
)
from migration.schema_loader import get_schema_for_index_type
from lib.geocoding import coarse_location_for_address as _coarse_location

RAW_DIR = "/app/data/disciple_tools/raw"
ARCHIVE_DIR = "/app/data/disciple_tools/raw_imported"
SOURCE_SLUG = "disciple_tools"
BULK_CHUNK_SIZE = 500


def _read_raw_files():
    for filename in sorted(os.listdir(RAW_DIR)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(RAW_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                yield filename, json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logging.error("DT send: could not read %s: %s", path, e)


def _team_title_for(team_cache, team_esid: str) -> str:
    return ((team_cache.get("by_esid") or {}).get(team_esid) or {}).get("team_title") or ""


def _archive(filename: str) -> None:
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    shutil.move(os.path.join(RAW_DIR, filename), os.path.join(ARCHIVE_DIR, f"{stamp}__{filename}"))


def main(params):
    lib.logging.define("dt-json-to-elastic")
    if not os.path.isdir(RAW_DIR):
        logging.info("DT send: no raw dir %s — nothing to do", RAW_DIR)
        return

    es = get_elasticsearch_client()
    if es is None:
        # an ES outage is a hard failure, not a successful no-op. Was a
        # bare return (rc 0), so a run that indexed nothing was recorded as a
        # SUCCESS. Unconditional: this is never a legitimate outcome.
        raise RuntimeError("DT send: failed to connect to Elasticsearch")

    schema = get_schema_for_index_type(SOURCE_SLUG)
    team_cache = build_team_lookup_cache(es)
    indices_created: dict[str, str] = {}
    touched_indices: set[str] = set()
    processed_files: list[str] = []

    def _actions():
        for filename, payload in _read_raw_files():
            team_esid = (payload or {}).get("team_esid") or ""
            if not team_esid:
                logging.warning("DT send: %s has no team_esid — skipping", filename)
                continue
            team_title = _team_title_for(team_cache, team_esid)

            if team_esid not in indices_created:
                indices_created[team_esid] = ensure_data_index(es, team_esid, SOURCE_SLUG, schema)
                logging.info("DT send: index %s ready", indices_created[team_esid])
            index_name = indices_created[team_esid]

            count = 0
            for post in payload.get("posts") or []:
                doc = mapping.map_post(post, team_esid, team_title=team_title, geocode=_coarse_location)
                if doc is None:
                    continue
                touched_indices.add(index_name)
                count += 1
                yield {
                    "_op_type": "update",
                    "_index": index_name,
                    "_id": doc["ID"],
                    "doc": doc,
                    "doc_as_upsert": True,
                }
            logging.info("DT send: %s → %d doc(s) into %s", filename, count, index_name)
            processed_files.append(filename)

    success_count, errors = bulk(es, _actions(), chunk_size=BULK_CHUNK_SIZE, raise_on_error=False)
    lib.logging.define("dt-json-to-elastic")  # es_helpers may have reset the prefix

    if errors:
        logging.error("DT send: %d bulk error(s):", len(errors))
        for err in errors[:10]:
            logging.error("  %s", err)

    # Post-processing: organization_title enrichment + geocache prewarm per index.
    if touched_indices:
        team_org_map = load_team_organizations(es) or {}
        for index_name in sorted(touched_indices):
            try:
                es.indices.refresh(index=index_name)
                process_index(es, index_name, team_org_map)
            except Exception as e:
                logging.warning("DT send: org enrichment on %s failed: %s", index_name, e)
            try:
                prewarm(es, index_name=index_name)
            except Exception as e:
                logging.warning("DT send: geocache prewarm on %s failed: %s", index_name, e)
        lib.logging.define("dt-json-to-elastic")

    for filename in processed_files:
        try:
            _archive(filename)
        except OSError as e:
            logging.warning("DT send: could not archive %s: %s", filename, e)

    logging.info(
        "DT send: indexed %d doc(s) into %d team index(es). Errors: %d",
        success_count,
        len(touched_indices),
        len(errors),
    )
