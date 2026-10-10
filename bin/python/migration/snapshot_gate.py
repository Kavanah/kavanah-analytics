"""the shared snapshot-or-refuse gate for destructive migrations.

Seven migration scripts hand-rolled this contract (dry-run by default,
``--apply`` to mutate, snapshot-or-refuse before anything is permanently
deleted), and the copies drifted: the name-collision fix never
propagated, three copies still parsed ``--apply`` with raw truthiness (so
``--apply=false`` ran write mode — the review's headline finding),
and none but the copy checked the snapshot's ``state`` or carried a
``request_timeout``. This module is the single implementation; the scripts
call it.

The contract (hardened per the review +):

* ``--apply`` must be an EXPLICIT true value — import.py delivers
  ``--apply=false`` as the truthy string "false"; anything not recognized as
  true is ignored with a warning. ``--apply`` combined with an explicit
  truthy ``--dry-run`` refuses.
* Fresh snapshots get a TIMESTAMPED name (a fixed name hard-fails every
  retry — the staging incident, 2026-06-29), a real
  ``request_timeout`` (the default ~10s client timeout cannot survive a
  prod-sized ``wait_for_completion=True`` snapshot), and a ``state ==
  SUCCESS`` check (a PARTIAL snapshot returns 200 and is not a restore
  point).
* A recent prefix-matching SUCCESS snapshot COVERING the indices is reused
  instead of re-snapshotting on retry (the other half of the fix).
* A pre-existing named snapshot is accepted only when it actually COVERS the
  indices about to be deleted and finished SUCCESS — existence alone was the
  gate's old blind spot.
* Refusals raise :class:`MigrationRefused`; callers log and ``return 1`` so
  import.py exits non-zero (the a refused destructive
  migration must never record as green).

Callers own everything else: locks, the mutation loop, reporting.
"""

import logging
import time

_FALSY = ("false", "0", "no")
_TRUTHY = ("true", "1", "yes")

# A prefix-matching snapshot younger than this is reused on retry instead of
# re-snapshotting the same indices minutes apart. Old snapshots are NOT
# reused — data written since would be missing from the restore point.
REUSE_WITHIN_HOURS = 24

SNAPSHOT_REQUEST_TIMEOUT = 3600


class MigrationRefused(Exception):
    """A deliberate refusal (bad flags, no restore point) — not a crash.

    Callers catch this at their entry point, log the message, and return 1.
    """


def resolve_apply_mode(params: dict) -> tuple[bool, str | None]:
    """Parse the shared ``--dry-run`` / ``--apply`` / ``--snapshot-repo``
    contract from an import.py params dict.

    Returns ``(dry_run, snapshot_repo)``. Raises :class:`MigrationRefused`
    on the contradictory ``--apply`` + explicit truthy ``--dry-run``.
    """
    dry_run_param = params.get("dry-run", params.get("dry_run"))
    dry_run = dry_run_param is None or str(dry_run_param).lower() not in _FALSY
    apply_param = params.get("apply")
    apply_flag = apply_param is not None and str(apply_param).lower() in _TRUTHY
    if apply_param is not None and not apply_flag:
        logging.warning("Ignoring --apply=%r (not a recognized true value); staying in dry-run.", apply_param)
    if apply_flag and dry_run_param is not None and dry_run:
        raise MigrationRefused("--apply combined with an explicit truthy --dry-run is contradictory; drop one flag.")
    if apply_flag:
        dry_run = False
    snapshot_repo = params.get("snapshot-repo") or params.get("snapshot_repo") or None
    return dry_run, snapshot_repo


def _snapshot_body(resp) -> dict:
    return resp.body if hasattr(resp, "body") else resp


def snapshot_covers(snap: dict, indices: list[str]) -> bool:
    """True iff a snapshot-info dict is a usable restore point for
    ``indices``: finished SUCCESS and containing every one of them."""
    if (snap or {}).get("state") != "SUCCESS":
        return False
    return set(indices) <= set(snap.get("indices") or ())


def _find_reusable(es, repo: str, name_prefix: str, indices: list[str]) -> str | None:
    """Newest prefix-matching SUCCESS snapshot covering ``indices`` and
    younger than REUSE_WITHIN_HOURS, or None. Any lookup failure means
    'take a fresh one' — timestamped names cannot collide, so this is
    safe to get wrong in the None direction only."""
    try:
        resp = es.snapshot.get(repository=repo, snapshot=f"{name_prefix}*", ignore_unavailable=True)
        snaps = _snapshot_body(resp).get("snapshots") or []
        cutoff_ms = (time.time() - REUSE_WITHIN_HOURS * 3600) * 1000
        candidates = [s for s in snaps if snapshot_covers(s, indices) and (s.get("start_time_in_millis") or 0) >= cutoff_ms]
        if not candidates:
            return None
        best = max(candidates, key=lambda s: s.get("start_time_in_millis") or 0)
        return str(best.get("snapshot") or "") or None
    except (
        Exception
    ) as e:  # noqa: BLE001 — reuse is an optimization, never a gate; oddly-shaped responses (test fakes included) mean "take a fresh one"
        logging.info("snapshot_gate: reuse lookup failed (%s); taking a fresh snapshot.", e)
        return None


def take_snapshot(es, repo: str, indices: list[str], *, name_prefix: str) -> str:
    """Secure a fresh (or recently-taken reusable) snapshot of ``indices``.

    Returns the snapshot name. Raises :class:`MigrationRefused` when the
    snapshot cannot be taken or finishes in a non-SUCCESS state.
    """
    reusable = _find_reusable(es, repo, name_prefix, indices)
    if reusable:
        logging.info(
            "Reusing recent snapshot %s/%s (covers all %d index(es), < %dh old).",
            repo,
            reusable,
            len(indices),
            REUSE_WITHIN_HOURS,
        )
        return reusable

    name = f"{name_prefix}-{int(time.time())}"
    try:
        resp = es.options(request_timeout=SNAPSHOT_REQUEST_TIMEOUT).snapshot.create(
            repository=repo,
            snapshot=name,
            body={"indices": ",".join(indices), "include_global_state": False},
            wait_for_completion=True,
        )
    except Exception as e:  # noqa: BLE001 — refusal boundary
        raise MigrationRefused(f"snapshot {repo}/{name} failed: {e}") from e
    state = (_snapshot_body(resp).get("snapshot") or {}).get("state")
    if state != "SUCCESS":
        raise MigrationRefused(f"snapshot {repo}/{name} finished in state {state!r} — not a usable restore point")
    logging.info("Snapshot %s/%s created for %d index(es).", repo, name, len(indices))
    return name


def ensure_restore_point(
    es,
    indices: list[str],
    *,
    snapshot_repo: str | None,
    name_prefix: str,
    presnapshot_repo: str | None = None,
    presnapshot_name: str | None = None,
) -> str:
    """Refuse-or-secure a restore point before a run that permanently
    deletes ``indices``.

    Resolution order:

    1. ``indices`` empty — idempotent re-run, nothing to delete: pass.
    2. ``snapshot_repo`` given — take (or reuse) a fresh snapshot there.
    3. A configured pre-snapshot that COVERS the indices and finished
       SUCCESS — accept it. Mere existence is not enough: an index created
       after the pre-snapshot era would otherwise be deleted with its data
       absent from the restore point.
    4. Otherwise refuse.

    Returns a human-readable description of the restore point secured.
    Raises :class:`MigrationRefused` on refusal.
    """
    if not indices:
        return "nothing to delete — no restore point required"

    if snapshot_repo:
        name = take_snapshot(es, snapshot_repo, indices, name_prefix=name_prefix)
        return f"fresh snapshot {snapshot_repo}/{name}"

    if presnapshot_repo and presnapshot_name:
        try:
            resp = es.snapshot.get(repository=presnapshot_repo, snapshot=presnapshot_name)
            snaps = _snapshot_body(resp).get("snapshots") or []
        except Exception:  # noqa: BLE001 — missing repo/snapshot -> refuse below
            snaps = []
        if snaps and snapshot_covers(snaps[0], indices):
            logging.info(
                "Pre-snapshot %s/%s present and covers all %d index(es) — proceeding.",
                presnapshot_repo,
                presnapshot_name,
                len(indices),
            )
            return f"pre-snapshot {presnapshot_repo}/{presnapshot_name}"
        if snaps:
            raise MigrationRefused(
                f"pre-snapshot {presnapshot_repo}/{presnapshot_name} exists but does not cover all "
                f"{len(indices)} index(es) (or is not state SUCCESS). Pass --snapshot-repo to take a fresh one."
            )

    raise MigrationRefused(
        f"no restore point: no pre-snapshot available and no --snapshot-repo given. "
        f"Take a snapshot first — this run permanently deletes {len(indices)} index(es)."
    )


def finalize_report(report: dict, *, dry_run: bool, errors_key: str = "errors") -> dict | int:
    """Exit-value discipline for a migration ``main()`` returning a report.

    import.py's failure check is deliberately narrow: only an
    explicit ``False`` or a non-zero int counts as failure — a report DICT
    always maps to exit 0, so an apply run whose every step landed in
    ``report["errors"]`` was recorded green (found in the
    review; the same gap was live in sibling migrations). Return the
    report unchanged on a clean or dry run; return 1 when an apply run
    recorded errors, so the refusal survives the exit-code boundary.

    A DRY run that recorded errors still returns the report (exit 0): the
    dry run is a preview, not a pre-flight, so "dry-run, then apply if exit
    0" cannot be scripted for the mapping-conflict case — read the report.
    """
    errors = report.get(errors_key) or []
    if errors and not dry_run:
        logging.error(
            "Apply run recorded %d error(s) — exiting 1 so the run is not recorded green.",
            len(errors),
        )
        return 1
    return report
