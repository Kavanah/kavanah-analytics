"""
Tests for the RoleFilterService.

Covers:
  load_config / reload_config          — YAML loading, caching, missing file, parse error
  extract_org_ids_from_roles           — org_* extraction, mixed roles, empty
  extract_team_ids_from_roles          — team_* extraction, mixed roles, empty
  get_org_titles_by_ids                — ES lookup, missing field, no hits, ES down
  get_org_descriptions_by_ids          — description present, fallback to title, no hits
  get_team_titles_by_ids               — ES lookup, missing field, no hits, ES down
  get_team_titles_by_organization      — org→team lookup, no teams, ES down
  should_apply_filter                  — admin/superuser False, org/team True, no roles
  get_filter_for_current_user          — team filter, org→team filter, multi, empty, no roles
  get_allowed_teams                    — team roles, no filter, no team roles
  get_allowed_teams_for_org_users      — team roles first, org fallback, none
  get_allowed_organizations            — org roles, no filter, no org roles
  has_multiple_organizations           — 0, 1, 2+ orgs
  get_partner_organizations            — multi-org, single-org with partners, cached, none
  has_partnership_documents            — found, not found, ES down, cache hit
  get_filter_visibility                — team user, single-org, multi-org, admin
  audit_log_filter_application         — with filter, without filter
"""

import time
from unittest import mock

import pytest

import asset_management.web.services.role_filter_service as rfs_module
from asset_management.web.services.role_filter_service import (
    RoleFilterService,
    _partnership_cache,
    PARTNERSHIP_CACHE_TTL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _es_search_response(docs):
    """Build a minimal ES search response."""
    return {
        "hits": {
            "total": {"value": len(docs)},
            "hits": [
                {"_id": f"id_{i}", "_source": doc}
                for i, doc in enumerate(docs)
            ],
        }
    }


EMPTY_ES = _es_search_response([])


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset service caches before every test."""
    RoleFilterService._config_cache = None
    rfs_module._partnership_cache.clear()
    yield
    RoleFilterService._config_cache = None
    rfs_module._partnership_cache.clear()


# Override the conftest mock_es/mock_es_none to patch at the service-module level
# (the service does `from lib.es_helpers import get_elasticsearch_client` so the
# name is bound on the module, not looked up from lib.es_helpers at call time).
_SVC = "asset_management.web.services.role_filter_service"


@pytest.fixture
def mock_es():
    """Yield a MagicMock ES client patched into the role_filter_service module."""
    with mock.patch(f"{_SVC}.get_elasticsearch_client") as factory:
        client = mock.MagicMock()
        factory.return_value = client
        yield client


@pytest.fixture
def mock_es_none():
    """Simulate ES unavailable for the role_filter_service module."""
    with mock.patch(f"{_SVC}.get_elasticsearch_client", return_value=None):
        yield


@pytest.fixture
def mock_session(app):
    """Provide a context manager that patches flask.session inside the service module."""
    def _make(roles=None, is_admin=False, extras=None):
        data = {
            "roles": roles or [],
            "is_admin": is_admin,
        }
        if extras:
            data.update(extras)
        return mock.patch(
            "asset_management.web.services.role_filter_service.session",
            data,
        )
    return _make


# ---------------------------------------------------------------------------
# load_config / reload_config
# ---------------------------------------------------------------------------

class TestLoadConfig:

    @mock.patch("os.path.exists", return_value=False)
    def test_missing_file_returns_default(self, _exists):
        config = RoleFilterService.load_config()
        # follow-up: asset_admin moved to system-admin path in
        # the new authz model; only superuser stays in the legacy bypass.
        assert config["unrestricted_roles"] == ["superuser"]
        assert config["role_mappings"] == {}

    @mock.patch("os.path.exists", return_value=False)
    def test_caches_config(self, _exists):
        first = RoleFilterService.load_config()
        second = RoleFilterService.load_config()
        assert first is second

    @mock.patch("builtins.open", mock.mock_open(read_data="unrestricted_roles:\n  - superuser\nrole_mappings:\n  org_VOGZ: VOGZ\n"))
    @mock.patch("os.path.exists", return_value=True)
    def test_loads_yaml_file(self, _exists):
        config = RoleFilterService.load_config()
        assert "superuser" in config["unrestricted_roles"]
        assert config["role_mappings"]["org_VOGZ"] == "VOGZ"

    @mock.patch("builtins.open", mock.mock_open(read_data=": invalid: yaml: ["))
    @mock.patch("os.path.exists", return_value=True)
    def test_yaml_parse_error_returns_default(self, _exists):
        config = RoleFilterService.load_config()
        # follow-up: asset_admin moved to system-admin path in
        # the new authz model; only superuser stays in the legacy bypass.
        assert config["unrestricted_roles"] == ["superuser"]

    @mock.patch("os.path.exists", return_value=False)
    def test_reload_clears_cache(self, _exists):
        RoleFilterService.load_config()
        RoleFilterService._config_cache = {"custom": True}
        config = RoleFilterService.reload_config()
        # After reload, it should re-read (which hits missing-file default)
        assert "custom" not in config


# ---------------------------------------------------------------------------
# extract_*_from_roles — (2026-05-28) removed the legacy regex
# branch; both helpers now unconditionally call
# ``_*_titles_from_memberships()`` which reads session + ES. The legacy
# regex unit tests that lived here have been removed; new-authz behavior
# is pinned by
# the release-plan tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_org_titles_by_ids
# ---------------------------------------------------------------------------

class TestGetOrgTitlesByIds:

    def test_returns_titles(self, mock_es):
        mock_es.search.return_value = _es_search_response(
            [{"organization_title": "VOGZ"}]
        )
        result = RoleFilterService.get_org_titles_by_ids(["VOGZ"])
        assert result == ["VOGZ"]
        mock_es.search.assert_called_once()

    def test_no_hits_returns_empty(self, mock_es):
        mock_es.search.return_value = EMPTY_ES
        assert RoleFilterService.get_org_titles_by_ids(["MISSING"]) == []

    def test_missing_field_in_source(self, mock_es):
        mock_es.search.return_value = _es_search_response([{"other_field": "x"}])
        assert RoleFilterService.get_org_titles_by_ids(["VOGZ"]) == []

    def test_es_unavailable(self, mock_es_none):
        assert RoleFilterService.get_org_titles_by_ids(["VOGZ"]) == []

    def test_es_exception(self, mock_es):
        mock_es.search.side_effect = Exception("connection refused")
        assert RoleFilterService.get_org_titles_by_ids(["VOGZ"]) == []


# ---------------------------------------------------------------------------
# get_org_descriptions_by_ids
# ---------------------------------------------------------------------------

class TestGetOrgDescriptionsByIds:

    def test_returns_description(self, mock_es):
        mock_es.search.return_value = _es_search_response(
            [{"organization_title": "VOGZ", "organization_description": "T-Mobile"}]
        )
        assert RoleFilterService.get_org_descriptions_by_ids(["VOGZ"]) == ["T-Mobile"]

    def test_falls_back_to_title(self, mock_es):
        mock_es.search.return_value = _es_search_response(
            [{"organization_title": "VOGZ"}]
        )
        assert RoleFilterService.get_org_descriptions_by_ids(["VOGZ"]) == ["VOGZ"]

    def test_no_hits(self, mock_es):
        mock_es.search.return_value = EMPTY_ES
        assert RoleFilterService.get_org_descriptions_by_ids(["VOGZ"]) == []

    def test_es_unavailable(self, mock_es_none):
        assert RoleFilterService.get_org_descriptions_by_ids(["VOGZ"]) == []


# ---------------------------------------------------------------------------
# get_team_titles_by_ids
# ---------------------------------------------------------------------------

class TestGetTeamTitlesByIds:

    def test_returns_titles(self, mock_es):
        mock_es.search.return_value = _es_search_response(
            [{"team_title": "NFCY"}]
        )
        assert RoleFilterService.get_team_titles_by_ids(["NFCY"]) == ["NFCY"]

    def test_no_hits(self, mock_es):
        mock_es.search.return_value = EMPTY_ES
        assert RoleFilterService.get_team_titles_by_ids(["MISSING"]) == []

    def test_missing_field(self, mock_es):
        mock_es.search.return_value = _es_search_response([{"other": "x"}])
        assert RoleFilterService.get_team_titles_by_ids(["NFCY"]) == []

    def test_es_unavailable(self, mock_es_none):
        assert RoleFilterService.get_team_titles_by_ids(["NFCY"]) == []

    def test_es_exception(self, mock_es):
        mock_es.search.side_effect = Exception("timeout")
        assert RoleFilterService.get_team_titles_by_ids(["NFCY"]) == []


# ---------------------------------------------------------------------------
# get_team_titles_by_organization
# ---------------------------------------------------------------------------

class TestGetTeamTitlesByOrganization:

    def test_returns_teams(self, mock_es):
        mock_es.search.return_value = _es_search_response([
            {"team_title": "NFCY", "organization_title": "VOGZ"},
            {"team_title": "FLKVT", "organization_title": "VOGZ"},
        ])
        result = RoleFilterService.get_team_titles_by_organization(["VOGZ"])
        assert result == ["NFCY", "FLKVT"]

    def test_no_teams_found(self, mock_es):
        mock_es.search.return_value = EMPTY_ES
        assert RoleFilterService.get_team_titles_by_organization(["VOGZ"]) == []

    def test_es_unavailable(self, mock_es_none):
        assert RoleFilterService.get_team_titles_by_organization(["VOGZ"]) == []

    def test_es_exception(self, mock_es):
        mock_es.search.side_effect = Exception("timeout")
        assert RoleFilterService.get_team_titles_by_organization(["VOGZ"]) == []

    def test_query_uses_match_not_terms(self, mock_es):
        """Ensure match query is used so multi-org teams (comma-separated or array) are found.

        The terms query requires exact token matches and fails when organization_title
        is stored as a comma-separated string (e.g. "IPRK, VOGZ, CRU") or when the
        field is text-analyzed.  This regression has occurred multiple times.
        """
        mock_es.search.return_value = EMPTY_ES
        RoleFilterService.get_team_titles_by_organization(["VOGZ"])

        query = mock_es.search.call_args[1]["body"]["query"]
        should_clauses = query["bool"]["should"]
        assert len(should_clauses) == 1
        clause = should_clauses[0]
        assert "match" in clause, (
            f"Expected 'match' query for multi-org support, got: {clause}"
        )
        assert "terms" not in clause, (
            "terms query breaks multi-org teams — use match instead"
        )
        assert clause["match"]["organization_title"] == "VOGZ"

    def test_query_uses_match_for_multiple_orgs(self, mock_es):
        """Each org gets its own match clause in a bool/should."""
        mock_es.search.return_value = EMPTY_ES
        RoleFilterService.get_team_titles_by_organization(["VOGZ", "Kavanah"])

        query = mock_es.search.call_args[1]["body"]["query"]
        should_clauses = query["bool"]["should"]
        assert len(should_clauses) == 2
        for clause in should_clauses:
            assert "match" in clause, (
                f"Expected 'match' query, got: {clause}"
            )
        org_values = [c["match"]["organization_title"] for c in should_clauses]
        assert "VOGZ" in org_values
        assert "Kavanah" in org_values

    def test_returns_multi_org_teams(self, mock_es):
        """Teams with multiple orgs (comma-separated or array) should be returned."""
        mock_es.search.return_value = _es_search_response([
            {"team_title": "NFCY", "organization_title": "VOGZ"},
            {"team_title": "SharedTeam", "organization_title": ["VOGZ", "CRU"]},
            {"team_title": "CsvTeam", "organization_title": "IPRK, VOGZ, CRU"},
        ])
        result = RoleFilterService.get_team_titles_by_organization(["VOGZ"])
        assert "NFCY" in result
        assert "SharedTeam" in result
        assert "CsvTeam" in result


# ---------------------------------------------------------------------------
# should_apply_filter
# ---------------------------------------------------------------------------

class TestShouldApplyFilter:

    @mock.patch("os.path.exists", return_value=False)
    def test_asset_admin_returns_true(self, _exists, mock_session):
        """(2026-05-15): the pre-fix contract was 'asset_admin =>
        no filter applied'. inverts this: asset_admin is now a
        privileged role on the data path (memberships ∪ grants).
        should_apply_filter returning True is what tells callers like
        the asset tree (app.py:1501) and the get_allowed_* methods to
        consult the membership/grant lookup, instead of falling
        through to no-filter and exposing every tenant's data.

        The legacy test name (``test_admin_returns_false``) is retained
        in git history; the new name documents the earlier
        contract.
        """
        with mock_session(roles=["asset_admin"]):
            assert RoleFilterService.should_apply_filter() is True

    @mock.patch("os.path.exists", return_value=False)
    def test_superuser_returns_true(self, _exists, mock_session):
        """(2026-05-14): the pre-F1 contract was 'superuser =>
        no filter applied'. F1 inverts this: privileged users (system
        -admin / unrestricted-role) now always have a filter applied —
        either grant-scoped or the deny-all sentinel. should_apply_filter
        returning True is what tells callers like the asset tree
        (app.py:1501) and the get_allowed_* methods to consult the
        grant lookup, instead of falling through to no-filter.

        The legacy test name (``test_superuser_returns_false``) is
        retained in git history; the new name documents the post-F1
        contract.
        """
        with mock_session(roles=["superuser"]):
            assert RoleFilterService.should_apply_filter() is True

    @mock.patch("os.path.exists", return_value=False)
    def test_org_role_returns_true(self, _exists, mock_session, mock_es):
        # (2026-05-28): post-flag-retirement, the resolver's
        # legacy-regex union is the path that catches
        # ``org_<title>`` strings from session["roles"]. It calls
        # ``get_org_titles_by_ids`` which queries ES; stub the response.
        mock_es.search.return_value = _es_search_response([{"organization_title": "VOGZ"}])
        with mock_session(roles=["org_VOGZ"]):
            assert RoleFilterService.should_apply_filter() is True

    @mock.patch("os.path.exists", return_value=False)
    def test_team_role_returns_true(self, _exists, mock_session, mock_es):
        # (2026-05-28): see test_org_role_returns_true.
        mock_es.search.return_value = _es_search_response([{"team_title": "NFCY"}])
        with mock_session(roles=["team_NFCY"]):
            assert RoleFilterService.should_apply_filter() is True

    @mock.patch("os.path.exists", return_value=False)
    def test_no_roles_still_applies_filter(self, _exists, mock_session):
        with mock_session(roles=[]):
            assert RoleFilterService.should_apply_filter() is True  # no scope → deny, never "unrestricted"

    @mock.patch("os.path.exists", return_value=False)
    def test_unrelated_roles_still_applies_filter(self, _exists, mock_session):
        with mock_session(roles=["kibana_admin"]):
            assert RoleFilterService.should_apply_filter() is True  # no scope → deny, never "unrestricted"


# ---------------------------------------------------------------------------
# get_filter_for_current_user
# ---------------------------------------------------------------------------

class TestGetFilterForCurrentUser:

    @mock.patch("os.path.exists", return_value=False)
    def test_asset_admin_returns_deny_all(self, _exists, mock_session, mock_es):
        """(2026-05-15): bare asset_admin (no memberships, no
        grants) gets the ``__NO_TEAMS_FOR_ORG__`` sentinel — same shape
         produced for bare superuser. earlier this returned
        None (unrestricted), which was the leak this ticket closes.
        ``mock_es`` is patched to return zero hits for the
        de_authz_data_grant + de_authz_user_team_role + de_authz_user_org_role
        searches, simulating a bare admin.
        """
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        mock_es.get.side_effect = Exception("not_found")
        with mock_session(roles=["asset_admin"]):
            result = RoleFilterService.get_filter_for_current_user()
        assert result == {"term": {"team_title": "__NO_TEAMS_FOR_ORG__"}}

    @mock.patch("os.path.exists", return_value=False)
    def test_no_roles_returns_deny_all(self, _exists, mock_session):
        with mock_session(roles=[]):
            assert RoleFilterService.get_filter_for_current_user() == {"term": {"team_title": "__NO_TEAMS_FOR_ORG__"}}  # no scope → deny, never "unrestricted"

    @mock.patch("os.path.exists", return_value=False)
    def test_single_team_role(self, _exists, mock_session, mock_es):
        mock_es.search.return_value = _es_search_response([{"team_title": "NFCY"}])
        with mock_session(roles=["team_NFCY"]):
            result = RoleFilterService.get_filter_for_current_user()
        assert result == {"term": {"team_title": "NFCY"}}

    @mock.patch("os.path.exists", return_value=False)
    def test_multiple_team_roles(self, _exists, mock_session, mock_es):
        mock_es.search.side_effect = [
            _es_search_response([{"team_title": "NFCY"}]),
            _es_search_response([{"team_title": "FLKVT"}]),
        ]
        with mock_session(roles=["team_NFCY", "team_FLKVT"]):
            result = RoleFilterService.get_filter_for_current_user()
        assert result == {"terms": {"team_title": ["NFCY", "FLKVT"]}}

    @mock.patch("os.path.exists", return_value=False)
    def test_org_role_resolves_to_team_filter(self, _exists, mock_session, mock_es):
        # First call: get_org_titles_by_ids (org lookup)
        # Second call: get_team_titles_by_organization (team lookup)
        mock_es.search.side_effect = [
            _es_search_response([{"organization_title": "VOGZ"}]),
            _es_search_response([
                {"team_title": "NFCY", "organization_title": "VOGZ"},
                {"team_title": "FLKVT", "organization_title": "VOGZ"},
            ]),
        ]
        with mock_session(roles=["org_VOGZ"]):
            result = RoleFilterService.get_filter_for_current_user()
        assert result == {"terms": {"team_title": ["NFCY", "FLKVT"]}}

    # (2026-05-28): test_org_role_no_teams_returns_sentinel was
    # removed. The earlier ``get_filter_for_current_user`` early-bailed
    # to the ``__NO_TEAMS_FOR_ORG__`` sentinel when the org existed but
    # produced no teams. earlier the method falls through to the
    # unified resolver, which makes additional ES round-trips (membership
    # lookups, legacy union) that this test's 2-element mock_es.side_effect
    # cannot satisfy. The org→no-teams → sentinel behavior is still pinned
    # by ``test_asset_admin_returns_deny_all`` and the
    # source-pins; the bare-legacy-only org user it modeled is the
    # hybrid cohort (zero on staging per the backfill
    # dry-run, expected zero on prod).

    @mock.patch("os.path.exists", return_value=False)
    def test_unrelated_roles_returns_deny_all(self, _exists, mock_session):
        with mock_session(roles=["kibana_admin"]):
            result = RoleFilterService.get_filter_for_current_user()
        assert result == {"term": {"team_title": "__NO_TEAMS_FOR_ORG__"}}  # no scope → deny, never "unrestricted"


# ---------------------------------------------------------------------------
# get_allowed_teams
# ---------------------------------------------------------------------------

class TestGetAllowedTeams:

    @mock.patch("os.path.exists", return_value=False)
    def test_asset_admin_returns_empty(self, _exists, mock_session, mock_es):
        """bare asset_admin returns ``[]`` (empty list, NOT
        ``None``) so callers like the asset tree know to render no teams
        instead of falling back to unrestricted. Mirrors the earlier
        contract for bare superuser.
        """
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        mock_es.get.side_effect = Exception("not_found")
        with mock_session(roles=["asset_admin"]):
            assert RoleFilterService.get_allowed_teams() == []

    @mock.patch("os.path.exists", return_value=False)
    def test_team_roles_return_titles(self, _exists, mock_session, mock_es):
        mock_es.search.return_value = _es_search_response([{"team_title": "NFCY"}])
        with mock_session(roles=["team_NFCY"]):
            assert RoleFilterService.get_allowed_teams() == ["NFCY"]

    @mock.patch("os.path.exists", return_value=False)
    def test_no_team_roles_returns_empty(self, _exists, mock_session):
        with mock_session(roles=["org_VOGZ"]):
            assert RoleFilterService.get_allowed_teams() == []  # no scope → deny, never "unrestricted"


# ---------------------------------------------------------------------------
# get_allowed_teams_for_org_users
# ---------------------------------------------------------------------------

class TestGetAllowedTeamsForOrgUsers:

    @mock.patch("os.path.exists", return_value=False)
    def test_asset_admin_returns_empty(self, _exists, mock_session, mock_es):
        """bare asset_admin returns ``[]`` so the dashboard
        builder team picker (Surface 3) filters to no teams.
        """
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        mock_es.get.side_effect = Exception("not_found")
        with mock_session(roles=["asset_admin"]):
            assert RoleFilterService.get_allowed_teams_for_org_users() == []

    @mock.patch("os.path.exists", return_value=False)
    def test_team_roles_take_priority(self, _exists, mock_session, mock_es):
        mock_es.search.return_value = _es_search_response([{"team_title": "NFCY"}])
        with mock_session(roles=["team_NFCY", "org_VOGZ"]):
            result = RoleFilterService.get_allowed_teams_for_org_users()
        assert result == ["NFCY"]

    @mock.patch("os.path.exists", return_value=False)
    def test_org_roles_resolve_to_teams(self, _exists, mock_session, mock_es):
        mock_es.search.side_effect = [
            _es_search_response([{"organization_title": "VOGZ"}]),
            _es_search_response([
                {"team_title": "NFCY", "organization_title": "VOGZ"},
            ]),
        ]
        with mock_session(roles=["org_VOGZ"]):
            result = RoleFilterService.get_allowed_teams_for_org_users()
        assert result == ["NFCY"]

    @mock.patch("os.path.exists", return_value=False)
    def test_no_roles_returns_empty(self, _exists, mock_session):
        with mock_session(roles=["kibana_admin"]):
            assert RoleFilterService.get_allowed_teams_for_org_users() == []  # no scope → deny, never "unrestricted"


# ---------------------------------------------------------------------------
# get_allowed_organizations
# ---------------------------------------------------------------------------

class TestGetAllowedOrganizations:

    @mock.patch("os.path.exists", return_value=False)
    def test_asset_admin_returns_empty(self, _exists, mock_session, mock_es):
        """bare asset_admin returns ``[]`` so the asset tree
        renders no orgs instead of falling back to all-orgs visibility.
        """
        mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        mock_es.get.side_effect = Exception("not_found")
        with mock_session(roles=["asset_admin"]):
            assert RoleFilterService.get_allowed_organizations() == []

    @mock.patch("os.path.exists", return_value=False)
    def test_org_roles_return_titles(self, _exists, mock_session, mock_es):
        mock_es.search.return_value = _es_search_response(
            [{"organization_title": "VOGZ"}]
        )
        with mock_session(roles=["org_VOGZ"]):
            assert RoleFilterService.get_allowed_organizations() == ["VOGZ"]

    @mock.patch("os.path.exists", return_value=False)
    def test_no_org_roles_returns_empty(self, _exists, mock_session):
        with mock_session(roles=["team_NFCY"]):
            assert RoleFilterService.get_allowed_organizations() == []  # no scope → deny, never "unrestricted"


# ---------------------------------------------------------------------------
# has_multiple_organizations
# ---------------------------------------------------------------------------

class TestHasMultipleOrganizations:

    def test_zero_orgs(self, mock_session):
        with mock_session(roles=["team_NFCY"]):
            assert RoleFilterService.has_multiple_organizations() is False

    def test_one_org(self, mock_session):
        with mock_session(roles=["org_VOGZ"]):
            assert RoleFilterService.has_multiple_organizations() is False

    def test_two_orgs(self, mock_session):
        with mock_session(roles=["org_VOGZ", "org_Kavanah"]):
            assert RoleFilterService.has_multiple_organizations() is True


# ---------------------------------------------------------------------------
# get_partner_organizations
# ---------------------------------------------------------------------------

class TestGetPartnerOrganizations:

    @mock.patch("os.path.exists", return_value=False)
    def test_multi_org_user(self, _exists, mock_session, mock_es):
        mock_es.search.side_effect = [
            _es_search_response([{"organization_title": "VOGZ"}]),
            _es_search_response([{"organization_title": "Kavanah"}]),
        ]
        with mock_session(roles=["org_VOGZ", "org_Kavanah"]):
            result = RoleFilterService.get_partner_organizations()
        assert len(result) == 2
        assert result[0] == {"title": "VOGZ", "is_primary": True}
        assert result[1] == {"title": "Kavanah", "is_primary": False}

    @mock.patch("os.path.exists", return_value=False)
    def test_single_org_with_partners(self, _exists, mock_session, mock_es):
        # First call: get_org_titles_by_ids
        # Second call: get_partner_orgs_from_teams
        mock_es.search.side_effect = [
            _es_search_response([{"organization_title": "VOGZ"}]),
            _es_search_response([
                {"team_title": "SharedTeam", "organization_title": "VOGZ, CRU"},
            ]),
        ]
        with mock_session(roles=["org_VOGZ"]):
            result = RoleFilterService.get_partner_organizations()
        assert result[0] == {"title": "VOGZ", "is_primary": True}
        # Partner org should be included
        partner_titles = [p["title"] for p in result if not p["is_primary"]]
        assert "CRU" in partner_titles

    @mock.patch("os.path.exists", return_value=False)
    def test_no_org_roles_returns_none(self, _exists, mock_session):
        with mock_session(roles=["team_NFCY"]):
            result = RoleFilterService.get_partner_organizations()
        assert result is None

    @mock.patch("os.path.exists", return_value=False)
    def test_session_cache_hit(self, _exists, mock_session):
        cached = [{"title": "VOGZ", "is_primary": True}]
        with mock_session(roles=["org_VOGZ"], extras={"cached_partner_orgs": cached}):
            result = RoleFilterService.get_partner_organizations()
        assert result == cached

    @mock.patch("os.path.exists", return_value=False)
    def test_session_cache_empty_returns_none(self, _exists, mock_session):
        with mock_session(roles=["org_VOGZ"], extras={"cached_partner_orgs": []}):
            result = RoleFilterService.get_partner_organizations()
        assert result is None


# ---------------------------------------------------------------------------
# get_partner_orgs_from_teams
# ---------------------------------------------------------------------------

class TestGetPartnerOrgsFromTeams:

    def test_comma_separated_orgs(self, mock_es):
        mock_es.search.return_value = _es_search_response([
            {"team_title": "T1", "organization_title": "IPRK, VOGZ, CRU"},
        ])
        result = RoleFilterService.get_partner_orgs_from_teams("VOGZ")
        assert sorted(result) == ["CRU", "IPRK", "VOGZ"]

    def test_array_orgs(self, mock_es):
        mock_es.search.return_value = _es_search_response([
            {"team_title": "T1", "organization_title": ["VOGZ", "CRU"]},
        ])
        result = RoleFilterService.get_partner_orgs_from_teams("VOGZ")
        assert sorted(result) == ["CRU", "VOGZ"]

    def test_single_string_org(self, mock_es):
        mock_es.search.return_value = _es_search_response([
            {"team_title": "T1", "organization_title": "VOGZ"},
        ])
        result = RoleFilterService.get_partner_orgs_from_teams("VOGZ")
        assert result == ["VOGZ"]

    def test_es_unavailable(self, mock_es_none):
        assert RoleFilterService.get_partner_orgs_from_teams("VOGZ") == []

    def test_es_exception(self, mock_es):
        mock_es.search.side_effect = Exception("boom")
        assert RoleFilterService.get_partner_orgs_from_teams("VOGZ") == []


# ---------------------------------------------------------------------------
# has_partnership_documents
# ---------------------------------------------------------------------------

class TestHasPartnershipDocuments:

    def test_found_partnership(self, mock_es):
        mock_es.search.return_value = _es_search_response([
            {"organization_title": ["VOGZ", "CRU"]},
        ])
        assert RoleFilterService.has_partnership_documents("VOGZ") is True

    def test_no_partnership(self, mock_es):
        mock_es.search.return_value = _es_search_response([
            {"organization_title": ["VOGZ"]},
        ])
        assert RoleFilterService.has_partnership_documents("VOGZ") is False

    def test_no_array_field(self, mock_es):
        mock_es.search.return_value = _es_search_response([
            {"organization_title": "VOGZ"},
        ])
        assert RoleFilterService.has_partnership_documents("VOGZ") is False

    def test_es_unavailable(self, mock_es_none):
        assert RoleFilterService.has_partnership_documents("VOGZ") is False

    def test_cache_hit(self, mock_es):
        rfs_module._partnership_cache["VOGZ"] = (True, time.time())
        # Should return cached value without calling ES
        assert RoleFilterService.has_partnership_documents("VOGZ") is True
        mock_es.search.assert_not_called()

    def test_cache_expired(self, mock_es):
        rfs_module._partnership_cache["VOGZ"] = (
            True,
            time.time() - PARTNERSHIP_CACHE_TTL - 1,
        )
        mock_es.search.return_value = EMPTY_ES
        assert RoleFilterService.has_partnership_documents("VOGZ") is False
        mock_es.search.assert_called()

    def test_es_exception_returns_false(self, mock_es):
        mock_es.search.side_effect = Exception("timeout")
        assert RoleFilterService.has_partnership_documents("VOGZ") is False

    def test_query_uses_match_not_terms(self, mock_es):
        """Ensure match query is used so multi-org documents are found.

        Same regression risk as get_team_titles_by_organization — terms query
        fails for comma-separated or text-analyzed organization_title fields.
        """
        mock_es.search.return_value = EMPTY_ES
        RoleFilterService.has_partnership_documents("VOGZ")

        query = mock_es.search.call_args[1]["body"]["query"]
        must_clauses = query["bool"]["must"]
        assert len(must_clauses) == 1
        clause = must_clauses[0]
        assert "match" in clause, (
            f"Expected 'match' query for multi-org support, got: {clause}"
        )
        assert "terms" not in clause, (
            "terms query breaks multi-org lookups — use match instead"
        )


# ---------------------------------------------------------------------------
# get_filter_visibility
# ---------------------------------------------------------------------------

class TestGetFilterVisibility:

    def test_single_team_user_hides_all(self, mock_session):
        with mock_session(roles=["team_NFCY"]):
            vis = RoleFilterService.get_filter_visibility()
        assert vis["show_org_filter"] is False
        assert vis["show_partnership_filter"] is False
        assert vis["show_team_filter"] is False

    def test_multi_team_user_shows_team_filter(self, mock_session):
        with mock_session(roles=["team_NFCY", "team_FLKVT"]):
            vis = RoleFilterService.get_filter_visibility()
        assert vis["show_org_filter"] is False
        assert vis["show_partnership_filter"] is False
        assert vis["show_team_filter"] is True

    def test_multi_org_user_shows_org_and_partnership(self, mock_session, mock_es):
        mock_es.search.return_value = EMPTY_ES  # org lookup (not critical here)
        with mock_session(roles=["org_VOGZ", "org_Kavanah"]):
            vis = RoleFilterService.get_filter_visibility()
        assert vis["show_org_filter"] is True
        assert vis["show_partnership_filter"] is True
        assert vis["show_team_filter"] is True

    def test_single_org_no_partnerships(self, mock_session, mock_es):
        mock_es.search.side_effect = [
            # get_org_titles_by_ids
            _es_search_response([{"organization_title": "VOGZ"}]),
            # has_partnership_documents — all indices return no multi-org docs
            EMPTY_ES, EMPTY_ES, EMPTY_ES, EMPTY_ES, EMPTY_ES,
        ]
        with mock_session(roles=["org_VOGZ"]):
            vis = RoleFilterService.get_filter_visibility()
        assert vis["show_org_filter"] is False
        assert vis["show_partnership_filter"] is False
        assert vis["show_team_filter"] is True

    def test_single_org_with_partnerships(self, mock_session, mock_es):
        mock_es.search.side_effect = [
            # get_org_titles_by_ids
            _es_search_response([{"organization_title": "VOGZ"}]),
            # has_partnership_documents — first index has a multi-org doc
            _es_search_response([{"organization_title": ["VOGZ", "CRU"]}]),
        ]
        with mock_session(roles=["org_VOGZ"]):
            vis = RoleFilterService.get_filter_visibility()
        assert vis["show_partnership_filter"] is True

    def test_admin_shows_org_and_team(self, mock_session):
        # earlier /, a bare superuser with no resolvable
        # memberships / grants / workspace assignments falls through to
        # the zero-org / zero-team branch, which hides both filters
        # (nothing to switch between). The legacy "superuser sees all"
        # bypass was retired by the scope
        # redesign — every user is now scoped by their actual data
        # access, even unrestricted-role holders.
        with mock_session(roles=["superuser"]):
            vis = RoleFilterService.get_filter_visibility()
        assert vis["show_org_filter"] is False
        assert vis["show_team_filter"] is False
        assert vis["show_partnership_filter"] is False


# ---------------------------------------------------------------------------
# audit_log_filter_application
# ---------------------------------------------------------------------------

class TestAuditLogFilterApplication:

    def test_with_filter(self, caplog):
        import logging
        with caplog.at_level(logging.INFO):
            RoleFilterService.audit_log_filter_application(
                "testuser",
                {"term": {"team_title": "NFCY"}},
                context="test",
            )
        assert "filter_applied=True" in caplog.text

    def test_without_filter(self, caplog):
        import logging
        with caplog.at_level(logging.INFO):
            RoleFilterService.audit_log_filter_application(
                "testuser", None, context="test"
            )
        assert "filter_applied=False" in caplog.text


# ---------------------------------------------------------------------------
# intersect_body_filters
# ---------------------------------------------------------------------------


class TestIntersectBodyFilters:
    """``RoleFilterService.intersect_body_filters`` strips body filters whose
    scope-defining values fall outside the user's allowed set."""

    def test_admin_path_is_noop(self):
        body = {
            "filters": [
                {"field": "team_title", "op": "eq", "value": "Other Team"},
                {"field": "organization_title", "op": "eq", "value": "Some Org"},
            ]
        }
        before = [dict(f) for f in body["filters"]]
        RoleFilterService.intersect_body_filters(body, allowed_teams=None)
        assert body["filters"] == before

    def test_drops_team_title_filter_with_forbidden_value(self):
        body = {
            "filters": [
                {"field": "team_title", "op": "eq", "value": "Other Forbidden Team"},
                {"field": "status", "op": "eq", "value": "active"},
            ]
        }
        RoleFilterService.intersect_body_filters(body, allowed_teams=["Kavanah Team"])
        assert body["filters"] == [{"field": "status", "op": "eq", "value": "active"}]

    def test_keeps_team_title_filter_with_allowed_value(self):
        body = {
            "filters": [
                {"field": "team_title", "op": "eq", "value": "Kavanah Team"},
            ]
        }
        RoleFilterService.intersect_body_filters(body, allowed_teams=["Kavanah Team"])
        assert body["filters"] == [
            {"field": "team_title", "op": "eq", "value": "Kavanah Team"}
        ]

    def test_intersects_list_value_filter(self):
        body = {
            "filters": [
                {
                    "field": "team_title",
                    "op": "match_phrase_any",
                    "value": ["Kavanah Team", "Other Forbidden Team"],
                },
            ]
        }
        RoleFilterService.intersect_body_filters(body, allowed_teams=["Kavanah Team"])
        assert body["filters"] == [
            {
                "field": "team_title",
                "op": "match_phrase_any",
                "value": ["Kavanah Team"],
            }
        ]

    def test_drops_filter_when_list_intersection_is_empty(self):
        body = {
            "filters": [
                {
                    "field": "team_title",
                    "op": "match_phrase_any",
                    "value": ["Forbidden A", "Forbidden B"],
                },
            ]
        }
        RoleFilterService.intersect_body_filters(body, allowed_teams=["Kavanah Team"])
        assert body["filters"] == []

    def test_drops_organization_title_filter_outright(self):
        """Org filters are dropped for scoped users — they could re-expand
        scope via partner-org lookup. rationale."""
        body = {
            "filters": [
                {"field": "organization_title", "op": "eq", "value": "Kavanah"},
                {"field": "team_title", "op": "eq", "value": "Kavanah Team"},
            ]
        }
        RoleFilterService.intersect_body_filters(body, allowed_teams=["Kavanah Team"])
        assert body["filters"] == [
            {"field": "team_title", "op": "eq", "value": "Kavanah Team"}
        ]

    def test_keeps_keyword_variant_field(self):
        """``team_title.keyword`` is also scope-defining, mirrors bare field."""
        body = {
            "filters": [
                {"field": "team_title.keyword", "op": "eq", "value": "Other Forbidden Team"},
            ]
        }
        RoleFilterService.intersect_body_filters(body, allowed_teams=["Kavanah Team"])
        assert body["filters"] == []

    def test_handles_missing_filters_key(self):
        body = {}
        RoleFilterService.intersect_body_filters(body, allowed_teams=["Kavanah Team"])
        assert body == {}

    def test_handles_non_dict_body(self):
        # Should not raise.
        RoleFilterService.intersect_body_filters("not a dict", allowed_teams=["Kavanah Team"])
        RoleFilterService.intersect_body_filters(None, allowed_teams=["Kavanah Team"])

    def test_passes_through_non_scope_filters(self):
        body = {
            "filters": [
                {"field": "date_created", "op": "gte", "value": "2026-01-01"},
                {"field": "campaign_name", "op": "eq", "value": "spring"},
            ]
        }
        RoleFilterService.intersect_body_filters(body, allowed_teams=["Kavanah Team"])
        assert len(body["filters"]) == 2


# ---------------------------------------------------------------------------
# append_scope_filter
# ---------------------------------------------------------------------------


class TestAppendScopeFilter:
    """``RoleFilterService.append_scope_filter`` adds an allow-only
    ``team_title`` filter so scoped queries can never return a row outside
    the user's allowed set."""

    def test_admin_path_is_noop(self):
        body = {"filters": []}
        RoleFilterService.append_scope_filter(body, allowed_teams=None)
        assert body == {"filters": []}

    def test_appends_match_phrase_any_clause(self):
        body = {"filters": []}
        RoleFilterService.append_scope_filter(body, allowed_teams=["Kavanah Team"])
        assert body["filters"] == [
            {
                "field": "team_title",
                "op": "match_phrase_any",
                "value": ["Kavanah Team"],
            }
        ]

    def test_creates_filters_key_if_absent(self):
        body = {}
        RoleFilterService.append_scope_filter(body, allowed_teams=["Kavanah Team"])
        assert "filters" in body
        assert len(body["filters"]) == 1

    def test_appends_alongside_existing_filters(self):
        body = {
            "filters": [
                {"field": "status", "op": "eq", "value": "active"},
            ]
        }
        RoleFilterService.append_scope_filter(body, allowed_teams=["Kavanah Team"])
        assert len(body["filters"]) == 2
        assert body["filters"][0] == {"field": "status", "op": "eq", "value": "active"}

    def test_idempotent_when_called_twice(self):
        body = {"filters": []}
        RoleFilterService.append_scope_filter(body, allowed_teams=["Kavanah Team"])
        RoleFilterService.append_scope_filter(body, allowed_teams=["Kavanah Team"])
        assert len(body["filters"]) == 1

    def test_empty_allowed_teams_appends_no_match_clause(self):
        """A scoped user with an empty allowed-team set should see nothing,
        not bypass the filter — append a clause that returns 0 hits."""
        body = {"filters": []}
        RoleFilterService.append_scope_filter(body, allowed_teams=[])
        assert body["filters"] == [
            {
                "field": "team_title",
                "op": "match_phrase_any",
                "value": [],
            }
        ]

    def test_handles_non_dict_body(self):
        # Should not raise.
        RoleFilterService.append_scope_filter("not a dict", allowed_teams=["Kavanah Team"])
        RoleFilterService.append_scope_filter(None, allowed_teams=["Kavanah Team"])
