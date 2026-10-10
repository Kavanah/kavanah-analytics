"""
Connection Service

Promotes ``connection`` and ``connection_asset`` to first-class concepts.
Backs three new ES indices that unify the OAuth-token storage scattered
across ``de_asset_fb_app_users.fb_app_user_token`` (Facebook) and
``de_asset_youtube_accounts.youtube_account_token`` (YouTube) today.

* ``de_connection`` — one OAuth credential per (principal, provider).
  ``oauth_token`` is encrypted at rest via ``lib.encryption.encrypt``
  using the same Fernet key as the asset-side token storage. Doc id is
  ES-generated (random) — the (principal, provider, provider_account_id)
  triple is the natural key and is enforced at the service layer.
* ``de_connection_asset`` — ad account / page / property / channel under
  a connection. FK = ``connection_esid``.
* ``de_workspace_connection_asset`` — link table for workspace-scoping
  (workspace-id → connection_asset_esid). **Stub in PR-A.** Mapping
  exists, no writers. Wizard's workspace-assignment step (,
  blocked on permissions refactor Phase 1) is the first writer.

Storage decision and migration plan are recorded in
``the private docs`` (entry: ``2026-05-11 — ``).
"""

import logging
import secrets
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/python")
from lib.encryption import decrypt, encrypt
from lib.es_helpers import (
    get_elasticsearch_client,
    put_regular_index_template,
    create_index_if_not_exists,
)

# ─────────────────────────────────────────────────────────────────────────────
# Index names
# ─────────────────────────────────────────────────────────────────────────────

INDEX_CONNECTION = "de_connection"
INDEX_CONNECTION_ASSET = "de_connection_asset"
INDEX_WORKSPACE_CONNECTION_ASSET = "de_workspace_connection_asset"

# ─────────────────────────────────────────────────────────────────────────────
# Provider enum
# ─────────────────────────────────────────────────────────────────────────────

PROVIDER_FACEBOOK = "facebook"
PROVIDER_INSTAGRAM = "instagram"
PROVIDER_YOUTUBE = "youtube"
PROVIDER_GOOGLE_ADS = "google_ads"
PROVIDER_GA4 = "ga4"
# Echo (Kavanah-internal messaging connector). Uses a
# cluster-wide service token rather than per-tenant OAuth, so
# connections created with this provider land status=active with
# oauth_token_enc empty (via create_tokenless_connection below) and
# the importer derives the team_esid from the connection's principal
# rather than from a stored token.
PROVIDER_ECHO = "echo"
# Disciple.Tools (WordPress CRM) pull connector. Unlike OAuth
# providers, the secret is a DT Site-to-Site Link token (HTTP-Basic-style
# rotating bearer, computed by import_scripts/disciple_tools/client.py). It is
# stored encrypted in oauth_token_enc like any other secret; the two domains the
# token-hash needs (dt_domain, our_domain) live on the dt_site connection_asset's
# metadata, and that asset's provider_asset_id is the team_esid the importer
# writes to ({team_esid}__disciple_tools). One connection per DT site.
PROVIDER_DISCIPLE_TOOLS = "disciple_tools"
# Phase 3 — Bitly link-clicks connector. Non-OAuth: the secret is a
# Bitly v4 API access token entered in the Data Connectors wizard, stored
# encrypted in oauth_token_enc like every other secret (DT pattern). One
# connection per team: provider_account_id is the team_esid, and the paired
# ``bitly_account`` connection_asset carries the tag filter the importer scopes
# links by (metadata.tags). Deliberately NOT in _OAUTH_REQUIRED_PROVIDERS —
# there is no OAuth roundtrip; the token lands at create time via
# create_bitly_connection, so a tokenless bitly row never legitimately exists.
PROVIDER_BITLY = "bitly"
# TikTok organic (Login Kit + Display API). OAuth: the wizard mints a
# ``wizard-pending-*`` row, the fb_oauth-host callback swaps it for the account's
# ``open_id`` via set_token_for_pending_connection. oauth_token_enc holds the
# REFRESH token (365 days; access tokens last 24h and are minted per import run),
# and token_expires_at is when that refresh token expires — i.e. when the account
# must re-authorize.
PROVIDER_TIKTOK = "tiktok"

_VALID_PROVIDERS = (
    PROVIDER_FACEBOOK,
    PROVIDER_INSTAGRAM,
    PROVIDER_YOUTUBE,
    PROVIDER_GOOGLE_ADS,
    PROVIDER_GA4,
    PROVIDER_ECHO,
    PROVIDER_DISCIPLE_TOOLS,
    PROVIDER_BITLY,
    PROVIDER_TIKTOK,
)


def _canonical_provider_account_id(provider: str, provider_account_id: str) -> str:
    """one canonical spelling per provider account id at WRITE time.

    Google Ads customer ids legitimately arrive both dashed ("646-706-0525")
    and bare ("6467060525"); readers normalize (dash-strip) on both sides, so
    writing them verbatim let the same customer produce two connection rows
    that BOTH matched every lookup — non-deterministic credential selection
    (the principal-blind first-match warning firing on every resolve).
    Canonicalize before the natural-key check and before the doc is written.
    Other providers pass through untouched (their ids have one spelling).
    """
    value = (provider_account_id or "").strip()
    if provider == PROVIDER_GOOGLE_ADS:
        return value.replace("-", "")
    return value


def _provider_account_id_spellings(provider: str, provider_account_id: str) -> list[str]:
    """every stored spelling a given id may have.

    New rows are canonical; LEGACY google_ads rows may hold the dashed
    customer-id form, which is deterministic (3-3-4 over the 10-digit id) —
    so both spellings can be matched exactly ES-side, keeping the natural-key
    finder a pure term query (no _source inspection)."""
    raw = (provider_account_id or "").strip()
    spellings = {raw} if raw else set()
    if provider == PROVIDER_GOOGLE_ADS:
        bare = raw.replace("-", "")
        spellings.add(bare)
        if len(bare) == 10 and bare.isdigit():
            spellings.add(f"{bare[0:3]}-{bare[3:6]}-{bare[6:]}")
    return sorted(spellings)


# providers that REQUIRE a per-tenant OAuth token to import and have
# NO service-account fallback. For these, a tokenless connection is un-importable,
# so create_tokenless_connection must land it ``pending_token`` (needs
# authorization), NOT ``active``. Google Ads (SA retired) and GA4 (SA
# retired) are the two. Echo / Disciple.Tools are deliberately ABSENT:
# Echo imports from a cluster-wide service token, and DT stores its own
# Site-Link token via create_disciple_tools_site — both are legitimately active
# with an empty/`oauth_token_enc`-managed-elsewhere row. adds TikTok:
# a TikTok row without a token can never import.
_OAUTH_REQUIRED_PROVIDERS = frozenset({PROVIDER_GOOGLE_ADS, PROVIDER_GA4, PROVIDER_TIKTOK})

# ─────────────────────────────────────────────────────────────────────────────
# Asset-type enum (the kind of resource a connection_asset represents)
# ─────────────────────────────────────────────────────────────────────────────

ASSET_TYPE_FB_AD_ACCOUNT = "fb_ad_account"
ASSET_TYPE_FB_PAGE = "fb_page"
ASSET_TYPE_IG_ACCOUNT = "ig_account"
ASSET_TYPE_YOUTUBE_CHANNEL = "youtube_channel"
ASSET_TYPE_GOOGLE_ADS_CUSTOMER = "google_ads_customer"
ASSET_TYPE_GA4_PROPERTY = "ga4_property"
# Echo is team-scoped (no provider-side asset hierarchy);
# the one connection_asset row written per Echo connection carries
# provider_asset_id=<primary_team_esid>. The importer reads
# connection.principal → _resolve_primary_team_esid, but the asset row
# is still written so the Sources page lists the connection with the
# user's team as its scope.
ASSET_TYPE_ECHO_TEAM = "echo_team"
# one row per DT site under its connection. Like echo_team, DT is
# team-scoped (no provider-side asset hierarchy): provider_asset_id is the
# team_esid the importer writes to, and ``metadata`` carries the
# {dt_domain, our_domain} the site-link bearer hash needs.
ASSET_TYPE_DT_SITE = "dt_site"
# Phase 3 — one row per team's Bitly connection (dt_site pattern):
# provider_asset_id is the team_esid the importer writes to, and ``metadata``
# carries {"tags": [...]} — the Bitly tag names whose links belong to this
# team. The importer enumerates by this asset_type (list_connection_assets_by_type)
# and joins to the parent connection for the encrypted API token.
ASSET_TYPE_BITLY_ACCOUNT = "bitly_account"
# the one TikTok account a connection authorizes (a Login Kit grant
# is per account). provider_asset_id is the account's open_id.
ASSET_TYPE_TIKTOK_ACCOUNT = "tiktok_account"

_VALID_ASSET_TYPES = (
    ASSET_TYPE_FB_AD_ACCOUNT,
    ASSET_TYPE_FB_PAGE,
    ASSET_TYPE_IG_ACCOUNT,
    ASSET_TYPE_YOUTUBE_CHANNEL,
    ASSET_TYPE_GOOGLE_ADS_CUSTOMER,
    ASSET_TYPE_GA4_PROPERTY,
    ASSET_TYPE_ECHO_TEAM,
    ASSET_TYPE_DT_SITE,
    ASSET_TYPE_BITLY_ACCOUNT,
    ASSET_TYPE_TIKTOK_ACCOUNT,
)

# ─────────────────────────────────────────────────────────────────────────────
# Connection status
# ─────────────────────────────────────────────────────────────────────────────

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
# a wizard-created connection that has selection + scope
# metadata (asset rows, org_shared flag) but no encrypted token yet
# because the wizard's OAuth handoff still discards the token (open
# follow-up — fb_oauth_server.py is not wizard_session-aware). The
# importer code path skips these; the Sources page can flag them as
# "complete the connection".
STATUS_PENDING_TOKEN = "pending_token"
# a wizard placeholder whose OAuth completion landed on an account
# the principal ALREADY had a connection for. The token was stamped on that
# existing (canonical) row instead of minting a duplicate; this row becomes a
# short-lived tombstone carrying ``superseded_by = <canonical esid>`` so the
# wizard's poll-by-esid loop can redirect the frontend to the canonical row.
# Nothing else references it; the abandoned-placeholder sweep deletes it.
STATUS_SUPERSEDED = "superseded"

# provider_account_id prefix the wizard stamps on a pending_token
# row BEFORE the real Meta /me user id is known. A row still carrying this
# prefix never completed OAuth, so the wizard reuses it on a re-authorize
# rather than minting a fresh placeholder (which used to leave an orphan row
# behind on every Authorize click — connection-count incident,
# 2026-06-05). The billing connection-count logic
# (SubscriptionStateService) imports this same constant so the two layers
# agree on what a placeholder is.
PENDING_ACCOUNT_ID_PREFIX = "wizard-pending-"

_VALID_STATUSES = (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_REVOKED,
    STATUS_PENDING_TOKEN,
    STATUS_SUPERSEDED,
)

# ─────────────────────────────────────────────────────────────────────────────
# Mappings
# ─────────────────────────────────────────────────────────────────────────────

CONNECTION_MAPPING = {
    "properties": {
        # Owner of the OAuth grant. In practice the user id who authorized;
        # for invited-org-owner flows this may be the org admin. Keyed for
        # exact-match lookups in the natural-key check.
        "principal": {"type": "keyword"},
        # Provider enum; see _VALID_PROVIDERS.
        "provider": {"type": "keyword"},
        # Provider-side account id (FB app_user id, Google account email,
        # etc.). Disambiguates multiple connections to the same provider
        # under the same principal.
        "provider_account_id": {"type": "keyword"},
        # Human-readable label surfaced in the wizard / connector grid.
        "label": {"type": "text"},
        # Fernet-encrypted access/refresh token. Stored as a single opaque
        # string — providers vary in shape (FB long-lived access token,
        # YouTube refresh_token), but the wire format is always the
        # ciphertext produced by lib.encryption.encrypt.
        "oauth_token_enc": {"type": "keyword", "index": False},
        # Optional: when the encrypted token expires, if known. FB
        # long-lived tokens last ~60 days; YouTube refresh tokens are
        # non-expiring → null.
        "token_expires_at": {"type": "date"},
        # when Meta's 90-day DATA ACCESS window on this token closes
        # (debug_token ``data_access_expires_at``), ISO-8601. Distinct from
        # token_expires_at: a long-lived token stays valid while its data access
        # lapses, and only a fresh user re-authorisation (NOT the 02:30
        # fb_exchange_token refresh) resets this clock. Null for system-user
        # tokens (no window) and for rows written until
        # tools/backfill_data_access_expiry.py runs.
        "data_access_expires_at": {"type": "date"},
        # staged alongside pending_reauth_expires_at
        # so a partial-coverage re-auth carries its data-access clock to commit.
        "pending_reauth_data_access_expires_at": {"type": "date"},
        # See _VALID_STATUSES. Importers can flip to 'expired' / 'revoked'
        # when an API call returns an OAuth error, surfacing the bad
        # connection in the wizard / connector grid.
        "status": {"type": "keyword"},
        # Scope list returned by the OAuth grant (e.g.
        # ["ads_read", "pages_show_list"]). Kept for diagnostics — a
        # connection that's missing a scope explains "import says 403".
        "scopes": {"type": "keyword"},
        # agency org-shared vs workspace-scoped flag.
        # True  → connection is visible to every workspace under the
        #          principal's agency org (agency owners default here).
        # False → connection is scoped to the principal's own workspace
        #          (the default for individual / org_owner / invited
        #          flows, and the agency owner's explicit opt-out).
        # Existing rows that predate are read as False because
        # the field is null/missing → workspace-scoped (the safer
        # default for the cutover).
        "org_shared": {"type": "boolean"},
        # Optional pointer back to the legacy asset doc this connection
        # was migrated from (de_asset_fb_app_users:<esid> or
        # de_asset_youtube_accounts:<esid>). Lets the importer fallback
        # path correlate new+old representations during the cutover
        # window (PR-C). Null for connections created via the wizard.
        "legacy_asset_index": {"type": "keyword"},
        "legacy_asset_esid": {"type": "keyword"},
        # transient OAuth-state handoff fields for the Google Ads
        # per-user OAuth flow. The wizard (app host) seeds these on a pending
        # connection; the OAuth callback (fb_oauth host — a SEPARATE Flask app
        # with no shared session) looks the connection up by oauth_state, then
        # clears them when the token lands. NOT durable credential state: they
        # exist only for the cross-host authorize→callback roundtrip. Modeled
        # on de_asset_ga4_accounts.{ga4_account_auth_url, ga4_account_auth_state}
        # but carried on de_connection so the token storage decision holds.
        "oauth_state": {"type": "keyword"},
        "oauth_auth_url": {"type": "keyword"},
        "oauth_state_expires_at": {"type": "date"},
        # on a ``superseded`` tombstone, the esid of the canonical
        # row the OAuth completion merged into. Read back from _source by the
        # wizard's oauth-status poll (never term-queried), so pre-existing
        # indices that dynamic-map it are fine.
        "superseded_by": {"type": "keyword"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    }
}

CONNECTION_ASSET_MAPPING = {
    "properties": {
        # FK → de_connection._id. Keyed for the assets-by-connection
        # lookup the wizard's confirm-step needs.
        "connection_esid": {"type": "keyword"},
        # follow-up — denormalised copy of the parent
        # de_connection.principal. Needed because the preview-
        # sweep config keys this index by ``principal`` for the
        # username-keyed delete_by_query; without the field stamped at
        # write-time, the sweep is silently a no-op against asset rows.
        # Sweep coverage is the load-bearing reason for the
        # denormalisation; the rest of the codebase reads the parent
        # connection for principal-keyed lookups, not the asset row.
        "principal": {"type": "keyword"},
        # See _VALID_ASSET_TYPES.
        "asset_type": {"type": "keyword"},
        # Provider-side asset id: FB ad_account_id ("act_..."), FB page
        # id, YouTube channel id ("UC..."), GA4 property id, etc. Caller
        # supplies — this index does not invent IDs.
        "provider_asset_id": {"type": "keyword"},
        # Human-readable name surfaced in the wizard's asset picker.
        "label": {"type": "text"},
        # Optional provider-side metadata (currency, timezone, etc.).
        # enabled=false because the shape varies wildly per asset_type and
        # we never query into it — only read the whole blob back.
        "metadata": {"type": "object", "enabled": False},
        # Pointer back to the existing de_asset_* doc this connection_asset
        # was migrated from (e.g. de_asset_fb_ad_accounts:<esid>). Same
        # purpose as on de_connection — supports importer cutover.
        "legacy_asset_index": {"type": "keyword"},
        "legacy_asset_esid": {"type": "keyword"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    }
}

# Link table — PR-A ships the mapping only, no writers. (wizard
# data-model wiring, blocked on permissions refactor) is the first writer.
# Schema is intentionally minimal: one row per (workspace, connection_asset).
# A connection_asset can move workspaces over time — that's a delete + insert,
# logged via assigned_at/assigned_by for audit.
WORKSPACE_CONNECTION_ASSET_MAPPING = {
    "properties": {
        "workspace_id": {"type": "keyword"},
        "connection_asset_esid": {"type": "keyword"},
        "assigned_at": {"type": "date"},
        "assigned_by": {"type": "keyword"},
    }
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectionService:
    """Service for managing connections, connection_assets, and the
    (stub) workspace-link index.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # Index bootstrap
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def ensure_indices_exist() -> None:
        """Create all three connection indices if they do not exist.

        Mirrors the WizardSessionService / FeatureFlagService pattern:
        ``put_regular_index_template`` → ``create_index_if_not_exists`` →
        ``put_mapping`` to update an existing index in place if the
        mapping has been extended.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("ConnectionService: could not connect to ES")
            return
        for index_name, mapping in (
            (INDEX_CONNECTION, CONNECTION_MAPPING),
            (INDEX_CONNECTION_ASSET, CONNECTION_ASSET_MAPPING),
            (INDEX_WORKSPACE_CONNECTION_ASSET, WORKSPACE_CONNECTION_ASSET_MAPPING),
        ):
            put_regular_index_template(es, index_name, mapping, priority=500)
            create_index_if_not_exists(es, index_name, mapping)
            try:
                es.indices.put_mapping(index=index_name, body=mapping)
            except Exception as e:
                logging.warning(
                    "ConnectionService: put_mapping update on %s: %s",
                    index_name,
                    e,
                )

    # ─────────────────────────────────────────────────────────────────────────
    # Connection — create / read / update
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def create_connection(
        *,
        principal: str,
        provider: str,
        provider_account_id: str,
        oauth_token: str,
        label: str = "",
        token_expires_at: str | None = None,
        data_access_expires_at: str | None = None,
        scopes: list[str] | None = None,
        org_shared: bool = False,
        legacy_asset_index: str | None = None,
        legacy_asset_esid: str | None = None,
    ) -> dict:
        """Create a new connection. Encrypts ``oauth_token`` at rest.

        Returns ``{"success": True, "esid": "...", "doc": {...}}`` on
        success or ``{"success": False, "error": "..."}``.

        The (principal, provider, provider_account_id) triple is the
        natural key — a duplicate raises an error rather than silently
        overwriting an existing connection's token. Callers that want
        idempotent upsert semantics (the backfill PR-B) call
        ``upsert_connection`` instead.
        """
        if not principal:
            return {"success": False, "error": "principal is required"}
        if provider not in _VALID_PROVIDERS:
            return {
                "success": False,
                "error": f"provider must be one of {_VALID_PROVIDERS}; got {provider!r}",
            }
        if not provider_account_id:
            return {"success": False, "error": "provider_account_id is required"}
        # canonical spelling before the natural-key check and the doc.
        provider_account_id = _canonical_provider_account_id(provider, provider_account_id)
        if not oauth_token:
            return {"success": False, "error": "oauth_token is required"}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        existing = ConnectionService._find_by_natural_key(es, principal, provider, provider_account_id)
        if existing:
            return {
                "success": False,
                "error": (
                    f"Connection already exists for principal={principal!r} "
                    f"provider={provider!r} provider_account_id={provider_account_id!r}; "
                    f"use upsert_connection to refresh the token"
                ),
            }

        now = _now_iso()
        doc = {
            "principal": principal,
            "provider": provider,
            "provider_account_id": provider_account_id,
            "label": label or "",
            "oauth_token_enc": encrypt(oauth_token),
            "token_expires_at": token_expires_at,
            "data_access_expires_at": data_access_expires_at,
            "status": STATUS_ACTIVE,
            "scopes": list(scopes or []),
            "org_shared": bool(org_shared),
            "legacy_asset_index": legacy_asset_index,
            "legacy_asset_esid": legacy_asset_esid,
            "created_at": now,
            "updated_at": now,
        }
        try:
            result = es.index(index=INDEX_CONNECTION, body=doc, refresh="wait_for")
        except Exception as e:
            logging.error("ConnectionService.create_connection error: %s", e)
            return {"success": False, "error": str(e)}
        return {"success": True, "esid": result["_id"], "doc": doc}

    @staticmethod
    def create_pending_connection(
        *,
        principal: str,
        provider: str,
        provider_account_id: str,
        label: str = "",
        scopes: list[str] | None = None,
        org_shared: bool = False,
    ) -> dict:
        """Create a ``pending_token`` connection.

        Wizard step-6 finish calls this to persist the user's selection
        + org_shared decision before the wizard-aware OAuth handoff
        (fb_oauth_server.py rework — open follow-up) can stamp the
        encrypted token. The doc carries every field a real connection
        would except ``oauth_token_enc``, which stays empty until the
        token-handoff ticket lands. Once that ships, the finishing
        step calls ``set_token`` (added in the same ticket) to flip
        status active.

        Returns ``{"success": True, "esid": ..., "doc": ...}`` on
        success, or ``{"success": False, "error": "..."}``.

        Natural key (principal, provider, provider_account_id) is
        upsert semantics: a re-finish reuses the existing row rather
        than creating a duplicate.
        """
        if not principal:
            return {"success": False, "error": "principal is required"}
        if provider not in _VALID_PROVIDERS:
            return {
                "success": False,
                "error": f"provider must be one of {_VALID_PROVIDERS}; got {provider!r}",
            }
        if not provider_account_id:
            return {"success": False, "error": "provider_account_id is required"}
        # canonical spelling before the natural-key check and the doc.
        provider_account_id = _canonical_provider_account_id(provider, provider_account_id)

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        existing = ConnectionService._find_by_natural_key(es, principal, provider, provider_account_id)
        now = _now_iso()
        # Preserve created_at + a prior token (if a re-finish lands
        # after the token-handoff has already stamped one) so this
        # call is safely re-runnable from the wizard.
        prior_source = (existing or {}).get("_source") or {}
        prior_token_enc = prior_source.get("oauth_token_enc") or ""
        prior_status = prior_source.get("status")
        resolved_status = prior_status if (prior_token_enc and prior_status == STATUS_ACTIVE) else STATUS_PENDING_TOKEN
        doc = {
            "principal": principal,
            "provider": provider,
            "provider_account_id": provider_account_id,
            "label": label or "",
            "oauth_token_enc": prior_token_enc,
            "token_expires_at": prior_source.get("token_expires_at"),
            "data_access_expires_at": prior_source.get("data_access_expires_at"),
            "status": resolved_status,
            "scopes": list(scopes or []),
            "org_shared": bool(org_shared),
            "legacy_asset_index": prior_source.get("legacy_asset_index"),
            "legacy_asset_esid": prior_source.get("legacy_asset_esid"),
            "created_at": prior_source.get("created_at") or now,
            "updated_at": now,
        }
        try:
            if existing:
                es.index(
                    index=INDEX_CONNECTION,
                    id=existing["_id"],
                    body=doc,
                    refresh="wait_for",
                )
                return {"success": True, "esid": existing["_id"], "doc": doc, "created": False}
            result = es.index(index=INDEX_CONNECTION, body=doc, refresh="wait_for")
            return {"success": True, "esid": result["_id"], "doc": doc, "created": True}
        except Exception as e:
            logging.error("ConnectionService.create_pending_connection error: %s", e)
            return {"success": False, "error": str(e)}

    @staticmethod
    def create_tokenless_connection(
        *,
        principal: str,
        provider: str,
        provider_account_id: str,
        label: str = "",
        org_shared: bool = False,
    ) -> dict:
        """Create-or-refresh a connection for a cluster-managed
        provider that has no per-tenant OAuth token.

        Unlike ``create_pending_connection``, this lands the row at
        ``status=active`` with an empty ``oauth_token_enc`` because the
        importer for these providers reads a service token from
        deployment config, not from the connection doc. The Sources
        page therefore does NOT render a "Complete connection" CTA on
        these rows — there is no token to complete.

        Natural key (principal, provider, provider_account_id) is upsert
        semantics. ``provider_account_id`` is conventionally the
        principal's primary team_esid for Echo (the only "asset" the
        importer scopes against), but the helper itself is provider-
        agnostic — the caller supplies whatever stable id makes
        (principal, provider, provider_account_id) unique.

        Returns ``{"success": True, "esid": ..., "doc": ..., "created": bool}``
        on success or ``{"success": False, "error": "..."}``.
        """
        if not principal:
            return {"success": False, "error": "principal is required"}
        if provider not in _VALID_PROVIDERS:
            return {
                "success": False,
                "error": f"provider must be one of {_VALID_PROVIDERS}; got {provider!r}",
            }
        if not provider_account_id:
            return {"success": False, "error": "provider_account_id is required"}
        # canonical spelling before the natural-key check and the doc.
        provider_account_id = _canonical_provider_account_id(provider, provider_account_id)

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        existing = ConnectionService._find_by_natural_key(es, principal, provider, provider_account_id)
        now = _now_iso()
        prior_source = (existing or {}).get("_source") or {}
        # PRESERVE a prior OAuth token. This helper exists for
        # cluster-managed tokenless providers (Echo), but the Google Ads OAuth
        # wizard finalize routes through the same tokenless apply path AFTER the
        # OAuth callback has already stamped a real refresh token on this
        # natural-key row. Blanking oauth_token_enc here wiped that token,
        # leaving the connection active+tokenless — the importer then fell back
        # to the service account and failed with USER_PERMISSION_DENIED
        # (confirmed on staging, connection hi6xrZ...). Mirror
        # create_pending_connection's prior-token preservation: keep an existing
        # token (and its expiry/scopes) when one is present; only land a truly
        # empty token for a genuinely tokenless (new, or already-empty) row.
        prior_token_enc = prior_source.get("oauth_token_enc") or ""
        # an OAuth-required provider (Google Ads / GA4) with NO token
        # is un-importable (no SA fallback). Land it
        # ``pending_token`` so the UI shows "needs authorization" instead of a
        # green "active" the importer can't use. With a prior token present (the
        # OAuth-first path), keep it active. Echo / DT (not in
        # _OAUTH_REQUIRED_PROVIDERS) stay active tokenless as before.
        if provider in _OAUTH_REQUIRED_PROVIDERS and not prior_token_enc:
            resolved_status = STATUS_PENDING_TOKEN
        else:
            resolved_status = STATUS_ACTIVE
        doc = {
            "principal": principal,
            "provider": provider,
            "provider_account_id": provider_account_id,
            "label": label or "",
            "oauth_token_enc": prior_token_enc,
            "token_expires_at": prior_source.get("token_expires_at") if prior_token_enc else None,
            "data_access_expires_at": prior_source.get("data_access_expires_at") if prior_token_enc else None,
            "status": resolved_status,
            "scopes": prior_source.get("scopes") or [] if prior_token_enc else [],
            "org_shared": bool(org_shared),
            "legacy_asset_index": prior_source.get("legacy_asset_index"),
            "legacy_asset_esid": prior_source.get("legacy_asset_esid"),
            "created_at": prior_source.get("created_at") or now,
            "updated_at": now,
        }
        try:
            if existing:
                es.index(
                    index=INDEX_CONNECTION,
                    id=existing["_id"],
                    body=doc,
                    refresh="wait_for",
                )
                return {"success": True, "esid": existing["_id"], "doc": doc, "created": False}
            result = es.index(index=INDEX_CONNECTION, body=doc, refresh="wait_for")
            return {"success": True, "esid": result["_id"], "doc": doc, "created": True}
        except Exception as e:
            logging.error("ConnectionService.create_tokenless_connection error: %s", e)
            return {"success": False, "error": str(e)}

    @staticmethod
    def upsert_connection(
        *,
        principal: str,
        provider: str,
        provider_account_id: str,
        oauth_token: str,
        label: str = "",
        token_expires_at: str | None = None,
        data_access_expires_at: str | None = None,
        scopes: list[str] | None = None,
        org_shared: bool = False,
        legacy_asset_index: str | None = None,
        legacy_asset_esid: str | None = None,
    ) -> dict:
        """Create-or-refresh a connection. If a doc exists for the
        natural key, refresh the encrypted token + scopes + label and
        flip status back to active. ``created_at`` is preserved.

        Used by the backfill to be safely re-runnable.
        """
        if not principal:
            return {"success": False, "error": "principal is required"}
        if provider not in _VALID_PROVIDERS:
            return {
                "success": False,
                "error": f"provider must be one of {_VALID_PROVIDERS}; got {provider!r}",
            }
        if not provider_account_id:
            return {"success": False, "error": "provider_account_id is required"}
        # canonical spelling before the natural-key check and the doc.
        provider_account_id = _canonical_provider_account_id(provider, provider_account_id)
        if not oauth_token:
            return {"success": False, "error": "oauth_token is required"}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        existing = ConnectionService._find_by_natural_key(es, principal, provider, provider_account_id)
        now = _now_iso()
        # preserve org_shared on token refresh. A re-OAuth
        # round-trip shouldn't silently reset the user's earlier
        # sharing decision back to the caller-supplied default.
        prior_org_shared = (existing or {}).get("_source", {}).get("org_shared")
        if isinstance(prior_org_shared, bool):
            resolved_org_shared = prior_org_shared
        else:
            resolved_org_shared = bool(org_shared)
        doc = {
            "principal": principal,
            "provider": provider,
            "provider_account_id": provider_account_id,
            "label": label or "",
            "oauth_token_enc": encrypt(oauth_token),
            "token_expires_at": token_expires_at,
            # None means "caller learned nothing new", not "no
            # window": the refresh cron's fb_exchange_token does not reset
            # data access, so the prior clock must survive a token refresh.
            "data_access_expires_at": (
                data_access_expires_at
                if data_access_expires_at is not None
                else (existing or {}).get("_source", {}).get("data_access_expires_at")
            ),
            "status": STATUS_ACTIVE,
            "scopes": list(scopes or []),
            "org_shared": resolved_org_shared,
            "legacy_asset_index": legacy_asset_index,
            "legacy_asset_esid": legacy_asset_esid,
            "created_at": (existing or {}).get("_source", {}).get("created_at") or now,
            "updated_at": now,
        }
        try:
            if existing:
                es.index(
                    index=INDEX_CONNECTION,
                    id=existing["_id"],
                    body=doc,
                    refresh="wait_for",
                )
                return {"success": True, "esid": existing["_id"], "doc": doc, "created": False}
            result = es.index(index=INDEX_CONNECTION, body=doc, refresh="wait_for")
            return {"success": True, "esid": result["_id"], "doc": doc, "created": True}
        except Exception as e:
            logging.error("ConnectionService.upsert_connection error: %s", e)
            return {"success": False, "error": str(e)}

    @staticmethod
    def get_connection(esid: str) -> dict | None:
        """Return the stored connection doc (with the token still
        encrypted) by ES doc id, or None if missing.
        """
        if not esid:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.get(index=INDEX_CONNECTION, id=esid)
        except Exception:
            return None
        return dict(result.get("_source") or {}) or None

    # ─────────────────────────────────────────────────────────────────────────
    # cross-host OAuth-state handoff (Google Ads per-user OAuth)
    # ─────────────────────────────────────────────────────────────────────────

    # State lives ~10 min: long enough for a human to click through Google's
    # consent screen, short enough that a leaked authorize URL can't be
    # replayed days later. The callback re-checks expiry defensively.
    OAUTH_STATE_TTL_SECONDS = 600

    @staticmethod
    def seed_oauth_state(connection_esid: str) -> dict:
        """mint a fresh OAuth state + 15-char auth_url path segment
        and stamp them on a pending connection so the fb_oauth-host callback
        can correlate Google's redirect back to this connection.

        The fb_oauth server is a separate Flask app with no shared session, so
        the state cannot live in flask.session (as the Meta reauth flow does).
        It is stored on the connection doc and looked up by ``oauth_state`` at
        callback. The 15-char ``oauth_auth_url`` mirrors the GA4/YouTube
        account-doc convention (``/google_ads/<auth_url>`` popup path).

        Returns ``{"success": True, "auth_url": ..., "state": ...}`` or
        ``{"success": False, "error": ...}``.
        """
        if not connection_esid:
            return {"success": False, "error": "connection_esid is required"}
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        # 15 chars to match the fb_oauth route guard (len == 15) used by the
        # YouTube/GA4 popup routes; token_urlsafe(11) yields ~15 url-safe chars.
        auth_url = secrets.token_urlsafe(11)[:15]
        state = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ConnectionService.OAUTH_STATE_TTL_SECONDS)).isoformat()
        try:
            es.update(
                index=INDEX_CONNECTION,
                id=connection_esid,
                doc={
                    "oauth_state": state,
                    "oauth_auth_url": auth_url,
                    "oauth_state_expires_at": expires_at,
                    "updated_at": _now_iso(),
                },
                refresh="wait_for",
            )
        except Exception as e:
            logging.warning("ConnectionService.seed_oauth_state %s: %s", connection_esid, e)
            return {"success": False, "error": str(e)}
        return {"success": True, "auth_url": auth_url, "state": state}

    @staticmethod
    def find_by_oauth_state(state: str) -> dict | None:
        """return the connection doc (with ``_id`` injected) whose
        ``oauth_state`` matches, or None. Used by the fb_oauth-host Google Ads
        callback to correlate Google's redirect. Queries the keyword field
        (oauth_state is mapped keyword, so the bare field is exact-match safe).
        """
        if not state:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.search(
                index=INDEX_CONNECTION,
                body={"query": {"term": {"oauth_state": state}}, "size": 1},
            )
        except Exception as e:
            logging.warning("ConnectionService.find_by_oauth_state: %s", e)
            return None
        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            return None
        doc = dict(hits[0].get("_source") or {})
        doc["_id"] = hits[0]["_id"]
        return doc

    @staticmethod
    def clear_oauth_state(connection_esid: str) -> None:
        """null out the transient OAuth-state fields once the token
        has landed, so a state value can never be replayed. Best-effort.
        """
        if not connection_esid:
            return
        es = get_elasticsearch_client()
        if es is None:
            return
        try:
            es.update(
                index=INDEX_CONNECTION,
                id=connection_esid,
                doc={
                    "oauth_state": None,
                    "oauth_auth_url": None,
                    "oauth_state_expires_at": None,
                    "updated_at": _now_iso(),
                },
                refresh="wait_for",
            )
        except Exception as e:
            logging.warning("ConnectionService.clear_oauth_state %s: %s", connection_esid, e)

    @staticmethod
    def is_connection_live(esid: str | None) -> bool:
        """is this connection something a connector card should
        surface as "connected" (and deep-link to /connections/<esid>)?

        True only when the connection exists AND its status is not
        ``revoked``. A deleted connection (get_connection → None) or a
        revoked one must NOT drive the Manage / connected-summary surface:
        the card would otherwise show "Manage" for a dead connection and
        the detail page would 404. Both card resolution paths
        (find_connection_esid_by_legacy_assets and the connector-status
        fallback) gate on this.

        ``expired`` is intentionally still "live" — an expired token is a
        real connection the user can reauthorize; revoked is the explicit
        teardown signal.
        """
        if not esid:
            return False
        doc = ConnectionService.get_connection(esid)
        if not doc:
            return False
        return (doc.get("status") or "") != STATUS_REVOKED

    @staticmethod
    def get_decrypted_token(esid: str) -> str | None:
        """Return the decrypted OAuth token for a connection by esid,
        or None if the connection is missing or has no token. The only
        code path that should call this is an importer that's about to
        hit the provider API.
        """
        doc = ConnectionService.get_connection(esid)
        if not doc:
            return None
        ciphertext = doc.get("oauth_token_enc")
        if not ciphertext:
            return None
        try:
            return decrypt(ciphertext)
        except Exception as e:
            logging.error("ConnectionService: decrypt failed for %s: %s", esid, e)
            return None

    @staticmethod
    def rotate_token(esid: str, *, oauth_token: str, token_expires_at: str | None = None) -> bool:
        """store a refreshed token on an existing connection.

        For providers whose refresh call can hand back a NEW refresh token
        (TikTok). Partial update: only the ciphertext, its expiry and
        ``updated_at`` change — status, scopes, label, principal and every
        team/asset reference stay as they are. Returns False when the write
        fails; the caller must then treat the old token as possibly dead.
        """
        if not esid or not oauth_token:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        doc = {"oauth_token_enc": encrypt(oauth_token), "updated_at": _now_iso()}
        if token_expires_at is not None:
            doc["token_expires_at"] = token_expires_at
        try:
            es.update(index=INDEX_CONNECTION, id=esid, doc=doc, refresh="wait_for")
            return True
        except Exception as e:  # noqa: BLE001 — reported to the caller as False
            logging.error("ConnectionService.rotate_token %s: %s", esid, e)
            return False

    @staticmethod
    def set_status(esid: str, status: str) -> bool:
        """Flip a connection's status (active / expired / revoked)."""
        if status not in _VALID_STATUSES:
            return False
        if not esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            es.update(
                index=INDEX_CONNECTION,
                id=esid,
                doc={"status": status, "updated_at": _now_iso()},
                refresh="wait_for",
            )
            return True
        except Exception as e:
            logging.warning("ConnectionService.set_status %s: %s", esid, e)
            return False

    @staticmethod
    def sweep_abandoned_pending_connections(max_age_hours: int = 24) -> int:
        """hard-delete abandoned wizard OAuth placeholder rows.

        The data-connectors wizard mints a ``pending_token`` placeholder
        (``provider_account_id`` = ``wizard-pending-<nonce>``) at the START
        of the OAuth roundtrip — before the popup opens — so the frontend
        has a stable esid to poll. If the user abandons the Meta consent
        screen the callback never lands, ``set_token_for_pending_connection``
        never runs, and the placeholder lingers forever (prod 2026-06-27:
        5 such rows across 3 users, oldest ~4 weeks). ``find_reusable_pending_
        connection`` recycles one on a *re-authorize*, but a user
        who never returns leaves a permanent orphan.

        Such a row is provably inert — no usable ``oauth_token_enc``, no
        ``de_connection_asset`` children, no team reference, and the billing
        connection-count (SubscriptionStateService) already excludes the
        ``wizard-pending-*`` prefix — so we hard-delete rather than introduce
        a 5th status (decision logged on -06-27).

        also deletes ``superseded`` tombstones (a placeholder whose
        OAuth completion merged into an existing row; nothing references them
        once the wizard's poll has redirected the frontend).

        Deletes ONLY rows matching ALL of:
          * ``status == pending_token`` AND ``provider_account_id`` starts
            with ``PENDING_ACCOUNT_ID_PREFIX`` — OR ``status == superseded``
          * ``updated_at`` older than ``max_age_hours`` (don't race a wizard
            flow that's mid-roundtrip, or a poll that hasn't followed the
            tombstone redirect yet)
          * no USABLE ``oauth_token_enc`` — checked in Python by truthiness,
            NOT by an ES ``exists`` filter. The wizard writes the placeholder
            with ``oauth_token_enc = ""`` (empty string, not an absent field),
            and ES ``exists`` matches an empty string — so an ``exists`` guard
            would wrongly spare every real placeholder (caught on staging,
            2026-06-27). Truthiness mirrors ``_process_connection``'s
            ``if not encrypted_token`` check in the refresh job.

        We resolve candidates first, then delete by explicit id list — a
        destructive op should target a reviewed id set, not fire a blind
        ``delete_by_query`` whose match set we never saw.

        Returns the number of rows deleted, or 0 on any error / no matches.
        Fail-soft: never raises — the caller (a cron) must not abort on it.
        """
        es = get_elasticsearch_client()
        if es is None:
            return 0

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        query = {
            "bool": {
                "filter": [
                    {"range": {"updated_at": {"lt": cutoff}}},
                ],
                "should": [
                    {
                        "bool": {
                            "filter": [
                                {"term": {"status": STATUS_PENDING_TOKEN}},
                                {"prefix": {"provider_account_id": PENDING_ACCOUNT_ID_PREFIX}},
                            ]
                        }
                    },
                    # merged-placeholder tombstones (see docstring).
                    {"term": {"status": STATUS_SUPERSEDED}},
                ],
                "minimum_should_match": 1,
            }
        }
        try:
            result = es.search(
                index=INDEX_CONNECTION,
                body={"query": query, "size": 1000},
            )
            hits = result.get("hits", {}).get("hits", [])
            # Token truthiness in Python — an empty-string token is still a
            # placeholder (see docstring; ES ``exists`` can't express this).
            stale_ids = [h["_id"] for h in hits if not (h.get("_source") or {}).get("oauth_token_enc")]
            if not stale_ids:
                return 0
            bulk_body = [{"delete": {"_index": INDEX_CONNECTION, "_id": cid}} for cid in stale_ids]
            es.bulk(body=bulk_body, refresh=True)
            logging.info(
                "ConnectionService.sweep_abandoned_pending_connections: "
                "deleted %d abandoned wizard placeholder(s) older than %dh: %s",
                len(stale_ids),
                max_age_hours,
                stale_ids,
            )
            return len(stale_ids)
        except Exception as e:
            logging.warning("ConnectionService.sweep_abandoned_pending_connections failed: %s", e)
            return 0

    @staticmethod
    def set_token_for_pending_connection(
        esid: str,
        *,
        provider_account_id: str,
        oauth_token: str,
        token_expires_at: str | None = None,
        data_access_expires_at: str | None = None,
        scopes: list[str] | None = None,
        label: str | None = None,
    ) -> str | None:
        """Path A — flip a pending_token row to active.

        ``label`` — when given, replaces the row's label. The
        placeholder's label is written before the provider account is known;
        TikTok's callback passes the account's @username once it is. None
        (every other caller) leaves the label untouched.

        The data-connectors wizard creates a pending_token connection
        BEFORE the OAuth popup opens, so the frontend has a stable esid
        to poll. The real Meta /me user_id arrives only at callback
        time, at which point this helper:

          1. Rewrites ``provider_account_id`` from the placeholder
             (``wizard-pending-...``) to the real Meta user_id.
          2. Encrypts and stores the access token.
          3. Records ``token_expires_at`` + the granted ``scopes``.
          4. Flips ``status`` to ``active``.

        Why not ``upsert_connection``? Upsert keys on
        (principal, provider, provider_account_id). The placeholder
        and real natural keys differ, so an upsert would create a
        second row and leave the placeholder orphaned — and the
        frontend would still be polling the placeholder esid.
        Updating by esid keeps the polling shape stable.

        dedup at OAuth completion. The in-place rewrite above
        was how duplicate de_connection rows accumulated: every COMPLETED
        wizard re-run turned a fresh placeholder into another active row
        for the same real (principal, provider, provider_account_id)
        (prod 2026-07-01: 6 fb_app_review + 4 allen_messimer rows for one
        account each). So when the real account id arrives and an existing
        row already holds that natural key, the token is stamped on THAT
        row (org_shared / created_at / label / any team or asset references
        to it stay intact — a revoked row reactivates, which is a legit
        reconnect), and this placeholder becomes a ``superseded`` tombstone
        pointing at it. The wizard's oauth-status poll follows
        ``superseded_by`` so the frontend adopts the canonical esid; the
        abandoned-placeholder sweep deletes the tombstone later. Different
        principals keep separate rows — the natural key includes the
        principal, so one user's re-auth can never overwrite another's
        token (design decision on -07-02).

        Returns the CANONICAL connection esid (== ``esid`` when no merge
        happened), or None if the connection is missing or ES is
        unreachable. Caller is responsible for authorization (only the
        connection's principal should be able to flip it).
        """
        if not esid:
            return None
        if not provider_account_id:
            return None
        if not oauth_token:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None

        # merge lookup. Only a placeholder-keyed row merges: a row
        # already carrying a real account id (defensive; today's only caller
        # passes wizard placeholders) keeps the plain in-place flip.
        canonical: dict | None = None
        try:
            current = es.get(index=INDEX_CONNECTION, id=esid)
            current_source = dict(current.get("_source") or {})
        except Exception as e:
            logging.warning("ConnectionService.set_token_for_pending_connection %s: %s", esid, e)
            return None
        if str(current_source.get("provider_account_id") or "").startswith(PENDING_ACCOUNT_ID_PREFIX):
            existing = ConnectionService._find_by_natural_key(
                es,
                current_source.get("principal") or "",
                current_source.get("provider") or "",
                provider_account_id,
            )
            if existing and existing.get("_id") and existing["_id"] != esid:
                canonical = existing

        token_doc = {
            "provider_account_id": provider_account_id,
            "oauth_token_enc": encrypt(oauth_token),
            "token_expires_at": token_expires_at,
            "data_access_expires_at": data_access_expires_at,
            "status": STATUS_ACTIVE,
            "scopes": list(scopes or []),
            "updated_at": _now_iso(),
        }
        if label is not None:
            token_doc["label"] = label
        try:
            if canonical is not None:
                es.update(
                    index=INDEX_CONNECTION,
                    id=canonical["_id"],
                    doc=token_doc,
                    refresh="wait_for",
                )
                es.update(
                    index=INDEX_CONNECTION,
                    id=esid,
                    doc={
                        "status": STATUS_SUPERSEDED,
                        "superseded_by": canonical["_id"],
                        "oauth_token_enc": "",
                        "updated_at": _now_iso(),
                    },
                    refresh="wait_for",
                )
                logging.info(
                    "ConnectionService.set_token_for_pending_connection: merged "
                    "placeholder %s into existing connection %s (same natural key)",
                    esid,
                    canonical["_id"],
                )
                return str(canonical["_id"])
            es.update(
                index=INDEX_CONNECTION,
                id=esid,
                doc=token_doc,
                refresh="wait_for",
            )
            return esid
        except Exception as e:
            logging.warning("ConnectionService.set_token_for_pending_connection %s: %s", esid, e)
            return None

    # ── re-auth by esid, not by natural key ──────────────────────
    #
    # ``upsert_connection`` routes on (principal, provider, provider_account_id).
    # The re-auth callback passed ``principal=<the actor>``, which was harmless
    # only because the gate allowed nobody but the principal through. Widening
    # that gate (Tier 1) turns the same call into a silent failure: a second
    # person's re-auth routes to a DIFFERENT row, they see a green success page,
    # and every team/asset reference still points at the dead esid.
    #
    # These helpers write by esid so the row a user re-authorised is the row that
    # gets the token. ``principal`` is re-stamped to record who last supplied it —
    # an attribute, not an identity. The full re-key lives.

    @staticmethod
    def adopt_reauth_token(
        esid: str,
        *,
        actor: str,
        oauth_token: str,
        token_expires_at: str | None = None,
        data_access_expires_at: str | None = None,
        scopes: list[str] | None = None,
        coverage: dict | None = None,
        token_kind: str | None = None,
    ) -> bool:
        """Commit a re-auth token onto THIS connection row.

        ``token_kind`` records HOW this connection authenticates
        ("system_user" vs the default user-OAuth). It is load-bearing, not
        decorative: the re-auth route refuses to run an OAuth round-trip on a
        system-user connection (that would silently swap a non-expiring token
        for one on a 90-day clock), and the banner uses it to offer the Business
        Manager action instead of a Reconnect button that cannot help. Omitted
        (None) leaves whatever the row already carried, so an ordinary re-auth
        never clears it.

        Preserves label / org_shared / created_at and every team+asset reference,
        because the row identity never changes. Clears any staged token so a
        committed re-auth cannot leave a half-finished one behind.
        """
        if not esid or not oauth_token:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        doc = {
            "oauth_token_enc": encrypt(oauth_token),
            "token_expires_at": token_expires_at,
            "data_access_expires_at": data_access_expires_at,
            "status": STATUS_ACTIVE,
            "scopes": list(scopes or []),
            "principal": actor,
            "last_reauth_by": actor,
            "last_reauth_at": _now_iso(),
            "reauth_coverage": coverage or {},
            "pending_reauth_token_enc": "",
            "pending_reauth_coverage": {},
            "pending_reauth_data_access_expires_at": None,
            "updated_at": _now_iso(),
        }
        if token_kind:
            doc["token_kind"] = token_kind
        try:
            es.update(index=INDEX_CONNECTION, id=esid, doc=doc, refresh="wait_for")
        except Exception as e:  # noqa: BLE001
            logging.warning("ConnectionService.adopt_reauth_token %s: %s", esid, e)
            return False

        # the token is live; re-probe health NOW rather than at the
        # next 6-hourly cron. Every path that commits a re-auth funnels through
        # this method (partial accept, full-coverage commit, system-user, and the
        # wizard adopt), so this one call covers all of them. The whole
        # sweep is launched, not a single-credential probe — cross-credential
        # coverage is computed across the fleet. Best-effort: the adopt has
        # already succeeded and bookkeeping never undoes it.
        try:
            from asset_management.web.services.credential_health_trigger import trigger_sweep_async

            trigger_sweep_async(reason=f"reauth adopt {esid} by {actor}")
        except Exception as e:  # noqa: BLE001 — never fail a committed re-auth on health bookkeeping
            logging.warning("post-adopt health sweep trigger failed for %s: %s", esid, e)
        return True

    @staticmethod
    def find_by_natural_key(principal: str, provider: str, provider_account_id: str) -> dict | None:
        """Public thin wrapper over the natural-key lookup (Phase B:
        the wizard-adoption decision must know whether the
        same-principal merge will handle a callback before considering
        adoption candidates). Returns the ES hit dict or None."""
        es = get_elasticsearch_client()
        if es is None:
            return None
        return ConnectionService._find_by_natural_key(es, principal, provider, provider_account_id)

    @staticmethod
    def list_adoption_candidates(provider: str, *, exclude_esid: str = "") -> list[dict]:
        """Phase B — connections a wizard OAuth completion could
        legitimately be a RECONNECT of.

        Returns raw ES hits for rows that are real connections (``active``,
        ``expired`` or ``revoked`` — a revoked row reactivating is a legit
        reconnect, same as the merge), same provider, excluding:

          * placeholders (``pending_token``) and ``superseded`` tombstones;
          * ``token_kind == system_user`` rows — adopting a wizard token onto
            one would silently swap a non-expiring token for a 90-day one
            (the exact downgrade start_reauthorize refuses);
          * ``exclude_esid`` (the caller's own placeholder).

        Authorization and asset-coverage filtering are the CALLER's job —
        this is enumeration only, and its result must never be shown to a
        user unfiltered (it spans workspaces).
        """
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            result = es.search(
                index=INDEX_CONNECTION,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"provider": provider}},
                                {"terms": {"status": [STATUS_ACTIVE, STATUS_EXPIRED, STATUS_REVOKED]}},
                            ],
                            "must_not": [
                                {"term": {"token_kind": "system_user"}},
                            ],
                        }
                    },
                    "size": 200,
                },
            )
        except Exception as e:  # noqa: BLE001 — enumeration failure = no candidates (adoption is optional)
            logging.warning("ConnectionService.list_adoption_candidates(%s): %s", provider, e)
            return []
        hits = result.get("hits", {}).get("hits", [])
        return [h for h in hits if h.get("_id") and h["_id"] != exclude_esid]

    @staticmethod
    def adopt_pending_into(
        placeholder_esid: str,
        candidate_esid: str,
        *,
        actor: str,
        oauth_token: str,
        token_expires_at: str | None = None,
        data_access_expires_at: str | None = None,
        scopes: list[str] | None = None,
        coverage: dict | None = None,
    ) -> str | None:
        """Phase B — commit a wizard OAuth completion onto an
        EXISTING connection instead of minting a new row.

        The mechanics are exactly the merge shape: the token lands
        on the candidate via :meth:`adopt_reauth_token` (row identity, label,
        org_shared and every team/asset reference stay intact; ``principal``
        is re-stamped as provenance), then the placeholder becomes a
        ``superseded`` tombstone pointing at it — the wizard's oauth-status
        poll follows ``superseded_by``, so the frontend adopts the canonical
        esid with no UI change.

        Returns the candidate esid on success, None on failure (the caller
        falls back to the plain pending-flip — never a lost token).
        """
        if not placeholder_esid or not candidate_esid or placeholder_esid == candidate_esid:
            return None
        if not ConnectionService.adopt_reauth_token(
            candidate_esid,
            actor=actor,
            oauth_token=oauth_token,
            token_expires_at=token_expires_at,
            data_access_expires_at=data_access_expires_at,
            scopes=scopes,
            coverage=coverage,
        ):
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            es.update(
                index=INDEX_CONNECTION,
                id=placeholder_esid,
                doc={
                    "status": STATUS_SUPERSEDED,
                    "superseded_by": candidate_esid,
                    "oauth_token_enc": "",
                    "updated_at": _now_iso(),
                },
                refresh="wait_for",
            )
        except Exception as e:  # noqa: BLE001
            # The token is safely committed on the candidate; a stranded
            # placeholder is swept by the abandoned-pending sweep. Log and
            # still report success so the wizard advances to the right row.
            logging.warning("ConnectionService.adopt_pending_into: tombstone failed for %s: %s", placeholder_esid, e)
        logging.info(
            "ConnectionService.adopt_pending_into: wizard OAuth by %s adopted onto existing connection %s (placeholder %s superseded)",
            actor,
            candidate_esid,
            placeholder_esid,
        )
        return str(candidate_esid)

    @staticmethod
    def stage_reauth_token(
        esid: str,
        *,
        actor: str,
        oauth_token: str,
        token_expires_at: str | None = None,
        data_access_expires_at: str | None = None,
        scopes: list[str] | None = None,
        coverage: dict | None = None,
        token_kind: str | None = None,
    ) -> bool:
        """Hold a PARTIAL-coverage re-auth token pending explicit acknowledgment.

         requires partial coverage to be acknowledged *before commit*. The
        token is parked on ``pending_reauth_token_enc``, which no importer reads,
        so nothing goes live until :meth:`promote_staged_reauth`. Storing it here
        rather than in the Flask session keeps a live credential out of a signed
        cookie.
        """
        if not esid or not oauth_token:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        doc = {
            "pending_reauth_token_enc": encrypt(oauth_token),
            "pending_reauth_expires_at": token_expires_at,
            "pending_reauth_data_access_expires_at": data_access_expires_at,
            "pending_reauth_scopes": list(scopes or []),
            "pending_reauth_coverage": coverage or {},
            "pending_reauth_by": actor,
            "pending_reauth_at": _now_iso(),
            # Carried through staging so promotion cannot lose it — a staged
            # system-user token that committed as an ordinary one would leave the
            # row claiming an expiry model it does not have.
            "pending_reauth_token_kind": token_kind or "",
            "updated_at": _now_iso(),
        }
        try:
            es.update(index=INDEX_CONNECTION, id=esid, doc=doc, refresh="wait_for")
            return True
        except Exception as e:  # noqa: BLE001
            logging.warning("ConnectionService.stage_reauth_token %s: %s", esid, e)
            return False

    @staticmethod
    def promote_staged_reauth(esid: str, *, actor: str) -> bool:
        """Commit a previously staged partial-coverage token after acknowledgment."""
        if not esid:
            return False
        source = ConnectionService.get_connection(esid) or {}
        staged = source.get("pending_reauth_token_enc") or ""
        if not staged:
            logging.info("ConnectionService.promote_staged_reauth: nothing staged on %s", esid)
            return False
        token = decrypt(staged)
        if not token:
            logging.warning("ConnectionService.promote_staged_reauth: staged token on %s decrypts empty", esid)
            return False
        return ConnectionService.adopt_reauth_token(
            esid,
            actor=actor,
            oauth_token=token,
            token_expires_at=source.get("pending_reauth_expires_at"),
            data_access_expires_at=source.get("pending_reauth_data_access_expires_at"),
            scopes=list(source.get("pending_reauth_scopes") or []),
            coverage=source.get("pending_reauth_coverage") or {},
            token_kind=source.get("pending_reauth_token_kind") or None,
        )

    @staticmethod
    def discard_staged_reauth(esid: str) -> bool:
        """Drop a staged token the user declined to accept."""
        if not esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            es.update(
                index=INDEX_CONNECTION,
                id=esid,
                doc={
                    "pending_reauth_token_enc": "",
                    "pending_reauth_coverage": {},
                    "pending_reauth_data_access_expires_at": None,
                    "pending_reauth_by": "",
                    "updated_at": _now_iso(),
                },
                refresh="wait_for",
            )
            return True
        except Exception as e:  # noqa: BLE001
            logging.warning("ConnectionService.discard_staged_reauth %s: %s", esid, e)
            return False

    @staticmethod
    def set_org_shared(esid: str, org_shared: bool) -> bool:
        """Flip a connection's org-shared flag.

        Reversibility surface for the wizard's step-6 org-shared
        toggle. Returns True on a successful update, False if the
        connection is missing or ES is unreachable. The caller is
        responsible for authorization (only the connection's principal
        — or a superuser — should be allowed to flip this).
        """
        if not esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            es.update(
                index=INDEX_CONNECTION,
                id=esid,
                doc={"org_shared": bool(org_shared), "updated_at": _now_iso()},
                refresh="wait_for",
            )
            return True
        except Exception as e:
            logging.warning("ConnectionService.set_org_shared %s: %s", esid, e)
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Connection asset — create / read
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def create_connection_asset(
        *,
        connection_esid: str,
        asset_type: str,
        provider_asset_id: str,
        label: str = "",
        metadata: dict | None = None,
        legacy_asset_index: str | None = None,
        legacy_asset_esid: str | None = None,
    ) -> dict:
        """Create a connection_asset under an existing connection."""
        if not connection_esid:
            return {"success": False, "error": "connection_esid is required"}
        if asset_type not in _VALID_ASSET_TYPES:
            return {
                "success": False,
                "error": f"asset_type must be one of {_VALID_ASSET_TYPES}; got {asset_type!r}",
            }
        if not provider_asset_id:
            return {"success": False, "error": "provider_asset_id is required"}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        # follow-up — stamp principal denormalised from the
        # parent connection. The preview-sweep config keys this
        # index by ``principal``; without the stamp the sweep is a no-op
        # against asset rows. Missing parent connection is non-fatal:
        # the asset row still lands (back-pointer via connection_esid is
        # intact) but its principal is empty, which downstream sweep
        # treats as orphan.
        principal = ConnectionService._lookup_principal(es, connection_esid)
        now = _now_iso()
        doc = {
            "connection_esid": connection_esid,
            "principal": principal or "",
            "asset_type": asset_type,
            "provider_asset_id": provider_asset_id,
            "label": label or "",
            "metadata": metadata or {},
            "legacy_asset_index": legacy_asset_index,
            "legacy_asset_esid": legacy_asset_esid,
            "created_at": now,
            "updated_at": now,
        }
        try:
            result = es.index(index=INDEX_CONNECTION_ASSET, body=doc, refresh="wait_for")
        except Exception as e:
            logging.error("ConnectionService.create_connection_asset error: %s", e)
            return {"success": False, "error": str(e)}
        return {"success": True, "esid": result["_id"], "doc": doc}

    @staticmethod
    def upsert_connection_asset(
        *,
        connection_esid: str,
        asset_type: str,
        provider_asset_id: str,
        label: str = "",
        metadata: dict | None = None,
        legacy_asset_index: str | None = None,
        legacy_asset_esid: str | None = None,
    ) -> dict:
        """Create-or-refresh a connection_asset. Natural key =
        (connection_esid, asset_type, provider_asset_id). Used by the
        backfill so re-running is idempotent.
        """
        if not connection_esid:
            return {"success": False, "error": "connection_esid is required"}
        if asset_type not in _VALID_ASSET_TYPES:
            return {
                "success": False,
                "error": f"asset_type must be one of {_VALID_ASSET_TYPES}; got {asset_type!r}",
            }
        if not provider_asset_id:
            return {"success": False, "error": "provider_asset_id is required"}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        existing = ConnectionService._find_asset_by_natural_key(es, connection_esid, asset_type, provider_asset_id)
        # follow-up — stamp principal denormalised from parent
        # connection. See ``create_connection_asset`` for rationale.
        principal = ConnectionService._lookup_principal(es, connection_esid)
        now = _now_iso()
        doc = {
            "connection_esid": connection_esid,
            "principal": principal or "",
            "asset_type": asset_type,
            "provider_asset_id": provider_asset_id,
            "label": label or "",
            "metadata": metadata or {},
            "legacy_asset_index": legacy_asset_index,
            "legacy_asset_esid": legacy_asset_esid,
            "created_at": (existing or {}).get("_source", {}).get("created_at") or now,
            "updated_at": now,
        }
        try:
            if existing:
                es.index(
                    index=INDEX_CONNECTION_ASSET,
                    id=existing["_id"],
                    body=doc,
                    refresh="wait_for",
                )
                return {"success": True, "esid": existing["_id"], "doc": doc, "created": False}
            result = es.index(index=INDEX_CONNECTION_ASSET, body=doc, refresh="wait_for")
            return {"success": True, "esid": result["_id"], "doc": doc, "created": True}
        except Exception as e:
            logging.error("ConnectionService.upsert_connection_asset error: %s", e)
            return {"success": False, "error": str(e)}

    @staticmethod
    def list_assets_for_connection(connection_esid: str) -> list[dict]:
        """Return every connection_asset doc under a connection.
        Used by the wizard confirm-step + the importer read-path.
        Each item is ``{"_id": ..., "_source": {...}}``.
        """
        if not connection_esid:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            result = es.search(
                index=INDEX_CONNECTION_ASSET,
                body={
                    "query": {"term": {"connection_esid": connection_esid}},
                    "size": 1000,
                },
            )
            return list(result.get("hits", {}).get("hits", []))
        except Exception as e:
            logging.error("ConnectionService.list_assets_for_connection error: %s", e)
            return []

    @staticmethod
    def list_connection_assets_by_type(asset_type: str) -> list[dict]:
        """Return every connection_asset row of a given ``asset_type`` across
        all connections. Each item is ``{"_id": ..., "_source": {...}}``.

        Used by team-scoped pull importers that
        enumerate their sites by asset_type rather than by principal. Contains
        no secrets — the encrypted token lives on the parent ``de_connection``.
        """
        if asset_type not in _VALID_ASSET_TYPES:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            result = es.search(
                index=INDEX_CONNECTION_ASSET,
                body={"query": {"term": {"asset_type": asset_type}}, "size": 1000},
            )
            return list(result.get("hits", {}).get("hits", []))
        except Exception as e:
            logging.error("ConnectionService.list_connection_assets_by_type %s: %s", asset_type, e)
            return []

    @staticmethod
    def get_ga4_oauth_connection_for_property(property_id: str) -> tuple[str, str] | None:
        """resolve a GA4 ``property_id`` to the active GA4 OAuth
        connection serving it, as ``(connection_esid, account_esid)``, or
        ``None`` when no active GA4 connection covers it (importer then falls
        back to the service account).

        Resolution chain (provider-correct — does NOT read the team-doc's single
        ``connection_esid`` field, which YouTube + GA4 share and would collide on):

            property_id
              -> de_connection_asset(asset_type=ga4_property, provider_asset_id=property_id)
              -> de_connection(provider=ga4, status=active)
              -> (connection_esid, legacy_asset_esid)  (the de_asset_ga4_accounts esid)

        GA4 ids surface as bare numbers on the asset doc but may carry a
        ``properties/`` prefix elsewhere; the caller should pass the bare id.
        The account esid is where the token lives; the connection esid is what
        the importer flips to ``revoked`` if the token turns out to be dead.
        """
        pid = str(property_id or "").split("/")[-1].strip()
        if not pid:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.search(
                index=INDEX_CONNECTION_ASSET,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"asset_type": ASSET_TYPE_GA4_PROPERTY}},
                                {"term": {"provider_asset_id": pid}},
                            ]
                        }
                    },
                    "size": 50,
                },
            )
        except Exception as e:
            logging.warning("ConnectionService.get_ga4_oauth_connection_for_property %s: %s", pid, e)
            return None

        for hit in result.get("hits", {}).get("hits", []):
            connection_esid = (hit.get("_source") or {}).get("connection_esid")
            if not connection_esid:
                continue
            conn = ConnectionService.get_connection(connection_esid)
            if not conn:
                continue
            # Only an active GA4 connection counts; a revoked/expired one falls
            # back to the service account rather than failing the import.
            if conn.get("provider") != PROVIDER_GA4 or conn.get("status") != STATUS_ACTIVE:
                continue
            account_esid = conn.get("legacy_asset_esid")
            if account_esid:
                return (connection_esid, account_esid)
        return None

    @staticmethod
    def get_ga4_oauth_account_esid_for_property(property_id: str) -> str | None:
        """thin wrapper returning just the ga4_account esid for a
        property (the token carrier), or ``None``. See
        :meth:`get_ga4_oauth_connection_for_property` for the full resolution.
        """
        resolved = ConnectionService.get_ga4_oauth_connection_for_property(property_id)
        return resolved[1] if resolved else None

    @staticmethod
    def get_google_ads_oauth_connection_esid(
        customer_id: str,
        *,
        prefer_connection_esid: str | None = None,
    ) -> str | None:
        """resolve a Google Ads customer id to the esid of an
        ACTIVE ``google_ads`` OAuth connection that carries a token, or ``None``.

        Unlike GA4 (where a connection holds many properties and the lookup
        goes property → connection_asset → connection), the Google Ads
        connection's natural key already includes the customer id as
        ``provider_account_id`` — the P1 wizard stamps it there — so this is a
        direct de_connection lookup. ``None`` means "no usable OAuth connection
        for this customer"; the Google Ads importer then SKIPS the customer and
        surfaces a "reconnect" error (there is no service-account fallback — it
        was retired).

        **principal scoping.** Matching by customer id alone is
        principal-blind: if two principals each hold a tokened connection for the
        same Google Ads customer, this returned whichever ES surfaced first
        (non-deterministic), so an import could authenticate with a refresh
        token belonging to a *different* user than the one who owns the
        importing team. Customer ids map to a single real-world account so this
        is a least-privilege / credential-attribution gap, not cross-tenant data
        exposure — but it makes per-user revocation ambiguous and is wrong on
        least-privilege grounds.

        ``prefer_connection_esid`` closes the gap. When the caller knows which
        connection owns the importing team (the team doc's
        ``google_ads_connection_esid`` stamp, written by the wizard / tokenless
        finalize), it passes it here: the resolver then accepts ONLY that exact
        connection — and only if it is the right customer, active, and tokened.
        A foreign principal's connection is never selected; if the team's own
        connection doesn't qualify, the result is ``None`` (skip + surface),
        never a silent swap to someone else's token.

        When ``prefer_connection_esid`` is ``None`` (a team predating the stamp,
        or a caller that legitimately wants "any" — e.g. the wizard's
        already-authorized check), the legacy first-tokened-match behavior is
        kept, but a WARNING is logged if more than one tokened connection
        matches the customer so the ambiguity is visible.

        Customer ids are normalised (dashes stripped) on both sides so
        ``230-994-4882`` and ``2309944882`` resolve the same connection.
        """
        normalized = str(customer_id or "").replace("-", "").strip()
        if not normalized:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.search(
                index=INDEX_CONNECTION,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"provider": PROVIDER_GOOGLE_ADS}},
                                {"term": {"status": STATUS_ACTIVE}},
                            ]
                        }
                    },
                    "size": 100,
                },
            )
        except Exception as e:
            logging.warning("ConnectionService.get_google_ads_oauth_connection_esid %s: %s", normalized, e)
            return None

        # Active + tokened connections whose provider_account_id matches the
        # customer (normalised — the stored value may carry dashes).
        matches = [
            hit["_id"]
            for hit in result.get("hits", {}).get("hits", [])
            if str((hit.get("_source") or {}).get("provider_account_id") or "").replace("-", "").strip() == normalized
            and (hit.get("_source") or {}).get("oauth_token_enc")
        ]

        if prefer_connection_esid:
            # only the importing team's OWN connection is acceptable.
            # If it isn't among the customer's tokened matches, return None so the
            # importer skips + surfaces, rather than borrowing a foreign token.
            if prefer_connection_esid in matches:
                return prefer_connection_esid
            logging.warning(
                "team's google_ads_connection_esid %s is not an active+tokened "
                "connection for customer %s (matches=%s) — not substituting a foreign "
                "principal's token; importer will skip + surface.",
                prefer_connection_esid,
                normalized,
                matches,
            )
            return None

        if not matches:
            return None
        if len(matches) > 1:
            logging.warning(
                "customer %s has %d active+tokened google_ads connections (%s) and the "
                "caller passed no prefer_connection_esid — returning the first principal-blind "
                "match. Stamp google_ads_connection_esid on the team to scope this.",
                normalized,
                len(matches),
                matches,
            )
        return matches[0]

    @staticmethod
    def create_disciple_tools_site(
        *,
        principal: str,
        dt_domain: str,
        our_domain: str,
        token: str,
        team_esid: str,
        label: str = "",
    ) -> dict:
        """create-or-refresh the DT-site credential for one team.

        Encrypts the Site-to-Site Link ``token`` on a ``de_connection`` row
        (provider=disciple_tools, provider_account_id=normalised dt_domain) and
        writes a ``dt_site`` connection_asset whose provider_asset_id is the
        ``team_esid`` the importer writes to, with ``metadata`` holding the two
        domains the bearer hash needs. Idempotent (natural-key upserts).

        Returns ``{"success": True, "connection_esid": ..., "asset_esid": ...}``
        or ``{"success": False, "error": ...}``.
        """
        if not dt_domain or not our_domain or not token or not team_esid:
            return {"success": False, "error": "dt_domain, our_domain, token, team_esid are all required"}
        norm_dt = dt_domain.replace("https://", "").replace("http://", "").replace("//", "").replace("/", "").strip()
        conn = ConnectionService.upsert_connection(
            principal=principal,
            provider=PROVIDER_DISCIPLE_TOOLS,
            provider_account_id=norm_dt,
            oauth_token=token,
            label=label or f"Disciple.Tools — {norm_dt}",
        )
        if not conn.get("success"):
            return conn
        connection_esid = conn["esid"]
        asset = ConnectionService.upsert_connection_asset(
            connection_esid=connection_esid,
            asset_type=ASSET_TYPE_DT_SITE,
            provider_asset_id=team_esid,
            label=label or norm_dt,
            metadata={"dt_domain": norm_dt, "our_domain": our_domain},
        )
        if not asset.get("success"):
            return asset
        return {"success": True, "connection_esid": connection_esid, "asset_esid": asset["esid"]}

    @staticmethod
    def create_bitly_connection(
        *,
        principal: str,
        team_esid: str,
        api_token: str,
        tags: list[str],
        label: str = "",
    ) -> dict:
        """Phase 3 — create-or-refresh a team's Bitly credential.

        Mirrors ``create_disciple_tools_site``: encrypts the Bitly v4 API
        ``api_token`` on a ``de_connection`` row (provider=bitly,
        provider_account_id=team_esid — one connection per team, so a
        re-configure for the same team upserts rather than duplicating) and
        writes a ``bitly_account`` connection_asset whose provider_asset_id is
        the ``team_esid`` the importer writes to, with ``metadata.tags``
        holding the tag filter the importer scopes links by. Idempotent
        (natural-key upserts).

        Returns ``{"success": True, "connection_esid": ..., "asset_esid": ...}``
        or ``{"success": False, "error": ...}``.
        """
        if not team_esid or not api_token:
            return {"success": False, "error": "team_esid and api_token are required"}
        clean_tags = sorted({str(t).strip() for t in (tags or []) if str(t).strip()})
        if not clean_tags:
            return {"success": False, "error": "at least one tag is required"}
        conn = ConnectionService.upsert_connection(
            principal=principal,
            provider=PROVIDER_BITLY,
            provider_account_id=team_esid,
            oauth_token=api_token,
            label=label or "Bitly",
        )
        if not conn.get("success"):
            return conn
        connection_esid = conn["esid"]
        asset = ConnectionService.upsert_connection_asset(
            connection_esid=connection_esid,
            asset_type=ASSET_TYPE_BITLY_ACCOUNT,
            provider_asset_id=team_esid,
            label=label or "Bitly",
            metadata={"tags": clean_tags},
        )
        if not asset.get("success"):
            return asset
        return {"success": True, "connection_esid": connection_esid, "asset_esid": asset["esid"]}

    @staticmethod
    def list_connections_for_principal(principal: str) -> list[dict]:
        """Return every connection doc for a principal (one per provider
        in the common case). Each item is ``{"_id": ..., "_source": {...}}``.
        """
        if not principal:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            result = es.search(
                index=INDEX_CONNECTION,
                body={
                    "query": {"term": {"principal": principal}},
                    "size": 100,
                },
            )
            return list(result.get("hits", {}).get("hits", []))
        except Exception as e:
            logging.error("ConnectionService.list_connections_for_principal error: %s", e)
            return []

    @staticmethod
    def find_connection_by_account_prefix(principal: str, provider: str, account_id_prefix: str) -> dict | None:
        """newest connection for ``(principal, provider)`` whose
        ``provider_account_id`` starts with ``account_id_prefix``, ANY status.

        Exists for placeholder families that are per-user scratch identities
        rather than per-grant rows — the Google Ads ``discovery:`` connection
        being the first: one row per user is correct, so the wizard reuses it
        (re-seeding OAuth state) instead of minting one per attempt. Status is
        deliberately not filtered: a tokened discovery row is just as reusable —
        re-auth overwrites its token in place.

        Returns ``{"_id", "_source"}`` or None. Fail-soft: errors return None
        and the caller mints a fresh row (the earlier behavior).
        """
        if not principal or not provider or not account_id_prefix:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.search(
                index=INDEX_CONNECTION,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"principal": principal}},
                                {"term": {"provider": provider}},
                                {"prefix": {"provider_account_id": account_id_prefix}},
                            ]
                        }
                    },
                    "sort": [{"created_at": {"order": "desc"}}],
                    "size": 1,
                },
            )
        except Exception as e:
            logging.warning(
                "ConnectionService.find_connection_by_account_prefix %s/%s: %s",
                principal,
                provider,
                e,
            )
            return None
        hits = result.get("hits", {}).get("hits", [])
        return hits[0] if hits else None

    @staticmethod
    def find_reusable_pending_connection(principal: str, provider: str) -> dict | None:
        """Return an existing reusable ``pending_token`` placeholder row for
        ``(principal, provider)`` so the wizard can re-use it on a
        re-authorize instead of minting a new placeholder (fix,
        2026-06-05).

        "Reusable" means: ``status == pending_token`` AND the
        ``provider_account_id`` still carries the ``PENDING_ACCOUNT_ID_PREFIX``
        placeholder — i.e. OAuth never completed on it. An already-active
        connection (real provider_account_id) is NEVER returned: that's a
        completed grant, not a placeholder to recycle.

        Returns the most-recently-created match as ``{"_id", "_source"}`` or
        ``None``. Fail-soft: ES errors return None (caller mints a fresh
        placeholder, same as before this method existed).
        """
        if not principal or not provider:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.search(
                index=INDEX_CONNECTION,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"principal": principal}},
                                {"term": {"provider": provider}},
                                {"term": {"status": STATUS_PENDING_TOKEN}},
                                {"prefix": {"provider_account_id": PENDING_ACCOUNT_ID_PREFIX}},
                            ]
                        }
                    },
                    "sort": [{"created_at": {"order": "desc"}}],
                    "size": 1,
                },
            )
        except Exception as e:
            logging.warning(
                "ConnectionService.find_reusable_pending_connection %s/%s: %s",
                principal,
                provider,
                e,
            )
            return None
        hits = result.get("hits", {}).get("hits", [])
        return hits[0] if hits else None

    @staticmethod
    def find_connection_esid_by_legacy_assets(
        legacy_asset_index: str,
        legacy_asset_esids: list[str],
        prefer: str | None = None,
    ) -> str | None:
        """given a list of legacy asset ESIDs that already
        appear on a team's connector card (e.g. de_asset_fb_pages
        ESIDs linked from team_facebook_page_esids), return the
        ``connection_esid`` that owns them via ``de_connection_asset``.

        The legacy stamp is the only cross-index pointer
        we have from a team's linked-asset chain back to the connection
        that holds the OAuth token — `_resolve_fb_token_health` walks
        the asset side; this walks the connection side.

        Returns the first matching connection_esid whose connection is
        LIVE (exists and not revoked — see ``is_connection_live``), or
        None. A team with multiple linked legacy assets normally resolves
        to one connection (all the asset rows under a connection share its
        parent FK).

        we fetch several candidate rows (not just one) and skip
        any whose ``de_connection`` is missing or revoked. Previously this
        took the first matching ``de_connection_asset`` row with ``size:1``
        and returned its ``connection_esid`` unconditionally — so an orphan
        asset row (its connection deleted) or a revoked connection made the
        card show "Manage" for a dead connection, and a dead row could mask
        a live one. Skipping dead candidates fixes both.

        a team's linked assets can belong to MORE than one live
        connection (seen on prod: a team's web property re-authorised under one
        Google account, its app property still under another). The hits carry
        no ordering, so "first live" picked either one, and the card showed
        whichever owner ES returned first, locking the other principal out of
        the summary. ``prefer`` (the team's provider-specific stamp, e.g.
        ``ga4_connection_esid``, written by the most recent wizard run) wins
        when it is one of the live candidates; otherwise the first live
        candidate is returned as before.
        """
        if not legacy_asset_index or not legacy_asset_esids:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.search(
                index=INDEX_CONNECTION_ASSET,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"legacy_asset_index": legacy_asset_index}},
                                {"terms": {"legacy_asset_esid": list(legacy_asset_esids)}},
                            ]
                        }
                    },
                    # more than 1 so a dead candidate doesn't mask a
                    # live one; a team's linked assets resolve to a small set.
                    "size": 25,
                },
            )
        except Exception as e:
            logging.warning(
                "ConnectionService.find_connection_esid_by_legacy_assets %s: %s",
                legacy_asset_index,
                e,
            )
            return None
        hits = result.get("hits", {}).get("hits", [])
        prefer = (prefer or "").strip() or None
        seen: set[str] = set()
        first_live: str | None = None
        for hit in hits:
            esid = ((hit.get("_source") or {}).get("connection_esid") or "").strip()
            if not esid or esid in seen:
                continue
            seen.add(esid)
            # once a live candidate is in hand, only the preferred
            # esid is worth a liveness probe; the rest can't change the answer.
            if first_live is not None and esid != prefer:
                continue
            if ConnectionService.is_connection_live(esid):
                if prefer is None or esid == prefer:
                    return esid
                first_live = esid
        return first_live

    @staticmethod
    def revoked_connection_exists_for_legacy_asset(
        legacy_asset_index: str,
        legacy_asset_esid: str,
    ) -> bool:
        """has this legacy asset been migrated to a de_connection
        that the user has since *revoked*?

        Returns True only when BOTH are true:
          * a ``de_connection_asset`` row stamps this legacy asset, AND
          * the ``de_connection`` it points at exists and is ``revoked``.

        Returns False when no connection-asset row exists at all (the asset
        was never migrated — a pure-legacy account) so the importer can
        fail-open and keep using its legacy token. This is the inverse case
        ``find_connection_esid_by_legacy_assets`` can't express: that method
        skips revoked rows and returns None, which is indistinguishable from
        "never migrated". The revoke short-circuit needs to tell those apart.
        """
        if not legacy_asset_index or not legacy_asset_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            result = es.search(
                index=INDEX_CONNECTION_ASSET,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"legacy_asset_index": legacy_asset_index}},
                                {"term": {"legacy_asset_esid": legacy_asset_esid}},
                            ]
                        }
                    },
                    "size": 25,
                },
            )
        except Exception as e:
            # Fail-open: if we can't read the mapping, don't block the import.
            logging.warning(
                "ConnectionService.revoked_connection_exists_for_legacy_asset %s/%s: %s",
                legacy_asset_index,
                legacy_asset_esid,
                e,
            )
            return False
        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            return False  # never migrated — pure legacy account
        seen: set[str] = set()
        any_live = False
        for hit in hits:
            esid = ((hit.get("_source") or {}).get("connection_esid") or "").strip()
            if not esid or esid in seen:
                continue
            seen.add(esid)
            if ConnectionService.is_connection_live(esid):
                any_live = True
        # A row exists; revoke the import only when NONE of the mapped
        # connections are live (all revoked/deleted). A single live
        # connection means the account is still authorized.
        return not any_live

    # ─────────────────────────────────────────────────────────────────────────
    # Shadow-mode parity check (PR-C)
    #
    # The importer cutover deliberately keeps the legacy read-path as the
    # source of truth for at least one release cycle. While the cutover
    # is in shadow mode, the importer's existing token-fetch site calls
    # ``shadow_compare_legacy_token`` to look up the equivalent connection
    # by its ``legacy_asset_esid`` stamp and verify the decrypted token
    # matches. Any mismatch is logged but does not affect the import.
    #
    # The return shape is a plain dict so the caller can log it as a
    # single structured line and grep over a week of telemetry without
    # touching service internals.
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def shadow_compare_legacy_token(
        *,
        legacy_asset_index: str,
        legacy_asset_esid: str,
        legacy_plaintext_token: str,
    ) -> dict:
        """Verify the connection migrated from ``(legacy_asset_index,
        legacy_asset_esid)`` decrypts to the same plaintext as the
        legacy read.

        Returns one of:

        * ``{"status": "match"}`` — the connection exists and its
          decrypted token equals ``legacy_plaintext_token``. The new
          representation is trusted to replace the legacy one here.
        * ``{"status": "missing", ...}`` — no connection has this
          legacy-asset stamp. The backfill didn't cover this row, or it
          was skipped as an orphan. Importer falls back to legacy as
          usual.
        * ``{"status": "mismatch", ...}`` — connection exists but
          decrypts to a different value. **Loud** — something went
          wrong in the backfill (re-encryption, wrong principal,
          stale token). Investigate before the hard cutover.
        * ``{"status": "decrypt_error", ...}`` — connection exists but
          its token can't be decrypted. Indicates key drift or a
          corrupted doc.
        """
        if not legacy_asset_index or not legacy_asset_esid:
            return {"status": "missing", "reason": "no legacy stamp supplied"}
        es = get_elasticsearch_client()
        if es is None:
            return {"status": "missing", "reason": "ES unreachable"}
        try:
            result = es.search(
                index=INDEX_CONNECTION,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"legacy_asset_index": legacy_asset_index}},
                                {"term": {"legacy_asset_esid": legacy_asset_esid}},
                            ]
                        }
                    },
                    "size": 1,
                },
            )
        except Exception as e:
            return {"status": "missing", "reason": f"search failed: {e}"}
        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            return {
                "status": "missing",
                "legacy_asset_index": legacy_asset_index,
                "legacy_asset_esid": legacy_asset_esid,
            }
        hit = hits[0]
        connection_esid = hit["_id"]
        ciphertext = hit["_source"].get("oauth_token_enc")
        if not ciphertext:
            return {
                "status": "decrypt_error",
                "connection_esid": connection_esid,
                "reason": "no oauth_token_enc on connection doc",
            }
        try:
            plaintext = decrypt(ciphertext)
        except Exception as e:
            return {
                "status": "decrypt_error",
                "connection_esid": connection_esid,
                "reason": f"decrypt failed: {e}",
            }
        if plaintext == legacy_plaintext_token:
            return {"status": "match", "connection_esid": connection_esid}
        return {
            "status": "mismatch",
            "connection_esid": connection_esid,
            "legacy_asset_index": legacy_asset_index,
            "legacy_asset_esid": legacy_asset_esid,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Natural-key lookups (private)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_by_natural_key(es, principal: str, provider: str, provider_account_id: str) -> dict | None:
        # size was 1 with no sort, so when duplicate rows shared a natural key
        # ES result order silently picked which doc every upsert flow updated —
        # the token could land on one duplicate while reads hit the other, and
        # nothing ever reported the duplication. Now: newest-updated_at wins
        # (that's the doc the latest OAuth wrote), deterministically, and the
        # duplication is logged loudly. Returning None instead would make the
        # caller CREATE a third duplicate, which is why the pick stays.
        # match EVERY known spelling of the id ES-side: legacy rows
        # may hold the dashed Google Ads form, and a term on just the
        # canonical input would miss them — the caller would then CREATE the
        # very duplicate this class of bug is about.
        try:
            result = es.search(
                index=INDEX_CONNECTION,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"principal": principal}},
                                {"term": {"provider": provider}},
                                {
                                    "terms": {
                                        "provider_account_id": _provider_account_id_spellings(provider, provider_account_id)
                                    }
                                },
                            ]
                        }
                    },
                    "sort": [{"updated_at": {"order": "desc", "unmapped_type": "date"}}],
                    "size": 10,
                },
            )
        except Exception:
            return None
        hits = result.get("hits", {}).get("hits", [])
        if len(hits) > 1:
            logging.warning(
                "ConnectionService._find_by_natural_key: %d de_connection docs share "
                "natural key (principal=%s provider=%s provider_account_id=%s): ids=%s. "
                "Returning the most recently updated; the others are duplicates that "
                "should be investigated and removed.",
                len(hits),
                principal,
                provider,
                provider_account_id,
                [h.get("_id") for h in hits],
            )
        return hits[0] if hits else None

    @staticmethod
    def _find_asset_by_natural_key(es, connection_esid: str, asset_type: str, provider_asset_id: str) -> dict | None:
        # Same size:1 defect and same fix as _find_by_natural_key above.
        try:
            result = es.search(
                index=INDEX_CONNECTION_ASSET,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"connection_esid": connection_esid}},
                                {"term": {"asset_type": asset_type}},
                                {"term": {"provider_asset_id": provider_asset_id}},
                            ]
                        }
                    },
                    "sort": [{"updated_at": {"order": "desc", "unmapped_type": "date"}}],
                    "size": 10,
                },
            )
        except Exception:
            return None
        hits = result.get("hits", {}).get("hits", [])
        if len(hits) > 1:
            logging.warning(
                "ConnectionService._find_asset_by_natural_key: %d de_connection_asset docs "
                "share natural key (connection_esid=%s asset_type=%s provider_asset_id=%s): "
                "ids=%s. Returning the most recently updated.",
                len(hits),
                connection_esid,
                asset_type,
                provider_asset_id,
                [h.get("_id") for h in hits],
            )
        return hits[0] if hits else None

    @staticmethod
    def _lookup_principal(es, connection_esid: str) -> str | None:
        """Return the parent connection's ``principal`` for stamping on
        an asset row. None on missing connection / ES failure (caller
        treats that as an empty string).
        """
        if not connection_esid:
            return None
        try:
            result = es.get(index=INDEX_CONNECTION, id=connection_esid)
        except Exception:
            return None
        return (result.get("_source") or {}).get("principal") or None
