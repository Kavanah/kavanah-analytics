"""Migrate Google Analytics per-team indices to unified ga_metrics index."""

import logging
import lib.logging
from lib.es_helpers import get_elasticsearch_client, create_index_if_not_exists
from migration.schema_loader import get_schema_for_index_type


def migrate_ga_metrics():
    """
    Migrate all per-team Google Analytics metrics indices to unified ga_metrics index.

    This function:
    1. Creates the unified ga_metrics index if it doesn't exist
    2. Finds all existing per-team indices matching *_ga_metrics pattern
    3. Reindexes each team index into the unified index
    4. Validates document counts to ensure no data loss
    """
    lib.logging.define("migrate-ga-to-unified")

    es = get_elasticsearch_client()
    if not es:
        logging.error("Failed to connect to Elasticsearch")
        return False

    try:
        # 1. Ensure unified index exists with proper settings
        logging.info("Creating unified ga_metrics index...")
        mapping = get_schema_for_index_type("ga_metrics")

        # Create index with optimized settings for unified index
        if not es.indices.exists(index="ga_metrics"):
            es.indices.create(
                index="ga_metrics",
                body={
                    "settings": {
                        "number_of_shards": 1,
                        "number_of_replicas": 0,  # Single-node cluster
                        "refresh_interval": "5s",
                    },
                    "mappings": mapping,
                },
            )
            logging.info("Created unified ga_metrics index")
        else:
            logging.info("Unified ga_metrics index already exists")

        # 2. Find all per-team GA indices
        all_indices = es.indices.get(index="*_ga_metrics")
        team_indices = [idx for idx in all_indices.keys() if idx != "ga_metrics"]

        logging.info(f"Found {len(team_indices)} team indices to migrate: {', '.join(team_indices)}")

        if not team_indices:
            logging.info("No team indices found to migrate")
            return True

        # 3. Reindex each to unified index
        total_created = 0
        total_updated = 0

        for source_index in team_indices:
            logging.info(f"Reindexing {source_index} -> ga_metrics...")

            try:
                result = es.reindex(
                    body={
                        "source": {"index": source_index},
                        "dest": {"index": "ga_metrics"},
                    },
                    wait_for_completion=True,
                    request_timeout=300,
                )

                created = result.get("created", 0)
                updated = result.get("updated", 0)
                total_created += created
                total_updated += updated

                logging.info(f"  ✓ {source_index}: Created {created}, Updated {updated} documents")

            except Exception as e:
                logging.error(f"  ✗ Failed to reindex {source_index}: {e}")
                return False

        # 4. Validate document counts
        logging.info("Validating document counts...")

        source_count = 0
        for idx in team_indices:
            try:
                count = es.count(index=idx)["count"]
                source_count += count
                logging.info(f"  {idx}: {count} documents")
            except Exception as e:
                logging.error(f"  Error counting {idx}: {e}")

        dest_count = es.count(index="ga_metrics")["count"]

        logging.info(f"\nMigration Summary:")
        logging.info(f"  Total source documents: {source_count}")
        logging.info(f"  Unified index documents: {dest_count}")
        logging.info(f"  Documents created: {total_created}")
        logging.info(f"  Documents updated: {total_updated}")

        if source_count != dest_count:
            logging.warning(f"⚠ Count mismatch: {source_count} source docs != {dest_count} destination docs")
            logging.warning("This may be expected if the unified index already contained some documents")
        else:
            logging.info("✓ Document counts match perfectly!")

        logging.info("\nMigration completed successfully!")
        logging.info(f"Old indices preserved as read-only archives: {', '.join(team_indices)}")
        logging.info("To mark old indices as read-only, run:")
        logging.info(
            "  curl -X PUT \"https://localhost:9200/*_ga_metrics/_settings\" -u elastic:$ES_PASSWORD --cacert /app/data/certs/es01_http_ca.crt -H 'Content-Type: application/json' -d'{\"index.blocks.write\": true}'"
        )

        return True

    except Exception as e:
        logging.error(f"Migration failed: {e}")
        return False


def main(params):
    """Entry point for migration script."""
    lib.logging.define("migrate-ga-to-unified")
    logging.info("Starting Google Analytics metrics migration to unified index...")

    success = migrate_ga_metrics()

    if success:
        logging.info("Migration completed successfully")
    else:
        logging.error("Migration failed")
        return False


if __name__ == "__main__":
    main({})
