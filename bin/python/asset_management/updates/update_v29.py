"""pin the asset-grain window fields onto EXISTING window indices.

 added the per-video-asset window grain (grain="asset" docs: one per
(ad set, video asset) plus per-asset marginals). New indices get the four new
fields (``video_asset_id``, ``video_asset_name``, ``asset_reach_accounts``,
``asset_reach_cross_account``) from the regenerated window schema; this
migration pins them onto every existing ``*_fb_insights_window`` index so they
carry the catalog's types instead of a dynamic guess. Same mechanism and
idempotency story as update_v27/update_v28.

Field definitions are read from the generated schema files (schema_loader),
not duplicated here — the catalog stays the single source of truth.

Idempotent: PUT _mapping with an identical type is a no-op. A conflicting
dynamic mapping fails per-index and is logged loudly.

MANUAL DEPLOY STEP (CLAUDE.md): update_vN does NOT run from a git pull. Run it
by hand on each env after cutover, BEFORE the first post-cutover window pull
(the script logs to /app/data/logs/system_<date>.log, not stdout):

    docker exec -w /app/python imports_container python3 \
        asset_management/updates/update_v29.py
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
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v29] %(message)s")

# {index wildcard: (schema index type, [new fields])}. Window-only fields, so
# only the *_fb_insights_window family is touched.
NEW_FIELDS = {
    "*fb_insights_window": (
        "fb_insights_window",
        [
            "asset_reach_accounts",
            "asset_reach_cross_account",
            "video_asset_id",
            "video_asset_name",
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
    logging.info("update_v29 done: %d index(es) updated, %d conflicted.", len(updated), len(conflicted))
    return updated, conflicted


if __name__ == "__main__":
    es_client = get_elasticsearch_client()
    if es_client is None:
        logging.error("No Elasticsearch client — aborting.")
        sys.exit(1)
    ok, bad = run(es_client)
    if bad:
        sys.exit(2)
