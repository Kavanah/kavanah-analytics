"""
Bulk-load windowed FB ads insight JSON files into Elasticsearch.

Reads from ``/app/data/facebook/ad_insights_window/`` and writes to
``{team_esid}__fb_insights_window`` indices (ESID-keyed). (Originally modeled
on the retired weekly loader, ``send_weekly_json_to_elastic.py`` — removed in
.)

It sets a deterministic ``_id`` so a re-pull of the same window upserts the
existing doc rather than stacking duplicates. The ``_id`` includes the window
bounds AND the publisher_platform so per-platform docs
for the same ad+window don't collide.

Files stay in ``ad_insights_window/`` and get overwritten in place on re-pull —
the deterministic ``_id`` makes ES the source of truth, not filesystem state.
"""

import json
import logging
import os

import lib.logging
from elasticsearch.helpers import bulk
from lib.es_helpers import (
    build_team_lookup_cache,
    ensure_data_index,
    get_elasticsearch_client,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_for_index_type

BULK_CHUNK_SIZE = 500
DATA_DIR = "/app/data/facebook/ad_insights_window/"
SOURCE_SLUG = "fb_insights_window"


def read_json_files(directory):
    lib.logging.define("fb-window-json-to-elastic")
    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                with open(filepath, "r") as file:
                    yield json.load(file)


def generate_bulk_actions(documents, indices_created, es_client, schema, team_cache, scope_esids=None):
    """Yield bulk index actions, creating ESID-keyed indices on first encounter.

    Index name is ``{team_esid}__fb_insights_window``. Docs that can't be
    resolved to a known team_esid are skipped with a warning rather than routed
    to an ``unknown_*`` index (mirrors the weekly loader).

    ``scope_esids`` is the resolved ``--team_ids`` scope (a set of
    team ESIDs). When set, docs whose resolved ``team_esid`` is NOT in scope are
    dropped, so a scoped run does not sweep up another team's leftover window
    JSON in the shared dir (cross-workspace leak; sibling of). ``None``
    (default) = unscoped.
    """
    skipped_orphans = 0
    skipped_out_of_scope = 0
    for data in documents:
        if "kibana_date" not in data or not data["kibana_date"]:
            data["kibana_date"] = data.get("insight_window_start")

        team_esid = resolve_team_esid(
            team_cache,
            team_esid=data.get("team_esid"),
            team_title=data.get("team_title"),
        )
        if not team_esid:
            skipped_orphans += 1
            if skipped_orphans <= 5:
                logging.warning(
                    "fb_insights_window: dropping doc with unresolved team — team_esid=%r team_title=%r",
                    data.get("team_esid"),
                    data.get("team_title"),
                )
            continue

        # drop docs outside this run's team scope (foreign-team
        # leftovers in the shared dir). ESIDs are case-sensitive raw team _ids.
        if scope_esids is not None and team_esid not in scope_esids:
            skipped_out_of_scope += 1
            continue

        if team_esid not in indices_created:
            index_name = ensure_data_index(es_client, team_esid, SOURCE_SLUG, schema)
            indices_created[team_esid] = index_name
            logging.info(f"Created team window index: {index_name}")
        else:
            index_name = indices_created[team_esid]

        # Deterministic _id so a re-pull of the same window upserts in place.
        # ad-set grain: keyed by ad set + window bounds + publisher_platform
        # so per-platform docs for the same ad-set+window don't collide.
        adset_id = data.get("adset_id", "unknown")
        window_start = data.get("insight_window_start", "unknown")
        window_end = data.get("insight_window_end", "unknown")
        publisher_platform = data.get("publisher_platform")
        base_id = f"{adset_id}_{window_start}_{window_end}"
        # grain="page" docs carry no adset_id, so they all fell into
        # the "unknown_{window}" id — colliding with each other AND the team doc
        # (same shape), last-writer-wins: page docs silently vanished from ES.
        # Key them by their page_id instead. Ad-set and team ids are UNCHANGED
        # (existing prod docs keep their ids; a re-pull still upserts in place).
        # grain="ad" docs CARRY an adset_id (their parent), so the
        # default adset-keyed id would COLLIDE with the ad set's own doc for the
        # same window+platform — last-writer-wins would silently destroy ad-set
        # docs. Key ad docs by their ad_id. Checked FIRST so the adset default
        # below stays byte-identical for real ad-set docs.
        if data.get("grain") == "ad":
            base_id = f"ad_{data.get('ad_id', 'unknown')}_{window_start}_{window_end}"
        # asset docs also carry an adset_id (their parent, or the
        # "all" marginal sentinel); same collision class as the ad grain. Keyed
        # by (adset, video asset) so cells and marginals stay distinct.
        elif data.get("grain") == "asset":
            base_id = (
                f"asset_{data.get('adset_id', 'unknown')}_{data.get('video_asset_id', 'unknown')}"
                f"_{window_start}_{window_end}"
            )
        elif data.get("grain") == "page":
            base_id = f"page_{data.get('page_id', 'unknown')}_{window_start}_{window_end}"
        # grain="demographics" docs carry no adset_id either; key
        # them by their (age, gender) cell so cells don't collide with each
        # other, the team doc, or the page docs (same last-writer-wins hazard
        # the page grain hit).
        elif data.get("grain") == "demographics":
            base_id = f"demo_{data.get('age', 'unknown')}_{data.get('gender', 'unknown')}_{window_start}_{window_end}"
        doc_id = f"{base_id}_{publisher_platform}" if publisher_platform else base_id

        yield {
            "_op_type": "index",
            "_index": index_name,
            "_id": doc_id,
            "_source": data,
        }

    if skipped_orphans:
        logging.warning(
            "fb_insights_window: total %d doc(s) dropped because team could not be "
            "resolved (team_esid missing AND team_title sanitization did not match any "
            "team in de_asset_teams)",
            skipped_orphans,
        )
    if skipped_out_of_scope:
        logging.info(
            "fb_insights_window: %d doc(s) skipped as outside the --team_ids scope "
            "(leftover JSON from another team's run in the shared dir; ).",
            skipped_out_of_scope,
        )


def main(params):
    lib.logging.define("fb-window-json-to-elastic")

    team_ids = params.get("team_ids") if params else None
    # parse --team_ids into the scope set; None = unscoped.
    scope_esids = {t.strip() for t in str(team_ids).split(",") if t.strip()} if team_ids else None
    logging.info(f"Reading window json files from {DATA_DIR}")
    logging.info("Using ESID-keyed window indices (pattern: {team_esid}__fb_insights_window)")
    if scope_esids:
        logging.info(f"Team filter: {team_ids}")

    if not os.path.exists(DATA_DIR):
        logging.info(f"Directory {DATA_DIR} does not exist; nothing to send.")
        return

    es_client = get_elasticsearch_client()

    indices_created = {}
    schema = get_schema_for_index_type("fb_insights_window")
    # Phase B: narrow scan to Facebook-enabled teams (ESID-first;
    # avoids the wasted all-teams scan + cross-workspace collision WARNING).
    team_cache = build_team_lookup_cache(es_client, team_filter={"ds_facebook": "true"})

    documents = read_json_files(DATA_DIR)
    actions = generate_bulk_actions(documents, indices_created, es_client, schema, team_cache, scope_esids=scope_esids)

    success_count, errors = bulk(es_client, actions, chunk_size=BULK_CHUNK_SIZE, raise_on_error=False)

    if errors:
        logging.error(f"Bulk indexing completed with {len(errors)} errors:")
        for err in errors[:10]:
            logging.error(f"  {err}")

    logging.info(
        f"Completed sending {success_count} window documents to "
        f"{len(indices_created)} team-specific window indices. Errors: {len(errors)}"
    )
