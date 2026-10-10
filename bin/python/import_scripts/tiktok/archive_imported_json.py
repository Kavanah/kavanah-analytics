"""archive step: move one run's staged files out of the queue.

    python3 /app/python/import.py tiktok archive_imported_json --run_name=<name>
"""

from __future__ import annotations

import logging
import os
import shutil

import lib.logging as custom_logging


def main(params):
    custom_logging.define("tiktok-archive")

    from import_scripts.tiktok.run_files import IMPORTED_BASE, STAGING_BASE, run_dir

    run_name = params.get("run_name")
    source = run_dir(STAGING_BASE, run_name)
    if not os.path.isdir(source):
        logging.info("tiktok: nothing to archive at %s", source)
        return None
    dest = run_dir(IMPORTED_BASE, run_name)
    os.makedirs(IMPORTED_BASE, exist_ok=True)
    shutil.move(source, dest)
    logging.info("tiktok: archived %s -> %s", source, dest)
    return None
