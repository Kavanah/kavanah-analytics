"""
Google Analytics Daily Import Orchestrator

Orchestrates the complete GA4 import workflow:
1. Fetch GA4 metrics from all accessible properties
2. Index JSON files to Elasticsearch
3. Archive processed files

Usage:
    python3 /app/python/import.py google_analytics run_daily_imports [--days_to_pull=N]
    python3 /app/python/import.py google_analytics run_daily_imports --connection_esid X [--start_date=...] [--end_date=...]
"""

import subprocess
import logging
import lib.logging
from datetime import datetime, timedelta, timezone


def _is_cancel_requested(progress_run_id):
    """fail-soft cancellation probe (mirrors the FB importer).

    True only when an on-demand run (non-None ``progress_run_id``) has been
    flipped to ``cancelling`` by the dashboard Cancel button. The cron path
    (None run_id) returns False trivially. Any ES error is swallowed to False
    so a transient blip doesn't abort an import the user hasn't cancelled.
    """
    if not progress_run_id:
        return False
    try:
        from lib.import_progress_service import ImportProgressService

        return ImportProgressService.is_cancelling(progress_run_id)
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("is_cancelling raised: %s — proceeding", exc)
        return False


def _stamp_cancelled(progress_run_id):
    """write the terminal ``cancelled`` stamp for an on-demand run.

    No-op on the cron path (None run_id). Soft-fails: a progress-write error
    must not mask that the import was cancelled.
    """
    if not progress_run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.complete_run(run_id=progress_run_id, status="cancelled")
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("complete_run(cancelled) raised: %s", exc)


# rolling dedup-window upkeep for GA4 (the FB pattern).
# A scheduled daily GA4 import resolves to a single day (rolling_1d unless the
# schedule carries days_to_pull > 1) and the window step below skips single-day
# ranges, so a plain daily schedule never wrote a {team_esid}__ga_metrics_window
# doc. On prod (2026-09-04) only 4 of 11 GA-enabled teams had a window index —
# the four with a multi-day schedule or a manual range pull — and the GA4
# first-look charts that read the window index rendered blank for the other 7,
# with nothing saying so: the step is best-effort and the parent stayed green.
# The scheduler now opts the GA importer in (--rolling_windows=true) and the run
# keeps a yesterday-anchored last-7 / 30 / 90 window fresh per scoped
# property, so the dashboard's nearest-window snap always has a ≤1-day-old
# deduplicated value. Manual paths (Import Data page / wizard / backfill) are
# untouched — they pull exactly the requested range.
_ROLLING_WINDOW_SPANS = (7, 30, 90)
# plus the last-90: the dashboards page DEFAULT range (now-90d/d)
# otherwise snapped to the last-30 window.


def _rolling_window_dates(span_days, today=None):
    """``[since, until]`` for the yesterday-anchored rolling window of
    ``span_days`` days (until = yesterday UTC; today's partial day would bake an
    incomplete final day into the window). Mirrors FB's helper."""
    anchor = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)
    start = anchor - timedelta(days=span_days - 1)
    return [start.isoformat(), anchor.isoformat()]


def _rolling_windows_requested(params):
    """True when the scheduler asked for rolling dedup-window upkeep
    (``--rolling_windows=true``). Same truthy set as FB's ``maturation_tail``."""
    return params.get("rolling_windows") in (True, "true", "True", "1", 1)


def _run_rolling_window_upkeep(start_date, end_date, range_status, property_ids, today=None):
    """pull the yesterday-anchored rolling windows and report per span.

    Returns ``{span_days: status}`` where status is :func:`_run_ga_window_step`'s
    string, or ``"covered"`` when the range the run just pulled equalled that
    span and succeeded (a days_to_pull=7 daily schedule IS the last-7 window —
    no second GA4 call). No ``progress_run_id`` is threaded: this upkeep must
    not touch the run's progress doc (the pill-rebase trap, as in FB).
    Any ``"failed"`` span is summarised at WARNING so the reason a team's window
    index did not advance is in the parent's log rather than only in the
    swallowed child's.
    """
    outcome = {}
    for span_days in _ROLLING_WINDOW_SPANS:
        since, until = _rolling_window_dates(span_days, today=today)
        if range_status == "ok" and (since, until) == (start_date, end_date):
            outcome[span_days] = "covered"
            continue
        outcome[span_days] = _run_ga_window_step(since, until, property_ids, None)
    summary = ", ".join(f"{span}d={status}" for span, status in outcome.items())
    if "failed" in outcome.values():
        logging.warning("ga-window upkeep: %s — the window index did not advance for the failed span(s).", summary)
    else:
        logging.info("ga-window upkeep: %s", summary)
    return outcome


def _run_ga_window_step(start_date, end_date, property_ids, progress_run_id):
    """P3 — deduplicated property-level window pull over [start, end].

    Best-effort sibling to the per-day pass (mirrors FB's _run_window_reach_step):
    get_ga_metrics_window writes one deduped row per property to the staging dir,
    then send_ga_window_json_to_elastic indexes it to {team_esid}__ga_metrics_window.

    Skipped when:
      * no explicit range (start_date/end_date) was given — a days_to_pull
        invocation has no window bounds to pull.
      * the range is a single day (window == day; the per-day doc already holds
        that day's trivially-deduplicated figure). NB the scheduled daily GA4 run
        IS a single-day range (rolling_1d), which is why added the
        rolling upkeep in main() — this step alone never populated the window
        index for a plain daily schedule.

    Any failure is logged and swallowed so it can't fail a per-day import that
    already completed.

    Returns a status string (so the caller can say WHY the window
    index was not written instead of the parent staying silently green):
    ``"ok"`` (window docs sent), ``"skipped"`` (no range / single day),
    ``"failed"`` (subprocess error — still swallowed).
    """
    if not (start_date and end_date):
        logging.info("ga-window: no explicit range (days_to_pull path); skipping windowed dedup pull.")
        return "skipped"
    if start_date == end_date:
        logging.info("ga-window: single-day range %s; skipping (no dedup benefit).", start_date)
        return "skipped"
    logging.info("ga-window: pulling deduplicated window %s..%s", start_date, end_date)
    try:
        cmd = [
            "python3",
            "/app/python/import.py",
            "google_analytics",
            "get_ga_metrics_window",
            f"--since={start_date}",
            f"--until={end_date}",
        ]
        if property_ids:
            cmd.append(f"--property_ids={property_ids}")
        if progress_run_id:
            cmd.append(f"--progress_run_id={progress_run_id}")
        subprocess.run(cmd, check=True)

        subprocess.run(
            ["python3", "/app/python/import.py", "google_analytics", "send_ga_window_json_to_elastic"],
            check=True,
        )
        logging.info("ga-window: completed for %s..%s", start_date, end_date)
        return "ok"
    except Exception as exc:  # noqa: BLE001 — boundary: never fail the per-day import
        logging.warning(
            "ga-window pull for %s..%s failed: %s — per-day import is unaffected.",
            start_date,
            end_date,
            exc,
        )
        return "failed"


def _resolve_connection_to_property_filter(connection_esid):
    """resolve a connection_esid to ``(property_ids, principal)``.

    GA4 is property-scoped (provider asset = ``properties/<id>``).
    The wizard's connection_asset rows carry the user's selected
    property_ids in ``provider_asset_id``. Mirrors the Google Ads
    shape follow-up IMPORTER-002.

    Returns ``("123,456", principal)`` on the happy path or
    ``(None, principal_or_None)`` on a missing connection /
    missing assets.
    """
    try:
        from asset_management.web.services.connection_service import ConnectionService
    except Exception as exc:  # noqa: BLE001
        logging.warning("connection resolve: import failed: %s", exc)
        return (None, None)

    doc = ConnectionService.get_connection(connection_esid)
    if not doc:
        return (None, None)
    # pause imports when the user has disconnected this
    # connection from the Manage Connection page. Tokenless provider,
    # so the gate is just on connection.status — the importer trusts
    # cluster-wide GA4 service credentials but skips the property-list
    # join.
    if (doc.get("status") or "") == "revoked":
        logging.info(
            "google_analytics importer: skipping revoked connection_esid=%s",
            connection_esid,
        )
        return (None, None)
    principal = doc.get("principal") or None
    if not principal:
        return (None, None)

    try:
        assets = ConnectionService.list_assets_for_connection(connection_esid)
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "connection resolve: list_assets failed for %r: %s",
            connection_esid,
            exc,
        )
        return (None, principal)

    property_ids = []
    for hit in assets:
        src = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(src, dict):
            continue
        pid = src.get("provider_asset_id")
        if pid:
            property_ids.append(str(pid))
    if not property_ids:
        return (None, principal)
    return (",".join(property_ids), principal)


def main(params):
    """
    Main entry point from import.py.

    Orchestrates the full GA4 import pipeline using subprocess calls.

    Args:
        params: Dict of command-line parameters
                - days_to_pull: Number of days to pull (default: 1)
                - start_date / end_date: Explicit date range (overrides days_to_pull)
                - property_ids: Optional comma-separated property ids filter
                - connection_esid: on-demand wizard kickoff.
                  Resolves to (property_ids, principal). Cron path
                  (no flag) is unaffected.
    """
    lib.logging.define("ga-run-daily-imports")

    # Explicit date range takes priority over days_to_pull
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    days_to_pull = params.get("days_to_pull", 1)
    property_ids = params.get("property_ids")

    # on-demand kickoff. Resolves the wizard's connection_esid
    # to a comma-separated property_ids filter so the importer only
    # pulls the user's freshly-connected properties (not every
    # property in the cluster).
    connection_esid = params.get("connection_esid")
    # the dashboard on-demand range path may dispatch a
    # pre-minted run_id (synthetic legacy_team: progress key) together with a
    # direct --property_ids scope filter, for service-account teams that have
    # no de_connection row. When present we WRITE progress against this run_id
    # (not a freshly-minted one) so the dashboard banner's poller — which polls
    # the web-layer's run_id — actually finds the doc. The cron path passes
    # neither flag, so this branch is on-demand-only.
    external_run_id = params.get("progress_run_id")
    progress_run_id = external_run_id or None
    progress_username = None
    if connection_esid:
        resolved_ids, principal = _resolve_connection_to_property_filter(connection_esid)
        progress_username = principal
        if resolved_ids:
            property_ids = resolved_ids
        else:
            logging.warning(
                "-connection_esid=%r did not resolve to property_ids; "
                "running unfiltered GA4 import without progress writes.",
                connection_esid,
            )
            connection_esid = None

    # require a property scope. The cron full-cluster path is retired
    # (imports run via the scheduler, which always passes
    # --property_ids); a scope-less invocation is a misconfiguration and a
    # cluster-wide pull is a cross-workspace exposure. Bail BEFORE any GA4 API
    # call / lock. Placed AFTER the connection_esid → property_ids resolution.
    # property_ids may be a list ([] = empty) — require_scope treats falsy as absent.
    from lib.import_scope import require_scope

    require_scope({"property_ids": property_ids}, scope_param="property_ids", connector_id="ga4")

    if start_date and end_date:
        logging.info(f"Starting Google Analytics import for date range {start_date} to {end_date}")
    else:
        logging.info(f"Starting Google Analytics daily import (pulling last {days_to_pull} days)")

    # Compute days_total for the progress doc.
    try:
        if start_date and end_date:
            days_total = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days + 1
        else:
            days_total = int(days_to_pull)
    except (TypeError, ValueError):
        days_total = 1

    # Mint our own progress run ONLY when the caller didn't hand us one
    # (synthetic-key path supplies progress_run_id directly). The
    # wizard/connection path mints here, keyed on connection_esid.
    if not external_run_id and connection_esid and progress_username:
        try:
            from lib.import_progress_service import ImportProgressService

            progress_run_id = ImportProgressService.start_run(
                username=progress_username,
                connection_esid=connection_esid,
                connector_id="google_analytics_4",
                days_total=max(1, days_total),
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("start_run raised: %s", exc)

    # GA4 importer lock.
    # scope the lock to THIS dispatch's property set so two different
    # teams' GA4 imports run in parallel; only same-property-set imports serialize
    # (preserving double-click + orphan-resolve). Auth is
    # per-property OAuth resolved per run, no shared mutable credential.
    # The cron full-pull (no property_ids) keeps the connector-GLOBAL lock.
    lock_scope = ""
    lock_id = None
    try:
        from lib.import_lock_service import ImportLockService

        lock_scope = ImportLockService.scope_key_for_ids(property_ids)
        # progress_run_id: queue position + cancel while waiting for a global slot.
        lock_id = ImportLockService.acquire("google_analytics", scope_id=lock_scope, progress_run_id=progress_run_id)
        if lock_id is None:
            logging.warning("lock: another google_analytics importer holds the lock; exiting cleanly.")
            # resolve our progress doc IFF it's a true orphan (the lock
            # winner is a DIFFERENT run that will never finalize it). A
            # same-schedule winner adopting this same run_id and progressing it is
            # left alone (preserves the double-click protection). Fail-soft.
            if progress_run_id:
                try:
                    from lib.import_progress_service import ImportProgressService

                    ImportProgressService.resolve_orphaned_lock_loser(progress_run_id)
                except Exception as exc:  # noqa: BLE001
                    logging.warning("lock: resolve_orphaned_lock_loser raised: %s", exc)
            return
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("lock: acquire raised: %s — proceeding", exc)

    start_time = datetime.now()
    logging.info(f"Import started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        # between-step cancel checkpoint (mirrors the FB importer's
        # between-stage checks). A cancel that lands before a step is observed
        # here and stamps the doc terminal, keeping whatever earlier steps wrote.
        # No-op on the cron path (progress_run_id is None). The lock is released
        # in the finally block on return.
        if _is_cancel_requested(progress_run_id):
            logging.info("ga-importer: cancellation observed before fetch; stamping cancelled.")
            _stamp_cancelled(progress_run_id)
            return

        # Step 1: Fetch GA4 metrics from all properties
        get_metrics_cmd = [
            "python3",
            "/app/python/import.py",
            "google_analytics",
            "get_ga_metrics",
        ]
        if start_date and end_date:
            get_metrics_cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
        else:
            get_metrics_cmd.append(f"--days_to_pull={days_to_pull}")
        if property_ids:
            get_metrics_cmd.append(f"--property_ids={property_ids}")
        # thread the run_id into the fetch child so it can honor a
        # cancel mid-pull (between properties), not just between steps.
        if progress_run_id:
            get_metrics_cmd.append(f"--progress_run_id={progress_run_id}")

        logging.info(f"Running get_ga_metrics.py...")
        subprocess.run(get_metrics_cmd, check=True)
        _bump_progress(progress_run_id, max(1, days_total // 3))

        # checkpoint between fetch and send (the fetch is the long
        # step; a cancel during it is observed here when the child returns).
        if _is_cancel_requested(progress_run_id):
            logging.info("ga-importer: cancellation observed after fetch; stamping cancelled (fetched data retained).")
            _stamp_cancelled(progress_run_id)
            return

        # Step 2: Send JSON files to Elasticsearch
        logging.info("Running send_json_to_elastic.py...")
        subprocess.run(
            [
                "python3",
                "/app/python/import.py",
                "google_analytics",
                "send_json_to_elastic",
            ],
            check=True,
        )
        _bump_progress(progress_run_id, max(1, (2 * days_total) // 3))

        # Step 3: Archive processed JSON files
        logging.info("Running archive_imported_json.py...")
        subprocess.run(
            [
                "python3",
                "/app/python/import.py",
                "google_analytics",
                "archive_imported_json",
            ],
            check=True,
        )

        # deduplicated window pull. The per-day docs above carry
        # activeUsers/totalUsers broken down by date + 8 dimensions, so the
        # dashboard's unique-users tile (sum agg) over-counts over a range. After
        # the per-day pass, fire ONE property-level window pull over
        # [start_date, end_date] so {team_esid}__ga_metrics_window carries GA4's
        # deduplicated counts for the range (mirrors FB's window-reach
        # step). Best-effort: a window-pull failure must not fail the per-day
        # import that already succeeded. Skipped for a single-day range (the
        # per-day doc already holds that day's trivially-deduplicated figure) and
        # when no explicit range was given (days_to_pull path).
        range_status = _run_ga_window_step(start_date, end_date, property_ids, progress_run_id)

        # rolling dedup-window upkeep (see _ROLLING_WINDOW_SPANS).
        # Scheduler path only (--rolling_windows=true); manual paths pull their
        # exact range and nothing more. Runs after the per-day pass succeeded,
        # like the FB upkeep, and never fails the import.
        if _rolling_windows_requested(params):
            _run_rolling_window_upkeep(start_date, end_date, range_status, property_ids)

        logging.info("Google Analytics import completed successfully")

        # terminal success stamp for the on-demand path.
        if progress_run_id:
            try:
                from lib.import_progress_service import ImportProgressService

                ImportProgressService.update_progress(run_id=progress_run_id, days_fetched=days_total)
                ImportProgressService.complete_run(run_id=progress_run_id, status="succeeded")
            except Exception as exc:  # noqa: BLE001
                logging.warning("complete_run(succeeded) raised: %s", exc)

    except subprocess.CalledProcessError as e:
        logging.error(f"Import failed: {e}")
        if progress_run_id:
            try:
                from lib.import_progress_service import ImportProgressService

                ImportProgressService.complete_run(run_id=progress_run_id, status="failed", error=str(e))
            except Exception as exc:  # noqa: BLE001
                logging.warning("complete_run(failed) raised: %s", exc)
        raise
    finally:
        # release the lock.
        if lock_id:
            try:
                from lib.import_lock_service import ImportLockService

                ImportLockService.release("google_analytics", lock_id, scope_id=lock_scope)
            except Exception as exc:  # noqa: BLE001
                logging.warning("lock release raised: %s", exc)

        end_time = datetime.now()
        elapsed = end_time - start_time
        minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
        logging.info(f"Import ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} | Total time: {minutes}m {seconds}s")


def _bump_progress(run_id, days_fetched):
    """best-effort progress bump."""
    if not run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.update_progress(run_id=run_id, days_fetched=days_fetched)
    except Exception as exc:  # noqa: BLE001
        logging.warning("update_progress raised: %s", exc)


if __name__ == "__main__":
    # For standalone testing
    main({"days_to_pull": 7})
