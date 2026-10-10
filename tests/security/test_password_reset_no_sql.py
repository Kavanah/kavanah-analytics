"""No-SQL contract for the standalone password-reset app.

Pin that the request-reset endpoint has no SQL backend. ZAP rule 40018
(SQL Injection) trips on POST /reset-password/request username with the
boolean payload "ZAP' OR '1'='1' --" because Flask response shape
(rate-limit hits, error rendering) varies with the input. The actual
backend is the Elasticsearch Security API, not a SQL query layer:

    /reset-password/request
      → password_reset.app.request_reset()
      → user_exists() / find_user_by_email()
      → requests.get(<es_host>/_security/user/<username>)

A strict username-allowlist regex (``_is_safe_es_username``) further
rejects any value containing characters outside ``[A-Za-z0-9._@+\\-]``
*before* any ES call, so SQLi-shaped payloads short-circuit and never
reach the ES API.

We suppress rule 40018 on this endpoint in
``.github/zap/automation.yaml``. This test is the own-it-don't-rent-it
guarantee that the suppression is justified.
"""

import importlib
import os
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
    # Redirect the audit log to a tmp file so module import succeeds
    # regardless of who owns the on-disk audit.log (mirrors the
    # release-plan fixture).
    audit_log = tmp_path_factory.mktemp("pw-reset-no-sql") / "audit.log"
    os.environ["PW_RESET_AUDIT_LOG"] = str(audit_log)

    if "password_reset.app" in sys.modules:
        importlib.reload(sys.modules["password_reset.app"])
    pw_reset = importlib.import_module("password_reset.app")
    pw_reset.app.config.update(TESTING=True)
    return pw_reset.app


@pytest.fixture
def client(pw_reset_app):
    return pw_reset_app.test_client()


SQLI_PAYLOADS = [
    "ZAP' OR '1'='1' -- ",
    "admin' --",
    "' UNION SELECT NULL--",
    "1; DROP TABLE users--",
    "' OR 1=1#",
]


@pytest.mark.parametrize("payload", SQLI_PAYLOADS)
def test_username_allowlist_rejects_sqli_payloads_before_es_lookup(payload):
    """``_is_safe_es_username`` must reject any payload that contains
    characters outside the allowlist — every SQLi shape includes spaces,
    quotes, semicolons, or operators that fail the regex."""
    # Locate the helper without importing the app (which has heavyweight
    # module-level setup); the function is a pure regex check.
    pw_reset_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "bin",
        "python",
    )
    if pw_reset_dir not in sys.path:
        sys.path.insert(0, pw_reset_dir)
    from password_reset.app import _is_safe_es_username

    assert _is_safe_es_username(payload) is False, (
        f"username allowlist must reject SQLi payload {payload!r} — every "
        "SQLi shape contains spaces / quotes / operators outside the "
        "[A-Za-z0-9._@+-] character class"
    )


@pytest.mark.parametrize("payload", SQLI_PAYLOADS)
def test_request_reset_returns_generic_response_for_sqli_username(client, payload):
    """The endpoint must return its uniform "if this account exists..."
    response shape regardless of input, never a 500 / SQL backtrace /
    payload echo. Combined with the allowlist test above, this pins
    that the SQLi shape never reaches any backend interpreter."""
    # Seed the CSRF cookie (earlier the token lives in pw_reset_csrf,
    # not in the Flask session) and submit with the matching form value.
    client.set_cookie("pw_reset_csrf", "test-csrf", domain="localhost", path="/reset-password")

    resp = client.post(
        "/reset-password/request",
        data={"username": payload, "csrf_token": "test-csrf"},
    )

    # Endpoint always returns 200 with a flash message — never 500.
    assert resp.status_code == 200, (
        f"SQLi payload {payload!r} produced a {resp.status_code}; the "
        "endpoint must short-circuit to a uniform response shape, not "
        "leak a backend error"
    )
    body = resp.get_data(as_text=True)
    # Body must not echo the payload (defends against XSS *and* against
    # any reflection that could feed a downstream SQL-shaped sink).
    assert payload not in body, (
        f"endpoint echoed SQLi payload {payload!r} in the response body"
    )
