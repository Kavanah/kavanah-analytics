"""
Tests for the restore_backup blueprint's snapshot and config-apply routes.

Routes covered:
  GET  /restore-backup/api/snapshots/repos
  GET  /restore-backup/api/snapshots/list?repo=...
  GET  /restore-backup/api/snapshots/inspect?repo=...&snapshot=...
  POST /restore-backup/api/snapshots/restore
  GET  /restore-backup/api/config-extractions
  GET  /restore-backup/api/config-extractions/files?name=...
  POST /restore-backup/api/config-extractions/apply

All routes gate on the ``mc_restore_backup`` permission.
"""

import time
from unittest import mock

import pytest
from lib.errors import InvalidRequestError, RequestedItemNotFoundError  # what the services raise

# The blueprint imports services using either ``web.services.*`` or
# ``...services.*`` depending on how the package is loaded. The actual
# attribute names on the route module after import are SnapshotService and
# ConfigRestoreService — patch those.
SNAP_SERVICE = "asset_management.web.blueprints.restore_backup.routes.SnapshotService"
CFG_SERVICE = "asset_management.web.blueprints.restore_backup.routes.ConfigRestoreService"
PERM_SERVICE = "asset_management.web.blueprints.restore_backup.routes.RolePermissionsService"
STEP_UP_GATE = "asset_management.web.blueprints.restore_backup.routes._require_recent_step_up"
AUDIT_WRITE = "asset_management.web.services.audit_log_service.AuditLogService.log_event"


@pytest.fixture(autouse=True)
def _recent_step_up_and_quiet_audit():
    """the destructive routes now demand a second-factor step-up
    within the last 10 minutes and write a de_audit_log row per call. The
    gate and the audit are exercised on their own in
    the release-plan tests; here every test keeps
    its original subject, so the gate answers "recent" and the audit write is
    a no-op. The permission gate still runs for real (it precedes the step-up)."""
    with mock.patch(STEP_UP_GATE, return_value=None), mock.patch(AUDIT_WRITE):
        yield


def _custom_role_client(app, perm_value: bool):
    """Test client with a custom role and a mocked mc_restore_backup perm.

    Used to verify the new permission gate without depending on any
    seeded admin role — proves the Role Settings checkbox is what
    actually controls access.
    """
    from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["username"] = "customuser"
        sess["roles"] = ["custom_backup_operator"]
        sess["is_admin"] = False
        sess["full_name"] = "Custom User"
        sess["email"] = "custom@example.com"
        sess["auth_timestamp"] = time.time()
        sess["csrf_token"] = CSRF_TEST_TOKEN
    return CsrfTestClient(c)


# ===========================================================================
# Auth: mc_restore_backup permission required
# ===========================================================================

class TestRoutesRequireMcRestoreBackup:

    @pytest.mark.parametrize("url,method", [
        ("/restore-backup/api/snapshots/repos", "GET"),
        ("/restore-backup/api/snapshots/list?repo=daily_repo", "GET"),
        ("/restore-backup/api/snapshots/inspect?repo=daily_repo&snapshot=s1", "GET"),
        ("/restore-backup/api/snapshots/restore", "POST"),
        ("/restore-backup/api/config-extractions", "GET"),
        ("/restore-backup/api/config-extractions/files?name=x", "GET"),
        ("/restore-backup/api/config-extractions/apply", "POST"),
    ])
    def test_should_return_403_when_user_lacks_mc_restore_backup(self, org_user_client, url, method):
        if method == "GET":
            resp = org_user_client.get(url)
        else:
            resp = org_user_client.post(url, json={})
        assert resp.status_code == 403

    def test_page_redirects_when_user_lacks_mc_restore_backup(self, org_user_client):
        # Page-level 403 is rewritten to a redirect by the global handler.
        resp = org_user_client.get("/restore-backup/")
        assert resp.status_code == 302

    def test_custom_role_with_mc_restore_backup_can_access_page(self, app):
        c = _custom_role_client(app, perm_value=True)
        with mock.patch(
            f"{PERM_SERVICE}.get_merged_permissions",
            return_value={"mc_restore_backup": True},
        ):
            resp = c.get("/restore-backup/")
        assert resp.status_code == 200

    def test_custom_role_without_mc_restore_backup_blocked_from_page(self, app):
        c = _custom_role_client(app, perm_value=False)
        with mock.patch(
            f"{PERM_SERVICE}.get_merged_permissions",
            return_value={"mc_restore_backup": False},
        ):
            resp = c.get("/restore-backup/")
        # Page-level 403 → redirect via global handler.
        assert resp.status_code == 302

    def test_custom_role_with_mc_restore_backup_can_call_api(self, app):
        c = _custom_role_client(app, perm_value=True)
        with mock.patch(
            f"{PERM_SERVICE}.get_merged_permissions",
            return_value={"mc_restore_backup": True},
        ), mock.patch(f"{SNAP_SERVICE}.list_repos", return_value=[]):
            resp = c.get("/restore-backup/api/snapshots/repos")
        assert resp.status_code == 200

    def test_custom_role_without_mc_restore_backup_blocked_from_api(self, app):
        c = _custom_role_client(app, perm_value=False)
        with mock.patch(
            f"{PERM_SERVICE}.get_merged_permissions",
            return_value={"mc_restore_backup": False},
        ):
            resp = c.get("/restore-backup/api/snapshots/repos")
        assert resp.status_code == 403


# ===========================================================================
# GET /api/snapshots/repos
# ===========================================================================

class TestApiSnapshotRepos:

    def test_should_return_list_of_repos_when_admin(self, auth_client):
        repos = [{"name": "daily_repo", "type": "fs", "settings": {}}]
        with mock.patch(f"{SNAP_SERVICE}.list_repos", return_value=repos):
            resp = auth_client.get("/restore-backup/api/snapshots/repos")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["repos"][0]["name"] == "daily_repo"

    def test_should_return_503_when_es_unavailable(self, auth_client):
        from asset_management.web.services.snapshot_service import SnapshotServiceError
        with mock.patch(f"{SNAP_SERVICE}.list_repos", side_effect=SnapshotServiceError("ES unreachable")):
            resp = auth_client.get("/restore-backup/api/snapshots/repos")
        assert resp.status_code == 503


# ===========================================================================
# GET /api/snapshots/list
# ===========================================================================

class TestApiSnapshotList:

    def test_should_return_snapshots_for_repo(self, auth_client):
        result = {
            "snapshots": [{"snapshot": "s1", "state": "SUCCESS", "indices_count": 2,
                           "indices": ["a", "b"], "start_time": "2026-04-01T00:00:00Z", "end_time": ""}],
            "warnings": [],
        }
        with mock.patch(f"{SNAP_SERVICE}.list_snapshots", return_value=result) as m:
            resp = auth_client.get("/restore-backup/api/snapshots/list?repo=daily_repo")
        assert resp.status_code == 200
        m.assert_called_once_with("daily_repo")
        body = resp.get_json()
        assert body["snapshots"][0]["snapshot"] == "s1"
        assert body["warnings"] == []

    def test_should_surface_warnings_when_broken_snapshots_skipped(self, auth_client):
        result = {"snapshots": [], "warnings": ["broken_snap: NotFoundError"]}
        with mock.patch(f"{SNAP_SERVICE}.list_snapshots", return_value=result):
            resp = auth_client.get("/restore-backup/api/snapshots/list?repo=daily_repo")
        assert resp.status_code == 200
        assert resp.get_json()["warnings"] == ["broken_snap: NotFoundError"]

    def test_should_return_400_when_repo_missing(self, auth_client):
        resp = auth_client.get("/restore-backup/api/snapshots/list")
        assert resp.status_code == 400

    def test_should_return_400_when_repo_name_invalid(self, auth_client):
        with mock.patch(f"{SNAP_SERVICE}.list_snapshots", side_effect=InvalidRequestError("Invalid repository name")):
            resp = auth_client.get("/restore-backup/api/snapshots/list?repo=../bad")
        assert resp.status_code == 400


# ===========================================================================
# GET /api/snapshots/inspect
# ===========================================================================

class TestApiSnapshotInspect:

    def test_should_return_index_list_with_conflict_flags(self, auth_client):
        info = {
            "snapshot": "s1",
            "state": "SUCCESS",
            "start_time": "",
            "end_time": "",
            "indices": [
                {"name": "a", "exists_in_cluster": True},
                {"name": "b", "exists_in_cluster": False},
            ],
        }
        with mock.patch(f"{SNAP_SERVICE}.inspect_snapshot", return_value=info):
            resp = auth_client.get(
                "/restore-backup/api/snapshots/inspect?repo=daily_repo&snapshot=s1"
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["indices"][0]["exists_in_cluster"] is True

    def test_should_return_400_when_snapshot_missing(self, auth_client):
        with mock.patch(f"{SNAP_SERVICE}.inspect_snapshot", side_effect=RequestedItemNotFoundError("Snapshot not found")):
            resp = auth_client.get(
                "/restore-backup/api/snapshots/inspect?repo=daily_repo&snapshot=missing"
            )
        assert resp.status_code == 400


# ===========================================================================
# POST /api/snapshots/restore
# ===========================================================================

class TestApiSnapshotRestore:

    def test_should_restore_when_valid_payload(self, auth_client):
        result = {"restored": ["a"], "deleted_before_restore": [], "snapshot_state": "SUCCESS"}
        with mock.patch(f"{SNAP_SERVICE}.restore_indices", return_value=result) as m:
            resp = auth_client.post(
                "/restore-backup/api/snapshots/restore",
                json={"repo": "daily_repo", "snapshot": "s1", "indices": ["a"], "replace_existing": False},
            )
        assert resp.status_code == 200
        assert resp.get_json()["restored"] == ["a"]
        m.assert_called_once_with("daily_repo", "s1", ["a"], replace_existing=False)

    def test_should_return_400_when_indices_missing(self, auth_client):
        resp = auth_client.post(
            "/restore-backup/api/snapshots/restore",
            json={"repo": "daily_repo", "snapshot": "s1", "indices": []},
        )
        assert resp.status_code == 400

    def test_should_return_400_when_repo_missing(self, auth_client):
        resp = auth_client.post(
            "/restore-backup/api/snapshots/restore",
            json={"snapshot": "s1", "indices": ["a"]},
        )
        assert resp.status_code == 400

    def test_should_return_400_when_index_not_in_snapshot(self, auth_client):
        with mock.patch(f"{SNAP_SERVICE}.restore_indices",
                        side_effect=InvalidRequestError("Indices not in snapshot")):
            resp = auth_client.post(
                "/restore-backup/api/snapshots/restore",
                json={"repo": "daily_repo", "snapshot": "s1", "indices": ["x"]},
            )
        assert resp.status_code == 400

    def test_should_return_400_when_existing_without_replace(self, auth_client):
        with mock.patch(f"{SNAP_SERVICE}.restore_indices",
                        side_effect=InvalidRequestError("already exist")):
            resp = auth_client.post(
                "/restore-backup/api/snapshots/restore",
                json={"repo": "daily_repo", "snapshot": "s1", "indices": ["a"]},
            )
        assert resp.status_code == 400


# ===========================================================================
# Config extraction endpoints
# ===========================================================================

class TestApiConfigExtractions:

    def test_should_list_extractions(self, auth_client):
        ext = [{"name": "elk-config-20260415", "extracted_at": "2026-04-15 12:00:00 UTC", "file_count": 3}]
        with mock.patch(f"{CFG_SERVICE}.list_extractions", return_value=ext):
            resp = auth_client.get("/restore-backup/api/config-extractions")
        assert resp.status_code == 200
        assert resp.get_json()["extractions"][0]["name"] == "elk-config-20260415"

    def test_should_list_extracted_files_with_diff_markers(self, auth_client):
        files = [
            {"path": "master-config.yaml", "size_bytes": 100, "size_human": "100.0 B",
             "live_exists": True, "differs": True},
        ]
        with mock.patch(f"{CFG_SERVICE}.list_extracted_files", return_value=files):
            resp = auth_client.get("/restore-backup/api/config-extractions/files?name=elk-1")
        assert resp.status_code == 200
        assert resp.get_json()["files"][0]["differs"] is True

    def test_should_return_400_when_name_missing_on_files(self, auth_client):
        resp = auth_client.get("/restore-backup/api/config-extractions/files")
        assert resp.status_code == 400


class TestApiConfigApply:

    def test_should_apply_files_when_valid_payload(self, auth_client):
        result = {"applied": ["master-config.yaml"], "skipped_missing": [], "rollback_dir": "/tmp/rb"}
        with mock.patch(f"{CFG_SERVICE}.apply_files", return_value=result) as m:
            resp = auth_client.post(
                "/restore-backup/api/config-extractions/apply",
                json={"name": "elk-1", "files": ["master-config.yaml"]},
            )
        assert resp.status_code == 200
        m.assert_called_once_with("elk-1", files=["master-config.yaml"])

    def test_should_apply_all_files_when_files_is_null(self, auth_client):
        result = {"applied": ["a", "b"], "skipped_missing": [], "rollback_dir": "/tmp/rb"}
        with mock.patch(f"{CFG_SERVICE}.apply_files", return_value=result) as m:
            resp = auth_client.post(
                "/restore-backup/api/config-extractions/apply",
                json={"name": "elk-1", "files": None},
            )
        assert resp.status_code == 200
        m.assert_called_once_with("elk-1", files=None)

    def test_should_return_400_when_name_missing(self, auth_client):
        resp = auth_client.post(
            "/restore-backup/api/config-extractions/apply",
            json={"files": ["a"]},
        )
        assert resp.status_code == 400

    def test_should_return_400_when_files_not_list(self, auth_client):
        resp = auth_client.post(
            "/restore-backup/api/config-extractions/apply",
            json={"name": "elk-1", "files": "not-a-list"},
        )
        assert resp.status_code == 400

    def test_should_return_400_when_path_traversal(self, auth_client):
        with mock.patch(f"{CFG_SERVICE}.apply_files", side_effect=InvalidRequestError("Invalid file path")):
            resp = auth_client.post(
                "/restore-backup/api/config-extractions/apply",
                json={"name": "elk-1", "files": ["../escape.yaml"]},
            )
        assert resp.status_code == 400
