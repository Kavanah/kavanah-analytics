"""index step: write one run's staged TikTok snapshots.

    python3 /app/python/import.py tiktok send_json_to_elastic --run_name=<name>

Docs go to ``{team_esid}__tiktok_organic`` under a deterministic id (record
type + account + video + date), so re-running a day overwrites that day's
snapshot instead of duplicating it. Any failed write fails the step: a missing
snapshot would make the next day's gains span two days.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

import lib.logging as custom_logging
from lib.es_helpers import (
    build_data_index_name,
    build_team_lookup_cache,
    create_index_if_not_exists,
    get_elasticsearch_client,
    resolve_team_esid,
)
from lib.tiktok_constants import SOURCE_SLUG
from migration.schema_loader import get_schema_for_index_type


def build_doc_id(doc: dict) -> str:
    key = "|".join(str(doc.get(k) or "") for k in ("record_type", "tiktok_open_id", "video_id", "date"))
    return hashlib.md5(  # nosemgrep: insecure-hash-algorithm-md5 - non-cryptographic doc ID fingerprint
        key.encode("utf-8")
    ).hexdigest()


def _ensure_index(es, index_name: str) -> None:
    # The mappings body; create_index_if_not_exists adds the wrapper.
    if not create_index_if_not_exists(es, index_name, get_schema_for_index_type(SOURCE_SLUG)):
        raise RuntimeError(f"failed to create {index_name} with its schema mapping; refusing to dynamic-map it")


def main(params):
    custom_logging.define("tiktok-json-to-elastic")

    from import_scripts.tiktok.run_files import STAGING_BASE, run_dir

    path = run_dir(STAGING_BASE, params.get("run_name"))
    if not os.path.isdir(path):
        logging.info("tiktok: nothing staged at %s", path)
        return None

    es = get_elasticsearch_client()
    if es is None:
        raise RuntimeError("tiktok: could not connect to Elasticsearch")
    team_cache = build_team_lookup_cache(es)

    ensured: set[str] = set()
    indexed, failed, orphans = 0, 0, 0
    for filename in sorted(os.listdir(path)):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(path, filename)) as f:
            docs = json.load(f)
        for doc in docs:
            team_esid = resolve_team_esid(team_cache, team_esid=doc.get("team_esid"), team_title=doc.get("team_title"))
            if not team_esid:
                orphans += 1
                continue
            index_name = build_data_index_name(team_esid, SOURCE_SLUG)
            if index_name not in ensured:
                _ensure_index(es, index_name)
                ensured.add(index_name)
            try:
                es.index(index=index_name, id=build_doc_id(doc), document=doc)
                indexed += 1
            except Exception as e:  # noqa: BLE001 — counted and fails the step below
                failed += 1
                logging.error("tiktok: write to %s failed: %s", index_name, e)

    logging.info("Successfully indexed: %d", indexed)
    if orphans:
        logging.error("tiktok: dropped %d doc(s) whose team no longer exists", orphans)
    if failed:
        logging.error("tiktok: %d doc(s) failed to index", failed)
        return False
    return None
