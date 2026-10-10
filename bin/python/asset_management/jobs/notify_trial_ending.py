#!/usr/bin/env python3
"""Daily cron job — email trialing users whose free trial ends soon.

 follow-up (2026-06-05). The "trial ending soon" billing email can't
come from a Stripe webhook (Stripe has no "trial day N" event), so a daily
scan finds users within the warning window and emails each once, guarded by
the ``trial_ending_email_sent_at`` metadata stamp.

Invoked from cron (deployment/03_provision_cron_jobs.sh):
    python3 /app/python/asset_management/jobs/notify_trial_ending.py [--days N]

All the logic lives in ``SubscriptionStateService.scan_and_notify_trial_ending``
(best-effort, never raises); this thin runner just parses args, calls it, and
exits 0 so a transient failure never wedges cron.
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, "/app/python")


def main() -> int:
    parser = argparse.ArgumentParser(description="Email trialing users whose trial ends soon.")
    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Warn users whose trial ends within this many days (default 3).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    try:
        from asset_management.web.services.subscription_state_service import (
            SubscriptionStateService,
        )
        from asset_management.web.services.workspace_billing_service import (
            WorkspaceBillingService,
        )
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.error("notify_trial_ending: import failed: %s", exc)
        return 0  # never wedge cron

    # workspace-billing Phase 3e: scan WORKSPACES (the new billing
    # home) AND the legacy per-user records (for trials not yet migrated to a
    # workspace record). Both are best-effort + idempotent (once-per stamp).
    ws_summary = WorkspaceBillingService.scan_and_notify_trial_ending(warn_within_days=args.days)
    logging.info("notify_trial_ending (workspace): %s", ws_summary)
    user_summary = SubscriptionStateService.scan_and_notify_trial_ending(warn_within_days=args.days)
    logging.info("notify_trial_ending (per-user): %s", user_summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
