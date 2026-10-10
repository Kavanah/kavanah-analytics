"""Org Billing Service.

Implements the chosen Stripe model: **one Stripe Customer per organization, one
subscription per team license** under that customer. "Organization" == the
top-level billing entity the rest of the billing code calls a *workspace*
(decision 2026-06-09); the NEW concept this service owns is the **per-team
subscription**.

Design (decisions 2026-06-09):
  * Persistence — a NEW index ``de_billing_team_subscription``, one doc per
    licensed team (doc id == ``team_esid``), holding the team's Stripe
    subscription id + status + the org/customer it bills under. Cleaner for
    per-team status/webhook updates than an array on the workspace doc.
  * The ORG customer reuses ``de_billing_workspace.stripe_customer_id`` (the
    org == the workspace billing entity) — we do NOT mint a second customer.
  * Team license is EXPLICIT — an org owner licenses specific teams; each gets
    its own subscription. (Not every team auto-billed.)
  * INERT in Phase O1: this module is added + unit-tested, but no checkout /
    get_state / webhook call-site is rewired yet (that's O2/O3, behind a flag).

Coupon/comp (item 3) and add-ons (item 5) already operate on "a subscription
id", so they compose onto a team subscription unchanged — the caller feeds the
team's subscription id resolved here.

Fail-soft posture: read helpers return None/[] on any ES error so a resolution
blip never hard-fails a billing page.
"""

from __future__ import annotations

import logging
import re
import sys

sys.path.insert(0, "/app/python")

from lib.es_helpers import (  # noqa: E402
    get_elasticsearch_client,
    put_regular_index_template,
    create_index_if_not_exists,
)

from asset_management.web.services.workspace_billing_service import (  # noqa: E402
    WorkspaceBillingService,
    INDEX_WORKSPACE_BILLING,
)
from asset_management.web.services.subscription_state_service import (  # noqa: E402
    STRIPE_CUSTOMER_ID_KEY,
    SubscriptionStateService,
    PAID_TIER_KEY,
    COMPED_ACTIVE_KEY,
    COMPED_BY_KEY,
    COMPED_AT_KEY,
    COMP_REVOKED_AT_KEY,
    STRIPE_STATUS_KEY,
    STATE_ACTIVE,
    STATE_LAPSED,
    STATE_COMP_REVOKED,
    _STRIPE_STATUS_TO_STATE,
    CONNECTION_CAP,
    ACCOUNT_CAP_PER_CONNECTION,
)

# One doc per LICENSED team, doc id == team_esid.
INDEX_TEAM_SUBSCRIPTION = "de_billing_team_subscription"

# one billing doc per ORG, doc id == org_esid. The ORG is the billing/
# comp entity (decision 2026-06-16): a single workspace can hold many orgs
# (kavanah-media holds 20), and comp/charge must target one org without touching
# its siblings. Mirrors de_billing_workspace's comp/state fields exactly so the
# state derivation can be shared. The org's Stripe customer is the SAME one
# OrgBillingService.get_or_create_org_customer mints (stored on the workspace
# record today); this record carries the org-level comp/paid/status verdict that
# get_state consults BEFORE the workspace path.
INDEX_ORG_BILLING = "de_billing_org"

ORGANIZATION_INDEX = "de_asset_organization"
WORKSPACE_INDEX = "de_asset_workspace"

ORG_BILLING_MAPPING = {
    "properties": {
        "org_esid": {"type": "keyword"},
        # The workspace this org sits under (for reverse lookup / migration
        # provenance). NOT the billing entity — the org is.
        "workspace_esid": {"type": "keyword"},
        "stripe_customer_id": {"type": "keyword"},
        "paid_tier": {"type": "boolean"},
        "comped_active": {"type": "boolean"},
        "comped_by": {"type": "keyword"},
        "comped_at": {"type": "date"},
        "comp_revoked_at": {"type": "date"},
        "stripe_status": {"type": "keyword"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    }
}

TEAM_SUBSCRIPTION_MAPPING = {
    "properties": {
        # The licensed team (doc id == this).
        "team_esid": {"type": "keyword"},
        # The org (== workspace billing entity) this team bills under, and the
        # org's Stripe Customer the subscription hangs off.
        "workspace_esid": {"type": "keyword"},
        "stripe_customer_id": {"type": "keyword"},
        # The team's own Stripe Subscription (Option B: one per team license).
        "stripe_subscription_id": {"type": "keyword"},
        "stripe_status": {"type": "keyword"},
        # Discount/comp + add-ons are per-team (applied to THIS subscription).
        "discount_coupon": {"type": "keyword"},
        "addon_connections": {"type": "integer"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    }
}


class OrgBillingService:
    """Org Stripe Customer + one subscription per licensed team (Option B)."""

    # ── Index bootstrap ─────────────────────────────────────────────────
    @staticmethod
    def ensure_index_exists() -> None:
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("OrgBillingService: could not connect to ES")
            return
        for index_name, mapping in (
            (INDEX_TEAM_SUBSCRIPTION, TEAM_SUBSCRIPTION_MAPPING),
            (INDEX_ORG_BILLING, ORG_BILLING_MAPPING),
        ):
            put_regular_index_template(es, index_name, mapping, priority=500)
            create_index_if_not_exists(es, index_name, mapping)
            try:
                es.indices.put_mapping(index=index_name, body=mapping)
            except Exception as e:  # noqa: BLE001
                logging.warning("OrgBillingService: put_mapping on %s: %s", index_name, e)

    # ── Member → team resolution (O2: which team a buyer licenses) ──────
    @staticmethod
    def resolve_primary_team_esid(username: str) -> str | None:
        """The team a buyer LICENSES at checkout (O2 decision: the
        member's primary team). Prefers the ``is_primary`` team role; else any
        team role (deterministic by sort). None when the member holds no team
        role. Mirrors WorkspaceBillingService.resolve_workspace_esid's
        primary-first pattern. Fail-soft → None on ES error."""
        if not username:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            resp = es.search(
                index="de_authz_user_team_role",
                body={
                    "query": {"term": {"username": username}},
                    "_source": ["team_esid", "is_primary"],
                    "size": 100,
                },
            )
            rows = [h.get("_source") or {} for h in resp.get("hits", {}).get("hits", [])]
        except Exception as e:  # noqa: BLE001
            logging.warning("OrgBillingService.resolve_primary_team_esid(%r): %s", username, e)
            return None
        primary = [r["team_esid"] for r in rows if r.get("is_primary") and r.get("team_esid")]
        if primary:
            return primary[0]
        any_team = sorted(r["team_esid"] for r in rows if r.get("team_esid"))
        return any_team[0] if any_team else None

    # ── Org customer (reuses the workspace billing record) ──────────────
    @staticmethod
    def get_org_customer_id(workspace_esid: str) -> str:
        """The org's Stripe Customer id, or "" if not yet created. Reads the
        existing ``de_billing_workspace`` record (the org == workspace billing
        entity)."""
        if not workspace_esid:
            return ""
        es = get_elasticsearch_client()
        if es is None:
            return ""
        try:
            doc = es.get(index=INDEX_WORKSPACE_BILLING, id=workspace_esid)
            return (doc.get("_source") or {}).get(STRIPE_CUSTOMER_ID_KEY) or ""
        except Exception:  # noqa: BLE001 — missing doc / ES blip
            return ""

    @staticmethod
    def get_or_create_org_customer(
        *,
        workspace_esid: str,
        secret_key: str,
        email: str = "",
        name: str = "",
        address: dict | None = None,
        company: str = "",
        payment_method_id: str = "",
        sdk=None,
    ) -> str:
        """Return the org's Stripe Customer id, creating it (and stamping it on
        the workspace billing record) if absent. One customer per org — every
        team subscription attaches to it. Raises ``StripeError`` on a create
        failure; returns "" only when ``workspace_esid`` is empty."""
        if not workspace_esid:
            return ""
        existing = OrgBillingService.get_org_customer_id(workspace_esid)
        if existing:
            return existing

        from asset_management.web.services.stripe_service import StripeService

        customer_id = StripeService.create_customer(
            secret_key=secret_key,
            email=email,
            name=name,
            address=address,
            company=company,
            payment_method_id=payment_method_id,
            sdk=sdk,
        )
        # Stamp the customer id on the workspace billing record. Use the
        # dedicated set_org_customer_id (NOT apply_stripe_status — that requires
        # a non-empty status and early-returns, so it would NOT persist the
        # customer id at create time, causing a duplicate customer next call).
        if not WorkspaceBillingService.set_org_customer_id(workspace_esid, customer_id):
            # The customer exists in Stripe; if the stamp fails we log loudly so
            # it can be reconciled rather than minting a duplicate next time.
            logging.error(
                "OrgBillingService: created org customer %s for ws=%s but failed to stamp it",
                customer_id,
                workspace_esid,
            )
        return customer_id

    # ── Per-team subscription CRUD ──────────────────────────────────────
    @staticmethod
    def _now_iso() -> str:
        return SubscriptionStateService._now().isoformat()

    @staticmethod
    def get_team_subscription(team_esid: str) -> dict | None:
        """The team's billing doc ({stripe_subscription_id, stripe_status, ...})
        or None if the team isn't licensed. Fail-soft → None."""
        if not team_esid:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            doc = es.get(index=INDEX_TEAM_SUBSCRIPTION, id=team_esid)
            return doc.get("_source") or {}
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def list_team_subscriptions(workspace_esid: str) -> list[dict]:
        """Every licensed team's billing doc under an org. Fail-soft → []."""
        if not workspace_esid:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            resp = es.search(
                index=INDEX_TEAM_SUBSCRIPTION,
                body={"query": {"term": {"workspace_esid": workspace_esid}}, "size": 1000},
            )
        except Exception as e:  # noqa: BLE001
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                return []
            logging.warning("OrgBillingService.list_team_subscriptions(%r): %s", workspace_esid, e)
            return []
        return [h.get("_source") or {} for h in resp.get("hits", {}).get("hits", [])]

    @staticmethod
    def list_team_subscriptions_display(workspace_esid: str) -> list[dict]:
        """The org's team subscriptions joined with team TITLES for the Current
        Plan "Team subscriptions" card. Each row::

            {"team_esid", "team_title", "stripe_status",
             "addon_connections", "has_discount"}

        Title resolution is a single mget on ``de_asset_teams`` (``team_title``,
        falling back to the esid so a deleted/missing team doc still shows a
        row rather than vanishing). Sorted by title. Fail-soft → [].
        """
        subs = OrgBillingService.list_team_subscriptions(workspace_esid)
        if not subs:
            return []
        es = get_elasticsearch_client()
        titles: dict[str, str] = {}
        team_ids = [s.get("team_esid") for s in subs if s.get("team_esid")]
        if es is not None and team_ids:
            try:
                resp = es.mget(index="de_asset_teams", body={"ids": team_ids})
                for doc in resp.get("docs", []):
                    if doc.get("found"):
                        src = doc.get("_source") or {}
                        titles[doc.get("_id")] = (src.get("team_title") or src.get("title") or "").strip()
            except Exception as e:  # noqa: BLE001
                logging.warning("OrgBillingService.list_team_subscriptions_display titles: %s", e)
        out = []
        for s in subs:
            team_esid = s.get("team_esid") or ""
            out.append(
                {
                    "team_esid": team_esid,
                    "team_title": titles.get(team_esid) or team_esid,
                    "stripe_status": (s.get("stripe_status") or "").strip().lower(),
                    "addon_connections": int(s.get("addon_connections") or 0),
                    "has_discount": bool(s.get("discount_coupon")),
                }
            )
        return sorted(out, key=lambda r: r["team_title"].lower())

    @staticmethod
    def upsert_team_subscription(
        *,
        team_esid: str,
        workspace_esid: str,
        stripe_customer_id: str,
        stripe_subscription_id: str,
        stripe_status: str = "",
        discount_coupon: str = "",
        addon_connections: int = 0,
    ) -> bool:
        """Create/replace the team's billing doc (doc id == team_esid). Returns
        True on success. Best-effort: False on ES error (caller logs)."""
        if not (team_esid and workspace_esid and stripe_subscription_id):
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        existing = OrgBillingService.get_team_subscription(team_esid) or {}
        now = OrgBillingService._now_iso()
        body = {
            "team_esid": team_esid,
            "workspace_esid": workspace_esid,
            "stripe_customer_id": stripe_customer_id,
            "stripe_subscription_id": stripe_subscription_id,
            "stripe_status": stripe_status,
            "discount_coupon": discount_coupon,
            "addon_connections": int(addon_connections or 0),
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
        }
        try:
            es.index(index=INDEX_TEAM_SUBSCRIPTION, id=team_esid, document=body, refresh="wait_for")
            return True
        except Exception as e:  # noqa: BLE001
            logging.warning("OrgBillingService.upsert_team_subscription(%r): %s", team_esid, e)
            return False

    @staticmethod
    def set_team_subscription_status(team_esid: str, stripe_status: str) -> bool:
        """Update only the team subscription's status (webhook path, O3). Returns
        True on success; False if the team has no billing doc / on error."""
        if not team_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            es.update(
                index=INDEX_TEAM_SUBSCRIPTION,
                id=team_esid,
                doc={"stripe_status": stripe_status, "updated_at": OrgBillingService._now_iso()},
            )
            return True
        except Exception as e:  # noqa: BLE001
            logging.warning("OrgBillingService.set_team_subscription_status(%r): %s", team_esid, e)
            return False

    @staticmethod
    def find_team_by_subscription_id(stripe_subscription_id: str) -> str | None:
        """Reverse-map a Stripe subscription id → the team_esid it bills (the
        webhook needs this to apply an event to the right team). None if no
        match. Fail-soft → None."""
        if not stripe_subscription_id:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            resp = es.search(
                index=INDEX_TEAM_SUBSCRIPTION,
                body={
                    "query": {"term": {"stripe_subscription_id": stripe_subscription_id}},
                    "_source": ["team_esid"],
                    "size": 1,
                },
            )
            hits = resp.get("hits", {}).get("hits", [])
            if hits:
                return (hits[0].get("_source") or {}).get("team_esid") or None
        except Exception as e:  # noqa: BLE001
            logging.warning("OrgBillingService.find_team_by_subscription_id(%r): %s", stripe_subscription_id, e)
        return None

    # ── Licensing a team (the Option B checkout primitive) ──────────────
    @staticmethod
    def create_team_subscription(
        *,
        workspace_esid: str,
        team_esid: str,
        price_id: str,
        secret_key: str,
        payment_method_id: str = "",
        coupon: str = "",
        promotion_code: str = "",
        owner_email: str = "",
        owner_name: str = "",
        owner_address: dict | None = None,
        company: str = "",
        sdk=None,
    ) -> dict:
        """License a team: ensure the org's Stripe Customer exists, then create
        ONE subscription for this team under it, and persist the
        team_esid→subscription link. Idempotent guard: a team that already has a
        subscription doc returns it unchanged (caller decides whether to modify).

        Returns ``{"customer_id", "subscription_id", "status", "team_esid",
        "already_licensed": bool}``. Raises ``StripeError`` on an SDK failure
        (no link persisted on failure)."""
        if not (workspace_esid and team_esid and price_id):
            from asset_management.web.services.stripe_service import StripeError

            raise StripeError("missing workspace/team/price id")

        existing = OrgBillingService.get_team_subscription(team_esid)
        if existing and existing.get("stripe_subscription_id"):
            return {
                "customer_id": existing.get("stripe_customer_id", ""),
                "subscription_id": existing.get("stripe_subscription_id", ""),
                "status": existing.get("stripe_status", ""),
                "team_esid": team_esid,
                "already_licensed": True,
            }

        from asset_management.web.services.stripe_service import StripeService

        customer_id = OrgBillingService.get_or_create_org_customer(
            workspace_esid=workspace_esid,
            secret_key=secret_key,
            email=owner_email,
            name=owner_name,
            address=owner_address,
            company=company,
            payment_method_id=payment_method_id,
            sdk=sdk,
        )
        result = StripeService.create_subscription_for_customer(
            secret_key=secret_key,
            customer_id=customer_id,
            price_id=price_id,
            coupon=coupon,
            promotion_code=promotion_code,
            metadata={"team_esid": team_esid, "workspace_esid": workspace_esid},
            payment_method_id=payment_method_id,
            sdk=sdk,
        )
        OrgBillingService.upsert_team_subscription(
            team_esid=team_esid,
            workspace_esid=workspace_esid,
            stripe_customer_id=customer_id,
            stripe_subscription_id=result.get("subscription_id", ""),
            stripe_status=result.get("status", ""),
            discount_coupon=(promotion_code or coupon or ""),
        )
        return {
            "customer_id": customer_id,
            "subscription_id": result.get("subscription_id", ""),
            "status": result.get("status", ""),
            "team_esid": team_esid,
            "already_licensed": False,
        }

    # ── ORG billing record ────────────────────────────────────
    #
    # The org-level comp/state layer. ``get_state`` consults
    # ``get_org_state(org_esid)`` BEFORE the workspace path, so an org under a
    # shared workspace (kavanah-media holds 20) can be comped/billed without
    # touching its siblings. Mirrors WorkspaceBillingService's comp/state I/O,
    # keyed by org_esid. The org's Stripe customer is the SAME one
    # get_or_create_org_customer mints (stored on the workspace record); this
    # record only carries the org verdict.

    # Date-typed fields: ES rejects an empty string against a ``date`` mapping,
    # so a cleared date key is OMITTED rather than written "" (a missing key
    # reads as falsy/cleared in get_org_state — same effect). Mirrors
    # WorkspaceBillingService._DATE_FIELDS.
    _ORG_DATE_FIELDS = frozenset({COMPED_AT_KEY, COMP_REVOKED_AT_KEY, "created_at", "updated_at"})

    @staticmethod
    def _get_org_billing_doc(es, org_esid: str) -> dict:
        """Return the org billing doc's fields (empty dict if none)."""
        try:
            res = es.get(index=INDEX_ORG_BILLING, id=org_esid, ignore=[404])
        except Exception as e:  # noqa: BLE001
            logging.warning("OrgBillingService._get_org_billing_doc(%r): %s", org_esid, e)
            return {}
        if not res or not res.get("found", res.get("_source") is not None):
            return {}
        return dict(res.get("_source") or {})

    @staticmethod
    def _put_org_billing_doc(es, org_esid: str, fields: dict) -> bool:
        try:
            body = {
                k: v for k, v in fields.items() if not (k in OrgBillingService._ORG_DATE_FIELDS and (v is None or v == ""))
            }
            body["org_esid"] = org_esid
            body["updated_at"] = SubscriptionStateService._now().isoformat()
            es.index(index=INDEX_ORG_BILLING, id=org_esid, body=body, refresh="wait_for")
            return True
        except Exception as e:  # noqa: BLE001
            logging.warning("OrgBillingService._put_org_billing_doc(%r): %s", org_esid, e)
            return False

    @staticmethod
    def delete_org_billing_doc(org_esid: str) -> dict:
        """Remove an org's ``de_billing_org`` record outright.

        Called from the organization delete path so billing state cannot
        outlive its entity: a comped record with no parent org rendered in the
        superuser roster as a bare ESID with a live Clear button, and would
        hand a comp to any future org that reused the ESID.

        The full source is logged BEFORE the delete so the record is
        recoverable from the app log if the decision is ever questioned —
        the same discipline as the sweep. A missing doc is a
        successful no-op (idempotent), so callers can fire this unconditionally
        on every org delete. Returns ``{"ok": bool, "existed": bool}``.
        """
        if not org_esid:
            return {"ok": False, "existed": False, "error": "missing org"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "existed": False, "error": "elasticsearch unavailable"}
        existing = OrgBillingService._get_org_billing_doc(es, org_esid)
        if not existing:
            return {"ok": True, "existed": False}
        logging.info("deleting %s/%s  source=%s", INDEX_ORG_BILLING, org_esid, existing)
        try:
            es.delete(index=INDEX_ORG_BILLING, id=org_esid, refresh="wait_for", ignore=[404])
            return {"ok": True, "existed": True}
        except Exception as e:  # noqa: BLE001
            logging.warning("OrgBillingService.delete_org_billing_doc(%r): %s", org_esid, e)
            return {"ok": False, "existed": True, "error": str(e)}

    @staticmethod
    def has_org_billing_record(org_esid: str) -> bool:
        """True iff a ``de_billing_org`` doc exists for this org.

        ``get_state`` uses the ORG verdict only when a record exists; otherwise
        it falls through to the workspace/per-user path, so no org's access
        changes until a record is created (comp grant or migration). Best-effort:
        ES error / empty org → False.
        """
        if not org_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        return bool(OrgBillingService._get_org_billing_doc(es, org_esid))

    @staticmethod
    def set_org_comp(org_esid: str, set_by: str, *, workspace_esid: str = "") -> dict:
        """Comp an ORG (every member of the org inherits ``active (comped)``).
        Clears stale stripe/paid + the revoked marker, mirroring
        ``WorkspaceBillingService.set_comp``. Creates the org billing record if
        absent. ``workspace_esid`` is stamped for provenance when known."""
        if not org_esid:
            return {"ok": False, "error": "missing org"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        b = OrgBillingService._get_org_billing_doc(es, org_esid)
        if workspace_esid and not b.get("workspace_esid"):
            b["workspace_esid"] = workspace_esid
        b["created_at"] = b.get("created_at") or SubscriptionStateService._now().isoformat()
        b[COMPED_ACTIVE_KEY] = True
        b[COMPED_BY_KEY] = set_by or ""
        b[COMPED_AT_KEY] = SubscriptionStateService._now().isoformat()
        b[STRIPE_STATUS_KEY] = ""
        b[PAID_TIER_KEY] = False
        b[COMP_REVOKED_AT_KEY] = ""
        ok = OrgBillingService._put_org_billing_doc(es, org_esid, b)
        return {"ok": ok, "error": "" if ok else "write failed"}

    @staticmethod
    def clear_org_comp(org_esid: str) -> dict:
        """Remove an org comp; stamp ``comp_revoked_at`` when one was active."""
        if not org_esid:
            return {"ok": False, "error": "missing org"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        b = OrgBillingService._get_org_billing_doc(es, org_esid)
        was_comped = bool(b.get(COMPED_ACTIVE_KEY))
        b[COMPED_ACTIVE_KEY] = False
        b[COMPED_BY_KEY] = ""
        b[COMPED_AT_KEY] = ""
        if was_comped:
            b[COMP_REVOKED_AT_KEY] = SubscriptionStateService._now().isoformat()
        ok = OrgBillingService._put_org_billing_doc(es, org_esid, b)
        return {"ok": ok, "error": "" if ok else "write failed", "was_comped": was_comped}

    @staticmethod
    def get_org_state(org_esid: str) -> dict:
        """Trial/active/lapsed/comp_revoked verdict for an ORG, same dict shape
        as ``SubscriptionStateService.get_state`` (minus usage, which the caller
        overlays). Returns ``None`` when the org has NO billing record so the
        caller can fall through to the workspace/per-user path — emptiness is a
        missing record, NOT a lapsed default (that's the caller's floor).

        Precedence mirrors WorkspaceBillingService.get_workspace_state: comp wins,
        then comp_revoked, then live stripe_status, then paid_tier. There is no
        org-level trial today (trial stays a workspace/per-user concept); an org
        record with none of comp/paid/stripe set resolves to ``lapsed`` (its
        creation implies a deliberate billing decision was recorded).
        """
        if not org_esid:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        b = OrgBillingService._get_org_billing_doc(es, org_esid)
        if not b:
            return None

        is_paid = bool(b.get(PAID_TIER_KEY))
        is_comped = bool(b.get(COMPED_ACTIVE_KEY))
        comped_by = b.get(COMPED_BY_KEY) or ""
        comped_at = b.get(COMPED_AT_KEY) or ""

        def _resolved(state: str, *, paid: bool, comped: bool) -> dict:
            return {
                "state": state,
                "is_paid": paid,
                "is_comped": comped,
                "comped_by": comped_by if comped else "",
                "comped_at": comped_at if comped else "",
                "trial_started_at": "",
                "trial_ends_at": "",
                "trial_days_remaining": 0,
                "connection_cap": CONNECTION_CAP,
                "account_cap_per_connection": ACCOUNT_CAP_PER_CONNECTION,
                "connections_used": 0,
                "accounts_used": 0,
                "determined": True,
            }

        # comp wins (highest authority among non-superusers).
        if is_comped:
            return _resolved(STATE_ACTIVE, paid=is_paid, comped=True)

        # Revoked comp — lost complimentary access (entitlement-equivalent to
        # lapsed; data stays visible). No org-level trial to fall back to.
        if b.get(COMP_REVOKED_AT_KEY):
            return _resolved(STATE_COMP_REVOKED, paid=False, comped=False)

        # Live Stripe status is authoritative over the local guess.
        stripe_status = (b.get(STRIPE_STATUS_KEY) or "").strip().lower()
        mapped = _STRIPE_STATUS_TO_STATE.get(stripe_status)
        if mapped is not None:
            return _resolved(mapped, paid=is_paid, comped=False)

        if is_paid:
            return _resolved(STATE_ACTIVE, paid=True, comped=False)

        # A record with no comp/paid/stripe signal: lapsed (its existence means a
        # billing decision was recorded, just not a paying/comped one).
        return _resolved(STATE_LAPSED, paid=False, comped=False)

    @staticmethod
    def find_org_owner(org_esid: str) -> str | None:
        """The org's billing owner — the username holding ``owner`` on this org
        (``de_authz_user_org_role``). None if absent / on error. The roster's
        per-row clear targets this username (the clear flow resolves it back to
        this org)."""
        if not org_esid:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            resp = es.search(
                index="de_authz_user_org_role",
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"org_esid": org_esid}},
                                {"term": {"role_esid": "owner"}},
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
            logging.warning("OrgBillingService.find_org_owner(%r): %s", org_esid, e)
            return None

    @staticmethod
    def list_comped_orgs() -> list[dict]:
        """Roster of currently-comped ORGS for the superuser comp control.

        Scans ``de_billing_org`` for ``comped_active=True``; one row per comped
        org with its title (``de_asset_organization``) and owner (display + the
        notification recipient; the per-row clear targets ``org_esid``)::

            {"username", "email", "org_esid", "org_title", "comped_by",
             "comped_at", "workspace_esid"}

        A comped org with no resolvable owner is still listed (username = "");
        ``org_esid`` is the ES document id, so it is always present and the
        per-row clear can target such a row. Sorted by title. Empty
        list on ES error.
        """
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            resp = es.search(
                index=INDEX_ORG_BILLING,
                body={"query": {"term": {COMPED_ACTIVE_KEY: True}}, "size": 1000},
            )
            hits = resp.get("hits", {}).get("hits", [])
        except Exception as e:  # noqa: BLE001
            logging.warning("OrgBillingService.list_comped_orgs: %s", e)
            return []

        ids = [h.get("_id") for h in hits if h.get("_id")]
        titles: dict[str, str] = {}
        if ids:
            try:
                mres = es.mget(index=ORGANIZATION_INDEX, body={"ids": ids})
                for doc in mres.get("docs", []) or []:
                    if doc.get("found"):
                        t = (doc.get("_source") or {}).get("organization_title")
                        if t:
                            titles[doc.get("_id")] = t
            except Exception as e:  # noqa: BLE001
                logging.warning("OrgBillingService.list_comped_orgs titles: %s", e)

        out = []
        for h in hits:
            org_esid = h.get("_id")
            src = h.get("_source") or {}
            owner = OrgBillingService.find_org_owner(org_esid) or ""
            out.append(
                {
                    "username": owner,
                    "email": SubscriptionStateService.get_user_email(owner) if owner else "",
                    "org_esid": org_esid,
                    "org_title": titles.get(org_esid) or org_esid,
                    "comped_by": src.get(COMPED_BY_KEY) or "",
                    "comped_at": src.get(COMPED_AT_KEY) or "",
                    "workspace_esid": src.get("workspace_esid") or "",
                }
            )
        out.sort(key=lambda m: (m["org_title"] or "").lower())
        return out

    # ── Workspace → Org picker (superuser org-comp UI) ──────────────────
    #
    # The superuser comp control can target an ORG directly (not just a user's
    # primary org) via a Workspace → Org cascade. These two read helpers back
    # the picker: search workspaces by title, then list the orgs under the
    # chosen workspace. Mirror the fail-soft posture + size/min-query shape of
    # SubscriptionStateService.search_users.

    # longest ``?q=`` the workspace picker will send to ES.
    SEARCH_QUERY_MAX_LEN = 64

    @staticmethod
    def _escape_wildcard_query(text: str) -> str:
        """Backslash-escape the ES ``wildcard`` metacharacters (``*``, ``?``,
        ``\\``) so ``text`` matches literally inside the ``*...*`` envelope."""
        return re.sub(r"([\\*?])", r"\\\1", text)

    @staticmethod
    def search_workspaces(query: str, limit: int = 10) -> list[dict]:
        """Substring-search workspaces by ``workspace_title`` for the org-comp
        picker. Returns up to ``limit`` ``{"workspace_esid", "workspace_title"}``
        dicts sorted by title. Empty list on a too-short query (<2 chars), no
        match, or any ES error (fail-soft). Mirrors ``search_users``' 2-char
        floor + limit cap.

        Matching is a case-insensitive SUBSTRING against the ``keyword``-mapped
        title — see the comment on the query body for why this is not
        ``match_phrase_prefix`` (which 400s on a keyword field and made this
        return [] for every query).
        """
        q = (query or "").strip()
        if len(q) < 2:
            return []
        # ``q`` comes straight from ``?q=`` (superuser-only, but still
        # request input). Cap the length and escape the wildcard metacharacters
        # so the user's text is matched literally — an unescaped ``*``/``?``
        # widens the pattern and a long run of them is an expensive Lucene walk.
        q = OrgBillingService._escape_wildcard_query(q[: OrgBillingService.SEARCH_QUERY_MAX_LEN])
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            # ``workspace_title`` is mapped ``keyword`` with NO analysed
            # sub-field, so match_phrase_prefix is invalid against it — ES
            # rejects the whole search with a 400 ("Can only use phrase prefix
            # queries on text fields"), the except below swallows it, and the
            # picker silently shows an empty dropdown for EVERY query. A
            # case-insensitive wildcard is the keyword-native equivalent and
            # also gives substring (not just prefix) matching, which is what
            # the as-you-type box implies. Chosen over adding a ``.text``
            # sub-field because that needs a mapping change + reindex (existing
            # docs don't backfill) for no gain at this index's size.
            # ``case_insensitive`` matters: a bare prefix/wildcard on a keyword
            # is case-SENSITIVE, so "kav" would miss "Kavanah Media".
            resp = es.search(
                index=WORKSPACE_INDEX,
                body={
                    "query": {"wildcard": {"workspace_title": {"value": f"*{q}*", "case_insensitive": True}}},
                    "_source": ["workspace_title"],
                    "size": max(limit * 5, 50),
                },
            )
            hits = resp.get("hits", {}).get("hits", [])
        except Exception as e:  # noqa: BLE001 — boundary; fail soft
            logging.warning("OrgBillingService.search_workspaces(%r): %s", query, e)
            return []
        out = [
            {
                "workspace_esid": h.get("_id"),
                "workspace_title": (h.get("_source") or {}).get("workspace_title") or h.get("_id"),
            }
            for h in hits
            if h.get("_id")
        ]
        out.sort(key=lambda r: (r["workspace_title"] or "").lower())
        return out[:limit]

    @staticmethod
    def list_orgs_for_workspace(workspace_esid: str) -> list[dict]:
        """List every org under ``workspace_esid`` as
        ``[{"org_esid", "org_title"}]`` sorted by title, for the org-comp
        picker's org dropdown.

        ``de_asset_organization.workspace_esid`` is explicitly mapped ``keyword``
        , so a bare ``term`` matches exactly — no ``.keyword``
        sub-field needed (unlike de_asset_teams). Fail-soft → [].

        Also emits a one-shot WARNING with the count of org docs that carry NO
        ``workspace_esid`` (``must_not exists``): every org SHOULD have a
        workspace, so an orphan is a data-integrity signal to backfill, not a
        normal case the UI buckets. The count never affects the returned list.
        """
        if not workspace_esid:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            resp = es.search(
                index=ORGANIZATION_INDEX,
                body={
                    "query": {"term": {"workspace_esid": workspace_esid}},
                    "_source": ["organization_title"],
                    "size": 1000,
                },
            )
            hits = resp.get("hits", {}).get("hits", [])
        except Exception as e:  # noqa: BLE001 — boundary; fail soft
            logging.warning("OrgBillingService.list_orgs_for_workspace(%r): %s", workspace_esid, e)
            return []

        # Data-integrity diagnostic: surface orgs missing a workspace link.
        try:
            orphan = es.count(
                index=ORGANIZATION_INDEX,
                body={"query": {"bool": {"must_not": [{"exists": {"field": "workspace_esid"}}]}}},
            )
            orphan_count = int((orphan or {}).get("count") or 0)
            if orphan_count > 0:
                logging.warning(
                    "OrgBillingService.list_orgs_for_workspace: %d organization(s) have no "
                    "workspace_esid and are unreachable by the Workspace→Org comp picker — backfill needed.",
                    orphan_count,
                )
        except Exception as e:  # noqa: BLE001 — diagnostic only; never break the list
            logging.warning("OrgBillingService.list_orgs_for_workspace orphan-count: %s", e)

        out = [
            {
                "org_esid": h.get("_id"),
                "org_title": (h.get("_source") or {}).get("organization_title") or h.get("_id"),
            }
            for h in hits
            if h.get("_id")
        ]
        out.sort(key=lambda r: (r["org_title"] or "").lower())
        return out
