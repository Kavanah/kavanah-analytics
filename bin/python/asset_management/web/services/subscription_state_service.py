"""
Subscription State Service

Single source of truth for a user's *subscription state* — the
``trial`` / ``active`` / ``lapsed`` machine the Billing → Current Plan
page (display), the on-demand import gate (entitlement enforcement),
and the superuser comp control all read.

This service sits next to :mod:`source_limit_service` and reads the
same ES Security user-metadata fields. It deliberately does NOT
duplicate ``SourceLimitService``'s step-4 *tile* math — that service
answers "how many connector tiles light up in the wizard grid"; this
one answers "what is this user's subscription state, and how does
their usage map to the caps for that state". They share the trial
window inputs (``trial_started_at`` + ``FREE_TIER_TRIAL_DAYS``) so the
two surfaces never disagree about whether a trial is live.

State derivation::

    comped_active flag set   -> "active" (comp), for-profit full caps
    paid_tier flag set       -> "active" (paid)
    within FREE_TIER_TRIAL_DAYS of trial_started_at -> "trial"
    otherwise                -> "lapsed"

v1 derives only those three states. "past due" / "canceled" come from a
live Stripe subscription object, which does not exist yet
(``STRIPE_LIVE_PAYMENTS`` is false and the webhook is a
follow-up). The page is built so those slot in later without reshaping
this dict.

Metadata keys owned here:
  * ``paid_tier``        (bool)  — shared with SourceLimitService (read-only here)
  * ``trial_started_at`` (str)   — shared with SourceLimitService (read-only here)
  * ``comped_active`` (bool) — superuser comp grant
  * ``comped_by``        (str)   — superuser username who set the comp
  * ``comped_at``        (str)   — ISO-8601 UTC when the comp was set

Caps:
  * for-profit / active / comped-active: ``CONNECTION_CAP`` connections,
    ``ACCOUNT_CAP_PER_CONNECTION`` ad account per connection.
  * trial: ``TRIAL_SOURCE_CAP`` source, ``TRIAL_ACCOUNT_CAP`` account.
  * lapsed: same display caps as active, but new imports are blocked
    (enforced at the kickoff gate, not here — this service only reports
    state).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/python")
try:
    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client
except ImportError:
    from pathlib import Path

    _repo_python = Path(__file__).resolve().parents[3]
    if str(_repo_python) not in sys.path:
        sys.path.insert(0, str(_repo_python))
    from lib.es_helpers import get_elasticsearch_client, get_es_admin_client  # noqa: E402

# Shared with SourceLimitService — read-only here.
PAID_TIER_KEY = "paid_tier"
TRIAL_STARTED_AT_KEY = "trial_started_at"

# Owned by this service.
COMPED_ACTIVE_KEY = "comped_active"
COMPED_BY_KEY = "comped_by"
COMPED_AT_KEY = "comped_at"

# follow-up (2026-06-05): a superuser never pays for a subscription.
# Holding this ES Security role resolves the account to ``active`` regardless
# of trial/lapse/Stripe state, and (because the import gate reads get_state)
# bypasses the import block too — the highest authority, above even comp.
SUPERUSER_ROLE = "superuser"

# live Stripe subscription status (scaffold). NOTHING writes this
# field yet: the Stripe webhook (a follow-up, gated by
# STRIPE_LIVE_PAYMENTS) will set it to the raw Stripe ``Subscription.status``
# string. When present + recognized it OVERRIDES the locally-derived
# trial/active/lapsed verdict — a real subscription is authoritative over the
# local trial-window math. Until the webhook lands this is inert (no doc has
# the key), so the state machine behaves exactly as before.
# Sentinel ``err`` value from ``_get_user`` when the target user does not
# exist (empty result OR a 404 NotFoundError). Callers (the comp endpoint)
# map this to a clean 404 instead of string-matching the raw exception.
NOT_FOUND_ERR = "user_not_found"

# Stamp recording that the "trial ending soon" email has been sent, so the
# daily cron scan emails each trialing user exactly once.
TRIAL_ENDING_EMAIL_SENT_KEY = "trial_ending_email_sent_at"

STRIPE_STATUS_KEY = "stripe_status"
# The Stripe Customer id (``cus_...``) stamped at subscription creation so
# the webhook can map an incoming event's customer back to our user.
STRIPE_CUSTOMER_ID_KEY = "stripe_customer_id"

# item 5b — count of self-serve add-on data connections the org has
# PURCHASED (a paid Stripe add-on item, quantity N). The effective connection
# cap for a paid/active org is ``CONNECTION_CAP + addon_connections`` (add-ons
# extend the included 6). Stamped by the add-on purchase route; only meaningful
# for a paying org (trial/lapsed keep the base caps). 0 / absent = none bought.
ADDON_CONNECTIONS_KEY = "addon_connections"

# (2026-06-12) — ISO datetime at which add-ons marked for removal
# actually stop counting. The Remove flow deletes the Stripe item immediately
# with NO proration credit (the period is already paid) and stamps this with
# the period end: capacity keeps counting toward the cap until then, the next
# bill excludes the add-ons. ``get_state`` treats ``addon_connections`` as 0
# once now >= this stamp. Cleared by any new purchase/adjust stamp (re-buying
# cancels the scheduled removal). Empty / absent = no removal scheduled.
ADDON_REMOVE_AT_KEY = "addon_remove_at"

# (2026-06-12, reductions) — the TARGET quantity the pending change
# lands on when ``addon_remove_at`` passes. 0 = full removal; N = a scheduled
# reduction (decreases never refund: the paid capacity stays until period end,
# then the cap drops to the target). Only meaningful while ADDON_REMOVE_AT_KEY
# is set; stale values are inert once a new purchase stamp clears the mark.
ADDON_PENDING_QTY_KEY = "addon_pending_qty"

# Stamp recording that a comp was GRANTED then REVOKED (admin cleared it).
# Lets get_state distinguish "lapsed from trial" from "lost complimentary
# access" so the Current Plan page can show the right copy. Cleared whenever
# the user becomes comped/paid again (so it never lingers past a re-grant).
COMP_REVOKED_AT_KEY = "comp_revoked_at"

# State constants — the page, the gate, and the comp control compare
# against these so a typo can't silently mis-gate.
STATE_TRIAL = "trial"
STATE_ACTIVE = "active"
STATE_LAPSED = "lapsed"
# Stripe scaffold — surfaced only when a stripe_status is present.
STATE_PAST_DUE = "past_due"  # payment failed, in Stripe's grace/retry window
STATE_CANCELED = "canceled"  # subscription ended — entitlement-equivalent to lapsed
# follow-up — admin revoked a previously-granted comp. Entitlement-
# equivalent to lapsed (imports blocked), but surfaced with its own copy
# ("you've lost complimentary access").
STATE_COMP_REVOKED = "comp_revoked"

# Stripe ``Subscription.status`` → our state. active/trialing map to active;
# past_due is its own grace state; canceled/unpaid/incomplete_expired end the
# subscription. An unrecognized status falls through to the local derivation
# (treated as "no authoritative override").
_STRIPE_STATUS_TO_STATE = {
    "active": STATE_ACTIVE,
    "trialing": STATE_ACTIVE,
    "past_due": STATE_PAST_DUE,
    "canceled": STATE_CANCELED,
    "unpaid": STATE_CANCELED,
    "incomplete_expired": STATE_CANCELED,
}

# States that BLOCK new data imports (the entitlement gate). lapsed and
# canceled are hard stops; past_due is a grace period (imports still allowed,
# the page just warns). trial/active import freely.
_IMPORT_BLOCKED_STATES = frozenset({STATE_LAPSED, STATE_CANCELED, STATE_COMP_REVOKED})

# Caps. The for-profit/active caps mirror the Billing section's
# ``_BILLING_CONNECTION_CAP`` (6 connections, 1 ad account per
# connection — bumped 5→6 item 5a). A comp grants for-profit
# full caps (decision 2026-06-04).
CONNECTION_CAP = 6
ACCOUNT_CAP_PER_CONNECTION = 1
TRIAL_SOURCE_CAP = 1
TRIAL_ACCOUNT_CAP = 1

# (user decision, a teammate 2026-09-18): there are NO caps on data
# sources. The constants above keep describing the historical plan shape
# (the add-on arithmetic and older tests still read them, and ``get_state``
# still reports them), but nothing ENFORCES them while this is False:
# ``inactive_source_keys_for_user`` reports no inactive sources (so the
# importer's Condition B can never fire), the Current Plan page splits with
# cap=None (no "Not imported" group), and the add-on purchase controls hide
# unless the org already owns add-ons. Condition A (lapsed / canceled /
# comp_revoked → no imports) is untouched. Flip to True to restore caps.
SOURCE_CAPS_ENABLED = False

# follow-up (2026-06-05): what the billing "Data connections" /
# "Ad accounts" counts and the Current-Plan connection list must measure.
#
# A user authorizes ONE Meta grant but the data-connectors wizard can leave
# several de_connection rows for it: re-finishing the wizard creates duplicate
# active rows (same provider_account_id) and abandoned pending_token rows whose
# provider_account_id is a ``wizard-pending-*`` placeholder. Counting raw
# connection docs over-counts both. The billing unit of a "connection" is the
# distinct REAL provider account: group by (provider, provider_account_id),
# dropping placeholder/pending rows. (Dedup of the underlying ES rows is a
# separate connection/wizard bug — tracked outside.)
#
# "Ad accounts" counts only ad-account-type assets — a Facebook page or
# Instagram account under the same grant is not an ad account.
_AD_ACCOUNT_ASSET_TYPES = frozenset(
    {
        "fb_ad_account",
        "google_ads_customer",
        "ga4_property",
    }
)
# provider_account_id prefix the wizard stamps before the real OAuth user id
# arrives (ConnectionService.create_pending_connection placeholder). Rows still
# carrying it never completed the grant, so they don't count as a connection.
# Canonical definition lives in connection_service.PENDING_ACCOUNT_ID_PREFIX;
# mirrored here as a literal so this module has no import-time dependency on
# ConnectionService (it's imported lazily inside the methods). Keep in sync.
_PENDING_ACCOUNT_ID_PREFIX = "wizard-pending-"


class SubscriptionStateService:
    """Per-user subscription-state lookup for the Billing Current Plan page."""

    @staticmethod
    def _get_user(es, username: str):
        """Fetch the ES Security user record. Returns (user_data, err).

        Mirrors ``SourceLimitService._get_user`` / ``UserProfileService.
        _get_user`` — same read-then-merge contract so the three services
        play nicely under one shared mock.

        ``err`` is the sentinel ``NOT_FOUND_ERR`` when the user genuinely
        doesn't exist — whether ES returned an empty result OR raised a 404
        ``NotFoundError`` (the ES client raises rather than returning {} for a
        missing security user). Callers map that sentinel to a clean 404
        instead of string-matching the raw exception.
        """
        try:
            response = es.security.get_user(username=username)
            user_data = dict(response).get(username)
            if user_data is None:
                return None, NOT_FOUND_ERR
            return user_data, None
        except Exception as e:  # noqa: BLE001 — surface as a soft error, not a 500
            logging.error("SubscriptionStateService: failed to fetch user '%s': %s", username, e)
            # A 404 from the ES client means the user doesn't exist. Detect it
            # by status (meta.status) or class name, across client versions.
            status = getattr(getattr(e, "meta", None), "status", None)
            if status == 404 or "NotFoundError" in type(e).__name__:
                return None, NOT_FOUND_ERR
            return None, str(e)

    @staticmethod
    def _now() -> datetime:
        """Indirection so tests can monkeypatch the clock at the service."""
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse_iso(value: str) -> datetime | None:
        """Parse an ISO-8601 string into an aware UTC datetime, or None."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _domain_from_url(url: str) -> str:
        """extract the bare site domain from a Disciple.Tools
        instance URL (``https://dt.example.org/`` -> ``dt.example.org``).

        Strips scheme, any path/query, and a leading ``www.``. Returns the
        input stripped of whitespace when it can't be parsed as a URL (so a
        bare ``dt.example.org`` already-domain value passes through). Empty
        in -> empty out.
        """
        url = (url or "").strip()
        if not url:
            return ""
        from urllib.parse import urlparse

        netloc = urlparse(url).netloc
        if not netloc:
            # No scheme — urlparse put everything in ``path``. Take the first
            # path segment as the host.
            netloc = url.split("/", 1)[0]
        # Drop any port and userinfo, then a leading www.
        netloc = netloc.split("@")[-1].split(":")[0]
        if netloc.lower().startswith("www."):
            netloc = netloc[4:]
        return netloc

    @staticmethod
    def canonical_connections(username: str) -> list[dict]:
        """Collapse a principal's raw ``de_connection`` rows into the
        billing unit of a "connection": one entry per distinct REAL
        provider account.

        Why: the wizard can leave several connection docs for a single
        Meta grant — duplicate active rows (same provider_account_id) and
        abandoned ``wizard-pending-*`` rows. The billing cap counts the
        distinct provider account, not the raw doc. Grouping key is
        (provider, provider_account_id); placeholder/empty account ids are
        dropped. Ad-account assets are de-duplicated by provider_asset_id
        across the merged rows (the same ad account can appear under more
        than one duplicate connection doc).

        Returns a list of::

            {
              "provider": "facebook",
              "status":   "active",          # best status across the group
              "ad_accounts": [               # ad-account-type assets only
                  {"name": "Kavanah Media USA", "id": "65290367"}, ...
              ],
            }

        Fail-soft: any error returns ``[]`` (caller renders zero usage /
        an empty list, never a 500). Shared by ``_usage`` and the Current
        Plan page's connection list so the counts and the list can never
        disagree.
        """
        try:
            from asset_management.web.services.connection_service import (
                STATUS_REVOKED,
                ConnectionService,
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("SubscriptionStateService canonical_connections: import failed: %s", exc)
            return []

        try:
            rows = ConnectionService.list_connections_for_principal(username)
        except Exception as exc:  # noqa: BLE001
            logging.warning("SubscriptionStateService canonical_connections: list failed for %r: %s", username, exc)
            return []

        # Group by the real provider account. Order is preserved by first
        # appearance so the display list is stable.
        groups: dict[tuple[str, str], dict] = {}
        for row in rows:
            src = row.get("_source", {}) or {}
            provider = (src.get("provider") or "").strip()
            account_id = (src.get("provider_account_id") or "").strip()
            # Drop placeholder / pre-OAuth rows — they don't represent a
            # completed connection to a real account.
            if not account_id or account_id.startswith(_PENDING_ACCOUNT_ID_PREFIX):
                continue
            key = (provider, account_id)
            group = groups.setdefault(
                key,
                {"provider": provider, "status": src.get("status", "") or "", "ad_accounts": {}},
            )
            # Prefer an "active" status if any row in the group is active.
            if (src.get("status") or "") == "active":
                group["status"] = "active"

            conn_esid = row.get("_id")
            if not conn_esid:
                continue
            try:
                assets = ConnectionService.list_assets_for_connection(conn_esid)
            except Exception as exc:  # noqa: BLE001
                logging.warning(
                    "SubscriptionStateService canonical_connections: assets failed for %r: %s",
                    conn_esid,
                    exc,
                )
                assets = []
            for asset in assets:
                a_src = asset.get("_source", {}) or {}
                if (a_src.get("asset_type") or "") not in _AD_ACCOUNT_ASSET_TYPES:
                    continue  # pages / IG accounts are not ad accounts
                provider_asset_id = (a_src.get("provider_asset_id") or "").strip()
                # De-dup the same ad account appearing under duplicate
                # connection docs. Fall back to the asset esid when an id
                # is somehow absent so distinct rows aren't merged.
                dedup_key = provider_asset_id or asset.get("_id", "")
                group["ad_accounts"].setdefault(
                    dedup_key,
                    {
                        "name": (a_src.get("label") or "").strip() or provider_asset_id or dedup_key,
                        "id": provider_asset_id,
                        # these are ad-account-type assets only (the
                        # type filter above), and provider_asset_id is the real
                        # human-facing ad-account id, NOT an esid. Carry it as
                        # provider_id + tag the kind so the display layer treats
                        # the per-principal path like the org rollup.
                        "provider_id": provider_asset_id,
                        "slug": "",
                        "kind": "ad_account",
                    },
                )

        # item 6 — exclude revoked connections. A group's status is
        # ``revoked`` only when EVERY row in it is revoked (any active row
        # upgrades the group to "active" above), i.e. the user fully revoked
        # this connection. Filtering here — the SHARED source for both the
        # Current Plan list and the "N of M used" count — keeps the two in
        # sync and frees the revoked connection's slot toward the cap.
        return [
            {
                "provider": g["provider"],
                "status": g["status"],
                "ad_accounts": list(g["ad_accounts"].values()),
            }
            for g in groups.values()
            if (g["status"] or "") != STATUS_REVOKED
        ]

    # connector-card ``key`` (connector_status_service.CONNECTORS) →
    # the billing ``provider`` label the per-principal ``canonical_connections``
    # uses, for connector cards whose linked assets are AD ACCOUNTS (the only
    # ones that count toward "Ad accounts"). The card ``key`` vocabulary differs
    # from the de_connection ``provider`` vocabulary: the GA4 card key is
    # ``google_analytics`` but its provider is ``ga4``. Cards not listed here
    # contribute a connection row (provider == key) with NO ad accounts (Echo /
    # Chatwoot / YouTube / Disciple.Tools / Meta-pages-only).
    _CARD_KEY_TO_AD_ACCOUNT_PROVIDER = {
        "meta_ads": "facebook",
        "google_ads": "google_ads",
        "google_analytics": "ga4",
    }

    @staticmethod
    def resolve_primary_org_esid(username: str) -> str | None:
        """the org whose connectors the Current Plan page shows.

        Resolution order (fail-soft, first hit wins). "Primary org" is
        reconciled to the team's first org (DECISIONS.md 2026-06-16); the
        ``is_primary`` role flag is preferred when set but NOT relied upon —
        the staging org-owner test user has no team role and its org role is
        ``is_primary:false``, so a flag-only resolver returns nothing:

        1. the user's ``is_primary == true`` org role, else
        2. any org role for the user (deterministic by sort), else
        3. the org reached from the user's primary team's
           ``organization_esid[0]`` (the team-array-positional definition).

        Returns ``None`` when nothing resolves — the caller then falls back to
        the per-principal connection list (never worse than today).
        """
        if not username:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None

        # 1 + 2: org-role rows.
        try:
            resp = es.search(
                index="de_authz_user_org_role",
                body={
                    "query": {"term": {"username": username}},
                    "_source": ["org_esid", "is_primary"],
                    "size": 100,
                },
            )
            rows = [h.get("_source") or {} for h in resp.get("hits", {}).get("hits", [])]
            primary = [r["org_esid"] for r in rows if r.get("is_primary") and r.get("org_esid")]
            if primary:
                return primary[0]
            any_org = sorted(r["org_esid"] for r in rows if r.get("org_esid"))
            if any_org:
                chosen = any_org[0]
                # Surface drift: when the org role isn't flagged primary, log so
                # the #3 (primary-attribution) work can see flag-vs-array gaps.
                logging.info(
                    "resolve_primary_org_esid(%r): no is_primary org role; using sorted-first org %r",
                    username,
                    chosen,
                )
                return chosen
        except Exception as e:  # noqa: BLE001
            logging.warning("resolve_primary_org_esid(%r): org-role lookup failed: %s", username, e)

        # 3: team-array-positional fallback (primary team → organization_esid[0]).
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
            # Exclude the system-admin sentinel row
            # (team_esid="__all__" in de_authz_user_team_role) — it means
            # "all teams", not a real team. Without this filter a superuser's
            # sentinel row was selected as team_esid and es.get(id="__all__")
            # 404'd on every call (fail-soft, but log-spamming and the org
            # never resolved via this leg). Mirrors role_filter_service's
            # ALL_SCOPES_SENTINEL handling.
            _ALL_SCOPES_SENTINEL = "__all__"
            primary_team = [
                r["team_esid"]
                for r in rows
                if r.get("is_primary") and r.get("team_esid") and r["team_esid"] != _ALL_SCOPES_SENTINEL
            ]
            fallback_teams = sorted(
                r["team_esid"] for r in rows if r.get("team_esid") and r["team_esid"] != _ALL_SCOPES_SENTINEL
            )
            team_esid = (primary_team or fallback_teams or [None])[0]
            if team_esid:
                team_doc = es.get(index="de_asset_teams", id=team_esid)
                org_field = (team_doc.get("_source") or {}).get("organization_esid")
                if isinstance(org_field, list):
                    return org_field[0] if org_field else None
                return org_field or None
        except Exception as e:  # noqa: BLE001
            logging.warning("resolve_primary_org_esid(%r): team-array fallback failed: %s", username, e)
        return None

    @staticmethod
    def canonical_connections_for_org(org_esid: str) -> list[dict]:
        """the ORG's connections + ad accounts, sourced via team docs.

        The Billing Current Plan page must show an org owner ALL of their org's
        data connections, not just the ones whose OAuth grant they personally
        authored. ``de_connection`` is keyed on ``principal`` (the authoring
        user) and carries no team/org field, so the per-principal
        ``canonical_connections`` returns an empty list for an org owner whose
        teams were wired by other members (verified on staging 2026-06-16:
        ``de_connection`` had 0 rows for the org-owner test user while the org's 17 teams
        were fully wired by other members).

        Source of truth is therefore the TEAM docs: org → teams →
        ``connector_status_service.get_team_connectors`` (the same resolution the
        Data Connectors page uses) → each configured connector card. The result
        is shaped exactly like ``canonical_connections`` so ``_usage`` and the
        Current Plan list can consume either interchangeably::

            {"provider": ..., "status": "active"|"needs_setup"|..., "ad_accounts": [...]}

        **Counting rule (a teammate 2026-06-16): the billing unit is the distinct
        provider ASSET — the "source".** Each distinct linked asset (FB page, IG
        account, GA4 property, Google Ads customer, Echo tenant, …) counts ONCE
        across the whole org, deduped by its asset esid: two teams sharing one FB
        page = 1 source; two teams with different FB pages = 2 sources; an ad
        account shared by N teams = 1 source. The Meta card contributes BOTH its
        FB pages (``linked_assets``) and IG accounts (``ig_linked_assets``) as
        separate sources. One connection row per provider for display, with each
        provider's distinct sources listed under it. ``_usage.connections_used``
        is the total distinct-source count (sum across providers), so "N of M
        used" reflects sources, not teams. Fail-soft: any error returns ``[]``.
        """
        if not org_esid:
            return []
        try:
            from asset_management.web.services.connector_status_service import (
                get_team_connectors,
                list_teams_for_org,
                primary_org_for_team,
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("canonical_connections_for_org: import failed: %s", exc)
            return []

        try:
            teams = list_teams_for_org(org_esid)
        except Exception as exc:  # noqa: BLE001
            logging.warning("canonical_connections_for_org: list_teams failed for %r: %s", org_esid, exc)
            return []

        # single-owner attribution for SHARED assets. The same asset
        # esid can be linked by teams across multiple orgs (e.g. one chatwoot
        # account wired onto teams whose primary orgs are org A, org B, and org C).
        # An asset stamped with ``owning_team_esid`` is billed to exactly ONE org
        # — its owning team's PRIMARY org — so it must be DROPPED from every other
        # org's rollup. ``_owned_by_other_org`` answers "is this source owned by a
        # team whose primary org is not this org?" with a per-call cache so a team
        # doc is fetched at most once. An UN-stamped asset (owning_team_esid empty)
        # is never dropped — it keeps the earlier behaviour until backfilled.
        _owner_org_cache: dict[str, str] = {}

        def _owned_by_other_org(asset: dict) -> bool:
            owner_team = (asset.get("owning_team_esid") or "").strip()
            if not owner_team:
                return False  # unstamped → not owner-attributable; keep it
            owner_org = _owner_org_cache.get(owner_team)
            if owner_org is None:
                owner_org = primary_org_for_team(owner_team)
                _owner_org_cache[owner_team] = owner_org
            # Fail-soft: if the owning team's org can't be resolved (""), don't
            # drop the source — leaving it visible is safer than vanishing it.
            return bool(owner_org) and owner_org != org_esid

        # One group per PROVIDER (for the display row); each provider's sources
        # are deduped by asset esid across ALL teams in the org, so a shared
        # asset counts once and distinct assets each count. ``ad_accounts`` holds
        # those distinct sources (the page renders them under the provider).
        groups: dict[str, dict] = {}
        for team in teams:
            team_esid = team.get("esid") or ""
            if not team_esid:
                continue
            try:
                # allowed_workspaces=None: the caller has already resolved the
                # acting user to THIS org; the org→teams walk is the scope. No
                # second tenant check here (it would deny on a None workspace).
                payload = get_team_connectors(team_esid, allowed_workspaces=None)
            except Exception as exc:  # noqa: BLE001
                logging.warning("canonical_connections_for_org: team %r failed: %s", team_esid, exc)
                continue
            if not payload:
                continue
            for card in payload.get("connectors", []):
                if not card.get("configured"):
                    continue
                key = card.get("key") or ""
                provider = SubscriptionStateService._CARD_KEY_TO_AD_ACCOUNT_PROVIDER.get(key, key)
                # Every distinct linked asset is a source. The Meta card splits
                # into FB pages (linked_assets) + IG accounts (ig_linked_assets);
                # both count. Other cards expose only linked_assets. A card with
                # no linked assets (e.g. a URL connector) still anchors a source
                # on the team so it isn't lost.
                assets = list(card.get("linked_assets") or []) + list(card.get("ig_linked_assets") or [])
                group = groups.setdefault(
                    provider,
                    {
                        "provider": provider,
                        # the connector's display label (e.g.
                        # "Disciple.Tools", "Echo Messaging") so the page never
                        # shows the raw underscore provider key.
                        "label": card.get("label") or "",
                        "status": card.get("state") or "",
                        "ad_accounts": {},
                    },
                )
                if not group.get("label"):
                    group["label"] = card.get("label") or ""
                if (card.get("state") or "") == "configured":
                    group["status"] = "configured"
                if assets:
                    for a in assets:
                        # a shared asset owned by another org's team
                        # belongs to that org's billing, not this one. Drop it
                        # here so it leaves both the display list AND the usage
                        # count (this is the single source for both).
                        if _owned_by_other_org(a):
                            continue
                        dedup_key = a.get("esid") or a.get("title") or ""
                        if not dedup_key:
                            continue
                        # carry the REAL provider id / slug / kind so
                        # the Current Plan page can show human-facing IDs (FB
                        # ad-account id, FB page slug+id) instead of the ESID,
                        # and split the Meta card into Ad accounts / Pages.
                        group["ad_accounts"].setdefault(
                            dedup_key,
                            {
                                "name": a.get("title") or dedup_key,
                                "id": a.get("esid") or "",
                                "provider_id": a.get("provider_id") or "",
                                "slug": a.get("slug") or "",
                                "kind": a.get("kind") or "",
                            },
                        )
                else:
                    # No linked asset to dedup on — count the team itself as the
                    # source so a URL/team-scoped connector still registers once
                    # per team (distinct teams = distinct sources). —
                    # Disciple.Tools surfaces its site DOMAIN (from the card's
                    # instance_url), not the ESID; other URL/team-scoped cards
                    # fall back to their label.
                    dt_domain = ""
                    if key == "disciple_tools":
                        dt_domain = SubscriptionStateService._domain_from_url(card.get("instance_url") or "")
                    group["ad_accounts"].setdefault(
                        f"__team__{team_esid}",
                        {
                            # For Disciple.Tools the DOMAIN is the display name
                            # (no separate id to append). Other URL/team-scoped
                            # cards fall back to the label.
                            "name": dt_domain or card.get("label") or provider,
                            "id": "",
                            "provider_id": "",
                            "slug": "",
                            "kind": "site" if key == "disciple_tools" else "",
                            # a team-scoped / URL connector source (echo,
                            # Disciple.Tools) has no asset esid / provider id, so the
                            # over-cap pre-check can only match it by its owning team.
                            # Carry the team esid on the source row (the synthetic
                            # ``__team__`` dict KEY is dropped by .values() below).
                            # Additive: the display layer ignores unknown keys.
                            "team_esid": team_esid,
                        },
                    )

        # drop a provider group whose every source was filtered out as
        # owned by another org. Without this a fully-foreign-owned connector would
        # render an empty provider row with a zero count. (A provider that legit-
        # imately has sources, including team-anchored URL connectors, is kept.)
        return [
            {
                "provider": g["provider"],
                "label": g.get("label") or "",
                "status": g["status"],
                "ad_accounts": list(g["ad_accounts"].values()),
            }
            for g in groups.values()
            if g["ad_accounts"]
        ]

    @staticmethod
    def connections_for_display(username: str) -> list[dict]:
        """the connection list the Current Plan page shows for
        ``username``, resolved at the ORG scope when the user owns an org.

        Resolves the user's primary org (``resolve_primary_org_esid``) and, when
        one is found, returns the org-scoped rollup (org → teams → connector
        cards). Falls back to the per-principal ``canonical_connections`` when no
        org resolves OR the org rollup is empty — so a user with no org, or whose
        org-scope resolution blips, is never worse off than the earlier
        behaviour. SINGLE source for both the displayed list
        (``_billing_current_connections``) and the "N of M used" count
        (``_usage``) so they can never disagree.
        """
        try:
            org_esid = SubscriptionStateService.resolve_primary_org_esid(username)
            if org_esid:
                org_conns = SubscriptionStateService.canonical_connections_for_org(org_esid)
                if org_conns:
                    return org_conns
        except Exception as exc:  # noqa: BLE001 — fall back to per-principal
            logging.warning("connections_for_display: org path failed for %r: %s", username, exc)
        return SubscriptionStateService.canonical_connections(username)

    @staticmethod
    def partition_connections_by_cap(connections: list[dict], effective_cap: int | None) -> dict:
        """split a connection list into ACTIVE vs INACTIVE *sources*
        by the paid connection cap, returning each source individually.

        This is the single source of truth for the active/inactive partition the
        Current Plan "Not imported" badge and the importer billing
        pre-check both read — so the importer's verdict can never drift
        from the badge the user sees.

        Partition rule (unchanged): sources are flattened in display
        order — for each connection in ``connections`` order, each entry in its
        ``ad_accounts`` list in order — and the FIRST ``effective_cap`` sources are
        active; every source beyond the cap is inactive. ``effective_cap=None``
        means "no cap" → every source is active (older / cap-free callers).

        ``connections`` is the list from ``connections_for_display`` (each item:
        ``{"provider", "label"?, "status", "ad_accounts": [...]}``); each
        ``ad_accounts`` row carries ``id`` (asset esid), ``provider_id`` (the real
        provider id), ``kind``, ``name``. A team-scoped / URL connector with no
        linked asset contributes a single synthetic source whose dedup key is
        ``__team__<team_esid>`` (id == "").

        Returns ``{"active": [...], "inactive": [...]}`` where each list holds the
        SAME raw source rows, each annotated with its owning ``provider`` (so a
        caller can tell which connector a source belongs to). Pure: no IO, never
        raises on well-formed input.
        """
        cap = None if effective_cap is None else max(0, int(effective_cap))
        seen = 0
        active: list[dict] = []
        inactive: list[dict] = []
        for conn in connections or []:
            provider = conn.get("provider", "") or ""
            for acct in conn.get("ad_accounts", []) or []:
                row = dict(acct)
                row["provider"] = provider
                if cap is None or seen < cap:
                    active.append(row)
                else:
                    inactive.append(row)
                seen += 1
        return {"active": active, "inactive": inactive}

    @staticmethod
    def caps_enabled() -> bool:
        """whether connection / account caps are enforced anywhere.

        Reads the module constant at call time so a test can flip it with
        ``monkeypatch.setattr(subscription_state_service, "SOURCE_CAPS_ENABLED", True)``
        to exercise the (now dormant) cap machinery.
        """
        return bool(SOURCE_CAPS_ENABLED)

    @staticmethod
    def _is_complimentary(username: str, state: dict) -> bool:
        """is ``username``'s account complimentary (uncapped)?

        A complimentary account is covered in FULL with no connection cap — the
        included-6 cap + over-cap split is a PAID-plan concept only. Two ways an
        account is complimentary, mirroring the Billing page (,
        ``is_superuser or state.get("is_comped")``):

        * an admin-granted comp — ``get_state`` carries ``is_comped=True`` for a
          comped org (the org-billing path) or a comped per-user record;
        * the superuser role — ``get_state`` collapses a superuser to ``active``
          BEFORE reading any comp, so ``is_comped`` is ``False`` for them and the
          role must be checked directly (the Billing page does this with the
          session ``is_superuser`` flag, which the importer has no access to).

        The comp check is free (already on ``state``); the superuser role lookup
        only runs when the comp check is False, and fails CLOSED (returns
        ``False``) on any error so a lookup blip can't accidentally uncap a
        paying account.
        """
        if state.get("is_comped"):
            return True
        try:
            es = get_elasticsearch_client()
            if es is None:
                return False
            user_data, err = SubscriptionStateService._get_user(es, username)
            if err or user_data is None:
                return False
            return SUPERUSER_ROLE in (user_data.get("roles") or [])
        except Exception:  # noqa: BLE001 — fail closed: don't uncap on a lookup blip
            return False

    @staticmethod
    def inactive_source_keys_for_user(username: str) -> dict:
        """the set of identifiers for ``username``'s sources that are
        INACTIVE under billing because they are OVER the connection cap.

        Resolves the user's effective connection cap (``get_state`` →
        ``connection_cap`` = base + add-ons) and their display connection list
        (``connections_for_display`` — org-scoped when they own an org, else
        per-principal), partitions per-source by the cap
        (``partition_connections_by_cap``), and collects the inactive (overflow)
        sources' identifiers into three match-sets the importer pre-check can test
        whatever identifier it has in scope against:

            {
              "provider_ids": {<real provider id>, ...},   # GA4 property id,
                                                            # Google Ads customer id,
                                                            # YouTube channel id,
                                                            # IG profile id, FB ad-acct id
              "asset_esids":  {<de_asset_* esid>, ...},     # the source's `id`
              "team_esids":   {<team esid>, ...},           # from `__team__<esid>` keys
                                                            # (team-scoped connectors: echo)
            }

        This is the OVER-CAP (Condition B) verdict ONLY — the no-subscription
        (Condition A) verdict is ``is_import_blocked``. Empty strings are never
        added (so a missing provider_id / esid can't false-match an importer that
        also has an empty value).

        Fail-SOFT: any error (ES outage, resolution blip) returns all-empty sets,
        so the pre-check blocks NOTHING — a transient lookup failure must never
        wrongly stop a paying source's import (mirrors ``is_import_blocked``'s
        fail-open posture).
        """
        empty = {"provider_ids": set(), "asset_esids": set(), "team_esids": set()}
        if not username:
            return empty
        # no source caps: nothing is ever inactive under billing.
        if not SubscriptionStateService.caps_enabled():
            return empty
        try:
            state = SubscriptionStateService.get_state(username)
            # A non-determined state (ES blip / missing user) must not over-block:
            # fail open with no inactive sources.
            if not state.get("determined", False):
                return empty
            # a complimentary account (comped org / superuser) is
            # UNCAPPED, exactly as the Billing page treats it. Without
            # this the page reads "all covered, no plan limit" while the importer
            # gate kept skipping connections 7+ as `over_cap` — display and
            # enforcement drifted. `None` makes partition_connections_by_cap put
            # every source in the active group (no over-cap split → empty key
            # sets → the pre-check blocks nothing for the comp's overflow).
            if SubscriptionStateService._is_complimentary(username, state):
                effective_cap = None
            else:
                effective_cap = int(state.get("connection_cap", CONNECTION_CAP))
            connections = SubscriptionStateService.connections_for_display(username)
            inactive = SubscriptionStateService.partition_connections_by_cap(connections, effective_cap)["inactive"]
        except Exception as exc:  # noqa: BLE001 — fail open: block nothing on error
            logging.warning("inactive_source_keys_for_user(%r) failed: %s", username, exc)
            return empty

        provider_ids: set = set()
        asset_esids: set = set()
        team_esids: set = set()
        for src in inactive:
            pid = (src.get("provider_id") or "").strip()
            if pid:
                provider_ids.add(pid)
            esid = (src.get("id") or "").strip()
            if esid:
                asset_esids.add(esid)
            # Team-scoped / URL connectors (echo, Disciple.Tools) have no asset esid
            # or provider id — the over-cap pre-check can only match them by their
            # owning team, carried on the source row as ``team_esid`` by
            # canonical_connections_for_org.
            team = (src.get("team_esid") or "").strip()
            if team:
                team_esids.add(team)
        return {"provider_ids": provider_ids, "asset_esids": asset_esids, "team_esids": team_esids}

    @staticmethod
    def _usage(es, username: str) -> dict:
        """Count the user's data sources + ad accounts for the "N of M" caps.

        Org owners: a "source" is a distinct provider ASSET — every
        FB page, IG account, GA4 property, Google Ads customer, Echo tenant, …
        deduped across the org's teams (a teammate 2026-06-16). ``connections_used``
        is the total distinct-source count; ``accounts_used`` is the subset that
        are literal ad accounts (Google Ads / GA4 / — the providers in
        ``_CARD_KEY_TO_AD_ACCOUNT_PROVIDER`` minus the page/IG organic sources).

        Non-org users keep the per-principal semantics: ``connections_used`` =
        distinct provider-account connections, ``accounts_used`` = ad-account
        assets under them. Fail-soft: any error returns zero usage.
        """
        org_esid = None
        try:
            org_esid = SubscriptionStateService.resolve_primary_org_esid(username)
        except Exception:  # noqa: BLE001
            org_esid = None

        if org_esid:
            connections = SubscriptionStateService.canonical_connections_for_org(org_esid)
            if connections:
                # Source = each distinct asset under any provider row.
                connections_used = sum(len(c["ad_accounts"]) for c in connections)
                # Ad accounts = sources under the literal ad-account providers
                # (Google Ads + GA4). FB pages / IG accounts / Echo are organic
                # sources, not ad accounts.
                ad_providers = {"google_ads", "ga4"}
                accounts_used = sum(len(c["ad_accounts"]) for c in connections if c["provider"] in ad_providers)
                return {"connections_used": connections_used, "accounts_used": accounts_used}

        connections = SubscriptionStateService.canonical_connections(username)
        # item 2 — count SOURCES (distinct assets), the same unit as
        # the org branch above AND the flattened list the cap partition
        # enforces against. Counting connection ROWS here made the Current
        # Plan badge read "9 of 6" while the gate blocked against 11 — the
        # badge and the cap are documented as sharing one source of truth
        # precisely so they cannot disagree.
        connections_used = sum(len(c["ad_accounts"]) for c in connections)
        # No provider filter here (unlike the org branch): the per-principal
        # canonical_connections shape already restricts ``ad_accounts`` to
        # ad-account-type assets, so summing all providers IS the
        # ad-account count. The org shape's ``ad_accounts`` holds every asset,
        # which is why THAT branch filters.
        accounts_used = sum(len(c["ad_accounts"]) for c in connections)
        return {"connections_used": connections_used, "accounts_used": accounts_used}

    @staticmethod
    def get_state(username: str) -> dict:
        """Return the subscription-state decision for ``username``.

        Shape::

            {
                "state": "trial" | "active" | "lapsed",
                "is_paid": bool,          # paid_tier flag
                "is_comped": bool,        # comped_active flag
                "comped_by": str,         # who set the comp ("" if none)
                "comped_at": str,         # ISO-8601 UTC ("" if none)
                "trial_started_at": str,  # ISO-8601 UTC ("" if not on trial path)
                "trial_ends_at": str,     # ISO-8601 UTC ("" if not on trial path)
                "trial_days_remaining": int,  # 0 when not trialing / expired
                "connection_cap": int,    # caps applicable to this state
                "account_cap_per_connection": int,
                "connections_used": int,
                "accounts_used": int,
                "determined": bool,       # True when derived from a real user
                                          # record; False for the fail-closed
                                          # default (missing user / ES outage)
            }

        Fail-closed on a missing user / ES outage: returns a ``lapsed``
        state with zero usage AND ``determined=False``. Lapsed is the safe
        DISPLAY default — it never grants an unearned active surface, and the
        page still renders (the user sees an upgrade prompt rather than a
        500). The ``determined`` flag lets a consumer that must NOT
        over-block (the import gate, ``is_lapsed``) distinguish a real
        lapsed verdict from "couldn't determine" — see ``is_lapsed``.
        """
        from asset_management.web.config import config

        def _lapsed_default() -> dict:
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
                "determined": False,
            }

        if not username:
            return _lapsed_default()

        es = get_elasticsearch_client()
        if es is None:
            return _lapsed_default()

        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return _lapsed_default()

        metadata = user_data.get("metadata") or {}
        is_paid = bool(metadata.get(PAID_TIER_KEY))
        is_comped = bool(metadata.get(COMPED_ACTIVE_KEY))
        comped_by = metadata.get(COMPED_BY_KEY) or ""
        comped_at = metadata.get(COMPED_AT_KEY) or ""

        # item 5b — purchased add-on connections extend the included
        # cap for a paid/active org. Coerce defensively (metadata is free-form);
        # negative / non-int → 0.
        try:
            addon_connections = max(0, int(metadata.get(ADDON_CONNECTIONS_KEY) or 0))
        except (TypeError, ValueError):
            addon_connections = 0
        # (2026-06-12) — add-ons marked for removal keep their paid
        # capacity until the stamped period end, then stop counting.
        _remove_at = SubscriptionStateService._parse_iso((metadata.get(ADDON_REMOVE_AT_KEY) or "").strip())
        if _remove_at is not None and SubscriptionStateService._now() >= _remove_at:
            # The scheduled change landed: drop to the pending target
            # (0 = full removal, N = reduction).
            try:
                addon_connections = max(0, min(addon_connections, int(metadata.get(ADDON_PENDING_QTY_KEY) or 0)))
            except (TypeError, ValueError):
                addon_connections = 0
        effective_cap = CONNECTION_CAP + addon_connections

        usage = SubscriptionStateService._usage(es, username)

        def _resolved(state: str) -> dict:
            """Build a non-trial result dict for a definitively-resolved
            state (active / past_due / canceled). All carry the active
            display caps — even canceled keeps the data visible; only NEW
            imports are gated (see ``is_import_blocked``). The connection cap
            is the included ``CONNECTION_CAP`` plus any purchased add-ons
            ."""
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
                "connections_used": usage["connections_used"],
                "accounts_used": usage["accounts_used"],
                "determined": True,
            }

        # A superuser never pays. Holding the
        # superuser role resolves to active above EVERYTHING — comp, Stripe
        # status, trial/lapse — and (since the import gate reads get_state)
        # bypasses the import block too. Roles come from the ES Security user
        # record we already fetched.
        roles = user_data.get("roles") or []
        if SUPERUSER_ROLE in roles:
            return _resolved(STATE_ACTIVE)

        # the ORG is the billing/comp entity. Resolve the member's
        # primary org and, IF that org has a billing record, return the ORG
        # verdict (with this member's org-scoped usage overlaid). This sits
        # ABOVE the per-team + workspace paths so a single comped org under a
        # shared workspace (kavanah-media holds 20 orgs) reads active-comped
        # without comping its siblings. Falls through (no org / no org record /
        # any error) to the workspace + per-user paths, so nothing changes for
        # an org until a billing record is created (comp grant or migration).
        # Superuser already short-circuited above; fail-open on any error.
        try:
            from asset_management.web.services.org_billing_service import OrgBillingService

            org_esid = SubscriptionStateService.resolve_primary_org_esid(username)
            if org_esid:
                org_state = OrgBillingService.get_org_state(org_esid)
                if org_state is not None:
                    org_state["connection_cap"] = effective_cap
                    org_state["connections_used"] = usage["connections_used"]
                    org_state["accounts_used"] = usage["accounts_used"]
                    return org_state
        except Exception as exc:  # noqa: BLE001 — fail-open onto the workspace/per-user path
            logging.warning("get_state org path failed for %r: %s", username, exc)

        # Option B (O2, flag-gated) — when ORG_BILLING_PER_TEAM is on,
        # billing is per-TEAM: a member's state derives from THEIR primary
        # team's subscription (one subscription per team under the org's Stripe
        # customer). Checked BEFORE the workspace overlay so per-team wins when
        # active. Falls through (flag off / no team subscription / any error)
        # to the workspace + per-user paths, so nothing changes until the flag
        # is flipped AND the O4 migration has populated team subscriptions.
        try:
            from asset_management.web.services.feature_flag_service import (
                FeatureFlagService,
                FLAG_ORG_BILLING_PER_TEAM,
            )

            if FeatureFlagService.is_enabled(FLAG_ORG_BILLING_PER_TEAM, username):
                from asset_management.web.services.org_billing_service import OrgBillingService

                team_esid = OrgBillingService.resolve_primary_team_esid(username)
                team_sub = OrgBillingService.get_team_subscription(team_esid) if team_esid else None
                team_status = (team_sub or {}).get("stripe_status") if team_sub else ""
                mapped_team = _STRIPE_STATUS_TO_STATE.get((team_status or "").strip().lower())
                if mapped_team:
                    return _resolved(mapped_team)
        except Exception as exc:  # noqa: BLE001 — fail-open onto the workspace/per-user path
            logging.warning("O2: get_state per-team path failed for %r: %s", username, exc)

        # workspace-billing Phase 2 — billing is a property of the
        # WORKSPACE (agency/company); every member inherits it. Resolve the
        # member's billing workspace and, IF that workspace has a billing
        # record, return the WORKSPACE verdict (with this member's own usage
        # counts overlaid). When the workspace has NO record yet (existing
        # workspaces before the Phase 4 migration) we fall through to the
        # per-user derivation below, so no one's access changes until real
        # data is migrated. Superuser already short-circuited above; fail-open:
        # any resolution error leaves us on the per-user path.
        try:
            from asset_management.web.services.workspace_billing_service import (
                WorkspaceBillingService,
            )

            ws_esid = WorkspaceBillingService.resolve_workspace_esid(username)
            if ws_esid and WorkspaceBillingService.has_billing_record(ws_esid):
                ws_state = WorkspaceBillingService.get_workspace_state(ws_esid)
                # Overlay the member's own usage (the per-principal rollup;
                # Phase 3 decides a workspace-wide rollup).
                ws_state["connections_used"] = usage["connections_used"]
                ws_state["accounts_used"] = usage["accounts_used"]
                return ws_state
        except Exception as exc:  # noqa: BLE001 — fail-open onto the per-user path
            logging.warning("workspace-billing: get_state workspace path failed for %r: %s", username, exc)

        # A superuser comp is the highest authority among non-superusers — it
        # grants active even over a lapsed/canceled Stripe status (an admin
        # explicitly comped this account).
        if is_comped:
            return _resolved(STATE_ACTIVE)

        # A REVOKED comp (admin granted then cleared complimentary access) is
        # checked BEFORE stripe_status — when an admin comps a paying user we
        # cancel their Stripe sub, so the webhook stamps stripe_status=canceled;
        # without this precedence the user would read as a generic "canceled"
        # sub instead of "you've lost complimentary access". Entitlement-
        # equivalent to lapsed (import-blocked); data stays visible at the
        # active display caps. The marker is cleared on re-comp (set_comp) /
        # re-subscribe (apply_stripe_status active), so this only fires while
        # they genuinely have neither comp nor active payment.
        #
        # EXCEPTION (2026-06-08): if the user still has a valid, unexpired free
        # trial, losing the comp falls back to the REMAINING trial rather than
        # comp_revoked — a comp briefly granted mid-trial shouldn't consume the
        # trial. set_comp preserves ``trial_started_at``, so we can tell. Only
        # scenario 3 (mid-trial) takes this path; a paid/canceled/expired-trial
        # user has no valid trial to return to and still shows comp_revoked.
        if metadata.get(COMP_REVOKED_AT_KEY):
            _revoked_now = SubscriptionStateService._now()
            _trial_start = SubscriptionStateService._parse_iso(metadata.get(TRIAL_STARTED_AT_KEY) or "")
            _trial_unexpired = _trial_start is not None and _revoked_now < (
                _trial_start + timedelta(days=config.FREE_TIER_TRIAL_DAYS)
            )
            # Only surface comp_revoked when there's NO valid trial to fall back
            # to. A still-unexpired trial falls through to the trial derivation
            # below (resumes the remaining days).
            if not _trial_unexpired:
                return {
                    "state": STATE_COMP_REVOKED,
                    "is_paid": False,
                    "is_comped": False,
                    "comped_by": "",
                    "comped_at": "",
                    "trial_started_at": "",
                    "trial_ends_at": "",
                    "trial_days_remaining": 0,
                    "connection_cap": CONNECTION_CAP,
                    "account_cap_per_connection": ACCOUNT_CAP_PER_CONNECTION,
                    "connections_used": usage["connections_used"],
                    "accounts_used": usage["accounts_used"],
                    "determined": True,
                }

        # Stripe scaffold — a present, recognized ``stripe_status``
        # is authoritative over the local trial/lapsed math (a real
        # subscription beats the trial-window guess). Inert until the webhook
        # writes the field. An unrecognized status falls through.
        stripe_status = (metadata.get(STRIPE_STATUS_KEY) or "").strip().lower()
        mapped = _STRIPE_STATUS_TO_STATE.get(stripe_status)
        if mapped is not None:
            return _resolved(mapped)

        # Paid (no live Stripe sub yet) resolves to active at for-profit caps —
        # plus any purchased add-on connections.
        if is_paid:
            return {
                "state": STATE_ACTIVE,
                "is_paid": is_paid,
                "is_comped": is_comped,
                "comped_by": comped_by,
                "comped_at": comped_at,
                "trial_started_at": "",
                "trial_ends_at": "",
                "trial_days_remaining": 0,
                "connection_cap": effective_cap,
                "account_cap_per_connection": ACCOUNT_CAP_PER_CONNECTION,
                "connections_used": usage["connections_used"],
                "accounts_used": usage["accounts_used"],
                "determined": True,
            }

        # Free tier — derive trial vs lapsed from the stamped start.
        # ``trial_started_at`` is normally stamped by SourceLimitService on
        # the user's first wizard step-4 hit. A user who reaches Billing
        # WITHOUT hitting step-4 has no stamp — so we lazily stamp it here on
        # first resolve. Without the stamp the
        # window was recomputed from "now" every load, so the countdown never
        # decremented. We write the SAME metadata key SourceLimitService uses,
        # so the trial is anchored consistently across both surfaces. The
        # stamp is best-effort: if the write fails we still render a window
        # from "now" (display-only) rather than 500 the page.
        now = SubscriptionStateService._now()
        started_at = SubscriptionStateService._parse_iso(metadata.get(TRIAL_STARTED_AT_KEY) or "")
        stored = metadata.get(TRIAL_STARTED_AT_KEY) or ""
        if started_at is None:
            started_at = now
            stored = now.isoformat()
            # Anchor the trial now so it counts down on every later load.
            stamped_metadata = dict(metadata)
            stamped_metadata[TRIAL_STARTED_AT_KEY] = stored
            if not SubscriptionStateService._put_metadata(es, username, user_data, stamped_metadata):
                # Write failed — fall back to a display-only window (matches
                # the pre-fix behaviour; no countdown, but the page renders).
                stored = ""

        ends_at = started_at + timedelta(days=config.FREE_TIER_TRIAL_DAYS)
        if now < ends_at:
            # Round up so "23h left" reads as 1 day remaining, not 0.
            total_secs = (ends_at - now).total_seconds()
            days_remaining = int(total_secs // 86400) + (1 if total_secs % 86400 > 0 else 0)
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
                "connections_used": usage["connections_used"],
                "accounts_used": usage["accounts_used"],
                "determined": True,
            }

        # Trial window elapsed, no payment, no comp -> lapsed. Lapsed
        # users keep the active display caps (their data stays visible);
        # only new imports are blocked, which the kickoff gate enforces.
        return {
            "state": STATE_LAPSED,
            "is_paid": False,
            "is_comped": False,
            "comped_by": "",
            "comped_at": "",
            "trial_started_at": stored,
            "trial_ends_at": ends_at.isoformat(),
            "trial_days_remaining": 0,
            "connection_cap": CONNECTION_CAP,
            "account_cap_per_connection": ACCOUNT_CAP_PER_CONNECTION,
            "connections_used": usage["connections_used"],
            "accounts_used": usage["accounts_used"],
            "determined": True,
        }

    @staticmethod
    def is_lapsed(username: str) -> bool:
        """Convenience for the import gate (Phase 68): True ONLY when the
        user is *definitively* lapsed — a real user record resolved to the
        lapsed state.

        This deliberately FAILS OPEN (returns False) when the state could
        not be determined (missing user / ES outage → the fail-closed
        ``determined=False`` default). For the import gate, fail-open is
        the right trade-off: blocking a legitimate active/trial user's
        import on a transient ES blip is worse UX than letting one import
        through (the data is theirs; the 07:05 cron would pull it anyway).
        The page DISPLAY, by contrast, reads the lapsed default directly —
        an upgrade prompt on an ES blip is harmless.
        """
        state = SubscriptionStateService.get_state(username)
        return state["state"] == STATE_LAPSED and state.get("determined", False)

    @staticmethod
    def is_import_blocked(username: str) -> bool:
        """The entitlement gate the importer uses (Phase 68 + 70 + 71): True
        ONLY when the user is *definitively* in an import-blocking state —
        ``lapsed`` or ``canceled``. ``past_due`` is a grace period and does
        NOT block (the page warns instead). ``trial`` / ``active`` import
        freely.

        Like ``is_lapsed``, this FAILS OPEN: an undetermined state (missing
        user / ES outage → ``determined=False``) returns False, so a
        transient blip never blocks a legitimate import — neither the
        on-demand kickoff nor the cron per-team sweep.
        """
        state = SubscriptionStateService.get_state(username)
        return state["state"] in _IMPORT_BLOCKED_STATES and state.get("determined", False)

    @staticmethod
    def _put_metadata(es, username: str, user_data: dict, metadata: dict) -> bool:
        """Read-modify-write the whole metadata dict back onto the user,
        preserving roles/full_name/email/enabled. Returns True on success.

        Mirrors ``SourceLimitService._stamp_trial_started_at`` — same
        put_user contract.
        """
        try:
            get_es_admin_client(es).security.put_user(
                username=username,
                roles=user_data.get("roles", []),
                full_name=user_data.get("full_name", ""),
                email=user_data.get("email", ""),
                enabled=user_data.get("enabled", True),
                metadata=metadata,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logging.warning("SubscriptionStateService: put_user failed for '%s': %s", username, e)
            return False

    @staticmethod
    def validate_comp_target(username: str) -> dict:
        """Validate a comp target user WITHOUT writing anything (
        workspace-billing Phase 3). The comp now acts on the user's WORKSPACE,
        but the same per-user guards still apply: the user must exist, and a
        superuser can't be comped (already has complimentary access). Returns
        ``{"ok": True}`` or ``{"ok": False, "error": NOT_FOUND_ERR |
        "cannot_comp_superuser" | ...}`` — the same sentinels the endpoint maps
        to friendly 404/400 messages."""
        if not username:
            return {"ok": False, "error": "missing username"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return {"ok": False, "error": err or "user not found"}
        if SUPERUSER_ROLE in (user_data.get("roles") or []):
            return {"ok": False, "error": "cannot_comp_superuser"}
        return {"ok": True}

    @staticmethod
    def set_comp(username: str, set_by: str) -> dict:
        """Mark ``username`` as active-via-comp (superuser grant).

        Writes ``comped_active=True`` + ``comped_by`` + ``comped_at`` onto
        the user's metadata, preserving every other key. Returns
        ``{"ok": bool, "error": str}``. Idempotent — re-comping refreshes
        ``comped_by``/``comped_at`` to the latest grant.

        Also blanks any stale ``stripe_status`` / ``paid_tier`` (
        follow-up 2026-06-05): a comped user no longer pays, and the CALLER
        cancels their live Stripe subscription, so leaving a ``canceled`` /
        ``past_due`` status on the record would resurface (wrongly) once the
        comp is later cleared. ``stripe_customer_id`` is preserved so the
        caller can still resolve + cancel the subscription, and so a future
        re-subscribe reuses the same Stripe customer.

        The caller (Phase 69 endpoint) is responsible for the superuser
        authorization check, the audit-log event, and CANCELING any live
        Stripe subscription (``cancel_stripe_for_user``) — this method only
        owns the metadata I/O.
        """
        if not username:
            return {"ok": False, "error": "missing username"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return {"ok": False, "error": err or "user not found"}

        # A superuser already has complimentary access — comping one is
        # meaningless. Reject server-side (defense beyond the search filter, so
        # a forged request can't comp a superuser). follow-up.
        if SUPERUSER_ROLE in (user_data.get("roles") or []):
            return {"ok": False, "error": "cannot_comp_superuser"}

        metadata = dict(user_data.get("metadata", {}))
        metadata[COMPED_ACTIVE_KEY] = True
        metadata[COMPED_BY_KEY] = set_by or ""
        metadata[COMPED_AT_KEY] = SubscriptionStateService._now().isoformat()
        # A comp supersedes any paid/Stripe state — clear it so it can't
        # resurface after the comp is cleared. (put_user merges, so write
        # falsy values rather than popping.)
        metadata[STRIPE_STATUS_KEY] = ""
        metadata[PAID_TIER_KEY] = False
        # A fresh comp is NOT a revoked comp — clear any prior revocation marker
        # so a re-granted user never shows "you've lost complimentary access".
        metadata[COMP_REVOKED_AT_KEY] = ""
        ok = SubscriptionStateService._put_metadata(es, username, user_data, metadata)
        return {"ok": ok, "error": "" if ok else "write failed"}

    @staticmethod
    def set_addon_connections(username: str, quantity: int) -> dict:
        """Persist the purchased add-on connection count for ``username``
        . Stamps ``addon_connections`` (clamped >= 0) onto the
        user's metadata, preserving every other key; ``get_state`` reads it to
        compute the effective cap (``CONNECTION_CAP + addon_connections``) for a
        paid/active org. The CALLER owns the Stripe ``set_addon_connections``
        call + audit log; this method only owns the metadata I/O. Returns
        ``{"ok": bool, "error": str}``."""
        if not username:
            return {"ok": False, "error": "missing username"}
        try:
            quantity = max(0, int(quantity))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid quantity"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return {"ok": False, "error": err or "user not found"}
        metadata = dict(user_data.get("metadata", {}))
        metadata[ADDON_CONNECTIONS_KEY] = quantity
        # A new purchase/adjust cancels any scheduled removal/reduction — the
        # user is actively keeping (some) add-ons, so the old mark must not
        # expire them.
        metadata[ADDON_REMOVE_AT_KEY] = ""
        metadata[ADDON_PENDING_QTY_KEY] = 0
        ok = SubscriptionStateService._put_metadata(es, username, user_data, metadata)
        return {"ok": ok, "error": "" if ok else "write failed"}

    @staticmethod
    def mark_addon_removal(username: str, remove_at: str, target_qty: int = 0) -> dict:
        """Mark the org's purchased add-on connections for a scheduled change
        at ``remove_at`` (ISO datetime — the already-paid period end):
        ``target_qty`` 0 = full removal, N = a reduction. Keeps
        ``addon_connections`` stamped so the paid capacity counts until then;
        ``get_state`` drops to the target once the stamp passes (,
        2026-06-12). The CALLER owns the Stripe item change (proration
        ``none`` — no credit, the period is paid) + the audit log."""
        if not username:
            return {"ok": False, "error": "missing username"}
        if SubscriptionStateService._parse_iso(remove_at) is None:
            return {"ok": False, "error": "invalid remove_at"}
        try:
            target_qty = max(0, int(target_qty))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid target_qty"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return {"ok": False, "error": err or "user not found"}
        metadata = dict(user_data.get("metadata", {}))
        metadata[ADDON_REMOVE_AT_KEY] = remove_at
        metadata[ADDON_PENDING_QTY_KEY] = target_qty
        ok = SubscriptionStateService._put_metadata(es, username, user_data, metadata)
        return {"ok": ok, "error": "" if ok else "write failed"}

    @staticmethod
    def get_addon_remove_at(username: str) -> str:
        """Return the pending add-on removal stamp (ISO string) for
        ``username`` — '' when none scheduled, it already passed, or on any
        lookup failure (fail-soft: the page simply renders no removal note).
        A PASSED stamp returns '' so a stale mark can't keep suppressing the
        Remove button after the capacity already expired."""
        if not username:
            return ""
        es = get_elasticsearch_client()
        if es is None:
            return ""
        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return ""
        raw = ((user_data.get("metadata") or {}).get(ADDON_REMOVE_AT_KEY) or "").strip()
        parsed = SubscriptionStateService._parse_iso(raw)
        if parsed is None or SubscriptionStateService._now() >= parsed:
            return ""
        return raw

    @staticmethod
    def get_addon_pending_qty(username: str) -> int:
        """The TARGET quantity of the pending scheduled change (0 = full
        removal) — only meaningful while ``get_addon_remove_at`` is non-empty.
        0 on any lookup failure (fail-soft)."""
        if not username:
            return 0
        es = get_elasticsearch_client()
        if es is None:
            return 0
        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return 0
        try:
            return max(0, int((user_data.get("metadata") or {}).get(ADDON_PENDING_QTY_KEY) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def clear_comp(username: str) -> dict:
        """Clear a comp grant — sets ``comped_active=False`` and blanks
        ``comped_by``/``comped_at``, preserving every other metadata key.

        IMPORTANT: ES Security ``put_user`` MERGES metadata — a key omitted
        from the dict is NOT deleted (confirmed on staging 2026-06-05: popping
        the keys reported success but left ``comped_active=True`` on re-read).
        So we must explicitly write a falsy value rather than delete the key.
        The read side (``get_state``/``list_comped_users``) treats
        ``comped_active`` truthily, so ``False`` correctly means "not comped".

        Returns ``{"ok": bool, "error": str}``. A no-op (no comp set) still
        returns ``ok=True``.
        """
        if not username:
            return {"ok": False, "error": "missing username"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return {"ok": False, "error": err or "user not found"}

        metadata = dict(user_data.get("metadata", {}))
        was_comped = bool(metadata.get(COMPED_ACTIVE_KEY))
        # Overwrite (not pop) — put_user merges, so set falsy values to clear.
        metadata[COMPED_ACTIVE_KEY] = False
        metadata[COMPED_BY_KEY] = ""
        metadata[COMPED_AT_KEY] = ""
        # Stamp that a comp was REVOKED so get_state can show "you've lost
        # complimentary access" instead of the generic lapsed/trial copy. Only
        # when there actually WAS a comp (clearing a non-comp is a no-op, not a
        # revocation). The marker is cleared again on re-comp / paid resubscribe
        # so it never lingers.
        if was_comped:
            metadata[COMP_REVOKED_AT_KEY] = SubscriptionStateService._now().isoformat()
        ok = SubscriptionStateService._put_metadata(es, username, user_data, metadata)
        return {"ok": ok, "error": "" if ok else "write failed", "was_comped": was_comped}

    @staticmethod
    def apply_stripe_status(username: str, stripe_status: str, customer_id: str = "") -> dict:
        """Apply a Stripe subscription status to ``username`` (
        follow-up — webhook + live create-subscription).

        Writes:
          * ``stripe_status`` — the raw Stripe ``Subscription.status`` (the
            Phase-71 derivation maps it to active/past_due/canceled);
          * ``paid_tier`` — synced True when the status is an *active* one
            (active/trialing), else False, so the existing is_paid readers
            stay consistent with the live subscription;
          * ``stripe_customer_id`` — stamped when ``customer_id`` is given
            (so the webhook can later map a customer back to this user).

        Read-modify-write preserves every other metadata key. Returns
        ``{"ok": bool, "error": str}``. The caller owns auth + audit.
        """
        if not username:
            return {"ok": False, "error": "missing username"}
        normalized = (stripe_status or "").strip().lower()
        if not normalized:
            return {"ok": False, "error": "missing stripe_status"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "elasticsearch unavailable"}
        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return {"ok": False, "error": err or "user not found"}

        metadata = dict(user_data.get("metadata", {}))
        metadata[STRIPE_STATUS_KEY] = normalized
        # Sync paid_tier to the live status so is_paid readers don't drift.
        is_active_status = normalized in ("active", "trialing")
        metadata[PAID_TIER_KEY] = is_active_status
        if customer_id:
            metadata[STRIPE_CUSTOMER_ID_KEY] = customer_id
        # Re-subscribing (a now-active Stripe status) clears any prior comp-
        # revocation marker — they're a paying customer again, not a user who
        # "lost complimentary access".
        if is_active_status:
            metadata[COMP_REVOKED_AT_KEY] = ""
        ok = SubscriptionStateService._put_metadata(es, username, user_data, metadata)
        return {"ok": ok, "error": "" if ok else "write failed"}

    @staticmethod
    def cancel_stripe_for_user(username: str) -> dict:
        """Cancel a user's Stripe subscription IMMEDIATELY (2026-06-05
        — called when the user is being deleted from Kavanah Analytics, so a
        removed account can't keep getting charged).

        Best-effort and NEVER raises: returns a small status dict the caller
        logs. ``mode``:
          * ``no_sub``   — no live Stripe subscription (comped / never paid /
                           not configured) → nothing to cancel.
          * ``canceled`` — the subscription was canceled immediately.
          * ``error``    — a Stripe error; the caller should log the orphaned
                           subscription for manual cleanup (deletion proceeds).

        MUST be called BEFORE the ES Security user is deleted — it reads the
        stamped ``stripe_customer_id`` off the user's metadata to find the sub.
        """
        from asset_management.web.config import config

        if not config.STRIPE_LIVE_PAYMENTS or not config.STRIPE_SECRET_KEY:
            return {"mode": "no_sub", "detail": "live payments off"}
        if not username:
            return {"mode": "no_sub", "detail": "no username"}

        es = get_elasticsearch_client()
        if es is None:
            return {"mode": "no_sub", "detail": "es unavailable"}
        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return {"mode": "no_sub", "detail": "user not found"}
        customer_id = (user_data.get("metadata") or {}).get(STRIPE_CUSTOMER_ID_KEY) or ""
        if not customer_id:
            return {"mode": "no_sub", "detail": "no stripe customer"}

        try:
            from asset_management.web.services.stripe_service import StripeService, StripeError

            summary = StripeService.get_subscription_summary(secret_key=config.STRIPE_SECRET_KEY, customer_id=customer_id)
            subscription_id = (summary or {}).get("subscription_id")
            status = (summary or {}).get("status")
            # Only an open subscription is worth canceling; an already-canceled
            # one is a no-op.
            if not subscription_id or status in ("canceled", "incomplete_expired"):
                return {"mode": "no_sub", "detail": f"status={status}"}
            StripeService.cancel_subscription(
                secret_key=config.STRIPE_SECRET_KEY,
                subscription_id=subscription_id,
                at_period_end=False,  # account is gone → stop billing now
            )
            return {"mode": "canceled", "subscription_id": subscription_id}
        except StripeError as exc:
            logging.warning(
                "cancel-on-delete: Stripe cancel FAILED for %r (customer=%s) — "
                "ORPHANED subscription, cancel manually in Stripe: %s",
                username,
                customer_id,
                exc,
            )
            return {"mode": "error", "detail": str(exc)[:200], "customer_id": customer_id}
        except Exception as exc:  # noqa: BLE001 — never block a user delete
            logging.warning("cancel-on-delete: unexpected error for %r: %s", username, exc)
            return {"mode": "error", "detail": str(exc)[:200], "customer_id": customer_id}

    @staticmethod
    def get_user_email(username: str) -> str:
        """Return the user's email from their ES Security record, or ""
        (follow-up — billing emails need a recipient). Best-effort:
        any error / missing user / missing email returns "".
        """
        if not username:
            return ""
        es = get_elasticsearch_client()
        if es is None:
            return ""
        user_data, err = SubscriptionStateService._get_user(es, username)
        if err or user_data is None:
            return ""
        return user_data.get("email") or ""

    @staticmethod
    def scan_and_notify_trial_ending(warn_within_days: int = 3, _dispatch_fn=None) -> dict:
        """Daily cron entry point: email every
        trialing user whose trial ends within ``warn_within_days`` days, once.

        A genuinely trialing user is one with NO comp / paid_tier / stripe
        status, a stamped ``trial_started_at``, and ``0 < days_remaining <=
        warn_within_days``. Each is emailed once — guarded by the
        ``trial_ending_email_sent_at`` metadata stamp so the daily scan never
        re-sends (the stamp is written only after a successful dispatch).

        Best-effort and never raises: returns a summary dict
        ``{"scanned", "notified", "skipped", "errors"}`` the cron logs.
        ``_dispatch_fn`` is a test seam (defaults to
        ``BillingEmailService.dispatch``).
        """
        from asset_management.web.config import config

        summary = {"scanned": 0, "notified": 0, "skipped": 0, "errors": 0}
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("trial-ending scan: ES unavailable")
            return summary

        if _dispatch_fn is None:
            try:
                from asset_management.web.services.billing_email_service import BillingEmailService

                _dispatch_fn = BillingEmailService.dispatch
            except Exception as exc:  # noqa: BLE001
                logging.warning("trial-ending scan: email service import failed: %s", exc)
                return summary

        try:
            response = dict(es.security.get_user())
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("trial-ending scan: get_user() failed: %s", exc)
            return summary

        now = SubscriptionStateService._now()
        trial_days = config.FREE_TIER_TRIAL_DAYS
        for uname, udata in response.items():
            if uname in SubscriptionStateService._BUILTIN_USERS:
                continue
            metadata = (udata or {}).get("metadata") or {}
            # Skip anyone who isn't a pure trialing user.
            if (
                metadata.get(COMPED_ACTIVE_KEY)
                or metadata.get(PAID_TIER_KEY)
                or (metadata.get(STRIPE_STATUS_KEY) or "").strip()
            ):
                continue
            started_at = SubscriptionStateService._parse_iso(metadata.get(TRIAL_STARTED_AT_KEY) or "")
            if started_at is None:
                continue  # never started a real trial (display-only window)
            summary["scanned"] += 1
            if metadata.get(TRIAL_ENDING_EMAIL_SENT_KEY):
                summary["skipped"] += 1
                continue
            ends_at = started_at + timedelta(days=trial_days)
            secs_left = (ends_at - now).total_seconds()
            if secs_left <= 0:
                continue  # already lapsed — that's the canceled/lapsed email's job
            days_remaining = int(secs_left // 86400) + (1 if secs_left % 86400 > 0 else 0)
            if days_remaining > warn_within_days:
                continue  # not in the warning window yet
            email = (udata or {}).get("email") or ""
            if not email:
                summary["skipped"] += 1
                continue
            try:
                _dispatch_fn("trial_ending", email, {"full_name": "", "days_remaining": days_remaining})
                # Stamp AFTER a successful dispatch so a failed send retries
                # tomorrow rather than silently swallowing the notification.
                new_meta = dict(metadata)
                new_meta[TRIAL_ENDING_EMAIL_SENT_KEY] = now.isoformat()
                SubscriptionStateService._put_metadata(es, uname, udata, new_meta)
                summary["notified"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad user must not stop the scan
                logging.warning("trial-ending scan: notify failed for %r: %s", uname, exc)
                summary["errors"] += 1
        logging.info("trial-ending scan complete: %s", summary)
        return summary

    @staticmethod
    def find_username_by_stripe_customer(customer_id: str) -> str | None:
        """Map a Stripe Customer id back to our ES Security username via the
        stamped ``metadata.stripe_customer_id``.

        Returns the username, or None if no user carries that customer id
        (or on an ES error — the webhook then 200-acks without a write so
        Stripe doesn't retry forever against an unknown customer).
        """
        if not customer_id:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            # ES Security has no query API for metadata; enumerate users and
            # match the stamped customer id. The paid-user population is
            # small (billing is workspace-owner scoped), so a full scan is
            # acceptable here and avoids a parallel index.
            response = es.security.get_user()
            for uname, udata in dict(response).items():
                md = (udata or {}).get("metadata") or {}
                if md.get(STRIPE_CUSTOMER_ID_KEY) == customer_id:
                    return uname
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning(
                "SubscriptionStateService.find_username_by_stripe_customer(%r): %s",
                customer_id,
                e,
            )
        return None

    # ES Security built-in service accounts that never log into DEAM — never
    # surface them in the comp search. Mirrors the filter in app.py grants_page.
    _BUILTIN_USERS = frozenset(
        {
            "elastic",
            "kibana_system",
            "logstash_system",
            "beats_system",
            "apm_system",
            "remote_monitoring_user",
        }
    )

    @staticmethod
    def search_users(query: str, limit: int = 10) -> list[dict]:
        """Search ES Security users by username OR email substring for the
        superuser comp autocomplete.

        Case-insensitive substring match against both the username and the
        ``email`` field. Excludes ES built-in service accounts AND superusers
        (a superuser already has complimentary access — comping one is
        meaningless, so they must not appear in the picker; follow-up
        2026-06-05). Returns up to ``limit`` ``{"username", "email",
        "full_name"}`` dicts sorted by username. Empty list on a too-short
        query (<2 chars), no match, or any ES error.
        """
        q = (query or "").strip().lower()
        if len(q) < 2:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            response = dict(es.security.get_user())
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning("SubscriptionStateService.search_users(%r): %s", query, e)
            return []

        matches = []
        for uname, udata in response.items():
            if uname in SubscriptionStateService._BUILTIN_USERS:
                continue
            # A superuser already has complimentary access — never offer them
            # as a comp target.
            if SUPERUSER_ROLE in ((udata or {}).get("roles") or []):
                continue
            email = (udata or {}).get("email") or ""
            if q in uname.lower() or q in email.lower():
                matches.append(
                    {
                        "username": uname,
                        "email": email,
                        "full_name": (udata or {}).get("full_name") or "",
                    }
                )
        matches.sort(key=lambda m: m["username"].lower())
        return matches[:limit]

    @staticmethod
    def list_comped_users() -> list[dict]:
        """Return every user currently comped (``comped_active=True``) for the
        superuser comp roster.

        Enumerates ES Security users (same scan as ``search_users``) and
        returns ``{"username", "email", "comped_by", "comped_at"}`` for each
        comped account, sorted by username. Excludes built-in accounts.
        Empty list on any ES error (the roster just shows nothing).
        """
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            response = dict(es.security.get_user())
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning("SubscriptionStateService.list_comped_users: %s", e)
            return []

        out = []
        for uname, udata in response.items():
            if uname in SubscriptionStateService._BUILTIN_USERS:
                continue
            md = (udata or {}).get("metadata") or {}
            if md.get(COMPED_ACTIVE_KEY):
                out.append(
                    {
                        "username": uname,
                        "email": (udata or {}).get("email") or "",
                        "comped_by": md.get(COMPED_BY_KEY) or "",
                        "comped_at": md.get(COMPED_AT_KEY) or "",
                    }
                )
        out.sort(key=lambda m: m["username"].lower())
        return out
