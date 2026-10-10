"""
Google Analytics JSON File Archiver

Moves processed JSON files from ga_metrics/ to ga_metrics_imported/
Preserves directory structure (organized by property_id).
"""

import os
import shutil
import logging
import lib.logging


def main(params):
    """
    Main entry point from import.py.

    Moves all JSON files from source to destination directory.

    Args:
        params: Dict of command-line parameters (unused)
    """
    lib.logging.define("ga-archive-json")

    source_directory = "/app/data/google_analytics/ga_metrics/"
    destination_directory = "/app/data/google_analytics/ga_metrics_imported/"

    # Ensure the destination directory exists
    os.makedirs(destination_directory, exist_ok=True)

    files_moved = 0

    # Walk through the source directory
    for root, dirs, files in os.walk(source_directory):
        for filename in files:
            if filename.endswith(".json"):
                source_file = os.path.join(root, filename)

                # Calculate the relative path to maintain directory structure
                relative_path = os.path.relpath(root, source_directory)
                destination_folder = os.path.join(destination_directory, relative_path)

                # Ensure the destination folder exists
                os.makedirs(destination_folder, exist_ok=True)

                destination_file = os.path.join(destination_folder, filename)

                # Move the file
                try:
                    shutil.move(source_file, destination_file)
                    logging.info(f"Moved: {source_file} -> {destination_file}")
                    files_moved += 1
                except Exception as e:
                    logging.error(f"Error moving {source_file}: {e}")

    if files_moved > 0:
        logging.info(f"Successfully moved {files_moved} JSON files to the imported directory")
    else:
        logging.info("No JSON files found to archive")


if __name__ == "__main__":
    # For standalone testing
    main({})
