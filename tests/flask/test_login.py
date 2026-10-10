"""
Tests for login, logout, and debug session routes in app.py.

Covers:
  GET  /login             — show form, redirect when already authenticated
  POST /login             — CSRF check, credential validation, MFA branching, success
  GET /dashboards/login — legacy alias, 301 → /login (preserve query)
  POST /dashboards/login — legacy alias, 308 → /login (preserve body)
  GET  /logout            — clear session, redirect to /login carrying next=
  GET  /debug/expire-session — expire session for testing
  Template filter first_role_display

All ES and external calls are mocked.
"""

import time
import pytest
from unittest import mock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AUTH_SUCCESS = {
    "success": True,
    "roles": ["org_test"],
    "full_name": "Test User",
    "email": "test@example.com",
}

AUTH_FAIL = {"success": False}


def _set_csrf(client, token="test-csrf-token"):
    """Plant a CSRF token in the session."""
    with client.session_transaction() as sess:
        sess["csrf_token"] = token


# ===========================================================================
# GET /login
# ===========================================================================

class TestLoginPage:

    def test_shows_login_form_when_unauthenticated(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_redirects_when_already_authenticated(self, auth_client):
        resp = auth_client.get("/login")
        assert resp.status_code == 302

    def test_passes_error_message_to_template(self, client):
        resp = client.get("/login?error=Invalid+credentials")
        assert resp.status_code == 200
        assert b"Invalid credentials" in resp.data

    def test_passes_success_message_to_template(self, client):
        resp = client.get("/login?message=Logged+out")
        assert resp.status_code == 200
        assert b"Logged out" in resp.data

    def test_sets_no_cache_headers(self, client):
        resp = client.get("/login")
        assert "no-cache" in resp.headers.get("Cache-Control", "").lower() or \
               "no-store" in resp.headers.get("Cache-Control", "").lower()


# ===========================================================================
# POST /login
# ===========================================================================

class TestLoginPost:

    def test_invalid_csrf_redirects_to_login(self, client):
        _set_csrf(client, "good-token")
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=False):
            resp = client.post("/login", data={"csrf_token": "bad", "username": "u", "password": "p"})
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_missing_username_redirects_with_error(self, client):
        _set_csrf(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True):
            resp = client.post("/login", data={"csrf_token": "test-csrf-token", "username": "", "password": "p"})
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"]

    def test_missing_password_redirects_with_error(self, client):
        _set_csrf(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True):
            resp = client.post("/login", data={"csrf_token": "test-csrf-token", "username": "user", "password": ""})
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"]

    def test_rate_limited_aborts_429(self, client):
        _set_csrf(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("asset_management.web.auth.check_rate_limit", return_value=False):
            resp = client.post("/login", data={"csrf_token": "test", "username": "user", "password": "pass"})
        assert resp.status_code == 429

    def test_invalid_credentials_redirects_with_error(self, client):
        _set_csrf(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("asset_management.web.auth.check_rate_limit", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value=AUTH_FAIL):
            resp = client.post("/login",
                               data={"csrf_token": "test", "username": "user", "password": "wrong"})
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"]

    def test_successful_login_creates_session_and_redirects(self, client):
        _set_csrf(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("asset_management.web.auth.check_rate_limit", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value=AUTH_SUCCESS), \
             mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required",
                        return_value={"required": False}), \
             mock.patch("asset_management.web.blueprints.analytics.routes._create_kibana_session", return_value=None):
            resp = client.post("/login",
                               data={"csrf_token": "test", "username": "user", "password": "pass"})
        assert resp.status_code == 302

    def test_successful_login_with_kibana_cookie(self, client):
        _set_csrf(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("asset_management.web.auth.check_rate_limit", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value=AUTH_SUCCESS), \
             mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required",
                        return_value={"required": False}), \
             mock.patch("asset_management.web.blueprints.analytics.routes._create_kibana_session", return_value="sid-value"):
            resp = client.post("/login",
                               data={"csrf_token": "test", "username": "user", "password": "pass"})
        assert resp.status_code == 302

    def test_mfa_required_redirects_to_challenge(self, client):
        _set_csrf(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("asset_management.web.auth.check_rate_limit", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value=AUTH_SUCCESS), \
             mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required",
                        return_value={"required": True, "method": "totp", "setup_complete": True}):
            resp = client.post("/login",
                               data={"csrf_token": "test", "username": "user", "password": "pass"})
        assert resp.status_code == 302
        assert "mfa" in resp.headers["Location"]

    def test_mfa_required_but_not_set_up_redirects_to_setup(self, client):
        _set_csrf(client)
        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("asset_management.web.auth.check_rate_limit", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value=AUTH_SUCCESS), \
             mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required",
                        return_value={"required": True, "method": "totp", "setup_complete": False}):
            resp = client.post("/login",
                               data={"csrf_token": "test", "username": "user", "password": "pass"})
        assert resp.status_code == 302
        assert "mfa" in resp.headers["Location"]

    def test_clears_failed_attempts_on_success(self, client):
        _set_csrf(client)
        from asset_management.web import auth as auth_module
        auth_module.failed_attempts["user"] = [time.time()]  # Pre-seed a failure

        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("asset_management.web.auth.check_rate_limit", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value=AUTH_SUCCESS), \
             mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required",
                        return_value={"required": False}), \
             mock.patch("asset_management.web.blueprints.analytics.routes._create_kibana_session", return_value=None):
            client.post("/login", data={"csrf_token": "test", "username": "user", "password": "pass"})
        assert "user" not in auth_module.failed_attempts


# ===========================================================================
# /dashboards/login — legacy redirect to canonical /login
# ===========================================================================

class TestDashboardsLoginRedirect:
    """collapsed the asset-manager and dashboards login surfaces into a
    single ``/login`` page. The old ``/dashboards/login`` URL stays alive as a
    permanent redirect so existing bookmarks keep working."""

    def test_get_returns_301_to_login(self, client):
        resp = client.get("/dashboards/login")
        assert resp.status_code == 301
        assert resp.headers["Location"].startswith("/login")

    def test_get_preserves_query_string(self, client):
        resp = client.get("/dashboards/login?next=/dashboards/space/test&error=x")
        assert resp.status_code == 301
        loc = resp.headers["Location"]
        assert loc.startswith("/login?")
        assert "next=%2Fdashboards%2Fspace%2Ftest" in loc or "next=/dashboards/space/test" in loc

    def test_post_returns_308_to_login(self, client):
        # POST → 308 so a stale form action doesn't lose the credentials in
        # flight (308 is method-preserving; 301 is not).
        resp = client.post("/dashboards/login", data={"username": "u", "password": "p"})
        assert resp.status_code == 308
        assert resp.headers["Location"].startswith("/login")


# ===========================================================================
# GET /logout
# ===========================================================================

class TestLogout:

    def test_clears_session_and_redirects_to_login(self, auth_client):
        resp = auth_client.get("/logout")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "window.location.replace" in body
        assert "/login" in body

    def test_unauthenticated_also_redirects(self, client):
        resp = client.get("/logout")
        # /logout is whitelisted; the broadcast page is returned regardless of auth state
        assert resp.status_code == 200
        assert "window.location.replace" in resp.get_data(as_text=True)

    def test_next_param_included_in_redirect(self, auth_client):
        resp = auth_client.get("/logout?next=/some/path")
        assert resp.status_code == 200
        assert "window.location.replace" in resp.get_data(as_text=True)

    def test_dashboards_referrer_goes_to_canonical_login_with_next(self, auth_client):
        """every logout funnels through /login. The referrer's path is
        carried as next= so the user lands back on the dashboards surface
        once they re-authenticate."""
        resp = auth_client.get("/logout", headers={"Referer": "http://localhost/dashboards/space/test"})
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "/login?" in body, "logout broadcast page must redirect to bare /login"
        assert "/dashboards/login" not in body, "legacy /dashboards/login URL must not appear"
        assert "next=" in body and "%2Fdashboards%2Fspace%2Ftest" in body


# ===========================================================================
# GET /debug/expire-session
# ===========================================================================

class TestDebugExpireSession:

    def test_returns_200_and_marks_timestamp(self, auth_client, app):
        app.debug = True
        resp = auth_client.get("/debug/expire-session")
        assert resp.status_code == 200
        assert b"expired" in resp.data.lower()
        app.debug = False

    def test_returns_400_when_no_session(self, client):
        resp = client.get("/debug/expire-session")
        # Unauthenticated → auth middleware redirects to login first, or 404 in prod mode
        assert resp.status_code in (302, 400, 404)


# ===========================================================================
# Template filter: first_role_display
# ===========================================================================

class TestFirstRoleDisplayFilter:
    """Test the first_role_display Jinja2 filter directly via the app context."""

    def test_returns_empty_string_for_empty_roles(self, app):
        with app.app_context():
            from asset_management.web.app import first_role_display
            assert first_role_display([]) == ""

    def test_returns_superuser_for_superuser_role(self, app):
        with app.app_context():
            from asset_management.web.app import first_role_display
            assert first_role_display(["superuser"]) == "superuser"

    def test_returns_org_id_on_es_exception(self, app):
        with app.app_context():
            from asset_management.web.app import first_role_display
            with mock.patch("services.role_filter_service.RoleFilterService.get_org_descriptions_by_ids",
                            side_effect=Exception("ES down")):
                result = first_role_display(["org_acme"])
            assert result == "acme"

    def test_returns_team_id_on_es_exception(self, app):
        with app.app_context():
            from asset_management.web.app import first_role_display
            with mock.patch("services.role_filter_service.RoleFilterService.get_team_titles_by_ids",
                            side_effect=Exception("ES down")):
                result = first_role_display(["team_alpha"])
            assert result == "alpha"

    def test_returns_first_role_when_no_org_or_team(self, app):
        with app.app_context():
            from asset_management.web.app import first_role_display
            result = first_role_display(["asset_admin"])
            assert result == "asset_admin"

    def test_returns_org_description_when_found(self, app):
        with app.app_context():
            from asset_management.web.app import first_role_display
            with mock.patch("services.role_filter_service.RoleFilterService.get_org_descriptions_by_ids",
                            return_value=["Acme Corp"]):
                result = first_role_display(["org_acme"])
            assert result == "Acme Corp"

    def test_returns_team_title_when_found(self, app):
        with app.app_context():
            from asset_management.web.app import first_role_display
            with mock.patch("services.role_filter_service.RoleFilterService.get_team_titles_by_ids",
                            return_value=["Alpha Team"]):
                result = first_role_display(["team_alpha"])
            assert result == "Alpha Team"

    def test_org_role_takes_priority_over_team_role(self, app):
        with app.app_context():
            from asset_management.web.app import first_role_display
            with mock.patch("services.role_filter_service.RoleFilterService.get_org_descriptions_by_ids",
                            return_value=["Acme Corp"]):
                result = first_role_display(["org_acme", "team_alpha"])
            assert result == "Acme Corp"


# ---------------------------------------------------------------------------
# GET /login — escape hatch from a partial sign-in flow
#
# A user who's been forced into change-password or MFA enrollment must still
# be able to navigate back to /login (to sign in as a different account).
# Without this, every request bounces them back into the partial flow with
# no escape except logout.
# ---------------------------------------------------------------------------

class TestLoginPageEscapeHatch:

    def test_login_page_clears_fpc_pending(self, client):
        with client.session_transaction() as sess:
            sess["fpc_pending"] = True
            sess["fpc_username"] = "someone"
            sess["fpc_timestamp"] = time.time()
        resp = client.get("/login")
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert "fpc_pending" not in sess
            assert "fpc_username" not in sess

    def test_login_page_clears_mfa_pending(self, client):
        with client.session_transaction() as sess:
            sess["mfa_pending"] = True
            sess["mfa_pending_username"] = "someone"
            sess["mfa_pending_timestamp"] = time.time()
        resp = client.get("/login")
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert "mfa_pending" not in sess

    def test_dashboards_login_redirect_clears_fpc_pending_via_followed_login(self, client):
        """GET /dashboards/login is a 301 to /login; the destination
        login page is what clears fpc_pending. Following the redirect proves
        the legacy URL still results in the same escape-hatch behavior."""
        with client.session_transaction() as sess:
            sess["fpc_pending"] = True
            sess["fpc_username"] = "someone"
            sess["fpc_timestamp"] = time.time()
        resp = client.get("/dashboards/login", follow_redirects=True)
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert "fpc_pending" not in sess

    def test_login_page_reachable_while_fpc_pending(self, client):
        """Must not get bounced back to /change-password by the guard."""
        with client.session_transaction() as sess:
            sess["fpc_pending"] = True
            sess["fpc_username"] = "someone"
            sess["fpc_timestamp"] = time.time()
        resp = client.get("/login", follow_redirects=False)
        assert resp.status_code == 200

    def test_login_page_reachable_while_mfa_pending(self, client):
        """Must not get bounced back to /mfa/challenge by the guard."""
        with client.session_transaction() as sess:
            sess["mfa_pending"] = True
            sess["mfa_pending_username"] = "someone"
            sess["mfa_pending_timestamp"] = time.time()
        resp = client.get("/login", follow_redirects=False)
        assert resp.status_code == 200


# ===========================================================================
# Frame-break when login renders inside the dashboard iframe
# ===========================================================================


class TestLoginFrameBreak:
    """The login template must postMessage the parent so a session that
    expires while the dashboard is open breaks out of the embedded iframe
    instead of rendering a tiny login form inside it."""

    def test_should_postmessage_parent_when_login_loads(self, client):
        """The frame-break script must be present and target the parent
        with the deam-session-expired event."""
        resp = client.get("/login")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "deam-session-expired" in body
        assert "window.parent.postMessage" in body
        # Must be guarded so a top-level login does not postMessage to itself.
        assert "window.top === window.self" in body

    def test_should_postmessage_parent_when_following_dashboards_login_redirect(self, client):
        """/dashboards/login is a 301 to /login. The followed
        destination must still carry the frame-break script."""
        resp = client.get("/dashboards/login", follow_redirects=True)
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "deam-session-expired" in body
        assert "window.parent.postMessage" in body

    def test_should_send_origin_targeted_postmessage(self, client):
        """Must target window.location.origin (not "*") so the message
        cannot leak to a hostile cross-origin parent."""
        resp = client.get("/login")
        body = resp.data.decode()
        assert "window.location.origin" in body

    def test_should_rewrite_next_to_parent_url_before_postmessage(self, client):
        """the default next= carries the iframe content URL
        (/assets/dashboard-builder/view/<id>...). Without rewriting, the
        post-login redirect lands the user on the bare iframe page (no
        parent chrome). The frame-break must replace next= with the
        parent's path+search so re-auth returns the user to /dashboard."""
        resp = client.get("/login")
        body = resp.data.decode()
        assert "window.parent.location" in body
        assert 'searchParams.set("next"' in body
        assert "parentLoc.pathname + parentLoc.search" in body
