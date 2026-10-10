"""
update_v14.py — Auto-enable ds_* data source checkboxes for teams that have data.

When update_v13 added the ds_* checkbox fields to de_asset_teams, it only
updated the mapping — existing team documents were left with no values, so
all checkboxes default to unchecked. This hides data source configuration
for teams that already have real data in Elasticsearch.

This script:
  1. Reads every team from de_asset_teams
  2. For each team, runs a batched msearch checking 7 data source index patterns
  3. Also checks whether the team's controlled asset field is non-empty
     (e.g. ig_account_id is set even if no ig_insights index exists yet)
  4. Sets ds_* = "true" if EITHER condition is true
  5. Never unchecks a box that is already "true"
"""

import logging
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v14] %(message)s")

TEAMS_INDEX = "de_asset_teams"

# Maps each ds_* field to the ES index patterns that indicate data presence.
# Patterns use wildcards so they match all tenant-prefixed indices.
DS_INDEX_PATTERNS = {
    "ds_facebook": ["*fb_insights", "*fb_page_insights"],
    "ds_echo": ["*echo_conversations"],
    "ds_chatwoot": ["*chatwoot_conversations", "*chatwoot_insights"],
    "ds_google_analytics": ["*ga_metrics"],
    "ds_google_ads": ["*google_ads"],
    "ds_instagram": ["*ig_insights"],
    "ds_youtube": ["*youtube_analytics"],
}

# Also check the team document's own asset link fields — if a field is non-empty
# the data source is considered active even if no ES import index exists yet.
#
# (2026-06-19): these must be the *_esid link fields that actually hold
# the wiring, NOT the legacy display fields (team_facebook_page_id,
# team_youtube_channel_handle, team_ga4_properties,...). The ESID
# migration moved the data to *_esid and emptied the legacy fields, so the old
# map under-detected every wired connector. This map mirrors
# connector_status_service.CONNECTORS' team_esid_field (the read side of truth).
# v14 is additive (only ever sets ds_*="true"), so a wrong map here is a missed
# enable, not data loss — but it should still be correct.
DS_ASSET_FIELDS = {
    "ds_facebook": "team_facebook_page_esids",
    "ds_echo": "echo_tenant_esid",
    "ds_chatwoot": "chatwoot_account_esid",
    "ds_google_analytics": "team_ga4_property_esids",
    "ds_google_ads": "google_ads_customer_esid",
    "ds_instagram": "team_instagram_profile_esids",
    "ds_youtube": "team_youtube_channel_handle_esids",
}


def _doc_count(response):
    """Extract hit count from an ES search response dict."""
    if "error" in response:
        return 0
    total = response.get("hits", {}).get("total", {})
    return total.get("value", 0) if isinstance(total, dict) else int(total or 0)


def detect_active_data_sources(es, team_title):
    """Return the set of ds_* fields that have data for this team.

    Runs all queries in a single msearch round-trip.
    """
    team_query = {
        "bool": {
            "should": [
                {"term": {"team_title": team_title}},
                {"term": {"team_title.keyword": team_title}},
            ],
            "minimum_should_match": 1,
        }
    }

    # Build msearch body — one query per ds_* field (patterns joined with comma)
    ds_fields = list(DS_INDEX_PATTERNS.keys())
    search_body = []
    for ds_field in ds_fields:
        patterns = ",".join(DS_INDEX_PATTERNS[ds_field])
        search_body.append({"index": patterns, "ignore_unavailable": True})
        search_body.append({"size": 0, "query": team_query})

    results = es.msearch(body=search_body)

    active = set()
    for i, ds_field in enumerate(ds_fields):
        if _doc_count(results["responses"][i]) > 0:
            active.add(ds_field)
    return active


def run_update():
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    logging.info("Starting update_v14: auto-enable ds_* checkboxes based on data presence")

    # Fetch all teams (up to 1000 — well above any realistic count)
    try:
        resp = es.search(index=TEAMS_INDEX, body={"query": {"match_all": {}}, "size": 1000})
    except Exception as e:
        logging.error(f"Failed to fetch teams: {e}")
        return "abort"

    teams = resp["hits"]["hits"]
    logging.info(f"Found {len(teams)} teams to process")

    updated = 0
    skipped = 0

    for team in teams:
        team_id = team["_id"]
        source = team.get("_source", {})
        team_title = source.get("team_title", "")

        if not team_title:
            logging.warning(f"Team {team_id} has no team_title, skipping")
            skipped += 1
            continue

        try:
            active_sources = detect_active_data_sources(es, team_title)
        except Exception as e:
            logging.error(f"msearch failed for team '{team_title}': {e}")
            skipped += 1
            continue

        # Also enable if the team's own asset field is non-empty
        # (e.g. ig_account_id is set even if no ig_insights index exists yet)
        for ds, asset_field in DS_ASSET_FIELDS.items():
            val = source.get(asset_field)
            has_val = val and (len(val) > 0 if isinstance(val, list) else str(val).strip() != "")
            if has_val:
                active_sources.add(ds)

        # Only update fields that are not already "true"
        fields_to_set = {ds: "true" for ds in active_sources if source.get(ds) != "true"}

        if fields_to_set:
            try:
                es.update(index=TEAMS_INDEX, id=team_id, body={"doc": fields_to_set})
                logging.info(f"  {team_title}: enabled {sorted(fields_to_set.keys())}")
                updated += 1
            except Exception as e:
                logging.error(f"  {team_title}: update failed — {e}")
                skipped += 1
        else:
            already = sorted(ds for ds in active_sources if source.get(ds) == "true")
            logging.info(
                f"  {team_title}: no changes needed (already set: {already}, no data: {sorted(set(DS_INDEX_PATTERNS) - active_sources)})"
            )

    logging.info(f"update_v14 complete: {updated} teams updated, {skipped} skipped")
    print(f"update_v14 complete: {updated} teams updated, {skipped} skipped")
    return "success"


if __name__ == "__main__":
    run_update()
