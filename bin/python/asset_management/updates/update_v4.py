from lib.es_helpers import get_elasticsearch_client, get_all_documents, edit_document
import json


def create_index_with_mapping(client, index_name, mapping):
    """Create an Elasticsearch index with the specified mapping if it doesn't already exist."""
    if client.indices.exists(index=index_name):
        print(f"Index {index_name} already exists. Skipping creation.")
        return
    print(f"Creating index: {index_name}")
    try:
        client.indices.create(index=index_name, body={"mappings": mapping})
        print(f"Index {index_name} created successfully.")
    except Exception as e:
        print(f"Error creating index {index_name}: {e}")


def index_has_field(client, index_name, field_name):
    """Check if an index has a specific field in its mappings."""
    try:
        mapping = client.indices.get_mapping(index=index_name)
        properties = mapping[index_name]["mappings"].get("properties", {})
        return field_name in properties
    except Exception as e:
        print(f"Error checking field {field_name} in {index_name}: {e}")
        return False


def delete_index(client, index_name):
    """Delete an Elasticsearch index if it exists."""
    if client.indices.exists(index=index_name):
        print(f"Deleting index: {index_name}")
        try:
            client.indices.delete(index=index_name)
            print(f"Index {index_name} deleted successfully.")
        except Exception as e:
            print(f"Error deleting index {index_name}: {e}")
    else:
        print(f"Index {index_name} does not exist. Skipping deletion.")


def backup_documents(client, index_name):
    """Retrieve all documents from an index."""
    print(f"Backing up documents from {index_name}...")
    documents = get_all_documents(index_name)
    if documents:
        print(f"Retrieved {len(documents)} documents from {index_name}.")
        return documents
    else:
        print(f"No documents found in {index_name}.")
        return []


def reindex_documents(client, index_name, documents):
    """Reindex documents into a new index."""
    print(f"Reindexing documents into {index_name}...")
    for doc in documents:
        doc_id = doc["_id"]
        source = doc["_source"]
        if edit_document(index_name, doc_id, source):
            print(f"Document {doc_id} reindexed successfully in {index_name}.")
        else:
            print(f"Failed to reindex document {doc_id} in {index_name}.")
    print(f"Finished reindexing documents into {index_name}.")


def run_update():
    """This function defines the update process."""
    print("Running Elasticsearch index update script...")

    # Connect to Elasticsearch using the helper
    es = get_elasticsearch_client()
    if es is None:
        print("Failed to establish Elasticsearch connection.")
        return "abort"

    # Step 1: Create de_asset_fb_app_users index if it doesn't exist
    create_index_with_mapping(
        es,
        "de_asset_fb_app_users",
        {
            "properties": {
                "fb_app_user_title": {"type": "keyword"},
                "fb_app_user_token": {"type": "keyword"},
                "fb_app_user_auth_url": {"type": "keyword"},
                "fb_app_user_auth_state": {"type": "keyword"},
            }
        },
    )

    # Step 2: Update de_asset_fb_ad_accounts index if necessary
    index_name = "de_asset_fb_ad_accounts"
    required_fields = {
        "fb_ad_account_description": {"type": "keyword"},
        "fb_app_user_esid": {"type": "keyword"},
        "fb_app_user_title": {"type": "keyword"},
    }

    # Backup documents only if the index exists
    if es.indices.exists(index=index_name):
        ad_accounts_docs = backup_documents(es, index_name)
    else:
        print(f"Index {index_name} not found. Skipping backup and update.")
        ad_accounts_docs = []

    # Check if the index needs to be deleted and recreated
    recreate_index = False
    for field in required_fields:
        if not index_has_field(es, index_name, field):
            print(f"Field {field} is missing from {index_name}. Marking for recreation.")
            recreate_index = True
            break

    # Modify documents before reindexing
    for doc in ad_accounts_docs:
        source = doc["_source"]
        if "fb_add_acount_description" in source:
            description_value = source.pop("fb_add_acount_description", None)
            if description_value:
                source["fb_ad_account_description"] = description_value
        if "fb_ad_account_description" not in source:
            source["fb_ad_account_description"] = ""
        if "fb_app_user_esid" not in source:
            source["fb_app_user_esid"] = ""
        if "fb_app_user_title" not in source:
            source["fb_app_user_title"] = ""

    if recreate_index:
        delete_index(es, index_name)

        # Create the index with the updated mapping
        ad_account_mapping = {
            "properties": {
                "fb_ad_account_id": {"type": "keyword"},
                "fb_ad_account_title": {"type": "keyword"},
                "fb_ad_account_description": {"type": "keyword"},
                "fb_app_esid": {"type": "keyword"},
                "fb_app_title": {"type": "keyword"},
                "fb_app_user_esid": {"type": "keyword"},
                "fb_app_user_title": {"type": "keyword"},
            }
        }
        create_index_with_mapping(es, index_name, ad_account_mapping)
        reindex_documents(es, index_name, ad_accounts_docs)
    else:
        print(f"No changes required for {index_name}. Skipping index recreation.")

    # Step 3: Update de_asset_teams index with new fields if needed
    team_index_name = "de_asset_teams"
    team_new_fields = {
        "fb_app_user_esid": {"type": "keyword"},
        "fb_app_user_title": {"type": "keyword"},
    }

    # Check if fields need to be added to the teams index
    fields_to_add = {}
    for field, mapping in team_new_fields.items():
        if not index_has_field(es, team_index_name, field):
            fields_to_add[field] = mapping

    if fields_to_add:
        try:
            es.indices.put_mapping(index=team_index_name, body={"properties": fields_to_add})
            print(f"Updated mapping for {team_index_name} with fields: {', '.join(fields_to_add.keys())}.")
        except Exception as e:
            print(f"Error updating mapping for {team_index_name}: {e}")
            return "abort"
    else:
        print(f"No new fields needed for {team_index_name}. Skipping update.")

    print("Elasticsearch update script completed.")
    return "success"


if __name__ == "__main__":
    run_update()
