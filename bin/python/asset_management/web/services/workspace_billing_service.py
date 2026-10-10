"""Workspace Billing Service.

Billing is a property of the WORKSPACE (an agency or company), not the
individual user: the workspace pays once and every member/org/team under it is
covered (or impacted on expiry). See
``the private docs`` and the 2026-06-08 entry
in ``the private docs``.

PHASE 1 is INERT — this module is added and unit-tested, but NO existing
call-site is rewired yet (get_state, the import gates, pay/comp/cancel still
run per-user). Phase 2 re-points ``SubscriptionStateService.get_state`` to
resolve a member's workspace and read state from here.

This service owns:
  * ``resolve_workspace_esid(username)`` — a member's BILLING workspace: their
    primary ``de_authz_user_workspace_role`` row, else org/team → workspace.
  * the ``de_billing_workspace`` record (one doc per workspace_esid) holding the
    same fields today's per-user metadata holds.
  * ``get_workspace_state(workspace_esid)`` — the trial/active/lapsed/canceled/
    past_due/comp_revoked verdict for a workspace (same shape + derivation as
    ``SubscriptionStateService.get_state``, minus the superuser short-circuit
    which stays role-based and per-user in the caller).
  * write helpers (set/clear comp, apply stripe status) keyed by workspace.

Fail-open posture (decision 2026-06-08): ``resolve_workspace_esid`` returns
None on any unresolvable/erroring path so the caller can keep today's
fail-open gate behavior (a resolution blip never blocks a paying workspace).
"""

from __future__ import annotations

import logging
import sys
from datetime import timedelta

sys.path.insert(0, "/app/python")

from lib.es_helpers import (  # noqa: E402
    get_elasticsearch_client,
    put_regular_index_template,
    create_index_if_not_exists,
)

# Reuse the per-user service's constants + state machine so the workspace
# verdict can never drift from the per-user one (Phase 2 converges them fully).
from asset_management.web.services.subscription_state_service import (  # noqa: E402
    PAID_TIER_KEY,
    TRIAL_STARTED_AT_KEY,
    COMPED_ACTIVE_KEY,
    COMPED_BY_KEY,
    COMPED_AT_KEY,
    COMP_REVOKED_AT_KEY,
    STRIPE_STATUS_KEY,
    STRIPE_CUSTOMER_ID_KEY,
    ADDON_CONNECTIONS_KEY,
    ADDON_REMOVE_AT_KEY,
    ADDON_PENDING_QTY_KEY,
    TRIAL_ENDING_EMAIL_SENT_KEY,
    STATE_TRIAL,
    STATE_ACTIVE,
    STATE_LAPSED,
    STATE_COMP_REVOKED,
    _STRIPE_STATUS_TO_STATE,
    CONNECTION_CAP,
    ACCOUNT_CAP_PER_CONNECTION,
    TRIAL_SOURCE_CAP,
    TRIAL_ACCOUNT_CAP,
    SubscriptionStateService,
)

# One billing doc per workspace, doc id == workspace_esid. Fields mirror the
# per-user metadata keys so the migration (Phase 4) is a straight copy.
INDEX_WORKSPACE_BILLING = "de_billing_workspace"

USER_WORKSPACE_ROLE_INDEX = "de_authz_user_workspace_role"
USER_ORG_ROLE_INDEX = "de_authz_user_org_role"
USER_TEAM_ROLE_INDEX = "de_authz_user_team_role"

WORKSPACE_BILLING_MAPPING = {
    "properties": {
        "workspace_esid": {"type": "keyword"},
        "paid_tier": {"type": "boolean"},
        "trial_started_at": {"type": "date"},
        "comped_active": {"type": "boolean"},
        "comped_by": {"type": "keyword"},
        "comped_at": {"type": "date"},
        "comp_revoked_at": {"type": "date"},
        "stripe_status": {"type": "keyword"},
        "stripe_customer_id": {"type": "keyword"},
        # (2026-06-12) — purchased add-on connections live on the
        # WORKSPACE doc for workspace-billed orgs (the per-user metadata stamp
        # is shadowed by the workspace overlay in get_state).
        "addon_connections": {"type": "integer"},
        "addon_remove_at": {"type": "date"},
        "addon_pending_qty": {"type": "integer"},
        "trial_ending_email_sent_at": {"type": "date"},
        "updated_at": {"type": "date"},
    }
}


class WorkspaceBillingService:
    """Per-workspace billing state + member→workspace resolution (Phase 1)."""

    # ── Index bootstrap ─────────────────────────────────────────────────
    @staticmethod
    def ensure_index_exists() -> None:
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("WorkspaceBillingService: could not connect to ES")
            return
        put_regular_index_template(es, INDEX_WORKSPACE_BILLING, WORKSPACE_BILLING_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX_WORKSPACE_BILLING, WORKSPACE_BILLING_MAPPING)
        try:
            es.indices.put_mapping(index=INDEX_WORKSPACE_BILLING, body=WORKSPACE_BILLING_MAPPING)
        except Exception as e:  # noqa: BLE001
            logging.warning("WorkspaceBillingService: put_mapping on %s: %s", INDEX_WORKSPACE_BILLING, e)

    # ── Member → billing workspace resolution ───────────────────────────
    @staticmethod
    def resolve_workspace_esid(username: str) -> str | None:
        """Return the workspace_esid that BILLS ``username``, or None.

        Order (decision 2026-06-08): the member's PRIMARY workspace role wins;
        else any workspace role; else walk their org/team scopes up to a
        workspace. None when nothing resolves (caller fails open). Best-effort:
        ES errors return None.
        """
        if not username:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None

        # 1) Direct workspace role — prefer the primary one.
        try:
            resp = es.search(
                index=USER_WORKSPACE_ROLE_INDEX,
                body={
                    "query": {"term": {"username": username}},
                    "_source": ["workspace_esid", "is_primary"],
                    "size": 100,
                },
            )
            rows = [h.get("_source") or {} for h in resp.get("hits", {}).get("hits", [])]
            primary = [r["workspace_esid"] for r in rows if r.get("is_primary") and r.get("workspace_esid")]
            if primary:
                return primary[0]
            any_ws = sorted({r["workspace_esid"] for r in rows if r.get("workspace_esid")})
            if any_ws:
                return WorkspaceBillingService._pick_billing_candidate(es, username, any_ws, source="workspace-role")
        except Exception as e:  # noqa: BLE001
            logging.warning("WorkspaceBillingService.resolve(%r): workspace-role lookup failed: %s", username, e)

        # 2) Org / team scopes → workspace (reuse the role-filter walk).
        try:
            org_esids = WorkspaceBillingService._scope_esids(es, USER_ORG_ROLE_INDEX, "org_esid", username)
            team_esids = WorkspaceBillingService._scope_esids(es, USER_TEAM_ROLE_INDEX, "team_esid", username)
            if org_esids or team_esids:
                from asset_management.web.services.role_filter_service import (
                    _workspace_esids_from_scope_esids,
                )

                ws = _workspace_esids_from_scope_esids(frozenset(org_esids), frozenset(team_esids))
                if ws:
                    return WorkspaceBillingService._pick_billing_candidate(es, username, sorted(ws), source="org/team-walk")
        except Exception as e:  # noqa: BLE001
            logging.warning("WorkspaceBillingService.resolve(%r): org/team walk failed: %s", username, e)

        return None

    @staticmethod
    def _pick_billing_candidate(es, username: str, candidates: list[str], *, source: str) -> str:
        """choose the billing workspace when no role is flagged
        ``is_primary`` and the user spans several workspaces.

        The old fallback was ``sorted(candidates)[0]`` — deterministic but
        arbitrary and SILENT, and the choice lands on real money (which
        workspace Stripe charges). Onboarding stamps ``is_primary`` on the
        user's first workspace role and admin-assigned roles are
        deliberately non-primary, so this branch is reachable for users whose
        rows were all admin-assigned or pre-date the stamp.

        Order:
        1. one candidate → it (unambiguous; no noise);
        2. exactly ONE candidate already holds a billing record
           (``de_billing_workspace`` doc with a ``stripe_customer_id`` or an
           active comp) → billing follows where billing already lives;
        3. otherwise the alphabetical pick survives as the last resort, but
           it now logs a WARNING naming the user, the pick, and the losing
           candidates — the silence was the bug.
        """
        if len(candidates) == 1:
            return candidates[0]
        billed = []
        try:
            resp = es.search(
                index=INDEX_WORKSPACE_BILLING,
                body={
                    "size": len(candidates),
                    "query": {"terms": {"workspace_esid": candidates}},
                    "_source": ["workspace_esid", "stripe_customer_id", "comped_active"],
                },
            )
            for h in resp.get("hits", {}).get("hits", []):
                src = h.get("_source") or {}
                if src.get("stripe_customer_id") or src.get("comped_active"):
                    ws = src.get("workspace_esid")
                    if ws in candidates:
                        billed.append(ws)
        except Exception as e:  # noqa: BLE001 — preference probe is best-effort
            logging.warning("WorkspaceBillingService._pick_billing_candidate(%r): billing probe failed: %s", username, e)
        if len(set(billed)) == 1:
            logging.info(
                "WorkspaceBillingService.resolve(%r): no is_primary role (%s); picked %s — "
                "the only candidate with an existing billing record (candidates: %s).",
                username,
                source,
                billed[0],
                candidates,
            )
            return billed[0]
        pick = candidates[0]
        logging.warning(
            "WorkspaceBillingService.resolve(%r): no is_primary role (%s) and no single "
            "billed candidate — falling back to alphabetical pick %s from %s. Set is_primary on "
            "the intended workspace role to make this deliberate.",
            username,
            source,
            pick,
            candidates,
        )
        return pick

    @staticmethod
    def _scope_esids(es, index: str, field: str, username: str) -> set[str]:
        """Collect a user's org_esid / team_esid scope ids from a role index."""
        try:
            resp = es.search(
                index=index,
                body={"query": {"term": {"username": username}}, "_source": [field], "size": 1000},
            )
        except Exception:  # noqa: BLE001 — fail-soft
            return set()
        out = set()
        for h in resp.get("hits", {}).get("hits", []):
            val = (h.get("_source") or {}).get(field)
            if isinstance(val, list):
                out.update(v for v in val if v)
            elif val:
                out.add(val)
        return out

    @staticmethod
    def find_workspace_owner(workspace_esid: str) -> str | None:
        """The workspace's billing owner — the username holding
        ``workspace_owner`` on this workspace. None if absent / on error."""
        if not workspace_esid:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            resp = es.search(
                index=USER_WORKSPACE_ROLE_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"workspace_esid": workspace_esid}},
                                {"term": {"role_esid": "workspace_owner"}},
                            ]
                        }
                    },
                    "_source": ["username"],
                    "size": 1,
                },
            )
            hits = resp.get("hits", {}).get("hits", [])
            return (hits[0].get("_source") or {}).get("username") if hits else None
        except Exception as e:  # noqa: BLE001
            logging.warning("WorkspaceBillingService.find_workspace_owner(%r): %s", workspace_esid, e)
            return None

    @staticmethod
    def find_workspace_by_stripe_customer(customer_id: str) -> str | None:
        """Map a Stripe customer id → workspace_esid via the stamped
        ``stripe_customer_id`` on the workspace billing record (Phase 3 —
        webhook). A direct term query (the field is indexed keyword), unlike
        the per-user full-scan. None on no match / error."""
        if not customer_id:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            resp = es.search(
                index=INDEX_WORKSPACE_BILLING,
                body={
                    "query": {"term": {STRIPE_CUSTOMER_ID_KEY: customer_id}},
                    "_source": ["workspace_esid"],
                    "size": 1,
                },
            )
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return None
            # Doc id IS the workspace_esid, but read the field for clarity.
            return (hits[0].get("_source") or {}).get("workspace_esid") or hits[0].get("_id")
        except Exception as e:  # noqa: BLE001
            logging.warning("WorkspaceBillingService.find_workspace_by_stripe_customer(%r): %s", customer_id, e)
            return None

    @staticmethod
    def cancel_stripe_for_workspace(workspace_esid: str) -> dict:
        """Cancel a workspace's live Stripe subscription IMMEDIATELY (Phase 3 —
        used when a workspace is comped, so it isn't charged while comped).
        Best-effort + never raises; returns ``{"mode": canceled|no_sub|error}``.
        Reads the stamped ``stripe_customer_id`` off the workspace record.
        Mirrors ``SubscriptionStateService.cancel_stripe_for_user``."""
        from asset_management.web.config import config

        if not config.STRIPE_LIVE_PAYMENTS or not config.STRIPE_SECRET_KEY:
            return {"mode": "no_sub", "detail": "live payments off"}
        if not workspace_esid:
            return {"mode": "no_sub", "detail": "no workspace"}
        es = get_elasticsearch_client()
        if es is None:
            return {"mode": "no_sub", "detail": "es unavailable"}
        customer_id = WorkspaceBillingService._get_billing_doc(es, workspace_esid).get(STRIPE_CUSTOMER_ID_KEY) or ""
        if not customer_id:
            return {"mode": "no_sub", "detail": "no stripe customer"}
        try:
            from asset_management.web.services.stripe_service import StripeService, StripeError

            summary = StripeService.get_subscription_summary(secret_key=config.STRIPE_SECRET_KEY, customer_id=customer_id)
            subscription_id = (summary or {}).get("subscription_id")
            status = (summary or {}).get("status")
            if not subscription_id or status in ("canceled", "incomplete_expired"):
                return {"mode": "no_sub", "detail": f"status={status}"}
            StripeService.cancel_subscription(
                secret_key=config.STRIPE_SECRET_KEY, subscription_id=subscription_id, at_period_end=False
            )
            return {"mode": "canceled", "subscription_id": subscription_id}
        except StripeError as exc:
            logging.warning(
                "workspace comp cancel: Stripe cancel FAILED for ws=%s (customer=%s) — ORPHANED: %s",
                workspace_esid,
                customer_id,
                exc,
            )
            return {"mode": "error", "detail": str(exc)[:200], "customer_id": customer_id}
        except Exception as exc:  # noqa: BLE001
            logging.warning("workspace comp cancel: unexpected error for ws=%s: %s", workspace_esid, exc)
            return {"mode": "error", "detail": str(exc)[:200], "customer_id": customer_id}

    @staticmethod
    def get_workspace_email(workspace_esid: str) -> str:
        """Best-effort recipient for workspace billing emails — the workspace
        owner's email (Phase 3). "" if no owner / no email."""
        owner = WorkspaceBillingService.find_workspace_owner(workspace_esid)
        if not owner:
            return ""
        return SubscriptionStateService.get_user_email(owner)

    @staticmethod
    def list_comped_workspaces() -> list[dict]:
        """Roster of currently-comped WORKSPACES for the superuser comp control
        (comp moved per-user → per-workspace, so the roster must read
        the workspace records, not per-user metadata).

        Scans ``de_billing_workspace`` for ``comped_active=True`` and returns one
        row per comped workspace, keyed by the workspace OWNER's username so the
        existing clear flow works unchanged (clearing posts ``username`` →
        ``resolve_workspace_esid(owner)`` → the same workspace → ``clear_comp``)::

            {"username": <owner>, "email": <owner email>, "comped_by",
             "comped_at", "workspace_esid", "workspace_title"}

        A comped workspace with no resolvable owner is still listed (username =
        "", workspace_title carries the label) so the superuser can SEE it even
        though the owner-keyed clear can't target it — surfacing the data gap
        beats hiding the comp. Sorted by display label. Empty list on ES error.
        """
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            resp = es.search(
                index=INDEX_WORKSPACE_BILLING,
                body={"query": {"term": {COMPED_ACTIVE_KEY: True}}, "size": 1000},
            )
            hits = resp.get("hits", {}).get("hits", [])
        except Exception as e:  # noqa: BLE001 — boundary; roster just shows nothing
            logging.warning("WorkspaceBillingService.list_comped_workspaces: %s", e)
            return []

        # Resolve each workspace's title once for display.
        titles = WorkspaceBillingService._workspace_titles(es, [h.get("_id") for h in hits])

        out = []
        for h in hits:
            ws_esid = h.get("_id")
            src = h.get("_source") or {}
            owner = WorkspaceBillingService.find_workspace_owner(ws_esid) or ""
            out.append(
                {
                    "username": owner,
                    "email": SubscriptionStateService.get_user_email(owner) if owner else "",
                    "comped_by": src.get(COMPED_BY_KEY) or "",
                    "comped_at": src.get(COMPED_AT_KEY) or "",
                    "workspace_esid": ws_esid,
                    "workspace_title": titles.get(ws_esid) or ws_esid,
                }
            )
        out.sort(key=lambda m: (m["workspace_title"] or "").lower())
        return out

    @staticmethod
    def _workspace_titles(es, workspace_esids: list) -> dict:
        """Map workspace_esid → display title from ``de_asset_workspace`` (one
        mget). Missing/erroring titles are simply absent from the map."""
        ids = [w for w in (workspace_esids or []) if w]
        if not ids:
            return {}
        try:
            res = es.mget(index="de_asset_workspace", body={"ids": ids})
        except Exception as e:  # noqa: BLE001
            logging.warning("WorkspaceBillingService._workspace_titles: %s", e)
            return {}
        out = {}
        for doc in res.get("docs", []) or []:
            if not doc.get("found"):
                continue
            src = doc.get("_source") or {}
            title = src.get("workspace_title") or src.get("title")
            if title:
                out[doc.get("_id")] = title
        return out

    # ── Workspace billing record I/O ────────────────────────────────────
    @staticmethod
    def _get_billing_doc(es, workspace_esid: str) -> dict:
        """Return the workspace billing doc's fields (empty dict if none)."""
        try:
            res = es.get(index=INDEX_WORKSPACE_BILLING, id=workspace_esid, ignore=[404])
        except Exception as e:  # noqa: BLE001
            logging.warning("WorkspaceBillingService._get_billing_doc(%r): %s", workspace_esid, e)
            return {}
        if not res or not res.get("found", res.get("_source") is not None):
            return {}
        return dict(res.get("_source") or {})

    @staticmethod
    def has_billing_record(workspace_esid: str) -> bool:
        """True iff a ``de_billing_workspace`` doc exists for this workspace.

        Phase 2 rollout gate: ``get_state`` uses the WORKSPACE verdict only
        when a record exists; otherwise it falls back to the member's per-user
        state, so no one's access changes until Phase 4 migrates real data into
        workspace records. Best-effort: ES error / no workspace → False.
        """
        if not workspace_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        return bool(WorkspaceBillingService._get_billing_doc(es, workspace_esid))

    # Date-typed fields in the mapping: ES rejects an empty string for a
    # ``date`` ("cannot parse empty datetime"). The per-user service wrote ""
    # to "clear" a key, but here we must OMIT the field instead (a missing key
    # reads as falsy/cleared on the get_workspace_state side, same effect).
    _DATE_FIELDS = frozenset(
        {
            COMPED_AT_KEY,
            COMP_REVOKED_AT_KEY,
            TRIAL_STARTED_AT_KEY,
            ADDON_REMOVE_AT_KEY,
            "trial_ending_email_sent_at",
            "updated_at",
        }
    )

    @staticmethod
    def _put_billing_doc(es, workspace_esid: str, fields: dict) -> bool:
        try:
            # Drop empty date fields so ES doesn't reject "" against a date type.
            body = {
                k: v for k, v in fields.items() if not (k in WorkspaceBillingService._DATE_FIELDS and (v is None or v == ""))
            }
            body["workspace_esid"] = workspace_esid
            body["updated_at"] = SubscriptionStateService._now().isoformat()
            es.index(index=INDEX_WORKSPACE_BILLING, id=workspace_esid, body=body, refresh="wait_for")
            return True
        except Exception as e:  # noqa: BLE001
            logging.warning("WorkspaceBillingService._put_billing_doc(%r): %s", workspace_esid, e)
            return False

    # ── State derivation (mirrors SubscriptionStateService.get_state) ────
    @staticmethod
    def get_workspace_state(workspace_esid: str) -> dict:
        """Trial/active/lapsed/past_due/canceled/comp_revoked verdict for a
        workspace. Same dict shape as the per-user get_state. The superuser
        short-circuit is NOT here — it stays role-based in the caller.

        Determined-ness:
          * empty ``workspace_esid`` or ES outage → fail-closed ``lapsed`` with
            ``determined=False`` (so the import gates fail OPEN and the page
            shows an upgrade prompt — never blocks on a resolution blip);
          * a resolvable workspace with NO billing doc yet → treated as a fresh
            free-tier workspace (``trial``, determined), same as a brand-new
            per-user account.

        Usage counts are intentionally 0 in Phase 1 (the connection→usage
        rollup is per-principal today; Phase 2/3 decides the workspace rollup).
        """
        from asset_management.web.config import config

        def _lapsed_default(determined: bool) -> dict:
            return {
                "state": STATE_LAPSED,
                "is_paid": False,
                "is_comped": False,
                "comped_by": "",
                "comped_at": "",
                "trial_started_at": "",
                "trial_ends_at": "",
                "trial_days_remaining": 0,
                "connection_cap": CONNECTION_CAP,
                "account_cap_per_connection": ACCOUNT_CAP_PER_CONNECTION,
                "connections_used": 0,
                "accounts_used": 0,
                "determined": determined,
            }

        if not workspace_esid:
            return _lapsed_default(False)
        es = get_elasticsearch_client()
        if es is None:
            return _lapsed_default(False)

        b = WorkspaceBillingService._get_billing_doc(es, workspace_esid)
        is_paid = bool(b.get(PAID_TIER_KEY))
        is_comped = bool(b.get(COMPED_ACTIVE_KEY))
        comped_by = b.get(COMPED_BY_KEY) or ""
        comped_at = b.get(COMPED_AT_KEY) or ""

        # (2026-06-12) — purchased add-on connections extend the
        # included cap, mirroring the per-user path; a removal mark keeps the
        # paid capacity until the stamped period end, then it stops counting.
        try:
            addon_connections = max(0, int(b.get(ADDON_CONNECTIONS_KEY) or 0))
        except (TypeError, ValueError):
            addon_connections = 0
        _remove_at = SubscriptionStateService._parse_iso((b.get(ADDON_REMOVE_AT_KEY) or "").strip())
        if _remove_at is not None and SubscriptionStateService._now() >= _remove_at:
            # The scheduled change landed: drop to the pending target
            # (0 = full removal, N = reduction).
            try:
                addon_connections = max(0, min(addon_connections, int(b.get(ADDON_PENDING_QTY_KEY) or 0)))
            except (TypeError, ValueError):
                addon_connections = 0
        effective_cap = CONNECTION_CAP + addon_connections

        def _resolved(state: str) -> dict:
            return {
                "state": state,
                "is_paid": is_paid,
                "is_comped": is_comped,
                "comped_by": comped_by,
                "comped_at": comped_at,
                "trial_started_at": "",
                "trial_ends_at": "",
                "trial_days_remaining": 0,
                "connection_cap": effective_cap,
                "account_cap_per_connection": ACCOUNT_CAP_PER_CONNECTION,
                "connections_used": 0,
                "accounts_used": 0,
                "determined": True,
            }

        # comp wins (highest authority among non-superusers).
        if is_comped:
            return _resolved(STATE_ACTIVE)

        # Revoked comp — unless a still-valid trial remains (resume it).
        if b.get(COMP_REVOKED_AT_KEY):
            now = SubscriptionStateService._now()
            ts = SubscriptionStateService._parse_iso(b.get(TRIAL_STARTED_AT_KEY) or "")
            trial_unexpired = ts is not None and now < (ts + timedelta(days=config.FREE_TIER_TRIAL_DAYS))
            if not trial_unexpired:
                d = _resolved(STATE_COMP_REVOKED)
                d["is_paid"] = False
                d["is_comped"] = False
                d["comped_by"] = ""
                d["comped_at"] = ""
                return d

        # Live Stripe status is authoritative over the trial/lapsed guess.
        stripe_status = (b.get(STRIPE_STATUS_KEY) or "").strip().lower()
        mapped = _STRIPE_STATUS_TO_STATE.get(stripe_status)
        if mapped is not None:
            return _resolved(mapped)

        if is_paid:
            return _resolved(STATE_ACTIVE)

        # Free tier — trial vs lapsed from the stamped start (display-only
        # window from "now" when unstamped; Phase 2 owns lazy-stamp policy).
        now = SubscriptionStateService._now()
        started_at = SubscriptionStateService._parse_iso(b.get(TRIAL_STARTED_AT_KEY) or "")
        stored = b.get(TRIAL_STARTED_AT_KEY) or ""
        if started_at is None:
            started_at = now
            stored = ""
        ends_at = started_at + timedelta(days=config.FREE_TIER_TRIAL_DAYS)
        if now < ends_at:
            total = (ends_at - now).total_seconds()
            days_remaining = int(total // 86400) + (1 if total % 86400 > 0 else 0)
            return {
                "state": STATE_TRIAL,
                "is_paid": False,
                "is_comped": False,
                "comped_by": "",
                "comped_at": "",
                "trial_started_at": stored,
                "trial_ends_at": ends_at.isoformat(),
                "trial_days_remaining": days_remaining,
                "connection_cap": TRIAL_SOURCE_CAP,
                "account_cap_per_connection": TRIAL_ACCOUNT_CAP,
                "connections_used": 0,
                "accounts_used": 0,
                "determined": True,
            }
        d = _lapsed_default(True)
        d["trial_started_at"] = stored
        d["trial_ends_at"] = ends_at.isoformat()
        return d

    # ── Write helpers (keyed by workspace) ──────────────────────────────
    @staticmethod
    def set_comp(workspace_esid: str, set_by: str) -> dict:
        """Comp a WORKSPACE (all members inherit). Clears stale stripe/paid +
        the revoked marker, mirroring the per-user ``set_comp``."""
        if not workspace_esid:
            return {"ok": False, "error": "missing workspace"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        b = WorkspaceBillingService._get_billing_doc(es, workspace_esid)
        b[COMPED_ACTIVE_KEY] = True
        b[COMPED_BY_KEY] = set_by or ""
        b[COMPED_AT_KEY] = SubscriptionStateService._now().isoformat()
        b[STRIPE_STATUS_KEY] = ""
        b[PAID_TIER_KEY] = False
        b[COMP_REVOKED_AT_KEY] = ""
        ok = WorkspaceBillingService._put_billing_doc(es, workspace_esid, b)
        return {"ok": ok, "error": "" if ok else "write failed"}

    @staticmethod
    def clear_comp(workspace_esid: str) -> dict:
        """Remove a workspace comp; stamp comp_revoked_at when one was active."""
        if not workspace_esid:
            return {"ok": False, "error": "missing workspace"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        b = WorkspaceBillingService._get_billing_doc(es, workspace_esid)
        was_comped = bool(b.get(COMPED_ACTIVE_KEY))
        b[COMPED_ACTIVE_KEY] = False
        b[COMPED_BY_KEY] = ""
        b[COMPED_AT_KEY] = ""
        if was_comped:
            b[COMP_REVOKED_AT_KEY] = SubscriptionStateService._now().isoformat()
        ok = WorkspaceBillingService._put_billing_doc(es, workspace_esid, b)
        return {"ok": ok, "error": "" if ok else "write failed", "was_comped": was_comped}

    @staticmethod
    def apply_stripe_status(workspace_esid: str, stripe_status: str, customer_id: str = "") -> dict:
        """Apply a Stripe status to a workspace (webhook / live create path)."""
        if not workspace_esid:
            return {"ok": False, "error": "missing workspace"}
        normalized = (stripe_status or "").strip().lower()
        if not normalized:
            return {"ok": False, "error": "missing stripe_status"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        b = WorkspaceBillingService._get_billing_doc(es, workspace_esid)
        b[STRIPE_STATUS_KEY] = normalized
        is_active_status = normalized in ("active", "trialing")
        b[PAID_TIER_KEY] = is_active_status
        if customer_id:
            b[STRIPE_CUSTOMER_ID_KEY] = customer_id
        if is_active_status:
            b[COMP_REVOKED_AT_KEY] = ""
        ok = WorkspaceBillingService._put_billing_doc(es, workspace_esid, b)
        return {"ok": ok, "error": "" if ok else "write failed"}

    @staticmethod
    def set_org_customer_id(workspace_esid: str, customer_id: str) -> bool:
        """Stamp the org's Stripe Customer id on the workspace billing record
        WITHOUT touching status/paid_tier (Option B — the org customer
        is created before any subscription/status exists, so apply_stripe_status
        — which requires a status and early-returns on empty — can't be used to
        persist just the customer id). Returns True on success."""
        if not (workspace_esid and customer_id):
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        b = WorkspaceBillingService._get_billing_doc(es, workspace_esid)
        b[STRIPE_CUSTOMER_ID_KEY] = customer_id
        return WorkspaceBillingService._put_billing_doc(es, workspace_esid, b)

    @staticmethod
    def set_addon_connections(workspace_esid: str, quantity: int) -> dict:
        """Persist the purchased add-on connection count on the WORKSPACE
        billing doc (-06-12 — workspace-billed orgs read their
        cap from get_workspace_state, which shadows the per-user metadata
        stamp). Clears any scheduled removal mark — a new purchase/adjust
        cancels it. Mirrors SubscriptionStateService.set_addon_connections."""
        if not workspace_esid:
            return {"ok": False, "error": "missing workspace"}
        try:
            quantity = max(0, int(quantity))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid quantity"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        b = WorkspaceBillingService._get_billing_doc(es, workspace_esid)
        b[ADDON_CONNECTIONS_KEY] = quantity
        b[ADDON_REMOVE_AT_KEY] = ""  # dropped by _put_billing_doc (date field)
        b[ADDON_PENDING_QTY_KEY] = 0
        ok = WorkspaceBillingService._put_billing_doc(es, workspace_esid, b)
        return {"ok": ok, "error": "" if ok else "write failed"}

    @staticmethod
    def mark_addon_removal(workspace_esid: str, remove_at: str, target_qty: int = 0) -> dict:
        """Mark the workspace's add-on connections for a scheduled change at
        ``remove_at`` (the already-paid period end): ``target_qty`` 0 = full
        removal, N = a reduction. Keeps the count so the capacity stays until
        then; get_workspace_state drops to the target once the stamp passes.
        Mirrors SubscriptionStateService.mark_addon_removal."""
        if not workspace_esid:
            return {"ok": False, "error": "missing workspace"}
        if SubscriptionStateService._parse_iso(remove_at) is None:
            return {"ok": False, "error": "invalid remove_at"}
        try:
            target_qty = max(0, int(target_qty))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid target_qty"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        b = WorkspaceBillingService._get_billing_doc(es, workspace_esid)
        b[ADDON_REMOVE_AT_KEY] = remove_at
        b[ADDON_PENDING_QTY_KEY] = target_qty
        ok = WorkspaceBillingService._put_billing_doc(es, workspace_esid, b)
        return {"ok": ok, "error": "" if ok else "write failed"}

    @staticmethod
    def get_addon_remove_at(workspace_esid: str) -> str:
        """Pending add-on removal stamp for the workspace — '' when none,
        already passed, or on lookup failure (fail-soft, mirrors the per-user
        SubscriptionStateService.get_addon_remove_at)."""
        if not workspace_esid:
            return ""
        es = get_elasticsearch_client()
        if es is None:
            return ""
        raw = (WorkspaceBillingService._get_billing_doc(es, workspace_esid).get(ADDON_REMOVE_AT_KEY) or "").strip()
        parsed = SubscriptionStateService._parse_iso(raw)
        if parsed is None or SubscriptionStateService._now() >= parsed:
            return ""
        return raw

    @staticmethod
    def get_addon_pending_qty(workspace_esid: str) -> int:
        """The TARGET quantity of the pending scheduled change (0 = full
        removal) — only meaningful while ``get_addon_remove_at`` is non-empty.
        0 on any lookup failure (fail-soft)."""
        if not workspace_esid:
            return 0
        es = get_elasticsearch_client()
        if es is None:
            return 0
        try:
            return max(0, int(WorkspaceBillingService._get_billing_doc(es, workspace_esid).get(ADDON_PENDING_QTY_KEY) or 0))
        except (TypeError, ValueError):
            return 0

    # ── AI Assistant spend readout ───────────────────────────
    @staticmethod
    def ai_spend_cycle_window(username: str) -> tuple[str, str | None, str]:
        """The current billing-cycle window for the AI-spend readout.

        Returns ``(since_iso, until_iso | None, source)`` where ``source``
        is ``"stripe"`` or ``"calendar"``.

        Decision (a), 2026-07-28: use the workspace's live Stripe period
        when the billing target has an ACTIVE subscription with a readable
        period start; fall back to the calendar month (UTC) for everyone
        else — comped, trial, and superuser workspaces have no Stripe
        period at all. No reset job exists or is needed: spend is a
        range-filtered sum over an event index, so "reset" is implicit in
        the window start moving.

        Fail-soft: any Stripe/config/lookup error falls back to calendar.
        """
        from datetime import datetime, timezone

        def _calendar_window() -> tuple[str, str | None, str]:
            now = datetime.now(timezone.utc)
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(), None, "calendar"

        try:
            from asset_management.web.config import config
            from asset_management.web.services.stripe_service import StripeService

            if not (config.STRIPE_LIVE_PAYMENTS and config.STRIPE_SECRET_KEY):
                return _calendar_window()
            workspace_esid = WorkspaceBillingService.resolve_workspace_esid(username)
            if not workspace_esid or not WorkspaceBillingService.has_billing_record(workspace_esid):
                return _calendar_window()
            es = get_elasticsearch_client()
            if es is None:
                return _calendar_window()
            customer_id = WorkspaceBillingService._get_billing_doc(es, workspace_esid).get(STRIPE_CUSTOMER_ID_KEY) or ""
            if not customer_id:
                return _calendar_window()
            summary = StripeService.get_subscription_summary(secret_key=config.STRIPE_SECRET_KEY, customer_id=customer_id)
            if summary and summary.get("status") == "active" and summary.get("current_period_start"):
                return (
                    summary["current_period_start"],
                    summary.get("current_period_end") or None,
                    "stripe",
                )
        except Exception as e:  # noqa: BLE001 — readout must never break the page
            logging.warning("WorkspaceBillingService.ai_spend_cycle_window(%r): %s", username, e)
        return _calendar_window()

    @staticmethod
    def resolve_ai_workspace_esid(username: str) -> str | None:
        """The workspace an AI Assistant turn belongs to.

        Deliberately NOT ``resolve_workspace_esid``. That resolver's
        no-primary fallback is ``sorted(any_ws)[0]`` (:151) — an
        alphabetical pick among every workspace role the user holds. For a
        multi-workspace superuser that is arbitrary: four non-primary
        ``workspace_owner`` rows resolve to ``control-workspace`` purely
        because "c" sorts first, while the workspace they actually work in
        is reached through a workspace-scope DATA GRANT the billing
        resolver cannot see at all. The gate read that arbitrary
        pick, found no ``ai_assistant_enabled`` on it, and denied with
        ``workspace_disabled`` — the reported bug.

        ``resolve_curation_workspace`` is the corrected ladder and
        already encodes exactly this case: is_primary role → workspace data
        grant → org/team walk → positional fallback. Its leg 2 is the grant,
        so the granted workspace wins over the alphabetical accident.

        ``resolve_workspace_esid`` is left untouched on purpose: it decides
        who STRIPE CHARGES across a dozen call sites, and changing which
        workspace gets billed real money is out of scope here. This is the
        AI-attribution resolver only — gate, readout, and usage stamp share
        it so all three always agree on one workspace.
        """
        if not username:
            return None
        try:
            from asset_management.web.services.workspace_membership_service import (
                WorkspaceMembershipService,
            )

            return WorkspaceMembershipService.resolve_curation_workspace(username)
        except Exception as e:  # noqa: BLE001 — callers decide their own posture
            logging.warning("WorkspaceBillingService.resolve_ai_workspace_esid(%r): %s", username, e)
            return None

    @staticmethod
    def get_ai_spend_readout(username: str) -> dict | None:
        """Current-cycle workspace AI spend for display.

        Returns ``None`` when no billing workspace resolves for the user
        (e.g. a superuser with no workspace roles) — the caller hides the
        readout rather than fabricating a bucket (never
        ``get_primary_workspace``; see analytics.routes notes).

        Otherwise returns::

            {
                "workspace_esid": str,
                "spend_usd": float | None,   # None = ES error → render blank
                "cycle_start": str,          # ISO-8601 UTC
                "cycle_end": str | None,     # None = open-ended (calendar)
                "source": "stripe" | "calendar",
            }

        ``spend_usd`` fails OPEN (None → blank) — this is a readout, not a
        budget gate; see nlq_usage_service.get_workspace_spend.
        """
        # AI attribution resolver, NOT the Stripe billing one — see
        # resolve_ai_workspace_esid. The gate, this readout and the
        # nlq_usage_log stamp must all name the same workspace, or the
        # panel shows one tenant's spend while the rows accrue to another.
        workspace_esid = WorkspaceBillingService.resolve_ai_workspace_esid(username)
        if not workspace_esid:
            return None
        since_iso, until_iso, source = WorkspaceBillingService.ai_spend_cycle_window(username)
        try:
            from asset_management.web.services.nlq_usage_service import get_workspace_spend

            spend = get_workspace_spend(workspace_esid, since_iso, until_iso)
        except Exception as e:  # noqa: BLE001 — readout must never break the page
            logging.warning("WorkspaceBillingService.get_ai_spend_readout(%r): %s", username, e)
            spend = None
        return {
            "workspace_esid": workspace_esid,
            "spend_usd": spend,
            "cycle_start": since_iso,
            "cycle_end": until_iso,
            "source": source,
        }

    # ── Trial-ending cron (workspace-scoped) ────────────────────────────
    @staticmethod
    def scan_and_notify_trial_ending(warn_within_days: int = 3, _dispatch_fn=None) -> dict:
        """Daily cron: email the OWNER of
        every WORKSPACE whose trial ends within ``warn_within_days`` days, once.

        Scans ``de_billing_workspace`` records (a workspace only has one once it
        has touched billing). A workspace qualifies when it has NO comp / paid /
        stripe status, a stamped ``trial_started_at``, and ``0 < days_remaining
        <= warn_within_days``. Emailed once — guarded by
        ``trial_ending_email_sent_at`` on the workspace record (stamped only
        after a successful dispatch). Best-effort; returns a summary dict.

        The legacy per-user ``SubscriptionStateService.scan_and_notify_trial_ending``
        still runs alongside this for users whose trial lives on their own
        record (pre-Phase-4 migration); the cron calls both.
        """
        from asset_management.web.config import config

        summary = {"scanned": 0, "notified": 0, "skipped": 0, "errors": 0}
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("workspace trial-ending scan: ES unavailable")
            return summary

        if _dispatch_fn is None:
            try:
                from asset_management.web.services.billing_email_service import BillingEmailService

                _dispatch_fn = BillingEmailService.dispatch
            except Exception as exc:  # noqa: BLE001
                logging.warning("workspace trial-ending scan: email service import failed: %s", exc)
                return summary

        try:
            resp = es.search(index=INDEX_WORKSPACE_BILLING, body={"query": {"match_all": {}}, "size": 5000})
            rows = resp.get("hits", {}).get("hits", [])
        except Exception as exc:  # noqa: BLE001
            logging.warning("workspace trial-ending scan: search failed: %s", exc)
            return summary

        now = SubscriptionStateService._now()
        trial_days = config.FREE_TIER_TRIAL_DAYS
        for hit in rows:
            ws_esid = hit.get("_id")
            b = hit.get("_source") or {}
            if b.get(COMPED_ACTIVE_KEY) or b.get(PAID_TIER_KEY) or (b.get(STRIPE_STATUS_KEY) or "").strip():
                continue
            started_at = SubscriptionStateService._parse_iso(b.get(TRIAL_STARTED_AT_KEY) or "")
            if started_at is None:
                continue
            summary["scanned"] += 1
            if b.get(TRIAL_ENDING_EMAIL_SENT_KEY):
                summary["skipped"] += 1
                continue
            secs_left = (started_at + timedelta(days=trial_days) - now).total_seconds()
            if secs_left <= 0 or (int(secs_left // 86400) + (1 if secs_left % 86400 > 0 else 0)) > warn_within_days:
                continue
            days_remaining = int(secs_left // 86400) + (1 if secs_left % 86400 > 0 else 0)
            owner_email = WorkspaceBillingService.get_workspace_email(ws_esid)
            if not owner_email:
                summary["skipped"] += 1
                continue
            try:
                _dispatch_fn("trial_ending", owner_email, {"full_name": "", "days_remaining": days_remaining})
                new_b = dict(b)
                new_b[TRIAL_ENDING_EMAIL_SENT_KEY] = now.isoformat()
                WorkspaceBillingService._put_billing_doc(es, ws_esid, new_b)
                summary["notified"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad workspace must not stop the scan
                logging.warning("workspace trial-ending scan: notify failed for ws=%r: %s", ws_esid, exc)
                summary["errors"] += 1
        logging.info("workspace trial-ending scan complete: %s", summary)
        return summary
