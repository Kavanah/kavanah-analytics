"""
Redistribute audience-level geographic fields across Kavanah Team documents.

The demo data generator picks locations uniformly from DEMO_LOCATIONS, but
existing documents may be skewed toward US locations.  This script fetches
every document in the audience-facing indices, assigns a new random location
from the full DEMO_LOCATIONS pool, and bulk-updates the documents in place.

Audience-level fields updated per index:
  - *_fb_insights       → location_cities[].name / .coordinates
  - *_ga_metrics        → country, city
  - *_echo_conversations → country
  - disciple-tools       → meta.contact_address  (filtered to team)

Usage (inside imports_container):
    python3 /app/python/import_scripts/migration/redistribute_audience_geo.py
    python3 /app/python/import_scripts/migration/redistribute_audience_geo.py --team "Kavanah Team"
    python3 /app/python/import_scripts/migration/redistribute_audience_geo.py --dry-run
"""

import argparse
import random
import sys
import os

sys.path.insert(0, "/app/python")
# Also support running locally from the bin/python directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

DEMO_LOCATIONS = [
    {"city": "New York", "country": "United States", "iso3": "USA", "lat": 40.7128, "lon": -74.0060},
    {"city": "London", "country": "United Kingdom", "iso3": "GBR", "lat": 51.5074, "lon": -0.1278},
    {"city": "Tokyo", "country": "Japan", "iso3": "JPN", "lat": 35.6762, "lon": 139.6503},
    {"city": "Sydney", "country": "Australia", "iso3": "AUS", "lat": -33.8688, "lon": 151.2093},
    {"city": "Berlin", "country": "Germany", "iso3": "DEU", "lat": 52.5200, "lon": 13.4050},
    {"city": "Sao Paulo", "country": "Brazil", "iso3": "BRA", "lat": -23.5505, "lon": -46.6333},
    {"city": "Lagos", "country": "Nigeria", "iso3": "NGA", "lat": 6.5244, "lon": 3.3792},
    {"city": "Mumbai", "country": "India", "iso3": "IND", "lat": 19.0760, "lon": 72.8777},
    {"city": "Dubai", "country": "United Arab Emirates", "iso3": "ARE", "lat": 25.2048, "lon": 55.2708},
    {"city": "Toronto", "country": "Canada", "iso3": "CAN", "lat": 43.6532, "lon": -79.3832},
    {"city": "Nairobi", "country": "Kenya", "iso3": "KEN", "lat": -1.2921, "lon": 36.8219},
    {"city": "Seoul", "country": "South Korea", "iso3": "KOR", "lat": 37.5665, "lon": 126.9780},
    {"city": "Mexico City", "country": "Mexico", "iso3": "MEX", "lat": 19.4326, "lon": -99.1332},
    {"city": "Jakarta", "country": "Indonesia", "iso3": "IDN", "lat": -6.2088, "lon": 106.8456},
    {"city": "Cairo", "country": "Egypt", "iso3": "EGY", "lat": 30.0444, "lon": 31.2357},
]


def jitter(val: float, spread: float = 0.3) -> float:
    """Add small random offset to a coordinate."""
    return round(val + random.uniform(-spread, spread), 4)


def rand_loc() -> dict:
    return random.choice(DEMO_LOCATIONS)


def sanitize(team_title: str) -> str:
    s = team_title.lower().replace(" ", "_").replace("-", "_")
    s = "".join(c if c.isalnum() or c == "_" else "_" for c in s)
    while "__" in s:
        s = s.replace("__", "_")
    return s


def scroll_all_ids(es, index: str, query: dict | None = None) -> list[dict]:
    """Scroll through an index and return list of {_id, _source} dicts."""
    body = {"query": query or {"match_all": {}}, "size": 500}
    docs = []
    resp = es.search(index=index, body=body, scroll="2m")
    scroll_id = resp.get("_scroll_id")
    hits = resp["hits"]["hits"]
    docs.extend(hits)

    while hits:
        resp = es.scroll(scroll_id=scroll_id, scroll="2m")
        scroll_id = resp.get("_scroll_id")
        hits = resp["hits"]["hits"]
        docs.extend(hits)

    if scroll_id:
        try:
            es.clear_scroll(scroll_id=scroll_id)
        except Exception:
            pass
    return docs


def update_fb_insights(es, index: str, dry_run: bool) -> int:
    """Update location_cities array with globally distributed locations."""
    docs = scroll_all_ids(es, index)
    if not docs:
        return 0

    updated = 0
    for doc in docs:
        loc1, loc2 = rand_loc(), rand_loc()
        new_cities = [
            {
                "name": loc1["city"],
                "coordinates": {
                    "lat": jitter(loc1["lat"]),
                    "lon": jitter(loc1["lon"]),
                },
            },
            {
                "name": loc2["city"],
                "coordinates": {
                    "lat": jitter(loc2["lat"]),
                    "lon": jitter(loc2["lon"]),
                },
            },
        ]
        if not dry_run:
            es.update(
                index=index,
                id=doc["_id"],
                body={"doc": {"location_cities": new_cities}},
            )
        updated += 1
    return updated


def update_ga_metrics(es, index: str, dry_run: bool) -> int:
    """Update country and city fields with globally distributed locations."""
    docs = scroll_all_ids(es, index)
    if not docs:
        return 0

    updated = 0
    for doc in docs:
        loc = rand_loc()
        if not dry_run:
            es.update(
                index=index,
                id=doc["_id"],
                body={"doc": {"country": loc["country"], "city": loc["city"]}},
            )
        updated += 1
    return updated


def update_echo_conversations(es, index: str, dry_run: bool) -> int:
    """Update country field with globally distributed locations."""
    docs = scroll_all_ids(es, index)
    if not docs:
        return 0

    updated = 0
    for doc in docs:
        loc = rand_loc()
        if not dry_run:
            es.update(
                index=index,
                id=doc["_id"],
                body={"doc": {"country": loc["country"]}},
            )
        updated += 1
    return updated


def update_disciple_tools(es, team_title: str, dry_run: bool) -> int:
    """Update meta.contact_address for contacts belonging to the team."""
    query = {
        "bool": {
            "must": [
                {"match_phrase": {"team_title": team_title}},
                {"term": {"post_type": "contacts"}},
            ]
        }
    }
    docs = scroll_all_ids(es, "disciple-tools", query)
    if not docs:
        return 0

    updated = 0
    for doc in docs:
        loc = rand_loc()
        if not dry_run:
            es.update(
                index="disciple-tools",
                id=doc["_id"],
                body={
                    "script": {
                        "source": "ctx._source.meta.contact_address = params.city",
                        "lang": "painless",
                        "params": {"city": loc["city"]},
                    }
                },
            )
        updated += 1
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Redistribute audience-level geo fields across demo data.")
    parser.add_argument(
        "--team",
        default="Kavanah Team",
        help="team_title to target (default: Kavanah Team)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count documents that would be updated without writing.",
    )
    args = parser.parse_args()

    team_title = args.team
    prefix = sanitize(team_title)
    dry_run = args.dry_run

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"{'='*60}")
    print(f"Redistribute Audience Geo Fields  [{mode}]")
    print(f"Team: {team_title}  |  Index prefix: {prefix}")
    print(f"{'='*60}")

    es = get_elasticsearch_client()
    if es is None:
        print("[ERROR] Cannot connect to Elasticsearch.")
        sys.exit(1)
    print("[OK] Connected to Elasticsearch.\n")

    random.seed()
    total = 0

    # 1. Facebook Insights — location_cities
    idx = f"{prefix}_fb_insights"
    try:
        n = update_fb_insights(es, idx, dry_run)
        print(f"  [{idx}] location_cities: {n} docs updated")
        total += n
    except Exception as e:
        if "index_not_found" in str(e):
            print(f"  [{idx}] not found — skipping")
        else:
            print(f"  [{idx}] ERROR: {e}")

    # 2. Google Analytics — country, city
    idx = f"{prefix}_ga_metrics"
    try:
        n = update_ga_metrics(es, idx, dry_run)
        print(f"  [{idx}] country/city: {n} docs updated")
        total += n
    except Exception as e:
        if "index_not_found" in str(e):
            print(f"  [{idx}] not found — skipping")
        else:
            print(f"  [{idx}] ERROR: {e}")

    # 3. Echo Conversations — country
    idx = f"{prefix}_echo_conversations"
    try:
        n = update_echo_conversations(es, idx, dry_run)
        print(f"  [{idx}] country: {n} docs updated")
        total += n
    except Exception as e:
        if "index_not_found" in str(e):
            print(f"  [{idx}] not found — skipping")
        else:
            print(f"  [{idx}] ERROR: {e}")

    # 4. Disciple Tools — meta.contact_address
    try:
        n = update_disciple_tools(es, team_title, dry_run)
        print(f"  [disciple-tools] contact_address: {n} docs updated")
        total += n
    except Exception as e:
        if "index_not_found" in str(e):
            print(f"  [disciple-tools] not found — skipping")
        else:
            print(f"  [disciple-tools] ERROR: {e}")

    print(f"\n{'='*60}")
    print(f"Done. Total documents {'would be ' if dry_run else ''}updated: {total}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
