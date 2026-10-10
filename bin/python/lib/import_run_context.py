"""Ambient import-run context.

Per-asset, user-actionable import errors (a Facebook page the user lacks
permission on, a GA4 property whose OAuth token died, a revoked connection
among several) are caught *deep inside* an importer's per-team/per-asset loop,
logged, and skipped — the loop continues so one bad asset doesn't sink the
whole run. To surface those to the user via the Report Scheduler's "Errors"
button, the catch site must record a classified error onto the run's
``de_import_progress`` doc.

Threading the run's ``progress_run_id`` down through every importer leaf
function's signature would be a large, cross-cutting change to code that is
otherwise unrelated to scheduling. Instead, this module holds the run_id as
*ambient process state* — set once by the importer's dispatcher from the
``--progress_run_id`` CLI arg, read implicitly by ``record_user_error`` at each
catch site. This mirrors how ``lib.logging.define(...)`` already establishes an
ambient logging context in these importers.

Safe because each importer invocation is its own process: one run_id per
process, no cross-run bleed. The cron path never sets a run_id, so
``record_user_error`` is a no-op there (cron imports have no scheduler dialog to
surface errors to). Both functions are fail-soft — recording a user-facing error
must never abort or slow the import that hit it.
"""

from __future__ import annotations

import logging

# Module-global: the active run's progress doc id, or None on the cron path /
# before the dispatcher sets it. One per process (see module docstring).
_PROGRESS_RUN_ID: str | None = None

# the team the importer's per-team loop is currently processing.
# Same ambient-state rationale as the run_id: catch sites live deep in leaf
# functions, and threading team_esid through every signature would be a large
# cross-cutting change. Set by each importer's per-team loop as it iterates;
# None outside a team loop (errors then record without team attribution,
# exactly as before).
_CURRENT_TEAM_ESID: str | None = None


def set_current_team(team_esid: str | None) -> None:
    """bind the team the per-team loop is currently processing.

    Importers call this at the top of each team iteration (and with ``None``
    after the loop) so ``record_user_error`` can stamp durable per-team
    attribution onto recorded errors. Fail-safe by construction: an unset or
    stale value only affects error ATTRIBUTION, never import behavior.
    """
    global _CURRENT_TEAM_ESID
    _CURRENT_TEAM_ESID = (team_esid or "").strip() or None


def get_current_team() -> str | None:
    """The currently-bound team_esid, or None. Exposed mainly for tests."""
    return _CURRENT_TEAM_ESID


def set_run_id(run_id: str | None) -> None:
    """Bind the active run's ``progress_run_id`` for this process.

    The importer dispatcher (e.g. ``run_daily_imports``) calls this once, after
    it has resolved/adopted the run_id, so subsequent ``record_user_error``
    calls from leaf functions attach to the right progress doc. Passing None
    (the cron path, which has no run_id) clears it, making ``record_user_error``
    a no-op.
    """
    global _PROGRESS_RUN_ID
    _PROGRESS_RUN_ID = (run_id or "").strip() or None


def get_run_id() -> str | None:
    """The currently-bound run_id, or None. Exposed mainly for tests."""
    return _PROGRESS_RUN_ID


def record_user_error(raw: str, *, connector_id: str | None = None, detail: str | None = None) -> None:
    """Record a per-asset, user-readable import error for the active run.

    Call this at the catch site of a user-actionable failure that the importer
    is about to skip-and-continue past (permission denied, revoked connection,
    dead OAuth token, asset not linked, Site-Link mismatch). ``raw`` is the raw
    error string / API message; it is classified into a user-readable entry and
    appended (deduped by category + detail) to the run's progress doc by
    :meth:`ImportProgressService.record_import_error`.

    ``detail`` names the specific asset the error is about (the team, customer,
    property, channel). It is woven into the user-facing message and forms part
    of the dedup key so two different broken assets each get their own line.

    No-op when no run_id is bound (cron path) or ``raw`` is empty. Fail-soft on
    any error — a reporting hiccup must never abort the import.
    """
    run_id = _PROGRESS_RUN_ID
    if not run_id or not (raw or "").strip():
        return
    try:
        from lib.import_progress_service import ImportProgressService

        # durable per-team attribution: stamp the ambient team
        # (set by the importer's per-team loop) onto the recorded entry.
        ImportProgressService.record_import_error(
            run_id=run_id,
            raw=raw,
            connector_id=connector_id,
            detail=detail,
            team_esid=_CURRENT_TEAM_ESID,
        )
    except Exception as exc:  # noqa: BLE001 — reporting must never abort the import
        logging.warning("record_user_error failed: %s", exc)
