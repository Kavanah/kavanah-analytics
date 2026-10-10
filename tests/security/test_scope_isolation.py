"""Live scope-isolation regression tests.

Verifies that a regular dashboard user cannot bypass org/team scope by
tampering with client-supplied URL parameters, nor read dashboard
configuration they are not authorized for.

Each test is one concrete repro of a known or suspected isolation bug.
Run against staging after fixes land:

    TEST_BASE_URL=https://<staging-host> \\
    TEST_SESSION_COOKIE=<paste from devtools> \\
    TEST_ALLOWED_TEAM="Kavanah Team" \\
    TEST_FORBIDDEN_TEAM=FLKCH \\
    TEST_FORBIDDEN_ORG=CRU \\
    TEST_SCOPED_DASHBOARD_ID=a6a205e3-8c9b-451d-8dbe-8a181a98fe80 \\
    TEST_HIDDEN_DASHBOARD_ID=bf2e72a8-9f4c-4486-b024-c437fe0c0323 \\
    pytest tests/security/test_scope_isolation.py -v -m live

Linked tickets:
    dashboard-builder API returns dashboard config to unauthorized users
    platform-availability trusts client-supplied team param
    map-data / map-data/cities trust client-supplied team / org param
"""

from typing import Any

import pytest
import requests


pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(session: requests.Session, base_url: str, path: str, **params: Any) -> requests.Response:
    url = f"{base_url}{path}"
    return session.get(url, params=params or None, allow_redirects=False)


def _dashboard_has_data_map(body: dict) -> dict[str, bool]:
    return {k: v.get("has_data", False) for k, v in (body.get("dashboards") or {}).items()}


# ---------------------------------------------------------------------------
# platform-availability
# ---------------------------------------------------------------------------

class TestPlatformAvailabilityScope:
    """Endpoint: GET /assets/api/dashboards/platform-availability

    After the fix the server must derive scope from the session —
    client-supplied ``team`` / ``org`` values outside the user's allowed set
    are silently dropped. The response no longer includes ``debug_teams``
    (that was the aid that confirmed the bypass and has no frontend
    consumer), so tests fingerprint scope via the per-dashboard
    ``has_data`` map instead.
    """

    PATH = "/assets/api/dashboards/platform-availability"

    def test_baseline_is_stable(self, live_session, live_config):
        """Sanity check: no params returns a 200 with platforms + dashboards."""
        r = _get(live_session, live_config["base_url"], self.PATH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True
        assert "platforms" in body
        assert "dashboards" in body

    def test_tampered_team_is_rejected_or_ignored(self, live_session, live_config):
        """Passing ?team=<forbidden> must not cause the server to query that team.

        Accepted outcomes (PASS):
            - HTTP 403, or
            - HTTP 200 whose result matches the baseline (server ignored the param).

        Failure (the bug):
            - HTTP 200 whose per-dashboard ``has_data`` differs from baseline
              (proves the server ran a query against the forbidden scope).
        """
        baseline = _get(live_session, live_config["base_url"], self.PATH).json()
        r = _get(live_session, live_config["base_url"], self.PATH,
                 team=live_config["forbidden_team"])
        if r.status_code == 403:
            return
        assert r.status_code == 200, r.text
        assert _dashboard_has_data_map(r.json()) == _dashboard_has_data_map(baseline), (
            "response differs from baseline when tampering with team param — "
            "server ran query against tampered scope"
        )

    def test_tampered_org_is_rejected_or_ignored(self, live_session, live_config):
        """Passing ?org=<forbidden> must not cause the server to query that org."""
        baseline = _get(live_session, live_config["base_url"], self.PATH).json()
        r = _get(live_session, live_config["base_url"], self.PATH,
                 org=live_config["forbidden_org"])
        if r.status_code == 403:
            return
        assert r.status_code == 200, r.text
        assert _dashboard_has_data_map(r.json()) == _dashboard_has_data_map(baseline), (
            "response differs from baseline when tampering with org param"
        )

    def test_unknown_team_is_rejected_or_ignored(self, live_session, live_config):
        """An arbitrary team name must not be accepted as a query scope.

        If the server were still honouring the client-supplied team, the
        response for a nonexistent team would be all-``has_data: false``
        (matches no docs) — which is visibly different from the baseline
        any real user would see.
        """
        baseline = _get(live_session, live_config["base_url"], self.PATH).json()
        r = _get(live_session, live_config["base_url"], self.PATH,
                 team="DOES_NOT_EXIST_scope_test_xyz")
        if r.status_code == 403:
            return
        assert r.status_code == 200, r.text
        assert _dashboard_has_data_map(r.json()) == _dashboard_has_data_map(baseline), (
            "response for an arbitrary team name differs from baseline — "
            "server accepted client-supplied scope without validation"
        )

    def test_debug_teams_field_removed(self, live_session, live_config):
        """``debug_teams`` was a debug aid for diagnosing it echoed
        the resolved scope back to the client. After the fix it is no
        longer emitted. The frontend does not read this field."""
        r = _get(live_session, live_config["base_url"], self.PATH)
        assert r.status_code == 200, r.text
        assert "debug_teams" not in r.json(), (
            "response still includes debug_teams field — remove it as part "
            "of the  fix (no frontend consumer, echoes session scope)"
        )


# ---------------------------------------------------------------------------
# map-data / map-data/cities
# ---------------------------------------------------------------------------

def _map_fingerprint(body: dict) -> dict:
    """Stable identity for a map-data response.

    The aggregations are unordered, so compare as sets keyed on (name, value).
    The client never reads ``regions`` for non-fb sources but it's cheap to
    include, and any divergence is a bug regardless.
    """
    def _bag(key: str) -> set[tuple]:
        return {
            (str(p.get("name")), p.get("value"))
            for p in (body.get(key) or [])
            if isinstance(p, dict)
        }
    return {"cities": _bag("cities"), "regions": _bag("regions"), "countries": _bag("countries")}


class TestMapDataScope:
    """Endpoints: GET /assets/dashboard-builder/api/map-data
                  GET /assets/dashboard-builder/api/map-data/cities

    After the fix the server must derive scope from the session —
    client-supplied ``team`` / ``org`` values outside the user's allowed
    set are silently dropped, mirroring on platform-availability.
    Tests fingerprint scope via the cities / regions / countries
    aggregations (the same shape both endpoints return).

    Pre-condition: must be live so non-admin users reach the
    cities sub-route at all (otherwise it 403s before this can be
    observed).
    """

    PATH_MAP = "/assets/dashboard-builder/api/map-data"
    PATH_CITIES = "/assets/dashboard-builder/api/map-data/cities"

    # Use a date range broad enough that the user's own team has data;
    # otherwise both baseline and tampered responses are uniformly empty
    # and the test can't tell the difference.
    DEFAULT_PARAMS: dict[str, Any] = {
        "statistic": "reach",
        "date_from": "2024-01-01",
        "date_to": "2026-12-31",
    }

    def _get(self, live_session, base_url, path, **extra) -> requests.Response:
        params = {**self.DEFAULT_PARAMS, **extra}
        return _get(live_session, base_url, path, **params)

    # -- /api/map-data ------------------------------------------------------

    def test_baseline_is_stable(self, live_session, live_config):
        """No tampering — should 200 with the user's own scope."""
        r = self._get(live_session, live_config["base_url"], self.PATH_MAP)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True
        assert "countries" in body

    def test_tampered_team_is_rejected_or_ignored(self, live_session, live_config):
        """Passing ?team=<forbidden> must not cause the server to query that team.

        Accepted outcomes (PASS):
            - HTTP 403, or
            - HTTP 200 whose result matches the baseline (server ignored the param).

        Failure (the bug):
            - HTTP 200 whose aggregations differ from baseline (proves the
              server ran a query against the forbidden scope).
        """
        baseline = self._get(live_session, live_config["base_url"], self.PATH_MAP).json()
        r = self._get(
            live_session, live_config["base_url"], self.PATH_MAP,
            team=live_config["forbidden_team"],
        )
        if r.status_code == 403:
            return
        assert r.status_code == 200, r.text
        assert _map_fingerprint(r.json()) == _map_fingerprint(baseline), (
            "response differs from baseline when tampering with team param — "
            "server ran query against tampered scope"
        )

    def test_tampered_org_is_rejected_or_ignored(self, live_session, live_config):
        baseline = self._get(live_session, live_config["base_url"], self.PATH_MAP).json()
        r = self._get(
            live_session, live_config["base_url"], self.PATH_MAP,
            org=live_config["forbidden_org"],
        )
        if r.status_code == 403:
            return
        assert r.status_code == 200, r.text
        assert _map_fingerprint(r.json()) == _map_fingerprint(baseline), (
            "response differs from baseline when tampering with org param"
        )

    def test_unknown_team_is_rejected_or_ignored(self, live_session, live_config):
        """An arbitrary team name must not be accepted as a query scope.

        If the server were still honouring the client-supplied team, the
        response for a nonexistent team would be uniformly empty — which
        is visibly different from the baseline any real user would see.
        """
        baseline = self._get(live_session, live_config["base_url"], self.PATH_MAP).json()
        r = self._get(
            live_session, live_config["base_url"], self.PATH_MAP,
            team="DOES_NOT_EXIST_scope_test_xyz",
        )
        if r.status_code == 403:
            return
        assert r.status_code == 200, r.text
        assert _map_fingerprint(r.json()) == _map_fingerprint(baseline), (
            "response for an arbitrary team name differs from baseline — "
            "server accepted client-supplied scope without validation"
        )

    # -- /api/map-data/cities ----------------------------------------------

    def test_cities_baseline_is_stable(self, live_session, live_config):
        r = self._get(live_session, live_config["base_url"], self.PATH_CITIES)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True

    def test_cities_tampered_team_is_rejected_or_ignored(self, live_session, live_config):
        baseline = self._get(live_session, live_config["base_url"], self.PATH_CITIES).json()
        r = self._get(
            live_session, live_config["base_url"], self.PATH_CITIES,
            team=live_config["forbidden_team"],
        )
        if r.status_code == 403:
            return
        assert r.status_code == 200, r.text
        assert _map_fingerprint(r.json()) == _map_fingerprint(baseline), (
            "cities response differs from baseline when tampering with team — "
            "server ran query against tampered scope"
        )

    def test_cities_tampered_org_is_rejected_or_ignored(self, live_session, live_config):
        baseline = self._get(live_session, live_config["base_url"], self.PATH_CITIES).json()
        r = self._get(
            live_session, live_config["base_url"], self.PATH_CITIES,
            org=live_config["forbidden_org"],
        )
        if r.status_code == 403:
            return
        assert r.status_code == 200, r.text
        assert _map_fingerprint(r.json()) == _map_fingerprint(baseline), (
            "cities response differs from baseline when tampering with org"
        )


# ---------------------------------------------------------------------------
# dashboard-builder API
# ---------------------------------------------------------------------------

class TestDashboardBuilderApi:
    """Endpoint: GET /assets/dashboard-builder/api/dashboards/<id>

    This API exposes dashboard configuration. The normal dashboard-viewing
    flow (``/dashboard`` + the builder iframe) goes through this endpoint
    for non-builder users, so a blanket "require mc_dashboard_builder"
    would break viewing. Instead, the server enforces the same per-dashboard
    visibility rules used by ``DashboardService.get_visible_echarts_dashboards``:

    - ``show_all`` dashboards are readable by any authenticated user.
    - ``show_teams`` dashboards are readable only by admins or by users
      with a matching ``allowed_team_esids`` / ``allowed_org_esids`` role.
    - ``hide`` dashboards are readable only by admins.
    """

    PATH = "/assets/dashboard-builder/api/dashboards/{id}"

    def test_public_dashboard_is_readable(self, live_session, live_config):
        """A show_all dashboard should be readable by any authenticated user —
        that is the normal ``/dashboard`` viewer path."""
        r = _get(
            live_session,
            live_config["base_url"],
            self.PATH.format(id=live_config["public_dashboard_id"]),
        )
        assert r.status_code == 200, (
            f"show_all dashboard returned {r.status_code} to an authenticated "
            f"user — this is how the iframe viewer loads dashboards "
            f". Body: {r.text[:300]}"
        )

    def test_team_scoped_dashboard_is_blocked(self, live_session, live_config):
        """A show_teams dashboard the user is NOT on must not be readable."""
        r = _get(
            live_session,
            live_config["base_url"],
            self.PATH.format(id=live_config["scoped_dashboard_id"]),
        )
        assert r.status_code in (401, 403, 404), (
            f"builder API returned {r.status_code} for a team-scoped dashboard "
            f"the user is not allowed to view. Body: {r.text[:300]}"
        )

    def test_hidden_dashboard_is_blocked(self, live_session, live_config):
        """A visibility_mode=hide dashboard must not be readable."""
        r = _get(
            live_session,
            live_config["base_url"],
            self.PATH.format(id=live_config["hidden_dashboard_id"]),
        )
        assert r.status_code in (401, 403, 404), (
            f"builder API returned {r.status_code} for a hidden dashboard "
            f". Body: {r.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Filter options — currently PASSing but worth regression coverage
# ---------------------------------------------------------------------------

class TestFilterOptionsScope:
    """Endpoint: GET /assets/api/dashboards/filter-options

    As of 2026-04-23 this endpoint correctly narrows (but doesn't widen)
    scope. Lock that behavior in so a future refactor can't regress it.
    """

    PATH = "/assets/api/dashboards/filter-options"

    def test_forbidden_org_does_not_leak_teams(self, live_session, live_config):
        r = _get(live_session, live_config["base_url"], self.PATH,
                 orgs=live_config["forbidden_org"])
        assert r.status_code == 200, r.text
        team_titles = {t.get("title") for t in r.json().get("teams", [])}
        assert live_config["allowed_team"] not in team_titles or len(team_titles) <= 1, (
            "filter-options leaked teams outside user's scope"
        )
        forbidden_indicators = {live_config["forbidden_team"]}
        assert not (team_titles & forbidden_indicators), (
            f"filter-options returned forbidden team(s): "
            f"{team_titles & forbidden_indicators}"
        )

    def test_extended_org_list_cannot_widen_scope(self, live_session, live_config):
        r = _get(live_session, live_config["base_url"], self.PATH,
                 orgs=f"{live_config['allowed_team']},{live_config['forbidden_org']}")
        assert r.status_code == 200, r.text
        team_titles = {t.get("title") for t in r.json().get("teams", [])}
        assert live_config["forbidden_team"] not in team_titles, (
            "extending orgs= with a forbidden org leaked teams from that org"
        )
