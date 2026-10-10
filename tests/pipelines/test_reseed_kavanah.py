"""
Tests for scripts.reseed_kavanah.

The script used to write DT demo docs directly to the legacy ``disciple-tools``
alias, which is retired. Pins that no source targets the
alias anymore and that --dt-only points operators at the seeder instead.

All Elasticsearch calls and generators are mocked — no real indexing.
"""

from unittest import mock

import pytest

import scripts.reseed_kavanah as reseed


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


def _patch_dependencies(monkeypatch):
    """Stub out ES, lookups, templates, and generators; return the index_docs Mock."""
    es = mock.MagicMock()
    monkeypatch.setattr(reseed, "get_elasticsearch_client", lambda: es)
    monkeypatch.setattr(reseed, "ensure_org_exists", lambda es_, org: None)
    monkeypatch.setattr(
        reseed,
        "find_team",
        lambda es_, team_title, org_title: {
            "team_title": team_title,
            "team_esid": "test-esid",
            "organization_title": org_title,
        },
    )
    monkeypatch.setattr(reseed, "load_schema_properties", lambda name: {})
    monkeypatch.setattr(reseed, "put_regular_index_template", lambda es_, idx, m: None)
    index_docs = mock.MagicMock()
    monkeypatch.setattr(reseed, "index_docs", index_docs)
    for name in GEN_NAMES:
        monkeypatch.setattr(reseed, name, mock.MagicMock(return_value=[{"_stub": True}]))
    return index_docs


class TestDtAliasRetired:

    def test_never_writes_the_retired_disciple_tools_alias(self, monkeypatch):
        """should write only the team's esid-form indices when re-seeding — never the
        retired cross-tenant disciple-tools alias."""
        index_docs = _patch_dependencies(monkeypatch)
        monkeypatch.setattr("sys.argv", ["reseed_kavanah.py", "--days", "1"])

        reseed.main()

        written_indices = {call.args[1] for call in index_docs.call_args_list}
        assert "disciple-tools" not in written_indices
        assert written_indices, "expected non-DT sources to index documents"
        assert all(idx.startswith("test-esid__") for idx in written_indices)

    def test_dt_only_exits_pointing_at_the_seeder(self, monkeypatch):
        _patch_dependencies(monkeypatch)
        monkeypatch.setattr("sys.argv", ["reseed_kavanah.py", "--days", "1", "--dt-only"])

        with pytest.raises(SystemExit) as excinfo:
            reseed.main()

        assert excinfo.value.code == 2

    def test_skip_dt_is_a_noop(self, monkeypatch):
        index_docs = _patch_dependencies(monkeypatch)
        monkeypatch.setattr("sys.argv", ["reseed_kavanah.py", "--days", "1", "--skip-dt"])

        reseed.main()

        assert index_docs.call_args_list, "expected non-DT sources to run"
