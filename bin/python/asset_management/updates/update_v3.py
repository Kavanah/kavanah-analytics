import json
import logging
from datetime import datetime
from lib.es_helpers import get_elasticsearch_client
from elasticsearch import helpers

# Set up logging
current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v3] %(message)s")


def update_de_asset_teams_mapping(es):
    """Update the mapping of de_asset_teams to include new fields, if they do not exist."""
    index_name = "de_asset_teams"
    new_fields = {
        "team_echo_integration_status": {"type": "keyword"},
        "echo_tenant_esid": {"type": "keyword"},
        "echo_tenant_title": {"type": "keyword"},
        "echo_convo_sources": {"type": "keyword"},  # For array of strings
    }

    # Get the current mapping
    try:
        current_mapping = es.indices.get_mapping(index=index_name)
        current_properties = current_mapping[index_name]["mappings"]["properties"]
    except Exception as e:
        logging.error(f"Error fetching current mapping for {index_name}: {e}")
        return "abort"

    # Check if fields already exist
    fields_to_update = {field: definition for field, definition in new_fields.items() if field not in current_properties}

    if fields_to_update:
        try:
            es.indices.put_mapping(index=index_name, body={"properties": fields_to_update})
            logging.info(f"Updated mapping for {index_name}. New fields added: {list(fields_to_update.keys())}")
        except Exception as e:
            logging.error(f"Error updating mapping for {index_name}: {e}")
            return "abort"
    else:
        logging.info(f"No mapping updates needed for {index_name}. All new fields already exist.")

    return "success"


def update_existing_de_asset_teams_documents(es):
    """Update existing documents in de_asset_teams with default values for new fields, if they do not already exist."""
    index_name = "de_asset_teams"

    try:
        response = es.search(index=index_name, body={"query": {"match_all": {}}}, scroll="2m", size=1000)
        scroll_id = response["_scroll_id"]
        total_hits = response["hits"]["total"]["value"]
        logging.info(f"Found {total_hits} documents in {index_name}.")

        actions = []

        while True:
            hits = response["hits"]["hits"]
            if not hits:
                break

            for doc in hits:
                doc_id = doc["_id"]
                source = doc["_source"]

                # Only update fields if they do not already exist
                updated = False
                if "team_echo_integration_status" not in source:
                    source["team_echo_integration_status"] = "Disabled"
                    updated = True
                if "echo_tenant_esid" not in source:
                    source["echo_tenant_esid"] = False
                    updated = True
                if "echo_tenant_title" not in source:
                    source["echo_tenant_title"] = False
                    updated = True
                if "echo_convo_sources" not in source:
                    source["echo_convo_sources"] = []  # Use empty list instead of False
                    updated = True

                if updated:
                    action = {
                        "_op_type": "update",
                        "_index": index_name,
                        "_id": doc_id,
                        "doc": source,
                    }
                    actions.append(action)

            response = es.scroll(scroll_id=scroll_id, scroll="2m")

        if actions:
            logging.info(f"Updating {len(actions)} documents in {index_name}...")
            helpers.bulk(es, actions)
            logging.info("Documents updated successfully.")
        else:
            logging.info("No documents to update.")

    except Exception as e:
        logging.error(f"Error updating documents in {index_name}: {e}")
        return "abort"

    return "success"


def create_de_asset_echo_tenants_index(es):
    """Create the de_asset_echo_tenants index with specified fields."""
    index_name = "de_asset_echo_tenants"
    mapping = {
        "properties": {
            "echo_tenant_title": {"type": "keyword"},
            "echo_tenant_api_key": {"type": "keyword"},
            "echo_tenant_subdomain": {"type": "keyword"},
            "echo_tenant_description": {"type": "keyword"},
        }
    }

    if not es.indices.exists(index=index_name):
        try:
            es.indices.create(index=index_name, body={"mappings": mapping})
            logging.info(f"Index {index_name} created successfully.")
        except Exception as e:
            logging.error(f"Error creating index {index_name}: {e}")
            return "abort"
    else:
        logging.info(f"Index {index_name} already exists.")

    return "success"


def run_update():
    """This function defines the update process for v3."""
    logging.info("Running Elasticsearch update script v3...")

    es = get_elasticsearch_client()

    if es is None:
        logging.error("Failed to establish Elasticsearch connection.")
        return "abort"

    # Step 1: Update the mapping of de_asset_teams
    if update_de_asset_teams_mapping(es) == "abort":
        return "abort"

    # Step 2: Update existing documents in de_asset_teams
    if update_existing_de_asset_teams_documents(es) == "abort":
        return "abort"

    # Step 3: Create de_asset_echo_tenants index
    if create_de_asset_echo_tenants_index(es) == "abort":
        return "abort"

    logging.info("Elasticsearch update v3 completed successfully.")
    return "success"


if __name__ == "__main__":
    run_update()
