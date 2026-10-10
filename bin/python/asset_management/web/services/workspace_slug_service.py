"""
Workspace Slug Service

Step-3 of the onboarding wizard collects ``workspace_title``. Two users
both signing up from acme.com under the org_owner branch would otherwise
both land on the same title-derived workspace ("Acme") and hit a hard
collision at the wizard's commit step — surfacing as a 500 (or worse, a
silent overwrite if the natural-key lookup matches).

This service is the step-3 soft-reservation surface:

* :func:`slugify` derives a deterministic url-safe slug from a freeform
  title — same transformation the inline check and the commit step both
  agree on.
* :meth:`WorkspaceSlugService.check_availability` consults the existing
  ``de_asset_workspace`` docs AND in-flight ``de_wizard_session`` state
  for collisions. The check is fail-open on ES error so a transient
  outage doesn't block all signups — the commit step re-checks and is
  the ultimate gate.
* :meth:`WorkspaceSlugService.suggest_variants` returns up to ``n``
  ``slug-2`` / ``slug-3`` / ``slug-team`` variants that are themselves
  free, so the auto-suggest UI never offers another taken value.

Uniqueness scope is **workspace-globally unique** (one ``acme`` across
the whole deployment, Slack/Linear pattern) per the spike's W1
decision. Org slugs are unique within their parent workspace — that
check lives in :func:`lib.es_helpers.check_org_slug_unique` (shipped
under F5 /). Team slugs are unique within their parent org.

Pre-F6 history: the probe ran against ``de_asset_organization.organization_title``
(client-side slugify). F6 flipped to ``de_asset_workspace.workspace_slug``
via a server-side ``term`` query — the workspace doc carries the slug
as a first-class keyword field (added in F4 /), so we no longer
need to over-fetch + slugify on the client.

The service is fail-soft by design: an ES outage returns
``(available=True, ...)`` so the user is never blocked behind unreachable
infra. The wizard's commit step (F7 /, follow-up) MUST re-run
this check and auto-suffix on conflict — that's the load-bearing dedup.
"""

from __future__ import annotations

import logging
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client

from asset_management.web.services.permissions_index_service import (
    WORKSPACE_INDEX,
)
from asset_management.web.services.wizard_session_service import (
    INDEX as WIZARD_SESSION_INDEX,
    STATUS_ACTIVE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Slug shape
#
# Lowercase ascii alnum + hyphens. Max 60 chars so the slug fits in URL
# segments and ES keyword fields without truncation surprises. Mins are
# 2 so we don't end up with "a" or "" slugs that are useless for routing.
# ─────────────────────────────────────────────────────────────────────────────

SLUG_MAX_LEN = 60
SLUG_MIN_LEN = 2
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_TRIM_HYPHENS = re.compile(r"^-+|-+$")

# Reserved slugs that would collide with existing route prefixes, ES
# index-name conventions, or Kavanah-the-operator's identity if a
# workspace claimed them. The route-prefix set was pre-existing under
# ; the operator-identity set was promoted to the workspace tier
# under F6 per the spike's Q5 answer.
RESERVED_SLUGS = frozenset(
    {
        # Route-prefix collisions (claiming these would shadow real routes).
        "admin",
        "api",
        "assets",
        "dashboard",
        "dashboards",
        "login",
        "logout",
        "onboarding",
        "profile",
        "settings",
        "signup",
        "static",
        "workspace",
        "workspaces",
        # Operator-identity reservations (/ F6 — promoted to
        # workspace tier so no tenant can register a slug that
        # impersonates Kavanah itself or claims a generic-support URL).
        "kavanah",
        "kavanahmedia",
        "support",
        "help",
        "billing",
    }
)


def slugify(title: str) -> str:
    """Deterministic title → slug transformation.

    Strips accents, lowercases, replaces every non-alnum run with a
    single hyphen, trims edge hyphens, and truncates to ``SLUG_MAX_LEN``.
    Empty / whitespace-only titles return ``""`` — the caller decides
    whether that's a check-against-empty or a fallback case.
    """
    if not title:
        return ""
    # NFKD strips accents/diacritics to their ascii base (é → e).
    normalised = unicodedata.normalize("NFKD", title)
    ascii_bytes = normalised.encode("ascii", "ignore").decode("ascii")
    lower = ascii_bytes.lower()
    hyphenated = _SLUG_PATTERN.sub("-", lower)
    trimmed = _TRIM_HYPHENS.sub("", hyphenated)
    return trimmed[:SLUG_MAX_LEN]


@dataclass(frozen=True)
class SlugAvailability:
    """Result of a single availability probe.

    ``available`` is the source-of-truth for the UI. ``reason`` is a
    short machine code for the template / JSON consumer ("taken",
    "reserved", "too_short", "empty"). ``suggestions`` is non-empty
    only when ``available`` is False AND the caller asked for them.
    """

    slug: str
    available: bool
    reason: str = "ok"
    suggestions: list[str] = field(default_factory=list)


class WorkspaceSlugService:
    """Step-3 slug-availability probe and suggestion generator.

    Stateless and pure-static; no instance state is carried between
    calls. The two ES queries (orgs index + wizard-session index) are
    bounded by ``size`` so a hot account never amplifies a check into
    a multi-megabyte response.
    """

    # How many docs to scan in each ES query. The query terms-narrow
    # on the slugified value, so this is a safety cap, not a window —
    # in practice the result set is 0 or 1.
    _MAX_HITS = 25

    @staticmethod
    def check_availability(
        slug_or_title: str,
        *,
        exclude_username: str | None = None,
        include_suggestions: bool = True,
    ) -> SlugAvailability:
        """Return the availability of ``slug_or_title`` at org-global scope.

        ``slug_or_title`` is passed through :func:`slugify` so the caller
        can hand the raw form value in without slugifying first — the
        inline check fires on every keystroke and ``slugify`` is the
        single shared source of truth for the resulting slug.

        ``exclude_username`` lets the user check their own in-flight
        wizard_session without it counting as a self-conflict — without
        it a user who typed "Acme", reloaded, and typed "Acme" again
        would see their own session reported as a collision.

        Fail-open contract: if ES is unreachable or a query raises,
        the method returns ``available=True`` with ``reason="check_failed"``
        so a transient outage does not block all signups. The wizard's
        commit step MUST re-check before writing the org doc.
        """
        slug = slugify(slug_or_title)

        if not slug:
            return SlugAvailability(slug=slug, available=False, reason="empty")
        if len(slug) < SLUG_MIN_LEN:
            return SlugAvailability(slug=slug, available=False, reason="too_short")
        if slug in RESERVED_SLUGS:
            suggestions = (
                WorkspaceSlugService._generate_suggestions(slug, exclude_username=exclude_username)
                if include_suggestions
                else []
            )
            return SlugAvailability(slug=slug, available=False, reason="reserved", suggestions=suggestions)

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("WorkspaceSlugService: ES unavailable; failing open for %r", slug)
            return SlugAvailability(slug=slug, available=True, reason="check_failed")

        if WorkspaceSlugService._slug_in_workspaces(
            es, slug, exclude_username=exclude_username
        ) or WorkspaceSlugService._slug_in_sessions(es, slug, exclude_username=exclude_username):
            suggestions = (
                WorkspaceSlugService._generate_suggestions(slug, exclude_username=exclude_username, es=es)
                if include_suggestions
                else []
            )
            return SlugAvailability(slug=slug, available=False, reason="taken", suggestions=suggestions)

        return SlugAvailability(slug=slug, available=True, reason="ok")

    @staticmethod
    def resolve_unique_slug(
        title: str,
        *,
        exclude_username: str | None = None,
        max_attempts: int = 50,
    ) -> str:
        """Return a definitely-free slug derived from ``title``.

        Used by the step-3 POST as the no-500 fallback when the user's
        slug collides at submit-time (the inline check is best-effort
        and can race a parallel signup). Walks ``slug``, ``slug-2``,
        ``slug-3`` … and returns the first free value. After
        ``max_attempts`` falls back to the original slug — the commit
        step will surface a hard error if it really is impossible to
        place the workspace, but step-3 itself never raises.
        """
        base = slugify(title) or "workspace"
        # Trim base so base + suffix fits in SLUG_MAX_LEN.
        suffix_budget = len(f"-{max_attempts}")
        base = base[: SLUG_MAX_LEN - suffix_budget].rstrip("-") or "workspace"

        for n in range(1, max_attempts + 1):
            candidate = base if n == 1 else f"{base}-{n}"
            probe = WorkspaceSlugService.check_availability(
                candidate, exclude_username=exclude_username, include_suggestions=False
            )
            if probe.available:
                return candidate
        # Defensive fallback. resolve_unique_slug is the no-500 path;
        # raising here would defeat the whole point.
        return base

    # ─────────────────────────────────────────────────────────────────────────
    # Internal probes
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _slug_in_workspaces(es, slug: str, *, exclude_username: str | None = None) -> bool:
        """True if any existing workspace doc holds ``slug`` as its workspace_slug.

        F6 flipped this probe from ``de_asset_organization``
        to ``de_asset_workspace``. The workspace doc carries
        ``workspace_slug`` as a first-class keyword (mapped under F4 /
        ), so we run a server-side ``term`` query rather than
        the pre-F6 over-fetch + client-side slugify dance.

        The bootstrap script writes the slug as the deterministic doc
        id (so an ``es.get`` would work too); we use ``search`` to keep
        the call site agnostic to that bootstrap-time convention — a
        future write path that picks a non-slug doc id would still
        index correctly.

        ``exclude_username`` lets the step-3 re-entry case (user posts,
        provisions a workspace, hits Back, types the same name) clear
        the inline-check warning: the matched workspace doc is the
        user's own, so it isn't a collision against them. The check
        uses ``WorkspaceMembershipService`` so a workspace counts as
        "the user's own" iff they're an actual member — not just any
        workspace they touched.
        """
        try:
            response = es.search(
                index=WORKSPACE_INDEX,
                body={
                    "query": {"term": {"workspace_slug": slug}},
                    "size": 1,
                    "_source": False,
                },
            )
        except Exception as e:
            logging.warning("WorkspaceSlugService: workspace probe failed for %r: %s", slug, e)
            return False

        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            return False
        if not exclude_username:
            return True
        # Workspace doc id IS the slug (deterministic, see _safe_create
        # in onboarding_service.py); compare against the user's own
        # workspace memberships and treat a match as "not a collision".
        try:
            from asset_management.web.services.workspace_membership_service import (
                WorkspaceMembershipService,
            )
        except ImportError:
            try:
                from web.services.workspace_membership_service import (  # type: ignore[no-redef]
                    WorkspaceMembershipService,
                )
            except ImportError:
                return True
        owned = set(WorkspaceMembershipService.get_workspaces_for_user(exclude_username) or [])
        if not owned:
            return True
        matched_id = hits[0].get("_id")
        return matched_id not in owned

    @staticmethod
    def _slug_in_sessions(es, slug: str, *, exclude_username: str | None) -> bool:
        """True if another user's active wizard_session would commit this slug.

        Pre-fix this check is what kept two browser tabs from racing
        each other to commit the same auto-named ``acme`` — without
        it, both tabs see "available" from the org probe and both
        proceed to step 4, only colliding at the commit step where one
        gets a 500 or a silent overwrite.

        ``state`` is ``enabled: false`` in the wizard_session mapping
        , so ES can't index nested
        ``state.workspace_title``. We pull active sessions for the
        ``onboarding`` wizard and slugify each one client-side.
        """
        try:
            response = es.search(
                index=WIZARD_SESSION_INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"status": STATUS_ACTIVE}},
                                {"term": {"wizard_type": "onboarding"}},
                            ]
                        }
                    },
                    "size": WorkspaceSlugService._MAX_HITS,
                    "_source": ["user_id", "state"],
                },
            )
        except Exception as e:
            logging.warning("WorkspaceSlugService: session probe failed for %r: %s", slug, e)
            return False

        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source") or {}
            if exclude_username and source.get("user_id") == exclude_username:
                continue
            state = source.get("state") or {}
            existing_title = state.get("workspace_title") or ""
            if existing_title and slugify(existing_title) == slug:
                return True
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # Suggestions
    # ─────────────────────────────────────────────────────────────────────────

    _SUFFIX_HINTS = ("team", "workspace", "hq", "co")

    @staticmethod
    def _generate_suggestions(
        slug: str,
        *,
        exclude_username: str | None,
        es=None,
        n: int = 3,
    ) -> list[str]:
        """Build up to ``n`` candidate variants that are themselves free.

        Mix of numeric (``slug-2``, ``slug-3``) and semantic
        (``slug-team``) so the user sees both flavours. Each candidate
        is probed; only free ones are returned. If we can't find ``n``
        free candidates the list is short — the UI handles that.
        """
        candidates: list[str] = []
        # Numeric variants: cheap, stable ordering.
        for i in range(2, 12):
            candidates.append(f"{slug}-{i}")
        # Semantic suffix variants.
        for hint in WorkspaceSlugService._SUFFIX_HINTS:
            candidates.append(f"{slug}-{hint}")

        free: list[str] = []
        for candidate in candidates:
            if len(candidate) > SLUG_MAX_LEN:
                continue
            if candidate in RESERVED_SLUGS:
                continue
            if es is None:
                # No ES — best-effort: return the first n candidates
                # without probing. The inline UI is informational; the
                # commit step is the real gate.
                free.append(candidate)
            else:
                taken = WorkspaceSlugService._slug_in_workspaces(
                    es, candidate, exclude_username=exclude_username
                ) or WorkspaceSlugService._slug_in_sessions(es, candidate, exclude_username=exclude_username)
                if not taken:
                    free.append(candidate)
            if len(free) >= n:
                break
        return free
