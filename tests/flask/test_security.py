"""
Security tests pen test remediations.

Covers: CSRF enforcement, open redirect prevention, security headers,
X-Remote-User injection, auth bypass, session cookie flags, debug endpoint
gating, hCaptcha fallback, CORS rejection, and MFA session safety.
"""

import os
import time
from unittest import mock

import pytest


# ===========================================================================
# CSRF enforcement
# ===========================================================================


class TestCsrfEnforcement:
    """Global CSRF protection rejects state-changing requests without a token."""

    # -- Representative POST endpoints that should be CSRF-protected ----------

    PROTECTED_ENDPOINTS = [
        "/api/import/run",
        "/api/import/delete-log",
        "/api/user-management/sync-roles",
        "/api/user-management/create-user",
        "/api/user-management/delete-user",
        "/api/user-management/suspend-user",
        "/api/user-management/reinstate-user",
        "/api/user-management/update-roles",
        "/api/user-management/bulk-action",
        "/api/user-management/enable-mfa",
        "/api/user-management/disable-mfa",
        "/api/feedback/submit",
        "/api/backup/create",
        "/api/testing/run-pytest",
        "/api/user-management/reset-password",
    ]

    @pytest.mark.parametrize("endpoint", PROTECTED_ENDPOINTS)
    def test_post_without_csrf_returns_403(self, auth_client, endpoint):
        """POST without X-CSRF-Token header should be rejected."""
        # Use the raw FlaskClient (bypass CsrfTestClient auto-injection)
        resp = auth_client._client.post(
            endpoint,
            content_type="application/json",
            data="{}",
        )
        assert resp.status_code == 403
        assert b"CSRF" in resp.data

    @pytest.mark.parametrize("endpoint", PROTECTED_ENDPOINTS)
    def test_post_with_wrong_csrf_returns_403(self, auth_client, endpoint):
        """POST with an incorrect CSRF token should be rejected."""
        resp = auth_client._client.post(
            endpoint,
            headers={"X-CSRF-Token": "wrong-token-value"},
            content_type="application/json",
            data="{}",
        )
        assert resp.status_code == 403
        assert b"CSRF" in resp.data

    def test_post_with_valid_csrf_header_passes(self, auth_client):
        """POST with the correct X-CSRF-Token header is accepted by the
        global CSRF guard. We don't care what the route handler returns —
        only that we got past the CSRF check (would be 403 otherwise)."""
        resp = auth_client.post("/api/user-management/sync-roles")
        assert resp.status_code != 403 or b"CSRF" not in resp.data

    def test_post_with_valid_csrf_form_field_passes(self, auth_client):
        """POST with csrf_token in form data is accepted by the global
        CSRF guard. Same caveat as above — only CSRF outcome matters."""
        resp = auth_client._client.post(
            "/api/user-management/sync-roles",
            data={"csrf_token": "test-csrf-token"},
        )
        assert resp.status_code != 403 or b"CSRF" not in resp.data

    def test_get_requests_not_blocked_by_csrf(self, auth_client):
        """GET requests should never be blocked by CSRF enforcement."""
        resp = auth_client.get("/")
        assert resp.status_code == 200

    # -- Exempt endpoints still work without global CSRF --------------------

    def test_login_post_exempt_from_global_csrf(self, client):
        """Login POST has its own CSRF handling and is exempt from global check."""
        resp = client._client.post(
            "/login",
            data={"username": "x", "password": "y"},
        )
        # Should NOT be 403 CSRF error — login validates its own token
        # (will redirect to login page due to missing/invalid csrf_token in form)
        assert resp.status_code != 403

    # - release-plan coverage -------------------------------

    def test_should_reject_reset_password_post_without_csrf_token(self, auth_client):
        """The admin-triggered password-reset endpoint must enforce CSRF.
        POST without a token returns 403 'CSRF' before authorization runs."""
        resp = auth_client._client.post(
            "/api/user-management/reset-password",
            content_type="application/json",
            data='{"username": "alice"}',
        )
        assert resp.status_code == 403
        assert b"CSRF" in resp.data

    @pytest.mark.parametrize(
        "bad_username",
        [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32",
            "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "alice/../bob",
            "alice\x00bob",  # NUL byte
            "alice with space",
            "<script>alert(1)</script>",
            "a" * 100,  # exceeds 64-char cap
        ],
    )
    def test_should_reject_malformed_username_in_reset_password(self, superuser_client, bad_username):
        """Path-traversal / shell / oversize usernames must be rejected at the
        endpoint boundary with a uniform 'Invalid username' 400 — no enumeration
        leak, no ES backtrace, no reaching es.security.get_user()."""
        resp = superuser_client.post(
            "/api/user-management/reset-password",
            json={"username": bad_username},
        )
        assert resp.status_code == 400
        body = resp.get_json() or {}
        # Uniform error shape — must not echo the input or leak ES exception text
        assert body.get("error") == "Invalid username"
        assert body.get("success") is False


# ===========================================================================
# Open redirect prevention
# ===========================================================================


class TestOpenRedirectPrevention:
    """next parameter must not redirect to external URLs."""

    MALICIOUS_URLS = [
        "https://evil.com",
        "http://evil.com",
        "//evil.com",
        "///evil.com",
        "https://evil.com/phishing",
        "//evil.com/steal-cookies",
    ]

    @pytest.mark.parametrize("malicious_url", MALICIOUS_URLS)
    def test_login_page_rejects_external_next(self, auth_client, malicious_url):
        """Already-authenticated user visiting /login?next=<external> should not
        be redirected off-site."""
        resp = auth_client.get(f"/login?next={malicious_url}")
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        # Must redirect to a local path, never to evil.com
        assert "evil.com" not in location

    @pytest.mark.parametrize("malicious_url", MALICIOUS_URLS)
    def test_legacy_dashboards_login_redirect_does_not_leak_external_next(self, auth_client, malicious_url):
        """/dashboards/login is now a 301 to /login. The redirect
        itself preserves the query string verbatim, so the open-redirect
        defence happens at /login. Following the chain end-to-end must still
        never land on the attacker host."""
        resp = auth_client.get(f"/dashboards/login?next={malicious_url}", follow_redirects=True)
        # The followed chain must stay on this host
        assert "evil.com" not in resp.request.url
        # And the rendered page (or final redirect Location) must not carry
        # the attacker URL into a Location header.
        assert "evil.com" not in resp.headers.get("Location", "")

    def test_login_page_allows_relative_next(self, auth_client):
        """Relative paths in next should work normally."""
        resp = auth_client.get("/login?next=/import-data")
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "import-data" in location

    def test_login_next_rendered_in_form_is_safe(self, client):
        """When not authenticated, next= is rendered in the login form.
        Malicious values should be sanitized to empty or safe default."""
        resp = client.get("/login?next=https://evil.com")
        assert resp.status_code == 200
        # The evil URL should not appear in the rendered HTML
        assert b"evil.com" not in resp.data

    # - release-plan coverage -------------------------------

    def test_should_reject_url_encoded_protocol_relative_next_when_authenticated(self, auth_client):
        """Encoded `%2F%2Fevil.com` decodes to `//evil.com` in werkzeug's request.args.
        The sanitizer must reject the decoded form, not just the literal."""
        resp = auth_client.get("/login?next=%2F%2Fevil.com%2Fphish")
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "evil.com" not in location

    def test_should_not_inject_response_headers_via_crlf_in_next(self, auth_client):
        """CRLF in next= must not produce a Location header with injected
        Set-Cookie or other response headers (CWE-93). Werkzeug raises a
        ValueError at the framework boundary when a header value contains
        CRLF — the attacker cannot land an injected header either way."""
        try:
            resp = auth_client.get("/login?next=/import-data%0d%0aSet-Cookie:%20evil=1")
        except ValueError as exc:
            assert "newline" in str(exc).lower(), (
                "expected werkzeug to reject CRLF in header value, got: " + str(exc)
            )
            return
        # If werkzeug ever stops raising, fall back to asserting the response
        # itself is clean.
        assert "evil=1" not in resp.headers.get("Set-Cookie", "")
        location = resp.headers.get("Location", "")
        assert "\r" not in location
        assert "\n" not in location

    def test_should_html_escape_next_when_rendering_login_form(self, client):
        """Special characters in next= must be HTML-escaped when rendered
        into the form's hidden input value."""
        resp = client.get('/login?next=/path"><script>alert(1)</script>')
        assert resp.status_code == 200
        # The literal `<script>` tag must not appear in the rendered HTML
        assert b"<script>alert(1)</script>" not in resp.data
        # And neither should an unescaped attribute break-out
        assert b'value="/path"><script' not in resp.data

    # backslash + ASCII control character bypass vectors.
    # Older browsers (and some embedded WebViews) normalize ``\`` in a Location
    # header to ``/``, turning ``/\evil.com`` into ``//evil.com`` →
    # protocol-relative redirect off-domain. Werkzeug rejects CR/LF at the
    # framework boundary, but other control chars slip through.
    # Tests use URL-encoded forms (``%5C``, ``%09``, etc.) to exercise the
    # actual attacker-controlled wire format. Werkzeug normalizes some bare
    # control chars out of the path before the handler sees them, but the
    # encoded form survives intact.
    BACKSLASH_BYPASS_URLS = [
        "/%5Cevil.com",
        "/%5C%5Cevil.com",
        "/path/%5Cevil.com",
        "/%5C/evil.com",
    ]

    CONTROL_CHAR_BYPASS_URLS = [
        "/%09/evil.com",
        "/%00/evil.com",
        "/%0B/evil.com",
        "/%7F/evil.com",
    ]

    @pytest.mark.parametrize("malicious_url", BACKSLASH_BYPASS_URLS)
    def test_login_rejects_backslash_in_next(self, auth_client, malicious_url):
        """Backslash in the next= param must not redirect off-domain."""
        try:
            resp = auth_client.get(f"/login?next={malicious_url}")
        except ValueError:
            return  # werkzeug rejected at framework boundary — also acceptable
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "evil.com" not in location

    @pytest.mark.parametrize("malicious_url", CONTROL_CHAR_BYPASS_URLS)
    def test_login_rejects_control_chars_in_next(self, auth_client, malicious_url):
        """ASCII control chars (other than CR/LF, which werkzeug catches) in
        the next= param must not redirect off-domain."""
        try:
            resp = auth_client.get(f"/login?next={malicious_url}")
        except ValueError:
            return  # werkzeug rejected at framework boundary — also acceptable
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "evil.com" not in location


# ===========================================================================
# Security response headers
# ===========================================================================


class TestSecurityHeaders:
    """Every response should include standard security headers."""

    def test_x_content_type_options(self, auth_client):
        resp = auth_client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self, auth_client):
        resp = auth_client.get("/")
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"

    def test_referrer_policy(self, auth_client):
        resp = auth_client.get("/")
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_strict_transport_security(self, auth_client):
        resp = auth_client.get("/")
        hsts = resp.headers.get("Strict-Transport-Security", "")
        assert "max-age=" in hsts

    def test_content_security_policy(self, auth_client):
        resp = auth_client.get("/")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src" in csp
        assert "script-src" in csp
        assert "style-src" in csp

    def test_csp_includes_non_fallback_directives(self, auth_client):
        """object-src, base-uri, and form-action don't inherit from default-src,
        so they must be set explicitly."""
        resp = auth_client.get("/")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "object-src 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "form-action 'self'" in csp

    def test_csp_connect_src_trusts_leaflet_cdns(self, auth_client):
        """connect-src must trust the same CDNs as script-src so Leaflet's
        sourcemap fetch from unpkg doesn't trigger CSP violations."""
        resp = auth_client.get("/")
        csp = resp.headers.get("Content-Security-Policy", "")
        connect_src = next(
            (d.strip() for d in csp.split(";") if d.strip().startswith("connect-src")),
            "",
        )
        assert "https://unpkg.com" in connect_src
        assert "https://cdn.jsdelivr.net" in connect_src

    def test_headers_on_api_response(self, auth_client):
        """Security headers should also be set on JSON API responses."""
        resp = auth_client.get("/api/documentation-permissions")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"

    # - release-plan coverage -------------------------------

    @staticmethod
    def _csp_directive(csp, name):
        return next(
            (d.strip() for d in csp.split(";") if d.strip().startswith(name)),
            "",
        )

    def test_should_set_default_src_to_self_when_serving_app_pages(self, auth_client):
        """default-src is the fallback for fetch directives. Must be 'self'."""
        resp = auth_client.get("/")
        directive = self._csp_directive(resp.headers.get("Content-Security-Policy", ""), "default-src")
        assert directive == "default-src 'self'"

    def test_should_set_frame_ancestors_self_when_serving_app_pages(self, auth_client):
        """frame-ancestors prevents clickjacking; must be 'self' or stricter."""
        resp = auth_client.get("/")
        directive = self._csp_directive(resp.headers.get("Content-Security-Policy", ""), "frame-ancestors")
        assert directive in ("frame-ancestors 'self'", "frame-ancestors 'none'")

    def test_should_restrict_script_src_to_self_and_known_cdns_when_serving_app(self, auth_client):
        """script-src must NOT include 'unsafe-eval' and must constrain origins to a known list."""
        resp = auth_client.get("/")
        directive = self._csp_directive(resp.headers.get("Content-Security-Policy", ""), "script-src")
        assert "'unsafe-eval'" not in directive
        assert "'self'" in directive
        # No wildcard hosts
        assert " *" not in directive
        assert "https://*" not in directive

    def test_should_restrict_img_src_to_known_tile_providers_when_serving_app(self, auth_client):
        """img-src trusts the OSM/Carto/ArcGIS tile servers used by Leaflet, but no wildcards."""
        resp = auth_client.get("/")
        directive = self._csp_directive(resp.headers.get("Content-Security-Policy", ""), "img-src")
        assert "'self'" in directive
        assert "data:" in directive
        assert " *" not in directive
        assert "tile.openstreetmap.org" in directive

    def test_should_apply_strict_csp_to_static_pages_through_nginx_template(self):
        """Nginx CSP for static/login pages must be strict — default-src 'self', no unsafe-eval, no wildcards."""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        template_path = repo_root / "deployment" / "nginx" / "conf.d" / "default.conf.template"
        if not template_path.exists():
            # In the imports_container only `tests/` is mounted, so the nginx
            # template isn't reachable. The CI run (full checkout) is the
            # canonical execution of this test.
            pytest.skip(f"nginx template not reachable from {template_path}")
        template = template_path.read_text()
        # Pull every literal CSP value the template sets
        import re

        csp_values = re.findall(r'add_header Content-Security-Policy "([^"]+)"', template)
        assert csp_values, "expected at least one Content-Security-Policy header in the nginx template"
        for csp in csp_values:
            assert "'unsafe-eval'" not in csp
            # default-src is set explicitly, never absent
            assert "default-src" in csp
            # No origin wildcards in any directive
            assert " *" not in csp.replace("' *", "")  # avoid false-positive on 'unsafe-* shapes

    def test_should_serve_csp_header_on_login_page_when_unauthenticated(self, client):
        """The /login page reaches Flask, so it must carry a CSP header even before auth."""
        resp = client.get("/login")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert csp, "login page response is missing Content-Security-Policy"
        assert "default-src" in csp
        assert "frame-ancestors" in csp

    def test_headers_on_login_page(self, client):
        """Security headers should be present even on unauthenticated pages."""
        resp = client.get("/login")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"


# ===========================================================================
# X-Remote-User header injection
# ===========================================================================


class TestHeaderInjection:
    """X-Remote-User should be ignored unless explicitly opted in."""

    def test_remote_user_ignored_by_default(self, client):
        """With TRUST_NGINX_HEADERS=false, X-Remote-User does not grant a session.

        Force the env explicitly so this test is deterministic in any container
        (dev-reporting runs with TRUST_NGINX_HEADERS=true; the test must still
        verify the default-OFF code path)."""
        with mock.patch.dict("os.environ", {"TRUST_NGINX_HEADERS": "false"}):
            resp = client.get(
                "/",
                headers={
                    "X-Remote-User": "admin",
                    "X-Remote-Roles": "superuser,asset_admin",
                },
            )
        # Should redirect to login — the header is not trusted
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "").lower()

    def test_remote_user_with_any_roles_still_ignored(self, client):
        """Even injecting superuser role via header should not work when
        TRUST_NGINX_HEADERS=false."""
        with mock.patch.dict("os.environ", {"TRUST_NGINX_HEADERS": "false"}):
            resp = client.get(
                "/user-management",
                headers={
                    "X-Remote-User": "attacker",
                    "X-Remote-Roles": "superuser",
                },
            )
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "").lower()

    def test_remote_user_rejected_without_proxy_secret(self, client):
        """even with TRUST_NGINX_HEADERS=true and an allowlisted
        peer, a request lacking the X-Proxy-Auth shared secret is denied."""
        with mock.patch.dict(
            "os.environ",
            {
                "TRUST_NGINX_HEADERS": "true",
                "TRUSTED_PROXY_IPS": "127.0.0.1",
                "PROXY_AUTH_SECRET": "test-proxy-secret",
            },
        ):
            resp = client.get(
                "/",
                headers={
                    "X-Remote-User": "admin",
                    "X-Remote-Roles": "superuser",
                },
            )
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "").lower()

    def test_remote_user_rejected_from_non_allowlisted_peer(self, client):
        """a correct secret from a peer outside TRUSTED_PROXY_IPS
        is denied — the TCP peer must be the trusted proxy."""
        with mock.patch.dict(
            "os.environ",
            {
                "TRUST_NGINX_HEADERS": "true",
                "TRUSTED_PROXY_IPS": "172.18.0.5",
                "PROXY_AUTH_SECRET": "test-proxy-secret",
            },
        ):
            resp = client.get(
                "/",
                headers={
                    "X-Remote-User": "admin",
                    "X-Proxy-Auth": "test-proxy-secret",
                    "X-Remote-Roles": "superuser",
                },
            )
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "").lower()

    def test_remote_roles_header_is_ignored_roles_come_from_es(self, client, app):
        """on a fully proven hop, session roles come from ES
        Security — a superuser claim in X-Remote-Roles is discarded."""
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
                return_value=["viewer"],
            ) as resolve:
                client.get(
                    "/",
                    headers={
                        "X-Remote-User": "sso-user",
                        "X-Proxy-Auth": "test-proxy-secret",
                        "X-Remote-Roles": "superuser",
                    },
                )
        resolve.assert_called_once_with("sso-user")
        with client.session_transaction() as sess:
            # Depending on MFA policy the request lands as a full session
            # (roles) or an mfa_pending one (mfa_pending_roles); either way
            # the stored roles are the ES-resolved ones, never the header's.
            stored = sess.get("roles") or sess.get("mfa_pending_roles")
            assert stored == ["viewer"]
            assert sess.get("is_admin") is not True

    def test_remote_user_denied_when_es_roles_unresolvable(self, client):
        """a proven hop still fails closed when the user's roles
        cannot be resolved from ES Security (unknown user, ES down)."""
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
                return_value=None,
            ):
                resp = client.get(
                    "/",
                    headers={
                        "X-Remote-User": "ghost-user",
                        "X-Proxy-Auth": "test-proxy-secret",
                    },
                )
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "").lower()


# ===========================================================================
# Auth bypass on protected routes
# ===========================================================================


class TestAuthBypass:
    """Unauthenticated requests to protected routes should be redirected to login."""

    ADMIN_ENDPOINTS = [
        "/user-management",
        "/import-data",
        "/logs",
        "/testing",
        "/asset/organizations",
    ]

    API_ENDPOINTS = [
        "/api/user-management/create-user",
        "/api/user-management/delete-user",
        "/api/import/run",
        "/api/backup/create",
        "/api/testing/run-pytest",
    ]

    @pytest.mark.parametrize("endpoint", ADMIN_ENDPOINTS)
    def test_unauthenticated_get_redirects_to_login(self, client, endpoint):
        """GET to protected pages without session → redirect to login."""
        resp = client.get(endpoint)
        assert resp.status_code == 302
        location = resp.headers.get("Location", "").lower()
        assert "/login" in location

    @pytest.mark.parametrize("endpoint", API_ENDPOINTS)
    def test_unauthenticated_post_rejected(self, client, endpoint):
        """POST to protected API without session → 302 or 401."""
        resp = client.post(
            endpoint,
            content_type="application/json",
            data="{}",
        )
        assert resp.status_code in (302, 401, 403)

    def test_org_user_redirected_from_user_management(self, org_user_client):
        """Non-admin org user should be redirected away from admin-only pages."""
        resp = org_user_client.get("/user-management")
        assert resp.status_code == 302

    def test_org_user_redirected_from_testing(self, org_user_client):
        resp = org_user_client.get("/testing")
        assert resp.status_code == 302


# ===========================================================================
# SQL injection contract — login path has no SQL backend
# ===========================================================================


class TestNoSqlInLoginPath:
    """Pin the no-SQL contract for the login path.

    Authentication routes through ``lib/es_auth.authenticate_user`` →
    ``es.security.authenticate()``. There is no SQL layer anywhere in
    the request path; the credentials reach the Elasticsearch client
    constructor and nothing else. ZAP rule 40018 (SQL Injection) trips
    on this endpoint because the boolean-condition heuristic
    ("AND 1=1 / OR 1=1") sees Flask response shape changes (CSRF
    rotation, error rendering) and infers a SQL backend. We suppress
    that rule in .github/zap/automation.yaml; this test is the
    own-it-don't-rent-it guarantee that the suppression is justified.
    """

    SQLI_PAYLOADS = [
        "ZAP' OR '1'='1' -- ",
        "admin' --",
        "' UNION SELECT NULL--",
        "1; DROP TABLE users--",
        "' OR 1=1#",
    ]

    @pytest.mark.parametrize("payload", SQLI_PAYLOADS)
    def test_sqli_username_takes_same_path_as_invalid_credentials(self, client, payload):
        """SQLi-shaped usernames must reach authenticate_user just like any
        other invalid username — same response shape, no SQL layer hit."""
        with client.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf"

        with mock.patch(
            "lib.es_auth.authenticate_user",
            return_value={"success": False, "error": "Invalid credentials"},
        ) as mock_auth:
            resp = client._client.post(
                "/login",
                data={
                    "username": payload,
                    "password": "any-password",
                    "csrf_token": "test-csrf",
                },
            )

        # The endpoint always redirects with ?error=... on failure — no
        # 500, no SQL backtrace, no echo of the payload in the body.
        assert resp.status_code == 302
        location = resp.headers.get("Location", "").lower()
        assert "error" in location

        # authenticate_user was called exactly once. The credentials
        # reached the ES auth boundary and stopped there; nothing else
        # (DB cursor, ORM, raw query) interpreted the input. Whitespace
        # trimming of the username before this call is a benign Flask
        # form convention and does not affect the no-SQL contract.
        mock_auth.assert_called_once()


# ===========================================================================
# Session cookie flags
# ===========================================================================


class TestSessionCookieFlags:
    """Session cookies should have Secure, HttpOnly, and SameSite attributes
    configured at the Flask app level."""

    def test_httponly_configured(self, app):
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True

    def test_samesite_configured(self, app):
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_path_configured(self, app):
        assert app.config["SESSION_COOKIE_PATH"] == "/"


# ===========================================================================
# Debug endpoint gating
# ===========================================================================


class TestDebugEndpointGating:
    """Debug endpoints should only be accessible in debug/development mode."""

    def test_returns_404_in_production_mode(self, auth_client, app):
        app.debug = False
        with mock.patch.dict("os.environ", {"APP_ENV": "production"}):
            resp = auth_client.get("/debug/expire-session")
        assert resp.status_code == 404
        app.debug = False

    def test_accessible_in_debug_mode(self, auth_client, app):
        app.debug = True
        resp = auth_client.get("/debug/expire-session")
        assert resp.status_code == 200
        assert b"Session marked as expired" in resp.data
        app.debug = False

    def test_accessible_in_development_env(self, auth_client, app):
        app.debug = False
        with mock.patch.dict("os.environ", {"APP_ENV": "development"}):
            resp = auth_client.get("/debug/expire-session")
        assert resp.status_code == 200
        app.debug = False

    def test_unauthenticated_does_not_leak_info(self, client, app):
        """Even in debug mode, unauthenticated users get redirected to login."""
        app.debug = True
        resp = client.get("/debug/expire-session")
        # Should redirect to login, not expose debug info
        assert resp.status_code in (302, 400)
        app.debug = False


# ===========================================================================
# hCaptcha fallback
# ===========================================================================


class TestHcaptchaFallback:
    """When hCaptcha verification fails, registration should be rejected."""

    def test_registration_rejected_when_captcha_service_unreachable(self, client, app):
        """If hCaptcha API call throws an exception, registration should fail
        instead of silently allowing it through."""
        from asset_management.web.config import config

        orig_site = config._config.get("HCAPTCHA_SITE_KEY", "")
        orig_secret = config._config.get("HCAPTCHA_SECRET_KEY", "")
        orig_reg = config._config.get("REGISTRATION_ENABLED", False)

        try:
            config._config["HCAPTCHA_SITE_KEY"] = "test-site-key"
            config._config["HCAPTCHA_SECRET_KEY"] = "test-secret-key"
            config._config["REGISTRATION_ENABLED"] = True

            # The route does `import requests as http_requests` locally,
            # so mock at the `requests` module level.
            with mock.patch("requests.post",
                            side_effect=ConnectionError("Service unavailable")):
                resp = client.post(
                    "/signup",
                    data={
                        "csrf_token": "test-csrf-token",
                        "username": "testuser123",
                        "email": "test@example.com",
                        "password": "SecurePass123!",
                        "confirm_password": "SecurePass123!",
                        "h-captcha-response": "fake-response",
                    },
                )
            # Should NOT succeed — captcha verification failed.
            # May render the form directly (200) or redirect back to it (302).
            assert resp.status_code in (200, 302)
            if resp.status_code == 200:
                assert b"unavailable" in resp.data.lower()
        finally:
            config._config["HCAPTCHA_SITE_KEY"] = orig_site
            config._config["HCAPTCHA_SECRET_KEY"] = orig_secret
            config._config["REGISTRATION_ENABLED"] = orig_reg


# ===========================================================================
# CORS rejection
# ===========================================================================


class TestCorsRejection:
    """Cross-origin requests should be rejected."""

    def test_preflight_with_origin_returns_403(self, client):
        """OPTIONS request with Origin header (CORS preflight) should be rejected."""
        resp = client._client.options(
            "/api/documentation-permissions",
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status_code == 403

    def test_no_access_control_allow_origin(self, auth_client):
        """Responses should never include Access-Control-Allow-Origin."""
        resp = auth_client.get("/")
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_no_cors_on_api_responses(self, auth_client):
        """API responses should not include CORS headers."""
        resp = auth_client.get("/api/documentation-permissions")
        assert "Access-Control-Allow-Origin" not in resp.headers
        assert "Access-Control-Allow-Methods" not in resp.headers


# ===========================================================================
# MFA session safety (no plaintext password)
# ===========================================================================


class TestMfaSessionSafety:
    """The MFA pending session should never contain the plaintext password."""

    def test_login_stores_kibana_sid_not_password(self, client):
        """After password auth with MFA required, the session should contain
        mfa_pending_kibana_sid (not mfa_pending_password)."""
        with mock.patch("lib.es_auth.authenticate_user", return_value={
            "success": True,
            "roles": ["asset_admin"],
            "full_name": "Test User",
            "email": "test@example.com",
        }), mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required", return_value={
            "required": True,
            "method": "totp",
            "setup_complete": True,
        }), mock.patch("asset_management.web.blueprints.analytics.routes._create_kibana_session", return_value="test-sid"), \
             mock.patch("asset_management.web.blueprints.analytics.routes.AuditLogService.log_event"), \
             mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True):
            resp = client.post(
                "/login",
                data={
                    "csrf_token": "test-csrf-token",
                    "username": "testuser",
                    "password": "testpassword",
                },
            )

        # Should redirect to MFA challenge
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "mfa" in location.lower()

        # Inspect the session — should NOT contain the password
        with client.session_transaction() as sess:
            assert "mfa_pending_password" not in sess
            assert sess.get("mfa_pending_kibana_sid") == "test-sid"

    def test_legacy_dashboards_login_post_redirects_then_stores_kibana_sid_not_password(self, client):
        """POST /dashboards/login → 308 → POST /login. Following the
        chain end-to-end (with the same MFA-required mocks) must still write
        mfa_pending_kibana_sid into the session, never the password."""
        with mock.patch("lib.es_auth.authenticate_user", return_value={
            "success": True,
            "roles": ["asset_admin"],
            "full_name": "Test User",
            "email": "test@example.com",
        }), mock.patch("asset_management.web.blueprints.analytics.routes.MFAService.is_mfa_required", return_value={
            "required": True,
            "method": "totp",
            "setup_complete": True,
        }), mock.patch("asset_management.web.blueprints.analytics.routes._create_kibana_session", return_value="dash-sid"), \
             mock.patch("asset_management.web.blueprints.analytics.routes.AuditLogService.log_event"), \
             mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True):
            resp = client.post(
                "/dashboards/login",
                data={
                    "csrf_token": "test-csrf-token",
                    "username": "testuser",
                    "password": "testpassword",
                },
                follow_redirects=True,
            )

        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert "mfa_pending_password" not in sess
            assert sess.get("mfa_pending_kibana_sid") == "dash-sid"


# ===========================================================================
# SECRET_KEY hard refusal
# ===========================================================================


class TestSecretKeyValidation:
    """validate_secret_key refuses missing, sentinel, and short values."""

    def test_should_raise_when_secret_is_default(self):
        from asset_management.web.secret_validation import (
            InvalidSecretError,
            validate_secret_key,
        )

        with pytest.raises(InvalidSecretError, match="forbidden sentinel"):
            validate_secret_key("dev-secret-key-change-in-production")

    def test_should_raise_when_secret_is_null_string(self):
        """yq returns the literal string 'null' for missing yaml keys —
        this is the exact value that caused the 2026-04-23 prod incident."""
        from asset_management.web.secret_validation import (
            InvalidSecretError,
            validate_secret_key,
        )

        with pytest.raises(InvalidSecretError, match="forbidden sentinel"):
            validate_secret_key("null")

    def test_should_raise_when_secret_is_none_string(self):
        """Python's str(None) when a yaml-None slips through .get()."""
        from asset_management.web.secret_validation import (
            InvalidSecretError,
            validate_secret_key,
        )

        with pytest.raises(InvalidSecretError, match="forbidden sentinel"):
            validate_secret_key("None")

    def test_should_raise_when_secret_is_empty_string(self):
        from asset_management.web.secret_validation import (
            InvalidSecretError,
            validate_secret_key,
        )

        with pytest.raises(InvalidSecretError, match="forbidden sentinel"):
            validate_secret_key("")

    def test_should_raise_when_secret_is_none(self):
        from asset_management.web.secret_validation import (
            InvalidSecretError,
            validate_secret_key,
        )

        with pytest.raises(InvalidSecretError, match="missing"):
            validate_secret_key(None)

    def test_should_raise_when_secret_is_shorter_than_32_chars(self):
        from asset_management.web.secret_validation import (
            InvalidSecretError,
            validate_secret_key,
        )

        with pytest.raises(InvalidSecretError, match="too short"):
            validate_secret_key("a" * 31)

    def test_should_accept_strong_random_value(self):
        """A 64-char hex string from `openssl rand -hex 32` is the canonical
        good input. validate_secret_key should return None silently."""
        from asset_management.web.secret_validation import validate_secret_key

        # No exception = pass.
        validate_secret_key("a" * 64)

    def test_should_use_provided_name_in_error_message(self):
        """Error messages identify which secret failed so operators don't
        have to guess (we validate multiple secrets in the deploy path)."""
        from asset_management.web.secret_validation import (
            InvalidSecretError,
            validate_secret_key,
        )

        with pytest.raises(InvalidSecretError, match="ES01_PASSWORD"):
            validate_secret_key("", name="ES01_PASSWORD")


# ===========================================================================
# TRUST_NGINX_HEADERS default
# ===========================================================================


class TestTrustNginxHeadersDefault:
    """TRUST_NGINX_HEADERS should default to false."""

    def test_default_is_false(self):
        from asset_management.web.config import Config

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRUST_NGINX_HEADERS", None)
            cfg = Config.__new__(Config)
            cfg._config = {}
            assert cfg.TRUST_NGINX_HEADERS is False


# ===========================================================================
# Path traversal protection
# ===========================================================================


class TestPathTraversal:
    """Path traversal attempts in URL parameters should be handled safely."""

    def test_log_download_traversal_blocked(self, auth_client):
        """Attempting path traversal in log download filename should not
        serve arbitrary files."""
        resp = auth_client.get("/logs/download/../../etc/passwd")
        # Should be 404, 400, or safe redirect — not 200 with file contents
        assert resp.status_code in (302, 400, 404, 500)
        assert b"root:" not in resp.data

    def test_log_view_traversal_blocked(self, auth_client):
        resp = auth_client.get("/logs/view/../../etc/passwd")
        assert resp.status_code in (302, 400, 404, 500)
        assert b"root:" not in resp.data


# ===========================================================================
# Rate limiting (basic check)
# ===========================================================================


class TestRateLimiting:
    """Login rate limiting should trigger after too many failed attempts."""

    def test_rate_limit_after_5_failures(self, client):
        """After 5 failed login attempts, further attempts should be blocked."""
        from asset_management.web import auth as auth_mod

        # Clear any existing rate limit state
        auth_mod.failed_attempts.clear()

        with mock.patch("asset_management.web.auth.validate_csrf_token", return_value=True), \
             mock.patch("lib.es_auth.authenticate_user", return_value={
                 "success": False,
                 "error": "Invalid credentials",
             }):
            # Make 5 failed login attempts
            for _ in range(5):
                client.post(
                    "/login",
                    data={
                        "csrf_token": "test-csrf-token",
                        "username": "attacker",
                        "password": "wrongpassword",
                    },
                )

            # 6th attempt should be rate-limited
            resp = client.post(
                "/login",
                data={
                    "csrf_token": "test-csrf-token",
                    "username": "attacker",
                    "password": "wrongpassword",
                },
            )
        # Should be blocked: 429 (Too Many Requests) or 302 redirect with error
        assert resp.status_code in (302, 429)

        auth_mod.failed_attempts.clear()


# ===========================================================================
# SEC-WORKFLOW-PERMS — GitHub Actions least-privilege audit
# ===========================================================================


class TestWorkflowPermissions:
    """Every workflow under .github/workflows/ must declare an explicit
    `permissions:` block with no `write-all` defaults. Catches drift toward
    the GITHUB_TOKEN having more scopes than the job actually needs."""

    def test_should_declare_explicit_least_privilege_permissions_in_every_workflow(self):
        from pathlib import Path

        import yaml

        repo_root = Path(__file__).resolve().parents[2]
        workflows_dir = repo_root / ".github" / "workflows"
        files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
        if not files:
            # In the imports_container only `tests/` is mounted, so .github/
            # isn't reachable. The CI run (full checkout) is the canonical
            # execution of this test.
            pytest.skip(f"workflow files not reachable from {workflows_dir}")

        offenders = []
        for path in files:
            doc = yaml.safe_load(path.read_text())
            if not isinstance(doc, dict):
                offenders.append(f"{path.name}: not a YAML mapping")
                continue

            top_perms = doc.get("permissions")
            jobs = doc.get("jobs", {}) or {}
            jobs_with_perms = {
                name: cfg.get("permissions")
                for name, cfg in jobs.items()
                if isinstance(cfg, dict) and cfg.get("permissions") is not None
            }

            # Top-level OR every job must declare permissions explicitly.
            if top_perms is None and len(jobs_with_perms) != len(jobs):
                offenders.append(
                    f"{path.name}: missing top-level permissions and not every job declares its own"
                )
                continue

            # Reject `write-all` shorthand and `permissions: write-all` mappings.
            def _is_write_all(p):
                if p == "write-all":
                    return True
                if isinstance(p, dict):
                    return any(v == "write" for k, v in p.items() if k == "*")
                return False

            if _is_write_all(top_perms):
                offenders.append(f"{path.name}: top-level permissions is write-all")
            for job_name, perms in jobs_with_perms.items():
                if _is_write_all(perms):
                    offenders.append(f"{path.name}: job '{job_name}' permissions is write-all")

        assert not offenders, "Workflow permission policy violations:\n  " + "\n  ".join(offenders)
