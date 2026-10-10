import json
import logging
import sys
import os
from datetime import datetime

# Add parent directory to path to import lib
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client
from elasticsearch import helpers

# Set up logging
current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v11] %(message)s")


def update_existing_de_asset_teams_documents(es):
    """Set team_fb_integration_status to 'Active' for all teams."""
    index_name = "de_asset_teams"

    try:
        response = es.search(index=index_name, body={"query": {"match_all": {}}}, scroll="2m", size=1000)
        scroll_id = response["_scroll_id"]
        total_hits = response["hits"]["total"]["value"]
        logging.info(f"Found {total_hits} documents in {index_name}.")

        actions = []
        updated_count = 0

        while True:
            hits = response["hits"]["hits"]
            if not hits:
                break

            for doc in hits:
                doc_id = doc["_id"]
                source = doc["_source"]

                # Set team_fb_integration_status to Active unconditionally
                source["team_fb_integration_status"] = "Active"
                updated_count += 1

                action = {
                    "_op_type": "update",
                    "_index": index_name,
                    "_id": doc_id,
                    "doc": source,
                }
                actions.append(action)

            response = es.scroll(scroll_id=scroll_id, scroll="2m")

        if actions:
            logging.info(f"Updating {len(actions)} documents in {index_name} to set team_fb_integration_status = 'Active'...")
            helpers.bulk(es, actions)
            logging.info(f"Successfully updated {updated_count} team documents.")
        else:
            logging.info("No documents to update.")

        # Clear the scroll
        try:
            es.clear_scroll(scroll_id=scroll_id)
        except:
            pass

    except Exception as e:
        logging.error(f"Error updating documents in {index_name}: {e}")
        return "abort"

    return "success"


def run():
    """Main function to run the update."""
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    logging.info("Starting update_v11: Set all teams to team_fb_integration_status = 'Active'")

    result = update_existing_de_asset_teams_documents(es)

    if result == "abort":
        logging.error("Update aborted due to errors")
        return "abort"

    logging.info("Update_v11 completed successfully")
    return "success"


if __name__ == "__main__":
    run()
