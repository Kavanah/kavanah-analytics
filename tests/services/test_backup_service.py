"""
Tests for the Backup Service.

Covers:
  create_backup   — success, no indices, ES unavailable
  list_backups    — lists files, empty dir, no dir
  restore_backup  — success, invalid filename, file not found, ES unavailable
"""

import io
import json
import tarfile
from unittest import mock

import pytest

from asset_management.web.services.backup_service import BackupService, BACKUP_DIR, MAX_BACKUPS_KEPT


# ---------------------------------------------------------------------------
# create_backup
# ---------------------------------------------------------------------------

class TestCreateBackup:

    def _mock_es_for_backup(self, index_names, docs_per_index=1):
        mock_es = mock.MagicMock()
        cat_body = mock.MagicMock()
        cat_body.body = "\n".join(index_names)
        mock_es.cat.indices.return_value = cat_body

        for idx in index_names:
            mapping_body = mock.MagicMock()
            mapping_body.body = {idx: {"mappings": {"properties": {"title": {"type": "keyword"}}}}}
            mock_es.indices.get_mapping.return_value = mapping_body

        hits = [{"_id": f"doc_{i}", "_source": {"title": f"item_{i}"}} for i in range(docs_per_index)]
        mock_es.search.return_value = {"_scroll_id": "scroll1", "hits": {"hits": hits}}
        mock_es.scroll.return_value = {"_scroll_id": "scroll2", "hits": {"hits": []}}
        return mock_es

    def test_success(self):
        mock_es = self._mock_es_for_backup(["de_asset_teams"])
        with mock.patch("asset_management.web.services.backup_service.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.services.backup_service.os.makedirs"), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(BackupService, "_prune_old_backups", return_value=[]):
            result = BackupService.create_backup()
        assert result["filename"].startswith("asset-backup-")
        assert result["filename"].endswith(".tar.gz")
        assert result["pruned"] == []

    def test_filename_includes_hhmmss_to_avoid_same_day_clobber(self):
        mock_es = self._mock_es_for_backup(["de_asset_teams"])
        with mock.patch("asset_management.web.services.backup_service.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.services.backup_service.os.makedirs"), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(BackupService, "_prune_old_backups", return_value=[]):
            result = BackupService.create_backup()
        # Format: asset-backup-YYYYMMDD-HHMMSS.tar.gz (one '-' between date and time)
        stem = result["filename"].removeprefix("asset-backup-").removesuffix(".tar.gz")
        date_part, _, time_part = stem.partition("-")
        assert len(date_part) == 8 and date_part.isdigit()
        assert len(time_part) == 6 and time_part.isdigit()

    def test_no_matching_indices_raises(self):
        mock_es = mock.MagicMock()
        cat_body = mock.MagicMock()
        cat_body.body = ""
        mock_es.cat.indices.return_value = cat_body
        with mock.patch("asset_management.web.services.backup_service.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.services.backup_service.os.makedirs"):
            with pytest.raises(RuntimeError, match="No de_asset"):
                BackupService.create_backup()

    def test_es_unavailable_raises(self):
        with mock.patch("asset_management.web.services.backup_service.get_elasticsearch_client", return_value=None):
            with pytest.raises(RuntimeError):
                BackupService.create_backup()

    def test_multiple_indices(self):
        mock_es = self._mock_es_for_backup(["de_asset_teams", "de_asset_organization"])
        with mock.patch("asset_management.web.services.backup_service.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.services.backup_service.os.makedirs"), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch.object(BackupService, "_prune_old_backups", return_value=[]):
            result = BackupService.create_backup()
        assert result["filename"].endswith(".tar.gz")


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------

class TestListBackups:

    def test_returns_matching_files(self):
        files = ["asset-backup-20240101.tar.gz", "asset-backup-20240102.tar.gz", "other.txt"]
        with mock.patch("asset_management.web.services.backup_service.os.path.isdir", return_value=True), \
             mock.patch("asset_management.web.services.backup_service.os.listdir", return_value=files):
            result = BackupService.list_backups()
        assert len(result) == 2
        assert "other.txt" not in result

    def test_sorted_newest_first(self):
        files = ["asset-backup-20240101.tar.gz", "asset-backup-20240301.tar.gz", "asset-backup-20240201.tar.gz"]
        with mock.patch("asset_management.web.services.backup_service.os.path.isdir", return_value=True), \
             mock.patch("asset_management.web.services.backup_service.os.listdir", return_value=files):
            result = BackupService.list_backups()
        assert result[0] == "asset-backup-20240301.tar.gz"

    def test_empty_directory(self):
        with mock.patch("asset_management.web.services.backup_service.os.path.isdir", return_value=True), \
             mock.patch("asset_management.web.services.backup_service.os.listdir", return_value=[]):
            result = BackupService.list_backups()
        assert result == []

    def test_no_directory(self):
        with mock.patch("asset_management.web.services.backup_service.os.path.isdir", return_value=False):
            result = BackupService.list_backups()
        assert result == []


# ---------------------------------------------------------------------------
# _prune_old_backups
# ---------------------------------------------------------------------------

class TestPruneOldBackups:

    def test_should_keep_only_n_most_recent(self):
        # 7 backups; keep 5, prune 2.
        files = [f"asset-backup-2026010{i}-120000.tar.gz" for i in range(1, 8)]
        with mock.patch.object(BackupService, "list_backups", return_value=sorted(files, reverse=True)), \
             mock.patch("asset_management.web.services.backup_service.os.remove") as rm:
            deleted = BackupService._prune_old_backups()
        assert len(deleted) == 7 - MAX_BACKUPS_KEPT
        assert rm.call_count == 7 - MAX_BACKUPS_KEPT

    def test_should_no_op_when_under_threshold(self):
        files = [f"asset-backup-2026010{i}-120000.tar.gz" for i in range(1, 4)]
        with mock.patch.object(BackupService, "list_backups", return_value=sorted(files, reverse=True)), \
             mock.patch("asset_management.web.services.backup_service.os.remove") as rm:
            deleted = BackupService._prune_old_backups()
        assert deleted == []
        rm.assert_not_called()

    def test_should_skip_files_that_fail_to_delete(self):
        files = [f"asset-backup-2026010{i}-120000.tar.gz" for i in range(1, 8)]
        with mock.patch.object(BackupService, "list_backups", return_value=sorted(files, reverse=True)), \
             mock.patch("asset_management.web.services.backup_service.os.remove",
                        side_effect=OSError("permission denied")):
            deleted = BackupService._prune_old_backups()
        # All deletes failed but no exception bubbles up.
        assert deleted == []


# ---------------------------------------------------------------------------
# restore_backup
# ---------------------------------------------------------------------------

class TestRestoreBackup:

    def _make_tar_bytes(self, indices_data):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for idx_name, data in indices_data.items():
                json_bytes = json.dumps(data).encode("utf-8")
                info = tarfile.TarInfo(name=f"{idx_name}.json")
                info.size = len(json_bytes)
                tar.addfile(info, io.BytesIO(json_bytes))
        buf.seek(0)
        return buf

    def test_success(self):
        data = {"de_asset_teams": {
            "mappings": {"properties": {"title": {"type": "keyword"}}},
            "documents": [{"_id": "1", "_source": {"title": "Alpha"}}],
        }}
        tar_buf = self._make_tar_bytes(data)
        mock_es = mock.MagicMock()

        with mock.patch("asset_management.web.services.backup_service.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.services.backup_service.os.path.isfile", return_value=True), \
             mock.patch("asset_management.web.services.backup_service.tarfile.open", return_value=tarfile.open(fileobj=tar_buf, mode="r:gz")), \
             mock.patch("asset_management.web.services.backup_service.helpers.bulk"):
            result = BackupService.restore_backup("asset-backup-20240101.tar.gz")

        assert "de_asset_teams" in result["restored"]
        mock_es.indices.delete.assert_called()
        mock_es.indices.create.assert_called()

    def test_invalid_filename_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid backup filename"):
            BackupService.restore_backup("../../../etc/backup.tar.gz")

    def test_invalid_filename_wrong_pattern(self):
        with pytest.raises(ValueError, match="Invalid backup filename"):
            BackupService.restore_backup("random-file.tar.gz")

    def test_file_not_found(self):
        with mock.patch("asset_management.web.services.backup_service.os.path.isfile", return_value=False):
            with pytest.raises(FileNotFoundError):
                BackupService.restore_backup("asset-backup-20240101.tar.gz")

    def test_es_unavailable(self):
        with mock.patch("asset_management.web.services.backup_service.os.path.isfile", return_value=True), \
             mock.patch("asset_management.web.services.backup_service.get_elasticsearch_client", return_value=None):
            with pytest.raises(RuntimeError):
                BackupService.restore_backup("asset-backup-20240101.tar.gz")
