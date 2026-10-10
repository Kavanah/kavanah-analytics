"""query backplane scope-bypass regression tests.

Originally written as xfail-strict probes demonstrating three
scope-bypass paths reachable by any authenticated team_*-scoped user.
 fixed all three paths via two helpers in ``RoleFilterService``:

  * ``intersect_body_filters(body, allowed_teams)`` — strips
    scope-defining filter values outside the user's allowed set.
  * ``append_scope_filter(body, allowed_teams)`` — appends an allow-only
    ``team_title`` clause so unfiltered queries are also pinned.

These tests now pass and serve as the regression watchdog for the fix.

Coverage:

  1. ``POST /api/query`` — body's ``filters`` array is intersected
     against allowed teams; an allow-only clause is appended.
  2. ``POST /api/trend-line-data`` — same fix.
  3. ``GET /api/dashboards/org-team-options`` — response teams/orgs
     are filtered to the scoped user's allowed set.

Admins (``get_allowed_teams_for_org_users`` returns ``None``) are
unaffected by all three fixes — see ``test_admin_path.py`` (or
the supporting tests below) for that path.
"""

import json
import time
from unittest import mock

import pytest

from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient

_ROUTES_MOD = "asset_management.web.blueprints.dashboard_builder.routes"

QUERY_PATH = "/dashboard-builder/api/query"
TREND_PATH = "/dashboard-builder/api/trend-line-data"
ORG_TEAM_PATH = "/dashboard-builder/api/dashboards/org-team-options"


# ---------------------------------------------------------------------------
# Session fixture
# ---------------------------------------------------------------------------


def _seed(sess, *, roles):
    sess["authenticated"] = True
    sess["username"] = "kavanah_demo_test"
    sess["roles"] = list(roles)
    sess["is_admin"] = False
    sess["full_name"] = "Demo Test User"
    sess["email"] = "demo-test@example.com"
    sess["auth_timestamp"] = time.time()
    sess["csrf_token"] = CSRF_TEST_TOKEN


@pytest.fixture
def scoped_kavanah(app):
    """Mirrors kavanah_demo on staging: scoped to one team, no admin role."""
    c = app.test_client()
    with c.session_transaction() as sess:
        _seed(sess, roles=["team_Kavanah Team"])
    return CsrfTestClient(c)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_es_search():
    """Return (mock_es, captured_calls) where captured_calls is a list of
    (index, body) tuples for every es.search() call routed through the mock.
    """
    captured = []

    def _impl(index=None, body=None, **kwargs):
        captured.append({"index": index, "body": body})
        # Return an empty-but-shaped ES response so the route doesn't crash
        # before we can inspect what it queried with.
        return {
            "hits": {"total": {"value": 0}, "hits": []},
            "aggregations": {},
        }

    es = mock.MagicMock()
    es.search.side_effect = _impl
    return es, captured


def _filter_values_in_body(body, target_value):
    """Return True if the malicious team_title value appears anywhere in the
    ES query body. We don't need to know the exact JSON path — any presence
    proves the filter reached ES untouched.
    """
    return target_value in json.dumps(body)


# ===========================================================================
# /api/query — body-supplied filter reaches ES untouched
# ===========================================================================


class TestApiQueryAllowsScopeBypassViaBodyFilters:
    """A user scoped to Kavanah Team can POST a /api/query body whose
    ``filters`` array references another team's ``team_title``, and the
    server passes that filter straight to ES.

    Demonstrates the bypass: today the malicious filter reaches ES; the
    desired behaviour is that it gets intersected against the user's
    allowed-team set (or stripped, or rejected with 403)."""

    def test_should_strip_forbidden_team_title_filter_from_body(self, scoped_kavanah):
        es, captured = _capture_es_search()

        config = {
            "data_view": "disciple-tools",
            "chart_type": "bar",
            "x_field": "team_title.keyword",
            "y_fields": ["count"],
            "aggregation": "count",
            "filters": [
                # Malicious: scoped user references a team they cannot see.
                {"field": "team_title", "op": "eq", "value": "Other Forbidden Team"}
            ],
        }

        with (
            mock.patch(f"{_ROUTES_MOD}.get_elasticsearch_client", return_value=es),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=["Kavanah Team"],
            ),
            mock.patch(f"{_ROUTES_MOD}._get_field_types", return_value={}),
            mock.patch(f"{_ROUTES_MOD}._require_index_access"),
        ):
            resp = scoped_kavanah.post(
                QUERY_PATH,
                json=config,
            )

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert len(captured) >= 1, "Expected at least one es.search call"
        # PASS = malicious value was stripped before reaching ES.
        assert not _filter_values_in_body(
            captured[0]["body"], "Other Forbidden Team"
        ), "Malicious team_title filter reached ES. Body: " + json.dumps(captured[0]["body"])


# ===========================================================================
# /api/trend-line-data — body-supplied filter reaches ES untouched
# ===========================================================================


class TestApiTrendLineDataAllowsScopeBypassViaBodyFilters:
    """Same bypass shape as /api/query. body['filters'] passes through
    ``_build_filters`` to ES with no scope intersect."""

    def test_should_strip_forbidden_team_title_filter_from_body(self, scoped_kavanah):
        es, captured = _capture_es_search()

        body = {
            "sources": [
                {
                    "label": "DT contacts",
                    "data_view": "disciple-tools",
                    "field": None,
                    "date_field": "kibana_date",
                }
            ],
            "filters": [{"field": "team_title", "op": "eq", "value": "Other Forbidden Team"}],
            "date_interval": "month",
        }

        with (
            mock.patch(f"{_ROUTES_MOD}.get_elasticsearch_client", return_value=es),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=["Kavanah Team"],
            ),
            mock.patch(f"{_ROUTES_MOD}._require_index_access"),
        ):
            resp = scoped_kavanah.post(
                TREND_PATH,
                json=body,
            )

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert len(captured) >= 1
        assert not _filter_values_in_body(
            captured[0]["body"], "Other Forbidden Team"
        ), "Malicious team_title filter reached ES. Body: " + json.dumps(captured[0]["body"])


# ===========================================================================
# /api/dashboards/org-team-options — full enumeration leak
# ===========================================================================


class TestOrgTeamOptionsLeaksFullEnumeration:
    """A scoped user GETs /api/dashboards/org-team-options and gets back
    every organization and team in the system, not just the ones in their
    allowed scope.

    Today this is a real enumeration leak — the demo account would see
    other customers' org/team names in any UI dropdown driven by this
    endpoint. The fix should filter the response by
    ``RoleFilterService.get_allowed_teams_for_org_users`` for scoped users."""

    def test_scoped_user_should_only_see_their_allowed_teams(self, scoped_kavanah):
        # Three orgs in the system, only one of which the scoped user belongs to.
        org_hits = [
            {"_id": "org1", "_source": {"organization_title": "Kavanah"}},
            {"_id": "org2", "_source": {"organization_title": "Acme Corp"}},
            {"_id": "org3", "_source": {"organization_title": "Widget Inc"}},
        ]
        # Five teams in the system, only one the scoped user can see.
        # each team now carries workspace_esid so the picker's
        # workspace-scope filter can enforce the tenant boundary. The
        # foreign teams live in other workspaces; "Kavanah Team" is in the
        # scoped user's own workspace.
        team_hits = [
            {"_id": "t1", "_source": {"team_title": "Kavanah Team", "workspace_esid": "kavanah-workspace"}},
            {"_id": "t2", "_source": {"team_title": "Acme Sales", "workspace_esid": "acme-workspace"}},
            {"_id": "t3", "_source": {"team_title": "Acme Engineering", "workspace_esid": "acme-workspace"}},
            {"_id": "t4", "_source": {"team_title": "Widget North", "workspace_esid": "widget-workspace"}},
            {"_id": "t5", "_source": {"team_title": "Widget South", "workspace_esid": "widget-workspace"}},
        ]

        with (
            mock.patch(
                f"{_ROUTES_MOD}.get_all_documents",
                side_effect=lambda idx: org_hits if idx == "de_asset_organization" else team_hits,
            ),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=["Kavanah Team"],
            ),
            # the scoped user is in kavanah-workspace only.
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_workspaces",
                return_value=["kavanah-workspace"],
            ),
        ):
            resp = scoped_kavanah.get(ORG_TEAM_PATH)

        assert resp.status_code == 200
        data = resp.get_json()
        team_titles_in_resp = {t["title"] for t in data.get("teams", [])}
        # PASS = response is filtered to the scoped user's allowed teams only.
        assert team_titles_in_resp == {"Kavanah Team"}, "Scoped user got back teams outside their allowed set: " + str(
            team_titles_in_resp
        )


# ===========================================================================
# supporting tests — admin path + legitimate scoped queries +
# allow-only append confirmation
# ===========================================================================


@pytest.fixture
def admin_client(app):
    """Admin (asset_admin) — RoleFilterService returns None for allowed_teams."""
    c = app.test_client()
    with c.session_transaction() as sess:
        _seed(sess, roles=["asset_admin"])
    return CsrfTestClient(c)


class TestApiQueryAdminAndScopedSupporting:
    """Confirms the fix doesn't break admin behaviour and that scoped users
    with legitimate queries still get their allowed-team data."""

    def test_admin_keeps_arbitrary_team_title_filter(self, admin_client):
        """Admin path: allowed_teams is None → no intersect, no append."""
        es, captured = _capture_es_search()

        config = {
            "data_view": "disciple-tools",
            "chart_type": "bar",
            "x_field": "team_title.keyword",
            "y_fields": ["count"],
            "aggregation": "count",
            "filters": [
                {"field": "team_title", "op": "eq", "value": "Any Team"},
            ],
        }

        with (
            mock.patch(f"{_ROUTES_MOD}.get_elasticsearch_client", return_value=es),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=None,
            ),
            mock.patch(f"{_ROUTES_MOD}._get_field_types", return_value={}),
            mock.patch(f"{_ROUTES_MOD}._require_index_access"),
        ):
            resp = admin_client.post(QUERY_PATH, json=config)

        assert resp.status_code == 200
        # Admin's filter reaches ES untouched.
        assert _filter_values_in_body(captured[0]["body"], "Any Team")

    def test_scoped_keeps_allowed_team_filter(self, scoped_kavanah):
        """Scoped user passing an allowed team_title keeps the filter."""
        es, captured = _capture_es_search()

        config = {
            "data_view": "disciple-tools",
            "chart_type": "bar",
            "x_field": "team_title.keyword",
            "y_fields": ["count"],
            "aggregation": "count",
            "filters": [
                {"field": "team_title", "op": "eq", "value": "Kavanah Team"},
            ],
        }

        with (
            mock.patch(f"{_ROUTES_MOD}.get_elasticsearch_client", return_value=es),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=["Kavanah Team"],
            ),
            mock.patch(f"{_ROUTES_MOD}._get_field_types", return_value={}),
            mock.patch(f"{_ROUTES_MOD}._require_index_access"),
        ):
            resp = scoped_kavanah.post(QUERY_PATH, json=config)

        assert resp.status_code == 200
        # Allowed value reached ES.
        assert _filter_values_in_body(captured[0]["body"], "Kavanah Team")

    def test_scoped_unfiltered_query_gets_allow_only_clause(self, scoped_kavanah):
        """Scoped user with NO filters still gets pinned to their allowed
        teams via the appended allow-only clause."""
        es, captured = _capture_es_search()

        config = {
            "data_view": "disciple-tools",
            "chart_type": "bar",
            "x_field": "team_title.keyword",
            "y_fields": ["count"],
            "aggregation": "count",
            # No filters at all.
        }

        with (
            mock.patch(f"{_ROUTES_MOD}.get_elasticsearch_client", return_value=es),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=["Kavanah Team"],
            ),
            mock.patch(f"{_ROUTES_MOD}._get_field_types", return_value={}),
            mock.patch(f"{_ROUTES_MOD}._require_index_access"),
        ):
            resp = scoped_kavanah.post(QUERY_PATH, json=config)

        assert resp.status_code == 200
        # ES body must contain "Kavanah Team" — proof the allow-only clause
        # was appended even though the original config had no filters.
        assert _filter_values_in_body(captured[0]["body"], "Kavanah Team")


class TestApiTrendLineDataAdminAndScopedSupporting:
    """Same admin/scoped/allow-only matrix for /api/trend-line-data."""

    def test_admin_keeps_arbitrary_team_title_filter(self, admin_client):
        es, captured = _capture_es_search()

        body = {
            "sources": [{"label": "DT", "data_view": "disciple-tools", "date_field": "kibana_date"}],
            "filters": [
                {"field": "team_title", "op": "eq", "value": "Any Team"},
            ],
        }

        with (
            mock.patch(f"{_ROUTES_MOD}.get_elasticsearch_client", return_value=es),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=None,
            ),
            mock.patch(f"{_ROUTES_MOD}._require_index_access"),
        ):
            resp = admin_client.post(TREND_PATH, json=body)

        assert resp.status_code == 200
        assert _filter_values_in_body(captured[0]["body"], "Any Team")

    def test_scoped_keeps_allowed_team_filter(self, scoped_kavanah):
        es, captured = _capture_es_search()

        body = {
            "sources": [{"label": "DT", "data_view": "disciple-tools", "date_field": "kibana_date"}],
            "filters": [
                {"field": "team_title", "op": "eq", "value": "Kavanah Team"},
            ],
        }

        with (
            mock.patch(f"{_ROUTES_MOD}.get_elasticsearch_client", return_value=es),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=["Kavanah Team"],
            ),
            mock.patch(f"{_ROUTES_MOD}._require_index_access"),
        ):
            resp = scoped_kavanah.post(TREND_PATH, json=body)

        assert resp.status_code == 200
        assert _filter_values_in_body(captured[0]["body"], "Kavanah Team")

    def test_scoped_unfiltered_query_gets_allow_only_clause(self, scoped_kavanah):
        es, captured = _capture_es_search()

        body = {
            "sources": [{"label": "DT", "data_view": "disciple-tools", "date_field": "kibana_date"}],
            # No filters at all.
        }

        with (
            mock.patch(f"{_ROUTES_MOD}.get_elasticsearch_client", return_value=es),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=["Kavanah Team"],
            ),
            mock.patch(f"{_ROUTES_MOD}._require_index_access"),
        ):
            resp = scoped_kavanah.post(TREND_PATH, json=body)

        assert resp.status_code == 200
        assert _filter_values_in_body(captured[0]["body"], "Kavanah Team")


class TestOrgTeamOptionsAdminAndScopedSupporting:
    """Admin sees full enumeration; scoped user sees their teams + parent
    orgs of those teams (org filter via team→org join when
    get_allowed_organizations returns nothing)."""

    _ORG_HITS = [
        {"_id": "org1", "_source": {"organization_title": "Kavanah"}},
        {"_id": "org2", "_source": {"organization_title": "Acme Corp"}},
        {"_id": "org3", "_source": {"organization_title": "Widget Inc"}},
    ]
    # workspace_esid added so the picker's workspace-scope filter
    # has a tenant boundary to enforce. Kavanah Team is in kavanah-workspace;
    # the Acme / Widget teams are in their own workspaces.
    _TEAM_HITS = [
        {"_id": "t1", "_source": {"team_title": "Kavanah Team", "organization_title": "Kavanah", "workspace_esid": "kavanah-workspace"}},
        {"_id": "t2", "_source": {"team_title": "Acme Sales", "organization_title": "Acme Corp", "workspace_esid": "acme-workspace"}},
        {"_id": "t3", "_source": {"team_title": "Acme Engineering", "organization_title": "Acme Corp", "workspace_esid": "acme-workspace"}},
        {"_id": "t4", "_source": {"team_title": "Widget North", "organization_title": "Widget Inc", "workspace_esid": "widget-workspace"}},
        {"_id": "t5", "_source": {"team_title": "Widget South", "organization_title": "Widget Inc", "workspace_esid": "widget-workspace"}},
    ]

    def _patch_get_all_documents(self):
        return mock.patch(
            f"{_ROUTES_MOD}.get_all_documents",
            side_effect=lambda idx: self._ORG_HITS if idx == "de_asset_organization" else self._TEAM_HITS,
        )

    def test_admin_sees_full_enumeration(self, admin_client):
        with (
            self._patch_get_all_documents(),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=None,
            ),
            # admin override — no workspace filter (None) — must
            # still see every team.
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_workspaces",
                return_value=None,
            ),
        ):
            resp = admin_client.get(ORG_TEAM_PATH)

        assert resp.status_code == 200
        data = resp.get_json()
        team_titles = {t["title"] for t in data["teams"]}
        org_titles = {o["title"] for o in data["organizations"]}
        assert team_titles == {"Kavanah Team", "Acme Sales", "Acme Engineering", "Widget North", "Widget South"}
        assert org_titles == {"Kavanah", "Acme Corp", "Widget Inc"}

    def test_team_only_user_sees_no_orgs(self, scoped_kavanah):
        """follow-up (2026-06-02): orgs appear in the visibility picker
        ONLY when the user holds an org/workspace role. A team-only user (e.g. a
        team admin) sees their teams but NO orgs — the previous "parent org of
        my allowed team via the team→org join" fallback is removed (a team
        admin's authority is the team, not the org).

        Mutation: restore the parent-org team-join fallback → org_titles becomes
        {"Kavanah"} and this assertion fails."""
        with (
            self._patch_get_all_documents(),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=["Kavanah Team"],
            ),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_organizations",
                return_value=None,  # team_* role only, no org_* role
            ),
            # scoped to kavanah-workspace.
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_workspaces",
                return_value=["kavanah-workspace"],
            ),
        ):
            resp = scoped_kavanah.get(ORG_TEAM_PATH)

        assert resp.status_code == 200
        data = resp.get_json()
        team_titles = {t["title"] for t in data["teams"]}
        org_titles = {o["title"] for o in data["organizations"]}
        assert team_titles == {"Kavanah Team"}
        # A team-only user gets NO orgs in the picker.
        assert org_titles == set(), f"team-only user must see no orgs, got {org_titles}"

    def test_scoped_user_with_explicit_org_filter(self, scoped_kavanah):
        """get_allowed_organizations returns a list — that list is used."""
        with (
            self._patch_get_all_documents(),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
                return_value=["Kavanah Team"],
            ),
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_organizations",
                return_value=["Kavanah"],
            ),
            # scoped to kavanah-workspace.
            mock.patch(
                f"{_ROUTES_MOD}.RoleFilterService.get_allowed_workspaces",
                return_value=["kavanah-workspace"],
            ),
        ):
            resp = scoped_kavanah.get(ORG_TEAM_PATH)

        assert resp.status_code == 200
        data = resp.get_json()
        org_titles = {o["title"] for o in data["organizations"]}
        assert org_titles == {"Kavanah"}
