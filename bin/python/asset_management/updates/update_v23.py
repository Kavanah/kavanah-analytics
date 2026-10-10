"""add the new FB field mappings to EXISTING data indices.

Index mappings only apply at index CREATION (ensure_data_index), so an index
created before these tickets dynamic-maps the new fields on first write —
``age``/``gender`` land as TEXT (fielddata-disabled: aggregations and the
demographics grain routing break) instead of the schema's keyword. Staging's
one team's window index hit exactly this on 2026-07-15 and needed a delete+resend.

This migration PUTs the new fields into every existing window / page index
BEFORE their first demographics/audience import, so the schema types win:

  ``*fb_insights_window``  — age, gender (keyword); demo_reach_accounts,
      demo_reach_cross_account; estimate_mau_lower_bound/upper_bound.
  ``*fb_page_insights``    — page_fan_count.

Field definitions are read from the generated schema files (schema_loader),
not duplicated here — the catalog stays the single source of truth.

Idempotent: PUT _mapping with an identical type is a no-op. An index that
already dynamic-mapped a field to a CONFLICTING type (text) fails per-index
and is logged loudly — that index needs the same treatment (delete the index,
re-run send_window_json_to_elastic; the JSON files persist, deterministic
_ids restore every doc).

MANUAL DEPLOY STEP (CLAUDE.md): update_vN does NOT run from a git pull. Run it
by hand on each env after cutover, BEFORE the first post-cutover window/page
import (the script logs to /app/data/logs/system_<date>.log, not stdout):

    docker exec -w /app/python imports_container python3 \
        asset_management/updates/update_v23.py
"""

import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client
from migration.schema_loader import get_schema_for_index_type

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v23] %(message)s")

# {index wildcard: (schema index type, [new fields])}. Wildcards match both the
# ESID-keyed (``{esid}__<suffix>``) and legacy (``{title}_<suffix>``) naming.
NEW_FIELDS = {
    "*fb_insights_window": (
        "fb_insights_window",
        [
            "age",
            "gender",
            "demo_reach_accounts",
            "demo_reach_cross_account",
            "estimate_mau_lower_bound",
            "estimate_mau_upper_bound",
        ],
    ),
    "*fb_page_insights": ("fb_page_insights", ["page_fan_count"]),
}


def mapping_updates():
    """``{index wildcard: {field: mapping}}`` pulled from the generated schemas,
    so this migration can never drift from what ensure_data_index creates."""
    updates = {}
    for pattern, (index_type, fields) in NEW_FIELDS.items():
        props = get_schema_for_index_type(index_type)["properties"]
        updates[pattern] = {f: props[f] for f in fields}
    return updates


def run(es):
    """Apply the new-field mappings per concrete index. Returns
    ``(updated, conflicted)`` index-name lists."""
    updated, conflicted = [], []
    for pattern, props in mapping_updates().items():
        indices = sorted(es.indices.get(index=pattern, ignore_unavailable=True) or {})
        for index in indices:
            if index.startswith("."):
                continue
            try:
                es.indices.put_mapping(index=index, body={"properties": props})
                updated.append(index)
                logging.info("mapped %s onto %s", sorted(props), index)
            except Exception as exc:  # noqa: BLE001 — per-index boundary; report, don't abort the sweep
                conflicted.append(index)
                logging.error(
                    "MAPPING CONFLICT on %s: %s — a new field was already dynamic-mapped "
                    "to the wrong type. Fix: DELETE the index, then re-run the matching "
                    "sender (send_window_json_to_elastic / send_page_json_to_elastic) — "
                    "the JSON files persist and deterministic _ids restore every doc.",
                    index,
                    exc,
                )
    logging.info("update_v23 done: %d index(es) updated, %d conflicted.", len(updated), len(conflicted))
    return updated, conflicted


if __name__ == "__main__":
    es_client = get_elasticsearch_client()
    if es_client is None:
        logging.error("No Elasticsearch client — aborting.")
        sys.exit(1)
    ok, bad = run(es_client)
    # Exit code is the operator signal (the run log has the detail; this script
    # logs to the system log, not stdout — update_vN convention).
    if bad:
        sys.exit(2)
