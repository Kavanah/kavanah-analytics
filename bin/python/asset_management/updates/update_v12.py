import json
import logging
import sys
import os
from datetime import datetime

# Add parent directory to path to import lib
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client, add_document
from asset_management.web.services.kibana_service import KibanaService

# Set up logging
current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v12] %(message)s")

# Legacy rule: dashboards whose title carried a team's code were team-specific. The marker is
# deployment config; unset = no dashboard is forced to show_teams. This migration
# already ran on every existing deployment.
_TEAM_DASHBOARD_TITLE_MARKER = os.environ.get("UPDATE_V12_TEAM_DASHBOARD_MARKER", "")


def populate_dashboard_configs(es):
    """Populate de_asset_dashboards with existing dashboards from Kibana."""
    index_name = "de_asset_dashboards"
    space_id = "default"

    try:
        # Fetch all dashboards from Kibana
        logging.info(f"Fetching dashboards from Kibana space: {space_id}")
        kibana_dashboards = KibanaService.get_dashboards_in_space(space_id)

        if not kibana_dashboards:
            logging.warning("No dashboards found in Kibana")
            return "success"

        logging.info(f"Found {len(kibana_dashboards)} dashboards in Kibana")

        created_count = 0
        hidden_count = 0

        for dashboard in kibana_dashboards:
            dashboard_id = dashboard.get("id")
            dashboard_title = dashboard.get("title", "Untitled")
            dashboard_description = dashboard.get("description", "")

            # Determine visibility mode based on legacy filtering rules
            visibility_mode = "show_all"  # Default

            # Legacy rule: a marked team's dashboards were team-specific (custom dashboards).
            # They will appear in custom menu once configured via Asset Explorer
            if _TEAM_DASHBOARD_TITLE_MARKER and _TEAM_DASHBOARD_TITLE_MARKER in dashboard_title:
                visibility_mode = "show_teams"
                hidden_count += 1
                logging.info(
                    f"Setting dashboard '{dashboard_title}' to show_teams (custom dashboard - configure via Asset Explorer)"
                )

            # Hide Elastic security dashboards
            elif dashboard_title.startswith("[Elastic"):
                visibility_mode = "hide"
                hidden_count += 1
                logging.info(f"Setting dashboard '{dashboard_title}' to hide (Elastic Security)")

            # Create document in de_asset_dashboards
            document = {
                "dashboard_id": dashboard_id,
                "dashboard_title": dashboard_title,
                "dashboard_description": dashboard_description,
                "space_id": space_id,
                "visibility_mode": visibility_mode,
                "allowed_org_esids": [],
                "allowed_team_esids": [],
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }

            if add_document(index_name, document):
                created_count += 1
                logging.info(f"Created dashboard config: {dashboard_title} (visibility: {visibility_mode})")
            else:
                logging.error(f"Failed to create dashboard config: {dashboard_title}")

        logging.info(f"Dashboard migration complete: {created_count} created, {hidden_count} hidden")
        return "success"

    except Exception as e:
        logging.error(f"Error populating dashboard configs: {e}")
        return "abort"


def run():
    """Main function to run the update."""
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    logging.info("Starting update_v12: Populate de_asset_dashboards from Kibana")

    result = populate_dashboard_configs(es)

    if result == "abort":
        logging.error("Update aborted due to errors")
        return "abort"

    logging.info("Update_v12 completed successfully")
    return "success"


if __name__ == "__main__":
    run()
