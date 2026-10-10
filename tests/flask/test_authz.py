"""
dashboard-builder API authorization regression tests.

Covers the two bugs reported :

  * Dashboard config leaks to unauthorized users — ``GET /api/dashboards/<id>``
    previously returned the full dashboard JSON to any authenticated user,
    ignoring ``visibility_mode``, ``allowed_team_esids``, and
    ``allowed_org_esids``.
  * Write endpoints on the same blueprint (POST / PATCH / DELETE,
    publish / regenerate / unpublish) previously required no builder
    permission — any authenticated user could mutate any dashboard.

The authorization model after the fix:

  * Reads respect the same visibility logic as
    ``DashboardService.get_visible_echarts_dashboards`` — admins bypass;
    otherwise the user must have a role matching the dashboard's
    allowed_team_esids / allowed_org_esids when visibility_mode is
    ``show_teams``. ``hide`` is admin-only.
  * Writes additionally require ``mc_dashboard_builder`` (or an admin role).
"""

import time
from unittest import mock

import pytest

from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient


_R = "asset_management.web.blueprints.dashboard_builder.routes"
BUILDER = "/dashboard-builder"


# ---------------------------------------------------------------------------
# Scope resolver — production maps ``team_<title>`` / ``org_<title>`` roles to
# ES document IDs via live lookups. For unit tests we stub the
# resolver so role strings themselves stand in as ESIDs; this keeps fixtures
# small and avoids patching ``get_all_documents`` in every test.
# ---------------------------------------------------------------------------

def _stub_resolve_user_scope_esids(roles):
    team = {r for r in (roles or []) if r.startswith("team_")}
    org = {r for r in (roles or []) if r.startswith("org_")}
    return team, org


@pytest.fixture(autouse=True)
def _patch_scope_resolver():
    # ``sys.path`` exposes the dashboard service under two dotted names
    # (``asset_management.web.services.dashboard_service`` and
    # ``services.dashboard_service``) — Python treats them as distinct
    # modules. routes.py reaches the service via the short path, so tests
    # must patch both to be safe. (Same gotcha documented.)
    with mock.patch(
        "asset_management.web.services.dashboard_service.DashboardService._resolve_user_scope_esids",
        side_effect=_stub_resolve_user_scope_esids,
    ), mock.patch(
        "services.dashboard_service.DashboardService._resolve_user_scope_esids",
        side_effect=_stub_resolve_user_scope_esids,
    ):
        yield


# ---------------------------------------------------------------------------
# Test clients — specialised session fixtures for this ticket
# ---------------------------------------------------------------------------

def _seed(sess, *, roles, is_admin=False):
    sess["authenticated"] = True
    sess["username"] = "testuser"
    sess["roles"] = list(roles)
    sess["is_admin"] = is_admin
    sess["full_name"] = "Test User"
    sess["email"] = "test@example.com"
    sess["auth_timestamp"] = time.time()
    sess["csrf_token"] = CSRF_TEST_TOKEN


@pytest.fixture
def viewer_team_a(app):
    """Authenticated user on team A only — not a builder."""
    c = app.test_client()
    with c.session_transaction() as sess:
        _seed(sess, roles=["team_A_esid"])
    return CsrfTestClient(c)


@pytest.fixture
def viewer_team_b(app):
    """Authenticated user on team B only — not a builder."""
    c = app.test_client()
    with c.session_transaction() as sess:
        _seed(sess, roles=["team_B_esid"])
    return CsrfTestClient(c)


@pytest.fixture
def builder_team_a(app):
    """User on team A with ``mc_dashboard_builder`` but no admin role."""
    c = app.test_client()
    with c.session_transaction() as sess:
        _seed(sess, roles=["team_A_esid", "dashboard_editor"])
    return CsrfTestClient(c)


# ---------------------------------------------------------------------------
# Fixtures — dashboard source factory
# ---------------------------------------------------------------------------

def _dashboard(**overrides):
    src = {
        "dashboard_id": "dash-1",
        "dashboard_type": "echarts",
        "title": "Test Dashboard",
        "menu": "Reports",
        "visibility_mode": "show_all",
        "allowed_org_esids": [],
        "allowed_team_esids": [],
        "echarts_config": {"charts": []},
    }
    src.update(overrides)
    return src


def _merged_perms(builder=False):
    """Shape of ``RolePermissionsService.get_merged_permissions`` return."""
    return {"mc_dashboard_builder": bool(builder)}


# ===========================================================================
# GET /api/dashboards/<id> — the original leak
# ===========================================================================


class TestLoadDashboardReadAuth:
    """The core config leaked to non-allowed users."""

    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_show_all_dashboard_when_user_is_not_builder(
        self, mock_get, viewer_team_a
    ):
        """Non-builder viewing a show_all dashboard — the normal iframe path."""
        mock_get.return_value = _dashboard(visibility_mode="show_all")
        resp = viewer_team_a.get(f"{BUILDER}/api/dashboards/dash-1")
        assert resp.status_code == 200

    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_when_user_is_not_on_allowed_team(
        self, mock_get, viewer_team_b
    ):
        mock_get.return_value = _dashboard(
            visibility_mode="show_teams",
            allowed_team_esids=["team_A_esid"],
        )
        resp = viewer_team_b.get(f"{BUILDER}/api/dashboards/dash-1")
        assert resp.status_code == 403

    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_200_when_user_is_on_allowed_team(
        self, mock_get, viewer_team_a
    ):
        mock_get.return_value = _dashboard(
            visibility_mode="show_teams",
            allowed_team_esids=["team_A_esid"],
        )
        resp = viewer_team_a.get(f"{BUILDER}/api/dashboards/dash-1")
        assert resp.status_code == 200

    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_200_when_user_is_on_allowed_org(
        self, mock_get, app
    ):
        """allowed_org_esids match should also grant access (org
        roles now resolve against the org list, not the team list)."""
        client = app.test_client()
        with client.session_transaction() as sess:
            _seed(sess, roles=["org_A_esid"])
        org_user = CsrfTestClient(client)

        mock_get.return_value = _dashboard(
            visibility_mode="show_teams",
            allowed_org_esids=["org_A_esid"],
        )
        resp = org_user.get(f"{BUILDER}/api/dashboards/dash-1")
        assert resp.status_code == 200

    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_for_hide_dashboard_when_user_is_not_admin(
        self, mock_get, viewer_team_a
    ):
        mock_get.return_value = _dashboard(visibility_mode="hide")
        resp = viewer_team_a.get(f"{BUILDER}/api/dashboards/dash-1")
        assert resp.status_code == 403

    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_200_for_hide_dashboard_when_user_is_builder(
        self, mock_get, auth_client
    ):
        """(2026-05-18): ``hide`` is a viewer-side rule, not
        a Builder rule. Editors (anyone with ``mc_dashboard_builder``,
        including ``asset_admin``) always see hidden dashboards inside
        the Builder so they can un-hide them — without this, a
        dashboard marked hide is permanently unreachable.

        The user-facing /dashboards page still hides them
        (DashboardService.get_visible_echarts_dashboards keeps the
        strict gate) — tested separately in
        tests/services/test_dashboard_service.py.

        This replaces the (2026-05-15) assertion that admins
        could not see hide dashboards; the two-surfaces split (Builder
        always shows, viewer always hides) is the new contract.
        """
        mock_get.return_value = _dashboard(visibility_mode="hide")
        resp = auth_client.get(f"{BUILDER}/api/dashboards/dash-1")
        assert resp.status_code == 200

    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_when_show_teams_has_no_allowed_lists(
        self, mock_get, viewer_team_a
    ):
        """show_teams with both lists empty fails closed for non-admins."""
        mock_get.return_value = _dashboard(
            visibility_mode="show_teams",
            allowed_team_esids=[],
            allowed_org_esids=[],
        )
        resp = viewer_team_a.get(f"{BUILDER}/api/dashboards/dash-1")
        assert resp.status_code == 403

    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_404_when_dashboard_does_not_exist(
        self, mock_get, viewer_team_a
    ):
        mock_get.return_value = None
        resp = viewer_team_a.get(f"{BUILDER}/api/dashboards/nope")
        assert resp.status_code == 404


# ===========================================================================
# GET /dashboard-builder/view/<id> — page-level gate
# ===========================================================================


class TestViewDashboardPageAuth:
    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_not_render_hidden_dashboard_when_user_is_not_admin(
        self, mock_get, viewer_team_a
    ):
        """The 403 page handler redirects HTML requests to an allowed page
        (302). The raw viewer should never render the template either way —
        both 302 and 403 are acceptable; a 200 is the bug."""
        mock_get.return_value = _dashboard(visibility_mode="hide")
        resp = viewer_team_a.get(f"{BUILDER}/view/dash-1")
        assert resp.status_code in (302, 403)

    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_render_viewer_for_allowed_dashboard(
        self, mock_get, viewer_team_a
    ):
        mock_get.return_value = _dashboard(visibility_mode="show_all")
        resp = viewer_team_a.get(f"{BUILDER}/view/dash-1")
        assert resp.status_code == 200


# ===========================================================================
# GET /api/dashboards — list endpoint must also filter
# ===========================================================================


class TestListDashboardsFiltersByVisibility:
    """Defense-in-depth filtering on the list endpoint.

    The auth layer already blocks non-admins from reaching
    ``GET /dashboard-builder/api/dashboards`` — so the filter is the second
    line of defense. These tests call the function directly via the Flask
    app context so the session-based visibility check runs against the
    data returned by ``get_all_documents``.
    """

    @mock.patch(f"{_R}.get_all_documents")
    def test_should_list_all_dashboards_for_privileged_admin(
        self, mock_all, auth_client
    ):
        """#2a (2026-06-04) — workspace-scoped admin visibility,
        SUPERSEDING the ``show_teams`` admin-block.

        Background: removed the admin bypass so a bare asset_admin
        couldn't see another team's show_teams dashboard. But the teams-only
        redesign then left a privileged admin with no team scope
        unable to see dashboards other users created — breaking admin
        oversight. Per a teammate (2026-06-04): a privileged admin (superuser /
        asset_admin) sees every dashboard in the workspaces they can resolve,
        and a bare admin resolves to ALL workspaces (get_allowed_workspaces
        is None), so they see everything — including a show_teams dashboard
        scoped to another team and a legacy dashboard with no workspace stamp.

        So a bare asset_admin loading the Builder list now sees:
          (a) show_all — visible
          (b) hide — visible
          (c) show_teams allowed_team_esids=[other_team] — NOW visible
              (#2a: privileged admins see all in-workspace dashboards)
        """
        mock_all.return_value = [
            {"_id": "a", "_source": _dashboard(dashboard_id="a", visibility_mode="show_all")},
            {"_id": "b", "_source": _dashboard(dashboard_id="b", visibility_mode="hide")},
            {"_id": "c", "_source": _dashboard(
                dashboard_id="c",
                visibility_mode="show_teams",
                allowed_team_esids=["other_team"],
            )},
        ]
        # Even with no team scope, the privileged admin now sees every row.
        with mock.patch(
            f"{_R}._current_user_scope_esids", return_value=(set(), set())
        ):
            resp = auth_client.get(f"{BUILDER}/api/dashboards")
        assert resp.status_code == 200
        ids = {d["id"] for d in resp.get_json()["dashboards"]}
        assert ids == {"a", "b", "c"}

    @mock.patch(f"{_R}.get_all_documents")
    def test_should_exclude_hidden_and_wrong_team_via_direct_call(
        self, mock_all, app
    ):
        """Unit-test the filter path for a non-admin session.

        Needed because the auth layer rejects non-admins before the
        endpoint runs — but if a future change ever admits them, the
        server must still only return dashboards they can see.
        """
        from asset_management.web.blueprints.dashboard_builder.routes import list_dashboards

        mock_all.return_value = [
            {"_id": "public", "_source": _dashboard(
                dashboard_id="public", visibility_mode="show_all"
            )},
            {"_id": "team-a", "_source": _dashboard(
                dashboard_id="team-a",
                visibility_mode="show_teams",
                allowed_team_esids=["team_A_esid"],
            )},
            {"_id": "team-b", "_source": _dashboard(
                dashboard_id="team-b",
                visibility_mode="show_teams",
                allowed_team_esids=["team_B_esid"],
            )},
            {"_id": "hidden", "_source": _dashboard(
                dashboard_id="hidden", visibility_mode="hide"
            )},
        ]

        with app.test_request_context("/"):
            from flask import session as flask_session
            flask_session["authenticated"] = True
            flask_session["roles"] = ["team_A_esid"]
            flask_session["is_admin"] = False
            resp = list_dashboards()

        body = resp.get_json()
        returned_ids = {d["id"] for d in body["dashboards"]}
        assert returned_ids == {"public", "team-a"}


# ===========================================================================
# Write endpoints — must require mc_dashboard_builder
# ===========================================================================


class TestWriteEndpointsRequireBuilderPermission:
    """severity-bump check: non-builder users must not mutate dashboards."""

    def _no_builder_perms(self, _roles):
        return _merged_perms(builder=False)

    @mock.patch(f"{_R}.RolePermissionsService.get_merged_permissions")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_on_save_when_user_is_not_builder(
        self, mock_get, mock_perms, viewer_team_a
    ):
        mock_perms.side_effect = self._no_builder_perms
        mock_get.return_value = None
        resp = viewer_team_a.post(
            f"{BUILDER}/api/dashboards",
            json={"title": "X", "charts": []},
        )
        assert resp.status_code == 403

    @mock.patch(f"{_R}.RolePermissionsService.get_merged_permissions")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_on_delete_when_user_is_not_builder(
        self, mock_get, mock_perms, viewer_team_a
    ):
        mock_perms.side_effect = self._no_builder_perms
        mock_get.return_value = _dashboard(visibility_mode="show_all")
        resp = viewer_team_a.delete(f"{BUILDER}/api/dashboards/dash-1")
        assert resp.status_code == 403

    @mock.patch(f"{_R}.RolePermissionsService.get_merged_permissions")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_on_patch_properties_when_user_is_not_builder(
        self, mock_get, mock_perms, viewer_team_a
    ):
        mock_perms.side_effect = self._no_builder_perms
        mock_get.return_value = _dashboard(visibility_mode="show_all")
        resp = viewer_team_a.patch(
            f"{BUILDER}/api/dashboards/dash-1/properties",
            json={"title": "Renamed"},
        )
        assert resp.status_code == 403

    @mock.patch(f"{_R}.RolePermissionsService.get_merged_permissions")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_on_publish_when_user_is_not_builder(
        self, mock_get, mock_perms, viewer_team_a
    ):
        mock_perms.side_effect = self._no_builder_perms
        mock_get.return_value = _dashboard(visibility_mode="show_all")
        resp = viewer_team_a.post(f"{BUILDER}/api/dashboards/dash-1/publish")
        assert resp.status_code == 403

    @mock.patch(f"{_R}.RolePermissionsService.get_merged_permissions")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_on_regenerate_when_user_is_not_builder(
        self, mock_get, mock_perms, viewer_team_a
    ):
        mock_perms.side_effect = self._no_builder_perms
        mock_get.return_value = _dashboard(visibility_mode="show_all")
        resp = viewer_team_a.post(
            f"{BUILDER}/api/dashboards/dash-1/publish/regenerate"
        )
        assert resp.status_code == 403

    @mock.patch(f"{_R}.RolePermissionsService.get_merged_permissions")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_on_unpublish_when_user_is_not_builder(
        self, mock_get, mock_perms, viewer_team_a
    ):
        mock_perms.side_effect = self._no_builder_perms
        mock_get.return_value = _dashboard(visibility_mode="show_all")
        resp = viewer_team_a.delete(f"{BUILDER}/api/dashboards/dash-1/publish")
        assert resp.status_code == 403


class TestBuilderWithVisibilityScope:
    """Builders without admin still respect per-dashboard visibility on writes."""

    @mock.patch(f"{_R}.RolePermissionsService.get_merged_permissions")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_when_builder_is_not_on_allowed_team(
        self, mock_get, mock_perms, builder_team_a
    ):
        """A builder on team A trying to overwrite a team-B-scoped dashboard."""
        mock_perms.return_value = _merged_perms(builder=True)
        mock_get.return_value = _dashboard(
            visibility_mode="show_teams",
            allowed_team_esids=["team_B_esid"],
        )
        resp = builder_team_a.post(
            f"{BUILDER}/api/dashboards",
            json={"id": "dash-1", "title": "Hijacked", "charts": []},
        )
        assert resp.status_code == 403

    @mock.patch(f"{_R}.RolePermissionsService.get_merged_permissions")
    @mock.patch(f"{_R}.delete_document_by_id")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_allow_builder_to_delete_hidden_dashboard(
        self, mock_get, mock_delete, mock_perms, builder_team_a
    ):
        """(2026-05-18): editors always see (and therefore can
        delete) hidden dashboards inside the Builder. earlier the
        Builder treated ``hide`` as a hard read-gate, which left
        hidden dashboards permanently unreachable for cleanup.
        """
        mock_perms.return_value = _merged_perms(builder=True)
        mock_get.return_value = _dashboard(visibility_mode="hide")
        mock_delete.return_value = True
        resp = builder_team_a.delete(f"{BUILDER}/api/dashboards/dash-1")
        assert resp.status_code == 200


# ===========================================================================
# Export endpoint — visibility gate, no builder gate
# ===========================================================================


class TestExportDashboardRespectsVisibility:
    @mock.patch(f"{_R}.get_document_by_id")
    def test_should_return_403_on_export_when_user_is_not_on_allowed_team(
        self, mock_get, viewer_team_b
    ):
        mock_get.return_value = _dashboard(
            visibility_mode="show_teams",
            allowed_team_esids=["team_A_esid"],
        )
        resp = viewer_team_b.post(
            f"{BUILDER}/api/dashboards/dash-1/export",
            json={"format": "csv"},
        )
        assert resp.status_code == 403
