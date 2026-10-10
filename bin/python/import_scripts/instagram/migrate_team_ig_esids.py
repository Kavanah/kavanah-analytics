import logging
import lib.logging
from import_scripts.facebook.lib.assets import get_fb_teams, get_ig_accounts
from lib.es_helpers import get_elasticsearch_client


def main(params):
    """
    One-time migration: re-link team_instagram_profile_esids to current de_asset_ig_accounts documents.

    Background:
        Teams store Instagram account references as ESIDs (team_instagram_profile_esids).
        When IG accounts are re-imported via CSV, new ES documents are created with new IDs,
        leaving the team ESIDs stale. This script reads the stable numeric ig_account_id
        values stored on each team and resolves them to the current correct ESIDs.

    Usage:
        python3 /app/python/import.py instagram migrate_team_ig_esids
        python3 /app/python/import.py instagram migrate_team_ig_esids --dry-run
    """
    lib.logging.define("ig-migrate-team-esids")

    dry_run = params.get("dry-run") or params.get("dry_run")
    if dry_run:
        logging.info("=== DRY RUN mode — no changes will be written ===")

    # Step 1: Build mapping from numeric ig_account_id → current ES document ESID
    logging.info("Loading current Instagram accounts from de_asset_ig_accounts...")
    ig_accounts_docs = get_ig_accounts(filters=None)
    if not ig_accounts_docs:
        logging.error("No Instagram accounts found in de_asset_ig_accounts. Aborting.")
        return

    ig_id_to_esid = {}  # str(numeric_id) → new ES _id
    ig_id_to_title = {}  # str(numeric_id) → ig_account_title (for logging)
    for doc in ig_accounts_docs:
        esid = doc["_id"]
        source = doc["_source"]
        ig_account_id = source.get("ig_account_id")
        ig_account_title = source.get("ig_account_title", "Unknown")
        if ig_account_id:
            ig_id_to_esid[str(ig_account_id)] = esid
            ig_id_to_title[str(ig_account_id)] = ig_account_title

    logging.info(f"Found {len(ig_id_to_esid)} current Instagram accounts")

    # Step 2: Load all teams
    logging.info("Loading teams from de_asset_teams...")
    teams_docs = get_fb_teams(filters=None)
    if not teams_docs:
        logging.error("No teams found. Aborting.")
        return

    logging.info(f"Found {len(teams_docs)} teams")

    # Step 3: Connect to ES for partial updates
    es = get_elasticsearch_client()
    if not es:
        logging.error("Failed to connect to Elasticsearch. Aborting.")
        return

    teams_updated = 0
    teams_skipped = 0
    teams_failed = 0

    for team_doc in teams_docs:
        team_esid = team_doc["_id"]
        team = team_doc["_source"]
        team_title = team.get("team_title", "Unknown")

        stale_esids = team.get("team_instagram_profile_esids", [])
        ig_account_ids = team.get("ig_account_id", [])  # numeric IDs stored on team by Asset Explorer

        # Skip teams with no Instagram profiles at all
        if not stale_esids and not ig_account_ids:
            continue

        if not ig_account_ids:
            logging.warning(
                f"Team '{team_title}' (ESID: {team_esid}) has team_instagram_profile_esids "
                f"but no ig_account_id values — cannot migrate automatically. "
                f"Re-select Instagram accounts in Asset Explorer manually."
            )
            teams_skipped += 1
            continue

        # Normalize to list (ES may store single value as scalar)
        if not isinstance(ig_account_ids, list):
            ig_account_ids = [ig_account_ids]
        if not isinstance(stale_esids, list):
            stale_esids = [stale_esids]

        # Map each numeric ID to its current ESID
        new_esids = []
        new_ig_ids = []
        missing_ids = []

        for ig_id in ig_account_ids:
            ig_id_str = str(ig_id)
            new_esid = ig_id_to_esid.get(ig_id_str)
            if new_esid:
                new_esids.append(new_esid)
                new_ig_ids.append(ig_id_str)
                logging.info(
                    f"  Team '{team_title}': IG {ig_id_str} " f"({ig_id_to_title.get(ig_id_str, '?')}) → ESID {new_esid}"
                )
            else:
                missing_ids.append(ig_id_str)
                logging.warning(
                    f"  Team '{team_title}': IG account ID {ig_id_str} not found in "
                    f"current de_asset_ig_accounts — skipping this account"
                )

        if not new_esids:
            logging.warning(
                f"Team '{team_title}': No IG accounts could be resolved — skipping. " f"Missing IDs: {missing_ids}"
            )
            teams_skipped += 1
            continue

        # Check if already up to date
        if sorted(stale_esids) == sorted(new_esids):
            logging.info(f"Team '{team_title}': ESIDs already up to date — skipping")
            teams_skipped += 1
            continue

        logging.info(
            f"Team '{team_title}': updating team_instagram_profile_esids\n"
            f"  Before: {stale_esids}\n"
            f"  After:  {new_esids}"
        )

        if not dry_run:
            try:
                es.update(
                    index="de_asset_teams",
                    id=team_esid,
                    body={
                        "doc": {
                            "team_instagram_profile_esids": new_esids,
                            "ig_account_id": new_ig_ids,
                        }
                    },
                    refresh="wait_for",
                )
                logging.info(f"  ✓ Team '{team_title}' updated successfully")
                teams_updated += 1
            except Exception as e:
                logging.error(f"  ✗ Failed to update team '{team_title}': {e}")
                teams_failed += 1
        else:
            logging.info(f"  [DRY RUN] Would update team '{team_title}'")
            teams_updated += 1

    logging.info("\n=== Migration Summary ===")
    logging.info(f"Teams updated: {teams_updated}")
    logging.info(f"Teams skipped (no change needed or not resolvable): {teams_skipped}")
    logging.info(f"Teams failed: {teams_failed}")
    if dry_run:
        logging.info("(DRY RUN — no actual changes were made)")
    elif teams_updated > 0 and teams_failed == 0:
        logging.info("Migration completed successfully.")
