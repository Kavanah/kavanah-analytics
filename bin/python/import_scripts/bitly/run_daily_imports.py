"""
Bitly Daily Import Orchestrator

Orchestrates the Bitly link-clicks import workflow:
1. Fetch daily click counts from the Bitly v4 API (tag → team attribution,
   credentials and scope from de_connection — see connections.py)
2. Index JSON files to Elasticsearch ({team_esid}__bitly_clicks)
3. Archive processed files

Usage:
    python3 /app/python/import.py bitly run_daily_imports [--days=N]
"""

import logging
import subprocess
import sys
from datetime import datetime

import lib.logging as custom_logging

# Phase 3 — fail-soft progress writers for the scheduler Run-now bar
# (YouTube pattern). The Bitly importer has no per-date loop; it runs
# three sequential steps (fetch -> index -> archive) and reports progress by
# phase: each step sets a phase + bumps a nominal counter (1/2/3 of 3). All
# no-ops when run_id is None (cron path) and never raise — a progress-write
# failure must not affect the import. Keep _BITLY_STEPS_TOTAL in sync with
# report_schedule_service._progress_total's bitly branch.
_BITLY_STEPS_TOTAL = 3


def _bitly_set_phase(run_id, phase, step_n):
    """Record the current Bitly stage + bump the milestone counter."""
    if not run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.set_phase(run_id=run_id, phase=phase)
        ImportProgressService.update_progress(run_id=run_id, days_fetched=step_n)
    except Exception as exc:  # noqa: BLE001 — boundary; progress is a UX nicety
        logging.warning("bitly progress write raised: %s", exc)


def _bitly_complete(run_id, status, error=None):
    """Terminal stamp (succeeded / failed). No-op on the cron path."""
    if not run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.complete_run(run_id=run_id, status=status, error=error)
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("bitly complete_run(%s) raised: %s", status, exc)


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

    returns False (or raises) on failure so import.py
    propagates a non-zero exit and the Report Scheduler records the run as
    failed instead of a silent green no-op. A lock-held abort returns None
    (clean exit — another Bitly import is already running; echo pattern).

    Args:
        params: Dict of command-line parameters
                - days: Trailing window of daily click points to pull
                        (default 3, max 30)
                - progress_run_id: de_import_progress doc id minted by the
                  Report Scheduler's Run-now dispatch. Absent on
                  cron/CLI runs → every progress write is a no-op.
    """
    custom_logging.define("bitly-run-daily-imports")

    # Scope guard. Phase 3: Bitly's scope is the set of
    # ACTIVE bitly de_connection rows (each carrying a team + tag filter), not
    # a config file — refuse to run when there are none. A scope-less run
    # could attribute nothing and would mask a misconfiguration as a
    # successful zero-row import. Checked BEFORE any Bitly API call /
    # subprocess / lock acquisition.
    from import_scripts.bitly.connections import list_bitly_connection_configs
    from import_scripts.bitly.get_link_clicks import resolve_days

    # Read the progress run id FIRST: every refusal below must stamp the
    # Run-now progress doc failed, or the UI's poll hangs on "pending"
    # forever (staging 2026-08-05: orphan-swept connection → guard refused
    # → stuck pending progress row).
    progress_run_id = params.get("progress_run_id") or None

    try:
        configs = list_bitly_connection_configs()
    except Exception as e:  # noqa: BLE001 — ES/decrypt failures must fail the run loudly
        logging.error(f"bitly: connection enumeration failed — {e}")
        _bitly_complete(progress_run_id, "failed", error="connection enumeration failed")
        return False
    if not configs:
        logging.error(
            "bitly: no active Bitly connections in de_connection — no team scope "
            "is configured, so nothing can be attributed. Connect Bitly from the "
            "Data Connectors page. Nothing was imported."
        )
        _bitly_complete(progress_run_id, "failed", error="no active Bitly connections")
        return False

    days = params.get("days")
    # Validate --days up front so a malformed value fails the run before the
    # fetch subprocess is launched (same value is re-resolved by the child).
    try:
        days = resolve_days(days)
    except (TypeError, ValueError):
        logging.error(f"bitly: invalid --days value {params.get('days')!r} — expected an integer")
        _bitly_complete(progress_run_id, "failed", error="invalid --days value")
        return False

    # Phase 3 — connector-global import lock (LOCK-001 pattern). Two
    # Bitly importers running concurrently against the same API risk
    # rate-limit exhaustion and duplicate JSON files; scope_id="" (global)
    # because the dispatch is a single unscoped run (report_schedule_service
    # reports lock_scope "" for bitly — keep the two in sync).
    lock_scope = ""
    lock_id = None
    try:
        from lib.import_lock_service import ImportLockService

        # progress_run_id: queue position + cancel while waiting for a global slot.
        lock_id = ImportLockService.acquire("bitly", scope_id=lock_scope, progress_run_id=progress_run_id)
        if lock_id is None:
            logging.warning("bitly lock: another bitly importer holds the lock; exiting cleanly.")
            # resolve our progress doc IFF it's a true orphan (the
            # lock winner is a DIFFERENT run that will never finalize it).
            if progress_run_id:
                try:
                    from lib.import_progress_service import ImportProgressService

                    ImportProgressService.resolve_orphaned_lock_loser(progress_run_id)
                except Exception as exc:  # noqa: BLE001
                    logging.warning("bitly lock: resolve_orphaned_lock_loser raised: %s", exc)
            return None
    except Exception as exc:  # noqa: BLE001 — boundary: a lock-service outage must not block the import
        logging.warning("bitly lock: acquire raised (continuing unlocked): %s", exc)
        lock_id = None

    logging.info(f"Starting Bitly import (last {days} day(s), {len(configs)} connection(s))")

    start_time = datetime.now()
    logging.info(f"Import started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # Define workflow steps
        steps = [
            {
                "name": "Fetch Bitly Link Clicks",
                "phase": "bitly_fetch",
                "command": [
                    "python3",
                    "/app/python/import.py",
                    "bitly",
                    "get_link_clicks",
                    f"--days={days}",
                ],
            },
            {
                "name": "Index to Elasticsearch",
                "phase": "bitly_index",
                "command": [
                    "python3",
                    "/app/python/import.py",
                    "bitly",
                    "send_json_to_elastic",
                ],
            },
            {
                "name": "Archive Processed Files",
                "phase": "bitly_archive",
                "command": [
                    "python3",
                    "/app/python/import.py",
                    "bitly",
                    "archive_imported_json",
                ],
            },
        ]

        # Execute workflow steps sequentially
        all_successful = True
        for i, step in enumerate(steps, start=1):
            # Announce the stage BEFORE running it so the bar shows the step
            # that's in flight (and its share of the 3-step progress).
            _bitly_set_phase(progress_run_id, step["phase"], i)
            success = run_subprocess(step["command"], step["name"])
            if not success:
                all_successful = False
                logging.error(f"Workflow stopped due to error in: {step['name']}")
                # Terminal failure stamp so the Run-now bar resolves to a
                # failed state instead of hanging.
                _bitly_complete(progress_run_id, "failed", error=f"step failed: {step['name']}")
                break

        if all_successful:
            _bitly_complete(progress_run_id, "succeeded")
    finally:
        # Release the lock so the next bitly importer can proceed.
        if lock_id:
            try:
                from lib.import_lock_service import ImportLockService

                ImportLockService.release("bitly", lock_id, scope_id=lock_scope)
            except Exception as exc:  # noqa: BLE001
                logging.warning("bitly lock release raised: %s", exc)

    # Final summary
    end_time = datetime.now()
    elapsed = end_time - start_time
    minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
    if all_successful:
        logging.info("✓ Bitly Daily Import Workflow COMPLETED")
    else:
        logging.error("✗ Bitly Daily Import Workflow FAILED")
    logging.info(f"Import ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} | Total time: {minutes}m {seconds}s")

    return all_successful


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Bitly link-clicks daily imports")
    parser.add_argument("--days", help="Trailing window of daily click points to pull (default 3, max 30)")
    parser.add_argument("--progress_run_id", help="de_import_progress doc id for Run-now progress")

    args = parser.parse_args()

    success = main({"days": args.days, "progress_run_id": args.progress_run_id})

    sys.exit(0 if success else 1)
