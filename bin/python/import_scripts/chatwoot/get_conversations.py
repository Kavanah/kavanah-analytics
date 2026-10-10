"""
Fetch open and pending conversations from Chatwoot and save as JSON files.

One JSON file per conversation, keyed by conversation ID.  Runs for all
Chatwoot accounts stored in de_asset_chatwoot_accounts.  When the account's
teams pick inboxes, only those inboxes are fetched, and each
conversation is stamped with the team that owns its inbox.

Usage:
    python3 /app/python/import.py chatwoot get_conversations
"""

import json
import logging
import os
import time
from datetime import datetime, timezone  # timezone used for kibana_date and @timestamp

import lib.logging
from import_scripts.chatwoot.api import ChatwootApiError, ChatwootAuthError, chatwoot_request
from lib.chatwoot_base_url import ChatwootBaseUrlError, normalize_chatwoot_base_url
from lib.encryption import decrypt
from lib.es_helpers import get_elasticsearch_client
from lib.import_scope import scope_resolution_error

# reuse the team→account scope resolution from the reports importer so
# both halves of a chatwoot run scope identically. the account FETCH
# is shared too (this module used to carry its own unscoped, size-capped copy):
# one paginated, query-scoped implementation for both halves.
from import_scripts.chatwoot.attribution import get_teams_for_account, log_problems, plan_account, prune_foreign_docs
from import_scripts.chatwoot.get_account_reports import _team_filter, accounts_for_team_ids, get_chatwoot_accounts
from lib.es_helpers import build_data_index_name

DATA_DIR = "/app/data/chatwoot/conversations"
PAGE_SIZE = 25
STATUSES = ["open", "pending"]


def fetch_conversations_page(account_id, api_token, status, page, *, base_url: str, inbox_id=None):
    """Fetch one page of conversations for the given status (one inbox's only,
    when ``inbox_id`` is given).

    raises ChatwootAuthError / ChatwootApiError instead of returning
    an empty page, which ended the pagination early and looked like "no more
    conversations"."""
    params = {"status": status, "page": page}
    if inbox_id is not None:
        params["inbox_id"] = inbox_id
    body = chatwoot_request(
        "GET",
        f"{base_url}/api/v1/accounts/{account_id}/conversations",
        api_token=api_token,
        what=f"conversations (status={status}, page={page}) of account {account_id}",
        params=params,
    )
    data = body.get("data", {}) if isinstance(body, dict) else {}
    return data.get("payload", []), data.get("meta", {})


def flatten_conversation(conv, account_title, account_id, account_esid, team_title, team_esid, org_title):
    """Flatten a raw Chatwoot conversation object into a doc for Elasticsearch."""
    meta = conv.get("meta", {})
    sender = meta.get("sender", {})
    sender_attrs = sender.get("additional_attributes", {})
    assignee = meta.get("assignee") or {}
    custom_attrs = conv.get("custom_attributes") or {}
    add_attrs = conv.get("additional_attributes") or {}

    activity_ts = conv.get("last_activity_at")
    kibana_date = datetime.fromtimestamp(activity_ts, tz=timezone.utc).strftime("%Y-%m-%d") if activity_ts else None

    return {
        "@timestamp": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "kibana_date": kibana_date,
        "chatwoot_account_title": account_title,
        "chatwoot_account_id": account_id,
        "chatwoot_account_esid": account_esid,
        "team_title": team_title,
        "team_esid": team_esid or "unknown",
        "organization_title": org_title,
        # Conversation core
        "conversation_id": conv.get("id"),
        "conversation_uuid": conv.get("uuid"),
        "status": conv.get("status"),
        "inbox_id": conv.get("inbox_id"),
        "created_at": conv.get("created_at"),
        "updated_at": conv.get("updated_at"),
        "last_activity_at": conv.get("last_activity_at"),
        "first_reply_created_at": conv.get("first_reply_created_at"),
        "waiting_since": conv.get("waiting_since"),
        "unread_count": conv.get("unread_count"),
        "priority": conv.get("priority"),
        "sla_policy_id": conv.get("sla_policy_id"),
        "muted": conv.get("muted"),
        "can_reply": conv.get("can_reply"),
        "labels": conv.get("labels") or [],
        # Channel type (e.g. instagram_direct_message, facebook, web_widget)
        "channel_type": add_attrs.get("type"),
        # Assignee
        "assignee_id": assignee.get("id"),
        "assignee_name": assignee.get("name"),
        "assignee_type": conv.get("assignee_type"),
        # Sender / contact — no name / email / phone / IG handle; sender_id is the identity
        "sender_id": sender.get("id"),
        "sender_instagram_follower_count": sender_attrs.get("social_instagram_follower_count"),
        "sender_instagram_is_verified": sender_attrs.get("social_instagram_is_verified_user"),
        # Custom attributes (Disciple.Tools integration)
    }


def save_conversation(doc, account_esid):
    directory = os.path.join(DATA_DIR, account_esid)
    os.makedirs(directory, exist_ok=True)
    filepath = os.path.join(directory, f"{doc['conversation_id']}.json")
    with open(filepath, "w") as f:
        json.dump(doc, f)


def _prune_stale_conversation_files(account_esid, owners):
    """in inbox mode, remove saved conversations whose inbox no team
    owns, or that are stamped with a team other than the inbox's owner. The
    indexer re-reads every file on disk, so a stale one would put it back."""
    directory = os.path.join(DATA_DIR, account_esid)
    if not os.path.isdir(directory):
        return
    removed = 0
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        filepath = os.path.join(directory, name)
        try:
            with open(filepath) as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        owner = owners.get(str(doc.get("inbox_id")))
        if owner is None or owner != doc.get("team_esid"):
            os.remove(filepath)
            removed += 1
    if removed:
        logging.info(f"Removed {removed} stale conversation file(s) for account {account_esid}.")


def main(params):
    lib.logging.define("chatwoot-get-conversations")

    es = get_elasticsearch_client()
    if es is None:
        # an ES outage is a hard failure, not a successful no-op. Was a
        # bare return (rc 0) on both the scoped and unscoped paths.
        raise RuntimeError("Failed to connect to Elasticsearch.")

    # scope to the requested teams' accounts when --team_ids is set.
    team_ids = _team_filter(params)
    if team_ids is not None:
        account_esids = accounts_for_team_ids(es, team_ids)
        logging.info(f"Scoped run for {len(team_ids)} team(s) → {len(account_esids)} chatwoot account(s).")
        if not account_esids:
            # a scoped run that resolved no account imported nothing;
            # exiting rc 0 let the scheduler record it as a SUCCESS.
            raise scope_resolution_error(
                scope_param="team_ids",
                scope_value=params.get("team_ids"),
                connector_id="chatwoot",
            )
        accounts = get_chatwoot_accounts(es, account_esids=account_esids)
    else:
        accounts = get_chatwoot_accounts(es)

    if not accounts:
        # on a SCOPED run this is a dangling link: the teams resolved to
        # account esids but no account doc came back. Unscoped runs keep the rc-0
        # return (no scope means nothing was promised).
        if team_ids is not None:
            raise scope_resolution_error(
                scope_param="team_ids",
                scope_value=params.get("team_ids"),
                connector_id="chatwoot",
            )
        logging.info("No Chatwoot accounts configured.")
        return

    # fetch failures; the run raises at the end instead of reporting success.
    failures = []

    for account_hit in accounts:
        account = account_hit["_source"]
        account_esid = account_hit["_id"]
        account_title = account.get("chatwoot_account_title", "unknown")
        account_id = account.get("chatwoot_account_id")
        encrypted_token = account.get("chatwoot_api_token")

        if not account_id or not encrypted_token:
            logging.warning(f"Skipping '{account_title}' — missing account_id or api_token.")
            continue

        # the account's own server (blank = Chatwoot Cloud), re-vetted
        # before the token is sent anywhere.
        try:
            base_url = normalize_chatwoot_base_url(account.get("chatwoot_base_url"))
        except ChatwootBaseUrlError as e:
            logging.error(f"Skipping '{account_title}' — server URL refused: {e}")
            continue

        api_token = decrypt(encrypted_token)

        # every team on the account, and which slice each one owns.
        plan = plan_account(account_title, get_teams_for_account(es, account_esid), None, scope_team_ids=team_ids)
        log_problems(plan)
        if plan.inbox_mode:
            _prune_stale_conversation_files(account_esid, plan.owners)
            for esid in {t.team_esid for t in plan.targets}:
                owned = [i for i, owner in plan.owners.items() if owner == esid]
                prune_foreign_docs(es, build_data_index_name(esid, "chatwoot_conversations"), account_esid, owned)

        logging.info(f"Fetching conversations for '{account_title}' (ID: {account_id}) on {base_url}")
        total = 0

        for target in plan.targets:
            team = target.team or {}
            team_title = team.get("team_title", "unknown")
            org_title = team.get("organization_title", "unknown") if target.team else "unknown"
            try:
                total += _fetch_target(
                    account_id, api_token, base_url, target, account_title, account_esid, team_title, org_title
                )
            except ChatwootAuthError as e:
                logging.error(f"Skipping '{account_title}': {e}")
                failures.append(f"account '{account_title}': {e}")
                break
            except ChatwootApiError as e:
                logging.error(f"Conversations for team '{team_title}' incomplete: {e}")
                failures.append(f"team '{team_title}': {e}")

        logging.info(f"Saved {total} conversations for '{account_title}'.")

    if failures:
        for failure in failures:
            logging.error(f"Chatwoot conversations not fully fetched — {failure}")
        raise RuntimeError(f"Chatwoot conversations: {len(failures)} fetch failure(s); see the log.")
    logging.info("Completed fetching Chatwoot conversations.")


def _fetch_target(account_id, api_token, base_url, target, account_title, account_esid, team_title, org_title):
    """Fetch and save every open / pending conversation of one target; returns
    how many were saved. Raises ChatwootApiError when a page cannot be read."""
    total = 0
    for status in STATUSES:
        page = 1
        while True:
            payload, meta = fetch_conversations_page(
                account_id, api_token, status, page, base_url=base_url, inbox_id=target.inbox_id
            )
            if not payload:
                break
            for conv in payload:
                # Backstop: never file another inbox's conversation under this team.
                if target.inbox_id is not None and str(conv.get("inbox_id")) != target.inbox_id:
                    continue
                doc = flatten_conversation(
                    conv,
                    account_title,
                    account_id,
                    account_esid,
                    team_title,
                    target.team_esid,
                    org_title,
                )
                save_conversation(doc, account_esid)
                total += 1
            logging.info(f"  status={status} page={page}: {len(payload)} conversations")
            # If we got fewer than PAGE_SIZE we've hit the last page
            if len(payload) < PAGE_SIZE:
                break
            page += 1
            time.sleep(0.2)
    return total
