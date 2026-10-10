"""nightly snapshot of every team with TikTok linked.

    python3 /app/python/import.py tiktok run_all_teams

Why a cron job when every other importer runs through the Report Scheduler
: the Display API reports lifetime totals only, so a day without a
snapshot is gone for good — there is no history to backfill it from. Leaving the
daily run to a user-created schedule would lose days for every team that never
made one or paused it. This job is provisioned in
deployment/03_provision_cron_jobs.sh and wrapped in tools/run_tracked_job.py, so a
failed or missing night shows on Status Reports and in the superuser banner
(lib.job_health). the team's decision, 2026-09-26.

It resolves the team set and hands it to ``run_daily_imports`` in one run, so
each TikTok account is refreshed and fetched once however many teams share it.
Same-day re-runs overwrite that day's snapshot (deterministic doc ids).
"""

from __future__ import annotations

import logging

import lib.logging as custom_logging
from lib.tiktok_constants import TEAM_CONNECTIONS_FIELD, TEAM_FLAG_FIELD


def linked_team_esids() -> list[str]:
    """Teams with ``ds_tiktok`` on and at least one linked TikTok connection.

    The flag is matched on the string ``"true"`` the wizard writes (dynamic
    keyword on de_asset_teams). ES failures propagate: an outage must fail the
    job, not read as "no teams".
    """
    from lib.es_helpers import get_all_documents_paginated

    hits = get_all_documents_paginated(
        "de_asset_teams",
        {"bool": {"filter": [{"term": {TEAM_FLAG_FIELD: "true"}}, {"exists": {"field": TEAM_CONNECTIONS_FIELD}}]}},
    )
    return sorted(h["_id"] for h in hits if (h.get("_source") or {}).get(TEAM_CONNECTIONS_FIELD))


def main(params):
    custom_logging.define("tiktok-run-all-teams")
    team_esids = linked_team_esids()
    if not team_esids:
        # A cluster with no TikTok teams is a normal state, not a failure.
        logging.info("tiktok: no teams have TikTok linked — nothing to snapshot")
        return None
    logging.info("tiktok: nightly snapshot for %d team(s)", len(team_esids))

    from import_scripts.tiktok import run_daily_imports

    return run_daily_imports.main({"team_ids": ",".join(team_esids)})
