#!/usr/bin/env python3
"""Poll-runner cron job — execute due report schedules.

Option A execution model (user decision 2026-06-09): a host-cron job polls every
5 minutes for active schedules whose ``next_run`` has passed, executes each into
a ``de_report_snapshots`` doc, and advances ``last_run``/``next_run``. Schedule
state lives in ES, so the runner is stateless and survives container restarts.

Invoked from cron (deployment/03_provision_cron_jobs.sh):
    python3 /app/python/asset_management/jobs/run_report_schedules.py

All the logic lives in ``ReportScheduleService.run_due`` (best-effort,
per-schedule isolated). This thin runner parses args, calls it, logs the batch
summary, and exits 0 so a transient failure never wedges cron — mirrors
notify_trial_ending.py.
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, "/app/python")


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute due report schedules.")
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max schedules to execute in one poll (default 500).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    try:
        from asset_management.web.services.report_schedule_service import (
            ReportScheduleService,
        )
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.error("run_report_schedules: import failed: %s", exc)
        return 0  # never wedge cron

    summary = ReportScheduleService.run_due(limit=args.limit)
    logging.info("run_report_schedules: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
