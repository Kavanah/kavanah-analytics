"""
One-time migration: backfill contact_country_iso2 on existing echo_conversations.

Derives ISO2 country codes from the existing client_country field using
lib.country_codes.resolve_country_to_iso2(). Only updates documents that
do not already have a contact_country_iso2 value.

Run inside the imports container:
  sudo docker exec imports_container python3 /app/python/import_scripts/migration/backfill_contact_country_iso2.py
"""

import sys
import logging

sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client
from lib.country_codes import resolve_country_to_iso2

INDEX_PATTERN = "*echo_conversations,-unknown_echo_conversations"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def get_unique_country_values(es, field: str) -> list[tuple[str, int]]:
    """Return all distinct values for *field* across echo_conversations."""
    resp = es.search(
        index=INDEX_PATTERN,
        body={
            "size": 0,
            "aggs": {"countries": {"terms": {"field": field, "size": 5000}}},
        },
    )
    rd = resp.body if hasattr(resp, "body") else dict(resp)
    buckets = rd.get("aggregations", {}).get("countries", {}).get("buckets", [])
    return [(b["key"], b["doc_count"]) for b in buckets]


def backfill_country(es, source_field: str, country_name: str, iso2_code: str) -> int:
    """Set contact_country_iso2 for all docs matching *source_field* that lack the field."""
    body = {
        "query": {
            "bool": {
                "must": [{"term": {source_field: country_name}}],
                "must_not": [{"exists": {"field": "contact_country_iso2"}}],
            }
        },
        "script": {
            "source": "ctx._source.contact_country_iso2 = params.v",
            "lang": "painless",
            "params": {"v": iso2_code},
        },
    }
    try:
        result = es.update_by_query(
            index=INDEX_PATTERN,
            body=body,
            conflicts="proceed",
            wait_for_completion=True,
        )
        rd = result.body if hasattr(result, "body") else dict(result)
        updated = rd.get("updated", 0)
        if updated > 0:
            logging.info("  %r -> %s: updated %d docs", country_name, iso2_code, updated)
        return updated
    except Exception as e:
        err = str(e)
        if "index_not_found" in err or "no such index" in err.lower():
            return 0
        logging.error("  %r -> %s: ERROR: %s", country_name, iso2_code, err[:200])
        return 0


def main():
    es = get_elasticsearch_client()

    # Existing docs store contact country in "country"; newer imports use "client_country".
    # Process both fields to cover all documents.
    source_fields = ["country", "client_country"]

    total_updated = 0
    unresolved = []

    for field in source_fields:
        logging.info("Discovering unique %s values...", field)
        countries = get_unique_country_values(es, field)
        logging.info("Found %d distinct %s values.\n", len(countries), field)

        for country_name, doc_count in countries:
            iso2 = resolve_country_to_iso2(country_name)
            if not iso2:
                unresolved.append((country_name, doc_count, field))
                continue
            logging.info("Processing %s: %r (%d docs) -> %s", field, country_name, doc_count, iso2)
            total_updated += backfill_country(es, field, country_name, iso2)

    if unresolved:
        logging.warning(
            "\nCould not resolve %d country values (add to _COUNTRY_NAME_TO_ISO2):",
            len(unresolved),
        )
        for name, count, field in unresolved:
            logging.warning("  %r (%d docs, field=%s)", name, count, field)

    logging.info("\nDone. Total documents updated: %d", total_updated)


if __name__ == "__main__":
    main()
