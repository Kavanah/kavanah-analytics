"""index the DT staging files into per-team ES indices.

Action: ``python3 import.py disciple_tools send_json_to_elastic``

Reads the staging files written by ``fetch.py`` — already-mapped, minimised
documents — adds ``team_title`` and
bulk-upserts (by DT ID) into ``{team_esid}__disciple_tools`` (ESID-keyed
scheme). After indexing it folds in the existing ``organization_title``
enrichment and the geocache prewarm against each per-team index — the same
post-processing the legacy ``promote_staging.py`` ran against the shared alias.

a staging file is DELETED once every document in it indexed
cleanly; it is no longer archived to ``raw_imported/`` (a new full copy every
run, never purged). A file with a failed document is kept for the next run and,
failing that, for the retention sweep's raw-JSON purge. A legacy file that still
carries the raw REST ``posts`` (written) is mapped here, through
the same street-free geocoder, and then deleted the same way.
"""

from __future__ import annotations

import json
import logging
import os

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


def _docs_for(payload: dict, team_esid: str, team_title: str):
    """Yield the documents a staging file carries, with ``team_title`` set.

    Current files carry mapped ``docs``. A legacy file with raw
    REST ``posts`` is mapped here through the street-free coarse geocoder.
    """
    if "docs" in payload:
        for doc in payload.get("docs") or []:
            if isinstance(doc, dict) and doc.get("ID"):
                doc["team_esid"] = team_esid
                doc["team_title"] = team_title
                yield doc
        return
    for post in payload.get("posts") or []:
        doc = mapping.map_post(post, team_esid, team_title=team_title, geocode=_coarse_location)
        if doc is not None:
            yield doc


def _failed_doc_ids(errors: list) -> set[str]:
    """Ids of every document the bulk call reported as failed. Matched by id
    alone (not index): DT ids repeat across sites, so a collision can only keep
    a file that could have been deleted — never delete one that failed."""
    ids: set[str] = set()
    for err in errors or []:
        if not isinstance(err, dict):
            continue
        for item in err.values():
            if isinstance(item, dict):
                ids.add(str(item.get("_id") or ""))
    return ids


def _remove_staging_file(filename: str) -> None:
    """delete an indexed staging file (no ``raw_imported`` copy)."""
    os.remove(os.path.join(RAW_DIR, filename))


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
    processed_files: dict[str, set[str]] = {}  # filename -> ids it carried

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

            ids: set[str] = set()
            for doc in _docs_for(payload, team_esid, team_title):
                touched_indices.add(index_name)
                ids.add(str(doc["ID"]))
                yield {
                    "_op_type": "update",
                    "_index": index_name,
                    "_id": doc["ID"],
                    "doc": doc,
                    "doc_as_upsert": True,
                }
            logging.info("DT send: %s → %d doc(s) into %s", filename, len(ids), index_name)
            processed_files[filename] = ids

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

    # delete each staging file whose documents all indexed; keep the
    # rest (minimised) for the next run / the retention sweep's raw-JSON purge.
    failed_ids = _failed_doc_ids(errors)
    for filename, ids in processed_files.items():
        if ids & failed_ids:
            logging.warning("DT send: keeping %s — %d doc(s) failed to index", filename, len(ids & failed_ids))
            continue
        try:
            _remove_staging_file(filename)
        except OSError as e:
            logging.warning("DT send: could not remove %s: %s", filename, e)

    logging.info(
        "DT send: indexed %d doc(s) into %d team index(es). Errors: %d",
        success_count,
        len(touched_indices),
        len(errors),
    )
