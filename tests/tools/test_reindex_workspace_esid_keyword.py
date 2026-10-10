"""Tests for the reindex tool's data-loss guard.

on staging the tool emptied de_asset_teams + de_asset_echarts_dashboards.
The destructive `es.indices.delete(original)` (step 4) must be UNREACHABLE when the
step-3 reindex moved no docs out of a non-empty source — otherwise the original is
deleted and its data is lost with the (later-deleted) temp index.
"""
import os
import sys
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from tools import reindex_workspace_esid_keyword as tool


def _es_with_counts(src_count, v2_count):
    """Build a mock ES client whose count() returns src_count for the original
    index and v2_count for the _v2 temp index. Mapping is text (needs reindex)."""
    es = mock.MagicMock()
    # The original index exists; the _v2 temp does not (no leftover).
    es.indices.exists.side_effect = lambda index: not index.endswith("_v2")
    es.indices.get_mapping.return_value = {
        "idx": {"mappings": {"properties": {"workspace_esid": {"type": "text"}}}}
    }

    def count(index):
        # the original index name is "idx"; temp is "idx_v2"
        return {"count": v2_count if index.endswith("_v2") else src_count}

    es.count.side_effect = count
    return es


class TestKan994DeleteGuard:
    def test_aborts_without_deleting_when_v2_empty_from_nonempty_source(self):
        """source had 34 docs but the reindex put 0 in _v2: the tool
        MUST abort and MUST NOT delete the original index.

        Note: arithmetically (34 != 0) the pre-existing count-mismatch check
        already aborts this case; the dedicated empty-v2 guard is defense in
        depth for a count-check regression. Either way the contract under test
        is the one that matters: the ORIGINAL is never deleted here."""
        es = _es_with_counts(src_count=34, v2_count=0)

        with mock.patch.object(tool, "put_regular_index_template"):
            outcome = tool._reindex_one(es, "idx", apply=True)

        assert outcome.startswith("aborted")
        # The destructive delete of the ORIGINAL must never have been called.
        delete_targets = [c.kwargs.get("index", (c.args[0] if c.args else None))
                          for c in es.indices.delete.call_args_list]
        assert "idx" not in delete_targets, (
            "the original index was deleted despite a failed reindex — data loss"
        )

    def test_normal_path_deletes_and_swaps_when_counts_match(self):
        """Sanity: when src and v2 counts agree (and are non-zero), the tool
        proceeds through the delete+recreate+swap as designed."""
        es = _es_with_counts(src_count=10, v2_count=10)
        # After step 5 the final count must also match src for success.
        with mock.patch.object(tool, "put_regular_index_template"):
            outcome = tool._reindex_one(es, "idx", apply=True)
        assert outcome == "reindexed"
        delete_targets = [c.kwargs.get("index", (c.args[0] if c.args else None))
                          for c in es.indices.delete.call_args_list]
        assert "idx" in delete_targets  # original was swapped as designed

    def test_empty_source_is_allowed_to_recreate(self):
        """A genuinely-empty source (0 -> 0) is the harmless already-empty case;
        the guard only fires on src>0 with v2==0, so this proceeds. No safety
        snapshot is needed (nothing to lose)."""
        es = _es_with_counts(src_count=0, v2_count=0)
        with mock.patch.object(tool, "put_regular_index_template"):
            outcome = tool._reindex_one(es, "idx", apply=True)
        assert outcome == "reindexed"
        es.snapshot.create.assert_not_called()  # nothing to protect

    def test_nonempty_source_aborts_when_safety_snapshot_fails(self):
        """if the pre-delete safety snapshot can't be taken, the tool
        MUST abort rather than delete the original unprotected."""
        es = _es_with_counts(src_count=10, v2_count=10)
        es.snapshot.create.side_effect = RuntimeError("no reindex_safety_repo")
        with mock.patch.object(tool, "put_regular_index_template"):
            outcome = tool._reindex_one(es, "idx", apply=True)
        assert outcome == "aborted: no safety snapshot"
        delete_targets = [c.kwargs.get("index", (c.args[0] if c.args else None))
                          for c in es.indices.delete.call_args_list]
        assert "idx" not in delete_targets, "deleted the original without a safety snapshot"

    def test_nonempty_source_takes_safety_snapshot_before_delete(self):
        """The happy path takes a safety snapshot of the original BEFORE the
        destructive delete."""
        es = _es_with_counts(src_count=10, v2_count=10)
        with mock.patch.object(tool, "put_regular_index_template"):
            tool._reindex_one(es, "idx", apply=True)
        es.snapshot.create.assert_called_once()
        assert es.snapshot.create.call_args.kwargs["repository"] == "reindex_safety_repo"
