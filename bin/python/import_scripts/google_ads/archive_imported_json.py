"""
Google Ads App Campaign JSON Archiver

Moves processed JSON files from app_campaigns/ to app_campaigns_imported/
to mark them as indexed and prevent reprocessing.

Usage:
    python3 /app/python/import.py google_ads archive_imported_json
"""

import os
import shutil
import logging
import lib.logging


def main(params):
    """
    Main entry point from import.py.

    Moves JSON files from /app/data/google_ads/app_campaigns/
    to /app/data/google_ads/app_campaigns_imported/

    Args:
        params: Dict of command-line parameters (unused)
    """
    lib.logging.define("google-ads-archive")

    source_dir = "/app/data/google_ads/app_campaigns/"
    dest_dir = "/app/data/google_ads/app_campaigns_imported/"

    if not os.path.exists(source_dir):
        logging.info(f"Source directory does not exist: {source_dir}")
        return

    file_count = 0

    # Walk through source directory
    for root, dirs, files in os.walk(source_dir):
        for filename in files:
            if filename.endswith(".json"):
                source_path = os.path.join(root, filename)

                # Build corresponding destination path
                rel_path = os.path.relpath(root, source_dir)
                dest_root = os.path.join(dest_dir, rel_path)
                dest_path = os.path.join(dest_root, filename)

                # Create destination directory if needed
                os.makedirs(dest_root, exist_ok=True)

                # Move file
                try:
                    shutil.move(source_path, dest_path)
                    file_count += 1
                except Exception as e:
                    logging.error(f"Failed to move {source_path} to {dest_path}: {e}")

    logging.info(f"Archived {file_count} JSON files to {dest_dir}")


if __name__ == "__main__":
    main({})
