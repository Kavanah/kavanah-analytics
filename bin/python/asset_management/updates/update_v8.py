# update_8.py

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

    # Update de_asset_countries index
    index_name = "de_asset_countries"
    required_fields = {"country_iso3_code": {"type": "keyword"}}

    # Backup documents only if the index exists
    if es.indices.exists(index=index_name):
        country_docs = backup_documents(es, index_name)
    else:
        print(f"Index {index_name} not found. Skipping backup and update.")
        country_docs = []

    # Check if the index needs to be deleted and recreated
    recreate_index = False
    for field in required_fields:
        if not index_has_field(es, index_name, field):
            print(f"Field {field} is missing from {index_name}. Marking for recreation.")
            recreate_index = True
            break

    # Modify documents before reindexing
    for doc in country_docs:
        source = doc["_source"]
        if "country_iso3_code" not in source:
            source["country_iso3_code"] = ""

    if recreate_index:
        delete_index(es, index_name)

        # Create the index with the updated mapping
        country_mapping = {
            "properties": {
                "country_title": {"type": "keyword"},
                "country_description": {"type": "keyword"},
                "country_iso3_code": {"type": "keyword"},
            }
        }
        create_index_with_mapping(es, index_name, country_mapping)
        reindex_documents(es, index_name, country_docs)
    else:
        print(f"No changes required for {index_name}. Skipping index recreation.")

    print("Elasticsearch update script completed.")
    return "success"


if __name__ == "__main__":
    run_update()
