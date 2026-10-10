"""turn one TikTok account's API answers into snapshot docs.

The Display API returns LIFETIME counts only (followers, a video's total views).
Summing those across days would count the same views again every day — the
 triple-count class. So every doc carries both:

* the lifetime value as reported (``follower_count``, ``view_count`` …) — for
  "latest value" tiles and follower-over-time lines, and
* the change since the previous snapshot (``followers_gained``,
  ``views_gained`` …) — the additive field a per-day or per-range sum may use.

``*_gained`` is ``None`` (unknown, not zero) when there is no earlier snapshot
to compare with: history starts at the first import and cannot be backfilled.
One exception: a video first seen AFTER the account already had a snapshot is
new since then, so everything it has counts as gained.

Pure functions — no I/O — so the rules are unit-testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from lib.tiktok_constants import RECORD_ACCOUNT_DAILY, RECORD_VIDEO_DAILY

ACCOUNT_COUNT_FIELDS = {
    "follower_count": "followers_gained",
    "likes_count": "likes_gained",
    "video_count": "videos_gained",
}
VIDEO_COUNT_FIELDS = {
    "view_count": "views_gained",
    "like_count": "video_likes_gained",
    "comment_count": "comments_gained",
    "share_count": "shares_gained",
}
# Phase 7 — video gains rolled up onto the account_daily doc. A Summary
# Report tile binds ONE data view + field and cannot filter on record_type, and
# video_daily docs are excluded from account-level sums by default, so the
# per-video gains are invisible to it unless the account doc carries the total.
ACCOUNT_VIDEO_TOTALS = {
    "views_gained": "video_views_gained",
    "comments_gained": "video_comments_gained",
    "shares_gained": "video_shares_gained",
}


def _int_or_none(value: Any) -> int | None:  # Any: raw JSON from TikTok or ES
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _gained(current: int | None, prior: int | None) -> int | None:
    if current is None or prior is None:
        return None
    return current - prior


def _unix_to_date(value: Any) -> str | None:  # Any: TikTok create_time (int seconds)
    seconds = _int_or_none(value)
    if seconds is None or seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")


def build_account_doc(
    *,
    snapshot_date: str,
    target_meta: dict[str, str],
    team: dict[str, str],
    user: dict[str, Any],  # Any: TikTok user object
    prior: dict[str, Any] | None,  # Any: the previous account_daily _source
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "@timestamp": snapshot_date,
        "kibana_date": snapshot_date,
        "date": snapshot_date,
        "record_type": RECORD_ACCOUNT_DAILY,
        **target_meta,
        "tiktok_username": str(user.get("username") or target_meta.get("tiktok_username") or ""),
        "tiktok_display_name": str(user.get("display_name") or ""),
        "following_count": _int_or_none(user.get("following_count")),
        **team,
    }
    for count_field, gained_field in ACCOUNT_COUNT_FIELDS.items():
        current = _int_or_none(user.get(count_field))
        doc[count_field] = current
        doc[gained_field] = _gained(current, _int_or_none((prior or {}).get(count_field)))
    return doc


def build_video_docs(
    *,
    snapshot_date: str,
    target_meta: dict[str, str],
    team: dict[str, str],
    videos: list[dict[str, Any]],  # Any: TikTok video objects
    priors: dict[str, dict[str, Any]],  # Any: video_id -> previous video_daily _source
    prior_account_date: str | None,
) -> list[dict[str, Any]]:
    """One ``video_daily`` doc per video.

    ``prior_account_date`` is the date of the account's previous snapshot (or
    None when this is its first). A video with no prior doc of its own that was
    created on or after that date is new since the last snapshot: all of its
    counts are gains. With no prior account snapshot at all, gains are unknown.
    """
    docs: list[dict[str, Any]] = []
    for video in videos:
        video_id = str(video.get("id") or "").strip()
        if not video_id:
            continue
        create_date = _unix_to_date(video.get("create_time"))
        prior = priors.get(video_id)
        is_new_since_last = (
            prior is None and prior_account_date is not None and create_date is not None and create_date >= prior_account_date
        )
        doc: dict[str, Any] = {
            "@timestamp": snapshot_date,
            "kibana_date": snapshot_date,
            "date": snapshot_date,
            "record_type": RECORD_VIDEO_DAILY,
            **target_meta,
            "video_id": video_id,
            "video_title": str(video.get("title") or ""),
            "video_description": str(video.get("video_description") or ""),
            "video_create_date": create_date,
            "video_duration_seconds": _int_or_none(video.get("duration")),
            "video_share_url": str(video.get("share_url") or ""),
            **team,
        }
        for count_field, gained_field in VIDEO_COUNT_FIELDS.items():
            current = _int_or_none(video.get(count_field))
            doc[count_field] = current
            if is_new_since_last:
                doc[gained_field] = current
            else:
                doc[gained_field] = _gained(current, _int_or_none((prior or {}).get(count_field)))
        docs.append(doc)
    return docs


def add_video_totals(account_doc: dict[str, Any], video_docs: list[dict[str, Any]], *, first_snapshot: bool) -> None:
    """Stamp ``video_*_gained`` on the account doc (in place): the sum of the
    day's known per-video gains.

    On the account's first snapshot every gain is unknown, so the totals are
    None (unknown), not 0 — the same rule as the account's own ``*_gained``. A
    video whose own gain is unknown (older than the last snapshot, no prior doc)
    adds nothing.
    """
    for video_field, total_field in ACCOUNT_VIDEO_TOTALS.items():
        if first_snapshot:
            account_doc[total_field] = None
            continue
        account_doc[total_field] = sum(int(d[video_field]) for d in video_docs if d.get(video_field) is not None)
