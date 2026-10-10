"""
S3 Backup Service — lists and restores Elasticsearch backups stored in AWS S3.

Uses boto3 to access S3 (credentials come from the standard AWS credential
chain: env vars, ~/.aws/credentials, or instance profile).

Full restores download the S3 tarball, stop Elasticsearch, swap the data
directory, and restart.  The Docker Engine API is accessed via the mounted
``/var/run/docker.sock`` socket.

This module never writes to S3.  The backups are produced by km-infra's
``backup-elk-*.sh`` on each ELK host. Since the ELK prefixes are
SSE-KMS under a per-environment customer key (``alias/km-backups-<prefix>``):
reading an object needs ``kms:Decrypt`` on that key in addition to
``s3:GetObject``, and the bucket policy denies any put that does not name it
(``the release-plan tests`` pins
that rule for any future write from here).  A ``ClientError`` with
``KMS.AccessDeniedException`` on download therefore means the key policy, not
the bucket policy, is what to check.
"""

import fnmatch
import http.client
import json
import logging
import os
import shutil
import socket
import tarfile
import time

from lib.paths import safe_path_join
from lib.errors import InvalidRequestError, RequestedItemNotFoundError

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    boto3 = None  # type: ignore[assignment]
    BotoCoreError = Exception  # type: ignore[assignment,misc]
    ClientError = Exception  # type: ignore[assignment,misc]


def _backup_bucket() -> str:
    """S3 bucket name. Read lazily so tests can patch config without
    re-importing this module."""
    try:
        from web.config import config
    except ImportError:
        from ..config import config
    return config.S3_BACKUP_BUCKET


def _backup_prefix() -> str:
    """S3 key prefix for this environment's backups. No trailing slash."""
    try:
        from web.config import config
    except ImportError:
        from ..config import config
    return config.S3_BACKUP_PREFIX


# Paths inside the imports_container (volume-mounted from host)
ES_DATA_DIR = "/es-data"  # host: ~/DE-Reporting/elk-01/es01/data
ES_DATA_BACKUP_DIR = "/es-data.bak"
S3_DOWNLOAD_DIR = "/app/data/s3-restore-temp"
S3_CONFIG_DIR = "/app/data/s3-config-restore"
DOCKER_SOCKET = "/var/run/docker.sock"
ES_CONTAINER_NAME = "es01"


def _require_boto3() -> None:
    """Raise a clear error if boto3 is not installed."""
    if boto3 is None:
        raise ImportError("boto3 is required for S3 backup operations. " "Install it with: pip install boto3")


class S3BackupService:

    @staticmethod
    def list_data_backups() -> tuple[list[dict], str | None]:
        """List ES data backups from S3.

        Returns (backups, error) where error is None on success or a string
        describing the failure.  On failure, backups is [].
        """
        return S3BackupService._list_prefix("data/")

    @staticmethod
    def list_config_backups() -> tuple[list[dict], str | None]:
        """List ELK config backups from S3."""
        return S3BackupService._list_prefix("config/")

    @staticmethod
    def _list_prefix(prefix: str) -> tuple[list[dict], str | None]:
        """List objects in the S3 bucket under the given prefix.

        Returns ``(list_of_dicts, error_string | None)``.
        """
        full_prefix = f"{_backup_prefix()}/{prefix}"
        _require_boto3()

        try:
            s3 = boto3.client("s3")
            paginator = s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=_backup_bucket(), Prefix=full_prefix)

            backups: list[dict] = []
            for page in pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    # Extract just the filename (strip the prefix)
                    filename = key.removeprefix(full_prefix)
                    if not filename:
                        continue

                    backups.append(
                        {
                            "filename": filename,
                            "date": obj["LastModified"].strftime("%Y-%m-%d %H:%M:%S"),
                            "size_bytes": obj["Size"],
                            "size_human": S3BackupService._humanize_bytes(obj["Size"]),
                        }
                    )

            # Sort newest first
            backups.sort(key=lambda b: b["date"], reverse=True)
            return backups, None

        except (BotoCoreError, ClientError):
            # don't echo boto exception text to the response body.
            # The full detail (with traceback) lands in the server log.
            logging.exception(f"S3 error listing {full_prefix}")
            return [], "S3 backup listing failed — see server logs for detail"
        except Exception:
            logging.exception(f"Unexpected error listing S3 backups under {full_prefix}")
            return [], "S3 backup listing failed — see server logs for detail"

    @staticmethod
    def restore_full_backup(filename: str, progress_callback: object | None = None) -> dict:
        """Download an S3 data tarball and replace the ES data directory.

        Steps:
        1. Validate filename
        2. Download tarball from S3
        3. Stop Elasticsearch
        4. Back up current data directory
        5. Extract tarball
        6. Start Elasticsearch
        7. Wait for ES to become healthy
        8. Clean up temp files

        Returns a dict with status details.  Raises on fatal errors.
        """
        # Validate filename. phase 4: use safe_path_join after the
        # fnmatch whitelist so CodeQL recognizes containment under
        # S3_DOWNLOAD_DIR, in addition to the format check.
        safe_name = os.path.basename(filename)
        if not fnmatch.fnmatch(safe_name, "es-data-*.tar.gz"):
            raise InvalidRequestError(f"Invalid backup filename: {filename!r}")

        s3_key = f"{_backup_prefix()}/data/{safe_name}"
        local_tarball = safe_path_join(S3_DOWNLOAD_DIR, safe_name)

        def _progress(step: str, detail: str = "") -> None:
            logging.info(f"S3 restore [{step}]: {detail}")
            if progress_callback:
                progress_callback(step, detail)

        _require_boto3()

        try:
            # 1. Download from S3
            os.makedirs(S3_DOWNLOAD_DIR, exist_ok=True)
            _progress("download", f"Downloading {safe_name} from S3...")

            s3 = boto3.client("s3")
            s3.download_file(_backup_bucket(), s3_key, local_tarball)

            tarball_size = os.path.getsize(local_tarball)
            _progress("download", f"Downloaded {S3BackupService._humanize_bytes(tarball_size)}")

            # 2. Stop Elasticsearch
            _progress("stop_es", "Stopping Elasticsearch...")
            S3BackupService._docker_api("POST", f"/containers/{ES_CONTAINER_NAME}/stop?t=30")
            _progress("stop_es", "Elasticsearch stopped")

            # 3. Back up current data directory
            _progress("backup", "Backing up current data directory...")
            if os.path.exists(ES_DATA_BACKUP_DIR):
                shutil.rmtree(ES_DATA_BACKUP_DIR)
            # Move instead of copy — much faster
            os.rename(ES_DATA_DIR, ES_DATA_BACKUP_DIR)
            os.makedirs(ES_DATA_DIR, exist_ok=True)
            _progress("backup", "Current data backed up")

            # 4. Extract tarball
            _progress("extract", "Extracting backup...")
            with tarfile.open(local_tarball, "r:gz") as tar:
                # The tarball contains data/ at the top level.
                # We need to strip that prefix and extract into ES_DATA_DIR.
                for member in tar.getmembers():
                    # Strip leading "data/" prefix
                    if member.name.startswith("data/"):
                        member.name = member.name[5:]  # remove "data/"
                    elif member.name == "data":
                        continue
                    else:
                        continue

                    if not member.name:
                        continue
                    # extract with the 'data' filter so a tar-slip
                    # member (e.g. "data/../../etc/..", an absolute path, or an
                    # escaping symlink) is rejected rather than written outside
                    # ES_DATA_DIR. The filter validates the rewritten member.name
                    # against the destination; this mirrors the config path's
                    # extractall(filter="data") hardened. A rejected
                    # member raises, and the outer except restores the backup.
                    tar.extract(member, ES_DATA_DIR, filter="data")

            _progress("extract", "Backup extracted")

            # 5. Start Elasticsearch
            _progress("start_es", "Starting Elasticsearch...")
            S3BackupService._docker_api("POST", f"/containers/{ES_CONTAINER_NAME}/start")

            # 6. Wait for ES to become healthy (up to 120s)
            _progress("health_check", "Waiting for Elasticsearch to become healthy...")
            healthy = S3BackupService._wait_for_es_healthy(timeout=120)
            if healthy:
                _progress("health_check", "Elasticsearch is healthy")
            else:
                _progress("health_check", "Elasticsearch started but health check timed out — check manually")

            # 7. Clean up
            _progress("cleanup", "Cleaning up temp files...")
            os.remove(local_tarball)
            _progress("done", "Restore complete")

            return {
                "status": "success",
                "backup_file": safe_name,
                "backup_kept_at": ES_DATA_BACKUP_DIR,
                "es_healthy": healthy,
            }

        except Exception:
            # Try to restart ES if it was stopped
            try:
                _progress("recovery", "Error during restore — attempting to restart ES...")
                # If the original data dir still exists as backup, restore it
                if os.path.exists(ES_DATA_BACKUP_DIR) and not os.listdir(ES_DATA_DIR):
                    os.rmdir(ES_DATA_DIR)
                    os.rename(ES_DATA_BACKUP_DIR, ES_DATA_DIR)
                    _progress("recovery", "Original data restored")
                S3BackupService._docker_api("POST", f"/containers/{ES_CONTAINER_NAME}/start")
                _progress("recovery", "Elasticsearch restarted")
            except Exception as recovery_err:
                logging.error(f"Recovery also failed: {recovery_err}")
            raise

    @staticmethod
    def download_config_backup(filename: str) -> dict:
        """Download an S3 config tarball and extract to a temp folder.

        Returns a dict with the extraction path and a file tree listing.
        """
        safe_name = os.path.basename(filename)
        if not fnmatch.fnmatch(safe_name, "elk-config-*.tar.gz"):
            raise InvalidRequestError(f"Invalid config backup filename: {filename!r}")

        s3_key = f"{_backup_prefix()}/config/{safe_name}"
        local_tarball = safe_path_join(S3_DOWNLOAD_DIR, safe_name)

        # Extract to a subfolder named after the backup (without .tar.gz)
        extract_name = safe_name.replace(".tar.gz", "")
        extract_dir = safe_path_join(S3_CONFIG_DIR, extract_name)

        # Clean previous extraction of the same backup
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)

        os.makedirs(S3_DOWNLOAD_DIR, exist_ok=True)
        os.makedirs(extract_dir, exist_ok=True)

        # Download
        _require_boto3()
        logging.info(f"Downloading config backup {safe_name} from S3...")
        s3 = boto3.client("s3")
        s3.download_file(_backup_bucket(), s3_key, local_tarball)

        # Extract. use the strict "data" filter so a malicious
        # tarball can't write outside extract_dir via "../" entries, absolute
        # paths, or symlinks — even though we trust the S3 source, this
        # closes CodeQL py/tarslip and is good belt-and-suspenders.
        logging.info(f"Extracting config backup to {extract_dir}...")
        with tarfile.open(local_tarball, "r:gz") as tar:
            tar.extractall(extract_dir, filter="data")

        # Clean up tarball
        os.remove(local_tarball)

        # Build file tree
        file_tree = S3BackupService._build_file_tree(extract_dir)

        return {
            "extract_dir": extract_dir,
            "backup_file": safe_name,
            "file_tree": file_tree,
        }

    @staticmethod
    def _build_file_tree(root: str) -> list[dict]:
        """Walk a directory and return a flat list of relative paths with metadata."""
        entries: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for fname in sorted(filenames):
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, root)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                entries.append(
                    {
                        "path": rel,
                        "size_bytes": size,
                        "size_human": S3BackupService._humanize_bytes(size),
                    }
                )
        return entries

    @staticmethod
    def _docker_api(method: str, path: str) -> dict | None:
        """Make a request to the Docker Engine API via Unix socket."""

        class UnixHTTPConnection(http.client.HTTPConnection):
            def __init__(self, socket_path: str) -> None:
                super().__init__("localhost")
                self.socket_path = socket_path

            def connect(self) -> None:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(self.socket_path)

        conn = UnixHTTPConnection(DOCKER_SOCKET)
        conn.request(method, path)
        response = conn.getresponse()
        body = response.read().decode()
        conn.close()

        if response.status >= 400:
            raise RuntimeError(f"Docker API error ({response.status}): {body}")

        if body:
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return None
        return None

    @staticmethod
    def _wait_for_es_healthy(timeout: int = 120) -> bool:
        """Poll Docker container health status until healthy or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = S3BackupService._docker_api("GET", f"/containers/{ES_CONTAINER_NAME}/json")
                if result:
                    health = result.get("State", {}).get("Health", {}).get("Status", "")
                    if health == "healthy":
                        return True
            except Exception:
                pass
            time.sleep(3)
        return False

    @staticmethod
    def _humanize_bytes(size: int) -> str:
        """Convert bytes to a human-readable string."""
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(size) < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024  # type: ignore[assignment]
        return f"{size:.1f} PB"
