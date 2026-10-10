"""
One-time migration: convert the 'disciple-tools' concrete index into an alias.

Before the staging→live promotion pipeline can operate, 'disciple-tools' must
be an alias pointing to a timestamped backing index, not a concrete index.

This script:
1. Reindexes 'disciple-tools' → 'disciple-tools-<timestamp>'
2. Enriches the new index with organization_title
3. Deletes the concrete 'disciple-tools' index
4. Creates 'disciple-tools' as an alias → timestamped index

Safe to re-run: exits immediately if 'disciple-tools' is already an alias.

Execution:
    python3 /app/python/import.py migration convert_dt_index_to_alias
    python3 /app/python/import.py migration convert_dt_index_to_alias --dry-run=true
"""

import logging
from datetime import datetime

import lib.logging
from lib.es_helpers import get_elasticsearch_client, is_alias
from import_scripts.migration.backfill_organization_title import (
    load_team_organizations,
    process_index,
)


def main(params):
    """Convert disciple-tools from a concrete index to an alias.

    Args:
        params: Dictionary of parameters from import.py
                - dry-run: If "true", only preview what would happen

    Returns:
        str: 'success' or 'abort'
    """
    lib.logging.define("convert-dt-to-alias")

    dry_run = (
        params.get("dry-run", "").lower() == "true" if isinstance(params.get("dry-run"), str) else bool(params.get("dry-run"))
    )

    logging.info("=" * 70)
    logging.info("Convert 'disciple-tools' concrete index to alias")
    if dry_run:
        logging.info("DRY RUN — no changes will be made")
    logging.info("=" * 70)

    # Connect to ES
    es = get_elasticsearch_client()
    if not es:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    # Check if already an alias
    if is_alias(es, "disciple-tools"):
        logging.info("'disciple-tools' is already an alias — nothing to do")
        return "success"

    # Check if concrete index exists
    if not es.indices.exists(index="disciple-tools"):
        logging.info("'disciple-tools' does not exist — nothing to migrate")
        return "success"

    # Get current doc count
    doc_count = es.count(index="disciple-tools")["count"]
    logging.info(f"Current 'disciple-tools' index has {doc_count} documents")

    if dry_run:
        logging.info("[DRY RUN] Would convert to alias with org enrichment")
        return "success"

    # Load team→org mappings for enrichment
    logging.info("Loading team organization mappings...")
    team_org_map = load_team_organizations(es)
    if team_org_map is None:
        logging.error("Failed to load team organizations")
        return "abort"
    logging.info(f"Loaded {len(team_org_map)} team mappings")

    # Create index template for timestamped indices
    template_name = "template_disciple_tools_timestamped"
    try:
        es.indices.put_index_template(
            name=template_name,
            body={
                "index_patterns": ["disciple-tools-*"],
                "priority": 200,
                "template": {
                    "settings": {"number_of_replicas": 0},
                },
            },
        )
        logging.info(f"Index template '{template_name}' created")
    except Exception as e:
        logging.error(f"Failed to create index template: {e}")
        return "abort"

    # Reindex to timestamped index
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    new_index = f"disciple-tools-{timestamp}"
    logging.info(f"Reindexing 'disciple-tools' → '{new_index}'...")

    try:
        es.reindex(
            body={
                "source": {"index": "disciple-tools"},
                "dest": {"index": new_index},
            },
            wait_for_completion=True,
            request_timeout=300,
        )
        es.indices.refresh(index=new_index)
    except Exception as e:
        logging.error(f"Reindex failed: {e}")
        try:
            if es.indices.exists(index=new_index):
                es.indices.delete(index=new_index)
        except Exception:
            pass
        return "abort"

    # Verify doc counts
    new_count = es.count(index=new_index)["count"]
    if new_count != doc_count:
        logging.error(f"Doc count mismatch: original={doc_count}, new={new_count}. Aborting.")
        try:
            es.indices.delete(index=new_index)
        except Exception:
            pass
        return "abort"

    logging.info(f"Reindex complete: {new_count} documents")

    # Enrich with organization_title
    logging.info("Enriching documents with organization_title...")
    enrich_stats = process_index(es, new_index, team_org_map)
    logging.info(
        f"Enrichment: {enrich_stats['updated']} updated, "
        f"{enrich_stats['already_correct']} already correct, "
        f"{enrich_stats['errors']} errors"
    )

    # Delete the concrete index
    logging.info("Deleting concrete 'disciple-tools' index...")
    try:
        es.indices.delete(index="disciple-tools")
    except Exception as e:
        logging.error(f"Failed to delete concrete index: {e}")
        logging.error("Manual cleanup required: delete 'disciple-tools' then create alias")
        return "abort"

    # Create alias
    logging.info(f"Creating alias 'disciple-tools' → '{new_index}'...")
    try:
        es.indices.put_alias(index=new_index, name="disciple-tools")
    except Exception as e:
        logging.error(f"Failed to create alias: {e}")
        logging.error(
            f"CRITICAL: 'disciple-tools' index was deleted but alias not created. "
            f"Data is in '{new_index}'. Run manually: "
            f"PUT /{new_index}/_alias/disciple-tools"
        )
        return "abort"

    # Verify
    final_count = es.count(index="disciple-tools")["count"]
    logging.info("")
    logging.info("=" * 70)
    logging.info("MIGRATION COMPLETE")
    logging.info(f"  'disciple-tools' is now an alias → '{new_index}'")
    logging.info(f"  Documents: {final_count}")
    logging.info(f"  Enriched with organization_title: {enrich_stats['updated']}")
    logging.info("=" * 70)

    return "success"


if __name__ == "__main__":
    main({})
