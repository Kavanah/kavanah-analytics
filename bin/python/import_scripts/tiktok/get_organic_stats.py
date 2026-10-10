"""fetch step: snapshot every in-scope TikTok account.

    python3 /app/python/import.py tiktok get_organic_stats --team_ids=a,b --run_name=<name>

For each TikTok connection the in-scope teams link to:

1. take the connection's refresh lock (waiting briefly, not aborting — TikTok
   may rotate the refresh token, so two runs refreshing the same connection at
   once could leave the stored token dead);
2. refresh the access token and store whatever refresh token comes back;
3. read the account's counts and every video's counts;
4. build ``account_daily`` + ``video_daily`` docs per team, with gains measured
   against that team's previous snapshot;
5. write them to the run's staging directory for the index step.

A per-account failure is recorded (Report Scheduler Errors button) and the run
moves on. The run fails when nothing at all could be imported.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import lib.logging as custom_logging
from lib.tiktok_client import TikTokError, get_user_info, list_videos, refresh_access_token, refresh_expiry_iso
from lib.tiktok_constants import RECORD_ACCOUNT_DAILY, RECORD_VIDEO_DAILY, SOURCE_SLUG

CONNECTOR_ID = "tiktok"
LOCK_CONNECTOR = "tiktok"

# Refresh-lock wait: a peer run holds it for seconds (one refresh + a few pages).
LOCK_ATTEMPTS = 12
LOCK_WAIT_SECONDS = 5

# 250 pages x 20 = 5,000 videos per account before the walk stops and says so.
MAX_VIDEO_PAGES = 250

USER_FIELDS_BASE = ["open_id", "display_name", "follower_count", "following_count", "likes_count", "video_count"]
VIDEO_FIELDS = [
    "id",
    "create_time",
    "title",
    "video_description",
    "duration",
    "share_url",
    "view_count",
    "like_count",
    "comment_count",
    "share_count",
]


class TargetFailed(Exception):
    """One account could not be imported; the message is user-facing."""


def _record(message: str, detail: str) -> None:
    from lib.import_run_context import record_user_error

    record_user_error(message, connector_id=CONNECTOR_ID, detail=detail)


def _record_health(target, failure: str | None) -> None:
    """Stamp the connection's credential-health row after an import attempt.

    ``failure`` None = imported (clears any banner); otherwise the TargetFailed
    message, classified the same way the Report Scheduler classifies it. Only
    token / permission categories change health — a rate limit or a lock wait
    does not say anything about the credential. Never raises: health
    bookkeeping must not fail an import.
    """
    try:
        from lib import credential_health as ch

        common = {
            "source_index": "de_connection",
            "source_esid": target.connection_esid,
            "provider": CONNECTOR_ID,
            "label": target.label,
            "workspace_esid": target.workspace_esid,
            "principal": target.principal,
        }
        if failure is None:
            ch.record_healthy(**common)
            return
        from lib.import_error_classifier import classify

        category = classify(failure, connector_id=CONNECTOR_ID).get("category") or ""
        ch.record_classified_error(category=category, detail=failure[:300], **common)
    except Exception as e:  # noqa: BLE001 — see docstring
        logging.warning("tiktok: credential-health write failed for %s: %s", target.connection_esid, e)


def _acquire_refresh_lock(connection_esid: str) -> str:
    from lib.import_lock_service import ImportLockService

    for attempt in range(LOCK_ATTEMPTS):
        lock_id = ImportLockService.acquire(LOCK_CONNECTOR, scope_id=connection_esid)
        if lock_id:
            return lock_id
        if attempt < LOCK_ATTEMPTS - 1:
            time.sleep(LOCK_WAIT_SECONDS)
    raise TargetFailed(
        f"another TikTok import is still using connection {connection_esid}; this account was skipped — run it again"
    )


def _release_refresh_lock(connection_esid: str, lock_id: str) -> None:
    try:
        from lib.import_lock_service import ImportLockService

        ImportLockService.release(LOCK_CONNECTOR, lock_id, scope_id=connection_esid)
    except Exception as e:  # noqa: BLE001 — the TTL / dead-holder check frees it; never fail the run here
        logging.warning("tiktok: refresh-lock release failed for %s: %s", connection_esid, e)


def _refresh(target) -> str:
    """Return a fresh access token, storing the (possibly rotated) refresh token.

    Caller holds the connection's refresh lock.
    """
    from asset_management.web.services.connection_service import STATUS_EXPIRED, ConnectionService

    refresh_token = ConnectionService.get_decrypted_token(target.connection_esid)
    if not refresh_token:
        raise TargetFailed(f"no oauth token on TikTok connection {target.label} — authorize it again")
    try:
        body = refresh_access_token(refresh_token)
    except TikTokError as e:
        text = str(e)
        if "invalid_grant" in text:
            # The refresh token is dead (revoked in TikTok, or past 365 days).
            # Flip the row so the UI asks for a reconnect and later runs skip it.
            ConnectionService.set_status(target.connection_esid, STATUS_EXPIRED)
            from asset_management.web.services import tiktok_account_projection

            tiktok_account_projection.sync(target.connection_esid)
            raise TargetFailed(f"TikTok token refresh rejected for {target.label} (invalid_grant) — reconnect it") from e
        raise TargetFailed(f"TikTok token refresh failed for {target.label}: {text}") from e

    new_refresh = str(body.get("refresh_token") or "")
    if new_refresh and new_refresh != refresh_token:
        if not ConnectionService.rotate_token(
            target.connection_esid, oauth_token=new_refresh, token_expires_at=refresh_expiry_iso(body)
        ):
            # TikTok may already have retired the old token. Stop before using
            # this one: a later run would otherwise fail on a token we lost.
            raise TargetFailed(f"could not store TikTok's rotated token for {target.label}; not imported this run")
    access_token = str(body.get("access_token") or "")
    if not access_token:
        raise TargetFailed(f"TikTok token refresh for {target.label} returned no access token")
    return access_token


def _prior_snapshots(es, team_esid: str, open_id: str, snapshot_date: str):
    """``(prior_account_source | None, {video_id: prior_video_source})`` — the
    latest snapshot strictly BEFORE ``snapshot_date`` (so a same-day re-run
    compares with yesterday, not with itself). A team with no index yet has
    no history."""
    from lib.es_helpers import build_data_index_name

    index = build_data_index_name(team_esid, SOURCE_SLUG)
    if not es.indices.exists(index=index):
        return None, {}

    def _filter(record_type: str) -> dict:
        return {
            "bool": {
                "filter": [
                    {"term": {"record_type": record_type}},
                    {"term": {"tiktok_open_id": open_id}},
                    {"range": {"date": {"lt": snapshot_date}}},
                ]
            }
        }

    account = es.search(
        index=index,
        body={"query": _filter(RECORD_ACCOUNT_DAILY), "sort": [{"date": {"order": "desc"}}], "size": 1},
    )
    account_hits = account.get("hits", {}).get("hits", [])
    prior_account = account_hits[0]["_source"] if account_hits else None

    videos = es.search(
        index=index,
        body={
            "size": 0,
            "query": _filter(RECORD_VIDEO_DAILY),
            "aggs": {
                "by_video": {
                    "terms": {"field": "video_id", "size": 10000},
                    "aggs": {"latest": {"top_hits": {"size": 1, "sort": [{"date": {"order": "desc"}}]}}},
                }
            },
        },
    )
    priors: dict[str, dict[str, Any]] = {}
    for bucket in (videos.get("aggregations") or {}).get("by_video", {}).get("buckets", []):
        hits = bucket.get("latest", {}).get("hits", {}).get("hits", [])
        if hits:
            priors[str(bucket.get("key"))] = hits[0].get("_source") or {}
    return prior_account, priors


def fetch_target(es, target, snapshot_date: str, run_path: str) -> int:
    """Snapshot one account for all its teams. Returns docs written; raises
    TargetFailed with a user-facing message."""
    from import_scripts.tiktok.snapshots import add_video_totals, build_account_doc, build_video_docs

    if "video.list" not in target.scopes or "user.info.stats" not in target.scopes:
        raise TargetFailed(f"TikTok access denied: {target.label} lacks the user.info.stats / video.list permission")

    lock_id = _acquire_refresh_lock(target.connection_esid)
    try:
        access_token = _refresh(target)
    finally:
        _release_refresh_lock(target.connection_esid, lock_id)

    fields = USER_FIELDS_BASE + (["username"] if "user.info.profile" in target.scopes else [])
    try:
        user = get_user_info(access_token, fields)
        videos, truncated = list_videos(access_token, VIDEO_FIELDS, MAX_VIDEO_PAGES)
    except TikTokError as e:
        raise TargetFailed(f"TikTok API call failed for {target.label}: {e}") from e
    if truncated:
        logging.warning("tiktok: %s has more than %d videos; imported the newest %d", target.label, len(videos), len(videos))
        _record(
            f"TikTok account {target.label}: only the newest {len(videos)} videos were imported",
            target.label,
        )

    target_meta = {
        "tiktok_connection_esid": target.connection_esid,
        "tiktok_open_id": target.open_id,
        "tiktok_username": str(user.get("username") or ""),
    }
    written = 0
    for team in target.teams:
        prior_account, priors = _prior_snapshots(es, team["team_esid"], target.open_id, snapshot_date)
        account_doc = build_account_doc(
            snapshot_date=snapshot_date, target_meta=target_meta, team=team, user=user, prior=prior_account
        )
        video_docs = build_video_docs(
            snapshot_date=snapshot_date,
            target_meta=target_meta,
            team=team,
            videos=videos,
            priors=priors,
            prior_account_date=(prior_account or {}).get("date"),
        )
        add_video_totals(account_doc, video_docs, first_snapshot=prior_account is None)
        docs = [account_doc] + video_docs
        path = os.path.join(run_path, f"{team['team_esid']}__{target.connection_esid}.json")
        with open(path, "w") as f:
            json.dump(docs, f)
        written += len(docs)
        logging.info(
            "tiktok: %s -> team %s: 1 account doc + %d video docs (%s)",
            target.label,
            team["team_title"],
            len(docs) - 1,
            "first snapshot" if prior_account is None else f"compared with {prior_account.get('date')}",
        )
    return written


def main(params):
    custom_logging.define("tiktok-get-organic-stats")

    from lib.billing_entitlement import record_billing_skip, source_billing_block
    from lib.es_helpers import get_elasticsearch_client
    from lib.import_run_context import set_current_team, set_run_id
    from lib.import_scope import require_scope, scope_resolution_error
    from import_scripts.tiktok.connections import resolve_targets
    from import_scripts.tiktok.run_files import STAGING_BASE, run_dir
    from import_scripts.youtube.get_youtube_analytics import resolve_team_ids_to_esids

    set_run_id(params.get("progress_run_id"))
    team_ids = require_scope(params, scope_param="team_ids", connector_id=CONNECTOR_ID)
    run_path = run_dir(STAGING_BASE, params.get("run_name"))

    team_esids = resolve_team_ids_to_esids(team_ids)
    if not team_esids:
        raise scope_resolution_error(scope_param="team_ids", scope_value=team_ids, connector_id=CONNECTOR_ID)

    targets, problems = resolve_targets(team_esids)
    for problem in problems:
        logging.error("tiktok: %s", problem.message)
        set_current_team(problem.team_esid or None)
        _record(problem.message, problem.detail)
    if not targets:
        raise scope_resolution_error(scope_param="team_ids", scope_value=team_ids, connector_id=CONNECTOR_ID)

    # billing pre-check per (account, team); fail-open inside the helper.
    for target in targets:
        kept = []
        for team in target.teams:
            block = source_billing_block(
                team_esid=team["team_esid"], asset_esid=target.connection_esid, provider_id=target.open_id
            )
            if block:
                logging.info("tiktok: billing skip %s for team %s (%s)", target.label, team["team_esid"], block)
                record_billing_skip(block, connector_id=CONNECTOR_ID, detail=target.label)
                continue
            kept.append(team)
        target.teams = kept
    targets = [t for t in targets if t.teams]
    if not targets:
        logging.error("tiktok: every in-scope account was skipped by the billing check; nothing imported")
        return False

    os.makedirs(run_path, exist_ok=True)
    es = get_elasticsearch_client()
    if es is None:
        raise RuntimeError("tiktok: could not connect to Elasticsearch")
    snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    imported, failed, docs = 0, 0, 0
    for target in targets:
        set_current_team(target.teams[0]["team_esid"] if len(target.teams) == 1 else None)
        try:
            docs += fetch_target(es, target, snapshot_date, run_path)
            imported += 1
            _record_health(target, None)
            from asset_management.web.services import tiktok_account_projection

            tiktok_account_projection.sync(target.connection_esid, last_snapshot_date=snapshot_date)
        except TargetFailed as e:
            failed += 1
            logging.error("tiktok: %s", e)
            _record(str(e), target.label)
            _record_health(target, str(e))

    logging.info(
        "TikTok fetch complete for %s: %d account(s) imported, %d failed, %d doc(s) staged in %s",
        snapshot_date,
        imported,
        failed,
        docs,
        run_path,
    )
    return imported > 0
