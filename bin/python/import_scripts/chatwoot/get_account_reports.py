import os
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests

import lib.logging
from import_scripts.chatwoot.api import ChatwootApiError, ChatwootAuthError, chatwoot_request
from import_scripts.chatwoot.attribution import (
    get_teams_for_account,
    log_problems,
    plan_account,
    prune_foreign_docs,
)
from lib.chatwoot_base_url import ChatwootBaseUrlError, normalize_chatwoot_base_url
from lib.es_helpers import build_data_index_name, get_all_documents_paginated, get_elasticsearch_client
from lib.import_scope import scope_resolution_error
from lib.encryption import decrypt
from lib.no_data_markers import write_no_data_markers
from lib.cleanup_no_data_markers import delete_stale_markers_for_date

DATA_DIR = "/app/data/chatwoot/reports"

METRICS = [
    "conversations_count",
    "incoming_messages_count",
    "outgoing_messages_count",
    "resolutions_count",
    "avg_first_response_time",
    "avg_resolution_time",
    "avg_reply_time",
]

# the stored field names above are not always the reports API's
# metric names. Chatwoot's timeseries calls reply time ``reply_time``;
# ``avg_reply_time`` exists only in its summary reports, and the timeseries
# answers an unknown metric with ``{}`` (report_builder.rb ``timeseries``).
# Every saved day had a null reply time made the ``{}`` fatal.
API_METRIC_NAMES = {"avg_reply_time": "reply_time"}

# contact checkbox attributes are stored as daily counts only, as
# ``contact_attr_<key>``: never per-person values, never the Disciple.Tools
# ids / links.
CONTACT_ATTR_PREFIX = "contact_attr_"
_EXCLUDED_ATTR_PREFIXES = ("dt_",)
_MAX_CONTACT_ATTRS = 50
_ATTR_KEY_RE = re.compile(r"[a-z0-9_]{1,60}")
CONVERSATION_FILTER_PAGE_SIZE = 25


def list_inboxes(account_id, api_token, *, base_url: str):
    """The account's inboxes as ``{"id", "name", "channel_type", "page_id",
    "instagram_id"}`` dicts. Raises ``requests`` errors, so a caller that must
    tell "wrong token" from "server down" can (the inbox picker)."""
    url = f"{base_url}/api/v1/accounts/{account_id}/inboxes"
    # no redirects: base_url is a vetted public host, and a redirect
    # could carry the token-bearing request somewhere that was never vetted.
    response = requests.get(url, headers={"api_access_token": api_token}, timeout=30, allow_redirects=False)
    response.raise_for_status()
    return _inboxes_from_body(response.json())


def _inboxes_from_body(body):
    inboxes = []
    for inbox in body.get("payload", []):
        if inbox.get("id") is None:
            continue
        inboxes.append(
            {
                "id": inbox.get("id"),
                "name": inbox.get("name"),
                "channel_type": inbox.get("channel_type"),
                "page_id": inbox.get("page_id"),
                "instagram_id": inbox.get("instagram_id"),
            }
        )
    return inboxes


def fetch_inboxes(account_id, api_token, *, base_url: str):
    """
    Fetch all inboxes for the account and return a dict of extracted metadata:
      - facebook_page_id: page_id from the first FacebookPage inbox
      - instagram_id: instagram_id from the first FacebookPage inbox
      - inbox_names: list of all inbox names
      - inboxes: one dict per inbox (id, name, channel_type, page_id,
        instagram_id), for per-inbox attribution
    raises ChatwootAuthError / ChatwootApiError instead of returning
    empty metadata: with no inboxes, per-inbox attribution cannot be planned.
    """
    lib.logging.define("chatwoot-get-account-reports")
    meta = {"facebook_page_id": None, "instagram_id": None, "inbox_names": [], "inboxes": []}
    body = chatwoot_request(
        "GET",
        f"{base_url}/api/v1/accounts/{account_id}/inboxes",
        api_token=api_token,
        what=f"inboxes of account {account_id}",
    )
    if not isinstance(body, dict):
        raise ChatwootApiError(f"inboxes of account {account_id}: unexpected response ({type(body).__name__}).")
    meta["inboxes"] = _inboxes_from_body(body)
    for inbox in meta["inboxes"]:
        if inbox["name"]:
            meta["inbox_names"].append(inbox["name"])
        if inbox["channel_type"] == "Channel::FacebookPage" and meta["facebook_page_id"] is None:
            meta["facebook_page_id"] = inbox["page_id"]
            meta["instagram_id"] = inbox["instagram_id"]
    return meta


def fetch_contact_checkbox_keys(account_id, api_token, *, base_url: str):
    """The account's contact checkbox attribute keys. New checkboxes
    added in Chatwoot are picked up without a code change; Disciple.Tools keys
    and malformed keys are never included. Raises ChatwootApiError when they
    cannot be read — a missing key list would silently drop counts."""
    definitions = chatwoot_request(
        "GET",
        f"{base_url}/api/v1/accounts/{account_id}/custom_attribute_definitions",
        api_token=api_token,
        what=f"contact attribute definitions of account {account_id}",
        params={"attribute_model": 1},  # 1 = contact
    )
    keys = []
    for definition in definitions if isinstance(definitions, list) else []:
        key = str(definition.get("attribute_key") or "")
        if definition.get("attribute_display_type") != "checkbox" or not _ATTR_KEY_RE.fullmatch(key):
            continue
        if key.startswith(_EXCLUDED_ATTR_PREFIXES) or key in keys:
            continue
        keys.append(key)
    return keys[:_MAX_CONTACT_ATTRS]


def fetch_contact_activity(account_id, api_token, keys, since_ts, until_ts, *, base_url: str, inbox_id=None):
    """Who started a conversation in the window (in ``inbox_id``, or the whole
    account), as counts only: ``{"contacts_started": <distinct contacts>,
    "flags": {<checkbox key>: <distinct contacts with it ticked now>}}``.

    ``contacts_started`` feeds the Summary Report's Messaging Contacts.
    Only the counts leave this function — contact ids and values stay in memory.
    Raises ChatwootApiError when the conversations cannot be read.
    """
    conditions = [
        # A day either side, then trimmed to the exact window below, so the day
        # boundaries match the reports (the filter compares dates in the server's zone).
        {"attribute_key": "created_at", "filter_operator": "is_greater_than", "values": [_day(since_ts - 86400)]},
        {"attribute_key": "created_at", "filter_operator": "is_less_than", "values": [_day(until_ts + 86400)]},
    ]
    if inbox_id is not None:
        conditions.insert(0, {"attribute_key": "inbox_id", "filter_operator": "equal_to", "values": [int(inbox_id)]})
    for i, condition in enumerate(conditions):
        condition["query_operator"] = "and" if i < len(conditions) - 1 else None

    url = f"{base_url}/api/v1/accounts/{account_id}/conversations/filter"
    flagged = {key: set() for key in keys}
    started = set()
    page = 1
    while True:
        body = chatwoot_request(
            "POST",
            url,
            api_token=api_token,
            what=f"conversation filter page {page} of account {account_id}",
            params={"page": page},
            json_body={"payload": conditions},
        )
        conversations = body.get("payload", []) if isinstance(body, dict) else []
        for conv in conversations:
            created = conv.get("created_at")
            if not isinstance(created, (int, float)) or not since_ts <= created <= until_ts:
                continue
            if inbox_id is not None and str(conv.get("inbox_id")) != str(inbox_id):
                continue
            sender = (conv.get("meta") or {}).get("sender") or {}
            if sender.get("id") is not None:
                started.add(sender.get("id"))
            attrs = sender.get("custom_attributes") or {}
            for key in keys:
                if attrs.get(key) is True:
                    flagged[key].add(sender.get("id"))
        if len(conversations) < CONVERSATION_FILTER_PAGE_SIZE:
            break
        page += 1
        time.sleep(0.2)
    return {"contacts_started": len(started), "flags": {key: len(contacts) for key, contacts in flagged.items()}}


def _day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _team_filter(params):
    """Parse the optional ``--team_ids=a,b`` scope flag into a set of team
    ESIDs, or None when unscoped (the daily-cron case pulls every account).

    the report scheduler dispatches a team-scoped import for a
    chatwoot schedule. A team maps to one chatwoot account, but several teams
    can share one account (like Echo), so the flag is team-based and resolved
    to account ESIDs in :func:`accounts_for_team_ids`."""
    raw = params.get("team_ids")
    if not raw or raw is True:
        return None
    return {t.strip() for t in str(raw).split(",") if t.strip()}


def accounts_for_team_ids(es, team_esids):
    """Return the set of chatwoot account ESIDs linked to the given teams.

    Each team doc carries a single ``chatwoot_account_esid`` (a team maps to
    one account); multiple teams may resolve to the same account. Returns an
    empty set when none of the teams link to a chatwoot account — the caller
    then has nothing in scope and pulls nothing."""
    lib.logging.define("chatwoot-get-account-reports")
    account_esids = set()
    for team_esid in team_esids:
        try:
            result = es.search(
                index="de_asset_teams",
                body={"query": {"bool": {"must": [{"term": {"_id": team_esid}}, {"term": {"ds_chatwoot": "true"}}]}}},
                size=1,
            )
            hits = result.get("hits", {}).get("hits", [])
            if not hits:
                logging.warning(f"Team {team_esid} has no chatwoot connector; skipping.")
                continue
            account_esid = hits[0]["_source"].get("chatwoot_account_esid")
            if account_esid:
                account_esids.add(account_esid)
            else:
                logging.warning(f"Team {team_esid} has ds_chatwoot but no chatwoot_account_esid; skipping.")
        except Exception:
            # was caught and swallowed, so an ES outage yielded an empty
            # set for EVERY team and looked identical to "no team links an account".
            # The caller's strict-mode guard would then classify an infrastructure
            # fault as no_data_source and tell the user to fix a configuration that
            # was fine. Re-raise; scope is never widened by a raise.
            logging.error(f"Error resolving chatwoot account for team {team_esid}", exc_info=True)
            raise
    return account_esids


def get_chatwoot_accounts(es, account_esids=None):
    """Return Chatwoot account documents from Elasticsearch.

    When ``account_esids`` is a set, only those accounts are returned (the
    scheduler's team-scoped path); otherwise every configured account is
    returned (the daily-cron path).

    the fetch is paginated (the old single-shot size=1000 silently
    dropped accounts past the cap) and a team-scoped run now scopes the QUERY
    to the resolved account ESIDs instead of post-filtering a global fetch;
    the in-Python filter stays as an exact-match safety net."""
    lib.logging.define("chatwoot-get-account-reports")
    query = {"match_all": {}} if account_esids is None else {"ids": {"values": sorted(account_esids)}}
    try:
        accounts = get_all_documents_paginated("de_asset_chatwoot_accounts", query, es=es)
        if account_esids is not None:
            accounts = [a for a in accounts if a["_id"] in account_esids]
        logging.info(f"Found {len(accounts)} Chatwoot account(s).")
        return accounts
    except Exception as e:
        logging.error(f"Error fetching Chatwoot accounts: {e}")
        return []


def date_to_unix(date_str, end_of_day=False):
    """Convert a YYYY-MM-DD string to a Unix timestamp."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def fetch_metric(account_id, api_token, metric, since_ts, until_ts, *, base_url: str, inbox_id=None):
    """
    Fetch a single metric from Chatwoot reports API — account-level, or for one
    inbox when ``inbox_id`` is given.
    Returns the summed value across all data points. Raises ChatwootApiError
    when the value cannot be read (a None here used to be saved as a
    null metric and the run reported success).
    """
    lib.logging.define("chatwoot-get-account-reports")
    url = f"{base_url}/api/v2/accounts/{account_id}/reports"
    params = {
        "type": "account",
        "metric": API_METRIC_NAMES.get(metric, metric),
        "since": since_ts,
        "until": until_ts,
    }
    if inbox_id is not None:
        params["type"] = "inbox"
        params["id"] = inbox_id

    what = f"metric '{metric}' of account {account_id}" + (f" inbox {inbox_id}" if inbox_id is not None else "")
    data = chatwoot_request("GET", url, api_token=api_token, what=what, params=params)
    if not isinstance(data, list):
        raise ChatwootApiError(f"{what}: unexpected response ({type(data).__name__}, not a list).")

    # API returns time-series data points; sum them for the day total
    # For averages (avg_*), compute the mean instead
    values = [float(point["value"]) for point in data if point.get("value") is not None]
    if not values:
        return 0

    if metric.startswith("avg_"):
        return round(sum(values) / len(values), 2)
    else:
        return int(sum(values))


def _report_path(account_esid, date_str, inbox_id=None):
    directory = os.path.join(DATA_DIR, account_esid)
    if inbox_id is not None:
        directory = os.path.join(directory, f"inbox_{inbox_id}")
    return directory, os.path.join(directory, f"{date_str}.json")


def save_report(report, account_esid, date_str, inbox_id=None):
    """Save a report document as a JSON file (one per inbox in inbox mode)."""
    lib.logging.define("chatwoot-get-account-reports")
    directory, filepath = _report_path(account_esid, date_str, inbox_id)
    os.makedirs(directory, exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(report, f)
    logging.info(f"Saved report to {filepath}")


def _prune_stale_report_files(account_esid, date_str, owners):
    """in inbox mode, remove this day's files that no team owns: the
    account-level file and those of inboxes nobody picks. The indexer re-reads
    every file on disk, so a stale one would put the data back."""
    stale = [_report_path(account_esid, date_str)[1]]
    root = os.path.join(DATA_DIR, account_esid)
    if os.path.isdir(root):
        for name in os.listdir(root):
            if name.startswith("inbox_") and name[len("inbox_") :] not in owners:
                stale.append(os.path.join(root, name, f"{date_str}.json"))
    for filepath in stale:
        if os.path.exists(filepath):
            os.remove(filepath)
            logging.info(f"Removed stale report file {filepath}")


def main(params):
    lib.logging.define("chatwoot-get-account-reports")

    date_str = params.get("date")
    if not date_str:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    since_ts = date_to_unix(date_str, end_of_day=False)
    until_ts = date_to_unix(date_str, end_of_day=True)

    logging.info(f"Fetching Chatwoot account reports for date: {date_str}")

    es = get_elasticsearch_client()
    if es is None:
        # see the sibling in get_conversations: a hard failure, not rc 0.
        raise RuntimeError("Failed to connect to Elasticsearch.")

    # when the scheduler passes --team_ids, scope the pull to only
    # the chatwoot accounts those teams link to; otherwise pull every account.
    team_ids = _team_filter(params)
    if team_ids is not None:
        account_esids = accounts_for_team_ids(es, team_ids)
        logging.info(f"Scoped run for {len(team_ids)} team(s) → {len(account_esids)} chatwoot account(s).")
        if not account_esids:
            # fail loudly instead of reporting a silent success.
            raise scope_resolution_error(
                scope_param="team_ids",
                scope_value=params.get("team_ids"),
                connector_id="chatwoot",
            )
        accounts = get_chatwoot_accounts(es, account_esids=account_esids)
    else:
        accounts = get_chatwoot_accounts(es)

    if not accounts:
        # dangling link on a scoped run; unscoped keeps its rc-0 return.
        if team_ids is not None:
            raise scope_resolution_error(
                scope_param="team_ids",
                scope_value=params.get("team_ids"),
                connector_id="chatwoot",
            )
        logging.info("No Chatwoot accounts configured. Nothing to import.")
        return

    teams_with_data = set()
    all_team_list = []
    # what could not be fetched this run. A failed team gets no report
    # file and no no_data marker (it is not known to be empty), and the run
    # raises at the end so the scheduler records a failure, not a success.
    failures = []
    failed_team_esids = set()

    for account_hit in accounts:
        account = account_hit["_source"]
        account_esid = account_hit["_id"]
        account_title = account.get("chatwoot_account_title", "unknown")
        account_id = account.get("chatwoot_account_id")
        encrypted_token = account.get("chatwoot_api_token")

        if not account_id or not encrypted_token:
            logging.warning(f"Skipping Chatwoot account '{account_title}' — missing account_id or api_token.")
            continue

        # the account's own server (blank = Chatwoot Cloud), re-vetted
        # before the token is sent anywhere.
        try:
            base_url = normalize_chatwoot_base_url(account.get("chatwoot_base_url"))
        except ChatwootBaseUrlError as e:
            logging.error(f"Skipping Chatwoot account '{account_title}' — server URL refused: {e}")
            continue

        api_token = decrypt(encrypted_token)

        # every team on the account, and which slice each one owns.
        teams = get_teams_for_account(es, account_esid)
        try:
            inbox_meta = fetch_inboxes(account_id, api_token, base_url=base_url)
            checkbox_keys = fetch_contact_checkbox_keys(account_id, api_token, base_url=base_url)
        except ChatwootApiError as e:
            # A refused token (ChatwootAuthError) lands here too: skip the account.
            logging.error(f"Skipping Chatwoot account '{account_title}' for {date_str}: {e}")
            failures.append(f"account '{account_title}': {e}")
            failed_team_esids.update(esid for esid, _doc in teams)
            continue
        plan = plan_account(account_title, teams, inbox_meta["inboxes"], scope_team_ids=team_ids)
        log_problems(plan)
        for target in plan.targets:
            if target.team_esid:
                title = target.team.get("team_title", "unknown")
                if (target.team_esid, title) not in all_team_list:
                    all_team_list.append((target.team_esid, title))

        if plan.inbox_mode:
            _prune_stale_report_files(account_esid, date_str, plan.owners)
            for esid in {t.team_esid for t in plan.targets}:
                owned = [i for i, owner in plan.owners.items() if owner == esid]
                prune_foreign_docs(es, build_data_index_name(esid, "chatwoot_insights"), account_esid, owned, date=date_str)

        for target in plan.targets:
            team = target.team or {}
            team_title = team.get("team_title", "unknown")
            logging.info(
                f"Fetching metrics for Chatwoot account '{account_title}' (ID: {account_id}) on {base_url}, "
                f"team: '{team_title}'" + (f", inbox {target.inbox_id} ({target.inbox.get('name')})" if target.inbox else "")
            )
            report = {
                "@timestamp": datetime.utcnow().isoformat() + "Z",
                "date": date_str,
                "kibana_date": date_str,
                "chatwoot_account_title": account_title,
                "chatwoot_account_id": account_id,
                "chatwoot_account_esid": account_esid,
                "team_title": team_title,
                "team_esid": target.team_esid or "unknown",
                "organization_title": team.get("organization_title", "unknown") if target.team else "unknown",
            }
            if target.inbox is None:
                report.update(
                    {
                        "facebook_page_id": inbox_meta["facebook_page_id"],
                        "instagram_id": inbox_meta["instagram_id"],
                        "inbox_names": inbox_meta["inbox_names"],
                    }
                )
            else:
                # Only this inbox's own identity — never the names of inboxes other
                # teams own on the same account.
                is_fb = target.inbox.get("channel_type") == "Channel::FacebookPage"
                report.update(
                    {
                        "inbox_id": int(target.inbox_id) if target.inbox_id.isdigit() else None,
                        "inbox_name": target.inbox.get("name"),
                        "inbox_channel_type": target.inbox.get("channel_type"),
                        "inbox_names": [target.inbox["name"]] if target.inbox.get("name") else [],
                        "facebook_page_id": target.inbox.get("page_id") if is_fb else None,
                        "instagram_id": target.inbox.get("instagram_id") if is_fb else None,
                    }
                )

            try:
                for metric in METRICS:
                    value = fetch_metric(
                        account_id, api_token, metric, since_ts, until_ts, base_url=base_url, inbox_id=target.inbox_id
                    )
                    report[metric] = value
                    logging.info(f"  {metric}: {value}")
                    # Brief pause to avoid hammering the API
                    time.sleep(0.2)

                activity = fetch_contact_activity(
                    account_id, api_token, checkbox_keys, since_ts, until_ts, base_url=base_url, inbox_id=target.inbox_id
                )
            except ChatwootAuthError as e:
                logging.error(f"Skipping Chatwoot account '{account_title}' for {date_str}: {e}")
                failures.append(f"account '{account_title}': {e}")
                failed_team_esids.update(t.team_esid for t in plan.targets if t.team_esid)
                break
            except ChatwootApiError as e:
                # do not save a report with holes in it; the day stays
                # missing (and visible as a failure) until a re-run fills it.
                logging.error(f"Not saving {date_str} for team '{team_title}': {e}")
                failures.append(f"team '{team_title}': {e}")
                if target.team_esid:
                    failed_team_esids.add(target.team_esid)
                continue
            report["contacts_started"] = activity["contacts_started"]
            for key in checkbox_keys:
                report[CONTACT_ATTR_PREFIX + key] = activity["flags"][key]
            logging.info(f"  contact activity: {activity}")

            save_report(report, account_esid, date_str, inbox_id=target.inbox_id)
            if target.team_esid:
                teams_with_data.add(target.team_esid)

    # Write no_data markers for teams with Chatwoot accounts but no data.
    # First, delete any stale markers for teams that got real data this run
    #. a team whose fetch failed is left out: it is not
    # known to have no data.
    marker_team_list = [(esid, title) for esid, title in all_team_list if esid not in failed_team_esids - teams_with_data]
    if marker_team_list:
        delete_stale_markers_for_date("*_chatwoot_insights", date_str, teams_with_data)
        write_no_data_markers(date_str, teams_with_data, "_chatwoot_insights", team_list=marker_team_list)

    if failures:
        for failure in failures:
            logging.error(f"Chatwoot report for {date_str} not saved — {failure}")
        raise RuntimeError(f"Chatwoot account reports for {date_str}: {len(failures)} fetch failure(s); see the log.")
    logging.info("Completed fetching Chatwoot account reports.")
