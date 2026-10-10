"""self-reporting applied-vs-pending state for ES-data migrations.

The ``update_vN.py`` scripts in this directory mutate ES runtime state and are
run by hand at cutover; nothing recorded which ran in which environment, so a
deploy could ship the code while the migration silently never ran on prod
(``update_v16`` skipped on prod, Countries dropdown stale for six
weeks; runbook was never finalized, leaving v24/v25's prod state
unknowable). This tool makes the state self-reporting:

  status                  applied-vs-unrecorded table for THIS cluster; the
                          cluster is the environment, so no env flag exists.
      --check-pending     exit 1 if anything is not cleanly applied (runbook
                          gate: wire into Stage 0/2 of every cutover).
  run <vN>                run the migration as a subprocess and record a
                          marker with its exit code (re-run overwrites — the
                          scripts are idempotent by contract).
  mark <vN> --note "..."  record a migration as applied WITHOUT running it —
                          the honesty hatch for backfilling history (e.g.
                          "verified via insight_website_ctr_link_click field
                          probe era"). The note is mandatory.

Report-only by design: nothing here auto-applies migrations on deploy — prod
data mutation keeps its human gate (design decision, DECISIONS.md
2026-08-19). Markers live in ``de_meta_migrations``, one doc per migration
(_id = migration name), so re-runs update in place and the index can never
accumulate duplicates.

Run from /app/python inside imports_container:

    python3 asset_management/updates/migrations.py status
"""

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

UPDATES_DIR = os.path.dirname(os.path.abspath(__file__))
MARKER_INDEX = "de_meta_migrations"
_NAME_RE = re.compile(r"^update_v(\d+)\.py$")

MARKER_MAPPING = {
    "properties": {
        "migration": {"type": "keyword"},
        "applied_at": {"type": "date"},
        "exit_code": {"type": "integer"},
        "recorded_by": {"type": "keyword"},  # "run" | "mark"
        "note": {"type": "keyword", "ignore_above": 8191},
        "duration_s": {"type": "float"},
    }
}


def discover_migrations(directory: str = UPDATES_DIR) -> list[str]:
    """All update_vN module names in the directory, natural-sorted by N.

    Strict on the ``update_v<digits>.py`` shape — the directory also holds
    helper scripts (audit_ds_checkboxes.py, update_migrate_affinities_to_
    regions.py) and data files that are not versioned migrations.
    """
    found = []
    for entry in os.listdir(directory):
        m = _NAME_RE.match(entry)
        if m:
            found.append((int(m.group(1)), entry[: -len(".py")]))
    return [name for _, name in sorted(found)]


def normalize_name(raw: str) -> str:
    """Accept 'update_v27', 'v27' or '27' and return 'update_v27'."""
    m = re.fullmatch(r"(?:update_v|v)?(\d+)", raw)
    if not m:
        raise ValueError(f"not a migration name: {raw!r} (expected update_vN / vN / N)")
    return f"update_v{m.group(1)}"


def classify(names: list[str], markers: dict[str, dict]) -> list[tuple[str, str, dict | None]]:
    """Pure pairing of discovered migrations with their markers.

    States: ``applied`` (marker, exit 0), ``attempted`` (marker, nonzero exit
    — v25/v27-style partial conflicts land here until a human inspects and
    re-runs or marks), ``unrecorded`` (no marker: never ran here, or ran
    before tracking existed).
    """
    out = []
    for name in names:
        marker = markers.get(name)
        if marker is None:
            out.append((name, "unrecorded", None))
        elif marker.get("exit_code", 0) == 0:
            out.append((name, "applied", marker))
        else:
            out.append((name, "attempted", marker))
    return out


def ensure_marker_index(es) -> None:
    if not es.indices.exists(index=MARKER_INDEX):
        es.indices.create(index=MARKER_INDEX, body={"mappings": MARKER_MAPPING})


def fetch_markers(es) -> dict[str, dict]:
    if not es.indices.exists(index=MARKER_INDEX):
        return {}
    resp = es.search(index=MARKER_INDEX, body={"query": {"match_all": {}}, "size": 1000})
    return {hit["_id"]: hit["_source"] for hit in resp["hits"]["hits"]}


def record_marker(es, name: str, exit_code: int, recorded_by: str, note: str = "", duration_s: float | None = None) -> None:
    ensure_marker_index(es)
    doc = {
        "migration": name,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": exit_code,
        "recorded_by": recorded_by,
        "note": note,
    }
    if duration_s is not None:
        doc["duration_s"] = round(duration_s, 2)
    es.index(index=MARKER_INDEX, id=name, document=doc, refresh=True)


def run_migration(name: str, directory: str = UPDATES_DIR) -> tuple[int, float]:
    """Run update_vN.py as a subprocess (their own logging setup stays intact)
    and return (exit_code, duration_s)."""
    path = os.path.join(directory, f"{name}.py")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    started = time.monotonic()
    proc = subprocess.run([sys.executable, path], cwd=os.path.abspath(os.path.join(directory, "../..")))
    return proc.returncode, time.monotonic() - started


def cmd_status(es, check_pending: bool) -> int:
    rows = classify(discover_migrations(), fetch_markers(es))
    width = max(len(n) for n, _, _ in rows)
    pending = 0
    for name, state, marker in rows:
        if state == "applied":
            detail = f"APPLIED     {marker['applied_at'][:19]}  ({marker['recorded_by']}"
            detail += f": {marker['note']})" if marker.get("note") else ")"
        elif state == "attempted":
            pending += 1
            detail = (
                f"ATTEMPTED   exit={marker['exit_code']} {marker['applied_at'][:19]} — inspect "
                f"system_<date>.log, then re-run or mark"
            )
        else:
            pending += 1
            detail = "UNRECORDED  never ran here, or ran before tracking — verify, then run or mark"
        print(f"{name:<{width}}  {detail}")
    print(f"\n{len(rows)} migrations: {len(rows) - pending} applied, {pending} pending/unverified.")
    return 1 if (check_pending and pending) else 0


def cmd_run(es, raw_name: str) -> int:
    name = normalize_name(raw_name)
    exit_code, duration = run_migration(name)
    record_marker(es, name, exit_code, recorded_by="run", duration_s=duration)
    state = "applied" if exit_code == 0 else f"ATTEMPTED (exit {exit_code} — inspect system_<date>.log)"
    print(f"{name}: {state}, {duration:.1f}s, marker recorded.")
    return exit_code


def cmd_mark(es, raw_name: str, note: str) -> int:
    name = normalize_name(raw_name)
    if name not in discover_migrations():
        print(f"refusing to mark unknown migration {name}", file=sys.stderr)
        return 1
    record_marker(es, name, exit_code=0, recorded_by="mark", note=note)
    print(f"{name}: marked applied (not run). Note: {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_status = sub.add_parser("status", help="applied-vs-pending table for this cluster")
    p_status.add_argument("--check-pending", action="store_true", help="exit 1 if anything is not cleanly applied")
    p_run = sub.add_parser("run", help="run a migration and record its marker")
    p_run.add_argument("name")
    p_mark = sub.add_parser("mark", help="record a migration as applied without running it")
    p_mark.add_argument("name")
    p_mark.add_argument("--note", required=True, help="WHY it is known-applied (mandatory — this is the audit trail)")
    args = parser.parse_args(argv)

    es = get_elasticsearch_client()
    if es is None:
        print("No Elasticsearch client — aborting.", file=sys.stderr)
        return 3
    if args.command == "status":
        return cmd_status(es, args.check_pending)
    if args.command == "run":
        return cmd_run(es, args.name)
    return cmd_mark(es, args.name, args.note)


if __name__ == "__main__":
    sys.exit(main())
