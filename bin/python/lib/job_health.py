"""Durable run state for DEAM's tracked cron jobs.

The 05:40 demo Disciple Tools reseed returned 1 every day from 2026-09-02 to
2026-09-07 (2165 of 2625 docs rejected by the mapping) and nobody saw it for
five days. The exit code was correct; it went nowhere: the root cron line only
appends the job's output to ``/var/log/kavanah_demo_dt_reseed.log``, and a log
file is read only once someone already suspects a problem.

This module gives every tracked job ONE document in ``de_job_health``, keyed by
the job's cron log slug and written by ``tools/run_tracked_job.py`` after each
run: the exit code, the last line the job printed, and when it started and
finished. Two readers turn that into something a person sees the next morning:

* the Status Reports page lists every tracked job with its last exit code
  (``data_quality_service._demo_job_runs``);
* superusers get a earlier banner on every page while a job is
  ``failed`` or ``stale`` (``credential_banner_service``).

It is the sweep heartbeat made generic. A job that silently stops being
scheduled is as invisible as one that fails, so :func:`classify_runs` reports
``stale`` (last finish older than ``DEFAULT_MAX_AGE_HOURS``) and ``never_ran``
(an expected job with no row at all) alongside ``ok`` and ``failed``.
``never_ran`` is a page-level fact, not an alert: on a cluster where the tracked
cron lines were never provisioned it would otherwise page superusers forever.

Self-clearing: the next successful run overwrites the row, so a fixed job drops
off the banner with no dismissal state to go stale.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from collections.abc import Callable

from lib.es_helpers import create_index_if_not_exists, get_elasticsearch_client
from lib.tiktok_constants import tiktok_enabled

INDEX = "de_job_health"

# Cron log slugs (deployment/03_provision_cron_jobs.sh SCHEDULE) that run daily
# through tools/run_tracked_job.py. A slug listed here with no row in the index
# is reported as ``never_ran`` so a missing cron line is visible, not silent.
# Add the demo clone job (kavanah_demo_clone) here when its cron line lands.
# These are expected on every deployment; see SWITCHED_DAILY_JOBS for the rest.
# youtube_duplicate_check: the daily duplicate-channel audit.
EXPECTED_DAILY_JOBS: tuple[str, ...] = ("kavanah_demo_data", "kavanah_demo_dt_reseed", "youtube_duplicate_check")

# Daily jobs that exist only while a deployment switch is on. The cron
# provisioner installs the line only when the switch is on, so the job is
# expected only then. While the switch is off its row is also dropped from
# list_runs: a host that ran the job and was then switched off must not carry a
# stale banner for a connector it no longer has.
# tiktok_daily_snapshot: a missed TikTok night can never be recovered.
SWITCHED_DAILY_JOBS: dict[str, Callable[[], bool]] = {
    "tiktok_daily_snapshot": tiktok_enabled,
}


def expected_daily_jobs() -> tuple[str, ...]:
    """The daily jobs this deployment should be running right now."""
    return EXPECTED_DAILY_JOBS + tuple(job for job, switch in SWITCHED_DAILY_JOBS.items() if switch())


def switched_off_jobs() -> frozenset[str]:
    """Jobs whose switch is off here; their rows are not reported at all."""
    return frozenset(job for job, switch in SWITCHED_DAILY_JOBS.items() if not switch())


# Daily jobs; 30h is one missed run plus slack for a slow night — the same
# threshold the credential-health heartbeat uses (probe_credential_health.py).
DEFAULT_MAX_AGE_HOURS = 30

STATE_OK = "ok"
STATE_FAILED = "failed"
STATE_STALE = "stale"
STATE_NEVER_RAN = "never_ran"

# States that earn a superuser banner. ``never_ran`` is deliberately absent —
# see the module docstring.
PROBLEM_STATES: tuple[str, ...] = (STATE_FAILED, STATE_STALE)

MAPPING = {
    "properties": {
        "job": {"type": "keyword"},
        "exit_code": {"type": "integer"},
        "ok": {"type": "boolean"},
        # The last non-empty line the job printed — "✓ Seed complete — all 5
        # totals match." or "FAILED: 2165/2625 ...". text, not keyword: it is
        # shown, never aggregated.
        "summary": {"type": "text"},
        # The command as the wrapper ran it. Cron commands carry no secrets
        # (credentials come from the container environment); do not route a
        # command that takes a secret argument through the wrapper.
        "command": {"type": "text"},
        "started_at": {"type": "date"},
        "finished_at": {"type": "date"},
        "duration_seconds": {"type": "float"},
        "updated_at": {"type": "date"},
    }
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def record_run(
    es,
    *,
    job: str,
    exit_code: int,
    summary: str,
    started_at: datetime,
    finished_at: datetime,
    command: str = "",
) -> bool:
    """Upsert the run row for ``job``. Returns False (and logs) on any ES error.

    One row per job, id == job: the row is the job's CURRENT state, not a
    history — the log file keeps the history. Fail-soft on purpose: recording is
    an observer, and an observer that can throw changes what it observes (the
    wrapper must still exit with the child's code).
    """
    doc = {
        "job": job,
        "exit_code": int(exit_code),
        "ok": int(exit_code) == 0,
        "summary": (summary or "")[:500],
        "command": command or "",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(max(0.0, (finished_at - started_at).total_seconds()), 3),
        "updated_at": _now().isoformat(),
    }
    try:
        if not create_index_if_not_exists(es, INDEX, MAPPING):
            logging.error("job_health: could not ensure %s exists", INDEX)
            return False
        es.index(index=INDEX, id=job, body=doc, refresh="wait_for")
    except Exception as exc:  # noqa: BLE001 — an observer must never raise into the job it observes
        logging.error("job_health: could not record run of %s: %s", job, exc)
        return False
    return True


def list_runs(es) -> list[dict[str, Any]]:
    """Every job row, sorted by job. Empty list when the index is absent or unreadable.

    Rows of switched-off jobs (see SWITCHED_DAILY_JOBS) are left out.
    """
    try:
        if not es.indices.exists(index=INDEX):
            return []
        res = es.search(index=INDEX, body={"size": 200, "query": {"match_all": {}}})
    except Exception as exc:  # noqa: BLE001 — a status surface degrades to "nothing recorded", never 500s
        logging.warning("job_health: list_runs could not read %s: %s", INDEX, exc)
        return []
    off = switched_off_jobs()
    rows = [
        h["_source"]
        for h in res.get("hits", {}).get("hits", [])
        if isinstance(h.get("_source"), dict) and h["_source"].get("job") not in off
    ]
    return sorted(rows, key=lambda r: r.get("job") or "")


def classify_runs(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    expected: tuple[str, ...] | None = None,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> list[dict[str, Any]]:
    """Pure: one classified entry per job (rows ∪ expected), sorted by job.

    ``expected`` defaults to :func:`expected_daily_jobs` — the one input read from
    the environment (the switches); pass it explicitly to stay pure.

    ``stale`` outranks ``failed``: a job whose last run failed AND has not run
    since is first of all not running, and the exit code is carried on the entry
    so the reader sees both.
    """
    now = now or _now()
    if expected is None:
        expected = expected_daily_jobs()
    by_job = {r.get("job") or "": r for r in rows if r.get("job")}
    out: list[dict[str, Any]] = []
    for job in sorted(set(by_job) | set(expected)):
        row = by_job.get(job)
        if row is None:
            out.append(
                {
                    "job": job,
                    "state": STATE_NEVER_RAN,
                    "exit_code": None,
                    "summary": "",
                    "started_at": "",
                    "finished_at": "",
                    "age_hours": None,
                    "duration_seconds": None,
                    "expected": True,
                }
            )
            continue
        finished = _parse_ts(row.get("finished_at"))
        age_hours = round((now - finished).total_seconds() / 3600, 1) if finished else None
        exit_code = row.get("exit_code")
        if finished is None or age_hours is None or age_hours > max_age_hours:
            state = STATE_STALE
        elif exit_code != 0:
            state = STATE_FAILED
        else:
            state = STATE_OK
        out.append(
            {
                "job": job,
                "state": state,
                "exit_code": exit_code,
                "summary": row.get("summary") or "",
                "started_at": row.get("started_at") or "",
                "finished_at": row.get("finished_at") or "",
                "age_hours": age_hours,
                "duration_seconds": row.get("duration_seconds"),
                "expected": job in expected,
            }
        )
    return out


def problems(classified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The banner-worthy subset of :func:`classify_runs` output."""
    return [c for c in classified if c.get("state") in PROBLEM_STATES]


def list_problems(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """Convenience for the banner service: connect, list, classify, filter."""
    es = get_elasticsearch_client()
    if es is None:
        return []
    return problems(classify_runs(list_runs(es), now=now))
