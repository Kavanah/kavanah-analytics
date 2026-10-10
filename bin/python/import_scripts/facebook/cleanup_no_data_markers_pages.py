"""Facebook *page* insights — cleanup of stale `no_data` markers.

Separate from cleanup_no_data_markers.py (which targets *_fb_insights — ad
insights). FB pages live under a different index suffix.

Usage:
    python3 import.py facebook cleanup_no_data_markers_pages [--dry-run]
"""

import lib.logging
from lib.cleanup_no_data_markers import delete_zombie_markers, dedupe_markers


INDEX_PATTERN = "*_fb_page_insights"


def main(params):
    lib.logging.define("fb-pages-cleanup-no-data-markers")
    dry_run = bool(params.get("dry_run") or params.get("dry-run"))
    dedupe_markers(INDEX_PATTERN, dry_run=dry_run)
    delete_zombie_markers(INDEX_PATTERN, dry_run=dry_run)
