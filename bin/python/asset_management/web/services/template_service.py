"""Dashboard template registry + instantiation.

ES-backed registry of curated dashboards that can be cloned into a
user's workspace. Replaces the Python-dict registry in
``first_look_templates.py`` + ``FirstLookDashboardService`` with an
ES index so non-engineers can curate templates via a "Save as
template" UI (slice 2 of) and so the wizard can install a
whole *pack* of dashboards per connector (e.g. every Meta-tagged
template lands under a "Meta" menu when a wizard user picks the
Meta connector).

Storage
-------
Templates live in ``de_dashboard_templates`` — one doc per template:

    {
      "template_id":           "<stable slug>",
      "pack_id":               "meta" | "echo" | "google_ads" | "google_analytics_4",
      "connector_id":          "meta_ads" | "echo" | ...,
      "name":                  "Meta — Spend & ROAS, weekly",
      "description":           "...",
      "menu_title":            "Meta",     # workspace menu the clone lands under
      "menu_position":         0,
      "required_sources":      ["meta_ads"],
      "placeholder_field":     "ad_account_id",   # which doc field the ASSET_PLACEHOLDER swaps to at instantiate
      "echarts_config":        { "charts": [...], "filters": [...], ... },
      "frozen_from_dashboard_id": "<source dashboard id>" | null,
      "created_at":            "...",
      "updated_at":            "..."
    }

Substitution
------------
Templates carry the same ``ASSET_PLACEHOLDER`` sentinel
``first_look_templates`` uses. ``instantiate`` walks every chart's
filters and swaps the sentinel for the user's actual asset ids,
collapsing ``match_phrase_any``→``match_phrase`` for the one-asset
case (mirrors ``first_look_dashboard_service._substitute_assets``).

Idempotency
-----------
``instantiate`` is idempotent on ``(team_esid, template_id)`` —
re-running for the same pair returns the existing dashboard's id
instead of writing a duplicate. The instantiated doc carries
``template_id`` + ``template_team_esid`` marker fields the
idempotency lookup keys on. ``materialize_pack_for_user`` loops
``instantiate`` per template in the pack, so the pack-level call
is idempotent too.

Visibility
----------
Templates are **global** — no workspace scope. Any workspace can
install any template. Curation authz (who can call
``freeze_dashboard_as_template``) is enforced at the route layer,
not here. Read access is unrestricted.
"""

from __future__ import annotations

import copy
import logging
import secrets
from datetime import datetime, timezone
from typing import Any
from lib.errors import UserFacingError

# The asset-bound placeholder sentinel. Lives here (was previously in
# first_look_templates.py, deleted slice 5). Tuple form
# is the canonical in-memory shape: immutable so a template author
# can't accidentally mutate it across sites. The substitution code in
# ``_substitute_assets`` walks every chart filter and swaps this tuple
# (or its bare string form) for the consumer's actual asset ids.
ASSET_PLACEHOLDER: tuple[str, ...] = ("__FIRST_LOOK_ASSET_PLACEHOLDER__",)

TEMPLATES_INDEX = "de_dashboard_templates"
DASHBOARDS_INDEX = "de_asset_echarts_dashboards"

# slice 2 follow-up (2026-05-22 re-apply): the JSON-bound
# sentinel string. ``ASSET_PLACEHOLDER`` itself is a tuple (
# invariant — immutable so a template author can't mutate it across
# sites), but when stamping into ``de_dashboard_templates.echarts_config``
# we need a plain string so JSON serializes cleanly. Otherwise the
# saved doc carries ``selected_values: [["..."]]`` (a tuple-in-list)
# which is ugly to read and would break consumers that expect a flat
# list of strings. The sentinel *detector* below recognizes BOTH
# shapes so legacy in-memory tuple templates kept working through the
# slice-3 → slice-5 transition.
ASSET_PLACEHOLDER_STR: str = ASSET_PLACEHOLDER[0]

# match-nothing sentinel for a placeholder template installed with NO
# resolved asset ids (teamless / no connected source). Substituting this value
# (which no real asset id ever equals) into an asset-bound filter makes the
# chart render BLANK rather than either erroring or matching cluster-wide. It is
# needed because the two filter shapes disagree on the empty case: a per-chart
# ``match_phrase_any([])`` already matches nothing, but a dashboard
# filter with ``selected_values=[]`` is SKIPPED by ``_build_filters`` — i.e. it
# matches everything in the caller's Layer-1 scope (the cross-tenant concern for
# broad-scope/privileged users). A non-empty sentinel makes BOTH shapes
# match-nothing uniformly. The dashboard is fully usable once a team is set + a
# source connected (re-install / properties re-scope replaces the sentinel).
UNRESOLVED_ASSET_SENTINEL: str = "__KAN1039_UNRESOLVED_TEMPLATE_ASSET__"

# placeholder fields that PIN a chart to one
# account/property/team *within* a team's data. Every source these target
# (ga_metrics, fb_insights, google_ads, echo_conversations) lives in a
# per-team ``{team_esid}__{source}`` index, so Layer-1 index scoping
# (scoped_index_pattern) already constrains the query to the user's own teams
# regardless of body filters — a filter on any of these fields is a within-scope
# NARROWING, never the tenant boundary. Product decision (a teammate, 2026-06-18): a
# template dashboard should show the viewer's in-scope data aggregated (like a
# from-scratch chart), not pin to a single account/property/team. So
# ``_substitute_assets`` DROPS a placeholder filter on any of these fields
# entirely instead of writing a value or the match-nothing sentinel.
#
# ``team_esid`` IS in this set. It was initially carved out
# on the assumption it was real cross-tenant SCOPING for Echo — but
# ``echo_conversations`` is a per-team index exactly like ga_metrics (verified:
# each ``{esid}__echo_conversations`` holds one team_esid), so the team_esid pin
# is redundant with Layer-1 and an Echo template installed by a multi-team org
# owner went blank (sentinel) instead of showing in-scope data. ``first_look_echo``
# is the only template using a team_esid placeholder, and it targets the per-team
# ``*_echo_conversations`` view, so dropping it cannot widen scope. (The carve-out
# was field-name reasoning, not index-structure verification — corrected here.)
_DROPPABLE_PLACEHOLDER_FIELDS: frozenset[str] = frozenset(
    {"property_id", "ad_account_id", "google_ads_customer_id", "team_esid"}
)


# ─── Exceptions ──────────────────────────────────────────────────────────────


class TemplateError(UserFacingError):
    """Base for template-service errors callers may want to distinguish."""


class TemplateNotFoundError(TemplateError):
    """``instantiate`` / ``get_template`` called with an unknown template_id."""


class TemplateInstantiationError(TemplateError):
    """Instantiation refused — typically because the template references
    ``ASSET_PLACEHOLDER`` but the caller supplied zero asset ids, which
    would produce a filter ``value=[]`` that silently matches every
    team's data (the cross-tenant leak class closed).
    """


class TemplateFreezeError(TemplateError):
    """``freeze_dashboard_as_template`` could not read the source dashboard
    or the curator supplied no placeholder fields against a dashboard
    whose filters contain no asset-bound values.
    """


# ─── Time / id helpers ───────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_dashboard_id() -> str:
    # 16 hex chars — matches the shape uuid4().hex[:16] would produce
    # but uses secrets.token_hex for a stronger source than uuid.
    return secrets.token_hex(8)


# ─── Service ─────────────────────────────────────────────────────────────────


class TemplateService:
    """Read + write the global template registry."""

    # ────────────────────────────────────────────────────────────────────
    # Read
    # ────────────────────────────────────────────────────────────────────

    @staticmethod
    def list_templates() -> list[dict[str, Any]]:
        """Return every template doc, sorted by ``(pack_id, menu_position)``.

        Returns an empty list on ES outage or empty index — callers
        treat that as "no templates available", which is identical
        to the day-zero state before the migration job runs.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("TemplateService.list_templates: ES unavailable")
            return []
        try:
            result = es.search(
                index=TEMPLATES_INDEX,
                body={"query": {"match_all": {}}, "size": 1000},
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning("TemplateService.list_templates ES error: %s", e)
            return []
        docs = [hit.get("_source", {}) for hit in result.get("hits", {}).get("hits", [])]
        docs.sort(key=lambda d: (d.get("pack_id", ""), d.get("menu_position", 0)))
        return docs

    @staticmethod
    def get_template(template_id: str) -> dict[str, Any] | None:
        """Single-template lookup. Returns ``None`` on miss — callers that
        require existence raise ``TemplateNotFoundError`` themselves so
        we don't tangle missing-vs-ES-error semantics here.
        """
        if not template_id:
            return None
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.search(
                index=TEMPLATES_INDEX,
                body={
                    "query": {"term": {"template_id": template_id}},
                    "size": 1,
                },
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning("TemplateService.get_template ES error: %s", e)
            return None
        hits = result.get("hits", {}).get("hits", [])
        return hits[0].get("_source") if hits else None

    # #2: ``required_sources`` on a template is a list of
    # *connector_ids* (the wizard's connector taxonomy — "meta_ads",
    # "google_ads", "echo", "google_analytics_4"). But ``de_connection``
    # stores the OAuth ``provider`` ("facebook" / "google" / etc.). The
    # earlier code compared the two directly and every Meta template
    # showed "Connect Meta to unlock" even for users with an active
    # Meta connection. Bridge the two namespaces here so the
    # availability check resolves correctly.
    _CONNECTOR_TO_PROVIDER: dict[str, str] = {
        "meta_ads": "facebook",
        # Future connectors land here as they are added to the wizard.
        # Google Ads / GA4 / Echo aren't yet wired through de_connection
        # the same way Meta is, but listing
        # known mappings here makes the bridge explicit when the next
        # connector ships.
    }

    @classmethod
    def list_templates_for_user(
        cls,
        principal: str,
        extra_providers: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Annotate every template with ``is_available`` based on the
        user's connected sources. A template is available when every
        entry in its ``required_sources`` (a list of connector_ids)
        maps to a ``provider`` on one of the user's ``de_connection``
        docs.

        The picker (slice 4) renders ``is_available=False`` tiles
        greyed out with a "Connect X to unlock" CTA. Listing templates
        whose required sources the user has NOT connected is
        deliberate — it advertises what they could unlock by adding a
        connection.

        ``extra_providers`` unions provider strings the caller
        resolved from a source other than the principal's de_connection
        rows. The picker availability check was username/de_connection
        only, so an org-tier user (org owner with no team membership and
        no de_connection of their own) saw EVERY template locked even
        though their org's teams carry working connectors. The route
        passes the providers enabled across the teams in the user's
        scope so those templates unlock. The provider strings must match
        the bridged ``required_sources`` taxonomy (e.g. ``facebook``,
        ``google_analytics_4``, ``echo``).
        """
        templates = cls.list_templates()
        if not templates:
            return []
        try:
            from asset_management.web.services.connection_service import (
                ConnectionService,
            )
        except ImportError:
            from web.services.connection_service import ConnectionService  # type: ignore[no-redef]

        connections = ConnectionService.list_connections_for_principal(principal) if principal else []
        connected_providers = {
            (hit.get("_source") or {}).get("provider") for hit in connections if (hit.get("_source") or {}).get("provider")
        }
        if extra_providers:
            connected_providers |= set(extra_providers)

        annotated = []
        for t in templates:
            required_connectors = set(t.get("required_sources") or [])
            # Translate each required connector_id to its provider via
            # the bridge map. An unmapped connector_id stays as-is so a
            # template authored to reference a literal provider string
            # (or a future connector not in the map) still resolves
            # against connected_providers without silently being
            # treated as "always available."
            required_providers = {cls._CONNECTOR_TO_PROVIDER.get(c, c) for c in required_connectors}
            available = required_providers.issubset(connected_providers) if required_providers else True
            annotated.append({**t, "is_available": available})
        return annotated

    @classmethod
    def list_packs_for_connector(cls, connector_id: str) -> list[dict[str, Any]]:
        """Every template whose ``connector_id`` matches, ordered by
        ``menu_position`` then ``template_id``. Drives the wizard
        auto-install in slice 3.
        """
        if not connector_id:
            return []
        all_templates = cls.list_templates()
        pack = [t for t in all_templates if t.get("connector_id") == connector_id]
        pack.sort(key=lambda d: (d.get("menu_position", 0), d.get("template_id", "")))
        return pack

    # ────────────────────────────────────────────────────────────────────
    # Instantiate
    # ────────────────────────────────────────────────────────────────────

    @classmethod
    def instantiate(
        cls,
        *,
        template_id: str,
        team_esid: str | None,
        source_account_ids: list[str],
        workspace_esid: str | None = None,
        org_esid: str | None = None,
        title_override: str | None = None,
        menu_override: str | None = None,
        created_by: str | None = None,
        visibility_override: dict[str, Any] | None = None,
    ) -> str:
        """Clone a template into ``de_asset_echarts_dashboards``. When a
        ``team_esid`` is supplied the clone is scoped to that team and is
        idempotent on ``(team_esid, template_id)`` — re-running returns the
        existing dashboard id.

        ``team_esid`` is now optional. A template only NEEDS a
        team when it carries an ``ASSET_PLACEHOLDER`` (the team supplies
        the account ids substituted into the placeholder). Most templates
        have no placeholder — they're plain dashboards — so an org/workspace
        owner with no team membership can install them and then choose which
        teams see them via the dashboard properties panel, exactly like a
        hand-built dashboard. A teamless install lands as ``show_all``
        (visible + editable) rather than ``show_teams`` with an empty
        ``allowed_team_esids`` (which would be invisible to everyone — the
         trap) and is NOT team-keyed for idempotency (a re-click
        creates another, same as duplicating a manual dashboard).

        ``title_override`` (smoke followup #3, 2026-05-25): if supplied,
        stamps that title on the new dashboard instead of the template's
        ``name``. Used by the install route to dedupe titles when a
        different dashboard already owns the template's default name
        (collision suffix ``[2]``, ``[3]``, …). Pre-fix, two templates
        whose default name matched an existing dashboard landed two
        dashboards with the same title and the sidebar showed a
        duplicate entry the user couldn't distinguish.

        Returns the dashboard id (new or existing). Raises:

        * ``TemplateNotFoundError`` — no template doc with this id.
        * ``TemplateInstantiationError`` — template references the
          placeholder but no ``team_esid`` / asset ids were supplied.
          (Empty filter values would match cluster-wide; refusing keeps
          the tenant boundary intact.)
        """
        template = cls.get_template(template_id)
        if template is None:
            raise TemplateNotFoundError(f"Template {template_id!r} not found")

        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            raise TemplateInstantiationError("ES unavailable")

        # Idempotency only applies to team-scoped installs (the dedupe key is
        # the team). Teamless plain-dashboard installs are not deduped.
        if team_esid:
            existing_id = cls._find_existing_instantiation(es, team_esid, template_id)
            if existing_id is not None:
                return existing_id

        echarts_config = copy.deepcopy(template.get("echarts_config") or {})
        substituted = _substitute_assets(
            echarts_config.get("charts") or [],
            source_account_ids or [],
            dashboard_filters=echarts_config.get("filters") or [],
        )
        # a placeholder template installed with no asset ids
        # (teamless / no connected source) is no longer refused. ``_substitute_assets``
        # now writes UNRESOLVED_ASSET_SENTINEL into the placeholder slots, which
        # makes the asset-bound charts render blank (match-nothing) on BOTH the
        # per-chart and dashboard-filter shapes — never the cluster-wide filter
        # the old guard protected against. The dashboard installs and is fully
        # usable; its data-bound charts populate once a team + source are set and
        # the template is re-installed / re-scoped. (The previous guard raised
        # TemplateInstantiationError here.)

        now = _now_iso()
        dashboard_id = _new_dashboard_id()
        title = title_override if title_override else (template.get("name") or template.get("title") or "Untitled")
        # ``menu_override`` lets the install flow drop the dashboard
        # into the menu the USER picked (the New-Dashboard name+menu modal),
        # instead of the menu the template author baked into ``menu_title``.
        # The user-chosen menu always exists in their workspace (the picker is
        # populated from it), so the dashboard lands in a visible group. Falls
        # back to the template's menu_title when no override is supplied
        # (e.g. pack materialize, which has no per-install user choice).
        menu = (menu_override or "").strip() or template.get("menu_title") or "Reports"
        # visibility resolution.
        #
        # ``visibility_override`` (supplied by the install route) makes a
        # template install obey the SAME teams-only model as the New button
        #: a non-workspace-admin creator lands ``show_teams`` scoped to
        # THEIR OWN teams — visible to their org only, NOT workspace-wide. This
        # closes the leak where an org owner's installed dashboard defaulted to
        # ``show_all`` and surfaced in sibling orgs of the same agency workspace.
        # Shape: ``{"visibility_mode", "allowed_team_esids", "allowed_org_esids"}``.
        #
        # Fallback (no override — pack-materialize / programmatic callers): the
        # legacy ``show_teams`` + the single resolved team, else
        # ``show_all`` (so a teamless install isn't hidden from everyone, the
        # trap). The install route always passes an override now, so the
        # fallback only serves non-interactive callers.
        if visibility_override is not None:
            visibility_mode = visibility_override.get("visibility_mode") or "show_teams"
            allowed_team_esids = list(visibility_override.get("allowed_team_esids") or [])
            allowed_org_esids = list(visibility_override.get("allowed_org_esids") or [])
        else:
            visibility_mode = "show_teams" if team_esid else "show_all"
            allowed_team_esids = [team_esid] if team_esid else []
            allowed_org_esids = [org_esid] if org_esid else []
        doc: dict[str, Any] = {
            "dashboard_id": dashboard_id,
            "dashboard_type": "echarts",
            "title": title,
            "menu": menu,
            "menu_position": template.get("menu_position", 0),
            "created_at": now,
            "updated_at": now,
            # record the installer so the creator
            # short-circuit (read + mutate gates) lets them edit/delete what
            # they just installed, exactly like a hand-built dashboard.
            "created_by": created_by or "",
            "visibility_mode": visibility_mode,
            "allowed_org_esids": allowed_org_esids,
            "allowed_team_esids": allowed_team_esids,
            "workspace_esid": workspace_esid or "",
            "echarts_config": {
                "global_date_range": echarts_config.get("global_date_range"),
                "filters": echarts_config.get("filters") or [],
                "charts": echarts_config.get("charts") or [],
            },
            # marker fields — drive the idempotency lookup
            # AND let future UI surfaces tell a template-clone apart
            # from a user-built dashboard (e.g. "from template X" badge).
            "from_template": True,
            "template_id": template_id,
            "template_team_esid": team_esid,
            "template_instantiated_at": now,
        }

        try:
            es.index(
                index=DASHBOARDS_INDEX,
                id=dashboard_id,
                document=doc,
                refresh="wait_for",
            )
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning(
                "TemplateService.instantiate write failed template=%r team=%r: %s",
                template_id,
                team_esid,
                e,
            )
            raise TemplateInstantiationError("Could not save the new dashboard. Try again in a moment.") from e

        return dashboard_id

    @classmethod
    def materialize_pack_for_user(
        cls,
        *,
        connector_id: str,
        team_esid: str,
        source_account_ids: list[str],
        workspace_esid: str | None = None,
        org_esid: str | None = None,
    ) -> list[str]:
        """Install every template in the connector's pack into the user's
        workspace. Per-template fail-soft: a template that raises
        ``TemplateInstantiationError`` is logged + skipped; the rest
        of the pack still installs. Returns the dashboard ids that
        successfully landed (existing ids from idempotent re-runs are
        included).

        Slice 3 wires this in at the wizard's step-7 finish handler.
        """
        pack = cls.list_packs_for_connector(connector_id)
        if not pack:
            return []

        # slice 3 (2026-05-22): ensure the menu_title each
        # template declares exists in the workspace before any
        # dashboard lands. Otherwise the user-facing sidebar groups
        # the new dashboards under a synthetic ``Reports`` fallback
        # (per the empty-workspace UX in workspace_menu_service.py).
        #
        # ``add_menu`` already enforces case-insensitive uniqueness
        # within a workspace (workspace_menu_service.py:166) and
        # raises ``DuplicateMenuTitleError`` if the title already
        # exists — exactly the "skip if a matching menu is there"
        # semantic we want. Catch + continue.
        if workspace_esid:
            cls._ensure_pack_menus(workspace_esid, pack)

        landed: list[str] = []
        for template in pack:
            tid = template.get("template_id")
            if not tid:
                continue
            try:
                dashboard_id = cls.instantiate(
                    template_id=tid,
                    team_esid=team_esid,
                    source_account_ids=source_account_ids,
                    workspace_esid=workspace_esid,
                    org_esid=org_esid,
                )
                landed.append(dashboard_id)
            except TemplateError as e:
                logging.warning(
                    "TemplateService.materialize_pack_for_user template=%r team=%r failed: %s",
                    tid,
                    team_esid,
                    e,
                )
                continue
        return landed

    @classmethod
    def install_starter_pack(
        cls,
        *,
        connector_id: str,
        team_esid: str,
        source_account_ids: list[str],
        workspace_esid: str | None = None,
        origin: str = "data_connectors",
    ) -> dict[str, Any]:
        """fail-soft starter-pack install for a connector added
        OUTSIDE the onboarding wizard (Data Connectors page).

        The wizard's step-7 / step-8 handlers call
        :meth:`materialize_pack_for_user` directly; every Data Connectors
        finish path that did not (Meta, Echo, Disciple.Tools) left a new
        user with an empty Dashboard Builder. This wrapper never raises and
        never blocks the connector save. It returns the same vocabulary the
        wizard reports (``first_look_status`` ``installed`` / ``no_pack`` /
        ``exception`` / ``skipped``) plus every landed dashboard id.
        Idempotent by construction: :meth:`instantiate` dedups on
        ``(team_esid, template_id)``, so re-configuring a connector returns
        the existing dashboards instead of cloning them again.
        """
        out: dict[str, Any] = {"first_look_status": "skipped", "first_look_id": None, "dashboard_ids": []}
        if not connector_id or not team_esid:
            return out
        try:
            landed = cls.materialize_pack_for_user(
                connector_id=connector_id,
                team_esid=team_esid,
                source_account_ids=list(source_account_ids or []),
                workspace_esid=workspace_esid,
            )
        except Exception as e:  # noqa: BLE001 — boundary: the connector save must not fail on a pack
            logging.warning(
                "starter pack (%s) raised for connector=%r team=%r: %s",
                origin,
                connector_id,
                team_esid,
                e,
            )
            out["first_look_status"] = "exception"
            return out
        out["dashboard_ids"] = list(landed or [])
        if landed:
            out["first_look_status"] = "installed"
            out["first_look_id"] = landed[0]
        else:
            out["first_look_status"] = "no_pack"
        logging.info(
            "starter pack (%s): connector=%r team=%r status=%s dashboards=%d",
            origin,
            connector_id,
            team_esid,
            out["first_look_status"],
            len(out["dashboard_ids"]),
        )
        return out

    @staticmethod
    def _ensure_pack_menus(workspace_esid: str, pack: list[dict[str, Any]]) -> None:
        """slice 3 — create each ``menu_title`` referenced by
        the pack if it isn't already present in the workspace. Idempotent:
        a duplicate raises ``DuplicateMenuTitleError`` which is caught
        and logged at DEBUG (case-insensitive check is inside ``add_menu``
        — no need to re-check here). Other errors are logged WARNING
        but don't abort the pack-install — a missing menu degrades the
        dashboard's sidebar grouping but doesn't break visibility.
        """
        try:
            from asset_management.web.services.workspace_menu_service import (
                DuplicateMenuTitleError,
                WorkspaceMenuService,
            )
        except ImportError:
            from web.services.workspace_menu_service import (  # type: ignore[no-redef]
                DuplicateMenuTitleError,
                WorkspaceMenuService,
            )

        # Unique menu titles in pack-iteration order so the FIRST
        # template's menu_title lands at the lowest menu_position
        # (deterministic ordering matters when a fresh workspace
        # gets multiple menus seeded at once by one wizard signup).
        seen: set[str] = set()
        menu_titles: list[str] = []
        for template in pack:
            title = (template.get("menu_title") or "").strip()
            if not title or title.lower() in seen:
                continue
            seen.add(title.lower())
            menu_titles.append(title)

        for title in menu_titles:
            try:
                WorkspaceMenuService.add_menu(workspace_esid, title)
            except DuplicateMenuTitleError:
                # Menu already exists (case-insensitive). The
                # pack-install's dashboards reference it by title and
                # will group cleanly — nothing to do.
                logging.debug(
                    "TemplateService._ensure_pack_menus: menu %r already in workspace %r",
                    title,
                    workspace_esid,
                )
            except Exception as e:  # noqa: BLE001 — boundary
                logging.warning(
                    "TemplateService._ensure_pack_menus: add_menu(%r, %r) failed: %s",
                    workspace_esid,
                    title,
                    e,
                )

    # ────────────────────────────────────────────────────────────────────
    # Freeze (Save-as-template)
    # ────────────────────────────────────────────────────────────────────

    @classmethod
    def freeze_dashboard_as_template(
        cls,
        *,
        dashboard_id: str,
        template_id: str,
        pack_id: str,
        connector_id: str,
        menu_title: str,
        placeholder_fields: list[str],
        name: str,
        description: str = "",
        menu_position: int = 0,
        required_sources: list[str] | None = None,
    ) -> str:
        """Read a live dashboard, swap the curator-selected fields for
        ``ASSET_PLACEHOLDER``, write the result as a template doc.

        ``placeholder_fields`` is the list of doc-field names whose
        filter values should become sentinels — e.g. ``["ad_account_id"]``
        for a Meta dashboard. Fields not in the list keep their literal
        values. If the dashboard has no filters on any of the listed
        fields, the freeze still succeeds but the resulting template
        has zero placeholder slots (instantiate will not raise — see
        the ``substituted == 0`` branch).

        Returns the new ``template_id``.

        Raises ``TemplateFreezeError`` if the source dashboard is
        missing.
        """
        if not template_id:
            raise TemplateFreezeError("template_id is required")
        if not dashboard_id:
            raise TemplateFreezeError("dashboard_id is required")

        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            raise TemplateFreezeError("ES unavailable")

        try:
            source = es.get(index=DASHBOARDS_INDEX, id=dashboard_id)
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning(
                "TemplateService.freeze_dashboard_as_template source read failed dashboard=%r: %s",
                dashboard_id,
                e,
            )
            raise TemplateFreezeError(f"Source dashboard {dashboard_id!r} not found.") from e

        source_doc = (source or {}).get("_source") or {}
        echarts_config = copy.deepcopy(source_doc.get("echarts_config") or {})

        # Walk every filter — dashboard-level (
        # ``echarts_config.filters`` strip) AND per-chart
        # (``chart.filters``). For any filter whose ``field`` is in
        # ``placeholder_fields``, replace the concrete value with the
        # sentinel. The substitution at instantiate time will swap it
        # back to the consumer's asset ids.
        #
        # slice 2 follow-up (2026-05-21): two filter shapes are
        # in production. The dashboard-filter strip writes
        # ``selected_values`` (list); per-chart filters historically use
        # ``value`` (string or list) + ``op``. The original slice 2
        # code only handled the latter and only walked per-chart, which
        # silently no-op'd against any real Meta dashboard built in the
        # era. Caught by a teammate's first save-as-template smoke.
        placeholder_set = set(placeholder_fields or [])
        all_filters = list(echarts_config.get("filters") or [])
        for chart in echarts_config.get("charts") or []:
            all_filters.extend(chart.get("filters") or [])
        for f in all_filters:
            if f.get("field") in placeholder_set:
                _sentinelize_filter(f)

        now = _now_iso()
        template_doc: dict[str, Any] = {
            "template_id": template_id,
            "pack_id": pack_id,
            "connector_id": connector_id,
            "name": name,
            "description": description,
            "menu_title": menu_title,
            "menu_position": menu_position,
            "required_sources": list(required_sources or [connector_id]),
            "placeholder_field": (placeholder_fields or [None])[0],
            "echarts_config": echarts_config,
            "frozen_from_dashboard_id": dashboard_id,
            "created_at": now,
            "updated_at": now,
        }

        try:
            es.index(
                index=TEMPLATES_INDEX,
                id=template_id,
                document=template_doc,
                refresh="wait_for",
            )
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning(
                "TemplateService.freeze_dashboard_as_template write failed template=%r: %s",
                template_id,
                e,
            )
            raise TemplateFreezeError("Could not save the template. Try again in a moment.") from e

        return template_id

    @classmethod
    def update_template(
        cls,
        *,
        template_id: str,
        echarts_config: dict[str, Any],
        pack_id: str,
        connector_id: str,
        menu_title: str,
        placeholder_fields: list[str],
        name: str,
        description: str = "",
        menu_position: int = 0,
        required_sources: list[str] | None = None,
    ) -> str:
        """#3 — re-freeze a template from an in-memory
        ``echarts_config`` (instead of reading a source dashboard).

        Used by the Edit Template flow: a superuser picks a template,
        the Builder hydrates its ``echarts_config`` into the canvas
        directly (no dashboard doc written), the curator edits, then
        saves back via this method. Mirrors the sentinel-stamping
        logic of :func:`freeze_dashboard_as_template` but bypasses the
        source-dashboard read — superusers don't have a primary team
        so the install + freeze path won't work for them.

        Idempotent on ``template_id`` — this overwrites the existing
        registry entry. Caller is expected to verify existence first
        (the route does this via :func:`get_template`).
        """
        if not template_id:
            raise TemplateFreezeError("template_id is required")
        if echarts_config is None:
            raise TemplateFreezeError("echarts_config is required")

        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            raise TemplateFreezeError("ES unavailable")

        # Deep-copy + sentinel-stamp the supplied config. Same walk as
        # freeze_dashboard_as_template — keeps the two paths writing
        # identically-shaped template docs so the picker / install
        # consumers don't care which path produced the row.
        echarts_config = copy.deepcopy(echarts_config or {})
        placeholder_set = set(placeholder_fields or [])
        all_filters = list(echarts_config.get("filters") or [])
        for chart in echarts_config.get("charts") or []:
            all_filters.extend(chart.get("filters") or [])
        for f in all_filters:
            if f.get("field") in placeholder_set:
                _sentinelize_filter(f)

        # Preserve created_at from the existing doc; bump updated_at.
        # A missing existing doc means the caller skipped the existence
        # check — fall back to "now" so the doc still writes cleanly.
        now = _now_iso()
        created_at = now
        try:
            existing = es.get(index=TEMPLATES_INDEX, id=template_id)
            created_at = (existing or {}).get("_source", {}).get("created_at") or now
        except Exception:  # noqa: BLE001 — boundary; missing doc is OK
            pass

        template_doc: dict[str, Any] = {
            "template_id": template_id,
            "pack_id": pack_id,
            "connector_id": connector_id,
            "name": name,
            "description": description,
            "menu_title": menu_title,
            "menu_position": menu_position,
            "required_sources": list(required_sources or [connector_id]),
            "placeholder_field": (placeholder_fields or [None])[0],
            "echarts_config": echarts_config,
            "frozen_from_dashboard_id": None,
            "created_at": created_at,
            "updated_at": now,
        }

        try:
            es.index(
                index=TEMPLATES_INDEX,
                id=template_id,
                document=template_doc,
                refresh="wait_for",
            )
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning(
                "TemplateService.update_template write failed template=%r: %s",
                template_id,
                e,
            )
            raise TemplateFreezeError("Could not save the template. Try again in a moment.") from e

        return template_id

    # ────────────────────────────────────────────────────────────────────
    # Internals
    # ────────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_existing_instantiation(es: Any, team_esid: str, template_id: str) -> str | None:
        """Idempotency lookup: has this template already been instantiated
        for this team? Returns the existing dashboard id, or ``None``.

        ES errors are swallowed (returning ``None``) — re-instantiating
        on a transient lookup failure is cheap; a stale duplicate is
        the failure mode we are guarding against, not the failure mode
        of the lookup itself.
        """

        # #3 smoke followup (2026-05-25 #3 retry): defensive
        # lookup. The dashboards index is created via dynamic ES
        # mapping, so under normal conditions ``template_id`` /
        # ``template_team_esid`` are text+keyword and the bare ``term``
        # query matches. But in practice the project has seen
        # mapping-drift cases where one cluster has
        # ``keyword`` and another has ``text + .keyword``. A failed
        # idempotency lookup is the LOAD-BEARING reason a re-install
        # produces a duplicate dashboard — if this returns None when
        # there IS an existing install, instantiate writes a second
        # doc with the same title. OR-on-both-shapes via ``bool.should``
        # so the lookup matches regardless of which mapping won.
        def _term_or_keyword(field: str, value: str) -> dict:
            return {
                "bool": {
                    "should": [
                        {"term": {field: value}},
                        {"term": {f"{field}.keyword": value}},
                    ],
                    "minimum_should_match": 1,
                }
            }

        try:
            resp = es.search(
                index=DASHBOARDS_INDEX,
                body={
                    "size": 1,
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"from_template": True}},
                                _term_or_keyword("template_id", template_id),
                                _term_or_keyword("template_team_esid", team_esid),
                            ]
                        }
                    },
                },
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as e:  # noqa: BLE001 — boundary
            logging.warning(
                "TemplateService._find_existing_instantiation lookup failed template=%r team=%r: %s",
                template_id,
                team_esid,
                e,
            )
            return None
        hits = (resp.get("hits") or {}).get("hits") or []
        if not hits:
            logging.info(
                "TemplateService._find_existing_instantiation: no existing dashboard for "
                "template=%r team=%r — will install fresh",
                template_id,
                team_esid,
            )
            return None
        existing_id = hits[0].get("_source", {}).get("dashboard_id")
        logging.info(
            "TemplateService._find_existing_instantiation: re-using dashboard %r for " "template=%r team=%r",
            existing_id,
            template_id,
            team_esid,
        )
        return existing_id


# ─── Substitution (shared with first_look) ───────────────────────────────────


def _sentinelize_filter(f: dict[str, Any]) -> None:
    """slice 2 follow-up — stamp ``ASSET_PLACEHOLDER`` onto a
    filter, picking the right key for the filter's shape.

    Two shapes are in production:

    * ** dashboard-filter strip** — ``{"field":..., "selected_values": [...], "title":...}``
      with no ``op``. The placeholder lives in ``selected_values`` as a
      single-element list ``[ASSET_PLACEHOLDER]`` so the renderer's
      existing list-shaped read keeps working.
    * **Per-chart filter (legacy)** — ``{"field": ..., "value": ..., "op": ...}``.
      The placeholder goes in ``value`` and the op is normalized to
      ``match_phrase_any`` so the multi-id substitution can fan out.

    Detect the shape by which key is present. A filter that carries
    both (shouldn't happen but defensive) gets both stamped.
    """
    has_selected_values = "selected_values" in f
    has_value = "value" in f
    if has_selected_values:
        # list of values, no op. Stamp the bare
        # string sentinel, NOT the tuple ``ASSET_PLACEHOLDER`` —
        # the latter serializes as a nested list when written to ES
        # (caught by a teammate's 2026-05-21 smoke).
        f["selected_values"] = [ASSET_PLACEHOLDER_STR]
    if has_value or not has_selected_values:
        # Per-chart shape (has ``value`` + ``op``), or a filter with
        # neither key (treat as per-chart by default so the
        # substitution at instantiate time has something to find).
        # Same bare-string sentinel as the selected_values branch so
        # JSON serializes consistently regardless of filter shape.
        f["value"] = ASSET_PLACEHOLDER_STR
        f["op"] = "match_phrase_any"


def _substitute_assets(
    charts: list[dict[str, Any]],
    asset_ids: list[str],
    *,
    dashboard_filters: list[dict[str, Any]] | None = None,
) -> int:
    """Walk every filter (dashboard-level + per-chart) and replace
    ``ASSET_PLACEHOLDER`` with the supplied asset ids. Returns the
    number of slots substituted.

    Mirrors ``first_look_dashboard_service._substitute_assets`` and
    extends it to handle the dashboard-filter strip shape:

    * **Per-chart filter** (``value`` + ``op``): single-asset case
      collapses ``match_phrase_any``→``match_phrase`` with a string
      value so the renderer's existing single-value branch applies;
      multi-asset case keeps ``match_phrase_any`` with a list value.
    * ** dashboard filter** (``selected_values``): the
      single-element ``[ASSET_PLACEHOLDER]`` becomes the user's list of
      asset ids (no op-collapse — the dashboard-filter strip's renderer
      always reads ``selected_values`` as a list).
    * Meta ad-account ids carrying the ``act_`` prefix get stripped
      to match what the FB importer stores (bare digits). Idempotent
      on non-``act_`` inputs.

    A return value of ``0`` means the template has no placeholder
    slots — legal for a template authored by a curator who chose not
    to flag any filter as asset-bound. The caller in ``instantiate``
    raises ``TemplateInstantiationError`` only when ``count > 0`` and
    ``asset_ids`` is empty (the cross-tenant-leak guard).

    a placeholder filter on a droppable field
    (``_DROPPABLE_PLACEHOLDER_FIELDS``: property_id / ad_account_id /
    google_ads_customer_id / team_esid) is now DROPPED entirely rather
    than substituted or sentinelled. Every source these target lives in a
    per-team ``{team_esid}__{source}`` index, so Layer-1 index scoping
    already constrains the query to the user's teams — an unpinned chart
    shows the viewer's in-scope data aggregated, exactly like a
    from-scratch chart, which is the product behavior (one team can own
    several properties/accounts; an org owner spans several teams; the
    user wants them shown together). Dropping also fixes the org-owner
    install that left these pins as the match-nothing sentinel (blank
    charts). ``team_esid`` is included after verifying
    ``echo_conversations`` is per-team (its pin was redundant with
    Layer-1, and an Echo install by a multi-team org owner went blank).
    """
    normalized = [_strip_act_prefix(a) for a in (asset_ids or [])]
    count = 0

    def _is_placeholder_filter(f: dict[str, Any]) -> bool:
        return _is_placeholder(f.get("value")) or _selected_values_is_placeholder(f.get("selected_values"))

    def _substitute_one(f: dict[str, Any]) -> None:
        """Swap the sentinel in a single non-pin placeholder filter in place
        (Echo team_esid). Mirrors the historical behavior."""
        if _selected_values_is_placeholder(f.get("selected_values")):
            # list semantics, no op-collapse.
            f["selected_values"] = list(normalized) if normalized else [UNRESOLVED_ASSET_SENTINEL]
        if _is_placeholder(f.get("value")):
            if not normalized:
                f["op"] = "match_phrase"
                f["value"] = UNRESOLVED_ASSET_SENTINEL
            elif len(normalized) == 1 and f.get("op") == "match_phrase_any":
                f["op"] = "match_phrase"
                f["value"] = normalized[0]
            else:
                f["value"] = list(normalized)

    def _process_filter_list(filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the rebuilt filter list: pin-field placeholders dropped,
        other placeholders substituted in place. Counts every placeholder slot
        seen (dropped or substituted) so the caller's leak-guard contract is
        unchanged."""
        nonlocal count
        kept: list[dict[str, Any]] = []
        for f in filters:
            if not isinstance(f, dict) or not _is_placeholder_filter(f):
                kept.append(f)
                continue
            count += 1
            if f.get("field") in _DROPPABLE_PLACEHOLDER_FIELDS:
                # drop the pin entirely (show in-scope aggregate).
                continue
            _substitute_one(f)
            kept.append(f)
        return kept

    if dashboard_filters is not None:
        # Rebuild the dashboard-level strip in place so a dropped pin is gone
        # from the caller's list (it passes echarts_config["filters"] directly).
        dashboard_filters[:] = _process_filter_list(dashboard_filters)
    for chart in charts:
        chart["filters"] = _process_filter_list(chart.get("filters", []))
    return count


def _selected_values_is_placeholder(value: Any) -> bool:
    """slice 2 follow-up — detect the sentinel in a
    ``selected_values`` list. We stamp a single-element list at freeze
    time, so the sentinel is at index 0.
    """
    if not isinstance(value, list) or not value:
        return False
    return _is_placeholder(value[0])


def _strip_act_prefix(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("act_"):
        return value[len("act_") :]
    return value


def _is_placeholder(value: Any) -> bool:
    """Recognize the sentinel in any of its persisted shapes:

    * Bare string ``ASSET_PLACEHOLDER_STR`` — the canonical
      shape used by ES-persisted templates and the freeze path.
    * Tuple ``ASSET_PLACEHOLDER`` — the legacy in-memory
      shape (first_look_templates.py). Still in use until the old
      service is deleted.
    * Tuple by equality (defensive against a future caller that
      constructs the tuple inline rather than importing the constant).
    * **Single-element list with the sentinel inside** (Bug 1,
      2026-05-26). The first_look_meta_ads template persisted on prod
      stores ``"value": ["__FIRST_LOOK_ASSET_PLACEHOLDER__"]`` — a
      list-wrapped string from an older template-write path. Without
      this branch, the wizard's instantiate() finds no placeholders
      to substitute, the cloned dashboard keeps the sentinel as its
      filter value, and every chart queries against the literal
      string → 0 hits → empty dashboard (the prod 2026-05-25
      "dashboard appears briefly then disappears" symptom). Once
      every persisted template is re-frozen on the canonical
      bare-string shape, this branch becomes dead code — leave it
      until a deliberate template re-bake.
    """
    if value == ASSET_PLACEHOLDER_STR:
        return True
    if value is ASSET_PLACEHOLDER:
        return True
    if isinstance(value, tuple) and value == ASSET_PLACEHOLDER:
        return True
    if isinstance(value, list) and len(value) == 1 and value[0] == ASSET_PLACEHOLDER_STR:
        return True
    return False
