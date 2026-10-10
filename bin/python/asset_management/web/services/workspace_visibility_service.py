"""Workspace visibility lookups for the Asset Explorer's Workspaces tier.

The Workspaces tier exposes a privacy-asymmetric contract: even a
system_admin sees only the workspaces they are a **member of** OR that
they **created**. Never the full workspace list. This is the Kavanah-
agreed rule that prevents superusers from enumerating every tenant's
workspace through the Asset Explorer.

The visibility set is the UNION of two legs:

* **Membership** — ``de_authz_workspace_membership`` doc id
  ``{username}:{workspace_esid}``. Already provided by
  :class:`WorkspaceMembershipService.get_workspaces_for_user`.
* **Creator** — ``de_asset_workspace.created_by == username``. Belt-and-
  suspenders for edge cases where a creator was removed from a workspace
  they made, or where the membership doc never landed.

On workspace create the Asset Explorer auto-writes a membership doc for
the creator, so in practice both legs are true together on day
1. The creator-only leg matters for: (a) the Kavanah singleton
(``created_by="system_migration"`` so no human admin sees it through the
creator leg), (b) a workspace whose creator was later removed but is
still recorded as ``created_by``.

The service is fail-soft: an ES outage returns an empty set. The route
handler in :func:`list_assets` maps an empty set to "show nothing" rather
than "show everything", consistent with the
``current_user_has_any_scope`` semantic.
"""

from __future__ import annotations

import logging
import sys

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client

try:
    from web.services.permissions_index_service import WORKSPACE_INDEX
    from web.services.workspace_membership_service import WorkspaceMembershipService
except ImportError:
    from asset_management.web.services.permissions_index_service import WORKSPACE_INDEX
    from asset_management.web.services.workspace_membership_service import (
        WorkspaceMembershipService,
    )


class WorkspaceVisibilityService:
    """Resolve the set of workspace ESIDs a given user is allowed to see."""

    @staticmethod
    def get_visible_workspaces_for_user(username: str, *, is_superuser: bool = False) -> set[str]:
        """Return workspace ESIDs the user can see in the Workspaces tier.

        For NON-superusers (default): ``(member-of) ∪ (created-by)``.

        For SUPERUSERS: ``(created-by) ∪ (active workspace-scoped
        grants)``. The membership leg is intentionally retired for
        superusers — backfill-time membership docs implicitly
        granted tier access that may never have been an explicit operator
        decision. Under the asymmetric-privacy contract, every cross-
        workspace superuser access must be auditable as either:

        * "I created this workspace" (``de_asset_workspace.created_by``)
        * "I have an audit-tracked grant" (``de_authz_data_grant`` row
          with ``scope_type='workspace'``, ``reason``, ``granted_by``,
          optional ``expires_at``)

        Non-superusers keep the (member-of) ∪ (created-by) rule — for
        them, membership IS the access mechanism.

        Returns an empty set on unknown user, no visible workspaces, or
        ES outage. The Asset Explorer's list/tree branches treat the
        empty set as deny-all (zero workspaces visible), matching the
        broader privacy contract.

        Default ``is_superuser=False`` preserves call sites that haven't
        been updated yet (gradual rollout safety — updates all
        5 known call sites in the same commit).
        """
        if not username:
            return set()

        # Creator leg — a single ES term query on workspace_doc.created_by.
        # Same for both superuser and non-superuser paths.
        created_by: list[str] = []
        es = get_elasticsearch_client()
        if es is not None:
            try:
                resp = es.search(
                    index=WORKSPACE_INDEX,
                    body={
                        "query": {"term": {"created_by": username}},
                        "size": 1000,
                        "_source": False,
                    },
                )
                created_by = [hit["_id"] for hit in resp.get("hits", {}).get("hits", [])]
            except Exception as e:
                # Fail soft — log and degrade to the other leg.
                logging.warning(
                    "WorkspaceVisibilityService.get_visible_workspaces_for_user(%s): " "created_by query failed: %s",
                    username,
                    e,
                )

        if is_superuser:
            # superuser visibility = created-by ∪ active
            # workspace-scoped grants. The DataGrantService already
            # filters expired grants (data_grant_service.py:243) so
            # ``list_for_user`` returns only active rows.
            try:
                try:
                    from web.services.data_grant_service import (
                        DataGrantService,
                        SCOPE_TYPE_WORKSPACE,
                    )
                except ImportError:
                    from asset_management.web.services.data_grant_service import (
                        DataGrantService,
                        SCOPE_TYPE_WORKSPACE,
                    )
                grant_workspace_esids = {
                    g["scope_esid"]
                    for g in DataGrantService.list_for_user(username)
                    if g.get("scope_type") == SCOPE_TYPE_WORKSPACE and g.get("scope_esid")
                }
            except Exception as e:
                logging.warning(
                    "WorkspaceVisibilityService.get_visible_workspaces_for_user(%s): " "grant lookup failed: %s",
                    username,
                    e,
                )
                grant_workspace_esids = set()
            return set(created_by) | grant_workspace_esids

        # Non-superuser path: (member-of) ∪ (created-by) — unchanged.
        member_of: list[str] = WorkspaceMembershipService.get_workspaces_for_user(username)
        return set(member_of) | set(created_by)
