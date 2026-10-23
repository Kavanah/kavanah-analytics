"""Global import slots — a cluster-wide cap on concurrent importers.

Why
---

The per-scope import locks only
stop DUPLICATE runs of the same connector scope. Nothing capped the TOTAL
number of importer processes running at once, so a burst of schedules (a
webinar intake, the morning scheduler sweep) could start one importer per
team and starve the box. measured ~4 concurrent imports as the safe
ceiling on 2 vCPU; prod is now 4 vCPU / 16 GB and the operator sets the cap
(``IMPORT_GLOBAL_CAP``, 8 there).

``ImportLockService.acquire`` is the single choke point every locked importer
passes through (the manual UI via ``lib.import_runner``, the scheduler's direct
Popen, the credential-health job), so it calls :func:`acquire_slot` right after
it wins its per-scope lock and :func:`release_slot` from ``release``.

Configuration (env, read on every call)
---------------------------------------

``IMPORT_GLOBAL_CAP``
    Max concurrent importer processes. Unset / ``0`` / not an int = the feature
    is OFF: :func:`acquire_slot` returns True and touches nothing (no files, no
    ES writes). That is the rollback path.
``IMPORT_QUEUE_MAX_WAIT_SECONDS``
    How long a run may wait for a slot before it is failed. Default 43200 (12 h).
``IMPORT_QUEUE_POLL_SECONDS``
    Re-check interval while queued. Default 5.
``IMPORT_SLOT_DIR``
    Where the slot / admission / ticket files live. Default
    ``/app/data/import_slots`` (the imports_container data volume, the same
    ``/app/data`` lib.import_runner and lib.logging use), falling back to
    ``<tempdir>/deam-import-slots`` when ``/app/data`` doesn't exist (dev, CI).
    Every importer runs inside imports_container, so one directory sees them all.

Slots
-----

A slot is one of ``cap`` files ``slot-<i>.lock``; it is held by holding an
exclusive non-blocking ``fcntl.flock`` on an open fd for the life of the run.
The kernel drops a flock when its last fd closes — including on SIGKILL / OOM
— so the cap cannot leak and needs no reaper. The fd is a raw ``os.open`` int
kept in a module-level registry (GC never closes it); ``release_slot`` closes
it. A process holds at most ONE slot: a second ``acquire_slot`` in a process
that already holds one (the ESID migration takes every connector's lock in one
process) shares it, refcounted by lock_id. The cap counts processes, not locks.

FIFO admission
--------------

A global admission flock (``admission.lock``) serialises the check. A waiter is
admitted only when it is the OLDEST waiter (by ``queued_at``) AND a slot is
free. The ordering source depends on what the caller has:

* With a ``progress_run_id`` the run's ``de_import_progress`` doc is the queue
  entry: status ``queued`` plus ``queued_at`` (ISO UTC) and ``queue_position``
  (1 = next), so the UI can show "queued (#N)". The doc is re-stamped every poll
  (``queue_position`` + ``updated_at``), which also keeps the 30-minute
  reaper off a live queued run; a waiter whose doc stopped updating for
  ``_waiter_stale_seconds`` (process died) drops out of the ordering, so a dead
  waiter can't block the head of the queue.
* Without one (the ESID migration script, a bare CLI run) the waiter takes a
  local ticket file ``ticket-<queued_at ns>-<pid>-<rand>.lock`` in the slot dir
  and holds a flock on it; a ticket whose flock can be taken belongs to a dead
  process and is removed. If a run-id waiter's ES registration fails it takes a
  ticket too, so it is never invisible to the others.

Both kinds are merged by ``queued_at``. If ES can't be read, the ordering
degrades to tickets + self — FIFO may be imperfect then, but the cap itself is
enforced purely by the slot flocks and never depends on ES.

While queued, cancel is honoured (the progress doc goes ``cancelling`` /
``cancelled``, or terminal by any other writer): the wait stops, the doc is
stamped ``cancelled`` and :func:`acquire_slot` returns False — the caller
(``ImportLockService.acquire``) drops its per-scope lock and returns None, the
same "couldn't get the lock" exit the importers already handle. After
``IMPORT_QUEUE_MAX_WAIT_SECONDS`` the run is failed with
"queued for global import slot longer than N s (cap=K)" and False is returned
the same way.

Infrastructure errors (slot dir unwritable, flock unsupported) fail OPEN with a
WARNING, matching the lock service's posture: a broken cap must not stop every
import. Only a timeout or a cancel returns False.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

ENV_CAP = "IMPORT_GLOBAL_CAP"
ENV_MAX_WAIT = "IMPORT_QUEUE_MAX_WAIT_SECONDS"
ENV_POLL = "IMPORT_QUEUE_POLL_SECONDS"
ENV_SLOT_DIR = "IMPORT_SLOT_DIR"

DEFAULT_MAX_WAIT_SECONDS = 43200  # 12 h
DEFAULT_POLL_SECONDS = 5.0

# imports_container's data volume (docker-compose: ${IMPORT_DATA_DIR}:/app/data).
_APP_DATA_DIR = "/app/data"

QUEUED_STATUS = "queued"
# Doc statuses that end a wait without admission: the user cancelled, or some
# other writer (the cancel route's complete_run, the reaper) already finalised it.
_CANCEL_STATUSES = ("cancelling", "cancelled")
_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.warning("import slots: %s=%r is not an int — using %r", name, raw, default)
        return default


def global_cap() -> int:
    """The configured cap, or 0 (feature OFF) when unset / 0 / negative / invalid."""
    return max(0, _int_env(ENV_CAP, 0))


def max_wait_seconds() -> int:
    value = _int_env(ENV_MAX_WAIT, DEFAULT_MAX_WAIT_SECONDS)
    return value if value > 0 else DEFAULT_MAX_WAIT_SECONDS


def poll_seconds() -> float:
    raw = (os.environ.get(ENV_POLL) or "").strip()
    if not raw:
        return DEFAULT_POLL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logging.warning("import slots: %s=%r is not a number — using %r", ENV_POLL, raw, DEFAULT_POLL_SECONDS)
        return DEFAULT_POLL_SECONDS
    return value if value > 0 else DEFAULT_POLL_SECONDS


def slot_dir() -> str:
    """Resolve the slot directory (not created here — see SlotPool)."""
    configured = (os.environ.get(ENV_SLOT_DIR) or "").strip()
    if configured:
        return configured
    if os.path.isdir(_APP_DATA_DIR):
        return os.path.join(_APP_DATA_DIR, "import_slots")
    return os.path.join(tempfile.gettempdir(), "deam-import-slots")


def _waiter_stale_seconds(poll: float) -> float:
    """A queued progress doc not re-stamped for this long belongs to a dead
    waiter and is ignored for ordering. Wide enough to ride out a slow ES write."""
    return max(60.0, 6.0 * poll)


def status_label(status: str | None, queue_position=None) -> str:
    """UI label for a run status: ``queued (#N)`` for a queued run,
    the bare status otherwise. Used by the progress API payloads so the
    dashboards banner and the Report Scheduler bar show the same text."""
    s = (status or "").strip()
    if s == QUEUED_STATUS:
        try:
            pos = int(queue_position)
        except (TypeError, ValueError):
            pos = 0
        return f"queued (#{pos})" if pos > 0 else "queued"
    return s


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _progress():
    from lib.import_progress_service import ImportProgressService

    return ImportProgressService


class _Abort(Exception):
    """Internal: the wait ended without admission (timeout / cancel)."""


class SlotPool:
    """One process's view of the slot directory.

    Production uses a single pool per process (:func:`_default_pool`). Tests
    build several pools on one directory to stand in for several processes:
    each pool opens its own fds, and flock conflicts per open file description,
    so two pools in one process exclude each other exactly like two processes.
    """

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.pid = os.getpid()
        self._mu = threading.Lock()
        # {"fd": int, "slot": int, "lock_ids": set[str]} while a slot is held.
        self._held: dict | None = None
        self._admission_fd: int | None = None
        # This pool's queue ticket while it waits without an ES queue entry.
        self._ticket_name: str | None = None
        self._ticket_fd: int | None = None

    # -- filesystem helpers -------------------------------------------------

    def _ensure_dir(self) -> None:
        os.makedirs(self.directory, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self.directory, name)

    def _admission_lock(self):
        pool = self

        class _Ctx:
            def __enter__(self_inner):
                if pool._admission_fd is None:
                    pool._admission_fd = os.open(pool._path("admission.lock"), os.O_RDWR | os.O_CREAT, 0o660)
                fcntl.flock(pool._admission_fd, fcntl.LOCK_EX)
                return self_inner

            def __exit__(self_inner, *exc):
                fcntl.flock(pool._admission_fd, fcntl.LOCK_UN)
                return False

        return _Ctx()

    def _try_take_slot(self, cap: int) -> tuple[int, int] | None:
        """Grab the first free slot file; (slot index, fd) or None when all held."""
        for i in range(cap):
            fd = os.open(self._path(f"slot-{i}.lock"), os.O_RDWR | os.O_CREAT, 0o660)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    continue
                raise
            return i, fd
        return None

    def _live_tickets(self) -> list[tuple[datetime, str]]:
        """(queued_at, name) for every ticket whose owner is alive; removes the
        dead ones. Caller holds the admission lock (ticket create/remove also
        happen under it, so a half-created ticket is never judged dead)."""
        out = []
        try:
            names = os.listdir(self.directory)
        except OSError:
            return out
        for name in names:
            if not (name.startswith("ticket-") and name.endswith(".lock")):
                continue
            if self._ticket_name is not None and name == self._ticket_name:
                continue
            path = self._path(name)
            try:
                fd = os.open(path, os.O_RDWR)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Held → its waiter is alive.
                os.close(fd)
                try:
                    ns = int(name.split("-")[1])
                    out.append((datetime.fromtimestamp(ns / 1e9, tz=timezone.utc), "ticket:" + name))
                except (IndexError, ValueError):
                    pass
                continue
            # We got the flock → the owner is gone. Remove the leftover.
            try:
                os.unlink(path)
            except OSError:
                pass
            os.close(fd)
        return out

    def _take_ticket(self, queued_at: datetime) -> None:
        ns = int(queued_at.timestamp() * 1e9)
        name = f"ticket-{ns:020d}-{os.getpid()}-{uuid.uuid4().hex[:8]}.lock"
        fd = os.open(self._path(name), os.O_RDWR | os.O_CREAT, 0o660)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self._ticket_name, self._ticket_fd = name, fd

    def _drop_ticket(self) -> None:
        if self._ticket_fd is None:
            return
        try:
            os.unlink(self._path(self._ticket_name))
        except OSError:
            pass
        try:
            os.close(self._ticket_fd)
        except OSError:
            pass
        self._ticket_name, self._ticket_fd = None, None

    # -- public -------------------------------------------------------------

    def held_slot(self) -> int | None:
        with self._mu:
            return self._held["slot"] if self._held else None

    def acquire(
        self,
        lock_id: str,
        *,
        cap: int,
        max_wait: float,
        poll: float,
        progress_run_id: str | None = None,
        on_poll: Callable[[], None] | None = None,
        label: str = "",
    ) -> bool:
        """Block until this process holds a slot (True) or the wait is
        abandoned — timeout or cancel (False). See the module docstring."""
        with self._mu:
            if self._held is not None:
                # One slot per process (module docstring): share it.
                self._held["lock_ids"].add(lock_id)
                return True

        self._ensure_dir()
        run_id = (progress_run_id or "").strip() or None
        queued_at = _now()
        start = time.monotonic()
        me: str | None = None  # this waiter's ordering key, set on registration
        try:
            while True:
                taken = None
                position = 0
                with self._admission_lock():
                    entries = self._ordering(poll)
                    if me is None:
                        if run_id:
                            # Not queued yet — a leftover doc entry of ours can't count.
                            entries = [e for e in entries if e[1] != "run:" + run_id]
                        # First pass: an empty queue + a free slot admits at once
                        # (no "queued" flicker on the progress doc).
                        if not entries:
                            taken = self._try_take_slot(cap)
                        if taken is None:
                            me = self._register(run_id, queued_at)
                    if taken is None:
                        mine = (queued_at, me)
                        entries = sorted([e for e in entries if e[1] != me] + [mine])
                        position = entries.index(mine) + 1
                        if position == 1:
                            taken = self._try_take_slot(cap)
                    if taken is not None:
                        self._drop_ticket()
                if taken is not None:
                    slot, fd = taken
                    with self._mu:
                        self._held = {"fd": fd, "slot": slot, "lock_ids": {lock_id}}
                    waited = time.monotonic() - start
                    if me is not None and run_id and me == "run:" + run_id:
                        _safe(lambda: _progress().mark_admitted(run_id=run_id))
                    if me is not None and on_poll is not None:
                        # Re-arm the caller's per-scope lock TTL from the real start.
                        _safe(on_poll)
                    logging.info(
                        "import slot: admitted %s after %.1fs queued (slot %d, cap %d, run_id=%s)",
                        label or lock_id,
                        waited,
                        slot,
                        cap,
                        run_id,
                    )
                    return True

                # Still queued.
                if run_id:
                    doc = _safe(lambda: _progress().get_run(run_id)) or {}
                    status = doc.get("status")
                    if status in _CANCEL_STATUSES or status in _TERMINAL_STATUSES:
                        if status == "cancelling":
                            _safe(lambda: _progress().complete_run(run_id=run_id, status="cancelled"))
                        logging.warning(
                            "import slot: %s left the queue — run %s is %s (cancel while queued)",
                            label or lock_id,
                            run_id,
                            status,
                        )
                        raise _Abort()
                waited = time.monotonic() - start
                if waited >= max_wait:
                    msg = f"queued for global import slot longer than {int(max_wait)} s (cap={cap})"
                    logging.warning("import slot: %s — %s", label or lock_id, msg)
                    if run_id:
                        _safe(lambda: _progress().complete_run(run_id=run_id, status="failed", error=msg))
                    raise _Abort()
                if run_id and me == "run:" + run_id:
                    _safe(lambda: _progress().heartbeat_queued(run_id=run_id, queue_position=position))
                if on_poll is not None:
                    _safe(on_poll)
                time.sleep(max(0.01, min(poll, max_wait - waited)))
        except _Abort:
            return False
        finally:
            if self._ticket_fd is not None:
                with self._admission_lock():
                    self._drop_ticket()

    def _register(self, run_id: str | None, queued_at: datetime) -> str:
        """Join the queue (caller holds the admission lock). Returns this
        waiter's ordering key."""
        if run_id:
            ok = _safe(
                lambda: _progress().mark_queued(run_id=run_id, queued_at=queued_at.isoformat(), queue_position=0),
                default=False,
            )
            if ok:
                return "run:" + run_id
            logging.warning("import slot: could not mark run %s queued in ES — using a local ticket", run_id)
        self._take_ticket(queued_at)
        return "ticket:" + self._ticket_name

    def _ordering(self, poll: float) -> list[tuple[datetime, str]]:
        """Every live waiter as (queued_at, key): fresh ``queued`` progress docs
        plus live ticket files. Caller holds the admission lock."""
        entries = self._live_tickets()
        fresh = timedelta(seconds=_waiter_stale_seconds(poll))
        docs = _safe(lambda: _progress().list_queued(fresh_within=fresh)) or []
        for d in docs:
            rid = d.get("run_id")
            qa = _parse_iso(d.get("queued_at"))
            if rid and qa is not None:
                entries.append((qa, "run:" + rid))
        return entries

    def release(self, lock_id: str) -> None:
        """Drop ``lock_id``'s share of the held slot; close the fd (freeing the
        slot) once no lock in this process uses it. No-op when nothing is held."""
        with self._mu:
            held = self._held
            if held is None or lock_id not in held["lock_ids"]:
                return
            held["lock_ids"].discard(lock_id)
            if held["lock_ids"]:
                return
            self._held = None
        try:
            # Close only — never LOCK_UN: a forked child may share this open
            # file description, and an explicit unlock would free the slot
            # under it. The kernel releases the flock when the last fd closes.
            os.close(held["fd"])
        except OSError:
            pass
        logging.info("import slot: released slot %d", held["slot"])


def _safe(fn, default=None):
    """Run a bookkeeping call fail-soft (progress-doc writes must never abort
    the wait or the import)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — boundary; bookkeeping only
        logging.warning("import slot: bookkeeping call failed: %s", exc)
        return default


_POOL: SlotPool | None = None
_POOL_MU = threading.Lock()


def _default_pool(create: bool = True) -> SlotPool | None:
    """This process's pool. A forked child gets a fresh one — the registry it
    inherited describes the parent's slot, which the child must not release.
    A pool that holds nothing is also rebuilt when ``IMPORT_SLOT_DIR`` changed."""
    global _POOL
    with _POOL_MU:
        pool = _POOL
        if pool is not None and pool.pid != os.getpid():
            pool = None
        if pool is not None and create and pool.directory != slot_dir() and pool.held_slot() is None:
            pool = None
        if pool is None and create:
            pool = SlotPool(slot_dir())
        _POOL = pool
        return pool


def acquire_slot(
    lock_id: str,
    *,
    progress_run_id: str | None = None,
    on_poll: Callable[[], None] | None = None,
    label: str = "",
) -> bool:
    """take a global import slot for ``lock_id``, queueing FIFO.

    True = proceed (admitted, cap OFF, or a fail-open infrastructure error).
    False = do not run (timed out or cancelled while queued; the progress doc
    has already been stamped). ``on_poll`` is called on every queued poll and
    once on admission — ``ImportLockService.acquire`` uses it to keep its
    per-scope lock's TTL alive while the run waits.
    """
    cap = global_cap()
    if cap <= 0 or not lock_id:
        return True
    try:
        pool = _default_pool()
        return pool.acquire(
            lock_id,
            cap=cap,
            max_wait=max_wait_seconds(),
            poll=poll_seconds(),
            progress_run_id=progress_run_id,
            on_poll=on_poll,
            label=label,
        )
    except Exception as exc:  # noqa: BLE001 — fail open (module docstring)
        logging.warning("import slot: slot machinery failed (%s) — proceeding WITHOUT a slot", exc)
        return True


def release_slot(lock_id: str) -> None:
    """Release ``lock_id``'s slot. Safe when none is held / the cap is OFF."""
    if not lock_id:
        return
    try:
        pool = _default_pool(create=False)
        if pool is not None and pool.pid == os.getpid():
            pool.release(lock_id)
    except Exception as exc:  # noqa: BLE001 — the kernel frees it at exit anyway
        logging.warning("import slot: release failed: %s", exc)
