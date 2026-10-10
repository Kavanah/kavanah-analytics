"""Unit tests for lib.import_runner — the wrapper that records
the real subprocess exit status into the import status file.

The wrapper is a thin layer; tests focus on the contract:
- Successful command → results contains success=True, exit_code=0
- Failing command → results contains success=False, exit_code=N
- Cancelled record (already present from cancel_import) → not overwritten
- Multiple sources writing concurrently → both records preserved
- Status file missing → no-op (don't recreate)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Path massage so we can import lib.import_runner without requiring the
# whole asset_management Flask app on the path.
PYTHON_DIR = Path(__file__).resolve().parents[2] / "bin" / "python"
sys.path.insert(0, str(PYTHON_DIR))

from lib import import_runner  # noqa: E402

pytestmark = []


@pytest.fixture
def status_file(tmp_path, monkeypatch):
    """Point the runner at a temp status file pre-populated like ImportService would."""
    status_dir = tmp_path / "import_status"
    status_dir.mkdir()
    monkeypatch.setattr(import_runner, "STATUS_DIR", str(status_dir))

    import_id = "abcdef12"
    path = status_dir / f"{import_id}.json"
    path.write_text(
        json.dumps(
            {
                "import_id": import_id,
                "started_at": "2026-05-04T12:00:00",
                "sources": ["echo"],
                "pids": [{"source": "echo", "pid": 9999}],
                "completed": False,
                "results": [],
            }
        )
    )
    return import_id, path


class TestRecordCompletion:
    def test_records_success(self, status_file):
        import_id, path = status_file
        import_runner._record_completion(import_id, "echo", exit_code=0)

        data = json.loads(path.read_text())
        assert len(data["results"]) == 1
        record = data["results"][0]
        assert record["source"] == "echo"
        assert record["success"] is True
        assert record["exit_code"] == 0
        assert "completed_at" in record

    def test_records_failure(self, status_file):
        import_id, path = status_file
        import_runner._record_completion(import_id, "echo", exit_code=1)

        data = json.loads(path.read_text())
        record = data["results"][0]
        assert record["success"] is False
        assert record["exit_code"] == 1

    def test_records_error_message(self, status_file):
        import_id, path = status_file
        import_runner._record_completion(
            import_id, "echo", exit_code=None, error="OSError: signal 9"
        )

        data = json.loads(path.read_text())
        record = data["results"][0]
        assert record["success"] is False
        assert record["error"] == "OSError: signal 9"

    def test_does_not_overwrite_cancelled_record(self, status_file):
        """If cancel_import wrote a cancelled record, the wrapper's
        SIGTERM-driven exit must not clobber it."""
        import_id, path = status_file
        # Pre-populate as if cancel_import ran first
        data = json.loads(path.read_text())
        data["results"] = [{"source": "echo", "success": False, "cancelled": True}]
        path.write_text(json.dumps(data))

        import_runner._record_completion(import_id, "echo", exit_code=143)

        data = json.loads(path.read_text())
        # cancelled record preserved
        assert len(data["results"]) == 1
        assert data["results"][0]["cancelled"] is True

    def test_two_sources_both_recorded(self, status_file):
        """Concurrent sources finishing in the same import — both records
        must end up in the file (no clobber)."""
        import_id, path = status_file
        # Add a second source to the file's pids so the test setup is
        # representative; recording is independent though.
        import_runner._record_completion(import_id, "echo", exit_code=0)
        import_runner._record_completion(import_id, "facebook_ads", exit_code=2)

        data = json.loads(path.read_text())
        sources = sorted(r["source"] for r in data["results"])
        assert sources == ["echo", "facebook_ads"]

    def test_replaces_prior_record_for_same_source(self, status_file):
        """Defensive: if the wrapper somehow runs twice for the same
        source, last write wins rather than ending up with duplicates.
        (Legacy no-item path — earlier launches.)"""
        import_id, path = status_file
        import_runner._record_completion(import_id, "echo", exit_code=0)
        import_runner._record_completion(import_id, "echo", exit_code=1)

        data = json.loads(path.read_text())
        echo_records = [r for r in data["results"] if r["source"] == "echo"]
        assert len(echo_records) == 1
        assert echo_records[0]["exit_code"] == 1

    # item-keyed records: two same-source batch items must keep
    # two separate completion records (previously the second one vanished —
    # records collapsed by source).
    def test_two_same_source_items_both_recorded(self, status_file):
        import_id, path = status_file
        import_runner._record_completion(import_id, "youtube", exit_code=0, item="youtube:0")
        import_runner._record_completion(import_id, "youtube", exit_code=1, item="youtube:1")

        data = json.loads(path.read_text())
        by_item = {r["item"]: r for r in data["results"]}
        assert set(by_item) == {"youtube:0", "youtube:1"}
        assert by_item["youtube:0"]["exit_code"] == 0
        assert by_item["youtube:1"]["exit_code"] == 1

    def test_item_keyed_replacement_leaves_siblings_alone(self, status_file):
        """A double-write for one item replaces only that item's record,
        never a same-source sibling's."""
        import_id, path = status_file
        import_runner._record_completion(import_id, "youtube", exit_code=0, item="youtube:0")
        import_runner._record_completion(import_id, "youtube", exit_code=0, item="youtube:1")
        import_runner._record_completion(import_id, "youtube", exit_code=7, item="youtube:1")

        data = json.loads(path.read_text())
        by_item = {r["item"]: r for r in data["results"]}
        assert len(data["results"]) == 2
        assert by_item["youtube:0"]["exit_code"] == 0
        assert by_item["youtube:1"]["exit_code"] == 7

    def test_item_keyed_cancelled_record_not_overwritten(self, status_file):
        """cancel_import stamps the item onto its cancelled record; the
        wrapper's SIGTERM-driven exit must match by item and leave it."""
        import_id, path = status_file
        data = json.loads(path.read_text())
        data["results"] = [{"source": "echo", "item": "echo:0", "success": False, "cancelled": True}]
        path.write_text(json.dumps(data))

        import_runner._record_completion(import_id, "echo", exit_code=143, item="echo:0")

        data = json.loads(path.read_text())
        assert len(data["results"]) == 1
        assert data["results"][0]["cancelled"] is True

    def test_missing_status_file_is_noop(self, tmp_path, monkeypatch):
        """If the status file was cleaned up underneath us, don't try to
        recreate it — we don't have the original started_at / pids."""
        status_dir = tmp_path / "import_status"
        status_dir.mkdir()
        monkeypatch.setattr(import_runner, "STATUS_DIR", str(status_dir))

        # No status file written. Should not raise, should not create one.
        import_runner._record_completion("deadbeef", "echo", exit_code=0)
        assert not (status_dir / "deadbeef.json").exists()

    def test_invalid_import_id_raises(self):
        with pytest.raises(ValueError):
            import_runner._record_completion("../etc/passwd", "echo", exit_code=0)
        with pytest.raises(ValueError):
            import_runner._record_completion("not-hex!", "echo", exit_code=0)


class TestMain:
    def test_propagates_zero_exit(self, status_file, monkeypatch):
        import_id, path = status_file

        # Stub Popen so we don't actually exec anything.
        fake_proc = mock.MagicMock()
        fake_proc.wait.return_value = 0
        with mock.patch("subprocess.Popen", return_value=fake_proc):
            rc = import_runner.main(
                ["--import_id", import_id, "--source", "echo", "--", "true"]
            )

        assert rc == 0
        data = json.loads(path.read_text())
        assert data["results"][0]["exit_code"] == 0

    def test_item_flag_lands_in_record(self, status_file):
        """main forwards --item into the completion record."""
        import_id, path = status_file

        fake_proc = mock.MagicMock()
        fake_proc.wait.return_value = 0
        with mock.patch("subprocess.Popen", return_value=fake_proc):
            rc = import_runner.main(
                ["--import_id", import_id, "--source", "echo", "--item", "echo:3", "--", "true"]
            )

        assert rc == 0
        data = json.loads(path.read_text())
        assert data["results"][0]["item"] == "echo:3"

    def test_propagates_nonzero_exit(self, status_file):
        import_id, path = status_file

        fake_proc = mock.MagicMock()
        fake_proc.wait.return_value = 7
        with mock.patch("subprocess.Popen", return_value=fake_proc):
            rc = import_runner.main(
                ["--import_id", import_id, "--source", "echo", "--", "false"]
            )

        assert rc == 7
        data = json.loads(path.read_text())
        assert data["results"][0]["success"] is False
        assert data["results"][0]["exit_code"] == 7

    def test_records_completion_even_when_subprocess_raises(self, status_file):
        """try/finally must record something even if Popen itself blows up.
        This is the property that gives the poller its 'killed mid-flight'
        signal — if even this fails, the result will be missing and the
        poller will surface that case."""
        import_id, path = status_file

        with mock.patch(
            "subprocess.Popen", side_effect=OSError("disk full")
        ):
            rc = import_runner.main(
                ["--import_id", import_id, "--source", "echo", "--", "true"]
            )

        assert rc == 1
        data = json.loads(path.read_text())
        record = data["results"][0]
        assert record["success"] is False
        assert "OSError" in record["error"]
