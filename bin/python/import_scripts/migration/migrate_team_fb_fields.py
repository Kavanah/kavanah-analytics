"""
Migrate Team Facebook fields from single-select to multi-select arrays.

This script migrates teams from the old single-select Facebook integration fields
(fb_page_esid, fb_page_name) to the new multi-select array fields
(team_facebook_page_esids, team_facebook_page_id).

The old fields are kept for rollback safety and will be removed in a later cleanup phase.
"""

import logging
import lib.logging
from lib.es_helpers import get_elasticsearch_client


def migrate_team_fb_fields():
    """
    Migrate team Facebook fields from single-select to multi-select.

    For each team:
    - If fb_page_esid exists and team_facebook_page_esids is empty:
      - Lookup FB page by ESID
      - Add fb_page_id to team_facebook_page_id array
      - Add fb_page_esid to team_facebook_page_esids array
      - Log migration action
    - Otherwise skip (already migrated or no FB page)

    Returns:
        dict: Statistics about the migration
    """
    lib.logging.define("migrate-team-fb-fields")

    es = get_elasticsearch_client()
    if not es:
        logging.error("Failed to connect to Elasticsearch")
        return {"success": False, "error": "Failed to connect to Elasticsearch"}

    stats = {
        "total_teams": 0,
        "already_migrated": 0,
        "no_fb_page": 0,
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
            if team.get("team_facebook_page_esids") and len(team.get("team_facebook_page_esids", [])) > 0:
                logging.debug(f"Team '{team_title}' (ESID={team_esid}) already migrated, skipping")
                stats["already_migrated"] += 1
                continue

            # Check if team has old FB page field
            fb_page_esid = team.get("fb_page_esid")
            if not fb_page_esid:
                logging.debug(f"Team '{team_title}' (ESID={team_esid}) has no fb_page_esid, skipping")
                stats["no_fb_page"] += 1
                continue

            try:
                # Lookup the FB page to get its fb_page_id
                logging.info(f"Migrating team '{team_title}' (ESID={team_esid}) with FB page ESID {fb_page_esid}")

                page_result = es.get(index="de_asset_fb_pages", id=fb_page_esid)
                page = page_result.get("_source", {})
                fb_page_id = page.get("fb_page_id")

                if not fb_page_id:
                    error_msg = f"FB page {fb_page_esid} has no fb_page_id field"
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
                        "team_facebook_page_id": [fb_page_id],
                        "team_facebook_page_esids": [fb_page_esid],
                    }
                }

                es.update(index="de_asset_teams", id=team_esid, body=update_body)

                logging.info(f"✓ Migrated team '{team_title}' (ESID={team_esid}): added FB page ID {fb_page_id}")
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
        logging.info(f"  No FB page: {stats['no_fb_page']}")
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
    """Entry point for team FB fields migration script."""
    lib.logging.define("migrate-team-fb-fields")
    logging.info("Starting Team Facebook fields migration...")
    logging.info("Migrating from single-select (fb_page_esid) to multi-select (team_facebook_page_esids)")
    logging.info("")

    result = migrate_team_fb_fields()

    if result.get("success"):
        logging.info("✓ Migration completed successfully")
        if result["migrated"] > 0:
            logging.info("")
            logging.info("Next steps:")
            logging.info("1. Test Echo import to verify conversations are matched correctly")
            logging.info("2. Monitor for 1 week before proceeding to Phase 3 (code cleanup)")
            logging.info("3. Old fields (fb_page_esid, fb_page_name) are kept for rollback safety")
    else:
        logging.error(f"✗ Migration failed: {result.get('error', 'Unknown error')}")
        return False

    return True
