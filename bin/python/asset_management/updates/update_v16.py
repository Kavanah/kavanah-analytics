"""
update_v16.py — seed de_asset_countries from ISO 3166-1 and purge non-ISO docs.

Reads iso_countries.json (sibling file) and reconciles de_asset_countries
against the ISO 3166-1 alpha-2 list (249 entries):

  1. Seed step — every ISO entry exists in the index. Existing docs are
     matched by country_iso3_code and updated in place (keeping their
     original ES _id, so historical team_country_esids references on
     team docs remain valid). Missing entries are inserted with the
     alpha-2 code as the _id.

  2. Purge step — country docs whose country_iso3_code is NOT in the ISO
     seed (or is empty / missing) are removed. The ReferenceCleanupService
     cascades the deletion to team_country_esids array elements on team
     docs so no team is left pointing at a deleted country.

Idempotent: re-running on an already-reconciled environment is a no-op.

 NAMING DEBT (deferred):
The field is named ``country_iso3_code`` but stores alpha-2 codes —
this naming mismatch predates (existing pipeline stores alpha-2
in this field; importers, dashboard map widgets, and _COUNTRY_CENTROIDS
all consume alpha-2). Renaming the field to ``country_iso2_code``
across the team form, importers, dashboards, and centroid lookups is
non-trivial and out of scope. Tracked as a separate
cleanup follow-up.
"""

import json
import logging
import os
import sys
import unicodedata
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v16] %(message)s")

INDEX_NAME = "de_asset_countries"
SEED_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "iso_countries.json"))

# Legacy-title aliases — alpha-2 → list of titles that should be accepted
# as a match for the ISO title during the seed-step title comparison.
# Use this only when the legacy title clearly refers to the same country
# (different political / romanization name). Adding an alias here is
# safer than loosening the title-match algorithm because it doesn't risk
# false positives on unrelated free-form rows.
TITLE_ALIASES: dict[str, list[str]] = {
    "TR": ["Turkey"],  # ISO title is "Türkiye"
    "PS": ["Palestinian Territories"],  # ISO title is "Palestine"
}


def _normalize_title(text: str) -> str:
    """Lower-case, strip diacritics + punctuation/whitespace for loose title comparison."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in ascii_only if ch.isalnum())


def _title_matches_iso(existing_title: str, iso_title: str, code: str) -> bool:
    """Return True when ``existing_title`` is close enough to ``iso_title`` (or an alias) that
    overwriting it with the ISO title is safe.

    Loose-match handles case + punctuation + diacritics (so "Cote dIvoire" matches "Côte d'Ivoire").
    ``TITLE_ALIASES[code]`` adds explicit allowlist entries for political-renaming cases
    (e.g. "Turkey" → "Türkiye").
    """
    candidates = [iso_title, *TITLE_ALIASES.get(code, [])]
    return any(_normalize_title(existing_title) == _normalize_title(c) for c in candidates)


def load_seed(path: str) -> list[dict[str, str]]:
    """Read the ISO 3166-1 seed JSON; raise if it is missing or malformed."""
    with open(path, encoding="utf-8") as fh:
        entries = json.load(fh)
    if not isinstance(entries, list):
        raise ValueError(f"Seed at {path} must be a JSON array")
    for entry in entries:
        if "country_iso3_code" not in entry or "country_title" not in entry:
            raise ValueError(f"Seed entry missing required keys: {entry!r}")
    return entries


def ensure_index(es, mapping: dict) -> None:
    if es.indices.exists(index=INDEX_NAME):
        logging.info(f"Index {INDEX_NAME} already exists")
        return
    logging.info(f"Creating index: {INDEX_NAME}")
    es.indices.create(index=INDEX_NAME, body={"mappings": mapping})


def fetch_all_country_docs(es) -> list[dict[str, Any]]:
    """Return every doc in de_asset_countries as a list of ES hits ({_id, _source})."""
    if not es.indices.exists(index=INDEX_NAME):
        return []
    resp = es.search(index=INDEX_NAME, body={"query": {"match_all": {}}, "size": 1000})
    return resp["hits"]["hits"]


def index_existing_by_code(docs: list[dict[str, Any]]) -> dict[str, str]:
    """Given the full doc list, return {country_iso3_code: doc_id} for docs that carry a code."""
    out: dict[str, str] = {}
    for hit in docs:
        code = hit["_source"].get("country_iso3_code")
        if code:
            out[code] = hit["_id"]
    return out


def classify_country_docs(docs: list[dict[str, Any]], iso_codes: set[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Split the doc list into (keep_doc_ids, purge_entries).

    A doc is a *keeper* when its country_iso3_code is non-empty and present
    in ``iso_codes``. Every other doc — missing code, empty string, or a
    code not in the ISO seed — is purged.

    Purge entries are returned as ``(_id, country_title)`` tuples so the
    audit log can name the deleted row.

    Pure function — does not touch ES.
    """
    keep: list[str] = []
    purge: list[tuple[str, str]] = []
    for hit in docs:
        doc_id = hit["_id"]
        src = hit["_source"]
        code = src.get("country_iso3_code") or ""
        title = src.get("country_title") or ""
        if code in iso_codes:
            keep.append(doc_id)
        else:
            purge.append((doc_id, title))
    return keep, purge


def purge_non_iso_docs(es, purge_entries: list[tuple[str, str]]) -> int:
    """Cascade-clean references on team docs, then delete each non-ISO country doc.

    Returns the number of docs successfully deleted. Failures are logged
    and skipped — the run continues so a single bad delete doesn't strand
    the rest of the cleanup.
    """
    if not purge_entries:
        return 0

    from asset_management.web.services.reference_cleanup_service import ReferenceCleanupService

    deleted = 0
    for doc_id, title in purge_entries:
        try:
            cleanup_result = ReferenceCleanupService.cleanup_references(INDEX_NAME, doc_id)
            if cleanup_result:
                logging.info(f"Cascaded cleanup for {doc_id!r}: {cleanup_result}")
            es.delete(index=INDEX_NAME, id=doc_id)
            logging.info(f"Purged non-ISO country doc _id={doc_id!r} title={title!r}")
            deleted += 1
        except Exception as e:  # noqa: BLE001 — operator-facing migration, log+continue is correct
            logging.error(f"Failed to purge country doc _id={doc_id!r} title={title!r}: {e}")
    return deleted


def run_update() -> str:
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    logging.info("Starting update_v16: reconcile de_asset_countries against ISO 3166-1")

    mapping = {
        "properties": {
            "country_title": {"type": "keyword"},
            "country_description": {"type": "keyword"},
            "country_iso3_code": {"type": "keyword"},
        }
    }
    ensure_index(es, mapping)

    seed = load_seed(SEED_PATH)
    logging.info(f"Loaded {len(seed)} ISO entries from {SEED_PATH}")

    all_docs = fetch_all_country_docs(es)
    existing_by_code = index_existing_by_code(all_docs)
    logging.info(f"Found {len(all_docs)} country docs; {len(existing_by_code)} carry a code")

    inserted = 0
    updated = 0
    unchanged = 0
    title_skipped = 0

    for entry in seed:
        code = entry["country_iso3_code"]
        title = entry["country_title"]

        if code in existing_by_code:
            doc_id = existing_by_code[code]
            current = es.get(index=INDEX_NAME, id=doc_id)["_source"]
            existing_title = current.get("country_title") or ""

            if existing_title == title and current.get("country_iso3_code") == code:
                unchanged += 1
                continue

            # Title-validation: only overwrite the title if the existing value
            # loosely matches ISO (or an explicit alias). Otherwise keep the
            # existing title and log a warning so the operator can review.
            # Example: code='ME' title='Middle East' must NOT be silently
            # relabeled to 'Montenegro'.
            if _title_matches_iso(existing_title, title, code):
                merged = {**current, "country_iso3_code": code, "country_title": title}
                es.index(index=INDEX_NAME, id=doc_id, body=merged)
                logging.info(f"Updated {code} ({title}) at existing _id={doc_id}")
                updated += 1
            else:
                logging.warning(
                    f"Skipping title update for _id={doc_id!r} (code {code}): "
                    f"existing title {existing_title!r} doesn't match ISO {title!r}. "
                    f"Row will fall through to purge step."
                )
                title_skipped += 1
        else:
            es.index(index=INDEX_NAME, id=code, body={"country_iso3_code": code, "country_title": title})
            logging.info(f"Inserted {code} ({title}) with _id={code}")
            inserted += 1

    es.indices.refresh(index=INDEX_NAME)

    # Purge step. A doc that has an ISO code but a non-matching title was
    # left alone by the seed step; here we delete it via classify-by-code
    # combined with a title check. Build a parallel "keep" allowlist of
    # (code, title) pairs and purge anything whose title doesn't match.
    iso_index = {entry["country_iso3_code"]: entry["country_title"] for entry in seed}
    post_seed_docs = fetch_all_country_docs(es)
    purge_entries: list[tuple[str, str]] = []
    for hit in post_seed_docs:
        doc_id = hit["_id"]
        src = hit["_source"]
        code = src.get("country_iso3_code") or ""
        existing_title = src.get("country_title") or ""
        iso_title = iso_index.get(code)
        if iso_title is None:
            # Code not in ISO seed at all — definitely purge.
            purge_entries.append((doc_id, existing_title))
        elif existing_title != iso_title and not _title_matches_iso(existing_title, iso_title, code):
            # Code is valid but title is genuinely different (e.g. "Middle East").
            purge_entries.append((doc_id, existing_title))

    purged = purge_non_iso_docs(es, purge_entries)
    if purged:
        es.indices.refresh(index=INDEX_NAME)

    summary = (
        f"update_v16 complete: {inserted} inserted, {updated} updated, "
        f"{unchanged} unchanged, {title_skipped} title-skipped, {purged} purged"
    )
    logging.info(summary)
    print(summary)
    return "success"


if __name__ == "__main__":
    run_update()
