"""
Connector Catalog Service

Source-of-truth for the connector tile grid shown on wizard step 4.

Each ``ConnectorTile`` describes one card in the Funnel.io-style grid:
its stable id (persisted to wizard_session.state.selected_connector_id
when the user clicks Continue), the label / sub-label rendered on the
tile, the logo glyph + background gradient, the "Top" / "Soon" badge,
the section the tile belongs to ("Most popular" or "All connectors"),
and whether the tile is selectable.

The catalog is hard-coded here rather than read from ES so step-4
renders without a live cluster (smoke-test surface) and so a new
connector can be added by editing this file alone. When the dashboard
checklist (post-wizard) grows the same tile grid, both surfaces import
``CONNECTORS`` from here — no need to rebuild the catalog twice.

The selectable / unselectable split applies scope trim: the
"Most popular" featured row is **three** tiles — Meta, Google Ads,
Google Analytics 4. TikTok dropped from the featured row (
scope review) and now lives in the "All connectors" section alongside
LinkedIn, X, HubSpot, Shopify, etc.

Meta, Google Ads, and GA4 are all fully selectable on first launch —
selecting any of them advances the wizard. Meta runs a per-user OAuth
roundtrip; Google Ads and GA4 are tokenless
(cluster-managed credentials + per-team account mapping) and dispatch
through ``_finalize_tokenless_connector``. The ``placeholder=True``
state (a clickable tile that routes to an "OAuth wizard pending —
please contact support" banner instead of advancing) is still
supported by the catalog accessors but is currently unused — no tile
sets it, so ``placeholder_ids()`` is empty in practice. It remains as
the documented UX for any future connector that ships with a partial
OAuth integration. The server still refuses a Continue POST that
targets a placeholder OR coming-soon id (so a hand-crafted POST can't
slip a non-selectable selection past the UX gate).

Snapchat / Pinterest / Reddit stay ``coming_soon=True`` with the
prototype's "Soon" badge — visibly disabled, no click affordance at
all (distinct UX from placeholder, which IS clickable but routes to
the notify-me banner).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lib.tiktok_constants import tiktok_enabled

SECTION_POPULAR = "popular"
SECTION_ALL = "all"

BADGE_TOP = "top"
BADGE_SOON = "soon"


@dataclass(frozen=True)
class ConnectorTile:
    """One tile in the step-4 grid.

    Frozen so a downstream renderer can't mutate the catalog mid-request.
    ``connector_id`` is the value persisted to wizard_session — keep it
    stable across deploys (the dashboard-checklist resume path reads it
    back).

    Logo rendering: if ``icon_file`` is
    set, the template renders an <img> sourced from
    ``/static/dashboard_builder/icons/<icon_file>`` and ignores
    logo_glyph + logo_gradient. The gradient+glyph fallback (with a
    1–2 char label on a coloured square) is retained for any future
    tile that ships before its real SVG lands.

    ``coming_soon`` and ``placeholder`` are three-state UX flags that
    are mutually exclusive at the data layer (a tile is either fully
    working, placeholder, OR coming-soon — never two at once):

    * ``coming_soon=True`` — tile renders visibly disabled with a "Soon"
      badge. No click affordance.
    * ``placeholder=True`` — tile renders clickable but selecting it
      surfaces the "OAuth wizard pending" inline banner with a notify-me
      affordance. Wizard does NOT advance. (Google Ads, GA4.)
    * Both False — tile is fully working; selecting it persists the id
      and advances the wizard.
    """

    connector_id: str
    name: str
    sub_label: str
    logo_glyph: str
    logo_gradient: str
    section: str
    badge: str | None
    coming_soon: bool
    placeholder: bool = False
    # follow-up (2026-05-20) — SVG icon filename under
    # bin/python/asset_management/web/static/dashboard_builder/icons/.
    # Empty string means "use the legacy gradient+glyph rendering."
    icon_file: str = ""
    # follow-up (2026-05-26) — controls whether the tile appears
    # on the onboarding wizard's step-4 catalog. /assets/data-connectors/
    # always renders the full catalog. Default True keeps existing tiles
    # unchanged. Set to False for connectors that need post-onboarding
    # setup (e.g. Echo's per-tenant subdomain+api_key wizard isn't
    # onboarding-shaped, but the connector still belongs on the
    # data-connectors page).
    onboarding_eligible: bool = True
    # (2026-06-06) — hides the tile from BOTH surfaces (onboarding
    # wizard step-4 AND /assets/data-connectors/). Distinct from
    # ``onboarding_eligible`` (which only suppresses the onboarding
    # surface). The tile stays in ``CONNECTORS`` so ``get()`` still
    # resolves its display name for audit logging and the notify-list
    # allowlist, but every rendering / id-set accessor filters it out.
    # Default False keeps existing tiles unchanged.
    hidden: bool = False
    # names a deployment switch (a key of ``_FEATURE_GATES``) that
    # must be on for the tile to show. While the switch is off the tile is
    # treated exactly like ``hidden=True``: gone from both surfaces and from
    # every id set, still resolvable through ``get()``. Empty = no switch.
    feature_gate: str = ""


# switch name → reader. Read per call, so flipping the environment
# variable and recreating the container is all it takes.
_FEATURE_GATES: dict[str, Callable[[], bool]] = {
    "tiktok": tiktok_enabled,
}


def _shown(tile: ConnectorTile) -> bool:
    """Whether a tile renders anywhere: not hidden, and its switch (if any) is on."""
    if tile.hidden:
        return False
    if not tile.feature_gate:
        return True
    return _FEATURE_GATES[tile.feature_gate]()


# follow-up (2026-05-20) — connector catalog overhaul.
# Real SVG icons replace the gradient+glyph placeholders for every tile
# that has one. Tiles without an icon are dropped from the catalog
# (Pinterest); tiles that have an icon but no backend integration are
# kept as ``coming_soon=True``.
#
# Active tiles ship with backend integrations today:
#   * meta_ads — Meta OAuth + fb_insights importer
# * google_ads — tokenless dispatch
# * google_analytics_4 — tokenless dispatch
#   * echo — per-tenant subdomain+API-key wizard (catalog B); data-
#     connectors page only (onboarding_eligible=False)
# * youtube — OAuth + channel-multiselect wizard; data-
#     connectors page only (onboarding_eligible=False), superuser-gated
# wireable (Google OAuth verification) lands
#
# (2026-05-25) — Echo dropped from the onboarding catalog. Two
# "Echo" connectors coexisted (tokenless vs the legacy per-team
# echo_tenants wizard); reconciliation picked the per-tenant model
# (subdomain + API key) because the Echo importer reads
# de_asset_echo_tenants, not the tokenless de_connection rows.
#
# follow-up (2026-05-26) — Echo tile RE-ADDED with
# onboarding_eligible=False so it stays off the onboarding wizard
# (where subdomain+api_key entry is a UX regression) but appears on
# /assets/data-connectors/ "Connect a new source" so new teams can
# set up Echo without an operator. Tile clicks launch the existing
# WIZARDS.echo modal (subdomain + api_key form). See DECISIONS.md
# 2026-05-25 + the 2026-05-26 follow-up entry.
#
# Coming-soon tiles have brand icons but no backend integration yet:
#   tiktok, linkedin, x, hubspot, shopify, snapchat, reddit, chatwoot,
#   disciple_tools.
CONNECTORS: tuple[ConnectorTile, ...] = (
    ConnectorTile(
        # NOTE on the `_ads` suffix: the connector_id historically named
        # the paid (ad-insights) product, but this connector pulls BOTH
        # paid AND organic data — Facebook/IG ad insights *and* page
        # insights, IG account insights, and page posts. The display
        # label was renamed "Meta Ads" → "Meta" (2026-06-15) to reflect
        # that scope. The id stays "meta_ads" on purpose: it is a
        # persisted key (report schedules, backfill-progress docs, the
        # notify-list index, _TILE_TO_WIZARD_KEY / _status_key routing),
        # so renaming it would be a multi-index ES migration, not a code
        # change. Don't "fix" the suffix without that migration. Same
        # caveat applies to `google_ads`.
        connector_id="meta_ads",
        name="Meta",
        sub_label="Facebook + Instagram",
        logo_glyph="f",
        logo_gradient="linear-gradient(135deg, #1877F2, #0866FF)",
        icon_file="meta.svg",
        section=SECTION_POPULAR,
        # follow-up (2026-05-21): "Top" badge dropped at
        # the team's request. The badge was decorative — it signalled
        # "most-used in our user research" but added visual weight to
        # one tile in the now-merged grid (popular/all sections merged
        # in the prior step-6 polish commit). Meta Ads still renders
        # first in the grid by virtue of being the first popular tile
        # in CONNECTORS.
        badge=None,
        coming_soon=False,
    ),
    # Google Ads flipped from placeholder=True to a fully
    # selectable tokenless connector. The system's Google Ads importer
    # uses a cluster-wide OAuth refresh token (configured in
    # /app/reporting_configs/google-ads.yaml); per-team customer
    # mapping lives in de_asset_teams.google_ads_customer_esid. Step-4
    # POST dispatches Google Ads through _finalize_tokenless_connector
    # the same way Echo does, resolving the team's configured
    # google_ads_customer_id and writing one connection_asset row.
    ConnectorTile(
        connector_id="google_ads",
        name="Google Ads",
        sub_label="Search · Display · YouTube",
        logo_glyph="G",
        logo_gradient="linear-gradient(135deg, #FBBC04, #EA4335)",
        icon_file="google-ads.svg",
        section=SECTION_POPULAR,
        badge=None,
        coming_soon=False,
    ),
    # GA4 flipped from placeholder=True to a fully selectable
    # tokenless connector (same architecture as Google Ads /):
    # cluster-wide credentials in /app/reporting_configs/google-analytics.yaml,
    # per-team property mapping in de_asset_teams.team_ga4_property_esids
    # (multi-select). Step-4 POST dispatches through
    # _finalize_tokenless_connector, which resolves the team's property
    # list and writes one connection_asset per property.
    ConnectorTile(
        connector_id="google_analytics_4",
        name="Google Analytics 4",
        sub_label="Site & funnel data",
        logo_glyph="A",
        logo_gradient="linear-gradient(135deg, #F9AB00, #E37400)",
        icon_file="google-analytics-3-logo.svg",
        section=SECTION_POPULAR,
        badge=None,
        coming_soon=False,
    ),
    # (2026-05-27): YouTube flipped from coming_soon → working
    # connector. The backend stack already shipped (importer, OAuth
    # server, per-channel tokens, asset model,
    # dashboards); this ticket wires the tile to a setup wizard
    # on /assets/data-connectors/ (Account → OAuth → pick channels → link
    # team). onboarding_eligible=False keeps it off the signup wizard (the
    # OAuth-popup + channel-multiselect flow is a wizard-regression
    # mid-onboarding), same posture as Echo.
    #
    # Positioned right after GA4 (at the team's request, 2026-05-27) so the
    # working YouTube tile sits with the other live connectors near the top
    # of /assets/data-connectors/ rather than at the end of the grid.
    # section stays SECTION_ALL (NOT promoted to SECTION_POPULAR): the
    # onboarding "Most popular" featured row is pinned to exactly
    # meta_ads / google_ads / google_analytics_4 and YouTube
    # is onboarding_eligible=False, so it must not enter that row. On
    # /assets/data-connectors/ (all_tiles(), unfiltered catalog order) it
    # now renders immediately after GA4.
    #
    # Wireable for any mc_data_connectors user. The original
    # superuser-only gate was lifted 2026-05-27 at the team's request (see
    # routes.index + DECISIONS.md). (Google OAuth verification) is
    # still unfinished — the unverified client caps at ~100 grants and
    # refresh tokens expire on a rolling 7-day window — so a channel onboarded
    # here may silently disconnect within a week until verification lands.
    # That's now an accepted, broadly-applicable caveat rather than a gate.
    ConnectorTile(
        connector_id="youtube",
        name="YouTube",
        sub_label="Channel analytics",
        logo_glyph="YT",
        logo_gradient="#FF0000",
        icon_file="youtube.svg",
        section=SECTION_ALL,
        badge=None,
        coming_soon=False,
        # follow-up (2026-05-27): onboarding_eligible flipped
        # False → True at the team's request so YouTube also shows in the
        # onboarding wizard step-4 (onboarding_step6_connector.html),
        # matching the Data Connectors page. The wizard renders a flat
        # merged grid (popular_tiles + all_section_tiles), so
        # YouTube lands right after GA4 on both surfaces — no section change
        # needed (stays SECTION_ALL). NOTE: this re-exposes new signups to
        # the risk (unverified client → 7-day token expiry); accepted
        # because there are no YouTube users yet will verify
        # before real usage. Echo stays onboarding_eligible=False.
        onboarding_eligible=True,
    ),
    # follow-up (2026-05-26) — Echo tile re-added with
    # onboarding_eligible=False. Stays off the onboarding wizard
    # (subdomain+api_key entry is a UX regression mid-wizard) but
    # appears on /assets/data-connectors/ so new teams can set up
    # Echo without an operator. Tile click launches the existing
    # legacy modal (WIZARDS.echo: subdomain + api_key form). The
    # tokenless dispatch stays decommissioned —
    # _NO_OAUTH_CONNECTOR_IDS no longer contains "echo".
    ConnectorTile(
        connector_id="echo",
        name="Echo",
        sub_label="Conversations & outcomes",
        logo_glyph="E",
        logo_gradient="linear-gradient(135deg, #6366F1, #8B5CF6)",
        icon_file="echo.svg",
        section=SECTION_ALL,
        badge=None,
        coming_soon=False,
        onboarding_eligible=False,
    ),
    # Chatwoot — un-gated 2026-06-05. It was a mistake to ship this tile
    # coming_soon: the full backend already exists (configure_chatwoot +
    # /api/wizard/chatwoot/configure route, the chatwoot importer suite, and
    # the daily chatwoot_import cron). Positioned immediately after Echo (the
    # catalog renders in tuple order via all_tiles(), so this puts Chatwoot
    # directly to Echo's right in the 4-col grid). onboarding_eligible=False
    # keeps it off the signup wizard — the account-id + API-token form is a
    # mid-onboarding UX regression, same posture as Echo. Tile click launches
    # the existing WIZARDS.chatwoot modal via _TILE_TO_WIZARD_KEY.
    ConnectorTile(
        connector_id="chatwoot",
        name="Chatwoot",
        sub_label="Conversations & support",
        logo_glyph="CW",
        logo_gradient="#1F93FF",
        icon_file="chatwoot.svg",
        section=SECTION_ALL,
        badge=None,
        coming_soon=False,
        onboarding_eligible=False,
    ),
    # (2026-06-11): Disciple Tools un-gated coming_soon → working
    # connector. The backend shipped with the pull importer
    # (de_connection Site-to-Site Link credential + dt_site asset +
    # {team_esid}__disciple_tools writer); the tile now launches the
    # disciple_tools setup wizard (DT domain + Site Link token form).
    # onboarding_eligible=False keeps it off the signup wizard — a
    # WordPress-admin token handoff is a mid-onboarding UX regression,
    # same posture as Echo / Chatwoot.
    ConnectorTile(
        connector_id="disciple_tools",
        name="Disciple Tools",
        sub_label="Contacts & groups",
        logo_glyph="DT",
        logo_gradient="#3F729B",
        icon_file="dt-caret.svg",
        section=SECTION_ALL,
        badge=None,
        coming_soon=False,
        onboarding_eligible=False,
    ),
    # Phase 3 (2026-08-05): Bitly link-clicks connector. Form-only
    # wizard (API token + tag filter) writing an encrypted de_connection +
    # bitly_account asset row; importer attributes links to teams by Bitly
    # tags. onboarding_eligible=False is hard policy — new connectors never
    # appear in the onboarding wizard (same posture as Echo / Chatwoot / DT).
    ConnectorTile(
        connector_id="bitly",
        name="Bitly",
        sub_label="Link clicks",
        logo_glyph="b",
        logo_gradient="#EE6123",
        icon_file="bitly.svg",
        section=SECTION_ALL,
        badge=None,
        coming_soon=False,
        onboarding_eligible=False,
    ),
    # (2026-09-26): TikTok organic — Login Kit OAuth + Display API
    # (account + per-video counts). A separate id from the coming-soon
    # ``tiktok_ads`` tile below on purpose: ads need a TikTok API for Business
    # app, which does not exist yet, and connector ids are costly to rename
    # once teams link them. onboarding_eligible=False is the new-connector
    # policy.
    ConnectorTile(
        connector_id="tiktok",
        name="TikTok",
        sub_label="Organic",
        logo_glyph="TT",
        logo_gradient="linear-gradient(135deg, #000, #25F4EE)",
        icon_file="tiktok-icon-white-1-logo.svg",
        section=SECTION_ALL,
        badge=None,
        coming_soon=False,
        onboarding_eligible=False,
        feature_gate="tiktok",
    ),
    # ─── Coming Soon ─── icons live; backend integrations don't. ───
    # follow-up (2026-05-20): TikTok / LinkedIn / X / HubSpot /
    # Shopify flipped from active → coming_soon. Their previous "active"
    # state was a no-op anyway (no importers existed); this just makes
    # the catalog truthful.
    # (2026-06-06): TikTok sub-label cleared (was "Notify me →") —
    # the "Soon" badge already signals coming-soon, so a sub-label is
    # redundant. LinkedIn / HubSpot / Shopify / Snapchat / Reddit below
    # set hidden=True (removed from both surfaces).
    ConnectorTile(
        connector_id="tiktok_ads",
        name="TikTok Ads",
        sub_label="",
        logo_glyph="TT",
        logo_gradient="linear-gradient(135deg, #000, #25F4EE)",
        icon_file="tiktok-icon-white-1-logo.svg",
        section=SECTION_ALL,
        badge=BADGE_SOON,
        coming_soon=True,
    ),
    # (2026-06-06): LinkedIn / HubSpot / Shopify / Snapchat /
    # Reddit hidden from both surfaces (hidden=True). They keep their
    # catalog entries (so get() still resolves names) but never render
    # and never enter any id set. The remaining visible coming-soon
    # tiles (TikTok / X / Disciple Tools) have their sub-label cleared —
    # the notify-me affordance was removed and the "Soon"
    # badge already signals the state, so the old "Notify me →" sub-label
    # was both stale and redundant.
    ConnectorTile(
        connector_id="linkedin_ads",
        name="LinkedIn Ads",
        sub_label="Notify me →",
        logo_glyph="in",
        logo_gradient="#0A66C2",
        icon_file="linkedin-logo.svg",
        section=SECTION_ALL,
        badge=BADGE_SOON,
        coming_soon=True,
        hidden=True,
    ),
    ConnectorTile(
        connector_id="x_ads",
        name="X / Twitter Ads",
        sub_label="",
        logo_glyph="𝕏",
        logo_gradient="#000",
        icon_file="x-logo.svg",
        section=SECTION_ALL,
        badge=BADGE_SOON,
        coming_soon=True,
    ),
    ConnectorTile(
        connector_id="hubspot",
        name="HubSpot",
        sub_label="Notify me →",
        logo_glyph="HS",
        logo_gradient="#FF7A59",
        icon_file="hubspot.svg",
        section=SECTION_ALL,
        badge=BADGE_SOON,
        coming_soon=True,
        hidden=True,
    ),
    ConnectorTile(
        connector_id="shopify",
        name="Shopify",
        sub_label="Notify me →",
        logo_glyph="S",
        logo_gradient="#5E8E3E",
        icon_file="shopify-color.svg",
        section=SECTION_ALL,
        badge=BADGE_SOON,
        coming_soon=True,
        hidden=True,
    ),
    ConnectorTile(
        connector_id="snapchat_ads",
        name="Snapchat Ads",
        sub_label="Notify me →",
        logo_glyph="SC",
        logo_gradient="#FFFC00",
        icon_file="snapchat-logo.svg",
        section=SECTION_ALL,
        badge=BADGE_SOON,
        coming_soon=True,
        hidden=True,
    ),
    ConnectorTile(
        connector_id="reddit_ads",
        name="Reddit Ads",
        sub_label="Notify me →",
        logo_glyph="R",
        logo_gradient="#FF4500",
        icon_file="reddit.svg",
        section=SECTION_ALL,
        badge=BADGE_SOON,
        coming_soon=True,
        hidden=True,
    ),
)


# Legacy color override for the gradient+glyph fallback path. With real
# SVG icons in place this dict is largely vestigial; kept so any tile
# without an ``icon_file`` still rendered legibly.
LOGO_TEXT_COLOR_OVERRIDES: dict[str, str] = {
    "snapchat_ads": "#000",
}


class ConnectorCatalogService:
    """Read-only accessor for the connector tile catalog.

     follow-up (2026-05-26) — two consumer surfaces with
    different audiences:

    * Onboarding wizard step-4 — uses ``popular_tiles()`` /
      ``all_section_tiles()`` / ``selectable_ids()`` /
      ``notify_me_ids()`` / ``placeholder_ids()`` /
      ``coming_soon_ids()``. These accessors filter to tiles where
      ``onboarding_eligible=True`` so a connector like Echo that
      needs a credentials-form setup outside the wizard never
      surfaces in onboarding.
    * /assets/data-connectors/ — uses ``all_tiles()`` (unfiltered).
      Renders the full catalog including ``onboarding_eligible=False``
      tiles so a user can set them up post-onboarding.

    ``get(connector_id)`` is unfiltered — both surfaces need to
    resolve display names for audit logging.
    """

    @staticmethod
    def all_tiles() -> tuple[ConnectorTile, ...]:
        """Return every visible tile in catalog order (popular first, then all).

        Used by /assets/data-connectors/, where Echo (and any future
        onboarding_eligible=False tile) belongs. ``hidden=True`` tiles
        are filtered out — they are suppressed on every
        surface, including this one.
        """
        return tuple(t for t in CONNECTORS if _shown(t))

    @staticmethod
    def popular_tiles() -> tuple[ConnectorTile, ...]:
        """Tiles in the "Most popular" section, in catalog order.

        Onboarding-eligible, non-hidden only.
        """
        return tuple(t for t in CONNECTORS if t.section == SECTION_POPULAR and t.onboarding_eligible and _shown(t))

    @staticmethod
    def all_section_tiles() -> tuple[ConnectorTile, ...]:
        """Tiles in the "All connectors" section, in catalog order.

        Onboarding-eligible, non-hidden only.
        """
        return tuple(t for t in CONNECTORS if t.section == SECTION_ALL and t.onboarding_eligible and _shown(t))

    @staticmethod
    def selectable_ids() -> frozenset[str]:
        """Set of connector_ids that may be persisted via step-4 POST.

        Excludes both ``coming_soon`` AND ``placeholder`` tiles — the
        Continue POST never advances the wizard on a placeholder
        selection (the click-time UX is the notify-me banner, not a
        wizard advance). Hand-crafted POSTs with a placeholder id are
        refused with 400, same shape as a coming-soon id.
        Also excludes ``onboarding_eligible=False`` tiles (
        follow-up).
        """
        return frozenset(
            t.connector_id
            for t in CONNECTORS
            if t.onboarding_eligible and _shown(t) and not t.coming_soon and not t.placeholder
        )

    @staticmethod
    def placeholder_ids() -> frozenset[str]:
        """Set of connector_ids whose tiles route to the OAuth-pending banner.

        These tiles render clickable but the wizard never advances on
        a placeholder selection — the click-time UX is the "OAuth
        wizard pending — please contact support" inline banner. The
        Continue POST refuses a placeholder id (400). Distinct from
        ``coming_soon_ids()``: placeholder tiles have a partial OAuth
        integration; coming-soon tiles have none.
        Onboarding-eligible only.
        """
        return frozenset(t.connector_id for t in CONNECTORS if t.placeholder and t.onboarding_eligible and _shown(t))

    @staticmethod
    def coming_soon_ids() -> frozenset[str]:
        """Set of connector_ids whose tiles are roadmap-only.

        These tiles render with the "Soon" badge. Per they are
        clickable into the same notify-me banner as ``placeholder_ids``
        (with adapted "Coming soon — get notified" copy). The
        Continue POST refuses a coming-soon id (400) — same gate as
        placeholder, different UX framing.
        Onboarding-eligible only.
        """
        return frozenset(t.connector_id for t in CONNECTORS if t.coming_soon and t.onboarding_eligible and _shown(t))

    @staticmethod
    def notify_me_ids() -> frozenset[str]:
        """Union of placeholder + coming-soon ids — the notify-me allowlist.

        Server-side allowlist for the ``notify_me=1`` POST so a forged
        request can't stuff an arbitrary connector_id (e.g. a working
        tile's id, or a typo) into the notify-list or the audit log.
        Disjoint from ``selectable_ids()`` by construction since the
        two underlying flags are mutually exclusive on the catalog.
        Onboarding-eligible only.
        """
        return frozenset(
            t.connector_id for t in CONNECTORS if t.onboarding_eligible and _shown(t) and (t.placeholder or t.coming_soon)
        )

    @staticmethod
    def get(connector_id: str) -> ConnectorTile | None:
        """Look up a single tile by id; ``None`` for unknown / coming-soon
        / empty inputs. Used by the route to resolve the user's selection
        back to the tile's display name for audit logging.
        """
        if not connector_id:
            return None
        for tile in CONNECTORS:
            if tile.connector_id == connector_id:
                return tile
        return None
