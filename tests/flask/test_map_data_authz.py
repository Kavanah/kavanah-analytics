"""
single-team users get 403 on /api/map-data/cities (Maps city dots missing).

The auth.py route-level whitelist exempts ``/dashboard-builder/api/map-data``
from the ``REQUIRED_ROLE`` check using an exact-string match. split
the FB cities second-paint into a separate route at
``/dashboard-builder/api/map-data/cities`` but did not extend the whitelist,
so single-team and other non-admin dashboard users fall through to the role
gate and get 403'd. Country shading still renders (the bare /map-data is
whitelisted) but city dots never load.

These tests pin the whitelist for both the bare and the cities sub-route
against a non-admin team user.
"""

import time
from unittest import mock

import pytest

from tests.conftest import CSRF_TEST_TOKEN


def _seed_team_user(sess):
    sess["authenticated"] = True
    sess["username"] = "team_a_user"
    sess["roles"] = ["team_A_esid"]
    sess["is_admin"] = False
    sess["full_name"] = "Team A User"
    sess["email"] = "team_a@example.com"
    sess["auth_timestamp"] = time.time()
    sess["csrf_token"] = CSRF_TEST_TOKEN


@pytest.fixture
def team_a_client(app):
    """Authenticated single-team user, no admin / builder roles."""
    c = app.test_client()
    with c.session_transaction() as sess:
        _seed_team_user(sess)
    return c


_ROUTES = "asset_management.web.blueprints.dashboard_builder.routes"


class TestMapDataWhitelistedForTeamUsers:
    """Both /api/map-data and /api/map-data/cities must reach the route handler
    for non-admin team users. The auth gate (auth.py REQUIRED_ROLE check) must
    not 403 either path."""

    def test_should_not_403_team_user_on_bare_map_data(self, team_a_client):
        with mock.patch(f"{_ROUTES}.get_elasticsearch_client", return_value=mock.MagicMock()), \
             mock.patch(f"{_ROUTES}._query_fb_region_map", return_value=[]), \
             mock.patch(f"{_ROUTES}._query_fb_country_map", return_value=[]):
            resp = team_a_client.get(
                "/dashboard-builder/api/map-data?statistic=reach"
            )
        assert resp.status_code != 403, resp.data
        assert resp.status_code == 200

    def test_should_not_403_team_user_on_cities_subroute(self, team_a_client):
        """regression: this returned 403 before the auth.py whitelist
        was extended to cover the /map-data/ sub-tree."""
        with mock.patch(f"{_ROUTES}.get_elasticsearch_client", return_value=mock.MagicMock()), \
             mock.patch(f"{_ROUTES}._query_fb_map_cached_only", return_value=([], 0, 0)):
            resp = team_a_client.get(
                "/dashboard-builder/api/map-data/cities?statistic=reach"
            )
        assert resp.status_code != 403, resp.data
        assert resp.status_code == 200

    def test_should_403_team_user_on_unrelated_admin_path(self, team_a_client):
        """Sanity check: the whitelist is targeted, not a blanket bypass.
        A non-admin team user must still be 403'd on admin-only routes."""
        resp = team_a_client.get("/user-management")
        # Either 403 from the role abort, or 302 redirect to /login —
        # both are "not allowed in", which is the negative side of the
        # whitelist we're pinning above.
        assert resp.status_code in (302, 403)
