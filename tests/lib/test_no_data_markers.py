"""Tests for ``lib.no_data_markers``.

 neutralized this subsystem at the library level — the new
dashboard design no longer reads no_data markers, so ``write_no_data_markers``
and ``backfill_missing_no_data_markers`` are now no-ops. The deterministic
``marker_doc_id`` helper survives (still used by the operator-CLI cleanup
tools in ``cleanup_no_data_markers``).

Historical contract (markers ARE written) was pinned by the +
 tests this file used to hold. Those assertions are gone with
; the new contract is "no writes, ever, regardless of arguments."
"""

from unittest import mock

import pytest

from lib.no_data_markers import (
    backfill_missing_no_data_markers,
    marker_doc_id,
    write_no_data_markers,
)


class TestMarkerDocId:
    """Pure-function tests — still valid because the
    operator-CLI cleanup tools (``delete_zombie_markers``, ``dedupe_markers``)
    use this helper to recognize and collapse existing legacy markers.
    """

    def test_id_is_deterministic_for_same_inputs(self):
        a = marker_doc_id("team1", "2025-04-16")
        b = marker_doc_id("team1", "2025-04-16")
        assert a == b

    def test_id_differs_when_team_differs(self):
        assert marker_doc_id("team1", "2025-04-16") != marker_doc_id("team2", "2025-04-16")

    def test_id_differs_when_date_differs(self):
        assert marker_doc_id("team1", "2025-04-16") != marker_doc_id("team1", "2025-04-17")

    def test_id_format_includes_no_data_suffix(self):
        assert marker_doc_id("team1", "2025-04-16").endswith("_no_data")


class TestWriteNoDataMarkersNoOp:
    """``write_no_data_markers`` is a library-level no-op. No ES
    writes should fire under any combination of inputs.
    """

    def _es_mock(self):
        return mock.MagicMock()

    def test_no_es_writes_when_teams_have_no_data(self):
        es = self._es_mock()
        team_mapping = {"page1": {"_id": "team-A", "team_title": "FLKVJ"}}
        with mock.patch("lib.no_data_markers.get_elasticsearch_client", return_value=es) as m_client:
            write_no_data_markers(
                date="2025-04-16",
                teams_with_data=set(),
                index_suffix="_fb_insights",
                team_mapping=team_mapping,
            )
        es.index.assert_not_called()
        es.indices.create.assert_not_called()
        m_client.assert_not_called()

    def test_no_es_writes_when_teams_have_data(self):
        es = self._es_mock()
        team_mapping = {"page1": {"_id": "team-A", "team_title": "FLKVJ"}}
        with mock.patch("lib.no_data_markers.get_elasticsearch_client", return_value=es):
            write_no_data_markers(
                date="2025-04-16",
                teams_with_data={"team-A"},
                index_suffix="_fb_insights",
                team_mapping=team_mapping,
            )
        es.index.assert_not_called()

    def test_no_es_writes_for_team_list_input_shape(self):
        es = self._es_mock()
        with mock.patch("lib.no_data_markers.get_elasticsearch_client", return_value=es):
            write_no_data_markers(
                date="2025-04-16",
                teams_with_data=set(),
                index_suffix="_google_ads",
                team_list=[("team-B", "Other Team")],
            )
        es.index.assert_not_called()

    def test_no_raise_when_called_with_empty_inputs(self):
        # Defensive: importers occasionally pass empty mapping + empty set.
        # No-op must not raise on degenerate input.
        with mock.patch("lib.no_data_markers.get_elasticsearch_client"):
            write_no_data_markers(
                date="2025-04-16",
                teams_with_data=set(),
                index_suffix="_fb_insights",
                team_mapping={},
            )


class TestBackfillMissingNoDataMarkersNoOp:
    """``backfill_missing_no_data_markers`` is also a no-op.
    Returns 0 so the operator CLI at ``scripts/backfill_no_data_markers.py``
    sees "wrote 0 markers" instead of raising.
    """

    def test_returns_zero_and_writes_nothing(self):
        es = mock.MagicMock()
        with mock.patch("lib.no_data_markers.get_elasticsearch_client", return_value=es) as m_client:
            wrote = backfill_missing_no_data_markers(
                start="2025-04-01",
                end="2025-04-30",
                index_suffix="_fb_insights",
                team_list=[("team-A", "Alpha")],
            )
        assert wrote == 0
        es.index.assert_not_called()
        es.search.assert_not_called()
        m_client.assert_not_called()
