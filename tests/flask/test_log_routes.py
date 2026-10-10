"""
Tests for import log routes.

Routes covered:
  GET  /logs                    list_logs
  GET  /logs/view/<filename>    view_log
  POST /logs/delete/<filename>  delete_log
  GET  /logs/download/<filename> download_log

All LogService calls are mocked.
"""

import io
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_LOGS = [
    {"filename": "import_2026-04-01.log", "size": 1024, "date": "2026-04-01"},
    {"filename": "import_2026-03-31.log", "size": 2048, "date": "2026-03-31"},
]

SAMPLE_LOG_CONTENT = (
    "2026-04-01 10:00:00 INFO Starting import\n"
    "2026-04-01 10:01:00 WARNING Slow response from API\n"
    "2026-04-01 10:02:00 ERROR Connection timeout\n"
    "2026-04-01 10:03:00 INFO Import complete"
)


# ===========================================================================
# GET /logs  — list_logs
# ===========================================================================


class TestListLogs:
    """Tests for the GET /logs route."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/logs")
        # before_request auth check should redirect or 401
        assert resp.status_code in (302, 401)

    @mock.patch("asset_management.web.app.LogService.get_recent_logs")
    def test_returns_200_with_logs(self, mock_get_logs, auth_client):
        mock_get_logs.return_value = SAMPLE_LOGS
        resp = auth_client.get("/logs")
        assert resp.status_code == 200
        mock_get_logs.assert_called_once_with(limit=20)

    @mock.patch("asset_management.web.app.LogService.get_recent_logs")
    def test_returns_200_with_empty_list(self, mock_get_logs, auth_client):
        mock_get_logs.return_value = []
        resp = auth_client.get("/logs")
        assert resp.status_code == 200

    @mock.patch("asset_management.web.app.LogService.get_recent_logs")
    def test_service_error_returns_500(self, mock_get_logs, auth_client):
        mock_get_logs.side_effect = RuntimeError("disk full")
        resp = auth_client.get("/logs")
        assert resp.status_code == 500

    @mock.patch("asset_management.web.app.LogService.get_recent_logs")
    def test_org_user_redirected(self, mock_get_logs, org_user_client):
        """Org users without asset_admin role are redirected away from logs."""
        mock_get_logs.return_value = SAMPLE_LOGS
        resp = org_user_client.get("/logs")
        assert resp.status_code == 302


# ===========================================================================
# GET /logs/view/<filename>  — view_log
# ===========================================================================


class TestViewLog:
    """Tests for the GET /logs/view/<filename> route."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/logs/view/import_2026-04-01.log")
        assert resp.status_code in (302, 401)

    @mock.patch("asset_management.web.app.LogService.get_line_class")
    @mock.patch("asset_management.web.app.LogService.read_log_file")
    def test_returns_200_with_content(self, mock_read, mock_class, auth_client):
        mock_read.return_value = SAMPLE_LOG_CONTENT
        mock_class.return_value = "log-info"
        resp = auth_client.get("/logs/view/import_2026-04-01.log")
        assert resp.status_code == 200
        mock_read.assert_called_once_with("import_2026-04-01.log")
        # get_line_class called once per line
        assert mock_class.call_count == len(SAMPLE_LOG_CONTENT.split("\n"))

    @mock.patch("asset_management.web.app.LogService.get_line_class")
    @mock.patch("asset_management.web.app.LogService.read_log_file")
    def test_empty_log_file(self, mock_read, mock_class, auth_client):
        mock_read.return_value = ""
        mock_class.return_value = ""
        resp = auth_client.get("/logs/view/empty.log")
        assert resp.status_code == 200

    @mock.patch("asset_management.web.app.LogService.read_log_file")
    def test_file_not_found_returns_404(self, mock_read, auth_client):
        mock_read.side_effect = FileNotFoundError("No such file")
        resp = auth_client.get("/logs/view/missing.log")
        assert resp.status_code == 404

    @mock.patch("asset_management.web.app.LogService.read_log_file")
    def test_general_error_returns_500(self, mock_read, auth_client):
        mock_read.side_effect = RuntimeError("permission denied")
        resp = auth_client.get("/logs/view/broken.log")
        assert resp.status_code == 500

    @mock.patch("asset_management.web.app.LogService.get_line_class")
    @mock.patch("asset_management.web.app.LogService.read_log_file")
    def test_line_class_called_for_each_line(self, mock_read, mock_class, auth_client):
        content = "line1\nline2\nline3"
        mock_read.return_value = content
        mock_class.side_effect = ["log-info", "log-warning", "log-error"]
        resp = auth_client.get("/logs/view/test.log")
        assert resp.status_code == 200
        assert mock_class.call_count == 3


# ===========================================================================
# POST /logs/delete/<filename>  — delete_log
# ===========================================================================


class TestDeleteLog:
    """Tests for the POST /logs/delete/<filename> route."""

    def test_unauthenticated_redirects(self, client):
        resp = client.post("/logs/delete/import_2026-04-01.log")
        assert resp.status_code in (302, 401)

    @mock.patch("asset_management.web.app.LogService.delete_log_file")
    def test_successful_delete_returns_json(self, mock_delete, auth_client):
        mock_delete.return_value = None
        resp = auth_client.post("/logs/delete/import_2026-04-01.log")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        mock_delete.assert_called_once_with("import_2026-04-01.log")

    @mock.patch("asset_management.web.app.LogService.delete_log_file")
    def test_delete_error_returns_500(self, mock_delete, auth_client):
        mock_delete.side_effect = RuntimeError("cannot delete")
        resp = auth_client.post("/logs/delete/import_2026-04-01.log")
        assert resp.status_code == 500
        data = resp.get_json()
        assert data["success"] is False
        # error body is now a generic message; the exception text
        # ("cannot delete") lands in the server log, not the response body.
        assert data["error"] == "Failed to delete log file"

    @mock.patch("asset_management.web.app.LogService.delete_log_file")
    def test_get_method_not_allowed(self, mock_delete, auth_client):
        resp = auth_client.get("/logs/delete/import_2026-04-01.log")
        assert resp.status_code == 405

    @mock.patch("asset_management.web.app.LogService.delete_log_file")
    def test_org_user_redirected(self, mock_delete, org_user_client):
        """Org users without asset_admin role are redirected away from log deletion."""
        resp = org_user_client.post("/logs/delete/test.log")
        assert resp.status_code == 302
        mock_delete.assert_not_called()


# ===========================================================================
# GET /logs/download/<filename>  — download_log
# ===========================================================================


class TestDownloadLog:
    """Tests for the GET /logs/download/<filename> route."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/logs/download/import_2026-04-01.log")
        assert resp.status_code in (302, 401)

    @mock.patch("asset_management.web.app.send_file")
    @mock.patch("asset_management.web.app.LogService.get_log_filepath")
    def test_successful_download(self, mock_filepath, mock_send, auth_client):
        mock_filepath.return_value = "/tmp/logs/import_2026-04-01.log"
        mock_send.return_value = auth_client.application.make_response("file data")
        resp = auth_client.get("/logs/download/import_2026-04-01.log")
        assert resp.status_code == 200
        mock_filepath.assert_called_once_with("import_2026-04-01.log")
        mock_send.assert_called_once_with(
            "/tmp/logs/import_2026-04-01.log",
            as_attachment=True,
            download_name="import_2026-04-01.log",
        )

    @mock.patch("asset_management.web.app.LogService.get_log_filepath")
    def test_file_not_found_returns_404(self, mock_filepath, auth_client):
        mock_filepath.side_effect = FileNotFoundError("No such file")
        resp = auth_client.get("/logs/download/missing.log")
        assert resp.status_code == 404

    @mock.patch("asset_management.web.app.LogService.get_log_filepath")
    def test_general_error_returns_500(self, mock_filepath, auth_client):
        mock_filepath.side_effect = RuntimeError("IO error")
        resp = auth_client.get("/logs/download/broken.log")
        assert resp.status_code == 500
