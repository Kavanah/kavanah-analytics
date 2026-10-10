import subprocess
from datetime import datetime, timedelta
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


def run_import_for_date(date, profile_id=None, team_ids=None, backfill_bailout=False):
    """Run the Instagram import process for a single date.

    ``team_ids`` is the comma-separated team-ESID
    filter the FB importer passes through when it's running scoped to a
    single wizard-cohort team. Cron-path callers (no team_ids) get the
    unfiltered behaviour; pass-through preserves backward compat.

    ``backfill_bailout`` forwards the backfill flag so a
    throttled per-date run bails out (keep partial data) instead of hammering.
    """
    logging.info(f"Running get_instagram_insights.py for date: {date}...")
    get_insights_cmd = [
        "python3",
        "/app/python/import.py",
        "instagram",
        "get_instagram_insights",
        f"--date={date}",
    ]
    if profile_id:
        get_insights_cmd.append(f"--profile_id={profile_id}")
        logging.info(f"Filtering to profile ID: {profile_id}")
    if team_ids:
        get_insights_cmd.append(f"--team_ids={team_ids}")
        logging.info(f"Filtering to team_ids: {team_ids}")
    if backfill_bailout:
        get_insights_cmd.append("--backfill_bailout=true")
    subprocess.run(get_insights_cmd, check=True)


def main(params):
    lib.logging.define("ig-run-daily-imports")

    # Determine date(s) to process
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    single_date = params.get("date")

    if start_date and end_date:
        dates = generate_date_range(start_date, end_date)
        logging.info(f"Processing Instagram date range: {start_date} to {end_date} ({len(dates)} days)")
    elif single_date:
        dates = [single_date]
    else:
        # Default to previous day
        dates = [(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")]

    profile_id = params.get("profile_id")
    # item 1 — propagate the FB importer's team_ids filter into
    # the per-date get_instagram_insights call. When the FB importer
    # runs scoped to a wizard-cohort team (via --connection_esid
    # resolved to one team), it passes --team_ids to us; we forward
    # that to get_instagram_insights so the IG fan-out stays scoped
    # to the same team. Cron-path callers (no team_ids param) get the
    # full-fleet behaviour unchanged.
    team_ids = params.get("team_ids")
    # item 2 — the FB importer passes --progress_run_id through
    # so the IG sibling can observe a mid-flight user cancel against
    # the same progress doc. None on the cron path (no progress doc).
    progress_run_id = params.get("progress_run_id")
    # the FB importer threads --backfill_bailout to us;
    # forward it into each per-date get_instagram_insights call so a throttled
    # backfill bails out (keep partial data) instead of hammering the app.
    backfill_bailout = params.get("backfill_bailout") in (True, "true", "True", "1", 1)

    # require a team scope. IG runs as a child stage of the FB
    # importer, which now requires --team_ids and always forwards it here; the
    # retired cron full-fleet path is the only scope-less caller. Bail BEFORE any
    # IG API call so a misconfigured scope-less run can't fan out cluster-wide.
    from lib.import_scope import require_scope

    require_scope({"team_ids": team_ids}, scope_param="team_ids", connector_id="meta_ads")

    start_time = datetime.now()
    logging.info(f"Import started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # Check FB token (reuse existing Facebook token check).
        # propagate team_ids so the token check honors strict mode
        # and short-circuits for wizard-cohort scopes (workspace isolation).
        # Without this propagation, the IG runner triggered check_fb_token
        # in its fleet-wide mode and probed every legacy app's token —
        # exactly the cross-tenant noise closes elsewhere.
        logging.info("Running check_fb_token.py...")
        check_token_cmd = ["python3", "/app/python/import.py", "facebook", "check_fb_token"]
        if team_ids:
            check_token_cmd.append(f"--team_ids={team_ids}")
        subprocess.run(check_token_cmd, check=True)

        # Get Instagram Insights for each date
        for date in dates:
            # item 2 — between-date cancellation check. Mirrors
            # the FB run_daily_imports loop's same-named check. is_cancelling
            # is fail-soft (False on any error) so a transient blip doesn't
            # abort an import the user hasn't asked to stop.
            if progress_run_id:
                try:
                    from lib.import_progress_service import ImportProgressService

                    if ImportProgressService.is_cancelling(progress_run_id):
                        logging.info(
                            "ig-importer: cancellation requested at date %s; " "breaking out of per-date loop.",
                            date,
                        )
                        return
                except Exception as exc:  # noqa: BLE001 — boundary
                    logging.warning("ig-importer is_cancelling raised: %s — proceeding", exc)

            run_import_for_date(date, profile_id=profile_id, team_ids=team_ids, backfill_bailout=backfill_bailout)

        # Instagram to Elastic
        logging.info("Running send_instagram_json_to_elastic.py...")
        # pass team_ids so the sender only indexes THIS run's teams.
        # The ig_insights/ dir is shared; without the scope a team-scoped run
        # would sweep up another team's leftover JSON (cross-workspace leak).
        ig_send_cmd = [
            "python3",
            "/app/python/import.py",
            "instagram",
            "send_instagram_json_to_elastic",
        ]
        if team_ids:
            ig_send_cmd.append(f"--team_ids={team_ids}")
        subprocess.run(ig_send_cmd, check=True)

        # Move JSON to Imported
        logging.info("Running archive_instagram_imported_json.py...")
        subprocess.run(
            [
                "python3",
                "/app/python/import.py",
                "instagram",
                "archive_instagram_imported_json",
            ],
            check=True,
        )

        logging.info("All Instagram import tasks completed successfully.")
    except subprocess.CalledProcessError as e:
        logging.error(f"An error occurred during Instagram import: {e}")
        # signal the failure to the CALLER. This handler logged the
        # error and then fell through, so main() returned None and import.py
        # exited 0: the Report Scheduler recorded a false SUCCESS for an import
        # whose critical path (get_instagram_insights, the sender, the archive
        # step — all check=True) did not land.
        #
        # `None` cannot be the failure signal, because it is what every connector
        # that reports failure by RAISING returns on success. So return False
        # explicitly; import.py converts that to a non-zero exit.
        # Returning rather than re-raising keeps the finally block's timing log
        # intact and matches the bool convention the other connectors use.
        #
        # NOTE the cancellation `return` above (, the is_cancelling check
        # in the per-date loop) is deliberately left alone: a user-cancelled
        # import is not a FAILED one, so it must keep returning None.
        return False
    finally:
        end_time = datetime.now()
        elapsed = end_time - start_time
        minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
        logging.info(f"Import ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} | Total time: {minutes}m {seconds}s")
