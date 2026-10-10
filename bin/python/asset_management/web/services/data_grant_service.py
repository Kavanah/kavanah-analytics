"""
Data Grant Service

Persistent per-user, per-scope data-access grants. ``RoleFilterService``
consults this index when resolving a superuser's query filter — without
an active grant for the queried scope, the superuser sees the deny-all
sentinel that yields zero hits.

Two consumer classes today:

* **Internal staff support** — granted explicitly via the (forthcoming)
  CLI / API to view one tenant's data during a support session. Pair
  with ``POST /admin/elevate`` (Layer B) to also unlock the raw Kibana
  surface for the same scope.
* **Agency self-service** — will wire the agency wizard step to
  call ``DataGrantService.create`` for each client org an agency
  provisions, so agency users see all their clients on the dashboard
  without per-org elevation prompts.

Storage substrate is the ``de_authz_data_grant`` index bootstrapped by
``PermissionsIndexService``. Doc id is deterministic
(``{username}:{scope_type}:{scope_esid}``), so a duplicate
``create`` against the same triple is the idempotent no-op path via
``op_type=create`` + 409-as-success. Mirrors the
``onboarding_service._safe_create`` pattern.

Audit-logged via the existing ``de_audit_log`` index with actions
``permission.data_grant.create`` and ``permission.data_grant.revoke``,
matching the namespace ratified in
``the private docs`` §2 Q4.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client

try:
    from web.services.audit_log_service import AuditLogService
    from web.services.permissions_index_service import DATA_GRANT_INDEX
except ImportError:
    from asset_management.web.services.audit_log_service import AuditLogService
    from asset_management.web.services.permissions_index_service import DATA_GRANT_INDEX


SCOPE_TYPE_ORG = "org"
SCOPE_TYPE_TEAM = "team"
# / F4: workspace-tier grants. Registered now so the constant is
# available to F7 (provision_*) and the future bootstrap that collapses
# the existing ~80 team-scoped grants into ~4 workspace-scoped ones. No
# consumers in F4 itself — RoleFilterService still routes ORG/TEAM.
SCOPE_TYPE_WORKSPACE = "workspace"
VALID_SCOPE_TYPES = (SCOPE_TYPE_ORG, SCOPE_TYPE_TEAM, SCOPE_TYPE_WORKSPACE)


class NonSuperuserGrantTargetError(ValueError):
    """raised by ``DataGrantService.create`` when the target
    username does NOT carry the ``superuser`` role.

    Grants exist only as a cross-workspace audit-tracked mechanism for
    cluster operators (superusers). Non-superusers gain workspace
    access via memberships or role assignments, not grants. This is a
    subclass of ``ValueError`` so callers using a broad
    ``except ValueError`` (input-validation path) catch it naturally;
    callers that want to surface the specific reason can
    ``except NonSuperuserGrantTargetError`` first.
    """


def _doc_id(username: str, scope_type: str, scope_esid: str) -> str:
    """Deterministic doc id used for op_type=create idempotency."""
    return f"{username}:{scope_type}:{scope_esid}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


class DataGrantService:
    """CRUD over ``de_authz_data_grant`` with audit-log integration."""

    @staticmethod
    def create(
        *,
        username: str,
        scope_type: str,
        scope_esid: str,
        granted_by: str,
        reason: str,
        expires_at: str | None = None,
    ) -> bool:
        """Write a grant. Returns True if a new doc was created, False
        if the grant already existed (idempotent no-op).

        The idempotency contract matters for agency provisioning
         which re-calls ``create`` on every wizard refresh.
        The 409 path must NOT write a duplicate audit-log row; that
        would flood ``de_audit_log`` with spurious ``permission.data_grant.create``
        rows on every page load.
        """
        if scope_type not in VALID_SCOPE_TYPES:
            raise ValueError(f"scope_type must be one of {VALID_SCOPE_TYPES}, got {scope_type!r}")
        if not username or not scope_esid:
            raise ValueError("username and scope_esid must be non-empty")

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("DataGrantService.create: no ES client; grant not written")
            return False

        # defense-in-depth. The api_create_grant route gates
        # this already, but an internal caller writing through the
        # service must hit the same constraint. Grants are
        # cluster-operator audit trail; non-superuser targets get
        # workspace access via memberships, not grants. The route
        # surface translates this exception to a 400 with a clear
        # user-facing message.
        try:
            target = dict(es.security.get_user(username=username)).get(username, {})
        except Exception as e:
            # Treat lookup failure the same as "not a superuser" —
            # better to refuse the write than allow it on degraded info.
            logging.warning("service: target lookup failed for %s: %s", username, e)
            target = {}
        if "superuser" not in (target.get("roles") or []):
            raise NonSuperuserGrantTargetError(f"Data grants can only target superusers; {username!r} is not.")

        doc = {
            "username": username,
            "scope_type": scope_type,
            "scope_esid": scope_esid,
            "granted_by": granted_by,
            "granted_at": _now_iso(),
            "expires_at": expires_at,
            "reason": reason,
        }
        try:
            es.index(
                index=DATA_GRANT_INDEX,
                id=_doc_id(username, scope_type, scope_esid),
                document=doc,
                op_type="create",
                refresh="wait_for",
            )
        except Exception as e:
            # version_conflict / 409 = grant already exists — happy idempotent path.
            # Re-raise everything else.
            if "version_conflict" in str(e).lower() or "conflict" in str(e).lower():
                return False
            raise

        AuditLogService.log_event(
            granted_by,
            "permission.data_grant.create",
            True,
            target_user=username,
            detail=f"scope={scope_type}:{scope_esid}, reason={reason}",
        )
        return True

    @staticmethod
    def revoke(
        *,
        username: str,
        scope_type: str,
        scope_esid: str,
        revoked_by: str,
    ) -> bool:
        """Delete a grant. Returns True if a doc was deleted, False if
        no grant existed (idempotent — already-revoked is a no-op, no
        audit-log row written).

        The no-audit-on-already-revoked rule mirrors ``create``'s
        no-audit-on-409 rule. Replaying a revoke from a script should
        not write a phantom ``permission.data_grant.revoke`` row.
        """
        if scope_type not in VALID_SCOPE_TYPES:
            raise ValueError(f"scope_type must be one of {VALID_SCOPE_TYPES}, got {scope_type!r}")

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("DataGrantService.revoke: no ES client; grant not deleted")
            return False

        try:
            es.delete(
                index=DATA_GRANT_INDEX,
                id=_doc_id(username, scope_type, scope_esid),
                refresh="wait_for",
            )
        except Exception as e:
            if "not_found" in str(e).lower() or "404" in str(e):
                return False
            raise

        AuditLogService.log_event(
            revoked_by,
            "permission.data_grant.revoke",
            True,
            target_user=username,
            detail=f"scope={scope_type}:{scope_esid}",
        )
        return True

    @staticmethod
    def list_for_user(username: str) -> list[dict]:
        """Return all active grants for the given username.

        ``Active`` means: doc exists AND either ``expires_at`` is null
        OR ``expires_at`` is a future ISO timestamp. Returns an empty
        list on ES outage so ``RoleFilterService`` fails closed (zero
        active grants → deny-all sentinel).
        """
        es = get_elasticsearch_client()
        if es is None:
            return []

        try:
            resp = es.search(
                index=DATA_GRANT_INDEX,
                body={
                    "query": {"term": {"username": username}},
                    "size": 1000,
                },
            )
        except Exception as e:
            logging.warning("DataGrantService.list_for_user: ES search failed: %s", e)
            return []

        now_iso = _now_iso()
        grants: list[dict] = []
        for hit in resp.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            expires_at = src.get("expires_at")
            if expires_at and expires_at < now_iso:
                # Expired — treat as if it doesn't exist.
                continue
            grants.append(src)
        return grants

    @staticmethod
    def has_active_grant(*, username: str, scope_type: str, scope_esid: str) -> bool:
        """Cheap lookup used by /admin/elevate to refuse self-grant
        bypass: you cannot elevate to a scope you do not already hold
        a persistent grant for.
        """
        if scope_type not in VALID_SCOPE_TYPES:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            resp = es.get(
                index=DATA_GRANT_INDEX,
                id=_doc_id(username, scope_type, scope_esid),
            )
        except Exception as e:
            if "not_found" in str(e).lower() or "404" in str(e):
                return False
            logging.warning("DataGrantService.has_active_grant: ES get failed: %s", e)
            return False
        src = resp.get("_source", {})
        expires_at = src.get("expires_at")
        if expires_at and expires_at < _now_iso():
            return False
        return True
