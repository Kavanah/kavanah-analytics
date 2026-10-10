#!/usr/bin/env python3
"""
One-time migration: rename menu field from "Custom Dashboards" to "Reports"
in de_asset_echarts_dashboards.

Part of.

Usage:
    sudo docker exec imports_container python3 /app/python/migration/migrate_dashboard_menu_to_reports.py [--dry-run]
"""

import sys
import os
import argparse
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.es_helpers import get_elasticsearch_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

INDEX = "de_asset_echarts_dashboards"


def main() -> int:
    parser = argparse.ArgumentParser(description='Rename dashboard menu "Custom Dashboards" -> "Reports"')
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Failed to connect to Elasticsearch")
        return 1

    query = {"term": {"menu.keyword": "Custom Dashboards"}}

    # Count affected documents
    try:
        count = es.count(index=INDEX, query=query)["count"]
    except Exception as e:
        logger.error(f"Error counting documents: {e}")
        return 1

    logger.info(f'Found {count} dashboard(s) with menu="Custom Dashboards"')

    if count == 0:
        logger.info("Nothing to migrate.")
        return 0

    if args.dry_run:
        logger.info(f'[DRY RUN] Would update {count} document(s) to menu="Reports"')
        return 0

    try:
        result = es.update_by_query(
            index=INDEX,
            query=query,
            script={
                "source": "ctx._source.menu = 'Reports'",
                "lang": "painless",
            },
            refresh=True,
        )
        updated = result.get("updated", 0)
        failures = result.get("failures", [])

        if failures:
            logger.error(f"Migration had {len(failures)} failure(s): {failures}")
            return 1

        logger.info(f'Successfully updated {updated} dashboard(s) to menu="Reports"')
        return 0
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
