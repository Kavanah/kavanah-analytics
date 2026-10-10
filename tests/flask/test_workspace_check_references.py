"""
check-references must gate the workspaces asset type (WEB-004).

Before the fix, ``check_asset_references`` gated teams/organizations
 and the data-source types but ``workspaces`` fell
through both branches — ``"workspaces"`` is in ``_DATA_SOURCE_SYSTEM_TYPES``
so the branch never fires, and the branch names only
teams/organizations. Since the workspace ``_id`` is its slug (,
guessable) and the dependents payload names every team/org/data-source
asset wired to the workspace, any caller passing route authorization could
enumerate another tenant's org, team and asset names.

After the fix the route mirrors the sibling routes (list_assets /
get_asset_details): the type-level gate (``_workspaces_visible_to``)
plus a per-object check against
``WorkspaceVisibilityService.get_visible_workspaces_for_user``.

Mutation pins:
  M1. Remove the workspaces branch -> ``test_foreign_workspace_404s`` fails.
  M2. Fail-open on visibility-service error -> ``test_lookup_error_fails_closed``
      fails.
"""

from unittest import mock

import pytest

# DoD gate — release-plan coverage for the cycle.
pytestmark = []


_APP = "asset_management.web.app"
_VIS = (
    "asset_management.web.services.workspace_visibility_service."
    "WorkspaceVisibilityService.get_visible_workspaces_for_user"
)
_REFSVC = "asset_management.web.services.reference_cleanup_service.ReferenceCleanupService"


@pytest.fixture
def workspaces_tier_visible():
    """Caller passes the type-level gate (is a system admin)."""
    with mock.patch(f"{_APP}._workspaces_visible_to", return_value=True):
        yield


class TestCheckReferencesWorkspaceGate:

    def test_foreign_workspace_404s(self, auth_client, workspaces_tier_visible):
        # M1 pin: a guessed slug outside the caller's visible set must 404
        # without ever computing dependents.
        with mock.patch(_VIS, return_value={"ws-own"}), \
             mock.patch(f"{_REFSVC}.get_dependents") as dep, \
             mock.patch(f"{_REFSVC}.get_dependent_docs") as docs:
            resp = auth_client.get("/asset/workspaces/check-references/ws-victim")
        assert resp.status_code == 404
        dep.assert_not_called()
        docs.assert_not_called()

    def test_own_workspace_returns_dependents(self, auth_client, workspaces_tier_visible):
        with mock.patch(_VIS, return_value={"ws-own"}), \
             mock.patch(f"{_REFSVC}.get_dependents", return_value={"de_asset_team": 2}), \
             mock.patch(f"{_REFSVC}.get_dependent_docs", return_value=[]):
            resp = auth_client.get("/asset/workspaces/check-references/ws-own")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["dependents"] == {"de_asset_team": 2}

    def test_non_admin_tier_gate_404s(self, auth_client):
        # type-level gate: when the Workspaces tier is not visible to
        # the caller at all, even a workspace they belong to is unreachable
        # here (mirrors list_assets — indistinguishable from wrong asset type).
        with mock.patch(f"{_APP}._workspaces_visible_to", return_value=False), \
             mock.patch(_VIS, return_value={"ws-own"}), \
             mock.patch(f"{_REFSVC}.get_dependents") as dep:
            resp = auth_client.get("/asset/workspaces/check-references/ws-own")
        assert resp.status_code == 404
        dep.assert_not_called()

    def test_lookup_error_fails_closed(self, auth_client, workspaces_tier_visible):
        # M2 pin: an unavailable visibility service must deny, not leak.
        with mock.patch(_VIS, side_effect=RuntimeError("es down")), \
             mock.patch(f"{_REFSVC}.get_dependents") as dep:
            resp = auth_client.get("/asset/workspaces/check-references/ws-own")
        assert resp.status_code == 404
        dep.assert_not_called()

    def test_teams_gate_unaffected(self, auth_client):
        # Regression guard: the teams/orgs branch still runs — the new
        # workspaces branch must not swallow or reorder it.
        with mock.patch(f"{_APP}.AssetService.get_by_id", return_value=None):
            resp = auth_client.get("/asset/teams/check-references/t-victim")
        assert resp.status_code == 404
