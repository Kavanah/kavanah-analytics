"""Kick off the credential-health sweep out-of-band.

``lib.credential_health.record_probe`` — the only writer of ``de_credential_health``
rows — had exactly one caller: the 6-hourly cron
``asset_management/jobs/probe_credential_health.py``. Every path that made a
re-auth token live then called ``credential_banner_service.invalidate_cache()``,
which dropped the cached banner rows and re-read the *same stale row*. A freshly
re-authed connection kept its red "needs reconnecting" banner for up to six
hours (prod, 2026-09-17: re-authed 11:30, banner still ``token_dead`` at 11:34,
cleared only by a hand-run sweep at 11:37).

Why the WHOLE sweep and not a one-credential probe: ``run_sweep`` applies
``apply_cross_credential_coverage()`` across every credential before recording
anything — an asset unreachable by this credential is not "lost" if another
credential imports it. A single-credential probe cannot compute that and would
not update the *other* rows whose verdict changed. The banner service's own
comment: "exactly one classifier and it runs at sweep time." Keep it that way.

Why detached: 27 credentials took 26s on prod. Same launch shape as
``import_service.ImportService.run_imports`` — ``Popen`` into its own session
with output discarded, so the request returns immediately and the job outlives
the worker. The job writes only to ``de_credential_health`` and guards against
overlapping itself with an ``flock`` (see the job's ``main``).

Failure to launch is logged and swallowed: the caller has already committed the
token, and health bookkeeping must never undo a successful re-auth.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

# The cron's own entry point, unchanged — this module only starts it early.
JOB_SCRIPT = "/app/python/asset_management/jobs/probe_credential_health.py"
JOB_CWD = "/app/python"

_log = logging.getLogger(__name__)


def trigger_sweep_async(*, reason: str = "") -> bool:
    """Start one detached credential-health sweep. Returns True if launched.

    ``reason`` is for the log line only (e.g. ``"reauth adopt 9a87…"``), so an
    operator reading the web log can tell why a sweep started between crons.
    """
    cmd = [sys.executable or "python3", JOB_SCRIPT]
    try:
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell, our own script
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=JOB_CWD,
            start_new_session=True,
            env={**os.environ},
        )
    except Exception as exc:  # noqa: BLE001 — never fail the caller on bookkeeping
        _log.warning("could not launch credential-health sweep (%s): %s", reason or "-", exc)
        return False
    _log.info("credential-health sweep launched (%s)", reason or "-")
    return True
