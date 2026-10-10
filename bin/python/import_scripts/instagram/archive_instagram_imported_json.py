import os
import shutil
import logging
import lib.logging


def move_files(src_dir, dest_dir):
    lib.logging.define("ig-archive-imported")
    # Check if the source directory exists
    if not os.path.exists(src_dir):
        logging.error(f"The source directory {src_dir} does not exist.")
        return

    # Check if the destination directory exists, if not create it
    if not os.path.exists(dest_dir):
        logging.info(f"The destination directory {dest_dir} does not exist, creating it.")
        os.makedirs(dest_dir)

    # Walk through the source directory
    for subdir, dirs, files in os.walk(src_dir):
        for filename in files:
            # Construct the file paths
            current_file_path = os.path.join(subdir, filename)
            relative_path = os.path.relpath(subdir, src_dir)  # relative path of subdir from src_dir
            destination_path = os.path.join(dest_dir, relative_path)

            # Check if the destination subdirectory exists, if not create it
            if not os.path.exists(destination_path):
                os.makedirs(destination_path)

            # Construct the destination file path
            destination_file_path = os.path.join(destination_path, filename)

            # Move the file
            if not os.path.exists(destination_file_path):  # check if the file doesn't already exist
                shutil.move(current_file_path, destination_file_path)
                logging.info(f"Moved {current_file_path} to {destination_file_path}")
            else:
                logging.warning(f"File {destination_file_path} already exists. Skipping.")

    # Clean up the source directory
    shutil.rmtree(src_dir)
    os.makedirs(src_dir)
    logging.info(f"Cleaned up source directory {src_dir} and recreated it.")


def main(params):
    lib.logging.define("ig-archive-imported")
    src_dir = "/app/data/instagram/ig_insights"
    dest_dir = "/app/data/instagram/ig_insights_imported"

    logging.info("Starting to move files from ig_insights to ig_insights_imported")
    move_files(src_dir, dest_dir)
    logging.info("Completed moving Instagram files.")
