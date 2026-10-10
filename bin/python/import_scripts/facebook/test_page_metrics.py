"""
Test individual Facebook Page Insights metrics to identify which ones are valid.
"""

import sys
import logging
import lib.logging
import requests
from datetime import datetime, timezone
from import_scripts.facebook.lib.assets import get_fb_pages
from import_scripts.facebook.get_insights import get_fb_app_user_token
from lib.fb_constants import FB_GRAPH_URL


def test_metric(page_id, metric_name, date, access_token, period="day"):
    """Test a single metric. Returns 'valid', 'no_data', or 'invalid'.

    ``period`` defaults to ``day`` (the daily-series case every existing caller
    wants). The page-level video probe overrides it to establish which
    granularities a metric actually serves.
    """
    lib.logging.define("fb-test-page-metrics")

    # Convert date to Unix timestamps
    date_obj = datetime.strptime(date, "%Y-%m-%d")
    since_timestamp = int(date_obj.replace(tzinfo=timezone.utc).timestamp())
    until_timestamp = since_timestamp + 86400  # Add 24 hours

    url = f"{FB_GRAPH_URL}/{page_id}/insights"
    params = {
        "metric": metric_name,
        "period": period,
        "since": since_timestamp,
        "until": until_timestamp,
        "access_token": access_token,
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        if data.get("data"):
            logging.info(f"✅ {metric_name}: VALID - returned {len(data['data'])} result(s)")
            return "valid"
        else:
            logging.warning(f"⚠️  {metric_name}: No data returned (metric accepted, page may be inactive)")
            return "no_data"
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 400:
            try:
                error_data = e.response.json()
                error = error_data.get("error", {})
                error_message = error.get("message", "Unknown")
                logging.error(f"❌ {metric_name}: INVALID - {error_message}")
            except:
                logging.error(f"❌ {metric_name}: INVALID - 400 error")
            return "invalid"
        else:
            logging.error(f"❌ {metric_name}: HTTP {e.response.status_code}")
            return "invalid"
    except Exception as e:
        logging.error(f"❌ {metric_name}: Exception - {e}")
        return "invalid"


def main(params):
    lib.logging.define("fb-test-page-metrics")
    from import_scripts.facebook.get_page_insights import get_all_teams_by_page_id
    from import_scripts.facebook.lib.assets import get_fb_apps, get_fb_app_users
    from lib.encryption import decrypt
    from facebook_business.api import FacebookAdsApi

    page_id = params.get("fb_page_id")
    date = params.get("date", "2026-01-01")

    if not page_id:
        logging.error("Missing required parameter: --fb_page_id")
        return

    # Get access token using the same logic as get_page_insights
    access_token = params.get("access_token")

    if not access_token:
        logging.info("No access token provided, attempting to resolve from assets...")

        # Get FB apps and tokens
        fb_apps = get_fb_apps(filters=None)
        if not fb_apps:
            logging.error("No Facebook apps found")
            return

        # Get first available token
        for fb_app_doc in fb_apps:
            fb_app_esid = fb_app_doc["_id"]
            fb_app = fb_app_doc["_source"]

            # Get users for this app
            all_fb_app_users = get_fb_app_users(filters={"fb_app_esid": fb_app_esid})

            for user_doc in all_fb_app_users:
                user_esid = user_doc["_id"]
                token = get_fb_app_user_token(user_esid)
                if token:
                    # Try to get page access token
                    from import_scripts.facebook.get_page_insights import (
                        get_page_access_token,
                    )

                    page_data = get_page_access_token(page_id, token)
                    if page_data and "access_token" in page_data:
                        access_token = page_data["access_token"]
                        logging.info(f"✅ Resolved page access token from FB App user")
                        break

            if access_token:
                break

        if not access_token:
            logging.error("Could not resolve access token. Try passing --access_token manually")
            return

    logging.info(f"Testing Facebook Page Insights metrics for page {page_id} on {date}\n")

    # Metrics currently requested by the importer. read the SINGLE
    # source of truth (get_page_insights.PAGE_INSIGHTS_METRICS) so this probe
    # never drifts from the real request — the prior hardcoded copy still listed
    # the -removed page_views_total / page_impressions_unique and missed
    # page_media_view, so it tested a request the importer no longer sends.
    from import_scripts.facebook.get_page_insights import PAGE_INSIGHTS_METRICS

    metrics_to_test = list(PAGE_INSIGHTS_METRICS)

    valid_metrics = []
    no_data_metrics = []
    invalid_metrics = []

    for metric in metrics_to_test:
        result = test_metric(page_id, metric, date, access_token)
        if result == "valid":
            valid_metrics.append(metric)
        elif result == "no_data":
            no_data_metrics.append(metric)
        else:
            invalid_metrics.append(metric)

    logging.info(f"\n{'='*60}")
    logging.info(f"SUMMARY")
    logging.info(f"{'='*60}")

    logging.info(f"Valid — returned data ({len(valid_metrics)}):")
    for metric in valid_metrics:
        logging.info(f"  ✅ {metric}")

    logging.info(f"\nAccepted — no data for this page/date ({len(no_data_metrics)}):")
    logging.info(f"  (Metric name is valid; try a more active page or date to confirm)")
    for metric in no_data_metrics:
        logging.info(f"  ⚠️  {metric}")

    logging.info(f"\nInvalid — API rejected with error ({len(invalid_metrics)}):")
    for metric in invalid_metrics:
        logging.info(f"  ❌ {metric}")

    usable = valid_metrics + no_data_metrics
    logging.info(f"\nSuggested metric string (valid + accepted, max 10):")
    if usable:
        logging.info(f"  '{','.join(usable[:10])}'")
    else:
        logging.info("  No usable metrics found!")

    # -- Page-level daily video-view candidates ---
    # page_video_views_15s_total is NOT a Meta metric: get_page_insights
    # synthesizes it by summing post_video_views_15s over videos PUBLISHED on
    # the target date, so any day without a video post reads 0 regardless of
    # actual viewing. Before rebuilding that as a rolling window, probe whether
    # Meta serves a genuine page-level DAILY equivalent we could use instead.
    # These are candidates only — several were retired in recent Graph
    # versions, which is exactly what this probe establishes.
    logging.info(f"\n{'='*60}")
    logging.info(f"PAGE-LEVEL DAILY VIDEO-VIEW CANDIDATES")
    logging.info(f"{'='*60}")
    logging.info("Probing for a true daily page-level video-views metric (period=day)...\n")

    # page_uploaded_views_15s_count is the documented page-level equivalent of
    # what the tile promises: "Total number of video views for those who watched
    # at least 15 seconds of your Page's video." The docs do NOT state which
    # periods it supports, which is the open question this probe answers — a
    # metric that only serves `lifetime` cannot back a daily series.
    page_video_candidates = [
        "page_video_views",  # already imported — baseline control
        "page_uploaded_views_15s_count",  # the target: documented 15s page metric
        "page_video_complete_views_30s",
        "page_video_views_autoplayed",
        "page_video_views_click_to_play",
        "page_video_views_by_paid_non_paid",
        "page_video_views_unique",  # deprecated above v25 — expect rejection
    ]

    # Probe each candidate across every period. `day` is what a daily series
    # needs; a metric that only answers `lifetime` or `days_28` is unusable for
    # per-day reporting even though it exists, and testing `day` alone cannot
    # tell that apart from a retired metric.
    day_capable = []
    other_period_only = []
    page_video_invalid = []

    for metric in page_video_candidates:
        periods_ok = []
        for period in ("day", "week", "days_28", "lifetime"):
            if test_metric(page_id, metric, date, access_token, period=period) in ("valid", "no_data"):
                periods_ok.append(period)
        if "day" in periods_ok:
            day_capable.append((metric, periods_ok))
            logging.info(f"  ✅ {metric}: usable periods = {', '.join(periods_ok)}")
        elif periods_ok:
            other_period_only.append((metric, periods_ok))
            logging.info(f"  ⚠️  {metric}: NO day period; usable = {', '.join(periods_ok)}")
        else:
            page_video_invalid.append(metric)
            logging.info(f"  ❌ {metric}: rejected on every period")

    logging.info(
        f"\nPage-level video candidates: {len(day_capable)} support period=day, "
        f"{len(other_period_only)} exist but not daily, {len(page_video_invalid)} rejected outright"
    )
    fifteen = [m for m, _ in day_capable if "15s" in m]
    if fifteen:
        logging.info(
            f"  → {fifteen[0]} supports period=day. Use it directly for the 15s tile "
            "instead of summing post-level lifetime values."
        )
    elif any("15s" in m for m, _ in other_period_only):
        logging.info(
            "  → A 15s page metric exists but NOT at day granularity. A daily series "
            "is impossible from it; rolling-window is the fallback."
        )
    else:
        logging.info(
            "  → NO page-level 15s metric reachable with this token. Check whether "
            "read_insights scope is granted before concluding it is deprecated."
        )

    # --- Post-level video metric test ---
    logging.info(f"\n{'='*60}")
    logging.info(f"POST-LEVEL VIDEO METRICS TEST")
    logging.info(f"{'='*60}")
    logging.info("Fetching recent video posts to test post_video_views_15s* variants...")

    post_video_metrics = [
        "post_video_views_15s",  # used for page_video_views_15s_total
        "post_video_avg_time_watched",  # used for page_video_avg_sec_watched calculation
        "post_video_views",  # used as weight in avg_sec_watched calculation
    ]

    try:
        resp = requests.get(
            f"{FB_GRAPH_URL}/{page_id}/posts",
            params={
                "fields": "id,created_time,shares,attachments{type,media_type}",
                "access_token": access_token,
                "limit": 25,
            },
        )
        resp.raise_for_status()
        posts = resp.json().get("data", [])

        video_posts = [
            p
            for p in posts
            if any(
                a.get("media_type") == "video" or a.get("type") == "video_inline"
                for a in p.get("attachments", {}).get("data", [])
            )
        ]

        logging.info(f"Found {len(posts)} recent posts, {len(video_posts)} with video attachments")

        # Show shares field values from the posts fetch (Option 1 — free field, no extra API call)
        logging.info(f"\n--- shares field (from posts fetch, all post types) ---")
        posts_with_shares = [(p["id"], p.get("shares", {}).get("count", 0), p.get("created_time", "?")) for p in posts]
        for pid, count, ts in posts_with_shares[:5]:
            logging.info(f"  post {pid} ({ts}): shares={count}")
        total_shares = sum(c for _, c, _ in posts_with_shares)
        logging.info(f"  Total shares across {len(posts)} fetched posts: {total_shares}")

        if not video_posts:
            logging.warning("No video posts found — cannot test post-level insight metrics")
        else:
            test_post = video_posts[0]
            post_id = test_post["id"]
            logging.info(
                f"\n--- post-level insight metrics against post {post_id} (created {test_post.get('created_time', '?')}) ---\n"
            )

            for metric in post_video_metrics:
                try:
                    r = requests.get(
                        f"{FB_GRAPH_URL}/{post_id}/insights",
                        params={"metric": metric, "access_token": access_token},
                    )
                    r.raise_for_status()
                    data = r.json().get("data", [])
                    if data:
                        values = data[0].get("values", [])
                        val = values[0].get("value", 0) if values else 0
                        logging.info(f"  ✅ {metric}: VALID — value={val}")
                    else:
                        logging.warning(f"  ⚠️  {metric}: accepted but no data")
                except requests.exceptions.HTTPError as e:
                    try:
                        err_msg = e.response.json().get("error", {}).get("message", str(e))
                    except Exception:
                        err_msg = str(e)
                    logging.error(f"  ❌ {metric}: INVALID — {err_msg}")
                except Exception as e:
                    logging.error(f"  ❌ {metric}: Exception — {e}")

    except Exception as e:
        logging.error(f"Could not fetch posts for video test: {e}")
