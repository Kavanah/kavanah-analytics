"""give ``*_fb_insights`` and ``*_ig_insights`` docs the ``date``
field the rest of the sources already carry.

FB/IG insights docs held only ``@timestamp`` + ``kibana_date``, so a
date-histogram or range on the cross-source ``date`` field returned EMPTY for
those indices even with data present — during this read as "the IG
importer died" and burned real investigation time. YouTube / Google Ads / GA
docs all carry ``date``.

Two passes per index (both idempotent, dry-run by default):

1. **Mapping pin** — additive ``put_mapping`` of ``date`` as
   ``{type: date, format: yyyy-MM-dd}`` (pattern; without it
   the sender's new stamp would dynamic-map on first write).
2. **Historical backfill** — ``update_by_query`` setting
   ``date = kibana_date`` on docs missing it (``conflicts=proceed`` so a
   concurrent import upsert never aborts the pass; re-run to sweep any
   conflicted leftovers). This is what makes EXISTING data answer cross-source
   date queries — forward-only stamping would leave history invisible.

Window indices (``*_fb_insights_window`` / ``*_ig_insights_window``) are
deliberately OUT of scope: their docs are window aggregates keyed to a range,
and stamping a scalar ``date`` would misrepresent them. The suffix match
below excludes them structurally.

Usage::

    docker compose -f deployment/docker-compose.yml exec -w /app/python \\
        imports_container python3 import.py migration add_date_field [--apply]
"""

from __future__ import annotations

import logging

import lib.logging
from migration.snapshot_gate import MigrationRefused, finalize_report, resolve_apply_mode
from lib.es_helpers import get_elasticsearch_client

SUFFIXES = ("_fb_insights", "_ig_insights")
FIELD_MAPPING = {"date": {"type": "date", "format": "yyyy-MM-dd"}}

_MISSING_DATE_QUERY = {
    "bool": {
        "filter": [{"exists": {"field": "kibana_date"}}],
        "must_not": [{"exists": {"field": "date"}}],
    }
}


def _concrete_indices(es) -> list[str]:
    out = []
    for suffix in SUFFIXES:
        idx = es.indices.get(index=f"*{suffix}", ignore_unavailable=True)
        out.extend(name for name in idx.keys() if name.endswith(suffix))
    return sorted(set(out))


def main(params):
    lib.logging.define("add-date-field")
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
    logging.info("%s: %d index(es) in scope", "DRY-RUN" if dry_run else "APPLY", len(indices))

    report = {"pinned": [], "backfilled": 0, "skipped": [], "errors": []}
    for index in indices:
        props = list(es.indices.get_mapping(index=index).values())[0]["mappings"].get("properties", {})
        have = props.get("date")
        if have is not None and (have.get("type") != "date" or "fields" in have):
            report["errors"].append(index)
            logging.error(
                "  ERROR %s — date already mapped as %s; needs a earlier rebuild, not put_mapping",
                index,
                have,
            )
            continue

        missing = int(es.count(index=index, body={"query": _MISSING_DATE_QUERY}).get("count", 0))

        if dry_run:
            action = []
            if have is None:
                action.append("pin mapping")
            if missing:
                action.append(f"backfill {missing} doc(s)")
            if action:
                report["pinned"].append(index)
                logging.info("  DRY-RUN %s: would %s", index, " + ".join(action))
            else:
                report["skipped"].append(index)
                logging.info("  skip %s — mapped and fully backfilled", index)
            continue

        try:
            if have is None:
                es.indices.put_mapping(index=index, body={"properties": dict(FIELD_MAPPING)})
                report["pinned"].append(index)
                logging.info("  pinned date on %s", index)
            if missing:
                resp = es.options(request_timeout=1800).update_by_query(
                    index=index,
                    body={
                        "query": _MISSING_DATE_QUERY,
                        "script": {
                            "source": "ctx._source.date = ctx._source.kibana_date",
                            "lang": "painless",
                        },
                    },
                    conflicts="proceed",
                    refresh=True,
                    wait_for_completion=True,
                )
                updated = int(resp.get("updated", 0))
                conflicts = int(resp.get("version_conflicts", 0))
                report["backfilled"] += updated
                logging.info(
                    "  backfilled %s: %d updated, %d conflict(s)%s",
                    index,
                    updated,
                    conflicts,
                    " — re-run to sweep the conflicted docs" if conflicts else "",
                )
            if have is not None and not missing:
                report["skipped"].append(index)
                logging.info("  skip %s — mapped and fully backfilled", index)
        except Exception as e:  # noqa: BLE001 — per-index isolation
            report["errors"].append(index)
            logging.error("  ERROR %s: %s", index, e)

    logging.info(
        "date-field  %s: %d index(es) pinned, %d doc(s) backfilled, %d skipped, %d errors.",
        "(dry-run)" if dry_run else "",
        len(report["pinned"]),
        report["backfilled"],
        len(report["skipped"]),
        len(report["errors"]),
    )
    return finalize_report(report, dry_run=dry_run)


if __name__ == "__main__":
    main({})
