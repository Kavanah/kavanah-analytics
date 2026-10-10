"""Workspace Menu Service.

Workspace-scoped Dashboard Builder menus. The user-facing dashboards
page and the Dashboard Builder left panel both render menu groups in
the order this service returns, and a workspace admin can drag-reorder
them.

Data model (one doc per (workspace, menu) in ``de_asset_workspace_menu``):

    {
      "menu_esid":      "<random>",
      "workspace_esid": "<the workspace>",
      "menu_title":     "Meta",         # unique within workspace
      "menu_position":  3,              # 0-based; lower = higher in sidebar
      "created_at":     "...",
      "updated_at":     "...",
    }

The dashboard's existing ``de_asset_echarts_dashboards.menu`` field is
the menu title (string), not the ESID — keeping it lets a dashboard
remain readable even if the WorkspaceMenuService row goes missing, and
spares us a backfill migration. Rename cascades through
:meth:`rename_menu`. Delete refuses on non-empty menus rather than
silently reassigning dashboards (see :meth:`delete_menu`).

A brand-new workspace has zero menus. The Dashboard Builder picker
falls back to ``"Reports"`` when ``_workspaceMenus`` is empty, so the
first dashboard saved under a fresh workspace lands at the synthetic
``Reports`` heading; subsequent menus are added via :meth:`add_menu`
through the workspace-admin "+ Add Menu" surface. follow-up
2026-05-19 removed the previous auto-seed of nine default menu names
(Overview / Meta / Google / Web / Apps / Echo / Chatwoot / Disciple
Tools / Reports) — they were Kavanah-specific and confused
non-Kavanah workspace admins who shipped with a pre-populated sidebar
they didn't ask for. Existing seeded workspaces keep their menus; the
change only affects newly-created workspaces.

Write authz (add / rename / delete / reorder): caller verifies the
session user holds ``workspace_admin`` or ``workspace_owner`` on the
target workspace via ``WorkspaceMembershipService.get_role``. This
service does NOT consult the session — it is called from Flask
routes (which authz-check).
"""

from __future__ import annotations

import logging
import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client
from lib.errors import UserFacingError

try:
    from web.services.permissions_index_service import WORKSPACE_MENU_INDEX
except ImportError:
    from asset_management.web.services.permissions_index_service import WORKSPACE_MENU_INDEX


DASHBOARDS_INDEX = "de_asset_echarts_dashboards"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_esid() -> str:
    # 12 hex chars — same shape as other random esids in the project
    # (e.g. role_esid, menu_esid in seeded role/permission docs).
    return secrets.token_hex(6)


class WorkspaceMenuError(UserFacingError):
    """Base for workspace-menu errors that callers may want to distinguish."""


class DuplicateMenuTitleError(WorkspaceMenuError):
    """``add_menu`` / ``rename_menu`` would collide with an existing title."""


class MenuNotEmptyError(WorkspaceMenuError):
    """``delete_menu`` refused: the menu still has dashboards under it."""

    def __init__(self, menu_title: str, dashboards_in_menu: int):
        super().__init__(f"Menu '{menu_title}' still has {dashboards_in_menu} dashboard(s).")
        self.menu_title = menu_title
        self.dashboards_in_menu = dashboards_in_menu


class MenuNotFoundError(WorkspaceMenuError):
    """No menu with the given esid exists in this workspace."""


def _dashboards_in_menu_query(workspace_esid: str, menu_title: str) -> dict:
    """Dashboards of ONE workspace filed under ``menu_title`` (exact title).

    ``menu`` is mapped as analysed text with a ``keyword`` subfield, so the
    exact match must use ``menu.keyword``.
    """
    return {
        "bool": {
            "filter": [
                {"term": {"workspace_esid": workspace_esid}},
                {"term": {"menu.keyword": menu_title}},
            ]
        }
    }


class WorkspaceMenuService:
    """CRUD over ``de_asset_workspace_menu`` with first-read seeding."""

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @staticmethod
    def list_menus(workspace_esid: str) -> list[dict]:
        """Return the workspace's menus in display order.

        Returns ``[]`` for a workspace with no menus yet — the
        Dashboard Builder picker falls back to ``"Reports"`` in that
        case so a fresh workspace is still usable. ES outage → empty
        list too (callers degrade to "no menus" rather than raising
        into the request path; matches the WorkspaceMembershipService
        convention).

         follow-up: previously auto-seeded nine default menu
        titles on the first call for a workspace. The seed has been
        removed — see the module docstring.
        """
        if not workspace_esid:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        rows = WorkspaceMenuService._fetch_rows(es, workspace_esid)
        return [
            {
                "esid": r["menu_esid"],
                "title": r["menu_title"],
                "position": r["menu_position"],
            }
            for r in rows
        ]

    @staticmethod
    def _fetch_rows(es, workspace_esid: str) -> list[dict]:
        try:
            resp = es.search(
                index=WORKSPACE_MENU_INDEX,
                body={
                    "query": {"term": {"workspace_esid": workspace_esid}},
                    "size": 200,
                    "sort": [
                        {"menu_position": {"order": "asc"}},
                        {"menu_title": {"order": "asc"}},
                    ],
                },
            )
        except Exception as e:
            # Index-not-found is normal on a brand-new cluster before
            # PermissionsIndexService.ensure_indices_exist() has run.
            # Treat as "no rows" so the seed path takes over.
            if "index_not_found" in str(e).lower() or "404" in str(e):
                return []
            logging.warning("WorkspaceMenuService._fetch_rows(%s): %s", workspace_esid, e)
            return []
        hits = resp.get("hits", {}).get("hits", [])
        return [h.get("_source", {}) for h in hits if h.get("_source")]

    # ------------------------------------------------------------------
    # Write — add / rename / reorder / delete
    # ------------------------------------------------------------------

    @staticmethod
    def add_menu(workspace_esid: str, title: str) -> dict:
        """Append a new menu at the end of the workspace's list.

        Returns the new menu row (esid + title + position).
        Raises :class:`DuplicateMenuTitleError` if the title is already
        used within the workspace (case-insensitive).
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("menu title must be non-empty")
        if not workspace_esid:
            raise ValueError("workspace_esid must be non-empty")

        es = get_elasticsearch_client()
        if es is None:
            raise WorkspaceMenuError("Elasticsearch unavailable")

        existing = WorkspaceMenuService._fetch_rows(es, workspace_esid)
        # follow-up: no auto-seed. If this is the workspace's
        # first menu, it lands at position 0 — the picker fall-through
        # to "Reports" covers the in-between state where the workspace
        # has zero menus.

        lowered = {r["menu_title"].strip().lower() for r in existing}
        if title.lower() in lowered:
            raise DuplicateMenuTitleError(f"Menu title '{title}' already exists in this workspace.")

        next_position = max((r["menu_position"] for r in existing), default=-1) + 1
        now = _now_iso()
        esid = _new_esid()
        doc = {
            "menu_esid": esid,
            "workspace_esid": workspace_esid,
            "menu_title": title,
            "menu_position": next_position,
            "created_at": now,
            "updated_at": now,
        }
        es.index(
            index=WORKSPACE_MENU_INDEX,
            id=esid,
            document=doc,
            refresh="wait_for",
        )
        return {"esid": esid, "title": title, "position": next_position}

    @staticmethod
    def rename_menu(workspace_esid: str, menu_esid: str, new_title: str) -> dict:
        """Rename a menu and cascade the change to every dashboard
        whose ``menu`` field referenced the old title within this
        workspace.

        Returns the updated row. Raises :class:`MenuNotFoundError`
        if no menu with the given esid exists in this workspace, or
        :class:`DuplicateMenuTitleError` if the new title collides.
        """
        new_title = (new_title or "").strip()
        if not new_title:
            raise ValueError("new menu title must be non-empty")

        es = get_elasticsearch_client()
        if es is None:
            raise WorkspaceMenuError("Elasticsearch unavailable")

        rows = WorkspaceMenuService._fetch_rows(es, workspace_esid)
        target = next((r for r in rows if r["menu_esid"] == menu_esid), None)
        if target is None:
            raise MenuNotFoundError(menu_esid)
        old_title = target["menu_title"]
        if new_title == old_title:
            return {
                "esid": menu_esid,
                "title": old_title,
                "position": target["menu_position"],
            }

        # Case-insensitive uniqueness (skipping the target itself so the
        # caller can re-case "reports" → "Reports").
        lowered = {r["menu_title"].strip().lower() for r in rows if r["menu_esid"] != menu_esid}
        if new_title.lower() in lowered:
            raise DuplicateMenuTitleError(f"Menu title '{new_title}' already exists in this workspace.")

        now = _now_iso()
        es.update(
            index=WORKSPACE_MENU_INDEX,
            id=menu_esid,
            doc={"menu_title": new_title, "updated_at": now},
            refresh="wait_for",
        )

        # Cascade: rewrite the dashboard.menu string on every dashboard
        # in THIS workspace whose menu matched. this used to
        # match the title across every workspace (one tenant's rename
        # relabelled every tenant's same-titled menu), and as a ``term``
        # on the analysed ``menu`` text field it missed any capitalised
        # or multi-word title. Every dashboard now carries workspace_esid.
        try:
            es.update_by_query(
                index=DASHBOARDS_INDEX,
                body={
                    "query": _dashboards_in_menu_query(workspace_esid, old_title),
                    "script": {
                        "source": "ctx._source.menu = params.new_title",
                        "lang": "painless",
                        "params": {"new_title": new_title},
                    },
                },
                refresh=True,
                conflicts="proceed",
            )
        except Exception as e:
            logging.warning(
                "WorkspaceMenuService.rename_menu cascade %s -> %s: %s",
                old_title,
                new_title,
                e,
            )

        return {
            "esid": menu_esid,
            "title": new_title,
            "position": target["menu_position"],
        }

    @staticmethod
    def reorder_menus(workspace_esid: str, ordered_esids: list[str]) -> None:
        """Persist a new menu order.

        Positions are reassigned 0..len-1 in the order ``ordered_esids``
        gives. Any menus in the workspace not present in
        ``ordered_esids`` keep their current positions (defensive — the
        caller is expected to supply the full list, but a partial list
        is non-destructive).

        Atomicity caveat: ES does not give us a transaction across N
        document updates. We accept that a crash mid-reorder can leave
        a half-reordered state; the next successful reorder cleans it
        up. The UI mitigates by reloading the list after each
        drag-drop.
        """
        if not workspace_esid:
            raise ValueError("workspace_esid must be non-empty")
        es = get_elasticsearch_client()
        if es is None:
            raise WorkspaceMenuError("Elasticsearch unavailable")

        rows = WorkspaceMenuService._fetch_rows(es, workspace_esid)
        by_esid = {r["menu_esid"]: r for r in rows}
        unknown = [esid for esid in ordered_esids if esid not in by_esid]
        if unknown:
            raise MenuNotFoundError(", ".join(unknown))

        now = _now_iso()
        for position, esid in enumerate(ordered_esids):
            existing = by_esid[esid]
            if existing["menu_position"] == position:
                continue
            es.update(
                index=WORKSPACE_MENU_INDEX,
                id=esid,
                doc={"menu_position": position, "updated_at": now},
                refresh="wait_for",
            )

    @staticmethod
    def reorder_menus_by_title(workspace_esid: str, ordered_titles: list[str]) -> None:
        """persist a new menu order keyed by menu TITLE, materializing
        a backing row for any title that doesn't have one yet.

        Why this exists alongside :meth:`reorder_menus` (which keys by
        ``menu_esid``): a workspace whose menus only exist IMPLICITLY — derived
        from the dashboards' ``menu`` strings, never created via "+ Add Menu" —
        has no ``de_asset_workspace_menu`` rows. The Builder still renders those
        groups (and drag), but every group's ``menu_esid`` is empty,
        so the esid-keyed reorder POST collected an empty list and silently did
        nothing — the drag reverted on reload with no error. (This was the
        long-standing gap surfaced while verifying.)

        Behavior: for each title in ``ordered_titles`` (the on-screen order),
        find its existing row case-insensitively or CREATE one, then assign
        ``menu_position`` 0..n-1. The first drag of an unconfigured workspace
        therefore materializes the whole menu set in the chosen order — the
        same "first reorder normalizes the group" contract
        :meth:`reorder_dashboards` uses for dashboards.

        Atomicity caveat: same as :meth:`reorder_menus` — N create/update calls,
        no transaction; a crash mid-reorder is cleaned up by the next one.
        """
        if not workspace_esid:
            raise ValueError("workspace_esid must be non-empty")
        es = get_elasticsearch_client()
        if es is None:
            raise WorkspaceMenuError("Elasticsearch unavailable")

        titles = [(t or "").strip() for t in ordered_titles if (t or "").strip()]
        if not titles:
            return

        rows = WorkspaceMenuService._fetch_rows(es, workspace_esid)
        by_lower = {r["menu_title"].strip().lower(): r for r in rows}

        now = _now_iso()
        for position, title in enumerate(titles):
            existing = by_lower.get(title.lower())
            if existing is None:
                # Materialize a row for this implicit menu at the right slot.
                esid = _new_esid()
                es.index(
                    index=WORKSPACE_MENU_INDEX,
                    id=esid,
                    document={
                        "menu_esid": esid,
                        "workspace_esid": workspace_esid,
                        "menu_title": title,
                        "menu_position": position,
                        "created_at": now,
                        "updated_at": now,
                    },
                    refresh="wait_for",
                )
                continue
            if existing.get("menu_position") == position:
                continue
            es.update(
                index=WORKSPACE_MENU_INDEX,
                id=existing["menu_esid"],
                doc={"menu_position": position, "updated_at": now},
                refresh="wait_for",
            )

    @staticmethod
    def reorder_dashboards(
        workspace_esid: str,
        menu_title: str,
        ordered_dashboard_ids: list[str],
    ) -> None:
        """persist a new order for the dashboards under one menu.

        The per-dashboard counterpart of :meth:`reorder_menus` — it writes
        ``menu_position`` onto ``de_asset_echarts_dashboards`` docs so the
        Builder left panel and the user-facing dashboards page (both of which
        already sort within a group by ``menu_position``, nulls last) honor a
        drag-reorder. Until the field was only ever written by
        seed/freeze scripts, so every dashboard's ``menu_position`` was
        ``null`` and the sidebar fell back to alphabetical-by-title.

        Granularity: scoped to a single ``(workspace_esid, menu_title)``
        group. ``ordered_dashboard_ids`` must be exactly the dashboards in
        that group, in the desired order; positions are reassigned
        ``0..n-1``.

        Scope enforcement: every supplied id must resolve to a dashboard in
        THIS workspace+menu. An id that does not (unknown, or belonging to a
        foreign workspace/menu) raises :class:`MenuNotFoundError` and nothing
        is written — a crafted payload cannot reposition a foreign-workspace
        dashboard.

        Backfill: existing dashboards have ``menu_position = null``. The first
        reorder of a menu materializes ``0..n-1`` for the whole group; there
        is no separate migration.

        Atomicity caveat: same as :meth:`reorder_menus` — N separate ES
        updates, no transaction. A crash mid-reorder can leave a half-applied
        order; the next successful reorder cleans it up and the UI reloads the
        list after each drop.
        """
        if not workspace_esid:
            raise ValueError("workspace_esid must be non-empty")
        menu_title = (menu_title or "").strip()
        if not menu_title:
            raise ValueError("menu_title must be non-empty")
        es = get_elasticsearch_client()
        if es is None:
            raise WorkspaceMenuError("Elasticsearch unavailable")

        # Read the group and key it by the same id the list payload exposes
        # (``dashboard_id`` falling back to ``_id`` — routes.list_dashboards),
        # while remembering the ES ``_id`` we must update by (the two usually
        # match but are not guaranteed to). Filtering in Python rather than a
        # ``term`` query because ``menu``/``workspace_esid`` on this
        # dynamically-mapped index are text+keyword, and a bare ``term`` on
        # the text field silently misses multi-token titles (a recurring
        # trap on de_asset_* fields).
        group = WorkspaceMenuService._dashboards_in_menu(es, workspace_esid, menu_title)
        es_id_by_dashboard_id = {}
        for doc_id, src in group:
            key = src.get("dashboard_id") or doc_id
            es_id_by_dashboard_id[key] = (doc_id, src.get("menu_position"))

        unknown = [d for d in ordered_dashboard_ids if d not in es_id_by_dashboard_id]
        if unknown:
            raise MenuNotFoundError(", ".join(unknown))

        now = _now_iso()
        for position, dashboard_id in enumerate(ordered_dashboard_ids):
            doc_id, current_position = es_id_by_dashboard_id[dashboard_id]
            if current_position == position:
                continue
            es.update(
                index=DASHBOARDS_INDEX,
                id=doc_id,
                doc={"menu_position": position, "updated_at": now},
                refresh="wait_for",
            )

    @staticmethod
    def _dashboards_in_menu(es, workspace_esid: str, menu_title: str) -> list[tuple[str, dict]]:
        """Return ``[(es_id, _source), ...]`` for the echarts dashboards the
        Builder shows under ``menu_title`` while the topbar is scoped to
        ``workspace_esid``.

        This MUST mirror the read path that renders the draggable items
        (``routes.list_dashboards`` + its topbar filter), or the
        write side rejects ids the user can clearly see — the exact mismatch
         first cut shipped: the read path lets a dashboard with NO
        ``workspace_esid`` stamp pass through (legacy + seeded dashboards
        like Trends are written ``show_all`` with no stamp, and hiding them
        would strand them), while a strict ``workspace_esid == selected``
        write filter excluded every one of them → "Unknown dashboard id(s)"
        on the first superuser drag.

        So the membership rule here is the read path's rule exactly: match
        the menu, and include a dashboard when its ``workspace_esid`` is
        BLANK (unattributable — belongs to whatever workspace the admin is
        viewing) OR equals the selected workspace. Filtered in Python (not a
        bare ``term``) because ``menu``/``workspace_esid`` on this
        dynamically-mapped index are text+keyword and a bare ``term`` on the
        text field silently misses multi-token titles.

        Returns ``[]`` on ES error / missing index (the caller then sees an
        empty group and any supplied id is rejected as unknown — fail-closed).
        """
        try:
            resp = es.search(
                index=DASHBOARDS_INDEX,
                body={
                    "query": {"match_all": {}},
                    "size": 5000,
                    "_source": [
                        "dashboard_id",
                        "dashboard_type",
                        "menu",
                        "menu_position",
                        "workspace_esid",
                    ],
                },
            )
        except Exception as e:
            logging.warning(
                "WorkspaceMenuService._dashboards_in_menu(%s, %s): %s",
                workspace_esid,
                menu_title,
                e,
            )
            return []
        # Mirror the client's bucketing: builder.js drops a dashboard whose
        # ``menu`` is blank/missing into the synthetic ``"Reports"`` group, so
        # a drag inside that bucket POSTs ``menu="Reports"`` for a doc whose
        # stored ``menu`` is "". Normalize the same way here, or those
        # blank-menu dashboards 400 as "unknown" the moment they're dragged.
        wanted = (menu_title or "").strip() or "Reports"
        out = []
        for h in resp.get("hits", {}).get("hits", []):
            doc_id = h.get("_id", "")
            src = h.get("_source", {})
            if not doc_id:
                continue
            # "canvas" mirrors list_dashboards (added it
            # there) — without it a canvas row visible in the Builder menu
            # editor 400s as "unknown dashboard id" on the first drag.
            if src.get("dashboard_type") not in ("echarts", "leaflet_map", "canvas"):
                continue
            doc_menu = (src.get("menu") or "").strip() or "Reports"
            if doc_menu != wanted:
                continue
            # Read-path workspace rule: blank stamp passes through; otherwise
            # must equal the selected workspace.
            doc_ws = src.get("workspace_esid") or ""
            if doc_ws and doc_ws != workspace_esid:
                continue
            out.append((doc_id, src))
        return out

    @staticmethod
    def next_dashboard_position(workspace_esid: str, menu_title: str) -> int:
        """the ``menu_position`` a dashboard should take to land at
        the END of ``(workspace_esid, menu_title)``.

        Used when a dashboard is MOVED into a menu (Properties ``menu`` edit)
        so it appends rather than keeping a stale position that could strand
        it mid-list or collide. Returns ``max(existing positions) + 1``,
        treating ``null`` positions as not-yet-materialized (they sort last
        today, so appending past the highest real position keeps the moved
        dashboard at the bottom). Returns ``0`` for an empty / unresolvable
        group.
        """
        if not workspace_esid or not (menu_title or "").strip():
            return 0
        es = get_elasticsearch_client()
        if es is None:
            return 0
        group = WorkspaceMenuService._dashboards_in_menu(es, workspace_esid, menu_title.strip())
        positions = [src.get("menu_position") for _doc_id, src in group if isinstance(src.get("menu_position"), int)]
        return (max(positions) + 1) if positions else 0

    @staticmethod
    def delete_menu(workspace_esid: str, menu_esid: str) -> None:
        """Delete a menu. Refuses (and raises :class:`MenuNotEmptyError`)
        if any dashboard in the workspace still references the menu's
        title — the workspace admin must move or delete those dashboards
        first.

        Raises :class:`MenuNotFoundError` if no menu with the given esid
        exists in this workspace.
        """
        es = get_elasticsearch_client()
        if es is None:
            raise WorkspaceMenuError("Elasticsearch unavailable")

        rows = WorkspaceMenuService._fetch_rows(es, workspace_esid)
        target = next((r for r in rows if r["menu_esid"] == menu_esid), None)
        if target is None:
            raise MenuNotFoundError(menu_esid)

        # Refuse if dashboards in this workspace still reference this
        # title. exact title on ``menu.keyword``, scoped to the
        # workspace (the old ``term`` on the analysed text field counted 0
        # for any capitalised title, so an in-use menu could be deleted).
        title = target["menu_title"]
        try:
            count_resp = es.count(
                index=DASHBOARDS_INDEX,
                body={"query": _dashboards_in_menu_query(workspace_esid, title)},
            )
            n = int(count_resp.get("count", 0) or 0)
        except Exception as e:
            logging.warning("WorkspaceMenuService.delete_menu count(%s): %s", title, e)
            # Fail safe: refuse the delete on lookup error rather than
            # silently dropping the row.
            raise WorkspaceMenuError("Could not verify menu is empty; refusing to delete.") from e
        if n > 0:
            raise MenuNotEmptyError(title, n)

        es.delete(index=WORKSPACE_MENU_INDEX, id=menu_esid, refresh="wait_for")
