"""Phase 1 — Google Ads multi-account foundation.

A team historically linked exactly ONE Google Ads customer via the scalar
``google_ads_customer_esid``. Both confirmed multi-account scenarios (multiple
customers under one login; a second Google login on the same team) need a team
to hold N customers, so this migration:

1. Adds the keyword list field ``team_google_ads_customer_esids`` to the
   de_asset_teams mapping (mirrors ``team_ga4_property_esids``). A keyword type
   so the importer's term query (``team_google_ads_customer_esids.keyword``)
   does exact-match containment without analyzer ambiguity.
2. Seeds the list from the existing scalar for every team that has one and does
   not already carry the list — so single-customer teams keep working and the
   new multi-select team-edit form shows the existing customer.

During the transition the scalar ``google_ads_customer_esid`` is still written
(= list primary) by configure_google_ads_customer, so the ~35 existing scalar
readers stay unbroken; the scalar is dropped in a later cleanup ticket.

Idempotent: re-running adds no field that already exists and seeds no team that
already carries the list.

MANUAL DEPLOY STEP (CLAUDE.md): update_vN does NOT run from a git pull. Run it
by hand on each env after cutover and verify the seed actually ran.
"""

import logging
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v19] %(message)s")

INDEX_NAME = "de_asset_teams"
LIST_FIELD = "team_google_ads_customer_esids"
SCALAR_FIELD = "google_ads_customer_esid"


def update_de_asset_teams_mapping(es):
    """Add team_google_ads_customer_esids as a keyword field on de_asset_teams."""
    new_fields = {LIST_FIELD: {"type": "keyword"}}

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
        logging.info(f"No mapping updates needed for {INDEX_NAME}. {LIST_FIELD} already exists.")

    return "success"


def seed_list_from_scalar(es):
    """For every team with a scalar google_ads_customer_esid and no list field
    yet, set team_google_ads_customer_esids=[scalar]. Idempotent — skips teams
    that already carry a non-empty list."""
    seeded = 0
    skipped = 0
    try:
        # Teams that have the scalar set. We seed only when the list is missing
        # or empty, so a re-run (or a team configured post-migration) is a no-op.
        query = {
            "bool": {
                "must": [{"exists": {"field": SCALAR_FIELD}}],
            }
        }
        page = es.search(index=INDEX_NAME, query=query, size=1000, _source=[SCALAR_FIELD, LIST_FIELD])
        for hit in page.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            scalar = (src.get(SCALAR_FIELD) or "").strip()
            if not scalar:
                continue
            existing_list = src.get(LIST_FIELD)
            if isinstance(existing_list, list) and existing_list:
                skipped += 1
                continue
            es.update(index=INDEX_NAME, id=hit["_id"], doc={LIST_FIELD: [scalar]})
            seeded += 1
        logging.info(f"Seeded {LIST_FIELD} on {seeded} team(s); {skipped} already had a list.")
        return "success"
    except Exception as e:
        logging.error(f"Error seeding {LIST_FIELD} from {SCALAR_FIELD}: {e}")
        return "abort"


def run_update():
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    logging.info("Starting update_v19:  add team_google_ads_customer_esids + seed from scalar")

    if update_de_asset_teams_mapping(es) == "abort":
        logging.error("Update aborted (mapping)")
        return "abort"

    if seed_list_from_scalar(es) == "abort":
        logging.error("Update aborted (seed)")
        return "abort"

    logging.info("Update_v19 completed successfully")
    return "success"


if __name__ == "__main__":
    run_update()
