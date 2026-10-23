"""
YouTube Analytics Daily Import Orchestrator

Orchestrates the complete YouTube Analytics import workflow:
1. Ensure YouTube Analytics Elasticsearch template exists
2. Fetch channel analytics from YouTube Analytics API
3. Index JSON files to Elasticsearch
4. Archive processed files

Usage:
    python3 /app/python/import.py youtube run_daily_imports [--start_date=YYYY-MM-DD] [--end_date=YYYY-MM-DD]
"""

import sys
import logging
import subprocess
from datetime import datetime, timedelta

sys.path.insert(0, "/app/python")
import lib.logging as custom_logging


# fail-soft progress writers for the scheduler Run-now bar. The
# YouTube importer has no per-date loop (it now takes a per-team-set
# import lock — see main()); it runs three sequential
# steps (fetch -> index -> archive). We report progress by phase: each step sets
# a phase + bumps a nominal day counter (1/2/3 of 3) so the bar shows a real
# percentage and the current stage. All no-ops when run_id is None (cron path)
# and never raise — a progress-write failure must not affect the import.
_YT_STEPS_TOTAL = 3


def _yt_set_phase(run_id, phase, step_n):
    """Record the current YouTube stage + bump the milestone day counter."""
    if not run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.set_phase(run_id=run_id, phase=phase)
        ImportProgressService.update_progress(run_id=run_id, days_fetched=step_n)
    except Exception as exc:  # noqa: BLE001 — boundary; progress is a UX nicety
        logging.warning("youtube progress write raised: %s", exc)


def _yt_complete(run_id, status, error=None):
    """Terminal stamp (succeeded / failed). No-op on the cron path."""
    if not run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.complete_run(run_id=run_id, status=status, error=error)
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("youtube complete_run(%s) raised: %s", status, exc)


def run_subprocess(command, step_name):
    """
    Run a subprocess command and handle errors.

    Args:
        command: List of command arguments
        step_name: Name of the step for logging

    Returns:
        bool: True if successful, False otherwise
    """
    logging.info(f"\n{'='*60}")
    logging.info(f"Step: {step_name}")
    logging.info(f"Command: {' '.join(command)}")
    logging.info(f"{'='*60}")

    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)

        # Log stdout if present
        if result.stdout:
            logging.info(result.stdout)

        logging.info(f"✓ {step_name} completed successfully")
        return True

    except subprocess.CalledProcessError as e:
        logging.error(f"✗ {step_name} failed with exit code {e.returncode}")
        if e.stdout:
            logging.error(f"STDOUT:\n{e.stdout}")
        if e.stderr:
            logging.error(f"STDERR:\n{e.stderr}")
        return False

    except Exception as e:
        logging.error(f"✗ {step_name} failed: {e}")
        return False


def main(params):
    """
    Main entry point from import.py.

    Args:
        params: Dict with command-line parameters:
                - start_date: Start date (YYYY-MM-DD), defaults to 30 days ago
                - end_date: End date (YYYY-MM-DD), defaults to yesterday
                - channel_ids: Optional comma-separated list of channel IDs
    """
    custom_logging.define("youtube-analytics-daily-imports")

    # Parse date parameters
    end_date = params.get("end_date")
    start_date = params.get("start_date")

    if not end_date:
        end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if not start_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_date = (end_dt - timedelta(days=30)).strftime("%Y-%m-%d")

    channel_ids = params.get("channel_ids")
    team_ids = params.get("team_ids")

    # require a team scope. The cron full-cluster path is retired
    # (imports run via the scheduler, which always passes --team_ids);
    # a scope-less invocation is a misconfiguration and a cluster-wide pull is a
    # cross-workspace exposure. Bail BEFORE any YouTube API call / subprocess.
    from lib.import_scope import require_scope

    require_scope({"team_ids": team_ids}, scope_param="team_ids", connector_id="youtube")

    # the Report Scheduler Run-now dispatch pre-mints a de_import_progress
    # doc and passes its run_id here so the progress bar can show a real percentage
    # + stage. Cron / CLI runs pass nothing → progress_run_id is None and every
    # progress write is a no-op.
    progress_run_id = params.get("progress_run_id") or None
    # progress doc. Both automated launchers pre-mint the doc and
    # pass --progress_run_id (the Report Scheduler, _start_progress_run; the
    # wizard / Custom Range kickoff, _kickoff_on_demand_backfill) — GA4's
    # external_run_id path. GA4 only mints its own run when handed
    # --connection_esid + a resolvable principal; YouTube is team-scoped and is
    # never given either, so there is nothing to mint from here. The
    # manual Import Data page passes no run id (it shows import_runner status
    # files, not progress docs); such a run queues on a local ticket.

    # per-scope import lock, GA4 pattern. YouTube
    # had no lock, so it bypassed the global import cap entirely — and a 12-month
    # backfill runs ~2.6 h per team. The importer is team-scoped (channels and
    # per-account OAuth tokens resolve from the team's linked assets; it never
    # reads de_connection), so there is no per-connection key: the
    # scope is the run's TEAM-ESID set via scope_key_for_ids — the same function
    # the scheduler uses to stamp import_lock_scope for Cancel's force_release
    # (report_schedule_service._PER_SCOPE_LOCK_SOURCES). Different teams run in
    # parallel; the same team set serializes (double-click /
    # loser-resolve). acquire() then waits for a global slot (lib.import_slots).
    lock_scope = ""
    lock_id = None
    try:
        from lib.import_lock_service import ImportLockService

        lock_scope = ImportLockService.scope_key_for_ids(team_ids)
        lock_id = ImportLockService.acquire("youtube", scope_id=lock_scope, progress_run_id=progress_run_id)
        if lock_id is None:
            logging.warning("lock: another youtube importer holds the lock (scope=%r); exiting cleanly.", lock_scope)
            # resolve our progress doc IFF it's a true orphan (the lock
            # winner is a DIFFERENT run that will never finalize it). A
            # same-schedule winner adopting this same run_id and progressing it is
            # left alone (preserves the double-click protection). Fail-soft.
            if progress_run_id:
                try:
                    from lib.import_progress_service import ImportProgressService

                    ImportProgressService.resolve_orphaned_lock_loser(progress_run_id)
                except Exception as exc:  # noqa: BLE001
                    logging.warning("lock: resolve_orphaned_lock_loser raised: %s", exc)
            return
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("lock: acquire raised: %s — proceeding", exc)

    logging.info(f"\n{'='*60}")
    logging.info(f"YouTube Analytics Daily Import Workflow")
    logging.info(f"{'='*60}")
    logging.info(f"Date range: {start_date} to {end_date}")
    if channel_ids:
        logging.info(f"Filtered channels: {channel_ids}")
    if team_ids:
        logging.info(f"Filtered teams: {team_ids}")
    logging.info(f"{'='*60}\n")

    start_time = datetime.now()
    logging.info(f"Import started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Define workflow steps
    # (2026-05-21): removed the "Ensure YouTube Analytics
    # Template" step. The project-wide default template installed at
    # app startup now governs new index creation.
    steps = [
        {
            "name": "Fetch YouTube Analytics Data",
            "phase": "youtube_fetch",
            "command": [
                "python3",
                "/app/python/import.py",
                "youtube",
                "get_youtube_analytics",
                "--start_date",
                start_date,
                "--end_date",
                end_date,
            ],
        },
        {
            "name": "Index to Elasticsearch",
            "phase": "youtube_index",
            "command": [
                "python3",
                "/app/python/import.py",
                "youtube",
                "send_json_to_elastic",
            ],
        },
        {
            "name": "Archive Processed Files",
            "phase": "youtube_archive",
            "command": [
                "python3",
                "/app/python/import.py",
                "youtube",
                "archive_imported_json",
            ],
        },
    ]

    # Add channel filter if provided
    if channel_ids:
        steps[0]["command"].extend(["--channel_ids", channel_ids])

    # forward the team filter to the fetch step so a manual UI run
    # scoped to one team only writes that team's data.
    if team_ids:
        steps[0]["command"].extend(["--team_ids", team_ids])

    # forward the run's progress doc id to the fetch step so a
    # per-channel permission failure (HttpError 403) it records surfaces in the
    # Report Scheduler's "Errors" button. Only the fetch step talks to the API;
    # the index/archive steps have no user-actionable per-asset errors.
    if progress_run_id:
        steps[0]["command"].extend(["--progress_run_id", progress_run_id])

    try:
        # Execute workflow steps sequentially
        all_successful = True
        for i, step in enumerate(steps, start=1):
            # announce the stage BEFORE running it so the bar shows the
            # step that's in flight (and its share of the 3-step progress).
            _yt_set_phase(progress_run_id, step["phase"], i)
            success = run_subprocess(step["command"], step["name"])
            if not success:
                all_successful = False
                logging.error(f"Workflow stopped due to error in: {step['name']}")
                # terminal failure stamp so the Run-now bar resolves to a
                # failed state instead of hanging.
                _yt_complete(progress_run_id, "failed", error=f"step failed: {step['name']}")
                break

        # terminal success stamp (all three steps completed).
        if all_successful:
            _yt_complete(progress_run_id, "succeeded")
    finally:
        # release the lock (and its global import slot).
        if lock_id:
            try:
                from lib.import_lock_service import ImportLockService

                ImportLockService.release("youtube", lock_id, scope_id=lock_scope)
            except Exception as exc:  # noqa: BLE001
                logging.warning("lock release raised: %s", exc)

    # Final summary
    end_time = datetime.now()
    elapsed = end_time - start_time
    minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
    logging.info(f"\n{'='*60}")
    if all_successful:
        logging.info("✓ YouTube Analytics Daily Import Workflow COMPLETED")
    else:
        logging.error("✗ YouTube Analytics Daily Import Workflow FAILED")
    logging.info(f"Import ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} | Total time: {minutes}m {seconds}s")
    logging.info(f"{'='*60}\n")

    return all_successful


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run YouTube Analytics daily imports")
    parser.add_argument("--start_date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end_date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--channel_ids", help="Comma-separated channel IDs")
    parser.add_argument("--team_ids", help="Comma-separated team ESIDs (or titles) to scope the import")

    args = parser.parse_args()

    success = main(
        {
            "start_date": args.start_date,
            "end_date": args.end_date,
            "channel_ids": args.channel_ids,
            "team_ids": args.team_ids,
        }
    )

    sys.exit(0 if success else 1)
