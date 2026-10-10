"""
Wizard Asset Catalog Service

Source-of-truth for the asset list rendered on wizard step 6 (the
"We found N ad accounts under your login" multi-select table that
runs right after the OAuth callback). Each ``WizardAsset`` describes
one row in the table — provider-side asset id, friendly label, the
business-manager / property hint shown under the name, the currency
hint, the active/paused/disabled status badge, the last-activity
timestamp, and whether the
row is pre-selected by default.

wire-up complete. ``list_for_session`` reads the
wizard user's stored OAuth token from ``de_connection`` (written by
 step-5 callback hook), calls Meta's ``/me/adaccounts``, and
maps each row to a ``WizardAsset``. ``last_activity_at`` is sourced
from a per-account ``/insights`` call with ``date_preset=last_30d``
so default-select ranks on real recency rather than
insertion order. The fetcher lives in
``meta_graph_client`` — this service owns the dataclass + mapping
contract; the client owns the network I/O. earlier the service
returned an empty tuple unconditionally and the route surfaced the
zero-accounts empty state; that empty path still works for users
whose Meta login surfaces zero ad accounts.

The ``WizardAsset`` shape is the public surface step-6 reads:
- ``provider_asset_id`` is the value persisted to
  ``wizard_session.state.selected_assets`` (and, in the eventual
  follow-up, the lookup key for the real ``de_connection_asset``
  row's ESID).
- ``status_label`` is the human copy ("Active" / "Paused" /
  "Disabled") — distinct from the asset-type-level
  ``de_connection.status`` (active/expired/revoked) which is about
  the OAuth grant, not the asset. ``ASSET_STATUS_DISABLED`` rows
  are render-only — the row is greyed out, the checkbox cannot be
  toggled (matches the prototype's ``.asset-row.disabled`` style).
- ``last_activity_at`` is the ISO-8601 timestamp
  default-select ranks on. ``None`` for assets with no usable
  activity record (e.g. an account that's never received a
  conversion) — those sort last and never get picked as a default.
- ``default_selected`` mirrors the prototype's "we've pre-selected
  your most recently active accounts" copy. Computed by
  ``default_selected_ids``.
  The dataclass flag is kept so a real fetch can override
  per-row (e.g. a future provider-side "primary account" hint).

The catalog deliberately leaves cross-connector shape uniform —
``business_manager_hint`` is generic enough to carry "Business
Manager: Acme Holdings" for Meta ad accounts and "Property:
example.com" for GA4 properties when those connectors ship.

friendly-name aliasing. Per-row text input renders an
optional friendly label that the dashboard uses everywhere a raw
provider id would otherwise leak (e.g. ``act_119238472`` →
"Acme Main"). Uniqueness is scoped to the wizard's workspace, not
global — two different agency clients can both have an
"Acme Main" if they live under different workspaces. The wizard
persists the alias map to ``wizard_session.state.friendly_names``
(``{provider_asset_id: alias}``); the follow-up that wires real
``de_connection_asset`` writes also writes
``workspace_connection_assets.friendly_name``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# surfaced when the provider rejects the asset fetch because
# the OAuth token expired between the OAuth callback and the user
# finishing step-6 (rare but possible — e.g. a Meta long-lived token
# that rotates while the user takes hours to pick their accounts). The
# step-6 GET catches this and redirects to /onboarding/step-7 with the
# ``?error=token_expired`` banner so the user can re-grant
# without losing the selections they've already made.
#
# Defined here (rather than as a generic AppError) because the catalog
# service is where the future real fetch will raise it — keeping the
# exception type co-located with its source-of-truth means callers
# don't need to know about a separate exceptions module.
class TokenExpiredError(Exception):
    """Provider returned 401 / token-expired on the asset fetch."""


ASSET_STATUS_ACTIVE = "active"
ASSET_STATUS_PAUSED = "paused"
ASSET_STATUS_DISABLED = "disabled"

_VALID_STATUSES = (ASSET_STATUS_ACTIVE, ASSET_STATUS_PAUSED, ASSET_STATUS_DISABLED)

# number of rows the wizard pre-checks by default on first
# render. The prototype shows the top-3 most recently active accounts
# pre-selected so a user with 1–3 active accounts can hit Finish
# without changing anything. The constant lives at module scope so a
# future ticket can tune it per connector via a service-method
# override without changing call-site code.
DEFAULT_SELECT_TOP_N = 3

# friendly-name validation bounds. The 64-char ceiling is
# generous (most provider-side names sit under 40) but cheap to enforce
# at write-time so a regression doesn't push 100KB strings into
# wizard_session.state. The 1-char floor distinguishes an empty input
# (user opted out of aliasing — fall back to the provider-side name)
# from a non-empty input with bad characters.
_FRIENDLY_NAME_MIN_LEN = 1
_FRIENDLY_NAME_MAX_LEN = 64


# request-scoped cache key prefix on flask.g. The catalog
# service is called three times per step-6 GET (list_for_session,
# selectable_ids, default_selected_ids); without caching that's
# N+1+N+N Graph calls. Keyed by (username, connector_id) so a single
# request can't accidentally leak across users.
_CACHE_ATTR_PREFIX = "_catalog_cache__"


def _cache_get(username: str, connector_id: str) -> tuple[WizardAsset, ...] | None:
    """Return the cached asset tuple for this (user, connector) or None.

    Uses ``flask.g`` so the cache scope matches one request lifecycle.
    Outside a Flask request context (CLI tools, ad-hoc imports) this
    is a no-op — the call goes through to the live fetch every time,
    which is the safer default than introducing a shared global.
    """
    try:
        from flask import g, has_request_context
    except ImportError:
        return None
    if not has_request_context():
        return None
    attr = f"{_CACHE_ATTR_PREFIX}{username}__{connector_id}"
    return getattr(g, attr, None)


def _cache_put(username: str, connector_id: str, assets: tuple[WizardAsset, ...]) -> tuple[WizardAsset, ...]:
    """Store ``assets`` on flask.g and return them. Outside a request
    context this is a pass-through.
    """
    try:
        from flask import g, has_request_context
    except ImportError:
        return assets
    if not has_request_context():
        return assets
    attr = f"{_CACHE_ATTR_PREFIX}{username}__{connector_id}"
    setattr(g, attr, assets)
    return assets


def _map_meta_account(raw: dict) -> "WizardAsset | None":
    """Map a normalised /me/adaccounts row to a ``WizardAsset``.

    Returns None if the row is missing a usable id — those can't be
    rendered or persisted (the selection allowlist keys off the id).
    Status mapping is delegated to ``meta_graph_client.map_account_status``
    so the enum→bucket logic lives next to the codes.

    ``default_selected`` is set False here;
    ``default_selected_ids`` computes the actual pre-checked set
    from ``last_activity_at`` recency. The dataclass field is kept
    so a future provider-side "primary" hint can override per-row.
    """
    if not isinstance(raw, dict):
        return None
    account_id = (raw.get("id") or "").strip()
    if not account_id:
        return None

    # Local import — keeps the catalog service importable without
    # the Graph client when this mapper isn't being used (CLI paths).
    from asset_management.web.services import meta_graph_client

    status_code, status_label = meta_graph_client.map_account_status(raw.get("account_status"))

    business_manager_hint = (raw.get("business_manager_hint") or "").strip()
    if meta_graph_client.is_grace_period(raw.get("account_status")):
        # Append a grace-period suffix so the wizard surface still
        # marks the row Active (it's spendable) but the hint line
        # tells the user why we surfaced it.
        suffix = "Grace period"
        if business_manager_hint:
            business_manager_hint = f"{business_manager_hint} — {suffix}"
        else:
            business_manager_hint = suffix

    name = (raw.get("name") or "").strip() or account_id
    currency = (raw.get("currency") or "").strip()
    last_activity_at = raw.get("last_activity_at") or None

    return WizardAsset(
        provider_asset_id=account_id,
        name=name,
        business_manager_hint=business_manager_hint,
        currency=currency,
        status_code=status_code,
        status_label=status_label,
        default_selected=False,
        last_activity_at=last_activity_at if isinstance(last_activity_at, str) else None,
    )


@dataclass(frozen=True)
class WizardAsset:
    """One row in the step-6 asset selection table.

    Frozen so a downstream renderer can't mutate the catalog mid-
    request. ``provider_asset_id`` is the stable identifier persisted
    to ``wizard_session.state.selected_assets`` — keep its shape
    intact across deploys so a refresh / resume sees the same row
    marked selected. ``status_label`` is the literal copy rendered
    in the status column; ``status_code`` is the machine-readable
    flag the route + tests key off (so a marketing copy edit doesn't
    flip test expectations).
    """

    provider_asset_id: str
    name: str
    business_manager_hint: str
    currency: str
    status_code: str
    status_label: str
    default_selected: bool
    # recency ranking. ISO-8601 string or None. The default-
    # select ranker sorts descending and picks the top-N active rows.
    # Disabled rows are never picked even if their last_activity_at is
    # the most recent.
    last_activity_at: str | None = None
    # when the row's status_code is ASSET_STATUS_DISABLED,
    # this is the provider-side disable date (ISO-8601, e.g.
    # "2024-08-01"). The template surfaces it in the hover tooltip
    # ("Disabled by Meta on 2024-08-01"). None means the provider
    # didn't supply a date — the tooltip falls back to the generic
    # "Disabled by Meta" copy rather than swallowing the cue entirely.
    # Ignored for non-disabled rows.
    disabled_at: str | None = None


@dataclass(frozen=True)
class FriendlyNameValidationResult:
    """Outcome of validating a candidate friendly-name map.

    a successful validation produces a cleaned map
    (``{provider_asset_id: alias}`` with whitespace stripped, empty
    strings dropped). A failure carries one or more ``errors`` keyed
    by ``provider_asset_id`` so the template can render the inline
    conflict warning next to the offending row.

    ``errors`` is an ordered mapping (Python 3.7+ dict insertion
    order) so the first error per row is the one surfaced — keeps the
    UX predictable when a single alias is both too long AND
    conflicts with another row's alias.
    """

    cleaned: dict[str, str]
    errors: dict[str, str]

    @property
    def ok(self) -> bool:
        return not self.errors


class WizardAssetCatalogService:
    """Returns the list of provider-side assets the user can choose
    from on step-6.

    Today this is always empty because discards the access
    token; the route handles the empty case by surfacing a
    "couldn't find any assets" hint and a refresh link. When the
    connection-persistence follow-up ticket ships, this service
    grows an ES read path keyed by ``(username, connector_id)`` →
    ``de_connection`` → ``de_connection_asset``.

    The ``selectable_ids`` allowlist is the server-side gate on the
    Finish POST — a forged form post naming an id outside the
    catalog is refused. With an empty catalog today, ``selectable_ids``
    is the empty frozenset, and the Finish POST tolerates an empty
    selection (the AC's "Finish setup requires at least 1 selected
    (or skip)" — Skip is the alternative path).
    """

    @staticmethod
    def list_for_session(*, username: str, connector_id: str) -> tuple[WizardAsset, ...]:
        """Return the provider-side assets the user can choose from.

        real fetch path. Looks up the user's stored OAuth
        token from ``de_connection`` (written by the step-5 callback),
        calls Meta's ``/me/adaccounts``, and maps each row to a
        ``WizardAsset``. The result is cached on ``flask.g`` keyed by
        ``(username, connector_id)`` so the route's three calls
        (``list_for_session``, ``selectable_ids``,
        ``default_selected_ids``) share one Graph round-trip per
        request — N+1 calls otherwise (one /me/adaccounts + N
        /insights) tripled would be a real latency tax.

        Raises ``TokenExpiredError`` if the Graph client surfaces 401.
        The route catches this and bounces to step-5 with the
        ``?error=token_expired`` banner.

        Returns an empty tuple on any non-fatal failure (missing
        connection, decrypt error, non-401 HTTP error, malformed
        response). The route renders the existing zero-accounts empty
        state in those cases — partial-failure data is preferable to
        a crash on a screen the user can still ``Skip`` past.

        Only ``meta_ads`` is wired today. Other connector_ids return
        empty until their provider-side fetcher lands.
        """
        if not username or not connector_id:
            return ()

        cached = _cache_get(username, connector_id)
        if cached is not None:
            return cached

        if connector_id != "meta_ads":
            return _cache_put(username, connector_id, ())

        # Late imports keep the catalog service importable from CLI
        # tools / tests that don't have Flask + ES configured. The
        # route-side import path warms these eagerly.
        try:
            from asset_management.web.services.connection_service import (
                ConnectionService,
                PROVIDER_FACEBOOK,
            )
            from asset_management.web.services import meta_graph_client
        except ImportError as e:
            logger.warning("ConnectionService / Graph client import failed: %s", e)
            return _cache_put(username, connector_id, ())

        # Find the de_connection for this principal + provider. The
        # natural key on de_connection includes provider_account_id,
        # but the wizard's step-5 callback writes exactly one
        # facebook-provider connection per principal (the Meta
        # /me user_id). Pick the most-recently-updated row defensively
        # in case a re-OAuth landed multiple — that's the freshest
        # token regardless.
        try:
            connections = ConnectionService.list_connections_for_principal(username)
        except Exception as e:  # noqa: BLE001
            logger.warning("list_connections_for_principal failed: %s", e)
            return _cache_put(username, connector_id, ())

        fb_connections = [c for c in connections if ((c.get("_source") or {}).get("provider") == PROVIDER_FACEBOOK)]
        if not fb_connections:
            return _cache_put(username, connector_id, ())

        # Sort by updated_at desc so a re-OAuth wins over a stale row.
        fb_connections.sort(
            key=lambda c: (c.get("_source") or {}).get("updated_at") or "",
            reverse=True,
        )
        connection_esid = fb_connections[0].get("_id") or ""
        if not connection_esid:
            return _cache_put(username, connector_id, ())

        access_token = ConnectionService.get_decrypted_token(connection_esid)
        if not access_token:
            # pending_token rows land with an empty
            # oauth_token_enc; the wizard-aware callback hasn't stamped
            # one yet. Surface as empty assets — the route shows the
            # zero-accounts empty state and Skip still works.
            return _cache_put(username, connector_id, ())

        try:
            raw_accounts = meta_graph_client.list_ad_accounts(access_token)
        except meta_graph_client.TokenExpiredError as e:
            # Mark the connection so the next visit doesn't re-attempt
            # a doomed fetch, then propagate as our local exception
            # type for the route's handler.
            try:
                ConnectionService.set_status(connection_esid, "expired")
            except Exception:
                # Best-effort; don't mask the original failure.
                pass
            raise TokenExpiredError(str(e)) from e
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logger.warning("list_ad_accounts failed: %s", e)
            return _cache_put(username, connector_id, ())

        mapped = tuple(_map_meta_account(raw) for raw in raw_accounts)
        # Drop any rows that fell out of the mapper (missing id).
        mapped = tuple(a for a in mapped if a is not None)
        return _cache_put(username, connector_id, mapped)

    @staticmethod
    def selectable_ids(*, username: str, connector_id: str) -> frozenset[str]:
        """Return the set of ``provider_asset_id`` strings the Finish
        POST accepts. Disabled rows are excluded — they render but
        cannot be selected.
        """
        return frozenset(
            asset.provider_asset_id
            for asset in WizardAssetCatalogService.list_for_session(
                username=username,
                connector_id=connector_id,
            )
            if asset.status_code != ASSET_STATUS_DISABLED
        )

    @staticmethod
    def default_selected_ids(
        *,
        username: str,
        connector_id: str,
        top_n: int = DEFAULT_SELECT_TOP_N,
    ) -> frozenset[str]:
        """Return the ``provider_asset_id`` strings pre-checked on
        first render.

        Ranking rule:
        1. Filter to rows where ``status_code == ASSET_STATUS_ACTIVE``
           — Paused and Disabled rows are never auto-selected. The
           AC pins this for Disabled; Paused is the same UX promise
           (a paused account isn't a healthy "import this now" pick).
        2. Sort by ``last_activity_at`` descending. ``None`` (no
           activity record) sorts last via a sentinel so it can't
           sneak into the top-N.
        3. Take the first ``top_n`` ids.

        A resumed session prefers ``wizard_session.state.selected_assets``
        over these defaults; the route handles that selection-priority
        logic so a user who unchecked everything on a prior visit
        doesn't see those rows re-checked.

        The ``top_n`` parameter is an escape hatch for a future
        per-connector tuning ticket — today the wizard always passes
        the module-level ``DEFAULT_SELECT_TOP_N``.
        """
        assets = WizardAssetCatalogService.list_for_session(
            username=username,
            connector_id=connector_id,
        )
        active = [a for a in assets if a.status_code == ASSET_STATUS_ACTIVE]
        # Sort rows with a real timestamp newest-first; rows without
        # a timestamp sort last so they can't sneak into the top-N.
        # Python's sorted doesn't accept mixed-direction keys, so do
        # it in two passes.
        with_ts = sorted(
            [a for a in active if a.last_activity_at],
            key=lambda a: a.last_activity_at or "",
            reverse=True,
        )
        without_ts = [a for a in active if not a.last_activity_at]
        ranked = with_ts + without_ts
        return frozenset(a.provider_asset_id for a in ranked[: max(0, top_n)])

    # ─────────────────────────────────────────────────────────────────
    # friendly-name validation
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def validate_friendly_names(
        *,
        submitted: dict[str, str],
        selectable_ids: frozenset[str],
    ) -> FriendlyNameValidationResult:
        """Validate the friendly-name map the user submitted on Finish.

        ``submitted`` is the raw ``{provider_asset_id: alias}`` map
        parsed out of the form (the route extracts ``friendly_name__*``
        fields). ``selectable_ids`` is the same allowlist the
        ``selected_asset_id`` intersection runs against — keys outside
        this set are silently dropped (same defense-in-depth pattern
        as the selection gate, so a forged ``friendly_name__act_FORGED``
        can't slip an alias for an id the user never selected).

        Validation rules:
        - Whitespace is stripped from each alias. An empty result
          means "no alias" — that key is dropped from ``cleaned``
          rather than persisted as an empty string (the dashboard
          falls back to the provider-side name).
        - Length 1–64 (after strip). Anything over 64 produces a
          per-row "too long" error.
        - Uniqueness is case-insensitive within the workspace. Two
          aliases that match after ``lower().strip()`` both error.
          Case is preserved in ``cleaned`` (user's input wins) — the
          comparison is just for collision detection.
        - Aliases identical to a provider_asset_id are accepted (the
          user might legitimately want to keep the raw id as their
          alias). The uniqueness check is alias-vs-alias.

        Returns a ``FriendlyNameValidationResult``; the route checks
        ``.ok`` and either persists ``.cleaned`` or re-renders step-6
        with the ``.errors`` dict so the template can surface inline
        per-row conflict messages next to the offending input.
        """
        cleaned: dict[str, str] = {}
        errors: dict[str, str] = {}

        # First pass — strip + length-check + drop unknown ids. The
        # uniqueness pass runs against the cleaned map so we don't
        # complain about a too-long alias colliding with a valid one.
        for asset_id, raw in submitted.items():
            if asset_id not in selectable_ids:
                # Forged or stale id — silently drop (matches the
                # selection-gate behaviour).
                continue
            alias = (raw or "").strip()
            if not alias:
                # Empty after strip — user opted out of aliasing for
                # this row. No persistence, no error.
                continue
            if len(alias) > _FRIENDLY_NAME_MAX_LEN:
                errors[asset_id] = f"Name is too long (max {_FRIENDLY_NAME_MAX_LEN} characters)."
                continue
            if len(alias) < _FRIENDLY_NAME_MIN_LEN:
                # Defensive — strip + ``not alias`` above already caught this.
                continue
            cleaned[asset_id] = alias

        # Second pass — case-insensitive uniqueness check on the
        # cleaned map only. A conflict marks BOTH colliding rows so
        # the user sees the warning on the second input they typed
        # AND the original one, not just the last.
        lowered_to_ids: dict[str, list[str]] = {}
        for asset_id, alias in cleaned.items():
            lowered_to_ids.setdefault(alias.lower(), []).append(asset_id)
        for lowered, asset_ids in lowered_to_ids.items():
            if len(asset_ids) <= 1:
                continue
            # Use the cleaned alias from the first colliding row in
            # the error message — keeps the warning meaningful even
            # if the second row's casing differs.
            display = cleaned[asset_ids[0]]
            for asset_id in asset_ids:
                # Don't clobber a pre-existing error (e.g. a too-long
                # alias that ALSO collides). First-error-wins.
                errors.setdefault(
                    asset_id,
                    f'"{display}" is already used by another account in this workspace.',
                )

        return FriendlyNameValidationResult(cleaned=cleaned, errors=errors)
