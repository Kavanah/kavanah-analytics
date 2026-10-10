import signal
import subprocess
from datetime import datetime, timedelta
import logging
import lib.logging


class _ImportCancelled(Exception):
    """raised by the SIGTERM handler so a cancel kill unwinds through
    the main try/finally (releasing the import lock) instead of terminating the
    process abruptly and leaving the lock to expire on its TTL."""


def _is_cancel_requested(progress_run_id):
    """fail-soft cancellation probe (mirrors the FB/GAds importers).

    True only when an on-demand run (non-None ``progress_run_id``) has been
    flipped to ``cancelling`` by the Cancel button. Cron path (None run_id)
    returns False trivially; any ES error is swallowed to False.
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
    No-op on the cron path (None run_id). Soft-fails."""
    if not progress_run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.complete_run(run_id=progress_run_id, status="cancelled")
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("complete_run(cancelled) raised: %s", exc)


def _resolve_connection_to_team_filter(connection_esid):
    """follow-up — resolve a connection_esid to ``(team_esid, principal)``.

    Echo is team-scoped (no provider asset), so the resolved team
    becomes the ``limit_teams`` filter for ``get_conversations``. The
    cron path (no ``--connection_esid``) is unaffected.

    Returns ``(team_esid, principal)`` on success or ``(None, None)`` on
    a missing connection / missing team membership.
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
    # connection from the Manage Connection page (status="revoked"
    # written via ConnectionService.set_status). Returning (None, None)
    # here aborts the daily-imports kickoff for this connection without
    # touching the cron path (no --connection_esid).
    if (doc.get("status") or "") == "revoked":
        logging.info(
            "echo importer: skipping revoked connection_esid=%s",
            connection_esid,
        )
        return (None, None)
    principal = doc.get("principal") or None
    if not principal:
        return (None, None)

    try:
        from asset_management.web.blueprints.analytics.routes import _resolve_primary_team_esid
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "connection resolve: _resolve_primary_team_esid import failed: %s",
            exc,
        )
        return (None, principal)

    team_esid = _resolve_primary_team_esid(principal)
    return (team_esid, principal)


def main(params):
    lib.logging.define("echo-run-daily-imports")

    # Determine date range to process
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    days_to_pull = params.get("days_to_pull")

    if start_date and end_date:
        # Use explicit date range if provided
        pass
    elif days_to_pull:
        # Pull specified number of days ending yesterday
        days_to_pull = int(days_to_pull)
        end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days_to_pull)).strftime("%Y-%m-%d")
    else:
        # Default to previous day only
        start_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        end_date = start_date

    # Get optional exclude_teams and limit_teams parameters
    exclude_teams = params.get("exclude_teams")
    limit_teams = params.get("limit_teams")

    # follow-up — on-demand kickoff path. The step-6 finish
    # handler passes ``--connection_esid X``; resolve it to a single
    # team_esid and use it as the limit_teams filter. The cron path
    # (no flag) is unaffected and continues to honor explicit
    # exclude_teams / limit_teams.
    connection_esid = params.get("connection_esid")
    # the Report Scheduler's Run-now dispatch pre-mints a progress doc
    # (keyed on a synthetic schedule:<id> connection) and hands us its run_id via
    # --progress_run_id, with --limit_teams carrying the scope directly (no
    # de_connection to resolve). Adopt that run_id and write progress against it,
    # exactly like FB / Google Ads / GA4. When set, this wins over the
    # connection_esid-derived path below and we skip the importer-side start_run.
    external_run_id = params.get("progress_run_id")
    progress_run_id = external_run_id or None
    progress_username = None
    if connection_esid:
        resolved_team, principal = _resolve_connection_to_team_filter(connection_esid)
        progress_username = principal
        if resolved_team:
            # On-demand path's resolved team wins over any
            # caller-supplied limit_teams (mixing is not a real call
            # path; precedence is deterministic if it ever happens).
            limit_teams = resolved_team
        else:
            logging.warning(
                "-connection_esid=%r did not resolve to a team; "
                "running unfiltered echo import without progress writes.",
                connection_esid,
            )
            connection_esid = None  # disable progress writes

    # require a team scope. The cron full-tenant path is retired
    # (imports run via the scheduler, which always passes
    # --limit_teams resolved from the schedule's teams); a scope-less invocation
    # is a misconfiguration and a tenant-wide pull is a cross-workspace exposure.
    # Bail BEFORE the lock / any Echo API call. Placed AFTER the connection_esid
    # → limit_teams resolution above.
    from lib.import_scope import require_scope

    require_scope({"limit_teams": limit_teams}, scope_param="limit_teams", connector_id="echo")

    # Check that both exclude_teams and limit_teams are not set
    if exclude_teams and limit_teams:
        logging.error("Cannot set both exclude_teams and limit_teams. Only one should be set.")
        return

    # Compute a coarse days_total for the progress doc. Echo's
    # importer doesn't iterate per date (one call to get_conversations
    # with the whole range), so update_progress is bumped at major
    # step boundaries (after each of the 4 pipeline steps below).
    try:
        days_total = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days + 1
    except (TypeError, ValueError):
        days_total = 1

    # follow-up — write the initial pending progress doc
    # synchronously so the building screen's first poll finds it.
    # skip when an explicit --progress_run_id was passed (scheduler
    # Run-now): the web layer already minted that doc and we have no
    # progress_username to start a fresh one. We just adopt its run_id.
    if connection_esid and progress_username and not external_run_id:
        try:
            from lib.import_progress_service import ImportProgressService

            progress_run_id = ImportProgressService.start_run(
                username=progress_username,
                connection_esid=connection_esid,
                connector_id="echo",
                days_total=max(1, days_total),
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("start_run raised: %s", exc)

    # follow-up LOCK-001 — cluster-level Echo importer lock.
    # Echo's auth surface is the Kavanah-managed token (not an OAuth
    # token-refresh race like FB), but the same serialisation makes
    # sense: two Echo importers running concurrently against the
    # same GraphQL endpoint risk rate-limit exhaustion. Per-connector
    # lock granularity means an Echo lock doesn't block FB or Google
    # Ads imports.
    # scope the lock PER-TENANT. The rate-limit the lock guards is
    # per-tenant (each Echo tenant has its own GraphQL endpoint / quota), so two
    # teams under DIFFERENT tenants are independent and run concurrently; two
    # imports touching the SAME tenant still serialize. We resolve this run's
    # ``limit_teams`` → their tenant set HERE (each team doc carries
    # ``echo_tenant_esid``) and key the lock on it. A cron full-pull (no
    # limit_teams) resolves to "" → the connector-GLOBAL lock, serializing
    # against everything (conservative). The scheduler stamps the SAME key via
    # ImportLockService.echo_tenant_scope_key so Cancel's force_release matches.
    lock_scope = ""
    lock_id = None
    try:
        from lib.import_lock_service import ImportLockService
        from lib.es_helpers import get_elasticsearch_client

        lock_scope = ImportLockService.echo_tenant_scope_key(
            get_elasticsearch_client(),
            limit_teams,  # comma-string or None (cron full-pull → "" → global lock)
        )
        lock_id = ImportLockService.acquire("echo", scope_id=lock_scope)
        if lock_id is None:
            logging.warning("lock: another echo importer holds the lock; " "exiting cleanly.")
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

    # install a SIGTERM handler so the Cancel button's process-group
    # kill (ImportService.cancel_import → os.killpg) unwinds through the try/
    # finally below: the import lock is released immediately instead of lingering
    # until its 30-min TTL. The previous handler (default SIG_DFL) terminated the
    # process abruptly, skipping the finally. The child subprocess.run() steps
    # are in the same process group, so they receive the same SIGTERM and exit;
    # this handler then converts our own SIGTERM into a clean unwind.
    _prev_sigterm = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(_signum, _frame):
        raise _ImportCancelled()

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):  # pragma: no cover — not in main thread / unsupported
        pass

    start_time = datetime.now()
    logging.info(f"Import started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # between-step cooperative cancel: when an on-demand run is flipped
    # to ``cancelling`` (Cancel button on the building screen), each call stops
    # the pipeline cleanly, stamps ``cancelled``, and returns (the finally still
    # releases the lock). Cron path (no run_id) is a no-op. Scope is between the
    # 6 pipeline steps, not inside get_conversations' tenant/date loop — a signal
    # kill (Part 2) handles mid-pull stops.
    def _cancel_checkpoint(stage):
        if _is_cancel_requested(progress_run_id):
            logging.info("echo importer: cancellation observed %s; stamping cancelled.", stage)
            _stamp_cancelled(progress_run_id)
            return True
        return False

    try:
        if _cancel_checkpoint("before conversations pull"):
            return

        # Build the command arguments for get_conversations.py
        cmd_args = [
            "python3",
            "/app/python/import.py",
            "echo",
            "get_conversations",
            f"--start_date={start_date}",
            f"--end_date={end_date}",
        ]

        # Add exclude_teams or limit_teams if provided
        if exclude_teams:
            cmd_args.append(f"--exclude_teams={exclude_teams}")
        if limit_teams:
            cmd_args.append(f"--limit_teams={limit_teams}")
        # thread the run's progress doc id to the created_at pull (the
        # comprehensive pass that processes every conversation) so its per-referrer
        # "asset on no team" notes can surface in the Report Scheduler. Only the
        # created pull carries it: the refresh pull re-processes the same closed
        # conversations and would re-emit identical notes (dedup would collapse
        # them, but passing once is cleaner). No-op on the cron path (None run_id).
        if progress_run_id:
            cmd_args.append(f"--progress_run_id={progress_run_id}")

        # Get Echo Conversations (created_at pull — brings NEW conversations in)
        logging.info(f"Running get_conversations.py for date range: {start_date} to {end_date}...")
        subprocess.run(cmd_args, check=True)
        _bump_progress(progress_run_id, days_total // 4 if days_total >= 4 else 1)
        if _cancel_checkpoint("after conversations pull"):
            return

        # Echo conversation REFRESH pull (closed_at window). Re-fetches
        # conversations RESOLVED recently — regardless of when they were created —
        # so their final status/outcome/disposition/message-counts upsert over the
        # stale creation-day docs. Without this, a conversation imported at creation
        # (active/no outcome) is never revisited after it closes+dispositions, so
        # our data drifts from Echo's closed_at-based dashboard.
        # The refresh self-defaults to a trailing 7-day closed_at window (the API
        # per-request span cap); pass the same explicit range when one is given.
        refresh_args = [
            "python3",
            "/app/python/import.py",
            "echo",
            "get_conversations",
            "--mode=refresh",
        ]
        if params.get("start_date") and params.get("end_date"):
            refresh_args.append(f"--start_date={start_date}")
            refresh_args.append(f"--end_date={end_date}")
        if exclude_teams:
            refresh_args.append(f"--exclude_teams={exclude_teams}")
        if limit_teams:
            refresh_args.append(f"--limit_teams={limit_teams}")
        logging.info("Running get_conversations.py --mode=refresh (closed_at refresh pull)...")
        subprocess.run(refresh_args, check=True)
        if _cancel_checkpoint("after refresh pull"):
            return

        # Echo JSON to Elastic
        logging.info("Running send_json_to_elastic.py...")
        subprocess.run(
            ["python3", "/app/python/import.py", "echo", "send_json_to_elastic"],
            check=True,
        )
        _bump_progress(progress_run_id, days_total // 2 if days_total >= 4 else max(1, days_total // 2))
        if _cancel_checkpoint("after send-to-elastic"):
            return

        # Archive Imported JSON
        logging.info("Running archive_imported_json.py...")
        subprocess.run(
            ["python3", "/app/python/import.py", "echo", "archive_imported_json"],
            check=True,
        )
        _bump_progress(progress_run_id, (3 * days_total) // 4 if days_total >= 4 else max(1, days_total - 1))
        if _cancel_checkpoint("after archive"):
            return

        # Get Echo User Stats
        logging.info(f"Running get_user_stats.py for date range: {start_date} to {end_date}...")
        user_stats_args = [
            "python3",
            "/app/python/import.py",
            "echo",
            "get_user_stats",
            f"--start_date={start_date}",
            f"--end_date={end_date}",
        ]
        # forward the team scope so the user-stats step only contacts
        # the tenants owning the targeted teams (same scoping as the two
        # get_conversations calls above).
        if limit_teams:
            user_stats_args.append(f"--limit_teams={limit_teams}")
        subprocess.run(user_stats_args, check=True)

        # Send User Stats to Elastic
        logging.info("Running send_user_stats_to_elastic.py...")
        subprocess.run(
            ["python3", "/app/python/import.py", "echo", "send_user_stats_to_elastic"],
            check=True,
        )

        logging.info("All tasks completed successfully.")

        # follow-up — terminal success stamp for the on-demand
        # path. Cron path (no run_id) is a no-op.
        if progress_run_id:
            try:
                from lib.import_progress_service import ImportProgressService

                ImportProgressService.update_progress(run_id=progress_run_id, days_fetched=days_total)
                ImportProgressService.complete_run(run_id=progress_run_id, status="succeeded")
            except Exception as exc:  # noqa: BLE001
                logging.warning("complete_run(succeeded) raised: %s", exc)
    except _ImportCancelled:
        # our SIGTERM handler fired (Cancel button's process-group
        # kill). Unwind cleanly: stamp cancelled and fall through to the finally
        # so the lock is released immediately (not left to its TTL).
        logging.info("echo importer: SIGTERM received; cancelling and releasing lock.")
        _stamp_cancelled(progress_run_id)
    except subprocess.CalledProcessError as e:
        # a child step dying from the group SIGTERM surfaces here as a
        # non-zero exit. If a cancel was requested, this is a cancellation, not a
        # failure — stamp cancelled so the banner is accurate.
        if _is_cancel_requested(progress_run_id):
            logging.info("echo importer: child exited under cancellation; stamping cancelled.")
            _stamp_cancelled(progress_run_id)
        else:
            logging.error(f"An error occurred: {e}")
            if progress_run_id:
                try:
                    from lib.import_progress_service import ImportProgressService

                    ImportProgressService.complete_run(run_id=progress_run_id, status="failed", error=str(e))
                except Exception as exc:  # noqa: BLE001
                    logging.warning("complete_run(failed) raised: %s", exc)
    finally:
        # restore the previous SIGTERM disposition so we don't leak our
        # handler into anything that imports this module.
        try:
            signal.signal(signal.SIGTERM, _prev_sigterm)
        except (ValueError, OSError, TypeError):  # pragma: no cover
            pass

        # follow-up LOCK-001 — release the lock so the next
        # echo importer can proceed. this now runs on a cancel kill
        # too (the SIGTERM handler raises _ImportCancelled instead of aborting).
        if lock_id:
            try:
                from lib.import_lock_service import ImportLockService

                # release the SAME (connector, scope_id) we acquired.
                ImportLockService.release("echo", lock_id, scope_id=lock_scope)
            except Exception as exc:  # noqa: BLE001
                logging.warning("lock release raised: %s", exc)

        end_time = datetime.now()
        elapsed = end_time - start_time
        minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
        logging.info(f"Import ended at {end_time.strftime('%Y-%m-%d %H:%M:%S')} | Total time: {minutes}m {seconds}s")


def _bump_progress(run_id, days_fetched):
    """follow-up — best-effort progress bump.

    Soft-fails so a progress write outage doesn't abort the import.
    No-op when run_id is None (cron path).
    """
    if not run_id:
        return
    try:
        from lib.import_progress_service import ImportProgressService

        ImportProgressService.update_progress(run_id=run_id, days_fetched=days_fetched)
    except Exception as exc:  # noqa: BLE001
        logging.warning("update_progress raised: %s", exc)
