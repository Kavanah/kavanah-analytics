import subprocess
import time
from datetime import datetime, timedelta
import logging
import lib.logging

# a self-hosted server rate-limited a 30-day backfill after ~23 days;
# a pause between days spreads the load (the fetch helpers also retry on 429).
DAY_PAUSE_SECONDS = 5


def _dates_in_range(start_date_str, end_date_str):
    """Yield each date string (YYYY-MM-DD) from start to end inclusive."""
    start = datetime.strptime(start_date_str, "%Y-%m-%d")
    end = datetime.strptime(end_date_str, "%Y-%m-%d")
    current = start
    while current <= end:
        yield current.strftime("%Y-%m-%d")
        current += timedelta(days=1)


def main(params):
    lib.logging.define("chatwoot-run-daily-imports")

    specific_dates = params.get("dates")  # comma-separated list of specific dates
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    single_date = params.get("date")

    # when the report scheduler dispatches a team-scoped import it
    # passes --team_ids=a,b. Forward it to every fetch sub-step so the pull is
    # scoped to those teams' chatwoot accounts; absent, each step pulls the
    # whole tenant (the daily-cron behavior).
    team_ids = params.get("team_ids")
    scope_args = [f"--team_ids={team_ids}"] if team_ids and team_ids is not True else []

    # require a team scope. The cron whole-tenant path is retired
    # (imports run via the scheduler, which always passes --team_ids);
    # a scope-less invocation is a misconfiguration and a tenant-wide pull is a
    # cross-workspace exposure. Bail BEFORE any fetch sub-step.
    from lib.import_scope import require_scope

    require_scope({"team_ids": team_ids}, scope_param="team_ids", connector_id="chatwoot")

    # Build list of dates to import
    if specific_dates:
        dates = [d.strip() for d in specific_dates.split(",") if d.strip()]
        logging.info(f"Processing {len(dates)} specific dates")
    elif start_date and end_date:
        dates = list(_dates_in_range(start_date, end_date))
        logging.info(f"Backfill mode: {start_date} to {end_date} ({len(dates)} days)")
    else:
        date = single_date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        dates = [date]

    start_time = datetime.now()
    logging.info(f"Import started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # a failed day no longer stops the backfill or passes silently:
    # the other days still run and are indexed, and the run returns False
    # (import.py then exits 1) naming what failed.
    failed_steps = []
    try:
        for i, date in enumerate(dates):
            if i:
                time.sleep(DAY_PAUSE_SECONDS)
            logging.info(f"Fetching Chatwoot account reports for {date}...")
            result = subprocess.run(
                [
                    "python3",
                    "/app/python/import.py",
                    "chatwoot",
                    "get_account_reports",
                    f"--date={date}",
                ]
                + scope_args,
            )
            if result.returncode != 0:
                logging.error(f"Chatwoot account reports for {date} failed (exit {result.returncode}).")
                failed_steps.append(f"account reports {date}")

        # Conversations and agents reflect current state — no date loop needed
        logging.info("Fetching Chatwoot conversations (open + pending)...")
        result = subprocess.run(["python3", "/app/python/import.py", "chatwoot", "get_conversations"] + scope_args)
        if result.returncode != 0:
            logging.error(f"Chatwoot conversations failed (exit {result.returncode}).")
            failed_steps.append("conversations")

        # Index all JSON files to Elasticsearch
        logging.info("Indexing account reports to Elasticsearch...")
        # index exactly the days fetched above. A --dates run passed
        # only dates[0], so the rest were fetched and never indexed.
        elastic_date_args = [f"--dates={','.join(dates)}"]
        subprocess.run(
            ["python3", "/app/python/import.py", "chatwoot", "send_json_to_elastic"] + elastic_date_args,
            check=True,
        )

        logging.info("Indexing conversations to Elasticsearch...")
        subprocess.run(
            ["python3", "/app/python/import.py", "chatwoot", "send_conversations_to_elastic"],
            check=True,
        )

    except subprocess.CalledProcessError as e:
        logging.error(f"An error occurred: {e}")
        return False
    finally:
        end_time = datetime.now()
        elapsed = end_time - start_time
        minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
        logging.info(f"Import ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} | Total time: {minutes}m {seconds}s")

    if failed_steps:
        logging.error(f"Chatwoot import finished with failures: {', '.join(failed_steps)}")
        return False
    logging.info("All tasks completed successfully.")
    return True
