"""Tests for data_connectors blueprint routes.

Covers:
- GET /data-connectors/ — page renders for users with mc_data_connectors
- GET /data-connectors/api/teams/<esid>/connectors — JSON payload
- Permission gating: only mc_data_connectors holders pass
"""

from unittest import mock

import pytest


# Service module path (used for patching)
_SVC = "asset_management.web.blueprints.data_connectors.routes"
_PERMS = "asset_management.web.blueprints.data_connectors.routes." "RolePermissionsService"
_RFS = "asset_management.web.blueprints.data_connectors.routes.RoleFilterService"


def _grant():
    """Patch the merged permissions to grant mc_data_connectors."""
    return mock.patch(
        _PERMS + ".get_merged_permissions",
        return_value={"mc_data_connectors": True},
    )


def _scope(allowed_workspaces=None, has_scope=True, is_privileged=False):
    """patch the RoleFilterService gates the wizard-options route
    applies. Returns a context manager that grants the caller the given scope.

    Defaults model a NON-privileged scoped operator: ``has_scope=True``
    (passes the deny-all floor), ``allowed_workspaces=None`` (or a concrete
    list to exercise narrowing), and ``is_privileged=False``.

    the selection-surface routes now resolve their workspace filter
    through ``_effective_allowed_workspaces()``, which forwards
    ``get_allowed_workspaces()`` verbatim for a NON-privileged caller and
    substitutes the narrow ``data_grant_and_primary_workspaces`` set for a
    PRIVILEGED one. These narrowing tests model the non-privileged
    scoped operator (whose ``get_allowed_workspaces()`` list reaches the
    service unchanged), so ``current_user_is_privileged`` is stubbed False by
    default. Pass ``is_privileged=True`` to exercise the privileged path.

    The unit-test session has no ES backend, so these gates would otherwise
    resolve to empty/deny — patching them here is what the route's tenant gate
    needs.
    """
    return mock.patch.multiple(
        _RFS,
        current_user_has_any_scope=mock.MagicMock(return_value=has_scope),
        get_allowed_workspaces=mock.MagicMock(return_value=allowed_workspaces),
        current_user_is_privileged=mock.MagicMock(return_value=is_privileged),
    )


def _deny():
    """Patch the merged permissions to deny mc_data_connectors."""
    return mock.patch(
        _PERMS + ".get_merged_permissions",
        return_value={"mc_data_connectors": False},
    )


def _team_in_scope(team_esid="team1"):
    """the wizard configure endpoints now run ``_check_team_for_wizard``,
    which resolves the team via ``get_team_connectors(..., allowed_workspaces=...)``
    and 404s when it returns None. Patch it to a truthy payload so happy-path
    tests model an in-scope team and reach the underlying service.
    """
    return mock.patch(
        _SVC + ".get_team_connectors",
        return_value={"team": {"esid": team_esid}},
    )


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------


class TestPageRoute:
    def test_renders_for_user_with_permission(self, superuser_client):
        with _grant(), mock.patch(_SVC + ".list_teams", return_value=[]):
            resp = superuser_client.get("/data-connectors/")
        assert resp.status_code == 200
        assert b"Data Connectors" in resp.data

    def test_lists_teams_in_picker(self, superuser_client):
        teams = [{"esid": "t1", "title": "Alpha", "org_title": "Org A"}]
        with _grant(), mock.patch(_SVC + ".list_teams", return_value=teams):
            resp = superuser_client.get("/data-connectors/")
        assert resp.status_code == 200
        assert b"Alpha" in resp.data

    def test_remove_account_endpoint_url_resolves(self, superuser_client):
        # the page templates the per-account remove endpoint into
        # a data-attr (data-remove-account-url) the JS reads, with __ESID__ /
        # __KEY__ / __ACCOUNT__ placeholders it substitutes at click time. A wrong
        # url_for target (or a missing path param) would 500 the page render, so
        # assert the route name resolves with all three placeholders.
        from flask import url_for

        with superuser_client.application.test_request_context():
            built = url_for(
                "data_connectors.api_remove_connector_account",
                team_esid="__ESID__",
                connector_key="__KEY__",
                account_esid="__ACCOUNT__",
            )
        assert built.endswith("/connectors/__KEY__/accounts/__ACCOUNT__/remove")
        assert "__ESID__" in built

    def test_403_without_permission(self, auth_client):
        with _deny():
            resp = auth_client.get("/data-connectors/")
        assert resp.status_code == 403

    def test_403_for_org_user(self, org_user_client):
        with _deny():
            resp = org_user_client.get("/data-connectors/")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# API: team connectors
# ---------------------------------------------------------------------------


class TestApiTeamConnectors:
    def test_returns_payload_for_valid_team(self, superuser_client):
        payload = {
            "team": {"esid": "t1", "title": "Alpha", "org_title": "Org A"},
            "connectors": [
                # merged Meta Ads card (was two rows: facebook + instagram).
                {
                    "key": "meta_ads",
                    "label": "Meta Ads",
                    "auth": "oauth",
                    "configured": True,
                    "state": "configured",
                    "linked_assets": [],
                    "asset_type": "facebook_pages",
                    "oauth_manage_asset_type": "facebook_app_users",
                },
            ],
        }
        with _grant(), mock.patch(_SVC + ".get_team_connectors", return_value=payload):
            resp = superuser_client.get("/data-connectors/api/teams/t1/connectors")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["team"]["title"] == "Alpha"
        assert body["connectors"][0]["key"] == "meta_ads"

    def test_404_for_unknown_team(self, superuser_client):
        with _grant(), mock.patch(_SVC + ".get_team_connectors", return_value=None):
            resp = superuser_client.get("/data-connectors/api/teams/nope/connectors")
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["success"] is False
        assert "not found" in body["error"].lower()

    def test_403_returns_json_for_api_path(self, auth_client):
        with _deny():
            resp = auth_client.get("/data-connectors/api/teams/t1/connectors")
        assert resp.status_code == 403
        body = resp.get_json()
        assert body is not None
        assert body["success"] is False
        assert "denied" in body["error"].lower()


# ---------------------------------------------------------------------------
# API: disconnect a connector
# ---------------------------------------------------------------------------


class TestApiDisconnectConnector:
    def test_disconnect_returns_repainted_payload(self, superuser_client):
        fresh = {
            "team": {"esid": "t1", "title": "Alpha", "org_title": "Org A"},
            "connectors": [
                {
                    "key": "youtube",
                    "label": "YouTube",
                    "auth": "oauth",
                    "configured": False,
                    "state": "not_configured",
                    "linked_assets": [],
                },
            ],
        }
        with _grant(), _scope(), mock.patch(_SVC + ".disconnect_connector", return_value=fresh) as mock_dc:
            resp = superuser_client.post("/data-connectors/api/teams/t1/connectors/youtube/disconnect")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        # The recomputed card is returned so the page repaints without refetch.
        yt = next(c for c in body["connectors"] if c["key"] == "youtube")
        assert yt["state"] == "not_configured"
        # Route threads the caller's workspace scope into the service.
        assert "allowed_workspaces" in mock_dc.call_args.kwargs

    def test_disconnect_team_not_found_is_404(self, superuser_client):
        err = __import__(
            "asset_management.web.services.connector_status_service",
            fromlist=["ConnectorDisconnectError"],
        ).ConnectorDisconnectError("Team not found")
        with _grant(), _scope(), mock.patch(_SVC + ".disconnect_connector", side_effect=err):
            resp = superuser_client.post("/data-connectors/api/teams/nope/connectors/youtube/disconnect")
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False

    def test_disconnect_unknown_key_is_400(self, superuser_client):
        err = __import__(
            "asset_management.web.services.connector_status_service",
            fromlist=["ConnectorDisconnectError"],
        ).ConnectorDisconnectError("Unknown connector: bogus")
        with _grant(), _scope(), mock.patch(_SVC + ".disconnect_connector", side_effect=err):
            resp = superuser_client.post("/data-connectors/api/teams/t1/connectors/bogus/disconnect")
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_disconnect_403_without_permission(self, auth_client):
        with _deny():
            resp = auth_client.post("/data-connectors/api/teams/t1/connectors/youtube/disconnect")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# API: remove a single account from a connector
# ---------------------------------------------------------------------------


class TestApiRemoveConnectorAccount:
    _URL = "/data-connectors/api/teams/t1/connectors/google_ads/accounts/cust_b/remove"

    def test_remove_returns_repainted_payload(self, superuser_client):
        fresh = {
            "team": {"esid": "t1", "title": "Alpha", "org_title": "Org A"},
            "connectors": [{"key": "google_ads", "state": "configured", "linked_assets": [{"esid": "cust_a"}]}],
        }
        with _grant(), _scope(), mock.patch(_SVC + ".remove_connector_account", return_value=fresh) as mock_rm:
            resp = superuser_client.post(self._URL)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        # Route forwards the path args + the caller's workspace scope.
        assert mock_rm.call_args.args[:3] == ("t1", "google_ads", "cust_b")
        assert "allowed_workspaces" in mock_rm.call_args.kwargs

    def test_remove_team_not_found_is_404(self, superuser_client):
        err = __import__(
            "asset_management.web.services.connector_status_service",
            fromlist=["ConnectorDisconnectError"],
        ).ConnectorDisconnectError("Team not found")
        with _grant(), _scope(), mock.patch(_SVC + ".remove_connector_account", side_effect=err):
            resp = superuser_client.post(self._URL)
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False

    def test_remove_account_not_linked_is_400(self, superuser_client):
        err = __import__(
            "asset_management.web.services.connector_status_service",
            fromlist=["ConnectorDisconnectError"],
        ).ConnectorDisconnectError("Account is not linked to this team")
        with _grant(), _scope(), mock.patch(_SVC + ".remove_connector_account", side_effect=err):
            resp = superuser_client.post(self._URL)
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_remove_403_without_permission(self, auth_client):
        with _deny():
            resp = auth_client.post(self._URL)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Wizard: GET /api/wizard/options/<asset_type>
# ---------------------------------------------------------------------------


class TestWizardOptions:
    def test_returns_options_for_allowed_type(self, superuser_client):
        with (
            _grant(),
            _scope(),
            mock.patch(
                _SVC + ".list_assets_for_picker",
                return_value=[{"esid": "t1", "title": "Tenant One"}],
            ),
        ):
            resp = superuser_client.get("/data-connectors/api/wizard/options/echo_tenants")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["options"][0]["esid"] == "t1"

    def test_rejects_disallowed_asset_type(self, superuser_client):
        with _grant(), _scope():
            resp = superuser_client.get("/data-connectors/api/wizard/options/teams")
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_403_without_permission(self, auth_client):
        with _deny():
            resp = auth_client.get("/data-connectors/api/wizard/options/echo_tenants")
        assert resp.status_code == 403

    # ── workspace-scope narrowing on the picker ─────────────────

    def test_passes_allowed_workspaces_to_service(self, superuser_client):
        """The route threads get_allowed_workspaces() into the service."""
        with (
            _grant(),
            _scope(allowed_workspaces=["ws_a"]),
            mock.patch(
                _SVC + ".list_assets_for_picker",
                return_value=[{"esid": "t1", "title": "Tenant One"}],
            ) as mock_picker,
        ):
            resp = superuser_client.get("/data-connectors/api/wizard/options/echo_tenants")
        assert resp.status_code == 200
        # The resolved allowed-workspace list must reach the service —
        # without it the narrowing never happens (the leak).
        assert mock_picker.call_args.kwargs["allowed_workspaces"] == ["ws_a"]

    def test_deny_all_floor_returns_empty(self, superuser_client):
        """A caller with no resolvable scope sees an empty picker, not the
        cluster list — and the service is never even called."""
        with (
            _grant(),
            _scope(has_scope=False),
            mock.patch(
                _SVC + ".list_assets_for_picker",
                return_value=[{"esid": "t1", "title": "Tenant One"}],
            ) as mock_picker,
        ):
            resp = superuser_client.get("/data-connectors/api/wizard/options/echo_tenants")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["options"] == []
        mock_picker.assert_not_called()


# ---------------------------------------------------------------------------
# Wizard: POST /api/wizard/echo/configure
# ---------------------------------------------------------------------------


class TestWizardEcho:
    def test_pick_existing_tenant(self, superuser_client):
        with (
            _grant(),
            _team_in_scope(),
            mock.patch(
                _SVC + ".configure_echo",
                return_value={"tenant_esid": "t_existing", "created": False},
            ) as mock_configure,
        ):
            resp = superuser_client.post(
                "/data-connectors/api/wizard/echo/configure",
                json={"team_esid": "team1", "tenant_esid": "t_existing"},
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["tenant_esid"] == "t_existing"
        assert body["created"] is False
        # Service called with the right args
        kwargs = mock_configure.call_args.kwargs
        assert kwargs["team_esid"] == "team1"
        assert kwargs["tenant_esid"] == "t_existing"
        assert kwargs["new_tenant"] is None

    def test_create_new_tenant(self, superuser_client):
        new_tenant = {"echo_tenant_title": "X"}
        with (
            _grant(),
            _team_in_scope(),
            mock.patch(
                _SVC + ".configure_echo",
                return_value={"tenant_esid": "t_new", "created": True},
            ) as mock_configure,
        ):
            resp = superuser_client.post(
                "/data-connectors/api/wizard/echo/configure",
                json={"team_esid": "team1", "new_tenant": new_tenant},
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["created"] is True
        assert mock_configure.call_args.kwargs["new_tenant"] == new_tenant

    def test_wizard_error_returns_400(self, superuser_client):
        with (
            _grant(),
            _team_in_scope(),
            mock.patch(
                _SVC + ".configure_echo",
                side_effect=__import__(
                    "asset_management.web.services.connector_wizard_service", fromlist=["WizardError"]
                ).WizardError("bad input"),
            ),
        ):
            resp = superuser_client.post(
                "/data-connectors/api/wizard/echo/configure",
                json={"team_esid": "team1"},
            )
        assert resp.status_code == 400
        assert "bad input" in resp.get_json()["error"]

    def test_rejects_non_json_body(self, superuser_client):
        with _grant():
            resp = superuser_client.post(
                "/data-connectors/api/wizard/echo/configure",
                json="not a dict",
            )
        assert resp.status_code == 400

    def test_403_without_permission(self, auth_client):
        with _deny():
            resp = auth_client.post(
                "/data-connectors/api/wizard/echo/configure",
                json={"team_esid": "x"},
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Wizard: POST /api/wizard/chatwoot/configure
# ---------------------------------------------------------------------------


class TestWizardChatwoot:
    def test_pick_existing_account(self, superuser_client):
        with (
            _grant(),
            _team_in_scope(),
            mock.patch(
                _SVC + ".configure_chatwoot",
                return_value={"account_esid": "cw1", "created": False},
            ) as mock_configure,
        ):
            resp = superuser_client.post(
                "/data-connectors/api/wizard/chatwoot/configure",
                json={"team_esid": "team1", "account_esid": "cw1"},
            )
        assert resp.status_code == 200
        assert resp.get_json()["account_esid"] == "cw1"
        assert mock_configure.call_args.kwargs["account_esid"] == "cw1"


# ---------------------------------------------------------------------------
# Wizard: Facebook routes
# ---------------------------------------------------------------------------


class TestWizardFb:
    def test_account_endpoint(self, superuser_client):
        with (
            _grant(),
            mock.patch(
                _SVC + ".fb_pick_or_create_account",
                return_value="acct_new",
            ) as mock_fn,
        ):
            resp = superuser_client.post(
                "/data-connectors/api/wizard/fb/account",
                json={"new_account": {"fb_account_title": "T", "fb_account_id": "100"}},
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["fb_account_esid"] == "acct_new"
        assert mock_fn.call_args.kwargs["account_esid"] is None

    # Path A Phase 3 (2026-05-28) removed test_app_endpoint —
    # /api/wizard/fb/app + fb_pick_or_create_app are gone. The Path A wizard
    # uses the unified Meta app via WizardOAuthService.get_meta_app instead
    # of letting users pick / create an FB App through the wizard.

    def test_app_selection_mode_skip_for_workspace_with_no_apps(self, superuser_client):
        # a workspace with no existing fb_apps gets the unified
        # Meta app pre-selected and the App step skipped.
        with (
            _grant(),
            _scope(allowed_workspaces=["new-ws"]),
            mock.patch(
                _SVC + ".fb_app_selection_mode",
                return_value={"mode": "skip", "fb_app_esid": "unified"},
            ) as mock_mode,
        ):
            resp = superuser_client.get("/data-connectors/api/wizard/fb/app-selection-mode")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["mode"] == "skip"
        assert body["fb_app_esid"] == "unified"
        # The service was invoked with the caller's workspace scope.
        mock_mode.assert_called_once_with(["new-ws"])

    def test_app_selection_mode_pick_for_legacy_workspace(self, superuser_client):
        with (
            _grant(),
            _scope(allowed_workspaces=["kavanah-media"]),
            mock.patch(
                _SVC + ".fb_app_selection_mode",
                return_value={"mode": "pick"},
            ),
        ):
            resp = superuser_client.get("/data-connectors/api/wizard/fb/app-selection-mode")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["mode"] == "pick"
        assert "fb_app_esid" not in body

    def test_app_selection_mode_deny_all_falls_through_to_pick(self, superuser_client):
        # A caller with no resolvable scope must NOT auto-skip into the
        # unified app — that would bind their (empty) workspace to
        # someone else's Meta app. Fall through to "pick" instead.
        with (
            _grant(),
            _scope(has_scope=False),
            mock.patch(
                _SVC + ".fb_app_selection_mode",
            ) as mock_mode,
        ):
            resp = superuser_client.get("/data-connectors/api/wizard/fb/app-selection-mode")
        assert resp.status_code == 200
        assert resp.get_json()["mode"] == "pick"
        # Service was NOT called — the floor short-circuited.
        mock_mode.assert_not_called()

    def test_app_selection_mode_requires_data_connectors_permission(self, superuser_client):
        with _deny():
            resp = superuser_client.get("/data-connectors/api/wizard/fb/app-selection-mode")
        # _require_data_connectors aborts with 403 (or whatever the gate uses);
        # the only requirement here is "not 200 with a skip payload".
        assert resp.status_code != 200 or resp.get_json().get("mode") != "skip"

    # Path A Phase 3 (2026-05-28) removed test_app_user_endpoint_includes_fboauth_hostname
    # and test_app_user_status_polling — /api/wizard/fb/app-user and
    # /api/wizard/fb/app-user/<esid>/status are gone along with their service fns
    # (fb_pick_or_create_app_user, fb_app_user_status, _read_fb_app_user_status).
    # The Path A wizard polls /api/wizard/fb/oauth-status/<connection_esid>
    # instead — pinned -PATH-A-BACKEND-005.

    def test_list_pages_endpoint(self, superuser_client):
        pages = [{"id": "111", "name": "Alpha", "username": "alpha"}]
        with (
            _grant(),
            # the legacy app_user_esid branch is workspace-gated.
            mock.patch(_SVC + "._check_account_asset_for_wizard", return_value=None),
            mock.patch(
                _SVC + ".fb_list_pages_via_graph",
                return_value=pages,
            ),
        ):
            resp = superuser_client.post(
                "/data-connectors/api/wizard/fb/list-pages",
                json={"app_user_esid": "u1"},
            )
        assert resp.status_code == 200
        assert resp.get_json()["pages"] == pages

    def test_list_pages_graph_failure_returns_400(self, superuser_client):
        from asset_management.web.services.connector_wizard_service import WizardError

        with (
            _grant(),
            # the legacy app_user_esid branch is workspace-gated.
            mock.patch(_SVC + "._check_account_asset_for_wizard", return_value=None),
            mock.patch(
                _SVC + ".fb_list_pages_via_graph",
                side_effect=WizardError("token expired"),
            ),
        ):
            resp = superuser_client.post(
                "/data-connectors/api/wizard/fb/list-pages",
                json={"app_user_esid": "u1"},
            )
        assert resp.status_code == 400
        assert "token expired" in resp.get_json()["error"]

    def test_configure_endpoint(self, superuser_client):
        with (
            _grant(),
            _team_in_scope(),
            # the legacy route now gates fb_account_esid and each
            # pages[].esid like the other account-backed wizard routes.
            mock.patch(_SVC + "._check_account_asset_for_wizard", return_value=None) as asset_gate,
            mock.patch(
                _SVC + ".fb_link_pages_to_team",
                return_value={"page_esids": ["p1", "p2"], "created_count": 1},
            ) as mock_fn,
        ):
            resp = superuser_client.post(
                "/data-connectors/api/wizard/fb/configure",
                json={
                    "team_esid": "team1",
                    "fb_account_esid": "acct1",
                    "pages": [{"esid": "p1"}, {"fb_page_id": "222", "fb_page_name": "Bravo"}],
                },
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["page_esids"] == ["p1", "p2"]
        assert body["created_count"] == 1
        assert mock_fn.call_args.kwargs["team_esid"] == "team1"
        assert [c.args for c in asset_gate.call_args_list] == [
            ("de_asset_fb_accounts", "acct1"),
            ("de_asset_fb_pages", "p1"),
        ]

    def test_403_without_permission(self, auth_client):
        with _deny():
            resp = auth_client.post(
                "/data-connectors/api/wizard/fb/account",
                json={"account_esid": "x"},
            )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# every connector configure endpoint that writes a team link must
# run the tenant-scope gate (_check_team_for_wizard). An out-of-scope team_esid
# collapses to 404 (get_team_connectors returns None) BEFORE the underlying
# service is reached, so a holder of mc_data_connectors in workspace A cannot
# bind a connector onto a team in workspace B (cross-tenant write / IDOR).
# ---------------------------------------------------------------------------

# (url, json body, service attr on the routes module that must NOT be called)
_KAN977_ENDPOINTS = [
    ("/data-connectors/api/wizard/echo/configure", {"team_esid": "victim", "tenant_esid": "t1"}, "configure_echo"),
    ("/data-connectors/api/wizard/chatwoot/configure", {"team_esid": "victim", "account_esid": "c1"}, "configure_chatwoot"),
    (
        "/data-connectors/api/wizard/youtube/configure",
        {"team_esid": "victim", "account_esid": "a1", "channels": [{"id": "UC_x"}]},
        "configure_youtube",
    ),
    (
        "/data-connectors/api/wizard/youtube/onboarding-finalize",
        {"team_esid": "victim", "account_esid": "a1", "channels": [{"id": "UC_x"}]},
        "configure_youtube",
    ),
    (
        "/data-connectors/api/wizard/ga4/oauth-configure",
        {"team_esid": "victim", "account_esid": "a1", "properties": [{"id": "p1"}]},
        "configure_ga4_oauth",
    ),
    (
        "/data-connectors/api/wizard/ga4/oauth-onboarding-finalize",
        {"team_esid": "victim", "account_esid": "a1", "properties": [{"id": "p1"}]},
        "configure_ga4_oauth",
    ),
    (
        "/data-connectors/api/wizard/fb/configure",
        {"team_esid": "victim", "fb_account_esid": "acct1", "pages": [{"esid": "p1"}]},
        "fb_link_pages_to_team",
    ),
]


class TestKan977WizardTenantScope:
    @pytest.mark.parametrize("url,body,service_attr", _KAN977_ENDPOINTS)
    def test_out_of_scope_team_404s_without_calling_service(self, superuser_client, url, body, service_attr):
        # get_team_connectors returning None models the team being outside the
        # caller's allowed_workspaces (the 404-collapse in _check_team_for_wizard).
        with (
            _grant(),
            mock.patch(_SVC + ".get_team_connectors", return_value=None),
            mock.patch(_SVC + "." + service_attr) as service,
        ):
            resp = superuser_client.post(url, json=body)
        # Mutation: drop the _check_team_for_wizard call from the endpoint ->
        # the service is reached and this 404 / not-called pair fails.
        assert resp.status_code == 404, f"{url} must 404 for an out-of-scope team_esid; got {resp.status_code}"
        service.assert_not_called()
