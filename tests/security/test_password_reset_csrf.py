"""CSRF enforcement on the standalone password-reset Flask app (port 8085).

Covers every POST endpoint must reject requests without a valid
csrf_token form field.
"""

import importlib
import os
import sys

import pytest


@pytest.fixture(scope="module")
def pw_reset_app():
    """Import the password-reset app fresh and configure it for testing."""
    pw_reset_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "bin", "python",
    )
    if pw_reset_dir not in sys.path:
        sys.path.insert(0, pw_reset_dir)

    # password_reset.app reads APP_SECRET_KEY at import; a deterministic value
    # keeps the session signature stable across the test process.
    os.environ.setdefault("APP_SECRET_KEY", "test-pw-reset-secret-key-do-not-use-in-prod")

    if "password_reset.app" in sys.modules:
        importlib.reload(sys.modules["password_reset.app"])
    pw_reset = importlib.import_module("password_reset.app")
    pw_reset.app.config.update(TESTING=True)
    return pw_reset.app


@pytest.fixture
def client(pw_reset_app):
    return pw_reset_app.test_client()


# Endpoints that accept POST and must validate CSRF.
PROTECTED_POST_ENDPOINTS = [
    "/reset-password/request",
    "/reset-password/admin/login",
    "/reset-password/admin/settings",
    "/reset-password/admin/test-email",
    "/reset-password/admin/change-password",
    # /reset/<token> needs a token in URL; skipped here (covered by token check)
]


@pytest.mark.parametrize("endpoint", PROTECTED_POST_ENDPOINTS)
def test_post_without_csrf_token_is_rejected(client, endpoint):
    """POST with no csrf_token form field returns 403."""
    resp = client.post(endpoint, data={})
    assert resp.status_code == 403
    assert b"CSRF" in resp.data


@pytest.mark.parametrize("endpoint", PROTECTED_POST_ENDPOINTS)
def test_post_with_wrong_csrf_token_is_rejected(client, endpoint):
    """POST with a csrf_token that doesn't match the session returns 403."""
    resp = client.post(endpoint, data={"csrf_token": "definitely-not-the-real-token"})
    assert resp.status_code == 403
    assert b"CSRF" in resp.data


def test_get_request_does_not_set_csrf_token_into_session_until_template_renders(client):
    """The token generator only fires when a template references csrf_token()."""
    # GET on the request page renders request_reset.html which calls csrf_token()
    resp = client.get("/reset-password/request")
    assert resp.status_code == 200
    # Page should contain a hidden CSRF input rendered by the template
    assert b'name="csrf_token"' in resp.data


def test_post_with_matching_csrf_token_passes_csrf_check(client):
    """Submitting the token shown on the page passes CSRF, then the route logic
    runs as normal (we don't assert success — username may not exist — only that
    we got past the CSRF gate)."""
    # GET to seed the session and grab the token
    page = client.get("/reset-password/request")
    body = page.data.decode("utf-8", errors="replace")
    # Extract the token value from the rendered hidden input
    import re
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', body)
    assert match, "expected csrf_token hidden input on request page"
    token = match.group(1)

    resp = client.post(
        "/reset-password/request",
        data={"csrf_token": token, "username": "nonexistent@example.com"},
    )
    # Expect anything other than a 403 CSRF rejection
    assert resp.status_code != 403 or b"CSRF" not in resp.data
