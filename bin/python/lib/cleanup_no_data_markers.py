"""
Cleanup utility for stale `no_data` marker docs.

Markers are written by lib/no_data_markers.py when an import for a date
finds zero results for a team. If the import is later re-run and DOES
find data for the same date, the marker becomes a zombie — it should be
deleted so doc counts on the Data Import Status page reflect reality.

Two cleanup operations are supported:

    1. delete_zombie_markers:
       Delete markers where a non-marker doc exists for the same
       (team_esid, kibana_date) pair within the given index pattern.

    2. dedupe_markers:
       For pairs with multiple markers (legacy data from before the
       deterministic-ID fix), keep one marker with the canonical ID
       and delete the rest.

Both operations require `kibana_date` values to be ISO `YYYY-MM-DD`.
Documents whose `kibana_date` doesn't match that pattern are counted and
skipped — for the Echo platform's non-ISO kibana_date issue.
"""

import logging
import re

from lib.es_helpers import get_elasticsearch_client
from lib.no_data_markers import marker_doc_id

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_iso_date(value) -> bool:
    return isinstance(value, str) and bool(ISO_DATE_RE.match(value))


def _scan_markers(es, index_pattern: str) -> list:
    """Return every marker doc in the given index pattern.

    Uses the scroll API to page through results, drains the scroll into a
    list, and clears the scroll context BEFORE returning. Callers may then
    safely modify the underlying indices without invalidating the scroll
    mid-iteration — a real bug observed on staging where rewriting marker
    docs while the scroll was still open raised
    `NotFoundError: No search context found for id ...`.

    Marker docs are tiny (a handful of fields each) and total counts are
    in the low thousands, so loading the full list into memory is fine.
    """
    markers = []
    resp = es.search(
        index=index_pattern,
        body={
            "query": {"term": {"no_data": True}},
            "_source": ["team_esid", "kibana_date", "team_title"],
            "size": 1000,
            "sort": ["_doc"],
        },
        scroll="2m",
        ignore_unavailable=True,
        allow_no_indices=True,
    )
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                break
            markers.extend(hits)
            resp = es.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = resp.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass
    return markers


def _real_doc_exists(es, index_pattern: str, team_esid: str, kibana_date: str) -> bool:
    """Return True if at least one non-marker doc exists for (team, date)."""
    resp = es.count(
        index=index_pattern,
        body={
            "query": {
                "bool": {
                    "must": [
                        # match BOTH mapping shapes. Correctly
                        # mapped indices have bare-keyword team_esid (no
                        # .keyword sub-field), so a .keyword-only term
                        # silently matched nothing there and marker cleanup
                        # no-opped on every healthy index. ES skips a clause
                        # targeting a non-existent field, so the should-pair
                        # works on either shape (es_query_builder
                        # _field_variants is the same defense on the read
                        # path).
                        {
                            "bool": {
                                "should": [
                                    {"term": {"team_esid": team_esid}},
                                    {"term": {"team_esid.keyword": team_esid}},
                                ],
                                "minimum_should_match": 1,
                            }
                        },
                        {"term": {"kibana_date": kibana_date}},
                    ],
                    "must_not": [{"term": {"no_data": True}}],
                }
            }
        },
        ignore_unavailable=True,
        allow_no_indices=True,
    )
    return resp.get("count", 0) > 0


def delete_zombie_markers(index_pattern: str, dry_run: bool = False) -> dict:
    """
    Delete markers where a real doc exists for the same (team_esid, kibana_date).

    Args:
        index_pattern: ES index pattern to scan (e.g. "*_fb_insights").
        dry_run: If True, count what would be deleted without deleting.

    Returns:
        dict with counts: scanned, zombies, deleted, skipped_non_iso, errors.
    """
    es = get_elasticsearch_client()
    if es is None:
        logging.error(f"Cannot run zombie-marker cleanup on {index_pattern}: ES client unavailable")
        return {"scanned": 0, "zombies": 0, "deleted": 0, "skipped_non_iso": 0, "errors": 1}

    counts = {"scanned": 0, "zombies": 0, "deleted": 0, "skipped_non_iso": 0, "errors": 0}

    for marker_hit in _scan_markers(es, index_pattern):
        counts["scanned"] += 1
        src = marker_hit.get("_source", {})
        team_esid = src.get("team_esid")
        kibana_date = src.get("kibana_date")
        if not team_esid or not kibana_date:
            continue
        if not _is_iso_date(kibana_date):
            counts["skipped_non_iso"] += 1
            continue

        if not _real_doc_exists(es, index_pattern, team_esid, kibana_date):
            continue

        counts["zombies"] += 1
        if dry_run:
            continue

        try:
            # Skip per-call wait_for refresh — this is a batch migration, so
            # we refresh once at the end. Keeping per-call refresh adds ~1s
            # per delete on a single-node cluster, turning a few-second job
            # into a half-hour stall for a few thousand markers.
            es.delete(
                index=marker_hit["_index"],
                id=marker_hit["_id"],
            )
            counts["deleted"] += 1
        except Exception as e:
            counts["errors"] += 1
            logging.warning(f"Failed to delete zombie marker {marker_hit['_index']}/{marker_hit['_id']}: {e}")

    if not dry_run and counts["deleted"]:
        # Single refresh after the batch so subsequent reads see the deletes.
        try:
            es.indices.refresh(index=index_pattern, ignore_unavailable=True, allow_no_indices=True)
        except Exception as e:
            logging.warning(f"Refresh after zombie deletion failed: {e}")

    if counts["skipped_non_iso"]:
        logging.warning(
            f"{index_pattern}: skipped {counts['skipped_non_iso']} marker(s) " f"with non-ISO kibana_date"
        )
    logging.info(
        f"{index_pattern}: scanned={counts['scanned']} zombies={counts['zombies']} "
        f"deleted={counts['deleted']} skipped_non_iso={counts['skipped_non_iso']} "
        f"errors={counts['errors']}{' (dry-run)' if dry_run else ''}"
    )
    return counts


def dedupe_markers(index_pattern: str, dry_run: bool = False) -> dict:
    """
    Collapse duplicate markers for the same (team_esid, kibana_date) down to
    one canonical-ID doc. Legacy markers had auto-generated IDs, so multiple
    can exist for the same logical (team, date) pair —.

    The canonical ID is `{team_esid}_{kibana_date}_no_data` (see
    lib.no_data_markers.marker_doc_id).

    Strategy:
        - For each marker: if its _id is already canonical, keep it.
        - Otherwise, write the canonical-ID marker (idempotent upsert) and
          delete the legacy doc.

    Returns:
        dict with counts: scanned, canonical_kept, replaced, skipped_non_iso,
        errors.
    """
    es = get_elasticsearch_client()
    if es is None:
        logging.error(f"Cannot run marker dedupe on {index_pattern}: ES client unavailable")
        return {
            "scanned": 0,
            "canonical_kept": 0,
            "replaced": 0,
            "skipped_non_iso": 0,
            "errors": 1,
        }

    counts = {
        "scanned": 0,
        "canonical_kept": 0,
        "replaced": 0,
        "skipped_non_iso": 0,
        "errors": 0,
    }

    for marker_hit in _scan_markers(es, index_pattern):
        counts["scanned"] += 1
        src = marker_hit.get("_source", {})
        team_esid = src.get("team_esid")
        kibana_date = src.get("kibana_date")
        team_title = src.get("team_title", "unknown")
        if not team_esid or not kibana_date:
            continue
        if not _is_iso_date(kibana_date):
            counts["skipped_non_iso"] += 1
            continue

        canonical_id = marker_doc_id(team_esid, kibana_date)
        if marker_hit["_id"] == canonical_id:
            counts["canonical_kept"] += 1
            continue

        if dry_run:
            counts["replaced"] += 1
            continue

        try:
            # Skip per-call wait_for refresh — single refresh at the end.
            # See delete_zombie_markers for the rationale.
            es.index(
                index=marker_hit["_index"],
                id=canonical_id,
                document={
                    "kibana_date": kibana_date,
                    "team_title": team_title,
                    "team_esid": team_esid,
                    "no_data": True,
                    "@timestamp": src.get("@timestamp"),
                },
            )
            es.delete(
                index=marker_hit["_index"],
                id=marker_hit["_id"],
            )
            counts["replaced"] += 1
        except Exception as e:
            counts["errors"] += 1
            logging.warning(
                f"Failed to replace legacy marker {marker_hit['_index']}/{marker_hit['_id']} "
                f"with canonical id {canonical_id}: {e}"
            )

    if not dry_run and counts["replaced"]:
        # Single refresh after the batch so subsequent reads see canonical-id
        # markers and the legacy ids gone.
        try:
            es.indices.refresh(index=index_pattern, ignore_unavailable=True, allow_no_indices=True)
        except Exception as e:
            logging.warning(f"Refresh after marker dedupe failed: {e}")

    if counts["skipped_non_iso"]:
        logging.warning(
            f"{index_pattern}: skipped {counts['skipped_non_iso']} marker(s) " f"with non-ISO kibana_date"
        )
    logging.info(
        f"{index_pattern}: scanned={counts['scanned']} canonical_kept={counts['canonical_kept']} "
        f"replaced={counts['replaced']} skipped_non_iso={counts['skipped_non_iso']} "
        f"errors={counts['errors']}{' (dry-run)' if dry_run else ''}"
    )
    return counts


def delete_stale_markers_for_date(index_pattern: str, kibana_date: str, team_esids_with_data: set) -> int:
    """No-op. Companion to ``lib.no_data_markers.write_no_data_markers``.
    The marker subsystem is library-level neutralized; the stale-marker
    cleanup hook is now a no-op too. Existing markers in ES are left in
    place — they're inert. Signature preserved so the ~36 importer call
    sites stay untouched. The other two helpers in this file
    (``delete_zombie_markers``, ``dedupe_markers``) are operator CLI tools
    and still function — they don't write new markers, only clean up
    existing ones, so they remain useful if an operator wants to purge.
    """
    logging.debug(f"delete_stale_markers_for_date[{kibana_date} {index_pattern}]: no-op")
    return 0
