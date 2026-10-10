"""
Backup Service - handles backup and restore of de_asset* Elasticsearch indices.
"""

import os
import re
import sys
import json
import io
import tarfile
import fnmatch
import logging
from datetime import datetime

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)

from lib.es_helpers import get_elasticsearch_client
from lib.paths import safe_path_join
from elasticsearch import helpers
from lib.errors import InvalidRequestError, RequestedItemNotFoundError

BACKUP_DIR = "/app/data/backups"

# (SVC-105): restore derives ES index names from ARCHIVE MEMBER
# NAMES — attacker/corruption-influenced input. Only de_asset* indices are
# legitimate restore targets (create_backup only ever exports those), and a
# name that fails this pattern must abort the restore BEFORE any ES call:
# a member named "*.json" would otherwise become
# es.indices.delete(index="*"). fullmatch + this character class also
# excludes wildcards, commas (multi-index syntax), dots (system indices)
# and uppercase (invalid in ES index names anyway).
_RESTORABLE_INDEX_RE = re.compile(r"^de_asset[a-z0-9_-]*$")

# Keep the N most recent on-demand asset backups; older are auto-pruned.
MAX_BACKUPS_KEPT = 5


class BackupService:

    @staticmethod
    def create_backup():
        """
        Export all de_asset* indices (mappings + documents) to a dated tar.gz file.
        Older backups beyond the most recent ``MAX_BACKUPS_KEPT`` are pruned.

        Returns:
            ``{"filename": str, "pruned": [str, ...]}``.
        """
        es = get_elasticsearch_client()
        if not es:
            raise RuntimeError("Could not connect to Elasticsearch")

        # Timestamp includes HHMMSS so multiple same-day backups don't clobber.
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"asset-backup-{timestamp}.tar.gz"

        os.makedirs(BACKUP_DIR, exist_ok=True)
        filepath = os.path.join(BACKUP_DIR, filename)

        # Get all de_asset* index names
        cat_response = es.cat.indices(index="de_asset*", h="index", s="index")
        index_names = [line.strip() for line in cat_response.body.strip().splitlines() if line.strip()]

        if not index_names:
            raise RuntimeError("No de_asset* indices found in Elasticsearch")

        logging.info(f"Backing up indices: {index_names}")

        # Build the tar.gz in memory, then write to disk
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for index_name in index_names:
                # Get mapping
                mapping_response = es.indices.get_mapping(index=index_name)
                mappings = mapping_response.body.get(index_name, {}).get("mappings", {})

                # Scroll all documents
                documents = []
                response = es.search(
                    index=index_name,
                    body={"query": {"match_all": {}}},
                    scroll="2m",
                    size=1000,
                )
                scroll_id = response["_scroll_id"]
                hits = response["hits"]["hits"]

                while hits:
                    for hit in hits:
                        documents.append({"_id": hit["_id"], "_source": hit["_source"]})
                    response = es.scroll(scroll_id=scroll_id, scroll="2m")
                    scroll_id = response["_scroll_id"]
                    hits = response["hits"]["hits"]

                try:
                    es.clear_scroll(scroll_id=scroll_id)
                except Exception:
                    pass

                index_data = {"mappings": mappings, "documents": documents}
                json_bytes = json.dumps(index_data, default=str).encode("utf-8")

                info = tarfile.TarInfo(name=f"{index_name}.json")
                info.size = len(json_bytes)
                tar.addfile(info, io.BytesIO(json_bytes))

                logging.info(f"  {index_name}: {len(documents)} documents")

        # Write to file (overwrite if exists)
        with open(filepath, "wb") as f:
            f.write(buf.getvalue())

        logging.info(f"Backup written to {filepath}")

        pruned = BackupService._prune_old_backups()
        return {"filename": filename, "pruned": pruned}

    @staticmethod
    def _prune_old_backups() -> list[str]:
        """Delete all but the ``MAX_BACKUPS_KEPT`` most recent asset backups.

        Returns the list of filenames that were deleted.
        """
        try:
            files = BackupService.list_backups()
        except Exception:
            return []
        to_delete = files[MAX_BACKUPS_KEPT:]
        deleted: list[str] = []
        for fname in to_delete:
            path = os.path.join(BACKUP_DIR, fname)
            try:
                os.remove(path)
                deleted.append(fname)
                logging.info(f"BackupService: pruned old backup {fname}")
            except OSError as exc:
                logging.warning(f"BackupService: failed to prune {fname}: {exc}")
        return deleted

    @staticmethod
    def list_backups():
        """
        Return a list of available backup filenames, newest first.
        """
        if not os.path.isdir(BACKUP_DIR):
            return []

        files = [f for f in os.listdir(BACKUP_DIR) if fnmatch.fnmatch(f, "asset-backup-*.tar.gz")]
        files.sort(reverse=True)
        return files

    @staticmethod
    def restore_backup(filename):
        """
        Restore de_asset* indices from a backup tar.gz file.
        Deletes and recreates each index, then bulk-imports all documents.
        Returns dict with list of restored index names.
        """
        # Validate filename — basename only, must match expected pattern.
        # phase 4: route through safe_path_join after the fnmatch
        # whitelist so CodeQL recognizes the containment under BACKUP_DIR.
        safe_name = os.path.basename(filename)
        if not fnmatch.fnmatch(safe_name, "asset-backup-*.tar.gz"):
            raise InvalidRequestError(f"Invalid backup filename: {filename!r}")

        filepath = safe_path_join(BACKUP_DIR, safe_name)
        if not os.path.isfile(filepath):
            raise RequestedItemNotFoundError(f"Backup file not found: {safe_name}")

        es = get_elasticsearch_client()
        if not es:
            raise RuntimeError("Could not connect to Elasticsearch")

        restored = []

        with tarfile.open(filepath, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.name.endswith(".json"):
                    continue

                index_name = member.name[:-5]  # strip .json

                # abort loudly on any member that does not map to a
                # restorable de_asset* index — never skip-and-continue, since
                # a partially-honored malicious archive is worse than a failed
                # restore.
                if not _RESTORABLE_INDEX_RE.fullmatch(index_name):
                    raise InvalidRequestError(
                        f"Backup archive member {member.name!r} maps to index name "
                        f"{index_name!r}, which is not a restorable de_asset* index — "
                        "aborting restore before any Elasticsearch call."
                    )

                logging.info(f"Restoring index: {index_name}")

                f = tar.extractfile(member)
                if f is None:
                    continue
                index_data = json.loads(f.read().decode("utf-8"))

                mappings = index_data.get("mappings", {})
                documents = index_data.get("documents", [])

                # Delete existing index
                es.indices.delete(index=index_name, ignore_unavailable=True)

                # Recreate with saved mappings
                create_body = {}
                if mappings:
                    create_body["mappings"] = mappings
                es.indices.create(index=index_name, body=create_body)

                # Bulk import documents
                if documents:
                    actions = [
                        {
                            "_index": index_name,
                            "_id": doc["_id"],
                            "_source": doc["_source"],
                        }
                        for doc in documents
                    ]
                    helpers.bulk(es, actions, raise_on_error=False)

                es.indices.refresh(index=index_name)
                restored.append(index_name)
                logging.info(f"  {index_name}: {len(documents)} documents restored")

        return {"restored": restored}
