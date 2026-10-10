# update_v9.py
# Fix GA Properties fields to match new store_esid pattern
# Converts ga_account_esid and team_esid fields to use the correct title/esid structure

import sys
import os

# Add parent directories to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.es_helpers import get_elasticsearch_client, get_all_documents, edit_document


def run_update():
    """Fix GA Properties to use correct field structure for ga_account and team references."""
    print("Running GA Properties field structure update...")

    # Connect to Elasticsearch
    es = get_elasticsearch_client()
    if es is None:
        print("Failed to establish Elasticsearch connection.")
        return "abort"

    # Get all GA Properties
    index_name = "de_asset_ga_properties"
    print(f"Fetching all documents from {index_name}...")

    try:
        properties = get_all_documents(index_name)
        print(f"Found {len(properties)} GA Properties to update.")
    except Exception as e:
        print(f"Error fetching documents: {e}")
        return "abort"

    # Get all GA Accounts for lookup (title -> ID mapping)
    print("Fetching GA Accounts for reference lookup...")
    try:
        ga_accounts = get_all_documents("de_asset_ga_accounts")
        ga_account_lookup = {doc["_source"]["ga_account_title"]: doc["_id"] for doc in ga_accounts}
        print(f"Loaded {len(ga_account_lookup)} GA Account mappings.")
    except Exception as e:
        print(f"Error fetching GA Accounts: {e}")
        ga_account_lookup = {}

    # Get all Teams for lookup (title -> ID mapping)
    print("Fetching Teams for reference lookup...")
    try:
        teams = get_all_documents("de_asset_teams")
        team_lookup = {doc["_source"]["team_title"]: doc["_id"] for doc in teams}
        print(f"Loaded {len(team_lookup)} Team mappings.")
    except Exception as e:
        print(f"Error fetching Teams: {e}")
        team_lookup = {}

    updated_count = 0
    skipped_count = 0

    for prop in properties:
        doc_id = prop["_id"]
        source = prop["_source"]
        modified = False

        print(f"\nProcessing property: {source.get('ga_property_title', 'Unknown')}")

        # Fix ga_account_esid field
        if "ga_account_esid" in source:
            old_value = source["ga_account_esid"]

            # Check if it's already an ID (alphanumeric) or a title
            # If it's a title, look up the ID
            if old_value in ga_account_lookup:
                # It's a title, convert to ID
                new_id = ga_account_lookup[old_value]
                source["ga_account_title"] = old_value  # Store the title
                source["ga_account_esid"] = new_id  # Store the ID
                print(f"  - Converted ga_account: '{old_value}' -> ID: {new_id}")
                modified = True
            else:
                # Assume it's already an ID, just add the title field
                # Try to find the title by ID
                matching_account = next((acc for acc in ga_accounts if acc["_id"] == old_value), None)
                if matching_account:
                    source["ga_account_title"] = matching_account["_source"]["ga_account_title"]
                    print(f"  - Added ga_account_title field from existing ID")
                    modified = True
                else:
                    print(f"  - Warning: Could not find GA Account with ID {old_value}")

        # Fix team_esid field
        if "team_esid" in source:
            old_value = source["team_esid"]

            # Check if it's already an ID or a title
            if old_value in team_lookup:
                # It's a title, convert to ID
                new_id = team_lookup[old_value]
                source["team_title"] = old_value  # Store the title
                source["team_esid"] = new_id  # Store the ID
                print(f"  - Converted team: '{old_value}' -> ID: {new_id}")
                modified = True
            else:
                # Assume it's already an ID, just add the title field
                matching_team = next((t for t in teams if t["_id"] == old_value), None)
                if matching_team:
                    source["team_title"] = matching_team["_source"]["team_title"]
                    print(f"  - Added team_title field from existing ID")
                    modified = True
                else:
                    print(f"  - Warning: Could not find Team with ID {old_value}")

        # Update document if modified
        if modified:
            if edit_document(index_name, doc_id, source):
                print(f"  ✓ Updated successfully")
                updated_count += 1
            else:
                print(f"  ✗ Failed to update")
        else:
            print(f"  - No changes needed")
            skipped_count += 1

    print(f"\n{'='*60}")
    print(f"Update complete!")
    print(f"  Updated: {updated_count} properties")
    print(f"  Skipped: {skipped_count} properties")
    print(f"{'='*60}")

    return "success"


if __name__ == "__main__":
    run_update()
