import os
import logging
from datetime import datetime


def setup_logging():
    # Ensure the log directory exists
    log_dir = "/app/data/logs/"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Manual imports set IMPORT_LOG_FILE to write to a separate log,
    # keeping the dated cron log untouched.
    log_file = os.environ.get("IMPORT_LOG_FILE") or f"{log_dir}/imports_{datetime.now().strftime('%Y%m%d')}.log"
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


def define(prefix, level="INFO"):
    # Convert the string level to the corresponding logging level
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Update the logging level for the root logger
    logging.getLogger().setLevel(log_level)

    # Update the formatter for each handler in the root logger with the new prefix
    formatter = logging.Formatter(f"%(asctime)s [%(levelname)s] [{prefix}]  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for handler in logging.root.handlers:
        handler.setFormatter(formatter)
