import os
import json
import logging
import lib.logging
from lib.es_helpers import (
    build_team_lookup_cache,
    ensure_data_index,
    get_elasticsearch_client,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_for_index_type
from elasticsearch.helpers import bulk

BULK_CHUNK_SIZE = 500
SOURCE_SLUG = "fb_insights"


def read_json_files(directory):
    lib.logging.define("fb-json-to-elastic")
    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                with open(filepath, "r") as file:
                    yield json.load(file)


def _build_doc_id(data):
    """deterministic _id for an fb_insights doc.

    Grain: one doc per (ad, date, publisher_platform). Returns
    ``{ad_id}_{insight_date_start}_{publisher_platform}`` or ``None`` if any
    component is missing/empty (caller then lets ES auto-generate the id —
    never build a "None_..." key that collides distinct rows).
    """
    ad_id = data.get("ad_id")
    date = data.get("insight_date_start")
    platform = data.get("publisher_platform")
    if not ad_id or not date or not platform:
        return None
    return f"{ad_id}_{date}_{platform}"


def generate_bulk_actions(documents, indices_created, es_client, schema, team_cache, scope_esids=None):
    """Yield bulk index actions, creating ESID-keyed indices on first encounter.

    index name is now `{team_esid}__fb_insights`. Docs that can't
    be resolved to a known team_esid are skipped with a warning rather
    than silently routed to an `unknown_*` index.

    ``scope_esids`` is the resolved ``--team_ids`` scope (a set of
    team ESIDs). When set, docs whose resolved ``team_esid`` is NOT in scope are
    dropped, so a scoped run does not sweep up another team's leftover JSON in
    the shared ``ad_insights/`` dir (cross-workspace leak; sibling of).
    Normally this dir is archived after each send, but archiving is best-effort,
    so the guard is defense-in-depth. ``None`` (default) = unscoped.
    """
    skipped_orphans = 0
    skipped_out_of_scope = 0
    for data in documents:
        if "kibana_date" not in data or not data["kibana_date"]:
            data["kibana_date"] = data["insight_date_start"]
        # fb_insights docs carried only kibana_date, so any
        # date-histogram / range on the cross-source ``date`` field silently
        # excluded them (read as "importer stopped" during). Stamp
        # ``date`` = kibana_date at the send choke point so every doc carries
        # both, matching youtube/google_ads/GA.
        if not data.get("date"):
            data["date"] = data["kibana_date"]

        team_esid = resolve_team_esid(
            team_cache,
            team_esid=data.get("team_esid"),
            team_title=data.get("team_title"),
        )
        if not team_esid:
            skipped_orphans += 1
            if skipped_orphans <= 5:
                logging.warning(
                    "fb_insights: dropping doc with unresolved team — " "team_esid=%r team_title=%r",
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
            logging.info(f"Created team index: {index_name}")
        else:
            index_name = indices_created[team_esid]

        # deterministic _id so re-importing the same date upserts in
        # place instead of stacking a duplicate doc (the importer fans out one
        # doc per ad × date × publisher_platform — get_insights.py:1249). If any
        # component is missing, fall back to an ES-generated id rather than
        # building a "None_..." key that would collide all such docs onto one.
        action = {
            "_op_type": "index",
            "_index": index_name,
            "_source": data,
        }
        doc_id = _build_doc_id(data)
        if doc_id:
            action["_id"] = doc_id
        yield action

    if skipped_orphans:
        logging.warning(
            "fb_insights: total %d doc(s) dropped because team could not be resolved "
            "(team_esid missing AND team_title sanitization did not match any team in "
            "de_asset_teams)",
            skipped_orphans,
        )
    if skipped_out_of_scope:
        logging.info(
            "fb_insights: %d doc(s) skipped as outside the --team_ids scope "
            "(leftover JSON from another team's run in the shared dir; ).",
            skipped_out_of_scope,
        )


def main(params):
    lib.logging.define("fb-json-to-elastic")
    directory = "/app/data/facebook/ad_insights/"

    team_ids = params.get("team_ids") if params else None
    # parse --team_ids into the scope set; None = unscoped.
    scope_esids = {t.strip() for t in str(team_ids).split(",") if t.strip()} if team_ids else None
    logging.info(f"Reading json files from {directory}")
    logging.info("Using ESID-keyed indices (pattern: {team_esid}__fb_insights)")
    if scope_esids:
        logging.info(f"Team filter: {team_ids}")

    es_client = get_elasticsearch_client()

    indices_created = {}
    schema = get_schema_for_index_type("fb_insights")
    # Phase B: narrow the scan to Facebook-enabled teams. FB docs
    # carry a pre-resolved team_esid (ESID-first resolution), so the all-teams
    # scan was wasted work and fired the cross-workspace title-collision
    # WARNING for unrelated teams. ds_facebook is a keyword field, value "true".
    team_cache = build_team_lookup_cache(es_client, team_filter={"ds_facebook": "true"})

    documents = read_json_files(directory)
    actions = generate_bulk_actions(documents, indices_created, es_client, schema, team_cache, scope_esids=scope_esids)

    success_count, errors = bulk(es_client, actions, chunk_size=BULK_CHUNK_SIZE, raise_on_error=False)

    # Re-set prefix since es_helpers calls may have changed it
    lib.logging.define("fb-json-to-elastic")

    if errors:
        logging.error(f"Bulk indexing completed with {len(errors)} errors:")
        for err in errors[:10]:
            logging.error(f"  {err}")

    logging.info(
        f"Completed sending {success_count} documents to "
        f"{len(indices_created)} team-specific indices. Errors: {len(errors)}"
    )
