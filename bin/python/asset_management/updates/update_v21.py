"""per-team time-range presets.

Each team can declare which time-range presets its dashboard viewers see via a
new keyword-list field ``team_time_presets`` on ``de_asset_teams`` (edited on the
team form as a checkbox group). This migration adds the explicit keyword mapping
for that field, mirroring how update_v19/update_v20 added
``team_google_ads_customer_esids`` / ``team_urls``.

No seed step: a team with NO ``team_time_presets`` value is exactly the
"no custom configuration" case, which the viewer resolves to the full default
preset catalog (time_preset_service.resolve_time_presets → None). So existing
teams need no data change — they keep the current full dropdown until an admin
opts in to trimming.

Idempotent: re-running adds no field that already exists.

MANUAL DEPLOY STEP (CLAUDE.md): update_vN does NOT run from a git pull. Run it
by hand on each env after cutover and verify the mapping was added.
"""

import logging
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v21] %(message)s")

INDEX_NAME = "de_asset_teams"
PRESETS_FIELD = "team_time_presets"


def update_de_asset_teams_mapping(es):
    """Add team_time_presets as a keyword field on de_asset_teams."""
    new_fields = {PRESETS_FIELD: {"type": "keyword"}}

    try:
        current_mapping = es.indices.get_mapping(index=INDEX_NAME)
        current_properties = current_mapping[INDEX_NAME]["mappings"]["properties"]
    except Exception as e:
        logging.error(f"Error fetching current mapping for {INDEX_NAME}: {e}")
        return "abort"

    fields_to_add = {field: defn for field, defn in new_fields.items() if field not in current_properties}

    if fields_to_add:
        try:
            es.indices.put_mapping(index=INDEX_NAME, body={"properties": fields_to_add})
            logging.info(f"Updated mapping for {INDEX_NAME}. New fields: {list(fields_to_add.keys())}")
        except Exception as e:
            logging.error(f"Error updating mapping for {INDEX_NAME}: {e}")
            return "abort"
    else:
        logging.info(f"No mapping updates needed for {INDEX_NAME}. {PRESETS_FIELD} already exists.")

    return "success"


def run_update():
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    logging.info("Starting update_v21:  add team_time_presets keyword mapping")

    if update_de_asset_teams_mapping(es) == "abort":
        logging.error("Update aborted (mapping)")
        return "abort"

    logging.info("Update_v21 completed successfully")
    return "success"


if __name__ == "__main__":
    run_update()
