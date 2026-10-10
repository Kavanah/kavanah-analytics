"""pin ``campaign_labels: keyword`` onto every existing ``*_google_ads``
index BEFORE the first import runs with the label-pulling code.

Background
==========
 adds ``campaign_labels`` (Google Ads campaign label display names) to
every campaign-grain doc and declares it as bare ``keyword`` in
``unified_schema_google_ads.json``. ``create_google_ads_index`` applies that
schema only when an index is CREATED — every existing per-team
``*_google_ads`` index keeps its old mapping. If the importer writes the new
field first, ES dynamic mapping types it as ``text`` + ``.keyword`` twin, and
the fleet drifts into the mixed-mapping class that broke aggregations in
.

Unlike this needs NO reindex: the field does not exist yet, and adding
a brand-new field to a live index via ``put_mapping`` is additive and safe. An
index where the field ALREADY exists with a different type (dynamic mapping got
there first — i.e. this migration ran too late) is reported as an error; that
index needs the earlier rebuild treatment instead.

Safety
======
* **Dry-run by default.** ``--apply`` is required to mutate.
* **Additive only.** ``put_mapping`` on a new field never touches existing
  fields or data; no snapshot gate is required (nothing to restore).
* **Idempotent.** An index already carrying bare-keyword ``campaign_labels``
  is skipped.

Usage
=====
::

    # Dry-run (default) — report only.
    docker compose -f deployment/docker-compose.yml exec -w /app/python \\
        imports_container python3 import.py migration add_campaign_labels_mapping

    # Apply.
    ... migration add_campaign_labels_mapping --apply
"""

from __future__ import annotations

import logging

import lib.logging
from migration.snapshot_gate import MigrationRefused, finalize_report, resolve_apply_mode
from lib.es_helpers import get_elasticsearch_client

SUFFIX = "_google_ads"
FIELD = "campaign_labels"
FIELD_MAPPING = {"type": "keyword"}


def _concrete_indices(es) -> list[str]:
    """Concrete ``*_google_ads`` indices (mirrors remap_google_ads_geo)."""
    idx = es.indices.get(index=f"*{SUFFIX}", ignore_unavailable=True)
    return sorted(name for name in idx.keys() if name.endswith(SUFFIX))


def _field_spec(es, index: str) -> dict | None:
    m = es.indices.get_mapping(index=index)
    props = list(m.values())[0]["mappings"].get("properties", {})
    return props.get(FIELD)


def main(params):
    lib.logging.define("add-campaign-labels-mapping")
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
    logging.info("%s: %d *_google_ads index(es) in scope", "DRY-RUN" if dry_run else "APPLY", len(indices))

    report = {"pinned": [], "skipped": [], "errors": []}
    for index in indices:
        spec = _field_spec(es, index)
        if spec is not None:
            if spec.get("type") == "keyword" and "fields" not in spec:
                report["skipped"].append(index)
                logging.info("  skip %s — %s already bare keyword", index, FIELD)
            else:
                # Dynamic mapping beat us to it; a type can't be changed in place.
                report["errors"].append(index)
                logging.error(
                    "  ERROR %s — %s already mapped as %s; needs a earlier rebuild, not put_mapping",
                    index,
                    FIELD,
                    spec,
                )
            continue

        if dry_run:
            report["pinned"].append(index)
            logging.info("  DRY-RUN would pin %s: keyword on %s", FIELD, index)
            continue

        try:
            es.indices.put_mapping(index=index, body={"properties": {FIELD: dict(FIELD_MAPPING)}})
            report["pinned"].append(index)
            logging.info("  pinned %s: keyword on %s", FIELD, index)
        except Exception as e:  # noqa: BLE001 — per-index isolation; report and continue
            report["errors"].append(index)
            logging.error("  ERROR %s — put_mapping failed: %s", index, e)

    logging.info(
        "campaign_labels mapping  %s: %d pinned, %d skipped, %d errors.",
        "(dry-run)" if dry_run else "",
        len(report["pinned"]),
        len(report["skipped"]),
        len(report["errors"]),
    )
    return finalize_report(report, dry_run=dry_run)


if __name__ == "__main__":
    main({})
