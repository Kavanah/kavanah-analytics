"""
Import Service - Handles data import operations from the web interface
"""

import os
import re
import subprocess
import json
import uuid
import logging
from datetime import datetime, timedelta

from lib.paths import UnsafePathError, safe_path_join


class ImportService:
    """Service for managing data imports from the web UI."""

    LOG_DIR = "/app/data/logs/"
    MANUAL_LOG_FILE = "/app/data/logs/imports_manual.log"
    STATUS_DIR = "/app/data/import_status/"

    # phase 4: import_id is the first 8 hex chars of uuid4 (see
    # run_imports). Reject anything else before it can flow into a path.
    _IMPORT_ID_RE = re.compile(r"^[0-9a-f]{1,32}$")

    # Data paths for each import source
    DATA_PATHS = {
        "facebook_ads": {
            "pending": "/app/data/facebook/ad_insights",
            "imported": ["/app/data/facebook/ad_insights_imported"],
        },
        "facebook_pages": {
            "pending": "/app/data/facebook/page_insights",
            "imported": ["/app/data/facebook/page_insights_imported"],
        },
        "echo": {
            "pending": "/app/data/echo/conversations",
            "imported": [
                "/app/data/echo/conversations_closed",
                "/app/data/echo/conversations_expired",
            ],
        },
        "google_analytics": {
            "pending": "/app/data/google_analytics/ga_metrics",
            "imported": ["/app/data/google_analytics/ga_metrics_imported"],
        },
        "google_ads": {
            "pending": "/app/data/google_ads/app_campaigns",
            "imported": ["/app/data/google_ads/app_campaigns_imported"],
        },
        "instagram": {
            "pending": "/app/data/instagram/ig_insights",
            "imported": ["/app/data/instagram/ig_insights_imported"],
        },
        "youtube": {
            "pending": "/app/data/youtube_analytics/channel_metrics",
            "imported": ["/app/data/youtube_analytics/channel_metrics_imported"],
        },
        "chatwoot": {"pending": "/app/data/chatwoot/reports", "imported": []},
        # Bitly link-clicks importer staging dirs (Phase 1 layout).
        "bitly": {
            "pending": "/app/data/bitly/link_clicks",
            "imported": ["/app/data/bitly/link_clicks_imported"],
        },
        # TikTok organic: each run stages under its own sub-directory.
        "tiktok": {
            "pending": "/app/data/tiktok/organic",
            "imported": ["/app/data/tiktok/organic_imported"],
        },
        # DT pull importer: raw site posts staged here, archived after indexing.
        "disciple_tools": {
            "pending": "/app/data/disciple_tools/raw",
            "imported": ["/app/data/disciple_tools/raw_imported"],
        },
    }

    @staticmethod
    def _ensure_status_dir():
        """Ensure the status directory exists."""
        if not os.path.exists(ImportService.STATUS_DIR):
            os.makedirs(ImportService.STATUS_DIR, exist_ok=True)

    @staticmethod
    def _get_status_file(import_id):
        """Get the path to a status file for an import.

        Raises ``UnsafePathError`` if ``import_id`` is not a hex string or
        if the resulting path would escape ``STATUS_DIR``.
        """
        if not isinstance(import_id, str) or not ImportService._IMPORT_ID_RE.match(import_id):
            raise UnsafePathError(f"Invalid import_id: {import_id!r}")
        return safe_path_join(ImportService.STATUS_DIR, f"{import_id}.json")

    @staticmethod
    def _write_status(import_id, status_data):
        """Write status to file."""
        ImportService._ensure_status_dir()
        status_file = ImportService._get_status_file(import_id)
        with open(status_file, "w") as f:
            json.dump(status_data, f)

    @staticmethod
    def _read_status(import_id):
        """Read status from file.

        Returns ``None`` if the import_id is invalid or the file is missing.
        """
        try:
            status_file = ImportService._get_status_file(import_id)
        except UnsafePathError:
            return None
        if not os.path.exists(status_file):
            return None
        try:
            with open(status_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    @staticmethod
    def delete_manual_log():
        """
        Delete the manual import log file if it exists.

        Returns:
            Dictionary with success status and message
        """
        log_path = ImportService.MANUAL_LOG_FILE

        if not os.path.exists(log_path):
            return {
                "success": True,
                "message": "No manual import log exists",
                "deleted": False,
            }

        try:
            os.remove(log_path)
            return {
                "success": True,
                "message": f"Deleted {os.path.basename(log_path)}",
                "deleted": True,
            }
        except Exception:
            logging.exception("ImportService.delete_manual_log: could not delete %s", log_path)
            return {
                "success": False,
                "message": "Failed to delete log.",
                "deleted": False,
            }

    @staticmethod
    def delete_recent_data(imports_config):
        """
        Delete recent JSON files for specified sources and date ranges.

        Args:
            imports_config: List of dicts with 'source' and either 'days' or 'start_date'/'end_date'

        Returns:
            Dictionary with deletion results per source
        """
        results = {}

        for config in imports_config:
            source = config["source"]
            days = config.get("days")
            start_date = config.get("start_date")
            end_date = config.get("end_date")

            if source not in ImportService.DATA_PATHS:
                results[source] = {"success": False, "error": "Unknown source"}
                continue

            paths = ImportService.DATA_PATHS[source]
            all_dirs = [paths["pending"]] + paths["imported"]

            deleted_count = 0
            errors = []

            # Calculate time range
            if start_date and end_date:
                start_cutoff = datetime.strptime(start_date, "%Y-%m-%d")
                end_cutoff = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                time_desc = f"{start_date} to {end_date}"
            elif days:
                end_cutoff = datetime.now()
                start_cutoff = end_cutoff - timedelta(days=days)
                time_desc = f"last {days} days"
            else:
                results[source] = {"success": False, "error": "No time range specified"}
                continue

            # Delete files in range
            for directory in all_dirs:
                if not os.path.exists(directory):
                    continue

                try:
                    for root, dirs, files in os.walk(directory):
                        for filename in files:
                            if not filename.endswith(".json"):
                                continue

                            filepath = os.path.join(root, filename)
                            try:
                                mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                                if start_cutoff <= mtime < end_cutoff:
                                    os.remove(filepath)
                                    deleted_count += 1
                            except (OSError, IOError) as e:
                                errors.append(f"{filepath}: {str(e)}")
                except Exception as e:
                    errors.append(f"{directory}: {str(e)}")

            results[source] = {
                "success": len(errors) == 0,
                "deleted_count": deleted_count,
                "time_range": time_desc,
                "errors": errors if errors else None,
            }

            logging.info(f"Deleted {deleted_count} JSON files for {source} ({time_desc})")

        return results

    @staticmethod
    def run_imports(imports_config):
        """
        Run multiple imports asynchronously.

        Args:
            imports_config: List of dicts with 'source' and either 'days' or 'start_date'/'end_date'

        Returns:
            Dictionary with import_id for tracking
        """
        # the status-file reaper had no invocation site, so
        # /app/data/import_status/ grew unbounded. Wire it to the moment the
        # directory is added to — whenever an import launches. Best-effort:
        # housekeeping must never block an import, so swallow any failure.
        try:
            ImportService.cleanup_old_status_files()
        except Exception:
            import logging

            logging.warning("cleanup_old_status_files failed; continuing", exc_info=True)

        import_id = str(uuid.uuid4())[:8]
        started = []
        pids = []
        # follow-up — per-source records for connectors we deliberately
        # did NOT spawn because the selected team has no linked account to scope
        # to (google_ads → no customer_id, GA4 → no property_ids). These are NOT
        # failures and NOT silent no-ops: they carry a user-facing message so the
        # Import Data Page explains "nothing imported because no account is
        # linked" instead of (a) hanging on a 0-byte log or (b) reporting a bogus
        # green success. Kept out of ``pids`` (no process) and merged into the
        # status results by get_import_status.
        skipped = []

        # one ES client for the whole batch, used to resolve a
        # selected team to google_ads customer_ids / GA4 property_ids in
        # _build_import_command. Built best-effort: if the client can't be made
        # (e.g. a unit test), resolution falls back to its own lazy build / a
        # skipped scope flag rather than blocking the import.
        es = None
        try:
            from lib.es_helpers import get_elasticsearch_client

            es = get_elasticsearch_client()
        except Exception:  # noqa: BLE001 — resolution degrades gracefully without a client
            logging.warning("could not build ES client for scope resolution; continuing", exc_info=True)

        for item_idx, config in enumerate(imports_config):
            source = config["source"]
            # stable per-batch-item key. Completion records used to
            # key by source alone, so two same-source imports in one batch
            # collapsed to one record (the second youtube result vanished on
            # the capacity panel). The index makes the key unique; the
            # source prefix keeps status files human-readable.
            item_key = f"{source}:{item_idx}"
            days = config.get("days")
            start_date = config.get("start_date")
            end_date = config.get("end_date")
            team_ids = config.get("team_ids")
            dates = config.get("dates")
            # per-import "Force refresh" toggle (default ON in the UI).
            # Re-pull and overwrite already-imported days (FB-ads only — the only
            # importer with a write guard to bypass). Accept a real bool or the
            # string forms the route may pass.
            force_refresh = config.get("force_refresh") in (True, "true", "True", "1", 1)

            # follow-up — google_ads / GA4 need the selected team
            # resolved to a customer_id / property_ids; a team with no linked
            # account resolves to nothing. Detect that BEFORE spawning so the
            # user gets a clear "no account linked" message rather than the
            # importer's require_scope raising into a swallowed no-op (the one-team
            # "do nothing, no message" symptom). Only when a team WAS selected —
            # a connector run with no team at all is a different (cron-style)
            # path we don't gate here.
            if source in ("google_ads", "google_analytics") and team_ids:
                if ImportService._resolve_scope_arg(source, team_ids, es) is None:
                    label = "Google Ads" if source == "google_ads" else "Google Analytics (GA4)"
                    skipped.append(
                        {
                            "source": source,
                            "item": item_key,
                            "success": True,
                            "skipped": True,
                            "message": f"No {label} account is linked to the selected team; nothing to import.",
                        }
                    )
                    continue

            # Build command
            cmd = ImportService._build_import_command(
                source=source,
                days=days,
                start_date=start_date,
                end_date=end_date,
                team_ids=team_ids,
                dates=dates,
                es=es,
                force_refresh=force_refresh,
            )

            if cmd:
                # Log the command for debugging
                import logging

                date_info = f"dates {start_date} to {end_date}" if start_date else f"last {days} days"
                logging.info(f"Starting {source} import for {date_info}")

                # wrap the import in lib.import_runner so the real
                # exit code is recorded in the status file. Without the
                # wrapper, get_import_status only sees PID liveness and
                # would mark every gone process as success — masking
                # crashes, container restarts, and non-zero exits.
                wrapped_cmd = [
                    "python3",
                    "-m",
                    "lib.import_runner",
                    "--import_id",
                    import_id,
                    "--source",
                    source,
                    # per-batch-item record key (see item_key above).
                    "--item",
                    item_key,
                    "--",
                    *cmd,
                ]

                # Start process in background, writing to the manual log
                # so the dated cron log is preserved.
                env = {**os.environ, "IMPORT_LOG_FILE": ImportService.MANUAL_LOG_FILE}
                process = subprocess.Popen(
                    wrapped_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd="/app/python",
                    start_new_session=True,
                    env=env,
                )
                pids.append(
                    {
                        "source": source,
                        # item joins this pid to its completion
                        # record; team_ids lets the capacity panel label two
                        # same-source items by team.
                        "item": item_key,
                        "team_ids": team_ids,
                        "pid": process.pid,
                        "days": days,
                        "start_date": start_date,
                        "end_date": end_date,
                    }
                )
                started.append(source)

        # Write status file. ``skipped`` rides alongside (not in ``pids`` — they
        # have no process, and not in ``results`` — get_import_status rebuilds
        # that from pids); get_import_status merges them into the final results.
        status_data = {
            "import_id": import_id,
            "started_at": datetime.now().isoformat(),
            "sources": started,
            "pids": pids,
            "skipped": skipped,
            "completed": False,
            "results": [],
        }
        ImportService._write_status(import_id, status_data)

        message = f"Started {len(started)} import(s)"
        if skipped:
            message += f"; skipped {len(skipped)} (no linked account)"
        return {
            "import_id": import_id,
            "started": started,
            "skipped": [s["source"] for s in skipped],
            "message": message,
        }

    # the manual Import Data Page sends the SELECTED team's _id(s) as
    # ``team_ids`` (collision-safe ESIDs). Each importer's
    # require_scope guard raises at entry if its own scope flag is missing — but
    # that flag is NOT always ``--team_ids``:
    #   facebook/instagram/youtube/disciple_tools/chatwoot → --team_ids   (native esids)
    #   echo                                               → --limit_teams (native esids)
    #   google_ads                                         → --customer_ids (RESOLVED)
    #   google_analytics (GA4)                             → --property_ids (RESOLVED)
    # The native-esid connectors append their flag inline (see each branch).
    # google_ads / GA4 don't accept team esids — the team must be resolved to its
    # customer_id / property_ids first. That resolution already exists, proven on
    # the Report Scheduler dispatch path (ReportScheduleService); reuse it as the
    # single source of truth rather than re-deriving the ES traversal here.
    @staticmethod
    def _resolve_scope_arg(source, team_ids, es=None):
        """Resolve the selected team ESID(s) to the ``--customer_ids`` /
        ``--property_ids`` flag the google_ads / GA4 importer requires, returning
        a ``--flag=value`` string or ``None``.

        ``team_ids`` is the comma-separated team ESID list from the UI. Each esid
        is resolved via the Report Scheduler's resolvers (the same path
        dispatch uses). Returns ``None`` when there is no team scope, the source
        is not a resolved connector, or resolution finds nothing — the caller
        then omits the flag and the importer's require_scope surfaces a
        classifiable "must be team-scoped" error rather than the UI hanging on a
        0-byte log.
        """
        if not team_ids:
            return None

        flag = {"google_ads": "customer_ids", "google_analytics": "property_ids"}.get(source)
        if not flag:
            return None

        if es is None:
            from lib.es_helpers import get_elasticsearch_client

            es = get_elasticsearch_client()

        from asset_management.web.services.report_schedule_service import ReportScheduleService

        team_esids = [t.strip() for t in team_ids.split(",") if t.strip()]
        values = []
        if flag == "customer_ids":
            # follow-up — multi-account aware (plural resolver); the
            # singular one resolved a multi-account team (e.g. Kavanah Test,
            # whose customer is in team_google_ads_customer_esids with an empty
            # legacy scalar) to nothing → bogus "no account linked" skip.
            for t in team_esids:
                values.extend(ReportScheduleService._resolve_team_to_google_ads_customer_ids(es, t))
        elif flag == "property_ids":
            for t in team_esids:
                values.extend(ReportScheduleService._resolve_team_to_ga_property_ids(es, t))

        if not values:
            return None
        return f"--{flag}={','.join(values)}"

    @staticmethod
    def _build_import_command(
        source, days=None, start_date=None, end_date=None, team_ids=None, dates=None, es=None, force_refresh=False
    ):
        """
        Build the subprocess command for a specific import source.

        Args:
            source: Import source (facebook, echo, google_analytics)
            days: Number of days to pull (optional if dates provided)
            start_date: Start date in YYYY-MM-DD format (optional)
            end_date: End date in YYYY-MM-DD format (optional)
            team_ids: Comma-separated team ESID filter (optional)
            dates: Comma-separated list of specific dates (optional, alternative to range)
            es: Elasticsearch client for team→scope resolution (google_ads / GA4);
                built lazily by _resolve_scope_arg when omitted.
            force_refresh: when True, re-pull and OVERWRITE days already
                imported instead of skipping them. Passed as --force_refetch to the
                importers with a write guard to bypass: FB ads (per-ad skip
                + per-day skip; made get_insights honor it) and
                FB pages (get_page_insights' archived-AND-indexed skip;
                the old claim that "every other connector is upsert-by-ID" was
                false for pages, which made the checkbox a silent no-op there).
                Connectors that genuinely upsert-by-ID on re-run don't take the
                flag — no dead flags on importers that don't read them.
                Default False = gap-fill (keep the idempotency skip).

        Returns:
            List of command arguments or None if invalid source
        """
        base_cmd = ["python3", "/app/python/import.py"]

        # For sources that don't support individual dates, convert to a date range.
        # FB ads and FB pages handle --dates natively; all others need start/end.
        if dates and not start_date and source not in ("facebook_ads", "facebook_pages", "chatwoot"):
            date_list = sorted(d.strip() for d in dates.split(",") if d.strip())
            if date_list:
                start_date = date_list[0]
                end_date = date_list[-1]

        if source == "facebook_ads":
            cmd = base_cmd + ["facebook", "run_daily_imports", "--ads_only"]

            if dates:
                # Specific dates list (e.g. from missing-dates backfill)
                cmd.append(f"--dates={dates}")
            elif start_date and end_date:
                # Facebook Ad Insights natively supports date ranges
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            elif days:
                # Convert days to date range for backward compatibility
                end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            else:
                return None

            if team_ids:
                cmd.append(f"--team_ids={team_ids}")

            # force-overwrite already-imported days (bypass the
            # per-ad + per-day skip via the force path). FB-ads
            # only: the other importers have no write guard to bypass.
            if force_refresh:
                cmd.append("--force_refetch=true")

            return cmd

        elif source == "facebook_pages":
            cmd = base_cmd + ["facebook", "run_page_daily_imports"]

            if dates:
                cmd.append(f"--dates={dates}")
            elif start_date and end_date:
                # Facebook Page Insights natively supports date ranges
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            elif days:
                # Convert days to date range for backward compatibility
                end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            else:
                return None

            if team_ids:
                cmd.append(f"--team_ids={team_ids}")

            # pages has its own write guard (archived-AND-indexed
            # skip in get_page_insights), so the checkbox must reach it too.
            if force_refresh:
                cmd.append("--force_refetch=true")

            return cmd

        elif source == "echo":
            cmd = base_cmd + ["echo", "run_daily_imports"]

            if start_date and end_date:
                # Echo natively supports date ranges
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            elif days:
                # Echo also supports days_to_pull
                cmd.append(f"--days_to_pull={days}")
            else:
                return None

            # scope to the selected team. Previously dropped here, so a
            # single-team UI run was unscoped: get_conversations iterated EVERY
            # tenant (including the dead controlorg-tripwire security-test tenant,
            # which 404-loops). Mirrors the YouTube fix, but Echo's
            # run_daily_imports reads --limit_teams (not --team_ids).
            if team_ids:
                cmd.append(f"--limit_teams={team_ids}")

            return cmd

        elif source == "google_analytics":
            cmd = base_cmd + ["google_analytics", "run_daily_imports"]

            if start_date and end_date:
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            elif days:
                cmd.append(f"--days_to_pull={days}")
            else:
                return None

            # GA4's importer requires --property_ids (NOT team esids);
            # resolve the selected team to its property_ids. Without this the
            # importer's require_scope raised and the UI hung on a
            # 0-byte log.
            scope_arg = ImportService._resolve_scope_arg(source, team_ids, es)
            if scope_arg:
                cmd.append(scope_arg)

            return cmd

        elif source == "instagram":
            cmd = base_cmd + ["instagram", "run_instagram_daily_imports"]

            if start_date and end_date:
                # Instagram wrapper supports date ranges
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            elif days:
                # Convert days to date range
                end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            else:
                return None

            # IG's importer require_scope reads team_ids (connector_id
            # meta_ads); the cmd-builder previously dropped it, so a team-scoped UI
            # run raised and hung. team esids pass straight through, like FB/YT.
            if team_ids:
                cmd.append(f"--team_ids={team_ids}")

            return cmd

        elif source == "youtube":
            cmd = base_cmd + ["youtube", "run_daily_imports"]

            if start_date and end_date:
                # YouTube supports date ranges
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            elif days:
                # Convert days to date range
                end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            else:
                return None

            # scope to the selected team. Previously dropped here, so
            # a single-team UI run imported every team a channel back-links to.
            if team_ids:
                cmd.append(f"--team_ids={team_ids}")

            return cmd

        elif source == "google_ads":
            cmd = base_cmd + ["google_ads", "run_daily_imports"]

            if start_date and end_date:
                # Google Ads natively supports date ranges
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            elif days:
                # Convert days to date range for Google Ads
                end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            else:
                return None

            # Google Ads' importer requires --customer_ids (NOT team
            # esids); resolve the selected team to its customer_id. Without this
            # the importer's require_scope raised and the UI hung on a
            # 0-byte log.
            scope_arg = ImportService._resolve_scope_arg(source, team_ids, es)
            if scope_arg:
                cmd.append(scope_arg)

            return cmd

        elif source == "chatwoot":
            cmd = base_cmd + ["chatwoot", "run_daily_imports"]

            if dates:
                cmd.append(f"--dates={dates}")
            elif start_date and end_date:
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            elif days:
                end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                cmd.extend([f"--start_date={start_date}", f"--end_date={end_date}"])
            else:
                return None

            # chatwoot's importer require_scope reads
            # team_ids and resolves teams→accounts itself; the cmd-builder
            # previously dropped it, so a team-scoped UI run raised and hung.
            # team esids pass straight through, like echo/DT.
            if team_ids:
                cmd.append(f"--team_ids={team_ids}")

            return cmd

        elif source == "bitly":
            # Phase 2 — bitly's importer takes a trailing --days window
            # (Bitly API caps it at 30; the importer clamps + defaults to 3).
            cmd = base_cmd + ["bitly", "run_daily_imports"]

            if days:
                cmd.append(f"--days={days}")
            elif start_date:
                # No native date-range; approximate with a trailing window that
                # reaches back to the requested start (anchored at now, like DT).
                delta = (datetime.now() - datetime.strptime(start_date, "%Y-%m-%d")).days + 1
                cmd.append(f"--days={max(delta, 1)}")
            # else: importer default window (3 days)

            # team_ids deliberately NOT appended — bitly's team scope comes from
            # the active bitly de_connection rows (per-team tag filters,
            # Phase 3), not a CLI flag. One run covers every active connection.
            return cmd

        elif source == "tiktok":
            # team-scoped; no window: the Display API only has
            # lifetime counts, so every run is today's snapshot whatever range
            # the page sends.
            if not team_ids:
                return None
            return base_cmd + ["tiktok", "run_daily_imports", f"--team_ids={team_ids}"]

        elif source == "disciple_tools":
            # DT pulls everything modified since the window; full sync
            # when no window is given. Upsert-by-ID makes either safe to re-run.
            cmd = base_cmd + ["disciple_tools", "run_daily_imports"]

            if days:
                cmd.append(f"--days_to_pull={days}")
            elif start_date and end_date:
                # No native date-range; approximate with a modified-since window.
                delta = (datetime.now() - datetime.strptime(start_date, "%Y-%m-%d")).days
                if delta > 0:
                    cmd.append(f"--days_to_pull={delta}")
            # else: full sync (no window arg)

            if team_ids:
                cmd.append(f"--team_ids={team_ids}")

            return cmd

        return None

    @staticmethod
    def _is_process_running(pid):
        """
        Check if a process with given PID is still running.

        Uses os.waitpid with WNOHANG to properly detect zombie processes
        that have finished but haven't been reaped yet.
        """
        try:
            # Try to reap the process if it's finished (zombie)
            pid_result, status = os.waitpid(pid, os.WNOHANG)

            if pid_result == 0:
                # Process is still running (WNOHANG returned 0)
                return True
            else:
                # Process has finished (waitpid returned the PID)
                return False

        except ChildProcessError:
            # Not our child — under gunicorn this is every poll that lands on
            # a worker other than the one that spawned the import. :
            # the old bare ``os.kill(pid, 0)`` fallback counts ZOMBIES as
            # alive, so a finished import whose spawning worker hadn't reaped
            # it yet read as "running" forever from every other worker
            # (observed live: all four imports done, 8 consecutive polls
            # stuck at completed=false). Read the process STATE instead —
            # a zombie is finished for every purpose the poller cares about.
            try:
                with open(f"/proc/{pid}/stat") as fh:
                    stat_line = fh.read()
            except OSError:
                return False  # no such process
            _, _, after_comm = stat_line.rpartition(")")
            fields = after_comm.split()
            state = fields[0] if fields else "?"
            return state != "Z"

    @staticmethod
    def get_import_status(import_id):
        """
        Get the status of a running or completed import.

        Args:
            import_id: The import session ID

        Returns:
            Dictionary with status information
        """
        status_data = ImportService._read_status(import_id)

        if not status_data:
            return {"found": False, "message": "Import session not found"}

        # Check if already marked completed
        if status_data.get("completed"):
            started_at = datetime.fromisoformat(status_data["started_at"])
            completed_at = datetime.fromisoformat(status_data["completed_at"])
            duration = completed_at - started_at
            duration_str = f"{int(duration.total_seconds())} seconds"

            all_success = all(r.get("success", False) for r in status_data.get("results", []))

            return {
                "found": True,
                "completed": True,
                "success": all_success,
                "duration": duration_str,
                "results": status_data.get("results", []),
                "log_file": os.path.basename(ImportService.MANUAL_LOG_FILE),
            }

        # Check if processes are still running. the wrapper
        # (lib.import_runner) writes per-source completion records into
        # status_data["results"] when each subprocess exits. Treat those
        # as authoritative for whether the import truly succeeded.
        pids = status_data.get("pids", [])
        # join pid → completion record by the per-batch-item key
        # when the pid carries one, so two same-source imports in one batch
        # keep separate records. Status files written before the item key
        # existed have neither side of the join; they fall back to the legacy
        # by-source lookup.
        existing_results_by_item = {r["item"]: r for r in status_data.get("results", []) if r.get("item")}
        existing_results_by_source = {r["source"]: r for r in status_data.get("results", []) if "source" in r}
        running = []
        # follow-up — seed with the deliberately-skipped connectors so
        # the final results explain them ("no linked account") alongside the
        # real per-process outcomes. A skip has no pid, so it is never "running".
        results = list(status_data.get("skipped", []))

        for pid_info in pids:
            pid = pid_info["pid"]
            source = pid_info["source"]
            item = pid_info.get("item")

            if ImportService._is_process_running(pid):
                running.append(source)
                continue

            # Process is gone. Did the wrapper record a completion? Item-keyed
            # pids must join by item — falling back to source here would hand
            # a same-source sibling's record to a killed item.
            if item:
                recorded = existing_results_by_item.get(item)
            else:
                recorded = existing_results_by_source.get(source)
            if recorded is not None:
                results.append(recorded)
            else:
                # Wrapper never wrote a result. The subprocess was
                # killed before completing — container restart, OOM,
                # SIGKILL, or any path that bypassed the try/finally
                # in import_runner. Surface as failure so the user
                # doesn't get a misleading green light.
                killed_record = {
                    "source": source,
                    "success": False,
                    "pid": pid,
                    "error": "process exited without recording completion (likely killed mid-flight)",
                }
                if item:
                    killed_record["item"] = item
                results.append(killed_record)

        if not running:
            # All processes completed - update status file
            status_data["completed"] = True
            status_data["completed_at"] = datetime.now().isoformat()
            status_data["results"] = results
            ImportService._write_status(import_id, status_data)

            started_at = datetime.fromisoformat(status_data["started_at"])
            completed_at = datetime.fromisoformat(status_data["completed_at"])
            duration = completed_at - started_at
            duration_str = f"{int(duration.total_seconds())} seconds"

            all_success = all(r.get("success", False) for r in results)
            return {
                "found": True,
                "completed": True,
                "success": all_success,
                "duration": duration_str,
                "results": results,
                "log_file": os.path.basename(ImportService.MANUAL_LOG_FILE),
            }

        return {
            "found": True,
            "completed": False,
            "running": running,
            "message": (f'Running: {", ".join(running)}' if running else "Finishing up..."),
        }

    @staticmethod
    def cancel_import(import_id):
        """
        Cancel a running import by sending SIGTERM to its process GROUP.

        each import is launched with ``start_new_session=True``
        (run_imports), so the recorded PID is the leader of a process group
        that contains the whole tree: import_runner → run_daily_imports →
        the get_conversations / get_campaign_data children. Signalling only
        the leader (the old ``os.kill(pid, ...)``) killed import_runner and
        ORPHANED the descendants, which kept running and holding the import
        lock — the "Cancel does nothing" bug. ``os.killpg`` reaches every
        descendant. We fall back to a single-PID kill if the group can't be
        resolved (e.g. the leader already exited).

        Args:
            import_id: The import session ID

        Returns:
            Dictionary with success status and list of cancelled sources
        """
        import signal

        status_data = ImportService._read_status(import_id)
        if not status_data:
            return {"success": False, "error": "Import not found"}
        if status_data.get("completed"):
            return {"success": False, "error": "Import already completed"}

        killed = []
        for pid_info in status_data.get("pids", []):
            pid = pid_info["pid"]
            try:
                # signal the whole process group (negative pgid) so
                # the import_runner wrapper AND its run_daily_imports /
                # get_conversations descendants all receive SIGTERM.
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (AttributeError, OSError):
                    # No process-group support (non-POSIX) or the leader is
                    # already gone — fall back to the single-PID signal.
                    os.kill(pid, signal.SIGTERM)
                killed.append(pid_info)
            except OSError:
                pass  # Already gone

        status_data["completed"] = True
        status_data["cancelled"] = True
        status_data["completed_at"] = datetime.now().isoformat()
        # carry the item key onto the cancelled record so the
        # wrapper's SIGTERM-driven completion write matches (and preserves)
        # it instead of clobbering a same-source sibling's record.
        cancelled_results = []
        for pid_info in killed:
            record = {"source": pid_info["source"], "success": False, "cancelled": True}
            if pid_info.get("item"):
                record["item"] = pid_info["item"]
            cancelled_results.append(record)
        status_data["results"] = cancelled_results
        ImportService._write_status(import_id, status_data)

        killed_sources = [p["source"] for p in killed]
        logging.info(f"Cancelled import {import_id}, killed sources: {killed_sources}")
        return {"success": True, "cancelled": killed_sources}

    @staticmethod
    def cleanup_old_status_files(max_age_hours=24):
        """
        Clean up old status files.

        Args:
            max_age_hours: Remove files older than this many hours
        """
        if not os.path.exists(ImportService.STATUS_DIR):
            return

        cutoff = datetime.now() - timedelta(hours=max_age_hours)

        for filename in os.listdir(ImportService.STATUS_DIR):
            if not filename.endswith(".json"):
                continue

            filepath = os.path.join(ImportService.STATUS_DIR, filename)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                if mtime < cutoff:
                    os.remove(filepath)
            except (OSError, IOError):
                pass
