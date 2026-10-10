"""
Regression tests for ``DashboardService._resolve_user_scope_esids``.

Historical context:

* reported the original bug: dashboards with
  ``visibility_mode: show_teams`` were hidden from everyone on the
  allowed list because role strings (``team_<title>``) were compared
  against ESIDs (de_asset_teams ``_id``). The fix translated role
  strings into ESIDs via a ``de_asset_teams`` match_phrase lookup.
* (Phase 4 cutover) moved scope resolution onto
  ``de_authz_user_team_role`` / ``de_authz_user_org_role``, keyed by
  ``session['username']``.
* (2026-05-12) flipped the ``USE_NEW_PERMISSIONS`` default to
  ``true`` (fail-closed).
* (2026-05-28) **retired the flag entirely** and deleted the
  legacy ``team_<title>`` / ``org_<title>`` regex path inside
  ``DashboardService`` along with ``_resolve_scope_from_legacy_roles``
  and ``_convert_name_to_esid``. The legacy unit + e2e test classes
  that used to live here have been removed because the code they
  exercised no longer exists.

What remains: the new-authz path tests below, which describe the
current behavior.
"""

from unittest import mock

import pytest

from asset_management.web.services.dashboard_service import DashboardService


class TestResolveUserScopeEsidsNewAuthz:
    """the resolver pulls membership from
    ``de_authz_user_team_role`` / ``de_authz_user_org_role`` keyed by
    ``session['username']``, ignoring ``user_roles``.

    Reproduces the 2026-05-12 prod outage: a ``team_user`` whose ES
    Security ``roles`` array is empty has been unable to see
    team-scoped dashboards since the 2026-05-07 cutover, even though
    their ``de_authz_user_team_role`` doc is correct. The legacy
    ``team_<title>`` regex (deleted) only ever matched when
    the role strings were actually present in the session.
    """

    def _mock_session(self, monkeypatch, *, username):
        """Replace ``flask.session`` (which ``dashboard_service`` reads
        directly under the new path) with a dict for the duration of one
        test."""
        monkeypatch.setattr(
            "asset_management.web.services.dashboard_service.session",
            {"username": username},
            raising=False,
        )

    def _mock_es_client_with_memberships(
        self, monkeypatch, *, team_esids=(), org_esids=()
    ):
        """Mock ``get_elasticsearch_client`` to return a fake ES that
        responds to the two membership-lookup queries the resolver
        issues. Returns the MagicMock for assertion."""
        es = mock.MagicMock()

        def _search(index, body=None, **_kw):
            if index == "de_authz_user_team_role":
                return {
                    "hits": {
                        "hits": [
                            {"_source": {"team_esid": esid}} for esid in team_esids
                        ]
                    }
                }
            if index == "de_authz_user_org_role":
                return {
                    "hits": {
                        "hits": [
                            {"_source": {"org_esid": esid}} for esid in org_esids
                        ]
                    }
                }
            return {"hits": {"hits": []}}

        es.search.side_effect = _search
        monkeypatch.setattr(
            "asset_management.web.services.dashboard_service.get_elasticsearch_client",
            lambda: es,
        )
        return es

    def test_resolves_team_membership_from_de_authz_index(self, monkeypatch):
        """The team_user prod repro: empty ``user_roles`` + a single
        ``de_authz_user_team_role`` doc → resolver returns the doc's
        ``team_esid``."""
        self._mock_session(monkeypatch, username="team_user")
        self._mock_es_client_with_memberships(
            monkeypatch, team_esids=["60_s4JsBIm2AdJKusvBg"]
        )

        team_esids, org_esids = DashboardService._resolve_user_scope_esids([])

        assert team_esids == {"60_s4JsBIm2AdJKusvBg"}
        assert org_esids == set()

    def test_resolves_org_membership_from_de_authz_index(self, monkeypatch):
        self._mock_session(monkeypatch, username="org_user")
        self._mock_es_client_with_memberships(
            monkeypatch, org_esids=["org_vogz_esid"]
        )

        team_esids, org_esids = DashboardService._resolve_user_scope_esids([])

        assert team_esids == set()
        assert org_esids == {"org_vogz_esid"}

    def test_resolves_both_team_and_org_memberships(self, monkeypatch):
        self._mock_session(monkeypatch, username="mixed_user")
        self._mock_es_client_with_memberships(
            monkeypatch,
            team_esids=["t1", "t2"],
            org_esids=["o1"],
        )

        team_esids, org_esids = DashboardService._resolve_user_scope_esids([])

        assert team_esids == {"t1", "t2"}
        assert org_esids == {"o1"}

    def test_ignores_legacy_role_strings_on_new_authz_path(self, monkeypatch):
        """Even if ``session['roles']`` still carries legacy ``team_<title>``
        strings (mixed-state environment), the new-authz path is the
        sole source of truth. The legacy regex MUST NOT run."""
        self._mock_session(monkeypatch, username="mixed_state_user")
        self._mock_es_client_with_memberships(
            monkeypatch, team_esids=["correct_esid"]
        )

        # If the legacy regex ran, it would try to resolve "team_Stale"
        # via de_asset_teams. Patch get_all_documents to blow up if
        # called — that proves the legacy path is dead under the flag.
        with mock.patch(
            "asset_management.web.services.dashboard_service.get_all_documents",
            side_effect=AssertionError("legacy de_asset_teams lookup must not run under USE_NEW_PERMISSIONS=true"),
        ):
            team_esids, _ = DashboardService._resolve_user_scope_esids(
                ["team_Stale", "team_AnotherStale"]
            )

        assert team_esids == {"correct_esid"}

    def test_empty_membership_returns_empty_sets(self, monkeypatch):
        """A user with no ``de_authz_user_*_role`` docs (e.g. wasn't
        backfilled) gets empty scope — same deny-by-default as the
        legacy path."""
        self._mock_session(monkeypatch, username="orphan_user")
        self._mock_es_client_with_memberships(monkeypatch)

        team_esids, org_esids = DashboardService._resolve_user_scope_esids([])

        assert team_esids == set()
        assert org_esids == set()

    def test_missing_session_username_returns_empty(self, monkeypatch):
        """Defensive: if the session somehow has no username (auth bug,
        unit test without proper setup), return empty rather than
        querying with empty key."""
        self._mock_session(monkeypatch, username="")
        # ES client must not be called when username is missing.
        es = self._mock_es_client_with_memberships(monkeypatch)

        team_esids, org_esids = DashboardService._resolve_user_scope_esids([])

        assert team_esids == set()
        assert org_esids == set()
        es.search.assert_not_called()


class TestGetVisibleEchartsDashboardsNewAuthzEndToEnd:
    """end-to-end: a team user with empty ``user_roles`` but a
    matching ``de_authz_user_team_role`` doc sees the team-scoped
    dashboard. This is the exact prod repro from 2026-05-12 that the
    fix must make pass.
    """

    @mock.patch("asset_management.web.services.dashboard_service.get_all_documents")
    def test_team_user_with_empty_roles_sees_team_dashboard(self, mock_get_all, monkeypatch):
        """Prod repro: team_user has roles=[], a de_authz_user_team_role
        doc for team esid 60_s4JsBIm2AdJKusvBg, and the dashboard
        requires that team esid. Before fix → dashboard hidden.
        After fix → dashboard visible."""
        monkeypatch.setattr(
            "asset_management.web.services.dashboard_service.session",
            {"username": "team_user"},
            raising=False,
        )

        # ES Security has no team_<title> in roles; new-authz model
        # has the membership.
        es = mock.MagicMock()
        def _es_search(index, body=None, **_kw):
            if index == "de_authz_user_team_role":
                return {"hits": {"hits": [
                    {"_source": {"team_esid": "60_s4JsBIm2AdJKusvBg"}}
                ]}}
            if index == "de_authz_user_org_role":
                return {"hits": {"hits": []}}
            return {"hits": {"hits": []}}
        es.search.side_effect = _es_search
        monkeypatch.setattr(
            "asset_management.web.services.dashboard_service.get_elasticsearch_client",
            lambda: es,
        )

        # get_all_documents only sees the dashboards-index lookup —
        # NOT de_asset_teams (the new path doesn't title-resolve).
        def _docs(index, query=None):
            if index == "de_asset_echarts_dashboards":
                return [{"_id": "d1", "_source": {
                    "dashboard_type": "echarts",
                    "dashboard_id": "d1",
                    "title": "Ad Sets (Delivery)",
                    "visibility_mode": "show_teams",
                    "allowed_team_esids": ["60_s4JsBIm2AdJKusvBg"],
                    "allowed_org_esids": [],
                }}]
            raise AssertionError(f"unexpected get_all_documents on {index!r} under new-authz path")
        mock_get_all.side_effect = _docs

        # User logs in with NO legacy team_<title> in session roles.
        result = DashboardService.get_visible_echarts_dashboards(user_roles=[])

        assert [d["id"] for d in result] == ["d1"], (
            "team_user with valid de_authz_user_team_role membership must see "
            "the team-scoped dashboard"
        )
