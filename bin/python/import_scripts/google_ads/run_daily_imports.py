"""
Google Ads Daily Import Orchestrator

Orchestrates the complete Google Ads app campaign import workflow:
1. Ensure Google Ads Elasticsearch template exists
2. Fetch app campaign data from Google Ads API
3. Index JSON files to Elasticsearch
4. Archive processed files

Usage:
    python3 /app/python/import.py google_ads run_daily_imports [--start_date=YYYY-MM-DD] [--end_date=YYYY-MM-DD]
"""

import subprocess
import logging
import lib.logging
from datetime import datetime, timedelta


def _is_cancel_requested(progress_run_id):
    """fail-soft cancellation probe (mirrors the FB importer).

    True only when an on-demand run (non-None ``progress_run_id``) has been
    flipped to ``cancelling`` by the dashboard Cancel button. Cron path (None
    run_id) returns False trivially; any ES error is swallowed to False.
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

    No-op on the cron path (None run_id). Soft-fails so a progress-write error
    doesn't mask that the import was cancelled.
    """
    if not progress_run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.complete_run(run_id=progress_run_id, status="cancelled")
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("complete_run(cancelled) raised: %s", exc)


def _resolve_connection_to_customer_filter(connection_esid):
    """follow-up — resolve a connection_esid to ``(customer_ids, principal)``.

    Google Ads is customer-scoped (provider asset =
    ``google_ads_customer_id``). The wizard's connection_asset rows
    carry the user's selected customer ids in ``provider_asset_id``.
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
    # connection from the Manage Connection page. Tokenless provider, so
    # the gate is just on connection.status — the importer trusts
    # cluster-wide service credentials but skips the customer-list join.
    if (doc.get("status") or "") == "revoked":
        logging.info(
            "google_ads importer: skipping revoked connection_esid=%s",
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

    customer_ids = []
    for hit in assets:
        src = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(src, dict):
            continue
        pid = src.get("provider_asset_id")
        if pid:
            customer_ids.append(str(pid))
    if not customer_ids:
        return (None, principal)
    return (",".join(customer_ids), principal)


def main(params):
    """
    Main entry point from import.py.

    Orchestrates the full Google Ads app campaign import pipeline using subprocess calls.

    Args:
        params: Dict of command-line parameters
                - start_date: Start date (YYYY-MM-DD), defaults to 30 days ago
                - end_date: End date (YYYY-MM-DD), defaults to yesterday
                - customer_ids: Optional comma-separated customer IDs
                - connection_esid: follow-up — on-demand
                  wizard kickoff. Resolves to (customer_ids,
                  principal). Cron path (no flag) unchanged.
    """
    lib.logging.define("google-ads-run-daily-imports")

    # Get date parameters
    end_date = params.get("end_date")
    start_date = params.get("start_date")
    customer_ids = params.get("customer_ids")

    # follow-up — on-demand kickoff resolves the
    # connection_esid to a comma-separated customer_ids filter so the
    # importer only pulls the user's freshly-connected accounts (not
    # every customer in the cluster).
    connection_esid = params.get("connection_esid")
    # dashboard on-demand range path may hand us a pre-minted run_id
    # (synthetic legacy_team: key) + a direct --customer_ids scope filter for
    # service-account teams with no de_connection. Write progress against this
    # run_id so the banner poller finds it. Cron passes neither flag.
    external_run_id = params.get("progress_run_id")
    progress_run_id = external_run_id or None
    progress_username = None
    if connection_esid:
        resolved_ids, principal = _resolve_connection_to_customer_filter(connection_esid)
        progress_username = principal
        if resolved_ids:
            customer_ids = resolved_ids
        else:
            logging.warning(
                "-connection_esid=%r did not resolve to customer_ids; "
                "running unfiltered google_ads import without progress writes.",
                connection_esid,
            )
            connection_esid = None

    # require a customer scope. The cron full-cluster path is retired
    # (imports run via the scheduler, which always passes
    # --customer_ids); a scope-less invocation is a misconfiguration and a
    # cluster-wide pull is a cross-workspace exposure. Bail BEFORE the lock / any
    # Google Ads API call. Placed AFTER the connection_esid → customer_ids
    # resolution above. customer_ids may be a list ([] = empty) — require_scope
    # treats falsy as absent.
    from lib.import_scope import require_scope

    require_scope({"customer_ids": customer_ids}, scope_param="customer_ids", connector_id="google_ads")

    # Default dates if not provided
    if not end_date:
        end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if not start_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_date = (end_dt - timedelta(days=30)).strftime("%Y-%m-%d")

    logging.info(f"Starting Google Ads import ({start_date} to {end_date})")

    # Compute days_total for the progress doc.
    try:
        days_total = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days + 1
    except (TypeError, ValueError):
        days_total = 1

    # Mint our own progress run ONLY when the caller didn't supply one
    # (synthetic-key path passes progress_run_id directly).
    if not external_run_id and connection_esid and progress_username:
        try:
            from lib.import_progress_service import ImportProgressService

            progress_run_id = ImportProgressService.start_run(
                username=progress_username,
                connection_esid=connection_esid,
                connector_id="google_ads",
                days_total=max(1, days_total),
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("start_run raised: %s", exc)

    # follow-up LOCK-001 — Google Ads import lock.
    # scope the lock to THIS dispatch's customer set so two different
    # teams' Google Ads imports run in parallel; only same-customer-set imports
    # serialize (preserving the double-click + orphan-resolve).
    # Auth is per-customer OAuth decrypted per run, so there's no shared
    # mutable credential forcing a connector-global lock. The cron full-pull (no
    # customer_ids) keeps the connector-GLOBAL lock (empty scope_id) so it still
    # serializes against everything.
    lock_scope = ""
    lock_id = None
    try:
        from lib.import_lock_service import ImportLockService

        lock_scope = ImportLockService.scope_key_for_ids(customer_ids)
        # progress_run_id: queue position + cancel while waiting for a global slot.
        lock_id = ImportLockService.acquire("google_ads", scope_id=lock_scope, progress_run_id=progress_run_id)
        if lock_id is None:
            logging.warning("lock: another google_ads importer holds the lock; " "exiting cleanly.")
            # resolve our progress doc IFF it's a true orphan (the lock
            # winner is a DIFFERENT run that will never finalize it). If a
            # same-schedule winner adopted this same run_id and is progressing it,
            # resolve_orphaned_lock_loser leaves it alone (preserves the
            # double-click protection). Fail-soft.
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
        # (2026-05-21): removed the per-pattern
        # create_google_ads_template invocation. The project-wide
        # default template installed at app startup governs new index
        # creation; Google Ads indices land as regular indices without
        # per-pattern intervention.
        _bump_progress(progress_run_id, max(1, days_total // 4))

        # between-step cancel checkpoint (mirrors the FB importer).
        # No-op on cron (progress_run_id None); lock released in finally on return.
        if _is_cancel_requested(progress_run_id):
            logging.info("gads-importer: cancellation observed before fetch; stamping cancelled.")
            _stamp_cancelled(progress_run_id)
            return

        # Step 1: Fetch app campaign data from Google Ads API
        logging.info(f"Running get_campaign_data.py...")
        cmd = [
            "python3",
            "/app/python/import.py",
            "google_ads",
            "get_campaign_data",
            f"--start_date={start_date}",
            f"--end_date={end_date}",
        ]
        if customer_ids:
            cmd.append(f"--customer_ids={customer_ids}")
        # thread the run_id so the fetch child can honor a cancel
        # mid-pull (between customers), not just between steps.
        if progress_run_id:
            cmd.append(f"--progress_run_id={progress_run_id}")

        subprocess.run(cmd, check=True)
        _bump_progress(progress_run_id, max(1, days_total // 2))

        # checkpoint between fetch and send (fetch is the long step;
        # a cancel during it is observed here when the child returns).
        if _is_cancel_requested(progress_run_id):
            logging.info(
                "gads-importer: cancellation observed after fetch; stamping cancelled (fetched data retained)."
            )
            _stamp_cancelled(progress_run_id)
            return

        # Step 2: Send JSON files to Elasticsearch
        logging.info("Running send_json_to_elastic.py...")
        subprocess.run(
            ["python3", "/app/python/import.py", "google_ads", "send_json_to_elastic"],
            check=True,
        )
        _bump_progress(progress_run_id, max(1, (3 * days_total) // 4))

        # Step 3: Archive processed JSON files
        logging.info("Running archive_imported_json.py...")
        subprocess.run(
            ["python3", "/app/python/import.py", "google_ads", "archive_imported_json"],
            check=True,
        )

        logging.info("Google Ads import completed successfully")

        # follow-up — terminal success stamp.
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
        # follow-up LOCK-001 — release the lock.
        if lock_id:
            try:
                from lib.import_lock_service import ImportLockService

                ImportLockService.release("google_ads", lock_id, scope_id=lock_scope)
            except Exception as exc:  # noqa: BLE001
                logging.warning("lock release raised: %s", exc)

        end_time = datetime.now()
        elapsed = end_time - start_time
        minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
        logging.info(f"Import ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} | Total time: {minutes}m {seconds}s")


def _bump_progress(run_id, days_fetched):
    """follow-up — best-effort progress bump."""
    if not run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.update_progress(run_id=run_id, days_fetched=days_fetched)
    except Exception as exc:  # noqa: BLE001
        logging.warning("update_progress raised: %s", exc)


if __name__ == "__main__":
    # For standalone testing
    main({"start_date": None, "end_date": None})
