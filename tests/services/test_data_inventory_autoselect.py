"""Data Inventory single-workspace scope source.

The front-end auto-selects the Data Inventory workspace switcher when
the user's scope resolves to exactly ONE workspace (workspace owners / org
owners), rather than forcing a manual pick. That auto-select is only safe if
the scope source it reads — ``/api/data-inventory/workspaces``, which returns
``WorkspaceMembershipService.data_grant_and_primary_workspaces`` — genuinely
returns exactly the user's own workspace(s) and never a second tenant's.

These tests pin that scope source (the cross-workspace leak guard behind the
auto-select):

  1. A single-workspace user (one workspace-scope data grant, no extra primary)
     resolves to exactly one workspace_esid — so the front-end's
     ``workspaces.length === 1`` auto-select branch fires for the right users.
  2. A foreign workspace another user holds a grant for never appears — the
     resolver filters grants by ``username``.

Unit-level: ``get_elasticsearch_client`` is mocked, so no real ES / network.

Mutation-verified locally:
  - Make ``data_grant_and_primary_workspaces`` union an out-of-scope workspace
    (e.g. append a hard-coded foreign esid) → the single-workspace count assert
    and the no-leak assert both fail.
"""

from __future__ import annotations

from unittest import mock

import pytest

from asset_management.web.services.workspace_membership_service import (
    WorkspaceMembershipService,
)

_SVC = "asset_management.web.services.workspace_membership_service"


def _fake_es(grant_hits_by_user):
    """Return a fake ES client whose ``search`` answers the data-grant query
    with the hits registered for the queried username, and answers every other
    index (the workspace-role lookups behind ``resolve_curation_workspace`` /
    ``get_primary_workspace``) with no hits.

    ``grant_hits_by_user`` maps username -> list of scope_esid strings.
    """

    def _search(index=None, body=None, **kwargs):
        query = (body or {}).get("query", {})
        filters = query.get("bool", {}).get("filter", [])
        username = None
        for f in filters:
            term = f.get("term", {})
            if "username" in term:
                username = term["username"]
        if index and "data_grant" in index:
            esids = grant_hits_by_user.get(username, [])
            return {"hits": {"hits": [{"_source": {"scope_esid": e}} for e in esids]}}
        # Role / workspace-role lookups: this user holds no role docs, so the
        # primary-workspace fallback contributes nothing beyond the grant.
        return {"hits": {"hits": []}}

    es = mock.MagicMock()
    es.search.side_effect = _search
    return es


def test_single_workspace_user_resolves_to_exactly_one_workspace() -> None:
    """A user with a single workspace-scope data grant (and no other workspace
    role) resolves to exactly one workspace_esid — the case the
    front-end auto-selects."""
    es = _fake_es({"owner_a": ["ws_A"]})
    with mock.patch(f"{_SVC}.get_elasticsearch_client", return_value=es):
        result = WorkspaceMembershipService.data_grant_and_primary_workspaces("owner_a")

    assert result == ["ws_A"], (
        "a single-workspace user must resolve to exactly one "
        f"workspace so the switcher auto-selects, got {result!r}"
    )


def test_scope_source_never_leaks_another_users_workspace() -> None:
    """The resolver filters grants by username — a workspace another user holds
    a grant for must never appear in this user's list (cross-workspace leak
    guard behind the auto-select)."""
    es = _fake_es({"owner_a": ["ws_A"], "owner_b": ["ws_B"]})
    with mock.patch(f"{_SVC}.get_elasticsearch_client", return_value=es):
        result = WorkspaceMembershipService.data_grant_and_primary_workspaces("owner_a")

    assert "ws_B" not in result, "owner_a must never see owner_b's workspace, got " f"{result!r}"
    assert result == ["ws_A"]
