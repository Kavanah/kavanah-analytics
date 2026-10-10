"""
Test Instagram Insights via Facebook Page ID

Given a Facebook Page ID, this script:
  1. Resolves a page access token from configured FB apps
  2. Looks up the Instagram Business Account linked to the page
  3. Fetches Instagram user metadata
  4. Fetches Instagram account-level insights
  5. Fetches recent media for the date

Usage:
    python3 /app/python/import.py instagram test_page_instagram_insights --fb_page_id=112289710303765
    python3 /app/python/import.py instagram test_page_instagram_insights --fb_page_id=112289710303765 --date=2025-02-23
"""

import logging
import lib.logging
from datetime import datetime, timedelta
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.page import Page
from facebook_business.adobjects.iguser import IGUser
from facebook_business.exceptions import FacebookRequestError
from import_scripts.facebook.lib.assets import get_fb_apps, get_fb_app_users
from import_scripts.facebook.get_insights import get_fb_app_user_token
from import_scripts.facebook.get_page_insights import get_page_access_token
from lib.fb_constants import FB_GRAPH_URL, FB_GRAPH_VERSION
from lib.encryption import decrypt


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\033[0m"


def ok(msg):
    print(f"{Colors.GREEN}✓ {msg}{Colors.RESET}")


def err(msg):
    print(f"{Colors.RED}✗ {msg}{Colors.RESET}")


def warn(msg):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.RESET}")


def info(msg):
    print(f"{Colors.BLUE}ℹ {msg}{Colors.RESET}")


def resolve_page_token(page_id):
    """
    Walk all configured FB apps/users and return the first page access token
    that works for page_id. Returns (app_id, app_secret, page_token) or None.
    """
    fb_apps = get_fb_apps(filters=None)
    if not fb_apps:
        err("No Facebook apps found in asset manager")
        return None

    info(f"Trying {len(fb_apps)} FB app(s) to find a page token...")

    for app_doc in fb_apps:
        app = app_doc["_source"]
        app_esid = app_doc["_id"]
        app_id = app.get("fb_app_id")
        app_secret = decrypt(app.get("fb_app_secret"))
        app_title = app.get("fb_app_title", "Unknown")

        users = get_fb_app_users(filters={"fb_app_esid": app_esid})
        for user_doc in users:
            user_token = get_fb_app_user_token(user_doc["_id"])
            if not user_token:
                continue

            page_data = get_page_access_token(page_id, user_token)
            if page_data and page_data.get("access_token"):
                ok(f"Page token obtained via FB App: {app_title} / user: {user_doc['_source'].get('fb_app_user_title', '?')}")
                return app_id, app_secret, page_data["access_token"]

    err(f"No app/user combination could produce a page token for page {page_id}")
    return None


def step_resolve_ig_account(page_id):
    """Step 2: Query the page for its linked Instagram Business Account."""
    print(f"\n{'='*70}")
    print("STEP 2: Resolve Instagram Business Account from Page")
    print("=" * 70)

    try:
        page = Page(page_id).api_get(fields=["id", "name", "instagram_business_account"])
        page_data = page.export_all_data()

        page_name = page_data.get("name", "unknown")
        ig_data = page_data.get("instagram_business_account", {})
        ig_id = ig_data.get("id") if ig_data else None

        ok(f"Page: {page_name} (ID: {page_id})")

        if ig_id:
            ok(f"Linked Instagram Business Account ID: {ig_id}")
        else:
            err("No Instagram Business Account linked to this page")
            warn("To fix: open Instagram app → Settings → Account → Switch to Professional Account → Business")
            warn("Then link the Instagram account to this Facebook Page in Page Settings → Instagram")

        return ig_id

    except FacebookRequestError as e:
        err(f"Failed to query page {page_id}: code={e.api_error_code()} {e.api_error_message()}")
        return None


def step_fetch_user_metadata(ig_id):
    """Step 3: Fetch Instagram user profile fields."""
    print(f"\n{'='*70}")
    print("STEP 3: Fetch Instagram User Metadata")
    print("=" * 70)

    try:
        user = IGUser(ig_id).api_get(
            fields=[
                "id",
                "username",
                "name",
                "biography",
                "website",
                "followers_count",
                "follows_count",
                "media_count",
                "profile_picture_url",
            ]
        )
        data = user.export_all_data()

        ok(f"@{data.get('username')} — {data.get('name')}")
        print(f"  Followers : {data.get('followers_count')}")
        print(f"  Following : {data.get('follows_count')}")
        print(f"  Posts     : {data.get('media_count')}")
        print(f"  Bio       : {data.get('biography', '').strip()[:80]}")
        print(f"  Website   : {data.get('website', 'none')}")
        return data

    except FacebookRequestError as e:
        code = e.api_error_code()
        err(f"Failed to fetch user metadata: code={code} {e.api_error_message()}")
        if code == 10:
            warn("Error 10: account may not be a Business account, or token lacks instagram_basic")
        elif code == 100:
            warn("Error 100: invalid Instagram account ID or token cannot access this account")
        return None


def step_fetch_insights(ig_id, date, page_token):
    """Step 4: Fetch account-level insights for the given date."""
    print(f"\n{'='*70}")
    print(f"STEP 4: Fetch Account Insights for {date}")
    print("=" * 70)

    try:
        import requests as req

        user = IGUser(ig_id)
        parsed = {}

        # Convert date to Unix timestamps. The API treats since/until as a half-open
        # interval [since, until), so until must be the start of the *next* day.
        from datetime import timezone as tz

        date_obj = datetime.strptime(date, "%Y-%m-%d")
        since_timestamp = int(date_obj.replace(tzinfo=tz.utc).timestamp())
        until_timestamp = since_timestamp + 86400  # next day

        # Call 1: time-series metrics via SDK
        # follower_count excluded — only supports last 30 days, and is
        # already shown in Step 3 user metadata (followers_count field)
        ts_insights = user.get_insights(
            params={
                "metric": ["reach"],
                "period": ["day"],
                "since": since_timestamp,
                "until": until_timestamp,
            }
        )
        for item in ts_insights:
            name = item.get("name")
            vals = item.get("values", [])
            parsed[name] = vals[0].get("value", 0) if vals else 0

        # Call 2: total-value metrics via raw HTTP
        # The Facebook Business SDK silently drops the total_value field from responses,
        # so we bypass it and call the Graph API directly.
        tv_metrics = [
            "profile_views",
            "website_clicks",
            "views",
            "accounts_engaged",
            "total_interactions",
            "follows_and_unfollows",
        ]
        response = req.get(
            f"{FB_GRAPH_URL}/{ig_id}/insights",
            params={
                "metric": ",".join(tv_metrics),
                "metric_type": "total_value",
                "period": "day",
                "since": since_timestamp,
                "until": until_timestamp,
                "access_token": page_token,
            },
        )
        response.raise_for_status()
        for item in response.json().get("data", []):
            name = item.get("name")
            total_value = item.get("total_value", {})
            parsed[name] = total_value.get("value", 0) if total_value else 0

        if parsed:
            ok(f"Received {len(parsed)} insight metric(s):")
            for metric, value in parsed.items():
                print(f"  {metric:<35} {value}")
        else:
            warn("Insights API returned no data for this date")

        return parsed

    except FacebookRequestError as e:
        code = e.api_error_code()
        err(f"Failed to fetch insights: code={code} {e.api_error_message()}")
        if code == 10:
            warn("Error 10: account is not a Business account, or token is missing instagram_manage_insights")
        elif code == 100:
            warn("Error 100: invalid parameters or date range")
        return None


def step_fetch_media(ig_id, date):
    """Step 5: Fetch media posted on the given date."""
    print(f"\n{'='*70}")
    print(f"STEP 5: Fetch Media Posted on {date}")
    print("=" * 70)

    try:
        user = IGUser(ig_id)
        media_list = user.get_media(
            fields=[
                "id",
                "media_type",
                "caption",
                "timestamp",
                "like_count",
                "comments_count",
                "permalink",
            ]
        )

        date_obj = datetime.strptime(date, "%Y-%m-%d").date()
        day_media = []
        for media in media_list:
            mdata = media.export_all_data()
            ts = mdata.get("timestamp", "")
            if ts:
                mdate = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                if mdate == date_obj:
                    day_media.append(mdata)

        if day_media:
            ok(f"{len(day_media)} post(s) on {date}:")
            for m in day_media:
                caption_preview = (m.get("caption") or "")[:60].replace("\n", " ")
                print(f"  [{m.get('media_type')}] likes={m.get('like_count')} comments={m.get('comments_count')}")
                print(f"    {m.get('permalink')}")
                if caption_preview:
                    print(f'    "{caption_preview}"')
        else:
            info(f"No posts found on {date} (account may have posted on other dates)")

        return day_media

    except FacebookRequestError as e:
        code = e.api_error_code()
        err(f"Failed to fetch media: code={code} {e.api_error_message()}")
        if code == 10:
            warn("Error 10: token is missing 'instagram_basic' permission")
            warn("Re-authorize the FB App user token with instagram_basic scope to enable media access")
        return []


def generate_date_range(start_date, end_date):
    """Return list of YYYY-MM-DD strings from start_date to end_date inclusive."""
    dates = []
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    while current <= end:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


def main(params):
    lib.logging.define("ig-test-page-insights")

    page_id = params.get("fb_page_id")
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    single_date = params.get("date")
    direct_token = params.get("access_token")
    direct_ig_id = params.get("ig_id")

    if not page_id and not (direct_token and direct_ig_id):
        err("Missing required parameter: --fb_page_id")
        print("Usage: python3 /app/python/import.py instagram test_page_instagram_insights --fb_page_id=<PAGE_ID>")
        print("       --date=YYYY-MM-DD            (single date, default: yesterday)")
        print("       --start_date=YYYY-MM-DD --end_date=YYYY-MM-DD  (date range)")
        print("       --access_token=<TOKEN>       (skip Step 1 — provide page token directly)")
        print("       --access_token=<TOKEN> --ig_id=<IG_ID>  (skip Steps 1 & 2 entirely)")
        return

    # Build date list
    if start_date and end_date:
        dates = generate_date_range(start_date, end_date)
        date_label = f"{start_date} → {end_date} ({len(dates)} days)"
    elif single_date:
        dates = [single_date]
        date_label = single_date
    else:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        dates = [yesterday]
        date_label = yesterday

    print("\n" + "=" * 70)
    print(" INSTAGRAM INSIGHTS TEST VIA FACEBOOK PAGE ID")
    print("=" * 70)
    print(f" Page ID : {page_id or '(skipped)'}")
    print(f" Date(s) : {date_label}")
    print("=" * 70)

    # Step 1: resolve a page access token (once, reused for all dates)
    print(f"\n{'='*70}")
    print("STEP 1: Resolve Page Access Token")
    print("=" * 70)

    if direct_token:
        ok("Using --access_token provided directly (skipping DEAM token lookup)")
        page_token = direct_token
        FacebookAdsApi.init(access_token=page_token, api_version=FB_GRAPH_VERSION)
    else:
        result = resolve_page_token(page_id)
        if not result:
            err("Cannot continue without a page access token")
            return
        app_id, app_secret, page_token = result
        FacebookAdsApi.init(app_id, app_secret, page_token, api_version=FB_GRAPH_VERSION)

    # Step 2: look up linked Instagram account (once)
    if direct_ig_id:
        ig_id = direct_ig_id
        print(f"\n{'='*70}")
        print("STEP 2: Resolve Instagram Business Account from Page")
        print("=" * 70)
        ok(f"Using --ig_id provided directly: {ig_id}")
    else:
        ig_id = step_resolve_ig_account(page_id)
    if not ig_id:
        err("Cannot continue without an Instagram Business Account ID")
        return

    # Step 3: user metadata (once — not date-specific)
    metadata = step_fetch_user_metadata(ig_id)

    # Steps 4 & 5: loop over each date
    total_insights_ok = 0
    total_media = 0

    for date in dates:
        insights = step_fetch_insights(ig_id, date, page_token)
        media = step_fetch_media(ig_id, date)
        if insights:
            total_insights_ok += 1
        total_media += len(media)

    # Summary
    print(f"\n{'='*70}")
    print(" SUMMARY")
    print("=" * 70)
    (ok(f"Page token        : resolved") if page_token else err("Page token        : failed"))
    ok(f"Instagram ID      : {ig_id}")
    (ok(f"User metadata     : fetched") if metadata else err("User metadata     : failed"))
    ok(f"Insights          : {total_insights_ok}/{len(dates)} date(s) succeeded")
    info(f"Media found       : {total_media} post(s) across {len(dates)} date(s)")
    print("=" * 70 + "\n")
