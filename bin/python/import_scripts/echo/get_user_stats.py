import os
import sys
import json
import re
import time
from datetime import datetime, timedelta, timezone
import requests
import logging

ECHO_RETRY_DELAYS = [60, 120, 240]  # seconds for exponential backoff on request failures

from lib.es_helpers import get_all_documents_paginated, get_elasticsearch_client
from lib.import_scope import scope_resolution_error
from lib.echo_base_url import EchoSubdomainError, echo_base_url
from lib.encryption import decrypt
from lib.paths import safe_path_join
from import_scripts.echo.api_ids import is_safe_echo_id
import lib.logging

# module constant so tests can point the importer at a temp dir.
USER_STATS_DIR = "/app/data/echo/user_stats"


def parse_hours_online_to_decimal(hours_str):
    """
    Parse Echo hoursOnline serialized string to a decimal float.

    Handles formats like:
        "3h 27m"  -> 3.45
        "0h 50m"  -> 0.83
        "10h 0m"  -> 10.0
        "45m"     -> 0.75
        "2h"      -> 2.0

    Returns 0.0 if the string cannot be parsed.
    """
    if not hours_str:
        return 0.0
    match = re.match(r"(?:(\d+)h\s*)?(?:(\d+)m)?", str(hours_str).strip())
    if not match:
        return 0.0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    return round(hours + minutes / 60, 2)


def get_tenants(tenant_esids=None):
    """Fetch Echo tenant docs.

    paginated (the old single-shot size=1000 silently dropped
    tenants past the cap) and, when ``tenant_esids`` is given (a
    ``limit_teams``-scoped run), scoped at query time so out-of-scope
    tenants are never loaded. None = all tenants (cron).
    """
    lib.logging.define("echo-get-user-stats")
    logging.info("Fetching tenants from Elasticsearch")
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return []
    query = {"match_all": {}} if tenant_esids is None else {"ids": {"values": sorted(tenant_esids)}}
    try:
        tenants = get_all_documents_paginated("de_asset_echo_tenants", query, es=es)
        logging.info(f"Found {len(tenants)} tenants.")
        return tenants
    except Exception as e:
        logging.error(f"Error fetching tenants from Elasticsearch: {e}")
        return []


def resolve_tenant_esids_for_teams(team_esids):
    """resolve team ``_id``s to the set of ``echo_tenant_esid`` values
    they belong to, so a ``limit_teams``-scoped run only contacts those tenants.

    User stats have no team concept (they are per-tenant agent stats), so team
    scoping at this stage means tenant-set scoping. An empty result (unknown
    team ids, or teams with no tenant) means a scoped run skips every tenant —
    consistent with get_conversations' strict ``terms _id`` filter.
    """
    lib.logging.define("echo-get-user-stats")
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch while resolving limit_teams.")
        return set()
    try:
        # paginated — no 1000-team cap on the scope resolution.
        hits = get_all_documents_paginated("de_asset_teams", {"terms": {"_id": team_esids}}, es=es)
        tenant_esids = {h["_source"].get("echo_tenant_esid") for h in hits if h["_source"].get("echo_tenant_esid")}
        logging.info(f"limit_teams {team_esids} resolved to tenant esids: {sorted(tenant_esids)}")
        return tenant_esids
    except Exception:
        # was `return set`; see the sibling in get_conversations. An ES
        # outage must not be laundered into a data-shaped "no tenants" answer.
        logging.error("Error resolving limit_teams to tenant esids", exc_info=True)
        raise


def fetch_user_stats_graphql(base_url, headers, start_date, end_date):
    """
    Fetch per-user stats for the given date range via GraphQL.

    Uses users { nodes { ... } } (UserConnection pattern).
    Queries sentMessagesCount, hoursOnline, claimedConversations (count),
    claimedConversationsCount, isScheduledNow, outcomeTotals, lastLoginAt.

    Note: claimedConversationsCount had a known server bug (user.rb:918).
    We query both and fall back to counting claimedConversations if it errors.

    Returns list of user stat dicts, or None on failure.
    """
    lib.logging.define("echo-get-user-stats")
    graphql_url = f"{base_url}/graphql"
    start_dt = f"{start_date}T00:00:00Z"
    end_dt = f"{end_date}T23:59:59Z"

    query = """
    {
        users {
            nodes {
                id
                name
                isScheduledNow
                sentMessagesCount(startDate: "%s", endDate: "%s")
                hoursOnline(startDate: "%s", endDate: "%s")
                claimedConversationsCount(startDate: "%s", endDate: "%s")
                outcomeTotals(startDate: "%s", endDate: "%s") {
                    value
                    count
                }
                lastLoginAt
            }
        }
    }
    """ % (
        start_dt,
        end_dt,
        start_dt,
        end_dt,
        start_dt,
        end_dt,
        start_dt,
        end_dt,
    )

    response = None
    for attempt, delay in enumerate([0] + ECHO_RETRY_DELAYS):
        if delay:
            logging.warning(f"User stats GraphQL retry (attempt {attempt}), sleeping {delay}s...")
            time.sleep(delay)
        try:
            response = requests.post(graphql_url, json={"query": query}, headers=headers)
            if response.status_code == 500:
                logging.warning("User stats query returned 500 — retrying without claimedConversationsCount")
                return fetch_user_stats_graphql_fallback(base_url, headers, start_date, end_date)
            response.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            logging.error(f"User stats GraphQL request failed (attempt {attempt + 1}): {e}")
            response = None
    if response is None:
        return None

    try:
        data = response.json()
    except json.JSONDecodeError:
        logging.error(f"Invalid JSON from user stats GraphQL: {response.text}")
        return None

    if "errors" in data:
        # claimedConversationsCount may still 500 via GraphQL error — retry without it
        logging.warning(f"GraphQL errors in user stats query: {data['errors']}")
        logging.info("Retrying user stats query without claimedConversationsCount")
        return fetch_user_stats_graphql_fallback(base_url, headers, start_date, end_date)

    users = data.get("data", {}).get("users", {}).get("nodes", [])
    logging.info(f"Fetched stats for {len(users)} users")
    return users


def fetch_user_stats_graphql_fallback(base_url, headers, start_date, end_date):
    """Fallback query without claimedConversationsCount for servers with the Ruby bug."""
    lib.logging.define("echo-get-user-stats")
    graphql_url = f"{base_url}/graphql"
    start_dt = f"{start_date}T00:00:00Z"
    end_dt = f"{end_date}T23:59:59Z"

    query = """
    {
        users {
            nodes {
                id
                name
                isScheduledNow
                sentMessagesCount(startDate: "%s", endDate: "%s")
                hoursOnline(startDate: "%s", endDate: "%s")
                outcomeTotals(startDate: "%s", endDate: "%s") {
                    value
                    count
                }
                lastLoginAt
            }
        }
    }
    """ % (
        start_dt,
        end_dt,
        start_dt,
        end_dt,
        start_dt,
        end_dt,
    )

    try:
        response = requests.post(graphql_url, json={"query": query}, headers=headers)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logging.error(f"Fallback user stats GraphQL request failed: {e}")
        return None

    if "errors" in data:
        logging.error(f"GraphQL errors in fallback user stats query: {data['errors']}")
        return None

    users = data.get("data", {}).get("users", {}).get("nodes", [])
    logging.info(f"Fallback: fetched stats for {len(users)} users")
    return users


def fetch_hourly_online_users(base_url, headers, start_date, end_date):
    """
    Fetch hourlyOnlineUsers for the date range, paginated via cursor.

    Returns a dict mapping user_id -> total_minutes_online for the period.
    Each HourlyOnlineUser record has hour and minutes fields.
    """
    lib.logging.define("echo-get-user-stats")
    graphql_url = f"{base_url}/graphql"
    start_dt = f"{start_date}T00:00:00Z"
    end_dt = f"{end_date}T23:59:59Z"

    user_minutes = {}
    cursor = None
    page = 0

    while True:
        page += 1
        after_arg = f', after: "{cursor}"' if cursor else ""
        query = """
        {
            hourlyOnlineUsers(startDate: "%s", endDate: "%s", first: 500%s) {
                pageInfo {
                    hasNextPage
                    endCursor
                }
                nodes {
                    userId
                    hour
                    minutes
                }
            }
        }
        """ % (
            start_dt,
            end_dt,
            after_arg,
        )

        data = None
        for attempt, delay in enumerate([0] + ECHO_RETRY_DELAYS):
            if delay:
                logging.warning(f"hourlyOnlineUsers retry (page {page}, attempt {attempt}), sleeping {delay}s...")
                time.sleep(delay)
            try:
                response = requests.post(graphql_url, json={"query": query}, headers=headers)
                response.raise_for_status()
                data = response.json()
                break
            except Exception as e:
                logging.error(f"hourlyOnlineUsers request failed (page {page}, attempt {attempt + 1}): {e}")
                data = None
        if data is None:
            break

        if "errors" in data:
            logging.error(f"GraphQL errors in hourlyOnlineUsers (page {page}): {data['errors']}")
            break

        result = data.get("data", {}).get("hourlyOnlineUsers", {})
        nodes = result.get("nodes", [])
        logging.info(f"hourlyOnlineUsers page {page}: {len(nodes)} records")

        for node in nodes:
            user_id = str(node.get("userId", ""))
            minutes = int(node.get("minutes") or 0)
            if user_id:
                user_minutes[user_id] = user_minutes.get(user_id, 0) + minutes

        page_info = result.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    logging.info(f"hourlyOnlineUsers: aggregated data for {len(user_minutes)} users")
    return user_minutes


def save_to_file(directory, filename, data):
    lib.logging.define("echo-get-user-stats")
    os.makedirs(directory, exist_ok=True)
    # raises UnsafePathError if the name would leave ``directory``.
    filepath = safe_path_join(directory, filename)
    logging.info(f"Saving user stats to {filepath}")
    with open(filepath, "w") as f:
        json.dump(data, f)


def main(params):
    lib.logging.define("echo-get-user-stats")
    logging.info("Starting get_user_stats main")

    start_date = params.get("start_date")
    end_date = params.get("end_date")
    if not start_date or not end_date:
        yesterday = datetime.now() - timedelta(days=1)
        start_date = yesterday.strftime("%Y-%m-%d")
        end_date = yesterday.strftime("%Y-%m-%d")
    logging.info(f"Fetching user stats for date range: {start_date} to {end_date}")

    # team-scoped runs only contact the tenants owning the targeted
    # teams. None (no limit_teams) keeps the unfiltered cron behavior: all tenants.
    limit_teams = params.get("limit_teams")
    limit_teams_list = [t.strip() for t in limit_teams.split(",") if t.strip()] if limit_teams else []
    allowed_tenant_esids = resolve_tenant_esids_for_teams(limit_teams_list) if limit_teams_list else None

    # the scope now also narrows the ES FETCH itself (not just the
    # per-tenant skip below, which stays as the safety net).
    if allowed_tenant_esids is not None and not allowed_tenant_esids:
        # fail loudly rather than exiting rc 0 as a silent success.
        raise scope_resolution_error(
            scope_param="limit_teams",
            scope_value=limit_teams,
            connector_id="echo",
        )
    tenants = get_tenants(tenant_esids=allowed_tenant_esids)
    if not tenants:
        # dangling link on a scoped run (see get_conversations); the
        # unscoped path keeps its rc-0 return.
        if limit_teams_list:
            raise scope_resolution_error(
                scope_param="limit_teams",
                scope_value=limit_teams,
                connector_id="echo",
            )
        logging.info("No tenants found.")
        return

    for tenant_hit in tenants:
        tenant = tenant_hit["_source"]
        tenant_esid = tenant_hit["_id"]
        echo_tenant_subdomain = tenant.get("echo_tenant_subdomain")
        encrypted_api_key = tenant.get("echo_tenant_api_key")

        # skip tenants outside the limit_teams scope before any
        # network call, so a single-team run never touches unrelated tenants
        # (e.g. the dead controlorg-tripwire tripwire tenant that 404-loops).
        if allowed_tenant_esids is not None and tenant_esid not in allowed_tenant_esids:
            logging.info(f"Skipping tenant {tenant_esid} (subdomain {echo_tenant_subdomain}) " f"— not in limit_teams scope.")
            continue

        if not echo_tenant_subdomain or not encrypted_api_key:
            logging.warning(f"Tenant {tenant_esid} missing subdomain or API key.")
            continue

        # the subdomain is the request host; never splice an
        # unvetted value into the URL. Decline and report, don't guess.
        try:
            base_url = echo_base_url(echo_tenant_subdomain)
        except EchoSubdomainError:
            logging.error(
                f"Tenant {tenant_esid}: stored Echo subdomain is not a single DNS label — "
                "skipping this tenant. Correct it in Data Connectors."
            )
            continue
        api_key = decrypt(encrypted_api_key)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        users = fetch_user_stats_graphql(base_url, headers, start_date, end_date)
        if users is None:
            logging.error(f"Skipping tenant {tenant_esid} due to GraphQL error.")
            continue

        # Fetch hourly online data and aggregate minutes per user
        hourly_minutes = fetch_hourly_online_users(base_url, headers, start_date, end_date)

        base_directory = os.path.join(USER_STATS_DIR, tenant_esid)

        for user in users:
            user_id = user.get("id")
            if not user_id:
                logging.warning("Skipping user with no id")
                continue
            # the id names the staging file; refuse anything that
            # is not a plain token (e.g. "../x").
            if not is_safe_echo_id(user_id):
                logging.warning(f"Tenant {tenant_esid}: skipping Echo user with an unsafe id {str(user_id)[:80]!r}")
                continue

            user_id_str = str(user_id)

            hours_str = user.get("hoursOnline", "")
            hours_decimal = parse_hours_online_to_decimal(hours_str)

            # claimedConversations and claimedConversationsCount both trigger a
            # server-side Ruby bug (user.rb:918-919) and return 500.
            # claimed_conversations_count is left null here; derive it by
            # aggregating claimed_by_user_name in the echo_conversations index.
            claimed_count = user.get("claimedConversationsCount")  # None if fallback query used

            # outcomeTotals is a list of {value, count} objects
            outcome_totals_raw = user.get("outcomeTotals") or []
            outcome_totals = {item["value"]: item["count"] for item in outcome_totals_raw if "value" in item}

            last_login_raw = user.get("lastLoginAt")
            last_login_at = None
            if last_login_raw:
                try:
                    dt = datetime.fromisoformat(last_login_raw.replace("Z", "+00:00"))
                    last_login_at = dt.strftime("%B %d, %Y %H:%M:%S UTC")
                except (ValueError, AttributeError):
                    last_login_at = last_login_raw

            # Hourly online minutes from the separate hourlyOnlineUsers query
            total_minutes_online = hourly_minutes.get(user_id_str, 0)
            hours_online_from_hourly = round(total_minutes_online / 60, 2)

            doc = {
                "id": f"{tenant_esid}_{user_id}_{start_date}_{end_date}",
                "user_id": user_id_str,
                "user_name": user.get("name"),
                "is_scheduled_now": user.get("isScheduledNow", False),
                "echo_tenant_subdomain": echo_tenant_subdomain,
                "echo_tenant_esid": tenant_esid,
                "period_start": start_date,
                "period_end": end_date,
                "sent_messages_count": user.get("sentMessagesCount", 0),
                "hours_online_str": hours_str,
                "hours_online_decimal": hours_decimal,
                "claimed_conversations_count": claimed_count,
                "total_minutes_online": total_minutes_online,
                "hours_online_from_hourly": hours_online_from_hourly,
                "outcome_totals": outcome_totals,
                "last_login_at": last_login_at,
                "@timestamp": datetime.now(timezone.utc).isoformat(),
            }

            filename = f"{user_id}_{start_date}_{end_date}.json"
            save_to_file(base_directory, filename, doc)

        logging.info(f"Saved user stats for {len(users)} users in tenant {tenant_esid}")

    logging.info("Completed get_user_stats.")
