"""
Shared pytest fixtures for all test modules.

Setup order:
1. sys.path is extended with bin/python so all project modules resolve.
2. Environment variables are set before any project module is imported.
3. The Flask app is imported and configured once per session.
4. Per-function fixtures provide fresh clients and clean auth state.
"""

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

import pytest
from unittest import mock  # allow_index_scope fixture

# ---------------------------------------------------------------------------
# sys.path — must run before any project import
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
_WEB_DIR = os.path.join(_PYTHON_DIR, "asset_management", "web")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)
if _WEB_DIR not in sys.path:
    sys.path.insert(0, _WEB_DIR)


# ---------------------------------------------------------------------------
# Environment variables — must be set before importing config.py / app.py
# ---------------------------------------------------------------------------
# Use _set_if_blank instead of setdefault: in CI, secrets that are unavailable
# in a given context (e.g. Dependabot PRs that can't read repo Actions secrets)
# expand to an empty string, which setdefault treats as "already set" and
# leaves blank. That breaks tests whose subjects early-return on missing
# credentials. Treat empty values the same as missing.
def _set_if_blank(key: str, value: str) -> None:
    if not os.environ.get(key):
        os.environ[key] = value


_set_if_blank("FLASK_USE_PREFIX", "false")
_set_if_blank("TRUST_NGINX_HEADERS", "false")
_set_if_blank("ES_USE_SSL", "true")  # cert won't exist locally → fast None return, no retries
_set_if_blank("ES_AUTH_REQUIRED", "true")
_set_if_blank("ES01_INTERNAL_IP", "127.0.0.1")
_set_if_blank("ES01_PASSWORD", "test-password")
_set_if_blank("SECRET_KEY", "test-secret-key-for-pytest-do-not-use-in-prod")
_set_if_blank("SESSION_LIFETIME", "3600")
_set_if_blank("ASSET_ADMIN_ROLE", "asset_admin")
# publish tokens are stored Fernet-encrypted (display copy), so the
# publish endpoints call lib.encryption.encrypt. A fixed, test-only key (built
# from a plain label, not a secret) lets them run without master-config.yaml.
_set_if_blank("ENC_KEY", base64.urlsafe_b64encode(b"pytest-only-enc-key-not-a-secret").decode())

# ---------------------------------------------------------------------------
# Project imports (after path / env setup)
# ---------------------------------------------------------------------------
from asset_management.web.app import app as _flask_app  # noqa: E402
from asset_management.web import auth as auth_module  # noqa: E402


CSRF_TEST_TOKEN = "test-csrf-token"


class CsrfTestClient:
    """Wrapper around FlaskClient that auto-injects the CSRF header on
    state-changing requests so existing tests don't need individual changes."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    def _inject(self, kwargs):
        if "headers" not in kwargs:
            kwargs["headers"] = {}
        if isinstance(kwargs["headers"], dict):
            kwargs["headers"].setdefault("X-CSRF-Token", CSRF_TEST_TOKEN)
        return kwargs

    def post(self, *args, **kwargs):
        return self._client.post(*args, **self._inject(kwargs))

    def put(self, *args, **kwargs):
        return self._client.put(*args, **self._inject(kwargs))

    def delete(self, *args, **kwargs):
        return self._client.delete(*args, **self._inject(kwargs))

    def patch(self, *args, **kwargs):
        return self._client.patch(*args, **self._inject(kwargs))

    def get(self, *args, **kwargs):
        return self._client.get(*args, **kwargs)


# ---------------------------------------------------------------------------
# App fixture (session-scoped — one Flask app for the whole test run)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def app():
    _flask_app.config.update(
        {
            "TESTING": True,
            "SESSION_COOKIE_SECURE": False,  # allow test client over http
            "SESSION_COOKIE_HTTPONLY": True,
        }
    )
    yield _flask_app


# ---------------------------------------------------------------------------
# Client fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def client(app):
    """Unauthenticated test client."""
    return CsrfTestClient(app.test_client())


@pytest.fixture
def auth_client(app):
    """Test client with an authenticated asset_admin session."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["username"] = "testuser"
        sess["roles"] = ["asset_admin"]
        sess["is_admin"] = True
        sess["full_name"] = "Test User"
        sess["email"] = "test@example.com"
        sess["auth_timestamp"] = time.time()
        sess["csrf_token"] = CSRF_TEST_TOKEN
    return CsrfTestClient(c)


@pytest.fixture
def tiktok_on(monkeypatch):
    """turn the TikTok connector switch on for one test.

    TIKTOK_ENABLED is off by absence, which is also how the test process starts,
    so a test of TikTok behavior asks for it explicitly (module-level
    ``pytestmark = pytest.mark.usefixtures("tiktok_on")``). The off state is
    pinned by the release-plan tests
    """
    monkeypatch.setenv("TIKTOK_ENABLED", "true")


@pytest.fixture
def superuser_client(app):
    """Test client with a superuser session."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["username"] = "superuser"
        sess["roles"] = ["superuser"]
        sess["is_admin"] = True
        sess["full_name"] = "Super User"
        sess["email"] = "super@example.com"
        sess["auth_timestamp"] = time.time()
        sess["csrf_token"] = CSRF_TEST_TOKEN
    return CsrfTestClient(c)


@pytest.fixture
def billing_owner_client(app, monkeypatch):
    """Test client for a NON-superuser who may reach Billing — a
    workspace_owner. Use this (not superuser_client) for billing tests
    that exercise the PAYING-user surface: a superuser now gets the
    'complimentary access / no payment required' view (follow-up
    2026-06-05), so superuser_client no longer renders the card form /
    pay flow. workspace_owner is a System-2 grant needing ES, so the
    access guard is bypassed here for the unit test.
    """
    import asset_management.web.auth as _auth

    monkeypatch.setattr(_auth, "user_can_access_billing", lambda *a, **k: True)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["username"] = "owneruser"
        sess["roles"] = ["workspace_owner"]
        sess["is_admin"] = False
        sess["full_name"] = "Owner User"
        sess["email"] = "owner@example.com"
        sess["auth_timestamp"] = time.time()
        sess["csrf_token"] = CSRF_TEST_TOKEN
    return CsrfTestClient(c)


@pytest.fixture
def org_user_client(app):
    """Test client authenticated with only an org role (not asset_admin or superuser)."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["username"] = "orguser"
        sess["roles"] = ["org_test123"]
        sess["is_admin"] = False
        sess["full_name"] = "Org User"
        sess["email"] = "org@example.com"
        sess["auth_timestamp"] = time.time()
        sess["csrf_token"] = CSRF_TEST_TOKEN
    return CsrfTestClient(c)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Clear in-memory rate limit state before and after every test.

    ``failed_attempts`` is LoginLockoutService's in-process username
    fallback (same object); the IP fallback store is cleared alongside it.

    the registration limiter's store also counts wizard verification
    emails per account / address, so it is cleared too — otherwise resends in
    one test eat the hourly budget of the next."""
    from asset_management.web.services import registration_service as _registration_mod
    from asset_management.web.services.login_lockout_service import LoginLockoutService

    auth_module.failed_attempts.clear()
    LoginLockoutService.local_ip_failures.clear()
    _registration_mod._registration_attempts.clear()
    yield
    auth_module.failed_attempts.clear()
    LoginLockoutService.local_ip_failures.clear()
    _registration_mod._registration_attempts.clear()


@pytest.fixture(autouse=True)
def _bypass_user_exists_guard(monkeypatch):
    """follow-up — bypass the verify_auth user-exists guard
    in every test by default.

    The guard (auth._user_still_exists) hits ES on cache-miss to
    check whether the session's username still exists in ES Security.
    In tests that planted a session for 'operator' / 'alice' / etc.
    via session_transaction(), there is no ES Security user with
    that name, so the guard would force-logout and turn 200s into
    302s across the existing test suite.

    Tests that specifically want to exercise the guard (the
    force-logout tests) opt back in by
    monkeypatching auth._user_still_exists / auth._user_is_disabled /
    auth._user_account_state / auth._user_account_record to their own
    function inside the test.

    the guard grew a second question — is the user suspended? —
    answered from the same ES record, so the bypass now sits on the shared
    primitive ``_user_account_state`` (every session is "active") rather
    than on one of its two views.

    the ES record answers a third question — has this account's
    credential changed since the session authenticated? — via the same
    single-fetch primitive, now named ``_user_account_record`` and returning
    ``(state, credential_changed_epoch, roles)`` (added the third
    element). ``_user_account_state`` is a thin view over element 0 of that
    tuple, so patching it alone (as above) still covers every existing
    caller of the state view; ``_user_account_record`` itself is patched
    separately here so ``session_denial_reason``'s credential-changed check
    (which calls ``_credential_changed_at``, the view over element 1) also
    answers "fine" — ``None`` means "never changed", i.e. never denies —
    for tests that don't opt back in.

    element 2 (roles) is ``None`` here, which is deliberately
    "unknown, don't touch the session" — ``auth.refresh_session_roles`` and
    ``auth._account_roles`` treat ``None`` as "cannot resolve" and leave
    ``session["roles"]`` alone, so this bypass does not perturb any
    existing test's session roles. Tests that want to exercise the
    refresh (the release-plan tests)
    opt back in the same way do, via the ``real_state``
    fixture pattern.
    """
    # Drop any state from a prior test before we start.
    auth_module._invalidate_user_exists_cache()
    # Monkeypatch the primitive so both views answer "fine" for the test.
    monkeypatch.setattr(auth_module, "_user_account_state", lambda username: "active")
    monkeypatch.setattr(auth_module, "_user_account_record", lambda username: ("active", None, None))
    yield
    auth_module._invalidate_user_exists_cache()


# ---------------------------------------------------------------------------
# OAuth browser-binding bypass
# ---------------------------------------------------------------------------
# Every fb_oauth_server callback now refuses a state unless the browser holds
# the binding cookie its initiate leg set. The callback tests written before
# call the callback directly with a made-up state and pin OTHER
# contracts (token exchange, identity probes, expiry, scopes), so the check is
# answered "bound" for them. The tests that pin the binding itself opt out by
# file and exercise the real helper.
_OAUTH_BINDING_OPT_OUT_FILES = ("test_hardening.py",)


@pytest.fixture(autouse=True)
def _bypass_oauth_browser_binding(request, monkeypatch):
    if str(request.node.fspath).endswith(_OAUTH_BINDING_OPT_OUT_FILES):
        return
    import lib.oauth_state as _oauth_state_mod

    monkeypatch.setattr(_oauth_state_mod, "oauth_browser_bound", lambda state, cookies: True)


# ---------------------------------------------------------------------------
# workspace-scope guard bypass
# ---------------------------------------------------------------------------
# Tests authored before the #2 user-list filter and the
# membership / target workspace-scope guards landed mock the
# UserProvisioningService and friends at the route layer. The new
# helpers query Elasticsearch and read Flask session state — in CI the
# integration runner has no fixture data and the unit runner has no
# session, so the guards spuriously 503 / 403 / drop rows and break
# dozens of unrelated tests.
#
# This autouse fixture neuters the helpers for every test except those
# (the release-plan tests that pin the guards' contracts) and
# tests/security/ (the attack-surface coverage). Those directories
# install their own per-test patches and need the real helpers, so
# they're opted out by path.
_WORKSPACE_GUARD_OPT_OUT_DIRS = (
    os.path.join("tests", "release_plans", "plan-52"),
    os.path.join("tests", "release_plans", "plan-59"),
    os.path.join("tests", "security"),
)

# Per-file opt-out — test files that need the real helpers but live in
# a directory not on the full opt-out list. Cheaper than promoting the
# whole directory when only one test in it pins these specific
# contracts.
_WORKSPACE_GUARD_OPT_OUT_FILES = (
    # pins get_allowed_workspaces resolution AND the data-
    # grant join leg of _filter_users_to_current_workspace_scope.
    "test_workspace_grant_resolution.py",
    # pins that _filter_users_to_current_workspace_scope's
    # Stage 1 reads the workspace ROLE index (membership index retired);
    # needs the real helper, not the passthrough stub.
    "test_user_list_workspace_role.py",
    # pins that _filter_users_to_current_workspace_scope narrows
    # an org-tier operator to their own org(s); needs the real helper.
    "test_user_list_org_scope.py",
    # follow-up — the /user-management render test drives the org
    # dropdown narrowing + Create User button gate; needs the real
    # get_allowed_workspaces (not the autouse None stub).
    "test_user_mgmt_org_dropdown.py",
    # pins that the reset-password route runs the REAL
    # _require_target_in_workspace_scope so an owner is denied an
    # out-of-scope target; needs the real helper, not the passthrough stub.
    "test_pwreset_owner.py",
    # pins that the REAL _require_target_in_workspace_scope
    # refuses platform-admin targets for non-superuser operators; the
    # passthrough stub would hide exactly that refusal.
    "test_route_authz.py",
    # high batch — same reason: these pin the REAL scope helpers.
    "test_route_authz_sched.py",
    "test_route_authz_invite_join.py",
    "test_route_authz_assets_dash.py",
    "test_route_authz_connectors.py",
    "test_route_authz_medium_admin.py",
    "test_route_authz_medium_connectors.py",
    "test_route_authz_residuals.py",
    # pins that the REAL _require_memberships_in_workspace_scope
    # resolves multi-org and org-less teams; the stub would hide the 403.
    "test_create_user_multi_org_team.py",
)


@pytest.fixture(autouse=True)
def _bypass_workspace_scope_guards(request, monkeypatch):
    fspath = str(request.node.fspath)
    for opt_out in _WORKSPACE_GUARD_OPT_OUT_DIRS:
        if opt_out in fspath:
            return
    for opt_out_file in _WORKSPACE_GUARD_OPT_OUT_FILES:
        if fspath.endswith(opt_out_file):
            return
    from asset_management.web import app as _app_mod
    from asset_management.web.services import role_filter_service as _rfs_mod

    monkeypatch.setattr(_app_mod, "_require_memberships_in_workspace_scope", lambda memberships: None)
    monkeypatch.setattr(_app_mod, "_require_target_in_workspace_scope", lambda target_username: None)
    monkeypatch.setattr(
        # follow-up: _filter_users_to_current_workspace_scope
        # accepts an optional ``override_workspace_esid`` kwarg now.
        # Accept arbitrary kwargs so tests in unrelated suites that
        # trigger this autouse fixture don't break when the route
        # passes the kwarg through.
        _app_mod,
        "_filter_users_to_current_workspace_scope",
        lambda users, **_kw: users,
    )
    monkeypatch.setattr(_rfs_mod.RoleFilterService, "get_allowed_workspaces", classmethod(lambda cls: None))


# ---------------------------------------------------------------------------
# wizard email-verified gate bypass
# ---------------------------------------------------------------------------
# refuses every wizard step past verify-email (step-4 on, and the
# invite team picker) until ``metadata.email_verified`` is set, read live from
# ES Security. The ~60 pre-existing wizard tests plant a session and mock the
# step's own services, with no ES user behind it, so the real gate would
# bounce every one of them to the verify step. This autouse fixture lets the
# gate pass everywhere except the files that pin it, which run the real one.
_EMAIL_VERIFIED_GATE_OPT_OUT_FILES = ("test_signup.py",)


@pytest.fixture(autouse=True)
def _bypass_wizard_email_verified_gate(request, monkeypatch):
    fspath = str(request.node.fspath)
    if fspath.endswith(_EMAIL_VERIFIED_GATE_OPT_OUT_FILES):
        return
    from asset_management.web.blueprints.analytics import routes as _routes_mod

    monkeypatch.setattr(_routes_mod, "_redirect_unless_email_verified", lambda username: None)


# ---------------------------------------------------------------------------
# Release-plan filtering and JSON report
#
# Two opt-in env vars:
#   RELEASE_PLAN_NAME    — when set, only tests whose @release_plan markers
#                          include this name are kept; others are deselected.
#                          (Argument-aware filtering; pytest's `-m` expression
#                          language only matches marker names, not args.)
#   RELEASE_PLAN_REPORT  — when set, a structured per-`test_id` JSON file is
#                          written to this path at session end.
#
# The Testing-page endpoint sets both. Schema is forward-compatible with
# Story 4's pytest+Playwright merge — every entry carries `source`.
# ---------------------------------------------------------------------------
def _marker_args(item, marker_name):
    """Return the list of all args from every marker named `marker_name`
    on the given item (or an empty list)."""
    return [m.args[0] for m in item.iter_markers(name=marker_name) if m.args]


def _critical_test_ids_for_plan(plan):
    """Read the private docs and return the
    set of test IDs whose Priority column is "Critical".

    Returns an empty set when the CSV is missing or unreadable — the runtime
    smoke promoter is best-effort by design, and a broken CSV must not break
    test collection. That empty set is SILENT only when ``internal/`` is not
    checked out at all. When
    ``the private docs`` exists and the plan CSV is missing or corrupt, a
    RuntimeWarning fires instead: an empty promotion set means an empty smoke
    lane, pytest exit 5, and a CI "success" that ran nothing (review).
    """
    import csv
    import warnings

    testing_dir = os.path.join(_REPO_ROOT, "internal", "docs", "testing")
    csv_path = os.path.join(testing_dir, f"{plan.lower()}-test-plan.csv")

    def _no_promotion(reason):
        if os.path.isdir(testing_dir):
            warnings.warn(
                f"release-plan smoke promotion for {plan} is OFF — {reason} ({csv_path}). "
                "No test will carry release_plan_smoke, so the smoke lane collects nothing.",
                RuntimeWarning,
                stacklevel=3,
            )
        return set()

    if not os.path.isfile(csv_path):
        return _no_promotion("plan CSV not found")
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            return {
                (row.get("ID") or "").strip()
                for row in reader
                if (row.get("Priority") or "").strip().lower() == "critical" and (row.get("ID") or "").strip()
            }
    except (OSError, csv.Error, ValueError) as exc:  # UnicodeError is a ValueError subclass
        return _no_promotion(f"plan CSV unreadable: {exc!r}")


def pytest_collection_modifyitems(config, items):
    plan = os.environ.get("RELEASE_PLAN_NAME")
    if not plan:
        return

    # Runtime smoke-marker promotion (KAN-???): the source of truth for
    # smoke-set membership is the release-plan CSV's Priority column, not a
    # hand-added marker on the stub. Read the CSV once per collection and
    # add @pytest.mark.release_plan_smoke to any test whose test_id is
    # Critical in the active plan. This makes the marker non-forgettable —
    # all-stubs-missing-the-marker incident (2026-05-09) cannot
    # recur. Best-effort: a missing CSV skips promotion — silently only when
    # internal/ is absent, with a RuntimeWarning when it is present (see
    # _critical_test_ids_for_plan).
    critical_ids = _critical_test_ids_for_plan(plan)
    if critical_ids:
        for item in items:
            test_ids = _marker_args(item, "test_id")
            if test_ids and test_ids[0] in critical_ids:
                item.add_marker(pytest.mark.release_plan_smoke)

    selected = []
    deselected = []
    for item in items:
        plans = _marker_args(item, "release_plan")
        if plan in plans:
            selected.append(item)
        else:
            deselected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" and not (report.when == "setup" and report.outcome == "failed"):
        return

    target = os.environ.get("RELEASE_PLAN_REPORT")
    if not target:
        return

    test_ids = _marker_args(item, "test_id")
    if not test_ids:
        return
    test_id = test_ids[0]

    entry = {
        "outcome": report.outcome,  # "passed" | "failed" | "skipped"
        "duration": round(report.duration, 4),
        "file": str(item.fspath),
        "node": item.nodeid,
        "source": "pytest",
        "longrepr": str(report.longrepr) if report.failed else None,
    }

    container = getattr(item.session, "_release_plan_results", None)
    if container is None:
        container = {}
        item.session._release_plan_results = container
    container[test_id] = entry


def pytest_sessionfinish(session, exitstatus):
    target = os.environ.get("RELEASE_PLAN_REPORT")
    if not target:
        return
    results = getattr(session, "_release_plan_results", {}) or {}
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_plan": os.environ.get("RELEASE_PLAN_NAME") or None,
        "results": results,
    }
    target_dir = os.path.dirname(target)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)
    with open(target, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Canned Elasticsearch response helpers
# ---------------------------------------------------------------------------
def make_es_hits(docs):
    """Build a minimal ES search response with the given document list."""
    return {
        "hits": {
            "total": {"value": len(docs)},
            "hits": [{"_id": doc.get("id", f"id_{i}"), "_source": doc} for i, doc in enumerate(docs)],
        }
    }


# ---------------------------------------------------------------------------
# opt-in index-scope grant for route tests (lives here, not in
# tests/flask/conftest.py, because release-plan tests under the release-plan tests
# exercise the same routes).
# ---------------------------------------------------------------------------
@pytest.fixture
def allow_index_scope():
    """Grant ``validate_index_access`` for tests that exercise orthogonal
    route behaviour.

    Until the validator was a no-op that returned True for every
    authenticated user, so route tests never had to think about index scope.
    It is now real — built on the ``UserScope.allowed_indices`` — and a
    unit-test session with no ES backend resolves to the empty scope, so every
    ``data_view``-taking route returns 403 before reaching the logic under test.
    This fixture neutralises ONLY that one gate, in every loaded copy of the two
    modules that import the name (the web tree is importable under three
    package spellings). The gate itself is covered by
    ``the release-plan tests``.

    Opt in per-module with::

        pytestmark = pytest.mark.usefixtures("allow_index_scope")
    """
    import sys
    from contextlib import ExitStack

    targets = [
        name
        for name, mod in list(sys.modules.items())
        if (name.endswith(".app") or name.endswith("dashboard_builder.routes")) and hasattr(mod, "validate_index_access")
    ]
    assert targets, "app / dashboard_builder.routes not imported — fixture ordering problem"
    with ExitStack() as stack:
        for name in targets:
            stack.enter_context(mock.patch(f"{name}.validate_index_access", return_value=True))
        yield
