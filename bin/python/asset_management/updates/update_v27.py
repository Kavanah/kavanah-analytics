"""pin the video-family ``_total`` metric fields onto EXISTING
daily indices.

The catalog pinned the video family's es_fields WITHOUT the ``_total``
suffix (e.g. ``insight_video_p25_watched_actions_video_view``), but the API
returns these action items with ``action_video_type='total'`` (the daily
request breaks down by it), so ``reformat_insights()`` emits
``insight_video_p25_watched_actions_video_view_total``. Verified on staging ES
2026-08-19: the suffix-less forms occur in 0 docs; the ``_total`` forms in
6,500–7,300. corrected the catalog pins to the ``_total`` names; this
migration pins those names onto every existing daily index so they carry the
schema type instead of a dynamic guess (today's dynamic mapping happens to
also be float, so the PUT is a no-op where data already exists — the point is
indices that have no video data yet). Same mechanism and idempotency story as
update_v25, which originally pinned the dead suffix-less names (harmless
leftovers on envs that ran it).

Field definitions are read from the generated schema files (schema_loader),
not duplicated here — the catalog stays the single source of truth.

Idempotent: PUT _mapping with an identical type is a no-op. A conflicting
dynamic mapping fails per-index and is logged loudly.

MANUAL DEPLOY STEP (CLAUDE.md): update_vN does NOT run from a git pull. Run it
by hand on each env after cutover, BEFORE the first post-cutover daily import
(the script logs to /app/data/logs/system_<date>.log, not stdout):

    docker exec -w /app/python imports_container python3 \
        asset_management/updates/update_v27.py
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
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v27] %(message)s")

# {index wildcard: (schema index type, [new fields])}. "*fb_insights" is
# suffix-anchored, so the *_window family never matches.
NEW_FIELDS = {
    "*fb_insights": (
        "fb_insights",
        [
            "insight_video_play_actions_video_view_total",
            "insight_video_30_sec_watched_actions_video_view_total",
            "insight_video_avg_time_watched_actions_video_view_total",
            "insight_video_p25_watched_actions_video_view_total",
            "insight_video_p50_watched_actions_video_view_total",
            "insight_video_p75_watched_actions_video_view_total",
            "insight_video_p95_watched_actions_video_view_total",
            "insight_video_p100_watched_actions_video_view_total",
            "insight_video_thruplay_watched_actions_video_view_total",
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
                    "and reconcile before re-running.",
                    index,
                    exc,
                )
    logging.info("update_v27 done: %d index(es) updated, %d conflicted.", len(updated), len(conflicted))
    return updated, conflicted


if __name__ == "__main__":
    es_client = get_elasticsearch_client()
    if es_client is None:
        logging.error("No Elasticsearch client — aborting.")
        sys.exit(1)
    ok, bad = run(es_client)
    if bad:
        sys.exit(2)
