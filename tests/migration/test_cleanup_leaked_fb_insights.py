"""tests for cleanup_leaked_fb_insights.

Focus on the leak-detection logic (which pages count as leaks given a
team's current config) and the audit logic (which page ids are shared
across teams). The ES interactions are mocked — integration is verified
by the smoke test on dev rather than a fixture cluster.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

PYTHON_DIR = Path(__file__).resolve().parents[2] / "bin" / "python"
sys.path.insert(0, str(PYTHON_DIR))

from import_scripts.migration import cleanup_leaked_fb_insights as mod  # noqa: E402

pytestmark = []


def _mk_es_with_teams(teams: list[dict]) -> mock.MagicMock:
    """Build a mock ES client whose de_asset_teams search returns the
    given team docs. Each team dict needs: _id, team_title,
    team_facebook_page_id."""
    es = mock.MagicMock()

    def fake_search(index=None, body=None, **kwargs):
        if index == "de_asset_teams":
            return {
                "hits": {
                    "hits": [
                        {
                            "_id": t["_id"],
                            "_source": {
                                "team_title": t["team_title"],
                                "team_facebook_page_id": t.get("team_facebook_page_id"),
                            },
                        }
                        for t in teams
                    ]
                }
            }
        return {"hits": {"hits": []}}

    es.search.side_effect = fake_search
    return es


class TestLoadTeamPages:
    def test_skips_teams_with_empty_page_list(self):
        es = _mk_es_with_teams(
            [
                {"_id": "t1", "team_title": "Team1", "team_facebook_page_id": ["111"]},
                {"_id": "t2", "team_title": "Team2", "team_facebook_page_id": []},
                {"_id": "t3", "team_title": "Team3", "team_facebook_page_id": None},
            ]
        )
        result = mod._load_team_pages(es)
        assert result == {"t1": {"111"}}

    def test_normalises_string_to_singleton(self):
        es = _mk_es_with_teams(
            [{"_id": "t1", "team_title": "Team1", "team_facebook_page_id": "999"}]
        )
        result = mod._load_team_pages(es)
        assert result == {"t1": {"999"}}

    def test_stringifies_numeric_page_ids(self):
        # ES might return ints if a doc was indexed as numeric somehow.
        es = _mk_es_with_teams(
            [{"_id": "t1", "team_title": "Team1", "team_facebook_page_id": [125, 294]}]
        )
        result = mod._load_team_pages(es)
        assert result == {"t1": {"125", "294"}}


class TestAuditPageIdCollisions:
    def test_no_collisions(self):
        es = _mk_es_with_teams(
            [
                {"_id": "t1", "team_title": "Team1", "team_facebook_page_id": ["111", "222"]},
                {"_id": "t2", "team_title": "Team2", "team_facebook_page_id": ["333"]},
            ]
        )
        assert mod._audit_page_id_collisions(es) == {}

    def test_detects_collision(self):
        es = _mk_es_with_teams(
            [
                {"_id": "t1", "team_title": "Team1", "team_facebook_page_id": ["111", "222"]},
                {"_id": "t2", "team_title": "Team2", "team_facebook_page_id": ["222", "333"]},
            ]
        )
        result = mod._audit_page_id_collisions(es)
        assert result == {"222": ["Team1", "Team2"]}

    def test_three_way_collision(self):
        es = _mk_es_with_teams(
            [
                {"_id": "t1", "team_title": "A", "team_facebook_page_id": ["X"]},
                {"_id": "t2", "team_title": "B", "team_facebook_page_id": ["X"]},
                {"_id": "t3", "team_title": "C", "team_facebook_page_id": ["X"]},
            ]
        )
        result = mod._audit_page_id_collisions(es)
        assert sorted(result["X"]) == ["A", "B", "C"]


class TestScanLeaksInIndex:
    def _mk_scrolling_es(self, page_field: str, hits_data: list[dict]) -> mock.MagicMock:
        """Mock an ES client that returns the given hits on a scroll
        followed by an empty response."""
        es = mock.MagicMock()
        # First search returns the hits + a scroll_id; first scroll returns
        # the same scroll_id with empty hits to terminate the loop.
        es.search.return_value = {
            "_scroll_id": "abc",
            "hits": {
                "hits": [
                    {"_id": f"doc{i}", "_source": {page_field: pid}}
                    for i, pid in enumerate(hits_data)
                ]
            },
        }
        es.scroll.return_value = {"_scroll_id": "abc", "hits": {"hits": []}}
        return es

    def test_returns_only_leaks(self):
        es = self._mk_scrolling_es("page_id", ["111", "999", "222", "888"])
        leaks = mod._scan_leaks_in_index(es, "team_fb_insights", "page_id", {"111", "222"})
        leaked_ids = sorted(h["_source"]["page_id"] for h in leaks)
        assert leaked_ids == ["888", "999"]

    def test_clean_index_returns_empty(self):
        es = self._mk_scrolling_es("page_id", ["111", "222"])
        leaks = mod._scan_leaks_in_index(es, "team_fb_insights", "page_id", {"111", "222"})
        assert leaks == []

    def test_handles_missing_field(self):
        # A doc without the page_id field is not a leak (we can't know).
        es = self._mk_scrolling_es("page_id", [None, "111"])
        leaks = mod._scan_leaks_in_index(es, "team_fb_insights", "page_id", {"111"})
        assert leaks == []

    def test_clears_scroll_on_completion(self):
        es = self._mk_scrolling_es("page_id", ["111"])
        mod._scan_leaks_in_index(es, "team_fb_insights", "page_id", {"999"})
        es.clear_scroll.assert_called_once_with(scroll_id="abc")

    def test_clears_scroll_even_on_exception(self):
        es = self._mk_scrolling_es("page_id", ["111"])
        es.scroll.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            mod._scan_leaks_in_index(es, "team_fb_insights", "page_id", {"999"})
        es.clear_scroll.assert_called_once_with(scroll_id="abc")


class TestDeleteLeaks:
    def test_deletes_each_doc(self):
        es = mock.MagicMock()
        leaks = {
            "flkvj_fb_page_insights": [
                {"_id": "doc1", "_source": {}},
                {"_id": "doc2", "_source": {}},
            ],
            "flkkp_h_fb_page_insights": [
                {"_id": "doc3", "_source": {}},
            ],
        }
        deleted = mod._delete_leaks(es, leaks)
        assert deleted == 3
        assert es.delete.call_count == 3

    def test_continues_after_individual_failure(self):
        es = mock.MagicMock()
        es.delete.side_effect = [None, RuntimeError("transient"), None]
        leaks = {
            "idx": [
                {"_id": "doc1", "_source": {}},
                {"_id": "doc2", "_source": {}},
                {"_id": "doc3", "_source": {}},
            ]
        }
        deleted = mod._delete_leaks(es, leaks)
        # Two succeeded, one raised; the count reflects only successes.
        assert deleted == 2
