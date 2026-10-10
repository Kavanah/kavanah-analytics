"""TikTok organic daily import orchestrator.

    python3 /app/python/import.py tiktok run_daily_imports --team_ids=a,b [--progress_run_id=<id>]

Three steps, each its own process (the YouTube / Bitly shape): fetch the
snapshots, index them, archive the staged files. The steps share a run name so
each touches only this run's files. There is no date window: the Display API
reports lifetime counts as of now, so every run is today's snapshot.

returns False on failure so import.py exits non-zero and the
Report Scheduler records the run as failed.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import lib.logging as custom_logging

# Keep in sync with report_schedule_service._progress_total (tiktok shares the
# 3-step _YOUTUBE_PROGRESS_STEPS denominator).
_TIKTOK_STEPS_TOTAL = 3


def _set_phase(run_id, phase, step_n):
    if not run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.set_phase(run_id=run_id, phase=phase)
        ImportProgressService.update_progress(run_id=run_id, days_fetched=step_n)
    except Exception as exc:  # noqa: BLE001 — progress is a UX nicety; never fail the import on it
        logging.warning("tiktok progress write raised: %s", exc)


def _complete(run_id, status, error=None):
    if not run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.complete_run(run_id=run_id, status=status, error=error)
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("tiktok complete_run(%s) raised: %s", status, exc)


def _run_step(command: list[str], name: str) -> bool:
    logging.info("Step: %s — %s", name, " ".join(command))
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logging.error("✗ %s failed with exit code %s", name, e.returncode)
        if e.stdout:
            logging.error("STDOUT:\n%s", e.stdout)
        if e.stderr:
            logging.error("STDERR:\n%s", e.stderr)
        return False
    if result.stdout:
        logging.info(result.stdout)
    logging.info("✓ %s completed", name)
    return True


def main(params):
    custom_logging.define("tiktok-run-daily-imports")

    from lib.import_scope import require_scope
    from import_scripts.tiktok.run_files import new_run_name

    progress_run_id = params.get("progress_run_id") or None
    try:
        team_ids = require_scope(params, scope_param="team_ids", connector_id="tiktok")
    except ValueError:
        _complete(progress_run_id, "failed", error="no team scope")
        raise

    run_name = new_run_name()
    base = ["python3", "/app/python/import.py", "tiktok"]
    fetch = base + ["get_organic_stats", f"--team_ids={team_ids}", f"--run_name={run_name}"]
    if progress_run_id:
        # The fetch step records per-account errors against this run.
        fetch.append(f"--progress_run_id={progress_run_id}")
    steps = [
        ("Fetch TikTok snapshots", "tiktok_fetch", fetch),
        ("Index to Elasticsearch", "tiktok_index", base + ["send_json_to_elastic", f"--run_name={run_name}"]),
        ("Archive staged files", "tiktok_archive", base + ["archive_imported_json", f"--run_name={run_name}"]),
    ]

    logging.info("TikTok organic import: teams=%s run=%s", team_ids, run_name)
    for step_n, (name, phase, command) in enumerate(steps, start=1):
        _set_phase(progress_run_id, phase, step_n)
        if not _run_step(command, name):
            _complete(progress_run_id, "failed", error=f"step failed: {name}")
            logging.error("✗ TikTok import FAILED at: %s", name)
            return False

    _complete(progress_run_id, "succeeded")
    logging.info("✓ TikTok import COMPLETED")
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the TikTok organic import")
    parser.add_argument("--team_ids", help="Comma-separated team ESIDs")
    parser.add_argument("--progress_run_id", help="de_import_progress doc id")
    args = parser.parse_args()
    sys.exit(0 if main({"team_ids": args.team_ids, "progress_run_id": args.progress_run_id}) else 1)
