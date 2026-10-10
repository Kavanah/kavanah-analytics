"""
Tests for the ConfigRestoreService.

Covers:
  list_extractions       — ignores hidden dirs, sorts newest first
  list_extracted_files   — flags live_exists / differs correctly
  apply_files            — writes to live dir, saves rollback, validates paths,
                           rejects path-traversal, skips missing files
"""

import os
from unittest import mock

import pytest

from asset_management.web.services import config_restore_service as crs


@pytest.fixture
def fake_layout(tmp_path, monkeypatch):
    """Set up a sandbox: extractions dir, live config dir, rollback dir."""
    extracted_root = tmp_path / "s3-config-restore"
    live_dir = tmp_path / "reporting_configs"
    rollback_root = extracted_root / ".applied-backup"
    extracted_root.mkdir()
    live_dir.mkdir()

    monkeypatch.setattr(crs, "EXTRACTED_ROOT", str(extracted_root))
    monkeypatch.setattr(crs, "APPLIED_BACKUP_ROOT", str(rollback_root))
    monkeypatch.setattr(crs, "LIVE_CONFIG_DIR", str(live_dir))

    return {
        "extracted_root": extracted_root,
        "live_dir": live_dir,
        "rollback_root": rollback_root,
    }


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# list_extractions
# ---------------------------------------------------------------------------

class TestListExtractions:

    def test_should_return_extraction_dirs_excluding_hidden(self, fake_layout):
        root = fake_layout["extracted_root"]
        (root / "elk-config-20260415").mkdir()
        _write(root / "elk-config-20260415" / "master-config.yaml", "x: 1")
        (root / ".applied-backup").mkdir()
        _write(root / ".applied-backup" / "skip.txt", "ignored")

        result = crs.ConfigRestoreService.list_extractions()
        names = [e["name"] for e in result]
        assert "elk-config-20260415" in names
        assert ".applied-backup" not in names

    def test_should_count_files_recursively(self, fake_layout):
        root = fake_layout["extracted_root"]
        _write(root / "elk-config-A" / "a.yaml", "1")
        _write(root / "elk-config-A" / "subdir" / "b.json", "2")
        _write(root / "elk-config-A" / "subdir" / "c.json", "3")

        result = crs.ConfigRestoreService.list_extractions()
        assert result[0]["file_count"] == 3

    def test_should_return_empty_when_root_missing(self, fake_layout, monkeypatch):
        monkeypatch.setattr(crs, "EXTRACTED_ROOT", str(fake_layout["extracted_root"] / "nope"))
        assert crs.ConfigRestoreService.list_extractions() == []


# ---------------------------------------------------------------------------
# list_extracted_files
# ---------------------------------------------------------------------------

class TestListExtractedFiles:

    def test_should_flag_live_exists_and_differs(self, fake_layout):
        root = fake_layout["extracted_root"]
        live = fake_layout["live_dir"]
        ext = root / "elk-config-1"

        _write(ext / "master-config.yaml", "version: 2")
        _write(ext / "fresh.json", "{}")
        # Live has a different version of master-config.yaml.
        _write(live / "master-config.yaml", "version: 1")

        files = crs.ConfigRestoreService.list_extracted_files("elk-config-1")
        by_path = {f["path"]: f for f in files}
        assert by_path["master-config.yaml"]["live_exists"] is True
        assert by_path["master-config.yaml"]["differs"] is True
        assert by_path["fresh.json"]["live_exists"] is False
        assert by_path["fresh.json"]["differs"] is False

    def test_should_mark_identical_when_bytes_match(self, fake_layout):
        root = fake_layout["extracted_root"]
        live = fake_layout["live_dir"]
        _write(root / "elk-config-2" / "same.yaml", "same content")
        _write(live / "same.yaml", "same content")

        files = crs.ConfigRestoreService.list_extracted_files("elk-config-2")
        assert files[0]["live_exists"] is True
        assert files[0]["differs"] is False

    def test_should_reject_path_traversal_in_extraction_name(self, fake_layout):
        with pytest.raises(ValueError):
            crs.ConfigRestoreService.list_extracted_files("../etc")

    def test_should_raise_when_extraction_missing(self, fake_layout):
        with pytest.raises(FileNotFoundError):
            crs.ConfigRestoreService.list_extracted_files("does-not-exist")


# ---------------------------------------------------------------------------
# apply_files
# ---------------------------------------------------------------------------

class TestApplyFiles:

    def test_should_copy_selected_file_to_live_dir(self, fake_layout):
        root = fake_layout["extracted_root"]
        live = fake_layout["live_dir"]
        _write(root / "elk-1" / "master-config.yaml", "new content")

        result = crs.ConfigRestoreService.apply_files("elk-1", files=["master-config.yaml"])

        assert (live / "master-config.yaml").read_text() == "new content"
        assert result["applied"] == ["master-config.yaml"]
        assert result["skipped_missing"] == []

    def test_should_save_previous_version_to_rollback_dir_when_overwriting(self, fake_layout):
        root = fake_layout["extracted_root"]
        live = fake_layout["live_dir"]
        _write(root / "elk-2" / "master-config.yaml", "from backup")
        _write(live / "master-config.yaml", "previous live")

        result = crs.ConfigRestoreService.apply_files("elk-2", files=["master-config.yaml"])

        rollback_path = os.path.join(result["rollback_dir"], "master-config.yaml")
        assert os.path.isfile(rollback_path)
        with open(rollback_path) as f:
            assert f.read() == "previous live"

    def test_should_apply_all_files_when_files_is_none(self, fake_layout):
        root = fake_layout["extracted_root"]
        live = fake_layout["live_dir"]
        _write(root / "elk-3" / "a.yaml", "A")
        _write(root / "elk-3" / "sub" / "b.json", "B")

        result = crs.ConfigRestoreService.apply_files("elk-3", files=None)

        assert (live / "a.yaml").read_text() == "A"
        assert (live / "sub" / "b.json").read_text() == "B"
        assert sorted(result["applied"]) == ["a.yaml", "sub/b.json"]

    def test_should_skip_missing_files(self, fake_layout):
        root = fake_layout["extracted_root"]
        _write(root / "elk-4" / "exists.yaml", "x")

        result = crs.ConfigRestoreService.apply_files(
            "elk-4", files=["exists.yaml", "missing.json"]
        )
        assert result["applied"] == ["exists.yaml"]
        assert result["skipped_missing"] == ["missing.json"]

    def test_should_reject_path_traversal_in_files(self, fake_layout):
        root = fake_layout["extracted_root"]
        _write(root / "elk-5" / "ok.yaml", "x")
        with pytest.raises(ValueError):
            crs.ConfigRestoreService.apply_files("elk-5", files=["../escape.yaml"])

    def test_should_reject_absolute_paths_in_files(self, fake_layout):
        root = fake_layout["extracted_root"]
        _write(root / "elk-6" / "ok.yaml", "x")
        with pytest.raises(ValueError):
            crs.ConfigRestoreService.apply_files("elk-6", files=["/etc/passwd"])

    def test_should_reject_empty_file_list(self, fake_layout):
        root = fake_layout["extracted_root"]
        (root / "elk-empty").mkdir()
        with pytest.raises(ValueError, match="No files"):
            crs.ConfigRestoreService.apply_files("elk-empty", files=[])
