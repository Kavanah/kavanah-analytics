from lib.es_helpers import get_elasticsearch_client
import json


def index_has_field(client, index_name, field_name):
    """Check if an index has a specific field in its mappings."""
    try:
        mapping = client.indices.get_mapping(index=index_name)
        properties = mapping[index_name]["mappings"].get("properties", {})
        return field_name in properties
    except Exception as e:
        print(f"Error checking field {field_name} in {index_name}: {e}")
        return False


def add_fields_to_index(client, index_name, new_fields):
    """Add new fields to an Elasticsearch index if they do not exist."""
    fields_to_add = {}
    for field, mapping in new_fields.items():
        if not index_has_field(client, index_name, field):
            fields_to_add[field] = mapping

    if fields_to_add:
        print(f"Adding new fields to {index_name}: {', '.join(fields_to_add.keys())}")
        try:
            client.indices.put_mapping(index=index_name, body={"properties": fields_to_add})
            print(f"Updated mapping for {index_name} with fields: {', '.join(fields_to_add.keys())}.")
        except Exception as e:
            print(f"Error updating mapping for {index_name}: {e}")
    else:
        print(f"No new fields needed for {index_name}. Skipping update.")


def run_update():
    """This function defines the update process for adding fields to de_asset_fb_app_users."""
    print("Running Elasticsearch field update script for de_asset_fb_app_users...")

    # Connect to Elasticsearch using the helper
    es = get_elasticsearch_client()
    if es is None:
        print("Failed to establish Elasticsearch connection.")
        return "abort"

    # Define the new fields to be added
    new_fields = {
        "fb_app_esid": {"type": "keyword"},
        "fb_app_title": {"type": "keyword"},
    }

    # Add new fields to the de_asset_fb_app_users index
    add_fields_to_index(es, "de_asset_fb_app_users", new_fields)

    print("Elasticsearch field update script completed.")
    return "success"  # Return 'success' to indicate that the update was applied


if __name__ == "__main__":
    run_update()
