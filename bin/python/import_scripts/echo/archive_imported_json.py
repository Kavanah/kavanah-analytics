import os
import json
import shutil
import logging
import lib.logging
from collections import Counter
from datetime import datetime, timedelta

# the same family pattern the retention sweep checks.
ECHO_INDEX_PATTERN = "*_echo_conversations"


def process_file(file_path: str, base_source_dir: str, indexed_ids: set[str]) -> str:
    """Archive one conversation file if it is closed/blocked or expired AND its
    document is confirmed in ES. Returns what happened, for the run summary.

     (the gate, which never reached Echo): send_json_to_elastic
    drops some docs (no team, no parseable date) and never fails, and it reads
    only /app/data/echo/conversations. Archiving an unindexed file therefore
    stranded it for good. Such a file now stays put, so the next send retries it.
    """
    lib.logging.define("echo-archive-imported")
    doc_id = os.path.splitext(os.path.basename(file_path))[0]
    try:
        # Read the JSON data
        with open(file_path, "r") as file:
            data = json.load(file)

        status = data.get("status", "").lower()
        now = datetime.utcnow()

        # If the conversation is closed or blocked, move it to conversations_closed
        if status in ["closed", "blocked"]:
            if doc_id not in indexed_ids:
                logging.debug(f"Keeping {file_path}: closed but not yet indexed.")
                return "kept_unindexed"
            relative_path = os.path.relpath(file_path, base_source_dir)
            target_base = os.path.join(os.path.dirname(base_source_dir), "conversations_closed")
            target_file_path = os.path.join(target_base, relative_path)
            os.makedirs(os.path.dirname(target_file_path), exist_ok=True)
            shutil.move(file_path, target_file_path)
            logging.info(f"Moved file {file_path} to {target_file_path} due to status '{status}'.")
            return "moved_closed"

        # Determine if the conversation is expired.
        # An expired conversation (which cannot be closed/blocked) is defined as either:
        # - having a valid expiration date that is at least 1 month (30 days) before today, or
        # - lacking a valid expiration but having a valid created_at date that is at least 3 months (90 days) before today.
        expired = False
        expiration_str = data.get("expiration", "").strip()
        created_at_str = data.get("created_at", "").strip()

        if expiration_str:
            try:
                expiration_date = datetime.strptime(expiration_str, "%B %d, %Y %H:%M:%S UTC")
                if expiration_date <= (now - timedelta(days=30)):
                    expired = True
            except ValueError as e:
                logging.error(f"Error parsing expiration date in {file_path}: {e}")
        elif created_at_str:
            try:
                created_date = datetime.strptime(created_at_str, "%B %d, %Y %H:%M:%S UTC")
                if created_date <= (now - timedelta(days=90)):
                    expired = True
            except ValueError as e:
                logging.error(f"Error parsing created_at date in {file_path}: {e}")

        # If the conversation is expired, move it to conversations_expired.
        if expired:
            if doc_id not in indexed_ids:
                logging.debug(f"Keeping {file_path}: expired but not yet indexed.")
                return "kept_unindexed"
            relative_path = os.path.relpath(file_path, base_source_dir)
            target_base = os.path.join(os.path.dirname(base_source_dir), "conversations_expired")
            target_file_path = os.path.join(target_base, relative_path)
            os.makedirs(os.path.dirname(target_file_path), exist_ok=True)
            shutil.move(file_path, target_file_path)
            logging.info(f"Moved file {file_path} to {target_file_path} due to expiration.")
            return "moved_expired"
        logging.debug(f"File {file_path} is not closed/blocked or expired. No action taken.")
        return "unchanged"

    except Exception as e:
        logging.error(f"Error processing file {file_path}: {e}")
        return "error"


def main(params, base_source_dir: str = "/app/data/echo/conversations") -> Counter:
    lib.logging.define("echo-archive-imported")
    logging.info("Starting the archiving of Echo conversations.")

    paths = [
        os.path.join(root, file) for root, _dirs, files in os.walk(base_source_dir) for file in files if file.endswith(".json")
    ]
    outcomes: Counter = Counter()
    if not paths:
        logging.info("Completed the archiving of Echo conversations: nothing to archive.")
        return outcomes

    # one exact lookup for every file. If it cannot be answered,
    # archive nothing: an unarchived file costs a re-read, a wrongly archived
    # one is lost to the importer.
    from lib.es_helpers import existing_doc_ids, get_elasticsearch_client

    try:
        es = get_elasticsearch_client()
        if es is None:
            raise RuntimeError("no Elasticsearch client")
        ids = sorted({os.path.splitext(os.path.basename(p))[0] for p in paths})
        indexed_ids = existing_doc_ids(es, ECHO_INDEX_PATTERN, ids)
    except Exception as e:  # noqa: BLE001 — job boundary; archiving nothing is the safe outcome
        logging.error(
            f"could not confirm which conversations are indexed ({type(e).__name__}: {e}); archiving nothing."
        )
        outcomes["lookup_failed"] = len(paths)
        return outcomes

    for file_path in paths:
        outcomes[process_file(file_path, base_source_dir, indexed_ids)] += 1

    logging.info(
        "Completed the archiving of Echo conversations: moved %d closed, %d expired; kept %d not yet indexed.",
        outcomes["moved_closed"],
        outcomes["moved_expired"],
        outcomes["kept_unindexed"],
    )
    return outcomes
