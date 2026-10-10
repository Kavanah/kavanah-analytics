import subprocess
from datetime import datetime, timedelta, timezone
import logging
import lib.logging


def generate_date_range(start_date, end_date):
    """Generate a list of dates from start_date to end_date (inclusive)."""
    dates = []
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    while current <= end:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


def run_import_for_date(date, extra_params=None):
    """Run the page import process for a single date."""
    logging.info(f"Running get_page_insights.py for date: {date}...")
    get_insights_cmd = [
        "python3",
        "/app/python/import.py",
        "facebook",
        "get_page_insights",
        f"--date={date}",
    ]
    if extra_params:
        for key, value in extra_params.items():
            get_insights_cmd.append(f"--{key}={value}")
    subprocess.run(get_insights_cmd, check=True)


def main(params):
    lib.logging.define("fb-run-page-daily-imports")

    # the pages path had no scope guard at ANY level: a scope-less
    # invocation (Import Data with no team selected, or a bare CLI run) walked
    # every ds_facebook team in the cluster and wrote into every team's index —
    # the cross-workspace class that ads/echo/ga4/youtube/ig
    # each gained this guard for connector-by-connector. The guarded
    # orchestrator (run_daily_imports, require_scope at its main) always
    # forwards --team_ids here, so the legitimate cron / on-demand path is
    # unaffected. Bail BEFORE any subprocess.
    from lib.import_scope import require_scope

    require_scope(params, scope_param="team_ids", connector_id="meta_ads")

    # Determine date(s) to process
    specific_dates = params.get("dates")  # comma-separated list of specific dates
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    single_date = params.get("date")

    if specific_dates:
        dates = [d.strip() for d in specific_dates.split(",") if d.strip()]
        logging.info(f"Processing {len(dates)} specific dates")
    elif start_date and end_date:
        dates = generate_date_range(start_date, end_date)
        logging.info(f"Processing date range: {start_date} to {end_date} ({len(dates)} days)")
    elif single_date:
        dates = [single_date]
    else:
        # Default to previous day. UTC-anchored: naive local time
        # shifted "yesterday" by the container's timezone on manual runs (the
        # scheduler always passes explicit UTC-derived ranges; this fallback
        # only fires on bare CLI invocations). Matches _trailing_cutoff /
        # _maturation_tail_days in run_daily_imports, both UTC-anchored.
        dates = [(datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")]

    # Collect passthrough filter params. force_refetch rides along
    # so get_page_insights bypasses its already-imported skip (the Import Data
    # page's Force refresh was a silent no-op for pages without it). It only
    # feeds run_import_for_date; the send/archive steps never see it.
    # posts_only rides along too (posts window only, no page insights).
    passthrough_keys = ["team_ids", "fb_app_ids", "fb_page_id", "force_refetch", "posts_only"]
    extra_params = {k: params[k] for k in passthrough_keys if k in params}

    start_time = datetime.now()
    logging.info(f"Import started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # Note: No token check needed - using same tokens as Instagram

        # a posts-only run needs ONE call: get_page_insights widens
        # the posts window to the earliest requested date, so a per-date loop
        # would only re-pull the same posts once per day in the range.
        if str(extra_params.get("posts_only", "")).lower() in ("true", "1"):
            dates = [min(dates)]

        # Get Facebook Page Insights for each date
        for date in dates:
            run_import_for_date(date, extra_params)

        # Facebook Page data to Elastic
        logging.info("Running send_page_json_to_elastic.py...")
        send_cmd = [
            "python3",
            "/app/python/import.py",
            "facebook",
            "send_page_json_to_elastic",
        ]
        if extra_params.get("team_ids"):
            send_cmd.append(f"--team_ids={extra_params['team_ids']}")
        subprocess.run(send_cmd, check=True)

        # Facebook Page POSTS to Elastic (pages_read_engagement App-Review
        # feature). get_page_insights writes posts JSON to a sibling dir
        # alongside page_insights; load it into {team_esid}__fb_page_posts.
        # Best-effort: a posts-send failure must not fail the page run, so
        # this is not wrapped in check=True's hard abort for the whole job.
        logging.info("Running send_post_json_to_elastic.py...")
        post_send_cmd = [
            "python3",
            "/app/python/import.py",
            "facebook",
            "send_post_json_to_elastic",
        ]
        if extra_params.get("team_ids"):
            post_send_cmd.append(f"--team_ids={extra_params['team_ids']}")
        post_result = subprocess.run(post_send_cmd)
        if post_result.returncode != 0:
            logging.warning("send_post_json_to_elastic returned non-zero; continuing (posts are best-effort)")

        # Move JSON to Imported
        logging.info("Running archive_page_imported_json.py...")
        subprocess.run(
            [
                "python3",
                "/app/python/import.py",
                "facebook",
                "archive_page_imported_json",
            ],
            check=True,
        )

        logging.info("All Facebook page insights tasks completed successfully.")
    except subprocess.CalledProcessError as e:
        logging.error(f"An error occurred: {e}")
    finally:
        end_time = datetime.now()
        elapsed = end_time - start_time
        minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
        logging.info(f"Import ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} | Total time: {minutes}m {seconds}s")
