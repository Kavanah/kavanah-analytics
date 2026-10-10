"""Facebook Page Posts JSON → Elasticsearch sender.

Clone of ``send_page_json_to_elastic.py`` for the App-Review "Recent FB
Page Posts" feature (pages_read_engagement). Routes every post doc through
``resolve_team_esid`` + ``ensure_data_index`` so the physical index is
``{team_esid_lower}__fb_page_posts`` — same ESID-keyed scheme as.

 (re-scope 2026-06-01): forward aliases are out of scope — the
importer creates the ESID-keyed index only.

Difference from the page-insights sender: posts are LATEST-ONLY, so the
bulk action sets ``_id`` to the post's ``post_id``. Re-running the importer
updates each post in place (refreshed engagement counts) instead of
appending a duplicate doc per run.
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
SOURCE_SLUG = "fb_page_posts"

# set by get_page_insights.massage_post_data on a post whose insights
# call returned nothing (dropped, denied, or 200 with empty data). Such a doc
# carries no insight-derived field; this sender copies the stored values forward
# so a failed fetch never overwrites real counts with 0 / absent, and strips the
# flag before indexing.
INSIGHTS_FAILED_FLAG = "_insights_failed"
# The post-doc fields that come from the insights batch. post_shares is carried
# only when the post itself did not return shares.count.
POST_INSIGHT_DERIVED_FIELDS = (
    "post_comments",
    "post_reactions_total",
    "post_shares",
    "post_media_view",
    "post_total_media_view_unique",
    "post_clicks",
)


def _carry_forward_insight_fields(es_client, index_name, post_id, data):
    """Copy the stored doc's insight-derived fields into ``data`` where the new
    doc lacks them. Returns how many fields were carried. A missing stored doc
    (first import) or a read failure carries nothing — the doc then lands
    without those fields, never with a fabricated 0."""
    try:
        stored = es_client.get(index=index_name, id=post_id)
    except Exception as e:  # noqa: BLE001 — NotFound on a first import; any read error must not block the send
        logging.debug("fb_page_posts: no stored doc to carry forward for %s (%s)", post_id, e)
        return 0
    src = (stored.body if hasattr(stored, "body") else stored).get("_source") or {}
    carried = 0
    for field in POST_INSIGHT_DERIVED_FIELDS:
        if field not in data and field in src:
            data[field] = src[field]
            carried += 1
    return carried


def read_json_files(directory):
    lib.logging.define("fb-post-json-to-elastic")
    for root, dirs, files in os.walk(directory):
        for filename in files:
            if filename.endswith(".json"):
                filepath = os.path.join(root, filename)
                with open(filepath, "r") as file:
                    yield json.load(file)


def generate_bulk_actions(documents, indices_created, es_client, schema, team_cache, scope_esids=None):
    """Yield bulk index actions, creating ESID-keyed indices on first encounter.

    Index name is ``{team_esid}__fb_page_posts``. Docs that can't be
    resolved to a known team_esid are skipped with a warning rather than
    silently routed to a shared ``unknown`` index. ``_id`` is the post_id
    so re-imports upsert in place (latest-only semantics).

    ``scope_esids`` is the resolved ``--team_ids`` scope (a set of
    team ESIDs) the run was launched with. When set, any doc whose resolved
    ``team_esid`` is NOT in the scope is dropped: the senders walk a SHARED
    on-disk directory (``/app/data/facebook/page_posts/``), so a run scoped to
    one team would otherwise sweep up another team's leftover JSON sitting in a
    sibling subdir and index it under this run — a cross-workspace leak (the
    sibling defect to, which scoped only the importer's team-filter
    resolvers, not these downstream senders). ``None`` (the default) preserves
    the unscoped full-directory behavior for manual / backfill runs.
    """
    skipped_orphans = 0
    skipped_out_of_scope = 0
    failed_insights = 0
    carried_forward = 0
    for data in documents:
        team_esid = resolve_team_esid(
            team_cache,
            team_esid=data.get("team_esid"),
            team_title=data.get("team_title"),
        )
        if not team_esid:
            skipped_orphans += 1
            if skipped_orphans <= 5:
                logging.warning(
                    "fb_page_posts: dropping doc with unresolved team — " "team_esid=%r team_title=%r",
                    data.get("team_esid"),
                    data.get("team_title"),
                )
            continue

        # drop docs outside this run's team scope (foreign-team
        # leftovers in the shared dir). ESIDs are case-sensitive; both sides
        # carry the raw team _id, so compare directly without normalising.
        if scope_esids is not None and team_esid not in scope_esids:
            skipped_out_of_scope += 1
            continue

        if team_esid not in indices_created:
            index_name = ensure_data_index(es_client, team_esid, SOURCE_SLUG, schema)
            indices_created[team_esid] = index_name
            logging.info(f"Created team index: {index_name}")
        else:
            index_name = indices_created[team_esid]

        # insights failed for this post: keep the stored counts.
        post_id = data.get("post_id")
        if data.pop(INSIGHTS_FAILED_FLAG, False):
            failed_insights += 1
            if post_id and _carry_forward_insight_fields(es_client, index_name, post_id, data):
                carried_forward += 1

        action = {
            "_op_type": "index",
            "_index": index_name,
            "_source": data,
        }
        # Latest-only: keying on post_id makes re-imports overwrite rather
        # than append. Fall back to auto-id if a doc somehow lacks post_id.
        if post_id:
            action["_id"] = post_id
        yield action

    if skipped_orphans:
        logging.warning(
            "fb_page_posts: total %d doc(s) dropped because team could not be resolved "
            "(team_esid missing AND team_title sanitization did not match any team in "
            "de_asset_teams)",
            skipped_orphans,
        )
    if failed_insights:
        logging.warning(
            "fb_page_posts: %d doc(s) arrived without insights (the fetch returned none); "
            "%d kept their stored counts, the rest had none stored.",
            failed_insights,
            carried_forward,
        )
    if skipped_out_of_scope:
        logging.info(
            "fb_page_posts: %d doc(s) skipped as outside the --team_ids scope "
            "(leftover JSON from another team's run in the shared dir; ).",
            skipped_out_of_scope,
        )


def main(params):
    lib.logging.define("fb-post-json-to-elastic")
    directory = "/app/data/facebook/page_posts/"

    team_ids = params.get("team_ids") if params else None
    # parse --team_ids into the scope set the sender filters by. None
    # (no flag) = unscoped full-directory behavior; a set scopes the run so a
    # foreign team's leftover JSON in the shared dir is not swept up.
    scope_esids = {t.strip() for t in str(team_ids).split(",") if t.strip()} if team_ids else None
    logging.info(f"Reading json files from {directory}")
    logging.info("Using ESID-keyed indices (pattern: {team_esid}__fb_page_posts)")
    if scope_esids:
        logging.info(f"Team filter: {team_ids}")
    else:
        logging.info("Processing all teams")

    es_client = get_elasticsearch_client()

    indices_created = {}
    schema = get_schema_for_index_type("fb_page_posts")
    # Phase B: narrow scan to Facebook-enabled teams (ESID-first;
    # avoids the wasted all-teams scan + cross-workspace collision WARNING).
    team_cache = build_team_lookup_cache(es_client, team_filter={"ds_facebook": "true"})

    documents = read_json_files(directory)
    actions = generate_bulk_actions(documents, indices_created, es_client, schema, team_cache, scope_esids=scope_esids)

    success_count, errors = bulk(es_client, actions, chunk_size=BULK_CHUNK_SIZE, raise_on_error=False)

    if errors:
        logging.error(f"Bulk indexing completed with {len(errors)} errors:")
        for err in errors[:10]:
            logging.error(f"  {err}")

    lib.logging.define("fb-post-json-to-elastic")
    logging.info(
        f"Completed sending {success_count} document(s) to "
        f"{len(indices_created)} team-specific indices. Errors: {len(errors)}"
    )
