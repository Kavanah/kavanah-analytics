from lib.es_helpers import get_elasticsearch_client
import json

INDEX_NAME = "de_asset_fb_app_users"


def run_update():
    """Run the update to add the fb_app_user_role field to the index."""
    print(f"Running update for index: {INDEX_NAME} to include fb_app_user_role field...")

    # Connect to Elasticsearch
    es = get_elasticsearch_client()
    if es is None:
        print("Failed to establish Elasticsearch connection.")
        return False  # Return False to indicate the update did not succeed

    # Define the new field mapping
    new_field_mapping = {"properties": {"fb_app_user_role": {"type": "keyword"}}}

    # Check if the index exists before updating the mapping
    if not es.indices.exists(index=INDEX_NAME):
        print(f"Index {INDEX_NAME} does not exist. Please create it first.")
        return False  # Return False for failure

    # Update the mapping
    try:
        es.indices.put_mapping(index=INDEX_NAME, body=new_field_mapping)
        print(f"Successfully added fb_app_user_role field to {INDEX_NAME}.")
        return True  # Return True for success
    except Exception as e:
        print(f"Error updating index {INDEX_NAME}: {e}")
        return False  # Return False for failure
