"""
One-time migration: normalize team_country to a single string in all import indices.

Fixes documents where team_country was stored as a Python list (e.g. ["US", "CA"])
or as a comma-separated string (e.g. "US,CA") due to a bug in _resolve_team_country().

Unlike backfill_team_country.py (which only fills missing/empty values), this script
overwrites team_country unconditionally for every document belonging to each team,
ensuring consistent single-string values everywhere.

Run once after deploying the _resolve_team_country() fix:
  sudo docker exec imports_container python3 /app/python/import_scripts/migration/normalize_team_country.py
"""

import sys
import os

sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client


def resolve_team_country(team):
    """Return the correct single-string ISO2 country code for a team.

    Mirrors the fixed _resolve_team_country() logic: always returns a single
    string (the first code), never a list.
    """
    country = team.get("team_country")
    if isinstance(country, list):
        country = country[0] if country else None
    country = (country or "").strip()
    # If team_country is already a valid single code, use it (skip comma-strings)
    if country and "," not in country:
        return country
    # Fall back to team_country_text, or split comma-string from team_country
    source = country if "," in country else (team.get("team_country_text") or "")
    if isinstance(source, list):
        source = source[0] if source else ""
    source = (source or "").strip()
    codes = [c.strip() for c in source.split(",") if c.strip()]
    return codes[0] if codes else None


def fetch_all_teams(es):
    resp = es.search(
        index="de_asset_teams",
        body={
            "size": 1000,
            "query": {"match_all": {}},
            "_source": ["team_title", "team_country", "team_country_text"],
        },
    )
    return [hit["_source"] for hit in resp["hits"]["hits"]]


def normalize_index(es, index_pattern, team_title, country_value):
    """Overwrite team_country for ALL docs matching team_title in index_pattern."""
    body = {
        "query": {"match_phrase": {"team_title": team_title}},
        "script": {
            "source": "ctx._source.team_country = params.v",
            "lang": "painless",
            "params": {"v": country_value},
        },
    }
    try:
        result = es.update_by_query(
            index=index_pattern,
            body=body,
            conflicts="proceed",
            wait_for_completion=True,
        )
        updated = result.get("updated", 0)
        if updated > 0:
            print(f"  [{index_pattern}] {team_title!r}: normalized {updated} docs")
        return updated
    except Exception as e:
        err = str(e)
        if "index_not_found" in err or "no such index" in err.lower():
            return 0
        print(f"  [{index_pattern}] {team_title!r}: ERROR: {err[:200]}")
        return 0


def main():
    es = get_elasticsearch_client()

    print("Fetching teams from de_asset_teams...")
    teams = fetch_all_teams(es)
    print(f"Found {len(teams)} teams.\n")

    index_patterns = [
        "*fb_insights",
        "*fb_page_insights",
        "*ig_insights",
        "*youtube_analytics",
        "*ga_metrics",
        "*echo_conversations",
    ]

    total_updated = 0

    for team in teams:
        team_title = (team.get("team_title") or "").strip()
        if not team_title:
            continue

        country = resolve_team_country(team)
        if not country:
            print(f"Skipping {team_title!r} — no country info")
            continue

        print(f"Team: {team_title!r}  ->  {country!r}")
        for pattern in index_patterns:
            total_updated += normalize_index(es, pattern, team_title, country)

    print(f"\nDone. Total documents normalized: {total_updated}")


if __name__ == "__main__":
    main()
