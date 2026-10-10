"""Instagram JSON → Elasticsearch sender.

 migrated this sender to ESID-keyed indexing scheme.
Pre-fix it wrote to ``{sanitize(team_title)}_ig_insights`` (e.g.
``test_account_ig_insights``), which silently collided across
workspaces when two teams shared a ``team_title`` — a supported state
under the workspace migration scheme. The fix routes every doc through
``resolve_team_esid`` + ``ensure_data_index`` so the physical index is
``{team_esid_lower}__ig_insights``.

 (re-scope 2026-06-01): forward aliases are out of scope — the
importer creates the ESID-keyed index only.

Mirrors ``import_scripts/facebook/send_json_to_elastic.py`` 1:1 — same
shape, same orphan-drop behavior.
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
SOURCE_SLUG = "ig_insights"


def read_json_files(directory):
    lib.logging.define("ig-json-to-elastic")
    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                with open(filepath, "r") as file:
                    yield json.load(file)


def _build_doc_id(data):
    """deterministic _id for an ig_insights doc.

    Grain: one doc per (profile, day). The only date field on the doc is
    ``kibana_date`` and the profile id is ``ig_user_id`` (verified against real
    docs — ``instagram_profile_id``/``profile_id`` are null on ig_insights
    docs). Returns ``{ig_user_id}_{kibana_date}`` or ``None`` if a component is
    missing.
    """
    profile_id = data.get("ig_user_id")
    date = data.get("kibana_date")
    if not profile_id or not date:
        return None
    return f"{profile_id}_{date}"


def generate_bulk_actions(documents, indices_created, es_client, schema, team_cache, scope_esids=None):
    """Yield bulk index actions, creating ESID-keyed indices on first encounter.

    index name is now ``{team_esid}__ig_insights``. Docs that can't
    be resolved to a known team_esid are skipped with a warning rather
    than silently routed to a shared ``unknown_ig_insights`` index.

    ``scope_esids`` is the resolved ``--team_ids`` scope (a set of
    team ESIDs). When set, docs whose resolved ``team_esid`` is NOT in scope are
    dropped, so a scoped run does not sweep up another team's leftover JSON in
    the shared ``ig_insights/`` dir (cross-workspace leak; sibling of).
    ``None`` (default) = unscoped, preserving the test behavior.
    """
    skipped_orphans = 0
    skipped_out_of_scope = 0
    for data in documents:
        # ig_insights docs carried only kibana_date; stamp ``date``
        # too so cross-source date queries include them (see the fb_insights
        # sender's twin comment).
        if data.get("kibana_date") and not data.get("date"):
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
                    "ig_insights: dropping doc with unresolved team — " "team_esid=%r team_title=%r",
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

        # deterministic _id (one doc per profile per day) so a
        # re-import upserts in place instead of appending a duplicate. Fall
        # back to an ES-generated id if a component is missing.
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
            "ig_insights: total %d doc(s) dropped because team could not be resolved "
            "(team_esid missing AND team_title sanitization did not match any team in "
            "de_asset_teams)",
            skipped_orphans,
        )
    if skipped_out_of_scope:
        logging.info(
            "ig_insights: %d doc(s) skipped as outside the --team_ids scope "
            "(leftover JSON from another team's run in the shared dir; ).",
            skipped_out_of_scope,
        )


def main(params):
    lib.logging.define("ig-json-to-elastic")
    directory = "/app/data/instagram/ig_insights/"

    team_ids = params.get("team_ids") if params else None
    # parse --team_ids into the scope set; None = unscoped.
    scope_esids = {t.strip() for t in str(team_ids).split(",") if t.strip()} if team_ids else None
    logging.info(f"Reading Instagram json files from {directory}")
    logging.info("Using ESID-keyed indices (pattern: {team_esid}__ig_insights)")
    if scope_esids:
        logging.info(f"Team filter: {team_ids}")

    es_client = get_elasticsearch_client()

    indices_created = {}
    schema = get_schema_for_index_type("ig_insights")
    # Phase B: narrow scan to Instagram-enabled teams (ESID-first;
    # avoids the wasted all-teams scan + cross-workspace collision WARNING).
    team_cache = build_team_lookup_cache(es_client, team_filter={"ds_instagram": "true"})

    documents = read_json_files(directory)
    actions = generate_bulk_actions(documents, indices_created, es_client, schema, team_cache, scope_esids=scope_esids)

    success_count, errors = bulk(es_client, actions, chunk_size=BULK_CHUNK_SIZE, raise_on_error=False)

    lib.logging.define("ig-json-to-elastic")

    if errors:
        logging.error(f"Bulk indexing completed with {len(errors)} errors:")
        for err in errors[:10]:
            logging.error(f"  {err}")

    logging.info(
        f"Completed sending {success_count} Instagram document(s) to "
        f"{len(indices_created)} team-specific indices. Errors: {len(errors)}"
    )
