from lib.es_helpers import get_elasticsearch_client


def run_update():
    """This function defines the update process for de_asset_fb_app_users index."""
    print("Running Elasticsearch update script for adding fields to de_asset_fb_app_users...")

    es = get_elasticsearch_client()
    if es is None:
        print("Failed to connect to Elasticsearch.")
        return "failed"

    index_name = "de_asset_fb_app_users"
    mapping_update = {
        "properties": {
            "fb_app_user_fbid": {"type": "keyword"},
            "fb_app_user_token_exp": {"type": "date", "format": "epoch_second"},
        }
    }

    try:
        # Update the index mapping
        es.indices.put_mapping(index=index_name, body=mapping_update)
        print(f"Mapping update applied to index '{index_name}' successfully.")
        return "success"
    except Exception as e:
        print(f"Error updating mappings for index '{index_name}': {e}")
        return "failed"
