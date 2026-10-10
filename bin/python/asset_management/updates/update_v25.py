"""pin the per-source-field flattened FB list metrics onto EXISTING
daily indices.

reformat_insights used to funnel every list-valued response field that isn't
``actions``/``cost_per_action_type`` into ``insight_actions_<action_type>`` —
colliding with the real actions family (cost_per_thruplay's COST overwrote the
insight_actions_video_view COUNT; the video percentile counts overwrote each
other). keys those fields by their own source name
(``insight_<api_field>_<action_type>``); this migration pins the 14 new
deterministic keys onto every existing daily index so their first post-deploy
write gets the schema type, not a dynamic guess.

Field definitions are read from the generated schema files (schema_loader),
not duplicated here — the catalog stays the single source of truth.

Idempotent: PUT _mapping with an identical type is a no-op. A conflicting
dynamic mapping fails per-index and is logged loudly.

MANUAL DEPLOY STEP (CLAUDE.md): update_vN does NOT run from a git pull. Run it
by hand on each env after cutover, BEFORE the first post-cutover daily import
(the script logs to /app/data/logs/system_<date>.log, not stdout):

    docker exec -w /app/python imports_container python3 \
        asset_management/updates/update_v25.py
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
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v25] %(message)s")

# {index wildcard: (schema index type, [new fields])}. "*fb_insights" is
# suffix-anchored, so the *_window family never matches.
#
# the eight suffix-less insight_video_* names this originally
# listed are GONE: they never occurred on any doc (the video family arrives
# with action_video_type='total', so reformat_insights emits the _total
# forms), and the catalog/schema now pin the _total names — keeping the old
# names here would KeyError against the regenerated schema on any env that
# hasn't run v25 yet. Envs that already ran v25 just carry eight
# dead-but-harmless mappings. update_v27 pins the corrected _total forms.
NEW_FIELDS = {
    "*fb_insights": (
        "fb_insights",
        [
            "insight_outbound_clicks_outbound_click",
            "insight_outbound_clicks_ctr_outbound_click",
            "insight_cost_per_outbound_click_outbound_click",
            "insight_cost_per_unique_outbound_click_outbound_click",
            "insight_website_ctr_link_click",
            "insight_cost_per_thruplay_video_view",
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
    logging.info("update_v25 done: %d index(es) updated, %d conflicted.", len(updated), len(conflicted))
    return updated, conflicted


if __name__ == "__main__":
    es_client = get_elasticsearch_client()
    if es_client is None:
        logging.error("No Elasticsearch client — aborting.")
        sys.exit(1)
    ok, bad = run(es_client)
    if bad:
        sys.exit(2)
