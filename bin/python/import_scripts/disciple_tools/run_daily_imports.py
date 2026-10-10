"""Disciple.Tools pull orchestrator.

Action: ``python3 import.py disciple_tools run_daily_imports [--days_to_pull=N] [--team_ids=a,b]``

Runs the two-stage pull (fetch from each DT site → map + index into
``{team_esid}__disciple_tools``) in one process, matching the FB/Echo/GAds
``run_daily_imports`` entry-point contract that ImportService drives.
"""

from __future__ import annotations

import logging

import lib.logging
from import_scripts.disciple_tools import fetch, send_json_to_elastic


def main(params):
    lib.logging.define("dt-run-daily-imports")
    logging.info("DT import: starting (params=%s)", {k: v for k, v in (params or {}).items() if k != "token"})

    # require a team scope. The cron full-cluster path is retired
    # (imports run via the scheduler, which always passes --team_ids);
    # a scope-less invocation is a misconfiguration and a cluster-wide pull is a
    # cross-workspace exposure. Bail BEFORE fetch.
    from lib.import_scope import require_scope

    require_scope(params, scope_param="team_ids", connector_id="disciple_tools")

    fetch.main(params)
    send_json_to_elastic.main(params)

    lib.logging.define("dt-run-daily-imports")
    logging.info("DT import: complete")
