"""per-team source on/off toggles for the three Meta importers.

The cached resolver in get_insights (_source_enabled_for_team) is shared by
all gates: default / store failure = ON (behavior-preserving, fail-soft);
an explicit team or workspace disable skips that team's pull for the source.
"""

from __future__ import annotations

from unittest import mock

import pytest

import import_scripts.facebook.get_insights as gi

_SVC = "asset_management.web.services.field_catalog_service.FieldCatalogService"


@pytest.fixture(autouse=True)
def _fresh_cache():
    gi._SOURCE_ENABLED_CACHE.clear()
    yield
    gi._SOURCE_ENABLED_CACHE.clear()


# ---- the shared resolver ----


def test_should_import_source_when_no_toggle_set():
    with mock.patch(f"{_SVC}.source_enabled_for_team", return_value=True):
        assert gi._source_enabled_for_team("team1", "fb_pages") is True


def test_should_skip_source_when_disabled():
    with mock.patch(f"{_SVC}.source_enabled_for_team", return_value=False):
        assert gi._source_enabled_for_team("team1", "instagram") is False


def test_should_import_source_when_store_fails():
    # Fail-soft: the toggle is config, never an import dependency.
    with mock.patch(f"{_SVC}.source_enabled_for_team", side_effect=Exception("es down")):
        assert gi._source_enabled_for_team("team1", "fb_ads") is True
    assert gi._source_enabled_for_team("unknown", "fb_ads") is True
    assert gi._source_enabled_for_team(None, "fb_pages") is True


def test_should_cache_resolution_per_team_source_pair():
    with mock.patch(f"{_SVC}.source_enabled_for_team", return_value=False) as m:
        gi._source_enabled_for_team("team1", "fb_pages")
        gi._source_enabled_for_team("team1", "fb_pages")
        gi._source_enabled_for_team("team1", "instagram")
    assert m.call_count == 2  # one per (team, source), not per call


# ---- fb_pages gate: opted-out teams drop out of the page map ----


def test_should_drop_opted_out_teams_pages_from_page_import():
    page_id_to_team = {
        "p1": {"_id": "team_on", "team_title": "On"},
        "p2": {"_id": "team_off", "team_title": "Off"},
    }
    with mock.patch(f"{_SVC}.source_enabled_for_team", side_effect=lambda t, s: t != "team_off"):
        kept = {
            pid: team
            for pid, team in page_id_to_team.items()
            if gi._source_enabled_for_team(team.get("_id"), "fb_pages")
        }
    assert set(kept) == {"p1"}


# ---- fb_ads gates ----


def test_should_filter_page_claiming_map_when_team_opted_out():
    # The daily importer's page_filtered claiming-map filter, in miniature.
    page_id_to_teams = {
        "p1": [{"_id": "team_on"}, {"_id": "team_off"}],
        "p2": [{"_id": "team_off"}],
    }
    with mock.patch(f"{_SVC}.source_enabled_for_team", side_effect=lambda t, s: t != "team_off"):
        off = {
            t.get("_id")
            for teams in page_id_to_teams.values()
            for t in teams
            if not gi._source_enabled_for_team(t.get("_id"), "fb_ads")
        }
        filtered = {
            pid: kept
            for pid, teams in page_id_to_teams.items()
            for kept in [[t for t in teams if t.get("_id") not in off]]
            if kept
        }
    assert off == {"team_off"}
    assert set(filtered) == {"p1"}
    assert [t["_id"] for t in filtered["p1"]] == ["team_on"]


def test_window_shared_account_loop_skips_opted_out_team():
    import import_scripts.facebook.get_insights_window as giw

    with mock.patch(f"{_SVC}.source_enabled_for_team", return_value=False):
        assert giw._daily._source_enabled_for_team("team_off", "fb_ads") is False


# ---- gate wiring: every importer calls the resolver at its gate site ----
# The gates live deep inside main()/account loops (full-run harnesses are
# integration scope), so the wiring is pinned at source level — same
# convention as the sections contract test.


def _src(relpath):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    return (root / relpath).read_text(encoding="utf-8")


def test_gates_are_wired_in_all_four_importers():
    daily = _src("bin/python/import_scripts/facebook/get_insights.py")
    assert '_source_enabled_for_team(_fb_team_esid, "fb_ads")' in daily  # account_wide skip
    assert 'if not _source_enabled_for_team(t.get("_id"), "fb_ads")' in daily  # claiming-map filter

    window = _src("bin/python/import_scripts/facebook/get_insights_window.py")
    assert '_daily._source_enabled_for_team(team_esid, "fb_ads")' in window  # shared-account loop
    assert '_daily._source_enabled_for_team(team_fields["team_esid"], "fb_ads")' in window  # owner path

    pages = _src("bin/python/import_scripts/facebook/get_page_insights.py")
    assert '_source_enabled_for_team(team.get("_id"), "fb_pages")' in pages

    ig = _src("bin/python/import_scripts/instagram/get_instagram_insights.py")
    assert '_source_enabled_for_team(team_esid, "instagram")' in ig


# --- orchestrator short-circuit ----


def test_all_scoped_teams_source_off_true_when_every_team_disabled():
    import import_scripts.facebook.run_daily_imports as rdi

    with mock.patch("import_scripts.facebook.lib.assets.resolve_team_filter_scope",
                    return_value={"team_esids": {"t1", "t2"}}), \
         mock.patch(f"{_SVC}.source_enabled_for_team", return_value=False):
        assert rdi._all_scoped_teams_source_off("t1,t2", "fb_pages") is True


def test_all_scoped_teams_source_off_false_when_any_team_on():
    import import_scripts.facebook.run_daily_imports as rdi

    with mock.patch("import_scripts.facebook.lib.assets.resolve_team_filter_scope",
                    return_value={"team_esids": {"t1", "t2"}}), \
         mock.patch(f"{_SVC}.source_enabled_for_team", side_effect=lambda t, s: t == "t2"):
        assert rdi._all_scoped_teams_source_off("t1,t2", "fb_pages") is False


def test_all_scoped_teams_source_off_false_for_cron_all_teams():
    # team_ids None (cron / all teams) must NEVER short-circuit — we can't prove
    # every team is off without enumerating the whole cluster.
    import import_scripts.facebook.run_daily_imports as rdi

    assert rdi._all_scoped_teams_source_off(None, "fb_ads") is False


def test_all_scoped_teams_source_off_false_on_resolution_failure():
    # Fail-soft: if the scope/toggle lookup blows up, run the stage (the
    # per-team gate is the correctness backstop).
    import import_scripts.facebook.run_daily_imports as rdi

    with mock.patch("import_scripts.facebook.lib.assets.resolve_team_filter_scope",
                    side_effect=Exception("es down")):
        assert rdi._all_scoped_teams_source_off("t1", "instagram") is False


def test_all_scoped_teams_source_off_false_when_scope_empty():
    import import_scripts.facebook.run_daily_imports as rdi

    with mock.patch("import_scripts.facebook.lib.assets.resolve_team_filter_scope",
                    return_value={"team_esids": set()}):
        assert rdi._all_scoped_teams_source_off("ghost", "fb_ads") is False


def test_backfill_exposes_all_disabled_phase_label():
    # Fix 2 — the all-off run surfaces a plain-English "nothing to import"
    # label and holds the bar high instead of a misleading "Importing…".
    from asset_management.web.blueprints.analytics import routes as ar

    assert ar._backfill_phase_label("all_sources_disabled") == (
        "All import sources are turned off for this team — nothing to import"
    )
    assert ar._BACKFILL_PHASE_FLOOR["all_sources_disabled"] == 99


# ---- instagram gate ----


def test_instagram_gate_skips_opted_out_team():
    with mock.patch(f"{_SVC}.source_enabled_for_team", side_effect=lambda t, s: not (t == "team_off" and s == "instagram")):
        assert gi._source_enabled_for_team("team_off", "instagram") is False
        assert gi._source_enabled_for_team("team_on", "instagram") is True
        # The same team's other sources are untouched.
        assert gi._source_enabled_for_team("team_off", "fb_ads") is True
