import logging
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v13] %(message)s")


def update_de_asset_teams_mapping(es):
    """Add data source checkbox fields to de_asset_teams mapping."""
    index_name = "de_asset_teams"
    new_fields = {
        "ds_chatwoot": {"type": "keyword"},
        "ds_echo": {"type": "keyword"},
        "ds_facebook": {"type": "keyword"},
        "ds_google_analytics": {"type": "keyword"},
        "ds_google_ads": {"type": "keyword"},
        "ds_instagram": {"type": "keyword"},
        "ds_youtube": {"type": "keyword"},
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

    logging.info("Starting update_v13: Add data source checkbox fields to de_asset_teams")

    result = update_de_asset_teams_mapping(es)
    if result == "abort":
        logging.error("Update aborted")
        return "abort"

    logging.info("Update_v13 completed successfully")
    return "success"


if __name__ == "__main__":
    run_update()
