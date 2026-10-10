"""
Promote Disciple Tools data from staging index to live alias.

The DT exporter (external process) writes to 'disciple-tools-staging'.
This script claims that data into a timestamped index, enriches it with
organization_title (looked up from de_asset_teams), and atomically swaps
the 'disciple-tools' alias to point at the new index.

Safe to run on a schedule — no-ops when staging is empty or missing.

Execution:
    python3 /app/python/import.py disciple_tools promote_staging
    python3 /app/python/import.py disciple_tools promote_staging --dry-run
    python3 /app/python/import.py disciple_tools promote_staging --min-ratio=0.3
"""

import logging
from datetime import datetime

import lib.logging
from lib.es_helpers import get_elasticsearch_client, is_alias, get_alias_backing_index
from import_scripts.migration.backfill_organization_title import (
    load_team_organizations,
    process_index,
)
from import_scripts.disciple_tools.prewarm_geocache import prewarm as prewarm_geocache

STAGING_INDEX = "disciple-tools-staging"
ALIAS_NAME = "disciple-tools"
INDEX_PREFIX = "disciple-tools-"
DEFAULT_MIN_RATIO = 0.5


def _ensure_index_template(es):
    """Ensure an index template exists for disciple-tools-* indices.

    This prevents data-stream-only templates from interfering with
    regular index creation during the reindex step.
    """
    template_name = "template_disciple_tools_timestamped"
    try:
        es.indices.put_index_template(
            name=template_name,
            body={
                "index_patterns": ["disciple-tools-*"],
                "priority": 200,
                "template": {
                    "settings": {"number_of_replicas": 0},
                    "mappings": {
                        "properties": {
                            "date_created_ms": {"type": "date", "format": "epoch_millis"},
                            "date_modified_ms": {"type": "date", "format": "epoch_millis"},
                            "meta": {
                                "properties": {
                                    "first_contact_date_ms": {"type": "date", "format": "epoch_millis"},
                                    "baptism_date_ms": {"type": "date", "format": "epoch_millis"},
                                    "start_date_ms": {"type": "date", "format": "epoch_millis"},
                                    "church_start_date_ms": {"type": "date", "format": "epoch_millis"},
                                }
                            },
                        }
                    },
                },
            },
        )
        logging.info(f"Index template '{template_name}' ready")
        return True
    except Exception as e:
        logging.error(f"Failed to create index template: {e}")
        return False


def main(params):
    """Promote disciple-tools-staging to the live alias with org enrichment.

    Args:
        params: Dictionary of parameters from import.py
                - dry-run: If present, only preview what would happen
                - min-ratio: Minimum staging/live doc count ratio (default 0.5)

    Returns:
        str: 'success' or 'abort'
    """
    lib.logging.define("dt-promote-staging")

    dry_run = params.get("dry-run", "") == "true" or params.get("dry-run") is True
    min_ratio = float(params.get("min-ratio", DEFAULT_MIN_RATIO))

    logging.info("=" * 70)
    logging.info("Disciple Tools: Promote Staging to Live")
    if dry_run:
        logging.info("DRY RUN — no changes will be made")
    logging.info("=" * 70)

    # Step 1: Connect to ES
    es = get_elasticsearch_client()
    if not es:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    # Step 2: Load team → organization mappings
    logging.info("Loading team organization mappings...")
    team_org_map = load_team_organizations(es)
    if team_org_map is None:
        logging.error("Failed to load team organizations")
        return "abort"
    logging.info(f"Loaded {len(team_org_map)} team mappings")

    # Step 3: Check staging exists and has documents
    if not es.indices.exists(index=STAGING_INDEX):
        logging.info(f"No staging index '{STAGING_INDEX}' found — nothing to promote")
        return "success"

    staging_count = es.count(index=STAGING_INDEX)["count"]
    if staging_count == 0:
        logging.info(f"Staging index '{STAGING_INDEX}' is empty — nothing to promote")
        return "success"

    logging.info(f"Staging index has {staging_count} documents")

    # Step 4: Safety check — compare staging to live doc count
    alias_exists = is_alias(es, ALIAS_NAME)
    if alias_exists:
        live_count = es.count(index=ALIAS_NAME)["count"]
        if live_count > 0:
            ratio = staging_count / live_count
            logging.info(f"Staging/live ratio: {staging_count}/{live_count} = {ratio:.2f} " f"(threshold: {min_ratio})")
            if ratio < min_ratio:
                logging.warning(
                    f"Staging doc count ({staging_count}) is less than "
                    f"{min_ratio:.0%} of live ({live_count}) — skipping this cycle. "
                    f"This may indicate a partial DT export."
                )
                return "success"
        else:
            logging.info("Live index is empty — skipping ratio check")
    else:
        logging.info("No existing alias — first run, skipping ratio check")

    if dry_run:
        logging.info(f"[DRY RUN] Would promote {staging_count} documents from staging")
        logging.info("[DRY RUN] No changes made")
        return "success"

    # Step 5: Ensure index template
    if not _ensure_index_template(es):
        return "abort"

    # Step 6: Claim staging — reindex to timestamped index
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    new_index = f"{INDEX_PREFIX}{timestamp}"
    logging.info(f"Claiming staging data → {new_index}")

    # Pre-create the index with explicit date mappings for _ms fields.
    # The template alone is not enough — reindex infers long from source values,
    # overriding the template.  Explicit mappings on create are authoritative.
    try:
        es.indices.create(
            index=new_index,
            body={
                "settings": {"number_of_replicas": 0},
                "mappings": {
                    "properties": {
                        "date_created_ms": {"type": "date", "format": "epoch_millis"},
                        "date_modified_ms": {"type": "date", "format": "epoch_millis"},
                        "meta": {
                            "properties": {
                                "first_contact_date_ms": {"type": "date", "format": "epoch_millis"},
                                "baptism_date_ms": {"type": "date", "format": "epoch_millis"},
                                "start_date_ms": {"type": "date", "format": "epoch_millis"},
                                "church_start_date_ms": {"type": "date", "format": "epoch_millis"},
                            }
                        },
                    }
                },
            },
        )
        logging.info(f"Pre-created index '{new_index}' with date mappings")
    except Exception as e:
        logging.error(f"Failed to pre-create index: {e}")
        return "abort"

    try:
        result = es.reindex(
            body={
                "source": {"index": STAGING_INDEX},
                "dest": {"index": new_index},
            },
            wait_for_completion=True,
            request_timeout=300,
        )
        es.indices.refresh(index=new_index)
    except Exception as e:
        logging.error(f"Reindex failed: {e}")
        # Clean up partial index if it was created
        try:
            if es.indices.exists(index=new_index):
                es.indices.delete(index=new_index)
        except Exception:
            pass
        return "abort"

    # Verify doc counts match
    new_count = es.count(index=new_index)["count"]
    if new_count != staging_count:
        logging.error(f"Doc count mismatch after reindex: staging={staging_count}, " f"new={new_count}. Aborting.")
        try:
            es.indices.delete(index=new_index)
        except Exception:
            pass
        return "abort"

    logging.info(f"Claimed {new_count} documents into {new_index}")

    # Step 7: Delete staging (unblocks next DT export)
    try:
        es.indices.delete(index=STAGING_INDEX)
        logging.info(f"Deleted staging index '{STAGING_INDEX}'")
    except Exception as e:
        logging.warning(f"Failed to delete staging index (non-fatal): {e}")

    # Step 8: Enrich with organization_title
    logging.info("Enriching documents with organization_title...")
    enrich_stats = process_index(es, new_index, team_org_map)
    logging.info(
        f"Enrichment complete: {enrich_stats['updated']} updated, "
        f"{enrich_stats['already_correct']} already correct, "
        f"{enrich_stats['errors']} errors"
    )

    if enrich_stats["errors"] > 0:
        logging.error(f"Enrichment had {enrich_stats['errors']} errors — aborting " f"to preserve current live data")
        try:
            es.indices.delete(index=new_index)
        except Exception:
            pass
        return "abort"

    # Step 9: Atomic alias swap
    logging.info("Swapping alias...")
    old_index = get_alias_backing_index(es, ALIAS_NAME)

    actions = []
    if old_index:
        actions.append({"remove": {"index": old_index, "alias": ALIAS_NAME}})
    actions.append({"add": {"index": new_index, "alias": ALIAS_NAME}})

    try:
        es.indices.update_aliases(body={"actions": actions})
        logging.info(f"Alias '{ALIAS_NAME}' now points to '{new_index}'")
    except Exception as e:
        logging.error(f"Alias swap failed: {e}")
        return "abort"

    # Step 10: Delete old timestamped index
    if old_index:
        try:
            es.indices.delete(index=old_index)
            logging.info(f"Deleted old index '{old_index}'")
        except Exception as e:
            logging.warning(f"Failed to delete old index '{old_index}' (non-fatal): {e}")

    # Step 11: Pre-warm geocode cache for map dashboard
    logging.info("Pre-warming geocode cache for map dashboard...")
    geo_stats = prewarm_geocache(es, new_index)
    logging.info(
        f"Geocache: {geo_stats['newly_geocoded']} new, "
        f"{geo_stats['already_cached']} cached, "
        f"{geo_stats['failed']} failed"
    )

    # Step 12: Final validation
    final_count = es.count(index=ALIAS_NAME)["count"]
    logging.info("")
    logging.info("=" * 70)
    logging.info("PROMOTION COMPLETE")
    logging.info(f"  Alias: {ALIAS_NAME} → {new_index}")
    logging.info(f"  Documents: {final_count}")
    logging.info(f"  Enriched: {enrich_stats['updated']}")
    logging.info(f"  Already correct: {enrich_stats['already_correct']}")
    logging.info(f"  Geocoded: {geo_stats['newly_geocoded']} new addresses")
    logging.info("=" * 70)

    return "success"


if __name__ == "__main__":
    main({})
