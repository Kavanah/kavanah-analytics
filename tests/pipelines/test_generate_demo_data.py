"""
Tests for import_scripts.migration.generate_demo_data.

the Disciple Tools sources were removed from this script (the
staging→promote pipeline they fed was retired at the cutover; demo DT
is owned by scripts/seed_kavanah_demo_dt.py). These tests pin that the daily
path never writes disciple-tools-staging again, that the legacy cron line
(--dt-days) still runs as a no-op, and that find_team disambiguates duplicate
team titles by organization.

All Elasticsearch calls and generators are mocked — no real indexing.
"""

from datetime import date, timedelta
from unittest import mock

import pytest

import import_scripts.migration.generate_demo_data as gdd


# ---------------------------------------------------------------------------
# _build_target_dates — pure date-list builder
# ---------------------------------------------------------------------------


class TestBuildTargetDates:

    def test_returns_single_none_when_days_is_none(self):
        assert gdd._build_target_dates(None) == [None]

    def test_returns_single_date_when_days_is_one(self):
        result = gdd._build_target_dates(1)
        assert result == [date.today()]

    def test_returns_consecutive_dates_ending_today(self):
        result = gdd._build_target_dates(5)
        today = date.today()
        assert result[-1] == today
        assert result[0] == today - timedelta(days=4)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# main — DT retired from the daily path
# ---------------------------------------------------------------------------

GEN_NAMES = [
    "gen_fb_insights",
    "gen_fb_page_insights",
    "gen_google_ads",
    "gen_google_ads_geo",
    "gen_ga_metrics",
    "gen_echo_conversations",
    "gen_ig_insights",
    "gen_youtube_analytics",
    "gen_youtube_country_breakdown",
    "gen_chatwoot_insights",
    "gen_chatwoot_conversations",
]


class TestDtRetiredFromDailyPath:
    """End-to-end test of main() with ES + generators mocked."""

    def _patch_dependencies(self, monkeypatch):
        """Stub out ES client, org/team lookup, schema loaders, and indexing.

        Returns the index_doc Mock so tests can inspect which indices were
        written.
        """
        monkeypatch.setattr(gdd, "get_elasticsearch_client", lambda: mock.MagicMock())
        monkeypatch.setattr(gdd, "ensure_org_exists", lambda es, org: None)
        monkeypatch.setattr(
            gdd,
            "find_team",
            lambda es, team_title, org_title: {
                "team_title": team_title,
                "team_esid": "test-esid",
                "organization_title": org_title,
            },
        )
        monkeypatch.setattr(gdd, "load_schema_properties", lambda name: {})
        monkeypatch.setattr(gdd, "put_regular_index_template", lambda es, idx, m: None)
        index_doc = mock.MagicMock()
        monkeypatch.setattr(gdd, "index_doc", index_doc)
        return index_doc

    def _count_generator_calls(self, monkeypatch):
        """Wrap every gen_* with a Mock; return dict of name → Mock."""
        tracked = {}
        for name in GEN_NAMES:
            m = mock.MagicMock(return_value=[{"_stub": True}])
            monkeypatch.setattr(gdd, name, m)
            tracked[name] = m
        return tracked

    def test_legacy_cron_line_never_writes_disciple_tools_staging(self, monkeypatch):
        """should not write disciple-tools-staging when invoked with the
        earlier prod cron arguments (--days 1 --dt-days 130)."""
        index_doc = self._patch_dependencies(monkeypatch)
        tracked = self._count_generator_calls(monkeypatch)

        monkeypatch.setattr(
            "sys.argv",
            ["generate_demo_data.py", "--team", "Kavanah Team", "--org", "Kavanah", "--days", "1", "--dt-days", "130"],
        )
        gdd.main()  # must not raise — --dt-days is a deprecated no-op

        written_indices = {call.args[1] for call in index_doc.call_args_list}
        assert "disciple-tools-staging" not in written_indices
        assert written_indices, "expected non-DT sources to index documents"
        # esid-form names ({team_esid}__{source}); the stub team's esid is "test-esid"
        assert all(idx.startswith("test-esid__") for idx in written_indices)

        # Non-DT generators run once (one day in --days); nothing runs 130x.
        assert tracked["gen_fb_insights"].call_count == 1
        assert tracked["gen_chatwoot_insights"].call_count == 1

    def test_days_drives_all_generators(self, monkeypatch):
        self._patch_dependencies(monkeypatch)
        tracked = self._count_generator_calls(monkeypatch)

        monkeypatch.setattr(
            "sys.argv",
            ["generate_demo_data.py", "--team", "TestTeam", "--org", "TestOrg", "--days", "3"],
        )
        gdd.main()

        for name in GEN_NAMES:
            assert tracked[name].call_count == 3, name

    def test_target_date_runs_single_day(self, monkeypatch):
        """--target-date picks a single date for everything, ignoring --days."""
        self._patch_dependencies(monkeypatch)
        tracked = self._count_generator_calls(monkeypatch)

        monkeypatch.setattr(
            "sys.argv",
            ["generate_demo_data.py", "--team", "TestTeam", "--org", "TestOrg", "--target-date", "2026-01-15", "--days", "10"],
        )
        gdd.main()

        assert tracked["gen_fb_insights"].call_count == 1
        assert tracked["gen_chatwoot_insights"].call_count == 1


# ---------------------------------------------------------------------------
# find_team — duplicate team_title disambiguated by organization
# ---------------------------------------------------------------------------


class TestFindTeamOrgDisambiguation:

    def _es_returning(self, hits):
        es = mock.MagicMock()
        es.search.return_value = {"hits": {"hits": hits}}
        return es

    @staticmethod
    def _hit(_id, team_title, org):
        return {"_id": _id, "_source": {"team_title": team_title, "organization_title": org}}

    def test_prefers_exact_title_hit_matching_org(self):
        """should return the team whose organization matches when two teams in
        different workspaces share the same title."""
        es = self._es_returning(
            [
                self._hit("foreign-esid", "Kavanah Team", "SomeoneElse"),
                self._hit("real-esid", "Kavanah Team", "Kavanah"),
            ]
        )

        team = gdd.find_team(es, "Kavanah Team", "Kavanah")

        assert team["team_esid"] == "real-esid"

    def test_matches_org_inside_list_valued_field(self):
        """organization_title is list-valued on some team docs."""
        es = self._es_returning(
            [
                self._hit("foreign-esid", "Kavanah Team", ["SomeoneElse"]),
                self._hit("real-esid", "Kavanah Team", ["Kavanah", "Other"]),
            ]
        )

        team = gdd.find_team(es, "Kavanah Team", "Kavanah")

        assert team["team_esid"] == "real-esid"

    def test_falls_back_to_first_exact_title_hit_when_no_org_matches(self):
        es = self._es_returning(
            [
                self._hit("only-esid", "Kavanah Team", "SomeoneElse"),
            ]
        )

        team = gdd.find_team(es, "Kavanah Team", "Kavanah")

        assert team["team_esid"] == "only-esid"
