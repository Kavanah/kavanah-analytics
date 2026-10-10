"""Session cookie scoping for the password-reset app.

The DEAM app and the password-reset app are separate Flask apps served
from the same host. Both default to a session cookie named ``session``
at path ``/``. When a user touched both apps in the same browser the
second app's Set-Cookie clobbered the first, producing intermittent
CSRF 403s on the password-reset form: GET rendered csrf_token from
session A, POST arrived with session B.

Two scoping attributes prevent collision:
  - SESSION_COOKIE_NAME = "pw_reset_session" (distinct name)
  - SESSION_COOKIE_PATH = "/reset-password" (distinct path)

Either alone would also work, but both together belt-and-braces it
and documents intent.
"""

import importlib
import os
import re
import sys

import pytest


@pytest.fixture(scope="module")
def pw_reset_app(tmp_path_factory):
    pw_reset_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "bin",
        "python",
    )
    if pw_reset_dir not in sys.path:
        sys.path.insert(0, pw_reset_dir)

    os.environ.setdefault(
        "APP_SECRET_KEY", "test-pw-reset-secret-key-do-not-use-in-prod"
    )
    audit_log = tmp_path_factory.mktemp("pw-reset-session-scope") / "audit.log"
    os.environ["PW_RESET_AUDIT_LOG"] = str(audit_log)

    if "password_reset.app" in sys.modules:
        importlib.reload(sys.modules["password_reset.app"])
    pw_reset = importlib.import_module("password_reset.app")
    pw_reset.app.config.update(TESTING=True)
    return pw_reset.app


@pytest.fixture
def client(pw_reset_app):
    return pw_reset_app.test_client()


_TOKEN_RE = re.compile(r'name="csrf_token" value="([0-9a-f]+)"')


def test_session_cookie_name_is_distinct_from_deam_app(pw_reset_app):
    """The DEAM app uses Flask's default 'session' cookie; password_reset
    must NOT, or the two cookies overwrite each other on the same host."""
    assert pw_reset_app.config["SESSION_COOKIE_NAME"] == "pw_reset_session"
    assert pw_reset_app.config["SESSION_COOKIE_NAME"] != "session"


def test_session_cookie_path_is_scoped_to_reset_password(pw_reset_app):
    """Defense in depth: even if the cookie name accidentally collided,
    scoping the path keeps the browser from sending it to the DEAM app."""
    assert pw_reset_app.config["SESSION_COOKIE_PATH"] == "/reset-password"


def test_set_cookie_header_carries_scoped_name_and_path(client):
    """End-to-end: a request flow that writes to the session must emit a
    Set-Cookie with the scoped name + path, not the Flask defaults.

    Note: earlier, the CSRF token lives in a dedicated cookie rather
    than the session, so a plain GET no longer triggers a session write.
    A POST that calls flash() does — exercise that path here.
    """
    seed = client.get("/reset-password/request")
    assert seed.status_code == 200
    token_match = _TOKEN_RE.search(seed.data.decode("utf-8", errors="replace"))
    assert token_match, "GET must render the CSRF token in the form"
    csrf_token = token_match.group(1)

    # POST hits the "no such user" branch which calls flash() — that is
    # what writes to the session and triggers the scoped Set-Cookie.
    resp = client.post(
        "/reset-password/request",
        data={"csrf_token": csrf_token, "username": "no-such-user"},
    )
    assert resp.status_code == 200

    # the response carries multiple Set-Cookie headers (one for
    # pw_reset_csrf, one for the session itself); inspect the session one
    # specifically rather than relying on header ordering.
    set_cookies = resp.headers.get_all("Set-Cookie")
    session_set_cookie = next(
        (sc for sc in set_cookies if "pw_reset_session=" in sc), None
    )
    assert session_set_cookie is not None, (
        f"POST that flashes a message must emit a pw_reset_session Set-Cookie; got: {set_cookies}"
    )

    # The default Flask 'session' name must not leak from any Set-Cookie.
    for sc in set_cookies:
        assert not re.search(r"(?:^|;\s*)session=", sc), (
            f"Set-Cookie must NOT carry the default 'session' name; got: {sc}"
        )

    # The path attribute on the session cookie is scoped.
    assert "Path=/reset-password" in session_set_cookie, (
        f"Session Set-Cookie must scope Path to /reset-password; got: {session_set_cookie}"
    )
