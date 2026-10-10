"""
Pre-warm the FB city geocode cache used by the map dashboard.

Iterates every unique (city_name, region, country_code) triplet referenced
in adset_targeting.geo_locations.cities across *fb_insights indices and
calls lib.geocoding.geocode_city on each.  Results are persisted to
/app/data/geocoding_cache.json — the same cache the web app reads.

The region is included in the cache key so ambiguous names like "Katra"
(present in both Jammu and Bihar) resolve to the city FB actually
targeted, matching what the FB import path writes.

when invoked with --team_ids the aggregation only scans index
patterns derived from the filtered team titles (e.g. acme_*fb_insights),
not every team's index.

Run standalone:
    sudo docker exec -w /app/python imports_container \\
        python3 import_scripts/facebook/prewarm_city_geocache.py
"""

import logging
import sys

sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client, sanitize_team_title  # noqa: E402
from lib.geocoding import _load_cache, geocode_city  # noqa: E402


def _index_pattern_for_team_ids(team_ids):
    """Translate a comma-separated team_ids string to an ES index pattern.

    With no team_ids → "*fb_insights" (current behavior).
    With team_ids → comma-separated per-team patterns
    e.g. "acme*fb_insights,other*fb_insights".
    """
    if not team_ids:
        return "*fb_insights"
    titles = [t.strip() for t in team_ids.split(",") if t.strip()]
    if not titles:
        return "*fb_insights"
    # Use the same sanitize logic the importer uses for index naming so the
    # patterns match exactly what was written.
    return ",".join(f"{sanitize_team_title(t)}*fb_insights" for t in titles)


def _collect_fb_targeting_cities(es, index_pattern="*fb_insights"):
    """Return sorted list of unique (city_name, region, country_code) triplets."""
    triples = set()
    resp = es.search(
        index=index_pattern,
        body={
            "size": 0,
            "aggs": {
                "by_city": {
                    "terms": {
                        "field": "adset_targeting.geo_locations.cities.name.keyword",
                        "size": 5000,
                    },
                    "aggs": {
                        "sample": {
                            "top_hits": {
                                "size": 1,
                                "_source": ["adset_targeting.geo_locations.cities"],
                            }
                        }
                    },
                }
            },
        },
        ignore_unavailable=True,
        allow_no_indices=True,
    )
    buckets = (
        (resp.body if hasattr(resp, "body") else dict(resp)).get("aggregations", {}).get("by_city", {}).get("buckets", [])
    )
    for bucket in buckets:
        name = bucket["key"]
        hits = bucket.get("sample", {}).get("hits", {}).get("hits", [])
        region = ""
        country = ""
        for hit in hits:
            cities = hit.get("_source", {}).get("adset_targeting", {}).get("geo_locations", {}).get("cities", [])
            for c in cities:
                if c.get("name") == name:
                    if not region:
                        region = c.get("region", "") or ""
                    if not country:
                        country = c.get("country", "") or ""
            if region and country:
                break
        triples.add((name, region, country))
    return sorted(triples)


def prewarm(team_ids=None):
    """Warm the geocode cache for every FB targeting city, with region context.

    Args:
        team_ids: optional comma-separated team title substrings. When set,
            the aggregation only walks indices whose names start with the
            sanitized team titles..
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Elasticsearch client unavailable")
        return 1

    index_pattern = _index_pattern_for_team_ids(team_ids)
    if team_ids:
        logging.info(f"Scoped to team_ids={team_ids!r} → index pattern '{index_pattern}'")

    triples = _collect_fb_targeting_cities(es, index_pattern=index_pattern)
    logging.info(f"Found {len(triples)} unique (city, region, country) triplets")

    cache = _load_cache()
    already = sum(1 for name, region, cc in triples if f"city|{name}|{region}|{cc}" in cache)
    logging.info(f"{already} already in cache, {len(triples) - already} to geocode")

    ok, missed = 0, 0
    for i, (name, region, cc) in enumerate(triples, 1):
        key = f"city|{name}|{region}|{cc}"
        if key in cache:
            continue
        logging.info(f"[{i}/{len(triples)}] geocoding {name!r} ({region!r}, {cc})")
        coords = geocode_city(name, region, cc)
        if coords:
            ok += 1
        else:
            missed += 1

    logging.info(f"Done. Newly cached: {ok}. Unresolvable: {missed}.")
    return 0


def main(params=None):
    """Entrypoint for the import.py dispatcher (`facebook prewarm_city_geocache`)."""
    team_ids = (params or {}).get("team_ids") or None
    return prewarm(team_ids=team_ids)


if __name__ == "__main__":
    sys.exit(prewarm())
