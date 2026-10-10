"""importer scope guard.

Every data importer is dispatched by the Report Scheduler with an explicit
scope flag (``--team_ids`` / ``--customer_ids`` / ``--property_ids`` /
``--limit_teams``) resolved from the schedule's teams. The legacy
"no flag = full-cluster scan" path was retired when moved daily imports
off cron onto the scheduler (the scheduler ALWAYS passes a scope flag). A
scope-less invocation is therefore a misconfiguration, not a cron sweep — and
falling through to a cluster-wide scan is a cross-workspace exposure (the
behavior class behind).

``require_scope`` is the single guard each importer's ``main()`` calls at entry,
BEFORE any API call / subprocess / lock acquire. It raises ``ValueError`` when
the scope flag is absent/empty, and records a *classifiable* user-facing error
(``no_data_source`` via :func:`lib.import_run_context.record_user_error`) so the
Report Scheduler surfaces "this import must be team-scoped" rather than a generic
"import didn't finish". Mirrors Facebook's existing strict-mode raise;
the resolves-to-zero half stays in each importer (it needs the connector's own
resolver) but should raise the same classifiable shape via
:func:`scope_resolution_error`.
"""

from __future__ import annotations


def require_scope(params, *, scope_param: str, connector_id: str) -> str:
    """Return the raw scope-flag value, or raise ValueError if absent/empty.

    ``params`` is the importer's CLI param dict; ``scope_param`` is the
    connector's scope flag name (``team_ids`` / ``customer_ids`` /
    ``property_ids`` / ``limit_teams``); ``connector_id`` is the dispatch id used
    for error classification (``meta_ads`` / ``google_ads`` / ``ga4`` / ``echo``
    / ``youtube`` / ``chatwoot`` / ``disciple_tools``).

    A standalone ``--flag`` (no value) arrives as ``True`` from import.py's arg
    parser; treat that as absent. The returned string is what the importer then
    resolves to concrete teams/customers/properties — a non-empty resolution is
    each importer's own second guard.
    """
    raw = params.get(scope_param) if params else None
    if not raw or raw is True:
        # Phrasing carries "zero teams" so it classifies as no_data_source
        # (import_error_classifier), surfacing "No data source linked" in the
        # scheduler rather than the generic "import didn't finish".
        msg = (
            f"{connector_id}: no --{scope_param} provided — resolved to zero teams. "
            f"This import must be team-scoped; nothing was imported."
        )
        _record(msg, connector_id)
        raise ValueError(msg)
    return raw


def scope_resolution_error(*, scope_param: str, scope_value, connector_id: str) -> ValueError:
    """Build (record + return) the ValueError an importer raises when a present
    scope flag resolves to ZERO teams/customers/properties.

    Records the classifiable error, then RETURNS the exception for the caller to
    ``raise`` — so the recording happens exactly once at the real raise site.
    Same ``no_data_source`` classification + "must be team-scoped" framing as
    :func:`require_scope`, keeping the absent and resolves-to-zero cases uniform.
    """
    msg = (
        f"{connector_id}: --{scope_param}={scope_value!r} resolved to zero teams; "
        f"this import must be team-scoped. Nothing was imported."
    )
    _record(msg, connector_id)
    return ValueError(msg)


def _record(msg: str, connector_id: str) -> None:
    """Record the classifiable error on the active run's progress doc (no-op when
    no run_id is bound, e.g. a manual CLI run). Fail-soft — recording must never
    mask the raise that follows."""
    try:
        from lib.import_run_context import record_user_error

        record_user_error(msg, connector_id=connector_id)
    except Exception:  # noqa: BLE001 — reporting must never block the guard's raise
        pass
