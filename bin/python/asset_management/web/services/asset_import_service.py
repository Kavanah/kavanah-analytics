"""
Asset Import Service - handles bulk CSV imports for assets.
"""

import sys
import os
import csv
import io
import logging
from datetime import datetime

# Add parent directories to path for imports
sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)

from lib.es_helpers import get_elasticsearch_client


class AssetImportService:
    """Service class for bulk importing assets from CSV files."""

    # Configuration for each importable asset type
    ASSET_IMPORT_CONFIGS = {
        "instagram_accounts": {
            "index": "de_asset_ig_accounts",
            "unique_field": "ig_account_id",
            "field_mapping": {
                "Instagram Account": "ig_account_title",
                "Account ID": "ig_account_id",
                "Facebook Page": "fb_page_id",
                "Meta Business Portfolio": "fb_account_title",
            },
            "required_fields": ["Instagram Account", "Account ID", "Facebook Page"],
            "custom_handler": True,  # Use custom import logic for Instagram accounts
        },
        "facebook_ad_accounts": {
            "index": "de_asset_fb_ad_accounts",
            "unique_field": "fb_ad_account_id",
            "field_mapping": {
                "Ad Account Name": "fb_ad_account_title",
                "Ad Account ID": "fb_ad_account_id",
                "Facebook App": "fb_app_title",
                "Facebook App User": "fb_app_user_title",
                "Meta Business Portfolio": "fb_account_title",
            },
            "required_fields": ["Ad Account Name", "Ad Account ID"],
            "custom_handler": True,
        },
        "facebook_pages": {
            "index": "de_asset_fb_pages",
            "unique_field": "fb_page_id",
            "field_mapping": {
                "Page Name": "fb_page_name",
                "Page ID": "fb_page_id",
                "Page Slug": "fb_page_slug",
                "Meta Business Portfolio": "fb_account_title",
            },
            "required_fields": ["Page Name", "Page ID", "Page Slug"],
            "custom_handler": True,
        },
        "organizations": {
            "index": "de_asset_organization",
            "unique_field": "organization_title",
            "field_mapping": {
                "Org ID": "organization_title",
                "Org Name": "organization_description",
            },
            "required_fields": ["Org ID"],
        },
        "teams": {
            "index": "de_asset_teams",
            "unique_field": "team_title",
            "field_mapping": {
                "Team ID": "team_title",
                "Team Name": "team_name",
                "Team Org": "organization_title",
                "Team Language": "team_language",
                "Team Region": "team_region",
                "Team Country": "team_country_text",
                "Team City": "team_city",
                "Team GA4 Properties": "team_ga4_properties",
                "Team Facebook Page": "team_facebook_page_id",
                "Team Instagram Profile": "team_instagram_profile_esids",
                "Team Disciple Tools Instance": "team_disciple_tools_instance",
                "Team Echo Instance": "team_echo_instance_url",
                "Team YouTube Channel Handles": "team_youtube_channel_handle",  # Also accepts 'Team YouTube Channel' (legacy)
            },
            "required_fields": ["Team ID"],
            "custom_handler": True,  # Use custom import logic for teams
        },
        "youtube_channels": {
            "index": "de_asset_youtube_channels",
            "unique_field": "youtube_channel_handle",
            "field_mapping": {
                "Channel Name": "youtube_channel_title",
                "Channel Handle": "youtube_channel_handle",
                "Channel ID": "youtube_channel_id",
            },
            "required_fields": ["Channel Name", "Channel Handle", "Channel ID"],
            "custom_handler": True,  # Use custom import logic for YouTube channels
        },
    }

    @classmethod
    def import_from_csv(cls, file_content, asset_type):
        """
        Main entry point for CSV import.

        Args:
            file_content: String content of CSV file
            asset_type: Type of asset (e.g., 'facebook_pages')

        Returns:
            dict with success, imported_count, skipped_count, error_count, errors
        """
        if asset_type not in cls.ASSET_IMPORT_CONFIGS:
            return {
                "success": False,
                "imported_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"line": 0, "message": f"Unknown asset type: {asset_type}"}],
            }

        config = cls.ASSET_IMPORT_CONFIGS[asset_type]

        # Use custom handler for teams
        if config.get("custom_handler") and asset_type == "teams":
            return cls.import_teams_from_csv(file_content)

        # Use custom handler for instagram_accounts
        if config.get("custom_handler") and asset_type == "instagram_accounts":
            return cls.import_instagram_accounts_from_csv(file_content)

        # Use custom handler for youtube_channels
        if config.get("custom_handler") and asset_type == "youtube_channels":
            return cls.import_youtube_channels_from_csv(file_content)

        # Use custom handler for facebook_pages
        if config.get("custom_handler") and asset_type == "facebook_pages":
            return cls.import_facebook_pages_from_csv(file_content)

        # Use custom handler for facebook_ad_accounts
        if config.get("custom_handler") and asset_type == "facebook_ad_accounts":
            return cls.import_facebook_ad_accounts_from_csv(file_content)

        # Parse CSV
        records, parse_errors = cls.parse_csv(file_content, asset_type)

        if not records and parse_errors:
            return {
                "success": False,
                "imported_count": 0,
                "skipped_count": 0,
                "error_count": len(parse_errors),
                "errors": parse_errors,
            }

        # Get existing IDs for duplicate detection
        existing_ids = cls.get_existing_ids(asset_type)

        # Import records
        result = cls.bulk_import_records(records, asset_type, existing_ids)
        result["errors"].extend(parse_errors)
        result["error_count"] = len(result["errors"])

        return result

    @classmethod
    def parse_csv(cls, file_content, asset_type):
        """
        Parse and validate CSV content.

        Args:
            file_content: String content of CSV file
            asset_type: Type of asset

        Returns:
            tuple of (records_list, errors_list)
        """
        config = cls.ASSET_IMPORT_CONFIGS[asset_type]
        field_mapping = config["field_mapping"]
        required_fields = config["required_fields"]

        records = []
        errors = []

        # Handle UTF-8 BOM
        if file_content.startswith("\ufeff"):
            file_content = file_content[1:]

        try:
            reader = csv.DictReader(io.StringIO(file_content))

            # Validate header has required fields
            if reader.fieldnames is None:
                errors.append({"line": 1, "message": "CSV file is empty or has no header row"})
                return records, errors

            # Check for required CSV columns
            missing_columns = []
            for csv_col in required_fields:
                if csv_col not in reader.fieldnames:
                    missing_columns.append(csv_col)

            if missing_columns:
                errors.append(
                    {
                        "line": 1,
                        "message": f"Missing required columns: {', '.join(missing_columns)}",
                    }
                )
                return records, errors

            # Parse each row
            for line_num, row in enumerate(reader, start=2):  # Start at 2 (header is line 1)
                record = {}
                row_errors = []

                # Map CSV columns to ES fields
                for csv_col, es_field in field_mapping.items():
                    value = row.get(csv_col, "").strip()

                    if csv_col in required_fields and not value:
                        row_errors.append(f"Missing required field: {csv_col}")
                    else:
                        record[es_field] = value

                if row_errors:
                    errors.append({"line": line_num, "message": "; ".join(row_errors)})
                else:
                    record["_line_num"] = line_num  # Track for error reporting
                    records.append(record)

        except csv.Error:
            logging.warning("AssetImportService: CSV parsing failed", exc_info=True)
            errors.append({"line": 0, "message": "CSV parsing error: the file could not be read as CSV"})

        return records, errors

    @classmethod
    def get_existing_ids(cls, asset_type):
        """
        Get set of existing unique field values for duplicate detection.

        Args:
            asset_type: Type of asset

        Returns:
            set of existing unique field values
        """
        config = cls.ASSET_IMPORT_CONFIGS[asset_type]
        index_name = config["index"]
        unique_field = config["unique_field"]

        existing_ids = set()

        es = get_elasticsearch_client()
        if es is None:
            return existing_ids

        try:
            # Use scroll API to get all documents
            query = {
                "query": {"match_all": {}},
                "_source": [unique_field],
                "size": 1000,
            }

            result = es.search(index=index_name, body=query, scroll="2m")
            scroll_id = result.get("_scroll_id")

            while True:
                hits = result["hits"]["hits"]
                if not hits:
                    break

                for hit in hits:
                    value = hit["_source"].get(unique_field)
                    if value:
                        existing_ids.add(str(value))

                # Get next batch
                result = es.scroll(scroll_id=scroll_id, scroll="2m")

            # Clear scroll
            if scroll_id:
                es.clear_scroll(scroll_id=scroll_id)

        except Exception as e:
            import logging

            logging.warning(f"Failed to get existing IDs for {asset_type}: {e}")

        return existing_ids

    @classmethod
    def bulk_import_records(cls, records, asset_type, existing_ids):
        """
        Import records with duplicate detection.

        Args:
            records: List of record dicts to import
            asset_type: Type of asset
            existing_ids: Set of existing unique field values

        Returns:
            dict with success, imported_count, skipped_count, errors
        """
        config = cls.ASSET_IMPORT_CONFIGS[asset_type]
        index_name = config["index"]
        unique_field = config["unique_field"]

        imported_count = 0
        skipped_count = 0
        errors = []

        es = get_elasticsearch_client()
        if es is None:
            return {
                "success": False,
                "imported_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"line": 0, "message": "Unable to connect to Elasticsearch"}],
            }

        for record in records:
            line_num = record.pop("_line_num", 0)
            unique_value = str(record.get(unique_field, ""))

            # Check for duplicate
            if unique_value in existing_ids:
                skipped_count += 1
                errors.append(
                    {
                        "line": line_num,
                        "message": f"Organization Title {unique_value} was a duplicate",
                    }
                )
                continue

            try:
                # Add timestamp
                record["@timestamp"] = datetime.utcnow().isoformat() + "Z"

                # Insert document
                es.index(index=index_name, document=record, refresh="wait_for")
                imported_count += 1

                # Add to existing_ids to prevent duplicates within same import
                existing_ids.add(unique_value)

            except Exception:  # noqa: BLE001 — one row failing must not abort the import
                logging.exception("AssetImportService.bulk_import_records: index failed for %s line %s", asset_type, line_num)
                errors.append({"line": line_num, "message": "Failed to import: could not save this row"})

        return {
            "success": True,
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "error_count": len(errors),
            "errors": errors,
        }

    @classmethod
    def _get_existing_team_by_title(cls, team_title):
        """
        Get an existing team document by team_title.

        Args:
            team_title: The team title to search for

        Returns:
            dict with 'esid' and 'source' keys, or None if not found
        """
        es = get_elasticsearch_client()
        if es is None:
            return None

        try:
            query = {"query": {"term": {"team_title": team_title}}, "size": 1}

            result = es.search(index="de_asset_teams", body=query)

            if result["hits"]["total"]["value"] > 0:
                hit = result["hits"]["hits"][0]
                return {"esid": hit["_id"], "source": hit["_source"]}
        except Exception as e:
            import logging

            logging.warning(f"Failed to get existing team {team_title}: {e}")

        return None

    @classmethod
    def _compare_team_documents(cls, old_doc, new_doc):
        """
        Compare two team documents and return list of changed fields.

        Args:
            old_doc: Existing team document
            new_doc: New team document from CSV

        Returns:
            list of field names that changed
        """
        changed_fields = []

        # Fields to compare (exclude @timestamp)
        comparable_fields = [
            "team_name",
            "organization_title",
            "team_language",
            "team_region",
            "team_country_text",
            "team_city",
            "team_ga4_properties",
            "team_facebook_page_id",
            "team_instagram_profile_esids",
            "team_disciple_tools_instance",
            "echo_tenant_title",
            "echo_tenant_esid",
            "team_youtube_channel_handle",
            "team_youtube_channel_handle_esids",
            "ig_account_id",
        ]

        for field in comparable_fields:
            old_value = old_doc.get(field)
            new_value = new_doc.get(field)

            # Normalize None and empty string
            if old_value == "" or old_value is None:
                old_value = None
            if new_value == "" or new_value is None:
                new_value = None

            # For list fields, compare as sets
            if isinstance(old_value, list) and isinstance(new_value, list):
                if set(old_value) != set(new_value):
                    changed_fields.append(field)
            elif old_value != new_value:
                changed_fields.append(field)

        return changed_fields

    @classmethod
    def import_teams_from_csv(cls, file_content):
        """
        Custom import handler for teams with Echo Tenant matching and list field support.

        Args:
            file_content: String content of CSV file

        Returns:
            dict with success, imported_count, skipped_count, error_count, errors
        """
        records = []
        errors = []
        imported_count = 0
        updated_count = 0
        skipped_count = 0

        # Handle UTF-8 BOM
        if file_content.startswith("\ufeff"):
            file_content = file_content[1:]

        try:
            reader = csv.DictReader(io.StringIO(file_content))

            # Validate header
            if reader.fieldnames is None:
                return {
                    "success": False,
                    "imported_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [{"line": 1, "message": "CSV file is empty or has no header row"}],
                }

            # Check for required column
            if "Team ID" not in reader.fieldnames:
                return {
                    "success": False,
                    "imported_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [{"line": 1, "message": "Missing required column: Team ID"}],
                }

            # Get existing team IDs
            existing_team_ids = cls.get_existing_ids("teams")

            # Get Echo Tenants for matching
            echo_tenants = cls._get_echo_tenants()

            # Get Instagram Accounts for matching
            ig_accounts = cls._get_instagram_accounts()

            # Get YouTube Channels for matching
            youtube_channels = cls._get_youtube_channels()

            # Connect to ES
            es = get_elasticsearch_client()
            if es is None:
                return {
                    "success": False,
                    "imported_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [{"line": 0, "message": "Unable to connect to Elasticsearch"}],
                }

            # Parse each row
            for line_num, row in enumerate(reader, start=2):
                team_id = row.get("Team ID", "").strip()

                # Skip empty rows
                if not team_id:
                    continue

                # Build team document
                team_doc = {
                    "team_title": team_id,
                    "team_name": row.get("Team Name", "").strip(),
                    "team_city": row.get("Team City", "").strip(),
                    "@timestamp": datetime.utcnow().isoformat() + "Z",
                }

                # Add simple text fields
                org_title = row.get("Team Org", "").strip()
                if org_title:
                    team_doc["organization_title"] = org_title

                team_language = row.get("Team Language", "").strip()
                if team_language:
                    team_doc["team_language"] = team_language

                team_region = row.get("Team Region", "").strip()
                if team_region:
                    team_doc["team_region"] = team_region

                team_country = row.get("Team Country", "").strip()
                if team_country:
                    team_doc["team_country_text"] = team_country

                dt_instance = row.get("Team Disciple Tools Instance", "").strip()
                if dt_instance:
                    team_doc["team_disciple_tools_instance"] = dt_instance

                # Support both old and new column names for backward compatibility
                youtube_handles = (
                    row.get("Team YouTube Channel Handles", "").strip() or row.get("Team YouTube Channel", "").strip()
                )
                if youtube_handles:
                    # Parse comma/newline-separated YouTube handles
                    handles = [h.strip() for h in youtube_handles.replace("\n", ",").split(",") if h.strip()]
                    # Lookup ESIDs for each handle
                    esids = []
                    valid_handles = []

                    for handle in handles:
                        matched_channel = cls._find_youtube_channel_by_handle(handle, youtube_channels)
                        if matched_channel:
                            esids.append(matched_channel["esid"])
                            valid_handles.append(matched_channel["handle"])
                        else:
                            errors.append(
                                {
                                    "line": line_num,
                                    "message": f"YouTube channel handle '{handle}' not found in YouTube Channels assets",
                                }
                            )

                    if esids:
                        team_doc["team_youtube_channel_handle_esids"] = esids
                        team_doc["team_youtube_channel_handle"] = valid_handles

                # Parse list fields (GA4 Properties)
                ga4_props = row.get("Team GA4 Properties", "").strip()
                if ga4_props:
                    team_doc["team_ga4_properties"] = [p.strip() for p in ga4_props.replace("\n", ",").split(",") if p.strip()]

                # Parse list fields (Facebook Pages)
                fb_pages = row.get("Team Facebook Page", "").strip()
                if fb_pages:
                    team_doc["team_facebook_page_id"] = [
                        p.strip() for p in fb_pages.replace("\n", ",").split(",") if p.strip()
                    ]

                # Parse list fields (Instagram Profiles)
                ig_profiles = row.get("Team Instagram Profile", "").strip()
                if ig_profiles:
                    # Parse comma/newline-separated Instagram Account IDs
                    account_ids = [p.strip() for p in ig_profiles.replace("\n", ",").split(",") if p.strip()]
                    # Lookup ESIDs for each account ID
                    esids = []
                    valid_account_ids = []
                    for account_id in account_ids:
                        matched_account = cls._find_instagram_account_by_id(account_id, ig_accounts)
                        if matched_account:
                            esids.append(matched_account["esid"])
                            valid_account_ids.append(matched_account["account_id"])
                        else:
                            errors.append(
                                {
                                    "line": line_num,
                                    "message": f"Imported Instagram profile ID {account_id} does not match any IDs in Instagram Assets (de_asset_ig_accounts)",
                                }
                            )
                    if esids:
                        team_doc["team_instagram_profile_esids"] = esids
                        team_doc["ig_account_id"] = valid_account_ids

                # Match Echo Tenant by subdomain
                echo_url = row.get("Team Echo Instance", "").strip()
                if echo_url:
                    matched_tenant = cls._match_echo_tenant_by_url(echo_url, echo_tenants)
                    if matched_tenant:
                        team_doc["echo_tenant_title"] = matched_tenant["title"]
                        if matched_tenant.get("esid"):
                            team_doc["echo_tenant_esid"] = matched_tenant["esid"]
                    else:
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"No Echo Tenant found matching URL: {echo_url}, imported without Echo Tenant",
                            }
                        )

                # Check if team already exists
                if team_id in existing_team_ids:
                    # Fetch existing team
                    existing_team = cls._get_existing_team_by_title(team_id)

                    if existing_team:
                        # Compare documents
                        changed_fields = cls._compare_team_documents(existing_team["source"], team_doc)

                        if changed_fields:
                            # Update the team
                            try:
                                es.update(
                                    index="de_asset_teams",
                                    id=existing_team["esid"],
                                    body={"doc": team_doc},
                                    refresh="wait_for",
                                )
                                updated_count += 1

                                # Log what was updated
                                field_names = ", ".join(changed_fields)
                                errors.append(
                                    {
                                        "line": line_num,
                                        "message": f"Updated Team ID: {team_id} - Changed fields: {field_names}",
                                    }
                                )
                            except Exception:  # noqa: BLE001 — one row failing must not abort the import
                                logging.exception(
                                    "AssetImportService.import_teams_from_csv: update failed for team %s", team_id
                                )
                                errors.append(
                                    {
                                        "line": line_num,
                                        "message": f"Failed to update team {team_id}",
                                    }
                                )
                        else:
                            # No changes, skip
                            skipped_count += 1
                            errors.append(
                                {
                                    "line": line_num,
                                    "message": f"Team ID: {team_id} already exists with identical data, skipped",
                                }
                            )
                    else:
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"Team ID: {team_id} found in index but could not fetch document, skipping",
                            }
                        )
                        skipped_count += 1
                else:
                    # Import new team
                    try:
                        es.index(
                            index="de_asset_teams",
                            document=team_doc,
                            refresh="wait_for",
                        )
                        imported_count += 1
                        existing_team_ids.add(team_id)
                    except Exception:  # noqa: BLE001 — one row failing must not abort the import
                        logging.exception("AssetImportService.import_teams_from_csv: index failed for team %s", team_id)
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"Failed to import team {team_id}",
                            }
                        )

        except csv.Error:
            logging.warning("AssetImportService: CSV parsing failed", exc_info=True)
            errors.append({"line": 0, "message": "CSV parsing error: the file could not be read as CSV"})

        return {
            "success": True,
            "imported_count": imported_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "error_count": len(errors),
            "errors": errors,
        }

    @classmethod
    def _get_echo_tenants(cls):
        """
        Fetch all Echo Tenants for subdomain matching.

        Returns:
            list of dicts with title, subdomain, and esid
        """
        tenants = []
        es = get_elasticsearch_client()
        if es is None:
            return tenants

        try:
            query = {
                "query": {"match_all": {}},
                "_source": ["echo_tenant_title", "echo_tenant_subdomain"],
                "size": 1000,
            }

            result = es.search(index="de_asset_echo_tenants", body=query)

            for hit in result["hits"]["hits"]:
                source = hit["_source"]
                tenants.append(
                    {
                        "title": source.get("echo_tenant_title", ""),
                        "subdomain": source.get("echo_tenant_subdomain", ""),
                        "esid": hit["_id"],
                    }
                )

        except Exception as e:
            import logging

            logging.warning(f"Failed to fetch Echo Tenants: {e}")

        return tenants

    @classmethod
    def _match_echo_tenant_by_url(cls, url, echo_tenants):
        """
        Match an Echo Tenant by extracting subdomain from URL.

        Args:
            url: URL string (e.g., 'https://subdomain.echo.com' or just 'subdomain')
            echo_tenants: list of tenant dicts with subdomain field

        Returns:
            matched tenant dict or None
        """
        if not url or not echo_tenants:
            return None

        # Extract subdomain from URL
        url = url.lower().strip()

        # Remove protocol
        if "://" in url:
            url = url.split("://", 1)[1]

        # Remove path
        if "/" in url:
            url = url.split("/")[0]

        # Extract subdomain (first part before first dot)
        subdomain = url.split(".")[0]

        # Match against tenants
        for tenant in echo_tenants:
            tenant_subdomain = tenant.get("subdomain", "").lower().strip()
            if tenant_subdomain and tenant_subdomain == subdomain:
                return tenant

        return None

    @classmethod
    def _get_instagram_accounts(cls):
        """
        Fetch all Instagram Accounts for ID matching.

        Returns:
            list of dicts with account_id and esid
        """
        accounts = []
        es = get_elasticsearch_client()
        if es is None:
            return accounts

        try:
            query = {
                "query": {"match_all": {}},
                "_source": ["ig_account_id"],
                "size": 1000,
            }

            result = es.search(index="de_asset_ig_accounts", body=query)

            for hit in result["hits"]["hits"]:
                source = hit["_source"]
                accounts.append({"account_id": source.get("ig_account_id", ""), "esid": hit["_id"]})

        except Exception as e:
            import logging

            logging.warning(f"Failed to fetch Instagram Accounts: {e}")

        return accounts

    @classmethod
    def _find_instagram_account_by_id(cls, account_id, ig_accounts):
        """
        Find an Instagram Account by ig_account_id.

        Args:
            account_id: Instagram Account ID string (e.g., '17841405309211844')
            ig_accounts: list of account dicts with account_id field

        Returns:
            matched account dict or None
        """
        if not account_id or not ig_accounts:
            return None

        account_id = account_id.strip()

        # Match against accounts
        for account in ig_accounts:
            if account.get("account_id", "").strip() == account_id:
                return account

        return None

    @classmethod
    def import_instagram_accounts_from_csv(cls, file_content):
        """
        Custom import handler for Instagram accounts with duplicate detection and update.

        Args:
            file_content: String content of CSV file

        Returns:
            dict with success, imported_count, updated_count, skipped_count, error_count, errors
        """
        records = []
        errors = []
        imported_count = 0
        updated_count = 0

        # Handle UTF-8 BOM
        if file_content.startswith("\ufeff"):
            file_content = file_content[1:]

        try:
            reader = csv.DictReader(io.StringIO(file_content))

            # Validate header
            if reader.fieldnames is None:
                return {
                    "success": False,
                    "imported_count": 0,
                    "updated_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [{"line": 1, "message": "CSV file is empty or has no header row"}],
                }

            # Check for required columns
            required_columns = ["Instagram Account", "Account ID", "Facebook Page"]
            missing_columns = [col for col in required_columns if col not in reader.fieldnames]

            if missing_columns:
                return {
                    "success": False,
                    "imported_count": 0,
                    "updated_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [
                        {
                            "line": 1,
                            "message": f"Missing required columns: {', '.join(missing_columns)}",
                        }
                    ],
                }

            # Parse each row
            for line_num, row in enumerate(reader, start=2):
                ig_account_title = row.get("Instagram Account", "").strip()
                ig_account_id = row.get("Account ID", "").strip()
                fb_page_id = row.get("Facebook Page", "").strip()
                fb_account_title = row.get("Meta Business Portfolio", "").strip()

                # Validate required fields
                if not ig_account_title or not ig_account_id or not fb_page_id:
                    errors.append(
                        {
                            "line": line_num,
                            "message": "Missing required field(s): Instagram Account, Account ID, or Facebook Page",
                        }
                    )
                    continue

                record = {
                    "ig_account_title": ig_account_title,
                    "ig_account_id": ig_account_id,
                    "fb_page_id": fb_page_id,
                    "_line_num": line_num,
                }
                if fb_account_title:
                    record["fb_account_title"] = fb_account_title
                records.append(record)

        except csv.Error:
            logging.warning("AssetImportService: CSV parsing failed", exc_info=True)
            return {
                "success": False,
                "imported_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"line": 0, "message": "CSV parsing error: the file could not be read as CSV"}],
            }

        # Get Elasticsearch client
        es = get_elasticsearch_client()
        if es is None:
            return {
                "success": False,
                "imported_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"line": 0, "message": "Unable to connect to Elasticsearch"}],
            }

        # Process each record
        for record in records:
            line_num = record.pop("_line_num")
            ig_account_id = record["ig_account_id"]

            try:
                # The "Facebook Page" column is filled in by hand and the resolver
                # has always accepted a page NAME there. Resolve it to the page doc
                # FIRST and keep that doc's own numeric id, so a display name never
                # lands in an id field. Resolving before the comparison
                # below also makes a re-import repair rows an earlier version wrote
                # with a name.
                fb_page_esid, canonical_page_id = cls._resolve_fb_page(record.get("fb_page_id"))
                if canonical_page_id:
                    record["fb_page_id"] = canonical_page_id
                elif record.get("fb_page_id"):
                    # Unresolvable: still import, but say so. A page id we cannot
                    # tie to a page doc matches nothing downstream and must not
                    # fail silently (CLAUDE.md, identifier discipline).
                    errors.append(
                        {
                            "line": line_num,
                            "message": (
                                f"Facebook Page {record['fb_page_id']!r} matched no page in "
                                f"de_asset_fb_pages - stored as given, and IG causation "
                                f"reporting will not work for this account until it is fixed"
                            ),
                        }
                    )

                # Check if Instagram account already exists
                existing_doc = cls._get_existing_ig_account_by_id(ig_account_id)

                if existing_doc:
                    # Account exists - check if update is needed
                    needs_update = False
                    old_doc = existing_doc["source"]
                    esid = existing_doc["esid"]

                    # Compare fields
                    if old_doc.get("ig_account_title") != record["ig_account_title"]:
                        needs_update = True
                    if old_doc.get("fb_page_id") != record["fb_page_id"]:
                        needs_update = True
                    if old_doc.get("fb_account_title") != record.get("fb_account_title"):
                        needs_update = True
                    # Also force update if ESID links are missing (backfill from earlier imports)
                    if record.get("fb_page_id") and not old_doc.get("fb_page_esid"):
                        needs_update = True
                    if record.get("fb_account_title") and not old_doc.get("fb_account_esid"):
                        needs_update = True

                    if needs_update:
                        # Update existing document
                        update_doc = {**old_doc, **record}
                        update_doc["@timestamp"] = datetime.utcnow().isoformat() + "Z"

                        # fb_page_esid comes from the same resolution that
                        # canonicalised fb_page_id above
                        if fb_page_esid:
                            update_doc["fb_page_esid"] = fb_page_esid

                        # Re-resolve fb_account_esid if title changed
                        if update_doc.get("fb_account_title"):
                            fb_account_esid = cls._get_fb_account_esid_by_title(update_doc["fb_account_title"])
                            if fb_account_esid:
                                update_doc["fb_account_esid"] = fb_account_esid

                        es.index(
                            index="de_asset_ig_accounts",
                            id=esid,
                            document=update_doc,
                            refresh="wait_for",
                        )
                        updated_count += 1
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"Instagram Account ID {ig_account_id} was a duplicate - updated",
                            }
                        )
                    else:
                        # No changes needed
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"Instagram Account ID {ig_account_id} was a duplicate - no changes needed",
                            }
                        )
                else:
                    # New account - create it
                    # Set default values
                    record["ig_account_status"] = "Active"
                    record["ig_account_username"] = record.get("ig_account_title", "").lower().replace(" ", "")
                    record["@timestamp"] = datetime.utcnow().isoformat() + "Z"

                    # fb_page_esid comes from the same resolution that
                    # canonicalised fb_page_id above
                    if fb_page_esid:
                        record["fb_page_esid"] = fb_page_esid

                    # Lookup Facebook Account ESID from Meta Business Portfolio title
                    if record.get("fb_account_title"):
                        fb_account_esid = cls._get_fb_account_esid_by_title(record["fb_account_title"])
                        if fb_account_esid:
                            record["fb_account_esid"] = fb_account_esid

                    es.index(
                        index="de_asset_ig_accounts",
                        document=record,
                        refresh="wait_for",
                    )
                    imported_count += 1

            except Exception:  # noqa: BLE001 — one row failing must not abort the import
                logging.exception("AssetImportService: import/update failed at line %s", line_num)
                errors.append({"line": line_num, "message": "Failed to import/update: could not save this row"})

        return {
            "success": True,
            "imported_count": imported_count,
            "updated_count": updated_count,
            "skipped_count": 0,
            "error_count": len([e for e in errors if "Failed" in e["message"]]),
            "errors": errors,
        }

    @classmethod
    def _get_existing_ig_account_by_id(cls, ig_account_id):
        """
        Get an existing Instagram account document by ig_account_id.

        Args:
            ig_account_id: The Instagram account ID to search for

        Returns:
            dict with 'esid' and 'source' keys, or None if not found
        """
        es = get_elasticsearch_client()
        if es is None:
            return None

        try:
            # Try string value first, then integer — ig_account_id may be stored as either
            # type depending on how the document was originally created.
            values_to_try = [ig_account_id]
            try:
                values_to_try.append(int(ig_account_id))
            except (ValueError, TypeError):
                pass

            for value in values_to_try:
                query = {"query": {"term": {"ig_account_id": value}}, "size": 1}
                result = es.search(index="de_asset_ig_accounts", body=query)
                if result["hits"]["total"]["value"] > 0:
                    hit = result["hits"]["hits"][0]
                    return {"esid": hit["_id"], "source": hit["_source"]}
        except Exception as e:
            import logging

            logging.warning(f"Failed to get existing Instagram account {ig_account_id}: {e}")

        return None

    @classmethod
    def _get_fb_account_esid_by_title(cls, fb_account_title):
        """
        Get Facebook Account ESID by fb_account_title.

        Args:
            fb_account_title: The Facebook Account title to search for

        Returns:
            ESID string or None if not found
        """
        es = get_elasticsearch_client()
        if es is None:
            return None

        try:
            # bare field (explicit keyword post-reindex).
            query = {
                "query": {"term": {"fb_account_title": fb_account_title}},
                "size": 1,
            }

            result = es.search(index="de_asset_fb_accounts", body=query)

            if result["hits"]["total"]["value"] > 0:
                return result["hits"]["hits"][0]["_id"]
        except Exception as e:
            import logging

            logging.warning(f"Failed to get Facebook Account ESID for '{fb_account_title}': {e}")

        return None

    @classmethod
    def _resolve_fb_page(cls, value):
        """Resolve a CSV "Facebook Page" cell to ``(esid, canonical_fb_page_id)``.

        The cell may hold Meta's numeric page id OR the page's display name --
        the name fallback below has always been deliberate, because operators
        fill this column by hand. What was NOT deliberate is that the caller
        then stored the cell verbatim in ``fb_page_id``: on staging 19 of 34
        ``de_asset_ig_accounts`` docs carry a page NAME in that field
        . Nothing raised -- the esid resolved correctly, the UI looked
        right -- but a name can never equal a page probe's ``asset_id``, so
        ``probe_assets``' ``caused_by`` shield was inert for every one of them
        and each IG failure was reported as an independent second fault.

        So return the resolved doc's OWN id alongside the esid, and let the
        caller store that. Both values are ``None`` when nothing matched.
        """
        es = get_elasticsearch_client()
        value = str(value or "").strip()
        if es is None or not value:
            return None, None

        try:
            # Try numeric fb_page_id first, then fall back to fb_page_name
            # (CSV may store the page name instead of numeric ID). :
            # fb_page_name is deliberately ``text`` in the asset schema, so
            # exact matching keeps the .keyword multi-field IF present; the
            # bare-field probe runs first for a keyword-mapped cohort.
            for field in ("fb_page_id", "fb_page_name.keyword", "fb_page_name"):
                query = {"query": {"term": {field: value}}, "size": 1}
                result = es.search(index="de_asset_fb_pages", body=query)
                if result["hits"]["total"]["value"] > 0:
                    hit = result["hits"]["hits"][0]
                    canonical = str((hit.get("_source") or {}).get("fb_page_id") or "").strip()
                    return hit["_id"], (canonical or None)
        except Exception as e:
            import logging

            logging.warning(f"Failed to resolve Facebook Page for {value}: {e}")

        return None, None

    @classmethod
    def _get_existing_fb_ad_account_by_id(cls, fb_ad_account_id):
        """
        Get an existing Facebook Ad Account document by fb_ad_account_id.

        Returns:
            dict with 'esid' and 'source' keys, or None if not found
        """
        es = get_elasticsearch_client()
        if es is None:
            return None

        try:
            query = {
                "query": {"term": {"fb_ad_account_id": fb_ad_account_id}},
                "size": 1,
            }
            result = es.search(index="de_asset_fb_ad_accounts", body=query)
            if result["hits"]["total"]["value"] > 0:
                hit = result["hits"]["hits"][0]
                return {"esid": hit["_id"], "source": hit["_source"]}
        except Exception as e:
            import logging

            logging.warning(f"Failed to get existing Facebook Ad Account {fb_ad_account_id}: {e}")

        return None

    @classmethod
    def _get_fb_app_esid_by_title(cls, fb_app_title):
        """
        Get Facebook App ESID by fb_app_title.

        Returns:
            ESID string or None if not found
        """
        es = get_elasticsearch_client()
        if es is None:
            return None

        try:
            # bare field (explicit keyword post-reindex).
            query = {
                "query": {"term": {"fb_app_title": fb_app_title}},
                "size": 1,
            }
            result = es.search(index="de_asset_fb_apps", body=query)
            if result["hits"]["total"]["value"] > 0:
                return result["hits"]["hits"][0]["_id"]
        except Exception as e:
            import logging

            logging.warning(f"Failed to get Facebook App ESID for '{fb_app_title}': {e}")

        return None

    @classmethod
    def _get_fb_app_user_esid_by_title(cls, fb_app_user_title):
        """
        Get Facebook App User ESID by fb_app_user_title.

        Returns:
            ESID string or None if not found
        """
        es = get_elasticsearch_client()
        if es is None:
            return None

        try:
            # bare field (explicit keyword post-reindex).
            query = {
                "query": {"term": {"fb_app_user_title": fb_app_user_title}},
                "size": 1,
            }
            result = es.search(index="de_asset_fb_app_users", body=query)
            if result["hits"]["total"]["value"] > 0:
                return result["hits"]["hits"][0]["_id"]
        except Exception as e:
            import logging

            logging.warning(f"Failed to get Facebook App User ESID for '{fb_app_user_title}': {e}")

        return None

    @classmethod
    def import_facebook_ad_accounts_from_csv(cls, file_content):
        """
        Custom import handler for Facebook Ad Accounts with duplicate detection and update.

        CSV columns: Ad Account Name, Ad Account ID (required),
                     Facebook App, Facebook App User, Meta Business Portfolio (optional)

        Returns:
            dict with success, imported_count, updated_count, skipped_count, error_count, errors
        """
        records = []
        errors = []
        imported_count = 0
        updated_count = 0

        # Handle UTF-8 BOM
        if file_content.startswith("\ufeff"):
            file_content = file_content[1:]

        try:
            reader = csv.DictReader(io.StringIO(file_content))

            if reader.fieldnames is None:
                return {
                    "success": False,
                    "imported_count": 0,
                    "updated_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [{"line": 1, "message": "CSV file is empty or has no header row"}],
                }

            required_columns = ["Ad Account Name", "Ad Account ID"]
            missing_columns = [col for col in required_columns if col not in reader.fieldnames]

            if missing_columns:
                return {
                    "success": False,
                    "imported_count": 0,
                    "updated_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [
                        {
                            "line": 1,
                            "message": f"Missing required columns: {', '.join(missing_columns)}",
                        }
                    ],
                }

            for line_num, row in enumerate(reader, start=2):
                ad_account_name = row.get("Ad Account Name", "").strip()
                ad_account_id = row.get("Ad Account ID", "").strip()
                fb_app_title = row.get("Facebook App", "").strip()
                fb_app_user_title = row.get("Facebook App User", "").strip()
                fb_account_title = row.get("Meta Business Portfolio", "").strip()

                if not ad_account_name or not ad_account_id:
                    errors.append(
                        {
                            "line": line_num,
                            "message": "Missing required field(s): Ad Account Name or Ad Account ID",
                        }
                    )
                    continue

                # Strip act_ prefix (consistent with manual entry handler)
                if ad_account_id.lower().startswith("act_"):
                    ad_account_id = ad_account_id[4:]

                record = {
                    "fb_ad_account_title": ad_account_name,
                    "fb_ad_account_id": ad_account_id,
                    "_line_num": line_num,
                }
                if fb_app_title:
                    record["fb_app_title"] = fb_app_title
                if fb_app_user_title:
                    record["fb_app_user_title"] = fb_app_user_title
                if fb_account_title:
                    record["fb_account_title"] = fb_account_title
                records.append(record)

        except csv.Error:
            logging.warning("AssetImportService: CSV parsing failed", exc_info=True)
            return {
                "success": False,
                "imported_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"line": 0, "message": "CSV parsing error: the file could not be read as CSV"}],
            }

        es = get_elasticsearch_client()
        if es is None:
            return {
                "success": False,
                "imported_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"line": 0, "message": "Unable to connect to Elasticsearch"}],
            }

        for record in records:
            line_num = record.pop("_line_num")
            fb_ad_account_id = record["fb_ad_account_id"]

            try:
                existing_doc = cls._get_existing_fb_ad_account_by_id(fb_ad_account_id)

                if existing_doc:
                    needs_update = False
                    old_doc = existing_doc["source"]
                    esid = existing_doc["esid"]

                    if old_doc.get("fb_ad_account_title") != record["fb_ad_account_title"]:
                        needs_update = True
                    if record.get("fb_app_title") and old_doc.get("fb_app_title") != record.get("fb_app_title"):
                        needs_update = True
                    if record.get("fb_app_user_title") and old_doc.get("fb_app_user_title") != record.get("fb_app_user_title"):
                        needs_update = True
                    if record.get("fb_account_title") and old_doc.get("fb_account_title") != record.get("fb_account_title"):
                        needs_update = True
                    # Force update if ESID links are missing (backfill)
                    if record.get("fb_app_title") and not old_doc.get("fb_app_esid"):
                        needs_update = True
                    if record.get("fb_app_user_title") and not old_doc.get("fb_app_user_esid"):
                        needs_update = True
                    if record.get("fb_account_title") and not old_doc.get("fb_account_esid"):
                        needs_update = True

                    if needs_update:
                        update_doc = {**old_doc, **record}
                        update_doc["@timestamp"] = datetime.utcnow().isoformat() + "Z"

                        if update_doc.get("fb_app_title"):
                            fb_app_esid = cls._get_fb_app_esid_by_title(update_doc["fb_app_title"])
                            if fb_app_esid:
                                update_doc["fb_app_esid"] = fb_app_esid

                        if update_doc.get("fb_app_user_title"):
                            fb_app_user_esid = cls._get_fb_app_user_esid_by_title(update_doc["fb_app_user_title"])
                            if fb_app_user_esid:
                                update_doc["fb_app_user_esid"] = fb_app_user_esid

                        if update_doc.get("fb_account_title"):
                            fb_account_esid = cls._get_fb_account_esid_by_title(update_doc["fb_account_title"])
                            if fb_account_esid:
                                update_doc["fb_account_esid"] = fb_account_esid

                        es.index(
                            index="de_asset_fb_ad_accounts",
                            id=esid,
                            document=update_doc,
                            refresh="wait_for",
                        )
                        updated_count += 1
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"Ad Account ID {fb_ad_account_id} was a duplicate - updated",
                            }
                        )
                    else:
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"Ad Account ID {fb_ad_account_id} was a duplicate - no changes needed",
                            }
                        )
                else:
                    record["@timestamp"] = datetime.utcnow().isoformat() + "Z"

                    if record.get("fb_app_title"):
                        fb_app_esid = cls._get_fb_app_esid_by_title(record["fb_app_title"])
                        if fb_app_esid:
                            record["fb_app_esid"] = fb_app_esid

                    if record.get("fb_app_user_title"):
                        fb_app_user_esid = cls._get_fb_app_user_esid_by_title(record["fb_app_user_title"])
                        if fb_app_user_esid:
                            record["fb_app_user_esid"] = fb_app_user_esid

                    if record.get("fb_account_title"):
                        fb_account_esid = cls._get_fb_account_esid_by_title(record["fb_account_title"])
                        if fb_account_esid:
                            record["fb_account_esid"] = fb_account_esid

                    es.index(
                        index="de_asset_fb_ad_accounts",
                        document=record,
                        refresh="wait_for",
                    )
                    imported_count += 1

            except Exception:  # noqa: BLE001 — one row failing must not abort the import
                logging.exception("AssetImportService: import/update failed at line %s", line_num)
                errors.append({"line": line_num, "message": "Failed to import/update: could not save this row"})

        return {
            "success": True,
            "imported_count": imported_count,
            "updated_count": updated_count,
            "skipped_count": 0,
            "error_count": len([e for e in errors if "Failed" in e["message"]]),
            "errors": errors,
        }

    @classmethod
    def import_youtube_channels_from_csv(cls, file_content):
        """
        Custom import handler for YouTube channels with duplicate detection and update.

        Args:
            file_content: String content of CSV file

        Returns:
            dict with success, imported_count, updated_count, skipped_count, error_count, errors
        """
        records = []
        errors = []
        imported_count = 0
        updated_count = 0
        skipped_count = 0

        # Handle UTF-8 BOM
        if file_content.startswith("\ufeff"):
            file_content = file_content[1:]

        try:
            reader = csv.DictReader(io.StringIO(file_content))

            # Validate header
            if reader.fieldnames is None:
                return {
                    "success": False,
                    "imported_count": 0,
                    "updated_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [{"line": 1, "message": "CSV file is empty or has no header row"}],
                }

            # Check for required columns
            required_columns = ["Channel Name", "Channel Handle", "Channel ID"]
            missing_columns = [col for col in required_columns if col not in reader.fieldnames]

            if missing_columns:
                return {
                    "success": False,
                    "imported_count": 0,
                    "updated_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [
                        {
                            "line": 1,
                            "message": f"Missing required columns: {', '.join(missing_columns)}",
                        }
                    ],
                }

            # Parse each row
            for line_num, row in enumerate(reader, start=2):
                channel_name = row.get("Channel Name", "").strip()
                channel_handle = row.get("Channel Handle", "").strip()
                channel_id = row.get("Channel ID", "").strip()

                # Validate required fields
                if not channel_name or not channel_handle or not channel_id:
                    errors.append(
                        {
                            "line": line_num,
                            "message": "Missing required field(s): Channel Name, Channel Handle, or Channel ID",
                        }
                    )
                    continue

                # Validate Channel ID format (should be UCxxxxxxxxxxxxxx - 24 characters starting with UC)
                if not channel_id.startswith("UC") or len(channel_id) != 24:
                    errors.append(
                        {
                            "line": line_num,
                            "message": f"Invalid Channel ID format: {channel_id} (should be 24 characters starting with UC)",
                        }
                    )
                    continue

                # Normalize channel handle to start with @
                if not channel_handle.startswith("@"):
                    channel_handle = "@" + channel_handle

                records.append(
                    {
                        "youtube_channel_title": channel_name,
                        "youtube_channel_handle": channel_handle,
                        "youtube_channel_id": channel_id,
                        "_line_num": line_num,
                    }
                )

        except csv.Error:
            logging.warning("AssetImportService: CSV parsing failed", exc_info=True)
            return {
                "success": False,
                "imported_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"line": 0, "message": "CSV parsing error: the file could not be read as CSV"}],
            }

        # Get Elasticsearch client
        es = get_elasticsearch_client()
        if es is None:
            return {
                "success": False,
                "imported_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"line": 0, "message": "Unable to connect to Elasticsearch"}],
            }

        # Process each record
        for record in records:
            line_num = record.pop("_line_num")
            channel_handle = record["youtube_channel_handle"]

            try:
                # Check if YouTube channel already exists
                existing_doc = cls._get_existing_youtube_channel_by_handle(channel_handle)

                if existing_doc:
                    # Channel exists - check if update is needed
                    needs_update = False
                    old_doc = existing_doc["source"]
                    esid = existing_doc["esid"]

                    # Compare fields
                    if old_doc.get("youtube_channel_title") != record["youtube_channel_title"]:
                        needs_update = True
                    if old_doc.get("youtube_channel_id") != record["youtube_channel_id"]:
                        needs_update = True

                    if needs_update:
                        # Update existing document
                        update_doc = {**old_doc, **record}
                        update_doc["@timestamp"] = datetime.utcnow().isoformat() + "Z"

                        es.index(
                            index="de_asset_youtube_channels",
                            id=esid,
                            document=update_doc,
                            refresh="wait_for",
                        )
                        updated_count += 1
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"YouTube Channel {channel_handle} was a duplicate - updated fields",
                            }
                        )
                    else:
                        # No changes needed
                        skipped_count += 1
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"YouTube Channel {channel_handle} was a duplicate - no changes needed",
                            }
                        )
                else:
                    # New channel - create it
                    record["@timestamp"] = datetime.utcnow().isoformat() + "Z"

                    es.index(
                        index="de_asset_youtube_channels",
                        document=record,
                        refresh="wait_for",
                    )
                    imported_count += 1

            except Exception:  # noqa: BLE001 — one row failing must not abort the import
                logging.exception("AssetImportService: import/update failed at line %s", line_num)
                errors.append({"line": line_num, "message": "Failed to import/update: could not save this row"})

        return {
            "success": True,
            "imported_count": imported_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "error_count": len([e for e in errors if "Failed" in e["message"]]),
            "errors": errors,
        }

    @classmethod
    def _get_existing_youtube_channel_by_handle(cls, youtube_channel_handle):
        """
        Get an existing YouTube channel document by youtube_channel_handle.

        Args:
            youtube_channel_handle: The YouTube channel handle to search for (e.g., @username)

        Returns:
            dict with 'esid' and 'source' keys, or None if not found
        """
        es = get_elasticsearch_client()
        if es is None:
            return None

        try:
            query = {
                # bare field (explicit keyword post-reindex).
                "query": {"term": {"youtube_channel_handle": youtube_channel_handle}},
                "size": 1,
            }

            result = es.search(index="de_asset_youtube_channels", body=query)

            if result["hits"]["total"]["value"] > 0:
                hit = result["hits"]["hits"][0]
                return {"esid": hit["_id"], "source": hit["_source"]}
        except Exception as e:
            import logging

            logging.warning(f"Failed to get existing YouTube channel {youtube_channel_handle}: {e}")

        return None

    @classmethod
    def _get_youtube_channels(cls):
        """
        Get all YouTube channels from de_asset_youtube_channels index.

        Returns:
            list: Array of dicts with 'esid', 'title', 'handle', and 'id' keys
        """
        es = get_elasticsearch_client()
        if es is None:
            return []

        try:
            result = es.search(index="de_asset_youtube_channels", query={"match_all": {}}, size=1000)
            channels = []
            for hit in result.get("hits", {}).get("hits", []):
                channels.append(
                    {
                        "esid": hit["_id"],
                        "title": hit["_source"].get("youtube_channel_title", "N/A"),
                        "handle": hit["_source"].get("youtube_channel_handle", "N/A"),
                        "id": hit["_source"].get("youtube_channel_id", "N/A"),
                    }
                )
            return channels
        except Exception:
            return []

    @classmethod
    def _find_youtube_channel_by_handle(cls, handle, youtube_channels):
        """
        Find a YouTube channel by its handle (case-insensitive).

        Args:
            handle: The handle to search for (e.g., @username)
            youtube_channels: List of YouTube channel dicts from _get_youtube_channels()

        Returns:
            dict with 'esid', 'title', 'handle', and 'id' keys, or None if not found
        """
        handle_normalized = handle.lower().strip()
        for channel in youtube_channels:
            if channel["handle"].lower() == handle_normalized:
                return channel
        return None

    @classmethod
    def _get_fb_accounts(cls):
        """
        Fetch all Facebook Accounts for title matching.

        Returns:
            list of dicts with title and esid
        """
        accounts = []
        es = get_elasticsearch_client()
        if es is None:
            return accounts

        try:
            query = {
                "query": {"match_all": {}},
                "_source": ["fb_account_title"],
                "size": 1000,
            }

            result = es.search(index="de_asset_fb_accounts", body=query)

            for hit in result["hits"]["hits"]:
                source = hit["_source"]
                accounts.append({"title": source.get("fb_account_title", ""), "esid": hit["_id"]})

        except Exception as e:
            import logging

            logging.warning(f"Failed to fetch Facebook Accounts: {e}")

        return accounts

    @classmethod
    def _find_fb_account_by_title(cls, title, fb_accounts):
        """
        Find a Facebook Account by fb_account_title (case-insensitive).

        Args:
            title: Account title string (e.g., 'My Business Portfolio')
            fb_accounts: list of account dicts with title and esid fields

        Returns:
            matched account dict or None
        """
        if not title or not fb_accounts:
            return None

        title_normalized = title.strip().lower()

        for account in fb_accounts:
            if account.get("title", "").strip().lower() == title_normalized:
                return account

        return None

    @classmethod
    def _get_existing_fb_page_by_id(cls, fb_page_id):
        """
        Get an existing Facebook Page document by fb_page_id.

        Args:
            fb_page_id: The Facebook Page ID to search for

        Returns:
            dict with 'esid' and 'source' keys, or None if not found
        """
        es = get_elasticsearch_client()
        if es is None:
            return None

        try:
            query = {"query": {"term": {"fb_page_id": fb_page_id}}, "size": 1}

            result = es.search(index="de_asset_fb_pages", body=query)

            if result["hits"]["total"]["value"] > 0:
                hit = result["hits"]["hits"][0]
                return {"esid": hit["_id"], "source": hit["_source"]}
        except Exception as e:
            import logging

            logging.warning(f"Failed to get existing Facebook Page {fb_page_id}: {e}")

        return None

    @classmethod
    def import_facebook_pages_from_csv(cls, file_content):
        """
        Custom import handler for Facebook Pages with Meta Business Portfolio
        (FB Account) lookup and duplicate detection/update.

        Args:
            file_content: String content of CSV file

        Returns:
            dict with success, imported_count, updated_count, skipped_count, error_count, errors
        """
        records = []
        errors = []
        imported_count = 0
        updated_count = 0
        skipped_count = 0

        # Handle UTF-8 BOM
        if file_content.startswith("\ufeff"):
            file_content = file_content[1:]

        try:
            reader = csv.DictReader(io.StringIO(file_content))

            # Validate header
            if reader.fieldnames is None:
                return {
                    "success": False,
                    "imported_count": 0,
                    "updated_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [{"line": 1, "message": "CSV file is empty or has no header row"}],
                }

            # Check for required columns
            required_columns = ["Page Name", "Page ID", "Page Slug"]
            missing_columns = [col for col in required_columns if col not in reader.fieldnames]

            if missing_columns:
                return {
                    "success": False,
                    "imported_count": 0,
                    "updated_count": 0,
                    "skipped_count": 0,
                    "error_count": 1,
                    "errors": [
                        {
                            "line": 1,
                            "message": f"Missing required columns: {', '.join(missing_columns)}",
                        }
                    ],
                }

            # Parse each row
            for line_num, row in enumerate(reader, start=2):
                fb_page_name = row.get("Page Name", "").strip()
                fb_page_id = row.get("Page ID", "").strip()
                fb_page_slug = row.get("Page Slug", "").strip()
                meta_business_portfolio = row.get("Meta Business Portfolio", "").strip()

                # Validate required fields
                if not fb_page_name or not fb_page_id or not fb_page_slug:
                    errors.append(
                        {
                            "line": line_num,
                            "message": "Missing required field(s): Page Name, Page ID, or Page Slug",
                        }
                    )
                    continue

                records.append(
                    {
                        "fb_page_name": fb_page_name,
                        "fb_page_id": fb_page_id,
                        "fb_page_slug": fb_page_slug,
                        "_meta_business_portfolio": meta_business_portfolio,
                        "_line_num": line_num,
                    }
                )

        except csv.Error:
            logging.warning("AssetImportService: CSV parsing failed", exc_info=True)
            return {
                "success": False,
                "imported_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"line": 0, "message": "CSV parsing error: the file could not be read as CSV"}],
            }

        # Get Elasticsearch client
        es = get_elasticsearch_client()
        if es is None:
            return {
                "success": False,
                "imported_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"line": 0, "message": "Unable to connect to Elasticsearch"}],
            }

        # Fetch Facebook Accounts for title matching (only if any row has a portfolio value)
        fb_accounts = []
        has_portfolio = any(r.get("_meta_business_portfolio") for r in records)
        if has_portfolio:
            fb_accounts = cls._get_fb_accounts()

        # Process each record
        for record in records:
            line_num = record.pop("_line_num")
            meta_business_portfolio = record.pop("_meta_business_portfolio", "")
            fb_page_id = record["fb_page_id"]

            # Resolve Meta Business Portfolio to fb_account_title + fb_account_esid
            if meta_business_portfolio:
                matched_account = cls._find_fb_account_by_title(meta_business_portfolio, fb_accounts)
                if matched_account:
                    record["fb_account_title"] = matched_account["title"]
                    record["fb_account_esid"] = matched_account["esid"]
                else:
                    errors.append(
                        {
                            "line": line_num,
                            "message": f"Meta Business Portfolio '{meta_business_portfolio}' not found in Facebook Accounts assets, imported without account link",
                        }
                    )

            try:
                # Check if Facebook Page already exists
                existing_doc = cls._get_existing_fb_page_by_id(fb_page_id)

                if existing_doc:
                    # Page exists - check if update is needed
                    needs_update = False
                    old_doc = existing_doc["source"]
                    esid = existing_doc["esid"]

                    # Compare fields
                    if old_doc.get("fb_page_name") != record.get("fb_page_name"):
                        needs_update = True
                    if old_doc.get("fb_page_slug") != record.get("fb_page_slug"):
                        needs_update = True
                    if record.get("fb_account_title") and old_doc.get("fb_account_title") != record.get("fb_account_title"):
                        needs_update = True
                    if record.get("fb_account_esid") and old_doc.get("fb_account_esid") != record.get("fb_account_esid"):
                        needs_update = True

                    if needs_update:
                        # Update existing document (merge preserves existing fields)
                        update_doc = {**old_doc, **record}
                        update_doc["@timestamp"] = datetime.utcnow().isoformat() + "Z"

                        es.index(
                            index="de_asset_fb_pages",
                            id=esid,
                            document=update_doc,
                            refresh="wait_for",
                        )
                        updated_count += 1
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"Facebook Page ID {fb_page_id} was a duplicate - updated",
                            }
                        )
                    else:
                        # No changes needed
                        skipped_count += 1
                        errors.append(
                            {
                                "line": line_num,
                                "message": f"Facebook Page ID {fb_page_id} was a duplicate - no changes needed",
                            }
                        )
                else:
                    # New page - create it
                    record["@timestamp"] = datetime.utcnow().isoformat() + "Z"

                    es.index(index="de_asset_fb_pages", document=record, refresh="wait_for")
                    imported_count += 1

            except Exception:  # noqa: BLE001 — one row failing must not abort the import
                logging.exception("AssetImportService: import/update failed at line %s", line_num)
                errors.append({"line": line_num, "message": "Failed to import/update: could not save this row"})

        return {
            "success": True,
            "imported_count": imported_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "error_count": len([e for e in errors if "Failed" in e["message"]]),
            "errors": errors,
        }
