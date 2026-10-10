"""
Migrate Team Instagram fields from single-select to multi-select arrays.

This script migrates teams from the old single-select Instagram integration fields
(team_instagram_profile_esid, ig_account_title) to the new multi-select array fields
(team_instagram_profile_esids, ig_account_id).

The old fields are kept for rollback safety and will be removed in a later cleanup phase.
"""

import logging
import lib.logging
from lib.es_helpers import get_elasticsearch_client


def migrate_team_instagram_fields():
    """
    Migrate team Instagram fields from single-select to multi-select.

    For each team:
    - If team_instagram_profile_esid exists and team_instagram_profile_esids is empty:
      - Lookup Instagram account by ESID
      - Add ig_account_id to ig_account_id array
      - Add team_instagram_profile_esid to team_instagram_profile_esids array
      - Log migration action
    - Otherwise skip (already migrated or no Instagram account)

    Returns:
        dict: Statistics about the migration
    """
    lib.logging.define("migrate-team-instagram-fields")

    es = get_elasticsearch_client()
    if not es:
        logging.error("Failed to connect to Elasticsearch")
        return {"success": False, "error": "Failed to connect to Elasticsearch"}

    stats = {
        "total_teams": 0,
        "already_migrated": 0,
        "no_ig_account": 0,
        "migrated": 0,
        "errors": 0,
        "error_details": [],
    }

    try:
        # Get all teams
        logging.info("Fetching all teams from de_asset_teams...")
        result = es.search(index="de_asset_teams", body={"query": {"match_all": {}}, "size": 10000})

        teams = result.get("hits", {}).get("hits", [])
        stats["total_teams"] = len(teams)

        logging.info(f"Found {stats['total_teams']} teams to process")

        for team_hit in teams:
            team_esid = team_hit["_id"]
            team = team_hit["_source"]
            team_title = team.get("team_title", "unknown")

            # Check if already migrated
            if team.get("team_instagram_profile_esids") and len(team.get("team_instagram_profile_esids", [])) > 0:
                logging.debug(f"Team '{team_title}' (ESID={team_esid}) already migrated, skipping")
                stats["already_migrated"] += 1
                continue

            # Check if team has old Instagram account field
            ig_account_esid = team.get("team_instagram_profile_esid")
            if not ig_account_esid:
                logging.debug(f"Team '{team_title}' (ESID={team_esid}) has no team_instagram_profile_esid, skipping")
                stats["no_ig_account"] += 1
                continue

            try:
                # Lookup the Instagram account to get its ig_account_id
                logging.info(f"Migrating team '{team_title}' (ESID={team_esid}) with Instagram account ESID {ig_account_esid}")

                ig_account_result = es.get(index="de_asset_ig_accounts", id=ig_account_esid)
                ig_account = ig_account_result.get("_source", {})
                ig_account_id = ig_account.get("ig_account_id")

                if not ig_account_id:
                    error_msg = f"Instagram account {ig_account_esid} has no ig_account_id field"
                    logging.warning(f"Team '{team_title}' (ESID={team_esid}): {error_msg}")
                    stats["errors"] += 1
                    stats["error_details"].append(
                        {
                            "team_esid": team_esid,
                            "team_title": team_title,
                            "error": error_msg,
                        }
                    )
                    continue

                # Update the team document with new array fields
                update_body = {
                    "doc": {
                        "ig_account_id": [ig_account_id],
                        "team_instagram_profile_esids": [ig_account_esid],
                    }
                }

                es.update(index="de_asset_teams", id=team_esid, body=update_body)

                logging.info(f"✓ Migrated team '{team_title}' (ESID={team_esid}): added Instagram Account ID {ig_account_id}")
                stats["migrated"] += 1

            except Exception as e:
                error_msg = str(e)
                logging.error(f"Error migrating team '{team_title}' (ESID={team_esid}): {error_msg}")
                stats["errors"] += 1
                stats["error_details"].append(
                    {
                        "team_esid": team_esid,
                        "team_title": team_title,
                        "error": error_msg,
                    }
                )

        # Print summary
        logging.info("=" * 70)
        logging.info("Migration Summary:")
        logging.info(f"  Total teams processed: {stats['total_teams']}")
        logging.info(f"  Already migrated: {stats['already_migrated']}")
        logging.info(f"  No Instagram account: {stats['no_ig_account']}")
        logging.info(f"  Successfully migrated: {stats['migrated']}")
        logging.info(f"  Errors: {stats['errors']}")

        if stats["errors"] > 0:
            logging.warning("Errors encountered during migration:")
            for error_detail in stats["error_details"]:
                logging.warning(
                    f"  - Team '{error_detail['team_title']}' (ESID={error_detail['team_esid']}): {error_detail['error']}"
                )

        logging.info("=" * 70)

        stats["success"] = True
        return stats

    except Exception as e:
        logging.error(f"Fatal error during migration: {e}")
        stats["success"] = False
        stats["error"] = str(e)
        return stats


def main(params):
    """Entry point for team Instagram fields migration script."""
    lib.logging.define("migrate-team-instagram-fields")
    logging.info("Starting Team Instagram fields migration...")
    logging.info("Migrating from single-select (team_instagram_profile_esid) to multi-select (team_instagram_profile_esids)")
    logging.info("")

    result = migrate_team_instagram_fields()

    if result.get("success"):
        logging.info("✓ Migration completed successfully")
        if result["migrated"] > 0:
            logging.info("")
            logging.info("Next steps:")
            logging.info("1. Test Edit Teams form to verify multi-select interface works correctly")
            logging.info("2. Test Instagram insights import to verify data is matched correctly")
            logging.info("3. Monitor for 1-2 weeks before proceeding to cleanup phase")
            logging.info("4. Old fields (team_instagram_profile_esid, ig_account_title) are kept for rollback safety")
    else:
        logging.error(f"✗ Migration failed: {result.get('error', 'Unknown error')}")
        return False

    return True
