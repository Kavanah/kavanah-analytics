"""pin the five response-driven FB scalars onto EXISTING daily indices.

The field picker (PHASE-107) makes attribution_setting, canvas_avg_view_percent,
canvas_avg_view_time, cost_per_estimated_ad_recallers and full_view_impressions
first-class catalog fields. They have been requested from Meta since before
, so live daily indices already hold them via ES dynamic mapping; the
catalog types were chosen to MATCH what dynamic mapping produces (text+keyword
for the string, float/long for the numerics), making this PUT a no-op where the
field exists and a correct pin where it doesn't (new or so-far-empty indices).

Field definitions are read from the generated schema files (schema_loader),
not duplicated here — the catalog stays the single source of truth.

Idempotent: PUT _mapping with an identical type is a no-op. An index that
dynamic-mapped one of these to a CONFLICTING type fails per-index and is
logged loudly — investigate that index before re-running.

MANUAL DEPLOY STEP (CLAUDE.md): update_vN does NOT run from a git pull. Run it
by hand on each env after cutover, BEFORE the first post-cutover daily import
(the script logs to /app/data/logs/system_<date>.log, not stdout):

    docker exec -w /app/python imports_container python3 \
        asset_management/updates/update_v24.py
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
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v24] %(message)s")

# {index wildcard: (schema index type, [new fields])}. Wildcards match both the
# ESID-keyed (``{esid}__<suffix>``) and legacy (``{title}_<suffix>``) naming;
# "*fb_insights" is suffix-anchored, so the *_window family never matches.
NEW_FIELDS = {
    "*fb_insights": (
        "fb_insights",
        [
            "insight_attribution_setting",
            "insight_canvas_avg_view_percent",
            "insight_canvas_avg_view_time",
            "insight_cost_per_estimated_ad_recallers",
            "insight_full_view_impressions",
        ],
    ),
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
                    "MAPPING CONFLICT on %s: %s — the field was dynamic-mapped to a "
                    "type that differs from the catalog's. Inspect the live mapping "
                    "and reconcile the catalog (or reindex) before re-running.",
                    index,
                    exc,
                )
    logging.info("update_v24 done: %d index(es) updated, %d conflicted.", len(updated), len(conflicted))
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
