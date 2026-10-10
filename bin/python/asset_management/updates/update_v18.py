"""Defect A — de_asset_teams mapping for account-wide FB import mode.

Adds three keyword fields the wizard step-8 finish writes onto a team
doc when the user picked the account-wide import path:

* ``fb_import_mode`` — ``"page_filtered"`` (default; absent on legacy
  teams) | ``"account_wide"`` (wizard cohort).
* ``fb_ad_account_esid`` — pointer to the wizard-written
  ``de_asset_fb_ad_accounts`` doc the account-wide importer fetches
  data for.
* ``connection_esid`` — pointer back to the de_connection row whose
  ``oauth_token_enc`` the importer decrypts for the Graph API call.

All three are keyword so the importer's term-filtered ES query
(``get_account_wide_teams_by_ad_account_id`` in get_insights.py) does
exact-match lookups without an analyzer ambiguity. Dynamic mapping
would auto-create them as text+keyword.sub on first wizard signup,
which works but isn't explicit — surfacing the schema here keeps the
contract discoverable.
"""

import logging
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v18] %(message)s")


def update_de_asset_teams_mapping(es):
    """Defect A — add fb_import_mode + fb_ad_account_esid +
    connection_esid as keyword fields on de_asset_teams."""
    index_name = "de_asset_teams"
    new_fields = {
        "fb_import_mode": {"type": "keyword"},
        "fb_ad_account_esid": {"type": "keyword"},
        "connection_esid": {"type": "keyword"},
    }

    try:
        current_mapping = es.indices.get_mapping(index=index_name)
        current_properties = current_mapping[index_name]["mappings"]["properties"]
    except Exception as e:
        logging.error(f"Error fetching current mapping for {index_name}: {e}")
        return "abort"

    fields_to_add = {field: defn for field, defn in new_fields.items() if field not in current_properties}

    if fields_to_add:
        try:
            es.indices.put_mapping(index=index_name, body={"properties": fields_to_add})
            logging.info(f"Updated mapping for {index_name}. New fields: {list(fields_to_add.keys())}")
        except Exception as e:
            logging.error(f"Error updating mapping for {index_name}: {e}")
            return "abort"
    else:
        logging.info(f"No mapping updates needed for {index_name}. All fields already exist.")

    return "success"


def run_update():
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    logging.info("Starting update_v18:  add FB account-wide import fields to de_asset_teams")

    result = update_de_asset_teams_mapping(es)
    if result == "abort":
        logging.error("Update aborted")
        return "abort"

    logging.info("Update_v18 completed successfully")
    return "success"


if __name__ == "__main__":
    run_update()
