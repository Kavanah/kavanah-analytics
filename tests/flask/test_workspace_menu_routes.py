"""Flask route tests for the workspace-menu API.

Endpoints under test (Dashboard Builder blueprint):

  GET    /dashboard-builder/api/menus
  POST   /dashboard-builder/api/menus
  PATCH  /dashboard-builder/api/menus/<menu_esid>
  DELETE /dashboard-builder/api/menus/<menu_esid>
  POST   /dashboard-builder/api/menus/reorder

Authz model:

* Read (GET): ``mc_dashboard_builder`` — provided by asset_admin
  (the default ``auth_client`` session).
* Write (POST / PATCH / DELETE / reorder): ALSO requires
  ``workspace_admin`` or ``workspace_owner`` on the resolved workspace,
  EXCEPT superusers, who bypass the membership check. The
  resolved workspace is the client-supplied ``workspace_esid`` when it is
  in ``get_allowed_workspaces`` (None = privileged/all), else the user's
  primary workspace — see ``_menu_write_workspace_or_abort``. The tests
  below pin a primary workspace and supply no ``workspace_esid``, so they
  exercise the primary-workspace fallback branch unchanged.

ES is mocked. ``WorkspaceMembershipService`` is patched at the routes
module level so we control the (username → workspace, workspace → role)
join without exercising the auth indices.
"""

from __future__ import annotations

from unittest import mock

import pytest


_R = "asset_management.web.blueprints.dashboard_builder.routes"
_SVC = "asset_management.web.services.workspace_menu_service"
_WS_MEMBERSHIP = (
    "asset_management.web.services.workspace_membership_service.WorkspaceMembershipService"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workspace_ctx(role: str | None = "workspace_admin"):
    """Patch the (username → workspace, workspace → role) lookups.

    ``role`` is the role esid ``get_role`` returns; pass ``None`` for an
    unprivileged workspace member.
    """
    return [
        mock.patch(
            f"{_WS_MEMBERSHIP}.get_primary_workspace",
            return_value="ws1",
        ),
        mock.patch(
            f"{_WS_MEMBERSHIP}.get_role",
            return_value=role,
        ),
    ]


def _enter(stack):
    return [cm.__enter__() for cm in stack]


def _exit(stack):
    for cm in reversed(stack):
        cm.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# GET /api/menus — read gate, seed-on-first-read
# ---------------------------------------------------------------------------


class TestListMenus:
    def test_returns_seeded_menus_for_workspace(self, auth_client):
        stack = _workspace_ctx(role=None)  # read gate doesn't check role
        _enter(stack)
        try:
            with mock.patch(
                f"{_SVC}.WorkspaceMenuService.list_menus",
                return_value=[
                    {"esid": "m1", "title": "Overview", "position": 0},
                    {"esid": "m2", "title": "Meta", "position": 1},
                ],
            ):
                resp = auth_client.get("/dashboard-builder/api/menus")
        finally:
            _exit(stack)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert [m["title"] for m in data["menus"]] == ["Overview", "Meta"]

    def test_no_primary_falls_back_to_resolver_menus(self, auth_client):
        """a None primary workspace (org-admin / team-grant /
        grant-holder, earlier) no longer 403s the read path — it
        falls back to the ``get_allowed_workspaces`` walk and returns the
        first resolved workspace's menus, so the Builder panel renders the
        configured order instead of collapsing to alphabetical.
        """
        with mock.patch(
            f"{_WS_MEMBERSHIP}.get_primary_workspace",
            return_value=None,
        ), mock.patch(
            f"{_R}.RoleFilterService.get_allowed_workspaces",
            return_value=["kavanah-media"],
        ), mock.patch(
            f"{_SVC}.WorkspaceMenuService.list_menus",
            return_value=[
                {"esid": "m1", "title": "Overview", "position": 0},
                {"esid": "m2", "title": "Meta", "position": 1},
            ],
        ):
            resp = auth_client.get("/dashboard-builder/api/menus")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert [m["title"] for m in data["menus"]] == ["Overview", "Meta"]

    def test_returns_empty_when_nothing_resolves(self, auth_client):
        """with no primary AND no resolvable workspace, the read
        path degrades to an empty list (the picker falls through to its
        synthetic ``Reports`` default) — it must NOT 403 or raise.
        """
        with mock.patch(
            f"{_WS_MEMBERSHIP}.get_primary_workspace",
            return_value=None,
        ), mock.patch(
            f"{_R}.RoleFilterService.get_allowed_workspaces",
            return_value=None,
        ):
            resp = auth_client.get("/dashboard-builder/api/menus")
        assert resp.status_code == 200
        assert resp.get_json()["menus"] == []


# ---------------------------------------------------------------------------
# POST /api/menus — write gate
# ---------------------------------------------------------------------------


class TestAddMenu:
    def test_workspace_admin_can_add(self, auth_client):
        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            with mock.patch(
                f"{_SVC}.WorkspaceMenuService.add_menu",
                return_value={"esid": "new1", "title": "Custom", "position": 9},
            ):
                resp = auth_client.post(
                    "/dashboard-builder/api/menus", json={"title": "Custom"}
                )
        finally:
            _exit(stack)
        assert resp.status_code == 200
        assert resp.get_json()["menu"]["title"] == "Custom"

    def test_workspace_owner_can_add(self, auth_client):
        stack = _workspace_ctx(role="workspace_owner")
        _enter(stack)
        try:
            with mock.patch(
                f"{_SVC}.WorkspaceMenuService.add_menu",
                return_value={"esid": "n", "title": "T", "position": 9},
            ):
                resp = auth_client.post(
                    "/dashboard-builder/api/menus", json={"title": "T"}
                )
        finally:
            _exit(stack)
        assert resp.status_code == 200

    def test_non_workspace_admin_403s(self, auth_client):
        stack = _workspace_ctx(role="workspace_member")
        _enter(stack)
        try:
            resp = auth_client.post(
                "/dashboard-builder/api/menus", json={"title": "Custom"}
            )
        finally:
            _exit(stack)
        assert resp.status_code == 403
        assert "Workspace admin" in resp.get_json()["error"]

    def test_no_role_403s(self, auth_client):
        stack = _workspace_ctx(role=None)
        _enter(stack)
        try:
            resp = auth_client.post(
                "/dashboard-builder/api/menus", json={"title": "Custom"}
            )
        finally:
            _exit(stack)
        assert resp.status_code == 403

    def test_blank_title_400(self, auth_client):
        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            resp = auth_client.post(
                "/dashboard-builder/api/menus", json={"title": "   "}
            )
        finally:
            _exit(stack)
        assert resp.status_code == 400

    def test_duplicate_title_409(self, auth_client):
        from asset_management.web.services.workspace_menu_service import (
            DuplicateMenuTitleError,
        )

        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            with mock.patch(
                f"{_SVC}.WorkspaceMenuService.add_menu",
                side_effect=DuplicateMenuTitleError("already exists"),
            ):
                resp = auth_client.post(
                    "/dashboard-builder/api/menus", json={"title": "Meta"}
                )
        finally:
            _exit(stack)
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# PATCH /api/menus/<esid> — rename
# ---------------------------------------------------------------------------


class TestRenameMenu:
    def test_workspace_admin_can_rename(self, auth_client):
        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            with mock.patch(
                f"{_SVC}.WorkspaceMenuService.rename_menu",
                return_value={"esid": "m1", "title": "Facebook", "position": 1},
            ):
                resp = auth_client.patch(
                    "/dashboard-builder/api/menus/m1", json={"title": "Facebook"}
                )
        finally:
            _exit(stack)
        assert resp.status_code == 200
        assert resp.get_json()["menu"]["title"] == "Facebook"

    def test_unknown_esid_404(self, auth_client):
        from asset_management.web.services.workspace_menu_service import (
            MenuNotFoundError,
        )

        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            with mock.patch(
                f"{_SVC}.WorkspaceMenuService.rename_menu",
                side_effect=MenuNotFoundError("nope"),
            ):
                resp = auth_client.patch(
                    "/dashboard-builder/api/menus/nope", json={"title": "X"}
                )
        finally:
            _exit(stack)
        assert resp.status_code == 404

    def test_non_admin_403(self, auth_client):
        stack = _workspace_ctx(role=None)
        _enter(stack)
        try:
            resp = auth_client.patch(
                "/dashboard-builder/api/menus/m1", json={"title": "X"}
            )
        finally:
            _exit(stack)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /api/menus/<esid> — delete (refuses non-empty)
# ---------------------------------------------------------------------------


class TestDeleteMenu:
    def test_delete_empty_menu_succeeds(self, auth_client):
        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            with mock.patch(f"{_SVC}.WorkspaceMenuService.delete_menu"):
                resp = auth_client.delete("/dashboard-builder/api/menus/m1")
        finally:
            _exit(stack)
        assert resp.status_code == 200

    def test_delete_non_empty_menu_409_with_count(self, auth_client):
        from asset_management.web.services.workspace_menu_service import (
            MenuNotEmptyError,
        )

        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            with mock.patch(
                f"{_SVC}.WorkspaceMenuService.delete_menu",
                side_effect=MenuNotEmptyError("Meta", 3),
            ):
                resp = auth_client.delete("/dashboard-builder/api/menus/m1")
        finally:
            _exit(stack)
        assert resp.status_code == 409
        assert resp.get_json()["dashboards_in_menu"] == 3

    def test_non_admin_403(self, auth_client):
        stack = _workspace_ctx(role=None)
        _enter(stack)
        try:
            resp = auth_client.delete("/dashboard-builder/api/menus/m1")
        finally:
            _exit(stack)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/menus/reorder
# ---------------------------------------------------------------------------


class TestPropertiesPatchAcceptsMenu:
    """PATCH /api/dashboards/<id>/properties — added ``menu`` to
    the allowed_fields whitelist so the Dashboard Properties panel can
    move an existing dashboard between menus.
    """

    def test_patch_with_menu_persists(self, auth_client):
        # The properties endpoint loads the dashboard via _load_dashboard_for_read
        # then calls es.update(...). We mock both: get_document_by_id returns
        # a show_all dashboard (so the read gate passes for any user) and
        # capture the doc that es.update receives.
        # the PATCH now also runs the per-dashboard mutate-scope gate;
        # this test covers menu/title persistence, not authz, so treat the test
        # admin as a workspace admin to pass the gate.
        mock_es = mock.MagicMock()
        captured_doc = {}

        def _capture_update(*, index, id, doc, refresh):
            captured_doc.update(doc)

        mock_es.update.side_effect = _capture_update

        with mock.patch(
            f"{_R}.get_document_by_id",
            return_value={
                "dashboard_id": "d1",
                "dashboard_type": "echarts",
                "title": "Old",
                "menu": "Reports",
                "visibility_mode": "show_all",
            },
        ), mock.patch(f"{_R}.get_elasticsearch_client", return_value=mock_es), mock.patch(
            f"{_R}._current_user_is_workspace_admin", return_value=True
        ), mock.patch(
            # per-workspace admin bypass; the fixture doc has no workspace_esid.
            f"{_R}._current_user_admins_dashboard_workspace",
            return_value=True,
        ):
            resp = auth_client.patch(
                "/dashboard-builder/api/dashboards/d1/properties",
                json={"menu": "Meta", "title": "New"},
            )

        assert resp.status_code == 200
        # Both menu and title made it through the allowed_fields filter.
        assert captured_doc.get("menu") == "Meta"
        assert captured_doc.get("title") == "New"
        # And the response advertises what changed.
        updated = resp.get_json().get("updated", [])
        assert "menu" in updated
        assert "title" in updated

    def test_patch_with_only_unknown_fields_400(self, auth_client):
        """Sanity-check that the allowed_fields filter is still active —
        an arbitrary key should NOT flow through just because we added
        ``menu`` to the set.
        """
        with mock.patch(
            f"{_R}.get_document_by_id",
            return_value={
                "dashboard_id": "d1",
                "dashboard_type": "echarts",
                "title": "X",
                "visibility_mode": "show_all",
            },
        ), mock.patch(f"{_R}.get_elasticsearch_client", return_value=mock.MagicMock()), mock.patch(
            f"{_R}._current_user_is_workspace_admin", return_value=True
        ), mock.patch(
            # per-workspace admin bypass; the fixture doc has no workspace_esid.
            f"{_R}._current_user_admins_dashboard_workspace",
            return_value=True,
        ):
            resp = auth_client.patch(
                "/dashboard-builder/api/dashboards/d1/properties",
                json={"some_unknown_field": "x"},
            )
        assert resp.status_code == 400


class TestSaveDashboardMenuFlow:
    """POST /api/dashboards — makes ``data["menu"]`` actually
    flow through at create-time (previously hardcoded to "Reports").
    """

    def _save_capture(self):
        """Build a mock_es whose .index() captures the persisted doc and
        whose .get() raises a 'not_found' for new-document mode.
        """
        mock_es = mock.MagicMock()
        captured = {}

        class _NotFound(Exception):
            pass

        mock_es.get.side_effect = _NotFound("not_found")

        def _capture_index(*, index, id, document, refresh):
            captured["index"] = index
            captured["id"] = id
            captured["doc"] = document

        mock_es.index.side_effect = _capture_index
        return mock_es, captured

    def test_create_with_payload_menu_persists(self, auth_client):
        mock_es, captured = self._save_capture()
        with mock.patch(f"{_R}.get_elasticsearch_client", return_value=mock_es):
            resp = auth_client.post(
                "/dashboard-builder/api/dashboards",
                json={
                    "title": "My Dashboard",
                    "menu": "Custom Menu",
                    "charts": [],
                },
            )
        assert resp.status_code == 200
        assert captured["doc"]["menu"] == "Custom Menu"

    def test_create_without_payload_menu_falls_back_to_workspace_first_menu(
        self, auth_client
    ):
        mock_es, captured = self._save_capture()
        with mock.patch(
            f"{_R}.get_elasticsearch_client", return_value=mock_es
        ), mock.patch(
            f"{_WS_MEMBERSHIP}.get_primary_workspace", return_value="ws1"
        ), mock.patch(
            f"{_SVC}.WorkspaceMenuService.list_menus",
            return_value=[
                {"esid": "m1", "title": "Overview", "position": 0},
                {"esid": "m2", "title": "Meta", "position": 1},
            ],
        ):
            resp = auth_client.post(
                "/dashboard-builder/api/dashboards",
                json={"title": "My Dashboard", "charts": []},
            )
        assert resp.status_code == 200
        assert captured["doc"]["menu"] == "Overview"

    def test_create_without_payload_menu_falls_back_to_reports_when_no_workspace(
        self, auth_client
    ):
        mock_es, captured = self._save_capture()
        with mock.patch(
            f"{_R}.get_elasticsearch_client", return_value=mock_es
        ), mock.patch(
            f"{_WS_MEMBERSHIP}.get_primary_workspace", return_value=None
        ):
            resp = auth_client.post(
                "/dashboard-builder/api/dashboards",
                json={"title": "My Dashboard", "charts": []},
            )
        assert resp.status_code == 200
        assert captured["doc"]["menu"] == "Reports"


class TestReorderMenus:
    def test_workspace_admin_can_reorder(self, auth_client):
        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            with mock.patch(f"{_SVC}.WorkspaceMenuService.reorder_menus"):
                resp = auth_client.post(
                    "/dashboard-builder/api/menus/reorder",
                    json={"ordered_esids": ["m1", "m2", "m3"]},
                )
        finally:
            _exit(stack)
        assert resp.status_code == 200

    def test_unknown_esid_400(self, auth_client):
        from asset_management.web.services.workspace_menu_service import (
            MenuNotFoundError,
        )

        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            with mock.patch(
                f"{_SVC}.WorkspaceMenuService.reorder_menus",
                side_effect=MenuNotFoundError("nope"),
            ):
                resp = auth_client.post(
                    "/dashboard-builder/api/menus/reorder",
                    json={"ordered_esids": ["nope"]},
                )
        finally:
            _exit(stack)
        assert resp.status_code == 400

    def test_non_admin_403(self, auth_client):
        stack = _workspace_ctx(role=None)
        _enter(stack)
        try:
            resp = auth_client.post(
                "/dashboard-builder/api/menus/reorder",
                json={"ordered_esids": []},
            )
        finally:
            _exit(stack)
        assert resp.status_code == 403

    def test_non_list_400(self, auth_client):
        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            resp = auth_client.post(
                "/dashboard-builder/api/menus/reorder",
                json={"ordered_esids": "not-a-list"},
            )
        finally:
            _exit(stack)
        assert resp.status_code == 400

    def test_ordered_titles_routes_to_by_title_service(self, auth_client):
        """an ``ordered_titles`` body uses the materializing
        title-keyed reorder (not the esid path), so implicit menus with no
        backing row are persisted on the first drag instead of silently
        reverting.

        Mutation: route ``ordered_titles`` through ``reorder_menus`` (esid
        path) instead -> ``reorder_menus_by_title`` is not called and this
        assert fails.
        """
        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            with mock.patch(
                f"{_SVC}.WorkspaceMenuService.reorder_menus_by_title"
            ) as by_title, mock.patch(f"{_SVC}.WorkspaceMenuService.reorder_menus") as by_esid:
                resp = auth_client.post(
                    "/dashboard-builder/api/menus/reorder",
                    json={"ordered_titles": ["Meta", "Overview", "Reports"]},
                )
        finally:
            _exit(stack)
        assert resp.status_code == 200
        by_title.assert_called_once_with("ws1", ["Meta", "Overview", "Reports"])
        by_esid.assert_not_called()

    def test_ordered_titles_non_list_400(self, auth_client):
        stack = _workspace_ctx(role="workspace_admin")
        _enter(stack)
        try:
            resp = auth_client.post(
                "/dashboard-builder/api/menus/reorder",
                json={"ordered_titles": "not-a-list"},
            )
        finally:
            _exit(stack)
        assert resp.status_code == 400
