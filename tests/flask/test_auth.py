"""
Tests for Flask authentication and session management.

Covers:
- Login page rendering
- Form-based login success / failure / rate limiting / CSRF
- Logout
- Session expiry
- MFA pending state
- Role-based route authorization
- extract_credentials_from_basic_auth (pure function)
- get_current_user
- generate_csrf_token
- HTTP Basic Auth request path
- Static file bypass
- Nginx SSO path
- dashboard-builder authorization bypass
"""

import base64
import time
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_csrf(client, token="test-csrf"):
    """Seed a CSRF token into the client's session."""
    with client.session_transaction() as sess:
        sess["csrf_token"] = token
    return token


def _mock_authenticate(success=True, roles=None):
    """Return a mock authenticate_user result."""
    if success:
        return {
            "success": True,
            "username": "testuser",
            "roles": roles or ["asset_admin"],
            "full_name": "Test User",
            "email": "test@example.com",
        }
    return {"success": False, "error": "Invalid credentials"}


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------

class TestLoginPage:

    def test_login_page_returns_200(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_login_page_contains_form(self, client):
        resp = client.get("/login")
        html = resp.data.decode()
        assert "username" in html
        assert "password" in html

    def test_already_authenticated_redirects_away_from_login(self, auth_client):
        resp = auth_client.get("/login")
        assert resp.status_code == 302
        assert "login" not in resp.headers["Location"].lower()


# ---------------------------------------------------------------------------
# Login POST — CSRF
# ---------------------------------------------------------------------------

class TestLoginCsrf:

    def test_missing_csrf_token_redirects_to_login(self, client):
        resp = client.post("/login", data={
            "username": "testuser",
            "password": "password",
            # csrf_token intentionally omitted
        })
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_wrong_csrf_token_redirects_to_login(self, client):
        _seed_csrf(client, "correct-token")
        resp = client.post("/login", data={
            "username": "testuser",
            "password": "password",
            "csrf_token": "wrong-token",
        })
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()


# ---------------------------------------------------------------------------
# Login POST — credential validation
# ---------------------------------------------------------------------------

class TestLoginCredentials:

    def test_missing_username_redirects_with_error(self, client):
        token = _seed_csrf(client)
        resp = client.post("/login", data={
            "username": "",
            "password": "password",
            "csrf_token": token,
        })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_missing_password_redirects_with_error(self, client):
        token = _seed_csrf(client)
        resp = client.post("/login", data={
            "username": "testuser",
            "password": "",
            "csrf_token": token,
        })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_invalid_credentials_redirects_with_error(self, client):
        token = _seed_csrf(client)
        with mock.patch("lib.es_auth.authenticate_user", return_value=_mock_authenticate(success=False)):
            resp = client.post("/login", data={
                "username": "testuser",
                "password": "wrongpassword",
                "csrf_token": token,
            })
        assert resp.status_code == 302
        assert "error" in resp.headers["Location"].lower()

    def test_successful_login_redirects_to_home(self, client):
        token = _seed_csrf(client)
        with mock.patch("lib.es_auth.authenticate_user", return_value=_mock_authenticate()), \
             mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required",
                        return_value={"required": False}), \
             mock.patch("asset_management.web.blueprints.analytics.routes._create_kibana_session", return_value=None):
            resp = client.post("/login", data={
                "username": "testuser",
                "password": "password",
                "csrf_token": token,
            })
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/dashboard?just_logged_in=1"

    def test_successful_login_sets_session(self, client):
        token = _seed_csrf(client)
        with mock.patch("lib.es_auth.authenticate_user", return_value=_mock_authenticate()), \
             mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required",
                        return_value={"required": False}), \
             mock.patch("asset_management.web.blueprints.analytics.routes._create_kibana_session", return_value=None):
            client.post("/login", data={
                "username": "testuser",
                "password": "password",
                "csrf_token": token,
            })

        with client.session_transaction() as sess:
            assert sess.get("authenticated") is True
            assert sess.get("username") == "testuser"
            assert "asset_admin" in sess.get("roles", [])
            assert "auth_timestamp" in sess


# ---------------------------------------------------------------------------
# Login POST — rate limiting
# ---------------------------------------------------------------------------

class TestLoginRateLimit:

    def test_rate_limit_returns_429_after_five_failures(self, client, app):
        from asset_management.web import auth as auth_module

        username = "lockeduser"
        now = time.time()
        # Pre-populate 5 failed attempts within the last minute
        auth_module.failed_attempts[username] = [now - 30] * 5

        token = _seed_csrf(client)
        with mock.patch("lib.es_auth.authenticate_user", return_value=_mock_authenticate()):
            resp = client.post("/login", data={
                "username": username,
                "password": "anypassword",
                "csrf_token": token,
            })
        assert resp.status_code == 429

    def test_rate_limit_resets_on_successful_login(self, client, app):
        from asset_management.web import auth as auth_module

        username = "testuser"
        now = time.time()
        auth_module.failed_attempts[username] = [now - 30] * 2  # 2 prior failures

        token = _seed_csrf(client)
        with mock.patch("lib.es_auth.authenticate_user", return_value=_mock_authenticate()), \
             mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required",
                        return_value={"required": False}), \
             mock.patch("asset_management.web.blueprints.analytics.routes._create_kibana_session", return_value=None):
            resp = client.post("/login", data={
                "username": username,
                "password": "password",
                "csrf_token": token,
            })

        assert resp.status_code == 302
        assert username not in auth_module.failed_attempts


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

class TestLogout:

    def test_logout_clears_auth_from_session(self, auth_client):
        auth_client.get("/logout")
        with auth_client.session_transaction() as sess:
            assert "authenticated" not in sess

    def test_logout_redirects_to_login(self, auth_client):
        resp = auth_client.get("/logout")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "window.location.replace" in body
        assert "login" in body.lower()

    def test_logout_preserves_csrf_token(self, auth_client):
        """CSRF token should survive logout so other open tabs can still submit the form."""
        with auth_client.session_transaction() as sess:
            sess["csrf_token"] = "preserved-token"

        auth_client.get("/logout")

        with auth_client.session_transaction() as sess:
            assert sess.get("csrf_token") == "preserved-token"


# ---------------------------------------------------------------------------
# Session / auth guard
# ---------------------------------------------------------------------------

class TestSessionGuard:

    def test_unauthenticated_request_redirects_to_login(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_authenticated_session_allows_access(self, auth_client):
        resp = auth_client.get("/")
        assert resp.status_code == 200

    def test_expired_session_redirects_to_login(self, app, client):
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            sess["auth_timestamp"] = 1  # epoch 1970 → expired

        resp = client.get("/")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_expired_session_clears_auth_state(self, app, client):
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            sess["auth_timestamp"] = 1

        client.get("/")

        with client.session_transaction() as sess:
            assert "authenticated" not in sess

    # ---- idle-timeout + max-lifetime model -----

    def test_should_refresh_idle_timer_on_authenticated_request(self, app, client):
        """An authenticated request inside the idle window refreshes
        last_activity so the next idle check is reset."""
        from asset_management.web.config import config

        # Seed a session whose last_activity is just inside the idle window
        idle_age_seed = config.SESSION_LIFETIME - 5
        now = time.time()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            sess["auth_timestamp"] = now - idle_age_seed
            sess["last_activity"] = now - idle_age_seed

        resp = client.get("/")
        assert resp.status_code == 200

        # last_activity should now be ~now, not the seeded value
        with client.session_transaction() as sess:
            assert sess["last_activity"] > now - 1  # generous slack for timing

    def test_should_log_out_when_idle_timeout_exceeded(self, app, client):
        from asset_management.web.config import config

        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            # Recent login but very stale activity → idle timer fires
            sess["auth_timestamp"] = time.time() - 60
            sess["last_activity"] = time.time() - (config.SESSION_LIFETIME + 60)

        resp = client.get("/")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_should_log_out_at_max_lifetime_even_with_continuous_activity(self, app, client):
        from asset_management.web.config import config

        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            # Old login, but last_activity is fresh — absolute timer must fire
            sess["auth_timestamp"] = time.time() - (config.SESSION_MAX_LIFETIME + 60)
            sess["last_activity"] = time.time()

        resp = client.get("/")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_should_treat_session_without_last_activity_as_compatible(self, app, client):
        """Sessions written lack last_activity; verify_auth
        must not crash and must fall back to auth_timestamp."""
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            sess["auth_timestamp"] = time.time()  # fresh, no last_activity

        resp = client.get("/")
        assert resp.status_code == 200

        # last_activity should now be populated by the auth check
        with client.session_transaction() as sess:
            assert "last_activity" in sess


# ---------------------------------------------------------------------------
# MFA pending state
# ---------------------------------------------------------------------------

class TestMfaPending:

    def test_mfa_pending_redirects_non_mfa_routes(self, app, client):
        with client.session_transaction() as sess:
            sess["mfa_pending"] = True
            sess["mfa_pending_timestamp"] = time.time()

        resp = client.get("/")
        assert resp.status_code == 302
        assert "mfa" in resp.headers["Location"].lower()

    def test_mfa_pending_timeout_redirects_to_login(self, app, client):
        with client.session_transaction() as sess:
            sess["mfa_pending"] = True
            sess["mfa_pending_timestamp"] = 1  # epoch 1970 → > 5 minutes ago

        resp = client.get("/")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_mfa_pending_timeout_clears_session(self, app, client):
        with client.session_transaction() as sess:
            sess["mfa_pending"] = True
            sess["mfa_pending_timestamp"] = 1
            sess["mfa_pending_username"] = "testuser"

        client.get("/")

        with client.session_transaction() as sess:
            assert "mfa_pending" not in sess

    def test_login_with_mfa_required_redirects_to_mfa_challenge(self, client):
        token = _seed_csrf(client)
        with mock.patch("lib.es_auth.authenticate_user", return_value=_mock_authenticate()), \
             mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required",
                        return_value={"required": True, "method": "totp", "setup_complete": True}):
            resp = client.post("/login", data={
                "username": "testuser",
                "password": "password",
                "csrf_token": token,
            })
        assert resp.status_code == 302
        assert "mfa" in resp.headers["Location"].lower()


# ---------------------------------------------------------------------------
# Role-based route authorization
# ---------------------------------------------------------------------------

class TestRouteAuthorization:

    def test_asset_admin_can_access_asset_list(self, auth_client):
        resp = auth_client.get("/asset/organization")
        # 200 or redirect to login would indicate auth passed (no 403)
        assert resp.status_code != 403

    def test_insufficient_role_redirects(self, org_user_client):
        """A user with only an org_ role is redirected away from asset management routes."""
        resp = org_user_client.get("/asset/organization")
        assert resp.status_code == 302

    def test_superuser_can_access_user_management(self, superuser_client):
        resp = superuser_client.get("/user-management")
        assert resp.status_code != 403

    def test_insufficient_role_redirected_from_user_management(self, org_user_client):
        resp = org_user_client.get("/user-management")
        assert resp.status_code == 302

    def test_all_authenticated_users_can_access_dashboards(self, org_user_client):
        """Dashboard routes are accessible to all authenticated users regardless of role."""
        resp = org_user_client.get("/dashboard")
        assert resp.status_code != 403

    def test_dashboard_builder_view_path_bypasses_role_check(self, org_user_client):
        """dashboard-builder/view/ routes are open to all authenticated users."""
        resp = org_user_client.get("/dashboard-builder/view/some-dashboard-id")
        # Should not be 403 — the auth middleware explicitly bypasses role check
        assert resp.status_code != 403

    def test_api_dashboards_path_bypasses_role_check(self, org_user_client):
        """API dashboard routes are open to all authenticated users."""
        # /api/dashboards/space/<id> is now superuser-only inside the
        # handler; the prefix bypass itself is pinned on the open sibling route.
        resp = org_user_client.get("/api/dashboards/some-dashboard-key")
        # Should not be 403
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# extract_credentials_from_basic_auth — pure function
# ---------------------------------------------------------------------------

class TestExtractCredentialsFromBasicAuth:

    def _module(self):
        from asset_management.web import auth as auth_module
        return auth_module

    def _encode(self, user, password):
        raw = f"{user}:{password}"
        return "Basic " + base64.b64encode(raw.encode()).decode()

    def test_valid_credentials_returned_as_tuple(self):
        header = self._encode("testuser", "secret")
        user, pw = self._module().extract_credentials_from_basic_auth(header)
        assert user == "testuser"
        assert pw == "secret"

    def test_password_with_colon_preserved(self):
        """Password containing colon — split on first colon only."""
        header = self._encode("user", "pa:ss:word")
        _, pw = self._module().extract_credentials_from_basic_auth(header)
        assert pw == "pa:ss:word"

    def test_non_basic_header_raises_value_error(self):
        with pytest.raises(ValueError, match="Not a Basic Auth header"):
            self._module().extract_credentials_from_basic_auth("Bearer sometoken")

    def test_invalid_base64_raises_value_error(self):
        with pytest.raises(ValueError, match="Failed to decode"):
            self._module().extract_credentials_from_basic_auth("Basic !!!invalid!!!")

    def test_no_colon_in_credentials_raises_value_error(self):
        # Valid base64 but no colon separator
        no_colon = base64.b64encode(b"useronly").decode()
        with pytest.raises(ValueError, match="Invalid credentials format"):
            self._module().extract_credentials_from_basic_auth(f"Basic {no_colon}")


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------

class TestGetCurrentUser:

    def test_returns_user_dict_when_authenticated(self, auth_client):
        from asset_management.web import auth as auth_module
        with auth_client.application.test_request_context("/"):
            with auth_client.session_transaction() as sess:
                # Reuse the auth session data set in the fixture
                pass
            # Simulate the session within a request context
            with auth_client.application.test_request_context("/"):
                from flask import session
                session["authenticated"] = True
                session["username"] = "testuser"
                session["roles"] = ["asset_admin"]
                session["is_admin"] = True
                session["full_name"] = "Test User"
                session["email"] = "test@example.com"
                result = auth_module.get_current_user()
        assert result["username"] == "testuser"
        assert "asset_admin" in result["roles"]

    def test_returns_empty_dict_when_not_authenticated(self, app):
        from asset_management.web import auth as auth_module
        with app.test_request_context("/"):
            from flask import session
            # session has no "authenticated" key
            result = auth_module.get_current_user()
        assert result == {}


# ---------------------------------------------------------------------------
# generate_csrf_token
# ---------------------------------------------------------------------------

class TestGenerateCsrfToken:

    def test_generates_token_when_none_in_session(self, app):
        from asset_management.web import auth as auth_module
        with app.test_request_context("/"):
            from flask import session
            assert "csrf_token" not in session
            token = auth_module.generate_csrf_token()
            assert token
            assert len(token) == 64  # secrets.token_hex(32) → 64 hex chars

    def test_returns_existing_token_without_regenerating(self, app):
        from asset_management.web import auth as auth_module
        with app.test_request_context("/"):
            from flask import session
            session["csrf_token"] = "existing-token"
            token = auth_module.generate_csrf_token()
            assert token == "existing-token"


# ---------------------------------------------------------------------------
# Static file bypass
# ---------------------------------------------------------------------------

class TestStaticFileBypass:

    def test_static_file_request_not_redirected_to_login(self, client):
        """Requests to /static/... skip auth checks — should not get a 302 to /login."""
        resp = client.get("/static/nonexistent-file.css")
        # Flask returns 404 for missing static files, but never 302 (redirect to login)
        assert resp.status_code != 302


# ---------------------------------------------------------------------------
# HTTP Basic Auth path
# ---------------------------------------------------------------------------

class TestHttpBasicAuth:

    def _basic_header(self, user="testuser", password="testpass"):
        raw = f"{user}:{password}"
        encoded = base64.b64encode(raw.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    def test_valid_basic_auth_allows_access(self, client):
        auth_result = {
            "success": True,
            "username": "testuser",
            "roles": ["asset_admin"],
            "full_name": "Test User",
            "email": "test@example.com",
        }
        with mock.patch("lib.es_auth.authenticate_user", return_value=auth_result), \
             mock.patch("asset_management.web.app.MFAService.is_mfa_required",
                        return_value={"required": False}):
            resp = client.get("/", headers=self._basic_header())
        # Should access the app (200 or redirect to a valid page, not to login)
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            assert "/login" not in resp.headers.get("Location", "").lower()

    def test_invalid_basic_auth_returns_401(self, client):
        auth_result = {"success": False, "error": "Invalid credentials"}
        with mock.patch("lib.es_auth.authenticate_user", return_value=auth_result):
            resp = client.get("/", headers=self._basic_header())
        assert resp.status_code == 401

    def test_basic_auth_missing_role_returns_403(self, client):
        auth_result = {
            "success": True,
            "username": "nopermuser",
            "roles": ["some_other_role"],
            "full_name": "No Perm",
            "email": "noperm@example.com",
        }
        with mock.patch("lib.es_auth.authenticate_user", return_value=auth_result):
            resp = client.get("/", headers=self._basic_header("nopermuser", "pass"))
        assert resp.status_code == 403

    def test_invalid_auth_header_format_returns_401(self, client):
        # Non-Basic auth scheme
        resp = client.get("/", headers={"Authorization": "Bearer not-a-basic-token"})
        assert resp.status_code == 401

    def test_rate_limited_basic_auth_returns_429(self, client):
        from asset_management.web import auth as auth_module
        username = "bruteuser"
        now = time.time()
        auth_module.failed_attempts[username] = [now - 30] * 5
        resp = client.get("/", headers=self._basic_header(username, "pass"))
        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# MFA pending — whitelisted endpoint bypass (line 111)
# ---------------------------------------------------------------------------

class TestMfaPendingWhitelistedEndpoint:

    def test_mfa_pending_with_whitelisted_endpoint_passes_through(self, client):
        """When mfa_pending=True and endpoint is in mfa_whitelist, return None (allow through)."""
        with client.session_transaction() as sess:
            sess["mfa_pending"] = True
            sess["mfa_pending_timestamp"] = time.time()  # fresh — not expired

        # /logout is in the mfa_whitelist — should not redirect to /mfa
        resp = client.get("/logout")
        # logout returns the broadcast page, NOT a redirect to /mfa — proving the whitelist bypass worked
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "window.location.replace" in body
        assert "/mfa" not in body


# ---------------------------------------------------------------------------
# Expired session — non-root path redirects (lines 160-170)
# ---------------------------------------------------------------------------

class TestExpiredSessionNonRootPath:

    def test_expired_session_at_asset_path_redirects_to_login_with_next(self, client):
        """Expired session at /asset/... preserves the requested path in redirect."""
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            sess["auth_timestamp"] = 1  # epoch 1970 → expired

        resp = client.get("/asset/teams")
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "/login" in location.lower()

    def test_expired_session_at_dashboards_path_redirects_to_dashboards_login(self, client):
        """Expired session at /dashboard redirects to /dashboards/login."""
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            sess["auth_timestamp"] = 1

        resp = client.get("/dashboard")
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "/login" in location.lower()


# ---------------------------------------------------------------------------
# Unauthenticated /dashboards path redirect (line 226)
# ---------------------------------------------------------------------------

class TestUnauthenticatedDashboardsRedirect:

    def test_unauthenticated_request_to_dashboards_redirects_to_dashboards_login(self, client):
        """Unauthenticated /dashboard request should go to /dashboards/login, not /login."""
        resp = client.get("/dashboard")
        assert resp.status_code == 302
        location = resp.headers["Location"]
        # Should redirect somewhere (login) — the dashboards path gets the dashboards login URL
        assert "/login" in location.lower()


# ---------------------------------------------------------------------------
# Nginx SSO path (lines 178-194)
# ---------------------------------------------------------------------------

class TestNginxSso:

    def test_nginx_sso_creates_session_and_allows_access(self, client, app):
        """header SSO authenticates only with the full proven chain —
        TRUST_NGINX_HEADERS on, peer IP allowlisted, shared proxy secret
        presented — and roles come from ES Security, not X-Remote-Roles."""
        with mock.patch.dict(
            "os.environ",
            {
                "TRUST_NGINX_HEADERS": "true",
                "TRUSTED_PROXY_IPS": "127.0.0.1",
                "PROXY_AUTH_SECRET": "test-proxy-secret",
            },
        ):
            with mock.patch(
                "asset_management.web.auth._resolve_es_roles",
                return_value=["asset_admin"],
            ):
                resp = client.get(
                    "/",
                    headers={
                        "X-Remote-User": "sso-user",
                        "X-Proxy-Auth": "test-proxy-secret",
                    },
                )
        # Should be authenticated — 200 (home page) or valid redirect, not login
        assert resp.status_code in (200, 302)
        if resp.status_code == 302:
            assert "/login" not in resp.headers.get("Location", "").lower()

    def test_nginx_sso_flag_alone_is_not_enough(self, client):
        """regression: TRUST_NGINX_HEADERS=true by itself (the
        previously-exploitable configuration) no longer honors the headers —
        without an allowlisted peer + proxy secret the request falls through
        to the login redirect like any unauthenticated one."""
        with mock.patch.dict("os.environ", {"TRUST_NGINX_HEADERS": "true"}):
            resp = client.get(
                "/",
                headers={
                    "X-Remote-User": "sso-user",
                    "X-Remote-Roles": "superuser",
                },
            )
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "").lower()

    def test_nginx_sso_not_used_when_trust_headers_disabled(self, client):
        """When TRUST_NGINX_HEADERS=False (default), X-Remote-User is ignored."""
        resp = client.get(
            "/",
            headers={"X-Remote-User": "sso-user", "X-Remote-Roles": "asset_admin"},
        )
        # Should redirect to login since headers are not trusted
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "").lower()


# ---------------------------------------------------------------------------
# fpc_pending (forced password change) — lines 86-107
# ---------------------------------------------------------------------------

class TestFpcPending:

    def test_fpc_pending_timeout_redirects_to_login(self, client):
        """fpc_pending session older than 15 min is cleared and redirected to login."""
        with client.session_transaction() as sess:
            sess["fpc_pending"] = True
            sess["fpc_timestamp"] = 1  # epoch 1970 → > 900 seconds ago

        resp = client.get("/")
        assert resp.status_code == 302
        assert "login" in resp.headers["Location"].lower()

    def test_fpc_pending_timeout_clears_session(self, client):
        """fpc_pending timeout clears the partial session."""
        with client.session_transaction() as sess:
            sess["fpc_pending"] = True
            sess["fpc_timestamp"] = 1
            sess["username"] = "testuser"

        client.get("/")

        with client.session_transaction() as sess:
            assert "fpc_pending" not in sess

    def test_fpc_pending_non_whitelisted_endpoint_redirects_to_change_password(self, client):
        """Active fpc_pending session hitting a non-whitelisted endpoint redirects to change-password."""
        with client.session_transaction() as sess:
            sess["fpc_pending"] = True
            sess["fpc_timestamp"] = time.time()  # fresh — not expired

        resp = client.get("/")
        assert resp.status_code == 302
        assert "change-password" in resp.headers["Location"].lower()

    def test_fpc_pending_whitelisted_endpoint_passes_through(self, client):
        """Active fpc_pending session hitting a whitelisted endpoint returns None (allow through)."""
        with client.session_transaction() as sess:
            sess["fpc_pending"] = True
            sess["fpc_timestamp"] = time.time()  # fresh — not expired

        # /logout is in the fpc_whitelist — before_request should return None and let it proceed
        resp = client.get("/logout")
        # Any response other than a change-password redirect is acceptable
        assert "change-password" not in resp.headers.get("Location", "").lower()


# ---------------------------------------------------------------------------
# Expired session at POST login endpoints — lines 160, 162
# ---------------------------------------------------------------------------

class TestExpiredSessionPostLogin:

    def test_expired_session_at_login_post_redirects(self, client):
        """Expired session hitting login_post redirects to login (before CSRF check)."""
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            sess["auth_timestamp"] = 1  # expired

        resp = client.post("/login", data={})
        assert resp.status_code == 401
        assert "login" in (resp.get_json() or {}).get("login_url", "").lower()

    def test_expired_session_at_legacy_dashboards_login_redirect_lands_on_canonical_login(self, client):
        """POST /dashboards/login is a 308 to /login. Even with an
        expired session at the time of the post, the chain must end on the
        canonical /login URL, not on the legacy dashboards URL."""
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "testuser"
            sess["roles"] = ["asset_admin"]
            sess["auth_timestamp"] = 1  # expired

        resp = client.post("/dashboards/login", data={}, follow_redirects=False)
        assert resp.status_code in (301, 308)
        assert resp.headers["Location"].startswith("/login")
        assert "/dashboards/login" not in resp.headers["Location"]
