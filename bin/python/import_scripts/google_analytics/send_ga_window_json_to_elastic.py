"""
GA4 windowed-metrics JSON → Elasticsearch indexer.

Sibling of ``send_json_to_elastic.py``. Reads the property-level window docs
written by ``get_ga_metrics_window.py`` and indexes them into
``{team_esid}__ga_metrics_window`` (one row per property per pulled window).

Reuses the per-day sender's machinery (team resolution, idempotent indexing by
``_doc_id``); only the source directory + SOURCE_SLUG differ. The window doc is
a subset of the ga_metrics fields plus ``insight_window_*``, so the existing
``ga_metrics`` schema (a superset) is reused for the mapping.
"""

import logging

import lib.logging
from lib.es_helpers import (
    build_team_lookup_cache,
    ensure_data_index,
    get_elasticsearch_client,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_for_index_type
from import_scripts.google_analytics.send_json_to_elastic import (
    read_json_files,
    send_to_elasticsearch,
)

SOURCE_SLUG = "ga_metrics_window"
_WINDOW_DIR = "/app/data/google_analytics/ga_metrics_window/"


def missing_schema_fields(live_properties, schema):
    """the schema fields an EXISTING index's live mapping lacks.

    Pure. ``live_properties`` is the index's current ``mappings.properties``;
    ``schema`` is the unified schema (``{"properties": {...}}``). Returns the
    subset of schema properties whose name is absent from the live mapping —
    add-only, never a type change (put_mapping rejects those anyway).
    """
    live = set((live_properties or {}).keys())
    return {name: spec for name, spec in (schema.get("properties") or {}).items() if name not in live}


def ensure_schema_fields_mapped(es_client, index_name, schema):
    """add-only ``put_mapping`` of schema fields missing from a
    pre-existing window index.

    The per-source template only applies when an index is CREATED, so
    a field added to the schema later (``grain``, ``eventCountPerUser``) would be
    dynamically mapped on the first doc that carries it — ``grain`` as
    text+keyword (the class), where the read path's ``term`` filter on
    the bare field name is unreliable. The alternative, a manual update_vN
    migration, is the deploy step CLAUDE.md records as silently skipped on prod
    . Doing it here makes the sender self-healing and idempotent:
    a no-op when nothing is missing. Fail-soft — a mapping failure is logged,
    never fatal to the send.
    """
    try:
        live = es_client.indices.get_mapping(index=index_name)
        props = {}
        for _idx, body in (live or {}).items():
            props.update(((body or {}).get("mappings") or {}).get("properties") or {})
        to_add = missing_schema_fields(props, schema)
        if not to_add:
            return set()
        es_client.indices.put_mapping(index=index_name, body={"properties": to_add})
        logging.info("ga-window: mapped %d missing field(s) on %s: %s", len(to_add), index_name, sorted(to_add))
        return set(to_add)
    except Exception as exc:  # noqa: BLE001 — boundary: a mapping heal must never fail the send
        logging.warning("ga-window: put_mapping on %s failed: %s — continuing.", index_name, exc)
        return set()


def main(params):
    lib.logging.define("ga-window-json-to-elastic")
    logging.info("ga-window send: reading from %s", _WINDOW_DIR)
    logging.info("Using ESID-keyed indices (pattern: {team_esid}__ga_metrics_window)")

    es_client = get_elasticsearch_client()
    # load the ga_metrics_window schema (a dedicated copy of ga_metrics)
    # so this index resolves the SAME mapping the per-source template
    # installs for the *_ga_metrics_window pattern. Was "ga_metrics" — but that
    # pattern (*_ga_metrics) does NOT match *_ga_metrics_window, so the window
    # index had no mapping template and could lose the create-index race.
    schema = get_schema_for_index_type("ga_metrics_window")
    # Phase B: narrow scan to GA-enabled teams (ESID-first; avoids the
    # wasted all-teams scan + cross-workspace collision WARNING).
    team_cache = build_team_lookup_cache(es_client, team_filter={"ds_google_analytics": "true"})

    indices_created = {}
    doc_count = 0
    error_count = 0
    skipped_orphans = 0

    for data in read_json_files(_WINDOW_DIR):
        doc_id = data.get("_doc_id")
        doc_data = {k: v for k, v in data.items() if k != "_doc_id"}

        team_esid = resolve_team_esid(
            team_cache,
            team_esid=data.get("team_esid"),
            team_title=data.get("team_title"),
        )
        if not team_esid:
            skipped_orphans += 1
            if skipped_orphans <= 5:
                logging.warning(
                    "ga_metrics_window: dropping doc with unresolved team — team_esid=%r team_title=%r",
                    data.get("team_esid"),
                    data.get("team_title"),
                )
            continue

        if team_esid not in indices_created:
            indices_created[team_esid] = ensure_data_index(es_client, team_esid, SOURCE_SLUG, schema)
            logging.info("Created team window index: %s", indices_created[team_esid])
            # heal a pre-existing index's mapping for fields added to
            # the schema after it was created (grain, eventCountPerUser).
            ensure_schema_fields_mapped(es_client, indices_created[team_esid], schema)

        if send_to_elasticsearch(es_client, indices_created[team_esid], doc_data, doc_id):
            doc_count += 1
        else:
            error_count += 1

    logging.info("Completed sending window data to %d team-specific index(es)", len(indices_created))
    logging.info("Total window documents indexed: %d", doc_count)
    if error_count:
        logging.error("Failed to index %d window document(s)", error_count)
    if skipped_orphans:
        logging.warning("ga_metrics_window: %d doc(s) dropped (team unresolved)", skipped_orphans)


if __name__ == "__main__":
    main({})
