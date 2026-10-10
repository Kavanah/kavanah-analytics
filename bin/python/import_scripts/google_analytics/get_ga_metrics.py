"""
GA4 Metrics Fetcher for Digital Engagement Dashboard

Fetches Google Analytics 4 data for properties configured in de_asset_ga_properties.
Only properties assigned to a team (via team_ga4_properties array) are processed.

This script:
1. Loads GA4 properties from de_asset_ga_properties index in Elasticsearch
2. Fetches metrics for each property (8 dimensions + 8 metrics)
3. Adds team metadata from de_asset_teams (team-centric model)
4. Saves data as JSON files for later Elasticsearch indexing
"""

import os
import sys
import json
import logging
import hashlib
import time
from datetime import datetime, timezone

# Add parent directory to path for lib imports
sys.path.insert(0, "/app/python")
import lib.logging as custom_logging
from lib.no_data_markers import write_no_data_markers
from lib.cleanup_no_data_markers import delete_stale_markers_for_date

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest,
    DateRange,
    Dimension,
    Metric,
)
from google.auth.exceptions import RefreshError

from import_scripts.google_analytics.lib.ga_config import GAConfig, resolve_ga4_credentials
from import_scripts.google_analytics.lib.assets import (
    get_ga_properties,
    get_team_by_ga_property_id,
)
from import_scripts.facebook.get_page_insights import _resolve_team_country

logger = logging.getLogger(__name__)

# Module-level flag for usage logging
_log_usage = False
_usage_logger = None


# focused-report model. One overloaded RunReport (8 dims / 8 metrics)
# is replaced by several smaller reports, each keyed by a coherent dimension set
# under GA4's 9-dimension / 10-metric per-request cap. Every report carries
# ``date`` + the base additive counts so each report_type stands alone; charts
# filter rows by ``report_type``. See the private docs
#
# RATIO GUARDRAIL: ``bounceRate`` / ``engagementRate`` on the ``pages`` report are
# per-page ratios. They are correct at row grain but MUST NEVER be summed across
# rows (a ratio can't be summed). The daily-additive base metrics below are
# safe to sum; the ratio metrics are not. The window importer carries the
# property-level deduplicated/ratio figures for the KPI tiles.
_BASE_METRICS = [
    "activeUsers",
    "sessions",
    "screenPageViews",
    "eventCount",
]

# report_type -> {"dimensions": [...], "metrics": [...]}. Each entry is validated
# against the 9/10 cap at module import (see _validate_report_specs below).
REPORT_SPECS = {
    "pages": {
        "dimensions": ["pageTitle", "pagePath", "unifiedScreenClass", "platform", "date"],
        # base counts + per-page ratios (NEVER summed) + total/engaged users
        "metrics": _BASE_METRICS + ["totalUsers", "engagedSessions", "bounceRate", "engagementRate"],
    },
    "acquisition": {
        "dimensions": ["sessionSource", "sessionMedium", "firstUserSource", "firstUserMedium", "date"],
        "metrics": _BASE_METRICS + ["newUsers", "totalUsers"],
    },
    "geo_device": {
        "dimensions": ["country", "city", "deviceCategory", "platform", "date"],
        "metrics": _BASE_METRICS + ["totalUsers"],
    },
    "retention": {
        # newVsReturning + audienceName folded together (, both "who is
        # this user" cuts); keyEvents lives here so the "Key events by Platform"
        # donut has its metric alongside platform.
        "dimensions": ["newVsReturning", "audienceName", "platform", "date"],
        "metrics": _BASE_METRICS + ["newUsers", "totalUsers", "keyEvents"],
    },
    "events": {
        # Re-homes today's eventName/linkUrl click-tracking dims at their natural
        # event grain (linkUrl is high-cardinality; keeping it off the other
        # reports prevents row-count explosion).
        "dimensions": ["eventName", "linkUrl", "platform", "date"],
        "metrics": _BASE_METRICS,
    },
}

# Metrics that are NON-ADDITIVE ratios — correct per row, never summable. The
# Phase-D test pins this; downstream charting must not aggregate these with sum.
RATIO_METRICS = frozenset({"bounceRate", "engagementRate"})

_GA4_MAX_DIMENSIONS = 9
_GA4_MAX_METRICS = 10


def _validate_report_specs():
    """Fail fast at import if any report_type breaches GA4's 9/10 per-request cap.

    A spec that exceeds the cap would make every RunReport for that type fail at
    runtime with an opaque API error; catching it here turns a prod-only failure
    into an import-time assertion the Phase-D test also exercises.
    """
    for report_type, spec in REPORT_SPECS.items():
        n_dims = len(spec["dimensions"])
        n_metrics = len(spec["metrics"])
        if n_dims > _GA4_MAX_DIMENSIONS:
            raise ValueError(f"report_type '{report_type}' has {n_dims} dimensions " f"(> {_GA4_MAX_DIMENSIONS} cap)")
        if n_metrics > _GA4_MAX_METRICS:
            raise ValueError(f"report_type '{report_type}' has {n_metrics} metrics " f"(> {_GA4_MAX_METRICS} cap)")


_validate_report_specs()


def _ga4_data_client_for_property(property_id, oauth_client_cache):
    """return the OAuth-backed data client for one property,
    or ``None`` if the property has no GA4 OAuth connection/token.

    GA4 is OAuth-only (the service account is retired). If the property
    resolves a GA4 OAuth connection with a token (``resolve_ga4_credentials``),
    build/reuse a client keyed by the resolving account so co-owned properties
    share one client. Otherwise return ``None`` — the caller skips the property
    (mirrors the YouTube importer's skip-if-no-token behaviour).
    """
    creds = resolve_ga4_credentials(property_id)
    if creds is None:
        return None
    # Key the cache by the credential's refresh token (one client per granted
    # account); falls back to the property id if the attribute is absent.
    cache_key = getattr(creds, "refresh_token", None) or str(property_id)
    client = oauth_client_cache.get(cache_key)
    if client is None:
        client = BetaAnalyticsDataClient(credentials=creds)
        oauth_client_cache[cache_key] = client
        logging.info("using OAuth credentials for GA4 property %s", property_id)
    return client


# days without data before a connected property counts as dark;
# the same threshold as the Status Reports continuity checks
# (data_quality_service._CONTINUITY_STALL_DAYS).
PROPERTY_DARK_DAYS = 4


def _newest_property_day(es, team_esid, property_id):
    """Newest stored ``kibana_date`` (yyyy-MM-dd) of ``property_id`` in the team's
    ``ga_metrics`` index, or None when it has no docs (or the index is missing)."""
    from lib.es_helpers import build_data_index_name

    resp = es.search(
        index=build_data_index_name(team_esid, "ga_metrics"),
        body={
            "size": 0,
            "query": {"term": {"property_id": str(property_id).split("/")[-1]}},
            "aggs": {"latest": {"max": {"field": "kibana_date", "format": "yyyy-MM-dd"}}},
        },
        ignore_unavailable=True,
    )
    value = ((resp.get("aggregations") or {}).get("latest") or {}).get("value_as_string")
    return value[:10] if value else None


def _note_if_property_dark(property_id, property_name, team_esid, es=None, today=None):
    """this run pulled 0 rows for a CONNECTED property. When its newest
    stored day is ``PROPERTY_DARK_DAYS``+ old (or it never had any), tell the team
    in the Report Scheduler: Google answers a property the website no longer
    feeds with successful empty reports, which otherwise looks like a quiet one
    (one team's web property went dark for two months). Scheduled path only;
    fail-soft — a lookup problem never breaks the import."""
    from lib.import_run_context import get_run_id, record_user_error

    if not get_run_id() or not team_esid:
        return None
    try:
        if es is None:
            from lib.es_helpers import get_elasticsearch_client

            es = get_elasticsearch_client()
        last = _newest_property_day(es, team_esid, property_id)
    except Exception as exc:  # noqa: BLE001 — a reporting lookup must never abort the import
        logging.warning("newest-day lookup failed for GA4 property %s: %s", property_id, exc)
        return None
    today = today or datetime.now(timezone.utc).date()
    if last is not None and (today - datetime.strptime(last, "%Y-%m-%d").date()).days < PROPERTY_DARK_DAYS:
        return None
    since = f"no data since {last}" if last else "no data has arrived yet"
    record_user_error(
        f"GA4 property went dark: {since}",
        connector_id="google_analytics_4",
        detail=f"{property_name or property_id} ({since})",
    )
    return last or "never"


def _revoke_ga4_connection_for_property(property_id):
    """flip the GA4 connection serving ``property_id`` to
    ``status=revoked`` after its OAuth refresh token is rejected by Google
    (invalid_grant / RefreshError).

    Google refresh tokens don't expire on a timer the way Meta access tokens do,
    so there is no rotation cron; instead the importer detects a dead token
    lazily here and marks the connection so (a) the next run skips OAuth and
    falls back to the service account, and (b) the connector card shows
    "needs attention" prompting the user to reconnect. Fail-soft: a status-flip
    failure is logged, never raised (the import continues on the SA fallback).
    Returns the revoked connection esid, or None if nothing was flipped.
    """
    try:
        from asset_management.web.services.connection_service import ConnectionService, STATUS_REVOKED

        resolved = ConnectionService.get_ga4_oauth_connection_for_property(property_id)
        if not resolved:
            return None
        connection_esid, _account_esid = resolved
        if ConnectionService.set_status(connection_esid, STATUS_REVOKED):
            logging.warning(
                "GA4 OAuth token rejected for property %s; flipped connection %s to revoked "
                "(falling back to service account for this run)",
                property_id,
                connection_esid,
            )
            return connection_esid
    except Exception as e:  # noqa: BLE001 — detection is best-effort; never break the import
        logging.warning("failed to revoke GA4 connection for property %s: %s", property_id, e)
    return None


def setup_usage_logging():
    """Setup a separate logger for rate limit usage metrics."""
    global _usage_logger
    _usage_logger = logging.getLogger("ga_usage")
    _usage_logger.setLevel(logging.INFO)

    current_date = datetime.now().strftime("%Y%m%d")
    log_file = f"/app/data/logs/ga_usage_{current_date}.log"
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    _usage_logger.addHandler(handler)
    _usage_logger.propagate = False  # Don't send to root logger


def handle_rate_limit_with_backoff(property_name, attempt, max_retries=5):
    """
    Handle Google Analytics API rate limit errors with exponential backoff.

    Args:
        property_name: GA4 property identifier for logging
        attempt: Current retry attempt number (0-indexed)
        max_retries: Maximum retry attempts before giving up

    Returns:
        bool: True if should retry, False if max retries exceeded
    """
    if attempt >= max_retries:
        logging.error(f"Rate limit: Max retries ({max_retries}) exceeded for {property_name}")
        return False

    # Exponential backoff: 60s, 120s, 240s, 480s, 960s
    sleep_seconds = 60 * (2**attempt)

    logging.warning(
        f"Rate limit: Property {property_name} hit quota limit. "
        f"Attempt {attempt + 1}/{max_retries}. Sleeping {sleep_seconds}s"
    )

    time.sleep(sleep_seconds)
    return True


def load_properties_from_assets(property_ids=None):
    """
    Load GA4 properties from de_asset_ga_properties index in Elasticsearch.

    Only returns properties that have a team assignment (property ID exists
    in a team's team_ga4_properties array).

    Args:
        property_ids: Optional iterable of GA4 property ids ("<id>" or
            "properties/<id>") to scope the ES FETCH to. a
            property-scoped run (wizard kickoff / scheduler) used to load
            EVERY property (each with its own all-teams lookup) and filter
            late in main(); scoping the fetch up front means only the
            requested properties get a team resolution + OAuth client.
            None = all properties (cron behavior, paginated — no 1000 cap).

    Returns:
        list: List of dicts with keys: property_id, property_name, team_metadata
    """
    properties = []

    # Get GA properties from Elasticsearch (scoped when property_ids given)
    if property_ids:
        wanted = sorted({str(p).split("/")[-1].strip() for p in property_ids if str(p).strip()})
        ga_property_docs = get_ga_properties(filters={"ga_property_id": wanted})
        if not ga_property_docs:
            # Fallback for a GA-properties index mapped text+keyword (the
            # trap) — mirrors _ga_property_esid_for_id.
            ga_property_docs = get_ga_properties(filters={"ga_property_id.keyword": wanted})
    else:
        ga_property_docs = get_ga_properties()

    if not ga_property_docs:
        logging.warning("No GA properties found in de_asset_ga_properties")
        return properties

    for doc in ga_property_docs:
        source = doc["_source"]
        property_id = source.get("ga_property_id")
        property_name = source.get("ga_property_title") or source.get("ga_property_display_name", "Unknown")

        # Look up team from team_ga4_properties array (team-centric model)
        team_metadata = get_team_by_ga_property_id(property_id)

        # Skip properties not assigned to any team
        if not team_metadata:
            logging.info(f"Skipping property {property_name} ({property_id}) - not in any team")
            continue

        properties.append(
            {
                "property_id": property_id,
                "property_name": property_name,
                "team_metadata": team_metadata,
            }
        )

    logging.info(f"Loaded {len(properties)} GA4 properties from Elasticsearch (with team assignments)")
    return properties


def check_if_already_imported(property_id, date_str):
    """
    Check if data for this property and date already exists in _imported folder.
    Helps avoid re-fetching data that's already been processed.

    this function is intentionally NOT called by the fetch loop —
    GA always re-fetches and the sender upserts each row by a deterministic
    MD5 _id (get_ga_metrics builds _doc_id; send_json_to_elastic indexes by it),
    so a re-run idempotently overwrites in place. That means GA does NOT have
    the archive-before-confirmed-send loss the other importers had: there is no
    skip-if-archived gate to strand data behind. Leave it uncalled. If a skip
    is ever wired up here for cost reasons, gate it on `indexed_doc_present`
    (es_helpers) the way the FB/IG/GAds/YouTube fetch steps now do — NOT on the
    archive folder alone.

    Args:
        property_id: GA4 property ID
        date_str: Date string in YYYYMMDD format

    Returns:
        bool: True if already imported, False otherwise
    """
    imported_dir = f"/app/data/google_analytics/ga_metrics_imported/{property_id}/"
    if not os.path.exists(imported_dir):
        return False

    # Check if any files for this date exist
    for filename in os.listdir(imported_dir):
        if filename.startswith(f"{date_str}_") and filename.endswith(".json"):
            return True

    return False


# Metrics cast to float; everything else cast to int. userEngagementDuration is
# seconds; the ratio metrics are 0..1 fractions — all non-integer.
_FLOAT_METRICS = frozenset({"userEngagementDuration", "bounceRate", "engagementRate"})


def _cast_metric(name, raw):
    """Cast a GA4 metric string to the right Python numeric type."""
    if name in _FLOAT_METRICS:
        return float(raw or 0)
    return int(raw or 0)


def fetch_property_data(data_client, property_id, property_name, config, start_date=None, end_date=None, report_type="pages"):
    """
    Fetch one GA4 report_type for a single property, with rate-limit retry.

    builds the request from ``REPORT_SPECS[report_type]`` (dimensions +
    metrics under GA4's 9/10 cap) instead of one hardcoded overloaded report.
    Rows are mapped generically by zipping the spec's dimension/metric NAMES to
    the response values (no fixed positional indices), and tagged with
    ``report_type`` so the single ``*_ga_metrics`` index can hold every type.

    Args:
        data_client: BetaAnalyticsDataClient instance
        property_id: GA4 property ID
        property_name: Property display name
        config: GAConfig instance
        start_date: Explicit start date (YYYY-MM-DD). Overrides days_to_pull.
        end_date: Explicit end date (YYYY-MM-DD). Overrides days_to_pull.
        report_type: Key into REPORT_SPECS (pages/acquisition/geo_device/retention/events).

    Returns:
        list: List of row dicts, or empty list if no data
    """
    spec = REPORT_SPECS.get(report_type)
    if spec is None:
        raise ValueError(f"unknown report_type {report_type!r}")
    dimension_names = spec["dimensions"]
    metric_names = spec["metrics"]

    # Get rate limit config
    rate_limit_config = config.get_rate_limit_config()
    max_retries = rate_limit_config.get("max_retries", 5)

    # Build the request once (reused across retries) from the report spec.
    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=d) for d in dimension_names],
        metrics=[Metric(name=m) for m in metric_names],
        date_ranges=[
            DateRange(
                start_date=start_date or f"{config.days_to_pull}daysAgo",
                end_date=end_date or "today",
            )
        ],
        limit=config.report_limit,
    )

    # Retry loop with exponential backoff
    for attempt in range(max_retries + 1):
        try:
            response = data_client.run_report(request)

            # Log successful API call if usage logging enabled
            if _log_usage and _usage_logger:
                _usage_logger.info(
                    f"property={property_id} report_type={report_type} attempt={attempt} "
                    f"status=success rows={len(response.rows)}"
                )

            logging.info(f"Pulled {len(response.rows)} rows for property {property_id} report_type={report_type}")

            if len(response.rows) == 0:
                logging.warning(f"No data found for property {property_id} report_type={report_type}")
                return []

            # Convert rows to dicts — map by spec NAME, not positional index, so
            # adding/reordering a field in REPORT_SPECS can't silently misalign.
            rows = []
            for row in response.rows:
                doc = {
                    "property_id": property_id,
                    "property_name": property_name,
                    "report_type": report_type,
                    "@timestamp": datetime.now(timezone.utc).isoformat(),
                }
                for i, name in enumerate(dimension_names):
                    doc[name] = row.dimension_values[i].value
                for i, name in enumerate(metric_names):
                    doc[name] = _cast_metric(name, row.metric_values[i].value)
                rows.append(doc)

            return rows

        except Exception as e:
            # Check if it's a rate limit error (429)
            error_message = str(e).lower()
            if "429" in error_message or "resource_exhausted" in error_message or "quota" in error_message:
                if _log_usage and _usage_logger:
                    _usage_logger.info(
                        f"property={property_id} report_type={report_type} attempt={attempt} "
                        f"status=rate_limit error={str(e)[:100]}"
                    )

                # Try backoff, or give up if max retries exceeded
                if not handle_rate_limit_with_backoff(property_name, attempt, max_retries):
                    logging.error(f"Giving up on {property_name} (ID: {property_id}) after {max_retries} retries")
                    return []
                continue  # Retry after backoff
            else:
                # Non-rate-limit error, log and return
                logging.error(f"Failed to fetch data for property {property_id} report_type={report_type}: {e}")
                return []

    # This should not be reached, but just in case
    return []


def save_property_json(
    property_data,
    property_id,
    team_metadata,
    output_dir="/app/data/google_analytics/ga_metrics",
):
    """
    Save fetched GA4 data as JSON files.

    Groups data by date and saves to: {output_dir}/{property_id}/{date}_{hash}.json

    Args:
        property_data: List of row dicts from fetch_property_data()
        property_id: GA4 property ID
        team_metadata: Dict with team_esid, team_title
        output_dir: Base directory for JSON files

    Returns:
        int: Number of files saved
    """
    if not property_data:
        return 0

    # Create property directory
    property_dir = os.path.join(output_dir, str(property_id))
    os.makedirs(property_dir, exist_ok=True)

    # Group data by date
    data_by_date = {}
    for doc in property_data:
        date = doc.get("date", "unknown")
        if date not in data_by_date:
            data_by_date[date] = []
        data_by_date[date].append(doc)

    files_saved = 0

    for date, docs in data_by_date.items():
        # Add team metadata to each document
        for doc in docs:
            doc["team_esid"] = team_metadata["team_esid"]
            doc["team_title"] = team_metadata["team_title"]
            doc["organization_title"] = team_metadata.get("organization_title", "Unknown")
            doc["team_country"] = _resolve_team_country(team_metadata)

            # Generate unique document ID for deduplication in ES.
            # each report_type has a different dimension set, so the
            # _id is built from report_type + that type's actual dimension
            # values (in spec order) rather than a fixed dimension list. This
            # keeps rows from different report_types distinct and makes a re-run
            # idempotently overwrite the same row.
            report_type = doc.get("report_type", "pages")
            dim_names = REPORT_SPECS.get(report_type, REPORT_SPECS["pages"])["dimensions"]
            dim_parts = "-".join(str(doc.get(name, "")) for name in dim_names)
            doc_id_string = f"{property_id}-{report_type}-{dim_parts}"
            doc["_doc_id"] = hashlib.md5(  # nosemgrep: insecure-hash-algorithm-md5 - non-cryptographic doc ID fingerprint
                doc_id_string.encode()
            ).hexdigest()

        # Save all docs for this date to a single JSON file.
        # the filename hash includes report_type so two report_types
        # with the same row count on the same date don't collide and overwrite
        # each other (all docs in one save call share a report_type).
        report_type = docs[0].get("report_type", "pages")
        filename_hash = hashlib.md5(  # nosemgrep: insecure-hash-algorithm-md5 - non-cryptographic doc ID fingerprint
            f"{date}-{report_type}-{len(docs)}".encode()
        ).hexdigest()[:8]
        filename = f"{date}_{report_type}_{filename_hash}.json"
        filepath = os.path.join(property_dir, filename)

        with open(filepath, "w") as f:
            for doc in docs:
                f.write(json.dumps(doc) + "\n")

        logging.info(f"Saved {len(docs)} rows to {filepath}")
        files_saved += 1

    return files_saved


def main(params):
    """
    Main entry point from import.py.

    Args:
        params: Dict of command-line parameters (e.g., {'days_to_pull': '7'})
    """
    global _log_usage

    custom_logging.define("ga-get-metrics")

    # bind this run's progress doc so a per-property OAuth-revoked
    # failure surfaces in the Report Scheduler's "Errors" button. No-op on cron.
    from lib.import_run_context import set_run_id

    set_run_id(params.get("progress_run_id"))

    try:
        # Load configuration
        logging.info("Loading configuration...")
        config = GAConfig()

        # Check for usage logging configuration
        rate_limit_config = config.get_rate_limit_config()
        if rate_limit_config.get("log_usage"):
            _log_usage = True
            setup_usage_logging()
            logging.info("Usage logging enabled - writing to /app/data/logs/ga_usage_YYYYMMDD.log")

        # Explicit date range overrides days_to_pull
        explicit_start = params.get("start_date")
        explicit_end = params.get("end_date")
        if explicit_start and explicit_end:
            logging.info(f"Using explicit date range: {explicit_start} to {explicit_end}")
        elif "days_to_pull" in params:
            config.days_to_pull = int(params["days_to_pull"])

        logging.info(
            f"Configuration loaded: {config.days_to_pull} days, {len(config.property_team_mapping)} properties mapped"
        )

        # GA4 is OAuth-only (service account retired). Each property's
        # client is resolved per-property from its team's GA4 OAuth connection
        # (see _ga4_data_client_for_property); a property with no connection is
        # skipped. Cache of OAuth-backed clients keyed by the granted account so
        # multiple properties under one authorized account reuse a single client.
        _oauth_client_cache = {}

        # optional per-property filter for the wizard's
        # on-demand kickoff path. The orchestrator's --connection_esid
        # handling resolves to a comma-separated list of property_ids
        # the user just connected. the filter now scopes the ES
        # FETCH itself (load_properties_from_assets) so out-of-scope
        # properties never get a team lookup or OAuth client; the late
        # normalized filter below is kept as a safety net. Cron path (no
        # flag) is unaffected.
        # require an explicit property scope BEFORE the ES fetch. Cron
        # is dead (deployment/03_provision_cron_jobs.sh migrated the importer jobs
        # to the Report Scheduler), the manual Import Data Page always
        # sends the selected team, and backfill reuses the scheduler dialog — so no
        # legitimate caller omits --property_ids. An absent flag must fail loudly
        # rather than fall through to a fleet-wide fetch (the cross-tenant exposure
        # class behind).
        #
        # ImportService._resolve_scope_arg (import_service.py:450-451) DELIBERATELY
        # omits the flag when the selected team resolves to no GA4 property, and its
        # docstring says it relies on this guard to surface a classifiable "must be
        # team-scoped" error instead of the UI hanging on a 0-byte log. That
        # contract only holds if the guard exists here.
        from lib.import_scope import require_scope, scope_resolution_error

        property_ids_filter = require_scope(params, scope_param="property_ids", connector_id="ga4")
        scoped_property_ids = [p.strip() for p in str(property_ids_filter).split(",") if p.strip()]

        # Load properties from Elasticsearch assets
        logging.info("Loading GA4 properties from Elasticsearch...")
        properties = load_properties_from_assets(property_ids=scoped_property_ids)

        if not properties:
            # the scope flag was provided (require_scope above proved
            # it) but the scoped ES fetch matched nothing. Pre-fix this warned and
            # returned, exiting rc 0, so the Report Scheduler recorded a SUCCESS
            # for a run that imported nothing — the failure mode that hid six dark
            # teams for weeks. Mirrors Facebook's
            # strict mode (facebook/get_insights.py:1783-1794).
            raise scope_resolution_error(
                scope_param="property_ids",
                scope_value=property_ids_filter,
                connector_id="ga4",
            )

        # property_ids_filter is guaranteed truthy by require_scope above; the
        # conditional is kept so this block reads the same as the other importers'
        # safety-net filters.
        if property_ids_filter:
            wanted = {p.strip() for p in str(property_ids_filter).split(",") if p.strip()}
            # GA4 ids surface as bare numbers in the asset doc but as
            # "properties/<id>" in the connection_asset rows; accept
            # both shapes by stripping the prefix on comparison.
            normalised_wanted = {w.split("/")[-1] for w in wanted}
            before = len(properties)
            properties = [p for p in properties if str(p["property_id"]).split("/")[-1] in normalised_wanted]
            logging.info(f"-property_ids filter applied: {before} -> {len(properties)} properties")
            if not properties:
                # the exact-match safety net emptied the list, so the
                # requested properties exist in ES but none matched after id
                # normalisation. Same contradiction as the fetch-level check above:
                # the caller named properties and none survived. Raise rather than
                # the old warn-and-return, which exited rc 0 and let the scheduler
                # record a success for a run that imported nothing.
                raise scope_resolution_error(
                    scope_param="property_ids",
                    scope_value=property_ids_filter,
                    connector_id="ga4",
                )

        # the per-property billing pre-check now runs INSIDE
        # the property loop below (it needs the property_id to match the over-cap
        # partition per-source, and the property name to surface a per-property
        # error). See the source_billing_block call after team_metadata is read.

        # on-demand cancel support. When the dashboard kickoff
        # threads a --progress_run_id, check between properties whether the
        # user clicked Cancel and stop promptly (keeping data already fetched).
        # No-op on the cron path (no run_id). Probe is fail-soft: a transient
        # ES error never aborts an import the user didn't cancel.
        progress_run_id = params.get("progress_run_id")

        def _cancel_requested():
            if not progress_run_id:
                return False
            try:
                from lib.import_progress_service import ImportProgressService

                return ImportProgressService.is_cancelling(progress_run_id)
            except Exception as exc:  # noqa: BLE001 — boundary
                logging.warning("ga get_metrics is_cancelling raised: %s — proceeding", exc)
                return False

        # Fetch and save data for each property
        total_files = 0
        total_rows = 0
        teams_with_data_by_date = {}  # {date_str: set(team_esid)}
        all_team_list = []  # [(team_esid, team_title)]
        all_dates_seen = set()

        for prop in properties:
            if _cancel_requested():
                logging.info("ga get_metrics: cancellation observed; stopping property loop (fetched data retained).")
                break

            property_id = prop["property_id"]
            property_name = prop["property_name"]
            team_metadata = prop["team_metadata"]

            # bind this property's team so recorded errors carry
            # durable per-team attribution.
            from lib.import_run_context import set_current_team

            set_current_team(team_metadata.get("team_esid"))

            # billing pre-check per source. Skip a property
            # under an inactive subscription (Condition A) or over the paid
            # connection cap (Condition B), matching the Current Plan badge. The
            # GA4 property_id ("properties/<id>") and bare "<id>" both occur; match
            # on either. Surfaces a classified error on the scheduled path
            # (no-op on cron). Fail-open per property.
            from lib.billing_entitlement import record_billing_skip, source_billing_block

            # The org rollup stores the GA4 provider_id as the BARE numeric id
            # (connector_status_service id_field = ga_property_id, e.g. "396371821"),
            # while property_id may arrive as "properties/<id>" — match on the bare id.
            _bare_property_id = str(property_id).split("/")[-1]
            _billing_block = source_billing_block(team_esid=team_metadata.get("team_esid"), provider_id=_bare_property_id)
            if _billing_block:
                logging.info(
                    "billing pre-check: skipping GA4 property %s (team %s, reason=%s).",
                    property_name,
                    team_metadata.get("team_esid"),
                    _billing_block,
                )
                record_billing_skip(_billing_block, connector_id="google_analytics_4", detail=property_name)
                continue

            all_team_list.append((team_metadata["team_esid"], team_metadata["team_title"]))

            logging.info(f"Processing property: {property_name} (ID: {property_id})")
            logging.info(f"Team mapping: {team_metadata['team_title']} (esid: {team_metadata['team_esid']})")

            # OAuth-only. Resolve this property's per-account OAuth
            # client; skip the property if its team hasn't authorized a GA4
            # connection (no service-account fallback any more).
            data_client = _ga4_data_client_for_property(property_id, _oauth_client_cache)
            if data_client is None:
                logging.warning(
                    "skipping property %s (%s) — no GA4 OAuth connection for its team. "
                    "Authorize it via the Data Connectors wizard.",
                    property_name,
                    property_id,
                )
                # the property's team hasn't authorized a GA4
                # connection; surface it as missing_credentials so the user knows
                # to connect it (no SA fallback). No-op on cron.
                from lib.import_run_context import record_user_error

                record_user_error(
                    f"GA4 property {property_name} has no OAuth connection — not authorized.",
                    connector_id="google_analytics_4",
                    detail=(property_name or team_metadata.get("team_title")),
                )
                continue

            # fetch each focused report_type for this property and
            # save each type's rows. when an OAuth token is dead
            # (revoked at Google / invalid_grant), the Credentials object raises
            # RefreshError on first use; that is a property-level failure, so we
            # revoke + skip the WHOLE property (break the report-type loop), not
            # just one report. There is no SA to retry on.
            property_token_dead = False
            property_rows = 0
            for report_type in REPORT_SPECS:
                try:
                    report_data = fetch_property_data(
                        data_client,
                        property_id,
                        property_name,
                        config,
                        start_date=explicit_start,
                        end_date=explicit_end,
                        report_type=report_type,
                    )
                except RefreshError as e:
                    logging.warning("GA4 OAuth refresh failed for property %s: %s — skipping.", property_id, e)
                    _revoke_ga4_connection_for_property(property_id)
                    # the property's connection just got revoked; tell
                    # the user to reconnect (classifier: invalid_grant/RefreshError
                    # → connection_revoked). No-op on the cron path.
                    from lib.import_run_context import record_user_error

                    record_user_error(
                        f"GA4 OAuth refresh failed (invalid_grant): {e}",
                        connector_id="google_analytics_4",
                        detail=(property_name or team_metadata.get("team_title")),
                    )
                    property_token_dead = True
                    break

                total_rows += len(report_data)
                property_rows += len(report_data)

                # Track which dates have data for this team (any report_type counts).
                for row in report_data:
                    d = row.get("date")
                    if d:
                        all_dates_seen.add(d)
                        teams_with_data_by_date.setdefault(d, set()).add(team_metadata["team_esid"])

                # Save this report_type's rows to JSON.
                files_saved = save_property_json(report_data, property_id, team_metadata)
                total_files += files_saved

            if property_token_dead:
                continue
            if property_rows == 0:
                _note_if_property_dark(property_id, property_name, team_metadata.get("team_esid"))

        logging.info(
            f"Completed: Processed {len(properties)} properties, fetched {total_rows} rows, saved {total_files} files"
        )

        # Write no_data markers for dates where teams had no GA data.
        # When an explicit date range is used, generate markers for every
        # date in the range (not just dates the API returned data for).
        if explicit_start and explicit_end:
            from datetime import timedelta

            marker_dates = []
            current = datetime.strptime(explicit_start, "%Y-%m-%d")
            end_dt = datetime.strptime(explicit_end, "%Y-%m-%d")
            while current <= end_dt:
                marker_dates.append(current.strftime("%Y-%m-%d"))
                current += timedelta(days=1)
        else:
            # Convert GA4 YYYYMMDD dates to YYYY-MM-DD
            marker_dates = []
            for d in sorted(all_dates_seen):
                iso_date = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
                marker_dates.append(iso_date)

        for iso_date in marker_dates:
            # Convert back to YYYYMMDD to look up in teams_with_data_by_date
            ga_date = iso_date.replace("-", "")
            teams_on_date = teams_with_data_by_date.get(ga_date, set())
            # clean up stale markers before writing fresh ones.
            delete_stale_markers_for_date("*_ga_metrics", iso_date, teams_on_date)
            write_no_data_markers(iso_date, teams_on_date, "_ga_metrics", team_list=all_team_list)

    except Exception as e:
        logging.error(f"Failed to get GA metrics: {e}")
        raise


if __name__ == "__main__":
    # For standalone testing
    main({})
