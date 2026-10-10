from lib.es_helpers import get_elasticsearch_client  # Import the connection helper
import json
import os


def create_index_with_mapping(client, index_name, mapping):
    """Create an Elasticsearch index with the specified mapping."""
    # Check if index already exists
    if not client.indices.exists(index=index_name):
        print(f"Creating index: {index_name}")
        print(f"Mapping for {index_name}: {json.dumps(mapping, indent=4)}")  # Print the mapping for debugging
        try:
            # Create the index with the provided mapping
            client.indices.create(index=index_name, body={"mappings": mapping})
        except Exception as e:
            print(f"Error creating index {index_name}: {e}")
    else:
        print(f"Index {index_name} already exists.")


def run_update():
    """This function defines the update process."""
    print("Running Elasticsearch index creation script...")

    # Connect to Elasticsearch using the helper
    es = get_elasticsearch_client()

    if es is None:
        print("Failed to establish Elasticsearch connection.")
        return "abort"

    # Define the indices for asset management
    indices_with_mappings = {
        "de_asset_regions": {
            "properties": {
                "region_title": {"type": "keyword"},
                "region_description": {"type": "keyword"},
            }
        },
        "de_asset_countries": {
            "properties": {
                "country_title": {"type": "keyword"},
                "country_description": {"type": "keyword"},
            }
        },
        "de_asset_cities": {
            "properties": {
                "city_title": {"type": "keyword"},
                "city_description": {"type": "keyword"},
            }
        },
        "de_asset_fb_pages": {
            "properties": {
                "fb_page_id": {"type": "keyword"},
                "fb_page_name": {"type": "keyword"},
                "fb_page_slug": {"type": "keyword"},
            }
        },
        "de_asset_fb_accounts": {
            "properties": {
                "fb_account_id": {"type": "keyword"},
                "fb_account_title": {"type": "keyword"},
                "fb_account_description": {"type": "keyword"},
            }
        },
        "de_asset_fb_apps": {
            "properties": {
                "fb_app_id": {"type": "keyword"},
                "fb_app_secret": {"type": "keyword"},
                "fb_app_token": {"type": "keyword"},
                "fb_app_title": {"type": "keyword"},
                "fb_app_description": {"type": "keyword"},
                "fb_account_esid": {"type": "keyword"},
                "fb_account_title": {"type": "keyword"},
            }
        },
        "de_asset_fb_ad_accounts": {
            "properties": {
                "fb_ad_account_id": {"type": "keyword"},
                "fb_ad_account_title": {"type": "keyword"},
                "fb_add_acount_description": {"type": "keyword"},
                "fb_app_esid": {"type": "keyword"},
                "fb_app_title": {"type": "keyword"},
            }
        },
        "de_asset_teams": {
            "properties": {
                "team_title": {"type": "keyword"},
                "team_region": {"type": "keyword"},
                "team_url": {"type": "keyword"},
                "team_country": {"type": "keyword"},
                "team_city": {"type": "keyword"},
                "team_fb_integration_status": {"type": "keyword"},
                "fb_page_esid": {"type": "keyword"},
                "fb_page_name": {"type": "keyword"},
                "fb_ad_account_esid": {"type": "keyword"},
                "fb_ad_account_title": {"type": "keyword"},
            }
        },
    }

    # Create each index with its mapping
    for index_name, mapping in indices_with_mappings.items():
        create_index_with_mapping(es, index_name, mapping)

    print("Elasticsearch index creation completed.")
    return "success"  # Return 'success' to indicate that the update was applied


if __name__ == "__main__":
    run_update()
