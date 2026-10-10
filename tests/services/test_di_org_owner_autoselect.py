"""Data Inventory org-owner single-workspace auto-select.

 added the front-end auto-select that fires when
``/api/data-inventory/workspaces`` returns exactly one workspace, and claimed
it covered "workspace owners / org owners". It did NOT: the scope source
``WorkspaceMembershipService.data_grant_and_primary_workspaces`` had only two
legs — workspace-scope data grants, and the primary workspace (an
``is_primary`` workspace ROLE, else positional). An **org owner** holds an org
ROLE (``de_authz_user_org_role``) but typically no workspace-scope grant and no
``is_primary`` workspace role, so both legs returned ``[]`` → the switcher saw
zero workspaces and never auto-selected.

 adds a third leg: walk the user's OWN resolved ``org_esids`` /
``team_esids`` up to their workspace(s), bounded to the user's concrete scope
(never a cluster-wide scan) so a broadly-granted superuser is not widened.

These tests pin that behaviour:

  1. An org owner (org role, no workspace grant, no is_primary role) resolves
     to exactly the one workspace under their org — so the front-end auto-select
     fires for org owners, the case the original test missed.
  2. The new leg is bounded to the user's own scope: a user with no concrete
     org/team scope gets nothing extra from it (the superuser-narrowing guard).

Unit-level: ``get_elasticsearch_client`` is mocked, so no real ES / network.

Mutation-verified locally:
  - Drop the leg-3 ``_workspaces_from_org_team_scope`` call in
    ``data_grant_and_primary_workspaces`` → the org-owner test fails with an
    empty list (the original bug).
"""

from __future__ import annotations

import contextlib
from unittest import mock

import pytest

from asset_management.web.services.workspace_membership_service import (
    WorkspaceMembershipService,
)

_SVC = "asset_management.web.services.workspace_membership_service"


def _rfs_module():
    """The role_filter_service module object the production helper actually
    imports from. ``_workspaces_from_org_team_scope`` tries ``web.services``
    first (the ``/app/python`` runtime path that conftest also puts on
    sys.path), then falls back to ``asset_management.web.services``. Patch on
    whichever one resolves so the patch lands on the name the code looks up."""
    try:
        import web.services.role_filter_service as rfs  # type: ignore
    except ImportError:  # pragma: no cover - depends on sys.path shape
        import asset_management.web.services.role_filter_service as rfs  # type: ignore
    return rfs


@contextlib.contextmanager
def _patch_scope(scope, es):
    """Patch the role_filter_service module so leg 3 runs the real
    org→workspace ES walk against the fake ``es``:

      * ``resolve_user_scope`` → the org-owner scope shape (so we don't stand
        up the full role-index resolver), and
      * ``load_config`` → {} (the resolver is mocked anyway), and
      * ``get_elasticsearch_client`` → the fake ``es`` (the walk helper
        ``_workspace_esids_from_scope_esids`` calls THIS module's client, not
        workspace_membership_service's — patching only the latter left the walk
        on a real/unreachable client and it returned nothing)."""
    rfs = _rfs_module()
    with (
        mock.patch.object(rfs, "resolve_user_scope", return_value=scope),
        mock.patch.object(rfs.RoleFilterService, "load_config", return_value={}),
        mock.patch.object(rfs, "get_elasticsearch_client", return_value=es),
    ):
        yield


def _fake_es_org_owner(org_esid: str, workspace_esid: str):
    """Fake ES for an ORG OWNER: no workspace-scope grants, no workspace role,
    one org role that walks up to ``workspace_esid``.

    Routes the indices ``data_grant_and_primary_workspaces`` + its leg-3 walk
    touch:

      * data_grant (workspace-scope) → no hits (org owner has no ws grant)
      * de_authz_user_workspace_role → no hits (no is_primary / positional ws)
      * de_authz_user_org_role → the org role doc carrying ``org_esid``
      * de_authz_user_team_role → no hits
      * de_asset_organization (ids walk) → org doc carrying ``workspace_esid``
      * de_asset_organization (terms workspace_esid, _workspace_scoped_titles) →
        no hits (the membership-gated path contributes nothing for an org owner
        with no workspace membership)
    """

    def _search(index=None, body=None, **kwargs):
        idx = index or ""
        body = body or {}
        query = body.get("query", {})

        # Org-role lookup → one role doc with org_esid.
        if "user_org_role" in idx:
            return {"hits": {"hits": [{"_id": f"{org_esid}:role", "_source": {"org_esid": org_esid}}]}}
        # No team roles, no workspace roles, no other role indices.
        if "user_team_role" in idx or "workspace_role" in idx:
            return {"hits": {"hits": []}}
        # No workspace-scope data grants.
        if "data_grant" in idx:
            return {"hits": {"hits": []}}
        # Org → workspace walk: the ESID-based `ids` query the leg-3 helper runs.
        if idx == "de_asset_organization" and "ids" in query:
            return {"hits": {"hits": [{"_id": org_esid, "_source": {"workspace_esid": workspace_esid}}]}}
        # Membership-gated workspace→org expansion (terms workspace_esid): the
        # org owner holds no workspace membership, so nothing here.
        return {"hits": {"hits": []}}

    es = mock.MagicMock()
    es.search.side_effect = _search
    return es


def _patch_scope_for_org_owner(org_esid: str):
    """Patch ``resolve_user_scope`` so leg 3 sees exactly one org_esid and no
    team_esids — the org-owner shape — without standing up the full role-index
    resolver. The org→workspace ES walk itself stays real (driven by the fake
    ES above), so the test still exercises the walk, not a stub of it."""
    scope = mock.MagicMock()
    scope.org_esids = frozenset({org_esid})
    scope.team_esids = frozenset()
    return scope


def test_org_owner_resolves_to_their_single_workspace() -> None:
    """An org owner (org role only, no workspace grant, no is_primary workspace
    role) resolves to exactly the one workspace under their org — so the
    front-end auto-selects it."""
    es = _fake_es_org_owner(org_esid="org_X", workspace_esid="ws_X")
    scope = _patch_scope_for_org_owner("org_X")

    with mock.patch(f"{_SVC}.get_elasticsearch_client", return_value=es), _patch_scope(scope, es):
        result = WorkspaceMembershipService.data_grant_and_primary_workspaces("org_owner")

    assert result == ["ws_X"], (
        "an org owner must resolve to exactly the one workspace under "
        f"their org so the Data Inventory switcher auto-selects, got {result!r}"
    )


def test_org_team_walk_is_bounded_to_users_own_scope() -> None:
    """The new leg contributes nothing for a user with no concrete org/team
    scope — it walks the user's OWN esids, never a cluster-wide scan, so it
    cannot widen a broadly-granted superuser's switcher to every workspace."""
    es = _fake_es_org_owner(org_esid="org_X", workspace_esid="ws_X")
    empty_scope = mock.MagicMock()
    empty_scope.org_esids = frozenset()
    empty_scope.team_esids = frozenset()

    with mock.patch(f"{_SVC}.get_elasticsearch_client", return_value=es), _patch_scope(empty_scope, es):
        result = WorkspaceMembershipService.data_grant_and_primary_workspaces("broad_superuser")

    assert result == [], (
        "a user with no concrete org/team scope must get no extra "
        f"workspace from the org/team walk (no cluster-wide widening), got {result!r}"
    )
