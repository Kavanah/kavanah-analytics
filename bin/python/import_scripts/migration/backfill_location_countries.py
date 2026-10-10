"""re-derive location_countries (and location_cities for
custom_locations) from existing adset_targeting on every fb_insights doc.

The geocoder change adds support for ``custom_locations`` and
``geo_markets`` and propagates per-city/region ``country`` codes into
``location_countries``. New imports pick this up automatically. This
migration applies the same logic to the docs already in ES so the
Impact Report has real geo data without waiting for a fresh import.

Idempotent: documents whose adset_targeting produces the same
location_countries we'd compute now are skipped.

Run inside imports_container (after pulling):
    sudo docker exec imports_container python3 \\
        /app/python/import_scripts/migration/backfill_location_countries.py [--team <TEAM_TITLE>] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, "/app/python")

import lib.logging  # noqa: F401  — sets up root logger
from lib.es_helpers import get_elasticsearch_client
from lib.geocoding import geocode_all_targeting_levels


_INDEX_PATTERN = "*fb_insights"
_PAGE_SIZE = 500


def _scan_targeting(es, team_title=None):
    """Yield (index, _id, adset_targeting, current_levels) for every fb_insights
    doc that has adset_targeting set."""
    must = [{"exists": {"field": "adset_targeting"}}]
    if team_title:
        must.append({"match_phrase": {"team_title": team_title}})

    body = {
        "query": {"bool": {"must": must}},
        "_source": [
            "adset_targeting",
            "location_cities",
            "location_regions",
            "location_countries",
        ],
        "sort": ["_doc"],
        "size": _PAGE_SIZE,
    }
    resp = es.search(index=_INDEX_PATTERN, body=body, scroll="2m")
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp["hits"]["hits"]
            if not hits:
                return
            for h in hits:
                src = h.get("_source") or {}
                yield (
                    h["_index"],
                    h["_id"],
                    src.get("adset_targeting") or {},
                    {
                        "location_cities": src.get("location_cities"),
                        "location_regions": src.get("location_regions"),
                        "location_countries": src.get("location_countries"),
                    },
                )
            resp = es.scroll(scroll_id=scroll_id, scroll="2m")
    finally:
        if scroll_id:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass


def _planned_update(targeting, current):
    """Compute the doc-update body for a single fb_insights doc.

    Returns ``None`` if the doc already matches the geocoder output
    (idempotent skip), otherwise a ``{"doc": {...}}`` body for
    ``es.update``.
    """
    levels = geocode_all_targeting_levels(targeting)

    # Map the importer's storage shape: 'city' → location_cities, etc.
    derived = {
        "location_cities": levels.get("city"),
        "location_regions": levels.get("region"),
        "location_countries": levels.get("country"),
    }

    # Build a partial doc only with fields whose value would change.
    # Treat None and missing as equivalent.
    partial = {}
    for field, new_value in derived.items():
        if new_value is None:
            continue  # don't clobber to None — leave existing value alone
        if current.get(field) == new_value:
            continue
        partial[field] = new_value

    if not partial:
        return None
    return {"doc": partial}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--team",
        help="Restrict to a single team_title. " "Default: every fb_insights doc.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to ES.",
    )
    args = parser.parse_args()

    es = get_elasticsearch_client()

    scope = f"team={args.team!r}" if args.team else "all teams"
    mode = "DRY-RUN" if args.dry_run else "WRITE"
    print(f"[{mode}] backfilling location_* on {_INDEX_PATTERN} ({scope})")

    n_seen = 0
    n_changed = 0
    n_country_added = 0  # docs that gained a non-empty location_countries
    n_city_added = 0  # docs that gained location_cities (custom_locations path)
    n_errors = 0

    for index, doc_id, targeting, current in _scan_targeting(es, team_title=args.team):
        n_seen += 1
        try:
            body = _planned_update(targeting, current)
        except Exception as e:
            n_errors += 1
            logging.warning(f"  [{index}/{doc_id}] geocoder error: {e}")
            continue

        if body is None:
            continue

        n_changed += 1
        partial = body["doc"]
        if "location_countries" in partial and not current.get("location_countries"):
            n_country_added += 1
        if "location_cities" in partial and not current.get("location_cities"):
            n_city_added += 1

        if args.dry_run:
            continue

        try:
            es.update(index=index, id=doc_id, body=body)
        except Exception as e:
            n_errors += 1
            logging.warning(f"  [{index}/{doc_id}] update error: {e}")

        if n_changed % 500 == 0:
            print(f"  ... {n_changed} updated so far ({n_seen} scanned)")

    print()
    print(f"Scanned: {n_seen} docs")
    print(f"Would update: {n_changed} docs" if args.dry_run else f"Updated: {n_changed} docs")
    print(f"  - gained location_countries: {n_country_added}")
    print(f"  - gained location_cities:    {n_city_added}")
    print(f"Errors: {n_errors}")

    return 0 if n_errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
