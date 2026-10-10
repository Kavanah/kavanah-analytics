"""map-data scope bypass regression tests.

Before the fix, ``GET /dashboard-builder/api/map-data`` and
``GET /dashboard-builder/api/map-data/cities`` accepted client-supplied
``team`` / ``org`` query params and passed them straight to the ES filter
with no validation that the requested team/org was in the session's
allowed scope. A scoped (non-admin) dashboard user could probe any
team's map activity by hand-crafting the request.

After the fix (mirrors the pattern on platform-availability):

  * Scoped users (``RoleFilterService.get_allowed_teams_for_org_users``
    returns a list) can only query within their allowed team set.
    Client-supplied teams outside that set are silently dropped; if the
    remaining list is empty the request falls back to the user's full
    allowed set. The ``org`` param is ignored for scoped users since it
    would re-expand scope via ``_get_team_titles_for_org``.
  * Admins / unrestricted users (``get_allowed_teams_for_org_users``
    returns ``None``) keep their pre-existing behaviour — they can query
    any team via the dropdown.
"""

import time
from unittest import mock

import pytest

from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient


MAP_PATH = "/dashboard-builder/api/map-data"
CITIES_PATH = "/dashboard-builder/api/map-data/cities"

_ROUTES_MOD = "asset_management.web.blueprints.dashboard_builder.routes"


# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------


def _seed(sess, *, roles, is_admin=False):
    sess["authenticated"] = True
    sess["username"] = "testuser"
    sess["roles"] = list(roles)
    sess["is_admin"] = is_admin
    sess["full_name"] = "Test User"
    sess["email"] = "test@example.com"
    sess["auth_timestamp"] = time.time()
    sess["csrf_token"] = CSRF_TEST_TOKEN


@pytest.fixture
def scoped_team_a(app):
    """Scoped user whose only allowed team is 'Team A'."""
    c = app.test_client()
    with c.session_transaction() as sess:
        _seed(sess, roles=["team_Team A"])
    return CsrfTestClient(c)


# ---------------------------------------------------------------------------
# Helpers — patch the FB query helpers so we can read off (org, teams) the
# endpoint actually queried with. Both routes fan into FB-insights helpers
# when ``statistic=reach``.
# ---------------------------------------------------------------------------


def _run_map_data(client, url, allowed_teams):
    """Hit ``/api/map-data`` and return (response, captured_kwargs).

    ``captured_kwargs`` is a dict of {func_name: {"org": ..., "teams": ...}}
    populated by the FB query helpers — so tests can assert on the scope
    that actually reached the query layer.
    """
    captured: dict[str, dict] = {}

    def _capture(name):
        def _impl(es, stat_cfg, org, teams, date_from, date_to):
            captured[name] = {"org": org, "teams": list(teams)}
            return []
        return _impl

    with mock.patch(
        f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
        return_value=allowed_teams,
    ), mock.patch(
        f"{_ROUTES_MOD}.get_elasticsearch_client",
        return_value=mock.MagicMock(),
    ), mock.patch(
        f"{_ROUTES_MOD}._query_fb_region_map", side_effect=_capture("region"),
    ), mock.patch(
        f"{_ROUTES_MOD}._query_fb_country_map", side_effect=_capture("country"),
    ):
        resp = client.get(url)
    return resp, captured


def _run_map_cities(client, url, allowed_teams):
    """Hit ``/api/map-data/cities`` and return (response, captured)."""
    captured: dict[str, dict] = {}

    def _impl(es, stat_cfg, org, teams, date_from, date_to):
        captured["cached"] = {"org": org, "teams": list(teams)}
        return ([], 0, 0)

    with mock.patch(
        f"{_ROUTES_MOD}.RoleFilterService.get_allowed_teams_for_org_users",
        return_value=allowed_teams,
    ), mock.patch(
        f"{_ROUTES_MOD}.get_elasticsearch_client",
        return_value=mock.MagicMock(),
    ), mock.patch(
        f"{_ROUTES_MOD}._query_fb_map_cached_only", side_effect=_impl,
    ):
        resp = client.get(url)
    return resp, captured


# ===========================================================================
# /api/map-data — scope enforcement
# ===========================================================================


class TestMapDataScopedUserCannotOverrideScope:
    """A user scoped to Team A cannot probe Team B / Team C via ``?team=``."""

    def test_should_drop_forbidden_team_and_fall_back_to_allowed(self, scoped_team_a):
        resp, captured = _run_map_data(
            scoped_team_a,
            f"{MAP_PATH}?statistic=reach&team=Team B",
            allowed_teams=["Team A"],
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert captured["region"]["teams"] == ["Team A"]
        assert captured["country"]["teams"] == ["Team A"]

    def test_should_keep_team_when_user_is_on_it(self, scoped_team_a):
        resp, captured = _run_map_data(
            scoped_team_a,
            f"{MAP_PATH}?statistic=reach&team=Team A",
            allowed_teams=["Team A"],
        )
        assert resp.status_code == 200
        assert captured["region"]["teams"] == ["Team A"]
        assert captured["country"]["teams"] == ["Team A"]

    def test_should_fall_back_to_allowed_on_arbitrary_team(self, scoped_team_a):
        """Nonexistent team name doesn't let the client probe team existence."""
        resp, captured = _run_map_data(
            scoped_team_a,
            f"{MAP_PATH}?statistic=reach&team=DOES_NOT_EXIST_xyz",
            allowed_teams=["Team A"],
        )
        assert resp.status_code == 200
        assert captured["region"]["teams"] == ["Team A"]

    def test_should_intersect_when_mixed_allowed_and_forbidden(self, scoped_team_a):
        """User on [A, B] passing ?team=A&team=C gets scoped to A only."""
        resp, captured = _run_map_data(
            scoped_team_a,
            f"{MAP_PATH}?statistic=reach&team=Team A&team=Team C",
            allowed_teams=["Team A", "Team B"],
        )
        assert resp.status_code == 200
        assert captured["region"]["teams"] == ["Team A"]

    def test_should_drop_org_filter_for_scoped_users(self, scoped_team_a):
        """``?org=<other>`` must not re-expand scope via the lookup table."""
        resp, captured = _run_map_data(
            scoped_team_a,
            f"{MAP_PATH}?statistic=reach&org=CRU",
            allowed_teams=["Team A"],
        )
        assert resp.status_code == 200
        assert captured["region"]["org"] == ""
        assert captured["region"]["teams"] == ["Team A"]


class TestMapDataAdminIsUnrestricted:
    """Admin / unrestricted users keep the existing dropdown behaviour."""

    def test_should_honour_team_param_for_admin(self, auth_client):
        resp, captured = _run_map_data(
            auth_client,
            f"{MAP_PATH}?statistic=reach&team=Any Team",
            allowed_teams=None,
        )
        assert resp.status_code == 200
        assert captured["region"]["teams"] == ["Any Team"]

    def test_should_honour_org_param_for_admin(self, auth_client):
        resp, captured = _run_map_data(
            auth_client,
            f"{MAP_PATH}?statistic=reach&org=AnyOrg",
            allowed_teams=None,
        )
        assert resp.status_code == 200
        assert captured["region"]["org"] == "AnyOrg"


# ===========================================================================
# /api/map-data/cities — scope enforcement (same shape, separate guard)
# ===========================================================================


class TestMapCitiesScopedUserCannotOverrideScope:
    """The cities sub-route shares ``_org_team_filters`` with map-data, so
    a hand-crafted request to it must hit the same guard."""

    def test_should_drop_forbidden_team_and_fall_back_to_allowed(self, scoped_team_a):
        resp, captured = _run_map_cities(
            scoped_team_a,
            f"{CITIES_PATH}?statistic=reach&team=Team B",
            allowed_teams=["Team A"],
        )
        assert resp.status_code == 200
        assert captured["cached"]["teams"] == ["Team A"]

    def test_should_drop_org_filter_for_scoped_users(self, scoped_team_a):
        resp, captured = _run_map_cities(
            scoped_team_a,
            f"{CITIES_PATH}?statistic=reach&org=CRU",
            allowed_teams=["Team A"],
        )
        assert resp.status_code == 200
        assert captured["cached"]["org"] == ""
        assert captured["cached"]["teams"] == ["Team A"]


class TestMapCitiesAdminIsUnrestricted:
    def test_should_honour_team_param_for_admin(self, auth_client):
        resp, captured = _run_map_cities(
            auth_client,
            f"{CITIES_PATH}?statistic=reach&team=Any Team",
            allowed_teams=None,
        )
        assert resp.status_code == 200
        assert captured["cached"]["teams"] == ["Any Team"]
