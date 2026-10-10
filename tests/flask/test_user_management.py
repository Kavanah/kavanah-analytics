"""
Tests for user management Flask routes.

Covers:
- GET /user-management — admin page rendering and authorization
- POST /api/user-management/create-user — user creation with validation
- POST /api/user-management/delete-user — user deletion with guards
- GET /api/user-management/audit-log — audit log query
- POST /api/user-management/sync-roles — role synchronization
- POST /api/user-management/create-role — individual role creation
- POST /api/user-management/enable-mfa — MFA enable
- POST /api/user-management/disable-mfa — MFA disable
- GET /api/user-management/role-sync-status — role sync status
- POST /api/user-management/suspend-user — suspend a user
- POST /api/user-management/reinstate-user — reinstate a suspended user
- POST /api/user-management/update-roles — edit a user's roles
- GET /api/user-management/user-detail/<username> — user detail with login history
- POST /api/user-management/bulk-action — bulk suspend/reinstate/delete
- GET /change-password — forced password change page
- POST /change-password — forced password change processing
"""

import time
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_post(client, url, data=None, **kwargs):
    """POST JSON to a route and return the response."""
    return client.post(url, json=data or {}, **kwargs)


def _seed_fpc_session(client, username="newuser"):
    """Seed a forced-password-change session for change-password tests."""
    with client.session_transaction() as sess:
        sess["fpc_pending"] = True
        sess["fpc_username"] = username
        sess["fpc_roles"] = ["asset_admin"]
        sess["fpc_full_name"] = "New User"
        sess["fpc_email"] = "new@example.com"
        sess["fpc_timestamp"] = time.time()
        sess["csrf_token"] = "test-csrf-token"


# ---------------------------------------------------------------------------
# GET /user-management
# ---------------------------------------------------------------------------

class TestUserManagementPage:

    def test_returns_200_for_admin(self, auth_client):
        with mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=[]), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=[]), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=[]):
            resp = auth_client.get("/user-management")
        assert resp.status_code == 200

    def test_returns_200_for_superuser(self, superuser_client):
        with mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=[]), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=[]), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=[]):
            resp = superuser_client.get("/user-management")
        assert resp.status_code == 200

    def test_non_admin_redirected(self, org_user_client):
        resp = org_user_client.get("/user-management")
        assert resp.status_code == 302

    def test_unauthenticated_redirects_to_login(self, client):
        resp = client.get("/user-management")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_renders_admin_users_in_template(self, superuser_client):
        """Asset Admins section is only visible to superusers."""
        fake_users = [
            {"username": "alice_testuser", "full_name": "Alice Test", "roles": ["asset_admin"],
             "mfa_enabled": False, "mfa_method": None, "setup_complete": False, "last_used": None},
        ]
        with mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=fake_users), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=[]), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=["asset_admin"]):
            resp = superuser_client.get("/user-management")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "alice_testuser" in html

    def test_admin_cannot_see_admin_section(self, auth_client):
        """asset_admin users should not see the Asset Admins section."""
        fake_users = [
            {"username": "alice_testuser", "full_name": "Alice Test", "roles": ["asset_admin"],
             "mfa_enabled": False, "mfa_method": None, "setup_complete": False, "last_used": None},
        ]
        with mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=fake_users), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=[]), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=["asset_admin"]):
            resp = auth_client.get("/user-management")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "alice_testuser" not in html

    def test_renders_org_users_with_matching_org(self, auth_client):
        """Org users only render when orgs list contains the matching organization.

         v3 follow-up: org/team membership is sourced from the new authz
        indices via ``_load_authz_memberships_index``, not from
        ``user.roles``. The mock returns the synthesized ``org_<title>``
        string for the user so the classification loop routes them into
        the org section.
        """
        fake_users = [
            {"username": "bob_testuser", "full_name": "Bob Test", "roles": [],
             "mfa_enabled": False, "mfa_method": None, "setup_complete": False, "last_used": None},
        ]
        fake_org_docs = [
            {"_source": {"organization_title": "TestOrg", "organization_logo": "", "organization_description": ""}},
        ]
        with mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=fake_users), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=fake_org_docs), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=[]), \
             mock.patch("asset_management.web.app._load_authz_memberships_index",
                        return_value={"bob_testuser": ["org_TestOrg"]}):
            resp = auth_client.get("/user-management")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "bob_testuser" in html

    def test_custom_role_with_mc_user_management_can_access(self, app):
        """a custom role with mc_user_management can access the page,
        not just hardcoded asset_admin/superuser."""
        from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "customuser"
            sess["roles"] = ["custom_user_admin"]
            sess["is_admin"] = False
            sess["full_name"] = "Custom"
            sess["email"] = "c@example.com"
            sess["auth_timestamp"] = time.time()
            sess["csrf_token"] = CSRF_TEST_TOKEN
        client = CsrfTestClient(c)
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.get_merged_permissions",
            return_value={"mc_user_management": True},
        ), mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=[]), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=[]), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=[]):
            resp = client.get("/user-management")
        assert resp.status_code == 200

    def test_custom_role_without_mc_user_management_redirected(self, app):
        from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "customuser"
            sess["roles"] = ["custom_no_perms"]
            sess["is_admin"] = False
            sess["full_name"] = "Custom"
            sess["email"] = "c@example.com"
            sess["auth_timestamp"] = time.time()
            sess["csrf_token"] = CSRF_TEST_TOKEN
        client = CsrfTestClient(c)
        with mock.patch(
            "asset_management.web.app.RolePermissionsService.get_merged_permissions",
            return_value={"mc_user_management": False},
        ):
            resp = client.get("/user-management")
        # Page-level 403 → redirect by global handler.
        assert resp.status_code == 302

    def test_user_with_orphaned_org_role_still_renders(self, superuser_client):
        """A user whose only org_* role has no matching org doc falls through to
        the legacy section instead of disappearing from the console entirely.
        Uses superuser_client because the Legacy / Other section is only visible
        to superusers (existing design)."""
        fake_users = [
            {"username": "carol_testuser", "full_name": "Carol Test", "roles": ["org_GhostOrg"],
             "mfa_enabled": False, "mfa_method": None, "setup_complete": False, "last_used": None},
        ]
        # No org doc for GhostOrg — simulates a role that points at a deleted or
        # never-existed organization (the a teammate-Test-User case: typo'd org_NFCY
        # when NFCY is a team, not an org).
        with mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=fake_users), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=[]), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=[]):
            resp = superuser_client.get("/user-management")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "carol_testuser" in html

    def test_roles_tab_system_filter_visible_to_superuser(self, superuser_client):
        """The Roles-tab 'System' type-filter dropdown option must be visible
        to a superuser — they're the only role that can see system roles.

        Phase 1 cleanup of the Roles tab (follow-up to): the API
        endpoint /api/user-management/roles already filters system roles
        from non-superusers (app.py:6489-6491). Gating the dropdown option
        too keeps the UX consistent — non-superusers don't see a filter
        that would return zero rows for them.
        """
        with mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=[]), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=[]), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=[]):
            resp = superuser_client.get("/user-management")
        assert resp.status_code == 200
        html = resp.data.decode()
        # The dropdown option's exact shape — text-match against the
        # rendered <option> tag (not a lenient substring) so a stray
        # mention of "System" elsewhere in the HTML can't make this pass.
        assert '<option value="system">System</option>' in html

    def test_roles_tab_system_filter_hidden_from_non_superuser(self, auth_client):
        """Non-superusers (here: asset_admin) must NOT see the Roles-tab
        'System' type-filter dropdown option.

        Mutation: drop the ``{% if is_superuser %}`` gate in the template
        → this test fails immediately (the option appears in the rendered
        HTML for a non-superuser).
        """
        with mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=[]), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=[]), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=[]):
            resp = auth_client.get("/user-management")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert '<option value="system">System</option>' not in html, (
            "non-superuser saw the System filter option — the {% if is_superuser %} "
            "gate in user_management.html:roles-type-filter is not firing"
        )


# ---------------------------------------------------------------------------
# POST /api/user-management/create-user
# ---------------------------------------------------------------------------

class TestCreateUser:

    def test_creates_user_successfully(self, auth_client):
        # ``auth_client`` is an asset_admin (NOT a superuser), and
        # asset_admin/data_explorer/superuser are now superuser-only grants.
        # Use an ordinary grantable cluster role (report_publisher) so this
        # happy-path assertion exercises the pass-through, not the new gate.
        create_result = {"success": True, "temp_password": "TempPass123!"}
        email_result = {"success": True}
        with mock.patch("asset_management.web.app.UserProvisioningService.create_user", return_value=create_result) as m_create, \
             mock.patch("asset_management.web.app.UserProvisioningService.send_welcome_email", return_value=email_result), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"), \
             mock.patch("asset_management.web.app._provision_new_authz_assignments"):
            resp = _json_post(auth_client, "/api/user-management/create-user", {
                "username": "newuser",
                "full_name": "New User",
                "email": "new@example.com",
                "system_roles": ["report_publisher"],
                "memberships": ["org_test"],
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["email_sent"] is True
        # ES Security gets system_roles only — memberships go to the
        # authz indices via _provision_new_authz_assignments.
        m_create.assert_called_once_with("newuser", "New User", "new@example.com", ["report_publisher"])

    def test_non_superuser_cannot_grant_superuser_only_role(self, auth_client):
        # asset_admin (non-superuser) granting a superuser-only
        # role (asset_admin/data_explorer) is refused 403 and the user is
        # never created.
        for role in ("asset_admin", "data_explorer"):
            with mock.patch("asset_management.web.app.UserProvisioningService.create_user") as m_create:
                resp = _json_post(auth_client, "/api/user-management/create-user", {
                    "username": "newuser",
                    "full_name": "New User",
                    "email": "new@example.com",
                    "system_roles": [role],
                    "memberships": ["org_test"],
                })
            assert resp.status_code == 403, f"{role} grant by non-superuser must be 403"
            assert role in resp.get_json()["error"]
            m_create.assert_not_called()

    def test_superuser_may_grant_superuser_only_role(self, superuser_client):
        # a superuser is unaffected by the gate.
        create_result = {"success": True, "temp_password": "TempPass123!"}
        with mock.patch("asset_management.web.app.UserProvisioningService.create_user", return_value=create_result) as m_create, \
             mock.patch("asset_management.web.app.UserProvisioningService.send_welcome_email", return_value={"success": True}), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"), \
             mock.patch("asset_management.web.app._provision_new_authz_assignments"):
            resp = _json_post(superuser_client, "/api/user-management/create-user", {
                "username": "newuser",
                "full_name": "New User",
                "email": "new@example.com",
                "system_roles": ["data_explorer"],
                "memberships": ["org_test"],
            })
        assert resp.status_code == 200
        m_create.assert_called_once_with("newuser", "New User", "new@example.com", ["data_explorer"])

    def test_missing_username_returns_400(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/create-user", {
            "username": "",
            "email": "a@b.com",
            "memberships": ["org_test"],
        })
        assert resp.status_code == 400
        assert "Username" in resp.get_json()["error"]

    def test_missing_email_returns_400(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/create-user", {
            "username": "newuser",
            "email": "",
            "memberships": ["org_test"],
        })
        assert resp.status_code == 400
        assert "Email" in resp.get_json()["error"]

    def test_empty_roles_returns_400(self, auth_client):
        # v3 follow-up: empty memberships is the rejection now —
        # at least one org/team scope is required.
        resp = _json_post(auth_client, "/api/user-management/create-user", {
            "username": "newuser",
            "email": "a@b.com",
            "system_roles": [],
            "memberships": [],
        })
        assert resp.status_code == 400
        assert "membership" in resp.get_json()["error"].lower()

    def test_non_list_roles_returns_400(self, auth_client):
        # system_roles must be a list, not a bare string.
        resp = _json_post(auth_client, "/api/user-management/create-user", {
            "username": "newuser",
            "email": "a@b.com",
            "system_roles": "asset_admin",
            "memberships": ["org_test"],
        })
        assert resp.status_code == 400

    def test_service_failure_returns_500(self, auth_client):
        create_result = {"success": False, "error": "User already exists"}
        with mock.patch("asset_management.web.app.UserProvisioningService.create_user", return_value=create_result), \
             mock.patch("asset_management.web.app._provision_new_authz_assignments"):
            resp = _json_post(auth_client, "/api/user-management/create-user", {
                "username": "existing",
                "full_name": "X",
                "email": "x@y.com",
                "system_roles": [],
                "memberships": ["org_test"],
            })
        assert resp.status_code == 500
        assert resp.get_json()["error"] == "User already exists"

    def test_email_failure_still_succeeds(self, auth_client):
        create_result = {"success": True, "temp_password": "TempPass123!"}
        email_result = {"success": False, "error": "SMTP down"}
        with mock.patch("asset_management.web.app.UserProvisioningService.create_user", return_value=create_result), \
             mock.patch("asset_management.web.app.UserProvisioningService.send_welcome_email", return_value=email_result), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"), \
             mock.patch("asset_management.web.app._provision_new_authz_assignments"):
            resp = _json_post(auth_client, "/api/user-management/create-user", {
                "username": "newuser",
                "full_name": "New User",
                "email": "new@example.com",
                "system_roles": [],
                "memberships": ["org_test"],
            })
        data = resp.get_json()
        assert data["success"] is True
        assert data["email_sent"] is False
        assert data["email_error"] == "SMTP down"

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = _json_post(org_user_client, "/api/user-management/create-user", {
            "username": "x", "email": "x@y.com",
            "system_roles": [], "memberships": ["org_test"],
        })
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, client):
        # unauthenticated AJAX/API calls (an /api/ path, or any
        # state-change carrying the X-CSRF-Token the fetch wrapper sets — which
        # CsrfTestClient injects on POST) now get 401 + JSON instead of a 302 to
        # /login, so a fetch never parses the login HTML as JSON.
        resp = _json_post(client, "/api/user-management/create-user", {
            "username": "x", "email": "x@y.com",
            "system_roles": [], "memberships": ["org_test"],
        })
        assert resp.status_code == 401

    def test_audit_log_is_written(self, auth_client):
        create_result = {"success": True, "temp_password": "TempPass123!"}
        email_result = {"success": True}
        with mock.patch("asset_management.web.app.UserProvisioningService.create_user", return_value=create_result), \
             mock.patch("asset_management.web.app.UserProvisioningService.send_welcome_email", return_value=email_result), \
             mock.patch("asset_management.web.app.AuditLogService.log_event") as m_audit, \
             mock.patch("asset_management.web.app._provision_new_authz_assignments"):
            _json_post(auth_client, "/api/user-management/create-user", {
                "username": "newuser",
                "full_name": "New User",
                "email": "new@example.com",
                "system_roles": [],
                "memberships": ["org_test"],
            })
        m_audit.assert_called_once()
        call_kwargs = m_audit.call_args
        assert call_kwargs[0][1] == "create_user"  # action arg
        assert call_kwargs[1]["target_user"] == "newuser"


# ---------------------------------------------------------------------------
# POST /api/user-management/delete-user
# ---------------------------------------------------------------------------

class TestDeleteUser:

    def test_deletes_user_successfully(self, auth_client):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(auth_client, "/api/user-management/delete-user", {
                "username": "victim",
            })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_es.security.delete_user.assert_called_once_with(username="victim")

    def test_missing_username_returns_400(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/delete-user", {
            "username": "",
        })
        assert resp.status_code == 400

    def test_cannot_delete_self(self, auth_client):
        # auth_client session has username="testuser"
        resp = _json_post(auth_client, "/api/user-management/delete-user", {
            "username": "testuser",
        })
        assert resp.status_code == 400
        assert "your own" in resp.get_json()["error"].lower()

    def test_cannot_delete_elastic_superuser(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/delete-user", {
            "username": "elastic",
        })
        assert resp.status_code == 400
        assert "elastic" in resp.get_json()["error"].lower()

    def test_es_unavailable_returns_500(self, auth_client):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = _json_post(auth_client, "/api/user-management/delete-user", {
                "username": "victim",
            })
        assert resp.status_code == 500
        assert "unavailable" in resp.get_json()["error"].lower()

    def test_es_exception_returns_500(self, auth_client):
        mock_es = mock.MagicMock()
        mock_es.security.delete_user.side_effect = Exception("Connection refused")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = _json_post(auth_client, "/api/user-management/delete-user", {
                "username": "victim",
            })
        assert resp.status_code == 500
        # generic client-facing message; exception text is in the
        # server log only.
        assert resp.get_json()["error"] == "Failed to delete user"

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = _json_post(org_user_client, "/api/user-management/delete-user", {
            "username": "victim",
        })
        assert resp.status_code == 403

    def test_audit_log_is_written(self, auth_client):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.AuditLogService.log_event") as m_audit:
            _json_post(auth_client, "/api/user-management/delete-user", {
                "username": "victim",
            })
        m_audit.assert_called_once()
        assert m_audit.call_args[0][1] == "delete_user"
        assert m_audit.call_args[1]["target_user"] == "victim"


# ---------------------------------------------------------------------------
# POST /api/user-management/suspend-user
# ---------------------------------------------------------------------------

class TestSuspendUser:

    def test_suspends_user_successfully(self, auth_client):
        with mock.patch("asset_management.web.app.UserManagementService.suspend_user", return_value={"success": True}) as m, \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(auth_client, "/api/user-management/suspend-user", {"username": "victim"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        m.assert_called_once_with("victim")

    def test_missing_username_returns_400(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/suspend-user", {"username": ""})
        assert resp.status_code == 400
        assert "Username" in resp.get_json()["error"]

    def test_cannot_suspend_self(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/suspend-user", {"username": "testuser"})
        assert resp.status_code == 400
        assert "your own" in resp.get_json()["error"].lower()

    def test_cannot_suspend_elastic(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/suspend-user", {"username": "elastic"})
        assert resp.status_code == 400
        assert "elastic" in resp.get_json()["error"].lower()

    def test_service_error_returns_400(self, auth_client):
        with mock.patch("asset_management.web.app.UserManagementService.suspend_user",
                        return_value={"success": False, "error": "Already suspended"}):
            resp = _json_post(auth_client, "/api/user-management/suspend-user", {"username": "victim"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Already suspended"

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = _json_post(org_user_client, "/api/user-management/suspend-user", {"username": "x"})
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, client):
        # AJAX/API call → 401 + JSON, not a 302 to /login.
        resp = _json_post(client, "/api/user-management/suspend-user", {"username": "x"})
        assert resp.status_code == 401

    def test_audit_log_written_on_success(self, auth_client):
        with mock.patch("asset_management.web.app.UserManagementService.suspend_user", return_value={"success": True}), \
             mock.patch("asset_management.web.app.AuditLogService.log_event") as m_audit:
            _json_post(auth_client, "/api/user-management/suspend-user", {"username": "victim"})
        m_audit.assert_called_once()
        assert m_audit.call_args[0][1] == "suspend_user"
        assert m_audit.call_args[1]["target_user"] == "victim"


# ---------------------------------------------------------------------------
# POST /api/user-management/reinstate-user
# ---------------------------------------------------------------------------

def _owner_suspended_target():
    """reinstate refuses non-superusers unless the account's metadata
    attributes its CURRENT suspension to a non-platform operator. Stub ES so the
    target reads as suspended by a workspace owner."""
    from asset_management.web import app as app_mod

    suspended = {
        "enabled": False,
        "metadata": {
            "suspended_at": "2026-10-10T00:00:00+00:00",
            "suspended_by": {
                "username": "owner1",
                "platform_admin": False,
                "suspended_at": "2026-10-10T00:00:00+00:00",
            },
        },
    }
    return mock.patch.multiple(
        app_mod.UserManagementService,
        _get_user=mock.Mock(return_value=(suspended, None)),
    ), mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock.Mock())


class TestReinstateUser:

    @pytest.fixture(autouse=True)
    def _target_suspended_by_an_owner(self):
        user_patch, es_patch = _owner_suspended_target()
        with user_patch, es_patch:
            yield

    def test_reinstates_user_successfully(self, auth_client):
        with mock.patch("asset_management.web.app.UserManagementService.reinstate_user", return_value={"success": True}) as m, \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(auth_client, "/api/user-management/reinstate-user", {"username": "victim"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        m.assert_called_once_with("victim")

    def test_missing_username_returns_400(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/reinstate-user", {"username": ""})
        assert resp.status_code == 400

    def test_service_error_returns_400(self, auth_client):
        with mock.patch("asset_management.web.app.UserManagementService.reinstate_user",
                        return_value={"success": False, "error": "Not suspended"}):
            resp = _json_post(auth_client, "/api/user-management/reinstate-user", {"username": "victim"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Not suspended"

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = _json_post(org_user_client, "/api/user-management/reinstate-user", {"username": "x"})
        assert resp.status_code == 403

    def test_audit_log_written_on_success(self, auth_client):
        with mock.patch("asset_management.web.app.UserManagementService.reinstate_user", return_value={"success": True}), \
             mock.patch("asset_management.web.app.AuditLogService.log_event") as m_audit:
            _json_post(auth_client, "/api/user-management/reinstate-user", {"username": "victim"})
        m_audit.assert_called_once()
        assert m_audit.call_args[0][1] == "reinstate_user"


# ---------------------------------------------------------------------------
# POST /api/user-management/update-roles
# ---------------------------------------------------------------------------

class TestUpdateRoles:

    def test_updates_roles_successfully(self, auth_client):
        # v3 follow-up: split payload — system_roles writes only
        # to ES Security; memberships go to the authz indices.
        # ``auth_client`` is asset_admin (non-superuser); the
        # superuser-only roles are gated, so this happy-path assertion uses
        # an ordinary grantable role (report_publisher).
        result = {"success": True, "old_roles": [], "new_roles": ["report_publisher"]}
        with mock.patch("asset_management.web.app.UserManagementService.update_user_roles", return_value=result) as m, \
             mock.patch("asset_management.web.app.AuditLogService.log_event"), \
             mock.patch("asset_management.web.app._sync_new_authz_memberships"):
            resp = _json_post(auth_client, "/api/user-management/update-roles", {
                "username": "victim",
                "system_roles": ["report_publisher"],
                "memberships": ["org_test"],
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["system_roles"] == ["report_publisher"]
        assert data["memberships"] == ["org_test"]
        m.assert_called_once_with("victim", ["report_publisher"])

    def test_missing_username_returns_400(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/update-roles", {
            "username": "", "system_roles": [], "memberships": ["org_test"],
        })
        assert resp.status_code == 400

    def test_empty_roles_returns_400(self, auth_client):
        # Empty memberships is the rejection now — at least one scope is required.
        resp = _json_post(auth_client, "/api/user-management/update-roles", {
            "username": "victim", "system_roles": [], "memberships": [],
        })
        assert resp.status_code == 400

    def test_non_list_roles_returns_400(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/update-roles", {
            "username": "victim",
            "system_roles": "asset_admin",
            "memberships": ["org_test"],
        })
        assert resp.status_code == 400

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = _json_post(org_user_client, "/api/user-management/update-roles", {
            "username": "x", "system_roles": [], "memberships": ["org_test"],
        })
        assert resp.status_code == 403

    def test_non_superuser_cannot_add_superuser_only_role(self, auth_client):
        # asset_admin (non-superuser) ADDING a superuser-only role
        # the target doesn't already hold is refused 403; the role write is
        # never invoked. get_user_detail is stubbed to the "no existing
        # superuser-only roles" case.
        with mock.patch(
            "asset_management.web.app.UserManagementService.get_user_detail",
            return_value={"success": True, "roles": ["viewer"]},
        ), mock.patch("asset_management.web.app.UserManagementService.update_user_roles") as m_update:
            resp = _json_post(auth_client, "/api/user-management/update-roles", {
                "username": "victim",
                "system_roles": ["asset_admin"],
                "memberships": ["org_test"],
            })
        assert resp.status_code == 403
        assert "asset_admin" in resp.get_json()["error"]
        m_update.assert_not_called()

    def test_non_superuser_edit_preserves_existing_superuser_only_role(self, auth_client):
        # editing a target who ALREADY holds asset_admin must not
        # silently strip it just because the picker-hidden payload omits it.
        # The role write must carry asset_admin through even though the
        # non-superuser caller didn't (and couldn't) send it.
        result = {"success": True, "old_roles": ["asset_admin"], "new_roles": []}
        with mock.patch(
            "asset_management.web.app.UserManagementService.get_user_detail",
            return_value={"success": True, "roles": ["asset_admin", "viewer"]},
        ), mock.patch(
            "asset_management.web.app.UserManagementService.update_user_roles", return_value=result
        ) as m_update, mock.patch("asset_management.web.app.AuditLogService.log_event"), \
             mock.patch("asset_management.web.app._sync_new_authz_memberships"):
            resp = _json_post(auth_client, "/api/user-management/update-roles", {
                "username": "victim",
                "system_roles": ["report_publisher"],
                "memberships": ["org_test"],
            })
        assert resp.status_code == 200
        # The write preserves the pre-existing asset_admin alongside the
        # newly-sent report_publisher.
        called_roles = m_update.call_args[0][1]
        assert "asset_admin" in called_roles
        assert "report_publisher" in called_roles

    def test_audit_log_includes_role_diff(self, auth_client):
        # ordinary grantable role (asset_admin is superuser-only).
        result = {
            "success": True,
            "old_roles": ["viewer"],
            "new_roles": ["report_publisher"],
        }
        with mock.patch("asset_management.web.app.UserManagementService.update_user_roles", return_value=result), \
             mock.patch("asset_management.web.app.AuditLogService.log_event") as m_audit, \
             mock.patch("asset_management.web.app._sync_new_authz_memberships"):
            _json_post(auth_client, "/api/user-management/update-roles", {
                "username": "victim",
                "system_roles": ["report_publisher"],
                "memberships": ["org_test"],
            })
        m_audit.assert_called_once()
        # Detail string captures the new shape — system_roles + memberships.
        assert "system_roles" in m_audit.call_args[1]["detail"]
        assert "memberships" in m_audit.call_args[1]["detail"]


# ---------------------------------------------------------------------------
# GET /api/user-management/user-detail/<username>
# ---------------------------------------------------------------------------

class TestUserDetail:

    def test_returns_user_detail(self, auth_client):
        detail = {
            "success": True, "username": "alice", "full_name": "Alice",
            "email": "a@b.com", "roles": ["asset_admin"], "enabled": True,
            "metadata": {}, "mfa": {"enabled": False, "method": "", "last_used": ""},
            "login_history": [{"action": "login", "success": True}],
        }
        with mock.patch("asset_management.web.app.UserManagementService.get_user_detail", return_value=detail) as m:
            resp = auth_client.get("/api/user-management/user-detail/alice")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["username"] == "alice"
        assert len(data["login_history"]) == 1
        m.assert_called_once_with("alice")

    def test_user_not_found_returns_404(self, auth_client):
        with mock.patch("asset_management.web.app.UserManagementService.get_user_detail",
                        return_value={"success": False, "error": "User 'ghost' not found"}):
            resp = auth_client.get("/api/user-management/user-detail/ghost")
        assert resp.status_code == 404

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = org_user_client.get("/api/user-management/user-detail/alice")
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, client):
        # an /api/ path classified as an AJAX/API caller → 401 + JSON.
        resp = client.get("/api/user-management/user-detail/alice")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/user-management/bulk-action
# ---------------------------------------------------------------------------

class TestBulkAction:

    @pytest.fixture(autouse=True)
    def _targets_are_ordinary_users(self):
        # bulk-action refuses platform-admin targets for non-superusers and
        # reads roles fail-closed from ES; the auth_client operator is asset_admin.
        from asset_management.web import app as app_mod

        with mock.patch.object(app_mod.auth_module, "_resolve_es_roles", return_value=["kibana_user"]):
            yield

    def test_bulk_suspend(self, auth_client):
        result = {"success_count": 2, "fail_count": 0, "errors": []}
        with mock.patch("asset_management.web.app.UserManagementService.bulk_action", return_value=result) as m, \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(auth_client, "/api/user-management/bulk-action", {
                "action": "suspend", "usernames": ["user1", "user2"],
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success_count"] == 2
        assert data["fail_count"] == 0
        m.assert_called_once_with("suspend", ["user1", "user2"], admin_username="testuser")

    def test_bulk_reinstate(self, auth_client):
        result = {"success_count": 1, "fail_count": 0, "errors": []}
        user_patch, es_patch = _owner_suspended_target()
        with mock.patch("asset_management.web.app.UserManagementService.bulk_action", return_value=result), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"), user_patch, es_patch:
            resp = _json_post(auth_client, "/api/user-management/bulk-action", {
                "action": "reinstate", "usernames": ["user1"],
            })
        assert resp.status_code == 200
        assert resp.get_json()["success_count"] == 1

    def test_bulk_delete(self, auth_client):
        result = {"success_count": 1, "fail_count": 0, "errors": []}
        with mock.patch("asset_management.web.app.UserManagementService.bulk_action", return_value=result), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(auth_client, "/api/user-management/bulk-action", {
                "action": "delete", "usernames": ["user1"],
            })
        assert resp.status_code == 200

    def test_invalid_action_returns_400(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/bulk-action", {
            "action": "promote", "usernames": ["user1"],
        })
        assert resp.status_code == 400
        assert "Invalid action" in resp.get_json()["error"]

    def test_empty_usernames_returns_400(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/bulk-action", {
            "action": "suspend", "usernames": [],
        })
        assert resp.status_code == 400

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = _json_post(org_user_client, "/api/user-management/bulk-action", {
            "action": "suspend", "usernames": ["x"],
        })
        assert resp.status_code == 403

    def test_audit_events_logged_per_user(self, auth_client):
        result = {"success_count": 2, "fail_count": 0, "errors": []}
        with mock.patch("asset_management.web.app.UserManagementService.bulk_action", return_value=result), \
             mock.patch("asset_management.web.app.AuditLogService.log_event") as m_audit:
            _json_post(auth_client, "/api/user-management/bulk-action", {
                "action": "suspend", "usernames": ["user1", "user2"],
            })
        assert m_audit.call_count == 2
        actions = [c[0][1] for c in m_audit.call_args_list]
        assert all(a == "suspend_user" for a in actions)

    def test_audit_skipped_for_failed_users(self, auth_client):
        result = {"success_count": 1, "fail_count": 1, "errors": [{"username": "user2", "error": "Protected"}]}
        with mock.patch("asset_management.web.app.UserManagementService.bulk_action", return_value=result), \
             mock.patch("asset_management.web.app.AuditLogService.log_event") as m_audit:
            _json_post(auth_client, "/api/user-management/bulk-action", {
                "action": "delete", "usernames": ["user1", "user2"],
            })
        # Only user1 should get an audit event (user2 failed)
        assert m_audit.call_count == 1
        assert m_audit.call_args[1]["target_user"] == "user1"


# ---------------------------------------------------------------------------
# GET /api/user-management/audit-log
# ---------------------------------------------------------------------------

class TestAuditLog:

    # Login Activity endpoints gated on superuser, not _require_admin.
    # The positive-path tests below run as superuser; the per-class
    # negative-path tests still hit asset_admin / org_user / anon via
    # their own fixtures.

    def test_returns_events(self, superuser_client):
        fake_result = {"events": [{"action": "login"}], "total": 1}
        with mock.patch("asset_management.web.app.AuditLogService.query_events", return_value=fake_result) as m_query:
            resp = superuser_client.get("/api/user-management/audit-log")
        assert resp.status_code == 200
        assert resp.get_json()["total"] == 1
        m_query.assert_called_once_with(
            username="",
            action="",
            success=None,
            exclude_scanners=False,
            username_in=None,
            size=100,
            from_offset=0,
        )

    def test_passes_query_params(self, superuser_client):
        with mock.patch("asset_management.web.app.AuditLogService.query_events", return_value={}) as m_query:
            superuser_client.get("/api/user-management/audit-log?username=alice&action=login&success=true&size=50&from=10&exclude_scanners=true")
        m_query.assert_called_once_with(
            username="alice",
            action="login",
            success=True,
            exclude_scanners=True,
            username_in=None,
            size=50,
            from_offset=10,
        )

    def test_success_false_param(self, superuser_client):
        with mock.patch("asset_management.web.app.AuditLogService.query_events", return_value={}) as m_query:
            superuser_client.get("/api/user-management/audit-log?success=false")
        assert m_query.call_args[1]["success"] is False

    def test_size_capped_at_500(self, superuser_client):
        with mock.patch("asset_management.web.app.AuditLogService.query_events", return_value={}) as m_query:
            superuser_client.get("/api/user-management/audit-log?size=9999")
        assert m_query.call_args[1]["size"] == 500

    def test_from_offset_cannot_be_negative(self, superuser_client):
        with mock.patch("asset_management.web.app.AuditLogService.query_events", return_value={}) as m_query:
            superuser_client.get("/api/user-management/audit-log?from=-5")
        assert m_query.call_args[1]["from_offset"] == 0

    def test_active_only_resolves_usernames_and_passes_to_query(self, superuser_client):
        with mock.patch("asset_management.web.app.AuditLogService.query_events", return_value={}) as m_query, \
             mock.patch("asset_management.web.app.AuditLogService.active_usernames", return_value={"alice", "bob"}) as m_active:
            superuser_client.get("/api/user-management/audit-log?active_only=true")
        m_active.assert_called_once()
        passed = m_query.call_args[1]["username_in"]
        assert sorted(passed) == ["alice", "bob"]

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = org_user_client.get("/api/user-management/audit-log")
        assert resp.status_code == 403

    def test_unauthenticated_returns_401(self, client):
        # an /api/ path classified as an AJAX/API caller → 401 + JSON.
        resp = client.get("/api/user-management/audit-log")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/user-management/login-trend
# ---------------------------------------------------------------------------

class TestLoginTrend:

    # superuser-only endpoint.

    def test_returns_points(self, superuser_client):
        points = [{"date": "2026-04-15", "count": 3}, {"date": "2026-04-16", "count": 5}]
        with mock.patch("asset_management.web.app.AuditLogService.daily_unique_logins", return_value=points) as m_trend:
            resp = superuser_client.get("/api/user-management/login-trend")
        assert resp.status_code == 200
        assert resp.get_json() == {"points": points}
        assert m_trend.call_args[1]["days"] == 30

    def test_days_param_clamped(self, superuser_client):
        with mock.patch("asset_management.web.app.AuditLogService.daily_unique_logins", return_value=[]) as m_trend:
            superuser_client.get("/api/user-management/login-trend?days=9999")
        assert m_trend.call_args[1]["days"] == 365

    def test_invalid_days_falls_back_to_default(self, superuser_client):
        with mock.patch("asset_management.web.app.AuditLogService.daily_unique_logins", return_value=[]) as m_trend:
            superuser_client.get("/api/user-management/login-trend?days=abc")
        assert m_trend.call_args[1]["days"] == 30

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = org_user_client.get("/api/user-management/login-trend")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/user-management/login-users
# ---------------------------------------------------------------------------

class TestLoginUsersEndpoint:

    # superuser-only endpoint.

    def test_returns_user_list_for_date(self, superuser_client):
        users = [{"username": "alice", "login_count": 2, "first_login": "x", "last_login": "y"}]
        with mock.patch("asset_management.web.app.AuditLogService.logins_for_day", return_value=users) as m:
            resp = superuser_client.get("/api/user-management/login-users?date=2026-04-15")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body == {"date": "2026-04-15", "users": users, "count": 1}
        assert m.call_args[1]["date"] == "2026-04-15"

    def test_invalid_date_returns_400(self, superuser_client):
        resp = superuser_client.get("/api/user-management/login-users?date=yesterday")
        assert resp.status_code == 400
        assert "YYYY-MM-DD" in resp.get_json()["error"]

    def test_missing_date_returns_400(self, superuser_client):
        resp = superuser_client.get("/api/user-management/login-users")
        assert resp.status_code == 400

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = org_user_client.get("/api/user-management/login-users?date=2026-04-15")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/user-management/active-users
# ---------------------------------------------------------------------------

class TestActiveUsersEndpoint:

    # superuser-only endpoint.
    # the authoritative "active now" signal is the per-request
    # activity heartbeat (PresenceService.present_usernames), not the old
    # login-age proxy (AuditLogService.active_usernames).

    def test_returns_sorted_usernames_and_count(self, superuser_client):
        with mock.patch("asset_management.web.app.PresenceService.present_usernames", return_value={"bob", "alice"}):
            resp = superuser_client.get("/api/user-management/active-users")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["count"] == 2
        assert body["usernames"] == ["alice", "bob"]

    def test_minutes_param_clamped(self, superuser_client):
        with mock.patch("asset_management.web.app.PresenceService.present_usernames", return_value=set()) as m_present:
            superuser_client.get("/api/user-management/active-users?minutes=99999")
        assert m_present.call_args[1]["minutes"] == 1440

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = org_user_client.get("/api/user-management/active-users")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/user-management/sync-roles
# ---------------------------------------------------------------------------

# TestSyncRoles deleted with /api/user-management/sync-roles.

# TestCreateRole deleted with /api/user-management/create-role.

# TestRoleSyncStatus deleted with /api/user-management/role-sync-status.

class TestEnableMFA:

    def test_enable_succeeds(self, auth_client):
        result = {"success": True}
        with mock.patch("asset_management.web.app.MFAService.admin_enable_mfa", return_value=result) as m:
            resp = _json_post(auth_client, "/api/user-management/enable-mfa", {
                "username": "alice",
            })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        m.assert_called_once_with("alice", enabled_by="testuser")

    def test_missing_username_returns_400(self, auth_client):
        resp = _json_post(auth_client, "/api/user-management/enable-mfa", {
            "username": "",
        })
        assert resp.status_code == 400

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = _json_post(org_user_client, "/api/user-management/enable-mfa", {
            "username": "alice",
        })
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/user-management/disable-mfa
# ---------------------------------------------------------------------------

class TestDisableMFA:

    def test_superuser_can_disable_mfa(self, superuser_client):
        """Only superusers can disable MFA."""
        result = {"success": True}
        with mock.patch("asset_management.web.app.MFAService.admin_disable_mfa", return_value=result) as m:
            resp = _json_post(superuser_client, "/api/user-management/disable-mfa", {
                "username": "alice",
            })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        m.assert_called_once_with("alice")

    def test_asset_admin_cannot_disable_mfa(self, auth_client):
        """asset_admin is not sufficient — superuser required."""
        resp = _json_post(auth_client, "/api/user-management/disable-mfa", {
            "username": "alice",
        })
        assert resp.status_code == 403

    def test_missing_username_returns_400(self, superuser_client):
        resp = _json_post(superuser_client, "/api/user-management/disable-mfa", {
            "username": "",
        })
        assert resp.status_code == 400

    def test_forbidden_for_non_admin(self, org_user_client):
        resp = _json_post(org_user_client, "/api/user-management/disable-mfa", {
            "username": "alice",
        })
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /change-password (forced password change page)
# ---------------------------------------------------------------------------

class TestChangePasswordPage:

    def test_redirects_without_fpc_session(self, client):
        resp = client.get("/change-password")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_returns_200_with_fpc_session(self, client):
        _seed_fpc_session(client)
        resp = client.get("/change-password")
        assert resp.status_code == 200

    def test_renders_username_in_form(self, client):
        _seed_fpc_session(client, username="jdoe")
        resp = client.get("/change-password")
        html = resp.data.decode()
        assert "jdoe" in html

    def test_displays_error_from_query_param(self, client):
        _seed_fpc_session(client)
        resp = client.get("/change-password?error=Bad+password")
        html = resp.data.decode()
        assert "Bad password" in html


# ---------------------------------------------------------------------------
# POST /change-password (forced password change processing)
# ---------------------------------------------------------------------------

class TestChangePasswordPost:

    def test_unauthenticated_without_fpc_session_returns_401(self, client):
        # CsrfTestClient injects X-CSRF-Token on POST, so this state
        # change is classified as an AJAX/API caller → 401 + JSON (login_url in
        # the body) rather than a 302 to /login.
        resp = client.post("/change-password", data={"new_password": "x"})
        assert resp.status_code == 401
        assert "login" in (resp.get_json() or {}).get("login_url", "").lower()

    def test_invalid_csrf_redirects_with_error(self, client):
        _seed_fpc_session(client)
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=False):
            resp = client.post("/change-password", data={
                "csrf_token": "bad-token",
                "current_password": "temp",
                "new_password": "NewPass123!",
                "confirm_password": "NewPass123!",
            })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_missing_current_password_redirects(self, client):
        _seed_fpc_session(client)
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True):
            resp = client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "",
                "new_password": "NewPass123!",
                "confirm_password": "NewPass123!",
            })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_wrong_temp_password_redirects(self, client):
        _seed_fpc_session(client)
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": False}):
            resp = client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "wrong",
                "new_password": "NewPass123!",
                "confirm_password": "NewPass123!",
            })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_short_password_redirects(self, client):
        _seed_fpc_session(client)
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}):
            resp = client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "temp",
                "new_password": "short",
                "confirm_password": "short",
            })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_same_as_temp_password_redirects(self, client):
        _seed_fpc_session(client)
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}):
            resp = client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "TempPass1",
                "new_password": "TempPass1",
                "confirm_password": "TempPass1",
            })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_mismatched_passwords_redirects(self, client):
        _seed_fpc_session(client)
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}):
            resp = client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "TempPass1",
                "new_password": "NewPassword1!",
                "confirm_password": "DifferentPw1!",
            })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_es_unavailable_redirects_with_error(self, client):
        _seed_fpc_session(client)
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            resp = client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "TempPass1",
                "new_password": "NewPassword1!",
                "confirm_password": "NewPassword1!",
            })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_es_change_password_failure_redirects(self, client):
        _seed_fpc_session(client)
        mock_es = mock.MagicMock()
        mock_es.security.change_password.side_effect = Exception("ES error")
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            resp = client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "TempPass1",
                "new_password": "NewPassword1!",
                "confirm_password": "NewPassword1!",
            })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_successful_password_change_redirects_to_home(self, client):
        _seed_fpc_session(client)
        mock_es = mock.MagicMock()
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.UserProvisioningService.clear_force_password_change"), \
             mock.patch("asset_management.web.app.create_kibana_session", return_value=None):
            resp = client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "TempPass1",
                "new_password": "NewPassword1!",
                "confirm_password": "NewPassword1!",
            })
        assert resp.status_code == 302
        # After force-password-change, all users land on /dashboard.
        assert resp.headers["Location"].rstrip("/").endswith("/dashboard")

    def test_successful_change_sets_session(self, client):
        _seed_fpc_session(client)
        mock_es = mock.MagicMock()
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.UserProvisioningService.clear_force_password_change"), \
             mock.patch("asset_management.web.app.create_kibana_session", return_value=None):
            client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "TempPass1",
                "new_password": "NewPassword1!",
                "confirm_password": "NewPassword1!",
            })
        with client.session_transaction() as sess:
            assert sess["authenticated"] is True
            assert sess["username"] == "newuser"
            assert sess.get("fpc_pending") is None

    def test_successful_change_sets_kibana_cookie(self, client):
        _seed_fpc_session(client)
        mock_es = mock.MagicMock()
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.UserProvisioningService.clear_force_password_change"), \
             mock.patch("asset_management.web.app.create_kibana_session", return_value="kibana-sid-123"):
            resp = client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "TempPass1",
                "new_password": "NewPassword1!",
                "confirm_password": "NewPassword1!",
            })
        # Check that Set-Cookie header contains the sid cookie
        cookie_header = resp.headers.get("Set-Cookie", "")
        assert "sid=" in cookie_header

    def test_non_admin_redirects_to_dashboard(self, client):
        """Non-admin fpc user should redirect to /dashboard after password change."""
        with client.session_transaction() as sess:
            sess["fpc_pending"] = True
            sess["fpc_username"] = "orguser"
            sess["fpc_roles"] = ["org_test123"]
            sess["fpc_full_name"] = "Org User"
            sess["fpc_email"] = "org@example.com"
            sess["fpc_timestamp"] = time.time()
            sess["csrf_token"] = "test-csrf-token"

        mock_es = mock.MagicMock()
        with mock.patch("asset_management.web.app.auth_module.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={"success": True}), \
             mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("asset_management.web.app.UserProvisioningService.clear_force_password_change"), \
             mock.patch("asset_management.web.app.create_kibana_session", return_value=None):
            resp = client.post("/change-password", data={
                "csrf_token": "test-csrf-token",
                "current_password": "TempPass1",
                "new_password": "NewPassword1!",
                "confirm_password": "NewPassword1!",
            })
        assert resp.status_code == 302
        assert resp.headers["Location"].rstrip("/").endswith("/dashboard")


# ---------------------------------------------------------------------------
# POST /api/user-management/reset-password
# ---------------------------------------------------------------------------

class TestResetPassword:

    def _svc_result(self, email="alice@example.com"):
        return {
            "success": True,
            "temp_password": "TempPass123xyz45",
            "email": email,
            "full_name": "Alice",
        }

    def test_superuser_can_reset_and_email(self, superuser_client):
        with mock.patch("asset_management.web.app.UserProvisioningService.reset_password",
                        return_value=self._svc_result()) as m_reset, \
             mock.patch("asset_management.web.app.UserProvisioningService.send_password_reset_email",
                        return_value={"success": True}) as m_email, \
             mock.patch("asset_management.web.app.AuditLogService.log_event") as audit:
            resp = _json_post(superuser_client, "/api/user-management/reset-password", {
                "username": "alice",
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["email_sent"] is True
        assert "temp_password" not in data  # never leaked when email is used
        m_reset.assert_called_once_with("alice")
        m_email.assert_called_once()
        audit.assert_called_once()
        kwargs = audit.call_args.kwargs
        assert kwargs["target_user"] == "alice"

    def test_skip_email_returns_temp_password(self, superuser_client):
        with mock.patch("asset_management.web.app.UserProvisioningService.reset_password",
                        return_value=self._svc_result()), \
             mock.patch("asset_management.web.app.UserProvisioningService.send_password_reset_email") as m_email, \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(superuser_client, "/api/user-management/reset-password", {
                "username": "alice",
                "skip_email": True,
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["skip_email"] is True
        assert data["email_sent"] is False
        assert data["temp_password"] == "TempPass123xyz45"
        m_email.assert_not_called()

    def test_no_email_on_file_returns_temp_password(self, superuser_client):
        """User with no email falls through to the skip-email response path."""
        with mock.patch("asset_management.web.app.UserProvisioningService.reset_password",
                        return_value=self._svc_result(email="")), \
             mock.patch("asset_management.web.app.UserProvisioningService.send_password_reset_email") as m_email, \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(superuser_client, "/api/user-management/reset-password", {
                "username": "alice",
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["temp_password"] == "TempPass123xyz45"
        m_email.assert_not_called()

    def test_asset_admin_can_reset(self, auth_client):
        """reset-password is no longer superuser-only. asset_admin
        is a cluster admin and short-circuits the _require_permission gate
        (same contract as the other destructive user-management routes), so
        the reset now succeeds instead of 403'ing. (earlier this asserted
        403 — superuser-only.)"""
        with mock.patch("asset_management.web.app.UserProvisioningService.reset_password",
                        return_value=self._svc_result()), \
             mock.patch("asset_management.web.app.UserProvisioningService.send_password_reset_email",
                        return_value={"success": True}), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(auth_client, "/api/user-management/reset-password", {
                "username": "alice",
            })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_org_user_returns_403(self, org_user_client):
        """a non-privileged org-tier user (no WORKSPACE_DELETE, not a
        cluster admin) is still denied the reset-password endpoint."""
        with mock.patch(
            "asset_management.web.services.permission_resolver.get_elasticsearch_client",
            return_value=None,
        ):
            resp = _json_post(org_user_client, "/api/user-management/reset-password", {
                "username": "alice",
            })
        assert resp.status_code == 403

    def test_missing_username_returns_400(self, superuser_client):
        resp = _json_post(superuser_client, "/api/user-management/reset-password", {
            "username": "",
        })
        assert resp.status_code == 400

    def test_self_reset_rejected(self, superuser_client):
        """Superuser cannot reset their own password via this endpoint."""
        resp = _json_post(superuser_client, "/api/user-management/reset-password", {
            "username": "superuser",
        })
        assert resp.status_code == 400
        assert "own password" in resp.get_json()["error"].lower()

    def test_service_failure_returns_500(self, superuser_client):
        with mock.patch("asset_management.web.app.UserProvisioningService.reset_password",
                        return_value={"success": False, "error": "ES down"}):
            resp = _json_post(superuser_client, "/api/user-management/reset-password", {
                "username": "alice",
            })
        assert resp.status_code == 500
        assert "ES down" in resp.get_json()["error"]

    def test_email_send_failure_still_succeeds(self, superuser_client):
        """Password was reset successfully — surface the email error but don't undo the reset."""
        with mock.patch("asset_management.web.app.UserProvisioningService.reset_password",
                        return_value=self._svc_result()), \
             mock.patch("asset_management.web.app.UserProvisioningService.send_password_reset_email",
                        return_value={"success": False, "error": "SMTP bad"}), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(superuser_client, "/api/user-management/reset-password", {
                "username": "alice",
            })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["email_sent"] is False
        assert data["email_error"] == "SMTP bad"


# ---------------------------------------------------------------------------
# POST /api/user-management/admin-set-password
# ---------------------------------------------------------------------------

class TestAdminSetPassword:
    """Admin sets a user's password to a known value, no email sent.

    Required for service / demo accounts (e.g. kavanah_demo) where emailing a
    temporary password defeats the purpose. Distinct from /reset-password,
    which generates a random temp password and emails it.
    """

    URL = "/api/user-management/admin-set-password"
    VALID = {
        "username": "alice",
        "new_password": "Abcdef1!ghij",
        "reason": "demo account seed",
    }

    def test_unauthenticated_returns_401(self, client):
        # AJAX/API call (X-CSRF-Token on POST) → 401 + JSON.
        resp = _json_post(client, self.URL, self.VALID)
        assert resp.status_code == 401

    def test_non_admin_returns_403(self, org_user_client):
        resp = _json_post(org_user_client, self.URL, self.VALID)
        assert resp.status_code == 403

    def test_admin_succeeds_on_happy_path(self, auth_client):
        with mock.patch("asset_management.web.app.UserProvisioningService.admin_set_password",
                        return_value={"success": True}) as svc, \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(auth_client, self.URL, self.VALID)
        assert resp.status_code == 200
        assert resp.get_json() == {"success": True}
        # Verify the service was called with session-derived set_by.
        svc.assert_called_once()
        kwargs = svc.call_args.kwargs
        assert kwargs["username"] == "alice"
        assert kwargs["new_password"] == "Abcdef1!ghij"
        assert kwargs["reason"] == "demo account seed"
        assert kwargs["set_by"] == "testuser"  # auth_client fixture username

    def test_superuser_succeeds(self, superuser_client):
        with mock.patch("asset_management.web.app.UserProvisioningService.admin_set_password",
                        return_value={"success": True}), \
             mock.patch("asset_management.web.app.AuditLogService.log_event"):
            resp = _json_post(superuser_client, self.URL, self.VALID)
        assert resp.status_code == 200

    def test_missing_username_returns_400(self, auth_client):
        payload = {**self.VALID, "username": ""}
        resp = _json_post(auth_client, self.URL, payload)
        assert resp.status_code == 400
        assert "Username" in resp.get_json()["error"]

    def test_missing_password_returns_400(self, auth_client):
        payload = {**self.VALID, "new_password": ""}
        resp = _json_post(auth_client, self.URL, payload)
        assert resp.status_code == 400
        assert "new_password" in resp.get_json()["error"]

    def test_missing_reason_returns_400(self, auth_client):
        payload = {**self.VALID, "reason": "  "}
        resp = _json_post(auth_client, self.URL, payload)
        assert resp.status_code == 400
        assert "reason" in resp.get_json()["error"].lower()

    def test_complexity_failure_returns_400(self, auth_client):
        with mock.patch("asset_management.web.app.UserProvisioningService.admin_set_password",
                        return_value={"success": False, "error": "Password must be at least 12 characters"}):
            resp = _json_post(auth_client, self.URL, self.VALID)
        assert resp.status_code == 400
        assert "12 characters" in resp.get_json()["error"]

    def test_user_not_found_returns_404(self, auth_client):
        with mock.patch("asset_management.web.app.UserProvisioningService.admin_set_password",
                        return_value={"success": False, "error": "User 'ghost' not found"}):
            resp = _json_post(auth_client, self.URL, {**self.VALID, "username": "ghost"})
        assert resp.status_code == 404

    def test_built_in_user_returns_400(self, auth_client):
        with mock.patch("asset_management.web.app.UserProvisioningService.admin_set_password",
                        return_value={"success": False, "error": "Cannot set password for built-in user 'elastic'"}):
            resp = _json_post(auth_client, self.URL, {**self.VALID, "username": "elastic"})
        assert resp.status_code == 400

    def test_service_unknown_failure_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.UserProvisioningService.admin_set_password",
                        return_value={"success": False, "error": "ES down"}):
            resp = _json_post(auth_client, self.URL, self.VALID)
        assert resp.status_code == 500

    def test_audit_event_logged_on_success(self, auth_client):
        with mock.patch("asset_management.web.app.UserProvisioningService.admin_set_password",
                        return_value={"success": True}), \
             mock.patch("asset_management.web.app.AuditLogService.log_event") as audit:
            _json_post(auth_client, self.URL, self.VALID)
        audit.assert_called_once()
        # Audit reason ends up in the detail field, not as a top-level kw.
        kwargs = audit.call_args.kwargs
        assert kwargs.get("target_user") == "alice"
        assert "demo account seed" in kwargs.get("detail", "")


class TestUserManagementDocsWorkspaceAnchor:
    """/user-management emits the viewer's active workspace TITLE into
    a hidden ``data-docs-workspace`` attribute on ``.page-header``.

    Why it exists: the docs-screenshot gate refuses to publish a
    capture that cannot prove its workspace context, and this page shows no
    workspace name to a non-superuser — the workspace filter bar above the
    table is inside ``{% if is_superuser %}``. Without the anchor, every
    demo-account capture of this page would fail the gate. Same
    attribute-only pattern as /dashboard's #dashboard-toolbar.

    Mutation pins (both verified): drop ``docs_workspace=_docs_workspace`` from
    the render call, or drop the template's ``{% if docs_workspace %}`` guard
    so the attribute renders empty — either fails the first test.
    """

    @staticmethod
    def _patched_resolver(title="Kavanah Demo", esid="kavanah-demo"):
        import importlib

        try:
            db_routes = importlib.import_module("web.blueprints.dashboard_builder.routes")
        except ModuleNotFoundError:  # pragma: no cover - path-setup dependent
            db_routes = importlib.import_module("asset_management.web.blueprints.dashboard_builder.routes")
        return (
            mock.patch.object(
                db_routes, "_selectable_workspaces_for_user", return_value=[{"esid": esid, "title": title}]
            ),
            mock.patch.object(db_routes, "_default_workspace_esid", return_value=esid),
        )

    def _render(self, client, resolver_patches):
        sel, default = resolver_patches
        with mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=[]), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=[]), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=[]), \
             sel, default:
            resp = client.get("/user-management")
        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    def test_emits_the_workspace_anchor_on_the_page_header(self, auth_client):
        html = self._render(auth_client, self._patched_resolver())
        assert 'data-docs-workspace="Kavanah Demo"' in html
        # Attribute-only: it must ride on .page-header, never as visible copy.
        assert 'class="page-header" data-docs-workspace="Kavanah Demo"' in html

    def test_omits_the_attribute_when_the_workspace_cannot_be_resolved(self, auth_client):
        """Fail-soft: an unresolvable workspace omits the attribute rather than
        emitting an empty one. The capture gate then fail-closes (no marker →
        nothing publishes), which is the safe outcome."""
        html = self._render(auth_client, self._patched_resolver(title="", esid=""))
        assert "data-docs-workspace" not in html
        # ...and the page still renders normally.
        assert "User Management" in html

    def test_resolver_failure_never_blocks_the_page(self, auth_client):
        import importlib

        try:
            db_routes = importlib.import_module("web.blueprints.dashboard_builder.routes")
        except ModuleNotFoundError:  # pragma: no cover
            db_routes = importlib.import_module("asset_management.web.blueprints.dashboard_builder.routes")
        with mock.patch("asset_management.web.app.MFAService.list_all_users_with_mfa", return_value=[]), \
             mock.patch("lib.es_helpers.get_all_documents", return_value=[]), \
             mock.patch("asset_management.web.app.RoleManagementService.get_all_role_names", return_value=[]), \
             mock.patch.object(db_routes, "_selectable_workspaces_for_user", side_effect=RuntimeError("ES down")):
            resp = auth_client.get("/user-management")
        assert resp.status_code == 200
        assert "data-docs-workspace" not in resp.get_data(as_text=True)
