import os
import re
import logging
from datetime import date, datetime, timedelta

# the dated import logs (imports_YYYYMMDD.log, one per day, read by
# the Import Logs and Status Reports pages) were never deleted: logrotate cannot
# age out a file that is never written again under its own name, and nothing
# else touched them. Keep this many days; older ones are removed by the first
# import run of each day (prune_old_import_logs below).
IMPORT_LOG_RETENTION_DAYS = 30
_DATED_IMPORT_LOG_RE = re.compile(r"imports_(\d{8})\.log")


def prune_old_import_logs(log_dir, keep_days=IMPORT_LOG_RETENTION_DAYS, today=None):
    """Delete ``imports_YYYYMMDD.log`` files dated more than ``keep_days`` before
    ``today``; return how many were removed.

    The date in the NAME decides, not the mtime (a late re-run appends to an old
    day's file). Anything else in the directory — imports_manual.log, the gunicorn
    logs, usage logs, pytest logs — is left alone. Best effort: a file another
    process already removed, or one we may not delete, is skipped and retried on
    the next day's first run; housekeeping never stops an import.
    """
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    try:
        names = os.listdir(log_dir)
    except OSError:
        return 0
    removed = 0
    for name in names:
        match = _DATED_IMPORT_LOG_RE.fullmatch(name)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if stamp >= cutoff:
            continue
        try:
            os.remove(os.path.join(log_dir, name))
        except OSError:
            continue
        removed += 1
    return removed


def setup_logging():
    # Ensure the log directory exists
    log_dir = "/app/data/logs/"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Manual imports set IMPORT_LOG_FILE to write to a separate log,
    # keeping the dated cron log untouched.
    log_file = os.environ.get("IMPORT_LOG_FILE") or f"{log_dir}/imports_{datetime.now().strftime('%Y%m%d')}.log"

    # the first run of a day (today's dated log not created yet)
    # prunes the dated logs past the retention window.
    pruned = 0
    if not os.environ.get("IMPORT_LOG_FILE") and not os.path.exists(log_file):
        pruned = prune_old_import_logs(log_dir)

    logging.basicConfig(filename=log_file, level=logging.INFO, format="%(message)s")

    # Suppress noisy third-party HTTP/transport loggers
    logging.getLogger("elastic_transport").setLevel(logging.WARNING)
    logging.getLogger("elasticsearch").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    # Phase 3: scrub CR/LF/NUL from every log record. Must run after
    # basicConfig has installed the FileHandler so the filter actually attaches.
    from lib.log_filters import install_filter_on_all_handlers

    install_filter_on_all_handlers()

    # write a run-boundary marker so the Import Summary can compute
    # each source's runtime from the LAST run of the day, not the span from the
    # day's first line to its last. The dated log accumulates every run (cron +
    # backfills + re-runs), so without a boundary "runtime" reads as the whole
    # day. The token is a non-source prefix (skipped by the summary's prefix map)
    # whose timestamp the runtime pass anchors on. One marker per process start.
    logging.info(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] [import-run-begin]  run start")
    if pruned:
        logging.info(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] [import-log-retention]  "
            f"removed {pruned} dated import log(s) older than {IMPORT_LOG_RETENTION_DAYS} days"
        )


def define(prefix, level="INFO"):
    # Convert the string level to the corresponding logging level
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Update the logging level for the root logger
    logging.getLogger().setLevel(log_level)

    # Update the formatter for each handler in the root logger with the new prefix
    formatter = logging.Formatter(f"%(asctime)s [%(levelname)s] [{prefix}]  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for handler in logging.root.handlers:
        handler.setFormatter(formatter)
