"""
Chatwoot API field explorer.

Probes all major Chatwoot API endpoints for a given account and prints a
summary of every available field and whether it contains data.

Usage (inside imports_container):
    python3 /app/python/import.py chatwoot explore_fields
    python3 /app/python/import.py chatwoot explore_fields --account_id 156008

The script reads account credentials from de_asset_chatwoot_accounts in ES.
If multiple accounts exist and no --account_id is given, the first one is used.
"""

import json
import logging

import requests

import lib.logging
from lib.encryption import decrypt
from lib.es_helpers import get_elasticsearch_client

CHATWOOT_BASE_URL = "https://app.chatwoot.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(url, headers, params=None):
    """GET with simple error handling. Returns parsed JSON or None."""
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.warning(f"  [request failed] {url}: {e}")
        return None


def _load_account(es, account_id_filter=None):
    """Return (account_source, account_esid) for the target account."""
    result = es.search(
        index="de_asset_chatwoot_accounts",
        body={"query": {"match_all": {}}},
        size=100,
    )
    hits = result.get("hits", {}).get("hits", [])
    if not hits:
        return None, None

    if account_id_filter:
        for h in hits:
            if str(h["_source"].get("chatwoot_account_id")) == str(account_id_filter):
                return h["_source"], h["_id"]
        logging.warning(f"Account ID {account_id_filter} not found in ES — using first account.")

    return hits[0]["_source"], hits[0]["_id"]


def _flatten_keys(obj, prefix=""):
    """Recursively yield dot-notation keys from a dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            full = f"{prefix}.{k}" if prefix else k
            yield from _flatten_keys(v, full)
    elif isinstance(obj, list):
        if obj:
            yield from _flatten_keys(obj[0], f"{prefix}[]")
        else:
            yield f"{prefix}[] (empty)"
    else:
        yield prefix


def _is_populated(value):
    """Return True if the value contains meaningful data."""
    if value is None:
        return False
    if value == "" or value == [] or value == {}:
        return False
    return True


def _summarise(label, data):
    """Print a formatted summary of all fields in a response dict or list."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")

    if data is None:
        print("  [no data — request failed or endpoint not available]")
        return

    # Normalise to a single sample object
    sample = data
    if isinstance(data, list):
        if not data:
            print("  [empty list — no records returned]")
            return
        sample = data[0]
        print(f"  (showing first of {len(data)} record(s))\n")
    elif isinstance(data, dict):
        # Some endpoints wrap in a 'data' key
        if "data" in data and isinstance(data["data"], (dict, list)):
            inner = data["data"]
            if isinstance(inner, list):
                if not inner:
                    print("  [empty list under 'data' key]")
                    return
                sample = inner[0]
                print(f"  (showing first of {len(inner)} record(s) under .data)\n")
            else:
                sample = inner

    if not isinstance(sample, dict):
        print(f"  value: {sample}")
        return

    # Print field-by-field
    _print_fields(sample)


def _print_fields(obj, indent=0):
    """Recursively print fields with populated/empty status."""
    pad = "  " * indent
    for key, value in obj.items():
        populated = _is_populated(value)
        marker = "✓" if populated else "·"

        if isinstance(value, dict):
            print(f"  {pad}{marker} {key}: {{...}}")
            _print_fields(value, indent + 1)
        elif isinstance(value, list):
            if value and isinstance(value[0], dict):
                print(f"  {pad}{marker} {key}: [array of objects, {len(value)} items]")
                _print_fields(value[0], indent + 1)
            elif value:
                display = str(value[:3])[0:80]
                print(f"  {pad}{marker} {key}: {display}{'...' if len(value) > 3 else ''}")
            else:
                print(f"  {pad}· {key}: [] (empty)")
        else:
            display = str(value)
            if len(display) > 80:
                display = display[:77] + "..."
            print(f"  {pad}{marker} {key}: {display}")


# ---------------------------------------------------------------------------
# Endpoint probes
# ---------------------------------------------------------------------------


def probe_summary(account_id, headers):
    """GET /api/v2/accounts/{id}/reports/summary (account-level KPIs)."""
    data = _get(
        f"{CHATWOOT_BASE_URL}/api/v2/accounts/{account_id}/reports/summary",
        headers,
        params={"since": "1704067200", "until": "9999999999"},
    )
    _summarise("Reports Summary  (/api/v2/accounts/{id}/reports/summary)", data)
    return data


def probe_conversations(account_id, headers):
    """GET /api/v1/accounts/{id}/conversations (paginated list)."""
    data = _get(
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/conversations",
        headers,
        params={"page": 1},
    )
    # Conversations endpoint returns {"data": {"meta": {...}, "payload": [...]}}
    label = "Conversations  (/api/v1/accounts/{id}/conversations)"
    if data and isinstance(data, dict):
        meta = data.get("data", {}).get("meta", {})
        payload = data.get("data", {}).get("payload", [])
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
        print(f"  Meta: {meta}")
        if payload:
            print(f"  (showing first of {len(payload)} conversation(s))\n")
            _print_fields(payload[0])
        else:
            print("  [no conversations found]")
    return data


def probe_contacts(account_id, headers):
    """GET /api/v1/accounts/{id}/contacts (paginated list)."""
    data = _get(
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/contacts",
        headers,
        params={"page": 1, "include_contacts": "true"},
    )
    _summarise("Contacts  (/api/v1/accounts/{id}/contacts)", data)
    return data


def probe_agents(account_id, headers):
    """GET /api/v1/accounts/{id}/agents."""
    data = _get(
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/agents",
        headers,
    )
    _summarise("Agents  (/api/v1/accounts/{id}/agents)", data)
    return data


def probe_inboxes(account_id, headers):
    """GET /api/v1/accounts/{id}/inboxes."""
    data = _get(
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/inboxes",
        headers,
    )
    # Response: {"payload": [...]}
    if data and "payload" in data:
        _summarise("Inboxes  (/api/v1/accounts/{id}/inboxes)", data["payload"])
    else:
        _summarise("Inboxes  (/api/v1/accounts/{id}/inboxes)", data)
    return data


def probe_teams(account_id, headers):
    """GET /api/v1/accounts/{id}/teams."""
    data = _get(
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/teams",
        headers,
    )
    _summarise("Teams  (/api/v1/accounts/{id}/teams)", data)
    return data


def probe_labels(account_id, headers):
    """GET /api/v1/accounts/{id}/labels."""
    data = _get(
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/labels",
        headers,
    )
    # Response: {"payload": [...]}
    if data and "payload" in data:
        _summarise("Labels  (/api/v1/accounts/{id}/labels)", data["payload"])
    else:
        _summarise("Labels  (/api/v1/accounts/{id}/labels)", data)
    return data


def probe_csat(account_id, headers):
    """GET /api/v1/accounts/{id}/reports/agents/conversations (CSAT / agent perf)."""
    data = _get(
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/reports/agents/conversations",
        headers,
        params={"since": "1704067200", "until": "9999999999"},
    )
    _summarise("Agent Conversation Report  (/api/v1/accounts/{id}/reports/agents/conversations)", data)
    return data


def probe_overview(account_id, headers):
    """GET /api/v1/accounts/{id}/reports/overview (live agent/queue status)."""
    data = _get(
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/reports/overview",
        headers,
    )
    _summarise("Overview Report  (/api/v1/accounts/{id}/reports/overview)", data)
    return data


def probe_custom_attributes(account_id, headers):
    """GET /api/v1/accounts/{id}/custom_attribute_definitions."""
    data = _get(
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/custom_attribute_definitions",
        headers,
        params={"attribute_model": "conversation_attribute"},
    )
    _summarise("Custom Attribute Definitions (conversation)  (/api/v1/accounts/{id}/custom_attribute_definitions)", data)
    data2 = _get(
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/custom_attribute_definitions",
        headers,
        params={"attribute_model": "contact_attribute"},
    )
    _summarise("Custom Attribute Definitions (contact)  (/api/v1/accounts/{id}/custom_attribute_definitions)", data2)
    return data, data2


def probe_individual_metrics(account_id, headers):
    """
    GET /api/v2/accounts/{id}/reports for each metric currently being collected.
    Shows a sample of the time-series data format.
    """
    metrics = [
        "conversations_count",
        "incoming_messages_count",
        "outgoing_messages_count",
        "resolutions_count",
        "avg_first_response_time",
        "avg_resolution_time",
        "avg_reply_time",
    ]
    print(f"\n{'='*60}")
    print("  Per-Metric Time-Series  (/api/v2/accounts/{id}/reports)")
    print(f"{'='*60}")
    for metric in metrics:
        data = _get(
            f"{CHATWOOT_BASE_URL}/api/v2/accounts/{account_id}/reports",
            headers,
            params={
                "type": "account",
                "metric": metric,
                "since": "1704067200",
                "until": "9999999999",
            },
        )
        if isinstance(data, list) and data:
            non_zero = [p for p in data if p.get("value") not in (None, 0, "0")]
            print(f"  ✓ {metric}: {len(data)} data points, {len(non_zero)} non-zero")
        elif isinstance(data, list):
            print(f"  · {metric}: returned empty list")
        else:
            print(f"  · {metric}: {data}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(params):
    lib.logging.define("chatwoot-explore-fields")

    account_id_filter = params.get("account_id")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return

    account, account_esid = _load_account(es, account_id_filter)
    if account is None:
        logging.error("No Chatwoot accounts found in de_asset_chatwoot_accounts.")
        return

    account_id = account.get("chatwoot_account_id")
    account_title = account.get("chatwoot_account_title", "unknown")
    encrypted_token = account.get("chatwoot_api_token")

    if not account_id or not encrypted_token:
        logging.error("Account is missing chatwoot_account_id or chatwoot_api_token.")
        return

    api_token = decrypt(encrypted_token)
    headers = {"api_access_token": api_token}

    print(f"\n{'#'*60}")
    print(f"  Chatwoot Field Explorer")
    print(f"  Account: {account_title}  (ID: {account_id})")
    print(f"  ESID: {account_esid}")
    print(f"{'#'*60}")
    print("\nKey:  ✓ = field has data    · = field is empty/null\n")

    probe_summary(account_id, headers)
    probe_individual_metrics(account_id, headers)
    probe_conversations(account_id, headers)
    probe_contacts(account_id, headers)
    probe_agents(account_id, headers)
    probe_inboxes(account_id, headers)
    probe_teams(account_id, headers)
    probe_labels(account_id, headers)
    probe_csat(account_id, headers)
    probe_overview(account_id, headers)
    probe_custom_attributes(account_id, headers)

    print(f"\n{'#'*60}")
    print("  Exploration complete.")
    print(f"{'#'*60}\n")
