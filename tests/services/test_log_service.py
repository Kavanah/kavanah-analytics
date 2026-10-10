"""
Tests for the Log Service.

Covers:
  _format_size            -- bytes, KB, MB, GB, TB, invalid input
  get_line_class          -- error, success, info, default, special cases
  get_recent_logs         -- normal listing, empty directory, limit, exception
  read_log_file           -- success, not found, path traversal sanitization
  get_log_filepath        -- success, not found, not a file
  delete_log_file         -- success, not found, not a file, OS error
  get_most_recent_log_errors -- with errors, no errors, no logs, read failure
  get_import_summary      -- per-source record counts, errors, unmatched lines
"""

import os
from unittest import mock

import pytest

from asset_management.web.services.log_service import LogService


# ---------------------------------------------------------------------------
# _format_size
# ---------------------------------------------------------------------------


class TestFormatSize:

    def test_bytes(self):
        assert LogService._format_size(500) == "500.0 B"

    def test_zero_bytes(self):
        assert LogService._format_size(0) == "0.0 B"

    def test_kilobytes(self):
        result = LogService._format_size(2048)
        assert result == "2.0 KB"

    def test_megabytes(self):
        result = LogService._format_size(1048576)
        assert result == "1.0 MB"

    def test_gigabytes(self):
        result = LogService._format_size(1073741824)
        assert result == "1.0 GB"

    def test_terabytes(self):
        result = LogService._format_size(1099511627776)
        assert result == "1.0 TB"

    def test_fractional_kilobytes(self):
        # 1536 bytes = 1.5 KB
        result = LogService._format_size(1536)
        assert result == "1.5 KB"

    def test_invalid_string_passthrough(self):
        result = LogService._format_size("not-a-number")
        assert result == "not-a-number"

    def test_none_passthrough(self):
        result = LogService._format_size(None)
        assert result is None


# ---------------------------------------------------------------------------
# get_line_class
# ---------------------------------------------------------------------------


class TestGetLineClass:

    # Error patterns
    def test_error_keyword(self):
        assert LogService.get_line_class("Something ERROR happened") == "log-error"

    def test_failed_keyword(self):
        assert LogService.get_line_class("Import failed for account") == "log-error"

    def test_does_not_exist_keyword(self):
        assert LogService.get_line_class("Index does not exist") == "log-error"

    def test_access_denied_keyword(self):
        assert LogService.get_line_class("Access denied for user") == "log-error"

    def test_exception_keyword(self):
        assert LogService.get_line_class("Exception raised during import") == "log-error"

    # Special case: "does not exist" + "creating" is info, not error
    def test_does_not_exist_creating_is_info(self):
        assert LogService.get_line_class("Index does not exist, creating now") == "log-info"

    # Success patterns
    def test_success_keyword(self):
        assert LogService.get_line_class("Operation completed with success") == "log-success"

    def test_imported_keyword(self):
        assert LogService.get_line_class("50 records imported") == "log-success"

    def test_completed_keyword(self):
        assert LogService.get_line_class("Import completed") == "log-success"

    def test_created_successfully_keyword(self):
        assert LogService.get_line_class("Index created successfully") == "log-success"

    # Info patterns
    def test_checking_keyword(self):
        assert LogService.get_line_class("Checking index status") == "log-info"

    def test_creating_keyword(self):
        assert LogService.get_line_class("Creating new index") == "log-info"

    def test_processing_keyword(self):
        assert LogService.get_line_class("Processing batch 3 of 10") == "log-info"

    def test_fetching_keyword(self):
        assert LogService.get_line_class("Fetching data from API") == "log-info"

    # Default
    def test_default_class(self):
        assert LogService.get_line_class("Just a regular line") == "log-default"

    def test_empty_line(self):
        assert LogService.get_line_class("") == "log-default"

    # Case insensitivity
    def test_case_insensitive_error(self):
        assert LogService.get_line_class("ERROR: something broke") == "log-error"

    def test_case_insensitive_success(self):
        assert LogService.get_line_class("IMPORTED 100 records") == "log-success"


# ---------------------------------------------------------------------------
# get_recent_logs
# ---------------------------------------------------------------------------


class TestGetRecentLogs:

    @mock.patch("asset_management.web.services.log_service.os.stat")
    @mock.patch("asset_management.web.services.log_service.os.path.isfile")
    @mock.patch("asset_management.web.services.log_service.glob.glob")
    def test_returns_formatted_files(self, mock_glob, mock_isfile, mock_stat):
        mock_glob.return_value = [
            "/app/data/logs/imports_20240101.log",
            "/app/data/logs/imports_20240102.log",
        ]
        mock_isfile.return_value = True

        stat1 = mock.MagicMock()
        stat1.st_size = 2048
        stat1.st_mtime = 1706745600.0  # older

        stat2 = mock.MagicMock()
        stat2.st_size = 4096
        stat2.st_mtime = 1706832000.0  # newer

        mock_stat.side_effect = [stat1, stat2]

        result = LogService.get_recent_logs()

        assert len(result) == 2
        # Most recent first
        assert result[0]["name"] == "imports_20240102.log"
        assert result[1]["name"] == "imports_20240101.log"
        # Check formatted size
        assert result[0]["size"] == "4.0 KB"
        assert result[0]["raw_size"] == 4096
        # Check keys exist
        assert "modified" in result[0]

    @mock.patch("asset_management.web.services.log_service.glob.glob")
    def test_empty_directory(self, mock_glob):
        mock_glob.return_value = []
        result = LogService.get_recent_logs()
        assert result == []

    @mock.patch("asset_management.web.services.log_service.os.stat")
    @mock.patch("asset_management.web.services.log_service.os.path.isfile")
    @mock.patch("asset_management.web.services.log_service.glob.glob")
    def test_limit_parameter(self, mock_glob, mock_isfile, mock_stat):
        mock_glob.return_value = [
            "/app/data/logs/imports_a.log",
            "/app/data/logs/imports_b.log",
            "/app/data/logs/imports_c.log",
        ]
        mock_isfile.return_value = True

        def make_stat(size, mtime):
            s = mock.MagicMock()
            s.st_size = size
            s.st_mtime = mtime
            return s

        mock_stat.side_effect = [
            make_stat(100, 1000.0),
            make_stat(200, 2000.0),
            make_stat(300, 3000.0),
        ]

        result = LogService.get_recent_logs(limit=2)
        assert len(result) == 2

    @mock.patch("asset_management.web.services.log_service.os.path.isfile")
    @mock.patch("asset_management.web.services.log_service.glob.glob")
    def test_skips_directories(self, mock_glob, mock_isfile):
        mock_glob.return_value = [
            "/app/data/logs/imports_subdir",
            "/app/data/logs/imports_file.log",
        ]
        mock_isfile.side_effect = [False, True]

        stat_obj = mock.MagicMock()
        stat_obj.st_size = 100
        stat_obj.st_mtime = 1000.0

        with mock.patch("asset_management.web.services.log_service.os.stat", return_value=stat_obj):
            result = LogService.get_recent_logs()

        assert len(result) == 1
        assert result[0]["name"] == "imports_file.log"

    @mock.patch("asset_management.web.services.log_service.os.stat")
    @mock.patch("asset_management.web.services.log_service.os.path.isfile")
    @mock.patch("asset_management.web.services.log_service.glob.glob")
    def test_excludes_non_import_logs(self, mock_glob, mock_isfile, mock_stat):
        """should list only imports* logs and drop system/release-plan/load-test logs"""
        mock_glob.return_value = [
            "/app/data/logs/imports_20240101.log",
            "/app/data/logs/imports_manual.log",
            "/app/data/logs/pytest_20240101.log",  # system/testing log
            "/app/data/logs/release_plan_PR-93_pytest_20240101.log",  # release-plan log
            "/app/data/logs/locust_20240101.log",  # load-test log
        ]
        mock_isfile.return_value = True

        stat_obj = mock.MagicMock()
        stat_obj.st_size = 100
        stat_obj.st_mtime = 1000.0
        mock_stat.return_value = stat_obj

        result = LogService.get_recent_logs()

        names = {r["name"] for r in result}
        assert names == {"imports_20240101.log", "imports_manual.log"}

    @mock.patch("asset_management.web.services.log_service.glob.glob")
    def test_exception_raises(self, mock_glob):
        mock_glob.side_effect = PermissionError("no access")
        with pytest.raises(Exception, match="Failed to list log files"):
            LogService.get_recent_logs()


# ---------------------------------------------------------------------------
# read_log_file
# ---------------------------------------------------------------------------


class TestReadLogFile:

    @mock.patch("builtins.open", mock.mock_open(read_data="line1\nline2\n"))
    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=True)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_reads_file_content(self, mock_exists, mock_isfile):
        result = LogService.read_log_file("test.log")
        assert result == "line1\nline2\n"

    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=False)
    def test_file_not_found(self, mock_exists):
        with pytest.raises(FileNotFoundError, match="Log file not found"):
            LogService.read_log_file("missing.log")

    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=False)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_not_a_file(self, mock_exists, mock_isfile):
        with pytest.raises(Exception, match="Not a file"):
            LogService.read_log_file("somedir")

    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=True)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_sanitizes_path_traversal(self, mock_exists, mock_isfile):
        """Path traversal attempts are stripped by os.path.basename + werkzeug.safe_join.

         Phase 4: ``../../etc/passwd`` is reduced to ``passwd`` and
        looked up under LOG_DIR. The lookup falls through the existence
        checks (mocked True here) and reaches ``open``.
        """
        with mock.patch("builtins.open", mock.mock_open(read_data="ok")) as mo:
            LogService.read_log_file("../../etc/passwd")
        # open should be called with sanitized path under LOG_DIR
        expected = os.path.join(LogService.LOG_DIR, "passwd")
        mo.assert_called_once_with(expected, "r", encoding="utf-8", errors="replace")

    @mock.patch("builtins.open", side_effect=IOError("disk error"))
    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=True)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_read_io_error(self, mock_exists, mock_isfile, mock_open):
        with pytest.raises(Exception, match="Failed to read log file"):
            LogService.read_log_file("test.log")


# ---------------------------------------------------------------------------
# get_log_filepath
# ---------------------------------------------------------------------------


class TestGetLogFilepath:

    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=True)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_returns_full_path(self, mock_exists, mock_isfile):
        result = LogService.get_log_filepath("test.log")
        assert result == os.path.join(LogService.LOG_DIR, "test.log")

    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=False)
    def test_file_not_found(self, mock_exists):
        with pytest.raises(FileNotFoundError):
            LogService.get_log_filepath("missing.log")

    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=False)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_not_a_file(self, mock_exists, mock_isfile):
        with pytest.raises(Exception, match="Not a file"):
            LogService.get_log_filepath("somedir")

    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=True)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_sanitizes_path(self, mock_exists, mock_isfile):
        result = LogService.get_log_filepath("../../../etc/shadow")
        assert result == os.path.join(LogService.LOG_DIR, "shadow")


# ---------------------------------------------------------------------------
# delete_log_file
# ---------------------------------------------------------------------------


class TestDeleteLogFile:

    @mock.patch("asset_management.web.services.log_service.os.remove")
    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=True)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_delete_success(self, mock_exists, mock_isfile, mock_remove):
        result = LogService.delete_log_file("old.log")
        assert result is True
        mock_remove.assert_called_once_with(os.path.join(LogService.LOG_DIR, "old.log"))

    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=False)
    def test_delete_not_found(self, mock_exists):
        with pytest.raises(FileNotFoundError, match="Log file not found"):
            LogService.delete_log_file("missing.log")

    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=False)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_delete_not_a_file(self, mock_exists, mock_isfile):
        with pytest.raises(Exception, match="Not a file"):
            LogService.delete_log_file("somedir")

    @mock.patch("asset_management.web.services.log_service.os.remove", side_effect=PermissionError("denied"))
    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=True)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_delete_os_error(self, mock_exists, mock_isfile, mock_remove):
        with pytest.raises(Exception, match="Failed to delete log file"):
            LogService.delete_log_file("test.log")

    @mock.patch("asset_management.web.services.log_service.os.remove")
    @mock.patch("asset_management.web.services.log_service.os.path.isfile", return_value=True)
    @mock.patch("asset_management.web.services.log_service.os.path.exists", return_value=True)
    def test_delete_sanitizes_path(self, mock_exists, mock_isfile, mock_remove):
        LogService.delete_log_file("../../etc/passwd")
        mock_remove.assert_called_once_with(os.path.join(LogService.LOG_DIR, "passwd"))


# ---------------------------------------------------------------------------
# get_most_recent_log_errors
# ---------------------------------------------------------------------------


class TestGetMostRecentLogErrors:

    @mock.patch.object(LogService, "read_log_file")
    @mock.patch.object(LogService, "get_recent_logs")
    def test_with_errors(self, mock_recent, mock_read):
        mock_recent.return_value = [{"name": "imports_20260115.log", "size": "1.0 KB", "modified": "Jan 15 10:00"}]
        mock_read.return_value = (
            "Starting import\n" "ERROR: connection failed\n" "Processing records\n" "Failed to send data\n"
        )
        result = LogService.get_most_recent_log_errors()

        assert result["filename"] == "imports_20260115.log"
        assert result["date"] == "Jan 15 10:00"
        assert result["has_errors"] is True
        assert len(result["errors"]) == 2

    @mock.patch.object(LogService, "read_log_file")
    @mock.patch.object(LogService, "get_recent_logs")
    def test_no_errors(self, mock_recent, mock_read):
        mock_recent.return_value = [{"name": "imports_20260115.log", "size": "512.0 B", "modified": "Jan 15 10:00"}]
        mock_read.return_value = "All imports completed\nSuccess\n"
        result = LogService.get_most_recent_log_errors()

        assert result["has_errors"] is False
        assert result["errors"] == []

    @mock.patch.object(LogService, "get_recent_logs")
    def test_no_logs_available(self, mock_recent):
        mock_recent.return_value = []
        result = LogService.get_most_recent_log_errors()

        assert result["filename"] is None
        assert result["date"] is None
        assert result["has_errors"] is False

    @mock.patch.object(LogService, "read_log_file", side_effect=Exception("read failed"))
    @mock.patch.object(LogService, "get_recent_logs")
    def test_read_failure_returns_empty_errors(self, mock_recent, mock_read):
        mock_recent.return_value = [{"name": "imports_20260115.log", "size": "1.0 KB", "modified": "Jan 15 10:00"}]
        result = LogService.get_most_recent_log_errors()

        assert result["filename"] == "imports_20260115.log"
        assert result["has_errors"] is False
        assert result["errors"] == []

    @mock.patch.object(LogService, "read_log_file")
    @mock.patch.object(LogService, "get_recent_logs")
    def test_blank_lines_ignored(self, mock_recent, mock_read):
        mock_recent.return_value = [{"name": "imports_20260115.log", "size": "1.0 KB", "modified": "Jan 15 10:00"}]
        mock_read.return_value = "\n\n\n"
        result = LogService.get_most_recent_log_errors()

        assert result["has_errors"] is False
        assert result["errors"] == []


# ---------------------------------------------------------------------------
# get_import_summary
# ---------------------------------------------------------------------------


class TestGetImportSummary:

    def test_facebook_ads_records(self):
        content = (
            "[fb-run-daily-imports] Starting Facebook Ads import\n"
            "[fb-json-to-elastic] Completed sending 42 documents to elasticsearch\n"
        )
        result = LogService.get_import_summary(content)
        fb_ads = next(s for s in result if s["name"] == "Facebook Ads")
        assert fb_ads["ran"] is True
        assert fb_ads["records"] == 42
        assert fb_ads["errors"] == 0

    def test_google_ads_records(self):
        content = (
            "[google-ads-run-daily-imports] Starting Google Ads import\n"
            "[google-ads-json-to-elastic] Total documents indexed: 100\n"
        )
        result = LogService.get_import_summary(content)
        ga = next(s for s in result if s["name"] == "Google Ads")
        assert ga["ran"] is True
        assert ga["records"] == 100

    def test_echo_records(self):
        content = (
            "[echo-run-daily-imports] Starting Echo import\n" "[echo-get-conversations] Total conversations processed: 55\n"
        )
        result = LogService.get_import_summary(content)
        echo = next(s for s in result if s["name"] == "Echo")
        assert echo["ran"] is True
        assert echo["records"] == 55

    def test_source_not_ran(self):
        content = "[fb-run-daily-imports] Starting Facebook Ads import\n"
        result = LogService.get_import_summary(content)
        google = next(s for s in result if s["name"] == "Google Ads")
        assert google["ran"] is False
        assert google["records"] == 0
        assert google["errors"] == 0

    def test_error_lines_counted(self):
        content = "[fb-run-daily-imports] Starting Facebook Ads import\n" "[fb-get-insights] ERROR: API rate limit exceeded\n"
        result = LogService.get_import_summary(content)
        fb_ads = next(s for s in result if s["name"] == "Facebook Ads")
        assert fb_ads["errors"] == 1

    def test_errors_zero_not_counted(self):
        """Lines mentioning 'Errors: 0' should not be counted as errors."""
        content = "[fb-run-daily-imports] Processed: 50, Errors: 0\n"
        result = LogService.get_import_summary(content)
        fb_ads = next(s for s in result if s["name"] == "Facebook Ads")
        assert fb_ads["errors"] == 0

    def test_processed_errors_summary_not_counted(self):
        """Lines like 'Processed: N, Errors: N' are informational summaries."""
        content = "[fb-run-daily-imports] Processed: 50, Errors: 3\n"
        result = LogService.get_import_summary(content)
        fb_ads = next(s for s in result if s["name"] == "Facebook Ads")
        assert fb_ads["errors"] == 0

    def test_multiple_record_lines_summed(self):
        content = (
            "[fb-json-to-elastic] Completed sending 10 documents to es\n"
            "[fb-json-to-elastic] Completed sending 20 documents to es\n"
        )
        result = LogService.get_import_summary(content)
        fb_ads = next(s for s in result if s["name"] == "Facebook Ads")
        assert fb_ads["records"] == 30

    def test_should_count_instagram_records_when_sender_logs_parenthesized_plural(self):
        """the IG sender logs 'document(s)', not 'documents'."""
        content = (
            "[ig-run-daily-imports] Starting Instagram import\n"
            "[ig-json-to-elastic] Completed sending 126 Instagram document(s) to "
            "3 team-specific indices. Errors: 0\n"
        )
        result = LogService.get_import_summary(content)
        ig = next(s for s in result if s["name"] == "Instagram")
        assert ig["records"] == 126
        assert ig["errors"] == 0

    def test_should_count_facebook_pages_records_when_sender_logs_parenthesized_plural(self):
        """the page/post senders log 'document(s)', not 'documents'."""
        content = (
            "[fb-run-page-daily-imports] Starting Facebook Pages import\n"
            "[fb-page-json-to-elastic] Completed sending 57 document(s) to "
            "2 team-specific indices. Errors: 0\n"
        )
        result = LogService.get_import_summary(content)
        pages = next(s for s in result if s["name"] == "Facebook Pages")
        assert pages["records"] == 57
        assert pages["errors"] == 0

    def test_should_not_count_parenthesized_completion_line_as_error(self):
        """knock-on — an unmatched completion line with 'Errors: N'
        (N > 0) was falling through to the error counter."""
        content = "[ig-json-to-elastic] Completed sending 10 Instagram document(s) to " "1 team-specific indices. Errors: 2\n"
        result = LogService.get_import_summary(content)
        ig = next(s for s in result if s["name"] == "Instagram")
        assert ig["records"] == 10
        assert ig["errors"] == 0

    def test_empty_content(self):
        result = LogService.get_import_summary("")
        assert len(result) == len(LogService._SUMMARY_SOURCES)
        for source in result:
            assert source["ran"] is False
            assert source["records"] == 0
            assert source["errors"] == 0

    def test_unrecognized_prefix_ignored(self):
        content = "[unknown-prefix] Some random log line with error\n"
        result = LogService.get_import_summary(content)
        for source in result:
            assert source["ran"] is False

    def test_lines_without_prefix_ignored(self):
        content = "No prefix here\nAlso no prefix\n"
        result = LogService.get_import_summary(content)
        for source in result:
            assert source["ran"] is False

    def test_all_sources_present(self):
        result = LogService.get_import_summary("")
        names = {s["name"] for s in result}
        expected = {
            "Facebook Ads",
            "Facebook Pages",
            "Google Ads",
            "Google Analytics",
            "Echo",
            "Instagram",
            "YouTube",
            "Chatwoot",
            "Disciple Tools",
            "Bitly",
            "TikTok",
        }
        assert names == expected

    def test_bitly_records(self):
        # Phase 2 — the summary table recognizes the bitly stages'
        # define() tokens and the sender's "Total documents indexed" line.
        content = "[bitly-json-to-elastic] Total documents indexed: 21\n"
        result = LogService.get_import_summary(content)
        bitly = next(s for s in result if s["name"] == "Bitly")
        assert bitly["ran"] is True
        assert bitly["records"] == 21

    def test_instagram_records(self):
        content = "[ig-json-to-elastic] Completed sending 75 Instagram documents to es\n"
        result = LogService.get_import_summary(content)
        ig = next(s for s in result if s["name"] == "Instagram")
        assert ig["ran"] is True
        assert ig["records"] == 75

    def test_youtube_records(self):
        content = "[youtube-analytics-json-to-elastic] Successfully indexed: 30\n"
        result = LogService.get_import_summary(content)
        yt = next(s for s in result if s["name"] == "YouTube")
        assert yt["ran"] is True
        assert yt["records"] == 30

    def test_chatwoot_records(self):
        content = "[chatwoot-conversations-to-elastic] Indexed 12 conversation documents\n"
        result = LogService.get_import_summary(content)
        cw = next(s for s in result if s["name"] == "Chatwoot")
        assert cw["ran"] is True
        assert cw["records"] == 12

    def test_facebook_pages_records(self):
        content = "[fb-page-json-to-elastic] Completed sending 25 documents to es\n"
        result = LogService.get_import_summary(content)
        fbp = next(s for s in result if s["name"] == "Facebook Pages")
        assert fbp["ran"] is True
        assert fbp["records"] == 25

    def test_google_analytics_records(self):
        content = "[ga-json-to-elastic] Total documents indexed: 200\n"
        result = LogService.get_import_summary(content)
        ga4 = next(s for s in result if s["name"] == "Google Analytics")
        assert ga4["ran"] is True
        assert ga4["records"] == 200

    def test_disciple_tools_records(self):
        # the DT send step's success line, as emitted by
        # send_json_to_elastic ("DT send: indexed N doc(s) into M team index(es).").
        content = (
            "[dt-run-daily-imports] DT import: starting\n"
            "[dt-json-to-elastic] DT send: indexed 64 doc(s) into 3 team index(es). Errors: 0\n"
        )
        result = LogService.get_import_summary(content)
        dt = next(s for s in result if s["name"] == "Disciple Tools")
        assert dt["ran"] is True
        assert dt["records"] == 64
        assert dt["errors"] == 0

    def test_disciple_tools_errors_counted(self):
        # A real DT failure ("DT fetch: ... failed") classifies as an error line.
        content = (
            "[dt-run-daily-imports] DT import: starting\n" "[dt-fetch] DT fetch: contacts @ example.org failed: HTTP 500\n"
        )
        result = LogService.get_import_summary(content)
        dt = next(s for s in result if s["name"] == "Disciple Tools")
        assert dt["ran"] is True
        assert dt["errors"] == 1

    def test_runtime_calculated_from_timestamps(self):
        content = (
            "2025-04-04 10:00:00 [INFO] [fb-run-daily-imports]  Starting Facebook Ads import\n"
            "2025-04-04 10:02:30 [INFO] [fb-json-to-elastic]  Completed sending 42 documents to es\n"
        )
        result = LogService.get_import_summary(content)
        fb_ads = next(s for s in result if s["name"] == "Facebook Ads")
        assert fb_ads["ran"] is True
        assert fb_ads["records"] == 42
        assert fb_ads["runtime"] == "2m 30s"

    def test_runtime_none_without_timestamps(self):
        content = "[fb-run-daily-imports] Starting Facebook Ads import\n"
        result = LogService.get_import_summary(content)
        fb_ads = next(s for s in result if s["name"] == "Facebook Ads")
        assert fb_ads["ran"] is True
        assert fb_ads["runtime"] is None

    def test_runtime_zero_for_single_timestamp(self):
        content = "2025-04-04 10:00:00 [INFO] [fb-run-daily-imports]  Starting\n"
        result = LogService.get_import_summary(content)
        fb_ads = next(s for s in result if s["name"] == "Facebook Ads")
        assert fb_ads["runtime"] == "0m 00s"

    def test_all_sources_have_runtime_key(self):
        result = LogService.get_import_summary("")
        for source in result:
            assert "runtime" in source

    def test_runtime_never_spans_whole_day_across_runs(self):
        """import-run-begin markers keep runtime a single-run
        span, never the whole-day span. A cron run at 06:00 + a backfill at
        14:30 must NOT read as ~8h."""
        content = (
            "2026-07-16 06:00:00 [INFO] [import-run-begin]  run start\n"
            "2026-07-16 06:00:00 [INFO] [fb-run-daily-imports]  Starting\n"
            "2026-07-16 06:02:00 [INFO] [fb-json-to-elastic]  Completed sending 10 documents to es\n"
            "2026-07-16 14:30:00 [INFO] [import-run-begin]  run start\n"
            "2026-07-16 14:30:00 [INFO] [fb-run-daily-imports]  Starting backfill\n"
            "2026-07-16 14:33:30 [INFO] [fb-json-to-elastic]  Completed sending 5 documents to es\n"
        )
        result = LogService.get_import_summary(content)
        fb = next(s for s in result if s["name"] == "Facebook Ads")
        # Longest of the two runs: 14:30->14:33:30 == 3m 30s (> the 2m 00s
        # first run), and NOT the ~510m whole-day span.
        assert fb["runtime"] == "3m 30s"
        # Record counts remain day totals (both runs accumulate).
        assert fb["records"] == 15

    def test_runtime_is_longest_run_not_last_run(self):
        """runtime is the LONGEST run's span, not the last run's.
        When the real-work run happens earlier and the last run is a trivial
        no-op, the earlier (longer) span wins."""
        content = (
            "2026-07-17 06:00:00 [INFO] [import-run-begin]  run start\n"
            "2026-07-17 06:00:00 [INFO] [ig-run-daily-imports]  Starting\n"
            "2026-07-17 06:06:00 [INFO] [ig-json-to-elastic]  Completed sending 20 Instagram document(s) to es\n"
            # A later run only re-checks and skips (a 0s single-line span).
            "2026-07-17 14:30:00 [INFO] [import-run-begin]  run start\n"
            "2026-07-17 14:30:00 [INFO] [ig-get-insights]  Profile X already imported and indexed. Skipping.\n"
        )
        result = LogService.get_import_summary(content)
        ig = next(s for s in result if s["name"] == "Instagram")
        # Longest run (06:00->06:06 == 6m 00s) wins over the last run's 0s.
        assert ig["runtime"] == "6m 00s"

    def test_runtime_reported_when_source_absent_from_last_run(self):
        """a source that did its work in an earlier run (and not the
        last run) still reports that earlier run's span, rather than None."""
        content = (
            "2026-07-16 06:00:00 [INFO] [import-run-begin]  run start\n"
            "2026-07-16 06:00:00 [INFO] [ig-run-daily-imports]  Starting\n"
            "2026-07-16 06:05:00 [INFO] [ig-json-to-elastic]  Completed sending 3 Instagram document(s) to es\n"
            "2026-07-16 14:30:00 [INFO] [import-run-begin]  run start\n"
            "2026-07-16 14:30:00 [INFO] [fb-run-daily-imports]  Starting\n"
            "2026-07-16 14:31:00 [INFO] [fb-json-to-elastic]  Completed sending 5 documents to es\n"
        )
        result = LogService.get_import_summary(content)
        ig = next(s for s in result if s["name"] == "Instagram")
        assert ig["ran"] is True
        assert ig["runtime"] == "5m 00s"  # its earlier run's span, not None
        assert ig["records"] == 3  # day total preserved


# ---------------------------------------------------------------------------
# get_import_summary_for_date — today-is-never-cached
# ---------------------------------------------------------------------------


class TestGetImportSummaryForDate:
    """The snapshot cache is write-once, so TODAY (still being appended to by
    later imports / same-day backfills) must never be served or written from it;
    past dates are immutable and stay cached."""

    _RAN_LOG = "[fb-run-daily-imports] Starting\n[fb-json-to-elastic] Completed sending 5 documents to es\n"

    @mock.patch.object(LogService, "_save_summary_snapshot")
    @mock.patch.object(LogService, "read_log_file")
    @mock.patch.object(LogService, "_load_summary_snapshot")
    @mock.patch("asset_management.web.services.log_service.datetime")
    def test_should_bypass_cache_and_reparse_for_today(self, mock_dt, mock_load, mock_read, mock_save):
        """A stale snapshot for today is ignored; the live log is re-parsed."""
        mock_dt.now.return_value.strftime.return_value = "2026-07-16"
        mock_load.return_value = [{"name": "Facebook Ads", "records": 0, "ran": True}]  # stale
        mock_read.return_value = self._RAN_LOG

        result = LogService.get_import_summary_for_date("2026-07-16", "imports_20260716.log")

        mock_load.assert_not_called()  # today's cache is never read
        fb = next(s for s in result if s["name"] == "Facebook Ads")
        assert fb["records"] == 5  # re-parsed from the live log, not the stale 0

    @mock.patch.object(LogService, "_save_summary_snapshot")
    @mock.patch.object(LogService, "read_log_file")
    @mock.patch("asset_management.web.services.log_service.datetime")
    def test_should_not_write_snapshot_for_today(self, mock_dt, mock_read, mock_save):
        """Today is never snapshotted (it's still growing)."""
        mock_dt.now.return_value.strftime.return_value = "2026-07-16"
        mock_read.return_value = self._RAN_LOG

        LogService.get_import_summary_for_date("2026-07-16", "imports_20260716.log")

        mock_save.assert_not_called()

    @mock.patch.object(LogService, "_load_summary_snapshot")
    @mock.patch("asset_management.web.services.log_service.datetime")
    def test_should_serve_cached_snapshot_for_past_date(self, mock_dt, mock_load):
        """A past date's immutable snapshot is served from cache."""
        mock_dt.now.return_value.strftime.return_value = "2026-07-16"
        cached = [{"name": "Facebook Ads", "records": 42, "ran": True}]
        mock_load.return_value = cached

        result = LogService.get_import_summary_for_date("2026-07-10", "imports_20260710.log")

        assert result is cached

    @mock.patch.object(LogService, "_save_summary_snapshot")
    @mock.patch.object(LogService, "read_log_file")
    @mock.patch.object(LogService, "_load_summary_snapshot")
    @mock.patch("asset_management.web.services.log_service.datetime")
    def test_should_write_snapshot_for_past_date(self, mock_dt, mock_load, mock_read, mock_save):
        """A past date with data that isn't yet cached gets snapshotted."""
        mock_dt.now.return_value.strftime.return_value = "2026-07-16"
        mock_load.return_value = None  # not cached yet
        mock_read.return_value = self._RAN_LOG

        LogService.get_import_summary_for_date("2026-07-10", "imports_20260710.log")

        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# _summary_snapshot_path — Phase 4 path-injection containment
# ---------------------------------------------------------------------------


class TestSummarySnapshotPath:
    """``date_str`` flows from request args into a path; validate the format
    and containment guard that replaced the bare ``os.path.join``."""

    def test_valid_date_str_returns_path_under_summary_dir(self):
        path = LogService._summary_snapshot_path("2026-04-29")
        assert path == os.path.join(LogService.SUMMARY_DIR, "summary_2026-04-29.json")

    def test_invalid_format_raises(self):
        from lib.paths import UnsafePathError

        with pytest.raises(UnsafePathError):
            LogService._summary_snapshot_path("not-a-date")

    def test_path_traversal_attempt_raises(self):
        from lib.paths import UnsafePathError

        with pytest.raises(UnsafePathError):
            LogService._summary_snapshot_path("../etc/passwd")

    def test_load_returns_none_for_invalid_date(self):
        # Invalid format should be swallowed and treated as cache miss
        assert LogService._load_summary_snapshot("../etc/passwd") is None

    def test_save_swallows_unsafe_path_in_caller(self):
        # Caller should not crash when an unsafe date_str reaches the save
        # path — the snapshot write is best-effort and the parsed summary
        # must still be returned to the user.
        log_content = "[fb-json-to-elastic] Completed sending 1 documents to es\n"
        with mock.patch.object(LogService, "read_log_file", return_value=log_content):
            result = LogService.get_import_summary_for_date("../etc/passwd", "imports.log")
        assert any(row["ran"] for row in result)
