"""
Google Analytics Configuration Module

YAML-based configuration management for Google Analytics integration.
Handles credentials loading, team mappings, and configuration validation.
"""

import os
import sys
import yaml
import logging

# Add parent directory to path for lib imports
sys.path.insert(0, "/app/python")

logger = logging.getLogger(__name__)

# GA4 OAuth shares the YouTube Google OAuth client (one verified
# client for all sensitive scopes) and adds the analytics.readonly
# scope. Mirrors import_scripts/youtube/get_youtube_analytics.load_account_credentials.
GA4_OAUTH_CLIENT_PATH = "/app/reporting_configs/youtube-oauth-client.json.enc"
GA4_OAUTH_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


def load_ga4_oauth_credentials(account_source):
    """Build OAuth2 user credentials for a GA4 account from its stored refresh token.

    Reads the per-account refresh token off a ``de_asset_ga4_accounts`` document
    (``ga4_account_token``, encrypted) and the shared client_id/client_secret from
    the encrypted OAuth client JSON, and returns a ``google.oauth2.credentials.Credentials``
    that the GA4 Admin/Data API clients accept exactly like service-account creds.

    Args:
        account_source: the ``_source`` dict from a de_asset_ga4_accounts document.

    Returns:
        google.oauth2.credentials.Credentials

    Raises:
        ValueError: if the account has no token stored (not yet authorized) or the
            OAuth client credentials file is malformed.
    """
    # Lazy imports — these libs are heavy and only the OAuth path needs them.
    from google.oauth2.credentials import Credentials
    from lib.encryption import decrypt
    from lib.google_oauth_client import load_google_oauth_client

    encrypted_token = (account_source or {}).get("ga4_account_token")
    if not encrypted_token:
        raise ValueError(
            f"GA4 account '{(account_source or {}).get('ga4_account_title', 'Unknown')}' "
            f"has no OAuth token. Authorize it via the Data Connectors wizard first."
        )

    refresh_token = decrypt(encrypted_token)

    # env-var first (SSM-injected), on-disk fallback.
    client = load_google_oauth_client()

    creds = Credentials(
        token=None,  # auto-refreshed on first API call
        refresh_token=refresh_token,
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GA4_OAUTH_SCOPES,
    )
    logger.info("Loaded GA4 OAuth credentials for account '%s'", (account_source or {}).get("ga4_account_title"))
    return creds


def iter_connected_ga4_accounts():
    """yield ``(account_source, credentials)`` for every GA4 account
    that has a stored OAuth token.

    The GA4 service account is retired, so property discovery
    (``export_properties``) can no longer use one SA identity that "sees
    everything." Instead it iterates the accounts users have authorized via the
    Data Connectors wizard (``de_asset_ga4_accounts`` docs carrying an encrypted
    ``ga4_account_token``) and lists each account's properties under that
    account's own token.

    Fail-soft per account: an account whose token won't build credentials is
    logged and skipped, never raised — one bad grant must not block discovery of
    the others.
    """
    try:
        from lib.es_helpers import get_elasticsearch_client
    except Exception as e:  # noqa: BLE001 — import boundary
        logger.error("iter_connected_ga4_accounts: es_helpers import failed: %s", e)
        return

    es = get_elasticsearch_client()
    if es is None:
        logger.error("iter_connected_ga4_accounts: ES unreachable")
        return

    query = {
        "query": {"exists": {"field": "ga4_account_token"}},
        "size": 1000,
    }
    try:
        result = es.search(index="de_asset_ga4_accounts", body=query)
    except Exception as e:  # noqa: BLE001 — boundary; no accounts is not fatal
        logger.error("iter_connected_ga4_accounts: search failed: %s", e)
        return

    hits = result.get("hits", {}).get("hits", [])
    logger.info("iter_connected_ga4_accounts: %d GA4 account(s) with an OAuth token", len(hits))
    for hit in hits:
        source = hit.get("_source", {})
        title = source.get("ga4_account_title", "Unknown")
        try:
            creds = load_ga4_oauth_credentials(source)
        except Exception as e:  # noqa: BLE001 — per-account boundary
            logger.warning("iter_connected_ga4_accounts: skipping account '%s' — %s", title, e)
            continue
        yield source, creds


def resolve_ga4_credentials(property_id):
    """OAuth credential selector for one GA4 property.

    Returns ``google.oauth2.credentials.Credentials`` when ``property_id``
    resolves a GA4 OAuth connection that has a stored token, or ``None`` when it
    does not.

    the service account is retired, so ``None`` now means "skip this
    property" (the caller has no SA to fall back to), not "use the SA." A
    property whose owning team hasn't authorized a GA4 connection is not
    imported until it is connected via the Data Connectors wizard.

    The decision logic is intentionally pure + side-effect-light (one ES read
    via the resolver, then a token decrypt) so it is unit-testable without a
    live Google client — -GA4-OAUTH-CRED-001.

    Fail-safe: any resolution error returns ``None`` (→ skip) rather than
    raising, so a connection-lookup hiccup never breaks the whole import.
    """
    pid = str(property_id or "").split("/")[-1].strip()
    if not pid:
        return None
    try:
        from asset_management.web.services.connection_service import ConnectionService
    except ImportError:  # pragma: no cover - import-env specific
        logger.warning("resolve_ga4_credentials: ConnectionService unavailable; using service account")
        return None

    account_esid = ConnectionService.get_ga4_oauth_account_esid_for_property(pid)
    if not account_esid:
        return None  # no OAuth connection → caller skips this property

    # Read the account doc (carries the encrypted ga4_account_token).
    try:
        from lib.es_helpers import get_document_by_id

        account_source = get_document_by_id("de_asset_ga4_accounts", account_esid)
    except Exception as e:  # noqa: BLE001 — fall back to SA on any read failure
        logger.warning("resolve_ga4_credentials: account read failed for %s: %s", account_esid, e)
        return None

    if not account_source or not account_source.get("ga4_account_token"):
        return None  # connection resolved but no token → caller skips

    try:
        return load_ga4_oauth_credentials(account_source)
    except Exception as e:  # noqa: BLE001 — never let a bad token break the import
        logger.warning("resolve_ga4_credentials: building OAuth creds failed for %s: %s", account_esid, e)
        return None


class GAConfig:
    """YAML-based configuration for Google Analytics integration."""

    def __init__(self, config_path="/app/reporting_configs/google-analytics.yaml"):
        """
        Load configuration from YAML file.

        Args:
            config_path: Path to YAML configuration file

        Raises:
            FileNotFoundError: If config file doesn't exist
            yaml.YAMLError: If config file is invalid YAML
        """
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        # Part B — no credentials fields. ``credentials_file`` (and
        # the ``_credentials`` cache) belonged to the retired GA4 service
        # account; the path it pointed at does
        # not exist on any environment and nothing read it.

        # Data collection settings
        self.days_to_pull = int(config.get("days_to_pull", 30))
        self.report_limit = int(config.get("report_limit", 100000))

        # Team mapping (Phase 1: hardcoded in YAML)
        self.property_team_mapping = config.get("property_team_mapping", {})
        self.default_team = config.get(
            "default_team",
            {
                "team_esid": "default",
                "team_title": "Unmapped Properties",
            },
        )

        # Logging
        self.log_level = config.get("log_level", "INFO").upper()

        # Account filtering
        self.account_filter = config.get("account_filter", [])

        # Normalize account_filter to always be a list
        if isinstance(self.account_filter, dict):
            # If using the enabled/account_ids structure
            if self.account_filter.get("enabled", False):
                self.account_filter = self.account_filter.get("account_ids", [])
            else:
                self.account_filter = []
        elif not isinstance(self.account_filter, list):
            self.account_filter = []

        # Rate limiting configuration
        self.rate_limit_config = config.get("rate_limit", {})

    # Part B — ``validate_config`` removed. had already
    # reduced it to a documented no-op (GA4 is OAuth-only; auth resolves per
    # property via ``resolve_ga4_credentials``); a no-op "hook" that three
    # scripts dutifully called was pure ritual, and its existence implied a
    # validation step that never ran.

    def is_account_allowed(self, account_resource_name):
        """
        Check if an account should be processed based on filter configuration.

        Args:
            account_resource_name: Account resource name (e.g., "accounts/123456")

        Returns:
            bool: True if account should be processed, False otherwise
        """
        # If no filter configured, allow all accounts
        if not self.account_filter:
            return True

        # Extract account ID from resource name
        # Format: "accounts/123456" -> "123456"
        account_id = account_resource_name.split("/")[-1]

        # Check if account ID is in whitelist
        return account_id in self.account_filter

    def get_team_metadata_from_assets(self, property_id):
        """
        Get team metadata from ES assets (Phase 2).

        Args:
            property_id: GA4 property ID (string or int)

        Returns:
            dict: Team metadata with keys: team_esid, team_title
            None: If no asset mapping found
        """
        from import_scripts.google_analytics.lib.assets import (
            get_team_by_ga_property_id,
        )

        property_id_str = str(property_id)

        try:
            team = get_team_by_ga_property_id(property_id_str)
            if team:
                return {
                    "team_esid": team.get("team_esid"),
                    "team_title": team.get("team_title"),
                }
        except Exception as e:
            logger.warning(f"Error querying ES assets for property {property_id}: {e}")

        return None

    def get_team_metadata(self, property_id):
        """
        Get team metadata with fallback: ES assets → YAML → default_team.

        Args:
            property_id: GA4 property ID (string or int)

        Returns:
            dict: Team metadata with keys: team_esid, team_title
        """
        property_id_str = str(property_id)

        # Priority 1: ES assets
        team_metadata = self.get_team_metadata_from_assets(property_id_str)
        if team_metadata:
            logger.debug(f"Found team mapping in ES assets for property {property_id}")
            return team_metadata

        # Priority 2: YAML (Phase 1 compatibility - to be deprecated)
        if property_id_str in self.property_team_mapping:
            logger.warning(f"Using deprecated YAML mapping for property {property_id}")
            return self.property_team_mapping[property_id_str]

        # Priority 3: Default team
        logger.warning(f"No team mapping for property {property_id}, using default")
        return self.default_team

    def get_rate_limit_config(self):
        """
        Get rate limiting configuration with defaults.

        Returns:
            dict: Rate limit config with keys: max_retries, initial_backoff_seconds, log_usage
        """
        return {
            "max_retries": self.rate_limit_config.get("max_retries", 5),
            "initial_backoff_seconds": self.rate_limit_config.get("initial_backoff_seconds", 60),
            "log_usage": self.rate_limit_config.get("log_usage", False),
        }
