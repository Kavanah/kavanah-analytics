"""pin the video view-time fields onto every existing
``*_fb_page_insights`` index BEFORE the first import runs with the new code.

Same class and pattern as ``add_campaign_labels_mapping``:
``create_index_if_not_exists`` applies the updated schema only at index
creation, so existing per-team indexes need an additive ``put_mapping`` or
ES dynamic mapping types the new fields on first write (the
 text+keyword drift class — and worse here, the snapshot
JSON string would dynamic-map as an INDEXED text field).

Fields pinned (see unified_schema_fb_page_insights.json):
  * ``page_video_view_time_daily_min``  — double; the dashboardable daily total
  * ``page_video_view_time_lifetime_ms`` — long; lifetime sum over recent videos
  * ``page_video_view_time_snapshot``   — keyword, index:false, doc_values:false;
    per-video lifetime JSON read back only as tomorrow's delta baseline

Safety: dry-run by default (``--apply`` mutates); additive-only (no snapshot
gate needed); idempotent; an index where a field already exists with a
conflicting mapping is reported as an error (needs a earlier rebuild).

Usage::

    docker compose -f deployment/docker-compose.yml exec -w /app/python \\
        imports_container python3 import.py migration add_fb_view_time_mapping [--apply]
"""

from __future__ import annotations

import logging

import lib.logging
from migration.snapshot_gate import MigrationRefused, finalize_report, resolve_apply_mode
from lib.es_helpers import get_elasticsearch_client

SUFFIX = "_fb_page_insights"
FIELDS = {
    "page_video_view_time_daily_min": {"type": "double"},
    "page_video_view_time_lifetime_ms": {"type": "long"},
    "page_video_view_time_snapshot": {"type": "keyword", "index": False, "doc_values": False},
}


def _concrete_indices(es) -> list[str]:
    idx = es.indices.get(index=f"*{SUFFIX}", ignore_unavailable=True)
    return sorted(name for name in idx.keys() if name.endswith(SUFFIX))


def main(params):
    lib.logging.define("add-fb-view-time-mapping")
    # shared parse — the old raw-truthiness apply check ran WRITE
    # MODE on --apply=false (import.py delivers it as the string "false").
    try:
        dry_run, _ = resolve_apply_mode(params)
    except MigrationRefused as e:
        logging.error("Refusing: %s", e)
        return 1

    es = get_elasticsearch_client()
    if es is None:
        logging.error("No ES client.")
        return 1

    indices = _concrete_indices(es)
    logging.info("%s: %d *_fb_page_insights index(es) in scope", "DRY-RUN" if dry_run else "APPLY", len(indices))

    report = {"pinned": [], "skipped": [], "errors": []}
    for index in indices:
        props = list(es.indices.get_mapping(index=index).values())[0]["mappings"].get("properties", {})
        missing = {}
        conflict = []
        for field, want in FIELDS.items():
            have = props.get(field)
            if have is None:
                missing[field] = dict(want)
            elif have.get("type") != want["type"] or "fields" in have:
                conflict.append((field, have))
        if conflict:
            report["errors"].append(index)
            for field, have in conflict:
                logging.error(
                    "  ERROR %s — %s already mapped as %s (want %s); needs a earlier " "rebuild, not put_mapping",
                    index,
                    field,
                    have,
                    FIELDS[field],
                )
            continue
        if not missing:
            report["skipped"].append(index)
            logging.info("  skip %s — all view-time fields already pinned", index)
            continue

        if dry_run:
            report["pinned"].append(index)
            logging.info("  DRY-RUN would pin %s on %s", sorted(missing), index)
            continue

        try:
            es.indices.put_mapping(index=index, body={"properties": missing})
            report["pinned"].append(index)
            logging.info("  pinned %s on %s", sorted(missing), index)
        except Exception as e:  # noqa: BLE001 — per-index isolation; report and continue
            report["errors"].append(index)
            logging.error("  ERROR %s — put_mapping failed: %s", index, e)

    logging.info(
        "fb view-time mapping  %s: %d pinned, %d skipped, %d errors.",
        "(dry-run)" if dry_run else "",
        len(report["pinned"]),
        len(report["skipped"]),
        len(report["errors"]),
    )
    return finalize_report(report, dry_run=dry_run)


if __name__ == "__main__":
    main({})
