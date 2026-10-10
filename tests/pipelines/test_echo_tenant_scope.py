"""Echo conversation attribution must be tenant-scoped.

Before the fix, the referrer→asset maps were built with a cluster-wide
match_all over de_asset_fb_pages / de_asset_youtube_channels, and the
asset→team resolution was an unconstrained size=1 query over de_asset_teams.
With the substring (strategy 5/6) and SequenceMatcher-0.75 (strategy 7)
matchers, a conversation pulled from tenant A's Echo endpoint could be
attributed to tenant B's team and indexed into tenant B's
``{team_esid}__echo_conversations`` — carrying client_name, client_country,
client_language, client_religion and disposition_notes onto another tenant's
dashboards.

After the fix, per-tenant pools are composed inside main()'s tenant loop:
``get_tenant_team_maps`` (teams of THIS tenant only, echo_tenant_esid clause)
feeds ``build_fb_pages_maps`` / ``build_yt_channels_map`` (only assets those
teams reference), and team resolution is a dict lookup that cannot leave the
tenant. ``require_scope`` additionally refuses scope-less direct invocations
(``import.py echo get_conversations`` with no --limit_teams).

Mutation pins:
  M1. Drop the ``allowed`` filter in build_fb_pages_maps ->
      ``test_foreign_page_never_enters_tenant_pool`` fails.
  M2. Drop the echo_tenant_esid clause in get_tenant_team_maps ->
      ``TestGetTenantTeamMaps.test_builds_maps_scoped_to_tenant_query``
      (tests/pipelines/test_echo.py) fails.
  M3. Drop the require_scope gate in main ->
      ``test_unscoped_run_refuses_before_fetching`` and
      ``TestEchoGetConversationsMain.test_requires_scope`` fail.
"""

from __future__ import annotations

from collections import defaultdict
from unittest import mock

import pytest

import import_scripts.echo.get_conversations as echo_get

# DoD gate — release-plan coverage for the cycle.
pytestmark = []

_ECHO = "import_scripts.echo.get_conversations"

# Tenant B's page — similar name to tenant A's referrer, the exact shape the
# substring matcher (strategy 5/6) used to cross-match.
_FOREIGN_PAGE_HITS = [
    {"_id": "pg-B", "_source": {"fb_page_id": "999", "fb_page_name": "Way Finder Global"}},
]


def _run_entry(entry, fb_pages_map, page_esid_to_fb_page_id, fb_page_id_to_team):
    stats = defaultdict(int)
    saves = []
    with mock.patch(f"{_ECHO}._resolve_team_country", return_value="US"), \
         mock.patch(f"{_ECHO}.create_directory"), \
         mock.patch(f"{_ECHO}.save_to_file", side_effect=lambda d, f, e: saves.append((d, e))), \
         mock.patch(f"{_ECHO}.os.path.exists", return_value=False), \
         mock.patch("lib.import_run_context.record_user_error"):
        echo_get.process_conversation_entry(
            entry,
            es=mock.MagicMock(),
            fb_pages_map=fb_pages_map,
            page_esid_to_fb_page_id=page_esid_to_fb_page_id,
            yt_channels_map={},
            fb_page_id_to_team=fb_page_id_to_team,
            yt_esid_to_team={},
            tenant_esid="tenant-A",
            base_url="https://tenant-a.echoglobal.org",
            limit_teams_list=[],
            match_stats=stats,
        )
    return stats, saves


def test_foreign_page_never_enters_tenant_pool():
    # M1 pin: tenant A's teams reference no pages, so the pool built from the
    # cluster-wide fetch must be EMPTY — the foreign page's name/id/slug keys
    # (the fuzzy-match surface) never become candidates.
    fb_map, esid_map = echo_get.build_fb_pages_maps(_FOREIGN_PAGE_HITS, allowed_fb_page_ids=set())
    assert fb_map == {}
    assert esid_map == {}


def test_cross_tenant_referrer_lands_in_unknownteams():
    # End-to-end through the per-tenant pool composition: a tenant-A
    # conversation whose referrer matches tenant B's page name must save to
    # unknownteams with team_esid "unknown" — never to tenant B's team.
    fb_page_id_to_team = {}  # tenant A's teams reference no assets
    fb_map, esid_map = echo_get.build_fb_pages_maps(_FOREIGN_PAGE_HITS, set(fb_page_id_to_team))

    entry = {"id": "c1", "referrer": "Way Finder Global", "status": "open"}
    stats, saves = _run_entry(entry, fb_map, esid_map, fb_page_id_to_team)

    assert stats["unmatched"] == 1
    assert entry["team_esid"] == "unknown"
    assert len(saves) == 1
    assert "unknownteams" in saves[0][0]


def test_in_tenant_match_still_attributes():
    # Regression guard: tenant A's own page still matches and resolves to
    # tenant A's team via the dict.
    team = {"team_title": "Alpha", "team_city": "c", "team_url": "u", "organization_title": "O"}
    page_hits = [{"_id": "pg-A", "_source": {"fb_page_id": "111", "fb_page_name": "Alpha Outreach"}}]
    fb_page_id_to_team = {"111": (team, "t-A")}
    fb_map, esid_map = echo_get.build_fb_pages_maps(page_hits, set(fb_page_id_to_team))

    entry = {"id": "c2", "referrer": "Alpha Outreach", "status": "open"}
    stats, saves = _run_entry(entry, fb_map, esid_map, fb_page_id_to_team)

    assert stats["exact"] == 1
    assert entry["team_esid"] == "t-A"
    assert entry["team_title"] == "Alpha"
    assert len(saves) == 1
