"""Run a cron job and record its exit code in ``de_job_health``.

The root crontab lines provisioned by ``deployment/03_provision_cron_jobs.sh``
append each job's output to ``/var/log/<slug>.log`` and nothing else. When the
05:40 demo DT reseed exited 1 five days running (2026-09-02..07), the only
evidence was in that file, and it was found by a PR review reading the log.

This wrapper sits between ``docker exec`` and the job::

    python3 /app/python/tools/run_tracked_job.py --job kavanah_demo_dt_reseed -- \\
        python3 /app/python/scripts/seed_kavanah_demo_dt.py --reseed

It runs the command after ``--`` with stdout and stderr merged, streams every
line straight through (so the cron redirect still captures the full log),
remembers the LAST non-empty line as the run's summary, and when the child
exits writes one row — exit code, summary, timings — to ``de_job_health`` via
``lib.job_health.record_run``. The Status Reports page and the superuser banner
read that row the next morning.

Invariants:

* The wrapper's exit code IS the child's exit code. Recording is best-effort;
  an unreachable Elasticsearch, a failed write, or any bug in the recording path
  prints a warning and changes nothing about the exit code. An observer that
  masks the failure it observes is worse than no observer.
* The child's output is never buffered away: lines are written as they arrive.
* ``--job`` must be the cron log slug, so the row's id matches the log file
  name and ``lib.job_health.EXPECTED_DAILY_JOBS`` can name it.

No ``--apply``; the only thing this tool writes is the run row.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

_PY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

from lib import job_health  # noqa: E402
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

EXIT_USAGE = 2
EXIT_NOT_FOUND = 127


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _split(argv: list[str]) -> tuple[list[str], list[str]]:
    """Wrapper args before the first ``--``; the child's command after it."""
    if "--" not in argv:
        return argv, []
    i = argv.index("--")
    return argv[:i], argv[i + 1 :]


def run_child(command: list[str], out=None) -> tuple[int, str]:
    """Run ``command``, stream its merged output to ``out``, return (exit_code, last_line).

    A command that cannot be started (missing binary) reports 127 like a shell
    would, with the OS error as its summary, so the run row still says WHY.
    """
    out = out or sys.stdout
    last = ""
    try:
        proc = subprocess.Popen(  # noqa: S603 — the command comes from the provisioned cron line
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        msg = f"run_tracked_job: cannot start {command[0]!r}: {exc}"
        print(msg, file=out, flush=True)
        return EXIT_NOT_FOUND, msg
    assert proc.stdout is not None
    for line in proc.stdout:
        out.write(line)
        out.flush()
        if line.strip():
            last = line.rstrip("\r\n")
    code = proc.wait()
    if code < 0:
        # Killed by a signal: report it the way a shell would (128 + signum) so
        # the row is a positive, recognisable code rather than a negative int.
        code = 128 - code
    return code, last


def record(
    job: str, *, exit_code: int, summary: str, started_at: datetime, finished_at: datetime, command: str, out=None
) -> bool:
    """Best-effort write of the run row. Never raises; never influences the exit code."""
    out = out or sys.stdout
    try:
        es = get_elasticsearch_client()
        if es is None:
            print(f"run_tracked_job: WARNING — Elasticsearch unreachable; run of {job} NOT recorded", file=out, flush=True)
            return False
        ok = job_health.record_run(
            es,
            job=job,
            exit_code=exit_code,
            summary=summary,
            started_at=started_at,
            finished_at=finished_at,
            command=command,
        )
    except Exception as exc:  # noqa: BLE001 — recording is an observer; it must not change the outcome
        print(f"run_tracked_job: WARNING — recording {job} failed: {exc}", file=out, flush=True)
        return False
    if not ok:
        print(f"run_tracked_job: WARNING — run of {job} NOT recorded (see log above)", file=out, flush=True)
    return ok


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    argv = list(sys.argv[1:] if argv is None else argv)
    wrapper_args, command = _split(argv)
    parser = argparse.ArgumentParser(
        prog="run_tracked_job.py",
        description="Run a cron job and record its exit code in de_job_health.",
        usage="%(prog)s --job <cron-log-slug> -- <command> [args...]",
    )
    parser.add_argument("--job", required=True, help="The job's cron log slug (the row id in de_job_health).")
    try:
        args = parser.parse_args(wrapper_args)
    except SystemExit:
        return EXIT_USAGE
    if not command:
        parser.print_usage(file=sys.stderr)
        print("run_tracked_job: nothing to run — put the job's command after `--`", file=sys.stderr)
        return EXIT_USAGE

    started = _now()
    print(f"run_tracked_job: {args.job} starting at {started.isoformat()} — {' '.join(command)}", file=out, flush=True)
    code, summary = run_child(command, out=out)
    finished = _now()
    duration = (finished - started).total_seconds()
    print(
        f"run_tracked_job: {args.job} exit {code} after {duration:.1f}s — {summary or '(no output)'}",
        file=out,
        flush=True,
    )
    record(
        args.job,
        exit_code=code,
        summary=summary,
        started_at=started,
        finished_at=finished,
        command=" ".join(command),
        out=out,
    )
    return code


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
