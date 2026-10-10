"""
Tests for the Import Service.

Covers:
  delete_manual_log         — deletes manual log file / no-op / error
  _build_import_command     — all sources, date range / days / invalid
  run_imports               — spawns subprocess, writes status
  get_import_status         — not found, completed, still running, transitions to completed
  cancel_import             — kills processes, marks cancelled
  cleanup_old_status_files  — removes old, keeps recent
  _is_process_running       — running, finished, not a child
"""

import json
import os
import signal
from datetime import datetime, timedelta
from unittest import mock

import pytest

from asset_management.web.services.import_service import ImportService


# ---------------------------------------------------------------------------
# MANUAL_LOG_FILE constant
# ---------------------------------------------------------------------------

class TestManualLogFile:

    def test_manual_log_file_is_set(self):
        assert ImportService.MANUAL_LOG_FILE.endswith("imports_manual.log")

    def test_manual_log_file_under_log_dir(self):
        assert ImportService.MANUAL_LOG_FILE.startswith(ImportService.LOG_DIR)


# ---------------------------------------------------------------------------
# delete_manual_log
# ---------------------------------------------------------------------------

class TestDeleteManualLog:

    @mock.patch("os.remove")
    @mock.patch("os.path.exists", return_value=True)
    def test_delete_success(self, mock_exists, mock_remove):
        result = ImportService.delete_manual_log()
        assert result["success"] is True
        assert result["deleted"] is True
        mock_remove.assert_called_once()

    @mock.patch("os.path.exists", return_value=False)
    def test_delete_no_file(self, mock_exists):
        result = ImportService.delete_manual_log()
        assert result["success"] is True
        assert result["deleted"] is False

    @mock.patch("os.remove", side_effect=OSError("permission denied"))
    @mock.patch("os.path.exists", return_value=True)
    def test_delete_error(self, mock_exists, mock_remove):
        result = ImportService.delete_manual_log()
        assert result["success"] is False
        # the OS error (and the log path) stays in the server log.
        assert result["message"] == "Failed to delete log."


# ---------------------------------------------------------------------------
# _build_import_command
# ---------------------------------------------------------------------------

class TestBuildImportCommand:

    def test_facebook_ads_with_dates(self):
        cmd = ImportService._build_import_command(
            "facebook_ads", start_date="2025-01-01", end_date="2025-01-07"
        )
        assert cmd is not None
        assert "facebook" in cmd
        assert "--ads_only" in cmd
        assert "--start_date=2025-01-01" in cmd
        assert "--end_date=2025-01-07" in cmd

    def test_facebook_ads_with_days(self):
        cmd = ImportService._build_import_command("facebook_ads", days=7)
        assert cmd is not None
        assert "--start_date=" in cmd[-2]
        assert "--end_date=" in cmd[-1]

    def test_facebook_ads_no_params_returns_none(self):
        cmd = ImportService._build_import_command("facebook_ads")
        assert cmd is None

    def test_echo_with_days(self):
        cmd = ImportService._build_import_command("echo", days=3)
        assert "echo" in cmd
        assert "--days_to_pull=3" in cmd

    def test_echo_with_dates(self):
        cmd = ImportService._build_import_command(
            "echo", start_date="2025-01-01", end_date="2025-01-03"
        )
        assert "--start_date=2025-01-01" in cmd

    def test_echo_forwards_team_ids_as_limit_teams(self):
        # the echo branch previously dropped the selected team, so a
        # single-team UI run was unscoped and iterated every tenant. Echo's
        # run_daily_imports reads --limit_teams (not --team_ids).
        cmd = ImportService._build_import_command(
            "echo", start_date="2025-01-01", end_date="2025-01-03", team_ids="esid123"
        )
        assert "--limit_teams=esid123" in cmd

    def test_echo_omits_limit_teams_when_absent(self):
        cmd = ImportService._build_import_command(
            "echo", start_date="2025-01-01", end_date="2025-01-03"
        )
        assert not any(c.startswith("--limit_teams") for c in cmd)

    def test_google_analytics_with_dates(self):
        cmd = ImportService._build_import_command(
            "google_analytics", start_date="2025-01-01", end_date="2025-01-07"
        )
        assert "--start_date=2025-01-01" in cmd
        assert "--end_date=2025-01-07" in cmd

    def test_google_analytics_with_days(self):
        cmd = ImportService._build_import_command("google_analytics", days=5)
        assert "--days_to_pull=5" in cmd

    def test_instagram_with_dates(self):
        cmd = ImportService._build_import_command(
            "instagram", start_date="2025-06-01", end_date="2025-06-10"
        )
        assert cmd is not None
        assert "instagram" in cmd
        assert "--start_date=2025-06-01" in cmd

    def test_youtube_with_days(self):
        cmd = ImportService._build_import_command("youtube", days=14)
        assert cmd is not None
        assert "youtube" in cmd

    def test_youtube_forwards_team_ids(self):
        # the youtube branch previously dropped --team_ids, so a
        # single-team UI run imported every team. It must now be forwarded.
        cmd = ImportService._build_import_command(
            "youtube", start_date="2025-01-01", end_date="2025-01-07",
            team_ids="VjhmT50BfzY0O-sz8QZX"
        )
        assert "--team_ids=VjhmT50BfzY0O-sz8QZX" in cmd

    def test_youtube_omits_team_ids_when_absent(self):
        # No team selected → no filter flag → all teams (cron behavior).
        cmd = ImportService._build_import_command(
            "youtube", start_date="2025-01-01", end_date="2025-01-07"
        )
        assert not any(c.startswith("--team_ids") for c in cmd)

    def test_google_ads_with_dates(self):
        cmd = ImportService._build_import_command(
            "google_ads", start_date="2025-03-01", end_date="2025-03-31"
        )
        assert "google_ads" in cmd
        assert "--start_date=2025-03-01" in cmd

    def test_chatwoot_with_dates(self):
        cmd = ImportService._build_import_command(
            "chatwoot", start_date="2025-01-01", end_date="2025-01-05"
        )
        assert "chatwoot" in cmd

    def test_facebook_pages_with_team_ids(self):
        cmd = ImportService._build_import_command(
            "facebook_pages", start_date="2025-01-01", end_date="2025-01-07",
            team_ids="team_a,team_b"
        )
        assert "--team_ids=team_a,team_b" in cmd

    def test_unknown_source(self):
        cmd = ImportService._build_import_command("unknown_source", days=7)
        assert cmd is None


# ---------------------------------------------------------------------------
# run_imports
# ---------------------------------------------------------------------------

class TestRunImports:

    @mock.patch("asset_management.web.services.import_service.ImportService._write_status")
    @mock.patch("subprocess.Popen")
    def test_run_imports_starts_processes(self, mock_popen, mock_write):
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        result = ImportService.run_imports([
            {"source": "echo", "days": 3},
        ])
        assert "import_id" in result
        assert "echo" in result["started"]
        mock_popen.assert_called_once()
        mock_write.assert_called_once()

    # each spawned item gets a stable per-batch key: on the
    # wrapper command (--item) and on its pid entry (with team_ids for the
    # capacity panel's labels). Two same-source items get distinct keys.
    @mock.patch("asset_management.web.services.import_service.ImportService._write_status")
    @mock.patch("subprocess.Popen")
    def test_run_imports_stamps_item_keys(self, mock_popen, mock_write):
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        ImportService.run_imports([
            {"source": "youtube", "days": 3, "team_ids": "Team-A"},
            {"source": "youtube", "days": 3, "team_ids": "Team-B"},
        ])

        cmds = [c.args[0] for c in mock_popen.call_args_list]
        items = [cmd[cmd.index("--item") + 1] for cmd in cmds]
        assert items == ["youtube:0", "youtube:1"]

        status_data = mock_write.call_args.args[1]
        pids = status_data["pids"]
        assert [(p["item"], p["team_ids"]) for p in pids] == [
            ("youtube:0", "Team-A"),
            ("youtube:1", "Team-B"),
        ]

    @mock.patch("asset_management.web.services.import_service.ImportService._write_status")
    @mock.patch("subprocess.Popen")
    def test_run_imports_skips_invalid_source(self, mock_popen, mock_write):
        result = ImportService.run_imports([
            {"source": "unknown_thing", "days": 3},
        ])
        assert result["started"] == []
        mock_popen.assert_not_called()

    # run_imports reaps stale status files on every launch (the
    # reaper's only invocation site — it was previously dead code).
    @mock.patch("asset_management.web.services.import_service.ImportService.cleanup_old_status_files")
    @mock.patch("asset_management.web.services.import_service.ImportService._write_status")
    @mock.patch("subprocess.Popen")
    def test_run_imports_invokes_status_file_reaper(self, mock_popen, mock_write, mock_cleanup):
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        ImportService.run_imports([{"source": "echo", "days": 3}])

        # Mutation: drop the cleanup_old_status_files() call from run_imports
        # -> this fails (the reaper goes back to never running).
        mock_cleanup.assert_called_once()

    @mock.patch(
        "asset_management.web.services.import_service.ImportService.cleanup_old_status_files",
        side_effect=OSError("disk gone"),
    )
    @mock.patch("asset_management.web.services.import_service.ImportService._write_status")
    @mock.patch("subprocess.Popen")
    def test_run_imports_reaper_failure_does_not_block_import(self, mock_popen, mock_write, mock_cleanup):
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc

        # Mutation: remove the try/except around the reaper call -> the OSError
        # propagates and this raises instead of starting the import.
        result = ImportService.run_imports([{"source": "echo", "days": 3}])
        assert "echo" in result["started"]
        mock_popen.assert_called_once()


# ---------------------------------------------------------------------------
# get_import_status
# ---------------------------------------------------------------------------

class TestGetImportStatus:

    @mock.patch.object(ImportService, "_read_status", return_value=None)
    def test_not_found(self, mock_read):
        result = ImportService.get_import_status("bad_id")
        assert result["found"] is False

    @mock.patch.object(ImportService, "_read_status")
    def test_already_completed(self, mock_read):
        mock_read.return_value = {
            "completed": True,
            "started_at": "2025-01-01T10:00:00",
            "completed_at": "2025-01-01T10:05:00",
            "results": [{"source": "echo", "success": True}],
        }
        result = ImportService.get_import_status("abc")
        assert result["found"] is True
        assert result["completed"] is True
        assert result["success"] is True
        assert "300 seconds" in result["duration"]

    @mock.patch.object(ImportService, "_write_status")
    @mock.patch.object(ImportService, "_is_process_running", return_value=False)
    @mock.patch.object(ImportService, "_read_status")
    def test_transitions_to_completed(self, mock_read, mock_running, mock_write):
        mock_read.return_value = {
            "completed": False,
            "started_at": datetime.now().isoformat(),
            "pids": [{"pid": 100, "source": "echo"}],
        }
        result = ImportService.get_import_status("abc")
        assert result["completed"] is True
        mock_write.assert_called_once()

    @mock.patch.object(ImportService, "_is_process_running", return_value=True)
    @mock.patch.object(ImportService, "_read_status")
    def test_still_running(self, mock_read, mock_running):
        mock_read.return_value = {
            "completed": False,
            "started_at": datetime.now().isoformat(),
            "pids": [{"pid": 100, "source": "echo"}],
        }
        result = ImportService.get_import_status("abc")
        assert result["completed"] is False
        assert "echo" in result["running"]

    # PID gone + wrapper recorded success → real success.
    @mock.patch.object(ImportService, "_write_status")
    @mock.patch.object(ImportService, "_is_process_running", return_value=False)
    @mock.patch.object(ImportService, "_read_status")
    def test_completion_record_success(self, mock_read, mock_running, mock_write):
        mock_read.return_value = {
            "completed": False,
            "started_at": datetime.now().isoformat(),
            "pids": [{"pid": 100, "source": "echo"}],
            "results": [
                {"source": "echo", "success": True, "exit_code": 0},
            ],
        }
        result = ImportService.get_import_status("abc")
        assert result["completed"] is True
        assert result["success"] is True

    # PID gone + wrapper recorded failure → reported as failure.
    @mock.patch.object(ImportService, "_write_status")
    @mock.patch.object(ImportService, "_is_process_running", return_value=False)
    @mock.patch.object(ImportService, "_read_status")
    def test_completion_record_failure(self, mock_read, mock_running, mock_write):
        mock_read.return_value = {
            "completed": False,
            "started_at": datetime.now().isoformat(),
            "pids": [{"pid": 100, "source": "echo"}],
            "results": [
                {"source": "echo", "success": False, "exit_code": 1},
            ],
        }
        result = ImportService.get_import_status("abc")
        assert result["completed"] is True
        assert result["success"] is False
        assert result["results"][0]["exit_code"] == 1

    # PID gone + NO completion record → killed mid-flight.
    # This is the case the bug report ("status said completed but I could
    # tell something happened") was hitting. The wrapper would have written
    # a record on clean exit; absence of one means the subprocess didn't
    # reach its finally block — most likely killed by a container restart,
    # OOM, or SIGKILL.
    @mock.patch.object(ImportService, "_write_status")
    @mock.patch.object(ImportService, "_is_process_running", return_value=False)
    @mock.patch.object(ImportService, "_read_status")
    def test_killed_without_completion_record_is_failure(self, mock_read, mock_running, mock_write):
        mock_read.return_value = {
            "completed": False,
            "started_at": datetime.now().isoformat(),
            "pids": [{"pid": 100, "source": "echo"}],
            "results": [],  # wrapper never wrote a record
        }
        result = ImportService.get_import_status("abc")
        assert result["completed"] is True
        assert result["success"] is False
        assert result["results"][0]["success"] is False
        assert "killed" in result["results"][0]["error"].lower()

    # mixed — one source succeeded, one was killed → overall failure.
    @mock.patch.object(ImportService, "_write_status")
    @mock.patch.object(ImportService, "_is_process_running", return_value=False)
    @mock.patch.object(ImportService, "_read_status")
    def test_partial_failure_is_failure(self, mock_read, mock_running, mock_write):
        mock_read.return_value = {
            "completed": False,
            "started_at": datetime.now().isoformat(),
            "pids": [
                {"pid": 100, "source": "echo"},
                {"pid": 200, "source": "facebook_ads"},
            ],
            "results": [
                {"source": "echo", "success": True, "exit_code": 0},
                # facebook_ads has no record → killed
            ],
        }
        result = ImportService.get_import_status("abc")
        assert result["completed"] is True
        assert result["success"] is False

    # item-keyed pids join their own records: two same-source
    # items report two separate outcomes (previously both pids resolved to
    # the single last-written source record).
    @mock.patch.object(ImportService, "_write_status")
    @mock.patch.object(ImportService, "_is_process_running", return_value=False)
    @mock.patch.object(ImportService, "_read_status")
    def test_same_source_items_keep_separate_records(self, mock_read, mock_running, mock_write):
        mock_read.return_value = {
            "completed": False,
            "started_at": datetime.now().isoformat(),
            "pids": [
                {"pid": 100, "source": "youtube", "item": "youtube:0"},
                {"pid": 200, "source": "youtube", "item": "youtube:1"},
            ],
            "results": [
                {"source": "youtube", "item": "youtube:0", "success": True, "exit_code": 0},
                {"source": "youtube", "item": "youtube:1", "success": False, "exit_code": 1},
            ],
        }
        result = ImportService.get_import_status("abc")
        assert result["completed"] is True
        assert result["success"] is False
        by_item = {r["item"]: r for r in result["results"]}
        assert len(result["results"]) == 2
        assert by_item["youtube:0"]["success"] is True
        assert by_item["youtube:1"]["success"] is False

    # an item-keyed pid must NOT inherit a same-source sibling's
    # record: a killed item stays a failure even when its sibling succeeded.
    @mock.patch.object(ImportService, "_write_status")
    @mock.patch.object(ImportService, "_is_process_running", return_value=False)
    @mock.patch.object(ImportService, "_read_status")
    def test_killed_item_does_not_borrow_sibling_record(self, mock_read, mock_running, mock_write):
        mock_read.return_value = {
            "completed": False,
            "started_at": datetime.now().isoformat(),
            "pids": [
                {"pid": 100, "source": "youtube", "item": "youtube:0"},
                {"pid": 200, "source": "youtube", "item": "youtube:1"},
            ],
            "results": [
                {"source": "youtube", "item": "youtube:0", "success": True, "exit_code": 0},
                # youtube:1 has no record → killed mid-flight
            ],
        }
        result = ImportService.get_import_status("abc")
        assert result["success"] is False
        by_item = {r["item"]: r for r in result["results"]}
        assert by_item["youtube:0"]["success"] is True
        assert by_item["youtube:1"]["success"] is False
        assert "killed" in by_item["youtube:1"]["error"].lower()


# ---------------------------------------------------------------------------
# cancel_import
# ---------------------------------------------------------------------------

class TestCancelImport:

    @mock.patch.object(ImportService, "_read_status", return_value=None)
    def test_cancel_not_found(self, mock_read):
        result = ImportService.cancel_import("bad_id")
        assert result["success"] is False

    @mock.patch.object(ImportService, "_read_status", return_value={"completed": True})
    def test_cancel_already_completed(self, mock_read):
        result = ImportService.cancel_import("abc")
        assert result["success"] is False
        assert "already completed" in result["error"]

    # os.getpgid / os.killpg are POSIX-only (absent on Windows dev machines);
    # create=True lets the patch stand in on any platform — the prod container
    # is Linux, where the real calls exist.
    @mock.patch.object(ImportService, "_write_status")
    @mock.patch("os.killpg", create=True)
    @mock.patch("os.getpgid", side_effect=lambda pid: pid, create=True)
    @mock.patch("os.kill")
    @mock.patch.object(ImportService, "_read_status")
    def test_cancel_kills_process_groups(self, mock_read, mock_kill, mock_getpgid, mock_killpg, mock_write):
        # cancel now signals the whole process GROUP, not just the
        # recorded leader PID, so orphaned descendants (run_daily_imports /
        # get_conversations) are killed too.
        mock_read.return_value = {
            "completed": False,
            "pids": [
                {"pid": 100, "source": "echo"},
                {"pid": 200, "source": "facebook_ads"},
            ],
        }
        result = ImportService.cancel_import("abc")
        assert result["success"] is True
        assert "echo" in result["cancelled"]
        assert "facebook_ads" in result["cancelled"]
        # One killpg per source on the resolved group; the single-PID kill is
        # only the fallback and must NOT be used when killpg succeeds.
        assert mock_killpg.call_count == 2
        assert mock_kill.call_count == 0

    # cancelled records carry the pid's item key so the wrapper's
    # SIGTERM-driven completion write matches (and preserves) the right
    # record instead of clobbering a same-source sibling's.
    @mock.patch.object(ImportService, "_write_status")
    @mock.patch("os.killpg", create=True)
    @mock.patch("os.getpgid", return_value=100, create=True)
    @mock.patch("os.kill")
    @mock.patch.object(ImportService, "_read_status")
    def test_cancel_stamps_item_on_records(self, mock_read, mock_kill, mock_getpgid, mock_killpg, mock_write):
        mock_read.return_value = {
            "completed": False,
            "pids": [
                {"pid": 100, "source": "youtube", "item": "youtube:0"},
                {"pid": 200, "source": "youtube", "item": "youtube:1"},
            ],
        }
        result = ImportService.cancel_import("abc")
        assert result["success"] is True

        status_data = mock_write.call_args.args[1]
        assert [(r["source"], r["item"], r["cancelled"]) for r in status_data["results"]] == [
            ("youtube", "youtube:0", True),
            ("youtube", "youtube:1", True),
        ]

    @mock.patch.object(ImportService, "_write_status")
    @mock.patch("os.killpg", create=True)
    @mock.patch("os.getpgid", side_effect=OSError("no such process"), create=True)
    @mock.patch("os.kill", side_effect=OSError("no such process"))
    @mock.patch.object(ImportService, "_read_status")
    def test_cancel_handles_already_dead(self, mock_read, mock_kill, mock_getpgid, mock_killpg, mock_write):
        # Group can't be resolved (leader gone) AND the single-PID fallback also
        # fails — cancel soft-handles it, nothing recorded as cancelled.
        mock_read.return_value = {
            "completed": False,
            "pids": [{"pid": 100, "source": "echo"}],
        }
        result = ImportService.cancel_import("abc")
        assert result["success"] is True
        assert result["cancelled"] == []


# ---------------------------------------------------------------------------
# cleanup_old_status_files
# ---------------------------------------------------------------------------

class TestCleanupOldStatusFiles:

    @mock.patch("os.path.exists", return_value=False)
    def test_no_dir_noop(self, mock_exists):
        ImportService.cleanup_old_status_files()

    @mock.patch("os.remove")
    @mock.patch("os.path.getmtime")
    @mock.patch("os.listdir", return_value=["old.json", "new.json", "readme.txt"])
    @mock.patch("os.path.exists", return_value=True)
    def test_removes_old_keeps_new(self, mock_exists, mock_listdir, mock_mtime, mock_remove):
        old_time = (datetime.now() - timedelta(hours=48)).timestamp()
        new_time = datetime.now().timestamp()
        mock_mtime.side_effect = [old_time, new_time]

        ImportService.cleanup_old_status_files(max_age_hours=24)
        mock_remove.assert_called_once()
        # Verify it removed the old file
        removed_path = mock_remove.call_args[0][0]
        assert "old.json" in removed_path


# ---------------------------------------------------------------------------
# _is_process_running
# ---------------------------------------------------------------------------

class TestIsProcessRunning:

    @mock.patch("os.waitpid", return_value=(0, 0))
    def test_running(self, mock_waitpid):
        # os.WNOHANG may not exist on Windows; ensure it's available for the call
        with mock.patch.object(os, "WNOHANG", 1, create=True):
            assert ImportService._is_process_running(123) is True

    @mock.patch("os.waitpid", return_value=(123, 0))
    def test_finished(self, mock_waitpid):
        with mock.patch.object(os, "WNOHANG", 1, create=True):
            assert ImportService._is_process_running(123) is False

    # when the poll lands on a gunicorn worker that didn't spawn the
    # import (ChildProcessError), the fallback reads /proc/<pid>/stat STATE —
    # the old os.kill(pid, 0) probe counted zombies as alive, wedging
    # completion from every non-spawning worker (observed live: 4 finished
    # imports, 8 consecutive polls stuck at completed=false).

    @mock.patch("builtins.open", mock.mock_open(read_data="123 (python3) S 1 123 123 0 -1 4194560 500 0 0 0 7 3 0 0"))
    @mock.patch("os.waitpid", side_effect=ChildProcessError)
    def test_not_child_but_alive(self, mock_waitpid):
        with mock.patch.object(os, "WNOHANG", 1, create=True):
            assert ImportService._is_process_running(123) is True

    @mock.patch("builtins.open", mock.mock_open(read_data="123 (python3) Z 1 123 123 0 -1 4194560 500 0 0 0 7 3 0 0"))
    @mock.patch("os.waitpid", side_effect=ChildProcessError)
    def test_not_child_zombie_is_finished(self, mock_waitpid):
        with mock.patch.object(os, "WNOHANG", 1, create=True):
            assert ImportService._is_process_running(123) is False

    @mock.patch("builtins.open", side_effect=OSError)
    @mock.patch("os.waitpid", side_effect=ChildProcessError)
    def test_not_child_not_exists(self, mock_waitpid, mock_open_):
        with mock.patch.object(os, "WNOHANG", 1, create=True):
            assert ImportService._is_process_running(123) is False


# ---------------------------------------------------------------------------
# _get_status_file — Phase 4 path-injection containment
# ---------------------------------------------------------------------------


class TestGetStatusFileValidation:
    """``import_id`` flows from request URL into a path; validate the format
    gate that replaced the bare ``os.path.join``."""

    def test_valid_hex_id(self):
        path = ImportService._get_status_file("a1b2c3d4")
        assert path == os.path.join(ImportService.STATUS_DIR, "a1b2c3d4.json")

    def test_path_traversal_raises(self):
        from lib.paths import UnsafePathError

        with pytest.raises(UnsafePathError):
            ImportService._get_status_file("../etc/passwd")

    def test_non_hex_chars_raise(self):
        from lib.paths import UnsafePathError

        with pytest.raises(UnsafePathError):
            ImportService._get_status_file("a1b2/c3d4")

    def test_empty_id_raises(self):
        from lib.paths import UnsafePathError

        with pytest.raises(UnsafePathError):
            ImportService._get_status_file("")

    def test_read_status_returns_none_for_invalid_id(self):
        # Invalid id should be swallowed and treated as "not found".
        assert ImportService._read_status("../etc/passwd") is None
