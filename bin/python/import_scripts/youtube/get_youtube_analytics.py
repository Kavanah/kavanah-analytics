"""
YouTube Analytics Data Fetcher

Fetches organic YouTube channel metrics via YouTube Analytics API.
Only channels associated with teams (via de_asset_youtube_channels) are processed.

Authentication: OAuth2 with per-account refresh tokens
- Shared client credentials: /app/reporting_configs/youtube-oauth-client.json.enc
- Per-account refresh tokens: stored encrypted in de_asset_youtube_accounts (ES)
- Setup: Create YouTube Account assets in DEAM and authorize via OAuth URL

This script:
1. Groups channels by their linked YouTube Account
2. Loads per-account OAuth2 credentials from Elasticsearch
3. Fetches channel analytics for each account's channels
4. Adds team metadata from de_asset_teams
5. Saves data as JSON files for later Elasticsearch indexing

Usage:
    python3 /app/python/import.py youtube get_youtube_analytics --start_date=YYYY-MM-DD --end_date=YYYY-MM-DD
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone

# Add parent directory to path for lib imports
sys.path.insert(0, "/app/python")
import lib.logging as custom_logging
from lib.es_helpers import build_data_index_name, get_all_documents_paginated, indexed_doc_present
from lib.encryption import decrypt
from lib.google_oauth_client import load_google_oauth_client
from lib.no_data_markers import write_no_data_markers
from lib.cleanup_no_data_markers import delete_stale_markers_for_date
from import_scripts.facebook.get_page_insights import _resolve_team_country

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Config file paths
YOUTUBE_OAUTH_CLIENT_PATH = "/app/reporting_configs/youtube-oauth-client.json.enc"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def get_youtube_accounts(account_esids=None):
    """
    Get YouTube accounts from de_asset_youtube_accounts index.

    the old single-shot ``search_index(..., size=100)`` silently
    dropped every account past the 100th (arbitrary ES order). The fetch is
    now paginated (no cap) and, when ``account_esids`` is given (a team-scoped
    run), scoped at query time so only the accounts the in-scope channels
    reference are loaded.

    Args:
        account_esids: Optional iterable of account ESIDs to scope the fetch
            to. None = every account (cron behavior).

    Returns:
        list: List of account documents with _id and _source
    """
    query = {"match_all": {}}
    if account_esids is not None:
        query = {"ids": {"values": sorted(account_esids)}}
    try:
        accounts = get_all_documents_paginated("de_asset_youtube_accounts", query)
        logging.info(f"Found {len(accounts)} YouTube account(s) in Elasticsearch")
        return accounts
    except Exception as e:
        logging.error(f"Failed to fetch YouTube accounts from Elasticsearch: {e}")
        return []


def load_account_credentials(account_source, account_esid=None):
    """
    Load OAuth2 credentials for a specific YouTube account.

    Reads the per-account refresh token from the account document and the
    shared client_id/client_secret from the encrypted OAuth client JSON.

    Args:
        account_source: The _source dict from a de_asset_youtube_accounts document.
        account_esid: ES doc id of the account, used only for the PR-C
            shadow-mode parity check. Optional — when omitted, the shadow
            check is skipped (the function still returns valid credentials).

    Returns:
        google.oauth2.credentials.Credentials: OAuth2 credentials for this account.

    Raises:
        ValueError: If the account has no token stored (not yet authorized).
    """
    encrypted_token = account_source.get("youtube_account_token")
    if not encrypted_token:
        raise ValueError(
            f"Account '{account_source.get('youtube_account_title', 'Unknown')}' "
            f"has no OAuth token. Use the OAuth URL on the account's detail page to authorize."
        )

    refresh_token = decrypt(encrypted_token)

    # PR-C: shadow-mode parity check. Mirrors the FB-side wiring
    # in import_scripts/facebook/get_insights.get_fb_app_user_token —
    # telemetry only, never blocks the import. Disabled via
    # CONNECTION_SHADOW_DISABLED=1 (same kill-switch as the FB side).
    if account_esid and os.environ.get("CONNECTION_SHADOW_DISABLED") != "1":
        try:
            from asset_management.web.services.connection_service import ConnectionService

            shadow = ConnectionService.shadow_compare_legacy_token(
                legacy_asset_index="de_asset_youtube_accounts",
                legacy_asset_esid=account_esid,
                legacy_plaintext_token=refresh_token,
            )
            status = shadow.get("status")
            if status == "match":
                logging.info(
                    "connection_shadow status=match legacy_asset_esid=%s connection_esid=%s",
                    account_esid,
                    shadow.get("connection_esid"),
                )
            elif status == "missing":
                # 'missing' is the expected state until
                # connection_backfill.py runs on a given env — log at INFO
                # so operator-actionable signals (mismatch / decrypt_error /
                # exception) keep their WARNING level and aren't drowned out.
                logging.info(
                    "connection_shadow status=missing legacy_asset_esid=%s detail=%s",
                    account_esid,
                    shadow,
                )
            else:
                logging.warning(
                    "connection_shadow status=%s legacy_asset_esid=%s detail=%s",
                    status,
                    account_esid,
                    shadow,
                )
        except Exception as e:
            logging.warning(
                "connection_shadow exception legacy_asset_esid=%s err=%s",
                account_esid,
                e,
            )

    # Load shared client credentials
    credentials = load_google_oauth_client()

    creds = Credentials(
        token=None,  # Will be auto-refreshed on first API call
        refresh_token=refresh_token,
        client_id=credentials["client_id"],
        client_secret=credentials["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )

    logging.info(f"✓ OAuth2 credentials loaded for account '{account_source.get('youtube_account_title')}'")
    return creds


def get_youtube_service(credentials):
    """
    Initialize YouTube Analytics API service.

    Args:
        credentials: OAuth2 credentials

    Returns:
        googleapiclient.discovery.Resource: YouTube Analytics API service
    """
    try:
        # Disable cache_discovery to avoid file_cache deprecation warnings
        youtube_analytics = build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)
        logging.info("✓ YouTube Analytics API service initialized")
        return youtube_analytics
    except Exception as e:
        logging.error(f"Failed to initialize YouTube Analytics service: {e}")
        raise


def get_youtube_data_service(credentials):
    """
    Initialize YouTube Data API v3 service (for listing videos, channels, etc.).

    Args:
        credentials: OAuth2 credentials

    Returns:
        googleapiclient.discovery.Resource: YouTube Data API v3 service
    """
    try:
        youtube_data = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        logging.info("✓ YouTube Data API v3 service initialized")
        return youtube_data
    except Exception as e:
        logging.error(f"Failed to initialize YouTube Data API service: {e}")
        raise


def get_youtube_channels(channel_esids=None):
    """
    Get YouTube channels from de_asset_youtube_channels index.

    the old single-shot ``search_index(..., size=100)`` silently
    dropped every channel past the 100th (arbitrary ES order), so a team's
    channel that sorted past the cap was never imported with no error. The
    fetch is now paginated (no cap) and, when ``channel_esids`` is given (a
    team-scoped run), scoped at query time so only the in-scope teams'
    channels are loaded at all.

    Args:
        channel_esids: Optional iterable of channel ESIDs to scope the fetch
            to. None = every channel (cron behavior).

    Returns:
        list: List of channel documents with _id and _source
    """
    query = {"match_all": {}}
    if channel_esids is not None:
        query = {"ids": {"values": sorted(channel_esids)}}
    try:
        channels = get_all_documents_paginated("de_asset_youtube_channels", query)
        logging.info(f"Found {len(channels)} YouTube channels in Elasticsearch")
        return channels

    except Exception as e:
        logging.error(f"Failed to fetch YouTube channels from Elasticsearch: {e}")
        return []


def resolve_team_ids_to_esids(team_ids):
    """resolve a ``--team_ids`` filter to the set of team ESIDs it
    names, collision-safe across workspaces.

    The import-data UI sends the selected team's ESID as ``team_ids`` (the
    option carries ``data-esid``); pre-ESID callers may still send a title.
    Resolution is **ESID-exact first, then EXACT-title** — deliberately NOT
    substring (the FB resolver's substring fallback matches every workspace's
    same-title team;). Exact-title can still match more than one
    team if two workspaces share a title, but the ESID path is what the UI
    actually exercises, so the common case is unambiguous.

    Args:
        team_ids: Comma-separated team ESIDs and/or titles. Falsy → no filter.

    Returns:
        None if ``team_ids`` is falsy → caller must NOT filter (all teams).
        set[str] of team ESIDs otherwise (possibly empty if nothing matched).

    Raises:
        The underlying exception if the ES lookup itself fails. this
        used to be caught and turned into an empty set, which the caller could not
        tell apart from "the requested teams don't exist". A transient ES outage
        therefore ended as "success, nothing to import" (rc 0) and the Report
        Scheduler recorded it as a successful no-op. An infrastructure failure must
        not be laundered into a data-shaped answer.
    """
    if not team_ids:
        return None
    terms = [t.strip() for t in str(team_ids).split(",") if t.strip()]
    if not terms:
        return None

    term_set = set(terms)
    title_terms = {t.lower() for t in terms}
    matched_esids = set()
    try:
        # paginated — the old size=1000 single shot dropped teams
        # past the cap, so a filter naming one of them silently matched nothing.
        for hit in get_all_documents_paginated("de_asset_teams", {"match_all": {}}):
            esid = hit["_id"]
            title = (hit["_source"].get("team_title") or "").strip()
            # ESID-exact (what the UI sends) first; exact-title fallback only.
            if esid in term_set or title.lower() in title_terms:
                matched_esids.add(esid)
    except Exception:
        # log and RE-RAISE. Returning set here made an ES failure
        # indistinguishable from a legitimate zero-match, so the strict-mode guard
        # in main() would misclassify an outage as no_data_source ("no teams
        # linked") and tell the user to fix their configuration.
        logging.error(f"resolve_team_ids_to_esids: ES lookup failed for team_ids={team_ids!r}", exc_info=True)
        raise

    if not matched_esids:
        logging.warning(f"resolve_team_ids_to_esids: no teams matched team_ids={team_ids!r}")
    return matched_esids


def _channel_esids_for_teams(team_esids):
    """resolve in-scope teams to the YouTube channel ESIDs they
    reference, so a team-scoped run (manual UI / scheduled / backfill) fetches
    ONLY those channel docs — instead of loading every channel in the cluster
    and discarding the out-of-scope ones after a per-channel team lookup.

    Reads both linkage conventions off the team docs: the multi-select
    ``team_youtube_channel_handle_esids`` list and the legacy scalar
    ``youtube_channel_esid`` (the same pair get_teams_by_youtube_channel_esid
    matches from the other direction).

    Returns a set of channel ESIDs; empty when the teams reference none.
    """
    if not team_esids:
        return set()
    team_docs = get_all_documents_paginated("de_asset_teams", {"ids": {"values": sorted(team_esids)}})
    channel_esids = set()
    for doc in team_docs:
        src = doc.get("_source", {}) or {}
        for esid in src.get("team_youtube_channel_handle_esids") or []:
            if esid:
                channel_esids.add(esid)
        legacy = src.get("youtube_channel_esid")
        if legacy:
            channel_esids.add(legacy)
    return channel_esids


def _scope_unassigned_channel_teams(teams, team_esid_filter):
    """narrow an unassigned channel's referencing-team list to the
    active ``--team_ids`` scope, mirroring the assigned-channel loop's filter.

    ``team_esid_filter`` is None for an unscoped (cron / all-teams) run — return
    ``teams`` unchanged. Otherwise keep only teams whose ``team_esid`` is in scope,
    so the "channel not linked to a YouTube Account" warning is emitted ONLY for a
    channel a SCOPED team needs — not for unassigned channels owned by other teams
    entirely (the cross-team error leak found backfill on staging).
    """
    if team_esid_filter is None or not teams:
        return teams
    return [tm for tm in teams if tm.get("team_esid") in team_esid_filter]


def get_teams_by_youtube_channel_esid(channel_esid):
    """
    Find ALL teams that reference this YouTube channel ESID.
    Supports both multi-select and legacy single field.

    Args:
        channel_esid: Elasticsearch document ID of the YouTube channel

    Returns:
        list: List of team metadata dicts, empty list if none found
    """
    try:
        teams = []

        # Query 1: Check multi-select field (current standard)
        # Only require the channel ESID, integration status is optional
        # paginated — no 100-teams-per-channel cap.
        results = get_all_documents_paginated(
            "de_asset_teams",
            {
                "bool": {
                    "must": [
                        # bare fields — de_asset_teams is explicit
                        # keyword post-reindex; the auto-mapped .keyword
                        # subfields no longer exist (a term on them silently
                        # matched nothing → every channel skipped as
                        # "Not assigned to any team").
                        {"term": {"team_youtube_channel_handle_esids": channel_esid}},
                        {"term": {"ds_youtube": "true"}},
                    ],
                    "should": [
                        {"term": {"team_youtube_integration_status": "Active"}},
                        {"bool": {"must_not": {"exists": {"field": "team_youtube_integration_status"}}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        )

        for hit in results:
            team = hit["_source"]
            teams.append(
                {
                    "team_esid": hit["_id"],
                    "team_title": team.get("team_title", "Unknown"),
                    "team_url": team.get("team_url", "unknown"),
                    "team_country": _resolve_team_country(team) or "unknown",
                    "team_city": team.get("team_city", "unknown"),
                    "organization_title": team.get("organization_title", "Unknown"),
                }
            )

        # Query 2: Check legacy single field (backward compatibility)
        results_legacy = get_all_documents_paginated(
            "de_asset_teams",
            {
                "bool": {
                    "must": [
                        # bare fields (see Query 1).
                        {"term": {"youtube_channel_esid": channel_esid}},
                        {"term": {"ds_youtube": "true"}},
                    ],
                    "should": [
                        {"term": {"team_youtube_integration_status": "Active"}},
                        {"bool": {"must_not": {"exists": {"field": "team_youtube_integration_status"}}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        )

        for hit in results_legacy:
            team = hit["_source"]
            team_esid = hit["_id"]
            # Avoid duplicates
            if not any(t["team_esid"] == team_esid for t in teams):
                teams.append(
                    {
                        "team_esid": team_esid,
                        "team_title": team.get("team_title", "Unknown"),
                        "team_url": team.get("team_url", "unknown"),
                        "team_country": _resolve_team_country(team) or "unknown",
                        "team_city": team.get("team_city", "unknown"),
                        "organization_title": team.get("organization_title", "Unknown"),
                    }
                )

        return teams

    except Exception as e:
        logging.warning(f"Error looking up teams for YouTube channel {channel_esid}: {e}")
        return []


def fetch_channel_analytics_by_country(youtube_analytics, channel_id, start_date, end_date, channel_title=None):
    """
    Fetch analytics for a specific YouTube channel broken down by country.

    Uses a separate query from fetch_channel_analytics so that country-level documents
    can be stored alongside (not instead of) the channel-level daily totals. This prevents
    inflation of metrics in existing dashboard charts that sum across all documents.

    The YouTube Analytics "User Activity by Country" report does NOT allow the ``day``
    dimension — the API rejects ``day,country`` with "The query is not supported". So this
    query is country-only and returns one row per country aggregated over the whole
    [start_date, end_date] window. Each row is stamped with ``end_date`` so it lands inside
    the queried range for kibana_date filtering, mirroring how video-retention records use a
    single period date.
    See https://developers.google.com/youtube/analytics/channel_reports (User Activity by Country).

    Returns:
        list: List of dicts with one entry per country
    """
    try:
        response = (
            youtube_analytics.reports()
            .query(
                ids=f"channel=={channel_id}",
                startDate=start_date,
                endDate=end_date,
                metrics=(
                    "views," "estimatedMinutesWatched," "subscribersGained," "subscribersLost," "likes," "comments," "shares"
                ),
                dimensions="country",
                sort="country",
            )
            .execute()
        )

        column_headers = response.get("columnHeaders", [])
        rows = response.get("rows", [])
        metrics_map = {header["name"]: idx for idx, header in enumerate(column_headers)}

        analytics_data = []
        for row in rows:
            data_point = {
                "date": end_date,
                "country": row[metrics_map["country"]],
                "views": (int(row[metrics_map["views"]]) if "views" in metrics_map else 0),
                "estimated_minutes_watched": (
                    int(row[metrics_map["estimatedMinutesWatched"]]) if "estimatedMinutesWatched" in metrics_map else 0
                ),
                "subscribers_gained": (
                    int(row[metrics_map["subscribersGained"]]) if "subscribersGained" in metrics_map else 0
                ),
                "subscribers_lost": (int(row[metrics_map["subscribersLost"]]) if "subscribersLost" in metrics_map else 0),
                "likes": (int(row[metrics_map["likes"]]) if "likes" in metrics_map else 0),
                "comments": (int(row[metrics_map["comments"]]) if "comments" in metrics_map else 0),
                "shares": (int(row[metrics_map["shares"]]) if "shares" in metrics_map else 0),
                "record_type": "country_breakdown",
            }
            analytics_data.append(data_point)

        logging.info(
            f"✓ Fetched {len(analytics_data)} country records for channel {channel_title or channel_id} "
            f"({start_date} to {end_date})"
        )
        return analytics_data

    except HttpError as e:
        error_details = json.loads(e.content.decode("utf-8"))
        error_message = error_details.get("error", {}).get("message", str(e))
        logging.error(f"YouTube Analytics API error (country breakdown) for channel {channel_id}:\n  {error_message}")
        return []
    except Exception as e:
        logging.error(f"Failed to fetch country analytics for channel {channel_title or channel_id}: {e}")
        return []


def fetch_channel_analytics(youtube_analytics, channel_id, start_date, end_date, channel_title=None):
    """
    Fetch analytics for a specific YouTube channel.

    Args:
        youtube_analytics: YouTube Analytics API service
        channel_id: YouTube channel ID (format: UCxxxxxxxxxxxxxx)
        start_date: Start date string (YYYY-MM-DD)
        end_date: End date string (YYYY-MM-DD)

    Returns:
        list: List of daily analytics dicts
    """
    try:
        # YouTube Analytics API query
        # Dimensions: day (daily breakdown)
        # Metrics: key channel performance indicators
        response = (
            youtube_analytics.reports()
            .query(
                ids=f"channel=={channel_id}",
                startDate=start_date,
                endDate=end_date,
                metrics=(
                    "views,"
                    "engagedViews,"
                    "estimatedMinutesWatched,"
                    "subscribersGained,"
                    "subscribersLost,"
                    "likes,"
                    "comments,"
                    "shares,"
                    "videosAddedToPlaylists,"
                    "videosRemovedFromPlaylists,"
                    "averageViewDuration"
                ),
                dimensions="day",
                sort="day",
            )
            .execute()
        )

        # Parse API response
        column_headers = response.get("columnHeaders", [])
        rows = response.get("rows", [])

        # Map column names
        metrics_map = {header["name"]: idx for idx, header in enumerate(column_headers)}

        analytics_data = []

        for row in rows:
            data_point = {
                # Date
                "date": row[metrics_map.get("day", 0)],
                # View metrics
                "views": (int(row[metrics_map.get("views", 0)]) if "views" in metrics_map else 0),
                "engaged_views": (int(row[metrics_map.get("engagedViews", 0)]) if "engagedViews" in metrics_map else 0),
                "estimated_minutes_watched": (
                    int(row[metrics_map.get("estimatedMinutesWatched", 0)]) if "estimatedMinutesWatched" in metrics_map else 0
                ),
                "average_view_duration": (
                    float(row[metrics_map.get("averageViewDuration", 0)]) if "averageViewDuration" in metrics_map else 0.0
                ),
                # Subscriber metrics
                "subscribers_gained": (
                    int(row[metrics_map.get("subscribersGained", 0)]) if "subscribersGained" in metrics_map else 0
                ),
                "subscribers_lost": (
                    int(row[metrics_map.get("subscribersLost", 0)]) if "subscribersLost" in metrics_map else 0
                ),
                # Engagement metrics
                "likes": (int(row[metrics_map.get("likes", 0)]) if "likes" in metrics_map else 0),
                "comments": (int(row[metrics_map.get("comments", 0)]) if "comments" in metrics_map else 0),
                "shares": (int(row[metrics_map.get("shares", 0)]) if "shares" in metrics_map else 0),
                # Playlist metrics
                "videos_added_to_playlists": (
                    int(row[metrics_map.get("videosAddedToPlaylists", 0)]) if "videosAddedToPlaylists" in metrics_map else 0
                ),
                "videos_removed_from_playlists": (
                    int(row[metrics_map.get("videosRemovedFromPlaylists", 0)])
                    if "videosRemovedFromPlaylists" in metrics_map
                    else 0
                ),
            }

            analytics_data.append(data_point)

        logging.info(
            f"✓ Fetched {len(analytics_data)} daily records for channel {channel_title or channel_id} "
            f"({start_date} to {end_date})"
        )
        return analytics_data

    except HttpError as e:
        error_details = json.loads(e.content.decode("utf-8"))
        error_message = error_details.get("error", {}).get("message", str(e))
        # extract the REAL API reason instead of hardcoding
        # "insufficientPermissions" into the recorded string. A bare reason
        # "forbidden" means the AUTHORIZING GOOGLE ACCOUNT lost analytics
        # access on the channel (a YouTube-side permissions change) — the
        # token is healthy and there is nothing to grant to the app, so the
        # permission_denied "grant the required permissions" copy misled
        # diagnosis (prod 2026-06-30: ~12 channels under one account).
        try:
            reason = str(((error_details.get("error", {}).get("errors") or [{}])[0]).get("reason") or "")
        except Exception:  # noqa: BLE001 — defensive: error body shape is Google's
            reason = ""

        if "insufficientPermissions" in str(e) or "forbidden" in str(e).lower():
            from lib.import_run_context import record_user_error

            if "insufficientPermissions" in str(e) or reason == "insufficientPermissions":
                # Genuine scope 403 — reconnecting and granting the app's
                # requested permissions actually helps (classifier:
                # access denied → permission_denied).
                logging.error(
                    f"Permission denied for channel {channel_id}:\n"
                    f"  {error_message}\n"
                    f"  → The OAuth2 grant lacks the required scopes/permissions\n"
                    f"  → Reconnect this account and grant the requested permissions"
                )
                record_user_error(
                    f"YouTube channel access denied ({reason or 'insufficientPermissions'}): {error_message}",
                    connector_id="youtube",
                    detail=(channel_title or channel_id),
                )
            else:
                # Channel-access 403 (reason "forbidden") — the authorizing
                # identity can no longer read this channel's analytics. The
                # fix lives in YouTube Studio (a channel owner restores
                # access) or in reconnecting AS an authorized identity —
                # NOT in granting app permissions. Distinct raw token
                # ("channel analytics forbidden") classifies to
                # youtube_channel_access_denied.
                logging.error(
                    f"Channel access lost for channel {channel_id} (reason={reason or 'forbidden'}):\n"
                    f"  {error_message}\n"
                    f"  → The authorizing Google account can no longer read this channel's analytics\n"
                    f"  → A channel owner must grant it access in YouTube Studio (Settings → Permissions),\n"
                    f"    or reconnect signed in as a Google account that manages this channel"
                )
                record_user_error(
                    f"YouTube channel analytics forbidden (reason={reason or 'forbidden'}): {error_message}",
                    connector_id="youtube",
                    detail=(channel_title or channel_id),
                )
        else:
            logging.error(f"YouTube Analytics API error for channel {channel_id}:\n  {error_message}")

        return []

    except Exception as e:
        logging.error(f"Failed to fetch analytics for channel {channel_title or channel_id}: {e}")
        return []


def get_channel_videos(youtube_data, channel_id, max_results=50):
    """
    List videos for a YouTube channel via Data API v3.

    Fetches the channel's uploads playlist, then lists video IDs and metadata.

    Args:
        youtube_data: YouTube Data API v3 service
        channel_id: YouTube channel ID (format: UCxxxxxxxxxxxxxx)
        max_results: Maximum number of videos to return (default 50)

    Returns:
        list: List of dicts with video_id, video_title, video_duration_seconds
    """
    try:
        # Get the uploads playlist ID from the channel
        channel_response = youtube_data.channels().list(part="contentDetails", id=channel_id).execute()

        items = channel_response.get("items", [])
        if not items:
            logging.warning(f"No channel found for ID {channel_id}")
            return []

        uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        # List videos from the uploads playlist
        video_ids = []
        next_page_token = None

        while len(video_ids) < max_results:
            playlist_response = (
                youtube_data.playlistItems()
                .list(
                    part="contentDetails",
                    playlistId=uploads_playlist_id,
                    maxResults=min(50, max_results - len(video_ids)),
                    pageToken=next_page_token,
                )
                .execute()
            )

            for item in playlist_response.get("items", []):
                video_ids.append(item["contentDetails"]["videoId"])

            next_page_token = playlist_response.get("nextPageToken")
            if not next_page_token:
                break

        if not video_ids:
            logging.info(f"No videos found for channel {channel_id}")
            return []

        # Get video details (title, duration) in batches of 50
        videos = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i : i + 50]
            video_response = youtube_data.videos().list(part="snippet,contentDetails", id=",".join(batch)).execute()

            for item in video_response.get("items", []):
                duration_iso = item["contentDetails"]["duration"]
                duration_seconds = _parse_iso8601_duration(duration_iso)
                videos.append(
                    {
                        "video_id": item["id"],
                        "video_title": item["snippet"]["title"],
                        "video_duration_seconds": duration_seconds,
                    }
                )

        logging.info(f"✓ Found {len(videos)} videos for channel {channel_id}")
        return videos

    except HttpError as e:
        error_details = json.loads(e.content.decode("utf-8"))
        error_message = error_details.get("error", {}).get("message", str(e))
        logging.error(f"YouTube Data API error listing videos for channel {channel_id}: {error_message}")
        return []
    except Exception as e:
        logging.error(f"Failed to list videos for channel {channel_id}: {e}")
        return []


def _parse_iso8601_duration(duration_str):
    """
    Parse ISO 8601 duration (e.g., 'PT1H2M30S') to total seconds.

    Args:
        duration_str: ISO 8601 duration string from YouTube API

    Returns:
        int: Total seconds
    """
    import re

    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_str)
    if not match:
        return 0

    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def fetch_video_retention(youtube_analytics, channel_id, video_id, start_date, end_date):
    """
    Fetch audience retention data for a single video.

    The audienceRetention report returns audienceWatchRatio at each
    elapsedVideoTimeRatio point (0.0 to 1.0 in ~0.01 increments).

    Args:
        youtube_analytics: YouTube Analytics API service
        channel_id: YouTube channel ID
        video_id: YouTube video ID
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)

    Returns:
        dict: {
            "retention_at_25_pct": float,
            "retention_at_50_pct": float,
            "retention_at_75_pct": float,
            "retention_at_95_pct": float,
            "retention_at_100_pct": float,
        } or None on error
    """
    try:
        response = (
            youtube_analytics.reports()
            .query(
                ids=f"channel=={channel_id}",
                startDate=start_date,
                endDate=end_date,
                metrics="audienceWatchRatio",
                dimensions="elapsedVideoTimeRatio",
                filters=f"video=={video_id}",
                sort="elapsedVideoTimeRatio",
            )
            .execute()
        )

        rows = response.get("rows", [])
        if not rows:
            logging.debug(f"No retention data for video {video_id}")
            return None

        # Build a lookup of elapsed ratio → watch ratio
        retention_map = {}
        for row in rows:
            elapsed_ratio = row[0]  # 0.0 to 1.0
            watch_ratio = row[1]  # fraction of viewers still watching
            retention_map[round(elapsed_ratio, 2)] = watch_ratio

        # Extract retention at key thresholds
        # Find closest match for each threshold (API may not return exact values)
        thresholds = {
            "retention_at_25_pct": 0.25,
            "retention_at_50_pct": 0.50,
            "retention_at_75_pct": 0.75,
            "retention_at_95_pct": 0.95,
            "retention_at_100_pct": 1.00,
        }

        result = {}
        sorted_ratios = sorted(retention_map.keys())

        for field_name, target in thresholds.items():
            # Find the closest ratio to the target
            closest = min(sorted_ratios, key=lambda x: abs(x - target))
            result[field_name] = round(retention_map[closest], 4)

        return result

    except HttpError as e:
        error_details = json.loads(e.content.decode("utf-8"))
        error_message = error_details.get("error", {}).get("message", str(e))
        logging.warning(f"Retention API error for video {video_id}: {error_message}")
        return None
    except Exception as e:
        logging.warning(f"Failed to fetch retention for video {video_id}: {e}")
        return None


def fetch_channel_video_retention(youtube_analytics, youtube_data, channel_id, start_date, end_date, channel_title=None):
    """
    Fetch audience retention at key thresholds for all videos in a channel.

    Combines get_channel_videos() and fetch_video_retention() to produce
    one record per video with retention percentages.

    Args:
        youtube_analytics: YouTube Analytics API service
        youtube_data: YouTube Data API v3 service
        channel_id: YouTube channel ID
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        channel_title: Optional channel title for logging

    Returns:
        list: List of dicts with video metadata and retention data
    """
    display_name = channel_title or channel_id

    videos = get_channel_videos(youtube_data, channel_id)
    if not videos:
        logging.info(f"  No videos to fetch retention for channel {display_name}")
        return []

    retention_records = []
    for video in videos:
        retention = fetch_video_retention(youtube_analytics, channel_id, video["video_id"], start_date, end_date)
        if retention is None:
            continue

        record = {
            "video_id": video["video_id"],
            "video_title": video["video_title"],
            "video_duration_seconds": video["video_duration_seconds"],
            "record_type": "video_retention",
            **retention,
        }
        retention_records.append(record)

    logging.info(f"  ✓ Fetched retention data for {len(retention_records)}/{len(videos)} videos " f"on channel {display_name}")
    return retention_records


def save_analytics_data(analytics_data, channel_esid, team_esid, date_str):
    """
    Save channel analytics data to JSON file.

    Args:
        analytics_data: Dict of channel analytics metrics
        channel_esid: YouTube channel Elasticsearch ID
        team_esid: Team Elasticsearch ID
        date_str: Date string (YYYY-MM-DD)
    """
    # Generate unique filename using channel, date, and country (if present)
    country = analytics_data.get("country")
    filename = f"{date_str}_{country}_{channel_esid}_metrics.json" if country else f"{date_str}_{channel_esid}_metrics.json"

    # Directory structure: /app/data/youtube_analytics/channel_metrics/{channel_esid}/{team_esid}/
    dir_path = f"/app/data/youtube_analytics/channel_metrics/{channel_esid}/{team_esid}"
    os.makedirs(dir_path, exist_ok=True)

    file_path = os.path.join(dir_path, filename)

    # Check if file already exists in _imported. the archive is NOT
    # proof the doc is in ES (send→archive isn't gated on send success), so skip
    # only when the file is archived AND ES actually holds the doc; otherwise
    # re-write so the next send recovers it. The sender's _id is an MD5 built at
    # send time, so verify by field-match (channel+date[+country]) rather than
    # reconstructing the hash. ES check fires only on the archived branch.
    imported_path = file_path.replace("/channel_metrics/", "/channel_metrics_imported/")
    if os.path.exists(imported_path):
        es_index = build_data_index_name(team_esid, "youtube_analytics")
        # date_str is a filename disambiguator, not always a date: retention
        # records pass "retention_<video_id>" here, which ES rejects against the
        # yyyy-MM-dd date mapping. Match on the record's own date field, and pin
        # retention docs by video_id so a same-day channel-metrics doc can't
        # satisfy the match and cause a spurious skip.
        match_fields = {
            "youtube_channel_id": analytics_data.get("youtube_channel_id"),
            "date": analytics_data.get("date"),
        }
        if country:
            match_fields["country"] = country
        if analytics_data.get("record_type") == "video_retention":
            match_fields["video_id"] = analytics_data.get("video_id")
        if indexed_doc_present(es_index, match_fields=match_fields):
            logging.debug(f"Skipping {filename} - already imported and indexed")
            return
        logging.warning(
            "recovery: %s is archived but no ES doc matches %s in %s " "— re-writing for re-send.",
            filename,
            match_fields,
            es_index,
        )

    # Write JSON file
    with open(file_path, "w") as f:
        json.dump(analytics_data, f, indent=2, sort_keys=True)

    logging.debug(f"Saved: {file_path}")


def main(params):
    """
    Main entry point from import.py.

    Args:
        params: Dict with command-line parameters:
                - start_date: Start date (YYYY-MM-DD), defaults to 30 days ago
                - end_date: End date (YYYY-MM-DD), defaults to yesterday
                - channel_ids: Optional comma-separated list of channel IDs to process
                - team_ids: Optional comma-separated team ESIDs (or titles) to scope
                  the import to. Falsy → all teams (cron behavior)..
    """
    custom_logging.define("youtube-analytics-fetch")

    # bind this run's progress doc so a per-channel permission failure
    # (403 insufficientPermissions) surfaces in the scheduler's "Errors" button.
    from lib.import_run_context import set_run_id

    set_run_id(params.get("progress_run_id"))

    # require an explicit team scope BEFORE any lookup. Cron is retired
    # (deployment/03_provision_cron_jobs.sh migrated the importer jobs to the Report
    # Scheduler), the manual Import Data Page always sends the selected
    # team, and backfill reuses the scheduler dialog — so no legitimate caller omits
    # --team_ids. An absent flag must fail loudly rather than fall through to a
    # cluster-wide scan (the cross-tenant exposure class behind).
    # youtube/run_daily_imports.py:126-128 already guards its own entry this way;
    # this is the direct-dispatch half (import.py / import_service.py call this
    # module straight, bypassing the wrapper).
    from lib.import_scope import require_scope, scope_resolution_error

    team_ids_param = require_scope(params, scope_param="team_ids", connector_id="youtube")

    # resolve the team filter to a set of team ESIDs. Never None now that
    # the flag is required; an empty set means the named teams matched nothing.
    team_esid_filter = resolve_team_ids_to_esids(team_ids_param)

    if not team_esid_filter:
        # scope provided but no team matched. Pre-fix, an empty-but-not-
        # None set flowed onward and was only caught two steps later by the channel
        # check, which warned and returned rc 0.
        raise scope_resolution_error(
            scope_param="team_ids",
            scope_value=team_ids_param,
            connector_id="youtube",
        )

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

    logging.info(f"Fetching YouTube Analytics channel data: {start_date} to {end_date}")

    # when a team filter is active, scope the channel FETCH to the
    # channels those teams reference (early scoping), instead of loading every
    # channel in the cluster and discarding out-of-scope ones after building
    # credentials / API clients for their accounts. The per-channel team
    # filter further below is kept as the exact-match safety net.
    scoped_channel_esids = _channel_esids_for_teams(team_esid_filter)
    logging.info(
        "early scope: %d team(s) reference %d channel(s).",
        len(team_esid_filter),
        len(scoped_channel_esids),
    )
    if not scoped_channel_esids:
        # the named teams exist but reference no YouTube channel. Pre-fix
        # this warned and returned rc 0, so the Report Scheduler recorded a SUCCESS
        # for a run that imported nothing — the failure mode that hid six dark teams
        # . This is the youtube analogue of the "flag on,
        # no assets" state: the team is configured for YouTube but has no channel
        # linked, which is a misconfiguration the user must see.
        raise scope_resolution_error(
            scope_param="team_ids",
            scope_value=team_ids_param,
            connector_id="youtube",
        )

    # Get YouTube channels from Elasticsearch
    channel_docs = get_youtube_channels(channel_esids=scoped_channel_esids)

    if not channel_docs:
        # the team referenced channel ESIDs but none resolved to a doc in
        # de_asset_youtube_channels: a dangling link, not an empty configuration.
        # Same class as the FB-side dangling case; must not exit rc 0.
        raise scope_resolution_error(
            scope_param="team_ids",
            scope_value=team_ids_param,
            connector_id="youtube",
        )

    # Filter by channel_ids parameter if provided
    channel_ids_filter = params.get("channel_ids")
    if channel_ids_filter:
        filter_list = [cid.strip() for cid in channel_ids_filter.split(",")]
        channel_docs = [doc for doc in channel_docs if doc["_source"].get("youtube_channel_id") in filter_list]
        logging.info(f"Filtered to {len(channel_docs)} channels: {filter_list}")

    # Load YouTube accounts and build a lookup map. On a team-scoped run only
    # the accounts the in-scope channels reference are fetched —
    # so no credential build / shadow check ever runs for a foreign account.
    if team_esid_filter is not None:
        referenced_account_esids = {
            doc["_source"].get("youtube_account_esid") for doc in channel_docs if doc["_source"].get("youtube_account_esid")
        }
        account_docs = get_youtube_accounts(account_esids=referenced_account_esids) if referenced_account_esids else []
    else:
        account_docs = get_youtube_accounts()
    accounts_map = {doc["_id"]: doc["_source"] for doc in account_docs}

    # Partition channels into per-account groups vs unassigned
    account_channels = {}  # account_esid → [channel_doc, …]
    unassigned_channels = []
    for ch in channel_docs:
        account_esid = ch["_source"].get("youtube_account_esid")
        if not account_esid:
            unassigned_channels.append(ch)
        else:
            account_channels.setdefault(account_esid, []).append(ch)

    # Warn on unassigned channels — but only if a team with ds_youtube enabled references them
    custom_logging.define("youtube-analytics-fetch")
    # imported once for the unassigned-channel sweep AND the
    # per-channel loop below.
    from lib.import_run_context import set_current_team

    for ch in unassigned_channels:
        channel_esid = ch["_id"]
        handle = ch["_source"].get("youtube_channel_handle", "Unknown")
        teams = get_teams_by_youtube_channel_esid(channel_esid)
        # scope guard (was missing here): when a team filter is active,
        # only warn about an unassigned channel if one of the teams referencing it
        # is in scope. Without this, a team-scoped run (manual / scheduled /
        # backfill) surfaced "no data source linked" for unassigned channels owned
        # by OTHER teams entirely — the assigned-channel loop below already scopes
        # this way (line ~1014); the warning loop never got the same guard.
        teams = _scope_unassigned_channel_teams(teams, team_esid_filter)
        # attribute the not-linked warning when exactly ONE in-scope
        # team references the channel; multiple claimants stay unattributed.
        set_current_team(teams[0].get("team_esid") if len(teams) == 1 else None)
        if teams:
            # A team with ds_youtube enabled needs this channel — warn loudly
            logging.error(
                f"Error: Skipping channel {handle}: "
                f"not linked to a YouTube Account. "
                f"Edit the channel in Asset Management and select its owner account."
            )
            # surface this user-actionable config skip to the Report
            # Scheduler's "Errors" button (the 403 site is wired; this "not linked
            # to a YouTube Account" site was not). The raw carries "not linked to a"
            # so it classifies to no_data_source ("check the team's Data
            # Connectors"); detail=handle dedups per channel. No-op on cron.
            from lib.import_run_context import record_user_error

            record_user_error(
                f"YouTube channel not linked to a YouTube Account: {handle}",
                connector_id="youtube",
                detail=handle,
            )
        else:
            logging.debug(f"Skipping unassigned channel {handle}: no active YouTube team references it")

    total_records = 0
    channels_with_teams = 0
    channels_without_teams = 0
    teams_with_data_by_date = {}  # {date_str: set(team_esid)}
    all_team_list = []  # [(team_esid, team_title)]

    # Process each account group
    for account_esid, channels in account_channels.items():
        custom_logging.define("youtube-analytics-fetch")
        account_source = accounts_map.get(account_esid)
        if not account_source:
            logging.error(f"Account ESID {account_esid} not found in Elasticsearch. " f"Skipping {len(channels)} channel(s).")
            continue

        account_title = account_source.get("youtube_account_title", "Unknown")
        logging.info(f"\n{'='*60}")
        logging.info(f"Account: {account_title}")
        logging.info(f"Channels in this account: {len(channels)}")
        logging.info(f"{'='*60}")

        # pause imports when the user has disconnected this account
        # via the Manage Connection page. The YouTube importer reads the legacy
        # youtube_account_token directly and was never wired into the
        # revoke short-circuit that FB/IG/Echo/GAds/GA got. We resolve the
        # account's de_connection and skip only when a connection row exists
        # AND it is revoked — pure-legacy accounts (never migrated) fail open.
        try:
            from asset_management.web.services.connection_service import ConnectionService

            if ConnectionService.revoked_connection_exists_for_legacy_asset(
                legacy_asset_index="de_asset_youtube_accounts",
                legacy_asset_esid=account_esid,
            ):
                logging.info(
                    "revoke: skipping account '%s' (account_esid=%s) — " "its connection has been disconnected.",
                    account_title,
                    account_esid,
                )
                continue
        except Exception as e:
            # Fail-open: a resolver error must not block legitimate imports.
            logging.warning("revoke check failed for account_esid=%s: %s", account_esid, e)

        # Load credentials for this account
        try:
            credentials = load_account_credentials(account_source, account_esid=account_esid)
        except ValueError as e:
            logging.error(f"Skipping account '{account_title}': {e}")
            continue

        # Initialize YouTube Analytics API service for this account
        try:
            youtube_analytics = get_youtube_service(credentials)
        except Exception as e:
            logging.error(f"Failed to initialize YouTube Analytics service for '{account_title}': {e}")
            continue

        # Initialize YouTube Data API v3 service (for listing videos)
        try:
            youtube_data = get_youtube_data_service(credentials)
        except Exception as e:
            logging.error(f"Failed to initialize YouTube Data API service for '{account_title}': {e}")
            youtube_data = None  # Non-fatal — retention fetch will be skipped

        # Process each channel in this account
        for channel_doc in channels:
            channel = channel_doc["_source"]
            channel_esid = channel_doc["_id"]
            channel_id = channel.get("youtube_channel_id")
            channel_title = channel.get("youtube_channel_title", "Unknown")
            channel_handle = channel.get("youtube_channel_handle", "Unknown")

            if not channel_id:
                logging.warning(f"Skipping channel {channel_esid} - missing channel_id")
                continue

            logging.info(f"\n  Processing: {channel_handle}")
            logging.info(f"  Channel ID: {channel_title}")

            # Look up teams for this channel
            team_metadata_list = get_teams_by_youtube_channel_esid(channel_esid)

            # when a team filter is active (manual UI run with a team
            # selected), scope the per-channel team list to the requested ESIDs.
            # ESID-exact (see resolve_team_ids_to_esids) so same-title teams in
            # other workspaces are not swept in. None → no filter (cron / all).
            if team_esid_filter is not None and team_metadata_list:
                _before_team = len(team_metadata_list)
                team_metadata_list = [tm for tm in team_metadata_list if tm.get("team_esid") in team_esid_filter]
                if len(team_metadata_list) != _before_team:
                    logging.info(
                        "team filter: channel %s narrowed from %d to %d team(s).",
                        channel_handle,
                        _before_team,
                        len(team_metadata_list),
                    )

            # billing pre-check per (channel, team). Drop a
            # team whose owner is under an inactive subscription (Condition A) OR
            # for which this channel is over the paid connection cap (Condition B —
            # matched by the channel id / esid against the Current Plan badge
            # partition). A channel can map to multiple teams; only the blocked
            # pairs are dropped, each surfaced as a classified error on the
            # scheduled path (no-op on cron). Fail-open per team.
            if team_metadata_list:
                from lib.billing_entitlement import record_billing_skip, source_billing_block

                _kept = []
                for tm in team_metadata_list:
                    _block = source_billing_block(
                        team_esid=tm.get("team_esid"),
                        asset_esid=channel_esid,
                        provider_id=channel_id,
                    )
                    if _block:
                        logging.info(
                            "billing pre-check: skipping channel %s for team %s (reason=%s).",
                            channel_handle,
                            tm.get("team_esid"),
                            _block,
                        )
                        record_billing_skip(_block, connector_id="youtube", detail=(channel_title or channel_handle))
                        continue
                    _kept.append(tm)
                team_metadata_list = _kept

            if not team_metadata_list:
                logging.warning(f"  ⚠ Skipping channel {channel_handle} ({channel_id}): " f"Not assigned to any team")
                channels_without_teams += 1
                continue

            channels_with_teams += 1
            # bind the channel's team when it serves exactly ONE
            # (the common case); a multi-team channel's errors stay
            # unattributed rather than guessing. Overwrites the previous
            # channel's binding every iteration.
            set_current_team(team_metadata_list[0].get("team_esid") if len(team_metadata_list) == 1 else None)

            logging.info(f"  ✓ Channel assigned to {len(team_metadata_list)} team(s):")
            for tm in team_metadata_list:
                logging.info(f"    - {tm['team_title']} (Org: {tm['organization_title']})")
                all_team_list.append((tm["team_esid"], tm["team_title"]))

            # Fetch channel analytics
            analytics = fetch_channel_analytics(youtube_analytics, channel_id, start_date, end_date, channel_title)

            if not analytics:
                logging.warning(f"  ⚠ No data returned for channel {channel_handle}")
                continue

            # Save data for each team
            for team_metadata in team_metadata_list:
                for daily_data in analytics:
                    team_daily_data = daily_data.copy()

                    team_daily_data.update(team_metadata)
                    team_daily_data["youtube_channel_id"] = channel_id
                    team_daily_data["youtube_channel_esid"] = channel_esid
                    team_daily_data["youtube_channel_title"] = channel_title
                    team_daily_data["youtube_channel_handle"] = channel.get("youtube_channel_handle", "")
                    team_daily_data["@timestamp"] = datetime.now(timezone.utc).isoformat()

                    save_analytics_data(
                        team_daily_data,
                        channel_esid,
                        team_metadata["team_esid"],
                        team_daily_data["date"],
                    )
                    teams_with_data_by_date.setdefault(team_daily_data["date"], set()).add(team_metadata["team_esid"])

                total_records += len(analytics)
                logging.info(f"    ✓ Saved {len(analytics)} records for team {team_metadata['team_title']}")

            # Fetch and save country breakdown (separate docs, won't inflate channel totals)
            country_analytics = fetch_channel_analytics_by_country(
                youtube_analytics, channel_id, start_date, end_date, channel_title
            )
            for team_metadata in team_metadata_list:
                for row in country_analytics:
                    team_row = row.copy()
                    team_row.update(team_metadata)
                    team_row["youtube_channel_id"] = channel_id
                    team_row["youtube_channel_esid"] = channel_esid
                    team_row["youtube_channel_title"] = channel_title
                    team_row["youtube_channel_handle"] = channel.get("youtube_channel_handle", "")
                    team_row["@timestamp"] = datetime.now(timezone.utc).isoformat()
                    save_analytics_data(
                        team_row,
                        channel_esid,
                        team_metadata["team_esid"],
                        team_row["date"],
                    )
            if country_analytics:
                logging.info(f"  ✓ Saved {len(country_analytics)} country-breakdown records")

            # Fetch and save video retention data
            if youtube_data:
                retention_records = fetch_channel_video_retention(
                    youtube_analytics, youtube_data, channel_id, start_date, end_date, channel_title
                )
                for team_metadata in team_metadata_list:
                    for record in retention_records:
                        team_record = record.copy()
                        team_record.update(team_metadata)
                        team_record["youtube_channel_id"] = channel_id
                        team_record["youtube_channel_esid"] = channel_esid
                        team_record["youtube_channel_title"] = channel_title
                        team_record["youtube_channel_handle"] = channel.get("youtube_channel_handle", "")
                        team_record["@timestamp"] = datetime.now(timezone.utc).isoformat()
                        # Use start_date as the date for retention records (query period start)
                        team_record["date"] = start_date
                        save_analytics_data(
                            team_record,
                            channel_esid,
                            team_metadata["team_esid"],
                            f"retention_{record['video_id']}",
                        )
                if retention_records:
                    logging.info(f"  ✓ Saved {len(retention_records)} video retention records")

    # Re-set prefix for summary
    custom_logging.define("youtube-analytics-fetch")
    logging.info(f"\n{'='*60}")
    logging.info(f"YouTube Analytics Import Summary")
    logging.info(f"{'='*60}")
    logging.info(f"Date range: {start_date} to {end_date}")
    logging.info(f"Accounts processed: {len(account_channels)}")
    logging.info(f"Channels unassigned (skipped): {len(unassigned_channels)}")
    logging.info(f"Channels with teams: {channels_with_teams}")
    logging.info(f"Channels without teams: {channels_without_teams}")
    logging.info(f"Total records saved: {total_records}")
    logging.info(f"{'='*60}")

    # Write no_data markers for each date in the range
    if all_team_list:
        from import_scripts.facebook.run_daily_imports import generate_date_range

        for d in generate_date_range(start_date, end_date):
            teams_on_date = teams_with_data_by_date.get(d, set())
            # clean up stale markers before writing fresh ones.
            delete_stale_markers_for_date("*_youtube_analytics", d, teams_on_date)
            write_no_data_markers(d, teams_on_date, "_youtube_analytics", team_list=all_team_list)


if __name__ == "__main__":
    # For standalone testing
    import argparse

    parser = argparse.ArgumentParser(description="Fetch YouTube Analytics channel data")
    parser.add_argument("--start_date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end_date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--channel_ids", help="Comma-separated channel IDs")
    parser.add_argument("--team_ids", help="Comma-separated team ESIDs (or titles) to scope the import")

    args = parser.parse_args()

    main(
        {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "channel_ids": args.channel_ids,
            "team_ids": args.team_ids,
        }
    )
