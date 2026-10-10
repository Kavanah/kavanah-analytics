"""
Migration script: Rename Affinities to Regions
- Creates new de_asset_regions index
- Copies data from de_asset_affinities with renamed fields
- Updates all team documents to use new field names
"""

import sys
import os

# Add the python directory to the path so we can import lib modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from lib.es_helpers import get_elasticsearch_client


def run_update():
    """Migrate affinities to regions."""
    print("Running migration: Affinities -> Regions...")

    es = get_elasticsearch_client()
    if es is None:
        print("Failed to establish Elasticsearch connection.")
        return "abort"

    # Step 1: Create the new de_asset_regions index
    regions_mapping = {
        "properties": {
            "region_title": {"type": "keyword"},
            "region_description": {"type": "keyword"},
        }
    }

    if not es.indices.exists(index="de_asset_regions"):
        print("Creating index: de_asset_regions")
        try:
            es.indices.create(index="de_asset_regions", body={"mappings": regions_mapping})
            print("Index de_asset_regions created successfully.")
        except Exception as e:
            print(f"Error creating index de_asset_regions: {e}")
            return "abort"
    else:
        print("Index de_asset_regions already exists.")

    # Step 2: Copy documents from de_asset_affinities to de_asset_regions
    if es.indices.exists(index="de_asset_affinities"):
        print("Copying documents from de_asset_affinities to de_asset_regions...")
        try:
            # Get all documents from the old index
            result = es.search(
                index="de_asset_affinities",
                body={"query": {"match_all": {}}, "size": 1000},
            )
            hits = result.get("hits", {}).get("hits", [])

            for hit in hits:
                old_doc = hit["_source"]
                doc_id = hit["_id"]

                # Map old field names to new field names
                new_doc = {
                    "region_title": old_doc.get("affinity_title", ""),
                    "region_description": old_doc.get("affinity_description", ""),
                }

                # Index with the same document ID to maintain references
                es.index(index="de_asset_regions", id=doc_id, body=new_doc)
                print(f"Migrated affinity '{old_doc.get('affinity_title')}' -> region '{new_doc['region_title']}'")

            print(f"Migrated {len(hits)} documents to de_asset_regions.")
        except Exception as e:
            print(f"Error copying documents: {e}")
            return "abort"
    else:
        print("Index de_asset_affinities does not exist. Skipping document migration.")

    # Step 3: Update de_asset_teams documents to use new field names
    if es.indices.exists(index="de_asset_teams"):
        print("Updating team documents with new field names...")
        try:
            # Get all team documents
            result = es.search(index="de_asset_teams", body={"query": {"match_all": {}}, "size": 1000})
            hits = result.get("hits", {}).get("hits", [])

            updated_count = 0
            for hit in hits:
                doc = hit["_source"]
                doc_id = hit["_id"]
                needs_update = False

                # Rename team_affinity -> team_region
                if "team_affinity" in doc:
                    doc["team_region"] = doc.pop("team_affinity")
                    needs_update = True

                # Rename team_affinity_esid -> team_region_esid
                if "team_affinity_esid" in doc:
                    doc["team_region_esid"] = doc.pop("team_affinity_esid")
                    needs_update = True

                if needs_update:
                    es.index(index="de_asset_teams", id=doc_id, body=doc)
                    print(f"Updated team: {doc.get('team_title', doc_id)}")
                    updated_count += 1

            print(f"Updated {updated_count} team documents.")
        except Exception as e:
            print(f"Error updating team documents: {e}")
            return "abort"
    else:
        print("Index de_asset_teams does not exist. Skipping team updates.")

    print("\nMigration completed successfully!")
    print("Note: The old de_asset_affinities index has been preserved.")
    print("You can delete it manually after verifying the migration: DELETE /de_asset_affinities")

    return "success"


if __name__ == "__main__":
    run_update()
