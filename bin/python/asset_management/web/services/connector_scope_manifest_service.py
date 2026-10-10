"""
Connector Scope Manifest Service

Source-of-truth for the OAuth scope summary rendered on wizard step 5
(the pre-redirect permissions review screen). Each entry maps a
``connector_id`` to a list of ``ScopeGroup`` rows describing what
Kavanah will ask for, why, and which provider-side scope keys back
that request — borrowing from Funnel.io / Stripe Connect's plain-
language permission boards.

The scope manifest (which scopes a connector asks for) is hard-coded
here rather than read from ES so step-5 renders without a live
cluster (smoke surface), and so a new connector's scopes can be added
in one place. Today only ``meta_ads`` has a manifest; Google Ads /
GA4 are ``placeholder=True`` on the catalog so the wizard never
reaches step-5 with those ids (carved them out of
``selectable_ids()``).

The Meta scope set — ``ads_read``, ``business_management``,
``pages_show_list`` for the dev-mode-available phase, plus
``pages_read_engagement``, ``instagram_basic``, ``instagram_manage_insights``
once Meta App Review approves them on app 1858829444798788 (
follow-up) — comes from the App Review narrative at
``the private docs``. The
narrative is the canonical source of truth for what Kavanah will
request in production; this manifest is a temporary subset that
the wizard ships with until App Review completes.

 Issue 1 (2026-05-21): pre-fix the manifest carried
``read_insights`` — a scope key that, at the time, did not render in
Meta's Login dialog. Meta's OAuth dialog responded with "Invalid
Scopes: read_insights" and refused to render, so it was removed.

2026-05-28: ``read_insights`` was re-added (reversing the
removal). The Meta-side permission was fixed and the active
integration authorises against a system user token that carries
``read_insights``, so the consent dialog renders it correctly now.
The display manifest and the wire contract
(``wizard_oauth_service._DEFAULT_SCOPES_BY_CONNECTOR``) are kept in
lockstep per the sync invariant.

The "Kavanah will never" reassurance list lives in YAML at
``bin/python/asset_management/web/data/never_does.yaml`` so marketing
can edit copy without touching Python. (``data/`` rather than
``config/`` because the project's top-level ``.gitignore`` broadly
excludes ``config`` to mask the password-reset runtime-state
directory.) The YAML carries a ``default:`` block (shown for any
connector without an override) and an optional ``connectors:`` map
for per-connector overrides. The service caches the file at import —
``imports_container`` is restarted via SIGHUP after a deploy, which
rebuilds the cache. A parse error or missing file falls back to a
Python-side default (``_FALLBACK_NEVER_DOES``) so step-5 never
crashes on a malformed copy edit.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScopeGroup:
    """One row in the step-5 scope summary.

    A *group* maps a single user-facing reason (``name``) to one or
    more provider-side scope keys (``scope_keys``) plus the
    plain-language rationale (``why``). The grouping is deliberate:
    Meta's ``business_management`` + ``pages_show_list`` both back
    the same UX promise ("list your business accounts and pages"),
    so they share a row rather than splitting into two near-duplicate
    rows that would dilute the screen.
    """

    name: str
    scope_keys: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class ConnectorScopeManifest:
    """All scope groups for one connector, in the order they should
    render on step 5. ``connector_id`` matches the
    :mod:`connector_catalog_service` id.

    ``never_does`` is an optional code-side per-connector override
    for the "Kavanah will never" reassurance list. When ``None`` (the
    default), the YAML file at ``web/data/never_does.yaml`` is
    consulted: a ``connectors:<id>:`` block overrides the
    ``default:`` block. When set on the dataclass, the dataclass
    value wins — reserved for cases where the copy must ship in the
    same change as a new connector and a YAML edit isn't viable.
    """

    connector_id: str
    groups: tuple[ScopeGroup, ...]
    never_does: tuple[str, ...] | None = None


# Meta Marketing API scopes — the full 6-scope superset that matches the
# App Review submission. returned the page-engagement +
# Instagram scopes to the manifest after the App Review session signed
# off; until Meta formally approves the additional three, only
# app-tester accounts (dev users) get the full consent dialog. Wizard
# signups in dev mode work today because the testers are devs; once
# approval lands, the gated scopes go live for everyone.
#
# Per the narratives:
# * ``pages_read_engagement`` — read post-level engagement metrics
#   (reactions, comments, shares, video views) on Pages the user
#   manages. NOT user-content reads.
# * ``instagram_basic`` — identify the Instagram Business account
#   connected to a managed Page.
# * ``instagram_manage_insights`` — read post-level Instagram metrics
#   and audience demographics.
#
# Each is presented as its own row on step-5 because the App Review
# submission claims six scopes; the reviewer's hands-on test compares
# the consent screen against the submission. A grouped row would
# obscure individual scope rationale and risk a rejection.
META_ADS_MANIFEST = ConnectorScopeManifest(
    connector_id="meta_ads",
    groups=(
        ScopeGroup(
            name="Read your ad accounts and campaigns",
            scope_keys=("ads_read",),
            why=("So we can pull spend, impressions, clicks, and conversions " "into your dashboard."),
        ),
        ScopeGroup(
            name="List your business accounts and pages",
            scope_keys=("business_management", "pages_show_list"),
            why=("So we can show you which assets are available to import."),
        ),
        ScopeGroup(
            name="Read engagement on your Facebook Pages",
            scope_keys=("pages_read_engagement",),
            why=(
                "So we can display post-level engagement (reactions, "
                "comments, shares, video views) on your Page alongside "
                "your ads performance. We do not read user-authored "
                "content like comments — only the aggregate counts."
            ),
        ),
        ScopeGroup(
            name="Identify your connected Instagram account",
            scope_keys=("instagram_basic",),
            why=(
                "So we can find the Instagram Business account linked to "
                "your Facebook Page and surface its profile on the "
                "Instagram dashboards."
            ),
        ),
        ScopeGroup(
            name="Read Instagram post insights",
            scope_keys=("instagram_manage_insights",),
            why=(
                "So we can display post-level Instagram metrics "
                "(impressions, reach, video views, engagement) and "
                "audience demographics for your Instagram Business "
                "account. We do not post, comment, or message via this "
                "permission."
            ),
        ),
        ScopeGroup(
            name="Read your Page and post insights",
            scope_keys=("read_insights",),
            why=(
                "So we can pull aggregate insights for your Facebook "
                "Pages and posts (reach, impressions, engagement) into "
                "your dashboard. We read only the metric totals, never "
                "user-authored content."
            ),
        ),
    ),
)


_MANIFESTS: dict[str, ConnectorScopeManifest] = {
    META_ADS_MANIFEST.connector_id: META_ADS_MANIFEST,
}


# Last-resort default used only when never_does.yaml is missing or
# unparseable. Keep in sync with the YAML's `default:` block — the
# YAML is the canonical edit surface, this tuple is defensive.
_FALLBACK_NEVER_DOES: tuple[str, ...] = (
    "Post to your account",
    "Change campaign settings",
    "Run new ads",
    "Charge your card",
    "Access private messages",
    "Share data outside your workspace",
)


_NEVER_DOES_YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "never_does.yaml",
)


def _coerce_str_tuple(value: Any) -> tuple[str, ...] | None:
    """Return a tuple of stripped non-empty strings, or ``None`` if
    ``value`` isn't a list of strings. Used to validate every list
    that comes out of the YAML — defends against a marketing edit
    that accidentally writes a string scalar or nests a list inside
    a list."""
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        stripped = item.strip()
        if stripped:
            out.append(stripped)
    if not out:
        return None
    return tuple(out)


def _load_never_does_yaml() -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Parse ``never_does.yaml`` and return ``(default_list, overrides_by_id)``.

    Returns the fallback default and an empty overrides map if the
    file is missing, unreadable, malformed, or has no usable
    ``default:`` block. A warning is logged so a copy regression
    surfaces in container logs rather than silently degrading to the
    fallback list.
    """
    if not os.path.exists(_NEVER_DOES_YAML_PATH):
        logger.warning(
            "never_does.yaml not found at %s; using fallback list",
            _NEVER_DOES_YAML_PATH,
        )
        return _FALLBACK_NEVER_DOES, {}

    try:
        with open(_NEVER_DOES_YAML_PATH, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("never_does.yaml unreadable (%s); using fallback list", exc)
        return _FALLBACK_NEVER_DOES, {}

    default_list = _coerce_str_tuple(raw.get("default")) if isinstance(raw, dict) else None
    if default_list is None:
        logger.warning("never_does.yaml missing or empty `default:` block; using fallback list")
        default_list = _FALLBACK_NEVER_DOES

    overrides: dict[str, tuple[str, ...]] = {}
    connectors_block = raw.get("connectors") if isinstance(raw, dict) else None
    if isinstance(connectors_block, dict):
        for connector_id, items in connectors_block.items():
            if not isinstance(connector_id, str) or not connector_id:
                continue
            coerced = _coerce_str_tuple(items)
            if coerced is not None:
                overrides[connector_id] = coerced

    return default_list, overrides


_DEFAULT_NEVER_DOES, _NEVER_DOES_OVERRIDES = _load_never_does_yaml()


# Back-compat alias: release-plan test imports ``NEVER_DOES``
# directly. Keep it pointing at the YAML-resolved default block so the
# existing assertion that NEVER_DOES is non-empty still holds without
# the test having to know about the YAML.
NEVER_DOES: tuple[str, ...] = _DEFAULT_NEVER_DOES


class ConnectorScopeManifestService:
    """Read-only accessor for the step-5 scope manifest catalog."""

    @staticmethod
    def get(connector_id: str) -> ConnectorScopeManifest | None:
        """Return the manifest for ``connector_id`` or ``None`` if the
        connector has no manifest yet. Today only ``meta_ads`` is
        backed; callers should treat ``None`` as "route the user back
        to step-4" since the wizard can't ask for permissions it
        can't describe.
        """
        if not connector_id:
            return None
        return _MANIFESTS.get(connector_id)

    @staticmethod
    def supported_connector_ids() -> frozenset[str]:
        """Set of connector_ids that have a step-5 manifest. Used by
        the step-5 route's allowlist gate so a stale wizard_session
        naming a connector without a manifest falls back to step-4
        instead of rendering an empty permissions list.
        """
        return frozenset(_MANIFESTS.keys())

    @staticmethod
    def never_does(connector_id: str | None = None) -> tuple[str, ...]:
        """Return the "Kavanah will never" reassurance list for
        ``connector_id``. Resolution order:

        1. ``ConnectorScopeManifest.never_does`` if set on the
           dataclass — reserved for code-side overrides.
        2. YAML ``connectors:<connector_id>:`` block.
        3. YAML ``default:`` block (or ``_FALLBACK_NEVER_DOES`` if
           the YAML is missing / malformed).

        Passing ``None`` (or an unknown id) returns the default — the
        same behaviour the callsite had before per-connector
        overrides were added.
        """
        if connector_id:
            manifest = _MANIFESTS.get(connector_id)
            if manifest is not None and manifest.never_does is not None:
                return manifest.never_does
            override = _NEVER_DOES_OVERRIDES.get(connector_id)
            if override is not None:
                return override
        return _DEFAULT_NEVER_DOES
