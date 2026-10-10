# update_v10.py
# Rename team_facebook_page field to team_facebook_page_id in Teams assets

import sys
import os

# Add parent directories to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.es_helpers import get_elasticsearch_client, get_all_documents, edit_document


def run_update():
    """Rename team_facebook_page to team_facebook_page_id in all Teams documents."""
    print("Running team_facebook_page -> team_facebook_page_id field rename...")

    # Connect to Elasticsearch
    es = get_elasticsearch_client()
    if es is None:
        print("Failed to establish Elasticsearch connection.")
        return "abort"

    # Get all Teams
    index_name = "de_asset_teams"
    print(f"Fetching all documents from {index_name}...")

    try:
        teams = get_all_documents(index_name)
        print(f"Found {len(teams)} Teams to check.")
    except Exception as e:
        print(f"Error fetching documents: {e}")
        return "abort"

    updated_count = 0
    skipped_count = 0

    for team in teams:
        doc_id = team["_id"]
        source = team["_source"]

        print(f"\nProcessing team: {source.get('team_title', 'Unknown')}")

        # Check if old field exists
        if "team_facebook_page" in source:
            # Copy value to new field name
            source["team_facebook_page_id"] = source["team_facebook_page"]
            # Remove old field
            del source["team_facebook_page"]

            print(f"  - Renamed team_facebook_page -> team_facebook_page_id")

            # Update document
            if edit_document(index_name, doc_id, source):
                print(f"  ✓ Updated successfully")
                updated_count += 1
            else:
                print(f"  ✗ Failed to update")
        else:
            print(f"  - No team_facebook_page field, skipping")
            skipped_count += 1

    print(f"\n{'='*60}")
    print(f"Update complete!")
    print(f"  Updated: {updated_count} teams")
    print(f"  Skipped: {skipped_count} teams")
    print(f"{'='*60}")

    return "success"


if __name__ == "__main__":
    run_update()
