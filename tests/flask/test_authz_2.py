"""
platform-availability scope bypass regression tests.

Before the fix, ``GET /api/dashboards/platform-availability`` used
client-supplied ``team`` / ``org`` query params directly as the scope,
letting a scoped user probe any team's dashboard activity by passing
``?team=<any_team>``.

After the fix:

  * Scoped users (``RoleFilterService.get_allowed_teams_for_org_users``
    returns a list) can only query within their allowed team set.
    Client-supplied teams outside that set are silently dropped; if the
    remaining list is empty the request falls back to the user's full
    allowed set. The ``org`` param is ignored for scoped users since it
    would re-expand scope via the lookup table.
  * Admins / unrestricted users (``get_allowed_teams_for_org_users``
    returns ``None``) keep their pre-existing behaviour — they can query
    any team via the dropdown.
  * The response no longer includes ``debug_teams`` (no frontend
    consumer, and it used to confirm the bypass).
"""

import time
from unittest import mock

import pytest

from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient


PATH = "/api/dashboards/platform-availability"


# ---------------------------------------------------------------------------
# Session fixtures — scoped users (not admins)
# ---------------------------------------------------------------------------


def _seed(sess, *, roles):
    sess["authenticated"] = True
    sess["username"] = "testuser"
    sess["roles"] = list(roles)
    sess["is_admin"] = False
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
# Shared mocks — the endpoint calls msearch once and returns platforms +
# dashboards. We care about what team_titles made it into the query body.
# ---------------------------------------------------------------------------


def _run_endpoint(client, url, allowed_teams, captured_bodies):
    """Execute the endpoint with enough mocks to reach the query-body stage.

    ``allowed_teams`` is what ``get_allowed_teams_for_org_users`` should
    return for this request (``None`` = admin / unrestricted).
    ``captured_bodies`` is populated with the list of query-body dicts
    passed to ``msearch`` so tests can inspect the effective scope.
    """
    mock_es = mock.MagicMock()
    mock_es.msearch.return_value = {"responses": [{"hits": {"total": {"value": 0}}}]}

    def _capture(*, body):
        captured_bodies.extend(body)
        return {"responses": [{"hits": {"total": {"value": 0}}}] * (len(body) // 2)}

    mock_es.msearch.side_effect = _capture

    with mock.patch(
        "asset_management.web.app.RoleFilterService.get_allowed_teams_for_org_users",
        return_value=allowed_teams,
    ), mock.patch(
        "lib.es_helpers.get_elasticsearch_client", return_value=mock_es
    ), mock.patch(
        "asset_management.web.app._get_platform_index_map", return_value={"Meta": "fb_*"}
    ), mock.patch(
        "asset_management.web.app._get_dashboard_index_map", return_value={}
    ):
        return client.get(url)


def _team_titles_from_query(captured_bodies):
    """Extract the team titles the server actually queried for."""
    # msearch body layout: [{index...}, {query...}, {index...}, {query...}, ...]
    # The team filter is duplicated (team_title + team_title.keyword) inside
    # the bool/should — deduplicate when extracting.
    for entry in captured_bodies:
        if not isinstance(entry, dict):
            continue
        query = entry.get("query")
        if not query:
            continue
        if query == {"match_all": {}}:
            return None  # unrestricted
        should = query.get("bool", {}).get("should", [])
        titles = set()
        for clause in should:
            for key in ("team_title", "team_title.keyword"):
                val = clause.get("term", {}).get(key)
                if val:
                    titles.add(val)
        if titles:
            return titles
    return set()


# ===========================================================================
# Scope enforcement — the core fix
# ===========================================================================


class TestScopedUserCannotOverrideScope:
    """A user scoped to Team A cannot probe Team B / Team C via ``?team=``."""

    def test_should_drop_forbidden_team_and_fall_back_to_allowed(self, scoped_team_a):
        captured = []
        resp = _run_endpoint(
            scoped_team_a,
            f"{PATH}?team=Team B",
            allowed_teams=["Team A"],
            captured_bodies=captured,
        )
        assert resp.status_code == 200
        titles = _team_titles_from_query(captured)
        assert titles == {"Team A"}, (
            f"expected query scoped to Team A, got {titles}"
        )

    def test_should_keep_team_when_user_is_on_it(self, scoped_team_a):
        captured = []
        resp = _run_endpoint(
            scoped_team_a,
            f"{PATH}?team=Team A",
            allowed_teams=["Team A"],
            captured_bodies=captured,
        )
        assert resp.status_code == 200
        assert _team_titles_from_query(captured) == {"Team A"}

    def test_should_fall_back_to_allowed_on_arbitrary_team(self, scoped_team_a):
        """Nonexistent team name doesn't let the client probe team existence."""
        captured = []
        resp = _run_endpoint(
            scoped_team_a,
            f"{PATH}?team=DOES_NOT_EXIST_xyz",
            allowed_teams=["Team A"],
            captured_bodies=captured,
        )
        assert resp.status_code == 200
        assert _team_titles_from_query(captured) == {"Team A"}

    def test_should_intersect_when_mixed_allowed_and_forbidden(self, scoped_team_a):
        """User on [A, B] passing ?team=A&team=C gets scoped to A only."""
        captured = []
        resp = _run_endpoint(
            scoped_team_a,
            f"{PATH}?team=Team A&team=Team C",
            allowed_teams=["Team A", "Team B"],
            captured_bodies=captured,
        )
        assert resp.status_code == 200
        assert _team_titles_from_query(captured) == {"Team A"}

    def test_should_drop_org_filter_for_scoped_users(self, scoped_team_a):
        """``?org=<other>`` must not re-expand scope via the lookup table."""
        captured = []
        resp = _run_endpoint(
            scoped_team_a,
            f"{PATH}?org=CRU",
            allowed_teams=["Team A"],
            captured_bodies=captured,
        )
        assert resp.status_code == 200
        assert _team_titles_from_query(captured) == {"Team A"}


class TestAdminIsUnrestricted:
    """Admin / unrestricted users keep the existing dropdown behaviour."""

    def test_should_honour_team_param_for_admin(self, auth_client):
        captured = []
        # ``auth_client`` is asset_admin; get_allowed_teams_for_org_users
        # returns None for admins, which is how our patch preserves
        # the existing behaviour.
        resp = _run_endpoint(
            auth_client,
            f"{PATH}?team=Any Team",
            allowed_teams=None,
            captured_bodies=captured,
        )
        assert resp.status_code == 200
        assert _team_titles_from_query(captured) == {"Any Team"}


class TestDebugTeamsFieldRemoved:
    """``debug_teams`` in the response body echoed the resolved scope and
    was the aid that confirmed the bypass. Remove it — no frontend reads
    it and leaving it encourages the same class of leak."""

    def test_response_body_omits_debug_teams(self, auth_client):
        captured = []
        resp = _run_endpoint(
            auth_client,
            PATH,
            allowed_teams=None,
            captured_bodies=captured,
        )
        assert resp.status_code == 200
        assert "debug_teams" not in resp.get_json()
