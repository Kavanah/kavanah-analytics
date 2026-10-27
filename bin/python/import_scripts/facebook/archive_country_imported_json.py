"""move sent per-country JSON from ``ad_insights_country/`` to
``ad_insights_country_imported/`` (the archived-AND-indexed skip in
``get_insights._write_country_docs`` reads the archive)."""

import logging

import lib.logging

from import_scripts.facebook.archive_imported_json import move_files

SRC_DIR = "/app/data/facebook/ad_insights_country"
DEST_DIR = "/app/data/facebook/ad_insights_country_imported"


def main(params):
    lib.logging.define("fb-archive-country-imported")
    logging.info("Moving files from %s to %s", SRC_DIR, DEST_DIR)
    move_files(SRC_DIR, DEST_DIR)
    logging.info("Completed moving country-breakdown files.")
