"""
Scope Reconciliation Service

Compares the scopes Kavanah *requested* on the step-5 permissions screen
against the scopes the provider actually *granted* (read from the OAuth
callback's debug_token response). Produces a ``ReconciliationResult``
the step-5 GET hands straight to the template so a partial-scope grant
surfaces a per-missing-scope explainer + a "Re-grant access" button
that re-redirects with **only the missing scope keys**.

The three outcomes:

* ``FULL`` — every requested scope was granted. Step-5 renders the
  normal Continue button; the wizard advances to step-6 (today still
  soft-redirects to /dashboard until the step-6 ticket lands).

* ``PARTIAL`` — at least one scope was granted, at least one was
  missing. The template renders the per-missing-scope explainer board
  (one row per missing ``ScopeGroup``) plus a "Re-grant access"
  anchor whose href hits ``/onboarding/step-7/start-oauth?scopes=<missing>``
  so the second consent screen asks for only what's missing instead
  of the full original set. This is the AC headline: "Granting
  only ``ads_read`` shows explainer for missing ``business_management``
  → Re-grant succeeds → wizard continues to step 6."

* ``NONE_USABLE`` — zero requested scopes were granted (the user
  unchecked every box on Meta's consent screen, or Meta returned an
  empty ``data.scopes`` array). Step-5 routes to docs instead of
  offering another re-grant; the AC says "skip step 6 if no usable
  scopes were granted; route to docs."

Why a *group*-aware reconciliation (rather than a flat set of scope
strings):

Meta's permission board groups two scopes (``business_management`` +
``pages_show_list``) under one user-facing reason ("list your
business accounts and pages"). If Meta grants ``business_management``
but not ``pages_show_list``, the user has *partial* coverage of that
group — and the explainer should name the group ("we can't list your
pages") rather than the raw key. The reconciliation walks the
manifest's ``ScopeGroup`` rows so the explainer copy matches the
permissions board the user just clicked through.

A group is "missing" if ANY of its scope_keys was not granted. A group
is "fully granted" only if EVERY scope_key in it was granted. The
``NONE_USABLE`` bucket is the strongest signal: no group is fully
granted AND no group is partially granted (i.e. zero overlap).

This module performs no Elasticsearch, network, or filesystem I/O and
so is import-time safe in the test harness without mocking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from asset_management.web.services.connector_scope_manifest_service import (
    ConnectorScopeManifest,
    ConnectorScopeManifestService,
    ScopeGroup,
)


class ReconciliationOutcome(str, Enum):
    """The three reconciliation buckets the step-5 GET keys off.

    String-valued so the value can drop straight into an audit-log
    ``detail`` field without a ``.value`` lookup at every callsite.
    """

    FULL = "full"
    PARTIAL = "partial"
    NONE_USABLE = "none_usable"


@dataclass(frozen=True)
class MissingScopeRow:
    """One row in the partial-scope explainer.

    Mirrors ``ScopeGroup`` shape so the template can iterate
    ``missing_rows`` the same way it iterates ``scope_groups`` on the
    full render path. ``missing_keys`` is the subset of the group's
    ``scope_keys`` that were NOT granted — populated even when only one
    key in a multi-key group is missing.
    """

    name: str
    missing_keys: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class ReconciliationResult:
    """Result of comparing requested vs granted scopes for a connector.

    Fields:

    * ``outcome`` — one of ``FULL`` / ``PARTIAL`` / ``NONE_USABLE``.
      The template branches on this; the audit-log ``detail`` field
      records it for the marketing funnel slice.

    * ``granted_scopes`` — the normalised set of scope keys the
      provider returned, in arbitrary order. Lower-cased and
      whitespace-stripped. Exposed so the audit detail can name
      *what was granted* without re-parsing the callback args.

    * ``requested_scopes`` — the normalised set of scope keys the
      manifest asked for (flattened across groups). Same normalisation
      as ``granted_scopes``.

    * ``missing_keys`` — the set difference ``requested - granted``,
      flattened. Drives the re-grant URL: the next OAuth roundtrip
      requests only these keys.

    * ``missing_rows`` — per-group explainer rows, in manifest order.
      Empty when ``outcome == FULL``. On ``NONE_USABLE`` the rows
      still surface so the docs handoff can name which scopes the
      user denied (helps debug "why isn't my Meta connected").
    """

    outcome: ReconciliationOutcome
    granted_scopes: frozenset[str]
    requested_scopes: frozenset[str]
    missing_keys: frozenset[str]
    missing_rows: tuple[MissingScopeRow, ...] = field(default_factory=tuple)


def _normalise(scopes: Iterable[str] | None) -> frozenset[str]:
    """Lower-case + strip + drop empties. ``None`` becomes ``frozenset()``.

    The callback's ``debug_token.data.scopes`` array is usually clean
    but defensive normalisation matters because the raw query string
    can interleave whitespace ("ads_read , business_management") and
    Meta has historically returned mixed-case keys during dev-mode
    consent flows.
    """
    if not scopes:
        return frozenset()
    out: set[str] = set()
    for s in scopes:
        if not isinstance(s, str):
            continue
        normalised = s.strip().lower()
        if normalised:
            out.add(normalised)
    return frozenset(out)


class ScopeReconciliationService:
    """Pure-function reconciler for the step-5 partial-scope flow."""

    @staticmethod
    def reconcile(
        connector_id: str,
        granted_scopes: Iterable[str] | None,
    ) -> ReconciliationResult | None:
        """Compare requested vs granted scopes for ``connector_id``.

        Returns ``None`` when the connector has no manifest — the
        step-5 GET already bounces back to step-4 in that case (the
        manifest catalog gate). Callers should treat ``None`` as
        "don't render the partial-scope path; fall back to the manifest
        guard."

        The reconciliation walks the manifest's groups so per-group
        explainer copy stays in sync with the permissions board the
        user clicked through on the first OAuth visit.
        """
        manifest = ConnectorScopeManifestService.get(connector_id)
        if manifest is None:
            return None
        return ScopeReconciliationService._reconcile_against_manifest(
            manifest=manifest,
            granted_scopes=granted_scopes,
        )

    @staticmethod
    def _reconcile_against_manifest(
        manifest: ConnectorScopeManifest,
        granted_scopes: Iterable[str] | None,
    ) -> ReconciliationResult:
        granted = _normalise(granted_scopes)
        requested = _normalise(key for group in manifest.groups for key in group.scope_keys)

        # Per-group walk so the explainer rows match the prototype's
        # permissions board ordering and copy.
        missing_rows: list[MissingScopeRow] = []
        fully_granted_groups = 0
        partially_granted_groups = 0
        for group in manifest.groups:
            group_keys = _normalise(group.scope_keys)
            group_missing = group_keys - granted
            if not group_missing:
                fully_granted_groups += 1
                continue
            if group_missing != group_keys:
                # The user granted SOME of this group's keys — still
                # surface the missing ones in the explainer (e.g. user
                # granted business_management but not pages_show_list).
                partially_granted_groups += 1
            missing_rows.append(
                MissingScopeRow(
                    name=group.name,
                    missing_keys=tuple(sorted(group_missing)),
                    why=group.why,
                )
            )

        missing_keys = requested - granted

        if not missing_keys:
            outcome = ReconciliationOutcome.FULL
        elif fully_granted_groups == 0 and partially_granted_groups == 0:
            # No group has any granted key — the user denied everything.
            # The AC's "skip step 6 if no usable scopes were granted"
            # path. The missing_rows are still surfaced so the docs
            # handoff can name what the user would need to re-grant.
            outcome = ReconciliationOutcome.NONE_USABLE
        else:
            outcome = ReconciliationOutcome.PARTIAL

        return ReconciliationResult(
            outcome=outcome,
            granted_scopes=granted,
            requested_scopes=requested,
            missing_keys=frozenset(missing_keys),
            missing_rows=tuple(missing_rows),
        )

    @staticmethod
    def re_grant_scope_string(missing_keys: Iterable[str]) -> str:
        """Build the comma-joined scope string for the re-grant URL.

        Meta's OAuth dialog takes ``scope=ads_read,business_management``
        — comma-joined, no spaces. The re-grant URL passes ONLY the
        missing keys so the second consent screen asks for less, not
        the full original set (the AC's "Re-grant access button →
        re-redirect with the missing scopes only" requirement).

        Keys are lower-cased + sorted for a stable URL across renders;
        a stable URL is the cache-friendliness AC implicit in "Re-grant
        succeeds → wizard continues to step 6" (a flaky scope ordering
        would make the second roundtrip's redirect_uri match flaky
        too).
        """
        normalised = sorted(_normalise(missing_keys))
        return ",".join(normalised)
