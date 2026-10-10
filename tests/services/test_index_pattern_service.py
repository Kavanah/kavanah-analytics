"""
Tests for the Index Pattern Service (org-scoped index isolation).

Covers:
  get_index_pattern       — unrestricted, org_admin, deny-all, caching
  validate_index_access   — concrete indices, wildcard patterns, deny cases
  get_allowed_team_prefixes — unrestricted returns None, org_admin returns prefixes
  sanitize_team_title     — shared helper edge cases
"""

from unittest import mock

import pytest

from lib.es_helpers import sanitize_team_title


# ---------------------------------------------------------------------------
# sanitize_team_title (shared utility)
# ---------------------------------------------------------------------------


class TestSanitizeTeamTitle:

    def test_basic(self):
        assert sanitize_team_title("FLK North") == "flk_north"

    def test_hyphens(self):
        assert sanitize_team_title("FLK-North") == "flk_north"

    def test_special_chars(self):
        assert sanitize_team_title("Team (Alpha)") == "team_alpha"

    def test_double_underscores_collapsed(self):
        assert sanitize_team_title("Team  A") == "team_a"

    def test_empty(self):
        assert sanitize_team_title("") == "unknown"

    def test_none(self):
        assert sanitize_team_title(None) == "unknown"

    def test_only_special_chars(self):
        assert sanitize_team_title("@#$") == "unknown"

    def test_leading_trailing_underscores(self):
        assert sanitize_team_title(" Test ") == "test"


# ---------------------------------------------------------------------------
# IndexPatternService — requires Flask app context
# ---------------------------------------------------------------------------


@pytest.fixture
def app_ctx(app):
    """Provide a Flask app and request context for session access."""
    with app.test_request_context():
        yield app


class TestGetIndexPattern:

    def test_superuser_returns_wildcard(self, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import (
            get_index_pattern,
            clear_cached_pattern,
        )

        session["roles"] = ["superuser"]
        clear_cached_pattern()
        assert get_index_pattern() == "*"

    def test_asset_admin_returns_wildcard(self, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import (
            get_index_pattern,
            clear_cached_pattern,
        )

        session["roles"] = ["asset_admin"]
        clear_cached_pattern()
        assert get_index_pattern() == "*"

    def test_regular_user_returns_wildcard(self, app_ctx):
        """Regular org/team users get unrestricted index access — data
        filtering is handled at query level by RoleFilterService."""
        from flask import session
        from asset_management.web.services.index_pattern_service import (
            get_index_pattern,
            clear_cached_pattern,
        )

        session["roles"] = ["org_VOGZ", "team_NFCY"]
        clear_cached_pattern()
        assert get_index_pattern() == "*"

    @mock.patch("asset_management.web.services.role_filter_service.RoleFilterService")
    def test_org_admin_resolves_team_patterns(self, mock_rfs, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import (
            get_index_pattern,
            clear_cached_pattern,
        )

        mock_rfs.extract_org_ids_from_roles.return_value = ["VOGZ"]
        mock_rfs.get_org_titles_by_ids.return_value = ["VOGZ"]
        mock_rfs.get_team_titles_by_organization.return_value = [
            "FLK North",
            "FLK South",
        ]

        session["roles"] = ["org_admin", "org_VOGZ"]
        clear_cached_pattern()
        result = get_index_pattern()
        assert "flk_south_*" in result
        assert "flk_north_*" in result

    @mock.patch("asset_management.web.services.role_filter_service.RoleFilterService")
    def test_org_admin_no_org_role_returns_empty(self, mock_rfs, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import (
            get_index_pattern,
            clear_cached_pattern,
        )

        mock_rfs.extract_org_ids_from_roles.return_value = []

        session["roles"] = ["org_admin"]
        clear_cached_pattern()
        assert get_index_pattern() == ""

    def test_cached_pattern(self, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import (
            get_index_pattern,
        )

        session["roles"] = ["superuser"]
        session["_index_pattern"] = "cached_value"
        assert get_index_pattern() == "cached_value"

    def test_clear_cached_pattern(self, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import (
            clear_cached_pattern,
        )

        session["_index_pattern"] = "old"
        clear_cached_pattern()
        assert "_index_pattern" not in session


class TestValidateIndexAccess:
    """(2026-09-08): ``validate_index_access`` is built on the
    scope resolver. The earlier tests here pinned the org_admin-era
    contract ("regular users and unrestricted roles get True for anything") —
    that contract was the bug: once org_admin was retired the function passed
    every authenticated user. The role → prefix scaffolding it used to read
    (``get_index_pattern`` / ``get_allowed_team_prefixes``) is still exercised
    by the classes around this one; the validator no longer consults it."""

    _SCOPE_A = None

    @staticmethod
    def _scope(*indices):
        from asset_management.web.services.role_filter_service import UserScope

        return UserScope(
            workspace_esids=("ws-a",),
            team_titles=("Team A",),
            team_esids=frozenset({"teama1"}),
            allowed_indices=frozenset(indices),
            scope_source="membership",
        )

    @staticmethod
    def _patch_resolver(scope):
        import sys
        from contextlib import ExitStack

        stack = ExitStack()
        for name, mod in list(sys.modules.items()):
            if name.endswith("role_filter_service") and hasattr(mod, "resolve_user_scope"):
                stack.enter_context(mock.patch(f"{name}.resolve_user_scope", return_value=scope))
        return stack

    def test_concrete_index_in_scope_allowed_foreign_denied(self, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import validate_index_access

        session["username"] = "alice"
        session["roles"] = []
        with self._patch_resolver(self._scope("teama1__fb_insights")):
            assert validate_index_access("teama1__fb_insights") is True
            assert validate_index_access("teamb1__fb_insights") is False

    def test_wildcard_allowed_only_with_an_in_scope_match(self, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import validate_index_access

        session["username"] = "alice"
        session["roles"] = []
        with self._patch_resolver(self._scope("teama1__fb_insights")):
            assert validate_index_access("*__fb_insights") is True
            assert validate_index_access("*__google_ads") is False

    def test_superuser_role_does_not_bypass_the_scope(self, app_ctx):
        """Unrestricted roles used to short-circuit to True. The resolver now
        decides for everyone; a superuser's breadth comes from the resolver
        giving them every index, not from this function trusting a role name."""
        from flask import session
        from asset_management.web.services.index_pattern_service import validate_index_access

        session["username"] = "root"
        session["roles"] = ["superuser"]
        with self._patch_resolver(self._scope("teama1__fb_insights")):
            assert validate_index_access("teamb1__fb_insights") is False

    def test_empty_scope_and_anonymous_are_denied(self, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import validate_index_access
        from asset_management.web.services.role_filter_service import UserScope

        session["username"] = "nobody-yet"
        session["roles"] = []
        with self._patch_resolver(UserScope()):
            assert validate_index_access("teama1__fb_insights") is False
            assert validate_index_access("*__fb_insights") is False

        session.pop("username", None)
        assert validate_index_access("teama1__fb_insights") is False
        assert validate_index_access("") is False
        assert validate_index_access(None) is False

    def test_shared_disciple_tools_pattern_keeps_its_allow(self, app_ctx):
        from asset_management.web.services.index_pattern_service import validate_index_access

        assert validate_index_access("disciple-tools") is True
        assert validate_index_access("disciple-tools-2026") is True


class TestGetAllowedTeamPrefixes:

    def test_unrestricted_returns_none(self, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import (
            get_allowed_team_prefixes,
        )

        session["roles"] = ["superuser"]
        assert get_allowed_team_prefixes() is None

    def test_regular_user_returns_empty(self, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import (
            get_allowed_team_prefixes,
        )

        session["roles"] = ["org_VOGZ"]
        assert get_allowed_team_prefixes() == []

    @mock.patch("asset_management.web.services.role_filter_service.RoleFilterService")
    def test_org_admin_returns_prefixes(self, mock_rfs, app_ctx):
        from flask import session
        from asset_management.web.services.index_pattern_service import (
            get_allowed_team_prefixes,
        )

        mock_rfs.extract_org_ids_from_roles.return_value = ["VOGZ"]
        mock_rfs.get_org_titles_by_ids.return_value = ["VOGZ"]
        mock_rfs.get_team_titles_by_organization.return_value = [
            "FLK North",
            "FLK South",
        ]

        session["roles"] = ["org_admin", "org_VOGZ"]
        prefixes = get_allowed_team_prefixes()
        assert "flk_north" in prefixes
        assert "flk_south" in prefixes
