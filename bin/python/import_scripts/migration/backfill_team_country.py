"""
Backfill team_country in existing Elasticsearch import data documents.

For each team, resolves the correct ISO2 country code(s) from team_country_text
(falling back to team_country), then runs update_by_query on all import data
indices to fill in team_country where it is currently missing/empty.

Indices targeted:
  - *fb_insights
  - *fb_page_insights
  - *ig_insights
  - *youtube_analytics
  - *ga_metrics
  - *echo_conversations
"""

import sys
import os

# Allow running from the container's /app/python path
sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client


def resolve_team_country(team):
    """Return team country from team_country_text (ISO2) when team_country is not set."""
    country = (team.get("team_country") or "").strip()
    if country:
        return country
    country_text = team.get("team_country_text") or ""
    # ES may store team_country_text as a list or a comma-separated string
    if isinstance(country_text, list):
        codes = [c.strip() for c in country_text if c.strip()]
    else:
        codes = [c.strip() for c in country_text.split(",") if c.strip()]
    if not codes:
        return None
    return codes[0] if codes else None


def fetch_all_teams(es):
    """Fetch all teams from de_asset_teams."""
    resp = es.search(
        index="de_asset_teams",
        body={
            "size": 1000,
            "query": {"match_all": {}},
            "_source": ["team_title", "team_country", "team_country_text"],
        },
    )
    teams = []
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        teams.append(src)
    return teams


def backfill_index(es, index_pattern, team_title, country_value):
    """
    Run update_by_query on index_pattern for docs with the given team_title
    where team_country is missing or empty. Sets team_country to country_value.
    """
    country_str = country_value

    body = {
        "query": {
            "bool": {
                "must": [{"match_phrase": {"team_title": team_title}}],
                "should": [
                    {"bool": {"must_not": {"exists": {"field": "team_country"}}}},
                    {"term": {"team_country": ""}},
                    {"term": {"team_country": "unknown"}},
                ],
                "minimum_should_match": 1,
            }
        },
        "script": {
            "source": "ctx._source.team_country = params.v",
            "lang": "painless",
            "params": {"v": country_str},
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
            print(f"  [{index_pattern}] team={team_title!r}: updated {updated} docs")
        return updated
    except Exception as e:
        err = str(e)
        if "index_not_found" in err or "no such index" in err.lower():
            return 0  # Index doesn't exist yet — skip silently
        print(f"  [{index_pattern}] team={team_title!r}: ERROR: {err[:200]}")
        return 0


def main():
    es = get_elasticsearch_client()

    print("Fetching teams from de_asset_teams...")
    teams = fetch_all_teams(es)
    print(f"Found {len(teams)} teams.")

    # Index patterns to update
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
            print(f"  Skipping {team_title!r} — no country info")
            continue

        print(f"\nTeam: {team_title!r}  ->  country={country!r}")
        for pattern in index_patterns:
            n = backfill_index(es, pattern, team_title, country)
            total_updated += n

    print(f"\nDone. Total documents updated: {total_updated}")


if __name__ == "__main__":
    main()
