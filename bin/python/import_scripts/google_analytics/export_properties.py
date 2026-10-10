"""
GA4 Properties Lister

Lists all accessible GA4 properties with their details, available dimensions, and metrics.
Useful for discovering property IDs and understanding available data fields.

Also auto-creates/updates ES assets for GA accounts and properties.
"""

import logging
import hashlib
from datetime import datetime
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.admin import AnalyticsAdminServiceClient
from import_scripts.google_analytics.lib.ga_config import GAConfig, iter_connected_ga4_accounts
from lib import logging as custom_logging
from lib.es_helpers import get_elasticsearch_client

# Initialize configuration
config = GAConfig()

# Logger will be set up by custom_logging.define() in main()
logger = logging.getLogger(__name__)


def create_or_update_ga_account(es_client, account_summary):
    """Create or update GA account in de_asset_ga_accounts."""
    account_id = account_summary.account.split("/")[-1]
    # MD5 used as a non-cryptographic stable hash to derive an ES
    # doc ID from the GA account ID. Not a credential, not a sensitive value.
    # codeql[py/weak-sensitive-data-hashing]: not sensitive data.
    doc_id = hashlib.md5(  # nosemgrep: insecure-hash-algorithm-md5 - non-cryptographic doc ID fingerprint
        f"ga_account_{account_id}".encode()
    ).hexdigest()

    doc = {
        "ga_account_id": account_id,
        "ga_account_resource_name": account_summary.account,
        "ga_account_title": account_summary.display_name,
        "ga_account_description": f"Google Analytics Account {account_id}",
    }

    try:
        es_client.index(index="de_asset_ga_accounts", id=doc_id, document=doc)
        logger.info(f"Created/updated GA account asset: {account_id}")
        return doc_id, account_id
    except Exception as e:
        logger.error(f"Failed to create/update GA account asset {account_id}: {e}")
        return None, account_id


def create_or_update_ga_property(
    es_client,
    property_summary,
    property_details,
    account_esid,
    account_id,
    account_title,
):
    """Create or update GA property in de_asset_ga_properties."""
    property_id = property_summary.property.split("/")[-1]
    doc_id = hashlib.md5(  # nosemgrep: insecure-hash-algorithm-md5 - non-cryptographic doc ID fingerprint
        f"ga_property_{property_id}".encode()
    ).hexdigest()

    doc = {
        "ga_property_id": property_id,
        "ga_property_resource_name": property_summary.property,
        "ga_property_title": property_details.display_name,
        "ga_property_display_name": property_details.display_name,
        "ga_account_id": account_id,
        "ga_account_esid": account_esid,
        "ga_account_title": account_title,
        # Team assignment is managed via team_ga4_properties on teams, not stored here
    }

    try:
        es_client.index(index="de_asset_ga_properties", id=doc_id, document=doc)
        logger.info(f"Created/updated GA property asset: {property_id}")
        return doc_id
    except Exception as e:
        logger.error(f"Failed to create/update GA property asset {property_id}: {e}")
        return None


def main(params):
    """Main entry point from import.py."""
    custom_logging.define("ga-export-properties")

    # Parse skip_dimensions parameter
    skip_dimensions = params.get("skip_dimensions", "false").lower() in (
        "true",
        "1",
        "yes",
    )

    if skip_dimensions:
        logger.info("skip_dimensions=true - will not fetch dimensions and metrics")

    # GA4 is OAuth-only (service account retired). Discovery now
    # iterates the GA4 accounts users have authorized via the Data Connectors
    # wizard (de_asset_ga4_accounts with a stored token) and lists each
    # account's properties under that account's own OAuth token. There is no
    # single "sees everything" identity any more — an account must be connected
    # before its properties can be discovered (the wizard authorizes at the
    # account level, so this matches the product flow).
    # validate_config call removed with the method (a no-op).

    # Get Elasticsearch client for asset management
    try:
        es_client = get_elasticsearch_client()
        logger.info("Connected to Elasticsearch for asset management")
    except Exception as e:
        logger.warning(f"Failed to connect to Elasticsearch: {e}. Assets will not be created/updated.")
        es_client = None

    print("\n" + "=" * 100)
    print("All Accessible Properties (across connected GA4 OAuth accounts)")
    print("=" * 100)

    property_count = 0
    accounts_synced = 0
    properties_synced = 0
    connected_accounts = 0

    # One (admin, data) client pair per connected GA4 account, scoped to that
    # account's OAuth token. iter_connected_ga4_accounts is fail-soft per account.
    for _account_doc, credentials in iter_connected_ga4_accounts():
        connected_accounts += 1
        try:
            admin_client = AnalyticsAdminServiceClient(credentials=credentials)
            data_client = BetaAnalyticsDataClient(credentials=credentials)
            account_summaries = admin_client.list_account_summaries()
        except Exception as e:  # noqa: BLE001 — per-account boundary
            logger.error(f"Failed to list properties for a connected GA4 account: {e}")
            continue

        # Iterate through account summaries and their property summaries
        for summary in account_summaries:
            account_name = summary.account
            account_display_name = summary.display_name

            # Apply account filtering
            if not config.is_account_allowed(account_name):
                logger.info(f"Skipping account {account_display_name} ({account_name}) - not in account_filter")
                continue

            print(f"\nAccount: {account_display_name} ({account_name})")

            # Create/update account asset
            account_esid = None
            if es_client:
                account_esid, account_id = create_or_update_ga_account(es_client, summary)
                if account_esid:
                    accounts_synced += 1

            for prop_summary in summary.property_summaries:
                property_count += 1
                property_resource_name = prop_summary.property
                property_id = property_resource_name.split("/")[-1]

                # Get full property details
                try:
                    property = admin_client.get_property(name=property_resource_name)

                    # Create/update property asset
                    if es_client and account_esid:
                        property_esid = create_or_update_ga_property(
                            es_client,
                            prop_summary,
                            property,
                            account_esid,
                            account_id,
                            account_display_name,
                        )
                        if property_esid:
                            properties_synced += 1

                    print(f"\n{'='*100}")
                    print(f"Property #{property_count}")
                    print(f"{'='*100}")
                    print(f"  Property Name: {property.display_name}")
                    print(f"  Property ID: {property_id}")
                    print(f"  Resource Name: {property.name}")
                    print(f"  Time Zone: {property.time_zone}")
                    print(f"  Currency Code: {property.currency_code}")
                    print(f"  Industry Category: {property.industry_category}")

                    # Fetch available dimensions for this property (unless skipped)
                    if not skip_dimensions:
                        try:
                            logger.info(f"Fetching dimensions for property: {property_id}")

                            # Get metadata (dimensions and metrics)
                            metadata = data_client.get_metadata(name=f"properties/{property_id}/metadata")

                            print(f"\n  Available Dimensions ({len(metadata.dimensions)}):")
                            print(f"  {'-'*96}")

                            for dimension in metadata.dimensions:
                                print(
                                    f"    - {dimension.api_name:40} | Category: {dimension.category:20} | UI Name: {dimension.ui_name}"
                                )

                            print(f"\n  Available Metrics ({len(metadata.metrics)}):")
                            print(f"  {'-'*96}")

                            for metric in metadata.metrics:
                                print(
                                    f"    - {metric.api_name:40} | Category: {metric.category:20} | UI Name: {metric.ui_name}"
                                )

                        except Exception as e:
                            logger.error(f"Failed to fetch dimensions for property {property_id}: {e}")
                            print(f"  Error fetching dimensions: {e}")

                except Exception as e:
                    logger.error(f"Failed to get property details for {property_resource_name}: {e}")
                    print(f"  Error: {e}")

    print(f"\n{'='*100}")
    print(f"Connected GA4 accounts processed: {connected_accounts}")
    print(f"Total Properties Found: {property_count}")
    if config.account_filter:
        print(f"Filtered by accounts: {', '.join(config.account_filter)}")
    if es_client:
        print(f"\nES Asset Management:")
        print(f"  Accounts synced: {accounts_synced}")
        print(f"  Properties synced: {properties_synced}")
    print(f"{'='*100}\n")

    if connected_accounts == 0:
        print("\nNo connected GA4 OAuth accounts found. Authorize a GA4 account via the Data Connectors wizard first.")
    elif property_count == 0:
        print("\nNo properties found.")
