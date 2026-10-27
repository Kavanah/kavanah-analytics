"""send the per-country delivery docs to ``{team_esid}__fb_insights_country``.

``get_insights.py`` writes one JSON per ad x day x delivery country under
``/app/data/facebook/ad_insights_country/``; this step indexes them with a
deterministic ``{ad_id}_{insight_date_start}_{country}`` id (re-sends upsert
in place) and ``archive_country_imported_json`` then moves the files aside.
Everything else — team resolution, the ``--team_ids`` scope guard, index
creation from the schema — is the platform sender's ``generate_bulk_actions``.
"""

import logging

import lib.logging
from elasticsearch.helpers import bulk
from lib.es_helpers import build_team_lookup_cache, get_elasticsearch_client
from migration.schema_loader import get_schema_for_index_type

from import_scripts.facebook.send_json_to_elastic import BULK_CHUNK_SIZE, generate_bulk_actions, read_json_files

SOURCE_SLUG = "fb_insights_country"
DIRECTORY = "/app/data/facebook/ad_insights_country/"


def build_country_doc_id(data):
    """``{ad_id}_{insight_date_start}_{country}`` or None when a part is missing."""
    ad_id = data.get("ad_id")
    date = data.get("insight_date_start")
    country = data.get("country")
    if not ad_id or not date or not country:
        return None
    return f"{ad_id}_{date}_{country}"


def main(params):
    lib.logging.define("fb-country-json-to-elastic")
    team_ids = params.get("team_ids") if params else None
    scope_esids = {t.strip() for t in str(team_ids).split(",") if t.strip()} if team_ids else None
    logging.info("Reading json files from %s", DIRECTORY)
    logging.info("Using ESID-keyed indices (pattern: {team_esid}__%s)", SOURCE_SLUG)
    if scope_esids:
        logging.info("Team filter: %s", team_ids)

    es_client = get_elasticsearch_client()
    if es_client is None:
        logging.error("Elasticsearch is not reachable; nothing sent.")
        return 1
    schema = get_schema_for_index_type(SOURCE_SLUG)
    team_cache = build_team_lookup_cache(es_client, team_filter={"ds_facebook": "true"})

    indices_created = {}
    actions = generate_bulk_actions(
        read_json_files(DIRECTORY),
        indices_created,
        es_client,
        schema,
        team_cache,
        scope_esids=scope_esids,
        source_slug=SOURCE_SLUG,
        doc_id_fn=build_country_doc_id,
    )
    success_count, errors = bulk(es_client, actions, chunk_size=BULK_CHUNK_SIZE, raise_on_error=False)
    lib.logging.define("fb-country-json-to-elastic")
    if errors:
        logging.error("Bulk indexing completed with %d errors:", len(errors))
        for err in errors[:10]:
            logging.error("  %s", err)
    logging.info(
        "Completed sending %d country-breakdown document(s) to %d team index(es). Errors: %d",
        success_count,
        len(indices_created),
        len(errors),
    )
    return 0
