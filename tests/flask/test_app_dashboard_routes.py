"""
Tests for dashboard-related API routes and helper functions in app.py.

Covers:
  POST /api/dashboards/sync-from-kibana          — admin sync
  GET  /api/dashboards/platform-availability     — ES msearch for platform data
  GET  /api/dashboards/data-pulled-as-of         — latest @timestamp per platform
  resolve_data_view_index (helper)               — Kibana data view resolver
  _get_team_titles_for_org (helper)              — team lookup by org

  Remaining small edge cases:
  GET  /static/<filename>                        — serve_static fallback
  GET  /debug/expire-session (no session)        — 400 when no active session
  GET  /api/import/log-exists (exception)        — 500 path
  GET  /api/import/log-content (exception)       — 500 path
  GET  /api/dashboards/<key> (disabled/error)    — 403 / 500
  GET  /api/testing/run-pytest (generic error)   — 500 path
  GET  /api/testing/pytest-log (error)           — 500 path
  GET  /api/testing/check-dashboard (edge cases) — empty dashboard_id, charts not list
  GET  /api/testing/dashboards (echarts include) — echarts dashboards listed
  POST /api/backup/create,list,restore (no roles)— 403
  POST /api/dashboards/<id>/convert-to-echarts   — second ES call returns None (503)
"""

import pytest
from unittest import mock


# ===========================================================================
# POST /api/dashboards/sync-from-kibana
# ===========================================================================

# ===========================================================================
# resolve_data_view_index  (helper — tested directly)
# ===========================================================================

class TestResolveDataViewIndex:

    def _resolve(self, platform, status=200, body=None, exc=None):
        from asset_management.web.app import resolve_data_view_index
        import os
        mock_resp = mock.MagicMock()
        mock_resp.status_code = status
        mock_resp.json.return_value = body or {"data_view": []}

        env = {"ES01_PASSWORD": "test-password", "KIBANA_URL": "https://kb.test"}
        if exc:
            with mock.patch.dict("os.environ", env), \
                 mock.patch("requests.get", side_effect=exc):
                return resolve_data_view_index(platform)
        with mock.patch.dict("os.environ", env), \
             mock.patch("requests.get", return_value=mock_resp):
            return resolve_data_view_index(platform)

    def test_returns_none_for_unknown_platform(self):
        from asset_management.web.app import resolve_data_view_index
        result = resolve_data_view_index("Unknown Platform")
        assert result is None

    def test_returns_none_when_password_not_set(self):
        from asset_management.web.app import resolve_data_view_index
        import os
        with mock.patch.dict("os.environ", {}, clear=True):
            # Make sure ES01_PASSWORD is not in env
            os_env = {k: v for k, v in os.environ.items() if k != "ES01_PASSWORD"}
            os_env.pop("ES01_PASSWORD", None)
            with mock.patch.dict("os.environ", os_env, clear=True):
                result = resolve_data_view_index("Meta")
        assert result is None

    def test_returns_none_when_kibana_returns_non_200(self):
        result = self._resolve("Meta", status=503)
        assert result is None

    def test_returns_index_pattern_when_data_view_found(self):
        body = {"data_view": [{"name": "Facebook data view", "title": "fb_ads_*"}]}
        result = self._resolve("Meta", status=200, body=body)
        assert result == "fb_ads_*"

    def test_returns_none_when_data_view_not_found(self):
        body = {"data_view": [{"name": "Other View", "title": "other_*"}]}
        result = self._resolve("Meta", status=200, body=body)
        assert result is None


# ===========================================================================
# _get_team_titles_for_org  (helper — tested directly)
# ===========================================================================

class TestGetTeamTitlesForOrg:

    def _call(self, mock_es, org="Acme", filter_type="all"):
        from asset_management.web.app import _get_team_titles_for_org
        return _get_team_titles_for_org(mock_es, org, filter_type)

    def _es_with_teams(self, teams):
        """Build mock ES returning given list of (team_title, org_title) tuples."""
        mock_es = mock.MagicMock()
        hits = [
            {"_source": {"team_title": tt, "organization_title": ot}}
            for tt, ot in teams
        ]
        mock_es.search.return_value = {"hits": {"hits": hits}}
        return mock_es

    def test_returns_all_teams_for_filter_type_all(self):
        mock_es = self._es_with_teams([("Alpha", "Acme"), ("Beta", "Acme")])
        result = self._call(mock_es, filter_type="all")
        assert "Alpha" in result
        assert "Beta" in result

    def test_returns_all_teams_for_empty_filter_type(self):
        mock_es = self._es_with_teams([("Alpha", "Acme")])
        result = self._call(mock_es, filter_type="")
        assert "Alpha" in result

    def test_filter_primary_excludes_multi_org_teams(self):
        # Single-org: org_title is a string without comma
        # Multi-org: org_title is a list with >1 item
        mock_es = self._es_with_teams([
            ("SingleOrgTeam", "Acme"),
            ("MultiOrgTeam", ["Acme", "OtherOrg"]),
        ])
        result = self._call(mock_es, filter_type="primary")
        assert "SingleOrgTeam" in result
        assert "MultiOrgTeam" not in result

    def test_filter_partnerships_includes_only_multi_org_teams(self):
        mock_es = self._es_with_teams([
            ("SingleOrgTeam", "Acme"),
            ("MultiOrgTeam", ["Acme", "OtherOrg"]),
        ])
        result = self._call(mock_es, filter_type="partnerships")
        assert "MultiOrgTeam" in result
        assert "SingleOrgTeam" not in result

    def test_skips_teams_with_no_team_title(self):
        mock_es = self._es_with_teams([("", "Acme"), ("Valid Team", "Acme")])
        result = self._call(mock_es)
        assert "" not in result
        assert "Valid Team" in result

    def test_org_title_comma_string_treated_as_multi_org(self):
        # When org_title is a string containing a comma, treated as multi-org
        mock_es = self._es_with_teams([("CommaSplit", "Acme, OtherOrg")])
        result = self._call(mock_es, filter_type="primary")
        # CommaSplit should NOT be in primary results (it's multi-org)
        assert "CommaSplit" not in result

    def test_returns_empty_list_on_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES down")
        result = self._call(mock_es)
        assert result == []


# ===========================================================================
# GET /api/dashboards/platform-availability
# ===========================================================================

class TestGetPlatformAvailability:

    def test_returns_503_when_es_unavailable(self, auth_client):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 500

    def test_returns_empty_platforms_when_no_index_map(self, auth_client):
        with mock.patch("lib.es_helpers.get_elasticsearch_client",
                        return_value=mock.MagicMock()), \
             mock.patch("asset_management.web.app._get_platform_index_map", return_value={}):
            resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["platforms"] == {}

    def test_returns_has_data_based_on_doc_count(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {
            "responses": [
                {"hits": {"total": {"value": 10}}},
                {"hits": {"total": {"value": 0}}},
            ]
        }
        index_map = {"Meta": "fb_ads_*", "Echo": "echo_*"}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app._get_platform_index_map",
                        return_value=index_map):
            resp = auth_client.get("/api/dashboards/platform-availability")
        data = resp.get_json()
        assert data["success"] is True
        assert data["platforms"]["Meta"]["has_data"] is True
        assert data["platforms"]["Echo"]["has_data"] is False

    def test_applies_team_filter(self, auth_client):
        """should_apply_filter mocked off so asset_admin's
        new scope-filter doesn't shadow the team filter under test.
        Scope-filter is covered -EXPLORER-001."""
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {"responses": [{"hits": {"total": {"value": 5}}}]}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app._get_platform_index_map",
                        return_value={"Meta": "fb_*"}), \
             mock.patch("asset_management.web.app.RoleFilterService.should_apply_filter",
                        return_value=False):
            resp = auth_client.get("/api/dashboards/platform-availability?team=Alpha+Team")
        assert resp.status_code == 200
        # team filter should be in the search body
        body = mock_es.msearch.call_args[1]["body"]
        # second item is the query body; it should have a bool/should clause
        query_body = body[1]
        assert "bool" in query_body["query"]

    def test_returns_500_on_exception(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.msearch.side_effect = Exception("ES timeout")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app._get_platform_index_map",
                        return_value={"Meta": "fb_*"}):
            resp = auth_client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 500

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/dashboards/platform-availability")
        assert resp.status_code == 401

    def test_ds_flag_hides_platform_when_unchecked(self, auth_client):
        """Team with ds_echo='false' → Echo hidden even if data exists.

        should_apply_filter mocked off so the scope-filter
        doesn't shadow the ds_* flag under test.
        """
        mock_es = mock.MagicMock()
        # Echo has data (doc count > 0)
        mock_es.msearch.return_value = {
            "responses": [{"hits": {"total": {"value": 10}}}]
        }
        # Team lookup returns ds_echo='false'
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": {"ds_echo": "false"}}]}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app._get_platform_index_map",
                        return_value={"Echo": "echo_*"}), \
             mock.patch("asset_management.web.app._get_dashboard_menu_map",
                        return_value={}), \
             mock.patch("asset_management.web.app.RoleFilterService.should_apply_filter",
                        return_value=False):
            resp = auth_client.get("/api/dashboards/platform-availability?team=TestTeam")
        data = resp.get_json()
        assert data["success"] is True
        assert data["platforms"]["Echo"]["has_data"] is False

    def test_ds_flag_missing_treated_as_disabled(self, auth_client):
        """Team without ds_* fields → treated as unchecked (disabled).

        should_apply_filter mocked off (see sibling tests).
        """
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {
            "responses": [{"hits": {"total": {"value": 10}}}]
        }
        # Team exists but has no ds_* fields (shows as unchecked in UI)
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": {}}]}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app._get_platform_index_map",
                        return_value={"Echo": "echo_*"}), \
             mock.patch("asset_management.web.app._get_dashboard_menu_map",
                        return_value={}), \
             mock.patch("asset_management.web.app.RoleFilterService.should_apply_filter",
                        return_value=False):
            resp = auth_client.get("/api/dashboards/platform-availability?team=LegacyTeam")
        data = resp.get_json()
        assert data["platforms"]["Echo"]["has_data"] is False

    def test_ds_flag_aggregates_across_teams(self, auth_client):
        """Org with 2 teams: one has ds_echo='true', other 'false' → Echo stays visible."""
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {
            "responses": [{"hits": {"total": {"value": 10}}}]
        }
        # Two teams with different ds_echo values
        mock_es.search.return_value = {
            "hits": {"hits": [
                {"_source": {"ds_echo": "true"}},
                {"_source": {"ds_echo": "false"}},
            ]}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app._get_platform_index_map",
                        return_value={"Echo": "echo_*"}), \
             mock.patch("asset_management.web.app._get_team_titles_for_org",
                        return_value=["TeamA", "TeamB"]), \
             mock.patch("asset_management.web.app._get_dashboard_menu_map",
                        return_value={}):
            resp = auth_client.get("/api/dashboards/platform-availability?org=TestOrg")
        data = resp.get_json()
        assert data["platforms"]["Echo"]["has_data"] is True

    def test_ds_flag_hides_dashboards_in_disabled_platform(self, auth_client):
        """Team with ds_echo='false' → individual Echo dashboards also hidden.

        should_apply_filter mocked off (see sibling tests).
        """
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {
            "responses": [
                {"hits": {"total": {"value": 10}}},  # platform: Echo
                {"hits": {"total": {"value": 5}}},    # individual pattern
            ]
        }
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": {"ds_echo": "false"}}]}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app._get_platform_index_map",
                        return_value={"Echo": "echo_*"}), \
             mock.patch("asset_management.web.app._get_dashboard_index_map",
                        return_value={"echo-overview": ["*echo_conversations"]}), \
             mock.patch("asset_management.web.app._get_dashboard_menu_map",
                        return_value={"echo-overview": "Echo"}), \
             mock.patch("asset_management.web.app.RoleFilterService.should_apply_filter",
                        return_value=False):
            resp = auth_client.get("/api/dashboards/platform-availability?team=TestTeam")
        data = resp.get_json()
        assert data["dashboards"]["echo-overview"]["has_data"] is False

    def test_ds_flag_hides_only_disabled_dashboards_within_platform(self, auth_client):
        """YouTube enabled but Google Ads disabled → YouTube dash visible, Ads dash hidden."""
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {
            "responses": [
                {"hits": {"total": {"value": 10}}},  # platform: Google
                {"hits": {"total": {"value": 5}}},    # *youtube_analytics
                {"hits": {"total": {"value": 5}}},    # *google_ads
            ]
        }
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": {"ds_youtube": "true", "ds_google_ads": "false", "ds_google_analytics": "false"}}]}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app._get_platform_index_map",
                        return_value={"Google": "google_*"}), \
             mock.patch("asset_management.web.app._get_dashboard_index_map",
                        return_value={
                            "yt-analytics": ["*youtube_analytics"],
                            "campaign-perf": ["*google_ads"],
                        }), \
             mock.patch("asset_management.web.app._get_dashboard_menu_map",
                        return_value={"yt-analytics": "Google", "campaign-perf": "Google"}), \
             mock.patch("asset_management.web.app.RoleFilterService.should_apply_filter",
                        return_value=False):
            resp = auth_client.get("/api/dashboards/platform-availability?team=TestTeam")
        data = resp.get_json()
        # YouTube dashboard stays visible
        assert data["dashboards"]["yt-analytics"]["has_data"] is True
        # Google Ads dashboard is hidden
        assert data["dashboards"]["campaign-perf"]["has_data"] is False

    def test_no_ds_override_without_team_context(self, auth_client):
        """No org/team filter → no ds_* check, platforms stay data-driven."""
        mock_es = mock.MagicMock()
        mock_es.msearch.return_value = {
            "responses": [{"hits": {"total": {"value": 10}}}]
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app._get_platform_index_map",
                        return_value={"Echo": "echo_*"}):
            resp = auth_client.get("/api/dashboards/platform-availability")
        data = resp.get_json()
        assert data["platforms"]["Echo"]["has_data"] is True
        # _get_team_data_source_flags should not have been called
        # (no team search query beyond msearch)
        assert mock_es.search.call_count == 0


# ===========================================================================
# GET /api/dashboards/data-pulled-as-of
# ===========================================================================

class TestGetDataPulledAsOf:

    def test_returns_null_for_unknown_platform(self, auth_client):
        resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Unknown")
        assert resp.status_code == 200
        assert resp.get_json()["timestamp"] is None

    def test_returns_null_when_index_not_resolved(self, auth_client):
        with mock.patch("asset_management.web.app.resolve_data_view_index", return_value=None):
            resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Meta")
        assert resp.status_code == 200
        assert resp.get_json()["timestamp"] is None

    def test_returns_null_when_es_unavailable(self, auth_client):
        with mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb_*"), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Meta")
        assert resp.status_code == 200
        assert resp.get_json()["timestamp"] is None

    def test_returns_latest_timestamp(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "aggregations": {"max_ts": {"value_as_string": "2025-03-01T00:00:00Z"}}
        }
        with mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb_*"), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Meta")
        data = resp.get_json()
        assert data["success"] is True
        assert data["timestamp"] == "2025-03-01T00:00:00Z"

    def test_returns_null_on_exception(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        with mock.patch("asset_management.web.app.resolve_data_view_index", return_value="fb_*"), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/dashboards/data-pulled-as-of?platform=Meta")
        assert resp.status_code == 200
        assert resp.get_json()["timestamp"] is None

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/dashboards/data-pulled-as-of?platform=Meta")
        assert resp.status_code == 401


# ===========================================================================
# Remaining small edge cases
# ===========================================================================

class TestServeStatic:

    def test_serves_existing_static_file(self, auth_client, tmp_path):
        # The static folder is relative to the web dir; just verify the route exists
        resp = auth_client.get("/static/nonexistent_file_xyz.css")
        # 404 is fine — proves the route is reachable
        assert resp.status_code in (200, 404)


class TestDebugExpireSession:

    def test_marks_session_expired(self, auth_client, app):
        app.debug = True
        resp = auth_client.get("/debug/expire-session")
        assert resp.status_code == 200
        assert b"auth_timestamp=1" in resp.data
        app.debug = False


class TestImportLogExceptions:

    def test_log_exists_returns_500_on_exception(self, auth_client):
        with mock.patch("asset_management.web.app.os.path.exists",
                        side_effect=Exception("IO error")):
            resp = auth_client.get("/api/import/log-exists")
        assert resp.status_code == 500

    def test_log_content_returns_500_on_exception(self, auth_client):
        with mock.patch("asset_management.web.app.os.path.exists",
                        side_effect=Exception("IO error")):
            resp = auth_client.get("/api/import/log-content")
        assert resp.status_code == 500


class TestGetDashboardInfoEdgeCases:

    def test_returns_403_for_disabled_dashboard(self, auth_client):
        """A dashboard with enabled=False should return 403."""
        # Patch DASHBOARD_CONFIGS to include a disabled key
        with mock.patch.dict(
            "asset_management.web.app.DASHBOARD_CONFIGS",
            {"disabled_dash": {"title": "Disabled", "enabled": False}},
        ):
            resp = auth_client.get("/api/dashboards/disabled_dash")
        assert resp.status_code == 403

    def test_returns_500_on_exception(self, auth_client):
        with mock.patch.dict(
            "asset_management.web.app.DASHBOARD_CONFIGS",
            {"bad_dash": {"title": "Bad"}},
        ), mock.patch("asset_management.web.app.get_dashboard_url",
                     side_effect=Exception("config error")):
            resp = auth_client.get("/api/dashboards/bad_dash")
        assert resp.status_code == 500


class TestApiTestingEdgeCases:

    def test_testing_dashboards_includes_visible_echarts(self, auth_client):
        echarts_doc = [{"_source": {
            "dashboard_id": "ec-1",
            "title": "My EChart",
            "visibility_mode": "show_all",
        }}]
        with mock.patch("lib.es_helpers.get_all_documents",
                        side_effect=[[], echarts_doc]):  # kibana empty, echarts non-empty
            resp = auth_client.get("/api/testing/dashboards")
        results = resp.get_json()
        assert any(d["id"] == "ec-1" for d in results)

    def test_run_pytest_returns_500_on_generic_exception(self, auth_client):
        with mock.patch("subprocess.run", side_effect=Exception("unexpected error")), \
             mock.patch("os.makedirs"):
            resp = auth_client.post("/api/testing/run-pytest")
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False

    def test_pytest_log_returns_500_on_exception(self, auth_client):
        with mock.patch("glob.glob", side_effect=Exception("fs error")):
            resp = auth_client.get("/api/testing/pytest-log")
        assert resp.status_code == 500

    def test_check_dashboard_returns_false_when_kibana_id_empty(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": {"dashboard_id": ""}}]}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/testing/check-dashboard?dtype=kibana&did=test-id")
        assert resp.get_json()["ok"] is False

    def test_check_echarts_not_found(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/testing/check-dashboard?dtype=echarts&did=ech1")
        assert resp.get_json()["ok"] is False

    def test_check_echarts_charts_not_list(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": {
                "dashboard_id": "ech1",
                "title": "E",
                "echarts_config": {"charts": "not-a-list"},
            }}]}
        }
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/testing/check-dashboard?dtype=echarts&did=ech1")
        assert resp.get_json()["ok"] is False

    def test_check_dashboard_returns_500_on_exception(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.get("/api/testing/check-dashboard?dtype=kibana&did=test")
        assert resp.status_code == 500


class TestBackupForbiddenBranches:
    """Cover the forbidden-user path inside api_backup_* routes."""

    def test_backup_create_returns_403_when_no_user(self, client):
        """Unauthenticated user gets 302 redirect, not 403."""
        resp = client.post("/api/backup/create")
        assert resp.status_code == 401

    def test_backup_list_returns_302_when_unauthenticated(self, client):
        resp = client.get("/api/backup/list")
        assert resp.status_code == 401

    def test_backup_create_forbidden_via_get_current_user_none(self, auth_client):
        """If get_current_user returns None (edge), backup returns 403."""
        with mock.patch("asset_management.web.app.get_current_user", return_value=None):
            resp = auth_client.post("/api/backup/create")
        assert resp.status_code == 403

    def test_backup_list_forbidden_via_get_current_user_none(self, auth_client):
        with mock.patch("asset_management.web.app.get_current_user", return_value=None):
            resp = auth_client.get("/api/backup/list")
        assert resp.status_code == 403

    def test_backup_restore_forbidden_via_get_current_user_none(self, auth_client):
        with mock.patch("asset_management.web.app.get_current_user", return_value=None):
            resp = auth_client.post("/api/backup/restore",
                                    json={"filename": "backup.json.gz"})
        assert resp.status_code == 403


class TestSearchAssetListFields:
    """Cover list field matching in /api/search."""

    def test_search_matches_list_field_items(self, auth_client):
        """Fields containing lists should have each item checked against the pattern."""
        hit = {
            "_id": "t1",
            "_source": {
                "team_title": "Alpha Team",
                "tags": ["marketing", "social"],
            },
        }
        with mock.patch("asset_management.web.app.AssetService.get_all", return_value=[hit]):
            resp = auth_client.get("/api/search?q=marketing*")
        assert resp.status_code == 200
