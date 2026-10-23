"""
Google Ads App Campaign Data Fetcher

Fetches mobile app install campaign performance data from Google Ads API for APP campaigns (MULTI_CHANNEL).
Only campaigns associated with teams (via de_asset_google_ads_customers) are processed.

This script:
1. Loads Google Ads API credentials from /app/reporting_configs/google-ads.yaml or service account JSON
2. Fetches APP INSTALL campaign metrics from configured Google Ads customers
3. Calculates app install KPIs: cost per install, conversion rate, cost per conversion
4. Adds team metadata from de_asset_teams
5. Saves data as JSON files for later Elasticsearch indexing

Usage:
    python3 /app/python/import.py google_ads run_daily_imports --start_date=YYYY-MM-DD --end_date=YYYY-MM-DD
"""

import os
import sys
import json
import logging
import hashlib
import yaml
import time
from datetime import datetime, timedelta, timezone

# Add parent directory to path for lib imports
sys.path.insert(0, "/app/python")
import lib.logging as custom_logging
from lib.es_helpers import build_data_index_name, get_all_documents_paginated, indexed_doc_present, search_index
from lib.geocoding import _COUNTRY_CENTROIDS
from lib.no_data_markers import write_no_data_markers
from lib.cleanup_no_data_markers import delete_stale_markers_for_date
from import_scripts.google_ads.attribution import (
    get_linked_teams,
    plan_customer,
    prune_foreign_docs,
    prune_foreign_files,
)
from import_scripts.google_ads.attribution import team_metadata as attribution_team_metadata

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.oauth2 import service_account as google_service_account

logger = logging.getLogger(__name__)

GOOGLE_ADS_RETRY_DELAYS = [30, 60, 120]  # seconds for exponential backoff on quota errors


# GAQL is built by string interpolation of start_date/end_date (CLI /
# dashboard params) and dash-stripped customer ids. Inputs are internal and the
# query goes to Google's API, so risk is low, but validate before interpolating
# so a malformed/crafted value can't alter the GAQL.
def _validate_gaql_date(value, field="date"):
    """Return ``value`` if it is a YYYY-MM-DD date, else raise ValueError.

    strptime is the validation — anything it can't parse (wrong format, GAQL
    fragments, quotes) is rejected before the value reaches a query string.
    """
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError):
        raise ValueError(f"Invalid {field} {value!r}: expected YYYY-MM-DD")
    return value


def _validate_customer_id(value):
    """Return the dash-stripped customer id if it is all digits, else raise.

    Mirrors the existing ``customer_id.replace('-', '')`` normalization but
    asserts the result ``.isdigit()`` so a non-numeric id can't be interpolated
    into GAQL or sent as a customer_id.
    """
    normalized = str(value or "").replace("-", "").strip()
    if not normalized.isdigit():
        raise ValueError(f"Invalid Google Ads customer id: {value!r}")
    return normalized


# the TRANSIENT quota/rate-limit enum values on the Google Ads
# `quota_error` oneof. RESOURCE_EXHAUSTED / RESOURCE_TEMPORARILY_EXHAUSTED + the
# excessive-consumption variants are genuinely retryable; ACCESS_PROHIBITED is a
# POLICY block under the same oneof and must NOT be retried as quota.
_TRANSIENT_QUOTA_ENUMS = frozenset(
    {
        "RESOURCE_EXHAUSTED",
        "RESOURCE_TEMPORARILY_EXHAUSTED",
        "EXCESSIVE_SHORT_TERM_QUERY_RESOURCE_CONSUMPTION",
        "EXCESSIVE_LONG_TERM_QUERY_RESOURCE_CONSUMPTION",
    }
)


def _interruptible_sleep(total_seconds, cancel_check=None, slice_seconds=1.0):
    """sleep up to ``total_seconds``, but bail early when cancelled.

    The Google Ads quota-retry backoff used a single blocking ``time.sleep(delay)``
    (delays 30/60/120s across three fetch sites). A user Cancel set the flag but
    couldn't be observed until the sleep finished — so a rate-throttled run ignored
    Cancel for minutes, never returned, and never released its import lock,
    leaving an orphaned lock until the 30-min TTL.

    Sleep in short slices and poll ``cancel_check`` between them, returning True the
    moment cancellation is observed (the caller then aborts the retry and returns,
    so its ``finally`` releases the lock cleanly). Returns False when the full delay
    elapsed without a cancel. ``cancel_check=None`` → a plain, uninterruptible sleep
    (the standalone ``__main__`` path, which has no progress run to cancel).
    """
    if cancel_check is None:
        time.sleep(total_seconds)
        return False
    remaining = float(total_seconds)
    while remaining > 0:
        if cancel_check():
            return True
        nap = slice_seconds if remaining > slice_seconds else remaining
        time.sleep(nap)
        remaining -= nap
    return cancel_check()


def _is_quota_error(ex):
    """Return True only for a TRANSIENT quota/rate-limit GoogleAdsException.

    key on the structured error-code ENUM, not the message text. The
    old substring match (``quota``/``rate``/``resource exhausted``/``too many``)
    misclassified any error whose message merely contained one of those words —
    most damagingly QueryError 59 ("issue sepa-RATE requests against each client
    account…"), which contains "rate" and was retried 3× as a fake quota for ~7
    minutes per run before failing (root cause masked).

    The authoritative signal is the ``quota_error`` oneof set to one of the
    transient enum values (``_TRANSIENT_QUOTA_ENUMS``). ``ACCESS_PROHIBITED`` —
    also a ``quota_error`` value — is a policy block, NOT retryable, so it does
    not count. When the error_code can't be introspected (older protos / odd
    wrappers), fall back to a TIGHT message check on whole, unambiguous phrases
    only (never the bare token "rate"). Never raises.
    """
    try:
        errors = ex.failure.errors
    except Exception:  # noqa: BLE001 — malformed exception → not a quota error
        return False
    for error in errors or []:
        code_name = _ads_error_code_name(getattr(error, "error_code", None))
        if code_name:
            # code_name looks like "quota_error=RESOURCE_TEMPORARILY_EXHAUSTED".
            field, _, value = code_name.partition("=")
            if field == "quota_error":
                if value in _TRANSIENT_QUOTA_ENUMS:
                    return True
                # A non-transient quota_error (e.g. ACCESS_PROHIBITED) → not retryable.
                continue
            # A non-quota error code (request_error / authorization_error / …) is
            # never a quota error — do NOT fall through to message matching, which
            # is what produced the "rate"-in-"separate" false positive.
            continue
        # No introspectable code → tight message fallback on unambiguous phrases.
        msg = (getattr(error, "message", "") or "").lower()
        if any(kw in msg for kw in ("quota", "resource exhausted", "rate limit", "too many requests")):
            return True
    return False


def _describe_google_ads_exception(ex):
    """render a GoogleAdsException's STRUCTURED detail for the log.

    The retry path only ever logged the generic "quota error ... sleeping" line,
    never the actual error. ``_is_quota_error`` matches on the message TEXT
    (substrings ``quota``/``rate``/``resource exhausted``/``too many``), which can
    misclassify a non-transient error whose message merely contains one of those
    words — and then we back off 30/60/120s and give up with no idea what really
    failed. This surfaces each underlying error's CODE (the error-code enum field
    name + value, the authoritative signal), its message, and the request_id, so
    an operator can tell a real ``RESOURCE_TEMPORARILY_EXHAUSTED`` from e.g. a
    developer-token / permission / login-customer mismatch.

    Returns a one-line string. Best-effort: never raises (it is a diagnostic).
    """
    parts = []
    try:
        for error in ex.failure.errors:
            code_name = ""
            try:
                # error.error_code is a oneof; the SET field's name IS the error
                # category (e.g. "quota_error"), its value the specific enum.
                ec = error.error_code
                set_field = ec.WhichOneof("error_code") if hasattr(ec, "WhichOneof") else None
                if set_field:
                    code_name = f"{set_field}={getattr(ec, set_field)}"
            except Exception:  # noqa: BLE001 — diagnostic only
                code_name = ""
            parts.append(f"[{code_name or 'UNKNOWN_CODE'}] {error.message}")
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        parts.append(f"(could not parse failure.errors: {exc})")
    request_id = getattr(ex, "request_id", "") or ""
    detail = " | ".join(parts) if parts else "(no errors in failure)"
    return f"{detail} (request_id={request_id})" if request_id else detail


def _normalize_campaign_datetime(value):
    """Normalize a Google Ads campaign start/end datetime for the ES `date` mapping.

    Google Ads returns `campaign.start_date_time` / `end_date_time` as either an
    empty string (no end date set) or the space-separated form
    ``YYYY-MM-DD HH:MM:SS``. The ES mapping is ``strict_date_optional_time``,
    which rejects both: empty strings ("cannot parse empty datetime") and the
    space separator (it requires the ISO-8601 ``T``). Convert the space to ``T``
    and map empty/missing values to ``None`` so the field is simply omitted.

    Args:
        value: Raw datetime string from the Google Ads API (may be empty/None).

    Returns:
        str | None: ISO-8601 datetime, or None if no value was set.
    """
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    return value.replace(" ", "T", 1)


# Config file paths
GOOGLE_ADS_CONFIG_PATH = "/app/reporting_configs/google-ads.yaml"

# per-user OAuth shares the verified YouTube/GA4 Google client.
# The shared service account is retired (no google-ads-sa.json on disk). OAuth
# carries the per-user IDENTITY; developer_token + login_customer_id still come
# from google-ads.yaml regardless of auth method, and are carried
# through the OAuth config by resolve_google_ads_oauth_config.
GOOGLE_ADS_OAUTH_CLIENT_PATH = "/app/reporting_configs/youtube-oauth-client.json.enc"


def load_google_ads_credentials():
    """
    Load the Google Ads API **developer-token base config** from YAML.

    the shared service account is retired. There is no longer a
    run-level SA identity — every customer authenticates with its own per-user
    OAuth token (``resolve_google_ads_oauth_config``). This function no longer
    returns a usable standalone client config; it returns only the pieces that
    are required for EVERY Google Ads call irrespective of auth method
    (``developer_token``, optional ``login_customer_id``, ``use_proto_plus``).
    The OAuth identity (refresh_token / client_id / client_secret) is layered on
    top per customer.

    Returns:
        dict: developer-token base config (no identity).

    Raises:
        FileNotFoundError: If the YAML config file doesn't exist.
        ValueError: If the developer token is missing.
    """
    if not os.path.exists(GOOGLE_ADS_CONFIG_PATH):
        raise FileNotFoundError(
            f"Google Ads config file not found: {GOOGLE_ADS_CONFIG_PATH}\n" "Run: bin/scripts/install_google_ads_api.sh"
        )

    with open(GOOGLE_ADS_CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f) or {}

    # env-var first (SSM-injected), YAML fallback. This INVERTS the
    # previous YAML-first order so an SSM-injected token takes effect without
    # editing the file. No env var is set in prod today, so this is a no-op
    # there until the SSM parameters land.
    from lib.google_oauth_client import load_google_ads_developer_token

    developer_token = load_google_ads_developer_token(yaml_config=config)
    if not developer_token:
        raise ValueError(
            "Developer token required for Google Ads API. Add to "
            f"{GOOGLE_ADS_CONFIG_PATH} or set GOOGLE_ADS_DEVELOPER_TOKEN environment variable"
        )

    base_config = {
        "developer_token": developer_token,
        "use_proto_plus": True,
    }
    login_customer_id = config.get("login_customer_id")
    if login_customer_id:
        base_config["login_customer_id"] = login_customer_id

    logging.info("✓ Google Ads developer-token base config loaded (OAuth-only; service account retired)")
    return base_config


def resolve_google_ads_oauth_config(customer_id, base_config, team_connection_esid=None):
    """OAuth-first per-customer credential selector.

    Returns a GoogleAdsClient config dict authenticated with the customer's
    per-user OAuth refresh token when an ACTIVE ``google_ads`` connection
    exists for ``customer_id``, or ``None`` when it does not — in which case
    the customer is SKIPPED (the shared service account was retired;
    there is no fallback identity). Mirrors ``ga_config.resolve_ga4_credentials``.

    ``team_connection_esid`` is the importing team's own
    ``google_ads_connection_esid`` stamp. When set, the credential lookup is
    scoped to that connection so a foreign principal's token is never used for
    this team's import (see
    ``ConnectionService.get_google_ads_oauth_connection_esid``). When ``None``
    (a team predating the stamp), the lookup falls back to the legacy
    principal-blind customer-id match.

    OAuth replaces only the IDENTITY. The returned config is ``base_config``
    minus the service-account ``json_key_file_path`` plus the OAuth keys
    (refresh_token / client_id / client_secret); ``developer_token`` and
    ``login_customer_id`` are inherited from ``base_config`` because the
    Google Ads API requires the developer token regardless of auth method
    .

    Fail-safe: any resolution error returns ``None`` (fall back to the service
    account) rather than raising, so a connection-lookup or token-decrypt
    hiccup never breaks the nightly import. The decision logic is pure enough
    to unit-test without a live Google client — -GADS-OAUTH-CRED-001.
    """
    cid = str(customer_id or "").strip()
    if not cid:
        return None
    try:
        from asset_management.web.services.connection_service import ConnectionService
    except ImportError:  # pragma: no cover - import-env specific
        logging.warning("resolve_google_ads_oauth_config: ConnectionService unavailable; using service account")
        return None

    connection_esid = ConnectionService.get_google_ads_oauth_connection_esid(cid, prefer_connection_esid=team_connection_esid)
    if not connection_esid:
        return None  # no usable OAuth connection for this team → skip + surface (no SA)

    try:
        refresh_token = ConnectionService.get_decrypted_token(connection_esid)
    except Exception as e:  # noqa: BLE001 — never let a bad token break the import
        logging.warning("resolve_google_ads_oauth_config: token decrypt failed for %s: %s", connection_esid, e)
        return None
    if not refresh_token:
        return None  # connection resolved but no usable token → SA fallback

    try:
        # env-var first (SSM-injected), on-disk fallback.
        from lib.google_oauth_client import load_google_oauth_client

        client = load_google_oauth_client()
    except Exception as e:  # noqa: BLE001
        logging.warning("resolve_google_ads_oauth_config: failed to load OAuth client: %s", e)
        return None

    # Carry developer_token from the SA base config; swap the SA identity
    # (json_key_file_path) for the OAuth user identity.
    #
    # do NOT inherit the SERVICE-ACCOUNT login_customer_id here. For a
    # per-user OAuth grant our MCC is irrelevant: the user operates under THEIR
    # OWN manager. If the target customer is a CLIENT under the user's manager,
    # the call needs THAT manager as login-customer-id, or Google returns
    # USER_PERMISSION_DENIED ("the manager's customer id must be set in the
    # 'login-customer-id' header"). Discover the right login-customer-id from
    # the user's own accessible customers (fail-safe: None on any error → a
    # direct/standalone account still works without a login-customer-id).
    oauth_config = {
        "developer_token": (base_config or {}).get("developer_token"),
        "use_proto_plus": True,
        "refresh_token": refresh_token,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
    }
    discovered = resolve_login_customer_id(oauth_config, cid)
    if discovered:
        oauth_config["login_customer_id"] = discovered

    logging.info(
        "✓ Google Ads OAuth credentials resolved for customer %s (connection %s, login_customer_id=%s)",
        cid,
        connection_esid,
        discovered or "<none>",
    )
    return oauth_config


def resolve_team_oauth_config(customer_id, base_config, team_doc):
    """the OAuth config for ``customer_id``, authenticated with the
    importing team's OWN connection when its ``google_ads_connection_esid``
    stamp is usable.

    Returns ``(oauth_config, fell_back)``. When the team has a stamp but it
    cannot be used for this customer — deleted, inactive, untokened, or for a
    different customer (a team links several customers but carries one stamp)
    — this falls back to the legacy customer-id match and reports
    ``fell_back=True`` so the caller warns and records it. the team's call
    (2026-09-26): an import that works today must not start failing because of
    a stale stamp. With no stamp the legacy match is used, as before.
    """
    stamp = str((team_doc or {}).get("google_ads_connection_esid") or "").strip() or None
    if stamp is None:
        return resolve_google_ads_oauth_config(customer_id, base_config), False
    oauth_config = resolve_google_ads_oauth_config(customer_id, base_config, team_connection_esid=stamp)
    if oauth_config:
        return oauth_config, False
    return resolve_google_ads_oauth_config(customer_id, base_config), True


def resolve_login_customer_id(oauth_config, target_customer_id):
    """discover the correct ``login-customer-id`` for a per-user OAuth
    call against ``target_customer_id``.

    For an OAuth identity, the right manager is one the USER can act under, not
    our service-account MCC. Resolution, using the user's own token:

      1. ``CustomerService.list_accessible_customers`` (needs NO login-customer-id)
         returns every customer the token is directly a user on — own accounts
         plus any managers they belong to.
      2. If the target is itself directly accessible → it acts as itself; return
         it (a standalone account reads fine with login-customer-id == its own id;
         Google also accepts no header there, but being explicit is harmless).
      3. Otherwise the target is a CLIENT under one of the accessible managers.
         For each accessible customer, walk ``customer_client`` and return the
         first manager whose tree contains the target.

    Returns the bare (dash-stripped) login-customer-id string, or ``None`` when
    it can't be determined (caller then sends no header — correct for a direct
    account, and a clean failure for a genuinely unreachable one). NEVER raises:
    discovery is best-effort and must not break the importer or the probe.
    """
    # target is interpolated into the customer_client GAQL below;
    # require all-digits (fail-safe to None, since this resolver never raises).
    target = str(target_customer_id or "").replace("-", "").strip()
    if not target.isdigit():
        return None
    # Build a client WITHOUT a login-customer-id for the discovery calls.
    disc_config = {k: v for k, v in (oauth_config or {}).items() if k != "login_customer_id"}
    try:
        client = GoogleAdsClient.load_from_dict(disc_config)
        customer_service = client.get_service("CustomerService")
        accessible = customer_service.list_accessible_customers()
        # resource_names look like "customers/1234567890".
        accessible_ids = [rn.split("/")[-1] for rn in getattr(accessible, "resource_names", []) or []]
    except Exception as e:  # noqa: BLE001 — discovery is best-effort
        logging.warning("resolve_login_customer_id: list_accessible_customers failed for %s: %s", target, e)
        return None

    if not accessible_ids:
        return None

    # Case 2 — target is directly accessible: it acts as itself.
    if target in accessible_ids:
        return target

    # Case 3 — target is a client under one of the accessible managers. Query
    # each accessible customer's client tree (customer_client) for the target.
    ga_service = client.get_service("GoogleAdsService")
    query = (
        "SELECT customer_client.id FROM customer_client "
        f"WHERE customer_client.id = {target} AND customer_client.status = 'ENABLED'"
    )
    for manager_id in accessible_ids:
        try:
            resp = ga_service.search(customer_id=manager_id, query=query)
            for _row in resp:  # any row means the target is in this manager's tree
                logging.info("target customer %s resolved under manager %s", target, manager_id)
                return manager_id
        except Exception as e:  # noqa: BLE001 — a manager we can't query is just skipped
            logging.debug("resolve_login_customer_id: probe of manager %s for %s skipped: %s", manager_id, target, e)
            continue

    logging.warning(
        "resolve_login_customer_id: target %s not found under any of the user's "
        "accessible customers %s; sending no login-customer-id.",
        target,
        accessible_ids,
    )
    return None


def list_leaf_customer_ids(client, manager_customer_id):
    """list the LEAF (non-manager) client account ids under an MCC.

    A manager account (MCC) has no metrics of its own — a metrics query against
    it returns ``query_error=59`` (METRICS_CANNOT_BE_REQUESTED_FOR_MANAGER_ACCOUNT).
    Metrics live only on the leaf client accounts beneath it, so the importer must
    fan out to each leaf. This walks ``customer_client`` from the manager and
    returns every ENABLED descendant that is itself NOT a manager
    (``customer_client.manager = FALSE``), dash-stripped.

    ``client`` must already carry the manager as ``login_customer_id`` (the
    importer's per-customer client does — for an MCC connected directly,
    ``resolve_login_customer_id`` returns the MCC itself). ``customer_client``
    enumerates the WHOLE subtree (``customer_client.level >= 1``), so nested
    sub-managers are traversed transparently — only their leaf descendants come
    back, which is exactly what we want.

    Returns a de-duped, sorted list of leaf customer-id strings, or ``[]`` when
    the manager has no reachable leaves or the query fails. NEVER raises —
    fan-out discovery is best-effort and must not break the importer.
    """
    mgr = str(manager_customer_id or "").replace("-", "").strip()
    if not mgr:
        return []
    query = (
        "SELECT customer_client.id, customer_client.manager, customer_client.level "
        "FROM customer_client "
        "WHERE customer_client.status = 'ENABLED' AND customer_client.level >= 1 "
        "AND customer_client.manager = FALSE"
    )
    try:
        ga_service = client.get_service("GoogleAdsService")
        resp = ga_service.search(customer_id=mgr, query=query)
        leaves = set()
        for row in resp:
            leaf_id = str(getattr(row.customer_client, "id", "") or "").strip()
            if leaf_id and leaf_id != mgr:
                leaves.add(leaf_id)
        out = sorted(leaves)
        logging.info("MCC %s expanded to %d leaf client account(s): %s", mgr, len(out), out)
        return out
    except Exception as e:  # noqa: BLE001 — discovery is best-effort
        logging.warning("list_leaf_customer_ids: customer_client walk failed for MCC %s: %s", mgr, e)
        return []


def is_manager_account(client, customer_id):
    """return True when ``customer_id`` is a manager (MCC) account.

    Reads the boolean ``customer.manager`` on the ``customer`` resource (a
    metadata field, queryable even on an MCC — only METRICS are rejected on a
    manager). ``client`` must carry the right ``login_customer_id`` for the
    query (the importer's per-customer client does).

    Returns ``False`` on any error or when the field can't be read — a False
    verdict keeps the EXISTING direct-query behavior (no regression), so a
    detection blip degrades to "treat as a normal account" rather than breaking
    the import. NEVER raises.
    """
    cid = str(customer_id or "").replace("-", "").strip()
    if not cid:
        return False
    try:
        ga_service = client.get_service("GoogleAdsService")
        resp = ga_service.search(
            customer_id=cid,
            query="SELECT customer.manager FROM customer LIMIT 1",
        )
        for row in resp:
            return bool(getattr(row.customer, "manager", False))
    except Exception as e:  # noqa: BLE001 — detection is best-effort
        logging.warning("is_manager_account: customer.manager probe failed for %s: %s", cid, e)
    return False


def resolve_metric_customer_ids(client, customer_id):
    """the list of customer ids to actually pull METRICS from for a
    connected ``customer_id``.

    * A direct (non-manager) account → ``[customer_id]`` (dash-stripped) — the
      existing behavior, unchanged.
    * A manager (MCC) account → its ENABLED leaf client accounts (``customer.manager``
      is True; metrics live on the leaves, not the MCC). If an MCC has no
      reachable leaves, returns ``[]`` so the caller skips it cleanly rather than
      querying the MCC and getting QueryError 59.

    ``client`` must carry the MCC/own customer as ``login_customer_id`` (the
    importer's per-customer client does). NEVER raises.
    """
    cid = str(customer_id or "").replace("-", "").strip()
    if not cid:
        return []
    if is_manager_account(client, cid):
        return list_leaf_customer_ids(client, cid)
    return [cid]


def _build_oauth_config_from_token(refresh_token):
    """build a Google Ads client config from a raw refresh token.

    Shared by the discovery path (where we have a connection's token but no
    customer id yet). Layers the per-user OAuth identity (refresh_token +
    client_id/secret from the OAuth client file) onto the developer-token base
    config, exactly as ``resolve_google_ads_oauth_config`` does — minus the
    login-customer-id (discovery calls must NOT pin one). Returns the config dict
    or ``None`` on any load failure (never raises)."""
    if not refresh_token:
        return None
    try:
        base_config = load_google_ads_credentials()
    except Exception as e:  # noqa: BLE001
        logging.warning("_build_oauth_config_from_token: base config unavailable: %s", e)
        return None
    try:
        # env-var first (SSM-injected), on-disk fallback.
        from lib.google_oauth_client import load_google_oauth_client

        client = load_google_oauth_client()
    except Exception as e:  # noqa: BLE001
        logging.warning("_build_oauth_config_from_token: failed to load OAuth client: %s", e)
        return None
    return {
        "developer_token": (base_config or {}).get("developer_token"),
        "use_proto_plus": True,
        "refresh_token": refresh_token,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
    }


def _descriptive_name_for_customer(client, customer_id):
    """best-effort human name for a customer id, or "" on any failure.

    ``client`` must carry a login-customer-id that can read this customer (its own
    id for a directly-accessible account, or its manager for a child). Reads
    ``customer.descriptive_name``. NEVER raises."""
    cid = str(customer_id or "").replace("-", "").strip()
    if not cid:
        return ""
    try:
        ga_service = client.get_service("GoogleAdsService")
        resp = ga_service.search(
            customer_id=cid,
            query="SELECT customer.descriptive_name FROM customer LIMIT 1",
        )
        for row in resp:
            return str(getattr(row.customer, "descriptive_name", "") or "")
    except Exception as e:  # noqa: BLE001 — name lookup is best-effort
        logging.debug("_descriptive_name_for_customer: lookup failed for %s: %s", cid, e)
    return ""


def list_accessible_customers_for_connection(connection_esid):
    """every Google Ads customer the connection's token can ADD to a
    team: directly-accessible non-manager accounts plus the leaf clients under
    any accessible manager (MCC).

    Discovery primitive for the wizard's "Discover from a Google account" mode.
    Uses the connection's OWN stored OAuth token (no login-customer-id), lists
    the accessible customers, and for each:
      * a non-manager account → itself (a linkable customer);
      * a manager (MCC) → its ENABLED leaf clients,
        since the MCC itself has no metrics and isn't a useful link target.
    De-dupes across managers (a client visible under two managers appears once).

    Returns a sorted list of ``{"id": <dash-stripped>, "name": <descriptive or "">,
    "manager": False}`` dicts (only linkable leaves/standalones — never a bare MCC).
    Returns ``[]`` on any resolution failure. NEVER raises (best-effort discovery).
    """
    if not connection_esid:
        return []
    try:
        from asset_management.web.services.connection_service import ConnectionService
    except ImportError:  # pragma: no cover - import-env specific
        logging.warning("list_accessible_customers_for_connection: ConnectionService unavailable")
        return []

    try:
        refresh_token = ConnectionService.get_decrypted_token(connection_esid)
    except Exception as e:  # noqa: BLE001
        logging.warning("list_accessible_customers_for_connection: token read failed: %s", e)
        return []
    oauth_config = _build_oauth_config_from_token(refresh_token)
    if not oauth_config:
        return []

    try:
        client = GoogleAdsClient.load_from_dict(oauth_config)
        customer_service = client.get_service("CustomerService")
        accessible = customer_service.list_accessible_customers()
        accessible_ids = [rn.split("/")[-1] for rn in getattr(accessible, "resource_names", []) or []]
    except Exception as e:  # noqa: BLE001 — discovery is best-effort
        logging.warning("list_accessible_customers_for_connection: list_accessible failed: %s", e)
        return []

    found = {}  # id -> name (de-dupe; a leaf under two managers appears once)
    for acc_id in accessible_ids:
        acc = str(acc_id).replace("-", "").strip()
        if not acc:
            continue
        # A client built with this account as login-customer-id can read it (and,
        # if it's a manager, walk its tree).
        cfg = dict(oauth_config)
        cfg["login_customer_id"] = acc
        try:
            scoped = GoogleAdsClient.load_from_dict(cfg)
        except Exception as e:  # noqa: BLE001
            logging.debug("client build failed for accessible %s: %s", acc, e)
            continue
        if is_manager_account(scoped, acc):
            for leaf in list_leaf_customer_ids(scoped, acc):
                found.setdefault(leaf, _descriptive_name_for_customer(scoped, leaf))
        else:
            found.setdefault(acc, _descriptive_name_for_customer(scoped, acc))

    return [{"id": cid, "name": found[cid] or "", "manager": False} for cid in sorted(found)]


def _ads_error_code_name(error_code):
    """diag — best-effort name of the set google.ads error_code oneof.

    ``error_code`` is a oneof (exactly one sub-field set, e.g.
    ``authorization_error`` / ``authentication_error`` / ``request_error``).
    Returns a string like ``authorization_error=USER_PERMISSION_DENIED`` or
    ``""`` when it can't be introspected (never raises — diagnostics must not
    break the probe). Works for proto-plus wrappers (``._pb``) and raw protos.
    """
    if error_code is None:
        return ""
    try:
        pb = getattr(error_code, "_pb", error_code)
        which = pb.WhichOneof("error_code")
        if not which:
            return ""
        enum_field = getattr(error_code, which, None)
        # enum_field is typically an IntEnum-like; .name gives the symbol.
        name = getattr(enum_field, "name", None) or str(enum_field)
        return f"{which}={name}"
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return ""


def probe_google_ads_customer_with_connection(connection_esid, customer_id):
    """probe a customer id using a CONNECTION's own OAuth token.

    The wizard calls this right after the user's OAuth grant lands, so a
    bogus-but-valid-format customer id is caught at the Authorize step instead
    of at Confirm — and crucially the probe uses the USER'S refresh token (the
    one just granted), NOT our MCC service account, so the error is
    OAuth-appropriate ("that account isn't accessible with the Google login you
    used") rather than the service-account "link it under our MCC" message.

    Returns a ``(ok, definitive, message)`` tuple mirroring the SA probe's
    classification (connector_validation_service.ProbeResult semantics):
      * ok=True               → the user's token can read the customer.
      * definitive=True       → auth/not-found: the grant can't reach this
                                customer (a real user error — block).
      * definitive=False      → infra/transient (quota, unavailable, missing
                                libs/config): fail-open, don't block on our flakiness.

    Reuses the proven OAuth client shape: this builds the same config the
    importer uses (resolve_google_ads_oauth_config), but keyed by the explicit
    connection_esid (not a principal-blind customer_id lookup), so it probes the
    exact token that just landed on THIS connection.
    """
    cid = str(customer_id or "").strip()
    if not cid:
        return (False, True, "customer_id is required")

    try:
        from asset_management.web.services.connection_service import ConnectionService
    except ImportError:
        return (False, False, "ConnectionService unavailable")

    try:
        refresh_token = ConnectionService.get_decrypted_token(connection_esid)
    except Exception as e:  # noqa: BLE001 — token read failure is infra (fail-open)
        return (False, False, f"token read failed: {e}")
    if not refresh_token:
        return (False, False, "connection has no token yet")

    # Build the OAuth client from the SA base config (developer_token +
    # login_customer_id) with the user's identity swapped in —
    # the same shape resolve_google_ads_oauth_config produces and that the
    # importer is live-verified to authenticate with.
    try:
        base_config = load_google_ads_credentials()
    except Exception as e:  # noqa: BLE001 — config load failure is infra
        return (False, False, f"base config unavailable: {e}")

    oauth_config = resolve_google_ads_oauth_config(cid, base_config)
    if not oauth_config:
        # No usable OAuth config resolved for this customer (e.g. token just
        # cleared). Infra/transient — don't hard-block the wizard.
        return (False, False, "no OAuth credentials resolved")

    try:
        client = GoogleAdsClient.load_from_dict(oauth_config)
        ga_service = client.get_service("GoogleAdsService")
        # Cheapest read scoped to the candidate customer.
        ga_service.search(
            customer_id=cid.replace("-", ""),
            query="SELECT customer.id FROM customer LIMIT 1",
        )
        return (True, False, "")
    except GoogleAdsException as e:  # type: ignore[misc]
        # include the error_code enum name (e.g.
        # USER_PERMISSION_DENIED, CUSTOMER_NOT_FOUND, NOT_ADS_USER), not just the
        # human message: the enum is what distinguishes "wrong customer id" from
        # "your login can't reach this account under our MCC context" — the two
        # most common definitive rejects on the per-user OAuth path.
        parts = []
        if getattr(e, "failure", None):
            for err in e.failure.errors:
                code_name = _ads_error_code_name(getattr(err, "error_code", None))
                parts.append(f"{code_name}: {err.message}" if code_name else err.message)
        msg = "; ".join(parts).lower() if parts else str(e).lower()
        infra_markers = ("quota", "rate", "resource exhausted", "too many", "internal", "deadline", "unavailable")
        if any(m in msg for m in infra_markers):
            return (False, False, f"Ads API transient error: {msg}")
        return (False, True, msg or "not authorized")
    except Exception as e:  # noqa: BLE001 — non-GoogleAds errors are infra (fail-open)
        return (False, False, f"probe error: {e}")


def get_google_ads_customers(customer_ids=None):
    """
    Get Google Ads customers from de_asset_google_ads_customers index.

    the old single-shot ``search_index(..., size=100)`` silently
    dropped every customer past the 100th (arbitrary ES order) and a scoped
    run filtered late in main(), so a customer that sorted past the cap was
    never imported with no error. The fetch is now paginated (no cap) and,
    when ``customer_ids`` is given, scoped at query time so only the
    requested customers are loaded at all.

    Args:
        customer_ids: Optional list of google_ads_customer_id values to scope
            the fetch to (the scheduler / wizard team-scoped path). None =
            every customer (cron behavior).

    Returns:
        list: List of customer documents with _id and _source
    """
    query = {"match_all": {}}
    if customer_ids:
        ids = [str(c).strip() for c in customer_ids if str(c).strip()]
        # Match either mapping shape: explicit keyword (bare field) or the
        # auto-mapped text+keyword twin (the trap).
        query = {
            "bool": {
                "should": [
                    {"terms": {"google_ads_customer_id": ids}},
                    {"terms": {"google_ads_customer_id.keyword": ids}},
                ],
                "minimum_should_match": 1,
            }
        }
    try:
        customers = get_all_documents_paginated("de_asset_google_ads_customers", query)
        logging.info(f"Found {len(customers)} Google Ads customers in Elasticsearch")
        return customers

    except Exception as e:
        logging.error(f"Failed to fetch Google Ads customers from Elasticsearch: {e}")
        return []


def get_team_by_google_ads_customer_esid(customer_esid):
    """
    Find team that references this Google Ads customer ESID.

    Args:
        customer_esid: Elasticsearch document ID of the Google Ads customer

    Returns:
        dict: Team metadata (team_title, team_esid, organization_title, etc.) or None
    """
    try:
        # Query teams index for team with matching google_ads_customer_esid.
        # a customer assigned to >1 team used to resolve to an
        # arbitrary `hits[0]` (size=1) — the import then landed on one team
        # and silently dropped the rest, nondeterministically (ES doc order
        # can flip on reindex). Pull several hits and sort by team_esid (_id)
        # so the chosen team is deterministic, and WARN when more than one
        # matches so the misconfiguration is visible.
        # Phase 1 — a team can now link MULTIPLE customers via the list
        # field team_google_ads_customer_esids; match EITHER that list OR the
        # legacy scalar google_ads_customer_esid (transition window) so customers
        # linked under the new multi-account path are found while pre-migration
        # teams that only carry the scalar still resolve. (Orthogonal to the
        # concern above, which is the inverse: one customer on >1 team.)
        results = search_index(
            index_name="de_asset_teams",
            query={
                "bool": {
                    "must": [
                        # bare fields — de_asset_teams is explicit
                        # keyword post-reindex; the .keyword subfields are gone
                        # (a term on them silently matches nothing, so this
                        # must-clause dropped EVERY team).
                        {"term": {"team_google_ads_integration_status": "Active"}},
                        {"term": {"ds_google_ads": "true"}},
                    ],
                    "should": [
                        # team_google_ads_customer_esids has been bare
                        # keyword since update_v19; brought the legacy
                        # scalar google_ads_customer_esid to the same shape, so
                        # both legs query bare now.
                        {"term": {"team_google_ads_customer_esids": customer_esid}},
                        {"term": {"google_ads_customer_esid": customer_esid}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            size=10,
        )

        hits = results.get("hits", {}).get("hits", [])
        if not hits:
            return None

        # Deterministic pick: lowest team_esid wins regardless of ES doc order.
        hits = sorted(hits, key=lambda h: h.get("_id", ""))

        team_doc = hits[0]
        team = team_doc["_source"]
        team["team_esid"] = team_doc["_id"]

        if len(hits) > 1:
            logging.warning(
                "Google Ads customer %s is assigned to %d teams — importing to "
                "%r (team_esid=%s) only; the others receive no data. "
                "Members: %s",
                customer_esid,
                len(hits),
                team.get("team_title", "Unknown"),
                team["team_esid"],
                ", ".join(
                    f"(team_esid={h['_id']}, team_title={h.get('_source', {}).get('team_title')!r}, "
                    f"workspace_esid={h.get('_source', {}).get('workspace_esid')!r})"
                    for h in hits
                ),
            )

        return {
            "team_esid": team.get("team_esid"),
            "team_title": team.get("team_title", "Unknown"),
            "team_url": team.get("team_url", "unknown"),
            "team_country": team.get("team_country", "unknown"),
            "team_city": team.get("team_city", "unknown"),
            "organization_title": team.get("organization_title", "Unknown"),
        }

    except Exception as e:
        logging.warning(f"Error looking up team for Google Ads customer {customer_esid}: {e}")
        return None


def leaf_directly_connected_to_team(leaf_customer_id, team_esid):
    """True when ``leaf_customer_id`` is ALSO a directly-connected
    Google Ads customer asset assigned to ``team_esid``.

    When an MCC and one of its leaf client accounts are BOTH connected as
    customer assets AND both assigned to the same team, the importer would write
    that leaf's metrics into the team's index twice: once via the MCC's
    leaf expansion, and once on the leaf asset's own direct pass. The two writes
    land in the same ``<team>__google_ads`` index under different
    ``google_ads_customer_esid`` values, so neither overwrites the other — the
    team's dashboard double-counts that account.

    This lets the MCC-expansion path skip a leaf that the leaf's OWN direct pass
    already covers for the same team. Scope is deliberately narrow (
    decision, "skip if direct + same team"): a direct asset on a DIFFERENT team
    is left alone (it imports into a different index — no duplication), and an
    UNassigned/unmatched direct asset does NOT trigger a skip (so the leaf is
    never silently dropped — its data still arrives via the MCC pass).

    Matches the asset on ``google_ads_customer_id.keyword`` (the bare text field
    silently returns 0 after analysis — the recurring trap) and
    reuses ``get_team_by_google_ads_customer_esid`` so "same team" is decided by
    the exact resolver the direct pass itself uses. Fail-OPEN: any lookup error
    returns False (keep the leaf — never drop data on a transient ES blip).
    """
    leaf = str(leaf_customer_id or "").replace("-", "").strip()
    want_team = str(team_esid or "").strip()
    if not leaf or not want_team:
        return False
    try:
        results = search_index(
            index_name="de_asset_google_ads_customers",
            query={"term": {"google_ads_customer_id.keyword": leaf}},
            size=20,
        )
        for hit in results.get("hits", {}).get("hits", []):
            direct_esid = hit.get("_id") or ""
            if not direct_esid:
                continue
            direct_team = get_team_by_google_ads_customer_esid(direct_esid)
            if direct_team and str(direct_team.get("team_esid") or "").strip() == want_team:
                return True
    except Exception as e:  # noqa: BLE001 — fail-open: keep the leaf on any error
        logging.warning(
            "leaf_directly_connected_to_team: lookup failed for leaf %s team %s: %s",
            leaf,
            want_team,
            e,
        )
    return False


# campaign-level GAQL covering ALL advertising_channel_types.
# FROM campaign (NOT ad_group_ad): PERFORMANCE_MAX has no ad_group_ad, so an
# ad-level query silently drops PMax. campaign.app_campaign_setting.* is safe to
# SELECT for every type (null for non-app). No advertising_channel_type filter —
# the connector serves the whole account, not just app-install campaigns.
_CAMPAIGN_GAQL_TEMPLATE = """
    SELECT
        campaign.id,
        campaign.name,
        campaign.advertising_channel_type,
        campaign.status,
        campaign.labels,
        campaign.app_campaign_setting.app_id,
        campaign.app_campaign_setting.app_store,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros,
        metrics.conversions,
        metrics.all_conversions,
        metrics.conversions_value,
        metrics.ctr,
        metrics.average_cpc,
        metrics.average_cpm,
        segments.date
    FROM campaign
    WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
    ORDER BY segments.date DESC
"""

# the advertising_channel_type enum name for app-install campaigns.
# app_installs / cost_per_install are only meaningful for these.
_APP_CHANNEL_TYPE = "MULTI_CHANNEL"


# per-customer cache of label resource_name → display name. Labels
# are a per-customer resource (unlike geo_target_constant, which is global), so
# the cache is keyed by normalized customer id. Process-lifetime, like
# _GEO_TARGET_COUNTRIES.
_CAMPAIGN_LABELS_BY_CUSTOMER = {}


def load_campaign_label_names(client, customer_id):
    """map this customer's label resource names to display names.

    ``campaign.labels`` returns RESOURCE NAMES (``customers/<cid>/labels/<id>``),
    not display names, so the importer resolves them against the ``label``
    resource once per customer per run. Deliberately unfiltered by label status
    so a label that is still attached to a campaign resolves even if it was
    since removed from the account's label list.

    Best-effort: any failure caches and returns ``{}`` (docs then fall back to
    the numeric label id — see ``_campaign_row_to_doc``) rather than failing the
    metrics fetch. Never raises.
    """
    cid = _validate_customer_id(customer_id)
    if cid in _CAMPAIGN_LABELS_BY_CUSTOMER:
        return _CAMPAIGN_LABELS_BY_CUSTOMER[cid]

    names = {}
    try:
        ga_service = client.get_service("GoogleAdsService")
        search_request = client.get_type("SearchGoogleAdsRequest")
        search_request.customer_id = cid
        search_request.query = "SELECT label.resource_name, label.name FROM label"
        for row in ga_service.search(request=search_request):
            resource_name = str(getattr(row.label, "resource_name", "") or "")
            name = str(getattr(row.label, "name", "") or "")
            if resource_name and name:
                names[resource_name] = name
        logging.info(f"loaded {len(names)} label name(s) for customer {cid}")
    except Exception as e:  # noqa: BLE001 — label names are best-effort metadata
        logging.warning(f"label name lookup failed for customer {cid}: {e} — docs will carry label ids")
        names = {}

    _CAMPAIGN_LABELS_BY_CUSTOMER[cid] = names
    return names


def _campaign_labels_for_row(row, label_names):
    """resolve one row's ``campaign.labels`` resource names to display
    names. An unresolvable resource name falls back to its trailing numeric id —
    visible in the data rather than silently dropped."""
    resolved = []
    for resource_name in getattr(row.campaign, "labels", None) or []:
        rn = str(resource_name)
        resolved.append((label_names or {}).get(rn) or rn.rsplit("/", 1)[-1])
    return resolved


def _campaign_row_to_doc(row, label_names=None):
    """map one campaign-grain GAQL row to a doc.

    Universal metrics (impressions/clicks/cost/conversions/ctr/cpc/cpm) are
    populated for every campaign type. The app-install-specific fields
    (app_installs, cost_per_install, app_id, app_store) are populated ONLY for
    MULTI_CHANNEL (app) campaigns so existing app dashboards don't regress and
    non-app campaigns don't carry a meaningless "install" reading. Shared by the
    main + quota-retry paths (was duplicated — the two drifted).
    """
    channel_type = row.campaign.advertising_channel_type.name
    cost = row.metrics.cost_micros / 1_000_000
    conversions = row.metrics.conversions
    clicks = row.metrics.clicks
    is_app = channel_type == _APP_CHANNEL_TYPE

    doc = {
        # Campaign info
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
        "campaign_channel_type": channel_type,
        "campaign_status": row.campaign.status.name,
        # display names of the campaign's labels (empty list when
        # unlabeled). Google reports only CURRENT labels, so each day's doc
        # carries the labels as of import time.
        "campaign_labels": _campaign_labels_for_row(row, label_names),
        # Universal metrics (every campaign type reports these)
        "impressions": row.metrics.impressions,
        "clicks": clicks,
        "cost_micros": row.metrics.cost_micros,
        "cost": cost,
        "conversions": conversions,
        "all_conversions": (row.metrics.all_conversions if hasattr(row.metrics, "all_conversions") else conversions),
        "conversions_value": getattr(row.metrics, "conversions_value", 0.0),
        "ctr": row.metrics.ctr,
        "average_cpc": (getattr(row.metrics, "average_cpc", 0) or 0) / 1_000_000,
        "average_cpm": (getattr(row.metrics, "average_cpm", 0) or 0) / 1_000_000,
        # Universal calculated metrics
        "conversion_rate": (conversions / clicks if clicks > 0 else 0),
        "cost_per_conversion": (cost / conversions if conversions > 0 else 0),
        # Date
        "date": row.segments.date,
    }

    if is_app:
        # App-install-specific fields — only meaningful for app campaigns.
        # conversions == app installs in an app campaign (the original KAN model).
        doc["app_id"] = row.campaign.app_campaign_setting.app_id if row.campaign.app_campaign_setting else ""
        doc["app_store"] = row.campaign.app_campaign_setting.app_store.name if row.campaign.app_campaign_setting else ""
        doc["app_installs"] = conversions
        doc["cost_per_install"] = cost / conversions if conversions > 0 else 0

    return doc


def fetch_video_campaign_performance(client, customer_id, start_date, end_date, cancel_check=None):
    """fetch performance for ALL campaign types at CAMPAIGN grain.

    (Name kept for caller stability.) Returns one dict per (campaign x day) with
    universal metrics for every advertising_channel_type; app-install fields are
    added only for MULTI_CHANNEL campaigns (see _campaign_row_to_doc).

    Args:
        client: GoogleAdsClient instance
        customer_id: Google Ads customer ID (format: 123-456-7890 or 1234567890)
        start_date: Start date string (YYYY-MM-DD)
        end_date: End date string (YYYY-MM-DD)

    Returns:
        list: List of campaign performance dicts (campaign grain, all types)
    """
    customer_id_normalized = _validate_customer_id(customer_id)
    start_date = _validate_gaql_date(start_date, "start_date")
    end_date = _validate_gaql_date(end_date, "end_date")
    ga_service = client.get_service("GoogleAdsService")
    query = _CAMPAIGN_GAQL_TEMPLATE.format(start_date=start_date, end_date=end_date)

    search_request = client.get_type("SearchGoogleAdsRequest")
    search_request.customer_id = customer_id_normalized
    search_request.query = query

    # resolve label resource names → display names once per customer
    # (cached). Best-effort: {} on failure, docs then carry numeric label ids.
    label_names = load_campaign_label_names(client, customer_id_normalized)

    try:
        response = ga_service.search(request=search_request)
        campaigns_data = [_campaign_row_to_doc(row, label_names) for row in response]
        logging.info(
            f"✓ Fetched {len(campaigns_data)} campaign records (all types) for customer {customer_id} "
            f"({start_date} to {end_date})"
        )
        return campaigns_data

    except GoogleAdsException as ex:
        # log the raw structured error BEFORE the quota-vs-not branch,
        # so a misclassified non-quota error (matched on message text) is visible.
        logging.error(f"GoogleAdsException (campaign) for customer {customer_id}: {_describe_google_ads_exception(ex)}")
        if _is_quota_error(ex):
            for attempt, delay in enumerate(GOOGLE_ADS_RETRY_DELAYS, start=1):
                logging.warning(
                    f"Google Ads quota error for customer {customer_id} "
                    f"(attempt {attempt}/{len(GOOGLE_ADS_RETRY_DELAYS)}). Sleeping {delay}s..."
                )
                if _interruptible_sleep(delay, cancel_check):
                    logging.info(f"cancel observed during quota backoff for {customer_id}; aborting retries.")
                    return []
                try:
                    response = ga_service.search(request=search_request)
                    campaigns_data = [_campaign_row_to_doc(row, label_names) for row in response]
                    logging.info(f"✓ Fetched {len(campaigns_data)} records after retry for customer {customer_id}")
                    return campaigns_data
                except GoogleAdsException:
                    pass
            logging.error(f"Google Ads quota error for customer {customer_id} not resolved after retries.")
        else:
            for error in ex.failure.errors:
                logging.error(f"  Error: {error.message}")
                if error.location:
                    for field in error.location.field_path_elements:
                        logging.error(f"    Field: {field.field_name}")
        return []

    except Exception as e:
        logging.error(f"Failed to fetch campaigns for customer {customer_id}: {e}")
        return []


# Cache for geo_target_constant country lookups (criterion_id → {name, country_code})
_GEO_TARGET_COUNTRIES = None


def load_geo_target_countries(client, customer_id):
    """Load all country geo target constants from Google Ads API.

    Maps criterion IDs (e.g. 2356) to country name and ISO2 code.
    Results are cached for the lifetime of the process.
    """
    global _GEO_TARGET_COUNTRIES
    if _GEO_TARGET_COUNTRIES is not None:
        return _GEO_TARGET_COUNTRIES

    customer_id_normalized = _validate_customer_id(customer_id)
    ga_service = client.get_service("GoogleAdsService")

    query = """
        SELECT
            geo_target_constant.id,
            geo_target_constant.name,
            geo_target_constant.country_code,
            geo_target_constant.canonical_name
        FROM geo_target_constant
        WHERE geo_target_constant.target_type = 'Country'
          AND geo_target_constant.status = 'ENABLED'
    """

    try:
        search_request = client.get_type("SearchGoogleAdsRequest")
        search_request.customer_id = customer_id_normalized
        search_request.query = query
        response = ga_service.search(request=search_request)

        _GEO_TARGET_COUNTRIES = {}
        for row in response:
            cid = row.geo_target_constant.id
            _GEO_TARGET_COUNTRIES[cid] = {
                "name": row.geo_target_constant.name,
                "country_code": row.geo_target_constant.country_code,
                "canonical_name": row.geo_target_constant.canonical_name,
            }
        logging.info(f"Loaded {len(_GEO_TARGET_COUNTRIES)} geo target countries")
    except Exception as e:
        logging.warning(f"Failed to load geo target countries: {e}")
        _GEO_TARGET_COUNTRIES = {}

    return _GEO_TARGET_COUNTRIES


def _geo_row_to_doc(row):
    """map one geographic_view row to a geo doc, or None to skip.

    Covers all campaign types now (no MULTI_CHANNEL filter). Carries a generic
    ``conversions`` field; ``app_installs`` (== conversions) is kept only for app
    campaigns so the existing app geo-map chart doesn't regress. Shared by the
    main + retry paths.

    Keeps every row with activity (impressions, clicks or conversions). Until
    2026-10-23 a row with zero conversions was dropped, so per-country
    impressions only existed for converting country-days and no geo doc
    carried clicks at all — anything summing reach or clicks by country
    (the dashboards' country maps, the country x month aggregate export) was
    reading a converting-days-only subset. Only a row with no country
    criterion, or no activity at all, is skipped.
    """
    criterion_id = row.geographic_view.country_criterion_id
    if not criterion_id:
        return None
    conversions = row.metrics.conversions
    impressions = row.metrics.impressions
    clicks = row.metrics.clicks
    if conversions <= 0 and impressions <= 0 and clicks <= 0:
        return None
    channel_type = row.campaign.advertising_channel_type.name
    doc = {
        "record_type": "geo",
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
        "campaign_channel_type": channel_type,
        "geo_criterion_id": criterion_id,
        "conversions": conversions,
        "impressions": impressions,
        "clicks": clicks,
        "cost": row.metrics.cost_micros / 1_000_000,
        "date": row.segments.date,
    }
    if channel_type == _APP_CHANNEL_TYPE:
        doc["app_installs"] = conversions
    return doc


def fetch_geo_performance(client, customer_id, start_date, end_date, cancel_check=None):
    """Fetch country-level geographic performance for every campaign.

    Queries the geographic_view resource, which returns impressions, clicks,
    conversions and cost broken down by the country where the user was
    physically present (LOCATION_OF_PRESENCE). Each row represents one
    campaign × country × day.

    Returns a list of dicts ready to be saved as JSON and indexed to ES.
    The caller is responsible for adding team/customer metadata and resolving
    country names via load_geo_target_countries().
    """
    customer_id_normalized = _validate_customer_id(customer_id)
    start_date = _validate_gaql_date(start_date, "start_date")
    end_date = _validate_gaql_date(end_date, "end_date")
    ga_service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.advertising_channel_type,
            geographic_view.country_criterion_id,
            geographic_view.location_type,
            metrics.conversions,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            segments.date
        FROM geographic_view
        WHERE geographic_view.location_type = 'LOCATION_OF_PRESENCE'
          AND segments.date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY segments.date DESC
    """

    try:
        search_request = client.get_type("SearchGoogleAdsRequest")
        search_request.customer_id = customer_id_normalized
        search_request.query = query
        response = ga_service.search(request=search_request)

        geo_data = [d for d in (_geo_row_to_doc(row) for row in response) if d]

        logging.info(
            f"✓ Fetched {len(geo_data)} geo performance records for customer {customer_id} " f"({start_date} to {end_date})"
        )
        return geo_data

    except GoogleAdsException as ex:
        # log the raw structured error BEFORE the quota-vs-not branch.
        logging.error(f"GoogleAdsException (geo) for customer {customer_id}: {_describe_google_ads_exception(ex)}")
        if _is_quota_error(ex):
            for attempt, delay in enumerate(GOOGLE_ADS_RETRY_DELAYS, start=1):
                logging.warning(
                    f"Google Ads quota error (geo) for customer {customer_id} "
                    f"(attempt {attempt}/{len(GOOGLE_ADS_RETRY_DELAYS)}). Sleeping {delay}s..."
                )
                if _interruptible_sleep(delay, cancel_check):
                    logging.info(f"cancel observed during geo quota backoff for {customer_id}; aborting retries.")
                    return []
                try:
                    response = ga_service.search(request=search_request)
                    geo_data = [d for d in (_geo_row_to_doc(row) for row in response) if d]
                    logging.info(f"✓ Fetched {len(geo_data)} geo records after retry for customer {customer_id}")
                    return geo_data
                except GoogleAdsException:
                    pass
            logging.error(f"Google Ads quota error (geo) for customer {customer_id} not resolved after retries.")
            return []
        logging.error(f"Google Ads API geo error for customer {customer_id}:")
        for error in ex.failure.errors:
            logging.error(f"  Error: {error.message}")
        return []
    except Exception as e:
        logging.error(f"Failed to fetch geo performance for customer {customer_id}: {e}")
        return []


def _metadata_row_to_doc(row, label_names=None):
    """One campaign row → a campaign_metadata doc (main + quota-retry paths).
    carries campaign_labels so the per-campaign team rules can be
    applied to every campaign of the account, active in the window or not."""
    return {
        "record_type": "campaign_metadata",
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
        "campaign_status": row.campaign.status.name,
        "campaign_start_date_time": _normalize_campaign_datetime(row.campaign.start_date_time),
        "campaign_end_date_time": _normalize_campaign_datetime(row.campaign.end_date_time),
        "campaign_channel_type": row.campaign.advertising_channel_type.name,
        "campaign_labels": _campaign_labels_for_row(row, label_names),
    }


def fetch_campaign_metadata(client, customer_id, cancel_check=None):
    """
    Fetch campaign-level metadata (start/end datetimes, status) for MULTI_CHANNEL campaigns.

    Queries FROM campaign directly. In Google Ads API v23, date fields on campaigns
    are named start_date_time / end_date_time (not start_date / end_date).
    Returns one doc per campaign with record_type='campaign_metadata'.

    Args:
        client: GoogleAdsClient instance
        customer_id: Google Ads customer ID

    Returns:
        list: List of campaign metadata dicts
    """
    customer_id_normalized = _validate_customer_id(customer_id)
    ga_service = client.get_service("GoogleAdsService")

    # metadata for ALL campaign types (was MULTI_CHANNEL-only).
    query = """
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.start_date_time,
            campaign.end_date_time,
            campaign.advertising_channel_type,
            campaign.labels
        FROM campaign
    """
    label_names = load_campaign_label_names(client, customer_id_normalized)

    try:
        search_request = client.get_type("SearchGoogleAdsRequest")
        search_request.customer_id = customer_id_normalized
        search_request.query = query
        response = ga_service.search(request=search_request)

        metadata = [_metadata_row_to_doc(row, label_names) for row in response]

        logging.info(f"✓ Fetched metadata for {len(metadata)} campaigns for customer {customer_id}")
        return metadata

    except GoogleAdsException as ex:
        # log the raw structured error BEFORE the quota-vs-not branch.
        logging.error(f"GoogleAdsException (metadata) for customer {customer_id}: {_describe_google_ads_exception(ex)}")
        if _is_quota_error(ex):
            for attempt, delay in enumerate(GOOGLE_ADS_RETRY_DELAYS, start=1):
                logging.warning(
                    f"Google Ads quota error (metadata) for customer {customer_id} "
                    f"(attempt {attempt}/{len(GOOGLE_ADS_RETRY_DELAYS)}). Sleeping {delay}s..."
                )
                if _interruptible_sleep(delay, cancel_check):
                    logging.info(
                        f"cancel observed during metadata quota backoff for {customer_id}; aborting retries."
                    )
                    return []
                try:
                    response = ga_service.search(request=search_request)
                    metadata = [_metadata_row_to_doc(row, label_names) for row in response]
                    logging.info(f"✓ Fetched {len(metadata)} metadata records after retry for customer {customer_id}")
                    return metadata
                except GoogleAdsException:
                    pass
            logging.error(f"Google Ads quota error (metadata) for customer {customer_id} not resolved after retries.")
        else:
            logging.error(f"Google Ads API error fetching campaign metadata for {customer_id}:")
            for error in ex.failure.errors:
                logging.error(f"  Error: {error.message}")
        return []
    except Exception as e:
        logging.error(f"Failed to fetch campaign metadata for {customer_id}: {e}")
        return []


def save_campaign_metadata(metadata_doc, customer_esid, team_esid):
    """
    Save a campaign metadata doc to JSON. Always overwrites so end dates stay current.

    Args:
        metadata_doc: Dict of campaign metadata
        customer_esid: Google Ads customer Elasticsearch ID
        team_esid: Team Elasticsearch ID
    """
    campaign_id = metadata_doc.get("campaign_id", "unknown")
    filename = f"metadata_{campaign_id}.json"

    dir_path = f"/app/data/google_ads/app_campaigns/{customer_esid}/{team_esid}"
    os.makedirs(dir_path, exist_ok=True)

    file_path = os.path.join(dir_path, filename)

    with open(file_path, "w") as f:
        json.dump(metadata_doc, f, indent=2, sort_keys=True)

    logging.debug(f"Saved metadata: {file_path}")


def save_geo_data(geo_doc, customer_esid, team_esid, date_str):
    """Save a geo performance document to JSON file.

    Files are stored alongside campaign JSON files in the same directory
    so they are picked up by send_json_to_elastic.py automatically.
    Filename prefix 'geo_' distinguishes them from ad-level campaign records.
    """
    campaign_id = geo_doc.get("campaign_id", "unknown")
    criterion_id = geo_doc.get("geo_criterion_id", "unknown")
    filename = f"geo_{date_str}_{campaign_id}_{criterion_id}.json"

    dir_path = f"/app/data/google_ads/app_campaigns/{customer_esid}/{team_esid}"
    os.makedirs(dir_path, exist_ok=True)

    file_path = os.path.join(dir_path, filename)

    # skip only when archived AND indexed (see save_campaign_data).
    # Geo docs are keyed by (campaign, geo_criterion, date); verify by
    # field-match since the sender's _id is an MD5 built at send time.
    imported_path = file_path.replace("/app_campaigns/", "/app_campaigns_imported/")
    if os.path.exists(imported_path):
        es_index = build_data_index_name(team_esid, "google_ads")
        if indexed_doc_present(
            es_index,
            match_fields={
                "record_type": "geo",
                "campaign_id": campaign_id,
                "geo_criterion_id": criterion_id,
                "date": date_str,
            },
        ):
            logging.debug(f"Skipping {filename} - already imported and indexed")
            return
        logging.warning(
            "recovery: %s is archived but no ES geo doc matches "
            "campaign_id=%s geo_criterion_id=%s date=%s in %s — re-writing.",
            filename,
            campaign_id,
            criterion_id,
            date_str,
            es_index,
        )

    with open(file_path, "w") as f:
        json.dump(geo_doc, f, indent=2, sort_keys=True)

    logging.debug(f"Saved geo: {file_path}")


def save_campaign_data(campaign_data, customer_esid, team_esid, date_str):
    """
    Save campaign performance data to JSON file.

    Args:
        campaign_data: Dict of campaign performance metrics
        customer_esid: Google Ads customer Elasticsearch ID
        team_esid: Team Elasticsearch ID
        date_str: Date string (YYYY-MM-DD)
    """
    # campaign grain: one file per (campaign, date); no ad_id.
    campaign_id = campaign_data.get("campaign_id", "unknown")
    filename = f"{date_str}_{campaign_id}.json"

    # Directory structure: /app/data/google_ads/app_campaigns/{customer_esid}/{team_esid}/
    dir_path = f"/app/data/google_ads/app_campaigns/{customer_esid}/{team_esid}"
    os.makedirs(dir_path, exist_ok=True)

    file_path = os.path.join(dir_path, filename)

    # Check if file already exists in _imported. the archive is NOT
    # proof the doc is in ES (send→archive isn't gated on send success), so skip
    # only when the file is archived AND ES actually holds the doc; otherwise
    # re-write so the next send recovers it. The sender's _id is an MD5 built at
    # send time, so verify by field-match (campaign_id+date) rather than
    # reconstructing the hash. ES check fires only on the archived branch.
    imported_path = file_path.replace("/app_campaigns/", "/app_campaigns_imported/")
    if os.path.exists(imported_path):
        es_index = build_data_index_name(team_esid, "google_ads")
        if indexed_doc_present(
            es_index,
            match_fields={"campaign_id": campaign_id, "date": date_str},
        ):
            logging.debug(f"Skipping {filename} - already imported and indexed")
            return
        logging.warning(
            "recovery: %s is archived but no ES doc matches " "campaign_id=%s date=%s in %s — re-writing for re-send.",
            filename,
            campaign_id,
            date_str,
            es_index,
        )

    # Write JSON file
    with open(file_path, "w") as f:
        json.dump(campaign_data, f, indent=2, sort_keys=True)

    logging.debug(f"Saved: {file_path}")


def main(params):
    """
    Main entry point from import.py.

    Args:
        params: Dict with command-line parameters:
                - start_date: Start date (YYYY-MM-DD), defaults to 30 days ago
                - end_date: End date (YYYY-MM-DD), defaults to yesterday
                - customer_ids: Optional comma-separated list of customer IDs to process
    """
    custom_logging.define("google-ads-get-video-campaigns")

    # Parse date parameters
    end_date = params.get("end_date")
    start_date = params.get("start_date")

    if not end_date:
        # Default to yesterday
        end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if not start_date:
        # Default to 30 days before end_date
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_date = (end_dt - timedelta(days=30)).strftime("%Y-%m-%d")

    logging.info(f"Fetching Google Ads video campaign data: {start_date} to {end_date}")

    # Load the Google Ads developer-token base config (the shared
    # service account is retired — there is no run-level SA client any more).
    # Each customer authenticates with its OWN per-user OAuth token; this base
    # only carries the developer_token + login_customer_id every call needs.
    try:
        base_credentials = load_google_ads_credentials()
    except Exception as e:
        # re-raise. This used to log and `return`, exiting rc 0, so a
        # missing/broken developer token was recorded by the Report Scheduler as a
        # SUCCESSFUL import of zero rows. A credentials failure is a hard failure,
        # not an empty result: nothing downstream can run without it.
        logging.error(f"Failed to load credentials: {e}")
        raise

    # Get Google Ads customers from Elasticsearch. when a
    # --customer_ids scope is present (scheduler / wizard team-scoped runs),
    # it now scopes the ES fetch itself instead of filtering after a capped
    # global fetch; the in-Python filter below stays as an exact-match safety
    # net over the query's mapping-tolerant should-clause.
    # require an explicit customer scope BEFORE the ES fetch. Cron is
    # retired (deployment/03_provision_cron_jobs.sh migrated the importer jobs to
    # the Report Scheduler), the manual Import Data Page always sends
    # the selected team, and backfill reuses the scheduler dialog — so no legitimate
    # caller omits --customer_ids. An absent flag must fail loudly rather than fetch
    # every tenant's customers (the cross-tenant exposure class behind
    # ).
    #
    # ImportService._resolve_scope_arg (import_service.py:450-451) DELIBERATELY
    # omits the flag when the selected team resolves to no Google Ads customer, and
    # documents that it relies on this guard to surface a classifiable "must be
    # team-scoped" error instead of the UI hanging on a 0-byte log. A pre-check at
    # import_service.py:292-293 was added for this connector after "a swallowed
    # no-op (the one-team incident)" — this guard is the importer-side half.
    #
    # Note the team→customer_ids resolution (multi-account aware,
    # ReportScheduleService._resolve_team_to_google_ads_customer_ids unions the
    # plural team_google_ads_customer_esids with the legacy scalar) happens in the
    # DISPATCHER. By the time we see --customer_ids they are already concrete
    # google_ads_customer_id values, so nothing here reads the team doc.
    from lib.import_scope import require_scope, scope_resolution_error

    customer_ids_filter = require_scope(params, scope_param="customer_ids", connector_id="google_ads")
    filter_list = [cid.strip() for cid in customer_ids_filter.split(",") if cid.strip()]

    customer_docs = get_google_ads_customers(customer_ids=filter_list)

    if not customer_docs:
        # the scope flag was provided (require_scope proved it) but the
        # scoped ES fetch matched no customer doc. Pre-fix this warned and returned,
        # exiting rc 0, so the Report Scheduler recorded a SUCCESS for a run that
        # imported nothing — the failure mode that hid six dark teams under
        # . Mirrors Facebook's strict mode
        # (facebook/get_insights.py:1783-1794).
        raise scope_resolution_error(
            scope_param="customer_ids",
            scope_value=customer_ids_filter,
            connector_id="google_ads",
        )

    # filter_list is guaranteed non-empty by require_scope above.
    customer_docs = [doc for doc in customer_docs if doc["_source"].get("google_ads_customer_id") in filter_list]
    logging.info(f"Filtered to {len(customer_docs)} customers: {filter_list}")
    if not customer_docs:
        # the exact-match safety net emptied the list. This site had NO
        # zero-check at all: execution fell through into the customer loop, iterated
        # nothing, reached the end of main() and returned None -> rc 0, with no log
        # line whatsoever. The quietest instance of this bug found in the sweep.
        raise scope_resolution_error(
            scope_param="customer_ids",
            scope_value=customer_ids_filter,
            connector_id="google_ads",
        )

    total_campaigns = 0
    teams_with_data_by_date = {}  # {date_str: set(team_esid)}
    all_team_list = []  # [(team_esid, team_title)] for marker writing

    # on-demand cancel support. When the dashboard kickoff threads a
    # --progress_run_id, check between customers whether the user clicked Cancel
    # and stop promptly (keeping data already fetched). No-op on cron (no run_id);
    # fail-soft probe never aborts an import the user didn't cancel.
    progress_run_id = params.get("progress_run_id")

    def _cancel_requested():
        if not progress_run_id:
            return False
        try:
            from lib.import_progress_service import ImportProgressService

            return ImportProgressService.is_cancelling(progress_run_id)
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("gads get_campaign_data is_cancelling raised: %s — proceeding", exc)
            return False

    def _record_not_connected(customer_title):
        """surface an unconnected customer as a user-readable import
        error so the Report Scheduler "Errors" button shows it (not just a log
        line). No-op on the cron path (no run_id). Fail-soft: recording must
        never break the import. The raw string is crafted to classify as
        ``missing_credentials``; ``detail`` names the specific customer so the
        message reads "<customer>: This connector isn't fully configured …" and
        two unconfigured customers each get their own line."""
        if not progress_run_id:
            return
        try:
            from lib.import_progress_service import ImportProgressService

            ImportProgressService.record_import_error(
                run_id=progress_run_id,
                raw=(f"Google Ads customer {customer_title} has no OAuth token — " "the connection isn't authorized."),
                connector_id="google_ads",
                detail=customer_title,
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("gads _record_not_connected raised: %s — proceeding", exc)

    def _record_stale_stamp(customer_title):
        """surface a stale team connection stamp (the import fell back
        to another authorized connection) in the Report Scheduler "Errors"
        view. Non-fatal: the customer still imported. No-op on the cron path."""
        if not progress_run_id:
            return
        try:
            from lib.import_progress_service import ImportProgressService

            ImportProgressService.record_import_error(
                run_id=progress_run_id,
                raw=(
                    f"Google Ads customer {customer_title}: the team's saved connection is no longer "
                    "authorized; the import used another authorized connection. Reconnect Google Ads "
                    "under Data Connectors."
                ),
                connector_id="google_ads",
                detail=customer_title,
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("gads _record_stale_stamp raised: %s — proceeding", exc)

    def _record_not_linked(customer_title):
        """surface a customer that resolves to NO active, ds_google_ads
        team as a user-readable import error, so the skip reaches the Report
        Scheduler "Errors" surface instead of only the log. Sibling of
        _record_not_connected; every OTHER skip in this loop already records an
        error (OAuth-missing, billing) — the not-assigned branch was the only
        silent one. No-op on cron (no run_id); fail-soft. The raw string carries
        the "not linked to any active team" token so it classifies as
        ``no_data_source`` (NOT missing_credentials — the connection is live; the
        fix is to link the customer to a team, not to reconnect)."""
        if not progress_run_id:
            return
        try:
            from lib.import_progress_service import ImportProgressService

            ImportProgressService.record_import_error(
                run_id=progress_run_id,
                raw=(f"Google Ads customer {customer_title} is not linked to any active team " "with Google Ads enabled."),
                connector_id="google_ads",
                detail=customer_title,
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("gads _record_not_linked raised: %s — proceeding", exc)

    def _report_unattributed(customer_title, declined, plan):
        """one report per customer listing the campaigns no team's
        rules claim (or several do) and the linked teams with no rules, as an
        import error (Report Scheduler "Errors") as well as the log."""
        if not declined and not any("no campaign rules" in p for p in plan.problems):
            return
        parts = []
        if declined:
            names = sorted(f"{name} ({reason})" for name, reason in declined.values())
            shown = ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
            parts.append(f"{len(names)} campaign(s) match no team's campaign rules or several: {shown}")
        parts.extend(p for p in plan.problems if "no campaign rules" in p)
        message = f"Google Ads campaign unattributed on customer {customer_title}: " + " ".join(parts)
        logging.error(message)
        if not progress_run_id:
            return
        try:
            from lib.import_progress_service import ImportProgressService

            ImportProgressService.record_import_error(
                run_id=progress_run_id, raw=message, connector_id="google_ads", detail=customer_title
            )
        except Exception as exc:  # noqa: BLE001 — boundary: recording must never break the import
            logging.warning("gads _report_unattributed raised: %s — proceeding", exc)

    def _prune_customer(customer_esid, campaign_owner, team_esids):
        """drop rows and files each linked team holds for this
        customer's campaigns that belong to another team or none (e.g. the whole
        account legacy mode had sent to one team). Campaigns not seen this run are
        never touched, so vanished campaigns keep their history."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            raise RuntimeError("Elasticsearch is unavailable — cannot prune Google Ads rows")
        for esid in team_esids:
            # Only campaigns seen this run and owned by someone else (or no one).
            foreign = {cid for cid, (owner, _reason) in campaign_owner.items() if owner != esid}
            prune_foreign_docs(es, build_data_index_name(esid, "google_ads"), customer_esid, foreign)
            prune_foreign_files(customer_esid, esid, foreign)

    # Process each customer
    for customer_doc in customer_docs:
        if _cancel_requested():
            logging.info(
                "gads get_campaign_data: cancellation observed; stopping customer loop (fetched data retained)."
            )
            break

        customer = customer_doc["_source"]
        customer_esid = customer_doc["_id"]
        customer_id = customer.get("google_ads_customer_id")
        customer_title = customer.get("google_ads_customer_title", "Unknown")

        # clear the ambient team at the top of each customer: the
        # not-linked record site below fires BEFORE a team is resolved, and a
        # stale binding from the previous customer would mis-attribute it.
        from lib.import_run_context import set_current_team

        set_current_team(None)

        if not customer_id:
            logging.warning(f"Skipping customer {customer_esid} - missing customer_id")
            continue

        logging.info(f"\n{'='*60}")
        logging.info(f"Processing: {customer_title} (ID: {customer_id})")
        logging.info(f"{'='*60}")

        # every team linked to this customer, and how its campaigns
        # split between them (attribution.py). Legacy mode (no team has campaign
        # rules) keeps the the lowest team_esid gets the whole
        # account. Rules mode sends each campaign to the one team whose rules
        # match it and declines the rest.
        linked_teams = get_linked_teams(customer_esid)

        if not linked_teams:
            logging.warning(
                f"Skipping customer {customer_title} - not assigned to any team " "or Google Ads integration not active"
            )
            # surface this skip in the Report Scheduler "Errors" view
            # (was log-only; every sibling skip already records an error).
            _record_not_linked(customer_title)
            continue

        plan = plan_customer(customer_title, linked_teams)
        for problem in plan.problems:
            logging.warning(problem)
        metas = {esid: attribution_team_metadata(esid, doc) for esid, doc in linked_teams}
        # The team whose connection authenticates the customer and, in legacy
        # mode, the team that receives it.
        team_metadata = metas[linked_teams[0][0]]
        candidate_owners = list(plan.rules) if plan.rules_mode else [team_metadata["team_esid"]]

        # billing pre-check. Skip a source that is either
        # under an inactive subscription (lapsed/canceled/comp_revoked — Condition
        # A) OR over the paid connection cap (Condition B), matching the Current
        # Plan "Not imported" badge per-source. On the scheduled path this surfaces
        # a classified, user-actionable error in the Report Scheduler "Errors"
        # button (no-op on cron). Fail-open per source. per owning team.
        from lib.billing_entitlement import record_billing_skip, source_billing_block

        billed_owners = []
        for owner_esid in candidate_owners:
            _billing_block = source_billing_block(
                team_esid=owner_esid,
                asset_esid=customer_esid,
                provider_id=customer_id,
            )
            if _billing_block:
                logging.info(
                    "billing pre-check: skipping Google Ads customer %s (team %s, reason=%s).",
                    customer_title,
                    owner_esid,
                    _billing_block,
                )
                record_billing_skip(_billing_block, connector_id="google_ads", detail=customer_title)
                continue
            billed_owners.append(owner_esid)
        if not billed_owners:
            continue

        # bind this customer's team so recorded errors carry
        # durable per-team attribution.
        set_current_team(team_metadata.get("team_esid"))
        logging.info(f"Team: {team_metadata['team_title']}")
        if plan.rules_mode:
            logging.info(
                "per-campaign attribution across %s",
                ", ".join(metas[e]["team_title"] for e in candidate_owners),
            )

        # OAuth-only per customer (the shared service account is
        # retired). Build a client from THIS customer's per-user OAuth token. A
        # customer with no active OAuth connection — or one whose token won't
        # build a client — is SKIPPED (there is no SA to fall back to) and
        # surfaced as a user-readable import error so it's visible in Report
        # Scheduler, not just the log.
        # scope the OAuth credential to the authenticating
        # team's own connection so a foreign principal's token (another user who
        # authorized the same customer id) is not used. The stamp lives on the
        # team DOC: team_metadata() is stamped onto every saved row and must not
        # carry it, which is why it was dropped. A stale stamp
        # falls back to the legacy customer-id match, warned and recorded.
        oauth_config, fell_back = resolve_team_oauth_config(customer_id, base_credentials, linked_teams[0][1])
        if fell_back:
            logging.warning(
                "team %s's saved Google Ads connection %s can't be used for customer %s (%s); "
                "imported with the customer's other authorized connection instead.",
                team_metadata["team_title"],
                linked_teams[0][1].get("google_ads_connection_esid"),
                customer_title,
                customer_id,
            )
            _record_stale_stamp(customer_title)
        if not oauth_config:
            logging.warning(
                "skipping Google Ads customer %s (%s) — no OAuth connection; " "reconnect it under Data Connectors.",
                customer_title,
                customer_id,
            )
            _record_not_connected(customer_title)
            continue
        try:
            client = GoogleAdsClient.load_from_dict(oauth_config)
            logging.info(f"Auth: per-user OAuth for customer {customer_id}")
        except Exception as e:  # noqa: BLE001 — skip this customer, never break the import
            logging.warning(
                "OAuth client init failed for %s (%s); skipping customer.",
                customer_id,
                e,
            )
            _record_not_connected(customer_title)
            continue

        # Only customers we will actually import contribute to no_data markers.
        for owner_esid in billed_owners:
            all_team_list.append((owner_esid, metas[owner_esid]["team_title"]))

        # a manager (MCC) account has NO metrics of its own (a metrics
        # query returns query_error=59). Resolve the customer ids we actually pull
        # METRICS from: a direct account → itself (unchanged); an MCC → its ENABLED
        # leaf client accounts. The per-customer `client` already carries the right
        # login-customer-id (the MCC itself, via resolve_login_customer_id), so the
        # same client queries each leaf — only the query target customer_id changes.
        # All saved docs stay keyed to the team's CONNECTED customer asset
        # (customer_esid / customer_title), so downstream indexing is unchanged;
        # each metric row also carries its own leaf id as google_ads_metric_customer_id.
        metric_customer_ids = resolve_metric_customer_ids(client, customer_id)
        if not metric_customer_ids:
            logging.warning(
                "customer %s (%s) is a manager account with no reachable leaf "
                "client accounts; nothing to import.",
                customer_title,
                customer_id,
            )
            continue
        is_mcc = metric_customer_ids != [str(customer_id).replace("-", "").strip()]
        if is_mcc:
            # a leaf that is ALSO a directly-connected customer asset
            # assigned to THIS SAME team is already imported on its own pass; pulling
            # it here too would double-write into the team's index (the two writes
            # carry different google_ads_customer_esid, so neither overwrites the
            # other). Drop such leaves from the MCC expansion; the direct pass covers
            # them. Leaves connected to a DIFFERENT team (or not connected at all) are
            # kept — they aren't otherwise imported into this team's index.
            # "this same team" = any team that can own this customer's rows.
            deduped_leaves = []
            for leaf in metric_customer_ids:
                direct_owner = next((e for e in billed_owners if leaf_directly_connected_to_team(leaf, e)), None)
                if direct_owner:
                    logging.info(
                        "skipping leaf %s under MCC %s (%s) — already imported "
                        "directly for team %s; avoids a double-count.",
                        leaf,
                        customer_id,
                        customer_title,
                        direct_owner,
                    )
                    continue
                deduped_leaves.append(leaf)
            metric_customer_ids = deduped_leaves
            if not metric_customer_ids:
                logging.info(
                    "all leaves under MCC %s (%s) are directly connected to its team(s); "
                    "nothing left to import via the MCC pass.",
                    customer_id,
                    customer_title,
                )
                continue
            logging.info(
                "%s (%s) is an MCC; pulling metrics from %d leaf account(s).",
                customer_title,
                customer_id,
                len(metric_customer_ids),
            )

        # campaign id → (owning team_esid | None, reason); declined
        # campaigns collected for one report per customer. Owners come from the
        # metadata pass (every campaign of the account) and, for a campaign the
        # metadata pass missed, from the row's own name + labels.
        campaign_owner = {}
        declined = {}
        # Stale-row pruning needs the COMPLETE campaign list — only prune when
        # every leaf's metadata came back and covered every row seen.
        metadata_complete = True
        billed_set = set(billed_owners)

        def _route(doc):
            """The owning team_esid for this row (and stamp its team fields), or
            None when the campaign is declined or its owner is billing-blocked."""
            nonlocal metadata_complete
            cid = str(doc.get("campaign_id"))
            if cid not in campaign_owner:
                campaign_owner[cid] = plan.owner_of(doc.get("campaign_name"), doc.get("campaign_labels"))
                if plan.rules_mode and doc.get("record_type") != "campaign_metadata":
                    metadata_complete = False  # a metric row the metadata pass never listed
            owner_esid, reason = campaign_owner[cid]
            if owner_esid is None:
                declined[cid] = (doc.get("campaign_name") or cid, reason)
                return None
            if owner_esid not in billed_set:
                return None
            doc.update(metas[owner_esid])
            return owner_esid

        for metric_customer_id in metric_customer_ids:
            if _cancel_requested():
                logging.info("gads get_campaign_data: cancellation observed mid-leaf; stopping.")
                break

            # Fetch campaign metadata (start/end datetimes) from FROM campaign resource.
            # In v23, date fields are start_date_time / end_date_time on the campaign resource.
            # first, so every campaign's owner is known before any metric row.
            campaign_metadata = fetch_campaign_metadata(client, metric_customer_id, cancel_check=_cancel_requested)
            if not campaign_metadata:
                metadata_complete = False
            for meta_doc in campaign_metadata:
                owner_esid = _route(meta_doc)
                if owner_esid is None:
                    continue
                meta_doc["google_ads_customer_id"] = customer_id
                meta_doc["google_ads_customer_esid"] = customer_esid
                meta_doc["google_ads_customer_title"] = customer_title
                if is_mcc:
                    meta_doc["google_ads_metric_customer_id"] = metric_customer_id
                meta_doc["@timestamp"] = datetime.now(timezone.utc).isoformat()
                meta_doc["date"] = datetime.now().strftime("%Y-%m-%d")
                save_campaign_metadata(meta_doc, customer_esid, owner_esid)

            if campaign_metadata:
                logging.info(f"✓ Saved metadata for {len(campaign_metadata)} campaigns for {customer_title}")

            # Fetch video campaign data
            campaigns = fetch_video_campaign_performance(
                client, metric_customer_id, start_date, end_date, cancel_check=_cancel_requested
            )

            # Add team and customer metadata and save each campaign
            for campaign_data in campaigns:
                owner_esid = _route(campaign_data)
                if owner_esid is None:
                    continue
                campaign_data["google_ads_customer_id"] = customer_id
                campaign_data["google_ads_customer_esid"] = customer_esid
                campaign_data["google_ads_customer_title"] = customer_title
                if is_mcc:
                    campaign_data["google_ads_metric_customer_id"] = metric_customer_id
                campaign_data["@timestamp"] = datetime.now(timezone.utc).isoformat()

                # Save to JSON file
                save_campaign_data(
                    campaign_data,
                    customer_esid,
                    owner_esid,
                    campaign_data["date"],
                )
                d = campaign_data["date"]
                teams_with_data_by_date.setdefault(d, set()).add(owner_esid)
                total_campaigns += 1

            # Fetch geographic performance data (country-level installs)
            geo_records = fetch_geo_performance(
                client, metric_customer_id, start_date, end_date, cancel_check=_cancel_requested
            )

            if geo_records:
                # Load country name lookup once (cached after first call)
                geo_countries = load_geo_target_countries(client, metric_customer_id)

                total_geo = 0
                for geo_doc in geo_records:
                    criterion_id = geo_doc.get("geo_criterion_id")
                    country_info = geo_countries.get(criterion_id, {})
                    if not country_info:
                        logging.debug(f"No country info for criterion_id {criterion_id}, skipping")
                        continue
                    owner_esid = _route(geo_doc)
                    if owner_esid is None:
                        continue

                    geo_doc["geo_country_name"] = country_info.get("name", "")
                    geo_country_code = country_info.get("country_code", "")
                    geo_doc["geo_country_code"] = geo_country_code
                    centroid = _COUNTRY_CENTROIDS.get(geo_country_code)
                    if centroid:
                        geo_doc["location"] = centroid
                    geo_doc["google_ads_customer_id"] = customer_id
                    geo_doc["google_ads_customer_esid"] = customer_esid
                    geo_doc["google_ads_customer_title"] = customer_title
                    if is_mcc:
                        geo_doc["google_ads_metric_customer_id"] = metric_customer_id
                    geo_doc["@timestamp"] = datetime.now(timezone.utc).isoformat()

                    save_geo_data(geo_doc, customer_esid, owner_esid, geo_doc["date"])
                    total_geo += 1

                logging.info(f"✓ Saved {total_geo} geo performance records for {customer_title}")

        if plan.rules_mode:
            _report_unattributed(customer_title, declined, plan)
            if metadata_complete and not _cancel_requested():
                _prune_customer(customer_esid, campaign_owner, [esid for esid, _doc in linked_teams])
            else:
                logging.warning(
                    "not pruning stale rows for %s — the campaign list is incomplete this run.",
                    customer_title,
                )

    logging.info(f"\n{'='*60}")
    logging.info(f"✓ Total ad campaign records fetched: {total_campaigns}")
    logging.info(f"✓ Date range: {start_date} to {end_date}")
    logging.info(f"{'='*60}")

    # Write no_data markers for each date in the range
    if all_team_list:
        from import_scripts.facebook.run_daily_imports import generate_date_range

        for d in generate_date_range(start_date, end_date):
            teams_on_date = teams_with_data_by_date.get(d, set())
            # clean up stale markers before writing fresh ones.
            delete_stale_markers_for_date("*_google_ads", d, teams_on_date)
            write_no_data_markers(d, teams_on_date, "_google_ads", team_list=all_team_list)


if __name__ == "__main__":
    # For standalone testing
    import argparse

    parser = argparse.ArgumentParser(description="Fetch Google Ads video campaign data")
    parser.add_argument("--start_date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end_date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--customer_ids", help="Comma-separated customer IDs")

    args = parser.parse_args()

    main(
        {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "customer_ids": args.customer_ids,
        }
    )
