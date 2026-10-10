import os
import sys
import json
import re
import time
import unicodedata

ECHO_RETRY_DELAYS = [60, 120, 240]  # seconds for escalating sleep on rate limits

from datetime import datetime, timedelta, time as datetime_time, timezone
from difflib import SequenceMatcher
import requests
import logging

# Zero-width and invisible Unicode characters common in Arabic/Hindi text
_ZW_CHAR_RE = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]")

# Map common Arabic/Persian confusable characters to a canonical form so that
# visually identical names stored with different code points still match.
# e.g. Arabic yeh ي (U+064A) and alef maksura ى (U+0649) -> Farsi yeh ی (U+06CC)
#      Arabic kaf ك (U+0643) -> Farsi kaf ک (U+06A9)
_ARABIC_NORMALIZE_MAP = str.maketrans(
    {
        "\u064a": "\u06cc",  # Arabic yeh -> Farsi yeh
        "\u0649": "\u06cc",  # Arabic alef maksura -> Farsi yeh
        "\u0643": "\u06a9",  # Arabic kaf -> Farsi kaf
    }
)
from lib.es_helpers import get_all_documents_paginated, get_elasticsearch_client
from lib.import_scope import scope_resolution_error
from lib.echo_base_url import EchoSubdomainError, echo_base_url
from lib.paths import safe_path_join
from import_scripts.echo.api_ids import is_safe_echo_id

# module constants so tests can point the importer at temp dirs.
CONVERSATIONS_DIR = "/app/data/echo/conversations"
CONVERSATIONS_CLOSED_DIR = "/app/data/echo/conversations_closed"
CONVERSATIONS_EXPIRED_DIR = "/app/data/echo/conversations_expired"
from lib.encryption import decrypt
from lib.country_codes import resolve_country_to_iso2
from import_scripts.echo.outcome_flags import compute_outcome_flags

# the created_at pull used to learn "this conversation is already
# closed and imported" only from a file in conversations_closed/. The retention
# sweep purges that JSON once it has been indexed for 7 days, so the truth now
# lives in Elasticsearch: a conversation is done when its ES doc carries a
# closed/blocked status. One ids lookup per closed entry, memoised per run.
ECHO_INDEX_PATTERN = "*_echo_conversations"
_CLOSED_LOOKUP_CACHE = {}


def reset_closed_lookup_cache():
    _CLOSED_LOOKUP_CACHE.clear()


def conversation_closed_in_es(es, entry_id):
    """True when ES already holds ``entry_id`` with status closed or blocked.

    False when the doc is absent, still open, or the lookup is impossible
    (``es`` is None or the search fails) — a failed lookup must never skip a
    conversation, because saving it again is harmless while skipping it
    could lose its final state. Failures are not cached, so a transient ES
    error does not pin a wrong answer for the rest of the run.
    """
    if es is None or entry_id in (None, ""):
        return False
    key = str(entry_id)
    if key in _CLOSED_LOOKUP_CACHE:
        return _CLOSED_LOOKUP_CACHE[key]
    try:
        resp = es.search(
            index=ECHO_INDEX_PATTERN,
            body={"size": 1, "_source": ["status"], "query": {"ids": {"values": [key]}}},
        )
        hits = resp["hits"]["hits"]
        closed = False
        if isinstance(hits, list):
            for hit in hits:
                status = str(((hit.get("_source") or {}).get("status")) or "").lower()
                closed = status in ("closed", "blocked")
    except Exception as exc:  # noqa: BLE001 — any lookup failure means "not known closed"
        logging.warning(f"ES closed-status lookup failed for conversation {key}: {type(exc).__name__}")
        return False
    _CLOSED_LOOKUP_CACHE[key] = closed
    return closed


from import_scripts.facebook.get_page_insights import _resolve_team_country
import lib.logging
from elasticsearch import Elasticsearch


def log_params(params):
    lib.logging.define("echo-get-conversations")
    for key, value in params.items():
        logging.info(f"{key}: {value}")


def get_unix_timestamp(date_string, end_of_day=False):
    lib.logging.define("echo-get-conversations")
    logging.info(f"Called with date_string: {date_string}, end_of_day: {end_of_day}")
    dt = datetime.strptime(date_string, "%Y-%m-%d")
    if end_of_day:
        dt = datetime.combine(dt, datetime_time(23, 59, 59))
    timestamp = int(dt.timestamp())
    logging.info(f"Converted date {date_string} to timestamp: {timestamp}")
    return timestamp


def sanitize_directory_name(name):
    lib.logging.define("echo-get-conversations")
    logging.info(f"Sanitizing directory name: {name}")
    valid_chars = "-_.() abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    sanitized_name = "".join(c for c in name if c in valid_chars)
    logging.info(f"Sanitized name: {sanitized_name}")
    return sanitized_name


def create_directory(directory_path):
    lib.logging.define("echo-get-conversations")
    logging.info(f"Checking if directory exists: {directory_path}")
    if not os.path.exists(directory_path):
        logging.info(f"Directory does not exist. Creating: {directory_path}")
        os.makedirs(directory_path)


def save_to_file(directory, filename, data):
    lib.logging.define("echo-get-conversations")
    # raises UnsafePathError if the name would leave ``directory``.
    filepath = safe_path_join(directory, filename)
    logging.info(f"Saving data to {filepath}")
    with open(filepath, "w") as file:
        json.dump(data, file)


def batch_date_ranges(start_date, end_date):
    lib.logging.define("echo-get-conversations")
    logging.info(f"Batching date ranges between {start_date} and {end_date}")
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    delta = end_dt - start_dt
    date_ranges = []

    if delta.days <= 2:
        logging.info("Date range is within 2 days.")
        return [(start_date, end_date)]
    else:
        logging.info("Date range exceeds 2 days, batching in chunks.")
        current_start = start_dt
        while current_start <= end_dt:
            current_end = min(current_start + timedelta(days=2), end_dt)
            date_ranges.append((current_start.strftime("%Y-%m-%d"), current_end.strftime("%Y-%m-%d")))
            current_start = current_end + timedelta(days=1)
        logging.info(f"Batched date ranges: {date_ranges}")
        return date_ranges


def get_tenants(tenant_esids=None):
    """Fetch Echo tenant docs.

    paginated (the old single-shot size=1000 silently dropped
    tenants past the cap) and, when ``tenant_esids`` is given (a
    ``limit_teams``-scoped run resolved to its tenants), scoped at query time
    so out-of-scope tenants are never loaded. None = all tenants (cron).
    """
    lib.logging.define("echo-get-conversations")
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


def _tenant_esids_for_teams(es, team_esids):
    """resolve ``limit_teams`` team ESIDs to the set of
    ``echo_tenant_esid`` values they belong to, so a scoped run fetches (and
    contacts) ONLY those tenants. Sibling of get_user_stats'
    resolve_tenant_esids_for_teams, kept local for the module's own
    log prefix. Empty result = the scoped run has nothing in scope.
    """
    lib.logging.define("echo-get-conversations")
    try:
        hits = get_all_documents_paginated("de_asset_teams", {"terms": {"_id": list(team_esids)}}, es=es)
        tenant_esids = {h["_source"].get("echo_tenant_esid") for h in hits if h["_source"].get("echo_tenant_esid")}
        logging.info(f"limit_teams {sorted(team_esids)} resolved to tenant esids: {sorted(tenant_esids)}")
        return tenant_esids
    except Exception:
        # was `return set`, which made an ES outage indistinguishable
        # from "these teams own no tenant". The caller's strict-mode guard would
        # then classify an infrastructure fault as no_data_source and tell the user
        # to fix a configuration that was fine. Re-raise; scope is never widened.
        logging.error("Error resolving limit_teams to tenant esids", exc_info=True)
        raise


def get_active_teams(es, tenant_esid, tenant_subdomain, exclude_teams=None, limit_teams=None):
    lib.logging.define("echo-get-conversations")
    logging.info(f"Fetching active teams for tenant subdomain {tenant_subdomain}")
    must_clauses = [{"match": {"echo_tenant_esid": tenant_esid}}, {"term": {"ds_echo": "true"}}]
    query = {"bool": {"must": must_clauses}}
    if exclude_teams:
        logging.info(f"Excluding teams: {exclude_teams}")
        must_not_clause = {"terms": {"_id": exclude_teams}}
        query["bool"]["must_not"] = [must_not_clause]
    if limit_teams:
        logging.info(f"Limiting to teams: {limit_teams}")
        must_clauses.append({"terms": {"_id": limit_teams}})
    try:
        # paginated — no 1000-teams-per-tenant cap.
        teams = get_all_documents_paginated("de_asset_teams", query, es=es)
        # skip teams that are billing-inactive (no active
        # subscription, or over the paid connection cap — echo is team-scoped, so
        # the team IS the billing source). Surfaces a classified error on the
        # scheduled path (no-op on cron). Fail-open per team.
        from lib.billing_entitlement import filter_entitled_teams

        teams = filter_entitled_teams(teams, connector_id="echo")
        team_names = [t["_source"].get("team_title", "unknown") for t in teams]
        logging.info(f"Found {len(teams)} active teams for tenant subdomain {tenant_subdomain}: {', '.join(team_names)}")
        return teams
    except Exception as e:
        logging.error(f"Error fetching teams from Elasticsearch: {e}")
        return []


def fetch_all_facebook_pages(es):
    """Fetch every de_asset_fb_pages doc (raw hits) once per run.

    fetched once, then narrowed PER TENANT by build_fb_pages_maps —
    the referrer-match pool must never span tenants.
    """
    lib.logging.define("echo-get-conversations")
    logging.info("Fetching Facebook pages from Elasticsearch")
    try:
        # paginated — no 1000-page cap on the referrer-match cache.
        return get_all_documents_paginated("de_asset_fb_pages", {"match_all": {}}, es=es)
    except Exception as e:
        logging.error(f"Error fetching Facebook pages: {e}")
        return []


def build_fb_pages_maps(pages, allowed_fb_page_ids):
    """Build the referrer→ESID maps from raw page hits, restricted to the
    pages a tenant's own teams reference.

    the maps used to be cluster-wide, so a referrer from tenant A
    could fuzzy-match tenant B's page (name keys + substring/ratio strategies)
    and the conversation — client name, country, language, religion,
    disposition notes — landed on tenant B's dashboards. Only pages whose
    fb_page_id appears in ``allowed_fb_page_ids`` (built from the tenant's
    team docs) enter the pool now.

    Returns:
        Tuple of (fb_pages_map, page_esid_to_fb_page_id):
        - fb_pages_map: referrer key (id / slug / name) → page ESID
        - page_esid_to_fb_page_id: page ESID → fb_page_id
    """
    lib.logging.define("echo-get-conversations")
    fb_pages_map = {}
    page_esid_to_fb_page_id = {}

    for page_hit in pages:
        page = page_hit["_source"]
        page_esid = page_hit["_id"]
        fb_page_id = str(page.get("fb_page_id", "") or "")
        if not fb_page_id or fb_page_id not in allowed_fb_page_ids:
            continue
        fb_page_slug = page.get("fb_page_slug", "")
        fb_page_name = page.get("fb_page_name", "")

        # Map by fb_page_id (primary identifier)
        fb_pages_map[fb_page_id] = page_esid
        page_esid_to_fb_page_id[page_esid] = fb_page_id
        logging.debug(f"Cached FB page: ID={fb_page_id}, ESID={page_esid}")

        # Also map by slug if available (for URL matching)
        if fb_page_slug:
            fb_pages_map[fb_page_slug] = page_esid
            logging.debug(f"Cached FB page: Slug={fb_page_slug}, ESID={page_esid}")

        # Also map by name if available
        if fb_page_name:
            fb_pages_map[fb_page_name] = page_esid
            logging.debug(f"Cached FB page: Name={fb_page_name}, ESID={page_esid}")

    logging.info(f"Tenant FB pool: {len(page_esid_to_fb_page_id)} pages with {len(fb_pages_map)} mappings")
    return fb_pages_map, page_esid_to_fb_page_id


def fetch_all_youtube_channels(es):
    """Fetch every de_asset_youtube_channels doc (raw hits) once per run.

    fetched once, then narrowed PER TENANT by build_yt_channels_map.
    """
    lib.logging.define("echo-get-conversations")
    logging.info("Fetching YouTube channels from Elasticsearch")
    try:
        # paginated — no 1000-channel cap on the referrer-match cache.
        return get_all_documents_paginated("de_asset_youtube_channels", {"match_all": {}}, es=es)
    except Exception as e:
        logging.error(f"Error fetching YouTube channels: {e}")
        return []


def build_yt_channels_map(channels, allowed_channel_esids):
    """Build the referrer→channel-ESID map from raw channel hits, restricted
    to the channels a tenant's own teams reference (see
    build_fb_pages_maps).

    Returns:
        yt_channels_map: Dict mapping title/handle to channel ESID
    """
    lib.logging.define("echo-get-conversations")
    yt_channels_map = {}

    for ch_hit in channels:
        ch = ch_hit["_source"]
        ch_esid = ch_hit["_id"]
        if ch_esid not in allowed_channel_esids:
            continue
        title = ch.get("youtube_channel_title", "")
        handle = ch.get("youtube_channel_handle", "")

        if title:
            yt_channels_map[title] = ch_esid
            logging.debug(f"Cached YT channel: title='{title}', ESID={ch_esid}")

        if handle:
            yt_channels_map[handle] = ch_esid
            # Also store without '@' prefix
            if handle.startswith("@"):
                yt_channels_map[handle[1:]] = ch_esid
            logging.debug(f"Cached YT channel: handle='{handle}', ESID={ch_esid}")

    logging.info(f"Tenant YT pool: {len(yt_channels_map)} mappings")
    return yt_channels_map


def get_tenant_team_maps(es, tenant_esid):
    """Build the asset→team resolution dicts for ONE tenant's teams.

    team resolution used to be a per-conversation ES query over
    de_asset_teams with no echo_tenant_esid clause and size=1 — an arbitrary
    team anywhere in the cluster could win. Resolution is now a dict lookup
    built exclusively from the teams of the tenant being imported, which also
    makes the "no active team in tenant {tenant_esid}" log lines true.

    Deliberately broader than get_active_teams: no ds_echo / billing /
    limit_teams narrowing, mirroring the old resolver's "any team owning the
    asset" semantics minus the cross-tenant reach. The per-conversation
    limit_teams filter still applies downstream.

    Returns:
        Tuple of (fb_page_id_to_team, yt_esid_to_team), each mapping the
        asset key → (team_source_dict, team_esid).
    """
    lib.logging.define("echo-get-conversations")
    fb_page_id_to_team = {}
    yt_esid_to_team = {}
    try:
        query = {"bool": {"must": [{"match": {"echo_tenant_esid": tenant_esid}}]}}
        teams = get_all_documents_paginated("de_asset_teams", query, es=es)
    except Exception as e:
        logging.error(f"Error fetching teams for tenant {tenant_esid}: {e}")
        return {}, {}

    # Sort for deterministic collision resolution (first team by ESID wins,
    # instead of the old resolver's arbitrary size=1 winner).
    for team_hit in sorted(teams, key=lambda h: h["_id"]):
        team = team_hit["_source"]
        team_esid = team_hit["_id"]

        fb_ids = team.get("team_facebook_page_id") or []
        if not isinstance(fb_ids, list):
            fb_ids = [fb_ids]
        for fb_id in fb_ids:
            fb_id = str(fb_id or "")
            if not fb_id:
                continue
            if fb_id in fb_page_id_to_team:
                logging.warning(
                    f"FB page ID {fb_id} referenced by multiple teams in tenant {tenant_esid} "
                    f"({fb_page_id_to_team[fb_id][1]} and {team_esid}) — keeping {fb_page_id_to_team[fb_id][1]}"
                )
                continue
            fb_page_id_to_team[fb_id] = (team, team_esid)

        yt_esids = team.get("team_youtube_channel_handle_esids") or []
        if not isinstance(yt_esids, list):
            yt_esids = [yt_esids]
        for yt_esid in yt_esids:
            if not yt_esid:
                continue
            if yt_esid in yt_esid_to_team:
                logging.warning(
                    f"YouTube channel ESID {yt_esid} referenced by multiple teams in tenant {tenant_esid} "
                    f"({yt_esid_to_team[yt_esid][1]} and {team_esid}) — keeping {yt_esid_to_team[yt_esid][1]}"
                )
                continue
            yt_esid_to_team[yt_esid] = (team, team_esid)

    logging.info(
        f"Tenant {tenant_esid}: {len(fb_page_id_to_team)} FB page → team mappings, "
        f"{len(yt_esid_to_team)} YouTube channel → team mappings"
    )
    return fb_page_id_to_team, yt_esid_to_team


def normalize_referrer(referrer):
    """
    Normalize referrer values for matching.

    Handles:
    - Case normalization (lowercase)
    - Whitespace trimming
    - URL parsing (extracts page name from facebook.com/PageName)
    - Multiple URL formats (facebook.com, fb.com, m.facebook.com)

    Args:
        referrer: Raw referrer string from Echo conversation

    Returns:
        Tuple of (normalized_referrer, extracted_variants)
        - normalized_referrer: Primary normalized value
        - extracted_variants: List of additional values to try (e.g., URL path component)
    """
    lib.logging.define("echo-get-conversations")

    if not referrer:
        return None, []

    # Strip whitespace and convert to lowercase for primary normalized value
    normalized = referrer.strip().lower()
    variants = []

    # Check if referrer looks like a URL.
    # codeql[py/incomplete-url-substring-sanitization]: this is a
    # heuristic classifier for tagging Echo conversation referrers, not a
    # security check. False positive on `facebook.com` / `fb.com` substring.
    if "/" in normalized or "facebook.com" in normalized or "fb.com" in normalized:
        # Try to extract page name from URL patterns
        # Examples:
        #   "facebook.com/PageName" -> "PageName"
        #   "https://www.facebook.com/PageName/" -> "PageName"
        #   "m.facebook.com/PageName" -> "PageName"

        # Remove protocol
        url_part = normalized.replace("https://", "").replace("http://", "")

        # Remove domain prefixes
        for domain in [
            "www.facebook.com/",
            "m.facebook.com/",
            "facebook.com/",
            "fb.com/",
        ]:
            if url_part.startswith(domain):
                path = url_part[len(domain) :]
                # Extract first path component (before next slash or query string)
                page_name = path.split("/")[0].split("?")[0]
                if page_name:
                    variants.append(page_name)
                break

    logging.debug(f"Normalized referrer '{referrer}' -> primary='{normalized}', variants={variants}")
    return normalized, variants


def _normalize_for_matching(text):
    """Strip zero-width chars, NFKC-normalize, apply Arabic/Persian char mappings, lowercase, and strip whitespace."""
    if not text:
        return text
    text = _ZW_CHAR_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_ARABIC_NORMALIZE_MAP)
    return text.strip().lower()


def _clean_referrer(text):
    """
    Remove SQL-escaping artifacts from referrer strings.
    Echo sometimes returns names wrapped in single quotes with internal
    apostrophes doubled (e.g. "'İsa''yı Keşfet- UK'" -> "İsa'yı Keşfet- UK").
    """
    if not text:
        return text
    # Collapse doubled apostrophes: '' -> '
    text = re.sub(r"'{2,}", "'", text)
    # Strip wrapping single or double quotes
    text = text.strip("'\"")
    return text


def find_fb_page_with_fuzzy_matching(referrer, asset_map):
    """
    Attempt to find an asset ESID (FB page or YouTube channel) using fuzzy matching.

    Strategies tried in order:
    1. Exact match
    2. Case-insensitive + Unicode NFC + zero-width char stripping
    3. URL-extracted variants (for Facebook URLs)
    4. No-space comparison (e.g. "WayFinder" matches "Way Finder")
    5. Partial match: asset name is a substring of referrer
       (e.g. "Way Finder" matches "Way Finder في العالم")
    6. Partial match: referrer is a substring of asset name

    Args:
        referrer: Raw referrer string from Echo conversation
        asset_map: Dict mapping names/IDs/handles to ESIDs

    Returns:
        Tuple of (esid, match_type, matched_key) or (None, None, None)
    """
    lib.logging.define("echo-get-conversations")

    if not referrer:
        return None, None, None

    # Pre-process: strip SQL-escaping artifacts (wrapping quotes, doubled apostrophes)
    referrer = _clean_referrer(referrer)
    if not referrer:
        return None, None, None

    # Strategy 1: Exact match
    if referrer in asset_map:
        logging.debug(f"Referrer '{referrer}' matched via exact match")
        return asset_map[referrer], "exact", referrer

    # Normalize referrer once for all subsequent strategies
    normalized, variants = normalize_referrer(referrer)
    normalized = _normalize_for_matching(normalized) if normalized else normalized

    # Build normalized map (zero-width stripped, NFC, lowercase)
    lowercase_map = {_normalize_for_matching(k): (k, v) for k, v in asset_map.items()}

    # Strategy 2: Case-insensitive + Unicode normalization
    if normalized in lowercase_map:
        original_key, esid = lowercase_map[normalized]
        logging.debug(f"Referrer '{referrer}' matched via case-insensitive match to '{original_key}'")
        return esid, "case-insensitive", original_key

    # Strategy 3: URL-extracted variants (Facebook page URLs)
    for variant in variants:
        norm_variant = _normalize_for_matching(variant)
        if norm_variant in lowercase_map:
            original_key, esid = lowercase_map[norm_variant]
            logging.debug(f"Referrer '{referrer}' matched via URL extraction to '{original_key}'")
            return esid, "url-extracted", original_key

    # Strategy 4: No-space comparison (handles "WayFinder" vs "Way Finder")
    if normalized:
        stripped_referrer = re.sub(r"\s+", "", normalized)
        stripped_map = {re.sub(r"\s+", "", k): (orig, v) for k, (orig, v) in lowercase_map.items()}
        if stripped_referrer in stripped_map:
            original_key, esid = stripped_map[stripped_referrer]
            logging.debug(f"Referrer '{referrer}' matched via no-space comparison to '{original_key}'")
            return esid, "no-space", original_key

    # Strategy 5: Partial match — asset name is contained within referrer
    # e.g. "Way Finder" (DEAM) found inside "Way Finder في العالم" (referrer)
    if normalized:
        for norm_key, (orig_key, esid) in lowercase_map.items():
            if norm_key and len(norm_key) >= 5 and norm_key in normalized:
                logging.debug(f"Referrer '{referrer}' partial-matched (asset in referrer) to '{orig_key}'")
                return esid, "partial", orig_key

    # Strategy 6: Partial match — referrer is contained within asset name
    if normalized and len(normalized) >= 5:
        for norm_key, (orig_key, esid) in lowercase_map.items():
            if norm_key and normalized in norm_key:
                logging.debug(f"Referrer '{referrer}' partial-matched (referrer in asset) to '{orig_key}'")
                return esid, "partial", orig_key

    # Strategy 7: Fuzzy similarity (handles morphological variants like "Keşfet" vs "KeşfedİIn")
    # Uses SequenceMatcher ratio; both strings must be ≥8 chars to avoid short-string false positives.
    if normalized and len(normalized) >= 8:
        best_ratio = 0.0
        best_esid = best_orig = None
        for norm_key, (orig_key, esid) in lowercase_map.items():
            if norm_key and len(norm_key) >= 8:
                ratio = SequenceMatcher(None, normalized, norm_key).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_esid = esid
                    best_orig = orig_key
        if best_ratio >= 0.75:
            logging.debug(f"Referrer '{referrer}' fuzzy-matched (ratio={best_ratio:.2f}) to '{best_orig}'")
            return best_esid, "fuzzy", best_orig

    logging.debug(f"Referrer '{referrer}' did not match any asset after all strategies")
    return None, None, None


def discover_echo_schema(graphql_url, headers):
    """
    Run introspection queries to log the Conversation type fields, Referrer type fields,
    and the arguments accepted by the conversations query. Call once per tenant.
    """
    lib.logging.define("echo-get-conversations")
    logging.info("[SCHEMA DISCOVERY] Querying Echo GraphQL schema...")
    introspection_queries = {
        "Conversation type fields": '{ __type(name: "Conversation") { fields { name type { name kind ofType { name kind } } } } }',
        "Referrer type fields": '{ __type(name: "Referrer") { fields { name type { name kind } } } }',
        "All query root args": "{ __schema { queryType { fields { name args { name defaultValue type { name kind ofType { name kind } } } } } } }",
    }
    for label, q in introspection_queries.items():
        try:
            r = requests.post(graphql_url, json={"query": q}, headers=headers)
            r.raise_for_status()
            data = r.json()
            logging.info(f"[SCHEMA DISCOVERY] {label}:\n{json.dumps(data.get('data'), indent=2)}")
        except Exception as e:
            logging.error(f"[SCHEMA DISCOVERY] Failed for '{label}': {e}")


def fetch_conversations_graphql(base_url, headers, start_date, end_date, rate_state, date_field=None):
    """
    Fetch all conversations for a date range via GraphQL with cursor-based pagination.
    rate_state: dict with 'calls' key for tracking API calls per minute.

    date_field: which date the startDate/endDate window filters on. None
    (default) sends no ``dateField`` argument, so the API defaults to ``created_at``
    — the original, unchanged behavior. Pass ``"closed_at"`` for the refresh pull,
    which re-fetches conversations RESOLVED in the window (any creation date) so
    their final status/outcome/messages can be upserted over the stale doc.

    Returns a flat list of conversation dicts, or None on failure.
    """
    lib.logging.define("echo-get-conversations")
    graphql_url = f"{base_url}/graphql"
    all_conversations = []
    cursor = None
    page = 0
    start_dt = f"{start_date}T00:00:00Z"
    end_dt = f"{end_date}T23:59:59Z"
    # inject the dateField arg only when explicitly requested, so the
    # created_at pull's query is byte-for-byte unchanged.
    date_field_arg = f', dateField: "{date_field}"' if date_field else ""

    while True:
        page += 1
        after_arg = f', after: "{cursor}"' if cursor else ""
        query = """
        {
            conversation(startDate: "%s", endDate: "%s"%s%s) {
                pageInfo {
                    hasNextPage
                    endCursor
                }
                nodes {
                    id
                    status
                    topic
                    createdAt
                    closedAt
                    locale
                    filterLabel
                    objectClass
                    outcomeTitle
                    incomingMessagesCount
                    outboundMessagesCount
                    referrer {
                        name
                        category
                        icon
                    }
                    user {
                        id
                        name
                    }
                    tags {
                        id
                        name
                    }
                    disposition {
                        completed
                        notes
                        outcome {
                            id
                            title
                        }
                    }
                    client {
                        id
                        name
                        country
                        language
                        religion
                        conversationsCount(startDate: "%s", endDate: "%s")
                    }
                    webContact {
                        utmCampaign
                        utmSource
                        utmMedium
                        utmTerm
                        url
                    }
                }
            }
        }
        """ % (
            start_dt,
            end_dt,
            date_field_arg,
            after_arg,
            start_dt,
            end_dt,
        )

        response = None
        for attempt, delay in enumerate([0] + ECHO_RETRY_DELAYS):
            if delay:
                logging.warning(f"GraphQL request retry (page {page}, attempt {attempt}), sleeping {delay}s...")
                time.sleep(delay)
            try:
                response = requests.post(graphql_url, json={"query": query}, headers=headers)
                response.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                logging.error(f"GraphQL request failed (page {page}, attempt {attempt + 1}): {e}")
                response = None
        if response is None:
            return None

        try:
            data = response.json()
        except json.JSONDecodeError:
            logging.error(f"Invalid JSON from GraphQL (page {page}): {response.text}")
            return None

        if "errors" in data:
            logging.error(f"GraphQL errors on page {page}: {data['errors']}")
            return None

        rate_state["calls"] += 1
        if rate_state["calls"] >= 90:
            sleep_count = rate_state.get("sleeps", 0)
            delay = ECHO_RETRY_DELAYS[min(sleep_count, len(ECHO_RETRY_DELAYS) - 1)]
            logging.info(f"Sleeping {delay}s for Echo API rate limit (sleep #{sleep_count + 1})")
            time.sleep(delay)
            rate_state["calls"] = 0
            rate_state["sleeps"] = sleep_count + 1

        conversations_data = data.get("data", {}).get("conversation", {})
        nodes = conversations_data.get("nodes", [])
        logging.info(f"GraphQL page {page}: fetched {len(nodes)} conversations")
        all_conversations.extend(nodes)

        page_info = conversations_data.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return all_conversations


# (data minimisation, a teammate 2026-09-08) — per-seeker fields the dashboards
# never read and the index must not hold: the seeker's name, opening message,
# religion, country, language, agent notes, contact handle and landing URL (plus
# four dead legacy keys). Removed from every entry before it is saved, so they
# never reach disk or Elasticsearch. Derived aggregates (contact_country_iso2,
# client_id, client_conversations_count, claimed_by_user_name) are kept — see
# the private docs
PERSON_FIELDS_DROPPED = (
    "client_name",
    "topic",
    "client_religion",
    "client_country",
    "client_language",
    "disposition_notes",
    "notes",
    "client_contact",
    "utm_url",
    "name",
    "user",
    "link",
    "pdf",
)


def minimise_entry(entry: dict) -> dict:
    """Strip PERSON_FIELDS_DROPPED from an entry in place."""
    for key in PERSON_FIELDS_DROPPED:
        entry.pop(key, None)
    return entry


def process_conversation_entry(
    entry,
    es,
    fb_pages_map,
    page_esid_to_fb_page_id,
    yt_channels_map,
    fb_page_id_to_team,
    yt_esid_to_team,
    tenant_esid,
    base_url,
    limit_teams_list,
    match_stats,
    skip_closed_on_disk=True,
):
    """Normalize one GraphQL conversation node, match it to a team, and save it to disk.

    Extracted verbatim from main's per-entry loop so both the created_at
    pull and the closed_at refresh pull share identical processing. Behavior is
    unchanged for the created path.

    ``skip_closed_on_disk`` controls the one refresh-specific divergence: the
    created_at pull skips a closed/blocked conversation that already exists in the
    conversations_closed/ archive OR whose ES doc is already closed/blocked
    (the archive JSON is purged after indexing), but the closed_at REFRESH pull
    (skip_closed_on_disk=False) must re-write it so the conversation's now-final
    status/outcome/messages overwrite the stale creation-day doc. The three former
    ``continue`` statements are now ``return`` (one entry per call).
    """
    # Normalize GraphQL response to match expected flat structure
    referrer_obj = entry.get("referrer")
    if isinstance(referrer_obj, dict):
        # capture the conversation's messaging channel before we
        # collapse referrer to its name. Echo's Referrer object carries the
        # platform in `category` ("Facebook"/"Instagram"/"Whatsapp") and a
        # Font Awesome class in `icon` ("fab fa-facebook-messenger" etc.). The
        # report renders the icon next to the category label. Note "Facebook"
        # is really Messenger transport — only the icon distinguishes it.
        entry["referrer_channel"] = referrer_obj.get("category")
        entry["referrer_channel_icon"] = referrer_obj.get("icon")
        entry["referrer"] = referrer_obj.get("name")
    if "outcomeTitle" in entry:
        entry["outcome"] = entry.pop("outcomeTitle")
    # Convert createdAt (ISO8601 from GraphQL) to created_at (schema format)
    created_at_iso = entry.pop("createdAt", None)
    if created_at_iso:
        try:
            dt = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
            entry["created_at"] = dt.strftime("%B %d, %Y %H:%M:%S UTC")
        except (ValueError, AttributeError):
            entry["created_at"] = created_at_iso
    # Convert closedAt (ISO8601 from GraphQL) to closed_at
    closed_at_iso = entry.pop("closedAt", None)
    if closed_at_iso:
        try:
            dt = datetime.fromisoformat(closed_at_iso.replace("Z", "+00:00"))
            entry["closed_at"] = dt.strftime("%B %d, %Y %H:%M:%S UTC")
        except (ValueError, AttributeError):
            entry["closed_at"] = closed_at_iso
    # Flatten user object -> claimed_by_user_id, claimed_by_user_name
    user_obj = entry.pop("user", None)
    if isinstance(user_obj, dict):
        entry["claimed_by_user_id"] = user_obj.get("id")
        entry["claimed_by_user_name"] = user_obj.get("name")
    # Flatten tags -> tag_names (list of tag name strings)
    tags_raw = entry.pop("tags", None)
    if isinstance(tags_raw, list):
        entry["tag_names"] = [t.get("name") for t in tags_raw if isinstance(t, dict) and t.get("name")]
    # Flatten disposition -> disposition_completed, disposition_notes, disposition_outcome_id/title
    disposition_obj = entry.pop("disposition", None)
    if isinstance(disposition_obj, dict):
        entry["disposition_completed"] = disposition_obj.get("completed", False)
        entry["disposition_notes"] = disposition_obj.get("notes")
        outcome_obj = disposition_obj.get("outcome") or {}
        if isinstance(outcome_obj, dict):
            entry["disposition_outcome_id"] = outcome_obj.get("id")
            entry["disposition_outcome_title"] = outcome_obj.get("title")
        # Populate top-level outcome if not already set by outcomeTitle
        if not entry.get("outcome") and entry.get("disposition_outcome_title"):
            entry["outcome"] = entry["disposition_outcome_title"]
    # Flatten client -> client_id, client_name, client_country, client_language, client_religion
    client_obj = entry.pop("client", None)
    if isinstance(client_obj, dict):
        entry["client_id"] = client_obj.get("id")
        entry["client_name"] = client_obj.get("name")
        entry["client_country"] = client_obj.get("country")
        entry["client_language"] = client_obj.get("language")
        entry["client_religion"] = client_obj.get("religion")
        entry["client_conversations_count"] = client_obj.get("conversationsCount")
        # Resolve client_country to ISO2 for map display
        _client_iso2 = resolve_country_to_iso2(entry.get("client_country"))
        if _client_iso2:
            entry["contact_country_iso2"] = _client_iso2
    # Flatten webContact -> utm_campaign, utm_source, utm_medium, utm_term, utm_url
    web_contact_obj = entry.pop("webContact", None)
    if isinstance(web_contact_obj, dict):
        entry["utm_campaign"] = web_contact_obj.get("utmCampaign")
        entry["utm_source"] = web_contact_obj.get("utmSource")
        entry["utm_medium"] = web_contact_obj.get("utmMedium")
        entry["utm_term"] = web_contact_obj.get("utmTerm")
        entry["utm_url"] = web_contact_obj.get("url")

    referrer = entry.get("referrer")
    entry_id = entry.get("id")
    if not referrer or not entry_id:
        logging.warning("Skipping entry with missing 'referrer' or 'id'")
        return
    # the id names the staging file (written AND removed below);
    # refuse anything that is not a plain token (e.g. "../x").
    if not is_safe_echo_id(entry_id):
        logging.warning(f"Tenant {tenant_esid}: skipping Echo conversation with an unsafe id {str(entry_id)[:80]!r}")
        return

    match_stats["total"] += 1
    matched_team = None
    matched_team_esid = None
    asset_source = None

    # --- Step 1: Try Facebook pages ---
    fb_page_esid, match_type, matched_key = find_fb_page_with_fuzzy_matching(referrer, fb_pages_map)

    if fb_page_esid:
        asset_source = "fb_page"
        fb_page_id = page_esid_to_fb_page_id.get(fb_page_esid)
        logging.info(
            f"[FB PAGE MATCH] Referrer '{referrer}' matched FB page via {match_type} (key: '{matched_key}', ID: {fb_page_id}, ESID: {fb_page_esid})"
        )

        if fb_page_id:
            # stamp the matched Page id on the doc. The value space
            # is Meta page ids (same as fb_insights page_id), so a Team x Page
            # table can split conversation metrics per page instead of lumping
            # the whole team total on one row. Stamped whenever the referrer
            # resolved to a page — even if the team lookup below fails, the
            # unknownteams doc keeps the attribution breadcrumb. YouTube and
            # unmatched referrers carry NO fb_page_id (absent, not empty).
            entry["fb_page_id"] = str(fb_page_id)
            # dict lookup over THIS tenant's teams only — never an
            # unconstrained cluster-wide de_asset_teams query.
            matched_team, matched_team_esid = fb_page_id_to_team.get(str(fb_page_id), (None, None))
        else:
            logging.warning(f"FB page ESID {fb_page_esid} has no fb_page_id — cannot look up team")

        if not matched_team:
            match_stats["fb_page_no_team"] += 1
            logging.error(
                f"[FB PAGE NO TEAM] Referrer '{referrer}' matched FB page ID {fb_page_id} "
                f"(via {match_type}: '{matched_key}'), but no active team in tenant {tenant_esid} "
                f"has this FB page ID in its team_facebook_page_id array."
            )
            # surface as a deduped INFO note (per FB page, not per
            # conversation). "referrer unattributed" pins the info-severity
            # classifier rule; detail=page id is the stable dedup key. No-op on cron.
            from lib.import_run_context import record_user_error

            record_user_error(
                f"echo referrer unattributed: FB page {fb_page_id} ('{matched_key}') on no team",
                connector_id="echo",
                detail=f"FB page {matched_key or fb_page_id}",
            )

    # --- Step 2: Try YouTube channels if FB page didn't yield a team ---
    if not matched_team:
        yt_channel_esid, match_type, matched_key = find_fb_page_with_fuzzy_matching(referrer, yt_channels_map)

        if yt_channel_esid:
            asset_source = "youtube"
            logging.info(
                f"[YT CHANNEL MATCH] Referrer '{referrer}' matched YouTube channel via {match_type} (key: '{matched_key}', ESID: {yt_channel_esid})"
            )
            # dict lookup over THIS tenant's teams only.
            matched_team, matched_team_esid = yt_esid_to_team.get(yt_channel_esid, (None, None))

            if not matched_team:
                match_stats["yt_channel_no_team"] += 1
                logging.error(
                    f"[YT CHANNEL NO TEAM] Referrer '{referrer}' matched YouTube channel ESID {yt_channel_esid} "
                    f"(via {match_type}: '{matched_key}'), but no active team in tenant {tenant_esid} "
                    f"has this channel in team_youtube_channel_handle_esids."
                )
                # deduped INFO note per YouTube channel (see FB page
                # site above). "referrer unattributed" pins the info rule; detail=
                # channel is the stable dedup key. No-op on cron.
                from lib.import_run_context import record_user_error

                record_user_error(
                    f"echo referrer unattributed: YouTube channel {yt_channel_esid} ('{matched_key}') on no team",
                    connector_id="echo",
                    detail=f"YouTube {matched_key or yt_channel_esid}",
                )
        else:
            logging.warning(
                f"[NO MATCH] Referrer '{referrer}' did not match any Facebook page or YouTube channel. "
                f"Conversation will go to unknown."
            )

    # --- Update match type statistics ---
    if matched_team:
        team_title = matched_team.get("team_title", "unknown")
        logging.info(
            f"[MATCH SUCCESS] Conversation {entry_id} assigned to team '{team_title}' (ESID: {matched_team_esid}) via {asset_source} {match_type} match"
        )
        if match_type == "exact":
            match_stats["exact"] += 1
        elif match_type == "case-insensitive":
            match_stats["case_insensitive"] += 1
        elif match_type == "url-extracted":
            match_stats["url_extracted"] += 1
        elif match_type == "no-space":
            match_stats["no_space"] += 1
        elif match_type == "partial":
            match_stats["partial"] += 1
        elif match_type == "fuzzy":
            match_stats["fuzzy"] += 1

    # If limit_teams is set, skip conversations that don't belong to a limited team
    if limit_teams_list and matched_team_esid not in limit_teams_list:
        logging.debug(f"Skipping conversation {entry_id} — matched team {matched_team_esid} " f"not in limit_teams list")
        return

    # FINAL RESULT: assign team data
    if matched_team:
        team_subfolder = matched_team_esid
        team_country = _resolve_team_country(matched_team) or "unknown"
        if isinstance(team_country, list):
            team_country = team_country[0] if team_country else "unknown"
        team_country_iso3_code = "unknown"
        if team_country != "unknown":
            if len(team_country) <= 3:
                # Already an ISO code — use it directly
                team_country_iso3_code = team_country.upper()
            else:
                # Full country name — look up the code
                try:
                    country_query = {"query": {"term": {"country_title": team_country}}}
                    country_result = es.search(
                        index="de_asset_countries",
                        body=country_query,
                        size=1,
                    )
                    country_hits = country_result.get("hits", {}).get("hits", [])
                    if country_hits:
                        team_country_iso3_code = country_hits[0]["_source"].get("country_iso3_code", "unknown")
                except Exception as e:
                    logging.error(f"Error looking up country code for '{team_country}': {e}")
        entry["team_country"] = team_country
        entry["team_country_iso3_code"] = team_country_iso3_code
        entry["team_city"] = matched_team.get("team_city", "unknown")
        entry["team_site_url"] = matched_team.get("team_url", "unknown")
        entry["team_title"] = matched_team.get("team_title", "unknown")
        entry["team_esid"] = matched_team_esid
        entry["organization_title"] = matched_team.get("organization_title", "Unknown")
    else:
        # NO MATCH FOUND
        match_stats["unmatched"] += 1
        logging.error(
            f"[MATCH FAILED] Conversation {entry_id} with referrer '{referrer}' could not be matched to any team. "
            f"No Facebook page or YouTube channel in DEAM matched '{referrer}' (tried exact, case-insensitive, "
            f"URL extraction, no-space, and partial matching). "
            f"ACTION REQUIRED: Add a Facebook page or YouTube channel named '{referrer}' in DEAM and assign it to a team."
        )
        team_subfolder = "unknownteams"
        entry["team_country"] = "unknown"
        entry["team_country_iso3_code"] = "unknown"
        entry["team_city"] = "unknown"
        entry["team_site_url"] = "unknown"
        entry["team_title"] = "unknown"
        entry["team_esid"] = "unknown"
        entry["organization_title"] = "Unknown"

    entry["echo_source"] = base_url
    entry["@timestamp"] = datetime.now(timezone.utc).isoformat()

    # Outcome category flags (exactly one is 1, the rest are 0). —
    # case-insensitive matching shared with the backfill migration; Echo
    # returns "Language Barrier" while the old inline compare expected
    # "Language barrier", so the flag was never set.
    entry.update(compute_outcome_flags(entry.get("outcome", "")))

    filename = f"{entry_id}.json"

    # Determine storage based on conversation status
    status = entry.get("status", "").lower()
    is_closed_or_blocked = status in ["closed", "blocked"]
    if is_closed_or_blocked:
        # Check if a closed/blocked conversation already exists in the archived folders
        closed_dir = os.path.join(
            CONVERSATIONS_CLOSED_DIR,
            tenant_esid,
            team_subfolder,
        )
        expired_dir = os.path.join(
            CONVERSATIONS_EXPIRED_DIR,
            tenant_esid,
            team_subfolder,
        )
        closed_filepath = safe_path_join(closed_dir, filename)
        expired_filepath = safe_path_join(expired_dir, filename)
        # the created_at pull skips an already-archived closed conversation
        # (it's done). The closed_at REFRESH pull must NOT skip — it re-writes the
        # conversation so its now-final status/outcome/messages land in ES, fixing
        # the stale creation-day doc.
        # the archive file is purged 7 days after indexing, so the
        # ES doc's status is the durable signal; the disk check stays as the
        # cheap first look while files still exist.
        if skip_closed_on_disk and (os.path.exists(closed_filepath) or conversation_closed_in_es(es, entry_id)):
            logging.info(f"Skipping conversation {entry_id} because it is already closed (archive or ES).")
            return
        if os.path.exists(expired_filepath):
            os.remove(expired_filepath)
            logging.info(f"Removed conversation {entry_id} from conversations_expired.")

    # Save all conversations to /app/data/echo/conversations
    base_path = CONVERSATIONS_DIR
    directory_path = os.path.join(base_path, tenant_esid, team_subfolder)
    create_directory(directory_path)

    # Remove duplicates from the other base folders to ensure only one copy exists on disk
    duplicate_bases = [
        CONVERSATIONS_DIR,
        CONVERSATIONS_CLOSED_DIR,
        CONVERSATIONS_EXPIRED_DIR,
    ]
    for dup_base in duplicate_bases:
        if dup_base != base_path:
            dup_dir = os.path.join(dup_base, tenant_esid, team_subfolder)
            dup_filepath = safe_path_join(dup_dir, filename)
            if os.path.exists(dup_filepath):
                os.remove(dup_filepath)
                logging.info(f"Removed duplicate conversation {entry_id} from {dup_filepath}")

    # Always update (overwrite) the file in /app/data/echo/conversations
    minimise_entry(entry)
    save_to_file(directory_path, filename, entry)
    logging.info(f"Saved conversation {entry_id} for tenant {tenant_esid}, team {team_subfolder}")


def main(params):
    lib.logging.define("echo-get-conversations")
    reset_closed_lookup_cache()  # per-run memo of ES closed-status lookups
    logging.info("Starting main function")
    log_params(params)

    # bind this run's progress doc so the per-referrer "matched an
    # asset no team owns" notes (the [FB PAGE NO TEAM] / [YT CHANNEL NO TEAM]
    # sites below) surface in the Report Scheduler as deduped info notes. The
    # parent (echo/run_daily_imports.py) threads --progress_run_id to this
    # subprocess; no-op on the cron path (None run_id).
    from lib.import_run_context import set_run_id

    set_run_id(params.get("progress_run_id"))

    start_date = params.get("start_date")
    end_date = params.get("end_date")
    exclude_teams = params.get("exclude_teams")
    limit_teams = params.get("limit_teams")
    # mode="refresh" runs the closed_at refresh pull — re-fetch
    # conversations RESOLVED in the window (any creation date) and upsert their
    # final status/outcome/messages over the stale creation-day doc. Default
    # mode (created_at, skip already-archived closed convs) is unchanged.
    mode = (params.get("mode") or "created").strip().lower()
    is_refresh = mode == "refresh"
    date_field = "closed_at" if is_refresh else None
    skip_closed_on_disk = not is_refresh
    if exclude_teams and limit_teams:
        logging.error("Cannot set both exclude_teams and limit_teams. Only one should be set.")
        sys.exit(1)

    # (mirrors run_daily_imports' guard): get_conversations
    # is directly invocable via `import.py echo get_conversations`, and the
    # scheduler-dispatched parent always threads --limit_teams through, so a
    # BARE direct invocation (no scope flag at all) is a misconfiguration —
    # not a cron sweep (that path was retired at). Refuse it BEFORE
    # any ES connection or Echo API call.
    #
    # exclude_teams-only runs stay allowed: pinned them as a real
    # manual call path (a tenant's remaining teams may still be in scope),
    # and the per-tenant attribution pools built below make even a broad run
    # tenant-safe — a conversation can no longer attribute outside the tenant
    # it was pulled from.
    if not exclude_teams:
        from lib.import_scope import require_scope

        require_scope(params, scope_param="limit_teams", connector_id="echo")

    exclude_teams_list = [team_id.strip() for team_id in exclude_teams.split(",")] if exclude_teams else []
    limit_teams_list = [team_id.strip() for team_id in limit_teams.split(",")] if limit_teams else []
    if not start_date or not end_date:
        if is_refresh:
            # Trailing 7-day closed_at window (the Echo API per-request span cap).
            # A simple UTC window is sufficient: daily overlapping windows give each
            # closure ~7 chances to be caught, so exact tz boundaries don't matter
            # for the refresh (tenant-tz precision is a custom-range-UI concern).
            today = datetime.now()
            start_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            end_date = today.strftime("%Y-%m-%d")
        else:
            yesterday = datetime.now() - timedelta(days=1)
            start_date = yesterday.strftime("%Y-%m-%d")
            end_date = yesterday.strftime("%Y-%m-%d")
    logging.info(
        f"Using start_date: {start_date}, end_date: {end_date} (mode={mode}, date_field={date_field or 'created_at'})"
    )
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        sys.exit(1)
    date_ranges = batch_date_ranges(start_date, end_date)
    # a limit_teams-scoped run resolves its teams to their tenants
    # up front and fetches ONLY those tenant docs, so no out-of-scope tenant
    # is loaded (or its GraphQL endpoint contacted). exclude_teams runs stay
    # unscoped — a tenant's remaining teams may still be in scope — and keep
    # relying on the per-tenant guard below.
    allowed_tenant_esids = None
    if limit_teams_list:
        allowed_tenant_esids = _tenant_esids_for_teams(es, limit_teams_list)
        if not allowed_tenant_esids:
            # a scoped run that resolves to no tenant imported nothing;
            # exiting rc 0 let the Report Scheduler record it as a SUCCESS (the
            # failure mode that hid six dark teams).
            raise scope_resolution_error(
                scope_param="limit_teams",
                scope_value=limit_teams,
                connector_id="echo",
            )
    tenants = get_tenants(tenant_esids=allowed_tenant_esids)
    if not tenants:
        # on a SCOPED run this is a dangling link: the teams resolved to
        # tenant esids, but no tenant doc exists for them. Unscoped runs keep the
        # old rc-0 return (no scope means nothing was promised).
        if limit_teams_list:
            raise scope_resolution_error(
                scope_param="limit_teams",
                scope_value=limit_teams,
                connector_id="echo",
            )
        logging.info("No tenants found.")
        return

    # Fetch the raw asset docs ONCE; the referrer-match maps are built PER
    # TENANT inside the loop below (a cluster-wide match pool let
    # fuzzy matching attribute tenant A's conversations to tenant B's teams).
    logging.info("Fetching Facebook pages for team matching...")
    all_fb_pages = fetch_all_facebook_pages(es)

    logging.info("Fetching YouTube channels for team matching...")
    all_yt_channels = fetch_all_youtube_channels(es)

    # Initialize match statistics
    match_stats = {
        "total": 0,
        "exact": 0,
        "case_insensitive": 0,
        "url_extracted": 0,
        "no_space": 0,
        "partial": 0,
        "fuzzy": 0,
        "fb_page_no_team": 0,
        "yt_channel_no_team": 0,
        "unmatched": 0,
    }

    for tenant_hit in tenants:
        tenant = tenant_hit["_source"]
        tenant_esid = tenant_hit["_id"]
        echo_tenant_subdomain = tenant.get("echo_tenant_subdomain")
        encrypted_api_key = tenant.get("echo_tenant_api_key")
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
        teams = get_active_teams(
            es,
            tenant_esid,
            echo_tenant_subdomain,
            exclude_teams=exclude_teams_list,
            limit_teams=limit_teams_list,
        )
        # on a FILTERED run (limit_teams / exclude_teams), skip a
        # tenant the filter empties out instead of still calling its GraphQL
        # endpoint. Without this, a filter that excludes every team in a tenant
        # still triggers a full per-date-range fetch whose conversations are all
        # discarded by the per-conversation limit_teams filter below. For a
        # dead/misconfigured tenant subdomain that means pointless 404 retry
        # loops (60/120/240s each); for a live tenant it burns API calls and
        # rate-limit budget.
        #
        # (earlier this guard was scoped to filtered runs because the
        # unfiltered cron path resolved teams via direct ES lookups instead of
        # get_active_teams' list. That path is gone — every run is limit_teams-
        # scoped now — but the run-shape condition is kept as harmless.)
        if (limit_teams_list or exclude_teams_list) and not teams:
            logging.info(
                f"Skipping tenant {tenant_esid} (subdomain {echo_tenant_subdomain}) "
                f"— no active teams match the current filter."
            )
            continue
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        # per-tenant attribution pools. Team resolution dicts come
        # from THIS tenant's team docs, and the referrer-match maps admit only
        # the assets those teams reference — a referrer can no longer match
        # (or fuzzy-match) another tenant's page/channel, and a match can
        # never resolve to another tenant's team.
        fb_page_id_to_team, yt_esid_to_team = get_tenant_team_maps(es, tenant_esid)
        fb_pages_map, page_esid_to_fb_page_id = build_fb_pages_maps(all_fb_pages, set(fb_page_id_to_team))
        yt_channels_map = build_yt_channels_map(all_yt_channels, set(yt_esid_to_team))

        rate_state = {"calls": 0, "sleeps": 0}

        for date_range in date_ranges:
            logging.info(f"Processing date range {date_range}")
            conversations = fetch_conversations_graphql(
                base_url, headers, date_range[0], date_range[1], rate_state, date_field=date_field
            )
            if conversations is None:
                logging.error(f"Skipping date range {date_range} for tenant {tenant_esid} due to GraphQL error.")
                continue

            for entry in conversations:
                process_conversation_entry(
                    entry,
                    es,
                    fb_pages_map,
                    page_esid_to_fb_page_id,
                    yt_channels_map,
                    fb_page_id_to_team,
                    yt_esid_to_team,
                    tenant_esid,
                    base_url,
                    limit_teams_list,
                    match_stats,
                    skip_closed_on_disk=skip_closed_on_disk,
                )

    logging.info("Completed processing conversations.")

    # Log matching statistics summary
    logging.info("=== ECHO CONVERSATION MATCHING STATISTICS ===")
    logging.info(f"Total conversations processed: {match_stats['total']}")
    matched_total = (
        match_stats["exact"]
        + match_stats["case_insensitive"]
        + match_stats["url_extracted"]
        + match_stats["no_space"]
        + match_stats["partial"]
    )
    logging.info(f"Successfully matched: {matched_total}")
    if matched_total > 0:
        logging.info(f"  - Exact matches: {match_stats['exact']}")
        logging.info(f"  - Case-insensitive matches: {match_stats['case_insensitive']}")
        logging.info(f"  - URL-extracted matches: {match_stats['url_extracted']}")
        logging.info(f"  - No-space matches: {match_stats['no_space']}")
        logging.info(f"  - Partial matches: {match_stats['partial']}")
        logging.info(f"  - Fuzzy matches: {match_stats['fuzzy']}")
    logging.info(f"FB page found but no team assigned: {match_stats['fb_page_no_team']}")
    logging.info(f"YouTube channel found but no team assigned: {match_stats['yt_channel_no_team']}")
    logging.info(f"Unmatched (sent to unknown): {match_stats['unmatched']}")
    logging.info("=" * 45)
