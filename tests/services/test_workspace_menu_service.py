"""Unit tests for WorkspaceMenuService.

 follow-up 2026-05-19: the auto-seed of nine default menus on
first read was removed. Tests no longer reference DEFAULT_MENUS; they
populate fixtures via explicit ``add_menu`` calls instead.
"""

from __future__ import annotations

from unittest import mock

import pytest

from asset_management.web.services.workspace_menu_service import (
    DuplicateMenuTitleError,
    MenuNotEmptyError,
    MenuNotFoundError,
    WorkspaceMenuError,
    WorkspaceMenuService,
)


# ---------------------------------------------------------------------------
# Fake ES double — keeps an in-memory list of docs and an in-memory dashboard
# counter, sufficient for every WorkspaceMenuService code path.
# ---------------------------------------------------------------------------



def _menu_title_of(query: dict) -> str:
    """the menu query is ``bool.filter[workspace_esid, menu.keyword]``;
    a query without the workspace filter is the cross-tenant bug, so refuse it."""
    terms = {k: v for f in query["bool"]["filter"] for k, v in f.get("term", {}).items()}
    assert terms.get("workspace_esid"), f"menu query not scoped to a workspace: {query}"
    return terms["menu.keyword"]

class _FakeES:
    def __init__(self, menus=None, dashboard_menu_counts=None):
        # workspace menu docs, list of (id, source)
        self.menus = list(menus or [])
        # mapping of menu_title → count of dashboards with that menu
        self.dashboard_menu_counts = dict(dashboard_menu_counts or {})
        # capture write side effects for assertions
        self.update_by_query_calls: list[dict] = []
        self.delete_calls: list[tuple[str, str]] = []

    # ---- WORKSPACE_MENU_INDEX queries ------------------------------------

    def search(self, index, body):
        if index == "de_asset_workspace_menu":
            ws = body["query"]["term"]["workspace_esid"]
            matching = [
                (i, s) for (i, s) in self.menus if s["workspace_esid"] == ws
            ]
            matching.sort(
                key=lambda x: (x[1]["menu_position"], x[1]["menu_title"])
            )
            return {
                "hits": {
                    "hits": [
                        {"_id": i, "_source": s} for (i, s) in matching
                    ]
                }
            }
        raise AssertionError(f"unexpected search index {index!r}")

    def index(self, index, id, document, refresh="wait_for", op_type=None):
        if index != "de_asset_workspace_menu":
            raise AssertionError(f"unexpected index() target {index!r}")
        # Replace if exists (mimics PUT semantics)
        self.menus = [(i, s) for (i, s) in self.menus if i != id]
        self.menus.append((id, dict(document)))

    def update(self, index, id, doc, refresh="wait_for"):
        if index != "de_asset_workspace_menu":
            raise AssertionError(f"unexpected update() target {index!r}")
        for i, (mid, src) in enumerate(self.menus):
            if mid == id:
                new_src = {**src, **doc}
                self.menus[i] = (mid, new_src)
                return
        raise Exception("not_found")

    def delete(self, index, id, refresh="wait_for"):
        self.delete_calls.append((index, id))
        if index != "de_asset_workspace_menu":
            raise AssertionError(f"unexpected delete() target {index!r}")
        self.menus = [(i, s) for (i, s) in self.menus if i != id]

    # ---- DASHBOARDS_INDEX queries (count + update_by_query) --------------

    def count(self, index, body):
        if index != "de_asset_echarts_dashboards":
            raise AssertionError(f"unexpected count() target {index!r}")
        title = _menu_title_of(body["query"])
        return {"count": self.dashboard_menu_counts.get(title, 0)}

    def update_by_query(self, index, body, refresh=True, conflicts="proceed"):
        self.update_by_query_calls.append({"index": index, "body": body})
        # Cascade simulation: rewrite the count key from old → new title.
        title = _menu_title_of(body["query"])
        new_title = body["script"]["params"]["new_title"]
        n = self.dashboard_menu_counts.pop(title, 0)
        self.dashboard_menu_counts[new_title] = (
            self.dashboard_menu_counts.get(new_title, 0) + n
        )


def _patch_es(fake):
    return mock.patch(
        "asset_management.web.services.workspace_menu_service.get_elasticsearch_client",
        return_value=fake,
    )


def _populate(fake, workspace_esid, titles):
    """Explicit-fixture helper: replaces the earlier list_menus auto-seed.

    Calls add_menu in order so positions are 0..n-1, matching what
    the auto-seed used to do.
    """
    with _patch_es(fake):
        for title in titles:
            WorkspaceMenuService.add_menu(workspace_esid, title)


# ---------------------------------------------------------------------------
# list_menus — no auto-seed
# ---------------------------------------------------------------------------


class TestListMenus:

    def test_empty_workspace_returns_empty_list_no_auto_seed(self):
        """follow-up: brand-new workspace has zero menus.

        The picker fall-through to ``"Reports"`` covers the in-between
        state. The service no longer auto-seeds defaults.
        """
        fake = _FakeES()
        with _patch_es(fake):
            menus = WorkspaceMenuService.list_menus("ws1")
        assert menus == []
        # No side-effect writes happened either.
        assert fake.menus == []

    def test_returns_added_menus_in_position_order(self):
        fake = _FakeES()
        _populate(fake, "ws1", ["Overview", "Meta", "Reports"])
        with _patch_es(fake):
            menus = WorkspaceMenuService.list_menus("ws1")
        assert [m["title"] for m in menus] == ["Overview", "Meta", "Reports"]
        assert [m["position"] for m in menus] == [0, 1, 2]

    def test_es_unavailable_returns_empty_list(self):
        with mock.patch(
            "asset_management.web.services.workspace_menu_service.get_elasticsearch_client",
            return_value=None,
        ):
            assert WorkspaceMenuService.list_menus("ws1") == []

    def test_empty_workspace_esid_returns_empty_list(self):
        with _patch_es(_FakeES()):
            assert WorkspaceMenuService.list_menus("") == []


# ---------------------------------------------------------------------------
# add_menu
# ---------------------------------------------------------------------------


class TestAddMenu:

    def test_appends_at_end(self):
        fake = _FakeES()
        _populate(fake, "ws1", ["Overview", "Meta", "Reports"])
        with _patch_es(fake):
            row = WorkspaceMenuService.add_menu("ws1", "Custom")
        assert row["position"] == 3
        assert row["title"] == "Custom"

    def test_rejects_duplicate_title_case_insensitive(self):
        fake = _FakeES()
        _populate(fake, "ws1", ["Meta"])
        with _patch_es(fake):
            with pytest.raises(DuplicateMenuTitleError):
                WorkspaceMenuService.add_menu("ws1", "meta")

    def test_first_menu_on_empty_workspace_lands_at_position_zero(self):
        """follow-up: no auto-seed before the first add."""
        fake = _FakeES()
        with _patch_es(fake):
            row = WorkspaceMenuService.add_menu("ws1", "Custom")
        # Single row only — no seed prefix.
        assert len(fake.menus) == 1
        assert row["position"] == 0
        assert row["title"] == "Custom"

    def test_blank_title_raises_value_error(self):
        with _patch_es(_FakeES()):
            with pytest.raises(ValueError):
                WorkspaceMenuService.add_menu("ws1", "   ")

    def test_es_unavailable_raises(self):
        with mock.patch(
            "asset_management.web.services.workspace_menu_service.get_elasticsearch_client",
            return_value=None,
        ):
            with pytest.raises(WorkspaceMenuError):
                WorkspaceMenuService.add_menu("ws1", "Custom")


# ---------------------------------------------------------------------------
# rename_menu
# ---------------------------------------------------------------------------


class TestRenameMenu:

    def test_rename_updates_row_and_cascades_dashboard_menu_field(self):
        fake = _FakeES(dashboard_menu_counts={"Meta": 5})
        _populate(fake, "ws1", ["Meta", "Reports"])
        with _patch_es(fake):
            menus = WorkspaceMenuService.list_menus("ws1")
            meta_esid = next(m["esid"] for m in menus if m["title"] == "Meta")
            row = WorkspaceMenuService.rename_menu("ws1", meta_esid, "Facebook")
        assert row["title"] == "Facebook"
        # Dashboards rewritten: cascade ran exactly once.
        assert len(fake.update_by_query_calls) == 1
        # And the count moved from "Meta" → "Facebook".
        assert fake.dashboard_menu_counts.get("Meta", 0) == 0
        assert fake.dashboard_menu_counts.get("Facebook") == 5

    def test_rename_to_existing_title_raises(self):
        fake = _FakeES()
        _populate(fake, "ws1", ["Meta", "Google"])
        with _patch_es(fake):
            menus = WorkspaceMenuService.list_menus("ws1")
            meta_esid = next(m["esid"] for m in menus if m["title"] == "Meta")
            with pytest.raises(DuplicateMenuTitleError):
                WorkspaceMenuService.rename_menu("ws1", meta_esid, "Google")

    def test_rename_unknown_esid_raises(self):
        fake = _FakeES()
        _populate(fake, "ws1", ["Meta"])
        with _patch_es(fake):
            with pytest.raises(MenuNotFoundError):
                WorkspaceMenuService.rename_menu("ws1", "nope", "Whatever")

    def test_rename_to_same_title_noop(self):
        fake = _FakeES(dashboard_menu_counts={"Meta": 5})
        _populate(fake, "ws1", ["Meta"])
        with _patch_es(fake):
            menus = WorkspaceMenuService.list_menus("ws1")
            meta_esid = next(m["esid"] for m in menus if m["title"] == "Meta")
            WorkspaceMenuService.rename_menu("ws1", meta_esid, "Meta")
        # No cascade fired — title didn't change.
        assert fake.update_by_query_calls == []


# ---------------------------------------------------------------------------
# reorder_menus
# ---------------------------------------------------------------------------


class TestReorderMenus:

    def test_rewrites_positions_in_given_order(self):
        fake = _FakeES()
        titles = ["Overview", "Meta", "Reports"]
        _populate(fake, "ws1", titles)
        with _patch_es(fake):
            menus = WorkspaceMenuService.list_menus("ws1")
            # Reverse the order.
            reversed_esids = [m["esid"] for m in reversed(menus)]
            WorkspaceMenuService.reorder_menus("ws1", reversed_esids)
            after = WorkspaceMenuService.list_menus("ws1")
        assert [m["title"] for m in after] == list(reversed(titles))

    def test_unknown_esid_raises_menu_not_found(self):
        fake = _FakeES()
        _populate(fake, "ws1", ["Meta"])
        with _patch_es(fake):
            with pytest.raises(MenuNotFoundError):
                WorkspaceMenuService.reorder_menus("ws1", ["nope"])

    def test_empty_workspace_esid_raises_value_error(self):
        with _patch_es(_FakeES()):
            with pytest.raises(ValueError):
                WorkspaceMenuService.reorder_menus("", [])


class TestReorderMenusByTitle:
    """title-keyed reorder that materializes rows for implicit menus.

    Regression: a workspace with NO de_asset_workspace_menu rows (menus exist
    only as dashboards' ``menu`` strings) rendered the drag but had
    empty menu_esids, so the esid-keyed reorder POSTed nothing and the drag
    silently reverted on reload.
    """

    def test_materializes_rows_for_implicit_menus_in_given_order(self):
        # No rows exist yet — the workspace's menus are purely implicit.
        fake = _FakeES()
        with _patch_es(fake):
            WorkspaceMenuService.reorder_menus_by_title("ws1", ["Meta", "Overview", "Reports"])
            menus = WorkspaceMenuService.list_menus("ws1")
        # Rows were created and ordered exactly as dragged.
        assert [m["title"] for m in menus] == ["Meta", "Overview", "Reports"]
        assert [m["position"] for m in menus] == [0, 1, 2]

    def test_reorders_existing_rows_and_matches_case_insensitively(self):
        fake = _FakeES()
        _populate(fake, "ws1", ["Overview", "Meta", "Reports"])
        with _patch_es(fake):
            # Drag to reverse, supplying titles in a different case.
            WorkspaceMenuService.reorder_menus_by_title("ws1", ["reports", "META", "overview"])
            menus = WorkspaceMenuService.list_menus("ws1")
        assert [m["title"] for m in menus] == ["Reports", "Meta", "Overview"]
        assert [m["position"] for m in menus] == [0, 1, 2]
        # No duplicate rows were created by the case-mismatched titles.
        assert len(fake.menus) == 3

    def test_mixed_existing_and_new_titles(self):
        fake = _FakeES()
        _populate(fake, "ws1", ["Overview"])  # one real row at position 0
        with _patch_es(fake):
            WorkspaceMenuService.reorder_menus_by_title("ws1", ["Meta", "Overview", "Reports"])
            menus = WorkspaceMenuService.list_menus("ws1")
        assert [m["title"] for m in menus] == ["Meta", "Overview", "Reports"]
        assert [m["position"] for m in menus] == [0, 1, 2]
        assert len(fake.menus) == 3  # Meta + Reports materialized, Overview reused

    def test_empty_workspace_esid_raises_value_error(self):
        with _patch_es(_FakeES()):
            with pytest.raises(ValueError):
                WorkspaceMenuService.reorder_menus_by_title("", ["Meta"])


# ---------------------------------------------------------------------------
# delete_menu
# ---------------------------------------------------------------------------


class TestDeleteMenu:

    def test_delete_empty_menu_succeeds(self):
        fake = _FakeES()
        _populate(fake, "ws1", ["Meta", "Reports"])
        with _patch_es(fake):
            menus = WorkspaceMenuService.list_menus("ws1")
            reports_esid = next(m["esid"] for m in menus if m["title"] == "Reports")
            WorkspaceMenuService.delete_menu("ws1", reports_esid)
        # Row is gone.
        assert reports_esid not in [i for (i, _s) in fake.menus]

    def test_delete_refuses_when_menu_has_dashboards(self):
        fake = _FakeES(dashboard_menu_counts={"Meta": 3})
        _populate(fake, "ws1", ["Meta"])
        with _patch_es(fake):
            menus = WorkspaceMenuService.list_menus("ws1")
            meta_esid = next(m["esid"] for m in menus if m["title"] == "Meta")
            with pytest.raises(MenuNotEmptyError) as excinfo:
                WorkspaceMenuService.delete_menu("ws1", meta_esid)
        assert excinfo.value.dashboards_in_menu == 3
        # And the row is NOT deleted.
        assert meta_esid in [i for (i, _s) in fake.menus]

    def test_delete_unknown_esid_raises(self):
        fake = _FakeES()
        _populate(fake, "ws1", ["Meta"])
        with _patch_es(fake):
            with pytest.raises(MenuNotFoundError):
                WorkspaceMenuService.delete_menu("ws1", "nope")
