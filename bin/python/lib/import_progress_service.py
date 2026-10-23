"""Import-progress service.

Writes per-run backfill progress to ``de_import_progress`` so the
onboarding step-7 "Building…" screen and the first-look dashboard's
sync pill can show live percentages instead of a static spinner.

Doc shape (one per (connection_esid, run_id))::

    {
        "run_id":          "<uuid>",
        "username":        "alice",
        "connection_esid": "conn_abc",
        "connector_id":    "meta_ads",
        "days_total":      90,
        "days_fetched":    0,            # bumped by update_progress
        "status":          "pending",    # pending | queued | running | succeeded | failed
                                         # (+ cancelling / cancelled)
        "queued_at": "<iso utc>", # set while waiting for a
        "queue_position":  2,            #   global import slot (1 = next); see
                                         #   lib.import_slots
        "started_at":      "<iso utc>",  # set on start_run
        "updated_at":      "<iso utc>",  # bumped on every write
        "ended_at":        "<iso utc>",  # set on complete_run
        "error":           "<msg>",      # set on complete_run(status="failed")
        "import_errors":   [ {category,title,message,severity}, ... ],
                                         # per-asset user-readable errors
                                         # appended by record_import_error (deduped
                                         # by category). Distinct from "error":
                                         # these are non-fatal per-asset failures the
                                         # run continued past.
    }

Idempotency
-----------

``start_run`` is idempotent on (connection_esid, last hour): if a doc
already exists for the connection AND it was started in the past 60
minutes AND its status is pending/running, the existing run_id is
returned and no new doc is written. This protects against a wizard
back-button retry firing the step-6 finish handler twice — without the
guard, the API's "latest" query would surface a fresh 0% doc and the
pill would visibly snap backwards.

The 60-minute window is deliberately wider than any single backfill —
a 90d Meta Ads pull is well under 10 minutes in practice. Anything
older is treated as a stale prior run and a fresh start_run begins a
new run_id.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

PROGRESS_INDEX = "de_import_progress"

# Idempotency window for start_run. A second start_run within this window
# for the same connection_esid reuses the existing run_id. Wide enough
# to cover an entire 90d backfill; narrow enough that a same-day retry
# starts fresh.
_RESTART_WINDOW = timedelta(minutes=60)

# staleness window for reap_stale_runs. A non-terminal doc whose
# last write (``updated_at``) is older than this is treated as orphaned: its
# importer died (container restart, OOM, SIGKILL, or a wedge) without ever
# stamping a terminal status, so the doc would sit pending/running/cancelling
# forever. 30 minutes is deliberately well past any real backfill (a 90d Meta
# Ads pull is under 10 minutes) AND past the route's existing 10-minute
# "display as stale" threshold, so the reaper never races a slow-but-alive
# run. Anchored on ``updated_at`` (not ``started_at``) so a long run that's
# actively bumping update_progress is never reaped.
_REAP_WINDOW = timedelta(minutes=30)

# guarded partial update for the global-import-queue fields: apply
# ``params.doc`` (and drop ``params.remove``) ONLY when the doc's current status
# is in ``params.expect``, else noop. A plain partial update would let a queued
# waiter's heartbeat overwrite a ``cancelling`` the cancel route wrote between
# the waiter's read and its write — silently un-cancelling the run.
_GUARDED_UPDATE_SCRIPT = (
    "if (params.expect.contains(ctx._source.status)) { "
    "for (e in params.doc.entrySet()) { ctx._source[e.getKey()] = e.getValue(); } "
    "for (f in params.remove) { ctx._source.remove(f); } "
    "} else { ctx.op = 'noop'; }"
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _es():
    """Lazy ES client lookup so tests can patch this module's import.

    Returns None on connect failure — every public method handles that
    by returning a soft default (None / no-op) rather than raising. The
    pill is a UX nicety; an ES outage should not 500 the step-6 finish
    handler.
    """
    from lib.es_helpers import get_elasticsearch_client

    return get_elasticsearch_client()


class ImportProgressService:
    """Thin CRUD wrapper around ``de_import_progress``.

    Methods are static; the class is just a namespace so callers read
    ``ImportProgressService.start_run(...)`` rather than five module-
    level functions.
    """

    @staticmethod
    def start_run(
        *,
        username: str,
        connection_esid: str,
        connector_id: str,
        days_total: int,
    ) -> str | None:
        """Begin a new progress doc; return the run_id (or None on ES error).

        Idempotent on (connection_esid, last 60min) — see module docstring.
        """
        if not username or not connection_esid or not connector_id:
            return None
        if days_total <= 0:
            return None

        es = _es()
        if es is None:
            return None

        existing = _find_active_run(es, connection_esid)
        if existing is not None:
            return existing

        run_id = str(uuid.uuid4())
        now = _now_iso()
        doc: dict[str, Any] = {
            "run_id": run_id,
            "username": username,
            "connection_esid": connection_esid,
            "connector_id": connector_id,
            "days_total": int(days_total),
            "days_fetched": 0,
            "status": "pending",
            "started_at": now,
            "updated_at": now,
        }
        try:
            es.index(
                index=PROGRESS_INDEX,
                id=run_id,
                document=doc,
                refresh="wait_for",
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning(
                "start_run failed for connection=%r: %s",
                connection_esid,
                exc,
            )
            return None
        return run_id

    @staticmethod
    def update_progress(*, run_id: str, days_fetched: int, days_total: int | None = None) -> None:
        """Bump ``days_fetched`` and ``updated_at``; flip status to running.

        No-op if ``run_id`` is empty or ES is unavailable. Soft-failure
        is by design — the importer must not abort because a progress
        write failed.

        optional ``days_total`` rebases the denominator mid-run.
        The dedup-only window pull's work units are AD ACCOUNTS, not days:
        the web layer mints the doc with a day-based total (all it knows),
        and the window importer — the run's ONLY stage — rebases to
        (accounts done / accounts total) once it has resolved its scope, so
        the pill advances per account instead of sitting at 0% for the whole
        pull. Callers that omit it (the per-day loop) are unchanged.
        """
        if not run_id:
            return
        es = _es()
        if es is None:
            return
        doc: dict[str, Any] = {
            "days_fetched": int(days_fetched),
            "status": "running",
            "updated_at": _now_iso(),
        }
        if days_total is not None:
            doc["days_total"] = int(days_total)
        try:
            es.update(
                index=PROGRESS_INDEX,
                id=run_id,
                body={"doc": doc},
                refresh="wait_for",
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("update_progress run_id=%r: %s", run_id, exc)

    @staticmethod
    def set_phase(*, run_id: str, phase: str) -> None:
        """follow-up 3 — record which pipeline stage is running.

        The FB import has a per-date ad-insights loop (which drives
        ``days_fetched``) followed by non-per-date post-loop stages
        (page insights, instagram) that can run for minutes without
        advancing the day counter — so the pill would sit at 100% while
        those stages churn. Recording the stage lets the UI show e.g.
        "Fetching page insights…" instead of a misleading static 100%.

        Known phases: ``ad_insights`` (implicit during the date loop),
        ``page_insights``, ``instagram``. Free-form string; no
        validation so a new stage doesn't need a service change.

        No-op on empty run_id or ES outage; bumps ``updated_at`` so the
        stale-detection heuristic doesn't misread a long page phase as a
        dead importer. Soft-fail — a phase write must not abort the import.
        """
        if not run_id or not phase:
            return
        es = _es()
        if es is None:
            return
        try:
            es.update(
                index=PROGRESS_INDEX,
                id=run_id,
                body={"doc": {"phase": str(phase), "updated_at": _now_iso()}},
                refresh="wait_for",
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("set_phase run_id=%r phase=%r: %s", run_id, phase, exc)

    @staticmethod
    def record_import_error(
        *,
        run_id: str,
        raw: str,
        connector_id: str | None = None,
        detail: str | None = None,
        team_esid: str | None = None,
    ) -> None:
        """follow-up — append a classified, user-readable import error to
        the run's ``import_errors`` array.

        The top-level ``error`` field (set by :meth:`complete_run`) only captures a
        run's *fatal* failure — the one that aborted it. But most user-actionable
        import problems are **per-asset**: a single Facebook page the user lacks
        permission on, one GA4 property whose OAuth token died, one revoked
        connection among several. Those are caught *inside* the importer's
        per-team/per-asset loop, logged, and the loop continues — so the run can
        "succeed" overall while silently dropping one asset's data. This method is
        how an importer surfaces those to the user without aborting: call it at the
        catch site with the raw error string, and the Report Scheduler's "Errors"
        button shows the classified message.

        Behaviour:

        * Classifies ``raw`` via :func:`lib.import_error_classifier.classify` so the
          stored entry is already user-readable (``{category, title, message,
          severity}``) — no raw log text reaches the UI.
        * ``detail`` names the SPECIFIC asset (the Google Ads customer, the FB
          page/team, the GA4 property, the YouTube channel). It is woven into the
          user-facing message ("Kavanah Media: This connector isn't fully
          configured …") AND becomes part of the dedup key — so the same asset
          hitting the error 90 times records once, but two DIFFERENT unconfigured
          assets each get their own line.
        * **De-duplicated by (category, detail)** via a painless script: a 90-day
          FB loop that hits "permission denied" for the same page records it once;
          a second page's permission error is a distinct line. The first
          occurrence of each (category, detail) wins (the message is identical for
          a given pair). This also makes the call idempotent on retry.
        * Atomic append (scripted update) so the FB page/instagram sub-importers —
          which share one ``run_id`` but run as separate processes — don't clobber
          each other's entries with a read-modify-write race.

        No-op on empty ``run_id`` (the cron path passes None) or empty ``raw``.
        Soft-fail on ES outage / classify error — recording a user-facing error
        must never abort or slow the import that hit it. Mirrors the fail-soft
        contract of every other write here.
        """
        if not run_id or not (raw or "").strip():
            return
        try:
            from lib.import_error_classifier import classify

            entry = classify(raw, connector_id=connector_id, detail=detail)
        except Exception as exc:  # noqa: BLE001 — classify must never abort the import
            logging.warning("record_import_error classify failed run_id=%r: %s", run_id, exc)
            return
        # durable per-team attribution. Stamped when the importer's
        # per-team loop bound a team (lib.import_run_context.set_current_team);
        # absent otherwise (historical shape, read-time heuristics still apply).
        # Also part of the dedup key below, so two teams hitting the identical
        # (category, detail) error each record their own line.
        if team_esid:
            entry["team_esid"] = team_esid
        es = _es()
        if es is None:
            return
        try:
            es.update(
                index=PROGRESS_INDEX,
                id=run_id,
                body={
                    "script": {
                        # Append only if this (category, detail) pair isn't already
                        # present (first occurrence wins). ctx._source.import_errors
                        # is created on first call. Keying on category AND detail lets
                        # two different unconfigured accounts each get a line while a
                        # single account hit repeatedly records once.
                        # team_esid joined the dedup key (null-safe on
                        # both sides so historical entries without it compare as '').
                        "source": (
                            "if (ctx._source.import_errors == null) "
                            "{ ctx._source.import_errors = []; } "
                            "boolean seen = false; "
                            "for (e in ctx._source.import_errors) "
                            "{ if (e.category == params.entry.category && "
                            "(e.detail == null ? '' : e.detail) == params.entry.detail && "
                            "(e.team_esid == null ? '' : e.team_esid) == params.entry_team) "
                            "{ seen = true; break; } } "
                            "if (!seen) { ctx._source.import_errors.add(params.entry); "
                            "ctx._source.updated_at = params.now; }"
                        ),
                        "lang": "painless",
                        "params": {
                            "entry": entry,
                            "entry_team": entry.get("team_esid") or "",
                            "now": _now_iso(),
                        },
                    }
                },
                refresh="wait_for",
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("record_import_error run_id=%r: %s", run_id, exc)

    @staticmethod
    def record_backfill_bailout(*, run_id: str, note: dict) -> None:
        """stamp a ``backfill_bailout`` sub-object on the run's progress
        doc when a backfill stopped gracefully at a Meta rate limit.

        Additive and distinct from ``error`` / ``complete_run``: a bail-out is NOT a
        failure — the data written before the limit is committed, real data. This
        note lets the Report Scheduler card + History tell the user the backfill
        stopped early and MORE remains (re-run to continue from where it left off).

        ``note`` carries ``{stopped_at_rate_limit, accounts_completed,
        accounts_total, date_from, date_to}``. No-op on empty ``run_id`` (cron path).
        Soft-fails on ES outage — a note write must never abort/slow the import.
        """
        if not run_id or not isinstance(note, dict):
            return
        es = _es()
        if es is None:
            return
        try:
            es.update(
                index=PROGRESS_INDEX,
                id=run_id,
                body={"doc": {"backfill_bailout": note, "updated_at": _now_iso()}},
                refresh="wait_for",
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("record_backfill_bailout run_id=%r: %s", run_id, exc)

    @staticmethod
    def complete_run(
        *,
        run_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Stamp the terminal status (``succeeded``, ``failed``, or ``cancelled``).

        ``error`` is only persisted when ``status == "failed"`` so the
        success path doesn't carry a stale error string from a previous
        retry. ``ended_at`` is set so a stuck "pending" doc can be
        distinguished from a recently-completed one.

         item 2: ``cancelled`` is a distinct terminal status —
        the importer acknowledged the user's cancel request (set via
        ``cancel_run`` → ``cancelling`` intermediate state). The banner
        renders this as "Import cancelled — partial data may be
        visible." rather than the failure-error styling.
        """
        if not run_id:
            return
        if status not in ("succeeded", "failed", "cancelled"):
            return
        es = _es()
        if es is None:
            return
        now = _now_iso()
        patch: dict[str, Any] = {
            "status": status,
            "updated_at": now,
            "ended_at": now,
        }
        if status == "failed" and error:
            patch["error"] = str(error)[:500]
        try:
            es.update(
                index=PROGRESS_INDEX,
                id=run_id,
                body={"doc": patch},
                refresh="wait_for",
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("complete_run run_id=%r: %s", run_id, exc)

    @staticmethod
    def resolve_orphaned_lock_loser(run_id: str) -> bool:
        """stamp a lock-race loser's progress doc ``failed`` ONLY when
        it is a true orphan (no other process is driving it).

        Background. A progress-capable importer that loses the per-connector
        import lock exits without doing the work. Whether it should stamp its
        ``progress_run_id`` terminal depends on WHO holds the lock:

          * **Different run** (a cron sweep, or another connection's import) — the
            winner has its OWN run_id and will never touch this doc. Left
            un-stamped, this doc sits ``pending`` at 0/N until the 30-min reaper,
            hanging the Run-now bar (observed for google_ads on staging
            2026-06-19; orphan doc 570d21c1).
          * **Same run** (two Run-now clicks on the SAME schedule within the
            ``start_run`` idempotency window resolve to the SAME run_id — keyed on
            ``connection_esid``) — the lock WINNER adopted this very doc and is
            progressing it. Stamping it failed here would mark a concurrently
            *succeeding* import as failed in the bar (the bug fixed).

        The discriminator: re-read the doc. If it is still ``pending`` and has
        ``days_fetched == 0``, no co-owner has advanced it → it is an orphan →
        stamp it ``failed``. If it is already ``running`` or has any progress, a
        same-run winner is driving it → leave it for that winner to finalize.

        Fail-soft: returns True iff it stamped the doc failed; any ES error (or a
        missing / already-terminal / already-advanced doc) returns False without
        raising, so a progress bookkeeping problem never aborts the importer's
        clean lock-loss exit.
        """
        if not run_id:
            return False
        doc = ImportProgressService.get_run(run_id)
        if not doc:
            return False
        # A co-owner (same-schedule lock winner) is driving this doc → don't touch.
        if doc.get("status") != "pending" or doc.get("days_fetched"):
            return False
        ImportProgressService.complete_run(
            run_id=run_id,
            status="failed",
            error="another import is already in progress; this run was skipped",
        )
        return True

    @staticmethod
    def cancel_run(run_id: str) -> bool:
        """Flip status to ``cancelling`` so the importer's per-iteration
        check picks it up and bails out.

        Returns True if the doc was found and the status was flipped
        (idempotent — calling twice on the same run_id returns True
        each time). False if the run_id is missing or the doc is
        already terminal (``succeeded`` / ``failed`` / ``cancelled``)
        — in that case the cancel is a no-op because the importer
        already exited.

        Importers MUST call ``is_cancelling`` between (team, date)
        iterations and ``complete_run(status="cancelled")`` when they
        observe True. The two-step protocol (cancelling → cancelled)
        lets the UI distinguish "user asked to stop" from "importer
        stopped" — useful when an importer is in a long Graph round-
        trip and hasn't ack'd yet.
        """
        if not run_id:
            return False
        es = _es()
        if es is None:
            return False
        try:
            current = es.get(index=PROGRESS_INDEX, id=run_id)
        except Exception:
            return False
        src = (current or {}).get("_source") or {}
        if src.get("status") in ("succeeded", "failed", "cancelled"):
            return False
        try:
            es.update(
                index=PROGRESS_INDEX,
                id=run_id,
                body={"doc": {"status": "cancelling", "updated_at": _now_iso()}},
                refresh="wait_for",
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("cancel_run run_id=%r: %s", run_id, exc)
            return False
        return True

    @staticmethod
    def is_cancelling(run_id: str) -> bool:
        """Check whether the user has requested cancellation (
        item 2). Importers call this between iterations; True means
        the per-loop break + ``complete_run(status="cancelled")`` path
        should fire.

        Fail-soft: ES outages, missing docs, or unexpected statuses
        all return False so a transient blip doesn't abort an import
        the user hasn't actually cancelled. The cron-path callers
        (no run_id) get False trivially.
        """
        if not run_id:
            return False
        es = _es()
        if es is None:
            return False
        try:
            current = es.get(index=PROGRESS_INDEX, id=run_id)
        except Exception:
            return False
        src = (current or {}).get("_source") or {}
        return src.get("status") == "cancelling"

    @staticmethod
    def get_run(run_id: str) -> dict[str, Any] | None:
        """Fetch a single progress doc by its ``run_id``.

        Returns the ``_source`` dict, or None when the run_id is empty,
        the doc is missing, or ES is down. Fail-soft like every other
        read here.

        This is the ownership-check primitive the cancel route needs:
        verifying ``get_run(run_id)["username"] == session_user`` is
        exact (it addresses the run the caller actually clicked Cancel
        on), where ``get_latest_for_user`` is only a heuristic — it
        returns whichever doc the user touched most recently, which is
        NOT necessarily the run being cancelled when the user has more
        than one progress doc. Using the latest doc let a legitimate
        cancel of an older-but-still-running run get rejected as
        ``wrong_user``.
        """
        if not run_id:
            return None
        es = _es()
        if es is None:
            return None
        try:
            current = es.get(index=PROGRESS_INDEX, id=run_id)
        except Exception:
            return None
        return (current or {}).get("_source") or None

    @staticmethod
    def get_latest_for_user(username: str) -> dict[str, Any] | None:
        """Return the most-recent (by ``updated_at``) progress doc for ``username``.

        Returns None when no doc exists or ES is down. The privacy
        guarantee — user A never sees user B's doc — comes from the
        ``term: {username.keyword: ...}`` clause; a regression that
        drops or wildcards this clause is the load-bearing bug
        PROGRESS-002 pins.

        the ``term`` query targets the ``.keyword`` sub-field
        because the ``username`` field is mapped as ``text`` (analyzed)
        with a ``keyword`` multi-field. A bare ``term: {username: ...}``
        never matches a hyphenated username (``test-account`` tokenizes
        into ``test`` + ``account`` at index time, so the exact-string
        term lookup misses). That broke the banner fetch for any user
        whose username carries a ``-``, ``.``, or ``@`` — i.e. roughly
        100% of email-style logins.
        """
        if not username:
            return None
        es = _es()
        if es is None:
            return None
        try:
            resp = es.search(
                index=PROGRESS_INDEX,
                body={
                    "size": 1,
                    "sort": [{"updated_at": {"order": "desc"}}],
                    "query": {"term": {"username.keyword": username}},
                },
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("get_latest_for_user user=%r: %s", username, exc)
            return None
        hits = (resp.get("hits") or {}).get("hits") or []
        if not hits:
            return None
        return hits[0].get("_source") or None

    @staticmethod
    def find_active_run_id(connection_esid: str) -> str | None:
        """public idempotency probe used by the wizard finish
        handler's Popen guard. Wraps :func:`_find_active_run` so callers
        outside this module can check for an in-flight run without
        creating one. Returns the run_id of an active (pending / running)
        doc started within the last hour, or ``None`` when no such doc
        exists (or ES is down).
        """
        if not connection_esid:
            return None
        es = _es()
        if es is None:
            return None
        return _find_active_run(es, connection_esid)

    @staticmethod
    def _guarded_update(run_id: str, *, expect: list[str], doc: dict, remove: list[str] | None = None) -> bool:
        """apply ``doc`` iff the current status is in ``expect``.

        Returns True only when ES reports the doc ``updated``; False on a noop
        (status moved on), a missing doc, or an ES error. Never raises."""
        if not run_id:
            return False
        es = _es()
        if es is None:
            return False
        try:
            resp = es.update(
                index=PROGRESS_INDEX,
                id=run_id,
                body={
                    "script": {
                        "source": _GUARDED_UPDATE_SCRIPT,
                        "lang": "painless",
                        "params": {"expect": list(expect), "doc": dict(doc), "remove": list(remove or [])},
                    }
                },
                refresh="wait_for",
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("progress queue update run_id=%r: %s", run_id, exc)
            return False
        result = resp.get("result") if hasattr(resp, "get") else None
        return result == "updated"

    @staticmethod
    def mark_queued(*, run_id: str, queued_at: str, queue_position: int) -> bool:
        """the run is waiting for a global import slot.

        Flips a non-terminal, non-cancelled doc (pending / running / queued) to
        ``queued`` with ``queued_at`` + ``queue_position``. ``updated_at`` is
        bumped so the reaper reads the waiter as alive. Returns True when
        the doc was updated — lib.import_slots falls back to a local queue ticket
        otherwise."""
        return ImportProgressService._guarded_update(
            run_id,
            expect=["pending", "running", "queued"],
            doc={
                "status": "queued",
                "queued_at": queued_at,
                "queue_position": int(queue_position),
                "updated_at": _now_iso(),
            },
        )

    @staticmethod
    def heartbeat_queued(*, run_id: str, queue_position: int) -> bool:
        """per-poll refresh of a queued run: new ``queue_position``
        and ``updated_at`` (keeps the reaper off it and keeps the waiter in the
        FIFO ordering). Noop once the status is no longer ``queued`` — a cancel
        written meanwhile is never overwritten."""
        return ImportProgressService._guarded_update(
            run_id,
            expect=["queued"],
            doc={"queue_position": int(queue_position), "updated_at": _now_iso()},
        )

    @staticmethod
    def mark_admitted(*, run_id: str) -> bool:
        """the queued run got its slot: status back to ``running``,
        ``queue_position`` cleared (``queued_at`` stays as a record of the wait)."""
        return ImportProgressService._guarded_update(
            run_id,
            expect=["queued"],
            doc={"status": "running", "updated_at": _now_iso()},
            remove=["queue_position"],
        )

    @staticmethod
    def list_queued(*, fresh_within: timedelta) -> list[dict[str, Any]] | None:
        """every ``queued`` doc re-stamped within ``fresh_within``
        (``run_id`` + ``queued_at``), the FIFO ordering source for
        lib.import_slots. A queued doc older than that belongs to a dead waiter
        and is left out so it cannot block the head of the queue. Returns None
        on ES error (caller degrades to its local ordering)."""
        es = _es()
        if es is None:
            return None
        cutoff = (datetime.now(tz=timezone.utc) - fresh_within).isoformat()
        try:
            resp = es.search(
                index=PROGRESS_INDEX,
                body={
                    "size": 500,
                    "_source": ["run_id", "queued_at"],
                    "query": {
                        "bool": {
                            "filter": [
                                {"terms": {"status": ["queued"]}},
                                {"range": {"updated_at": {"gte": cutoff}}},
                            ]
                        }
                    },
                },
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("list_queued search failed: %s", exc)
            return None
        out = []
        for hit in (resp.get("hits") or {}).get("hits") or []:
            src = hit.get("_source") or {}
            out.append({"run_id": src.get("run_id") or hit.get("_id"), "queued_at": src.get("queued_at")})
        return out

    @staticmethod
    def reap_stale_runs() -> int:
        """stamp orphaned non-terminal docs terminal.

        Root-cause cleanup for the staging symptom that opened this
        ticket: an importer that dies mid-run (container restart, OOM,
        SIGKILL, or a wedged Graph call) never stamps a terminal status,
        so its ``de_import_progress`` doc sits ``pending`` / ``running`` /
        ``cancelling`` forever. The read-path route only
        *reinterprets* such a doc cosmetically; this method actually
        writes the terminal state so the doc stops being non-terminal in
        ES (and ``get_latest_for_user`` / ``_find_active_run`` stop
        surfacing it).

        Reaps any doc whose ``updated_at`` is older than ``_REAP_WINDOW``:

          * ``cancelling`` → ``cancelled`` — the user asked to stop and
            the importer died before acking; honor the request. Mirrors
            the route's stale-cancelling reinterpretation.
          * ``pending`` / ``running`` → ``failed`` (error="orphaned:
            importer died before completing") — the run never finished.
          * ``queued`` → ``failed``, same as ``running``. A live
            waiter for a global import slot re-stamps ``updated_at`` every
            poll, so only a waiter that died in the queue ever ages past the
            window.

        Idempotent and safe to call on every importer start: it routes
        each doc through :meth:`complete_run`, whose terminal-guard makes
        a doc that finished legitimately between the query and the update
        a no-op (no double-stamp, no clobbering a real success). A run
        that's slow but alive is never reaped because it keeps bumping
        ``updated_at`` via :meth:`update_progress`.

        Returns the number of docs stamped (0 on ES outage or none stale).
        Fail-soft: never raises — a reaper failure must not abort the
        importer that called it.
        """
        es = _es()
        if es is None:
            return 0

        cutoff = (datetime.now(tz=timezone.utc) - _REAP_WINDOW).isoformat()
        try:
            resp = es.search(
                index=PROGRESS_INDEX,
                body={
                    "size": 500,
                    "query": {
                        "bool": {
                            "filter": [
                                # ``queued`` is non-terminal like ``running``.
                                {"terms": {"status": ["pending", "queued", "running", "cancelling"]}},
                                {"range": {"updated_at": {"lt": cutoff}}},
                            ]
                        }
                    },
                },
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("reap_stale_runs search failed: %s", exc)
            return 0

        hits = (resp.get("hits") or {}).get("hits") or []
        reaped = 0
        for hit in hits:
            src = hit.get("_source") or {}
            run_id = src.get("run_id") or hit.get("_id")
            if not run_id:
                continue
            status = src.get("status")
            if status == "cancelling":
                ImportProgressService.complete_run(run_id=run_id, status="cancelled")
            else:
                ImportProgressService.complete_run(
                    run_id=run_id,
                    status="failed",
                    error="orphaned: importer died before completing",
                )
            reaped += 1

        if reaped:
            logging.info("reap_stale_runs: stamped %d orphaned run(s) terminal", reaped)
        return reaped


def _find_active_run(es: Any, connection_esid: str) -> str | None:
    """Idempotency lookup. Returns the run_id of an active doc started
    within the last hour, or None.

    "Active" = status in {pending, queued, running} (a run waiting
    for a global import slot is still in flight). A failed/succeeded doc
    from the same connection earlier today is *not* reused — the user
    is starting a fresh backfill (e.g. they reconfigured assets and re-
    finished onboarding), and conflating the two would surface the old
    doc's terminal status.
    """
    cutoff = (datetime.now(tz=timezone.utc) - _RESTART_WINDOW).isoformat()
    try:
        resp = es.search(
            index=PROGRESS_INDEX,
            body={
                "size": 1,
                "sort": [{"started_at": {"order": "desc"}}],
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"connection_esid": connection_esid}},
                            {"terms": {"status": ["pending", "queued", "running"]}},
                            {"range": {"started_at": {"gte": cutoff}}},
                        ]
                    }
                },
            },
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning(
            "_find_active_run connection=%r: %s",
            connection_esid,
            exc,
        )
        return None
    hits = (resp.get("hits") or {}).get("hits") or []
    if not hits:
        return None
    return (hits[0].get("_source") or {}).get("run_id")
