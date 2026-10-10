"""
YouTube Analytics JSON Archive Script

Moves processed JSON files from channel_metrics/ to channel_metrics_imported/
to prevent reprocessing in future import runs.

Usage:
    python3 /app/python/import.py youtube archive_imported_json
"""

import os
import sys
import shutil
import logging

sys.path.insert(0, "/app/python")
import lib.logging as custom_logging


def archive_json_files(source_dir, dest_dir):
    """
    Move JSON files from source to destination, preserving directory structure.

    Args:
        source_dir: Source directory containing JSON files
        dest_dir: Destination archive directory

    Returns:
        tuple: (moved_count, error_count)
    """
    moved_count = 0
    error_count = 0

    if not os.path.exists(source_dir):
        logging.warning(f"Source directory not found: {source_dir}")
        return (0, 0)

    for root, dirs, files in os.walk(source_dir):
        for file in files:
            if file.endswith(".json"):
                source_path = os.path.join(root, file)

                # Calculate relative path and create corresponding destination
                rel_path = os.path.relpath(source_path, source_dir)
                dest_path = os.path.join(dest_dir, rel_path)
                dest_file_dir = os.path.dirname(dest_path)

                try:
                    # Create destination directory if it doesn't exist
                    os.makedirs(dest_file_dir, exist_ok=True)

                    # Move file
                    shutil.move(source_path, dest_path)
                    moved_count += 1
                    logging.debug(f"Moved: {source_path} → {dest_path}")

                except Exception as e:
                    error_count += 1
                    logging.error(f"Failed to move {source_path}: {e}")

    return (moved_count, error_count)


def main(params):
    """
    Main entry point from import.py.

    Args:
        params: Dict with command-line parameters
    """
    custom_logging.define("youtube-analytics-archive")

    logging.info("Starting YouTube Analytics JSON archiving")

    # Define paths
    source_dir = "/app/data/youtube_analytics/channel_metrics"
    dest_dir = "/app/data/youtube_analytics/channel_metrics_imported"

    # Archive files
    moved_count, error_count = archive_json_files(source_dir, dest_dir)

    # Summary
    logging.info(f"\n{'='*60}")
    logging.info(f"YouTube Analytics Archive Complete")
    logging.info(f"{'='*60}")
    logging.info(f"Files moved: {moved_count}")
    logging.info(f"Errors: {error_count}")
    logging.info(f"Source: {source_dir}")
    logging.info(f"Destination: {dest_dir}")
    logging.info(f"{'='*60}")


if __name__ == "__main__":
    main({})
