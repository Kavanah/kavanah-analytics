"""Google Ads — cleanup of stale `no_data` markers.

Usage:
    python3 import.py google_ads cleanup_no_data_markers [--dry-run]
"""

import lib.logging
from lib.cleanup_no_data_markers import delete_zombie_markers, dedupe_markers


INDEX_PATTERN = "*_google_ads"


def main(params):
    lib.logging.define("google-ads-cleanup-no-data-markers")
    dry_run = bool(params.get("dry_run") or params.get("dry-run"))
    dedupe_markers(INDEX_PATTERN, dry_run=dry_run)
    delete_zombie_markers(INDEX_PATTERN, dry_run=dry_run)
