"""
Tests for backup API routes.

Routes covered:
  POST /api/backup/create     api_backup_create
  GET  /api/backup/list       api_backup_list
  POST /api/backup/restore    api_backup_restore

All backup routes require an authenticated session with asset_admin or
superuser role.  All BackupService calls are mocked; no real filesystem
or Elasticsearch instance is required.
"""

from unittest import mock

import pytest
from lib.errors import InvalidRequestError, RequestedItemNotFoundError  # what the services raise


# ===========================================================================
# Auth: unauthenticated access
# ===========================================================================

class TestBackupAuthRequired:
    """All backup routes require authentication."""

    @pytest.mark.parametrize("url,method", [
        ("/api/backup/create", "POST"),
        ("/api/backup/list", "GET"),
        ("/api/backup/restore", "POST"),
    ])
    def test_unauthenticated_redirects_to_login(self, client, url, method):
        if method == "GET":
            resp = client.get(url)
        else:
            resp = client.post(url, json={})
        assert resp.status_code == 401
        assert "login" in (resp.get_json() or {}).get("login_url", "").lower()


# ===========================================================================
# Auth: insufficient role returns 403
# ===========================================================================

class TestBackupRoleRequired:
    """Backup routes require asset_admin or superuser role."""

    def test_create_backup_non_admin_returns_403(self, org_user_client):
        resp = org_user_client.post("/api/backup/create")
        assert resp.status_code == 403

    def test_list_backups_non_admin_returns_403(self, org_user_client):
        resp = org_user_client.get("/api/backup/list")
        assert resp.status_code == 403

    def test_restore_backup_non_admin_returns_403(self, org_user_client):
        resp = org_user_client.post("/api/backup/restore",
                                    json={"filename": "backup.json"})
        assert resp.status_code == 403


# ===========================================================================
# POST /api/backup/create
# ===========================================================================

class TestBackupCreate:

    def test_successful_create_returns_filename(self, auth_client):
        result = {"filename": "asset-backup-20260403-120000.tar.gz", "pruned": []}
        with mock.patch("asset_management.web.app.BackupService.create_backup",
                        return_value=result) as m:
            resp = auth_client.post("/api/backup/create")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["filename"] == "asset-backup-20260403-120000.tar.gz"
        assert data["pruned"] == []
        m.assert_called_once()

    def test_response_includes_pruned_filenames_when_old_backups_removed(self, auth_client):
        result = {"filename": "asset-backup-20260403-120000.tar.gz",
                  "pruned": ["asset-backup-20260301-120000.tar.gz"]}
        with mock.patch("asset_management.web.app.BackupService.create_backup",
                        return_value=result):
            resp = auth_client.post("/api/backup/create")
        assert resp.status_code == 200
        assert resp.get_json()["pruned"] == ["asset-backup-20260301-120000.tar.gz"]

    def test_superuser_can_create_backup(self, superuser_client):
        with mock.patch("asset_management.web.app.BackupService.create_backup",
                        return_value={"filename": "asset-backup-x.tar.gz", "pruned": []}):
            resp = superuser_client.post("/api/backup/create")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_create_backup_service_error_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.create_backup",
                        side_effect=RuntimeError("disk full")):
            resp = auth_client.post("/api/backup/create")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["ok"] is False
        # error body is now a generic message; the exception text
        # ("disk full") lands in the server log, not the response body.
        assert data["error"] == "Backup failed"


# ===========================================================================
# GET /api/backup/list
# ===========================================================================

class TestBackupList:

    def test_successful_list_returns_backups(self, auth_client):
        mock_backups = [
            {"filename": "backup_001.json", "size": 1024, "created": "2026-04-01"},
            {"filename": "backup_002.json", "size": 2048, "created": "2026-04-02"},
        ]
        with mock.patch("asset_management.web.app.BackupService.list_backups",
                        return_value=mock_backups) as m:
            resp = auth_client.get("/api/backup/list")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["backups"]) == 2
        assert data["backups"][0]["filename"] == "backup_001.json"
        m.assert_called_once()

    def test_superuser_can_list_backups(self, superuser_client):
        with mock.patch("asset_management.web.app.BackupService.list_backups",
                        return_value=[]):
            resp = superuser_client.get("/api/backup/list")
        assert resp.status_code == 200
        assert resp.get_json()["backups"] == []

    def test_list_backups_empty(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.list_backups",
                        return_value=[]):
            resp = auth_client.get("/api/backup/list")
        assert resp.status_code == 200
        assert resp.get_json()["backups"] == []

    def test_list_backups_service_error_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.list_backups",
                        side_effect=RuntimeError("ES connection failed")):
            resp = auth_client.get("/api/backup/list")
        assert resp.status_code == 500
        data = resp.get_json()
        # generic client-facing message; exception text is in the
        # server log only.
        assert data["error"] == "Failed to list backups"


# ===========================================================================
# POST /api/backup/restore
# ===========================================================================

class TestBackupRestore:

    def test_successful_restore(self, auth_client):
        mock_result = {"indices_restored": 5, "documents_restored": 120}
        with mock.patch("asset_management.web.app.BackupService.restore_backup",
                        return_value=mock_result) as m:
            resp = auth_client.post("/api/backup/restore",
                                    json={"filename": "backup_001.json"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["indices_restored"] == 5
        m.assert_called_once_with("backup_001.json")

    def test_superuser_can_restore_backup(self, superuser_client):
        with mock.patch("asset_management.web.app.BackupService.restore_backup",
                        return_value={"indices_restored": 1}):
            resp = superuser_client.post("/api/backup/restore",
                                         json={"filename": "backup.json"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_missing_filename_returns_400(self, auth_client):
        resp = auth_client.post("/api/backup/restore", json={})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "filename" in data["error"].lower()

    def test_empty_filename_returns_400(self, auth_client):
        resp = auth_client.post("/api/backup/restore",
                                json={"filename": ""})
        assert resp.status_code == 400

    def test_whitespace_only_filename_returns_400(self, auth_client):
        resp = auth_client.post("/api/backup/restore",
                                json={"filename": "   "})
        assert resp.status_code == 400

    def test_file_not_found_returns_400(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.restore_backup",
                        side_effect=RequestedItemNotFoundError("backup not found")):
            resp = auth_client.post("/api/backup/restore",
                                    json={"filename": "nonexistent.json"})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False
        assert "not found" in data["error"].lower()

    def test_value_error_returns_400(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.restore_backup",
                        side_effect=InvalidRequestError("invalid backup format")):
            resp = auth_client.post("/api/backup/restore",
                                    json={"filename": "corrupt.json"})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["ok"] is False

    def test_unexpected_error_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.BackupService.restore_backup",
                        side_effect=RuntimeError("unexpected failure")):
            resp = auth_client.post("/api/backup/restore",
                                    json={"filename": "backup.json"})
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["ok"] is False
        # generic client-facing message; exception text is in the
        # server log only.
        assert data["error"] == "Restore failed"

    def test_restore_without_json_body_returns_400(self, auth_client):
        """POST with no JSON body should treat filename as empty."""
        resp = auth_client.post("/api/backup/restore",
                                content_type="application/json",
                                data="{}")
        assert resp.status_code == 400
