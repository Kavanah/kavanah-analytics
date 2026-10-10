"""Tests for ``lib.cleanup_no_data_markers``."""

from unittest import mock

import pytest

from lib.cleanup_no_data_markers import (
    dedupe_markers,
    delete_stale_markers_for_date,
    delete_zombie_markers,
)


def _marker_hit(_id, team_esid, kibana_date, index="flkvj_fb_insights", team_title="FLKVJ"):
    return {
        "_index": index,
        "_id": _id,
        "_source": {
            "team_esid": team_esid,
            "kibana_date": kibana_date,
            "team_title": team_title,
            "no_data": True,
        },
    }


def _scroll_returning(hits_pages):
    """Build an es mock whose .search/.scroll return successive pages."""
    es = mock.MagicMock()
    pages = list(hits_pages) + [[]]  # final empty page exits the loop
    responses = [{"_scroll_id": "s", "hits": {"hits": p}} for p in pages]
    es.search.return_value = responses[0]
    es.scroll.side_effect = responses[1:]
    return es


class TestDeleteZombieMarkers:
    def test_deletes_marker_when_real_doc_exists_for_same_team_and_date(self):
        es = _scroll_returning([[_marker_hit("m1", "team-A", "2025-04-16")]])
        es.count.return_value = {"count": 5}  # real docs exist

        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            counts = delete_zombie_markers("*_fb_insights")

        es.delete.assert_called_once_with(index="flkvj_fb_insights", id="m1")
        # Single end-of-batch refresh, not per-call.
        es.indices.refresh.assert_called_once()
        assert counts["zombies"] == 1
        assert counts["deleted"] == 1

    def test_keeps_marker_when_no_real_doc_exists(self):
        es = _scroll_returning([[_marker_hit("m1", "team-A", "2025-04-16")]])
        es.count.return_value = {"count": 0}  # no real docs

        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            counts = delete_zombie_markers("*_fb_insights")

        es.delete.assert_not_called()
        assert counts["zombies"] == 0
        assert counts["deleted"] == 0

    def test_dry_run_counts_but_does_not_delete(self):
        es = _scroll_returning([[_marker_hit("m1", "team-A", "2025-04-16")]])
        es.count.return_value = {"count": 5}

        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            counts = delete_zombie_markers("*_fb_insights", dry_run=True)

        es.delete.assert_not_called()
        es.indices.refresh.assert_not_called()
        assert counts["zombies"] == 1
        assert counts["deleted"] == 0

    def test_no_refresh_when_nothing_deleted(self):
        # Marker exists but no real doc — nothing to delete, no need to
        # refresh. Avoids a wasted round trip on a clean cluster.
        es = _scroll_returning([[_marker_hit("m1", "team-A", "2025-04-16")]])
        es.count.return_value = {"count": 0}

        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            delete_zombie_markers("*_fb_insights")

        es.indices.refresh.assert_not_called()

    def test_skips_marker_with_non_iso_kibana_date(self):
        # Echo writes free-text dates like "April 27, 2026..." into
        # kibana_date. The cleanup must not pretend to compare those.
        es = _scroll_returning(
            [[_marker_hit("m1", "team-A", "April 27, 2026 05:23:46 UTC")]]
        )

        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            counts = delete_zombie_markers("*_echo_conversations")

        es.count.assert_not_called()  # never queried because skipped early
        es.delete.assert_not_called()
        assert counts["skipped_non_iso"] == 1
        assert counts["zombies"] == 0

    def test_returns_error_count_when_es_client_unavailable(self):
        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=None
        ):
            counts = delete_zombie_markers("*_fb_insights")
        assert counts["errors"] == 1
        assert counts["scanned"] == 0


class TestDedupeMarkers:
    def test_replaces_legacy_id_with_canonical_id(self):
        es = _scroll_returning([[_marker_hit("legacy-abc", "team-A", "2025-04-16")]])
        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            counts = dedupe_markers("*_fb_insights")

        # First an upsert at the canonical ID, then delete the legacy doc.
        assert es.index.call_count == 1
        upsert_kwargs = es.index.call_args.kwargs
        assert upsert_kwargs["id"] == "team-A_2025-04-16_no_data"
        assert upsert_kwargs["document"]["no_data"] is True

        es.delete.assert_called_once_with(index="flkvj_fb_insights", id="legacy-abc")
        # Single end-of-batch refresh, not per-call.
        es.indices.refresh.assert_called_once()
        assert counts["replaced"] == 1
        assert counts["canonical_kept"] == 0

    def test_keeps_marker_already_at_canonical_id(self):
        canonical = "team-A_2025-04-16_no_data"
        es = _scroll_returning([[_marker_hit(canonical, "team-A", "2025-04-16")]])
        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            counts = dedupe_markers("*_fb_insights")

        es.index.assert_not_called()
        es.delete.assert_not_called()
        assert counts["canonical_kept"] == 1
        assert counts["replaced"] == 0

    def test_dedupe_collapses_multiple_legacy_markers_for_same_pair(self):
        # Two markers, same team+date, both with legacy auto IDs.
        # First gets replaced with the canonical-id doc; second hits the same
        # canonical id (idempotent) and gets its own legacy id deleted.
        hits = [
            _marker_hit("legacy-1", "team-A", "2025-04-16"),
            _marker_hit("legacy-2", "team-A", "2025-04-16"),
        ]
        es = _scroll_returning([hits])
        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            counts = dedupe_markers("*_fb_insights")

        assert es.index.call_count == 2  # both upsert at the canonical id
        # Both legacy ids should have been deleted.
        deleted_ids = {c.kwargs["id"] for c in es.delete.call_args_list}
        assert deleted_ids == {"legacy-1", "legacy-2"}
        assert counts["replaced"] == 2

    def test_dry_run_replaces_nothing(self):
        es = _scroll_returning([[_marker_hit("legacy-abc", "team-A", "2025-04-16")]])
        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            counts = dedupe_markers("*_fb_insights", dry_run=True)

        es.index.assert_not_called()
        es.delete.assert_not_called()
        es.indices.refresh.assert_not_called()
        assert counts["replaced"] == 1

    def test_individual_writes_do_not_pass_refresh_kwarg(self):
        # Per-call refresh="wait_for" was the perf bug — every es.index/es.delete
        # blocked for ~1s. Confirm it stays gone so a future regression doesn't
        # silently re-introduce a 16-minute migration.
        es = _scroll_returning([[_marker_hit("legacy-abc", "team-A", "2025-04-16")]])
        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            dedupe_markers("*_fb_insights")

        for call in es.index.call_args_list:
            assert "refresh" not in call.kwargs
        for call in es.delete.call_args_list:
            assert "refresh" not in call.kwargs

    def test_scroll_is_cleared_before_any_writes(self):
        # Regression: writing to the scanned indices while the scroll is still
        # open invalidates the scroll context (observed on staging as
        # "NotFoundError: No search context found for id ..."). The scan must
        # drain the scroll into a list and call clear_scroll BEFORE any index/
        # delete operation runs.
        es = _scroll_returning([[
            _marker_hit("legacy-1", "team-A", "2025-04-16"),
            _marker_hit("legacy-2", "team-B", "2025-04-17"),
        ]])
        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            dedupe_markers("*_fb_insights")

        # Build a flat list of method names in the order they were called on
        # the mock. clear_scroll must come before the first index/delete.
        call_names = [name for name, _, _ in es.mock_calls if name in {
            "search", "scroll", "clear_scroll", "index", "delete",
        }]
        assert "clear_scroll" in call_names, "scroll context must be cleared explicitly"
        clear_idx = call_names.index("clear_scroll")
        first_write_idx = next(
            (i for i, n in enumerate(call_names) if n in {"index", "delete"}),
            None,
        )
        assert first_write_idx is not None, "test setup error — no writes happened"
        assert clear_idx < first_write_idx, (
            "clear_scroll must run before any index/delete — otherwise the "
            "writes invalidate the scroll context"
        )


class TestDeleteStaleMarkersForDateNoOp:
    """``delete_stale_markers_for_date`` is now a library-level
    no-op companion to ``write_no_data_markers``. No deletes fire under any
    combination of inputs; the operator CLI tools above
    (``delete_zombie_markers``, ``dedupe_markers``) remain functional for
    cleaning up existing markers if an operator wants to purge.
    """

    def test_no_es_writes_for_any_team_set(self):
        es = mock.MagicMock()
        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ) as m_client:
            deleted = delete_stale_markers_for_date(
                "*_fb_insights",
                "2025-04-16",
                {"team-A", "team-B"},
            )
        assert deleted == 0
        es.delete_by_query.assert_not_called()
        m_client.assert_not_called()

    def test_returns_zero_on_empty_team_set(self):
        es = mock.MagicMock()
        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client", return_value=es
        ):
            deleted = delete_stale_markers_for_date("*_fb_insights", "2025-04-16", set())
        assert deleted == 0
        es.delete_by_query.assert_not_called()

    def test_returns_zero_on_non_iso_date(self):
        # earlier, non-ISO dates triggered a warning + skip. Now the
        # whole function no-ops, so the same path returns 0 silently.
        with mock.patch(
            "lib.cleanup_no_data_markers.get_elasticsearch_client"
        ) as m_client:
            deleted = delete_stale_markers_for_date(
                "*_echo_conversations",
                "April 27, 2026 05:23:46 UTC",
                {"team-A"},
            )
        assert deleted == 0
        m_client.assert_not_called()
