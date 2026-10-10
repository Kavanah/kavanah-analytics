"""
One-time migration: remove the team_country field from all de_asset_teams documents.

team_country (single ISO2 string) was added by backfill_de_asset_teams_country.py
and the updated importer. We are no longer using team_country to locate
teams on the map, so the field is being removed entirely.

team_country_text (raw comma-string from the spreadsheet) is left intact.

Run once:
  sudo docker exec imports_container python3 \
    /app/python/import_scripts/migration/remove_team_country_from_de_asset_teams.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from lib.es_helpers import get_elasticsearch_client


def main():
    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: Could not connect to Elasticsearch")
        sys.exit(1)

    resp = es.search(
        index="de_asset_teams",
        body={
            "size": 1000,
            "query": {"exists": {"field": "team_country"}},
            "_source": ["team_title", "team_country"],
        },
    )
    teams = resp["hits"]["hits"]
    print(f"Found {len(teams)} de_asset_teams documents with team_country")

    if not teams:
        print("Nothing to do.")
        return

    updated = 0
    for hit in teams:
        doc_id = hit["_id"]
        src = hit["_source"]
        title = src.get("team_title", doc_id)

        es.update(
            index="de_asset_teams",
            id=doc_id,
            body={
                "script": {
                    "source": 'ctx._source.remove("team_country")',
                    "lang": "painless",
                }
            },
        )
        print(f"  Removed team_country from {title!r}")
        updated += 1

    print(f"\nDone — removed team_country from {updated} documents")


if __name__ == "__main__":
    main()
