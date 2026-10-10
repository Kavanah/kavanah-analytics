"""
Config Restore Service — apply files from an already-extracted S3 config
backup back to the live config directory.

Workflow expected from the caller:
1. Extract a config backup with ``S3BackupService.download_config_backup``.
   That populates a directory under ``/app/data/s3-config-restore/<name>/``.
2. Call ``ConfigRestoreService.list_extracted_files(extract_name)`` to see
   the files available for restore.
3. Call ``ConfigRestoreService.apply_files(extract_name, files=None)`` to
   copy all files (full restore) or a specific list (cherry-pick).

Each file written to disk is preceded by writing the previous version (if
any) to ``/app/data/s3-config-restore/.applied-backup/<extract_name>/<rel>``
so that an accidental overwrite is reversible without going back to S3.

The container's ``/app/reporting_configs`` is bind-mounted from the host's
config directory in docker-compose.yml, so writes here land on the host.
"""

import logging
import os
import shutil
from datetime import datetime, timezone

from lib.paths import UnsafePathError, safe_path_join
from lib.errors import InvalidRequestError, RequestedItemNotFoundError

# Where extracted S3 config backups live (matches S3BackupService.S3_CONFIG_DIR).
EXTRACTED_ROOT = "/app/data/s3-config-restore"

# Where current files are saved before being overwritten (rollback path).
APPLIED_BACKUP_ROOT = "/app/data/s3-config-restore/.applied-backup"

# Live config directory inside the imports_container (host: $CONFIG_DIR).
LIVE_CONFIG_DIR = "/app/reporting_configs"


class ConfigRestoreService:

    # ------------------------------------------------------------------ list

    @staticmethod
    def list_extractions() -> list[dict]:
        """List previously-extracted config backups available for restore.

        Each entry: ``{"name": str, "extracted_at": str, "file_count": int}``.
        """
        if not os.path.isdir(EXTRACTED_ROOT):
            return []

        entries: list[dict] = []
        for name in sorted(os.listdir(EXTRACTED_ROOT)):
            full = os.path.join(EXTRACTED_ROOT, name)
            if not os.path.isdir(full) or name.startswith("."):
                continue
            count = 0
            for _, _, files in os.walk(full):
                count += len(files)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(full), tz=timezone.utc)
                extracted_at = mtime.strftime("%Y-%m-%d %H:%M:%S UTC")
            except OSError:
                extracted_at = "unknown"
            entries.append({"name": name, "extracted_at": extracted_at, "file_count": count})
        # Newest first
        entries.sort(key=lambda e: e["extracted_at"], reverse=True)
        return entries

    @staticmethod
    def list_extracted_files(extract_name: str) -> list[dict]:
        """List relative file paths inside an extraction.

        Each entry: ``{"path": str, "size_bytes": int, "size_human": str,
        "live_exists": bool, "differs": bool}``. ``differs`` is True when the
        live file exists and has different bytes than the backup version.
        """
        extract_dir = ConfigRestoreService._validate_extraction(extract_name)

        out: list[dict] = []
        for dirpath, _, filenames in os.walk(extract_dir):
            for fname in sorted(filenames):
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, extract_dir)
                # Skip our own .applied-backup tree if it ever ends up nested.
                if rel.startswith(".applied-backup" + os.sep):
                    continue
                size = os.path.getsize(full) if os.path.exists(full) else 0
                live_path = os.path.join(LIVE_CONFIG_DIR, rel)
                live_exists = os.path.isfile(live_path)
                differs = live_exists and not _files_identical(full, live_path)
                out.append(
                    {
                        "path": rel.replace(os.sep, "/"),
                        "size_bytes": size,
                        "size_human": _humanize_bytes(size),
                        "live_exists": live_exists,
                        "differs": differs,
                    }
                )
        out.sort(key=lambda e: e["path"])
        return out

    # ------------------------------------------------------------------ apply

    @staticmethod
    def apply_files(extract_name: str, files: list[str] | None = None) -> dict:
        """Copy files from the extraction onto the live config directory.

        Args:
            extract_name: name of the extracted backup folder (no path
                separators, validated).
            files: list of file paths *relative to the extraction root* to
                apply. If None, every file in the extraction is applied.

        Returns:
            ``{"applied": [...], "skipped_missing": [...], "rollback_dir": str}``.
        """
        extract_dir = ConfigRestoreService._validate_extraction(extract_name)

        if files is None:
            files = [f["path"] for f in ConfigRestoreService.list_extracted_files(extract_name)]

        if not files:
            raise InvalidRequestError("No files specified for restore")

        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        # phase 4: safe_path_join enforces (and lets CodeQL see) that
        # rollback_dir / dest / src / rb_path stay under their respective
        # base directories. _validate_relative_path remains the format gate.
        rollback_dir = safe_path_join(APPLIED_BACKUP_ROOT, f"{extract_name}_{timestamp}")
        os.makedirs(rollback_dir, exist_ok=True)

        applied: list[str] = []
        skipped: list[str] = []

        for rel in files:
            ConfigRestoreService._validate_relative_path(rel)
            try:
                src = safe_path_join(extract_dir, rel)
                dest = safe_path_join(LIVE_CONFIG_DIR, rel)
            except UnsafePathError as e:
                # _validate_relative_path should have caught this; defense in depth.
                raise InvalidRequestError(f"Path escapes target root: {rel!r}") from e
            if not os.path.isfile(src):
                skipped.append(rel)
                continue

            os.makedirs(os.path.dirname(dest) or LIVE_CONFIG_DIR, exist_ok=True)

            # Save previous version (if any) so the user can roll back.
            if os.path.isfile(dest):
                rb_path = safe_path_join(rollback_dir, rel)
                os.makedirs(os.path.dirname(rb_path) or rollback_dir, exist_ok=True)
                shutil.copy2(dest, rb_path)

            shutil.copy2(src, dest)
            applied.append(rel)
            logging.info(f"ConfigRestoreService: applied {rel} from extraction {extract_name}")

        return {
            "applied": applied,
            "skipped_missing": skipped,
            "rollback_dir": rollback_dir,
        }

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _validate_extraction(extract_name: str) -> str:
        """Resolve and validate an extraction folder name.

        Returns the realpath of the extraction directory.
        """
        if not isinstance(extract_name, str) or not extract_name:
            raise InvalidRequestError("extraction name is required")
        if os.sep in extract_name or "/" in extract_name or extract_name.startswith("."):
            raise InvalidRequestError(f"Invalid extraction name: {extract_name!r}")
        candidate = os.path.realpath(os.path.join(EXTRACTED_ROOT, extract_name))
        if not candidate.startswith(os.path.realpath(EXTRACTED_ROOT) + os.sep):
            raise InvalidRequestError(f"Extraction path escapes root: {extract_name!r}")
        if not os.path.isdir(candidate):
            raise RequestedItemNotFoundError(f"Extraction not found: {extract_name}")
        return candidate

    @staticmethod
    def _validate_relative_path(rel: str) -> None:
        """Reject path-traversal and absolute paths in user-supplied file lists."""
        if not isinstance(rel, str) or not rel:
            raise InvalidRequestError("file path is required")
        if rel.startswith("/") or rel.startswith("\\") or ".." in rel.replace("\\", "/").split("/"):
            raise InvalidRequestError(f"Invalid file path: {rel!r}")


def _files_identical(a: str, b: str, chunk: int = 65536) -> bool:
    """Return True if two files have identical bytes."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ba = fa.read(chunk)
                bb = fb.read(chunk)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except OSError:
        return False


def _humanize_bytes(size: int) -> str:
    s: float = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(s) < 1024:
            return f"{s:.1f} {unit}"
        s /= 1024
    return f"{s:.1f} TB"
