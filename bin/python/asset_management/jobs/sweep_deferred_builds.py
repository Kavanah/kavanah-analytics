#!/usr/bin/env python3
"""High-cadence deferred-build sweep.

A report-scheduler ``run_now`` returns immediately with ``phase=importing`` and
defers the snapshot build until the import lands. The build is finalized by
either the inline status poll or the cron sweep inside ``run_due``
. But the inline poll only fires while the builder tab is open and
polling — which it doesn't do during the importing phase — and
``run_due`` runs only every 5 minutes. So a finished ``run_now`` whose tab was
closed/backgrounded sticks on "Importing fresh data…" for up to 5 minutes (and
indefinitely on a host where the ``run_due`` cron is missing/stale).

This job runs JUST the deferred-build sweep on a fast cadence (``*/1`` minute,
see deployment/03_provision_cron_jobs.sh) so a finished run finalizes within
~1 minute regardless of the browser. It does NOT dispatch due schedules, so it
is safe to run far more often than ``run_due`` — there is no risk of
re-triggering imports.

Invoked from cron (deployment/03_provision_cron_jobs.sh):
    python3 /app/python/asset_management/jobs/sweep_deferred_builds.py

All the logic lives in ``ReportScheduleService.sweep_deferred_builds`` (best-
effort, per-schedule isolated, idempotent, race-safe with run_due's own sweep).
This thin runner parses args, calls it, logs the summary, and exits 0 so a
transient failure never wedges cron — mirrors run_report_schedules.py.
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, "/app/python")


def main() -> int:
    parser = argparse.ArgumentParser(description="Finalize deferred report-snapshot builds.")
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max schedules to scan in one sweep (default 500).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    try:
        from asset_management.web.services.report_schedule_service import (
            ReportScheduleService,
        )
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.error("sweep_deferred_builds: import failed: %s", exc)
        return 0  # never wedge cron

    summary = ReportScheduleService.sweep_deferred_builds(limit=args.limit)
    logging.info("sweep_deferred_builds: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
