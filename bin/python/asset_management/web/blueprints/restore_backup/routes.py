"""
Restore from Backup Blueprint Routes

Provides a management console page for viewing available backups (both local
asset backups and S3 full-cluster backups) and triggering local restores.
"""

import logging
import os
import time
from datetime import datetime, timezone

from flask import abort, jsonify, render_template, request, session

from . import restore_backup_bp
from lib.errors import InvalidRequestError, RequestedItemNotFoundError

try:
    from web.services.backup_service import BackupService, BACKUP_DIR
    from web.services.s3_backup_service import S3BackupService
    from web.services.snapshot_service import SnapshotService, SnapshotServiceError
    from web.services.config_restore_service import ConfigRestoreService
    from web.services.role_permissions_service import RolePermissionsService
except ImportError:
    from ...services.backup_service import BackupService, BACKUP_DIR
    from ...services.s3_backup_service import S3BackupService
    from ...services.snapshot_service import SnapshotService, SnapshotServiceError
    from ...services.config_restore_service import ConfigRestoreService
    from ...services.role_permissions_service import RolePermissionsService


def _require_mc_restore_backup() -> None:
    """Abort with 403 if the user lacks the mc_restore_backup permission."""
    perms = RolePermissionsService.get_merged_permissions(session.get("roles", []))
    if not perms.get("mc_restore_backup"):
        abort(403, description="This page requires the Restore Backup permission.")


# ── step-up MFA for the destructive routes ─────────────────────────
#
# The four routes that WRITE to the cluster or the config directory
# (restore-local, restore-s3, snapshots/restore, config-extractions/apply) can
# overwrite production data from a single admin session. A stolen session
# cookie (XSS while script-src 'unsafe-inline' stands; a stolen
# laptop) reaches them with no further proof of presence. They now require the
# caller to have re-proved their OWN second factor within STEP_UP_LIFETIME
# seconds, through POST /api/step-up below. Read-only routes (list, inspect,
# repos, download-config) are untouched.
#
# Why not /admin/elevate: its load-bearing rule is "you already hold
# a de_authz_data_grant for this org/team", which has no meaning for a cluster
# restore, and its gate check was deliberately dropped. See DECISIONS
# 2026-09-11.
STEP_UP_SESSION_KEY = "restore_step_up_until"
STEP_UP_LIFETIME_SECONDS = 600  # matches ELEVATION_LIFETIME / fpc / mfa_pending timeouts (15 min ceiling)
STEP_UP_LOCKOUT_SURFACE = "restore_step_up"

AUDIT_STEP_UP = "restore.step_up"
AUDIT_RESTORE_LOCAL = "restore.local_backup"
AUDIT_RESTORE_S3 = "restore.s3_full"
AUDIT_RESTORE_SNAPSHOT = "restore.snapshot"
AUDIT_CONFIG_APPLY = "restore.config_apply"


def _step_up_remaining_seconds() -> float:
    try:
        until = float(session.get(STEP_UP_SESSION_KEY) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, until - time.time())


def _require_recent_step_up():
    """``None`` when the session re-proved its second factor within
    ``STEP_UP_LIFETIME_SECONDS``; otherwise the ``(json, 403)`` tuple the caller
    returns verbatim. The body carries ``step_up_required`` so the page can
    open the code prompt and retry, instead of showing a dead error."""
    if _step_up_remaining_seconds() > 0:
        return None
    return (
        jsonify(
            {
                "ok": False,
                "step_up_required": True,
                "error": "Confirm your second factor before running a restore.",
            }
        ),
        403,
    )


def _audit(action: str, success: bool, *, detail: str, failure_reason: str = "") -> None:
    """one ``de_audit_log`` row per destructive call: actor, address,
    what was restored / applied, outcome. Fire-and-forget by contract of
    ``AuditLogService.log_event`` (a dropped write is now logged at ERROR there)."""
    try:
        from web.services.audit_log_service import AuditLogService
    except ImportError:
        from ...services.audit_log_service import AuditLogService
    AuditLogService.log_event(
        session.get("username", "unknown"),
        action,
        success,
        ip_address=request.remote_addr or "",
        user_agent=request.headers.get("User-Agent", ""),
        detail=detail,
        failure_reason=failure_reason,
    )


@restore_backup_bp.route("/api/step-up", methods=["POST"])
def api_step_up():
    """re-prove the caller's OWN second factor for the destructive
    routes. Body: ``{"code": "<totp or yubikey otp>"}``.

    Order: permission → lockout → factor present → verify. Fail closed: a
    caller with no confirmed factor cannot restore at all (403,
    ``mfa_not_configured``) — every human holder of mc_restore_backup on prod
    has one (2026-09-11 census); the built-in ``elastic`` login has none and
    should not be driving restores from the UI. A wrong code counts against the
    shared lockout on its own surface and is audited; a right code
    stamps ``restore_step_up_until`` for STEP_UP_LIFETIME_SECONDS and is
    audited. The code is never echoed.
    """
    _require_mc_restore_backup()
    try:
        from web import auth as auth_module
        from web.config import config
        from web.services.mfa_service import MFAService
    except ImportError:
        from ... import auth as auth_module
        from ...config import config
        from ...services.mfa_service import MFAService

    username = session.get("username", "") or ""
    if not username:
        return jsonify({"ok": False, "error": "Not authenticated"}), 401
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "error": "code is required"}), 400

    if not auth_module.check_rate_limit(username):
        _audit(AUDIT_STEP_UP, False, detail="locked_out", failure_reason="locked_out")
        return jsonify({"ok": False, "error": "Too many failed attempts. Try again later."}), 429

    record = MFAService.get_mfa_record(username) or {}
    if not (record.get("totp_confirmed") or record.get("yubikey_confirmed")):
        _audit(AUDIT_STEP_UP, False, detail="mfa_not_configured", failure_reason="mfa_not_configured")
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Restores require a second factor. Set up MFA on your profile first.",
                    "mfa_not_configured": True,
                }
            ),
            403,
        )

    ok = MFAService.verify_active_factor(
        username,
        code,
        yubico_client_id=config.YUBICO_CLIENT_ID,
        yubico_secret_key=config.YUBICO_SECRET_KEY,
        allow_insecure_dev_mode=config.MFA_ALLOW_INSECURE_DEV,
    )
    method = record.get("mfa_method") or ""
    if not ok:
        auth_module.record_failed_attempt(username, surface=STEP_UP_LOCKOUT_SURFACE)
        _audit(AUDIT_STEP_UP, False, detail=f"method={method}", failure_reason="invalid_mfa_code")
        return jsonify({"ok": False, "error": "Invalid code. Please try again."}), 401

    until = time.time() + STEP_UP_LIFETIME_SECONDS
    session[STEP_UP_SESSION_KEY] = until
    _audit(AUDIT_STEP_UP, True, detail=f"method={method} expires_at={int(until)}")
    return jsonify({"ok": True, "step_up_until": int(until), "lifetime_seconds": STEP_UP_LIFETIME_SECONDS})


# ── Page route ────────────────────────────────────────────────────────────────


@restore_backup_bp.route("/")
def index() -> str:
    _require_mc_restore_backup()
    return render_template("restore_backup.html")


# ── API routes ────────────────────────────────────────────────────────────────


@restore_backup_bp.route("/api/list")
def api_list():
    """Return combined list of local asset backups and S3 full backups."""
    _require_mc_restore_backup()

    # Local backups — enrich with file size and date
    local_backups: list[dict] = []
    try:
        filenames = BackupService.list_backups()
        for fname in filenames:
            filepath = os.path.join(BACKUP_DIR, fname)
            entry: dict = {"filename": fname, "type": "local"}
            try:
                stat = os.stat(filepath)
                entry["size_bytes"] = stat.st_size
                entry["size_human"] = _humanize_bytes(stat.st_size)
                entry["date"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                entry["size_bytes"] = 0
                entry["size_human"] = "unknown"
                entry["date"] = "unknown"
            local_backups.append(entry)
    except Exception as exc:
        logging.exception("Failed to list local backups")
        local_backups = []

    # S3 backups (exclude kib-data files — too small to be useful)
    s3_data_backups_raw, s3_data_error = S3BackupService.list_data_backups()
    s3_data_backups = [b for b in s3_data_backups_raw if b["filename"].startswith("es-data-")]
    s3_config_backups, s3_config_error = S3BackupService.list_config_backups()

    return jsonify(
        {
            "local_backups": local_backups,
            "s3_data_backups": s3_data_backups,
            "s3_config_backups": s3_config_backups,
            "s3_error": s3_data_error or s3_config_error,
        }
    )


@restore_backup_bp.route("/api/restore-local", methods=["POST"])
def api_restore_local():
    """Restore de_asset* indices from a local backup file."""
    _require_mc_restore_backup()
    step_up_err = _require_recent_step_up()
    if step_up_err is not None:
        return step_up_err

    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "filename is required"}), 400

    username = session.get("username", "unknown")

    try:
        result = BackupService.restore_backup(filename)
        logging.info(
            f"AUDIT: user={username} action=restore_local_backup " f"file={filename} restored={result.get('restored', [])}"
        )
        _audit(AUDIT_RESTORE_LOCAL, True, detail=f"file={filename} restored={result.get('restored', [])}")
        return jsonify({"ok": True, **result})
    except (InvalidRequestError, RequestedItemNotFoundError) as exc:
        # BackupService raises these with operator-facing validation
        # messages ("filename does not match expected pattern", "backup file
        # does not exist"). Surface them; this is the documented contract.
        _audit(AUDIT_RESTORE_LOCAL, False, detail=f"file={filename}", failure_reason=str(exc)[:200])
        return jsonify({"ok": False, "error": exc.public_message}), 400
    except Exception as exc:
        from lib.errors import safe_error_response

        _audit(AUDIT_RESTORE_LOCAL, False, detail=f"file={filename}", failure_reason=type(exc).__name__)
        return safe_error_response(
            "Local backup restore failed",
            log_detail=f"api_restore_local: failed for filename '{filename}'",
            exc=exc,
            body={"ok": False},
        )


@restore_backup_bp.route("/api/restore-s3", methods=["POST"])
def api_restore_s3():
    """Full ES data restore from an S3 backup.

    Downloads the tarball, stops ES, swaps the data directory, restarts ES.
    This is a long-running operation (several minutes).
    """
    _require_mc_restore_backup()
    step_up_err = _require_recent_step_up()
    if step_up_err is not None:
        return step_up_err

    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "filename is required"}), 400

    username = session.get("username", "unknown")

    try:
        logging.info(f"AUDIT: user={username} action=restore_s3_backup file={filename} status=started")
        result = S3BackupService.restore_full_backup(filename)
        logging.info(f"AUDIT: user={username} action=restore_s3_backup file={filename} status=complete")
        _audit(AUDIT_RESTORE_S3, True, detail=f"file={filename} es_healthy={result.get('es_healthy')}")
        return jsonify({"ok": True, **result})
    except (InvalidRequestError, RequestedItemNotFoundError) as exc:
        # S3BackupService raises these with operator-facing
        # validation messages; surfacing them is the documented contract.
        _audit(AUDIT_RESTORE_S3, False, detail=f"file={filename}", failure_reason=str(exc)[:200])
        return jsonify({"ok": False, "error": exc.public_message}), 400
    except Exception as exc:
        from lib.errors import safe_error_response

        _audit(AUDIT_RESTORE_S3, False, detail=f"file={filename}", failure_reason=type(exc).__name__)
        return safe_error_response(
            "S3 backup restore failed",
            log_detail=f"api_restore_s3: failed for filename '{filename}'",
            exc=exc,
            body={"ok": False},
        )


@restore_backup_bp.route("/api/download-config", methods=["POST"])
def api_download_config():
    """Download and extract an S3 config backup to a temp folder."""
    _require_mc_restore_backup()

    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "filename is required"}), 400

    username = session.get("username", "unknown")

    try:
        logging.info(f"AUDIT: user={username} action=download_config_backup file={filename}")
        result = S3BackupService.download_config_backup(filename)
        return jsonify({"ok": True, **result})
    except (InvalidRequestError, RequestedItemNotFoundError) as exc:
        # S3BackupService raises these with operator-facing
        # validation messages; surfacing them is the documented contract.
        return jsonify({"ok": False, "error": exc.public_message}), 400
    except Exception as exc:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Config backup download failed",
            log_detail=f"api_download_config: failed for filename '{filename}'",
            exc=exc,
            body={"ok": False},
        )


# ── ES Snapshot routes ────────────────────────────────────────────────────────


@restore_backup_bp.route("/api/snapshots/repos")
def api_snapshot_repos():
    """List all registered ES snapshot repositories."""
    _require_mc_restore_backup()
    try:
        return jsonify({"ok": True, "repos": SnapshotService.list_repos()})
    except SnapshotServiceError as exc:
        # SnapshotServiceError is the domain exception class with an
        # operator-facing message contract; surfacing str(exc) is intentional.
        return jsonify({"ok": False, "error": exc.public_message}), 503
    except Exception as exc:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to list snapshot repos",
            log_detail="api_snapshot_repos failed",
            exc=exc,
            body={"ok": False},
        )


@restore_backup_bp.route("/api/snapshots/list")
def api_snapshot_list():
    """List all snapshots in the given repository (?repo=<name>).

    Returns ``{ok, snapshots: [...], warnings: [...]}``. ``warnings`` lists
    snapshots that could not be loaded (corrupted / missing data files).
    """
    _require_mc_restore_backup()
    repo = (request.args.get("repo") or "").strip()
    if not repo:
        return jsonify({"ok": False, "error": "repo is required"}), 400
    try:
        result = SnapshotService.list_snapshots(repo)
        return jsonify({"ok": True, **result})
    except InvalidRequestError as exc:
        # validation message contract — see api_restore_local.
        return jsonify({"ok": False, "error": exc.public_message}), 400
    except SnapshotServiceError as exc:
        # domain exception with operator-facing message contract.
        return jsonify({"ok": False, "error": exc.public_message}), 503
    except Exception as exc:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to list snapshots",
            log_detail=f"api_snapshot_list: failed for repo '{repo}'",
            exc=exc,
            body={"ok": False},
        )


@restore_backup_bp.route("/api/snapshots/inspect")
def api_snapshot_inspect():
    """Return indices contained in a snapshot, with conflict markers."""
    _require_mc_restore_backup()
    repo = (request.args.get("repo") or "").strip()
    snapshot = (request.args.get("snapshot") or "").strip()
    if not repo or not snapshot:
        return jsonify({"ok": False, "error": "repo and snapshot are required"}), 400
    try:
        return jsonify({"ok": True, **SnapshotService.inspect_snapshot(repo, snapshot)})
    except (InvalidRequestError, RequestedItemNotFoundError) as exc:
        # validation message contract — see api_restore_local.
        return jsonify({"ok": False, "error": exc.public_message}), 400
    except SnapshotServiceError as exc:
        # domain exception with operator-facing message contract.
        return jsonify({"ok": False, "error": exc.public_message}), 503
    except Exception as exc:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to inspect snapshot",
            log_detail=f"api_snapshot_inspect: failed for repo '{repo}' snapshot '{snapshot}'",
            exc=exc,
            body={"ok": False},
        )


@restore_backup_bp.route("/api/snapshots/restore", methods=["POST"])
def api_snapshot_restore():
    """Restore selected indices from a snapshot.

    Body: {"repo": str, "snapshot": str, "indices": [str], "replace_existing": bool}
    """
    _require_mc_restore_backup()
    step_up_err = _require_recent_step_up()
    if step_up_err is not None:
        return step_up_err
    data = request.get_json(silent=True) or {}
    repo = (data.get("repo") or "").strip()
    snapshot = (data.get("snapshot") or "").strip()
    indices = data.get("indices") or []
    replace_existing = bool(data.get("replace_existing", False))

    if not repo or not snapshot:
        return jsonify({"ok": False, "error": "repo and snapshot are required"}), 400
    if not isinstance(indices, list) or not indices:
        return jsonify({"ok": False, "error": "indices must be a non-empty list"}), 400

    username = session.get("username", "unknown")
    try:
        logging.info(
            f"AUDIT: user={username} action=restore_snapshot repo={repo} "
            f"snapshot={snapshot} indices={len(indices)} replace_existing={replace_existing} status=started"
        )
        result = SnapshotService.restore_indices(repo, snapshot, indices, replace_existing=replace_existing)
        logging.info(
            f"AUDIT: user={username} action=restore_snapshot repo={repo} "
            f"snapshot={snapshot} restored={len(result['restored'])} "
            f"deleted={len(result['deleted_before_restore'])} status=complete"
        )
        _audit(
            AUDIT_RESTORE_SNAPSHOT,
            True,
            detail=(
                f"repo={repo} snapshot={snapshot} indices={len(indices)} replace_existing={replace_existing} "
                f"restored={len(result['restored'])} deleted={len(result['deleted_before_restore'])}"
            ),
        )
        return jsonify({"ok": True, **result})
    except (InvalidRequestError, RequestedItemNotFoundError) as exc:
        # validation message contract — see api_restore_local.
        _audit(AUDIT_RESTORE_SNAPSHOT, False, detail=f"repo={repo} snapshot={snapshot}", failure_reason=str(exc)[:200])
        return jsonify({"ok": False, "error": exc.public_message}), 400
    except SnapshotServiceError as exc:
        # domain exception with operator-facing message contract.
        _audit(AUDIT_RESTORE_SNAPSHOT, False, detail=f"repo={repo} snapshot={snapshot}", failure_reason=str(exc)[:200])
        return jsonify({"ok": False, "error": exc.public_message}), 503
    except Exception as exc:
        from lib.errors import safe_error_response

        _audit(AUDIT_RESTORE_SNAPSHOT, False, detail=f"repo={repo} snapshot={snapshot}", failure_reason=type(exc).__name__)
        return safe_error_response(
            "Snapshot restore failed",
            log_detail=f"api_snapshot_restore: failed for repo '{repo}' snapshot '{snapshot}'",
            exc=exc,
            body={"ok": False},
        )


# ── Config-restore-to-disk routes ─────────────────────────────────────────────


@restore_backup_bp.route("/api/config-extractions")
def api_config_extractions():
    """List previously-extracted S3 config backups available for apply-to-disk."""
    _require_mc_restore_backup()
    try:
        return jsonify({"ok": True, "extractions": ConfigRestoreService.list_extractions()})
    except Exception as exc:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to list config extractions",
            log_detail="api_config_extractions failed",
            exc=exc,
            body={"ok": False},
        )


@restore_backup_bp.route("/api/config-extractions/files")
def api_config_extraction_files():
    """List files inside an extracted config backup, with diff markers vs live."""
    _require_mc_restore_backup()
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    try:
        return jsonify({"ok": True, "files": ConfigRestoreService.list_extracted_files(name)})
    except (InvalidRequestError, RequestedItemNotFoundError) as exc:
        # validation message contract — see api_restore_local.
        return jsonify({"ok": False, "error": exc.public_message}), 400
    except Exception as exc:
        from lib.errors import safe_error_response

        return safe_error_response(
            "Failed to list extraction files",
            log_detail=f"api_config_extraction_files: failed for name '{name}'",
            exc=exc,
            body={"ok": False},
        )


@restore_backup_bp.route("/api/config-extractions/apply", methods=["POST"])
def api_config_apply():
    """Apply selected files from an extracted config backup to /app/reporting_configs.

    Body: {"name": str, "files": [str] | null}.  ``files=null`` means apply all.
    """
    _require_mc_restore_backup()
    step_up_err = _require_recent_step_up()
    if step_up_err is not None:
        return step_up_err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    files = data.get("files")
    if files is not None and not isinstance(files, list):
        return jsonify({"ok": False, "error": "files must be a list or null"}), 400
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400

    username = session.get("username", "unknown")
    try:
        file_count = "all" if files is None else len(files)
        logging.info(
            f"AUDIT: user={username} action=apply_config_backup extraction={name} " f"files={file_count} status=started"
        )
        result = ConfigRestoreService.apply_files(name, files=files)
        logging.info(
            f"AUDIT: user={username} action=apply_config_backup extraction={name} "
            f"applied={len(result['applied'])} skipped={len(result['skipped_missing'])} status=complete"
        )
        _audit(
            AUDIT_CONFIG_APPLY,
            True,
            detail=(
                f"extraction={name} files={file_count} applied={len(result['applied'])} "
                f"skipped={len(result['skipped_missing'])} rollback_dir={result.get('rollback_dir', '')}"
            ),
        )
        return jsonify({"ok": True, **result})
    except (InvalidRequestError, RequestedItemNotFoundError) as exc:
        # validation message contract — see api_restore_local.
        _audit(AUDIT_CONFIG_APPLY, False, detail=f"extraction={name}", failure_reason=str(exc)[:200])
        return jsonify({"ok": False, "error": exc.public_message}), 400
    except Exception as exc:
        from lib.errors import safe_error_response

        _audit(AUDIT_CONFIG_APPLY, False, detail=f"extraction={name}", failure_reason=type(exc).__name__)
        return safe_error_response(
            "Config apply failed",
            log_detail=f"api_config_apply: failed for name '{name}'",
            exc=exc,
            body={"ok": False},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _humanize_bytes(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} PB"
