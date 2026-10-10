"""Unit tests for WorkspaceMembershipService.resolve_curation_workspace.

The Dashboard Builder topbar default and the read-only dashboards sidebar
must resolve the SAME workspace, or a Builder menu reorder edits one
workspace while the read-only view renders another's order. This service
method is the shared source of truth: is_primary role -> workspace data
grant -> org-owner org/team-scope walk -> positional
get_primary_workspace fallback.
"""

from __future__ import annotations

from unittest import mock

import pytest

from asset_management.web.services.workspace_membership_service import (
    WorkspaceMembershipService,
)

_MOD = "asset_management.web.services.workspace_membership_service"
_ROLE_IDX = "de_authz_user_workspace_role"
_GRANT_IDX = "de_authz_data_grant"


class _FakeES:
    """Minimal ES double returning canned hits per index.

    ``role_hits`` / ``grant_hits`` are lists of ``_source`` dicts.
    """

    def __init__(self, role_hits=None, grant_hits=None):
        self._role = role_hits or []
        self._grant = grant_hits or []

    def search(self, index, body):
        if index == _ROLE_IDX:
            hits = self._role
        elif index == _GRANT_IDX:
            hits = self._grant
        else:
            hits = []
        return {"hits": {"hits": [{"_source": s} for s in hits]}}


def _run(es, *, positional="positional-ws", org_walk=None):
    """Resolve for jane_doe with leg 4 (positional) and leg 3 (
    org/team-scope walk) controlled. ``org_walk`` is the list
    ``_workspaces_from_org_team_scope`` returns (default empty)."""
    with (
        mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=es),
        mock.patch(
            f"{_MOD}.WorkspaceMembershipService.get_primary_workspace",
            return_value=positional,
        ),
        mock.patch(
            f"{_MOD}.WorkspaceMembershipService._workspaces_from_org_team_scope",
            return_value=list(org_walk or []),
        ),
    ):
        return WorkspaceMembershipService.resolve_curation_workspace("jane_doe")


class TestResolveCurationWorkspace:
    def test_prefers_is_primary_role_over_grant_and_positional(self):
        es = _FakeES(
            role_hits=[{"workspace_esid": "primary-ws"}],
            grant_hits=[{"scope_esid": "grant-ws"}],
        )
        # is_primary role wins over both the data grant and the positional fallback.
        assert _run(es, org_walk=["org-walk-ws"]) == "primary-ws"

    def test_falls_back_to_workspace_grant_when_no_primary_role(self):
        """The jane_doe case: no is_primary role, a kavanah-media grant.

        Without this, resolution would drop to the positional
        get_primary_workspace (an arbitrary [0] role workspace), which is
        the exact builder/read-only mismatch fixes.
        """
        es = _FakeES(role_hits=[], grant_hits=[{"scope_esid": "kavanah-media"}])
        # The grant wins over both the org-walk and the positional fallback.
        assert _run(es, positional="workspace-a", org_walk=["org-walk-ws"]) == "kavanah-media"

    def test_org_owner_walk_resolves_when_no_role_and_no_grant(self):
        """the org-owner case (e.g. test_iprk): no is_primary role,
        no workspace grant, but an org role whose org points at a workspace.
        The org/team-scope walk (leg 3) resolves it BEFORE the positional
        fallback, so the org owner lands in their real workspace instead of
        an empty/arbitrary one.

        Mutation pin: remove the leg-3 org-walk from resolve_curation_workspace
        -> this drops to the positional "workspace-a" and the assertion fails
        (the exact empty-workspace install bug).
        """
        es = _FakeES(role_hits=[], grant_hits=[])
        assert _run(es, positional="workspace-a", org_walk=["kavanah-media"]) == "kavanah-media"

    def test_falls_back_to_positional_when_no_role_grant_or_org_walk(self):
        es = _FakeES(role_hits=[], grant_hits=[])
        # Org walk empty too (a user with truly no resolvable scope) -> positional.
        assert _run(es, positional="workspace-a", org_walk=[]) == "workspace-a"

    def test_none_username_returns_none(self):
        assert WorkspaceMembershipService.resolve_curation_workspace("") is None

    def test_es_unavailable_degrades_to_positional(self):
        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=None),
            mock.patch(
                f"{_MOD}.WorkspaceMembershipService.get_primary_workspace",
                return_value="workspace-a",
            ),
        ):
            assert WorkspaceMembershipService.resolve_curation_workspace("jane_doe") == "workspace-a"
