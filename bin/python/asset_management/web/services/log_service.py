"""
Log Service - Handles import log file operations
"""

import json
import os
import re
import glob
from datetime import datetime, timedelta

from lib.paths import UnsafePathError, safe_path_join


class LogService:
    """Service for managing import log files."""

    LOG_DIR = "/app/data/logs/"
    SUMMARY_DIR = "/app/data/logs/summaries/"

    # Only files whose basename starts with this prefix are import logs.
    # Everything else in LOG_DIR — pytest_*.log (system/testing logs),
    # release_plan_*_pytest_*.log (release-plan runs), locust_* (load tests) —
    # is excluded from the import logs listing. Allowlisting on "imports"
    # rather than blacklisting known noise keeps new non-import log types out
    # of the page automatically.
    _IMPORT_LOG_PREFIX = "imports"

    @staticmethod
    def get_recent_logs(limit=20):
        """
        Get the most recently modified log files.

        Args:
            limit: Maximum number of files to return

        Returns:
            List of dictionaries with file info (name, size, modified_time)
        """
        try:
            # Get all files in the log directory
            pattern = os.path.join(LogService.LOG_DIR, "*")
            all_files = glob.glob(pattern)

            # Filter to only files (not directories) and get their stats
            file_stats = []
            for filepath in all_files:
                if os.path.isfile(filepath):
                    basename = os.path.basename(filepath)
                    if not basename.startswith(LogService._IMPORT_LOG_PREFIX):
                        continue
                    stat = os.stat(filepath)
                    file_stats.append(
                        {
                            "path": filepath,
                            "name": basename,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                        }
                    )

            # Sort by modification time (most recent first)
            file_stats.sort(key=lambda x: x["mtime"], reverse=True)

            # Limit results
            file_stats = file_stats[:limit]

            # Format for display
            files = []
            for stat in file_stats:
                mtime = datetime.fromtimestamp(stat["mtime"])
                files.append(
                    {
                        "name": stat["name"],
                        "size": LogService._format_size(stat["size"]),
                        "modified": mtime.strftime("%b %d %H:%M"),
                        "raw_size": stat["size"],
                    }
                )

            return files

        except Exception as e:
            raise Exception(f"Failed to list log files: {str(e)}")

    @staticmethod
    def _format_size(size_str):
        """Format file size to human-readable format."""
        try:
            size = int(size_str)
            for unit in ["B", "KB", "MB", "GB"]:
                if size < 1024.0:
                    return f"{size:.1f} {unit}"
                size /= 1024.0
            return f"{size:.1f} TB"
        except (ValueError, TypeError):
            return size_str

    @staticmethod
    def _resolve_log_path(filename):
        """Resolve ``filename`` to a path inside ``LOG_DIR`` or raise.

         Phase 4: ``werkzeug.utils.safe_join`` is the
        CodeQL-recognized containment sanitizer. ``os.path.basename`` is
        kept as a leading defense so callers that pass a slashed path
        get the historical "use only the leaf" behavior; ``safe_join``
        then enforces that the result really lives under ``LOG_DIR``.
        """
        safe_name = os.path.basename(filename)
        try:
            return safe_path_join(LogService.LOG_DIR, safe_name)
        except UnsafePathError as e:
            raise FileNotFoundError(f"Log file not found: {filename}") from e

    @staticmethod
    def read_log_file(filename):
        """
        Read the contents of a log file.

        Args:
            filename: Name of the log file

        Returns:
            String containing file contents
        """
        filepath = LogService._resolve_log_path(filename)

        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Log file not found: {filename}")

        if not os.path.isfile(filepath):
            raise Exception(f"Not a file: {filename}")

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                return f.read()

        except Exception as e:
            raise Exception(f"Failed to read log file: {str(e)}")

    @staticmethod
    def get_log_filepath(filename):
        """Return the full sanitized path for a log file."""
        filepath = LogService._resolve_log_path(filename)
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Log file not found: {filename}")
        if not os.path.isfile(filepath):
            raise Exception(f"Not a file: {filename}")
        return filepath

    @staticmethod
    def delete_log_file(filename):
        """
        Delete a log file.

        Args:
            filename: Name of the log file to delete

        Returns:
            True if successful
        """
        filepath = LogService._resolve_log_path(filename)

        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Log file not found: {filename}")

        if not os.path.isfile(filepath):
            raise Exception(f"Not a file: {filename}")

        try:
            os.remove(filepath)
            return True

        except Exception as e:
            raise Exception(f"Failed to delete log file: {str(e)}")

    @staticmethod
    def get_line_class(line):
        """
        Determine CSS class for syntax highlighting based on line content.

        Args:
            line: The log line text

        Returns:
            CSS class name string
        """
        line_lower = line.lower()

        # Special case: "does not exist" + "creating" is informational, not an error
        if "does not exist" in line_lower and "creating" in line_lower:
            return "log-info"

        # Error patterns (red) - highest priority
        if any(
            keyword in line_lower
            for keyword in [
                "error",
                "failed",
                "does not exist",
                "access denied",
                "exception",
            ]
        ):
            return "log-error"

        # Success patterns (green)
        if any(keyword in line_lower for keyword in ["success", "imported", "created successfully", "completed"]):
            return "log-success"

        # Info patterns (blue)
        if any(
            keyword in line_lower
            for keyword in [
                "checking",
                "creating",
                "processing",
                "fetching",
                "head",
                "put",
                "index",
            ]
        ):
            return "log-info"

        # Default (no special highlighting)
        return "log-default"

    @staticmethod
    def get_most_recent_log_errors():
        """
        Get all error lines from the most recent dated import log file.

        Returns:
            Dictionary with:
                - filename: Name of the most recent log file
                - date: Formatted modification date
                - errors: List of error line strings
                - has_errors: Boolean indicating if errors exist
        """
        # Get most recent dated log (skip non-dated files like imports_manual.log)
        recent_logs = LogService.get_recent_logs(limit=10)
        recent_logs = [log for log in recent_logs if re.match(r"imports_\d{8}\.log$", log["name"])]
        if recent_logs:
            recent_logs = recent_logs[:1]

        if not recent_logs:
            return {"filename": None, "date": None, "errors": [], "has_errors": False}

        log_info = recent_logs[0]
        filename = log_info["name"]
        date = log_info["modified"]

        # Read log file contents
        try:
            content = LogService.read_log_file(filename)
        except Exception:
            return {
                "filename": filename,
                "date": date,
                "errors": [],
                "has_errors": False,
            }

        # Parse for error lines
        lines = content.split("\n")
        errors = []

        for line in lines:
            if line.strip() and LogService.get_line_class(line) == "log-error":
                errors.append(line)

        return {
            "filename": filename,
            "date": date,
            "errors": errors,
            "has_errors": len(errors) > 0,
        }

    _SUMMARY_SOURCES = [
        {
            "name": "Facebook Ads",
            "prefixes": {"fb-run-daily-imports", "fb-get-insights", "fb-json-to-elastic", "fb-archive-imported"},
            "record_pattern": r"Completed sending (\d+) documents to",
        },
        {
            "name": "Facebook Pages",
            "prefixes": {
                "fb-run-page-daily-imports",
                "fb-get-page-insights",
                "fb-page-json-to-elastic",
                "fb-page-archive-imported",
            },
            # The page/post senders log "document(s)"; accept both that and
            # the plural "documents" so counts survive log-wording drift.
            "record_pattern": r"Completed sending (\d+) document(?:\(s\)|s) to",
        },
        {
            "name": "Google Ads",
            "prefixes": {
                "google-ads-run-daily-imports",
                "google-ads-get-video-campaigns",
                "google-ads-json-to-elastic",
                "google-ads-archive",
            },
            "record_pattern": r"Total documents indexed: (\d+)",
        },
        {
            "name": "Google Analytics",
            "prefixes": {
                "ga-run-daily-imports",
                "ga-get-metrics",
                "ga-export-properties",
                "ga-json-to-elastic",
                "ga-archive-json",
            },
            "record_pattern": r"Total documents indexed: (\d+)",
        },
        {
            "name": "Echo",
            "prefixes": {
                "echo-run-daily-imports",
                "echo-get-conversations",
                "echo-json-to-elastic",
                "echo-archive-imported",
                "echo-get-user-stats",
                "echo-user-stats-to-elastic",
            },
            "record_pattern": r"Total conversations processed: (\d+)",
        },
        {
            "name": "Instagram",
            "prefixes": {"ig-run-daily-imports", "ig-get-insights", "ig-json-to-elastic", "ig-archive-imported"},
            # The sender logs "Instagram document(s)"; accept both that and
            # the plural "documents" so counts survive log-wording drift.
            "record_pattern": r"Completed sending (\d+) Instagram document(?:\(s\)|s)",
        },
        {
            "name": "YouTube",
            "prefixes": {
                "youtube-analytics-daily-imports",
                "youtube-analytics-fetch",
                "youtube-analytics-json-to-elastic",
                "youtube-analytics-archive",
            },
            "record_pattern": r"Successfully indexed: (\d+)",
        },
        {
            "name": "Chatwoot",
            "prefixes": {
                "chatwoot-run-daily-imports",
                "chatwoot-get-conversations",
                "chatwoot-json-to-elastic",
                "chatwoot-get-account-reports",
                "chatwoot-conversations-to-elastic",
            },
            "record_pattern": r"Indexed (\d+) conversation document",
        },
        {
            # Disciple Tools imports run via the report scheduler
            # and write to the dated daily log through import.py's
            # setup_logging, but the summary table never recognised them. Prefixes
            # are the lib.logging.define() tokens the DT importer stages set; the
            # record line is send_json_to_elastic's "DT send: indexed N doc(s) ...".
            "name": "Disciple Tools",
            "prefixes": {
                "dt-run-daily-imports",
                "dt-fetch",
                "dt-json-to-elastic",
                "dt-promote-staging",
                "dt-prewarm-geocache",
            },
            "record_pattern": r"indexed (\d+) doc\(s\)",
        },
        {
            # Phase 2 — Bitly link-clicks imports run via the report
            # scheduler and write the dated daily log through import.py's
            # setup_logging. Prefixes are the lib.logging.define() tokens the
            # bitly stages set; the record line is send_json_to_elastic's
            # "Total documents indexed: N".
            "name": "Bitly",
            "prefixes": {
                "bitly-run-daily-imports",
                "bitly-get-link-clicks",
                "bitly-json-to-elastic",
                "bitly-archive",
            },
            "record_pattern": r"Total documents indexed: (\d+)",
        },
        {
            # TikTok organic snapshots; the record line is the
            # index step's "Successfully indexed: N".
            "name": "TikTok",
            "prefixes": {
                "tiktok-run-all-teams",
                "tiktok-run-daily-imports",
                "tiktok-get-organic-stats",
                "tiktok-json-to-elastic",
                "tiktok-archive",
            },
            "record_pattern": r"Successfully indexed: (\d+)",
        },
    ]

    @staticmethod
    def cleanup_old_logs(keep_days=7):
        """
        Delete import log files and summary snapshots older than keep_days.
        """
        cutoff = datetime.now() - timedelta(days=keep_days)

        # Clean up log files
        try:
            log_files = glob.glob(os.path.join(LogService.LOG_DIR, "imports_*.log"))
        except Exception:
            log_files = []

        for filepath in log_files:
            filename = os.path.basename(filepath)
            m = re.match(r"imports_(\d{4})(\d{2})(\d{2})\.log$", filename)
            if m:
                date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                try:
                    file_date = datetime.strptime(date_str, "%Y-%m-%d")
                    if file_date < cutoff:
                        os.remove(filepath)
                except (ValueError, OSError):
                    continue

        # Clean up summary snapshots
        try:
            snapshot_files = glob.glob(os.path.join(LogService.SUMMARY_DIR, "summary_*.json"))
        except Exception:
            snapshot_files = []

        for filepath in snapshot_files:
            filename = os.path.basename(filepath)
            m = re.match(r"summary_(\d{4}-\d{2}-\d{2})\.json$", filename)
            if m:
                try:
                    file_date = datetime.strptime(m.group(1), "%Y-%m-%d")
                    if file_date < cutoff:
                        os.remove(filepath)
                except (ValueError, OSError):
                    continue

    @staticmethod
    def get_available_log_dates(limit=7):
        """
        Get dates for which import summaries or log files exist, most recent first.

        Includes dates that have a cached summary snapshot even if the log file
        has been deleted.

        Args:
            limit: Maximum number of dates to return

        Returns:
            List of dicts with 'date' (YYYY-MM-DD), 'filename', and 'display' (formatted)
        """
        date_map = {}  # date_str -> filename (or None)

        # Collect dates from log files
        try:
            log_files = glob.glob(os.path.join(LogService.LOG_DIR, "imports_*.log"))
        except Exception:
            log_files = []

        for filepath in log_files:
            filename = os.path.basename(filepath)
            m = re.match(r"imports_(\d{4})(\d{2})(\d{2})\.log$", filename)
            if m:
                date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                date_map[date_str] = filename

        # Collect dates from summary snapshots (covers deleted log files)
        try:
            snapshot_files = glob.glob(os.path.join(LogService.SUMMARY_DIR, "summary_*.json"))
        except Exception:
            snapshot_files = []

        for filepath in snapshot_files:
            filename = os.path.basename(filepath)
            m = re.match(r"summary_(\d{4}-\d{2}-\d{2})\.json$", filename)
            if m:
                date_str = m.group(1)
                if date_str not in date_map:
                    # No log file, but snapshot exists
                    date_map[date_str] = None

        dates = []
        for date_str, log_filename in date_map.items():
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                dates.append(
                    {
                        "date": date_str,
                        "filename": log_filename or f"imports_{date_str.replace('-', '')}.log",
                        "display": dt.strftime("%b %d, %Y"),
                    }
                )
            except ValueError:
                continue

        dates.sort(key=lambda x: x["date"], reverse=True)
        return dates[:limit]

    @staticmethod
    def get_import_summary(content):
        """
        Parse log content and return a per-source summary of records indexed, errors,
        and runtime.

        Returns:
            List of dicts with: name, records, errors, ran, runtime
        """
        prefix_map = {}
        for i, source in enumerate(LogService._SUMMARY_SOURCES):
            for prefix in source["prefixes"]:
                prefix_map[prefix] = i

        n_sources = len(LogService._SUMMARY_SOURCES)
        records = [0] * n_sources
        errors = [0] * n_sources
        ran = [False] * n_sources
        # Per-source span tracking for the CURRENT run (reset at each
        # import-run-begin boundary), plus the longest span seen across all
        # runs of the day. Runtime is the longest run's span — the
        # run where real work happened — not the last run's (which may be a
        # trivial "already imported, skipping" no-op).
        first_ts = [None] * n_sources
        last_ts = [None] * n_sources
        max_secs = [None] * n_sources

        def _finalize_run_spans():
            """Fold the current run's per-source span into max_secs, then clear."""
            for i in range(n_sources):
                if first_ts[i] is not None and last_ts[i] is not None:
                    secs = int((last_ts[i] - first_ts[i]).total_seconds())
                    if max_secs[i] is None or secs > max_secs[i]:
                        max_secs[i] = secs
                first_ts[i] = None
                last_ts[i] = None

        # Regex to extract timestamp from new log format:
        # "2025-04-04 10:15:23 [INFO] [prefix]  message"
        ts_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

        for line in content.split("\n"):
            stripped = line.strip()

            # Extract prefix — support both old "[prefix]  msg" and new "timestamp [LEVEL] [prefix]  msg"
            # Requires lowercase start to skip log-level tokens like [INFO], [ERROR]
            m = re.search(r"\[([a-z][a-z0-9_-]+)\]\s+", stripped)
            if not m:
                continue
            prefix = m.group(1)
            # Skip log level tokens like INFO, WARNING, ERROR, DEBUG
            if prefix in ("INFO", "WARNING", "ERROR", "DEBUG"):
                continue

            # a run-boundary marker closes the current run's
            # per-source span (folding it into the longest-span tracker) and
            # resets for the next run. The dated log accumulates cron +
            # backfills + re-runs; runtime is the LONGEST single run's span (the
            # run that did real work), never the whole-day span. Record
            # counts/errors still accumulate across all runs — day totals by
            # design.
            if prefix == "import-run-begin":
                _finalize_run_spans()
                continue

            if prefix not in prefix_map:
                continue

            idx = prefix_map[prefix]
            ran[idx] = True

            # Track timestamps for runtime calculation
            ts_m = ts_pattern.match(stripped)
            if ts_m:
                try:
                    ts = datetime.strptime(ts_m.group(1), "%Y-%m-%d %H:%M:%S")
                    if first_ts[idx] is None:
                        first_ts[idx] = ts
                    last_ts[idx] = ts
                except ValueError:
                    pass

            rec_m = re.search(LogService._SUMMARY_SOURCES[idx]["record_pattern"], line)
            if rec_m:
                records[idx] += int(rec_m.group(1))
            elif LogService.get_line_class(line) == "log-error":
                # Skip false positives: summary lines that mention "Errors: 0"
                # or "Processed: N, Errors: N" are informational, not actual errors
                if not re.search(r"Errors:\s*0\b", line) and not re.search(r"Processed:\s*\d+.*Errors:\s*\d+", line):
                    errors[idx] += 1

        # Fold the final (unterminated) run's spans in before reporting.
        _finalize_run_spans()

        # Calculate runtimes from the longest run's span per source.
        runtimes = []
        for i in range(n_sources):
            if max_secs[i] is not None:
                mins, secs = divmod(max_secs[i], 60)
                runtimes.append(f"{mins}m {secs:02d}s")
            else:
                runtimes.append(None)

        result = [
            {
                "name": LogService._SUMMARY_SOURCES[i]["name"],
                "records": records[i],
                "errors": errors[i],
                "ran": ran[i],
                "runtime": runtimes[i],
            }
            for i in range(len(LogService._SUMMARY_SOURCES))
        ]
        result.sort(key=lambda x: x["name"].lower())
        return result

    _DATE_STR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    @staticmethod
    def _summary_snapshot_path(date_str):
        """Return the file path for a summary snapshot given a YYYY-MM-DD date string.

        Raises ``UnsafePathError`` if ``date_str`` does not match the
        expected format or escapes ``SUMMARY_DIR``.
        """
        if not LogService._DATE_STR_RE.match(date_str):
            raise UnsafePathError(f"Invalid summary snapshot date: {date_str!r}")
        return safe_path_join(LogService.SUMMARY_DIR, f"summary_{date_str}.json")

    @staticmethod
    def _save_summary_snapshot(date_str, summary):
        """Save a summary snapshot as JSON. Creates the summary directory if needed."""
        os.makedirs(LogService.SUMMARY_DIR, exist_ok=True)
        path = LogService._summary_snapshot_path(date_str)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    @staticmethod
    def _load_summary_snapshot(date_str):
        """Load a cached summary snapshot. Returns None if not found or unsafe."""
        try:
            path = LogService._summary_snapshot_path(date_str)
        except UnsafePathError:
            return None
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def get_import_summary_for_date(date_str, log_filename):
        """
        Return the import summary for a given date.

        If a snapshot already exists for the date, return it (read-only).
        Otherwise parse the log file, save a snapshot, and return the result.

        Args:
            date_str: Date in YYYY-MM-DD format
            log_filename: Name of the log file to parse if no snapshot exists

        Returns:
            List of summary dicts, or empty list on failure
        """
        # never serve or write TODAY's snapshot from cache. Today's log
        # is still being appended to (a later import, a same-day backfill, a
        # re-run), so a snapshot minted on first view pins whatever partial numbers
        # existed then and is never refreshed (the cache is write-once). Historical
        # dates are complete/immutable, so caching them stays correct + fast.
        is_today = date_str == datetime.now().strftime("%Y-%m-%d")

        if not is_today:
            cached = LogService._load_summary_snapshot(date_str)
            if cached is not None:
                return cached

        try:
            content = LogService.read_log_file(log_filename)
        except Exception:
            return []

        summary = LogService.get_import_summary(content)

        # Only snapshot a PAST date, and only if at least one source ran (avoid
        # caching empty results and avoid pinning the still-growing current day).
        if not is_today and any(row["ran"] for row in summary):
            try:
                LogService._save_summary_snapshot(date_str, summary)
            except (OSError, UnsafePathError):
                pass

        return summary
