"""
Tests for the SnapshotService.

Covers:
  list_repos        — happy path, ES unavailable
  list_snapshots    — happy path, invalid repo name, ES unavailable
  inspect_snapshot  — happy path with conflict markers, missing snapshot
  restore_indices   — happy path (replace + new), conflict without
                      replace_existing, missing snapshot, missing index,
                      invalid input
"""

from unittest import mock

import pytest

from asset_management.web.services.snapshot_service import SnapshotService, SnapshotServiceError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wrap(body):
    """Return a mock response object whose .body is the given dict (mirrors elasticsearch-py)."""
    r = mock.MagicMock()
    r.body = body
    return r


# ---------------------------------------------------------------------------
# list_repos
# ---------------------------------------------------------------------------

class TestListRepos:

    def test_should_return_repos_sorted_by_name_when_multiple_registered(self):
        es = mock.MagicMock()
        es.snapshot.get_repository.return_value = _wrap({
            "migration_repo": {"type": "fs", "settings": {"location": "/m"}},
            "daily_repo": {"type": "fs", "settings": {"location": "/d"}},
        })
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            repos = SnapshotService.list_repos()
        assert [r["name"] for r in repos] == ["daily_repo", "migration_repo"]
        assert repos[0]["type"] == "fs"

    def test_should_raise_when_es_unavailable(self):
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=None):
            with pytest.raises(SnapshotServiceError):
                SnapshotService.list_repos()


# ---------------------------------------------------------------------------
# list_snapshots
# ---------------------------------------------------------------------------

class TestListSnapshots:

    def test_should_return_snapshots_newest_first(self):
        es = mock.MagicMock()
        es.snapshot.get.return_value = _wrap({
            "snapshots": [
                {"snapshot": "old", "state": "SUCCESS", "start_time": "2026-04-01T00:00:00Z",
                 "end_time": "2026-04-01T00:01:00Z", "indices": ["a", "b"]},
                {"snapshot": "new", "state": "SUCCESS", "start_time": "2026-04-15T00:00:00Z",
                 "end_time": "2026-04-15T00:01:00Z", "indices": ["a"]},
            ]
        })
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            result = SnapshotService.list_snapshots("daily_repo")
        assert result["snapshots"][0]["snapshot"] == "new"
        assert result["snapshots"][1]["indices_count"] == 2
        assert result["warnings"] == []

    def test_should_skip_broken_snapshots_and_record_warning(self):
        """When the bulk fetch fails, the service falls back to per-snapshot
        loads and skips ones that error out (e.g. missing snap-*.dat files)."""
        from elasticsearch import NotFoundError

        es = mock.MagicMock()
        # Bulk call fails — simulates the "snapshot_missing_exception" we see
        # when a snapshot in the repo's index has no data file on disk.
        es.snapshot.get.side_effect = [
            NotFoundError("missing", meta=mock.MagicMock(), body={}),  # bulk
            _wrap({"snapshots": [{"snapshot": "good"}, {"snapshot": "broken"}]}),  # fallback name list
            _wrap({"snapshots": [{"snapshot": "good", "state": "SUCCESS",
                                  "start_time": "2026-04-15T00:00:00Z", "end_time": "",
                                  "indices": ["a"]}]}),  # detail for "good"
            NotFoundError("missing", meta=mock.MagicMock(), body={}),  # detail for "broken"
        ]
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            result = SnapshotService.list_snapshots("migration_repo")
        assert [s["snapshot"] for s in result["snapshots"]] == ["good"]
        assert any("broken" in w for w in result["warnings"])

    def test_should_reject_invalid_repo_name(self):
        with pytest.raises(ValueError, match="Invalid repository name"):
            SnapshotService.list_snapshots("../etc/passwd")

    def test_should_reject_empty_repo_name(self):
        with pytest.raises(ValueError):
            SnapshotService.list_snapshots("")


# ---------------------------------------------------------------------------
# inspect_snapshot
# ---------------------------------------------------------------------------

class TestInspectSnapshot:

    def test_should_mark_existing_indices_when_present_in_cluster(self):
        es = mock.MagicMock()
        es.snapshot.get.return_value = _wrap({
            "snapshots": [{
                "snapshot": "snap1",
                "state": "SUCCESS",
                "start_time": "2026-04-01T00:00:00Z",
                "end_time": "2026-04-01T00:01:00Z",
                "indices": ["de_asset_teams", "de_asset_organization", "fresh_index"],
            }]
        })
        # Cluster currently has de_asset_teams and de_asset_organization but not fresh_index.
        es.cat.indices.return_value = _wrap("de_asset_teams\nde_asset_organization\n")

        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            info = SnapshotService.inspect_snapshot("daily_repo", "snap1")

        by_name = {i["name"]: i for i in info["indices"]}
        assert by_name["de_asset_teams"]["exists_in_cluster"] is True
        assert by_name["de_asset_organization"]["exists_in_cluster"] is True
        assert by_name["fresh_index"]["exists_in_cluster"] is False

    def test_should_not_filter_cat_call_by_candidate_list(self):
        """Regression: previously _existing_indices passed the snapshot's
        index list as a filter to cat.indices. With 100+ indices that hit
        URL-length caps and silently 404'd — making every existing index
        appear "new". Verify the cat call is unfiltered and intersection
        happens locally."""
        es = mock.MagicMock()
        es.snapshot.get.return_value = _wrap({
            "snapshots": [{
                "snapshot": "snap1",
                "state": "SUCCESS",
                "start_time": "2026-04-01T00:00:00Z",
                "end_time": "2026-04-01T00:01:00Z",
                "indices": [f"idx_{i}" for i in range(150)],
            }]
        })
        # Cluster has all 150 of them.
        es.cat.indices.return_value = _wrap("\n".join(f"idx_{i}" for i in range(150)))

        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            info = SnapshotService.inspect_snapshot("daily_repo", "snap1")

        # Every index should be marked as existing.
        assert all(idx["exists_in_cluster"] for idx in info["indices"])
        # And cat.indices must NOT have been called with an index= filter
        # (that was the bug — long URL caused failures).
        call_kwargs = es.cat.indices.call_args.kwargs
        assert "index" not in call_kwargs

    def test_should_raise_when_snapshot_missing(self):
        es = mock.MagicMock()
        es.snapshot.get.return_value = _wrap({"snapshots": []})
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            with pytest.raises(FileNotFoundError):
                SnapshotService.inspect_snapshot("daily_repo", "missing")

    def test_should_reject_invalid_snapshot_name(self):
        with pytest.raises(ValueError, match="Invalid snapshot name"):
            SnapshotService.inspect_snapshot("daily_repo", "bad name with spaces")


# ---------------------------------------------------------------------------
# restore_indices
# ---------------------------------------------------------------------------

class TestRestoreIndices:

    def _es_with_snapshot(self, indices_in_snapshot, indices_in_cluster=()):
        es = mock.MagicMock()
        es.snapshot.get.return_value = _wrap({
            "snapshots": [{
                "snapshot": "snap1",
                "state": "SUCCESS",
                "start_time": "2026-04-01T00:00:00Z",
                "end_time": "2026-04-01T00:01:00Z",
                "indices": list(indices_in_snapshot),
            }]
        })
        es.cat.indices.return_value = _wrap("\n".join(indices_in_cluster))
        return es

    def test_should_restore_when_indices_are_new_and_no_replace_needed(self):
        es = self._es_with_snapshot(["fresh_a", "fresh_b"], indices_in_cluster=[])
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            result = SnapshotService.restore_indices("daily_repo", "snap1", ["fresh_a", "fresh_b"])
        assert sorted(result["restored"]) == ["fresh_a", "fresh_b"]
        assert result["deleted_before_restore"] == []
        es.indices.delete.assert_not_called()
        es.snapshot.restore.assert_called_once()

    def test_should_delete_existing_when_replace_existing_true(self):
        es = self._es_with_snapshot(["de_asset_teams", "fresh"], indices_in_cluster=["de_asset_teams"])
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            result = SnapshotService.restore_indices(
                "daily_repo", "snap1", ["de_asset_teams", "fresh"], replace_existing=True,
            )
        assert "de_asset_teams" in result["deleted_before_restore"]
        es.indices.delete.assert_called_once()
        es.snapshot.restore.assert_called_once()

    def test_should_reject_when_existing_index_and_replace_existing_false(self):
        es = self._es_with_snapshot(["de_asset_teams"], indices_in_cluster=["de_asset_teams"])
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            with pytest.raises(ValueError, match="already exist"):
                SnapshotService.restore_indices("daily_repo", "snap1", ["de_asset_teams"])
        es.indices.delete.assert_not_called()
        es.snapshot.restore.assert_not_called()

    def test_should_reject_when_index_not_in_snapshot(self):
        es = self._es_with_snapshot(["only_this"])
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            with pytest.raises(ValueError, match="not in snapshot"):
                SnapshotService.restore_indices("daily_repo", "snap1", ["missing_idx"])

    def test_should_reject_when_snapshot_missing(self):
        es = mock.MagicMock()
        es.snapshot.get.return_value = _wrap({"snapshots": []})
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=es):
            with pytest.raises(FileNotFoundError):
                SnapshotService.restore_indices("daily_repo", "missing", ["a"])

    def test_should_reject_empty_index_list(self):
        with pytest.raises(ValueError, match="non-empty"):
            SnapshotService.restore_indices("daily_repo", "snap1", [])

    def test_should_reject_invalid_index_name(self):
        with pytest.raises(ValueError, match="Invalid index name"):
            SnapshotService.restore_indices("daily_repo", "snap1", ["../bad"])

    def test_should_raise_when_es_unavailable(self):
        with mock.patch("asset_management.web.services.snapshot_service.get_elasticsearch_client", return_value=None):
            with pytest.raises(SnapshotServiceError):
                SnapshotService.restore_indices("daily_repo", "snap1", ["a"])
