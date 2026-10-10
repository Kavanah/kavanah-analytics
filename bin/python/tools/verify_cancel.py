"""verify that cancelling a Report Scheduler run TRULY killed its import.

 made the scoped import cancellable: each dispatch runs in its own
process group (``start_new_session=True``) and ``cancel_run`` ``os.killpg``s that
group (SIGTERM -> grace -> SIGKILL -> settle -> verify), then stamps the schedule
terminal. The risk this guards against is a "cancel" that only hides the UI bar
while the paged FB/GAds/GA4/Echo API pull keeps running (and holding the import
lock). So the real test is OS ground-truth: are the recorded import PIDs actually
gone after a cancel?

This tool answers that. It does NOT cancel anything itself — you cancel from the
UI (or the API) so the test exercises the real route. The tool:

1. Reads the schedule's ``last_run_state.import_pids`` (the ``{pid, pgid}`` pairs
   ``_execute_one`` stamps on dispatch) and confirms the group is ALIVE now, so
   the test isn't vacuous.
2. Waits for you to click Cancel (``--watch``), polling until the process group
   dies or a timeout.
3. Asserts the group is truly dead AND the persisted run-state resolved cleanly
   (``phase=cancelled``, ``import_pids=[]``, no ``error``).

Ground-truth liveness reuses ``ReportScheduleService._pgid_alive`` verbatim — the
SAME zombie-aware ``/proc`` scan the service uses — so this verifier can never
drift from the code under test. (A SIGKILL'd leader becomes a zombie whose pgid
still answers ``killpg(.,0)``; only a non-zombie member keeps the group alive.)

Exit code 0 = cancel verified (process dead + state clean). Non-zero = the import
survived the cancel, or the state is inconsistent — a real failure.

Usage::

    # List schedules that currently have live import pids (find one to test):
    docker exec -w /app/python imports_container python3 \\
        -m tools.verify_cancel --list

    # Watch a specific schedule: confirm alive, wait for your UI Cancel, verify death:
    docker exec -w /app/python imports_container python3 \\
        -m tools.verify_cancel --schedule-id <ID> --watch

    # One-shot check AFTER you've already cancelled (no waiting):
    docker exec -w /app/python imports_container python3 \\
        -m tools.verify_cancel --schedule-id <ID>
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# bin/python on the path (mirrors the other tools/ entrypoints).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asset_management.web.services.report_schedule_service import (  # noqa: E402
    INDEX_REPORT_SCHEDULES,
    ReportScheduleService,
)
from lib.es_helpers import get_elasticsearch_client  # noqa: E402


def _run_state(es, schedule_id: str) -> dict:
    """The schedule's last_run_state, or {} if the schedule/field is absent."""
    if not es.exists(index=INDEX_REPORT_SCHEDULES, id=schedule_id):
        raise SystemExit(f"FAIL: schedule {schedule_id!r} not found in {INDEX_REPORT_SCHEDULES}")
    src = es.get(index=INDEX_REPORT_SCHEDULES, id=schedule_id)["_source"]
    return src.get("last_run_state") or {}


def _pgids(run_state: dict) -> list[int]:
    """The distinct, valid process-group ids recorded on the run."""
    return sorted({int(p["pgid"]) for p in (run_state.get("import_pids") or []) if p and p.get("pgid")})


def _any_alive(pgids: list[int]) -> bool:
    """True iff ANY recorded group still has a non-zombie member (service's own check)."""
    return any(ReportScheduleService._pgid_alive(p) for p in pgids)


def cmd_list(es) -> int:
    """Print every schedule that currently has live import pids stamped.

    NOTE: ``last_run_state`` is mapped ``{type: object, enabled: false}`` — stored
    in _source but NOT indexed — so an ``exists`` query on
    ``last_run_state.import_pids`` matches nothing even when pids are present. We
    must therefore fetch the docs (match_all) and inspect ``import_pids`` from
    _source in Python, not push the filter into ES.
    """
    r = es.search(index=INDEX_REPORT_SCHEDULES, size=500, query={"match_all": {}})
    found = 0
    for h in r["hits"]["hits"]:
        rs = h["_source"].get("last_run_state") or {}
        pgids = _pgids(rs)
        if not pgids:
            continue
        found += 1
        alive = "ALIVE" if _any_alive(pgids) else "dead/none"
        print(f"  {h['_id']:40} phase={str(rs.get('phase')):12} pgids={pgids} [{alive}]")
    if not found:
        print("  (no schedule currently has live import_pids — start a Run-now and list WHILE it imports)")
    return 0


def _assert_state_clean(run_state: dict) -> list[str]:
    """Return a list of state problems (empty list == clean cancel)."""
    problems = []
    if run_state.get("phase") != "cancelled":
        problems.append(f"phase is {run_state.get('phase')!r}, expected 'cancelled'")
    if run_state.get("import_pids"):
        problems.append(f"import_pids not cleared: {run_state.get('import_pids')!r}")
    if run_state.get("error"):
        problems.append(f"run-state carries an error: {run_state.get('error')!r}")
    return problems


def cmd_verify(es, schedule_id: str, *, watch: bool, timeout: float) -> int:
    rs = _run_state(es, schedule_id)
    pgids = _pgids(rs)

    if not pgids:
        # No recorded pids. Either the run never dispatched an import, or it already
        # finished terminal and cleared them. Distinguish so the result isn't
        # misread as a pass.
        print(f"No import_pids on {schedule_id!r} (phase={rs.get('phase')!r}).")
        print("Nothing to kill — start a Run-now that dispatches an import, then re-run with --watch.")
        return 2

    print(f"Recorded process groups: {pgids}")

    if watch:
        if not _any_alive(pgids):
            print("FAIL: the import group is already dead BEFORE you cancelled — test is vacuous.")
            print("Start a fresh Run-now (so the import is genuinely mid-flight), then --watch again.")
            return 3
        print(f"Import group ALIVE. Click Cancel in the UI now — polling up to {timeout:.0f}s for it to die...")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _any_alive(pgids):
                break
            time.sleep(0.5)
        else:
            print(f"*** FAIL: import group {pgids} STILL ALIVE after {timeout:.0f}s — cancel did NOT kill it. ***")
            return 1
        # Re-read state after the kill so the service has stamped terminal.
        time.sleep(0.5)
        rs = _run_state(es, schedule_id)

    # Ground-truth: the OS no longer has a live (non-zombie) member of the group.
    if _any_alive(pgids):
        print(f"*** FAIL: import group {pgids} is STILL ALIVE — the import survived the cancel. ***")
        return 1
    print(f"OK: process group {pgids} is dead (gone or zombie) — the import was killed.")

    # Cross-check the persisted run-state agrees (catches a kill that reaped the
    # process but left the schedule/lock/progress doc inconsistent).
    problems = _assert_state_clean(rs)
    if problems:
        print("*** FAIL: process is dead but the run-state is inconsistent:")
        for p in problems:
            print(f"    - {p}")
        print("    (a dead process with a dirty state means the cleanup path didn't finish)")
        return 1

    print("OK: run-state resolved cleanly (phase=cancelled, import_pids=[], no error).")
    print("PASS:  cancel verified — the import was truly cancelled.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a  cancel truly killed its scoped import.")
    ap.add_argument("--schedule-id", help="schedule whose in-flight run you are cancelling")
    ap.add_argument("--list", action="store_true", help="list schedules with live import_pids and exit")
    ap.add_argument(
        "--watch",
        action="store_true",
        help="confirm the group is alive, wait for your UI Cancel, then verify death",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="seconds to wait for the group to die after you cancel (default 30)",
    )
    args = ap.parse_args()

    es = get_elasticsearch_client()

    if args.list:
        return cmd_list(es)
    if not args.schedule_id:
        ap.error("--schedule-id is required (or use --list to find one)")
    return cmd_verify(es, args.schedule_id, watch=args.watch, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
